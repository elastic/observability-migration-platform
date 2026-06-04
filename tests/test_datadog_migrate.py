# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the Datadog → Kibana migration tool.

Covers:
    - Metric query parser
    - Log search parser
    - Formula parser
    - Dashboard normalization
    - Backend planner
    - Query translation
    - YAML generation
    - End-to-end pipeline
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observability_migration.adapters.source.datadog import alert_pipeline as datadog_alert_pipeline
from observability_migration.adapters.source.datadog import cli as datadog_cli
from observability_migration.adapters.source.datadog import extract as datadog_extract
from observability_migration.adapters.source.datadog import preflight as datadog_preflight
from observability_migration.adapters.source.datadog.execution import build_source_execution_summary
from observability_migration.adapters.source.datadog.field_map import (
    OTEL_PROFILE,
    PASSTHROUGH_PROFILE,
    PROMETHEUS_PROFILE,
    FieldMapProfile,
    load_profile,
)
from observability_migration.adapters.source.datadog.generate import generate_dashboard_yaml
from observability_migration.adapters.source.datadog.log_parser import (
    log_ast_to_esql_where,
    log_ast_to_kql,
    parse_log_query,
    parse_log_query_result,
)
from observability_migration.adapters.source.datadog.manifest import build_migration_manifest
from observability_migration.adapters.source.datadog.models import (
    DashboardResult,
    FormulaBinOp,
    FormulaFuncCall,
    FormulaNumber,
    FormulaRef,
    LogAttributeFilter,
    LogBoolOp,
    LogNot,
    LogRange,
    LogTerm,
    NormalizedDashboard,
    NormalizedWidget,
    TranslationResult,
    WidgetFormula,
    WidgetQuery,
)
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.query_parser import (
    ParseError,
    parse_formula,
    parse_formula_result,
    parse_legacy_query,
    parse_metric_query,
    parse_metric_query_result,
)
from observability_migration.adapters.source.datadog.report import save_detailed_report
from observability_migration.adapters.source.datadog.rollout import build_rollout_plan
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.adapters.source.datadog.verification import annotate_results_with_verification
from observability_migration.core.verification.field_capabilities import FieldCapability
from observability_migration.targets.kibana.adapter import KibanaTargetAdapter

# =========================================================================
# Metric Query Parser Tests
# =========================================================================

class TestMetricQueryParser(unittest.TestCase):
    """Tests for Datadog metric query string parsing."""

    def test_simple_query(self):
        mq = parse_metric_query("avg:system.cpu.user{*}")
        self.assertEqual(mq.space_agg, "avg")
        self.assertEqual(mq.metric, "system.cpu.user")
        self.assertEqual(mq.scope, [])
        self.assertEqual(mq.group_by, [])

    def test_query_with_scope_filter(self):
        mq = parse_metric_query("avg:system.cpu.user{host:web01}")
        self.assertEqual(mq.space_agg, "avg")
        self.assertEqual(mq.metric, "system.cpu.user")
        self.assertEqual(len(mq.scope), 1)
        self.assertEqual(mq.scope[0].key, "host")
        self.assertEqual(mq.scope[0].value, "web01")
        self.assertFalse(mq.scope[0].negated)

    def test_query_with_multiple_filters(self):
        mq = parse_metric_query("avg:system.cpu.user{host:web01,env:prod}")
        self.assertEqual(len(mq.scope), 2)
        self.assertEqual(mq.scope[0].key, "host")
        self.assertEqual(mq.scope[1].key, "env")
        self.assertEqual(mq.scope[1].value, "prod")

    def test_query_with_negated_filter(self):
        mq = parse_metric_query("avg:system.cpu.user{!env:staging}")
        self.assertEqual(len(mq.scope), 1)
        self.assertTrue(mq.scope[0].negated)
        self.assertEqual(mq.scope[0].key, "env")
        self.assertEqual(mq.scope[0].value, "staging")

    def test_query_with_dash_negation(self):
        mq = parse_metric_query("avg:system.cpu.user{-env:staging}")
        self.assertTrue(mq.scope[0].negated)

    def test_query_with_group_by(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        self.assertEqual(mq.group_by, ["host"])

    def test_query_with_multi_group_by(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host,env}")
        self.assertEqual(mq.group_by, ["host", "env"])

    def test_query_with_rollup(self):
        mq = parse_metric_query("avg:system.disk.free{*}.rollup(avg, 60)")
        self.assertIsNotNone(mq.rollup)
        self.assertEqual(mq.rollup.name, "rollup")
        self.assertEqual(mq.rollup.args, ["avg", 60])

    def test_query_with_fill(self):
        mq = parse_metric_query("avg:system.cpu.user{*}.fill(zero)")
        self.assertEqual(mq.fill_value, "zero")

    def test_query_with_as_count(self):
        mq = parse_metric_query("sum:http.requests{*}.as_count()")
        self.assertTrue(mq.as_count)
        self.assertFalse(mq.as_rate)

    def test_query_with_as_rate(self):
        mq = parse_metric_query("sum:http.requests{*}.as_rate()")
        self.assertTrue(mq.as_rate)
        self.assertFalse(mq.as_count)

    def test_query_with_chained_functions(self):
        mq = parse_metric_query(
            "avg:system.cpu.user{*}.rollup(avg, 60).fill(zero)"
        )
        self.assertEqual(len(mq.functions), 2)
        self.assertEqual(mq.functions[0].name, "rollup")
        self.assertEqual(mq.functions[1].name, "fill")

    def test_sum_aggregator(self):
        mq = parse_metric_query("sum:trace.flask.request.hits{service:web}")
        self.assertEqual(mq.space_agg, "sum")
        self.assertEqual(mq.metric, "trace.flask.request.hits")
        self.assertEqual(mq.scope[0].key, "service")

    def test_count_aggregator(self):
        mq = parse_metric_query("count:events{*}")
        self.assertEqual(mq.space_agg, "count")

    def test_template_variable_in_scope(self):
        mq = parse_metric_query("avg:system.cpu.user{$host}")
        self.assertEqual(len(mq.scope), 1)
        self.assertEqual(mq.scope[0].key, "$host")
        self.assertEqual(mq.scope[0].value, "*")

    def test_boolean_scope_with_and_before_paren(self):
        mq = parse_metric_query(
            "avg:system.cpu.user{env:prod AND(host:web01 OR host:web02)}"
        )
        scope_map = {item.key: item.value for item in mq.scope}
        self.assertEqual(scope_map["env"], "prod")
        self.assertEqual(scope_map["host"], "web01|web02")

    def test_boolean_scope_with_no_space_before_and(self):
        mq = parse_metric_query(
            "avg:system.cpu.user{(host:web01 OR host:web02)AND env:prod}"
        )
        scope_map = {item.key: item.value for item in mq.scope}
        self.assertEqual(scope_map["env"], "prod")
        self.assertEqual(scope_map["host"], "web01|web02")

    def test_wildcard_in_scope_value(self):
        mq = parse_metric_query("avg:system.cpu.user{host:web*}")
        self.assertEqual(mq.scope[0].value, "web*")

    def test_scope_tags_property(self):
        mq = parse_metric_query("avg:system.cpu.user{host:web01,!env:staging}")
        self.assertEqual(mq.scope_tags, {"host": "web01"})
        self.assertEqual(mq.negated_tags, {"env": "staging"})

    def test_complex_query(self):
        mq = parse_metric_query(
            "avg:system.mem.usable{env:prod,!host:db01} by {host,service}.rollup(avg, 300)"
        )
        self.assertEqual(mq.space_agg, "avg")
        self.assertEqual(mq.metric, "system.mem.usable")
        self.assertEqual(len(mq.scope), 2)
        self.assertEqual(mq.group_by, ["host", "service"])
        self.assertIsNotNone(mq.rollup)
        self.assertEqual(mq.rollup.args, ["avg", 300])

    def test_empty_query_raises(self):
        with self.assertRaises(ParseError):
            parse_metric_query("")

    def test_no_colon_raises(self):
        with self.assertRaises(ParseError):
            parse_metric_query("invalid query string")

    def test_trailing_tokens_raise(self):
        with self.assertRaises(ParseError):
            parse_metric_query("avg:system.cpu.user{*} trailing")

    def test_trailing_tokens_result_reports_diagnostic(self):
        result = parse_metric_query_result("avg:system.cpu.user{*} trailing")
        self.assertIsNone(result.value)
        self.assertTrue(result.degraded)
        self.assertFalse(result.lossless)
        self.assertEqual(result.diagnostics[0].code, "METRIC_TRAILING_TOKENS")


# =========================================================================
# Formula Parser Tests
# =========================================================================

class TestFormulaParser(unittest.TestCase):
    """Tests for Datadog formula expression parsing."""

    def test_simple_ref(self):
        fe = parse_formula("query1")
        self.assertIsInstance(fe.ast, FormulaRef)
        self.assertEqual(fe.ast.name, "query1")

    def test_arithmetic(self):
        fe = parse_formula("query1 + query2")
        self.assertIsInstance(fe.ast, FormulaBinOp)
        self.assertEqual(fe.ast.op, "+")
        self.assertIsInstance(fe.ast.left, FormulaRef)
        self.assertIsInstance(fe.ast.right, FormulaRef)

    def test_division(self):
        fe = parse_formula("query1 / query2")
        self.assertEqual(fe.ast.op, "/")

    def test_multiply_with_constant(self):
        fe = parse_formula("query1 * 100")
        self.assertEqual(fe.ast.op, "*")
        self.assertIsInstance(fe.ast.right, FormulaNumber)
        self.assertEqual(fe.ast.right.value, 100.0)

    def test_function_call(self):
        fe = parse_formula("per_second(query1)")
        self.assertIsInstance(fe.ast, FormulaFuncCall)
        self.assertEqual(fe.ast.name, "per_second")
        self.assertEqual(len(fe.ast.args), 1)
        self.assertIsInstance(fe.ast.args[0], FormulaRef)

    def test_nested_function(self):
        fe = parse_formula("abs(query1 - query2)")
        self.assertIsInstance(fe.ast, FormulaFuncCall)
        self.assertEqual(fe.ast.name, "abs")
        self.assertIsInstance(fe.ast.args[0], FormulaBinOp)

    def test_complex_formula(self):
        fe = parse_formula("query1 / query2 * 100")
        self.assertIsInstance(fe.ast, FormulaBinOp)
        self.assertEqual(fe.ast.op, "*")

    def test_top_formula_with_string_args_parses(self):
        # Datadog's top() takes string literals — the tokenizer must accept them.
        fe = parse_formula("top(query1, 10, 'mean', 'desc')")
        self.assertIsInstance(fe.ast, FormulaFuncCall)
        self.assertEqual(fe.ast.name, "top")
        self.assertEqual(len(fe.ast.args), 4)

    def test_top_formula_with_expression_arg(self):
        fe = parse_formula("top(query1 / 1000, 10, 'mean', 'desc')")
        self.assertIsInstance(fe.ast, FormulaFuncCall)
        self.assertEqual(fe.ast.name, "top")
        self.assertIsInstance(fe.ast.args[0], FormulaBinOp)

    def test_referenced_queries(self):
        fe = parse_formula("per_second(query1) / query2 * 100")
        refs = fe.referenced_queries
        self.assertIn("query1", refs)
        self.assertIn("query2", refs)

    def test_empty_formula(self):
        fe = parse_formula("")
        self.assertIsNone(fe.ast)

    def test_invalid_formula_result_reports_diagnostic(self):
        result = parse_formula_result("query1 + )")
        self.assertIsNone(result.value)
        self.assertTrue(result.degraded)
        self.assertFalse(result.lossless)
        self.assertEqual(result.diagnostics[0].code, "FORMULA_PARSE_ERROR")

    def test_precedence(self):
        fe = parse_formula("query1 + query2 * query3")
        self.assertIsInstance(fe.ast, FormulaBinOp)
        self.assertEqual(fe.ast.op, "+")
        self.assertIsInstance(fe.ast.right, FormulaBinOp)
        self.assertEqual(fe.ast.right.op, "*")

    def test_parenthesized_expression(self):
        fe = parse_formula("(query1 + query2) * query3")
        self.assertIsInstance(fe.ast, FormulaBinOp)
        self.assertEqual(fe.ast.op, "*")
        self.assertIsInstance(fe.ast.left, FormulaBinOp)
        self.assertEqual(fe.ast.left.op, "+")

    def test_clamp_min(self):
        fe = parse_formula("clamp_min(query1, 0)")
        self.assertIsInstance(fe.ast, FormulaFuncCall)
        self.assertEqual(fe.ast.name, "clamp_min")
        self.assertEqual(len(fe.ast.args), 2)


# =========================================================================
# Legacy Query Parser Tests
# =========================================================================

class TestLegacyQueryParser(unittest.TestCase):

    def test_plain_metric_query(self):
        mq, fns = parse_legacy_query("avg:system.cpu.user{*}")
        self.assertIsNotNone(mq)
        self.assertEqual(mq.metric, "system.cpu.user")
        self.assertEqual(fns, [])

    def test_wrapped_in_per_second(self):
        mq, fns = parse_legacy_query("per_second(avg:system.cpu.user{*})")
        self.assertIsNotNone(mq)
        self.assertEqual(mq.metric, "system.cpu.user")
        self.assertEqual(len(fns), 1)
        self.assertEqual(fns[0].name, "per_second")

    def test_wrapped_in_top(self):
        mq, fns = parse_legacy_query(
            "top(avg:system.cpu.user{*} by {host}, 10, 'mean', 'desc')"
        )
        self.assertIsNotNone(mq)
        self.assertEqual(mq.group_by, ["host"])
        self.assertEqual(fns[0].name, "top")
        self.assertEqual(fns[0].args, [10, "mean", "desc"])

    def test_malformed_wrapped_query_does_not_leak_outer_functions(self):
        mq, fns = parse_legacy_query("per_second(invalid query)")
        self.assertIsNone(mq)
        self.assertEqual(fns, [])

    def test_top_with_multi_value_braced_scope_parses(self):
        # Regression: _split_args didn't track brace depth, so commas inside
        # {$host,$scope} were treated as argument separators, mangling the
        # metric query string and causing a ParseError.
        raw = "top(avg:apache.performance.cpu_load{$host,$scope} by {host}, 10, 'mean', 'desc')"
        mq, fns = parse_legacy_query(raw)
        self.assertIsNotNone(mq, "query with braced multi-value scope should parse")
        self.assertEqual(mq.metric, "apache.performance.cpu_load")
        self.assertEqual(mq.group_by, ["host"])
        self.assertEqual(len(fns), 1)
        self.assertEqual(fns[0].name, "top")
        self.assertEqual(fns[0].args, [10, "mean", "desc"])

    def test_multi_value_braced_scope_without_wrapper_parses(self):
        # Same brace-depth fix should handle unwrapped queries too.
        mq, _fns = parse_legacy_query("avg:system.cpu.user{host:web01,env:prod} by {host}")
        self.assertIsNotNone(mq)
        self.assertEqual(mq.metric, "system.cpu.user")
        self.assertEqual(len(mq.scope), 2)


# =========================================================================
# Log Parser Tests
# =========================================================================

class TestLogParser(unittest.TestCase):
    """Tests for Datadog log search syntax parsing."""

    def test_empty_query(self):
        lq = parse_log_query("")
        self.assertTrue(lq.is_empty)

    def test_wildcard_query(self):
        lq = parse_log_query("*")
        self.assertTrue(lq.is_empty)

    def test_simple_term(self):
        lq = parse_log_query("error")
        self.assertIsInstance(lq.ast, LogTerm)
        self.assertEqual(lq.ast.value, "error")

    def test_quoted_phrase(self):
        lq = parse_log_query('"connection refused"')
        self.assertIsInstance(lq.ast, LogTerm)
        self.assertTrue(lq.ast.quoted)
        self.assertEqual(lq.ast.value, "connection refused")

    def test_attribute_filter(self):
        lq = parse_log_query("@http.status_code:500")
        self.assertIsInstance(lq.ast, LogAttributeFilter)
        self.assertEqual(lq.ast.attribute, "http.status_code")
        self.assertEqual(lq.ast.value, "500")

    def test_reserved_tag_filter(self):
        lq = parse_log_query("service:web")
        self.assertIsInstance(lq.ast, LogAttributeFilter)
        self.assertEqual(lq.ast.attribute, "service")
        self.assertTrue(lq.ast.is_tag)

    def test_and_boolean(self):
        lq = parse_log_query("service:web AND status:error")
        self.assertIsInstance(lq.ast, LogBoolOp)
        self.assertEqual(lq.ast.op, "AND")
        self.assertEqual(len(lq.ast.children), 2)

    def test_or_boolean(self):
        lq = parse_log_query("service:web OR service:api")
        self.assertIsInstance(lq.ast, LogBoolOp)
        self.assertEqual(lq.ast.op, "OR")

    def test_implicit_and(self):
        lq = parse_log_query("service:web status:error")
        self.assertIsInstance(lq.ast, LogBoolOp)
        self.assertEqual(lq.ast.op, "AND")

    def test_negation(self):
        lq = parse_log_query("-service:web")
        self.assertIsInstance(lq.ast, LogNot)

    def test_wildcard_pattern(self):
        lq = parse_log_query("host:web*")
        self.assertIsInstance(lq.ast, LogAttributeFilter)
        self.assertEqual(lq.ast.value, "web*")

    def test_range_filter(self):
        lq = parse_log_query("@http.status_code:[400 TO 499]")
        self.assertIsInstance(lq.ast, LogRange)
        self.assertEqual(lq.ast.low, "400")
        self.assertEqual(lq.ast.high, "499")

    def test_complex_query(self):
        lq = parse_log_query('service:web AND status:error @http.url:"/api/*"')
        self.assertIsInstance(lq.ast, LogBoolOp)

    def test_grouped_boolean_precedence(self):
        lq = parse_log_query("service:web AND (status:error OR status:warn)")
        self.assertIsInstance(lq.ast, LogBoolOp)
        self.assertEqual(lq.ast.op, "AND")
        self.assertIsInstance(lq.ast.children[1], LogBoolOp)
        self.assertEqual(lq.ast.children[1].op, "OR")

    def test_unbalanced_group_is_recovered(self):
        lq = parse_log_query("service:web AND (status:error OR host:api")
        esql = log_ast_to_esql_where(lq.ast)
        self.assertIn('service == "web"', esql)
        self.assertIn('status == "error"', esql)
        self.assertIn('host == "api"', esql)

    def test_unbalanced_group_result_reports_fallback_diagnostic(self):
        result = parse_log_query_result("service:web AND (status:error OR host:api")
        self.assertTrue(result.degraded)
        codes = {diagnostic.code for diagnostic in result.diagnostics}
        self.assertIn("LOG_BOOLEAN_FALLBACK", codes)

    def test_ast_to_kql(self):
        lq = parse_log_query("service:web AND status:error")
        kql = log_ast_to_kql(lq.ast)
        self.assertIn("service", kql)
        self.assertIn("status", kql)

    def test_ast_to_kql_unescapes_datadog_forward_slash(self):
        # Datadog log searches escape forward slashes (``felix\/int_dataplane.go``)
        # but KQL has no ``\/`` escape and the Elasticsearch KQL parser rejects it
        # with a "token recognition error". A forward slash never needs escaping
        # in KQL, so the backslash must be dropped to produce parseable KQL.
        lq = parse_log_query("felix\\/int_dataplane.go")
        kql = log_ast_to_kql(lq.ast)
        self.assertNotIn("\\/", kql, f"emitted invalid KQL escape: {kql!r}")
        self.assertIn("felix/int_dataplane.go", kql)

    def test_ast_to_esql_where(self):
        lq = parse_log_query("service:web AND status:error")
        esql = log_ast_to_esql_where(lq.ast)
        self.assertIn("==", esql)

    def test_grouped_numeric_attribute_filter(self):
        lq = parse_log_query("@http.status_code:(404 OR 500)")
        esql = log_ast_to_esql_where(lq.ast)
        self.assertIn("http.status_code == 404", esql)
        self.assertIn("OR", esql)

    def test_grouped_numeric_attribute_filter_quotes_keyword_fields(self):
        profile = FieldMapProfile(
            name="typed-logs",
            logs_index="logs-*",
            log_field_caps={
                "http.status_code": FieldCapability(
                    name="http.status_code",
                    type="keyword",
                    searchable=True,
                    aggregatable=True,
                )
            },
        )
        lq = parse_log_query("@http.status_code:(404 OR 500)")
        esql = log_ast_to_esql_where(lq.ast, profile)
        self.assertIn('http.status_code == "404"', esql)
        self.assertIn('http.status_code == "500"', esql)
        self.assertIn("OR", esql)

    def test_numeric_comparison_and_template_term(self):
        lq = parse_log_query("source:squid @http.status_code:>=400 $Protocol")
        esql = log_ast_to_esql_where(lq.ast, {"source": "service.name"})
        self.assertIn("service.name == \"squid\"", esql)
        self.assertIn("http.status_code >= 400", esql)
        self.assertNotIn("Protocol", esql)


# =========================================================================
# Normalization Tests
# =========================================================================

class TestNormalization(unittest.TestCase):

    def _load_sample(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        return json.loads(path.read_text())

    def test_normalize_sample_dashboard(self):
        raw = self._load_sample()
        nd = normalize_dashboard(raw)
        self.assertEqual(nd.title, "System Overview - Sample")
        self.assertEqual(len(nd.widgets), 11)
        self.assertEqual(len(nd.template_variables), 2)

    def test_widget_types_extracted(self):
        raw = self._load_sample()
        nd = normalize_dashboard(raw)
        types = [w.widget_type for w in nd.widgets]
        self.assertIn("timeseries", types)
        self.assertIn("query_value", types)
        self.assertIn("toplist", types)
        self.assertIn("note", types)
        self.assertIn("table", types)
        self.assertIn("heatmap", types)
        self.assertIn("manage_status", types)

    def test_metric_queries_parsed(self):
        raw = self._load_sample()
        nd = normalize_dashboard(raw)
        cpu_widget = nd.widgets[0]
        self.assertEqual(len(cpu_widget.queries), 1)
        self.assertEqual(cpu_widget.queries[0].data_source, "metrics")
        self.assertIsNotNone(cpu_widget.queries[0].metric_query)
        self.assertEqual(cpu_widget.queries[0].metric_query.metric, "system.cpu.user")

    def test_log_queries_parsed(self):
        raw = self._load_sample()
        nd = normalize_dashboard(raw)
        log_widget = nd.widgets[6]
        self.assertEqual(log_widget.widget_type, "timeseries")
        self.assertTrue(log_widget.has_log_queries)

    def test_multi_query_widget(self):
        raw = self._load_sample()
        nd = normalize_dashboard(raw)
        disk_widget = nd.widgets[4]
        self.assertEqual(len(disk_widget.queries), 2)
        self.assertEqual(len(disk_widget.formulas), 1)
        self.assertEqual(disk_widget.formulas[0].raw, "query1 + query2")

    def test_legacy_q_arithmetic_part_normalizes_to_metric_queries_and_formula(self):
        raw = {
            "title": "Legacy arithmetic",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "title": "System memory",
                        "requests": [
                            {
                                "q": (
                                    "sum:system.mem.usable{$scope},"
                                    "sum:system.mem.total{$scope}-sum:system.mem.usable{$scope}"
                                )
                            }
                        ],
                    }
                }
            ],
        }

        nd = normalize_dashboard(raw)
        widget = nd.widgets[0]

        self.assertEqual([q.query_type for q in widget.queries], ["metric", "metric", "metric"])
        self.assertEqual([q.metric_query.metric for q in widget.queries], [
            "system.mem.usable",
            "system.mem.total",
            "system.mem.usable",
        ])
        self.assertEqual([f.raw for f in widget.formulas], ["query0", "query1-query2"])

    def test_legacy_q_arithmetic_with_non_count_as_count_stays_manual(self):
        raw = {
            "title": "Legacy arithmetic",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "title": "Response percentage",
                        "requests": [
                            {
                                "q": (
                                    "100 * sum:gunicorn.request.status.500{*}.as_count() / "
                                    "sum:gunicorn.requests{*}.as_count()"
                                )
                            }
                        ],
                    }
                }
            ],
        }

        widget = normalize_dashboard(raw).widgets[0]

        self.assertEqual([q.query_type for q in widget.queries], ["legacy_unparsed"])
        self.assertEqual(widget.formulas, [])

    def test_legacy_q_heatmap_expression_without_group_stays_manual(self):
        raw = {
            "title": "Legacy heatmap",
            "widgets": [
                {
                    "definition": {
                        "type": "heatmap",
                        "title": "Latency ratio",
                        "requests": [
                            {
                                "q": (
                                    "sum:cilium.policy.regeneration_time_stats.seconds.sum{*}/"
                                    "sum:cilium.policy.regeneration_time_stats.seconds.count{upper_bound:none}"
                                )
                            }
                        ],
                    }
                }
            ],
        }

        widget = normalize_dashboard(raw).widgets[0]

        self.assertEqual([q.query_type for q in widget.queries], ["legacy_unparsed"])
        self.assertEqual(widget.formulas, [])

    def test_template_variables(self):
        raw = self._load_sample()
        nd = normalize_dashboard(raw)
        self.assertEqual(nd.template_variables[0].name, "host")
        self.assertEqual(nd.template_variables[1].name, "env")
        self.assertEqual(nd.template_variables[1].default, "prod")

    def test_unsupported_widget_flagged(self):
        raw = self._load_sample()
        nd = normalize_dashboard(raw)
        monitor_widget = nd.widgets[9]
        self.assertEqual(monitor_widget.widget_type, "manage_status")
        self.assertFalse(monitor_widget.is_supported)

    def test_legacy_wrapper_functions_preserved(self):
        raw = {
            "title": "Legacy",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "requests": [{"q": "per_second(avg:system.cpu.user{*})"}],
                    }
                }
            ],
        }
        nd = normalize_dashboard(raw)
        mq = nd.widgets[0].queries[0].metric_query
        self.assertIsNotNone(mq)
        self.assertEqual(mq.functions[-1].name, "per_second")

    def test_event_stream_in_list_widget_stays_unsupported(self):
        raw = {
            "title": "Events",
            "widgets": [
                {
                    "definition": {
                        "type": "list_stream",
                        "requests": [
                            {
                                "query": {
                                    "query_string": "source:kubernetes $node",
                                    "data_source": "event_stream",
                                }
                            }
                        ],
                    }
                }
            ],
        }
        nd = normalize_dashboard(raw)
        plan = plan_widget(nd.widgets[0])
        self.assertEqual(plan.backend, "markdown")

    def test_modern_bare_metric_query_is_parsed(self):
        raw = {
            "title": "Bare metric",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "requests": [
                            {
                                "queries": [
                                    {
                                        "data_source": "metrics",
                                        "name": "query1",
                                        "query": "system.load.1{$scope}",
                                    }
                                ],
                                "formulas": [{"formula": "query1"}],
                            }
                        ],
                    }
                }
            ],
        }
        nd = normalize_dashboard(raw)
        widget = nd.widgets[0]
        self.assertEqual(widget.queries[0].query_type, "metric")
        self.assertIsNotNone(widget.queries[0].metric_query)
        self.assertEqual(widget.queries[0].metric_query.metric, "system.load.1")

    def test_duplicate_query_names_are_rewritten_per_request(self):
        raw = {
            "title": "Duplicate refs",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "requests": [
                            {
                                "queries": [
                                    {
                                        "data_source": "metrics",
                                        "name": "query1",
                                        "query": "avg:postgresql.rows_fetched{*}",
                                    }
                                ],
                                "formulas": [{"formula": "query1"}],
                            },
                            {
                                "queries": [
                                    {
                                        "data_source": "metrics",
                                        "name": "query1",
                                        "query": "avg:postgresql.rows_returned{*}",
                                    }
                                ],
                                "formulas": [{"formula": "query1"}],
                            },
                        ],
                    }
                }
            ],
        }
        nd = normalize_dashboard(raw)
        widget = nd.widgets[0]
        self.assertEqual([q.name for q in widget.queries], ["query1", "query1_2"])
        self.assertEqual([f.raw for f in widget.formulas], ["query1", "query1_2"])

    def test_ordered_dashboard_without_layout_gets_synthetic_rows(self):
        raw = {
            "title": "Ordered",
            "layout_type": "ordered",
            "widgets": [
                {"definition": {"type": "timeseries", "requests": [{"queries": [{"data_source": "metrics", "name": "a", "query": "avg:system.cpu.user{*}"}]}]}},
                {"definition": {"type": "timeseries", "requests": [{"queries": [{"data_source": "metrics", "name": "b", "query": "avg:system.cpu.system{*}"}]}]}},
                {"definition": {"type": "timeseries", "requests": [{"queries": [{"data_source": "metrics", "name": "c", "query": "avg:system.cpu.idle{*}"}]}]}},
            ],
        }
        nd = normalize_dashboard(raw)
        self.assertEqual([w.layout["y"] for w in nd.widgets], [0, 4, 8])
        self.assertTrue(all(w.layout["x"] == 0 for w in nd.widgets))


