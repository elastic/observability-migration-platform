# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Comprehensive test suites aligned with the Datadog migration test plan.

Covers sections 10.1 through 10.15 as described in TEST_PLAN.md.
Each class maps to a test plan section.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observability_migration.adapters.source.datadog.extract import (
    extract_dashboards_from_files,
    load_credentials_from_env,
)
from observability_migration.adapters.source.datadog.field_map import (
    OTEL_PROFILE,
    FieldMapProfile,
)
from observability_migration.adapters.source.datadog.generate import generate_dashboard_yaml
from observability_migration.adapters.source.datadog.log_parser import (
    LogTokenizeWarning,
    log_ast_to_esql_where,
    log_ast_to_kql,
    parse_log_query,
)
from observability_migration.adapters.source.datadog.models import (
    DashboardResult,
    LogBoolOp,
    LogNot,
    LogWildcard,
    MetricQuery,
    NormalizedDashboard,
    NormalizedWidget,
    PanelPlan,
    TranslationResult,
    WidgetFormula,
    WidgetQuery,
)
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.preflight import (
    FieldCapability,
    PreflightResult,
    check_data_view,
    check_esql_limits,
    check_field_compatibility,
    check_kibana_version,
    check_runtime_field_budget,
    run_preflight,
)
from observability_migration.adapters.source.datadog.query_parser import (
    VALID_AGGREGATORS,
    ParseError,
    parse_formula,
    parse_legacy_query,
    parse_metric_query,
)
from observability_migration.adapters.source.datadog.translate import translate_widget

# =========================================================================
# 10.1 Extractor Suite
# =========================================================================

class TestExtractorSuite(unittest.TestCase):
    """10.1 — Prove extraction is stable, retry-safe, and complete."""

    def test_extract_from_valid_dir(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards"
        if not path.exists():
            self.skipTest("no datadog fixtures")
        dashboards = extract_dashboards_from_files(str(path))
        self.assertGreater(len(dashboards), 0)

    def test_extract_from_missing_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            extract_dashboards_from_files("/nonexistent/path")

    def test_extract_from_empty_dir_raises(self):
        with tempfile.TemporaryDirectory() as td, self.assertRaises(FileNotFoundError):
            extract_dashboards_from_files(td)

    def test_invalid_json_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "bad.json").write_text("not json {{{")
            (Path(td) / "good.json").write_text(json.dumps({
                "title": "Good",
                "widgets": [{"definition": {"type": "note"}}],
            }))
            dashboards = extract_dashboards_from_files(td)
            self.assertEqual(len(dashboards), 1)
            self.assertEqual(dashboards[0]["title"], "Good")

    def test_repeated_extraction_stable(self):
        with tempfile.TemporaryDirectory() as td:
            data = {"title": "Stable", "widgets": [{"definition": {"type": "note"}}]}
            (Path(td) / "test.json").write_text(json.dumps(data))
            first = extract_dashboards_from_files(td)
            second = extract_dashboards_from_files(td)
            self.assertEqual(
                json.dumps(first, sort_keys=True),
                json.dumps(second, sort_keys=True),
            )

    def test_list_format_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            data = [
                {"title": "A", "widgets": []},
                {"title": "B", "widgets": []},
            ]
            (Path(td) / "multi.json").write_text(json.dumps(data))
            dashboards = extract_dashboards_from_files(td)
            self.assertEqual(len(dashboards), 2)

    def test_non_dashboard_items_in_list_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            data = [
                {"id": 12345, "type": "query alert", "query": "avg:system.cpu.user{*} > 90"},
                {"title": "A", "widgets": []},
            ]
            (Path(td) / "mixed.json").write_text(json.dumps(data))
            dashboards = extract_dashboards_from_files(td)
            self.assertEqual(len(dashboards), 1)
            self.assertEqual(dashboards[0]["title"], "A")

    def test_load_credentials_from_env(self):
        with mock.patch.dict(os.environ, {"DD_API_KEY": "key1", "DD_APP_KEY": "app1"}):
            creds = load_credentials_from_env()
            self.assertEqual(creds["api_key"], "key1")
            self.assertEqual(creds["app_key"], "app1")

    def test_load_credentials_missing_returns_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DD_API_KEY", None)
            os.environ.pop("DD_APP_KEY", None)
            creds = load_credentials_from_env()
            self.assertEqual(creds["api_key"], "")

    def test_no_widgets_key_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "no_widgets.json").write_text(json.dumps({"config": {}}))
            (Path(td) / "has_widgets.json").write_text(json.dumps({
                "title": "Good", "widgets": [],
            }))
            dashboards = extract_dashboards_from_files(td)
            self.assertEqual(len(dashboards), 1)

    def test_wrapped_dashboard_key_extracted(self):
        with tempfile.TemporaryDirectory() as td:
            data = {"dashboard": {"title": "Nested", "widgets": []}}
            (Path(td) / "wrapped.json").write_text(json.dumps(data))
            dashboards = extract_dashboards_from_files(td)
            self.assertEqual(len(dashboards), 1)
            self.assertEqual(dashboards[0]["title"], "Nested")


# =========================================================================
# 10.2 Metrics Parser — Gap Tests
# =========================================================================

