#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Audit and trace the migration pipeline for all available dashboards.

Captures every intermediate layer for semantic review and can generate
both JSON audit files and Markdown pipeline-trace documents.

Usage:
    # Audit all dashboards, write JSON + Markdown to audit_output/
    python scripts/audit_pipeline.py --output-dir audit_output

    # Only Grafana, only JSON
    python scripts/audit_pipeline.py --source grafana --format json

    # Only Datadog, generate pipeline-trace markdown
    python scripts/audit_pipeline.py --source datadog --format markdown

    # Specific dashboard files
    python scripts/audit_pipeline.py --files infra/grafana/dashboards/node-exporter-full.json

    # Update docs — writes all three trace docs:
    #   docs/pipeline-trace.md  (shared)
    #   docs/sources/grafana-trace.md
    #   docs/sources/datadog-trace.md
    python scripts/audit_pipeline.py --update-docs

    # Update only the Grafana trace doc
    python scripts/audit_pipeline.py --update-docs --source grafana
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class PanelAudit:
    title: str = ""
    source_type: str = ""
    source_panel_type: str = ""
    kibana_type: str = ""
    status: str = ""
    confidence: float = 0.0
    source_queries: list[str] = field(default_factory=list)
    source_queries_detail: list[dict] = field(default_factory=list)
    translated_query: str = ""
    query_ir: dict = field(default_factory=dict)
    visual_ir: dict = field(default_factory=dict)
    operational_ir: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    semantic_losses: list[str] = field(default_factory=list)
    yaml_fragment: dict = field(default_factory=dict)
    translation_path: str = ""
    inventory: dict = field(default_factory=dict)
    readiness: str = ""
    query_language: str = ""
    datasource_type: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class DashboardAudit:
    source: str = ""
    file_name: str = ""
    dashboard_title: str = ""
    dashboard_id: str = ""
    total_panels: int = 0
    status_counts: dict = field(default_factory=dict)
    panels: list[PanelAudit] = field(default_factory=list)
    yaml_content: str = ""
    controls: list[dict] = field(default_factory=list)
    template_variables: list[dict] = field(default_factory=list)
    feature_gap_summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_grafana_translation_path(panel_result: Any, notes: list[str]) -> str:
    notes_lower = " ".join(str(n) for n in notes).lower()
    if "native promql" in notes_lower:
        return "native_promql"
    ql = str(getattr(panel_result, "query_language", "") or "").lower()
    if ql == "esql":
        return "native_esql"
    if ql == "logql":
        return "logql"
    if ql == "text":
        return "static"
    status = str(getattr(panel_result, "status", "") or "").lower()
    if status == "skipped":
        return "skipped"
    if status == "not_feasible":
        return "not_feasible"
    trace = getattr(panel_result, "trace", []) or []
    if trace:
        return "rule_engine"
    esql = str(getattr(panel_result, "esql_query", "") or "")
    if esql.strip().upper().startswith("PROMQL"):
        return "native_promql"
    if esql:
        return "rule_engine"
    return "unknown"


def _safe_ir_dict(ir_obj: Any) -> dict:
    if ir_obj is None:
        return {}
    if isinstance(ir_obj, dict):
        return ir_obj
    if hasattr(ir_obj, "to_dict"):
        return ir_obj.to_dict()
    return {}


def _build_dd_query_ir(result: Any) -> dict:
    """Build a lightweight query IR from a Datadog TranslationResult."""
    query = str(getattr(result, "esql_query", "") or "")
    if not query:
        return {}
    try:
        from observability_migration.targets.kibana.emit.esql_utils import extract_esql_shape
        shape = extract_esql_shape(query)
        metric_field = ""
        if shape.metric_fields:
            metric_field = shape.metric_fields[0]
        elif shape.projected_fields:
            metric_field = shape.projected_fields[0]
        group_fields = list(shape.group_fields or shape.time_fields or [])
    except Exception:
        metric_field = ""
        group_fields = []
    source_queries = list(getattr(result, "source_queries", []) or [])
    return {
        "source_expression": source_queries[0] if source_queries else "",
        "target_query": query,
        "output_metric_field": metric_field,
        "output_group_fields": group_fields,
        "semantic_losses": list(getattr(result, "semantic_losses", []) or []),
    }


# ---------------------------------------------------------------------------
# Grafana audit
# ---------------------------------------------------------------------------