# =========================================================================
# Planner Tests
# =========================================================================

class TestPlanner(unittest.TestCase):

    def _make_widget(self, **kwargs):
        return NormalizedWidget(**kwargs)

    def test_text_widget_plans_markdown(self):
        w = self._make_widget(id="1", widget_type="note", title="Notes")
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "markdown")

    def test_unsupported_type_blocked(self):
        w = self._make_widget(id="1", widget_type="manage_status", title="Monitors")
        plan = plan_widget(w)
        self.assertIn(plan.backend, ("blocked", "markdown"))

    def test_metric_timeseries_plans_lens_or_esql(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="avg:system.cpu.user{*} by {host}", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="timeseries", title="CPU", queries=[wq])
        plan = plan_widget(w)
        self.assertIn(plan.backend, ("esql", "lens"))
        self.assertEqual(plan.kibana_type, "xy")

    def test_query_value_plans_metric(self):
        mq = parse_metric_query("avg:system.cpu.user{*}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="avg:system.cpu.user{*}", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="query_value", title="CPU Avg", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")
        self.assertEqual(plan.kibana_type, "metric")

    def test_toplist_plans_table(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="toplist", title="Top", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")
        self.assertEqual(plan.kibana_type, "table")

    def test_bar_chart_plans_grouped_aggregation_table(self):
        # Datadog bar_chart is a grouped aggregation (group_by + compute +
        # sort + limit), structurally identical to a toplist. It must be a
        # supported widget — not an "unsupported widget type" not_feasible.
        mq = parse_metric_query("sum:system.net.bytes_rcvd{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="bar_chart", title="Bars", queries=[wq])
        plan = plan_widget(w)
        self.assertIn(plan.backend, ("esql", "lens"))
        self.assertEqual(plan.kibana_type, "table")
        self.assertNotIn(
            "unsupported widget type: bar_chart",
            plan.reasons,
        )

    def test_bar_chart_translates_without_not_feasible(self):
        mq = parse_metric_query("sum:system.net.bytes_rcvd{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="bar_chart", title="Bars", queries=[wq])
        result = translate_widget(w, plan_widget(w), OTEL_PROFILE)
        self.assertNotIn(result.status, ("not_feasible", "blocked"))
        self.assertEqual(result.kibana_type, "table")

    def test_query_table_plans_esql_table(self):
        mq = parse_metric_query("sum:consul.catalog.services_critical{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="query_table", title="Services", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")
        self.assertEqual(plan.kibana_type, "table")

    def test_grouped_query_value_stays_lens(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="query_value", title="CPU Avg", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "lens")
        self.assertEqual(plan.kibana_type, "metric")

    def test_log_timeseries_plans_esql(self):
        lq = parse_log_query("service:web")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="service:web", log_query=lq, query_type="log")
        w = self._make_widget(id="1", widget_type="timeseries", title="Logs", queries=[wq])
        plan = plan_widget(w)
        self.assertIn(plan.backend, ("esql", "esql_with_kql"))
        self.assertEqual(plan.kibana_type, "xy")

    def test_legacy_logs_rollup_by_query_is_normalized_as_log_timeseries(self):
        raw = {
            "title": "Dash",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "title": "Errors by service",
                        "requests": [
                            {"q": 'logs("status:error").index("*").rollup("count").by("service")'}
                        ],
                    }
                }
            ],
        }

        dashboard = normalize_dashboard(raw)
        widget = dashboard.widgets[0]

        self.assertEqual(widget.queries[0].query_type, "log")
        self.assertIsNotNone(widget.queries[0].log_query)
        plan = plan_widget(widget)
        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn("FROM logs-*", result.esql_query)
        self.assertIn("COUNT(*) BY time_bucket", result.esql_query)
        self.assertIn("service.name", result.esql_query)

    def test_legacy_log_attribute_group_by_maps_to_otel_field(self):
        raw = {
            "title": "Dash",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "title": "Errors by path",
                        "requests": [
                            {"q": 'logs("status:error").index("*").rollup("count").by("@http.url_details.path")'}
                        ],
                    }
                }
            ],
        }

        widget = normalize_dashboard(raw).widgets[0]
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)

        self.assertEqual(result.status, "ok")
        self.assertIn("http.url", result.esql_query)
        self.assertNotIn("`@http`.url_details.path", result.esql_query)

    def test_unsupported_data_source_markdown(self):
        wq = WidgetQuery(name="q1", data_source="apm", raw_query="...", query_type="apm")
        w = self._make_widget(id="1", widget_type="timeseries", title="APM", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "markdown")

    def test_geomap_not_yet_supported(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="geomap", title="Map", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "markdown")

    def test_no_queries_markdown(self):
        w = self._make_widget(id="1", widget_type="timeseries", title="Empty")
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "markdown")

    def test_mixed_metric_and_log_queries_require_manual(self):
        mq = parse_metric_query("avg:system.cpu.user{*}")
        lq = parse_log_query("service:web")
        w = self._make_widget(
            id="1",
            widget_type="timeseries",
            title="Mixed",
            queries=[
                WidgetQuery(name="q1", data_source="metrics", raw_query="avg:system.cpu.user{*}", metric_query=mq, query_type="metric"),
                WidgetQuery(name="q2", data_source="logs", raw_query="service:web", log_query=lq, query_type="log"),
            ],
        )
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "markdown")


# =========================================================================
# Translation Tests
# =========================================================================

