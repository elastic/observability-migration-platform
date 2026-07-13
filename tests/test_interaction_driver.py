# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the Playwright Kibana interaction driver and control adapters."""

from __future__ import annotations

import builtins
import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from observability_migration.targets.kibana.interaction_audit import CapabilityCategory
from observability_migration.targets.kibana.interaction_driver import (
    _MAX_PAGE_CAPTURE_TEXT,
    BrowserAdapterError,
    ControlNotFound,
    EsqlControlAdapter,
    FilterPillAdapter,
    OptionNotFound,
    OptionsListAdapter,
    PanelFilterAdapter,
    PlaywrightKibanaBrowser,
    QueryBarAdapter,
    RangeSliderAdapter,
    SelectionDidNotStick,
    SettlePolicy,
    SettleTimeout,
    TimeRangeAdapter,
    _adapter_for,
    _scoped_options_container,
    _searchbox,
    _visible_action_menu,
)
from observability_migration.targets.kibana.interaction_scenarios import (
    Assertions,
    ControlScenario,
    OptionPolicy,
)

# ---------------------------------------------------------------------------
# Fake Playwright primitives
# ---------------------------------------------------------------------------


@dataclass
class FakeElement:
    role: str = ""
    name: str = ""
    test_subj: str = ""
    text: str = ""
    selector: str = ""
    aria_selected: str = "false"
    aria_multiselectable: str = "false"
    data_multiselect: str = "false"
    data_selected_options: str = ""
    data_panel_id: str = ""
    data_test_embeddable_id: str = ""
    aria_valuemin: str = ""
    aria_valuemax: str = ""
    aria_label: str = ""
    input_value: str = ""
    children: list[FakeElement] = field(default_factory=list)
    parent: FakeElement | None = None
    open: bool = False
    sticky: bool = True
    mounted: bool = True
    owner_name: str = ""
    linked_listbox: FakeElement | None = None
    evaluate_sets: int = 0
    fill_calls: int = 0
    owner_panel_id: str = ""


class FakeLocator:
    def __init__(self, page: FakePage, elements: list[FakeElement]) -> None:
        self._page = page
        self._elements = elements

    def count(self) -> int:
        return len(self._elements)

    def nth(self, index: int) -> FakeLocator:
        return FakeLocator(self._page, [self._elements[index]])

    def click(self, **kwargs: Any) -> None:
        del kwargs
        if len(self._elements) != 1:
            raise ControlNotFound(
                f"click requires exactly one element, got {len(self._elements)}"
            )
        self._page._click(self._elements[0])

    def fill(self, value: str, **kwargs: Any) -> None:
        del kwargs
        if len(self._elements) != 1:
            raise ControlNotFound(
                f"fill requires exactly one element, got {len(self._elements)}"
            )
        self._page._fill(self._elements[0], value)

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        del expression
        if len(self._elements) != 1:
            raise ControlNotFound(
                f"evaluate requires exactly one element, got {len(self._elements)}"
            )
        return self._page._evaluate_set(self._elements[0], arg)

    def press(self, key: str, **kwargs: Any) -> None:
        del kwargs
        if len(self._elements) != 1:
            raise ControlNotFound(
                f"press requires exactly one element, got {len(self._elements)}"
            )
        self._page._press(self._elements[0], key)

    def input_value(self) -> str:
        if len(self._elements) != 1:
            raise ControlNotFound(
                f"input_value requires exactly one element, got {len(self._elements)}"
            )
        return self._elements[0].input_value

    def inner_text(self) -> str:
        if len(self._elements) != 1:
            raise ControlNotFound(
                f"inner_text requires exactly one element, got {len(self._elements)}"
            )
        return self._elements[0].text

    def all_inner_texts(self) -> list[str]:
        return [element.text for element in self._elements]

    def all_text_contents(self) -> list[str]:
        return [element.text for element in self._elements]

    def get_attribute(self, name: str) -> str | None:
        if len(self._elements) != 1:
            return None
        element = self._elements[0]
        mapping = {
            "aria-selected": element.aria_selected,
            "aria-multiselectable": element.aria_multiselectable,
            "data-multiselect": element.data_multiselect,
            "data-selected-options": element.data_selected_options,
            "data-panel-id": element.data_panel_id,
            "data-test-embeddable-id": element.data_test_embeddable_id,
            "aria-valuemin": element.aria_valuemin,
            "aria-valuemax": element.aria_valuemax,
            "aria-label": element.aria_label,
        }
        return mapping.get(name, "")

    def filter(self, *, has_text: str | None = None) -> FakeLocator:
        if has_text is None:
            return self
        filtered = [element for element in self._elements if has_text in element.text]
        return FakeLocator(self._page, filtered)

    def get_by_role(
        self, role: str, *, name: str | None = None, exact: bool = False
    ) -> FakeLocator:
        return self._page._find_by_role(self._elements, role, name=name, exact=exact)

    def locator(self, selector: str) -> FakeLocator:
        return self._page._find_by_selector(self._elements, selector)

    def aria_snapshot(self) -> str:
        return "\n".join(element.text for element in self._elements)

    @property
    def evaluate_sets(self) -> int:
        if len(self._elements) != 1:
            raise ControlNotFound(
                f"evaluate_sets requires exactly one element, got {len(self._elements)}"
            )
        return self._elements[0].evaluate_sets

    @property
    def fill_calls(self) -> int:
        if len(self._elements) != 1:
            raise ControlNotFound(
                f"fill_calls requires exactly one element, got {len(self._elements)}"
            )
        return self._elements[0].fill_calls


