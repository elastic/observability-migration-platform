# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

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
from observability_migration.core.reporting.summary_md import (
    AttentionItem,
    DashboardRow,
    GapSummary,
    GapTask,
    PanelProvenance,
    SummaryTotals,
    SummaryView,
    classify_panel_provenance,
)
from observability_migration.core.verification.disposition import (
    SELF_HEAL_SEMANTIC_LOSS,
    missing_target_field_warning,
)


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
    # Dashboard-level warnings for template-variable -> Kibana control
    # translation that don't fit the per-panel PanelResult model (issue
    # #269): a control dropped because the target schema lacks its field, or
    # a cascading/label-filtered `label_values()` variable whose scope can't
    # be expressed as an inter-control dependency in Kibana. Controls have no
    # PanelResult-style tracking of their own, so this is dashboard-scoped
    # rather than per-panel.
    control_warnings: list = field(default_factory=list)
    source_file: str = ""
    folder_title: str = ""
    inventory: dict = field(default_factory=dict)
    metadata_polish: dict = field(default_factory=dict)
    verification_summary: dict = field(default_factory=dict)
    review_explanations: dict = field(default_factory=dict)
    upload_attempted: bool = False
    uploaded: bool | None = None
    upload_error: str = ""
    upload_warnings: list = field(default_factory=list)
    # Leaf panels Kibana silently dropped while accepting the upload with an
    # HTTP 200 (``{"title", "reason", "section", "grid"}`` each). Non-empty means
    # the uploaded dashboard is missing panels even though nothing failed.
    upload_dropped_panels: list = field(default_factory=list)
    kibana_saved_object_id: str = ""
    uploaded_space: str = ""
    uploaded_kibana_url: str = ""
    # Filename stem shared by this dashboard's artifacts
    # (``native/<stem>.native.json``, ``ir/<stem>.ir.json``). Empty when
    # translation failed.
    artifact_stem: str = ""
    # Native Dashboard-as-Code review artifacts (see
    # targets/kibana/native_artifacts.py): the on-disk twin of
    # `native_dashboard`/`dashboard_ir`, written before upload so the exact
    # typed API payload can be reviewed and later deployed with
    # `obs-migrate upload --artifact-dir ...`.
    native_artifact_path: str = ""
    ir_artifact_path: str = ""
    runtime_summary: dict = field(default_factory=dict)
    dashboard_links: list = field(default_factory=list)
    annotations: list = field(default_factory=list)
    alert_migration_tasks: list = field(default_factory=list)
    feature_gap_summary: dict = field(default_factory=dict)
    alert_results: list = field(default_factory=list)  # list of AlertingIR.to_dict()
    alert_summary: dict = field(default_factory=dict)  # {"total": N, "automated": N, "draft_review": N, "manual_required": N, "by_kind": {...}}
    translation_error: str = ""   # non-empty iff translate_dashboard() raised
    curated_pack: str = ""        # non-empty when a curated pack was applied (e.g. "grafana_763_redis_exporter")
    # Semantic DashboardIR -- the primary working artifact of the IR-first
    # pipeline. `native_dashboard` and the on-disk YAML are both *derived*
    # from this (see targets.kibana.dashboards_api.native_dashboard_from_ir /
    # DashboardIR.to_yaml_dict), not the other way around. Not JSON-serialized
    # directly; call .to_dict() for that.
    dashboard_ir: Any = None
    # NativeDashboard IR built from `dashboard_ir` via native_dashboard_from_ir.
    # Not JSON-serialized directly; call .to_api_payload() for that.
    native_dashboard: Any = None
    native_dashboard_stats: dict = field(default_factory=dict)


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
    target_query_contract: Any = field(default_factory=dict)
    contract_evaluation: Any = field(default_factory=dict)
    fulfillment_plan: Any = field(default_factory=dict)
    review_explanation: dict = field(default_factory=dict)
    runtime_rollups: list = field(default_factory=list)
    link_migrations: list = field(default_factory=list)
    transformation_redesign_tasks: list = field(default_factory=list)
    applied_transform_indices: list = field(default_factory=list)
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


