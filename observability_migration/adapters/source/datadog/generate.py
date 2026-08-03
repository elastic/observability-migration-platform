# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""YAML generation for kb-dashboard-cli format.

Converts TranslationResults into YAML structures matching the
kb-dashboard schema: dashboards[] → panels[] with size/position/esql blocks.

Layout strategy mirrors the Grafana tool: detect visual rows from source
positions, distribute panels evenly across 48 columns, apply type-appropriate
heights, then resolve any remaining overlaps.
"""

from __future__ import annotations

import math
import re
from typing import Any

import yaml

from observability_migration.core.assets.dashboard import DashboardIR
from observability_migration.core.assets.native_dashboard import NativeDashboard
from observability_migration.core.assets.visual import VisualIR
from observability_migration.core.reporting.report import _panel_query_index
from observability_migration.targets.kibana.dashboards_api import native_dashboard_from_ir
from observability_migration.targets.kibana.emit.esql_utils import extract_esql_shape
from observability_migration.targets.kibana.emit.layout import (
    PANEL_SIZE_CONSTRAINTS,
    apply_style_guide_layout,
)

from .curated_packs import load_curated_pack
from .display import enrich_panel_display
from .field_map import FieldMapProfile
from .models import (
    DISPLAY_TYPE_MAP,
    NormalizedDashboard,
    NormalizedWidget,
    TemplateVariable,
    TranslationResult,
)

GRID_COLUMNS = 48
KIBANA_MIN_VERSION = "9.5.0"
MIN_PANEL_WIDTH = 8

CHART_TYPE_MAP: dict[str, str] = {
    "xy": "line",
    "table": "datatable",
    "metric": "metric",
    "heatmap": "heatmap",
    "partition": "pie",
    "treemap": "treemap",
}

KIBANA_TYPE_HEIGHT: dict[str, int] = {
    "metric": 5,
    "gauge": 6,
    "line": 12,
    "bar": 12,
    "area": 12,
    "datatable": 15,
    "pie": 12,
    "treemap": 12,
    "heatmap": 12,
    "markdown": 6,
}
KIBANA_DEFAULT_HEIGHT = 8
_DATADOG_PRIVATE_PANEL_KEYS = (
    "_dd_y",
    "_dd_x",
    "_dd_w",
    "_dd_h",
    "_dd_type",
    "_dd_display_type",
    "_dd_widget_id",
    "_markdown_role",
)


PANEL_PRESENTATION_KINDS = ("markdown", "esql", "lens", "links", "image")


def _panel_presentation_kind(panel: dict[str, Any]) -> str:
    """Return a leaf panel's presentation block key (``markdown``, ``esql``, ...).

    Curated packs select notes by kind rather than title because emitted note
    titles are generated (``Datadog note <widget id>`` / ``Datadog note <ordinal>``)
    and are therefore not stable pack keys.
    """
    for kind in PANEL_PRESENTATION_KINDS:
        if kind in panel:
            return kind
    return ""


def _curated_spec_candidates(
    sec_panels: list[dict[str, Any]],
    layout_entry: dict[str, Any],
) -> list[dict[str, Any]]:
    """Leaf panels a curated pack spec selects, in section order (before ``nth``).

    A spec constrains the leaf title when ``title`` is given and non-empty, and
    the presentation kind when ``kind`` is given; an omitted selector matches
    anything, so ``kind: markdown`` + ``nth: 1`` means "the second markdown panel
    in this section" regardless of its generated title.
    """
    want_title = str(layout_entry.get("title", "") or "")
    want_kind = str(layout_entry.get("kind", "") or "")
    candidates: list[dict[str, Any]] = []
    for leaf in _iter_leaf_panels(sec_panels):
        if want_title and leaf.get("title", "") != want_title:
            continue
        if want_kind and _panel_presentation_kind(leaf) != want_kind:
            continue
        candidates.append(leaf)
    return candidates


def _warn_uncovered_curated_panels(
    dashboard_name: str,
    section_title: str,
    sec_panels: list[dict[str, Any]],
    covered: set[int],
    title_to_result: dict[str, TranslationResult],
) -> list[str]:
    """Report and self-heal a curated section that left panels unpositioned.

    A curated pack only moves the panels its specs match. When a spec stops
    matching (emitted titles changed, a widget was added upstream), the leftover
    panels keep auto-generated coordinates and can collide with the curated ones,
    so the miss is surfaced as an operator-visible warning on the affected panels
    and the generic overlap resolver is re-applied to the section. With a complete
    pack this is a no-op.
    """
    uncovered = [leaf for leaf in _iter_leaf_panels(sec_panels) if id(leaf) not in covered]
    if not uncovered:
        return []
    titles = [str(leaf.get("title", "") or "(untitled)") for leaf in uncovered]
    detail = (
        f"Curated layout pack for dashboard '{dashboard_name}' does not cover "
        f"{len(uncovered)} panel(s) in section '{section_title}': {', '.join(titles)}. "
        "Those panels kept their auto-generated positions and the generic overlap "
        "resolver was re-applied, so this section no longer matches the curated "
        "design. Add a matching panel spec (title, or kind + nth) to the pack."
    )
    for title in titles:
        result = title_to_result.get(title)
        if result is None:
            continue
        if detail not in result.warnings:
            result.warnings.append(detail)
    _resolve_overlaps(sec_panels)
    return titles


def _apply_curated_layout(
    doc: dict[str, Any],
    pack: dict[str, Any],
    results: list[TranslationResult] | None = None,
) -> None:
    """Apply size/position overrides from a curated Datadog pack.

    Each ``sections[].panels[]`` spec selects one leaf panel by ``title`` and/or
    ``kind`` plus ``nth`` (see :func:`_curated_spec_candidates`). Panels no spec
    matched are reported and de-overlapped by
    :func:`_warn_uncovered_curated_panels`, so a partially covered pack can never
    emit overlapping panels.
    """
    title_to_result: dict[str, TranslationResult] = {}
    for result in results or []:
        title = str(result.title or "")
        if title:
            title_to_result.setdefault(title, result)

    for dashboard in doc.get("dashboards", []):
        dashboard_name = str(dashboard.get("name", "") or "")
        panels = dashboard.get("panels", [])
        for section_spec in pack.get("sections", []):
            sec_title = section_spec.get("title", "")
            section_panel = next(
                (p for p in panels if p.get("title") == sec_title and "section" in p),
                None,
            )
            if section_panel is None:
                continue
            if "collapsed" in section_spec:
                section_panel["section"]["collapsed"] = section_spec["collapsed"]
            sec_panels = section_panel["section"].get("panels", [])
            covered: set[int] = set()
            for layout_entry in section_spec.get("panels", []):
                nth = int(layout_entry.get("nth", 0))
                candidates = _curated_spec_candidates(sec_panels, layout_entry)
                if nth < 0 or nth >= len(candidates):
                    continue
                leaf = candidates[nth]
                if "size" in layout_entry:
                    leaf["size"] = dict(layout_entry["size"])
                if "position" in layout_entry:
                    leaf["position"] = dict(layout_entry["position"])
                covered.add(id(leaf))
            _warn_uncovered_curated_panels(
                dashboard_name,
                str(sec_title),
                sec_panels,
                covered,
                title_to_result,
            )


def _build_dashboard_yaml_doc(
    dashboard: NormalizedDashboard,
    results: list[TranslationResult],
    data_view: str = "metrics-*",
    *,
    metrics_dataset_filter: str = "",
    logs_dataset_filter: str = "",
    logs_index: str = "logs-*",
    field_map: FieldMapProfile | None = None,
) -> dict[str, Any]:
    """Build the kb-dashboard-core YAML document dict for one dashboard.

    Shared by :func:`generate_dashboard_yaml` (the YAML string bridge) and
    :func:`generate_dashboard_artifacts` (which also derives a
    :class:`NativeDashboard` from this exact doc), so the two never drift.
    """
    panels = []
    result_map = {r.widget_id: r for r in results}
    _warn_mixed_template_variable_usage(dashboard, result_map)

    for widget in dashboard.widgets:
        result = result_map.get(widget.id)
        if not result:
            continue

        if widget.widget_type in ("group", "powerpack"):
            group_panel = _build_group_panel(widget, result_map, data_view)
            if group_panel:
                panels.append(group_panel)
            continue

        panel = _build_yaml_panel(widget, result, data_view)
        if panel:
            panels.append(panel)

    _ensure_unique_leaf_panel_titles(panels, result_map)

    non_section = [p for p in panels if "section" not in p]
    _apply_row_layout(non_section)

    for p in panels:
        if "section" in p:
            p.pop("size", None)
            p.pop("position", None)

    _resolve_overlaps(non_section)
    _strip_datadog_private_keys(panels)

    doc: dict[str, Any] = {
        "dashboards": [
            {
                "name": dashboard.title,
                "description": dashboard.description or f"Migrated from Datadog: {dashboard.title}",
                "minimum_kibana_version": KIBANA_MIN_VERSION,
                "settings": {"sync": {"cursor": True}},
                "panels": panels,
            }
        ]
    }

    filters = _infer_dashboard_filters(
        panels,
        metrics_index=data_view,
        logs_index=logs_index,
        metrics_dataset_filter=metrics_dataset_filter,
        logs_dataset_filter=logs_dataset_filter,
    )
    if filters:
        doc["dashboards"][0]["filters"] = filters

    controls = _build_controls_from_template_vars(
        dashboard.template_variables,
        data_view,
        field_map,
        logs_data_view=logs_index,
        widgets=dashboard.widgets,
    )
    if controls:
        doc["dashboards"][0]["controls"] = controls

    apply_style_guide_layout(doc)

    curated_pack = load_curated_pack(dashboard.title)
    if curated_pack:
        _apply_curated_layout(doc, curated_pack, results)

    return doc


def dashboard_yaml_from_ir(dashboard_ir: DashboardIR) -> str:
    """Serialise a :class:`DashboardIR` to its kb-dashboard YAML export.

    The single place that knows the dump options for the derived document, so
    every caller that wants to *read* the export (inspection helpers, the audit
    trace generator, tests) gets byte-identical output.

    This is deliberately *not* on the migration path: the run's artifacts are
    ``native/`` + ``ir/`` (see ``docs/architecture/asset-model.md``), so the
    string is built only where something actually consumes it.
    """
    return yaml.dump(
        {"dashboards": [dashboard_ir.to_yaml_dict()]},
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def generate_dashboard_yaml(
    dashboard: NormalizedDashboard,
    results: list[TranslationResult],
    data_view: str = "metrics-*",
    *,
    metrics_dataset_filter: str = "",
    logs_dataset_filter: str = "",
    logs_index: str = "logs-*",
    field_map: FieldMapProfile | None = None,
) -> str:
    """Generate a complete kb-dashboard YAML string for a dashboard.

    IR-first Phase 2: the string is a *derived export* of the semantic
    :class:`DashboardIR` (see :func:`generate_dashboard_artifacts`), not an
    independent rendering of the source widgets. It is an inspection helper --
    the migration run writes ``native/`` + ``ir/`` and never this string -- so
    it derives the document on demand rather than making every migrated
    dashboard pay for a serialisation nobody reads.
    """
    _native, _stats, dashboard_ir = generate_dashboard_artifacts(
        dashboard,
        results,
        data_view,
        metrics_dataset_filter=metrics_dataset_filter,
        logs_dataset_filter=logs_dataset_filter,
        logs_index=logs_index,
        field_map=field_map,
    )
    return dashboard_yaml_from_ir(dashboard_ir)


def generate_dashboard_artifacts(
    dashboard: NormalizedDashboard,
    results: list[TranslationResult],
    data_view: str = "metrics-*",
    *,
    metrics_dataset_filter: str = "",
    logs_dataset_filter: str = "",
    logs_index: str = "logs-*",
    field_map: FieldMapProfile | None = None,
    id_disambiguator: str = "",
) -> tuple[NativeDashboard, dict[str, Any], DashboardIR]:
    """Generate the NativeDashboard and the semantic DashboardIR.

    IR-first Phase 2 (mirrors Grafana's ``translate_dashboard``): the
    per-widget translators still assemble a kb-dashboard-core dict (the
    expensive, well-tested part of the pipeline), then that dict is
    converted to a :class:`DashboardIR` *before* the native mapping. From
    that point on the dict is no longer the source of truth -- both the typed
    Dashboards API payload (``native_dashboard_from_ir``) and the YAML export
    (``DashboardIR.to_yaml_dict``) are derived from the same IR, so they
    cannot drift from each other.

    Returns ``(native_dashboard, native_stats, dashboard_ir)``
    where ``native_stats`` has ``mapped``/``unmapped``/``sections``/
    ``controls``/``reasons`` (see :class:`NativeMappingCounts`).

    No YAML string is returned: dashboard YAML stopped being an artifact
    format, so anything that still wants to read the derived document asks
    for it explicitly via :func:`dashboard_yaml_from_ir`.

    ``id_disambiguator`` comes from the run's artifact-stem allocation and is
    non-empty only when another dashboard in the run has the same title; it
    keeps the two dashboards off one Kibana dashboard id (see
    ``targets/kibana/dashboards_api.py::_stable_dashboard_id_from_ir``).
    """
    doc = _build_dashboard_yaml_doc(
        dashboard,
        results,
        data_view,
        metrics_dataset_filter=metrics_dataset_filter,
        logs_dataset_filter=logs_dataset_filter,
        logs_index=logs_index,
        field_map=field_map,
    )
    # IR-first: `DashboardIR` is the primary working artifact from here on.
    dashboard_ir = DashboardIR.from_yaml_dict(doc["dashboards"][0], source_adapter="datadog")
    dashboard_ir.uid = str(dashboard.id or "")
    dashboard_ir.title = dashboard.title or dashboard_ir.title
    # Set before `native_dashboard_from_ir`: it is what keeps two same-titled
    # dashboards off one Kibana dashboard id (the upsert key).
    dashboard_ir.id_disambiguator = str(id_disambiguator or "")
    # The YAML document shape carries neither tags nor source lineage, so both
    # have to come off the normalized dashboard: otherwise they are absent from
    # ir/<stem>.ir.json, and because native_dashboard_from_ir reads tags
    # straight off the IR they are also stripped from the dashboard this run
    # uploads. Datadog tags keep their source ``key:value`` form rather than
    # being split, so no scoping information is invented or lost.
    dashboard_ir.tags = [str(tag) for tag in (dashboard.tags or [])]
    dashboard_ir.source_file = str(dashboard.source_file or "")
    template_vars_by_name = {
        variable.name: variable
        for variable in dashboard.template_variables
        if variable.name
    }
    for control_ir in dashboard_ir.controls:
        template_var = template_vars_by_name.get(control_ir.label)
        if template_var is None:
            continue
        control_ir.variable_name = template_var.name
        control_ir.variable_type = "datadog_template"
        control_ir.available_options = [
            str(value)
            for value in template_var.available_values
            if str(value)
        ]
    native_dashboard, counts = native_dashboard_from_ir(dashboard_ir)
    counts_dict, reasons = counts.as_dicts()
    stats: dict[str, Any] = {**counts_dict, "reasons": reasons}
    return native_dashboard, stats, dashboard_ir


def _iter_leaf_panels(panels: list[dict[str, Any]]):
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            yield from _iter_leaf_panels(section.get("panels") or [])
        else:
            yield panel


def _fallback_panel_title(panel: dict[str, Any], result: TranslationResult | None) -> str:
    source_type = str(
        panel.get("_dd_display_type")
        or panel.get("_dd_type")
        or (result.dd_widget_type if result else "")
        or "widget"
    ).replace("_", " ")
    widget_id = str(panel.get("_dd_widget_id") or (result.widget_id if result else "") or "").strip()
    suffix = f" {widget_id}" if widget_id else ""
    return f"Datadog {source_type}{suffix}".strip()


def _ensure_unique_leaf_panel_titles(
    panels: list[dict[str, Any]],
    result_map: dict[str, TranslationResult],
) -> None:
    """Keep emitted Datadog panel titles usable as render-audit keys.

    Datadog integration dashboards frequently omit widget titles or repeat the
    same short title across several tiles. Kibana can render that, but the
    migration report and render audit key panel metadata by title, so blanks or
    duplicates collapse per-panel verdicts. Assign stable emitted titles and keep
    the matching TranslationResult title in sync for the report.
    """
    used: set[str] = set()
    for ordinal, panel in enumerate(_iter_leaf_panels(panels), start=1):
        widget_id = str(panel.get("_dd_widget_id") or "")
        result = result_map.get(widget_id)
        base = str((result.title if result else "") or panel.get("title") or "").strip()
        if not base:
            base = _fallback_panel_title(panel, result)

        title = base
        if title in used:
            suffix = f"widget {widget_id}" if widget_id else str(ordinal)
            title = f"{base} ({suffix})"
            counter = 2
            while title in used:
                title = f"{base} ({suffix}-{counter})"
                counter += 1

        used.add(title)
        panel["title"] = title
        if result is not None:
            result.title = title


def _strip_datadog_private_keys(panels: list[dict[str, Any]]) -> None:
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            _strip_datadog_private_keys(section.get("panels") or [])
        for key in _DATADOG_PRIVATE_PANEL_KEYS:
            panel.pop(key, None)


def _options_list_field_name(es_field: str) -> str:
    """Normalize a mapped tag to a field name options-list controls can resolve.

    Kibana options-list looks fields up on the data view. Multi-fields such as
    ``k8s.node.name.keyword`` often appear in ``_field_caps`` (and may be what
    ``map_tag`` prefers for aggregations when the base field looked unsafe at
    migrate time), but they are registered as ``subType.multi`` children.
    Options-list then fails with ``Could not locate field: ….keyword`` even
    when the parent keyword / TSDS dimension works. Prefer the parent name for
    dashboard controls so filters load.
    """
    field = str(es_field or "").strip()
    if field.endswith(".keyword"):
        return field[: -len(".keyword")]
    return field


def _build_controls_from_template_vars(
    template_vars: list[TemplateVariable],
    data_view: str,
    field_map: FieldMapProfile | None,
    *,
    logs_data_view: str = "logs-*",
    widgets: list[NormalizedWidget] | None = None,
) -> list[dict[str, Any]]:
    """Build Kibana dashboard controls from Datadog template variables.

    Maps each template variable's tag to an ES field via the field map and
    emits an ``options`` control that Kibana applies as a dashboard-level filter.
    """
    _UNRESOLVABLE_VARS = {"scope"}

    controls: list[dict[str, Any]] = []
    for tv in template_vars:
        tag = tv.tag or tv.prefix
        if not tag:
            if tv.name.lower() in _UNRESOLVABLE_VARS:
                continue
            tag = tv.name
        if not tag:
            continue
        # A template-variable prefix can use Datadog's "@tag" facet syntax
        # (e.g. "@host"); the facet refers to the same tag key as "host", so
        # strip the leading "@" before mapping. Without this the control binds
        # to a literal "@host" field that doesn't exist, leaving an empty
        # dropdown that can't filter. (map_tag must NOT strip globally: "@attr"
        # is a real field name in the log-query path.)
        tag = tag.lstrip("@")
        context = _template_variable_query_context(tv.name, widgets or [])
        control_data_view = logs_data_view if context == "log" else data_view
        es_field = field_map.map_tag(tag, context=context) if field_map else tag
        control_field = _options_list_field_name(es_field)
        control: dict[str, Any] = {
            "type": "options",
            "label": tv.name,
            "data_view": control_data_view,
            "field": control_field,
            "multiple": len(tv.defaults) > 1 or tv.default == "*",
        }
        preselected = _template_var_preselected(tv)
        # Datadog template defaults are source vocabulary. When the tag was
        # remapped onto a different target field (env → deployment.environment),
        # those defaults often do not exist as field values (e.g. "prod" vs
        # OTel "production"). Options-list applies them as filters and empties
        # every panel with "selection returns no results". Skip preselect in
        # that remapped case; operators pick live field values instead.
        if preselected and _tag_was_remapped(tag, control_field):
            preselected = []
        if preselected:
            control["preselected"] = preselected
        controls.append(control)
    return controls


def _tag_was_remapped(source_tag: str, control_field: str) -> bool:
    """True when the options-list field is not the source tag (or its .keyword)."""
    source = str(source_tag or "").strip()
    field = str(control_field or "").strip()
    if not source or not field:
        return False
    return field not in {source, f"{source}.keyword"}


def _template_variable_query_context(
    variable_name: str,
    widgets: list[NormalizedWidget],
) -> str:
    """Return ``log`` only when a variable is referenced exclusively by logs."""
    log_widget_ids, metric_widget_ids = _template_variable_usage(
        variable_name,
        widgets,
    )
    return "log" if log_widget_ids and not metric_widget_ids else "metric"


def _template_variable_usage(
    variable_name: str,
    widgets: list[NormalizedWidget],
) -> tuple[set[str], set[str]]:
    escaped_name = re.escape(variable_name)
    reference_re = re.compile(
        rf"\$(?:\{{{escaped_name}(?::[^}}]+)?\}}|{escaped_name}\b)"
    )
    log_widget_ids: set[str] = set()
    metric_widget_ids: set[str] = set()

    pending = list(widgets)
    while pending:
        widget = pending.pop()
        pending.extend(widget.children)
        for query in widget.queries:
            if not reference_re.search(query.raw_query or ""):
                continue
            if query.log_query is not None or query.data_source == "logs":
                log_widget_ids.add(widget.id)
            if query.metric_query is not None or query.data_source == "metrics":
                metric_widget_ids.add(widget.id)

    return log_widget_ids, metric_widget_ids


def _warn_mixed_template_variable_usage(
    dashboard: NormalizedDashboard,
    result_map: dict[str, TranslationResult],
) -> None:
    for variable in dashboard.template_variables:
        log_widget_ids, metric_widget_ids = _template_variable_usage(
            variable.name,
            dashboard.widgets,
        )
        if not log_widget_ids or not metric_widget_ids:
            continue
        detail = (
            f"Template variable '${variable.name}' is used by both metric and "
            "log widgets; the migrated options-list control targets the metrics "
            "data view because one Kibana control cannot target both data views. "
            "Recreate a separate logs control or filter in Kibana"
        )
        for widget_id in log_widget_ids | metric_widget_ids:
            result = result_map.get(widget_id)
            if result is None:
                continue
            if detail not in result.warnings:
                result.warnings.append(detail)
            if detail not in result.semantic_losses:
                result.semantic_losses.append(detail)
            if result.status == "ok":
                result.status = "warning"


def _template_var_preselected(tv: TemplateVariable) -> list[str]:
    values = [str(value) for value in tv.defaults if str(value) and str(value) != "*"]
    if values:
        return values
    if tv.default and tv.default != "*":
        return [str(tv.default)]
    return []


def _panel_data_index(panel: dict[str, Any]) -> str:
    """Extract the data index from an esql or lens panel."""
    idx = _panel_query_index(panel)
    if idx:
        return idx
    lens = panel.get("lens")
    if isinstance(lens, dict):
        return lens.get("data_view", "")
    return ""


def _infer_dashboard_filters(
    yaml_panels: list[dict[str, Any]],
    *,
    metrics_index: str,
    logs_index: str,
    metrics_dataset_filter: str,
    logs_dataset_filter: str,
) -> list[dict[str, str]]:
    """Infer dashboard-level ``data_stream.dataset`` filters from panel indexes.

    Mirrors the Grafana path logic: apply the filter only when all panels
    target the same data stream family (all-metrics or all-logs).  Mixed
    dashboards get no filter (safe default).
    """
    all_panels = list(yaml_panels)
    for p in yaml_panels:
        section = p.get("section")
        if section and isinstance(section, dict):
            all_panels.extend(section.get("panels") or [])

    indexes = {_panel_data_index(p) for p in all_panels if _panel_data_index(p)}
    if not indexes:
        return []

    if indexes == {logs_index}:
        if not logs_dataset_filter:
            return []
        return [{"field": "data_stream.dataset", "equals": logs_dataset_filter}]

    if logs_index in indexes:
        return []

    if metrics_index and not indexes.issubset({metrics_index}):
        return []

    if not metrics_dataset_filter:
        return []
    return [{"field": "data_stream.dataset", "equals": metrics_dataset_filter}]


def _warn_dropped_xy_breakdowns(
    non_time_dims: list[str], result: TranslationResult
) -> None:
    """Warn when an XY chart cannot show every grouping dimension.

    A Kibana XY chart (line/bar/area) breaks its series down by a single field.
    When the source query groups by two or more non-time dimensions, only the
    first becomes the visual breakdown and the rest are absent from the chart,
    so series differing only by a dropped dimension are visually merged. Surface
    that as a warning instead of silently rendering a different shape than the
    Datadog source.
    """
    extra = [d for d in non_time_dims[1:] if d]
    if not extra:
        return
    warning = (
        "XY chart shows a single breakdown; additional grouping "
        f"dimension(s) {extra} are in the query but not on the chart, "
        "so series differing only by those are visually merged"
    )
    if warning not in result.warnings:
        result.warnings.append(warning)


def _build_yaml_panel(
    widget: NormalizedWidget,
    result: TranslationResult,
    data_view: str,
) -> dict[str, Any] | None:
    """Build a single YAML panel dict in kb-dashboard schema."""

    if result.status in ("blocked", "skipped"):
        return None

    layout = widget.layout
    dd_x = int(layout.get("x") or 0)
    dd_y = int(layout.get("y") or 0)
    dd_w = int(layout.get("width") or 0)

    if result.backend == "image":
        panel = _build_image_panel(widget, result, 0, 0, 12, 6)
    elif result.backend == "markdown" or result.status in ("not_feasible", "requires_manual"):
        panel = _build_markdown_panel(widget, result, 0, 0, 8, 6)
    elif result.backend == "lens" and result.yaml_panel and result.yaml_panel.get("type") == "lens":
        panel = _build_lens_panel(widget, result, data_view, 0, 0, 8, 8)
    elif result.esql_query:
        panel = _build_esql_panel(widget, result, data_view, 0, 0, 8, 8)
    else:
        panel = _build_markdown_panel(widget, result, 0, 0, 8, 6)

    if panel and "esql" in panel:
        enrich_panel_display(panel, widget, result)
    result.yaml_panel = panel or {}
    # Stamp source id before visual IR so report/lint pairing can key on it.
    # Underscore keys are stripped by DashboardIR export; visual_ir keeps the
    # typed presentation for Grafana-parity reporting.
    if panel is not None:
        panel["_dd_y"] = dd_y
        panel["_dd_x"] = dd_x
        panel["_dd_w"] = dd_w
        panel["_dd_h"] = int(layout.get("height", 2) or 2)
        panel["_dd_type"] = widget.widget_type
        panel["_dd_display_type"] = widget.display_type
        panel["_dd_widget_id"] = widget.id
        if widget.id and not panel.get("_source_panel_id"):
            panel["_source_panel_id"] = str(widget.id)
    result.source_panel_id = result.source_panel_id or str(widget.id or "")
    query_ir = result.query_ir if isinstance(result.query_ir, dict) else {}
    result.visual_ir = VisualIR.from_yaml_panel(
        panel,
        source_panel_id=result.source_panel_id,
        grafana_type=str(widget.widget_type or result.dd_widget_type or ""),
        kibana_type=str(result.kibana_type or ""),
        warnings=[str(item) for item in (result.warnings or []) + (result.reasons or [])],
        metadata={
            "query_language": str(result.query_language or ""),
            "output_shape": str(query_ir.get("output_shape", "") or ""),
            "source_adapter": "datadog",
        },
    )
    return panel


def _build_esql_panel(
    widget: NormalizedWidget,
    result: TranslationResult,
    data_view: str,
    x: int, y: int, w: int, h: int,
) -> dict[str, Any]:
    """Build an ES|QL-powered panel matching kb-dashboard schema."""
    chart_type = CHART_TYPE_MAP.get(result.kibana_type, "line")
    display_type = DISPLAY_TYPE_MAP.get(widget.display_type, "")
    mode: str | None = None
    if chart_type == "line":
        if display_type == "bar_stacked":
            chart_type = "bar"
            mode = "stacked"
        elif display_type in ("bar", "area"):
            chart_type = display_type

    panel: dict[str, Any] = {
        "title": result.title or widget.title or "Untitled",
        "size": {"w": w, "h": h},
        "position": {"x": x, "y": y},
    }

    esql_block: dict[str, Any] = {
        "type": chart_type,
        "query": result.esql_query,
    }
    if mode:
        esql_block["mode"] = mode

    dims = _infer_dimensions(result)
    metrics = _infer_metrics(result)

    if result.kibana_type == "xy":
        if dims:
            time_dim = next((d for d in dims if "time" in d.lower() or "bucket" in d.lower()), None)
            if time_dim:
                esql_block["dimension"] = _dimension_config(time_dim, data_type="date")
            other_dims = [d for d in dims if d != time_dim]
            if len(other_dims) >= 2:
                # Mirror heatmap multi-tag compositing: Lens XY has one
                # breakdown field, so CONCAT the categorical dims instead of
                # silently dropping all but the first.
                new_query, breakdown_field = _composite_y_column(
                    esql_block["query"], other_dims, name="series_group"
                )
                esql_block["query"] = new_query
                result.esql_query = new_query
                esql_block["breakdown"] = _dimension_config(breakdown_field)
                warning = (
                    "XY chart grouped by multiple tags "
                    f"({', '.join(other_dims)}); composited into a single "
                    "breakdown column"
                )
                if warning not in result.warnings:
                    result.warnings.append(warning)
            elif other_dims:
                esql_block["breakdown"] = _dimension_config(other_dims[0])
        if metrics:
            esql_block["metrics"] = [
                _metric_config(widget, result, m)
                for m in metrics
            ]
        esql_block["appearance"] = {
            "x_axis": {"title": False},
            "y_left_axis": {"title": False},
            "y_right_axis": {"title": False},
        }

    elif result.kibana_type == "metric":
        if metrics:
            esql_block["primary"] = _metric_config(widget, result, metrics[0])

    elif result.kibana_type == "table":
        if widget.widget_type in ("log_stream", "list_stream"):
            keep_fields = _infer_keep_fields(result.esql_query)
            esql_block["breakdowns"] = [
                _dimension_config(field, data_type="date" if field == "@timestamp" else None)
                for field in keep_fields
            ]
        else:
            if widget.widget_type == "change" and "value" not in metrics:
                # A change widget ranks by the computed delta `value` (the SORT
                # key), which STATS-based inference misses because it is an EVAL
                # alias. Surface it as the leading metric so the ranked delta —
                # the whole point of the widget — is actually displayed.
                metrics = ["value", *metrics]
            if metrics:
                esql_block["metrics"] = [
                    _metric_config(widget, result, m)
                    for m in metrics
                ]
                if widget.widget_type == "change" and esql_block["metrics"][0]["field"] == "value":
                    esql_block["metrics"][0]["label"] = "Change"
            non_time_dims = [d for d in dims if "time" not in d.lower() and "bucket" not in d.lower()]
            if non_time_dims:
                esql_block["breakdowns"] = [_dimension_config(b) for b in non_time_dims]

    elif result.kibana_type == "partition":
        if metrics:
            esql_block["metrics"] = [
                _metric_config(widget, result, m)
                for m in metrics
            ]
        breakdown = [d for d in dims if "time" not in d.lower() and "bucket" not in d.lower()]
        if not breakdown:
            breakdown = dims[:1] if dims else ["value"]
        esql_block["breakdowns"] = [_dimension_config(b) for b in breakdown]
        esql_block["legend"] = {"visible": "auto", "truncate_labels": 1}

    elif result.kibana_type == "treemap":
        if metrics:
            esql_block["metric"] = _metric_config(widget, result, metrics[0])
        breakdown = [d for d in dims if "time" not in d.lower() and "bucket" not in d.lower()]
        if not breakdown:
            breakdown = dims[:1] if dims else ["value"]
            warning = "treemap had no categorical breakdown; using fallback column"
            if warning not in result.warnings:
                result.warnings.append(warning)
        esql_block["breakdowns"] = [_dimension_config(b) for b in breakdown[:2]]
        esql_block["legend"] = {"visible": "auto", "truncate_labels": 1}

    elif result.kibana_type == "heatmap":
        if metrics:
            esql_block["metric"] = _metric_config(widget, result, metrics[0])
        time_dim = next((d for d in dims if "time" in d.lower() or "bucket" in d.lower()), None)
        other_dims = [d for d in dims if d != time_dim]
        if time_dim:
            esql_block["x_axis"] = _dimension_config(time_dim, data_type="date")
        elif dims:
            esql_block["x_axis"] = _dimension_config(dims[0])
        else:
            esql_block["x_axis"] = _dimension_config("@timestamp", data_type="date")
        if len(other_dims) >= 2:
            # The heatmap Y axis is a single field, but the query groups by two
            # (or more) explicit categorical tags the user chose (e.g. service +
            # host). Using only the first merges distinct buckets, so composite
            # the grouping tags into one synthetic Y column instead of dropping
            # the rest.
            new_query, y_field = _composite_y_column(esql_block["query"], other_dims)
            esql_block["query"] = new_query
            result.esql_query = new_query
            y_cfg: dict[str, Any] = {"field": y_field}
            label = " / ".join(lbl for lbl in (_pretty_field_label(d) for d in other_dims) if lbl)
            if label:
                y_cfg["label"] = label
            esql_block["y_axis"] = y_cfg
            warning = (
                "Heatmap grouped by multiple tags "
                f"({', '.join(other_dims)}); composited into a single Y axis column"
            )
            if warning not in result.warnings:
                result.warnings.append(warning)
        elif other_dims:
            esql_block["y_axis"] = _dimension_config(other_dims[0])
        esql_block.setdefault("appearance", {})["legend"] = {
            "visible": "show",
            "position": "right",
        }

    panel["esql"] = esql_block
    return panel


def _build_lens_panel(
    widget: NormalizedWidget,
    result: TranslationResult,
    data_view: str,
    x: int, y: int, w: int, h: int,
) -> dict[str, Any]:
    """Build a Lens-backed panel in kb-dashboard schema.

    Lens panels declare a data_view reference and aggregation config
    rather than a raw ES|QL query string.  The schema requires different
    structures per chart type: ``primary`` for metrics, ``dimension`` /
    ``metrics`` / ``breakdown`` for XY charts, etc.
    """
    lens_cfg = result.yaml_panel or {}
    chart_type = CHART_TYPE_MAP.get(result.kibana_type, "line")

    panel: dict[str, Any] = {
        "title": result.title or widget.title or "Untitled",
        "size": {"w": w, "h": h},
        "position": {"x": x, "y": y},
    }

    _LENS_AGG_NAMES: dict[str, str] = {
        "avg": "average", "AVG": "average", "average": "average",
        "sum": "sum", "SUM": "sum",
        "min": "min", "MIN": "min",
        "max": "max", "MAX": "max",
        "count": "count", "COUNT": "count",
        "last": "last_value", "LAST": "last_value", "last_value": "last_value",
        "median": "median",
        "standard_deviation": "standard_deviation",
        "unique_count": "unique_count",
    }
    metric_field = lens_cfg.get("metric_field", "value")
    raw_agg = lens_cfg.get("aggregation", "avg")
    aggregation = _LENS_AGG_NAMES.get(raw_agg, raw_agg.lower())
    percentile = None
    percentile_match = re.fullmatch(r"PERCENTILE\(%\s*,\s*(\d+)\)", str(raw_agg or ""), re.IGNORECASE)
    if percentile_match:
        aggregation = "percentile"
        percentile = int(percentile_match.group(1))
    dv = lens_cfg.get("data_view", data_view)
    group_by = lens_cfg.get("group_by", [])

    lens_block: dict[str, Any] = {"type": chart_type, "data_view": dv}
    metric_config = {"aggregation": aggregation, "field": metric_field}
    if percentile is not None:
        metric_config["percentile"] = percentile

    if chart_type == "metric":
        lens_block["primary"] = dict(metric_config)
    elif chart_type in ("line", "bar", "area"):
        lens_block["dimension"] = {"type": "date_histogram", "field": "@timestamp"}
        lens_block["metrics"] = [dict(metric_config)]
        if group_by:
            lens_block["breakdown"] = {"type": "values", "field": group_by[0]}
            _warn_dropped_xy_breakdowns(group_by, result)
    elif chart_type == "pie" or chart_type == "datatable":
        lens_block["metrics"] = [dict(metric_config)]
        if group_by:
            lens_block["breakdowns"] = [{"type": "values", "field": g} for g in group_by]
    else:
        lens_block["primary"] = dict(metric_config)

    panel["lens"] = lens_block
    return panel


_STATUS_PLACEHOLDER_HINTS: dict[str, str] = {
    "check_status": (
        "Datadog **check_status** widgets show the health of a synthetic "
        "check (HTTP, TCP, SSL, etc.). The closest Elastic equivalent is a "
        "[Synthetics monitor](https://www.elastic.co/guide/en/observability/current/monitor-uptime-synthetics.html); "
        "configure one targeting the same endpoint, then visualize its "
        "status field on this panel."
    ),
    "manage_status": (
        "Datadog **manage_status** widgets summarize the state of one or "
        "more monitors. The closest Elastic equivalent is the "
        "[Alerts UI](https://www.elastic.co/guide/en/kibana/current/create-and-manage-rules.html); "
        "create matching rules in Kibana and link to them from this panel."
    ),
}


def _build_markdown_panel(
    widget: NormalizedWidget,
    result: TranslationResult,
    x: int, y: int, w: int, h: int,
) -> dict[str, Any]:
    """Build a markdown panel matching kb-dashboard schema."""
    is_text_widget = widget.widget_type in ("note", "free_text", "image", "iframe")

    if is_text_widget:
        content = _extract_text_content(widget)
    else:
        lines = [f"**{result.title or widget.title or 'Untitled'}**", ""]
        lines.append(f"Original widget type: {widget.widget_type}")
        lines.append(f"Migration status: {result.status}")
        hint = _STATUS_PLACEHOLDER_HINTS.get(widget.widget_type)
        if hint:
            lines.append("")
            lines.append(hint)
        if result.source_queries:
            lines.append("")
            for sq in result.source_queries[:3]:
                lines.append(f"```\n{sq}\n```")
        if result.warnings:
            lines.append("")
            for w_msg in result.warnings[:3]:
                lines.append(f"- {w_msg}")
        content = "\n".join(lines)

    panel = {
        "title": result.title or widget.title or "",
        "size": {"w": w, "h": h},
        "position": {"x": x, "y": y},
        "markdown": {"content": content},
    }
    panel["_markdown_role"] = "text" if is_text_widget else "placeholder"
    return panel


_DATADOG_IMAGE_FIT_MAP = {
    "fill": "fill",
    "contain": "contain",
    "cover": "cover",
    "none": "none",
    # Datadog's deprecated aliases retain their closest CSS object-fit
    # semantics on the Kibana image panel.
    "fit": "contain",
    "zoom": "cover",
    "center": "none",
    # Kibana has no scale-down enum; contain is the non-cropping fallback.
    "scale-down": "contain",
}


def _build_image_panel(
    widget: NormalizedWidget,
    result: TranslationResult,
    x: int, y: int, w: int, h: int,
) -> dict[str, Any]:
    """Build a native ``image`` YAML panel (kb-dashboard-core ``ImagePanel``).

    Only reached when ``image_widget_rule`` confirmed an absolute http(s) URL
    (see planner.py); the relative/static-asset fallback still goes through
    ``_build_markdown_panel``.
    """
    url = str(widget.raw_definition.get("url") or "").strip()
    image_config: dict[str, Any] = {"from_url": url}
    source_sizing = str(widget.raw_definition.get("sizing") or "").strip().lower()
    fit = _DATADOG_IMAGE_FIT_MAP.get(source_sizing)
    if fit:
        image_config["fit"] = fit
    if source_sizing == "scale-down":
        result.warnings.append("image sizing scale-down approximated as contain")
        if result.status == "ok":
            result.status = "warning"

    panel = {
        "title": result.title or widget.title or "",
        "size": {"w": w, "h": h},
        "position": {"x": x, "y": y},
        "image": image_config,
    }
    return panel


def _build_group_panel(
    widget: NormalizedWidget,
    result_map: dict[str, TranslationResult],
    data_view: str,
) -> dict[str, Any] | None:
    """Build a section/group panel with its children."""
    child_panels: list[dict[str, Any]] = []

    for child in widget.children:
        child_result = result_map.get(child.id)
        if not child_result:
            continue
        panel = _build_yaml_panel(child, child_result, data_view)
        if panel:
            child_panels.append(panel)

    if not child_panels:
        return None

    _apply_row_layout(child_panels)
    _resolve_overlaps(child_panels)

    return {
        "title": widget.title or "Section",
        "section": {
            "collapsed": False,
            "panels": child_panels,
        },
    }


def _extract_text_content(widget: NormalizedWidget) -> str:
    defn = widget.raw_definition
    if widget.widget_type == "note":
        return defn.get("content") or ""
    if widget.widget_type == "free_text":
        return defn.get("text") or ""
    if widget.widget_type == "image":
        url = defn.get("url", "")
        if not url:
            return ""
        if url.startswith("/static/") or not url.startswith(("http://", "https://")):
            return f"*(Datadog image: {url} — replace with a publicly accessible URL)*"
        return f"![image]({url})"
    if widget.widget_type == "iframe":
        url = defn.get("url", "")
        return f"[Embedded content]({url})" if url else ""
    return ""


# ---------------------------------------------------------------------------
# Layout: row-based distribution (adopted from Grafana tool)
# ---------------------------------------------------------------------------

_DD_TYPE_KIBANA_MAP: dict[str, str] = {
    "query_value": "metric",
    "change": "metric",
    "slo": "metric",
    "check_status": "metric",
    "timeseries": "line",
    "heatmap": "heatmap",
    "distribution": "line",
    "scatter_plot": "line",
    "geomap": "line",
    "sunburst": "pie",
    "funnel": "bar",
    "toplist": "datatable",
    "table": "datatable",
    "list_stream": "datatable",
    "log_stream": "datatable",
    "treemap": "treemap",
    "note": "markdown",
    "free_text": "markdown",
    "image": "markdown",
    "iframe": "markdown",
    "hostmap": "datatable",
}


def _kibana_panel_type(panel: dict[str, Any]) -> str:
    """Return the effective Kibana visualization type for height lookup."""
    esql = panel.get("esql")
    if isinstance(esql, dict):
        return esql.get("type", "line")
    lens = panel.get("lens")
    if isinstance(lens, dict):
        return lens.get("type", "line")
    if "markdown" in panel:
        return "markdown"
    dd_type = panel.get("_dd_type", "")
    if dd_type in _DD_TYPE_KIBANA_MAP:
        return _DD_TYPE_KIBANA_MAP[dd_type]
    return "metric"




def _apply_row_layout(panels: list[dict[str, Any]]) -> None:
    """Kibana-native row layout: proportional for free-form, heuristic for ordered."""
    if not panels:
        return

    source_rows = _collect_source_rows(panels)

    if _is_ordered_layout(source_rows):
        rows = _transform_rows(source_rows)
        _apply_heuristic_layout(rows)
    else:
        rows = _split_intro_markdown_rows(source_rows)
        rows = _split_placeholder_rows(rows)
        _apply_proportional_layout(rows)

    _normalize_tile_sizes(panels)


def _is_ordered_layout(source_rows: list[list[dict[str, Any]]]) -> bool:
    """Detect ordered/stacked layout where all panels sit at x=0 with uniform width."""
    return all(
        len(row) == 1 and int(row[0].get("_dd_x", 0) or 0) == 0
        for row in source_rows
    )


def _effective_panel_height(panel: dict[str, Any], width: int | None = None) -> int:
    """Preferred height clamped to the panel type's (min_h, max_h).

    The layout y-cursor must advance by the height a tile will ACTUALLY have
    after ``_normalize_tile_sizes`` floors it to min_h (and caps at max_h). Using
    the raw preferred height desyncs the cursor (e.g. a query_value's preferred 5
    vs metric min_h 6), so the next row lands a row too high; ``_resolve_overlaps``
    then pushes only the panels that overlap the row above, splitting the row and
    leaving an overlap that ``_fill_simple_row`` can widen into a real collision.
    """
    h = _preferred_panel_height(panel, width)
    constraints = PANEL_SIZE_CONSTRAINTS.get(_kibana_panel_type(panel))
    if constraints is not None:
        _min_w, min_h, max_h = constraints
        h = max(h, min_h)
        if max_h is not None:
            h = min(h, max_h)
    return h


def _apply_heuristic_layout(rows: list[list[dict[str, Any]]]) -> None:
    """Layout using family-based width heuristics (for ordered/stacked dashboards)."""
    y_cursor = 0
    for row_panels in rows:
        widths = _plan_row_widths(row_panels)
        heights = [
            _effective_panel_height(panel, width)
            for panel, width in zip(row_panels, widths)
        ]
        row_height = max(heights) if heights else KIBANA_DEFAULT_HEIGHT
        x_cursor = 0
        for panel, width, height in zip(row_panels, widths, heights):
            panel["size"] = {"w": width, "h": height}
            panel["position"] = {"x": x_cursor, "y": y_cursor}
            x_cursor += width
        y_cursor += row_height


def _apply_proportional_layout(rows: list[list[dict[str, Any]]]) -> None:
    """Scale source coordinates proportionally to the 48-column Kibana grid.

    Uses the span of each row (min_x..max_extent) so that rows produced
    by splitting transforms still map correctly even when x offsets are
    non-zero.
    """
    y_cursor = 0
    for row_panels in rows:
        # A lone stat/metric tile must not balloon to the full grid: span-based
        # scaling derives col_scale from the panel's own extent, so a 3/12-wide
        # query_value would stretch to all 48 columns (one number across the
        # whole dashboard). Give it the single-metric width the heuristic branch
        # already uses (_plan_row_widths -> 24); charts/tables still expand.
        if len(row_panels) == 1 and _panel_family(row_panels[0]) == "metric":
            panel = row_panels[0]
            w = _plan_row_widths(row_panels)[0]
            h = _effective_panel_height(panel, w)
            panel["size"] = {"w": w, "h": h}
            panel["position"] = {"x": 0, "y": y_cursor}
            y_cursor += h
            continue

        xs = [int(p.get("_dd_x", 0) or 0) for p in row_panels]
        ws = [int(p.get("_dd_w", 1) or 1) for p in row_panels]
        source_min_x = min(xs) if xs else 0
        source_max_extent = max(x + w for x, w in zip(xs, ws)) if xs else 1
        source_span = max(source_max_extent - source_min_x, 1)
        col_scale = GRID_COLUMNS / source_span

        for panel, dd_x, dd_w in zip(row_panels, xs, ws):
            w = max(MIN_PANEL_WIDTH, round(dd_w * col_scale))
            x = round((dd_x - source_min_x) * col_scale)
            h = _effective_panel_height(panel, w)
            panel["size"] = {"w": w, "h": h}
            panel["position"] = {"x": x, "y": y_cursor}

        _adjust_row_to_grid(row_panels)

        row_height = max(
            (p.get("size", {}).get("h", KIBANA_DEFAULT_HEIGHT) for p in row_panels),
            default=KIBANA_DEFAULT_HEIGHT,
        )
        y_cursor += row_height


def _adjust_row_to_grid(row_panels: list[dict[str, Any]]) -> None:
    """Ensure row panels fill exactly 48 columns with contiguous positions."""
    if not row_panels:
        return

    total = sum(p["size"]["w"] for p in row_panels)
    diff = GRID_COLUMNS - total

    if diff != 0:
        indices = sorted(
            range(len(row_panels)),
            key=lambda i: -row_panels[i]["size"]["w"],
        )
        for i in indices:
            if diff == 0:
                break
            if diff > 0:
                row_panels[i]["size"]["w"] += 1
                diff -= 1
            elif row_panels[i]["size"]["w"] > MIN_PANEL_WIDTH:
                row_panels[i]["size"]["w"] -= 1
                diff += 1

    x = 0
    for p in row_panels:
        p["position"]["x"] = x
        x += p["size"]["w"]


def _collect_source_rows(panels: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = {}
    for panel in panels:
        dd_y = int(panel.get("_dd_y", 0) or 0)
        rows.setdefault(dd_y, []).append(panel)
    return [sorted(rows[dd_y], key=lambda p: p.get("_dd_x", 0)) for dd_y in sorted(rows)]


def _transform_rows(rows: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    rows = _split_intro_markdown_rows(rows)
    rows = _split_placeholder_rows(rows)
    rows = _merge_placeholder_rows(rows)
    return _merge_consecutive_singletons(rows)


def _split_intro_markdown_rows(rows: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    transformed: list[list[dict[str, Any]]] = []
    for row in rows:
        intro_markdown = [panel for panel in row if _markdown_role(panel) == "text"]
        others = [panel for panel in row if _markdown_role(panel) != "text"]
        if intro_markdown and others and len(row) >= 3:
            transformed.append(intro_markdown)
            transformed.append(others)
        else:
            transformed.append(row)
    return transformed


def _split_placeholder_rows(rows: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    transformed: list[list[dict[str, Any]]] = []
    for row in rows:
        placeholders = [panel for panel in row if _markdown_role(panel) == "placeholder"]
        others = [panel for panel in row if _markdown_role(panel) != "placeholder"]
        if placeholders and others:
            transformed.append(others)
            transformed.append(placeholders)
        else:
            transformed.append(row)
    return transformed


def _merge_placeholder_rows(rows: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    merged: list[list[dict[str, Any]]] = []
    idx = 0
    while idx < len(rows):
        row = rows[idx]
        if len(row) == 1 and _markdown_role(row[0]) == "placeholder":
            bucket = list(row)
            idx += 1
            while idx < len(rows) and len(bucket) < 2:
                next_row = rows[idx]
                if len(next_row) != 1 or _markdown_role(next_row[0]) != "placeholder":
                    break
                bucket.extend(next_row)
                idx += 1
            merged.append(bucket)
            continue
        merged.append(row)
        idx += 1
    return merged


def _merge_consecutive_singletons(rows: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    merged: list[list[dict[str, Any]]] = []
    idx = 0
    while idx < len(rows):
        row = rows[idx]
        if _is_mergeable_singleton_row(row):
            family = _panel_family(row[0])
            limit = 4 if family == "metric" else 2
            bucket = [row[0]]
            idx += 1
            while idx < len(rows) and len(bucket) < limit:
                candidate = rows[idx]
                if not _is_mergeable_singleton_row(candidate):
                    break
                if _panel_family(candidate[0]) != family:
                    break
                bucket.append(candidate[0])
                idx += 1
            merged.append(bucket)
            continue
        merged.append(row)
        idx += 1
    return merged


def _is_mergeable_singleton_row(row: list[dict[str, Any]]) -> bool:
    return len(row) == 1 and _panel_family(row[0]) in {"metric", "table", "chart"}


def _plan_row_widths(row_panels: list[dict[str, Any]]) -> list[int]:
    families = [_panel_family(panel) for panel in row_panels]
    n = len(row_panels)

    if all(family == "markdown" for family in families):
        if n == 1:
            return [GRID_COLUMNS]
        if n == 2:
            return [24, 24]
        if n == 3:
            return [16, 16, 16]
        return _even_widths(n)

    if all(family == "metric" for family in families):
        if n == 1:
            return [24]
        if n == 2:
            return [24, 24]
        if n == 3:
            return [16, 16, 16]
        if n == 4:
            return [12, 12, 12, 12]
        return _even_widths(n)

    if n == 1:
        return [24] if families[0] == "metric" else [GRID_COLUMNS]

    if n == 2 and set(families) == {"metric", "chart"}:
        return [16 if family == "metric" else 32 for family in families]

    if n == 2 and set(families) == {"metric", "table"}:
        return [16 if family == "metric" else 32 for family in families]

    if n == 2 and set(families) == {"markdown", "chart"}:
        return [16 if family == "markdown" else 32 for family in families]

    if n == 2 and set(families) == {"markdown", "table"}:
        return [16 if family == "markdown" else 32 for family in families]

    if n == 2:
        return [24, 24]

    if n == 3 and families.count("metric") == 2 and any(
        family in {"chart", "table"} for family in families
    ):
        return [24 if family in {"chart", "table"} else 12 for family in families]

    if n == 3:
        return [16, 16, 16]

    return _even_widths(n)


_DD_TYPE_FAMILY: dict[str, str] = {
    "query_value": "metric",
    "change": "metric",
    "slo": "metric",
    "check_status": "metric",
    "timeseries": "chart",
    "heatmap": "chart",
    "distribution": "chart",
    "scatter_plot": "chart",
    "geomap": "chart",
    "sunburst": "chart",
    "funnel": "chart",
    "toplist": "table",
    "table": "table",
    "list_stream": "table",
    "log_stream": "table",
    "treemap": "chart",
    "note": "markdown",
    "free_text": "markdown",
    "image": "markdown",
    "iframe": "markdown",
    "hostmap": "table",
}


def _panel_family(panel: dict[str, Any]) -> str:
    dd_type = panel.get("_dd_type", "")
    if dd_type in _DD_TYPE_FAMILY:
        return _DD_TYPE_FAMILY[dd_type]
    panel_type = _kibana_panel_type(panel)
    if panel_type == "markdown":
        return "markdown"
    if panel_type in ("metric", "gauge"):
        return "metric"
    if panel_type == "datatable":
        return "table"
    return "chart"


def _markdown_role(panel: dict[str, Any]) -> str:
    return str(panel.get("_markdown_role", ""))


_DD_TYPE_HEIGHT: dict[str, int] = {
    "query_value": 5,
    "change": 5,
    "slo": 5,
    "check_status": 5,
    "timeseries": 12,
    "heatmap": 12,
    "distribution": 12,
    "scatter_plot": 12,
    "geomap": 12,
    "sunburst": 12,
    "funnel": 12,
    "toplist": 15,
    "table": 15,
    "list_stream": 15,
    "log_stream": 15,
    "treemap": 12,
    "hostmap": 8,
}


def _preferred_panel_height(panel: dict[str, Any], width: int | None = None) -> int:
    panel_type = _kibana_panel_type(panel)
    if panel_type == "markdown":
        content = (panel.get("markdown") or {}).get("content") or ""
        role = _markdown_role(panel)
        estimated_lines = _estimate_markdown_lines(content, width or 24)
        if role == "placeholder":
            if estimated_lines > 10 and (width or 24) <= 16:
                return 8
            return 6
        if estimated_lines <= 4:
            return 6
        if estimated_lines <= 8:
            return 8
        if estimated_lines <= 13:
            return 10
        return 12
    dd_type = panel.get("_dd_type", "")
    if dd_type in _DD_TYPE_HEIGHT:
        return _DD_TYPE_HEIGHT[dd_type]
    return KIBANA_TYPE_HEIGHT.get(panel_type, KIBANA_DEFAULT_HEIGHT)


def _even_widths(n: int) -> list[int]:
    if n <= 0:
        return []
    base = GRID_COLUMNS // n
    widths = [base] * n
    for idx in range(GRID_COLUMNS - sum(widths)):
        widths[idx % n] += 1
    return widths


def _estimate_markdown_lines(content: str, width: int) -> int:
    chars_per_line = 96 if width >= 48 else 64 if width >= 32 else 46 if width >= 24 else 30
    lines = content.splitlines() or [content]
    estimated = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            estimated += 1
            continue
        estimated += max(1, math.ceil(len(stripped) / chars_per_line))
    return estimated


def _normalize_tile_sizes(panels: list[dict[str, Any]]) -> None:
    """Enforce per-type min/max sizes, matching the shared PANEL_SIZE_CONSTRAINTS table.

    Descends into sections so nested panels get the same treatment.
    """
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            inner = section.get("panels")
            if isinstance(inner, list):
                _normalize_tile_sizes(inner)

        size = panel.setdefault("size", {})
        position = panel.setdefault("position", {})
        chart_type = _kibana_panel_type(panel)

        constraints = PANEL_SIZE_CONSTRAINTS.get(chart_type)
        if constraints is not None:
            min_w, min_h, max_h = constraints
            w = int(size.get("w", 0) or 0)
            h = int(size.get("h", 0) or 0)
            if w > 0 and w < min_w:
                size["w"] = min_w
            if h > 0 and h < min_h:
                size["h"] = min_h
            if max_h is not None and h > max_h:
                size["h"] = max_h

        w = int(size.get("w", 8) or 8)
        max_x = max(0, GRID_COLUMNS - w)
        x = int(position.get("x", 0) or 0)
        if x > max_x:
            position["x"] = max_x


# ---------------------------------------------------------------------------
# Overlap resolution
# ---------------------------------------------------------------------------

def _resolve_overlaps(panels: list[dict[str, Any]]) -> None:
    """Push panels down to eliminate overlapping positions (iterate to convergence)."""
    for panel in panels:
        section = panel.get("section")
        if section and "panels" in section:
            _resolve_overlaps(section["panels"])

    for _pass in range(50):
        changed = False
        for i in range(len(panels)):
            p = panels[i]
            pos_i = p.get("position", {})
            sz_i = p.get("size", {})
            if not pos_i or not sz_i:
                continue
            x1, y1 = pos_i.get("x", 0), pos_i.get("y", 0)
            w1, h1 = sz_i.get("w", 8), sz_i.get("h", 6)

            for j in range(i + 1, len(panels)):
                q = panels[j]
                pos_j = q.get("position", {})
                sz_j = q.get("size", {})
                if not pos_j or not sz_j:
                    continue
                x2, y2 = pos_j.get("x", 0), pos_j.get("y", 0)
                w2, h2 = sz_j.get("w", 8), sz_j.get("h", 6)

                if x1 < x2 + w2 and x1 + w1 > x2 and y1 < y2 + h2 and y1 + h1 > y2:
                    pos_j["y"] = y1 + h1
                    changed = True
        if not changed:
            break


# ---------------------------------------------------------------------------
# Dimension / metric inference from ES|QL
# ---------------------------------------------------------------------------

def _infer_dimensions(result: TranslationResult) -> list[str]:
    """Infer dimension fields from the ES|QL query (group-by fields)."""
    query = result.esql_query or ""
    shape = extract_esql_shape(query)
    return list(shape.group_fields)


def _split_by_clause(text: str) -> list[str]:
    """Split a BY clause on commas, respecting parenthesized expressions."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _infer_metrics(result: TranslationResult) -> list[str]:
    """Infer metric fields from the ES|QL STATS clause."""
    query = result.esql_query or ""
    dims = _infer_dimensions(result)
    shape = extract_esql_shape(query)
    if shape.metric_fields:
        return list(shape.metric_fields)
    keep_fields = _infer_keep_fields(query)
    if keep_fields:
        metrics = [
            field for field in keep_fields
            if field not in dims and field != "@timestamp"
        ]
        if metrics:
            return metrics

    metrics: list[str] = []

    stats_matches = list(re.finditer(r"\bSTATS\b", query, re.IGNORECASE))
    if stats_matches:
        stats_idx = stats_matches[-1].start()
        after_stats = query[stats_idx + 6:]
        by_idx = after_stats.upper().find(" BY ")
        if by_idx >= 0:
            stats_clause = after_stats[:by_idx]
        else:
            stats_clause = after_stats.split("\n")[0]

        for part in _split_by_clause(stats_clause):
            part = part.strip()
            if "=" in part:
                alias = part.split("=")[0].strip()
                metrics.append(alias)

    return metrics or ["value"]


def _infer_keep_fields(query: str) -> list[str]:
    query = query or ""
    keep_matches = list(re.finditer(r"\|\s*KEEP\s+(.+?)(?=\s*\||$)", query, re.IGNORECASE | re.DOTALL))
    if not keep_matches:
        return []
    keep_clause = keep_matches[-1].group(1).replace("\n", " ").strip()
    return [
        part.strip()
        for part in _split_by_clause(keep_clause)
        if part.strip()
    ]


def _composite_y_column(query: str, dims: list[str], name: str = "y_group") -> tuple[str, str]:
    """Splice a composite Y column into a heatmap query.

    Builds ``| EVAL <name> = CONCAT(COALESCE(TO_STRING(d1), ""), " / ", …)`` from
    the grouping dimensions and inserts it just before the trailing ``KEEP`` (so
    the composite is a real output column), adding ``<name>`` to that ``KEEP``.
    When the query has no ``KEEP`` stage the ``EVAL`` is appended after the last
    ``STATS`` stage. Returns ``(new_query, column_name)``.
    """
    concat_args: list[str] = []
    for index, dim in enumerate(dims):
        if index:
            concat_args.append('" / "')
        concat_args.append(f'COALESCE(TO_STRING({dim}), "")')
    eval_stage = f"| EVAL {name} = CONCAT({', '.join(concat_args)})"

    stages = [line.strip() for line in query.splitlines() if line.strip()]
    keep_idx = None
    for index, stage in enumerate(stages):
        if stage.upper().startswith("| KEEP "):
            keep_idx = index
    if keep_idx is not None:
        keep_body = stages[keep_idx][len("| KEEP "):].strip()
        stages[keep_idx] = f"| KEEP {keep_body}, {name}"
        stages.insert(keep_idx, eval_stage)
    else:
        stats_indices = [
            index for index, stage in enumerate(stages)
            if stage.upper().startswith("| STATS ") or stage.upper().startswith("STATS ")
        ]
        insert_at = (stats_indices[-1] + 1) if stats_indices else len(stages)
        stages.insert(insert_at, eval_stage)
    return "\n".join(stages), name


def _dimension_config(field: str, data_type: str | None = None) -> dict[str, Any]:
    field = _strip_field_name(field)
    config: dict[str, Any] = {"field": field}
    label = _pretty_field_label(field)
    if label:
        config["label"] = label
    if data_type:
        config["data_type"] = data_type
    return config


def _metric_config(
    widget: NormalizedWidget,
    result: TranslationResult,
    field: str,
) -> dict[str, Any]:
    field = _strip_field_name(field)
    config: dict[str, Any] = {"field": field}
    label = _metric_label(widget, result, field)
    # On metric (query_value) panels the panel title is already displayed by
    # Kibana; setting primary.label to the same string triggers the
    # metric-redundant-label lint rule and causes compile to fail.
    if label and not (result.kibana_type == "metric" and label == (widget.title or "")):
        config["label"] = label
    return config


def _metric_label(
    widget: NormalizedWidget,
    result: TranslationResult,
    field: str,
) -> str:
    normalized = _strip_field_name(field)

    if normalized == "value":
        if widget.formulas:
            has_alias = any(f.alias for f in widget.formulas)
            if has_alias:
                label = _formula_output_label(widget, normalized)
                if label:
                    return label
            elif result.kibana_type == "metric" and widget.title:
                return widget.title
            else:
                label = _formula_output_label(widget, normalized)
                if label:
                    return label
        if len([q for q in widget.queries if q.metric_query]) == 1:
            query_label = _query_output_label(widget, normalized)
            if query_label:
                return query_label
        return widget.title or _pretty_field_label(normalized)

    label = _formula_output_label(widget, normalized)
    if label:
        return label

    label = _query_output_label(widget, normalized)
    if label:
        return label

    if _is_generic_metric_field(normalized):
        return widget.title or _pretty_field_label(normalized)

    return _pretty_field_label(normalized)


def _formula_output_label(widget: NormalizedWidget, field: str) -> str:
    for formula in widget.formulas:
        candidates = {
            _safe_output_name(formula.alias or ""),
            _safe_output_name(formula.raw or ""),
        }
        if field == "value" and len(widget.formulas) == 1:
            candidates.add("value")
        if field in {c for c in candidates if c}:
            raw = (formula.alias or formula.raw or "").strip()
            if raw:
                query_label = _query_output_label(widget, _safe_output_name(raw))
                if query_label:
                    return query_label
                return _pretty_formula_label(raw)
    return ""


def _query_output_label(widget: NormalizedWidget, field: str) -> str:
    metric_queries = [q for q in widget.queries if q.metric_query]
    for _idx, query in enumerate(metric_queries, start=1):
        candidates = {
            _safe_output_name(query.name),
        }
        if len(metric_queries) == 1:
            candidates.add("value")
        if field in {c for c in candidates if c}:
            metric_name = query.metric_query.metric if query.metric_query else ""
            if metric_name:
                return _pretty_metric_name(metric_name)
            if query.name:
                return _pretty_field_label(query.name)
    return ""


def _pretty_formula_label(raw: str) -> str:
    cleaned = raw.strip()
    if not cleaned:
        return ""
    if re.fullmatch(r"query\d+", cleaned, re.IGNORECASE):
        return cleaned.upper()
    cleaned = cleaned.replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _pretty_metric_name(metric_name: str) -> str:
    parts = metric_name.split(".")
    leaf = parts[-1]
    if leaf.isdigit() and len(parts) >= 2:
        leaf = f"{parts[-2]} {leaf}"
    return _pretty_field_label(leaf)


def _pretty_field_label(field: str) -> str:
    normalized = _strip_field_name(field)
    special = {
        "@timestamp": "Timestamp",
        "time_bucket": "Time",
        "host.name": "Host",
        "service.name": "Service",
        "log.level": "Level",
        "message": "Message",
    }
    if normalized in special:
        return special[normalized]
    leaf = normalized.split(".")[-1]
    leaf = leaf.replace("_", " ").strip()
    if not leaf:
        return normalized
    return leaf[:1].upper() + leaf[1:]


def _is_generic_metric_field(field: str) -> bool:
    return bool(re.fullmatch(r"(value|query\d+(?:_query\d+)*|formula_\d+|f_\d+)", field))


def _safe_output_name(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", raw or "").strip("_").lower()
    if not cleaned:
        return ""
    if cleaned[0].isdigit():
        cleaned = f"f_{cleaned}"
    return cleaned


def _strip_field_name(field: str) -> str:
    return field.strip().strip("`")
