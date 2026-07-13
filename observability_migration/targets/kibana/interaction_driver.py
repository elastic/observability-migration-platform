# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Playwright-backed Kibana dashboard control interaction driver.

Browser automation is isolated behind adapter classes and a lazy Playwright
import so modules remain importable without the optional ``browser`` extra.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, cast, runtime_checkable

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


# ---------------------------------------------------------------------------
# Observation / state models
# ---------------------------------------------------------------------------


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


@runtime_checkable
class LocatorLike(Protocol):
    def count(self) -> int: ...

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

    def capture(self, expected_panels: Collection[str]) -> BrowserObservation: ...

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
_TEST_SUBJ_COMBOBOX_POPOVER = '[data-test-subj="comboBoxOptionsList"]'
_TEST_SUBJ_INCOMPATIBLE_WARNING = '[data-test-subj="esqlControlsIncompatibleSelectionsWarning"]'

_ROLE_COMBOBOX = "combobox"
_ROLE_LISTBOX = "listbox"
_ROLE_OPTION = "option"
_ROLE_SLIDER = "slider"
_ROLE_SEARCHBOX = "searchbox"
_ROLE_BUTTON = "button"
_ROLE_GROUP = "group"

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
    count = by_role.count()
    if count == 1:
        return by_role
    if count > 1:
        raise ControlNotFound(
            f"combobox {label!r}: ambiguous control ({count} matches)"
        )
    by_test_subj = page.locator(f'[data-test-subj="{label}"]')
    return _require_exactly_one(by_test_subj, description=f"combobox {label!r}")


def _searchbox(page: PageLike) -> LocatorLike:
    by_test_subj = page.locator(_TEST_SUBJ_QUERY_INPUT)
    if by_test_subj.count() == 1:
        return by_test_subj
    by_role = page.get_by_role(_ROLE_SEARCHBOX)
    return _require_exactly_one(by_role, description="query bar")


def _option_locator(page: PageLike, option_text: str) -> LocatorLike:
    scoped = _scoped_options_container(page)
    if scoped.count() == 1:
        option = scoped.get_by_role(_ROLE_OPTION, name=option_text, exact=True)
        if option.count() == 1:
            return option
        option = scoped.locator(f'[role="option"]:has-text("{option_text}")')
        return _require_exactly_one(option, description=f"option {option_text!r}")

    option = page.get_by_role(_ROLE_OPTION, name=option_text, exact=True)
    if option.count() == 1:
        return option
    option = page.locator(f'[role="option"]:has-text("{option_text}")')
    return _require_exactly_one(option, description=f"option {option_text!r}")


def _scoped_options_container(page: PageLike) -> LocatorLike:
    popover = page.locator(_TEST_SUBJ_COMBOBOX_POPOVER)
    if popover.count() == 1:
        listbox = popover.get_by_role(_ROLE_LISTBOX)
        if listbox.count() == 1:
            return listbox
        return popover
    return page.get_by_role(_ROLE_LISTBOX)


def _read_option_texts(page: PageLike) -> tuple[str, ...]:
    container = _scoped_options_container(page)
    if container.count() == 0:
        return ()
    options = container.get_by_role(_ROLE_OPTION)
    if options.count() == 0:
        texts = container.all_text_contents()
        return tuple(text.strip() for text in texts if text.strip())
    return tuple(
        text.strip()
        for text in options.all_inner_texts()
        if text.strip()
    )


def _read_selected_option_texts(page: PageLike) -> tuple[str, ...]:
    container = _scoped_options_container(page)
    if container.count() == 0:
        return ()
    options = container.get_by_role(_ROLE_OPTION)
    selected: list[str] = []
    for index in range(options.count()):
        option = options.nth(index)
        aria_selected = option.get_attribute("aria-selected")
        if aria_selected == "true":
            selected.append(option.inner_text().strip())
    if selected:
        return tuple(selected)

    comboboxes = page.get_by_role(_ROLE_COMBOBOX)
    for index in range(comboboxes.count()):
        combobox = comboboxes.nth(index)
        value = combobox.get_attribute("data-selected-options")
        if value:
            return tuple(part.strip() for part in value.split(",") if part.strip())
    return ()


def _close_open_popover(page: PageLike) -> None:
    popover = page.locator(_TEST_SUBJ_COMBOBOX_POPOVER)
    if popover.count() == 1:
        page.locator("body").click()


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


def _selected_count(page: PageLike) -> int:
    selected = _read_selected_option_texts(page)
    if selected:
        return len(selected)
    warning = _read_incompatible_warning(page)
    if warning:
        match = _INCOMPATIBLE_SELECTIONS_RE.search(warning)
        if match:
            return int(match.group(1))
    return 0


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
    by_data_panel = page.locator(f'[data-panel-id="{panel_id}"]')
    if by_data_panel.count() == 1:
        return by_data_panel
    by_test_subj = page.locator(f'[data-test-subj="dashboardPanel-{panel_id}"]')
    return _require_exactly_one(
        by_test_subj,
        description=f"panel {panel_id!r}",
    )


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


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class EsqlControlAdapter:
    """Adapter for ES|QL dashboard controls (value/field/function/interval)."""

    def __init__(self, page: PageLike) -> None:
        self._page = page

    def read_state(self) -> ControlState:
        return ControlState(
            selected_count=_selected_count(self._page),
            incompatible_warning=_read_incompatible_warning(self._page),
        )

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        combobox = _combobox_by_label(self._page, control.label)
        combobox.click()
        options = _read_option_texts(self._page)
        selected = _read_selected_option_texts(self._page)
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=options,
            selected=selected,
        )

    def select(self, control: ControlScenario, option: str) -> None:
        combobox = _combobox_by_label(self._page, control.label)
        combobox.click()
        available = set(_read_option_texts(self._page))
        if option not in available:
            raise OptionNotFound(
                f"option {option!r} not found for control {control.label!r}"
            )
        _option_locator(self._page, option).click()
        _close_open_popover(self._page)
        selected = _read_selected_option_texts(self._page)
        if option not in selected:
            raise SelectionDidNotStick(
                f"option {option!r} did not stick for control {control.label!r}"
            )


