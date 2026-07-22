# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""LogQL emitter path matrix (issue #301 PR3)."""

from __future__ import annotations

from pathlib import Path

from observability_migration.adapters.source.grafana.broader_surface_gate import (
    check_logql_emission,
    gate_bugs,
)
from observability_migration.adapters.source.grafana.logql_emitters import (
    EMITTER_HELPER_SYMBOLS,
    GRAFANA_LOGQL_EMITTERS,
)
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import translate_promql_to_esql

MATRIX_CELLS: tuple[str, ...] = ("logql_stream", "logql_count")

EMITTER_MATRIX_TESTS: dict[str, str] = {
    "logql_stream": "test_emitter_logql_stream",
    "logql_count": "test_emitter_logql_count",
}


def test_registry_symbols_exist_in_source():
    root = Path("observability_migration/adapters/source/grafana")
    text = "\n".join(p.read_text() for p in root.glob("*.py"))
    for emitter_id, symbol in EMITTER_HELPER_SYMBOLS.items():
        assert f"def {symbol}" in text, emitter_id


def test_every_emitter_has_matrix_cell():
    assert set(MATRIX_CELLS) == set(GRAFANA_LOGQL_EMITTERS)
    assert set(EMITTER_MATRIX_TESTS) == set(MATRIX_CELLS)
    for emitter_id, test_name in EMITTER_MATRIX_TESTS.items():
        assert callable(globals().get(test_name)), emitter_id


def _translate(expr: str):
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    return translate_promql_to_esql(
        expr,
        esql_index=rule_pack.logs_index,
        rule_pack=rule_pack,
        resolver=resolver,
        panel_type="logs",
    )


def test_emitter_logql_stream():
    ctx = _translate('{job="app"} |= "error"')
    assert ctx.feasibility == "feasible"
    assert (ctx.fragment and ctx.fragment.family) == "logql_stream"
    assert "Approximated Loki logs panel" in " ".join(ctx.warnings)
    bugs = gate_bugs(check_logql_emission(ctx.esql_query or "", feasibility=ctx.feasibility))
    assert bugs == [], bugs


def test_emitter_logql_count():
    ctx = _translate('sum(count_over_time({job="app"}[5m]))')
    assert ctx.feasibility == "feasible"
    assert (ctx.fragment and ctx.fragment.family) == "logql_count"
    assert "STATS log_count = COUNT(*)" in (ctx.esql_query or "")
    bugs = gate_bugs(check_logql_emission(ctx.esql_query or "", feasibility=ctx.feasibility))
    assert bugs == [], bugs
