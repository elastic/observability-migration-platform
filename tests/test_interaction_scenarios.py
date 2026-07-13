# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for strict dashboard interaction scenario manifest loading."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

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


def _minimal_doc() -> dict:
    return yaml.safe_load(MINIMAL.read_text(encoding="utf-8"))


def _write_manifest(tmp_path: Path, doc: dict, name: str = "manifest.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _mutated_manifest(
    tmp_path: Path,
    mutator: Callable[[dict], None],
    *,
    name: str = "manifest.yaml",
) -> Path:
    doc = _minimal_doc()
    mutator(doc)
    return _write_manifest(tmp_path, doc, name=name)


def test_valid_fixture_loads_contract() -> None:
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


def test_optional_control_schema_defaults_to_empty(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["source"].pop("control_schema"),
    )

    scenario = load_scenario(path)

    assert scenario.control_schema_path == ""


@pytest.mark.parametrize(
    "control_schema",
    ["", "   "],
)
def test_whitespace_control_schema_normalizes_to_empty(
    tmp_path: Path, control_schema: str
) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["source"].update({"control_schema": control_schema}),
    )

    scenario = load_scenario(path)

    assert scenario.control_schema_path == ""


def test_invalid_fixture_rejects_version_and_unknown_key() -> None:
    with pytest.raises(ManifestError, match="unknown root key"):
        load_scenario(INVALID)


def test_unsupported_manifest_version_rejects(tmp_path: Path) -> None:
    path = _mutated_manifest(tmp_path, lambda doc: doc.update({"version": 2}))

    with pytest.raises(ManifestError, match="unsupported manifest version: 2"):
        load_scenario(path)


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
    base = _minimal_doc()
    overlay = yaml.safe_load(fragment)
    base.update(overlay)
    path = _write_manifest(tmp_path, base, name="nested-unknown.yaml")

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


def test_malformed_yaml_rejects(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("version: [unclosed\n", encoding="utf-8")

    with pytest.raises(ManifestError, match="invalid YAML"):
        load_scenario(path)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("just a scalar", "manifest root must be a mapping"),
        ("- not: a mapping", "manifest root must be a mapping"),
    ],
)
def test_non_mapping_root_rejects(tmp_path: Path, content: str, match: str) -> None:
    path = tmp_path / "non-mapping-root.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("controls", {}, "controls must be a list"),
        ("combinations", {}, "combinations must be a list"),
        ("noise_allowances", {}, "noise_allowances must be a list"),
    ],
)
def test_collection_fields_must_be_lists(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    path = _mutated_manifest(tmp_path, lambda doc: doc.update({field: value}))

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda doc: doc.update({"id": 1}), "id must be a string"),
        (lambda doc: doc["controls"][0].update({"adapter": 7}), "adapter must be a string"),
        (
            lambda doc: doc["controls"][0]["assertions"].update({"expect_data_change": "yes"}),
            "expect_data_change must be a boolean",
        ),
        (
            lambda doc: doc["controls"][0]["assertions"].update({"selection": "namespace"}),
            "selection must be a list",
        ),
        (
            lambda doc: doc["controls"][0]["assertions"].update({"minimum_rows": "1"}),
            "minimum_rows must be an integer",
        ),
    ],
)
def test_wrong_scalar_and_collection_types_reject(
    tmp_path: Path, mutator: Callable[[dict], None], match: str
) -> None:
    path = _mutated_manifest(tmp_path, mutator)

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda doc: doc.update({"version": True}), "version must be an integer"),
        (
            lambda doc: doc["controls"][0]["assertions"].update({"minimum_rows": True}),
            "minimum_rows must be an integer",
        ),
        (
            lambda doc: doc["noise_allowances"][0].update({"status": False}),
            "status must be an integer",
        ),
    ],
)
def test_bool_is_not_accepted_as_integer(
    tmp_path: Path, mutator: Callable[[dict], None], match: str
) -> None:
    path = _mutated_manifest(tmp_path, mutator)

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


@pytest.mark.parametrize(
    "status",
    [99, 600],
)
def test_invalid_noise_status_rejects(tmp_path: Path, status: int) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["noise_allowances"][0].update({"status": status}),
    )

    with pytest.raises(ManifestError, match="status must be a valid HTTP status code"):
        load_scenario(path)


