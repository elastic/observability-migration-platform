"""Datadog source adapter."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from observability_migration.core.interfaces.registries import source_registry
from observability_migration.core.interfaces.source_adapter import SourceAdapter


@source_registry.register
class DatadogAdapter(SourceAdapter):
    name = "datadog"

    @property
    def supported_assets(self) -> Sequence[str]:
        return [
            "dashboards", "panels", "queries", "controls",
        ]

    @property
    def supported_input_modes(self) -> Sequence[str]:
        return ["files", "api"]

    def validate_credentials(self, config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        input_mode = config.get("input_mode", "files")
        if input_mode == "api":
            if not config.get("dd_api_key"):
                errors.append("dd_api_key is required for API mode")
            if not config.get("dd_app_key"):
                errors.append("dd_app_key is required for API mode")
        return errors

    def extract_dashboards(
        self,
        *,
        input_mode: str,
        input_dir: Path | None = None,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        from .extract import (
            extract_dashboards_from_api,
            extract_dashboards_from_files,
        )
        config = config or {}
        if input_mode == "api":
            return extract_dashboards_from_api(
                api_key=config.get("dd_api_key", ""),
                app_key=config.get("dd_app_key", ""),
                dashboard_ids=config.get("dashboard_ids", []),
            )
        return extract_dashboards_from_files(str(input_dir or ""))

    def normalize_dashboard(self, raw: dict[str, Any], **kwargs: Any) -> Any:
        from .normalize import normalize_dashboard
        return normalize_dashboard(raw)

    def translate_queries(self, normalized: Any, **kwargs: Any) -> Any:
        return normalized

    def build_extension_catalog(self, **kwargs: Any) -> dict[str, Any]:
        from . import planner as _planner  # noqa: F401
        from . import translate as _translate  # noqa: F401
        from .rules import build_rule_catalog

        return build_rule_catalog()

    def build_extension_template(self, **kwargs: Any) -> dict[str, Any]:
        from .rules import build_extension_template

        return build_extension_template()
