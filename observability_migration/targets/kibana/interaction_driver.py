# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Playwright-backed Kibana dashboard control interaction driver.

Browser automation is isolated behind adapter classes and a lazy Playwright
import so modules remain importable without the optional ``browser`` extra.
"""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast, runtime_checkable

from observability_migration.targets.kibana.interaction_audit import (
    EvidenceParseError,
    NetworkEvidence,
    PanelEvidence,
    _is_esql_endpoint,
    enrich_esql_response,
    parse_esql_request,
)
from observability_migration.targets.kibana.interaction_scenarios import (
    ControlScenario,
    DiscoveredControl,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BrowserAdapterError(Exception):
    """Raised when browser interaction fails in a non-recoverable way."""


class ControlNotFound(BrowserAdapterError):
    """Raised when a control cannot be located unambiguously."""


class OptionNotFound(BrowserAdapterError):
    """Raised when a requested option is absent from the control UI."""


class SelectionDidNotStick(BrowserAdapterError):
    """Raised when a single selection attempt did not persist in the UI."""


class SettleTimeout(BrowserAdapterError):
    """Raised when dashboard interaction evidence did not settle in time."""

    def __init__(self, observation: BrowserObservation, reason: str) -> None:
        self.observation = observation
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Observation / state models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingRequest:
    panel_id: str
    endpoint: str
    opaque_id: str
    age_ms: int


@dataclass(frozen=True)
class SettlePolicy:
    timeout_seconds: float = 30.0
    poll_interval_ms: int = 100
    stable_polls: int = 3


@dataclass(frozen=True)
class CaptureCursor:
    network_index: int
    console_index: int


@dataclass(frozen=True)
class ControlState:
    """Adapter-specific control state beyond ``DiscoveredControl``."""

    selected_count: int = 0
    incompatible_warning: str = ""
    low_value: str = ""
    high_value: str = ""
    bounds: tuple[str, str] = ("", "")


@dataclass(frozen=True)
class BrowserObservation:
    """Minimal immutable browser snapshot for interaction auditing."""

    url: str
    accessibility_snapshot: str
    visible_text: str
    selected_state: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    network: tuple[NetworkEvidence, ...] = ()
    panels: tuple[PanelEvidence, ...] = ()
    console_errors: tuple[str, ...] = ()
    pending_requests: tuple[PendingRequest, ...] = ()


# ---------------------------------------------------------------------------
# Capture bounds and helpers
# ---------------------------------------------------------------------------

_MAX_BOUND_TEXT = 2048
_MAX_CONSOLE_MESSAGE = 2048
_MAX_PAGE_CAPTURE_TEXT = 32 * 1024
_COLLECTOR_DETACHED_ERROR = "collector detached before response"
_LOADING_TEXT_RE = re.compile(r"\b(loading|updating)\b", re.IGNORECASE)
_LOADING_TEST_SUBJECTS = frozenset(
    {
        "euiLoadingChart",
        "lnsEmbeddablePanelLoadingIndicator",
        "kbnLoadingMessage",
        "embPanelLoadingIndicator",
    }
)


def _bound_text(value: str, *, limit: int = _MAX_BOUND_TEXT) -> str:
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit]


def _bound_page_capture(value: str) -> str:
    """Bound full-page capture fields (``visible_text``, ``aria_snapshot``) at 32 KiB."""
    text = str(value or "")
    if len(text) <= _MAX_PAGE_CAPTURE_TEXT:
        return text
    return text[:_MAX_PAGE_CAPTURE_TEXT]


def _clone_network_evidence(evidence: NetworkEvidence) -> NetworkEvidence:
    """Return an isolated copy so observation mutation cannot corrupt collector state."""
    return NetworkEvidence(
        endpoint=evidence.endpoint,
        method=evidence.method,
        status=evidence.status,
        url=evidence.url,
        query=evidence.query,
        headers=dict(evidence.headers),
        body=copy.deepcopy(evidence.body),
        panel_id=evidence.panel_id,
        panel_title=evidence.panel_title,
        opaque_id=evidence.opaque_id,
        params=copy.deepcopy(evidence.params),
        param_kinds=dict(evidence.param_kinds),
        response_columns=evidence.response_columns,
        row_count=evidence.row_count,
        error=evidence.error,
    )


def _validate_settle_policy(policy: SettlePolicy) -> None:
    if policy.timeout_seconds <= 0 or policy.poll_interval_ms <= 0 or policy.stable_polls <= 0:
        raise BrowserAdapterError("invalid settle policy")


def _request_body(request: Any) -> Mapping[str, object] | None:
    body = getattr(request, "post_data_json", None)
    if body is None:
        raw = getattr(request, "post_data", None)
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
            if isinstance(parsed, Mapping):
                body = parsed
    if isinstance(body, Mapping):
        normalized = dict(body)
        nested = normalized.get("params")
        if isinstance(nested, Mapping) and "query" in nested:
            return dict(nested)
        return normalized
    return None


def _request_headers(request: Any) -> dict[str, str]:
    headers = getattr(request, "headers", None)
    if isinstance(headers, Mapping):
        return {str(key): str(value) for key, value in headers.items()}
    return {}


def _panel_container_for_capture(page: PageLike, panel_id: str) -> tuple[LocatorLike | None, str]:
    identity_selectors = (
        f'[data-panel-id="{panel_id}"]',
        f'[data-test-embeddable-id="{panel_id}"]',
        f'[data-test-subj="dashboardPanel-{panel_id}"]',
    )
    for selector in identity_selectors:
        matches = page.locator(selector)
        count = matches.count()
        if count == 1:
            return matches, ""
        if count > 1:
            return None, _bound_text(
                f"panel {panel_id!r}: ambiguous control ({count} matches for {selector})"
            )

    embeddable_panels = page.locator(_TEST_SUBJ_EMBEDDABLE_PANEL)
    matched: list[LocatorLike] = []
    for index in range(embeddable_panels.count()):
        candidate = embeddable_panels.nth(index)
        panel_attr = candidate.get_attribute("data-panel-id")
        embeddable_attr = candidate.get_attribute("data-test-embeddable-id")
        if panel_attr == panel_id or embeddable_attr == panel_id:
            matched.append(candidate)
    if len(matched) == 1:
        return matched[0], ""
    if len(matched) > 1:
        return None, _bound_text(
            f"panel {panel_id!r}: ambiguous embeddablePanel matches ({len(matched)})"
        )
    return None, ""


def _panel_title_from_container(container: LocatorLike, panel_id: str) -> str:
    headings = container.get_by_role("heading")
    if headings.count() >= 1:
        title = headings.nth(0).inner_text().strip()
        if title:
            return title
    labelled = container.get_attribute("aria-label")
    if labelled and labelled.strip():
        return labelled.strip()
    return panel_id


def _panel_has_loading_indicator(container: LocatorLike, text: str) -> bool:
    if _LOADING_TEXT_RE.search(text):
        return True
    for test_subj in _LOADING_TEST_SUBJECTS:
        if container.locator(f'[data-test-subj="{test_subj}"]').count() > 0:
            return True
    return False


def _capture_panel_evidence(page: PageLike, panel_id: str) -> PanelEvidence:
    container, ambiguity = _panel_container_for_capture(page, panel_id)
    if container is None:
        detail = ambiguity
        return PanelEvidence(
            panel_id=panel_id,
            title=panel_id,
            status="missing",
            detail=detail,
        )
    text = _bound_text(container.inner_text())
    title = _panel_title_from_container(container, panel_id)
    status = "loading" if _panel_has_loading_indicator(container, text) else "stable"
    return PanelEvidence(
        panel_id=panel_id,
        title=title,
        status=status,
        detail=text,
    )


def _panel_fingerprint(panels: Sequence[PanelEvidence]) -> tuple[tuple[str, str, str], ...]:
    return tuple((panel.panel_id, panel.status, panel.detail) for panel in panels)


def _network_fingerprint(
    network: Sequence[NetworkEvidence],
    pending: Sequence[PendingRequest],
) -> tuple[tuple[int, str, str], ...]:
    evidence_part = tuple((item.status, item.panel_id, item.opaque_id) for item in network)
    pending_part = tuple(
        (0, pending_item.panel_id, pending_item.opaque_id) for pending_item in pending
    )
    return evidence_part + pending_part


class _NetworkEventCollector:
    """Installs page listeners and accumulates bounded interaction evidence."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._network: list[NetworkEvidence] = []
        self._console_errors: list[str] = []
        self._pending: dict[int, tuple[int, float, str, str, str]] = {}
        self._page: Any | None = None
        self._handlers: dict[str, Any] = {}
        self._listeners_attached = False

    def attach(self, page: Any) -> None:
        if self._listeners_attached and self._page is page:
            return
        self.detach()
        self._page = page
        self._handlers = {
            "request": self._on_request,
            "response": self._on_response,
            "requestfailed": self._on_request_failed,
            "console": self._on_console,
        }
        for event_name, handler in self._handlers.items():
            page.on(event_name, handler)
        self._listeners_attached = True

    def detach(self) -> None:
        self._finalize_pending_requests(error=_COLLECTOR_DETACHED_ERROR)
        page = self._page
        if page is not None and hasattr(page, "off"):
            for event_name, handler in self._handlers.items():
                try:
                    page.off(event_name, handler)
                except (AttributeError, TypeError, ValueError):
                    pass
        self._page = None
        self._handlers = {}
        self._listeners_attached = False

    def _finalize_pending_requests(self, *, error: str) -> None:
        bounded_error = _bound_text(error, limit=_MAX_CONSOLE_MESSAGE)
        for _request_id, (index, _started, _panel_id, _endpoint, _opaque_id) in list(
            self._pending.items()
        ):
            self._network[index] = enrich_esql_response(
                self._network[index],
                status=-1,
                body={},
                error=bounded_error,
            )
        self._pending.clear()

    def clear(self) -> None:
        self._network.clear()
        self._console_errors.clear()
        self._pending.clear()

    def begin_step(self) -> CaptureCursor:
        return CaptureCursor(
            network_index=len(self._network),
            console_index=len(self._console_errors),
        )

    def _record_framework_error(self, message: str) -> None:
        self._console_errors.append(_bound_text(f"[framework] {message}", limit=_MAX_CONSOLE_MESSAGE))

    def _on_request(self, request: Any) -> None:
        try:
            method = str(getattr(request, "method", "") or "")
            url = str(getattr(request, "url", "") or "")
            if method.casefold() != "post" or not _is_esql_endpoint(url):
                return
            headers = _request_headers(request)
            body = _request_body(request)
            try:
                evidence = parse_esql_request(
                    url=url,
                    method=method,
                    headers=headers,
                    body=body,
                )
            except EvidenceParseError as exc:
                self._record_framework_error(str(exc))
                return
            if evidence is None:
                return
            if not evidence.panel_id:
                # Dashboard-control option queries use the same ES|QL endpoint
                # but have application context rather than a dashboard child.
                # They are not panel refresh evidence.
                return
            index = len(self._network)
            self._network.append(evidence)
            self._pending[id(request)] = (
                index,
                self._clock(),
                evidence.panel_id,
                evidence.endpoint,
                evidence.opaque_id,
            )
        except Exception as exc:
            self._record_framework_error(f"request listener failed: {exc}")

    def _on_response(self, response: Any) -> None:
        try:
            request = getattr(response, "request", None)
            if request is None:
                return
            pending = self._pending.get(id(request))
            if pending is None:
                return
            index, _started, _panel_id, _endpoint, _opaque_id = pending
            status = int(getattr(response, "status", 0) or 0)
            body: object = {}
            parse_error = ""
            try:
                body = response.json()
            except Exception as exc:
                parse_error = _bound_text(str(exc), limit=_MAX_CONSOLE_MESSAGE)
            evidence = self._network[index]
            enriched = enrich_esql_response(
                evidence,
                status=status,
                body=body,
                error=parse_error,
            )
            self._network[index] = enriched
            del self._pending[id(request)]
        except Exception as exc:
            self._record_framework_error(f"response listener failed: {exc}")

    def _on_request_failed(self, request: Any) -> None:
        try:
            pending = self._pending.get(id(request))
            if pending is None:
                return
            index, _started, _panel_id, _endpoint, _opaque_id = pending
            failure = getattr(request, "failure", None)
            error_text = ""
            if failure is not None:
                error_text = _bound_text(getattr(failure, "error_text", "") or str(failure))
            evidence = enrich_esql_response(
                self._network[index],
                status=-1,
                body={},
                error=error_text or "request failed",
            )
            self._network[index] = evidence
            del self._pending[id(request)]
        except Exception as exc:
            self._record_framework_error(f"requestfailed listener failed: {exc}")

    def _on_console(self, message: Any) -> None:
        try:
            message_type = str(getattr(message, "type", "") or "")
            if message_type.casefold() != "error":
                return
            text = _bound_text(str(getattr(message, "text", "") or ""), limit=_MAX_CONSOLE_MESSAGE)
            if text.startswith(
                "Failed to load resource: the server responded with a status of"
            ):
                return
            if text.startswith(
                "Executing inline script violates the following Content Security Policy"
            ):
                return
            if text:
                self._console_errors.append(text)
        except Exception as exc:
            self._record_framework_error(f"console listener failed: {exc}")

    def _pending_requests_since(
        self,
        cursor: CaptureCursor | None = None,
    ) -> tuple[PendingRequest, ...]:
        now = self._clock()
        start_index = 0 if cursor is None else cursor.network_index
        pending: list[tuple[int, PendingRequest]] = []
        for _request_id, (index, started, panel_id, endpoint, opaque_id) in self._pending.items():
            if index < start_index:
                continue
            age_ms = max(0, int((now - started) * 1000))
            pending.append(
                (
                    index,
                    PendingRequest(
                        panel_id=panel_id,
                        endpoint=endpoint,
                        opaque_id=opaque_id,
                        age_ms=age_ms,
                    ),
                )
            )
        pending.sort(key=lambda item: item[0])
        return tuple(item for _, item in pending)

    def _pending_requests(self) -> tuple[PendingRequest, ...]:
        return self._pending_requests_since(None)

    def network_since(self, cursor: CaptureCursor | None) -> tuple[NetworkEvidence, ...]:
        start = 0 if cursor is None else cursor.network_index
        return tuple(_clone_network_evidence(item) for item in self._network[start:])

    def console_since(self, cursor: CaptureCursor | None) -> tuple[str, ...]:
        start = 0 if cursor is None else cursor.console_index
        return tuple(self._console_errors[start:])

    def _panels_terminal_for_expected(
        self,
        *,
        cursor: CaptureCursor,
        expected_panels: Collection[str],
    ) -> dict[str, bool]:
        network = self.network_since(cursor)
        pending_panels = {
            item.panel_id
            for item in self._pending_requests_since(cursor)
            if item.panel_id
        }
        terminal_by_panel: dict[str, bool] = {}
        for panel_id in expected_panels:
            has_terminal = any(
                item.panel_id == panel_id and item.status != 0 for item in network
            )
            terminal_by_panel[panel_id] = has_terminal and panel_id not in pending_panels
        return terminal_by_panel

    def _all_network_terminal_since(self, cursor: CaptureCursor) -> bool:
        network = self.network_since(cursor)
        pending = self._pending_requests_since(cursor)
        if not network and not pending:
            return True
        if pending:
            return False
        return all(item.status != 0 for item in network)

    def _settle_blockers(
        self,
        *,
        cursor: CaptureCursor,
        expected_panels: Collection[str],
        panels: Sequence[PanelEvidence],
    ) -> list[str]:
        reasons: list[str] = []
        if expected_panels:
            terminal = self._panels_terminal_for_expected(
                cursor=cursor,
                expected_panels=expected_panels,
            )
            for panel_id in expected_panels:
                if not terminal.get(panel_id, False):
                    reasons.append(f"panel {panel_id}: ES|QL request not terminal")
        elif not self._all_network_terminal_since(cursor):
            reasons.append("observed ES|QL requests still pending")

        panels_by_id = {panel.panel_id: panel for panel in panels}
        for panel_id in expected_panels:
            panel = panels_by_id.get(panel_id)
            if panel is None:
                reasons.append(f"panel {panel_id}: snapshot missing")
            elif panel.status != "stable":
                reasons.append(f"panel {panel_id}: status {panel.status}")
        pending = self._pending_requests_since(cursor)
        if pending:
            for item in pending:
                reasons.append(
                    f"pending request for panel {item.panel_id or 'unknown'} "
                    f"({item.endpoint})"
                )
        return reasons


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class PageLike(Protocol):
    def goto(self, url: str, *, wait_until: str | None = ...) -> Any: ...

    def get_by_role(
        self,
        role: str,
        *,
        name: str | None = ...,
        exact: bool = ...,
    ) -> LocatorLike: ...

    def locator(self, selector: str) -> LocatorLike: ...

    @property
    def url(self) -> str: ...

    def screenshot(self, *, path: str, full_page: bool = ...) -> bytes: ...


