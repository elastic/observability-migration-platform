# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the Kibana target adapter runtime behavior."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from observability_migration.targets.kibana.adapter import KibanaTargetAdapter


class TestKibanaTargetAdapterUpload(unittest.TestCase):
    def test_upload_ensures_data_views_before_dashboard_upload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = Path(tmpdir)
            yaml_path = yaml_dir / "dash.yaml"
            yaml_path.write_text("dashboards: []", encoding="utf-8")
            call_order: list[str] = []

            def fake_ensure(*args, **kwargs):
                call_order.append("ensure")
                return [{"id": "metrics-*", "title": "metrics-*"}]

            def fake_upload(*args, **kwargs):
                call_order.append("upload")
                return True, "ok"

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                side_effect=fake_ensure,
            ) as ensure_data_views, mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                side_effect=fake_upload,
            ):
                payload = KibanaTargetAdapter().upload(
                    yaml_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                )

        self.assertEqual(call_order, ["ensure", "upload"])
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)
        ensure_data_views.assert_called_once_with(
            "https://kibana.example",
            data_view_patterns=None,
            api_key="secret",
            space_id="shadow",
            verify=True,
        )

    def test_upload_dashboard_ensures_data_views_before_dashboard_upload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "dash.yaml"
            output_dir = Path(tmpdir) / "compiled"
            yaml_path.write_text("dashboards: []", encoding="utf-8")
            call_order: list[str] = []

            def fake_ensure(*args, **kwargs):
                call_order.append("ensure")
                return [{"id": "metrics-*", "title": "metrics-*"}]

            def fake_upload(*args, **kwargs):
                call_order.append("upload")
                return True, "ok"

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                side_effect=fake_ensure,
            ) as ensure_data_views, mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                side_effect=fake_upload,
            ):
                payload = KibanaTargetAdapter().upload_dashboard(
                    yaml_path,
                    output_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                )

        self.assertEqual(call_order, ["ensure", "upload"])
        self.assertTrue(payload["success"])
        ensure_data_views.assert_called_once_with(
            "https://kibana.example",
            data_view_patterns=None,
            api_key="secret",
            space_id="shadow",
            verify=True,
        )

    def test_upload_dashboard_rewrites_data_view_references_to_created_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "dash.yaml"
            output_dir = Path(tmpdir) / "compiled"
            yaml_path.write_text(
                """
dashboards:
- name: Dash
  panels:
  - title: Lens
    lens:
      type: line
      data_view: metrics-*
  - title: Query
    esql:
      type: datatable
      query: |
        FROM metrics-*
        | STATS value = COUNT(*)
""",
                encoding="utf-8",
            )
            uploaded_yaml = ""

            def fake_upload(upload_yaml_path, *args, **kwargs):
                nonlocal uploaded_yaml
                uploaded_yaml = Path(upload_yaml_path).read_text(encoding="utf-8")
                return True, "ok"

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "generated-id", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                side_effect=fake_upload,
            ):
                payload = KibanaTargetAdapter().upload_dashboard(
                    yaml_path,
                    output_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                )

        self.assertTrue(payload["success"])
        self.assertIn("data_view: generated-id", uploaded_yaml)
        self.assertIn("FROM metrics-*", uploaded_yaml)


if __name__ == "__main__":
    unittest.main()
