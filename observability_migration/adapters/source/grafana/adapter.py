# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana source adapter.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from observability_migration.core.interfaces.registries import source_registry
from observability_migration.core.interfaces.source_adapter import SourceAdapter


@source_registry.register
class GrafanaAdapter(SourceAdapter):
    name = "grafana"

    @property
    def supported_assets(self) -> Sequence[str]:
        return [
            "dashboards", "panels", "queries", "controls",
            "alerts", "annotations", "links", "transforms",
        ]

    @property
    def supported_input_modes(self) -> Sequence[str]:
        return ["files", "api"]

    def validate_credentials(self, config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        input_mode = config.get("input_mode", "files")
        if input_mode == "api" and not config.get("grafana_url"):
            errors.append("grafana_url is required for API mode")
        return errors

    def extract_dashboards(
        self,
        *,
        input_mode: str,
        input_dir: Path | None = None,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        from .extract import (
            extract_dashboards_from_files,
            extract_dashboards_from_grafana,
        )
        config = config or {}
        if input_mode == "api":
            return extract_dashboards_from_grafana(
                grafana_url=config.get("grafana_url", ""),
                user=config.get("grafana_user", ""),
                password=config.get("grafana_pass", ""),
            )
        return extract_dashboards_from_files(str(input_dir or ""))

    def normalize_dashboard(self, raw: dict[str, Any], **kwargs: Any) -> Any:
        return raw

    def translate_queries(self, normalized: Any, **kwargs: Any) -> Any:
        return normalized

    def build_extension_catalog(self, **kwargs: Any) -> dict[str, Any]:
        from . import panels as _panels  # noqa: F401
        from . import translate as _translate  # noqa: F401
        from .rules import build_rule_catalog, load_rule_pack_files

        rule_pack = kwargs.get("rule_pack") or load_rule_pack_files([])
        return build_rule_catalog(rule_pack)

    def build_extension_template(self, **kwargs: Any) -> dict[str, Any]:
        from .rules import build_rule_pack_template

        return build_rule_pack_template()
