# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for issue #377 — an aggregation wrapping a PromQL binary operator.

PromQL evaluates ``A <op> B`` per matching series pair, matching on the
operands' full label set, *before* the enclosing aggregation reduces the
survivors. ES|QL has no equivalent stage, so ``agg(A <op> B)`` is only
translatable when a specific rewrite proves the operator can be preserved
(``sum(A ± B)`` push-down, scalar hoisting, the co-located per-document
renderer).

Before this fix the parser recognised those rewrites and then fell through
*silently* for every other shape: the fragment kept ``family="unknown"`` with
``outer_agg`` set, and the generic ``fragment_extract``/``stats_expression``
fallback rebuilt ``agg(<first metric leaf>)`` from the fragment's summary
fields. The operator and every other operand were discarded, and the panel
shipped as ``migrated_with_warnings`` with a plausible but wrong number.

The reproduction is grafana.com dashboard 11454 ("K8s / Storage / Volumes /
Cluster"), panel 12 ``Running PVCs Above % Used Warning Threshold``: a
``count(<vector> and <comparison>)`` singlestat that rendered a drifting count
of *all* PVCs while its sibling graph panel 41 — the same expression without the
``count(...)`` wrapper — was already correctly refused as ``not_feasible``.

Covered here:

1. The reproduction refuses honestly, emits no ES|QL, and reaches the operator
   in its message.
2. The refusal is a property of the nesting, not of the panel type, and the
   wrapped and bare forms of the same expression now agree.
3. ``and``/``unless``, two-series comparisons, and arithmetic with no renderer
   all refuse rather than dropping an operand.
4. The rewrites that *can* preserve the operator still translate, byte for byte
   — including ``or``, which keeps its established range-window fallback.

``or`` between two genuinely different metrics was deliberately left open here
and closed later, at classification time, by issue #434 — see
``tests/test_issue434_agg_over_or_operand_drop.py``.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.promql import (
    _agg_over_binary_not_feasible_reason,
)
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)

INDEX = "metrics-*"

# Issue #377 reproduction: dashboard 11454 panel 12, with the
# ``$pvc_percent_used_warning_threshold`` textbox variable already inlined as
# ``80`` by the issue #378 pre-pass.
PVC_COUNT_EXPR = (
    "count (\n"
    "  (max by (persistentvolumeclaim,namespace) (kubelet_volume_stats_used_bytes ))\n"
    "  and\n"
    "  (\n"
    "    (max by (persistentvolumeclaim,namespace) (kubelet_volume_stats_used_bytes ))\n"
    "    / (max by (persistentvolumeclaim,namespace) (kubelet_volume_stats_capacity_bytes ))\n"
    "  ) >= (80 / 100)\n"
    ")\n"
    "or vector(0)"
)

# Panel 41 of the same dashboard: the identical construct rendered over time,
# i.e. the inner expression without the ``count(...) or vector(0)`` wrapper.
PVC_BARE_EXPR = (
    "(max by (persistentvolumeclaim,namespace) (kubelet_volume_stats_used_bytes ))\n"
    "and\n"
    "(\n"
    "  (max by (persistentvolumeclaim,namespace) (kubelet_volume_stats_used_bytes ))\n"
    "  / (max by (persistentvolumeclaim,namespace) (kubelet_volume_stats_capacity_bytes ))\n"
    ") >= (80 / 100)"
)


def _translate(expr: str, panel_type: str = "stat"):
    rule_pack = rules.RulePackConfig()
    return translate_promql_to_esql(
        expr,
        datasource_index=INDEX,
        esql_index=INDEX,
        panel_type=panel_type,
        rule_pack=rule_pack,
        resolver=schema.SchemaResolver(rule_pack),
        translation_hints={"summary_mode": panel_type in {"stat", "singlestat"}},
    )


def _translate_panel(expr: str, panel_type: str = "stat"):
    """Migrate a one-panel dashboard the way a real run does."""
    rule_pack = rules.RulePackConfig()
    dashboard = {
        "uid": "u-377",
        "title": "issue 377",
        "panels": [
            {
                "id": 12,
                "type": panel_type,
                "title": "Running PVCs Above % Used Warning Threshold",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                "targets": [
                    {"refId": "A", "expr": expr, "datasource": {"type": "prometheus"}}
                ],
            }
        ],
    }
    result = panels.translate_dashboard(
        dashboard,
        datasource_index=INDEX,
        esql_index=INDEX,
        rule_pack=rule_pack,
        resolver=schema.SchemaResolver(rule_pack),
    )
    return result.panel_results[0], result


