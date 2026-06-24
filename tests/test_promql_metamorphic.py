# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Metamorphic / differential (Layer 8) checks for the PromQL translator.

A *metamorphic relation* applies a semantics-preserving mutation to an input and
asserts the output changes only in expected ways. These catch ordering bugs and
hidden state that snapshot tests (one fixed input) cannot:

* **whitespace insensitivity** - reformatting the PromQL must not change the
  translation at all.
* **by-label reordering** - ``by (a, b)`` and ``by (b, a)`` must produce the
  same feasibility and the same *set* of grouping fields.
* **matcher reordering** - ``{a="1", b="2"}`` and ``{b="2", a="1"}`` must
  translate to the same feasibility and grouping fields.

These are generated across many expressions with Hypothesis so the relation is
checked broadly, not on a single hand-picked case.
"""

from __future__ import annotations

import re

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)

INDEX = "metrics-*"
_TIME_LIKE = {"time_bucket", "timestamp_bucket", "step", "@timestamp"}

_RULE_PACK = RulePackConfig()
_RESOLVER = SchemaResolver(_RULE_PACK)

_LABELS = ("job", "instance", "namespace", "pod", "method", "status", "region")
_METRICS = (
    "http_requests_total",
    "node_cpu_seconds_total",
    "kube_pod_info",
    "container_cpu_usage_seconds_total",
    "apiserver_request_total",
)
_OUTER_AGGS = ("sum", "avg", "max", "min")
_RANGES = ("1m", "5m", "1h")


def _translate(expr: str, panel_type: str = "timeseries"):
    return translate_promql_to_esql(
        expr,
        datasource_index=INDEX,
        panel_type=panel_type,
        rule_pack=_RULE_PACK,
        resolver=_RESOLVER,
    )


def _non_time_groups(result) -> set[str]:
    return {f for f in result.output_group_fields if f not in _TIME_LIKE}


# --------------------------------------------------------------------- #
# expression generation
# --------------------------------------------------------------------- #


@st.composite
def _expr_with_matchers_and_by(draw: st.DrawFn) -> str:
    metric = draw(st.sampled_from(_METRICS))
    labels = draw(st.lists(st.sampled_from(_LABELS), min_size=2, max_size=3, unique=True))
    matchers = draw(
        st.lists(
            st.tuples(st.sampled_from(_LABELS), st.sampled_from(("a", "b", "prod"))),
            min_size=2,
            max_size=3,
            unique_by=lambda pair: pair[0],
        )
    )
    quote = '"'
    matcher_str = ", ".join(k + "=" + quote + v + quote for k, v in matchers)
    selector = f"{metric}{{{matcher_str}}}"
    oagg = draw(st.sampled_from(_OUTER_AGGS))
    rng = draw(st.sampled_from(_RANGES))
    use_rate = draw(st.booleans())
    inner = f"rate({selector}[{rng}])" if use_rate else selector
    return f"{oagg}({inner}) by ({', '.join(labels)})"


# --------------------------------------------------------------------- #
# mutations
# --------------------------------------------------------------------- #


def _reverse_csv_in(pattern: re.Pattern[str], text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        body = m.group(1)
        parts = [p.strip() for p in body.split(",")]
        return m.group(0).replace(body, ", ".join(reversed(parts)))

    return pattern.sub(repl, text)


_BY_RE = re.compile(r"by\s*\(([^)]*)\)")
_BRACE_RE = re.compile(r"\{([^}]*)\}")


def _mutate_whitespace(expr: str) -> str:
    return expr.replace(" by (", "  by  ( ").replace(",", " , ").replace("(", "( ")


def _mutate_reorder_by(expr: str) -> str:
    return _reverse_csv_in(_BY_RE, expr)


def _mutate_reorder_matchers(expr: str) -> str:
    return _reverse_csv_in(_BRACE_RE, expr)


# --------------------------------------------------------------------- #
# metamorphic relations
# --------------------------------------------------------------------- #


class TestWhitespaceInsensitivity:
    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(expr=_expr_with_matchers_and_by())
    def test_whitespace_does_not_change_translation(self, expr: str) -> None:
        base = _translate(expr)
        mutated = _translate(_mutate_whitespace(expr))
        assert base.feasibility == mutated.feasibility, expr
        if base.feasibility == "feasible":
            assert base.esql_query == mutated.esql_query, expr


class TestByLabelReorderInvariance:
    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(expr=_expr_with_matchers_and_by())
    def test_by_label_order_does_not_change_grouping(self, expr: str) -> None:
        base = _translate(expr)
        mutated = _translate(_mutate_reorder_by(expr))
        assert base.feasibility == mutated.feasibility, expr
        if base.feasibility == "feasible":
            assert _non_time_groups(base) == _non_time_groups(mutated), expr


class TestMatcherReorderInvariance:
    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(expr=_expr_with_matchers_and_by())
    def test_matcher_order_does_not_change_grouping(self, expr: str) -> None:
        base = _translate(expr)
        mutated = _translate(_mutate_reorder_matchers(expr))
        assert base.feasibility == mutated.feasibility, expr
        if base.feasibility == "feasible":
            assert _non_time_groups(base) == _non_time_groups(mutated), expr
