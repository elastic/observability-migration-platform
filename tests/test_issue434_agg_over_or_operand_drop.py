# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for issue #434 — ``agg(A or B)`` dropping the right-hand operand.

Issue #377 closed ``agg(A <op> B)`` for every binary operator *except* ``or``.
``or`` was deferred because it carries two reductions that only a stage holding
a schema resolver can evaluate: the Grafana same-metric range-window fallback
(``rate(M[$interval]) or irate(M[5m])``) and the live-absent operand drop. A
parse-time refusal cannot tell those apart from a genuine cross-metric union,
so it would have refused both.

The hole that was left is ``or`` between operands that neither reduction
covers. ``count(node_a or node_b)`` emitted ``COUNT(node_a)`` — feasible,
confidence 0.85, **no warning at all** — because no rewrite claimed the
fragment and the generic ``fragment_extract``/``stats_expression`` fallback
rebuilt ``agg(<first metric leaf>)`` from the fragment's summary fields.

The bare (unaggregated) forms of the very same chains had already settled this:
``node_a or node_b`` keeps both operands as ``COALESCE(node_a, node_b)``,
``node_up{job="a"} or node_up{job="b"}`` keeps both as a unified ``WHERE ... OR``,
and a chain whose operands cannot be aligned is refused outright with "marked
for manual review so no series are silently dropped". Wrapping the identical
chain in ``count(...)`` inverted every one of those verdicts. That wrapped-vs-bare
disagreement is the same defect #377 fixed for ``and``.

An aggregation cannot be distributed over a union — ``agg(A or B)`` is not
``agg(A) or agg(B)`` — so the union rewrites the bare path uses are unavailable
here and refusing is the honest answer.

Covered here:

1. The reproduction refuses, emits no ES|QL, and names both operands.
2. The refusal survives ``by()`` grouping, longer ``or`` chains, every outer
   aggregation, every panel type, and an enclosing wrapper such as
   ``clamp_max()`` — the follow-ups the issue asked to check.
3. The two reductions the deferral existed to protect still translate.
4. Those reductions now *disclose* the operand they drop, with the same warning
   text the bare path already emits.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.promql import (
    _agg_over_binary_not_feasible_reason,
    _agg_over_or_not_feasible_reason,
)
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)

INDEX = "metrics-*"

# grafana.com "Node Exporter Full"-style CPU panel: a Linux node_exporter
# expression ``or``-ed with a differently-named cloud metric, each side itself a
# same-metric range-window fallback. The bare chain is already refused as
# unalignable; only the ``avg by (...)`` wrapper let it through.
MIXED_SOURCE_CHAIN = (
    '(avg by (mode) ( '
    '(clamp_max(rate(node_cpu_seconds_total{mode!="idle"}[$interval]),1)) '
    'or (clamp_max(irate(node_cpu_seconds_total{mode!="idle"}[5m]),1)) '
    "))*100 "
    'or (max_over_time(node_cpu_average{mode=~"user|system"}[$interval]) '
    'or max_over_time(node_cpu_average{mode=~"user|system"}[5m]))'
)


def _resolver(rule_pack, fields=None):
    """Offline resolver by default; a live one when *fields* is given."""
    resolver = schema.SchemaResolver(rule_pack)
    if fields is not None:
        resolver._discovery_attempted = True
        resolver._field_cache = dict(fields)
        resolver._discovered_mappings = {}
        resolver._schema_profile_cache_id = None
        resolver._discovery_status = "ok"
    return resolver


def _translate(expr, panel_type="timeseries", fields=None):
    rule_pack = rules.RulePackConfig()
    return translate_promql_to_esql(
        expr,
        datasource_index=INDEX,
        esql_index=INDEX,
        panel_type=panel_type,
        rule_pack=rule_pack,
        resolver=_resolver(rule_pack, fields),
        translation_hints={"summary_mode": panel_type in {"stat", "singlestat"}},
    )


