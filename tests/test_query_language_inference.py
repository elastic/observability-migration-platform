# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""An explicit datasource must outrank the panel type when picking a language."""

from __future__ import annotations

from observability_migration.adapters.source.grafana.manifest import infer_query_language


def test_logs_panel_on_a_prometheus_datasource_is_promql():
    """Grafana's Logs panel is a renderer, not a datasource.

    It is routinely pointed at Prometheus to show a metric as a table. Routing on
    the panel type first sent those panels down the LogQL path, where labels
    resolve with OTEL/ECS naming: a canary panel with a Prometheus datasource and
    `sum(redis_db_keys) by (instance)` emitted `service.instance.id` and failed in
    Kibana with "Unknown column", while all 15 other panel types on the same
    dashboard correctly used the prometheus_native passthrough field.
    """
    assert infer_query_language(
        'sum(redis_db_keys{instance=~"$instance"}) by (instance)',
        datasource_type="prometheus",
        panel_type="logs",
    ) == "promql"


def test_logs_panel_on_loki_is_still_logql():
    assert infer_query_language(
        '{job="app"} |= "error"', datasource_type="loki", panel_type="logs"
    ) == "logql"


def test_logs_panel_without_a_datasource_hint_is_still_logql():
    """With nothing better to go on, the panel type remains the signal."""
    assert infer_query_language(
        '{job="app"}', datasource_type="", panel_type="logs"
    ) == "logql"


def test_logs_panel_on_elasticsearch_is_not_logql():
    assert infer_query_language(
        "FROM logs-* | LIMIT 10", datasource_type="elasticsearch", panel_type="logs"
    ) == "esql"
