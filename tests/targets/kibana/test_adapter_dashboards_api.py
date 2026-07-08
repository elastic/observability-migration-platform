# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the default native Dashboards API upload path of the adapter.

The native typed Dashboards API is the default; ``--legacy-import`` opts back
into the kb-dashboard-cli saved-objects import, which also backs the
per-dashboard fallback when the native API rejects a payload.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from observability_migration.core.assets.native_dashboard import NativeDashboard, NativeGrid, NativePanel
from observability_migration.targets.kibana.adapter import KibanaTargetAdapter
from observability_migration.targets.kibana.dashboards_api import UploadResult

_LEGACY_RECORD_KEYS = {"yaml_file", "success", "output", "space_id", "kibana_url"}
_LEGACY_SUMMARY_KEYS = {"uploaded_ok", "total", "space_id", "kibana_url"}


def _yaml_dir_with_one_dashboard(tmpdir: str) -> Path:
    yaml_dir = Path(tmpdir)
    (yaml_dir / "dash.yaml").write_text(
        "dashboards:\n- name: Dash\n  panels: []\n", encoding="utf-8"
    )
    return yaml_dir


def _yaml_dir_with_two_dashboards(tmpdir: str) -> Path:
    yaml_dir = Path(tmpdir)
    (yaml_dir / "dash.yaml").write_text(
        "dashboards:\n"
        "- name: Good\n"
        "  panels: []\n"
        "- name: Bad\n"
        "  panels: []\n",
        encoding="utf-8",
    )
    return yaml_dir


class TestDefaultPathNative(unittest.TestCase):
    def test_default_path_calls_api_and_not_legacy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = _yaml_dir_with_one_dashboard(tmpdir)

            def fake_api(yaml_paths, kibana_url, *, fallback=None, **kwargs):
                return [UploadResult(dashboard="Dash", dashboard_id="d1", status="created", mapped=3)]

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                return_value=(True, "ok"),
            ) as legacy, mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_yaml_files",
                side_effect=fake_api,
            ) as api:
                payload = KibanaTargetAdapter().upload(
                    yaml_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                )

        api.assert_called_once()
        legacy.assert_not_called()
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)
        self.assertEqual(payload["summary"]["total"], 1)

    def test_default_record_and_summary_shape_is_native(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = _yaml_dir_with_one_dashboard(tmpdir)

            def fake_api(yaml_paths, kibana_url, *, fallback=None, **kwargs):
                return [UploadResult(dashboard="Dash", dashboard_id="d1", status="created", mapped=2)]

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                return_value=(True, "ok"),
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_yaml_files",
                side_effect=fake_api,
            ):
                payload = KibanaTargetAdapter().upload(
                    yaml_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                )

        self.assertTrue(_LEGACY_SUMMARY_KEYS.issubset(set(payload["summary"])))
        self.assertIn("fallbacks", payload["summary"])
        self.assertTrue(_LEGACY_RECORD_KEYS.issubset(set(payload["records"][0])))

    def test_legacy_import_opt_out_calls_legacy_and_not_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = _yaml_dir_with_one_dashboard(tmpdir)
            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                return_value=(True, "ok"),
            ) as legacy, mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_yaml_files",
            ) as api:
                payload = KibanaTargetAdapter().upload(
                    yaml_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                    use_dashboards_api=False,
                )

        legacy.assert_called_once()
        api.assert_not_called()
        self.assertEqual(set(payload["summary"]), _LEGACY_SUMMARY_KEYS)
        self.assertEqual(set(payload["records"][0]), _LEGACY_RECORD_KEYS)


