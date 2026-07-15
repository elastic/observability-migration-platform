# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Parity tests between the production and verifier report->API panel mappers.

``observability_migration.targets.kibana.dashboards_api.map_panel`` (the
production mapper used by ``obs-migrate upload``) and
``parity-rig/verifier/dashboards_api.py``'s ``api_panel_from_report_panel``
(the standalone conformance oracle used by the corpus/benchmark gates) are two
independently maintained implementations of the same
``migration_report.json`` panel -> typed Dashboards API panel mapping. They
evolved separately and can silently drift apart -- e.g. one gaining a display
field (duration format defaults, axis/legend handling) the other lacks. This
module asserts they produce byte-identical ``{grid, type, config}`` shapes for
a representative sample of report panels spanning every chart family both
mappers claim to support, so a future change to one mapper without the other
fails CI instead of surfacing as a live-only oracle/production disagreement.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from observability_migration.targets.kibana import dashboards_api as production

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import dashboards_api as oracle  # noqa: E402


def _panel(
    *,
    title: str = "Requests",
    kind: str = "esql",
    layout: dict[str, int] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "visual_ir": {
            "layout": layout or {"x": 1, "y": 2, "w": 24, "h": 8},
            "presentation": {"kind": kind, "config": config or {}},
        },
    }


# Representative panels spanning every chart family + display-metadata option
# both mappers claim to support. Each config is a plausible emitted
# ``visual_ir.presentation.config`` -- the exact shape ``adapters/source/*``
# writes into ``migration_report.json``.
_REPRESENTATIVE_PANELS: list[tuple[str, dict[str, Any]]] = [
    (
        "markdown",
        _panel(kind="markdown", config={"content": "# hello\nworld"}),
    ),
    (
        "links",
        _panel(
            title="Navigation",
            kind="links",
            config={
                "layout": "horizontal",
                "items": [
                    {
                        "label": "Runbook",
                        "url": "https://example.com/runbook",
                        "new_tab": False,
                    }
                ],
            },
        ),
    ),
    (
        "image",
        _panel(
            title="Architecture",
            kind="image",
            config={
                "from_url": "https://example.com/architecture.png",
                "fit": "cover",
                "description": "System architecture",
            },
        ),
    ),
    (
        "xy_line_with_breakdown",
        _panel(
            config={
                "type": "line",
                "query": "FROM metrics-* | STATS value = AVG(metric) BY time_bucket, service.name",
                "dimension": {"field": "time_bucket", "label": "Time"},
                "metrics": [{"field": "value", "label": "Requests", "format": {"type": "number", "decimals": 1}}],
                "breakdown": {"field": "service.name", "label": "Service"},
                "legend": {"visibility": "hidden", "position": "right"},
            }
        ),
    ),
    (
        "xy_bar_horizontal_stacked",
        _panel(
            config={
                "type": "bar",
                "mode": "stacked",
                "horizontal": True,
                "query": "FROM metrics-* | STATS value = SUM(metric) BY handler",
                "dimension": {"field": "handler"},
                "metrics": [
                    {"field": "a", "color": {"type": "static", "color": "#54B399"}},
                    {"field": "b", "axis": "y2"},
                ],
            }
        ),
    ),
    (
        "metric_primary_secondary",
        _panel(
            config={
                "type": "metric",
                "query": "FROM metrics-* | STATS value = LAST(metric)",
                "primary": {"field": "requests", "label": "Requests", "format": {"type": "number", "decimals": 0}},
                "secondary": {"field": "error_rate", "label": "Error rate", "format": {"type": "percent"}},
            }
        ),
    ),
    (
        "gauge_with_bounds",
        _panel(
            config={
                "type": "gauge",
                "query": "FROM metrics-* | STATS value = AVG(metric)",
                "metric": {"field": "memory_pct", "format": {"type": "percent", "decimals": 1}},
                "minimum": {"field": "min_pct"},
                "maximum": {"field": "max_pct"},
                "goal": {"field": "goal_pct"},
            }
        ),
    ),
    (
        "pie_with_breakdown",
        _panel(
            config={
                "type": "pie",
                "query": "FROM metrics-* | STATS value = SUM(metric) BY service.name",
                "metrics": [{"field": "requests"}],
                "breakdowns": [{"field": "service.name"}],
            }
        ),
    ),
    (
        "treemap_with_breakdown",
        _panel(
            config={
                "type": "treemap",
                "query": "FROM metrics-* | STATS value = SUM(metric) BY service.name",
                "metrics": [{"field": "requests"}],
                "breakdowns": [{"field": "service.name"}],
            }
        ),
    ),
    (
        "datatable_metrics_and_rows",
        _panel(
            config={
                "type": "datatable",
                "query": "FROM metrics-* | STATS value = AVG(metric) BY service.name",
                "metrics": [{"field": "requests", "format": {"type": "number", "decimals": 0}}],
                "breakdowns": [{"field": "service.name"}],
            }
        ),
    ),
    (
        "datatable_no_columns_defaults_row",
        _panel(config={"type": "datatable", "query": "FROM metrics-* | LIMIT 1"}),
    ),
    (
        "heatmap_full",
        _panel(
            config={
                "type": "heatmap",
                "query": "FROM metrics-* | STATS value = COUNT() BY time_bucket, le",
                "x_axis": {"field": "time_bucket"},
                "y_axis": {"field": "le"},
                "metric": {"field": "bucket"},
            }
        ),
    ),
    (
        "duration_format_defaults",
        _panel(
            config={
                "type": "metric",
                "query": "FROM metrics-* | STATS value = AVG(latency)",
                "primary": {"field": "latency", "label": "Latency", "format": {"type": "duration"}},
            }
        ),
    ),
]


@pytest.mark.parametrize("name,panel", _REPRESENTATIVE_PANELS, ids=[name for name, _ in _REPRESENTATIVE_PANELS])
def test_production_and_oracle_mappers_agree_on_representative_panels(name, panel) -> None:
    production_result = production.map_panel(panel)
    oracle_result, oracle_findings = oracle.api_panel_from_report_panel("D", panel)

    assert oracle_findings == [], f"{name}: oracle mapper produced unexpected findings: {oracle_findings}"
    assert production_result.api_panel is not None, f"{name}: production mapper failed to map panel"
    assert oracle_result is not None, f"{name}: oracle mapper failed to map panel"
    assert production_result.api_panel == oracle_result, (
        f"{name}: production and oracle mappers disagree on the API panel shape.\n"
        f"production={production_result.api_panel}\noracle={oracle_result}"
    )


def test_production_and_oracle_mappers_agree_on_unsupported_chart() -> None:
    panel = _panel(config={"type": "legacy_metric", "query": "FROM metrics-*"})

    production_result = production.map_panel(panel)
    oracle_result, oracle_findings = oracle.api_panel_from_report_panel("D", panel)

    assert production_result.api_panel is None
    assert oracle_result is None
    assert len(oracle_findings) == 1
    assert oracle_findings[0].category == "unsupported_by_api_oracle"


def test_production_and_oracle_supported_esql_type_sets_match() -> None:
    # Both mappers should claim support for exactly the same set of ES|QL
    # chart-config ``type`` values -- a mismatch here means one mapper added
    # (or dropped) chart-family coverage without the other following suit.
    assert production._CONFIG_BUILDERS.keys() | production._XY_KINDS == oracle._SUPPORTED_ESQL_TYPES
