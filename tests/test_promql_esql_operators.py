# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for ES|QL operator/function emission correctness.

These guard two runtime-only translator bugs found by executing emitted ES|QL
against a live Elasticsearch cluster (the queries compiled and linted clean but
Elasticsearch rejected them at query time):

* ``quantile(phi, …)`` must emit ES|QL ``PERCENTILE(expr, phi*100)`` - a
  two-argument call - across every aggregation shape (simple, over rate, and
  nested over an inner aggregation). A one-argument ``PERCENTILE(...)`` fails
  with "error building [percentile]: expects exactly two arguments". (issue #213)
* PromQL ``^`` (power) has no ES|QL infix operator; it must emit ``POW(base,
  exponent)``. A passed-through ``^`` fails with "token recognition error at:
  '^'".
"""

from __future__ import annotations

import re

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)

_RP = RulePackConfig()
_RES = SchemaResolver(_RP)


def _esql(expr: str, panel_type: str = "timeseries") -> str:
    result = translate_promql_to_esql(
        expr, datasource_index="metrics-*", panel_type=panel_type, rule_pack=_RP, resolver=_RES
    )
    assert result.feasibility == "feasible", f"{expr} -> {result.feasibility}: {result.warnings}"
    return result.esql_query


def _assert_percentile_pct(esql: str, pct: int) -> None:
    assert "PERCENTILE(" in esql, esql
    # The percentile fraction must be emitted as the second argument (phi*100).
    assert re.search(rf",\s*{pct}\)", esql), f"missing two-arg PERCENTILE(..., {pct}): {esql}"


class TestQuantilePercentile:
    def test_quantile_over_rate_emits_two_arg_percentile(self) -> None:
        _assert_percentile_pct(_esql("quantile(0.9, rate(http_requests_total[5m]))"), 90)

    def test_quantile_over_gauge_grouped(self) -> None:
        _assert_percentile_pct(
            _esql("quantile(0.95, node_memory_MemAvailable_bytes) by (instance)"), 95
        )

    def test_quantile_nested_over_inner_aggregation(self) -> None:
        _assert_percentile_pct(
            _esql("quantile(0.99, sum(rate(http_requests_total[5m])) by (instance))"), 99
        )

    def test_quantile_nested_summary_with_inner_group_emits_outer_stats(self) -> None:
        esql = _esql(
            "quantile(0.99, sum(node_memory_MemAvailable_bytes) by (instance))",
            panel_type="stat",
        )
        _assert_percentile_pct(esql, 99)
        assert "inner_val =" in esql, esql
        assert re.search(r"\| STATS node_memory_MemAvailable_bytes_quantile = PERCENTILE\(inner_val, 99\)", esql), esql

    def test_topk_quantile_over_rate_emits_two_arg_percentile(self) -> None:
        _assert_percentile_pct(
            _esql("topk(5, quantile(0.9, rate(http_requests_total[5m])))"),
            90,
        )

    def test_topk_quantile_over_gauge_emits_two_arg_percentile(self) -> None:
        _assert_percentile_pct(
            _esql("topk(5, quantile(0.9, node_memory_MemAvailable_bytes))"),
            90,
        )

    def test_join_left_quantile_over_rate_emits_two_arg_percentile(self) -> None:
        _assert_percentile_pct(
            _esql(
                "quantile(0.9, rate(http_requests_total[5m])) "
                "+ on(instance) group_left(foo) http_requests_total"
            ),
            90,
        )

    def test_quantile_median(self) -> None:
        _assert_percentile_pct(_esql("quantile(0.5, node_memory_MemAvailable_bytes)"), 50)


class TestPowerOperator:
    def test_gauge_power_emits_pow_not_caret(self) -> None:
        esql = _esql("node_memory_MemAvailable_bytes ^ 2")
        assert "POW(" in esql, esql
        assert "^" not in esql, f"raw caret leaked into ES|QL: {esql}"

    def test_aggregation_power(self) -> None:
        esql = _esql("sum(rate(http_requests_total[5m])) by (instance) ^ 2")
        assert "POW(" in esql, esql
        assert "^" not in esql, esql

    def test_non_power_operators_unaffected(self) -> None:
        # Sanity: a normal multiply must stay infix, not become POW.
        esql = _esql("irate(node_network_receive_bytes_total[5m]) * 8")
        assert "POW(" not in esql
        assert "* 8" in esql
