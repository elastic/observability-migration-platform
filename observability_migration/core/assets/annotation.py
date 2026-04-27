"""Canonical annotation IR — timeline annotations and events."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .status import AssetStatus


@dataclass
class AnnotationIR:
    """Source-agnostic annotation / event asset."""

    version: int = 1
    annotation_id: str = ""
    name: str = ""
    kind: str = ""
    source_query: str = ""
    source_datasource: str = ""

    status: AssetStatus = AssetStatus.MANUAL_REQUIRED
    manual_required: bool = True
    target_candidate: str = ""
    losses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    source_extension: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d
