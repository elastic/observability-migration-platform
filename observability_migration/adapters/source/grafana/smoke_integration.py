"""Backward-compatible import path for Kibana smoke integration helpers."""

from observability_migration.targets.kibana.smoke_integration import (
    load_smoke_report,
    merge_smoke_into_results,
)

__all__ = ["load_smoke_report", "merge_smoke_into_results"]
