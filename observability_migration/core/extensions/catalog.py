"""Shared extension catalog types for source adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


@dataclass
class ExtensionSurface:
    """A user-visible way an adapter can be extended."""

    id: str
    kind: str
    summary: str
    entrypoint: str = ""
    format: str = ""
    example_path: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class ExtensionRuleCard:
    """A stable description of a built-in rule or pass."""

    id: str
    stage: str
    summary: str
    registry: str = ""
    priority: int | None = None
    extenders: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtensionCatalog:
    """Cross-adapter extension contract for discovery and tooling."""

    adapter: str
    summary: str
    stages: list[str] = field(default_factory=list)
    current_surfaces: list[ExtensionSurface] = field(default_factory=list)
    planned_surfaces: list[ExtensionSurface] = field(default_factory=list)
    rules: list[ExtensionRuleCard] = field(default_factory=list)
    template: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(asdict(self))


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return _serialize(asdict(value))
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


__all__ = [
    "ExtensionCatalog",
    "ExtensionRuleCard",
    "ExtensionSurface",
]
