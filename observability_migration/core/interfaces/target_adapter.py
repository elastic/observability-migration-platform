"""Abstract base for target adapters (Kibana, future targets)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class TargetAdapter(ABC):
    """Contract for target-side emission, compilation, and validation."""

    name: str  # e.g. "kibana"

    @abstractmethod
    def emit_dashboard(self, dashboard_ir: Any, output_dir: Path, **kwargs: Any) -> Path:
        """Emit a dashboard artifact (e.g. YAML) and return the output path."""

    @abstractmethod
    def compile(self, yaml_dir: Path, output_dir: Path, **kwargs: Any) -> dict[str, Any]:
        """Compile emitted artifacts and return a structured summary."""

    @abstractmethod
    def validate_queries(self, run_dir: Path, **kwargs: Any) -> dict[str, Any]:
        """Validate emitted queries against the live target; return a summary."""

    @abstractmethod
    def upload(self, compiled_dir: Path, **kwargs: Any) -> dict[str, Any]:
        """Upload compiled artifacts to the target and return a structured summary."""

    @abstractmethod
    def smoke(self, **kwargs: Any) -> dict[str, Any]:
        """Run post-upload smoke validation and return a structured summary."""
