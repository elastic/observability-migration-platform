# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for shared metric_mapping building blocks."""

from __future__ import annotations

import unittest

from observability_migration.core.metric_mapping import (
    CLASS_EXACT,
    CLASS_REQUIRES_TRANSFORM,
    MetricMapEntry,
    normalize_metric_map,
    parse_metric_map_entry,
    resolve_metric_map,
)


class MetricMapCoreTests(unittest.TestCase):
    def test_parse_string_entry(self):
        from observability_migration.core.metric_mapping import classify_metric_map_entry

        entry = parse_metric_map_entry("container.memory.working_set", source_key="a")
        self.assertEqual(entry.target, "container.memory.working_set")
        self.assertEqual(classify_metric_map_entry(entry), CLASS_EXACT)

    def test_parse_rich_entry_requires_transform(self):
        entry = parse_metric_map_entry(
            {
                "target": "k8s.pod.network.io",
                "attribute_filter": {"network.direction": "receive"},
            },
            source_key="net",
        )
        from observability_migration.core.metric_mapping import classify_metric_map_entry

        self.assertEqual(classify_metric_map_entry(entry), CLASS_REQUIRES_TRANSFORM)

    def test_reject_unknown_keys(self):
        with self.assertRaises(ValueError):
            parse_metric_map_entry({"target": "x", "extra": 1}, source_key="a")

    def test_reject_empty_target(self):
        with self.assertRaises(ValueError):
            MetricMapEntry(target="")

    def test_resolve_exact_applies(self):
        metric_map = normalize_metric_map(
            {"container_memory_working_set_bytes": "container.memory.working_set"}
        )
        result = resolve_metric_map("container_memory_working_set_bytes", metric_map)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.applied)
        self.assertEqual(result.target, "container.memory.working_set")
        self.assertEqual(result.klass, CLASS_EXACT)

    def test_resolve_class2_is_gap_not_rename(self):
        metric_map = normalize_metric_map(
            {
                "container_network_receive_bytes_total": {
                    "target": "k8s.pod.network.io",
                    "attribute_filter": {"network.direction": "receive"},
                }
            }
        )
        result = resolve_metric_map("container_network_receive_bytes_total", metric_map)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.applied)
        self.assertEqual(result.target, "container_network_receive_bytes_total")
        self.assertEqual(result.klass, CLASS_REQUIRES_TRANSFORM)
        self.assertIn("attribute_filter", result.gap_reason)

    def test_unit_scale_requires_transform_in_v1(self):
        metric_map = normalize_metric_map(
            {
                "source.bytes": {
                    "target": "target.kilobytes",
                    "unit_scale": 0.001,
                }
            }
        )
        result = resolve_metric_map("source.bytes", metric_map)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.applied)
        self.assertEqual(result.target, "source.bytes")
        self.assertEqual(result.klass, CLASS_REQUIRES_TRANSFORM)
        self.assertIn("unit_scale", result.gap_reason)

    def test_unmapped_returns_none(self):
        self.assertIsNone(resolve_metric_map("x", {}))


if __name__ == "__main__":
    unittest.main()
