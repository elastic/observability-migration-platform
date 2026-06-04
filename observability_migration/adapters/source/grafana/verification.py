# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from observability_migration.core.assets.operational import build_operational_ir
from observability_migration.core.reporting.report import build_runtime_summary
from observability_migration.core.verification.comparators import (
    build_comparison_result,
    build_sample_window,
    comparison_gate_override,
)

from .execution.source import build_source_execution_summary
from .execution.target import build_target_execution_summary

RUNTIME_ERROR_ROLLUPS = {
    "yaml_lint_failed",
    "compile_failed",
    "layout_failed",
    "upload_failed",
    "smoke_failed",
    "browser_failed",
}
RUNTIME_WARNING_ROLLUPS = {
    "empty_result",
    "not_runtime_checked",
    "source_execution_failed",
    "validation_skipped",
}


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _query_ir_dict(panel_result: Any) -> dict[str, Any]:
    query_ir = getattr(panel_result, "query_ir", {}) or {}
    return query_ir if isinstance(query_ir, dict) else {}


def _has_geo_fields(query_ir: dict[str, Any]) -> bool:
    fields = set(query_ir.get("output_group_fields", []) or [])
    fields.add(query_ir.get("output_metric_field", "") or "")
    geo_tokens = ("geo.", "location", "latitude", "longitude", "lat", "lon")
    return any(any(token in field.lower() for token in geo_tokens) for field in fields if field)


def build_target_candidates(panel_result: Any) -> list[dict[str, Any]]:
    query_ir = _query_ir_dict(panel_result)
    query_language = str(getattr(panel_result, "query_language", "") or "").lower()
    grafana_type = str(getattr(panel_result, "grafana_type", "") or "").lower()
    status = str(getattr(panel_result, "status", "") or "").lower()
    output_shape = str(query_ir.get("output_shape", "") or "").lower()
    candidates: list[dict[str, Any]] = []

    def add(target: str, rank: int, disposition: str, reason: str) -> None:
        candidates.append(
            {
                "target": target,
                "rank": rank,
                "disposition": disposition,
                "reason": reason,
            }
        )

    if status == "skipped":
        add("skip", 1, "ignored", "Panel is intentionally skipped by the translator")
        return candidates

    if status in {"not_feasible", "requires_manual"}:
        add("manual_redesign", 1, "required", "Current translation flow cannot preserve this panel safely")
        add("block", 2, "required", "Do not auto-promote this panel")
        return candidates

    if grafana_type == "text":
        add("markdown", 1, "preferred", "Text and documentation panels map best to Kibana markdown")
        add("manual_redesign", 2, "review", "Complex HTML formatting may still need manual cleanup")
        return candidates

    if query_language == "esql":
        add("native_esql_panel", 1, "preferred", "Source query is already expressed as ES|QL")
        if output_shape == "event_rows":
            add("discover_embed", 2, "fallback", "Discover can preserve event-table workflows for ES|QL results")
    elif query_language == "logql":
        if output_shape == "event_rows" or grafana_type == "logs":
            add("discover_embed", 1, "preferred", "Log/event workflows map best to Discover-style views")
            add("esql_table", 2, "fallback", "An ES|QL datatable remains a viable log migration target")
        else:
            add("esql_table", 1, "preferred", "Translated LogQL results are currently emitted as ES|QL tables")
        add("manual_redesign", 3, "review", "Structured ingest or panel redesign may still be required for full fidelity")
    else:
        if output_shape == "table" or grafana_type in {"table", "table-old"}:
            add("esql_table", 1, "preferred", "Tabular results map cleanly to ES|QL-backed tables")
        elif output_shape == "single_value" or grafana_type in {"stat", "singlestat", "gauge", "bargauge"}:
            add("native_metric_panel", 1, "preferred", "Single-value metrics fit metric-oriented Kibana panels")
            add("lens_fallback", 2, "fallback", "Lens can remain a fallback for presentation-specific needs")
        else:
            add("native_esql_panel", 1, "preferred", "Time-series results should target native ES|QL visualizations first")
            add("lens_fallback", 2, "fallback", "Lens remains a safe fallback when presentation tuning is needed")

    if _has_geo_fields(query_ir):
        add("maps", len(candidates) + 1, "optional", "Geo-oriented fields were detected in the translated result")

    if not any(item["target"] == "manual_redesign" for item in candidates):
        add("manual_redesign", len(candidates) + 1, "review", "Keep a redesign path for semantics the compiler cannot yet prove")
    return candidates


