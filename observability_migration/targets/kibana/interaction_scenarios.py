# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Strict loader for dashboard interaction scenario manifests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from observability_migration.targets.kibana.interaction_audit import CapabilityCategory

_ROOT_KEYS = frozenset(
    {
        "version",
        "id",
        "title",
        "source",
        "dashboard",
        "controls",
        "combinations",
        "noise_allowances",
    }
)
_SOURCE_KEYS = frozenset({"kind", "path", "control_schema"})
_DASHBOARD_KEYS = frozenset({"title", "time_from", "time_to"})
_CONTROL_KEYS = frozenset(
    {
        "label",
        "key",
        "adapter",
        "capability",
        "options",
        "assertions",
        "expected_gap",
    }
)
_OPTIONS_KEYS = frozenset({"strategy", "include", "exclude"})
_ASSERTIONS_KEYS = frozenset(
    {
        "selection",
        "affected_panels",
        "unaffected_panels",
        "query_contains",
        "query_not_contains",
        "required_columns",
        "stable_alias",
        "minimum_rows",
        "expected_legend",
        "expect_data_change",
        "allow_incompatible_selections",
    }
)
_COMBINATION_KEYS = frozenset({"id", "selections"})
_NOISE_KEYS = frozenset({"endpoint", "method", "status", "rationale"})

_SUPPORTED_ADAPTERS = frozenset(
    {
        "esql_value",
        "esql_field",
        "esql_function",
        "esql_interval",
        "options_list",
        "range_slider",
        "query_bar",
        "filter_pill",
        "time_range",
        "panel_filter",
    }
)
_SUPPORTED_STRATEGIES = frozenset({"every", "declared"})
_AFFECTED_PANELS_STRINGS = frozenset({"query_dependency", "all_query_panels"})
_KIBANA_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_CAPABILITY_BY_VALUE = {item.value: item for item in CapabilityCategory}


class ManifestError(ValueError):
    """Raised when a scenario manifest violates the strict contract."""


@dataclass(frozen=True)
class OptionPolicy:
    strategy: str = "every"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assertions:
    selection: tuple[str, ...] = ()
    affected_panels: str | tuple[str, ...] = "query_dependency"
    unaffected_panels: tuple[str, ...] = ()
    query_contains: tuple[str, ...] = ()
    query_not_contains: tuple[str, ...] = ()
    required_columns: tuple[str, ...] = ()
    stable_alias: str = ""
    minimum_rows: int = 0
    expected_legend: tuple[str, ...] = ()
    expect_data_change: bool = True
    allow_incompatible_selections: bool = False


@dataclass(frozen=True)
class ControlScenario:
    label: str
    key: str
    adapter: str
    capability: CapabilityCategory
    options: OptionPolicy
    assertions: Assertions
    expected_gap: str = ""


@dataclass(frozen=True)
class CombinationScenario:
    id: str
    selections: Mapping[str, str]


@dataclass(frozen=True)
class NoiseAllowance:
    endpoint: str
    method: str
    status: int
    rationale: str


@dataclass(frozen=True)
class DashboardScenario:
    version: int
    id: str
    title: str
    source_kind: str
    source_path: str
    control_schema_path: str
    dashboard_title: str
    time_from: str
    time_to: str
    controls: tuple[ControlScenario, ...]
    combinations: tuple[CombinationScenario, ...]
    noise_allowances: tuple[NoiseAllowance, ...]


def load_scenario(path: str | Path) -> DashboardScenario:
    manifest_path = Path(path)
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"{manifest_path}: unreadable manifest: {exc}") from exc

    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{manifest_path}: invalid YAML: {exc}") from exc

    if not isinstance(document, dict):
        raise ManifestError(f"{manifest_path}: manifest root must be a mapping")

    return _parse_root(document, manifest_path)


def _unknown_keys(mapping: Mapping[str, Any], allowed: frozenset[str]) -> tuple[str, ...]:
    return tuple(sorted(key for key in mapping if key not in allowed))


def _reject_unknown_keys(
    mapping: Mapping[str, Any],
    allowed: frozenset[str],
    manifest_path: Path,
    section: str,
) -> None:
    unknown = _unknown_keys(mapping, allowed)
    if unknown:
        joined = ", ".join(unknown)
        raise ManifestError(f"{manifest_path}: unknown {section} key(s): {joined}")


