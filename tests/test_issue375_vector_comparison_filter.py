# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for issue #375 — PromQL vector-to-vector comparison without ``bool``.

A PromQL comparison with no ``bool`` modifier is a *filter*, not arithmetic:
the elements for which it holds are returned with the vector operand's own
value, and every element for which it does not hold is dropped. Before this
fix ``A == B`` between two vectors was rendered as ES|QL ``EVAL computed_value
= (a == b)``, which emits ``true``/``false`` for *every* series — losing both
the filter and the value.

The reproduction is grafana.com dashboard 13332 ("kube-state-metrics-v2"),
panels ``current==max`` and ``current==min``. With three HPAs seeded and only
``order-service`` sitting at its max, Grafana renders one series with value 5
while the migrated panel rendered three series with ``False``/``False``/``True``.

The scalar form (``sum(phase == 1)``) was already correct — it is turned into a
``| WHERE`` at parse time — and so was the explicit ``bool`` form, which asks
for a 0/1 indicator. Both are covered here as regression guards.

Also covered: the secondary report on the same panels, where a literal
``hpa=~".*"`` matcher was reported as ``Dropped variable-driven label filters
during migration``. A match-all regex is not a Grafana variable and filters
nothing, so it must not raise a semantic-loss warning; genuine ``$var``
matchers still must.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)

INDEX = "metrics-*"

COMPARISON_OPERATORS = ("==", "!=", ">", "<", ">=", "<=")

# Issue #375 reproduction: dashboard 13332, panel ``current==max``. The legend
# hint reproduces the panel's ``BY ..., labels.hpa`` grouping so the emitted
# query is the one quoted in the issue.
HPA_MAX_EXPR = (
    'kube_hpa_status_current_replicas{hpa=~".*"} '
    '== kube_hpa_spec_max_replicas{hpa=~".*"}'
)
HPA_HINTS = {
    "preferred_group_labels": ["hpa"],
    "preferred_group_labels_origin": "legend",
}

_DROPPED_VARS = "Dropped variable-driven label filters during migration"


def _translate(expr: str, panel_type: str = "timeseries", hints: dict | None = None):
    rule_pack = RulePackConfig()
    return translate_promql_to_esql(
        expr,
        datasource_index=INDEX,
        esql_index=INDEX,
        panel_type=panel_type,
        rule_pack=rule_pack,
        resolver=SchemaResolver(rule_pack),
        translation_hints=hints,
    )


def _eval_line(query: str) -> str:
    for line in (query or "").splitlines():
        if line.strip().startswith("| EVAL computed_value ="):
            return line.strip()
    return ""