@runtime_checkable
class LocatorLike(Protocol):
    def count(self) -> int: ...

    def is_visible(self) -> bool: ...

    def wait_for(self, **kwargs: Any) -> None: ...

    def click(self, **kwargs: Any) -> None: ...

    def fill(self, value: str, **kwargs: Any) -> None: ...

    def press(self, key: str, **kwargs: Any) -> None: ...

    def input_value(self) -> str: ...

    def inner_text(self) -> str: ...

    def all_inner_texts(self) -> list[str]: ...

    def all_text_contents(self) -> list[str]: ...

    def get_attribute(self, name: str) -> str | None: ...

    def nth(self, index: int) -> LocatorLike: ...

    def filter(self, *, has_text: str | None = ...) -> LocatorLike: ...

    def get_by_role(
        self,
        role: str,
        *,
        name: str | None = ...,
        exact: bool = ...,
    ) -> LocatorLike: ...

    def locator(self, selector: str) -> LocatorLike: ...

    def evaluate(self, expression: str, arg: Any = None) -> Any: ...

    def aria_snapshot(self) -> str: ...


class ControlAdapter(Protocol):
    def discover(self, control: ControlScenario) -> DiscoveredControl: ...

    def select(self, control: ControlScenario, option: str) -> None: ...

    def read_state(self) -> ControlState: ...


