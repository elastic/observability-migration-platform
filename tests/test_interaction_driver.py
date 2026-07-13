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
    TimeRangeAdapter,
    _adapter_for,
    _scoped_options_container,
    _searchbox,
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
    aria_valuemin: str = ""
    aria_valuemax: str = ""
    input_value: str = ""
    children: list[FakeElement] = field(default_factory=list)
    parent: FakeElement | None = None
    open: bool = False
    sticky: bool = True
    mounted: bool = True
    owner_name: str = ""
    linked_listbox: FakeElement | None = None


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
            "aria-valuemin": element.aria_valuemin,
            "aria-valuemax": element.aria_valuemax,
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
        self._elements: list[FakeElement] = []
        self._body = FakeElement(role="document", text=body_text, selector="body")
        self._elements.append(self._body)

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
                if element.test_subj == f"dashboardPanel-{panel_id}":
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
            if element.test_subj == "comboBoxOptionsList" or element.role == "listbox":
                element.mounted = False
            element.open = False

    def _close_popovers(self) -> None:
        self._unmount_all_listboxes()

    def _click(self, element: FakeElement) -> None:
        if element.role == "combobox":
            self._mount_listbox_for(element)
            return
        if element.selector == "body":
            self._close_popovers()
            return
        if element.role == "option":
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
            self.add(FakeElement(test_subj="filterFieldSuggestionList", role="combobox"))
            self.add(FakeElement(test_subj="filterValueInput", role="textbox"))
            self.add(FakeElement(test_subj="saveFilterButton", role="button", name="Save"))
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
            element.open = True
            return
        if element.test_subj == "embeddablePanelAction-addPanelFilter":
            self.add(FakeElement(test_subj="filterFieldSuggestionList", role="combobox"))
            self.add(FakeElement(test_subj="filterValueInput", role="textbox"))
            self.add(FakeElement(test_subj="saveFilterButton", role="button", name="Save"))
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
        if element.role == "slider" and not element.sticky:
            return
        element.input_value = value
        if element.role == "slider":
            element.text = value

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


def _panel_filter_page(*, panel_id: str = "panel-1", missing_panel: bool = False) -> FakePage:
    page = FakePage()
    if not missing_panel:
        panel = FakeElement(test_subj=f"dashboardPanel-{panel_id}")
        panel.children.append(
            FakeElement(
                test_subj="embeddablePanelAction-togglePanelActionMenu",
                role="button",
                name="Panel actions",
            )
        )
        page.add(panel)
    page.add(
        FakeElement(
            test_subj="embeddablePanelAction-addPanelFilter",
            role="button",
            name="Create filter",
        )
    )
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
