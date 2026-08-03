# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for source-neutral metric_map YAML files."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from observability_migration.core.metric_mapping.files import (
    load_metric_map_files,
    load_tag_map_files,
)


class MetricMapFileTests(unittest.TestCase):
    def test_loads_source_neutral_metric_map_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metric-map.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "metric_map": {
                            "container_memory_working_set_bytes": "container.memory.working_set",
                            "container_network_receive_bytes_total": {
                                "target": "k8s.pod.network.io",
                                "attribute_filter": {"network.direction": "receive"},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            entries = load_metric_map_files([str(path)])

        self.assertEqual(
            entries["container_memory_working_set_bytes"].target,
            "container.memory.working_set",
        )
        self.assertEqual(
            entries["container_network_receive_bytes_total"].attribute_filter,
            {"network.direction": "receive"},
        )

    def test_later_files_override_duplicate_metric_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.yaml"
            second = Path(tmp) / "second.yaml"
            first.write_text(yaml.safe_dump({"metric_map": {"metric.a": "target.first"}}), encoding="utf-8")
            second.write_text(yaml.safe_dump({"metric_map": {"metric.a": "target.second"}}), encoding="utf-8")

            entries = load_metric_map_files([str(first), str(second)])

        self.assertEqual(entries["metric.a"].target, "target.second")

    def test_rejects_files_without_top_level_metric_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text(yaml.safe_dump({"query": {"metric_map": {"a": "b"}}}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "top-level 'metric_map'"):
                load_metric_map_files([str(path)])

    def test_missing_file_raises_operator_friendly_value_error(self):
        missing = Path("/tmp/does-not-exist-metric-map.yaml")
        with self.assertRaisesRegex(ValueError, "not found or unreadable"):
            load_metric_map_files([str(missing)])

    def test_malformed_yaml_raises_operator_friendly_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("metric_map: [\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "YAML parse error"):
                load_metric_map_files([str(path)])


class TagMapFileTests(unittest.TestCase):
    def test_loads_tag_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.yaml"
            path.write_text(
                yaml.safe_dump({"tag_map": {"host": "host.name", "env": "deployment.environment"}}),
                encoding="utf-8",
            )
            tags = load_tag_map_files([str(path)])
        self.assertEqual(tags, {"host": "host.name", "env": "deployment.environment"})

    def test_tag_map_only_file_is_valid_for_both_loaders(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tags-only.yaml"
            path.write_text(yaml.safe_dump({"tag_map": {"host": "host.name"}}), encoding="utf-8")
            # metric loader tolerates a tag_map-only file (no metric_map required).
            self.assertEqual(load_metric_map_files([str(path)]), {})
            self.assertEqual(load_tag_map_files([str(path)]), {"host": "host.name"})

    def test_metric_map_only_file_yields_empty_tag_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics-only.yaml"
            path.write_text(yaml.safe_dump({"metric_map": {"a.b": "a.b.c"}}), encoding="utf-8")
            self.assertEqual(load_tag_map_files([str(path)]), {})

    def test_later_files_override_duplicate_tag_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.yaml"
            second = Path(tmp) / "second.yaml"
            first.write_text(yaml.safe_dump({"tag_map": {"host": "host.first"}}), encoding="utf-8")
            second.write_text(yaml.safe_dump({"tag_map": {"host": "host.second"}}), encoding="utf-8")
            self.assertEqual(load_tag_map_files([str(first), str(second)])["host"], "host.second")

    def test_rejects_file_with_neither_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text(yaml.safe_dump({"something_else": {"a": "b"}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "metric_map.*and/or.*tag_map|tag_map"):
                load_tag_map_files([str(path)])

    def test_rejects_non_string_tag_map_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text(yaml.safe_dump({"tag_map": {"host": {"nested": "x"}}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "'tag_map' must map string"):
                load_tag_map_files([str(path)])

    def test_rejects_empty_tag_map_keys_or_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text(yaml.safe_dump({"tag_map": {"": "host.name"}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-empty"):
                load_tag_map_files([str(path)])
            path.write_text(yaml.safe_dump({"tag_map": {"host": "  "}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-empty"):
                load_tag_map_files([str(path)])


if __name__ == "__main__":
    unittest.main()
