# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""--metric-map-file tag_map wiring for Datadog and Grafana."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from observability_migration.core.metric_mapping import (
    load_metric_map_files,
    load_tag_map_files,
)


def _write(payload: dict) -> str:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "map.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return str(path)


class DatadogTagMapFileTests(unittest.TestCase):
    def test_merge_tag_map_overrides_profile_tag(self):
        import copy

        from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE

        profile = copy.deepcopy(OTEL_PROFILE)
        path = _write({"tag_map": {"custom_tag": "service.node.name"}})
        profile.merge_metric_map(load_metric_map_files([path]))
        profile.merge_tag_map(load_tag_map_files([path]))
        self.assertEqual(profile.map_tag("custom_tag", context="metric"), "service.node.name")


class GrafanaTagMapFileTests(unittest.TestCase):
    def test_tag_map_populates_label_rewrites(self):
        from observability_migration.adapters.source.grafana.rules import RulePackConfig

        rule_pack = RulePackConfig()
        path = _write({"metric_map": {"m": "m2"}, "tag_map": {"instance": "host.name"}})
        rule_pack.metric_map.update(load_metric_map_files([path]))
        rule_pack.label_rewrites.update(load_tag_map_files([path]))
        self.assertEqual(rule_pack.label_rewrites["instance"], "host.name")


class DatadogCandidateFieldsTests(unittest.TestCase):
    def test_candidate_fields_accepts_metric_map_entry_objects(self):
        """After --metric-map-file, metric_map values are MetricMapEntry, not str."""
        import copy

        from observability_migration.adapters.source.datadog.cli import (
            _DatadogValidationResolver,
        )
        from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
        from observability_migration.core.metric_mapping import MetricMapEntry

        profile = copy.deepcopy(OTEL_PROFILE)
        profile.metric_map["system.cpu.user"] = MetricMapEntry(target="k8s.node.cpu.usage")
        resolver = _DatadogValidationResolver(profile, "metrics-*")
        candidates = resolver._candidate_fields("system.cpu.user")
        self.assertTrue(all(isinstance(c, str) for c in candidates), candidates)
        self.assertIn("k8s.node.cpu.usage", candidates)


if __name__ == "__main__":
    unittest.main()
