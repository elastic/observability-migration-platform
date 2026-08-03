# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana adapter for the shared translation correctness oracle.

Canonical implementation lives in
``observability_migration.core.verification.translation_oracle``. This module
re-exports the shared API so existing Grafana harness imports keep working.
"""

from __future__ import annotations

from observability_migration.core.verification.translation_oracle import (
    StructuralFinding,
    StructuralRuleId,
    StructuralSeverity,
    check_esql_structure,
    structural_errors,
)

__all__ = [
    "StructuralFinding",
    "StructuralRuleId",
    "StructuralSeverity",
    "check_esql_structure",
    "structural_errors",
]
