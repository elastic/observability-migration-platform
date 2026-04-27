"""Adapter interfaces and registries."""

from .registries import source_registry, target_registry
from .source_adapter import SourceAdapter
from .target_adapter import TargetAdapter

__all__ = [
    "SourceAdapter",
    "TargetAdapter",
    "source_registry",
    "target_registry",
]