@runtime_checkable
class BrowserAdapter(Protocol):
    def open_dashboard(self, url: str) -> None: ...

    def reset(self, url: str) -> None: ...

    def discover(self, control: ControlScenario) -> DiscoveredControl: ...

    def select(self, control: ControlScenario, option: str) -> None: ...

    def capture(
        self,
        expected_panels: Collection[str],
        cursor: CaptureCursor | None = None,
    ) -> BrowserObservation: ...

    def begin_step(self) -> CaptureCursor: ...

    def settle(
        self,
        cursor: CaptureCursor,
        expected_panels: Collection[str],
        *,
        policy: SettlePolicy | None = None,
    ) -> BrowserObservation: ...

    def read_state(self, control: ControlScenario) -> ControlState: ...

    def read_selected(self, control: ControlScenario) -> tuple[str, ...]: ...

    def clear_evidence(self) -> None: ...

    def screenshot(self, path: str | Path) -> bool: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Selector constants
# ---------------------------------------------------------------------------

_VIEWPORT_WIDTH = 1600
_VIEWPORT_HEIGHT = 1200

_TEST_SUBJ_QUERY_INPUT = '[data-test-subj="queryInput"]'
_TEST_SUBJ_QUERY_SUBMIT = '[data-test-subj="querySubmitButton"]'
_TEST_SUBJ_ADD_FILTER = '[data-test-subj="addFilter"]'
_TEST_SUBJ_FILTER_FIELD = '[data-test-subj="filterFieldSuggestionList"]'
_TEST_SUBJ_FILTER_VALUE = '[data-test-subj="filterValueInput"]'
_TEST_SUBJ_SAVE_FILTER = '[data-test-subj="saveFilterButton"]'
_TEST_SUBJ_FILTER_BADGE = '[data-test-subj="filterBadge"]'
_TEST_SUBJ_DATE_PICKER = '[data-test-subj="superDatePickerShowDatesButton"]'
_TEST_SUBJ_DATE_PICKER_TEXT = '[data-test-subj="superDatePickerstartDatePopoverButton"]'
_TEST_SUBJ_OPTIONS_LIST_APPLY = '[data-test-subj="optionsListControlApplyButton"]'
_TEST_SUBJ_PANEL_ACTIONS = '[data-test-subj="embeddablePanelAction-togglePanelActionMenu"]'
_TEST_SUBJ_PANEL_FILTER_ACTION = '[data-test-subj="embeddablePanelAction-addPanelFilter"]'
_TEST_SUBJ_CONTEXT_MENU = '[data-test-subj="euiContextMenuPanel"]'
_TEST_SUBJ_COMBOBOX_POPOVER = '[data-test-subj="comboBoxOptionsList"]'
_TEST_SUBJ_INCOMPATIBLE_WARNING = '[data-test-subj="esqlControlsIncompatibleSelectionsWarning"]'
_TEST_SUBJ_EMBEDDABLE_PANEL = '[data-test-subj="embeddablePanel"]'
_TEST_SUBJ_CONTROL_FRAME = '[data-test-subj="control-frame"]'

