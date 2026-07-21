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
from pathlib import Path
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

from .display import _clean_template_vars, enrich_panel_display
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


def _clean_title_string(title: str) -> str:
    """Strip Datadog ``$template`` placeholders from a panel title."""
    return _clean_template_vars(title)


def _panel_title(
    widget: NormalizedWidget,
    result: TranslationResult | None = None,
    *,
    default: str = "Untitled",
) -> str:
    """Human panel title for Kibana — drop Datadog ``$template`` noise.

    Datadog often appends ``over $host`` (and friends). Kibana already has
    matching Options List controls, so leaving the literal ``$host`` in the
    chrome reads as a migration bug. Empty log streams become ``Logs``.
    """
    raw = ""
    if result is not None:
        raw = str(result.title or "").strip()
    if not raw:
        raw = str(widget.title or "").strip()
    if not raw:
        if widget.widget_type in ("log_stream", "list_stream"):
            return "Log stream"
        return default
    return _clean_title_string(raw) or default


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
    "_free_anchor_y",
    "_free_band_left",
    "_free_band_right",
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
    # Style-guide row fill can widen a lower stripe under taller tiles; push any
    # remaining collisions down without touching x. Runs after ``_dd_*`` strip, so
    # do not gate on ``_is_wide_free_board``.
    leaf = [
        p
        for p in doc["dashboards"][0]["panels"]
        if "section" not in p
        and isinstance(p.get("position"), dict)
        and isinstance(p.get("size"), dict)
    ]
    _repair_free_board_vertical_overlaps(leaf)
    return doc


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
    independent rendering of the source widgets.
    """
    _yaml_string, _native, _stats, _dashboard_ir = generate_dashboard_artifacts(
        dashboard,
        results,
        data_view,
        metrics_dataset_filter=metrics_dataset_filter,
        logs_dataset_filter=logs_dataset_filter,
        logs_index=logs_index,
        field_map=field_map,
    )
    return _yaml_string


def generate_dashboard_artifacts(
    dashboard: NormalizedDashboard,
    results: list[TranslationResult],
    data_view: str = "metrics-*",
    *,
    metrics_dataset_filter: str = "",
    logs_dataset_filter: str = "",
    logs_index: str = "logs-*",
    field_map: FieldMapProfile | None = None,
) -> tuple[str, NativeDashboard, dict[str, Any], DashboardIR]:
    """Generate YAML, NativeDashboard, and the semantic DashboardIR.

    IR-first Phase 2 (mirrors Grafana's ``translate_dashboard``): the
    per-widget translators still assemble a kb-dashboard-core dict (the
    expensive, well-tested part of the pipeline), then that dict is
    converted to a :class:`DashboardIR` *before* the native mapping and
    the YAML dump. From that point on the dict is no longer the source of
    truth -- both the typed Dashboards API payload
    (``native_dashboard_from_ir``) and the on-disk YAML
    (``DashboardIR.to_yaml_dict``) are derived from the same IR, so they
    cannot drift from each other.

    Returns ``(yaml_string, native_dashboard, native_stats, dashboard_ir)``
    where ``native_stats`` has ``mapped``/``unmapped``/``sections``/
    ``controls``/``reasons`` (see :class:`NativeMappingCounts`).
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
    exported_doc = {"dashboards": [dashboard_ir.to_yaml_dict()]}
    yaml_string = yaml.dump(exported_doc, default_flow_style=False, sort_keys=False, allow_unicode=True)
    native_dashboard, counts = native_dashboard_from_ir(dashboard_ir)
    counts_dict, reasons = counts.as_dicts()
    stats: dict[str, Any] = {**counts_dict, "reasons": reasons}
    return yaml_string, native_dashboard, stats, dashboard_ir


def _iter_leaf_panels(panels: list[dict[str, Any]]):
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            yield from _iter_leaf_panels(section.get("panels") or [])
        else:
            yield panel


def _title_from_markdown_content(content: str, *, max_len: int = 72) -> str:
    """Derive a short panel title from note/free_text markdown body.

    Datadog ``note`` widgets almost never set ``title`` — the body *is* the
    tile. Prefer the first meaningful line over a synthetic ``Datadog note
    <id>`` chrome title.
    """
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip common markdown heading/emphasis markers and images/links noise.
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"[*_`]+", "", line)
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"\s+", " ", line).strip(" -:|")
        if line:
            if len(line) > max_len:
                return line[: max_len - 1].rstrip() + "…"
            return line
    return ""


def _fallback_panel_title(panel: dict[str, Any], result: TranslationResult | None) -> str:
    markdown = panel.get("markdown")
    if isinstance(markdown, dict):
        derived = _title_from_markdown_content(str(markdown.get("content") or ""))
        if derived:
            return derived
    image = panel.get("image")
    if isinstance(image, dict):
        url = str(image.get("from_url") or "")
        if url:
            return _datadog_static_asset_label(url)
    source_type = str(
        panel.get("_dd_display_type")
        or panel.get("_dd_type")
        or (result.dd_widget_type if result else "")
        or "widget"
    ).replace("_", " ")
    widget_id = str(panel.get("_dd_widget_id") or (result.widget_id if result else "") or "").strip()
    suffix = f" {widget_id}" if widget_id else ""
    return f"Datadog {source_type}{suffix}".strip()


