# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for ``KibanaTargetAdapter.upload`` with persisted native artifacts.

``native/*.native.json`` is the only upload input: covers discovery against the
shapes ``native_artifacts.py`` writes (a dashboard artifact root holding a
``native/`` child, or that child passed directly) and dispatch to the typed
Dashboards API.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from observability_migration.targets.kibana import adapter as adapter_module
from observability_migration.targets.kibana.adapter import (
    KibanaTargetAdapter,
    _resolve_native_artifact_files,
)
from observability_migration.targets.kibana.dashboards_api import UploadResult


def _artifact_root_with_native(tmpdir: str) -> Path:
    root = Path(tmpdir)
    native_dir = root / "native"
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
    native_dir.joinpath("index.json").write_text(
        json.dumps({"kind": "native_dashboard_index", "version": 1, "dashboards": []}),
        encoding="utf-8",
    )
    return root


class TestResolveNativeArtifactFiles(unittest.TestCase):
    def test_discovers_native_json_directly_in_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            native_dir = Path(tmpdir)
            (native_dir / "dash.native.json").write_text("{}", encoding="utf-8")
            found = _resolve_native_artifact_files(native_dir)
        self.assertEqual([p.name for p in found], ["dash.native.json"])

    def test_discovers_nested_native_child(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native(tmpdir)
            found = _resolve_native_artifact_files(root)
        self.assertEqual([p.name for p in found], ["dash.native.json"])

    def test_ignores_index_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native(tmpdir)
            found = _resolve_native_artifact_files(root / "native")
        self.assertEqual([p.name for p in found], ["dash.native.json"])

    def test_returns_empty_when_no_native_artifacts_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            unrelated = Path(tmpdir)
            (unrelated / "migration_report.json").write_text("{}", encoding="utf-8")
            found = _resolve_native_artifact_files(unrelated)
        self.assertEqual(found, [])


class TestUploadNativeArtifacts(unittest.TestCase):
    def _upload(self, path: Path, **kwargs):
        with mock.patch.object(KibanaTargetAdapter, "_ensure_default_data_views", return_value=[]):
            return KibanaTargetAdapter().upload(
                path,
                kibana_url="https://kibana.example",
                kibana_api_key="secret",
                space_id="shadow",
                **kwargs,
            )

    def test_native_artifacts_are_uploaded_through_the_typed_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native(tmpdir)
            with mock.patch.object(
                adapter_module.dashboards_api,
                "upload_native_artifact",
                return_value=UploadResult(dashboard="Dash", dashboard_id="obs-migrate-dash", status="created", mapped=1),
            ) as native_upload:
                payload = self._upload(root)

        native_upload.assert_called_once()
        self.assertEqual(payload["summary"]["artifact_format"], "native")
        self.assertEqual(payload["records"][0]["artifact"], "dash.native.json")
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)

    def test_native_upload_record_carries_status_and_dashboard_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native(tmpdir)
            with mock.patch.object(
                adapter_module.dashboards_api,
                "upload_native_artifact",
                return_value=UploadResult(dashboard="Dash", dashboard_id="obs-migrate-dash", status="created", mapped=1),
            ) as native_upload:
                payload = self._upload(root)

        native_upload.assert_called_once()
        self.assertEqual(payload["records"][0]["status"], "created")
        self.assertEqual(payload["records"][0]["dashboard_ids"], ["obs-migrate-dash"])

    def test_directory_without_native_artifacts_errors_clearly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            unrelated = Path(tmpdir)
            (unrelated / "migration_report.json").write_text("{}", encoding="utf-8")
            payload = self._upload(unrelated)

        self.assertEqual(payload["summary"]["total"], 0)
        self.assertEqual(payload["summary"]["error"], "no_native_artifacts_found")
        self.assertEqual(payload["records"], [])

    def test_native_upload_payload_sent_matches_persisted_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native(tmpdir)
            captured = {}

            def fake_upload_native_artifact(artifact, kibana_url, **kwargs):
                captured["artifact"] = artifact
                return UploadResult(dashboard="Dash", dashboard_id="obs-migrate-dash", status="created", mapped=1)

            with mock.patch.object(
                adapter_module.dashboards_api,
                "upload_native_artifact",
                side_effect=fake_upload_native_artifact,
            ):
                self._upload(root)

            on_disk = json.loads((root / "native" / "dash.native.json").read_text())
        self.assertEqual(captured["artifact"], on_disk)


if __name__ == "__main__":
    unittest.main()
