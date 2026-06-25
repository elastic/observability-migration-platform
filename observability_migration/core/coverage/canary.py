# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Kitchen-sink canary dashboard generator.

Builds a single Grafana dashboard that exercises one panel per distinct
chart-bearing Kibana target (line, bar, gauge, metric, datatable, pie, heatmap,
markdown). Generated from representative queries so it stays in lockstep with the
supported-type registry — a frozen-but-self-updating "maximum variety" dashboard.

Used three ways:
  * offline — must migrate cleanly and validate against the Kibana schema
    (tests/test_canary.py);
  * coverage — proves every supported panel type's Kibana target is represented;
  * live (later) — uploaded to a real Kibana for the render-audit gate.

Queries reference the synthetic telemetry the seeder generates
(``scripts/setup_telemetry_data.py``) so the same canary can render with data.
"""

from __future__ import annotations

from typing import Any

# (panel type, kibana target, representative PromQL). ``None`` expr = no query.
_CANARY_PANELS: list[tuple[str, str, str | None]] = [
    ("timeseries", "line", "sum(rate(http_requests_total[5m])) by (method)"),
    ("barchart", "bar", "sum(rate(http_requests_total[5m])) by (method)"),
    ("gauge", "gauge", "avg(node_memory_MemAvailable_bytes)"),
    ("stat", "metric", "sum(rate(http_requests_total[5m]))"),
    ("table", "datatable", "avg(node_cpu_seconds_total) by (instance)"),
    ("piechart", "pie", "sum(rate(http_requests_total[5m])) by (method)"),
    ("heatmap", "heatmap", "sum(rate(http_request_duration_seconds_bucket[5m])) by (le)"),
    ("text", "markdown", None),
]

CANARY_UID = "obs-migrate-canary-kitchen-sink"
CANARY_TITLE = "obs-migrate canary (kitchen sink)"

# Kibana targets the canary is expected to exercise.
CANARY_KIBANA_TARGETS = frozenset(target for _t, target, _e in _CANARY_PANELS)


def _panel(idx: int, ptype: str, expr: str | None) -> dict[str, Any]:
    x = (idx % 2) * 12
    y = (idx // 2) * 8
    panel: dict[str, Any] = {
        "id": idx + 1,
        "type": ptype,
        "title": f"canary {ptype}",
        "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
        "fieldConfig": {"defaults": {}, "overrides": []},
    }
    if ptype == "text":
        panel["options"] = {
            "mode": "markdown",
            "content": "# Canary\nKitchen-sink dashboard for migration render verification.",
        }
    else:
        panel["targets"] = [
            {"expr": expr, "refId": "A", "datasource": {"type": "prometheus"}}
        ]
    return panel


def build_grafana_canary() -> dict[str, Any]:
    """Return a Grafana dashboard dict covering all chart-bearing Kibana targets."""
    panels = [_panel(idx, ptype, expr) for idx, (ptype, _target, expr) in enumerate(_CANARY_PANELS)]
    return {
        "uid": CANARY_UID,
        "title": CANARY_TITLE,
        "description": "Generated kitchen-sink canary covering all supported panel types.",
        "schemaVersion": 39,
        "panels": panels,
        "templating": {"list": []},
        "time": {"from": "now-3h", "to": "now"},
    }
