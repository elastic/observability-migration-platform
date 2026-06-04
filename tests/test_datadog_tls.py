# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import argparse
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from observability_migration.adapters.source.datadog import cli as datadog_cli
from observability_migration.adapters.source.datadog import extract as datadog_extract
from observability_migration.adapters.source.datadog import verification as datadog_verification
from observability_migration.adapters.source.datadog.execution import (
    _execute_metric_query,
    build_source_execution_summary,
)
from observability_migration.adapters.source.datadog.models import DashboardResult, TranslationResult


class TestDatadogTlsParser(unittest.TestCase):
    def test_insecure_flag(self):
        with mock.patch.dict(
            "os.environ",
            {"OBS_MIGRATE_INSECURE": "", "OBS_MIGRATE_CA_CERT": ""},
            clear=False,
        ):
            args = datadog_cli.parse_args(["--insecure"])
        self.assertTrue(args.insecure)

    def test_ca_cert_flag(self):
        with mock.patch.dict(
            "os.environ",
            {"OBS_MIGRATE_INSECURE": "", "OBS_MIGRATE_CA_CERT": ""},
            clear=False,
        ):
            args = datadog_cli.parse_args(["--ca-cert", "/tmp/ca.pem"])
        self.assertEqual(args.ca_cert, "/tmp/ca.pem")

    def test_defaults_without_flags(self):
        with mock.patch.dict(
            "os.environ",
            {"OBS_MIGRATE_INSECURE": "", "OBS_MIGRATE_CA_CERT": ""},
            clear=False,
        ):
            args = datadog_cli.parse_args([])
        self.assertFalse(args.insecure)
        self.assertEqual(args.ca_cert, "")


class TestDatadogTlsExecution(unittest.TestCase):
    @mock.patch("observability_migration.adapters.source.datadog.execution.requests.get")
    def test_execute_metric_query_passes_verify(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "series": [
                {
                    "metric": "system.cpu.user",
                    "scope": "*",
                    "pointlist": [[1710000000, 42.0]],
                }
            ],
        }

        _execute_metric_query(
            "avg:system.cpu.user{*}",
            api_key="k",
            app_key="a",
            site="datadoghq.com",
            timeout=30,
            verify="/tmp/ca.pem",
        )

        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs.get("verify"), "/tmp/ca.pem")

    @mock.patch("observability_migration.adapters.source.datadog.execution.requests.get")
    def test_build_source_execution_summary_threads_verify(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "series": [
                {
                    "metric": "system.cpu.user",
                    "scope": "*",
                    "pointlist": [[1710000000, 42.0]],
                }
            ],
        }
        panel = TranslationResult(
            widget_id="w1",
            source_panel_id="w1",
            title="CPU",
            kibana_type="metric",
            query_language="datadog_metric",
            source_queries=["avg:system.cpu.user{*}"],
        )

        build_source_execution_summary(
            panel,
            api_key="k",
            app_key="a",
            verify="/tmp/ca.pem",
        )

        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args.kwargs.get("verify"), "/tmp/ca.pem")


class TestDatadogSdkTlsExtraction(unittest.TestCase):
    @mock.patch("datadog_api_client.v1.api.dashboards_api.DashboardsApi")
    @mock.patch("datadog_api_client.ApiClient")
    def test_dashboard_api_extraction_sets_custom_ca_on_sdk_config(self, mock_client, mock_dashboards_api):
        mock_client.return_value.__enter__.return_value = mock.Mock()
        mock_dashboards_api.return_value.list_dashboards.return_value = SimpleNamespace(dashboards=[])

        datadog_extract.extract_dashboards_from_api(
            api_key="k",
            app_key="a",
            verify="/tmp/ca.pem",
        )

        config = mock_client.call_args.args[0]
        self.assertEqual(config.ssl_ca_cert, "/tmp/ca.pem")
        self.assertTrue(config.verify_ssl)

    @mock.patch("datadog_api_client.v1.api.monitors_api.MonitorsApi")
    @mock.patch("datadog_api_client.ApiClient")
    def test_monitor_api_extraction_can_disable_sdk_tls_verification(self, mock_client, mock_monitors_api):
        mock_client.return_value.__enter__.return_value = mock.Mock()
        mock_monitors_api.return_value.list_monitors_with_pagination.return_value = []

        datadog_extract.extract_monitors_from_api(
            api_key="k",
            app_key="a",
            verify=False,
        )

        config = mock_client.call_args.args[0]
        self.assertFalse(config.verify_ssl)


