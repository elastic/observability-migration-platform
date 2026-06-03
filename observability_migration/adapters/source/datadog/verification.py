# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Datadog verification packets and semantic gate helpers."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from observability_migration.adapters.source.datadog.execution import build_source_execution_summary
from observability_migration.adapters.source.grafana.esql_validate import (
    _query_source_and_index,
    validate_query_with_fixes,
)
from observability_migration.adapters.source.grafana.execution.target import build_target_execution_summary
from observability_migration.core.assets.operational import build_operational_ir
from observability_migration.core.verification.comparators import (
    build_comparison_result,
    build_sample_window,
    comparison_gate_override,
)
from observability_migration.targets.kibana.emit.esql_utils import extract_esql_shape

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


def _output_shape_for_panel(panel_result: Any) -> str:
    kibana_type = str(getattr(panel_result, "kibana_type", "") or "").lower()
    if kibana_type == "metric":
        return "single_value"
    if kibana_type == "table":
        return "table"
    if kibana_type == "xy":
        return "time_series"
    if kibana_type in {"partition", "treemap"}:
        return "categorical_distribution"
    if kibana_type == "heatmap":
        return "heatmap"
    return "unknown"


def _build_query_ir_snapshot(panel_result: Any) -> dict[str, Any]:
    existing = getattr(panel_result, "query_ir", {}) or {}
    if isinstance(existing, dict) and existing.get("target_query"):
        return existing
    query = str(getattr(panel_result, "esql_query", "") or "")
    metric_field = ""
    group_fields: list[str] = []
    target_index = ""
    if query:
        shape = extract_esql_shape(query)
        target_index = _query_source_and_index(query)[1]
        if shape.metric_fields:
            metric_field = shape.metric_fields[0]
        elif shape.projected_fields:
            metric_field = shape.projected_fields[0]
        group_fields = list(shape.group_fields or shape.time_fields or [])
    return {
        "source_expression": str((getattr(panel_result, "source_queries", []) or [""])[0] or ""),
        "target_query": query,
        "output_shape": _output_shape_for_panel(panel_result),
        "target_index": target_index,
        "output_metric_field": metric_field,
        "output_group_fields": group_fields,
        "semantic_losses": list(getattr(panel_result, "semantic_losses", []) or []),
    }


def _build_target_candidates(panel_result: Any) -> list[dict[str, Any]]:
    status = str(getattr(panel_result, "status", "") or "").lower()
    backend = str(getattr(panel_result, "backend", "") or "").lower()
    kibana_type = str(getattr(panel_result, "kibana_type", "") or "").lower()
    widget_type = str(getattr(panel_result, "dd_widget_type", "") or "").lower()
    query_language = str(getattr(panel_result, "query_language", "") or "").lower()
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
        add("skip", 1, "ignored", "Widget was intentionally skipped by the planner")
        return candidates

    if status in {"not_feasible", "requires_manual", "blocked"}:
        add("manual_redesign", 1, "required", "Current Datadog translation cannot preserve this widget safely")
        add("block", 2, "required", "Do not auto-promote this widget")
        return candidates

    if widget_type in {"note", "free_text", "image", "iframe"}:
        add("markdown", 1, "preferred", "Text-style widgets map best to markdown panels")
        add("manual_redesign", 2, "review", "Complex formatting may still need manual cleanup")
        return candidates

    if kibana_type == "metric":
        add("native_metric_panel", 1, "preferred", "Single-value Datadog widgets map to Kibana metric-style panels")
        if backend == "lens":
            add("lens_fallback", 2, "fallback", "Lens remains a valid fallback for aggregation-only metric widgets")
    elif kibana_type == "table":
        if query_language == "datadog_log":
            add("discover_embed", 1, "preferred", "Log/event workflows often fit Discover-style tables best")
            add("esql_table", 2, "fallback", "An ES|QL table remains a viable saved-dashboard target")
        else:
            add("esql_table", 1, "preferred", "Tabular Datadog widgets map cleanly to ES|QL tables")
    elif kibana_type == "xy":
        add("native_esql_panel", 1, "preferred", "Time-series widgets should prefer native ES|QL visualizations")
        if backend == "lens":
            add("lens_fallback", 2, "fallback", "Lens remains useful for simpler aggregation-only charts")
    elif kibana_type == "partition":
        add("partition_chart", 1, "preferred", "Categorical Datadog widgets map to Kibana partition charts")
    elif kibana_type == "treemap":
        add("treemap", 1, "preferred", "Treemap widgets can target Kibana treemap panels")
    elif kibana_type == "heatmap":
        add("heatmap", 1, "preferred", "Heatmap widgets map to Kibana heatmap panels")
    else:
        add("native_esql_panel", 1, "preferred", "Default ES|QL visualization path is the safest target")

    add("manual_redesign", len(candidates) + 1, "review", "Keep a manual redesign path for semantics the runtime cannot yet prove")
    return candidates