@pytest.mark.parametrize(
    ("field", "match"),
    [
        ("endpoint", "endpoint must be a string"),
        ("method", "method must be a string"),
        ("rationale", "rationale must be a string"),
    ],
)
def test_missing_noise_allowance_fields_reject(
    tmp_path: Path, field: str, match: str
) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["noise_allowances"][0].pop(field),
    )

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda doc: doc.pop("id"), "id must be a string"),
        (lambda doc: doc.pop("title"), "title must be a string"),
        (lambda doc: doc.pop("source"), "source must be a mapping"),
        (lambda doc: doc.pop("dashboard"), "dashboard must be a mapping"),
        (lambda doc: doc["source"].pop("kind"), "source kind must be a string"),
        (lambda doc: doc["source"].pop("path"), "source path must be a string"),
        (lambda doc: doc["dashboard"].pop("title"), "dashboard title must be a string"),
        (lambda doc: doc["dashboard"].pop("time_from"), "dashboard.time_from must be a string"),
        (lambda doc: doc["dashboard"].pop("time_to"), "dashboard.time_to must be a string"),
        (lambda doc: doc["controls"][0].pop("label"), "control label must be a string"),
        (lambda doc: doc["controls"][0].pop("key"), "control key must be a string"),
        (lambda doc: doc["controls"][0].pop("adapter"), "adapter must be a string"),
        (lambda doc: doc["controls"][0].pop("capability"), "capability must be a string"),
        (lambda doc: doc["controls"][0].pop("options"), "options must be a mapping"),
        (lambda doc: doc["controls"][0].pop("assertions"), "assertions must be a mapping"),
    ],
)
def test_missing_required_fields_reject(
    tmp_path: Path, mutator: Callable[[dict], None], match: str
) -> None:
    path = _mutated_manifest(tmp_path, mutator)

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


def test_duplicate_control_keys_reject(tmp_path: Path) -> None:
    doc = _minimal_doc()
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
    path = _write_manifest(tmp_path, doc, name="dup-controls.yaml")

    with pytest.raises(ManifestError, match="duplicate control key"):
        load_scenario(path)


def test_duplicate_combination_ids_reject(tmp_path: Path) -> None:
    doc = _minimal_doc()
    doc["combinations"].append(
        {
            "id": "namespace-and-instance",
            "selections": {"namespace": "namespace_1", "instance": "service.instance.id_2"},
        }
    )
    path = _write_manifest(tmp_path, doc, name="dup-combinations.yaml")

    with pytest.raises(ManifestError, match="duplicate combination id"):
        load_scenario(path)


def test_undeclared_combination_control_rejects(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["combinations"][0]["selections"].update({"missing": "value"}),
        name="undeclared-control.yaml",
    )

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
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0].update({field: value}),
        name=f"unsupported-{field}.yaml",
    )

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


def test_unsupported_option_strategy_rejects(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0]["options"].update({"strategy": "random"}),
        name="bad-strategy.yaml",
    )

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
    tmp_path: Path, mutator: Callable[[dict], None], match: str
) -> None:
    path = _mutated_manifest(tmp_path, mutator, name="empty-required.yaml")

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


def test_declared_strategy_without_include_rejects(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0].update({"options": {"strategy": "declared"}}),
        name="declared-no-include.yaml",
    )

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
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0]["assertions"].update(
            {"affected_panels": affected_panels}
        ),
        name="affected-panels.yaml",
    )

    scenario = load_scenario(path)
    parsed = scenario.controls[0].assertions.affected_panels
    if isinstance(affected_panels, list):
        assert parsed == tuple(affected_panels)
    else:
        assert parsed == affected_panels


def test_affected_panels_invalid_string_rejects(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0]["assertions"].update(
            {"affected_panels": "every_panel"}
        ),
        name="bad-affected-panels.yaml",
    )

    with pytest.raises(ManifestError, match="affected_panels"):
        load_scenario(path)


def test_affected_panels_rejects_kibana_uuid(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0]["assertions"].update(
            {"affected_panels": ["a1b2c3d4-e5f6-7890-abcd-ef1234567890"]}
        ),
        name="kibana-uuid-panels.yaml",
    )

    with pytest.raises(ManifestError, match="Kibana UUID"):
        load_scenario(path)


def test_negative_minimum_rows_rejects(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0]["assertions"].update({"minimum_rows": -1}),
        name="negative-minimum-rows.yaml",
    )

    with pytest.raises(ManifestError, match="minimum_rows"):
        load_scenario(path)


@pytest.mark.parametrize(
    "rationale",
    ["", "   "],
)
def test_empty_noise_rationale_rejects(tmp_path: Path, rationale: str) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["noise_allowances"][0].update({"rationale": rationale}),
        name="empty-noise-rationale.yaml",
    )

    with pytest.raises(ManifestError, match="rationale"):
        load_scenario(path)


def test_loaded_collections_are_immutable() -> None:
    scenario = load_scenario(MINIMAL)

    assert isinstance(scenario.controls, tuple)
    assert isinstance(scenario.combinations, tuple)
    assert isinstance(scenario.noise_allowances, tuple)
    assert isinstance(scenario.controls[0].options.include, tuple)
    assert isinstance(scenario.controls[0].assertions.selection, tuple)

    with pytest.raises(FrozenInstanceError):
        scenario.id = "mutated"  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        scenario.controls[0].label = "mutated"  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        scenario.controls = (scenario.controls[0],)  # type: ignore[misc]

    include = scenario.controls[0].options.include
    with pytest.raises(AttributeError):
        include.append("mutated")  # type: ignore[attr-defined]

    selection = scenario.controls[0].assertions.selection
    with pytest.raises(TypeError):
        selection[0] = "mutated"  # type: ignore[index]

    selections = scenario.combinations[0].selections
    assert isinstance(selections, MappingProxyType)
    with pytest.raises(TypeError):
        selections["namespace"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        del selections["namespace"]  # type: ignore[attr-defined]