class TestVectorComparisonKeepsFilterSemantics(unittest.TestCase):
    """``A <op> B`` between two vectors filters instead of emitting a boolean."""

    def test_reproduction_keeps_left_value_and_drops_non_matching_series(self):
        result = _translate(HPA_MAX_EXPR, hints=HPA_HINTS)

        self.assertEqual(result.feasibility, "feasible")
        query = result.esql_query
        # The panel's own grouping must survive, otherwise the per-HPA filter
        # would compare two cross-series averages.
        self.assertIn("hpa", result.output_group_fields)

        eval_line = _eval_line(query)
        lhs = "kube_hpa_status_current_replicas_hpa"
        rhs = "kube_hpa_spec_max_replicas_hpa"
        self.assertEqual(
            eval_line,
            f"| EVAL computed_value = CASE({lhs} == {rhs}, {lhs}, NULL)",
        )
        # Non-matching rows are dropped, matching PromQL's filter semantics.
        self.assertIn("| WHERE computed_value IS NOT NULL", query)

    def test_no_operator_emits_a_bare_boolean_expression(self):
        for op in COMPARISON_OPERATORS:
            with self.subTest(op=op):
                result = _translate(f"a_metric {op} b_metric")

                eval_line = _eval_line(result.esql_query)
                self.assertEqual(
                    eval_line,
                    f"| EVAL computed_value = CASE(a_metric {op} b_metric, a_metric, NULL)",
                )
                self.assertNotIn(
                    f"| EVAL computed_value = (a_metric {op} b_metric)",
                    result.esql_query,
                )

    def test_aggregated_operands_keep_the_left_aggregate_value(self):
        result = _translate("sum(a_metric) by (x) == sum(b_metric) by (x)")

        self.assertEqual(
            _eval_line(result.esql_query),
            "| EVAL computed_value = CASE(a_metric_sum == b_metric_sum, "
            "a_metric_sum, NULL)",
        )

    def test_scalar_on_the_left_keeps_the_vector_operand_value(self):
        """``0.5 < node_load1`` filters the vector and returns *its* value.

        The scalar side is never the result: PromQL retains the vector element
        with its original value whichever side the scalar sits on.
        """
        result = _translate("0.5 < node_load1")

        self.assertEqual(
            _eval_line(result.esql_query),
            "| EVAL computed_value = CASE(0.5 < node_load1, node_load1, NULL)",
        )

    def test_filtered_comparison_nested_in_arithmetic_propagates_the_drop(self):
        """``c / (a > b)`` divides by the filtered value, not by a boolean."""
        result = _translate("c_metric / (a_metric > b_metric)")

        self.assertEqual(
            _eval_line(result.esql_query),
            "| EVAL computed_value = (c_metric / "
            "CASE(a_metric > b_metric, a_metric, NULL))",
        )
        self.assertIn("| WHERE computed_value IS NOT NULL", result.esql_query)

    def test_range_function_operands_are_filtered_too(self):
        result = _translate("rate(a_total[5m]) > rate(b_total[5m])")

        self.assertEqual(
            _eval_line(result.esql_query),
            "| EVAL computed_value = CASE(a_total_rate > b_total_rate, "
            "a_total_rate, NULL)",
        )

    def test_filtered_comparison_nested_in_a_bool_comparison_stays_dropped(self):
        """``(a > b) > bool 0`` must not resurrect the elements ``a > b`` dropped.

        The inner filter leaves NULL for a rejected element, and a NULL
        condition falls through to ``CASE``'s default — reporting a real ``0``
        for a series PromQL removed. Guarding on the inner operand keeps it
        NULL so the row is dropped, while a genuine ``0`` (an element that
        survived the filter but fails the ``bool`` test) is still reported.
        """
        result = _translate("(a_metric > b_metric) > bool 0")

        inner = "CASE(a_metric > b_metric, a_metric, NULL)"
        self.assertEqual(
            _eval_line(result.esql_query),
            f"| EVAL computed_value = CASE({inner} IS NOT NULL, "
            f"CASE({inner} > 0, 1, 0), NULL)",
        )
        self.assertIn("| WHERE computed_value IS NOT NULL", result.esql_query)

    def test_bool_comparison_between_two_filtered_operands_guards_on_both(self):
        """``(a > b) > bool (c > d)`` exists only where *both* filters survive."""
        result = _translate(
            "(a_metric > b_metric) > bool (c_metric > d_metric)"
        )

        left = "CASE(a_metric > b_metric, a_metric, NULL)"
        right = "CASE(c_metric > d_metric, c_metric, NULL)"
        self.assertEqual(
            _eval_line(result.esql_query),
            f"| EVAL computed_value = CASE({left} IS NOT NULL AND "
            f"{right} IS NOT NULL, CASE({left} > {right}, 1, 0), NULL)",
        )
        self.assertIn("| WHERE computed_value IS NOT NULL", result.esql_query)

    def test_filtered_comparison_nested_in_a_bool_divisor_propagates_the_drop(self):
        """The bool-divisor rewrite must carry the *right* operand's filter too.

        ``c / ((a > b) > bool 0)`` already evaluates to NULL for a rejected
        element, but without propagating the flag the row was charted as a gap
        instead of being dropped.
        """
        result = _translate("c_metric / ((a_metric > b_metric) > bool 0)")

        self.assertEqual(
            _eval_line(result.esql_query),
            "| EVAL computed_value = (c_metric / "
            "CASE(CASE(a_metric > b_metric, a_metric, NULL) > 0, 1, NULL))",
        )
        self.assertIn("| WHERE computed_value IS NOT NULL", result.esql_query)

    def test_scalar_panel_null_skip_is_not_emitted_twice(self):
        """The rate-boundary collapse skips nulls with the same clause.

        A scalar panel over a rate comparison drops null buckets before taking
        the penultimate one, which is the clause the filter already appended —
        emitting it twice would be dead weight in every uploaded query.
        """
        result = _translate(
            "rate(a_total[5m]) > rate(b_total[5m])",
            panel_type="stat",
            hints={"summary_mode": True, "reduce_calc": "lastNotNull"},
        )

        lines = result.esql_query.splitlines()
        self.assertEqual(
            lines.count("| WHERE computed_value IS NOT NULL"), 1, result.esql_query
        )


