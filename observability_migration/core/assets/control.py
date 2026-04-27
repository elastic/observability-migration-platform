"""Canonical control IR — variables and template controls."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .status import AssetStatus


@dataclass
class ControlIR:
    """Source-agnostic variable / template control.

    Covers Grafana template variables and Datadog template_variables.
    """

    version: int = 1
    control_id: str = ""
    name: str = ""
    label: str = ""
    kind: str = ""
    default_value: str = ""
    query: str = ""
    datasource: str = ""

    status: AssetStatus = AssetStatus.SKIPPED
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_extension: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d
