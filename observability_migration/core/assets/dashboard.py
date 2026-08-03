# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Canonical dashboard IR — the run-level asset container."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .alerting import AlertingIR
from .annotation import AnnotationIR
from .control import ControlIR
from .link import LinkIR
from .panel import PanelIR
from .transform import TransformIR


@dataclass
class DashboardIR:
    """Source-agnostic dashboard container.

    Holds panels, controls, annotations, links, alerting assets,
    transforms, source lineage, and rollout metadata.
    """

    version: int = 1
    title: str = ""
    uid: str = ""
    source_adapter: str = ""
    source_file: str = ""
    folder: str = ""
    tags: list[str] = field(default_factory=list)

    description: str = ""
    filters: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    minimum_kibana_version: str = ""

    panels: list[PanelIR] = field(default_factory=list)
    controls: list[ControlIR] = field(default_factory=list)
    alerts: list[AlertingIR] = field(default_factory=list)
    annotations: list[AnnotationIR] = field(default_factory=list)
    links: list[LinkIR] = field(default_factory=list)
    transforms: list[TransformIR] = field(default_factory=list)

    metadata: dict[str, Any] = field(default_factory=dict)
    source_extension: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialize to one kb-dashboard-core ``dashboards[]`` entry.

        This is the export direction of the IR-first pipeline: for Grafana,
        YAML is written *from* this semantic IR (see
        ``adapters/source/grafana/panels.py::translate_dashboard`` and
        ``docs/architecture/asset-model.md``), not the other way around. The
        native Dashboards API payload is derived from the same IR via
        ``targets/kibana/dashboards_api.py::native_dashboard_from_ir``, which
        reuses this exact dict shape so both outputs can never drift from
        each other.
        """
        dashboard: dict[str, Any] = {"name": self.title}
        if self.description:
            dashboard["description"] = self.description
        if self.minimum_kibana_version:
            dashboard["minimum_kibana_version"] = self.minimum_kibana_version
        if self.settings:
            dashboard["settings"] = dict(self.settings)
        dashboard["panels"] = [panel.to_yaml_panel_entry() for panel in self.panels]
        if self.filters:
            dashboard["filters"] = list(self.filters)
        if self.controls:
            mapped_controls = [control.to_yaml_control() for control in self.controls]
            dashboard["controls"] = [control for control in mapped_controls if control]
        return dashboard

    @classmethod
    def from_yaml_dict(cls, dashboard: dict[str, Any], *, source_adapter: str = "") -> DashboardIR:
        """Build a :class:`DashboardIR` from one kb-dashboard-core
        ``dashboards[]`` entry. Inverse of :meth:`to_yaml_dict`."""
        dashboard = dashboard if isinstance(dashboard, dict) else {}
        panels = [
            PanelIR.from_yaml_panel_entry(entry, panel_id=str(idx))
            for idx, entry in enumerate(dashboard.get("panels") or [])
            if isinstance(entry, dict)
        ]
        controls = [
            ControlIR.from_yaml_control(control)
            for control in (dashboard.get("controls") or [])
            if isinstance(control, dict)
        ]
        settings = dashboard.get("settings")
        return cls(
            title=str(dashboard.get("name") or dashboard.get("title") or ""),
            source_adapter=source_adapter,
            description=str(dashboard.get("description") or ""),
            minimum_kibana_version=str(dashboard.get("minimum_kibana_version") or ""),
            settings=dict(settings) if isinstance(settings, dict) else {},
            panels=panels,
            controls=controls,
            filters=[item for item in (dashboard.get("filters") or []) if isinstance(item, dict)],
        )
