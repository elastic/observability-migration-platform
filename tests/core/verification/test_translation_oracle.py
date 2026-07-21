# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared translation_oracle package smoke + adapter wiring (issue #301)."""

from __future__ import annotations

from pathlib import Path

from observability_migration.adapters.source.datadog import esql_structural_oracle as dd_oracle
from observability_migration.adapters.source.grafana import esql_structural_oracle as gf_oracle
from observability_migration.core.verification import translation_oracle as shared


def test_shared_package_exports_canonical_api():
    assert callable(shared.check_esql_structure)
    assert callable(shared.structural_errors)
    assert callable(shared.split_pipeline_stages)


def test_grafana_adapter_reexports_shared_types():
    assert gf_oracle.StructuralRuleId is shared.StructuralRuleId
    assert gf_oracle.check_esql_structure is shared.check_esql_structure
    assert gf_oracle.structural_errors is shared.structural_errors


def test_datadog_adapter_uses_shared_types_without_importing_grafana_module():
    assert dd_oracle.StructuralRuleId is shared.StructuralRuleId
    source = Path(dd_oracle.__file__).read_text(encoding="utf-8")
    assert "adapters.source.grafana" not in source
    assert "core.verification.translation_oracle" in source


def test_shared_oracle_flags_inner_case_value_arg():
    q = (
        "TS metrics-*\n"
        '| STATS a = SUM(IRATE(CASE((mode == "user"), m, NULL), 1m)) '
        "BY time_bucket = TBUCKET(5 minute)\n"
    )
    errs = shared.structural_errors(shared.check_esql_structure(q))
    assert any(e.rule_id == shared.StructuralRuleId.STATS_TS_CASE_VALUE_ARG for e in errs)


def test_datadog_missing_from_still_layers_on_shared_checks():
    q = "| STATS a = AVG(system.cpu.user) BY host\n"
    errs = shared.structural_errors(
        dd_oracle.check_datadog_esql_structure(q, status="ok", backend="esql")
    )
    assert any(e.rule_id == shared.StructuralRuleId.MISSING_FROM for e in errs)
