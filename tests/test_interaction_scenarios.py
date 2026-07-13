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
    DiscoveredControl,
    InteractionStep,
    ManifestError,
    NoiseAllowance,
    OptionPolicy,
    _safe_step_id_component,
    _stable_component_hash,
    build_execution_plan,
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
        (lambda doc: doc.update({"id": 1}), "root.id must be a string"),
        (lambda doc: doc["controls"][0].update({"adapter": 7}), r"controls\[0\]\.adapter must be a string"),
        (
            lambda doc: doc["controls"][0]["assertions"].update({"expect_data_change": "yes"}),
            r"controls\[0\]\.assertions\.expect_data_change must be a boolean",
        ),
        (
            lambda doc: doc["controls"][0]["assertions"].update({"selection": "namespace"}),
            r"controls\[0\]\.assertions\.selection must be a list",
        ),
        (
            lambda doc: doc["controls"][0]["assertions"].update({"minimum_rows": "1"}),
            r"controls\[0\]\.assertions\.minimum_rows must be an integer",
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
        (lambda doc: doc.update({"version": True}), "root.version must be an integer"),
        (
            lambda doc: doc["controls"][0]["assertions"].update({"minimum_rows": True}),
            r"controls\[0\]\.assertions\.minimum_rows must be an integer",
        ),
        (
            lambda doc: doc["noise_allowances"][0].update({"status": False}),
            r"noise_allowances\[0\]\.status must be an integer",
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

    with pytest.raises(ManifestError, match=r"noise_allowances\[0\]\.status must be a valid HTTP status code"):
        load_scenario(path)


@pytest.mark.parametrize(
    ("field", "match"),
    [
        ("endpoint", r"noise_allowances\[0\]\.endpoint must be a string"),
        ("method", r"noise_allowances\[0\]\.method must be a string"),
        ("rationale", r"noise_allowances\[0\]\.rationale must be a string"),
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
        (lambda doc: doc.pop("id"), "root.id must be a string"),
        (lambda doc: doc.pop("title"), "root.title must be a string"),
        (lambda doc: doc.pop("source"), "root.source must be a mapping"),
        (lambda doc: doc.pop("dashboard"), "root.dashboard must be a mapping"),
        (lambda doc: doc["source"].pop("kind"), "source.kind must be a string"),
        (lambda doc: doc["source"].pop("path"), "source.path must be a string"),
        (lambda doc: doc["dashboard"].pop("title"), "dashboard.title must be a string"),
        (lambda doc: doc["controls"][0].pop("label"), r"controls\[0\]\.label must be a string"),
        (lambda doc: doc["controls"][0].pop("key"), r"controls\[0\]\.key must be a string"),
        (lambda doc: doc["controls"][0].pop("adapter"), r"controls\[0\]\.adapter must be a string"),
        (lambda doc: doc["controls"][0].pop("capability"), r"controls\[0\]\.capability must be a string"),
        (lambda doc: doc["controls"][0].pop("options"), r"controls\[0\]\.options must be a mapping"),
        (lambda doc: doc["controls"][0].pop("assertions"), r"controls\[0\]\.assertions must be a mapping"),
    ],
)
def test_missing_required_fields_reject(
    tmp_path: Path, mutator: Callable[[dict], None], match: str
) -> None:
    path = _mutated_manifest(tmp_path, mutator)

    with pytest.raises(ManifestError, match=match):
        load_scenario(path)


def test_missing_dashboard_time_fields_default_to_empty(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: (
            doc["dashboard"].pop("time_from"),
            doc["dashboard"].pop("time_to"),
        ),
    )

    scenario = load_scenario(path)

    assert scenario.time_from == ""
    assert scenario.time_to == ""


def test_empty_dashboard_time_fields_are_allowed(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["dashboard"].update({"time_from": "", "time_to": ""}),
    )

    scenario = load_scenario(path)

    assert scenario.time_from == ""
    assert scenario.time_to == ""


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

    with pytest.raises(ManifestError, match=r"combinations\[0\]\.selections undeclared control key"):
        load_scenario(path)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("adapter", "unsupported_adapter", r"controls\[0\]\.adapter unsupported adapter"),
        ("capability", "unsupported_capability", r"controls\[0\]\.capability unsupported capability"),
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

    with pytest.raises(ManifestError, match=r"controls\[0\]\.options.strategy unsupported option strategy"):
        load_scenario(path)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda doc: doc.update({"id": ""}), "root.id must not be empty"),
        (lambda doc: doc.update({"title": "   "}), "root.title must not be empty"),
        (lambda doc: doc["source"].update({"kind": ""}), "source.kind must not be empty"),
        (lambda doc: doc["source"].update({"path": ""}), "source.path must not be empty"),
        (lambda doc: doc["dashboard"].update({"title": ""}), "dashboard.title must not be empty"),
        (
            lambda doc: doc["controls"][0].update({"label": ""}),
            r"controls\[0\]\.label must not be empty",
        ),
        (lambda doc: doc["controls"][0].update({"key": ""}), r"controls\[0\]\.key must not be empty"),
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

    with pytest.raises(ManifestError, match=r"controls\[0\]\.options.include must be non-empty when strategy is declared"):
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

    with pytest.raises(ManifestError, match=r"controls\[0\]\.assertions.affected_panels must be query_dependency"):
        load_scenario(path)


def test_affected_panels_rejects_kibana_uuid(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0]["assertions"].update(
            {"affected_panels": ["a1b2c3d4-e5f6-7890-abcd-ef1234567890"]}
        ),
        name="kibana-uuid-panels.yaml",
    )

    with pytest.raises(ManifestError, match=r"controls\[0\]\.assertions.affected_panels must not declare generated Kibana UUID"):
        load_scenario(path)


