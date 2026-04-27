# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for example extension artifacts shipped with the repo."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import yaml

from observability_migration.adapters.source.datadog.field_map import load_profile
from observability_migration.adapters.source.datadog.rules import (
    build_extension_template as build_datadog_extension_template,
)
from observability_migration.adapters.source.grafana.rules import load_rule_pack_files
from observability_migration.core.interfaces import source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"


class _FakeRulePack:
    def __init__(self) -> None:
        self.label_candidates: dict[str, list[str]] = {}


class _FakeRegistry:
    def __init__(self) -> None:
        self.registered: list[tuple[str, int]] = []

    def register(self, name: str, priority: int = 100):
        def decorator(fn):
            self.registered.append((name, priority))
            return fn

        return decorator


class TestExtensionExamples(unittest.TestCase):
    def test_grafana_rule_pack_example_loads(self):
        example_path = EXAMPLES_DIR / "rule-pack.example.yaml"

        rule_pack = load_rule_pack_files([str(example_path)])

        self.assertEqual(rule_pack.default_rate_window, "5m")
        self.assertEqual(rule_pack.label_rewrites["cluster"], "service.name")
        self.assertIn("canvas", rule_pack.skip_panel_types)
        self.assertIn("service.instance.id", rule_pack.control_field_overrides.values())

    def test_grafana_plugin_example_registers_against_minimal_api(self):
        example_path = EXAMPLES_DIR / "plugin_example.py"
        spec = importlib.util.spec_from_file_location("plugin_example", example_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        fake_rule_pack = _FakeRulePack()
        fake_registry = _FakeRegistry()
        api = {
            "rule_pack": fake_rule_pack,
            "query_preprocessors": fake_registry,
            "append_unique": lambda bucket, value: bucket.append(value) if value not in bucket else None,
        }

        module.register(api)

        self.assertEqual(fake_rule_pack.label_candidates["cluster"][0], "k8s.cluster.name")
        self.assertIn(("unwrap_abs", 15), fake_registry.registered)

    def test_datadog_field_profile_example_loads(self):
        example_path = EXAMPLES_DIR / "datadog-field-profile.example.yaml"

        profile = load_profile(str(example_path))

        self.assertEqual(profile.name, "custom_otel_serverless")
        self.assertEqual(profile.metric_index, "metrics-*")
        self.assertEqual(profile.map_metric("system.cpu.user"), "system.cpu.user.pct")
        self.assertEqual(profile.map_tag("host"), "host.name")
        self.assertEqual(profile.map_tag("service"), "service.name")

    def test_datadog_field_profile_example_matches_template_keys(self):
        example_path = EXAMPLES_DIR / "datadog-field-profile.example.yaml"
        raw = yaml.safe_load(example_path.read_text(encoding="utf-8"))

        template = build_datadog_extension_template()

        self.assertTrue(set(template).issubset(raw))

    def test_extension_catalog_example_paths_exist(self):
        import observability_migration.adapters.source.datadog.adapter
        import observability_migration.adapters.source.grafana.adapter  # noqa: F401

        for source_name in ("grafana", "datadog"):
            adapter = source_registry.get(source_name)()
            catalog = adapter.build_extension_catalog()
            for surface in catalog["current_surfaces"] + catalog["planned_surfaces"]:
                example_path = surface.get("example_path", "")
                if not example_path:
                    continue
                self.assertTrue((REPO_ROOT / example_path).exists(), example_path)

    def test_cue_starter_examples_exist(self):
        self.assertTrue((EXAMPLES_DIR / "cue" / "datadog-field-profile.cue").exists())
        self.assertTrue((EXAMPLES_DIR / "cue" / "grafana-rule-pack.cue").exists())


if __name__ == "__main__":
    unittest.main()