def _is_untitled_datadog_text_panel(panel: dict[str, Any], result: TranslationResult | None) -> bool:
    """True when the source Datadog tile had no title (typical for notes/images)."""
    dd_type = str(
        panel.get("_dd_type")
        or panel.get("_dd_display_type")
        or (result.dd_widget_type if result else "")
        or ""
    )
    if dd_type in {"note", "free_text", "image"}:
        return True
    if isinstance(panel.get("image"), dict):
        return True
    return panel.get("_markdown_role") == "text"


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

    Untitled Datadog notes/free_text keep a content-derived (or last-resort
    synthetic) title for audit keys, but set ``hide_title`` so Kibana matches
    Datadog's chrome-less note tiles.
    """
    used: set[str] = set()
    for ordinal, panel in enumerate(_iter_leaf_panels(panels), start=1):
        widget_id = str(panel.get("_dd_widget_id") or "")
        result = result_map.get(widget_id)
        base = str((result.title if result else "") or panel.get("title") or "").strip()
        if base:
            base = _clean_title_string(base) or base
        source_blank = not base
        if not base:
            # Prefer type-aware defaults (Logs) over generic Datadog fallbacks.
            dd_type = str(panel.get("_dd_type") or "")
            if dd_type in {"log_stream", "list_stream"}:
                base = "Log stream"
            else:
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
        if source_blank and _is_untitled_datadog_text_panel(panel, result):
            panel["hide_title"] = True
        if result is not None:
            result.title = title


def _strip_datadog_private_keys(panels: list[dict[str, Any]]) -> None:
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            _strip_datadog_private_keys(section.get("panels") or [])
        for key in _DATADOG_PRIVATE_PANEL_KEYS:
            panel.pop(key, None)


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
        control: dict[str, Any] = {
            "type": "options",
            "label": tv.name,
            "data_view": control_data_view,
            "field": es_field,
            "multiple": len(tv.defaults) > 1 or tv.default == "*",
        }
        preselected = _template_var_preselected(tv)
        if preselected:
            control["preselected"] = preselected
        controls.append(control)
    return controls


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
        "title": _panel_title(widget, result),
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
            _disambiguate_metric_labels(esql_block["metrics"], widget)
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
                _disambiguate_metric_labels(esql_block["metrics"], widget)
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
            _disambiguate_metric_labels(esql_block["metrics"], widget)
        breakdown = [d for d in dims if "time" not in d.lower() and "bucket" not in d.lower()]
        if not breakdown:
            breakdown = dims[:1] if dims else ["value"]
        esql_block["breakdowns"] = [_dimension_config(b) for b in breakdown]
        esql_block["legend"] = {"visible": "auto", "truncate_labels": 0}

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
        esql_block["legend"] = {"visible": "auto", "truncate_labels": 0}

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
        "title": _panel_title(widget, result),
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
        "Datadog **check_status** has no direct Lens equivalent. "
        "Create an Elastic [Synthetics monitor](https://www.elastic.co/guide/en/observability/current/monitor-uptime-synthetics.html) "
        "for the same endpoint, then visualize its status here."
    ),
    "manage_status": (
        "Datadog **manage_status** summarizes monitors. "
        "Recreate the checks as Kibana [Alerts](https://www.elastic.co/guide/en/kibana/current/create-and-manage-rules.html) "
        "rules and link to them from this panel."
    ),
    "hostmap": (
        "Datadog **hostmap** is a topology/heatmap of hosts. "
        "Use Elastic [Infrastructure inventory](https://www.elastic.co/guide/en/observability/current/view-infrastructure-metrics.html) "
        "or [Maps](https://www.elastic.co/guide/en/kibana/current/maps.html) for a similar view."
    ),
}

_DATADOG_STATIC_LOGO_LABELS: dict[str, str] = {
    "haproxy": "HAProxy",
    "apache": "Apache",
    "nginx": "NGINX",
    "redis": "Redis",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mongo": "MongoDB",
    "mongodb": "MongoDB",
    "kafka": "Kafka",
    "consul": "Consul",
    "vault": "Vault",
    "elastic": "Elasticsearch",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "coredns": "CoreDNS",
    "rabbitmq": "RabbitMQ",
    "nginx_ingress_controller": "NGINX",
    "nginx-ingress-controller": "NGINX",
}


def _datadog_static_asset_label(url: str) -> str:
    """Human label for a Datadog logo URL (static path or CDN)."""
    stem = Path(url.split("?", 1)[0]).stem.lower()
    stem = re.sub(r"(_large|_small|_icon|_logo|_color|_white)$", "", stem)
    stem_key = stem.replace("-", "_")
    if stem_key in _DATADOG_STATIC_LOGO_LABELS:
        return _DATADOG_STATIC_LOGO_LABELS[stem_key]
    # nginx-ingress-controller_small → try progressive prefixes
    parts = stem_key.split("_")
    for i in range(len(parts), 0, -1):
        key = "_".join(parts[:i])
        if key in _DATADOG_STATIC_LOGO_LABELS:
            return _DATADOG_STATIC_LOGO_LABELS[key]
    key = parts[0] if parts else ""
    if key in _DATADOG_STATIC_LOGO_LABELS:
        return _DATADOG_STATIC_LOGO_LABELS[key]
    pretty = stem.replace("_", " ").replace("-", " ").strip()
    return pretty.title() if pretty else "Logo"


def _placeholder_markdown_content(
    widget: NormalizedWidget,
    result: TranslationResult,
) -> str:
    """Compact operator-facing body for unsupported / manual widgets.

    Keep the rebuild hint on the tile; omit source-query dumps that balloon
    height into empty slabs. Full query text stays on ``TranslationResult``.
    """
    kind = str(widget.widget_type or "widget").replace("_", " ")
    hint = _STATUS_PLACEHOLDER_HINTS.get(widget.widget_type)
    lines = [
        f"_Needs follow-up_ — `{widget.widget_type or kind}`",
        "",
    ]
    if hint:
        lines.append(hint)
    else:
        lines.append(
            f"This **{kind}** widget has no automated Kibana translation yet. "
            "Leave as a reminder or rebuild with a Lens panel."
        )
    return "\n".join(lines)


def _is_section_header_markdown(content: str) -> bool:
    """True for short Datadog free_text/note labels used as section titles.

    ActiveMQ ``Broker`` / ``Queue`` / ``Topics`` are tall on the free canvas
    but are labels, not essays - they must stay 1-2 Kibana rows high.
    """
    text = str(content or "").strip()
    if not text or "```" in text or "http://" in text.lower() or "https://" in text.lower():
        return False
    # Brand / logo tiles use ``## Label``; keep them as normal notes so they
    # stay in their source column instead of jumping to the next band.
    if text.lstrip().startswith("#"):
        return False
    plain = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE).strip()
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    if not lines or len(lines) > 2:
        return False
    joined = " ".join(lines)
    # Prose notes often end with a period; section labels do not.
    if joined.endswith((".", "!", "?")):
        return False
    words = joined.split()
    if len(words) > 4 or len(joined) > 32:
        return False
    return True


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
        content = _placeholder_markdown_content(widget, result)

    title = str(result.title or widget.title or "").strip()
    hide_title = False
    # Datadog notes/free_text are usually untitled; the body is the tile.
    if widget.widget_type in ("note", "free_text") and not title:
        title = _title_from_markdown_content(content)
        hide_title = True
    elif widget.widget_type == "image" and not title:
        # Static-logo placeholders are body-led brand tiles.
        title = _title_from_markdown_content(content) or "Logo"
        hide_title = True
    elif title:
        title = _clean_title_string(title) or title

    panel: dict[str, Any] = {
        "title": title,
        "size": {"w": w, "h": h},
        "position": {"x": x, "y": y},
        "markdown": {"content": content},
    }
    if hide_title:
        panel["hide_title"] = True
    # Static integration logos behave like notes (chrome-less), not red warning tiles.
    if widget.widget_type == "image":
        panel["_markdown_role"] = "text"
    elif is_text_widget and _is_section_header_markdown(content):
        panel["_markdown_role"] = "header"
    else:
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

    title = _panel_title(widget, result, default="")
    hide_title = False
    if not title:
        # Untitled Datadog logos: derive a brand label for audit keys, hide chrome.
        title = _datadog_static_asset_label(url) or "Logo"
        hide_title = True
    panel: dict[str, Any] = {
        "title": title,
        "size": {"w": w, "h": h},
        "position": {"x": x, "y": y},
        "image": image_config,
    }
    if hide_title:
        panel["hide_title"] = True
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
        url = str(defn.get("url") or "").strip()
        if not url:
            return ""
        if url.startswith("/static/") or not url.startswith(("http://", "https://")):
            label = _datadog_static_asset_label(url)
            # Brand-only tile — Datadog shows the logo with no caption chrome.
            return f"## {label}"
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

    # Wide Datadog "free" canvases (x/width often ≫ 12) must keep a single global
    # scale so columns stay aligned across rows. Per-row "fill to 48" stretches
    # each stripe independently and makes Apache/HAProxy-style boards look
    # scrambled.
    if _is_wide_free_board(panels):
        _apply_free_board_layout(panels)
        # Free boards: only height floors/caps. Type min-widths (8/12) blow up
        # dense HAProxy/Apache columns and force horizontal reshuffles.
        _normalize_free_board_tile_sizes(panels)
        _pack_free_board_vertically([p for p in panels if "section" not in p])
        _repair_free_board_vertical_overlaps([p for p in panels if "section" not in p])
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


def _board_max_extent(panels: list[dict[str, Any]]) -> int:
    """Return max(x + width) across panels in Datadog layout units."""
    extent = 0
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            continue
        x = int(panel.get("_dd_x", 0) or 0)
        w = int(panel.get("_dd_w", 1) or 1)
        extent = max(extent, x + w)
    return extent


def _is_wide_free_board(panels: list[dict[str, Any]]) -> bool:
    """Datadog free layouts use a fine canvas (often 100-200 units wide).

    Ordered dashboards and group children live on a ~12-column board; those keep
    the row/heuristic path. Anything wider is treated as a free board.
    """
    return _board_max_extent(panels) > 12


def _apply_free_board_layout(panels: list[dict[str, Any]]) -> None:
    """Map a Datadog free canvas onto Kibana via readable column bands.

    Hybrid (spec C): detect left→right source columns, give each a usable
    share of 48 cols (chart/metric floors), never merge/reorder bands. Dense
    KPI siblings inside one band share that band via proportional sub-slots.
    """
    leaf = [p for p in panels if "section" not in p]
    if not leaf:
        return

    board_w = max(_board_max_extent(leaf), 1)
    band_starts = _cluster_free_board_band_starts(leaf)
    band_widths = _assign_free_board_band_widths(leaf, band_starts, board_w)
    band_x = []
    cursor = 0
    for w in band_widths:
        band_x.append(cursor)
        cursor += w

    scale_y = GRID_COLUMNS / board_w

    for panel in leaf:
        dd_x = int(panel.get("_dd_x", 0) or 0)
        dd_y = int(panel.get("_dd_y", 0) or 0)
        dd_w = int(panel.get("_dd_w", 1) or 1)
        dd_h = int(panel.get("_dd_h", 1) or 1)
        left_i, right_i = _free_board_covered_band_range(
            band_starts, board_w, dd_x, dd_w
        )
        # Section headers that lived in a dropped gutter start should sit on the
        # next content band to their right (Topics → topic tables), not the
        # previous chart column.
        if _is_free_board_section_header_panel(panel):
            for i, start in enumerate(band_starts):
                if start > dd_x:
                    left_i = right_i = i
                    break
        x = band_x[left_i]
        w = sum(band_widths[left_i : right_i + 1])
        h = max(1, round(dd_h * scale_y))
        if _kibana_panel_type(panel) == "markdown":
            # Free-canvas notes/labels often have huge Datadog heights (vertical
            # section titles). Size from content role, not the decorative dd_h.
            role = _markdown_role(panel)
            if role == "header":
                h = 2
            elif role == "placeholder":
                dd_type = str(panel.get("_dd_type") or "")
                if dd_type in {"check_status", "manage_status"}:
                    # Status chips — never grow into essay slabs.
                    h = 3
                elif dd_type in {"log_stream", "list_stream"}:
                    h = max(8, min(round(dd_h * scale_y), 28))
                else:
                    # hostmap and other large unsupported tiles.
                    h = max(4, min(round(dd_h * scale_y), 10))
            else:
                h = _preferred_panel_height(panel, w)
        y = max(0, round(dd_y * scale_y))
        panel["size"] = {"w": w, "h": h}
        panel["position"] = {"x": x, "y": y}
        panel["_free_anchor_y"] = y
        panel["_free_band_left"] = left_i
        panel["_free_band_right"] = right_i

    _apply_free_board_band_subslots(leaf, band_starts, band_widths, band_x, board_w)
    # Sub-slots may have narrowed headers; stretch section titles back to the
    # full content band so they don't leave a 4-col gutter label.
    for panel in leaf:
        if not _is_free_board_section_header_panel(panel):
            continue
        bi = int(panel.get("_free_band_left", 0) or 0)
        panel["position"]["x"] = band_x[bi]
        panel["size"]["w"] = band_widths[bi]
    _repair_free_board_horizontal_overlaps(leaf)
    _pack_free_board_vertically(leaf)
    _repair_free_board_vertical_overlaps(leaf)

    for panel in leaf:
        panel.pop("_free_band_left", None)
        panel.pop("_free_band_right", None)


def _cluster_free_board_band_starts(panels: list[dict[str, Any]]) -> list[int]:
    """Cluster Datadog ``_dd_x`` starts into ordered column band origins.

    Narrow tiles that sit just to the right of a wider column (HAProxy KPIs at
    x=43 beside the x=25 overview column) become sub-slots of the previous
    band instead of new top-level columns — that keeps band count low enough
    for readable chart/metric floors on a 48-col grid.

    Near-duplicate starts (nginx-ingress charts at x=37 and x=41) always fold
    into one band even when both are wide: they are staggered placements of
    the same visual column, not distinct L→R story columns.
    """
    starts = sorted({int(p.get("_dd_x", 0) or 0) for p in panels})
    if not starts:
        return [0]
    widths = [max(1, int(p.get("_dd_w", 1) or 1)) for p in panels]
    widths.sort()
    median_w = widths[len(widths) // 2]
    gap = max(12, median_w // 2 + 1)
    near_merge = max(8, median_w // 4)
    narrow_cap = max(20, median_w // 2)

    max_w_at: dict[int, int] = {}
    for panel in panels:
        x = int(panel.get("_dd_x", 0) or 0)
        w = max(1, int(panel.get("_dd_w", 1) or 1))
        max_w_at[x] = max(max_w_at.get(x, 0), w)

    bands: list[int] = [starts[0]]
    for x in starts[1:]:
        prev = bands[-1]
        delta = x - prev
        # Same visual column with a slightly staggered start.
        if delta <= near_merge:
            continue
        # Fold narrow sibling starts into the previous band as sub-slots.
        if delta <= gap and max_w_at.get(x, 1) <= narrow_cap:
            continue
        bands.append(x)

    # Section-title free_text (ActiveMQ Topics / Broker) must not become its own
    # skinny gutter column — drop those starts; headers reattach to the next
    # content band during placement.
    content_bands: list[int] = []
    for start in bands:
        at_start = [
            p for p in panels if int(p.get("_dd_x", 0) or 0) == start
        ]
        if at_start and all(_is_free_board_section_header_panel(p) for p in at_start):
            continue
        content_bands.append(start)
    return content_bands or bands


def _is_free_board_section_header_panel(panel: dict[str, Any]) -> bool:
    return (
        _kibana_panel_type(panel) == "markdown"
        and _markdown_role(panel) == "header"
    )


def _free_board_family_min_width(panel: dict[str, Any]) -> int:
    family = _panel_family(panel)
    if family == "metric":
        return 6
    if family in {"chart", "table"}:
        return 8
    # Long notes need a readable column; short headers can stay compact.
    if family == "markdown":
        role = _markdown_role(panel)
        if role == "header":
            return 4
        if role == "placeholder":
            return 4
        content = ""
        md = panel.get("markdown")
        if isinstance(md, dict):
            content = str(md.get("content") or "")
        if _estimate_markdown_lines(content, 12) >= 6:
            return 8
        return 4
    return 4


def _free_board_subslot_min_width(panel: dict[str, Any]) -> int:
    """Readable floors when splitting one band among siblings.

    Prefer stacking over postage-stamp KPIs: metric/chart floors match the
    primary band mins so a dense overview band stacks rather than emitting
    4-col tiles.
    """
    family = _panel_family(panel)
    if family == "metric":
        return 6
    if family in {"chart", "table"}:
        return 8
    if family == "markdown":
        # Placeholders / short headers share KPI rows; don't block the split.
        if _markdown_role(panel) in {"placeholder", "header"}:
            return 4
        return min(6, _free_board_family_min_width(panel))
    return _free_board_family_min_width(panel)


def _free_board_band_dd_range(
    band_starts: list[int], board_w: int, index: int
) -> tuple[int, int]:
    start = band_starts[index]
    end = band_starts[index + 1] if index + 1 < len(band_starts) else board_w
    return start, max(start + 1, end)


def _free_board_covered_band_range(
    band_starts: list[int], board_w: int, dd_x: int, dd_w: int
) -> tuple[int, int]:
    """Inclusive band indices overlapped by ``[dd_x, dd_x+dd_w)``."""
    lo = dd_x
    hi = dd_x + max(1, dd_w)
    indices: list[int] = []
    for i in range(len(band_starts)):
        b0, b1 = _free_board_band_dd_range(band_starts, board_w, i)
        if b0 < hi and b1 > lo:
            indices.append(i)
    if not indices:
        left = 0
        for i, start in enumerate(band_starts):
            if start <= dd_x:
                left = i
            else:
                break
        return left, left
    return indices[0], indices[-1]


def _assign_free_board_band_widths(
    panels: list[dict[str, Any]],
    band_starts: list[int],
    board_w: int,
) -> list[int]:
    """Allocate 48 columns across bands with readable floors when possible."""
    n = len(band_starts)
    weights: list[int] = []
    mins: list[int] = []
    for i in range(n):
        b0, b1 = _free_board_band_dd_range(band_starts, board_w, i)
        weights.append(max(1, b1 - b0))
        min_w = 4
        for panel in panels:
            dd_x = int(panel.get("_dd_x", 0) or 0)
            left, _right = _free_board_covered_band_range(
                band_starts, board_w, dd_x, int(panel.get("_dd_w", 1) or 1)
            )
            # Band minimum driven by panels that live primarily here.
            if left == i:
                min_w = max(min_w, _free_board_family_min_width(panel))
        mins.append(min_w)

    if sum(mins) >= GRID_COLUMNS:
        scale = GRID_COLUMNS / max(sum(mins), 1)
        widths = [max(1, int(m * scale)) for m in mins]
    else:
        widths = list(mins)
        remaining = GRID_COLUMNS - sum(widths)
        weight_sum = sum(weights) or 1
        extras = [int(remaining * w / weight_sum) for w in weights]
        for i, extra in enumerate(extras):
            widths[i] += extra
        # Distribute rounding leftover to heaviest bands.
        while sum(widths) < GRID_COLUMNS:
            i = max(range(n), key=lambda j: weights[j])
            widths[i] += 1
        while sum(widths) > GRID_COLUMNS:
            i = max(range(n), key=lambda j: widths[j] - mins[j])
            if widths[i] <= 1:
                break
            widths[i] -= 1

    # Pull bands up to mins by stealing slack from wider bands.
    for _ in range(n * 3):
        short = [i for i in range(n) if widths[i] < mins[i]]
        if not short:
            break
        donors = sorted(
            (j for j in range(n) if widths[j] > mins[j]),
            key=lambda j: widths[j] - mins[j],
            reverse=True,
        )
        if not donors:
            break
        progressed = False
        for i in short:
            need = mins[i] - widths[i]
            for j in donors:
                take = min(need, widths[j] - mins[j])
                if take <= 0:
                    continue
                widths[j] -= take
                widths[i] += take
                need -= take
                progressed = True
                if need <= 0:
                    break
        if not progressed:
            break

    # Final sum repair without collapsing bands.
    while sum(widths) > GRID_COLUMNS:
        i = max(range(n), key=lambda j: widths[j])
        if widths[i] <= 1:
            break
        widths[i] -= 1
    while sum(widths) < GRID_COLUMNS:
        i = max(range(n), key=lambda j: weights[j])
        widths[i] += 1
    return widths


def _apply_free_board_band_subslots(
    panels: list[dict[str, Any]],
    band_starts: list[int],
    band_widths: list[int],
    band_x: list[int],
    board_w: int,
) -> None:
    """Split a single band among y-overlapping siblings with different ``_dd_x``.

    Multi-band spanning panels are left alone so wide notes/charts keep their
    column span.
    """
    # Only single-band panels participate.
    singles = [
        p
        for p in panels
        if int(p.get("_free_band_left", 0)) == int(p.get("_free_band_right", 0))
    ]
    by_band: dict[int, list[dict[str, Any]]] = {}
    for panel in singles:
        by_band.setdefault(int(panel["_free_band_left"]), []).append(panel)

    for band_i, members in by_band.items():
        if len(members) < 2:
            continue
        # Group by nearby Datadog y — not scaled-position overlap. Inflated
        # placeholder heights (check_status) otherwise glue several source rows
        # into one sub-slot group and the split fails the soft mins.
        ordered = sorted(
            members,
            key=lambda p: (
                int(p.get("_dd_y", 0) or 0),
                int(p.get("_dd_x", 0) or 0),
            ),
        )
        groups: list[list[dict[str, Any]]] = []
        for panel in ordered:
            dy = int(panel.get("_dd_y", 0) or 0)
            placed = False
            for group in groups:
                group_min = min(int(o.get("_dd_y", 0) or 0) for o in group)
                group_max = max(int(o.get("_dd_y", 0) or 0) for o in group)
                if dy - group_max <= 5 and dy - group_min <= 10:
                    group.append(panel)
                    placed = True
                    break
            if not placed:
                groups.append([panel])

        bx = band_x[band_i]
        bw = band_widths[band_i]
        for group in groups:
            # Distinct source x → sub-slots; identical x stays full band (stack).
            xs = sorted({int(p.get("_dd_x", 0) or 0) for p in group})
            if len(xs) < 2:
                continue
            weights = []
            for x in xs:
                w = max(
                    (
                        int(p.get("_dd_w", 1) or 1)
                        for p in group
                        if int(p.get("_dd_x", 0) or 0) == x
                    ),
                    default=1,
                )
                weights.append(max(1, w))
            slot_ws = _even_split_by_weight(bw, weights)
            # If a horizontal split would violate readable mins, keep full-band
            # width and let vertical packing stack the siblings instead. Soften
            # floors slightly for sub-slots so a 9-col overview band can still
            # host two KPI tiles side-by-side (HAProxy Instances | Memory).
            x_to_slot = {x: i for i, x in enumerate(xs)}
            if any(
                slot_ws[x_to_slot[int(p.get("_dd_x", 0) or 0)]]
                < _free_board_subslot_min_width(p)
                for p in group
            ):
                continue
            slot_xs = []
            cursor = bx
            for sw in slot_ws:
                slot_xs.append(cursor)
                cursor += sw
            for panel in group:
                slot = x_to_slot[int(panel.get("_dd_x", 0) or 0)]
                panel["position"]["x"] = slot_xs[slot]
                panel["size"]["w"] = slot_ws[slot]


def _even_split_by_weight(total: int, weights: list[int]) -> list[int]:
    """Split ``total`` columns across weights; each slot ≥ 1 when possible."""
    n = len(weights)
    if n == 0:
        return []
    if total <= n:
        parts = [1] * total + [0] * (n - total)
        return parts[:n]
    wsum = sum(weights) or n
    parts = [max(1, int(total * w / wsum)) for w in weights]
    while sum(parts) > total:
        i = max(range(n), key=lambda j: parts[j])
        if parts[i] <= 1:
            break
        parts[i] -= 1
    while sum(parts) < total:
        i = max(range(n), key=lambda j: weights[j])
        parts[i] += 1
    return parts


def _repair_free_board_horizontal_overlaps(panels: list[dict[str, Any]]) -> None:
    """Shrink widths in place when scaled tiles overlap — never move ``x``.

    Moving x to "make room" destroys free-board column alignment (HAProxy).
    """
    for panel in panels:
        size = panel["size"]
        pos = panel["position"]
        if pos["x"] + size["w"] > GRID_COLUMNS:
            size["w"] = max(1, GRID_COLUMNS - pos["x"])

    ordered = sorted(panels, key=lambda p: (p["position"]["x"], p["position"]["y"]))
    for i, panel in enumerate(ordered):
        if _is_free_board_section_header_panel(panel):
            # Section titles intentionally span the content band above sub-slots.
            continue
        x = int(panel["position"]["x"])
        y = int(panel["position"]["y"])
        w = int(panel["size"]["w"])
        h = int(panel["size"]["h"])
        for other in ordered[i + 1 :]:
            ox = int(other["position"]["x"])
            oy = int(other["position"]["y"])
            ow = int(other["size"]["w"])
            oh = int(other["size"]["h"])
            if oy >= y + h or y >= oy + oh:
                continue
            # Same-column stacks (ox == x) are vertical neighbors — leave width alone.
            if ox <= x:
                continue
            if x < ox + ow and x + w > ox:
                # Keep left panel's x; trim its width so the right column starts cleanly.
                panel["size"]["w"] = max(1, ox - x)
                w = int(panel["size"]["w"])


def _normalize_free_board_tile_sizes(panels: list[dict[str, Any]]) -> None:
    """Height mins/max only — preserve free-board column widths."""
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            inner = section.get("panels")
            if isinstance(inner, list):
                _normalize_free_board_tile_sizes(inner)
                continue

        size = panel.setdefault("size", {})
        position = panel.setdefault("position", {})
        chart_type = _kibana_panel_type(panel)
        constraints = PANEL_SIZE_CONSTRAINTS.get(chart_type)
        h = int(size.get("h", 0) or 0)
        if constraints is not None:
            _min_w, min_h, max_h = constraints
            if h > 0 and h < min_h:
                size["h"] = min_h
            if max_h is not None and h > max_h:
                size["h"] = max_h
        w = int(size.get("w", 8) or 8)
        max_x = max(0, GRID_COLUMNS - w)
        x = int(position.get("x", 0) or 0)
        if x > max_x:
            position["x"] = max_x


def _pack_free_board_vertically(panels: list[dict[str, Any]]) -> None:
    """Place panels top-to-bottom without breaking left-to-right columns.

    Chart/metric/table panels that share a Datadog source row keep a common
    baseline ``y`` across columns so HAProxy sessions sit beside Frontend/
    Backend denials. Markdown-only columns (Apache sidebar notes) pack against
    their own column only — they must not inherit the mid-board chart stack's
    gutters. Panels that share a column inside that row are still stacked by
    ``_dd_y`` so KPI sub-slot failures don't overlap.
    """
    if not panels:
        return
    rows = _cluster_free_board_source_rows(panels)
    placed: list[dict[str, Any]] = []
    for row in rows:
        columns = _group_panels_by_horizontal_overlap(row)
        col_bases: list[int] = []
        note_cols: list[bool] = []
        for column in columns:
            # Pack by rendered Kibana type: check_status/hostmap placeholders are
            # markdown tiles even though ``_dd_type`` still says metric/chart.
            is_note = all(_kibana_panel_type(p) == "markdown" for p in column)
            base_y = 0
            for panel in column:
                # Sidebar notes/placeholders: ignore scaled source anchors so a
                # late note is not held down to mid-board chart rows.
                # Charts/metrics still honor anchors for cross-column baselines.
                if not is_note:
                    anchor = int(panel.get("_free_anchor_y", panel["position"]["y"]))
                    base_y = max(base_y, anchor)
                x = int(panel["position"]["x"])
                w = int(panel["size"]["w"])
                for other in placed:
                    ox = int(other["position"]["x"])
                    oy = int(other["position"]["y"])
                    ow = int(other["size"]["w"])
                    oh = int(other["size"]["h"])
                    if x < ox + ow and x + w > ox:
                        base_y = max(base_y, oy + oh)
            col_bases.append(base_y)
            note_cols.append(is_note)

        content_bases = [b for b, is_note in zip(col_bases, note_cols) if not is_note]
        shared = max(content_bases) if content_bases else None

        for column, own_base, is_note in zip(columns, col_bases, note_cols):
            y = own_base if is_note or shared is None else shared
            for panel in sorted(
                column,
                key=lambda p: (
                    int(p.get("_dd_y", 0) or 0),
                    int(p.get("_dd_x", 0) or 0),
                ),
            ):
                panel["position"]["y"] = y
                y += int(panel["size"]["h"])
                placed.append(panel)


def _repair_free_board_vertical_overlaps(panels: list[dict[str, Any]]) -> None:
    """Push panels down when they still overlap after row packing.

    Wide spanning tiles (Istio GC toplist) can cover multiple bands and collide
    with a later source row whose band x-range intersects that span. Never move
    ``x`` — only advance ``y``.
    """
    if len(panels) < 2:
        return
    # Iterate until stable; each pass only moves panels downward.
    for _ in range(len(panels) + 2):
        moved = False
        ordered = sorted(
            panels,
            key=lambda p: (int(p["position"]["y"]), int(p["position"]["x"])),
        )
        for i, panel in enumerate(ordered):
            x = int(panel["position"]["x"])
            y = int(panel["position"]["y"])
            w = int(panel["size"]["w"])
            h = int(panel["size"]["h"])
            new_y = y
            for other in ordered[:i]:
                ox = int(other["position"]["x"])
                oy = int(other["position"]["y"])
                ow = int(other["size"]["w"])
                oh = int(other["size"]["h"])
                if x < ox + ow and x + w > ox and y < oy + oh and oy < y + h:
                    new_y = max(new_y, oy + oh)
            if new_y != y:
                panel["position"]["y"] = new_y
                moved = True
        if not moved:
            break


def _group_panels_by_horizontal_overlap(
    panels: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Cluster panels that share horizontal space (same column stack)."""
    ordered = sorted(panels, key=lambda p: int(p["position"]["x"]))
    groups: list[list[dict[str, Any]]] = []
    for panel in ordered:
        x = int(panel["position"]["x"])
        w = int(panel["size"]["w"])
        placed = False
        for group in groups:
            if any(
                x < int(o["position"]["x"]) + int(o["size"]["w"])
                and int(o["position"]["x"]) < x + w
                for o in group
            ):
                group.append(panel)
                placed = True
                break
        if not placed:
            groups.append([panel])
    return groups


