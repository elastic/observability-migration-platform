"""Adapter registries — central lookup for source and target adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class _AdapterRegistry:
    """Simple name-keyed adapter registry."""

    def __init__(self, label: str) -> None:
        self._label = label
        self._adapters: dict[str, type] = {}

    def register(self, adapter_cls: type) -> type:
        """Register an adapter class (decorator-friendly)."""
        name = getattr(adapter_cls, "name", None)
        if not name:
            raise ValueError(f"{self._label} adapter must define a 'name' class attribute")
        if name in self._adapters:
            raise ValueError(f"{self._label} adapter '{name}' already registered")
        self._adapters[name] = adapter_cls
        return adapter_cls

    def get(self, name: str) -> type:
        if name not in self._adapters:
            available = ", ".join(sorted(self._adapters)) or "(none)"
            raise KeyError(f"Unknown {self._label} adapter '{name}'. Available: {available}")
        return self._adapters[name]

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def __contains__(self, name: str) -> bool:
        return name in self._adapters


source_registry: _AdapterRegistry = _AdapterRegistry("source")
target_registry: _AdapterRegistry = _AdapterRegistry("target")
