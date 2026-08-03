# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Dashboard-scoped ES|QL named-param binding for Grafana $var label filters (gap A)."""

from __future__ import annotations

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.runtime_features import (
    ESQL_NAMED_PARAM_BINDING,
    binds_esql_named_params,
    get_runtime_features,
    set_runtime_feature,
)
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)

_DROPPED = "Dropped variable-driven label filters during migration"


def _dashboard_with_instance_var(expr: str) -> dict:
    return {
        "uid": "vf-param",
        "title": "var filter",
        "panels": [
            {
                "id": 1,
                "type": "timeseries",
                "title": "filtered",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                "targets": [
                    {
                        "refId": "A",
                        "expr": expr,
                        "datasource": {"type": "prometheus"},
                    }
                ],
            }
        ],
        "templating": {
            "list": [
                {
                    "name": "instance",
                    "type": "custom",
                    "label": "Instance",
                    "query": "host-a,host-b",
                    "current": {"text": "host-a", "value": "host-a"},
                    "options": [
                        {"text": "host-a", "value": "host-a", "selected": True},
                        {"text": "host-b", "value": "host-b", "selected": False},
                    ],
                }
            ]
        },
        "time": {"from": "now-3h", "to": "now"},
    }


def test_dashboard_templating_emits_named_param_and_control():
    dash = _dashboard_with_instance_var(
        'sum(rate(http_requests_total{instance="$instance"}[5m]))'
    )
    rp = rules.RulePackConfig()
    resolver = schema.SchemaResolver(rp)
    result = panels.translate_dashboard(
        dash,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=resolver,
    )
    payload = {"dashboards": [result.dashboard_ir.to_yaml_dict()]}

    assert binds_esql_named_params(rp)
    pr = result.panel_results[0]
    assert pr.status in {"migrated", "migrated_with_warnings"}
    assert "?instance" in (pr.esql_query or ""), pr.esql_query
    joined = " ".join(pr.reasons or [])
    assert _DROPPED not in joined, joined
    controls = payload["dashboards"][0].get("controls") or []
    assert any(c.get("variable_name") == "instance" for c in controls), controls


def test_explicit_unsupported_probe_still_drops_matchers():
    """Live probe that refuses binding must not be overridden by templating."""
    dash = _dashboard_with_instance_var(
        'sum(rate(http_requests_total{instance="$instance"}[5m]))'
    )
    rp = rules.RulePackConfig()
    set_runtime_feature(
        rp,
        ESQL_NAMED_PARAM_BINDING,
        supported=False,
        source="probe",
        confidence="verified",
        reason="cluster cannot bind named params",
    )
    resolver = schema.SchemaResolver(rp)
    result = panels.translate_dashboard(
        dash,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=resolver,
    )

    assert not binds_esql_named_params(rp)
    pr = result.panel_results[0]
    assert "?instance" not in (pr.esql_query or "")
    assert _DROPPED in " ".join(pr.reasons or [])


def test_single_panel_without_feature_still_drops_var_matcher():
    rp = rules.RulePackConfig()
    assert ESQL_NAMED_PARAM_BINDING not in get_runtime_features(rp)
    ctx = translate_promql_to_esql(
        'rate(http_requests_total{job="$job"}[5m])',
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=schema.SchemaResolver(rp),
    )
    assert "?job" not in (ctx.esql_query or "")
    assert _DROPPED in ctx.warnings
