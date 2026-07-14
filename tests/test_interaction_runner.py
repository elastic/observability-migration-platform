# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for dashboard interaction scenario execution and artifact writing."""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from observability_migration.targets.kibana.interaction_audit import (
    CapabilityCategory,
    FailureClass,
    InteractionStatus,
    NetworkEvidence,
    PanelEvidence,
)
from observability_migration.targets.kibana.interaction_driver import (
    BrowserAdapterError,
    BrowserObservation,
    CaptureCursor,
    ControlNotFound,
    ControlState,
    OptionNotFound,
    SelectionDidNotStick,
    SettleTimeout,
)
from observability_migration.targets.kibana.interaction_runner import (
    InteractionRunner,
    PanelContract,
    RunConfig,
    _expected_params_for_control,
    load_panel_contract,
    validate_run_artifact_paths,
)
from observability_migration.targets.kibana.interaction_scenarios import (
    Assertions,
    CombinationScenario,
    ControlScenario,
    DashboardScenario,
    DiscoveredControl,
    NoiseAllowance,
    OptionPolicy,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "interaction_audit"
MINIMAL = FIXTURES / "minimal.yaml"

_SENSITIVE_PATTERNS = (
    re.compile(r"https?://[^/\s]+:[^@\s]+@", re.IGNORECASE),
    re.compile(r"ApiKey\s+secret", re.IGNORECASE),
    re.compile(r"sid=secret", re.IGNORECASE),
    re.compile(r'"api_key"\s*:\s*"secret"', re.IGNORECASE),
    re.compile(r'"Authorization"\s*:\s*"ApiKey', re.IGNORECASE),
    re.compile(r'"cookie"\s*:\s*"sid=', re.IGNORECASE),
    re.compile(r'"x-elastic-api-key"\s*:\s*"abc"', re.IGNORECASE),
    re.compile(r"Authorization:\s*ApiKey\s+secret", re.IGNORECASE),
    re.compile(r"Cookie:\s*sid=secret", re.IGNORECASE),
    re.compile(r"Set-Cookie:\s*session=abc", re.IGNORECASE),
    re.compile(r"X-Elastic-Api-Key:\s*abc", re.IGNORECASE),
    re.compile(r"api_key=leaked", re.IGNORECASE),
)


@dataclass
class FakeBrowser:
    """High-level browser fake implementing the interaction driver contract."""

    controls: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    selected: dict[str, tuple[str, ...]] = field(default_factory=dict)
    panel_details: dict[str, str] = field(default_factory=dict)
    accessibility_snapshot: str = ""
    visible_text: str = ""
    network_by_step: list[tuple[NetworkEvidence, ...]] = field(default_factory=list)
    console_by_step: list[tuple[str, ...]] = field(default_factory=list)
    baseline_network_by_step: list[tuple[NetworkEvidence, ...]] | None = None
    baseline_console_by_step: list[tuple[str, ...]] | None = None
    pending_after_step: bool = False
    screenshot_ok: bool = True
    settle_timeout: bool = False
    baseline_settle_timeout: bool = False
    select_errors: dict[tuple[str, str], Exception] = field(default_factory=dict)
    discover_errors: dict[str, Exception] = field(default_factory=dict)
    clear_errors: list[Exception] = field(default_factory=list)
    incompatible_warnings: dict[str, str] = field(default_factory=dict)
    selected_after_reset: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    reset_count: int = 0
    begin_count: int = 0
    settle_count: int = 0
    clear_count: int = 0
    select_calls: list[tuple[str, str]] = field(default_factory=list)
    settle_expected_panels: list[tuple[str, ...]] = field(default_factory=list)
    opened_url: str = ""
    _step_index: int = 0
    _phase: str = "idle"

    def open_dashboard(self, url: str) -> None:
        self.opened_url = url

    def reset(self, url: str) -> None:
        self.reset_count += 1
        self.opened_url = url
        self.selected.update(self.selected_after_reset)
        self._phase = "baseline"

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        error = self.discover_errors.get(control.key)
        if error is not None:
            raise error
        if control.key not in self.controls:
            raise ControlNotFound(f"control {control.key!r} not found")
        options = self.controls[control.key]
        selected = self.selected.get(control.key, ())
        if not selected and options:
            selected = (options[0],)
            self.selected[control.key] = selected
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=options,
            selected=selected,
        )

    def select(self, control: ControlScenario, option: str) -> None:
        self.select_calls.append((control.key, option))
        error = self.select_errors.get((control.key, option))
        if error is not None:
            raise error
        self.selected[control.key] = (option,)

    def read_state(self, control: ControlScenario) -> ControlState:
        selected = self.selected.get(control.key, ())
        return ControlState(
            selected_count=len(selected),
            incompatible_warning=self.incompatible_warnings.get(control.key, ""),
        )

    def read_selected(self, control: ControlScenario) -> tuple[str, ...]:
        return self.selected.get(control.key, ())

    def capture(
        self,
        expected_panels: Sequence[str],
        cursor: CaptureCursor | None = None,
    ) -> BrowserObservation:
        del cursor
        panels = tuple(
            PanelEvidence(
                panel_id=panel_id,
                title=panel_id,
                status="stable",
                detail=self.panel_details.get(panel_id, f"{panel_id} rendered"),
            )
            for panel_id in expected_panels
        )
        network: tuple[NetworkEvidence, ...] = ()
        console: tuple[str, ...] = ()
        networks = self.network_by_step
        consoles = self.console_by_step
        if self._phase == "baseline":
            if self.baseline_network_by_step is not None:
                networks = self.baseline_network_by_step
            if self.baseline_console_by_step is not None:
                consoles = self.baseline_console_by_step
        if self._step_index < len(networks):
            network = networks[self._step_index]
        if self._step_index < len(consoles):
            console = consoles[self._step_index]
        return BrowserObservation(
            url=self.opened_url,
            accessibility_snapshot=self.accessibility_snapshot,
            visible_text=self.visible_text,
            network=network,
            panels=panels,
            console_errors=console,
        )

    def begin_step(self) -> CaptureCursor:
        self.begin_count += 1
        return CaptureCursor(self.begin_count, self.begin_count)

    def settle(
        self,
        cursor: CaptureCursor,
        expected_panels: Sequence[str],
        *,
        policy: Any = None,
    ) -> BrowserObservation:
        del cursor, policy
        self.settle_count += 1
        self.settle_expected_panels.append(tuple(expected_panels))
        if self._phase == "baseline" and self.baseline_settle_timeout:
            observation = self.capture(expected_panels)
            raise SettleTimeout(observation, "reset baseline did not settle")
        if self.settle_timeout:
            observation = self.capture(expected_panels)
            raise SettleTimeout(observation, "dashboard evidence did not settle")
        observation = self.capture(expected_panels)
        return observation

    def screenshot(self, path: str | Path) -> bool:
        target = Path(path)
        if not self.screenshot_ok:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n")
        return True

    def clear_evidence(self) -> None:
        self.clear_count += 1
        if self.clear_errors:
            raise self.clear_errors.pop(0)
        if self.pending_after_step:
            raise BrowserAdapterError("cannot clear evidence while requests are pending")
        if self._phase == "baseline":
            self._phase = "action"
            return
        self._step_index += 1
        self._phase = "idle"

    def close(self) -> None:
        return


def _esql_network(
    panel_id: str,
    *,
    query: str = "FROM metrics-* | WHERE service.environment == ?namespace",
    params: dict[str, object] | None = None,
    status: int = 200,
    columns: tuple[str, ...] = ("value",),
    row_count: int = 10,
) -> NetworkEvidence:
    resolved_params = params or {"namespace": "ns_1"}
    return NetworkEvidence(
        endpoint="/internal/search/esql",
        method="POST",
        status=status,
        url=f"http://localhost:5601/internal/search/esql?panel={panel_id}",
        query=query,
        panel_id=panel_id,
        params=resolved_params,
        param_kinds={str(key): "value" for key in resolved_params},
        response_columns=columns,
        row_count=row_count,
    )