def _audit_grafana_dashboard(dashboard_path: Path, data_view: str) -> DashboardAudit:
    from observability_migration.adapters.source.grafana.panels import translate_dashboard

    raw = json.loads(dashboard_path.read_text())
    if "dashboard" in raw and isinstance(raw["dashboard"], dict):
        dash = raw["dashboard"]
    else:
        dash = raw
    dash["__source_file__"] = str(dashboard_path)

    tmp = tempfile.mkdtemp(prefix="audit_grafana_")
    try:
        result, yaml_path = translate_dashboard(
            dash, tmp,
            datasource_index=data_view,
            esql_index=data_view,
        )
    except Exception as exc:
        return DashboardAudit(
            source="grafana",
            file_name=dashboard_path.name,
            dashboard_title=dash.get("title", ""),
            panels=[PanelAudit(
                title="PIPELINE_ERROR",
                status="error",
                warnings=[f"Pipeline crashed: {exc}"],
            )],
        )

    yaml_content = yaml_path.read_text() if yaml_path and yaml_path.exists() else ""

    panels = []
    for pr in result.panel_results:
        qir = pr.query_ir if isinstance(pr.query_ir, dict) else {}

        vir = {}
        if hasattr(pr, "visual_ir") and pr.visual_ir is not None:
            vir = pr.visual_ir.to_dict() if hasattr(pr.visual_ir, "to_dict") else {}

        oir = {}
        if hasattr(pr, "operational_ir") and pr.operational_ir is not None:
            oir = pr.operational_ir.to_dict() if hasattr(pr.operational_ir, "to_dict") else {}

        inv = dict(getattr(pr, "inventory", {}) or {})
        notes = list(getattr(pr, "notes", []) or [])
        readiness = str(getattr(pr, "readiness", "") or "")
        query_lang = str(getattr(pr, "query_language", "") or "")
        ds_type = str(getattr(pr, "datasource_type", "") or "")

        translation_path = _infer_grafana_translation_path(pr, notes)

        panels.append(PanelAudit(
            title=pr.title,
            source_type="grafana",
            source_panel_type=pr.grafana_type,
            kibana_type=pr.kibana_type,
            status=pr.status,
            confidence=pr.confidence,
            source_queries=[pr.promql_expr] if pr.promql_expr else [],
            translated_query=pr.esql_query,
            query_ir=qir,
            visual_ir=vir,
            operational_ir=oir,
            trace=pr.trace,
            warnings=notes + list(pr.reasons),
            semantic_losses=qir.get("semantic_losses", []) if isinstance(qir, dict) else [],
            translation_path=translation_path,
            inventory=inv,
            readiness=readiness,
            query_language=query_lang,
            datasource_type=ds_type,
            notes=notes,
        ))

    counts = {
        "migrated": result.migrated,
        "migrated_with_warnings": result.migrated_with_warnings,
        "requires_manual": result.requires_manual,
        "not_feasible": result.not_feasible,
        "skipped": result.skipped,
    }

    controls = []
    if yaml_content:
        try:
            import yaml as _yaml
            doc = _yaml.safe_load(yaml_content) or {}
            dashboards = doc.get("dashboards", [])
            if dashboards:
                controls = dashboards[0].get("controls", [])
        except Exception:
            pass

    return DashboardAudit(
        source="grafana",
        file_name=dashboard_path.name,
        dashboard_title=result.dashboard_title,
        dashboard_id=result.dashboard_uid,
        total_panels=result.total_panels,
        status_counts=counts,
        panels=panels,
        yaml_content=yaml_content,
        controls=controls,
        feature_gap_summary=dict(getattr(result, "feature_gap_summary", {}) or {}),
    )


# ---------------------------------------------------------------------------
# Datadog audit
# ---------------------------------------------------------------------------

def _audit_datadog_dashboard(dashboard_path: Path, data_view: str) -> DashboardAudit:
    from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
    from observability_migration.adapters.source.datadog.generate import generate_dashboard_yaml
    from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
    from observability_migration.adapters.source.datadog.planner import plan_widget
    from observability_migration.adapters.source.datadog.translate import translate_widget

    raw = json.loads(dashboard_path.read_text())
    try:
        dashboard = normalize_dashboard(raw)
    except Exception as exc:
        return DashboardAudit(
            source="datadog",
            file_name=dashboard_path.name,
            panels=[PanelAudit(
                title="NORMALIZE_ERROR",
                status="error",
                warnings=[f"Normalization crashed: {exc}"],
            )],
        )

    field_map = OTEL_PROFILE
    panels_audit: list[PanelAudit] = []
    panel_results = []
    status_counts: dict[str, int] = {}

    def process_widget(widget, depth=0):
        try:
            plan = plan_widget(widget)
            result = translate_widget(widget, plan, field_map)
        except Exception as exc:
            panels_audit.append(PanelAudit(
                title=widget.title or widget.id,
                source_type="datadog",
                source_panel_type=widget.widget_type,
                status="error",
                warnings=[f"Translation crashed: {exc}"],
            ))
            return

        panel_results.append(result)
        s = result.status
        status_counts[s] = status_counts.get(s, 0) + 1

        query_details = []
        for q in widget.queries:
            query_details.append({
                "raw_query": q.raw_query,
                "metric_query": str(q.metric_query) if q.metric_query else "",
                "log_query": str(q.log_query) if q.log_query else "",
                "data_source": q.data_source,
            })

        qir = _build_dd_query_ir(result)

        dd_translation_path = plan.backend or "unknown"
        if dd_translation_path == "esql":
            ds_types = {q.data_source for q in widget.queries if q.data_source}
            if "logs" in ds_types or any(q.log_query for q in widget.queries):
                dd_translation_path = "esql_log"
            elif any(getattr(widget, "formulas", None) or []):
                dd_translation_path = "esql_formula"
            else:
                dd_translation_path = "esql_metric"

        panels_audit.append(PanelAudit(
            title=result.title or widget.title or widget.id,
            source_type="datadog",
            source_panel_type=widget.widget_type,
            kibana_type=result.kibana_type,
            status=result.status,
            confidence=result.confidence,
            source_queries=result.source_queries,
            source_queries_detail=query_details,
            translated_query=result.esql_query,
            query_ir=qir,
            plan={
                "backend": plan.backend,
                "kibana_type": plan.kibana_type,
                "reasons": plan.reasons,
                "trace": plan.trace,
                "data_source": plan.data_source,
                "field_issues": plan.field_issues,
            },
            trace=result.trace,
            warnings=result.warnings,
            semantic_losses=result.semantic_losses,
            yaml_fragment=dict(getattr(result, "yaml_panel", {}) or {}),
            translation_path=dd_translation_path,
            query_language=str(getattr(result, "query_language", "") or ""),
            datasource_type="datadog",
        ))

        for child in (widget.children or []):
            process_widget(child, depth + 1)

    for widget in dashboard.widgets:
        process_widget(widget)

    try:
        yaml_str = generate_dashboard_yaml(
            dashboard, panel_results, data_view=data_view,
            field_map=field_map,
        )
    except Exception as exc:
        yaml_str = f"# YAML generation failed: {exc}"

    for pa in panels_audit:
        wid = pa.title
        for r in panel_results:
            if (r.title == pa.title or getattr(r, "widget_id", "") == wid):
                yp = getattr(r, "yaml_panel", None)
                if yp and isinstance(yp, dict):
                    pa.yaml_fragment = {k: v for k, v in yp.items() if not k.startswith("_")}
                break

    tpl_vars = []
    for tv in (dashboard.template_variables or []):
        tpl_vars.append({
            "name": tv.name,
            "tag": tv.tag,
            "default": tv.default,
            "prefix": getattr(tv, "prefix", ""),
        })

    return DashboardAudit(
        source="datadog",
        file_name=dashboard_path.name,
        dashboard_title=dashboard.title,
        dashboard_id=dashboard.id,
        total_panels=len(panels_audit),
        status_counts=status_counts,
        panels=panels_audit,
        yaml_content=yaml_str,
        template_variables=tpl_vars,
    )


