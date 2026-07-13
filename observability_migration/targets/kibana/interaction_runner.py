# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Execute dashboard interaction scenarios and write redacted step artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class PanelContract:
    all_query_panels: tuple[str, ...] = ()
    by_control: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


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
        return affected
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
        query_contains.extend(assertions.query_contains)
        query_not_contains.extend(assertions.query_not_contains)
        required_columns.extend(assertions.required_columns)
        expected_legend.extend(assertions.expected_legend)
        unaffected_panels.extend(assertions.unaffected_panels)
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
    tmp_path.write_text(
        json.dumps(redact_evidence(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


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


def _clear_evidence_best_effort(browser: Any) -> InteractionFinding | None:
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
        browser: Any,
        scenario: DashboardScenario,
        panel_contract: PanelContract,
        config: RunConfig,
    ) -> None:
        self._browser = browser
        self._scenario = scenario
        self._panel_contract = panel_contract
        self._config = config

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

        report = InteractionReport(scenario=self._scenario.id, results=results)
        self._write_report(report)
        return report

    def _step_dir(self, step: InteractionStep) -> Path:
        return (
            self._config.artifact_root
            / self._scenario.id
            / self._config.run_id
            / step.id
        )

    def _write_report(self, report: InteractionReport) -> None:
        run_root = self._config.artifact_root / self._scenario.id / self._config.run_id
        run_root.mkdir(parents=True, exist_ok=True)
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
        _write_json_atomic(run_root / "report.json", payload)

    def _execute_step(
        self,
        step: InteractionStep,
        *,
        controls_by_key: Mapping[str, ControlScenario],
        discovered_by_key: Mapping[str, DiscoveredControl],
        discovery_errors: Mapping[str, str],
    ) -> InteractionResult:
        capability = step.capability
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
        expected_panels = merged.expected_panels

        if step.reset_before:
            self._browser.reset(self._config.dashboard_url)

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
                        expected_panels,
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
                merged=merged,
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

        if merged.expect_data_change and _selection_changes_from_baseline(
            step.selections,
            discovered_by_key,
        ):
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
        self._write_step_artifacts(
            step,
            result=preliminary,
            observation=observation,
            selection_records=selection_records,
            artifact_flags=artifact_flags,
            cursor=cursor,
        )
        return self._finalize_written_step(
            step,
            preliminary=preliminary,
            findings=findings,
            capability=capability,
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
        self._write_step_artifacts(
            step,
            result=preliminary,
            observation=None,
            selection_records=[],
            artifact_flags=artifact_flags,
        )
        return self._finalize_written_step(
            step,
            preliminary=preliminary,
            findings=list(preliminary.findings),
            capability=capability,
        )

    def _finalize_written_step(
        self,
        step: InteractionStep,
        *,
        preliminary: InteractionResult,
        findings: list[InteractionFinding],
        capability: CapabilityCategory,
    ) -> InteractionResult:
        clear_finding = _clear_evidence_best_effort(self._browser)
        if clear_finding is not None:
            findings.append(clear_finding)
        findings = _dedupe_findings(findings)
        if findings == preliminary.findings and preliminary.status == _step_status(
            findings,
            capability,
        ):
            return preliminary
        final = InteractionResult(
            name=preliminary.name,
            status=_step_status(findings, capability),
            capability=capability,
            findings=findings,
            network=preliminary.network,
            panels=preliminary.panels,
        )
        _write_json_atomic(self._step_dir(step) / "result.json", final.to_dict())
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
    ) -> None:
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
            snapshot_path = step_dir / "snapshot.txt"
            snapshot_path.write_text(_bound_snapshot_text(observation), encoding="utf-8")
        else:
            _write_json_atomic(step_dir / "network.json", {"requests": []})
            _write_json_atomic(step_dir / "console.json", {"errors": []})
            _write_json_atomic(step_dir / "pending-requests.json", {"pending": []})
            (step_dir / "snapshot.txt").write_text("", encoding="utf-8")

        _write_json_atomic(
            step_dir / "selection.json",
            {"selections": list(selection_records)},
        )
        _write_json_atomic(
            step_dir / "result.json",
            {
                **result.to_dict(),
                "artifact_flags": dict(artifact_flags),
                "cursor": {
                    "network_index": cursor.network_index if cursor else 0,
                    "console_index": cursor.console_index if cursor else 0,
                },
            },
        )


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
    if not isinstance(all_query_raw, list) or not all(
        isinstance(item, str) for item in all_query_raw
    ):
        raise ValueError(f"{contract_path}: all_query_panels must be a list of strings")
    if not isinstance(by_control_raw, dict):
        raise ValueError(f"{contract_path}: by_control must be a mapping")

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
    )
