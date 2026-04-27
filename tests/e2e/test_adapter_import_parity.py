# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""End-to-end adapter import parity tests."""

import subprocess
import sys
import unittest


def _run_module_help(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
    )


class TestGrafanaImportParity(unittest.TestCase):
    def test_extract_canonical(self):
        from observability_migration.adapters.source.grafana.extract import extract_dashboards_from_files
        self.assertTrue(callable(extract_dashboards_from_files))

    def test_translate_canonical(self):
        from observability_migration.adapters.source.grafana.translate import TranslationContext
        self.assertTrue(TranslationContext is not None)

    def test_alerts_canonical(self):
        from observability_migration.adapters.source.grafana.alerts import extract_alerts_from_dashboard
        self.assertTrue(callable(extract_alerts_from_dashboard))

    def test_panels_canonical(self):
        from observability_migration.adapters.source.grafana.panels import translate_panel
        self.assertTrue(callable(translate_panel))

    def test_promql_canonical(self):
        from observability_migration.adapters.source.grafana.promql import preprocess_grafana_macros
        self.assertTrue(callable(preprocess_grafana_macros))


class TestDatadogImportParity(unittest.TestCase):
    def test_normalize_canonical(self):
        from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
        self.assertTrue(callable(normalize_dashboard))

    def test_translate_canonical(self):
        from observability_migration.adapters.source.datadog.translate import translate_widget
        self.assertTrue(callable(translate_widget))

    def test_metric_parser_canonical(self):
        from observability_migration.adapters.source.datadog.query_parser import parse_metric_query
        self.assertTrue(callable(parse_metric_query))

    def test_models_canonical(self):
        from observability_migration.adapters.source.datadog.models import NormalizedWidget
        self.assertTrue(NormalizedWidget is not None)

    def test_field_map_canonical(self):
        from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
        self.assertTrue(OTEL_PROFILE is not None)


class TestTargetImportParity(unittest.TestCase):
    def test_compile_via_shared_path(self):
        from observability_migration.targets.kibana.compile import compile_yaml
        self.assertTrue(callable(compile_yaml))

    def test_comparators_via_shared_path(self):
        from observability_migration.core.verification.comparators import ComparisonResult
        self.assertTrue(ComparisonResult is not None)

    def test_report_canonical(self):
        from observability_migration.core.reporting.report import MigrationResult
        self.assertTrue(MigrationResult is not None)

    def test_visual_ir_canonical(self):
        from observability_migration.core.assets.visual import VisualIR
        self.assertTrue(VisualIR is not None)

    def test_query_ir_canonical(self):
        from observability_migration.core.assets.query import QueryIR
        self.assertTrue(QueryIR is not None)

    def test_operational_ir_canonical(self):
        from observability_migration.core.assets.operational import OperationalIR
        self.assertTrue(OperationalIR is not None)


class TestModuleEntrypoints(unittest.TestCase):
    def test_app_cli_migrate_module_help_executes_main(self):
        proc = _run_module_help("-m", "observability_migration.app.cli", "migrate", "--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--assets {dashboards,alerts,all}", proc.stdout)
        self.assertIn("--source", proc.stdout)
        self.assertIn("Deprecated compatibility alias", proc.stdout)
        self.assertIn("alert-capable asset selection", proc.stdout)
        self.assertIn("--assets alerts", proc.stdout)
        self.assertIn("--assets all", proc.stdout)
        self.assertNotIn("After --fetch-alerts", proc.stdout)
        self.assertNotIn("Requires --fetch-alerts", proc.stdout)

    def test_app_cli_cluster_module_help_executes_main(self):
        proc = _run_module_help("-m", "observability_migration.app.cli", "cluster", "--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ensure-data-views", proc.stdout)
        self.assertIn("list-dashboards", proc.stdout)
        self.assertNotIn("--assets", proc.stdout)

    def test_grafana_cli_module_help_executes_main(self):
        proc = _run_module_help("-m", "observability_migration.adapters.source.grafana.cli", "--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Grafana", proc.stdout)
        self.assertIn("--assets {dashboards,alerts,all}", proc.stdout)
        self.assertIn("--fetch-alerts", proc.stdout)
        self.assertIn("Deprecated compatibility alias", proc.stdout)
        self.assertIn("alert-capable asset selection", proc.stdout)
        self.assertIn("--assets alerts", proc.stdout)
        self.assertIn("--assets all", proc.stdout)
        self.assertNotIn("Requires --fetch-alerts", proc.stdout)

    def test_datadog_cli_module_help_executes_main(self):
        proc = _run_module_help("-m", "observability_migration.adapters.source.datadog.cli", "--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Datadog", proc.stdout)
        self.assertIn("--assets {dashboards,alerts,all}", proc.stdout)
        self.assertIn("--fetch-monitors", proc.stdout)
        self.assertIn("Deprecated compatibility alias", proc.stdout)
        self.assertIn("alert-capable asset selection", proc.stdout)
        self.assertIn("--assets alerts", proc.stdout)
        self.assertIn("--assets all", proc.stdout)
        self.assertNotIn("Requires --fetch-monitors", proc.stdout)


if __name__ == "__main__":
    unittest.main()
