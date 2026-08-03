# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the native Dashboards API upload path of the adapter.

The typed Dashboards API is the only upload path: ``upload()`` deploys the
persisted ``native/*.native.json`` review artifacts, and ``upload_dashboard()``
deploys the in-memory ``NativeDashboard`` a migration just built. There is no
YAML artifact and no second deploy path to degrade to, so a rejected payload is
reported as rejected.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from observability_migration.core.assets.native_dashboard import NativeDashboard, NativeGrid, NativePanel
from observability_migration.targets.kibana.adapter import KibanaTargetAdapter
from observability_migration.targets.kibana.dashboards_api import DroppedPanel, UploadResult

# The record/summary keys every upload record carries regardless of status.
_BASE_RECORD_KEYS = {"artifact", "success", "output", "space_id", "kibana_url"}
_BASE_SUMMARY_KEYS = {"uploaded_ok", "total", "space_id", "kibana_url"}


def _artifact_dir_with_one_dashboard(tmpdir: str) -> Path:
    """A dashboard artifact root holding one ``native/dash.native.json``."""
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


class TestNativeArtifactUpload(unittest.TestCase):
    def test_upload_calls_the_typed_dashboards_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir_with_one_dashboard(tmpdir)

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
                return_value=UploadResult(dashboard="Dash", dashboard_id="d1", status="created", mapped=3),
            ) as api:
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                )

        api.assert_called_once()
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)
        self.assertEqual(payload["summary"]["total"], 1)

    def test_record_and_summary_shape_is_native(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir_with_one_dashboard(tmpdir)

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
                return_value=UploadResult(dashboard="Dash", dashboard_id="d1", status="created", mapped=2),
            ):
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                )

        self.assertTrue(_BASE_SUMMARY_KEYS.issubset(set(payload["summary"])))
        self.assertEqual(payload["summary"]["artifact_format"], "native")
        self.assertTrue(_BASE_RECORD_KEYS.issubset(set(payload["records"][0])))

    def test_native_upload_reports_status_mapped_and_dashboard_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir_with_one_dashboard(tmpdir)

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
                return_value=UploadResult(dashboard="Dash", dashboard_id="d1", status="created", mapped=3),
            ) as api:
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                )

        api.assert_called_once()
        record = payload["records"][0]
        self.assertEqual(record["status"], "created")
        self.assertEqual(record["mapped"], 3)
        self.assertTrue(record["success"])
        self.assertEqual(record["dashboard_ids"], ["d1"])
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)

    def test_conflict_result_is_a_terminal_failure(self):
        # A 409 "conflict" is a cluster-global shareable-id collision from
        # another space, not a payload defect. Nothing can resolve it at upload
        # time, so the adapter must report it as a terminal (non-success) status
        # rather than retrying or overwriting.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir_with_one_dashboard(tmpdir)

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
                return_value=UploadResult(dashboard="Dash", status="conflict", http_status=409, mapped=1),
            ):
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    space_id="shadow",
                )

        record = payload["records"][0]
        self.assertEqual(record["status"], "conflict")
        self.assertFalse(record["success"])
        self.assertEqual(payload["summary"]["uploaded_ok"], 0)

    def test_lossy_result_is_not_a_success_and_names_the_dropped_panel(self):
        # HTTP 200 with panels missing. The record must fail (a partial write
        # that reports success is never investigated) and carry the per-panel
        # detail so the operator can see which panels Kibana silently dropped.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir_with_one_dashboard(tmpdir)
            lossy = UploadResult(
                dashboard="Dash",
                dashboard_id="d1",
                status="lossy",
                http_status=200,
                mapped=2,
                message="Kibana accepted the upload but kept only 1 of 2 panel(s)",
                panels_sent=2,
                panels_accepted=1,
                dropped_panels=[
                    DroppedPanel(
                        title="Memory usage",
                        reason="Unable to transform panel config. Error: [color]",
                        section="Overview",
                        grid={"x": 12, "y": 0, "w": 12, "h": 6},
                    )
                ],
            )

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
                return_value=lossy,
            ):
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                )

        record = payload["records"][0]
        self.assertEqual(record["status"], "lossy")
        self.assertFalse(record["success"])
        self.assertEqual(record["panels_sent"], 2)
        self.assertEqual(record["panels_accepted"], 1)
        self.assertEqual([d["title"] for d in record["dropped_panels"]], ["Memory usage"])
        self.assertIn("kept only 1 of 2", record["output"])
        self.assertEqual(payload["summary"]["uploaded_ok"], 0)
        self.assertEqual(payload["summary"]["panels_dropped"], 1)

    def test_intact_upload_reports_no_dropped_panels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir_with_one_dashboard(tmpdir)

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
                return_value=UploadResult(
                    dashboard="Dash",
                    dashboard_id="d1",
                    status="updated",
                    http_status=200,
                    mapped=2,
                    panels_sent=2,
                    panels_accepted=2,
                ),
            ):
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                )

        record = payload["records"][0]
        self.assertTrue(record["success"])
        self.assertEqual(record["dropped_panels"], [])
        self.assertEqual(payload["summary"]["panels_dropped"], 0)

    def test_native_records_carry_exactly_the_documented_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir_with_one_dashboard(tmpdir)

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
                return_value=UploadResult(dashboard="Dash", dashboard_id="d1", status="created", mapped=2),
            ):
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                )

        record = payload["records"][0]
        self.assertTrue(_BASE_RECORD_KEYS.issubset(set(record)))
        self.assertTrue(_BASE_SUMMARY_KEYS.issubset(set(payload["summary"])))
        self.assertEqual(
            set(record) - _BASE_RECORD_KEYS,
            {
                "status",
                "mapped",
                "unmapped",
                "unmapped_reasons",
                "dashboard_ids",
                # Silent-panel-loss evidence: leaves sent vs leaves Kibana kept,
                # plus the per-panel detail for the ones it dropped.
                "panels_sent",
                "panels_accepted",
                "dropped_panels",
            },
        )


class TestUploadDashboardNativePath(unittest.TestCase):
    def test_upload_dashboard_requires_a_native_payload(self):
        # There is no artifact on disk to fall back to reading: without a
        # payload there is nothing to upload, and a silent no-op would be
        # reported as a successful upload by the caller.
        with mock.patch(
            "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
            return_value=[{"id": "metrics-*", "title": "metrics-*"}],
        ) as ensure_data_views:
            with self.assertRaises(ValueError):
                KibanaTargetAdapter().upload_dashboard(
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                    native_dashboard=None,
                )
        ensure_data_views.assert_not_called()

    def test_upload_dashboard_sends_the_in_memory_native_dashboard(self):
        native_dashboard = NativeDashboard(
            title="Direct IR",
            dashboard_id="direct-ir",
            items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
        )
        with mock.patch(
            "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
            return_value=[{"id": "metrics-*", "title": "metrics-*"}],
        ), mock.patch(
            "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_dashboard",
            return_value=UploadResult(dashboard="Direct IR", dashboard_id="direct-ir", status="updated", mapped=1),
        ) as native_api:
            result = KibanaTargetAdapter().upload_dashboard(
                kibana_url="https://kibana.example",
                kibana_api_key="secret",
                native_dashboard=native_dashboard,
                native_dashboard_stats={"mapped": 1, "unmapped": 0, "reasons": {}},
            )

        native_api.assert_called_once()
        self.assertIs(native_api.call_args.args[0], native_dashboard)
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["dashboard_ids"], ["direct-ir"])

    def test_upload_dashboard_passes_resolved_data_view_ids_to_native_upload(self):
        # Regression test for PR #278 review: a data-view-backed control's
        # `data_view_id` must be resolved from the created data view's title
        # to its Kibana-assigned id (which can differ for wildcard titles).
        # Verifies the adapter wires the title->id lookup through to
        # `upload_native_dashboard`; the lookup's own rewrite logic is
        # covered directly in test_dashboards_api.py.
        native_dashboard = NativeDashboard(
            title="Direct IR",
            dashboard_id="direct-ir",
            items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
        )
        with mock.patch(
            "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
            # Kibana assigned a different id than the wildcard title the control uses.
            return_value=[{"id": "generated-id", "title": "metrics-*"}],
        ), mock.patch(
            "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_dashboard",
            return_value=UploadResult(dashboard="Direct IR", dashboard_id="direct-ir", status="updated", mapped=1),
        ) as native_api:
            KibanaTargetAdapter().upload_dashboard(
                kibana_url="https://kibana.example",
                kibana_api_key="secret",
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


def test_referenced_data_view_patterns_are_collected_from_the_payload():
    """Ensure the data views the payload actually asks for, not a fixed list.

    _ensure_default_data_views only ensured metrics-prometheus-* / metrics-* /
    logs-*. A control pointing anywhere else -- the Datadog prometheus_native
    profile uses ``metrics-*.prometheus-*`` -- had nothing to resolve against,
    so the raw pattern stayed in ``data_view_id`` and Kibana rendered the
    control as "An error occurred".
    """
    from observability_migration.core.assets.native_dashboard import (
        NativeControl,
        NativeDashboard,
    )
    from observability_migration.targets.kibana.adapter import (
        _referenced_data_view_patterns,
    )

    dashboard = NativeDashboard(title="t")
    dashboard.controls = [
        NativeControl(
            type="options_list_control",
            config={"data_view_id": "metrics-*.prometheus-*", "field_name": "labels.instance"},
        )
    ]
    assert _referenced_data_view_patterns(dashboard) == ["metrics-*.prometheus-*"]


def test_referenced_data_view_patterns_handles_no_dashboard():
    from observability_migration.targets.kibana.adapter import (
        _referenced_data_view_patterns,
    )

    assert _referenced_data_view_patterns(None) == []