def _collect_semantic_losses(panel_result: Any) -> list[str]:
    losses: list[str] = []
    for item in getattr(panel_result, "semantic_losses", []) or []:
        _append_unique(losses, str(item))
    for collection in (getattr(panel_result, "warnings", []) or [], getattr(panel_result, "reasons", []) or []):
        for item in collection:
            lowered = str(item).lower()
            if any(
                token in lowered
                for token in ("approximat", "drop", "manual", "fallback", "broad", "placeholder", "not yet supported")
            ):
                _append_unique(losses, str(item))
    return losses


def _runtime_rollups(
    panel_result: Any,
    validation_record: dict[str, Any] | None,
    dashboard_result: Any | None = None,
) -> list[str]:
    rollups: list[str] = []
    for item in getattr(panel_result, "runtime_rollups", []) or []:
        _append_unique(rollups, str(item))
    if validation_record and validation_record.get("status") == "fixed_empty":
        _append_unique(rollups, "empty_result")
    if dashboard_result:
        runtime_summary = dashboard_result.build_runtime_summary()
        if runtime_summary.get("compile", {}).get("status") == "fail":
            _append_unique(rollups, "compile_failed")
        if runtime_summary.get("layout", {}).get("status") == "fail":
            _append_unique(rollups, "layout_failed")
        if runtime_summary.get("upload", {}).get("status") == "fail":
            _append_unique(rollups, "upload_failed")
    return rollups


def _runtime_state(
    panel_result: Any,
    validation_record: dict[str, Any] | None,
    dashboard_result: Any | None = None,
) -> dict[str, Any]:
    dashboard_runtime = dashboard_result.build_runtime_summary() if dashboard_result else {}
    return {
        "compile": dashboard_runtime.get("compile", {"status": "not_run", "error": ""}),
        "layout": dashboard_runtime.get("layout", {"status": "not_run", "error": ""}),
        "upload": dashboard_runtime.get("upload", {"status": "not_run", "error": ""}),
        "validation": {
            "status": validation_record.get("status", "not_run") if validation_record else "not_run",
            "error": validation_record.get("error", "") if validation_record else "",
        },
        "smoke": dashboard_runtime.get("smoke", {"status": "not_run", "error": ""}),
        "browser": dashboard_runtime.get("browser", {"status": "not_run", "error": ""}),
    }


def _semantic_gate(
    panel_result: Any,
    validation_record: dict[str, Any] | None,
    semantic_losses: list[str],
    runtime_rollups: list[str],
    comparison: dict[str, Any] | None = None,
) -> str:
    status = str(getattr(panel_result, "status", "") or "").lower()
    if status in {"not_feasible", "requires_manual", "blocked"}:
        return "Red"
    comparison_override = comparison_gate_override(comparison)
    if comparison_override == "Red":
        return "Red"
    if any(item in RUNTIME_ERROR_ROLLUPS for item in runtime_rollups):
        return "Red"
    if validation_record and validation_record.get("status") in {"fail", "fixed_empty"}:
        return "Red"
    if validation_record and validation_record.get("status") in {"fixed", "skip"}:
        return "Yellow"
    if comparison_override == "Yellow":
        return "Yellow"
    if any(item in RUNTIME_WARNING_ROLLUPS for item in runtime_rollups):
        return "Yellow"
    if status == "warning" or semantic_losses:
        return "Yellow"
    return "Green"