def mark_panel_migrated_with_missing_target_fields(panel_result, validation_result):
    """Keep the real visualization for a panel whose live validation only failed
    because a target field or index has not been ingested yet.

    The translated ES|QL is structurally valid; Kibana renders an empty "no
    data" panel today and self-heals once telemetry arrives. Replacing it with a
    markdown placeholder would force a needless re-migration, so we keep the
    panel and attach a warning instead (issue #154).
    """
    panel_result.status = "migrated_with_warnings"
    if panel_result.confidence > 0.7:
        panel_result.confidence = 0.7
    reason = missing_target_field_warning(validation_result)
    _append_unique(panel_result.reasons, reason)
    _append_unique(panel_result.notes, reason)
    if isinstance(panel_result.query_ir, dict):
        panel_result.query_ir.setdefault("semantic_losses", [])
        _append_unique(panel_result.query_ir["semantic_losses"], SELF_HEAL_SEMANTIC_LOSS)


def recompute_result_counts(result):
    result.migrated = sum(1 for item in result.panel_results if item.status == "migrated")
    result.migrated_with_warnings = sum(1 for item in result.panel_results if item.status == "migrated_with_warnings")
    result.requires_manual = sum(1 for item in result.panel_results if item.status == "requires_manual")
    result.not_feasible = sum(1 for item in result.panel_results if item.status == "not_feasible")
    result.skipped = sum(1 for item in result.panel_results if item.status == "skipped")


def build_runtime_summary(result):
    upload_status = {"status": "not_run", "error": "", "warnings": [], "dropped_panels": []}
    if getattr(result, "upload_attempted", False) or getattr(result, "upload_error", ""):
        upload_status = {
            "status": "pass" if getattr(result, "uploaded", False) and not getattr(result, "upload_error", "") else "fail",
            "error": getattr(result, "upload_error", "") or "",
            "warnings": list(getattr(result, "upload_warnings", []) or []),
            # Panels Kibana dropped behind an HTTP 200. These ride in the
            # runtime summary (not just the log) so the manifest and any CI gate
            # reading it can see *which* panels a "successful" upload lost.
            "dropped_panels": list(getattr(result, "upload_dropped_panels", []) or []),
        }
    return {
        "upload": upload_status,
    }


def upload_panel_loss_rows(results):
    """``(dashboard_title, dropped_panel)`` for every panel lost on upload."""
    return [
        (getattr(result, "dashboard_title", ""), dropped)
        for result in results
        for dropped in (getattr(result, "upload_dropped_panels", None) or [])
        if isinstance(dropped, dict)
    ]


def print_upload_panel_loss(results):
    """Report panels Kibana dropped while returning HTTP 200 on the upload.

    This is a data-loss section, not a warning list: the dashboards below were
    reported as uploaded and are missing panels. Printed even though the same
    dashboards already count as upload failures, because the count alone never
    says *which* panels vanished.
    """
    rows = upload_panel_loss_rows(results)
    if not rows:
        return
    print("UPLOAD DATA LOSS (Kibana returned HTTP 200 but dropped panels):")
    for dashboard_title, dropped in rows:
        title = dropped.get("title") or "(untitled)"
        section = f" [section {dropped.get('section')}]" if dropped.get("section") else ""
        print(f"  ✗ {dashboard_title}: {title}{section}")
        reason = str(dropped.get("reason") or "")
        if reason:
            print(f"      {reason[:300]}")
    print(
        f"  {len(rows)} panel(s) are missing from the uploaded dashboard(s). "
        "Fix the panel(s) and re-upload."
    )
    print()


def _row_count(result):
    """Number of Grafana ``type=="row"`` containers recorded on a result.

    Rows are structural section dividers, not panels — they're tracked in
    panel_results with grafana_type=="row" so we can separate them out for
    reporting without changing the underlying data model.
    """
    return sum(1 for pr in result.panel_results if pr.grafana_type == "row")


def _element_summary(panels: int, rows: int) -> str:
    """Render the ``N total (X panels [+ Y rows])`` line content.

    Panels and rows are pluralised independently so single-element dashboards
    don't read awkwardly ("1 panels"). Rows are omitted when zero so dashboards
    without any structural containers stay tidy (also the Datadog-style case
    if this function is reused).
    """
    total = panels + rows
    panel_word = "panel" if panels == 1 else "panels"
    if rows:
        row_word = "row" if rows == 1 else "rows"
        breakdown = f"{panels} {panel_word} + {rows} {row_word}"
    else:
        breakdown = f"{panels} {panel_word}"
    return f"{total} total ({breakdown})"


