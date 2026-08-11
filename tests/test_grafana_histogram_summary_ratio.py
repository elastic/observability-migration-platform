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


def test_sum_over_colocated_ratio_is_now_translated():
    """Superseded: this shape is exactly the co-located per-element case.

    ``avail / size`` carries no ``on()``/``ignoring()`` modifier, so PromQL
    matches on ALL labels -- the operands are the same node_filesystem series,
    and every Prometheus->Elasticsearch layout stores them on one document per
    label-set. ``colocated_binary_agg_family`` therefore evaluates the division
    per document and aggregates the result, which is precisely
    ``sum(A / B)``.

    The old expectation was the conservative default from before that rule
    existed. Verified numerically on the equivalent Redis memory ratio: the
    generated query returns 1.2333526611328125, identical to the hand-written
    curated-pack query it replaced.

    Genuinely unaligned joins (those carrying vector_matching/join_labels) are
    still refused -- see test_join_with_on_modifier_stays_not_feasible.
    """
    result = _translate("sum(node_filesystem_avail_bytes / node_filesystem_size_bytes)")
    assert result.feasibility == "feasible"
    assert "node_filesystem_avail_bytes" in (result.esql_query or "")
    assert "node_filesystem_size_bytes" in (result.esql_query or "")


def test_join_with_on_modifier_stays_not_feasible():
    """An explicit vector-matching join is NOT co-located and must still refuse."""
    result = _translate("sum(node_a / on(x) group_left node_b)")
    assert result.feasibility == "not_feasible"
