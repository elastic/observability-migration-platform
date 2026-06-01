# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Snapshot tests for Datadog query -> ESQL translation."""

from __future__ import annotations

import difflib
import os
import unittest
from pathlib import Path
from typing import Any

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.log_parser import parse_log_query
from observability_migration.adapters.source.datadog.models import (
    NormalizedWidget,
    TranslationResult,
    WidgetFormula,
    WidgetQuery,
)
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.query_parser import (
    parse_formula,
    parse_legacy_query,
    parse_metric_query,
)
from observability_migration.adapters.source.datadog.translate import translate_widget

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "datadog_to_esql"
UPDATE_SNAPSHOTS = os.environ.get("UPDATE_SNAPSHOTS") == "1"

CaseSpec = dict[str, Any]


CASES: list[CaseSpec] = [
    {
        "name": "avg_cpu_by_host",
        "kind": "metric",
        "query": "avg:system.cpu.user{*} by {host}",
        "widget_type": "timeseries",
    },
    {
        "name": "query_value_avg_cpu",
        "kind": "metric",
        "query": "avg:system.cpu.user{*}",
        "widget_type": "query_value",
    },
    {
        "name": "toplist_avg_cpu_by_host",
        "kind": "metric",
        "query": "avg:system.cpu.user{*} by {host}",
        "widget_type": "toplist",
    },
    {
        "name": "as_rate_counter_sum",
        "kind": "metric",
        "query": "sum:http.requests{*}.as_rate()",
        "widget_type": "timeseries",
    },
    {
        "name": "as_count_grouped_event_metric",
        "kind": "metric",
        "query": "sum:rabbitmq.queues.created.count{$node_name} by {rabbitmq_node}.as_count()",
        "widget_type": "timeseries",
    },
    {
        "name": "rollup_fill_grouped",
        "kind": "metric",
        "query": "max:mongodb.replset.optime_lag{$scope,$replset_name} by {replset_name}.fill(zero).rollup(avg, 15)",
        "widget_type": "timeseries",
    },
    {
        "name": "boolean_scope_or",
        "kind": "metric",
        "query": "avg:system.cpu.user{env:prod AND(host:web01 OR host:web02)}",
        "widget_type": "query_value",
    },
    {
        "name": "percentile_grouped_duration",
        "kind": "metric",
        "query": "p95:trace.http.request.duration{*} by {resource_name}",
        "widget_type": "timeseries",
    },
    {
        "name": "formula_single_query_percent",
        "kind": "formula",
        "queries": [("query1", "avg:system.cpu.user{*}")],
        "formulas": [("query1 * 100", "")],
        "widget_type": "query_value",
    },
    {
        "name": "haproxy_success_rate_formula",
        "kind": "formula",
        "queries": [
            ("query1", "sum:haproxy.backend.response.4xx{*,*,$backend} by {haproxy_service}"),
            ("query2", "sum:haproxy.backend.response.3xx{*,*,$backend} by {haproxy_service}"),
            ("query3", "sum:haproxy.backend.response.2xx{*,*,$backend} by {haproxy_service}"),
            ("query4", "sum:haproxy.backend.response.5xx{*,*,$backend} by {haproxy_service}"),
        ],
        "formulas": [("(query3 + query1 + query2) / (query3 + query1 + query2 + query4) * 100", "")],
        "widget_type": "timeseries",
    },
    {
        "name": "default_zero_count_rate_formula",
        "kind": "formula",
        "queries": [
            (
                "upstream_services_throughput",
                "count:data_streams.latency{type:kafka AND direction:out AND (pathway_type:edge OR pathway_type:partial_edge) AND $topic AND $env} by {topic,env}.as_rate()",
            )
        ],
        "formulas": [("default_zero(upstream_services_throughput)", "")],
        "widget_type": "timeseries",
    },
    {
        "name": "rate_formula_on_gauge_fallback",
        "kind": "formula",
        "queries": [("query1", "avg:mysql.performance.user_time{*}")],
        "formulas": [("rate(query1)", "")],
        "widget_type": "timeseries",
    },
    {
        "name": "legacy_top_wrapper",
        "kind": "legacy_metric",
        "query": "top(avg:system.cpu.user{*} by {host}, 50, 'last', 'asc')",
        "widget_type": "toplist",
    },
    {
        "name": "legacy_metric_times_scalar",
        "kind": "legacy_q",
        "query": "avg:flink.task.Shuffle.Netty.Input.Buffers.inPoolUsage{*} by {task_id,subtask_index}*100",
        "widget_type": "timeseries",
    },
    {
        "name": "change_grouped_request_rate",
        "kind": "change",
        "query": "sum:nginx.net.request_per_s{*} by {service}",
        "widget_type": "change",
    },
    {
        "name": "log_free_text",
        "kind": "log",
        "query": "connection timeout",
        "widget_type": "log_stream",
    },
    {
        "name": "log_structured_range",
        "kind": "log",
        "query": "@response_time:[100 TO 500]",
        "widget_type": "log_stream",
    },
    # --- IN / NOT IN list scope filters (regression for dropped lists) -------
    {
        "name": "scope_in_list",
        "kind": "metric",
        "query": "avg:system.cpu.user{service IN (web, api, worker)} by {host}",
        "widget_type": "timeseries",
    },
    {
        "name": "scope_not_in_list",
        "kind": "metric",
        "query": "avg:system.cpu.user{env:prod AND location NOT IN (atlanta, seattle)}",
        "widget_type": "query_value",
    },
    {
        "name": "scope_in_list_with_and",
        "kind": "metric",
        "query": "avg:system.cpu.user{env:staging AND availability_zone IN (us-east-1a, us-east-1b)} by {availability_zone}",
        "widget_type": "timeseries",
    },
    # --- additional space aggregations ---------------------------------------
    {
        "name": "min_space_agg_by_device",
        "kind": "metric",
        "query": "min:system.disk.free{*} by {device}",
        "widget_type": "timeseries",
    },
    {
        "name": "max_space_agg_by_host",
        "kind": "metric",
        "query": "max:system.mem.used{*} by {host}",
        "widget_type": "timeseries",
    },
    {
        "name": "p99_percentile_by_resource",
        "kind": "metric",
        "query": "p99:trace.http.request.duration{*} by {resource_name}",
        "widget_type": "timeseries",
    },
    # --- scope negation with wildcard ----------------------------------------
    {
        "name": "scope_negation_wildcard",
        "kind": "metric",
        "query": "avg:system.cpu.user{env:prod, !host:canary*}",
        "widget_type": "query_value",
    },
    # --- formula functions: abs and diff -------------------------------------
    {
        "name": "formula_abs_distance",
        "kind": "formula",
        "queries": [("query1", "avg:system.cpu.idle{*}")],
        "formulas": [("abs(query1 - 100)", "")],
        "widget_type": "timeseries",
    },
    {
        "name": "formula_diff_counter",
        "kind": "formula",
        "queries": [("query1", "sum:redis.keyspace.hits{*}")],
        "formulas": [("diff(query1)", "")],
        "widget_type": "timeseries",
    },
    # --- legacy q scalar arithmetic ------------------------------------------
    {
        "name": "legacy_div_scalar",
        "kind": "legacy_q",
        "query": "avg:system.mem.used{*} / 1048576",
        "widget_type": "timeseries",
    },
    # --- formula functions with no ES|QL equivalent (degrade gracefully) -----
    {
        "name": "formula_timeshift_not_feasible",
        "kind": "formula",
        "queries": [("query1", "avg:system.cpu.user{*}")],
        "formulas": [("timeshift(query1, -3600)", "")],
        "widget_type": "timeseries",
    },
    {
        "name": "formula_forecast_not_feasible",
        "kind": "formula",
        "queries": [("query1", "avg:system.cpu.user{*}")],
        "formulas": [("forecast(query1, 'linear', 1)", "")],
        "widget_type": "timeseries",
    },
    {
        "name": "formula_anomalies_not_feasible",
        "kind": "formula",
        "queries": [("query1", "avg:system.cpu.user{*}")],
        "formulas": [("anomalies(query1, 'basic', 2)", "")],
        "widget_type": "timeseries",
    },
]


