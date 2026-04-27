# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import hashlib
import json
import os
import re
from typing import Any

from .local_ai import request_structured_json

REVIEW_AI_BATCH_SIZE = 12
REVIEW_AI_REASON_TOKENS = (
    "approximat",
    "repeat",
    "library panel",
    "mixed datasource",
    "transformation",
    "drilldown",
    "link",
    "semantic loss",
    "merged compatible targets",
    "fallback",
    "counter-compatible",
    "manual redesign",
)


def _sanitize_short_text(value: str, fallback: str = "", max_length: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"[`*#]", "", text).strip()
    text = text[:max_length].strip()
    return text or fallback


def _first_text(values: list[Any], fallback: str = "") -> str:
    for value in values or []:
        text = _sanitize_short_text(str(value or ""), "")
        if text:
            return text
    return fallback


def _suggested_checks(panel_result: Any, verification_packet: dict[str, Any]) -> list[str]:
    checks: list[str] = []
    validation = dict(verification_packet.get("validation", {}) or {})
    analysis = dict(validation.get("analysis", {}) or {})
    status = str(getattr(panel_result, "status", "") or "").lower()
    source_query = str(getattr(panel_result, "promql_expr", "") or "")

    if status == "skipped":
        return ["No review needed unless you want to recreate the skipped structural element manually."]

    placeholder_action = str(getattr(panel_result, "post_validation_action", "") or "")
    if placeholder_action == "placeholder_empty_result":
        narrowed_to = _sanitize_short_text(analysis.get("narrowed_to_index", ""), "", max_length=120)
        if narrowed_to:
            checks.append(f"Confirm whether `{narrowed_to}` is only lab data or is missing production rows.")
        checks.append("Verify the original Grafana metric exists in the intended target metrics streams.")
    elif placeholder_action.startswith("placeholder_"):
        checks.append("Review the live validation error and redesign or remap this panel before promotion.")

    if analysis.get("unknown_columns"):
        first_unknown = analysis["unknown_columns"][0]["name"]
        checks.append(f"Check field mapping for `{first_unknown}` in the target Elasticsearch schema.")

    if analysis.get("counter_mismatch_metrics"):
        metric_name = analysis["counter_mismatch_metrics"][0]
        checks.append(f"Inspect whether `{metric_name}` is mapped as a counter-compatible time-series field.")

    if "histogram_quantile" in source_query:
        checks.append("Review the histogram bucket semantics and redesign the panel manually.")

    if "__name__" in source_query:
        checks.append("Replace metric-name introspection with an explicit inventory or redesign step.")

    losses = list(verification_packet.get("known_semantic_losses", []) or [])
    if losses:
        checks.append("Review the listed semantic losses before promoting this dashboard.")

    if not checks:
        if status == "migrated":
            checks.append("Perform a quick visual review in Kibana to confirm labels and panel rendering.")
        else:
            checks.append("Review this panel in Kibana before promoting it.")

    deduped: list[str] = []
    for item in checks:
        sanitized = _sanitize_short_text(item, "", max_length=140)
        if sanitized and sanitized not in deduped:
            deduped.append(sanitized)
    return deduped[:3]


