"""Shared asset-status vocabulary used across all sources and reports."""

from __future__ import annotations

from enum import Enum


class AssetStatus(str, Enum):
    """Unified status for any migrated asset.

    Maps from source-specific vocabularies:
      Grafana:  migrated -> TRANSLATED, migrated_with_warnings -> TRANSLATED_WITH_WARNINGS,
                requires_manual -> MANUAL_REQUIRED, not_feasible -> NOT_FEASIBLE
      Datadog:  ok -> TRANSLATED, warning -> TRANSLATED_WITH_WARNINGS,
                blocked -> NOT_FEASIBLE
    """

    TRANSLATED = "translated"
    TRANSLATED_WITH_WARNINGS = "translated_with_warnings"
    DRAFT_REVIEW = "draft_review"
    MANUAL_REQUIRED = "manual_required"
    NOT_FEASIBLE = "not_feasible"
    BLOCKED = "blocked"
    SKIPPED = "skipped"

    @classmethod
    def from_grafana(cls, status: str) -> AssetStatus:
        _map = {
            "migrated": cls.TRANSLATED,
            "migrated_with_warnings": cls.TRANSLATED_WITH_WARNINGS,
            "requires_manual": cls.MANUAL_REQUIRED,
            "not_feasible": cls.NOT_FEASIBLE,
        }
        return _map.get(status, cls.NOT_FEASIBLE)

    @classmethod
    def from_datadog(cls, status: str) -> AssetStatus:
        _map = {
            "ok": cls.TRANSLATED,
            "warning": cls.TRANSLATED_WITH_WARNINGS,
            "blocked": cls.NOT_FEASIBLE,
        }
        return _map.get(status, cls.NOT_FEASIBLE)
