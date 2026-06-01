# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""YAML panel-construction snapshots for representative query shapes.

The ``test_promql_esql_snapshots`` and ``test_datadog_esql_snapshots`` suites
pin the *ES|QL string* produced for each query shape. This suite is the
complementary view: it pins the *Kibana panel YAML* built around each query —
chart type, time dimension, breakdown, and metric/primary field bindings — so
regressions in panel construction (wrong chart type, dropped breakdown, a
metric field that does not exist in the query output) are caught.

Two groups:

1. PromQL → Grafana panel via ``translate_panel`` (the Grafana panel builder).
2. Datadog widget → Kibana panel via ``translate_widget`` + ``_build_yaml_panel``
   (the Datadog YAML emission path).

Each case renders a compact, human-readable snapshot of the panel's ``esql``
block shape plus migration status and warnings.

Updating snapshots
------------------
    UPDATE_SNAPSHOTS=1 python -m pytest tests/test_query_yaml_snapshots.py -v

Review the diffs with ``git diff tests/snapshots/query_yaml/`` before
committing.
"""

from __future__ import annotations

import difflib
import os
import unittest
from pathlib import Path
from typing import Any

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import _build_yaml_panel
from observability_migration.adapters.source.datadog.log_parser import parse_log_query
from observability_migration.adapters.source.datadog.models import (
    NormalizedWidget,
    WidgetFormula,
    WidgetQuery,
)
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.query_parser import (
    parse_formula,
    parse_metric_query,
)
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.adapters.source.grafana.panels import translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "query_yaml"
UPDATE_SNAPSHOTS = os.environ.get("UPDATE_SNAPSHOTS") == "1"
INDEX = "metrics-*"


# ---------------------------------------------------------------------------
# PromQL cases: (name, promql_expr, grafana_panel_type)
# ---------------------------------------------------------------------------
# grafana_panel_type drives the chart-type decision in the panel builder.
PROMQL_CASES: list[tuple[str, str, str]] = [
    ("timeseries_rate_sum_by", 'sum(rate(http_requests_total{job="web"}[5m])) by (job)', "timeseries"),
    ("timeseries_simple_gauge", "node_memory_MemAvailable_bytes", "timeseries"),
    ("timeseries_sum_by_two_labels", "sum(kube_pod_info) by (namespace, pod)", "timeseries"),
    ("stat_rate_sum", "sum(rate(http_requests_total[5m]))", "stat"),
    (
        "gauge_disk_used_percent",
        '100 - ((node_filesystem_avail_bytes{mountpoint!~".*pods.*"} / node_filesystem_size_bytes) * 100)',
        "gauge",
    ),
    (
        "timeseries_arithmetic_with_labels",
        'avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
        "timeseries",
    ),
    (
        "timeseries_arithmetic_no_labels",
        '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
        "timeseries",
    ),
    ("bargauge_topk_grouped", "topk(10, sum(rate(http_requests_total[5m])) by (handler))", "bargauge"),
    ("timeseries_histogram_by_le", "sum(rate(http_request_duration_seconds_bucket[5m])) by (le)", "timeseries"),
    # not_feasible shapes still produce a (markdown) panel — confirm graceful shape.
    ("timeseries_join_not_feasible", "max(node_filesystem_avail_bytes / node_filesystem_size_bytes)", "timeseries"),
    ("stat_histogram_quantile_blocked", "histogram_quantile(0.9, rate(foo_bucket[5m]))", "stat"),
]


# ---------------------------------------------------------------------------
# Datadog cases: dicts mirroring tests/test_datadog_esql_snapshots.py specs.
# ---------------------------------------------------------------------------
CaseSpec = dict[str, Any]

DATADOG_CASES: list[CaseSpec] = [
    {"name": "metric_avg_cpu_by_host", "kind": "metric", "query": "avg:system.cpu.user{*} by {host}", "widget_type": "timeseries"},
    {"name": "metric_query_value_avg_cpu", "kind": "metric", "query": "avg:system.cpu.user{*}", "widget_type": "query_value"},
    {"name": "metric_toplist_by_host", "kind": "metric", "query": "avg:system.cpu.user{*} by {host}", "widget_type": "toplist"},
    {"name": "metric_in_list_scope", "kind": "metric", "query": "avg:system.cpu.user{service IN (web, api, worker)} by {host}", "widget_type": "timeseries"},
    {"name": "metric_p99_by_resource", "kind": "metric", "query": "p99:trace.http.request.duration{*} by {resource_name}", "widget_type": "timeseries"},
    {
        "name": "formula_success_rate",
        "kind": "formula",
        "queries": [
            ("query1", "sum:haproxy.backend.response.2xx{*} by {haproxy_service}"),
            ("query2", "sum:haproxy.backend.response.5xx{*} by {haproxy_service}"),
        ],
        "formulas": [("query1 / (query1 + query2) * 100", "")],
        "widget_type": "timeseries",
    },
    {"name": "change_request_rate", "kind": "change", "query": "sum:nginx.net.request_per_s{*} by {service}", "widget_type": "change"},
    {"name": "log_free_text", "kind": "log", "query": "connection timeout", "widget_type": "log_stream"},
    {
        "name": "formula_forecast_not_feasible",
        "kind": "formula",
        "queries": [("query1", "avg:system.cpu.user{*}")],
        "formulas": [("forecast(query1, 'linear', 1)", "")],
        "widget_type": "timeseries",
    },
]


# ---------------------------------------------------------------------------
# Datadog widget builders (shared shape with test_datadog_esql_snapshots.py)
# ---------------------------------------------------------------------------

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
        return NormalizedWidget(
            id="1",
            widget_type=widget_type,
            title=spec["name"],
            queries=[_metric_query("query1", spec["query"])],
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
        return NormalizedWidget(
            id="1",
            widget_type=widget_type,
            title=spec["name"],
            queries=[
                WidgetQuery(
                    name="query1",
                    data_source="logs",
                    raw_query=spec["query"],
                    log_query=parse_log_query(spec["query"]),
                    query_type="log",
                )
            ],
        )
    raise AssertionError(f"Unknown Datadog YAML snapshot kind: {kind}")


# ---------------------------------------------------------------------------
# Snapshot rendering
# ---------------------------------------------------------------------------

def _field_of(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("field", ""))
    return str(value) if value else ""


def _datadog_source_lines(spec: CaseSpec) -> list[str]:
    """Render the original Datadog input as ``source:`` / ``formula:`` lines."""
    if spec["kind"] == "formula":
        lines = [f"source: {name} = {query}" for name, query in spec["queries"]]
        for raw, alias in spec["formulas"]:
            suffix = f"  (as {alias})" if alias else ""
            lines.append(f"formula: {raw}{suffix}")
        return lines
    return [f"source: {spec['query']}"]


def _render_panel_snapshot(
    status: str,
    warnings: list[str],
    esql_block: dict[str, Any],
    source: list[str] | None = None,
) -> str:
    """Render a compact snapshot of one panel's YAML construction.

    ``source`` records the original PromQL/Datadog input so each snapshot
    documents the full ``from this -> to this`` translation in one place.
    """
    lines = list(source or [])
    lines += [
        f"status: {status}",
        f"chart_type: {esql_block.get('type', 'none')}",
    ]
    if "dimension" in esql_block:
        lines.append(f"dimension: {_field_of(esql_block['dimension'])}")
    if "breakdown" in esql_block:
        lines.append(f"breakdown: {_field_of(esql_block['breakdown'])}")
    if "breakdowns" in esql_block:
        lines.append(f"breakdowns: {[_field_of(b) for b in esql_block['breakdowns']]}")
    if "metrics" in esql_block:
        lines.append(f"metrics: {[_field_of(m) for m in esql_block['metrics']]}")
    if "primary" in esql_block:
        lines.append(f"primary: {_field_of(esql_block['primary'])}")
    if "metric" in esql_block:
        lines.append(f"metric: {_field_of(esql_block['metric'])}")
    if "mode" in esql_block:
        lines.append(f"mode: {esql_block['mode']}")
    for w in warnings:
        lines.append(f"warning: {w}")
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


def _check_snapshot(test: unittest.TestCase, name: str, actual: str) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = SNAPSHOT_DIR / f"{name}.txt"
    if UPDATE_SNAPSHOTS or not snapshot_path.exists():
        snapshot_path.write_text(actual, encoding="utf-8")
        if not UPDATE_SNAPSHOTS:
            test.fail(
                f"Created new snapshot '{name}'. "
                "Run again (or with UPDATE_SNAPSHOTS=1) to pass."
            )
        return
    expected = snapshot_path.read_text(encoding="utf-8")
    if actual != expected:
        test.fail(
            f"Snapshot mismatch for '{name}'.\n"
            "To update: UPDATE_SNAPSHOTS=1 pytest tests/test_query_yaml_snapshots.py\n"
            f"\n{_diff(expected, actual)}"
        )


# ---------------------------------------------------------------------------
# Test class 1: PromQL → Grafana panel YAML
# ---------------------------------------------------------------------------

class TestPromQLYAMLSnapshots(unittest.TestCase):
    _rule_pack: RulePackConfig
    _resolver: SchemaResolver

    @classmethod
    def setUpClass(cls):
        cls._rule_pack = RulePackConfig()
        cls._resolver = SchemaResolver(cls._rule_pack)

    def _run_case(self, name: str, expr: str, grafana_type: str) -> None:
        panel = {
            "title": name,
            "type": grafana_type,
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"refId": "A", "expr": expr}],
        }
        yaml_panel, result = translate_panel(
            panel,
            datasource_index=INDEX,
            esql_index=INDEX,
            rule_pack=self._rule_pack,
            resolver=self._resolver,
        )
        esql_block = (yaml_panel or {}).get("esql", {}) if yaml_panel else {}
        actual = _render_panel_snapshot(
            result.status,
            list(getattr(result, "reasons", [])),
            esql_block,
            source=[f"source: {expr}"],
        )
        _check_snapshot(self, f"promql__{name}", actual)


def _make_promql_test(name: str, expr: str, grafana_type: str):
    def test_method(self):
        self._run_case(name, expr, grafana_type)

    test_method.__name__ = f"test_{name}"
    test_method.__doc__ = expr[:80]
    return test_method


for _name, _expr, _gtype in PROMQL_CASES:
    setattr(TestPromQLYAMLSnapshots, f"test_{_name}", _make_promql_test(_name, _expr, _gtype))


# ---------------------------------------------------------------------------
# Test class 2: Datadog widget → Kibana panel YAML
# ---------------------------------------------------------------------------

class TestDatadogYAMLSnapshots(unittest.TestCase):
    def _run_case(self, spec: CaseSpec) -> None:
        widget = _build_widget(spec)
        plan = plan_widget(widget)
        if spec.get("force_esql", True) and plan.backend == "lens":
            plan.backend = "esql"
        result = translate_widget(widget, plan, OTEL_PROFILE)
        panel = _build_yaml_panel(widget, result, INDEX)
        esql_block = (panel or {}).get("esql", {})
        actual = _render_panel_snapshot(
            result.status,
            list(result.warnings),
            esql_block,
            source=_datadog_source_lines(spec),
        )
        _check_snapshot(self, f"datadog__{spec['name']}", actual)


def _make_datadog_test(spec: CaseSpec):
    def test_method(self):
        self._run_case(spec)

    test_method.__name__ = f"test_{spec['name']}"
    test_method.__doc__ = str(spec.get("query") or spec.get("formulas") or spec["kind"])[:80]
    return test_method


for _case in DATADOG_CASES:
    setattr(TestDatadogYAMLSnapshots, f"test_{_case['name']}", _make_datadog_test(_case))
