"""Migration reporting: console output and JSON report generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import DashboardResult


def _maybe_to_dict(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def build_monitor_migration_results(monitor_irs: list[Any]) -> dict[str, Any]:
    """Build the raw monitor migration results artifact."""
    by_tier: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_target_rule_type: dict[str, int] = {}
    by_selected_target_rule_type: dict[str, int] = {}

    for ir in monitor_irs:
        tier = str(getattr(ir, "automation_tier", "") or "")
        if tier:
            by_tier[tier] = by_tier.get(tier, 0) + 1

        kind = str(getattr(ir, "kind", "") or "")
        if kind:
            by_kind[kind] = by_kind.get(kind, 0) + 1

        target_rule_type = str(getattr(ir, "target_rule_type", "") or "")
        if target_rule_type:
            by_target_rule_type[target_rule_type] = by_target_rule_type.get(target_rule_type, 0) + 1

        selected_target_rule_type = str(getattr(ir, "selected_target_rule_type", "") or "")
        if selected_target_rule_type:
            by_selected_target_rule_type[selected_target_rule_type] = (
                by_selected_target_rule_type.get(selected_target_rule_type, 0) + 1
            )

    return {
        "total": len(monitor_irs),
        "by_automation_tier": by_tier,
        "by_target_rule_type": by_target_rule_type,
        "by_selected_target_rule_type": by_selected_target_rule_type,
        "by_kind": by_kind,
        "monitors": [ir.to_dict() for ir in monitor_irs],
    }


def build_monitor_comparison_results(
    raw_monitors: list[dict[str, Any]],
    monitor_irs: list[Any],
    mapping_batch: dict[str, Any],
    *,
    payload_validation_by_alert_id: dict[str, Any] | None = None,
    verification_by_alert_id: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a review-friendly source-vs-target artifact for Datadog monitors."""
    mapping_results = mapping_batch.get("results", []) if isinstance(mapping_batch, dict) else []
    summary = mapping_batch.get("summary", {}) if isinstance(mapping_batch, dict) else {}
    payload_validation_lookup = payload_validation_by_alert_id or {}
    verification_lookup = verification_by_alert_id or {}

    monitors: list[dict[str, Any]] = []
    for idx, ir in enumerate(monitor_irs):
        raw = raw_monitors[idx] if idx < len(raw_monitors) and isinstance(raw_monitors[idx], dict) else {}
        mapping_entry = mapping_results[idx]["mapping"] if idx < len(mapping_results) else {}
        target_payload = mapping_entry.get("rule_payload", {}) if isinstance(mapping_entry, dict) else {}
        validation_errors = list(mapping_entry.get("validation_errors", []) or []) if isinstance(mapping_entry, dict) else []
        automation_tier = mapping_entry.get("automation_tier", "") if isinstance(mapping_entry, dict) else ""
        blocked_reasons: list[str] = []

        if not getattr(ir, "translated_query", "") or automation_tier == "manual_required":
            for warning in getattr(ir, "warnings", []) or []:
                _append_unique(blocked_reasons, str(warning))
        for error in validation_errors:
            _append_unique(blocked_reasons, str(error))

        source_options = raw.get("options")
        if not isinstance(source_options, dict):
            source_options = (
                getattr(ir, "source_extension", {}).get("options", {})
                if isinstance(getattr(ir, "source_extension", {}), dict)
                else {}
            )
        source_tags = raw.get("tags")
        if not isinstance(source_tags, list):
            source_tags = list(getattr(ir, "metadata", {}).get("tags", []) or [])
        metadata = getattr(ir, "metadata", {}) if isinstance(getattr(ir, "metadata", {}), dict) else {}
        parser_diagnostics = list(metadata.get("parser_diagnostics", []) or [])
        parse_degraded = bool(metadata.get("parse_degraded"))

        monitor_row = {
            "alert_id": getattr(ir, "alert_id", ""),
            "name": getattr(ir, "name", ""),
            "kind": getattr(ir, "kind", ""),
            "source": {
                "type": raw.get("type", "") or getattr(ir, "metadata", {}).get("datadog_type", ""),
                "query": raw.get("query", "") or getattr(ir, "source_extension", {}).get("query", ""),
                "options": source_options,
                "message": raw.get("message", "") or getattr(ir, "source_extension", {}).get("message", ""),
                "tags": source_tags,
            },
            "translation": {
                "query": getattr(ir, "translated_query", ""),
                "provenance": getattr(ir, "translated_query_provenance", ""),
                "warnings": list(getattr(ir, "warnings", []) or []),
                "group_by": list(getattr(ir, "group_by", []) or []),
                "parser_diagnostics": parser_diagnostics,
                "parse_degraded": parse_degraded,
            },
            "target": {
                "automation_tier": automation_tier,
                "target_rule_type": mapping_entry.get("target_rule_type", "") if isinstance(mapping_entry, dict) else "",
                "selected_target_rule_type": (
                    mapping_entry.get("selected_target_rule_type", "") if isinstance(mapping_entry, dict) else ""
                ),
                "payload_emitted": (
                    bool(mapping_entry.get("payload_emitted")) if isinstance(mapping_entry, dict) else bool(target_payload)
                ),
                "payload_status": mapping_entry.get("payload_status", "") if isinstance(mapping_entry, dict) else "",
                "payload_status_reason": (
                    mapping_entry.get("payload_status_reason", "") if isinstance(mapping_entry, dict) else ""
                ),
                "rule_payload": target_payload,
                "valid": bool(mapping_entry.get("valid")) if isinstance(mapping_entry, dict) else False,
                "validation_errors": validation_errors,
                "payload_validation": payload_validation_lookup.get(str(getattr(ir, "alert_id", "") or ""), {}),
            },
            "semantic_losses": list(mapping_entry.get("losses", []) or []) if isinstance(mapping_entry, dict) else [],
            "blocked_reasons": blocked_reasons,
        }
        verification_entry = verification_lookup.get(str(getattr(ir, "alert_id", "") or ""))
        if verification_entry:
            monitor_row["verification"] = verification_entry
        monitors.append(monitor_row)

    by_kind: dict[str, int] = {}
    for ir in monitor_irs:
        kind = str(getattr(ir, "kind", "") or "")
        by_kind[kind] = by_kind.get(kind, 0) + 1

    return {
        "total": len(monitors),
        "summary": {
            "by_automation_tier": dict(summary.get("by_automation_tier", {}) or {}),
            "by_target_rule_type": dict(summary.get("by_target_rule_type", {}) or {}),
            "by_selected_target_rule_type": dict(summary.get("by_selected_target_rule_type", {}) or {}),
            "by_kind": by_kind,
        },
        "monitors": monitors,
    }