class FakePage:
    def __init__(
        self,
        *,
        url: str = "about:blank",
        body_text: str = "",
        a11y_snapshot: str = "",
    ) -> None:
        self.url = url
        self.body_text = body_text
        self.a11y_snapshot = a11y_snapshot
        self.goto_calls: list[tuple[str, str | None]] = []
        self.option_clicks: dict[str, int] = {}
        self.field_suggestion_clicks: dict[str, int] = {}
        self._elements: list[FakeElement] = []
        self._body = FakeElement(role="document", text=body_text, selector="body")
        self._elements.append(self._body)
        self._listeners: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Any) -> None:
        handlers = self._listeners.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def wait_for_timeout(self, milliseconds: int) -> None:
        del milliseconds

    def add(self, element: FakeElement) -> FakeElement:
        self._elements.append(element)
        return element

    def goto(self, url: str, *, wait_until: str | None = None) -> None:
        self.goto_calls.append((url, wait_until))
        self.url = url

    def get_by_role(
        self,
        role: str,
        *,
        name: str | None = None,
        exact: bool = False,
    ) -> FakeLocator:
        return self._find_by_role(self._elements, role, name=name, exact=exact)

    def locator(self, selector: str) -> FakeLocator:
        return self._find_by_selector(self._elements, selector)

    def _name_matches(self, element_name: str, query: str | None, *, exact: bool) -> bool:
        if query is None:
            return True
        if exact:
            return element_name == query
        return query in element_name

    def _visible_elements(self, scope: list[FakeElement]) -> list[FakeElement]:
        return [element for element in scope if element.mounted]

    def _find_by_role(
        self,
        scope: list[FakeElement],
        role: str,
        *,
        name: str | None = None,
        exact: bool = False,
    ) -> FakeLocator:
        matches: list[FakeElement] = []
        for element in self._visible_elements(scope):
            if element.role == role and self._name_matches(element.name, name, exact=exact):
                matches.append(element)
            matches.extend(
                child
                for child in element.children
                if child.mounted
                and child.role == role
                and self._name_matches(child.name, name, exact=exact)
            )
        return FakeLocator(self, matches)

    def _find_by_selector(
        self,
        scope: list[FakeElement],
        selector: str,
    ) -> FakeLocator:
        if selector == "body":
            return FakeLocator(self, [self._body])
        matches: list[FakeElement] = []
        for element in self._visible_elements(scope):
            if element.selector == selector:
                matches.append(element)
            if element.test_subj and f'[data-test-subj="{element.test_subj}"]' == selector:
                matches.append(element)
            if selector.startswith('[data-panel-id="') and selector.endswith('"]'):
                panel_id = selector[len('[data-panel-id="') : -2]
                if element.data_panel_id == panel_id:
                    matches.append(element)
                if element.test_subj == f"dashboardPanel-{panel_id}":
                    matches.append(element)
            if selector.startswith('[data-test-embeddable-id="') and selector.endswith('"]'):
                embeddable_id = selector[len('[data-test-embeddable-id="') : -2]
                if element.data_test_embeddable_id == embeddable_id:
                    matches.append(element)
            if selector == '[data-test-subj="embeddablePanel"]':
                if element.test_subj == "embeddablePanel":
                    matches.append(element)
            matches.extend(
                child
                for child in element.children
                if child.mounted
                and (
                    child.selector == selector
                    or (
                        child.test_subj
                        and f'[data-test-subj="{child.test_subj}"]' == selector
                    )
                )
            )
        return FakeLocator(self, matches)

    def _mount_listbox_for(self, combobox: FakeElement) -> None:
        self._unmount_all_listboxes()
        if combobox.linked_listbox is not None:
            combobox.linked_listbox.mounted = True
            combobox.open = True

    def _unmount_all_listboxes(self) -> None:
        for element in self._elements:
            if element.role == "listbox" or element.test_subj == "comboBoxOptionsList":
                element.mounted = False
            element.open = False

    def _unmount_all_menus(self) -> None:
        for element in self._elements:
            if element.test_subj == "euiContextMenuPanel" or element.role == "menu":
                element.mounted = False

    def _close_popovers(self) -> None:
        self._unmount_all_listboxes()

    def _open_filter_editor(self) -> None:
        field = FakeElement(
            test_subj="filterFieldSuggestionList",
            role="combobox",
            input_value="",
        )
        self.add(field)
        self.add(FakeElement(test_subj="filterValueInput", role="textbox", input_value=""))
        self.add(FakeElement(test_subj="saveFilterButton", role="button", name="Save"))

    def _mount_field_suggestions(self, field_name: str) -> None:
        self._unmount_all_listboxes()
        listbox = FakeElement(
            role="listbox",
            test_subj="filterFieldSuggestions",
            mounted=True,
        )
        listbox.children.append(
            FakeElement(role="option", name=field_name, text=field_name)
        )
        self.add(listbox)

    def _click(self, element: FakeElement) -> None:
        if element.role == "combobox":
            self._mount_listbox_for(element)
            return
        if element.selector == "body":
            self._close_popovers()
            return
        if element.role == "option":
            if any(
                candidate.test_subj == "filterFieldSuggestions" and candidate.mounted
                for candidate in self._elements
            ):
                self.field_suggestion_clicks[element.text] = (
                    self.field_suggestion_clicks.get(element.text, 0) + 1
                )
                field_el = next(
                    e for e in self._elements if e.test_subj == "filterFieldSuggestionList"
                )
                field_el.input_value = element.text
                self._unmount_all_listboxes()
                return
            self.option_clicks[element.text] = self.option_clicks.get(element.text, 0) + 1
            combobox = self._combobox_for_option(element)
            if combobox is None:
                return
            if not combobox.sticky:
                return
            if combobox.aria_multiselectable == "true" or combobox.data_multiselect == "true":
                selected = [
                    part.strip()
                    for part in combobox.data_selected_options.split(",")
                    if part.strip()
                ]
                if element.text not in selected:
                    selected.append(element.text)
                combobox.data_selected_options = ",".join(selected)
                for child in combobox.children:
                    if child.role == "option":
                        child.aria_selected = "true" if child.text in selected else "false"
            else:
                combobox.data_selected_options = element.text
                for child in combobox.children:
                    if child.role == "option":
                        child.aria_selected = "true" if child.text == element.text else "false"
            return
        if element.test_subj == "superDatePickerShowDatesButton":
            element.open = True
            return
        if element.test_subj == "optionsListControlApplyButton":
            self._close_popovers()
            return
        if element.test_subj == "addFilter":
            self._open_filter_editor()
            return
        if element.test_subj == "saveFilterButton":
            field_el = next(
                e for e in self._elements if e.test_subj == "filterFieldSuggestionList"
            )
            value_el = next(e for e in self._elements if e.test_subj == "filterValueInput")
            self.add(
                FakeElement(
                    test_subj="filterBadge",
                    text=f"{field_el.input_value}: {value_el.input_value}",
                )
            )
            return
        if element.test_subj == "embeddablePanelAction-togglePanelActionMenu":
            self._unmount_all_menus()
            menu = FakeElement(
                role="menu",
                test_subj="euiContextMenuPanel",
                mounted=True,
                owner_panel_id=element.owner_panel_id,
            )
            menu.children.append(
                FakeElement(
                    test_subj="embeddablePanelAction-addPanelFilter",
                    role="button",
                    name="Create filter",
                    owner_panel_id=element.owner_panel_id,
                )
            )
            self.add(menu)
            element.open = True
            return
        if element.test_subj == "embeddablePanelAction-addPanelFilter":
            self._open_filter_editor()
            return
        if element.role == "button" and element.text:
            for existing in self._elements:
                if existing.test_subj == "superDatePickerstartDatePopoverButton":
                    existing.text = element.text
                    return
            self.add(
                FakeElement(
                    test_subj="superDatePickerstartDatePopoverButton",
                    text=element.text,
                )
            )

    def _fill(self, element: FakeElement, value: str) -> None:
        element.fill_calls += 1
        if element.role == "slider" and not element.sticky:
            return
        element.input_value = value
        if element.role == "slider":
            element.text = value
        if element.test_subj == "filterFieldSuggestionList":
            self._mount_field_suggestions(value)

    def _evaluate_set(self, element: FakeElement, value: Any) -> None:
        element.evaluate_sets += 1
        if element.role == "slider" and not element.sticky:
            return
        element.input_value = str(value)
        if element.role == "slider":
            element.text = str(value)

    def _press(self, element: FakeElement, key: str) -> None:
        if key == "Enter":
            element.input_value = element.input_value

    def _combobox_for_option(self, option: FakeElement) -> FakeElement | None:
        for element in self._elements:
            if element.role == "combobox" and option in element.children:
                return element
            if (
                element.role == "combobox"
                and element.linked_listbox is not None
                and option in element.linked_listbox.children
            ):
                return element
        return None


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def advance_ms(self, milliseconds: int) -> None:
        self._now += milliseconds / 1000.0


@dataclass
class FakeRequestFailure:
    error_text: str = ""


@dataclass
class FakeNetworkRequest:
    method: str
    url: str
    headers: dict[str, str]
    post_data_json: dict[str, object] | None = None
    failure: FakeRequestFailure | None = None


@dataclass
class FakeNetworkResponse:
    request: FakeNetworkRequest
    status: int
    body: object = None
    json_error: str = ""

    def json(self) -> object:
        if self.json_error:
            raise ValueError(self.json_error)
        return self.body if self.body is not None else {}


@dataclass
class FakeConsoleMessage:
    type: str
    text: str


_KBN_CONTEXT_PANEL = (
    "%7B%22type%22%3A%22application%22%2C%22name%22%3A%22dashboards%22%2C%22child%22%3A%7B"
    "%22type%22%3A%22lens%22%2C%22id%22%3A%22{panel_id}%22%2C%22description%22%3A%22{title}%22%7D%7D"
)


def _esql_headers(panel_id: str, *, title: str = "Panel", opaque_id: str = "") -> dict[str, str]:
    encoded = _KBN_CONTEXT_PANEL.format(panel_id=panel_id, title=title.replace(" ", "%20"))
    headers = {"x-kbn-context": encoded}
    if opaque_id:
        headers["x-opaque-id"] = opaque_id
    return headers


