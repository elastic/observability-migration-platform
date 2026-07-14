# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Execute dashboard interaction scenarios and write redacted step artifacts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from observability_migration.targets.kibana.interaction_audit import (
    CapabilityCategory,
    FailureClass,
    InteractionFinding,
    InteractionReport,
    InteractionResult,
    InteractionStatus,
    NetworkEvidence,
    PanelEvidence,
    check_network_contract,
    match_noise_allowance,
    redact_evidence,
)
from observability_migration.targets.kibana.interaction_driver import (
    BrowserAdapter,
    BrowserAdapterError,
    BrowserObservation,
    CaptureCursor,
    ControlNotFound,
    ControlState,
    OptionNotFound,
    SelectionDidNotStick,
    SettlePolicy,
    SettleTimeout,
)
from observability_migration.targets.kibana.interaction_scenarios import (
    ControlScenario,
    DashboardScenario,
    DiscoveredControl,
    InteractionStep,
    build_execution_plan,
)
from observability_migration.targets.kibana.render_audit import classify_panel

_ESQL_PATH_PREFIX = "/internal/search/esql"
_ESQL_VALUE_ADAPTERS = frozenset({"esql_value", "esql_interval"})
_ESQL_IDENTIFIER_ADAPTERS = frozenset({"esql_field", "esql_function"})

_HARD_FAILURE_CLASSES = frozenset(
    {
        FailureClass.INTERACTION_REGRESSION,
        FailureClass.QUERY_CONTRACT_ERROR,
        FailureClass.QUERY_CONTRACT_MISMATCH,
        FailureClass.CONTROL_NOT_FOUND,
        FailureClass.OPTION_NOT_FOUND,
        FailureClass.SELECTION_DID_NOT_STICK,
        FailureClass.EXPECTED_REQUEST_MISSING,
        FailureClass.UNEXPECTED_PANEL_REQUEST,
        FailureClass.RENDER_ERROR,
        FailureClass.CONSOLE_ERROR,
        FailureClass.SERVER_ERROR,
        FailureClass.SETTLE_TIMEOUT,
        FailureClass.FRAMEWORK_ERROR,
    }
)

_WARN_FAILURE_CLASSES = frozenset(
    {
        FailureClass.COVERAGE_GAP,
        FailureClass.FIELD_GAP,
        FailureClass.DATA_GAP,
        FailureClass.UNEXPECTED_EMPTY,
    }
)

_MAX_SNAPSHOT_TEXT = 32 * 1024

_URL_USERINFO_RE = re.compile(r"https?://[^\s/]+:[^\s/@]+@[^\s/]+", re.IGNORECASE)
_SENSITIVE_LINE_ASSIGNMENT_RE = re.compile(
    r"^(\s*(?:authorization|cookie|set-cookie|x-elastic-api-key|api_key)\s*[:=]\s*).+$",
    re.IGNORECASE,
)
_INLINE_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(authorization|cookie|set-cookie|x-elastic-api-key|api_key)\s*[:=]\s*\S+",
)
_UNSAFE_ARTIFACT_COMPONENTS = frozenset({".", ".."})
_MAX_BOUND_IO_ERROR = 2048

_CAPABILITY_PRECEDENCE: tuple[CapabilityCategory, ...] = (
    CapabilityCategory.MIGRATION_GAP,
    CapabilityCategory.SOURCE_ONLY,
    CapabilityCategory.KIBANA_ONLY,
    CapabilityCategory.MIGRATED_LIVE,
)


@dataclass(frozen=True)
class PanelContract:
    all_query_panels: tuple[str, ...] = ()
    by_control: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    panel_aliases: Mapping[str, str] = field(default_factory=dict)

    def resolve_panel_ids(self, panel_ids: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                self.panel_aliases.get(str(panel_id), str(panel_id))
                for panel_id in panel_ids
                if str(panel_id)
            )
        )


@dataclass(frozen=True)
class RunConfig:
    dashboard_url: str
    artifact_root: Path
    run_id: str
    settle_policy: SettlePolicy = field(default_factory=SettlePolicy)


@dataclass(frozen=True)
class _MergedAssertions:
    query_contains: tuple[str, ...]
    query_not_contains: tuple[str, ...]
    required_columns: tuple[str, ...]
    stable_alias: str
    minimum_rows: int
    expected_legend: tuple[str, ...]
    expect_data_change: bool
    allow_incompatible_selections: bool
    expected_panels: tuple[str, ...]
    unaffected_panels: tuple[str, ...]
    expected_value_params: dict[str, object]
    expected_identifier_params: dict[str, str]


def _bound_io_error(message: str) -> str:
    cleaned = " ".join(str(message or "").split())
    if len(cleaned) <= _MAX_BOUND_IO_ERROR:
        return cleaned
    return cleaned[:_MAX_BOUND_IO_ERROR]