class TestNativeApiPath(unittest.TestCase):
    def test_use_dashboards_api_calls_api_and_not_legacy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = _yaml_dir_with_one_dashboard(tmpdir)

            def fake_api(yaml_paths, kibana_url, *, fallback=None, **kwargs):
                return [UploadResult(dashboard="Dash", dashboard_id="d1", status="created", mapped=3)]

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                return_value=(True, "ok"),
            ) as legacy, mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_yaml_files",
                side_effect=fake_api,
            ) as api:
                payload = KibanaTargetAdapter().upload(
                    yaml_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                    use_dashboards_api=True,
                )

        api.assert_called_once()
        legacy.assert_not_called()
        record = payload["records"][0]
        self.assertEqual(record["status"], "created")
        self.assertEqual(record["mapped"], 3)
        self.assertFalse(record["fallback_used"])
        self.assertTrue(record["success"])
        self.assertEqual(record["dashboard_ids"], ["d1"])
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)
        self.assertEqual(payload["summary"]["fallbacks"], 0)

    def test_rejected_result_triggers_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = _yaml_dir_with_one_dashboard(tmpdir)
            seen_paths: list[str] = []

            def fake_api(yaml_paths, kibana_url, *, fallback=None, **kwargs):
                out = []
                for path in yaml_paths:
                    res = UploadResult(dashboard="Dash", status="rejected", mapped=1, unmapped=2)
                    if fallback is not None:
                        fallback(path, {"name": "Dash", "panels": []})
                    out.append(res)
                return out

            def fake_legacy(upload_yaml_path, *args, **kwargs):
                seen_paths.append(str(upload_yaml_path))
                return True, "legacy import ok"

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                side_effect=fake_legacy,
            ) as legacy, mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_yaml_files",
                side_effect=fake_api,
            ):
                payload = KibanaTargetAdapter().upload(
                    yaml_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                    use_dashboards_api=True,
                )

        legacy.assert_called_once()
        self.assertTrue(seen_paths)
        record = payload["records"][0]
        self.assertEqual(record["status"], "rejected")
        self.assertTrue(record["fallback_used"])
        self.assertTrue(record["success"])
        self.assertEqual(record["output"], "legacy import ok")
        self.assertEqual(payload["summary"]["fallbacks"], 1)

    def test_rejected_dashboard_fallback_splits_multi_dashboard_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = _yaml_dir_with_two_dashboards(tmpdir)
            legacy_docs: list[str] = []

            def fake_api(yaml_paths, kibana_url, *, fallback=None, **kwargs):
                assert fallback is not None
                fallback(yaml_paths[0], {"name": "Bad", "panels": []})
                return [
                    UploadResult(dashboard="Good", dashboard_id="good", status="created", mapped=1),
                    UploadResult(dashboard="Bad", status="rejected", mapped=0, unmapped=1),
                ]

            def fake_legacy(upload_yaml_path, *args, **kwargs):
                legacy_docs.append(Path(upload_yaml_path).read_text(encoding="utf-8"))
                return True, "legacy import ok"

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                side_effect=fake_legacy,
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_yaml_files",
                side_effect=fake_api,
            ):
                payload = KibanaTargetAdapter().upload(
                    yaml_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    use_dashboards_api=True,
                )

        self.assertEqual(len(legacy_docs), 1)
        self.assertIn("name: Bad", legacy_docs[0])
        self.assertNotIn("name: Good", legacy_docs[0])
        self.assertEqual(payload["summary"]["fallbacks"], 1)

    def test_empty_result_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = _yaml_dir_with_one_dashboard(tmpdir)

            def fake_api(yaml_paths, kibana_url, *, fallback=None, **kwargs):
                return [UploadResult(dashboard="Dash", status="empty")]

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                return_value=(True, "legacy ok"),
            ) as legacy, mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_yaml_files",
                side_effect=fake_api,
            ):
                payload = KibanaTargetAdapter().upload(
                    yaml_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    use_dashboards_api=True,
                )

        legacy.assert_called_once()
        record = payload["records"][0]
        self.assertTrue(record["fallback_used"])
        self.assertTrue(record["success"])

    def test_native_records_include_legacy_shape_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = _yaml_dir_with_one_dashboard(tmpdir)

            def fake_api(yaml_paths, kibana_url, *, fallback=None, **kwargs):
                return [UploadResult(dashboard="Dash", dashboard_id="d1", status="created", mapped=2)]

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                return_value=(True, "ok"),
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_yaml_files",
                side_effect=fake_api,
            ):
                payload = KibanaTargetAdapter().upload(
                    yaml_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    use_dashboards_api=True,
                )

        record = payload["records"][0]
        self.assertTrue(_LEGACY_RECORD_KEYS.issubset(set(record)))
        self.assertTrue(_LEGACY_SUMMARY_KEYS.issubset(set(payload["summary"])))
        self.assertEqual(
            set(record) - _LEGACY_RECORD_KEYS,
            {"status", "mapped", "unmapped", "fallback_used", "fallback_count", "dashboard_ids"},
        )


