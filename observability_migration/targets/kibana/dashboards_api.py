# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Native Kibana Dashboards API target (typed, ES|QL-first).

This is an alternative to the ``kb-dashboard-cli`` compile+``_import`` path.
The schema authority for this module is the typed Kibana Dashboards API
itself — see ``observability_migration.core.assets.native_dashboard`` for the
:class:`NativeDashboard`/:class:`NativePanel`/:class:`NativeSection`/
:class:`NativeControl` IR that mirrors that API's own payload shape (``panels``
/ ``pinned_panels`` / ``grid`` / ``config``, plus native API item caps). YAML and
the flat migration report are *bridge inputs* into that IR, not the schema
itself; neither one decides what the native API accepts.

Why this exists
---------------
``kb-dashboard-cli`` compiles YAML into legacy stringified ``panelsJSON`` saved
objects and uploads them through ``POST /api/saved_objects/_import``. That path
accepts blobs the typed UI contract would reject, so "compiled" is not the same
as "valid". The Dashboards API validates structurally at write time.

Coverage (verified live against Elastic Serverless 9.5.0)
---------------------------------------------------------
Every visualization type the migration engine emits has an ES|QL variant on the
API. The ``vis.config.type`` discriminator accepts exactly:

    metric, legacy_metric, xy, gauge, heatmap, tag_cloud, region_map,
    data_table, pie, mosaic, treemap, waffle

All of the above except ``legacy_metric`` accept an ``esql`` ``data_source``.
The column references in each chart's config are the ES|QL *output column*
names — exactly what ``visual_ir.presentation.config`` already records as
``field``.

Two entry points
----------------
* **Report-based** (``native_dashboard_from_report`` / ``build_dashboard_payload``
  / ``upload_report``): maps the *flat* ``migration_report.json`` panels via
  ``visual_ir.presentation``. The flat report carries no controls and no
  nested sections.
* **YAML-based** (``native_dashboard_from_yaml`` / ``build_payload_from_yaml``
  / ``upload_yaml_files``): maps the canonical kb-dashboard-core **YAML** —
  the richest bridge input. It reconstructs nested ``section`` blocks into
  native API sections and dashboard-level ``controls`` into native API
  ``pinned_panels`` (ES|QL controls), on top of the same 11 chart types +
  markdown. Prefer this path.

Both entry points build a :class:`NativeDashboard` first and then call
``NativeDashboard.to_api_payload()`` for the actual JSON body — the
``build_dashboard_payload*`` functions are thin, backward-compatible wrappers
around that IR, kept so existing callers do not need the object form.

Scope of this module
--------------------
Structural mapping + deploy with per-panel fallback classification. Display
polish (axis titles, legends, number formats, gauge thresholds) is intentionally
minimal in this first cut; those are additive and never change acceptance.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests
import yaml

from observability_migration.core.assets.native_dashboard import (
    MAX_DASHBOARD_ITEMS,
    MAX_PINNED_CONTROLS,
    MAX_SECTION_PANELS,
    MAX_TOTAL_ITEMS,
    NativeControl,
    NativeDashboard,
    NativeGrid,
    NativeMappingCounts,
    NativePanel,
    NativeSection,
    dashboard_item_count,
    dashboard_leaf_panel_count,
)
from observability_migration.core.http import apply_tls
from observability_migration.targets.kibana.compile import kibana_url_for_space

# Current typed Dashboards API caps. The API schema is still preview and its
# full reference is externally hosted, so keep these in sync with
# ``scripts/fetch_dashboards_api_schema.py`` and Elastic's Dashboards API docs.
_MAX_DASHBOARD_ITEMS = MAX_DASHBOARD_ITEMS
_MAX_SECTION_PANELS = MAX_SECTION_PANELS
_MAX_PINNED_CONTROLS = MAX_PINNED_CONTROLS
_MAX_TOTAL_ITEMS = MAX_TOTAL_ITEMS

# Partition charts (pie/treemap/waffle) reject a ``group_by`` with more than 3
# non-collapsed dimensions: "The number of non-collapsed group_by dimensions
# must not exceed 3". Sources with wide multi-tag breakdowns (e.g. a Datadog
# widget grouped by 9 tags) must be truncated rather than rejected outright.
_MAX_PARTITION_GROUP_BY = 3

# ES|QL chart config ``type`` values accepted by the Dashboards API vis panel.
# Verified live on Serverless 9.5.0; only ``legacy_metric`` lacks an ES|QL
# variant. ``line``/``bar``/``area`` are emitted by the engine but collapse into
# a single API ``xy`` panel with per-layer series types.
_XY_KINDS = {"line", "bar", "area"}
_ESQL_VIS_TYPES = {
    "metric",
    "gauge",
    "pie",
    "heatmap",
    "treemap",
    "datatable",
    "tag_cloud",
    "waffle",
    "mosaic",
    "region_map",
} | _XY_KINDS

# YAML/visual-IR chart ``type`` -> API ``xy`` layer series ``type``. The engine
# records stacking via a separate ``mode`` key, folded in by ``_xy_series_type``.
_XY_SERIES_BASE = {"line": "line", "bar": "bar", "area": "area"}
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")
_COLLAPSE_BY = {"avg", "sum", "max", "min"}
_ALIGNMENTS = {"left", "center", "right"}
_APPLY_COLOR_TO = {"value", "background", "badge"}
_SUMMARY_TYPES = {"sum", "avg", "count", "min", "max"}


@dataclass
class PanelMapping:
    """Result of mapping one report panel to the typed API."""

    api_panel: dict[str, Any] | None
    reason: str = ""
    kind: str = ""


@dataclass
class UploadResult:
    dashboard: str
    dashboard_id: str = ""
    status: str = ""  # created | rejected | empty
    mapped: int = 0
    unmapped: int = 0
    http_status: int = 0
    message: str = ""
    unmapped_reasons: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Column helpers
# --------------------------------------------------------------------------- #

def _keep_keys(obj: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: obj[key] for key in keys if key in obj and obj[key] is not None}


def _column(obj: Any) -> dict[str, Any] | None:
    """A single ``{"column": <esql output column>}`` reference.

    ``visual_ir.presentation.config`` records the column as ``field`` (the name
    the emitted ES|QL query actually produces); the API calls it ``column``.
    """
    if not isinstance(obj, dict):
        return None
    name = obj.get("field") or obj.get("column")
    if not name:
        return None
    return {"column": str(name)}


def _api_format(fmt: Any) -> dict[str, Any] | None:
    """Return only field-format objects accepted by the Dashboards API."""
    if not isinstance(fmt, dict):
        return None
    fmt_type = str(fmt.get("type") or "")
    if fmt_type in {"number", "percent"}:
        out = {"type": fmt_type}
        out.update(_keep_keys(fmt, ("decimals", "suffix", "compact")))
        return out
    if fmt_type in {"bytes", "bits"}:
        out = {"type": fmt_type}
        out.update(_keep_keys(fmt, ("decimals", "suffix")))
        return out
    if fmt_type == "duration":
        # Verified live on Serverless 9.5.0: the single-value metric/gauge
        # format schema accepts a bare ``{type: duration}``, but the
        # multi-column schema (xy/data_table/heatmap/etc. row and series
        # values) *requires* ``from``/``to`` as well — omitting them 400s with
        # "expected value of type [string] but got [undefined]" on
        # ``.format.from``. Both schemas accept ``from``/``to`` together
        # harmlessly, so always emit the pair rather than only forwarding an
        # already-complete pair — that previously let an incomplete/absent
        # pair silently through to the multi-column schema and got the whole
        # panel rejected.
        #
        # Defaults are not arbitrary: they're Kibana's own DurationFormat
        # defaults (src/platform/plugins/shared/field_formats/common/
        # constants/duration_formats.ts — DEFAULT_DURATION_INPUT_FORMAT.kind
        # == "seconds", DEFAULT_DURATION_OUTPUT_FORMAT.method == "humanize"),
        # which the Lens-as-code schema's ``from``/``to`` pass straight
        # through into as ``fromUnit``/``toUnit``
        # (kbn-lens-embeddable-utils/config_builder/transforms/columns/
        # format.ts). So an unspecified duration format renders exactly like
        # Kibana's own default "Duration" value-format selection would.
        out = {"type": fmt_type}
        out.update(_keep_keys(fmt, ("decimals", "suffix", "compact")))
        out["from"] = str(fmt["from"]) if fmt.get("from") else "seconds"
        out["to"] = str(fmt["to"]) if fmt.get("to") else "humanize"
        return out
    if fmt_type == "custom" and fmt.get("pattern"):
        return {"type": fmt_type, "pattern": str(fmt["pattern"])}
    return None


