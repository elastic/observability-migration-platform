"""Grafana display metadata to kb-dashboard YAML schema mapping.

Handles: units/format, legend, axis config, metric labels, title cleanup.
"""

from __future__ import annotations

import re
from typing import Any

GRAFANA_UNIT_TO_YAML: dict[str, dict[str, Any]] = {
    "percent": {"type": "number", "suffix": "%"},
    "percentunit": {"type": "percent"},
    "bytes": {"type": "bytes"},
    "decbytes": {"type": "bytes"},
    "kbytes": {"type": "bytes"},
    "mbytes": {"type": "bytes"},
    "gbytes": {"type": "bytes"},
    "tbytes": {"type": "bytes"},
    "bits": {"type": "bits"},
    "decbits": {"type": "bits"},
    "kbits": {"type": "bits"},
    "mbits": {"type": "bits"},
    "gbits": {"type": "bits"},
    "Bps": {"type": "bytes", "suffix": "/s"},
    "bps": {"type": "bits", "suffix": "/s"},
    "binBps": {"type": "bytes", "suffix": "/s"},
    "KBs": {"type": "bytes", "suffix": "/s"},
    "MBs": {"type": "bytes", "suffix": "/s"},
    "GBs": {"type": "bytes", "suffix": "/s"},
    "Kbits": {"type": "bits", "suffix": "/s"},
    "Mbits": {"type": "bits", "suffix": "/s"},
    "Gbits": {"type": "bits", "suffix": "/s"},
    "s": {"type": "duration"},
    "ms": {"type": "number", "suffix": " ms", "decimals": 1},
    "µs": {"type": "number", "suffix": " µs", "decimals": 0},
    "ns": {"type": "number", "suffix": " ns", "decimals": 0},
    "m": {"type": "number", "suffix": " min"},
    "h": {"type": "number", "suffix": " h"},
    "d": {"type": "number", "suffix": " d"},
    "dtdurationms": {"type": "number", "suffix": " ms"},
    "dtdurations": {"type": "duration"},
    "dthms": {"type": "duration"},
    "short": {"type": "number", "compact": True},
    "iops": {"type": "number", "suffix": " iops"},
    "pps": {"type": "number", "suffix": " pps"},
    "reqps": {"type": "number", "suffix": " req/s"},
    "ops": {"type": "number", "suffix": " ops/s"},
    "opm": {"type": "number", "suffix": " ops/min"},
    "rps": {"type": "number", "suffix": " req/s"},
    "rpm": {"type": "number", "suffix": " req/min"},
    "cps": {"type": "number", "suffix": " conn/s"},
    "cpm": {"type": "number", "suffix": " conn/min"},
    "wps": {"type": "number", "suffix": " writes/s"},
    "rds": {"type": "number", "suffix": " reads/s"},
    "mps": {"type": "number", "suffix": " msg/s"},
    "eps": {"type": "number", "suffix": " events/s"},
    "hertz": {"type": "number", "suffix": " Hz"},
    "mhertz": {"type": "number", "suffix": " MHz"},
    "ghertz": {"type": "number", "suffix": " GHz"},
    "celsius": {"type": "number", "suffix": " °C"},
    "fahrenheit": {"type": "number", "suffix": " °F"},
    "kelvin": {"type": "number", "suffix": " K"},
    "humidity": {"type": "number", "suffix": " %H"},
    "pressurembar": {"type": "number", "suffix": " mbar"},
    "pressurehpa": {"type": "number", "suffix": " hPa"},
    "pressureatm": {"type": "number", "suffix": " atm"},
    "pressurepsi": {"type": "number", "suffix": " psi"},
    "watt": {"type": "number", "suffix": " W"},
    "kwatt": {"type": "number", "suffix": " kW"},
    "mwatt": {"type": "number", "suffix": " mW"},
    "voltamp": {"type": "number", "suffix": " VA"},
    "volt": {"type": "number", "suffix": " V"},
    "amp": {"type": "number", "suffix": " A"},
    "mamp": {"type": "number", "suffix": " mA"},
    "kwatth": {"type": "number", "suffix": " kWh"},
    "watth": {"type": "number", "suffix": " Wh"},
    "joule": {"type": "number", "suffix": " J"},
    "dBm": {"type": "number", "suffix": " dBm"},
    "dB": {"type": "number", "suffix": " dB"},
    "locale": {"type": "number"},
    "none": {"type": "number"},
    "bool": {"type": "number"},
    "bool_yes_no": {"type": "number"},
    "bool_on_off": {"type": "number"},
}


def extract_grafana_unit(panel: dict) -> str:
    """Return the primary Grafana unit string from *fieldConfig* or legacy *yaxes*."""
    defaults = _field_defaults(panel)
    unit = defaults.get("unit", "")
    if unit:
        return str(unit)
    for axis in (panel.get("yaxes") or []):
        if isinstance(axis, dict):
            fmt = axis.get("format", "")
            if fmt and fmt != "short":
                return str(fmt)
    for axis in (panel.get("yaxes") or []):
        if isinstance(axis, dict) and axis.get("format"):
            return str(axis["format"])
    return ""