_ROLE_COMBOBOX = "combobox"
_ROLE_LISTBOX = "listbox"
_ROLE_OPTION = "option"
_ROLE_SLIDER = "slider"
_ROLE_SEARCHBOX = "searchbox"
_ROLE_BUTTON = "button"
_ROLE_GROUP = "group"
_ROLE_MENU = "menu"
_ROLE_TEXTBOX = "textbox"

_INCOMPATIBLE_SELECTIONS_RE = re.compile(
    r"Incompatible selections\s*\((\d+)\)",
    re.IGNORECASE,
)
_RANGE_SELECTION_RE = re.compile(r"^\s*(.+?)\s*\.\.\s*(.+?)\s*$")

_ESQL_ADAPTERS = frozenset(
    {
        "esql_value",
        "esql_field",
        "esql_function",
        "esql_interval",
    }
)

_SUPPORTED_ADAPTERS = frozenset(
    {
        *_ESQL_ADAPTERS,
        "options_list",
        "range_slider",
        "query_bar",
        "filter_pill",
        "time_range",
        "panel_filter",
    }
)


# ---------------------------------------------------------------------------
# Locator helpers
# ---------------------------------------------------------------------------


def _require_exactly_one(locator: LocatorLike, *, description: str) -> LocatorLike:
    count = locator.count()
    if count == 0:
        raise ControlNotFound(f"{description}: control not found")
    if count > 1:
        raise ControlNotFound(f"{description}: ambiguous control ({count} matches)")
    return locator


def _combobox_by_label(page: PageLike, label: str) -> LocatorLike:
    by_role = page.get_by_role(_ROLE_COMBOBOX, name=label, exact=True)
    if by_role.count() == 0:
        wait_for = getattr(by_role, "wait_for", None)
        if callable(wait_for):
            try:
                wait_for(state="visible", timeout=60_000)
            except Exception:
                pass
    count = by_role.count()
    if count == 1:
        return by_role
    if count > 1:
        raise ControlNotFound(
            f"combobox {label!r}: ambiguous control ({count} matches)"
        )
    by_button = page.get_by_role(_ROLE_BUTTON, name=label, exact=True)
    if by_button.count() == 0:
        wait_for = getattr(by_button, "wait_for", None)
        if callable(wait_for):
            try:
                wait_for(state="visible", timeout=60_000)
            except Exception:
                pass
    if by_button.count() == 1:
        return by_button
    if by_button.count() > 1:
        raise ControlNotFound(
            f"control button {label!r}: ambiguous control ({by_button.count()} matches)"
        )
    by_test_subj = page.locator(f'[data-test-subj="{label}"]')
    return _require_exactly_one(by_test_subj, description=f"combobox {label!r}")


def _searchbox(page: PageLike) -> LocatorLike:
    by_test_subj = page.locator(_TEST_SUBJ_QUERY_INPUT)
    count = by_test_subj.count()
    if count == 1:
        return by_test_subj
    if count > 1:
        raise ControlNotFound(f"query bar: ambiguous control ({count} matches)")
    by_role = page.get_by_role(_ROLE_SEARCHBOX)
    return _require_exactly_one(by_role, description="query bar")


def _scoped_options_container(
    page: PageLike,
    *,
    label: str = "",
) -> LocatorLike:
    if label:
        named = page.get_by_role(
            _ROLE_LISTBOX,
            name=f"Available options for {label}",
            exact=True,
        )
        visible_named = [
            named.nth(index)
            for index in range(named.count())
            if named.nth(index).is_visible()
        ]
        if visible_named:
            # Kibana 9.5 can leave prior EUI listboxes mounted after a control
            # closes. The newest matching listbox belongs to the latest trigger.
            return visible_named[-1]

    popover = page.locator(_TEST_SUBJ_COMBOBOX_POPOVER)
    visible_popovers = [
        popover.nth(index)
        for index in range(popover.count())
        if popover.nth(index).is_visible()
    ]
    popover_count = len(visible_popovers)
    if popover_count > 1:
        raise ControlNotFound(
            f"options popover: ambiguous control ({popover_count} matches)"
        )
    if popover_count == 1:
        visible_popover = visible_popovers[0]
        listbox = visible_popover.get_by_role(_ROLE_LISTBOX)
        if listbox.count() == 1:
            return listbox
        return visible_popover
    listboxes = page.get_by_role(_ROLE_LISTBOX)
    visible_listboxes = [
        listboxes.nth(index)
        for index in range(listboxes.count())
        if listboxes.nth(index).is_visible()
    ]
    listbox_count = len(visible_listboxes)
    if listbox_count == 0:
        raise ControlNotFound("options listbox: control not found")
    if listbox_count > 1:
        raise ControlNotFound(
            f"options listbox: ambiguous control ({listbox_count} matches)"
        )
    return visible_listboxes[0]


def _read_option_texts(page: PageLike, *, label: str = "") -> tuple[str, ...]:
    container = _scoped_options_container(page, label=label)
    options = container.get_by_role(_ROLE_OPTION)
    if options.count() == 0:
        texts = container.all_text_contents()
        return tuple(
            cleaned
            for text in texts
            if (cleaned := _clean_option_text(text))
        )
    return tuple(
        cleaned
        for text in options.all_inner_texts()
        if (cleaned := _clean_option_text(text))
    )


