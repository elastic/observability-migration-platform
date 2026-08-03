# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Contract tests for the NativeDashboard IR.

``NativeDashboard``/``NativePanel``/``NativeSection``/``NativeControl`` mirror
the typed Kibana Dashboards API payload shape directly (``POST``/``PUT
/api/dashboards``). They are the single in-memory model that
``targets/kibana/dashboards_api.py`` serializes to JSON — YAML is a bridge
input, never the schema authority.
"""

from __future__ import annotations

from observability_migration.core.assets.native_dashboard import (
    MAX_DASHBOARD_ITEMS,
    MAX_ITEMS,
    MAX_SECTION_PANELS,
    NativeControl,
    NativeDashboard,
    NativeGrid,
    NativeMappingCounts,
    NativePanel,
    NativeSection,
    coalesce_loose_into_sections,
    dashboard_item_count,
    sectionize,
)


class TestNativeGrid:
    def test_from_dict_applies_api_defaults_when_missing(self) -> None:
        grid = NativeGrid.from_dict(None)
        assert grid == NativeGrid(x=0, y=0, w=24, h=8)

    def test_from_dict_reads_partial_overrides(self) -> None:
        grid = NativeGrid.from_dict({"x": 3, "h": 12})
        assert grid == NativeGrid(x=3, y=0, w=24, h=12)

    def test_to_dict_roundtrip(self) -> None:
        grid = NativeGrid(x=1, y=2, w=3, h=4)
        assert grid.to_dict() == {"x": 1, "y": 2, "w": 3, "h": 4}


class TestNativePanel:
    def test_to_api_dict_shape(self) -> None:
        panel = NativePanel(grid=NativeGrid(x=0, y=0, w=24, h=8), type="vis", config={"type": "metric"})
        assert panel.to_api_dict() == {
            "grid": {"x": 0, "y": 0, "w": 24, "h": 8},
            "type": "vis",
            "config": {"type": "metric"},
        }

    def test_from_api_dict_roundtrip(self) -> None:
        raw = {"grid": {"x": 1, "y": 2, "w": 12, "h": 4}, "type": "markdown", "config": {"content": "hi"}}
        panel = NativePanel.from_api_dict(raw)
        assert panel.grid == NativeGrid(x=1, y=2, w=12, h=4)
        assert panel.type == "markdown"
        assert panel.config == {"content": "hi"}
        assert panel.to_api_dict() == raw


class TestNativeSection:
    def test_to_api_dict_has_no_type_discriminator(self) -> None:
        section = NativeSection(
            title="System Metrics",
            collapsed=True,
            grid=NativeGrid(y=3),
            panels=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
        )
        api = section.to_api_dict()
        assert "type" not in api
        assert api["title"] == "System Metrics"
        assert api["collapsed"] is True
        assert api["grid"] == {"y": 3}
        assert len(api["panels"]) == 1
        assert api["panels"][0]["type"] == "vis"


class TestNativeControl:
    def test_to_api_dict_shape(self) -> None:
        control = NativeControl(type="esql_control", config={"variable_name": "env"})
        assert control.to_api_dict() == {"type": "esql_control", "config": {"variable_name": "env"}}


class TestNativeMappingCounts:
    def test_record_success_increments_mapped(self) -> None:
        counts = NativeMappingCounts()
        counts.record(True)
        counts.record(True)
        assert counts.mapped == 2
        assert counts.unmapped == 0

    def test_record_failure_tracks_reason_histogram(self) -> None:
        counts = NativeMappingCounts()
        counts.record(False, reason="no query")
        counts.record(False, reason="no query")
        counts.record(False, reason="bad type")
        assert counts.unmapped == 3
        assert counts.reasons == {"no query": 2, "bad type": 1}

    def test_as_dicts_returns_counts_and_reasons(self) -> None:
        counts = NativeMappingCounts(sections=1, controls=2)
        counts.record(True)
        counts.record(False, reason="x")
        counts_dict, reasons_dict = counts.as_dicts()
        assert counts_dict == {"mapped": 1, "unmapped": 1, "sections": 1, "controls": 2}
        assert reasons_dict == {"x": 1}


class TestNativeDashboardPayload:
    def test_to_api_payload_minimal(self) -> None:
        dashboard = NativeDashboard(title="D")
        assert dashboard.to_api_payload() == {"title": "D", "panels": []}

    def test_to_api_payload_includes_description_when_present(self) -> None:
        dashboard = NativeDashboard(title="D", description="hello")
        payload = dashboard.to_api_payload()
        assert payload["description"] == "hello"

    def test_to_api_payload_omits_description_when_blank(self) -> None:
        dashboard = NativeDashboard(title="D", description="")
        assert "description" not in dashboard.to_api_payload()

    def test_to_api_payload_includes_pinned_panels_when_controls_present(self) -> None:
        dashboard = NativeDashboard(
            title="D",
            controls=[NativeControl(type="esql_control", config={"variable_name": "env"})],
        )
        payload = dashboard.to_api_payload()
        assert payload["pinned_panels"] == [{"type": "esql_control", "config": {"variable_name": "env"}}]

    def test_to_api_payload_omits_pinned_panels_when_no_controls(self) -> None:
        dashboard = NativeDashboard(title="D")
        assert "pinned_panels" not in dashboard.to_api_payload()

    def test_to_api_payload_mixes_panels_and_sections_in_order(self) -> None:
        leaf = NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})
        section = NativeSection(title="Sec", panels=[leaf])
        dashboard = NativeDashboard(title="D", items=[leaf, section])
        payload = dashboard.to_api_payload()
        assert len(payload["panels"]) == 2
        assert payload["panels"][0]["type"] == "vis"
        assert "type" not in payload["panels"][1]


def _leaf(name: str) -> NativePanel:
    return NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric", "title": name})


class TestItemCapEnforcement:
    def test_under_cap_is_unchanged(self) -> None:
        items = [_leaf(f"p{i}") for i in range(5)]
        dashboard = NativeDashboard(title="D", items=list(items))
        counts = NativeMappingCounts()
        dashboard.enforce_item_cap(counts)
        assert dashboard.items == items
        assert counts.reasons == {}

    def test_over_cap_flat_panels_are_trimmed_to_total_cap(self) -> None:
        items: list[NativePanel | NativeSection] = [_leaf(f"p{i}") for i in range(1200)]
        dashboard = NativeDashboard(title="Big", items=items)
        counts = NativeMappingCounts()
        dashboard.enforce_item_cap(counts)
        assert len(dashboard.items) == MAX_DASHBOARD_ITEMS
        assert all(isinstance(item, NativePanel) for item in dashboard.items)
        assert counts.reasons.get("dropped_over_item_cap") == 200

    def test_over_cap_real_sections_are_truncated_with_drop_reason(self) -> None:
        items: list[NativePanel | NativeSection] = [NativeSection(title=f"s{i}") for i in range(1050)]
        dashboard = NativeDashboard(title="TooMany", items=items)
        counts = NativeMappingCounts()
        dashboard.enforce_item_cap(counts)
        assert len(dashboard.items) == MAX_ITEMS
        assert counts.reasons.get("dropped_over_item_cap") == 50

    def test_total_item_cap_truncates_section_children(self) -> None:
        items: list[NativePanel | NativeSection] = [
            NativeSection(title="s", panels=[_leaf(f"p{i}") for i in range(MAX_SECTION_PANELS)])
        ]
        dashboard = NativeDashboard(title="TooManyInside", items=items)
        counts = NativeMappingCounts()
        dashboard.enforce_item_cap(counts)
        assert dashboard_item_count(dashboard.items) == MAX_DASHBOARD_ITEMS
        assert isinstance(dashboard.items[0], NativeSection)
        assert len(dashboard.items[0].panels) == MAX_DASHBOARD_ITEMS - 1
        assert counts.reasons.get("dropped_over_total_item_cap") == 1

    def test_mixed_sections_and_loose_panels_over_total_cap_record_drops(self) -> None:
        items: list[NativePanel | NativeSection] = []
        for i in range(600):
            items.append(NativeSection(title=f"s{i}", panels=[_leaf("inner")]))
        for i in range(300):
            items.append(_leaf(f"loose{i}"))
        dashboard = NativeDashboard(title="Mixed", items=items)
        counts = NativeMappingCounts()
        dashboard.enforce_item_cap(counts)
        assert dashboard_item_count(dashboard.items) == MAX_ITEMS
        assert "dropped_over_item_cap" not in counts.reasons

        def _count_leaves(entries: list[NativePanel | NativeSection]) -> int:
            total = 0
            for entry in entries:
                if isinstance(entry, NativeSection):
                    total += len(entry.panels)
                else:
                    total += 1
            return total

        assert _count_leaves(dashboard.items) == 500
        assert counts.reasons.get("dropped_over_total_item_cap") == 500


class TestSectionizeHelpers:
    def test_sectionize_groups_panels_into_chunks_of_max_size(self) -> None:
        panels = [_leaf(f"p{i}") for i in range(120)]
        sections = sectionize(panels, size=100)
        assert [len(section.panels) for section in sections] == [100, 20]
        assert sections[1].title == "Panels 101\u2013120"

    def test_coalesce_preserves_real_sections_in_order(self) -> None:
        real_section = NativeSection(title="Real")
        loose = [_leaf("a"), _leaf("b")]
        items: list[NativePanel | NativeSection] = [loose[0], real_section, loose[1]]
        out = coalesce_loose_into_sections(items, max_items=100)
        assert out[1] is real_section
        assert isinstance(out[0], NativeSection)
        assert isinstance(out[2], NativeSection)
