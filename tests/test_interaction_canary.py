# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the synthetic Kibana interaction capability canary."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from observability_migration.core.coverage.interaction_canary import (
    _ENVIRONMENT_VALUES_QUERY,
    _PANEL_QUERIES,
    INTERACTION_CANARY_TITLE,
    INTERACTION_CANARY_UID,
    RANGE_INTERACTION_SELECTION,
    RANGE_SLIDER_DEFAULT_BOUNDS,
    SYNTHETIC_HOST_NAMES,
    build_interaction_canary,
    build_interaction_failure_canaries,
    write_interaction_canary_artifact,
)
from observability_migration.core.telemetry_contract import build_telemetry_contract
from observability_migration.core.telemetry_data import generate_documents
from observability_migration.targets.kibana import dashboards_api as api
from observability_migration.targets.kibana.interaction_audit import (
    CapabilityCategory,
    FailureClass,
    NetworkEvidence,
    check_network_contract,
)
from observability_migration.targets.kibana.interaction_runner import (
    _selection_changes_from_baseline,
)
from observability_migration.targets.kibana.interaction_scenarios import (
    Assertions,
    ControlScenario,
    DiscoveredControl,
    OptionPolicy,
    build_execution_plan,
    load_scenario,
)
from observability_migration.targets.kibana.render_audit import classify_panel
from tests.test_interaction_runner import FakeBrowser, _run
from tests.test_interaction_runner import _scenario as runner_scenario

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


def _esql_control(native, variable_name: str):
    return next(
        control
        for control in native.controls
        if control.type == "esql_control"
        and control.config.get("variable_name") == variable_name
    )


def test_interaction_canary_native_environment_uses_values_from_query():
    native, _ = api.native_dashboard_from_ir(build_interaction_canary())
    environment = _esql_control(native, "environment")
    assert environment.config["control_type"] == "VALUES_FROM_QUERY"
    assert environment.config["variable_type"] == "values"
    assert environment.config["esql_query"] == _ENVIRONMENT_VALUES_QUERY
    assert environment.config["selected_options"] == ["prod"]


def test_interaction_canary_native_static_controls_use_static_values():
    native, _ = api.native_dashboard_from_ir(build_interaction_canary())
    grouping = _esql_control(native, "grouping")
    aggregate = _esql_control(native, "aggregate")
    interval = _esql_control(native, "interval")

    assert grouping.config["control_type"] == "STATIC_VALUES"
    assert grouping.config["variable_type"] == "fields"
    assert grouping.config["available_options"] == ["service.name", "host.name"]
    assert grouping.config["selected_options"] == ["service.name"]

    assert aggregate.config["control_type"] == "STATIC_VALUES"
    assert aggregate.config["variable_type"] == "functions"
    assert aggregate.config["available_options"] == ["AVG", "MAX", "SUM"]
    assert aggregate.config["selected_options"] == ["AVG"]

    assert interval.config["control_type"] == "STATIC_VALUES"
    assert interval.config["variable_type"] == "time_literal"
    assert interval.config["available_options"] == ["1 minute", "5 minutes", "15 minutes"]
    assert interval.config["selected_options"] == ["5 minutes"]


def test_interaction_canary_native_services_preserves_multi_selection():
    native, _ = api.native_dashboard_from_ir(build_interaction_canary())
    services = _esql_control(native, "services")
    assert services.config["control_type"] == "VALUES_FROM_QUERY"
    assert services.config["variable_type"] == "values"
    assert services.config["selected_options"] == ["api", "worker"]
    assert services.config.get("single_select") is not True


def test_interaction_canary_native_classic_controls_map_fields():
    dashboard = build_interaction_canary()
    host = next(control for control in dashboard.controls if control.control_id == "host.name")
    assert list(host.available_options) == list(SYNTHETIC_HOST_NAMES)
    assert list(host.selected_options) == [SYNTHETIC_HOST_NAMES[0]]

    native, _ = api.native_dashboard_from_ir(dashboard)
    options = next(c for c in native.controls if c.type == "options_list_control")
    range_control = next(c for c in native.controls if c.type == "range_slider_control")
    assert options.config["field_name"] == "host.name"
    assert options.config["data_view_id"] == "metrics-*"
    assert options.config["selected_options"] == [SYNTHETIC_HOST_NAMES[0]]
    assert range_control.config["field_name"] == "latency_ms"
    assert range_control.config["value"] == list(RANGE_SLIDER_DEFAULT_BOUNDS)


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
    assert set(stream["required_values"]["host.name"]) >= set(SYNTHETIC_HOST_NAMES)
    assert "aggregate" not in stream["fields"]
    assert "interval" not in stream["fields"]


def test_interaction_canary_contract_seeds_varying_numeric_latency(tmp_path):
    write_interaction_canary_artifact(tmp_path)
    contract = build_telemetry_contract(tmp_path)
    stream = contract["streams"]["metrics-*"]
    latency = stream["fields"]["latency_ms"]
    assert latency["role"] == "metric"
    assert latency["type_family"] == "numeric"
    assert latency["seed_range"] == [20.0, 80.0]

    values = [
        doc["latency_ms"]
        for _, doc in generate_documents(contract)
        if "latency_ms" in doc
    ]
    assert values
    assert min(values) == 20.0
    assert max(values) == 80.0
    assert len(set(values)) > 1
    inside = [value for value in values if 40.0 <= value <= 60.0]
    outside = [value for value in values if value < 40.0 or value > 60.0]
    assert inside
    assert outside