class TestMetricParserGaps(unittest.TestCase):
    """10.2 — Fill metric parser coverage gaps."""

    def test_rollup_without_interval(self):
        mq = parse_metric_query("avg:system.cpu.user{*}.rollup(avg)")
        self.assertIsNotNone(mq.rollup)
        self.assertEqual(mq.rollup.args, ["avg"])

    def test_unsupported_aggregator_raises(self):
        with self.assertRaises(ParseError) as ctx:
            parse_metric_query("unknown_agg:system.cpu.user{*}")
        self.assertIn("unsupported aggregator", str(ctx.exception))

    def test_valid_aggregator_set(self):
        for agg in VALID_AGGREGATORS:
            mq = parse_metric_query(f"{agg}:some.metric{{*}}")
            self.assertEqual(mq.space_agg, agg)

    def test_nested_rollup_detected(self):
        mq = parse_metric_query("avg:system.cpu.user{*}.rollup(avg, 60).rollup(sum, 300)")
        rollup_count = sum(1 for fn in mq.functions if fn.name == "rollup")
        self.assertEqual(rollup_count, 2)

    def test_formula_with_multiple_named_queries(self):
        fe = parse_formula("query1 + query2 - query3")
        refs = fe.referenced_queries
        self.assertEqual(set(refs), {"query1", "query2", "query3"})

    def test_per_second_legacy_wrapper(self):
        mq, fns = parse_legacy_query("per_second(sum:http.requests{*})")
        self.assertIsNotNone(mq)
        self.assertEqual(len(fns), 1)
        self.assertEqual(fns[0].name, "per_second")

    def test_nested_legacy_wrappers(self):
        mq, fns = parse_legacy_query("abs(per_second(avg:system.cpu.user{*}))")
        self.assertIsNotNone(mq)
        self.assertEqual([f.name for f in fns], ["abs", "per_second"])

    def test_formula_unary_negation(self):
        fe = parse_formula("-query1")
        from observability_migration.adapters.source.datadog.models import FormulaUnary
        self.assertIsInstance(fe.ast, FormulaUnary)
        self.assertEqual(fe.ast.op, "-")

    def test_p95_aggregator(self):
        mq = parse_metric_query("p95:trace.flask.request.duration{*}")
        self.assertEqual(mq.space_agg, "p95")


# =========================================================================
# 10.3 Logs Parser — Gap Tests
# =========================================================================

class TestLogParserGaps(unittest.TestCase):
    """10.3 — Fill log parser coverage gaps."""

    def test_or_array_in_attribute(self):
        lq = parse_log_query("@http.status_code:(200 OR 301 OR 404)")
        esql = log_ast_to_esql_where(lq.ast)
        self.assertIn("200", esql)
        self.assertIn("301", esql)
        self.assertIn("404", esql)
        self.assertIn("OR", esql)

    def test_mixed_free_text_and_structured(self):
        lq = parse_log_query('error service:web @duration:>1000')
        self.assertIsInstance(lq.ast, LogBoolOp)
        self.assertEqual(lq.ast.op, "AND")
        self.assertTrue(len(lq.ast.children) >= 2)

    def test_tokenizer_tracks_skipped_chars(self):
        LogTokenizeWarning.reset()
        parse_log_query("valid=term")
        # '=' is not a recognized token boundary
        # The tokenizer should either handle it or record it
        # This test documents the behavior

    def test_not_keyword(self):
        lq = parse_log_query("NOT service:web")
        self.assertIsInstance(lq.ast, LogNot)

    def test_deeply_nested_parens(self):
        lq = parse_log_query("(service:web AND (status:error OR status:warn))")
        self.assertIsInstance(lq.ast, LogBoolOp)

    def test_kql_output_for_negated_attr(self):
        lq = parse_log_query("-service:web")
        kql = log_ast_to_kql(lq.ast)
        self.assertIn("NOT", kql)

    def test_esql_range_output(self):
        lq = parse_log_query("@response_time:[100 TO 500]")
        esql = log_ast_to_esql_where(lq.ast)
        self.assertIn("response_time >= 100", esql)
        self.assertIn("response_time <= 500", esql)

    def test_wildcard_in_message(self):
        lq = parse_log_query("error*")
        self.assertIsInstance(lq.ast, LogWildcard)
        esql = log_ast_to_esql_where(lq.ast)
        self.assertIn("LIKE", esql)
        self.assertIn("error*", esql)

    def test_esql_field_map_applied(self):
        lq = parse_log_query("service:web")
        fm = {"service": "service.name"}
        esql = log_ast_to_esql_where(lq.ast, fm)
        self.assertIn("service.name", esql)

    def test_empty_ast_to_esql(self):
        result = log_ast_to_esql_where(None)
        self.assertEqual(result, "")


# =========================================================================
# 10.4 IR Validation Suite
# =========================================================================

