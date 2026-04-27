"""Abstract base for source adapters (Grafana, Datadog, future sources)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class SourceAdapter(ABC):
    """Contract every source adapter must implement.

    A source adapter is responsible for:
    1. Extracting raw dashboards from files or an API.
    2. Normalizing vendor-specific shapes into adapter-defined normalized models
       or canonical asset IRs.
    3. Translating vendor-specific queries into target query plans or other
       adapter-defined translated output.
    """

    name: str  # e.g. "grafana", "datadog"

    # ----- capabilities -----

    @property
    @abstractmethod
    def supported_assets(self) -> Sequence[str]:
        """Asset kinds this adapter can produce (e.g. dashboards, controls, alerts)."""

    @property
    @abstractmethod
    def supported_input_modes(self) -> Sequence[str]:
        """Input modes this adapter supports (e.g. files, api)."""

    # ----- credentials -----

    @abstractmethod
    def validate_credentials(self, config: dict[str, Any]) -> list[str]:
        """Return a list of missing-credential errors, empty if OK."""

    # ----- extraction -----

    @abstractmethod
    def extract_dashboards(
        self,
        *,
        input_mode: str,
        input_dir: Path | None = None,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return raw dashboard dicts from the source."""

    # ----- normalization -----

    @abstractmethod
    def normalize_dashboard(
        self,
        raw: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        """Normalize a raw dashboard dict into an adapter-defined normalized form or canonical IR."""

    # ----- query translation -----

    @abstractmethod
    def translate_queries(
        self,
        normalized: Any,
        **kwargs: Any,
    ) -> Any:
        """Translate queries from the normalized dashboard into target query plans or adapter-specific translated output."""

    def build_extension_catalog(self, **kwargs: Any) -> dict[str, Any]:
        """Return a machine-readable description of adapter extension points."""
        return {
            "adapter": self.name,
            "summary": f"No extension catalog has been declared for '{self.name}' yet.",
            "stages": [],
            "current_surfaces": [],
            "planned_surfaces": [],
            "rules": [],
            "template": {},
            "metadata": {},
        }

    def build_extension_template(self, **kwargs: Any) -> dict[str, Any]:
        """Return a starter declarative extension template for the adapter."""
        return {}