def _describe_field_discovery(field_discovery):
    """Human-readable cause for an OTel/pass-through field fallback."""
    status = (field_discovery or {}).get("status", "")
    index_pattern = (field_discovery or {}).get("index_pattern") or "metrics-*"
    error = (field_discovery or {}).get("error") or ""
    schema_profile = (field_discovery or {}).get("schema_profile")
    if status == "offline":
        return "target schema discovery did not run (no --es-url provided)"
    if status == "empty":
        return f"target schema discovery found no fields under '{index_pattern}'"
    if status == "error":
        detail = f": {error}" if error else ""
        return f"target schema discovery failed{detail}"
    # Discovery succeeded. A recognized profile that still triggered a fallback
    # is missing some of the dashboards' fields; otherwise no layout matched.
    if schema_profile:
        return f"target schema '{schema_profile}' is missing some fields the dashboards use"
    return f"target schema under '{index_pattern}' was not recognized"


def print_field_discovery_warning(field_discovery):
    """Print a prominent, top-of-summary warning when migrated panels query
    unverified OTel field defaults. No-op unless ``otel_fallback`` is set, so
    a normal run (discovery succeeded and fields resolved) prints nothing."""
    if not field_discovery or not field_discovery.get("otel_fallback"):
        return
    index_pattern = field_discovery.get("index_pattern") or "metrics-*"
    print("\n" + "!" * 70)
    print("WARNING: migrated panels may render empty")
    print("!" * 70)
    if field_discovery.get("field_profile") == "passthrough":
        if field_discovery.get("status") == "ok":
            print(
                f"  Raw source fields are missing from target index "
                f"'{index_pattern}'."
            )
        else:
            print(f"  {_describe_field_discovery(field_discovery)}.")
        print(
            "  Strict passthrough emits raw source label and metric names against\n"
            f"  index '{index_pattern}'. Without matching target fields these names\n"
            "  are unverified and panels can come up empty."
        )
        print(
            "  Fix: ensure the target stores the raw Prometheus names, or re-run\n"
            "       with --field-profile otel and --es-url for automatic mapping."
        )
        return
    print(f"  {_describe_field_discovery(field_discovery)}.")
    # Discovery that ran did reach the target, so don't claim the schema is
    # missing — distinguish a recognized-but-incomplete schema from one where
    # no known layout matched at all.
    if field_discovery.get("status") == "ok":
        if field_discovery.get("schema_profile"):
            cause = "Some fields the dashboards use are absent from the target"
        else:
            cause = "No known Prometheus or OTel field layout was detected"
    else:
        cause = "The target schema is unknown"
    print(
        f"  {cause}, so queries fall back to OTel field defaults\n"
        f"  (e.g. service.name) and index '{index_pattern}'. These are unverified\n"
        "  guesses that may not match your data, so panels can come up empty."
    )
    print(
        "  Fix: re-run pointing the migration at your target data —\n"
        "       --es-url (and --es-api-key if required) for schema discovery, and\n"
        "       --esql-index for the index/data stream your metrics live in."
    )


