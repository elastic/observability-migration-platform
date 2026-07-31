# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the native/IR dashboard review artifact helpers.

Verifies the envelope shapes ``targets/kibana/native_artifacts.py`` writes:
the ``payload`` in a native artifact must be exactly
``NativeDashboard.to_api_payload()``, ``json_safe`` must normalize
``AssetStatus`` enum members that ``DashboardIR.to_dict()`` leaves as enum
instances, and the index/writer helpers must round-trip through disk.
"""

import json
import tempfile
import unittest
from pathlib import Path

from observability_migration.core.assets.dashboard import DashboardIR
from observability_migration.core.assets.native_dashboard import (
    NativeDashboard,
    NativeGrid,
    NativePanel,
)
from observability_migration.core.assets.panel import PanelIR
from observability_migration.core.assets.status import AssetStatus
from observability_migration.targets.kibana import native_artifacts as na


def _native_dashboard() -> NativeDashboard:
    return NativeDashboard(
        title="Dash",
        dashboard_id="obs-migrate-dash",
        items=[
            NativePanel(grid=NativeGrid(x=0, y=0, w=24, h=8), type="vis", config={"type": "metric"}),
        ],
    )


def _dashboard_ir() -> DashboardIR:
    return DashboardIR(
        title="Dash",
        source_adapter="grafana",
        panels=[
            PanelIR(panel_id="1", title="Metric", status=AssetStatus.TRANSLATED),
            PanelIR(panel_id="2", title="Skipped", status=AssetStatus.SKIPPED),
        ],
    )


class TestJsonSafe(unittest.TestCase):
    def test_converts_enum_to_its_value(self):
        self.assertEqual(na.json_safe(AssetStatus.TRANSLATED), AssetStatus.TRANSLATED.value)

    def test_normalizes_nested_enum_inside_dict(self):
        raw = {"status": AssetStatus.SKIPPED, "title": "x"}
        self.assertEqual(na.json_safe(raw), {"status": AssetStatus.SKIPPED.value, "title": "x"})

    def test_normalizes_enum_inside_list(self):
        self.assertEqual(na.json_safe([AssetStatus.TRANSLATED, "x"]), [AssetStatus.TRANSLATED.value, "x"])

    def test_dashboard_ir_to_dict_round_trips_through_json_dumps(self):
        ir = _dashboard_ir()
        safe = na.json_safe(ir.to_dict())
        # A bare `asdict()` would leave AssetStatus enum members in the tree,
        # which `json.dumps` cannot serialize -- this is the regression this
        # helper exists to prevent.
        encoded = json.dumps(safe)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["panels"][0]["status"], "translated")
        self.assertEqual(decoded["panels"][1]["status"], "skipped")

    def test_passes_through_plain_json_types_unchanged(self):
        for value in ("s", 1, 1.5, True, None, [1, "a"], {"a": 1}):
            self.assertEqual(na.json_safe(value), value)


class TestBuildNativeArtifact(unittest.TestCase):
    def test_payload_matches_to_api_payload_exactly(self):
        native = _native_dashboard()
        artifact = na.build_native_artifact(
            dashboard_ir=_dashboard_ir(),
            native_dashboard=native,
            native_stats={"mapped": 1, "unmapped": 0, "sections": 0, "controls": 0, "reasons": {}},
        )
        self.assertEqual(artifact["payload"], native.to_api_payload())

    def test_envelope_metadata_fields(self):
        artifact = na.build_native_artifact(
            dashboard_ir=_dashboard_ir(),
            native_dashboard=_native_dashboard(),
            native_stats=None,
        )
        self.assertEqual(artifact["kind"], na.NATIVE_ARTIFACT_KIND)
        self.assertEqual(artifact["version"], na.ARTIFACT_ENVELOPE_VERSION)
        self.assertEqual(artifact["dashboard_id"], "obs-migrate-dash")
        self.assertEqual(artifact["title"], "Dash")
        self.assertEqual(artifact["source_adapter"], "grafana")

    def test_missing_stats_default_to_zero_mapping(self):
        artifact = na.build_native_artifact(
            dashboard_ir=_dashboard_ir(), native_dashboard=_native_dashboard(), native_stats=None
        )
        self.assertEqual(
            artifact["mapping"],
            {"mapped": 0, "unmapped": 0, "sections": 0, "controls": 0, "reasons": {}},
        )

    def test_mapping_stats_are_preserved(self):
        artifact = na.build_native_artifact(
            dashboard_ir=_dashboard_ir(),
            native_dashboard=_native_dashboard(),
            native_stats={"mapped": 3, "unmapped": 2, "sections": 1, "controls": 4, "reasons": {"no_query": 2}},
        )
        self.assertEqual(
            artifact["mapping"],
            {"mapped": 3, "unmapped": 2, "sections": 1, "controls": 4, "reasons": {"no_query": 2}},
        )

    def test_title_falls_back_to_native_dashboard_title_when_ir_has_none(self):
        ir = DashboardIR(title="")
        native = NativeDashboard(title="Fallback Title")
        artifact = na.build_native_artifact(dashboard_ir=ir, native_dashboard=native, native_stats=None)
        self.assertEqual(artifact["title"], "Fallback Title")

    def test_artifact_is_json_serializable(self):
        artifact = na.build_native_artifact(
            dashboard_ir=_dashboard_ir(), native_dashboard=_native_dashboard(), native_stats={"mapped": 1}
        )
        json.dumps(artifact)  # must not raise


class TestBuildIrArtifact(unittest.TestCase):
    def test_dashboard_ir_field_holds_enum_safe_dict(self):
        artifact = na.build_ir_artifact(_dashboard_ir())
        self.assertEqual(artifact["kind"], na.IR_ARTIFACT_KIND)
        self.assertEqual(artifact["title"], "Dash")
        self.assertEqual(artifact["source_adapter"], "grafana")
        self.assertEqual(artifact["dashboard_ir"]["panels"][0]["status"], "translated")
        json.dumps(artifact)  # must not raise

    def test_handles_object_without_to_dict(self):
        class NotADashboardIR:
            title = "Untyped"
            source_adapter = "datadog"

        artifact = na.build_ir_artifact(NotADashboardIR())
        self.assertEqual(artifact["dashboard_ir"], {})
        self.assertEqual(artifact["title"], "Untyped")


class TestWriteHelpers(unittest.TestCase):
    def test_write_native_artifact_persists_expected_file_and_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            native_dir = Path(tmpdir) / "native"
            path = na.write_native_artifact(
                dashboard_ir=_dashboard_ir(),
                native_dashboard=_native_dashboard(),
                native_stats={"mapped": 1},
                native_dir=native_dir,
                stem="dash",
            )
            self.assertEqual(path, native_dir / "dash.native.json")
            self.assertTrue(path.exists())
            on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["dashboard_id"], "obs-migrate-dash")
        self.assertEqual(on_disk["payload"], _native_dashboard().to_api_payload())

    def test_write_ir_artifact_persists_expected_file_and_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ir_dir = Path(tmpdir) / "ir"
            path = na.write_ir_artifact(dashboard_ir=_dashboard_ir(), ir_dir=ir_dir, stem="dash")
            self.assertEqual(path, ir_dir / "dash.ir.json")
            on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["title"], "Dash")
        self.assertEqual(on_disk["dashboard_ir"]["panels"][1]["status"], "skipped")

    def test_write_native_artifact_index_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            native_dir = Path(tmpdir) / "native"
            entries = [
                {
                    "stem": "dash",
                    "title": "Dash",
                    "dashboard_id": "obs-migrate-dash",
                    "native_path": "native/dash.native.json",
                    "ir_path": "ir/dash.ir.json",
                }
            ]
            path = na.write_native_artifact_index(native_dir, entries)
            self.assertEqual(path, native_dir / "index.json")
            on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["kind"], na.NATIVE_ARTIFACT_INDEX_KIND)
        self.assertEqual(on_disk["version"], na.ARTIFACT_ENVELOPE_VERSION)
        self.assertEqual(on_disk["dashboards"], entries)

    def test_write_native_artifact_creates_missing_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested_dir = Path(tmpdir) / "a" / "b" / "native"
            path = na.write_native_artifact(
                dashboard_ir=_dashboard_ir(),
                native_dashboard=_native_dashboard(),
                native_stats=None,
                native_dir=nested_dir,
                stem="dash",
            )
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()


def test_ir_dashboard_fields_bypass_the_yaml_document_shape():
    """API-only dashboard fields must not be routed through the YAML shape.

    ``docs/dashboards/schema.json`` declares ``additionalProperties: false``, so
    anything the Dashboards API supports but the (deprecated) YAML format does
    not was silently destroyed when the API path was built as
    ``native_dashboard_from_yaml(ir.to_yaml_dict())``. Tags are the worked
    example: Kibana stores dashboard-level tags and accepts plain strings.
    """
    from observability_migration.core.assets.dashboard import DashboardIR
    from observability_migration.targets.kibana.dashboards_api import (
        native_dashboard_from_ir,
    )

    ir = DashboardIR.from_yaml_dict({"name": "Tagged", "panels": []}, source_adapter="grafana")
    ir.tags = ["prometheus", "redis"]

    # The YAML shape cannot carry them...
    assert "tags" not in ir.to_yaml_dict()
    # ...but the API payload does.
    native, _counts = native_dashboard_from_ir(ir)
    assert native.to_api_payload()["tags"] == ["prometheus", "redis"]


def test_ir_dashboard_id_is_unchanged_by_the_direct_mapping():
    """Removing the YAML hop must not move dashboards to new ids.

    The id is the upsert key; deriving it differently would orphan every
    previously uploaded dashboard (and was measured to get the payload
    rejected). Any change to id derivation is its own migration concern.
    """
    from observability_migration.core.assets.dashboard import DashboardIR
    from observability_migration.targets.kibana.dashboards_api import (
        native_dashboard_from_ir,
        native_dashboard_from_yaml,
    )

    ir = DashboardIR.from_yaml_dict({"name": "Redis Overview", "panels": []}, source_adapter="grafana")
    ir.uid = "someGrafanaUid"
    via_ir, _ = native_dashboard_from_ir(ir)
    via_yaml, _ = native_dashboard_from_yaml(ir.to_yaml_dict())
    assert via_ir.dashboard_id == via_yaml.dashboard_id
