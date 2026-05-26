# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Display enrichment: Datadog units and visual config → kb-dashboard YAML format.

Maps Datadog unit strings and formatting to the YAML format spec used by
kb-dashboard-cli.
"""

from __future__ import annotations

from typing import Any

from .models import NormalizedWidget, TranslationResult

DATADOG_UNIT_MAP: dict[str, dict[str, Any]] = {
    "byte": {"type": "bytes"},
    "kibibyte": {"type": "bytes"},
    "mebibyte": {"type": "bytes"},
    "gibibyte": {"type": "bytes"},
    "tebibyte": {"type": "bytes"},
    "bit": {"type": "bits"},
    "kilobit": {"type": "bits"},
    "megabit": {"type": "bits"},
    "gigabit": {"type": "bits"},
    "percent": {"type": "number", "suffix": "%"},
    "percent_nano": {"type": "number", "suffix": "%"},
    "nanosecond": {"type": "number", "suffix": " ns", "decimals": 0},
    "microsecond": {"type": "number", "suffix": " µs", "decimals": 0},
    "millisecond": {"type": "number", "suffix": " ms", "decimals": 1},
    "second": {"type": "duration"},
    "minute": {"type": "number", "suffix": " min"},
    "hour": {"type": "number", "suffix": " h"},
    "day": {"type": "number", "suffix": " d"},
    "hertz": {"type": "number", "suffix": " Hz"},
    "operation": {"type": "number", "suffix": " ops"},
    "request": {"type": "number", "suffix": " req"},
    "packet": {"type": "number", "suffix": " pkt"},
    "error": {"type": "number", "suffix": " err"},
    "connection": {"type": "number", "suffix": " conn"},
    "page": {"type": "number", "suffix": " pg"},
    "query": {"type": "number", "suffix": " qry"},
    "thread": {"type": "number", "suffix": " thr"},
    "process": {"type": "number", "suffix": " proc"},
    "core": {"type": "number", "suffix": " core"},
    "dollar": {"type": "number", "suffix": " $"},
    "euro": {"type": "number", "suffix": " €"},
}


def enrich_panel_display(
    yaml_panel: dict[str, Any],
    widget: NormalizedWidget,
    result: TranslationResult,
) -> dict[str, Any]:
    """Enrich a generated YAML panel with display formatting."""

    esql = yaml_panel.get("esql", {})
    if not esql:
        return yaml_panel

    unit_format = _resolve_unit(widget)
    if unit_format:
        _apply_format(esql, unit_format, result.kibana_type)

    _apply_legend(esql, widget, result.kibana_type)
    _apply_axis(yaml_panel, widget, result)

    if widget.title:
        yaml_panel["title"] = _clean_template_vars(widget.title)

    return yaml_panel


def _warn(result: TranslationResult, message: str) -> None:
    if message not in result.warnings:
        result.warnings.append(message)


def _resolve_unit(widget: NormalizedWidget) -> dict[str, Any] | None:
    unit = widget.custom_unit
    if not unit:
        yaxis = widget.yaxis
        if isinstance(yaxis, dict):
            unit = yaxis.get("label", "")
    if not unit:
        return None
    return DATADOG_UNIT_MAP.get(unit.lower())


def _apply_format(
    esql: dict[str, Any],
    fmt: dict[str, Any],
    kibana_type: str,
) -> None:
    if kibana_type == "metric":
        primary = esql.get("primary")
        if isinstance(primary, dict):
            primary.setdefault("format", fmt)
        secondary = esql.get("secondary")
        if isinstance(secondary, dict):
            secondary.setdefault("format", fmt)
    elif kibana_type == "xy":
        metrics = esql.get("metrics", [])
        if metrics:
            for metric in metrics:
                if isinstance(metric, dict):
                    metric.setdefault("format", fmt)
    elif kibana_type in ("heatmap", "treemap"):
        metric = esql.get("metric")
        if isinstance(metric, dict):
            metric.setdefault("format", fmt)
    elif kibana_type in ("table", "partition"):
        metrics = esql.get("metrics", [])
        for metric in metrics:
            if isinstance(metric, dict):
                metric.setdefault("format", fmt)


def _apply_legend(
    esql: dict[str, Any],
    widget: NormalizedWidget,
    kibana_type: str,
) -> None:
    legend = widget.legend
    shown = True
    if isinstance(legend, dict):
        visible = legend.get("visible", True)
        shown = visible in (True, "true", "show")

    if kibana_type in ("xy",):
        esql.setdefault("legend", {
            "visible": "show" if shown else "hide",
            "position": "right",
            "truncate_labels": 1,
        })
    elif kibana_type in ("partition", "treemap"):
        esql.setdefault("legend", {
            "visible": "auto" if shown else "hide",
            "truncate_labels": 1,
        })
    elif kibana_type == "heatmap":
        appearance = esql.setdefault("appearance", {})
        appearance.setdefault("legend", {"visible": "show", "position": "right"})


def _apply_axis(yaml_panel: dict[str, Any], widget: NormalizedWidget, result: TranslationResult) -> None:
    """Map Datadog yaxis config into kb-dashboard appearance.y_left_axis.

    Only XY panels (line/bar/area, kibana_type='xy') accept y_left_axis in
    their appearance block.  All other Kibana types reject it with
    'Extra inputs are not permitted'.  For non-XY panels we skip axis mapping
    entirely; scale and bounds cannot be preserved without a supported target.

    Kibana's extent requires BOTH min and max when mode='custom'.  When only
    one bound is present we apply these rules:
      - max-only + include_zero=true (Datadog default): infer min=0 and emit
        a full custom extent — include_zero is semantically identical to min=0.
      - max-only + include_zero=false: omit extent and warn; Kibana auto-scales.
      - min-only (no max): omit extent and warn; Kibana auto-scales upper bound.
      - Both: emit full custom extent (already correct).
      - Neither parseable: omit extent.
    Unparseable sentinels such as "auto" are treated as absent.
    """
    yaxis = widget.yaxis
    if not isinstance(yaxis, dict):
        return
    esql = yaml_panel.get("esql")
    if not isinstance(esql, dict):
        return
    # y_left_axis is only valid for XY panels; skip for all other types.
    if result.kibana_type != "xy":
        return
    y_cfg: dict[str, Any] = {}
    label = yaxis.get("label")
    if label and isinstance(label, str):
        y_cfg["title"] = label
    scale = yaxis.get("scale")
    if scale == "log":
        y_cfg["scale"] = "log"
    elif scale == "sqrt":
        y_cfg["scale"] = "sqrt"

    # include_zero defaults to True in Datadog — omitting it means "anchor at 0"
    include_zero: bool = yaxis.get("include_zero", True) is not False

    parsed_min: float | None = None
    parsed_max: float | None = None
    raw_min = yaxis.get("min")
    raw_max = yaxis.get("max")
    if raw_min is not None:
        try:
            parsed_min = float(raw_min)
        except (ValueError, TypeError):
            pass  # "auto" or other non-numeric sentinel → treat as absent
    if raw_max is not None:
        try:
            parsed_max = float(raw_max)
        except (ValueError, TypeError):
            pass

    if parsed_min is not None and parsed_max is not None:
        y_cfg["extent"] = {"mode": "custom", "min": parsed_min, "max": parsed_max}
    elif parsed_max is not None and include_zero:
        # include_zero=true is an exact translation of min=0
        y_cfg["extent"] = {"mode": "custom", "min": 0.0, "max": parsed_max}
    elif parsed_max is not None:
        _warn(result, f"y-axis max={parsed_max} has no inferable min (include_zero=false); "
              "extent omitted — Kibana will auto-scale. Review axis bounds.")
    elif parsed_min is not None:
        _warn(result, f"y-axis min={parsed_min} has no max; "
              "extent omitted — Kibana will auto-scale upper bound. Review axis bounds.")

    if y_cfg:
        appearance = esql.setdefault("appearance", {})
        appearance.setdefault("y_left_axis", {}).update(y_cfg)


def _clean_template_vars(title: str) -> str:
    """Replace Datadog template variable placeholders for Kibana."""
    import re
    title = re.sub(r"\$(\w+)\.value", r"{\1}", title)
    title = re.sub(r"\$(\w+)", r"{\1}", title)
    return title