class TestIssue377Reproduction(unittest.TestCase):
    """grafana.com 11454 panel 12 must refuse instead of shipping a wrong count."""

    def test_count_over_and_comparison_is_not_feasible(self):
        translated = _translate(PVC_COUNT_EXPR)

        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertEqual(translated.confidence, 0.0)
        self.assertFalse(translated.esql_query)

    def test_refusal_names_the_operator_and_the_aggregation(self):
        translated = _translate(PVC_COUNT_EXPR)

        joined = " ".join(translated.warnings)
        self.assertIn("set operator 'and' inside an aggregation", joined)
        self.assertIn("count(A and B)", joined)
        self.assertIn("not_feasible", joined)

    def test_no_bare_count_over_the_first_metric_is_emitted(self):
        """The exact symptom: COUNT(kubelet_volume_stats_used_bytes), threshold gone."""
        translated = _translate(PVC_COUNT_EXPR)

        self.assertNotIn("COUNT(", translated.esql_query or "")
        self.assertNotIn("kubelet_volume_stats_used_bytes", translated.esql_query or "")

    def test_panel_status_is_not_feasible_not_migrated_with_warnings(self):
        panel_result, result = _translate_panel(PVC_COUNT_EXPR)

        self.assertEqual(panel_result.status, "not_feasible")
        self.assertEqual(result.not_feasible, 1)
        self.assertEqual(result.migrated_with_warnings, 0)

    def test_wrapped_and_bare_forms_of_the_same_expression_agree(self):
        """Panel 12 and its sibling panel 41 reached opposite verdicts before."""
        wrapped = _translate(PVC_COUNT_EXPR, panel_type="stat")
        bare = _translate(PVC_BARE_EXPR, panel_type="timeseries")

        self.assertEqual(wrapped.feasibility, "not_feasible")
        self.assertEqual(bare.feasibility, "not_feasible")

    def test_refusal_does_not_depend_on_panel_type(self):
        for panel_type in ("stat", "singlestat", "gauge", "timeseries", "graph", "table"):
            with self.subTest(panel_type=panel_type):
                self.assertEqual(
                    _translate(PVC_COUNT_EXPR, panel_type=panel_type).feasibility,
                    "not_feasible",
                )


class TestAggOverBinaryRefusals(unittest.TestCase):
    """Every ``agg(A op B)`` shape with no honest rewrite must refuse."""

    def test_set_operators_between_distinct_series(self):
        for agg in ("count", "sum", "max", "min", "avg"):
            for op in ("and", "unless"):
                with self.subTest(agg=agg, op=op):
                    translated = _translate(f"{agg}(node_up {op} node_ready)")
                    self.assertEqual(translated.feasibility, "not_feasible")
                    self.assertFalse(translated.esql_query)

    def test_set_operators_between_the_same_metric(self):
        """Differing matchers are still a per-series intersection, not a filter."""
        for op in ("and", "unless"):
            with self.subTest(op=op):
                translated = _translate(
                    f'sum(node_up{{job="a"}} {op} node_up{{job="b"}})'
                )
                self.assertEqual(translated.feasibility, "not_feasible")

    def test_comparison_between_two_series(self):
        for op in (">", ">=", "<", "<=", "==", "!="):
            with self.subTest(op=op):
                translated = _translate(
                    f"count(node_filesystem_avail_bytes {op} node_filesystem_size_bytes)"
                )
                self.assertEqual(translated.feasibility, "not_feasible")
                self.assertFalse(translated.esql_query)

    def test_arithmetic_with_no_renderer(self):
        # %, ^ and atan2 are outside the co-located renderer's allowlist, and
        # grouped operands are outside it too (each carries its own reduction).
        for expr in (
            "count(node_a % node_b)",
            "max(node_a ^ 5)",
            "count(node_a atan2 node_b)",
            "count(max by (ns) (node_a) + max by (ns) (node_b))",
        ):
            with self.subTest(expr=expr):
                translated = _translate(expr)
                self.assertEqual(translated.feasibility, "not_feasible")
                self.assertFalse(translated.esql_query)

    def test_no_binary_operator_silently_drops_an_operand(self):
        """The invariant the guard exists to hold, swept over every operator.

        Each operator must either refuse, or emit a query that still reads the
        right-hand operand. Shipping ``COUNT(node_a)`` for ``count(node_a op
        node_b)`` is the bug. The guard is an allowlist of operators it hands on
        (only ``or``) rather than a list of operators it refuses, because a
        deny-list omitted ``atan2`` and let exactly that through unwarned.

        ``or`` is still the one operator the *parse-time* guard hands on -- its
        two reductions need a resolver the parser does not have -- but it is
        swept here too, because ``agg_over_or_operand_drop_rule`` now closes the
        same invariant at classification time, where the resolver exists
        (issue #434).
        """
        for op in (
            "+", "-", "*", "/", "%", "^", "atan2",
            "==", "!=", ">", "<", ">=", "<=",
            "and", "unless", "or",
        ):
            with self.subTest(op=op):
                translated = _translate(f"count(node_a {op} node_b)")
                if translated.feasibility == "not_feasible":
                    self.assertFalse(translated.esql_query)
                    self.assertTrue(translated.warnings)
                else:
                    self.assertIn("node_b", translated.esql_query)

    def test_scalar_comparison_on_the_left_is_not_silently_dropped(self):
        translated = _translate("max(5 > node_load1)")

        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertNotIn("MAX(", translated.esql_query or "")


