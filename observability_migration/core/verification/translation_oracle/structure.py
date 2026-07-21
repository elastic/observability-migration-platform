# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared ES|QL structural checks used by Grafana and Datadog harnesses."""

from __future__ import annotations

import re

from observability_migration.core.verification.translation_oracle.pipeline import (
    parse_stats_assignments,
    split_pipeline_stages,
)
from observability_migration.core.verification.translation_oracle.types import (
    StructuralFinding,
    StructuralRuleId,
    StructuralSeverity,
)

_WRAPPED_OVER_TIME_ASSIGNMENT = re.compile(
    r"=\s*(AVG|SUM|MIN|MAX|COUNT)\(\s*[A-Z_]+_OVER_TIME\(",
    re.IGNORECASE,
)
_BARE_OVER_TIME_ASSIGNMENT = re.compile(
    r"=\s*[A-Z_]+_OVER_TIME\(",
    re.IGNORECASE,
)
_RATE_IRATE_INCREASE = re.compile(r"\b(RATE|IRATE|INCREASE)\(", re.IGNORECASE)
_ASSIGNMENT_LHS = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.+)$", re.DOTALL)
_EVAL_SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_EVAL_BODY = re.compile(r"^EVAL\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.+)$", re.IGNORECASE | re.DOTALL)
_TS_INNER_CASE_VALUE = re.compile(
    r"\b(?:RATE|IRATE|INCREASE|DELTA|DERIV|AVG_OVER_TIME|SUM_OVER_TIME|"
    r"MIN_OVER_TIME|MAX_OVER_TIME|COUNT_OVER_TIME|LAST_OVER_TIME|PRESENT_OVER_TIME)"
    r"\(\s*CASE\(",
    re.IGNORECASE,
)


def check_esql_structure(
    query: str,
    *,
    feasibility: str | None = None,
    require_stats_for_feasible: bool = False,
) -> list[StructuralFinding]:
    """Shared STATS/EVAL structural oracle.

    Skips native ``PROMQL(...)`` passthrough. Source adapters layer additional
    rules (Datadog ``MISSING_FROM``, empty-feasible) on top of this.
    """
    text = (query or "").strip()
    if text.upper().startswith("PROMQL"):
        return []

    findings: list[StructuralFinding] = []
    if feasibility == "feasible" and require_stats_for_feasible and not text:
        findings.append(
            StructuralFinding(
                rule_id=StructuralRuleId.EMPTY_FEASIBLE_QUERY,
                severity=StructuralSeverity.error,
                message="Feasible translation produced an empty ES|QL query",
                evidence={"query": query, "feasibility": feasibility},
            )
        )
        return findings

    defined_columns: set[str] = set()
    for stage in split_pipeline_stages(text):
        stage_upper = stage.upper()
        if stage_upper.startswith("STATS"):
            stats_assignments = parse_stats_assignments(stage)
            for assignment in stats_assignments:
                lhs_match = _ASSIGNMENT_LHS.match(assignment.strip())
                if lhs_match:
                    defined_columns.add(lhs_match.group(1))
            findings.extend(_check_stats_assignments(stats_assignments))
        elif stage_upper.startswith("EVAL"):
            eval_match = _EVAL_BODY.match(stage.strip())
            if eval_match:
                alias = eval_match.group(1)
                rhs = eval_match.group(2).strip()
                if _EVAL_SIMPLE_IDENT.fullmatch(rhs) and rhs not in defined_columns:
                    findings.append(
                        StructuralFinding(
                            rule_id=StructuralRuleId.EVAL_UNDEFINED_COLUMN,
                            severity=StructuralSeverity.error,
                            message=(
                                f"EVAL references undefined column {rhs!r}; "
                                f"defined columns: {sorted(defined_columns)!r}"
                            ),
                            evidence={
                                "alias": alias,
                                "referenced": rhs,
                                "defined_columns": sorted(defined_columns),
                            },
                        )
                    )
                defined_columns.add(alias)

    return findings


def _check_stats_assignments(assignments: list[str]) -> list[StructuralFinding]:
    if not assignments:
        return []

    findings: list[StructuralFinding] = []
    inner_case_assignments = [
        assignment for assignment in assignments if _TS_INNER_CASE_VALUE.search(assignment)
    ]
    if inner_case_assignments:
        findings.append(
            StructuralFinding(
                rule_id=StructuralRuleId.STATS_TS_CASE_VALUE_ARG,
                severity=StructuralSeverity.error,
                message=(
                    "STATS time-series function uses CASE(...) as its value argument; "
                    "wrap CASE around the call instead "
                    "(CASE(cond, IRATE(field, window), NULL))"
                ),
                evidence={"assignments": inner_case_assignments},
            )
        )

    has_wrapped = any(_WRAPPED_OVER_TIME_ASSIGNMENT.search(a) for a in assignments)
    has_bare_over_time = any(_BARE_OVER_TIME_ASSIGNMENT.search(a) for a in assignments)
    if has_wrapped and has_bare_over_time:
        wrapped = [a for a in assignments if _WRAPPED_OVER_TIME_ASSIGNMENT.search(a)]
        bare = [a for a in assignments if _BARE_OVER_TIME_ASSIGNMENT.search(a)]
        findings.append(
            StructuralFinding(
                rule_id=StructuralRuleId.STATS_BARE_WRAPPED_OVER_TIME_MIX,
                severity=StructuralSeverity.error,
                message=(
                    "STATS mixes wrapped bucket aggregates over OVER_TIME with bare "
                    "OVER_TIME measures"
                ),
                evidence={"wrapped_assignments": wrapped, "bare_assignments": bare},
            )
        )

    stats_text = ", ".join(assignments)
    has_rate_family = bool(_RATE_IRATE_INCREASE.search(stats_text))
    if has_rate_family and has_bare_over_time:
        findings.append(
            StructuralFinding(
                rule_id=StructuralRuleId.MIXED_IRATE_AVG_OVER_TIME,
                severity=StructuralSeverity.warning,
                message=(
                    "STATS mixes RATE/IRATE/INCREASE with bare OVER_TIME aggregates"
                ),
                evidence={"assignments": assignments},
            )
        )

    return findings


__all__ = ["check_esql_structure"]