def grafana_unit_to_yaml_format(unit: str) -> dict[str, Any] | None:
    """Map a Grafana unit string to a YAML ``format`` dict, or *None*."""
    if not unit or unit == "none":
        return None
    return GRAFANA_UNIT_TO_YAML.get(unit)


def extract_legend_config(panel: dict) -> dict[str, Any] | None:
    """Derive ``legend`` dict for the YAML from Grafana's legend settings."""
    options = panel.get("options") or {}
    modern = options.get("legend") or {}
    if modern:
        display_mode = str(modern.get("displayMode", "")).lower()
        shown = display_mode != "hidden" and bool(modern.get("showLegend", True))
        placement = str(modern.get("placement", "bottom"))
        if placement not in ("top", "bottom", "left", "right"):
            placement = "bottom"
        return {
            "visible": shown,
            "visible_str": "show" if shown else "hide",
            "position": placement,
        }

    legacy = panel.get("legend") or {}
    if isinstance(legacy, dict) and legacy:
        shown = bool(legacy.get("show", True))
        position = "right" if legacy.get("rightSide") else "bottom"
        return {
            "visible": shown,
            "visible_str": "show" if shown else "hide",
            "position": position,
        }

    return None


def extract_axis_config(panel: dict) -> dict[str, Any] | None:
    """Derive ``appearance`` dict (axes) for XY charts from Grafana's config."""
    appearance: dict[str, Any] = {}
    defaults = _field_defaults(panel)
    custom = defaults.get("custom") or {}

    axis_label = str(custom.get("axisLabel", "") or "").strip()
    if not axis_label:
        for axis in (panel.get("yaxes") or []):
            if isinstance(axis, dict):
                label = str(axis.get("label") or "").strip()
                if label:
                    axis_label = label
                    break

    if axis_label:
        appearance.setdefault("y_left_axis", {})["title"] = axis_label

    has_log = False
    scale_dist = custom.get("scaleDistribution") or {}
    if isinstance(scale_dist, dict) and scale_dist.get("type") == "log":
        has_log = True
    if not has_log:
        for axis in (panel.get("yaxes") or []):
            if isinstance(axis, dict) and (axis.get("logBase") or 0) > 1:
                has_log = True
                break
    if has_log:
        appearance.setdefault("y_left_axis", {})["scale"] = "log"

    y_min = _first_axis_bound(panel, "min", defaults)
    y_max = _first_axis_bound(panel, "max", defaults)
    if y_min is not None and y_max is not None:
        extent: dict[str, Any] = {"mode": "custom", "min": y_min, "max": y_max}
        appearance.setdefault("y_left_axis", {})["extent"] = extent

    yaxes = panel.get("yaxes") or []
    if len(yaxes) >= 2 and isinstance(yaxes[1], dict):
        right = yaxes[1]
        right_label = str(right.get("label") or "").strip()
        right_show = right.get("show", True)
        if right_show and right_label:
            appearance.setdefault("y_right_axis", {})["title"] = right_label
        right_log = (right.get("logBase") or 0) > 1
        if right_show and right_log:
            appearance.setdefault("y_right_axis", {})["scale"] = "log"

    return appearance if appearance else None


def _first_axis_bound(panel: dict, key: str, defaults: dict) -> float | None:
    val = _coerce_number(defaults.get(key))
    if val is not None:
        return val
    for axis in (panel.get("yaxes") or []):
        if isinstance(axis, dict):
            val = _coerce_number(axis.get(key))
            if val is not None:
                return val
    return None


_TEMPLATE_VAR_RE = re.compile(
    r"\$\{[^}]+\}"
    r"|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\{\{[^}]*\}\}"
    r"|\[\[[^\]]*\]\]"
)


_TRAILING_PREPOSITION_RE = re.compile(
    r"\s+(?:for|by|on|in|of|per|from|to|at|with|via|vs)\s*$", re.IGNORECASE
)


def clean_template_variables(text: str) -> str:
    """Strip Grafana template-variable syntax (``$var``, ``{{var}}``, etc.)."""
    if not text or not _TEMPLATE_VAR_RE.search(text):
        return text
    cleaned = _TEMPLATE_VAR_RE.sub("", text)
    cleaned = re.sub(r"[【\[\(（][^】\]）)]*[：:][^】\]）)]*[】\]）)]", " ", cleaned)
    cleaned = re.sub(r"[【\[（(]\s*[】\]）)]", "", cleaned)
    cleaned = re.sub(r"\s*[,;:：，]+\s*(?=[】\]）)])", " ", cleaned)
    cleaned = re.sub(r"\s*[,;:：，]+\s*$", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"^\s*[,;:：，\-–—]+\s*", "", cleaned)
    cleaned = re.sub(r"\s*[,;:：，\-–—]+\s*$", "", cleaned)
    cleaned = _TRAILING_PREPOSITION_RE.sub("", cleaned)
    return cleaned.strip()


