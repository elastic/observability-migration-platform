# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the Kibana target adapter runtime behavior."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from observability_migration.core.assets.native_dashboard import NativeDashboard, NativeGrid, NativePanel
from observability_migration.targets.kibana.adapter import KibanaTargetAdapter
from observability_migration.targets.kibana.dashboards_api import UploadResult


def _native_artifact_dir(tmpdir: str) -> Path:
    """A dashboard artifact root holding one ``native/*.native.json``."""
    native_dir = Path(tmpdir) / "native"
    native_dir.mkdir(parents=True)
    native_dir.joinpath("dash.native.json").write_text(
        json.dumps(
            {
                "kind": "native_dashboard",
                "version": 1,
                "dashboard_id": "obs-migrate-dash",
                "title": "Dash",
                "payload": {
                    "title": "Dash",
                    "panels": [
                        {"grid": {"x": 0, "y": 0, "w": 24, "h": 8}, "type": "vis", "config": {"type": "metric"}},
                    ],
                },
                "mapping": {"mapped": 1, "unmapped": 0, "sections": 0, "controls": 0, "reasons": {}},
            }
        ),
        encoding="utf-8",
    )
    return Path(tmpdir)


class TestKibanaTargetAdapterUpload(unittest.TestCase):
    """The data views a dashboard references must exist *before* it is sent.

    Uploading first and creating data views afterwards leaves every
    data-view-backed control pointing at an id Kibana does not have yet, which
    renders as "An error occurred" in the browser. The ordering is therefore
    pinned on both upload entry points.
    """

    def test_upload_ensures_data_views_before_dashboard_upload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _native_artifact_dir(tmpdir)
            call_order: list[str] = []

            def fake_ensure(*args, **kwargs):
                call_order.append("ensure")
                return [{"id": "metrics-*", "title": "metrics-*"}]

            def fake_upload(*args, **kwargs):
                call_order.append("upload")
                return UploadResult(
                    dashboard="Dash", dashboard_id="obs-migrate-dash", status="created", mapped=1,
                )

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                side_effect=fake_ensure,
            ) as ensure_data_views, mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
                side_effect=fake_upload,
            ):
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
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
        native_dashboard = NativeDashboard(
            title="Dash",
            dashboard_id="obs-migrate-dash",
            items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
        )
        call_order: list[str] = []

        def fake_ensure(*args, **kwargs):
            call_order.append("ensure")
            return [{"id": "metrics-*", "title": "metrics-*"}]

        def fake_upload(*args, **kwargs):
            call_order.append("upload")
            return UploadResult(
                dashboard="Dash", dashboard_id="obs-migrate-dash", status="created", mapped=1,
            )

        with mock.patch(
            "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
            side_effect=fake_ensure,
        ) as ensure_data_views, mock.patch(
            "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_dashboard",
            side_effect=fake_upload,
        ):
            payload = KibanaTargetAdapter().upload_dashboard(
                kibana_url="https://kibana.example",
                kibana_api_key="secret",
                space_id="shadow",
                native_dashboard=native_dashboard,
                native_dashboard_stats={"mapped": 1, "unmapped": 0, "reasons": {}},
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


if __name__ == "__main__":
    unittest.main()
