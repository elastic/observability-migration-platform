# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Display enrichment: Datadog units and visual config → kb-dashboard YAML format.

Maps Datadog unit strings and formatting to the YAML format spec used by
kb-dashboard-cli.
"""

from __future__ import annotations

from typing import Any

from .models import NormalizedWidget, TranslationResult
from observability_migration.targets.kibana.emit.display import sanitize_axis_title_text

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
    # NOTE: intentionally a bare ``{"type": "duration"}`` -- the kb-dashboard-core
    # YAML schema's per-panel format definitions (``ESQLMetricFormat`` et al,
    # see docs/dashboards/schema.json) do not accept ``from``/``to`` at all
    # ("Extra inputs are not permitted"), so adding them here would break the
    # legacy kb-dashboard-cli compile path. The typed Dashboards API's OWN
    # multi-column format schema does require ``from``/``to``, but
    # ``targets.kibana.dashboards_api._api_format`` already defaults them
    # independently for that path regardless of what a YAML panel's format
    # block carries, so nothing needs to be duplicated here.
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
    _apply_conditional_formats(esql, widget, result.kibana_type)

    if widget.title:
        yaml_panel["title"] = _clean_template_vars(widget.title)

    return yaml_panel


# Datadog conditional-format palettes are foreground_on_background pairs (plus a
# few named variants). We key off the semantic color word and emit a hex the
# Kibana Dashboards API accepts for a dynamic color-by-value step.
_PALETTE_COLORS: dict[str, str] = {
    "red": "#F6726A",
    "green": "#24C292",
    "yellow": "#F5A700",
    "orange": "#FBA740",
    "blue": "#61A2FF",
    "gray": "#98A2B3",
    "grey": "#98A2B3",
    "white": "#FFFFFF",
    "black": "#1D2A3E",
}


_NEUTRAL_PALETTE_WORDS = {"white", "black", "gray", "grey"}


def _palette_to_color(palette: str) -> str:
    """Map a Datadog conditional-format palette to a hex color.

    Datadog palettes are ``<foreground>_on_<background>`` (e.g. ``white_on_red``,
    ``red_on_white``, ``black_on_light_green``). The semantic status color is the
    *non-neutral* hue wherever it appears — white/black/grey are only the
    contrast color. Picking the ``_on_<bg>`` word blindly turned ``red_on_white``
    into an invisible white, so we prefer a meaningful hue first and only fall
    back to a neutral word when no hue is present.
    """
    text = (palette or "").lower()
    for name, hex_color in _PALETTE_COLORS.items():
        if name in _NEUTRAL_PALETTE_WORDS:
            continue
        if name in text:
            return hex_color
    for name, hex_color in _PALETTE_COLORS.items():
        if name in text:
            return hex_color
    return ""


_CF_COMPARATORS = {">", ">=", "<", "<=", "=", "=="}


def _cf_matches(value: float, comparator: str, threshold: float) -> bool:
    if comparator == ">":
        return value > threshold
    if comparator == ">=":
        return value >= threshold
    if comparator == "<":
        return value < threshold
    if comparator == "<=":
        return value <= threshold
    return value == threshold  # "=" / "=="


def _cf_specificity(comparator: str, threshold: float) -> float:
    """Precedence when several rules match one value: the tightest bound wins.

    For ``>``/``>=`` a higher threshold is more severe; for ``<``/``<=`` a lower
    threshold is more severe; an equality rule is the most specific of all. This
    reproduces the common traffic-light intent (e.g. ``>70`` warn, ``>90`` crit)
    without depending on the exact list order for overlapping ranges.
    """
    if comparator in (">", ">="):
        return threshold
    if comparator in ("<", "<="):
        return -threshold
    return float("inf")


def _conditional_format_color(conditional_formats: list[Any]) -> dict[str, Any] | None:
    """Translate Datadog threshold rules into a Kibana ``MetricChartColor`` /
    ``DatatableMetricColor`` dynamic color-by-value (``thresholds`` of
    ascending ``up_to``/``color`` bands, matching ``docs/dashboards/schema.json``
    — the same shape Grafana gauge thresholds use via
    ``grafana.panels._build_gauge_color_mapping``).

    Datadog conditional formats are independent ``value <comparator> threshold
    -> palette`` rules; Kibana wants contiguous ascending bands. We split the
    value axis at every threshold, pick the winning rule per segment (tightest
    matching bound; ties resolved by later list position), then coalesce
    adjacent same-color segments into bands.
    """
    rules: list[tuple[str, float, str]] = []
    thresholds: set[float] = set()
    for cf in conditional_formats or []:
        color = _palette_to_color(str(getattr(cf, "palette", "") or ""))
        comparator = str(getattr(cf, "comparator", "") or "")
        raw_value = getattr(cf, "value", None)
        if raw_value is None or isinstance(raw_value, bool):
            continue
        try:
            threshold = float(raw_value)
        except (TypeError, ValueError):
            continue
        if not color or comparator not in _CF_COMPARATORS:
            continue
        rules.append((comparator, threshold, color))
        thresholds.add(threshold)

    if not rules:
        return None
    boundaries = sorted(thresholds)

    # Half-open segments split by thresholds: (-inf, t0), [t0, t1), ..., [tn, inf).
    segments: list[tuple[float | None, float | None, float]] = [
        (None, boundaries[0], boundaries[0] - 1.0)
    ]
    for index, lower in enumerate(boundaries):
        upper = boundaries[index + 1] if index + 1 < len(boundaries) else None
        rep = lower + 1.0 if upper is None else (lower + upper) / 2.0
        segments.append((lower, upper, rep))

    def _winning_color(value: float) -> str | None:
        best_color: str | None = None
        best_key: float | None = None
        for comparator, threshold, color in rules:
            if not _cf_matches(value, comparator, threshold):
                continue
            key = _cf_specificity(comparator, threshold)
            if best_key is None or key >= best_key:
                best_key = key
                best_color = color
        return best_color

    resolved: list[tuple[float | None, float | None, str | None]] = [
        (lower, upper, _winning_color(rep)) for lower, upper, rep in segments
    ]

    bands: list[dict[str, Any]] = []
    for band_lower, band_upper, band_color in resolved:
        if band_color is None:
            continue
        if bands and bands[-1]["color"] == band_color and bands[-1]["_hi"] == band_lower:
            bands[-1]["_hi"] = band_upper
        else:
            bands.append({"_lo": band_lower, "_hi": band_upper, "color": band_color})

    if not bands:
        return None

    # Thresholds an inclusive ``<=`` rule keeps in the band *below* the
    # boundary. The target mapper (``dashboards_api._api_color``) turns each
    # ``up_to`` into the *next* band's exclusive ``lt`` edge, so without a
    # nudge the boundary value would fall into the wrong (upper) band; add a
    # negligible epsilon so it stays governed by ``lt=up_to+eps`` and lands in
    # this (lower, inclusive) band instead. Strict ``>``/``<`` rules keep the
    # boundary on the severe (upper) side, which the plain ``up_to`` already
    # expresses.
    inclusive_upper = {threshold for comparator, threshold, _ in rules if comparator == "<="}
    epsilon = 1e-9

    # ``ColorThreshold.up_to`` requires a real number for every band (the YAML
    # schema has no "open-ended" marker), so an unbounded top band needs a
    # finite placeholder. It doesn't distort the rendered colors: the target
    # mapper only uses a threshold's ``up_to`` to derive the *next* band's
    # lower edge — the last band's own ``up_to`` is never turned into an upper
    # bound, so any number at or above the highest configured rule is a safe
    # stand-in. This mirrors the ascending ``up_to``/``color`` shape Grafana
    # gauge thresholds use (``grafana.panels._build_gauge_color_mapping``).
    highest_rule_value = max(threshold for _, threshold, _ in rules)
    color_thresholds: list[dict[str, Any]] = []
    for band in bands:
        hi = band["_hi"]
        if hi is None:
            up_to = max(highest_rule_value, band["_lo"] or 0.0)
        elif hi in inclusive_upper and _winning_color(hi) == band["color"]:
            up_to = hi + epsilon
        else:
            up_to = hi
        # The schema requires ``thresholds`` strictly ascending. A real
        # boundary can legitimately coincide with (or, via the epsilon nudge
        # above, sit just past) the previous band's cutoff — e.g. three
        # overlapping Datadog rules (">0", ">=1", "<1") resolve to adjacent
        # bands that both cut at 1. Nudge instead of dropping so every
        # resolved band still gets a threshold entry.
        if color_thresholds and up_to <= color_thresholds[-1]["up_to"]:
            prev = color_thresholds[-1]["up_to"]
            up_to = prev + (abs(prev) * 1e-6 or 1e-6)
        color_thresholds.append({"up_to": up_to, "color": band["color"]})

    color_config: dict[str, Any] = {"thresholds": color_thresholds}
    range_min = bands[0]["_lo"]
    if range_min is not None:
        color_config["range_min"] = range_min
    range_max = bands[-1]["_hi"]
    if range_max is not None:
        color_config["range_max"] = range_max
    return color_config


def _apply_conditional_formats(
    esql: dict[str, Any],
    widget: NormalizedWidget,
    kibana_type: str,
) -> None:
    """Attach a dynamic color-by-value derived from Datadog conditional formats.

    Only roles whose Dashboards API color schema accepts a dynamic color are
    targeted: metric ``primary`` (query_value) and table ``metrics`` accept
    color-by-value; other chart families (pie/treemap/waffle/xy) reject it and
    the target mapper drops it there, so we do not emit it for them.
    """
    color = _conditional_format_color(widget.conditional_formats)
    if not color:
        return
    # Datadog conditional-format palettes (e.g. ``white_on_red``) are
    # background colors by definition, so both roles apply to the cell/value
    # background rather than just the text.
    if kibana_type == "metric":
        primary = esql.get("primary")
        if isinstance(primary, dict):
            primary.setdefault("color", {**color, "apply_to": "background"})
    elif kibana_type == "table":
        for metric in esql.get("metrics", []) or []:
            if isinstance(metric, dict):
                metric.setdefault("color", {**color, "apply_to": "cell"})


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
    fmt = DATADOG_UNIT_MAP.get(unit.lower()) or {"type": "number", "suffix": f" {unit}"}
    fmt = dict(fmt)
    if widget.precision is not None and widget.precision >= 0:
        fmt["decimals"] = widget.precision
    return fmt


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
        title = sanitize_axis_title_text(label)
        if title:
            y_cfg["title"] = title
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