def _namespace_control(**assertion_overrides: object) -> ControlScenario:
    base = Assertions(
        selection=("namespace",),
        affected_panels="query_dependency",
        query_contains=("?namespace",),
        minimum_rows=1,
    )
    if assertion_overrides:
        base = Assertions(**{**base.__dict__, **assertion_overrides})
    return ControlScenario(
        label="namespace",
        key="namespace",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="every"),
        assertions=base,
    )


def _instance_control() -> ControlScenario:
    return ControlScenario(
        label="instance",
        key="instance",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("i_1",)),
        assertions=Assertions(affected_panels="all_query_panels"),
    )


def _scenario(
    controls: tuple[ControlScenario, ...] | None = None,
    combinations: tuple[CombinationScenario, ...] = (),
    noise_allowances: tuple[NoiseAllowance, ...] = (),
) -> DashboardScenario:
    return DashboardScenario(
        version=1,
        id="test-scenario",
        title="Test",
        source_kind="grafana",
        source_path="path.json",
        control_schema_path="",
        dashboard_title="Test Dashboard",
        time_from="now-3h",
        time_to="now",
        controls=controls or (_namespace_control(),),
        combinations=combinations,
        noise_allowances=noise_allowances,
    )


def _panel_contract() -> PanelContract:
    return PanelContract(
        all_query_panels=("panel-a", "panel-b"),
        by_control={"namespace": ("panel-a",), "instance": ("panel-a", "panel-b")},
    )


def _run(
    browser: FakeBrowser,
    scenario: DashboardScenario,
    tmp_path: Path,
    panel_contract: PanelContract | None = None,
) -> Any:
    return InteractionRunner(
        browser,
        scenario,
        panel_contract or _panel_contract(),
        RunConfig(
            dashboard_url="http://localhost:5601/app/dashboards#/view/test",
            artifact_root=tmp_path,
            run_id="run-1",
        ),
    ).run()


def _step_dir(tmp_path: Path, step_id: str) -> Path:
    return tmp_path / "test-scenario" / "run-1" / step_id


def _scan_artifacts(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def _assert_no_temp_artifacts(root: Path) -> None:
    temp_files = [path for path in root.rglob("*") if path.name.startswith(".") and path.name.endswith(".tmp")]
    assert temp_files == []


def _foreign_browser_adapter_error() -> type[Exception]:
    """Return BrowserAdapterError from the live driver module (reload-safe)."""
    driver = importlib.import_module(
        "observability_migration.targets.kibana.interaction_driver",
    )
    return driver.BrowserAdapterError


def _load_cli_module(monkeypatch: pytest.MonkeyPatch, stub: Any) -> Any:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_interaction_audit.py"
    spec = importlib.util.spec_from_file_location("run_interaction_audit", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PlaywrightKibanaBrowser", lambda: stub)
    return module


def _assert_no_sensitive_text(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="ignore")
    for pattern in _SENSITIVE_PATTERNS:
        assert not pattern.search(text), f"sensitive pattern in {path}: {pattern.pattern}"


def test_happy_option_pass_calls_browser_contract_once(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        selected={"namespace": ("ns_2",)},
        panel_details={"panel-a": "namespace ns_1 legend"},
        network_by_step=[(_esql_network("panel-a", params={"namespace": "ns_1"}),)],
    )
    single_option = _scenario(
        controls=(
            ControlScenario(
                label="namespace",
                key="namespace",
                adapter="esql_value",
                capability=CapabilityCategory.MIGRATED_LIVE,
                options=OptionPolicy(strategy="declared", include=("ns_1",)),
                assertions=Assertions(
                    selection=("namespace",),
                    affected_panels="query_dependency",
                    query_contains=("?namespace",),
                    minimum_rows=1,
                    expect_data_change=False,
                ),
            ),
        )
    )
    report = _run(browser, single_option, tmp_path)

    assert browser.reset_count == 1
    assert browser.begin_count == 2
    assert browser.settle_count == 2
    assert browser.select_calls == [("namespace", "ns_1")]
    assert browser.settle_expected_panels == [(), ("panel-a",)]
    assert report.status == "pass"
    assert report.results[0].status is InteractionStatus.PASS


def test_every_option_resets_independently_and_preserves_plan_order(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        selected={"namespace": ("ns_1",)},
        network_by_step=[
            (_esql_network("panel-a", params={"namespace": "ns_1"}),),
            (_esql_network("panel-a", params={"namespace": "ns_2"}),),
        ],
    )
    report = _run(browser, _scenario(), tmp_path)

    assert browser.reset_count == 2
    assert browser.select_calls == [("namespace", "ns_2")]
    assert [result.name for result in report.results] == ["namespace=ns_1", "namespace=ns_2"]


def test_combination_selects_in_manifest_order_once(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",), "instance": ("i_1", "i_2")},
        selected={"namespace": ("ns_1",), "instance": ("i_1",)},
        network_by_step=[(_esql_network("panel-a"), _esql_network("panel-b"))],
    )
    scenario = _scenario(
        controls=(_namespace_control(), _instance_control()),
        combinations=(
            CombinationScenario(
                id="combo",
                selections=MappingProxyType({"namespace": "ns_1", "instance": "i_2"}),
            ),
        ),
    )
    _run(browser, scenario, tmp_path)
    assert browser.select_calls == [("instance", "i_2")]


def test_affected_panel_resolution_modes(tmp_path: Path) -> None:
    tuple_control = ControlScenario(
        label="fixed",
        key="fixed",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("x",)),
        assertions=Assertions(affected_panels=("panel-z",)),
    )
    browser = FakeBrowser(
        controls={"fixed": ("x",), "instance": ("i_1",), "namespace": ("ns_1",), "missing": ("x",)},
        selected={
            "fixed": ("baseline",),
            "instance": ("baseline",),
            "namespace": ("baseline",),
            "missing": ("baseline",),
        },
        network_by_step=[
            (_esql_network("panel-z"),),
            (_esql_network("panel-a"), _esql_network("panel-b")),
            (_esql_network("panel-a"),),
            (_esql_network("panel-a"),),
        ],
    )
    contract = PanelContract(all_query_panels=("panel-a", "panel-b"), by_control={"namespace": ("panel-a",)})
    missing_dep = ControlScenario(
        label="missing",
        key="missing",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("x",)),
        assertions=Assertions(affected_panels="query_dependency"),
    )
    scenario = _scenario(controls=(tuple_control, _instance_control(), _namespace_control(), missing_dep))
    report = _run(browser, scenario, tmp_path, contract)
    missing_result = next(result for result in report.results if result.name == "missing=x")
    assert any(f.failure_class is FailureClass.FRAMEWORK_ERROR for f in missing_result.findings)
    assert browser.settle_expected_panels[1] == ("panel-z",)
    assert set(browser.settle_expected_panels[3]) == {"panel-a", "panel-b"}


def test_gap_and_missing_plan_step_statuses(tmp_path: Path) -> None:
    gap_control = ControlScenario(
        label="function",
        key="function",
        adapter="esql_function",
        capability=CapabilityCategory.MIGRATION_GAP,
        options=OptionPolicy(strategy="every"),
        assertions=Assertions(),
        expected_gap="translator does not emit function controls",
    )
    missing_control = ControlScenario(
        label="ghost",
        key="ghost",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="every"),
        assertions=Assertions(),
    )
    missing_option_control = ControlScenario(
        label="empty",
        key="empty",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("only",)),
        assertions=Assertions(affected_panels=("panel-a",)),
    )
    browser = FakeBrowser(controls={"empty": ()})
    scenario = _scenario(controls=(gap_control, missing_control, missing_option_control))
    report = _run(browser, scenario, tmp_path)

    gap = next(r for r in report.results if r.name == "function:coverage_gap")
    missing = next(r for r in report.results if r.name == "ghost:missing_control")
    missing_option = next(r for r in report.results if r.name == "empty:missing_option")
    assert gap.status is InteractionStatus.WARN
    assert missing.status is InteractionStatus.FAIL
    assert missing_option.status is InteractionStatus.FAIL


