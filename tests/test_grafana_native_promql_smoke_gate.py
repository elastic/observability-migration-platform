# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Native PROMQL(...) passthrough smoke gate (issue #301 PR3)."""

from __future__ import annotations

import pytest

from observability_migration.adapters.source.grafana.broader_surface_gate import (
    check_native_promql_emission,
    gate_bugs,
)
from observability_migration.adapters.source.grafana.panels import (
    build_native_promql_query,
    can_use_native_promql,
    translate_panel,
)
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver

SMOKE_EXPRS = (
    "rate(http_requests_total[5m])",
    "sum(rate(http_requests_total[5m])) by (service)",
    'up{job="api"}',
)


@pytest.mark.parametrize("expr", SMOKE_EXPRS)
def test_native_promql_smoke_exprs(expr: str):
    assert can_use_native_promql(expr)
    index = "metrics-*"
    query = build_native_promql_query(expr, index=index)
    bugs = gate_bugs(check_native_promql_emission(query, esql_index=index))
    assert bugs == [], bugs


def test_native_promql_panel_path_with_rule_pack_flag():
    rule_pack = RulePackConfig()
    rule_pack.native_promql = True
    resolver = SchemaResolver(rule_pack)
    panel = {
        "id": 1,
        "type": "timeseries",
        "title": "Native rate",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [{"expr": "rate(http_requests_total[5m])", "refId": "A"}],
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    query = (yaml_panel or {}).get("esql", {}).get("query") or ""
    assert result.status not in {"requires_manual", "skipped"}
    bugs = gate_bugs(check_native_promql_emission(query, esql_index="metrics-*"))
    assert bugs == [], (query, bugs)


def test_corrupting_index_makes_native_smoke_fail():
    query = build_native_promql_query("rate(http_requests_total[5m])", index="metrics-*")
    bugs = gate_bugs(check_native_promql_emission(query, esql_index="other-metrics-*"))
    assert any(b.rule_id.value == "NATIVE_PROMQL_INDEX" for b in bugs)
