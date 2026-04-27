# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import os
import re
from pathlib import Path
from typing import Any

import yaml

from observability_migration.targets.kibana.compile import _iter_leaf_panels

from .local_ai import request_structured_json

GENERIC_PANEL_TITLES = {
    "",
    "untitled",
    "panel",
    "chart",
    "graph",
    "table",
    "logs",
    "stat",
    "gauge",
}


def _humanize_identifier(raw: str) -> str:
    text = re.sub(r"[_\.]+", " ", str(raw or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Untitled"
    return " ".join(part if part.isupper() else part.capitalize() for part in text.split(" "))


def _is_slug_like(value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return True
    if "_" in value or "." in value:
        return True
    return bool(re.fullmatch(r"[a-z0-9\._:-]+", value))


def _metric_based_title(panel_result: Any) -> str:
    query_ir = getattr(panel_result, "query_ir", {}) or {}
    if not isinstance(query_ir, dict):
        return ""
    metric = str(query_ir.get("metric", "") or "").strip()
    if not metric:
        return ""
    base = _humanize_identifier(metric)
    range_func = str(query_ir.get("range_function", "") or "").lower()
    output_shape = str(query_ir.get("output_shape", "") or "").lower()
    if range_func in {"rate", "irate", "increase"} and "rate" not in base.lower():
        base = f"{base} Rate"
    if output_shape == "single_value":
        return base
    if output_shape == "event_rows":
        return f"{base} Events"
    return base


def _suggest_panel_title(panel_result: Any) -> str:
    current = str(getattr(panel_result, "title", "") or "").strip()
    lowered = current.lower()
    if lowered and lowered not in GENERIC_PANEL_TITLES and not _is_slug_like(current):
        return current
    if current and lowered not in GENERIC_PANEL_TITLES and _is_slug_like(current):
        return _humanize_identifier(current)
    if str(getattr(panel_result, "query_language", "") or "").lower() == "logql" and str(getattr(panel_result, "grafana_type", "") or "").lower() == "logs":
        return "Log Events"
    metric_title = _metric_based_title(panel_result)
    if metric_title:
        return metric_title
    panel_type = str(getattr(panel_result, "grafana_type", "") or "").lower()
    if panel_type in {"table", "table-old"}:
        return "Summary Table"
    if panel_type in {"stat", "singlestat", "gauge", "bargauge"}:
        return "Key Metric"
    if panel_type == "logs":
        return "Log Events"
    return current or "Untitled"


def _suggest_control_label(control: dict[str, Any]) -> str:
    label = str(control.get("label") or "").strip()
    field = str(control.get("field") or "").strip()
    if label and label != field and not _is_slug_like(label):
        return label
    return _humanize_identifier(label or field)


def _sanitize_short_text(value: str, fallback: str, max_length: int = 80) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = text[:max_length].strip()
    text = re.sub(r"[`*#]", "", text).strip()
    return text or fallback


def _emitted_panel_results(result: Any) -> list[Any]:
    emitted = getattr(result, "yaml_panel_results", None)
    if emitted:
        return list(emitted)
    return [
        panel_result
        for panel_result in (getattr(result, "panel_results", []) or [])
        if getattr(panel_result, "status", "") != "skipped"
    ]


def build_heuristic_polish_plan(result: Any, yaml_doc: dict[str, Any]) -> dict[str, Any]:
    dashboard = ((yaml_doc or {}).get("dashboards") or [{}])[0]
    leaf_panels = list(_iter_leaf_panels(dashboard.get("panels", []) or []))
    visible_panel_results = _emitted_panel_results(result)

    panel_titles = {}
    for idx, panel_result in enumerate(visible_panel_results):
        if idx >= len(leaf_panels):
            break
        suggested = _sanitize_short_text(_suggest_panel_title(panel_result), leaf_panels[idx].get("title", "") or "Untitled")
        current = str(leaf_panels[idx].get("title") or "").strip()
        if suggested and suggested != current:
            panel_titles[str(idx)] = suggested

    control_labels = {}
    for idx, control in enumerate(dashboard.get("controls", []) or []):
        suggested = _sanitize_short_text(_suggest_control_label(control), str(control.get("label") or "Filter"))
        current = str(control.get("label") or "").strip()
        if suggested and suggested != current:
            control_labels[str(idx)] = suggested

    return {
        "mode": "heuristic",
        "dashboard_title": _sanitize_short_text(str(dashboard.get("name") or ""), str(dashboard.get("name") or "")),
        "panel_titles": panel_titles,
        "control_labels": control_labels,
        "notes": [],
    }


def _local_ai_request(payload: dict[str, Any], endpoint: str, model: str, api_key: str = "", timeout: int = 20) -> dict[str, Any]:
    return request_structured_json(
        payload,
        endpoint,
        model,
        (
            "You polish dashboard metadata only. "
            "Do not change query semantics or invent context. "
            "Return exactly one JSON object with keys dashboard_title, panel_titles, control_labels, notes. "
            "Never output keys named dashboard, panels, controls, heuristic, input, or explanation. "
            "panel_titles and control_labels must be sparse objects keyed by index as strings and should include only changed items. "
            "When a heuristic title or label is already readable, keep it unchanged. "
            "Only shorten a heuristic title when the meaning stays identical and the shorter form is plainly better. "
            "Keep names terse, plain, and stable. "
            "notes must be empty unless you could not comply."
        ),
        api_key=api_key,
        timeout=timeout,
        max_tokens=500,
    )


def _validate_ai_polish(ai_output: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    if not any(key in ai_output for key in ("dashboard_title", "panel_titles", "control_labels", "notes")):
        nested = ai_output.get("heuristic")
        if isinstance(nested, dict):
            ai_output = nested

    result = {
        "mode": "local_ai",
        "dashboard_title": fallback.get("dashboard_title", ""),
        "panel_titles": {},
        "control_labels": {},
        "notes": [],
    }
    dashboard_title = _sanitize_short_text(ai_output.get("dashboard_title", ""), fallback.get("dashboard_title", ""))
    result["dashboard_title"] = dashboard_title or fallback.get("dashboard_title", "")
    for key, value in (ai_output.get("panel_titles", {}) or {}).items():
        result["panel_titles"][str(key)] = _sanitize_short_text(value, fallback.get("panel_titles", {}).get(str(key), "Untitled"))
    for key, value in (ai_output.get("control_labels", {}) or {}).items():
        result["control_labels"][str(key)] = _sanitize_short_text(value, fallback.get("control_labels", {}).get(str(key), "Filter"))
    for note in (ai_output.get("notes", []) or []):
        note_text = _sanitize_short_text(note, "", max_length=120)
        if note_text and any(token in note_text.lower() for token in ("could not", "unable", "insufficient", "missing", "unclear")):
            result["notes"].append(note_text)
    return result


def _build_ai_payload(result: Any, yaml_doc: dict[str, Any], heuristic: dict[str, Any]) -> dict[str, Any]:
    dashboard = ((yaml_doc or {}).get("dashboards") or [{}])[0]
    visible_panel_results = _emitted_panel_results(result)
    return {
        "dashboard_title": dashboard.get("name", ""),
        "dashboard_description": dashboard.get("description", ""),
        "heuristic_dashboard_title": heuristic.get("dashboard_title", ""),
        "panels": [
            {
                "index": idx,
                "source_title": getattr(panel_result, "title", ""),
                "heuristic_title": heuristic.get("panel_titles", {}).get(str(idx), getattr(panel_result, "title", "")),
                "grafana_type": getattr(panel_result, "grafana_type", ""),
                "query_language": getattr(panel_result, "query_language", ""),
                "metric": (getattr(panel_result, "query_ir", {}) or {}).get("metric", ""),
                "range_function": (getattr(panel_result, "query_ir", {}) or {}).get("range_function", ""),
                "output_shape": (getattr(panel_result, "query_ir", {}) or {}).get("output_shape", ""),
                "notes": list(getattr(panel_result, "notes", []) or [])[:2],
            }
            for idx, panel_result in enumerate(visible_panel_results)
        ],
        "controls": [
            {
                "index": idx,
                "current_label": control.get("label", ""),
                "heuristic_label": heuristic.get("control_labels", {}).get(str(idx), control.get("label", "")),
                "field": control.get("field", ""),
                "type": control.get("type", ""),
            }
            for idx, control in enumerate(dashboard.get("controls", []) or [])
        ],
    }


def apply_metadata_polish(
    yaml_path: str | Path,
    result: Any,
    enable_ai: bool = False,
    ai_endpoint: str = "",
    ai_model: str = "",
    ai_api_key: str = "",
    timeout: int = 20,
) -> dict[str, Any]:
    yaml_path = Path(yaml_path)
    yaml_doc = yaml.safe_load(yaml_path.read_text())
    dashboard = (yaml_doc.get("dashboards") or [{}])[0]
    visible_panel_results = _emitted_panel_results(result)

    heuristic = build_heuristic_polish_plan(result, yaml_doc)
    applied = dict(heuristic)

    if enable_ai:
        endpoint = ai_endpoint or os.getenv("LOCAL_AI_ENDPOINT") or os.getenv("OPENAI_BASE_URL", "")
        model = ai_model or os.getenv("LOCAL_AI_MODEL") or os.getenv("OPENAI_MODEL", "")
        token = ai_api_key or os.getenv("LOCAL_AI_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        if endpoint and model:
            try:
                payload = _build_ai_payload(result, yaml_doc, heuristic)
                ai_output = _local_ai_request(payload, endpoint, model, api_key=token, timeout=timeout)
                applied = _validate_ai_polish(ai_output, heuristic)
            except Exception as exc:  # pragma: no cover - exercised only with live local AI
                applied = dict(heuristic)
                applied["notes"] = list(applied.get("notes", [])) + [f"Local AI metadata polish failed: {exc}"]
        else:
            applied = dict(heuristic)
            applied["notes"] = list(applied.get("notes", [])) + ["Local AI metadata polish requested but endpoint/model were not configured"]

    leaf_panels = list(_iter_leaf_panels(dashboard.get("panels", []) or []))
    for key, value in (applied.get("panel_titles", {}) or {}).items():
        idx = int(key)
        if idx >= len(leaf_panels) or idx >= len(visible_panel_results):
            continue
        leaf_panels[idx]["title"] = value
        panel_result = visible_panel_results[idx]
        panel_result.metadata_polish = {
            "mode": applied.get("mode", "heuristic"),
            "original_title": panel_result.title,
            "final_title": value,
        }
        panel_result.title = value
        if hasattr(panel_result.visual_ir, "title"):
            panel_result.visual_ir.title = value
        elif isinstance(panel_result.visual_ir, dict):
            panel_result.visual_ir["title"] = value

    for key, value in (applied.get("control_labels", {}) or {}).items():
        idx = int(key)
        if idx >= len(dashboard.get("controls", []) or []):
            continue
        dashboard["controls"][idx]["label"] = value

    with yaml_path.open("w") as fh:
        yaml.safe_dump(yaml_doc, fh, sort_keys=False, allow_unicode=True, width=120)

    result.metadata_polish = {
        "mode": applied.get("mode", "heuristic"),
        "dashboard_title": dashboard.get("name", ""),
        "panel_titles": applied.get("panel_titles", {}),
        "control_labels": applied.get("control_labels", {}),
        "notes": applied.get("notes", []),
    }
    return result.metadata_polish
