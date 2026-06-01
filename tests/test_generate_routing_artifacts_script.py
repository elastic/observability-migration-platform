# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import tempfile
import unittest
from pathlib import Path

import yaml

from scripts import generate_routing_artifacts as script


class BuildRoutingArtifactsTests(unittest.TestCase):
    def test_builtin_mapping_otel_profile(self):
        artifacts = script.build_routing_artifacts(rules_files=[], schema_profile="otel")
        self.assertEqual(set(artifacts), {"otel-collector-migration.yaml"})
        statements = yaml.safe_load(artifacts["otel-collector-migration.yaml"])[
            "processors"
        ]["transform/grafana_to_elastic"]["metric_statements"][0]["statements"]
        self.assertIn('set(attributes["service.name"], attributes["job"])', statements)

    def test_rule_pack_label_rewrites_applied(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rules = Path(tmpdir) / "rules.yaml"
            rules.write_text(
                yaml.safe_dump({"query": {"label_rewrites": {"job": "service.custom"}}}),
                encoding="utf-8",
            )
            artifacts = script.build_routing_artifacts(
                rules_files=[str(rules)], schema_profile="prometheus_native"
            )
        relabel = yaml.safe_load(artifacts["prometheus-relabel.yaml"])
        targets = {
            cfg["target_label"]
            for cfg in relabel["remote_write"][0]["write_relabel_configs"]
        }
        self.assertIn("service_custom", targets)


class MainWritesFilesTests(unittest.TestCase):
    def test_main_writes_artifacts_to_out_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "routing"
            exit_code = script.main(["--out-dir", str(out_dir), "--schema-profile", "otel"])
            self.assertEqual(exit_code, 0)
            self.assertTrue((out_dir / "otel-collector-migration.yaml").exists())


if __name__ == "__main__":
    unittest.main()
