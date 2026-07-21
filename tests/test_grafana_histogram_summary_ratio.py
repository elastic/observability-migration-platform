# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Histogram summary-pair ratio approximation: sum(m_sum)/sum(m_count)."""

from __future__ import annotations

from observability_migration.adapters.source.grafana import rules, schema, translate
from observability_migration.adapters.source.grafana.runtime_features import (
    ESQL_NAMED_PARAM_BINDING,
    set_runtime_feature,
)


def _translate(expr: str):
    rp = rules.RulePackConfig()
    set_runtime_feature(rp, ESQL_NAMED_PARAM_BINDING, supported=True, source="test")
    return translate.translate_promql_to_esql(
        expr,
        esql_index="metrics-*",
        panel_type="graph",
        rule_pack=rp,
        resolver=schema.SchemaResolver(rp),
    )


def test_sum_over_increase_sum_div_count_approximates_as_ratio_of_sums():
    expr = (
        'sum(increase(prometheus_tsdb_compaction_duration_sum{instance="$instance"}[30m]) '
        '/ increase(prometheus_tsdb_compaction_duration_count{instance="$instance"}[30m])) '
        "by (instance)"
    )
    result = _translate(expr)
    assert result.feasibility == "feasible", result.warnings
    q = result.esql_query or ""
    assert "prometheus_tsdb_compaction_duration_sum" in q
    assert "prometheus_tsdb_compaction_duration_count" in q
    assert "/" in q or "EVAL" in q
    assert any("Approximated sum(" in w and "ratio of aggregates" in w for w in result.warnings), (
        result.warnings
    )


def test_sum_over_unrelated_ratio_stays_not_feasible():
    result = _translate("sum(node_filesystem_avail_bytes / node_filesystem_size_bytes)")
    assert result.feasibility == "not_feasible"
