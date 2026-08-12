# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Kibana space-URL helpers and post-validation IR sync.

The YAML *artifact* surfaces this module used to host -- rendering a
kb-dashboard YAML document, shelling out to ``kb-dashboard-cli compile``, and
the legacy saved-objects ``_import`` upload -- have been removed. A migration
writes ``native/*.native.json`` + ``ir/*.ir.json`` and uploads through the
typed Kibana Dashboards API; nothing produces or consumes dashboard YAML.

The ``*_yaml_*`` names that remain here (``YAML_ROUND_TRIPPED_IR_FIELDS``,
``carry_over_non_yaml_ir_fields``) describe the internal ``DashboardIR``
*dict* shape that ``native_dashboard_from_ir`` maps through -- not a file
format. See ``docs/architecture/asset-model.md``.
"""

from __future__ import annotations

import copy
import dataclasses
import re
from urllib.parse import urlsplit, urlunsplit

from observability_migration.core.assets.visual import refresh_visual_ir
from observability_migration.targets.kibana.emit.esql_utils import (
    extract_esql_columns,
    extract_esql_shape,
    is_time_like_output_field,
)


def detect_space_id_from_kibana_url(kibana_url):
    path_parts = [part for part in urlsplit(str(kibana_url or "")).path.split("/") if part]
    for idx, part in enumerate(path_parts[:-1]):
        if part == "s":
            return path_parts[idx + 1]
    return ""


def kibana_url_for_space(kibana_url, space_id=""):
    if not space_id:
        return str(kibana_url or "")
    split = urlsplit(str(kibana_url or ""))
    path_parts = [part for part in split.path.split("/") if part]
    normalized_parts = []
    idx = 0
    while idx < len(path_parts):
        if path_parts[idx] == "s" and idx + 1 < len(path_parts):
            idx += 2
            continue
        normalized_parts.append(path_parts[idx])
        idx += 1
    if space_id:
        normalized_parts.extend(["s", str(space_id)])
    normalized_path = "/" + "/".join(normalized_parts) if normalized_parts else ""
    return urlunsplit((split.scheme, split.netloc, normalized_path, split.query, split.fragment))


def _sync_esql_panel_fields(yaml_panel, old_query, new_query):
    esql_config = yaml_panel.get("esql")
    if not isinstance(esql_config, dict):
        return False
    new_shape = extract_esql_shape(new_query or "")
    old_metric, old_by_cols = extract_esql_columns(old_query or "")
    new_metric, new_by_cols = extract_esql_columns(new_query or "")
    changed = False

    def _replace_field(container, old_value, new_value):
        nonlocal changed
        if not isinstance(container, dict):
            return
        if old_value and new_value and container.get("field") == old_value and old_value != new_value:
            container["field"] = new_value
            changed = True

    def _sync_dimension(field_name):
        nonlocal changed
        if not field_name:
            return
        dimension = esql_config.get("dimension")
        if not isinstance(dimension, dict):
            esql_config["dimension"] = {"field": field_name}
            dimension = esql_config["dimension"]
            changed = True
        elif dimension.get("field") != field_name:
            dimension["field"] = field_name
            changed = True
        if is_time_like_output_field(field_name):
            if dimension.get("data_type") != "date":
                dimension["data_type"] = "date"
                changed = True
        elif "data_type" in dimension:
            dimension.pop("data_type", None)
            changed = True

    # Long-form XY queries keep a single numeric metric column (`value`) plus a
    # synthetic series identity column (`series_group`). When post-validation
    # swaps a panel's query to that shape, field-by-field replacement mutates
    # the existing multi-metric config into an invalid/weak XY contract
    # (`series_group` becomes a y-metric). Rebuild the panel surface explicitly.
    projected = list(new_shape.projected_fields or [])
    new_time_field = next((field for field in (new_shape.time_fields or []) if field in projected), "")
    if (
        esql_config.get("type") in {"line", "area", "bar"}
        and "series_group" in projected
        and "value" in projected
        and new_time_field
    ):
        _sync_dimension(new_time_field)
        breakdown = esql_config.get("breakdown")
        if not isinstance(breakdown, dict):
            esql_config["breakdown"] = {"field": "series_group"}
            changed = True
        elif breakdown.get("field") != "series_group":
            breakdown["field"] = "series_group"
            changed = True
        format_cfg = None
        label_cfg = None
        metrics = esql_config.get("metrics")
        if isinstance(metrics, list):
            for item in metrics:
                if not isinstance(item, dict):
                    continue
                if format_cfg is None and isinstance(item.get("format"), dict):
                    format_cfg = copy.deepcopy(item["format"])
                if (
                    label_cfg is None
                    and str(item.get("field") or item.get("column") or "").strip()
                    in {"value", "computed_value"}
                    and str(item.get("label") or "").strip()
                ):
                    label_cfg = item["label"]
        new_metric_item = {"field": "value"}
        if format_cfg:
            new_metric_item["format"] = format_cfg
        # Preserve a caller-derived label (panel title / static legend
        # fallback for the placeholder ``value`` column, issue #351) across
        # this rebuild -- otherwise a post-validation query swap silently
        # regresses a labeled axis back to the raw column name.
        if label_cfg:
            new_metric_item["label"] = label_cfg
        if esql_config.get("metrics") != [new_metric_item]:
            esql_config["metrics"] = [new_metric_item]
            changed = True
        breakdowns = esql_config.get("breakdowns")
        if isinstance(breakdowns, list) and breakdowns != [{"field": "series_group"}]:
            esql_config["breakdowns"] = [{"field": "series_group"}]
            changed = True
        return changed

    for key in ("primary", "metric"):
        _replace_field(esql_config.get(key), old_metric, new_metric)

    metrics = esql_config.get("metrics")
    if isinstance(metrics, list):
        for item in metrics:
            _replace_field(item, old_metric, new_metric)

    if old_by_cols and new_by_cols:
        _sync_dimension(new_by_cols[0])

    if len(old_by_cols) > 1:
        breakdown = esql_config.get("breakdown")
        if isinstance(breakdown, dict):
            new_breakdown = new_by_cols[1] if len(new_by_cols) > 1 else ""
            if new_breakdown:
                _replace_field(breakdown, old_by_cols[1], new_breakdown)

    breakdowns = esql_config.get("breakdowns")
    if isinstance(breakdowns, list):
        for old_value, new_value in zip(old_by_cols, new_by_cols):
            for item in breakdowns:
                _replace_field(item, old_value, new_value)

    # Issue #109 safety net: a gauge's min/max/goal accessors must reference
    # columns the (resynced) query actually produces. If a resync replaced the
    # query with one that no longer carries the ``_gauge_*`` bounds (e.g. a
    # native-PROMQL gauge whose trailing ``| EVAL _gauge_*`` was dropped), keep
    # the bound only when its column still appears in the query. Otherwise drop
    # it so the gauge degrades gracefully instead of erroring with "Provided
    # column name or index is invalid".
    if esql_config.get("type") == "gauge":
        for bound_key in ("minimum", "maximum", "goal"):
            bound = esql_config.get(bound_key)
            if not isinstance(bound, dict):
                continue
            field = bound.get("field")
            if field and not re.search(rf"\b{re.escape(field)}\b", new_query or ""):
                esql_config.pop(bound_key, None)
                changed = True

    return changed


def _iter_leaf_panels(panels):
    """Yield mutable references to leaf panels, descending into sections."""
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            yield from _iter_leaf_panels(section.get("panels") or [])
        else:
            yield panel


def _pair_yaml_leaves_to_panel_results(leaf_panels, panel_results):
    """Pair YAML leaf panels to translation results without relying on list order.

    Post-translation layout, section wrapping, and polish can reorder YAML leaves
    relative to ``yaml_panel_results``. Index-based ``zip`` then writes the wrong
    query/placeholder into a panel and corrupts ``visual_ir``. Prefer an explicit
    ``_source_panel_id`` stamp when present; otherwise match by title (with a
    same-title ordinal for duplicates), then fall back to remaining positional
    slots.
    """
    leaves = list(leaf_panels or [])
    results = list(panel_results or [])
    paired: list[tuple[dict, object | None]] = [(leaf, None) for leaf in leaves]
    used_results: set[int] = set()

    def _claim(leaf_idx: int, result_idx: int) -> None:
        paired[leaf_idx] = (leaves[leaf_idx], results[result_idx])
        used_results.add(result_idx)

    # 1) Explicit source-panel id (survives only while still on the in-memory
    #    YAML dict; DashboardIR export strips underscore keys).
    by_source_id: dict[str, list[int]] = {}
    for idx, panel_result in enumerate(results):
        source_id = str(getattr(panel_result, "source_panel_id", "") or "").strip()
        if source_id:
            by_source_id.setdefault(source_id, []).append(idx)
    for leaf_idx, leaf in enumerate(leaves):
        source_id = str(leaf.get("_source_panel_id") or "").strip()
        if not source_id:
            continue
        candidates = by_source_id.get(source_id) or []
        for result_idx in candidates:
            if result_idx not in used_results:
                _claim(leaf_idx, result_idx)
                break

    # 2) Title match, preserving relative order among duplicate titles.
    by_title: dict[str, list[int]] = {}
    for idx, panel_result in enumerate(results):
        if idx in used_results:
            continue
        title = str(getattr(panel_result, "title", "") or "").strip()
        if title:
            by_title.setdefault(title, []).append(idx)
    title_cursors: dict[str, int] = {}
    for leaf_idx, (leaf, matched) in enumerate(paired):
        if matched is not None:
            continue
        title = str(leaf.get("title") or "").strip()
        if not title:
            continue
        candidates = by_title.get(title) or []
        cursor = title_cursors.get(title, 0)
        while cursor < len(candidates) and candidates[cursor] in used_results:
            cursor += 1
        if cursor < len(candidates):
            _claim(leaf_idx, candidates[cursor])
            title_cursors[title] = cursor + 1

    # 3) Positional fallback for anything still unmatched.
    unused = [idx for idx in range(len(results)) if idx not in used_results]
    unused_iter = iter(unused)
    for leaf_idx, (leaf, matched) in enumerate(paired):
        if matched is not None:
            continue
        try:
            result_idx = next(unused_iter)
        except StopIteration:
            paired[leaf_idx] = (leaf, None)
            continue
        _claim(leaf_idx, result_idx)

    return paired


_ESQL_PARAM_RE = re.compile(r"(?<!\?)\?(?!\?)(?P<name>[A-Za-z][A-Za-z0-9_]*)")
_ESQL_FIELD_CONTROL_RE = re.compile(r"\?\?(?P<name>[A-Za-z][A-Za-z0-9_]*)")
_ESQL_QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
_INTERNAL_ESQL_PARAMS = {"_tstart", "_tend", "_job"}


def _query_param_names(query):
    if not isinstance(query, str):
        return set()
    unquoted = _ESQL_QUOTED_RE.sub('""', query)
    return {
        match.group("name")
        for match in _ESQL_PARAM_RE.finditer(unquoted)
        if match.group("name") not in _INTERNAL_ESQL_PARAMS
    }


def _query_field_control_names(query):
    if not isinstance(query, str):
        return set()
    unquoted = _ESQL_QUOTED_RE.sub('""', query)
    return {
        match.group("name")
        for match in _ESQL_FIELD_CONTROL_RE.finditer(unquoted)
    }


def _esql_identifier(name):
    text = str(name or "")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        return text
    return "`" + text.replace("`", "``") + "`"


def _query_index(query):
    match = re.search(r"(?im)^\s*(?:FROM|TS)\s+([^\s|]+)", str(query or ""))
    return match.group(1).strip() if match else ""


def _infer_control_data_view(dashboard, leaf_panels):
    for panel in leaf_panels:
        esql_config = panel.get("esql") if isinstance(panel, dict) else None
        if isinstance(esql_config, dict):
            index = _query_index(esql_config.get("query"))
            if index:
                return index
    for control in dashboard.get("controls") or []:
        if not isinstance(control, dict):
            continue
        index = _query_index(control.get("query"))
        if index:
            return index
    return "metrics-*"


def _values_control_query(field_name, data_view):
    field = _esql_identifier(field_name)
    return (
        f"FROM {data_view or 'metrics-*'} | WHERE {field} IS NOT NULL"
        f" | STATS count = COUNT(*) BY {field}"
        f" | SORT {field} ASC | KEEP {field} | LIMIT 1000"
    )


def _ensure_controls_for_emitted_params(dashboard, leaf_panels):
    emitted: set[str] = set()
    emitted_fields: set[str] = set()
    for panel in leaf_panels:
        if not isinstance(panel, dict):
            continue
        esql_config = panel.get("esql")
        query = esql_config.get("query") if isinstance(esql_config, dict) else None
        emitted |= _query_param_names(query)
        emitted_fields |= _query_field_control_names(query)

    controls = dashboard.setdefault("controls", [])
    value_bound = {
        control.get("variable_name")
        for control in controls
        if isinstance(control, dict)
        and control.get("type") == "esql"
        and control.get("variable_name")
        and control.get("variable_type") != "fields"
    }
    # A fields control cannot bind ``?name``. Do not auto-create a duplicate
    # values control when the dashboard also still emits ``??name``; the lint
    # gate reports that unsatisfiable dual-semantics shape. When the field
    # control is stale, replace it with the required values control.
    missing = sorted(
        name
        for name in emitted
        if name not in value_bound and name not in emitted_fields
    )
    if not missing:
        return False

    data_view = _infer_control_data_view(dashboard, leaf_panels)
    for name in missing:
        controls[:] = [
            control
            for control in controls
            if not (
                isinstance(control, dict)
                and control.get("type") == "esql"
                and control.get("variable_name") == name
                and control.get("variable_type") == "fields"
            )
        ]
        controls.append(
            {
                "type": "esql",
                "label": name,
                "variable_name": name,
                "variable_type": "values",
                "query": _values_control_query(name, data_view),
                "multiple": False,
                "default": ".*",
            }
        )
    return True


# The YAML document shape is a LOSSY carrier for a ``DashboardIR``: its schema
# (``docs/dashboards/schema.json``) declares ``additionalProperties: false``, so
# ``DashboardIR.to_yaml_dict()`` emits -- and ``from_yaml_dict()`` restores --
# only the fields below. Rebuilding an IR from that document therefore resets
# every *other* field to its dataclass default unless it is carried across
# explicitly (see :func:`carry_over_non_yaml_ir_fields`).
YAML_ROUND_TRIPPED_IR_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "minimum_kibana_version",
        "settings",
        "panels",
        "filters",
        "controls",
    }
)

# The complement: ``DashboardIR`` fields the YAML shape cannot express, which the
# rebuild has to carry over from the pre-rebuild IR. Spelled out rather than
# derived so the exhaustiveness guard in ``tests/test_migrate.py``
# (``test_validate_stage_ir_rebuild_classifies_every_dashboard_ir_field``) fails
# when a new IR field is left unclassified. The rebuild itself carries *any*
# field outside :data:`YAML_ROUND_TRIPPED_IR_FIELDS`, so a newly added field is
# never silently dropped while that classification is pending.
IR_FIELDS_CARRIED_ACROSS_YAML_REBUILD: frozenset[str] = frozenset(
    {
        "version",
        "uid",
        "source_adapter",
        "source_file",
        "folder",
        "tags",
        # Part of dashboard identity. Losing it here would rebuild the native
        # payload under the plain title slug, and the rebuilt dashboard would
        # upsert over its same-titled sibling on upload.
        "id_disambiguator",
        # Same "YAML shape cannot carry it" reasoning as ``tags``: neither has
        # a slot in ``docs/dashboards/schema.json`` (``refresh_interval`` not
        # at all; ``time_range`` there has no ``mode``), so
        # ``native_dashboard_from_ir`` reads both straight off the IR.
        "time_range",
        "refresh_interval",
        "alerts",
        "annotations",
        "links",
        "transforms",
        "metadata",
        "source_extension",
    }
)

# ``sync_result_queries_to_ir`` lives in shared target code but is only reached
# from the Grafana pipeline today, so the rebuild needs a source adapter to name
# when the IR under sync does not carry one. It is a *fallback*, never an
# override -- see :func:`carry_over_non_yaml_ir_fields`.
_REBUILD_FALLBACK_SOURCE_ADAPTER = "grafana"


def carry_over_non_yaml_ir_fields(rebuilt, original, *, fallback_source_adapter=""):
    """Copy every ``DashboardIR`` field the YAML document shape cannot carry.

    Driven off ``dataclasses.fields(DashboardIR)`` instead of a hand-written copy
    list: a field added to the IR is carried across automatically, so this cannot
    quietly start losing data (dashboard ``tags`` -- which
    ``dashboards_api.native_dashboard_from_ir`` reads straight off the IR and
    uploads to Kibana -- were lost exactly that way). Values are deep-copied so
    the pre- and post-rebuild IRs never alias each other.

    ``source_adapter`` is handled deliberately rather than copied blind: the
    original IR is authoritative, and ``fallback_source_adapter`` applies only
    when it is empty.
    """
    from observability_migration.core.assets.dashboard import DashboardIR

    for ir_field in dataclasses.fields(DashboardIR):
        name = ir_field.name
        if name in YAML_ROUND_TRIPPED_IR_FIELDS or not hasattr(original, name):
            continue
        value = getattr(original, name)
        if name == "source_adapter":
            setattr(rebuilt, name, str(value or fallback_source_adapter))
            continue
        setattr(rebuilt, name, copy.deepcopy(value))
    return rebuilt


def sync_result_queries_to_ir(result):
    """Fold post-validation query fixes back into ``result.dashboard_ir``.

    Validation mutates ``panel_result.esql_query`` (auto-fixes) and can
    manualize a panel into a markdown placeholder. Those fixes have to reach
    the artifacts the run actually writes (``native/``, ``ir/``), so this walks
    the dashboard dict derived from the IR, applies the fixes to it, and
    rebuilds both the IR and the native payload from the mutated dict. Nothing
    is written to disk here.
    """
    dashboard_ir = getattr(result, "dashboard_ir", None)
    if dashboard_ir is None:
        return False
    dashboard = dashboard_ir.to_yaml_dict()
    panels = dashboard.get("panels") or []
    leaf_panels = list(_iter_leaf_panels(panels))
    yaml_panel_results = getattr(result, "yaml_panel_results", None)
    panel_results = yaml_panel_results if yaml_panel_results is not None else result.panel_results
    updated = False
    for yaml_panel, panel_result in _pair_yaml_leaves_to_panel_results(leaf_panels, panel_results):
        if panel_result is None:
            continue
        if str(panel_result.post_validation_action or "").startswith("placeholder_"):
            yaml_panel.pop("esql", None)
            yaml_panel["markdown"] = {
                "content": panel_result.post_validation_message or "*(Manual review required after validation.)*"
            }
            updated = True
            panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)
            continue
        esql_config = yaml_panel.get("esql")
        if not isinstance(esql_config, dict):
            panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)
            continue
        if not panel_result.esql_query:
            panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)
            continue
        existing_query = esql_config.get("query") or ""
        if existing_query == panel_result.esql_query:
            panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)
            continue
        esql_config["query"] = panel_result.esql_query
        _sync_esql_panel_fields(yaml_panel, existing_query, panel_result.esql_query)
        updated = True
        panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)
    if _ensure_controls_for_emitted_params(dashboard, leaf_panels):
        updated = True
    if updated:
        # Deferred import: dashboards_api.py imports kibana_url_for_space
        # from this module, so a module-level import here would cycle.
        # `DashboardIR` is the primary artifact: rebuild it from the same
        # in-memory `dashboard` dict just mutated (post-validation fixes --
        # placeholder rewrites, corrected queries/indexes/controls) and derive
        # the native payload *from that IR*, so the two artifacts the run
        # writes cannot drift from post-validation fixes or from each other.
        from observability_migration.core.assets.dashboard import DashboardIR
        from observability_migration.targets.kibana.dashboards_api import (
            native_dashboard_from_ir,
        )

        # The YAML document is a lossy carrier (additionalProperties: false), so
        # the rebuild alone would reset dashboard identity, lineage and the
        # referenced asset collections to their defaults -- shipping the
        # dashboard to Kibana with, for one, its `tags` stripped.
        previous_ir = dashboard_ir
        dashboard_ir = DashboardIR.from_yaml_dict(
            dashboard, source_adapter=_REBUILD_FALLBACK_SOURCE_ADAPTER
        )
        carry_over_non_yaml_ir_fields(
            dashboard_ir,
            previous_ir,
            fallback_source_adapter=_REBUILD_FALLBACK_SOURCE_ADAPTER,
        )
        result.dashboard_ir = dashboard_ir
        native_dashboard, native_counts = native_dashboard_from_ir(dashboard_ir)
        result.native_dashboard = native_dashboard
        native_counts_dict, native_reasons = native_counts.as_dicts()
        result.native_dashboard_stats = {**native_counts_dict, "reasons": native_reasons}
    return updated


__all__ = [
    "IR_FIELDS_CARRIED_ACROSS_YAML_REBUILD",
    "YAML_ROUND_TRIPPED_IR_FIELDS",
    "carry_over_non_yaml_ir_fields",
    "detect_space_id_from_kibana_url",
    "kibana_url_for_space",
    "sync_result_queries_to_ir",
]