class InstrumentedFakePage(FakePage):
    """FakePage with Playwright-style event listeners and deterministic timing."""

    def __init__(
        self,
        *,
        clock: FakeClock | None = None,
        url: str = "about:blank",
        body_text: str = "",
        a11y_snapshot: str = "",
    ) -> None:
        super().__init__(url=url, body_text=body_text, a11y_snapshot=a11y_snapshot)
        self._clock = clock or FakeClock()
        self.wait_timeout_calls: list[int] = []

    @property
    def listener_counts(self) -> dict[str, int]:
        return {event: len(handlers) for event, handlers in self._listeners.items()}

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.wait_timeout_calls.append(milliseconds)
        self._clock.advance_ms(milliseconds)

    def emit_request(self, request: FakeNetworkRequest) -> None:
        for handler in self._listeners.get("request", []):
            handler(request)

    def emit_response(self, response: FakeNetworkResponse) -> None:
        for handler in self._listeners.get("response", []):
            handler(response)

    def emit_request_failed(self, request: FakeNetworkRequest) -> None:
        for handler in self._listeners.get("requestfailed", []):
            handler(request)

    def emit_console(self, message: FakeConsoleMessage) -> None:
        for handler in self._listeners.get("console", []):
            handler(message)

    def emit_esql_request(
        self,
        panel_id: str,
        *,
        title: str = "Panel",
        opaque_id: str = "",
        query: str = "FROM metrics-*",
        params: list[dict[str, object]] | None = None,
    ) -> FakeNetworkRequest:
        request = FakeNetworkRequest(
            method="POST",
            url="http://localhost:5601/internal/search/esql_async",
            headers=_esql_headers(panel_id, title=title, opaque_id=opaque_id),
            post_data_json={"query": query, "params": params or []},
        )
        self.emit_request(request)
        return request

    def emit_esql_response(
        self,
        request: FakeNetworkRequest,
        *,
        status: int = 200,
        body: object | None = None,
        json_error: str = "",
    ) -> None:
        if body is None:
            body = {
                "columns": [{"name": "value"}],
                "values": [[1]],
            }
        self.emit_response(
            FakeNetworkResponse(
                request=request,
                status=status,
                body=body,
                json_error=json_error,
            )
        )


def _panel_page(
    panel_id: str,
    *,
    title: str = "",
    text: str = "stable panel output",
    loading: bool = False,
    missing: bool = False,
    identity: str = "dashboardPanel",
    clock: FakeClock | None = None,
) -> InstrumentedFakePage:
    page = InstrumentedFakePage(clock=clock, body_text="dashboard body")
    if missing:
        return page
    if identity == "dashboardPanel":
        panel = FakeElement(test_subj=f"dashboardPanel-{panel_id}", text=text)
    else:
        panel = FakeElement(
            test_subj="embeddablePanel",
            data_panel_id=panel_id,
            text=text,
        )
    if title:
        panel.children.append(FakeElement(role="heading", name=title, text=title))
    if loading:
        panel.children.append(
            FakeElement(test_subj="lnsEmbeddablePanelLoadingIndicator", text="Loading")
        )
        panel.text = f"{text} Loading"
    page.add(panel)
    return page


def _attach_listbox(
    page: FakePage,
    combobox: FakeElement,
    *,
    options: list[str],
    selected: list[str] | None = None,
) -> FakeElement:
    listbox = FakeElement(
        role="listbox",
        test_subj="comboBoxOptionsList",
        owner_name=combobox.name,
        mounted=False,
    )
    selected_set = set(selected or [])
    for option_text in options:
        option = FakeElement(
            role="option",
            name=option_text,
            text=option_text,
            aria_selected="true" if option_text in selected_set else "false",
        )
        listbox.children.append(option)
        combobox.children.append(option)
    combobox.linked_listbox = listbox
    page.add(listbox)
    return listbox


def _control(
    label: str,
    key: str,
    adapter: str,
    *,
    capability: CapabilityCategory = CapabilityCategory.MIGRATED_LIVE,
) -> ControlScenario:
    return ControlScenario(
        label=label,
        key=key,
        adapter=adapter,
        capability=capability,
        options=OptionPolicy(strategy="every"),
        assertions=Assertions(),
    )


def _esql_page(
    *,
    combobox_name: str,
    options: list[str],
    selected: list[str] | None = None,
    sticky: bool = True,
    duplicate: bool = False,
    incompatible_warning: str = "",
) -> FakePage:
    page = FakePage()
    combobox = FakeElement(
        role="combobox",
        name=combobox_name,
        sticky=sticky,
        data_selected_options=",".join(selected or []),
    )
    page.add(combobox)
    _attach_listbox(page, combobox, options=options, selected=selected)
    if duplicate:
        page.add(FakeElement(role="combobox", name=combobox_name))
    if incompatible_warning:
        page.add(
            FakeElement(
                test_subj="esqlControlsIncompatibleSelectionsWarning",
                text=incompatible_warning,
            )
        )
    return page


def test_esql_control_adapter_uses_accessible_name_and_reads_all_options() -> None:
    page = _esql_page(
        combobox_name="Group by",
        options=["exporter", "transport", "receiver"],
    )
    discovered = EsqlControlAdapter(page).discover(
        _control("Group by", "grouping", "esql_field")
    )
    assert discovered.options == ("exporter", "transport", "receiver")


def test_esql_discover_reads_selected_values() -> None:
    page = _esql_page(
        combobox_name="namespace",
        options=["ns_1", "ns_2"],
        selected=["ns_1"],
    )
    discovered = EsqlControlAdapter(page).discover(
        _control("namespace", "namespace", "esql_value")
    )
    assert discovered.selected == ("ns_1",)


def test_esql_missing_control_raises() -> None:
    page = FakePage()
    with pytest.raises(ControlNotFound, match="control not found"):
        EsqlControlAdapter(page).discover(_control("missing", "missing", "esql_value"))


def test_esql_ambiguous_control_raises() -> None:
    page = _esql_page(
        combobox_name="namespace",
        options=["ns_1"],
        duplicate=True,
    )
    with pytest.raises(ControlNotFound, match="ambiguous"):
        EsqlControlAdapter(page).discover(_control("namespace", "namespace", "esql_value"))


def test_esql_exact_option_select_and_selected_state_verification() -> None:
    page = _esql_page(
        combobox_name="namespace",
        options=["ns_1", "ns_2"],
        selected=["ns_1"],
    )
    EsqlControlAdapter(page).select(_control("namespace", "namespace", "esql_value"), "ns_2")
    assert page.option_clicks["ns_2"] == 1
    discovered = EsqlControlAdapter(page).discover(
        _control("namespace", "namespace", "esql_value")
    )
    assert discovered.selected == ("ns_2",)


def test_selection_is_not_retried_when_it_does_not_stick() -> None:
    page = _esql_page(
        combobox_name="namespace",
        options=["ns_1", "ns_2"],
        selected=["ns_1"],
        sticky=False,
    )
    with pytest.raises(SelectionDidNotStick):
        EsqlControlAdapter(page).select(
            _control("namespace", "namespace", "esql_value"),
            "ns_2",
        )
    assert page.option_clicks["ns_2"] == 1


def test_esql_missing_option_raises() -> None:
    page = _esql_page(combobox_name="namespace", options=["ns_1"])
    with pytest.raises(OptionNotFound):
        EsqlControlAdapter(page).select(
            _control("namespace", "namespace", "esql_value"),
            "missing",
        )


def test_esql_incompatible_warning_state_extraction() -> None:
    page = _esql_page(
        combobox_name="namespace",
        options=["ns_1", "ns_2"],
        selected=["ns_1", "ns_2"],
        incompatible_warning="Incompatible selections (2)",
    )
    adapter = EsqlControlAdapter(page)
    adapter.discover(_control("namespace", "namespace", "esql_value"))
    state = adapter.read_state()
    assert state.incompatible_warning == "Incompatible selections (2)"
    assert state.selected_count == 2


def test_esql_special_character_option_select() -> None:
    page = _esql_page(
        combobox_name="owner",
        options=['O"Brien', "plain"],
        selected=["plain"],
    )
    EsqlControlAdapter(page).select(_control("owner", "owner", "esql_value"), 'O"Brien')
    combobox = page.get_by_role("combobox", name="owner", exact=True)
    assert combobox.get_attribute("data-selected-options") == 'O"Brien'
    assert page.option_clicks['O"Brien'] == 1