def _api_color_mapping(color: dict[str, Any]) -> dict[str, Any] | None:
    mode = str(color.get("mode") or "")
    palette = color.get("palette")
    if mode == "gradient" and isinstance(palette, str) and palette:
        out: dict[str, Any] = {"mode": mode, "palette": palette}
        for key in ("gradient", "mapping", "sort", "unassigned"):
            if key in color:
                out[key] = color[key]
        return out
    if mode != "categorical" or not isinstance(palette, str) or not palette:
        return None
    mapping: list[dict[str, Any]] = []
    for item in color.get("mapping") or []:
        if not isinstance(item, dict) or not isinstance(item.get("values"), list) or "color" not in item:
            continue
        entry = {"values": item["values"], "color": item["color"]}
        mapping.append(entry)
    if not mapping:
        return None
    out = {"mode": mode, "palette": palette, "mapping": mapping}
    if "unassigned" in color:
        out["unassigned"] = color["unassigned"]
    return out


def _api_color(color: Any) -> dict[str, Any] | None:
    """Return API-shaped color configs, dropping incomplete legacy thresholds."""
    if isinstance(color, str) and _HEX_COLOR_RE.match(color):
        return {"type": "static", "color": color}
    if not isinstance(color, dict):
        return None
    mapped = _api_color_mapping(color)
    if mapped:
        return mapped
    legacy_thresholds = color.get("thresholds")
    if isinstance(legacy_thresholds, list):
        legacy_steps: list[dict[str, Any]] = []
        lower = color.get("range_min")
        valid_thresholds = [
            item for item in legacy_thresholds
            if isinstance(item, dict) and isinstance(item.get("color"), str)
        ]
        for index, item in enumerate(valid_thresholds):
            if not isinstance(item, dict) or not isinstance(item.get("color"), str):
                continue
            upper = item.get("up_to")
            step: dict[str, Any] = {"color": item["color"]}
            if isinstance(lower, int | float):
                step["gte"] = lower
            if isinstance(upper, int | float):
                if index + 1 < len(valid_thresholds):
                    step["lt"] = upper
                lower = upper
            if {"gte", "lt"} & set(step):
                legacy_steps.append(step)
        if legacy_steps:
            return {"type": "dynamic", "range": "absolute", "steps": legacy_steps}
    color_type = str(color.get("type") or "")
    if color_type in {"auto", "none"}:
        return {"type": color_type}
    if color_type == "static" and isinstance(color.get("color"), str):
        return {"type": color_type, "color": color["color"]}
    if color_type != "dynamic" or color.get("range") not in {"absolute", "percentage"}:
        return None
    steps: list[dict[str, Any]] = []
    for step in color.get("steps") or []:
        if not isinstance(step, dict) or not isinstance(step.get("color"), str):
            continue
        out_step = _keep_keys(step, ("gte", "lt", "lte", "color"))
        if {"gte", "lt", "lte"} & set(out_step):
            steps.append(out_step)
    if not steps:
        return None
    return {"type": color_type, "range": color["range"], "steps": steps}


# Roles whose API schema exposes NO ``color`` property at all (emitting one
# is rejected with "Additional properties are not allowed"). Verified live on
# Serverless 9.5.0: mosaic ``metric`` and metric ``breakdown_by`` have no color.
_NO_COLOR_ROLES = {
    "x", "datatable_split", "region", "region_metric", "tag_metric",
    "gauge_bound", "mosaic_metric", "metric_breakdown",
}
# Roles whose color must be a categorical/gradient ``colorMapping`` (a
# ``{type: auto|dynamic|static}`` object is rejected there).
_MAPPING_ONLY_ROLES = {"breakdown", "partition_group", "tag_by", "datatable_row"}
# Per-metric-role allow-lists of ``color.type`` values, verified live: each
# chart's metric color schema is narrower than the union of all color shapes.
_COLOR_TYPES_BY_ROLE = {
    "xy_y": {"static", "auto"},
    "gauge_metric": {"dynamic", "none", "auto"},
    "secondary_metric": {"static", "none", "auto"},
    "partition_metric": {"static", "auto"},   # pie / treemap / waffle
    "heatmap_metric": {"dynamic", "auto"},
    "primary_metric": {"dynamic", "static", "auto"},
}


def _api_color_for_role(color: Any, role: str) -> dict[str, Any] | None:
    mapped = _api_color(color)
    if not mapped:
        return None
    color_type = mapped.get("type")
    mode = mapped.get("mode")
    if role in _NO_COLOR_ROLES:
        return None
    if role in _MAPPING_ONLY_ROLES:
        return mapped if mode in {"categorical", "gradient"} else None
    if role == "datatable_metric":
        # colorByValue (dynamic) | colorMapping (categorical/gradient) | auto — no static.
        return mapped if color_type in {"dynamic", "auto"} or mode in {"categorical", "gradient"} else None
    allowed = _COLOR_TYPES_BY_ROLE.get(role)
    if allowed is not None:
        return mapped if color_type in allowed else None
    # Unknown role: only the universally-safe shapes.
    return mapped if color_type in {"static", "auto"} else None


def _api_summary(summary: Any) -> dict[str, Any] | None:
    if not isinstance(summary, dict) or summary.get("type") not in _SUMMARY_TYPES:
        return None
    out = {"type": summary["type"]}
    if summary.get("label"):
        out["label"] = str(summary["label"])
    return out


def _api_column(obj: Any, role: str = "generic") -> dict[str, Any] | None:
    """Column reference plus only the optional keys valid for its API role."""
    col = _column(obj)
    if col is None or not isinstance(obj, dict):
        return col
    if obj.get("label"):
        col["label"] = str(obj["label"])
    fmt = _api_format(obj.get("format"))
    if fmt and role not in {"gauge_bound", "region"}:
        col["format"] = fmt
    color = _api_color_for_role(obj.get("color"), role)
    if color:
        col["color"] = color
    if role in {"primary_metric", "gauge_metric"} and obj.get("subtitle"):
        col["subtitle"] = str(obj["subtitle"])
    if role == "xy_y":
        axis = {"left": "y", "right": "y2", "y": "y", "y2": "y2"}.get(str(obj.get("axis") or ""))
        if axis:
            col["axis"] = axis
    if role in {"breakdown", "metric_breakdown", "partition_group", "datatable_row", "tag_by"}:
        collapse_by = obj.get("collapse_by") or obj.get("collapse")
        if collapse_by in _COLLAPSE_BY:
            col["collapse_by"] = collapse_by
    if role in {"datatable_metric", "datatable_row"}:
        if isinstance(obj.get("width"), int | float) and obj["width"] >= 0:
            col["width"] = obj["width"]
        if obj.get("alignment") in _ALIGNMENTS:
            col["alignment"] = obj["alignment"]
        if isinstance(obj.get("visible"), bool):
            col["visible"] = obj["visible"]
        if obj.get("apply_color_to") in _APPLY_COLOR_TO:
            col["apply_color_to"] = obj["apply_color_to"]
    if role == "datatable_row" and isinstance(obj.get("click_filter"), bool):
        col["click_filter"] = obj["click_filter"]
    if role == "datatable_metric":
        summary = _api_summary(obj.get("summary"))
        if summary:
            col["summary"] = summary
    if role == "gauge_metric":
        ticks = obj.get("ticks")
        if isinstance(ticks, dict):
            safe_ticks: dict[str, Any] = {}
            if isinstance(ticks.get("visible"), bool):
                safe_ticks["visible"] = ticks["visible"]
            if ticks.get("mode") in {"auto", "bands"}:
                safe_ticks["mode"] = ticks["mode"]
            if safe_ticks:
                col["ticks"] = safe_ticks
        gauge_title = obj.get("title")
        if isinstance(gauge_title, dict):
            safe_title: dict[str, Any] = {}
            if isinstance(gauge_title.get("text"), str):
                safe_title["text"] = gauge_title["text"]
            if isinstance(gauge_title.get("visible"), bool):
                safe_title["visible"] = gauge_title["visible"]
            if safe_title:
                col["title"] = safe_title
    return col


