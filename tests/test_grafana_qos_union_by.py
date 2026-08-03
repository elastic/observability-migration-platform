# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""QoS-style union BY multi-target fusion — gap C1."""

from __future__ import annotations

from observability_migration.adapters.source.grafana import panels, rules, schema


def test_qos_union_by_fuses_grouped_and_ungrouped():
    panel = {
        "id": 1,
        "type": "timeseries",
        "title": "Pods QoS",
        "targets": [
            {
                "refId": "A",
                "expr": "sum(kube_pod_status_qos_class) by (qos_class)",
                "legendFormat": "QoS",
            },
            {
                "refId": "B",
                "expr": "sum(kube_pod_info)",
                "legendFormat": "Total",
            },
        ],
    }
    rp = rules.RulePackConfig()
    _yaml, result = panels.translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=schema.SchemaResolver(rp),
    )
    assert result.status in {"migrated", "migrated_with_warnings"}
    joined = " ".join(result.reasons or [])
    assert "only 1 could be migrated" not in joined, joined
    query = result.esql_query or ""
    assert "qos_class" in query
    assert "EVAL QoS" in query or "QoS =" in query or "kube_pod_status_qos_class" in query
    assert "EVAL Total" in query or "Total =" in query or "kube_pod_info" in query
    assert "Unioned BY fields" in joined
    assert "KEEP" in query and "QoS" in query and "Total" in query
