# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Datadog adapter for the shared translation correctness oracle."""

from __future__ import annotations

from observability_migration.core.verification.translation_oracle import (
    StructuralFinding,
    StructuralRuleId,
    StructuralSeverity,
    check_esql_structure,
    split_pipeline_stages,
    structural_errors,
)

ESQL_EMITTING_BACKENDS = frozenset({"esql", "esql_with_kql"})


def check_datadog_esql_structure(
    query: str,
    *,
    status: str | None = None,
    backend: str | None = None,
) -> list[StructuralFinding]:
    if backend is not None and backend not in ESQL_EMITTING_BACKENDS:
        return []

    text = (query or "").strip()
    findings: list[StructuralFinding] = []

    if status in {"ok", "warning"} and not text:
        findings.append(
            StructuralFinding(
                rule_id=StructuralRuleId.EMPTY_FEASIBLE_QUERY,
                severity=StructuralSeverity.error,
                message="Feasible Datadog translation produced an empty ES|QL query",
                evidence={"status": status, "backend": backend},
            )
        )
        return findings

    if text:
        findings.extend(check_esql_structure(text))
        has_from = any(stage.upper().startswith("FROM") for stage in split_pipeline_stages(text))
        if not has_from:
            findings.append(
                StructuralFinding(
                    rule_id=StructuralRuleId.MISSING_FROM,
                    severity=StructuralSeverity.error,
                    message="Datadog ES|QL query is missing a FROM stage",
                    evidence={"query": query},
                )
            )

    return findings


__all__ = [
    "ESQL_EMITTING_BACKENDS",
    "StructuralFinding",
    "StructuralRuleId",
    "StructuralSeverity",
    "check_datadog_esql_structure",
    "structural_errors",
]
