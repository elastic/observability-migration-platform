# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""NativeDashboard IR — the canonical model of a typed Kibana Dashboards API
payload (``POST``/``PUT /api/dashboards``).

This is the schema authority for the native upload path: it mirrors the API's
own ``panels``/``pinned_panels``/``grid``/``config`` shape directly instead of
going through the ``kb-dashboard-core`` YAML schema. ``targets/kibana/
dashboards_api.py`` builds one of these from either a YAML dashboard or a flat
migration-report dashboard and then calls :meth:`NativeDashboard.to_api_payload`
to get the exact JSON body the API expects. YAML remains a *bridge input* (and
the legacy ``kb-dashboard-cli`` renderer's own schema) — it never decides what
the native API accepts.

This module has no knowledge of YAML, Grafana, or Datadog: it only knows the
typed API's own shape and its item-count caps (see
``docs/explore-analyze/dashboards/create-dashboards-programmatically`` and
``scripts/fetch_dashboards_api_schema.py`` for schema refresh). Per-panel
mapping (chart-type dispatch, column/format/color rules) stays in
``dashboards_api.py``, which is the single place those live-verified quirks are
encoded; this module is where the *assembled dashboard* — items in order,
section nesting, top-level/section/pinned-control caps — is modeled and
serialized.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Current Dashboards API limits (Elastic docs, preview 9.4+): up to 1,000
# top-level dashboard items (panels + sections combined), up to 1,000 panels
# per section, up to 100 pinned controls, and at most 1,000 combined panels /
# sections / controls across the dashboard. The API schema is technical preview
# and externally hosted; use ``scripts/fetch_dashboards_api_schema.py`` to
# refresh/check it when Kibana moves.
MAX_DASHBOARD_ITEMS = 1000
MAX_SECTION_PANELS = 1000
MAX_PINNED_CONTROLS = 100
MAX_TOTAL_ITEMS = 1000

# Backward-compatible alias used by older callers/tests. New code should choose
# the collection-specific constant above.
MAX_ITEMS = MAX_DASHBOARD_ITEMS


@dataclass(frozen=True)
class NativeGrid:
    """A panel or section's position on the API's grid."""

    x: int = 0
    y: int = 0
    w: int = 24
    h: int = 8

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, raw: Any) -> NativeGrid:
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        return cls(
            x=int(data.get("x") or 0),
            y=int(data.get("y") or 0),
            w=int(data.get("w") or 24),
            h=int(data.get("h") or 8),
        )


@dataclass
class NativePanel:
    """One typed API leaf panel item (``type: "vis"`` or ``"markdown"``)."""

    grid: NativeGrid
    type: str
    config: dict[str, Any] = field(default_factory=dict)

    def to_api_dict(self) -> dict[str, Any]:
        return {"grid": self.grid.to_dict(), "type": self.type, "config": self.config}

    @classmethod
    def from_api_dict(cls, raw: dict[str, Any]) -> NativePanel:
        return cls(
            grid=NativeGrid.from_dict(raw.get("grid")),
            type=str(raw.get("type") or ""),
            config=dict(raw.get("config") or {}),
        )


@dataclass
class NativeSection:
    """A nested group of leaf panels rendered as a collapsible section.

    Sections carry no ``type`` discriminator in the API — that absence is how
    Kibana tells a section apart from a leaf panel.
    """

    title: str = ""
    collapsed: bool = False
    panels: list[NativePanel] = field(default_factory=list)
    grid: NativeGrid = field(default_factory=NativeGrid)

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "grid": {"y": self.grid.y},
            "title": self.title,
            "collapsed": self.collapsed,
            "panels": [panel.to_api_dict() for panel in self.panels],
        }


NativeItem = NativePanel | NativeSection


@dataclass
class NativeControl:
    """One ``pinned_panels`` entry (an ES|QL or data-view-backed control)."""

    type: str
    config: dict[str, Any] = field(default_factory=dict)

    def to_api_dict(self) -> dict[str, Any]:
        return {"type": self.type, "config": self.config}


@dataclass
class NativeMappingCounts:
    """Mapped/unmapped tallies plus an unmapped-reason histogram.

    Mirrors the ``(counts, reasons)`` shape ``dashboards_api.py`` has always
    returned, so it can be dropped in without changing any public return
    value.
    """

    mapped: int = 0
    unmapped: int = 0
    sections: int = 0
    controls: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def record(self, ok: bool, reason: str = "") -> None:
        if ok:
            self.mapped += 1
        else:
            self.unmapped += 1
            if reason:
                self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def add_reason(self, reason: str, count: int = 1) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + count

    def as_dicts(self) -> tuple[dict[str, int], dict[str, int]]:
        counts = {
            "mapped": self.mapped,
            "unmapped": self.unmapped,
            "sections": self.sections,
            "controls": self.controls,
        }
        return counts, dict(self.reasons)


def dashboard_item_count(items: list[NativeItem], controls: int = 0) -> int:
    """Count panels + sections + pinned controls against the API's total cap."""
    total = controls
    for item in items:
        total += 1
        if isinstance(item, NativeSection):
            total += len(item.panels)
    return total


def dashboard_leaf_panel_count(items: list[NativeItem]) -> int:
    """Count only leaf panels, including panels nested inside sections."""
    total = 0
    for item in items:
        if isinstance(item, NativeSection):
            total += len(item.panels)
        else:
            total += 1
    return total


