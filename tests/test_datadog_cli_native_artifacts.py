# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""End-to-end coverage of the native/IR review artifacts the Datadog CLI
pipeline writes under ``<output-dir>/dashboards/{native,ir}/``.

Mirrors ``tests/test_grafana_cli_native_artifacts.py``: proves the persisted
``*.native.json`` payload equals the YAML-bridged upload payload, and that
IR/index/manifest artifacts are present and shaped as documented in
``docs/targets/kibana.md``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from observability_migration.adapters.source.datadog import cli as datadog_cli


def _write_dashboard(tmp_path):
    (tmp_path / "infra.json").write_text(
        json.dumps(
            {
                "id": "d-infra",
                "title": "Native Artifact Dashboard",
                "tags": ["team:infra"],
                "widgets": [
                    {
                        "id": 1,
                        "definition": {
                            "type": "query_value",
                            "requests": [{"q": "avg:system.cpu.user{*}"}],
                        },
                    }
                ],
            }
        )
    )


def _run(tmp_path, out_dir, *extra_args):
    argv = [
        "--source", "files",
        "--input-dir", str(tmp_path),
        "--output-dir", str(out_dir),
        "--assets", "dashboards",
        "--field-profile", "otel",
        *extra_args,
    ]
    with patch.object(datadog_cli, "_load_live_field_capabilities"):
        datadog_cli.main(argv)


def _run_capturing_yaml_exports(tmp_path, out_dir, *extra_args):
    """Run the pipeline and capture each dashboard's derived YAML document.

    The migration writes no YAML, so the kb-dashboard document
    ``generate_dashboard_artifacts`` derives only exists in memory. Capture it
    per artifact stem so the artifact-vs-YAML-bridge guards keep an oracle.
    """
    import yaml as yaml_lib

    from observability_migration.targets.kibana import compile as compile_module

    exports: dict[str, dict] = {}
    real_native = datadog_cli.write_native_artifact

    def _record(*, dashboard_ir, native_dashboard, native_stats, native_dir, stem):
        exports[stem] = yaml_lib.safe_load(
            compile_module.dashboard_yaml_text(dashboard_ir)
        )["dashboards"][0]
        return real_native(
            dashboard_ir=dashboard_ir,
            native_dashboard=native_dashboard,
            native_stats=native_stats,
            native_dir=native_dir,
            stem=stem,
        )

    with patch.object(datadog_cli, "write_native_artifact", side_effect=_record):
        _run(tmp_path, out_dir, *extra_args)
    return exports


class TestDatadogCliNativeArtifacts:
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

        ``generate_dashboard_artifacts`` still returns the kb-dashboard document
        alongside the native payload; nothing writes it to disk. Building the
        payload through that document is the only structural cross-check on the
        artifact we ship, so it stays -- against the in-memory export.
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
        assert artifact["source_adapter"] == "datadog"
        assert "payload" in artifact
        assert set(artifact["mapping"]) == {"mapped", "unmapped", "sections", "controls", "reasons"}

    def test_ir_artifact_rebuilds_the_exact_dashboard_yaml_export(self, tmp_path):
        """Datadog counterpart of the Grafana IR round-trip guard.

        Datadog still emits some ``lens`` presentation blocks and template
        variables, so this covers presentation kinds and control shapes the
        Grafana fixture does not reach. Every reader ported off the YAML export
        depends on this round-trip being lossless. The oracle is the
        kb-dashboard document the pipeline derives in memory (nothing writes it
        to disk any more).
        """
        from observability_migration.core.assets.dashboard import DashboardIR

        _write_dashboard(tmp_path)
        (tmp_path / "grouped.json").write_text(
            json.dumps(
                {
                    "id": "d-grouped",
                    "title": "Grouped Dashboard",
                    "template_variables": [
                        {"name": "env", "prefix": "env", "default": "prod"}
                    ],
                    "widgets": [
                        {
                            "id": 10,
                            "definition": {
                                "type": "group",
                                "title": "Group One",
                                "widgets": [
                                    {
                                        "id": 11,
                                        "definition": {
                                            "type": "timeseries",
                                            "title": "CPU",
                                            "requests": [
                                                {"q": "avg:system.cpu.user{*} by {host}"}
                                            ],
                                        },
                                    },
                                    {
                                        "id": 12,
                                        "definition": {
                                            "type": "note",
                                            "content": "hello",
                                        },
                                    },
                                ],
                            },
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

    def test_ir_artifact_contains_json_safe_dashboard_ir(self, tmp_path):
        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        ir_file = next((out_dir / "dashboards" / "ir").glob("*.ir.json"))
        artifact = json.loads(ir_file.read_text())

        assert artifact["kind"] == "dashboard_ir"
        assert artifact["title"] == "Native Artifact Dashboard"
        panels = artifact["dashboard_ir"]["panels"]
        assert len(panels) >= 1
        assert isinstance(panels[0]["status"], str)

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
