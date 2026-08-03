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
from unittest.mock import patch

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


def _run_capturing_yaml_exports(tmp_path, out_dir):
    """Run the pipeline and capture each dashboard's derived YAML document.

    The migration writes no YAML, so the kb-dashboard document the artifacts are
    derived from only exists in memory. Capture it per dashboard (keyed by
    artifact stem) so the artifact-vs-YAML-bridge guards still have an oracle.
    """
    import yaml as yaml_lib

    from observability_migration.targets.kibana import compile as compile_module

    exports: dict[str, dict] = {}
    # The native artifact writer is the pipeline's last stop and receives the
    # same IR the ir/ artifact is built from, so capture the derived document
    # there.
    real_native = grafana_cli.write_native_artifact

    def _record(*, dashboard_ir, native_dashboard, native_stats, native_dir, stem):
        exports[stem] = yaml_lib.safe_load(compile_module.dashboard_yaml_text(dashboard_ir))["dashboards"][0]
        return real_native(
            dashboard_ir=dashboard_ir,
            native_dashboard=native_dashboard,
            native_stats=native_stats,
            native_dir=native_dir,
            stem=stem,
        )

    with patch.object(grafana_cli, "write_native_artifact", side_effect=_record):
        _run(tmp_path, out_dir)
    return exports


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
        """The persisted typed payload must equal the one the YAML bridge builds.

        The migration no longer writes YAML, but the kb-dashboard document is
        still derived from the same IR in memory. Building the payload through
        that document is the only structural cross-check on the artifact we ship,
        so it stays -- against the in-memory export instead of a file on disk.
        """
        from observability_migration.targets.kibana import dashboards_api

        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        exports = _run_capturing_yaml_exports(tmp_path, out_dir)

        dashboards_dir = out_dir / "dashboards"
        native_file = next((dashboards_dir / "native").glob("*.native.json"))
        stem = native_file.name[: -len(".native.json")]

        artifact = json.loads(native_file.read_text())
        bridged_payload, _stats = dashboards_api.build_payload_from_yaml(
            {"dashboards": [exports[stem]]}
        )

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

    def test_ir_artifact_rebuilds_the_exact_dashboard_yaml_export(self, tmp_path):
        """The IR artifact must round-trip to the YAML export, byte for byte.

        Every in-repo reader that used to glob ``yaml/*.yaml`` now reads
        ``ir/*.ir.json`` and rebuilds the kb-dashboard-core document with
        ``DashboardIR.from_dict(...).to_yaml_dict()``. This is the invariant
        that makes those ports lossless: if the round-trip ever drops a field,
        the telemetry contract, the verifier's T2 tier and visual-regression
        panel discovery all silently lose it at once. The oracle is the
        kb-dashboard document the pipeline derives in memory (nothing writes it
        to disk any more).
        """
        from observability_migration.core.assets.dashboard import DashboardIR

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
        exports = _run_capturing_yaml_exports(tmp_path, out_dir)

        dashboards_dir = out_dir / "dashboards"
        ir_files = sorted((dashboards_dir / "ir").glob("*.ir.json"))
        assert len(ir_files) == 2
        for ir_file in ir_files:
            stem = ir_file.name[: -len(".ir.json")]
            expected = exports[stem]
            artifact = json.loads(ir_file.read_text())
            rebuilt = DashboardIR.from_dict(artifact["dashboard_ir"]).to_yaml_dict()
            assert rebuilt == expected, ir_file.name

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
