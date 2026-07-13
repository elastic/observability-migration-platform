# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for strict dashboard interaction scenario manifest loading."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from observability_migration.targets.kibana.interaction_audit import CapabilityCategory
from observability_migration.targets.kibana.interaction_scenarios import (
    Assertions,
    CombinationScenario,
    ControlScenario,
    DashboardScenario,
    ManifestError,
    NoiseAllowance,
    OptionPolicy,
    load_scenario,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "interaction_audit"
MINIMAL = FIXTURES / "minimal.yaml"
INVALID = FIXTURES / "invalid.yaml"


def test_valid_fixture_loads_contract(tmp_path: Path) -> None:
    scenario = load_scenario(MINIMAL)

    assert isinstance(scenario, DashboardScenario)
    assert scenario.version == 1
    assert scenario.id == "minimal"
    assert scenario.title == "Minimal dashboard"
    assert scenario.source_kind == "grafana"
    assert scenario.source_path == "infra/grafana/dashboards/redis-11835.json"
    assert (
        scenario.control_schema_path
        == "infra/grafana/dashboards/control_schemas/redis-11835.json"
    )
    assert scenario.dashboard_title == "Redis Dashboard for Prometheus Redis Exporter"
    assert scenario.time_from == "now-3h"
    assert scenario.time_to == "now"

    assert len(scenario.controls) == 2
    namespace, instance = scenario.controls
    assert isinstance(namespace, ControlScenario)
    assert namespace.label == "namespace"
    assert namespace.key == "namespace"
    assert namespace.adapter == "esql_value"
    assert namespace.capability is CapabilityCategory.MIGRATED_LIVE
    assert namespace.options == OptionPolicy(strategy="every")
    assert namespace.assertions == Assertions(
        selection=("namespace",),
        affected_panels="query_dependency",
        query_contains=("?namespace",),
        minimum_rows=1,
    )
    assert instance.label == "instance"
    assert instance.key == "instance"
    assert instance.assertions.affected_panels == "all_query_panels"

    assert scenario.combinations == (
        CombinationScenario(
            id="namespace-and-instance",
            selections={
                "namespace": "namespace_1",
                "instance": "service.instance.id_2",
            },
        ),
    )
    assert scenario.noise_allowances == (
        NoiseAllowance(
            endpoint="/internal/security/user_profile",
            method="GET",
            status=404,
            rationale="Security is disabled in the local test stack.",
        ),
    )


def test_invalid_fixture_rejects_version_and_unknown_key() -> None:
    with pytest.raises(ManifestError, match=r"(version|unknown root key)"):
        load_scenario(INVALID)


@pytest.mark.parametrize(
    ("fragment", "match"),
    [
        ("source:\n  kind: grafana\n  path: p\n  extra: 1", "source"),
        ("dashboard:\n  title: T\n  time_from: now-1h\n  time_to: now\n  extra: 1", "dashboard"),
        (
            "controls:\n  - label: L\n    key: k\n    adapter: esql_value\n    capability: migrated_live\n    options: {}\n    assertions: {}\n    extra: 1",
            "control",
        ),
        (
            "controls:\n  - label: L\n    key: k\n    adapter: esql_value\n    capability: migrated_live\n    options:\n      extra: 1\n    assertions: {}",
            "options",
        ),
        (
            "controls:\n  - label: L\n    key: k\n    adapter: esql_value\n    capability: migrated_live\n    options: {}\n    assertions:\n      extra: 1",
            "assertions",
        ),
        ("combinations:\n  - id: c\n    selections: {}\n    extra: 1", "combination"),
        (
            "noise_allowances:\n  - endpoint: /x\n    method: GET\n    status: 404\n    rationale: ok\n    extra: 1",
            "noise allowance",
        ),
    ],
)
def test_unknown_nested_keys_reject(tmp_path: Path, fragment: str, match: str) -> None:
    base = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    overlay = yaml.safe_load(fragment)
    base.update(overlay)
    path = tmp_path / "nested-unknown.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


def test_duplicate_control_keys_reject(tmp_path: Path) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["controls"].append(
        {
            "label": "namespace duplicate",
            "key": "namespace",
            "adapter": "esql_value",
            "capability": "migrated_live",
            "options": {"strategy": "every"},
            "assertions": {},
        }
    )
    path = tmp_path / "dup-controls.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match="duplicate control key"):
        load_scenario(path)


def test_duplicate_combination_ids_reject(tmp_path: Path) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["combinations"].append(
        {
            "id": "namespace-and-instance",
            "selections": {"namespace": "namespace_1", "instance": "service.instance.id_2"},
        }
    )
    path = tmp_path / "dup-combinations.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match="duplicate combination id"):
        load_scenario(path)


def test_undeclared_combination_control_rejects(tmp_path: Path) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["combinations"][0]["selections"]["missing"] = "value"
    path = tmp_path / "undeclared-control.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match="undeclared control key"):
        load_scenario(path)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("adapter", "unsupported_adapter", "unsupported adapter"),
        ("capability", "unsupported_capability", "unsupported capability"),
    ],
)
def test_unsupported_adapter_and_capability_reject(
    tmp_path: Path, field: str, value: str, match: str
) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["controls"][0][field] = value
    path = tmp_path / f"unsupported-{field}.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