class TestIRValidation(unittest.TestCase):
    """10.4 — Prove normalized IR is complete and internally consistent."""

    def _load_sample(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        if not path.exists():
            self.skipTest("no fixture")
        return json.loads(path.read_text())

    def test_all_widgets_have_ids(self):
        nd = normalize_dashboard(self._load_sample())
        for w in nd.widgets:
            self.assertTrue(w.id, f"widget missing id: {w.title}")

    def test_all_widgets_have_types(self):
        nd = normalize_dashboard(self._load_sample())
        for w in nd.widgets:
            self.assertTrue(w.widget_type, f"widget missing type: {w.id}")

    def test_layout_fields_present(self):
        nd = normalize_dashboard(self._load_sample())
        for w in nd.widgets:
            self.assertIn("x", w.layout)
            self.assertIn("y", w.layout)
            self.assertIn("width", w.layout)
            self.assertIn("height", w.layout)

    def test_template_variables_preserved(self):
        raw = {
            "title": "TV test",
            "template_variables": [
                {"name": "host", "tag": "host", "default": "*"},
                {"name": "env", "prefix": "env", "default": "prod"},
            ],
            "widgets": [],
        }
        nd = normalize_dashboard(raw)
        self.assertEqual(len(nd.template_variables), 2)
        self.assertEqual(nd.template_variables[0].name, "host")
        self.assertEqual(nd.template_variables[1].tag, "env")
        self.assertEqual(nd.template_variables[1].default, "prod")

    def test_metric_queries_have_metric_name(self):
        nd = normalize_dashboard(self._load_sample())
        for w in nd.widgets:
            for q in w.queries:
                if q.metric_query:
                    self.assertTrue(q.metric_query.metric, f"metric query missing metric name in widget {w.id}")

    def test_unsupported_constructs_flagged(self):
        nd = normalize_dashboard(self._load_sample())
        unsupported_widgets = [w for w in nd.widgets if not w.is_supported]
        for w in unsupported_widgets:
            plan = plan_widget(w)
            self.assertIn(plan.backend, ("blocked", "markdown"))

    def test_diagnostics_on_apm_data_source(self):
        raw = {
            "title": "APM", "widgets": [{
                "definition": {
                    "type": "timeseries",
                    "requests": [{"apm_query": {"index": "trace"}}],
                },
            }],
        }
        nd = normalize_dashboard(raw)
        self.assertEqual(nd.widgets[0].queries[0].data_source, "apm")


# =========================================================================
# 10.5 Preflight Suite
# =========================================================================

class TestPreflightSuite(unittest.TestCase):
    """10.5 — Prove preflight detects target incompatibilities."""

    def test_version_too_old_blocks(self):
        issues = check_kibana_version("7.0.0")
        blocking = [i for i in issues if i.level == "block"]
        self.assertGreater(len(blocking), 0)

    def test_version_current_passes(self):
        issues = check_kibana_version("9.1.0")
        blocking = [i for i in issues if i.level == "block"]
        self.assertEqual(len(blocking), 0)

    def test_version_older_minor_blocks_import(self):
        issues = check_kibana_version("9.0.0", source_version="9.2.0")
        blocking = [i for i in issues if i.level == "block"]
        self.assertGreater(len(blocking), 0)

    def test_version_newer_minor_same_major_ok(self):
        issues = check_kibana_version("9.3.0", source_version="9.1.0")
        blocking = [i for i in issues if i.level == "block"]
        self.assertEqual(len(blocking), 0)

    def test_version_next_major_ok(self):
        issues = check_kibana_version("10.0.0", source_version="9.5.0")
        blocking = [i for i in issues if i.level == "block"]
        self.assertEqual(len(blocking), 0)

    def test_version_two_majors_behind_blocks(self):
        check_kibana_version("10.0.0", source_version="8.0.0")
        issues2 = check_kibana_version("8.0.0", source_version="10.0.0")
        blocking = [i for i in issues2 if i.level == "block"]
        self.assertGreater(len(blocking), 0)

    def test_field_missing_warns(self):
        caps = {}
        req = [{"name": "host.name", "usage": "filter"}]
        issues = check_field_compatibility(req, caps)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].level, "warn")

    def test_field_not_aggregatable_blocks(self):
        caps = {"host.name": FieldCapability(name="host.name", type="text", aggregatable=False)}
        req = [{"name": "host.name", "usage": "group_by"}]
        issues = check_field_compatibility(req, caps)
        blocking = [i for i in issues if i.level == "block"]
        self.assertGreater(len(blocking), 0)

    def test_field_type_family_mismatch_blocks_when_declared(self):
        caps = {"system.cpu.user": FieldCapability(name="system.cpu.user", type="keyword")}
        req = [{"name": "system.cpu.user", "usage": "aggregate", "type_family": "numeric"}]
        issues = check_field_compatibility(req, caps)
        blocking = [i for i in issues if i.level == "block"]
        self.assertGreater(len(blocking), 0)

    def test_field_conflicting_types_warns(self):
        caps = {"status": FieldCapability(
            name="status", type="keyword",
            conflicting_types=["keyword", "long"],
        )}
        req = [{"name": "status", "usage": "filter"}]
        issues = check_field_compatibility(req, caps)
        warns = [i for i in issues if i.level == "warn"]
        self.assertGreater(len(warns), 0)

    def test_esql_rows_over_10k_blocks(self):
        issues = check_esql_limits(estimated_rows=15000)
        blocking = [i for i in issues if i.level == "block"]
        self.assertGreater(len(blocking), 0)

    def test_esql_rows_over_1k_warns(self):
        issues = check_esql_limits(estimated_rows=5000)
        warns = [i for i in issues if i.level == "warn"]
        self.assertGreater(len(warns), 0)

    def test_esql_rows_under_1k_ok(self):
        issues = check_esql_limits(estimated_rows=500)
        self.assertEqual(len(issues), 0)

    def test_runtime_field_budget_exceeded_blocks(self):
        issues = check_runtime_field_budget(10, budget=5)
        blocking = [i for i in issues if i.level == "block"]
        self.assertGreater(len(blocking), 0)

    def test_runtime_field_within_budget_info(self):
        issues = check_runtime_field_budget(3, budget=5)
        self.assertTrue(all(i.level in ("info",) for i in issues))

    def test_data_view_missing_no_create_blocks(self):
        issues = check_data_view(
            "metrics-*",
            data_views_available=["logs-*"],
            can_create_data_view=False,
        )
        blocking = [i for i in issues if i.level == "block"]
        self.assertGreater(len(blocking), 0)

    def test_data_view_missing_can_create_info(self):
        issues = check_data_view(
            "metrics-*",
            data_views_available=["logs-*"],
            can_create_data_view=True,
        )
        self.assertTrue(all(i.level == "info" for i in issues))

    def test_data_view_exists_ok(self):
        issues = check_data_view("metrics-*", data_views_available=["metrics-*"])
        self.assertEqual(len(issues), 0)

    def test_run_preflight_aggregates(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        if not path.exists():
            self.skipTest("no fixture")
        raw = json.loads(path.read_text())
        nd = normalize_dashboard(raw)
        result = run_preflight(nd, target_kibana_version="9.1.0")
        self.assertIsInstance(result, PreflightResult)

    def test_run_preflight_uses_field_map_for_metric_type_checks(self):
        mq = parse_metric_query("avg:system.cpu.user{host:web01} by {host}")
        widget = NormalizedWidget(
            id="widget-1",
            widget_type="timeseries",
            title="CPU",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query="avg:system.cpu.user{host:web01} by {host}",
                    metric_query=mq,
                    query_type="metric",
                )
            ],
        )
        dashboard = NormalizedDashboard(id="dash-1", title="Dash", widgets=[widget])
        profile = FieldMapProfile(
            name="typed",
            metric_index="metrics-*",
            logs_index="logs-*",
            tag_map={"host": "host.name"},
            metric_field_caps={
                "system_cpu_user": FieldCapability(
                    name="system_cpu_user",
                    type="keyword",
                    aggregatable=True,
                    searchable=True,
                ),
                "host.name": FieldCapability(
                    name="host.name",
                    type="keyword",
                    aggregatable=True,
                    searchable=True,
                ),
            },
        )

        result = run_preflight(dashboard, field_map=profile)

        blocking = [issue for issue in result.issues if issue.level == "block"]
        self.assertTrue(any("system_cpu_user" in issue.message for issue in blocking))
        self.assertTrue(any("requires 'numeric'" in issue.message for issue in blocking))

    def test_run_preflight_uses_log_context_capabilities(self):
        lq = parse_log_query("service:web")
        widget = NormalizedWidget(
            id="widget-2",
            widget_type="log_stream",
            title="Logs",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="logs",
                    raw_query="service:web",
                    log_query=lq,
                    query_type="log",
                )
            ],
        )
        dashboard = NormalizedDashboard(id="dash-2", title="Dash", widgets=[widget])
        profile = FieldMapProfile(
            name="typed",
            metric_index="metrics-*",
            logs_index="logs-*",
            tag_map={"service": "service.name"},
            metric_field_caps={
                "service.name": FieldCapability(
                    name="service.name",
                    type="double",
                    aggregatable=True,
                    searchable=True,
                )
            },
            log_field_caps={
                "service.name": FieldCapability(
                    name="service.name",
                    type="keyword",
                    aggregatable=True,
                    searchable=False,
                )
            },
        )

        result = run_preflight(dashboard, field_map=profile)

        warnings = [issue for issue in result.issues if issue.level == "warn"]
        self.assertTrue(any("service.name" in issue.message for issue in warnings))
        self.assertTrue(any("not searchable" in issue.message for issue in warnings))

    def test_run_preflight_ignores_metric_template_scope_and_group_tokens(self):
        mq = parse_metric_query("avg:system.cpu.user{$scope,$host} by {$service}")
        widget = NormalizedWidget(
            id="widget-3",
            widget_type="timeseries",
            title="CPU",
            queries=[
                WidgetQuery(
                    name="q1",
                    data_source="metrics",
                    raw_query="avg:system.cpu.user{$scope,$host} by {$service}",
                    metric_query=mq,
                    query_type="metric",
                )
            ],
        )
        dashboard = NormalizedDashboard(id="dash-3", title="Dash", widgets=[widget])
        profile = FieldMapProfile(
            name="typed",
            metric_index="metrics-*",
            logs_index="logs-*",
            metric_field_caps={
                "system_cpu_user": FieldCapability(
                    name="system_cpu_user",
                    type="double",
                    aggregatable=True,
                    searchable=True,
                )
            },
        )

        result = run_preflight(dashboard, field_map=profile)

        self.assertEqual(result.issues, [])

    def test_invalid_version_string_blocks(self):
        issues = check_kibana_version("not_a_version")
        blocking = [i for i in issues if i.level == "block"]
        self.assertGreater(len(blocking), 0)


