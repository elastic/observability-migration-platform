# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Property-based (Layer 7) checks for the PromQL -> ES|QL translator.

These generalize hand-picked snapshot cases into invariants that must hold for
*any* generated PromQL the translator is fed:

* **P1 no-crash / graceful** - structured PromQL never raises; the translator
  always returns a result with a known feasibility and a string query.
* **P2 label conservation** - for a feasible ``agg(metric) by (l1..ln)`` the
  translator either carries every grouping label into the output group fields
  or discloses the loss with a warning (the issue #189 silent-merge class,
  fuzzed across many label combinations).
* **P3 determinism** - translating the same expression twice yields byte-for-byte
  identical output (no dict/set ordering leaks).
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)

INDEX = "metrics-*"
_TIME_LIKE = {"time_bucket", "timestamp_bucket", "step", "@timestamp"}

# Shared, stateless across examples (cheaper than rebuilding per call).
_RULE_PACK = RulePackConfig()
_RESOLVER = SchemaResolver(_RULE_PACK)

# Realistic, "boring" labels that don't trigger special-case handling
# (avoid ``le`` / ``__name__`` / ``quantile`` which legitimately reshape output).
_SAFE_LABELS = (
    "job",
    "instance",
    "namespace",
    "pod",
    "container",
    "node",
    "method",
    "status",
    "region",
    "zone",
    "service",
)

_METRICS = (
    "http_requests_total",
    "node_cpu_seconds_total",
    "node_memory_MemAvailable_bytes",
    "kube_pod_info",
    "up",
    "container_cpu_usage_seconds_total",
    "apiserver_request_total",
    "process_resident_memory_bytes",
)

_RANGE_AGGS = ("rate", "irate", "increase", "avg_over_time", "max_over_time")
_OUTER_AGGS = ("sum", "avg", "max", "min")
_RANGES = ("1m", "5m", "15m", "1h")

_metric = st.sampled_from(_METRICS)
_label = st.sampled_from(_SAFE_LABELS)
_range = st.sampled_from(_RANGES)
_distinct_labels = st.lists(_label, min_size=1, max_size=3, unique=True)


def _selector(draw: st.DrawFn, metric: str) -> str:
    matchers = draw(
        st.lists(
            st.tuples(_label, st.sampled_from(("a", "b", "prod", "web"))),
            max_size=2,
            unique_by=lambda pair: pair[0],
        )
    )
    if not matchers:
        return metric
    inner = ",".join(f'{k}="{v}"' for k, v in matchers)
    return f"{metric}{{{inner}}}"


@st.composite
def _promql_expr(draw: st.DrawFn) -> str:
    """Generate a bounded, structurally-valid-ish PromQL expression."""
    metric = draw(_metric)
    selector = _selector(draw, metric)
    form = draw(st.integers(min_value=0, max_value=4))
    labels = draw(_distinct_labels)
    by_clause = f" by ({', '.join(labels)})"
    rng = draw(_range)
    ragg = draw(st.sampled_from(_RANGE_AGGS))
    oagg = draw(st.sampled_from(_OUTER_AGGS))

    if form == 0:
        return selector
    if form == 1:
        return f"{oagg}({selector}){by_clause}"
    if form == 2:
        return f"{ragg}({selector}[{rng}])"
    if form == 3:
        return f"{oagg}({ragg}({selector}[{rng}])){by_clause}"
    scalar = draw(st.integers(min_value=1, max_value=1000))
    return f"{oagg}({ragg}({selector}[{rng}])){by_clause} * {scalar}"


@st.composite
def _simple_agg_with_labels(draw: st.DrawFn) -> tuple[str, list[str]]:
    """``sum(metric) by (l1..ln)`` plus the label list, for conservation."""
    metric = draw(_metric)
    oagg = draw(st.sampled_from(_OUTER_AGGS))
    labels = draw(_distinct_labels)
    expr = f"{oagg}({metric}) by ({', '.join(labels)})"
    return expr, labels


def _translate(expr: str, panel_type: str = "timeseries"):
    return translate_promql_to_esql(
        expr,
        datasource_index=INDEX,
        panel_type=panel_type,
        rule_pack=_RULE_PACK,
        resolver=_RESOLVER,
    )


_KNOWN_FEASIBILITY = {"feasible", "not_feasible", "partial", "manual"}
_LOSS_HINT = ("merg", "collaps", "drop", "approx", "not feasible", "manual")


class TestNoCrash:
    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(expr=_promql_expr())
    def test_translation_never_raises(self, expr: str) -> None:
        result = _translate(expr)
        assert isinstance(result.esql_query, str)
        assert result.feasibility in _KNOWN_FEASIBILITY

    @settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        expr=_promql_expr(),
        panel_type=st.sampled_from(("timeseries", "stat", "gauge", "bargauge", "table")),
    )
    def test_translation_never_raises_across_panel_types(self, expr: str, panel_type: str) -> None:
        result = _translate(expr, panel_type=panel_type)
        assert isinstance(result.esql_query, str)
        assert result.feasibility in _KNOWN_FEASIBILITY


class TestDeterminism:
    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(expr=_promql_expr())
    def test_translation_is_deterministic(self, expr: str) -> None:
        first = _translate(expr)
        second = _translate(expr)
        assert first.esql_query == second.esql_query
        assert first.feasibility == second.feasibility
        assert sorted(first.warnings) == sorted(second.warnings)
        assert list(first.output_group_fields) == list(second.output_group_fields)


class TestLabelConservation:
    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(case=_simple_agg_with_labels())
    def test_grouping_labels_preserved_or_disclosed(self, case: tuple[str, list[str]]) -> None:
        expr, labels = case
        result = _translate(expr)
        if result.feasibility != "feasible":
            return
        non_time = [f for f in result.output_group_fields if f not in _TIME_LIKE]
        if len(non_time) >= len(labels):
            return
        # Fewer target group fields than source labels is only acceptable if the
        # translator disclosed the reduction (no silent label loss).
        disclosed = any(
            hint in w.lower() for w in result.warnings for hint in _LOSS_HINT
        )
        assert disclosed, (
            f"silent label loss for {expr!r}: source labels={labels} "
            f"-> group_fields={result.output_group_fields} with no warning"
        )
