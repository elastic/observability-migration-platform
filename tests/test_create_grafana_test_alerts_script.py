# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import importlib.util
import pathlib
import unittest
from unittest.mock import Mock, patch

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "create_grafana_test_alerts.py"


class CreateGrafanaTestAlertsScriptTests(unittest.TestCase):
    @staticmethod
    def _load_script_module():
        spec = importlib.util.spec_from_file_location("create_grafana_test_alerts_script", SCRIPT_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_main_dry_run_skips_network_calls(self):
        module = self._load_script_module()
        with patch.object(module.session, "post") as mock_post:
            exit_code = module.main(["--dry-run"])
        self.assertEqual(exit_code, 0)
        mock_post.assert_not_called()

    def test_main_handles_connection_errors_with_nonzero_exit(self):
        module = self._load_script_module()
        with patch.object(
            module.session,
            "post",
            side_effect=requests.exceptions.ConnectionError("connection refused"),
        ):
            exit_code = module.main(["--grafana-url", "http://localhost:1"])
        self.assertEqual(exit_code, 1)

    def test_ensure_folder_uid_creates_folder_when_missing(self):
        module = self._load_script_module()
        list_response = Mock()
        list_response.json.return_value = []
        list_response.raise_for_status.return_value = None

        create_response = Mock()
        create_response.status_code = 200
        create_response.content = b'{"uid":"new-folder"}'
        create_response.json.return_value = {"uid": "new-folder"}

        with patch.object(module.session, "get", return_value=list_response), patch.object(
            module.session,
            "post",
            return_value=create_response,
        ):
            resolved_uid = module._ensure_folder_uid(
                module.session,
                "http://localhost:13000",
                "missing-folder",
                "Migration Test Alerts",
                10.0,
            )

        self.assertEqual(resolved_uid, "new-folder")

    def test_ensure_folder_uid_reuses_existing_folder_title(self):
        module = self._load_script_module()
        list_response = Mock()
        list_response.json.return_value = [{"uid": "existing-title", "title": "Migration Test Alerts"}]
        list_response.raise_for_status.return_value = None

        with patch.object(module.session, "get", return_value=list_response), patch.object(
            module.session,
            "post",
        ) as mock_post:
            resolved_uid = module._ensure_folder_uid(
                module.session,
                "http://localhost:13000",
                "missing-folder",
                "Migration Test Alerts",
                10.0,
            )

        self.assertEqual(resolved_uid, "existing-title")
        mock_post.assert_not_called()

    def test_ensure_folder_uid_reloads_folder_after_conflict(self):
        module = self._load_script_module()
        first_list_response = Mock()
        first_list_response.json.return_value = []
        first_list_response.raise_for_status.return_value = None

        second_list_response = Mock()
        second_list_response.json.return_value = [{"uid": "reloaded-folder", "title": "Migration Test Alerts"}]
        second_list_response.raise_for_status.return_value = None

        create_response = Mock()
        create_response.status_code = 412
        create_response.raise_for_status.return_value = None

        with patch.object(
            module.session,
            "get",
            side_effect=[first_list_response, second_list_response],
        ) as mock_get, patch.object(
            module.session,
            "post",
            return_value=create_response,
        ):
            resolved_uid = module._ensure_folder_uid(
                module.session,
                "http://localhost:13000",
                "missing-folder",
                "Migration Test Alerts",
                10.0,
            )

        self.assertEqual(resolved_uid, "reloaded-folder")
        self.assertEqual(mock_get.call_count, 2)

    def test_ensure_folder_uid_rejects_create_response_without_uid(self):
        module = self._load_script_module()
        list_response = Mock()
        list_response.json.return_value = []
        list_response.raise_for_status.return_value = None

        create_response = Mock()
        create_response.status_code = 201
        create_response.content = b"{}"
        create_response.json.return_value = {}

        with patch.object(module.session, "get", return_value=list_response), patch.object(
            module.session,
            "post",
            return_value=create_response,
        ), self.assertRaises(requests.RequestException):
            module._ensure_folder_uid(
                module.session,
                "http://localhost:13000",
                "missing-folder",
                "Migration Test Alerts",
                10.0,
            )


if __name__ == "__main__":
    unittest.main()
