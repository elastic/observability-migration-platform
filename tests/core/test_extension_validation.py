"""Focused validation tests for extension inputs."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from observability_migration.adapters.source.datadog.field_map import load_profile
from observability_migration.adapters.source.grafana.rules import load_rule_pack_files


class TestExtensionValidation(unittest.TestCase):
    def test_grafana_rule_pack_rejects_invalid_regex(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rule-pack.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "query": {
                            "warning_patterns": [
                                {"pattern": "([", "reason": "broken regex"},
                            ]
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Invalid Grafana rule pack"):
                load_rule_pack_files([str(path)])

    def test_grafana_rule_pack_accepts_legacy_top_level_query_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rule-pack.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "default_rate_window": "10m",
                        "label_rewrites": {"cluster": "service.name"},
                        "ignored_labels": ["origin_prometheus"],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            rule_pack = load_rule_pack_files([str(path)])

        self.assertEqual(rule_pack.default_rate_window, "10m")
        self.assertEqual(rule_pack.label_rewrites["cluster"], "service.name")
        self.assertIn("origin_prometheus", rule_pack.ignored_labels)

    def test_datadog_profile_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "profile.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "name": "custom",
                        "metric_index": "metrics-*",
                        "extra_field": "unexpected",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Invalid Datadog field profile"):
                load_profile(str(path))

    def test_datadog_profile_rejects_blank_mapped_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "profile.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "name": "custom",
                        "metric_map": {"system.cpu.user": ""},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Invalid Datadog field profile"):
                load_profile(str(path))


if __name__ == "__main__":
    unittest.main()