# ---------------------------------------------------------------------------
# Markdown pipeline-trace generator
# ---------------------------------------------------------------------------

def _verdict(panel: PanelAudit) -> str:
    if panel.status in ("error",):
        return "ERROR"
    if panel.status in ("not_feasible", "blocked", "skipped"):
        return "EXPECTED_LIMITATION"
    if panel.semantic_losses:
        return "MINOR_ISSUE"
    if panel.warnings:
        for w in panel.warnings:
            if any(k in w.lower() for k in ("collapsed", "approximated", "dropped", "meta-metric")):
                return "MINOR_ISSUE"
    if not panel.translated_query and panel.status != "skipped":
        return "EXPECTED_LIMITATION"
    return "CORRECT"


def _escape_md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _truncate(text: str, maxlen: int = 120) -> str:
    if len(text) <= maxlen:
        return text
    return text[:maxlen] + "..."


def _render_query_ir(lines: list[str], qir: dict) -> None:
    if not qir:
        return
    lines.append("**Query IR:**\n")
    key_fields = [
        ("family", "Family"), ("metric", "Metric"),
        ("range_function", "Range func"), ("range_window", "Range window"),
        ("outer_agg", "Outer agg"), ("group_labels", "Group labels"),
        ("label_filters", "Label filters"), ("binary_op", "Binary op"),
        ("output_shape", "Output shape"), ("source_language", "Source lang"),
        ("target_index", "Target index"),
        ("output_metric_field", "Output metric"), ("output_group_fields", "Output groups"),
    ]
    shown = False
    for key, label in key_fields:
        val = qir.get(key)
        if val and val not in ([], {}, "", 0, False):
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            lines.append(f"- {label}: `{val}`")
            shown = True
    losses = qir.get("semantic_losses", [])
    if losses:
        lines.append(f"- Semantic losses: {', '.join(losses)}")
        shown = True
    if not shown:
        target_q = qir.get("target_query", "")
        if target_q:
            lines.append(f"- Target query: `{_truncate(target_q, 80)}`")
    lines.append("")


def _render_visual_ir(lines: list[str], vir: dict) -> None:
    if not vir:
        return
    layout = vir.get("layout", {})
    pres = vir.get("presentation", {})
    kibana_type = vir.get("kibana_type", "")
    if not layout and not pres and not kibana_type:
        return
    lines.append("**Visual IR:**\n")
    if kibana_type:
        lines.append(f"- Kibana type: `{kibana_type}`")
    if layout:
        lines.append(f"- Layout: x={layout.get('x', 0)}, y={layout.get('y', 0)}, "
                     f"w={layout.get('w', 0)}, h={layout.get('h', 0)}")
    if pres:
        kind = pres.get("kind", "")
        if kind:
            lines.append(f"- Presentation kind: `{kind}`")
        config = pres.get("config", {})
        if config:
            config_keys = list(config.keys())[:5]
            lines.append(f"- Config keys: {', '.join(config_keys)}")
    lines.append("")


def _render_operational_ir(lines: list[str], oir: dict) -> None:
    if not oir:
        return
    review = oir.get("review", {})
    lineage = oir.get("lineage", {})
    deployment = oir.get("deployment", {})
    has_content = False
    for section in (review, lineage, deployment):
        for v in section.values():
            if v and v not in ("", "not_run", 0, [], {}):
                has_content = True
                break
    if not has_content:
        return
    lines.append("**Operational IR:**\n")
    if review:
        gate = review.get("semantic_gate", "")
        vmode = review.get("verification_mode", "")
        vstatus = review.get("validation_status", "")
        if gate:
            lines.append(f"- Semantic gate: `{gate}`")
        if vmode:
            lines.append(f"- Verification mode: `{vmode}`")
        if vstatus and vstatus != "not_run":
            lines.append(f"- Validation status: `{vstatus}`")
    if deployment:
        ql = deployment.get("query_language", "")
        rollups = deployment.get("runtime_rollups", [])
        if ql:
            lines.append(f"- Query language: `{ql}`")
        if rollups:
            lines.append(f"- Runtime rollups: {', '.join(rollups)}")
    lines.append("")