def _validate_artifact_component(name: str, value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"invalid {name}: must not be empty")
    if cleaned in _UNSAFE_ARTIFACT_COMPONENTS:
        raise ValueError(f"invalid {name}: {cleaned!r} is not allowed")
    if "\0" in cleaned or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"invalid {name}: path separators and NUL are not allowed")
    return cleaned


def _resolve_under_root(root: Path, *parts: str) -> Path:
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*parts).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError(
            f"artifact path {candidate} escapes artifact root {resolved_root}"
        )
    return candidate


def validate_run_artifact_paths(
    artifact_root: Path,
    scenario_id: str,
    run_id: str,
) -> tuple[Path, Path]:
    """Validate scenario/run identifiers and return resolved run/step roots."""
    safe_scenario_id = _validate_artifact_component("scenario_id", scenario_id)
    safe_run_id = _validate_artifact_component("run_id", run_id)
    resolved_root = artifact_root.expanduser().resolve()
    run_root = _resolve_under_root(resolved_root, safe_scenario_id, safe_run_id)
    return resolved_root, run_root


def _derive_combination_capability(
    controls: Sequence[ControlScenario],
) -> CapabilityCategory:
    present = {control.capability for control in controls}
    for capability in _CAPABILITY_PRECEDENCE:
        if capability in present:
            return capability
    return CapabilityCategory.MIGRATED_LIVE


def _step_capability(
    step: InteractionStep,
    controls_by_key: Mapping[str, ControlScenario],
) -> CapabilityCategory:
    if step.kind != "combination":
        return step.capability
    controls = _controls_for_step(step, controls_by_key)
    return _derive_combination_capability(controls)


def format_runtime_error(exc: BaseException) -> str:
    message = _bound_io_error(str(exc))
    return f"ERROR: {_redact_artifact_text(message)}"


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in values if str(item)))


def _is_esql_endpoint(endpoint: str) -> bool:
    if not endpoint.startswith(_ESQL_PATH_PREFIX):
        return False
    if endpoint == _ESQL_PATH_PREFIX:
        return True
    suffix = endpoint[len(_ESQL_PATH_PREFIX) :]
    return suffix.startswith(("/", "_"))


def _panel_fingerprint(panels: Sequence[PanelEvidence]) -> tuple[tuple[str, str, str], ...]:
    return tuple((panel.panel_id, panel.status, panel.detail) for panel in panels)


def _dedupe_findings(findings: Sequence[InteractionFinding]) -> list[InteractionFinding]:
    deduped: list[InteractionFinding] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding.failure_class.value, finding.detail)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def _append_finding(
    findings: list[InteractionFinding],
    failure_class: FailureClass,
    detail: str,
) -> None:
    findings.append(InteractionFinding(failure_class, detail))


def _resolve_affected_panels(
    control: ControlScenario,
    panel_contract: PanelContract,
    findings: list[InteractionFinding],
) -> tuple[str, ...]:
    affected = control.assertions.affected_panels
    if isinstance(affected, tuple):
        return panel_contract.resolve_panel_ids(affected)
    if affected == "all_query_panels":
        return panel_contract.all_query_panels
    if affected == "query_dependency":
        mapped = panel_contract.by_control.get(control.key)
        if mapped is None:
            _append_finding(
                findings,
                FailureClass.FRAMEWORK_ERROR,
                f"control {control.key!r}: missing query_dependency panel mapping",
            )
            return ()
        return mapped
    _append_finding(
        findings,
        FailureClass.FRAMEWORK_ERROR,
        f"control {control.key!r}: unsupported affected_panels {affected!r}",
    )
    return ()


def _expected_params_for_control(
    control: ControlScenario,
    selected_value: str,
) -> tuple[dict[str, object], dict[str, str]]:
    value_params: dict[str, object] = {}
    identifier_params: dict[str, str] = {}
    param_names = control.assertions.selection or (control.key,)
    if control.adapter in _ESQL_VALUE_ADAPTERS:
        for name in param_names:
            value_params[name] = selected_value
    elif control.adapter in _ESQL_IDENTIFIER_ADAPTERS:
        for name in param_names:
            identifier_params[name] = selected_value
    return value_params, identifier_params