def test_esql_missing_option_with_special_characters_raises() -> None:
    page = _esql_page(combobox_name="owner", options=["plain"])
    with pytest.raises(OptionNotFound):
        EsqlControlAdapter(page).select(
            _control("owner", "owner", "esql_value"),
            'missing"quote',
        )


def test_esql_verifies_operated_control_after_popover_unmounts() -> None:
    page = _esql_page(
        combobox_name="namespace",
        options=["ns_1", "ns_2"],
        selected=["ns_1"],
    )
    EsqlControlAdapter(page).select(_control("namespace", "namespace", "esql_value"), "ns_2")
    assert page.locator('[data-test-subj="comboBoxOptionsList"]').count() == 0
    combobox = page.get_by_role("combobox", name="namespace", exact=True)
    assert combobox.get_attribute("data-selected-options") == "ns_2"


def _multi_esql_page() -> FakePage:
    page = FakePage()
    namespace = FakeElement(
        role="combobox",
        name="namespace",
        data_selected_options="ns_1",
    )
    page.add(namespace)
    _attach_listbox(
        page,
        namespace,
        options=["ns_1", "ns_2"],
        selected=["ns_1"],
    )
    instance = FakeElement(
        role="combobox",
        name="instance",
        data_selected_options="redis_1",
    )
    page.add(instance)
    _attach_listbox(
        page,
        instance,
        options=["redis_1", "redis_2"],
        selected=["redis_1"],
    )
    return page


def test_multi_control_select_verifies_second_control_not_first() -> None:
    page = _multi_esql_page()
    EsqlControlAdapter(page).select(_control("instance", "instance", "esql_value"), "redis_2")
    namespace = page.get_by_role("combobox", name="namespace", exact=True)
    instance = page.get_by_role("combobox", name="instance", exact=True)
    assert namespace.get_attribute("data-selected-options") == "ns_1"
    assert instance.get_attribute("data-selected-options") == "redis_2"


def test_scoped_options_container_raises_when_no_popover_or_listbox() -> None:
    page = FakePage()
    with pytest.raises(ControlNotFound, match="listbox: control not found"):
        _scoped_options_container(page)


def test_scoped_options_container_raises_when_multiple_listboxes() -> None:
    page = FakePage()
    page.add(FakeElement(role="listbox", name="first"))
    page.add(FakeElement(role="listbox", name="second"))
    with pytest.raises(ControlNotFound, match="listbox: ambiguous"):
        _scoped_options_container(page)


def test_scoped_options_container_raises_when_multiple_popovers() -> None:
    page = FakePage()
    page.add(FakeElement(test_subj="comboBoxOptionsList", role="listbox", name="a"))
    page.add(FakeElement(test_subj="comboBoxOptionsList", role="listbox", name="b"))
    with pytest.raises(ControlNotFound, match="popover: ambiguous"):
        _scoped_options_container(page)


def _options_list_page(
    *,
    combobox_name: str,
    options: list[str],
    selected: list[str] | None = None,
    multiselect: bool = False,
    has_apply: bool = False,
    sticky: bool = True,
) -> FakePage:
    page = FakePage()
    combobox = FakeElement(
        role="combobox",
        name=combobox_name,
        aria_multiselectable="true" if multiselect else "false",
        data_multiselect="true" if multiselect else "false",
        data_selected_options=",".join(selected or []),
        sticky=sticky,
    )
    page.add(combobox)
    _attach_listbox(page, combobox, options=options, selected=selected)
    if has_apply:
        page.add(FakeElement(test_subj="optionsListControlApplyButton", role="button"))
    return page


def test_options_list_single_select() -> None:
    page = _options_list_page(
        combobox_name="region",
        options=["us-east", "eu-west"],
        selected=["us-east"],
    )
    OptionsListAdapter(page).select(_control("region", "region", "options_list"), "eu-west")
    discovered = OptionsListAdapter(page).discover(_control("region", "region", "options_list"))
    assert discovered.selected == ("eu-west",)


def test_options_list_multiselect_comma_separated() -> None:
    page = _options_list_page(
        combobox_name="services",
        options=["api", "web", "worker"],
        multiselect=True,
        has_apply=True,
    )
    OptionsListAdapter(page).select(
        _control("services", "services", "options_list"),
        "api,worker",
    )
    discovered = OptionsListAdapter(page).discover(
        _control("services", "services", "options_list")
    )
    assert discovered.selected == ("api", "worker")
    assert page.option_clicks["api"] == 1
    assert page.option_clicks["worker"] == 1


def test_options_list_comma_literal_for_non_multi() -> None:
    page = _options_list_page(
        combobox_name="label",
        options=["a,b", "plain"],
    )
    OptionsListAdapter(page).select(_control("label", "label", "options_list"), "a,b")
    discovered = OptionsListAdapter(page).discover(_control("label", "label", "options_list"))
    assert discovered.selected == ("a,b",)


def test_options_list_missing_option_raises() -> None:
    page = _options_list_page(combobox_name="region", options=["us-east"])
    with pytest.raises(OptionNotFound):
        OptionsListAdapter(page).select(
            _control("region", "region", "options_list"),
            "missing",
        )


def test_options_list_sticky_failure_single_select() -> None:
    page = _options_list_page(
        combobox_name="region",
        options=["us-east", "eu-west"],
        selected=["us-east"],
        sticky=False,
    )
    with pytest.raises(SelectionDidNotStick):
        OptionsListAdapter(page).select(
            _control("region", "region", "options_list"),
            "eu-west",
        )
    assert page.option_clicks["eu-west"] == 1


def test_options_list_sticky_failure_multiselect_clicks_each_option_once() -> None:
    page = _options_list_page(
        combobox_name="services",
        options=["api", "web", "worker"],
        multiselect=True,
        has_apply=True,
        sticky=False,
    )
    with pytest.raises(SelectionDidNotStick):
        OptionsListAdapter(page).select(
            _control("services", "services", "options_list"),
            "api,worker",
        )
    assert page.option_clicks.get("api", 0) == 1
    assert page.option_clicks.get("worker", 0) == 1
    assert page.option_clicks.get("web", 0) == 0


def _range_slider_page(
    *,
    label: str,
    low_bound: str = "0",
    high_bound: str = "100",
    low_value: str = "10",
    high_value: str = "90",
    sticky: bool = True,
    handle_count: int = 2,
) -> FakePage:
    page = FakePage()
    group = FakeElement(role="group", name=label)
    for index in range(handle_count):
        group.children.append(
            FakeElement(
                role="slider",
                name=f"{label} handle {index}",
                aria_valuemin=low_bound if index == 0 else "",
                aria_valuemax=high_bound if index == handle_count - 1 else "",
                input_value=low_value if index == 0 else high_value,
                sticky=sticky,
            )
        )
    page.add(group)
    return page


def test_range_slider_parses_fills_and_verifies_values() -> None:
    page = _range_slider_page(label="latency")
    adapter = RangeSliderAdapter(page)
    adapter.select(_control("latency", "latency", "range_slider"), "20..80")
    state = adapter.read_state()
    assert state.low_value == "20"
    assert state.high_value == "80"


def test_range_slider_malformed_selection_raises() -> None:
    page = _range_slider_page(label="latency")
    with pytest.raises(BrowserAdapterError, match=r"low\.\.high"):
        RangeSliderAdapter(page).select(
            _control("latency", "latency", "range_slider"),
            "not-a-range",
        )


def test_range_slider_wrong_handle_count_raises() -> None:
    page = _range_slider_page(label="latency", handle_count=1)
    with pytest.raises(ControlNotFound, match="exactly two handles"):
        RangeSliderAdapter(page).select(
            _control("latency", "latency", "range_slider"),
            "20..80",
        )


def test_range_slider_sticky_failure_raises() -> None:
    page = _range_slider_page(label="latency", sticky=False)
    with pytest.raises(SelectionDidNotStick):
        RangeSliderAdapter(page).select(
            _control("latency", "latency", "range_slider"),
            "20..80",
        )


