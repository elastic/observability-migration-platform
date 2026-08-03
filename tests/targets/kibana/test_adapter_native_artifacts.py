# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for ``KibanaTargetAdapter.upload`` with persisted native artifacts.

Covers ``--artifact-format`` discovery/dispatch (``auto``/``native``/``yaml``)
against the shapes ``native_artifacts.py`` writes: a dashboard artifact root
holding ``native/`` and ``yaml/`` children, or either child directory passed
directly.
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


def _artifact_root_with_native_and_yaml(tmpdir: str) -> Path:
    root = Path(tmpdir)
    native_dir = root / "native"
    yaml_dir = root / "yaml"
    native_dir.mkdir(parents=True)
    yaml_dir.mkdir(parents=True)
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
    yaml_dir.joinpath("dash.yaml").write_text("dashboards:\n- name: Dash\n  panels: []\n", encoding="utf-8")
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
            root = _artifact_root_with_native_and_yaml(tmpdir)
            found = _resolve_native_artifact_files(root)
        self.assertEqual([p.name for p in found], ["dash.native.json"])

    def test_ignores_index_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native_and_yaml(tmpdir)
            found = _resolve_native_artifact_files(root / "native")
        self.assertEqual([p.name for p in found], ["dash.native.json"])

    def test_returns_empty_when_no_native_artifacts_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_only = Path(tmpdir)
            (yaml_only / "dash.yaml").write_text("dashboards: []", encoding="utf-8")
            found = _resolve_native_artifact_files(yaml_only)
        self.assertEqual(found, [])


class TestUploadArtifactFormat(unittest.TestCase):
    def _upload(self, path: Path, **kwargs):
        with mock.patch.object(KibanaTargetAdapter, "_ensure_default_data_views", return_value=[]):
            return KibanaTargetAdapter().upload(
                path,
                kibana_url="https://kibana.example",
                kibana_api_key="secret",
                space_id="shadow",
                **kwargs,
            )

    def test_auto_prefers_native_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native_and_yaml(tmpdir)
            with mock.patch.object(
                adapter_module.dashboards_api,
                "upload_native_artifact",
                return_value=UploadResult(dashboard="Dash", dashboard_id="obs-migrate-dash", status="created", mapped=1),
            ) as native_upload, mock.patch.object(
                adapter_module.dashboards_api, "upload_yaml_files",
            ) as yaml_upload:
                payload = self._upload(root)

        native_upload.assert_called_once()
        yaml_upload.assert_not_called()
        self.assertEqual(payload["summary"]["artifact_format"], "native")
        self.assertEqual(payload["records"][0]["yaml_file"], "dash.native.json")
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)

    def test_auto_rejects_mismatched_native_and_yaml_sets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native_and_yaml(tmpdir)
            (root / "yaml" / "yaml-only.yaml").write_text(
                "dashboards:\n- name: YAML Only\n  panels: []\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                adapter_module.dashboards_api,
                "upload_native_artifact",
            ) as native_upload, mock.patch.object(
                adapter_module.dashboards_api,
                "upload_yaml_files",
            ) as yaml_upload:
                payload = self._upload(root)

        native_upload.assert_not_called()
        yaml_upload.assert_not_called()
        self.assertEqual(payload["summary"]["error"], "mixed_native_yaml_artifacts")
        self.assertEqual(payload["summary"]["missing_native_artifacts"], ["yaml-only"])
        self.assertEqual(payload["records"], [])

    def test_auto_allows_direct_native_directory_with_yaml_sibling_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native_and_yaml(tmpdir)
            (root / "yaml" / "yaml-only.yaml").write_text(
                "dashboards:\n- name: YAML Only\n  panels: []\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                adapter_module.dashboards_api,
                "upload_native_artifact",
                return_value=UploadResult(dashboard="Dash", dashboard_id="obs-migrate-dash", status="created", mapped=1),
            ) as native_upload:
                payload = self._upload(root / "native")

        native_upload.assert_called_once()
        self.assertEqual(payload["summary"]["artifact_format"], "native")
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)

    def test_explicit_native_format_uses_native_artifact_upload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native_and_yaml(tmpdir)
            with mock.patch.object(
                adapter_module.dashboards_api,
                "upload_native_artifact",
                return_value=UploadResult(dashboard="Dash", dashboard_id="obs-migrate-dash", status="created", mapped=1),
            ) as native_upload:
                payload = self._upload(root, artifact_format="native")

        native_upload.assert_called_once()
        self.assertEqual(payload["records"][0]["status"], "created")
        self.assertEqual(payload["records"][0]["dashboard_ids"], ["obs-migrate-dash"])

    def test_explicit_yaml_format_ignores_native_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native_and_yaml(tmpdir)

            def fake_yaml_api(yaml_paths, kibana_url, *, fallback=None, **kwargs):
                return [UploadResult(dashboard="Dash", dashboard_id="d1", status="created", mapped=1)]

            with mock.patch.object(
                adapter_module.dashboards_api, "upload_native_artifact",
            ) as native_upload, mock.patch.object(
                adapter_module.dashboards_api, "upload_yaml_files", side_effect=fake_yaml_api,
            ) as yaml_upload:
                payload = self._upload(root, artifact_format="yaml")

        native_upload.assert_not_called()
        yaml_upload.assert_called_once()
        self.assertEqual(payload["records"][0]["yaml_file"], "dash.yaml")

    def test_native_format_with_only_yaml_present_errors_clearly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_only = Path(tmpdir)
            (yaml_only / "dash.yaml").write_text("dashboards: []", encoding="utf-8")
            payload = self._upload(yaml_only, artifact_format="native")

        self.assertEqual(payload["summary"]["total"], 0)
        self.assertEqual(payload["summary"]["error"], "no_native_artifacts_found")
        self.assertEqual(payload["records"], [])

    def test_legacy_import_forces_yaml_even_when_native_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native_and_yaml(tmpdir)

            with mock.patch.object(
                adapter_module.dashboards_api, "upload_native_artifact",
            ) as native_upload, mock.patch.object(
                adapter_module, "upload_yaml", return_value=(True, "ok"),
            ) as legacy_upload:
                payload = self._upload(root, use_dashboards_api=False, artifact_format="native")

        native_upload.assert_not_called()
        legacy_upload.assert_called_once()
        self.assertEqual(payload["records"][0]["yaml_file"], "dash.yaml")

    def test_native_upload_payload_sent_matches_persisted_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = _artifact_root_with_native_and_yaml(tmpdir)
            captured = {}

            def fake_upload_native_artifact(artifact, kibana_url, **kwargs):
                captured["artifact"] = artifact
                return UploadResult(dashboard="Dash", dashboard_id="obs-migrate-dash", status="created", mapped=1)

            with mock.patch.object(
                adapter_module.dashboards_api,
                "upload_native_artifact",
                side_effect=fake_upload_native_artifact,
            ):
                self._upload(root, artifact_format="native")

            on_disk = json.loads((root / "native" / "dash.native.json").read_text())
        self.assertEqual(captured["artifact"], on_disk)


if __name__ == "__main__":
    unittest.main()
