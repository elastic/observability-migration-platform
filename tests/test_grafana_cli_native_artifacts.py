# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""End-to-end coverage of the native/IR review artifacts the Grafana CLI
pipeline writes under ``<output-dir>/dashboards/{native,ir}/``.

``translate_dashboard`` already proves the in-memory ``native_dashboard``
matches the YAML bridge payload (see
``tests/test_grafana_native_dashboard_emission.py``); this file proves the
CLI pipeline persists that exact same payload to disk before any upload, so
``obs-migrate upload --artifact-dir ... --artifact-format native`` deploys
byte-for-byte what a reviewer inspected.
"""

from __future__ import annotations

import json

from observability_migration.adapters.source.grafana import cli as grafana_cli


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

    def test_native_artifact_payload_matches_yaml_bridge_payload(self, tmp_path):
        import yaml as yaml_lib

        from observability_migration.targets.kibana import dashboards_api

        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        dashboards_dir = out_dir / "dashboards"
        native_file = next((dashboards_dir / "native").glob("*.native.json"))
        yaml_file = next((dashboards_dir / "yaml").glob("*.yaml"))

        artifact = json.loads(native_file.read_text())
        doc = yaml_lib.safe_load(yaml_file.read_text())
        bridged_payload, _stats = dashboards_api.build_payload_from_yaml(doc)

        assert artifact["payload"] == bridged_payload

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
        yaml_files = sorted((dashboards_dir / "yaml").glob("*.yaml"))
        native_files = sorted((dashboards_dir / "native").glob("*.native.json"))
        ir_files = sorted((dashboards_dir / "ir").glob("*.ir.json"))
        index = json.loads((dashboards_dir / "native" / "index.json").read_text())

        assert len(yaml_files) == 2
        assert len(native_files) == 2
        assert len(ir_files) == 2
        assert len({path.stem for path in yaml_files}) == 2
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