def _metric_query(name: str, raw_query: str) -> WidgetQuery:
    return WidgetQuery(
        name=name,
        data_source="metrics",
        raw_query=raw_query,
        metric_query=parse_metric_query(raw_query),
        query_type="metric",
    )


def _formula(raw: str, alias: str = "") -> WidgetFormula:
    formula = WidgetFormula(raw=raw, alias=alias)
    formula.expression = parse_formula(raw)
    return formula


def _build_widget(spec: CaseSpec) -> NormalizedWidget:
    kind = spec["kind"]
    widget_type = spec.get("widget_type", "timeseries")
    if kind == "metric":
        query = spec["query"]
        return NormalizedWidget(
            id="1",
            widget_type=widget_type,
            title=spec["name"],
            queries=[_metric_query("query1", query)],
        )
    if kind == "formula":
        queries = [_metric_query(name, query) for name, query in spec["queries"]]
        formulas = [_formula(raw, alias) for raw, alias in spec["formulas"]]
        return NormalizedWidget(
            id="1",
            widget_type=widget_type,
            title=spec["name"],
            queries=queries,
            formulas=formulas,
        )
    if kind == "legacy_metric":
        query = spec["query"]
        metric_query, outer_functions = parse_legacy_query(query)
        if metric_query is None:
            raise AssertionError(f"Unable to parse legacy metric query: {query}")
        metric_query.functions.extend(outer_functions)
        return NormalizedWidget(
            id="1",
            widget_type=widget_type,
            title=spec["name"],
            queries=[
                WidgetQuery(
                    name="query1",
                    data_source="metrics",
                    raw_query=query,
                    metric_query=metric_query,
                    query_type="metric",
                )
            ],
        )
    if kind == "legacy_q":
        raw_dashboard = {
            "title": spec["name"],
            "widgets": [
                {
                    "definition": {
                        "type": widget_type,
                        "title": spec["name"],
                        "requests": [{"q": spec["query"]}],
                    }
                }
            ],
        }
        return normalize_dashboard(raw_dashboard).widgets[0]
    if kind == "change":
        query = spec["query"]
        return NormalizedWidget(
            id="1",
            widget_type=widget_type,
            title=spec["name"],
            queries=[_metric_query("query1", query)],
            time={"live_span": "1w"},
            raw_definition={
                "type": "change",
                "time": {"live_span": "1w"},
                "requests": [
                    {
                        "change_type": "absolute",
                        "order_dir": "desc",
                        "compare_to": "week_before",
                        "order_by": "change",
                        "q": query,
                    }
                ],
            },
        )
    if kind == "log":
        query = spec["query"]
        return NormalizedWidget(
            id="1",
            widget_type=widget_type,
            title=spec["name"],
            queries=[
                WidgetQuery(
                    name="query1",
                    data_source="logs",
                    raw_query=query,
                    log_query=parse_log_query(query),
                    query_type="log",
                )
            ],
        )
    raise AssertionError(f"Unknown Datadog snapshot kind: {kind}")