def sectionize(panels: list[NativePanel], size: int = MAX_SECTION_PANELS) -> list[NativeSection]:
    """Group flat leaf panels into synthetic sections to respect the item cap."""
    sections: list[NativeSection] = []
    for start in range(0, len(panels), size):
        chunk = panels[start : start + size]
        sections.append(
            NativeSection(
                title=f"Panels {start + 1}\u2013{start + len(chunk)}",
                collapsed=False,
                panels=chunk,
                grid=NativeGrid(y=start),
            )
        )
    return sections


def coalesce_loose_into_sections(items: list[NativeItem], max_items: int = MAX_SECTION_PANELS) -> list[NativeItem]:
    """Wrap consecutive loose leaf panels into synthetic sections, in order.

    Real sections pass through untouched; only loose (non-section) panels are
    grouped, so a mixed dashboard over the top-level cap keeps every panel
    instead of truncating them.
    """
    out: list[NativeItem] = []
    buffer: list[NativePanel] = []

    def _flush() -> None:
        if not buffer:
            return
        start = len(out)
        for chunk_start in range(0, len(buffer), max_items):
            chunk = buffer[chunk_start : chunk_start + max_items]
            out.append(
                NativeSection(
                    title=f"Panels {chunk_start + 1}\u2013{chunk_start + len(chunk)}",
                    collapsed=False,
                    panels=chunk,
                    grid=NativeGrid(y=start),
                )
            )
        buffer.clear()

    for item in items:
        if isinstance(item, NativeSection):
            _flush()
            out.append(item)
        else:
            buffer.append(item)
    _flush()
    return out


def _truncate_to_total_item_cap(items: list[NativeItem], max_total_items: int) -> tuple[list[NativeItem], int]:
    """Trim panels/sections in order so their combined API item count fits."""
    out: list[NativeItem] = []
    remaining = max_total_items

    for item in items:
        if remaining <= 0:
            break
        if isinstance(item, NativePanel):
            out.append(item)
            remaining -= 1
            continue

        # A section itself consumes one item, and each child panel consumes one
        # more under the API's combined dashboard limit.
        section_budget = remaining - 1
        if section_budget < 0:
            break
        kept_panels = item.panels[:section_budget]
        out.append(
            NativeSection(
                title=item.title,
                collapsed=item.collapsed,
                panels=kept_panels,
                grid=item.grid,
            )
        )
        remaining -= 1 + len(kept_panels)

    return out, dashboard_item_count(out)


@dataclass
class NativeDashboard:
    """In-memory model of one typed ``POST``/``PUT /api/dashboards`` payload."""

    title: str = "migrated dashboard"
    description: str = ""
    items: list[NativeItem] = field(default_factory=list)
    controls: list[NativeControl] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    # Dashboard-level tags. Kibana stores these and accepts plain strings on
    # create (verified on 9.5). They reach here straight from the IR, never via
    # the YAML document shape, whose schema forbids unknown keys.
    tags: list[str] = field(default_factory=list)
    dashboard_id: str = ""

    def enforce_item_cap(
        self,
        counts: NativeMappingCounts,
        *,
        max_items: int = MAX_DASHBOARD_ITEMS,
        max_section_panels: int = MAX_SECTION_PANELS,
        max_total_items: int = MAX_TOTAL_ITEMS,
    ) -> None:
        """Respect the API's top-level item cap in place, recording any drops."""
        has_sections = any(isinstance(item, NativeSection) for item in self.items)
        if not has_sections:
            if len(self.items) > max_items:
                leaves = [item for item in self.items if isinstance(item, NativePanel)]
                if max_total_items <= max_items:
                    dropped = max(0, len(leaves) - max_total_items)
                    if dropped:
                        counts.add_reason("dropped_over_item_cap", dropped)
                        counts.mapped = max(0, counts.mapped - dropped)
                    kept_leaves: list[NativeItem] = list(leaves[:max_total_items])
                    self.items = kept_leaves
                else:
                    self.items = list(sectionize(leaves, max_section_panels))
        elif len(self.items) > max_items:
            self.items = coalesce_loose_into_sections(self.items, max_section_panels)
        if len(self.items) > max_items:
            before_leaf_count = dashboard_leaf_panel_count(self.items)
            dropped = len(self.items) - max_items
            counts.add_reason("dropped_over_item_cap", dropped)
            self.items = self.items[:max_items]
            counts.mapped = max(0, counts.mapped - (before_leaf_count - dashboard_leaf_panel_count(self.items)))
        total_items = dashboard_item_count(self.items)
        if total_items > max_total_items:
            before_leaf_count = dashboard_leaf_panel_count(self.items)
            self.items, kept = _truncate_to_total_item_cap(self.items, max_total_items)
            counts.add_reason("dropped_over_total_item_cap", total_items - kept)
            counts.mapped = max(0, counts.mapped - (before_leaf_count - dashboard_leaf_panel_count(self.items)))

    def to_api_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": self.title,
            "panels": [item.to_api_dict() for item in self.items],
        }
        if self.description:
            payload["description"] = self.description
        if self.controls:
            payload["pinned_panels"] = [control.to_api_dict() for control in self.controls]
        if self.filters:
            payload["filters"] = self.filters
        if self.tags:
            payload["tags"] = list(self.tags)
        return payload


__all__ = [
    "MAX_DASHBOARD_ITEMS",
    "MAX_ITEMS",
    "MAX_PINNED_CONTROLS",
    "MAX_SECTION_PANELS",
    "MAX_TOTAL_ITEMS",
    "NativeControl",
    "NativeDashboard",
    "NativeGrid",
    "NativeItem",
    "NativeMappingCounts",
    "NativePanel",
    "NativeSection",
    "coalesce_loose_into_sections",
    "dashboard_item_count",
    "dashboard_leaf_panel_count",
    "sectionize",
]