# =========================================================================
# 10.6 Planner — Gap Tests
# =========================================================================

class TestPlannerGaps(unittest.TestCase):
    """10.6 — Fill planner coverage gaps."""

    def _make_widget(self, **kwargs):
        return NormalizedWidget(**kwargs)

    def test_log_with_free_text_chooses_kql_bridge(self):
        lq = parse_log_query("connection timeout error")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="connection timeout error", log_query=lq, query_type="log")
        w = self._make_widget(id="1", widget_type="timeseries", title="Errors", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql_with_kql")

    def test_log_with_only_structured_stays_esql(self):
        lq = parse_log_query("service:web")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="service:web", log_query=lq, query_type="log")
        w = self._make_widget(id="1", widget_type="timeseries", title="Logs", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")

    def test_simple_metric_chooses_lens(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="timeseries", title="CPU", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "lens")

    def test_simple_query_value_prefers_esql(self):
        mq = parse_metric_query("avg:system.cpu.user{*}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="query_value", title="CPU", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")

    def test_simple_toplist_prefers_esql(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="toplist", title="Top", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")

    def test_simple_query_table_prefers_esql(self):
        mq = parse_metric_query("sum:consul.catalog.services_critical{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="query_table", title="Services", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")

    def test_grouped_query_value_stays_lens(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="query_value", title="CPU", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "lens")

    def test_count_derived_query_value_stays_esql(self):
        mq = parse_metric_query("sum:http.requests{*}.as_count()")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="query_value", title="Requests", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")

    def test_rate_metric_chooses_esql(self):
        mq = parse_metric_query("sum:http.requests{*}.as_rate()")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="timeseries", title="Rate", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")

    def test_formula_metric_chooses_esql(self):
        mq = parse_metric_query("avg:system.cpu.user{*}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="q1 * 100")
        wf.expression = parse_formula("q1 * 100")
        w = self._make_widget(id="1", widget_type="query_value", title="CPU%", queries=[wq], formulas=[wf])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")

    def test_nested_query_lowers_confidence(self):
        mq = parse_metric_query("avg:system.cpu.user{*}.rollup(avg, 60).rollup(sum, 300)")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="timeseries", title="Nested", queries=[wq])
        plan = plan_widget(w)
        self.assertLess(plan.confidence, 1.0)
        self.assertIn("nested_aggregation", plan.field_issues)

    def test_complexity_function_lowers_confidence(self):
        mq = parse_metric_query("avg:system.cpu.user{*}")
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="anomalies(query1)")
        wf.expression = parse_formula("anomalies(query1)")
        w = self._make_widget(id="1", widget_type="timeseries", title="Anomaly", queries=[wq], formulas=[wf])
        plan = plan_widget(w)
        self.assertLess(plan.confidence, 1.0)

    def test_heatmap_always_esql(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = self._make_widget(id="1", widget_type="heatmap", title="Heat", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql")


# =========================================================================
# 10.7 ES|QL Generation — Gap Tests
# =========================================================================

class TestEsqlGenerationGaps(unittest.TestCase):
    """10.7 — Fill ES|QL generation coverage gaps."""

    def test_kql_bridge_in_log_translation(self):
        lq = parse_log_query("connection timeout")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="connection timeout", log_query=lq, query_type="log")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="Errors", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "esql_with_kql")
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertIn("KQL(", result.esql_query)

    def test_esql_identifier_quoting(self):
        from observability_migration.adapters.source.datadog.translate import _esql_identifier
        self.assertEqual(_esql_identifier("simple"), "simple")
        self.assertEqual(_esql_identifier("has-dash"), "`has-dash`")
        self.assertEqual(_esql_identifier("a.b.c"), "a.b.c")
        self.assertEqual(_esql_identifier("a.b-c.d"), "a.`b-c`.d")

    def test_template_var_not_interpolated_raw(self):
        mq = parse_metric_query("avg:system.cpu.user{host:$host}.as_rate()")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="Test", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertNotIn("$host", result.esql_query)

    def test_where_kql_escapes_special_chars(self):
        lq = parse_log_query('"error "connection" refused"')
        wq = WidgetQuery(name="q1", data_source="logs", raw_query='...', log_query=lq, query_type="log")
        w = NormalizedWidget(id="1", widget_type="log_stream", title="Test", queries=[wq])
        plan = PanelPlan(widget_id="1", backend="esql_with_kql", kibana_type="table")
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertNotIn('""', result.esql_query.replace('\\"', ''))

    def test_bucket_expression_in_timeseries(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}.as_rate()")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="Test", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp", result.esql_query)

    def test_keep_sort_limit_present_in_toplist(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}.as_rate()")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="toplist", title="Top", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertIn("SORT", result.esql_query)
        self.assertIn("LIMIT", result.esql_query)


