"""Typed Kibana Dashboards API conformance oracle.

The saved-object import path accepts a stringified ``panelsJSON`` blob. That is
useful for migration, but it is not a strong contract for "can Kibana's typed UI
model accept this dashboard?". Kibana 9.4+ exposes a typed Dashboards API
(``POST /api/dashboards``) that validates dashboard and visualization payloads
server-side.

This module converts the *emitted* migration presentation (``visual_ir`` in
``migration_report.json``) into the typed Dashboards API shape for the common
ES|QL chart families, submits it to a scratch dashboard, and classifies any 400
as a UI-contract gap. Unsupported chart families are reported explicitly rather
than guessed.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from observability_migration.targets.kibana import dashboards_api as production_dashboards_api

ApiCall = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any] | str]]
_production_map_panel = production_dashboards_api.map_panel

_XY_TYPES = {"line", "area", "bar"}
# Every visualization type the migration engine emits has an ES|QL variant on the
# native Dashboards API (verified live against Elastic Serverless 9.5.0 — all 11
# chart families return HTTP 201). Only ``legacy_metric`` lacks an ES|QL variant.
# ``line``/``bar``/``area`` collapse into a single API ``xy`` panel with per-layer
# series types. The concrete per-type config shapes and the set of supported
# ``config.type`` values are defined by ``_ESQL_CONFIG_BUILDERS`` /
# ``_SUPPORTED_ESQL_TYPES`` below (after the column helpers they depend on).


def _is_time_like(field: str) -> bool:
    name = str(field or "").strip("`")
    return name in {"time_bucket", "timestamp_bucket", "step", "@timestamp"} or "bucket" in name.lower()


def _fallback_esql_config(panel: dict[str, Any]) -> dict[str, Any]:
    yaml_panel_value = panel.get("yaml_panel")
    yaml_panel = yaml_panel_value if isinstance(yaml_panel_value, dict) else {}
    yaml_esql_value = yaml_panel.get("esql")
    yaml_esql = yaml_esql_value if isinstance(yaml_esql_value, dict) else {}
    if yaml_esql:
        return dict(yaml_esql)
    query = str(panel.get("esql_query") or panel.get("esql") or "").strip()
    if not query:
        return {}
    query_ir_value = panel.get("query_ir")
    query_ir = query_ir_value if isinstance(query_ir_value, dict) else {}
    metric = str(query_ir.get("output_metric_field") or "value")
    groups = [str(item) for item in (query_ir.get("output_group_fields") or []) if str(item)]
    time_dim = next((field for field in groups if _is_time_like(field)), "time_bucket")
    breakdown = next((field for field in groups if not _is_time_like(field)), "")
    kibana_type = str(panel.get("kibana_type") or "").lower()
    chart_type = {
        "xy": "line",
        "metric": "metric",
        "table": "datatable",
        "partition": "pie",
        "treemap": "treemap",
        "heatmap": "heatmap",
    }.get(kibana_type, kibana_type)
    config: dict[str, Any] = {"type": chart_type, "query": query}
    if chart_type in _XY_TYPES:
        config["dimension"] = {"field": time_dim}
        config["metrics"] = [{"field": metric}]
        if breakdown:
            config["breakdown"] = {"field": breakdown}
    elif chart_type == "metric":
        config["primary"] = {"field": metric}
    return config


@dataclass
class Finding:
    category: str
    severity: str
    dashboard: str
    panel: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "dashboard": self.dashboard,
            "panel": self.panel,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


def _visual_presentation(panel: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    vir = panel.get("visual_ir") if isinstance(panel.get("visual_ir"), dict) else {}
    pres = vir.get("presentation") if isinstance(vir, dict) else {}
    if not isinstance(pres, dict):
        fallback = _fallback_esql_config(panel)
        return ("esql", fallback) if fallback else ("", {})
    cfg_value = pres.get("config")
    cfg: dict[str, Any] = cfg_value if isinstance(cfg_value, dict) else {}
    kind = str(pres.get("kind") or "")
    if not kind and (fallback := _fallback_esql_config(panel)):
        return "esql", fallback
    return kind, dict(cfg)


def _layout(panel: dict[str, Any]) -> dict[str, int]:
    vir = panel.get("visual_ir") if isinstance(panel.get("visual_ir"), dict) else {}
    layout = vir.get("layout") if isinstance(vir, dict) else {}
    if not isinstance(layout, dict):
        layout = {}
    return {
        "x": int(layout.get("x") or 0),
        "y": int(layout.get("y") or 0),
        "w": int(layout.get("w") or 24),
        "h": int(layout.get("h") or 8),
    }


def _field(obj: Any) -> str:
    if isinstance(obj, dict):
        return str(obj.get("field") or obj.get("column") or "").strip("`")
    return ""


def _metric_items(config: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in keys:
        value = config.get(key)
        if isinstance(value, dict) and _field(value):
            items.append(value)
    metrics = config.get("metrics")
    if isinstance(metrics, list):
        items.extend(item for item in metrics if isinstance(item, dict) and _field(item))
    return items


def _metric_fields(config: dict[str, Any]) -> list[str]:
    fields = [_field(item) for item in _metric_items(config, "metric", "primary")]
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(field for field in fields if field))


# --------------------------------------------------------------------------- #
# Column helpers: ``visual_ir.presentation.config`` records the ES|QL output
# column as ``field`` (also accepts ``column``); the typed API calls it
# ``column``. These normalize both singular ``{field}`` dicts and lists.
# --------------------------------------------------------------------------- #

def _column(obj: Any) -> dict[str, Any] | None:
    name = _field(obj)
    return {"column": name} if name else None


def _api_format(obj: Any) -> dict[str, Any] | None:
    """Return only field-format objects accepted by the typed API."""
    if not isinstance(obj, dict):
        return None
    typ = str(obj.get("type") or "").lower()
    if typ in {"number", "percent"}:
        out: dict[str, Any] = {"type": typ}
        if isinstance(obj.get("decimals"), int | float) and not isinstance(obj.get("decimals"), bool):
            out["decimals"] = obj["decimals"]
        if isinstance(obj.get("suffix"), str):
            out["suffix"] = obj["suffix"]
        if isinstance(obj.get("compact"), bool):
            out["compact"] = obj["compact"]
        return out
    if typ in {"bits", "bytes"}:
        out = {"type": typ}
        if isinstance(obj.get("decimals"), int | float) and not isinstance(obj.get("decimals"), bool):
            out["decimals"] = obj["decimals"]
        if isinstance(obj.get("suffix"), str):
            out["suffix"] = obj["suffix"]
        return out
    if typ == "duration":
        # Verified live on Serverless 9.5.0: the single-value metric/gauge
        # format schema accepts a bare ``{type: duration}``, but the
        # multi-column schema (xy/data_table/etc.) *requires* ``from``/``to``
        # — and both schemas accept the pair harmlessly when present. Default
        # rather than drop so this oracle doesn't reject a format the live API
        # actually accepts. Defaults mirror Kibana's own DurationFormat
        # defaults (seconds -> humanize; see dashboards_api.py's ``_api_format``
        # for the source citations), so an unspecified format still renders
        # like Kibana's own default "Duration" value-format selection.
        # Units use the transform's abbreviated vocabulary and the branch is
        # closed to {type, from, to}: long names ("seconds") and extra keys
        # ("suffix"/"decimals") are rejected by the API, so mirror what the
        # emitter now produces rather than the looser shape the deprecated
        # compiler used to tolerate.
        from observability_migration.targets.kibana.dashboards_api import (
            _duration_output,
            _duration_unit,
        )

        return {
            "type": "duration",
            "from": _duration_unit(obj.get("from"), default="s"),
            "to": _duration_output(obj.get("to")),
        }
    if typ == "custom" and isinstance(obj.get("pattern"), str) and obj["pattern"]:
        return {"type": "custom", "pattern": obj["pattern"]}
    return None


def _api_color(
    obj: Any,
    *,
    static: bool = False,
    auto: bool = False,
    none: bool = False,
    dynamic: bool = False,
    mapping: bool = False,
) -> dict[str, Any] | None:
    """Return only color shapes accepted by the target field."""
    if not isinstance(obj, dict):
        return None
    typ = str(obj.get("type") or "").lower()
    if static and typ == "static" and isinstance(obj.get("color"), str) and obj["color"]:
        return {"type": "static", "color": obj["color"]}
    if auto and typ == "auto":
        return {"type": "auto"}
    if none and typ == "none":
        return {"type": "none"}
    if dynamic and typ == "dynamic":
        range_type = obj.get("range")
        steps = obj.get("steps")
        if range_type not in {"absolute", "percentage"} or not isinstance(steps, list) or not steps:
            return None
        safe_steps: list[dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("color"), str):
                return None
            safe_step: dict[str, Any] = {"color": step["color"]}
            for key in ("gte", "lt", "lte"):
                value = step.get(key)
                if value is None or (isinstance(value, int | float) and not isinstance(value, bool)):
                    safe_step[key] = value
            safe_steps.append(safe_step)
        return {"type": "dynamic", "range": range_type, "steps": safe_steps}
    if mapping:
        mode = obj.get("mode")
        if mode == "categorical" and isinstance(obj.get("palette"), str) and isinstance(obj.get("mapping"), list):
            out: dict[str, Any] = {
                "mode": "categorical",
                "palette": obj["palette"],
                "mapping": obj["mapping"],
            }
            if isinstance(obj.get("unassigned"), dict):
                out["unassigned"] = obj["unassigned"]
            return out
        if mode == "gradient" and isinstance(obj.get("palette"), str):
            return {"mode": "gradient", "palette": obj["palette"]}
    return None


def _api_summary(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict) or obj.get("type") not in {"sum", "avg", "count", "min", "max"}:
        return None
    out = {"type": obj["type"]}
    if isinstance(obj.get("label"), str):
        out["label"] = obj["label"]
    return out


def _api_title(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = {}
    if isinstance(obj.get("text"), str):
        out["text"] = obj["text"]
    if isinstance(obj.get("visible"), bool):
        out["visible"] = obj["visible"]
    return out or None


def _api_ticks(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = {}
    if obj.get("mode") in {"auto", "bands"}:
        out["mode"] = obj["mode"]
    if isinstance(obj.get("visible"), bool):
        out["visible"] = obj["visible"]
    return out or None


def _api_column(
    obj: Any,
    *,
    color: str = "",
    axis: bool = False,
    collapse: bool = False,
    columns: bool = False,
    table: bool = False,
    summary: bool = False,
    subtitle: bool = False,
    apply_color_to: set[str] | None = None,
) -> dict[str, Any] | None:
    col = _column(obj)
    if col is None or not isinstance(obj, dict):
        return col
    if isinstance(obj.get("label"), str) and obj["label"]:
        col["label"] = obj["label"]
    if subtitle and isinstance(obj.get("subtitle"), str):
        col["subtitle"] = obj["subtitle"]
    fmt = _api_format(obj.get("format"))
    if fmt:
        col["format"] = fmt
    color_value: dict[str, Any] | None = None
    if color == "static_auto":
        color_value = _api_color(obj.get("color"), static=True, auto=True)
    elif color == "dynamic_auto":
        color_value = _api_color(obj.get("color"), dynamic=True, auto=True)
    elif color == "dynamic_auto_none":
        color_value = _api_color(obj.get("color"), dynamic=True, auto=True, none=True)
    elif color == "dynamic_mapping_auto":
        color_value = _api_color(obj.get("color"), dynamic=True, mapping=True, auto=True)
    elif color == "mapping_auto":
        color_value = _api_color(obj.get("color"), mapping=True, auto=True)
    elif color == "mapping":
        color_value = _api_color(obj.get("color"), mapping=True)
    elif color == "static_auto_dynamic":
        color_value = _api_color(obj.get("color"), static=True, auto=True, dynamic=True)
    elif color == "static_none":
        color_value = _api_color(obj.get("color"), static=True, none=True)
    if color_value:
        col["color"] = color_value
    if axis and obj.get("axis") in {"y", "y2"}:
        col["axis"] = obj["axis"]
    if collapse and obj.get("collapse_by") in {"avg", "sum", "max", "min"}:
        col["collapse_by"] = obj["collapse_by"]
    if columns and isinstance(obj.get("columns"), int | float) and not isinstance(obj.get("columns"), bool):
        col["columns"] = obj["columns"]
    if apply_color_to is not None and obj.get("apply_color_to") in apply_color_to:
        col["apply_color_to"] = obj["apply_color_to"]
    if table:
        if obj.get("alignment") in {"left", "center", "right"}:
            col["alignment"] = obj["alignment"]
        if isinstance(obj.get("visible"), bool):
            col["visible"] = obj["visible"]
        if isinstance(obj.get("width"), int | float) and not isinstance(obj.get("width"), bool) and obj["width"] >= 0:
            col["width"] = obj["width"]
    if summary and (safe_summary := _api_summary(obj.get("summary"))):
        col["summary"] = safe_summary
    return col


def _columns(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(items, list):
        for item in items:
            col = _column(item)
            if col:
                out.append(col)
    return out


def _first_column(config: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    """First column ref found across ``keys`` (singular dict or list value)."""
    for key in keys:
        value = config.get(key)
        col = _column(value)
        if col:
            return col
        cols = _columns(value)
        if cols:
            return cols[0]
    return None


def _api_columns(items: Any, **kwargs: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(items, list):
        for item in items:
            col = _api_column(item, **kwargs)
            if col:
                out.append(col)
    return out


def _first_api_column(config: dict[str, Any], *keys: str, **kwargs: Any) -> dict[str, Any] | None:
    for key in keys:
        value = config.get(key)
        col = _api_column(value, **kwargs)
        if col:
            return col
        cols = _api_columns(value, **kwargs)
        if cols:
            return cols[0]
    return None


def _group_columns(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Grouping columns, preferring list keys then a singular ``breakdown``."""
    groups = _columns(config.get("breakdowns")) or _columns(config.get("group_by"))
    if not groups:
        single = _column(config.get("breakdown"))
        if single:
            groups = [single]
    return groups


