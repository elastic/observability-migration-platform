# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline checks for broader Grafana surface (LogQL, native PromQL, controls/links).

Issue #301 PR3 — separate from the PromQL ES|QL structural fusion harness.
Production stays degrade-graceful; these helpers hard-fail only in tests/CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    check_esql_structure,
    structural_errors,
)


class SurfaceGateDisposition(str, Enum):
    ok = "ok"
    real_bug = "real_bug"
    expected_manual = "expected_manual"


class SurfaceGateRuleId(str, Enum):
    LOGQL_MISSING_FROM = "LOGQL_MISSING_FROM"
    LOGQL_STRUCTURAL = "LOGQL_STRUCTURAL"
    NATIVE_PROMQL_SHAPE = "NATIVE_PROMQL_SHAPE"
    NATIVE_PROMQL_INDEX = "NATIVE_PROMQL_INDEX"
    CONTROLS_SILENT_DROP = "CONTROLS_SILENT_DROP"
    LINKS_SILENT_DROP = "LINKS_SILENT_DROP"


@dataclass(frozen=True)
class SurfaceGateFinding:
    rule_id: SurfaceGateRuleId
    disposition: SurfaceGateDisposition
    message: str
    evidence: dict[str, Any]


def gate_bugs(findings: list[SurfaceGateFinding]) -> list[SurfaceGateFinding]:
    return [f for f in findings if f.disposition == SurfaceGateDisposition.real_bug]


def check_logql_emission(
    query: str,
    *,
    logs_index: str = "logs-*",
    feasibility: str | None = None,
) -> list[SurfaceGateFinding]:
    """Assert a claimed-feasible LogQL translation emits FROM + oracle-clean ES|QL."""
    findings: list[SurfaceGateFinding] = []
    text = (query or "").strip()
    if feasibility and feasibility not in {"feasible", "feasible_with_warnings", "ok", "warning"}:
        return [
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.LOGQL_MISSING_FROM,
                disposition=SurfaceGateDisposition.expected_manual,
                message=f"LogQL translation not claimed feasible ({feasibility})",
                evidence={"feasibility": feasibility},
            )
        ]
    if not text.upper().startswith("FROM"):
        findings.append(
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.LOGQL_MISSING_FROM,
                disposition=SurfaceGateDisposition.real_bug,
                message="Feasible LogQL emission missing top-level FROM stage",
                evidence={"query": text, "expected_index_prefix": logs_index},
            )
        )
        return findings
    if logs_index and logs_index not in text.split("\n", 1)[0]:
        findings.append(
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.LOGQL_MISSING_FROM,
                disposition=SurfaceGateDisposition.real_bug,
                message=f"LogQL FROM stage does not reference expected logs index {logs_index!r}",
                evidence={"query_head": text.split("\n", 1)[0]},
            )
        )
    for err in structural_errors(check_esql_structure(text)):
        findings.append(
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.LOGQL_STRUCTURAL,
                disposition=SurfaceGateDisposition.real_bug,
                message=f"LogQL ES|QL structural: {err.rule_id.value}: {err.message}",
                evidence=err.evidence,
            )
        )
    if not findings:
        findings.append(
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.LOGQL_MISSING_FROM,
                disposition=SurfaceGateDisposition.ok,
                message="LogQL emission passed offline gate",
                evidence={"query_head": text.split("\n", 1)[0]},
            )
        )
    return findings


def check_native_promql_emission(
    query: str,
    *,
    esql_index: str,
) -> list[SurfaceGateFinding]:
    """Assert native passthrough shape: PROMQL index=<esql_index> …"""
    findings: list[SurfaceGateFinding] = []
    text = (query or "").strip()
    if not text.upper().startswith("PROMQL"):
        findings.append(
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.NATIVE_PROMQL_SHAPE,
                disposition=SurfaceGateDisposition.real_bug,
                message="Expected native PROMQL(...) passthrough query",
                evidence={"query": text[:200]},
            )
        )
        return findings
    expected = f"index={esql_index}"
    if expected not in text:
        findings.append(
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.NATIVE_PROMQL_INDEX,
                disposition=SurfaceGateDisposition.real_bug,
                message=f"Native PROMQL missing {expected!r}",
                evidence={"query_head": text.split("\n", 1)[0]},
            )
        )
    # Structural oracle must skip PROMQL — if it ever returns errors, that is a harness bug.
    if structural_errors(check_esql_structure(text)):
        findings.append(
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.NATIVE_PROMQL_SHAPE,
                disposition=SurfaceGateDisposition.real_bug,
                message="Structural oracle incorrectly flagged PROMQL passthrough",
                evidence={"query_head": text.split("\n", 1)[0]},
            )
        )
    if not findings:
        findings.append(
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.NATIVE_PROMQL_SHAPE,
                disposition=SurfaceGateDisposition.ok,
                message="Native PROMQL emission passed smoke gate",
                evidence={"query_head": text.split("\n", 1)[0]},
            )
        )
    return findings


def check_dashboard_surface(
    *,
    source_variable_count: int,
    translated_control_count: int,
    source_dashboard_link_count: int,
    links_summary: dict[str, Any] | None,
) -> list[SurfaceGateFinding]:
    """Assert controls/links are not silently dropped when the source has them."""
    findings: list[SurfaceGateFinding] = []
    summary = links_summary or {}
    if source_variable_count > 0 and translated_control_count == 0:
        findings.append(
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.CONTROLS_SILENT_DROP,
                disposition=SurfaceGateDisposition.real_bug,
                message="Source templating variables present but zero controls translated",
                evidence={
                    "source_variable_count": source_variable_count,
                    "translated_control_count": translated_control_count,
                },
            )
        )
    if source_dashboard_link_count > 0:
        reported = int(summary.get("dashboard_links") or 0)
        if reported == 0:
            findings.append(
                SurfaceGateFinding(
                    rule_id=SurfaceGateRuleId.LINKS_SILENT_DROP,
                    disposition=SurfaceGateDisposition.real_bug,
                    message="Source dashboard links present but links_summary.dashboard_links is 0",
                    evidence={
                        "source_dashboard_link_count": source_dashboard_link_count,
                        "links_summary": summary,
                    },
                )
            )
    if not findings:
        findings.append(
            SurfaceGateFinding(
                rule_id=SurfaceGateRuleId.CONTROLS_SILENT_DROP,
                disposition=SurfaceGateDisposition.ok,
                message="Dashboard surface (controls/links) passed offline gate",
                evidence={
                    "source_variable_count": source_variable_count,
                    "translated_control_count": translated_control_count,
                    "source_dashboard_link_count": source_dashboard_link_count,
                    "links_summary": summary,
                },
            )
        )
    return findings


__all__ = [
    "SurfaceGateDisposition",
    "SurfaceGateFinding",
    "SurfaceGateRuleId",
    "check_dashboard_surface",
    "check_logql_emission",
    "check_native_promql_emission",
    "gate_bugs",
]