def build_verification_packet(
    dashboard_result: Any,
    panel_result: Any,
    validation_record: dict[str, Any] | None = None,
    *,
    datadog_api_key: str = "",
    datadog_app_key: str = "",
    datadog_site: str = "datadoghq.com",
    source_timeout: int = 30,
    verify: bool | str = True,
) -> dict[str, Any]:
    query_ir = _build_query_ir_snapshot(panel_result)
    panel_result.query_ir = dict(query_ir)
    semantic_losses = _collect_semantic_losses(panel_result)
    runtime_rollups = _runtime_rollups(panel_result, validation_record, dashboard_result)
    runtime_state = _runtime_state(panel_result, validation_record, dashboard_result)
    sample_window = build_sample_window(query_ir, validation_record).to_dict()
    source_execution = build_source_execution_summary(
        panel_result,
        api_key=datadog_api_key,
        app_key=datadog_app_key,
        site=datadog_site,
        timeout=source_timeout,
        verify=verify,
    ).to_dict()
    target_execution = build_target_execution_summary(panel_result, validation_record).to_dict()
    comparison = build_comparison_result(source_execution, target_execution, query_ir, validation_record).to_dict()
    if source_execution.get("status") == "fail":
        _append_unique(runtime_rollups, "source_execution_failed")
    gate = _semantic_gate(panel_result, validation_record, semantic_losses, runtime_rollups, comparison=comparison)
    candidates = _build_target_candidates(panel_result)
    validation_status = validation_record.get("status", "not_run") if validation_record else "not_run"
    source_status = str(source_execution.get("status", "") or "")
    target_status = str(target_execution.get("status", "") or "")
    verification_mode = (
        "source_target_comparison"
        if source_status == "pass" and target_status in {"pass", "fixed"}
        else "source_validated"
        if source_status == "pass"
        else "runtime_validation"
        if validation_record
        else "post_upload_smoke"
        if any(item in runtime_rollups for item in ("smoke_failed", "browser_failed", "empty_result", "not_runtime_checked"))
        else "static_analysis"
    )
    return {
        "dashboard": str(getattr(dashboard_result, "dashboard_title", "") or ""),
        "dashboard_id": str(getattr(dashboard_result, "dashboard_id", "") or ""),
        "kibana_saved_object_id": str(getattr(dashboard_result, "kibana_saved_object_id", "") or ""),
        "panel": str(getattr(panel_result, "title", "") or ""),
        "source_panel_id": str(getattr(panel_result, "source_panel_id", "") or getattr(panel_result, "widget_id", "") or ""),
        "status": str(getattr(panel_result, "status", "") or ""),
        "semantic_gate": gate,
        "verification_mode": verification_mode,
        "validation_status": validation_status,
        "source_language": str(getattr(panel_result, "query_language", "") or ""),
        "source_queries": list(getattr(panel_result, "source_queries", []) or []),
        "translated_query": str(getattr(panel_result, "esql_query", "") or ""),
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
    dashboard_id: str = "",
    panel_title: str = "",
    widget_id: str = "",
) -> list[tuple[str, str, str]]:
    keys: list[tuple[str, str, str]] = []
    if dashboard_id and widget_id:
        keys.append(("dashboard_id+widget_id", dashboard_id, widget_id))
    if dashboard_title and widget_id:
        keys.append(("dashboard_title+widget_id", dashboard_title, widget_id))
    if dashboard_id and panel_title:
        keys.append(("dashboard_id+panel_title", dashboard_id, panel_title))
    if dashboard_title and panel_title:
        keys.append(("dashboard_title+panel_title", dashboard_title, panel_title))
    return keys


def _index_validation_records(validation_records: list[dict[str, Any]] | None = None) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in validation_records or []:
        for key in _validation_lookup_keys(
            dashboard_title=str(record.get("dashboard", "")),
            dashboard_id=str(record.get("dashboard_id", "")),
            panel_title=str(record.get("widget", "")),
            widget_id=str(record.get("widget_id", "")),
        ):
            index[key].append(record)
    return index


def _lookup_validation_record(validation_index: dict[tuple[str, str, str], list[dict[str, Any]]], result: Any, panel_result: Any) -> dict[str, Any] | None:
    lookup_keys = _validation_lookup_keys(
        dashboard_title=str(getattr(result, "dashboard_title", "")),
        dashboard_id=str(getattr(result, "dashboard_id", "")),
        panel_title=str(getattr(panel_result, "title", "")),
        widget_id=str(getattr(panel_result, "widget_id", "")),
    )
    widget_keys = [key for key in lookup_keys if key[0].endswith("widget_id")]
    title_keys = [key for key in lookup_keys if key[0].endswith("panel_title")]
    for key in widget_keys:
        matches = validation_index.get(key, [])
        if len(matches) == 1:
            return matches[0]
    for key in title_keys:
        matches = validation_index.get(key, [])
        if len(matches) == 1:
            return matches[0]
    return None


