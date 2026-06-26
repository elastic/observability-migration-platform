# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana semantic-accuracy suite (Layer 1, Grafana side).

Mirror of ``tests/e2e/test_datadog_semantic_accuracy.py``: instead of asserting
byte-for-byte snapshot output, this asserts that each translated panel *preserves
the meaning* of the source PromQL — aggregation, metric identity, group-by
dimension, and time-bucketing — by inspecting the backend-agnostic structured
``esql`` block the translator emits (robust to native-PROMQL vs ES|QL emission
and to OTel field remapping like ``instance`` -> ``service.instance.id``).

Grafana previously had no explicit semantic assertions; correctness leaned on
snapshots + the panel matrix. This closes that gap.
"""

from __future__ import annotations

import pytest

from observability_migration.adapters.source.grafana import panels, rules, schema

_TRANSLATED = {"migrated", "migrated_with_warnings"}


def _translate(expr: str, panel_type: str = "timeseries"):
    rp = rules.RulePackConfig()
    resolver = schema.SchemaResolver(rp)
    panel = {
        "id": 1,
        "type": panel_type,
        "title": "X",
        "targets": [{"expr": expr, "refId": "A"}],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
    }
    return panels.translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=resolver,
    )


# (expr, expected_agg_fn, source_metric, has_group_by)
_TIMESERIES_CASES = [
    ("sum(rate(http_requests_total[5m])) by (method)", "SUM", "http_requests_total", True),
    ("avg(node_cpu_seconds_total) by (instance)", "AVG", "node_cpu_seconds_total", True),
    ("min(node_cpu_seconds_total) by (instance)", "MIN", "node_cpu_seconds_total", True),
    ("max(node_cpu_seconds_total) by (instance)", "MAX", "node_cpu_seconds_total", True),
    ("sum(rate(http_requests_total[5m]))", "SUM", "http_requests_total", False),
]


@pytest.mark.parametrize("expr,agg,metric,has_by", _TIMESERIES_CASES)
def test_timeseries_panel_preserves_semantics(expr, agg, metric, has_by):
    yaml_panel, result = _translate(expr)

    assert result.status in _TRANSLATED, f"{expr!r} did not translate: {result.status}"
    esql = yaml_panel.get("esql") or {}
    query = str(esql.get("query") or "")

    # No empty query for a translated panel.
    assert query.strip(), f"{expr!r} produced an empty query"

    # Aggregation preserved.
    assert f"{agg}(" in query, f"{expr!r} lost aggregation {agg}: {query}"

    # Metric identity preserved (dotted/underscored source metric present in metrics block).
    metric_fields = [m.get("field") for m in esql.get("metrics", []) if isinstance(m, dict)]
    assert any(metric in str(f) for f in metric_fields), (
        f"{expr!r} lost metric {metric}: metrics={metric_fields}"
    )

    # Timeseries panels bucket over time.
    dim = esql.get("dimension") or {}
    dim_field = str(dim.get("field") or "")
    assert "bucket" in dim_field.lower() or "time" in dim_field.lower(), (
        f"{expr!r} timeseries has no time dimension: {dim_field}"
    )

    # Group-by preserved when the source has a by() clause (field may be remapped).
    breakdown = esql.get("breakdown")
    breakdown_field = breakdown.get("field") if isinstance(breakdown, dict) else breakdown
    if has_by:
        assert breakdown_field, f"{expr!r} lost its group-by breakdown"
    # When there is no by(), we don't require a breakdown.


def test_stat_panel_translates_with_non_empty_query():
    yaml_panel, result = _translate("node_memory_MemAvailable_bytes", panel_type="stat")
    assert result.status in _TRANSLATED
    esql = yaml_panel.get("esql") or {}
    assert str(esql.get("query") or "").strip(), "stat panel produced an empty query"


def test_aggregation_is_not_silently_swapped():
    # A regression that turned SUM into AVG (or dropped RATE) would be caught here.
    yaml_panel, _ = _translate("sum(rate(http_requests_total[5m])) by (method)")
    query = str((yaml_panel.get("esql") or {}).get("query") or "")
    assert "SUM(" in query and "RATE(" in query
    assert "AVG(" not in query