class TestComparisonFormsThatMustNotChange(unittest.TestCase):
    """Shapes that were already correct keep their existing translation."""

    def test_bool_modifier_still_emits_a_numeric_indicator(self):
        for op in COMPARISON_OPERATORS:
            with self.subTest(op=op):
                result = _translate(f"a_metric {op} bool b_metric")

                self.assertEqual(
                    _eval_line(result.esql_query),
                    f"| EVAL computed_value = CASE(a_metric {op} b_metric, 1, 0)",
                )
                self.assertNotIn(
                    "| WHERE computed_value IS NOT NULL", result.esql_query
                )

    def test_bool_indicator_used_as_divisor_keeps_its_null_guard(self):
        result = _translate("a_metric / (b_metric > bool 0)")

        self.assertEqual(
            _eval_line(result.esql_query),
            "| EVAL computed_value = (a_metric / CASE(b_metric > 0, 1, NULL))",
        )

    def test_scalar_threshold_comparison_stays_a_where_filter(self):
        result = _translate("node_load1 > 0.5")

        self.assertIn("| WHERE node_load1 > 0.5", result.esql_query)
        self.assertNotIn("CASE(", result.esql_query)

    def test_scalar_comparison_inside_an_aggregation_stays_a_where_filter(self):
        result = _translate(
            'sum(kube_persistentvolumeclaim_status_phase{phase="Bound"}==1)'
        )

        self.assertIn(
            "| WHERE kube_persistentvolumeclaim_status_phase == 1",
            result.esql_query,
        )
        self.assertNotIn("CASE(", result.esql_query)

    def test_aggregation_over_a_two_series_comparison_stays_not_feasible(self):
        """Issue #377: the outer aggregation still has no honest rendering.

        The inner filter is per matching series pair, matched on the operands'
        full label set, and only then reduced — a stage ES|QL has no equivalent
        for, so making the bare form faithful must not quietly make the wrapped
        form translatable.
        """
        for op in COMPARISON_OPERATORS:
            with self.subTest(op=op):
                result = _translate(f"count(a_metric {op} b_metric)")

                self.assertEqual(result.feasibility, "not_feasible")
                self.assertFalse(result.esql_query)


class TestMatchAllMatcherIsNotAVariableDrop(unittest.TestCase):
    """Secondary report: ``hpa=~".*"`` is not a dropped dashboard variable."""

    def test_literal_match_all_regex_does_not_warn(self):
        result = _translate('a_metric{host=~".*"}')

        self.assertEqual(result.feasibility, "feasible")
        self.assertNotIn(_DROPPED_VARS, result.warnings)

    def test_reproduction_panel_does_not_claim_a_variable_was_dropped(self):
        result = _translate(HPA_MAX_EXPR, hints=HPA_HINTS)

        self.assertNotIn(_DROPPED_VARS, result.warnings)

    def test_template_variable_matchers_still_warn(self):
        for expr in (
            'a_metric{host=~"$host"}',
            'a_metric{host="$host"}',
            'a_metric{host="${host}"}',
            'a_metric{host=~"[[host]]"}',
        ):
            with self.subTest(expr=expr):
                result = _translate(expr)

                self.assertIn(_DROPPED_VARS, result.warnings)


if __name__ == "__main__":
    unittest.main()
