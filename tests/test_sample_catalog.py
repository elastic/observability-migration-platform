# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from observability_migration.sample_dashboards.catalog import (
    SampleDashboard,
    list_samples,
    resolve_input_dir,
)


class SampleCatalogTests(unittest.TestCase):
    def test_lists_at_least_one_sample_per_source(self):
        samples = list_samples()
        sources = {s.source for s in samples}
        self.assertIn("grafana", sources)
        self.assertIn("datadog", sources)

    def test_every_entry_resolves_to_a_parseable_dashboard(self):
        for sample in list_samples():
            self.assertIsInstance(sample, SampleDashboard)
            input_dir = resolve_input_dir(sample.id)
            self.assertTrue(input_dir.is_dir(), f"{sample.id} dir missing: {input_dir}")
            jsons = sorted(input_dir.glob("*.json"))
            self.assertTrue(jsons, f"{sample.id} has no dashboard JSON")
            for path in jsons:
                json.loads(path.read_text(encoding="utf-8"))

    def test_every_entry_declares_an_expected_unsupported_panel(self):
        for sample in list_samples():
            self.assertTrue(
                sample.expected_unsupported,
                f"{sample.id} must declare >=1 expected_unsupported panel",
            )

    def test_resolve_unknown_id_raises_keyerror(self):
        with self.assertRaises(KeyError):
            resolve_input_dir("does-not-exist")

    def test_catalog_dir_is_importable_package_data(self):
        # Resolves via importlib.resources (package data), not a repo-relative
        # path, so it works from an installed wheel.
        from importlib import resources

        root = resources.files("observability_migration.sample_dashboards")
        for sample in list_samples():
            entry = root.joinpath(sample.relative_dir)
            self.assertTrue(entry.is_dir(), f"missing packaged dir for {sample.id}")


class SampleMigrationSmokeTests(unittest.TestCase):
    def test_grafana_sample_migrates_offline_and_flags_unsupported_panel(self):
        from observability_migration.app import cli

        sample_id = "grafana-prom-basics"
        input_dir = resolve_input_dir(sample_id)
        unsupported_title = next(
            s for s in list_samples() if s.id == sample_id
        ).expected_unsupported[0]

        with tempfile.TemporaryDirectory() as out:
            try:
                cli.main([
                    "migrate",
                    "--source", "grafana",
                    "--input-mode", "files",
                    "--input-dir", str(input_dir),
                    "--output-dir", out,
                    "--assets", "dashboards",
                ])
            except SystemExit as exc:
                self.assertIn(exc.code, (0, None), f"migrate exited non-zero: {exc.code}")
            manifest = Path(out) / "dashboards" / "migration_manifest.json"
            self.assertTrue(manifest.is_file(), f"missing manifest: {manifest}")
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            panels: list[dict] = []
            for dashboard in manifest_data.get("dashboards", []):
                panels.extend(dashboard.get("panels", []))
            if not panels:
                panels = manifest_data.get("panels", [])
            matching = [p for p in panels if p.get("title") == unsupported_title]
            self.assertTrue(
                matching,
                f"no panel titled {unsupported_title!r} in manifest",
            )
            for panel in matching:
                self.assertIn(
                    panel["status"],
                    ("not_feasible", "requires_manual"),
                    f"expected degrade status for {unsupported_title!r}, got {panel['status']!r}",
                )
                reasons = panel.get("reasons", [])
                self.assertTrue(
                    reasons,
                    f"expected non-empty reasons for {unsupported_title!r}",
                )


if __name__ == "__main__":
    unittest.main()