def test_range_slider_sets_each_handle_exactly_once_via_evaluate() -> None:
    page = _range_slider_page(label="latency")
    group = page.get_by_role("group", name="latency", exact=True)
    sliders = group.get_by_role("slider")
    RangeSliderAdapter(page).select(_control("latency", "latency", "range_slider"), "20..80")
    assert sliders.nth(0).evaluate_sets == 1
    assert sliders.nth(1).evaluate_sets == 1
    assert sliders.nth(0).fill_calls == 0
    assert sliders.nth(1).fill_calls == 0


def _query_bar_page(*, query: str = "") -> FakePage:
    page = FakePage()
    page.add(
        FakeElement(
            test_subj="queryInput",
            role="searchbox",
            input_value=query,
        )
    )
    return page


def test_query_bar_fill_submit_verify() -> None:
    page = _query_bar_page()
    QueryBarAdapter(page).select(
        _control("query", "query", "query_bar"),
        "status:error",
    )
    discovered = QueryBarAdapter(page).discover(_control("query", "query", "query_bar"))
    assert discovered.selected == ("status:error",)


def test_query_bar_uses_submit_button_when_present() -> None:
    page = _query_bar_page()
    page.add(FakeElement(test_subj="querySubmitButton", role="button", name="Update"))
    QueryBarAdapter(page).select(
        _control("query", "query", "query_bar"),
        "service:api",
    )
    assert page.get_by_role("searchbox").input_value() == "service:api"


def test_query_bar_ambiguous_query_input_raises() -> None:
    page = FakePage()
    page.add(FakeElement(test_subj="queryInput", role="searchbox", input_value="a"))
    page.add(FakeElement(test_subj="queryInput", role="searchbox", input_value="b"))
    with pytest.raises(ControlNotFound, match="query bar: ambiguous"):
        _searchbox(page)


def _filter_pill_page() -> FakePage:
    page = FakePage()
    page.add(FakeElement(test_subj="addFilter", role="button", name="Add filter"))
    return page


def test_filter_pill_field_value_action_and_verification() -> None:
    page = _filter_pill_page()
    FilterPillAdapter(page).select(
        _control("filter", "filter", "filter_pill"),
        "service.name=api",
    )
    badge = page.locator('[data-test-subj="filterBadge"]')
    assert badge.count() == 1
    assert "service.name" in badge.inner_text()
    assert "api" in badge.inner_text()
    assert page.field_suggestion_clicks["service.name"] == 1


def test_filter_pill_selects_field_suggestion_once() -> None:
    page = _filter_pill_page()
    FilterPillAdapter(page).select(
        _control("filter", "filter", "filter_pill"),
        "host.name=web-01",
    )
    assert page.field_suggestion_clicks["host.name"] == 1
    assert page.locator('[data-test-subj="filterFieldSuggestions"]').count() == 0


def test_filter_pill_malformed_selection_raises() -> None:
    page = _filter_pill_page()
    with pytest.raises(BrowserAdapterError, match="field=value"):
        FilterPillAdapter(page).select(
            _control("filter", "filter", "filter_pill"),
            "missing-equals-sign",
        )


def _time_range_page(*, current: str = "Last 15 minutes") -> FakePage:
    page = FakePage()
    page.add(
        FakeElement(
            test_subj="superDatePickerShowDatesButton",
            role="button",
            name="Date quick select",
        )
    )
    page.add(
        FakeElement(
            test_subj="superDatePickerstartDatePopoverButton",
            text=current,
        )
    )
    page.add(FakeElement(role="button", name="Last 24 hours", text="Last 24 hours"))
    return page


def test_time_range_exact_option_and_verification() -> None:
    page = _time_range_page()
    TimeRangeAdapter(page).select(
        _control("time", "time", "time_range"),
        "Last 24 hours",
    )
    display = page.locator('[data-test-subj="superDatePickerstartDatePopoverButton"]')
    assert "Last 24 hours" in display.inner_text()


def _panel_filter_page(
    *,
    panel_id: str = "panel-1",
    missing_panel: bool = False,
    identity: str = "dashboardPanel",
) -> FakePage:
    page = FakePage()
    if not missing_panel:
        if identity == "dashboardPanel":
            panel = FakeElement(test_subj=f"dashboardPanel-{panel_id}")
        elif identity == "data-test-embeddable-id":
            panel = FakeElement(
                test_subj="embeddablePanel",
                data_test_embeddable_id=panel_id,
            )
        elif identity == "data-panel-id":
            panel = FakeElement(data_panel_id=panel_id, test_subj="embeddablePanel")
        else:
            panel = FakeElement(test_subj=f"dashboardPanel-{panel_id}")
        panel.children.append(
            FakeElement(
                test_subj="embeddablePanelAction-togglePanelActionMenu",
                role="button",
                name="Panel actions",
                owner_panel_id=panel_id,
            )
        )
        page.add(panel)
    return page


def test_panel_filter_action_and_verification() -> None:
    page = _panel_filter_page(panel_id="uptime")
    PanelFilterAdapter(page).select(
        _control("panel filter", "panel_filter", "panel_filter"),
        "uptime|service.name|checkout",
    )
    badge = page.locator('[data-test-subj="filterBadge"]')
    assert badge.count() == 1
    assert "service.name" in badge.inner_text()
    assert "checkout" in badge.inner_text()
    assert page.field_suggestion_clicks["service.name"] == 1


def test_panel_filter_uses_data_test_embeddable_id() -> None:
    page = _panel_filter_page(panel_id="abc-123", identity="data-test-embeddable-id")
    PanelFilterAdapter(page).select(
        _control("panel filter", "panel_filter", "panel_filter"),
        "abc-123|host.name|web-01",
    )
    badge = page.locator('[data-test-subj="filterBadge"]')
    assert badge.count() == 1
    assert "host.name" in badge.inner_text()


def test_panel_filter_ambiguous_panel_identity_raises() -> None:
    page = FakePage()
    page.add(FakeElement(test_subj="embeddablePanel", data_panel_id="dup"))
    page.add(FakeElement(test_subj="embeddablePanel", data_panel_id="dup"))
    with pytest.raises(ControlNotFound, match="ambiguous control"):
        PanelFilterAdapter(page).select(
            _control("panel filter", "panel_filter", "panel_filter"),
            "dup|field|value",
        )


def test_visible_action_menu_ambiguous_open_menus_fail_closed() -> None:
    page = FakePage()
    page.add(FakeElement(test_subj="euiContextMenuPanel", role="menu"))
    page.add(FakeElement(test_subj="euiContextMenuPanel", role="menu"))
    with pytest.raises(ControlNotFound, match="menu: ambiguous"):
        _visible_action_menu(page)


def _multi_panel_filter_page() -> FakePage:
    page = FakePage()
    for panel_id in ("panel-a", "panel-b"):
        panel = FakeElement(test_subj=f"dashboardPanel-{panel_id}")
        panel.children.append(
            FakeElement(
                test_subj="embeddablePanelAction-togglePanelActionMenu",
                role="button",
                name="Panel actions",
                owner_panel_id=panel_id,
            )
        )
        page.add(panel)
    return page


def test_panel_filter_uses_target_panel_menu_not_other_panel() -> None:
    page = _multi_panel_filter_page()
    PanelFilterAdapter(page).select(
        _control("panel filter", "panel_filter", "panel_filter"),
        "panel-b|region|us-east",
    )
    menu = page.locator('[data-test-subj="euiContextMenuPanel"]')
    assert menu.count() == 1
    assert menu.inner_text() == ""
    badge = page.locator('[data-test-subj="filterBadge"]')
    assert "region" in badge.inner_text()
    assert "us-east" in badge.inner_text()


def test_panel_filter_malformed_selection_raises() -> None:
    page = _panel_filter_page()
    with pytest.raises(BrowserAdapterError, match="panel_id\\|field\\|value"):
        PanelFilterAdapter(page).select(
            _control("panel filter", "panel_filter", "panel_filter"),
            "only|two",
        )


def test_panel_filter_missing_panel_raises() -> None:
    page = _panel_filter_page(missing_panel=True)
    with pytest.raises(ControlNotFound, match="panel"):
        PanelFilterAdapter(page).select(
            _control("panel filter", "panel_filter", "panel_filter"),
            "missing|field|value",
        )