def build_heuristic_review_explanation(panel_result: Any, verification_packet: dict[str, Any]) -> dict[str, Any]:
    gate = str(verification_packet.get("semantic_gate", "") or "")
    validation_status = str(verification_packet.get("validation_status", "") or "")
    status = str(getattr(panel_result, "status", "") or "")
    reasons = list(getattr(panel_result, "reasons", []) or [])
    notes = list(getattr(panel_result, "notes", []) or [])
    losses = list(verification_packet.get("known_semantic_losses", []) or [])
    validation = dict(verification_packet.get("validation", {}) or {})
    analysis = dict(validation.get("analysis", {}) or {})

    if status == "skipped":
        summary = "This panel was intentionally skipped by the translator."
    elif status == "not_feasible":
        summary = _sanitize_short_text(
            f"Manual redesign required. {_first_text(reasons, 'The current translation flow cannot preserve this panel safely.')}",
            "Manual redesign required.",
        )
    elif getattr(panel_result, "post_validation_action", "") == "placeholder_empty_result":
        narrowed_to = _sanitize_short_text(analysis.get("narrowed_to_index", ""), "", max_length=120)
        suffix = f" after narrowing to `{narrowed_to}`" if narrowed_to else " after narrowing to a fallback data stream"
        summary = _sanitize_short_text(
            f"Manual review required. Validation only succeeded{suffix}, and that query returned no rows.",
            "Manual review required after validation fallback returned no rows.",
        )
    elif getattr(panel_result, "post_validation_action", "") == "placeholder_validation_failure":
        summary = _sanitize_short_text(
            f"Manual review required. Live ES|QL validation failed before upload. {_sanitize_short_text(validation.get('error', ''), 'Inspect the validation error and remap the panel.')}",
            "Manual review required after live validation failed.",
        )
    elif status == "requires_manual":
        summary = _sanitize_short_text(
            f"Manual review required. {_first_text(reasons + notes, 'The current output needs reviewer intervention before promotion.')}",
            "Manual review required.",
        )
    elif validation_status == "fail":
        summary = _sanitize_short_text(
            f"Runtime validation failed. {_sanitize_short_text(validation.get('error', ''), 'Inspect the Elasticsearch validation error.')}",
            "Runtime validation failed.",
        )
    elif validation_status == "fixed":
        summary = _sanitize_short_text(
            "Translation needed a deterministic validation fix before it could run. Review the adjusted target query carefully.",
            "Translation required a deterministic validation fix.",
        )
    elif gate == "Yellow":
        summary = _sanitize_short_text(
            f"Review recommended. {_first_text(losses + reasons + notes, 'The panel migrated, but the translator recorded warnings or semantic losses.')}",
            "Review recommended for a warning-level migration.",
        )
    else:
        summary = "Translation validated cleanly and no major semantic risk was detected."

    return {
        "mode": "heuristic",
        "summary": summary,
        "suggested_checks": _suggested_checks(panel_result, verification_packet),
        "notes": [],
    }


def _local_ai_request(payload: dict[str, Any], endpoint: str, model: str, api_key: str = "", timeout: int = 20) -> dict[str, Any]:
    return request_structured_json(
        payload,
        endpoint,
        model,
        (
            "You explain migration review risks only. "
            "Use only facts from the payload. "
            "Do not invent missing facts, speculate, or change query semantics. "
            "Return exactly one JSON object with keys summary, suggested_checks, notes. "
            "Never output keys named dashboard, panel, heuristic, reasons, validation, candidate_targets, or input. "
            "summary must be one short sentence under 18 words. "
            "suggested_checks must contain at most two short imperative strings. "
            "notes must be empty unless you could not comply. "
            "Be terse and operational."
        ),
        api_key=api_key,
        timeout=timeout,
        max_tokens=220,
    )