class TestAggOverBinaryStillTranslatable(unittest.TestCase):
    """The rewrites that preserve the operator must be untouched by the guard."""

    def test_scalar_comparison_filter_still_counts_series(self):
        """count(m > k) has a real translation and must not be caught."""
        translated = _translate("count(kubelet_volume_stats_used_bytes > 0.8)")

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("WHERE kubelet_volume_stats_used_bytes > 0.8", translated.esql_query)
        self.assertIn("COUNT(*)", translated.esql_query)

    def test_colocated_arithmetic_still_evaluates_per_document(self):
        for expr in (
            "count(node_a + node_b)",
            "max(node_a - node_b)",
            "sum(node_a + 5)",
            "max(5 - node_a)",
        ):
            with self.subTest(expr=expr):
                translated = _translate(expr)
                self.assertEqual(translated.feasibility, "feasible")
                self.assertIn("computed_value", translated.esql_query)

    def test_sum_over_addition_still_pushes_down(self):
        translated = _translate("sum(node_a + node_b)")

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("SUM(node_a)", translated.esql_query)
        self.assertIn("SUM(node_b)", translated.esql_query)

    def test_scalar_scaling_still_hoists(self):
        translated = _translate("max(node_a * 8)", panel_type="timeseries")

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("MAX(node_a)", translated.esql_query)
        self.assertIn("* 8", translated.esql_query)

    def test_or_keeps_its_range_window_fallback(self):
        """``or`` is deliberately outside the guard; this idiom must keep working."""
        translated = _translate(
            "avg by (service_name) ("
            "max_over_time(mysql_max_used_connections[$interval])"
            " or max_over_time(mysql_max_used_connections[5m]))",
            panel_type="timeseries",
        )

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("AVG(MAX_OVER_TIME(", translated.esql_query)

    def test_histogram_summary_ratio_still_approximates(self):
        translated = _translate(
            "sum(increase(rpc_duration_sum[5m]) / increase(rpc_duration_count[5m]))",
            panel_type="timeseries",
        )

        self.assertEqual(translated.feasibility, "feasible")
        self.assertTrue(translated.esql_query)


class TestAggOverBinaryReason(unittest.TestCase):
    """The refusal message has to tell an operator what to do next."""

    def test_set_operator_reason(self):
        reason = _agg_over_binary_not_feasible_reason("count", "and")

        self.assertIn("set operator 'and' inside an aggregation", reason)
        self.assertIn("count(A and B)", reason)

    def test_comparison_reason_suggests_a_scalar_threshold(self):
        reason = _agg_over_binary_not_feasible_reason("sum", ">=")

        self.assertIn("sum(A >= B)", reason)
        self.assertIn("scalar threshold", reason)

    def test_ratio_reason_is_unchanged_for_multiplication_and_division(self):
        """Existing snapshots pin this wording; keep it byte-identical."""
        for op in ("*", "/"):
            with self.subTest(op=op):
                self.assertEqual(
                    _agg_over_binary_not_feasible_reason("sum", op),
                    f"Aggregating over a per-element {op} between two time-series "
                    f"(sum(A {op} B)) cannot be expressed accurately in ES|QL; "
                    "rewrite as a ratio of aggregates if the series are label-aligned",
                )

    def test_reason_tolerates_a_missing_outer_agg(self):
        self.assertIn("aggregation(A and B)", _agg_over_binary_not_feasible_reason("", "and"))

    def test_reason_tolerates_an_unrecognised_operator(self):
        reason = _agg_over_binary_not_feasible_reason("count", "")

        self.assertIn("unrecognised PromQL binary expression", reason)
        self.assertIn("count(A op B)", reason)


if __name__ == "__main__":
    unittest.main()