class TestTranslation(unittest.TestCase):

    def _translate_metric_widget(self, query_str, widget_type="timeseries", force_esql=False, **kwargs):
        mq = parse_metric_query(query_str)
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query=query_str, metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type=widget_type, title="Test", queries=[wq], **kwargs)
        plan = plan_widget(w)
        if force_esql and plan.backend == "lens":
            plan.backend = "esql"
        return translate_widget(w, plan, OTEL_PROFILE)

    def test_timeseries_produces_esql(self):
        result = self._translate_metric_widget("avg:system.cpu.user{*} by {host}", force_esql=True)
        self.assertEqual(result.status, "ok")
        self.assertIn("FROM", result.esql_query)
        self.assertIn("STATS", result.esql_query)
        self.assertIn("AVG", result.esql_query)
        self.assertIn("BUCKET", result.esql_query)

    def test_query_value_produces_scalar_esql(self):
        result = self._translate_metric_widget("avg:system.cpu.user{*}", widget_type="query_value", force_esql=True)
        self.assertEqual(result.status, "ok")
        self.assertIn("STATS", result.esql_query)
        self.assertIn("BUCKET", result.esql_query)
        self.assertIn("LAST(_bucket_value, time_bucket)", result.esql_query)

    def test_simple_query_value_uses_native_esql_without_forcing(self):
        query = "avg:system.cpu.user{*}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        widget = NormalizedWidget(id="1", widget_type="query_value", title="CPU", queries=[wq])
        plan = plan_widget(widget)
        self.assertEqual(plan.backend, "esql")
        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn("FROM metrics-*", result.esql_query)
        self.assertIn("| STATS", result.esql_query)

    def test_simple_toplist_uses_native_esql_without_forcing(self):
        query = "avg:system.cpu.user{*} by {host}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        widget = NormalizedWidget(id="1", widget_type="toplist", title="Top CPU", queries=[wq])
        plan = plan_widget(widget)
        self.assertEqual(plan.backend, "esql")
        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn("SORT value", result.esql_query)
        self.assertIn("LIMIT", result.esql_query)

    def test_grouped_change_widget_translates_to_change_table(self):
        raw_query = "sum:nginx.net.request_per_s{*} by {service}"
        mq = parse_metric_query(raw_query)
        widget = NormalizedWidget(
            id="1",
            widget_type="change",
            title="Change in overall requests per second",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query=raw_query,
                    metric_query=mq,
                    query_type="metric",
                )
            ],
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
                        "q": raw_query,
                    }
                ],
            },
        )

        plan = plan_widget(widget)
        self.assertEqual(plan.kibana_type, "table")

        result = translate_widget(widget, plan, OTEL_PROFILE)

        self.assertEqual(result.status, "warning")
        self.assertIn("current_value =", result.esql_query)
        self.assertIn("previous_value =", result.esql_query)
        self.assertIn("| EVAL value = current_value - previous_value", result.esql_query)
        self.assertIn("service.name", result.esql_query)

    def test_toplist_produces_sorted_esql(self):
        result = self._translate_metric_widget("avg:system.cpu.user{*} by {host}", widget_type="toplist", force_esql=True)
        self.assertIn("ORDER BY", result.esql_query.upper().replace("SORT", "ORDER BY") if "SORT" in result.esql_query.upper() else result.esql_query.upper())

    def test_top_wrapper_uses_top_limit_reducer_and_sort_order(self):
        raw = "top(avg:system.cpu.user{*} by {host}, 50, 'last', 'asc')"
        mq, outer_fns = parse_legacy_query(raw)
        self.assertIsNotNone(mq)
        mq.functions.extend(outer_fns)
        widget = NormalizedWidget(
            id="1",
            widget_type="toplist",
            title="Top CPU",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query=raw,
                    metric_query=mq,
                    query_type="metric",
                )
            ],
        )
        plan = plan_widget(widget)
        plan.backend = "esql"
        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn("LAST(_bucket_value, time_bucket)", result.esql_query)
        self.assertIn("SORT value ASC", result.esql_query)
        self.assertIn("LIMIT 50", result.esql_query)

    def test_scope_filter_translated(self):
        result = self._translate_metric_widget("avg:system.cpu.user{host:web01}", force_esql=True)
        self.assertIn("host.name", result.esql_query)
        self.assertIn("web01", result.esql_query)

    def test_negated_filter_translated(self):
        result = self._translate_metric_widget("avg:system.cpu.user{!env:staging}", force_esql=True)
        self.assertIn("!=", result.esql_query)

    def test_group_by_mapped(self):
        result = self._translate_metric_widget("avg:system.cpu.user{*} by {host}", force_esql=True)
        self.assertIn("host.name", result.esql_query)

    def test_field_map_applied(self):
        result = self._translate_metric_widget("avg:system.cpu.user{*}", force_esql=True)
        self.assertIn("system_cpu_user", result.esql_query)

    def test_metric_trace_includes_planner_and_translator_rule_ids(self):
        query = "sum:http.requests{*}.as_rate()"
        mq = parse_metric_query(query)
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Req Rate",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query=query,
                    metric_query=mq,
                    query_type="metric",
                )
            ],
        )

        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)

        trace_rules = [entry["rule"] for entry in result.trace]
        self.assertIn("datadog.plan.metric_timeseries", trace_rules)
        self.assertIn("datadog.translate.metric_single_query", trace_rules)

    def test_log_trace_includes_kql_bridge_rule_id(self):
        query = "connection timeout"
        lq = parse_log_query(query)
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Logs",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="logs",
                    raw_query=query,
                    log_query=lq,
                    query_type="log",
                )
            ],
        )

        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)

        trace_rules = [entry["rule"] for entry in result.trace]
        self.assertIn("datadog.plan.log_timeseries", trace_rules)
        self.assertIn("datadog.translate.log_kql_bridge", trace_rules)

    def test_metric_translation_blocks_known_non_numeric_target_field(self):
        profile = FieldMapProfile(
            name="typed",
            metric_index="metrics-*",
            logs_index="logs-*",
            field_caps={
                "system_cpu_user": FieldCapability(
                    name="system_cpu_user",
                    type="keyword",
                    aggregatable=True,
                    searchable=True,
                )
            },
        )
        mq = parse_metric_query("avg:system.cpu.user{*}")
        wq = WidgetQuery(
            name="q1",
            data_source="metrics",
            raw_query="avg:system.cpu.user{*}",
            metric_query=mq,
            query_type="metric",
        )
        widget = NormalizedWidget(id="1", widget_type="timeseries", title="CPU", queries=[wq])
        plan = plan_widget(widget)
        plan.backend = "esql"
        result = translate_widget(widget, plan, profile)
        self.assertEqual(result.status, "not_feasible")
        self.assertTrue(any("not safe for metric aggregation" in warning for warning in result.warnings))

    def test_time_filter_present(self):
        result = self._translate_metric_widget("avg:system.cpu.user{*}", force_esql=True)
        self.assertIn("?_tstart", result.esql_query)
        self.assertIn("?_tend", result.esql_query)

    def test_as_rate_generates_rate_expr(self):
        result = self._translate_metric_widget("sum:http.requests{*}.as_rate()")
        self.assertIn("MAX(", result.esql_query)
        self.assertIn("MIN(", result.esql_query)

    def test_metric_template_variable_becomes_broad_match(self):
        result = self._translate_metric_widget("avg:system.cpu.user{host:$host}", force_esql=True)
        self.assertNotIn("$host", result.esql_query)
        self.assertTrue(any("template variable" in w.lower() for w in result.warnings))

    def test_boolean_scope_or_is_translated_safely(self):
        result = self._translate_metric_widget(
            "sum:istio.mesh.request.count.total{(response_code:2* OR response_code:3*) AND $cluster_name}.as_count()"
        )
        self.assertIn("response_code LIKE \"2%\"", result.esql_query)
        self.assertIn("OR", result.esql_query)
        self.assertNotIn("`(response_code`", result.esql_query)
        self.assertTrue(any("template variable" in w.lower() for w in result.warnings), result.warnings)

    def test_boolean_scope_or_across_keys_is_preserved(self):
        result = self._translate_metric_widget(
            "avg:system.cpu.user{service:web OR host:api}.as_rate()",
            widget_type="query_value",
        )
        self.assertIn("(service.name == \"web\" OR host.name == \"api\")", result.esql_query)

    def test_scope_in_list_is_translated_to_esql_in(self):
        # `key IN (a, b, c)` filters to any of the listed values (OR logic).
        # Previously the comma splitter broke the list and the filter was
        # silently dropped, returning all series.
        result = self._translate_metric_widget(
            "avg:system.cpu.user{service IN (web, api, worker)} by {host}",
            force_esql=True,
        )
        self.assertIn(
            'service.name IN ("web", "api", "worker")',
            result.esql_query,
            result.esql_query,
        )

    def test_scope_not_in_list_is_translated_to_esql_not_in(self):
        # `key NOT IN (a, b)` excludes the listed values.
        result = self._translate_metric_widget(
            "avg:system.cpu.user{env:prod AND location NOT IN (atlanta, seattle)}",
            widget_type="query_value",
        )
        self.assertIn('deployment.environment == "prod"', result.esql_query)
        self.assertIn('location NOT IN ("atlanta", "seattle")', result.esql_query)

    def test_single_query_formula_is_applied(self):
        query = "avg:system.cpu.user{*}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
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
        self.assertEqual(result.status, "ok")
        self.assertIn("| EVAL value = (query1 * 100)", result.esql_query)

    def test_legacy_q_arithmetic_metric_expression_translates_to_formula_panel(self):
        raw = {
            "title": "Flink",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "title": "Input buffer usage (%)",
                        "requests": [
                            {
                                "q": (
                                    "avg:flink.task.Shuffle.Netty.Input.Buffers.inPoolUsage{*} "
                                    "by {task_id,subtask_index}*100"
                                )
                            }
                        ],
                    }
                }
            ],
        }
        widget = normalize_dashboard(raw).widgets[0]

        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)

        self.assertEqual(result.status, "ok")
        self.assertIn("query0 = AVG(flink_task_Shuffle_Netty_Input_Buffers_inPoolUsage)", result.esql_query)
        self.assertIn("BY time_bucket = BUCKET(@timestamp", result.esql_query)
        self.assertIn("task_id, subtask_index", result.esql_query)
        self.assertIn("| EVAL query0_100 = (query0 * 100)", result.esql_query)
        self.assertIn("| KEEP time_bucket, task_id, subtask_index, query0_100", result.esql_query)

    def test_count_nonzero_formula_reduces_grouped_query(self):
        query = "avg:kubernetes.pods.running{*} by {kube_cluster_name}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="count_nonzero(query1)")
        wf.expression = parse_formula("count_nonzero(query1)")
        widget = NormalizedWidget(
            id="1",
            widget_type="query_value",
            title="Clusters",
            queries=[wq],
            formulas=[wf],
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn("| WHERE query1 > 0", result.esql_query)
        self.assertIn("| STATS value = COUNT(*)", result.esql_query)

    def test_count_formula_nested_in_default_zero_reduces_timeseries_groups(self):
        query = "avg:cisco_sdwan.control_connection.status{*} by {hostname,peer_system_ip}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        up = WidgetFormula(raw="default_zero(count_nonzero(query1))", alias="Control Conns UP")
        up.expression = parse_formula("default_zero(count_nonzero(query1))")
        down = WidgetFormula(raw="default_zero(count_not_null(query1) - count_nonzero(query1))", alias="Control Conns DOWN")
        down.expression = parse_formula("default_zero(count_not_null(query1) - count_nonzero(query1))")
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Control Connections UP / DOWN over time",
            queries=[wq],
            formulas=[up, down],
        )

        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)

        self.assertEqual(result.status, "ok")
        self.assertIn("BY time_bucket = BUCKET(@timestamp", result.esql_query)
        self.assertIn("hostname, peer_system_ip", result.esql_query)
        self.assertIn("_count_nonzero_query1 = COUNT(*) WHERE query1 > 0", result.esql_query)
        self.assertIn("_count_not_null_query1 = COUNT(*) WHERE query1 IS NOT NULL", result.esql_query)
        self.assertIn("control_conns_up = COALESCE(_count_nonzero_query1, 0)", result.esql_query)
        self.assertIn(
            "control_conns_down = COALESCE((_count_not_null_query1 - _count_nonzero_query1), 0)",
            result.esql_query,
        )
        self.assertIn("| KEEP time_bucket, control_conns_up, control_conns_down", result.esql_query)

    def test_count_not_null_cutoff_max_formula_counts_values_at_or_below_threshold(self):
        query = "avg:cisco_sdwan.device.reachable{type:vbond} by {system_ip,device_namespace}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="count_not_null(cutoff_max(query1, 0.5))")
        wf.expression = parse_formula("count_not_null(cutoff_max(query1, 0.5))")
        widget = NormalizedWidget(
            id="1",
            widget_type="query_value",
            title="Validators down",
            queries=[wq],
            formulas=[wf],
        )

        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)

        self.assertEqual(result.status, "ok")
        self.assertIn("BY system_ip, device_namespace", result.esql_query)
        self.assertIn(
            "_count_not_null_query1_cutoff_max_0_5 = COUNT(*) WHERE query1 IS NOT NULL AND query1 <= 0.5",
            result.esql_query,
        )
        self.assertIn("| EVAL value = _count_not_null_query1_cutoff_max_0_5", result.esql_query)
        self.assertIn("| KEEP value", result.esql_query)

    def test_rate_formula_uses_ts_rate_when_metric_is_counter_typed(self):
        # When the live field-caps loader knows the target field is a
        # TSDS counter, the translator switches from the FROM+FIRST/LAST
        # fallback to native ES|QL TS|QL RATE() — the same pattern Grafana
        # uses for PromQL rate(). Construct a counter capability inline.
        from copy import deepcopy

        from observability_migration.core.verification.field_capabilities import (
            FieldCapability,
        )

        profile = deepcopy(OTEL_PROFILE)
        # Inject a counter capability for the mapped ES metric name.
        profile.field_caps["parity_counter"] = FieldCapability(
            name="parity_counter",
            type="counter_long",
            time_series_metric_kind="counter",
        )

        query = "sum:parity.counter{host:h1}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="rate(query1)")
        wf.expression = parse_formula("rate(query1)")
        widget = NormalizedWidget(
            id="1", widget_type="timeseries", title="Counter rate",
            queries=[wq], formulas=[wf],
        )
        result = translate_widget(widget, plan_widget(widget), profile)
        self.assertIn("TS metrics-*", result.esql_query)
        self.assertIn("RATE(parity_counter, 5 minute)", result.esql_query)
        self.assertIn("TBUCKET(5 minute)", result.esql_query)
        # FIRST/LAST fallback should NOT appear when we go the TS path.
        self.assertNotIn("FIRST(parity_counter", result.esql_query)

    def test_diff_formula_uses_ts_increase_when_metric_is_counter_typed(self):
        from copy import deepcopy

        from observability_migration.core.verification.field_capabilities import (
            FieldCapability,
        )

        profile = deepcopy(OTEL_PROFILE)
        profile.field_caps["parity_counter"] = FieldCapability(
            name="parity_counter",
            type="counter_double",
            time_series_metric_kind="counter",
        )

        query = "sum:parity.counter{host:h1}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="diff(query1)")
        wf.expression = parse_formula("diff(query1)")
        widget = NormalizedWidget(
            id="1", widget_type="timeseries", title="Counter delta",
            queries=[wq], formulas=[wf],
        )
        result = translate_widget(widget, plan_widget(widget), profile)
        self.assertIn("TS metrics-*", result.esql_query)
        self.assertIn("INCREASE(parity_counter, 5 minute)", result.esql_query)

    def test_rate_formula_falls_back_to_first_last_for_gauges(self):
        # No counter capability injected — current FIRST/LAST behaviour
        # stays as the fallback for plain gauge metrics.
        query = "sum:parity.counter{host:h1}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="rate(query1)")
        wf.expression = parse_formula("rate(query1)")
        widget = NormalizedWidget(
            id="1", widget_type="timeseries", title="Gauge rate",
            queries=[wq], formulas=[wf],
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertNotIn("TS metrics-*", result.esql_query)
        self.assertIn("FROM metrics-*", result.esql_query)
        self.assertIn("FIRST(parity_counter, @timestamp)", result.esql_query)

    def test_rate_formula_uses_first_last_for_proper_derivative(self):
        # rate(query_ref) where query_ref is a direct reference: STATS
        # emits FIRST/LAST so EVAL can compute (last - first)/span — true
        # DD derivative semantics, not the value/span approximation.
        query = "sum:mysql.performance.table_locks_waited{*}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="rate(query1)")
        wf.expression = parse_formula("rate(query1)")
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Locking rate",
            queries=[wq],
            formulas=[wf],
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("FIRST(mysql_performance_table_locks_waited, @timestamp)", result.esql_query)
        self.assertIn("LAST(mysql_performance_table_locks_waited, @timestamp)", result.esql_query)
        self.assertIn("(query1_last - query1_first) / bucket_span_seconds", result.esql_query)
        self.assertIn("BUCKET(@timestamp", result.esql_query)
        self.assertTrue(any("rate()" in w for w in result.warnings), result.warnings)

    def test_diff_formula_uses_first_last_for_proper_delta(self):
        query = "sum:redis.net.rejected{*}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="diff(query1)")
        wf.expression = parse_formula("diff(query1)")
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Rejected connections delta",
            queries=[wq],
            formulas=[wf],
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("FIRST(redis_net_rejected, @timestamp)", result.esql_query)
        self.assertIn("LAST(redis_net_rejected, @timestamp)", result.esql_query)
        self.assertIn("(query1_last - query1_first)", result.esql_query)
        self.assertIn("BUCKET(@timestamp", result.esql_query)
        self.assertFalse(any("diff()" in w for w in result.warnings))

    def test_top_formula_translates_to_unwrapped_query_with_warning(self):
        query = "avg:apache.performance.cpu_load{*} by {host}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="top(query1, 10, 'mean', 'desc')")
        wf.expression = parse_formula("top(query1, 10, 'mean', 'desc')")
        widget = NormalizedWidget(
            id="1",
            widget_type="toplist",
            title="Top CPU hosts",
            queries=[wq],
            formulas=[wf],
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        # toplist widgets emit status=warning by default; ensure no
        # `not_feasible` and that the panel sort/limit applies.
        self.assertNotEqual(result.status, "not_feasible")
        self.assertIn("apache_performance_cpu_load", result.esql_query)
        self.assertTrue(any("top()" in w for w in result.warnings))

    def test_top_formula_on_timeseries_produces_ranked_flat_table(self):
        # Real-world case: top(query1 / 1000, 10, 'mean', 'desc') on a
        # timeseries widget (Redis Slowlog duration, Kafka lag, RabbitMQ queue time).
        # ES|QL cannot filter to N series; we collapse to a per-group ranked table.
        query = "sum:redis.slowlog.micros.95percentile{*} by {name,command}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="top(query1 / 1000, 10, 'mean', 'desc')")
        wf.expression = parse_formula("top(query1 / 1000, 10, 'mean', 'desc')")
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Slowlog duration",
            queries=[wq],
            formulas=[wf],
        )
        plan = plan_widget(widget)
        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertNotEqual(result.status, "not_feasible")
        # The plan kibana_type must be overridden to table (not timeseries xy).
        self.assertEqual(plan.kibana_type, "table", "top() on timeseries must produce a flat table")
        # The inner expression must be preserved.
        self.assertIn("(query1 / 1000)", result.esql_query)
        # A ranking STATS must collapse the time dimension.
        self.assertIn("STATS _rank = AVG(", result.esql_query)
        # Must be limited to top N.
        self.assertIn("| LIMIT 10", result.esql_query)
        self.assertIn("| SORT _rank DESC", result.esql_query)
        # Warning must mention approximation but NOT the old "panel-level sort/limit" phrasing.
        self.assertTrue(any("top(10)" in w for w in result.warnings))
        self.assertFalse(
            any("panel-level sort/limit" in w for w in result.warnings),
            "old inaccurate warning must be replaced by the ranked-table warning",
        )

    def test_top_formula_on_toplist_uses_panel_sort_limit_unchanged(self):
        # toplist widgets already have sort/limit; top() in that context is a
        # pass-through with just the old approximation warning — no type change.
        query = "avg:apache.performance.cpu_load{*} by {host}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="top(query1, 10, 'mean', 'desc')")
        wf.expression = parse_formula("top(query1, 10, 'mean', 'desc')")
        widget = NormalizedWidget(
            id="1",
            widget_type="toplist",
            title="Top CPU hosts",
            queries=[wq],
            formulas=[wf],
        )
        plan = plan_widget(widget)
        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertNotEqual(result.status, "not_feasible")
        self.assertEqual(plan.kibana_type, "table", "toplist must stay as table")
        # toplist path uses panel sort/limit — no extra ranking STATS
        self.assertNotIn("_rank", result.esql_query)
        self.assertTrue(any("top()" in w for w in result.warnings))

    def test_legacy_top_on_timeseries_produces_ranked_flat_table(self):
        # Regression: legacy format top(avg:metric{$var,$var2} by {host}, N, agg, order)
        # was previously legacy_unparsed due to _split_args not tracking brace depth.
        # After the fix it must translate as a ranked flat table, same as the formula path.
        raw = "top(avg:apache.performance.cpu_load{$host,$scope} by {host}, 10, 'mean', 'desc')"
        mq, fns = parse_legacy_query(raw)
        mq.functions.extend(fns)
        wq = WidgetQuery(name="query0", data_source="metrics", raw_query=raw, metric_query=mq, query_type="metric")
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Apache process CPU usage (top 10 hosts)",
            queries=[wq],
            formulas=[],
        )
        plan = plan_widget(widget)
        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertNotEqual(result.status, "not_feasible")
        self.assertEqual(plan.kibana_type, "table", "legacy top() on timeseries must produce a flat table")
        self.assertIn("STATS _rank = AVG(", result.esql_query)
        self.assertIn("| LIMIT 10", result.esql_query)
        self.assertIn("| SORT _rank DESC", result.esql_query)
        self.assertTrue(any("top(10)" in w for w in result.warnings))

    def test_ratio_formula_uses_both_queries(self):
        q1 = "avg:etcd.disk.wal.fsync.duration.seconds.sum{env:prod} by {host}"
        q2 = "avg:etcd.disk.wal.fsync.duration.seconds.count{env:prod} by {host}"
        mq1 = parse_metric_query(q1)
        mq2 = parse_metric_query(q2)
        wf = WidgetFormula(raw="query1 / query2")
        wf.expression = parse_formula("query1 / query2")
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Latency",
            queries=[
                WidgetQuery(name="query1", data_source="metrics", raw_query=q1, metric_query=mq1, query_type="metric"),
                WidgetQuery(name="query2", data_source="metrics", raw_query=q2, metric_query=mq2, query_type="metric"),
            ],
            formulas=[wf],
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn("query1 = AVG(etcd_disk_wal_fsync_duration_seconds_sum)", result.esql_query)
        self.assertIn("query2 = AVG(etcd_disk_wal_fsync_duration_seconds_count)", result.esql_query)
        self.assertIn("| EVAL query1_query2 = (query1 / query2)", result.esql_query)

    def test_query_table_keeps_grouped_rows(self):
        result = self._translate_metric_widget(
            "sum:consul.catalog.services_critical{*} by {host}",
            widget_type="query_table",
            force_esql=True,
        )
        self.assertEqual(result.status, "ok")
        self.assertIn("BY host.name", result.esql_query)

    def test_query_value_uses_request_aggregator_over_time(self):
        query = "sum:mongodb.chunks.total{*}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(
            name="query1",
            data_source="metrics",
            raw_query=query,
            metric_query=mq,
            aggregator="last",
            query_type="metric",
        )
        widget = NormalizedWidget(
            id="1",
            widget_type="query_value",
            title="Chunks",
            queries=[wq],
        )
        plan = plan_widget(widget)
        plan.backend = "esql"
        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn("| STATS _bucket_value = SUM(mongodb_chunks_total) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)", result.esql_query)
        self.assertIn("| STATS value = LAST(_bucket_value, time_bucket)", result.esql_query)

    def test_query_table_last_aggregator_reduces_after_grouped_buckets(self):
        query = "max:mongodb.replset.optime_lag{*} by {replset_name}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(
            name="query1",
            data_source="metrics",
            raw_query=query,
            metric_query=mq,
            aggregator="last",
            query_type="metric",
        )
        widget = NormalizedWidget(
            id="1",
            widget_type="query_table",
            title="Lag",
            queries=[wq],
        )
        plan = plan_widget(widget)
        plan.backend = "esql"
        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn("BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), replset_name", result.esql_query)
        self.assertIn("| STATS value = LAST(_bucket_value, time_bucket) BY replset_name", result.esql_query)

    def test_multi_query_formula_with_different_filters_uses_per_agg_where(self):
        # Two queries with identical metric and grouping but different
        # tag filters — the kafka data_streams.latency pattern. The
        # translator now emits per-aggregation WHERE clauses rather than
        # blocking the widget.
        q1 = "count:data_streams.latency{type:kafka AND direction:out} by {topic}.as_rate()"
        q2 = "count:data_streams.latency{type:kafka AND direction:in} by {topic}.as_rate()"
        mq1 = parse_metric_query(q1)
        mq2 = parse_metric_query(q2)
        wf = WidgetFormula(raw="query1 / query2")
        wf.expression = parse_formula("query1 / query2")
        widget = NormalizedWidget(
            id="1",
            widget_type="query_table",
            title="Topic Health",
            queries=[
                WidgetQuery(name="query1", data_source="metrics", raw_query=q1, metric_query=mq1, query_type="metric"),
                WidgetQuery(name="query2", data_source="metrics", raw_query=q2, metric_query=mq2, query_type="metric"),
            ],
            formulas=[wf],
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertNotEqual(result.status, "not_feasible")
        # Each per-aggregation WHERE preserves its own direction filter.
        self.assertIn('WHERE type == "kafka" AND direction == "out"', result.esql_query)
        self.assertIn('WHERE type == "kafka" AND direction == "in"', result.esql_query)
        # Outer WHERE includes the OR of both spec filters.
        self.assertIn(' OR ', result.esql_query)
        # Formula is applied as EVAL.
        self.assertIn("query1_query2 = (query1 / query2)", result.esql_query)

    def test_log_widget_translation(self):
        lq = parse_log_query("service:web AND status:error")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="service:web AND status:error", log_query=lq, query_type="log")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="Logs", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn("FROM", result.esql_query)
        self.assertIn("COUNT", result.esql_query)

    def test_multi_log_timeseries_translates_each_query_as_separate_series(self):
        raw = {
            "title": "Logs",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "title": "Authorised and Unauthorised",
                        "requests": [
                            {
                                "queries": [
                                    {
                                        "data_source": "logs",
                                        "name": "query1",
                                        "compute": {"aggregation": "cardinality", "metric": "@pspReference"},
                                        "search": {"query": "source:adyen @evt.name:AUTHORISATION @success:true"},
                                    },
                                    {
                                        "data_source": "logs",
                                        "name": "query2",
                                        "compute": {"aggregation": "cardinality", "metric": "@pspReference"},
                                        "search": {"query": "source:adyen @evt.name:AUTHORISATION @success:false"},
                                    },
                                ],
                                "formulas": [
                                    {"formula": "query1", "alias": "Authorised"},
                                    {"formula": "query2", "alias": "Unauthorised"},
                                ],
                            }
                        ],
                    }
                }
            ],
        }
        widget = normalize_dashboard(raw).widgets[0]

        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.warnings, [])
        self.assertIn("query1 = COUNT_DISTINCT(`@pspReference`) WHERE", result.esql_query)
        self.assertIn("query2 = COUNT_DISTINCT(`@pspReference`) WHERE", result.esql_query)
        self.assertIn("| EVAL authorised = query1, unauthorised = query2", result.esql_query)
        self.assertIn("| KEEP time_bucket, authorised, unauthorised", result.esql_query)

    def test_multi_log_timeseries_formula_applies_query_side_arithmetic(self):
        raw = {
            "title": "Logs",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "title": "Failure rate",
                        "requests": [
                            {
                                "queries": [
                                    {
                                        "data_source": "logs",
                                        "name": "query1",
                                        "compute": {"aggregation": "count"},
                                        "search": {"query": "service:vpn status:error"},
                                    },
                                    {
                                        "data_source": "logs",
                                        "name": "query2",
                                        "compute": {"aggregation": "count"},
                                        "search": {"query": "service:vpn"},
                                    },
                                ],
                                "formulas": [
                                    {"formula": "default_zero((query1 / query2) * 100)", "alias": "Failure rate"}
                                ],
                            }
                        ],
                    }
                }
            ],
        }
        widget = normalize_dashboard(raw).widgets[0]

        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.warnings, [])
        self.assertIn("query1 = COUNT(*) WHERE", result.esql_query)
        self.assertIn("query2 = COUNT(*) WHERE", result.esql_query)
        self.assertIn("failure_rate = COALESCE(((query1 / query2) * 100), 0)", result.esql_query)
        self.assertIn("| KEEP time_bucket, failure_rate", result.esql_query)

    def test_log_stream_translation(self):
        lq = parse_log_query("status:error")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="status:error", log_query=lq, query_type="log")
        w = NormalizedWidget(id="1", widget_type="log_stream", title="Logs", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertIn("SORT", result.esql_query)
        self.assertIn("KEEP", result.esql_query)
        self.assertIn("LIMIT", result.esql_query)

    def test_log_stream_translation_uses_keyword_field_caps_for_status_code_filters(self):
        profile = FieldMapProfile(
            name="typed-logs",
            logs_index="logs-*",
            tag_map={"source": "service.name"},
            log_field_caps={
                "service.name": FieldCapability(
                    name="service.name",
                    type="keyword",
                    searchable=True,
                    aggregatable=True,
                ),
                "http.status_code": FieldCapability(
                    name="http.status_code",
                    type="keyword",
                    searchable=True,
                    aggregatable=True,
                ),
            },
        )
        raw_query = "source:nginx @http.status_code:(404 OR 500)"
        lq = parse_log_query(raw_query)
        wq = WidgetQuery(name="q1", data_source="logs", raw_query=raw_query, log_query=lq, query_type="log")
        w = NormalizedWidget(id="1", widget_type="log_stream", title="Logs", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, profile)
        self.assertIn('service.name == "nginx"', result.esql_query)
        self.assertIn('http.status_code == "404"', result.esql_query)
        self.assertIn('http.status_code == "500"', result.esql_query)

    def test_log_template_variable_is_not_kept_literal(self):
        lq = parse_log_query("service:$svc")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="service:$svc", log_query=lq, query_type="log")
        w = NormalizedWidget(id="1", widget_type="log_stream", title="Logs", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertNotIn("$svc", result.esql_query)
        self.assertNotIn("service.name ==", result.esql_query)

    def test_wildcard_filter_translated(self):
        result = self._translate_metric_widget("avg:system.cpu.user{host:web*}", force_esql=True)
        self.assertIn("LIKE", result.esql_query)
        self.assertIn("web%", result.esql_query)

    def test_hyphenated_group_by_is_quoted(self):
        result = self._translate_metric_widget(
            "sum:kafka.consumer.messages_in{*} by {client-id}",
            widget_type="timeseries",
            force_esql=True,
        )
        self.assertIn("`client-id`", result.esql_query)

    def test_passthrough_profile(self):
        mq = parse_metric_query("avg:system.cpu.user{*}.as_rate()")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="avg:system.cpu.user{*}.as_rate()", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="query_value", title="Test", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, PASSTHROUGH_PROFILE)
        self.assertIn("system_cpu_user", result.esql_query)


# =========================================================================
# YAML Generation Tests
# =========================================================================