_LEGEND_TEMPLATE_RE = re.compile(r"\{\{[^}]*\}\}")


def humanize_metric_label(field_name: str, legend_format: str | None = None) -> str | None:
    """Derive a human-readable label for a YAML metric field."""
    if legend_format:
        label = clean_template_variables(_LEGEND_TEMPLATE_RE.sub("", legend_format).strip())
        label = re.sub(r"^[\s\-–—:,;]+|[\s\-–—:,;]+$", "", label)
        if label and len(label) > 1:
            return label.strip()

    if not field_name:
        return None
    if re.fullmatch(r"[a-zA-Z]+", field_name):
        return None
    text = re.sub(r"_+", " ", field_name).strip()
    text = re.sub(r"\s{2,}", " ", text)
    if not text or text.lower() in ("series", "value", "metric", "time bucket"):
        return None
    parts = text.split()
    label = " ".join(p if p.isupper() else p.capitalize() for p in parts)
    return label if label != field_name else None


def enrich_yaml_panel_display(
    yaml_panel: dict,
    grafana_panel: dict,
    *,
    metric_labels: dict[str, str] | None = None,
) -> None:
    """Add display metadata to *yaml_panel* in-place."""
    esql = yaml_panel.get("esql")
    if not esql or not isinstance(esql, dict):
        return

    chart_type = esql.get("type", "")

    unit = extract_grafana_unit(grafana_panel)
    fmt = grafana_unit_to_yaml_format(unit)

    _apply_metric_format_and_label(esql, "metrics", fmt, metric_labels)
    _apply_metric_format_and_label(esql, "breakdowns", None, None)

    if "primary" in esql and isinstance(esql["primary"], dict):
        if fmt:
            esql["primary"].setdefault("format", dict(fmt))
        cleaned_title = str(yaml_panel.get("title") or "").strip()
        if chart_type == "metric" and cleaned_title:
            esql["primary"].setdefault("label", cleaned_title)
            if esql["primary"].get("label") == cleaned_title:
                yaml_panel["hide_title"] = True
        else:
            label = _label_for_field(esql["primary"].get("field", ""), metric_labels)
            if label:
                esql["primary"].setdefault("label", label)

    if "metric" in esql and isinstance(esql["metric"], dict):
        if fmt:
            esql["metric"].setdefault("format", dict(fmt))
        cleaned_title = str(yaml_panel.get("title") or "").strip()
        if chart_type == "gauge" and cleaned_title:
            esql["metric"].setdefault("label", cleaned_title)
            if esql["metric"].get("label") == cleaned_title:
                yaml_panel["hide_title"] = True

    if chart_type in ("metric", "gauge") and "titles_and_text" not in esql:
        subtitle = str(grafana_panel.get("description") or "").strip()
        if subtitle:
            esql["titles_and_text"] = {"subtitle": subtitle}

    dim = esql.get("dimension")
    if isinstance(dim, dict) and dim.get("field") in ("time_bucket", "timestamp_bucket", "step"):
        dim.setdefault("label", "Time")

    if chart_type in ("line", "bar", "area"):
        legend = extract_legend_config(grafana_panel)
        if "legend" not in esql:
            legend_block: dict[str, Any] = {
                "visible": legend["visible_str"] if legend else "show",
                "position": legend.get("position", "right") if legend else "right",
                "truncate_labels": 1,
            }
            esql["legend"] = legend_block

        axis = extract_axis_config(grafana_panel)
        if axis and "appearance" not in esql:
            esql["appearance"] = axis

    if chart_type == "pie":
        legend = extract_legend_config(grafana_panel)
        if "legend" not in esql:
            esql["legend"] = {
                "visible": legend.get("visible_str", "auto") if legend else "auto",
                "truncate_labels": 1,
            }

    if chart_type == "heatmap" and "appearance" not in esql:
        esql["appearance"] = {"legend": {"visible": "show", "position": "right"}}


def _apply_metric_format_and_label(
    esql: dict,
    key: str,
    fmt: dict[str, Any] | None,
    metric_labels: dict[str, str] | None,
) -> None:
    entries = esql.get(key)
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        field = entry.get("field", "")
        if fmt and "format" not in entry:
            entry["format"] = dict(fmt)
        label = _label_for_field(field, metric_labels)
        if label and "label" not in entry:
            entry["label"] = label


def _label_for_field(
    field: str, metric_labels: dict[str, str] | None
) -> str | None:
    if metric_labels and field in metric_labels:
        return humanize_metric_label(field, metric_labels[field])
    return humanize_metric_label(field)


def _field_defaults(panel: dict) -> dict:
    defaults = ((panel or {}).get("fieldConfig") or {}).get("defaults") or {}
    return defaults if isinstance(defaults, dict) else {}


def _coerce_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
