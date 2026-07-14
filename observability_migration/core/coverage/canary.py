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
#
# Breakdown labels MUST exist in the standard seeded telemetry
# (scripts/setup_telemetry_data.py) so the canary is a deterministic render
# positive-control: ``instance`` and ``le`` are present, whereas a label the
# target data lacks (e.g. ``method``) makes the PROMQL command emit no column
# for it, and Lens then errors "Provided column name or index is invalid" on the
# splitAccessor. That is a field-mapping/data-readiness gap, not a translation
# bug — the render audit is what surfaces it.
_CANARY_PANELS: list[tuple[str, str, str | None]] = [
    ("timeseries", "line", "sum(rate(http_requests_total[5m])) by (instance)"),
    ("barchart", "bar", "sum(rate(http_requests_total[5m])) by (instance)"),
    ("gauge", "gauge", "avg(node_memory_MemAvailable_bytes)"),
    ("stat", "metric", "sum(rate(http_requests_total[5m]))"),
    ("table", "datatable", "avg(node_cpu_seconds_total) by (instance)"),
    ("piechart", "pie", "sum(rate(http_requests_total[5m])) by (instance)"),
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


LATE_BOUND_GROUPING_CANARY_UID = "obs-migrate-canary-late-bound-grouping"
LATE_BOUND_GROUPING_CANARY_TITLE = "obs-migrate canary (late-bound grouping)"

# Dimensions the ``grouping`` selector offers. All three must exist in the
# seeded telemetry (the contract picks them up from the emitted control choices
# and STATS BY columns) so the interactive field control has real fields to
# switch between when the render audit drives the panel.
_LATE_BOUND_GROUPING_CHOICES = ("exporter", "transport", "receiver")
# Counter metric wrapped in rate(); the seeder types ``*_spans`` from the
# counter-style source function so RATE() has a valid counter input.
_LATE_BOUND_GROUPING_METRIC = "otelcol_receiver_accepted_spans"


def build_late_bound_grouping_canary(default_grouping: str = "transport") -> dict[str, Any]:
    """Return a Grafana dashboard exercising late-bound ``by ($grouping)`` (issue #282).

    Two panels pin both halves of the feature so the render audit proves each
    actually renders in Kibana (the class of Lens "invalid column" failure that
    ES|QL execution and the schema gate miss):

    * **pure** ``by ($grouping)`` -> migrates to an interactive ES|QL field
      control (``STATS ... BY grouping = ??grouping``); the Lens breakdown binds
      the stable ``grouping`` alias regardless of which field is selected.
    * **concrete + variable** ``by (exporter, $grouping)`` -> the collision fix:
      the optional selector is dropped and the explicit ``exporter`` grouping is
      kept, so the panel still renders (no shared field control whose choices
      could collide with the concrete breakdown column).

    ``default_grouping`` lets the live render gate upload one dashboard per
    selectable field. That exercises every identifier substitution without
    brittle browser-control automation.
    """
    if default_grouping not in _LATE_BOUND_GROUPING_CHOICES:
        raise ValueError(
            f"default_grouping must be one of {_LATE_BOUND_GROUPING_CHOICES}, "
            f"got {default_grouping!r}"
        )
    variant_suffix = "" if default_grouping == "transport" else f"-{default_grouping}"
    title_suffix = "" if default_grouping == "transport" else f": {default_grouping}"
    variable = {
        "name": "grouping",
        "type": "custom",
        "label": "Group by",
        "query": ",".join(_LATE_BOUND_GROUPING_CHOICES),
        "current": {"text": default_grouping, "value": default_grouping},
        "options": [
            {"text": choice, "value": choice, "selected": choice == default_grouping}
            for choice in _LATE_BOUND_GROUPING_CHOICES
        ],
    }

    def _ts_panel(idx: int, title: str, expr: str) -> dict[str, Any]:
        return {
            "id": idx + 1,
            "type": "timeseries",
            "title": title,
            "gridPos": {"x": (idx % 2) * 12, "y": (idx // 2) * 8, "w": 12, "h": 8},
            "fieldConfig": {"defaults": {}, "overrides": []},
            "targets": [
                {"expr": expr, "refId": "A", "datasource": {"type": "prometheus"}}
            ],
        }

    panels = [
        _ts_panel(
            0,
            "spans by grouping",
            f"sum(rate({_LATE_BOUND_GROUPING_METRIC}[5m])) by ($grouping)",
        ),
        _ts_panel(
            1,
            "spans by exporter and grouping",
            f"sum(rate({_LATE_BOUND_GROUPING_METRIC}[5m])) by (exporter, $grouping)",
        ),
    ]
    return {
        "uid": f"{LATE_BOUND_GROUPING_CANARY_UID}{variant_suffix}",
        "title": f"{LATE_BOUND_GROUPING_CANARY_TITLE}{title_suffix}",
        "description": "Late-bound Grafana grouping-variable canary (issue #282).",
        "schemaVersion": 39,
        "panels": panels,
        "templating": {"list": [variable]},
        "time": {"from": "now-3h", "to": "now"},
    }
