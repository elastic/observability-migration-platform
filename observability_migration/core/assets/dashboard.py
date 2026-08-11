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

    # Extra token appended to the title-slug Kibana dashboard id when another
    # dashboard in the same run carries the same title. Empty for the common
    # unique-title case, which is what keeps every already-uploaded dashboard's
    # id byte-identical -- the id is the upsert key, so changing it orphans the
    # uploaded copy (see
    # ``targets/kibana/dashboards_api.py::_stable_dashboard_id_from_ir``).
    # Allocated together with the artifact stem so the two agree: artifact
    # ``shared_title_dash-beta`` carries id ``obs-migrate-shared-title-dash-beta``.
    id_disambiguator: str = ""

    description: str = ""
    filters: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    minimum_kibana_version: str = ""

    # Dashboard-level ``time_range``/``refresh_interval`` the typed Dashboards
    # API accepts (``{"from", "to", "mode"}`` / ``{"pause", "value"}``). Read
    # straight off the IR by ``native_dashboard_from_ir`` rather than through
    # :meth:`to_yaml_dict`, same discipline as ``tags``: the kb-dashboard-core
    # YAML schema has no ``refresh_interval`` at all and only a partial
    # (mode-less) ``time_range``, so routing these through it would silently
    # drop what the API supports (see
    # ``targets/kibana/dashboards_api.py::native_dashboard_from_ir``).
    time_range: dict[str, Any] = field(default_factory=dict)
    refresh_interval: dict[str, Any] = field(default_factory=dict)

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

    @classmethod
    def from_dict(cls, raw: Any) -> DashboardIR:
        """Rebuild a :class:`DashboardIR` from its :meth:`to_dict` form.

        Inverse of :meth:`to_dict`. This is the *import* direction of the
        on-disk IR artifact (``ir/<stem>.ir.json``, written by
        ``targets/kibana/native_artifacts.py::write_ir_artifact``): every
        in-repo tool that used to read the dashboard YAML off disk now reads
        that artifact and rebuilds the IR here, so the artifact readers and
        the migration share one definition of what a dashboard is.

        Restores dashboard identity, ``panels`` (with layout + presentation),
        ``controls``, ``filters`` and ``settings`` -- everything
        :meth:`to_yaml_dict` and the artifact readers consume. The referenced
        asset collections (``alerts``/``annotations``/``links``/
        ``transforms``) are left empty: no artifact reader consumes them, and
        they are exported through :meth:`to_dict` rather than through this
        path.
        """
        raw = raw if isinstance(raw, dict) else {}
        settings = raw.get("settings")
        metadata = raw.get("metadata")
        source_extension = raw.get("source_extension")
        time_range = raw.get("time_range")
        refresh_interval = raw.get("refresh_interval")
        return cls(
            title=str(raw.get("title") or ""),
            uid=str(raw.get("uid") or ""),
            source_adapter=str(raw.get("source_adapter") or ""),
            source_file=str(raw.get("source_file") or ""),
            folder=str(raw.get("folder") or ""),
            tags=[str(tag) for tag in (raw.get("tags") or [])],
            # Part of dashboard identity: without it a re-upload from the
            # persisted IR would resolve a disambiguated dashboard back to the
            # plain title slug and overwrite its same-titled sibling.
            id_disambiguator=str(raw.get("id_disambiguator") or ""),
            description=str(raw.get("description") or ""),
            filters=[item for item in (raw.get("filters") or []) if isinstance(item, dict)],
            settings=dict(settings) if isinstance(settings, dict) else {},
            minimum_kibana_version=str(raw.get("minimum_kibana_version") or ""),
            time_range=dict(time_range) if isinstance(time_range, dict) else {},
            refresh_interval=dict(refresh_interval) if isinstance(refresh_interval, dict) else {},
            panels=[
                PanelIR.from_dict(panel)
                for panel in (raw.get("panels") or [])
                if isinstance(panel, dict)
            ],
            controls=[
                ControlIR.from_dict(control)
                for control in (raw.get("controls") or [])
                if isinstance(control, dict)
            ],
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
            source_extension=dict(source_extension) if isinstance(source_extension, dict) else {},
        )

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