def _collect_semantic_losses(panel_result: Any) -> list[str]:
    losses: list[str] = []
    query_ir = _query_ir_dict(panel_result)
    for item in (query_ir.get("semantic_losses", []) or []):
        _append_unique(losses, str(item))
    for item in (getattr(panel_result, "reasons", []) or []):
        lowered = str(item).lower()
        if any(token in lowered for token in ("approximat", "drop", "manual", "fallback", "mixed datasource")):
            _append_unique(losses, str(item))
    for item in (getattr(panel_result, "notes", []) or []):
        lowered = str(item).lower()
        if any(token in lowered for token in ("manual", "verify", "not preserved", "redesign", "transformation", "link")):
            _append_unique(losses, str(item))
    return losses


def _field_mapping_summary(panel_result: Any) -> dict[str, Any]:
    query_ir = _query_ir_dict(panel_result)
    return {
        "target_index": query_ir.get("target_index", ""),
        "output_metric_field": query_ir.get("output_metric_field", ""),
        "output_group_fields": list(query_ir.get("output_group_fields", []) or []),
    }


def _runtime_rollups(
    panel_result: Any,
    validation_record: dict[str, Any] | None,
    dashboard_result: Any | None = None,
) -> list[str]:
    rollups: list[str] = []
    for item in getattr(panel_result, "runtime_rollups", []) or []:
        _append_unique(rollups, str(item))
    if getattr(panel_result, "post_validation_action", "") == "placeholder_empty_result":
        _append_unique(rollups, "empty_result")
    if validation_record and validation_record.get("status") == "fixed_empty":
        _append_unique(rollups, "empty_result")
    if dashboard_result:
        runtime_summary = build_runtime_summary(dashboard_result)
        if runtime_summary["yaml_lint"]["status"] == "fail":
            _append_unique(rollups, "yaml_lint_failed")
        if runtime_summary["compile"]["status"] == "fail":
            _append_unique(rollups, "compile_failed")
        if runtime_summary["layout"]["status"] == "fail":
            _append_unique(rollups, "layout_failed")
        if runtime_summary["upload"]["status"] == "fail":
            _append_unique(rollups, "upload_failed")
    return rollups


def _runtime_state(
    panel_result: Any,
    validation_record: dict[str, Any] | None,
    dashboard_result: Any | None = None,
) -> dict[str, Any]:
    dashboard_runtime = build_runtime_summary(dashboard_result) if dashboard_result else {}
    rollups = set(_runtime_rollups(panel_result, validation_record, dashboard_result))
    smoke_status = "fail" if "smoke_failed" in rollups else "empty_result" if "empty_result" in rollups else "not_runtime_checked" if "not_runtime_checked" in rollups else "not_run"
    browser_status = "fail" if "browser_failed" in rollups else "not_run"
    return {
        "yaml_lint": dashboard_runtime.get("yaml_lint", {"status": "not_run", "error": ""}),
        "compile": dashboard_runtime.get("compile", {"status": "not_run", "error": ""}),
        "layout": dashboard_runtime.get("layout", {"status": "not_run", "error": ""}),
        "upload": dashboard_runtime.get("upload", {"status": "not_run", "error": ""}),
        "validation": {
            "status": validation_record.get("status", "not_run") if validation_record else "not_run",
            "error": validation_record.get("error", "") if validation_record else "",
        },
        "smoke": {"status": smoke_status, "error": ""},
        "browser": {"status": browser_status, "error": ""},
    }


def _semantic_gate(
    panel_result: Any,
    validation_record: dict[str, Any] | None,
    semantic_losses: list[str],
    runtime_rollups: list[str],
    comparison: dict[str, Any] | None = None,
) -> str:
    status = str(getattr(panel_result, "status", "") or "").lower()
    if status in {"not_feasible", "requires_manual"}:
        return "Red"
    comparison_override = comparison_gate_override(comparison)
    if comparison_override == "Red":
        return "Red"
    if any(item in RUNTIME_ERROR_ROLLUPS for item in runtime_rollups):
        return "Red"
    if validation_record and validation_record.get("status") in {"fail", "fixed_empty"}:
        return "Red"
    if validation_record and validation_record.get("status") == "fixed":
        return "Yellow"
    if validation_record and validation_record.get("status") == "skip":
        return "Yellow"
    if comparison_override == "Yellow":
        return "Yellow"
    if any(item in RUNTIME_WARNING_ROLLUPS for item in runtime_rollups):
        return "Yellow"
    if status == "migrated_with_warnings" or semantic_losses:
        return "Yellow"
    return "Green"


