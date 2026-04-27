"""Migration result models and reporting helpers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from observability_migration.adapters.source.grafana.rules import _append_unique
from observability_migration.core.assets.operational import OperationalIR
from observability_migration.core.assets.visual import VisualIR


def _ir_to_dict(obj: Any) -> dict:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return obj
    return {}


@dataclass
class MigrationResult:
    dashboard_title: str
    dashboard_uid: str
    total_panels: int = 0
    migrated: int = 0
    migrated_with_warnings: int = 0
    requires_manual: int = 0
    not_feasible: int = 0
    skipped: int = 0
    panel_results: list = field(default_factory=list)
    yaml_panel_results: list = field(default_factory=list)
    compiled: bool = False
    compile_error: str = ""
    source_file: str = ""
    folder_title: str = ""
    inventory: dict = field(default_factory=dict)
    metadata_polish: dict = field(default_factory=dict)
    verification_summary: dict = field(default_factory=dict)
    review_explanations: dict = field(default_factory=dict)
    yaml_linted: bool | None = None
    yaml_lint_error: str = ""
    layout_validated: bool | None = None
    layout_error: str = ""
    upload_attempted: bool = False
    uploaded: bool | None = None
    upload_error: str = ""
    kibana_saved_object_id: str = ""
    uploaded_space: str = ""
    uploaded_kibana_url: str = ""
    yaml_path: str = ""
    compiled_path: str = ""
    runtime_summary: dict = field(default_factory=dict)
    dashboard_links: list = field(default_factory=list)
    annotations: list = field(default_factory=list)
    alert_migration_tasks: list = field(default_factory=list)
    feature_gap_summary: dict = field(default_factory=dict)
    alert_results: list = field(default_factory=list)  # list of AlertingIR.to_dict()
    alert_summary: dict = field(default_factory=dict)  # {"total": N, "automated": N, "draft_review": N, "manual_required": N, "by_kind": {...}}


@dataclass
class PanelResult:
    title: str
    grafana_type: str
    kibana_type: str
    status: str
    confidence: float
    reasons: list = field(default_factory=list)
    promql_expr: str = ""
    esql_query: str = ""
    trace: list = field(default_factory=list)
    source_panel_id: str = ""
    datasource_type: str = ""
    datasource_uid: str = ""
    datasource_name: str = ""
    query_language: str = ""
    readiness: str = ""
    recommended_target: str = ""
    notes: list = field(default_factory=list)
    inventory: dict = field(default_factory=dict)
    query_ir: dict = field(default_factory=dict)
    visual_ir: Any = field(default_factory=VisualIR)
    operational_ir: Any = field(default_factory=OperationalIR)
    metadata_polish: dict = field(default_factory=dict)
    target_candidates: list = field(default_factory=list)
    verification_packet: dict = field(default_factory=dict)
    review_explanation: dict = field(default_factory=dict)
    runtime_rollups: list = field(default_factory=list)
    link_migrations: list = field(default_factory=list)
    transformation_redesign_tasks: list = field(default_factory=list)
    post_validation_action: str = ""
    post_validation_message: str = ""


def _panel_query_index(yaml_panel):
    esql = (yaml_panel or {}).get("esql", {})
    if not isinstance(esql, dict):
        return ""
    query = esql.get("query", "")
    for line in query.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^(?:FROM|TS)\s+(\S+)", stripped)
        if match:
            return match.group(1)
        break
    return ""


def _validation_placeholder_message(panel_result):
    target_index = _panel_query_index({"esql": {"query": panel_result.esql_query}})
    lines = [
        f"**{panel_result.title}**",
        "",
        "Manual review required.",
        "",
        "Validation only succeeded after narrowing this panel to a fallback data stream that returned no rows.",
    ]
    if target_index:
        lines.append(f"Fallback data stream: `{target_index}`")
    if panel_result.promql_expr:
        lines.extend(["", f"Original query: `{panel_result.promql_expr}`"])
    return "\n".join(lines)


def _validation_failure_placeholder_message(panel_result, validation_result):
    analysis = (validation_result or {}).get("analysis") or {}
    error = ((validation_result or {}).get("error") or analysis.get("raw_error") or "").splitlines()[0].strip()
    lines = [
        f"**{panel_result.title}**",
        "",
        "Manual review required.",
        "",
        "This panel failed live ES|QL validation and was replaced with a placeholder before upload.",
    ]
    if error:
        lines.append(f"Validation error: `{error}`")
    if panel_result.promql_expr:
        lines.extend(["", f"Original query: `{panel_result.promql_expr}`"])
    return "\n".join(lines)


def _mark_panel_requires_manual_with_placeholder(panel_result, action, message):
    panel_result.status = "requires_manual"
    panel_result.kibana_type = "markdown"
    panel_result.confidence = 0.0
    panel_result.post_validation_action = action
    panel_result.post_validation_message = message


def mark_panel_requires_manual_after_validation(panel_result, validation_result):
    _mark_panel_requires_manual_with_placeholder(
        panel_result,
        "placeholder_empty_result",
        _validation_placeholder_message(panel_result),
    )
    _append_unique(
        panel_result.reasons,
        "Validation only succeeded on an empty fallback data stream",
    )
    narrowed_to = (validation_result.get("analysis") or {}).get("narrowed_to_index")
    if narrowed_to:
        _append_unique(
            panel_result.notes,
            f"Fallback validation target `{narrowed_to}` returned zero rows; manual review required.",
        )
    if isinstance(panel_result.query_ir, dict):
        panel_result.query_ir.setdefault("semantic_losses", [])
        if "empty fallback data stream during validation" not in panel_result.query_ir["semantic_losses"]:
            panel_result.query_ir["semantic_losses"].append("empty fallback data stream during validation")


def mark_panel_requires_manual_after_failed_validation(panel_result, validation_result):
    _mark_panel_requires_manual_with_placeholder(
        panel_result,
        "placeholder_validation_failure",
        _validation_failure_placeholder_message(panel_result, validation_result),
    )
    _append_unique(
        panel_result.reasons,
        "Live ES|QL validation failed; uploaded placeholder instead of a broken runtime panel",
    )
    error = ((validation_result or {}).get("error") or "").splitlines()[0].strip()
    if error:
        _append_unique(panel_result.notes, f"Validation error: {error}")
    if isinstance(panel_result.query_ir, dict):
        panel_result.query_ir.setdefault("semantic_losses", [])
        if "validation failure placeholder" not in panel_result.query_ir["semantic_losses"]:
            panel_result.query_ir["semantic_losses"].append("validation failure placeholder")


def recompute_result_counts(result):
    result.migrated = sum(1 for item in result.panel_results if item.status == "migrated")
    result.migrated_with_warnings = sum(1 for item in result.panel_results if item.status == "migrated_with_warnings")
    result.requires_manual = sum(1 for item in result.panel_results if item.status == "requires_manual")
    result.not_feasible = sum(1 for item in result.panel_results if item.status == "not_feasible")
    result.skipped = sum(1 for item in result.panel_results if item.status == "skipped")


def _stage_summary(completed, error):
    if completed is None:
        return {"status": "not_run", "error": ""}
    return {"status": "pass" if completed and not error else "fail", "error": error or ""}


def build_runtime_summary(result):
    upload_status = {"status": "not_run", "error": ""}
    if getattr(result, "upload_attempted", False) or getattr(result, "upload_error", ""):
        upload_status = {
            "status": "pass" if getattr(result, "uploaded", False) and not getattr(result, "upload_error", "") else "fail",
            "error": getattr(result, "upload_error", "") or "",
        }
    return {
        "yaml_lint": _stage_summary(getattr(result, "yaml_linted", None), getattr(result, "yaml_lint_error", "")),
        "compile": {
            "status": "pass" if getattr(result, "compiled", False) else "fail" if getattr(result, "compile_error", "") else "not_run",
            "error": getattr(result, "compile_error", "") or "",
        },
        "layout": _stage_summary(getattr(result, "layout_validated", None), getattr(result, "layout_error", "")),
        "upload": upload_status,
    }


def print_report(results, compile_results):
    total_panels = sum(r.total_panels for r in results)
    total_migrated = sum(r.migrated for r in results)
    total_warnings = sum(r.migrated_with_warnings for r in results)
    total_manual = sum(r.requires_manual for r in results)
    total_nf = sum(r.not_feasible for r in results)
    total_skipped = sum(r.skipped for r in results)
    compiled_ok = sum(1 for _, ok, _ in compile_results if ok)
    total_green = sum(1 for r in results for pr in r.panel_results if (pr.verification_packet or {}).get("semantic_gate") == "Green")
    total_yellow = sum(1 for r in results for pr in r.panel_results if (pr.verification_packet or {}).get("semantic_gate") == "Yellow")
    total_red = sum(1 for r in results for pr in r.panel_results if (pr.verification_packet or {}).get("semantic_gate") == "Red")
    upload_attempted = sum(1 for r in results if r.upload_attempted)
    uploaded_ok = sum(1 for r in results if r.uploaded)

    print("\n" + "=" * 70)
    print("MIGRATION REPORT")
    print("=" * 70)
    print(f"\nDashboards processed: {len(results)}")
    print(f"Total panels found:  {total_panels}")
    print(f"  Migrated:          {total_migrated} ({pct(total_migrated, total_panels)})")
    print(f"  With warnings:     {total_warnings} ({pct(total_warnings, total_panels)})")
    print(f"  Requires manual:   {total_manual} ({pct(total_manual, total_panels)})")
    print(f"  Not feasible:      {total_nf} ({pct(total_nf, total_panels)})")
    print(f"  Skipped (rows):    {total_skipped} ({pct(total_skipped, total_panels)})")
    if total_green or total_yellow or total_red:
        print(f"  Verification gate: {total_green} Green / {total_yellow} Yellow / {total_red} Red")
    print(f"\nCompilation results: {compiled_ok}/{len(compile_results)} dashboards compiled successfully")
    if upload_attempted:
        print(f"Upload results:      {uploaded_ok}/{upload_attempted} dashboards uploaded successfully")
    print()

    print("─" * 70)
    print(f"{'Dashboard':<40} {'Panels':>6} {'OK':>5} {'Warn':>5} {'Man':>5} {'NF':>5} {'Compiled':>10}")
    print("─" * 70)

    for r in results:
        comp_status = "YES" if r.compiled else "FAIL" if r.compile_error else "?"
        print(
            f"{r.dashboard_title[:39]:<40} {r.total_panels:>6} {r.migrated:>5} "
            f"{r.migrated_with_warnings:>5} {r.requires_manual:>5} {r.not_feasible:>5} {comp_status:>10}"
        )

    print("─" * 70)

    if any(not ok for _, ok, _ in compile_results):
        print("\nCOMPILATION ERRORS:")
        for name, ok, output in compile_results:
            if not ok:
                print(f"\n  {name}:")
                for line in output.strip().split("\n"):
                    if "error" in line.lower() or "validation" in line.lower():
                        print(f"    {line.strip()}")

    not_feasible_panels = [(r.dashboard_title, pr) for r in results for pr in r.panel_results if pr.status == "not_feasible"]
    if not_feasible_panels:
        print(f"\nNOT FEASIBLE ({len(not_feasible_panels)} panels):")
        for dash_title, pr in not_feasible_panels[:20]:
            print(f"  [{dash_title}] {pr.title}: {', '.join(pr.reasons)}")
            if pr.promql_expr:
                print(f"    PromQL: {pr.promql_expr[:100]}")

    total_alerts = sum(len(getattr(r, "alert_results", [])) for r in results)
    if total_alerts:
        automated = sum(sum(1 for a in getattr(r, "alert_results", []) if a.get("automation_tier") == "automated") for r in results)
        draft = sum(sum(1 for a in getattr(r, "alert_results", []) if a.get("automation_tier") == "draft_requires_review") for r in results)
        manual = sum(sum(1 for a in getattr(r, "alert_results", []) if a.get("automation_tier") == "manual_required") for r in results)
        print(f"\nAlert migration: {total_alerts} alerts")
        print(f"  Automated:     {automated}")
        print(f"  Draft review:  {draft}")
        print(f"  Manual:        {manual}")

    print("\n" + "=" * 70)


def pct(n, total):
    return f"{n / total * 100:.1f}%" if total > 0 else "0%"


def save_detailed_report(results, compile_results, output_path, validation_summary=None, validation_records=None, verification_payload=None):
    report = {
        "summary": {
            "dashboards": len(results),
            "total_panels": sum(r.total_panels for r in results),
            "migrated": sum(r.migrated for r in results),
            "migrated_with_warnings": sum(r.migrated_with_warnings for r in results),
            "requires_manual": sum(r.requires_manual for r in results),
            "not_feasible": sum(r.not_feasible for r in results),
            "skipped": sum(r.skipped for r in results),
            "compiled_ok": sum(1 for _, ok, _ in compile_results if ok),
            "uploaded_ok": sum(1 for r in results if r.uploaded),
            "upload_attempted": sum(1 for r in results if r.upload_attempted),
            "yaml_lint_ok": sum(1 for r in results if build_runtime_summary(r)["yaml_lint"]["status"] == "pass"),
            "layout_ok": sum(1 for r in results if build_runtime_summary(r)["layout"]["status"] == "pass"),
            "total_alerts": sum(len(getattr(r, "alert_results", [])) for r in results),
            "alerts_automated": sum(
                sum(1 for a in getattr(r, "alert_results", []) if a.get("automation_tier") == "automated")
                for r in results
            ),
            "alerts_draft_review": sum(
                sum(1 for a in getattr(r, "alert_results", []) if a.get("automation_tier") == "draft_requires_review")
                for r in results
            ),
            "alerts_manual": sum(
                sum(1 for a in getattr(r, "alert_results", []) if a.get("automation_tier") == "manual_required")
                for r in results
            ),
        },
        "dashboards": [],
    }
    if validation_summary or validation_records:
        report["validation"] = {
            "summary": validation_summary or {},
            "records": validation_records or [],
        }
    if verification_payload:
        report["verification"] = verification_payload
    for r in results:
        runtime_summary = build_runtime_summary(r)
        r.runtime_summary = runtime_summary
        d = {
            "title": r.dashboard_title,
            "uid": r.dashboard_uid,
            "source_file": r.source_file,
            "folder_title": r.folder_title,
            "compiled": r.compiled,
            "compile_error": r.compile_error,
            "runtime_summary": runtime_summary,
            "inventory": r.inventory,
            "metadata_polish": r.metadata_polish,
            "verification_summary": r.verification_summary,
            "review_explanations": r.review_explanations,
            "total_panels": r.total_panels,
            "migrated": r.migrated,
            "warnings": r.migrated_with_warnings,
            "manual": r.requires_manual,
            "not_feasible": r.not_feasible,
            "panels": [
                {
                    "title": pr.title,
                    "grafana_type": pr.grafana_type,
                    "kibana_type": pr.kibana_type,
                    "status": pr.status,
                    "confidence": pr.confidence,
                    "reasons": pr.reasons,
                    "promql": pr.promql_expr,
                    "esql": pr.esql_query,
                    "trace": pr.trace,
                    "source_panel_id": pr.source_panel_id,
                    "datasource_type": pr.datasource_type,
                    "datasource_uid": pr.datasource_uid,
                    "datasource_name": pr.datasource_name,
                    "query_language": pr.query_language,
                    "readiness": pr.readiness,
                    "recommended_target": pr.recommended_target,
                    "notes": pr.notes,
                    "inventory": pr.inventory,
                    "query_ir": pr.query_ir,
                    "visual_ir": _ir_to_dict(pr.visual_ir),
                    "operational_ir": _ir_to_dict(pr.operational_ir),
                    "metadata_polish": pr.metadata_polish,
                    "target_candidates": pr.target_candidates,
                    "verification_packet": pr.verification_packet,
                    "review_explanation": pr.review_explanation,
                    "runtime_rollups": pr.runtime_rollups,
                    "post_validation_action": pr.post_validation_action,
                    "post_validation_message": pr.post_validation_message,
                }
                for pr in r.panel_results
            ],
            "alert_results": getattr(r, "alert_results", []),
            "alert_summary": getattr(r, "alert_summary", {}),
        }
        report["dashboards"].append(d)

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)


__all__ = [
    "MigrationResult",
    "PanelResult",
    "_ir_to_dict",
    "_panel_query_index",
    "build_runtime_summary",
    "mark_panel_requires_manual_after_failed_validation",
    "mark_panel_requires_manual_after_validation",
    "pct",
    "print_report",
    "recompute_result_counts",
    "save_detailed_report",
]