def print_report(results, field_discovery=None):
    total_rows = sum(_row_count(r) for r in results)
    # ``total_panels`` on the MigrationResult includes rows (it's the raw count
    # from _flatten_dashboard_panels). The user-facing "renderable panels"
    # number subtracts rows so the breakdown (Migrated/Warn/Man/NF/Skipped)
    # sums consistently.
    total_panels = sum(r.total_panels for r in results) - total_rows
    total_migrated = sum(r.migrated for r in results)
    total_warnings = sum(r.migrated_with_warnings for r in results)
    total_manual = sum(r.requires_manual for r in results)
    total_nf = sum(r.not_feasible for r in results)
    # r.skipped counts every panel with status=="skipped" — including rows.
    # The remainder is genuine panel skips (variable-expansion warnings, L4
    # repeat caps, non-normalized group panels, etc.).
    total_panel_skipped = sum(r.skipped for r in results) - total_rows
    total_green = sum(
        1
        for r in results
        for pr in r.panel_results
        if pr.grafana_type != "row"
        and (pr.verification_packet or {}).get("semantic_gate") == "Green"
    )
    total_yellow = sum(
        1
        for r in results
        for pr in r.panel_results
        if pr.grafana_type != "row"
        and (pr.verification_packet or {}).get("semantic_gate") == "Yellow"
    )
    total_red = sum(
        1
        for r in results
        for pr in r.panel_results
        if pr.grafana_type != "row"
        and (pr.verification_packet or {}).get("semantic_gate") == "Red"
    )
    upload_attempted = sum(1 for r in results if r.upload_attempted)
    uploaded_ok = sum(1 for r in results if r.uploaded)

    print("\n" + "=" * 70)
    print("MIGRATION REPORT")
    print("=" * 70)
    print_field_discovery_warning(field_discovery)
    print(f"\nDashboards processed: {len(results)}")
    # One summary line surfaces both the source-side total and the panel/row
    # split so the reader can verify the math at a glance.
    print(f"Elements:            {_element_summary(total_panels, total_rows)}")
    print(f"Renderable panels:   {total_panels}")
    print(f"  Migrated:          {total_migrated} ({pct(total_migrated, total_panels)})")
    print(f"  With warnings:     {total_warnings} ({pct(total_warnings, total_panels)})")
    print(f"  Requires manual:   {total_manual} ({pct(total_manual, total_panels)})")
    print(f"  Not feasible:      {total_nf} ({pct(total_nf, total_panels)})")
    # ``Skipped`` is always shown so the breakdown shape stays predictable for
    # log-diff / grep workflows; the other four states already print at zero.
    print(f"  Skipped:           {total_panel_skipped} ({pct(total_panel_skipped, total_panels)})")
    if total_green or total_yellow or total_red:
        print(f"Verification gate:   {total_green} Green / {total_yellow} Yellow / {total_red} Red")
    if upload_attempted:
        print(f"Upload results:      {uploaded_ok}/{upload_attempted} dashboards uploaded successfully")
    print()
    print_upload_panel_loss(results)

    print("─" * 70)
    # ``Skip`` and ``Rows`` columns make the per-dashboard totals add up
    # (Panels = OK + Warn + Man + NF + Skip; Rows is informational).
    print(
        f"{'Dashboard':<40} {'Panels':>6} {'OK':>5} {'Warn':>5} {'Man':>5} "
        f"{'NF':>5} {'Skip':>5} {'Rows':>5}"
    )
    print("─" * 70)

    for r in results:
        rows_for_dashboard = _row_count(r)
        panels_for_dashboard = r.total_panels - rows_for_dashboard
        skip_for_dashboard = r.skipped - rows_for_dashboard
        print(
            f"{r.dashboard_title[:39]:<40} {panels_for_dashboard:>6} {r.migrated:>5} "
            f"{r.migrated_with_warnings:>5} {r.requires_manual:>5} {r.not_feasible:>5} "
            f"{skip_for_dashboard:>5} {rows_for_dashboard:>5}"
        )

    print("─" * 70)

    not_feasible_panels = [(r.dashboard_title, pr) for r in results for pr in r.panel_results if pr.status == "not_feasible"]
    if not_feasible_panels:
        print(f"\nNOT FEASIBLE ({len(not_feasible_panels)} panels):")
        for dash_title, pr in not_feasible_panels[:20]:
            print(f"  [{dash_title}] {pr.title}: {', '.join(pr.reasons)}")
            if pr.promql_expr:
                print(f"    PromQL: {pr.promql_expr[:100]}")

    # Controls have no PanelResult-style tracking of their own (issue #269),
    # so a dropped/degraded template-variable control is otherwise invisible
    # in this summary even though it changes what the migrated dashboard can
    # filter on.
    control_warnings = [
        (r.dashboard_title, warning) for r in results for warning in getattr(r, "control_warnings", [])
    ]
    if control_warnings:
        print(f"\nCONTROL WARNINGS ({len(control_warnings)}):")
        for dash_title, warning in control_warnings[:20]:
            print(f"  [{dash_title}] {warning}")

    # Data-readiness gaps. Live discovery can PROVE a metric is absent from the
    # target, and we still emit the query so the panel self-heals once the
    # telemetry lands. But ES|QL rejects an unknown column outright, so until
    # then Kibana renders a red "Unknown column [...]" card -- where Grafana
    # would simply have drawn an empty panel. Staying quiet about that means
    # the operator meets it in the browser instead of in the run that knew.
    readiness_gaps = []
    for result in results:
        for panel in getattr(result, "panel_results", []) or []:
            for reason in (getattr(panel, "reasons", None) or []):
                if "missing from live schema discovery" in str(reason):
                    readiness_gaps.append(
                        (result.dashboard_title, getattr(panel, "title", ""), str(reason))
                    )
    if readiness_gaps:
        print(f"\nDATA READINESS ({len(readiness_gaps)} panel(s) will show an error until telemetry lands):")
        for dash_title, panel_title, reason in readiness_gaps[:20]:
            field = reason.split("Target field ", 1)[-1].split(" is missing", 1)[0]
            print(f"  [{dash_title}] {panel_title}: '{field}' is absent from the target.")
        print(
            "  These panels are translated correctly. Kibana shows 'Unknown column'\n"
            "  rather than an empty chart because ES|QL rejects unknown columns; they\n"
            "  start working as soon as the metric is ingested."
        )

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


