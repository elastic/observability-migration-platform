"""Canonical link IR — dashboard and panel links / drilldowns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .status import AssetStatus


@dataclass
class LinkIR:
    """Source-agnostic link / drilldown asset."""

    version: int = 1
    link_id: str = ""
    title: str = ""
    kind: str = ""
    scope: str = ""
    original_url: str = ""
    translated_url: str = ""

    status: AssetStatus = AssetStatus.MANUAL_REQUIRED
    manual_required: bool = True
    target_candidate: str = ""
    losses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_extension: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d