def test_query_bar_filter_text_is_not_required_in_esql_query_text(
    tmp_path: Path,
) -> None:
    control = ControlScenario(
        label="query bar",
        key="query_bar",
        adapter="query_bar",
        capability=CapabilityCategory.KIBANA_ONLY,
        options=OptionPolicy(
            strategy="declared",
            include=('service.environment:"prod"',),
        ),
        assertions=Assertions(
            affected_panels=("panel-a",),
            query_contains=('service.environment:"prod"',),
            minimum_rows=1,
            expect_data_change=False,
        ),
        expected_gap="native Kibana capability",
    )
    browser = FakeBrowser(
        controls={"query_bar": ()},
        network_by_step=[
            (
                _esql_network(
                    "panel-a",
                    query="FROM metrics-* | STATS value=COUNT(*)",
                ),
            )
        ],
    )

    report = _run(
        browser,
        _scenario(controls=(control,)),
        tmp_path,
        PanelContract(all_query_panels=("panel-a",)),
    )

    assert report.results[0].status is InteractionStatus.PASS


def test_multiple_value_control_builds_sequence_wire_expectation() -> None:
    control = ControlScenario(
        label="services",
        key="services",
        adapter="esql_value",
        capability=CapabilityCategory.KIBANA_ONLY,
        options=OptionPolicy(strategy="declared", include=("api,worker",)),
        assertions=Assertions(selection=("services",)),
        multiple=True,
        expected_gap="native capability",
    )

    value_params, identifier_params = _expected_params_for_control(
        control,
        "api,worker",
    )

    assert value_params == {"services": ["api", "worker"]}
    assert identifier_params == {}


def test_multiple_value_control_single_selection_uses_sequence_wire() -> None:
    control = ControlScenario(
        label="services",
        key="services",
        adapter="esql_value",
        capability=CapabilityCategory.KIBANA_ONLY,
        options=OptionPolicy(strategy="declared", include=("worker",)),
        assertions=Assertions(selection=("services",)),
        multiple=True,
    )

    value_params, _ = _expected_params_for_control(control, "worker")

    assert value_params == {"services": ["worker"]}


def test_selection_matches_baseline_requires_exact_multiselect_set() -> None:
    from observability_migration.targets.kibana.interaction_runner import (
        _selection_matches_baseline,
    )

    control = ControlScenario(
        label="services",
        key="services",
        adapter="esql_value",
        capability=CapabilityCategory.KIBANA_ONLY,
        options=OptionPolicy(strategy="every"),
        assertions=Assertions(),
        multiple=True,
    )

    assert _selection_matches_baseline(control, "api", ("api", "worker")) is False
    assert _selection_matches_baseline(control, "api,worker", ("api", "worker")) is True
    assert _selection_matches_baseline(control, "worker", ("worker",)) is True


def test_combination_applies_each_query_contract_to_its_own_panel(
    tmp_path: Path,
) -> None:
    function = ControlScenario(
        label="aggregate",
        key="aggregate",
        adapter="esql_function",
        capability=CapabilityCategory.KIBANA_ONLY,
        options=OptionPolicy(strategy="declared", include=("MAX",)),
        assertions=Assertions(
            selection=("aggregate",),
            affected_panels=("function-panel",),
            query_contains=("??aggregate(",),
            required_columns=("value",),
            minimum_rows=1,
            expect_data_change=False,
        ),
        expected_gap="native Kibana capability",
    )
    interval = ControlScenario(
        label="interval",
        key="interval",
        adapter="esql_interval",
        capability=CapabilityCategory.KIBANA_ONLY,
        options=OptionPolicy(strategy="declared", include=("5 minutes",)),
        assertions=Assertions(
            selection=("interval",),
            affected_panels=("interval-panel",),
            query_contains=("TBUCKET(?interval)",),
            required_columns=("value", "bucket"),
            minimum_rows=1,
            expect_data_change=False,
        ),
        expected_gap="native Kibana capability",
    )
    browser = FakeBrowser(
        controls={
            "aggregate": ("AVG", "MAX"),
            "interval": ("1 minute", "5 minutes"),
        },
        network_by_step=[
            (_esql_network("function-panel", query="STATS value=??aggregate(x)"),),
            (_esql_network("interval-panel", query="STATS BY TBUCKET(?interval)"),),
            (
                _esql_network(
                    "function-panel",
                    query="STATS value=??aggregate(x)",
                    params={"aggregate": "MAX", "interval": "5 minutes"},
                ),
                _esql_network(
                    "interval-panel",
                    query="STATS value=AVG(x) BY bucket=TBUCKET(?interval)",
                    params={"aggregate": "MAX", "interval": "5 minutes"},
                    columns=("value", "bucket"),
                ),
            ),
        ],
    )
    scenario = _scenario(
        controls=(function, interval),
        combinations=(
            CombinationScenario(
                id="function-and-interval",
                selections=MappingProxyType(
                    {"aggregate": "MAX", "interval": "5 minutes"}
                ),
            ),
        ),
    )

    report = _run(browser, scenario, tmp_path)
    combination = next(
        result for result in report.results if result.name == "function-and-interval"
    )

    assert combination.status is InteractionStatus.PASS


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ControlNotFound("missing"), FailureClass.CONTROL_NOT_FOUND),
        (OptionNotFound("missing option"), FailureClass.OPTION_NOT_FOUND),
        (SelectionDidNotStick("stuck"), FailureClass.SELECTION_DID_NOT_STICK),
        (BrowserAdapterError("broken"), FailureClass.FRAMEWORK_ERROR),
    ],
)
def test_adapter_exception_mapping(
    tmp_path: Path,
    error: Exception,
    expected: FailureClass,
) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        selected={"namespace": ("baseline",)},
        select_errors={("namespace", "ns_1"): error},
    )
    report = _run(browser, _scenario(), tmp_path)
    assert report.status == "fail"
    assert any(f.failure_class is expected for f in report.results[0].findings)


def test_settle_timeout_mapping_and_observation_artifacts(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        settle_timeout=True,
        network_by_step=[(_esql_network("panel-a"),)],
    )
    report = _run(browser, _scenario(), tmp_path)
    assert any(f.failure_class is FailureClass.SETTLE_TIMEOUT for f in report.results[0].findings)
    assert (_step_dir(tmp_path, "namespace=ns_1") / "network.json").exists()


def test_query_contract_render_legend_console_and_status_classifications(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        selected={"namespace": ("ns_1",)},
        panel_details={"panel-a": "Unexpected error from Elasticsearch", "panel-b": "No results found"},
        network_by_step=[
            (
                _esql_network("panel-a", params={"namespace": "ns_1"}, row_count=0),
                _esql_network("panel-b", params={"namespace": "ns_1"}, row_count=0),
            ),
            (
                _esql_network("panel-a", params={"namespace": "wrong"}, row_count=0),
                _esql_network("panel-b", params={"namespace": "ns_2"}, row_count=0),
            ),
        ],
        console_by_step=[("duplicate error", "duplicate error"), ("visible console failure",)],
    )
    scenario = _scenario(
        controls=(
            _namespace_control(
                expected_legend=("missing-legend",),
                affected_panels=("panel-a", "panel-b"),
            ),
        )
    )
    report = _run(browser, scenario, tmp_path)
    first = report.results[0]
    assert any(f.failure_class is FailureClass.RENDER_ERROR for f in first.findings)
    assert any(f.failure_class is FailureClass.UNEXPECTED_EMPTY for f in first.findings)
    assert len([f for f in first.findings if f.failure_class is FailureClass.CONSOLE_ERROR]) == 1
    assert any(f.failure_class is FailureClass.INTERACTION_REGRESSION for f in first.findings)


def test_expected_data_change_skips_default_reselection(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        selected={"namespace": ("ns_1",)},
        panel_details={"panel-a": "baseline"},
        network_by_step=[(_esql_network("panel-a"),)],
    )
    report = _run(browser, _scenario(), tmp_path)
    assert report.results[0].status is InteractionStatus.PASS


