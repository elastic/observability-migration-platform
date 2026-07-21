# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the histogram_quantile() PromQL -> ES|QL translation path (issue #55).

These exercise the ES|QL ``PERCENTILE()`` fallback that applies when the target
stack cannot evaluate ``histogram_quantile`` natively (ES < 9.5). The chosen
ES|QL form depends on the target field type of the *base* histogram metric:

- ``exponential_histogram`` / tdigest -> ``PERCENTILE(field, phi*100)``
- ``histogram``                       -> ``PERCENTILE(TO_TDIGEST(field), phi*100)``
- unknown / schema unavailable        -> assume exponential_histogram + warn
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import translate_promql_to_esql

INDEX = "metrics-*"


def _resolver_with_field_type(field_name: str, es_type: str) -> SchemaResolver:
    """Build an offline resolver whose field cache reports ``field_name`` as ``es_type``."""
    resolver = SchemaResolver(RulePackConfig())
    resolver._field_cache = {
        field_name: {
            es_type: {"type": es_type, "searchable": True, "aggregatable": True, "indices": [INDEX]},
        }
    }
    resolver._discovery_attempted = True
    return resolver


def _translate(expr: str, resolver: SchemaResolver):
    return translate_promql_to_esql(
        expr,
        datasource_index=INDEX,
        panel_type="timeseries",
        rule_pack=resolver._rule_pack,
        resolver=resolver,
    )


class HistogramQuantileExponentialHistogramTests(unittest.TestCase):
    def test_exponential_histogram_field_emits_percentile(self):
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )
        result = _translate(
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn("PERCENTILE(http_request_duration_seconds, 95)", result.esql_query)

    def test_percentile_path_emits_approximate_warning(self):
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )
        result = _translate(
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertTrue(
            any("approximate" in w and "t-digest" in w for w in result.warnings),
            result.warnings,
        )

    def test_full_query_shape_mirrors_percentile_path(self):
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )
        result = _translate(
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertEqual(
            result.esql_query,
            "TS metrics-*\n"
            "| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend\n"
            "| WHERE http_request_duration_seconds IS NOT NULL\n"
            "| STATS http_request_duration_seconds = "
            "PERCENTILE(http_request_duration_seconds, 95) BY time_bucket = TBUCKET(5 minute)\n"
            "| SORT time_bucket ASC",
        )


class HistogramQuantileHistogramFieldTests(unittest.TestCase):
    def test_histogram_field_wraps_in_to_tdigest(self):
        resolver = _resolver_with_field_type("http_request_duration_seconds", "histogram")
        result = _translate(
            "histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn(
            "PERCENTILE(TO_TDIGEST(http_request_duration_seconds), 99)", result.esql_query
        )


class HistogramQuantileBucketAggregationTests(unittest.TestCase):
    def test_non_sum_bucket_aggregation_is_not_feasible(self):
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )
        result = _translate(
            "histogram_quantile(0.95, max(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertFalse(result.esql_query)
        self.assertTrue(
            any("max" in w and "sum" in w for w in result.warnings), result.warnings
        )

    def test_bare_classic_bucket_series_is_not_feasible(self):
        # A bare classic _bucket operand keeps one series per (all labels except
        # le) in Prometheus; PERCENTILE BY time_bucket alone would collapse them
        # into one global percentile, and the non-le labels can't be enumerated.
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )
        result = _translate(
            "histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))",
            resolver,
        )
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertFalse(result.esql_query)


class HistogramQuantileClassicBucketLeTests(unittest.TestCase):
    def setUp(self):
        self.resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )

    def test_aggregation_without_le_is_not_feasible(self):
        result = _translate(
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (job))",
            self.resolver,
        )
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertFalse(result.esql_query)
        self.assertTrue(any("le" in w for w in result.warnings), result.warnings)

    def test_le_label_matcher_is_not_feasible(self):
        result = _translate(
            'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{le!="+Inf"}[5m])) by (le))',
            self.resolver,
        )
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertFalse(result.esql_query)
        self.assertTrue(any("le" in w for w in result.warnings), result.warnings)

    def test_sum_by_le_translates(self):
        result = _translate(
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            self.resolver,
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn("PERCENTILE(http_request_duration_seconds, 95)", result.esql_query)


class HistogramQuantilePhiRangeTests(unittest.TestCase):
    def test_phi_above_one_is_not_feasible(self):
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )
        result = _translate(
            "histogram_quantile(1.5, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertFalse(result.esql_query)
        self.assertTrue(any("1.5" in w for w in result.warnings), result.warnings)

    def test_phi_below_zero_is_not_feasible(self):
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )
        result = _translate(
            "histogram_quantile(-0.1, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertFalse(result.esql_query)

    def test_phi_at_bounds_translates(self):
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )
        result = _translate(
            "histogram_quantile(1.0, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn("PERCENTILE(http_request_duration_seconds, 100)", result.esql_query)


class HistogramQuantileUnknownFieldTests(unittest.TestCase):
    def test_offline_unknown_field_type_assumes_exponential_histogram(self):
        resolver = SchemaResolver(RulePackConfig())  # no field cache -> field type unknown
        result = _translate(
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn("PERCENTILE(http_request_duration_seconds, 95)", result.esql_query)
        self.assertTrue(
            any("assumed exponential_histogram" in w for w in result.warnings),
            result.warnings,
        )

    def test_aggregate_metric_double_is_not_feasible(self):
        # aggregate_metric_double stores only min/max/sum/value_count, not the
        # distribution, so an arbitrary percentile cannot be computed from it.
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "aggregate_metric_double"
        )
        result = _translate(
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertFalse(result.esql_query)
        self.assertTrue(
            any("aggregate_metric_double" in w for w in result.warnings), result.warnings
        )


class HistogramQuantileGroupingTests(unittest.TestCase):
    def test_le_dropped_but_other_labels_preserved(self):
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )
        result = _translate(
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, job))",
            resolver,
        )
        self.assertIn("BY time_bucket = TBUCKET(5 minute), service.name", result.esql_query)
        self.assertNotIn(" le", result.esql_query)


if __name__ == "__main__":
    unittest.main()