def _column_op(obj: Any) -> dict[str, Any] | None:
    """Column reference plus optional safe display metadata."""
    return _api_column(obj)


def _columns(items: Any, role: str = "generic") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(items, list):
        for item in items:
            col = _api_column(item, role=role)
            if col:
                out.append(col)
    return out


def _first_column(items: Any, role: str = "generic") -> dict[str, Any] | None:
    if isinstance(items, list):
        return (_columns(items, role=role) or [None])[0]
    return _api_column(items, role=role)


def _xy_series_type(cfg: dict[str, Any]) -> str:
    base = _XY_SERIES_BASE.get(str(cfg.get("type") or ""), "line")
    if base == "bar" and (cfg.get("orientation") == "horizontal" or cfg.get("horizontal") is True):
        base = "bar_horizontal"
    mode = str(cfg.get("mode") or "").lower()
    if base == "line":
        return base
    if mode in {"stacked", "stack"}:
        return f"{base}_stacked"
    if mode in {"percentage", "percent", "normalized"}:
        return f"{base}_percentage"
    return base


def _legend_visibility(value: Any) -> str | None:
    if isinstance(value, bool):
        return "visible" if value else "hidden"
    mapped = {"show": "visible", "visible": "visible", "hide": "hidden", "hidden": "hidden", "auto": "auto"}
    return mapped.get(str(value or "").lower())


def _truncate_config(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bool):
        return {"enabled": value}
    if isinstance(value, int | float) and 1 <= value <= 10:
        return {"enabled": True, "max_lines": value}
    return None


def _api_legend(legend: Any, kind: str) -> dict[str, Any] | None:
    if not isinstance(legend, dict):
        return None
    out: dict[str, Any] = {}
    visibility = _legend_visibility(legend.get("visibility", legend.get("visible")))
    if visibility:
        out["visibility"] = visibility
    truncate = _truncate_config(legend.get("truncate_labels", legend.get("truncate_after_lines")))
    if kind == "xy":
        position = str(legend.get("position") or "").lower()
        if position in {"left", "right", "top", "bottom"}:
            out["placement"] = "outside"
            out["position"] = position
        elif position in {"top_left", "top_right", "bottom_left", "bottom_right"}:
            out["placement"] = "inside"
            out["position"] = position
        if truncate:
            out["layout"] = {"type": "grid", "truncate": truncate}
        return out or None
    if truncate and "max_lines" in truncate:
        out["truncate_after_lines"] = truncate["max_lines"]
    if kind in {"pie", "treemap", "waffle", "mosaic"} and isinstance(legend.get("nested"), bool):
        out["nested"] = legend["nested"]
    return out or None


def _api_axis_title(title: Any) -> dict[str, Any] | None:
    if not isinstance(title, dict):
        return None
    out: dict[str, Any] = {}
    if title.get("text") is not None:
        out["text"] = str(title["text"])
    if isinstance(title.get("visible"), bool):
        out["visible"] = title["visible"]
    return out or None


def _yaml_axis_title(title: Any) -> dict[str, Any] | None:
    if isinstance(title, bool):
        return {"visible": title}
    if isinstance(title, str) and title:
        return {"text": title, "visible": True}
    if isinstance(title, dict):
        return _api_axis_title(title)
    return None


def _yaml_axis(axis: Any) -> dict[str, Any] | None:
    if not isinstance(axis, dict):
        return None
    out: dict[str, Any] = {}
    if axis.get("scale") in {"linear", "log", "sqrt", "ordinal", "temporal"}:
        out["scale"] = axis["scale"]
    title = _yaml_axis_title(axis.get("title"))
    if title:
        out["title"] = title
    extent = axis.get("extent")
    if isinstance(extent, dict):
        safe_extent = _keep_keys(extent, ("mode", "min", "max", "nice_values", "enforce"))
        if safe_extent:
            out["extent"] = safe_extent
    return out or None


def _api_xy_axis(axis: Any) -> dict[str, Any] | None:
    if not isinstance(axis, dict):
        return None
    out: dict[str, Any] = {}
    for name in ("x", "y", "y2"):
        raw = axis.get(name)
        if not isinstance(raw, dict):
            continue
        item: dict[str, Any] = {}
        scale = raw.get("scale")
        if (name == "x" and scale in {"ordinal", "temporal", "linear"}) or (
            name in {"y", "y2"} and scale in {"linear", "log", "sqrt"}
        ):
            item["scale"] = scale
        title = _api_axis_title(raw.get("title"))
        if title:
            item["title"] = title
        domain = raw.get("domain")
        extent = raw.get("extent")
        if isinstance(domain, dict):
            domain_type = domain.get("type")
            if domain_type in {"fit", "full"}:
                item["domain"] = {"type": domain_type}
            elif (
                domain_type == "custom"
                and isinstance(domain.get("min"), int | float)
                and isinstance(domain.get("max"), int | float)
                and not isinstance(domain.get("min"), bool)
                and not isinstance(domain.get("max"), bool)
            ):
                item["domain"] = {"type": "custom", "min": domain["min"], "max": domain["max"]}
        elif isinstance(extent, dict):
            mode = extent.get("mode")
            if mode == "custom" and isinstance(extent.get("min"), int | float) and isinstance(extent.get("max"), int | float):
                item["domain"] = {"type": "custom", "min": extent["min"], "max": extent["max"]}
        for key in ("grid", "ticks"):
            if isinstance(raw.get(key), dict) and isinstance(raw[key].get("visible"), bool):
                item[key] = {"visible": raw[key]["visible"]}
        labels = raw.get("labels")
        if isinstance(labels, dict) and labels.get("orientation") in {"horizontal", "vertical", "angled"}:
            item["labels"] = {"orientation": labels["orientation"]}
        if item:
            out[name] = item
    return out or None


def _api_heatmap_axis(axis: Any) -> dict[str, Any] | None:
    if not isinstance(axis, dict):
        return None
    out: dict[str, Any] = {}
    x = axis.get("x")
    if isinstance(x, dict) and x.get("scale") in {"ordinal", "temporal", "linear"}:
        item: dict[str, Any] = {"scale": x["scale"]}
        title = _api_axis_title(x.get("title"))
        if title:
            item["title"] = title
        labels = x.get("labels")
        if isinstance(labels, dict):
            label_cfg: dict[str, Any] = {}
            if labels.get("orientation") in {"horizontal", "vertical", "angled"}:
                label_cfg["orientation"] = labels["orientation"]
            if isinstance(labels.get("visible"), bool):
                label_cfg["visible"] = labels["visible"]
            if label_cfg:
                item["labels"] = label_cfg
        if x.get("sort") in {"asc", "desc"}:
            item["sort"] = x["sort"]
        out["x"] = item
    y = axis.get("y")
    if isinstance(y, dict):
        item = {}
        title = _api_axis_title(y.get("title"))
        if title:
            item["title"] = title
        labels = y.get("labels")
        if isinstance(labels, dict) and isinstance(labels.get("visible"), bool):
            item["labels"] = {"visible": labels["visible"]}
        if y.get("sort") in {"asc", "desc"}:
            item["sort"] = y["sort"]
        if item:
            out["y"] = item
    return out or None


