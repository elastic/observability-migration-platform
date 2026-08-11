# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for issue #156 — label-aligned vector-matching joins.

A PromQL panel that joins two metrics on a shared key with explicit vector
matching (``on``/``group_left``/``group_right``) and aggregates per key can be
translated to a per-key ES|QL aggregation that is numerically identical to the
source. Two requirements follow:

1. **Clean status.** A faithfully-translated, label-aligned per-key join is
   bit-for-bit identical to the source PromQL, so it must be reported as
   *migrated (clean)* — never "migrated with warnings". The "same-bucket
   approximation" caveat is wrong for this subset and erodes trust.

2. **Enrichment carried, not dropped.** ``A * on(k) group_left(l) B`` is a
   label-enrichment join: ``l`` lives on the same data stream as ``A`` (the
   metrics are co-scraped), so it belongs in the ``STATS ... BY`` clause rather
   than being dropped.

Genuinely lossy shapes (per-element ``avg(A/B)``) stay ``not_feasible`` and
plain non-join arithmetic keeps its same-bucket caveat — those are unchanged.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import translate_promql_to_esql

INDEX = "metrics-*"

_APPROX_ARITH = "Approximated PromQL arithmetic using same-bucket ES|QL math"
_APPROX_RATIO = "Approximated PromQL join ratio as same-bucket ES|QL ratio"
_DROPPED_ENRICH = "Dropped group_left label enrichment"


