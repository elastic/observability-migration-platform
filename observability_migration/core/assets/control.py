# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Canonical control IR — variables and template controls."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .status import AssetStatus


@dataclass
class ControlIR:
    """Source-agnostic variable / template control.

    Covers Grafana template variables and Datadog template_variables, and
    maps onto the Dashboards API's ``pinned_panels`` controls (options-list,
    range-slider, and ES|QL ``esql_control``) -- see
    ``targets/kibana/dashboards_api.py::map_yaml_control``.
    """

    version: int = 1
    control_id: str = ""
    name: str = ""
    label: str = ""
    kind: str = ""
    default_value: str = ""
    query: str = ""
    datasource: str = ""

    variable_name: str = ""
    variable_type: str = ""
    data_view: str = ""
    field_name: str = ""
    selected_options: list[str] = field(default_factory=list)
    available_options: list[str] = field(default_factory=list)
    multiple: bool = True

    status: AssetStatus = AssetStatus.SKIPPED
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_extension: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    def to_yaml_control(self) -> dict[str, Any]:
        """Serialize back to one kb-dashboard-core ``controls[]`` entry.

        Controls carry many translator-specific keys (see
        ``adapters/source/grafana/panels.py::translate_variables``), so when
        built from a raw dict (:meth:`from_yaml_control`) that dict is kept
        verbatim in ``source_extension`` and only overlaid with fields a
        mutator may have changed since (currently just ``label``, for
        metadata-polish control-label rewrites) -- this guarantees no
        translator-specific key is silently dropped on the way back out.
        A control built purely from typed fields (no ``source_extension``,
        e.g. a synthesized ES|QL binding control) is instead assembled from
        those fields directly.
        """
        if self.source_extension:
            control = dict(self.source_extension)
        else:
            control = {"type": self.kind or "esql"}
            if self.variable_name:
                control["variable_name"] = self.variable_name
            if self.variable_type:
                control["variable_type"] = self.variable_type
            if self.data_view:
                control["data_view_id"] = self.data_view
            if self.field_name:
                control["field_name"] = self.field_name
            if self.query:
                control["query"] = self.query
            if self.available_options:
                control["available_options"] = list(self.available_options)
            if self.selected_options:
                control["defaults"] = list(self.selected_options)
            if not self.multiple:
                control["multiple"] = False
        if self.label:
            control["label"] = self.label
        return control

    @classmethod
    def from_yaml_control(cls, raw: dict[str, Any]) -> ControlIR:
        """Build a :class:`ControlIR` from one kb-dashboard-core
        ``controls[]`` entry. Inverse of :meth:`to_yaml_control`."""
        raw = raw if isinstance(raw, dict) else {}
        label = str(raw.get("label") or raw.get("title") or "")
        variable_name = str(raw.get("variable_name") or "")
        field_name = str(raw.get("field_name") or raw.get("field") or "")
        data_view = str(raw.get("data_view_id") or raw.get("data_view") or "")
        kind = str(raw.get("type") or "")
        query = str(raw.get("query") or "")

        defaults_raw = raw.get("defaults") if "defaults" in raw else raw.get("default")
        if defaults_raw is None:
            defaults_raw = raw.get("selected_options")
        if defaults_raw is None:
            defaults_raw = raw.get("preselected")
        if isinstance(defaults_raw, list):
            selected_options = [str(item) for item in defaults_raw]
        elif defaults_raw is not None:
            selected_options = [str(defaults_raw)]
        else:
            selected_options = []

        available_raw = raw.get("available_options") or raw.get("options") or []
        available_options = [str(item) for item in available_raw] if isinstance(available_raw, list) else []

        return cls(
            control_id=variable_name or field_name,
            name=variable_name or field_name,
            label=label,
            kind=kind,
            query=query,
            datasource=data_view,
            variable_name=variable_name,
            variable_type=str(raw.get("variable_type") or ""),
            data_view=data_view,
            field_name=field_name,
            selected_options=selected_options,
            available_options=available_options,
            multiple=raw.get("multiple", True) is not False,
            status=AssetStatus.TRANSLATED,
            source_extension=dict(raw),
        )