def _cfg_axis_source(cfg: dict[str, Any]) -> Any:
    raw_appearance = cfg.get("appearance")
    appearance: dict[str, Any] = raw_appearance if isinstance(raw_appearance, dict) else {}
    axis = cfg.get("axis") or cfg.get("axes") or appearance.get("axis") or appearance.get("axes")
    if axis:
        return axis
    schema_axis: dict[str, Any] = {}
    for yaml_key, api_key in (("x_axis", "x"), ("y_left_axis", "y"), ("y_right_axis", "y2")):
        converted = _yaml_axis(appearance.get(yaml_key))
        if converted:
            schema_axis[api_key] = converted
    return schema_axis or None


def _api_gauge_shape(value: Any) -> dict[str, Any] | None:
    raw = value.get("type") if isinstance(value, dict) else value
    shape = str(raw or "")
    if shape in {"arc", "circle", "semi_circle"}:
        return {"type": shape}
    if shape in {"horizontal_bullet", "vertical_bullet", "bullet"}:
        out = {"type": "bullet"}
        if shape.startswith("horizontal"):
            out["orientation"] = "horizontal"
        elif shape.startswith("vertical"):
            out["orientation"] = "vertical"
        elif isinstance(value, dict) and value.get("orientation") in {"horizontal", "vertical"}:
            out["orientation"] = value["orientation"]
        return out
    return None


def _api_metric_styling(styling: Any) -> dict[str, Any] | None:
    if not isinstance(styling, dict):
        return None
    out = {key: styling[key] for key in ("primary", "secondary", "icon") if isinstance(styling.get(key), dict)}
    return out or None


def _api_heatmap_styling(styling: Any) -> dict[str, Any] | None:
    if not isinstance(styling, dict):
        return None
    cells = styling.get("cells")
    if not isinstance(cells, dict):
        return None
    labels = cells.get("labels")
    if isinstance(labels, dict) and isinstance(labels.get("visible"), bool):
        return {"cells": {"labels": {"visible": labels["visible"]}}}
    return None


def _api_partition_styling(styling: Any, kind: str) -> dict[str, Any] | None:
    if not isinstance(styling, dict):
        return None
    out: dict[str, Any] = {}
    values = styling.get("values")
    if kind in {"pie", "treemap", "waffle", "mosaic"} and isinstance(values, dict):
        item: dict[str, Any] = {}
        if isinstance(values.get("visible"), bool):
            item["visible"] = values["visible"]
        if values.get("mode") in {"absolute", "percentage"}:
            item["mode"] = values["mode"]
        if isinstance(values.get("percent_decimals"), int | float) and 0 <= values["percent_decimals"] <= 10:
            item["percent_decimals"] = values["percent_decimals"]
        if item:
            out["values"] = item
    if kind == "pie":
        if styling.get("donut_hole") in {"none", "s", "m", "l"}:
            out["donut_hole"] = styling["donut_hole"]
        labels = styling.get("labels")
        if isinstance(labels, dict):
            label_cfg: dict[str, Any] = {}
            if isinstance(labels.get("visible"), bool):
                label_cfg["visible"] = labels["visible"]
            if labels.get("position") in {"inside", "outside"}:
                label_cfg["position"] = labels["position"]
            if label_cfg:
                out["labels"] = label_cfg
    if kind == "tag_cloud":
        for key in ("caption", "font_size"):
            if isinstance(styling.get(key), dict):
                out[key] = styling[key]
        if styling.get("orientation") in {"horizontal", "vertical", "angled"}:
            out["orientation"] = styling["orientation"]
    return out or None


# --------------------------------------------------------------------------- #
# Per-type config builders (esql presentation config -> API vis config)
# --------------------------------------------------------------------------- #

def _cfg_xy(title: str, cfg: dict[str, Any], query: str) -> dict[str, Any]:
    layer: dict[str, Any] = {
        "type": _xy_series_type(cfg),
        "data_source": {"type": "esql", "query": query},
    }
    x = _api_column(cfg.get("dimension"), role="x")
    if x:
        layer["x"] = x
    y = _columns(cfg.get("metrics"), role="xy_y")
    layer["y"] = y or [{"column": "value"}]
    bd = _api_column(cfg.get("breakdown"), role="breakdown") or _first_column(cfg.get("breakdowns"), role="breakdown")
    if bd:
        layer["breakdown_by"] = bd
    out: dict[str, Any] = {"type": "xy", "title": title, "layers": [layer]}
    legend = _api_legend(cfg.get("legend"), kind="xy")
    if legend:
        out["legend"] = legend
    axis = _api_xy_axis(_cfg_axis_source(cfg))
    if axis:
        out["axis"] = axis
    styling_source = cfg.get("styling")
    raw_appearance = cfg.get("appearance")
    appearance: dict[str, Any] = raw_appearance if isinstance(raw_appearance, dict) else {}
    styling = styling_source if isinstance(styling_source, dict) else appearance.get("styling")
    if isinstance(styling, dict):
        safe_styling = {key: styling[key] for key in ("areas", "bars", "fitting", "interpolation", "overlays", "points") if key in styling}
        if safe_styling:
            out["styling"] = safe_styling
    return out


def _cfg_metric(title: str, cfg: dict[str, Any], query: str) -> dict[str, Any]:
    primary = _api_column(cfg.get("primary"), role="primary_metric")
    if primary is None and isinstance(cfg.get("metrics"), list):
        candidates = [item for item in cfg["metrics"] if not isinstance(item, dict) or item.get("type") != "secondary"]
        primary = _first_column(candidates, role="primary_metric")
    primary = primary or {"column": "value"}
    metrics: list[dict[str, Any]] = [{"type": "primary", **primary}]
    secondary = _api_column(cfg.get("secondary"), role="secondary_metric")
    if secondary is None and isinstance(cfg.get("metrics"), list):
        secondary = _first_column([item for item in cfg["metrics"] if isinstance(item, dict) and item.get("type") == "secondary"], role="secondary_metric")
    if secondary:
        metrics.append({"type": "secondary", **secondary})
    out: dict[str, Any] = {
        "type": "metric",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metrics": metrics,
    }
    breakdown = (
        _api_column(cfg.get("breakdown"), role="metric_breakdown")
        or _first_column(cfg.get("breakdowns"), role="metric_breakdown")
        or _first_column(cfg.get("breakdown_by"), role="metric_breakdown")
    )
    if breakdown:
        out["breakdown_by"] = breakdown
    styling = _api_metric_styling(cfg.get("styling"))
    if styling:
        out["styling"] = styling
    return out


def _cfg_gauge(title: str, cfg: dict[str, Any], query: str) -> dict[str, Any]:
    metric = _api_column(cfg.get("metric"), role="gauge_metric") or (_columns(cfg.get("metrics"), role="gauge_metric") or [{"column": "value"}])[0]
    if "color" not in metric:
        top_color = _api_color_for_role(cfg.get("color"), "gauge_metric")
        if top_color:
            metric["color"] = top_color
    for yaml_key, api_key in (("minimum", "min"), ("maximum", "max"), ("goal", "goal")):
        bound = _api_column(cfg.get(yaml_key), role="gauge_bound")
        if bound:
            metric[api_key] = bound
    out: dict[str, Any] = {
        "type": "gauge",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metric": metric,
    }
    appearance = cfg.get("appearance")
    styling = cfg.get("styling") if isinstance(cfg.get("styling"), dict) else {}
    shape = _api_gauge_shape(styling.get("shape") if isinstance(styling, dict) and styling.get("shape") else (appearance or {}).get("shape") if isinstance(appearance, dict) else None)
    if shape:
        out["styling"] = {"shape": shape}
    return out