def print_report(results: list[DashboardResult]) -> None:
    """Print a human-readable migration report to stdout."""
    total_widgets = 0
    total_ok = 0
    total_warning = 0
    total_manual = 0
    total_nf = 0
    total_skipped = 0
    total_blocked = 0

    print("\n" + "=" * 70)
    print("DATADOG → KIBANA MIGRATION REPORT")
    print("=" * 70)

    for dr in results:
        dr.recompute_counts()
        total_widgets += dr.total_widgets
        total_ok += dr.migrated
        total_warning += dr.migrated_with_warnings
        total_manual += dr.requires_manual
        total_nf += dr.not_feasible
        total_skipped += dr.skipped
        total_blocked += dr.blocked

        print(f"\n  Dashboard: {dr.dashboard_title}")
        print(f"    Source:  {dr.source_file}")
        print(f"    Panels:  {dr.total_widgets}")
        print(f"    OK:      {dr.migrated}  Warning: {dr.migrated_with_warnings}  "
              f"Manual: {dr.requires_manual}  NF: {dr.not_feasible}  "
              f"Skip: {dr.skipped}  Blocked: {dr.blocked}")

        if dr.compile_error:
            print(f"    COMPILE ERROR: {dr.compile_error}")

        if dr.upload_attempted:
            upload_status = "pass" if dr.uploaded and not dr.upload_error else "fail"
            print(f"    Upload: {upload_status}")
            if dr.upload_error:
                print(f"    UPLOAD ERROR: {dr.upload_error}")
        if dr.smoke_attempted:
            print(f"    Smoke: {dr.smoke_status}")
            if dr.smoke_error:
                print(f"    SMOKE DETAIL: {dr.smoke_error}")
        if dr.verification_summary:
            print(
                f"    Verification: Green={dr.verification_summary.get('green', 0)}  "
                f"Yellow={dr.verification_summary.get('yellow', 0)}  "
                f"Red={dr.verification_summary.get('red', 0)}"
            )

        if dr.preflight_issues:
            preflight_blocks = sum(1 for issue in dr.preflight_issues if issue.get("level") == "block")
            preflight_warns = sum(1 for issue in dr.preflight_issues if issue.get("level") == "warn")
            preflight_info = sum(1 for issue in dr.preflight_issues if issue.get("level") == "info")
            print(
                f"    Preflight: {'pass' if dr.preflight_passed else 'issues'}  "
                f"Block: {preflight_blocks}  Warn: {preflight_warns}  Info: {preflight_info}"
            )
            for issue in dr.preflight_issues[:5]:
                widget_prefix = f"{issue.get('widget_id')}: " if issue.get("widget_id") else ""
                print(f"      - [{issue.get('level', 'info')}] {widget_prefix}{issue.get('message', '')}")
            if len(dr.preflight_issues) > 5:
                print(f"      ... and {len(dr.preflight_issues) - 5} more")

        nf_panels = [p for p in dr.panel_results if p.status in ("not_feasible", "blocked")]
        if nf_panels:
            print("    Not feasible / blocked panels:")
            for p in nf_panels[:5]:
                reason_str = "; ".join(p.reasons[:2]) if p.reasons else "unknown"
                print(f"      - {p.title}: {reason_str}")
            if len(nf_panels) > 5:
                print(f"      ... and {len(nf_panels) - 5} more")

    print(f"\n{'=' * 70}")
    print(f"TOTALS: {len(results)} dashboards, {total_widgets} widgets")
    print(f"  OK: {total_ok}  Warning: {total_warning}  Manual: {total_manual}  "
          f"NF: {total_nf}  Skip: {total_skipped}  Blocked: {total_blocked}")

    if total_widgets > 0:
        success_rate = (total_ok + total_warning) / total_widgets * 100
        print(f"  Success rate: {success_rate:.1f}%")

    print("=" * 70 + "\n")


