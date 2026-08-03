# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared translation correctness oracle (issue #301).

Source adapters plug thin wrappers:
- Grafana: ``adapters.source.grafana.esql_structural_oracle``
- Datadog: ``adapters.source.datadog.esql_structural_oracle`` (adds FROM / empty)
"""

from __future__ import annotations

from observability_migration.core.verification.translation_oracle.pipeline import (
    parse_stats_assignments,
    split_pipeline_stages,
    split_top_level_csv,
)
from observability_migration.core.verification.translation_oracle.structure import (
    check_esql_structure,
)
from observability_migration.core.verification.translation_oracle.types import (
    StructuralFinding,
    StructuralRuleId,
    StructuralSeverity,
    structural_errors,
)

__all__ = [
    "StructuralFinding",
    "StructuralRuleId",
    "StructuralSeverity",
    "check_esql_structure",
    "parse_stats_assignments",
    "split_pipeline_stages",
    "split_top_level_csv",
    "structural_errors",
]