def _cfg_pie(title: str, cfg: dict[str, Any], query: str, api_type: str = "pie") -> dict[str, Any]:
    metrics = _columns(cfg.get("metrics"), role="partition_metric")
    metric = _api_column(cfg.get("metric"), role="partition_metric")
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
    group_by = _columns(cfg.get("breakdowns"), role="partition_group") or _columns(cfg.get("group_by"), role="partition_group")
    single = _api_column(cfg.get("breakdown"), role="partition_group")
    if not group_by and single:
        group_by = [single]
    if group_by:
        out["group_by"] = group_by[:_MAX_PARTITION_GROUP_BY]
    legend = _api_legend(cfg.get("legend"), kind=api_type)
    if legend:
        out["legend"] = legend
    styling = _api_partition_styling(cfg.get("styling"), kind=api_type)
    if styling:
        out["styling"] = styling
    return out


def _cfg_heatmap(title: str, cfg: dict[str, Any], query: str) -> dict[str, Any]:
    x = _api_column(cfg.get("x_axis"), role="x") or _api_column(cfg.get("dimension"), role="x") or {"column": "time_bucket"}
    metric = _api_column(cfg.get("metric"), role="heatmap_metric") or (_columns(cfg.get("metrics"), role="heatmap_metric") or [{"column": "value"}])[0]
    out: dict[str, Any] = {
        "type": "heatmap",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "x": x,
        "metric": metric,
    }
    y = _api_column(cfg.get("y_axis"), role="breakdown") or _api_column(cfg.get("breakdown"), role="breakdown")
    if y:
        out["y"] = y
    appearance = cfg.get("appearance")
    if not isinstance(appearance, dict):
        appearance = {}
    # Datadog emits heatmap legend under ``appearance.legend``; accept either.
    legend = _api_legend(cfg.get("legend"), kind="heatmap") or _api_legend(appearance.get("legend"), kind="heatmap")
    if legend:
        out["legend"] = legend
    axis = _api_heatmap_axis(_cfg_axis_source(cfg))
    if axis:
        out["axis"] = axis
    styling = _api_heatmap_styling(cfg.get("styling")) or _api_heatmap_styling(appearance)
    if styling:
        out["styling"] = styling
    return out


def _cfg_datatable(title: str, cfg: dict[str, Any], query: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "data_table",
        "title": title,
        "data_source": {"type": "esql", "query": query},
    }
    metrics = _columns(cfg.get("metrics"), role="datatable_metric")
    rows = _columns(cfg.get("breakdowns"), role="datatable_row") or _columns(cfg.get("dimensions"), role="datatable_row")
    single_row = _api_column(cfg.get("dimension"), role="datatable_row") or _api_column(cfg.get("breakdown"), role="datatable_row")
    if not rows and single_row:
        rows = [single_row]
    if metrics:
        out["metrics"] = metrics
    if rows:
        out["rows"] = rows
    split_metrics_by = _columns(cfg.get("split_metrics_by"), role="datatable_split")
    if split_metrics_by:
        out["split_metrics_by"] = split_metrics_by
    if not metrics and not rows:
        # data_table needs at least one column reference to render.
        out["rows"] = [{"column": "value"}]
    return out


def _cfg_treemap(title: str, cfg: dict[str, Any], query: str) -> dict[str, Any]:
    return _cfg_pie(title, cfg, query, api_type="treemap")


def _cfg_waffle(title: str, cfg: dict[str, Any], query: str) -> dict[str, Any]:
    return _cfg_pie(title, cfg, query, api_type="waffle")


def _cfg_tagcloud(title: str, cfg: dict[str, Any], query: str) -> dict[str, Any]:
    metric = _api_column(cfg.get("metric"), role="tag_metric") or (_columns(cfg.get("metrics"), role="tag_metric") or [{"column": "value"}])[0]
    tag_by = (
        _api_column(cfg.get("breakdown"), role="tag_by")
        or (_columns(cfg.get("breakdowns"), role="tag_by") or [None])[0]
        or {"column": "label"}
    )
    out: dict[str, Any] = {
        "type": "tag_cloud",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metric": metric,
        "tag_by": tag_by,
    }
    styling = _api_partition_styling(cfg.get("styling"), kind="tag_cloud")
    if styling:
        out["styling"] = styling
    return out


def _cfg_mosaic(title: str, cfg: dict[str, Any], query: str) -> dict[str, Any]:
    metric = _api_column(cfg.get("metric"), role="mosaic_metric") or (_columns(cfg.get("metrics"), role="mosaic_metric") or [{"column": "value"}])[0]
    out: dict[str, Any] = {
        "type": "mosaic",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metric": metric,
    }
    groups = _columns(cfg.get("breakdowns"), role="partition_group") or _columns(cfg.get("group_by"), role="partition_group")
    if groups:
        out["group_by"] = [groups[0]]
    legend = _api_legend(cfg.get("legend"), kind="mosaic")
    if legend:
        out["legend"] = legend
    styling = _api_partition_styling(cfg.get("styling"), kind="mosaic")
    if styling:
        out["styling"] = styling
    return out


def _cfg_region_map(title: str, cfg: dict[str, Any], query: str) -> dict[str, Any]:
    metric = _api_column(cfg.get("metric"), role="region_metric") or (_columns(cfg.get("metrics"), role="region_metric") or [{"column": "value"}])[0]
    region = (
        _api_column(cfg.get("region"), role="region")
        or _api_column(cfg.get("breakdown"), role="region")
        or (_columns(cfg.get("breakdowns"), role="region") or [None])[0]
        or {"column": "region"}
    )
    return {
        "type": "region_map",
        "title": title,
        "data_source": {"type": "esql", "query": query},
        "metric": metric,
        "region": region,
    }


_CONFIG_BUILDERS = {
    "metric": _cfg_metric,
    "gauge": _cfg_gauge,
    "pie": _cfg_pie,
    "heatmap": _cfg_heatmap,
    "treemap": _cfg_treemap,
    "datatable": _cfg_datatable,
    "data_table": _cfg_datatable,
    "tag_cloud": _cfg_tagcloud,
    "tagcloud": _cfg_tagcloud,
    "waffle": _cfg_waffle,
    "mosaic": _cfg_mosaic,
    "region_map": _cfg_region_map,
    "regionmap": _cfg_region_map,
}


# --------------------------------------------------------------------------- #
# Panel mapping
# --------------------------------------------------------------------------- #

def _grid(panel: dict[str, Any]) -> dict[str, int]:
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


def _presentation(panel: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    vir = panel.get("visual_ir") if isinstance(panel.get("visual_ir"), dict) else {}
    pres = vir.get("presentation") if isinstance(vir, dict) else {}
    if not isinstance(pres, dict):
        return "", {}
    cfg_obj = pres.get("config")
    cfg: dict[str, Any] = cfg_obj if isinstance(cfg_obj, dict) else {}
    return str(pres.get("kind") or ""), cfg


def map_panel(panel: dict[str, Any]) -> PanelMapping:
    """Map one migration-report panel into a typed API panel item."""
    title = str(panel.get("title") or "")
    kind, cfg = _presentation(panel)
    grid = _grid(panel)

    if kind == "markdown":
        content = str(cfg.get("content") or "")
        return PanelMapping(
            {"grid": grid, "type": "markdown", "config": {"title": title, "content": content, "settings": {}}},
            kind="markdown",
        )

    if kind != "esql":
        return PanelMapping(None, reason=f"presentation kind '{kind or '(none)'}' not mappable", kind=kind)

    chart_type = str(cfg.get("type") or "").lower()
    query = str(cfg.get("query") or panel.get("esql") or "").strip()
    if not query:
        return PanelMapping(None, reason="esql panel has no query", kind=chart_type)

    if chart_type in _XY_KINDS:
        config = _cfg_xy(title, cfg, query)
    else:
        builder = _CONFIG_BUILDERS.get(chart_type)
        if builder is None:
            return PanelMapping(None, reason=f"esql chart type '{chart_type}' has no API builder", kind=chart_type)
        config = builder(title, cfg, query)

    return PanelMapping({"grid": grid, "type": "vis", "config": config}, kind=chart_type)


def native_dashboard_from_report(dashboard: dict[str, Any]) -> tuple[NativeDashboard, NativeMappingCounts]:
    """Build a :class:`NativeDashboard` from one flat migration-report dashboard.

    This is the report-path counterpart of :func:`native_dashboard_from_yaml`.
    Grafana rows arrive as skipped section markers in the flat report; nested
    section reconstruction is a YAML-only capability, since the flat report
    carries no ``section``/``controls`` structure.
    """
    title = str(dashboard.get("title") or "migrated dashboard")
    native = NativeDashboard(title=title)
    counts = NativeMappingCounts()
    for panel in dashboard.get("panels", []):
        if not isinstance(panel, dict):
            continue
        if str(panel.get("grafana_type") or "") == "row":
            continue
        result = map_panel(panel)
        counts.record(result.api_panel is not None, result.reason)
        if result.api_panel is not None:
            native.items.append(NativePanel.from_api_dict(result.api_panel))
    return native, counts


def build_dashboard_payload(dashboard: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int], dict[str, int]]:
    """Build a typed ``POST /api/dashboards`` body from one report dashboard.

    Thin wrapper: the JSON body is exactly
    ``NativeDashboard.to_api_payload()`` (see :func:`native_dashboard_from_report`)
    -- this function does not assemble the payload shape itself. Returns
    ``(payload, counts, unmapped_reasons)``.
    """
    native, counts = native_dashboard_from_report(dashboard)
    return native.to_api_payload(), {"mapped": counts.mapped, "unmapped": counts.unmapped}, dict(counts.reasons)