class OptionsListAdapter:
    """Adapter for Kibana options-list dashboard controls."""

    def __init__(self, page: PageLike) -> None:
        self._page = page

    def read_state(self) -> ControlState:
        return ControlState(selected_count=_selected_count(self._page))

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        combobox = _combobox_by_label(self._page, control.label)
        combobox.click()
        options = _read_option_texts(self._page)
        selected = _read_selected_option_texts(self._page)
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=options,
            selected=selected,
        )

    def _is_multiselect(self, combobox: LocatorLike) -> bool:
        multiselectable = combobox.get_attribute("aria-multiselectable")
        if multiselectable == "true":
            return True
        marker = combobox.get_attribute("data-multiselect")
        return marker == "true"

    def _expand_options(self, option: str, *, multiselect: bool) -> tuple[str, ...]:
        if multiselect and "," in option:
            return tuple(part.strip() for part in option.split(",") if part.strip())
        return (option,)

    def select(self, control: ControlScenario, option: str) -> None:
        combobox = _combobox_by_label(self._page, control.label)
        multiselect = self._is_multiselect(combobox)
        expected = self._expand_options(option, multiselect=multiselect)

        combobox.click()
        available = set(_read_option_texts(self._page))
        for part in expected:
            if part not in available:
                raise OptionNotFound(
                    f"option {part!r} not found for control {control.label!r}"
                )
            _option_locator(self._page, part).click()

        apply = self._page.locator(_TEST_SUBJ_OPTIONS_LIST_APPLY)
        if apply.count() == 1:
            apply.click()

        _close_open_popover(self._page)
        selected = set(_read_selected_option_texts(self._page))
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

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        group = self._control_group(control.label)
        sliders = group.get_by_role(_ROLE_SLIDER)
        count = sliders.count()
        options: list[str] = []
        if count >= 1:
            low_bound = sliders.nth(0).get_attribute("aria-valuemin") or ""
            high_bound = sliders.nth(count - 1).get_attribute("aria-valuemax") or ""
            if low_bound and high_bound:
                mid = str((float(low_bound) + float(high_bound)) / 2)
                options.extend([low_bound, mid, high_bound])
        current = tuple(
            sliders.nth(index).input_value()
            for index in range(count)
        )
        return DiscoveredControl(
            key=control.key,
            label=control.label,
            options=tuple(dict.fromkeys(options)),
            selected=current,
        )

    def select(self, control: ControlScenario, option: str) -> None:
        low, high = _parse_range_selection(option)
        group = self._control_group(control.label)
        sliders = group.get_by_role(_ROLE_SLIDER)
        if sliders.count() != 2:
            raise ControlNotFound(
                f"range slider {control.label!r} requires exactly two handles, "
                f"found {sliders.count()}"
            )
        sliders.nth(0).fill(low)
        sliders.nth(1).fill(high)
        actual_low = sliders.nth(0).input_value()
        actual_high = sliders.nth(1).input_value()
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
        field_input = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_FILTER_FIELD),
            description="filter field",
        )
        field_input.fill(field)
        value_input = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_FILTER_VALUE),
            description="filter value",
        )
        value_input.fill(value)
        save = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_SAVE_FILTER),
            description="save filter",
        )
        save.click()
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
        filter_action = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_PANEL_FILTER_ACTION),
            description="panel filter action",
        )
        filter_action.click()
        field_input = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_FILTER_FIELD),
            description="filter field",
        )
        field_input.fill(field)
        value_input = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_FILTER_VALUE),
            description="filter value",
        )
        value_input.fill(value)
        save = _require_exactly_one(
            self._page.locator(_TEST_SUBJ_SAVE_FILTER),
            description="save filter",
        )
        save.click()
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


# ---------------------------------------------------------------------------
# Browser driver
# ---------------------------------------------------------------------------


class PlaywrightKibanaBrowser:
    """Playwright-backed ``BrowserAdapter`` for Kibana dashboard interactions."""

    def __init__(self, page: PageLike | None = None) -> None:
        self._page = page
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._closed = False

    def start(
        self,
        *,
        headless: bool = True,
        user_data_dir: str = "",
        executable_path: str = "",
    ) -> None:
        from playwright.sync_api import sync_playwright

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

    def _require_page(self) -> PageLike:
        if self._page is None:
            raise BrowserAdapterError("browser page is not available; call start()")
        return self._page

    def open_dashboard(self, url: str) -> None:
        self._require_page().goto(url, wait_until="domcontentloaded")

    def reset(self, url: str) -> None:
        self._require_page().goto(url, wait_until="domcontentloaded")

    def discover(self, control: ControlScenario) -> DiscoveredControl:
        return _adapter_for(self._require_page(), control.adapter).discover(control)

    def select(self, control: ControlScenario, option: str) -> None:
        _adapter_for(self._require_page(), control.adapter).select(control, option)

    def capture(self, expected_panels: Collection[str]) -> BrowserObservation:
        del expected_panels  # Task 7 adds panel/network settling.
        page = self._require_page()
        body = page.locator("body")
        accessibility_snapshot = ""
        visible_text = ""
        if body.count() == 1:
            accessibility_snapshot = body.aria_snapshot()
            visible_text = body.inner_text()
        return BrowserObservation(
            url=page.url,
            accessibility_snapshot=accessibility_snapshot,
            visible_text=visible_text,
            selected_state=MappingProxyType({}),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
