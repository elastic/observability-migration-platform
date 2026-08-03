# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""End-to-end coverage of the native/IR review artifacts the Grafana CLI
pipeline writes under ``<output-dir>/dashboards/{native,ir}/``.

``obs-migrate upload --artifact-dir ...`` deploys ``native/*.native.json``
byte-for-byte, so this file proves the pipeline persists exactly the payload a
reviewer inspects, and that the payload still structurally describes the
``DashboardIR`` it was built from (see ``tests/native_payload_guard.py`` for the
two cross-checks and why each exists).
"""

from __future__ import annotations

import json

from observability_migration.adapters.source.grafana import cli as grafana_cli
from observability_migration.core.assets.dashboard import DashboardIR
from tests.native_payload_guard import (
    assert_payload_matches_dict_shape_bridge,
    assert_payload_matches_ir,
)


def _write_dashboard(tmp_path):
    (tmp_path / "infra.json").write_text(
        json.dumps(
            {
                "title": "Native Artifact Dashboard",
                "uid": "native-artifact-1",
                "schemaVersion": 30,
                "panels": [
                    {
                        "title": "CPU Usage",
                        "type": "stat",
                        "gridPos": {"w": 12, "h": 8, "x": 0, "y": 0},
                        "targets": [
                            {"refId": "A", "expr": "sum(rate(node_cpu_seconds_total[5m]))", "instant": True},
                        ],
                    }
                ],
            }
        )
    )


def _run(tmp_path, out_dir):
    grafana_cli.main(
        [
            "--source", "files",
            "--input-dir", str(tmp_path),
            "--output-dir", str(out_dir),
            "--assets", "dashboards",
            "--field-profile", "otel",
        ]
    )


def _ir_by_stem(dashboards_dir):
    """``{artifact stem: DashboardIR}`` rebuilt from the persisted ir/ artifacts."""
    out = {}
    for ir_file in sorted((dashboards_dir / "ir").glob("*.ir.json")):
        stem = ir_file.name[: -len(".ir.json")]
        out[stem] = DashboardIR.from_dict(json.loads(ir_file.read_text())["dashboard_ir"])
    return out


class TestGrafanaCliNativeArtifacts:
    def test_migrate_without_upload_writes_native_and_ir_artifacts(self, tmp_path):
        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        dashboards_dir = out_dir / "dashboards"
        native_files = sorted((dashboards_dir / "native").glob("*.native.json"))
        ir_files = sorted((dashboards_dir / "ir").glob("*.ir.json"))
        assert len(native_files) == 1
        assert len(ir_files) == 1

    def test_native_artifact_payload_describes_the_persisted_ir(self, tmp_path):
        """The shipped payload must still describe the IR it was built from.

        This is the load-bearing structural cross-check on the artifact we
        upload: it compares the payload against the ``DashboardIR`` rather than
        re-running the mapper, so a panel or ES|QL query lost during mapping
        shows up here instead of being reproduced identically on both sides.
        """
        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        dashboards_dir = out_dir / "dashboards"
        irs = _ir_by_stem(dashboards_dir)
        native_files = sorted((dashboards_dir / "native").glob("*.native.json"))
        assert native_files
        for native_file in native_files:
            stem = native_file.name[: -len(".native.json")]
            artifact = json.loads(native_file.read_text())
            assert_payload_matches_ir(artifact["payload"], irs[stem], label=stem)

    def test_native_artifact_payload_matches_dict_shape_bridge(self, tmp_path):
        """Both mapper entry points must build the same payload from one IR.

        Pins the dashboard-level derivations (stable id, title, filters) that
        the per-panel IR guard above does not look at. In-memory dict shape --
        no YAML file is written or read.
        """
        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        dashboards_dir = out_dir / "dashboards"
        irs = _ir_by_stem(dashboards_dir)
        native_file = next((dashboards_dir / "native").glob("*.native.json"))
        stem = native_file.name[: -len(".native.json")]

        artifact = json.loads(native_file.read_text())
        assert_payload_matches_dict_shape_bridge(artifact["payload"], irs[stem], label=stem)

    def test_native_artifact_envelope_shape(self, tmp_path):
        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        native_file = next((out_dir / "dashboards" / "native").glob("*.native.json"))
        artifact = json.loads(native_file.read_text())

        assert artifact["kind"] == "native_dashboard"
        assert artifact["title"] == "Native Artifact Dashboard"
        assert artifact["source_adapter"] == "grafana"
        assert "payload" in artifact
        assert "mapping" in artifact
        assert set(artifact["mapping"]) == {"mapped", "unmapped", "sections", "controls", "reasons"}

    def test_ir_artifact_contains_json_safe_dashboard_ir(self, tmp_path):
        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        ir_file = next((out_dir / "dashboards" / "ir").glob("*.ir.json"))
        artifact = json.loads(ir_file.read_text())

        assert artifact["kind"] == "dashboard_ir"
        assert artifact["title"] == "Native Artifact Dashboard"
        panels = artifact["dashboard_ir"]["panels"]
        assert len(panels) == 1
        # Enum status must already be a plain string, not an Enum repr.
        assert isinstance(panels[0]["status"], str)

    def test_ir_artifact_round_trips_through_the_internal_dict_shape(self, tmp_path):
        """``ir/*.ir.json`` must survive a ``to_yaml_dict``/``from_yaml_dict`` round trip.

        Every in-repo reader reads ``ir/*.ir.json`` and rebuilds the internal
        dict shape with ``DashboardIR.from_dict(...).to_yaml_dict()``. This is
        the invariant that makes those readers lossless: if the round-trip ever
        drops a field, the telemetry contract, the verifier's T2 tier and
        visual-regression panel discovery all silently lose it at once.
        """
        _write_dashboard(tmp_path)
        (tmp_path / "sectioned.json").write_text(
            json.dumps(
                {
                    "title": "Sectioned Dashboard",
                    "uid": "native-artifact-3",
                    "schemaVersion": 30,
                    "templating": {
                        "list": [
                            {
                                "name": "instance",
                                "type": "query",
                                "datasource": {"type": "prometheus"},
                                "query": "label_values(up, instance)",
                                "multi": True,
                                "current": {"text": "All", "value": "$__all"},
                            }
                        ]
                    },
                    "panels": [
                        {
                            "title": "Row One",
                            "type": "row",
                            "gridPos": {"w": 24, "h": 1, "x": 0, "y": 0},
                            "panels": [
                                {
                                    "title": "Requests",
                                    "type": "timeseries",
                                    "gridPos": {"w": 12, "h": 8, "x": 0, "y": 1},
                                    "targets": [
                                        {
                                            "refId": "A",
                                            "expr": 'sum(rate(http_requests_total{instance=~"$instance"}[5m])) by (method)',
                                        }
                                    ],
                                },
                                {
                                    "title": "Notes",
                                    "type": "text",
                                    "gridPos": {"w": 12, "h": 8, "x": 12, "y": 1},
                                    "options": {"content": "hello"},
                                },
                            ],
                        }
                    ],
                }
            )
        )
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        dashboards_dir = out_dir / "dashboards"
        ir_files = sorted((dashboards_dir / "ir").glob("*.ir.json"))
        assert len(ir_files) == 2
        for ir_file in ir_files:
            artifact = json.loads(ir_file.read_text())
            dashboard_ir = DashboardIR.from_dict(artifact["dashboard_ir"])
            exported = dashboard_ir.to_yaml_dict()
            reloaded = DashboardIR.from_yaml_dict(exported, source_adapter="grafana")
            assert reloaded.to_yaml_dict() == exported, ir_file.name

    def test_native_index_lists_every_migrated_dashboard(self, tmp_path):
        _write_dashboard(tmp_path)
        (tmp_path / "web.json").write_text(
            json.dumps(
                {
                    "title": "Second Dashboard",
                    "uid": "native-artifact-2",
                    "schemaVersion": 30,
                    "panels": [],
                }
            )
        )
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        index_file = out_dir / "dashboards" / "native" / "index.json"
        index = json.loads(index_file.read_text())

        assert index["kind"] == "native_dashboard_index"
        titles = {entry["title"] for entry in index["dashboards"]}
        assert titles == {"Native Artifact Dashboard", "Second Dashboard"}
        for entry in index["dashboards"]:
            assert (out_dir / "dashboards" / entry["native_path"]).exists()
            assert (out_dir / "dashboards" / entry["ir_path"]).exists()

    def test_duplicate_dashboard_titles_get_unique_artifact_stems(self, tmp_path):
        first = {
            "title": "Duplicate Dashboard",
            "uid": "duplicate-one",
            "schemaVersion": 30,
            "panels": [],
        }
        second = {
            "title": "Duplicate Dashboard",
            "uid": "duplicate-two",
            "schemaVersion": 30,
            "panels": [],
        }
        (tmp_path / "one.json").write_text(json.dumps(first))
        (tmp_path / "two.json").write_text(json.dumps(second))
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        dashboards_dir = out_dir / "dashboards"
        native_files = sorted((dashboards_dir / "native").glob("*.native.json"))
        ir_files = sorted((dashboards_dir / "ir").glob("*.ir.json"))
        index = json.loads((dashboards_dir / "native" / "index.json").read_text())

        assert not (dashboards_dir / "yaml").exists()
        assert len(native_files) == 2
        assert len(ir_files) == 2
        assert len({path.name for path in native_files}) == 2
        assert len({path.name for path in ir_files}) == 2
        index_stems = [entry["stem"] for entry in index["dashboards"]]
        assert len(index_stems) == 2
        assert len(set(index_stems)) == 2
        for entry in index["dashboards"]:
            assert (dashboards_dir / entry["native_path"]).exists()
            assert (dashboards_dir / entry["ir_path"]).exists()

    def test_manifest_records_artifact_paths(self, tmp_path):
        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        manifest = json.loads((out_dir / "dashboards" / "migration_manifest.json").read_text())
        dashboard_entries = manifest.get("dashboards") or manifest.get("results") or []
        assert dashboard_entries, "expected at least one dashboard entry in the manifest"
        entry = dashboard_entries[0]
        assert entry.get("native_artifact_path", "").endswith(".native.json")
        assert entry.get("ir_artifact_path", "").endswith(".ir.json")

    def test_migrate_with_upload_still_writes_native_artifacts(self, tmp_path, capsys):
        """``migrate --upload`` must still emit review artifacts (plan's UX rule):
        immediate-upload users get the same post-hoc audit trail as
        two-step reviewers, and support can reproduce exactly what was sent.
        """
        from unittest import mock

        from observability_migration.targets.kibana.dashboards_api import UploadResult

        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"

        with mock.patch(
            "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
            return_value=[],
        ), mock.patch(
            "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_dashboard",
            return_value=UploadResult(dashboard="Native Artifact Dashboard", dashboard_id="d1", status="created", mapped=1),
        ):
            grafana_cli.main(
                [
                    "--source", "files",
                    "--input-dir", str(tmp_path),
                    "--output-dir", str(out_dir),
                    "--assets", "dashboards",
                    "--field-profile", "otel",
                    "--kibana-url", "https://kibana.example",
                    "--kibana-api-key", "secret",
                    "--upload",
                ]
            )

        native_files = list((out_dir / "dashboards" / "native").glob("*.native.json"))
        assert len(native_files) == 1
