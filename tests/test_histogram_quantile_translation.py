# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the histogram_quantile() PromQL -> ES|QL translation path (issue #55).

These exercise the ES|QL ``PERCENTILE()`` fallback that applies when the target
stack cannot evaluate ``histogram_quantile`` natively (ES < 9.5). The chosen
ES|QL form depends on the target field type of the *base* histogram metric:

- ``exponential_histogram`` / tdigest -> ``PERCENTILE(field, phi*100)``
- ``histogram``                       -> ``PERCENTILE(TO_TDIGEST(field), phi*100)``
- unknown / schema unavailable        -> not_feasible (field type can't be verified)
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

    def test_bare_bucket_series_without_outer_agg_still_translates(self):
        resolver = _resolver_with_field_type(
            "http_request_duration_seconds", "exponential_histogram"
        )
        result = _translate(
            "histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))",
            resolver,
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertIn("PERCENTILE(http_request_duration_seconds, 95)", result.esql_query)


class HistogramQuantileUnknownFieldTests(unittest.TestCase):
    def test_offline_unknown_field_type_is_not_feasible(self):
        resolver = SchemaResolver(RulePackConfig())  # no field cache -> field type unknown
        result = _translate(
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
            resolver,
        )
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertFalse(result.esql_query)
        self.assertTrue(
            any("field type could not be determined" in w for w in result.warnings),
            result.warnings,
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
