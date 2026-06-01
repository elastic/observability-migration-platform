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

from observability_migration.core.reporting.report import _panel_query_index
from observability_migration.targets.kibana.emit.esql_utils import extract_esql_shape
from observability_migration.targets.kibana.emit.layout import (
    PANEL_SIZE_CONSTRAINTS,
    apply_style_guide_layout,
)

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
KIBANA_MIN_VERSION = "9.1.0"
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
    """Generate a complete kb-dashboard YAML string for a dashboard."""
    panels = []
    result_map = {r.widget_id: r for r in results}

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

    non_section = [p for p in panels if "section" not in p]
    _apply_row_layout(non_section)
    for p in non_section:
        for key in ("_dd_y", "_dd_x", "_dd_w", "_dd_h", "_dd_type", "_dd_display_type", "_markdown_role"):
            p.pop(key, None)

    for p in panels:
        if "section" in p:
            for key in ("_dd_y", "_dd_x", "_dd_w", "_dd_h", "_dd_type", "_dd_display_type", "_markdown_role"):
                p.pop(key, None)
            p.pop("size", None)
            p.pop("position", None)

    _resolve_overlaps(non_section)

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
        dashboard.template_variables, data_view, field_map,
    )
    if controls:
        doc["dashboards"][0]["controls"] = controls

    apply_style_guide_layout(doc)

    return yaml.dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _build_controls_from_template_vars(
    template_vars: list[TemplateVariable],
    data_view: str,
    field_map: FieldMapProfile | None,
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
        es_field = field_map.map_tag(tag, context="metric") if field_map else tag
        control: dict[str, Any] = {
            "type": "options",
            "label": tv.name,
            "data_view": data_view,
            "field": es_field,
            "multiple": len(tv.defaults) > 1 or tv.default == "*",
        }
        controls.append(control)
    return controls


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

    if result.backend == "markdown" or result.status in ("not_feasible", "requires_manual"):
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
    panel["_dd_y"] = dd_y
    panel["_dd_x"] = dd_x
    panel["_dd_w"] = dd_w
    panel["_dd_h"] = int(layout.get("height", 2) or 2)
    panel["_dd_type"] = widget.widget_type
    panel["_dd_display_type"] = widget.display_type
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
            if other_dims:
                esql_block["breakdown"] = _dimension_config(other_dims[0])
                _warn_dropped_xy_breakdowns(other_dims, result)
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
            if metrics:
                esql_block["metrics"] = [
                    _metric_config(widget, result, m)
                    for m in metrics
                ]
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
        if other_dims:
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

    for p in child_panels:
        for key in ("_dd_y", "_dd_x", "_dd_w", "_dd_h", "_dd_type", "_dd_display_type", "_markdown_role"):
            p.pop(key, None)

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
    "hostmap": "markdown",
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


def _apply_heuristic_layout(rows: list[list[dict[str, Any]]]) -> None:
    """Layout using family-based width heuristics (for ordered/stacked dashboards)."""
    y_cursor = 0
    for row_panels in rows:
        widths = _plan_row_widths(row_panels)
        heights = [
            _preferred_panel_height(panel, width)
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
        xs = [int(p.get("_dd_x", 0) or 0) for p in row_panels]
        ws = [int(p.get("_dd_w", 1) or 1) for p in row_panels]
        source_min_x = min(xs) if xs else 0
        source_max_extent = max(x + w for x, w in zip(xs, ws)) if xs else 1
        source_span = max(source_max_extent - source_min_x, 1)
        col_scale = GRID_COLUMNS / source_span

        for panel, dd_x, dd_w in zip(row_panels, xs, ws):
            w = max(MIN_PANEL_WIDTH, round(dd_w * col_scale))
            x = round((dd_x - source_min_x) * col_scale)
            h = _preferred_panel_height(panel, w)
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
    "hostmap": "markdown",
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


def _dimension_config(field: str, data_type: str | None = None) -> dict[str, Any]:
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