def test_default_option_passes_from_reset_baseline_without_click(
    tmp_path: Path,
) -> None:
    baseline_request = _esql_network(
        "panel-a",
        params={"namespace": "ns_1"},
    )
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        selected={"namespace": ("ns_2",)},
        selected_after_reset={"namespace": ("ns_1",)},
        baseline_network_by_step=[(baseline_request,)],
        network_by_step=[()],
    )
    control = _namespace_control(
        affected_panels=("panel-a",),
        expect_data_change=True,
    )
    control = ControlScenario(
        **{
            **control.__dict__,
            "options": OptionPolicy(strategy="declared", include=("ns_1",)),
        }
    )

    report = _run(browser, _scenario(controls=(control,)), tmp_path)

    assert report.results[0].status is InteractionStatus.PASS
    assert browser.select_calls == []
    payload = json.loads(
        (_step_dir(tmp_path, "namespace=ns_1") / "selection.json").read_text()
    )
    assert payload["selections"] == [
        {
            "control_key": "namespace",
            "selected_value": "ns_1",
            "baseline_selected": ["ns_1"],
            "mode": "baseline_noop",
            "selected_count": 1,
            "incompatible_warning": "",
        }
    ]


def test_reset_baseline_remaps_generated_panel_ids_by_title(
    tmp_path: Path,
) -> None:
    baseline_request = replace(
        _esql_network("fresh-panel", params={"namespace": "ns_1"}),
        panel_title="Namespace panel",
    )
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        baseline_network_by_step=[(baseline_request,)],
        network_by_step=[()],
    )
    control = ControlScenario(
        label="namespace",
        key="namespace",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("ns_1",)),
        assertions=Assertions(
            selection=("namespace",),
            affected_panels=("stable-panel",),
            minimum_rows=1,
            expect_data_change=False,
        ),
    )
    contract = PanelContract(
        all_query_panels=("stale-panel",),
        panel_aliases={"stable-panel": "stale-panel"},
        panel_titles={"stale-panel": "Namespace panel"},
    )

    report = _run(
        browser,
        _scenario(controls=(control,)),
        tmp_path,
        contract,
    )

    assert report.results[0].status is InteractionStatus.PASS
    assert [panel.panel_id for panel in report.results[0].panels] == [
        "fresh-panel"
    ]


def test_nondefault_action_cannot_pass_from_reset_request_alone(
    tmp_path: Path,
) -> None:
    reset_request_with_future_value = _esql_network(
        "panel-a",
        params={"namespace": "ns_2"},
    )
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        selected_after_reset={"namespace": ("ns_1",)},
        baseline_network_by_step=[(reset_request_with_future_value,)],
        network_by_step=[()],
    )
    control = _namespace_control(
        affected_panels=("panel-a",),
        expect_data_change=False,
    )
    control = ControlScenario(
        **{
            **control.__dict__,
            "options": OptionPolicy(strategy="declared", include=("ns_2",)),
        }
    )

    report = _run(browser, _scenario(controls=(control,)), tmp_path)
    result = report.results[0]

    assert browser.select_calls == [("namespace", "ns_2")]
    assert any(
        finding.failure_class is FailureClass.EXPECTED_REQUEST_MISSING
        for finding in result.findings
    )


def test_mixed_combination_skips_baseline_and_selects_changed_once(
    tmp_path: Path,
) -> None:
    namespace = ControlScenario(
        label="namespace",
        key="namespace",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("ns_1",)),
        assertions=Assertions(
            selection=("namespace",),
            affected_panels=("panel-a",),
            minimum_rows=1,
            expect_data_change=False,
        ),
    )
    instance = ControlScenario(
        label="instance",
        key="instance",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("i_1",)),
        assertions=Assertions(
            selection=("instance",),
            affected_panels=("panel-b",),
            minimum_rows=1,
            expect_data_change=False,
        ),
    )
    browser = FakeBrowser(
        controls={
            "namespace": ("ns_1",),
            "instance": ("i_1", "i_2"),
        },
        selected_after_reset={
            "namespace": ("ns_1",),
            "instance": ("i_1",),
        },
        baseline_network_by_step=[
            (_esql_network("panel-a", params={"namespace": "ns_1"}),),
            (_esql_network("panel-b", params={"instance": "i_1"}),),
            (
                _esql_network(
                    "panel-a",
                    params={"namespace": "ns_1", "instance": "i_1"},
                ),
                _esql_network(
                    "panel-b",
                    params={"namespace": "ns_1", "instance": "i_1"},
                ),
            ),
        ],
        network_by_step=[
            (),
            (),
            (
                _esql_network(
                    "panel-a",
                    params={"namespace": "ns_1", "instance": "i_2"},
                ),
                _esql_network(
                    "panel-b",
                    params={"namespace": "ns_1", "instance": "i_2"},
                ),
            ),
        ],
    )
    scenario = _scenario(
        controls=(namespace, instance),
        combinations=(
            CombinationScenario(
                id="mixed",
                selections=MappingProxyType(
                    {"namespace": "ns_1", "instance": "i_2"}
                ),
            ),
        ),
    )

    report = _run(browser, scenario, tmp_path)
    mixed = next(result for result in report.results if result.name == "mixed")

    assert mixed.status is InteractionStatus.PASS
    assert browser.select_calls == [("instance", "i_2")]


def test_reset_baseline_timeout_does_not_attempt_action(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        selected_after_reset={"namespace": ("ns_1",)},
        baseline_settle_timeout=True,
    )
    control = _namespace_control(
        affected_panels=("panel-a",),
        expect_data_change=False,
    )
    control = ControlScenario(
        **{
            **control.__dict__,
            "options": OptionPolicy(strategy="declared", include=("ns_2",)),
        }
    )

    report = _run(browser, _scenario(controls=(control,)), tmp_path)
    result = report.results[0]

    assert browser.select_calls == []
    assert any(
        finding.failure_class is FailureClass.SETTLE_TIMEOUT
        and "reset baseline" in finding.detail
        for finding in result.findings
    )


def test_expected_data_change_requires_difference_for_non_default(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        selected={"namespace": ("ns_1",)},
        panel_details={"panel-a": "same fingerprint"},
        network_by_step=[(_esql_network("panel-a", params={"namespace": "ns_2"}),)],
    )
    report = _run(browser, _scenario(controls=(_namespace_control(expect_data_change=True),)), tmp_path)
    second = next(result for result in report.results if result.name == "namespace=ns_2")
    assert any(
        f.failure_class is FailureClass.INTERACTION_REGRESSION and "fingerprints" in f.detail
        for f in second.findings
    )


def test_incompatible_warning_allowed_vs_failure(tmp_path: Path) -> None:
    allowed = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        incompatible_warnings={"namespace": "Incompatible selections (2)"},
        network_by_step=[(_esql_network("panel-a"),)],
    )
    allowed_report = _run(
        allowed,
        _scenario(controls=(_namespace_control(allow_incompatible_selections=True),)),
        tmp_path,
    )
    assert allowed_report.results[0].status is InteractionStatus.PASS, [
        (finding.failure_class, finding.detail)
        for finding in allowed_report.results[0].findings
    ]

    failing = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        incompatible_warnings={"namespace": "Incompatible selections (2)"},
        network_by_step=[(_esql_network("panel-a"),)],
    )
    failing_report = _run(failing, _scenario(), tmp_path / "fail")
    assert any(f.failure_class is FailureClass.INTERACTION_REGRESSION for f in failing_report.results[0].findings)


def test_exact_network_allowance_only(tmp_path: Path) -> None:
    allowed = NetworkEvidence(
        endpoint="/internal/security/user_profile",
        method="GET",
        status=404,
        url="http://localhost:5601/internal/security/user_profile",
    )
    rejected = NetworkEvidence(
        endpoint="/internal/security/user_profile",
        method="POST",
        status=404,
        url="http://localhost:5601/internal/security/user_profile",
    )
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        network_by_step=[(_esql_network("panel-a"), allowed, rejected)],
    )
    scenario = _scenario(
        noise_allowances=(
            NoiseAllowance(
                endpoint="/internal/security/user_profile",
                method="GET",
                status=404,
                rationale="Security disabled locally.",
            ),
        )
    )
    report = _run(browser, scenario, tmp_path)
    findings = report.results[0].findings
    assert any("allowed network noise" in f.detail for f in findings)
    assert any(f.failure_class is FailureClass.QUERY_CONTRACT_ERROR for f in findings)


