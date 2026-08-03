# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""CLI integration tests for the unified ``--metric-map-file`` surface."""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

import yaml

from observability_migration.adapters.source.datadog.cli import _load_configured_field_map
from observability_migration.adapters.source.grafana.cli import _load_configured_rule_pack


class MetricMapFileCliTests(unittest.TestCase):
    def test_grafana_metric_map_file_merges_into_rule_pack_and_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.yaml"
            metric_map = Path(tmp) / "metric-map.yaml"
            rules.write_text(
                yaml.safe_dump(
                    {
                        "query": {
                            "metric_map": {
                                "http_requests_total": "from.rules",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            metric_map.write_text(
                yaml.safe_dump(
                    {
                        "metric_map": {
                            "http_requests_total": "from.metric_map_file",
                            "container_memory_working_set_bytes": "container.memory.working_set",
                        }
                    }
                ),
                encoding="utf-8",
            )

            pack = _load_configured_rule_pack(
                argparse.Namespace(
                    rules_file=[str(rules)],
                    metric_map_file=[str(metric_map)],
                    logs_index="",
                    dataset_filter="",
                    logs_dataset_filter="",
                    plugin=[],
                )
            )

        self.assertEqual(pack.metric_map["http_requests_total"].target, "from.metric_map_file")
        self.assertEqual(
            pack.metric_map["container_memory_working_set_bytes"].target,
            "container.memory.working_set",
        )

    def test_datadog_metric_map_file_merges_into_field_profile_and_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.yaml"
            metric_map = Path(tmp) / "metric-map.yaml"
            profile.write_text(
                yaml.safe_dump(
                    {
                        "name": "test",
                        "metric_map": {
                            "system.cpu.user": "from.profile",
                        },
                    }
                ),
                encoding="utf-8",
            )
            metric_map.write_text(
                yaml.safe_dump(
                    {
                        "metric_map": {
                            "system.cpu.user": "from.metric_map_file",
                            "system.net.bytes_rcvd": {
                                "target": "system.network.in.bytes",
                                "transform": "to_rate",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            field_map = _load_configured_field_map(
                argparse.Namespace(
                    field_profile=str(profile),
                    metric_map_file=[str(metric_map)],
                    data_view="metrics-otel-*",
                    logs_index="",
                    dataset_filter="",
                    logs_dataset_filter="",
                )
            )

        self.assertEqual(field_map.metric_index, "metrics-otel-*")
        self.assertEqual(field_map.map_metric("system.cpu.user"), "from.metric_map_file")
        self.assertEqual(field_map.map_metric("system.net.bytes_rcvd"), "system.network.in.bytes")
        self.assertFalse(field_map.metric_map_gaps())
        self.assertTrue(field_map.metric_map_warnings())


if __name__ == "__main__":
    unittest.main()