# --------------------------------------------------------------------------- #
# YAML-based mapping (kb-dashboard-core schema -> typed API)
#
# The emitted YAML is the richest source: it carries nested ``section`` blocks
# and dashboard-level ``controls`` that the flat report drops. A leaf panel
# stores its chart config under ``esql`` (or ``markdown``) with the exact same
# keys the per-type builders above already consume, so those builders are reused
# verbatim here.
# --------------------------------------------------------------------------- #

def _grid_from_yaml(panel: dict[str, Any]) -> dict[str, int]:
    """Map YAML ``size`` + ``position`` to an API panel ``grid``."""
    size_obj = panel.get("size")
    pos_obj = panel.get("position")
    size: dict[str, Any] = size_obj if isinstance(size_obj, dict) else {}
    pos: dict[str, Any] = pos_obj if isinstance(pos_obj, dict) else {}
    return {
        "x": int(pos.get("x") or 0),
        "y": int(pos.get("y") or 0),
        "w": int(size.get("w") or 24),
        "h": int(size.get("h") or 8),
    }


def _config_from_esql(title: str, esql: dict[str, Any]) -> tuple[dict[str, Any] | None, str, str]:
    """Build an API ``vis`` config from a YAML ``esql`` block.

    Returns ``(config, kind, reason)``; ``config`` is ``None`` on failure.
    """
    chart_type = str(esql.get("type") or "").lower()
    query = str(esql.get("query") or "").strip()
    if not query:
        return None, chart_type, "esql panel has no query"
    if chart_type in _XY_KINDS:
        return _cfg_xy(title, esql, query), chart_type, ""
    builder = _CONFIG_BUILDERS.get(chart_type)
    if builder is None:
        return None, chart_type, f"esql chart type '{chart_type}' has no API builder"
    return builder(title, esql, query), chart_type, ""


def _stable_dashboard_id(dashboard: dict[str, Any]) -> str:
    """Return a deterministic API dashboard id for idempotent upserts."""
    for key in ("id", "uid", "dashboard_id"):
        value = str(dashboard.get(key) or "").strip()
        if value:
            return value
    title = str(dashboard.get("name") or dashboard.get("title") or "migrated-dashboard")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"obs-migrate-{slug or 'dashboard'}"


def map_yaml_panel(panel: dict[str, Any]) -> PanelMapping:
    """Map one YAML *leaf* panel (``esql`` or ``markdown``) to an API panel."""
    title = str(panel.get("title") or "")
    grid = _grid_from_yaml(panel)
    hide_title = bool(panel.get("hide_title"))

    markdown = panel.get("markdown")
    if isinstance(markdown, dict):
        markdown_config: dict[str, Any] = {
            "title": title,
            "content": str(markdown.get("content") or ""),
            "settings": {},
        }
        if hide_title:
            markdown_config["hide_title"] = True
        return PanelMapping({"grid": grid, "type": "markdown", "config": markdown_config}, kind="markdown")

    esql = panel.get("esql")
    if isinstance(esql, dict):
        esql_config, kind, reason = _config_from_esql(title, esql)
        if esql_config is None:
            return PanelMapping(None, reason=reason, kind=kind)
        if hide_title:
            esql_config["hide_title"] = True
        return PanelMapping({"grid": grid, "type": "vis", "config": esql_config}, kind=kind)

    return PanelMapping(None, reason="panel has neither esql nor markdown", kind="")


def _selected_options(control: dict[str, Any]) -> list[str | int | float]:
    raw = control.get("defaults") if "defaults" in control else control.get("default")
    if raw is None:
        raw = control.get("selected_options")
    if raw is None:
        # Datadog template-variable controls carry defaults under ``preselected``.
        raw = control.get("preselected")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str | int | float) and not isinstance(item, bool)]
    if isinstance(raw, str | int | float) and not isinstance(raw, bool):
        return [raw]
    return []


def map_yaml_control(control: dict[str, Any]) -> dict[str, Any] | None:
    """Map a YAML dashboard ``control`` to a native ``pinned_panels`` item.

    A control with a ``query`` becomes a ``VALUES_FROM_QUERY`` ES|QL control
    (YAML ``query`` -> ``esql_query``, ``label`` -> ``title``); a control with a
    fixed option list becomes a ``STATIC_VALUES`` control.
    """
    if not isinstance(control, dict):
        return None
    control_type = str(control.get("type") or "").lower()
    title = str(control.get("label") or control.get("title") or control.get("field") or control.get("variable_name") or "")
    defaults = _selected_options(control)
    if control_type in {"options", "option", "options_list", "options_list_control"}:
        data_view_id = str(control.get("data_view_id") or control.get("data_view") or "")
        field_name = str(control.get("field_name") or control.get("field") or "")
        if not data_view_id or not field_name:
            return None
        options_config: dict[str, Any] = {
            "title": title or field_name,
            "data_view_id": data_view_id,
            "field_name": field_name,
            "selected_options": defaults,
        }
        if control.get("multiple") is False:
            options_config["single_select"] = True
        return {"type": "options_list_control", "config": options_config}
    if control_type in {"range", "range_slider", "range_slider_control"}:
        data_view_id = str(control.get("data_view_id") or control.get("data_view") or "")
        field_name = str(control.get("field_name") or control.get("field") or "")
        if not data_view_id or not field_name:
            return None
        range_config: dict[str, Any] = {"title": title or field_name, "data_view_id": data_view_id, "field_name": field_name}
        if len(defaults) == 2:
            range_config["value"] = [str(defaults[0]), str(defaults[1])]
        return {"type": "range_slider_control", "config": range_config}

    variable_name = str(control.get("variable_name") or "")
    if not variable_name:
        return None
    title = title or variable_name
    variable_type = str(control.get("variable_type") or "values")
    query = str(control.get("query") or "").strip()
    if query:
        esql_config: dict[str, Any] = {
            "control_type": "VALUES_FROM_QUERY",
            "title": title,
            "variable_name": variable_name,
            "variable_type": variable_type,
            "esql_query": query,
            "selected_options": [str(option) for option in defaults],
        }
    else:
        raw_options = control.get("available_options") or control.get("options") or []
        options = [str(opt) for opt in raw_options] if isinstance(raw_options, list) else []
        esql_config = {
            "control_type": "STATIC_VALUES",
            "title": title,
            "variable_name": variable_name,
            "variable_type": variable_type,
            "available_options": options,
            "selected_options": [str(option) for option in defaults],
        }
    if control.get("multiple") is False:
        esql_config["single_select"] = True
    return {"type": "esql_control", "config": esql_config}