def validate_monitor_queries(
    monitor_irs: list[Any],
    *,
    es_url: str = "",
    es_api_key: str = "",
    validate_query_fn=validate_query_with_fixes,
    max_attempts: int = 8,
    verify: bool | str = True,
) -> list[dict[str, Any]]:
    """Validate translated monitor queries against Elasticsearch when configured."""
    records: list[dict[str, Any]] = []
    for ir in monitor_irs:
        alert_id = str(getattr(ir, "alert_id", "") or "")
        translated_query = str(getattr(ir, "translated_query", "") or "")
        base_record = {
            "alert_id": alert_id,
            "name": str(getattr(ir, "name", "") or ""),
            "kind": str(getattr(ir, "kind", "") or ""),
            "query": translated_query,
            "error": "",
            "analysis": {},
            "fix_attempts": [],
        }
        if not translated_query:
            records.append({**base_record, "status": "not_translated"})
            continue
        if not es_url:
            records.append({**base_record, "status": "not_run"})
            continue
        validation = validate_query_fn(
            translated_query,
            es_url,
            resolver=None,
            max_attempts=max_attempts,
            es_api_key=es_api_key or None,
            verify=verify,
        )
        records.append({**base_record, **validation})
    return records


def build_monitor_verification_lookup(
    monitor_irs: list[Any],
    validation_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build monitor-target validation/execution evidence keyed by alert ID."""
    validation_index = {
        str(record.get("alert_id", "") or ""): record
        for record in (validation_records or [])
        if isinstance(record, dict)
    }
    verification: dict[str, Any] = {}

    for ir in monitor_irs:
        alert_id = str(getattr(ir, "alert_id", "") or "")
        record = validation_index.get(alert_id, {"status": "not_run", "query": "", "error": "", "analysis": {}, "fix_attempts": []})
        target_index = ""
        if str(record.get("query", "") or ""):
            target_index = _query_source_and_index(str(record.get("query", "") or ""))[1]
        panel_stub = SimpleNamespace(
            esql_query=str(getattr(ir, "translated_query", "") or ""),
            query_ir={"target_index": target_index},
        )
        verification[alert_id] = {
            "validation": {
                "status": str(record.get("status", "not_run") or "not_run"),
                "query": str(record.get("query", "") or ""),
                "error": str(record.get("error", "") or ""),
                "analysis": dict(record.get("analysis", {}) or {}),
                "fix_attempts": list(record.get("fix_attempts", []) or []),
            },
            "target_execution": build_target_execution_summary(panel_stub, record).to_dict(),
        }

    return verification


def annotate_results_with_verification(
    results: list[Any],
    validation_records: list[dict[str, Any]] | None = None,
    *,
    datadog_api_key: str = "",
    datadog_app_key: str = "",
    datadog_site: str = "datadoghq.com",
    source_timeout: int = 30,
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
            skippable = str(getattr(panel_result, "status", "") or "").lower() in {"skipped", "not_feasible"}
            if validation_attempted and record is None and has_query and not skippable:
                unmatched_panels += 1
                rollups = list(getattr(panel_result, "runtime_rollups", []) or [])
                _append_unique(rollups, "not_runtime_checked")
                panel_result.runtime_rollups = rollups

            packet = build_verification_packet(
                result,
                panel_result,
                record,
                datadog_api_key=datadog_api_key,
                datadog_app_key=datadog_app_key,
                datadog_site=datadog_site,
                source_timeout=source_timeout,
                verify=verify,
            )
            panel_result.target_candidates = list(packet.get("candidate_targets", []) or [])
            panel_result.recommended_target = str(packet.get("recommended_target", "") or "")
            panel_result.verification_packet = packet
            panel_result.runtime_rollups = list(packet.get("runtime_rollups", []) or [])
            panel_result.query_ir = dict(packet.get("query_ir", {}) or {})
            panel_result.operational_ir = build_operational_ir(
                panel_result,
                dashboard_title=str(getattr(result, "dashboard_title", "") or ""),
                dashboard_uid=str(getattr(result, "dashboard_id", "") or ""),
                source_file=str(getattr(result, "source_file", "") or ""),
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(verification_payload, fh, indent=2)


__all__ = [
    "annotate_results_with_verification",
    "build_monitor_verification_lookup",
    "build_verification_packet",
    "save_verification_packets",
    "validate_monitor_queries",
]
