# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared translation correctness oracle types (issue #301)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StructuralSeverity(str, Enum):
    error = "error"
    warning = "warning"


class StructuralRuleId(str, Enum):
    STATS_CASE_BARE_TS_MIX = "STATS_CASE_BARE_TS_MIX"
    STATS_TS_CASE_VALUE_ARG = "STATS_TS_CASE_VALUE_ARG"
    STATS_BARE_WRAPPED_OVER_TIME_MIX = "STATS_BARE_WRAPPED_OVER_TIME_MIX"
    EVAL_UNDEFINED_COLUMN = "EVAL_UNDEFINED_COLUMN"
    EMPTY_FEASIBLE_QUERY = "EMPTY_FEASIBLE_QUERY"
    MIXED_IRATE_AVG_OVER_TIME = "MIXED_IRATE_AVG_OVER_TIME"
    MISSING_FROM = "MISSING_FROM"


@dataclass(frozen=True)
class StructuralFinding:
    rule_id: StructuralRuleId
    severity: StructuralSeverity
    message: str
    evidence: dict[str, Any]


def structural_errors(findings: list[StructuralFinding]) -> list[StructuralFinding]:
    return [finding for finding in findings if finding.severity == StructuralSeverity.error]


__all__ = [
    "StructuralFinding",
    "StructuralRuleId",
    "StructuralSeverity",
    "structural_errors",
]
