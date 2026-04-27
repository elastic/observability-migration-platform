"""Shared test fixtures for the migration test suite.

Provides reusable panel builders, mock resolvers, and context factories
used across test modules.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver


def make_grafana_panel(
    expr: str = "up",
    panel_type: str = "timeseries",
    datasource_type: str = "prometheus",
    title: str = "Test Panel",
    grid_pos: dict[str, int] | None = None,
    extra_targets: list[dict[str, Any]] | None = None,
    **extra_fields: Any,
) -> dict[str, Any]:
    """Build a minimal Grafana panel dict for testing."""
    pos = grid_pos or {"x": 0, "y": 0, "w": 12, "h": 8}
    targets = [{"expr": expr, "refId": "A"}]
    if extra_targets:
        targets.extend(extra_targets)
    panel: dict[str, Any] = {
        "type": panel_type,
        "title": title,
        "datasource": {"type": datasource_type, "uid": "prom1"},
        "targets": targets,
        "gridPos": pos,
    }
    panel.update(extra_fields)
    return panel


def make_datadog_widget(
    widget_type: str = "timeseries",
    title: str = "Test Widget",
    queries: list[dict[str, Any]] | None = None,
    layout: dict[str, Any] | None = None,
    **extra_fields: Any,
) -> dict[str, Any]:
    """Build a minimal Datadog widget dict for testing."""
    widget: dict[str, Any] = {
        "definition": {
            "type": widget_type,
            "title": title,
            "requests": queries or [],
        },
        "layout": layout or {"x": 0, "y": 0, "width": 4, "height": 2},
    }
    widget["definition"].update(extra_fields)
    return widget


def default_rule_pack() -> RulePackConfig:
    """Return a default RulePackConfig for testing."""
    return RulePackConfig()


def default_resolver(rule_pack: RulePackConfig | None = None) -> SchemaResolver:
    """Return a SchemaResolver using default rules."""
    return SchemaResolver(rule_pack or default_rule_pack())