def _require_mapping(
    value: Any,
    manifest_path: Path,
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{manifest_path}: {field} must be a mapping")
    return value


def _require_list(
    value: Any,
    manifest_path: Path,
    field: str,
) -> list[Any]:
    if not isinstance(value, list):
        raise ManifestError(f"{manifest_path}: {field} must be a list")
    return value


def _require_non_empty_str(
    value: Any,
    manifest_path: Path,
    field: str,
) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{manifest_path}: {field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ManifestError(f"{manifest_path}: {field} must not be empty")
    return cleaned


def _require_str(value: Any, manifest_path: Path, field: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{manifest_path}: {field} must be a string")
    return value


def _require_bool(value: Any, manifest_path: Path, field: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError(f"{manifest_path}: {field} must be a boolean")
    return value


def _require_int(value: Any, manifest_path: Path, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{manifest_path}: {field} must be an integer")
    return value


def _string_tuple(value: Any, manifest_path: Path, field: str) -> tuple[str, ...]:
    items = _require_list(value, manifest_path, field)
    parsed: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ManifestError(
                f"{manifest_path}: {field}[{index}] must be a string"
            )
        parsed.append(item)
    return tuple(parsed)


def _parse_root(document: dict[str, Any], manifest_path: Path) -> DashboardScenario:
    _reject_unknown_keys(document, _ROOT_KEYS, manifest_path, "root")

    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ManifestError(f"{manifest_path}: version must be an integer")
    if version != 1:
        raise ManifestError(f"{manifest_path}: unsupported manifest version: {version}")

    scenario_id = _require_non_empty_str(document.get("id"), manifest_path, "id")
    title = _require_non_empty_str(document.get("title"), manifest_path, "title")

    source = _require_mapping(document.get("source"), manifest_path, "source")
    _reject_unknown_keys(source, _SOURCE_KEYS, manifest_path, "source")
    source_kind = _require_non_empty_str(source.get("kind"), manifest_path, "source kind")
    source_path = _require_non_empty_str(source.get("path"), manifest_path, "source path")
    control_schema_path = _require_str(
        source.get("control_schema"), manifest_path, "source.control_schema"
    )

    dashboard = _require_mapping(document.get("dashboard"), manifest_path, "dashboard")
    _reject_unknown_keys(dashboard, _DASHBOARD_KEYS, manifest_path, "dashboard")
    dashboard_title = _require_non_empty_str(
        dashboard.get("title"), manifest_path, "dashboard title"
    )
    time_from = _require_str(dashboard.get("time_from"), manifest_path, "dashboard.time_from")
    time_to = _require_str(dashboard.get("time_to"), manifest_path, "dashboard.time_to")

    controls = _parse_controls(document.get("controls"), manifest_path)
    control_keys = {control.key for control in controls}

    combinations = _parse_combinations(
        document.get("combinations"), manifest_path, control_keys
    )
    noise_allowances = _parse_noise_allowances(
        document.get("noise_allowances"), manifest_path
    )

    return DashboardScenario(
        version=version,
        id=scenario_id,
        title=title,
        source_kind=source_kind,
        source_path=source_path,
        control_schema_path=control_schema_path,
        dashboard_title=dashboard_title,
        time_from=time_from,
        time_to=time_to,
        controls=controls,
        combinations=combinations,
        noise_allowances=noise_allowances,
    )


def _parse_controls(value: Any, manifest_path: Path) -> tuple[ControlScenario, ...]:
    raw_controls = _require_list(value, manifest_path, "controls")
    parsed: list[ControlScenario] = []
    seen_keys: set[str] = set()

    for index, raw_control in enumerate(raw_controls):
        field_prefix = f"controls[{index}]"
        control = _require_mapping(raw_control, manifest_path, field_prefix)
        _reject_unknown_keys(control, _CONTROL_KEYS, manifest_path, "control")

        label = _require_non_empty_str(control.get("label"), manifest_path, "control label")
        key = _require_non_empty_str(control.get("key"), manifest_path, "control key")
        if key in seen_keys:
            raise ManifestError(f"{manifest_path}: duplicate control key: {key}")
        seen_keys.add(key)

        adapter = _require_str(control.get("adapter"), manifest_path, f"{field_prefix}.adapter")
        if adapter not in _SUPPORTED_ADAPTERS:
            raise ManifestError(f"{manifest_path}: unsupported adapter: {adapter}")

        capability_raw = _require_str(
            control.get("capability"), manifest_path, f"{field_prefix}.capability"
        )
        capability = _CAPABILITY_BY_VALUE.get(capability_raw)
        if capability is None:
            raise ManifestError(
                f"{manifest_path}: unsupported capability: {capability_raw}"
            )

        options = _parse_options(control.get("options"), manifest_path, field_prefix)
        assertions = _parse_assertions(
            control.get("assertions"), manifest_path, field_prefix
        )

        expected_gap = ""
        if "expected_gap" in control:
            expected_gap = _require_str(
                control.get("expected_gap"), manifest_path, f"{field_prefix}.expected_gap"
            )
            if not expected_gap.strip():
                raise ManifestError(
                    f"{manifest_path}: {field_prefix}.expected_gap must not be whitespace-only"
                )

        parsed.append(
            ControlScenario(
                label=label,
                key=key,
                adapter=adapter,
                capability=capability,
                options=options,
                assertions=assertions,
                expected_gap=expected_gap,
            )
        )

    return tuple(parsed)


def _parse_options(
    value: Any,
    manifest_path: Path,
    field_prefix: str,
) -> OptionPolicy:
    options = _require_mapping(value, manifest_path, f"{field_prefix}.options")
    _reject_unknown_keys(options, _OPTIONS_KEYS, manifest_path, "options")

    strategy = "every"
    if "strategy" in options:
        strategy = _require_str(
            options.get("strategy"), manifest_path, f"{field_prefix}.options.strategy"
        )
        if strategy not in _SUPPORTED_STRATEGIES:
            raise ManifestError(
                f"{manifest_path}: unsupported option strategy: {strategy}"
            )

    include = _string_tuple(
        options.get("include", []),
        manifest_path,
        f"{field_prefix}.options.include",
    )
    exclude = _string_tuple(
        options.get("exclude", []),
        manifest_path,
        f"{field_prefix}.options.exclude",
    )

    if strategy == "declared" and not include:
        raise ManifestError(
            f"{manifest_path}: {field_prefix}.options.include must be non-empty when strategy is declared"
        )

    return OptionPolicy(strategy=strategy, include=include, exclude=exclude)


def _parse_affected_panels(
    value: Any,
    manifest_path: Path,
    field: str,
) -> str | tuple[str, ...]:
    if isinstance(value, str):
        if value not in _AFFECTED_PANELS_STRINGS:
            raise ManifestError(
                f"{manifest_path}: {field} must be query_dependency, all_query_panels, or a list of stable panel identifiers"
            )
        return value

    panel_ids = _string_tuple(value, manifest_path, field)
    for panel_id in panel_ids:
        if _KIBANA_UUID_RE.match(panel_id):
            raise ManifestError(
                f"{manifest_path}: {field} must not declare generated Kibana UUID panel identifiers"
            )
    return panel_ids


def _parse_assertions(
    value: Any,
    manifest_path: Path,
    field_prefix: str,
) -> Assertions:
    assertions = _require_mapping(value, manifest_path, f"{field_prefix}.assertions")
    _reject_unknown_keys(assertions, _ASSERTIONS_KEYS, manifest_path, "assertions")

    selection = _string_tuple(
        assertions.get("selection", []),
        manifest_path,
        f"{field_prefix}.assertions.selection",
    )
    affected_panels = _parse_affected_panels(
        assertions.get("affected_panels", "query_dependency"),
        manifest_path,
        f"{field_prefix}.assertions.affected_panels",
    )
    unaffected_panels = _string_tuple(
        assertions.get("unaffected_panels", []),
        manifest_path,
        f"{field_prefix}.assertions.unaffected_panels",
    )
    query_contains = _string_tuple(
        assertions.get("query_contains", []),
        manifest_path,
        f"{field_prefix}.assertions.query_contains",
    )
    query_not_contains = _string_tuple(
        assertions.get("query_not_contains", []),
        manifest_path,
        f"{field_prefix}.assertions.query_not_contains",
    )
    required_columns = _string_tuple(
        assertions.get("required_columns", []),
        manifest_path,
        f"{field_prefix}.assertions.required_columns",
    )
    stable_alias = _require_str(
        assertions.get("stable_alias", ""),
        manifest_path,
        f"{field_prefix}.assertions.stable_alias",
    )
    minimum_rows = _require_int(
        assertions.get("minimum_rows", 0),
        manifest_path,
        f"{field_prefix}.assertions.minimum_rows",
    )
    if minimum_rows < 0:
        raise ManifestError(
            f"{manifest_path}: {field_prefix}.assertions.minimum_rows must not be negative"
        )
    expected_legend = _string_tuple(
        assertions.get("expected_legend", []),
        manifest_path,
        f"{field_prefix}.assertions.expected_legend",
    )
    expect_data_change = _require_bool(
        assertions.get("expect_data_change", True),
        manifest_path,
        f"{field_prefix}.assertions.expect_data_change",
    )
    allow_incompatible_selections = _require_bool(
        assertions.get("allow_incompatible_selections", False),
        manifest_path,
        f"{field_prefix}.assertions.allow_incompatible_selections",
    )

    return Assertions(
        selection=selection,
        affected_panels=affected_panels,
        unaffected_panels=unaffected_panels,
        query_contains=query_contains,
        query_not_contains=query_not_contains,
        required_columns=required_columns,
        stable_alias=stable_alias,
        minimum_rows=minimum_rows,
        expected_legend=expected_legend,
        expect_data_change=expect_data_change,
        allow_incompatible_selections=allow_incompatible_selections,
    )


def _parse_combinations(
    value: Any,
    manifest_path: Path,
    control_keys: set[str],
) -> tuple[CombinationScenario, ...]:
    raw_combinations = _require_list(value, manifest_path, "combinations")
    parsed: list[CombinationScenario] = []
    seen_ids: set[str] = set()

    for index, raw_combination in enumerate(raw_combinations):
        field_prefix = f"combinations[{index}]"
        combination = _require_mapping(raw_combination, manifest_path, field_prefix)
        _reject_unknown_keys(combination, _COMBINATION_KEYS, manifest_path, "combination")

        combination_id = _require_non_empty_str(
            combination.get("id"), manifest_path, f"{field_prefix}.id"
        )
        if combination_id in seen_ids:
            raise ManifestError(
                f"{manifest_path}: duplicate combination id: {combination_id}"
            )
        seen_ids.add(combination_id)

        selections_raw = _require_mapping(
            combination.get("selections"), manifest_path, f"{field_prefix}.selections"
        )
        if not isinstance(selections_raw, dict):
            raise ManifestError(
                f"{manifest_path}: {field_prefix}.selections must be a mapping"
            )

        selections: dict[str, str] = {}
        for selection_key, selection_value in selections_raw.items():
            if not isinstance(selection_key, str):
                raise ManifestError(
                    f"{manifest_path}: {field_prefix}.selections keys must be strings"
                )
            if selection_key not in control_keys:
                raise ManifestError(
                    f"{manifest_path}: undeclared control key in combination selections: {selection_key}"
                )
            if not isinstance(selection_value, str):
                raise ManifestError(
                    f"{manifest_path}: {field_prefix}.selections[{selection_key!r}] must be a string"
                )
            selections[selection_key] = selection_value

        parsed.append(
            CombinationScenario(
                id=combination_id,
                selections=MappingProxyType(selections),
            )
        )

    return tuple(parsed)


def _parse_noise_allowances(value: Any, manifest_path: Path) -> tuple[NoiseAllowance, ...]:
    raw_allowances = _require_list(value, manifest_path, "noise_allowances")
    parsed: list[NoiseAllowance] = []

    for index, raw_allowance in enumerate(raw_allowances):
        field_prefix = f"noise_allowances[{index}]"
        allowance = _require_mapping(raw_allowance, manifest_path, field_prefix)
        _reject_unknown_keys(allowance, _NOISE_KEYS, manifest_path, "noise allowance")

        endpoint = _require_non_empty_str(
            allowance.get("endpoint"), manifest_path, f"{field_prefix}.endpoint"
        )
        method = _require_non_empty_str(
            allowance.get("method"), manifest_path, f"{field_prefix}.method"
        )
        status = _require_int(
            allowance.get("status"), manifest_path, f"{field_prefix}.status"
        )
        if status < 100 or status > 599:
            raise ManifestError(
                f"{manifest_path}: {field_prefix}.status must be a valid HTTP status code"
            )
        rationale = _require_str(
            allowance.get("rationale"), manifest_path, f"{field_prefix}.rationale"
        )
        if not rationale.strip():
            raise ManifestError(
                f"{manifest_path}: {field_prefix}.rationale must not be empty"
            )

        parsed.append(
            NoiseAllowance(
                endpoint=endpoint,
                method=method,
                status=status,
                rationale=rationale,
            )
        )

    return tuple(parsed)