def native_dashboard_from_yaml(dashboard: dict[str, Any]) -> tuple[NativeDashboard, NativeMappingCounts]:
    """Build a :class:`NativeDashboard` from one kb-dashboard-core YAML dashboard.

    Reconstructs nested ``section`` blocks into native API sections and
    dashboard-level ``controls`` into ``pinned_panels`` -- capabilities the
    flat migration report cannot express (see
    :func:`native_dashboard_from_report`). The 100-item caps (top-level and
    per-section, controls, and combined total) are enforced by
    :meth:`NativeDashboard.enforce_item_cap` plus the control-budget gate below.
    """
    title = str(dashboard.get("name") or dashboard.get("title") or "migrated dashboard")
    description = dashboard.get("description")
    native = NativeDashboard(
        title=title,
        description=str(description) if description else "",
        dashboard_id=_stable_dashboard_id(dashboard),
    )
    counts = NativeMappingCounts()
    next_y = 0

    for panel in dashboard.get("panels", []) or []:
        if not isinstance(panel, dict):
            continue
        section = panel.get("section")
        if isinstance(section, dict):
            sec_panels: list[NativePanel] = []
            for sub in (section.get("panels") or []):
                if not isinstance(sub, dict):
                    continue
                result = map_yaml_panel(sub)
                if result.api_panel is not None and len(sec_panels) >= _MAX_SECTION_PANELS:
                    # Beyond the per-section panel cap: record the drop instead
                    # of silently counting it as mapped.
                    counts.add_reason("dropped_over_section_panel_cap")
                    continue
                counts.record(result.api_panel is not None, result.reason)
                if result.api_panel is not None:
                    sec_panels.append(NativePanel.from_api_dict(result.api_panel))
            native.items.append(
                NativeSection(
                    title=str(panel.get("title") or ""),
                    collapsed=bool(section.get("collapsed", False)),
                    panels=sec_panels,
                    grid=NativeGrid(y=next_y),
                )
            )
            counts.sections += 1
            next_y += 1
            continue
        result = map_yaml_panel(panel)
        counts.record(result.api_panel is not None, result.reason)
        if result.api_panel is not None:
            native_panel = NativePanel.from_api_dict(result.api_panel)
            native.items.append(native_panel)
            next_y = max(next_y, native_panel.grid.y + (native_panel.grid.h or 8))

    # Respect the top-level and total item caps. If the dashboard is a flat wall of
    # leaf panels (no real sections), wrap them into synthetic sections; if
    # it mixes sections with loose panels, coalesce runs of loose panels into
    # synthetic sections instead of truncating.
    native.enforce_item_cap(
        counts,
        max_items=_MAX_DASHBOARD_ITEMS,
        max_section_panels=_MAX_SECTION_PANELS,
        max_total_items=_MAX_TOTAL_ITEMS,
    )

    pinned: list[NativeControl] = []
    available_control_slots = max(0, min(_MAX_PINNED_CONTROLS, _MAX_TOTAL_ITEMS - dashboard_item_count(native.items)))
    mapped_control_count = 0
    for control in dashboard.get("controls") or []:
        mapped_control = map_yaml_control(control)
        if mapped_control is not None:
            if mapped_control_count >= available_control_slots:
                reason = (
                    "dropped_over_pinned_control_cap"
                    if mapped_control_count >= _MAX_PINNED_CONTROLS
                    else "dropped_over_total_item_cap"
                )
                counts.add_reason(reason)
                mapped_control_count += 1
                continue
            pinned.append(NativeControl(type=mapped_control["type"], config=mapped_control["config"]))
            mapped_control_count += 1
    native.controls = pinned
    counts.controls = len(pinned)

    return native, counts


def build_dashboard_payload_from_yaml(
    dashboard: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int], dict[str, int]]:
    """Build a typed ``POST /api/dashboards`` body from one YAML dashboard.

    Thin wrapper: the JSON body is exactly
    ``NativeDashboard.to_api_payload()`` (see :func:`native_dashboard_from_yaml`)
    -- this function does not assemble the payload shape itself. Returns
    ``(payload, counts, unmapped_reasons)`` where ``counts`` has ``mapped`` /
    ``unmapped`` / ``sections`` / ``controls``.
    """
    native, counts = native_dashboard_from_yaml(dashboard)
    counts_dict, reasons = counts.as_dicts()
    return native.to_api_payload(), counts_dict, reasons


def _iter_yaml_dashboards(doc: Any) -> list[dict[str, Any]]:
    """Yield dashboard dicts from a loaded YAML doc or a bare dashboard dict."""
    if isinstance(doc, dict) and isinstance(doc.get("dashboards"), list):
        return [d for d in doc["dashboards"] if isinstance(d, dict)]
    if isinstance(doc, dict) and ("panels" in doc or "name" in doc or "title" in doc):
        return [doc]
    if isinstance(doc, list):
        return [d for d in doc if isinstance(d, dict)]
    return []


