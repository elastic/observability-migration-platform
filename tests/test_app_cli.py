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
                    "--validate",
                    "--preflight",
                ],
            )
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_forwards_translation_mode_when_set(self, mock_main):
        # A non-default --translation-mode is forwarded to the adapter CLI (for
        # Datadog it is a documented no-op, but the user gets the acknowledgement);
        # the default "auto" is NOT forwarded so the adapter's own default applies.
        base = dict(
            input_mode="files", input_dir="infra/datadog/dashboards",
            output_dir="out", data_view="metrics-*", field_profile="otel",
            logs_index="logs-*", validate=False, upload=False,
            preflight=False, es_url="", es_api_key="", kibana_url="",
            kibana_api_key="", space_id="", dataset_filter="", logs_dataset_filter="",
            smoke=False, browser_audit=False, capture_screenshots=False,
            smoke_output="", smoke_timeout=30, chrome_binary="",
        )
        original_argv = list(sys.argv)
        try:
            app_cli._run_datadog_migration(SimpleNamespace(**base, translation_mode="esql"))
            self.assertIn("--translation-mode", sys.argv)
            idx = sys.argv.index("--translation-mode")
            self.assertEqual(sys.argv[idx + 1], "esql")
            mock_main.reset_mock()

            app_cli._run_datadog_migration(SimpleNamespace(**base, translation_mode="auto"))
            self.assertNotIn("--translation-mode", sys.argv)
        finally:
            sys.argv = original_argv

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_forwards_source_execution(self, mock_main):
        # The Datadog source-side oracle (--source-execution) is implemented
        # on the standalone datadog CLI; the canonical obs-migrate wrapper
        # must expose and forward it, or users cannot reach live
        # source-vs-target comparison through the documented interface.
        args = SimpleNamespace(
            input_mode="api",
            input_dir="",
            output_dir="out",
            data_view="",
            field_profile="otel",
            logs_index="",
            validate=True,
            upload=False,
            preflight=False,
            source_execution=True,
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
        )
        original_argv = list(sys.argv)
        try:
            app_cli._run_datadog_migration(args)
            self.assertIn("--source-execution", sys.argv)
        finally:
            sys.argv = original_argv
        mock_main.assert_called_once_with()

    def test_migrate_parser_accepts_source_execution(self):
        parser = app_cli._build_parser()
        args = parser.parse_args(["migrate", "--source", "datadog", "--source-execution"])
        self.assertTrue(args.source_execution)

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_omits_default_data_view(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/datadog/dashboards",
            output_dir="datadog_migration_output",
            data_view="",
            field_profile="prometheus",
            logs_index="",
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
        )
        original_argv = list(sys.argv)

        try:
            app_cli._run_datadog_migration(args)
            self.assertNotIn("--data-view", sys.argv)
            self.assertIn("--field-profile", sys.argv)
            self.assertIn("prometheus", sys.argv)
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
        args = self._make_grafana_args()
        original_argv = list(sys.argv)

        try:
            app_cli._run_grafana_migration(args)

            self.assertIn("--field-profile", sys.argv)
            self.assertIn("otel", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_omits_default_data_view(self, mock_main):
        args = self._make_grafana_args(data_view="", esql_index="")
        original_argv = list(sys.argv)

        try:
            app_cli._run_grafana_migration(args)
            self.assertNotIn("--data-view", sys.argv)
        finally:
            sys.argv = original_argv

        mock_main.assert_called_once_with()

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_never_forwards_native_promql_flags(self, mock_main):
        """Issue #158: the native-PROMQL override flags are gone; the unified
        CLI never forwards them to the source adapter."""
        args = self._make_grafana_args()
        original_argv = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            self.assertNotIn("--native-promql", sys.argv)
            self.assertNotIn("--no-native-promql", sys.argv)
        finally:
            sys.argv = original_argv

    def test_run_cluster_threads_verify_into_serverless_calls(self):
        args = SimpleNamespace(
            action="list-dashboards",
            kibana_url="https://kb",
            kibana_api_key="key",
            space_id="",
            dashboard_ids="",
            data_view_patterns="metrics-*",
            ca_cert="/tmp/ca.pem",
            insecure=False,
        )
        with patch(
            "observability_migration.targets.kibana.serverless.list_dashboards",
            return_value=[],
        ) as mock_list, redirect_stdout(io.StringIO()):
            app_cli._run_cluster(args)
        self.assertEqual(mock_list.call_args.kwargs.get("verify"), "/tmp/ca.pem")

    def test_migrate_parser_tls_flag_defaults(self):
        parser = app_cli._build_parser()
        with mock.patch.dict(
            "os.environ", {"OBS_MIGRATE_CA_CERT": "", "OBS_MIGRATE_INSECURE": ""}, clear=False
        ):
            args = parser.parse_args(["migrate", "--source", "grafana"])
        self.assertEqual(args.ca_cert, "")
        self.assertFalse(args.insecure)
        self.assertEqual(args.grafana_url, "")
        self.assertEqual(args.grafana_user, "")
        self.assertEqual(args.grafana_pass, "")

    def test_migrate_parser_accepts_tls_and_grafana_flags(self):
        parser = app_cli._build_parser()
        args = parser.parse_args([
            "migrate", "--source", "grafana",
            "--ca-cert", "/tmp/ca.pem", "--insecure",
            "--grafana-url", "https://graf", "--grafana-user", "u", "--grafana-pass", "p",
        ])
        self.assertEqual(args.ca_cert, "/tmp/ca.pem")
        self.assertTrue(args.insecure)
        self.assertEqual(args.grafana_url, "https://graf")
        self.assertEqual(args.grafana_user, "u")
        self.assertEqual(args.grafana_pass, "p")

    def test_cluster_and_audit_parsers_expose_tls_flags(self):
        parser = app_cli._build_parser()
        cluster = parser.parse_args([
            "cluster", "list-dashboards", "--kibana-url", "https://kb", "--insecure",
        ])
        self.assertTrue(cluster.insecure)
        audit = parser.parse_args([
            "audit-rules", "--kibana-url", "https://kb", "--ca-cert", "/tmp/ca.pem",
        ])
        self.assertEqual(audit.ca_cert, "/tmp/ca.pem")

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_forwards_tls_and_grafana_flags(self, mock_main):
        args = self._make_grafana_args(
            ca_cert="/tmp/ca.pem",
            insecure=True,
            grafana_url="https://graf",
            grafana_user="u",
            grafana_pass="p",
        )
        original_argv = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            forwarded = sys.argv
            self.assertIn("--ca-cert", forwarded)
            self.assertIn("/tmp/ca.pem", forwarded)
            self.assertIn("--insecure", forwarded)
            self.assertIn("--grafana-url", forwarded)
            self.assertIn("https://graf", forwarded)
            self.assertIn("--grafana-user", forwarded)
            self.assertIn("--grafana-pass", forwarded)
        finally:
            sys.argv = original_argv

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_omits_tls_flags_by_default(self, mock_main):
        args = self._make_grafana_args()
        original_argv = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            self.assertNotIn("--ca-cert", sys.argv)
            self.assertNotIn("--insecure", sys.argv)
            self.assertNotIn("--grafana-url", sys.argv)
        finally:
            sys.argv = original_argv

    def test_migrate_parser_accepts_select_flags(self):
        parser = app_cli._build_parser()
        args = parser.parse_args([
            "migrate", "--source", "grafana",
            "--select-folder", "Prod",
            "--select-tag", "team:infra", "--select-tag", "env:prod",
            "--select-datasource", "prometheus",
            "--select-team", "infra",
            "--select-updated-after", "2026-01-01",
            "--select-updated-before", "2026-02-01",
            "--select-starred",
        ])
        self.assertEqual(args.select_folder, ["Prod"])
        self.assertEqual(args.select_tag, ["team:infra", "env:prod"])
        self.assertEqual(args.select_datasource, ["prometheus"])
        self.assertEqual(args.select_team, ["infra"])
        self.assertEqual(args.select_updated_after, "2026-01-01")
        self.assertEqual(args.select_updated_before, "2026-02-01")
        self.assertTrue(args.select_starred)

    def test_migrate_parser_select_flag_defaults(self):
        parser = app_cli._build_parser()
        args = parser.parse_args(["migrate", "--source", "datadog"])
        self.assertEqual(args.select_folder, [])
        self.assertEqual(args.select_tag, [])
        self.assertEqual(args.select_datasource, [])
        self.assertEqual(args.select_team, [])
        self.assertEqual(args.select_updated_after, "")
        self.assertEqual(args.select_updated_before, "")
        self.assertFalse(args.select_starred)

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_forwards_select_flags(self, mock_main):
        args = self._make_grafana_args(
            select_folder=["Prod"],
            select_tag=["team:infra"],
            select_datasource=["prometheus"],
            select_team=["infra"],
            select_updated_after="2026-01-01",
            select_updated_before="",
            select_starred=True,
        )
        original_argv = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            forwarded = sys.argv
            self.assertIn("--select-folder", forwarded)
            self.assertIn("Prod", forwarded)
            self.assertIn("--select-tag", forwarded)
            self.assertIn("team:infra", forwarded)
            self.assertIn("--select-datasource", forwarded)
            self.assertIn("--select-team", forwarded)
            self.assertIn("--select-updated-after", forwarded)
            self.assertIn("2026-01-01", forwarded)
            self.assertNotIn("--select-updated-before", forwarded)
            self.assertIn("--select-starred", forwarded)
        finally:
            sys.argv = original_argv

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_forwards_alert_uids(self, mock_main):
        args = self._make_grafana_args(
            assets="alerts",
            create_alert_rules=True,
            alert_uids="rule-uid-1,rule-uid-2",
            alert_folder="",
        )
        original_argv = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            forwarded = sys.argv
            self.assertIn("--alert-uids", forwarded)
            self.assertIn("rule-uid-1,rule-uid-2", forwarded)
            self.assertNotIn("--alert-folder", forwarded)
        finally:
            sys.argv = original_argv

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_forwards_alert_folder(self, mock_main):
        args = self._make_grafana_args(
            assets="alerts",
            create_alert_rules=True,
            alert_uids="",
            alert_folder="infra-folder-uid",
        )
        original_argv = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            forwarded = sys.argv
            self.assertIn("--alert-folder", forwarded)
            self.assertIn("infra-folder-uid", forwarded)
            self.assertNotIn("--alert-uids", forwarded)
        finally:
            sys.argv = original_argv

    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_run_grafana_migration_omits_alert_selectors_by_default(self, mock_main):
        args = self._make_grafana_args()
        original_argv = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            forwarded = sys.argv
            self.assertNotIn("--alert-uids", forwarded)
            self.assertNotIn("--alert-folder", forwarded)
        finally:
            sys.argv = original_argv

    def test_migrate_parser_accepts_grafana_alert_selectors(self):
        parser = app_cli._build_parser()
        args = parser.parse_args([
            "migrate", "--source", "grafana", "--assets", "alerts",
            "--alert-uids", "rule-uid-1,rule-uid-2",
            "--alert-folder", "infra-folder-uid",
        ])
        self.assertEqual(args.alert_uids, "rule-uid-1,rule-uid-2")
        self.assertEqual(args.alert_folder, "infra-folder-uid")

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_forwards_select_flags(self, mock_main):
        args = SimpleNamespace(
            input_mode="api",
            input_dir="infra/datadog/dashboards",
            output_dir="datadog_migration_output",
            data_view="",
            field_profile="otel",
            logs_index="",
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
            select_folder=[],
            select_tag=["team:payments"],
            select_datasource=[],
            select_team=[],
            select_updated_after="",
            select_updated_before="2026-03-01",
            select_starred=False,
        )
        original_argv = list(sys.argv)
        try:
            app_cli._run_datadog_migration(args)
            forwarded = sys.argv
            self.assertIn("--select-tag", forwarded)
            self.assertIn("team:payments", forwarded)
            self.assertIn("--select-updated-before", forwarded)
            self.assertIn("2026-03-01", forwarded)
            self.assertNotIn("--select-folder", forwarded)
            self.assertNotIn("--select-starred", forwarded)
        finally:
            sys.argv = original_argv

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_forwards_tls_flags(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/datadog/dashboards",
            output_dir="datadog_migration_output",
            data_view="metrics-*",
            field_profile="otel",
            logs_index="",
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
            ca_cert="/tmp/ca.pem",
            insecure=True,
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
            self.assertIn("--ca-cert", sys.argv)
            self.assertIn("/tmp/ca.pem", sys.argv)
            self.assertIn("--insecure", sys.argv)
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
    def test_run_datadog_migration_forwards_dashboard_ids(self, mock_main):
        args = SimpleNamespace(
            input_mode="api",
            input_dir="",
            output_dir="datadog_migration_output",
            data_view="metrics-*",
            field_profile="otel",
            logs_index="",
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
            dashboard_ids="abc-def-123",
            monitor_ids="",
            monitor_query="",
            env_file="",
            ca_cert="",
            insecure=False,
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
            self.assertIn("--dashboard-ids", sys.argv)
            self.assertIn("abc-def-123", sys.argv)
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

    @staticmethod
    def _make_native_artifact_dir(root: Path) -> Path:
        """Create the only upload input shape: <dir>/native/*.native.json."""
        (root / "native").mkdir(parents=True, exist_ok=True)
        (root / "native" / "dash.native.json").write_text("{}", encoding="utf-8")
        return root

    @patch("observability_migration.app.cli.target_registry.get")
    def test_run_upload_uses_registered_target_adapter(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = self._make_native_artifact_dir(Path(tmpdir))
            adapter = mock.Mock()
            adapter.upload.return_value = {
                "summary": {"uploaded_ok": 1, "total": 1},
                "records": [{"artifact": "dash.native.json", "success": True, "output": ""}],
            }
            mock_get.return_value = mock.Mock(return_value=adapter)

            with redirect_stdout(io.StringIO()):
                app_cli._run_upload(
                    SimpleNamespace(
                        artifact_dir=str(artifact_dir),
                        kibana_url="https://kibana.example",
                        kibana_api_key="secret",
                        space_id="shadow",
                        ca_cert="/tmp/ca.pem",
                        insecure=False,
                    )
                )

        adapter.upload.assert_called_once()
        self.assertEqual(adapter.upload.call_args.kwargs.get("verify"), "/tmp/ca.pem")

    def test_upload_requires_artifact_dir(self):
        # --artifact-dir is a plain required argument now that the YAML/compiled
        # aliases are gone; omitting it must be an argparse usage error, not a
        # run that silently uploads nothing.
        parser = app_cli._build_parser()
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(stderr):
            parser.parse_args(["upload", "--kibana-url", "https://kibana.example"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--artifact-dir", stderr.getvalue())

    @patch("observability_migration.app.cli.target_registry.get")
    def test_run_upload_defaults_to_native_dashboards_api(self, mock_get):
        # The typed Dashboards API is now the only upload path: the adapter is
        # called with just the connection kwargs, and never with the removed
        # use_dashboards_api / artifact_format switches.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = self._make_native_artifact_dir(Path(tmpdir))
            adapter = mock.Mock()
            adapter.upload.return_value = {
                "summary": {"uploaded_ok": 1, "total": 1, "artifact_format": "native"},
                "records": [
                    {
                        "artifact": "dash.native.json",
                        "success": True,
                        "output": "",
                        "status": "created",
                    }
                ],
            }
            mock_get.return_value = mock.Mock(return_value=adapter)

            parser = app_cli._build_parser()
            args = parser.parse_args(
                [
                    "upload",
                    "--artifact-dir",
                    str(artifact_dir),
                    "--kibana-url",
                    "https://kibana.example",
                ]
            )
            with redirect_stdout(io.StringIO()):
                app_cli._run_upload(args)

        adapter.upload.assert_called_once()
        kwargs = adapter.upload.call_args.kwargs
        self.assertEqual(
            set(kwargs), {"kibana_url", "kibana_api_key", "space_id", "verify"}
        )
        self.assertEqual(kwargs["kibana_url"], "https://kibana.example")

    def test_upload_help_documents_typed_dashboards_api_only(self):
        parser = app_cli._build_parser()
        stdout = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(stdout):
            parser.parse_args(["upload", "--help"])
        help_text = stdout.getvalue()
        self.assertIn("/api/dashboards", help_text)
        self.assertIn("typed Kibana Dashboards API", help_text)
        # The legacy saved-objects _import opt-out is gone from the surface.
        self.assertNotIn("--legacy-import", help_text)

    def test_upload_help_describes_split_dashboard_artifact_shapes(self):
        parser = app_cli._build_parser()

        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(stdout):
            parser.parse_args(["upload", "--help"])

        self.assertEqual(ctx.exception.code, 0)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("migration_output/dashboards", help_text)
        # A migration writes native/ + ir/, and native/*.native.json is the
        # only accepted upload input. The YAML/NDJSON shapes are gone and must
        # not be advertised, or operators will hand us input we cannot read.
        self.assertIn("'native/' child", help_text)
        self.assertIn("native/*.native.json", help_text)
        self.assertNotIn("directory of .yaml files", help_text)
        self.assertNotIn("'yaml/' child", help_text)
        self.assertNotIn("'compiled/' directory", help_text)
        self.assertNotIn("single .yaml file", help_text)
        self.assertNotIn("migration_output/yaml", help_text)

    @patch("observability_migration.app.cli.target_registry.get")
    def test_run_upload_empty_records_describes_split_dashboard_inputs(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir)
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
                        artifact_dir=str(artifact_dir),
                        kibana_url="https://kibana.example",
                        kibana_api_key="secret",
                        space_id="shadow",
                        ca_cert="",
                        insecure=False,
                    )
                )

        self.assertEqual(ctx.exception.code, 1)
        error_text = " ".join(stderr.getvalue().split())
        self.assertIn("migration_output/dashboards", error_text)
        self.assertIn("which holds native/ and ir/", error_text)
        # The removed YAML/NDJSON shapes must not be offered as a way out.
        self.assertNotIn("directory of .yaml files", error_text)
        self.assertNotIn("a dir containing 'yaml/'", error_text)
        self.assertNotIn("'compiled/' directory", error_text)
        self.assertNotIn("a .yaml file,", error_text)
        self.assertNotIn("migration_output/yaml", error_text)

    @patch("observability_migration.app.cli.target_registry.get")
    def test_run_upload_exits_nonzero_and_names_silently_dropped_panels(self, mock_get):
        # Kibana returned HTTP 200 but kept fewer panels than were sent. The
        # command must fail (a silent partial write that exits 0 is never
        # investigated) and name the panel so the operator can fix it.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = self._make_native_artifact_dir(Path(tmpdir))
            adapter = mock.Mock()
            adapter.upload.return_value = {
                "summary": {"uploaded_ok": 0, "total": 1, "panels_dropped": 1},
                "records": [
                    {
                        "artifact": "dash.native.json",
                        "success": False,
                        "output": "Dash: lossy",
                        "status": "lossy",
                        "panels_sent": 6,
                        "panels_accepted": 5,
                        "dropped_panels": [
                            {
                                "title": "Memory usage",
                                "reason": "Unable to transform panel config. Error: [color]",
                                "section": "Overview",
                                "grid": {"x": 12, "y": 0, "w": 12, "h": 6},
                            }
                        ],
                    }
                ],
            }
            mock_get.return_value = mock.Mock(return_value=adapter)

            parser = app_cli._build_parser()
            args = parser.parse_args(
                [
                    "upload",
                    "--artifact-dir", str(artifact_dir),
                    "--kibana-url", "https://kibana.example",
                ]
            )
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as ctx, redirect_stdout(io.StringIO()), \
                    redirect_stderr(stderr):
                app_cli._run_upload(args)

        self.assertEqual(ctx.exception.code, 1)
        error_text = " ".join(stderr.getvalue().split())
        self.assertIn("DROPPED PANEL Memory usage", error_text)
        self.assertIn("[section Overview]", error_text)
        self.assertIn("Unable to transform panel config", error_text)
        self.assertIn("silently dropped by Kibana", error_text)

    @patch("observability_migration.app.cli.target_registry.get")
    def test_run_upload_stays_silent_and_exits_zero_when_no_panels_dropped(self, mock_get):
        # No false positive: a clean upload must not print a data-loss warning
        # or fail, or the check would be turned off.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = self._make_native_artifact_dir(Path(tmpdir))
            adapter = mock.Mock()
            adapter.upload.return_value = {
                "summary": {"uploaded_ok": 1, "total": 1, "panels_dropped": 0},
                "records": [
                    {
                        "artifact": "dash.native.json",
                        "success": True,
                        "output": "Dash: updated",
                        "status": "updated",
                        "panels_sent": 6,
                        "panels_accepted": 6,
                        "dropped_panels": [],
                    }
                ],
            }
            mock_get.return_value = mock.Mock(return_value=adapter)

            parser = app_cli._build_parser()
            args = parser.parse_args(
                [
                    "upload",
                    "--artifact-dir", str(artifact_dir),
                    "--kibana-url", "https://kibana.example",
                ]
            )
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                app_cli._run_upload(args)

        self.assertNotIn("DROPPED PANEL", stderr.getvalue())
        self.assertNotIn("silently dropped", stderr.getvalue())

    # -- --artifact-dir (native review artifacts) ----------------------------

    @patch("observability_migration.app.cli.target_registry.get")
    def test_run_upload_artifact_dir_passes_through_to_adapter(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = self._make_native_artifact_dir(Path(tmpdir))
            adapter = mock.Mock()
            adapter.upload.return_value = {
                "summary": {"uploaded_ok": 1, "total": 1, "artifact_format": "native"},
                "records": [
                    {
                        "artifact": "dash.native.json",
                        "success": True,
                        "output": "",
                        "status": "created",
                    }
                ],
            }
            mock_get.return_value = mock.Mock(return_value=adapter)

            with redirect_stdout(io.StringIO()):
                app_cli._run_upload(
                    SimpleNamespace(
                        artifact_dir=str(artifact_dir),
                        kibana_url="https://kibana.example",
                        kibana_api_key="secret",
                        space_id="shadow",
                        ca_cert="",
                        insecure=False,
                    )
                )

        adapter.upload.assert_called_once()
        # The artifact dir is handed to the adapter verbatim; there is no
        # format negotiation left to forward.
        self.assertEqual(adapter.upload.call_args.args[0], Path(str(artifact_dir)))
        self.assertNotIn("artifact_format", adapter.upload.call_args.kwargs)
        self.assertNotIn("use_dashboards_api", adapter.upload.call_args.kwargs)

    @patch("observability_migration.app.cli.target_registry.get")
    def test_run_upload_no_native_artifacts_found_reports_clear_error(self, mock_get):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir)
            adapter = mock.Mock()
            adapter.upload.return_value = {
                "summary": {"uploaded_ok": 0, "total": 0, "error": "no_native_artifacts_found"},
                "records": [],
            }
            mock_get.return_value = mock.Mock(return_value=adapter)

            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as ctx, redirect_stderr(stderr):
                app_cli._run_upload(
                    SimpleNamespace(
                        artifact_dir=str(artifact_dir),
                        kibana_url="https://kibana.example",
                        kibana_api_key="secret",
                        space_id="shadow",
                        ca_cert="",
                        insecure=False,
                    )
                )

        self.assertEqual(ctx.exception.code, 1)
        error_text = stderr.getvalue()
        self.assertIn("No native Dashboard-as-Code artifacts", error_text)
        # The fix is a different --artifact-dir, never a removed format flag.
        self.assertNotIn("--artifact-format", error_text)

    def test_upload_help_mentions_artifact_dir_and_native_artifacts(self):
        parser = app_cli._build_parser()
        stdout = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(stdout):
            parser.parse_args(["upload", "--help"])
        help_text = stdout.getvalue()
        self.assertIn("--artifact-dir", help_text)
        self.assertIn("native", help_text)
        # --artifact-format is gone: native is the only artifact format.
        self.assertNotIn("--artifact-format", help_text)

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_forwards_smoke_flags(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="infra/datadog/dashboards",
            output_dir="datadog_migration_output",
            data_view="metrics-*",
            field_profile="otel",
            logs_index="",
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
    def test_run_grafana_migration_never_forwards_removed_yaml_flags(self, mock_main):
        # The Grafana adapter CLI no longer accepts these; forwarding one would
        # abort the migration with an argparse error deep inside the adapter.
        parser = app_cli._build_parser()
        args = parser.parse_args(["migrate", "--source", "grafana"])
        original_argv = list(sys.argv)

        try:
            app_cli._run_grafana_migration(args)
            for removed in ("--legacy-import", "--compile", "--no-compile",
                            "--use-dashboards-api"):
                self.assertNotIn(removed, sys.argv)
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

    @patch("observability_migration.adapters.source.datadog.cli.main")
    def test_run_datadog_migration_never_forwards_removed_yaml_flags(self, mock_main):
        # Same contract as the Grafana side: the Datadog adapter CLI dropped the
        # YAML-era switches, so the unified CLI must never emit them.
        parser = app_cli._build_parser()
        args = parser.parse_args(["migrate", "--source", "datadog"])
        original_argv = list(sys.argv)

        try:
            app_cli._run_datadog_migration(args)
            for removed in ("--legacy-import", "--compile", "--no-compile",
                            "--use-dashboards-api"):
                self.assertNotIn(removed, sys.argv)
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
                "--no-invariants",
                "--live-oracle",
                "--fail-on-invariant",
            ]
        )
        self.assertEqual(args.command, "verify-panels")
        self.assertEqual(args.migration_out, "/tmp/migration")
        self.assertEqual(args.output, "/tmp/report.json")
        self.assertEqual(args.space, "default")
        self.assertEqual(args.limit, 0)
        self.assertTrue(args.no_invariants)
        self.assertTrue(args.live_oracle)
        self.assertTrue(args.fail_on_invariant)
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
            no_invariants=True,
            live_oracle=True,
            fail_on_invariant=True,
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
        self.assertIn("--no-invariants", forwarded)
        self.assertIn("--live-oracle", forwarded)
        self.assertIn("--fail-on-invariant", forwarded)
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

    def test_unified_cli_rejects_removed_native_promql_flags(self):
        """Issue #158: the native-PROMQL override flags are removed from the
        unified ``migrate`` subcommand."""
        parser = app_cli._build_parser()
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parser.parse_args(["migrate", "--source", "grafana", "--native-promql"])
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                parser.parse_args(["migrate", "--source", "grafana", "--no-native-promql"])


class TestRemovedDashboardYamlSurfaces(unittest.TestCase):
    """The dashboard-YAML surfaces are gone; the break must be legible.

    argparse would report a removed flag as "unrecognized arguments" and a
    removed subcommand as "invalid choice", both of which read like a typo. An
    operator upgrading a script needs to be told the surface was removed and
    what replaced it — and must never get a traceback.
    """

    def _run_main(self, argv: list[str]) -> str:
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(stderr), \
                redirect_stdout(io.StringIO()):
            app_cli.main(argv)
        self.assertEqual(ctx.exception.code, 2, stderr.getvalue())
        message = stderr.getvalue()
        self.assertNotIn("Traceback (most recent call last)", message)
        self.assertNotIn("unrecognized arguments", message)
        return message

    def test_compile_subcommand_removed_names_replacement(self):
        message = self._run_main(
            ["compile", "--yaml-dir", "/tmp/yaml", "--output-dir", "/tmp/compiled"]
        )
        collapsed = " ".join(message.split())
        self.assertIn("the 'compile' command has been removed", collapsed)
        # Name the replacement, not just the removal.
        self.assertIn("native/*.native.json", collapsed)
        self.assertIn("obs-migrate upload --artifact-dir", collapsed)
        self.assertIn("typed Kibana Dashboards API", collapsed)
        self.assertIn("command-contract.md", collapsed)

    def test_upload_yaml_dir_removed(self):
        message = self._run_main(
            [
                "upload",
                "--yaml-dir", "/tmp/yaml",
                "--kibana-url", "https://kibana.example",
            ]
        )
        collapsed = " ".join(message.split())
        self.assertIn("the '--yaml-dir' option has been removed", collapsed)
        self.assertIn("--artifact-dir", collapsed)

    def test_upload_compiled_dir_removed(self):
        message = self._run_main(
            [
                "upload",
                "--compiled-dir", "/tmp/compiled",
                "--kibana-url", "https://kibana.example",
            ]
        )
        collapsed = " ".join(message.split())
        self.assertIn("the '--compiled-dir' option has been removed", collapsed)
        self.assertIn("--artifact-dir", collapsed)

    def test_upload_artifact_format_removed(self):
        message = self._run_main(
            [
                "upload",
                "--artifact-dir", "/tmp/x",
                "--artifact-format", "yaml",
                "--kibana-url", "https://kibana.example",
            ]
        )
        collapsed = " ".join(message.split())
        self.assertIn("the '--artifact-format' option has been removed", collapsed)
        self.assertIn("native", collapsed)

    def test_upload_legacy_import_removed(self):
        message = self._run_main(
            [
                "upload",
                "--artifact-dir", "/tmp/x",
                "--kibana-url", "https://kibana.example",
                "--legacy-import",
            ]
        )
        collapsed = " ".join(message.split())
        self.assertIn("the '--legacy-import' option has been removed", collapsed)
        self.assertIn("PUT /api/dashboards/{id}", collapsed)

    def test_migrate_compile_removed(self):
        message = self._run_main(["migrate", "--source", "grafana", "--compile"])
        collapsed = " ".join(message.split())
        self.assertIn("the '--compile' option has been removed", collapsed)
        self.assertIn("'compiled/' directory", collapsed)

    def test_migrate_no_compile_removed(self):
        message = self._run_main(["migrate", "--source", "grafana", "--no-compile"])
        collapsed = " ".join(message.split())
        self.assertIn("the '--no-compile' option has been removed", collapsed)

    def test_migrate_legacy_import_removed(self):
        message = self._run_main(["migrate", "--source", "grafana", "--legacy-import"])
        collapsed = " ".join(message.split())
        self.assertIn("the '--legacy-import' option has been removed", collapsed)

    def test_migrate_use_dashboards_api_removed(self):
        # Was a hidden no-op selecting "the typed API", which is now the only
        # path. Undocumented, but scripts may still pass it.
        message = self._run_main(
            ["migrate", "--source", "grafana", "--use-dashboards-api"]
        )
        collapsed = " ".join(message.split())
        self.assertIn("the '--use-dashboards-api' option has been removed", collapsed)

    def test_removed_flag_detected_in_equals_form(self):
        # Scripts commonly write --flag=value; the rejection must not depend on
        # the space-separated spelling.
        message = self._run_main(
            ["upload", "--yaml-dir=/tmp/yaml", "--kibana-url", "https://kibana.example"]
        )
        self.assertIn("the '--yaml-dir' option has been removed", " ".join(message.split()))

    def test_surviving_commands_are_untouched(self):
        # No false positive: the rejection gate must not swallow a valid run.
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                app_cli.main(["doctor"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertNotIn("has been removed", buf.getvalue())


class TestDoctorSubcommand(unittest.TestCase):
    def test_doctor_reports_install_readiness_without_kb_tools(self):
        # The dashboard-YAML compile path is gone, so the external kb-dashboard-*
        # tools are no longer required by any command. Doctor must not probe or
        # mention them, or operators will chase an install gap that cannot
        # affect a migration.
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                app_cli.main(["doctor"])
        self.assertEqual(ctx.exception.code, 0)
        out = buf.getvalue().lower()
        self.assertIn("obs-migrate doctor", out)
        self.assertIn("python:", out)
        self.assertIn("platform:", out)
        self.assertIn("required dependencies:", out)
        self.assertIn("optional extras:", out)
        self.assertNotIn("kb-dashboard-cli", out)
        self.assertNotIn("kb-dashboard-lint", out)
        self.assertIn("ready.", out)
        self.assertIn("next steps", out)
        self.assertIn("list-samples", out)
        self.assertIn("migrate", out)

    def test_doctor_followup_cmd_prefers_path_when_bare_command_missing(self):
        # Issue #254: Ready Next steps must not print a bare obs-migrate when
        # that console script is not on PATH (typical after uvx).
        with mock.patch("shutil.which", return_value=None):
            with mock.patch.object(sys, "argv", ["pytest-runner"]):
                cmd = app_cli._doctor_followup_cmd()
        self.assertEqual(
            cmd,
            "uvx --from 'elastic-observability-migration[all]' obs-migrate",
        )

    def test_doctor_followup_cmd_ignores_uvx_cache_paths(self):
        cache_script = (
            Path.home()
            / ".cache"
            / "uv"
            / "archive-v0"
            / "hash"
            / "bin"
            / "obs-migrate"
        )
        with mock.patch("shutil.which", return_value=None):
            with mock.patch.object(sys, "argv", [str(cache_script)]):
                cmd = app_cli._doctor_followup_cmd()
        self.assertEqual(
            cmd,
            "uvx --from 'elastic-observability-migration[all]' obs-migrate",
        )

    def test_doctor_followup_cmd_reuses_explicit_venv_console_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / ".venv" / "bin"
            bin_dir.mkdir(parents=True)
            (root / ".venv" / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
            script = bin_dir / "obs-migrate"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch("shutil.which", return_value=None):
                with mock.patch.object(sys, "argv", [str(script)]):
                    old_cwd = Path.cwd()
                    try:
                        import os

                        os.chdir(root)
                        cmd = app_cli._doctor_followup_cmd()
                    finally:
                        os.chdir(old_cwd)
        self.assertEqual(cmd, str(Path(".venv") / "bin" / "obs-migrate"))

    def test_doctor_ready_next_steps_use_followup_launcher(self):
        buf = io.StringIO()
        with mock.patch.object(
            app_cli,
            "_doctor_followup_cmd",
            return_value="uvx --from 'elastic-observability-migration[all]' obs-migrate",
        ):
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    app_cli.main(["doctor"])
        self.assertEqual(ctx.exception.code, 0)
        out = buf.getvalue()
        self.assertIn("reuse the same launcher", out.lower())
        self.assertIn(
            "uvx --from 'elastic-observability-migration[all]' obs-migrate list-samples",
            out,
        )
        self.assertNotRegex(out, r"(?m)^  obs-migrate list-samples$")

    def test_root_help_mentions_single_cli_quick_start(self):
        parser = app_cli._build_parser()
        help_text = parser.format_help()
        self.assertIn("obs-migrate doctor", help_text)
        self.assertIn("list-samples", help_text)
        self.assertIn("Prefer this `obs-migrate` entry point", help_text)


if __name__ == "__main__":
    unittest.main()