class TestUploadDashboardNativePath(unittest.TestCase):
    def test_upload_dashboard_uses_api_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "dash.yaml"
            yaml_path.write_text("dashboards:\n- name: Dash\n  panels: []\n", encoding="utf-8")
            out_dir = Path(tmpdir) / "compiled"

            def fake_api(yaml_paths, kibana_url, *, fallback=None, **kwargs):
                return [UploadResult(dashboard="Dash", dashboard_id="d9", status="created", mapped=1)]

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.upload_yaml",
                return_value=(True, "ok"),
            ) as legacy, mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_yaml_files",
                side_effect=fake_api,
            ) as api:
                result = KibanaTargetAdapter().upload_dashboard(
                    yaml_path,
                    out_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    use_dashboards_api=True,
                )

        api.assert_called_once()
        legacy.assert_not_called()
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["dashboard_ids"], ["d9"])

    def test_upload_dashboard_prefers_native_dashboard_ir_when_provided(self):
        native_dashboard = NativeDashboard(
            title="Direct IR",
            dashboard_id="direct-ir",
            items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "dash.yaml"
            yaml_path.write_text("dashboards:\n- name: Stale YAML\n  panels: []\n", encoding="utf-8")
            out_dir = Path(tmpdir) / "compiled"

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_dashboard",
                return_value=UploadResult(dashboard="Direct IR", dashboard_id="direct-ir", status="updated", mapped=1),
            ) as native_api, mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_yaml_files",
                side_effect=AssertionError("native upload must not re-read YAML when IR is available"),
            ):
                result = KibanaTargetAdapter().upload_dashboard(
                    yaml_path,
                    out_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    use_dashboards_api=True,
                    native_dashboard=native_dashboard,
                    native_dashboard_stats={"mapped": 1, "unmapped": 0, "reasons": {}},
                )

        native_api.assert_called_once()
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["dashboard_ids"], ["direct-ir"])

    def test_upload_dashboard_passes_resolved_data_view_ids_to_native_upload(self):
        # Regression test for PR #278 review: a data-view-backed control's
        # `data_view_id` must be resolved from the created data view's title
        # to its Kibana-assigned id (which can differ for wildcard titles),
        # on the native path -- not just the legacy `_prepare_upload_yaml`
        # path. Verifies the adapter wires the title->id lookup through to
        # `upload_native_dashboard`; the lookup's own rewrite logic is
        # covered directly in test_dashboards_api.py.
        native_dashboard = NativeDashboard(
            title="Direct IR",
            dashboard_id="direct-ir",
            items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = Path(tmpdir) / "dash.yaml"
            yaml_path.write_text("dashboards:\n- name: Stale YAML\n  panels: []\n", encoding="utf-8")
            out_dir = Path(tmpdir) / "compiled"

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                # Kibana assigned a different id than the wildcard title used in YAML controls.
                return_value=[{"id": "generated-id", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_dashboard",
                return_value=UploadResult(dashboard="Direct IR", dashboard_id="direct-ir", status="updated", mapped=1),
            ) as native_api:
                KibanaTargetAdapter().upload_dashboard(
                    yaml_path,
                    out_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    use_dashboards_api=True,
                    native_dashboard=native_dashboard,
                    native_dashboard_stats={"mapped": 1, "unmapped": 0, "reasons": {}},
                )

        native_api.assert_called_once()
        self.assertEqual(
            native_api.call_args.kwargs["data_view_ids"],
            {"metrics-*": "generated-id"},
        )


if __name__ == "__main__":
    unittest.main()