def _merge_assertions(
    controls: Sequence[ControlScenario],
    selections: Mapping[str, str],
    panel_contract: PanelContract,
    findings: list[InteractionFinding],
) -> _MergedAssertions:
    query_contains: list[str] = []
    query_not_contains: list[str] = []
    required_columns: list[str] = []
    expected_legend: list[str] = []
    expected_panels: list[str] = []
    unaffected_panels: list[str] = []
    expected_value_params: dict[str, object] = {}
    expected_identifier_params: dict[str, str] = {}
    stable_aliases: list[str] = []
    minimum_rows = 0
    expect_data_change = False
    allow_incompatible = True

    for control in controls:
        assertions = control.assertions
        # Query-bar text is translated into the request's filter DSL by Kibana;
        # it is not embedded in the ES|QL query string.
        if control.adapter != "query_bar":
            query_contains.extend(assertions.query_contains)
            query_not_contains.extend(assertions.query_not_contains)
        required_columns.extend(assertions.required_columns)
        expected_legend.extend(assertions.expected_legend)
        unaffected_panels.extend(
            panel_contract.resolve_panel_ids(assertions.unaffected_panels)
        )
        minimum_rows = max(minimum_rows, assertions.minimum_rows)
        expect_data_change = expect_data_change or assertions.expect_data_change
        allow_incompatible = allow_incompatible and assertions.allow_incompatible_selections
        if assertions.stable_alias:
            stable_aliases.append(assertions.stable_alias)
        expected_panels.extend(
            _resolve_affected_panels(control, panel_contract, findings)
        )
        selected_value = selections.get(control.key, "")
        if selected_value:
            value_params, identifier_params = _expected_params_for_control(
                control,
                selected_value,
            )
            expected_value_params.update(value_params)
            expected_identifier_params.update(identifier_params)

    stable_alias = ""
    non_empty_aliases = [alias for alias in stable_aliases if alias]
    if non_empty_aliases:
        unique_aliases = _ordered_unique(non_empty_aliases)
        if len(unique_aliases) > 1:
            _append_finding(
                findings,
                FailureClass.FRAMEWORK_ERROR,
                f"conflicting stable_alias values: {', '.join(unique_aliases)}",
            )
        else:
            stable_alias = unique_aliases[0]

    if len(controls) > 1:
        # A combination may affect disjoint panels with different queries,
        # params, and output columns. Individual option steps already ratchet
        # those panel-local contracts; the combination step proves all selected
        # controls coexist and every unioned affected panel refreshes cleanly.
        query_contains = []
        query_not_contains = []
        required_columns = []
        expected_value_params = {}
        expected_identifier_params = {}
        stable_alias = ""

    return _MergedAssertions(
        query_contains=_ordered_unique(query_contains),
        query_not_contains=_ordered_unique(query_not_contains),
        required_columns=_ordered_unique(required_columns),
        stable_alias=stable_alias,
        minimum_rows=minimum_rows,
        expected_legend=_ordered_unique(expected_legend),
        expect_data_change=expect_data_change,
        allow_incompatible_selections=allow_incompatible,
        expected_panels=_ordered_unique(expected_panels),
        unaffected_panels=_ordered_unique(unaffected_panels),
        expected_value_params=expected_value_params,
        expected_identifier_params=expected_identifier_params,
    )


def _controls_for_step(
    step: InteractionStep,
    controls_by_key: Mapping[str, ControlScenario],
) -> tuple[ControlScenario, ...]:
    if step.kind == "option":
        control = controls_by_key.get(step.control_key)
        return (control,) if control is not None else ()
    if step.kind == "combination":
        ordered: list[ControlScenario] = []
        for key in step.selections:
            control = controls_by_key.get(key)
            if control is not None:
                ordered.append(control)
        return tuple(ordered)
    return ()


def _step_status(
    findings: Sequence[InteractionFinding],
    capability: CapabilityCategory,
) -> InteractionStatus:
    if any(finding.failure_class in _HARD_FAILURE_CLASSES for finding in findings):
        return InteractionStatus.FAIL
    if any(finding.failure_class in _WARN_FAILURE_CLASSES for finding in findings):
        return InteractionStatus.WARN
    if capability in (CapabilityCategory.SOURCE_ONLY, CapabilityCategory.MIGRATION_GAP):
        return InteractionStatus.WARN
    return InteractionStatus.PASS


def _bound_snapshot_text(observation: BrowserObservation) -> str:
    parts = [
        "=== accessibility ===",
        observation.accessibility_snapshot,
        "=== visible_text ===",
        observation.visible_text,
        "=== panels ===",
    ]
    for panel in observation.panels:
        parts.append(f"[{panel.panel_id}] {panel.title}: {panel.status}")
        parts.append(panel.detail)
    text = "\n".join(parts)
    if len(text) <= _MAX_SNAPSHOT_TEXT:
        return text
    return text[:_MAX_SNAPSHOT_TEXT]


def _redact_url_userinfo_in_text(text: str) -> str:
    def _strip_userinfo(match: re.Match[str]) -> str:
        raw = match.group(0)
        scheme_sep = raw.find("://")
        if scheme_sep < 0:
            return raw
        scheme = raw[: scheme_sep + 3]
        remainder = raw[scheme_sep + 3 :]
        if "@" not in remainder:
            return raw
        _userinfo, _sep, hostpart = remainder.rpartition("@")
        return f"{scheme}{hostpart}"

    return _URL_USERINFO_RE.sub(_strip_userinfo, text)


def _redact_sensitive_line_assignments(text: str) -> str:
    redacted_lines: list[str] = []
    for line in text.splitlines():
        match = _SENSITIVE_LINE_ASSIGNMENT_RE.match(line)
        if match is None:
            redacted_lines.append(line)
            continue
        redacted_lines.append(f"{match.group(1)}[REDACTED]")
    result = "\n".join(redacted_lines)
    if text.endswith("\n"):
        return result + "\n"
    return result