def _metric_column(config: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    for item in _metric_items(config, "metric", "primary"):
        col = _api_column(item, **kwargs)
        if col:
            return col
    return {"column": "value"}


def _xy_series_type(config: dict[str, Any]) -> str:
    base = {"line": "line", "bar": "bar", "area": "area"}.get(str(config.get("type") or ""), "line")
    mode = str(config.get("mode") or "").lower()
    if base == "line":
        return base
    if base == "bar" and config.get("horizontal") is True:
        if mode in {"stacked", "stack"}:
            return "bar_horizontal_stacked"
        if mode in {"percentage", "percent", "normalized"}:
            return "bar_horizontal_percentage"
        return "bar_horizontal"
    if mode in {"stacked", "stack"}:
        return f"{base}_stacked"
    if mode in {"percentage", "percent", "normalized"}:
        return f"{base}_percentage"
    return base


def _api_legend(obj: Any, *, kind: str) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    visibility_values = {"visible", "hidden"} if kind == "heatmap" else {"auto", "visible", "hidden"}
    out: dict[str, Any] = {}
    if obj.get("visibility") in visibility_values:
        out["visibility"] = obj["visibility"]
    if obj.get("size") in {"auto", "s", "m", "l", "xl"}:
        out["size"] = obj["size"]
    if isinstance(obj.get("truncate_after_lines"), int | float) and 1 <= obj["truncate_after_lines"] <= 10:
        out["truncate_after_lines"] = obj["truncate_after_lines"]
    if kind == "xy":
        position = obj.get("position")
        placement = obj.get("placement")
        if placement == "inside":
            out["placement"] = "inside"
            if position in {"top_left", "top_right", "bottom_left", "bottom_right"}:
                out["position"] = position
        elif placement == "outside" or position in {"left", "right", "top", "bottom"}:
            out["placement"] = "outside"
            if position in {"left", "right", "top", "bottom"}:
                out["position"] = position
        stats = obj.get("statistics")
        if isinstance(stats, list):
            valid = {
                "min",
                "max",
                "avg",
                "median",
                "range",
                "last_value",
                "last_non_null_value",
                "first_value",
                "first_non_null_value",
                "difference",
                "difference_percentage",
                "count",
                "total",
                "standard_deviation",
                "variance",
                "distinct_count",
                "current_and_last_value",
            }
            safe_stats = [stat for stat in stats if stat in valid]
            if safe_stats:
                out["statistics"] = safe_stats[:17]
    elif kind == "mosaic" and isinstance(obj.get("nested"), bool):
        out["nested"] = obj["nested"]
    return out or None


def _api_xy_axis(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = {}
    for axis_name in ("x", "y", "y2"):
        source = obj.get(axis_name)
        if not isinstance(source, dict):
            continue
        axis: dict[str, Any] = {}
        if axis_name == "x" and source.get("scale") in {"ordinal", "temporal", "linear"}:
            axis["scale"] = source["scale"]
        if axis_name in {"y", "y2"} and source.get("scale") in {"linear", "log", "sqrt"}:
            axis["scale"] = source["scale"]
        domain = source.get("domain")
        if isinstance(domain, dict):
            domain_type = domain.get("type")
            if domain_type == "fit":
                axis["domain"] = {"type": "fit"}
            elif domain_type == "full":
                axis["domain"] = {"type": "full"}
            elif (
                domain_type == "custom"
                and isinstance(domain.get("min"), int | float)
                and isinstance(domain.get("max"), int | float)
                and not isinstance(domain.get("min"), bool)
                and not isinstance(domain.get("max"), bool)
            ):
                axis["domain"] = {"type": "custom", "min": domain["min"], "max": domain["max"]}
        if grid := _visible_config(source.get("grid")):
            axis["grid"] = grid
        if ticks := _visible_config(source.get("ticks")):
            axis["ticks"] = ticks
        if title := _api_title(source.get("title")):
            axis["title"] = title
        labels = source.get("labels")
        if isinstance(labels, dict) and labels.get("orientation") in {-90, -45, 0, 45, 90}:
            axis["labels"] = {"orientation": labels["orientation"]}
        if axis:
            out[axis_name] = axis
    return out or None


def _visible_config(obj: Any) -> dict[str, bool] | None:
    if isinstance(obj, dict) and isinstance(obj.get("visible"), bool):
        return {"visible": obj["visible"]}
    return None


def _api_metric_styling(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = {}
    primary = obj.get("primary")
    if isinstance(primary, dict):
        safe_primary: dict[str, Any] = {}
        if primary.get("position") in {"top", "middle", "bottom"}:
            safe_primary["position"] = primary["position"]
        labels = primary.get("labels")
        if isinstance(labels, dict) and labels.get("alignment") in {"left", "center", "right"}:
            safe_primary["labels"] = {"alignment": labels["alignment"]}
        value = primary.get("value")
        if isinstance(value, dict):
            safe_value: dict[str, Any] = {}
            if value.get("alignment") in {"left", "center", "right"}:
                safe_value["alignment"] = value["alignment"]
            if value.get("sizing") in {"auto", "fill"}:
                safe_value["sizing"] = value["sizing"]
            if safe_value:
                safe_primary["value"] = safe_value
        if safe_primary:
            out["primary"] = safe_primary
    secondary = obj.get("secondary")
    if isinstance(secondary, dict):
        safe_secondary: dict[str, Any] = {}
        label = secondary.get("label")
        if isinstance(label, dict):
            safe_label: dict[str, Any] = {}
            if label.get("placement") in {"before", "after"}:
                safe_label["placement"] = label["placement"]
            if isinstance(label.get("visible"), bool):
                safe_label["visible"] = label["visible"]
            if safe_label:
                safe_secondary["label"] = safe_label
        value = secondary.get("value")
        if isinstance(value, dict) and value.get("alignment") in {"left", "center", "right"}:
            safe_secondary["value"] = {"alignment": value["alignment"]}
        if safe_secondary:
            out["secondary"] = safe_secondary
    return out or None


def _api_gauge_shape(config: dict[str, Any]) -> dict[str, Any] | None:
    appearance_value = config.get("appearance")
    appearance: dict[str, Any] = appearance_value if isinstance(appearance_value, dict) else {}
    source = config.get("shape") or appearance.get("shape")
    if isinstance(source, str):
        shape_type = source
        orientation = config.get("orientation") or appearance.get("orientation")
    elif isinstance(source, dict):
        shape_type = source.get("type")
        orientation = source.get("orientation")
    else:
        return None
    if shape_type == "bullet":
        out = {"type": "bullet"}
        if orientation in {"horizontal", "vertical"}:
            out["orientation"] = orientation
        return out
    if shape_type in {"circle", "semi_circle", "arc"}:
        return {"type": shape_type}
    return None


def _api_value_display(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = {}
    if isinstance(obj.get("visible"), bool):
        out["visible"] = obj["visible"]
    if obj.get("mode") in {"absolute", "percentage"}:
        out["mode"] = obj["mode"]
    if (
        isinstance(obj.get("percent_decimals"), int | float)
        and not isinstance(obj.get("percent_decimals"), bool)
        and 0 <= obj["percent_decimals"] <= 10
    ):
        out["percent_decimals"] = obj["percent_decimals"]
    return out or None


def _api_partition_styling(obj: Any, *, api_type: str) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = {}
    if values := _api_value_display(obj.get("values")):
        out["values"] = values
    if api_type == "pie":
        if obj.get("donut_hole") in {"none", "s", "m", "l"}:
            out["donut_hole"] = obj["donut_hole"]
        labels = obj.get("labels")
        if isinstance(labels, dict):
            safe_labels: dict[str, Any] = {}
            if labels.get("position") in {"inside", "outside"}:
                safe_labels["position"] = labels["position"]
            if isinstance(labels.get("visible"), bool):
                safe_labels["visible"] = labels["visible"]
            if safe_labels:
                out["labels"] = safe_labels
    return out or None


def _api_heatmap_axis(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = {}
    x_source = obj.get("x")
    if isinstance(x_source, dict) and x_source.get("scale") in {"ordinal", "temporal", "linear"}:
        x_axis: dict[str, Any] = {"scale": x_source["scale"]}
        if x_source.get("sort") in {"asc", "desc"}:
            x_axis["sort"] = x_source["sort"]
        if title := _api_title(x_source.get("title")):
            x_axis["title"] = title
        labels = x_source.get("labels")
        if isinstance(labels, dict):
            safe_labels: dict[str, Any] = {}
            if isinstance(labels.get("visible"), bool):
                safe_labels["visible"] = labels["visible"]
            if labels.get("orientation") in {-90, -45, 0, 45, 90}:
                safe_labels["orientation"] = labels["orientation"]
            if safe_labels:
                x_axis["labels"] = safe_labels
        out["x"] = x_axis
    y_source = obj.get("y")
    if isinstance(y_source, dict):
        y_axis: dict[str, Any] = {}
        if y_source.get("sort") in {"asc", "desc"}:
            y_axis["sort"] = y_source["sort"]
        if title := _api_title(y_source.get("title")):
            y_axis["title"] = title
        labels = y_source.get("labels")
        if isinstance(labels, dict) and isinstance(labels.get("visible"), bool):
            y_axis["labels"] = {"visible": labels["visible"]}
        if y_axis:
            out["y"] = y_axis
    return out or None


def _api_datatable_styling(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    out: dict[str, Any] = {}
    if obj.get("paging") in {10, 20, 30, 50, 100}:
        out["paging"] = obj["paging"]
    if row_numbers := _visible_config(obj.get("row_numbers")):
        out["row_numbers"] = row_numbers
    sort_by = obj.get("sort_by")
    if isinstance(sort_by, dict) and sort_by.get("direction") in {"asc", "desc"}:
        column_type = sort_by.get("column_type")
        index = sort_by.get("index")
        if column_type in {"metric", "row"} and isinstance(index, int | float) and index >= 0:
            out["sort_by"] = {
                "column_type": column_type,
                "index": index,
                "direction": sort_by["direction"],
            }
        elif column_type == "pivoted_metric" and isinstance(index, int | float) and index >= 0:
            values = sort_by.get("values")
            if isinstance(values, list) and values and all(isinstance(value, str) for value in values):
                out["sort_by"] = {
                    "column_type": "pivoted_metric",
                    "index": index,
                    "values": values[:20],
                    "direction": sort_by["direction"],
                }
    return out or None


# --------------------------------------------------------------------------- #
# Per-type config builders: emitted ``esql`` presentation config -> typed API
# ``vis.config``. Shapes verified live against Elastic Serverless 9.5.0.
# --------------------------------------------------------------------------- #

def _build_xy(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    x_col = _api_column(config.get("dimension")) or {"column": "time_bucket"}
    y_cols: list[dict[str, Any]] = []
    for item in _metric_items(config, "metric", "primary"):
        col = _api_column(item, color="static_auto", axis=True)
        if col:
            y_cols.append(col)
    layer: dict[str, Any] = {
        "type": _xy_series_type(config),
        "data_source": {"type": "esql", "query": query},
        "x": x_col,
        "y": y_cols or [{"column": "value"}],
    }
    breakdown = _first_api_column(config, "breakdown", "breakdowns", color="mapping", collapse=True)
    if breakdown:
        layer["breakdown_by"] = breakdown
    out: dict[str, Any] = {"type": "xy", "title": title, "layers": [layer]}
    if legend := _api_legend(config.get("legend"), kind="xy"):
        out["legend"] = legend
    if axis := _api_xy_axis(config.get("axis")):
        out["axis"] = axis
    return out


def _build_metric(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    primary = (
        _api_column(
            config.get("primary"),
            color="static_auto_dynamic",
            subtitle=True,
            apply_color_to={"value", "background"},
        )
        or _api_column(
            config.get("metric"),
            color="static_auto_dynamic",
            subtitle=True,
            apply_color_to={"value", "background"},
        )
        or (
            _api_columns(
                config.get("metrics"),
                color="static_auto_dynamic",
                subtitle=True,
                apply_color_to={"value", "background"},
            )
            or [{"column": "value"}]
        )[0]
    )
    metrics: list[dict[str, Any]] = [{"type": "primary", **primary}]
    secondary = _api_column(config.get("secondary"), color="static_none")
    metric_list = _api_columns(config.get("metrics"), color="static_auto_dynamic")
    if secondary is None and len(metric_list) > 1:
        secondary = metric_list[1]
    if secondary:
        metrics.append({"type": "secondary", **secondary})
    out: dict[str, Any] = {
        "type": "metric",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metrics": metrics[:2],
    }
    breakdown = _first_api_column(
        config,
        "breakdown_by",
        "breakdown",
        "breakdowns",
        collapse=True,
        columns=True,
    )
    if breakdown:
        out["breakdown_by"] = breakdown
    if styling := _api_metric_styling(config.get("styling")):
        out["styling"] = styling
    return out


def _build_gauge(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    metric = _metric_column(config, color="dynamic_auto_none", subtitle=True)
    for target, keys in {
        "min": ("min", "minimum"),
        "max": ("max", "maximum"),
        "goal": ("goal",),
    }.items():
        bound = _first_api_column(config, *keys)
        if bound:
            metric[target] = bound
    if ticks := _api_ticks(config.get("ticks")):
        metric["ticks"] = ticks
    title_config = config.get("title") if isinstance(config.get("title"), dict) else config.get("title_config")
    if gauge_title := _api_title(title_config):
        metric["title"] = gauge_title
    out: dict[str, Any] = {
        "type": "gauge",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metric": metric,
    }
    if shape := _api_gauge_shape(config):
        out["styling"] = {"shape": shape}
    return out


def _build_partition(title: str, config: dict[str, Any], query: str, *, api_type: str) -> dict[str, Any]:
    metrics = _api_columns(config.get("metrics"), color="static_auto")
    metric = _api_column(config.get("metric"), color="static_auto")
    if not metrics and metric:
        metrics = [metric]
    if not metrics:
        metrics = [{"column": "value"}]
    out: dict[str, Any] = {
        "type": api_type,
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metrics": metrics,
    }
    groups = _api_columns(config.get("breakdowns"), color="mapping", collapse=True) or _api_columns(
        config.get("group_by"),
        color="mapping",
        collapse=True,
    )
    if not groups:
        single = _api_column(config.get("breakdown"), color="mapping", collapse=True)
        if single:
            groups = [single]
    if groups:
        out["group_by"] = groups
    if legend := _api_legend(config.get("legend"), kind=api_type):
        out["legend"] = legend
    if styling := _api_partition_styling(config.get("styling"), api_type=api_type):
        out["styling"] = styling
    return out


def _build_pie(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    return _build_partition(title, config, query, api_type="pie")


def _build_treemap(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    return _build_partition(title, config, query, api_type="treemap")


def _build_waffle(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    return _build_partition(title, config, query, api_type="waffle")


def _build_heatmap(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "heatmap",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "x": _first_api_column(config, "x_axis", "dimension") or {"column": "time_bucket"},
        "metric": _metric_column(config, color="dynamic_auto"),
    }
    y = _first_api_column(config, "y_axis", "breakdown", "breakdowns")
    if y:
        out["y"] = y
    if legend := _api_legend(config.get("legend"), kind="heatmap"):
        out["legend"] = legend
    if axis := _api_heatmap_axis(config.get("axis")):
        out["axis"] = axis
    return out


def _build_datatable(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "data_table",
        "title": title,
        "data_source": {"type": "esql", "query": query},
    }
    table_kwargs = {
        "color": "dynamic_mapping_auto",
        "table": True,
        "apply_color_to": {"value", "background", "badge"},
    }
    metrics = _api_columns(config.get("metrics"), summary=True, **table_kwargs)
    rows = _api_columns(config.get("breakdowns"), collapse=True, **table_kwargs) or _api_columns(
        config.get("dimensions"),
        collapse=True,
        **table_kwargs,
    )
    if not rows:
        single = _first_api_column(config, "dimension", "breakdown", collapse=True, **table_kwargs)
        if single:
            rows = [single]
    if metrics:
        out["metrics"] = metrics
    if rows:
        out["rows"] = rows
    split_metrics_by = _api_columns(config.get("split_metrics_by"))
    if split_metrics_by:
        out["split_metrics_by"] = split_metrics_by
    if styling := _api_datatable_styling(config.get("styling")):
        out["styling"] = styling
    # data_table requires at least one of metrics/rows to render.
    if not metrics and not rows:
        out["rows"] = [{"column": "value"}]
    return out


def _build_tagcloud(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    return {
        "type": "tag_cloud",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metric": _metric_column(config),
        "tag_by": _first_api_column(config, "breakdown", "breakdowns", "tag_by") or {"column": "label"},
    }


def _build_mosaic(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "mosaic",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metric": _metric_column(config),
    }
    groups = _api_columns(config.get("breakdowns"), color="mapping", collapse=True) or _api_columns(
        config.get("group_by"),
        color="mapping",
        collapse=True,
    )
    if not groups:
        single = _api_column(config.get("breakdown"), color="mapping", collapse=True)
        if single:
            groups = [single]
    if groups:
        out["group_by"] = [groups[0]]
    if legend := _api_legend(config.get("legend"), kind="mosaic"):
        out["legend"] = legend
    if styling := _api_partition_styling(config.get("styling"), api_type="mosaic"):
        out["styling"] = styling
    return out


def _build_region_map(title: str, config: dict[str, Any], query: str) -> dict[str, Any]:
    return {
        "type": "region_map",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metric": _metric_column(config),
        "region": _first_api_column(config, "region", "breakdown", "breakdowns") or {"column": "region"},
    }


# ``config.type`` (as emitted in the migration YAML/IR) -> API config builder.
# ``datatable`` -> API ``data_table`` and ``tagcloud`` -> API ``tag_cloud``; the
# aliases keep both spellings acceptable.
_ESQL_CONFIG_BUILDERS: dict[str, Callable[[str, dict[str, Any], str], dict[str, Any]]] = {
    "line": _build_xy,
    "bar": _build_xy,
    "area": _build_xy,
    "metric": _build_metric,
    "gauge": _build_gauge,
    "pie": _build_pie,
    "treemap": _build_treemap,
    "waffle": _build_waffle,
    "heatmap": _build_heatmap,
    "datatable": _build_datatable,
    "data_table": _build_datatable,
    "tagcloud": _build_tagcloud,
    "tag_cloud": _build_tagcloud,
    "mosaic": _build_mosaic,
    "region_map": _build_region_map,
    "regionmap": _build_region_map,
}

# All 11 chart families the engine emits have an ES|QL Dashboards API variant.
_SUPPORTED_ESQL_TYPES = set(_ESQL_CONFIG_BUILDERS)


def _api_panel_from_esql(
    dashboard: str,
    panel: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[Finding]]:
    title = str(panel.get("title") or "")
    chart_type = str(config.get("type") or "").lower()
    query = str(config.get("query") or "").strip()
    if not query:
        return None, [
            Finding("missing_query", "error", dashboard, title, "ES|QL panel has no query")
        ]
    builder = _ESQL_CONFIG_BUILDERS.get(chart_type)
    if builder is None:
        return None, [
            Finding(
                "unsupported_by_api_oracle",
                "info",
                dashboard,
                title,
                f"ES|QL type '{chart_type}' is not yet mapped by the Dashboards API oracle",
            )
        ]
    return {
        "grid": _layout(panel),
        "type": "vis",
        "config": builder(title, config, query),
    }, []


def api_panel_from_report_panel(
    dashboard: str, panel: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[Finding]]:
    title = str(panel.get("title") or "")
    kind, config = _visual_presentation(panel)
    visual_ir = panel.get("visual_ir") if isinstance(panel.get("visual_ir"), dict) else {}
    has_standard_presentation = isinstance(visual_ir.get("presentation"), dict)
    if has_standard_presentation and kind in {"markdown", "esql", "links", "image"}:
        production_result = _production_map_panel(panel)
        if production_result.api_panel is not None:
            return production_result.api_panel, []
        if production_result.reason == "esql panel has no query":
            return None, [
                Finding("missing_query", "error", dashboard, title, "ES|QL panel has no query")
            ]
        return None, [
            Finding(
                "unsupported_by_api_oracle",
                "info",
                dashboard,
                title,
                production_result.reason or f"visual presentation kind '{kind or '(none)'}' is not mapped",
            )
        ]
    if kind == "markdown":
        return {
            "grid": _layout(panel),
            "type": "markdown",
            "config": {
                "title": title,
                "content": str(config.get("content") or ""),
                "settings": {},
            },
        }, []
    if kind == "esql":
        return _api_panel_from_esql(dashboard, panel, config)
    return None, [
        Finding(
            "unsupported_by_api_oracle",
            "info",
            dashboard,
            title,
            f"visual presentation kind '{kind or '(none)'}' is not mapped",
        )
    ]


def build_dashboard_payload(report: dict[str, Any]) -> tuple[dict[str, Any], list[Finding]]:
    panels: list[dict[str, Any]] = []
    findings: list[Finding] = []
    filters: list[dict[str, Any]] = []
    title = ""
    for dash in report.get("dashboards", []):
        title = title or str(dash.get("title") or "migration conformance")
        dashboard_title = str(dash.get("title") or "")
        # Include dashboard-level filters (mapped through the production mapper)
        # so the live conformance submit actually exercises the filter shape --
        # otherwise a filter block Kibana rejects would never be part of the
        # validated payload and the gate could not catch it.
        mapped_filters, _dropped = production_dashboards_api.map_yaml_filters(dash.get("filters"))
        filters.extend(mapped_filters)
        for panel in dash.get("panels", []):
            if not isinstance(panel, dict):
                continue
            api_panel, panel_findings = api_panel_from_report_panel(dashboard_title, panel)
            findings.extend(panel_findings)
            if api_panel is not None:
                panels.append(api_panel)
    payload: dict[str, Any] = {"title": f"vf-conformance-{title}", "panels": panels}
    if filters:
        payload["filters"] = filters
    return payload, findings


def mapped_panel_count(payload: dict[str, Any]) -> int:
    panels = payload.get("panels")
    return len(panels) if isinstance(panels, list) else 0


def make_kibana_api_call(kibana_url: str, api_key: str) -> ApiCall:
    base = kibana_url.rstrip("/")
    headers = {
        "kbn-xsrf": "true",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"

    def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | str]:
        response = requests.request(
            method, f"{base}{path}", headers=headers, json=body, timeout=30
        )
        try:
            payload: dict[str, Any] | str = response.json()
        except ValueError:
            payload = response.text[:2000]
        return response.status_code, payload

    return call


def validate_payload(
    payload: dict[str, Any],
    *,
    api_call: ApiCall,
    delete_on_success: bool = True,
) -> list[Finding]:
    if not payload.get("panels"):
        return [
            Finding(
                "empty_payload",
                "warning",
                str(payload.get("title") or ""),
                "",
                "no panels were mapped into the typed Dashboards API payload",
            )
        ]
    status, body = api_call("POST", "/api/dashboards", payload)
    if 200 <= status < 300 and isinstance(body, dict):
        dash_id = body.get("id")
        if delete_on_success and dash_id:
            api_call("DELETE", f"/api/dashboards/{dash_id}", None)
        return []
    message = body if isinstance(body, str) else body.get("message", json.dumps(body))
    return [
        Finding(
            "dashboards_api_rejected",
            "error",
            str(payload.get("title") or ""),
            "",
            str(message),
            evidence={"status": status},
        )
    ]


def _panel_title(api_panel: dict[str, Any]) -> str:
    config_value = api_panel.get("config")
    config = config_value if isinstance(config_value, dict) else {}
    return str(config.get("title") or "")


def validate_payload_per_panel(
    payload: dict[str, Any],
    *,
    api_call: ApiCall,
    delete_on_success: bool = True,
) -> list[Finding]:
    """Validate each mapped panel in its own scratch dashboard.

    The typed Dashboards API reports schema paths such as ``panels.0``; on a
    large dashboard that is not enough context for triage. Per-panel mode trades
    speed for precise attribution.
    """
    panels = payload.get("panels") if isinstance(payload.get("panels"), list) else []
    if not panels:
        return validate_payload(payload, api_call=api_call, delete_on_success=delete_on_success)
    findings: list[Finding] = []
    for idx, panel in enumerate(panels):
        title = _panel_title(panel)
        panel_payload = {
            "title": f"{payload.get('title') or 'vf-conformance'}-panel-{idx}",
            "panels": [panel],
        }
        status, body = api_call("POST", "/api/dashboards", panel_payload)
        if 200 <= status < 300 and isinstance(body, dict):
            dash_id = body.get("id")
            if delete_on_success and dash_id:
                api_call("DELETE", f"/api/dashboards/{dash_id}", None)
            continue
        message = body if isinstance(body, str) else body.get("message", json.dumps(body))
        findings.append(
            Finding(
                "dashboards_api_rejected",
                "error",
                str(payload.get("title") or ""),
                title,
                str(message),
                evidence={"status": status, "panel_index": idx},
            )
        )
    return findings


def validate_report(
    report: dict[str, Any],
    *,
    api_call: ApiCall,
    delete_on_success: bool = True,
    per_panel: bool = False,
) -> list[Finding]:
    payload, findings = build_dashboard_payload(report)
    validator = validate_payload_per_panel if per_panel else validate_payload
    findings.extend(validator(payload, api_call=api_call, delete_on_success=delete_on_success))
    return findings


def apply_coverage_budget(
    findings: list[Finding],
    *,
    mapped_panels: int,
    max_unsupported: int | None = None,
    min_mapped_panels: int | None = None,
) -> list[Finding]:
    budget_findings: list[Finding] = []
    unsupported = sum(1 for finding in findings if finding.category == "unsupported_by_api_oracle")
    if max_unsupported is not None and unsupported > max_unsupported:
        budget_findings.append(
            Finding(
                "unsupported_budget_exceeded",
                "error",
                "",
                "",
                f"unsupported panel count {unsupported} exceeds budget {max_unsupported}",
                evidence={"unsupported": unsupported, "max_unsupported": max_unsupported},
            )
        )
    if min_mapped_panels is not None and mapped_panels < min_mapped_panels:
        budget_findings.append(
            Finding(
                "mapped_panel_budget_not_met",
                "error",
                "",
                "",
                f"mapped panel count {mapped_panels} is below required minimum {min_mapped_panels}",
                evidence={"mapped_panels": mapped_panels, "min_mapped_panels": min_mapped_panels},
            )
        )
    return [*findings, *budget_findings]


def summarize(findings: list[Finding], *, mapped_panels: int = 0) -> dict[str, Any]:
    counts: dict[str, int] = {}
    errors = 0
    for finding in findings:
        counts[finding.category] = counts.get(finding.category, 0) + 1
        if finding.severity == "error":
            errors += 1
    return {
        "total": len(findings),
        "errors": errors,
        "mapped_panels": mapped_panels,
        "unsupported": counts.get("unsupported_by_api_oracle", 0),
        "by_category": counts,
    }


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verifier.dashboards_api",
        description="Validate migrated dashboards against Kibana's typed Dashboards API.",
    )
    parser.add_argument("--migration-out", type=Path, required=True)
    parser.add_argument("--kibana-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-error", action="store_true")
    parser.add_argument("--max-unsupported", type=int)
    parser.add_argument("--min-mapped-panels", type=int)
    parser.add_argument(
        "--per-panel",
        action="store_true",
        help="Validate each mapped panel in an isolated scratch dashboard for precise failures.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    report = json.loads((args.migration_out / "migration_report.json").read_text())
    payload, findings = build_dashboard_payload(report)
    mapped = mapped_panel_count(payload)
    validator = validate_payload_per_panel if args.per_panel else validate_payload
    findings.extend(validator(payload, api_call=make_kibana_api_call(args.kibana_url, args.api_key)))
    findings = apply_coverage_budget(
        findings,
        mapped_panels=mapped,
        max_unsupported=args.max_unsupported,
        min_mapped_panels=args.min_mapped_panels,
    )
    payload = {"summary": summarize(findings, mapped_panels=mapped), "findings": [f.to_jsonable() for f in findings]}
    if args.output:
        args.output.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["summary"], indent=2))
    return 1 if args.fail_on_error and payload["summary"]["errors"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