def save_detailed_report(results, output_path, validation_summary=None, validation_records=None, verification_payload=None, field_discovery=None, metric_map_summary=None):
    runtime_features = {}
    for result in results:
        runtime_features.update(dict(getattr(result, "runtime_features", {}) or {}))
    report = {
        "summary": {
            "dashboards": len(results),
            "total_panels": sum(r.total_panels for r in results),
            "migrated": sum(r.migrated for r in results),
            "migrated_with_warnings": sum(r.migrated_with_warnings for r in results),
            "requires_manual": sum(r.requires_manual for r in results),
            "not_feasible": sum(r.not_feasible for r in results),
            "skipped": sum(r.skipped for r in results),
            "uploaded_ok": sum(1 for r in results if r.uploaded),
            "upload_attempted": sum(1 for r in results if r.upload_attempted),
            # Leaf panels Kibana dropped behind an HTTP 200 upload. Any non-zero
            # value means at least one "uploaded" dashboard is incomplete.
            "upload_panels_dropped": len(upload_panel_loss_rows(results)),
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
        "runtime_features": runtime_features,
        "dashboards": [],
    }
    if field_discovery is not None:
        report["field_discovery"] = field_discovery
    if metric_map_summary is not None:
        report["metric_map_summary"] = metric_map_summary
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
                    "target_query_contract": _ir_to_dict(pr.target_query_contract),
                    "contract_evaluation": _ir_to_dict(pr.contract_evaluation),
                    "fulfillment_plan": _ir_to_dict(pr.fulfillment_plan),
                    "review_explanation": pr.review_explanation,
                    "runtime_rollups": pr.runtime_rollups,
                    "post_validation_action": pr.post_validation_action,
                    "post_validation_message": pr.post_validation_message,
                }
                for pr in r.panel_results
            ],
            "alert_results": getattr(r, "alert_results", []),
            "alert_summary": getattr(r, "alert_summary", {}),
            "control_warnings": getattr(r, "control_warnings", []),
            "translation_error": r.translation_error,
        }
        report["dashboards"].append(d)

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)


def _gap_tasks_from_grafana(gap_data: dict) -> list[GapTask]:
    """Map the Grafana feature-gap report dict to a normalized GapTask list."""
    tasks: list[GapTask] = []
    if not isinstance(gap_data, dict):
        return tasks
    for t in (gap_data.get("transformation_redesign", {}) or {}).get("tasks", []) or []:
        tasks.append(
            GapTask(
                category="transformation",
                dashboard=t.get("dashboard", ""),
                item=t.get("panel", ""),
                detail=t.get("transform_id", "") or t.get("task_type", ""),
                kibana_alternative=t.get("kibana_alternative", ""),
                complexity=t.get("complexity", ""),
            )
        )
    for a in (gap_data.get("annotations", {}) or {}).get("items", []) or []:
        if a.get("kibana_action") in ("unsupported", "manual"):
            tasks.append(
                GapTask(
                    category="annotation",
                    dashboard="",
                    item=a.get("name", ""),
                    detail=a.get("description", ""),
                    kibana_alternative="",
                )
            )
    return tasks