def build_payload_from_yaml(yaml_doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a typed API payload from a loaded kb-dashboard-core YAML document.

    Convenience wrapper around :func:`build_dashboard_payload_from_yaml` for the
    common single-dashboard-per-file case. ``stats`` includes ``mapped`` /
    ``unmapped`` / ``sections`` / ``controls`` plus a ``reasons`` breakdown.
    """
    dashboards = _iter_yaml_dashboards(yaml_doc)
    if not dashboards:
        return {"title": "migrated dashboard", "panels": []}, {
            "mapped": 0, "unmapped": 0, "sections": 0, "controls": 0, "reasons": {},
        }
    payload, counts, reasons = build_dashboard_payload_from_yaml(dashboards[0])
    stats: dict[str, Any] = dict(counts)
    stats["reasons"] = dict(reasons)
    return payload, stats


# --------------------------------------------------------------------------- #
# Deploy
# --------------------------------------------------------------------------- #

def _session(api_key: str = "", verify: bool | str = True) -> requests.Session:
    session = requests.Session()
    apply_tls(session, verify)
    session.headers.update({"kbn-xsrf": "true", "Content-Type": "application/json"})
    if api_key:
        session.headers["Authorization"] = f"ApiKey {api_key}"
    return session


def upload_report(
    report: dict[str, Any],
    kibana_url: str,
    *,
    api_key: str = "",
    space_id: str = "",
    verify: bool | str = True,
    timeout: int = 60,
    fallback: Any = None,
) -> list[UploadResult]:
    """Deploy each dashboard in a migration report via the typed API.

    ``fallback`` (optional) is called ``fallback(dashboard)`` when the typed
    payload is rejected, so callers can route the rejected dashboard through the
    legacy ``kb-dashboard-cli`` ``_import`` path. This module does not import
    that path itself to keep the dependency direction clean.
    """
    session = _session(api_key, verify=verify)
    base = kibana_url_for_space(kibana_url, space_id).rstrip("/")
    results: list[UploadResult] = []

    for dashboard in report.get("dashboards", []):
        if not isinstance(dashboard, dict):
            continue
        payload, counts, reasons = build_dashboard_payload(dashboard)
        res = UploadResult(
            dashboard=str(dashboard.get("title") or ""),
            mapped=counts["mapped"],
            unmapped=counts["unmapped"],
            unmapped_reasons=dict(reasons),
        )
        if not payload["panels"]:
            res.status = "empty"
            results.append(res)
            continue
        response = session.post(f"{base}/api/dashboards", data=json.dumps(payload), timeout=timeout)
        res.http_status = response.status_code
        if 200 <= response.status_code < 300:
            res.status = "created"
            try:
                res.dashboard_id = str(response.json().get("id") or "")
            except ValueError:
                pass
        else:
            res.status = "rejected"
            try:
                body = response.json()
                res.message = str(body.get("message", body))[:2000]
            except ValueError:
                res.message = response.text[:2000]
            if fallback is not None:
                fallback(dashboard)
        results.append(res)
    return results


def _classify_response(res: UploadResult, response: requests.Response) -> None:
    """Fill an ``UploadResult`` from a ``PUT /api/dashboards/{id}`` response."""
    res.http_status = response.status_code
    if 200 <= response.status_code < 300:
        res.status = "created" if response.status_code == 201 else "updated"
        try:
            res.dashboard_id = str(response.json().get("id") or "")
        except ValueError:
            pass
    else:
        res.status = "rejected"
        try:
            body = response.json()
            res.message = str(body.get("message", body))[:2000]
        except ValueError:
            res.message = response.text[:2000]


# Retryable server-side conditions: rate limiting and transient gateway/service
# failures on a slow or momentarily overloaded cluster. Genuine 4xx schema
# rejections (400, 404, ...) are not retried -- retrying would not change a
# real payload defect, only waste time before the legacy fallback kicks in.
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_UPLOAD_MAX_ATTEMPTS = 3
_UPLOAD_BACKOFF_SECONDS = 1.5


def _put_with_retry(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    *,
    timeout: int,
    max_attempts: int = _UPLOAD_MAX_ATTEMPTS,
    backoff_seconds: float = _UPLOAD_BACKOFF_SECONDS,
) -> tuple[requests.Response | None, str]:
    """``PUT`` with retry-with-backoff so a transient failure doesn't
    permanently downgrade a valid dashboard to the legacy import fallback.

    A 20-30 dashboard live batch test found that ~58% of dashboards whose
    payload the typed API independently accepted on retry still fell back to
    legacy ``_import`` during the real run, on a slow/shared staging
    cluster -- almost certainly transient 5xx/timeouts with no retry before
    ``_classify_response`` marked them ``"rejected"``. Retry connection
    errors, read timeouts, and ``_RETRYABLE_STATUS_CODES`` a few times with
    exponential backoff before giving up.

    Returns ``(response, error)``. ``response`` is ``None`` only when every
    attempt raised a network-level exception (no HTTP response was ever
    received); ``error`` then holds a short description so the caller can
    report a ``"rejected"`` :class:`UploadResult` (and let its existing
    fallback path run) instead of the exception propagating and crashing the
    whole batch upload.
    """
    data = json.dumps(payload)
    last_error = ""
    for attempt in range(max_attempts):
        is_last_attempt = attempt == max_attempts - 1
        try:
            response = session.put(url, data=data, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
            if not is_last_attempt:
                time.sleep(backoff_seconds * (2**attempt))
            continue
        if response.status_code in _RETRYABLE_STATUS_CODES and not is_last_attempt:
            time.sleep(backoff_seconds * (2**attempt))
            continue
        return response, ""
    return None, last_error or "request failed after retries"


def upload_native_dashboard(
    dashboard: NativeDashboard,
    kibana_url: str,
    *,
    api_key: str = "",
    space_id: str = "",
    verify: bool | str = True,
    timeout: int = 60,
    native_stats: dict[str, Any] | None = None,
    dashboard_id: str = "",
) -> UploadResult:
    """Deploy one pre-built :class:`NativeDashboard` via the typed API.

    Source translators can hand the emitted native artifact straight to upload
    instead of forcing a disk YAML reparse. YAML remains available as the
    legacy/debug fallback artifact, but this path makes the in-memory IR the
    canonical native payload for Grafana/Datadog CLI uploads.
    """
    stats = native_stats if isinstance(native_stats, dict) else {}
    raw_reasons = stats.get("reasons")
    reasons: dict[str, int] = {}
    if isinstance(raw_reasons, dict):
        reasons = {
            str(key): int(value)
            for key, value in raw_reasons.items()
            if isinstance(value, int | float) and not isinstance(value, bool)
        }
    res = UploadResult(
        dashboard=dashboard.title,
        mapped=int(stats.get("mapped", dashboard_leaf_panel_count(dashboard.items)) or 0),
        unmapped=int(stats.get("unmapped", 0) or 0),
        unmapped_reasons=reasons,
    )
    payload = dashboard.to_api_payload()
    if not payload["panels"] and not payload.get("pinned_panels"):
        res.status = "empty"
        return res

    session = _session(api_key, verify=verify)
    base = kibana_url_for_space(kibana_url, space_id).rstrip("/")
    resolved_dashboard_id = dashboard_id or dashboard.dashboard_id or _stable_dashboard_id({"name": dashboard.title})
    response, error = _put_with_retry(
        session, f"{base}/api/dashboards/{resolved_dashboard_id}", payload, timeout=timeout,
    )
    if response is None:
        res.status = "rejected"
        res.message = error[:2000]
        return res
    _classify_response(res, response)
    return res


def upload_yaml_files(
    yaml_paths: list[str],
    kibana_url: str,
    *,
    api_key: str = "",
    space_id: str = "",
    verify: bool | str = True,
    timeout: int = 60,
    fallback: Any = None,
) -> list[UploadResult]:
    """Deploy each dashboard in each kb-dashboard-core YAML file via the typed API.

    This is the richest path: it reconstructs nested sections and dashboard
    controls (``pinned_panels``) that the flat report cannot express. Each YAML
    file may contain one or more dashboards. ``fallback`` (optional) is called
    ``fallback(yaml_path, dashboard)`` when a dashboard's typed payload is rejected, so
    callers can route it through the legacy ``kb-dashboard-cli`` ``_import`` path.
    """
    session = _session(api_key, verify=verify)
    base = kibana_url_for_space(kibana_url, space_id).rstrip("/")
    results: list[UploadResult] = []
    seen_ids: set[str] = set()

    for path in yaml_paths:
        with open(path, encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
        for dashboard in _iter_yaml_dashboards(doc):
            payload, counts, reasons = build_dashboard_payload_from_yaml(dashboard)
            res = UploadResult(
                dashboard=str(dashboard.get("name") or dashboard.get("title") or ""),
                mapped=counts["mapped"],
                unmapped=counts["unmapped"],
                unmapped_reasons=dict(reasons),
            )
            if not payload["panels"] and not payload.get("pinned_panels"):
                res.status = "empty"
                if fallback is not None:
                    fallback(path, dashboard)
                results.append(res)
                continue
            # Distinct dashboards that slug to the same id (e.g. duplicate
            # titles) must not overwrite each other via idempotent PUT.
            dashboard_id = _stable_dashboard_id(dashboard)
            if dashboard_id in seen_ids:
                suffix = 2
                while f"{dashboard_id}-{suffix}" in seen_ids:
                    suffix += 1
                dashboard_id = f"{dashboard_id}-{suffix}"
            seen_ids.add(dashboard_id)
            response, error = _put_with_retry(
                session, f"{base}/api/dashboards/{dashboard_id}", payload, timeout=timeout,
            )
            if response is None:
                res.status = "rejected"
                res.message = error[:2000]
            else:
                _classify_response(res, response)
            if res.status == "rejected" and fallback is not None:
                fallback(path, dashboard)
            results.append(res)
    return results


def delete_dashboard(
    dashboard_id: str,
    kibana_url: str,
    *,
    api_key: str = "",
    space_id: str = "",
    verify: bool | str = True,
    timeout: int = 30,
) -> bool:
    """DELETE /api/dashboards/{id}. Returns True on a 2xx response."""
    session = _session(api_key, verify=verify)
    base = kibana_url_for_space(kibana_url, space_id).rstrip("/")
    response = session.delete(f"{base}/api/dashboards/{dashboard_id}", timeout=timeout)
    return 200 <= response.status_code < 300


__all__ = [
    "PanelMapping",
    "UploadResult",
    "build_dashboard_payload",
    "build_dashboard_payload_from_yaml",
    "build_payload_from_yaml",
    "delete_dashboard",
    "map_panel",
    "map_yaml_control",
    "map_yaml_panel",
    "native_dashboard_from_report",
    "native_dashboard_from_yaml",
    "upload_native_dashboard",
    "upload_report",
    "upload_yaml_files",
]
