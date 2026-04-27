"""Unit tests for the Serverless Kibana API helpers."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from observability_migration.targets.kibana.serverless import (
    _api_base,
    create_data_view,
    delete_dashboards,
    detect_serverless,
    ensure_data_view,
    list_dashboards,
    list_data_views,
)


class TestApiBase(unittest.TestCase):
    def test_default_space(self):
        base = _api_base("https://my-kibana.cloud.es.io")
        self.assertEqual(base, "https://my-kibana.cloud.es.io")

    def test_with_space(self):
        base = _api_base("https://my-kibana.cloud.es.io", "production")
        self.assertIn("/s/production", base)


class TestListDashboards(unittest.TestCase):
    @patch("observability_migration.targets.kibana.serverless._session")
    def test_returns_sorted_dashboards(self, mock_session_fn):
        session = MagicMock()
        mock_session_fn.return_value = session

        ndjson = "\n".join([
            json.dumps({"type": "dashboard", "id": "d2", "attributes": {"title": "Zulu"}}),
            json.dumps({"type": "dashboard", "id": "d1", "attributes": {"title": "Alpha"}}),
            json.dumps({"type": "index-pattern", "id": "ip1", "attributes": {"title": "metrics-*"}}),
        ])
        response = MagicMock()
        response.text = ndjson
        response.raise_for_status = MagicMock()
        session.post.return_value = response

        result = list_dashboards("https://kb.test")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["attributes"]["title"], "Alpha")
        self.assertEqual(result[1]["attributes"]["title"], "Zulu")

    @patch("observability_migration.targets.kibana.serverless._session")
    def test_filters_non_dashboard_types(self, mock_session_fn):
        session = MagicMock()
        mock_session_fn.return_value = session

        ndjson = "\n".join([
            json.dumps({"type": "dashboard", "id": "d1", "attributes": {"title": "My Dash"}}),
            json.dumps({"type": "lens", "id": "l1", "attributes": {"title": "A Lens"}}),
            json.dumps({"exportedCount": 2}),
        ])
        response = MagicMock()
        response.text = ndjson
        response.raise_for_status = MagicMock()
        session.post.return_value = response

        result = list_dashboards("https://kb.test")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "d1")


class TestDeleteDashboards(unittest.TestCase):
    @patch("observability_migration.targets.kibana.serverless.import_saved_objects")
    def test_clears_dashboards_via_overwrite(self, mock_import):
        mock_import.return_value = {"success": True}

        result = delete_dashboards("https://kb.test", ["dash-1", "dash-2"])
        self.assertEqual(len(result["cleared"]), 2)
        self.assertEqual(len(result["failed"]), 0)
        self.assertIn("does not support DELETE", result["note"])

    @patch("observability_migration.targets.kibana.serverless.import_saved_objects")
    def test_reports_import_failures(self, mock_import):
        mock_import.side_effect = Exception("Connection refused")

        result = delete_dashboards("https://kb.test", ["dash-1"])
        self.assertEqual(len(result["cleared"]), 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("Connection refused", result["failed"][0]["error"])


class TestListDataViews(unittest.TestCase):
    @patch("observability_migration.targets.kibana.serverless._session")
    def test_returns_data_views(self, mock_session_fn):
        session = MagicMock()
        mock_session_fn.return_value = session

        response = MagicMock()
        response.json.return_value = {
            "data_view": [
                {"id": "dv-1", "title": "metrics-*"},
                {"id": "dv-2", "title": "logs-*"},
            ]
        }
        response.raise_for_status = MagicMock()
        session.get.return_value = response

        result = list_data_views("https://kb.test")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "metrics-*")


class TestCreateDataView(unittest.TestCase):
    @patch("observability_migration.targets.kibana.serverless._session")
    def test_creates_data_view(self, mock_session_fn):
        session = MagicMock()
        mock_session_fn.return_value = session

        response = MagicMock()
        response.json.return_value = {
            "data_view": {"id": "new-dv", "title": "metrics-prom-*", "timeFieldName": "@timestamp"}
        }
        response.raise_for_status = MagicMock()
        session.post.return_value = response

        result = create_data_view("https://kb.test", title="metrics-prom-*")
        self.assertEqual(result["id"], "new-dv")

        call_args = session.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        self.assertEqual(body["data_view"]["title"], "metrics-prom-*")
        self.assertTrue(body["override"])


class TestEnsureDataView(unittest.TestCase):
    @patch("observability_migration.targets.kibana.serverless.create_data_view")
    @patch("observability_migration.targets.kibana.serverless.list_data_views")
    def test_returns_existing_without_creating(self, mock_list, mock_create):
        mock_list.return_value = [{"id": "existing", "title": "metrics-*"}]

        result = ensure_data_view("https://kb.test", title="metrics-*")
        self.assertEqual(result["id"], "existing")
        mock_create.assert_not_called()

    @patch("observability_migration.targets.kibana.serverless.create_data_view")
    @patch("observability_migration.targets.kibana.serverless.list_data_views")
    def test_creates_when_not_found(self, mock_list, mock_create):
        mock_list.return_value = [{"id": "other", "title": "logs-*"}]
        mock_create.return_value = {"id": "new", "title": "metrics-*"}

        result = ensure_data_view("https://kb.test", title="metrics-*")
        self.assertEqual(result["id"], "new")
        mock_create.assert_called_once()


class TestDetectServerless(unittest.TestCase):
    @patch("observability_migration.targets.kibana.serverless._session")
    def test_detects_serverless_from_find_response(self, mock_session_fn):
        session = MagicMock()
        mock_session_fn.return_value = session

        response = MagicMock()
        response.status_code = 400
        response.json.return_value = {"message": "This API is not available with the current configuration"}
        session.get.return_value = response

        self.assertTrue(detect_serverless("https://kb.test"))

    @patch("observability_migration.targets.kibana.serverless._session")
    def test_non_serverless_when_find_works(self, mock_session_fn):
        session = MagicMock()
        mock_session_fn.return_value = session

        response = MagicMock()
        response.status_code = 200
        session.get.return_value = response

        self.assertFalse(detect_serverless("https://kb.test"))

    @patch("observability_migration.targets.kibana.serverless._session")
    def test_returns_false_on_connection_error(self, mock_session_fn):
        session = MagicMock()
        mock_session_fn.return_value = session
        session.get.side_effect = ConnectionError("unreachable")

        self.assertFalse(detect_serverless("https://kb.test"))


if __name__ == "__main__":
    unittest.main()
