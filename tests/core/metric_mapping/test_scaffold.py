# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for metric_map scaffold helpers."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from observability_migration.core.metric_mapping.scaffold import (
    build_scaffold_yaml,
    collect_unmapped_source_metrics,
)


class MetricMapScaffoldTests(unittest.TestCase):
    def test_collects_unmapped_metrics_and_skips_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            contract = {
                "required_fields": {
                    "mapped.target": {
                        "target_field": "mapped.target",
                        "source_fields": ["source.mapped"],
                        "roles": ["metric"],
                        "mapped_from": "source.mapped",
                    },
                    "unmapped.metric": {
                        "target_field": "unmapped.metric",
                        "source_fields": ["unmapped.metric"],
                        "roles": ["metric"],
                    },
                },
                "metric_map": {
                    "applied": [{"source": "source.mapped", "target": "mapped.target"}],
                    "gaps": [],
                    "warnings": [],
                    "totals": {"applied": 1, "gaps": 0, "warnings": 0},
                },
            }
            (base / "required_target_contract.json").write_text(
                json.dumps(contract),
                encoding="utf-8",
            )

            unmapped = collect_unmapped_source_metrics(base)
            self.assertEqual(unmapped, ["unmapped.metric"])

            payload = yaml.safe_load(build_scaffold_yaml(base))
            self.assertEqual(payload["metric_map"]["unmapped.metric"]["target"], "")
            self.assertEqual(payload["metric_map"]["unmapped.metric"]["provenance"], "scaffold")

            from observability_migration.core.metric_mapping import (
                normalize_metric_map,
                resolve_metric_map,
            )

            metric_map = normalize_metric_map(payload["metric_map"])
            result = resolve_metric_map("unmapped.metric", metric_map)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertFalse(result.applied)
            self.assertIn("scaffold placeholder", result.gap_reason)

    def test_recording_rule_scaffold_gets_drop_rate_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            contract = {
                "required_fields": {
                    "job:http_requests:rate5m": {
                        "target_field": "job:http_requests:rate5m",
                        "source_fields": ["job:http_requests:rate5m"],
                        "roles": ["metric"],
                    },
                    "plain_metric": {
                        "target_field": "plain_metric",
                        "source_fields": ["plain_metric"],
                        "roles": ["metric"],
                    },
                },
            }
            (base / "required_target_contract.json").write_text(
                json.dumps(contract),
                encoding="utf-8",
            )

            scaffold_text = build_scaffold_yaml(base)
            self.assertIn("Recording-rule-style names", scaffold_text)
            self.assertIn("job:http_requests:rate5m", scaffold_text)
            payload = yaml.safe_load(scaffold_text)
            self.assertEqual(
                payload["metric_map"]["job:http_requests:rate5m"]["transform"],
                "drop_rate",
            )
            self.assertNotIn("transform", payload["metric_map"]["plain_metric"])


if __name__ == "__main__":
    unittest.main()
