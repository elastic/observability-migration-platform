# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the synthetic Kibana interaction capability canary."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from observability_migration.core.coverage.interaction_canary import (
    _PANEL_QUERIES,
    INTERACTION_CANARY_TITLE,
    INTERACTION_CANARY_UID,
    build_interaction_canary,
    build_interaction_failure_canaries,
    write_interaction_canary_artifact,
)
from observability_migration.core.telemetry_contract import build_telemetry_contract
from observability_migration.targets.kibana import dashboards_api as api
from observability_migration.targets.kibana.interaction_audit import FailureClass
from observability_migration.targets.kibana.interaction_scenarios import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_MANIFEST = REPO_ROOT / "parity-rig" / "interaction-scenarios" / "synthetic-controls.yaml"

_EXPECTED_FAILURE_IDS = {
    "invalid-output-accessor",
    "value-instead-of-identifier",
    "missing-target-field",
    "missing-required-values",
    "unexpected-empty",
    "manifest-control-absent",
}


def _dashboard_yaml(dashboard):
    return {"dashboards": [dashboard.to_yaml_dict()]}


def test_interaction_canary_maps_every_control_to_native_api():
    dashboard = build_interaction_canary()
    native, counts = api.native_dashboard_from_ir(dashboard)

    assert counts.controls == 7
    assert len(native.controls) == 7
    esql_types = {
        control.config["variable_type"]
        for control in native.controls
        if control.type == "esql_control"
    }
    assert esql_types == {"values", "fields", "functions", "time_literal"}
    assert {control.type for control in native.controls} >= {
        "esql_control",
        "options_list_control",
        "range_slider_control",
    }


def test_interaction_canary_queries_retain_param_semantics():
    queries = _PANEL_QUERIES
    assert "?environment" in queries["interaction-value"]
    assert "MV_CONTAINS(?services, service.name)" in queries["interaction-multi"]
    assert "grouping=??grouping" in queries["interaction-field"]
    assert "??aggregate(interaction_value)" in queries["interaction-function"]
    assert "TBUCKET(?interval)" in queries["interaction-interval"]
    assert "host.name" in queries["interaction-options"]
    assert "latency_ms" in queries["interaction-range"]


def test_interaction_canary_ids_and_titles_are_deterministic_and_unique():
    dashboard = build_interaction_canary()
    panel_ids = [panel.panel_id for panel in dashboard.panels]
    panel_titles = [panel.title for panel in dashboard.panels]
    control_ids = [control.control_id for control in dashboard.controls]

    assert dashboard.uid == INTERACTION_CANARY_UID
    assert dashboard.title == INTERACTION_CANARY_TITLE
    assert len(panel_ids) == len(set(panel_ids))
    assert len(panel_titles) == len(set(panel_titles))
    assert len(control_ids) == len(set(control_ids))
    assert "interaction-unaffected" in panel_ids


def test_interaction_canary_source_extensions_are_minimal_and_json_serializable():
    dashboard = build_interaction_canary()
    payload = _dashboard_yaml(dashboard)
    json.dumps(payload)

    for control in dashboard.controls:
        extension = control.source_extension
        assert isinstance(extension, dict)
        assert extension.get("type")
        if control.kind == "esql":
            assert extension.get("variable_name")
            assert extension.get("variable_type")
        if control.metadata.get("bound_field"):
            metadata = extension.get("metadata") or {}
            assert metadata.get("bound_field") == control.metadata["bound_field"]


def test_interaction_canary_contract_seeds_all_control_fields_and_values(tmp_path):
    write_interaction_canary_artifact(tmp_path)
    contract = build_telemetry_contract(tmp_path)
    stream = contract["streams"]["metrics-*"]

    assert {
        "service.name",
        "service.environment",
        "host.name",
        "latency_ms",
    } <= set(stream["control_fields"])
    assert set(stream["required_values"]["service.name"]) >= {"api", "worker", "frontend"}
    assert set(stream["required_values"]["service.environment"]) >= {"prod"}
    assert "aggregate" not in stream["fields"]
    assert "interval" not in stream["fields"]


def test_interaction_failure_canaries_cover_expected_classes():
    canaries = build_interaction_failure_canaries()
    assert len(canaries) == len(_EXPECTED_FAILURE_IDS)
    assert {canary.canary_id for canary in canaries} == _EXPECTED_FAILURE_IDS

    by_id = {canary.canary_id: canary for canary in canaries}
    assert FailureClass.RENDER_ERROR in by_id["invalid-output-accessor"].expected_failure_classes
    assert FailureClass.QUERY_CONTRACT_ERROR in by_id["value-instead-of-identifier"].expected_failure_classes
    assert FailureClass.FIELD_GAP in by_id["missing-target-field"].expected_failure_classes
    assert FailureClass.DATA_GAP in by_id["missing-required-values"].expected_failure_classes
    assert FailureClass.UNEXPECTED_EMPTY in by_id["unexpected-empty"].expected_failure_classes
    assert FailureClass.FRAMEWORK_ERROR in by_id["manifest-control-absent"].expected_failure_classes


def test_interaction_failure_canaries_are_isolated_native_dashboards():
    for canary in build_interaction_failure_canaries():
        native, counts = api.native_dashboard_from_ir(canary.dashboard)
        assert len(native.items) == 1
        assert counts.mapped == 1


def test_synthetic_controls_manifest_strict_loads():
    scenario = load_scenario(SYNTHETIC_MANIFEST)
    assert scenario.id == "synthetic-controls"
    assert scenario.dashboard_title == INTERACTION_CANARY_TITLE
    assert scenario.controls
    assert scenario.combinations


def test_synthetic_controls_manifest_declares_capabilities_and_gaps():
    scenario = load_scenario(SYNTHETIC_MANIFEST)
    keys = {control.key for control in scenario.controls}
    assert {
        "environment",
        "services",
        "grouping",
        "aggregate",
        "interval",
        "host.name",
        "latency_ms",
        "query_bar",
    } <= keys
    assert keys & {
        "gap_grafana_any",
        "gap_like_param",
        "gap_datasource_replacement",
        "gap_chained_controls",
        "gap_invalid_unbound_params",
        "gap_disallowed_function_names",
        "gap_control_query_timeout",
    }
    combination_ids = {combination.id for combination in scenario.combinations}
    assert {
        "multi-and-field",
        "function-and-interval",
        "options-range-and-query",
    } <= combination_ids


def test_synthetic_controls_manifest_has_no_uuid_panel_ids_or_duplicated_queries():
    raw = yaml.safe_load(SYNTHETIC_MANIFEST.read_text(encoding="utf-8"))
    serialized = yaml.safe_dump(raw)
    assert "query:" not in serialized.lower()
    for control in raw["controls"]:
        affected = control["assertions"].get("affected_panels")
        if isinstance(affected, list):
            for panel_id in affected:
                assert "-" in panel_id or "_" in panel_id
                assert len(panel_id) < 40


def test_interaction_canary_yaml_round_trips_through_dashboard_ir():
    dashboard = build_interaction_canary()
    payload = _dashboard_yaml(dashboard)
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "canary.yaml"
        path.write_text(yaml.safe_dump(payload), encoding="utf-8")
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["dashboards"][0]["name"] == INTERACTION_CANARY_TITLE
    assert len(loaded["dashboards"][0]["controls"]) == 7
    assert len(loaded["dashboards"][0]["panels"]) == 8