class TestDatadogTlsCliThreading(unittest.TestCase):
    def _args(self, **overrides):
        defaults = dict(
            kibana_url="https://kibana.example",
            kibana_api_key="secret",
            es_url="https://es.example",
            es_api_key="es-secret",
            space_id="space-a",
            ca_cert="/tmp/ca.pem",
            insecure=False,
            dashboard_ids="dash-1",
            delete_dashboards="dash-1",
            data_view="metrics-*",
            smoke_output="",
            smoke_timeout=30,
            browser_audit=False,
            capture_screenshots=False,
            chrome_binary="",
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_load_live_field_capabilities_threads_verify(self):
        field_map = mock.Mock()
        args = self._args()

        datadog_cli._load_live_field_capabilities(field_map, args)

        field_map.load_live_field_capabilities.assert_called_once_with(
            "https://es.example",
            es_api_key="es-secret",
            verify="/tmp/ca.pem",
        )

    def test_list_and_delete_dashboard_helpers_thread_verify(self):
        target_adapter = mock.Mock()
        target_adapter.list_dashboards.return_value = []
        target_adapter.delete_dashboards.return_value = {"cleared": [], "failed": [], "note": ""}

        datadog_cli._handle_list_dashboards(self._args(), target_adapter)
        datadog_cli._handle_delete_dashboards(self._args(), target_adapter)

        self.assertEqual(target_adapter.list_dashboards.call_args.kwargs.get("verify"), "/tmp/ca.pem")
        self.assertEqual(target_adapter.delete_dashboards.call_args.kwargs.get("verify"), "/tmp/ca.pem")

    def test_ensure_data_views_threads_verify(self):
        target_adapter = mock.Mock()
        target_adapter.ensure_data_views.return_value = []
        field_map = SimpleNamespace(metric_index="metrics-*", logs_index="logs-*")

        datadog_cli._ensure_data_views(self._args(), target_adapter, field_map)

        self.assertEqual(target_adapter.ensure_data_views.call_args.kwargs.get("verify"), "/tmp/ca.pem")

    def test_upload_threads_verify(self):
        target_adapter = mock.Mock()
        target_adapter.upload_dashboard.return_value = {
            "success": True,
            "output": "ok",
            "kibana_url": "https://kibana.example",
        }
        result = DashboardResult(
            yaml_path="dash.yaml",
            dashboard_title="Dash",
            dashboard_id="dash-1",
            compiled=True,
            compile_error="",
            layout_error="",
        )

        with TemporaryDirectory() as tmpdir:
            datadog_cli._upload_all_dashboards([result], Path(tmpdir), self._args(), target_adapter)

        self.assertEqual(target_adapter.upload_dashboard.call_args.kwargs.get("verify"), "/tmp/ca.pem")

    def test_smoke_threads_verify(self):
        target_adapter = mock.Mock()
        target_adapter.smoke.return_value = {
            "dashboards": [],
            "summary": {"total": 0},
        }
        result = DashboardResult(
            uploaded=True,
            dashboard_title="Dash",
            kibana_saved_object_id="",
            panel_results=[],
        )

        with TemporaryDirectory() as tmpdir:
            datadog_cli._smoke_uploaded_dashboards([result], Path(tmpdir), self._args(), target_adapter)

        self.assertEqual(target_adapter.smoke.call_args.kwargs.get("verify"), "/tmp/ca.pem")

    @mock.patch("observability_migration.adapters.source.datadog.cli.validate_query_with_fixes")
    def test_dashboard_validation_threads_verify(self, mock_validate):
        mock_validate.return_value = {
            "status": "pass",
            "query": "FROM metrics-* | LIMIT 1",
            "error": "",
            "fix_attempts": [],
            "analysis": {},
        }
        result = DashboardResult(
            dashboard_id="dash-1",
            dashboard_title="Dash",
            panel_results=[
                TranslationResult(
                    widget_id="w1",
                    source_panel_id="w1",
                    title="CPU",
                    esql_query="FROM metrics-* | LIMIT 1",
                )
            ],
        )
        field_map = SimpleNamespace(metric_index="metrics-*")

        datadog_cli._validate_all_dashboards(
            [(result, {})],
            field_map,
            self._args(),
            verify="/tmp/ca.pem",
        )

        self.assertEqual(mock_validate.call_args.kwargs.get("verify"), "/tmp/ca.pem")


class TestDatadogMonitorValidationTls(unittest.TestCase):
    def test_monitor_validation_threads_verify(self):
        validate_query_fn = mock.Mock(
            return_value={
                "status": "pass",
                "query": "FROM metrics-* | LIMIT 1",
                "error": "",
                "analysis": {},
                "fix_attempts": [],
            }
        )
        monitor_ir = SimpleNamespace(
            alert_id="mon-1",
            name="CPU",
            kind="datadog_metric_monitor",
            translated_query="FROM metrics-* | LIMIT 1",
        )

        datadog_verification.validate_monitor_queries(
            [monitor_ir],
            es_url="https://es.example",
            es_api_key="secret",
            validate_query_fn=validate_query_fn,
            verify="/tmp/ca.pem",
        )

        self.assertEqual(validate_query_fn.call_args.kwargs.get("verify"), "/tmp/ca.pem")


class TestDatadogVerificationTls(unittest.TestCase):
    @mock.patch("observability_migration.adapters.source.datadog.verification.build_source_execution_summary")
    def test_build_verification_packet_threads_verify_to_source_execution(self, mock_source_summary):
        mock_source_summary.return_value.to_dict.return_value = {"status": "pass"}
        panel = TranslationResult(
            widget_id="w1",
            source_panel_id="w1",
            title="CPU",
            kibana_type="metric",
            query_language="datadog_metric",
            source_queries=["avg:system.cpu.user{*}"],
            esql_query="FROM metrics-* | LIMIT 1",
        )
        dashboard = SimpleNamespace(
            dashboard_title="Dash",
            dashboard_uid="uid-1",
            build_runtime_summary=lambda: {"rollups": []},
        )

        datadog_verification.build_verification_packet(
            dashboard,
            panel,
            datadog_api_key="k",
            datadog_app_key="a",
            verify="/tmp/ca.pem",
        )

        self.assertEqual(mock_source_summary.call_args.kwargs.get("verify"), "/tmp/ca.pem")


if __name__ == "__main__":
    unittest.main()