def _translate_panel(expr, panel_type="stat"):
    """Migrate a one-panel dashboard the way a real run does."""
    rule_pack = rules.RulePackConfig()
    dashboard = {
        "uid": "u-434",
        "title": "issue 434",
        "panels": [
            {
                "id": 7,
                "type": panel_type,
                "title": "Nodes up",
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
        resolver=_resolver(rule_pack),
    )
    return result.panel_results[0], result


class TestIssue434Reproduction(unittest.TestCase):
    """``count(node_a or node_b)`` must refuse instead of counting node_a."""

    def test_count_over_cross_metric_or_is_not_feasible(self):
        translated = _translate("count(node_a or node_b)")

        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertEqual(translated.confidence, 0.0)
        self.assertFalse(translated.esql_query)

    def test_the_exact_symptom_no_count_over_the_left_operand(self):
        """``STATS node_a = COUNT(node_a)`` with node_b nowhere in the query."""
        translated = _translate("count(node_a or node_b)")

        self.assertNotIn("COUNT(", translated.esql_query or "")
        self.assertNotIn("node_a", translated.esql_query or "")

    def test_refusal_names_the_operator_and_both_operands(self):
        joined = " ".join(_translate("count(node_a or node_b)").warnings)

        self.assertIn("set operator 'or' inside an aggregation", joined)
        self.assertIn("count(A or B)", joined)
        self.assertIn("node_a", joined)
        self.assertIn("node_b", joined)
        self.assertIn("not_feasible", joined)

    def test_panel_status_is_not_feasible_not_a_plausible_number(self):
        panel_result, result = _translate_panel("count(node_a or node_b)")

        self.assertEqual(panel_result.status, "not_feasible")
        self.assertEqual(result.not_feasible, 1)
        self.assertEqual(result.migrated_with_warnings, 0)


class TestAggOverOrRefusals(unittest.TestCase):
    """Every ``agg(<irreducible or chain>)`` shape must refuse."""

    def test_every_outer_aggregation_refuses(self):
        for agg in ("count", "sum", "max", "min", "avg", "stddev", "stdvar"):
            with self.subTest(agg=agg):
                translated = _translate(f"{agg}(node_a or node_b)")
                self.assertEqual(translated.feasibility, "not_feasible")
                self.assertFalse(translated.esql_query)

    def test_grouping_does_not_hide_the_drop(self):
        """The issue asked whether ``by()`` reopened the hole. It did."""
        for expr in (
            "count by (job) (node_a or node_b)",
            "sum by (instance, job) (node_a or node_b)",
            "count(node_a or node_b) by (job)",
        ):
            with self.subTest(expr=expr):
                translated = _translate(expr)
                self.assertEqual(translated.feasibility, "not_feasible")
                self.assertFalse(translated.esql_query)

    def test_nested_or_chains_refuse(self):
        """The issue asked about nested chains. A 3-way union drops two operands."""
        translated = _translate("count(node_a or node_b or node_c)")

        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertFalse(translated.esql_query)
        joined = " ".join(translated.warnings)
        for metric in ("node_a", "node_b", "node_c"):
            self.assertIn(metric, joined)

    def test_same_metric_with_differing_matchers_refuses(self):
        """``COUNT(node_up)`` with *both* job filters gone was the old output."""
        translated = _translate('count(node_up{job="a"} or node_up{job="b"})')

        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertFalse(translated.esql_query)

    def test_quantile_over_a_union_refuses(self):
        translated = _translate("quantile(0.9, node_a or node_b)")

        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertFalse(translated.esql_query)

    def test_an_enclosing_wrapper_does_not_reopen_the_hole(self):
        """``clamp_max(sum(a or b), 100)`` kept the aggregate fragment on top."""
        translated = _translate("clamp_max(sum(node_a or node_b), 100)")

        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertFalse(translated.esql_query)

    def test_refusal_does_not_depend_on_panel_type(self):
        for panel_type in ("stat", "singlestat", "gauge", "timeseries", "graph", "table"):
            with self.subTest(panel_type=panel_type):
                self.assertEqual(
                    _translate("count(node_a or node_b)", panel_type=panel_type).feasibility,
                    "not_feasible",
                )

    def test_wrapped_and_bare_forms_of_the_same_chain_agree(self):
        """The #377 invariant, applied to ``or``: the wrapper cannot flip the verdict."""
        bare = _translate(MIXED_SOURCE_CHAIN)
        wrapped = _translate(f"clamp_max(avg by (node_name,mode) ({MIXED_SOURCE_CHAIN}),100)")

        self.assertEqual(bare.feasibility, "not_feasible")
        self.assertEqual(wrapped.feasibility, "not_feasible")
        self.assertFalse(wrapped.esql_query)

    def test_no_refused_chain_leaks_a_partial_query(self):
        for expr in (
            "count(node_a or node_b)",
            "sum by (job) (node_a or node_b)",
            "avg(node_a or node_b or node_c)",
            'count(node_up{job="a"} or node_up{job="b"})',
        ):
            with self.subTest(expr=expr):
                translated = _translate(expr)
                self.assertFalse(translated.esql_query)
                self.assertTrue(translated.warnings)


class TestAggOverOrStillTranslatable(unittest.TestCase):
    """The reductions the ``or`` deferral existed to protect must be untouched."""

    def test_or_vector_zero_fill_still_translates(self):
        translated = _translate("count(node_a or vector(0))")

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("COUNT(node_a)", translated.esql_query)

    def test_same_metric_range_window_fallback_still_translates(self):
        translated = _translate(
            "avg by (service_name) ("
            "max_over_time(mysql_max_used_connections[$interval])"
            " or max_over_time(mysql_max_used_connections[5m]))",
        )

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("AVG(MAX_OVER_TIME(", translated.esql_query)

    def test_rate_irate_fallback_under_an_aggregation_still_translates(self):
        translated = _translate("avg(rate(m_total[10m]) or irate(m_total[5m]))")

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("AVG(RATE(m_total))", translated.esql_query)

    def test_colocated_ratio_over_a_range_fallback_still_renders(self):
        translated = _translate(
            "sum(rate(a_total[5m]) / rate(b_total[5m])"
            " or irate(a_total[5m]) / irate(b_total[5m]))"
        )

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("SUM((RATE(a_total) / RATE(b_total)))", translated.esql_query)

    def test_live_absent_operand_still_reduces_to_the_surviving_one(self):
        """The resolver-backed drop the issue named as legitimate."""
        translated = _translate(
            "count(node_a or node_b)",
            fields={"node_a": {"double": {"searchable": True, "aggregatable": True}}},
        )

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("COUNT(node_a)", translated.esql_query)

    def test_an_operand_absent_only_offline_is_not_dropped(self):
        """Without live caps nothing is disprovable, so the chain still refuses."""
        translated = _translate("count(node_a or node_b)")

        self.assertEqual(translated.feasibility, "not_feasible")


class TestAggOverOrDisclosure(unittest.TestCase):
    """A reduction that drops an operand has to say so, as the bare path does."""

    def test_range_window_fallback_drop_is_disclosed(self):
        translated = _translate("avg(rate(m_total[10m]) or irate(m_total[5m]))")

        joined = " ".join(translated.warnings)
        self.assertIn("PromQL same-metric 'or': preferred left 'rate(...)'", joined)
        self.assertIn("irate(...)", joined)

    def test_wrapped_and_bare_disclose_the_same_range_fallback_drop(self):
        bare = _translate("rate(m_total[10m]) or irate(m_total[5m])")
        wrapped = _translate("avg(rate(m_total[10m]) or irate(m_total[5m]))")

        shared = [w for w in bare.warnings if "same-metric 'or'" in w]
        self.assertTrue(shared)
        for warning in shared:
            self.assertIn(warning, wrapped.warnings)

    def test_colocated_ratio_discloses_the_dropped_window_fallback(self):
        translated = _translate(
            "sum(rate(a_total[5m]) / rate(b_total[5m])"
            " or irate(a_total[5m]) / irate(b_total[5m]))"
        )

        self.assertIn(
            "PromQL same-metric 'or': preferred left range-window operand and "
            "dropped the alternate-window fallback; Grafana uses the right "
            "side only when the left lacks samples",
            translated.warnings,
        )

    def test_absent_operand_drop_is_disclosed(self):
        translated = _translate(
            "count(node_a or node_b)",
            fields={"node_a": {"double": {"searchable": True, "aggregatable": True}}},
        )

        self.assertIn(
            "PromQL 'or': dropped operands whose metrics are absent from the live target",
            translated.warnings,
        )

    def test_a_chain_that_loses_nothing_stays_clean(self):
        """``count(m)`` has no ``or`` to reduce; the rule must not tag it."""
        translated = _translate("count(node_a)")

        self.assertEqual(translated.feasibility, "feasible")
        joined = " ".join(translated.warnings)
        self.assertNotIn("same-metric 'or'", joined)
        self.assertNotIn("absent from the live target", joined)


class TestAggOverOrReason(unittest.TestCase):
    """The refusal message has to tell an operator what to do next."""

    def test_reason_describes_union_semantics_not_filtering(self):
        reason = _agg_over_or_not_feasible_reason("count")

        self.assertIn("set operator 'or' inside an aggregation", reason)
        self.assertIn("count(A or B)", reason)
        self.assertIn("union", reason)
        self.assertIn("not_feasible", reason)

    def test_reason_lists_the_operand_metrics(self):
        reason = _agg_over_or_not_feasible_reason("sum", ["node_a", "node_b"])

        self.assertIn("node_a", reason)
        self.assertIn("node_b", reason)

    def test_reason_tolerates_a_missing_outer_agg(self):
        self.assertIn("aggregation(A or B)", _agg_over_or_not_feasible_reason(""))

    def test_binary_reason_helper_routes_or_to_the_union_text(self):
        """``or`` must never inherit the ``and``/``unless`` "selects which series" text."""
        reason = _agg_over_binary_not_feasible_reason("count", "or")

        self.assertEqual(reason, _agg_over_or_not_feasible_reason("count"))
        self.assertNotIn("selects which series survive", reason)

    def test_and_and_unless_wording_is_unchanged(self):
        for op in ("and", "unless"):
            with self.subTest(op=op):
                reason = _agg_over_binary_not_feasible_reason("count", op)
                self.assertIn("selects which series survive", reason)


if __name__ == "__main__":
    unittest.main()
