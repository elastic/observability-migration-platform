# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

from pathlib import Path

from observability_migration.adapters.source.datadog.esql_emitters import (
    DATADOG_ESQL_EMITTERS,
    EMITTER_HELPER_SYMBOLS,
    EMITTER_RULE_IDS,
)
from observability_migration.adapters.source.datadog.esql_structural_oracle import (
    check_datadog_esql_structure,
    structural_errors,
)
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.log_parser import parse_log_query
from observability_migration.adapters.source.datadog.models import (
    NormalizedWidget,
    WidgetFormula,
    WidgetQuery,
)
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.query_parser import (
    parse_formula,
    parse_metric_query,
)
from observability_migration.adapters.source.datadog.translate import translate_widget

MATRIX_CELLS: tuple[str, ...] = (
    "metric_single_query",
    "metric_formula",
    "log_direct_esql",
    "log_kql_bridge",
)

EMITTER_MATRIX_TESTS: dict[str, str] = {
    "metric_single_query": "test_emitter_metric_single_query",
    "metric_formula": "test_emitter_metric_formula",
    "log_direct_esql": "test_emitter_log_direct_esql",
    "log_kql_bridge": "test_emitter_log_kql_bridge",
}


def test_registry_symbols_exist_in_source():
    text = Path("observability_migration/adapters/source/datadog/translate.py").read_text()
    for emitter_id, symbol in EMITTER_HELPER_SYMBOLS.items():
        assert f"def {symbol}" in text, emitter_id


def test_every_emitter_has_matrix_cell():
    assert set(MATRIX_CELLS) == set(DATADOG_ESQL_EMITTERS)
    assert set(EMITTER_MATRIX_TESTS) == set(MATRIX_CELLS)
    for emitter_id, test_name in EMITTER_MATRIX_TESTS.items():
        assert callable(globals().get(test_name)), emitter_id


def _assert_path_and_oracle(result, emitter_id: str) -> None:
    rule_id = EMITTER_RULE_IDS[emitter_id]
    assert any(entry.get("rule") == rule_id for entry in result.trace), (
        emitter_id,
        result.trace,
    )
    assert result.backend in {"esql", "esql_with_kql"}, result.backend
    assert result.status in {"ok", "warning"}, (result.status, result.warnings)
    errs = structural_errors(
        check_datadog_esql_structure(
            result.esql_query or "",
            status=result.status,
            backend=result.backend,
        )
    )
    assert errs == [], (emitter_id, result.esql_query, errs)


def test_emitter_metric_single_query():
    mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
    wq = WidgetQuery(
        name="query1",
        data_source="metrics",
        raw_query="avg:system.cpu.user{*} by {host}",
        metric_query=mq,
        query_type="metric",
    )
    widget = NormalizedWidget(
        id="1", widget_type="timeseries", title="CPU", queries=[wq]
    )
    result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
    _assert_path_and_oracle(result, "metric_single_query")


def test_emitter_metric_formula():
    mq = parse_metric_query("avg:system.cpu.user{*}")
    wq = WidgetQuery(
        name="query1",
        data_source="metrics",
        raw_query="avg:system.cpu.user{*}",
        metric_query=mq,
        query_type="metric",
    )
    wf = WidgetFormula(raw="query1 * 100")
    wf.expression = parse_formula("query1 * 100")
    widget = NormalizedWidget(
        id="1",
        widget_type="query_value",
        title="CPU %",
        queries=[wq],
        formulas=[wf],
    )
    result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
    _assert_path_and_oracle(result, "metric_formula")


def test_emitter_log_direct_esql():
    lq = parse_log_query("service:web AND status:error")
    wq = WidgetQuery(
        name="query1",
        data_source="logs",
        raw_query="service:web AND status:error",
        log_query=lq,
        query_type="log",
    )
    widget = NormalizedWidget(
        id="1", widget_type="timeseries", title="Errors", queries=[wq]
    )
    result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
    assert result.backend == "esql"
    _assert_path_and_oracle(result, "log_direct_esql")


def test_emitter_log_kql_bridge():
    # Free-text LogTerm forces esql_with_kql via planner._choose_log_backend
    lq = parse_log_query("connection refused")
    wq = WidgetQuery(
        name="query1",
        data_source="logs",
        raw_query="connection refused",
        log_query=lq,
        query_type="log",
    )
    widget = NormalizedWidget(
        id="1", widget_type="list_stream", title="Free text", queries=[wq]
    )
    result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
    assert result.backend == "esql_with_kql"
    _assert_path_and_oracle(result, "log_kql_bridge")
