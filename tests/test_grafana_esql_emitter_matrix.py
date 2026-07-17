# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Emitter path matrix: one cell per registered Grafana ES|QL fusion path.

Each registered emitter has a minimal fixture that forces that fusion path and
asserts a path token (warning substring, query shape, or provenance key) before
running the structural oracle.
"""

from __future__ import annotations

import pytest

from observability_migration.adapters.source.grafana.esql_emitters import (
    EMITTER_HELPER_SYMBOLS,
    GRAFANA_ESQL_EMITTERS,
)
from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    check_esql_structure,
    structural_errors,
)
from observability_migration.adapters.source.grafana.panels import translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)

_PRETRANSLATED_FUSE_WARNING = (
    "Fused multi-target panel from independently translated ES|QL queries"
)

MATRIX_CELLS: tuple[str, ...] = (
    "single_target_formula",
    "join_family_ratio",
    "shared_measure_pipeline",
    "pretranslated_xy_merge",
    "same_metric_collapse",
)

EMITTER_MATRIX_TESTS: dict[str, str] = {
    "single_target_formula": "test_emitter_single_target_formula",
    "join_family_ratio": "test_emitter_join_family_ratio",
    "shared_measure_pipeline": "test_emitter_shared_measure_pipeline",
    "pretranslated_xy_merge": "test_emitter_pretranslated_xy_merge",
    "same_metric_collapse": "test_emitter_same_metric_collapse",
}


def test_registry_symbols_exist_in_source():
    from pathlib import Path

    root = Path("observability_migration/adapters/source/grafana")
    text = "\n".join(p.read_text() for p in root.glob("*.py"))
    for emitter_id, symbol in EMITTER_HELPER_SYMBOLS.items():
        assert f"def {symbol}" in text or f"{symbol} =" in text, emitter_id


def test_every_emitter_has_matrix_cell():
    assert set(MATRIX_CELLS) == set(GRAFANA_ESQL_EMITTERS)
    assert set(EMITTER_MATRIX_TESTS) == set(MATRIX_CELLS)
    for emitter_id, test_name in EMITTER_MATRIX_TESTS.items():
        test_fn = globals().get(test_name)
        assert callable(test_fn), f"missing matrix test {test_name!r} for emitter {emitter_id!r}"


def _rule_pack_and_resolver() -> tuple[RulePackConfig, SchemaResolver]:
    rule_pack = RulePackConfig()
    return rule_pack, SchemaResolver(rule_pack)


def test_emitter_single_target_formula():
    rule_pack, resolver = _rule_pack_and_resolver()
    expr = "sum(rate(http_requests_total[5m]))"
    ctx = translate_promql_to_esql(
        expr,
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    query = ctx.esql_query or ""
    stats_lines = [ln for ln in query.splitlines() if ln.lstrip().startswith("| STATS")]
    assert len(stats_lines) == 1, query
    assert _PRETRANSLATED_FUSE_WARNING not in " ".join(ctx.warnings)
    assert structural_errors(check_esql_structure(query)) == []


def test_emitter_join_family_ratio():
    rule_pack, resolver = _rule_pack_and_resolver()
    expr = (
        'sum by(instance) (irate(node_cpu_guest_seconds_total{instance="n", mode="user"}[1m]))'
        " / on(instance) group_left "
        'sum by (instance)(irate(node_cpu_seconds_total{instance="n"}[1m]))'
    )
    ctx = translate_promql_to_esql(
        expr,
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    query = ctx.esql_query or ""
    assert "numerator =" in query
    assert "denominator =" in query
    assert structural_errors(check_esql_structure(query)) == []


def test_emitter_shared_measure_pipeline():
    rule_pack, resolver = _rule_pack_and_resolver()
    panel = {
        "id": 1,
        "type": "timeseries",
        "title": "CPU and memory",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {"expr": "rate(http_requests_total[5m])", "refId": "A", "legendFormat": "requests"},
            {"expr": "rate(http_errors_total[5m])", "refId": "B", "legendFormat": "errors"},
        ],
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    query = yaml_panel["esql"]["query"]
    provenance = result.query_ir["metadata"].get("collapsed_targets") or []
    assert _PRETRANSLATED_FUSE_WARNING not in " ".join(result.reasons)
    assert all(not entry.get("whole_translated") for entry in provenance)
    assert structural_errors(check_esql_structure(query)) == []


def test_emitter_same_metric_collapse():
    rule_pack, resolver = _rule_pack_and_resolver()
    panel = {
        "id": 3,
        "type": "graph",
        "title": "Systemd Units",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {"expr": 'node_systemd_units{state="active"}', "refId": "A"},
            {"expr": 'node_systemd_units{state="failed"}', "refId": "B"},
        ],
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    query = yaml_panel["esql"]["query"]
    assert any("Collapsed 2 same-metric targets into BY state" in reason for reason in result.reasons)
    assert structural_errors(check_esql_structure(query)) == []


def test_emitter_pretranslated_xy_merge():
    rule_pack, resolver = _rule_pack_and_resolver()
    panel = {
        "id": 2,
        "type": "timeseries",
        "title": "Guest CPU ratios",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {
                "expr": (
                    'sum by(instance) (irate(node_cpu_guest_seconds_total{mode="user"}[1m]))'
                    " / on(instance) group_left "
                    "sum by(instance)(irate(node_cpu_seconds_total[1m]))"
                ),
                "refId": "A",
                "legendFormat": "Guest",
            },
            {
                "expr": (
                    'sum by(instance) (irate(node_cpu_guest_seconds_total{mode="nice"}[1m]))'
                    " / on(instance) group_left "
                    "sum by(instance)(irate(node_cpu_seconds_total[1m]))"
                ),
                "refId": "B",
                "legendFormat": "GuestNice",
            },
        ],
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    query = yaml_panel["esql"]["query"]
    assert any(_PRETRANSLATED_FUSE_WARNING in reason for reason in result.reasons)
    assert structural_errors(check_esql_structure(query)) == []


@pytest.mark.parametrize(
    "emitter_id",
    GRAFANA_ESQL_EMITTERS,
)
def test_matrix_cell_registered(emitter_id: str):
    assert emitter_id in MATRIX_CELLS