def save_detailed_report(
    results: list[DashboardResult],
    output_path: str,
    validation_summary: dict[str, Any] | None = None,
    validation_records: list[dict[str, Any]] | None = None,
    smoke_payload: dict[str, Any] | None = None,
    verification_payload: dict[str, Any] | None = None,
) -> None:
    """Save a detailed JSON report."""
    report: dict[str, Any] = {
        "tool": "datadog-to-kibana-migration",
        "version": "0.1.0",
        "dashboards": [],
        "summary": {},
    }
    if validation_summary or validation_records:
        report["validation"] = {
            "summary": validation_summary or {},
            "records": validation_records or [],
        }
    if smoke_payload:
        report["smoke"] = {
            "summary": smoke_payload.get("summary", {}),
            "report_path": next(
                (
                    dr.smoke_report_path
                    for dr in results
                    if getattr(dr, "smoke_report_path", "")
                ),
                "",
            ),
        }
    if verification_payload:
        report["verification"] = verification_payload

    total_widgets = 0
    total_ok = 0
    total_warning = 0
    total_manual = 0
    total_nf = 0
    total_preflight_blocks = 0
    total_preflight_warns = 0
    total_upload_attempted = 0
    total_uploaded = 0

    for dr in results:
        dr.recompute_counts()
        total_widgets += dr.total_widgets
        total_ok += dr.migrated
        total_warning += dr.migrated_with_warnings
        total_manual += dr.requires_manual
        total_nf += dr.not_feasible
        total_preflight_blocks += sum(1 for issue in dr.preflight_issues if issue.get("level") == "block")
        total_preflight_warns += sum(1 for issue in dr.preflight_issues if issue.get("level") == "warn")
        total_upload_attempted += 1 if dr.upload_attempted else 0
        total_uploaded += 1 if dr.uploaded else 0

        dashboard_entry: dict[str, Any] = {
            "id": dr.dashboard_id,
            "title": dr.dashboard_title,
            "source_file": dr.source_file,
            "total_widgets": dr.total_widgets,
            "migrated": dr.migrated,
            "migrated_with_warnings": dr.migrated_with_warnings,
            "requires_manual": dr.requires_manual,
            "not_feasible": dr.not_feasible,
            "skipped": dr.skipped,
            "blocked": dr.blocked,
            "compiled": dr.compiled,
            "compiled_path": dr.compiled_path,
            "compile_error": dr.compile_error,
            "yaml_path": dr.yaml_path,
            "runtime_summary": dr.build_runtime_summary(),
            "validation": dr.validation_summary,
            "verification_summary": dr.verification_summary,
            "upload": {
                "attempted": dr.upload_attempted,
                "uploaded": dr.uploaded,
                "error": dr.upload_error,
                "space": dr.uploaded_space,
                "kibana_url": dr.uploaded_kibana_url,
                "saved_object_id": dr.kibana_saved_object_id,
            },
            "smoke": {
                "attempted": dr.smoke_attempted,
                "status": dr.smoke_status,
                "error": dr.smoke_error,
                "report_path": dr.smoke_report_path,
                "browser_audit_attempted": dr.browser_audit_attempted,
                "browser_audit_status": dr.browser_audit_status,
                "browser_audit_error": dr.browser_audit_error,
            },
            "preflight": {
                "passed": dr.preflight_passed,
                "issue_counts": {
                    "block": sum(1 for issue in dr.preflight_issues if issue.get("level") == "block"),
                    "warn": sum(1 for issue in dr.preflight_issues if issue.get("level") == "warn"),
                    "info": sum(1 for issue in dr.preflight_issues if issue.get("level") == "info"),
                },
                "issues": dr.preflight_issues,
            },
            "panels": [],
        }

        for pr in dr.panel_results:
            panel_entry: dict[str, Any] = {
                "widget_id": pr.widget_id,
                "title": pr.title,
                "dd_widget_type": pr.dd_widget_type,
                "kibana_type": pr.kibana_type,
                "status": pr.status,
                "backend": pr.backend,
                "confidence": pr.confidence,
                "query_language": pr.query_language,
                "source_queries": pr.source_queries,
                "esql_query": pr.esql_query,
                "warnings": pr.warnings,
                "semantic_losses": pr.semantic_losses,
                "reasons": pr.reasons,
                "runtime_rollups": pr.runtime_rollups,
                "target_candidates": pr.target_candidates,
                "recommended_target": pr.recommended_target,
                "query_ir": pr.query_ir,
                "verification_packet": pr.verification_packet,
                "operational_ir": _maybe_to_dict(pr.operational_ir),
            }
            dashboard_entry["panels"].append(panel_entry)

        report["dashboards"].append(dashboard_entry)

    report["summary"] = {
        "total_dashboards": len(results),
        "total_widgets": total_widgets,
        "migrated": total_ok,
        "migrated_with_warnings": total_warning,
        "requires_manual": total_manual,
        "not_feasible": total_nf,
        "preflight_blocks": total_preflight_blocks,
        "preflight_warnings": total_preflight_warns,
        "upload_attempted": total_upload_attempted,
        "uploaded": total_uploaded,
        "smoke_attempted": sum(1 for dr in results if dr.smoke_attempted),
        "smoke_failed": sum(1 for dr in results if dr.smoke_status == "fail"),
        "verification_red": sum((dr.verification_summary or {}).get("red", 0) for dr in results),
        "success_rate": (
            f"{(total_ok + total_warning) / total_widgets * 100:.1f}%"
            if total_widgets > 0
            else "0.0%"
        ),
    }

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  Detailed report saved: {output_path}")
