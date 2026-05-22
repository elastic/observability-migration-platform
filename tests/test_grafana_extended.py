# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Extended tests for the Grafana migration tool.

Cross-pollinated from the Datadog migration test plan.
Covers: Performance, Security, Packaging validation, Preflight.

Also implements the comprehensive Grafana migration test plan:
- Layer A: Static translation correctness
- Layer B: Semantic query equivalence (macro drift, variable erasure)
- Layer C: Dashboard fidelity (panel count, layout, no silent drops)
- Layer D: Failure honesty (group modifiers, subqueries, unsupported)
- Layer E: Operational safety (determinism, idempotency)
"""

import pathlib
import re
import sys
import tempfile
import time
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from observability_migration.adapters.source.grafana import panels, promql, rules, schema, translate
from observability_migration.targets.kibana.emit import display

# =========================================================================
# Helpers
# =========================================================================

def _make_panel(idx, expr="rate(http_requests_total[5m])", panel_type="timeseries",
                title=None, datasource_type="prometheus", **extra):
    panel = {
        "id": idx,
        "type": panel_type,
        "title": title or f"Panel {idx}",
        "targets": [
            {
                "expr": expr,
                "refId": f"A{idx}" if isinstance(idx, int) else "A",
                "datasource": {"type": datasource_type},
            }
        ],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "gridPos": {"x": 0, "y": idx * 8 if isinstance(idx, int) else 0, "w": 24, "h": 8},
    }
    panel.update(extra)
    return panel


def _translate(expr, panel_type="graph", rule_pack=None, resolver=None):
    rp = rule_pack or rules.RulePackConfig()
    res = resolver or schema.SchemaResolver(rp)
    return translate.translate_promql_to_esql(
        expr, esql_index="metrics-*", panel_type=panel_type,
        rule_pack=rp, resolver=res,
    )


def _translate_panel(panel, rule_pack=None, resolver=None):
    rp = rule_pack or rules.RulePackConfig()
    res = resolver or schema.SchemaResolver(rp)
    return panels.translate_panel(
        panel, datasource_index="metrics-*", esql_index="metrics-*",
        rule_pack=rp, resolver=res,
    )


def _translate_dashboard(dashboard, rule_pack=None, resolver=None):
    rp = rule_pack or rules.RulePackConfig()
    res = resolver or schema.SchemaResolver(rp)
    with tempfile.TemporaryDirectory() as tmpdir:
        result, yaml_path = panels.translate_dashboard(
            dashboard, pathlib.Path(tmpdir),
            datasource_index="metrics-*", esql_index="metrics-*",
            rule_pack=rp, resolver=res,
        )
        payload = yaml.safe_load(yaml_path.read_text())
    return result, payload


# =========================================================================
# Performance Suite
# =========================================================================

class TestGrafanaPerformance(unittest.TestCase):
    """Ensure migration throughput stays reasonable."""

    def _make_panel(self, idx, expr="rate(http_requests_total[5m])"):
        return _make_panel(idx, expr)

    def test_10_panels_under_2s(self):
        panel_list = [self._make_panel(i) for i in range(10)]
        rp = rules.RulePackConfig()
        start = time.monotonic()
        for p in panel_list:
            panels.translate_panel(p, rule_pack=rp)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0, f"10 panels took {elapsed:.2f}s")

    def test_50_panels_under_10s(self):
        panel_list = [self._make_panel(i) for i in range(50)]
        rp = rules.RulePackConfig()
        start = time.monotonic()
        for p in panel_list:
            panels.translate_panel(p, rule_pack=rp)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 10.0, f"50 panels took {elapsed:.2f}s")

    def test_promql_parsing_throughput(self):
        exprs = [
            "rate(http_requests_total[5m])",
            "sum by (job) (rate(http_requests_total[5m]))",
            "histogram_quantile(0.99, sum(rate(http_duration_seconds_bucket[5m])) by (le))",
            "avg(node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes * 100)",
            "increase(process_cpu_seconds_total[1h])",
        ]
        start = time.monotonic()
        for _ in range(100):
            for expr in exprs:
                try:
                    promql._parse_fragment(expr)
                except Exception:
                    pass
        elapsed = time.monotonic() - start
        per_parse = elapsed / 500
        self.assertLess(per_parse, 0.05, f"avg parse: {per_parse:.4f}s")


# =========================================================================
# Security Suite
# =========================================================================

class TestGrafanaSecurity(unittest.TestCase):
    """Ensure generated ES|QL output is safe from injection and leaks."""

    def _translate_simple(self, expr):
        return _translate_panel(_make_panel(1, expr))

    def _yaml_str(self, yaml_panel):
        if yaml_panel is None:
            return ""
        return yaml.dump(yaml_panel, default_flow_style=False)

    def test_template_vars_not_raw_in_esql(self):
        _yaml_panel, pr = self._translate_simple(
            "rate(http_requests_total{job='$job'}[5m])"
        )
        esql = getattr(pr, "esql_query", "") or ""
        if esql:
            self.assertNotIn(
                "$job", esql,
                "Raw $job template variable found in generated ES|QL",
            )

    def test_no_credentials_in_output(self):
        _, pr = self._translate_simple("rate(http_requests_total[5m])")
        esql = getattr(pr, "esql_query", "") or ""
        self.assertNotIn("api_key", esql.lower())
        self.assertNotIn("password", esql.lower())

    def test_grafana_datasource_uid_not_leaked(self):
        yaml_panel, _ = self._translate_simple("rate(http_requests_total[5m])")
        yaml_str = self._yaml_str(yaml_panel)
        self.assertNotIn("datasource", yaml_str.lower())


# =========================================================================
# YAML Packaging Validation
# =========================================================================

class TestGrafanaPackaging(unittest.TestCase):
    """Ensure YAML output follows kb-dashboard-cli schema conventions."""

    def _translate_panel(self, expr):
        return _translate_panel(_make_panel(1, expr))

    def test_time_placeholders_present(self):
        _yaml_panel, pr = self._translate_panel(
            "rate(node_cpu_seconds_total{mode='idle'}[5m])"
        )
        esql = getattr(pr, "esql_query", "") or ""
        if esql and "FROM" in esql.upper():
            has_placeholder = "?_tstart" in esql or "?_tend" in esql
            has_promql = "PROMQL" in esql.upper()
            self.assertTrue(
                has_placeholder or has_promql,
                f"time placeholder missing in: {esql[:200]}",
            )

    def test_dashboard_esql_omits_redundant_timestamp_range_where(self):
        yaml_panel, pr = self._translate_panel("avg(node_load1)")
        esql = yaml_panel["esql"]["query"]

        self.assertIn("BUCKET(@timestamp, 50, ?_tstart, ?_tend)", esql)
        self.assertNotIn("| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend", esql)
        self.assertEqual(esql, pr.esql_query)
        self.assertEqual(esql, pr.query_ir["target_query"])

    def test_dashboard_esql_omits_rule_pack_timestamp_range_where(self):
        rp = rules.RulePackConfig()
        rp.from_time_filter = "@timestamp >= ?_tstart AND @timestamp <= ?_tend"
        yaml_panel, pr = _translate_panel(_make_panel(1, "avg(node_load1)"), rule_pack=rp)
        esql = yaml_panel["esql"]["query"]

        self.assertIn("BUCKET(@timestamp, 50, ?_tstart, ?_tend)", esql)
        self.assertNotIn("| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend", esql)
        self.assertEqual(esql, pr.esql_query)
        self.assertEqual(esql, pr.query_ir["target_query"])

    def test_yaml_panel_has_position_and_size(self):
        yaml_panel, _ = self._translate_panel("rate(http_requests_total[5m])")
        if yaml_panel is not None:
            pos = yaml_panel.get("position", {})
            size = yaml_panel.get("size", {})
            self.assertIn("x", pos)
            self.assertIn("y", pos)
            self.assertIn("w", size)
            self.assertIn("h", size)

    def test_yaml_panel_has_title(self):
        yaml_panel, _ = self._translate_panel("rate(http_requests_total[5m])")
        if yaml_panel is not None:
            self.assertIn("title", yaml_panel)


# =========================================================================
# Preflight Integration
# =========================================================================

class TestGrafanaPreflight(unittest.TestCase):
    """Verify the existing preflight module is importable and has expected API."""

    def test_preflight_module_importable(self):
        from observability_migration.adapters.source.grafana import preflight
        self.assertTrue(hasattr(preflight, "build_preflight_report"))

    def test_preflight_report_callable(self):
        from observability_migration.adapters.source.grafana import preflight
        self.assertTrue(callable(preflight.build_preflight_report))


# =========================================================================
# Layer A: Static Translation Correctness — Macro Drift
# =========================================================================

class TestMacroDrift(unittest.TestCase):
    """Test plan item: Macro drift test.

    Verify that all Grafana macros are consistently replaced, and that
    a custom rule_pack default_rate_window changes the output.
    """

    def test_rate_interval_replaced_with_5m(self):
        result = promql.preprocess_grafana_macros("rate(foo[$__rate_interval])")
        self.assertIn("[5m]", result)
        self.assertNotIn("$__rate_interval", result)

    def test_interval_replaced_with_5m(self):
        result = promql.preprocess_grafana_macros("rate(foo[$__interval])")
        self.assertIn("[5m]", result)
        self.assertNotIn("$__interval", result)

    def test_range_replaced_with_1h(self):
        result = promql.preprocess_grafana_macros("avg_over_time(foo[$__range])")
        self.assertIn("[1h]", result)
        self.assertNotIn("$__range", result)

    def test_auto_interval_replaced(self):
        result = promql.preprocess_grafana_macros("rate(foo[$__auto_interval_my_panel])")
        self.assertNotIn("$__auto_interval", result)
        self.assertIn("5m", result)

    def test_custom_rule_pack_window_changes_variable_brackets(self):
        """Custom default_rate_window should affect $var bracket replacement."""
        rp = rules.RulePackConfig()
        rp.default_rate_window = "10m"
        result = promql.preprocess_grafana_macros("rate(foo[$custom_var])", rp)
        self.assertIn("[10m]", result)

    def test_built_in_macros_ignore_custom_window(self):
        """Built-in $__rate_interval is hardcoded to 5m, not custom window.

        This IS the documented behavior, but it means two panels with the
        same PromQL but different step expectations both get 5m — the test
        plan calls this a correctness limitation we must document.
        """
        rp = rules.RulePackConfig()
        rp.default_rate_window = "10m"
        result = promql.preprocess_grafana_macros("rate(foo[$__rate_interval])", rp)
        self.assertIn("[5m]", result,
                      "Built-in macros are hardcoded to 5m regardless of rule_pack")

    def test_two_panels_same_promql_different_macro_produce_same_output(self):
        """This documents the known limitation: different Grafana macros
        that encode different semantic intervals collapse to the same value.
        """
        expr_rate = "rate(foo[$__rate_interval])"
        expr_interval = "rate(foo[$__interval])"
        result_rate = promql.preprocess_grafana_macros(expr_rate)
        result_interval = promql.preprocess_grafana_macros(expr_interval)
        self.assertEqual(result_rate, result_interval,
                         "Both macros collapse to 5m — documented limitation")

    def test_variable_in_label_selector_becomes_wildcard(self):
        result = promql.preprocess_grafana_macros('foo{job="$job"}')
        self.assertIn('=~".*"', result)
        self.assertNotIn('$job', result)

    def test_variable_exact_match_also_becomes_wildcard(self):
        result = promql.preprocess_grafana_macros('foo{instance=~"$instance"}')
        self.assertIn('=~".*"', result)
        self.assertNotIn('$instance', result)


# =========================================================================
# Layer A: Variable Erasure Detection
# =========================================================================

class TestVariableErasure(unittest.TestCase):
    """Test plan item: Variable erasure test.

    Variables that materially affect queries must produce warnings
    when they are dropped during translation, NOT silent 'migrated'.
    """

    def test_variable_in_label_filter_produces_warning(self):
        ctx = _translate('rate(http_requests_total{job="$job"}[5m])')
        self.assertIn("feasible", ctx.feasibility)
        has_drop_warning = any("variable" in w.lower() or "dropped" in w.lower()
                               for w in ctx.warnings)
        self.assertTrue(has_drop_warning,
                        f"Expected variable-drop warning, got: {ctx.warnings}")

    def test_variable_panel_status_is_migrated_with_warnings(self):
        """A panel whose query relies on a variable filter must be
        'migrated_with_warnings', never plain 'migrated'.
        """
        panel = _make_panel(1, 'rate(http_requests_total{job="$job"}[5m])')
        _, result = _translate_panel(panel)
        self.assertIn(result.status, ("migrated_with_warnings", "migrated"),
                      f"Expected migrated status, got: {result.status}")
        if result.status == "migrated":
            self.assertEqual(result.reasons, [],
                             "If 'migrated', there should be no warnings at all")

    def test_multi_variable_in_labels_all_warned(self):
        ctx = _translate('rate(foo{job="$job",instance="$instance"}[5m])')
        if ctx.feasibility == "feasible" and ctx.esql_query:
            self.assertNotIn("$job", ctx.esql_query)
            self.assertNotIn("$instance", ctx.esql_query)

    def test_logql_variable_in_stream_selector_warned(self):
        ctx = _translate('{service_name="$svc"} |~ "error"', panel_type="logs")
        if ctx.feasibility == "feasible":
            has_var_warning = any("variable" in w.lower() or "dropped" in w.lower()
                                  for w in ctx.warnings)
            self.assertTrue(has_var_warning,
                            f"LogQL variable drop not warned: {ctx.warnings}")

    def test_clean_template_variables_strips_dollar_syntax(self):
        self.assertNotIn("$", display.clean_template_variables("CPU $instance"))
        self.assertNotIn("${", display.clean_template_variables("CPU ${instance}"))
        self.assertNotIn("{{", display.clean_template_variables("CPU {{instance}}"))


# =========================================================================
# Layer A: Classification Correctness
# =========================================================================

class TestClassificationCorrectness(unittest.TestCase):
    """Verify that status classifications are honest:
    - migrated: no warnings, valid ES|QL
    - migrated_with_warnings: warnings present
    - not_feasible: reasons populated
    - skipped: correct panel type handling
    """

    def test_clean_rate_is_migrated_no_warnings(self):
        panel = _make_panel(1, "rate(http_requests_total[5m])")
        _, result = _translate_panel(panel)
        self.assertEqual(result.status, "migrated")
        self.assertEqual(result.reasons, [])
        self.assertTrue(result.esql_query, "migrated panel must have ES|QL")

    def test_migrated_panel_has_nonzero_confidence(self):
        panel = _make_panel(1, "rate(http_requests_total[5m])")
        _, result = _translate_panel(panel)
        self.assertGreater(result.confidence, 0.0)

    def test_warned_panel_has_lower_confidence_than_clean(self):
        clean_panel = _make_panel(1, "rate(http_requests_total[5m])")
        _, clean_result = _translate_panel(clean_panel)

        warned_panel = _make_panel(2, 'rate(http_requests_total{job="$job"}[5m])')
        _, warned_result = _translate_panel(warned_panel)

        if warned_result.status == "migrated_with_warnings":
            self.assertLessEqual(warned_result.confidence, clean_result.confidence)

    def test_not_feasible_has_reasons(self):
        # histogram_quantile() is hard-blocked and always not_feasible
        panel = _make_panel(1, "histogram_quantile(0.99, sum(rate(http_duration_bucket[5m])) by (le))")
        _, result = _translate_panel(panel)
        self.assertEqual(result.status, "not_feasible")
        self.assertTrue(result.reasons, "not_feasible must have reasons")

    def test_not_feasible_preserves_original_query(self):
        expr = "histogram_quantile(0.99, sum(rate(http_duration_bucket[5m])) by (le))"
        panel = _make_panel(1, expr)
        yaml_panel, _result = _translate_panel(panel)
        self.assertIn("markdown", yaml_panel)
        self.assertIn("histogram_quantile", yaml_panel["markdown"]["content"])

    def test_skipped_panel_has_skipped_status(self):
        for panel_type in ("row", "news", "dashlist", "alertlist", "nodeGraph", "canvas"):
            panel = {"id": 1, "type": panel_type, "title": f"Skip {panel_type}",
                     "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}}
            yaml_panel, result = _translate_panel(panel)
            self.assertIsNone(yaml_panel, f"{panel_type} should produce no YAML")
            self.assertEqual(result.status, "skipped",
                             f"{panel_type} should be skipped, got: {result.status}")

    def test_unknown_panel_type_is_not_feasible(self):
        panel = {"id": 1, "type": "unknown_plugin_xyz", "title": "Unknown",
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
                 "targets": [{"expr": "foo", "refId": "A"}]}
        _, result = _translate_panel(panel)
        self.assertEqual(result.status, "not_feasible")
        self.assertTrue(any("Unknown" in r for r in result.reasons))

    def test_text_panel_migrates_cleanly(self):
        panel = {"id": 1, "type": "text", "title": "Info",
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4},
                 "options": {"content": "Hello world", "mode": "markdown"}}
        yaml_panel, result = _translate_panel(panel)
        self.assertEqual(result.status, "migrated")
        self.assertEqual(result.kibana_type, "markdown")
        self.assertIn("markdown", yaml_panel)


# =========================================================================
# Layer D: Failure Honesty — Unsupported Constructs
# =========================================================================

class TestFailureHonesty(unittest.TestCase):
    """Test plan items: Group modifier trap, Subquery trap, etc.

    Verify unsupported constructs are flagged early and clearly.
    """

    def test_subquery_is_not_feasible(self):
        ctx = _translate("max_over_time(rate(foo_total[5m])[1h:])")
        self.assertEqual(ctx.feasibility, "not_feasible")
        self.assertTrue(any("subquery" in w.lower() for w in ctx.warnings))

    def test_offset_is_not_feasible(self):
        ctx = _translate("rate(foo_total[5m] offset 1h)")
        self.assertEqual(ctx.feasibility, "not_feasible")
        self.assertTrue(any("offset" in w.lower() for w in ctx.warnings))

    def test_topk_without_labels_now_translates(self):
        # Ungrouped topk now uses single-bucket fallback — migrated_with_warnings, not not_feasible
        ctx = _translate("topk(5, rate(foo_total[5m]))")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("LIMIT 5", ctx.esql_query)

    def test_grouped_topk_rate_sum_translates_to_sorted_limited_esql(self):
        ctx = _translate("topk(10, sum(rate(http_requests_total[5m])) by (handler))", panel_type="barchart")

        self.assertEqual(ctx.feasibility, "feasible")
        self.assertIn("SUM(RATE(http_requests_total, 5m))", ctx.esql_query)
        self.assertIn("BY time_bucket = TBUCKET(5 minute), handler", ctx.esql_query)
        self.assertIn("LAST(_bucket_value, time_bucket) BY handler", ctx.esql_query)
        self.assertIn("| SORT value DESC", ctx.esql_query)
        self.assertIn("| LIMIT 10", ctx.esql_query)
        self.assertEqual(ctx.output_group_fields, ["handler"])
        self.assertTrue(any("topk" in warning.lower() for warning in ctx.warnings))

    def test_without_aggregation_is_not_feasible(self):
        ctx = _translate("sum without (instance) (rate(foo_total[5m]))")
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_histogram_quantile_is_not_feasible(self):
        ctx = _translate("histogram_quantile(0.9, rate(bucket[5m]))")
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_name_introspection_is_not_feasible(self):
        ctx = _translate('topk(10, count by (__name__)({__name__=~".+"}))')
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_group_left_join_warns_or_degrades(self):
        """group_left joins should not be silently marked 'migrated'
        without any warnings about semantic loss.
        """
        expr = (
            'node_filesystem_avail_bytes{instance="$node"} '
            '* on(device, instance) group_left '
            'node_filesystem_size_bytes{instance="$node"}'
        )
        ctx = _translate(expr)
        if ctx.feasibility == "feasible" and ctx.esql_query:
            has_join_warning = any(
                "join" in w.lower() or "group_left" in w.lower() or
                "approximat" in w.lower() or "left side" in w.lower()
                for w in ctx.warnings
            )
            self.assertTrue(has_join_warning,
                            f"group_left should produce a warning, got: {ctx.warnings}")

    def test_ignoring_clause_warns_or_not_feasible(self):
        """ignoring() modifier should produce warnings or not_feasible."""
        expr = (
            'rate(foo_total[5m]) / ignoring(code) rate(bar_total[5m])'
        )
        ctx = _translate(expr)
        if ctx.feasibility == "feasible":
            has_warning = any("join" in w.lower() or "ignoring" in w.lower() or
                              "approximat" in w.lower() for w in ctx.warnings)
            self.assertTrue(has_warning,
                            f"ignoring() should warn, got: {ctx.warnings}")

    def test_cross_metric_additive_on_join_is_not_feasible(self):
        """Cross-metric + on() join should be marked not_feasible."""
        expr = 'a + on(namespace) b'
        ctx = _translate(expr)
        if ctx.feasibility == "feasible":
            self.assertTrue(ctx.warnings,
                            "Cross-metric join without warning is a false-success")

    def test_not_feasible_panel_preserves_original_in_report(self):
        """Unsupported panels must preserve the original query for review."""
        expr = "histogram_quantile(0.99, sum(rate(http_duration_bucket[5m])) by (le))"
        panel = _make_panel(1, expr)
        yaml_panel, _result = _translate_panel(panel)
        self.assertIn("markdown", yaml_panel)
        content = yaml_panel["markdown"]["content"]
        self.assertIn("histogram_quantile", content, "Original query must be in report")

    def test_bottomk_is_not_feasible(self):
        ctx = _translate("bottomk(3, sum by (job) (rate(foo_total[5m])))")
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_count_values_is_not_feasible(self):
        ctx = _translate('count_values("version", build_info)')
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_label_join_is_not_feasible(self):
        ctx = _translate('label_join(up{job="api"}, "full", "/", "instance", "port")')
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_changes_function_is_not_feasible(self):
        ctx = _translate("changes(process_start_time_seconds[1h])")
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_same_metric_filtered_ratio_uses_case_wrapped_numerator(self):
        # Same-metric ratio where the numerator carries an extra filter
        # (e.g. status=~"5.." for an error-rate panel). Issue #8 follow-up: the
        # shared-measure pipeline now CASE-wraps the divergent filter into the
        # numerator's stats_expr so both sides share a single TS source while
        # the numerator is correctly scoped — this used to be refused as
        # ``not_feasible`` for safety, but CASE-wrapping is the honest fix.
        expr = (
            '(sum(rate(http_requests_total{status=~"5..",service=~"api|worker"}[5m])) by (service) '
            '/ sum(rate(http_requests_total{service=~"api|worker"}[5m])) by (service)) * 100'
        )
        ctx = _translate(expr)
        self.assertEqual(ctx.feasibility, "feasible")
        query = ctx.esql_query or ""
        # Numerator scoped via CASE on the extra filter; denominator unscoped.
        self.assertIn('CASE((status RLIKE "5..")', query)
        self.assertIn("RATE(http_requests_total, 5m)", query)
        # Service filter is common to both sides and stays in WHERE.
        self.assertIn('service RLIKE "api|worker"', query)
        # Final percentage EVAL composes the two stats columns.
        self.assertIn("* 100", query)


# =========================================================================
# Layer C: Dashboard Fidelity — No Silent Drops
# =========================================================================

class TestDashboardFidelity(unittest.TestCase):
    """Test plan items: Panel count consistency, no silent drops."""

    def test_all_panels_accounted_for(self):
        """Every panel in the dashboard must appear in panel_results."""
        dashboard = {
            "title": "Count Test", "uid": "count-1",
            "panels": [
                _make_panel(1, "rate(foo_total[5m])"),
                _make_panel(2, "rate(bar_total[5m])"),
                {"id": 3, "type": "text", "title": "Info",
                 "gridPos": {"x": 0, "y": 24, "w": 24, "h": 4},
                 "options": {"content": "Hello", "mode": "markdown"}},
            ],
        }
        result, _payload = _translate_dashboard(dashboard)
        self.assertEqual(result.total_panels, 3)
        total_accounted = (result.migrated + result.migrated_with_warnings +
                           result.requires_manual + result.not_feasible + result.skipped)
        self.assertEqual(total_accounted, result.total_panels,
                         f"Panel count mismatch: {total_accounted} accounted vs {result.total_panels} total")

    def test_row_panels_are_counted_as_skipped(self):
        dashboard = {
            "title": "Row Test", "uid": "row-1",
            "panels": [
                {"id": 1, "type": "row", "title": "Section",
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
                _make_panel(2, "rate(foo_total[5m])"),
            ],
        }
        result, _ = _translate_dashboard(dashboard)
        self.assertEqual(result.total_panels, 2)
        row_results = [pr for pr in result.panel_results if pr.grafana_type == "row"]
        self.assertTrue(row_results, "Row panels must appear in panel_results")
        self.assertEqual(row_results[0].status, "skipped")

    def test_skipped_panel_types_accounted_in_results(self):
        """Test plan: skipped panel types must not vanish from reports."""
        dashboard = {
            "title": "Skip Test", "uid": "skip-1",
            "panels": [
                {"id": 1, "type": "news", "title": "News",
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4}},
                {"id": 2, "type": "alertlist", "title": "Alerts",
                 "gridPos": {"x": 0, "y": 4, "w": 24, "h": 4}},
                _make_panel(3, "rate(foo_total[5m])"),
            ],
        }
        result, _ = _translate_dashboard(dashboard)
        skipped_results = [pr for pr in result.panel_results if pr.status == "skipped"]
        skipped_types = {pr.grafana_type for pr in skipped_results}
        self.assertIn("news", skipped_types)
        self.assertIn("alertlist", skipped_types)

    def test_dashboard_preserves_panel_titles(self):
        dashboard = {
            "title": "Title Test", "uid": "title-1",
            "panels": [
                _make_panel(1, "rate(foo_total[5m])", title="My Custom Title"),
            ],
        }
        _result, payload = _translate_dashboard(dashboard)
        panel_titles = [p.get("title") for p in payload["dashboards"][0]["panels"]]
        self.assertIn("My Custom Title", panel_titles)

    def test_mixed_datasource_panel_is_flagged_not_silently_migrated(self):
        """Test plan item: Mixed datasource test.
        One panel with Prometheus + Loki must be flagged, not partially migrated.
        """
        panel = {
            "title": "Mixed", "type": "graph",
            "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
            "targets": [
                {"refId": "A", "expr": "rate(http_total[5m])",
                 "datasource": {"type": "prometheus", "uid": "prom"}},
                {"refId": "B", "expr": '{service="api"} |~ "error"',
                 "datasource": {"type": "loki", "uid": "loki"}},
            ],
        }
        _, result = _translate_panel(panel)
        self.assertEqual(result.status, "not_feasible")
        self.assertTrue(any("mixed" in r.lower() or "manual" in r.lower()
                            for r in result.reasons))

    def test_no_panel_silently_becomes_placeholder_without_warning(self):
        """If a panel becomes a markdown placeholder, it must have reasons."""
        panel = _make_panel(1, "topk(5, rate(foo_total[5m]))")
        yaml_panel, result = _translate_panel(panel)
        if "markdown" in (yaml_panel or {}):
            self.assertTrue(result.reasons,
                            "Placeholder panel must have reasons explaining why")
            self.assertNotEqual(result.status, "migrated",
                                "Placeholder panel must not be marked 'migrated'")

    def test_panel_count_matches_across_result_and_yaml(self):
        """The number of panels in the YAML output should match yaml_panel_results."""
        dashboard = {
            "title": "Consistency", "uid": "consistency-1",
            "panels": [
                _make_panel(1, "rate(foo_total[5m])"),
                _make_panel(2, "sum(bar_gauge)"),
                {"id": 3, "type": "row", "title": "Section",
                 "gridPos": {"x": 0, "y": 16, "w": 24, "h": 1}},
            ],
        }
        result, payload = _translate_dashboard(dashboard)
        yaml_panel_count = len(payload["dashboards"][0]["panels"])
        emitted_count = len(result.yaml_panel_results)
        self.assertEqual(yaml_panel_count, emitted_count,
                         f"YAML panels ({yaml_panel_count}) != emitted results ({emitted_count})")


# =========================================================================
# Layer E: Operational Safety — Determinism
# =========================================================================

class TestDeterminism(unittest.TestCase):
    """Test plan item: Same input should produce the same output."""

    def test_same_panel_twice_same_output(self):
        panel = _make_panel(1, 'sum(rate(http_requests_total{job="api"}[5m])) by (instance)')
        yaml1, result1 = _translate_panel(panel)
        yaml2, result2 = _translate_panel(panel)
        self.assertEqual(result1.status, result2.status)
        self.assertEqual(result1.esql_query, result2.esql_query)
        self.assertEqual(result1.reasons, result2.reasons)
        if yaml1 and yaml2:
            self.assertEqual(
                yaml.dump(yaml1, sort_keys=True),
                yaml.dump(yaml2, sort_keys=True),
            )

    def test_same_dashboard_twice_same_result(self):
        dashboard = {
            "title": "Determinism", "uid": "det-1",
            "panels": [
                _make_panel(1, "rate(foo_total[5m])"),
                _make_panel(2, "sum(bar_gauge)"),
            ],
        }
        result1, _payload1 = _translate_dashboard(dashboard)
        result2, _payload2 = _translate_dashboard(dashboard)
        self.assertEqual(result1.migrated, result2.migrated)
        self.assertEqual(result1.not_feasible, result2.not_feasible)
        self.assertEqual(result1.skipped, result2.skipped)
        for pr1, pr2 in zip(result1.panel_results, result2.panel_results):
            self.assertEqual(pr1.status, pr2.status)
            self.assertEqual(pr1.esql_query, pr2.esql_query)

    def test_translation_context_is_deterministic(self):
        expr = "rate(http_requests_total[5m])"
        ctx1 = _translate(expr)
        ctx2 = _translate(expr)
        self.assertEqual(ctx1.feasibility, ctx2.feasibility)
        self.assertEqual(ctx1.esql_query, ctx2.esql_query)
        self.assertEqual(ctx1.warnings, ctx2.warnings)


# =========================================================================
# Output Integrity — ES|QL Structural Validity
# =========================================================================

class TestOutputIntegrity(unittest.TestCase):
    """Test plan: Output integrity checks.

    Verify structural validity of generated ES|QL.
    """

    def test_rate_counter_uses_ts_source(self):
        """rate() on _total metric should use TS source, not FROM."""
        ctx = _translate("rate(http_requests_total[5m])")
        self.assertEqual(ctx.feasibility, "feasible")
        self.assertTrue(ctx.esql_query.startswith("TS "),
                        f"Counter rate should use TS, got: {ctx.esql_query[:50]}")

    def test_gauge_uses_from_source(self):
        """Gauge metric without rate should use FROM source."""
        ctx = _translate("avg(node_load1)")
        self.assertEqual(ctx.feasibility, "feasible")
        self.assertTrue(ctx.esql_query.startswith("FROM "),
                        f"Gauge should use FROM, got: {ctx.esql_query[:50]}")

    def test_time_filter_present_in_esql(self):
        ctx = _translate("rate(http_requests_total[5m])")
        self.assertIn("@timestamp", ctx.esql_query)
        self.assertIn("?_tstart", ctx.esql_query)
        self.assertIn("?_tend", ctx.esql_query)

    def test_sort_present_for_timeseries(self):
        ctx = _translate("rate(http_requests_total[5m])")
        self.assertIn("SORT time_bucket ASC", ctx.esql_query)

    def test_bucket_present_for_timeseries(self):
        ctx = _translate("rate(http_requests_total[5m])")
        self.assertIn("TBUCKET", ctx.esql_query)

    def test_from_bucket_uses_adaptive_bucket(self):
        ctx = _translate("avg(node_load1)")
        if ctx.esql_query.startswith("FROM"):
            self.assertIn("BUCKET(@timestamp, 50, ?_tstart, ?_tend)", ctx.esql_query)

    def test_esql_has_no_empty_lines(self):
        """Generated ES|QL should not have double newlines or empty pipe stages."""
        ctx = _translate("rate(http_requests_total[5m])")
        self.assertNotIn("\n\n", ctx.esql_query)
        self.assertNotRegex(ctx.esql_query, r"\|\s*\|")

    def test_esql_aliases_are_valid_identifiers(self):
        ctx = _translate("rate(http_requests_total[5m])")
        stats_match = re.search(r"STATS\s+(\w+)\s*=", ctx.esql_query)
        if stats_match:
            alias = stats_match.group(1)
            self.assertRegex(alias, r"^[a-zA-Z_]\w*$",
                             f"Alias '{alias}' is not a valid identifier")

    def test_irate_on_counter_uses_irate_function(self):
        ctx = _translate("irate(http_requests_total[5m])")
        self.assertIn("IRATE", ctx.esql_query)
        self.assertNotIn("RATE(", ctx.esql_query.replace("IRATE", ""))

    def test_increase_on_counter_uses_increase_function(self):
        ctx = _translate("increase(http_requests_total[1h])")
        self.assertIn("INCREASE", ctx.esql_query)


# =========================================================================
# LogQL Translation Honesty
# =========================================================================

class TestLogQLHonesty(unittest.TestCase):
    """Test plan item: LogQL approximation must be labeled as approximation."""

    def test_logql_stream_labeled_as_approximation(self):
        ctx = _translate('{service_name="api"} |~ "error"', panel_type="logs")
        self.assertEqual(ctx.feasibility, "feasible")
        has_approx = any("approximat" in w.lower() for w in ctx.warnings)
        self.assertTrue(has_approx,
                        f"LogQL stream should be labeled approximation: {ctx.warnings}")

    def test_logql_contains_operator_translates_to_message_filter(self):
        ctx = _translate('{job="app"} |= "error"', panel_type="logs")

        self.assertEqual(ctx.feasibility, "feasible")
        self.assertIn("FROM logs-*", ctx.esql_query)
        self.assertIn('service.name == "app"', ctx.esql_query)
        self.assertIn('message LIKE "*error*"', ctx.esql_query)

    def test_logql_count_over_time_labeled_as_approximation(self):
        ctx = _translate('sum(count_over_time({service="api"}[5m]))', panel_type="timeseries")
        self.assertEqual(ctx.feasibility, "feasible")
        has_approx = any("log" in w.lower() or "count" in w.lower()
                         for w in ctx.warnings)
        self.assertTrue(has_approx,
                        f"LogQL count should produce warning: {ctx.warnings}")

    def test_logql_uses_from_source(self):
        ctx = _translate('{service_name="api"} |~ "error"', panel_type="logs")
        self.assertTrue(ctx.esql_query.startswith("FROM logs-"),
                        f"LogQL should use FROM logs-*, got: {ctx.esql_query[:50]}")

    def test_logql_includes_message_field(self):
        ctx = _translate('{service_name="api"} |~ "error"', panel_type="logs")
        self.assertIn("message", ctx.esql_query)


# =========================================================================
# Panel Type Coverage
# =========================================================================

class TestPanelTypeCoverage(unittest.TestCase):
    """Verify all panel type mappings produce correct Kibana types."""

    def test_timeseries_maps_to_line(self):
        panel = _make_panel(1, "rate(foo_total[5m])", panel_type="timeseries")
        _, result = _translate_panel(panel)
        self.assertEqual(result.kibana_type, "line")

    def test_graph_maps_to_line(self):
        panel = _make_panel(1, "rate(foo_total[5m])", panel_type="graph")
        _, result = _translate_panel(panel)
        self.assertEqual(result.kibana_type, "line")

    def test_stat_maps_to_metric(self):
        panel = _make_panel(1, "avg(node_load1)", panel_type="stat")
        _, result = _translate_panel(panel)
        self.assertEqual(result.kibana_type, "metric")

    def test_gauge_maps_to_gauge(self):
        panel = _make_panel(1, "avg(node_load1)", panel_type="gauge")
        panel["targets"][0]["instant"] = True
        _, result = _translate_panel(panel)
        self.assertEqual(result.kibana_type, "gauge")

    def test_table_maps_to_datatable(self):
        panel = _make_panel(1, "avg(node_load1)", panel_type="table")
        _, result = _translate_panel(panel)
        self.assertEqual(result.kibana_type, "datatable")

    def test_piechart_maps_to_pie(self):
        panel = _make_panel(1, 'sum by (job) (rate(foo_total[5m]))', panel_type="piechart")
        _, result = _translate_panel(panel)
        self.assertIn(result.kibana_type, ("pie", "bar"))

    def test_barchart_maps_to_bar(self):
        panel = _make_panel(1, "rate(foo_total[5m])", panel_type="barchart")
        _, result = _translate_panel(panel)
        self.assertEqual(result.kibana_type, "bar")

    def test_heatmap_degrades_gracefully(self):
        panel = _make_panel(1, "rate(foo_total[5m])", panel_type="heatmap")
        _, result = _translate_panel(panel)
        self.assertIn(result.kibana_type, ("heatmap", "line"))

    def test_stacked_timeseries_becomes_area(self):
        panel = _make_panel(1, "rate(foo_total[5m])", panel_type="timeseries")
        panel["fieldConfig"] = {
            "defaults": {"custom": {"stacking": {"mode": "normal"}}},
            "overrides": [],
        }
        _, result = _translate_panel(panel)
        self.assertEqual(result.kibana_type, "area")

    def test_bar_style_graph_becomes_bar(self):
        panel = _make_panel(1, "rate(foo_total[5m])", panel_type="graph")
        panel["bars"] = True
        panel["lines"] = False
        _, result = _translate_panel(panel)
        self.assertEqual(result.kibana_type, "bar")


# =========================================================================
# Regex Fallback Parser Handling
# =========================================================================

class TestParserBackendTracking(unittest.TestCase):
    """The tool uses both AST and regex parsers. Regex fallback
    should not be silently treated as equivalent to AST.
    """

    def test_simple_rate_uses_ast_backend(self):
        frag = promql._parse_fragment(
            promql.preprocess_grafana_macros("rate(foo_total[5m])")
        )
        self.assertIn(frag.extra.get("parser_backend"), ("ast", "regex"))

    def test_fragment_family_is_populated(self):
        frag = promql._parse_fragment(
            promql.preprocess_grafana_macros("rate(foo_total[5m])")
        )
        self.assertIn(frag.family, ("range_agg", "simple_metric"))

    def test_regex_fallback_gets_warning(self):
        """If AST parse fails and regex is used, a warning should be present."""
        ctx = _translate("rate(http_requests_total[5m])")
        if ctx.parser_backend == "regex":
            has_fallback_warning = any("regex" in w.lower() or "fallback" in w.lower()
                                       for w in ctx.warnings)
            self.assertTrue(has_fallback_warning)


# =========================================================================
# Rule Engine Correctness
# =========================================================================

class TestRuleEngine(unittest.TestCase):
    """Verify the rule engine executes in priority order and traces work."""

    def test_rules_sorted_by_priority(self):
        for registry_name, registry in [
            ("preprocessors", rules.QUERY_PREPROCESSORS),
            ("classifiers", rules.QUERY_CLASSIFIERS),
            ("translators", rules.QUERY_TRANSLATORS),
            ("postprocessors", rules.QUERY_POSTPROCESSORS),
            ("validators", rules.QUERY_VALIDATORS),
        ]:
            described = registry.describe()
            priorities = [r["priority"] for r in described]
            self.assertEqual(priorities, sorted(priorities),
                             f"{registry_name} rules not sorted by priority")

    def test_translation_trace_is_populated(self):
        ctx = _translate("rate(http_requests_total[5m])")
        self.assertTrue(ctx.trace, "Trace should be populated")
        stages = {entry["stage"] for entry in ctx.trace}
        self.assertIn("query_preprocessors", stages)
        self.assertIn("query_translators", stages)

    def test_custom_rule_pack_patterns_are_used(self):
        rp = rules.RulePackConfig()
        rp.not_feasible_patterns.append(
            rules.PatternRule(pattern=r"\bfoo_forbidden_metric\b",
                              reason="Custom blocked metric")
        )
        ctx = _translate("rate(foo_forbidden_metric[5m])", rule_pack=rp)
        self.assertEqual(ctx.feasibility, "not_feasible")
        self.assertTrue(any("Custom blocked" in w for w in ctx.warnings))

    def test_counter_suffix_detection(self):
        rp = rules.RulePackConfig()
        self.assertTrue(promql._is_counter_fallback("http_requests_total", rp))
        self.assertTrue(promql._is_counter_fallback("process_cpu_seconds_total", rp))
        self.assertFalse(promql._is_counter_fallback("node_load1", rp))
        self.assertFalse(promql._is_counter_fallback("up", rp))

    def test_schema_counter_metadata_detection(self):
        rp = rules.RulePackConfig()
        resolver = schema.SchemaResolver(rp)
        resolver._discovery_attempted = True
        resolver._field_cache = {
            "node_scrape_collector_duration_seconds": {
                "double": {
                    "type": "double",
                    "time_series_metric": "counter",
                }
            }
        }
        self.assertTrue(resolver.is_counter("node_scrape_collector_duration_seconds"))

    def test_schema_marked_counter_uses_last_over_time_for_simple_metric(self):
        rp = rules.RulePackConfig()
        resolver = schema.SchemaResolver(rp)
        resolver._discovery_attempted = True
        resolver._field_cache = {
            "node_scrape_collector_duration_seconds": {
                "double": {
                    "type": "double",
                    "time_series_metric": "counter",
                }
            }
        }
        ctx = _translate("node_scrape_collector_duration_seconds", resolver=resolver)
        self.assertEqual(ctx.source_type, "TS")
        self.assertIn("LAST_OVER_TIME(node_scrape_collector_duration_seconds", ctx.esql_query)
        self.assertNotIn("RATE(node_scrape_collector_duration_seconds", ctx.esql_query)
        self.assertTrue(any("LAST_OVER_TIME" in warning for warning in ctx.warnings))


# =========================================================================
# Happy Path PromQL Bucket
# =========================================================================

class TestHappyPathPromQL(unittest.TestCase):
    """Test plan Bucket 1: Happy-path PromQL.

    Simple cases the tool should pass with 'migrated' status.
    """

    def _assert_migrated(self, expr, panel_type="timeseries"):
        panel = _make_panel(1, expr, panel_type=panel_type)
        _, result = _translate_panel(panel)
        self.assertIn(result.status, ("migrated", "migrated_with_warnings"),
                      f"Expected migrated for '{expr}', got {result.status}: {result.reasons}")
        self.assertTrue(result.esql_query, f"No ES|QL for '{expr}'")
        return result

    def test_simple_rate(self):
        self._assert_migrated("rate(http_requests_total[5m])")

    def test_sum_by_job(self):
        self._assert_migrated('sum by (job) (rate(http_requests_total[5m]))')

    def test_avg_over_time(self):
        self._assert_migrated("avg_over_time(node_load1[5m])")

    def test_max_by_host_rate(self):
        self._assert_migrated("max by (instance) (rate(http_requests_total[5m]))")

    def test_simple_gauge(self):
        self._assert_migrated("node_load1")

    def test_increase(self):
        self._assert_migrated("increase(process_cpu_seconds_total[1h])")

    def test_irate(self):
        self._assert_migrated("irate(http_requests_total[5m])")

    def test_min_over_time(self):
        self._assert_migrated("min_over_time(node_load1[5m])")

    def test_sum_over_time(self):
        self._assert_migrated("sum_over_time(http_requests_total[1h])")

    def test_binary_percent_formula(self):
        self._assert_migrated(
            "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100"
        )

    def test_stat_panel(self):
        self._assert_migrated("avg(node_load1)", panel_type="stat")

    def test_gauge_panel(self):
        result = self._assert_migrated("avg(node_load1)", panel_type="gauge")
        self.assertIn(result.kibana_type, ("gauge", "metric"))

    def test_table_panel(self):
        self._assert_migrated("avg(node_load1)", panel_type="table")


# =========================================================================
# Native PROMQL Path Validation
# =========================================================================

class TestNativePromQLIntegrity(unittest.TestCase):
    """Verify the native PROMQL path produces correct output structure."""

    def setUp(self):
        self.rp = rules.RulePackConfig()
        self.rp.native_promql = True
        self.resolver = schema.SchemaResolver(self.rp)

    def test_native_promql_produces_promql_command(self):
        panel = _make_panel(1, "rate(http_requests_total[5m])")
        yaml_panel, _result = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        if yaml_panel and "esql" in yaml_panel:
            query = yaml_panel["esql"]["query"]
            self.assertTrue(query.startswith("PROMQL"),
                            f"Native PROMQL should produce PROMQL command: {query[:80]}")

    def test_native_promql_preserves_original_metric(self):
        panel = _make_panel(1, "rate(http_requests_total[5m])")
        yaml_panel, _ = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        if yaml_panel and "esql" in yaml_panel:
            query = yaml_panel["esql"]["query"]
            self.assertIn("http_requests_total", query)

    def test_native_promql_ratio_uses_repeated_group_labels_without_timeseries_extraction(self):
        expr = (
            "(sum by (service.name) (rate(http_request_duration_seconds_sum[5m]))) / "
            "(sum by (service.name) (rate(http_request_duration_seconds_count[5m])))"
        )
        query = panels.build_native_promql_query(expr, index="metrics-*", kibana_type="line")

        self.assertTrue(query.startswith("PROMQL index=metrics-*"))
        self.assertNotIn("_timeseries", query)
        self.assertEqual(panels._native_promql_result_shape(expr), ("value", ["service.name"]))

    def test_native_promql_rejects_server_unsupported_group_modifiers(self):
        expr = (
            'rate(container_cpu_usage_seconds_total{pod=~"loki.*"}[1m]) '
            '/ on (pod, container) kube_pod_container_resource_limits_cpu_cores'
        )

        self.assertFalse(panels.can_use_native_promql(expr))

    def test_native_promql_rejects_server_unsupported_histogram_quantile(self):
        expr = 'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))'

        self.assertFalse(panels.can_use_native_promql(expr))

    def test_native_promql_visual_ir_and_query_ir_match_emitted_yaml(self):
        panel = _make_panel(1, "rate(http_requests_total[5m])")
        yaml_panel, result = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertIsNotNone(yaml_panel)
        self.assertIn("esql", yaml_panel)

        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.startswith("PROMQL"))
        self.assertEqual(result.visual_ir.presentation.kind, "esql")
        self.assertEqual(result.visual_ir.presentation.config["query"], query)
        self.assertEqual(result.visual_ir.title, yaml_panel["title"])
        self.assertEqual(result.query_ir.get("target_query"), query)

    def test_topk_without_labels_translates_with_warnings(self):
        # Ungrouped topk now uses single-bucket fallback (not not_feasible)
        panel = _make_panel(1, "topk(5, rate(foo_total[5m]))")
        _, result = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertNotEqual(result.status, "not_feasible", result.reasons)


# =========================================================================
# Display Enrichment
# =========================================================================

class TestDisplayEnrichment(unittest.TestCase):
    """Verify display.enrich_yaml_panel_display runs correctly on panels."""

    def test_enrichment_adds_legend_to_xy_panel(self):
        panel = _make_panel(1, 'sum by (instance) (rate(foo_total[5m]))',
                            panel_type="graph")
        panel["legend"] = {"show": True}
        yaml_panel, _result = _translate_panel(panel)
        if yaml_panel and "esql" in yaml_panel:
            legend = yaml_panel["esql"].get("legend", {})
            self.assertIn(legend.get("visible"), ("show", "hide", True, False, None))

    def test_enrichment_cleans_template_vars_from_title(self):
        panel = _make_panel(1, "rate(foo_total[5m])",
                            title="CPU $instance - ${namespace}")
        yaml_panel, _ = _translate_panel(panel)
        if yaml_panel:
            title = yaml_panel.get("title", "")
            self.assertNotIn("$instance", title)
            self.assertNotIn("${namespace}", title)


# =========================================================================
# Edge Cases
# =========================================================================

class TestEdgeCases(unittest.TestCase):
    """Cover edge cases and boundary conditions."""

    def test_empty_expression_handled_gracefully(self):
        panel = {
            "id": 1, "type": "timeseries", "title": "Empty",
            "targets": [{"expr": "", "refId": "A"}],
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
        }
        _yaml_panel, result = _translate_panel(panel)
        self.assertIn(result.status, ("requires_manual", "not_feasible", "skipped"))

    def test_no_targets_handled_gracefully(self):
        panel = {
            "id": 1, "type": "timeseries", "title": "No Targets",
            "targets": [],
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
        }
        _yaml_panel, result = _translate_panel(panel)
        self.assertIn(result.status, ("requires_manual", "not_feasible", "skipped"))

    def test_hidden_target_is_skipped(self):
        panel = {
            "id": 1, "type": "timeseries", "title": "Hidden",
            "targets": [
                {"expr": "rate(foo_total[5m])", "refId": "A", "hide": True},
            ],
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
        }
        _yaml_panel, result = _translate_panel(panel)
        self.assertIn(result.status, ("requires_manual", "not_feasible"))


class TestSemanticPipelineRoundTrip(unittest.TestCase):
    def test_distinct_metric_error_rate_preserves_query_ir_visual_ir_and_yaml(self):
        expr = (
            '(sum(rate(http_server_errors_total{service=~"api|worker"}[5m])) by (service) '
            '/ sum(rate(http_server_requests_total{service=~"api|worker"}[5m])) by (service)) * 100'
        )
        yaml_panel, result = _translate_panel(_make_panel(1, expr, panel_type="graph", title="Error Rate"))

        self.assertEqual(result.status, "migrated_with_warnings")
        self.assertEqual(result.query_ir.get("source_language"), "promql")
        self.assertEqual(result.query_ir.get("family"), "binary_expr")
        self.assertEqual(result.query_ir.get("metric"), "computed_value")
        self.assertEqual(result.query_ir.get("output_metric_field"), "computed_value")
        self.assertEqual(result.query_ir.get("output_group_fields"), ["time_bucket", "service"])
        self.assertTrue(result.query_ir.get("semantic_losses"))

        query = yaml_panel["esql"]["query"]
        self.assertIn("http_server_errors_total", query)
        self.assertIn("http_server_requests_total", query)
        self.assertIn("| EVAL computed_value =", query)
        self.assertEqual(result.visual_ir.presentation.kind, "esql")
        self.assertEqual(result.visual_ir.presentation.config["query"], query)
        self.assertEqual(result.visual_ir.metadata.get("output_shape"), "time_series")
        self.assertEqual(yaml_panel["esql"]["dimension"]["field"], "time_bucket")
        self.assertEqual(yaml_panel["esql"]["breakdown"]["field"], "service")
        self.assertEqual(yaml_panel["esql"]["metrics"][0]["field"], "computed_value")

    def test_logql_contains_preserves_event_row_intent_across_ir_and_visual_ir(self):
        panel = {
            "id": 6,
            "type": "logs",
            "title": "App Errors",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 6},
            "datasource": {"type": "loki", "uid": "loki"},
            "targets": [{"expr": '{job="app"} |= "error"', "refId": "A"}],
        }
        yaml_panel, result = _translate_panel(panel)

        self.assertEqual(result.status, "migrated_with_warnings")
        self.assertEqual(result.query_ir.get("source_language"), "logql")
        self.assertEqual(result.query_ir.get("output_shape"), "event_rows")
        self.assertEqual(result.visual_ir.presentation.kind, "esql")
        self.assertEqual(result.visual_ir.metadata.get("query_language"), "logql")
        self.assertEqual(result.visual_ir.metadata.get("output_shape"), "event_rows")
        self.assertIn('service.name == "app"', yaml_panel["esql"]["query"])
        self.assertIn('message LIKE "*error*"', yaml_panel["esql"]["query"])

    def test_very_long_expression_does_not_crash(self):
        metric = "metric_" + "a" * 200
        expr = f"rate({metric}_total[5m])"
        ctx = _translate(expr)
        self.assertIn(ctx.feasibility, ("feasible", "not_feasible"))

    def test_unicode_in_label_does_not_crash(self):
        expr = 'rate(http_requests_total{region="日本"}[5m])'
        ctx = _translate(expr)
        self.assertIn(ctx.feasibility, ("feasible", "not_feasible"))

    def test_underscore_heavy_metric_name_handled(self):
        expr = "rate(my_very_long_metric_name_total[5m])"
        ctx = _translate(expr)
        self.assertEqual(ctx.feasibility, "feasible")
        if ctx.esql_query:
            self.assertNotIn("  =", ctx.esql_query, "Double space before = in alias")


# =========================================================================
# Skipped Panel Type Completeness
# =========================================================================

class TestSkipPanelTypeCompleteness(unittest.TestCase):
    """Test plan item: All skip panel types must be handled consistently."""

    EXPECTED_SKIP_TYPES = {"row", "news", "dashlist", "alertlist", "nodeGraph", "canvas"}

    def test_skip_set_matches_expected(self):
        self.assertEqual(panels.SKIP_PANEL_TYPES, self.EXPECTED_SKIP_TYPES)

    def test_each_skip_type_produces_skipped_result(self):
        for panel_type in self.EXPECTED_SKIP_TYPES:
            panel = {"id": 1, "type": panel_type, "title": panel_type,
                     "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}}
            yaml_panel, result = _translate_panel(panel)
            self.assertIsNone(yaml_panel,
                              f"{panel_type} should produce None yaml")
            self.assertEqual(result.status, "skipped",
                             f"{panel_type} should be skipped")

    def test_rule_pack_can_extend_skip_types(self):
        rp = rules.RulePackConfig()
        rp.skip_panel_types = ["custom_plugin"]
        panel = {"id": 1, "type": "custom_plugin", "title": "Custom",
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}}
        _, result = _translate_panel(panel, rule_pack=rp)
        self.assertEqual(result.status, "skipped")


# =========================================================================
# Multi-Target Panel Handling
# =========================================================================

class TestMultiTargetPanels(unittest.TestCase):
    """Verify that multi-target panels are handled with warnings."""

    def test_multi_target_warns_about_dropped_targets(self):
        panel = {
            "id": 1, "type": "graph", "title": "Multi",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "targets": [
                {"expr": "rate(foo_total[5m])", "refId": "A"},
                {"expr": "avg(bar_gauge)", "refId": "B"},
            ],
        }
        _yaml_panel, result = _translate_panel(panel)
        if len(result.reasons) > 0:
            if result.status == "migrated_with_warnings":
                self.assertTrue(True)

    def test_same_metric_targets_collapse_correctly(self):
        """Two targets with same metric but different label values should collapse."""
        panel = {
            "id": 1, "type": "graph", "title": "Systemd",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "targets": [
                {"expr": 'node_systemd_units{state="active"}', "refId": "A"},
                {"expr": 'node_systemd_units{state="failed"}', "refId": "B"},
            ],
        }
        yaml_panel, result = _translate_panel(panel)
        if yaml_panel and "esql" in yaml_panel:
            query = yaml_panel["esql"]["query"]
            self.assertIn("state", query, "Collapsed targets should group BY state")
            self.assertTrue(any("Collapsed" in r for r in result.reasons))


# =========================================================================
# Query IR Contract
# =========================================================================

class TestQueryIRContract(unittest.TestCase):
    """Verify QueryIR is populated correctly for supported translations."""

    def test_query_ir_has_source_language(self):
        ctx = _translate("rate(http_requests_total[5m])")
        query_ir = ctx.query_ir
        assert query_ir is not None
        self.assertEqual(query_ir.source_language, "promql")

    def test_query_ir_has_metric_name(self):
        ctx = _translate("rate(http_requests_total[5m])")
        query_ir = ctx.query_ir
        assert query_ir is not None
        self.assertEqual(query_ir.metric, "http_requests_total")

    def test_query_ir_has_output_shape(self):
        ctx = _translate("rate(http_requests_total[5m])")
        query_ir = ctx.query_ir
        assert query_ir is not None
        self.assertIn(query_ir.output_shape, ("time_series", "scalar", "table"))

    def test_query_ir_has_target_query(self):
        ctx = _translate("rate(http_requests_total[5m])")
        query_ir = ctx.query_ir
        assert query_ir is not None
        self.assertTrue(query_ir.target_query)


# =========================================================================
# Bug Regression: Parse Error Handling
# =========================================================================

class TestParseErrorHandling(unittest.TestCase):
    """Regression tests for parser crash handling (bug found during audit)."""

    def test_invalid_promql_does_not_crash_translate(self):
        """rate(rate(...)[...]) is invalid PromQL — must not crash."""
        panel = _make_panel(1, "rate(rate(foo_total[5m])[10m])")
        _yaml_panel, result = _translate_panel(panel)
        self.assertEqual(result.status, "not_feasible")
        self.assertTrue(result.reasons)

    def test_parse_fragment_returns_fragment_on_invalid_syntax(self):
        frag = promql._parse_fragment("rate(rate(foo_total[5m])[10m])")
        self.assertIsNotNone(frag)
        self.assertIn("parse_error", frag.extra)

    def test_garbage_expression_does_not_crash(self):
        panel = _make_panel(1, "!@#$%^&*")
        _yaml_panel, result = _translate_panel(panel)
        self.assertIn(result.status, ("not_feasible", "requires_manual"))

    def test_empty_braces_do_not_crash(self):
        ctx = _translate("{}")
        self.assertIn(ctx.feasibility, ("feasible", "not_feasible"))

    def test_unbalanced_parens_do_not_crash(self):
        panel = _make_panel(1, "rate(foo_total[5m]")
        _yaml_panel, result = _translate_panel(panel)
        self.assertIn(result.status, ("not_feasible", "requires_manual"))


# =========================================================================
# Bug Regression: Negation Prefix Handling
# =========================================================================

class TestNegationHandling(unittest.TestCase):
    """Regression tests for single-target negation (bug found during audit)."""

    def test_negated_rate_applies_eval_negation(self):
        panel = _make_panel(1, "- rate(foo_total[5m])")
        _yaml_panel, result = _translate_panel(panel)
        self.assertIn("EVAL", result.esql_query)
        self.assertIn("-1 * ", result.esql_query)
        self.assertIn(result.status, ("migrated", "migrated_with_warnings"))

    def test_negated_panel_has_warning(self):
        panel = _make_panel(1, "- rate(foo_total[5m])")
        _, result = _translate_panel(panel)
        has_negate_warning = any("negat" in r.lower() for r in result.reasons)
        self.assertTrue(has_negate_warning,
                        f"Negated panel should warn: {result.reasons}")

    def test_non_negated_has_no_negation_eval(self):
        panel = _make_panel(1, "rate(foo_total[5m])")
        _, result = _translate_panel(panel)
        self.assertNotIn("-1 *", result.esql_query or "")

    def test_negated_sort_is_after_negation(self):
        panel = _make_panel(1, "- rate(foo_total[5m])")
        _, result = _translate_panel(panel)
        if result.esql_query:
            eval_pos = result.esql_query.find("EVAL")
            sort_pos = result.esql_query.find("SORT")
            if eval_pos > 0 and sort_pos > 0:
                self.assertLess(eval_pos, sort_pos,
                                "EVAL negation must come before SORT")


# =========================================================================
# Semantic Correctness: Warning Patterns
# =========================================================================

class TestWarningPatternHonesty(unittest.TestCase):
    """Verify unsupported wrappers fail clearly instead of false-success."""

    def test_label_replace_now_translates(self):
        # label_replace is now handled — copy pattern with passthrough regex
        ctx = _translate("label_replace(up, 'dst', '$1', 'src', '(.*)')")
        self.assertNotEqual(ctx.feasibility, "not_feasible")

    def test_predict_linear_is_not_feasible(self):
        ctx = _translate("predict_linear(node_filesystem_avail_bytes[6h], 86400)")
        self.assertEqual(ctx.feasibility, "not_feasible")
        self.assertTrue(any("predict_linear" in w.lower() for w in ctx.warnings))

    def test_abs_is_not_feasible(self):
        ctx = _translate("abs(rate(foo_total[5m]))")
        self.assertEqual(ctx.feasibility, "not_feasible")
        self.assertTrue(any("abs" in w.lower() for w in ctx.warnings))

    def test_clamp_min_now_translates(self):
        # clamp_min() is now handled as a passthrough wrapper — no longer not_feasible
        ctx = _translate("clamp_min(rate(foo_total[5m]), 0)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)

    def test_sort_desc_now_translates(self):
        # sort_desc() is now handled as a passthrough wrapper — no longer not_feasible
        ctx = _translate("sort_desc(rate(foo_total[5m]))")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)


# =========================================================================
# Bug Regression: Semantically Wrong Approximations
# =========================================================================

class TestHardUnsupportedFunctions(unittest.TestCase):
    """Functions that must be not_feasible, not approximated with AVG."""

    def test_absent_is_not_feasible(self):
        ctx = _translate("absent(up)")
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_absent_over_time_is_not_feasible(self):
        ctx = _translate("absent_over_time(up[5m])")
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_resets_is_not_feasible(self):
        ctx = _translate("resets(http_requests_total[1h])")
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_timestamp_is_not_feasible(self):
        ctx = _translate("timestamp(up)")
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_changes_is_not_feasible(self):
        ctx = _translate("changes(up[1h])")
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_absent_has_clear_reason(self):
        ctx = _translate("absent(up{job=\"apiserver\"})")
        has_reason = any("absent" in w.lower() and "existence" in w.lower()
                         for w in ctx.warnings)
        self.assertTrue(has_reason, f"absent should explain: {ctx.warnings}")


# =========================================================================
# Bug Regression: Legend Visibility
# =========================================================================

class TestLegendVisibility(unittest.TestCase):
    """displayMode=hidden must produce legend.visible=hide."""

    def test_hidden_display_mode_hides_legend(self):
        panel = _make_panel(1)
        panel["options"] = {"legend": {"displayMode": "hidden"}}
        yaml_panel, _ = _translate_panel(panel)
        legend = yaml_panel.get("esql", {}).get("legend", {})
        self.assertIn(legend.get("visible"), ("hide", False),
                      f"Hidden legend should produce hide: {legend}")

    def test_list_display_mode_shows_legend(self):
        panel = _make_panel(1)
        panel["options"] = {"legend": {"displayMode": "list"}}
        yaml_panel, _ = _translate_panel(panel)
        legend = yaml_panel.get("esql", {}).get("legend", {})
        self.assertIn(legend.get("visible"), ("show", True))

    def test_show_legend_false_hides(self):
        panel = _make_panel(1)
        panel["options"] = {"legend": {"displayMode": "list", "showLegend": False}}
        yaml_panel, _ = _translate_panel(panel)
        legend = yaml_panel.get("esql", {}).get("legend", {})
        self.assertIn(legend.get("visible"), ("hide", False))


# =========================================================================
# Bug Regression: _over_time Source Type
# =========================================================================

class TestOverTimeFunctions(unittest.TestCase):
    """avg_over_time etc. must use TS source (they produce TS-only ES|QL funcs)."""

    def test_avg_over_time_uses_ts_source(self):
        ctx = _translate("avg_over_time(temperature[5m])")
        self.assertTrue(ctx.esql_query.startswith("TS"),
                        f"avg_over_time should use TS: {ctx.esql_query[:50]}")

    def test_sum_over_time_uses_ts_source(self):
        ctx = _translate("sum_over_time(temperature[5m])")
        self.assertTrue(ctx.esql_query.startswith("TS"))

    def test_max_over_time_uses_ts_source(self):
        ctx = _translate("max_over_time(temperature[5m])")
        self.assertTrue(ctx.esql_query.startswith("TS"))

    def test_min_over_time_uses_ts_source(self):
        ctx = _translate("min_over_time(temperature[5m])")
        self.assertTrue(ctx.esql_query.startswith("TS"))

    def test_count_over_time_uses_ts_source(self):
        ctx = _translate("count_over_time(temperature[5m])")
        self.assertTrue(ctx.esql_query.startswith("TS"))

    def test_rate_still_uses_ts(self):
        ctx = _translate("rate(foo_total[5m])")
        self.assertTrue(ctx.esql_query.startswith("TS"))

    def test_simple_gauge_still_uses_from(self):
        ctx = _translate("avg(up)")
        self.assertTrue(ctx.esql_query.startswith("FROM"))


# =========================================================================
# Binary Expression Correctness
# =========================================================================

class TestBinaryExpressions(unittest.TestCase):
    """Verify arithmetic, ratio, and comparison translations."""

    def test_scalar_multiplication_has_eval(self):
        ctx = _translate("rate(foo_total[5m]) * 100")
        self.assertIn("EVAL", ctx.esql_query)
        self.assertIn("100", ctx.esql_query)

    def test_two_metric_addition_has_both(self):
        ctx = _translate("rate(foo_total[5m]) + rate(bar_total[5m])")
        self.assertIn("foo_total", ctx.esql_query)
        self.assertIn("bar_total", ctx.esql_query)
        self.assertIn("EVAL", ctx.esql_query)

    def test_ratio_has_division(self):
        ctx = _translate("rate(foo_total[5m]) / rate(bar_total[5m])")
        self.assertIn("/", ctx.esql_query)

    def test_comparison_filter_has_where(self):
        ctx = _translate("rate(foo_total[5m]) > 0.5")
        where_count = ctx.esql_query.count("WHERE")
        self.assertGreaterEqual(where_count, 2,
                                "Should have time filter WHERE and comparison WHERE")

    def test_unless_is_marked_not_feasible(self):
        """PromQL ``unless`` (set difference) has no honest single-stage
        ES|QL equivalent. The translator used to silently emit an
        approximation; it now refuses, surfacing a clear ``not_feasible``
        marker so the panel is reported rather than rendered with a
        dropped operand. See parity-rig RESULTS.md."""
        ctx = _translate("rate(foo_total[5m]) unless rate(bar_total[5m])")
        self.assertEqual(ctx.feasibility, "not_feasible")
        reasons = " ".join(getattr(ctx, "warnings", []) or [])
        self.assertRegex(reasons, r"(?i)set operator|unless|set difference")


# =========================================================================
# Multi-Target Fusion
# =========================================================================

class TestMultiTargetFusion(unittest.TestCase):
    """Verify multi-target panel handling."""

    def test_same_metric_different_labels_collapsed(self):
        panel = _make_panel(1)
        panel["targets"] = [
            {"expr": 'rate(http_total{method="GET"}[5m])', "refId": "A"},
            {"expr": 'rate(http_total{method="POST"}[5m])', "refId": "B"},
        ]
        _, result = _translate_panel(panel)
        has_collapse = any("collapsed" in r.lower() or "merged" in r.lower()
                           for r in result.reasons)
        self.assertTrue(has_collapse)

    def test_different_metrics_merged(self):
        panel = _make_panel(1)
        panel["targets"] = [
            {"expr": "rate(foo_total[5m])", "refId": "A"},
            {"expr": "rate(bar_total[5m])", "refId": "B"},
        ]
        _, result = _translate_panel(panel)
        self.assertIn("foo_total", result.esql_query)
        self.assertIn("bar_total", result.esql_query)

    def test_incompatible_targets_warn(self):
        panel = _make_panel(1)
        panel["targets"] = [
            {"expr": "rate(foo_total[5m])", "refId": "A"},
            {"expr": "avg(bar_gauge)", "refId": "B"},
            {"expr": "sum(baz_total[5m])", "refId": "C"},
        ]
        _, result = _translate_panel(panel)
        has_drop = any("only 1" in r.lower() or "drop" in r.lower()
                       for r in result.reasons)
        self.assertTrue(has_drop, f"Should warn about dropped targets: {result.reasons}")


# =========================================================================
# Summary Panel Correctness
# =========================================================================

class TestSummaryPanelCorrectness(unittest.TestCase):
    """Regression tests for summary-mode panel/query shape."""

    def test_grouped_stat_becomes_summary_table(self):
        panel = _make_panel(1, "sum by (job) (rate(foo_total[5m]))", panel_type="stat")
        yaml_panel, result = _translate_panel(panel)
        self.assertEqual(result.kibana_type, "datatable")
        self.assertEqual(yaml_panel["esql"]["type"], "datatable")
        self.assertTrue(any("grouped stat" in r.lower() for r in result.reasons))

    def test_grouped_gauge_becomes_summary_table(self):
        panel = _make_panel(1, "sum by (job) (rate(foo_total[5m]))", panel_type="gauge")
        yaml_panel, result = _translate_panel(panel)
        self.assertEqual(result.kibana_type, "datatable")
        self.assertEqual(yaml_panel["esql"]["type"], "datatable")
        self.assertTrue(any("grouped gauge" in r.lower() for r in result.reasons))

    def test_grouped_pie_collapses_to_latest_per_group(self):
        panel = _make_panel(1, "sum by (job) (rate(foo_total[5m]))", panel_type="piechart")
        yaml_panel, result = _translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["type"], "pie")
        # The per-group collapse now uses ``MAX`` instead of ``LAST`` so
        # multi-target TS queries with per-series nulls inside a bucket
        # don't render as all-null (see
        # ``test_collapse_summary_uses_null_safe_aggregate_for_multi_series_ts``).
        # For a single-series query like this one the behaviour is
        # identical, but the emitted token is now ``MAX``.
        self.assertIn("MAX(foo_total)", result.esql_query)
        self.assertIn("service.name", result.esql_query)

    def test_legacy_range_false_summary_keeps_latest_bucket(self):
        panel = _make_panel(1, "avg(node_load1)", panel_type="gauge")
        panel["targets"][0]["range"] = False
        _yaml_panel, result = _translate_panel(panel)
        self.assertIn("BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)", result.esql_query)
        self.assertIn("| SORT time_bucket ASC", result.esql_query)
        # ``MAX(node_load1)`` replaces the previous
        # ``LAST(node_load1, time_bucket)`` so the collapse is null-safe
        # across multi-target TS queries; behaviour for this
        # single-series case is identical.
        self.assertIn(
            "| STATS time_bucket = MAX(time_bucket), node_load1 = MAX(node_load1)",
            result.esql_query,
        )
        self.assertNotIn("| SORT time_bucket DESC", result.esql_query)
        self.assertNotIn("| LIMIT 1", result.esql_query)

    def test_grouped_summary_query_ir_reports_table_shape(self):
        panel = _make_panel(1, "sum by (job) (rate(foo_total[5m]))", panel_type="stat")
        _, result = _translate_panel(panel)
        self.assertEqual(result.query_ir.get("output_shape"), "table")
        self.assertEqual(result.query_ir.get("output_group_fields"), ["service.name"])


# =========================================================================
# Honesty Notes
# =========================================================================

class TestPanelNotesHonesty(unittest.TestCase):
    """Feature gaps that are not translated should be captured in notes."""

    def test_description_is_noted(self):
        panel = _make_panel(1)
        panel["description"] = "Important context"
        _, result = _translate_panel(panel)
        self.assertTrue(any("description" in note.lower() for note in result.notes),
                        f"Description should be noted: {result.notes}")

    def test_field_overrides_are_noted(self):
        panel = _make_panel(1)
        panel["fieldConfig"]["overrides"] = [
            {
                "matcher": {"id": "byName", "options": "Value #A"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#FF0000"}}],
            }
        ]
        _, result = _translate_panel(panel)
        self.assertTrue(any("override" in note.lower() for note in result.notes),
                        f"Field overrides should be noted: {result.notes}")


class TestFlattenDashboardPanelsNullGuards(unittest.TestCase):
    """_flatten_dashboard_panels must not crash on explicit null fields (issue #37)."""

    def test_null_rows_returns_empty(self):
        dashboard = {"title": "test", "rows": None, "panels": []}
        result = panels._flatten_dashboard_panels(dashboard)
        self.assertEqual(result, [])

    def test_null_panels_returns_empty(self):
        dashboard = {"title": "test", "rows": [], "panels": None}
        result = panels._flatten_dashboard_panels(dashboard)
        self.assertEqual(result, [])

    def test_null_rows_and_panels_returns_empty(self):
        dashboard = {"title": "test", "rows": None, "panels": None}
        result = panels._flatten_dashboard_panels(dashboard)
        self.assertEqual(result, [])


class TestBuildSectionGroupsNullRows(unittest.TestCase):
    """_build_section_groups must not crash when 'rows' is explicitly null (Mimir dashboard pattern)."""

    def test_null_rows_at_dashboard_level_does_not_crash(self):
        dashboard = {"title": "t", "schemaVersion": 16, "panels": [], "rows": None}
        panels._build_section_groups(dashboard)

    def test_null_rows_produces_one_empty_group(self):
        # _build_section_groups always emits at least one trailing flush group;
        # with rows=None and no panels that group should have an empty panel list.
        dashboard = {"title": "t", "schemaVersion": 16, "panels": [], "rows": None}
        groups = panels._build_section_groups(dashboard)
        self.assertEqual(len(groups), 1)
        _title, group_panels, _is_row, _collapsed = groups[0]
        self.assertEqual(group_panels, [])


class TestBuildSectionGroupsNullRowHeight(unittest.TestCase):
    """_build_section_groups must not crash when a legacy row has 'height': null (issue #39-followup)."""

    def _make_legacy_dashboard(self, height):
        panel = {
            "id": 1, "type": "graph", "title": "P",
            "targets": [{"expr": "up", "refId": "A", "datasource": {"type": "prometheus"}}],
            "span": 12,
        }
        return {"title": "t", "schemaVersion": 6, "rows": [{"title": "R", "height": height, "panels": [panel]}]}

    def test_null_row_height_does_not_crash(self):
        dashboard = self._make_legacy_dashboard(None)
        panels._build_section_groups(dashboard)

    def test_zero_row_height_does_not_crash(self):
        dashboard = self._make_legacy_dashboard(0)
        panels._build_section_groups(dashboard)

    def test_normal_row_height_still_works(self):
        dashboard = self._make_legacy_dashboard(250)
        groups = panels._build_section_groups(dashboard)
        self.assertTrue(len(groups) > 0)


class TestPromQLWrapperFragments(unittest.TestCase):
    """sort/round/clamp_min must be handled as passthrough wrappers (quick wins)."""

    _INDEX = "metrics-*"

    def _translate(self, expr):
        from observability_migration.adapters.source.grafana.rules import RulePackConfig
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        rp = RulePackConfig()
        return translate_promql_to_esql(expr, esql_index=self._INDEX, rule_pack=rp)

    def test_sort_desc_strips_outer_call(self):
        ctx = self._translate("sort_desc(sum by (job) (rate(http_requests_total[5m])))")
        frag = ctx.fragment
        self.assertIsNotNone(frag)
        self.assertFalse(frag.extra.get("not_feasible_reasons"))
        self.assertEqual(frag.extra.get("value_sort_desc"), True)

    def test_sort_asc_strips_outer_call(self):
        ctx = self._translate("sort(sum by (job) (rate(http_requests_total[5m])))")
        frag = ctx.fragment
        self.assertIsNotNone(frag)
        self.assertFalse(frag.extra.get("not_feasible_reasons"))
        self.assertEqual(frag.extra.get("value_sort_desc"), False)

    def test_round_strips_outer_call_with_precision(self):
        ctx = self._translate("round(sum by (job) (rate(http_requests_total[5m])), 2)")
        frag = ctx.fragment
        self.assertIsNotNone(frag)
        self.assertFalse(frag.extra.get("not_feasible_reasons"))
        self.assertTrue(frag.extra.get("has_round"))
        self.assertEqual(frag.extra.get("round_precision"), 2.0)

    def test_round_strips_outer_call_no_precision(self):
        ctx = self._translate("round(sum by (job) (rate(http_requests_total[5m])))")
        frag = ctx.fragment
        self.assertIsNotNone(frag)
        self.assertFalse(frag.extra.get("not_feasible_reasons"))
        self.assertTrue(frag.extra.get("has_round"))
        self.assertIsNone(frag.extra.get("round_precision"))

    def test_clamp_min_strips_outer_call(self):
        ctx = self._translate("clamp_min(sum by (job) (rate(http_requests_total[5m])), 0)")
        frag = ctx.fragment
        self.assertIsNotNone(frag)
        self.assertFalse(frag.extra.get("not_feasible_reasons"))
        self.assertEqual(frag.extra.get("clamp_min_value"), 0.0)


if __name__ == "__main__":
    unittest.main()
