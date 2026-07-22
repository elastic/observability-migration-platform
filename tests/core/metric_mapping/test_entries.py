# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for shared metric_mapping building blocks."""

from __future__ import annotations

import unittest

from observability_migration.core.metric_mapping import (
    CLASS_EXACT,
    CLASS_NONE,
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

    def test_parse_variant_entry_without_top_level_target(self):
        entry = parse_metric_map_entry(
            {
                "variants": [
                    {
                        "target": "k8s.pod.network.io",
                        "source_filter": {"direction": "receive"},
                        "attribute_filter": {"network.direction": "receive"},
                    },
                    {
                        "target": "k8s.pod.network.io",
                        "source_filter": {"direction": "transmit"},
                        "attribute_filter": {"network.direction": "transmit"},
                    },
                ]
            },
            source_key="net",
        )
        self.assertEqual(entry.target, "")
        self.assertEqual(len(entry.variants), 2)

    def test_reject_unknown_keys(self):
        with self.assertRaises(ValueError):
            parse_metric_map_entry({"target": "x", "extra": 1}, source_key="a")

    def test_reject_empty_target(self):
        with self.assertRaises(ValueError):
            MetricMapEntry(target="")

    def test_reject_empty_target_without_variants(self):
        with self.assertRaises(ValueError):
            parse_metric_map_entry({"target": ""}, source_key="a")

    def test_scaffold_allows_empty_target(self):
        entry = parse_metric_map_entry(
            {"target": "", "provenance": "scaffold"},
            source_key="a",
        )
        self.assertEqual(entry.target, "")
        self.assertEqual(entry.provenance, "scaffold")

    def test_resolve_scaffold_placeholder_is_gap(self):
        metric_map = normalize_metric_map(
            {"source.metric": {"target": "", "provenance": "scaffold"}}
        )
        result = resolve_metric_map("source.metric", metric_map)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.applied)
        self.assertEqual(result.klass, CLASS_NONE)
        self.assertIn("scaffold placeholder", result.gap_reason)

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
        self.assertFalse(result.is_gap)

    def test_resolve_class2_applies_with_warnings(self):
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
        self.assertTrue(result.applied)
        self.assertEqual(result.target, "k8s.pod.network.io")
        self.assertEqual(result.klass, CLASS_REQUIRES_TRANSFORM)
        self.assertEqual(result.gap_reason, "")
        self.assertFalse(result.is_gap)
        self.assertTrue(any("attribute_filter" in warning for warning in result.warnings))

    def test_unit_scale_applies_class2(self):
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
        self.assertTrue(result.applied)
        self.assertEqual(result.target, "target.kilobytes")
        self.assertEqual(result.klass, CLASS_REQUIRES_TRANSFORM)
        self.assertEqual(result.gap_reason, "")
        self.assertTrue(any("unit_scale" in warning for warning in result.warnings))

    def test_resolve_variant_by_source_labels(self):
        metric_map = normalize_metric_map(
            {
                "net_bytes": {
                    "variants": [
                        {
                            "target": "k8s.pod.network.io",
                            "source_filter": {"direction": "receive"},
                            "attribute_filter": {"network.direction": "receive"},
                        },
                        {
                            "target": "k8s.pod.network.io",
                            "source_filter": {"direction": "transmit"},
                            "attribute_filter": {"network.direction": "transmit"},
                        },
                    ]
                }
            }
        )
        receive = resolve_metric_map("net_bytes", metric_map, {"direction": "receive"})
        assert receive is not None
        self.assertTrue(receive.applied)
        self.assertEqual(receive.target, "k8s.pod.network.io")
        self.assertEqual(receive.entry.attribute_filter, {"network.direction": "receive"})

        transmit = resolve_metric_map("net_bytes", metric_map, {"direction": "transmit"})
        assert transmit is not None
        self.assertEqual(transmit.entry.attribute_filter, {"network.direction": "transmit"})

    def test_resolve_variant_empty_source_filter_matches_anything(self):
        metric_map = normalize_metric_map(
            {
                "metric": {
                    "variants": [
                        {"target": "default.target"},
                        {
                            "target": "special.target",
                            "source_filter": {"env": "prod"},
                        },
                    ]
                }
            }
        )
        result = resolve_metric_map("metric", metric_map, {"env": "staging"})
        assert result is not None
        self.assertTrue(result.applied)
        self.assertEqual(result.target, "default.target")

    def test_resolve_variant_no_match_is_gap(self):
        metric_map = normalize_metric_map(
            {
                "metric": {
                    "variants": [
                        {
                            "target": "special.target",
                            "source_filter": {"env": "prod"},
                        }
                    ]
                }
            }
        )
        result = resolve_metric_map("metric", metric_map, {"env": "staging"})
        assert result is not None
        self.assertFalse(result.applied)
        self.assertEqual(result.target, "metric")
        self.assertIn("none matched", result.gap_reason)

    def test_unmapped_returns_none(self):
        self.assertIsNone(resolve_metric_map("x", {}))


if __name__ == "__main__":
    unittest.main()
