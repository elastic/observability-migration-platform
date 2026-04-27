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
