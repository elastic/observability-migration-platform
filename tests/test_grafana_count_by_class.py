# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Express-style Count-by-class multi-target fusion — gap C2."""

from __future__ import annotations

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.runtime_features import (
    ESQL_NAMED_PARAM_BINDING,
    set_runtime_feature,
)


def _class_expr(status_re: str, fill_label: str) -> str:
    return (
        f'sum(\n http_requests_total{{instance="$instance",status=~"{status_re}"}} or\n'
        f' on() label_replace(vector(0),"status","{fill_label}","","")\n)'
    )


def test_count_by_class_fuses_with_case_scoped_filters():
    panel = {
        "id": 1,
        "type": "gauge",
        "title": "Count by class",
        "targets": [
            {"refId": "B", "expr": _class_expr("1..", "100"), "legendFormat": "1xx"},
            {"refId": "C", "expr": _class_expr("2..", "200"), "legendFormat": "2xx"},
            {"refId": "E", "expr": _class_expr("4..", "400"), "legendFormat": "4xx"},
        ],
    }
    rp = rules.RulePackConfig()
    set_runtime_feature(rp, ESQL_NAMED_PARAM_BINDING, supported=True, source="test")
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
    # Must CASE-scope status filters, not AND them all at the top level.
    assert "CASE(" in query and "status RLIKE" in query
    top_level_status_wheres = [
        line
        for line in query.splitlines()
        if line.strip().startswith("| WHERE") and "status RLIKE" in line
    ]
    assert top_level_status_wheres == [], top_level_status_wheres
    assert "1xx" in query and "2xx" in query and "4xx" in query
