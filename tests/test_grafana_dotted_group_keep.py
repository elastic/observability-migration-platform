# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression: a projection (KEEP) after ``STATS BY <dotted field> | EVAL`` makes
ES|QL's optimizer re-attribute the dotted grouping field (e.g. service.name) from
field -> reference, raising verification_exception "Output has changed" and
breaking the panel in Kibana. The fix omits the KEEP when a grouping field is
dotted. Root cause was bisected live against Elastic 9.5.0."""

from __future__ import annotations

from observability_migration.adapters.source.grafana import panels, rules, schema


def _translate(expr: str):
    rp = rules.RulePackConfig()
    resolver = schema.SchemaResolver(rp)
    panel = {
        "id": 1, "type": "timeseries", "title": "Mem",
        "targets": [{"expr": expr, "refId": "A"}],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
    }
    return panels.translate_panel(panel, datasource_index="metrics-*",
                                  esql_index="metrics-*", rule_pack=rp, resolver=resolver)


def test_dotted_group_formula_omits_keep_projection():
    # Binary-op formula grouped by ``job`` (which maps to the dotted service.name).
    yaml_panel, _ = _translate(
        "sum(node_memory_MemTotal_bytes) by (job) "
        "- sum(node_memory_MemAvailable_bytes) by (job)"
    )
    query = (yaml_panel.get("esql") or {}).get("query", "")
    # Guard against a vacuous pass: we must actually hit the dotted-group path.
    assert "STATS" in query and "service.name" in query and "| EVAL" in query, query
    # The dotted grouping field must NOT be re-projected by a KEEP (the bug).
    keep_lines = [ln for ln in query.splitlines() if ln.strip().startswith("| KEEP")]
    assert not any("service.name" in ln for ln in keep_lines), (
        f"KEEP re-projects the dotted grouping field, which triggers "
        f"'Output has changed' in Kibana:\n{query}"
    )
