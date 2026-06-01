# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
from observability_migration.adapters.source.grafana.series_labels import (
    build_metric_series_labels,
)


def _panel(expr):
    return {"type": "timeseries", "targets": [{"expr": expr}]}


def test_single_panel_by_clause():
    dash = {"panels": [_panel("sum(go_goroutines) by (instance)")]}
    assert build_metric_series_labels(dash) == {"go_goroutines": ["instance"]}


def test_cross_panel_union_first_seen_order():
    dash = {
        "panels": [
            _panel("sum(go_goroutines) by (instance)"),
            _panel("avg(go_goroutines) by (job)"),
        ]
    }
    assert build_metric_series_labels(dash) == {"go_goroutines": ["instance", "job"]}


def test_label_values_template_variable():
    dash = {
        "panels": [],
        "templating": {
            "list": [{"type": "query", "query": "label_values(go_goroutines, job)"}]
        },
    }
    assert build_metric_series_labels(dash) == {"go_goroutines": ["job"]}


def test_without_clause_is_ignored():
    dash = {"panels": [_panel("sum(go_goroutines) without (instance)")]}
    assert build_metric_series_labels(dash) == {}


def test_single_value_equality_filter_excluded():
    dash = {"panels": [_panel('go_goroutines{instance="prometheus-1:9090"}')]}
    assert build_metric_series_labels(dash) == {}


def test_variable_filter_included():
    dash = {"panels": [_panel('go_goroutines{instance="$inst"}')]}
    assert build_metric_series_labels(dash) == {"go_goroutines": ["instance"]}


def test_union_cap_overflow_drops_metric():
    expr = "sum(m) by (a, b, c, d)"
    dash = {"panels": [_panel(expr)]}
    # 4 distinct labels exceeds the cap of 3 -> metric omitted (honest fallback).
    assert build_metric_series_labels(dash) == {}