def _redact_inline_sensitive_assignments(text: str) -> str:
    return _INLINE_SENSITIVE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}: [REDACTED]",
        text,
    )


def _redact_artifact_text(text: str) -> str:
    """Redact secrets from bounded text artifacts while preserving panel/query prose."""
    cleaned = _redact_url_userinfo_in_text(str(text or ""))
    cleaned = _redact_sensitive_line_assignments(cleaned)
    return _redact_inline_sensitive_assignments(cleaned)


def _sanitize_artifact_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_artifact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_artifact_strings(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_artifact_strings(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_artifact_strings(item) for item in value)
    return value


def _sanitize_artifact_payload(value: Any) -> Any:
    return _sanitize_artifact_strings(redact_evidence(value))


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(_redact_artifact_text(text), encoding="utf-8")
    tmp_path.replace(path)


def _network_payload(network: Sequence[NetworkEvidence]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for item in network:
        body = item.body
        if isinstance(body, Mapping):
            sanitized = dict(body)
            for key in ("values", "rows", "rawResponse"):
                sanitized.pop(key, None)
            if isinstance(sanitized.get("response"), Mapping):
                response = dict(sanitized["response"])
                response.pop("values", None)
                response.pop("rows", None)
                sanitized["response"] = response
            body = sanitized
        entry = item.to_dict()
        entry["body"] = body
        payload.append(entry)
    return payload


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    sanitized = _sanitize_artifact_payload(payload)
    tmp_path.write_text(
        json.dumps(sanitized, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _result_payload(
    result: InteractionResult,
    artifact_flags: Mapping[str, bool],
    cursor: CaptureCursor | None,
) -> dict[str, object]:
    return {
        **result.to_dict(),
        "artifact_flags": dict(artifact_flags),
        "cursor": {
            "network_index": cursor.network_index if cursor else 0,
            "console_index": cursor.console_index if cursor else 0,
        },
    }


def _selection_changes_from_baseline(
    selections: Mapping[str, str],
    discovered_by_key: Mapping[str, DiscoveredControl],
) -> bool:
    for key, value in selections.items():
        discovered = discovered_by_key.get(key)
        if discovered is None:
            continue
        if not discovered.selected:
            if value:
                return True
            continue
        if value not in discovered.selected:
            return True
    return False


def _clear_evidence_best_effort(browser: BrowserAdapter) -> InteractionFinding | None:
    try:
        browser.clear_evidence()
    except BrowserAdapterError as exc:
        return InteractionFinding(
            FailureClass.FRAMEWORK_ERROR,
            f"clear_evidence failed: {exc}",
        )
    return None


class InteractionRunner:
    """Orchestrate scenario discovery, execution, classification, and artifacts."""

    def __init__(
        self,
        browser: BrowserAdapter,
        scenario: DashboardScenario,
        panel_contract: PanelContract,
        config: RunConfig,
    ) -> None:
        self._browser = browser
        self._scenario = scenario
        self._panel_contract = panel_contract
        self._config = config
        self._artifact_root, self._run_root = validate_run_artifact_paths(
            config.artifact_root,
            scenario.id,
            config.run_id,
        )

    def run(self) -> InteractionReport:
        controls_by_key = {control.key: control for control in self._scenario.controls}
        self._browser.open_dashboard(self._config.dashboard_url)

        discovered_controls: list[DiscoveredControl] = []
        discovery_errors: dict[str, str] = {}
        for control in self._scenario.controls:
            try:
                discovered_controls.append(self._browser.discover(control))
            except ControlNotFound:
                continue
            except BrowserAdapterError as exc:
                discovery_errors[control.key] = str(exc)
            except Exception as exc:  # pragma: no cover - defensive boundary
                discovery_errors[control.key] = str(exc)

        plan = build_execution_plan(self._scenario, discovered_controls)
        discovered_by_key = {item.key: item for item in discovered_controls}
        results: list[InteractionResult] = []

        for step in plan:
            result = self._execute_step(
                step,
                controls_by_key=controls_by_key,
                discovered_by_key=discovered_by_key,
                discovery_errors=discovery_errors,
            )
            results.append(result)
            self._write_report(InteractionReport(scenario=self._scenario.id, results=results))

        report = InteractionReport(scenario=self._scenario.id, results=results)
        self._write_report(report)
        return report

    def _step_dir(self, step: InteractionStep) -> Path:
        safe_step_id = _validate_artifact_component("step_id", step.id)
        step_dir = _resolve_under_root(self._run_root, safe_step_id)
        if step_dir != self._run_root and self._run_root not in step_dir.parents:
            raise ValueError(f"step directory {step_dir} escapes run root {self._run_root}")
        return step_dir

    def _write_report(self, report: InteractionReport) -> None:
        self._run_root.mkdir(parents=True, exist_ok=True)
        counts = {"pass": 0, "warn": 0, "fail": 0, "skipped": 0, "total": len(report.results)}
        capabilities: dict[str, dict[str, int]] = {
            category.value: {
                "pass": 0,
                "warn": 0,
                "fail": 0,
                "skipped": 0,
                "total": 0,
            }
            for category in CapabilityCategory
        }
        for result in report.results:
            status_key = result.status.value
            if status_key in counts:
                counts[status_key] += 1
            bucket = capabilities[result.capability.value]
            bucket["total"] += 1
            if status_key in bucket:
                bucket[status_key] += 1

        panel_ids = set(self._panel_contract.all_query_panels)
        for panels in self._panel_contract.by_control.values():
            panel_ids.update(panels)

        payload: dict[str, object] = {
            **report.to_dict(),
            "scenario_id": self._scenario.id,
            "run_id": self._config.run_id,
            "counts": counts,
            "capabilities": capabilities,
            "verification_total": len(report.results),
            "panels_total": len(panel_ids),
            "exit_code": report.exit_code,
        }
        _write_json_atomic(self._run_root / "report.json", payload)

    def _execute_step(
        self,
        step: InteractionStep,
        *,
        controls_by_key: Mapping[str, ControlScenario],
        discovered_by_key: Mapping[str, DiscoveredControl],
        discovery_errors: Mapping[str, str],
    ) -> InteractionResult:
        capability = _step_capability(step, controls_by_key)
        findings: list[InteractionFinding] = []
        artifact_flags: dict[str, bool] = {
            "before_screenshot": False,
            "after_screenshot": False,
        }

        if step.kind == "coverage_gap":
            control = controls_by_key.get(step.control_key)
            detail = control.expected_gap if control is not None else step.label
            if not detail:
                detail = f"coverage gap for control {step.control_key!r}"
            _append_finding(findings, FailureClass.COVERAGE_GAP, detail)
            return self._finalize_step(
                step,
                findings=findings,
                capability=capability,
                artifact_flags=artifact_flags,
            )

        if step.kind == "missing_control":
            detail = f"control {step.control_key!r} not found"
            discovery_error = discovery_errors.get(step.control_key)
            if discovery_error:
                _append_finding(
                    findings,
                    FailureClass.FRAMEWORK_ERROR,
                    discovery_error,
                )
            _append_finding(findings, FailureClass.CONTROL_NOT_FOUND, detail)
            return self._finalize_step(
                step,
                findings=findings,
                capability=capability,
                artifact_flags=artifact_flags,
            )

        if step.kind == "missing_option":
            missing = ", ".join(step.missing_declared_options) or "unknown"
            _append_finding(
                findings,
                FailureClass.OPTION_NOT_FOUND,
                f"control {step.control_key!r}: missing declared options: {missing}",
            )
            return self._finalize_step(
                step,
                findings=findings,
                capability=capability,
                artifact_flags=artifact_flags,
            )

        controls = _controls_for_step(step, controls_by_key)
        premerge_findings: list[InteractionFinding] = []
        merged = _merge_assertions(
            controls,
            step.selections,
            self._panel_contract,
            premerge_findings,
        )
        findings.extend(premerge_findings)

        if step.reset_before:
            self._browser.reset(self._config.dashboard_url)

        selection_changes = _selection_changes_from_baseline(
            step.selections,
            discovered_by_key,
        )
        network_merged = merged
        if step.kind in {"option", "combination"} and not selection_changes:
            network_merged = replace(
                merged,
                query_contains=(),
                query_not_contains=(),
                required_columns=(),
                stable_alias="",
                minimum_rows=0,
                expected_panels=(),
                expected_value_params={},
                expected_identifier_params={},
            )
        expected_panels = merged.expected_panels
        settle_expected_panels = expected_panels if selection_changes else ()

        baseline_observation = self._browser.capture(expected_panels)
        baseline_fingerprint = _panel_fingerprint(baseline_observation.panels)

        step_dir = self._step_dir(step)
        before_path = step_dir / "before.png"
        artifact_flags["before_screenshot"] = self._browser.screenshot(before_path)
        if not artifact_flags["before_screenshot"]:
            _append_finding(
                findings,
                FailureClass.UNEXPECTED_EMPTY,
                "before.png screenshot missing or empty",
            )

        cursor: CaptureCursor | None = None
        selection_records: list[dict[str, object]] = []
        settle_timed_out = False
        observation = baseline_observation

        if step.kind in {"option", "combination"}:
            cursor = self._browser.begin_step()
            for control in controls:
                selected_value = step.selections.get(control.key, "")
                if not selected_value:
                    continue
                try:
                    self._browser.select(control, selected_value)
                except ControlNotFound as exc:
                    _append_finding(
                        findings,
                        FailureClass.CONTROL_NOT_FOUND,
                        str(exc),
                    )
                    break
                except OptionNotFound as exc:
                    _append_finding(
                        findings,
                        FailureClass.OPTION_NOT_FOUND,
                        str(exc),
                    )
                    break
                except SelectionDidNotStick as exc:
                    _append_finding(
                        findings,
                        FailureClass.SELECTION_DID_NOT_STICK,
                        str(exc),
                    )
                    break
                except BrowserAdapterError as exc:
                    _append_finding(
                        findings,
                        FailureClass.FRAMEWORK_ERROR,
                        str(exc),
                    )
                    break
                except Exception as exc:  # pragma: no cover - defensive boundary
                    _append_finding(
                        findings,
                        FailureClass.FRAMEWORK_ERROR,
                        str(exc),
                    )
                    break

                try:
                    state = self._browser.read_state(control)
                except BrowserAdapterError as exc:
                    _append_finding(
                        findings,
                        FailureClass.FRAMEWORK_ERROR,
                        f"read_state failed for {control.key!r}: {exc}",
                    )
                    state = ControlState()
                except Exception as exc:  # pragma: no cover - defensive boundary
                    _append_finding(
                        findings,
                        FailureClass.FRAMEWORK_ERROR,
                        f"read_state failed for {control.key!r}: {exc}",
                    )
                    state = ControlState()

                selection_records.append(
                    {
                        "control_key": control.key,
                        "selected_value": selected_value,
                        "selected_count": state.selected_count,
                        "incompatible_warning": state.incompatible_warning,
                    }
                )
                if state.incompatible_warning and not merged.allow_incompatible_selections:
                    _append_finding(
                        findings,
                        FailureClass.INTERACTION_REGRESSION,
                        (
                            f"control {control.key!r}: incompatible selection warning "
                            f"{state.incompatible_warning!r}"
                        ),
                    )

            hard_before_settle = any(
                finding.failure_class in _HARD_FAILURE_CLASSES for finding in findings
            )
            if cursor is not None and not hard_before_settle:
                try:
                    observation = self._browser.settle(
                        cursor,
                        settle_expected_panels,
                        policy=self._config.settle_policy,
                    )
                except SettleTimeout as exc:
                    settle_timed_out = True
                    observation = exc.observation
                    _append_finding(
                        findings,
                        FailureClass.SETTLE_TIMEOUT,
                        exc.reason,
                    )
                except BrowserAdapterError as exc:
                    _append_finding(
                        findings,
                        FailureClass.FRAMEWORK_ERROR,
                        str(exc),
                    )
                except Exception as exc:  # pragma: no cover - defensive boundary
                    _append_finding(
                        findings,
                        FailureClass.FRAMEWORK_ERROR,
                        str(exc),
                    )

        after_path = step_dir / "after.png"
        artifact_flags["after_screenshot"] = self._browser.screenshot(after_path)
        if not artifact_flags["after_screenshot"]:
            _append_finding(
                findings,
                FailureClass.UNEXPECTED_EMPTY,
                "after.png screenshot missing or empty",
            )

        if cursor is not None:
            scoped_observation = self._browser.capture(expected_panels, cursor=cursor)
            observation = scoped_observation if not settle_timed_out else observation
            network_evidence = list(observation.network)
            console_errors = list(observation.console_errors)
        else:
            network_evidence = list(observation.network)
            console_errors = list(observation.console_errors)

        findings.extend(
            self._network_findings(
                merged=network_merged,
                network_evidence=network_evidence,
                noise_allowances=self._scenario.noise_allowances,
            )
        )

        if not settle_timed_out:
            findings.extend(
                self._render_findings(
                    panels=list(observation.panels),
                    minimum_rows=merged.minimum_rows,
                )
            )

        for message in _ordered_unique(console_errors):
            _append_finding(
                findings,
                FailureClass.CONSOLE_ERROR,
                message,
            )

        findings.extend(
            self._legend_findings(
                expected_legend=merged.expected_legend,
                panels=list(observation.panels),
            )
        )

        if not settle_timed_out and merged.expect_data_change and selection_changes:
            after_fingerprint = _panel_fingerprint(observation.panels)
            if after_fingerprint == baseline_fingerprint:
                _append_finding(
                    findings,
                    FailureClass.INTERACTION_REGRESSION,
                    "expected panel fingerprints to change after selection",
                )

        findings = _dedupe_findings(findings)
        preliminary = InteractionResult(
            name=step.id,
            status=_step_status(findings, capability),
            capability=capability,
            findings=findings,
            network=network_evidence,
            panels=list(observation.panels),
        )
        artifact_error = self._write_step_artifacts(
            step,
            result=preliminary,
            observation=observation,
            selection_records=selection_records,
            artifact_flags=artifact_flags,
            cursor=cursor,
        )
        if artifact_error is not None:
            findings.append(artifact_error)
            findings = _dedupe_findings(findings)
            preliminary = InteractionResult(
                name=step.id,
                status=_step_status(findings, capability),
                capability=capability,
                findings=findings,
                network=network_evidence,
                panels=list(observation.panels),
            )
        return self._finalize_written_step(
            step,
            preliminary=preliminary,
            findings=findings,
            capability=capability,
            artifact_flags=artifact_flags,
            cursor=cursor,
            artifact_write_failed=artifact_error is not None,
        )

    def _finalize_step(
        self,
        step: InteractionStep,
        *,
        findings: list[InteractionFinding],
        capability: CapabilityCategory,
        artifact_flags: Mapping[str, bool],
    ) -> InteractionResult:
        preliminary = InteractionResult(
            name=step.id,
            status=_step_status(findings, capability),
            capability=capability,
            findings=_dedupe_findings(findings),
        )
        artifact_error = self._write_step_artifacts(
            step,
            result=preliminary,
            observation=None,
            selection_records=[],
            artifact_flags=artifact_flags,
        )
        if artifact_error is not None:
            findings.append(artifact_error)
            preliminary = InteractionResult(
                name=step.id,
                status=_step_status(findings, capability),
                capability=capability,
                findings=_dedupe_findings(findings),
            )
        return self._finalize_written_step(
            step,
            preliminary=preliminary,
            findings=list(preliminary.findings),
            capability=capability,
            artifact_flags=artifact_flags,
            cursor=None,
            artifact_write_failed=artifact_error is not None,
        )

    def _finalize_written_step(
        self,
        step: InteractionStep,
        *,
        preliminary: InteractionResult,
        findings: list[InteractionFinding],
        capability: CapabilityCategory,
        artifact_flags: Mapping[str, bool],
        cursor: CaptureCursor | None,
        artifact_write_failed: bool = False,
    ) -> InteractionResult:
        clear_finding = _clear_evidence_best_effort(self._browser)
        if clear_finding is not None:
            findings.append(clear_finding)
        findings = _dedupe_findings(findings)
        final_status = _step_status(findings, capability)
        if (
            not artifact_write_failed
            and findings == preliminary.findings
            and preliminary.status == final_status
        ):
            return preliminary
        final = InteractionResult(
            name=preliminary.name,
            status=final_status,
            capability=capability,
            findings=findings,
            network=preliminary.network,
            panels=preliminary.panels,
        )
        if not artifact_write_failed:
            try:
                _write_json_atomic(
                    self._step_dir(step) / "result.json",
                    _result_payload(final, artifact_flags, cursor),
                )
            except OSError as exc:
                findings.append(
                    InteractionFinding(
                        FailureClass.FRAMEWORK_ERROR,
                        f"artifact write failed for result.json: {_bound_io_error(exc)}",
                    )
                )
                findings = _dedupe_findings(findings)
                final = InteractionResult(
                    name=preliminary.name,
                    status=_step_status(findings, capability),
                    capability=capability,
                    findings=findings,
                    network=preliminary.network,
                    panels=preliminary.panels,
                )
        return final

    def _network_findings(
        self,
        *,
        merged: _MergedAssertions,
        network_evidence: Sequence[NetworkEvidence],
        noise_allowances: Sequence[object],
    ) -> list[InteractionFinding]:
        findings: list[InteractionFinding] = []
        esql_evidence = [item for item in network_evidence if _is_esql_endpoint(item.endpoint)]
        non_esql_evidence = [
            item for item in network_evidence if not _is_esql_endpoint(item.endpoint)
        ]

        for item in non_esql_evidence:
            if item.status < 400:
                continue
            rationale = match_noise_allowance(
                item.endpoint,
                item.method,
                item.status,
                noise_allowances,
            )
            if rationale:
                findings.append(
                    InteractionFinding(
                        FailureClass.UNEXPECTED_EMPTY,
                        (
                            f"allowed network noise {item.method} {item.endpoint} "
                            f"{item.status}: {rationale}"
                        ),
                    )
                )
                continue
            if 500 <= item.status <= 599:
                findings.append(
                    InteractionFinding(
                        FailureClass.SERVER_ERROR,
                        f"{item.method} {item.endpoint}: server error status {item.status}",
                    )
                )
            else:
                findings.append(
                    InteractionFinding(
                        FailureClass.QUERY_CONTRACT_ERROR,
                        f"{item.method} {item.endpoint}: non-success status {item.status}",
                    )
                )

        findings.extend(
            check_network_contract(
                expected_panel_ids=merged.expected_panels,
                unaffected_panel_ids=merged.unaffected_panels,
                evidence=esql_evidence,
                query_contains=merged.query_contains,
                query_not_contains=merged.query_not_contains,
                expected_value_params=merged.expected_value_params,
                expected_identifier_params=merged.expected_identifier_params,
                required_columns=merged.required_columns,
                stable_alias=merged.stable_alias,
                minimum_rows=merged.minimum_rows,
            )
        )
        return findings

    def _render_findings(
        self,
        *,
        panels: Sequence[PanelEvidence],
        minimum_rows: int,
    ) -> list[InteractionFinding]:
        findings: list[InteractionFinding] = []
        for panel in panels:
            if panel.status == "missing":
                findings.append(
                    InteractionFinding(
                        FailureClass.FRAMEWORK_ERROR,
                        f"panel {panel.panel_id}: snapshot missing after settle",
                    )
                )
                continue
            if panel.status == "loading":
                findings.append(
                    InteractionFinding(
                        FailureClass.RENDER_ERROR,
                        f"panel {panel.panel_id}: still loading after settle",
                    )
                )
                continue
            render = classify_panel(
                panel.title,
                panel.detail,
                expects_data=minimum_rows > 0,
            )
            if render.error_class == "render_error":
                findings.append(
                    InteractionFinding(
                        FailureClass.RENDER_ERROR,
                        f"panel {panel.panel_id}: {render.detail}",
                    )
                )
            elif render.error_class == "field_gap":
                findings.append(
                    InteractionFinding(
                        FailureClass.FIELD_GAP,
                        f"panel {panel.panel_id}: {render.detail}",
                    )
                )
            elif render.error_class == "data_gap":
                findings.append(
                    InteractionFinding(
                        FailureClass.DATA_GAP,
                        f"panel {panel.panel_id}: {render.detail}",
                    )
                )
            elif render.error_class == "unexpected_empty":
                findings.append(
                    InteractionFinding(
                        FailureClass.UNEXPECTED_EMPTY,
                        f"panel {panel.panel_id}: {render.detail}",
                    )
                )
        return findings

    def _legend_findings(
        self,
        *,
        expected_legend: Sequence[str],
        panels: Sequence[PanelEvidence],
    ) -> list[InteractionFinding]:
        if not expected_legend:
            return []
        findings: list[InteractionFinding] = []
        combined_detail = "\n".join(panel.detail for panel in panels)
        for fragment in expected_legend:
            if fragment not in combined_detail:
                findings.append(
                    InteractionFinding(
                        FailureClass.INTERACTION_REGRESSION,
                        f"expected legend fragment {fragment!r} not found in panel detail",
                    )
                )
        return findings

    def _write_step_artifacts(
        self,
        step: InteractionStep,
        *,
        result: InteractionResult,
        observation: BrowserObservation | None,
        selection_records: Sequence[Mapping[str, object]],
        artifact_flags: Mapping[str, bool],
        cursor: CaptureCursor | None = None,
    ) -> InteractionFinding | None:
        try:
            step_dir = self._step_dir(step)
            step_dir.mkdir(parents=True, exist_ok=True)

            if observation is not None:
                _write_json_atomic(
                    step_dir / "network.json",
                    {"requests": _network_payload(result.network)},
                )
                _write_json_atomic(
                    step_dir / "console.json",
                    {"errors": list(observation.console_errors)},
                )
                _write_json_atomic(
                    step_dir / "pending-requests.json",
                    {
                        "pending": [
                            {
                                "panel_id": item.panel_id,
                                "endpoint": item.endpoint,
                                "opaque_id": item.opaque_id,
                                "age_ms": item.age_ms,
                            }
                            for item in observation.pending_requests
                        ]
                    },
                )
                _write_text_atomic(step_dir / "snapshot.txt", _bound_snapshot_text(observation))
            else:
                _write_json_atomic(step_dir / "network.json", {"requests": []})
                _write_json_atomic(step_dir / "console.json", {"errors": []})
                _write_json_atomic(step_dir / "pending-requests.json", {"pending": []})
                _write_text_atomic(step_dir / "snapshot.txt", "")

            _write_json_atomic(
                step_dir / "selection.json",
                {"selections": list(selection_records)},
            )
            _write_json_atomic(
                step_dir / "result.json",
                _result_payload(result, artifact_flags, cursor),
            )
        except OSError as exc:
            return InteractionFinding(
                FailureClass.FRAMEWORK_ERROR,
                f"artifact write failed for step {step.id!r}: {_bound_io_error(exc)}",
            )
        return None


def load_panel_contract(path: str | Path) -> PanelContract:
    """Load a runtime panel contract JSON file."""
    contract_path = Path(path)
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{contract_path}: unreadable panel contract: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{contract_path}: invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"{contract_path}: panel contract root must be a mapping")

    all_query_raw = payload.get("all_query_panels", [])
    by_control_raw = payload.get("by_control", {})
    panel_aliases_raw = payload.get("panel_aliases", {})
    if not isinstance(all_query_raw, list) or not all(
        isinstance(item, str) for item in all_query_raw
    ):
        raise ValueError(f"{contract_path}: all_query_panels must be a list of strings")
    if not isinstance(by_control_raw, dict):
        raise ValueError(f"{contract_path}: by_control must be a mapping")
    if not isinstance(panel_aliases_raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in panel_aliases_raw.items()
    ):
        raise ValueError(f"{contract_path}: panel_aliases must map strings to strings")

    by_control: dict[str, tuple[str, ...]] = {}
    for key, value in by_control_raw.items():
        if not isinstance(key, str) or not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError(
                f"{contract_path}: by_control values must be lists of strings"
            )
        by_control[key] = tuple(value)

    return PanelContract(
        all_query_panels=tuple(all_query_raw),
        by_control=by_control,
        panel_aliases=dict(panel_aliases_raw),
    )
