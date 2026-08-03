# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Multi-target summary (stat/gauge) fusion — gap B."""

from __future__ import annotations

from observability_migration.adapters.source.grafana import panels, rules, schema

_ONLY_ONE = "only 1 could be migrated"


def _translate_stat(targets):
    panel = {
        "id": 1,
        "type": "stat",
        "title": "CPU modes",
        "targets": targets,
    }
    rp = rules.RulePackConfig()
    return panels.translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=schema.SchemaResolver(rp),
    )


def test_compatible_multi_target_stat_fuses_series():
    yaml_panel, result = _translate_stat(
        [
            {
                "refId": "A",
                "expr": 'sum(rate(node_cpu_seconds_total{mode="idle"}[5m]))',
                "legendFormat": "Idle",
            },
            {
                "refId": "B",
                "expr": 'sum(rate(node_cpu_seconds_total{mode="user"}[5m]))',
                "legendFormat": "User",
            },
            {
                "refId": "C",
                "expr": 'sum(rate(node_cpu_seconds_total{mode="system"}[5m]))',
                "legendFormat": "System",
            },
        ]
    )
    assert result.status in {"migrated", "migrated_with_warnings"}
    joined = " ".join(result.reasons or [])
    assert _ONLY_ONE not in joined, joined
    query = result.esql_query or ""
    # Fused wide query should keep more than the primary idle series.
    assert "mode == \"user\"" in query or "Idle" in query or "User" in query
    meta = {}
    if result.query_ir is not None:
        meta = getattr(result.query_ir, "metadata", None) or result.query_ir.get("metadata", {})
    series = meta.get("multi_series_metric_fields") or []
    assert len(series) >= 2, (series, query)
    # Grouped/multi-series stat approximates as datatable.
    assert result.kibana_type in {"datatable", "metric"}
    esql = yaml_panel.get("esql") or {}
    assert esql.get("query")


def test_single_target_stat_unchanged():
    yaml_panel, result = _translate_stat(
        [
            {
                "refId": "A",
                "expr": "sum(rate(http_requests_total[5m]))",
            }
        ]
    )
    assert result.status in {"migrated", "migrated_with_warnings"}
    assert _ONLY_ONE not in " ".join(result.reasons or [])
    assert (yaml_panel.get("esql") or {}).get("type") == "metric"


def test_windows_drop_suffix_only_when_all_dropped_are_windows():
    _yaml_panel, result = _translate_stat(
        [
            {
                "refId": "A",
                "expr": "sum(rate(node_cpu_seconds_total[5m]))",
                "legendFormat": "Linux",
            },
            {
                "refId": "B",
                "expr": "sum(rate(windows_cpu_time_total[5m]))",
                "legendFormat": "Windows",
            },
            {
                # Deliberately incompatible grouping so fusion keeps one series.
                "refId": "C",
                "expr": "sum(rate(node_cpu_seconds_total[5m])) by (instance)",
                "legendFormat": "By instance",
            },
        ]
    )
    joined = " ".join(result.reasons or [])
    if "Windows-specific" in joined:
        # Must not claim *all* drops are Windows when a non-Windows peer was dropped.
        assert "dropped targets are Windows-specific)" not in joined or (
            "of the dropped targets are Windows-specific" in joined
        )