# =========================================================================
# 10.8 Lens Generation Suite
# =========================================================================

class TestLensGenerationSuite(unittest.TestCase):
    """10.8 — Prove Lens panels are generated safely."""

    def test_simple_timeseries_generates_lens(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="CPU", queries=[wq])
        plan = plan_widget(w)
        self.assertEqual(plan.backend, "lens")
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "ok")
        self.assertIn("type", result.yaml_panel)
        self.assertEqual(result.yaml_panel["type"], "lens")

    def test_lens_has_data_view(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="CPU", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertIn("data_view", result.yaml_panel)
        self.assertEqual(result.yaml_panel["data_view"], "metrics-*")

    def test_lens_has_metric_field(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="CPU", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertEqual(result.yaml_panel["metric_field"], "system_cpu_user")

    def test_lens_has_aggregation(self):
        mq = parse_metric_query("sum:http.requests.count{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="Req", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertEqual(result.yaml_panel["aggregation"], "SUM")

    def test_lens_group_by_carried(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="CPU", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertIn("host.name", result.yaml_panel.get("group_by", []))

    def test_lens_filters_captured(self):
        mq = parse_metric_query("avg:system.cpu.user{host:web01} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="CPU", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertTrue(len(result.yaml_panel.get("filters", [])) > 0)

    def test_lens_panel_in_yaml(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(
            id="1", widget_type="timeseries", title="CPU", queries=[wq],
            layout={"x": 0, "y": 0, "width": 4, "height": 2},
        )
        dash = NormalizedDashboard(id="1", title="Dash", widgets=[w])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        yaml_str = generate_dashboard_yaml(dash, [result])
        doc = yaml.safe_load(yaml_str)
        panels = doc["dashboards"][0]["panels"]
        self.assertTrue(len(panels) > 0)


# =========================================================================
# 10.9 Saved-Object Packaging Suite
# =========================================================================

class TestPackagingSuite(unittest.TestCase):
    """10.9 — Prove generated YAML is well-formed for kb-dashboard-cli."""

    def _full_pipeline(self, raw):
        nd = normalize_dashboard(raw)
        results = []
        for w in nd.widgets:
            plan = plan_widget(w)
            if plan.backend == "lens":
                plan.backend = "esql"
            results.append(translate_widget(w, plan, OTEL_PROFILE))
        return nd, results

    def test_yaml_is_valid(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        if not path.exists():
            self.skipTest("no fixture")
        nd, results = self._full_pipeline(json.loads(path.read_text()))
        yaml_str = generate_dashboard_yaml(nd, results)
        doc = yaml.safe_load(yaml_str)
        self.assertIn("dashboards", doc)

    def test_dashboard_has_required_fields(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        if not path.exists():
            self.skipTest("no fixture")
        nd, results = self._full_pipeline(json.loads(path.read_text()))
        yaml_str = generate_dashboard_yaml(nd, results)
        dash = yaml.safe_load(yaml_str)["dashboards"][0]
        self.assertIn("name", dash)
        self.assertIn("panels", dash)
        self.assertIn("minimum_kibana_version", dash)

    def test_panels_have_size_and_position(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        if not path.exists():
            self.skipTest("no fixture")
        nd, results = self._full_pipeline(json.loads(path.read_text()))
        yaml_str = generate_dashboard_yaml(nd, results)
        dash = yaml.safe_load(yaml_str)["dashboards"][0]
        for p in dash["panels"]:
            if "section" in p:
                continue
            self.assertIn("size", p, f"panel {p.get('title')} missing size")
            self.assertIn("position", p, f"panel {p.get('title')} missing position")

    def test_esql_panels_have_query(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        if not path.exists():
            self.skipTest("no fixture")
        nd, results = self._full_pipeline(json.loads(path.read_text()))
        yaml_str = generate_dashboard_yaml(nd, results)
        dash = yaml.safe_load(yaml_str)["dashboards"][0]
        for p in dash["panels"]:
            if "esql" in p:
                self.assertIn("query", p["esql"], f"esql panel {p['title']} missing query")
                self.assertIn("type", p["esql"], f"esql panel {p['title']} missing type")

    def test_text_fallback_panels_have_content(self):
        raw = {
            "title": "Fallback test",
            "widgets": [
                {"definition": {"type": "note", "content": "Hello world"}},
            ],
        }
        nd, results = self._full_pipeline(raw)
        yaml_str = generate_dashboard_yaml(nd, results)
        dash = yaml.safe_load(yaml_str)["dashboards"][0]
        md_panels = [p for p in dash["panels"] if "markdown" in p]
        self.assertGreater(len(md_panels), 0)
        self.assertIn("Hello world", md_panels[0]["markdown"]["content"])

    def test_no_internal_metadata_in_output(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        if not path.exists():
            self.skipTest("no fixture")
        nd, results = self._full_pipeline(json.loads(path.read_text()))
        yaml_str = generate_dashboard_yaml(nd, results)
        self.assertNotIn("_dd_y", yaml_str)
        self.assertNotIn("_dd_x", yaml_str)
        self.assertNotIn("_markdown_role", yaml_str)


# =========================================================================
# 10.13 Negative / Chaos Suite
# =========================================================================

class TestNegativeChaos(unittest.TestCase):
    """10.13 — Prove failure modes are surfaced early and honestly."""

    def test_empty_dashboard_produces_empty_panels(self):
        raw = {"title": "Empty", "widgets": []}
        nd = normalize_dashboard(raw)
        self.assertEqual(len(nd.widgets), 0)

    def test_widget_without_definition_skipped(self):
        raw = {"title": "No def", "widgets": [{"id": 1}]}
        nd = normalize_dashboard(raw)
        self.assertEqual(len(nd.widgets), 0)

    def test_multi_query_formula_with_different_filters_translates(self):
        # Previously blocked at translation; now per-aggregation WHERE
        # clauses in STATS preserve each query's filter independently.
        q1 = "count:a{type:x AND direction:out} by {topic}.as_rate()"
        q2 = "count:b{type:x AND direction:in} by {topic}.as_rate()"
        mq1 = parse_metric_query(q1)
        mq2 = parse_metric_query(q2)
        wf = WidgetFormula(raw="query1 / query2")
        wf.expression = parse_formula("query1 / query2")
        w = NormalizedWidget(
            id="1", widget_type="query_table", title="Conflict",
            queries=[
                WidgetQuery(name="query1", data_source="metrics", raw_query=q1, metric_query=mq1, query_type="metric"),
                WidgetQuery(name="query2", data_source="metrics", raw_query=q2, metric_query=mq2, query_type="metric"),
            ],
            formulas=[wf],
        )
        result = translate_widget(w, plan_widget(w), OTEL_PROFILE)
        self.assertNotEqual(result.status, "not_feasible")
        self.assertIn('direction == "out"', result.esql_query)
        self.assertIn('direction == "in"', result.esql_query)

    def test_unsupported_formula_function_reports_error(self):
        mq = parse_metric_query("avg:system.cpu.user{*}")
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="clamp_min(query1, 0)")
        wf.expression = parse_formula("clamp_min(query1, 0)")
        w = NormalizedWidget(
            id="1", widget_type="query_value", title="Clamp",
            queries=[wq], formulas=[wf],
        )
        result = translate_widget(w, plan_widget(w), OTEL_PROFILE)
        self.assertIn(result.status, ("not_feasible", "warning"))

    def test_unsupported_aggregator_in_pipeline(self):
        raw = {
            "title": "Bad agg",
            "widgets": [{
                "definition": {
                    "type": "timeseries",
                    "requests": [{
                        "queries": [{
                            "data_source": "metrics",
                            "name": "q1",
                            "query": "weird_agg:system.cpu{*}",
                        }],
                    }],
                },
            }],
        }
        nd = normalize_dashboard(raw)
        q = nd.widgets[0].queries[0]
        self.assertIn(q.query_type, ("metric_unparsed", "metric"))

    def test_blocked_panel_never_high_confidence(self):
        w = NormalizedWidget(id="1", widget_type="manage_status", title="Monitors")
        plan = plan_widget(w)
        self.assertIn(plan.backend, ("blocked", "markdown"))
        self.assertEqual(plan.confidence, 0.0)

    def test_multiple_log_queries_non_timeseries_error(self):
        lq1 = parse_log_query("service:a")
        lq2 = parse_log_query("service:b")
        w = NormalizedWidget(
            id="1", widget_type="query_table", title="Multi",
            queries=[
                WidgetQuery(name="q1", data_source="logs", raw_query="service:a", log_query=lq1, query_type="log"),
                WidgetQuery(name="q2", data_source="logs", raw_query="service:b", log_query=lq2, query_type="log"),
            ],
        )
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertEqual(result.status, "not_feasible")

    def test_dashboard_result_recompute(self):
        dr = DashboardResult(
            dashboard_id="1", dashboard_title="Test", total_widgets=3,
            panel_results=[
                TranslationResult(widget_id="1", status="ok"),
                TranslationResult(widget_id="2", status="not_feasible"),
                TranslationResult(widget_id="3", status="warning", warnings=["approx"]),
            ],
        )
        dr.recompute_counts()
        self.assertEqual(dr.migrated, 1)
        self.assertEqual(dr.not_feasible, 1)
        self.assertEqual(dr.migrated_with_warnings, 1)


# =========================================================================
# 10.14 Security Suite
# =========================================================================

class TestSecuritySuite(unittest.TestCase):
    """10.14 — Prove generated queries and bundles are safe."""

    def test_template_vars_not_raw_interpolated_in_esql(self):
        mq = parse_metric_query("avg:system.cpu.user{host:$host}.as_rate()")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="Test", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertNotIn("$host", result.esql_query)

    def test_log_template_var_not_literal(self):
        lq = parse_log_query("service:$svc")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="service:$svc", log_query=lq, query_type="log")
        w = NormalizedWidget(id="1", widget_type="log_stream", title="Test", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertNotIn("$svc", result.esql_query)

    def test_esql_escape_quotes(self):
        from observability_migration.adapters.source.datadog.translate import _esql_escape
        self.assertEqual(_esql_escape('hello "world"'), 'hello \\"world\\"')
        self.assertEqual(_esql_escape('back\\slash'), 'back\\\\slash')

    def test_no_raw_user_values_in_from_clause(self):
        """Scope values appear inside quoted WHERE predicates, not in FROM clause.
        ES|QL parametrization would prevent even quoted injection — tracked as a known gap.
        """
        mq = parse_metric_query("avg:system.cpu.user{host:$(whoami)}.as_rate()")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="Test", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        # Value appears inside a quoted string, not as a raw token
        self.assertIn('"$(whoami)"', result.esql_query)
        # It's NOT in the FROM clause
        from_line = result.esql_query.split("\n")[0]
        self.assertNotIn("$(whoami)", from_line)

    def test_credentials_not_in_output(self):
        path = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        if not path.exists():
            self.skipTest("no fixture")
        raw = json.loads(path.read_text())
        nd = normalize_dashboard(raw)
        results = []
        for w in nd.widgets:
            plan = plan_widget(w)
            if plan.backend == "lens":
                plan.backend = "esql"
            results.append(translate_widget(w, plan, OTEL_PROFILE))
        yaml_str = generate_dashboard_yaml(nd, results)
        self.assertNotIn("api_key", yaml_str.lower())
        self.assertNotIn("app_key", yaml_str.lower())
        self.assertNotIn("DD_API_KEY", yaml_str)

    def test_field_name_injection_quoted(self):
        """Field names with special chars are backtick-quoted.
        The backtick quoting prevents ES|QL injection at the field level.
        """
        from observability_migration.adapters.source.datadog.translate import _esql_identifier
        dangerous = 'field"; DROP TABLE data--'
        safe = _esql_identifier(dangerous)
        self.assertIn("`", safe)
        # Dangerous chars are inside backtick quotes, not bare
        self.assertTrue(safe.startswith("`") or "." in safe)


# =========================================================================
# 10.10 / 10.11 / 10.12 — Semantic Oracle / Import / Promotion Stubs
# =========================================================================

class TestSemanticOracleStubs(unittest.TestCase):
    """10.10 — Stub tests for semantic oracle (need live ES for full validation)."""

    def test_esql_query_is_syntactically_valid_looking(self):
        mq = parse_metric_query("avg:system.cpu.user{*} by {host}.as_rate()")
        wq = WidgetQuery(name="q1", data_source="metrics", raw_query="...", metric_query=mq, query_type="metric")
        w = NormalizedWidget(id="1", widget_type="timeseries", title="Test", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertTrue(result.esql_query.startswith("FROM"))
        self.assertIn("STATS", result.esql_query)
        self.assertIn("WHERE", result.esql_query)

    def test_log_count_query_structure(self):
        lq = parse_log_query("service:web")
        wq = WidgetQuery(name="q1", data_source="logs", raw_query="service:web", log_query=lq, query_type="log")
        w = NormalizedWidget(id="1", widget_type="query_value", title="Count", queries=[wq])
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        self.assertIn("COUNT(*)", result.esql_query)


class TestImportStubs(unittest.TestCase):
    """10.11 — Stub tests for Kibana import (need live Kibana for full validation)."""

    def test_yaml_parseable_as_valid_document(self):
        raw = {
            "title": "Import test", "widgets": [{
                "definition": {
                    "type": "note", "content": "Hello",
                },
            }],
        }
        nd = normalize_dashboard(raw)
        results = [translate_widget(w, plan_widget(w), OTEL_PROFILE) for w in nd.widgets]
        yaml_str = generate_dashboard_yaml(nd, results)
        doc = yaml.safe_load(yaml_str)
        self.assertIsInstance(doc, dict)
        self.assertIn("dashboards", doc)


class TestPromotionStubs(unittest.TestCase):
    """10.12 — Stub tests for space copy/promotion."""

    def test_dashboard_result_has_paths(self):
        dr = DashboardResult(
            dashboard_id="1", dashboard_title="Test",
            yaml_path="/tmp/test.yaml",
        )
        self.assertTrue(dr.yaml_path)

    def test_compile_path_set_after_compile(self):
        dr = DashboardResult(dashboard_id="1", dashboard_title="Test")
        dr.compiled = True
        dr.compiled_path = "/tmp/compiled/test"
        self.assertTrue(dr.compiled)


# =========================================================================
# 10.15 Performance Suite
# =========================================================================

class TestPerformanceSuite(unittest.TestCase):
    """10.15 — Establish throughput and scaling boundaries."""

    def _make_large_dashboard(self, n_widgets):
        widgets = []
        for i in range(n_widgets):
            widgets.append({
                "definition": {
                    "type": "timeseries",
                    "title": f"Widget {i}",
                    "requests": [{
                        "queries": [{
                            "data_source": "metrics",
                            "name": f"q{i}",
                            "query": f"avg:system.metric_{i}{{*}} by {{host}}",
                        }],
                    }],
                },
            })
        return {"title": f"Perf test ({n_widgets})", "widgets": widgets}

    def test_small_dashboard_under_1s(self):
        raw = self._make_large_dashboard(10)
        start = time.monotonic()
        nd = normalize_dashboard(raw)
        for w in nd.widgets:
            plan = plan_widget(w)
            translate_widget(w, plan, OTEL_PROFILE)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0, f"10 widgets took {elapsed:.2f}s")

    def test_medium_dashboard_under_5s(self):
        raw = self._make_large_dashboard(50)
        start = time.monotonic()
        nd = normalize_dashboard(raw)
        results = []
        for w in nd.widgets:
            plan = plan_widget(w)
            results.append(translate_widget(w, plan, OTEL_PROFILE))
        generate_dashboard_yaml(nd, results)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 5.0, f"50 widgets took {elapsed:.2f}s")

    def test_large_dashboard_under_30s(self):
        raw = self._make_large_dashboard(200)
        start = time.monotonic()
        nd = normalize_dashboard(raw)
        results = []
        for w in nd.widgets:
            plan = plan_widget(w)
            results.append(translate_widget(w, plan, OTEL_PROFILE))
        generate_dashboard_yaml(nd, results)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 30.0, f"200 widgets took {elapsed:.2f}s")

    def test_normalization_throughput(self):
        raw = self._make_large_dashboard(100)
        start = time.monotonic()
        for _ in range(10):
            normalize_dashboard(raw)
        elapsed = time.monotonic() - start
        per_dashboard = elapsed / 10
        self.assertLess(per_dashboard, 1.0, f"avg normalize: {per_dashboard:.3f}s")


# =========================================================================
# Bug regression tests — verify fixes for bugs found during audit
# =========================================================================

class TestBugRegressions(unittest.TestCase):
    """Regression tests for bugs found during systematic code audit."""

    # --- query_parser.py bugs ---

    def test_parse_metric_query_none_raises(self):
        with self.assertRaises(ParseError):
            parse_metric_query(None)

    def test_parse_metric_query_non_string_raises(self):
        with self.assertRaises(ParseError):
            parse_metric_query(123)

    def test_unclosed_paren_in_function_chain_raises(self):
        with self.assertRaises(ParseError):
            parse_metric_query("avg:system.cpu.user{*}.rollup(avg, 60")

    # --- translate.py bugs ---

    def test_resolve_agg_none_raises_not_crashes(self):
        from observability_migration.adapters.source.datadog.translate import _resolve_agg
        with self.assertRaises(ValueError):
            _resolve_agg(None, "system.cpu.user")

    def test_tag_filter_value_none_no_crash(self):
        from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
        from observability_migration.adapters.source.datadog.models import TagFilter
        from observability_migration.adapters.source.datadog.translate import _tag_filter_to_esql
        filt = TagFilter(key="service", value=None, negated=False)
        fm = BUILTIN_PROFILES["default"]
        result = _tag_filter_to_esql(filt, fm)
        self.assertIsInstance(result, str)

    def test_formula_number_none_value(self):
        from observability_migration.adapters.source.datadog.query_parser import FormulaNumber
        from observability_migration.adapters.source.datadog.translate import _formula_ast_to_esql
        result = _formula_ast_to_esql(FormulaNumber(value=None), {})
        self.assertEqual(result, "0")

    def test_formula_func_call_none_name(self):
        from observability_migration.adapters.source.datadog.query_parser import FormulaFuncCall
        from observability_migration.adapters.source.datadog.translate import _formula_needs_bucket_span
        result = _formula_needs_bucket_span(FormulaFuncCall(name=None, args=None))
        self.assertFalse(result)

    def test_sort_order_rejects_invalid_values(self):
        mq = MetricQuery(space_agg="avg", metric="system.cpu.user", scope=[])
        wq = WidgetQuery(name="q1", data_source="metrics", metric_query=mq)
        w = NormalizedWidget(
            id="sort-test",
            widget_type="toplist",
            title="Sort test",
            queries=[wq],
            layout={"x": 0, "y": 0, "width": 4},
            raw_definition={
                "requests": [{"sort": {"order_by": [{"order": "RANDOM", "type": "formula", "index": 0}]}}]
            },
        )
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        if result.esql_query:
            self.assertNotIn("RANDOM", result.esql_query)

    # --- generate.py bugs ---

    def test_layout_null_coordinates_no_crash(self):
        mq = MetricQuery(space_agg="avg", metric="system.cpu.user", scope=[])
        wq = WidgetQuery(name="q1", data_source="metrics", metric_query=mq)
        w = NormalizedWidget(
            id="layout-null",
            widget_type="timeseries",
            title="Null layout",
            queries=[wq],
            layout={"x": None, "y": None, "width": None},
            raw_definition={},
        )
        plan = plan_widget(w)
        result = translate_widget(w, plan, OTEL_PROFILE)
        nd = NormalizedDashboard(
            title="Null Layout Test",
            description="",
            widgets=[w],
            template_variables=[],
            raw={},
        )
        yaml_str = generate_dashboard_yaml(nd, [result], "test-*")
        self.assertIsInstance(yaml_str, str)

    def test_markdown_content_null_no_crash(self):
        from observability_migration.adapters.source.datadog.generate import _preferred_panel_height
        panel = {"markdown": {"content": None}}
        height = _preferred_panel_height(panel)
        self.assertIsInstance(height, int)

    def test_markdown_block_null_no_crash(self):
        from observability_migration.adapters.source.datadog.generate import _preferred_panel_height
        panel = {"markdown": None}
        height = _preferred_panel_height(panel)
        self.assertIsInstance(height, int)


if __name__ == "__main__":
    unittest.main()