def test_finding_dedup_preserves_first_occurrence_order(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        console_by_step=[("dup", "dup")],
        network_by_step=[(_esql_network("panel-a", status=500),)],
    )
    report = _run(browser, _scenario(), tmp_path)
    classes = [f.failure_class for f in report.results[0].findings]
    assert classes.index(FailureClass.SERVER_ERROR) < classes.index(FailureClass.CONSOLE_ERROR)
    assert len([f for f in report.results[0].findings if f.failure_class is FailureClass.CONSOLE_ERROR]) == 1


def test_continue_after_safe_failure(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        select_errors={("namespace", "ns_1"): OptionNotFound("missing option")},
        network_by_step=[(), (_esql_network("panel-a", params={"namespace": "ns_2"}),)],
    )
    report = _run(
        browser,
        _scenario(controls=(_namespace_control(expect_data_change=False),)),
        tmp_path,
    )
    assert len(report.results) == 2
    assert report.results[0].status is InteractionStatus.FAIL
    assert report.results[1].status is InteractionStatus.PASS


def test_clear_evidence_after_artifact_write_and_pending_rejection(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        network_by_step=[(_esql_network("panel-a"),)],
        pending_after_step=True,
    )
    report = _run(browser, _scenario(), tmp_path)
    assert browser.clear_count == 2
    assert any("clear_evidence failed" in f.detail for f in report.results[0].findings)


def test_artifact_file_set_atomic_writes_and_no_response_rows(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        network_by_step=[
            (
                NetworkEvidence(
                    endpoint="/internal/search/esql",
                    method="POST",
                    status=200,
                    url="http://localhost:5601/internal/search/esql",
                    query="FROM metrics-*",
                    panel_id="panel-a",
                    body={"query": "FROM metrics-*", "values": [[1, 2, 3]]},
                    response_columns=("value",),
                    row_count=3,
                ),
            )
        ],
    )
    _run(browser, _scenario(), tmp_path)
    step_dir = _step_dir(tmp_path, "namespace=ns_1")
    expected = {
        "before.png",
        "after.png",
        "network.json",
        "console.json",
        "snapshot.txt",
        "selection.json",
        "pending-requests.json",
        "result.json",
    }
    assert expected.issubset({path.name for path in step_dir.iterdir()})
    serialized = (step_dir / "network.json").read_text(encoding="utf-8")
    assert "values" not in serialized
    _assert_no_temp_artifacts(tmp_path)


def test_snapshot_text_redaction_scans_all_text_artifacts(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        accessibility_snapshot="Authorization: ApiKey secret\nCookie: sid=secret",
        visible_text="Visit https://user:secret@localhost:5601/app?namespace=prod",
        panel_details={
            "panel-a": "api_key=leaked and Set-Cookie: session=abc",
        },
        network_by_step=[(_esql_network("panel-a"),)],
    )
    _run(browser, _scenario(), tmp_path)
    for path in _scan_artifacts(tmp_path):
        if path.suffix in {".json", ".txt"}:
            _assert_no_sensitive_text(path)
    snapshot = (_step_dir(tmp_path, "namespace=ns_1") / "snapshot.txt").read_text(encoding="utf-8")
    assert "Authorization: [REDACTED]" in snapshot
    assert "https://localhost:5601" in snapshot
    assert "namespace=prod" in snapshot
    assert "ApiKey secret" not in snapshot
    assert "sid=secret" not in snapshot


def test_clear_evidence_failure_preserves_result_payload(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        network_by_step=[(_esql_network("panel-a"),)],
        pending_after_step=True,
    )
    _run(browser, _scenario(), tmp_path)
    result_path = _step_dir(tmp_path, "namespace=ns_1") / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["artifact_flags"]["before_screenshot"] is True
    assert payload["artifact_flags"]["after_screenshot"] is True
    assert payload["cursor"]["network_index"] == 1
    assert any(
        finding["failure_class"] == FailureClass.FRAMEWORK_ERROR.value
        and "clear_evidence failed" in finding["detail"]
        for finding in payload["findings"]
    )


def test_settle_timeout_skips_data_change_regression(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        selected={"namespace": ("ns_1",)},
        panel_details={"panel-a": "unchanged after timeout"},
        settle_timeout=True,
        network_by_step=[(_esql_network("panel-a", params={"namespace": "ns_2"}),)],
    )
    report = _run(
        browser,
        _scenario(
            controls=(
                ControlScenario(
                    label="namespace",
                    key="namespace",
                    adapter="esql_value",
                    capability=CapabilityCategory.MIGRATED_LIVE,
                    options=OptionPolicy(strategy="declared", include=("ns_2",)),
                    assertions=Assertions(
                        selection=("namespace",),
                        affected_panels=("panel-a",),
                        expect_data_change=True,
                    ),
                ),
            )
        ),
        tmp_path,
        PanelContract(all_query_panels=("panel-a",), by_control={"namespace": ("panel-a",)}),
    )
    result = report.results[0]
    assert any(f.failure_class is FailureClass.SETTLE_TIMEOUT for f in result.findings)
    assert not any(
        f.failure_class is FailureClass.INTERACTION_REGRESSION and "fingerprints" in f.detail
        for f in result.findings
    )


def test_conflicting_stable_alias_is_framework_error(tmp_path: Path) -> None:
    left = ControlScenario(
        label="left",
        key="left",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("a",)),
        assertions=Assertions(
            affected_panels=("panel-a",),
            stable_alias="alias_left",
        ),
    )
    right = ControlScenario(
        label="right",
        key="right",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("b",)),
        assertions=Assertions(
            affected_panels=("panel-b",),
            stable_alias="alias_right",
        ),
    )
    browser = FakeBrowser(
        controls={"left": ("a",), "right": ("b",)},
        network_by_step=[
            (_esql_network("panel-a"),),
            (_esql_network("panel-b"),),
            (
                _esql_network("panel-a"),
                _esql_network("panel-b"),
            ),
        ],
    )
    scenario = _scenario(
        controls=(left, right),
        combinations=(
            CombinationScenario(
                id="both",
                selections=MappingProxyType({"left": "a", "right": "b"}),
            ),
        ),
    )
    report = _run(browser, scenario, tmp_path)
    combo = next(result for result in report.results if result.name == "both")
    assert any(
        f.failure_class is FailureClass.FRAMEWORK_ERROR and "conflicting stable_alias" in f.detail
        for f in combo.findings
    )


def test_compatible_stable_alias_is_allowed(tmp_path: Path) -> None:
    control = ControlScenario(
        label="namespace",
        key="namespace",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("ns_1",)),
        assertions=Assertions(
            selection=("namespace",),
            affected_panels=("panel-a",),
            stable_alias="value",
            expect_data_change=False,
        ),
    )
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        network_by_step=[
            (
                NetworkEvidence(
                    endpoint="/internal/search/esql",
                    method="POST",
                    status=200,
                    url="http://localhost:5601/internal/search/esql",
                    query="FROM metrics-* | WHERE service.environment == ?namespace",
                    panel_id="panel-a",
                    params={"namespace": "ns_1"},
                    param_kinds={"namespace": "value"},
                    response_columns=("value",),
                    row_count=3,
                ),
            )
        ],
    )
    report = _run(
        browser,
        _scenario(controls=(control,)),
        tmp_path,
        PanelContract(all_query_panels=("panel-a",), by_control={"namespace": ("panel-a",)}),
    )
    assert not any(
        f.failure_class is FailureClass.FRAMEWORK_ERROR and "stable_alias" in f.detail
        for f in report.results[0].findings
    )


