# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from observability_migration.app import cli as app_cli


class TestUnifiedCliRouting(unittest.TestCase):
    def test_migrate_parser_defaults_field_profile_to_otel(self):
        parser = app_cli._build_parser()

        args = parser.parse_args(["migrate", "--source", "grafana"])

        self.assertEqual(args.field_profile, "otel")

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_forwards_es_capability_flags(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/datadog/dashboards",
            output_dir="datadog_migration_output",
            data_view="metrics-*",
            field_profile="otel",
            logs_index="logs-*",
            compile=True,
            validate=True,
            upload=False,
            preflight=True,
            es_url="https://example.es",
            es_api_key="secret",
            kibana_url="",
            kibana_api_key="",
            space_id="",
            dataset_filter="",
            logs_dataset_filter="",
            smoke=False,
            browser_audit=False,
            capture_screenshots=False,
            smoke_output="",
            smoke_timeout=30,
            chrome_binary="",
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_datadog_migration(args)

            self.assertEqual(
                sys.argv,
                [
                    "obs-migrate",
                    "--source", "files",
                    "--input-dir", "infra/datadog/dashboards",
                    "--output-dir", "datadog_migration_output",
                    "--data-view", "metrics-*",
                    "--field-profile", "otel",
                    "--logs-index", "logs-*",
                    "--es-url", "https://example.es",
                    "--es-api-key", "secret",
                    "--compile",
                    "--validate",
                    "--preflight",
                ],
            )
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    def _make_grafana_args(self, **overrides):
        defaults = dict(
            input_mode="files",
            input_dir="infra/grafana/dashboards",
            output_dir="migration_output",
            data_view="metrics-*",
            field_profile="otel",
            assets="dashboards",
            esql_index="metrics-*",
            logs_index="",
            native_promql_flag="auto",
            fetch_alerts=False,
            create_alert_rules=False,
            grafana_token="",
            validate=False,
            upload=False,
            preflight=False,
            es_url="",
            es_api_key="",
            kibana_url="",
            kibana_api_key="",
            space_id="",
            rules_file=[],
            plugin=[],
            polish_metadata=False,
            dataset_filter="",
            logs_dataset_filter="",
            smoke_report="",
            smoke=False,
            browser_audit=False,
            capture_screenshots=False,
            smoke_output="",
            smoke_timeout=30,
            chrome_binary="",
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_forwards_field_profile(self, mock_main):
        args = self._make_grafana_args(native_promql_flag="force_on")
        original_argv = list(sys.argv)

        try:
            app_cli._run_grafana_migration(args)

            self.assertIn("--field-profile", sys.argv)
            self.assertIn("otel", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_forwards_force_on_native_promql(self, mock_main):
        args = self._make_grafana_args(native_promql_flag="force_on")
        original_argv = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            self.assertIn("--native-promql", sys.argv)
            self.assertNotIn("--no-native-promql", sys.argv)
        finally:
            sys.argv = original_argv

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_forwards_force_off_native_promql(self, mock_main):
        args = self._make_grafana_args(native_promql_flag="force_off")
        original_argv = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            self.assertIn("--no-native-promql", sys.argv)
            self.assertNotIn("--native-promql", sys.argv)
        finally:
            sys.argv = original_argv

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_auto_does_not_forward_native_promql_flag(self, mock_main):
        args = self._make_grafana_args(native_promql_flag="auto")
        original_argv = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            self.assertNotIn("--native-promql", sys.argv)
            self.assertNotIn("--no-native-promql", sys.argv)
        finally:
            sys.argv = original_argv

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_forwards_upload_flags(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/datadog/dashboards",
            output_dir="datadog_migration_output",
            data_view="metrics-*",
            field_profile="otel",
            logs_index="",
            compile=False,
            validate=False,
            upload=True,
            preflight=False,
            es_url="",
            es_api_key="",
            kibana_url="https://kibana.example",
            kibana_api_key="secret-kb",
            space_id="shadow",
            dataset_filter="",
            logs_dataset_filter="",
            smoke=False,
            browser_audit=False,
            capture_screenshots=False,
            smoke_output="",
            smoke_timeout=30,
            chrome_binary="",
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_datadog_migration(args)

            self.assertIn("--upload", sys.argv)
            self.assertIn("--kibana-url", sys.argv)
            self.assertIn("https://kibana.example", sys.argv)
            self.assertIn("--kibana-api-key", sys.argv)
            self.assertIn("secret-kb", sys.argv)
            self.assertIn("--space-id", sys.argv)
            self.assertIn("shadow", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_forwards_dataset_filter_flags(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/datadog/dashboards",
            output_dir="datadog_migration_output",
            data_view="metrics-otel-default",
            field_profile="otel",
            logs_index="",
            compile=False,
            validate=False,
            upload=False,
            preflight=False,
            es_url="",
            es_api_key="",
            kibana_url="",
            kibana_api_key="",
            space_id="",
            dataset_filter="otel",
            logs_dataset_filter="generic",
            smoke=False,
            browser_audit=False,
            capture_screenshots=False,
            smoke_output="",
            smoke_timeout=30,
            chrome_binary="",
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_datadog_migration(args)

            self.assertIn("--dataset-filter", sys.argv)
            self.assertIn("otel", sys.argv)
            self.assertIn("--logs-dataset-filter", sys.argv)
            self.assertIn("generic", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.app.cli.target_registry.get")
    def test_run_compile_uses_registered_target_adapter(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = Path(tmpdir) / "yaml"
            output_dir = Path(tmpdir) / "compiled"
            yaml_dir.mkdir(parents=True, exist_ok=True)
            (yaml_dir / "dash.yaml").write_text("dashboards: []", encoding="utf-8")
            adapter = mock.Mock()
            adapter.compile.return_value = {
                "yaml_lint": {"ok": True, "output": ""},
                "compile_results": [{"name": "dash.yaml", "success": True, "output": ""}],
                "summary": {"compiled_ok": 1, "total": 1},
                "layout": {"ok": True, "output": ""},
            }
            mock_get.return_value = mock.Mock(return_value=adapter)

            with redirect_stdout(io.StringIO()):
                app_cli._run_compile(SimpleNamespace(yaml_dir=str(yaml_dir), output_dir=str(output_dir)))

        adapter.compile.assert_called_once()

    @patch("observability_migration.app.cli.target_registry.get")
    def test_run_upload_uses_registered_target_adapter(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            compiled_dir = Path(tmpdir)
            adapter = mock.Mock()
            adapter.upload.return_value = {
                "summary": {"uploaded_ok": 1, "total": 1},
                "records": [{"yaml_file": "dash.yaml", "success": True, "output": ""}],
            }
            mock_get.return_value = mock.Mock(return_value=adapter)

            with redirect_stdout(io.StringIO()):
                app_cli._run_upload(
                    SimpleNamespace(
                        compiled_dir=str(compiled_dir),
                        kibana_url="https://kibana.example",
                        kibana_api_key="secret",
                        space_id="shadow",
                    )
                )

        adapter.upload.assert_called_once()

    def test_upload_help_describes_split_dashboard_artifact_shapes(self):
        parser = app_cli._build_parser()

        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(stdout):
            parser.parse_args(["upload", "--help"])

        self.assertEqual(ctx.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("migration_output/dashboards", help_text)
        self.assertIn("migration_output/dashboards/yaml", help_text)
        self.assertIn("migration_output/dashboards/compiled", help_text)
        self.assertNotIn("single .yaml file", help_text)
        self.assertNotIn("migration_output/yaml", help_text)

    @patch("observability_migration.app.cli.target_registry.get")
    def test_run_upload_empty_records_describes_split_dashboard_inputs(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = Path(tmpdir)
            adapter = mock.Mock()
            adapter.upload.return_value = {
                "summary": {"uploaded_ok": 0, "total": 0},
                "records": [],
            }
            mock_get.return_value = mock.Mock(return_value=adapter)

            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as ctx, redirect_stderr(stderr):
                app_cli._run_upload(
                    SimpleNamespace(
                        yaml_dir=str(yaml_dir),
                        compiled_dir=None,
                        kibana_url="https://kibana.example",
                        kibana_api_key="secret",
                        space_id="shadow",
                    )
                )

        self.assertEqual(ctx.exception.code, 1)
        error_text = stderr.getvalue()
        self.assertIn("migration_output/dashboards", error_text)
        self.assertIn("migration_output/dashboards/yaml", error_text)
        self.assertIn("migration_output/dashboards/compiled", error_text)
        self.assertNotIn("a .yaml file", error_text)
        self.assertNotIn("migration_output/yaml", error_text)

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_forwards_smoke_flags(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/datadog/dashboards",
            output_dir="datadog_migration_output",
            data_view="metrics-*",
            field_profile="otel",
            logs_index="",
            compile=False,
            validate=False,
            upload=False,
            preflight=False,
            es_url="https://example.es",
            es_api_key="secret",
            kibana_url="https://kibana.example",
            kibana_api_key="secret-kb",
            space_id="shadow",
            dataset_filter="",
            logs_dataset_filter="",
            smoke=True,
            browser_audit=True,
            capture_screenshots=True,
            smoke_output="out/smoke.json",
            smoke_timeout=45,
            chrome_binary="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_datadog_migration(args)

            self.assertIn("--smoke", sys.argv)
            self.assertIn("--browser-audit", sys.argv)
            self.assertIn("--capture-screenshots", sys.argv)
            self.assertIn("--smoke-output", sys.argv)
            self.assertIn("out/smoke.json", sys.argv)
            self.assertIn("--smoke-timeout", sys.argv)
            self.assertIn("45", sys.argv)
            self.assertIn("--chrome-binary", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_forwards_smoke_flags(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/grafana/dashboards",
            output_dir="migration_output",
            data_view="metrics-*",
            esql_index="metrics-*",
            logs_index="logs-*",
            native_promql_flag="auto",
            validate=False,
            upload=False,
            es_url="https://example.es",
            es_api_key="secret",
            kibana_url="https://kibana.example",
            kibana_api_key="secret-kb",
            rules_file=[],
            plugin=[],
            polish_metadata=False,
            preflight=False,
            dataset_filter="",
            logs_dataset_filter="",
            smoke_report="",
            smoke=True,
            browser_audit=True,
            capture_screenshots=True,
            smoke_output="out/smoke.json",
            smoke_timeout=45,
            chrome_binary="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_grafana_migration(args)

            self.assertIn("--smoke", sys.argv)
            self.assertIn("--browser-audit", sys.argv)
            self.assertIn("--capture-screenshots", sys.argv)
            self.assertIn("--smoke-output", sys.argv)
            self.assertIn("out/smoke.json", sys.argv)
            self.assertIn("--smoke-timeout", sys.argv)
            self.assertIn("45", sys.argv)
            self.assertIn("--chrome-binary", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_forwards_assets_flag(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/datadog/dashboards",
            output_dir="datadog_migration_output",
            data_view="metrics-*",
            field_profile="otel",
            logs_index="",
            compile=False,
            validate=False,
            upload=False,
            preflight=False,
            es_url="",
            es_api_key="",
            kibana_url="",
            kibana_api_key="",
            space_id="",
            dataset_filter="",
            logs_dataset_filter="",
            smoke=False,
            browser_audit=False,
            capture_screenshots=False,
            smoke_output="",
            smoke_timeout=30,
            chrome_binary="",
            assets="alerts",
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_datadog_migration(args)

            self.assertIn("--assets", sys.argv)
            self.assertIn("alerts", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_normalizes_legacy_alert_alias_to_assets_all(self, mock_main):
        parser = app_cli._build_parser()
        args = parser.parse_args(["migrate", "--source", "datadog", "--fetch-alerts"])
        original_argv = list(sys.argv)

        try:
            with self.assertWarnsRegex(
                FutureWarning,
                "--fetch-alerts/--fetch-monitors are deprecated",
            ):
                app_cli._run_datadog_migration(args)

            asset_index = sys.argv.index("--assets")
            self.assertEqual(sys.argv[asset_index + 1], "all")
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_forwards_assets_flag(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/grafana/dashboards",
            output_dir="migration_output",
            data_view="metrics-*",
            esql_index="",
            logs_index="",
            native_promql_flag="auto",
            validate=False,
            upload=False,
            es_url="",
            es_api_key="",
            kibana_url="",
            kibana_api_key="",
            rules_file=[],
            plugin=[],
            polish_metadata=False,
            preflight=False,
            dataset_filter="",
            logs_dataset_filter="",
            smoke_report="",
            smoke=False,
            browser_audit=False,
            capture_screenshots=False,
            smoke_output="",
            smoke_timeout=30,
            chrome_binary="",
            assets="all",
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_grafana_migration(args)

            self.assertIn("--assets", sys.argv)
            self.assertIn("all", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_forwards_space_id_as_shadow_space(self, mock_main):
        parser = app_cli._build_parser()
        args = parser.parse_args(
            ["migrate", "--source", "grafana", "--space-id", "shadow-space"]
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_grafana_migration(args)

            self.assertIn("--shadow-space", sys.argv)
            self.assertIn("shadow-space", sys.argv)
            self.assertNotIn("--space-id", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_normalizes_legacy_alert_alias_to_assets_all(self, mock_main):
        parser = app_cli._build_parser()
        args = parser.parse_args(["migrate", "--source", "grafana", "--fetch-alerts"])
        original_argv = list(sys.argv)

        try:
            with self.assertWarnsRegex(
                FutureWarning,
                "--fetch-alerts/--fetch-monitors are deprecated",
            ):
                app_cli._run_grafana_migration(args)

            asset_index = sys.argv.index("--assets")
            self.assertEqual(sys.argv[asset_index + 1], "all")
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_does_not_forward_include_flag(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/datadog/dashboards",
            output_dir="datadog_migration_output",
            data_view="metrics-*",
            field_profile="otel",
            logs_index="",
            compile=False,
            validate=False,
            upload=False,
            preflight=False,
            es_url="",
            es_api_key="",
            kibana_url="",
            kibana_api_key="",
            space_id="",
            dataset_filter="",
            logs_dataset_filter="",
            smoke=False,
            browser_audit=False,
            capture_screenshots=False,
            smoke_output="",
            smoke_timeout=30,
            chrome_binary="",
            include="dashboards,monitors",
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_datadog_migration(args)
            self.assertNotIn("--include", sys.argv)
            self.assertNotIn("dashboards,monitors", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_does_not_forward_include_flag(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/grafana/dashboards",
            output_dir="migration_output",
            data_view="metrics-*",
            esql_index="metrics-*",
            logs_index="",
            native_promql_flag="auto",
            validate=False,
            upload=False,
            es_url="",
            es_api_key="",
            kibana_url="",
            kibana_api_key="",
            rules_file=[],
            plugin=[],
            polish_metadata=False,
            preflight=False,
            dataset_filter="",
            logs_dataset_filter="",
            smoke_report="",
            smoke=False,
            browser_audit=False,
            capture_screenshots=False,
            smoke_output="",
            smoke_timeout=30,
            chrome_binary="",
            include="dashboards,alerts",
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_grafana_migration(args)
            self.assertNotIn("--include", sys.argv)
            self.assertNotIn("dashboards,alerts", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    def test_run_extensions_prints_grafana_catalog_json(self):
        args = SimpleNamespace(source="grafana", format="json", template_only=False, template_out="")

        with redirect_stdout(io.StringIO()) as stdout:
            app_cli._run_extensions(args)

        output = stdout.getvalue()
        self.assertIn('"adapter": "grafana"', output)
        self.assertIn('"current_surfaces"', output)

    def test_run_extensions_prints_datadog_catalog_yaml(self):
        args = SimpleNamespace(source="datadog", format="yaml", template_only=False, template_out="")

        with redirect_stdout(io.StringIO()) as stdout:
            app_cli._run_extensions(args)

        output = stdout.getvalue()
        self.assertIn("adapter: datadog", output)
        self.assertIn("current_surfaces:", output)

    def test_run_extensions_prints_template_only(self):
        args = SimpleNamespace(source="datadog", format="yaml", template_only=True, template_out="")

        with redirect_stdout(io.StringIO()) as stdout:
            app_cli._run_extensions(args)

        output = stdout.getvalue()
        self.assertIn("name: custom", output)
        self.assertNotIn("adapter: datadog", output)

    def test_run_extensions_writes_template_to_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "datadog-profile.yaml"
            args = SimpleNamespace(
                source="datadog",
                format="yaml",
                template_only=False,
                template_out=str(output_path),
            )

            with redirect_stdout(io.StringIO()) as stdout:
                app_cli._run_extensions(args)

            self.assertEqual(stdout.getvalue().strip(), str(output_path))
            contents = output_path.read_text(encoding="utf-8")
            self.assertIn("name: custom", contents)
            self.assertIn("metric_map: {}", contents)


class TestUnifiedCliVerifyPanels(unittest.TestCase):
    def test_verify_panels_parser_has_required_flags(self):
        parser = app_cli._build_parser()
        args = parser.parse_args(
            [
                "verify-panels",
                "--migration-out", "/tmp/migration",
                "--output", "/tmp/report.json",
            ]
        )
        self.assertEqual(args.command, "verify-panels")
        self.assertEqual(args.migration_out, "/tmp/migration")
        self.assertEqual(args.output, "/tmp/report.json")
        self.assertEqual(args.space, "default")
        self.assertEqual(args.limit, 0)
        self.assertFalse(args.verbose)

    def test_verify_panels_routes_to_verifier_main(self):
        # Ensure the verifier package is importable before we patch its
        # `main` symbol (the routing function mutates sys.path at call
        # time; pre-mutating here lets `patch` resolve the target).
        repo_root = Path(app_cli.__file__).resolve().parents[2]
        verifier_parent = repo_root / "parity-rig"
        if str(verifier_parent) not in sys.path:
            sys.path.insert(0, str(verifier_parent))
        import verifier.cli as _verifier_cli  # noqa: F401

        args = SimpleNamespace(
            command="verify-panels",
            migration_out="/tmp/m",
            output="/tmp/o.json",
            kibana_url="https://kbn",
            es_url="https://es",
            api_key="KEY",
            dashboard_id="dash-1",
            space="custom",
            es_index="metrics-*",
            limit=5,
            verbose=True,
        )
        with patch("sys.exit") as mock_exit, patch(
            "verifier.cli.main", return_value=0
        ) as mock_main:
            app_cli._run_verify_panels(args)
        self.assertTrue(mock_main.called)
        forwarded = mock_main.call_args.args[0]
        self.assertIn("--migration-out", forwarded)
        self.assertIn("/tmp/m", forwarded)
        self.assertIn("--dashboard-id", forwarded)
        self.assertIn("dash-1", forwarded)
        self.assertIn("--space", forwarded)
        self.assertIn("custom", forwarded)
        self.assertIn("--limit", forwarded)
        self.assertIn("5", forwarded)
        self.assertIn("--verbose", forwarded)
        mock_exit.assert_called_once_with(0)


class TestUnifiedCliVerifyVisual(unittest.TestCase):
    def test_verify_visual_parser_has_required_flags(self):
        parser = app_cli._build_parser()
        args = parser.parse_args(
            [
                "verify-visual",
                "--migration-out", "/tmp/m",
                "--grafana-uid", "g-uid",
                "--grafana-slug", "g-slug",
                "--kibana-url", "https://kbn",
                "--kibana-dash-id", "k-id",
                "--output-dir", "/tmp/vrout",
                "--report", "/tmp/vrout/report.json",
            ]
        )
        self.assertEqual(args.command, "verify-visual")
        self.assertEqual(args.grafana_uid, "g-uid")
        self.assertEqual(args.kibana_dash_id, "k-id")
        # Default Grafana URL points at the parity rig
        self.assertEqual(args.grafana_url, "http://localhost:23000")
        self.assertEqual(args.from_, "now-1h")
        self.assertEqual(args.to, "now")
        self.assertEqual(args.threshold, 0.15)

    def test_verify_visual_routes_to_visual_regression_main(self):
        repo_root = Path(app_cli.__file__).resolve().parents[2]
        verifier_parent = repo_root / "parity-rig"
        if str(verifier_parent) not in sys.path:
            sys.path.insert(0, str(verifier_parent))
        import verifier.visual_regression as _vr  # noqa: F401

        args = SimpleNamespace(
            command="verify-visual",
            migration_out="/tmp/m",
            grafana_url="http://localhost:23000",
            grafana_uid="g-uid",
            grafana_slug="g-slug",
            kibana_url="https://kbn",
            kibana_dash_id="k-id",
            output_dir="/tmp/vrout",
            report="/tmp/vrout/report.json",
            from_="now-2h",
            to="now",
            threshold=0.20,
            wait_extra_seconds=6,
            state="/tmp/state.json",
            verbose=True,
        )
        with patch("sys.exit") as mock_exit, patch(
            "verifier.visual_regression.main", return_value=0
        ) as mock_main:
            app_cli._run_verify_visual(args)
        self.assertTrue(mock_main.called)
        forwarded = mock_main.call_args.args[0]
        # Key flags forwarded verbatim
        self.assertIn("--grafana-uid", forwarded)
        self.assertIn("g-uid", forwarded)
        self.assertIn("--kibana-dash-id", forwarded)
        self.assertIn("k-id", forwarded)
        self.assertIn("--from", forwarded)
        self.assertIn("now-2h", forwarded)
        self.assertIn("--threshold", forwarded)
        self.assertIn("0.2", forwarded)
        self.assertIn("--state", forwarded)
        self.assertIn("/tmp/state.json", forwarded)
        self.assertIn("--verbose", forwarded)
        mock_exit.assert_called_once_with(0)


class TestUnifiedCliLegacyFlagContract(unittest.TestCase):
    def test_unified_migrate_does_not_accept_include(self):
        parser = app_cli._build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["migrate", "--source", "grafana", "--include", "dashboards,alerts"]
            )

    def test_unified_migrate_does_not_accept_alert_dry_run(self):
        parser = app_cli._build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["migrate", "--source", "grafana", "--alert-dry-run"])

    def test_unified_migrate_does_not_accept_cluster_shortcuts(self):
        parser = app_cli._build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["migrate", "--source", "grafana", "--list-dashboards"])

    def test_unified_cli_native_promql_mutex(self):
        parser = app_cli._build_parser()
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parser.parse_args(
                    [
                        "migrate", "--source", "grafana",
                        "--native-promql", "--no-native-promql",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