def build_verification_packet(
    dashboard_title: str,
    panel_result: Any,
    validation_record: dict[str, Any] | None = None,
    dashboard_result: Any | None = None,
    *,
    prometheus_url: str = "",
    loki_url: str = "",
    verify: bool | str = True,
) -> dict[str, Any]:
    query_ir = _query_ir_dict(panel_result)
    candidates = build_target_candidates(panel_result)
    semantic_losses = _collect_semantic_losses(panel_result)
    runtime_rollups = _runtime_rollups(panel_result, validation_record, dashboard_result)
    runtime_state = _runtime_state(panel_result, validation_record, dashboard_result)
    sample_window = build_sample_window(query_ir, validation_record).to_dict()
    source_execution = build_source_execution_summary(
        panel_result, prometheus_url=prometheus_url, loki_url=loki_url, verify=verify,
    ).to_dict()
    target_execution = build_target_execution_summary(panel_result, validation_record).to_dict()
    comparison = build_comparison_result(source_execution, target_execution, query_ir, validation_record).to_dict()

    if source_execution.get("status") == "fail":
        _append_unique(runtime_rollups, "source_execution_failed")
    if validation_record and validation_record.get("status") == "skip":
        _append_unique(runtime_rollups, "validation_skipped")

    gate = _semantic_gate(panel_result, validation_record, semantic_losses, runtime_rollups, comparison=comparison)
    validation_status = validation_record.get("status", "not_run") if validation_record else "not_run"
    source_status = source_execution.get("status", "")
    target_status = target_execution.get("status", "")
    verification_mode = (
        "source_target_comparison"
        if source_status == "pass" and target_status in {"pass", "fixed"}
        else "source_validated"
        if source_status == "pass" and not validation_record
        else "runtime_validation"
        if validation_record
        else "static_analysis"
    )
    return {
        "dashboard": dashboard_title,
        "dashboard_uid": getattr(dashboard_result, "dashboard_uid", ""),
        "panel": getattr(panel_result, "title", ""),
        "source_panel_id": getattr(panel_result, "source_panel_id", ""),
        "status": getattr(panel_result, "status", ""),
        "semantic_gate": gate,
        "verification_mode": verification_mode,
        "validation_status": validation_status,
        "source_language": getattr(panel_result, "query_language", ""),
        "source_query": getattr(panel_result, "promql_expr", ""),
        "translated_query": getattr(panel_result, "esql_query", ""),
        "field_mappings_used": _field_mapping_summary(panel_result),
        "candidate_targets": candidates,
        "recommended_target": candidates[0]["target"] if candidates else "",
        "known_semantic_losses": semantic_losses,
        "runtime_rollups": runtime_rollups,
        "runtime_state": runtime_state,
        "sample_window": sample_window,
        "source_execution": source_execution,
        "target_execution": target_execution,
        "comparison": comparison,
        "query_ir": query_ir,
        "validation": {
            "query": (validation_record or {}).get("query", ""),
            "error": (validation_record or {}).get("error", ""),
            "analysis": (validation_record or {}).get("analysis", {}),
            "fix_attempts": (validation_record or {}).get("fix_attempts", []),
        },
    }


def _validation_lookup_keys(
    dashboard_title: str = "",
    dashboard_uid: str = "",
    panel_title: str = "",
    source_panel_id: str = "",
) -> list[tuple[str, str, str]]:
    keys: list[tuple[str, str, str]] = []
    if dashboard_uid and source_panel_id:
        keys.append(("dashboard_uid+source_panel_id", dashboard_uid, source_panel_id))
    if dashboard_title and source_panel_id:
        keys.append(("dashboard_title+source_panel_id", dashboard_title, source_panel_id))
    if dashboard_uid and panel_title:
        keys.append(("dashboard_uid+panel_title", dashboard_uid, panel_title))
    if dashboard_title and panel_title:
        keys.append(("dashboard_title+panel_title", dashboard_title, panel_title))
    return keys