def _identifier_network(
    panel_id: str,
    *,
    query: str,
    param_name: str,
    identifier: str,
) -> NetworkEvidence:
    return NetworkEvidence(
        endpoint="/internal/search/esql",
        method="POST",
        status=200,
        url=f"http://localhost:5601/internal/search/esql?panel={panel_id}",
        query=query,
        panel_id=panel_id,
        params={param_name: identifier},
        param_kinds={param_name: "identifier"},
        response_columns=("value",),
        row_count=3,
    )


@pytest.mark.parametrize(
    ("adapter", "param_name", "query", "selection"),
    [
        (
            "esql_field",
            "grouping",
            "FROM metrics-* | STATS value=AVG(x) BY grouping=??grouping",
            "host.name",
        ),
        (
            "esql_function",
            "aggregate",
            "FROM metrics-* | STATS value=??aggregate(x)",
            "AVG",
        ),
    ],
)
def test_identifier_adapters_use_expected_identifier_params(
    tmp_path: Path,
    adapter: str,
    param_name: str,
    query: str,
    selection: str,
) -> None:
    control = ControlScenario(
        label=param_name,
        key=param_name,
        adapter=adapter,
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=(selection,)),
        assertions=Assertions(
            selection=(param_name,),
            affected_panels=("panel-a",),
            expect_data_change=False,
        ),
    )
    browser = FakeBrowser(
        controls={param_name: (selection,)},
        network_by_step=[(_identifier_network("panel-a", query=query, param_name=param_name, identifier=selection),)],
    )
    report = _run(
        browser,
        _scenario(controls=(control,)),
        tmp_path,
        PanelContract(all_query_panels=("panel-a",), by_control={param_name: ("panel-a",)}),
    )
    assert report.results[0].status is InteractionStatus.PASS


def test_esql_value_and_interval_use_expected_value_params(tmp_path: Path) -> None:
    value_control = ControlScenario(
        label="environment",
        key="environment",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("prod",)),
        assertions=Assertions(
            selection=("environment",),
            affected_panels=("panel-a",),
            query_contains=("?environment",),
            expect_data_change=False,
        ),
    )
    interval_control = ControlScenario(
        label="interval",
        key="interval",
        adapter="esql_interval",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("5 minutes",)),
        assertions=Assertions(
            selection=("interval",),
            affected_panels=("panel-b",),
            query_contains=("?interval",),
            expect_data_change=False,
        ),
    )
    browser = FakeBrowser(
        controls={"environment": ("prod",), "interval": ("5 minutes",)},
        network_by_step=[
            (
                _esql_network(
                    "panel-a",
                    query="FROM metrics-* | WHERE environment == ?environment",
                    params={"environment": "prod"},
                ),
                _esql_network(
                    "panel-b",
                    query="TS metrics-* | STATS value=AVG(x) BY bucket=TBUCKET(?interval)",
                    params={"interval": "5 minutes"},
                ),
            ),
            (
                _esql_network(
                    "panel-b",
                    query="TS metrics-* | STATS value=AVG(x) BY bucket=TBUCKET(?interval)",
                    params={"interval": "5 minutes"},
                ),
            ),
        ],
    )
    contract = PanelContract(
        all_query_panels=("panel-a", "panel-b"),
        by_control={"environment": ("panel-a",), "interval": ("panel-b",)},
    )
    report = _run(browser, _scenario(controls=(value_control, interval_control)), tmp_path, contract)
    assert all(result.status is InteractionStatus.PASS for result in report.results)


def test_unaffected_panel_success_triggers_unexpected_panel_request(tmp_path: Path) -> None:
    control = ControlScenario(
        label="namespace",
        key="namespace",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("ns_1",)),
        assertions=Assertions(
            selection=("namespace",),
            affected_panels=("panel-a",),
            unaffected_panels=("panel-b",),
            expect_data_change=False,
        ),
    )
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        selected={"namespace": ("ns_0",)},
        network_by_step=[
            (
                _esql_network("panel-a", params={"namespace": "ns_1"}),
                _esql_network("panel-b", params={"namespace": "ns_1"}),
            )
        ],
    )
    report = _run(
        browser,
        _scenario(controls=(control,)),
        tmp_path,
        PanelContract(all_query_panels=("panel-a", "panel-b"), by_control={"namespace": ("panel-a",)}),
    )
    assert any(
        f.failure_class is FailureClass.UNEXPECTED_PANEL_REQUEST
        for f in report.results[0].findings
    )


def test_selection_json_records_post_read_state(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        incompatible_warnings={"namespace": "Incompatible selections (2)"},
        network_by_step=[(_esql_network("panel-a"),)],
    )
    _run(
        browser,
        _scenario(controls=(_namespace_control(allow_incompatible_selections=True),)),
        tmp_path,
    )
    payload = json.loads((_step_dir(tmp_path, "namespace=ns_1") / "selection.json").read_text())
    assert payload["selections"] == [
        {
            "control_key": "namespace",
            "selected_value": "ns_1",
            "baseline_selected": ["ns_1"],
            "mode": "baseline_noop",
            "selected_count": 1,
            "incompatible_warning": "Incompatible selections (2)",
        }
    ]


def test_cli_missing_and_invalid_manifest_return_two_without_browser_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _TrackingBrowser:
        started = False

        def start(self, **kwargs: Any) -> None:
            self.started = True

        def close(self) -> None:
            return

    stub = _TrackingBrowser()
    module = _load_cli_module(monkeypatch, stub)

    missing = module.main(
        [
            "--manifest",
            str(tmp_path / "missing.yaml"),
            "--dashboard-url",
            "http://localhost:5601/app/dashboards#/view/test",
        ]
    )
    assert missing == 2
    assert stub.started is False
    assert "ERROR:" in capsys.readouterr().err

    invalid_manifest = tmp_path / "invalid.yaml"
    invalid_manifest.write_text("version: 2\nunknown: true\n", encoding="utf-8")
    invalid = module.main(
        [
            "--manifest",
            str(invalid_manifest),
            "--dashboard-url",
            "http://localhost:5601/app/dashboards#/view/test",
        ]
    )
    assert invalid == 2
    assert stub.started is False
    err = capsys.readouterr().err
    assert "ERROR:" in err
    assert "secret" not in err.lower()


def test_recursive_redaction_scan_over_artifacts(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        network_by_step=[
            (
                NetworkEvidence(
                    endpoint="/internal/search/esql",
                    method="POST",
                    status=200,
                    url="https://user:secret@localhost:5601/internal/search/esql",
                    query="FROM metrics-*",
                    panel_id="panel-a",
                    headers={
                        "Authorization": "ApiKey secret",
                        "cookie": "sid=secret",
                        "x-elastic-api-key": "abc",
                    },
                    body={"api_key": "secret"},
                ),
            )
        ],
    )
    _run(browser, _scenario(), tmp_path)
    for path in _scan_artifacts(tmp_path):
        if path.suffix in {".json", ".txt"}:
            _assert_no_sensitive_text(path)


def test_report_denominator_capability_counts_and_exit_code(tmp_path: Path) -> None:
    gap_control = ControlScenario(
        label="gap",
        key="gap",
        adapter="esql_function",
        capability=CapabilityCategory.MIGRATION_GAP,
        options=OptionPolicy(strategy="every"),
        assertions=Assertions(),
        expected_gap="unsupported",
    )
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        network_by_step=[(_esql_network("panel-a"),)],
    )
    report = _run(browser, _scenario(controls=(_namespace_control(), gap_control)), tmp_path)
    payload = json.loads((tmp_path / "test-scenario" / "run-1" / "report.json").read_text())
    assert payload["verification_total"] == len(report.results)
    assert payload["panels_total"] == 2
    assert payload["counts"]["total"] == len(report.results)
    assert payload["capabilities"]["migrated_live"]["pass"] == 1
    assert payload["capabilities"]["migration_gap"]["warn"] == 1
    assert payload["exit_code"] == report.exit_code