@pytest.mark.parametrize(
    ("adapter_name", "expected_type"),
    [
        ("esql_value", EsqlControlAdapter),
        ("esql_field", EsqlControlAdapter),
        ("esql_function", EsqlControlAdapter),
        ("esql_interval", EsqlControlAdapter),
        ("options_list", OptionsListAdapter),
        ("range_slider", RangeSliderAdapter),
        ("query_bar", QueryBarAdapter),
        ("filter_pill", FilterPillAdapter),
        ("time_range", TimeRangeAdapter),
        ("panel_filter", PanelFilterAdapter),
    ],
)
def test_routing_supported_adapter_strings(adapter_name: str, expected_type: type) -> None:
    page = FakePage()
    adapter = _adapter_for(page, adapter_name)
    assert isinstance(adapter, expected_type)


def test_unknown_adapter_raises() -> None:
    page = FakePage()
    with pytest.raises(BrowserAdapterError, match="unsupported adapter"):
        _adapter_for(page, "unknown_adapter")


def test_playwright_browser_routes_unknown_adapter() -> None:
    browser = PlaywrightKibanaBrowser(FakePage())
    control = _control("x", "x", "not-real")
    object.__setattr__(control, "adapter", "not-real")
    with pytest.raises(BrowserAdapterError, match="unsupported adapter"):
        browser.discover(control)


def test_browser_open_reset_capture_use_domcontentloaded_only() -> None:
    page = FakePage(body_text="dashboard body", a11y_snapshot="snapshot")
    browser = PlaywrightKibanaBrowser(page)
    browser.open_dashboard("https://kibana.example/app/dashboards#/view/1")
    browser.reset("https://kibana.example/app/dashboards#/view/1?reset=1")
    observation = browser.capture(["panel-a"])
    assert page.goto_calls == [
        ("https://kibana.example/app/dashboards#/view/1", "domcontentloaded"),
        ("https://kibana.example/app/dashboards#/view/1?reset=1", "domcontentloaded"),
    ]
    assert observation.url == "https://kibana.example/app/dashboards#/view/1?reset=1"
    assert observation.visible_text == "dashboard body"
    assert observation.accessibility_snapshot == "dashboard body"
    assert observation.selected_state == {}


def test_browser_close_is_idempotent() -> None:
    browser = PlaywrightKibanaBrowser(FakePage())
    browser.close()
    browser.close()
    assert browser._closed is True


def test_browser_read_state_preserves_esql_adapter_state() -> None:
    page = _esql_page(
        combobox_name="namespace",
        options=["ns_1", "ns_2"],
        selected=["ns_1"],
    )
    browser = PlaywrightKibanaBrowser(page)
    control = _control("namespace", "namespace", "esql_value")
    browser.discover(control)
    browser.select(control, "ns_2")
    state = browser.read_state(control)
    assert state.selected_count == 1


def test_browser_read_state_preserves_range_adapter_state() -> None:
    page = _range_slider_page(label="latency")
    browser = PlaywrightKibanaBrowser(page)
    control = _control("latency", "latency", "range_slider")
    browser.select(control, "20..80")
    state = browser.read_state(control)
    assert state.low_value == "20"
    assert state.high_value == "80"


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False
        self._context = _FakeContext()

    def close(self) -> None:
        self.closed = True

    def new_context(self, **kwargs: Any) -> _FakeContext:
        del kwargs
        return self._context


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = [FakePage()]
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


class _FakePlaywright:
    def __init__(self) -> None:
        self.stopped = False
        self._context = _FakeContext()
        self._browser = _FakeBrowser()

    def start(self) -> _FakePlaywright:
        return self

    def stop(self) -> None:
        self.stopped = True

    @property
    def chromium(self) -> _FakePlaywright:
        return self

    def launch(self, **kwargs: Any) -> _FakeBrowser:
        del kwargs
        return self._browser

    def launch_persistent_context(self, user_data_dir: str, **kwargs: Any) -> _FakeContext:
        del user_data_dir, kwargs
        return self._context


def test_start_rejects_already_active_session() -> None:
    browser = PlaywrightKibanaBrowser()
    browser._playwright = object()
    with pytest.raises(BrowserAdapterError, match="already active"):
        browser.start()


def test_lifecycle_close_restart_releases_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_playwright = _FakePlaywright()

    def fake_sync_playwright() -> _FakePlaywright:
        return fake_playwright

    import playwright.sync_api

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", fake_sync_playwright)

    browser = PlaywrightKibanaBrowser()
    browser.start()
    assert browser._closed is False
    browser.close()
    assert browser._closed is True
    assert fake_playwright.stopped is True
    assert fake_playwright._browser.closed is True

    browser.start()
    assert browser._closed is False
    browser.close()
    assert browser._closed is True
    assert fake_playwright.stopped is True


def test_module_imports_without_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    module_name = "observability_migration.targets.kibana.interaction_driver"
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name == "playwright" or name.startswith("playwright."):
            raise ImportError("playwright blocked for test")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    sys.modules.pop(module_name, None)
    module = importlib.import_module(module_name)
    assert isinstance(module, ModuleType)
    assert PlaywrightKibanaBrowser is not None


def test_source_has_no_top_level_playwright_import() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "observability_migration"
        / "targets"
        / "kibana"
        / "interaction_driver.py"
    )
    source = source_path.read_text(encoding="utf-8")
    module_scope = source.split("def start")[0]
    import_lines = [
        line.strip()
        for line in module_scope.splitlines()
        if line.strip().startswith(("import playwright", "from playwright"))
    ]
    assert import_lines == []


# ---------------------------------------------------------------------------
# Task 7: event capture, panel snapshots, deterministic settling
# ---------------------------------------------------------------------------


def test_listeners_correlate_same_url_requests_by_object_identity() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    req_a = page.emit_esql_request("panel-a", opaque_id="a-1")
    req_b = page.emit_esql_request("panel-b", opaque_id="b-1")
    page.emit_esql_response(req_a)
    page.emit_esql_response(req_b)
    observation = browser.capture(["panel-a", "panel-b"])
    panel_ids = [item.panel_id for item in observation.network]
    assert panel_ids == ["panel-a", "panel-b"]
    assert all(item.status != 0 for item in observation.network)


def test_malformed_esql_request_is_contained_in_console_errors() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    bad_request = FakeNetworkRequest(
        method="POST",
        url="http://localhost:5601/internal/search/esql_async",
        headers=_esql_headers("panel-1"),
        post_data_json={"query": {"invalid": True}},
    )
    page.emit_request(bad_request)
    observation = browser.capture([])
    assert any("query must be a string" in message for message in observation.console_errors)
    assert observation.network == ()


def test_requestfailed_becomes_terminal_error_evidence() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    request = page.emit_esql_request("panel-1")
    request.failure = FakeRequestFailure(error_text="net::ERR_FAILED")
    page.emit_request_failed(request)
    observation = browser.capture(["panel-1"])
    assert len(observation.network) == 1
    assert observation.network[0].status == -1
    assert "ERR_FAILED" in observation.network[0].error
    assert observation.pending_requests == ()


def test_only_error_console_messages_are_recorded_and_bounded() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    page.emit_console(FakeConsoleMessage(type="log", text="ignored info"))
    page.emit_console(FakeConsoleMessage(type="error", text="visible error"))
    page.emit_console(FakeConsoleMessage(type="warning", text="ignored warning"))
    observation = browser.capture([])
    assert observation.console_errors == ("visible error",)


def test_listeners_installed_once_and_detached_on_close() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    assert page.listener_counts.get("request", 0) == 1
    browser.open_dashboard("https://kibana.example/dashboard")
    assert page.listener_counts.get("request", 0) == 1
    browser.close()
    assert all(count == 0 for count in page.listener_counts.values())


def test_begin_step_slices_network_and_console_since_cursor() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    page.emit_esql_request("panel-1")
    page.emit_console(FakeConsoleMessage(type="error", text="before step"))
    cursor = browser.begin_step()
    req = page.emit_esql_request("panel-2")
    page.emit_esql_response(req)
    page.emit_console(FakeConsoleMessage(type="error", text="after step"))
    observation = browser.capture(["panel-2"], cursor=cursor)
    assert len(observation.network) == 1
    assert observation.network[0].panel_id == "panel-2"
    assert observation.console_errors == ("after step",)


