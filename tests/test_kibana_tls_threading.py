# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest
from unittest.mock import MagicMock, patch

import requests

from observability_migration.targets.kibana import alerting, serverless, smoke


class TestKibanaTlsThreading(unittest.TestCase):
    @patch("observability_migration.targets.kibana.serverless.requests.Session")
    def test_serverless_list_data_views_default_verify(self, mock_session_cls):
        session = requests.Session()
        mock_session_cls.return_value = session
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data_view": []}
        session.get = MagicMock(return_value=response)

        serverless.list_data_views("https://kb.test")

        self.assertIs(session.verify, True)

    @patch("observability_migration.targets.kibana.serverless.requests.Session")
    def test_serverless_list_data_views_custom_ca_verify(self, mock_session_cls):
        session = requests.Session()
        mock_session_cls.return_value = session
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data_view": []}
        session.get = MagicMock(return_value=response)

        serverless.list_data_views("https://kb.test", verify="/tmp/ca.pem")

        self.assertEqual(session.verify, "/tmp/ca.pem")

    @patch("observability_migration.targets.kibana.smoke.requests.Session")
    @patch("observability_migration.targets.kibana.smoke.grafana_smoke.load_dashboards")
    @patch("observability_migration.targets.kibana.smoke.grafana_smoke.should_include_dashboard")
    @patch("observability_migration.targets.kibana.smoke.grafana_smoke.inspect_dashboard")
    @patch("observability_migration.targets.kibana.smoke.grafana_smoke.build_summary")
    def test_smoke_run_smoke_report_threads_verify(
        self,
        mock_build_summary,
        mock_inspect_dashboard,
        mock_should_include,
        mock_load_dashboards,
        mock_session_cls,
    ):
        session = requests.Session()
        mock_session_cls.return_value = session
        mock_load_dashboards.return_value = [
            {"id": "dash-1", "attributes": {"panelsJSON": "[]", "title": "Test"}},
        ]
        mock_should_include.return_value = True
        mock_inspect_dashboard.return_value = {"title": "Test", "panels": []}
        mock_build_summary.return_value = {"dashboards": 1}

        smoke.run_smoke_report(
            kibana_url="https://kb.test",
            es_url="https://es.test",
            output_path="",
            verify="/tmp/ca.pem",
        )

        self.assertEqual(session.verify, "/tmp/ca.pem")

    @patch("observability_migration.targets.kibana.smoke.requests.Session")
    @patch("observability_migration.targets.kibana.smoke.grafana_smoke.load_dashboards")
    @patch("observability_migration.targets.kibana.smoke.grafana_smoke.should_include_dashboard")
    @patch("observability_migration.targets.kibana.smoke.grafana_smoke.inspect_dashboard")
    @patch("observability_migration.targets.kibana.smoke.grafana_smoke.build_summary")
    def test_smoke_run_smoke_report_default_verify(
        self,
        mock_build_summary,
        mock_inspect_dashboard,
        mock_should_include,
        mock_load_dashboards,
        mock_session_cls,
    ):
        session = requests.Session()
        mock_session_cls.return_value = session
        mock_load_dashboards.return_value = [
            {"id": "dash-1", "attributes": {"panelsJSON": "[]", "title": "Test"}},
        ]
        mock_should_include.return_value = True
        mock_inspect_dashboard.return_value = {"title": "Test", "panels": []}
        mock_build_summary.return_value = {"dashboards": 1}

        smoke.run_smoke_report(
            kibana_url="https://kb.test",
            es_url="https://es.test",
            output_path="",
        )

        self.assertIs(session.verify, True)

    @patch("observability_migration.targets.kibana.alerting.requests.Session")
    def test_alerting_list_rules_default_verify(self, mock_session_cls):
        session = requests.Session()
        mock_session_cls.return_value = session
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": [], "total": 0}
        session.get = MagicMock(return_value=response)

        alerting.list_rules("https://kb.test")

        self.assertIs(session.verify, True)

    @patch("observability_migration.targets.kibana.alerting.requests.Session")
    def test_alerting_list_rules_custom_ca_verify(self, mock_session_cls):
        session = requests.Session()
        mock_session_cls.return_value = session
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"data": [], "total": 0}
        session.get = MagicMock(return_value=response)

        alerting.list_rules("https://kb.test", verify="/tmp/ca.pem")

        self.assertEqual(session.verify, "/tmp/ca.pem")


if __name__ == "__main__":
    unittest.main()