def _cluster_free_board_source_rows(
    panels: list[dict[str, Any]],
    *,
    gap: int = 5,
    max_span: int = 10,
) -> list[list[dict[str, Any]]]:
    """Group panels whose Datadog ``_dd_y`` values form one visual row.

    Join when the candidate is within ``gap`` of the current row's latest
    ``_dd_y`` *and* the row's total span stays ≤ ``max_span``. That keeps
    HAProxy's sessions/denials together without chaining the whole board
    (gap-only vs running max) or splitting Backend Denials from sessions
    (gap-only vs row min).
    """
    ordered = sorted(
        panels,
        key=lambda p: (
            int(p.get("_dd_y", 0) or 0),
            int(p.get("_dd_x", 0) or 0),
            int(p.get("_free_anchor_y", p["position"]["y"])),
        ),
    )
    rows: list[list[dict[str, Any]]] = []
    for panel in ordered:
        dy = int(panel.get("_dd_y", 0) or 0)
        if not rows:
            rows.append([panel])
            continue
        row_ys = [int(p.get("_dd_y", 0) or 0) for p in rows[-1]]
        row_min = min(row_ys)
        row_max = max(row_ys)
        if dy - row_max <= gap and dy - row_min <= max_span:
            rows[-1].append(panel)
        else:
            rows.append([panel])
    return rows


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
    """Promote a left-aligned note stripe above analytics in the same source row.

    Datadog often places explanatory notes *beside* charts (middle/right of a
    row). Pulling those notes into their own full-width row (the old behaviour)
    destroys the source arrangement. Only split when every note sits entirely
    to the left of every non-note panel — a true intro/header stripe.
    """
    transformed: list[list[dict[str, Any]]] = []
    for row in rows:
        intro_markdown = [panel for panel in row if _markdown_role(panel) == "text"]
        others = [panel for panel in row if _markdown_role(panel) != "text"]
        if intro_markdown and others and len(row) >= 3:
            max_note_extent = max(
                int(panel.get("_dd_x", 0) or 0) + int(panel.get("_dd_w", 1) or 1)
                for panel in intro_markdown
            )
            min_other_x = min(int(panel.get("_dd_x", 0) or 0) for panel in others)
            if max_note_extent <= min_other_x:
                transformed.append(intro_markdown)
                transformed.append(others)
                continue
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
        if role == "header":
            return 2
        if role == "placeholder":
            return 3
        estimated_lines = _estimate_markdown_lines(content, width or 24)
        # Real Datadog notes are body-led (often hide_title); keep them compact.
        if estimated_lines <= 4:
            return 3
        if estimated_lines <= 8:
            return 5
        if estimated_lines <= 13:
            return 8
        return 10
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

    leaf = [p for p in panels if "section" not in p]
    # Free boards already use column-aware packing; the generic pairwise pusher
    # destroys HAProxy/Apache column alignment (tall log streams especially).
    if leaf and _is_wide_free_board(leaf):
        _pack_free_board_vertically(leaf)
        _repair_free_board_vertical_overlaps(leaf)
        return

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
    # metric-redundant-label lint rule and causes compile to fail. Omitting
    # the label entirely makes Lens fall back to the ES|QL field name
    # ("value"). An empty string still falls through to that field name in
    # Lens metric charts, so emit a non-breaking space instead — visually
    # blank, but customLabel stays set.
    if result.kibana_type == "metric":
        if label and label != (widget.title or ""):
            config["label"] = label
        else:
            config["label"] = "\u00a0"
        return config
    if label:
        config["label"] = label
    return config