def test_load_panel_contract_validation(tmp_path: Path) -> None:
    valid = tmp_path / "contract.json"
    valid.write_text(
        json.dumps({"all_query_panels": ["panel-a"], "by_control": {"namespace": ["panel-a"]}}),
        encoding="utf-8",
    )
    contract = load_panel_contract(valid)
    assert contract.all_query_panels == ("panel-a",)
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"by_control": "nope"}', encoding="utf-8")
    with pytest.raises(ValueError, match="by_control"):
        load_panel_contract(invalid)


def test_cli_args_panel_contract_validation_and_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "cli-manifest.yaml"
    manifest_path.write_text(
        """
version: 1
id: cli-minimal
title: CLI minimal
source:
  kind: grafana
  path: path.json
dashboard:
  title: CLI Dashboard
controls:
  - label: namespace
    key: namespace
    adapter: esql_value
    capability: migrated_live
    options:
      strategy: declared
      include: [ns_1]
    assertions:
      selection: [namespace]
      affected_panels: query_dependency
      query_contains: ["?namespace"]
      expect_data_change: false
combinations: []
noise_allowances: []
""".strip(),
        encoding="utf-8",
    )
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps({"all_query_panels": ["panel-a"], "by_control": {"namespace": ["panel-a"]}}),
        encoding="utf-8",
    )

    class _StubBrowser:
        closed = False

        def start(self, **kwargs: Any) -> None:
            self.start_kwargs = kwargs

        def close(self) -> None:
            self.closed = True

        def open_dashboard(self, url: str) -> None:
            del url

        def reset(self, url: str) -> None:
            del url

        def discover(self, control: ControlScenario) -> DiscoveredControl:
            return DiscoveredControl(
                key=control.key,
                label=control.label,
                options=("ns_1",),
                selected=("ns_1",),
            )

        def select(self, control: ControlScenario, option: str) -> None:
            del control, option

        def read_state(self, control: ControlScenario) -> ControlState:
            del control
            return ControlState()

        def capture(
            self,
            expected_panels: Sequence[str],
            cursor: CaptureCursor | None = None,
        ) -> BrowserObservation:
            del cursor
            return BrowserObservation(
                url="http://localhost",
                accessibility_snapshot="",
                visible_text="",
                panels=tuple(
                    PanelEvidence(panel_id=panel_id, title=panel_id, status="stable", detail="ok")
                    for panel_id in expected_panels
                ),
                network=tuple(
                    _esql_network(
                        panel_id,
                        params={"namespace": "ns_1"},
                        query="FROM metrics-* | WHERE service.environment == ?namespace",
                    )
                    for panel_id in expected_panels
                ),
            )

        def begin_step(self) -> CaptureCursor:
            return CaptureCursor(0, 0)

        def settle(
            self,
            cursor: CaptureCursor,
            expected_panels: Sequence[str],
            *,
            policy: Any = None,
        ) -> BrowserObservation:
            del cursor, policy
            return self.capture(expected_panels)

        def screenshot(self, path: str | Path) -> bool:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"png")
            return True

        def clear_evidence(self) -> None:
            return

    stub = _StubBrowser()
    run_interaction_audit = _load_cli_module(monkeypatch, stub)

    exit_code = run_interaction_audit.main(
        [
            "--manifest",
            str(manifest_path),
            "--dashboard-url",
            "http://localhost:5601/app/dashboards#/view/test",
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "cli-run",
            "--panel-contract",
            str(contract_path),
            "--headed",
            "--timeout-seconds",
            "5",
        ]
    )
    assert exit_code == 0
    assert (tmp_path / "artifacts" / "cli-minimal" / "cli-run" / "report.json").exists()
    assert stub.closed is True
    assert stub.start_kwargs["headless"] is False

    bad_contract = tmp_path / "bad-contract.json"
    bad_contract.write_text('{"by_control": "nope"}', encoding="utf-8")
    failed = run_interaction_audit.main(
        [
            "--manifest",
            str(manifest_path),
            "--dashboard-url",
            "http://localhost:5601/app/dashboards#/view/test",
            "--panel-contract",
            str(bad_contract),
        ]
    )
    assert failed == 2


def test_recursive_redaction_covers_inline_and_midline_strings(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",)},
        console_by_step=[
            (
                "preflight failed Authorization: ApiKey secret mid-request",
                "retry Cookie: sid=secret after backoff",
                "upstream Set-Cookie: session=abc blocked",
                "header X-Elastic-Api-Key: abc rejected",
                "body api_key=leaked in payload",
            ),
        ],
        network_by_step=[
            (
                NetworkEvidence(
                    endpoint="/internal/search/esql",
                    method="POST",
                    status=500,
                    url="https://user:secret@localhost:5601/internal/search/esql",
                    query="FROM metrics-* | WHERE service.environment == ?namespace",
                    panel_id="panel-a",
                    headers={"Authorization": "ApiKey secret"},
                    body={"api_key": "secret"},
                ),
            )
        ],
        incompatible_warnings={"namespace": "inline Authorization: ApiKey secret in warning"},
    )
    _run(browser, _scenario(), tmp_path)
    for path in _scan_artifacts(tmp_path):
        if path.suffix in {".json", ".txt"}:
            _assert_no_sensitive_text(path)
    result = json.loads((_step_dir(tmp_path, "namespace=ns_1") / "result.json").read_text())
    console = json.loads((_step_dir(tmp_path, "namespace=ns_1") / "console.json").read_text())
    joined = "\n".join(console["errors"])
    assert "Authorization: [REDACTED]" in joined
    assert "Cookie: [REDACTED]" in joined
    assert "Set-Cookie: [REDACTED]" in joined
    assert "X-Elastic-Api-Key: [REDACTED]" in joined
    assert "api_key: [REDACTED]" in joined
    assert "?namespace" in json.dumps(result)


@pytest.mark.parametrize(
    ("scenario_id", "run_id"),
    [
        ("", "run-1"),
        (".", "run-1"),
        ("..", "run-1"),
        ("../escape", "run-1"),
        ("test-scenario", ""),
        ("test-scenario", "."),
        ("test-scenario", ".."),
        ("test-scenario", "../escape"),
        ("scenario/with/slash", "run-1"),
        ("test-scenario", "run/with/slash"),
        ("test-scenario", "run\\with\\slash"),
    ],
)
def test_runner_rejects_unsafe_artifact_paths(
    tmp_path: Path,
    scenario_id: str,
    run_id: str,
) -> None:
    browser = FakeBrowser(controls={"namespace": ("ns_1",)})
    scenario = DashboardScenario(
        version=1,
        id=scenario_id,
        title="Test",
        source_kind="grafana",
        source_path="path.json",
        control_schema_path="",
        dashboard_title="Test Dashboard",
        time_from="now-3h",
        time_to="now",
        controls=(_namespace_control(),),
        combinations=(),
        noise_allowances=(),
    )
    with pytest.raises(ValueError, match=r"invalid|escapes"):
        InteractionRunner(
            browser,
            scenario,
            _panel_contract(),
            RunConfig(
                dashboard_url="http://localhost:5601/app/dashboards#/view/test",
                artifact_root=tmp_path,
                run_id=run_id,
            ),
        )


def test_runner_step_directories_stay_under_run_root(tmp_path: Path) -> None:
    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        network_by_step=[(_esql_network("panel-a"),), (_esql_network("panel-a"),)],
    )
    runner = InteractionRunner(
        browser,
        _scenario(),
        _panel_contract(),
        RunConfig(
            dashboard_url="http://localhost:5601/app/dashboards#/view/test",
            artifact_root=tmp_path,
            run_id="run-1",
        ),
    )
    report = runner.run()
    run_root = (tmp_path / "test-scenario" / "run-1").resolve()
    for result in report.results:
        step_dir = (run_root / result.name).resolve()
        assert step_dir == run_root or run_root in step_dir.parents


