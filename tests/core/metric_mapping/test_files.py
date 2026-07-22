# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for source-neutral metric_map YAML files."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from observability_migration.core.metric_mapping.files import load_metric_map_files


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


if __name__ == "__main__":
    unittest.main()