def test_interaction_canary_seed_docs_cooccur_with_global_control_fields(tmp_path):
    write_interaction_canary_artifact(tmp_path)
    contract = build_telemetry_contract(tmp_path)
    value_docs = [
        doc
        for _, doc in generate_documents(contract)
        if "interaction_value" in doc
    ]

    assert value_docs
    missing_by_doc = [
        sorted(
            {
                "service.environment",
                "service.name",
                "host.name",
                "latency_ms",
            }
            - set(doc)
        )
        for doc in value_docs
    ]
    assert all(
        {
            "service.environment",
            "service.name",
            "host.name",
            "latency_ms",
        }
        <= set(doc)
        for doc in value_docs
    ), sorted({tuple(missing) for missing in missing_by_doc})


def test_interaction_failure_canaries_cover_expected_classes():
    canaries = build_interaction_failure_canaries()
    assert len(canaries) == len(_EXPECTED_FAILURE_IDS)
    assert {canary.canary_id for canary in canaries} == _EXPECTED_FAILURE_IDS

    by_id = {canary.canary_id: canary for canary in canaries}
    assert by_id["invalid-output-accessor"].expected_failure_classes == ("render_error",)
    assert FailureClass.RENDER_ERROR.value in by_id["invalid-output-accessor"].expected_failure_classes
    assert FailureClass.QUERY_CONTRACT_ERROR.value in by_id["value-instead-of-identifier"].expected_failure_classes
    assert FailureClass.FIELD_GAP.value in by_id["missing-target-field"].expected_failure_classes
    assert FailureClass.DATA_GAP.value in by_id["missing-required-values"].expected_failure_classes
    assert FailureClass.UNEXPECTED_EMPTY.value in by_id["unexpected-empty"].expected_failure_classes
    assert FailureClass.CONTROL_NOT_FOUND.value in by_id["manifest-control-absent"].expected_failure_classes
    for canary in canaries:
        assert len(canary.expected_failure_classes) == 1


def _failure_by_id(canary_id: str):
    return {canary.canary_id: canary for canary in build_interaction_failure_canaries()}[canary_id]


def test_failure_canary_invalid_accessor_classifies_render_error():
    canary = _failure_by_id("invalid-output-accessor")
    text = "Provided column name or index is invalid: missing_value_column"
    result = classify_panel("invalid accessor", text, expects_data=True)
    assert result.error_class == "render_error"
    assert FailureClass.RENDER_ERROR.value in canary.expected_failure_classes
    assert result.error_class != "field_gap"


def test_failure_canary_value_binding_classifies_query_contract_error():
    canary = _failure_by_id("value-instead-of-identifier")
    evidence = NetworkEvidence(
        endpoint="/internal/search/esql_async",
        method="POST",
        status=200,
        url="http://localhost:5601/internal/search/esql_async",
        query=(
            "FROM metrics-* | STATS value=AVG(interaction_value) BY grouping=?grouping"
        ),
        panel_id="failure-value-binding",
        params={"grouping": "host.name"},
        param_kinds={"grouping": "value"},
        response_columns=("value", "grouping"),
        row_count=1,
    )
    findings = check_network_contract(
        expected_panel_ids=["failure-value-binding"],
        unaffected_panel_ids=[],
        evidence=[evidence],
        expected_identifier_params={"grouping": "host.name"},
    )
    assert any(
        finding.failure_class is FailureClass.QUERY_CONTRACT_ERROR for finding in findings
    )
    assert FailureClass.QUERY_CONTRACT_ERROR.value in canary.expected_failure_classes


def test_failure_canary_missing_field_classifies_field_gap():
    canary = _failure_by_id("missing-target-field")
    text = "Provided column name or index is invalid: missing_dimension"
    result = classify_panel(
        "field gap",
        text,
        breakdown_fields=["missing_dimension"],
        available_fields=["service.name", "host.name"],
        expects_data=True,
    )
    assert result.error_class == "field_gap"
    assert FailureClass.FIELD_GAP.value in canary.expected_failure_classes


def test_failure_canary_missing_values_classifies_data_gap():
    canary = _failure_by_id("missing-required-values")
    result = classify_panel(
        "data gap",
        "No results found",
        referenced_metrics=["interaction_value"],
        available_metrics=["other_metric"],
        expects_data=True,
    )
    assert result.error_class == "data_gap"
    assert FailureClass.DATA_GAP.value in canary.expected_failure_classes


def test_failure_canary_zero_rows_classifies_unexpected_empty():
    canary = _failure_by_id("unexpected-empty")
    result = classify_panel(
        "unexpected empty",
        "No results found",
        referenced_metrics=["interaction_value"],
        available_metrics=["interaction_value"],
        expects_data=True,
    )
    assert result.error_class == "unexpected_empty"
    assert FailureClass.UNEXPECTED_EMPTY.value in canary.expected_failure_classes