def test_panel_snapshot_missing_loading_stable_and_title() -> None:
    missing_page = _panel_page("missing", missing=True)
    browser = PlaywrightKibanaBrowser(missing_page)
    missing = browser.capture(["missing"]).panels[0]
    assert missing.status == "missing"
    assert missing.title == "missing"

    loading_page = _panel_page("panel-1", title="CPU Usage", loading=True)
    loading = PlaywrightKibanaBrowser(loading_page).capture(["panel-1"]).panels[0]
    assert loading.status == "loading"
    assert loading.title == "CPU Usage"

    stable_page = _panel_page("panel-2", title="Memory", text="42% used")
    stable = PlaywrightKibanaBrowser(stable_page).capture(["panel-2"]).panels[0]
    assert stable.status == "stable"
    assert stable.title == "Memory"
    assert "42% used" in stable.detail


def test_panel_snapshot_prefers_aria_label_when_no_heading() -> None:
    page = InstrumentedFakePage()
    panel = FakeElement(
        test_subj="dashboardPanel-panel-x",
        aria_label="Network throughput",
        text="1.2 Gbps",
    )
    page.add(panel)
    evidence = PlaywrightKibanaBrowser(page).capture(["panel-x"]).panels[0]
    assert evidence.title == "Network throughput"
    assert evidence.status == "stable"