def test_unaffected_panels_rejects_kibana_uuid(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0]["assertions"].update(
            {"unaffected_panels": ["a1b2c3d4-e5f6-7890-abcd-ef1234567890"]}
        ),
        name="kibana-uuid-unaffected-panels.yaml",
    )

    with pytest.raises(ManifestError, match=r"controls\[0\]\.assertions.unaffected_panels must not declare generated Kibana UUID"):
        load_scenario(path)


def test_negative_minimum_rows_rejects(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0]["assertions"].update({"minimum_rows": -1}),
        name="negative-minimum-rows.yaml",
    )

    with pytest.raises(ManifestError, match=r"controls\[0\]\.assertions.minimum_rows must not be negative"):
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

    with pytest.raises(ManifestError, match=r"noise_allowances\[0\]\.rationale must not be empty"):
        load_scenario(path)


@pytest.mark.parametrize("expected_gap", ["   ", "\t"])
def test_whitespace_only_expected_gap_rejects(tmp_path: Path, expected_gap: str) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["controls"][0].update({"expected_gap": expected_gap}),
        name="whitespace-expected-gap.yaml",
    )

    with pytest.raises(
        ManifestError,
        match=r"controls\[0\]\.expected_gap must not be whitespace-only",
    ):
        load_scenario(path)