def build_summary_view(
    results,
    *,
    review_queue=None,
    gap_data=None,
    run_id: str = "",
) -> SummaryView:
    """Build a normalized SummaryView from a Grafana/shared MigrationResult list."""
    import time as _time

    review_queue = review_queue or []
    gap_data = gap_data or {}

    def _renderable(r):
        return [pr for pr in r.panel_results if pr.grafana_type != "row"]

    def _gate(pr, name):
        return (pr.verification_packet or {}).get("semantic_gate") == name

    def _provenance(pr):
        return classify_panel_provenance(
            status=pr.status,
            query=getattr(pr, "esql_query", "") or "",
            query_ir=getattr(pr, "query_ir", {}),
            kibana_type=getattr(pr, "kibana_type", "") or "",
        )

    elements_total = sum(len(_renderable(r)) for r in results)
    rows_total = sum(1 for r in results for pr in r.panel_results if pr.grafana_type == "row")
    skipped = sum(r.skipped for r in results) - rows_total

    totals = SummaryTotals(
        dashboards=len(results),
        elements_total=elements_total,
        migrated=sum(r.migrated for r in results),
        warnings=sum(r.migrated_with_warnings for r in results),
        manual=sum(r.requires_manual for r in results),
        not_feasible=sum(r.not_feasible for r in results),
        skipped=max(skipped, 0),
        green=sum(1 for r in results for pr in _renderable(r) if _gate(pr, "Green")),
        yellow=sum(1 for r in results for pr in _renderable(r) if _gate(pr, "Yellow")),
        red=sum(1 for r in results for pr in _renderable(r) if _gate(pr, "Red")),
        uploaded_ok=sum(1 for r in results if r.uploaded),
        upload_attempted=sum(1 for r in results if r.upload_attempted),
        native_promql=sum(
            1 for r in results for pr in _renderable(r) if _provenance(pr) == PanelProvenance.NATIVE
        ),
        esql_translated=sum(
            1 for r in results for pr in _renderable(r) if _provenance(pr) == PanelProvenance.ESQL
        ),
        placeholder=sum(
            1 for r in results for pr in _renderable(r) if _provenance(pr) == PanelProvenance.PLACEHOLDER
        ),
    )

    risk_by_title = {item.get("dashboard"): item.get("risk_score") for item in review_queue}

    dashboards: list[DashboardRow] = []
    attention: list[AttentionItem] = []
    warning_items: list[AttentionItem] = []
    for r in results:
        renderable = _renderable(r)
        prov = [_provenance(pr) for pr in renderable]
        dashboards.append(
            DashboardRow(
                title=r.dashboard_title,
                # Match the ✓/⚠/?/✗ columns — exclude skipped so the row sums.
                elements=r.migrated + r.migrated_with_warnings + r.requires_manual + r.not_feasible,
                migrated=r.migrated,
                warnings=r.migrated_with_warnings,
                manual=r.requires_manual,
                not_feasible=r.not_feasible,
                risk_score=risk_by_title.get(r.dashboard_title),
                rollout_state="",
                native_promql=prov.count(PanelProvenance.NATIVE),
                esql_translated=prov.count(PanelProvenance.ESQL),
                placeholder=prov.count(PanelProvenance.PLACEHOLDER),
            )
        )
        seen_attention: set = set()
        for pr in renderable:
            if pr.status in ("not_feasible", "requires_manual", "blocked"):
                attention.append(
                    AttentionItem(
                        dashboard=r.dashboard_title,
                        panel=pr.title,
                        status=pr.status,
                        reasons=list(pr.reasons),
                        source_query=pr.promql_expr,
                    )
                )
                seen_attention.add(pr.title)
            elif pr.status == "migrated_with_warnings":
                warning_items.append(
                    AttentionItem(
                        dashboard=r.dashboard_title,
                        panel=pr.title,
                        status="warning",
                        reasons=list(pr.reasons),
                    )
                )
        control_warnings = list(getattr(r, "control_warnings", []) or [])
        if control_warnings:
            warning_items.append(
                AttentionItem(
                    dashboard=r.dashboard_title,
                    panel="Dashboard controls",
                    status="warning",
                    reasons=control_warnings,
                )
            )
        # Red panels not already flagged above are added to the worklist (deduped).
        for pr in renderable:
            if _gate(pr, "Red") and pr.title not in seen_attention:
                attention.append(
                    AttentionItem(
                        dashboard=r.dashboard_title,
                        panel=pr.title,
                        status="red",
                        reasons=list(pr.reasons),
                        source_query=pr.promql_expr,
                    )
                )
                seen_attention.add(pr.title)

    gaps = GapSummary(
        links=(gap_data.get("links", {}) or {}).get("summary", {}),
        annotations=(gap_data.get("annotations", {}) or {}).get("summary", {}),
        transformations=(gap_data.get("transformation_redesign", {}) or {}).get("summary", {}),
        alerts=(gap_data.get("alert_migration", {}) or {}).get("summary", {}),
        tasks=_gap_tasks_from_grafana(gap_data),
    )

    return SummaryView(
        source="grafana",
        element_noun="panel",
        run_id=run_id,
        timestamp=_time.time(),
        totals=totals,
        dashboards=dashboards,
        attention=attention,
        warnings=warning_items,
        gaps=gaps,
    )


__all__ = [
    "MigrationResult",
    "PanelResult",
    "_ir_to_dict",
    "_panel_query_index",
    "build_runtime_summary",
    "build_summary_view",
    "mark_panel_requires_manual_after_failed_validation",
    "mark_panel_requires_manual_after_validation",
    "pct",
    "print_report",
    "recompute_result_counts",
    "save_detailed_report",
]
