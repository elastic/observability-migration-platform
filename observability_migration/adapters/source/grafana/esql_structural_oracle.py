# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline ES|QL structural oracle for fused STATS/EVAL invariants."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from observability_migration.adapters.source.grafana.promql import (
    _BARE_TS_VALUE_ARG,
    _split_top_level_csv,
)

# Wrapped OVER_TIME: outer bucket agg wraps a window aggregate.
_WRAPPED_OVER_TIME_ASSIGNMENT = re.compile(
    r"=\s*(AVG|SUM|MIN|MAX|COUNT)\(\s*[A-Z_]+_OVER_TIME\(",
    re.IGNORECASE,
)
# Bare OVER_TIME: window aggregate used directly as the STATS measure RHS.
_BARE_OVER_TIME_ASSIGNMENT = re.compile(
    r"=\s*[A-Z_]+_OVER_TIME\(",
    re.IGNORECASE,
)
_RATE_IRATE_INCREASE = re.compile(r"\b(RATE|IRATE|INCREASE)\(", re.IGNORECASE)
_ASSIGNMENT_LHS = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.+)$", re.DOTALL)
_EVAL_SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_STATS_BODY = re.compile(r"^STATS\s+(.+?)\s+BY\s+", re.IGNORECASE | re.DOTALL)
_EVAL_BODY = re.compile(r"^EVAL\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.+)$", re.IGNORECASE | re.DOTALL)


class StructuralSeverity(str, Enum):
    error = "error"
    warning = "warning"


class StructuralRuleId(str, Enum):
    STATS_CASE_BARE_TS_MIX = "STATS_CASE_BARE_TS_MIX"
    STATS_BARE_WRAPPED_OVER_TIME_MIX = "STATS_BARE_WRAPPED_OVER_TIME_MIX"
    EVAL_UNDEFINED_COLUMN = "EVAL_UNDEFINED_COLUMN"
    EMPTY_FEASIBLE_QUERY = "EMPTY_FEASIBLE_QUERY"
    MIXED_IRATE_AVG_OVER_TIME = "MIXED_IRATE_AVG_OVER_TIME"


@dataclass(frozen=True)
class StructuralFinding:
    rule_id: StructuralRuleId
    severity: StructuralSeverity
    message: str
    evidence: dict


def structural_errors(findings: list[StructuralFinding]) -> list[StructuralFinding]:
    return [finding for finding in findings if finding.severity == StructuralSeverity.error]


def check_esql_structure(
    query: str,
    *,
    feasibility: str | None = None,
    require_stats_for_feasible: bool = False,
) -> list[StructuralFinding]:
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

    stages = _split_pipeline_stages(text)
    defined_columns: set[str] = set()
    stats_assignments: list[str] = []

    for stage in stages:
        stage_upper = stage.upper()
        if stage_upper.startswith("STATS"):
            stats_assignments = _parse_stats_assignments(stage)
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


def _split_pipeline_stages(query: str) -> list[str]:
    text = query.strip()
    if not text:
        return []
    parts = re.split(r"\n\s*\|\s*", text)
    stages: list[str] = []
    first = parts[0].strip()
    if first:
        stages.append(first)
    stages.extend(part.strip() for part in parts[1:] if part.strip())
    return stages


def _parse_stats_assignments(stage: str) -> list[str]:
    match = _STATS_BODY.match(stage.strip())
    if not match:
        return []
    return _split_top_level_csv(match.group(1))


def _check_stats_assignments(assignments: list[str]) -> list[StructuralFinding]:
    if not assignments:
        return []

    findings: list[StructuralFinding] = []
    has_case = any("CASE(" in assignment for assignment in assignments)
    bare_ts_assignments = [
        assignment for assignment in assignments if _BARE_TS_VALUE_ARG.search(assignment)
    ]
    if has_case and bare_ts_assignments:
        findings.append(
            StructuralFinding(
                rule_id=StructuralRuleId.STATS_CASE_BARE_TS_MIX,
                severity=StructuralSeverity.error,
                message=(
                    "STATS mixes CASE-wrapped and bare time-series value arguments; "
                    "wrap bare metrics as CASE(true, field, NULL)"
                ),
                evidence={"bare_assignments": bare_ts_assignments},
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
