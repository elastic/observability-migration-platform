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


def test_stripped_keep_becomes_a_drop_of_intermediate_aliases():
    """Deleting the KEEP must not leak intermediate STATS aliases.

    A dotted ``STATS BY`` key (``labels.cmd``) makes ES|QL's optimizer reject a
    KEEP that re-projects it, so the projection is stripped. Stripping it
    outright, however, ships every intermediate alias in the panel output
    alongside the real metric. Those extra numeric columns are read by the
    parity oracle as label dimensions, which inflated one 10-series panel to
    375 and produced a false FAIL.

    A DROP of just the unwanted columns keeps the projection's intent without
    naming the dotted key, so ES accepts it (verified on 9.5).
    """
    from observability_migration.adapters.source.grafana.panels import (
        _strip_dotted_group_keep,
    )

    query = "\n".join([
        "TS metrics-*",
        "| STATS a = AVG(x), b = AVG(y) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), labels.cmd",
        "| EVAL computed_value = (a / b)",
        "| KEEP time_bucket, `labels.cmd`, computed_value",
        "| SORT time_bucket ASC",
    ])
    out = _strip_dotted_group_keep(query)
    lines = out.splitlines()
    assert not any(line.strip().startswith("| KEEP") for line in lines), out
    drop = [line for line in lines if line.strip().startswith("| DROP")]
    assert len(drop) == 1, out
    assert "a" in drop[0] and "b" in drop[0]
    assert "computed_value" not in drop[0]
    assert "labels.cmd" not in drop[0]
    # The DROP takes the KEEP's position, so the trailing SORT stays last and
    # is not duplicated.
    assert lines[-1].strip() == "| SORT time_bucket ASC"
    assert sum(1 for line in lines if line.strip().startswith("| SORT")) == 1


def test_query_without_dotted_group_keeps_its_projection():
    from observability_migration.adapters.source.grafana.panels import (
        _strip_dotted_group_keep,
    )

    query = "\n".join([
        "TS metrics-*",
        "| STATS a = AVG(x) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), cmd",
        "| EVAL v = a",
        "| KEEP time_bucket, cmd, v",
    ])
    assert _strip_dotted_group_keep(query) == query