def _validate_ai_review(ai_output: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    if not any(key in ai_output for key in ("summary", "suggested_checks", "notes")):
        nested = ai_output.get("heuristic")
        if isinstance(nested, dict):
            ai_output = nested

    return {
        "mode": "local_ai",
        "summary": _sanitize_short_text(ai_output.get("summary", ""), fallback.get("summary", ""), max_length=220),
        "suggested_checks": [
            _sanitize_short_text(item, "", max_length=140)
            for item in (ai_output.get("suggested_checks", []) or [])
            if _sanitize_short_text(item, "", max_length=140)
        ][:2] or list(fallback.get("suggested_checks", []) or [])[:2],
        "notes": [
            _sanitize_short_text(item, "", max_length=140)
            for item in (ai_output.get("notes", []) or [])
            if _sanitize_short_text(item, "", max_length=140)
            and any(
                token in _sanitize_short_text(item, "", max_length=140).lower()
                for token in ("could not", "unable", "insufficient", "missing", "unclear")
            )
        ][:2],
    }


def _build_ai_payload(panel_result: Any, verification_packet: dict[str, Any], heuristic: dict[str, Any]) -> dict[str, Any]:
    validation = dict(verification_packet.get("validation", {}) or {})
    analysis = dict(validation.get("analysis", {}) or {})
    return {
        "dashboard_title": verification_packet.get("dashboard", ""),
        "panel_title": verification_packet.get("panel", ""),
        "status": getattr(panel_result, "status", ""),
        "semantic_gate": verification_packet.get("semantic_gate", ""),
        "validation_status": verification_packet.get("validation_status", ""),
        "summary_hint": heuristic.get("summary", ""),
        "suggested_checks_hint": list(heuristic.get("suggested_checks", []) or [])[:2],
        "source_query": getattr(panel_result, "promql_expr", ""),
        "translated_query": getattr(panel_result, "esql_query", ""),
        "primary_reason": _first_text(list(getattr(panel_result, "reasons", []) or []), ""),
        "panel_notes": list(getattr(panel_result, "notes", []) or [])[:2],
        "semantic_losses": list(verification_packet.get("known_semantic_losses", []) or []),
        "validation_error": validation.get("error", ""),
        "unknown_columns": [item.get("name", "") for item in (analysis.get("unknown_columns", []) or [])[:3]],
        "counter_mismatch_metrics": list(analysis.get("counter_mismatch_metrics", []) or [])[:3],
        "narrowed_to_index": analysis.get("narrowed_to_index", ""),
        "candidate_targets": list(verification_packet.get("candidate_targets", []) or []),
    }


def _local_ai_batch_request(
    items: list[dict[str, Any]],
    endpoint: str,
    model: str,
    api_key: str = "",
    timeout: int = 20,
) -> dict[str, dict[str, Any]]:
    response = request_structured_json(
        {"items": items},
        endpoint,
        model,
        (
            "You explain migration review risks only. "
            "Use only facts from each input item. "
            "Return exactly one JSON object with key items. "
            "items must be an object keyed by each input id string. "
            "Each items value must be an object with keys summary, suggested_checks, notes. "
            "Never output keys outside items except the top-level items key. "
            "summary must be one short sentence under 18 words. "
            "suggested_checks must contain at most two short imperative strings. "
            "notes must be empty unless you could not comply. "
            "Stay close to summary_hint and suggested_checks_hint unless the payload gives a clearly better concise phrasing. "
            "Be terse and operational."
        ),
        api_key=api_key,
        timeout=timeout,
        max_tokens=max(400, min(2600, 220 * len(items))),
    )
    raw_items = response.get("items", response)
    if not isinstance(raw_items, dict):
        return {}
    return {
        str(key): value
        for key, value in raw_items.items()
        if isinstance(value, dict)
    }


def _should_use_ai(panel_result: Any, verification_packet: dict[str, Any], heuristic: dict[str, Any]) -> bool:
    gate = str(verification_packet.get("semantic_gate", "") or "")
    validation_status = str(verification_packet.get("validation_status", "") or "").lower()
    status = str(getattr(panel_result, "status", "") or "").lower()
    if gate == "Red" or status in {"requires_manual", "not_feasible"}:
        return True
    if validation_status in {"fixed", "fixed_empty", "fail"}:
        return True
    losses = list(verification_packet.get("known_semantic_losses", []) or [])
    if losses:
        return True
    if gate != "Yellow":
        return False

    evidence = " ".join(
        [
            str(heuristic.get("summary", "") or ""),
            " ".join(str(item) for item in (getattr(panel_result, "reasons", []) or [])),
            " ".join(str(item) for item in (getattr(panel_result, "notes", []) or [])),
        ]
    ).lower()
    return any(token in evidence for token in REVIEW_AI_REASON_TOKENS)


def _review_case_key(panel_result: Any, verification_packet: dict[str, Any], heuristic: dict[str, Any]) -> str:
    validation = dict(verification_packet.get("validation", {}) or {})
    analysis = dict(validation.get("analysis", {}) or {})
    normalized = {
        "status": str(getattr(panel_result, "status", "") or "").lower(),
        "semantic_gate": str(verification_packet.get("semantic_gate", "") or ""),
        "validation_status": str(verification_packet.get("validation_status", "") or ""),
        "summary_hint": _sanitize_short_text(heuristic.get("summary", ""), "", max_length=160),
        "suggested_checks_hint": list(heuristic.get("suggested_checks", []) or [])[:2],
        "primary_reason": _first_text(list(getattr(panel_result, "reasons", []) or []), ""),
        "semantic_losses": sorted(str(item) for item in (verification_packet.get("known_semantic_losses", []) or [])),
        "validation_error": _sanitize_short_text(validation.get("error", ""), "", max_length=120),
        "unknown_columns": sorted(item.get("name", "") for item in (analysis.get("unknown_columns", []) or [])[:3]),
        "counter_mismatch_metrics": sorted(str(item) for item in (analysis.get("counter_mismatch_metrics", []) or [])[:3]),
        "narrowed_to_index": str(analysis.get("narrowed_to_index", "") or ""),
        "candidate_targets": [
            f"{item.get('target', '')}:{item.get('disposition', '')}"
            for item in (verification_packet.get("candidate_targets", []) or [])[:3]
        ],
        "query_markers": {
            "histogram_quantile": "histogram_quantile" in str(getattr(panel_result, "promql_expr", "") or ""),
            "metric_name_introspection": "__name__" in str(getattr(panel_result, "promql_expr", "") or ""),
        },
    }
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _copy_explanation(explanation: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": explanation.get("mode", "heuristic"),
        "summary": explanation.get("summary", ""),
        "suggested_checks": list(explanation.get("suggested_checks", []) or []),
        "notes": list(explanation.get("notes", []) or []),
    }


def _batched(items: list[str], size: int) -> list[list[str]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def apply_review_explanations(
    results: list[Any],
    verification_payload: dict[str, Any],
    enable_ai: bool = False,
    ai_endpoint: str = "",
    ai_model: str = "",
    ai_api_key: str = "",
    timeout: int = 20,
) -> dict[str, Any]:
    endpoint = ai_endpoint or os.getenv("LOCAL_AI_ENDPOINT") or os.getenv("OPENAI_BASE_URL", "")
    model = ai_model or os.getenv("LOCAL_AI_MODEL") or os.getenv("OPENAI_MODEL", "")
    token = ai_api_key or os.getenv("LOCAL_AI_API_KEY") or os.getenv("OPENAI_API_KEY", "")

    notes: list[str] = []
    ai_available = bool(enable_ai and endpoint and model)
    if enable_ai and not ai_available:
        notes.append("Local AI review explanations requested but endpoint/model were not configured")

    total_panels = 0
    ai_panels = 0
    heuristic_panels = 0
    ai_requests = 0
    unique_ai_cases = 0
    reused_panels = 0
    case_groups: dict[str, dict[str, Any]] = {}
    case_order: list[str] = []

    for result in results:
        for panel_result in getattr(result, "panel_results", []) or []:
            packet = getattr(panel_result, "verification_packet", {}) or {}
            heuristic = build_heuristic_review_explanation(panel_result, packet)
            if ai_available and _should_use_ai(panel_result, packet, heuristic):
                case_key = _review_case_key(panel_result, packet, heuristic)
                if case_key not in case_groups:
                    case_groups[case_key] = {
                        "payload": _build_ai_payload(panel_result, packet, heuristic),
                        "fallback": heuristic,
                        "members": [],
                    }
                    case_order.append(case_key)
                case_groups[case_key]["members"].append(panel_result)
            else:
                applied = _copy_explanation(heuristic)
                panel_result.review_explanation = applied
                if isinstance(panel_result.verification_packet, dict):
                    panel_result.verification_packet["review_explanation"] = applied
                heuristic_panels += 1
            total_panels += 1

    unique_ai_cases = len(case_order)
    for chunk in _batched(case_order, REVIEW_AI_BATCH_SIZE):
        ai_requests += 1
        chunk_items = [
            {"id": case_key, **case_groups[case_key]["payload"]}
            for case_key in chunk
        ]
        chunk_outputs: dict[str, dict[str, Any]] = {}
        chunk_error = ""
        try:
            chunk_outputs = _local_ai_batch_request(
                chunk_items,
                endpoint,
                model,
                api_key=token,
                timeout=timeout,
            )
        except Exception as exc:  # pragma: no cover - exercised only with live local AI
            chunk_error = _sanitize_short_text(f"Local AI reviewer batch failed: {exc}", "", max_length=140)
            notes.append(chunk_error)

        for case_key in chunk:
            group = case_groups[case_key]
            fallback = group["fallback"]
            raw_output = chunk_outputs.get(case_key)
            if raw_output:
                applied = _validate_ai_review(raw_output, fallback)
                ai_panels += len(group["members"])
                reused_panels += max(0, len(group["members"]) - 1)
            else:
                applied = _copy_explanation(fallback)
                if chunk_error:
                    applied["notes"] = list(applied.get("notes", [])) + [chunk_error]
                heuristic_panels += len(group["members"])

            for panel_result in group["members"]:
                panel_result.review_explanation = _copy_explanation(applied)
                if isinstance(panel_result.verification_packet, dict):
                    panel_result.verification_packet["review_explanation"] = _copy_explanation(applied)

    mode = "local_ai" if ai_panels and not heuristic_panels else "mixed" if ai_panels else "heuristic"
    verification_payload["review_explanations"] = {
        "mode": mode,
        "panels": total_panels,
        "ai_panels": ai_panels,
        "heuristic_panels": heuristic_panels,
        "ai_requests": ai_requests,
        "unique_ai_cases": unique_ai_cases,
        "reused_panels": reused_panels,
        "model": model if ai_available else "",
        "notes": notes,
    }
    return verification_payload["review_explanations"]