def test_partial_report_written_after_each_step_and_survives_artifact_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import observability_migration.targets.kibana.interaction_runner as runner_module

    browser = FakeBrowser(
        controls={"namespace": ("ns_1", "ns_2")},
        network_by_step=[(_esql_network("panel-a"),), (_esql_network("panel-a"),)],
    )
    original_write = runner_module._write_json_atomic
    write_calls: list[Path] = []

    def guarded_write(path: Path, payload: object) -> None:
        write_calls.append(path)
        if "namespace=ns_2" in str(path) and path.name != "report.json":
            raise OSError("disk full")
        original_write(path, payload)

    monkeypatch.setattr(runner_module, "_write_json_atomic", guarded_write)
    report = _run(browser, _scenario(), tmp_path)
    report_path = tmp_path / "test-scenario" / "run-1" / "report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["verification_total"] == 2
    assert payload["counts"]["total"] == 2
    assert payload["counts"]["fail"] >= 1
    assert len(report.results) == 2
    assert report.exit_code != 0
    assert any(
        finding.failure_class == FailureClass.FRAMEWORK_ERROR
        for result in report.results
        for finding in result.findings
        if result.name == "namespace=ns_2"
    )
    assert report_path.exists()
    _assert_no_temp_artifacts(tmp_path)


def test_combination_capability_prefers_migration_gap_over_migrated_live(
    tmp_path: Path,
) -> None:
    migrated = ControlScenario(
        label="namespace",
        key="namespace",
        adapter="esql_value",
        capability=CapabilityCategory.MIGRATED_LIVE,
        options=OptionPolicy(strategy="declared", include=("ns_1",)),
        assertions=Assertions(
            selection=("namespace",),
            affected_panels="query_dependency",
            query_contains=("?namespace",),
            expect_data_change=False,
        ),
    )
    gap = ControlScenario(
        label="gap",
        key="gap",
        adapter="esql_function",
        capability=CapabilityCategory.MIGRATION_GAP,
        options=OptionPolicy(strategy="declared", include=("sum",)),
        assertions=Assertions(
            selection=("gap",),
            affected_panels="query_dependency",
            expect_data_change=False,
        ),
    )
    browser = FakeBrowser(
        controls={"namespace": ("ns_1",), "gap": ("sum",)},
        network_by_step=[(_esql_network("panel-a"),)],
    )
    scenario = _scenario(
        controls=(migrated, gap),
        combinations=(
            CombinationScenario(
                id="mixed-combo",
                selections={"namespace": "ns_1", "gap": "sum"},
            ),
        ),
    )
    report = _run(browser, scenario, tmp_path)
    combo = next(result for result in report.results if result.name == "mixed-combo")
    assert combo.capability == CapabilityCategory.MIGRATION_GAP
    payload = json.loads((tmp_path / "test-scenario" / "run-1" / "report.json").read_text())
    assert payload["capabilities"]["migration_gap"]["total"] >= 1


def test_cli_browser_start_failure_returns_one_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "cli-manifest.yaml"
    manifest_path.write_text(MINIMAL.read_text(encoding="utf-8"), encoding="utf-8")

    class _FailingBrowser:
        closed = False

        def start(self, **kwargs: Any) -> None:
            del kwargs
            raise OSError("Authorization: ApiKey secret startup failure")

        def close(self) -> None:
            self.closed = True

    module = _load_cli_module(monkeypatch, _FailingBrowser())
    exit_code = module.main(
        [
            "--manifest",
            str(manifest_path),
            "--dashboard-url",
            "http://localhost:5601/app/dashboards#/view/test",
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "cli-run",
        ]
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "ERROR:" in err
    assert "ApiKey secret" not in err
    assert "Authorization: [REDACTED]" in err


def test_cli_runner_failure_returns_one_and_closes_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "cli-manifest.yaml"
    manifest_path.write_text(MINIMAL.read_text(encoding="utf-8"), encoding="utf-8")

    class _RunnerFailBrowser:
        closed = False

        def start(self, **kwargs: Any) -> None:
            del kwargs

        def close(self) -> None:
            self.closed = True

        def open_dashboard(self, url: str) -> None:
            del url
            foreign_error = _foreign_browser_adapter_error()
            raise foreign_error("Cookie: sid=secret navigation failed")

        def reset(self, url: str) -> None:
            del url

        def discover(self, control: ControlScenario) -> DiscoveredControl:
            del control
            raise BrowserAdapterError("unused")

        def select(self, control: ControlScenario, option: str) -> None:
            del control, option

        def read_state(self, control: ControlScenario) -> ControlState:
            del control
            return ControlState()

        def capture(
            self,
            expected_panels: Sequence[str],
            cursor: CaptureCursor | None = None,
        ) -> BrowserObservation:
            del expected_panels, cursor
            return BrowserObservation(url="", accessibility_snapshot="", visible_text="", panels=())

        def begin_step(self) -> CaptureCursor:
            return CaptureCursor(0, 0)

        def settle(
            self,
            cursor: CaptureCursor,
            expected_panels: Sequence[str],
            *,
            policy: Any = None,
        ) -> BrowserObservation:
            del cursor, expected_panels, policy
            return BrowserObservation(url="", accessibility_snapshot="", visible_text="", panels=())

        def screenshot(self, path: str | Path) -> bool:
            del path
            return False

        def clear_evidence(self) -> None:
            return

    stub = _RunnerFailBrowser()
    module = _load_cli_module(monkeypatch, stub)
    exit_code = module.main(
        [
            "--manifest",
            str(manifest_path),
            "--dashboard-url",
            "http://localhost:5601/app/dashboards#/view/test",
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "cli-run",
        ]
    )
    assert exit_code == 1
    assert stub.closed is True
    err = capsys.readouterr().err
    assert "ERROR:" in err
    assert "sid=secret" not in err
    assert "Cookie: [REDACTED]" in err
    assert "Traceback" not in err


def test_cli_arbitrary_runtime_error_returns_one_redacted_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "cli-manifest.yaml"
    manifest_path.write_text(MINIMAL.read_text(encoding="utf-8"), encoding="utf-8")

    class _RuntimeFailBrowser:
        closed = False

        def start(self, **kwargs: Any) -> None:
            del kwargs

        def close(self) -> None:
            self.closed = True

        def open_dashboard(self, url: str) -> None:
            del url
            raise RuntimeError(
                "unexpected Authorization: ApiKey secret and api_key=leaked during runtime",
            )

        def reset(self, url: str) -> None:
            del url

        def discover(self, control: ControlScenario) -> DiscoveredControl:
            del control
            raise RuntimeError("unused")

        def select(self, control: ControlScenario, option: str) -> None:
            del control, option

        def read_state(self, control: ControlScenario) -> ControlState:
            del control
            return ControlState()

        def capture(
            self,
            expected_panels: Sequence[str],
            cursor: CaptureCursor | None = None,
        ) -> BrowserObservation:
            del expected_panels, cursor
            return BrowserObservation(url="", accessibility_snapshot="", visible_text="", panels=())

        def begin_step(self) -> CaptureCursor:
            return CaptureCursor(0, 0)

        def settle(
            self,
            cursor: CaptureCursor,
            expected_panels: Sequence[str],
            *,
            policy: Any = None,
        ) -> BrowserObservation:
            del cursor, expected_panels, policy
            return BrowserObservation(url="", accessibility_snapshot="", visible_text="", panels=())

        def screenshot(self, path: str | Path) -> bool:
            del path
            return False

        def clear_evidence(self) -> None:
            return

    stub = _RuntimeFailBrowser()
    module = _load_cli_module(monkeypatch, stub)
    exit_code = module.main(
        [
            "--manifest",
            str(manifest_path),
            "--dashboard-url",
            "http://localhost:5601/app/dashboards#/view/test",
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "cli-run",
        ]
    )
    assert exit_code == 1
    assert stub.closed is True
    err = capsys.readouterr().err
    assert "ERROR:" in err
    assert "ApiKey secret" not in err
    assert "api_key=leaked" not in err
    assert "Authorization: [REDACTED]" in err
    assert "api_key: [REDACTED]" in err
    assert "Traceback" not in err


def test_validate_run_artifact_paths_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    with pytest.raises(ValueError, match=r"escapes|invalid"):
        validate_run_artifact_paths(root, "..", "run-1")

