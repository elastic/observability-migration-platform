# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""End-to-end coverage of the native/IR review artifacts the Datadog CLI
pipeline writes under ``<output-dir>/dashboards/{native,ir}/``.

Mirrors ``tests/test_grafana_cli_native_artifacts.py``: proves the persisted
``*.native.json`` payload still structurally describes the ``DashboardIR`` it
was built from, and that IR/index/manifest artifacts are present and shaped as
documented in ``docs/targets/kibana.md``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from observability_migration.adapters.source.datadog import cli as datadog_cli
from observability_migration.core.assets.dashboard import DashboardIR
from tests.native_payload_guard import (
    assert_payload_matches_dict_shape_bridge,
    assert_payload_matches_ir,
)


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


def _ir_by_stem(dashboards_dir):
    """``{artifact stem: DashboardIR}`` rebuilt from the persisted ir/ artifacts."""
    out = {}
    for ir_file in sorted((dashboards_dir / "ir").glob("*.ir.json")):
        stem = ir_file.name[: -len(".ir.json")]
        out[stem] = DashboardIR.from_dict(json.loads(ir_file.read_text())["dashboard_ir"])
    return out


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

    def test_native_artifact_payload_describes_the_persisted_ir(self, tmp_path):
        """The shipped payload must still describe the IR it was built from.

        Load-bearing structural cross-check on the artifact we upload: compares
        the payload against the ``DashboardIR`` rather than re-running the
        mapper, so a widget or ES|QL query lost during mapping surfaces here.
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

        The internal dict shape cannot express dashboard-level ``tags`` -- its
        schema is ``additionalProperties: false``, which is exactly why
        ``native_dashboard_from_ir`` reads dashboard fields off the IR rather
        than through ``to_yaml_dict()``. So the divergence is *pinned* rather
        than ignored: tags may differ, nothing else may, and tags must actually
        be populated (an empty list would make the two payloads match again and
        silently re-open the bug where Datadog tags never reached the upload).
        """
        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        dashboards_dir = out_dir / "dashboards"
        irs = _ir_by_stem(dashboards_dir)
        native_file = next((dashboards_dir / "native").glob("*.native.json"))
        stem = native_file.name[: -len(".native.json")]

        artifact = json.loads(native_file.read_text())
        assert_payload_matches_dict_shape_bridge(
            artifact["payload"],
            irs[stem],
            allow_divergent_keys=frozenset({"tags"}),
            label=stem,
        )
        assert artifact["payload"]["tags"] == ["team:infra"]

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

    def test_ir_artifact_round_trips_through_the_internal_dict_shape(self, tmp_path):
        """Datadog counterpart of the Grafana IR round-trip guard.

        Datadog still emits some ``lens`` presentation blocks and template
        variables, so this covers presentation kinds and control shapes the
        Grafana fixture does not reach. Every reader of ``ir/*.ir.json`` depends
        on this round-trip being lossless.
        """
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
        _run(tmp_path, out_dir)

        dashboards_dir = out_dir / "dashboards"
        ir_files = sorted((dashboards_dir / "ir").glob("*.ir.json"))
        assert len(ir_files) == 2
        for ir_file in ir_files:
            artifact = json.loads(ir_file.read_text())
            dashboard_ir = DashboardIR.from_dict(artifact["dashboard_ir"])
            exported = dashboard_ir.to_yaml_dict()
            reloaded = DashboardIR.from_yaml_dict(exported, source_adapter="datadog")
            assert reloaded.to_yaml_dict() == exported, ir_file.name

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

    def test_source_tags_and_lineage_reach_the_ir_and_uploaded_payload(self, tmp_path):
        """Datadog source tags must survive into both artifacts.

        The IR is built from the kb-dashboard document, whose shape carries
        neither tags nor source lineage, so both have to be set from the
        normalized dashboard. They were not: ``ir/<stem>.ir.json`` recorded
        ``tags: []`` and an empty ``source_file``, and because
        ``native_dashboard_from_ir`` reads tags straight off the IR, a dashboard
        tagged in Datadog uploaded to Kibana with no tags at all. The fixture is
        tagged ``team:infra``; Datadog's ``key:value`` form is preserved rather
        than split, so no scoping information is invented.
        """
        _write_dashboard(tmp_path)
        out_dir = tmp_path / "out"
        _run(tmp_path, out_dir)

        ir_file = next((out_dir / "dashboards" / "ir").glob("*.ir.json"))
        dashboard_ir = json.loads(ir_file.read_text())["dashboard_ir"]
        assert dashboard_ir["tags"] == ["team:infra"]
        assert dashboard_ir["source_file"].endswith("infra.json")

        native_file = next((out_dir / "dashboards" / "native").glob("*.native.json"))
        payload = json.loads(native_file.read_text())["payload"]
        assert payload.get("tags") == ["team:infra"], (
            "tags must reach the payload the run uploads, not just the IR"
        )
