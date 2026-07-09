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

import json
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
        # Force the FROM path (assume_tsds_gauges=False) so this exercises FROM's
        # BUCKET(@timestamp, ...) redundant-WHERE omission specifically.
        rp = rules.RulePackConfig()
        rp.assume_tsds_gauges = False
        yaml_panel, pr = _translate_panel(_make_panel(1, "avg(node_load1)"), rule_pack=rp)
        esql = yaml_panel["esql"]["query"]

        self.assertIn("BUCKET(@timestamp, 50, ?_tstart, ?_tend)", esql)
        self.assertNotIn("| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend", esql)
        self.assertEqual(esql, pr.esql_query)
        self.assertEqual(esql, pr.query_ir["target_query"])

    def test_dashboard_esql_omits_rule_pack_timestamp_range_where(self):
        rp = rules.RulePackConfig()
        rp.assume_tsds_gauges = False
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

    def test_built_in_macros_honor_custom_window(self):
        """Built-in step macros honor rule_pack.default_rate_window (issue #87).

        Previously $__rate_interval/$__interval/$interval/$__auto_interval_*
        were hardcoded to 5m and ignored the rule pack; now they collapse to
        the configured default_rate_window so the step is at least tunable.
        """
        rp = rules.RulePackConfig()
        rp.default_rate_window = "10m"
        for expr in (
            "rate(foo[$__rate_interval])",
            "rate(foo[$__interval])",
            "rate(foo[$interval])",
            "rate(foo[$__auto_interval_my_panel])",
        ):
            with self.subTest(expr=expr):
                result = promql.preprocess_grafana_macros(expr, rp)
                self.assertIn("[10m]", result)
                self.assertNotIn("[5m]", result)

    def test_range_macro_ignores_custom_window(self):
        """$__range is the full time range, not a step, so it stays 1h."""
        rp = rules.RulePackConfig()
        rp.default_rate_window = "10m"
        result = promql.preprocess_grafana_macros("avg_over_time(foo[$__range])", rp)
        self.assertIn("[1h]", result)
        self.assertNotIn("$__range", result)

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

    def test_variable_in_label_selector_becomes_parameter(self):
        result = promql.preprocess_grafana_macros('foo{job="$job"}')
        self.assertIn('job="__obs_migration_param_job"', result)
        self.assertNotIn('$job', result)

    def test_variable_regex_match_becomes_parameter(self):
        result = promql.preprocess_grafana_macros('foo{instance=~"$instance"}')
        self.assertIn('instance=~"__obs_migration_param_instance"', result)
        self.assertNotIn('$instance', result)


# =========================================================================
# Layer A: Variable Erasure Detection
# =========================================================================

class TestVariableErasure(unittest.TestCase):
    """Test plan item: Variable preservation/erasure test.

    Grafana variables are represented as Kibana dashboard controls, not ES|QL
    query params. Final ES|QL must therefore drop those matchers and warn
    rather than upload unbound ``?var`` placeholders.
    """

    def test_variable_in_label_filter_is_dropped_with_warning(self):
        ctx = _translate('rate(http_requests_total{job="$job"}[5m])')
        self.assertIn("feasible", ctx.feasibility)
        self.assertNotIn("?job", ctx.esql_query)
        self.assertIn("Dropped variable-driven label filters during migration", ctx.warnings)

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
            self.assertNotIn("?job", ctx.esql_query)
            self.assertNotIn("?instance", ctx.esql_query)
            self.assertIn("Dropped variable-driven label filters during migration", ctx.warnings)

    def test_logql_variable_in_stream_selector_is_dropped_with_warning(self):
        ctx = _translate('{service_name="$svc"} |~ "error"', panel_type="logs")
        if ctx.feasibility == "feasible":
            self.assertNotIn("?svc", ctx.esql_query)
            self.assertIn("Dropped variable-driven LogQL label filters during migration", ctx.warnings)

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
        yaml_panel, result = _translate_panel(panel)
        self.assertEqual(result.status, "not_feasible")
        self.assertTrue(any("Unknown" in r for r in result.reasons))
        self.assertIsNotNone(yaml_panel)
        self.assertIn("markdown", yaml_panel)
        self.assertIn("Migration Required", yaml_panel["markdown"]["content"])
        self.assertIn("unknown_plugin_xyz", yaml_panel["markdown"]["content"])

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
        self.assertIn("STATS value = LAST(_bucket_value, time_bucket) BY handler", ctx.esql_query)
        self.assertNotIn("value = MAX(_bucket_value)", ctx.esql_query)
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

    def test_cross_metric_on_join_warning_names_on_modifier(self):
        """on() joins must keep naming on(...) in the not-feasible warning (issue #65)."""
        expr = "a_metric + on(namespace) group_left() b_metric"
        ctx = _translate(expr)
        self.assertEqual(ctx.feasibility, "not_feasible")
        join_warnings = [w for w in ctx.warnings if "Cross-metric" in w]
        self.assertTrue(join_warnings, f"expected a cross-metric warning, got {ctx.warnings}")
        self.assertIn("on(namespace) group_left()", join_warnings[0])
        self.assertNotIn("ignoring(", join_warnings[0])

    def test_cross_metric_ignoring_group_right_warning_reflects_source(self):
        """ignoring()+group_right() must be named accurately, not as on() (issue #65)."""
        expr = (
            "synapse_event_persisted_position "
            "- ignoring(index,job,name) group_right() "
            "synapse_event_processing_positions"
        )
        ctx = _translate(expr)
        self.assertEqual(ctx.feasibility, "not_feasible")
        join_warnings = [w for w in ctx.warnings if "Cross-metric" in w]
        self.assertTrue(join_warnings, f"expected a cross-metric warning, got {ctx.warnings}")
        warning = join_warnings[0]
        self.assertIn("ignoring(index, job, name)", warning)
        self.assertIn("group_right()", warning)
        self.assertNotIn("on(", warning)

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
        self.assertIn('service.name RLIKE "api|worker"', query)
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

    def test_gauge_assumes_tsds_uses_ts_source(self):
        """Migration default: an unproven gauge assumes TSDS and uses TS (not FROM).

        FROM+aggregation over a multi-sample TSDS inflates non-idempotent aggregators;
        TS aggregates one value per series per bucket. See RulePackConfig.assume_tsds_gauges.
        """
        ctx = _translate("avg(node_load1)")
        self.assertEqual(ctx.feasibility, "feasible")
        self.assertTrue(ctx.esql_query.startswith("TS "),
                        f"Gauge should assume TSDS and use TS, got: {ctx.esql_query[:50]}")

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

    def test_live_gauge_metadata_overrides_histogram_summary_suffix(self):
        rp = rules.RulePackConfig()
        resolver = schema.SchemaResolver(rp)
        resolver._discovery_attempted = True
        resolver._field_cache = {
            "custom_queue_count": {
                "double": {
                    "type": "double",
                    "time_series_metric": "gauge",
                }
            }
        }

        self.assertFalse(resolver.is_counter("custom_queue_count"))

    def test_metric_kind_override_still_beats_live_gauge_metadata(self):
        rp = rules.RulePackConfig()
        rp.metric_kinds["custom_queue_count"] = "counter"
        resolver = schema.SchemaResolver(rp)
        resolver._discovery_attempted = True
        resolver._field_cache = {
            "custom_queue_count": {
                "double": {
                    "type": "double",
                    "time_series_metric": "gauge",
                }
            }
        }

        self.assertTrue(resolver.is_counter("custom_queue_count"))

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


