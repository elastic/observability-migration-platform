# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    StructuralRuleId,
    check_esql_structure,
    structural_errors,
)


def test_case_bare_irate_mix_is_error():
    q = (
        "TS metrics-*\n"
        '| STATS a = SUM(IRATE(CASE((mode == "user"), m, NULL), 1m)), '
        "b = SUM(IRATE(other, 1m)) BY time_bucket = TBUCKET(5 minute)\n"
    )
    errs = structural_errors(check_esql_structure(q))
    assert any(f.rule_id == StructuralRuleId.STATS_CASE_BARE_TS_MIX for f in errs)


def test_case_true_wrap_is_clean():
    q = (
        "TS metrics-*\n"
        '| STATS a = SUM(IRATE(CASE((mode == "user"), m, NULL), 1m)), '
        "b = SUM(IRATE(CASE(true, other, NULL), 1m)) BY time_bucket = TBUCKET(5 minute)\n"
    )
    assert structural_errors(check_esql_structure(q)) == []


def test_bare_and_wrapped_over_time_mix_is_error():
    q = (
        "TS metrics-*\n"
        "| STATS a = AVG(AVG_OVER_TIME(x, 5m)), b = AVG_OVER_TIME(y, 5m) "
        "BY time_bucket = TBUCKET(5 minute), instance\n"
    )
    errs = structural_errors(check_esql_structure(q))
    assert any(f.rule_id == StructuralRuleId.STATS_BARE_WRAPPED_OVER_TIME_MIX for f in errs)


def test_eval_undefined_column_is_error():
    q = (
        "TS metrics-*\n"
        "| STATS freq_B = MAX(LAST_OVER_TIME(freq)) BY time_bucket = TBUCKET(5 minute)\n"
        "| EVAL CPU = freq\n"
    )
    errs = structural_errors(check_esql_structure(q))
    assert any(f.rule_id == StructuralRuleId.EVAL_UNDEFINED_COLUMN for f in errs)


def test_eval_renamed_alias_is_clean():
    q = (
        "TS metrics-*\n"
        "| STATS freq_B = MAX(LAST_OVER_TIME(freq)) BY time_bucket = TBUCKET(5 minute)\n"
        "| EVAL CPU = freq_B\n"
    )
    assert structural_errors(check_esql_structure(q)) == []


def test_promql_passthrough_skipped():
    assert check_esql_structure("PROMQL index=metrics-* value=(up)") == []


def test_empty_feasible_query_is_error():
    errs = structural_errors(
        check_esql_structure("", feasibility="feasible", require_stats_for_feasible=True)
    )
    assert any(f.rule_id == StructuralRuleId.EMPTY_FEASIBLE_QUERY for f in errs)