def test_missing_manifest_path_rejects(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"

    with pytest.raises(ManifestError, match=f"{missing}: unreadable manifest"):
        load_scenario(missing)


def test_non_string_combination_selection_value_rejects(tmp_path: Path) -> None:
    path = _mutated_manifest(
        tmp_path,
        lambda doc: doc["combinations"][0]["selections"].update({"namespace": 123}),
        name="non-string-selection.yaml",
    )

    with pytest.raises(
        ManifestError,
        match=r"combinations\[0\]\.selections\['namespace'\] must be a string",
    ):
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


# --- Execution plan (Task 4) ------------------------------------------------


def _control(
    key: str,
    *,
    capability: CapabilityCategory = CapabilityCategory.MIGRATED_LIVE,
    options: OptionPolicy | None = None,
    label: str | None = None,
) -> ControlScenario:
    return ControlScenario(
        label=label or key,
        key=key,
        adapter="esql_value",
        capability=capability,
        options=options or OptionPolicy(strategy="every"),
        assertions=Assertions(),
    )


def _scenario(
    controls: tuple[ControlScenario, ...],
    combinations: tuple[CombinationScenario, ...] = (),
) -> DashboardScenario:
    return DashboardScenario(
        version=1,
        id="test",
        title="Test",
        source_kind="grafana",
        source_path="p",
        control_schema_path="",
        dashboard_title="T",
        time_from="",
        time_to="",
        controls=controls,
        combinations=combinations,
        noise_allowances=(),
    )


def _combination(
    combination_id: str,
    selections: dict[str, str],
) -> CombinationScenario:
    return CombinationScenario(
        id=combination_id,
        selections=MappingProxyType(selections),
    )


def test_every_option_runs_from_a_fresh_baseline() -> None:
    scenario = _scenario(
        (
            _control("namespace"),
            _control("instance"),
        ),
        (
            _combination(
                "namespace-and-instance",
                {
                    "namespace": "namespace_1",
                    "instance": "service.instance.id_2",
                },
            ),
        ),
    )
    plan = build_execution_plan(
        scenario,
        [
            DiscoveredControl("namespace", "namespace", ("ns_1", "ns_2"), ("ns_1",)),
            DiscoveredControl("instance", "instance", ("redis_1", "redis_2"), ("redis_1",)),
        ],
    )
    assert [(step.kind, dict(step.selections)) for step in plan] == [
        ("option", {"namespace": "ns_1"}),
        ("option", {"namespace": "ns_2"}),
        ("option", {"instance": "redis_1"}),
        ("option", {"instance": "redis_2"}),
        (
            "combination",
            {"namespace": "namespace_1", "instance": "service.instance.id_2"},
        ),
    ]
    assert all(step.reset_before for step in plan)
    combo = plan[-1]
    assert combo.id == "namespace-and-instance"


def test_ordinary_option_ids_remain_key_equals_option() -> None:
    scenario = _scenario((_control("namespace"),))
    plan = build_execution_plan(
        scenario,
        [DiscoveredControl("namespace", "namespace", ("ns_1", "ns_2"))],
    )
    assert [step.id for step in plan] == ["namespace=ns_1", "namespace=ns_2"]


def test_every_with_exclude_and_include_required_missing_metadata() -> None:
    scenario = _scenario(
        (
            _control(
                "namespace",
                options=OptionPolicy(
                    strategy="every",
                    include=("required_missing", "ns_1"),
                    exclude=("ns_2",),
                ),
            ),
        ),
    )
    plan = build_execution_plan(
        scenario,
        [DiscoveredControl("namespace", "namespace", ("ns_1", "ns_2", "ns_3"))],
    )
    assert len(plan) == 2
    assert [(step.kind, dict(step.selections)) for step in plan] == [
        ("option", {"namespace": "ns_1"}),
        ("option", {"namespace": "ns_3"}),
    ]
    for step in plan:
        assert step.skipped_options == ("ns_2",)
        assert step.missing_declared_options == ("required_missing",)


def test_declared_include_order_and_absent_declared_option() -> None:
    scenario = _scenario(
        (
            _control(
                "region",
                options=OptionPolicy(
                    strategy="declared",
                    include=("us-east", "eu-west", "missing-region"),
                    exclude=("eu-west",),
                ),
            ),
        ),
    )
    plan = build_execution_plan(
        scenario,
        [DiscoveredControl("region", "region", ("ap-south", "us-east", "eu-west"))],
    )
    assert len(plan) == 1
    step = plan[0]
    assert step.kind == "option"
    assert dict(step.selections) == {"region": "us-east"}
    assert step.skipped_options == ("eu-west",)
    assert step.missing_declared_options == ("missing-region",)


def test_no_runnable_declared_options_yields_missing_option() -> None:
    scenario = _scenario(
        (
            _control(
                "region",
                options=OptionPolicy(
                    strategy="declared",
                    include=("us-east", "eu-west"),
                ),
            ),
        ),
    )
    plan = build_execution_plan(
        scenario,
        [DiscoveredControl("region", "region", ("ap-south",))],
    )
    assert len(plan) == 1
    step = plan[0]
    assert step.kind == "missing_option"
    assert step.id == "region:missing_option"
    assert dict(step.selections) == {}
    assert step.missing_declared_options == ("us-east", "eu-west")


@pytest.mark.parametrize(
    "capability",
    [CapabilityCategory.MIGRATION_GAP, CapabilityCategory.SOURCE_ONLY],
)
def test_gap_capability_absent_yields_coverage_gap(
    capability: CapabilityCategory,
) -> None:
    scenario = _scenario((_control("legacy_filter", capability=capability),))
    plan = build_execution_plan(scenario, [])
    assert len(plan) == 1
    step = plan[0]
    assert step.kind == "coverage_gap"
    assert step.id == "legacy_filter:coverage_gap"
    assert step.capability is capability
    assert dict(step.selections) == {}


@pytest.mark.parametrize(
    "capability",
    [CapabilityCategory.MIGRATED_LIVE, CapabilityCategory.KIBANA_ONLY],
)
def test_live_capability_absent_yields_missing_control(
    capability: CapabilityCategory,
) -> None:
    scenario = _scenario((_control("namespace", capability=capability),))
    plan = build_execution_plan(scenario, [])
    assert len(plan) == 1
    step = plan[0]
    assert step.kind == "missing_control"
    assert step.id == "namespace:missing_control"
    assert step.capability is capability


def test_discovered_controls_not_in_manifest_are_ignored() -> None:
    scenario = _scenario((_control("namespace"),))
    plan = build_execution_plan(
        scenario,
        [
            DiscoveredControl("namespace", "namespace", ("ns_1",)),
            DiscoveredControl("orphan", "orphan", ("x",)),
        ],
    )
    assert [(step.kind, dict(step.selections)) for step in plan] == [
        ("option", {"namespace": "ns_1"}),
    ]


def test_duplicate_discovered_keys_reject() -> None:
    scenario = _scenario((_control("namespace"),))
    with pytest.raises(ManifestError, match="duplicate discovered control key"):
        build_execution_plan(
            scenario,
            [
                DiscoveredControl("namespace", "namespace", ("ns_1",)),
                DiscoveredControl("namespace", "namespace duplicate", ("ns_2",)),
            ],
        )


def test_duplicate_discovered_options_reject() -> None:
    scenario = _scenario((_control("namespace"),))
    with pytest.raises(ManifestError, match="duplicate discovered option"):
        build_execution_plan(
            scenario,
            [DiscoveredControl("namespace", "namespace", ("ns_1", "ns_1"))],
        )


def test_browser_order_and_scenario_order_preserved() -> None:
    scenario = _scenario(
        (
            _control("alpha"),
            _control("beta"),
            _control("gamma"),
        ),
    )
    plan = build_execution_plan(
        scenario,
        [
            DiscoveredControl("gamma", "gamma", ("g3", "g1", "g2")),
            DiscoveredControl("alpha", "alpha", ("a2", "a1")),
            DiscoveredControl("beta", "beta", ("b1",)),
        ],
    )
    assert [next(iter(step.selections.values()), None) for step in plan[:5]] == [
        "a2",
        "a1",
        "b1",
        "g3",
        "g1",
    ]


def test_no_cartesian_combinations() -> None:
    scenario = _scenario(
        (
            _control("namespace"),
            _control("instance"),
        ),
    )
    plan = build_execution_plan(
        scenario,
        [
            DiscoveredControl("namespace", "namespace", ("ns_1", "ns_2")),
            DiscoveredControl("instance", "instance", ("redis_1", "redis_2")),
        ],
    )
    option_steps = [step for step in plan if step.kind == "option"]
    assert len(option_steps) == 4
    assert all(len(step.selections) == 1 for step in option_steps)


def test_combination_value_not_in_initial_options_is_retained() -> None:
    scenario = _scenario(
        (_control("namespace"), _control("instance")),
        (
            _combination(
                "chained-selection",
                {"namespace": "dynamic_ns", "instance": "dynamic_instance"},
            ),
        ),
    )
    plan = build_execution_plan(
        scenario,
        [
            DiscoveredControl("namespace", "namespace", ("ns_1",)),
            DiscoveredControl("instance", "instance", ("redis_1",)),
        ],
    )
    combo = plan[-1]
    assert combo.kind == "combination"
    assert combo.id == "chained-selection"
    assert dict(combo.selections) == {
        "namespace": "dynamic_ns",
        "instance": "dynamic_instance",
    }


def test_empty_discovered_options_yields_missing_option() -> None:
    scenario = _scenario((_control("namespace"),))
    plan = build_execution_plan(
        scenario,
        [DiscoveredControl("namespace", "namespace", ())],
    )
    assert len(plan) == 1
    assert plan[0].kind == "missing_option"


def test_plan_selections_are_immutable() -> None:
    scenario = _scenario((_control("namespace"),))
    plan = build_execution_plan(
        scenario,
        [DiscoveredControl("namespace", "namespace", ("ns_1",))],
    )
    step = plan[0]
    assert isinstance(step, InteractionStep)
    assert isinstance(step.selections, MappingProxyType)
    with pytest.raises(TypeError):
        step.selections["namespace"] = "mutated"  # type: ignore[index]


def test_safe_step_ids_are_collision_resistant_and_deterministic() -> None:
    scenario = _scenario((_control("namespace"),))
    discovered = [DiscoveredControl("namespace", "namespace", ("a/b",))]

    plan_first = build_execution_plan(scenario, discovered)
    plan_second = build_execution_plan(scenario, discovered)
    assert plan_first[0].id == plan_second[0].id

    slug_id = build_execution_plan(
        scenario,
        [DiscoveredControl("namespace", "namespace", ("a_b",))],
    )[0].id
    slash_id = plan_first[0].id

    assert slug_id == "namespace=a_b"
    assert slash_id == f"namespace=a_b_{_stable_component_hash('a/b')}"
    assert slug_id != slash_id
    assert dict(plan_first[0].selections) == {"namespace": "a/b"}


def test_safe_step_ids_contain_no_slash_or_space() -> None:
    scenario = _scenario((_control("path/to control"),))
    plan = build_execution_plan(
        scenario,
        [DiscoveredControl("path/to control", "path", ("foo/bar baz",))],
    )
    step = plan[0]
    assert "/" not in step.id
    assert " " not in step.id
    assert dict(step.selections) == {"path/to control": "foo/bar baz"}


def test_unsafe_combination_id_is_sanitized_with_hash() -> None:
    scenario = _scenario(
        (_control("namespace"),),
        (_combination("combo/with space", {"namespace": "ns_1"}),),
    )
    plan = build_execution_plan(
        scenario,
        [DiscoveredControl("namespace", "namespace", ("ns_1",))],
    )
    combo = plan[-1]
    assert combo.id == _safe_step_id_component("combo/with space")
    assert combo.id != "combo/with space"
    assert dict(combo.selections) == {"namespace": "ns_1"}


def test_safe_deterministic_ids_for_special_characters() -> None:
    scenario = _scenario((_control("path"),))
    plan = build_execution_plan(
        scenario,
        [DiscoveredControl("path", "path", ("foo/bar baz",))],
    )
    step = plan[0]
    assert step.id == f"path=foo_bar_baz_{_stable_component_hash('foo/bar baz')}"
    assert dict(step.selections) == {"path": "foo/bar baz"}