def test_settle_succeeds_after_terminal_response_and_stable_panels() -> None:
    clock = FakeClock()
    page = _panel_page("panel-1", title="CPU", text="stable output", clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    request = page.emit_esql_request("panel-1")
    page.emit_esql_response(request)
    result = browser.settle(
        cursor,
        ["panel-1"],
        policy=SettlePolicy(timeout_seconds=1.0, poll_interval_ms=10, stable_polls=3),
    )
    assert result.panels[0].status == "stable"
    assert result.network[0].status == 200
    assert result.pending_requests == ()
    assert len(page.wait_timeout_calls) >= 2


def test_settle_late_request_resets_stable_counter() -> None:
    clock = FakeClock()
    page = _panel_page("panel-1", text="stable output", clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    first = page.emit_esql_request("panel-1", opaque_id="first")
    page.emit_esql_response(first)
    policy = SettlePolicy(timeout_seconds=2.0, poll_interval_ms=10, stable_polls=3)

    poll_count_before_late = 0

    def maybe_emit_late_request() -> None:
        nonlocal poll_count_before_late
        poll_count_before_late += 1
        if poll_count_before_late == 2:
            late = page.emit_esql_request("panel-1", opaque_id="late")
            page.emit_esql_response(late)

    original_wait = page.wait_for_timeout

    def wait_with_late_request(ms: int) -> None:
        maybe_emit_late_request()
        original_wait(ms)

    page.wait_for_timeout = wait_with_late_request  # type: ignore[method-assign]

    result = browser.settle(cursor, ["panel-1"], policy=policy)
    assert result.network[-1].opaque_id == "late"
    assert len(page.wait_timeout_calls) >= 4


def test_settle_panel_text_change_resets_stable_counter() -> None:
    clock = FakeClock()
    page = _panel_page("panel-1", text="initial output", clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    request = page.emit_esql_request("panel-1")
    page.emit_esql_response(request)
    policy = SettlePolicy(timeout_seconds=2.0, poll_interval_ms=10, stable_polls=3)
    poll_count = 0
    panel = page.locator('[data-test-subj="dashboardPanel-panel-1"]')._elements[0]

    original_wait = page.wait_for_timeout

    def wait_with_text_change(ms: int) -> None:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 2:
            panel.text = "updated output"
        original_wait(ms)

    page.wait_for_timeout = wait_with_text_change  # type: ignore[method-assign]
    result = browser.settle(cursor, ["panel-1"], policy=policy)
    assert "updated output" in result.panels[0].detail


def test_settle_requires_all_expected_panels() -> None:
    clock = FakeClock()
    page = _panel_page("panel-a", text="a output", clock=clock)
    page.add(
        FakeElement(
            test_subj="dashboardPanel-panel-b",
            text="b output",
            children=[FakeElement(role="heading", name="Panel B", text="Panel B")],
        )
    )
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    req_a = page.emit_esql_request("panel-a")
    req_b = page.emit_esql_request("panel-b")
    page.emit_esql_response(req_a)
    page.emit_esql_response(req_b)
    result = browser.settle(
        cursor,
        ["panel-a", "panel-b"],
        policy=SettlePolicy(timeout_seconds=1.0, poll_interval_ms=10, stable_polls=2),
    )
    assert {panel.panel_id for panel in result.panels} == {"panel-a", "panel-b"}


def test_settle_empty_expected_panels_requires_terminal_network_only() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock, body_text="dashboard body")
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    request = page.emit_esql_request("panel-orphan")
    page.emit_esql_response(request)
    result = browser.settle(
        cursor,
        [],
        policy=SettlePolicy(timeout_seconds=1.0, poll_interval_ms=10, stable_polls=2),
    )
    assert result.network[0].panel_id == "panel-orphan"
    assert result.panels == ()


def test_settle_treats_4xx_5xx_as_terminal() -> None:
    clock = FakeClock()
    page = _panel_page("panel-1", text="error panel", clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    request = page.emit_esql_request("panel-1")
    page.emit_esql_response(request, status=503, body={"error": {"reason": "server busy"}})
    result = browser.settle(
        cursor,
        ["panel-1"],
        policy=SettlePolicy(timeout_seconds=1.0, poll_interval_ms=10, stable_polls=2),
    )
    assert result.network[0].status == 503
    assert result.network[0].error == "server busy"


def test_settle_timeout_includes_pending_and_reason_without_secrets() -> None:
    clock = FakeClock()
    page = _panel_page("panel-1", text="still loading", loading=True, clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    page.emit_esql_request("panel-1")
    with pytest.raises(SettleTimeout) as exc_info:
        browser.settle(
            cursor,
            ["panel-1"],
            policy=SettlePolicy(timeout_seconds=0.05, poll_interval_ms=10, stable_polls=2),
        )
    timeout = exc_info.value
    assert timeout.observation.pending_requests
    assert "panel panel-1" in timeout.reason
    assert "Authorization" not in timeout.reason
    assert "body" not in timeout.reason.casefold()


def test_settle_rejects_invalid_policy() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    with pytest.raises(BrowserAdapterError, match="invalid settle policy"):
        browser.settle(cursor, [], policy=SettlePolicy(timeout_seconds=0))
    with pytest.raises(BrowserAdapterError, match="invalid settle policy"):
        browser.settle(cursor, [], policy=SettlePolicy(poll_interval_ms=0))
    with pytest.raises(BrowserAdapterError, match="invalid settle policy"):
        browser.settle(cursor, [], policy=SettlePolicy(stable_polls=0))


def test_settle_uses_wait_for_timeout_not_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    page = _panel_page("panel-1", text="stable", clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    request = page.emit_esql_request("panel-1")
    page.emit_esql_response(request)

    def fail_sleep(_seconds: float) -> None:
        raise AssertionError("time.sleep must not be used during settle")

    import observability_migration.targets.kibana.interaction_driver as driver_module

    monkeypatch.setattr(driver_module.time, "sleep", fail_sleep)
    browser.settle(
        cursor,
        ["panel-1"],
        policy=SettlePolicy(timeout_seconds=1.0, poll_interval_ms=10, stable_polls=2),
    )
    assert page.wait_timeout_calls


def test_settle_succeeds_despite_pre_step_pending_request() -> None:
    clock = FakeClock()
    page = _panel_page("panel-1", text="stable output", clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    page.emit_esql_request("panel-0", opaque_id="pre-step")
    cursor = browser.begin_step()
    post_req = page.emit_esql_request("panel-1", opaque_id="post-step")
    page.emit_esql_response(post_req)
    result = browser.settle(
        cursor,
        ["panel-1"],
        policy=SettlePolicy(timeout_seconds=1.0, poll_interval_ms=10, stable_polls=2),
    )
    assert len(result.network) == 1
    assert result.network[0].opaque_id == "post-step"
    assert result.pending_requests == ()


def test_capture_scopes_pending_requests_to_cursor() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock, body_text="dashboard body")
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    page.emit_esql_request("panel-old", opaque_id="pre")
    cursor = browser.begin_step()
    page.emit_esql_request("panel-new", opaque_id="post")

    scoped = browser.capture([], cursor=cursor)
    all_observation = browser.capture([], cursor=None)

    assert len(scoped.pending_requests) == 1
    assert scoped.pending_requests[0].opaque_id == "post"
    assert len(all_observation.pending_requests) == 2
    assert {item.opaque_id for item in all_observation.pending_requests} == {"pre", "post"}


def test_pre_step_pending_completion_does_not_reset_post_step_stable_polls() -> None:
    clock = FakeClock()
    page = _panel_page("panel-1", text="stable output", clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    pre_req = page.emit_esql_request("panel-0", opaque_id="pre-step")
    cursor = browser.begin_step()
    post_req = page.emit_esql_request("panel-1", opaque_id="post-step")
    page.emit_esql_response(post_req)
    policy = SettlePolicy(timeout_seconds=2.0, poll_interval_ms=10, stable_polls=3)
    poll_count = 0
    original_wait = page.wait_for_timeout

    def wait_with_pre_step_completion(ms: int) -> None:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 2:
            page.emit_esql_response(pre_req)
        original_wait(ms)

    page.wait_for_timeout = wait_with_pre_step_completion  # type: ignore[method-assign]
    result = browser.settle(cursor, ["panel-1"], policy=policy)
    assert result.network[-1].opaque_id == "post-step"
    assert result.pending_requests == ()


def test_settle_timeout_lists_only_post_cursor_pending() -> None:
    clock = FakeClock()
    page = _panel_page("panel-1", text="still loading", loading=True, clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    page.emit_esql_request("panel-0", opaque_id="pre-step")
    cursor = browser.begin_step()
    page.emit_esql_request("panel-1", opaque_id="post-step")
    with pytest.raises(SettleTimeout) as exc_info:
        browser.settle(
            cursor,
            ["panel-1"],
            policy=SettlePolicy(timeout_seconds=0.05, poll_interval_ms=10, stable_polls=2),
        )
    timeout = exc_info.value
    assert len(timeout.observation.pending_requests) == 1
    assert timeout.observation.pending_requests[0].panel_id == "panel-1"
    assert "panel-1" in timeout.reason
    assert "panel-0" not in timeout.reason


def test_unrelated_body_churn_does_not_block_expected_panel_settle() -> None:
    clock = FakeClock()
    page = _panel_page("panel-1", text="stable output", clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    request = page.emit_esql_request("panel-1")
    page.emit_esql_response(request)
    poll_count = 0
    original_wait = page.wait_for_timeout

    def wait_with_toast(ms: int) -> None:
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 2:
            page.body_text = "dashboard body with transient toast notification"
            page._body.text = page.body_text
        original_wait(ms)

    page.wait_for_timeout = wait_with_toast  # type: ignore[method-assign]
    result = browser.settle(
        cursor,
        ["panel-1"],
        policy=SettlePolicy(timeout_seconds=1.0, poll_interval_ms=10, stable_polls=3),
    )
    assert result.panels[0].status == "stable"
    assert "toast" in result.visible_text


def test_body_churn_resets_empty_expected_settle() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock, body_text="initial dashboard body")
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    request = page.emit_esql_request("panel-orphan")
    page.emit_esql_response(request)
    original_wait = page.wait_for_timeout

    def wait_with_churning_body(ms: int) -> None:
        page._body.text = f"dashboard body variant {len(page.wait_timeout_calls)}"
        original_wait(ms)

    page.wait_for_timeout = wait_with_churning_body  # type: ignore[method-assign]
    with pytest.raises(SettleTimeout):
        browser.settle(
            cursor,
            [],
            policy=SettlePolicy(timeout_seconds=0.08, poll_interval_ms=10, stable_polls=2),
        )


def test_capture_bounds_page_visible_text_and_accessibility_snapshot() -> None:
    huge = "x" * (_MAX_PAGE_CAPTURE_TEXT + 500)
    page = InstrumentedFakePage(body_text=huge, a11y_snapshot=huge)
    browser = PlaywrightKibanaBrowser(page)
    observation = browser.capture([])
    assert len(observation.visible_text) == _MAX_PAGE_CAPTURE_TEXT
    assert len(observation.accessibility_snapshot) == _MAX_PAGE_CAPTURE_TEXT


def test_settle_timeout_observation_page_fields_are_bounded() -> None:
    clock = FakeClock()
    huge = "z" * (_MAX_PAGE_CAPTURE_TEXT + 500)
    page = _panel_page("panel-1", text="still loading", loading=True, clock=clock)
    page.body_text = huge
    page._body.text = huge
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    cursor = browser.begin_step()
    page.emit_esql_request("panel-1")
    with pytest.raises(SettleTimeout) as exc_info:
        browser.settle(
            cursor,
            ["panel-1"],
            policy=SettlePolicy(timeout_seconds=0.05, poll_interval_ms=10, stable_polls=2),
        )
    observation = exc_info.value.observation
    assert len(observation.visible_text) <= _MAX_PAGE_CAPTURE_TEXT
    assert len(observation.accessibility_snapshot) <= _MAX_PAGE_CAPTURE_TEXT


def test_clear_evidence_succeeds_when_no_pending() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    request = page.emit_esql_request("panel-1")
    page.emit_esql_response(request)
    page.emit_console(FakeConsoleMessage(type="error", text="old error"))
    browser.clear_evidence()
    observation = browser.capture([])
    assert observation.network == ()
    assert observation.console_errors == ()


def test_clear_evidence_rejects_when_requests_pending() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    page.emit_esql_request("panel-1")
    with pytest.raises(BrowserAdapterError, match="cannot clear evidence while requests are pending"):
        browser.clear_evidence()


def test_begin_step_cursor_resets_after_clear_evidence() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    request = page.emit_esql_request("panel-1")
    page.emit_esql_response(request)
    page.emit_console(FakeConsoleMessage(type="error", text="stale"))
    browser.clear_evidence()
    cursor = browser.begin_step()
    assert cursor.network_index == 0
    assert cursor.console_index == 0
    post_req = page.emit_esql_request("panel-2")
    page.emit_esql_response(post_req)
    observation = browser.capture([], cursor=cursor)
    assert len(observation.network) == 1
    assert observation.network[0].panel_id == "panel-2"


def test_detach_finalizes_in_flight_network_evidence() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    page.emit_esql_request("panel-1", opaque_id="in-flight")
    browser.close()
    evidence = browser._collector._network[0]
    assert evidence.status == -1
    assert "collector detached before response" in evidence.error
    assert not browser._collector._pending


def test_non_json_response_becomes_terminal_with_bounded_parse_error() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    request = page.emit_esql_request("panel-1")
    page.emit_response(
        FakeNetworkResponse(
            request=request,
            status=502,
            json_error="Response body is not valid JSON",
        )
    )
    observation = browser.capture(["panel-1"])
    assert len(observation.network) == 1
    assert observation.network[0].status == 502
    assert "not valid JSON" in observation.network[0].error
    assert observation.pending_requests == ()


def test_capture_network_evidence_is_isolated_from_collector() -> None:
    clock = FakeClock()
    page = InstrumentedFakePage(clock=clock)
    browser = PlaywrightKibanaBrowser(page, clock=clock.__call__)
    request = page.emit_esql_request("panel-1")
    page.emit_esql_response(request)
    first = browser.capture([], cursor=None)
    first.network[0].headers["Authorization"] = "mutated"
    first.network[0].params["injected"] = "value"
    second = browser.capture([], cursor=None)
    assert "Authorization" not in second.network[0].headers
    assert "injected" not in second.network[0].params
    assert browser._collector._network[0].headers.get("Authorization") != "mutated"