class TestYAMLGeneration(unittest.TestCase):

    def _make_metric_widget(self, wid, title, widget_type="timeseries", layout=None):
        query = "avg:system.cpu.user{*}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name=f"q_{wid}", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        return NormalizedWidget(
            id=str(wid),
            widget_type=widget_type,
            title=title,
            queries=[wq],
            layout=layout or {"x": 0, "y": 0, "width": 4, "height": 2},
        )

    def _render_dashboard(self, widgets):
        dash = NormalizedDashboard(id="1", title="Dash", widgets=widgets)
        results = []
        for widget in widgets:
            if widget.widget_type in ("group", "powerpack"):
                continue
            plan = plan_widget(widget)
            if plan.backend == "lens":
                plan.backend = "esql"
            results.append(translate_widget(widget, plan, OTEL_PROFILE))
        yaml_str = generate_dashboard_yaml(dash, results)
        return yaml.safe_load(yaml_str)["dashboards"][0]

    def test_generate_from_sample(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        raw = json.loads(path.read_text())
        nd = normalize_dashboard(raw)

        results = []
        for widget in nd.widgets:
            plan = plan_widget(widget)
            result = translate_widget(widget, plan, OTEL_PROFILE)
            results.append(result)

        yaml_str = generate_dashboard_yaml(nd, results)
        self.assertIn("title:", yaml_str)
        self.assertIn("panels:", yaml_str)
        self.assertIn("System Overview", yaml_str)

    def test_esql_panel_has_query(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        raw = json.loads(path.read_text())
        nd = normalize_dashboard(raw)

        results = []
        for widget in nd.widgets:
            plan = plan_widget(widget)
            result = translate_widget(widget, plan, OTEL_PROFILE)
            results.append(result)

        esql_results = [r for r in results if r.esql_query]
        self.assertTrue(len(esql_results) > 0)

    def test_generated_yaml_is_compile_shaped_and_matches_result_panel(self):
        widget = self._make_metric_widget("1", "CPU trend", "timeseries", {"x": 0, "y": 0, "width": 4, "height": 2})
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        plan = plan_widget(widget)
        if plan.backend == "lens":
            plan.backend = "esql"
        result = translate_widget(widget, plan, OTEL_PROFILE)

        payload = yaml.safe_load(generate_dashboard_yaml(dash, [result]))
        rendered_dash = payload["dashboards"][0]
        rendered_panel = rendered_dash["panels"][0]

        self.assertEqual(rendered_dash["minimum_kibana_version"], "9.1.0")
        self.assertEqual(rendered_panel["title"], result.yaml_panel["title"])
        self.assertEqual(rendered_panel["esql"]["query"], result.yaml_panel["esql"]["query"])
        self.assertEqual(rendered_panel["size"], result.yaml_panel["size"])
        self.assertEqual(rendered_panel["position"], result.yaml_panel["position"])
        self.assertNotIn("_dd_y", rendered_panel)
        self.assertNotIn("_dd_x", rendered_panel)
        self.assertNotIn("_dd_w", rendered_panel)
        self.assertNotIn("_dd_h", rendered_panel)

    def test_esql_panel_fields_ignore_by_inside_quoted_strings(self):
        widget = self._make_metric_widget("1", "CPU trend", "timeseries", {"x": 0, "y": 0, "width": 4, "height": 2})
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        result = TranslationResult(
            widget_id=widget.id,
            title=widget.title,
            dd_widget_type=widget.widget_type,
            kibana_type="xy",
            status="ok",
            backend="esql",
            esql_query=(
                "FROM metrics-* "
                "| STATS value = AVG(system_cpu_user) "
                "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), host.name "
                '| EVAL note = "sent by host" '
                "| KEEP time_bucket, host.name, value"
            ),
        )

        panel = yaml.safe_load(generate_dashboard_yaml(dash, [result]))["dashboards"][0]["panels"][0]

        self.assertEqual(panel["esql"]["dimension"]["field"], "time_bucket")
        self.assertEqual(panel["esql"]["breakdown"]["field"], "host.name")
        self.assertEqual([metric["field"] for metric in panel["esql"]["metrics"]], ["value"])

    def test_lens_percentile_aggregation_uses_schema_percentile_field(self):
        query = "p95:trace.http.request.duration{*} by {resource_name}"
        mq = parse_metric_query(query)
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Latency",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query=query,
                    metric_query=mq,
                    query_type="metric",
                )
            ],
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )
        plan = plan_widget(widget)
        result = translate_widget(widget, plan, OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])

        panel = yaml.safe_load(generate_dashboard_yaml(dash, [result]))["dashboards"][0]["panels"][0]

        metric = panel["lens"]["metrics"][0]
        self.assertEqual(metric["aggregation"], "percentile")
        self.assertEqual(metric["percentile"], 95)
        self.assertEqual(metric["field"], "trace_http_request_duration")

    def test_markdown_panel_for_note(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        raw = json.loads(path.read_text())
        nd = normalize_dashboard(raw)

        note_widget = next(w for w in nd.widgets if w.widget_type == "note")
        plan = plan_widget(note_widget)
        result = translate_widget(note_widget, plan, OTEL_PROFILE)
        self.assertEqual(result.backend, "markdown")

    def test_blocked_widget_not_in_yaml(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        raw = json.loads(path.read_text())
        nd = normalize_dashboard(raw)

        results = []
        for widget in nd.widgets:
            plan = plan_widget(widget)
            result = translate_widget(widget, plan, OTEL_PROFILE)
            results.append(result)

        yaml_str = generate_dashboard_yaml(nd, results)
        import yaml as yaml_lib
        doc = yaml_lib.safe_load(yaml_str)
        dash = doc["dashboards"][0]
        for p in dash.get("panels", []):
            self.assertNotIn("manage_status", str(p.get("esql", {}).get("type", "")))

    def test_display_enrichment_attaches_primary_format(self):
        query = "avg:system.cpu.user{*}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        widget = NormalizedWidget(
            id="1",
            widget_type="query_value",
            title="CPU",
            queries=[wq],
            custom_unit="percent",
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )
        plan = plan_widget(widget)
        plan.backend = "esql"
        result = translate_widget(widget, plan, OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        self.assertEqual(result.yaml_panel["esql"]["primary"]["format"]["type"], "number")
        self.assertEqual(result.yaml_panel["esql"]["primary"]["format"]["suffix"], "%")

    def test_formula_metric_uses_keep_field_for_primary(self):
        query = "avg:system.cpu.user{*}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="query1 * 100")
        wf.expression = parse_formula("query1 * 100")
        widget = NormalizedWidget(
            id="1",
            widget_type="query_value",
            title="CPU %",
            queries=[wq],
            formulas=[wf],
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        self.assertEqual(result.yaml_panel["esql"]["primary"]["field"], "value")

    def test_xy_panel_hides_axis_titles_and_labels_series(self):
        q1 = "avg:system.cpu.user{*}"
        q2 = "avg:system.cpu.system{*}"
        mq1 = parse_metric_query(q1)
        mq2 = parse_metric_query(q2)
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="CPU",
            queries=[
                WidgetQuery(name="query1", data_source="metrics", raw_query=q1, metric_query=mq1, query_type="metric"),
                WidgetQuery(name="query2", data_source="metrics", raw_query=q2, metric_query=mq2, query_type="metric"),
            ],
            layout={"x": 0, "y": 0, "width": 8, "height": 4},
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        esql = result.yaml_panel["esql"]
        self.assertFalse(esql["appearance"]["x_axis"]["title"])
        self.assertFalse(esql["appearance"]["y_left_axis"]["title"])
        self.assertEqual([metric["label"] for metric in esql["metrics"]], ["User", "System"])

    def _make_timeseries_widget(self, yaxis: dict) -> NormalizedWidget:
        query = "avg:system.cpu.user{*}"
        return NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="CPU",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query=query,
                    metric_query=parse_metric_query(query),
                    query_type="metric",
                )
            ],
            yaxis=yaxis,
            layout={"x": 0, "y": 0, "width": 8, "height": 4},
        )

    def _translate_with_yaml(self, widget: NormalizedWidget):
        plan = plan_widget(widget)
        if plan.backend == "lens":
            plan.backend = "esql"
        result = translate_widget(widget, plan, OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        return result

    def test_y_axis_both_bounds_emits_custom_extent(self):
        result = self._translate_with_yaml(
            self._make_timeseries_widget({"min": 0, "max": 100, "label": "CPU %"})
        )
        self.assertFalse(any("y-axis bounds are not mapped yet" in w for w in result.warnings))
        extent = result.yaml_panel["esql"]["appearance"]["y_left_axis"]["extent"]
        self.assertEqual(extent["mode"], "custom")
        self.assertEqual(extent["min"], 0.0)
        self.assertEqual(extent["max"], 100.0)

    def test_y_axis_max_only_with_include_zero_true_infers_min_zero(self):
        # Regression: max-only + include_zero=true previously emitted {mode:custom, max:N}
        # which kb-dashboard-cli rejects. Fix: infer min=0 (semantically identical).
        result = self._translate_with_yaml(
            self._make_timeseries_widget({"max": "100", "include_zero": True})
        )
        extent = result.yaml_panel["esql"]["appearance"]["y_left_axis"]["extent"]
        self.assertEqual(extent["mode"], "custom")
        self.assertEqual(extent["min"], 0.0)
        self.assertEqual(extent["max"], 100.0)
        self.assertFalse(any("extent omitted" in w for w in result.warnings))

    def test_y_axis_max_only_include_zero_default_true_infers_min_zero(self):
        # include_zero defaults to True in Datadog — omitting it is equivalent to True.
        result = self._translate_with_yaml(
            self._make_timeseries_widget({"max": "1"})
        )
        extent = result.yaml_panel["esql"]["appearance"]["y_left_axis"]["extent"]
        self.assertEqual(extent["min"], 0.0)
        self.assertEqual(extent["max"], 1.0)

    def test_y_axis_max_only_include_zero_false_omits_extent_and_warns(self):
        result = self._translate_with_yaml(
            self._make_timeseries_widget({"max": "100", "include_zero": False})
        )
        y_left = result.yaml_panel["esql"]["appearance"].get("y_left_axis", {})
        self.assertNotIn("extent", y_left)
        self.assertTrue(any("extent omitted" in w for w in result.warnings))

    def test_y_axis_auto_min_with_valid_max_and_include_zero_infers_min_zero(self):
        # "auto" is a Datadog sentinel for auto-scaling — treat as absent, then apply
        # include_zero logic. include_zero=true (default) → infer min=0.
        result = self._translate_with_yaml(
            self._make_timeseries_widget({"min": "auto", "max": "100", "include_zero": True})
        )
        extent = result.yaml_panel["esql"]["appearance"]["y_left_axis"]["extent"]
        self.assertEqual(extent["min"], 0.0)
        self.assertEqual(extent["max"], 100.0)

    def test_y_axis_min_only_omits_extent_and_warns(self):
        # min-only (no max) — Kibana cannot anchor only the lower bound in custom mode.
        result = self._translate_with_yaml(
            self._make_timeseries_widget({"min": "0"})
        )
        y_left = result.yaml_panel["esql"]["appearance"].get("y_left_axis", {})
        self.assertNotIn("extent", y_left)
        self.assertTrue(any("extent omitted" in w for w in result.warnings))

    def test_non_xy_panel_with_yaxis_does_not_emit_y_left_axis(self):
        # Regression: _apply_axis emitted appearance.y_left_axis for ALL panel
        # types.  Kibana rejects y_left_axis on non-XY panels with
        # "Extra inputs are not permitted", failing the entire dashboard compile.
        query = "avg:system.mem.used{*}"
        mq = parse_metric_query(query)
        for widget_type in ("query_value", "toplist"):
            with self.subTest(widget_type=widget_type):
                widget = NormalizedWidget(
                    id="1",
                    widget_type=widget_type,
                    title="Memory",
                    queries=[
                        WidgetQuery(name="q1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
                    ],
                    yaxis={"min": "0", "max": "100", "scale": "log", "label": "MB"},
                    layout={"x": 0, "y": 0, "width": 4, "height": 3},
                )
                result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
                dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
                generate_dashboard_yaml(dash, [result])
                appearance = (result.yaml_panel.get("esql") or {}).get("appearance", {})
                self.assertNotIn(
                    "y_left_axis", appearance,
                    f"{widget_type} panel must not have y_left_axis in appearance",
                )

    def test_xy_panel_with_yaxis_still_emits_y_left_axis(self):
        # Ensure the guard does not break XY (timeseries) panels.
        result = self._translate_with_yaml(
            self._make_timeseries_widget({"min": "0", "max": "100", "scale": "log"})
        )
        appearance = result.yaml_panel["esql"].get("appearance", {})
        self.assertIn("y_left_axis", appearance)
        self.assertIn("scale", appearance["y_left_axis"])
        self.assertIn("extent", appearance["y_left_axis"])

    def test_metric_panel_primary_label_not_set_when_equal_to_title(self):
        # Regression: _metric_label returned widget.title for query_value panels,
        # causing primary.label == panel title.  kb-dashboard-cli fires
        # metric-redundant-label in that case, failing the compile step.
        query = "avg:calico.felix.active_local_endpoints{*}"
        mq = parse_metric_query(query)
        widget = NormalizedWidget(
            id="1",
            widget_type="query_value",
            title="Active endpoints",
            queries=[
                WidgetQuery(name="q1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
            ],
            layout={"x": 0, "y": 0, "width": 4, "height": 3},
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Calico overview", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        primary = result.yaml_panel["esql"]["primary"]
        panel_title = result.yaml_panel.get("title", widget.title)
        self.assertNotEqual(
            primary.get("label", ""),
            panel_title,
            "primary.label must not duplicate the panel title (metric-redundant-label)",
        )

    def test_metric_panel_formula_alias_label_preserved(self):
        # When a formula has an explicit alias, that alias IS meaningful and must
        # be kept as the primary label (even if it happens to differ from the title).
        query = "avg:system.cpu.user{*}"
        mq = parse_metric_query(query)
        widget = NormalizedWidget(
            id="1",
            widget_type="query_value",
            title="CPU Usage",
            queries=[
                WidgetQuery(name="q1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
            ],
            formulas=[WidgetFormula(raw="q1", alias="User CPU %")],
            layout={"x": 0, "y": 0, "width": 4, "height": 3},
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        primary = result.yaml_panel["esql"]["primary"]
        self.assertEqual(primary.get("label"), "User CPU %")

    def test_log_stream_uses_breakdown_columns(self):
        lq = parse_log_query("status:error")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="status:error", log_query=lq, query_type="log")
        widget = NormalizedWidget(
            id="1",
            widget_type="log_stream",
            title="Logs",
            queries=[wq],
            layout={"x": 0, "y": 0, "width": 8, "height": 4},
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        esql = result.yaml_panel["esql"]
        self.assertNotIn("metrics", esql)
        self.assertEqual([col["field"] for col in esql["breakdowns"][:3]], ["@timestamp", "message", "log.level"])
        self.assertEqual(esql["breakdowns"][0]["label"], "Timestamp")

    def test_query_table_metric_label_uses_metric_name(self):
        query = "sum:mongodb.opcounters.getmoreps{*} by {replset_name}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(
            name="query1",
            data_source="metrics",
            raw_query=query,
            metric_query=mq,
            aggregator="sum",
            query_type="metric",
        )
        widget = NormalizedWidget(
            id="1",
            widget_type="query_table",
            title="For reads",
            queries=[wq],
            layout={"x": 0, "y": 0, "width": 8, "height": 4},
        )
        plan = plan_widget(widget)
        plan.backend = "esql"
        result = translate_widget(widget, plan, OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        self.assertEqual(result.yaml_panel["esql"]["metrics"][0]["label"], "Getmoreps")

    def test_count_nonzero_metric_uses_final_value_field(self):
        query = "avg:kubernetes.pods.running{*} by {kube_cluster_name}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="count_nonzero(query1)")
        wf.expression = parse_formula("count_nonzero(query1)")
        widget = NormalizedWidget(
            id="1",
            widget_type="query_value",
            title="Clusters",
            queries=[wq],
            formulas=[wf],
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        self.assertEqual(result.yaml_panel["esql"]["primary"]["field"], "value")

    def test_heatmap_uses_non_time_dimension_as_y_axis(self):
        query = "avg:system.cpu.user{*} by {host}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        widget = NormalizedWidget(
            id="1",
            widget_type="heatmap",
            title="CPU Heatmap",
            queries=[wq],
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        self.assertEqual(result.yaml_panel["esql"]["y_axis"]["field"], "host.name")

    def test_treemap_breakdowns_are_capped_to_two_fields(self):
        query = "sum:consul.catalog.services_critical{*} by {consul_node_id,consul_datacenter,host}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        widget = NormalizedWidget(
            id="1",
            widget_type="treemap",
            title="Treemap",
            queries=[wq],
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        self.assertEqual(len(result.yaml_panel["esql"]["breakdowns"]), 2)

    def test_timeseries_with_two_group_dims_warns_about_dropped_breakdown(self):
        # A Datadog timeseries grouped by two tags maps to a Kibana XY chart that
        # can only break the series down by a single field. The second dimension
        # is in the ES|QL output but not on the chart, so series differing only by
        # it are visually merged. That must surface as a warning, not silently.
        query = "avg:cassandra.gc.minor.collection_time{*} by {cloud_region,host}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(
            name="query1",
            data_source="metrics",
            raw_query=query,
            metric_query=mq,
            aggregator="avg",
            query_type="metric",
        )
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="GC per region/host",
            queries=[wq],
            layout={"x": 0, "y": 0, "width": 12, "height": 4},
        )
        plan = plan_widget(widget)
        plan.backend = "esql"
        result = translate_widget(widget, plan, OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        esql = result.yaml_panel["esql"]
        # only one breakdown is rendered (the first non-time dimension)
        self.assertIn("breakdown", esql)
        self.assertIn(esql["breakdown"]["field"], ("cloud_region", "cloud.region"))
        # ...so the dropped dimension must be called out in the warnings
        self.assertTrue(
            any("not on the chart" in w or "visually merged" in w for w in result.warnings),
            f"expected a dropped-breakdown warning, got: {result.warnings}",
        )

    def test_timeseries_with_single_group_dim_does_not_warn(self):
        # A single grouping dimension fits the XY breakdown exactly: no warning.
        query = "avg:cassandra.gc.minor.collection_time{*} by {host}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(
            name="query1",
            data_source="metrics",
            raw_query=query,
            metric_query=mq,
            aggregator="avg",
            query_type="metric",
        )
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="GC per host",
            queries=[wq],
            layout={"x": 0, "y": 0, "width": 12, "height": 4},
        )
        plan = plan_widget(widget)
        plan.backend = "esql"
        result = translate_widget(widget, plan, OTEL_PROFILE)
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[widget])
        generate_dashboard_yaml(dash, [result])
        self.assertFalse(
            any("not on the chart" in w or "visually merged" in w for w in result.warnings),
            f"did not expect a dropped-breakdown warning, got: {result.warnings}",
        )

    def test_single_chart_expands_to_full_width(self):
        widget = self._make_metric_widget("1", "CPU trend", "timeseries", {"x": 0, "y": 0, "width": 4, "height": 2})
        dash = self._render_dashboard([widget])
        panel = dash["panels"][0]
        self.assertEqual(panel["size"]["w"], 48)
        self.assertEqual(panel["size"]["h"], 12)

    def test_consecutive_singleton_charts_repack_two_up(self):
        widgets = [
            self._make_metric_widget("1", "A", "timeseries", {"x": 0, "y": 0, "width": 4, "height": 2}),
            self._make_metric_widget("2", "B", "timeseries", {"x": 0, "y": 4, "width": 4, "height": 2}),
            self._make_metric_widget("3", "C", "timeseries", {"x": 0, "y": 8, "width": 4, "height": 2}),
            self._make_metric_widget("4", "D", "timeseries", {"x": 0, "y": 12, "width": 4, "height": 2}),
        ]
        dash = self._render_dashboard(widgets)
        panels = dash["panels"]
        self.assertEqual([p["size"]["w"] for p in panels], [24, 24, 24, 24])
        self.assertEqual([p["position"]["x"] for p in panels], [0, 24, 0, 24])
        self.assertEqual([p["position"]["y"] for p in panels], [0, 0, 12, 12])

    def test_metric_and_chart_row_use_kibana_split_and_heights(self):
        widgets = [
            self._make_metric_widget("1", "CPU avg", "query_value", {"x": 0, "y": 0, "width": 4, "height": 2}),
            self._make_metric_widget("2", "CPU trend", "timeseries", {"x": 4, "y": 0, "width": 8, "height": 2}),
        ]
        dash = self._render_dashboard(widgets)
        left, right = dash["panels"]
        # metric (query_value) min_h=6; line (timeseries) default h=12
        self.assertEqual(left["size"], {"w": 16, "h": 6})
        self.assertEqual(right["size"], {"w": 32, "h": 12})
        self.assertEqual(left["position"]["y"], right["position"]["y"])

    def test_intro_markdown_row_is_promoted_before_analytics(self):
        note = NormalizedWidget(
            id="n1",
            widget_type="note",
            title="",
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
            raw_definition={"type": "note", "content": "Intro"},
        )
        table_a = self._make_metric_widget("2", "Table A", "toplist", {"x": 4, "y": 0, "width": 4, "height": 2})
        table_b = self._make_metric_widget("3", "Table B", "toplist", {"x": 8, "y": 0, "width": 4, "height": 2})
        dash = self._render_dashboard([note, table_a, table_b])
        intro, first, second = dash["panels"]
        self.assertEqual(intro["markdown"]["content"], "Intro")
        self.assertEqual(intro["size"]["w"], 48)
        self.assertEqual(first["position"]["y"], 6)
        self.assertEqual(second["position"]["y"], 6)
        self.assertEqual(first["size"]["w"], 24)
        self.assertEqual(second["size"]["w"], 24)

    def test_ordered_dashboard_without_layout_reflows_charts_two_up(self):
        raw = {
            "title": "Ordered",
            "layout_type": "ordered",
            "widgets": [
                {"definition": {"type": "timeseries", "title": "A", "requests": [{"queries": [{"data_source": "metrics", "name": "a", "query": "avg:system.cpu.user{*}"}]}]}},
                {"definition": {"type": "timeseries", "title": "B", "requests": [{"queries": [{"data_source": "metrics", "name": "b", "query": "avg:system.cpu.system{*}"}]}]}},
                {"definition": {"type": "timeseries", "title": "C", "requests": [{"queries": [{"data_source": "metrics", "name": "c", "query": "avg:system.cpu.idle{*}"}]}]}},
                {"definition": {"type": "timeseries", "title": "D", "requests": [{"queries": [{"data_source": "metrics", "name": "d", "query": "avg:system.cpu.iowait{*}"}]}]}},
            ],
        }
        nd = normalize_dashboard(raw)
        results = []
        for widget in nd.widgets:
            plan = plan_widget(widget)
            if plan.backend == "lens":
                plan.backend = "esql"
            results.append(translate_widget(widget, plan, OTEL_PROFILE))
        dash = yaml.safe_load(generate_dashboard_yaml(nd, results))["dashboards"][0]
        panels = dash["panels"]
        self.assertEqual([p["size"]["w"] for p in panels], [24, 24, 24, 24])
        self.assertEqual([p["position"]["x"] for p in panels], [0, 24, 0, 24])
        self.assertEqual([p["position"]["y"] for p in panels], [0, 0, 12, 12])

    def test_placeholder_mixed_with_table_moves_below_real_panel(self):
        broken = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Broken",
            queries=[WidgetQuery(name="q1", data_source="metrics", raw_query="broken", query_type="metric_unparsed")],
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )
        table = self._make_metric_widget("2", "Table", "toplist", {"x": 4, "y": 0, "width": 4, "height": 2})
        dash = self._render_dashboard([broken, table])
        broken_panel = next(panel for panel in dash["panels"] if panel["title"] == "Broken")
        table_panel = next(panel for panel in dash["panels"] if panel["title"] == "Table")
        self.assertEqual(table_panel["size"]["w"], 48)
        self.assertGreater(broken_panel["position"]["y"], table_panel["position"]["y"])

    def test_duplicate_request_formula_labels_use_metric_names(self):
        raw = {
            "title": "Labels",
            "layout_type": "ordered",
            "widgets": [
                {
                    "definition": {
                        "type": "timeseries",
                        "title": "Rows",
                        "requests": [
                            {
                                "queries": [{"data_source": "metrics", "name": "query1", "query": "avg:postgresql.rows_fetched{*}"}],
                                "formulas": [{"formula": "query1"}],
                            },
                            {
                                "queries": [{"data_source": "metrics", "name": "query1", "query": "avg:postgresql.rows_returned{*}"}],
                                "formulas": [{"formula": "query1"}],
                            },
                        ],
                    }
                }
            ],
        }
        nd = normalize_dashboard(raw)
        results = [translate_widget(widget, plan_widget(widget), OTEL_PROFILE) for widget in nd.widgets]
        dash = yaml.safe_load(generate_dashboard_yaml(nd, results))["dashboards"][0]
        labels = dash["panels"][0]["esql"]["metrics"]
        self.assertEqual([metric["label"] for metric in labels], ["Rows fetched", "Rows returned"])


class TestSemanticPipelineRoundTrip(unittest.TestCase):
    def test_metric_formula_pipeline_preserves_plan_query_and_yaml_semantics(self):
        q1 = "sum:http.requests.errors_total{env:prod,service:checkout} by {service}.as_count()"
        q2 = "sum:http.requests.total{env:prod,service:checkout} by {service}.as_count()"
        mq1 = parse_metric_query(q1)
        mq2 = parse_metric_query(q2)
        wf = WidgetFormula(raw="(query1 / query2) * 100", alias="error_rate_pct")
        wf.expression = parse_formula("(query1 / query2) * 100")
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Error rate",
            queries=[
                WidgetQuery(name="query1", data_source="metrics", raw_query=q1, metric_query=mq1, query_type="metric"),
                WidgetQuery(name="query2", data_source="metrics", raw_query=q2, metric_query=mq2, query_type="metric"),
            ],
            formulas=[wf],
            layout={"x": 0, "y": 0, "width": 8, "height": 4},
        )

        plan = plan_widget(widget)
        self.assertEqual(plan.backend, "esql")
        self.assertEqual(plan.kibana_type, "xy")

        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "warning")
        self.assertTrue(any("as_count" in w for w in result.warnings), result.warnings)
        self.assertEqual(result.source_queries, [q1, q2])
        self.assertIn("query1 = SUM(http_requests_errors_total)", result.esql_query)
        self.assertIn("query2 = SUM(http_requests_total)", result.esql_query)
        self.assertIn("| EVAL error_rate_pct = ((query1 / query2) * 100)", result.esql_query)
        self.assertIn("| KEEP time_bucket, service.name, error_rate_pct", result.esql_query)

        payload = yaml.safe_load(
            generate_dashboard_yaml(NormalizedDashboard(id="1", title="Dash", widgets=[widget]), [result])
        )
        panel = payload["dashboards"][0]["panels"][0]
        self.assertEqual(panel["esql"]["query"], result.yaml_panel["esql"]["query"])
        self.assertEqual(panel["esql"]["dimension"]["field"], "time_bucket")
        self.assertEqual(panel["esql"]["breakdown"]["field"], "service.name")
        self.assertEqual(panel["esql"]["metrics"][0]["field"], "error_rate_pct")

    def test_log_timeseries_pipeline_uses_kql_bridge_and_xy_yaml_contract(self):
        raw_query = "service:checkout status:error timeout"
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Checkout errors",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="logs",
                    raw_query=raw_query,
                    log_query=parse_log_query(raw_query),
                    query_type="log",
                )
            ],
            layout={"x": 0, "y": 0, "width": 8, "height": 4},
        )

        plan = plan_widget(widget)
        self.assertEqual(plan.backend, "esql_with_kql")
        self.assertEqual(plan.kibana_type, "xy")

        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn('KQL("(service.name: checkout) AND (log.level: error) AND timeout")', result.esql_query)
        self.assertIn("| STATS count = COUNT(*) BY time_bucket", result.esql_query)

        payload = yaml.safe_load(
            generate_dashboard_yaml(NormalizedDashboard(id="1", title="Dash", widgets=[widget]), [result])
        )
        panel = payload["dashboards"][0]["panels"][0]
        self.assertEqual(panel["esql"]["query"], result.yaml_panel["esql"]["query"])
        self.assertEqual(panel["esql"]["dimension"]["field"], "time_bucket")
        self.assertEqual(panel["esql"]["metrics"][0]["field"], "count")

    def test_manage_status_pipeline_emits_markdown_placeholder_with_hint(self):
        widget = NormalizedWidget(
            id="1",
            widget_type="manage_status",
            title="Monitors",
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )

        plan = plan_widget(widget)
        # New behavior: routed to markdown backend so the widget renders as
        # an informative placeholder instead of blocking.
        self.assertEqual(plan.backend, "markdown")

        result = translate_widget(widget, plan, OTEL_PROFILE)
        # Markdown placeholders for non-text widgets land as requires_manual
        # (the YAML uploads, the panel surfaces guidance).
        self.assertEqual(result.status, "requires_manual")

        payload = yaml.safe_load(
            generate_dashboard_yaml(NormalizedDashboard(id="1", title="Dash", widgets=[widget]), [result])
        )
        panel = payload["dashboards"][0]["panels"][0]
        self.assertIn("markdown", panel)
        content = panel["markdown"]["content"]
        self.assertIn("manage_status", content)
        # Hint about the Elastic Alerts equivalent must be present.
        self.assertIn("Alerts", content)

    def test_check_status_pipeline_emits_markdown_placeholder_with_hint(self):
        widget = NormalizedWidget(
            id="1",
            widget_type="check_status",
            title="Server reachable",
        )

        plan = plan_widget(widget)
        self.assertEqual(plan.backend, "markdown")

        result = translate_widget(widget, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "requires_manual")

        payload = yaml.safe_load(
            generate_dashboard_yaml(NormalizedDashboard(id="1", title="Dash", widgets=[widget]), [result])
        )
        content = payload["dashboards"][0]["panels"][0]["markdown"]["content"]
        self.assertIn("check_status", content)
        # Hint about the Elastic Synthetics equivalent.
        self.assertIn("Synthetics", content)

    def test_partition_widget_without_groupby_is_requires_manual(self):
        # Sunburst (partition) widgets need at least one group-by; when
        # the source has none, the translator surfaces requires_manual
        # instead of blocking the upload.
        query = "sum:rabbitmq.connection.incoming_packets.count{*}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(
            name="query1", data_source="metrics", raw_query=query,
            metric_query=mq, query_type="metric",
        )
        widget = NormalizedWidget(
            id="1", widget_type="sunburst", title="Packets",
            queries=[wq],
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertEqual(result.status, "requires_manual")
        # The placeholder mentions the source query so the reviewer can see it.
        self.assertTrue(
            any("group" in (w or "").lower() for w in result.warnings),
            f"expected a group-related warning, got {result.warnings}",
        )

    def test_log_widget_with_modern_search_query_translates(self):
        # Modern Datadog log widgets put the filter in raw_q["search"]["query"]
        # instead of raw_q["query"]. Verify normalize captures it.
        raw_dashboard = {
            "title": "Log",
            "widgets": [{
                "definition": {
                    "title": "Count by status",
                    "type": "timeseries",
                    "requests": [{
                        "response_format": "timeseries",
                        "queries": [{
                            "data_source": "logs",
                            "name": "a",
                            "search": {"query": "service:kafka"},
                            "compute": {"aggregation": "count"},
                        }],
                        "formulas": [{"formula": "a"}],
                    }],
                },
            }],
        }
        from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
        nz = normalize_dashboard(raw_dashboard)
        widget = nz.widgets[0]
        # The raw_query should reflect the modern search.query field.
        self.assertEqual(widget.queries[0].raw_query, "service:kafka")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertNotEqual(result.status, "not_feasible")
        self.assertIn("service.name", result.esql_query)

    def test_log_widget_with_empty_query_translates_as_match_all(self):
        # Some Datadog widgets are emitted with no log filter at all; we
        # should treat that as a match-all query rather than failing.
        raw_dashboard = {
            "title": "Log",
            "widgets": [{
                "definition": {
                    "title": "All logs",
                    "type": "timeseries",
                    "requests": [{
                        "response_format": "timeseries",
                        "queries": [{
                            "data_source": "logs",
                            "name": "a",
                        }],
                        "formulas": [{"formula": "a"}],
                    }],
                },
            }],
        }
        from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
        nz = normalize_dashboard(raw_dashboard)
        widget = nz.widgets[0]
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertNotEqual(result.status, "not_feasible")
        self.assertIn("FROM logs-*", result.esql_query)
        # No KQL filter beyond the time clause.
        self.assertIn("WHERE @timestamp", result.esql_query)


# =========================================================================
# Field Map Tests
# =========================================================================

class TestFieldMap(unittest.TestCase):

    def test_otel_metric_map(self):
        self.assertEqual(OTEL_PROFILE.map_metric("system.cpu.user"), "system_cpu_user")

    def test_otel_tag_map(self):
        self.assertEqual(OTEL_PROFILE.map_tag("host"), "host.name")
        self.assertEqual(OTEL_PROFILE.map_tag("env"), "deployment.environment")
        self.assertEqual(OTEL_PROFILE.map_tag("service"), "service.name")

    def test_otel_tag_map_prefers_otel_kubernetes_semconv_fields(self):
        self.assertEqual(OTEL_PROFILE.map_tag("pod_name"), "k8s.pod.name")
        self.assertEqual(OTEL_PROFILE.map_tag("kube_namespace"), "k8s.namespace.name")
        self.assertEqual(OTEL_PROFILE.map_tag("kube_cluster_name"), "k8s.cluster.name")
        self.assertEqual(OTEL_PROFILE.map_tag("kube_deployment"), "k8s.deployment.name")
        self.assertEqual(OTEL_PROFILE.map_tag("kube_daemon_set"), "k8s.daemonset.name")
        self.assertEqual(OTEL_PROFILE.map_tag("kube_replica_set"), "k8s.replicaset.name")
        self.assertEqual(OTEL_PROFILE.map_tag("kube_stateful_set"), "k8s.statefulset.name")
        self.assertEqual(OTEL_PROFILE.map_tag("node"), "k8s.node.name")
        self.assertEqual(OTEL_PROFILE.map_tag("region"), "cloud.region")
        self.assertEqual(OTEL_PROFILE.map_tag("availability_zone"), "cloud.availability_zone")
        self.assertEqual(OTEL_PROFILE.map_tag("zone"), "cloud.availability_zone")

    def test_elastic_agent_profile_keeps_elastic_kubernetes_fields(self):
        profile = load_profile("elastic_agent")

        self.assertEqual(profile.map_tag("pod_name"), "kubernetes.pod.name")
        self.assertEqual(profile.map_tag("kube_namespace"), "kubernetes.namespace")

    def test_otel_tag_map_prefers_aggregatable_keyword_subfield_when_live_caps_require_it(self):
        profile = load_profile("otel")
        profile.metric_field_caps = {
            "k8s.cluster.name": FieldCapability(
                name="k8s.cluster.name",
                type="text",
                aggregatable=False,
                conflicting_types=["keyword", "text"],
            ),
            "k8s.cluster.name.keyword": FieldCapability(
                name="k8s.cluster.name.keyword",
                type="keyword",
                aggregatable=True,
            ),
        }

        self.assertEqual(profile.map_tag("kube_cluster_name", context="metric"), "k8s.cluster.name.keyword")

    def test_otel_tag_map_keeps_base_field_when_live_caps_are_aggregatable(self):
        profile = load_profile("otel")
        profile.metric_field_caps = {
            "k8s.cluster.name": FieldCapability(
                name="k8s.cluster.name",
                type="keyword",
                aggregatable=True,
            ),
            "k8s.cluster.name.keyword": FieldCapability(
                name="k8s.cluster.name.keyword",
                type="keyword",
                aggregatable=True,
            ),
        }

        self.assertEqual(profile.map_tag("kube_cluster_name", context="metric"), "k8s.cluster.name")

    def test_otel_tag_map_prefers_keyword_subfield_for_passthrough_dimensions(self):
        profile = load_profile("otel")
        profile.metric_field_caps = {
            "docker_image": FieldCapability(
                name="docker_image",
                type="text",
                aggregatable=False,
            ),
            "docker_image.keyword": FieldCapability(
                name="docker_image.keyword",
                type="keyword",
                aggregatable=True,
            ),
        }

        self.assertEqual(profile.map_tag("docker_image", context="metric"), "docker_image.keyword")

    def test_passthrough_keeps_names(self):
        self.assertEqual(PASSTHROUGH_PROFILE.map_metric("system.cpu.user"), "system_cpu_user")
        self.assertEqual(PASSTHROUGH_PROFILE.map_tag("host"), "host")

    def test_load_builtin_profile(self):
        profile = load_profile("otel")
        self.assertEqual(profile.name, "otel")

    def test_load_builtin_profile_returns_independent_copy(self):
        profile = load_profile("otel")
        profile.metric_index = "custom-metrics-*"
        self.assertEqual(load_profile("otel").metric_index, "metrics-*")

    @patch("observability_migration.adapters.source.datadog.field_map.fetch_field_capabilities")
    def test_load_live_field_capabilities_separates_metric_and_log_contexts(self, mock_fetch):
        mock_fetch.side_effect = [
            {"shared.field": FieldCapability(name="shared.field", type="double")},
            {"shared.field": FieldCapability(name="shared.field", type="keyword")},
        ]
        profile = load_profile("otel")

        counts = profile.load_live_field_capabilities("https://example.es", es_api_key="secret")

        self.assertEqual(counts, {"metric_fields": 1, "log_fields": 1})
        metric_cap = profile.field_capability("shared.field", context="metric")
        log_cap = profile.field_capability("shared.field", context="log")
        assert metric_cap is not None
        assert log_cap is not None
        self.assertEqual(metric_cap.type, "double")
        self.assertEqual(log_cap.type, "keyword")
        self.assertTrue(profile.is_numeric_field("shared.field", context="metric"))
        self.assertFalse(profile.is_numeric_field("shared.field", context="log"))
        self.assertEqual(mock_fetch.call_args_list[0].args, ("https://example.es", "metrics-*"))
        self.assertEqual(mock_fetch.call_args_list[1].args, ("https://example.es", "logs-*"))
        self.assertEqual(mock_fetch.call_args_list[0].kwargs, {"es_api_key": "secret", "verify": True})
        self.assertEqual(mock_fetch.call_args_list[1].kwargs, {"es_api_key": "secret", "verify": True})

    def test_load_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            load_profile("nonexistent")


class TestDatadogCliFieldProfileContract(unittest.TestCase):
    def test_parse_args_defaults_field_profile_to_otel(self):
        args = datadog_cli.parse_args([])

        self.assertEqual(args.field_profile, "otel")

    def test_parse_args_accepts_input_mode_alias_for_source(self):
        args = datadog_cli.parse_args(["--input-mode", "api"])

        self.assertEqual(args.input_mode, "api")
        self.assertEqual(args.source, "api")

    def test_parse_args_rejects_conflicting_source_and_input_mode(self):
        with self.assertRaises(SystemExit):
            datadog_cli.parse_args(["--source", "files", "--input-mode", "api"])

    def test_parse_args_compiles_dashboards_by_default(self):
        args = datadog_cli.parse_args([])

        self.assertTrue(args.compile)

    def test_parse_args_can_disable_default_compile(self):
        args = datadog_cli.parse_args(["--no-compile"])

        self.assertFalse(args.compile)

    def test_parse_args_does_not_default_data_view_over_profile_index(self):
        args = datadog_cli.parse_args(["--field-profile", "prometheus"])

        self.assertIsNone(args.data_view)

    def test_explicit_data_view_still_overrides_profile_index(self):
        args = datadog_cli.parse_args([
            "--field-profile",
            "prometheus",
            "--data-view",
            "metrics-custom-*",
        ])

        self.assertEqual(args.data_view, "metrics-custom-*")

    def test_target_readiness_contract_reports_mapped_field_status(self):
        field_map = load_profile("otel")
        metric_cap = FieldCapability(name="system_cpu_user", type="double")
        metric_cap.aggregatable = True
        tag_cap = FieldCapability(name="host.name", type="keyword")
        tag_cap.aggregatable = True
        field_map.metric_field_caps = {
            "system_cpu_user": metric_cap,
            "host.name": tag_cap,
        }
        field_map.field_caps = dict(field_map.metric_field_caps)
        query = "avg:system.cpu.user{host:web01} by {host}"
        widget = NormalizedWidget(
            id="w1",
            widget_type="timeseries",
            title="CPU",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query=query,
                    metric_query=parse_metric_query(query),
                    query_type="metric",
                ),
            ],
        )
        dashboard = NormalizedDashboard(id="dash1", title="Dash", widgets=[widget])

        contract = datadog_preflight.build_target_readiness_contract(
            [dashboard],
            field_map,
        )

        self.assertEqual(contract["source"], "datadog")
        self.assertEqual(contract["field_profile"], "otel")
        self.assertEqual(contract["metric_index"], "metrics-*")
        self.assertEqual(contract["required_fields"]["system_cpu_user"]["status"], "confirmed")
        self.assertEqual(
            contract["required_fields"]["system_cpu_user"]["source_fields"],
            ["system.cpu.user"],
        )
        self.assertEqual(contract["required_fields"]["host.name"]["status"], "confirmed")
        self.assertEqual(contract["required_fields"]["host.name"]["roles"], ["filter", "group_by"])

    def test_target_readiness_contract_reports_missing_and_unknown_fields(self):
        field_map = load_profile("otel")
        metric_cap = FieldCapability(name="system_cpu_user", type="double")
        metric_cap.aggregatable = True
        field_map.metric_field_caps = {"system_cpu_user": metric_cap}
        field_map.field_caps = dict(field_map.metric_field_caps)
        query = "avg:system.cpu.user{host:web01}"
        widget = NormalizedWidget(
            id="w1",
            widget_type="query_value",
            title="CPU",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query=query,
                    metric_query=parse_metric_query(query),
                    query_type="metric",
                ),
            ],
        )
        dashboard = NormalizedDashboard(id="dash1", title="Dash", widgets=[widget])

        contract = datadog_preflight.build_target_readiness_contract(
            [dashboard],
            field_map,
        )

        self.assertEqual(contract["required_fields"]["system_cpu_user"]["status"], "confirmed")
        self.assertEqual(contract["required_fields"]["host.name"]["status"], "missing")

        offline_contract = datadog_preflight.build_target_readiness_contract(
            [dashboard],
            load_profile("otel"),
        )

        self.assertEqual(offline_contract["required_fields"]["system_cpu_user"]["status"], "unknown")
        self.assertEqual(offline_contract["required_fields"]["host.name"]["status"], "unknown")

    def test_dashboard_pipeline_writes_target_readiness_contract(self):
        args = argparse.Namespace(
            source="files",
            input_dir="unused",
            validate=False,
            es_url="",
            upload=False,
            ensure_data_views=False,
            smoke=False,
            smoke_output="",
            space_id="",
            preflight=False,
        )
        raw_dashboard = {
            "id": "dash1",
            "title": "Dash",
            "widgets": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            datadog_cli,
            "_extract",
            return_value=[raw_dashboard],
        ):
            datadog_cli._run_dashboard_pipeline(
                args=args,
                field_map=load_profile("otel"),
                output_dir=Path(tmpdir),
                dd_creds={},
                target_adapter=None,
                compile_requested=False,
            )

            contract_path = Path(tmpdir) / "target_readiness_contract.json"
            schema_report_path = Path(tmpdir) / "schema_change_report.md"
            telemetry_contract_path = Path(tmpdir) / "telemetry_contract.json"
            contract_exists = contract_path.exists()
            schema_report_exists = schema_report_path.exists()
            telemetry_contract_exists = telemetry_contract_path.exists()
            contract = json.loads(contract_path.read_text(encoding="utf-8"))

        self.assertTrue(contract_exists)
        self.assertEqual(contract["source"], "datadog")
        self.assertEqual(contract["field_profile"], "otel")
        self.assertTrue(schema_report_exists)
        self.assertTrue(telemetry_contract_exists)

    def test_dashboard_pipeline_treats_schema_report_failure_as_non_fatal(self):
        args = argparse.Namespace(
            source="files",
            input_dir="unused",
            validate=False,
            es_url="",
            upload=False,
            ensure_data_views=False,
            smoke=False,
            smoke_output="",
            space_id="",
            preflight=False,
        )
        raw_dashboard = {
            "id": "dash1",
            "title": "Dash",
            "widgets": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            datadog_cli,
            "_extract",
            return_value=[raw_dashboard],
        ), patch.object(
            datadog_cli,
            "write_schema_report_artifacts",
            side_effect=RuntimeError("schema failed"),
        ):
            datadog_cli._run_dashboard_pipeline(
                args=args,
                field_map=load_profile("otel"),
                output_dir=Path(tmpdir),
                dd_creds={},
                target_adapter=None,
                compile_requested=False,
            )

            summary_exists = (Path(tmpdir) / "migration_summary.md").exists()
            readiness_exists = (Path(tmpdir) / "target_readiness_contract.json").exists()
            schema_report_exists = (Path(tmpdir) / "schema_change_report.md").exists()

        self.assertTrue(summary_exists)
        self.assertTrue(readiness_exists)
        self.assertFalse(schema_report_exists)


