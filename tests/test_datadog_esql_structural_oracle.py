# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from observability_migration.adapters.source.datadog.esql_structural_oracle import (
    check_datadog_esql_structure,
)
from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    StructuralRuleId,
    structural_errors,
)


def test_skips_non_esql_backends():
    assert check_datadog_esql_structure("", status="ok", backend="markdown") == []
    assert check_datadog_esql_structure("", status="ok", backend="lens") == []


def test_empty_ok_status_is_error():
    errs = structural_errors(
        check_datadog_esql_structure("", status="ok", backend="esql")
    )
    assert any(f.rule_id == StructuralRuleId.EMPTY_FEASIBLE_QUERY for f in errs)


def test_missing_from_is_error():
    q = "| STATS value = AVG(system.cpu.user) BY host"
    errs = structural_errors(
        check_datadog_esql_structure(q, status="ok", backend="esql")
    )
    assert any(f.rule_id == StructuralRuleId.MISSING_FROM for f in errs)


def test_clean_from_query_passes():
    q = (
        "FROM metrics-*\n"
        "| WHERE system.cpu.user IS NOT NULL\n"
        "| STATS value = AVG(system.cpu.user) BY host\n"
    )
    assert structural_errors(
        check_datadog_esql_structure(q, status="ok", backend="esql")
    ) == []


def test_shared_eval_undefined_still_errors():
    q = (
        "FROM metrics-*\n"
        "| STATS freq_B = AVG(freq) BY host\n"
        "| EVAL CPU = freq\n"
    )
    errs = structural_errors(
        check_datadog_esql_structure(q, status="ok", backend="esql")
    )
    assert any(f.rule_id == StructuralRuleId.EVAL_UNDEFINED_COLUMN for f in errs)
