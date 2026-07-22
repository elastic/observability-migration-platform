# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for metric map binding helpers."""

from __future__ import annotations

import unittest

from observability_migration.core.metric_mapping import (
    CLASS_EXACT,
    CLASS_REQUIRES_TRANSFORM,
    MetricMapResult,
    binding_from_result,
    normalize_metric_map,
    plan_rate_transform,
    resolve_metric_map,
)
from observability_migration.core.metric_mapping.entries import MetricMapEntry


class MetricBindingTests(unittest.TestCase):
    def test_binding_from_exact_result(self):
        metric_map = normalize_metric_map({"http_requests_total": "http.server.request.duration"})
        result = resolve_metric_map("http_requests_total", metric_map)
        assert result is not None
        binding = binding_from_result(result)
        self.assertEqual(binding.source, "http_requests_total")
        self.assertEqual(binding.target_field, "http.server.request.duration")
        self.assertTrue(binding.applied)
        self.assertEqual(binding.klass, CLASS_EXACT)
        self.assertTrue(binding.native_promql_compatible)

    def test_binding_from_class2_result(self):
        metric_map = normalize_metric_map(
            {
                "src": {
                    "target": "dst",
                    "transform": "to_rate",
                    "attribute_filter": {"direction": "in"},
                    "unit_scale": 0.001,
                    "target_index": "metrics-*",
                }
            }
        )
        result = resolve_metric_map("src", metric_map)
        assert result is not None
        binding = binding_from_result(result)
        self.assertEqual(binding.target_field, "dst")
        self.assertEqual(binding.transform, "to_rate")
        self.assertEqual(binding.target_filters, {"direction": "in"})
        self.assertEqual(binding.target_index, "metrics-*")
        self.assertEqual(binding.unit_scale, 0.001)
        self.assertFalse(binding.native_promql_compatible)

    def test_binding_from_unapplied_gap(self):
        result = MetricMapResult(
            source="src",
            target="src",
            applied=False,
            klass=CLASS_REQUIRES_TRANSFORM,
            gap_reason="no variant matched",
            entry=MetricMapEntry(target="dst"),
        )
        binding = binding_from_result(result)
        self.assertFalse(binding.applied)
        self.assertEqual(binding.gap_reason, "no variant matched")
        self.assertFalse(binding.native_promql_compatible)


class PlanRateTransformTests(unittest.TestCase):
    def test_none_without_rate(self):
        self.assertEqual(plan_rate_transform(source_has_rate=False, transform="none", target_is_counter=None), ("none", ""))

    def test_none_with_rate(self):
        self.assertEqual(plan_rate_transform(source_has_rate=True, transform="none", target_is_counter=None), ("keep_source_rate", ""))

    def test_drop_rate_on_rate_gauge(self):
        self.assertEqual(plan_rate_transform(source_has_rate=True, transform="drop_rate", target_is_counter=False), ("drop_rate", ""))

    def test_drop_rate_on_rate_counter_is_gap(self):
        action, reason = plan_rate_transform(source_has_rate=True, transform="drop_rate", target_is_counter=True)
        self.assertEqual(action, "gap")
        self.assertIn("counter", reason)

    def test_drop_rate_without_rate(self):
        self.assertEqual(plan_rate_transform(source_has_rate=False, transform="drop_rate", target_is_counter=False), ("none", ""))

    def test_to_rate_on_counter(self):
        self.assertEqual(plan_rate_transform(source_has_rate=False, transform="to_rate", target_is_counter=True), ("to_rate", ""))

    def test_to_rate_on_gauge_is_gap(self):
        action, reason = plan_rate_transform(source_has_rate=False, transform="to_rate", target_is_counter=False)
        self.assertEqual(action, "gap")
        self.assertIn("gauge", reason)

    def test_to_rate_when_already_rated(self):
        self.assertEqual(plan_rate_transform(source_has_rate=True, transform="to_rate", target_is_counter=True), ("keep_source_rate", ""))


if __name__ == "__main__":
    unittest.main()