# =========================================================================
# End-to-End Pipeline Test
# =========================================================================

class TestEndToEnd(unittest.TestCase):

    def test_full_pipeline_sample_dashboard(self):
        """Run the full pipeline on the sample dashboard and verify outputs."""
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        raw = json.loads(path.read_text())

        nd = normalize_dashboard(raw)
        self.assertEqual(len(nd.widgets), 11)

        field_map = OTEL_PROFILE
        results = []
        for widget in nd.widgets:
            plan = plan_widget(widget)
            result = translate_widget(widget, plan, field_map)
            results.append(result)

        ok_count = sum(1 for r in results if r.status == "ok")
        warning_count = sum(1 for r in results if r.status == "warning")

        self.assertTrue(ok_count + warning_count > 0, "at least some panels should translate")
        self.assertTrue(ok_count >= 4, f"expected at least 4 OK panels, got {ok_count}")

        esql_count = sum(1 for r in results if r.esql_query)
        self.assertTrue(esql_count >= 4, f"expected at least 4 ES|QL queries, got {esql_count}")

        yaml_str = generate_dashboard_yaml(nd, results)
        self.assertTrue(len(yaml_str) > 100)

        import yaml as yaml_lib
        doc = yaml_lib.safe_load(yaml_str)
        self.assertIn("dashboards", doc)
        dash = doc["dashboards"][0]
        self.assertIn("panels", dash)
        self.assertTrue(len(dash["panels"]) > 0)


