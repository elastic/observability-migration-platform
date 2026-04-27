"""Canonical panel IR — the visual asset unit."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .status import AssetStatus


@dataclass
class PanelIR:
    """Source-agnostic panel / widget representation."""

    version: int = 1
    panel_id: str = ""
    title: str = ""
    source_type: str = ""
    target_type: str = ""
    status: AssetStatus = AssetStatus.SKIPPED

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