def _disambiguate_metric_labels(
    metrics: list[dict[str, Any]],
    widget: NormalizedWidget,
) -> None:
    """Give colliding series distinct legend labels so Lens does not append ``[1]``.

    Sibling metrics like ``….proxy_queue_time.sum`` and ``….proxy_queue_time.count``
    both pretty-print to the same parent name; without a suffix Kibana de-dupes
    them as ``Name`` / ``Name [1]``.
    """
    if len(metrics) < 2:
        return
    groups: dict[str, list[dict[str, Any]]] = {}
    for metric in metrics:
        label = str(metric.get("label") or "")
        groups.setdefault(label, []).append(metric)
    for label, group in groups.items():
        if len(group) < 2:
            continue
        for metric in group:
            field = str(metric.get("field") or "")
            leaf = _metric_leaf_for_output_field(widget, field)
            if not leaf:
                continue
            metric["label"] = f"{label} ({leaf})" if label else leaf


def _metric_leaf_for_output_field(widget: NormalizedWidget, field: str) -> str:
    """Return the Datadog metric leaf (``sum``/``count``/…) for an ES|QL output field."""
    normalized = _strip_field_name(field)
    for query in widget.queries:
        mq = query.metric_query
        if not mq:
            continue
        candidates = {_safe_output_name(query.name)}
        if len([q for q in widget.queries if q.metric_query]) == 1:
            candidates.add("value")
        if normalized not in candidates:
            continue
        metric_name = mq.metric or ""
        leaf = metric_name.rsplit(".", 1)[-1] if metric_name else ""
        if leaf and not leaf.isdigit():
            return leaf.lower()
        if query.name:
            return _pretty_field_label(query.name)
    if normalized.startswith("query") or "_" in normalized:
        return normalized.replace("_", "/")
    return ""


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
    parts = [p for p in metric_name.split(".") if p]
    if not parts:
        return metric_name
    leaf = parts[-1]
    if leaf.isdigit() and len(parts) >= 2:
        leaf = f"{parts[-2]} {leaf}"
        parts = parts[:-2] + [leaf]
    # Generic trailing tokens (``.count`` / ``.total``) need parent context so
    # multi-series panels don't all legend as "Count" / "Count [1]".
    generic_leaves = {"count", "total", "sum", "avg", "max", "min", "rate"}
    if leaf.lower() in generic_leaves and len(parts) >= 2:
        # Prefer the last meaningful segment(s) before the generic leaf.
        # e.g. haproxy.frontend.bytes.out.count -> "Bytes out"
        body = parts[:-1]
        if len(body) >= 2:
            body = body[-2:]
        return _pretty_field_label(" ".join(body))
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