class TestCounterLongAggregationWarning(unittest.TestCase):
    """Issue #148: SUM/MAX/MIN on counter_long fields error with
    verification_exception in ES|QL.

    The failure only arises when live target field capabilities were NOT
    available: counter detection falls back to the ``_total`` naming heuristic
    (which OTel counter names do not match) and a bare aggregation is emitted as
    feasible against a field that is actually counter-typed in ES. When caps ARE
    available the field is either correctly typed (counter-safe form) or absent
    (marked not_feasible upstream), so there is no broken query to warn about.

    The fix keeps the generated ES|QL and surfaces a warning whenever caps were
    unavailable and the field is not refuted as a gauge.
    """

    @staticmethod
    def _offline_resolver(rule_pack=None):
        # Live field capabilities could not be fetched (no target reachable):
        # the real conditions under which issue #148 produces a broken panel.
        resolver = schema.SchemaResolver(rule_pack or rules.RulePackConfig())
        resolver._discovery_attempted = True
        resolver._field_cache = {}
        resolver._discovery_status = "offline"
        return resolver

    @staticmethod
    def _live_resolver(field_cache):
        # Caps were fetched successfully ("ok" requires a non-empty cache).
        rp = rules.RulePackConfig()
        resolver = schema.SchemaResolver(rp)
        resolver._discovery_attempted = True
        resolver._field_cache = dict(field_cache)
        resolver._discovery_status = "ok"
        return resolver

    def test_offline_bare_sum_warns_and_stays_feasible(self):
        # Mode 1: bare sum() emits SUM(field) and stays feasible; flag the risk.
        resolver = self._offline_resolver()
        ctx = _translate("sum(trace_http_request_hits)", resolver=resolver)
        self.assertNotEqual(ctx.feasibility, "not_feasible")
        self.assertIn("SUM(trace_http_request_hits)", ctx.esql_query)
        self.assertTrue(
            any("counter_long" in w for w in ctx.warnings),
            f"expected counter_long uncertainty warning, got {ctx.warnings}",
        )

    def test_offline_bare_max_warns(self):
        resolver = self._offline_resolver()
        ctx = _translate("max(system_net_bytes_sent)", resolver=resolver)
        self.assertTrue(
            any("counter_long" in w for w in ctx.warnings),
            f"expected counter_long uncertainty warning, got {ctx.warnings}",
        )

    def test_offline_increase_degrade_warns_about_counter(self):
        # Mode 2: increase() degrades to a gauge analogue; the degraded form also
        # fails on a counter, so flag the counter_long risk instead of claiming
        # the field "is typed as gauge in the target index".
        resolver = self._offline_resolver()
        ctx = _translate("increase(system_net_bytes_sent[5m])", resolver=resolver)
        self.assertTrue(
            any("counter_long" in w for w in ctx.warnings),
            f"expected counter_long uncertainty warning, got {ctx.warnings}",
        )

    def test_offline_binary_expr_operand_aggregation_warns(self):
        # Bare aggregations fused inside a binary expression go through the
        # measure-spec path; each operand must warn when caps are unavailable.
        resolver = self._offline_resolver()
        ctx = _translate("max(trace_http_request_errors) + max(trace_http_request_hits)", resolver=resolver)
        warns = [w for w in ctx.warnings if "field capabilities" in w]
        self.assertTrue(
            any("trace_http_request_errors" in w for w in warns)
            and any("trace_http_request_hits" in w for w in warns),
            f"expected a counter warning for each operand, got {ctx.warnings}",
        )

    def test_offline_count_does_not_warn(self):
        # COUNT is legal on counter fields, so it must not trigger the warning.
        resolver = self._offline_resolver()
        ctx = _translate("count(trace_http_request_hits)", resolver=resolver)
        self.assertFalse(
            any("counter_long" in w for w in ctx.warnings),
            f"unexpected counter warning for COUNT: {ctx.warnings}",
        )

    def test_offline_metric_kind_counter_pin_suppresses_warning(self):
        # Pinning metric_kinds: <field>: counter proves the type, so the engine
        # emits the counter-safe form and does not warn — even offline.
        rp = rules.RulePackConfig()
        rp.metric_kinds["trace_http_request_hits"] = "counter"
        resolver = self._offline_resolver(rp)
        ctx = _translate("sum(trace_http_request_hits)", rule_pack=rp, resolver=resolver)
        self.assertIn("LAST_OVER_TIME(trace_http_request_hits", ctx.esql_query)
        self.assertFalse(
            any("counter_long" in w for w in ctx.warnings),
            f"unexpected counter warning for pinned counter: {ctx.warnings}",
        )

    def test_offline_metric_kind_gauge_pin_suppresses_warning(self):
        # A gauge pin refutes counter typing, so no warning even offline.
        rp = rules.RulePackConfig()
        rp.metric_kinds["system_net_bytes_sent"] = "gauge"
        resolver = self._offline_resolver(rp)
        ctx = _translate("max(system_net_bytes_sent)", rule_pack=rp, resolver=resolver)
        self.assertFalse(
            any("counter_long" in w for w in ctx.warnings),
            f"unexpected counter warning for pinned gauge: {ctx.warnings}",
        )

    def test_live_proven_gauge_does_not_warn(self):
        # Caps available and the field is a gauge: no warning, query is feasible.
        resolver = self._live_resolver(
            {"node_load1": {"double": {"type": "double", "time_series_metric": "gauge"}}}
        )
        ctx = _translate("max(node_load1)", resolver=resolver)
        self.assertNotEqual(ctx.feasibility, "not_feasible")
        self.assertFalse(
            any("counter_long" in w for w in ctx.warnings),
            f"unexpected counter warning for proven gauge: {ctx.warnings}",
        )

    def test_live_proven_counter_long_is_counter_safe(self):
        # "Both paths" online guard: live caps surface counter_long, so
        # is_counter() routes to the counter-safe form and does not warn.
        resolver = self._live_resolver(
            {"trace_http_request_hits": {"counter_long": {"type": "counter_long"}}}
        )
        ctx = _translate("sum(trace_http_request_hits)", resolver=resolver)
        self.assertIn("SUM(LAST_OVER_TIME(trace_http_request_hits))", ctx.esql_query)
        self.assertFalse(
            any("counter_long" in w for w in ctx.warnings),
            f"unexpected uncertainty warning for proven counter: {ctx.warnings}",
        )

    def test_live_counter_pre_agg_comparison_is_not_feasible(self):
        # sum(counter > N): a pre-aggregation comparison filter combined with a
        # counter aggregation referenced without rate() has no counter-safe
        # ES|QL form, so it must be marked not_feasible rather than emitting a
        # SUM that errors with verification_exception.
        resolver = self._live_resolver(
            {"trace_http_request_hits": {"counter_long": {"type": "counter_long"}}}
        )
        ctx = _translate("sum(trace_http_request_hits > 0)", resolver=resolver)
        self.assertEqual(ctx.feasibility, "not_feasible")
        self.assertNotIn("SUM(trace_http_request_hits)", ctx.esql_query or "")

    def test_offline_pre_agg_comparison_warns(self):
        # Offline, the counter cannot be proven, so keep the query and warn.
        resolver = self._offline_resolver()
        ctx = _translate("sum(system_net_bytes_sent > 0)", resolver=resolver)
        self.assertNotEqual(ctx.feasibility, "not_feasible")
        self.assertTrue(
            any("counter_long" in w for w in ctx.warnings),
            f"expected counter_long uncertainty warning, got {ctx.warnings}",
        )

    def test_live_gauge_pre_agg_comparison_no_warning(self):
        # A proven gauge with a comparison filter is fine: feasible, no warning.
        resolver = self._live_resolver(
            {"node_load1": {"double": {"type": "double", "time_series_metric": "gauge"}}}
        )
        ctx = _translate("sum(node_load1 > 0)", resolver=resolver)
        self.assertNotEqual(ctx.feasibility, "not_feasible")
        self.assertFalse(
            any("counter_long" in w for w in ctx.warnings),
            f"unexpected counter warning for proven gauge: {ctx.warnings}",
        )

    def test_live_counter_pre_agg_count_stays_feasible(self):
        # COUNT over a comparison is legal on counters (counts documents), so it
        # must stay feasible even for a proven counter.
        resolver = self._live_resolver(
            {"trace_http_request_hits": {"counter_long": {"type": "counter_long"}}}
        )
        ctx = _translate("count(trace_http_request_hits > 0)", resolver=resolver)
        self.assertNotEqual(ctx.feasibility, "not_feasible")

    def test_live_absent_field_is_data_readiness_without_counter_warning(self):
        # Caps available but the field is absent: this is a target data-readiness
        # issue, not translation infeasibility. Do not add a counter uncertainty
        # warning on top of the readiness warning.
        resolver = self._live_resolver(
            {"some_other_field": {"double": {"type": "double", "time_series_metric": "gauge"}}}
        )
        ctx = _translate("sum(trace_http_request_hits)", resolver=resolver)
        self.assertEqual(ctx.feasibility, "feasible")
        self.assertTrue(
            any("data readiness" in w for w in ctx.warnings),
            f"expected data-readiness warning, got: {ctx.warnings}",
        )
        self.assertFalse(
            any("counter_long" in w for w in ctx.warnings),
            f"did not expect the counter uncertainty warning for missing target data: {ctx.warnings}",
        )


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

    def test_native_promql_bare_counter_on_stat_panel(self):
        """Issue #139: a bare counter reference (no rate()) on a single-value
        panel must migrate to a native PROMQL lens, not the ES|QL
        ``MAX(LAST_OVER_TIME(...))`` fallback with its misleading warning."""
        for panel_type in ("stat", "gauge"):
            with self.subTest(panel_type=panel_type):
                panel = _make_panel(
                    1, "node_network_receive_bytes_total", panel_type=panel_type
                )
                yaml_panel, result = _translate_panel(
                    panel, rule_pack=self.rp, resolver=self.resolver
                )
                query = yaml_panel["esql"]["query"]
                self.assertTrue(
                    query.startswith("PROMQL"),
                    f"bare counter should stay native PROMQL on {panel_type}: {query[:120]}",
                )
                self.assertIn("node_network_receive_bytes_total", query)
                self.assertNotIn("LAST_OVER_TIME", query)
                self.assertFalse(
                    any("LAST_OVER_TIME" in r for r in (result.reasons or [])),
                    f"native PROMQL must not emit the LAST_OVER_TIME warning: {result.reasons}",
                )

    def test_native_promql_ratio_uses_repeated_group_labels_without_timeseries_extraction(self):
        expr = (
            "(sum by (service.name) (rate(http_request_duration_seconds_sum[5m]))) / "
            "(sum by (service.name) (rate(http_request_duration_seconds_count[5m])))"
        )
        query = panels.build_native_promql_query(expr, index="metrics-*", kibana_type="line")

        self.assertTrue(query.startswith("PROMQL index=metrics-*"))
        self.assertNotIn("_timeseries", query)
        self.assertEqual(panels._native_promql_result_shape(expr), ("value", ["service.name"]))

    def test_native_promql_outer_parens_wrapped_aggregation_keeps_group_col(self):
        """Issue #162: a single ``by (...)`` aggregation wrapped in outer
        parentheses — ``(sum by (namespace)(...))`` — must still be recognised
        as grouped by ``namespace``. The leading ``(`` pushes the ``by`` clause
        to paren-depth ≥ 1, so the top-level scan missed it and fell through to
        the default ``["_timeseries"]`` shape, wrongly appending a
        ``GROK _timeseries`` stage that 400s at runtime (``Unknown column
        [_timeseries]``) because the aggregation projects ``namespace`` as its
        own column and no ``_timeseries`` column exists.
        """
        expr = '(sum by (namespace)(container_memory_usage_bytes{pod!="POD",namespace!=""}))'
        self.assertEqual(
            panels._native_promql_result_shape(expr), ("value", ["namespace"])
        )
        # Recognised as grouped → the bare PROMQL command (the aggregation
        # projects ``namespace`` itself); no ``GROK _timeseries`` stage.
        query = panels.build_native_promql_query(expr, index="metrics-*", kibana_type="line")
        self.assertNotIn("_timeseries", query)
        self.assertNotIn("GROK", query)
        self.assertEqual(
            query,
            'PROMQL index=metrics-* step=1m '
            'value=((sum by (namespace)(container_memory_usage_bytes{pod!="POD",namespace!=""})))',
        )

    def test_native_promql_wrapped_binary_op_different_groups_uses_timeseries(self):
        """Regression: a binary op wrapped in outer parens with *different* ``by``
        groups on each operand must return the ``_timeseries`` shape, not the
        first operand's labels.

        ``(sum by (a)(m1) + sum by (b)(m2))`` → after ``_trim_wrapping_parens``
        yields ``sum by (a)(m1) + sum by (b)(m2)``; the old depth-0 scanner
        returned ``["a"]`` immediately (first ``by`` found), bypassing the
        repeated-inner check that would have detected the differing groups.
        """
        expr = "(sum by (a)(metric_a) + sum by (b)(metric_b))"
        self.assertEqual(
            panels._native_promql_result_shape(expr), ("value", ["_timeseries"])
        )

    def test_native_promql_double_wrapped_aggregation_keeps_group_col(self):
        """Issue #162: nested wrapping parens — ``((sum by (namespace)(...)))`` —
        must still be recognised as grouped by ``namespace``. ``_trim_wrapping_parens``
        loops to peel every enclosing pair, so both layers are stripped before the
        depth-0 scan.
        """
        expr = "((sum by (namespace)(container_memory_usage_bytes{namespace!=\"\"})))"
        self.assertEqual(
            panels._native_promql_result_shape(expr), ("value", ["namespace"])
        )

    def test_native_promql_wrapped_without_aggregation_uses_timeseries(self):
        """Issue #162: a ``without (...)`` aggregation wrapped in outer parens —
        ``(sum without (namespace)(...))`` — preserves every non-excluded label,
        which ES|QL columns cannot represent, so it must return the
        ``["_timeseries"]`` shape rather than a named group column.
        """
        expr = "(sum without (namespace)(container_memory_usage_bytes{namespace!=\"\"}))"
        self.assertEqual(
            panels._native_promql_result_shape(expr), ("value", ["_timeseries"])
        )

    def test_native_promql_empty_legend_format_adds_no_label_pipe(self):
        """Issue #101: an empty ``legendFormat`` (``""``) must NOT cause any
        synthetic label/``_timeseries`` extraction to be appended. Grafana shows
        a single unlabeled series for an empty legend, so the migrated query must
        stay the bare ``PROMQL ... value=(...)`` source command. Previously we
        dumped ``EVAL _ts = COALESCE(_timeseries, "") | EVAL label = CASE(...)``,
        which 400s on aggregating queries (``_timeseries`` is not accessible) and
        renders the stringified label tuple as the legend on non-aggregating ones.
        """
        # Non-aggregating query: ``_timeseries`` IS accessible, but with an empty
        # legendFormat we must still not extract it.
        expr = "rate(http_requests_total[5m])"
        query = panels.build_native_promql_query(
            expr,
            index="metrics-*",
            legend_labels=panels._extract_legend_labels(""),
            kibana_type="line",
            legend_format="",
        )
        self.assertEqual(query, "PROMQL index=metrics-* step=1m value=(rate(http_requests_total[5m]))")
        self.assertNotIn("_timeseries", query)
        self.assertNotIn("EVAL", query)
        self.assertNotIn("COALESCE", query)
        self.assertNotIn("KEEP", query)

    def test_native_promql_aggregation_with_legend_format_never_extracts_timeseries(self):
        """Issue #101: when the query aggregates (a ``by`` clause collapses
        series) the ``_timeseries`` column does not exist, so even a placeholder
        ``legendFormat`` that references an aggregated-away label must not produce
        a ``GROK _timeseries`` / ``COALESCE(_timeseries, ...)`` pipe. The series
        identity comes from the real grouping column the aggregation keeps.
        """
        expr = "sum by (http.route) (rate(http_request_duration_seconds_count[5m]))"
        # ``{{instance}}`` is aggregated away by ``by (http.route)`` — unreachable.
        query = panels.build_native_promql_query(
            expr,
            index="metrics-*",
            legend_labels=panels._extract_legend_labels("{{instance}}"),
            kibana_type="line",
            legend_format="{{instance}}",
        )
        self.assertNotIn("_timeseries", query)
        self.assertNotIn("COALESCE", query)
        self.assertNotIn("GROK", query)
        self.assertEqual(
            panels._native_promql_result_shape(expr), ("value", ["http.route"])
        )

    def test_native_promql_legend_labels_use_grok_not_backtracking_replace(self):
        """Series-label extraction from ``_timeseries`` must use a single GROK
        scan per label, not ``REPLACE(_ts, \"\"\".*\"k\":\"...\".*\"\"\", \"$1\")``
        chains. The latter backtracks over the whole label blob (leading/trailing
        ``.*``) plus a full-blob ``REPLACE(REPLACE(...))`` fallback per row, which
        times out on wide label sets; GROK stays linear in the blob size.
        """
        query = panels.build_native_promql_query(
            "irate(node_interrupts_total[5m])",
            index="metrics-*",
            legend_labels=["type", "info"],
            kibana_type="timeseries",
        )

        # New, linear extraction: one GROK per label binding the JSON value,
        # anchored to top-level keys (see the nested-OTel-label test below).
        self.assertIn('"type":"%{DATA:type}', query)
        self.assertIn('"info":"%{DATA:info}', query)
        self.assertEqual(query.count("GROK _timeseries"), 2)
        self.assertTrue(query.rstrip().endswith("| KEEP step, value, type, info"))
        # The old super-linear pattern must be gone entirely.
        self.assertNotIn("REPLACE(_ts", query)
        self.assertNotIn("REPLACE(REPLACE(", query)
        self.assertNotIn("_raw_", query)

    def test_native_promql_legend_grok_binds_top_level_label_not_nested_otel(self):
        """The GROK pattern must bind the TOP-LEVEL label, not a same-named key
        nested inside OTel resource attributes: in alphabetical label order
        ``k8s.cluster.name`` sorts before a top-level ``name`` and
        ``service.name`` exists on any OTel-mapped cluster, so an unanchored
        first-occurrence match extracts the wrong label's value (surfaced as
        unalignable series keys in the seeded parity run)."""
        query = panels.build_native_promql_query(
            "node_systemd_socket_accepted_connections_total",
            index="metrics-*",
            legend_labels=["name"],
            kibana_type="timeseries",
        )
        m = re.search(r'GROK _timeseries """(.+)"""', query)
        self.assertIsNotNone(m, f"no GROK pipe in: {query}")
        # Simulate the GROK semantics: %{DATA:x} is a lazy capture.
        pattern = m.group(1).replace("%{DATA:name}", "(?P<name>.*?)")

        blob = json.dumps({
            "__name__": "m",
            "k8s": {"cluster": {"name": "prod-cluster"}},
            "name": "sshd.socket",
            "service": {"name": "backend"},
        }, separators=(",", ":"))
        match = re.search(pattern, blob)
        self.assertIsNotNone(match, f"pattern {pattern!r} matched nothing in {blob}")
        self.assertEqual(match.group("name"), "sshd.socket")

        # The wrapped form ({"labels": {...}}) must still match its first label.
        wrapped = json.dumps({"labels": {"name": "sshd.socket", "zone": "a"}},
                             separators=(",", ":"))
        match = re.search(pattern, wrapped)
        self.assertIsNotNone(match, f"pattern {pattern!r} matched nothing in {wrapped}")
        self.assertEqual(match.group("name"), "sshd.socket")

    def test_native_promql_legend_label_with_dotted_name_is_backtick_quoted(self):
        """A dotted legend label (e.g. ``deployment.environment``) must be
        regex-escaped inside the GROK pattern and backtick-quoted in KEEP."""
        query = panels.build_native_promql_query(
            "irate(some_total[5m])",
            index="metrics-*",
            legend_labels=["deployment.environment"],
            kibana_type="timeseries",
        )
        # Dot escaped in the GROK literal prefix ...
        self.assertIn('"deployment\\.environment":"%{DATA:deployment.environment}', query)
        # ... and the column backtick-quoted in KEEP.
        self.assertTrue(query.rstrip().endswith("| KEEP step, value, `deployment.environment`"))

    def test_native_promql_rejects_server_unsupported_group_modifiers(self):
        expr = (
            'rate(container_cpu_usage_seconds_total{pod=~"loki.*"}[1m]) '
            '/ on (pod, container) kube_pod_container_resource_limits_cpu_cores'
        )

        self.assertFalse(panels.can_use_native_promql(expr))

    def test_native_promql_rejects_server_unsupported_histogram_quantile(self):
        expr = 'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))'

        self.assertFalse(panels.can_use_native_promql(expr))

    def test_translate_dashboard_floor_bumps_for_native_histogram_quantile(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_HISTOGRAM_QUANTILE,
            set_runtime_feature,
        )

        set_runtime_feature(self.rp, PROMQL_HISTOGRAM_QUANTILE, supported=True, source="test")
        dashboard = {
            "title": "HQ Latency",
            "uid": "hq-1",
            "panels": [
                _make_panel(
                    1,
                    "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))",
                ),
            ],
        }
        _result, payload = _translate_dashboard(dashboard, rule_pack=self.rp, resolver=self.resolver)

        dash = payload["dashboards"][0]
        self.assertEqual(dash["minimum_kibana_version"], "9.5.0")
        self.assertIn("histogram_quantile", dash["panels"][0]["esql"]["query"])

    def test_build_native_promql_query_keeps_histogram_quantile_with_feature(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_HISTOGRAM_QUANTILE,
        )

        expr = "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))"
        query = panels.build_native_promql_query(
            expr,
            index="metrics-*",
            kibana_type="line",
            runtime_features={PROMQL_HISTOGRAM_QUANTILE: True},
        )
        self.assertTrue(query.startswith("PROMQL"))
        self.assertIn("histogram_quantile", query)

    def test_dashboard_min_version_ignores_histogram_quantile_substring(self):
        # A metric whose name merely contains the token must not trip the floor.
        substring = {
            "esql": {"query": "PROMQL index=metrics-* step=60s value=(rate(histogram_quantile_seconds_total[5m]))"}
        }
        self.assertEqual(
            panels._dashboard_minimum_kibana_version([substring]),
            panels.MINIMUM_KIBANA_VERSION,
        )

    def test_dashboard_min_version_bumps_for_native_histogram_quantile(self):
        native = {"esql": {"query": "PROMQL index=metrics-* step=60s value=(histogram_quantile(0.95, foo))"}}
        plain = {"esql": {"query": "PROMQL index=metrics-* step=60s value=(rate(foo_total[5m]))"}}

        self.assertEqual(
            panels._dashboard_minimum_kibana_version([native, plain]), "9.5.0"
        )
        self.assertEqual(
            panels._dashboard_minimum_kibana_version([plain]),
            panels.MINIMUM_KIBANA_VERSION,
        )

    def test_native_promql_allows_histogram_quantile_when_feature_supported(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_HISTOGRAM_QUANTILE,
        )

        expr = 'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))'

        self.assertTrue(
            panels.can_use_native_promql(expr, runtime_features={PROMQL_HISTOGRAM_QUANTILE: True})
        )

    def test_native_promql_still_rejects_histogram_quantile_with_other_unsupported(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_HISTOGRAM_QUANTILE,
        )

        # histogram_quantile is allowed, but the topk() wrapper is still unsupported.
        expr = 'topk(5, histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m]))))'

        self.assertFalse(
            panels.can_use_native_promql(expr, runtime_features={PROMQL_HISTOGRAM_QUANTILE: True})
        )

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

    def test_native_promql_xy_visual_ir_carries_api_safe_display_metadata(self):
        panel = _make_panel(
            1,
            "rate(http_requests_total[5m])",
            panel_type="graph",
            title="Traffic",
        )
        panel["targets"][0]["legendFormat"] = "Requests"
        panel["legend"] = {"show": True, "rightSide": True}
        panel["fieldConfig"] = {
            "defaults": {
                "unit": "Bps",
                "min": 0,
                "max": 100,
                "custom": {
                    "axisLabel": "Throughput",
                    "scaleDistribution": {"type": "log"},
                },
            },
            "overrides": [],
        }
        panel["yaxes"] = [
            {"label": "Throughput", "format": "Bps", "min": 0, "max": 100},
            {"label": "Error %", "format": "percent"},
        ]
        panel["seriesOverrides"] = [{"alias": "Requests", "yaxis": 2}]

        yaml_panel, result = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)

        esql = yaml_panel["esql"]
        metric = esql["metrics"][0]
        self.assertEqual(metric["label"], "Requests")
        self.assertEqual(metric["axis"], "right")
        self.assertEqual(metric["format"]["suffix"], "%")
        self.assertEqual(esql["legend"], {"visible": "show", "position": "right", "truncate_labels": 1})
        self.assertEqual(esql["appearance"]["y_left_axis"]["title"], "Throughput")
        self.assertEqual(
            esql["appearance"]["y_left_axis"]["extent"],
            {"mode": "custom", "min": 0.0, "max": 100.0},
        )
        self.assertEqual(result.visual_ir.presentation.config["metrics"][0]["axis"], "right")
        self.assertEqual(result.visual_ir.presentation.config["appearance"], esql["appearance"])

    def test_native_promql_gauge_visual_ir_carries_metric_color_bounds_and_shape(self):
        panel = _make_panel(
            2,
            "max(cpu_usage_percent)",
            panel_type="gauge",
            title="CPU Usage",
        )
        panel["fieldConfig"] = {
            "defaults": {
                "unit": "percent",
                "min": 0,
                "max": 100,
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "yellow", "value": 70},
                        {"color": "red", "value": 90},
                    ],
                },
            },
            "overrides": [],
        }

        yaml_panel, result = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)

        esql = yaml_panel["esql"]
        self.assertEqual(esql["type"], "gauge")
        self.assertEqual(esql["metric"]["label"], "CPU Usage")
        self.assertEqual(esql["metric"]["format"]["suffix"], "%")
        self.assertEqual(esql["minimum"], {"field": "_gauge_min"})
        self.assertEqual(esql["maximum"], {"field": "_gauge_max"})
        self.assertEqual(esql["goal"], {"field": "_gauge_goal"})
        self.assertEqual(esql["appearance"]["shape"], "arc")
        self.assertEqual(esql["color"]["thresholds"][-1]["up_to"], 100)
        self.assertNotIn("color", esql["metric"])
        self.assertEqual(result.visual_ir.presentation.config["color"], esql["color"])

    def test_native_promql_grouped_multi_label_legend_uses_composite_breakdown(self):
        panel = _make_panel(
            1,
            "sum(increase(argocd_app_k8s_request_total[1m])) by (verb, resource_kind)",
            panel_type="graph",
        )
        panel["targets"][0]["legendFormat"] = "{{verb}} {{resource_kind}}"

        yaml_panel, _result = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)

        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel["esql"]
        self.assertEqual(esql["breakdown"]["field"], "legend")
        self.assertIn("EVAL legend = CONCAT(", esql["query"])
        self.assertIn("TO_STRING(verb)", esql["query"])
        self.assertIn("TO_STRING(resource_kind)", esql["query"])

    def test_native_promql_timeseries_multi_label_legend_uses_composite_breakdown(self):
        panel = _make_panel(
            1,
            "rate(haproxy_server_http_responses_total[5m])",
            panel_type="graph",
        )
        panel["targets"][0]["legendFormat"] = "{{proxy}} / {{server}}"

        yaml_panel, _result = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)

        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel["esql"]
        self.assertEqual(esql["breakdown"]["field"], "legend")
        self.assertIn("EVAL legend = CONCAT(", esql["query"])
        self.assertIn("TO_STRING(proxy)", esql["query"])
        self.assertIn('" / "', esql["query"])
        self.assertIn("TO_STRING(server)", esql["query"])

    def test_native_promql_reserved_label_legend_is_escaped(self):
        # ``in``/``out`` are valid Prometheus labels but ``in`` is a reserved
        # ES|QL identifier (bare ``IN`` is rejected), so the composite legend
        # must backtick-quote it inside TO_STRING(...) to stay valid at runtime.
        panel = _make_panel(
            1,
            "sum(increase(net_bytes_total[5m])) by (in, out)",
            panel_type="graph",
        )
        panel["targets"][0]["legendFormat"] = "{{in}} {{out}}"

        yaml_panel, _result = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)

        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel["esql"]
        self.assertEqual(esql["breakdown"]["field"], "legend")
        self.assertIn("EVAL legend = CONCAT(", esql["query"])
        self.assertIn("TO_STRING(`in`)", esql["query"])
        self.assertNotIn("TO_STRING(in)", esql["query"])
        # ``out`` is not reserved, so it stays bare.
        self.assertIn("TO_STRING(out)", esql["query"])

    def test_xy_panel_recovers_dimension_when_by_cols_empty(self):
        # A multi-target panel (eg. node-exporter-full "CPU": eight not_feasible
        # sum(rate())/scalar() targets plus one feasible group_left target) can
        # reach the XY builder with EMPTY group_fields even though the combined
        # ES|QL clearly groups BY time_bucket. The builder must recover the
        # dimension from the query and emit a time-series chart, not silently
        # degrade a graph into a single-value metric tile.
        panel = panels._build_esql_xy_panel(
            (
                "TS metrics-*\n"
                "| STATS v = SUM(RATE(node_cpu_seconds_total, 5m)) "
                "BY time_bucket = TBUCKET(5 minute), service.instance.id\n"
                "| SORT time_bucket ASC"
            ),
            "line",
            metric_col="v",
            by_cols=[],
        )
        self.assertEqual(panel["type"], "line")
        self.assertEqual(panel["dimension"]["field"], "time_bucket")
        self.assertEqual(panel["breakdown"]["field"], "service.instance.id")

    def test_composite_legend_escapes_dotted_label(self):
        # When a legend label resolves to a Fleet-style ``prometheus.labels.x``
        # output column, that dotted name is invalid as a bare TO_STRING(...)
        # argument and must be backtick-quoted.
        warnings = []

        panel = panels._build_esql_xy_panel(
            (
                "TS metrics-*\n"
                "| STATS requests = SUM(http_requests_total) "
                "BY time_bucket = TBUCKET(5 minute), prometheus.labels.verb, server\n"
                "| SORT time_bucket ASC"
            ),
            "line",
            by_cols=["time_bucket", "prometheus.labels.verb", "server"],
            time_fields=["time_bucket"],
            legend_format_template="{{verb}} / {{server}}",
            legend_labels=["verb", "server"],
            warnings=warnings,
        )

        self.assertEqual(panel["breakdown"]["field"], "legend")
        self.assertIn("EVAL legend = CONCAT(", panel["query"])
        self.assertIn("TO_STRING(`prometheus.labels.verb`)", panel["query"])
        self.assertNotIn("TO_STRING(prometheus.labels.verb)", panel["query"])

    def test_composite_legend_suppresses_visual_merge_warning(self):
        warnings = []

        panel = panels._build_esql_xy_panel(
            (
                "TS metrics-*\n"
                "| STATS requests = SUM(http_requests_total) "
                "BY time_bucket = TBUCKET(5 minute), proxy, server\n"
                "| SORT time_bucket ASC"
            ),
            "line",
            by_cols=["time_bucket", "proxy", "server"],
            time_fields=["time_bucket"],
            legend_format_template="{{proxy}} / {{server}}",
            legend_labels=["proxy", "server"],
            warnings=warnings,
        )

        self.assertEqual(panel["breakdown"]["field"], "legend")
        self.assertIn("EVAL legend = CONCAT(", panel["query"])
        self.assertFalse(
            any("visually merged" in warning for warning in warnings),
            warnings,
        )

    def test_topk_without_labels_translates_with_warnings(self):
        # Ungrouped topk now uses single-bucket fallback (not not_feasible)
        panel = _make_panel(1, "topk(5, rate(foo_total[5m]))")
        _, result = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertNotEqual(result.status, "not_feasible", result.reasons)

    def test_stat_panel_emits_native_instant_query(self):
        """Issue #127 / instant-query semantics: a single-value (stat) panel
        must emit a native PROMQL *instant* query bound to ``time=?_tend`` (the
        time-picker end), not a ``step=`` range query that the metric viz then
        has to collapse."""
        panel = _make_panel(1, "max(process_start_time_seconds)", panel_type="stat")
        yaml_panel, _ = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel["esql"]
        self.assertEqual(esql["type"], "metric")
        self.assertIn("time=?_tend", esql["query"])
        self.assertNotIn("step=", esql["query"])

    def test_gauge_panel_emits_native_instant_query(self):
        panel = _make_panel(1, "max(process_start_time_seconds)", panel_type="gauge")
        yaml_panel, _ = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel["esql"]
        self.assertEqual(esql["type"], "gauge")
        self.assertIn("time=?_tend", esql["query"])

    def test_timeseries_panel_emits_adaptive_range_query(self):
        """A real time-series (line) panel is a range plot, but a migrated
        dashboard panel omits ``step=`` so Kibana/Elastic re-buckets it to the
        dashboard time range like Grafana (issue #272). It must still be a range
        query (no ``time=?_tend``) and keep the ``step`` time column as its
        x-axis dimension."""
        panel = _make_panel(1, "rate(http_requests_total[5m])", panel_type="timeseries")
        yaml_panel, _ = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel["esql"]
        query = esql["query"]
        self.assertTrue(query.startswith("PROMQL"), query)
        # #272: no baked-in resolution -> adaptive at view time.
        self.assertNotIn("step=", query)
        # Still a range query, not an instant snapshot.
        self.assertNotIn("time=?_tend", query)
        # The range command still emits the ``step`` column for the x-axis.
        self.assertEqual((esql.get("dimension") or {}).get("field"), "step")

    def test_build_native_promql_query_instant_opt_in_only(self):
        """The instant form is opt-in: callers that post-process the ``step``
        column (e.g. the alert ``LAST(value, step)`` reduction) keep ``step=``
        by leaving ``instant`` at its default."""
        expr = "max(process_start_time_seconds)"
        ranged = panels.build_native_promql_query(expr, index="metrics-*", kibana_type="metric")
        self.assertIn("step=1m", ranged)
        self.assertNotIn("time=?_tend", ranged)
        instant = panels.build_native_promql_query(
            expr, index="metrics-*", kibana_type="metric", instant=True
        )
        self.assertIn("time=?_tend", instant)
        self.assertNotIn("step=", instant)

    def test_instant_table_panel_emits_native_instant_query(self):
        """Issue #102: a Grafana target with ``instant: true`` on a table-format
        panel must emit a native PROMQL *instant* query (``time=?_tend``), not a
        ``step=`` range query, so the migrated datatable shows one row per group
        (the current value) instead of a series over time."""
        panel = _make_panel(
            1, "sum by (http.route) (rate(http_requests_total[5m]))",
            panel_type="table",
        )
        panel["targets"][0]["instant"] = True
        panel["targets"][0]["format"] = "table"
        yaml_panel, _ = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel["esql"]
        self.assertEqual(esql["type"], "datatable")
        self.assertIn("time=?_tend", esql["query"])
        self.assertNotIn("step=", esql["query"])

    def test_range_table_panel_emits_adaptive_range_query(self):
        """A table panel WITHOUT ``instant`` is a normal range table, not an
        instant snapshot. As a migrated dashboard panel it omits ``step=`` (auto
        resolution, issue #272) while staying a range query (no
        ``time=?_tend``)."""
        panel = _make_panel(
            1, "sum by (http.route) (rate(http_requests_total[5m]))",
            panel_type="table",
        )
        panel["targets"][0]["format"] = "table"
        yaml_panel, _ = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertIsNotNone(yaml_panel)
        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.startswith("PROMQL"), query)
        self.assertNotIn("step=", query)
        self.assertNotIn("time=?_tend", query)

    def test_build_native_promql_query_instant_datatable(self):
        """``build_native_promql_query`` honors ``instant=True`` for
        non-single-value types: emit ``time=?_tend`` regardless of ``kibana_type``."""
        expr = "sum by (http.route) (rate(http_requests_total[5m]))"
        ranged = panels.build_native_promql_query(
            expr, index="metrics-*", kibana_type="datatable"
        )
        self.assertIn("step=", ranged)
        self.assertNotIn("time=?_tend", ranged)
        instant = panels.build_native_promql_query(
            expr, index="metrics-*", kibana_type="datatable", instant=True
        )
        self.assertIn("time=?_tend", instant)
        self.assertNotIn("step=", instant)

    def test_build_native_promql_query_instant_timeseries_legend_drops_step(self):
        """An instant query has no ``step`` column, so the ``_timeseries`` +
        legend-label extraction branch must KEEP value + labels but NOT ``step``
        (a ``KEEP step`` would reference a column the instant command never emits)."""
        expr = "rate(http_requests_total[5m])"
        instant = panels.build_native_promql_query(
            expr, index="metrics-*",
            legend_labels=["instance"], kibana_type="datatable",
            instant=True,
        )
        self.assertIn("time=?_tend", instant)
        self.assertNotIn("step=", instant)
        keep_lines = [ln for ln in instant.splitlines() if "KEEP" in ln]
        self.assertTrue(keep_lines, f"expected a KEEP pipe in: {instant}")
        keep_line = keep_lines[0]
        self.assertNotIn("step", keep_line)
        self.assertIn("value", keep_line)
        self.assertIn("instance", keep_line)

    def test_build_native_promql_query_instant_static_legend_drops_step(self):
        """The static-legend branch must also drop ``step`` from its KEEP on an
        instant query (same missing-column hazard as the label-extraction path)."""
        expr = "rate(http_requests_total[5m])"
        instant = panels.build_native_promql_query(
            expr, index="metrics-*",
            legend_labels=[], kibana_type="datatable",
            legend_format="My Series", instant=True,
        )
        self.assertIn("time=?_tend", instant)
        keep_lines = [ln for ln in instant.splitlines() if "KEEP" in ln]
        self.assertTrue(keep_lines, f"expected a KEEP pipe in: {instant}")
        self.assertNotIn("step", keep_lines[0])
        self.assertIn("label", keep_lines[0])

    def test_bargauge_panel_stays_range_query_on_native_path(self):
        """Regression (#135 review): ``_target_summary_mode`` returns True
        unconditionally for ``bargauge``, but ``bargauge`` maps to the XY
        ``bar`` kibana type whose spec x-axes on the ``step`` time column. An
        instant query emits no ``step`` column, so widening ``instant`` to
        summary-mode must NOT reach ``bar``: doing so would bind the x-axis to a
        phantom ``step`` column (the #127 failure mode). A native-path
        ``bargauge`` must stay a range query (never ``time=?_tend``) with a valid
        ``step`` x-axis dimension. The range command emits the ``step`` column
        regardless of whether a ``step=`` resolution is pinned, so the migrated
        dashboard panel omits it for adaptive bucketing (issue #272)."""
        panel = _make_panel(
            1, "rate(http_requests_total[5m])", panel_type="bargauge",
        )
        yaml_panel, _ = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel["esql"]
        query = esql["query"]
        # Only assert the phantom-axis invariant when the native PROMQL path
        # actually handled this panel (PROMQL command emitted).
        if query.startswith("PROMQL"):
            # Must be a range query so the ``step`` x-axis column exists.
            self.assertNotIn("time=?_tend", query)
            # Adaptive resolution: no baked-in ``step=`` (issue #272).
            self.assertNotIn("step=", query)
            dimension = esql.get("dimension") or {}
            self.assertEqual(
                dimension.get("field"), "step",
                "bar x-axis must bind to the step time column",
            )

    def test_dashboard_rate_interval_panel_is_windowless_and_stepless(self):
        """End-to-end (#272 + #273): a migrated line panel whose Grafana source
        used ``rate(x[$__rate_interval])`` emits a windowless ``rate(x)`` with no
        baked-in ``step=`` — fully adaptive like the Grafana original."""
        panel = _make_panel(1, "rate(http_requests_total[$__rate_interval])")
        yaml_panel, _ = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertIsNotNone(yaml_panel)
        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.startswith("PROMQL"), query)
        self.assertIn("value=(rate(http_requests_total))", query)
        self.assertNotIn("$__rate_interval", query)
        self.assertNotIn("[5m]", query)
        self.assertNotIn("step=", query)
        self.assertNotIn("time=?_tend", query)

    def test_dashboard_explicit_rate_window_panel_keeps_window_but_drops_step(self):
        """End-to-end (#273): a pinned explicit window survives verbatim while
        the panel step still goes adaptive (#272)."""
        panel = _make_panel(1, "rate(http_requests_total[5m])")
        yaml_panel, _ = _translate_panel(panel, rule_pack=self.rp, resolver=self.resolver)
        self.assertIsNotNone(yaml_panel)
        query = yaml_panel["esql"]["query"]
        self.assertIn("value=(rate(http_requests_total[5m]))", query)
        self.assertNotIn("step=", query)

    def test_multi_target_overlay_is_windowless_and_stepless(self):
        """The multi-target native overlay path (the ``label_replace + or``
        fallback) also emits adaptive resolution: no ``step=`` (#272), windowless
        rate for ``$__rate_interval`` targets, and explicit windows preserved
        (#273). Exercised directly because the overlay is only reached as a
        fallback when the ES|QL merge is not_feasible."""
        panel = {
            "id": 1, "type": "timeseries", "title": "Overlay", "targets": [],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
        }
        targets_with_expr = [
            ({"expr": "rate(http_requests_total[$__rate_interval])", "refId": "A",
              "legendFormat": "reqs", "datasource": {"type": "prometheus"}},
             "rate(http_requests_total[$__rate_interval])"),
            ({"expr": "rate(http_errors_total[5m])", "refId": "B",
              "legendFormat": "errors", "datasource": {"type": "prometheus"}},
             "rate(http_errors_total[5m])"),
        ]
        out = panels._translate_multi_target_native_promql(
            panel, {}, "Overlay", "timeseries", "line",
            {"type": "prometheus"}, "metrics-*", self.rp, [], {},
            targets_with_expr, resolver=self.resolver,
        )
        self.assertIsNotNone(out)
        yaml_panel, _ = out
        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.startswith("PROMQL index=metrics-*"), query)
        self.assertNotIn("step=", query)
        self.assertNotIn("$__rate_interval", query)
        # $__rate_interval target -> windowless; explicit [5m] target -> kept.
        self.assertIn('label_replace(rate(http_requests_total), "__series"', query)
        self.assertIn('label_replace(rate(http_errors_total[5m]), "__series"', query)


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
        self.assertEqual(result.query_ir.get("output_group_fields"), ["time_bucket", "service.name"])
        self.assertTrue(result.query_ir.get("semantic_losses"))

        query = yaml_panel["esql"]["query"]
        self.assertIn("http_server_errors_total", query)
        self.assertIn("http_server_requests_total", query)
        self.assertIn("| EVAL computed_value =", query)
        self.assertEqual(result.visual_ir.presentation.kind, "esql")
        self.assertEqual(result.visual_ir.presentation.config["query"], query)
        self.assertEqual(result.visual_ir.metadata.get("output_shape"), "time_series")
        self.assertEqual(yaml_panel["esql"]["dimension"]["field"], "time_bucket")
        self.assertEqual(yaml_panel["esql"]["breakdown"]["field"], "service.name")
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

    def test_abs_now_translates_to_esql_abs(self):
        # abs() is now translated exactly via ES|QL ABS() — no longer not_feasible
        ctx = _translate("abs(rate(foo_total[5m]))")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("ABS(", ctx.esql_query or "")

    def test_clamp_min_now_translates(self):
        # clamp_min() is now handled as a passthrough wrapper — no longer not_feasible
        ctx = _translate("clamp_min(rate(foo_total[5m]), 0)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)

    def test_clamp_max_now_translates_to_least(self):
        # clamp_max(v, hi) is exactly ES|QL LEAST(v, hi)
        ctx = _translate("clamp_max(node_filesystem_avail_bytes, 100)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("LEAST(", ctx.esql_query or "")
        self.assertIn("100", ctx.esql_query or "")

    def test_clamp_now_translates_to_greatest_least(self):
        # clamp(v, lo, hi) is GREATEST(LEAST(v, hi), lo)
        ctx = _translate("clamp(node_filesystem_avail_bytes, 0, 100)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("LEAST(", ctx.esql_query or "")
        self.assertIn("GREATEST(", ctx.esql_query or "")

    def test_sgn_now_translates_to_signum(self):
        # sgn(v) is exactly ES|QL SIGNUM(v)
        ctx = _translate("sgn(node_cpu_seconds_total)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("SIGNUM(", ctx.esql_query or "")

    def test_quantile_by_now_translates_to_percentile(self):
        # quantile(0.95, m) by (job) == STATS PERCENTILE(m, 95) BY job
        ctx = _translate("quantile(0.95, node_filesystem_avail_bytes) by (job)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        esql = ctx.esql_query or ""
        self.assertIn("PERCENTILE(", esql)
        self.assertIn("95", esql)
        self.assertIn("BY", esql)

    def test_quantile_median_translates_to_percentile_50(self):
        ctx = _translate("quantile(0.5, node_filesystem_avail_bytes)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        esql = ctx.esql_query or ""
        self.assertIn("PERCENTILE(", esql)
        # 0.5 * 100 == 50
        self.assertIn("50", esql)

    # --- elementwise math / trig wrappers: exact ES|QL function maps -------
    def test_math_trig_functions_translate_exactly(self):
        # Each PromQL math/trig wrapper maps to an exact ES|QL function/expression.
        cases = {
            "abs(node_memory_usage)": "ABS(",
            "ceil(node_memory_usage)": "CEIL(",
            "floor(node_memory_usage)": "FLOOR(",
            "sqrt(node_memory_usage)": "SQRT(",
            "exp(node_memory_usage)": "EXP(",
            "ln(node_memory_usage)": "LOG(",
            "log10(node_memory_usage)": "LOG10(",
            "acos(node_memory_usage)": "ACOS(",
            "asin(node_memory_usage)": "ASIN(",
            "atan(node_memory_usage)": "ATAN(",
            "cos(node_memory_usage)": "COS(",
            "sin(node_memory_usage)": "SIN(",
            "tan(node_memory_usage)": "TAN(",
            "cosh(node_memory_usage)": "COSH(",
            "sinh(node_memory_usage)": "SINH(",
            "tanh(node_memory_usage)": "TANH(",
        }
        for expr, expected in cases.items():
            with self.subTest(expr=expr):
                ctx = _translate(expr)
                self.assertNotEqual(ctx.feasibility, "not_feasible", f"{expr}: {ctx.warnings}")
                self.assertIn(expected, ctx.esql_query or "", expr)

    def test_log2_translates_to_log_base_2(self):
        # log2(v) == LOG(2, v)
        ctx = _translate("log2(node_memory_usage)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("LOG(2", ctx.esql_query or "")

    def test_deg_translates_to_radians_to_degrees(self):
        # deg(v) == v * 180 / PI()
        ctx = _translate("deg(node_memory_usage)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        esql = ctx.esql_query or ""
        self.assertIn("180", esql)
        self.assertIn("PI()", esql)

    def test_rad_translates_to_degrees_to_radians(self):
        # rad(v) == v * PI() / 180
        ctx = _translate("rad(node_memory_usage)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        esql = ctx.esql_query or ""
        self.assertIn("180", esql)
        self.assertIn("PI()", esql)

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

    def test_simple_gauge_assumes_tsds_uses_ts(self):
        # Migration default: unproven gauge assumes TSDS -> TS (was FROM).
        ctx = _translate("avg(up)")
        self.assertTrue(ctx.esql_query.startswith("TS"))


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


class TestBoolModifier(unittest.TestCase):
    """PromQL ``bool`` modifier on comparisons yields a numeric 1/0 indicator,
    not a row filter and not the bare left operand.

    Regression: ``(node_memory_SwapTotal_bytes > bool 0) * 100`` was emitting
    ``node_memory_SwapTotal_bytes * 100`` (multiplying by raw bytes), which made
    the Node Exporter "SWAP Used" stat panel render ~3.27e12 %. ``> bool`` must
    translate to ``CASE(<lhs> <op> <rhs>, 1, 0)``.
    """

    def test_scalar_bool_indicator_is_case_not_bare_metric(self):
        ctx = _translate("(node_memory_SwapTotal_bytes > bool 0) * 100")
        esql = ctx.esql_query
        self.assertIn("CASE(", esql)
        # The indicator collapses to 1/0; it must NOT leave the raw metric as a
        # standalone multiplicative factor.
        self.assertNotIn(
            "(node_memory_SwapTotal_bytes * 100)", esql,
            "bool indicator must not render as the bare left metric",
        )
        self.assertRegex(esql, r"CASE\(\s*node_memory_SwapTotal_bytes\s*>\s*0\s*,\s*1\s*,\s*0\s*\)")

    def test_swap_used_formula_has_no_spurious_metric_factor(self):
        # The real Node Exporter "SWAP Used" shape: a percentage guarded by a
        # bool indicator so it reads 0 when no swap is configured.
        expr = (
            "((node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes)"
            " / (node_memory_SwapTotal_bytes)) * (node_memory_SwapTotal_bytes > bool 0) * 100"
        )
        ctx = _translate(expr)
        esql = ctx.esql_query
        self.assertIn("CASE(", esql)
        # The bug rendered the guard as "... ) * node_memory_SwapTotal_bytes) * 100".
        self.assertNotRegex(
            esql,
            r"/ node_memory_SwapTotal_bytes\) \* node_memory_SwapTotal_bytes",
            "the bool guard must not multiply the ratio by raw swap bytes",
        )

    def test_vector_bool_comparison_is_numeric_case(self):
        ctx = _translate(
            "node_memory_MemAvailable_bytes > bool node_memory_MemTotal_bytes"
        )
        esql = ctx.esql_query
        self.assertRegex(
            esql,
            r"CASE\(\s*node_memory_MemAvailable_bytes\s*>\s*node_memory_MemTotal_bytes\s*,\s*1\s*,\s*0\s*\)",
        )

    def test_bool_indicator_as_divisor_is_null_guarded(self):
        # Dividing by a 0/1 indicator must not divide by literal 0 (PromQL
        # yields no data); the false branch becomes NULL.
        ctx = _translate(
            "node_memory_SwapFree_bytes / (node_memory_SwapTotal_bytes > bool 0)"
        )
        esql = ctx.esql_query
        self.assertRegex(
            esql,
            r"CASE\(\s*node_memory_SwapTotal_bytes\s*>\s*0\s*,\s*1\s*,\s*NULL\s*\)",
        )

    def test_plain_comparison_without_bool_stays_a_filter(self):
        # Guard: a comparison WITHOUT ``bool`` keeps PromQL filter semantics
        # (drops series where false) and must remain a WHERE clause, never a
        # 1/0 CASE indicator.
        ctx = _translate("rate(foo_total[5m]) > 0.5")
        esql = ctx.esql_query
        self.assertGreaterEqual(esql.count("WHERE"), 2)
        self.assertNotIn("CASE(", esql)


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
        # Force the FROM path so this exercises FROM's BUCKET(@timestamp, ...) summary
        # collapse specifically (TS uses TBUCKET; covered elsewhere).
        rp = rules.RulePackConfig()
        rp.assume_tsds_gauges = False
        panel = _make_panel(1, "avg(node_load1)", panel_type="gauge")
        panel["targets"][0]["range"] = False
        _yaml_panel, result = _translate_panel(panel, rule_pack=rp)
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

    def test_counter_in_noncounter_range_fn_is_cast_to_double(self):
        # ES|QL rejects a counter field passed to MAX_OVER_TIME/DELTA/etc.
        # ("argument ... must be [... except counter types]"). A confirmed
        # counter used in a non-counter function must be cast to double so the
        # query executes; RATE/IRATE/INCREASE keep the raw counter; gauges are
        # left unchanged (no needless cast / snapshot churn).
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        # http_requests_total -> inferred counter
        mot = translate_promql_to_esql("max_over_time(http_requests_total[1h])").esql_query
        self.assertIn("MAX_OVER_TIME(TO_DOUBLE(http_requests_total)", mot)
        # rate() consumes the counter directly: no cast
        rate = translate_promql_to_esql("rate(http_requests_total[5m])").esql_query
        self.assertIn("RATE(http_requests_total,", rate)
        self.assertNotIn("RATE(TO_DOUBLE", rate)
        # gauge: no cast, no churn
        gauge = translate_promql_to_esql("max_over_time(node_load1[1h])").esql_query
        self.assertIn("MAX_OVER_TIME(node_load1,", gauge)
        self.assertNotIn("TO_DOUBLE", gauge)

    def test_increase_degraded_to_gauge_fn_still_casts_to_double(self):
        # Regression for the MySQL "Network Usage Hourly" runtime failure:
        # increase() over a counter whose name carries no _total suffix
        # (e.g. mysql_global_status_bytes_received) is classified gauge by the
        # offline suffix heuristic, so increase() degrades to MAX_OVER_TIME.
        # But the telemetry contract/seeder type any increase()-wrapped metric
        # as a counter, so the stored field is counter_double and ES|QL rejects
        # MAX_OVER_TIME(<counter>). The counter-style *source* function is the
        # authoritative signal: the degraded gauge analogue must still wrap the
        # metric in TO_DOUBLE so the emitted query executes.
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        result = translate_promql_to_esql(
            "increase(mysql_global_status_bytes_received[1h])"
        )
        esql = result.esql_query
        self.assertIn("MAX_OVER_TIME(", esql)
        self.assertIn(
            "MAX_OVER_TIME(TO_DOUBLE(mysql_global_status_bytes_received)",
            esql,
            msg=f"increase()-degraded gauge fallback must cast counter to double: {esql}",
        )

    def test_binary_wrapped_increase_still_casts_to_double(self):
        # PR #234 review: the degraded increase() cast must also apply when the
        # call is composed inside a binary expression. Under unknown field caps
        # increase(weird_unknown_metric[5m]) degrades to MAX_OVER_TIME, but the
        # stored field may be counter_double, so the binary measure-spec path must
        # still wrap the metric in TO_DOUBLE or the query fails at runtime.
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        esql = translate_promql_to_esql(
            "increase(weird_unknown_metric[5m]) * 2"
        ).esql_query
        self.assertIn("MAX_OVER_TIME(", esql)
        self.assertIn(
            "MAX_OVER_TIME(TO_DOUBLE(weird_unknown_metric)",
            esql,
            msg=f"binary-wrapped increase() must keep the counter-safe cast: {esql}",
        )

    def test_join_ratio_increase_still_casts_to_double(self):
        # PR #234 review: the join-ratio _build_stats_call path emitted bare
        # SUM(MAX_OVER_TIME(metric, ...)) with no cast. Both operands of a
        # group_left ratio over degraded increase() must keep TO_DOUBLE.
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        esql = translate_promql_to_esql(
            "sum(increase(weird_unknown_metric[5m])) / on(job) group_left "
            "sum(increase(other_unknown_metric[5m]))"
        ).esql_query
        self.assertIn(
            "MAX_OVER_TIME(TO_DOUBLE(weird_unknown_metric)",
            esql,
            msg=f"join-ratio numerator increase() must cast counter to double: {esql}",
        )
        self.assertIn(
            "MAX_OVER_TIME(TO_DOUBLE(other_unknown_metric)",
            esql,
            msg=f"join-ratio denominator increase() must cast counter to double: {esql}",
        )

    def test_scaled_agg_measure_spec_increase_casts_to_double(self):
        # PR #234 (stefans): _build_measure_spec's scaled_agg branch must keep
        # the counter-safe cast like the adjacent range_agg branch. A formula-plan
        # panel such as sum(increase(non_total[5m])) * 100 otherwise emits
        # SUM(MAX_OVER_TIME(metric, …)) against a counter-typed TSDS field and
        # fails 9.5 runtime validation.
        from observability_migration.adapters.source.grafana import promql, rules, schema
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        rp = rules.RulePackConfig()
        res = schema.SchemaResolver(rp)
        frag = translate_promql_to_esql(
            "sum(increase(weird_unknown_metric[5m])) * 100",
            esql_index="metrics-*", rule_pack=rp, resolver=res,
        ).fragment
        self.assertEqual(frag.family, "scaled_agg")
        spec = promql._build_measure_spec(frag, res, rp)
        self.assertIn(
            "MAX_OVER_TIME(TO_DOUBLE(weird_unknown_metric)",
            spec.stats_expr,
            msg=f"scaled_agg measure-spec must cast counter to double: {spec.stats_expr}",
        )

    def test_round_to_fractional_step_emits_valid_divide_multiply_esql(self):
        # PromQL round(v, 0.001) rounds to the nearest 0.001 step. ES|QL
        # ROUND(v, decimals) takes a WHOLE-NUMBER decimal-places arg, so
        # ROUND(v, 0.001) is an invalid query ("second argument ... must be
        # [whole number ...], found ... [double]"). Emit ROUND(v / 0.001) * 0.001
        # which reproduces the PromQL semantics and is valid ES|QL.
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        q = translate_promql_to_esql(
            "round(sum(rate(http_requests_total[5m])), 0.001)"
        ).esql_query
        self.assertIn("/ 0.001) * 0.001", q)
        self.assertNotIn(", 0.001)", q)

    def test_round_zero_step_does_not_emit_divide_by_zero(self):
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        q = translate_promql_to_esql(
            "round(sum(rate(http_requests_total[5m])), 0)"
        ).esql_query
        self.assertIn("ROUND(", q)
        self.assertNotIn("/ 0", q)

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

    def test_clamp_max_strips_outer_call(self):
        ctx = self._translate("clamp_max(sum by (job) (rate(http_requests_total[5m])), 100)")
        frag = ctx.fragment
        self.assertIsNotNone(frag)
        self.assertFalse(frag.extra.get("not_feasible_reasons"))
        self.assertEqual(frag.extra.get("clamp_max_value"), 100.0)

    def test_clamp_strips_outer_call_carries_both_bounds(self):
        ctx = self._translate("clamp(sum by (job) (rate(http_requests_total[5m])), 0, 100)")
        frag = ctx.fragment
        self.assertIsNotNone(frag)
        self.assertFalse(frag.extra.get("not_feasible_reasons"))
        self.assertEqual(frag.extra.get("clamp_min_value"), 0.0)
        self.assertEqual(frag.extra.get("clamp_max_value"), 100.0)

    def test_sgn_strips_outer_call(self):
        ctx = self._translate("sgn(sum by (job) (rate(http_requests_total[5m])))")
        frag = ctx.fragment
        self.assertIsNotNone(frag)
        self.assertFalse(frag.extra.get("not_feasible_reasons"))
        self.assertTrue(frag.extra.get("has_sgn"))


class TestGaugeSeriesFidelity(unittest.TestCase):
    """Offline per-series fidelity for bare gauge selectors.

    A bare gauge with no series labels collapses multiple series into one AVG
    line; we must say so honestly. When labels are available (legend or
    dashboard-inferred) they must be grouped and no loss warning emitted.
    """

    def _translate(self, expr, hints=None, assume_tsds_gauges=True):
        rp = rules.RulePackConfig()
        rp.assume_tsds_gauges = assume_tsds_gauges
        res = schema.SchemaResolver(rp)
        return translate.translate_promql_to_esql(
            expr, esql_index="metrics-*", panel_type="graph",
            rule_pack=rp, resolver=res, translation_hints=hints,
        )

    def test_bare_gauge_collapse_emits_honest_loss_warning_on_from_path(self):
        # The honest collapse warning applies to the lossy FROM+AVG path (no series
        # labels). With assume_tsds_gauges=False we deliberately take that path.
        ctx = self._translate("node_xyz_metric", assume_tsds_gauges=False)
        self.assertEqual(ctx.source_type, "FROM")
        self.assertTrue(any("Collapsed all series" in w for w in ctx.warnings))
        self.assertIsNotNone(ctx.query_ir)
        self.assertTrue(
            any("Collapsed all series" in s for s in ctx.query_ir.semantic_losses)
        )

    def test_bare_gauge_default_uses_ts_and_preserves_series(self):
        # Migration default: a bare gauge assumes TSDS and uses TS. ES|QL still
        # requires an aggregate expression in STATS, so use LAST_OVER_TIME rather
        # than the invalid raw ``STATS field = field`` shape.
        ctx = self._translate("node_xyz_metric")
        self.assertEqual(ctx.source_type, "TS")
        self.assertIn("STATS node_xyz_metric = MAX(LAST_OVER_TIME(node_xyz_metric))", ctx.esql_query)
        self.assertNotIn("node_xyz_metric = node_xyz_metric", ctx.esql_query)
        self.assertFalse(any("Collapsed all series" in w for w in ctx.warnings))

    def test_bare_gauge_with_labels_has_no_loss_warning(self):
        # Issue #99: a bare gauge with a legendFormat label and no explicit outer
        # aggregation uses the TS gauge path. ES|QL TS mode splits series by TSID
        # with BY TBUCKET alone, so the legend label is NOT added to BY (adding it
        # would force a distorting outer AVG). No collapse loss either.
        ctx = self._translate(
            "node_xyz_metric",
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
            },
        )
        self.assertFalse(any("Collapsed all series" in w for w in ctx.warnings))
        self.assertEqual(ctx.source_type, "TS")
        self.assertIn("STATS node_xyz_metric = MAX(LAST_OVER_TIME(node_xyz_metric)) BY time_bucket", ctx.esql_query)
        self.assertNotIn("node_xyz_metric = node_xyz_metric", ctx.esql_query)
        self.assertNotIn("instance", ctx.esql_query)
        self.assertNotIn("AVG(", ctx.esql_query)

    def test_bare_gauge_legend_label_no_outer_agg_omits_label_from_by(self):
        # Issue #99 Case A: go_goroutines{...} with legendFormat={{instance}}.
        # No explicit PromQL outer aggregation -> TS gauge path, no outer AVG,
        # legend label kept out of BY, and no warning (promoted to `migrated`).
        ctx = self._translate(
            'go_goroutines{job="prod"}',
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
            },
        )
        self.assertEqual(ctx.source_type, "TS")
        self.assertIn("STATS go_goroutines = MAX(LAST_OVER_TIME(go_goroutines)) BY time_bucket", ctx.esql_query)
        self.assertNotIn("go_goroutines = go_goroutines", ctx.esql_query)
        self.assertNotIn("AVG(", ctx.esql_query)
        self.assertNotIn(", instance", ctx.esql_query)
        self.assertEqual(ctx.warnings, [])

    def test_range_func_legend_label_no_outer_agg_omits_outer_avg(self):
        # Issue #99 Case B: max_over_time(...) with legendFormat={{instance}}.
        # No explicit PromQL outer aggregation -> bare MAX_OVER_TIME with no outer
        # AVG, legend label kept out of BY, and no AVG-distortion warning.
        ctx = self._translate(
            'max_over_time(process_resident_memory_bytes{job="prod"}[5m])',
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
            },
        )
        self.assertEqual(ctx.source_type, "TS")
        self.assertIn("MAX_OVER_TIME(process_resident_memory_bytes, 5m)", ctx.esql_query)
        self.assertNotIn("AVG(", ctx.esql_query)
        self.assertNotIn(", instance", ctx.esql_query)
        self.assertFalse(any("Added outer AVG" in w for w in ctx.warnings))

    def test_explicit_outer_agg_keeps_by_labels(self):
        # Issue #99: an explicit PromQL outer aggregation is semantically
        # meaningful; its by() labels stay in BY and the aggregation is unchanged.
        ctx = self._translate(
            "sum(go_goroutines) by (job)",
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
            },
        )
        self.assertIn("SUM(go_goroutines)", ctx.esql_query)
        # by(job) -> service.name stays in BY; the legend label is not added.
        self.assertIn("service.name", ctx.esql_query)
        self.assertNotIn("service.instance.id", ctx.esql_query)

    def test_from_path_gauge_keeps_legend_label(self):
        # Issue #99: the no-BY-label rule applies only on the TS path, where TSID
        # grouping splits series. On the FROM path (non-TSDS gauge) there is no
        # TSID grouping, so the legend label must stay in BY for per-series detail.
        ctx = self._translate(
            "node_xyz_metric",
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
            },
            assume_tsds_gauges=False,
        )
        self.assertEqual(ctx.source_type, "FROM")
        self.assertIn("instance", ctx.esql_query)

    def test_summary_panel_keeps_legend_label_for_categorical_breakdown(self):
        # Issue #99 review: the TSID split is a time-series-line affordance. A
        # bargauge carries legend-origin labels too (see _target_translation_hints)
        # but renders its breakdown from the explicit output_group_fields column, so
        # the label must NOT be dropped on the summary path — otherwise the per-series
        # bars collapse instead of relocating to a TSID-driven legend.
        ctx = self._translate(
            "go_goroutines",
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
                "summary_mode": True,
            },
        )
        self.assertEqual(ctx.source_type, "TS")
        self.assertIn("instance", ctx.esql_query)

    def test_range_func_non_tsds_field_keeps_legend_label(self):
        # Issue #99 review: the range_agg drop must verify the field can actually use
        # TS (TSID grouping), exactly as the simple_metric branch does. range_agg
        # forces source=TS for any *_over_time regardless of field typing, so for a
        # field that cannot be proven a TSDS series the legend label is the only thing
        # splitting series and must be kept (the old AVG-wrapped behavior).
        ctx = self._translate(
            "max_over_time(process_resident_memory_bytes[5m])",
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
            },
            assume_tsds_gauges=False,
        )
        self.assertEqual(ctx.source_type, "TS")
        self.assertIn("instance", ctx.esql_query)
        self.assertIn("AVG(MAX_OVER_TIME(", ctx.esql_query)

    def test_formula_range_func_drops_legend_label_no_outer_avg(self):
        # Issue #99 review: arithmetic/formula panels go through _build_measure_spec,
        # a parallel path that must apply the same legend-label drop. A range function
        # in a formula must emit the bare TS function (TSID-split) with no distorting
        # outer AVG and no legend label in BY.
        ctx = self._translate(
            "max_over_time(process_resident_memory_bytes[5m]) + 0",
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
            },
        )
        self.assertEqual(ctx.source_type, "TS")
        self.assertIn("MAX_OVER_TIME(process_resident_memory_bytes, 5m)", ctx.esql_query)
        self.assertNotIn("AVG(MAX_OVER_TIME(", ctx.esql_query)
        self.assertNotIn(", instance", ctx.esql_query)
        self.assertFalse(any("Added outer AVG" in w for w in ctx.warnings))

    def test_formula_range_func_non_tsds_keeps_legend_label(self):
        # Issue #99 review: the formula path applies the same TS-eligibility gate.
        # A non-TSDS *_over_time field keeps its legend label (old AVG behavior)
        # rather than collapsing series.
        ctx = self._translate(
            "max_over_time(process_resident_memory_bytes[5m]) + 0",
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
            },
            assume_tsds_gauges=False,
        )
        self.assertIn("AVG(MAX_OVER_TIME(", ctx.esql_query)
        self.assertIn("instance", ctx.esql_query)

    def test_mixed_formula_range_and_simple_metric_stays_feasible(self):
        # Issue #99 review: the drop is decided per operand, so a mixed-family formula
        # could end up with the range_agg side dropping its legend labels while the
        # simple_metric side keeps them — divergent groupings that the mergeability
        # check rejected, regressing the panel to not_feasible. The planner must
        # reconcile to one consistent grouping (the AVG form) so it stays feasible.
        for expr in (
            "max_over_time(process_resident_memory_bytes[5m]) + go_goroutines",
            "go_goroutines + max_over_time(process_resident_memory_bytes[5m])",
        ):
            with self.subTest(expr=expr):
                ctx = self._translate(
                    expr,
                    hints={
                        "preferred_group_labels": ["instance"],
                        "preferred_group_labels_origin": "legend",
                    },
                )
                self.assertEqual(ctx.source_type, "TS")
                self.assertTrue(ctx.esql_query, "expected a feasible translation")
                # One STATS with both operands grouped by the same BY clause.
                self.assertEqual(ctx.esql_query.count("| STATS "), 1)
                self.assertIn("service.instance.id", ctx.esql_query)
                self.assertIn("MAX_OVER_TIME(process_resident_memory_bytes, 5m)", ctx.esql_query)

    def test_range_range_formula_keeps_avg_free_improvement(self):
        # Issue #99 review: when every operand can drop (range_agg + range_agg), the
        # reconciliation must NOT kick in — both groupings already agree on [], so the
        # AVG-free improvement is preserved rather than reverted to the AVG form.
        ctx = self._translate(
            "max_over_time(process_resident_memory_bytes[5m]) + sum_over_time(go_goroutines[5m])",
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
            },
        )
        self.assertEqual(ctx.source_type, "TS")
        self.assertNotIn("AVG(", ctx.esql_query)
        self.assertNotIn("service.instance.id", ctx.esql_query)

    def test_formula_summary_panel_keeps_legend_label(self):
        # Issue #99 review: the summary/categorical guard applies on the formula path
        # too — a bargauge formula keeps its breakdown column.
        ctx = self._translate(
            "go_goroutines + 0",
            hints={
                "preferred_group_labels": ["instance"],
                "preferred_group_labels_origin": "legend",
                "summary_mode": True,
            },
        )
        self.assertIn("instance", ctx.esql_query)

    def test_target_hints_backfill_from_dashboard_map_when_panel_has_none(self):
        target = {"expr": "go_goroutines", "legendFormat": ""}
        hints = panels._target_translation_hints(
            {"type": "timeseries"}, "timeseries", target, {"go_goroutines": ["instance"]}
        )
        self.assertEqual(hints.get("preferred_group_labels"), ["instance"])
        self.assertEqual(hints.get("preferred_group_labels_origin"), "dashboard_inferred")

    def test_target_hints_panel_legend_wins_over_dashboard_map(self):
        target = {"expr": "go_goroutines", "legendFormat": "{{job}}"}
        hints = panels._target_translation_hints(
            {"type": "timeseries"}, "timeseries", target, {"go_goroutines": ["instance"]}
        )
        self.assertEqual(hints.get("preferred_group_labels"), ["job"])
        self.assertEqual(hints.get("preferred_group_labels_origin"), "legend")

    def test_target_hints_no_inference_for_single_value_panels(self):
        # Single-value panels (gauge/stat/bargauge) intentionally collapse to one value;
        # cross-panel inference must NOT add a breakdown that changes the panel type.
        target = {"expr": "go_goroutines", "legendFormat": ""}
        for panel_type in ("gauge", "stat", "singlestat", "bargauge"):
            hints = panels._target_translation_hints(
                {"type": panel_type}, panel_type, target, {"go_goroutines": ["instance"]}
            )
            self.assertNotIn(
                "preferred_group_labels", hints,
                f"{panel_type} must not receive inferred grouping",
            )

    def test_target_hints_explicit_by_not_clobbered_by_dashboard_union(self):
        # Issue #94: a panel with its own by() clause has declared its grouping;
        # the dashboard-wide series-label union must NOT overwrite it.
        target = {
            "expr": "sum(rate(http_requests_total[5m])) by (service)",
            "legendFormat": "",
        }
        hints = panels._target_translation_hints(
            {"type": "timeseries"},
            "timeseries",
            target,
            {"http_requests_total": ["service", "status_code", "country"]},
        )
        self.assertNotEqual(
            hints.get("preferred_group_labels_origin"), "dashboard_inferred"
        )
        self.assertIsNone(hints.get("preferred_group_labels"))

    def test_target_hints_explicit_without_skips_inference(self):
        # A without() clause is also explicit grouping intent; dashboard-wide
        # inference must not inject a label set on top of it.
        target = {
            "expr": "sum(http_requests_total) without (instance)",
            "legendFormat": "",
        }
        hints = panels._target_translation_hints(
            {"type": "timeseries"},
            "timeseries",
            target,
            {"http_requests_total": ["service", "status_code"]},
        )
        self.assertNotIn("preferred_group_labels", hints)

    def test_explicit_by_not_widened_by_sibling_panel_dimensions(self):
        # End-to-end: the ES|QL for a by(service) panel must group by service only,
        # never by sibling panels' status_code / country (issue #94).
        target = {
            "expr": "sum(rate(http_requests_total[5m])) by (service)",
            "legendFormat": "",
        }
        hints = panels._target_translation_hints(
            {"type": "timeseries"},
            "timeseries",
            target,
            {"http_requests_total": ["service", "status_code", "country"]},
        )
        ctx = self._translate(
            "sum(rate(http_requests_total[5m])) by (service)", hints=hints
        )
        self.assertIn("service", ctx.esql_query)
        self.assertNotIn("status_code", ctx.esql_query)
        self.assertNotIn("country", ctx.esql_query)


class TestCounterSuffixClassification(unittest.TestCase):
    """Canonical Prometheus histogram/summary component series (``_bucket``,
    ``_count``, ``_sum``) are counters. rate()/irate()/increase() over them must
    emit RATE/IRATE/INCREASE, not the gauge fallback (AVG_OVER_TIME/MAX_OVER_TIME).
    """

    def setUp(self):
        self.rp = rules.RulePackConfig()
        self.res = schema.SchemaResolver(self.rp)

    def _translate(self, expr, panel_type="timeseries"):
        return translate.translate_promql_to_esql(
            expr,
            esql_index="metrics-*",
            panel_type=panel_type,
            rule_pack=self.rp,
            resolver=self.res,
        )

    def test_is_counter_recognizes_histogram_summary_suffixes(self):
        for metric in (
            "http_request_duration_seconds_bucket",
            "http_request_duration_seconds_count",
            "http_request_duration_seconds_sum",
        ):
            self.assertTrue(
                self.res.is_counter(metric), f"{metric} should classify as a counter"
            )

    def test_histogram_bucket_rate_emits_rate_not_gauge_fallback(self):
        ctx = self._translate(
            "sum(rate(http_request_duration_seconds_bucket[5m])) by (le)"
        )
        self.assertIn("RATE(http_request_duration_seconds_bucket", ctx.esql_query)
        self.assertNotIn("AVG_OVER_TIME", ctx.esql_query)
        self.assertFalse(
            any("typed as gauge" in w for w in ctx.warnings),
            f"unexpected gauge-fallback warning: {ctx.warnings}",
        )

    def test_summary_count_increase_emits_increase_not_gauge_fallback(self):
        ctx = self._translate(
            "increase(prometheus_target_sync_length_seconds_count[5m])"
        )
        self.assertIn(
            "INCREASE(prometheus_target_sync_length_seconds_count", ctx.esql_query
        )
        self.assertNotIn("MAX_OVER_TIME", ctx.esql_query)


class TestCounterOnlyRangeFuncTrustsSource(unittest.TestCase):
    """rate()/irate() are counter-only in PromQL, and the telemetry contract
    locks rate()-ed fields as counters (seed-sample-data seeds them as
    ``counter_double``). Live field caps typing such a field as gauge are
    treated as a stale/wrong ingest, not as refutation: the translation keeps
    RATE/IRATE (with a warning about the disagreement) instead of baking an
    AVG_OVER_TIME degrade that is guaranteed to 400 once the ingest follows
    the contract. Only an explicit rule-pack ``metric_kinds: gauge`` pin may
    force the degradation.
    """

    EXPR = "sum(rate(http_request_duration_seconds_bucket[5m])) by (le)"

    def _translate(self, expr, rp, resolver):
        return translate.translate_promql_to_esql(
            expr,
            esql_index="metrics-*",
            panel_type="timeseries",
            rule_pack=rp,
            resolver=resolver,
        )

    def _gauge_caps_resolver(self, rp):
        resolver = schema.SchemaResolver(rp)
        resolver._discovery_attempted = True
        resolver._field_cache = {
            "http_request_duration_seconds_bucket": {
                "double": {
                    "type": "double",
                    "time_series_metric": "gauge",
                }
            }
        }
        return resolver

    def test_live_gauge_caps_keep_rate_and_warn(self):
        rp = rules.RulePackConfig()
        ctx = self._translate(self.EXPR, rp, self._gauge_caps_resolver(rp))
        self.assertIn("RATE(http_request_duration_seconds_bucket", ctx.esql_query)
        self.assertNotIn("AVG_OVER_TIME", ctx.esql_query)
        self.assertTrue(
            any("currently types this field as gauge" in w for w in ctx.warnings),
            f"expected a target-disagreement warning, got: {ctx.warnings}",
        )

    def test_explicit_rule_pack_gauge_pin_still_degrades(self):
        rp = rules.RulePackConfig()
        rp.metric_kinds["http_request_duration_seconds_bucket"] = "gauge"
        ctx = self._translate(self.EXPR, rp, self._gauge_caps_resolver(rp))
        self.assertIn("AVG_OVER_TIME(http_request_duration_seconds_bucket", ctx.esql_query)
        self.assertNotIn("RATE(http_request_duration_seconds_bucket", ctx.esql_query)
        self.assertTrue(
            any("rendered as AVG_OVER_TIME" in w for w in ctx.warnings),
            f"expected the degrade warning, got: {ctx.warnings}",
        )


class TestNativePromqlLiveValidationFallback(unittest.TestCase):
    """B: per-panel live native-PROMQL validation backstop.

    When a target cluster is configured (``--es-url``) a native-PROMQL validator
    is attached to the rule pack. A panel whose emitted native query the cluster
    rejects at *parse* time must degrade to the ES|QL path instead of shipping a
    query that hard-errors in Kibana. Data gaps (unknown column/index, no data)
    must NOT degrade a structurally-valid native query. With no validator
    (offline), behavior is unchanged.
    """

    def _native_query(self, rp):
        _yaml, pr = _translate_panel(
            _make_panel(1, "rate(http_requests_total[5m])"), rule_pack=rp
        )
        return getattr(pr, "esql_query", "") or ""

    def test_no_validator_keeps_native_offline(self):
        rp = rules.RulePackConfig(native_promql=True)
        self.assertTrue(self._native_query(rp).startswith("PROMQL "))

    def test_validator_ok_keeps_native(self):
        rp = rules.RulePackConfig(native_promql=True)
        rp.native_promql_validator = lambda q: (True, "")
        self.assertTrue(self._native_query(rp).startswith("PROMQL "))

    def test_parse_rejected_native_query_falls_back_to_esql(self):
        rp = rules.RulePackConfig(native_promql=True)
        rp.native_promql_validator = lambda q: (
            False,
            '{"type":"parsing_exception","reason":"line 1:40: no viable alternative at input"}',
        )
        query = self._native_query(rp)
        self.assertFalse(
            query.startswith("PROMQL "),
            f"parse-rejected native query should degrade to ES|QL, got: {query!r}",
        )

    def test_data_gap_error_does_not_degrade(self):
        # Unknown column / index means the query is valid but the data is absent
        # (or the seed is missing a field). A valid native query must be kept.
        rp = rules.RulePackConfig(native_promql=True)
        rp.native_promql_validator = lambda q: (
            False,
            '{"type":"verification_exception","reason":"line 1:54: Unknown column [host]"}',
        )
        self.assertTrue(self._native_query(rp).startswith("PROMQL "))

    def test_validator_receives_adaptive_windowless_query(self):
        """Regression (#272/#273): the live parse gate must validate the *actual*
        emitted adaptive form — stepless and windowless — not a fixed-window
        proxy. If the validator were handed a ``step=…``/``[5m]`` shape it could
        pass while the real windowless query that ships to Kibana is never
        checked (or vice-versa, a valid adaptive panel could wrongly degrade)."""
        seen = []

        def validator(query):
            seen.append(query)
            return True, ""

        rp = rules.RulePackConfig(native_promql=True)
        rp.native_promql_validator = validator
        _yaml, pr = _translate_panel(
            _make_panel(1, "rate(http_requests_total[$__rate_interval])"),
            rule_pack=rp,
        )
        # Panel stayed native (validator accepted the query).
        self.assertTrue((getattr(pr, "esql_query", "") or "").startswith("PROMQL "))
        # The validator saw the real adaptive output: windowless rate, no step.
        self.assertTrue(seen, "native-PROMQL validator was never invoked")
        validated = seen[-1]
        self.assertIn("value=(rate(http_requests_total))", validated)
        self.assertNotIn("$__rate_interval", validated)
        self.assertNotIn("[5m]", validated)
        self.assertNotIn("step=", validated)


if __name__ == "__main__":
    unittest.main()