def test_failure_canary_absent_control_classifies_control_not_found(tmp_path: Path):
    canary = _failure_by_id("manifest-control-absent")
    ghost = ControlScenario(
        label="ghost",
        key="ghost",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="every"),
        assertions=Assertions(affected_panels="query_dependency"),
    )
    scenario = runner_scenario(controls=(ghost,))
    report = _run(FakeBrowser(controls={}), scenario, tmp_path)
    result = next(item for item in report.results if item.name == "ghost:missing_control")
    classes = {finding.failure_class for finding in result.findings}
    assert FailureClass.CONTROL_NOT_FOUND in classes
    assert FailureClass.CONTROL_NOT_FOUND.value in canary.expected_failure_classes
    assert FailureClass.RENDER_ERROR not in classes


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


def _discovered_from_canary(dashboard):
    return tuple(
        DiscoveredControl(
            key=control.control_id,
            label=control.label,
            options=tuple(control.available_options or control.selected_options or []),
            selected=tuple(control.selected_options),
        )
        for control in dashboard.controls
    )


def test_synthetic_manifest_execution_plan_covers_gaps_actions_and_combinations():
    scenario = load_scenario(SYNTHETIC_MANIFEST)
    dashboard = build_interaction_canary()
    plan = build_execution_plan(scenario, _discovered_from_canary(dashboard))

    gap_steps = {
        step.control_key
        for step in plan
        if step.kind == "coverage_gap" and step.control_key.startswith("gap_")
    }
    assert gap_steps == {
        "gap_grafana_any",
        "gap_like_param",
        "gap_datasource_replacement",
        "gap_chained_controls",
        "gap_invalid_unbound_params",
        "gap_disallowed_function_names",
        "gap_control_query_timeout",
    }
    assert not any(
        step.kind == "missing_control" and step.control_key.startswith("gap_")
        for step in plan
    )

    query_bar_steps = [
        step for step in plan if step.control_key == "query_bar" and step.kind == "option"
    ]
    assert len(query_bar_steps) == 1
    assert dict(query_bar_steps[0].selections) == {
        "query_bar": 'service.environment:"prod"'
    }

    range_steps = [
        step for step in plan if step.control_key == "latency_ms" and step.kind == "option"
    ]
    assert any(
        dict(step.selections) == {"latency_ms": RANGE_INTERACTION_SELECTION}
        for step in range_steps
    )

    combination_ids = {step.id for step in plan if step.kind == "combination"}
    assert {
        "multi-and-field",
        "function-and-interval",
        "options-range-and-query",
    } <= combination_ids


def test_synthetic_manifest_range_control_contract():
    scenario = load_scenario(SYNTHETIC_MANIFEST)
    latency = next(control for control in scenario.controls if control.key == "latency_ms")
    assert latency.adapter == "range_slider"
    assert latency.options.include == (RANGE_INTERACTION_SELECTION,)
    assert latency.assertions.query_contains == ("latency_ms",)
    assert latency.assertions.expect_data_change is False
    assert "interaction-range" in latency.assertions.affected_panels
    assert _PANEL_QUERIES["interaction-range"].count("latency_ms") >= 2

    dashboard = build_interaction_canary()
    discovered = {item.key: item for item in _discovered_from_canary(dashboard)}
    range_control = next(
        control for control in dashboard.controls if control.control_id == "latency_ms"
    )
    assert tuple(range_control.selected_options) == RANGE_SLIDER_DEFAULT_BOUNDS
    assert _selection_changes_from_baseline(
        {"latency_ms": RANGE_INTERACTION_SELECTION},
        discovered,
    )


def test_synthetic_query_bar_allows_redundant_default_filter():
    scenario = load_scenario(SYNTHETIC_MANIFEST)
    query_bar = next(
        control for control in scenario.controls if control.key == "query_bar"
    )

    assert query_bar.assertions.expect_data_change is False


def test_synthetic_non_textual_charts_use_network_evidence_for_changes():
    scenario = load_scenario(SYNTHETIC_MANIFEST)
    by_key = {control.key: control for control in scenario.controls}

    assert by_key["interval"].assertions.expect_data_change is False
    assert by_key["host.name"].assertions.expect_data_change is False
    assert by_key["host.name"].options.exclude == ("Exists",)


def test_synthetic_combination_options_list_selections_match_seeded_values(tmp_path):
    write_interaction_canary_artifact(tmp_path)
    contract = build_telemetry_contract(tmp_path)
    seeded_hosts = set(contract["streams"]["metrics-*"]["required_values"]["host.name"])
    assert seeded_hosts >= set(SYNTHETIC_HOST_NAMES)

    scenario = load_scenario(SYNTHETIC_MANIFEST)
    for combination in scenario.combinations:
        for key, value in combination.selections.items():
            control = next(item for item in scenario.controls if item.key == key)
            if control.adapter != "options_list":
                continue
            assert value in seeded_hosts, (
                f"{combination.id} selects {key}={value!r} but seeded values are {sorted(seeded_hosts)}"
            )