class TestDatadogExtractionContracts(unittest.TestCase):
    def test_extract_dashboards_from_api_requires_optional_client_extra(self):
        real_import = __import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name.startswith("datadog_api_client"):
                raise ImportError("No module named 'datadog_api_client'")
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import), self.assertRaisesRegex(
            ImportError,
            r"datadog-api-client.*\.\[datadog\]",
        ):
            datadog_extract.extract_dashboards_from_api(
                api_key="api-key",
                app_key="app-key",
            )

    def test_extract_empty_input_dir_exits_with_clean_message(self):
        """An empty/no-JSON input dir should exit(1) with a helpful message, not a traceback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(source="files", input_dir=tmpdir)
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(stderr):
                datadog_cli._extract(args)
        self.assertEqual(ctx.exception.code, 1)
        message = stderr.getvalue()
        self.assertIn("no Datadog dashboards found", message)
        self.assertIn(tmpdir, message)
        self.assertNotIn("Traceback", message)


class TestDatadogAssetStatusIntegration(unittest.TestCase):
    """Verify Datadog models integrate with shared AssetStatus vocabulary."""

    def test_load_raw_monitors_reads_root_level_monitor_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "monitor-high-5xx-rate.json").write_text(
                json.dumps(
                    {
                        "id": 91001,
                        "name": "High 5xx rate",
                        "type": "query alert",
                        "query": "avg(last_5m):sum:http.requests{service:sample-api}.as_count() > 25",
                    }
                ),
                encoding="utf-8",
            )

            monitors = datadog_alert_pipeline.load_raw_monitors(
                argparse.Namespace(source="files", input_dir=str(root)),
                {},
            )

        self.assertEqual(len(monitors), 1)
        self.assertEqual(monitors[0]["type"], "query alert")

    def test_translation_result_asset_status(self):
        from observability_migration.core.assets.status import AssetStatus

        ok_result = TranslationResult(status="ok")
        self.assertEqual(ok_result.asset_status, AssetStatus.TRANSLATED)

        warning_result = TranslationResult(status="warning")
        self.assertEqual(warning_result.asset_status, AssetStatus.TRANSLATED_WITH_WARNINGS)

        blocked_result = TranslationResult(status="blocked")
        self.assertEqual(blocked_result.asset_status, AssetStatus.NOT_FEASIBLE)

    def test_dashboard_result_runtime_summary(self):
        dr = DashboardResult(dashboard_title="Test", compiled=True)
        summary = dr.build_runtime_summary()
        self.assertEqual(summary["compile"]["status"], "pass")
        self.assertEqual(summary["yaml_lint"]["status"], "not_run")
        self.assertEqual(summary["upload"]["status"], "not_run")

    def test_dashboard_result_runtime_summary_upload_failure(self):
        dr = DashboardResult(
            dashboard_title="Test",
            upload_attempted=True,
            uploaded=False,
            upload_error="boom",
        )
        summary = dr.build_runtime_summary()
        self.assertEqual(summary["upload"]["status"], "fail")
        self.assertEqual(summary["upload"]["error"], "boom")

    def test_dashboard_result_unified_counts(self):
        dr = DashboardResult(
            dashboard_title="Test",
            panel_results=[
                TranslationResult(status="ok"),
                TranslationResult(status="ok", warnings=["w"]),
                TranslationResult(status="blocked"),
            ],
        )
        dr.recompute_counts()
        counts = dr.unified_status_counts
        self.assertEqual(counts["translated"], 2)
        self.assertEqual(counts["not_feasible"], 1)

    def test_detailed_report_includes_preflight_summary(self):
        dr = DashboardResult(
            dashboard_id="dash-1",
            dashboard_title="Test",
            preflight_passed=False,
            preflight_issues=[
                {
                    "level": "block",
                    "category": "field",
                    "message": "field 'system_cpu_user' is typed as 'keyword' (keyword) but aggregate requires 'numeric'",
                    "widget_id": "widget-1",
                    "field_name": "system_cpu_user",
                }
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"
            save_detailed_report([dr], str(output_path))
            payload = json.loads(output_path.read_text())

        self.assertEqual(payload["summary"]["preflight_blocks"], 1)
        self.assertEqual(payload["summary"]["preflight_warnings"], 0)
        dashboard_entry = payload["dashboards"][0]
        self.assertFalse(dashboard_entry["preflight"]["passed"])
        self.assertEqual(dashboard_entry["preflight"]["issue_counts"]["block"], 1)
        self.assertEqual(dashboard_entry["preflight"]["issues"][0]["widget_id"], "widget-1")

    def test_detailed_report_includes_upload_summary(self):
        dr = DashboardResult(
            dashboard_id="dash-1",
            dashboard_title="Test",
            compiled=True,
            compiled_path="/tmp/compiled/dash-1",
            upload_attempted=True,
            uploaded=True,
            uploaded_space="shadow",
            uploaded_kibana_url="https://kibana.example/s/shadow",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"
            save_detailed_report([dr], str(output_path))
            payload = json.loads(output_path.read_text())

        dashboard_entry = payload["dashboards"][0]
        self.assertEqual(dashboard_entry["compiled_path"], "/tmp/compiled/dash-1")
        self.assertTrue(dashboard_entry["upload"]["attempted"])
        self.assertTrue(dashboard_entry["upload"]["uploaded"])
        self.assertEqual(dashboard_entry["upload"]["space"], "shadow")
        self.assertEqual(payload["summary"]["upload_attempted"], 1)
        self.assertEqual(payload["summary"]["uploaded"], 1)

    def test_detailed_report_includes_validation_section(self):
        dr = DashboardResult(
            dashboard_id="dash-1",
            dashboard_title="Test",
            validation_summary={"pass": 1, "fail": 1},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"
            save_detailed_report(
                [dr],
                str(output_path),
                validation_summary={"counts": {"pass": 1, "fail": 1}},
                validation_records=[{"dashboard": "Test", "status": "fail"}],
            )
            payload = json.loads(output_path.read_text())

        self.assertIn("validation", payload)
        self.assertEqual(payload["validation"]["summary"]["counts"]["fail"], 1)
        self.assertEqual(payload["dashboards"][0]["validation"]["fail"], 1)

    def test_parse_args_accepts_upload_options(self):
        args = datadog_cli.parse_args(
            [
                "--upload",
                "--kibana-url", "https://kibana.example",
                "--kibana-api-key", "secret",
                "--space-id", "shadow",
            ]
        )
        self.assertTrue(args.upload)
        self.assertEqual(args.kibana_url, "https://kibana.example")
        self.assertEqual(args.kibana_api_key, "secret")
        self.assertEqual(args.space_id, "shadow")

    def test_parse_args_accepts_validate_option(self):
        args = datadog_cli.parse_args(["--validate"])
        self.assertTrue(args.validate)

    def test_parse_args_accepts_smoke_options(self):
        args = datadog_cli.parse_args(
            [
                "--smoke",
                "--browser-audit",
                "--capture-screenshots",
                "--smoke-output", "smoke.json",
                "--smoke-timeout", "45",
                "--chrome-binary", "/usr/bin/chrome",
            ]
        )
        self.assertTrue(args.smoke)
        self.assertTrue(args.browser_audit)
        self.assertTrue(args.capture_screenshots)
        self.assertEqual(args.smoke_output, "smoke.json")
        self.assertEqual(args.smoke_timeout, 45)
        self.assertEqual(args.chrome_binary, "/usr/bin/chrome")

    def test_upload_with_es_url_does_not_auto_enable_validate(self):
        field_map = datadog_cli.load_profile("otel")
        mock_target_adapter = mock.Mock()

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            datadog_cli,
            "load_credentials_from_env",
            return_value={},
        ), patch.object(
            datadog_cli,
            "load_profile",
            return_value=field_map,
        ), patch.object(
            datadog_cli,
            "_load_live_field_capabilities",
        ), patch.object(
            datadog_cli.target_registry,
            "get",
            return_value=mock.Mock(return_value=mock_target_adapter),
        ), patch.object(
            datadog_cli,
            "_run_dashboard_pipeline",
            return_value={
                "total": 0,
                "artifacts_dir": str(Path(tmpdir) / "dashboards"),
            },
        ) as mock_run_dashboard_pipeline, patch.object(
            datadog_cli,
            "_write_run_summary",
        ):
            datadog_cli.main(
                [
                    "--source",
                    "files",
                    "--input-dir",
                    tmpdir,
                    "--output-dir",
                    tmpdir,
                    "--upload",
                    "--es-url",
                    "https://example.es",
                    "--kibana-url",
                    "https://kibana.example",
                    "--kibana-api-key",
                    "secret",
                ]
            )

        self.assertFalse(mock_run_dashboard_pipeline.call_args.kwargs["args"].validate)

    def test_dashboard_preflight_only_runs_when_explicitly_requested(self):
        field_map = datadog_cli.load_profile("otel")
        field_map.metric_field_caps = {"system_cpu_user": FieldCapability(name="system_cpu_user", type="double")}
        dashboard = NormalizedDashboard(id="d1", title="Dash", widgets=[])

        with patch.object(datadog_cli, "run_preflight", side_effect=AssertionError("preflight should not run")):
            result = datadog_cli._run_dashboard_preflight(
                dashboard,
                field_map,
                argparse.Namespace(preflight=False),
            )

        self.assertIsNone(result)

    def test_monitor_payload_preflight_skipped_without_preflight_flag(self):
        args = argparse.Namespace(
            preflight=False,
            kibana_url="https://kibana.example",
            kibana_api_key="secret",
            space_id="shadow",
        )
        mapping_batch = {
            "results": [
                {
                    "alert_id": "monitor-1",
                    "mapping": {
                        "rule_payload": {
                            "rule_type_id": ".es-query",
                            "params": {"esqlQuery": {"esql": "FROM metrics-*"}},
                        }
                    },
                }
            ]
        }

        with patch.object(
            datadog_alert_pipeline,
            "run_alerting_preflight",
            side_effect=AssertionError("preflight should not run"),
        ), patch.object(
            datadog_alert_pipeline,
            "validate_rule_payload",
            side_effect=AssertionError("payload validation requires preflight"),
        ):
            lookup, preflight = datadog_alert_pipeline.build_payload_validation_lookup(
                args,
                mapping_batch,
            )

        self.assertIsNone(preflight)
        self.assertEqual(lookup, {})

    def test_dashboard_pipeline_clears_stale_yaml_before_writing_current_run(self):
        field_map = datadog_cli.load_profile("otel")
        dashboard = NormalizedDashboard(id="current-id", title="Current Dashboard", widgets=[])
        args = argparse.Namespace(
            validate=False,
            es_url="",
            upload=False,
            ensure_data_views=False,
            smoke=False,
            space_id="",
            smoke_output="",
            preflight=False,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            yaml_dir = output_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "stale-from-other-run.yaml").write_text(
                "dashboard: stale\n",
                encoding="utf-8",
            )

            with patch.object(
                datadog_cli,
                "_extract",
                return_value=[{"id": "current-id", "title": "Current Dashboard"}],
            ), patch.object(
                datadog_cli,
                "normalize_dashboard",
                return_value=dashboard,
            ), patch.object(
                datadog_cli,
                "generate_dashboard_yaml",
                return_value="dashboard: current\n",
            ), patch.object(
                datadog_cli,
                "annotate_results_with_verification",
                return_value={},
            ), patch.object(
                datadog_cli,
                "print_report",
            ), patch.object(
                datadog_cli,
                "save_detailed_report",
            ), patch.object(
                datadog_cli,
                "save_migration_manifest",
            ), patch.object(
                datadog_cli,
                "save_verification_packets",
            ), patch.object(
                datadog_cli,
                "build_rollout_plan",
                return_value={},
            ), patch.object(
                datadog_cli,
                "save_rollout_plan",
            ), patch.object(
                datadog_cli,
                "generate_review_queue",
                return_value=[],
            ):
                datadog_cli._run_dashboard_pipeline(
                    args=args,
                    field_map=field_map,
                    output_dir=output_dir,
                    dd_creds={},
                    target_adapter=mock.Mock(),
                    compile_requested=False,
                )

            yaml_names = sorted(path.name for path in yaml_dir.glob("*.yaml"))

        self.assertEqual(yaml_names, ["current_dashboard.yaml"])

    def test_allocate_yaml_stem_avoids_case_collision(self):
        used_stems: set[str] = set()
        first = datadog_cli._allocate_yaml_stem("Test", "dash-001", used_stems)
        second = datadog_cli._allocate_yaml_stem("test", "dash-002", used_stems)
        self.assertEqual(first, "test")
        self.assertEqual(second, "test_dash-002")
        self.assertNotEqual(first, second)

    def test_allocate_yaml_stem_uses_numeric_suffix_without_id(self):
        used_stems: set[str] = {"test"}
        stem = datadog_cli._allocate_yaml_stem("Test", None, used_stems)
        self.assertEqual(stem, "test_2")

    @patch("observability_migration.targets.kibana.adapter.KibanaTargetAdapter.upload_dashboard")
    def test_upload_all_dashboards_skips_compile_failures(self, mock_upload_dashboard):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            yaml_path = output_dir / "yaml" / "dash.yaml"
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_path.write_text("dashboards: []", encoding="utf-8")

            dr = DashboardResult(
                dashboard_title="Dash",
                yaml_path=str(yaml_path),
                compiled=False,
                compile_error="compile failed",
            )

            datadog_cli._upload_all_dashboards(
                [dr],
                output_dir,
                type(
                    "Args",
                    (),
                    {
                        "kibana_url": "https://kibana.example",
                        "kibana_api_key": "secret",
                        "space_id": "shadow",
                    },
                )(),
                KibanaTargetAdapter(),
            )

            self.assertTrue(dr.upload_attempted)
            self.assertFalse(dr.uploaded)
            self.assertIn("compile failed", dr.upload_error)
            mock_upload_dashboard.assert_not_called()

    def test_compile_all_dashboards_layout_validates_each_successful_dashboard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            yaml_dir = output_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            bad_yaml = yaml_dir / "bad.yaml"
            good_yaml = yaml_dir / "good.yaml"
            bad_yaml.write_text("dashboards: []", encoding="utf-8")
            good_yaml.write_text("dashboards: []", encoding="utf-8")
            bad = DashboardResult(dashboard_title="Bad", yaml_path=str(bad_yaml))
            good = DashboardResult(dashboard_title="Good", yaml_path=str(good_yaml))
            target_adapter = mock.Mock()
            target_adapter.compile_dashboard.side_effect = [
                (False, "bad compile failed"),
                (True, "good compiled"),
            ]

            with patch.object(
                datadog_cli,
                "validate_compiled_layout",
                return_value=(True, "layout ok"),
            ) as mock_layout:
                datadog_cli._compile_all_dashboards(
                    [bad, good],
                    output_dir,
                    target_adapter,
                )

            self.assertFalse(bad.compiled)
            self.assertEqual(bad.compile_error, "bad compile failed")
            self.assertFalse(bad.layout_checked)
            self.assertTrue(good.compiled)
            self.assertTrue(good.layout_checked)
            self.assertEqual(good.layout_error, "")
            mock_layout.assert_called_once_with(output_dir / "compiled" / "good")

    @patch("observability_migration.targets.kibana.adapter.KibanaTargetAdapter.upload_dashboard")
    def test_upload_all_dashboards_skips_layout_failures(self, mock_upload_dashboard):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            yaml_path = output_dir / "yaml" / "dash.yaml"
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_path.write_text("dashboards: []", encoding="utf-8")

            dr = DashboardResult(
                dashboard_title="Dash",
                yaml_path=str(yaml_path),
                compiled=True,
                layout_checked=True,
                layout_error="1 overlap(s), 0 invalid size(s), 0 out-of-bounds panel(s)",
            )

            datadog_cli._upload_all_dashboards(
                [dr],
                output_dir,
                type(
                    "Args",
                    (),
                    {
                        "kibana_url": "https://kibana.example",
                        "kibana_api_key": "secret",
                        "space_id": "shadow",
                    },
                )(),
                KibanaTargetAdapter(),
            )

            self.assertTrue(dr.upload_attempted)
            self.assertFalse(dr.uploaded)
            self.assertIn("layout validation failed", dr.upload_error)
            mock_upload_dashboard.assert_not_called()

    def test_smoke_uploaded_dashboards_updates_dashboard_and_panel_runtime_state(self):
        panel_fail = TranslationResult(
            widget_id="w1",
            source_panel_id="w1",
            title="CPU",
            dd_widget_type="timeseries",
            kibana_type="xy",
            status="ok",
            backend="esql",
            esql_query="FROM metrics-* | STATS value = AVG(system_cpu_user) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
            query_language="datadog_metric",
            source_queries=["avg:system.cpu.user{*}"],
        )
        panel_empty = TranslationResult(
            widget_id="w2",
            source_panel_id="w2",
            title="Memory",
            dd_widget_type="timeseries",
            kibana_type="xy",
            status="ok",
            backend="esql",
            esql_query="FROM metrics-* | STATS value = AVG(system_memory_used) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
            query_language="datadog_metric",
            source_queries=["avg:system.mem.used{*}"],
        )
        dr = DashboardResult(
            dashboard_id="d1",
            dashboard_title="Dash",
            uploaded=True,
            panel_results=[panel_fail, panel_empty],
        )
        smoke_payload = {
            "summary": {
                "runtime_error_panels": 1,
                "empty_panels": 1,
                "not_runtime_checked_panels": 0,
                "dashboards_with_layout_issues": 0,
                "dashboards_with_browser_errors": 1,
            },
            "dashboards": [
                {
                    "id": "kibana-1",
                    "title": "Dash",
                    "status": "has_runtime_errors",
                    "failing_panels": [{"panel": "CPU", "status": "fail"}],
                    "empty_panels": [{"panel": "Memory", "status": "empty"}],
                    "not_runtime_checked_panels": [],
                    "layout": {"overlaps": [], "invalid_sizes": [], "out_of_bounds": []},
                    "browser_audit": {"status": "error", "issues": ["Error loading data"]},
                    "panels": [
                        {"panel": "CPU", "status": "fail"},
                        {"panel": "Memory", "status": "empty"},
                    ],
                }
            ],
        }
        target_adapter = mock.Mock()
        target_adapter.smoke.return_value = smoke_payload

        with tempfile.TemporaryDirectory() as tmpdir:
            payload = datadog_cli._smoke_uploaded_dashboards(
                [dr],
                Path(tmpdir),
                type(
                    "Args",
                    (),
                    {
                        "kibana_url": "https://kibana.example",
                        "kibana_api_key": "secret",
                        "es_url": "https://example.es",
                        "es_api_key": "secret",
                        "space_id": "shadow",
                        "smoke_output": "",
                        "smoke_timeout": 30,
                        "browser_audit": True,
                        "capture_screenshots": True,
                        "chrome_binary": "",
                    },
                )(),
                target_adapter,
            )

        self.assertEqual(payload["summary"]["runtime_error_panels"], 1)
        smoke_kwargs = target_adapter.smoke.call_args.kwargs
        self.assertEqual(
            smoke_kwargs["browser_audit_dir"],
            str(Path(tmpdir) / "browser_qa"),
        )
        self.assertEqual(
            smoke_kwargs["screenshot_dir"],
            str(Path(tmpdir) / "dashboard_qa"),
        )
        self.assertTrue(dr.smoke_attempted)
        self.assertEqual(dr.smoke_status, "fail")
        self.assertEqual(dr.browser_audit_status, "fail")
        self.assertEqual(dr.kibana_saved_object_id, "kibana-1")
        self.assertIn("smoke_failed", panel_fail.runtime_rollups)
        self.assertIn("empty_result", panel_empty.runtime_rollups)
        self.assertIn("browser_failed", panel_fail.runtime_rollups)
        self.assertIn("browser_failed", panel_empty.runtime_rollups)

    def test_apply_smoke_dashboard_state_describes_lens_panels_left_by_design(self):
        dr = DashboardResult(
            dashboard_id="d1",
            dashboard_title="Dash",
            uploaded=True,
            panel_results=[],
        )

        datadog_cli._apply_smoke_dashboard_state(
            dr,
            {
                "id": "kibana-1",
                "title": "Dash",
                "status": "has_runtime_gaps",
                "not_runtime_checked_panels": [
                    {"panel": "CPU", "status": "not_runtime_checked", "coverage_reason": "lens_by_design"}
                ],
                "lens_by_design_panels": [
                    {"panel": "CPU", "status": "not_runtime_checked", "coverage_reason": "lens_by_design"}
                ],
                "unexpected_runtime_gap_panels": [],
                "layout": {"overlaps": [], "invalid_sizes": [], "out_of_bounds": []},
                "browser_audit": {"status": "not_requested"},
            },
            output_path=Path("/tmp/report.json"),
            browser_requested=False,
        )

        self.assertEqual(dr.smoke_status, "not_runtime_checked")
        self.assertIn("still Lens by design", dr.smoke_error)

    def test_annotate_results_with_verification_builds_semantic_gates(self):
        clean = TranslationResult(
            widget_id="w1",
            source_panel_id="w1",
            title="CPU",
            dd_widget_type="timeseries",
            kibana_type="xy",
            status="ok",
            backend="esql",
            esql_query="FROM metrics-* | STATS value = AVG(system_cpu_user) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
            query_language="datadog_metric",
            source_queries=["avg:system.cpu.user{*}"],
        )
        broken = TranslationResult(
            widget_id="w2",
            source_panel_id="w2",
            title="Errors",
            dd_widget_type="timeseries",
            kibana_type="xy",
            status="requires_manual",
            backend="esql",
            esql_query="FROM logs-* | STATS count = COUNT(*) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
            query_language="datadog_log",
            source_queries=["status:error"],
        )
        dr = DashboardResult(
            dashboard_id="d1",
            dashboard_title="Dash",
            source_file="dash.json",
            compiled=True,
            uploaded=True,
            panel_results=[clean, broken],
        )
        payload = annotate_results_with_verification(
            [dr],
            validation_records=[
                {
                    "dashboard": "Dash",
                    "dashboard_id": "d1",
                    "widget": "CPU",
                    "widget_id": "w1",
                    "status": "pass",
                    "query": clean.esql_query,
                    "error": "",
                    "fix_attempts": [],
                    "analysis": {"result_rows": 10, "result_columns": ["time_bucket", "value"]},
                },
                {
                    "dashboard": "Dash",
                    "dashboard_id": "d1",
                    "widget": "Errors",
                    "widget_id": "w2",
                    "status": "fail",
                    "query": broken.esql_query,
                    "error": "Unknown column [count]",
                    "fix_attempts": [],
                    "analysis": {},
                },
            ],
        )

        self.assertEqual(payload["summary"]["green"], 1)
        self.assertEqual(payload["summary"]["red"], 1)
        self.assertEqual(clean.verification_packet["semantic_gate"], "Green")
        self.assertEqual(broken.verification_packet["semantic_gate"], "Red")
        self.assertTrue(clean.recommended_target)
        self.assertEqual(clean.operational_ir.review.semantic_gate, "Green")

    @patch("observability_migration.adapters.source.datadog.execution.requests.get")
    def test_datadog_metric_source_execution_normalizes_scalar_widget(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "series": [
                {
                    "metric": "system.cpu.user",
                    "scope": "host:web01",
                    "pointlist": [[1710000000, 40.0], [1710000060, 42.0]],
                }
            ],
        }
        panel = TranslationResult(
            widget_id="w1",
            source_panel_id="w1",
            title="CPU",
            kibana_type="metric",
            query_language="datadog_metric",
            source_queries=["avg:system.cpu.user{*}"],
        )

        summary = build_source_execution_summary(panel, api_key="api", app_key="app")

        self.assertEqual(summary.status, "pass")
        self.assertEqual(summary.result_summary["rows"], 1)
        self.assertEqual(summary.result_summary["values"], [[42.0]])
        self.assertEqual(summary.result_summary["metadata"]["latest_value"], 42.0)

    @patch("observability_migration.adapters.source.datadog.execution.requests.get")
    def test_annotate_results_with_verification_uses_source_target_comparison(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "ok",
            "series": [
                {
                    "metric": "system.cpu.user",
                    "scope": "*",
                    "pointlist": [[1710000000, 42.0]],
                }
            ],
        }
        panel = TranslationResult(
            widget_id="w1",
            source_panel_id="w1",
            title="CPU",
            dd_widget_type="query_value",
            kibana_type="metric",
            status="ok",
            backend="esql",
            esql_query="FROM metrics-* | STATS value = AVG(system_cpu_user)",
            query_language="datadog_metric",
            source_queries=["avg:system.cpu.user{*}"],
        )
        dashboard = DashboardResult(
            dashboard_id="d1",
            dashboard_title="Dash",
            source_file="dash.json",
            compiled=True,
            uploaded=True,
            panel_results=[panel],
        )

        payload = annotate_results_with_verification(
            [dashboard],
            validation_records=[
                {
                    "dashboard": "Dash",
                    "dashboard_id": "d1",
                    "widget": "CPU",
                    "widget_id": "w1",
                    "status": "pass",
                    "query": panel.esql_query,
                    "error": "",
                    "fix_attempts": [],
                    "analysis": {
                        "result_rows": 1,
                        "result_columns": ["value"],
                        "result_values": [[42.0]],
                        "result_metadata": {},
                    },
                }
            ],
            datadog_api_key="api",
            datadog_app_key="app",
        )

        self.assertEqual(payload["summary"]["green"], 1)
        self.assertEqual(panel.verification_packet["verification_mode"], "source_target_comparison")
        self.assertEqual(panel.verification_packet["comparison"]["status"], "within_tolerance")

    def _run_main_capturing_verification_creds(self, extra_argv, env_creds):
        """Run the Datadog CLI on the sample dashboard and capture the
        Datadog API credentials handed to the verification step.

        Returns the kwargs dict that ``annotate_results_with_verification``
        was called with so callers can assert whether live source-side
        execution would have been attempted.
        """
        sample = (
            Path(__file__).parent.parent
            / "infra"
            / "datadog"
            / "dashboards"
            / "sample_dashboard.json"
        )
        captured: dict[str, Any] = {}

        def _fake_annotate(results, validation_records=None, **kwargs):
            captured.update(kwargs)
            return {"summary": {"green": 0, "yellow": 0, "red": 0}, "packets": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            input_dir.mkdir()
            shutil.copy(sample, input_dir / "sample_dashboard.json")
            output_dir = Path(tmpdir) / "out"

            with patch.dict(os.environ, env_creds, clear=False), patch.object(
                datadog_cli,
                "annotate_results_with_verification",
                side_effect=_fake_annotate,
            ), patch(
                "observability_migration.adapters.source.datadog.execution.requests.get",
                side_effect=AssertionError(
                    "offline migration must not call the Datadog API"
                ),
            ):
                datadog_cli.main(
                    [
                        "--source",
                        "files",
                        "--input-dir",
                        str(input_dir),
                        "--output-dir",
                        str(output_dir),
                        "--assets",
                        "dashboards",
                        "--env-file",
                        "/dev/null",
                        *extra_argv,
                    ]
                )
        return captured

    def test_offline_migration_does_not_use_datadog_api_when_creds_in_env(self):
        # DD_API_KEY/DD_APP_KEY present in the environment must NOT trigger
        # blocking live Datadog API calls during plain offline translation.
        captured = self._run_main_capturing_verification_creds(
            extra_argv=[],
            env_creds={"DD_API_KEY": "envkey", "DD_APP_KEY": "envapp"},
        )
        self.assertEqual(captured.get("datadog_api_key"), "")
        self.assertEqual(captured.get("datadog_app_key"), "")

    def test_source_execution_flag_opts_into_datadog_api_creds(self):
        # With --source-execution the live Datadog credentials are forwarded
        # to the verification step (the network call is mocked out here).
        captured = self._run_main_capturing_verification_creds(
            extra_argv=["--source-execution"],
            env_creds={"DD_API_KEY": "envkey", "DD_APP_KEY": "envapp"},
        )
        self.assertEqual(captured.get("datadog_api_key"), "envkey")
        self.assertEqual(captured.get("datadog_app_key"), "envapp")

    def test_parse_args_source_execution_defaults_off(self):
        self.assertFalse(datadog_cli.parse_args([]).source_execution)
        self.assertTrue(datadog_cli.parse_args(["--source-execution"]).source_execution)

    def test_datadog_manifest_and_rollout_capture_artifacts(self):
        panel = TranslationResult(
            widget_id="w1",
            source_panel_id="w1",
            title="CPU",
            status="ok",
            verification_packet={"semantic_gate": "Yellow"},
        )
        dashboard = DashboardResult(
            dashboard_id="dash-1",
            dashboard_title="Infra",
            source_file="infra.json",
            yaml_path="yaml/infra.yaml",
            compiled_path="compiled/infra",
            uploaded=True,
            uploaded_space="shadow",
            kibana_saved_object_id="kb-1",
            verification_summary={"green": 0, "yellow": 1, "red": 0},
            panel_results=[panel],
        )

        manifest = build_migration_manifest([dashboard])
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "yaml").mkdir()
            (base / "compiled" / "infra").mkdir(parents=True)
            (base / "yaml" / "infra.yaml").write_text("dashboards: []", encoding="utf-8")
            (base / "compiled" / "infra" / "compiled_dashboards.ndjson").write_text("{}", encoding="utf-8")
            rollout = build_rollout_plan(
                [dashboard],
                target_space="prod",
                shadow_space="shadow",
                output_dir=str(base),
                smoke_report_path=str(base / "smoke.json"),
            )

        self.assertEqual(manifest["summary"]["dashboards"], 1)
        self.assertEqual(manifest["dashboards"][0]["kibana_saved_object_id"], "kb-1")
        self.assertEqual(rollout.dashboards[0].rollout_state, "shadow_imported")
        self.assertEqual(rollout.dashboards[0].kibana_saved_object_id, "kb-1")

    def test_detailed_report_includes_smoke_and_verification_sections(self):
        dr = DashboardResult(
            dashboard_id="dash-1",
            dashboard_title="Test",
            smoke_attempted=True,
            smoke_status="fail",
            smoke_error="1 panel runtime error(s)",
            smoke_report_path="/tmp/smoke.json",
            verification_summary={"green": 0, "yellow": 1, "red": 1},
        )
        panel = TranslationResult(
            widget_id="w1",
            title="CPU",
            verification_packet={"semantic_gate": "Red"},
        )
        dr.panel_results = [panel]
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"
            save_detailed_report(
                [dr],
                str(output_path),
                smoke_payload={"summary": {"runtime_error_panels": 1}},
                verification_payload={"summary": {"red": 1}, "packets": [{"panel": "CPU"}]},
            )
            payload = json.loads(output_path.read_text())

        self.assertIn("smoke", payload)
        self.assertIn("verification", payload)
        self.assertEqual(payload["dashboards"][0]["smoke"]["status"], "fail")
        self.assertEqual(payload["dashboards"][0]["verification_summary"]["red"], 1)

    @patch("observability_migration.adapters.source.datadog.cli.validate_query_with_fixes")
    def test_validate_all_dashboards_manualizes_failed_queries_and_rewrites_yaml(self, mock_validate):
        field_map = load_profile("otel")
        widget = NormalizedWidget(
            id="w1",
            widget_type="timeseries",
            title="CPU",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query="avg:system.cpu.user{*}",
                    metric_query=parse_metric_query("avg:system.cpu.user{*}"),
                    query_type="metric",
                )
            ],
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )
        dashboard = NormalizedDashboard(id="d1", title="Dash", widgets=[widget])
        plan = plan_widget(widget)
        if plan.backend == "lens":
            plan.backend = "esql"
        panel_result = translate_widget(widget, plan, field_map)
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            yaml_dir = output_dir / "yaml"
            yaml_dir.mkdir(parents=True, exist_ok=True)
            yaml_path = yaml_dir / "dash.yaml"
            yaml_path.write_text(
                generate_dashboard_yaml(
                    dashboard,
                    [panel_result],
                    data_view=field_map.metric_index,
                    metrics_dataset_filter=field_map.metrics_dataset_filter,
                    logs_dataset_filter=field_map.logs_dataset_filter,
                    logs_index=field_map.logs_index,
                    field_map=field_map,
                ),
                encoding="utf-8",
            )
            dr = DashboardResult(
                dashboard_id="d1",
                dashboard_title="Dash",
                yaml_path=str(yaml_path),
                panel_results=[panel_result],
            )
            mock_validate.return_value = {
                "status": "fail",
                "query": panel_result.esql_query,
                "error": "Unknown column [system_cpu_user]",
                "analysis": {"raw_error": "Unknown column [system_cpu_user]"},
                "fix_attempts": [],
            }

            records, summary = datadog_cli._validate_all_dashboards(
                [(dr, dashboard)],
                field_map,
                argparse.Namespace(es_url="https://example.es", es_api_key="secret"),
            )

            payload = yaml.safe_load(yaml_path.read_text())

        self.assertEqual(records[0]["status"], "fail")
        self.assertEqual(summary["counts"]["fail"], 1)
        self.assertEqual(dr.panel_results[0].status, "requires_manual")
        first_panel = payload["dashboards"][0]["panels"][0]
        self.assertIn("markdown", first_panel)
        self.assertNotIn("esql", first_panel)

    @patch("observability_migration.adapters.source.datadog.cli.validate_query_with_fixes")
    def test_validate_all_dashboards_rewrites_fixed_query(self, mock_validate):
        field_map = load_profile("otel")
        widget = NormalizedWidget(
            id="w1",
            widget_type="timeseries",
            title="CPU",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query="avg:system.cpu.user{*}",
                    metric_query=parse_metric_query("avg:system.cpu.user{*}"),
                    query_type="metric",
                )
            ],
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )
        dashboard = NormalizedDashboard(id="d1", title="Dash", widgets=[widget])
        plan = plan_widget(widget)
        if plan.backend == "lens":
            plan.backend = "esql"
        panel_result = translate_widget(widget, plan, field_map)
        fixed_query = "FROM metrics-* | STATS AVG(system_cpu_user) | LIMIT 10"
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            yaml_dir = output_dir / "yaml"
            yaml_dir.mkdir(parents=True, exist_ok=True)
            yaml_path = yaml_dir / "dash.yaml"
            yaml_path.write_text(
                generate_dashboard_yaml(
                    dashboard,
                    [panel_result],
                    data_view=field_map.metric_index,
                    metrics_dataset_filter=field_map.metrics_dataset_filter,
                    logs_dataset_filter=field_map.logs_dataset_filter,
                    logs_index=field_map.logs_index,
                    field_map=field_map,
                ),
                encoding="utf-8",
            )
            dr = DashboardResult(
                dashboard_id="d1",
                dashboard_title="Dash",
                yaml_path=str(yaml_path),
                panel_results=[panel_result],
            )
            mock_validate.return_value = {
                "status": "fixed",
                "query": fixed_query,
                "error": "",
                "analysis": {"result_rows": 1},
                "fix_attempts": ["Unknown column [foo]"],
            }

            records, summary = datadog_cli._validate_all_dashboards(
                [(dr, dashboard)],
                field_map,
                argparse.Namespace(es_url="https://example.es", es_api_key="secret"),
            )

            payload = yaml.safe_load(yaml_path.read_text())

        self.assertEqual(records[0]["status"], "fixed")
        self.assertEqual(summary["counts"]["fixed"], 1)
        self.assertEqual(dr.panel_results[0].esql_query, fixed_query)
        first_panel = payload["dashboards"][0]["panels"][0]
        self.assertEqual(first_panel["esql"]["query"], fixed_query)

    @patch("observability_migration.adapters.source.datadog.cli.validate_query_with_fixes")
    def test_validate_all_dashboards_reuses_resolver_per_index_pattern(self, mock_validate):
        field_map = load_profile("otel")
        first = TranslationResult(
            widget_id="w1",
            title="CPU 1",
            status="ok",
            esql_query="FROM metrics-* | STATS value = AVG(system_cpu_user)",
        )
        second = TranslationResult(
            widget_id="w2",
            title="CPU 2",
            status="ok",
            esql_query="FROM metrics-* | STATS value = MAX(system_cpu_user)",
        )
        dashboard = DashboardResult(
            dashboard_id="d1",
            dashboard_title="Dash",
            panel_results=[first, second],
        )
        mock_validate.return_value = {
            "status": "pass",
            "query": first.esql_query,
            "error": "",
            "analysis": {"result_rows": 1},
            "fix_attempts": [],
        }

        original_resolver = datadog_cli._DatadogValidationResolver
        with mock.patch.object(
            datadog_cli,
            "_DatadogValidationResolver",
            wraps=original_resolver,
        ) as mock_resolver:
            datadog_cli._validate_all_dashboards(
                [(dashboard, object())],
                field_map,
                argparse.Namespace(es_url="https://example.es", es_api_key="secret"),
            )

        self.assertEqual(mock_validate.call_count, 2)
        self.assertEqual(mock_resolver.call_count, 1)


class TestDashboardDatasetFilters(unittest.TestCase):
    """Verify data_stream.dataset filters are emitted in generated YAML."""

    def _make_metric_widget(self, wid, title, query_str="avg:system.cpu.user{*}"):
        mq = parse_metric_query(query_str)
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query=query_str, metric_query=mq, query_type="metric")
        return NormalizedWidget(id=wid, widget_type="timeseries", title=title, queries=[wq],
                                layout={"x": 0, "y": 0, "width": 4, "height": 2})

    def _make_log_widget(self, wid, title):
        lq = parse_log_query("status:error")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="status:error", log_query=lq, query_type="log")
        return NormalizedWidget(id=wid, widget_type="log_stream", title=title, queries=[wq],
                                layout={"x": 0, "y": 0, "width": 4, "height": 2})

    def _translate_and_generate(self, widgets, profile=None, **generate_kwargs):
        fm = profile or OTEL_PROFILE
        dash = NormalizedDashboard(id="1", title="Dash", widgets=widgets)
        results = [translate_widget(w, plan_widget(w), fm) for w in widgets]
        yaml_str = generate_dashboard_yaml(
            dash, results, data_view=fm.metric_index,
            metrics_dataset_filter=generate_kwargs.get("metrics_dataset_filter", fm.metrics_dataset_filter),
            logs_dataset_filter=generate_kwargs.get("logs_dataset_filter", fm.logs_dataset_filter),
            logs_index=fm.logs_index,
        )
        return yaml.safe_load(yaml_str)["dashboards"][0]

    def test_otel_profile_metrics_no_filter_by_default(self):
        """OTEL_PROFILE uses metrics-* so dataset is indeterminate → no filter."""
        widget = self._make_metric_widget("1", "CPU")
        rendered = self._translate_and_generate([widget])
        self.assertNotIn("filters", rendered)

    def test_explicit_metrics_dataset_filter_emits_filter(self):
        widget = self._make_metric_widget("1", "CPU")
        rendered = self._translate_and_generate(
            [widget], metrics_dataset_filter="otel",
        )
        self.assertIn("filters", rendered)
        self.assertEqual(
            rendered["filters"],
            [{"field": "data_stream.dataset", "equals": "otel"}],
        )

    def test_prometheus_profile_derives_dataset_from_index(self):
        widget = self._make_metric_widget("1", "CPU")
        rendered = self._translate_and_generate([widget], profile=PROMETHEUS_PROFILE)
        self.assertIn("filters", rendered)
        self.assertEqual(rendered["filters"][0]["equals"], "prometheus")

    def test_logs_dataset_filter_applied_for_log_only_dashboard(self):
        widget = self._make_log_widget("1", "Errors")
        rendered = self._translate_and_generate(
            [widget], logs_dataset_filter="generic",
        )
        self.assertIn("filters", rendered)
        self.assertEqual(rendered["filters"][0]["equals"], "generic")

    def test_mixed_metric_log_dashboard_gets_no_filter(self):
        metric_w = self._make_metric_widget("1", "CPU")
        log_w = self._make_log_widget("2", "Errors")
        rendered = self._translate_and_generate(
            [metric_w, log_w],
            metrics_dataset_filter="otel",
            logs_dataset_filter="generic",
        )
        self.assertNotIn("filters", rendered)


class TestDeriveDatasetFromIndex(unittest.TestCase):
    def test_three_part_pattern(self):
        from observability_migration.adapters.source.datadog.field_map import derive_dataset_from_index
        self.assertEqual(derive_dataset_from_index("metrics-prometheus-default"), "prometheus")
        self.assertEqual(derive_dataset_from_index("metrics-otel-default"), "otel")
        self.assertEqual(derive_dataset_from_index("logs-generic-default"), "generic")

    def test_wildcard_dataset_returns_empty(self):
        from observability_migration.adapters.source.datadog.field_map import derive_dataset_from_index
        self.assertEqual(derive_dataset_from_index("metrics-*"), "")
        self.assertEqual(derive_dataset_from_index("logs-*"), "")

    def test_two_part_pattern_returns_empty(self):
        from observability_migration.adapters.source.datadog.field_map import derive_dataset_from_index
        self.assertEqual(derive_dataset_from_index("custom-index"), "")

    def test_three_part_with_wildcard_namespace(self):
        from observability_migration.adapters.source.datadog.field_map import derive_dataset_from_index
        self.assertEqual(derive_dataset_from_index("metrics-prometheus-*"), "prometheus")


class TestMultiQueryMetricExecution(unittest.TestCase):
    """Verify multi-query metric source execution merges results."""

    @patch("observability_migration.adapters.source.datadog.execution.requests.get")
    def test_multi_query_merges_passing_results(self, mock_get):
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "ok",
            "series": [
                {"metric": "system.cpu.user", "scope": "host:a", "pointlist": [[1000, 42.0]]},
            ],
        }
        mock_get.return_value = mock_resp

        panel = TranslationResult(
            widget_id="w1", title="Multi", status="ok", kibana_type="xy",
            query_language="datadog_metric",
        )
        panel.source_queries = ["avg:system.cpu.user{host:a}", "avg:system.cpu.system{host:a}"]

        summary = build_source_execution_summary(panel, api_key="k", app_key="a", site="test.com")
        self.assertEqual(summary.status, "pass")
        self.assertEqual(mock_get.call_count, 2)
        self.assertIn("queries_executed", summary.result_summary.get("metadata", {}))
        self.assertEqual(summary.result_summary["metadata"]["queries_executed"], 2)
        self.assertEqual(summary.result_summary["metadata"]["queries_passed"], 2)

    @patch("observability_migration.adapters.source.datadog.execution.requests.get")
    def test_multi_query_partial_failure_still_passes(self, mock_get):
        success_resp = mock.Mock()
        success_resp.status_code = 200
        success_resp.json.return_value = {
            "status": "ok",
            "series": [{"metric": "m1", "scope": "*", "pointlist": [[1000, 10.0]]}],
        }
        fail_resp = mock.Mock()
        fail_resp.status_code = 400
        fail_resp.json.return_value = {"errors": ["bad query"]}
        mock_get.side_effect = [success_resp, fail_resp]

        panel = TranslationResult(
            widget_id="w2", title="Partial", status="ok", kibana_type="xy",
            query_language="datadog_metric",
        )
        panel.source_queries = ["avg:m1{*}", "bad_query"]

        summary = build_source_execution_summary(panel, api_key="k", app_key="a", site="test.com")
        self.assertEqual(summary.status, "pass")
        self.assertIn("1/2 queries failed", summary.reason)

    @patch("observability_migration.adapters.source.datadog.execution.requests.get")
    def test_multi_query_all_fail_returns_fail(self, mock_get):
        fail_resp = mock.Mock()
        fail_resp.status_code = 500
        fail_resp.json.return_value = {"errors": ["server error"]}
        mock_get.return_value = fail_resp

        panel = TranslationResult(
            widget_id="w3", title="AllFail", status="ok", kibana_type="xy",
            query_language="datadog_metric",
        )
        panel.source_queries = ["avg:m1{*}", "avg:m2{*}"]

        summary = build_source_execution_summary(panel, api_key="k", app_key="a", site="test.com")
        self.assertEqual(summary.status, "fail")
        self.assertIn("All 2", summary.reason)


class TestLogSourceExecution(unittest.TestCase):
    """Verify Datadog log query source execution."""

    @patch("observability_migration.adapters.source.datadog.execution.requests.post")
    def test_log_query_execution_returns_event_rows(self, mock_post):
        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [
                {"attributes": {"timestamp": "2025-01-01T00:00:00Z", "service": "web", "status": "error", "message": "fail"}},
                {"attributes": {"timestamp": "2025-01-01T00:00:01Z", "service": "api", "status": "warn", "message": "slow"}},
            ],
            "meta": {"page": {}},
        }
        mock_post.return_value = mock_resp

        panel = TranslationResult(
            widget_id="l1", title="Logs", status="ok", kibana_type="table",
            query_language="datadog_log",
        )
        panel.source_queries = ["service:web status:error"]

        summary = build_source_execution_summary(panel, api_key="k", app_key="a", site="test.com")
        self.assertEqual(summary.status, "pass")
        self.assertEqual(summary.adapter, "datadog_logs_http")
        self.assertEqual(summary.result_summary["rows"], 2)
        self.assertIn("timestamp", summary.result_summary["columns"])

    def test_log_query_without_credentials_returns_not_configured(self):
        panel = TranslationResult(
            widget_id="l2", title="NoCreds", status="ok", kibana_type="table",
            query_language="datadog_log",
        )
        panel.source_queries = ["service:web"]

        summary = build_source_execution_summary(panel, api_key="", app_key="")
        self.assertEqual(summary.status, "not_configured")

    @patch("observability_migration.adapters.source.datadog.execution.requests.post")
    def test_log_query_failure_returns_fail(self, mock_post):
        mock_resp = mock.Mock()
        mock_resp.status_code = 403
        mock_resp.json.return_value = {"errors": ["forbidden"]}
        mock_post.return_value = mock_resp

        panel = TranslationResult(
            widget_id="l3", title="Forbidden", status="ok", kibana_type="table",
            query_language="datadog_log",
        )
        panel.source_queries = ["*"]

        summary = build_source_execution_summary(panel, api_key="k", app_key="a", site="test.com")
        self.assertEqual(summary.status, "fail")


class TestDatadogRolloutPromoteRollback(unittest.TestCase):
    """Verify promote/rollback lifecycle for Datadog rollout plans."""

    def _make_plan(self):
        from observability_migration.adapters.source.datadog.rollout import (
            DashboardLineage,
            RolloutPlan,
            promote_dashboard,
            rollback_dashboard,
        )
        plan = RolloutPlan(run_id="test", target_space="prod")
        lineage = DashboardLineage(
            source_dashboard_id="abc123",
            source_dashboard_title="Test Dashboard",
            panel_count=5,
            migrated_panels=4,
            rollout_state="report_only",
        )
        plan.dashboards.append(lineage)
        return plan, promote_dashboard, rollback_dashboard

    def test_promote_from_shadow_imported(self):
        plan, promote, _ = self._make_plan()
        plan.dashboards[0].transition("shadow_imported", reason="initial upload")
        self.assertTrue(promote(plan, "abc123", reason="approved"))
        self.assertEqual(plan.dashboards[0].rollout_state, "promoted")
        self.assertEqual(len(plan.dashboards[0].state_history), 2)

    def test_promote_from_report_only_fails(self):
        plan, promote, _ = self._make_plan()
        self.assertFalse(promote(plan, "abc123"))
        self.assertEqual(plan.dashboards[0].rollout_state, "report_only")

    def test_promote_unknown_dashboard_fails(self):
        plan, promote, _ = self._make_plan()
        self.assertFalse(promote(plan, "unknown"))

    def test_rollback_from_promoted(self):
        plan, promote, rollback = self._make_plan()
        plan.dashboards[0].transition("shadow_imported", reason="upload")
        promote(plan, "abc123")
        self.assertTrue(rollback(plan, "abc123", reason="issue found"))
        self.assertEqual(plan.dashboards[0].rollout_state, "rolled_back")
        self.assertEqual(len(plan.dashboards[0].state_history), 3)

    def test_rollback_from_report_only_fails(self):
        plan, _, rollback = self._make_plan()
        self.assertFalse(rollback(plan, "abc123"))

    def test_full_lifecycle(self):
        from observability_migration.adapters.source.datadog.rollout import (
            promote_dashboard,
            rollback_dashboard,
        )
        plan, _, _ = self._make_plan()
        d = plan.dashboards[0]
        d.transition("shadow_imported", reason="shadow deploy")
        d.transition("review_approved", reason="peer review")
        self.assertTrue(promote_dashboard(plan, "abc123", reason="go live"))
        self.assertEqual(d.rollout_state, "promoted")
        self.assertTrue(rollback_dashboard(plan, "abc123", reason="regression"))
        self.assertEqual(d.rollout_state, "rolled_back")
        self.assertEqual(len(d.state_history), 4)


class TestPreflightAssessFieldUsageIntegration(unittest.TestCase):
    """Verify that Datadog preflight check_field_compatibility exercises assess_field_usage."""

    def test_missing_field_produces_warning(self):
        from observability_migration.adapters.source.datadog.preflight import check_field_compatibility
        issues = check_field_compatibility(
            required_fields=[{"name": "nonexistent.field", "usage": "aggregate", "widget_id": "w1"}],
            field_caps={},
        )
        warnings = [i for i in issues if i.level == "warn"]
        self.assertTrue(len(warnings) >= 1)
        self.assertIn("not found", warnings[0].message)

    def test_non_aggregatable_field_blocks(self):
        from observability_migration.adapters.source.datadog.preflight import check_field_compatibility
        cap = FieldCapability(name="body.text", type="text")
        cap.aggregatable = False
        issues = check_field_compatibility(
            required_fields=[{"name": "body.text", "usage": "group_by", "widget_id": "w2"}],
            field_caps={"body.text": cap},
        )
        blockers = [i for i in issues if i.level == "block"]
        self.assertTrue(len(blockers) >= 1)
        self.assertIn("not aggregatable", blockers[0].message)

    def test_type_family_mismatch_blocks(self):
        from observability_migration.adapters.source.datadog.preflight import check_field_compatibility
        cap = FieldCapability(name="host.name", type="keyword")
        issues = check_field_compatibility(
            required_fields=[{"name": "host.name", "usage": "aggregate", "type_family": "numeric", "widget_id": "w3"}],
            field_caps={"host.name": cap},
        )
        blockers = [i for i in issues if i.level == "block"]
        self.assertTrue(len(blockers) >= 1)
        self.assertIn("requires 'numeric'", blockers[0].message)

    def test_healthy_numeric_field_no_issues(self):
        from observability_migration.adapters.source.datadog.preflight import check_field_compatibility
        cap = FieldCapability(name="cpu.pct", type="double")
        cap.aggregatable = True
        cap.searchable = True
        issues = check_field_compatibility(
            required_fields=[{"name": "cpu.pct", "usage": "aggregate", "type_family": "numeric"}],
            field_caps={"cpu.pct": cap},
        )
        self.assertEqual(len(issues), 0)

    def test_conflicting_types_produces_warning(self):
        from observability_migration.adapters.source.datadog.preflight import check_field_compatibility
        cap = FieldCapability(name="status", type="keyword", conflicting_types=["keyword", "long"])
        issues = check_field_compatibility(
            required_fields=[{"name": "status", "usage": "filter", "widget_id": "w4"}],
            field_caps={"status": cap},
        )
        warnings = [i for i in issues if i.level == "warn"]
        self.assertTrue(any("conflicting" in w.message for w in warnings))


class DatadogAssetIsolationTests(unittest.TestCase):
    def test_alerts_only_does_not_initialize_dashboard_target_adapter(self):
        field_map = load_profile("otel")
        alert_pipeline = ModuleType(
            "observability_migration.adapters.source.datadog.alert_pipeline"
        )
        alert_pipeline.run_alert_pipeline = mock.Mock(
            side_effect=RuntimeError("datadog-alert-pipeline-called")
        )

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            sys.modules,
            {
                "observability_migration.adapters.source.datadog.alert_pipeline": alert_pipeline,
            },
        ), patch.object(
            datadog_cli.target_registry,
            "get",
            side_effect=AssertionError(
                "dashboard target adapter should not be resolved for --assets alerts"
            ),
        ), patch.object(
            datadog_cli,
            "load_credentials_from_env",
            return_value={},
        ), patch.object(
            datadog_cli,
            "load_profile",
            return_value=field_map,
        ), patch.object(
            datadog_cli,
            "_load_live_field_capabilities",
        ):
            with self.assertRaisesRegex(RuntimeError, "datadog-alert-pipeline-called"):
                datadog_cli.main(
                    [
                        "--assets",
                        "alerts",
                        "--source",
                        "files",
                        "--input-dir",
                        tmpdir,
                        "--output-dir",
                        tmpdir,
                    ]
                )

        alert_pipeline.run_alert_pipeline.assert_called_once()

    @patch("observability_migration.adapters.source.datadog.cli._extract")
    @patch("observability_migration.adapters.source.datadog.alert_pipeline.extract_monitors_from_files")
    def test_file_alerts_only_works_without_dashboards(
        self,
        mock_extract_monitors,
        mock_extract_dashboards,
    ):
        monitor = {
            "id": 123,
            "name": "CPU high",
            "type": "metric alert",
            "query": "avg(last_5m):avg:system.cpu.user{*} > 80",
            "options": {},
        }
        mock_extract_monitors.return_value = [monitor]

        with tempfile.TemporaryDirectory() as tmpdir:
            monitor_dir = Path(tmpdir) / "monitors"
            monitor_dir.mkdir()

            datadog_cli.main(
                [
                    "--assets",
                    "alerts",
                    "--source",
                    "files",
                    "--input-dir",
                    tmpdir,
                    "--output-dir",
                    tmpdir,
                ]
            )

        mock_extract_dashboards.assert_not_called()
        mock_extract_monitors.assert_called_once_with(str(monitor_dir))

    @patch("observability_migration.adapters.source.datadog.extract.extract_dashboards_from_api")
    @patch("observability_migration.adapters.source.datadog.alert_pipeline.extract_monitors_from_api")
    def test_api_alerts_only_does_not_pull_dashboards(
        self,
        mock_extract_monitors,
        mock_extract_dashboards,
    ):
        mock_extract_monitors.return_value = []

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            datadog_cli,
            "load_credentials_from_env",
            return_value={
                "api_key": "dd-api-key",
                "app_key": "dd-app-key",
                "site": "datadoghq.com",
            },
        ):
            datadog_cli.main(
                [
                    "--assets",
                    "alerts",
                    "--source",
                    "api",
                    "--env-file",
                    "datadog_creds.env",
                    "--output-dir",
                    tmpdir,
                ]
            )

        mock_extract_dashboards.assert_not_called()
        mock_extract_monitors.assert_called_once()

    @patch(
        "observability_migration.adapters.source.datadog.cli._extract",
        side_effect=AssertionError(
            "dashboard extraction should be skipped for --assets alerts"
        ),
    )
    def test_alerts_only_skips_dashboard_extraction(self, mock_extract):
        field_map = load_profile("otel")
        alert_pipeline = ModuleType(
            "observability_migration.adapters.source.datadog.alert_pipeline"
        )
        alert_pipeline.run_alert_pipeline = mock.Mock(
            side_effect=RuntimeError("datadog-alert-pipeline-called")
        )

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            sys.modules,
            {
                "observability_migration.adapters.source.datadog.alert_pipeline": alert_pipeline,
            },
        ), patch.object(
            datadog_cli.target_registry,
            "get",
            return_value=mock.Mock(return_value=mock.Mock()),
        ), patch.object(
            datadog_cli,
            "load_credentials_from_env",
            return_value={},
        ), patch.object(
            datadog_cli,
            "load_profile",
            return_value=field_map,
        ), patch.object(
            datadog_cli,
            "_load_live_field_capabilities",
        ):
            with self.assertRaisesRegex(RuntimeError, "datadog-alert-pipeline-called"):
                datadog_cli.main(
                    [
                        "--assets",
                        "alerts",
                        "--source",
                        "files",
                        "--input-dir",
                        tmpdir,
                        "--output-dir",
                        tmpdir,
                    ]
                )

        mock_extract.assert_not_called()
        alert_pipeline.run_alert_pipeline.assert_called_once()

    def test_cli_routes_asset_outputs_to_scoped_directories(self):
        field_map = load_profile("otel")
        alert_pipeline = ModuleType(
            "observability_migration.adapters.source.datadog.alert_pipeline"
        )

        def _fake_alert_pipeline(*_args, **kwargs):
            output_dir = kwargs["output_dir"]
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "monitor_migration_results.json").write_text(
                "{}",
                encoding="utf-8",
            )
            return {
                "total": 2,
                "artifacts_dir": str(output_dir),
            }

        alert_pipeline.run_alert_pipeline = mock.Mock(side_effect=_fake_alert_pipeline)

        def _fake_dashboard_pipeline(*, output_dir, **_kwargs):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "yaml").mkdir(parents=True, exist_ok=True)
            ((output_dir / "yaml") / "dashboard.yaml").write_text(
                "dashboard: true\n",
                encoding="utf-8",
            )
            return {
                "total": 1,
                "artifacts_dir": str(output_dir),
            }

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            sys.modules,
            {
                "observability_migration.adapters.source.datadog.alert_pipeline": alert_pipeline,
            },
        ), patch.object(
            datadog_cli,
            "_run_dashboard_pipeline",
            side_effect=_fake_dashboard_pipeline,
        ) as mock_dashboard_pipeline, patch.object(
            datadog_cli.target_registry,
            "get",
            return_value=mock.Mock(return_value=mock.Mock()),
        ), patch.object(
            datadog_cli,
            "load_credentials_from_env",
            return_value={},
        ), patch.object(
            datadog_cli,
            "load_profile",
            return_value=field_map,
        ), patch.object(
            datadog_cli,
            "_load_live_field_capabilities",
        ):
            datadog_cli.main(
                [
                    "--assets",
                    "all",
                    "--source",
                    "files",
                    "--input-dir",
                    tmpdir,
                    "--output-dir",
                    tmpdir,
                ]
            )

            dashboards_dir = Path(tmpdir) / "dashboards"
            alerts_dir = Path(tmpdir) / "alerts"
            run_summary = json.loads(
                (Path(tmpdir) / "run_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                mock_dashboard_pipeline.call_args.kwargs["output_dir"],
                dashboards_dir,
            )
            self.assertEqual(
                alert_pipeline.run_alert_pipeline.call_args.kwargs["output_dir"],
                alerts_dir,
            )
            self.assertEqual(run_summary["requested_assets"], "all")
            self.assertEqual(run_summary["ran"], {"dashboards": True, "alerts": True})
            self.assertEqual(run_summary["dashboards"]["artifacts_dir"], str(dashboards_dir))
            self.assertEqual(run_summary["alerts"]["artifacts_dir"], str(alerts_dir))
            self.assertTrue((dashboards_dir / "yaml" / "dashboard.yaml").exists())
            self.assertTrue((alerts_dir / "monitor_migration_results.json").exists())


class DatadogNormalizeTileSizesTests(unittest.TestCase):
    """Tests for _normalize_tile_sizes in datadog/generate.py."""

    def _make_panel(self, chart_type: str, w: int, h: int, x: int = 0) -> dict:
        panel: dict = {"position": {"x": x, "y": 0}, "size": {"w": w, "h": h}}
        if chart_type == "datatable":
            panel["lens"] = {"type": "datatable"}
        elif chart_type == "markdown":
            panel["markdown"] = {"content": "note"}
        else:
            panel["esql"] = {"type": chart_type, "query": "FROM x"}
        return panel

    def _run(self, panels):
        from observability_migration.adapters.source.datadog.generate import (
            _normalize_tile_sizes,
        )
        _normalize_tile_sizes(panels)

    def test_gauge_gets_min_height(self):
        panel = self._make_panel("gauge", w=12, h=4)
        self._run([panel])
        self.assertGreaterEqual(panel["size"]["h"], 8)

    def test_metric_gets_min_height(self):
        panel = self._make_panel("metric", w=12, h=3)
        self._run([panel])
        self.assertGreaterEqual(panel["size"]["h"], 6)

    def test_line_gets_min_height(self):
        panel = self._make_panel("line", w=12, h=2)
        self._run([panel])
        self.assertGreaterEqual(panel["size"]["h"], 6)

    def test_datatable_gets_min_width_and_height(self):
        panel = self._make_panel("datatable", w=6, h=4)
        self._run([panel])
        self.assertGreaterEqual(panel["size"]["w"], 12)
        self.assertGreaterEqual(panel["size"]["h"], 8)

    def test_max_h_capped_for_metric(self):
        panel = self._make_panel("metric", w=12, h=20)
        self._run([panel])
        self.assertLessEqual(panel["size"]["h"], 12)

    def test_panel_already_above_min_unchanged(self):
        panel = self._make_panel("gauge", w=24, h=16)
        self._run([panel])
        self.assertEqual(panel["size"]["w"], 24)
        self.assertEqual(panel["size"]["h"], 16)

    def test_x_clamped_when_panel_overflows(self):
        panel = self._make_panel("line", w=24, h=8, x=30)
        self._run([panel])
        self.assertLessEqual(panel["position"]["x"] + panel["size"]["w"], 48)

    def test_descends_into_sections(self):
        inner = self._make_panel("gauge", w=12, h=3)
        section_panel = {
            "section": {"title": "S", "panels": [inner]},
            "position": {},
            "size": {},
        }
        self._run([section_panel])
        self.assertGreaterEqual(inner["size"]["h"], 8, "section panels must also be normalized")

    def test_all_constrained_types_meet_min_h(self):
        """Every type in PANEL_SIZE_CONSTRAINTS gets its min_h applied."""
        from observability_migration.targets.kibana.emit.layout import PANEL_SIZE_CONSTRAINTS

        for vtype, (min_w, min_h, _max_h) in PANEL_SIZE_CONSTRAINTS.items():
            panel = self._make_panel(vtype, w=max(min_w, 8), h=1)
            self._run([panel])
            self.assertGreaterEqual(
                panel["size"]["h"],
                min_h,
                f"type '{vtype}': expected h >= {min_h}, got {panel['size']['h']}",
            )


class TestDatadogSummaryView(unittest.TestCase):
    def _result(self):
        from observability_migration.adapters.source.datadog.models import (
            DashboardResult,
            TranslationResult,
        )

        dr = DashboardResult(dashboard_id="d1", dashboard_title="DD One", source_file="dd.json")
        dr.compiled = True
        ok = TranslationResult(widget_id="1", title="CPU", status="ok")
        ok.verification_packet = {"semantic_gate": "Green"}
        nf = TranslationResult(widget_id="2", title="APM thing", status="not_feasible")
        nf.reasons = ["unsupported data source apm"]
        nf.source_queries = ["avg:trace.http.request{*}"]
        nf.verification_packet = {"semantic_gate": "Red"}
        blocked = TranslationResult(widget_id="3", title="Blocked", status="blocked")
        blocked.reasons = ["query parse failed"]
        grp = TranslationResult(widget_id="g", title="Group", status="skipped", kibana_type="group")
        dr.panel_results = [ok, nf, blocked, grp]
        dr.total_widgets = 4
        dr.recompute_counts()
        return dr

    def test_datadog_view_uses_widget_noun_and_folds_blocked(self):
        from observability_migration.adapters.source.datadog.report import (
            build_summary_view,
        )

        results = [self._result()]
        review_queue = [
            {
                "dashboard": "DD One",
                "panels": 3,
                "migrated": 1,
                "gates": {"green": 1, "yellow": 0, "red": 1},
                "risk_score": 10,
            }
        ]
        view = build_summary_view(results, review_queue=review_queue, run_id="dd1")
        self.assertEqual(view.source, "datadog")
        self.assertEqual(view.element_noun, "widget")
        # Group excluded from renderable widget total: 3, not 4
        self.assertEqual(view.totals.elements_total, 3)
        # blocked + not_feasible both land in attention
        statuses = sorted(a.status for a in view.attention)
        self.assertIn("blocked", statuses)
        self.assertIn("not_feasible", statuses)
        # group never appears in attention/warnings
        self.assertFalse(any(a.panel == "Group" for a in view.attention))
        # Datadog source query list is joined into the attention item
        nf = next(a for a in view.attention if a.status == "not_feasible")
        self.assertEqual(nf.source_query, "avg:trace.http.request{*}")


class TestDatadogWritesMarkdownSummary(unittest.TestCase):
    def test_offline_migration_writes_markdown_summary(self):
        sample = (
            Path(__file__).parent.parent
            / "infra"
            / "datadog"
            / "dashboards"
            / "sample_dashboard.json"
        )

        def _fake_annotate(results, validation_records=None, **kwargs):
            return {"summary": {"green": 0, "yellow": 0, "red": 0}, "packets": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            input_dir = Path(tmpdir) / "in"
            input_dir.mkdir()
            shutil.copy(sample, input_dir / "sample_dashboard.json")
            output_dir = Path(tmpdir) / "out"

            with patch.object(
                datadog_cli,
                "annotate_results_with_verification",
                side_effect=_fake_annotate,
            ), patch(
                "observability_migration.adapters.source.datadog.execution.requests.get",
                side_effect=AssertionError("offline migration must not call the Datadog API"),
            ):
                datadog_cli.main(
                    [
                        "--source", "files",
                        "--input-dir", str(input_dir),
                        "--output-dir", str(output_dir),
                        "--assets", "dashboards",
                        "--env-file", "/dev/null",
                    ]
                )

            summary_path = output_dir / "dashboards" / "migration_summary.md"
            rollout_path = output_dir / "dashboards" / "rollout_plan.json"
            self.assertTrue(summary_path.exists())
            self.assertTrue(rollout_path.exists())
            text = summary_path.read_text(encoding="utf-8")
            rollout = json.loads(rollout_path.read_text(encoding="utf-8"))
            self.assertIn("# Migration Summary — Datadog → Kibana", text)
            self.assertIn(f"`{rollout['run_id']}`", text)
            self.assertIn("Widgets", text)


if __name__ == "__main__":
    unittest.main()