def _translate_case(spec: CaseSpec) -> TranslationResult:
    widget = _build_widget(spec)
    plan = plan_widget(widget)
    if spec.get("force_esql", True) and plan.backend == "lens":
        plan.backend = "esql"
    return translate_widget(widget, plan, OTEL_PROFILE)


def _render_source(spec: CaseSpec) -> list[str]:
    """Render the original Datadog input as one or more ``source:`` lines.

    Documents the full ``from this -> to this`` translation in the snapshot.
    Metric / legacy / change / log queries render as a single ``source:`` line;
    formulas render one ``source: <name> = <query>`` line per sub-query plus a
    ``formula:`` line for each expression.
    """
    kind = spec["kind"]
    if kind == "formula":
        lines = [f"source: {name} = {query}" for name, query in spec["queries"]]
        for raw, alias in spec["formulas"]:
            suffix = f"  (as {alias})" if alias else ""
            lines.append(f"formula: {raw}{suffix}")
        return lines
    return [f"source: {spec['query']}"]


def _render_snapshot(spec: CaseSpec, result: TranslationResult) -> str:
    lines = [
        *_render_source(spec),
        f"status: {result.status}",
        f"backend: {result.backend}",
        f"kibana_type: {result.kibana_type}",
    ]
    for warning in result.warnings:
        lines.append(f"warning: {warning}")
    lines.append("---")
    lines.append(result.esql_query or "")
    return "\n".join(lines) + "\n"


def _diff(expected: str, actual: str) -> str:
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile="expected",
            tofile="actual",
        )
    )


class TestDatadogESQLSnapshots(unittest.TestCase):
    def _run_case(self, spec: CaseSpec) -> None:
        result = _translate_case(spec)
        actual = _render_snapshot(spec, result)
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_path = SNAPSHOT_DIR / f"{spec['name']}.txt"

        if UPDATE_SNAPSHOTS or not snapshot_path.exists():
            snapshot_path.write_text(actual, encoding="utf-8")
            if not UPDATE_SNAPSHOTS:
                self.fail(
                    f"Created new snapshot '{spec['name']}'. "
                    "Run again (or with UPDATE_SNAPSHOTS=1) to pass."
                )
            return

        expected = snapshot_path.read_text(encoding="utf-8")
        if actual != expected:
            self.fail(
                f"Snapshot mismatch for '{spec['name']}'.\n"
                "To update: UPDATE_SNAPSHOTS=1 pytest tests/test_datadog_esql_snapshots.py\n"
                f"\n{_diff(expected, actual)}"
            )


def _make_test(spec: CaseSpec):
    def test_method(self):
        self._run_case(spec)

    test_method.__name__ = f"test_{spec['name']}"
    test_method.__doc__ = str(spec.get("query") or spec.get("formulas") or spec["kind"])[:80]
    return test_method


for _case in CASES:
    setattr(TestDatadogESQLSnapshots, f"test_{_case['name']}", _make_test(_case))
