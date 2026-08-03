# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Canonical panel IR — the visual asset unit."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .query import QueryIR
from .status import AssetStatus
from .visual import VisualIR


@dataclass
class PanelIR:
    """Source-agnostic panel / widget representation.

    A panel is either a leaf (``kind == "panel"``, carrying its own
    ``visual``/``query``) or a nested group (``kind == "section"``, carrying
    ``children`` -- the semantic equivalent of a kb-dashboard-core YAML
    ``section`` block / Grafana row). ``visual``/``query`` are embedded
    directly (rather than only referenced via ``query_ids``) so a
    :class:`DashboardIR` carries everything the native Dashboards API mapper
    needs without going back through YAML -- see
    ``targets/kibana/dashboards_api.py::native_dashboard_from_ir``.
    """

    version: int = 1
    panel_id: str = ""
    title: str = ""
    source_type: str = ""
    target_type: str = ""
    status: AssetStatus = AssetStatus.SKIPPED

    kind: str = "panel"
    hide_title: bool = False
    collapsed: bool = False
    children: list[PanelIR] = field(default_factory=list)
    visual: VisualIR | None = None
    query: QueryIR | None = None

    query_ids: list[str] = field(default_factory=list)
    transform_ids: list[str] = field(default_factory=list)
    link_ids: list[str] = field(default_factory=list)
    annotation_ids: list[str] = field(default_factory=list)
    alert_ids: list[str] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)
    semantic_losses: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_extension: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, raw: Any) -> PanelIR:
        """Rebuild a :class:`PanelIR` tree from its :meth:`to_dict` form.

        Inverse of :meth:`to_dict`, used to read back the on-disk
        ``ir/<stem>.ir.json`` artifact. ``children`` and ``visual`` are
        rehydrated recursively; the embedded :class:`QueryIR` is not, because
        every artifact reader works from ``visual.presentation`` (the panel's
        emitted query) and ``query`` duplicates translator bookkeeping that no
        reader consumes. Anything not restored here still round-trips through
        :meth:`to_dict` untouched.
        """
        raw = raw if isinstance(raw, dict) else {}
        try:
            status = AssetStatus(str(raw.get("status") or ""))
        except ValueError:
            status = AssetStatus.SKIPPED
        children = [
            cls.from_dict(child)
            for child in (raw.get("children") or [])
            if isinstance(child, dict)
        ]
        visual_raw = raw.get("visual")
        metadata = raw.get("metadata")
        source_extension = raw.get("source_extension")
        return cls(
            panel_id=str(raw.get("panel_id") or ""),
            title=str(raw.get("title") or ""),
            source_type=str(raw.get("source_type") or ""),
            target_type=str(raw.get("target_type") or ""),
            status=status,
            kind=str(raw.get("kind") or "panel"),
            hide_title=bool(raw.get("hide_title")),
            collapsed=bool(raw.get("collapsed")),
            children=children,
            visual=VisualIR.from_dict(visual_raw) if isinstance(visual_raw, dict) else None,
            warnings=[str(item) for item in (raw.get("warnings") or [])],
            semantic_losses=[str(item) for item in (raw.get("semantic_losses") or [])],
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
            source_extension=dict(source_extension) if isinstance(source_extension, dict) else {},
        )

    def to_yaml_panel_entry(self) -> dict[str, Any]:
        """Serialize back to one kb-dashboard-core ``panels[]`` entry.

        Inverse of :meth:`from_yaml_panel_entry`. A section panel becomes a
        ``{"title": ..., "section": {...}}`` block; a leaf panel is rendered
        from its embedded :class:`VisualIR` (``VisualIR.to_yaml_panel``),
        which already round-trips ``size``/``position`` and all supported
        presentation blocks (``esql``/``lens``/``markdown``/``links``/``image``).
        """
        if self.kind == "section":
            return {
                "title": self.title,
                "section": {
                    "collapsed": self.collapsed,
                    "panels": [child.to_yaml_panel_entry() for child in self.children],
                },
            }
        entry = self.visual.to_yaml_panel() if self.visual is not None else {"title": self.title}
        if self.hide_title:
            entry["hide_title"] = True
        return entry

    @classmethod
    def from_yaml_panel_entry(cls, entry: dict[str, Any], *, panel_id: str = "") -> PanelIR:
        """Build a :class:`PanelIR` from one kb-dashboard-core ``panels[]``
        entry (leaf or nested ``section``). Inverse of
        :meth:`to_yaml_panel_entry`."""
        entry = entry if isinstance(entry, dict) else {}
        section = entry.get("section")
        if isinstance(section, dict):
            children = [
                cls.from_yaml_panel_entry(sub, panel_id=f"{panel_id}.{idx}" if panel_id else str(idx))
                for idx, sub in enumerate(section.get("panels") or [])
                if isinstance(sub, dict)
            ]
            return cls(
                panel_id=panel_id,
                title=str(entry.get("title") or ""),
                kind="section",
                collapsed=bool(section.get("collapsed", False)),
                children=children,
                status=AssetStatus.TRANSLATED,
            )
        visual = VisualIR.from_yaml_panel(entry)
        return cls(
            panel_id=panel_id,
            title=visual.title,
            kind="panel",
            hide_title=bool(entry.get("hide_title")),
            visual=visual,
            status=AssetStatus.TRANSLATED,
        )