def _index_validation_records(validation_records: list[dict[str, Any]] | None = None):
    index: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in validation_records or []:
        for key in _validation_lookup_keys(
            dashboard_title=str(record.get("dashboard", "")),
            dashboard_uid=str(record.get("dashboard_uid", "")),
            panel_title=str(record.get("panel", "")),
            source_panel_id=str(record.get("source_panel_id", "")),
        ):
            index[key].append(record)
    return index


def _lookup_validation_record(validation_index, result: Any, panel_result: Any):
    dashboard_title = str(getattr(result, "dashboard_title", ""))
    dashboard_uid = str(getattr(result, "dashboard_uid", ""))
    panel_title = str(getattr(panel_result, "title", ""))
    source_panel_id = str(getattr(panel_result, "source_panel_id", ""))
    lookup_keys = _validation_lookup_keys(
        dashboard_title=dashboard_title,
        dashboard_uid=dashboard_uid,
        panel_title=panel_title,
        source_panel_id=source_panel_id,
    )
    source_keys = [key for key in lookup_keys if key[0].endswith("source_panel_id")]
    title_keys = [key for key in lookup_keys if key[0].endswith("panel_title")]

    for key in source_keys:
        matches = validation_index.get(key, [])
        if len(matches) == 1:
            return matches[0]
    if source_panel_id:
        for key in title_keys:
            matches = validation_index.get(key, [])
            if any(str(match.get("source_panel_id", "")) for match in matches):
                return None
    for key in title_keys:
        matches = validation_index.get(key, [])
        if len(matches) == 1:
            return matches[0]
    return None


def annotate_results_with_verification(
    results: list[Any],
    validation_records: list[dict[str, Any]] | None = None,
    *,
    prometheus_url: str = "",
    loki_url: str = "",
    verify: bool | str = True,
) -> dict[str, Any]:
    validation_index = _index_validation_records(validation_records)
    validation_attempted = bool(validation_records)

    summary_counter = Counter()
    unmatched_panels = 0
    packets = []
    for result in results:
        for panel_result in getattr(result, "panel_results", []) or []:
            record = _lookup_validation_record(validation_index, result, panel_result)

            has_query = bool(getattr(panel_result, "esql_query", ""))
            panel_status = str(getattr(panel_result, "status", "") or "").lower()
            skippable = panel_status in {"skipped", "not_feasible"}
            if (
                validation_attempted
                and record is None
                and has_query
                and not skippable
            ):
                unmatched_panels += 1

            packet = build_verification_packet(
                result.dashboard_title, panel_result, record, dashboard_result=result,
                prometheus_url=prometheus_url, loki_url=loki_url, verify=verify,
            )

            if (
                validation_attempted
                and record is None
                and has_query
                and not skippable
            ):
                rollups = list(packet.get("runtime_rollups", []) or [])
                if "not_runtime_checked" not in rollups:
                    rollups.append("not_runtime_checked")
                packet["runtime_rollups"] = rollups

            candidates = list(packet.get("candidate_targets", []) or [])
            panel_result.target_candidates = candidates
            panel_result.verification_packet = packet
            panel_result.runtime_rollups = list(packet.get("runtime_rollups", []) or [])
            if not getattr(panel_result, "recommended_target", "") and candidates:
                panel_result.recommended_target = candidates[0]["target"]
            panel_result.operational_ir = build_operational_ir(
                panel_result,
                dashboard_title=str(getattr(result, "dashboard_title", "") or ""),
                dashboard_uid=str(getattr(result, "dashboard_uid", "") or ""),
                source_file=str(getattr(result, "source_file", "") or ""),
                folder_title=str(getattr(result, "folder_title", "") or ""),
                semantic_gate=str(packet.get("semantic_gate", "") or ""),
                verification_mode=str(packet.get("verification_mode", "") or ""),
                validation_status=str(packet.get("validation_status", "not_run") or "not_run"),
            )
            summary_counter[packet["semantic_gate"]] += 1
            packets.append(packet)

    return {
        "summary": {
            "panels": len(packets),
            "green": summary_counter["Green"],
            "yellow": summary_counter["Yellow"],
            "red": summary_counter["Red"],
            "unmatched_panels": unmatched_panels,
        },
        "packets": packets,
    }


def save_verification_packets(verification_payload: dict[str, Any], output_path: str | Path) -> None:
    output_path = Path(output_path)
    with output_path.open("w") as fh:
        json.dump(verification_payload, fh, indent=2)