def _clean_option_text(text: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = line.strip()
        if cleaned and not cleaned.startswith("."):
            return cleaned
    return ""


def _open_options_container(
    page: PageLike,
    combobox: LocatorLike,
    *,
    label: str,
) -> LocatorLike:
    combobox.click()
    return _scoped_options_container(page, label=label)


def _combobox_is_multiselect(combobox: LocatorLike) -> bool:
    if combobox.get_attribute("aria-multiselectable") == "true":
        return True
    return combobox.get_attribute("data-multiselect") == "true"


def _combobox_selected_option_texts(combobox: LocatorLike) -> tuple[str, ...]:
    data_selected = combobox.get_attribute("data-selected-options")
    if data_selected:
        if _combobox_is_multiselect(combobox):
            return tuple(
                part.strip() for part in data_selected.split(",") if part.strip()
            )
        return (data_selected.strip(),)
    try:
        input_value = combobox.input_value().strip()
    except Exception:
        input_value = ""
    if input_value:
        return (input_value,)
    visible = combobox.inner_text().strip()
    if visible:
        values = [
            line.strip()
            for line in visible.splitlines()
            if line.strip() and not line.strip().isdigit()
        ]
        if not values:
            return ()
        if len(values) == 1 and "," in values[0]:
            return tuple(part.strip() for part in values[0].split(",") if part.strip())
        return tuple(values)
    return ()


def _read_selected_option_texts(
    page: PageLike,
    *,
    combobox: LocatorLike,
) -> tuple[str, ...]:
    try:
        container = _scoped_options_container(
            page,
            label=combobox.get_attribute("aria-label") or "",
        )
    except ControlNotFound:
        return _combobox_selected_option_texts(combobox)

    if container.count() == 1:
        options = container.get_by_role(_ROLE_OPTION)
        selected: list[str] = []
        for index in range(options.count()):
            option = options.nth(index)
            if (
                option.get_attribute("aria-selected") == "true"
                or option.get_attribute("aria-checked") == "true"
            ):
                cleaned = _clean_option_text(option.inner_text())
                if cleaned:
                    selected.append(cleaned)
        if selected:
            return tuple(selected)
    return _combobox_selected_option_texts(combobox)


def _option_locator(
    page: PageLike,
    option_text: str,
    *,
    label: str = "",
) -> LocatorLike:
    scoped = _scoped_options_container(page, label=label)
    option = scoped.get_by_role(_ROLE_OPTION, name=option_text, exact=True)
    if option.count() == 1:
        return option
    options = scoped.get_by_role(_ROLE_OPTION)
    matches = [
        options.nth(index)
        for index in range(options.count())
        if _clean_option_text(options.nth(index).inner_text()) == option_text
    ]
    if len(matches) == 1:
        return matches[0]
    return _require_exactly_one(option, description=f"option {option_text!r}")


def _close_open_popover(
    page: PageLike,
    *,
    trigger: LocatorLike | None = None,
) -> None:
    del page, trigger


def _read_incompatible_warning(page: PageLike) -> str:
    warning = page.locator(_TEST_SUBJ_INCOMPATIBLE_WARNING)
    if warning.count() == 1:
        return str(warning.inner_text()).strip()
    body = page.locator("body")
    if body.count() == 1:
        match = _INCOMPATIBLE_SELECTIONS_RE.search(str(body.inner_text()))
        if match:
            return match.group(0)
    return ""


def _selected_count(page: PageLike, *, combobox: LocatorLike) -> int:
    selected = _read_selected_option_texts(page, combobox=combobox)
    warning = _read_incompatible_warning(page)
    if warning:
        match = _INCOMPATIBLE_SELECTIONS_RE.search(warning)
        if match:
            return max(len(selected), int(match.group(1)))
    return len(selected)


def _wait_for_expected_selected(
    page: PageLike,
    combobox: LocatorLike,
    expected: Collection[str],
    *,
    timeout_ms: int = 3_000,
) -> set[str]:
    deadline = time.monotonic() + timeout_ms / 1000
    expected_set = set(expected)
    selected: set[str] = set()
    while time.monotonic() < deadline:
        selected = set(
            _read_selected_option_texts(page, combobox=combobox)
        )
        if expected_set <= selected:
            return selected
        page.wait_for_timeout(100)
    return selected


def _split_field_value(selection: str) -> tuple[str, str]:
    if "=" not in selection:
        raise BrowserAdapterError(
            f"filter selection must be field=value, got {selection!r}"
        )
    field, value = selection.split("=", 1)
    if not field.strip() or not value.strip():
        raise BrowserAdapterError(
            f"filter selection must be field=value, got {selection!r}"
        )
    return field.strip(), value.strip()


def _split_panel_filter(selection: str) -> tuple[str, str, str]:
    parts = selection.split("|")
    if len(parts) != 3:
        raise BrowserAdapterError(
            "panel filter selection must be panel_id|field|value, "
            f"got {selection!r}"
        )
    panel_id, field, value = (part.strip() for part in parts)
    if not panel_id or not field or not value:
        raise BrowserAdapterError(
            "panel filter selection must be panel_id|field|value, "
            f"got {selection!r}"
        )
    return panel_id, field, value


def _parse_range_selection(selection: str) -> tuple[str, str]:
    match = _RANGE_SELECTION_RE.match(selection)
    if match is None:
        raise BrowserAdapterError(
            f"range selection must be low..high, got {selection!r}"
        )
    low, high = match.group(1).strip(), match.group(2).strip()
    if not low or not high:
        raise BrowserAdapterError(
            f"range selection must be low..high, got {selection!r}"
        )
    return low, high


def _panel_container(page: PageLike, panel_id: str) -> LocatorLike:
    identity_selectors = (
        f'[data-panel-id="{panel_id}"]',
        f'[data-test-embeddable-id="{panel_id}"]',
        f'[data-test-subj="dashboardPanel-{panel_id}"]',
    )
    for selector in identity_selectors:
        matches = page.locator(selector)
        count = matches.count()
        if count == 1:
            return matches
        if count > 1:
            raise ControlNotFound(
                f"panel {panel_id!r}: ambiguous control ({count} matches for {selector})"
            )

    embeddable_panels = page.locator(_TEST_SUBJ_EMBEDDABLE_PANEL)
    matched: list[LocatorLike] = []
    for index in range(embeddable_panels.count()):
        candidate = embeddable_panels.nth(index)
        panel_attr = candidate.get_attribute("data-panel-id")
        embeddable_attr = candidate.get_attribute("data-test-embeddable-id")
        if panel_attr == panel_id or embeddable_attr == panel_id:
            matched.append(candidate)
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        raise ControlNotFound(
            f"panel {panel_id!r}: ambiguous embeddablePanel matches ({len(matched)})"
        )
    raise ControlNotFound(f"panel {panel_id!r}: control not found")


def _visible_action_menu(page: PageLike) -> LocatorLike:
    by_test_subj = page.locator(_TEST_SUBJ_CONTEXT_MENU)
    count = by_test_subj.count()
    if count == 1:
        return by_test_subj
    if count > 1:
        raise ControlNotFound(
            f"panel action menu: ambiguous control ({count} matches)"
        )
    by_role = page.get_by_role(_ROLE_MENU)
    return _require_exactly_one(by_role, description="panel action menu")


def _panel_filter_action(menu: LocatorLike) -> LocatorLike:
    by_test_subj = menu.locator(_TEST_SUBJ_PANEL_FILTER_ACTION)
    if by_test_subj.count() == 1:
        return by_test_subj
    if by_test_subj.count() > 1:
        raise ControlNotFound(
            f"panel filter action: ambiguous control ({by_test_subj.count()} matches)"
        )
    by_role = menu.get_by_role(
        _ROLE_BUTTON,
        name="Create filter",
        exact=True,
    )
    return _require_exactly_one(by_role, description="panel filter action")


def _filter_field_input(page: PageLike) -> LocatorLike:
    by_test_subj = page.locator(_TEST_SUBJ_FILTER_FIELD)
    count = by_test_subj.count()
    if count == 1:
        return by_test_subj
    if count > 1:
        raise ControlNotFound(f"filter field: ambiguous control ({count} matches)")
    by_role = page.get_by_role(_ROLE_COMBOBOX)
    return _require_exactly_one(by_role, description="filter field")


def _filter_value_input(page: PageLike) -> LocatorLike:
    by_test_subj = page.locator(_TEST_SUBJ_FILTER_VALUE)
    if by_test_subj.count() == 1:
        return by_test_subj
    if by_test_subj.count() > 1:
        raise ControlNotFound(
            f"filter value: ambiguous control ({by_test_subj.count()} matches)"
        )
    by_role = page.get_by_role(_ROLE_TEXTBOX)
    return _require_exactly_one(by_role, description="filter value")


def _select_exact_field_suggestion_if_present(page: PageLike, field: str) -> None:
    listboxes = page.get_by_role(_ROLE_LISTBOX)
    count = listboxes.count()
    if count == 0:
        return
    if count > 1:
        raise ControlNotFound(
            f"filter field suggestions: ambiguous listbox ({count} matches)"
        )
    suggestion = listboxes.get_by_role(_ROLE_OPTION, name=field, exact=True)
    suggestion = _require_exactly_one(
        suggestion,
        description=f"field suggestion {field!r}",
    )
    suggestion.click()


def _fill_filter_editor(page: PageLike, field: str, value: str) -> None:
    field_input = _filter_field_input(page)
    field_input.fill(field)
    _select_exact_field_suggestion_if_present(page, field)
    value_input = _filter_value_input(page)
    value_input.fill(value)
    save = _require_exactly_one(
        page.locator(_TEST_SUBJ_SAVE_FILTER),
        description="save filter",
    )
    save.click()


def _verify_filter_pill(page: PageLike, field: str, value: str) -> None:
    badges = page.locator(_TEST_SUBJ_FILTER_BADGE)
    for index in range(badges.count()):
        badge = badges.nth(index)
        text = badge.inner_text()
        if field in text and value in text:
            return
    raise SelectionDidNotStick(
        f"filter pill for {field}={value!r} not visible after submit"
    )


def _set_range_handle_value(handle: LocatorLike, value: str) -> None:
    test_subj = handle.get_attribute("data-test-subj") or ""
    if test_subj in {
        "rangeSlider__lowerBoundFieldNumber",
        "rangeSlider__upperBoundFieldNumber",
    }:
        handle.fill(value)
        return
    try:
        handle.evaluate(
            """(element, nextValue) => {
                element.value = nextValue;
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )
    except (AttributeError, TypeError, BrowserAdapterError):
        handle.fill(value)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class EsqlControlAdapter:
    """Adapter for ES|QL dashboard controls (value/field/function/interval)."""

    def __init__(self, page: PageLike) -> None:
        self._page = page
        self._active_combobox: LocatorLike | None = None

    def read_state(self) -> ControlState:
        combobox = self._active_combobox
        selected_count = 0
        if combobox is not None:
            selected_count = _selected_count(self._page, combobox=combobox)
        return ControlState(
            selected_count=selected_count,
            incompatible_warning=_read_incompatible_warning(self._page),
        )

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        combobox = _combobox_by_label(self._page, control.label)
        self._active_combobox = combobox
        _open_options_container(
            self._page,
            combobox,
            label=control.label,
        )
        options = _read_option_texts(self._page, label=control.label)
        selected = _read_selected_option_texts(
            self._page,
            combobox=combobox,
        )
        _close_open_popover(self._page, trigger=combobox)
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=options,
            selected=selected,
        )

    def select(self, control: ControlScenario, option: str) -> None:
        combobox = _combobox_by_label(self._page, control.label)
        self._active_combobox = combobox
        container = _open_options_container(
            self._page,
            combobox,
            label=control.label,
        )
        multiselect = (
            container.get_attribute("aria-multiselectable") == "true"
            or _combobox_is_multiselect(combobox)
        )
        expected = (
            tuple(part.strip() for part in option.split(",") if part.strip())
            if multiselect and "," in option
            else (option,)
        )
        available = set(_read_option_texts(self._page, label=control.label))
        for part in expected:
            if part not in available:
                raise OptionNotFound(
                    f"option {part!r} not found for control {control.label!r}"
                )
        selected_before = set(
            _read_selected_option_texts(self._page, combobox=combobox)
        )
        if multiselect:
            expected_set = set(expected)
            if len(expected) == 1 and selected_before != expected_set:
                _option_locator(
                    self._page,
                    expected[0],
                    label=control.label,
                ).click()
            elif len(expected) > 1 and selected_before != expected_set:
                for part in expected:
                    if part not in selected_before:
                        _option_locator(
                            self._page,
                            part,
                            label=control.label,
                        ).click()
        else:
            _option_locator(
                self._page,
                option,
                label=control.label,
            ).click()
        selected = _wait_for_expected_selected(
            self._page,
            combobox,
            expected,
        )
        _close_open_popover(self._page, trigger=combobox)
        missing = [part for part in expected if part not in selected]
        if missing:
            raise SelectionDidNotStick(
                f"option(s) {missing!r} did not stick for control {control.label!r}"
            )


class OptionsListAdapter:
    """Adapter for Kibana options-list dashboard controls."""

    def __init__(self, page: PageLike) -> None:
        self._page = page
        self._active_combobox: LocatorLike | None = None

    def read_state(self) -> ControlState:
        combobox = self._active_combobox
        selected_count = 0
        if combobox is not None:
            selected_count = _selected_count(self._page, combobox=combobox)
        return ControlState(selected_count=selected_count)

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        combobox = _combobox_by_label(self._page, control.label)
        self._active_combobox = combobox
        _open_options_container(
            self._page,
            combobox,
            label=control.label,
        )
        options = _read_option_texts(self._page, label=control.label)
        selected = _read_selected_option_texts(self._page, combobox=combobox)
        _close_open_popover(self._page, trigger=combobox)
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=options,
            selected=selected,
        )

    def _is_multiselect(self, combobox: LocatorLike) -> bool:
        return _combobox_is_multiselect(combobox)

    def _expand_options(self, option: str, *, multiselect: bool) -> tuple[str, ...]:
        if multiselect and "," in option:
            return tuple(part.strip() for part in option.split(",") if part.strip())
        return (option,)

    def select(self, control: ControlScenario, option: str) -> None:
        combobox = _combobox_by_label(self._page, control.label)
        self._active_combobox = combobox
        container = _open_options_container(
            self._page,
            combobox,
            label=control.label,
        )
        multiselect = (
            container.get_attribute("aria-multiselectable") == "true"
            or self._is_multiselect(combobox)
        )
        expected = self._expand_options(option, multiselect=multiselect)
        available = set(_read_option_texts(self._page, label=control.label))
        for part in expected:
            if part not in available:
                raise OptionNotFound(
                    f"option {part!r} not found for control {control.label!r}"
                )
        selected_before = set(
            _read_selected_option_texts(self._page, combobox=combobox)
        )
        if multiselect:
            for part in sorted(selected_before - set(expected)):
                _option_locator(
                    self._page,
                    part,
                    label=control.label,
                ).click()
            for part in expected:
                if part not in selected_before:
                    _option_locator(
                        self._page,
                        part,
                        label=control.label,
                    ).click()
        else:
            _option_locator(
                self._page,
                option,
                label=control.label,
            ).click()

        selected_in_popover = _wait_for_expected_selected(
            self._page,
            combobox,
            expected,
        )
        apply = self._page.locator(_TEST_SUBJ_OPTIONS_LIST_APPLY)
        if apply.count() == 1:
            apply.click()

        _close_open_popover(self._page, trigger=combobox)
        selected = selected_in_popover | set(
            _read_selected_option_texts(self._page, combobox=combobox)
        )
        missing = [part for part in expected if part not in selected]
        if missing:
            raise SelectionDidNotStick(
                f"options {missing!r} did not stick for control {control.label!r}"
            )


class RangeSliderAdapter:
    """Adapter for numeric range-slider dashboard controls."""

    def __init__(self, page: PageLike) -> None:
        self._page = page
        self._last_low = ""
        self._last_high = ""

    def read_state(self) -> ControlState:
        return ControlState(
            low_value=self._last_low,
            high_value=self._last_high,
        )

    def _control_group(self, label: str) -> LocatorLike:
        by_role = self._page.get_by_role(_ROLE_GROUP, name=label, exact=True)
        if by_role.count() == 1:
            return by_role
        by_label = self._page.locator(f'[aria-label="{label}"]')
        return _require_exactly_one(by_label, description=f"range slider {label!r}")

    def _handles(self, label: str) -> tuple[LocatorLike, LocatorLike]:
        try:
            group = self._control_group(label)
        except ControlNotFound:
            lower = self._page.locator(
                '[data-test-subj="rangeSlider__lowerBoundFieldNumber"]'
            )
            upper = self._page.locator(
                '[data-test-subj="rangeSlider__upperBoundFieldNumber"]'
            )
            if lower.count() == 1 and upper.count() == 1:
                return lower, upper
            raise
        sliders = group.get_by_role(_ROLE_SLIDER)
        if sliders.count() != 2:
            raise ControlNotFound(
                f"range slider {label!r} requires exactly two handles, "
                f"found {sliders.count()}"
            )
        return sliders.nth(0), sliders.nth(1)

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        low_handle, high_handle = self._handles(control.label)
        options: list[str] = []
        low_bound = low_handle.get_attribute("aria-valuemin") or ""
        high_bound = high_handle.get_attribute("aria-valuemax") or ""
        if low_bound and high_bound:
            mid = str((float(low_bound) + float(high_bound)) / 2)
            options.extend([low_bound, mid, high_bound])
        current = (low_handle.input_value(), high_handle.input_value())
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=tuple(dict.fromkeys(options)),
            selected=current,
        )

    def select(self, control: ControlScenario, option: str) -> None:
        low, high = _parse_range_selection(option)
        low_handle, high_handle = self._handles(control.label)
        _set_range_handle_value(low_handle, low)
        _set_range_handle_value(high_handle, high)
        actual_low = low_handle.input_value()
        actual_high = high_handle.input_value()
        self._last_low = actual_low
        self._last_high = actual_high
        if actual_low != low or actual_high != high:
            raise SelectionDidNotStick(
                f"range {low!r}..{high!r} did not stick for control {control.label!r}"
            )


class QueryBarAdapter:
    """Adapter for the global Kibana query bar."""

    def __init__(self, page: PageLike) -> None:
        self._page = page
        self._last_query = ""

    def read_state(self) -> ControlState:
        return ControlState()

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        searchbox = _searchbox(self._page)
        current = searchbox.input_value().strip()
        selected = (current,) if current else ()
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=selected,
            selected=selected,
        )

    def select(self, control: ControlScenario, option: str) -> None:
        searchbox = _searchbox(self._page)
        searchbox.fill(option)
        submit = self._page.locator(_TEST_SUBJ_QUERY_SUBMIT)
        if submit.count() == 1:
            submit.click()
        else:
            searchbox.press("Enter")
        actual = searchbox.input_value()
        self._last_query = actual
        if actual != option:
            raise SelectionDidNotStick(
                f"query {option!r} did not stick in query bar"
            )


class FilterPillAdapter:
    """Adapter for adding a global filter pill via the filter editor."""

    def __init__(self, page: PageLike) -> None:
        self._page = page

    def read_state(self) -> ControlState:
        return ControlState()

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=(),
            selected=(),
        )

    def select(self, control: ControlScenario, option: str) -> None:
        field, value = _split_field_value(option)
        add_filter = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_ADD_FILTER),
            description="add filter",
        )
        add_filter.click()
        _fill_filter_editor(self._page, field, value)
        _verify_filter_pill(self._page, field, value)


class TimeRangeAdapter:
    """Adapter for the global Kibana time-range picker."""

    def __init__(self, page: PageLike) -> None:
        self._page = page

    def read_state(self) -> ControlState:
        return ControlState()

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        picker = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_DATE_PICKER_TEXT),
            description="time range display",
        )
        current = picker.inner_text().strip()
        selected = (current,) if current else ()
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=selected,
            selected=selected,
        )

    def select(self, control: ControlScenario, option: str) -> None:
        opener = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_DATE_PICKER),
            description="time range picker",
        )
        opener.click()
        quick_option = self._page.get_by_role(_ROLE_OPTION, name=option, exact=True)
        if quick_option.count() != 1:
            quick_option = self._page.get_by_role(
                _ROLE_BUTTON,
                name=option,
                exact=True,
            )
        quick_option = _require_exactly_one(
            quick_option,
            description=f"time range option {option!r}",
        )
        quick_option.click()
        display = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_DATE_PICKER_TEXT),
            description="time range display",
        )
        if option not in display.inner_text():
            raise SelectionDidNotStick(
                f"time range {option!r} did not stick in global picker"
            )


class PanelFilterAdapter:
    """Adapter for creating a filter from a panel action menu."""

    def __init__(self, page: PageLike) -> None:
        self._page = page

    def read_state(self) -> ControlState:
        return ControlState()

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=(),
            selected=(),
        )

    def select(self, control: ControlScenario, option: str) -> None:
        panel_id, field, value = _split_panel_filter(option)
        panel = _panel_container(self._page, panel_id)
        actions = _require_exactly_one(
            panel.locator(_TEST_SUBJ_PANEL_ACTIONS),
            description=f"panel actions for {panel_id!r}",
        )
        actions.click()
        menu = _visible_action_menu(self._page)
        filter_action = _panel_filter_action(menu)
        filter_action.click()
        _fill_filter_editor(self._page, field, value)
        _verify_filter_pill(self._page, field, value)


def _adapter_for(page: PageLike, adapter_name: str) -> ControlAdapter:
    if adapter_name not in _SUPPORTED_ADAPTERS:
        raise BrowserAdapterError(f"unsupported adapter: {adapter_name!r}")
    if adapter_name in _ESQL_ADAPTERS:
        return EsqlControlAdapter(page)
    if adapter_name == "options_list":
        return OptionsListAdapter(page)
    if adapter_name == "range_slider":
        return RangeSliderAdapter(page)
    if adapter_name == "query_bar":
        return QueryBarAdapter(page)
    if adapter_name == "filter_pill":
        return FilterPillAdapter(page)
    if adapter_name == "time_range":
        return TimeRangeAdapter(page)
    if adapter_name == "panel_filter":
        return PanelFilterAdapter(page)
    raise BrowserAdapterError(f"unsupported adapter: {adapter_name!r}")


def _adapter_cache_key(control: ControlScenario) -> tuple[str, str]:
    return (control.adapter, control.key)


# ---------------------------------------------------------------------------
# Browser driver
# ---------------------------------------------------------------------------


class PlaywrightKibanaBrowser:
    """Playwright-backed ``BrowserAdapter`` for Kibana dashboard interactions."""

    def __init__(
        self,
        page: PageLike | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._page = page
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._closed = False
        self._adapters: dict[tuple[str, str], ControlAdapter] = {}
        self._clock = clock
        self._collector = _NetworkEventCollector(clock=clock)
        if page is not None:
            self._collector.attach(page)

    def _clear_adapter_cache(self) -> None:
        self._adapters.clear()

    def _adapter_for_control(self, control: ControlScenario) -> ControlAdapter:
        cache_key = _adapter_cache_key(control)
        adapter = self._adapters.get(cache_key)
        if adapter is None:
            adapter = _adapter_for(self._require_page(), control.adapter)
            self._adapters[cache_key] = adapter
        return adapter

    def _session_is_active(self) -> bool:
        return (
            self._playwright is not None
            or self._browser is not None
            or self._context is not None
        )

    def _release_session(self) -> None:
        self._collector.detach()
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self._page = None
        self._clear_adapter_cache()

    def start(
        self,
        *,
        headless: bool = True,
        user_data_dir: str = "",
        executable_path: str = "",
    ) -> None:
        if self._session_is_active():
            raise BrowserAdapterError(
                "browser session is already active; call close() before start()"
            )
        from playwright.sync_api import sync_playwright

        self._closed = False
        self._clear_adapter_cache()
        self._playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": headless}
        if executable_path:
            launch_kwargs["executable_path"] = executable_path

        viewport: Any = {"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT}
        if user_data_dir:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir,
                viewport=viewport,
                **launch_kwargs,
            )
            self._browser = None
            pages = self._context.pages
            self._page = cast(
                PageLike,
                pages[0] if pages else self._context.new_page(),
            )
        else:
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
            self._context = self._browser.new_context(viewport=viewport)
            pages = self._context.pages
            self._page = cast(
                PageLike,
                pages[0] if pages else self._context.new_page(),
            )
        self._collector.attach(self._page)

    def _require_page(self) -> PageLike:
        if self._page is None:
            raise BrowserAdapterError("browser page is not available; call start()")
        return self._page

    def open_dashboard(self, url: str) -> None:
        self._clear_adapter_cache()
        page = self._require_page()
        page.goto(url, wait_until="domcontentloaded")
        page.locator(_TEST_SUBJ_CONTROL_FRAME).nth(0).wait_for(
            state="attached",
            timeout=60_000,
        )

    def reset(self, url: str) -> None:
        self._clear_adapter_cache()
        page = self._require_page()
        # Navigating to the same hash route does not reload Kibana and can
        # preserve open popovers and prior control state. Leave the SPA first
        # so every interaction starts from the dashboard's persisted baseline.
        page.goto("about:blank", wait_until="domcontentloaded")
        page.goto(url, wait_until="domcontentloaded")
        page.locator(_TEST_SUBJ_CONTROL_FRAME).nth(0).wait_for(
            state="attached",
            timeout=60_000,
        )

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        return self._adapter_for_control(control).discover(control)

    def select(self, control: ControlScenario, option: str) -> None:
        self._adapter_for_control(control).select(control, option)

    def read_state(self, control: ControlScenario) -> ControlState:
        return self._adapter_for_control(control).read_state()

    def read_selected(self, control: ControlScenario) -> tuple[str, ...]:
        page = self._require_page()
        if control.adapter in _ESQL_ADAPTERS or control.adapter == "options_list":
            combobox = _combobox_by_label(page, control.label)
            return _combobox_selected_option_texts(combobox)
        if control.adapter == "range_slider":
            adapter = RangeSliderAdapter(page)
            low_handle, high_handle = adapter._handles(control.label)
            return (f"{low_handle.input_value()}..{high_handle.input_value()}",)
        if control.adapter == "query_bar":
            value = _searchbox(page).input_value().strip()
            return (value,) if value else ()
        return ()

    def begin_step(self) -> CaptureCursor:
        page = self._require_page()
        self._collector.attach(page)
        return self._collector.begin_step()

    def capture(
        self,
        expected_panels: Collection[str],
        cursor: CaptureCursor | None = None,
    ) -> BrowserObservation:
        page = self._require_page()
        self._collector.attach(page)
        body = page.locator("body")
        accessibility_snapshot = ""
        visible_text = ""
        if body.count() == 1:
            accessibility_snapshot = _bound_page_capture(body.aria_snapshot())
            visible_text = _bound_page_capture(body.inner_text())
        panels = tuple(_capture_panel_evidence(page, panel_id) for panel_id in expected_panels)
        return BrowserObservation(
            url=page.url,
            accessibility_snapshot=accessibility_snapshot,
            visible_text=visible_text,
            selected_state=MappingProxyType({}),
            network=self._collector.network_since(cursor),
            panels=panels,
            console_errors=self._collector.console_since(cursor),
            pending_requests=self._collector._pending_requests_since(cursor),
        )

    def clear_evidence(self) -> None:
        if self._collector._pending_requests():
            raise BrowserAdapterError("cannot clear evidence while requests are pending")
        self._collector.clear()

    def screenshot(self, path: str | Path) -> bool:
        target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._require_page().screenshot(path=str(target), full_page=True)
        except Exception:
            return False
        return target.is_file() and target.stat().st_size > 0

    def settle(
        self,
        cursor: CaptureCursor,
        expected_panels: Collection[str],
        *,
        policy: SettlePolicy | None = None,
    ) -> BrowserObservation:
        settle_policy = policy or SettlePolicy()
        _validate_settle_policy(settle_policy)
        page = self._require_page()
        self._collector.attach(page)
        deadline = self._clock() + settle_policy.timeout_seconds
        stable_count = 0
        previous_body: str | None = None
        previous_panel_fp: tuple[tuple[str, str, str], ...] | None = None
        previous_network_fp: tuple[tuple[int, str, str], ...] | None = None
        track_body_stability = not expected_panels

        while self._clock() < deadline:
            observation = self.capture(expected_panels, cursor=cursor)
            if expected_panels:
                terminal = self._collector._panels_terminal_for_expected(
                    cursor=cursor,
                    expected_panels=expected_panels,
                )
                requests_ok = all(terminal.get(panel_id, False) for panel_id in expected_panels)
            else:
                requests_ok = self._collector._all_network_terminal_since(cursor)

            panels_by_id = {panel.panel_id: panel for panel in observation.panels}
            panels_ok = all(
                panels_by_id.get(panel_id) is not None
                and panels_by_id[panel_id].status == "stable"
                for panel_id in expected_panels
            )

            panel_fp = _panel_fingerprint(observation.panels)
            network_fp = _network_fingerprint(
                self._collector.network_since(cursor),
                observation.pending_requests,
            )

            conditions_met = requests_ok and panels_ok
            if conditions_met:
                unstable = False
                if expected_panels:
                    if previous_panel_fp is not None and panel_fp != previous_panel_fp:
                        unstable = True
                    if previous_network_fp is not None and network_fp != previous_network_fp:
                        unstable = True
                elif track_body_stability:
                    if previous_body is not None and observation.visible_text != previous_body:
                        unstable = True
                    if previous_network_fp is not None and network_fp != previous_network_fp:
                        unstable = True
                if unstable:
                    stable_count = 0
                else:
                    stable_count += 1
                    if stable_count >= settle_policy.stable_polls:
                        return observation
            else:
                stable_count = 0

            if track_body_stability:
                previous_body = observation.visible_text
            if expected_panels:
                previous_panel_fp = panel_fp
            previous_network_fp = network_fp

            wait_for_timeout = getattr(page, "wait_for_timeout", None)
            if callable(wait_for_timeout):
                wait_for_timeout(settle_policy.poll_interval_ms)

        final = self.capture(expected_panels, cursor=cursor)
        reasons = self._collector._settle_blockers(
            cursor=cursor,
            expected_panels=expected_panels,
            panels=final.panels,
        )
        if not reasons:
            reasons = ["dashboard evidence did not remain stable"]
        raise SettleTimeout(final, "; ".join(reasons))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._release_session()