class TestLabelAlignedJoinsCleanStatus(unittest.TestCase):
    def setUp(self):
        self.rule_pack = RulePackConfig()
        self.resolver = SchemaResolver(self.rule_pack)

    def _translate(self, expr, panel_type="timeseries"):
        return translate_promql_to_esql(
            expr,
            datasource_index=INDEX,
            panel_type=panel_type,
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    def _resolved(self, label):
        return self.resolver.resolve_labels([label])[0]

    def test_ratio_on_key_is_clean(self):
        """sum(a) by(k) / on(k) sum(b) by(k): exact per-key ratio, no caveat."""
        result = self._translate("sum(a) by (instance) / on(instance) sum(b) by (instance)")
        self.assertEqual(result.feasibility, "feasible")
        self.assertNotIn(_APPROX_ARITH, result.warnings)
        # The matching key must survive into the grouping for the parity to hold.
        self.assertIn(self._resolved("instance"), result.esql_query)

    def test_ratio_mirrors_source_aggregator(self):
        """sum→SUM, not a forced AVG (the per-key parity depends on it)."""
        result = self._translate(
            "sum(ceph_pool_stored) by (pool_id) / on(pool_id) sum(ceph_pool_max_avail) by (pool_id)"
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn("SUM(ceph_pool_stored)", result.esql_query)
        self.assertIn("SUM(ceph_pool_max_avail)", result.esql_query)
        self.assertNotIn(_APPROX_ARITH, result.warnings)

    def test_rate_ratio_group_left_denominator_is_clean(self):
        """rate(A)/on(k) group_left rate(B): exact per-key ratio of aggregates."""
        result = self._translate(
            'sum by(instance) (irate(node_cpu_guest_seconds_total{mode="user"}[1m]))'
            " / on(instance) group_left sum by(instance)(irate(node_cpu_seconds_total[1m]))"
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertNotIn(_APPROX_RATIO, result.warnings)
        self.assertIn(self._resolved("instance"), result.esql_query)

    def test_plain_non_join_arithmetic_keeps_caveat(self):
        """No vector matcher → still a same-bucket approximation; unchanged."""
        result = self._translate("1 - sum(a)/sum(b)")
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn(_APPROX_ARITH, result.warnings)

    def test_ignoring_matcher_keeps_caveat(self):
        """`ignoring(k)` is not the proven on()-aligned subset; stay cautious."""
        result = self._translate(
            "sum(a) by (instance) / ignoring(instance) sum(b) by (instance)"
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn(_APPROX_ARITH, result.warnings)

    def test_per_element_ratio_is_not_substituted_by_ratio_of_aggregates(self):
        """avg(A/B) must never become avg(A)/avg(B).

        Issue #156's concern was the silent substitution, and it still holds --
        but refusing outright is no longer the only way to honour it.
        ``colocated_binary_agg_family`` divides per document and aggregates the
        result, which IS avg(A/B): the operands carry no on()/ignoring(), so
        PromQL matches on all labels and they share a document per label-set.

        Asserted on the emitted query rather than on refusal, so a future
        regression to ratio-of-aggregates fails here.
        """
        result = self._translate(
            "avg(node_filesystem_avail_bytes / node_filesystem_size_bytes)"
        )
        self.assertEqual(result.feasibility, "feasible")
        query = result.esql_query or ""
        # The division must sit INSIDE the aggregate, not between two of them.
        self.assertRegex(
            query.replace("\n", " "),
            r"AVG\(\s*\(?node_filesystem_avail_bytes\s*/\s*node_filesystem_size_bytes",
        )
        self.assertNotRegex(query, r"AVG\([^)]*\)\s*/\s*AVG\(")


class TestGroupLeftEnrichmentCarried(unittest.TestCase):
    def setUp(self):
        self.rule_pack = RulePackConfig()
        self.resolver = SchemaResolver(self.rule_pack)

    def _translate(self, expr, panel_type="timeseries"):
        return translate_promql_to_esql(
            expr,
            datasource_index=INDEX,
            panel_type=panel_type,
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    def _resolved(self, label):
        return self.resolver.resolve_labels([label])[0]

    def test_group_left_enrichment_label_in_by(self):
        """A * on(chip) group_left(chip_name) info → BY carries chip + chip_name."""
        result = self._translate(
            "node_hwmon_temp_celsius * on(chip) group_left(chip_name) node_hwmon_chip_names"
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn(self._resolved("chip"), result.esql_query)
        self.assertIn(self._resolved("chip_name"), result.esql_query)
        # Enrichment is carried, not dropped, so the degrade warning is gone.
        self.assertFalse(
            any(_DROPPED_ENRICH in w for w in result.warnings),
            result.warnings,
        )
        # The enrichment metric itself contributes no value (info metric == 1).
        self.assertNotIn("node_hwmon_chip_names", result.esql_query)


class TestCleanStatusRequiresAllSourceLabels(unittest.TestCase):
    """Review #164 — clean status must require *every* source dimension to
    survive, not just the on(...) key. group_left preserves the left series'
    labels; dropping a `by(...)` dimension (cpu, sensor) collapses series and
    changes values, so it must stay flagged (degrade gracefully)."""

    def setUp(self):
        self.rule_pack = RulePackConfig()
        self.resolver = SchemaResolver(self.rule_pack)

    def _translate(self, expr, panel_type="timeseries"):
        return translate_promql_to_esql(
            expr,
            datasource_index=INDEX,
            panel_type=panel_type,
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    def test_ratio_dropping_extra_by_label_is_not_clean(self):
        """on(instance) but left also keys by cpu → cpu dropped → keep caveat."""
        result = self._translate(
            "sum by(instance,cpu) (irate(a[1m]))"
            " / on(instance) group_left sum by(instance)(irate(b[1m]))"
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn(_APPROX_RATIO, result.warnings)

    def test_star_enrichment_dropping_extra_by_label_is_not_clean(self):
        """on(chip) group_left(chip_name) but left keys by sensor → keep warning."""
        result = self._translate(
            "sum by(chip,sensor)(node_hwmon_temp_celsius)"
            " * on(chip) group_left(chip_name) node_hwmon_chip_names"
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertTrue(
            any(_DROPPED_ENRICH in w for w in result.warnings),
            result.warnings,
        )

    def test_ratio_operand_aggregating_away_on_key_is_not_clean(self):
        """RHS ``sum(irate(b))`` has no ``instance`` to match on(instance) →
        the per-key denominator is invented, so keep the caveat."""
        result = self._translate(
            "sum by(instance)(irate(a[1m]))"
            " / on(instance) group_left sum(irate(b[1m]))"
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn(_APPROX_RATIO, result.warnings)

    def test_ratio_lhs_aggregating_away_on_key_is_not_clean(self):
        """Symmetric: LHS ``sum(irate(a))`` dropped the on(instance) key."""
        result = self._translate(
            "sum(irate(a[1m]))"
            " / on(instance) group_left sum by(instance)(irate(b[1m]))"
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn(_APPROX_RATIO, result.warnings)

    def test_nested_join_does_not_attach_inner_include_to_outer(self):
        """Outer join must not borrow a nested group_left(...) include list."""
        result = self._translate(
            "(a * on(k) group_left(inner_label) b)"
            " * on(j) group_left(outer_label) c"
        )
        self.assertEqual(result.feasibility, "feasible")
        # The outer join's BY must not contain the inner join's enrichment label.
        self.assertNotIn("inner_label", result.esql_query)


if __name__ == "__main__":
    unittest.main()