def test_unsupported_option_strategy_rejects(tmp_path: Path) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["controls"][0]["options"]["strategy"] = "random"
    path = tmp_path / "bad-strategy.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match="unsupported option strategy"):
        load_scenario(path)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda doc: doc.update({"id": ""}), "id"),
        (lambda doc: doc.update({"title": "   "}), "title"),
        (lambda doc: doc["source"].update({"kind": ""}), "source kind"),
        (lambda doc: doc["source"].update({"path": ""}), "source path"),
        (lambda doc: doc["dashboard"].update({"title": ""}), "dashboard title"),
        (
            lambda doc: doc["controls"][0].update({"label": ""}),
            "control label",
        ),
        (lambda doc: doc["controls"][0].update({"key": ""}), "control key"),
    ],
)
def test_empty_required_strings_reject(
    tmp_path: Path, mutator, match: str
) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    mutator(doc)
    path = tmp_path / "empty-required.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


def test_declared_strategy_without_include_rejects(tmp_path: Path) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["controls"][0]["options"] = {"strategy": "declared"}
    path = tmp_path / "declared-no-include.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match="include"):
        load_scenario(path)


@pytest.mark.parametrize(
    "affected_panels",
    [
        "query_dependency",
        "all_query_panels",
        ["Uptime", "Commands/sec"],
    ],
)
def test_affected_panels_accepted_forms(tmp_path: Path, affected_panels) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["controls"][0]["assertions"]["affected_panels"] = affected_panels
    path = tmp_path / "affected-panels.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    scenario = load_scenario(path)
    parsed = scenario.controls[0].assertions.affected_panels
    if isinstance(affected_panels, list):
        assert parsed == tuple(affected_panels)
    else:
        assert parsed == affected_panels


def test_affected_panels_invalid_string_rejects(tmp_path: Path) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["controls"][0]["assertions"]["affected_panels"] = "every_panel"
    path = tmp_path / "bad-affected-panels.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match="affected_panels"):
        load_scenario(path)


def test_affected_panels_rejects_kibana_uuid(tmp_path: Path) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["controls"][0]["assertions"]["affected_panels"] = [
        "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    ]
    path = tmp_path / "kibana-uuid-panels.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match="Kibana UUID"):
        load_scenario(path)


def test_negative_minimum_rows_rejects(tmp_path: Path) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["controls"][0]["assertions"]["minimum_rows"] = -1
    path = tmp_path / "negative-minimum-rows.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match="minimum_rows"):
        load_scenario(path)


@pytest.mark.parametrize(
    "rationale",
    ["", "   "],
)
def test_empty_noise_rationale_rejects(tmp_path: Path, rationale: str) -> None:
    doc = yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))
    doc["noise_allowances"][0]["rationale"] = rationale
    path = tmp_path / "empty-noise-rationale.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(ManifestError, match="rationale"):
        load_scenario(path)


def test_loaded_collections_are_immutable(tmp_path: Path) -> None:
    scenario = load_scenario(MINIMAL)

    assert isinstance(scenario.controls, tuple)
    assert isinstance(scenario.combinations, tuple)
    assert isinstance(scenario.noise_allowances, tuple)
    assert isinstance(scenario.controls[0].options.include, tuple)
    assert isinstance(scenario.controls[0].assertions.selection, tuple)

    selections = scenario.combinations[0].selections
    with pytest.raises(TypeError):
        selections["namespace"] = "mutated"  # type: ignore[index]