def generate_pipeline_trace_md(audits: list[DashboardAudit]) -> str:
    lines: list[str] = []
    lines.append("# Pipeline Trace — Auto-Generated Audit\n")
    lines.append("This document is generated by `scripts/audit_pipeline.py` and traces")
    lines.append("real dashboards through every layer of the migration pipeline.\n")
    lines.append("Each panel shows: source query → translation trace → translated query → verdict.\n")

    # Summary table — rows (type=="row") are structural containers, not panels,
    # so the Panels column and the Skipped column both exclude them. A separate
    # ``Rows`` column surfaces the count without conflating it with skipped panels.
    lines.append("## Dashboard Summary\n")
    lines.append("| Source | Dashboard | Panels | Migrated | Warnings | Manual | Not Feasible | Skipped | Rows |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for a in audits:
        c = a.status_counts
        row_count = sum(1 for p in a.panels if p.source_panel_type == "row")
        audited_panels = len(a.panels) - row_count
        panel_skipped = max(c.get("skipped", 0) - row_count, 0)
        lines.append(
            f"| {a.source} | {_escape_md(a.dashboard_title or a.file_name)} "
            f"| {audited_panels} "
            f"| {c.get('migrated', c.get('ok', 0))} "
            f"| {c.get('migrated_with_warnings', c.get('warning', 0))} "
            f"| {c.get('requires_manual', 0)} "
            f"| {c.get('not_feasible', 0)} "
            f"| {panel_skipped} "
            f"| {row_count} |"
        )
    lines.append("")

    # Verdict summary
    verdicts: dict[str, int] = {}
    for a in audits:
        for p in a.panels:
            v = _verdict(p)
            verdicts[v] = verdicts.get(v, 0) + 1
    lines.append("## Verdict Summary\n")
    lines.append("| Verdict | Count |")
    lines.append("|---|---|")
    for v in ("CORRECT", "MINOR_ISSUE", "EXPECTED_LIMITATION", "WRONG", "ERROR"):
        if v in verdicts:
            lines.append(f"| {v} | {verdicts[v]} |")
    lines.append("")

    # Per-dashboard detail
    for a in audits:
        audited_panels = len(a.panels)
        lines.append("---\n")
        lines.append(f"## {a.source.title()}: {a.dashboard_title or a.file_name}\n")
        lines.append(f"**File:** `{a.file_name}` — **Panels:** {audited_panels}\n")

        lines.append("| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |")
        lines.append("|---|---|---|---|---|---|")
        for p in a.panels:
            src_q = _truncate(_escape_md(p.source_queries[0]) if p.source_queries else "—", 80)
            tgt_q = _truncate(_escape_md(p.translated_query or "—"), 80)
            v = _verdict(p)
            lines.append(
                f"| {_escape_md(p.title or '(untitled)')} "
                f"| `{p.source_panel_type}` → `{p.kibana_type}` "
                f"| {p.status} | **{v}** "
                f"| {src_q} "
                f"| {tgt_q} |"
            )
        lines.append("")

        # Detailed traces for non-trivial panels
        interesting = [p for p in a.panels if p.translated_query or p.trace or p.query_ir]
        if interesting:
            lines.append("### Detailed Traces\n")
            for p in interesting[:15]:
                lines.append(f"#### {p.title or '(untitled)'}\n")

                path_label = p.translation_path or "unknown"
                lines.append(f"**Translation path:** `{path_label}` · "
                             f"**Query language:** `{p.query_language or '—'}` · "
                             f"**Readiness:** `{p.readiness or '—'}`\n")

                lines.append(f"**Source ({p.source_panel_type}):**\n")
                for sq in p.source_queries:
                    lines.append(f"```\n{sq}\n```\n")

                if p.trace:
                    lines.append("**Pipeline trace:**\n")
                    for step in p.trace:
                        stage = step.get("stage", "")
                        rule = step.get("rule", "")
                        detail = step.get("detail", "")
                        if detail:
                            lines.append(f"- `{stage}` / `{rule}` → {detail}")
                        elif rule:
                            lines.append(f"- `{stage}` / `{rule}`")
                    lines.append("")

                if p.translated_query:
                    lines.append(f"**Translated ({p.kibana_type}):**\n")
                    lines.append(f"```\n{p.translated_query}\n```\n")

                if p.plan and p.source_type == "datadog":
                    lines.append("**Plan:**\n")
                    lines.append(f"- Backend: `{p.plan.get('backend', '—')}`")
                    lines.append(f"- Kibana type: `{p.plan.get('kibana_type', '—')}`")
                    lines.append(f"- Data source: `{p.plan.get('data_source', '—')}`")
                    if p.plan.get("reasons"):
                        lines.append(f"- Reasons: {'; '.join(p.plan['reasons'][:5])}")
                    if p.plan.get("field_issues"):
                        lines.append(f"- Field issues: {'; '.join(p.plan['field_issues'][:5])}")
                    lines.append("")

                _render_query_ir(lines, p.query_ir)
                _render_visual_ir(lines, p.visual_ir)
                _render_operational_ir(lines, p.operational_ir)

                if p.inventory:
                    lines.append("**Inventory:**\n")
                    for k, v in p.inventory.items():
                        if v and v not in (False, 0, [], {}, ""):
                            lines.append(f"- {k}: {v}")
                    lines.append("")

                if p.warnings:
                    lines.append(f"**Warnings:** {'; '.join(p.warnings[:5])}\n")
                if p.semantic_losses:
                    lines.append(f"**Semantic losses:** {'; '.join(p.semantic_losses)}\n")
                if p.notes and p.notes != p.warnings:
                    lines.append(f"**Notes:** {'; '.join(p.notes[:5])}\n")
                lines.append(f"**Verdict:** {_verdict(p)}\n")

        if a.controls:
            lines.append("### Controls / Variables\n")
            for ctrl in a.controls:
                if isinstance(ctrl, dict):
                    label = ctrl.get("label", ctrl.get("fieldName", "—"))
                    kind = ctrl.get("type", "—")
                    lines.append(f"- `{label}` (type: `{kind}`)")
                else:
                    lines.append(f"- {ctrl}")
            lines.append("")

        if a.template_variables:
            lines.append("### Template Variables\n")
            for tv in a.template_variables:
                lines.append(f"- `${tv.get('name', '?')}` → tag: `{tv.get('tag', '—')}`, "
                             f"default: `{tv.get('default', '*')}`")
            lines.append("")

        if a.feature_gap_summary:
            lines.append("### Feature Gap Summary\n")
            for k, v in a.feature_gap_summary.items():
                lines.append(f"- **{k}:** {v}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def _to_json(audits: list[DashboardAudit]) -> str:
    data = []
    for a in audits:
        d = {
            "source": a.source,
            "file_name": a.file_name,
            "dashboard_title": a.dashboard_title,
            "dashboard_id": a.dashboard_id,
            "total_panels": a.total_panels,
            "status_counts": a.status_counts,
            "controls": a.controls,
            "template_variables": a.template_variables,
            "feature_gap_summary": a.feature_gap_summary,
            "panels": [],
        }
        for p in a.panels:
            d["panels"].append({
                "title": p.title,
                "source_panel_type": p.source_panel_type,
                "kibana_type": p.kibana_type,
                "status": p.status,
                "confidence": p.confidence,
                "verdict": _verdict(p),
                "translation_path": p.translation_path,
                "query_language": p.query_language,
                "datasource_type": p.datasource_type,
                "readiness": p.readiness,
                "source_queries": p.source_queries,
                "source_queries_detail": p.source_queries_detail,
                "translated_query": p.translated_query,
                "query_ir": p.query_ir,
                "visual_ir": p.visual_ir,
                "operational_ir": p.operational_ir,
                "plan": p.plan,
                "trace": p.trace,
                "warnings": p.warnings,
                "semantic_losses": p.semantic_losses,
                "inventory": p.inventory,
                "notes": p.notes,
                "yaml_fragment": p.yaml_fragment,
            })
        data.append(d)
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_grafana(input_dir: Path) -> list[Path]:
    return sorted(input_dir.glob("*.json"))


def _discover_datadog(input_dir: Path) -> list[Path]:
    paths = []
    for f in sorted(input_dir.rglob("*.json")):
        try:
            raw = json.loads(f.read_text())
            if "widgets" in raw or "dash" in raw:
                paths.append(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return paths


# ---------------------------------------------------------------------------
# Template-based docs generator
# ---------------------------------------------------------------------------

TEMPLATE_PATH = ROOT / "docs" / "pipeline-trace.tpl.md"
DOCS_OUTPUT_PATH = ROOT / "docs" / "pipeline-trace.md"

GRAFANA_TEMPLATE_PATH = ROOT / "docs" / "sources" / "grafana-trace.tpl.md"
GRAFANA_DOCS_PATH = ROOT / "docs" / "sources" / "grafana-trace.md"

DATADOG_TEMPLATE_PATH = ROOT / "docs" / "sources" / "datadog-trace.tpl.md"
DATADOG_DOCS_PATH = ROOT / "docs" / "sources" / "datadog-trace.md"


def _section_dashboard_summary(audits: list[DashboardAudit], *, source: str = "all") -> str:
    # Rows (type=="row") are structural containers, not panels; they're tracked
    # in a separate ``Rows`` column and excluded from the panel + skipped counts.
    lines = [
        "| Source | Dashboard | Panels | Migrated | Warnings | Manual | Not Feasible | Skipped | Rows |",
        "|--------|-----------|--------|----------|----------|--------|--------------|---------|------|",
    ]
    total_panels = 0
    for a in audits:
        c = a.status_counts
        row_count = sum(1 for p in a.panels if p.source_panel_type == "row")
        audited_panels = len(a.panels) - row_count
        panel_skipped = max(c.get("skipped", 0) - row_count, 0)
        total_panels += audited_panels
        lines.append(
            f"| {a.source} | {_escape_md(a.dashboard_title or a.file_name)} "
            f"| {audited_panels} "
            f"| {c.get('migrated', c.get('ok', 0))} "
            f"| {c.get('migrated_with_warnings', c.get('warning', 0))} "
            f"| {c.get('requires_manual', 0)} "
            f"| {c.get('not_feasible', 0)} "
            f"| {panel_skipped} "
            f"| {row_count} |"
        )
    lines.append("")
    if source == "grafana":
        dirs = "`infra/grafana/dashboards/`"
    elif source == "datadog":
        dirs = "`infra/datadog/dashboards/`"
    else:
        dirs = "`infra/grafana/dashboards/` and `infra/datadog/dashboards/`"
    lines.append(f"**{len(audits)} dashboards, {total_panels} panels** audited from {dirs}.")
    return "\n".join(lines)


def _section_verdict_summary(audits: list[DashboardAudit]) -> str:
    verdicts: dict[str, int] = {}
    for a in audits:
        for p in a.panels:
            v = _verdict(p)
            verdicts[v] = verdicts.get(v, 0) + 1
    lines = [
        "## Verdict Summary",
        "",
        "| Verdict | Count | Meaning |",
        "|---------|-------|---------|",
    ]
    labels = {
        "CORRECT": "Translation is semantically accurate",
        "MINOR_ISSUE": "Translated with approximations — review recommended",
        "EXPECTED_LIMITATION": "Known unsupported feature — placeholder or skip",
        "WRONG": "Semantic error in translation",
        "ERROR": "Pipeline crash",
    }
    for v in ("CORRECT", "MINOR_ISSUE", "EXPECTED_LIMITATION", "WRONG", "ERROR"):
        if v in verdicts:
            lines.append(f"| **{v}** | {verdicts[v]} | {labels.get(v, '')} |")
    return "\n".join(lines)


def _section_warning_patterns(audits: list[DashboardAudit]) -> str:
    from collections import Counter
    warnings: Counter[str] = Counter()
    for a in audits:
        for p in a.panels:
            for w in p.warnings:
                warnings[w] += 1
    if not warnings:
        return "No warnings recorded."
    lines = [
        "## Top Warning Patterns",
        "",
        "| Count | Warning |",
        "|------:|---------|",
    ]
    for w, c in warnings.most_common(15):
        lines.append(f"| {c} | {_escape_md(w)} |")
    return "\n".join(lines)


def _section_per_dashboard_traces(audits: list[DashboardAudit]) -> str:
    lines: list[str] = []
    for a in audits:
        audited_panels = len(a.panels)
        lines.append(f"### {a.source.title()}: {a.dashboard_title or a.file_name}")
        lines.append("")
        lines.append(f"**File:** `{a.file_name}` — **Panels:** {audited_panels}")
        lines.append("")
        lines.append("| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |")
        lines.append("|-------|---------------------|--------|---------|-------------|-----------------|")
        for p in a.panels:
            src_q = _truncate(_escape_md(p.source_queries[0]) if p.source_queries else "—", 80)
            tgt_q = _truncate(_escape_md(p.translated_query or "—"), 80)
            v = _verdict(p)
            lines.append(
                f"| {_escape_md(p.title or '(untitled)')} "
                f"| `{p.source_panel_type}` → `{p.kibana_type}` "
                f"| {p.status} | **{v}** "
                f"| {src_q} "
                f"| {tgt_q} |"
            )
        lines.append("")

        interesting = [p for p in a.panels if p.translated_query or p.trace or p.query_ir]
        if interesting:
            lines.append("<details>")
            lines.append(f"<summary>Detailed traces ({len(interesting)} panels)</summary>")
            lines.append("")
            for p in interesting[:15]:
                lines.append(f"#### {p.title or '(untitled)'}")
                lines.append("")

                path_label = p.translation_path or "unknown"
                lines.append(f"**Translation path:** `{path_label}` · "
                             f"**Query language:** `{p.query_language or '—'}` · "
                             f"**Readiness:** `{p.readiness or '—'}`")
                lines.append("")

                lines.append(f"**Source ({p.source_panel_type}):**")
                lines.append("")
                for sq in p.source_queries:
                    lines.append(f"```\n{sq}\n```")
                    lines.append("")

                if p.trace:
                    lines.append("**Pipeline trace:**")
                    lines.append("")
                    for step in p.trace:
                        stage = step.get("stage", "")
                        rule = step.get("rule", "")
                        detail = step.get("detail", "")
                        if detail:
                            lines.append(f"- `{stage}` / `{rule}` → {detail}")
                        elif rule:
                            lines.append(f"- `{stage}` / `{rule}`")
                    lines.append("")

                if p.translated_query:
                    lines.append(f"**Translated ({p.kibana_type}):**")
                    lines.append("")
                    lines.append(f"```\n{p.translated_query}\n```")
                    lines.append("")

                if p.plan and p.source_type == "datadog":
                    lines.append("**Plan:**")
                    lines.append("")
                    lines.append(f"- Backend: `{p.plan.get('backend', '—')}`")
                    lines.append(f"- Kibana type: `{p.plan.get('kibana_type', '—')}`")
                    lines.append(f"- Data source: `{p.plan.get('data_source', '—')}`")
                    if p.plan.get("reasons"):
                        lines.append(f"- Reasons: {'; '.join(p.plan['reasons'][:5])}")
                    if p.plan.get("field_issues"):
                        lines.append(f"- Field issues: {'; '.join(p.plan['field_issues'][:5])}")
                    lines.append("")

                _render_query_ir(lines, p.query_ir)
                _render_visual_ir(lines, p.visual_ir)
                _render_operational_ir(lines, p.operational_ir)

                if p.inventory:
                    lines.append("**Inventory:**")
                    lines.append("")
                    for k, v in p.inventory.items():
                        if v and v not in (False, 0, [], {}, ""):
                            lines.append(f"- {k}: {v}")
                    lines.append("")

                if p.warnings:
                    lines.append(f"**Warnings:** {'; '.join(p.warnings[:5])}")
                    lines.append("")
                if p.semantic_losses:
                    lines.append(f"**Semantic losses:** {'; '.join(p.semantic_losses)}")
                    lines.append("")
                if p.notes and p.notes != p.warnings:
                    lines.append(f"**Notes:** {'; '.join(p.notes[:5])}")
                    lines.append("")
                lines.append(f"**Verdict:** {_verdict(p)}")
                lines.append("")

            lines.append("</details>")
            lines.append("")

        if a.controls:
            lines.append("<details>")
            lines.append(f"<summary>Controls / Variables ({len(a.controls)})</summary>")
            lines.append("")
            for ctrl in a.controls:
                if isinstance(ctrl, dict):
                    label = ctrl.get("label", ctrl.get("fieldName", "—"))
                    kind = ctrl.get("type", "—")
                    lines.append(f"- `{label}` (type: `{kind}`)")
                else:
                    lines.append(f"- {ctrl}")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        if a.template_variables:
            lines.append("<details>")
            lines.append(f"<summary>Template Variables ({len(a.template_variables)})</summary>")
            lines.append("")
            for tv in a.template_variables:
                lines.append(f"- `${tv.get('name', '?')}` → tag: `{tv.get('tag', '—')}`, "
                             f"default: `{tv.get('default', '*')}`")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _section_appendix_stats(audits: list[DashboardAudit]) -> str:
    totals: dict[str, int] = {}
    for a in audits:
        for k, v in a.status_counts.items():
            totals[k] = totals.get(k, 0) + v
    grand_total = sum(totals.values())
    if grand_total == 0:
        return "No panels audited."

    # Match print_report's accounting: Grafana ``type=="row"`` containers are
    # structural elements, not panels — pull them out so percentages and the
    # "renderable panels" number describe migratable content only.
    total_rows = sum(
        1 for a in audits for p in a.panels if p.source_panel_type == "row"
    )
    total_panel_skipped = max(totals.get("skipped", 0) - total_rows, 0)
    total = grand_total - total_rows

    if total_rows:
        elements_line = f"{grand_total} total ({total} panels + {total_rows} rows)"
    else:
        elements_line = f"{grand_total} total ({total} panels)"

    lines = ["From the latest trace run:", "", "```"]
    lines.append(f"Elements:            {elements_line}")
    lines.append(f"Renderable panels:   {total}")
    for key, label in [
        ("migrated", "Migrated"),
        ("migrated_with_warnings", "With warnings"),
        ("ok", "OK"),
        ("warning", "Warning"),
        ("requires_manual", "Requires manual"),
        ("not_feasible", "Not feasible"),
    ]:
        count = totals.get(key, 0)
        if count:
            pct = count / total * 100 if total > 0 else 0.0
            lines.append(f"  {label + ':':<20s} {count:>4d} ({pct:.1f}%)")
    pct = total_panel_skipped / total * 100 if total > 0 else 0.0
    lines.append(f"  {'Skipped:':<20s} {total_panel_skipped:>4d} ({pct:.1f}%)")
    lines.append("```")

    verdicts: dict[str, int] = {}
    for a in audits:
        for p in a.panels:
            v = _verdict(p)
            verdicts[v] = verdicts.get(v, 0) + 1
    lines.append("")
    lines.append("Verdict breakdown:")
    lines.append("")
    lines.append("```")
    for v in ("CORRECT", "MINOR_ISSUE", "EXPECTED_LIMITATION", "WRONG", "ERROR"):
        if v in verdicts:
            lines.append(f"  {v + ':':<24s} {verdicts[v]:>4d}")
    lines.append("```")
    return "\n".join(lines)


def _section_not_feasible_breakdown(audits: list[DashboardAudit]) -> str:
    nf_panels = []
    for a in audits:
        for p in a.panels:
            if p.status in ("not_feasible", "blocked"):
                nf_panels.append((a.dashboard_title, p))
    if not nf_panels:
        return "No not-feasible panels in this trace run."

    lines = [
        f"Every panel marked `not_feasible` in the trace run ({len(nf_panels)} total):",
        "",
        "| Panel Title | Dashboard | Source | Reason |",
        "|-------------|-----------|--------|--------|",
    ]
    for dash_title, p in nf_panels:
        reasons = "; ".join(p.warnings[:2]) if p.warnings else "—"
        lines.append(
            f"| {_escape_md(p.title)} "
            f"| {_escape_md(dash_title)} "
            f"| {p.source_type} "
            f"| {_escape_md(_truncate(reasons, 100))} |"
        )

    reason_counts: dict[str, int] = {}
    for _, p in nf_panels:
        for w in p.warnings:
            bucket = w[:60]
            reason_counts[bucket] = reason_counts.get(bucket, 0) + 1
    if reason_counts:
        lines.append("")
        lines.append("**Pattern analysis:**")
        lines.append("")
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- **{count}×** {reason}")

    return "\n".join(lines)


def _fill_template(template_text: str, audits: list[DashboardAudit], *, source: str = "all") -> str:
    """Replace ``<!-- GENERATED:NAME -->...<!-- /GENERATED:NAME -->`` blocks.

    *source* controls the footer text in the dashboard summary and is passed
    through to ``_section_dashboard_summary``.
    """
    import datetime

    generators: dict[str, Any] = {
        "DASHBOARD_SUMMARY": lambda a: _section_dashboard_summary(a, source=source),
        "VERDICT_SUMMARY": _section_verdict_summary,
        "WARNING_PATTERNS": _section_warning_patterns,
        "PER_DASHBOARD_TRACES": _section_per_dashboard_traces,
        "APPENDIX_STATS": _section_appendix_stats,
        "NOT_FEASIBLE_BREAKDOWN": _section_not_feasible_breakdown,
    }

    result = template_text
    for name, gen_fn in generators.items():
        open_tag = f"<!-- GENERATED:{name} -->"
        close_tag = f"<!-- /GENERATED:{name} -->"
        if open_tag in result and close_tag in result:
            content = gen_fn(audits)
            start = result.index(open_tag)
            end = result.index(close_tag) + len(close_tag)
            result = result[:start] + open_tag + "\n" + content + "\n" + close_tag + result[end:]

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
    last_tag = None
    for tag in ("NOT_FEASIBLE_BREAKDOWN", "APPENDIX_STATS", "WARNING_PATTERNS"):
        close = f"<!-- /GENERATED:{tag} -->"
        if close in result:
            last_tag = close
            break
    if last_tag:
        result = result.replace(
            last_tag,
            f"{last_tag}\n\n---\n\n*Last generated: {ts}*",
            1,
        )
    return result


def _run_audit(args) -> list[DashboardAudit]:
    """Run the audit on all discovered dashboards and return results."""
    audits: list[DashboardAudit] = []

    if args.files:
        for f in args.files:
            if not f.exists():
                print(f"  SKIP (missing): {f}")
                continue
            raw = json.loads(f.read_text())
            if "widgets" in raw or "dash" in raw:
                print(f"  [datadog] {f.name}")
                audits.append(_audit_datadog_dashboard(f, args.datadog_data_view))
            else:
                print(f"  [grafana] {f.name}")
                audits.append(_audit_grafana_dashboard(f, args.data_view))
    else:
        if args.source in ("grafana", "all"):
            grafana_dir = ROOT / "infra" / "grafana" / "dashboards"
            if grafana_dir.exists():
                files = _discover_grafana(grafana_dir)
                print(f"=== Grafana: {len(files)} dashboards ===")
                for f in files:
                    print(f"  {f.name}...", end=" ", flush=True)
                    audits.append(_audit_grafana_dashboard(f, args.data_view))
                    a = audits[-1]
                    print(f"{a.total_panels} panels, "
                          f"{a.status_counts.get('migrated', 0)}+{a.status_counts.get('migrated_with_warnings', 0)} migrated")

        if args.source in ("datadog", "all"):
            dd_dir = ROOT / "infra" / "datadog" / "dashboards"
            if dd_dir.exists():
                files = _discover_datadog(dd_dir)
                print(f"\n=== Datadog: {len(files)} dashboards ===")
                for f in files:
                    print(f"  {f.name}...", end=" ", flush=True)
                    audits.append(_audit_datadog_dashboard(f, args.datadog_data_view))
                    a = audits[-1]
                    n = sum(a.status_counts.values())
                    print(f"{a.total_panels} widgets, {n} translated")

    return audits


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Audit migration pipeline on real dashboards",
    )
    parser.add_argument(
        "--source", choices=["grafana", "datadog", "all"], default="all",
        help="Which source adapter to audit",
    )
    parser.add_argument(
        "--format", choices=["json", "markdown", "both"], default="both",
        dest="output_format",
        help="Output format",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "audit_output",
        help="Directory for output files",
    )
    parser.add_argument(
        "--files", nargs="*", type=Path,
        help="Specific dashboard files to audit (overrides --source discovery)",
    )
    parser.add_argument(
        "--data-view", default="metrics-prometheus-*",
        help="Grafana data view / index pattern (default: metrics-prometheus-*)",
    )
    parser.add_argument(
        "--datadog-data-view", default="metrics-otel-default",
        help="Datadog data view (default: metrics-otel-default)",
    )
    parser.add_argument(
        "--update-docs", action="store_true",
        help="Fill template-based docs: shared pipeline-trace.md plus "
             "per-source grafana-trace.md and datadog-trace.md.  "
             "Respects --source to limit which per-source doc is updated.",
    )
    args = parser.parse_args()

    audits = _run_audit(args)

    if args.update_docs:
        grafana_audits = [a for a in audits if a.source == "grafana"]
        datadog_audits = [a for a in audits if a.source == "datadog"]

        # --- shared doc (always written when --update-docs) ---
        if TEMPLATE_PATH.exists():
            filled = _fill_template(TEMPLATE_PATH.read_text(), audits, source="all")
            DOCS_OUTPUT_PATH.write_text(filled)
            print(f"\nDocs updated: {DOCS_OUTPUT_PATH}")
        else:
            print(f"WARNING: shared template not found at {TEMPLATE_PATH}")

        # --- grafana trace ---
        if args.source in ("grafana", "all") and GRAFANA_TEMPLATE_PATH.exists():
            filled = _fill_template(GRAFANA_TEMPLATE_PATH.read_text(), grafana_audits, source="grafana")
            GRAFANA_DOCS_PATH.write_text(filled)
            print(f"Docs updated: {GRAFANA_DOCS_PATH}")
        elif not GRAFANA_TEMPLATE_PATH.exists():
            print(f"WARNING: Grafana template not found at {GRAFANA_TEMPLATE_PATH}")

        # --- datadog trace ---
        if args.source in ("datadog", "all") and DATADOG_TEMPLATE_PATH.exists():
            filled = _fill_template(DATADOG_TEMPLATE_PATH.read_text(), datadog_audits, source="datadog")
            DATADOG_DOCS_PATH.write_text(filled)
            print(f"Docs updated: {DATADOG_DOCS_PATH}")
        elif not DATADOG_TEMPLATE_PATH.exists():
            print(f"WARNING: Datadog template not found at {DATADOG_TEMPLATE_PATH}")
    else:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if args.output_format in ("json", "both"):
            json_path = args.output_dir / "audit_results.json"
            json_path.write_text(_to_json(audits))
            print(f"\nJSON audit: {json_path}")
        if args.output_format in ("markdown", "both"):
            md_path = args.output_dir / "pipeline_trace.md"
            md_path.write_text(generate_pipeline_trace_md(audits))
            print(f"Pipeline trace: {md_path}")

    total_panels = sum(len(a.panels) for a in audits)
    verdicts: dict[str, int] = {}
    for a in audits:
        for p in a.panels:
            v = _verdict(p)
            verdicts[v] = verdicts.get(v, 0) + 1

    print(f"\n{'='*60}")
    print(f"Audited {len(audits)} dashboards, {total_panels} panels")
    for v in ("CORRECT", "MINOR_ISSUE", "EXPECTED_LIMITATION", "WRONG", "ERROR"):
        if v in verdicts:
            print(f"  {v}: {verdicts[v]}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
