"""Canonical visual IR — panel presentation and layout.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


@dataclass
class VisualLayout:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0


@dataclass
class VisualPresentation:
    kind: str = ""
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class VisualIR:
    version: int = 1
    title: str = ""
    source_panel_id: str = ""
    grafana_type: str = ""
    kibana_type: str = ""
    layout: VisualLayout = field(default_factory=VisualLayout)
    presentation: VisualPresentation = field(default_factory=VisualPresentation)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_yaml_panel(self) -> dict[str, Any]:
        panel: dict[str, Any] = {}
        if self.title:
            panel["title"] = self.title
        panel["size"] = {"w": self.layout.w, "h": self.layout.h}
        panel["position"] = {"x": self.layout.x, "y": self.layout.y}
        if self.presentation.kind and self.presentation.config:
            panel[self.presentation.kind] = dict(self.presentation.config)
        return panel

    @classmethod
    def from_yaml_panel(
        cls,
        yaml_panel: dict[str, Any] | None,
        *,
        source_panel_id: str = "",
        grafana_type: str = "",
        kibana_type: str = "",
        warnings: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VisualIR:
        yaml_panel = yaml_panel or {}
        size = yaml_panel.get("size") if isinstance(yaml_panel.get("size"), dict) else {}
        position = yaml_panel.get("position") if isinstance(yaml_panel.get("position"), dict) else {}

        kind = ""
        config: dict[str, Any] = {}
        if isinstance(yaml_panel.get("esql"), dict):
            kind = "esql"
            config = dict(yaml_panel["esql"])
        elif isinstance(yaml_panel.get("markdown"), dict):
            kind = "markdown"
            config = dict(yaml_panel["markdown"])

        resolved_kibana_type = kibana_type or str(config.get("type") or "")
        return cls(
            title=str(yaml_panel.get("title") or ""),
            source_panel_id=str(source_panel_id or ""),
            grafana_type=str(grafana_type or ""),
            kibana_type=str(resolved_kibana_type or ""),
            layout=VisualLayout(
                x=_safe_int(position.get("x", 0) or 0),
                y=_safe_int(position.get("y", 0) or 0),
                w=_safe_int(size.get("w", 0) or 0),
                h=_safe_int(size.get("h", 0) or 0),
            ),
            presentation=VisualPresentation(
                kind=kind,
                config=config,
            ),
            warnings=[str(item) for item in (warnings or [])],
            metadata=dict(metadata or {}),
        )


def refresh_visual_ir(panel_result: Any, yaml_panel: dict[str, Any] | None) -> VisualIR:
    if not yaml_panel:
        return VisualIR()
    query_ir = getattr(panel_result, "query_ir", {}) or {}
    if not isinstance(query_ir, dict):
        query_ir = {}
    return VisualIR.from_yaml_panel(
        yaml_panel,
        source_panel_id=str(getattr(panel_result, "source_panel_id", "") or ""),
        grafana_type=str(getattr(panel_result, "grafana_type", "") or ""),
        kibana_type=str(getattr(panel_result, "kibana_type", "") or ""),
        warnings=[str(item) for item in (getattr(panel_result, "reasons", []) or [])],
        metadata={
            "query_language": str(getattr(panel_result, "query_language", "") or ""),
            "output_shape": str(query_ir.get("output_shape", "") or ""),
        },
    )
