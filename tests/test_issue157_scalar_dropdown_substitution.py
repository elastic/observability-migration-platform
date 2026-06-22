# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for issue #157 — numeric dropdowns in PromQL scalar slots.

Many Grafana dashboards put a dropdown at the top so the viewer can pick a
*number* — a percentile (``0.95``), a "top N" (``5``), a threshold. The panel
then references that dropdown in a PromQL **scalar** argument slot, e.g.
``histogram_quantile($quantile, …)``.

Before this fix the generic ``$var → label_var`` rewrite turned the dropdown
into a bare instant-vector selector in the scalar slot, so the PromQL parser
rejected it (``expected scalar, got vector``) and the panel silently degraded
to a "Migration Required" placeholder. The native PROMQL path was no better: it
blanked the variable to the literal ``1``, producing a wrong value.

The fix substitutes the dropdown's *selected numeric value* into the scalar
slot before any translation runs, so a parameterized panel migrates exactly as
well as the same panel with the value hardcoded — the issue's stated success
criterion ("the same panel with the percentile hardcoded migrates without this
error").
"""

from __future__ import annotations

import tempfile
import unittest

from observability_migration.adapters.source.grafana.panels import (
    _coerce_scalar_number,
    _dropdown_scalar_values,
    _substitute_scalar_dropdown_values,
    translate_dashboard,
)
from observability_migration.adapters.source.grafana.promql import (
    substitute_scalar_template_vars,
)
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.runtime_features import (
    PROMQL_COMMAND_V0,
    PROMQL_HISTOGRAM_QUANTILE,
    PROMQL_LABEL_MATCHER_PARAMS,
    set_runtime_feature,
)


class TestSubstituteScalarTemplateVars(unittest.TestCase):
    """Unit tests for the position-aware scalar substitution primitive."""

    VALUES = {
        "quantile": "0.95",
        "percentile": "95",
        "top_n": "5",
        "value": "0.95",
        "max": "5",
        "cpu_percentile": "0.9",
    }

    def _sub(self, expr):
        return substitute_scalar_template_vars(expr, self.VALUES)

    def test_histogram_quantile_first_arg(self):
        self.assertEqual(
            self._sub("histogram_quantile($quantile, sum by (le) (rate(k6_http[5m])))"),
            "histogram_quantile(0.95, sum by (le) (rate(k6_http[5m])))",
        )

    def test_topk_limit_arg(self):
        self.assertEqual(
            self._sub("topk($top_n, changes(process_start_time_seconds[1h])) > 0"),
            "topk(5, changes(process_start_time_seconds[1h])) > 0",
        )

    def test_vector_value(self):
        self.assertEqual(self._sub("vector($value)"), "vector(0.95)")

    def test_clamp_max_threshold(self):
        self.assertEqual(self._sub("clamp_max(up, $max)"), "clamp_max(up, 5)")

    def test_percentile_over_one_hundred_keeps_arithmetic(self):
        self.assertEqual(
            self._sub("histogram_quantile($percentile/100, sum(rate(x[1m])) by (le))"),
            "histogram_quantile(95/100, sum(rate(x[1m])) by (le))",
        )

    def test_quantile_over_time_first_arg(self):
        self.assertEqual(
            self._sub("quantile_over_time($cpu_percentile, foo[5m])"),
            "quantile_over_time(0.9, foo[5m])",
        )

    def test_braced_and_bracket_syntax(self):
        self.assertEqual(self._sub("histogram_quantile(${quantile}, x)"), "histogram_quantile(0.95, x)")
        self.assertEqual(self._sub("topk([[top_n]], y)"), "topk(5, y)")

    def test_nested_scalar_calls(self):
        self.assertEqual(
            self._sub("clamp_max(histogram_quantile($quantile, foo), $max)"),
            "clamp_max(histogram_quantile(0.95, foo), 5)",
        )

    def test_aggregate_leading_by_modifier(self):
        # PromQL allows the aggregation modifier *before* the argument list:
        # ``topk by (pod) (5, …)`` ≡ ``topk(5, …) by (pod)``. The scalar slot
        # still sits in the trailing argument list and must be substituted.
        self.assertEqual(
            self._sub("topk by (pod) ($top_n, rate(http_requests_total[5m]))"),
            "topk by (pod) (5, rate(http_requests_total[5m]))",
        )

    def test_aggregate_leading_without_modifier(self):
        self.assertEqual(
            self._sub("bottomk without (job) ($top_n, rate(http_requests_total[5m]))"),
            "bottomk without (job) (5, rate(http_requests_total[5m]))",
        )

    def test_quantile_leading_by_modifier(self):
        self.assertEqual(
            self._sub("quantile by (job) ($quantile, latency_seconds)"),
            "quantile by (job) (0.95, latency_seconds)",
        )

    def test_aggregate_trailing_modifier_still_substitutes(self):
        # The trailing-modifier form was already handled; keep it covered so the
        # leading-modifier support doesn't regress it.
        self.assertEqual(
            self._sub("topk($top_n, rate(http_requests_total[5m])) by (pod)"),
            "topk(5, rate(http_requests_total[5m])) by (pod)",
        )

    def test_empty_without_label_list_modifier(self):
        self.assertEqual(
            self._sub("bottomk without () ($top_n, foo)"),
            "bottomk without () (5, foo)",
        )

    def test_leading_modifier_does_not_confuse_similar_function(self):
        # ``quantile_over_time`` is a range function, not the ``quantile``
        # aggregate, so the agg-modifier branch must not swallow its prefix.
        self.assertEqual(
            self._sub("quantile_over_time($cpu_percentile, foo[5m])"),
            "quantile_over_time(0.9, foo[5m])",
        )

    def test_label_matcher_value_is_left_untouched(self):
        # A variable inside a label selector is NOT a scalar slot — leave it for
        # the label-matcher / control machinery.
        expr = 'sum(rate(foo{code="$quantile"}[5m]))'
        self.assertEqual(self._sub(expr), expr)

    def test_grouping_clause_is_left_untouched(self):
        expr = "sum by ($top_n) (foo)"
        self.assertEqual(self._sub(expr), expr)

    def test_unknown_variable_is_left_untouched(self):
        expr = "histogram_quantile($unknown, foo)"
        self.assertEqual(self._sub(expr), expr)

    def test_non_scalar_position_is_left_untouched(self):
        # ``$quantile`` here is a metric selector, not a scalar arg.
        expr = "sum(rate($quantile[5m]))"
        self.assertEqual(self._sub(expr), expr)

    def test_empty_values_is_noop(self):
        expr = "histogram_quantile($quantile, foo)"
        self.assertEqual(substitute_scalar_template_vars(expr, {}), expr)


class TestDropdownScalarValueResolution(unittest.TestCase):
    """How a dropdown's *selected number* is extracted from templating."""

    def test_coerce_accepts_numbers_and_numeric_strings(self):
        self.assertEqual(_coerce_scalar_number("0.95"), "0.95")
        self.assertEqual(_coerce_scalar_number("95"), "95")
        self.assertEqual(_coerce_scalar_number(5), "5")
        self.assertEqual(_coerce_scalar_number(["0.99", "0.95"]), "0.99")

    def test_coerce_rejects_non_numbers(self):
        self.assertIsNone(_coerce_scalar_number("instance"))
        self.assertIsNone(_coerce_scalar_number("All"))
        self.assertIsNone(_coerce_scalar_number(""))
        self.assertIsNone(_coerce_scalar_number(True))
        self.assertIsNone(_coerce_scalar_number(None))

    def test_prefers_current_selection(self):
        variables = [
            {
                "name": "quantile",
                "current": {"text": "95th", "value": "0.95"},
                "options": [{"value": "0.5"}, {"value": "0.95"}, {"value": "0.99"}],
            }
        ]
        self.assertEqual(_dropdown_scalar_values(variables), {"quantile": "0.95"})

    def test_sole_numeric_option_fallback(self):
        variables = [{"name": "top_n", "options": [{"value": "5"}]}]
        self.assertEqual(_dropdown_scalar_values(variables), {"top_n": "5"})

    def test_label_name_variable_excluded(self):
        # A grouping/label variable resolves to a label name, not a number, so
        # it must never enter the scalar-substitution map.
        variables = [{"name": "instance", "current": {"value": "host-a"}}]
        self.assertEqual(_dropdown_scalar_values(variables), {})


class TestDashboardPrePass(unittest.TestCase):
    """The dashboard pre-pass rewrites only numeric scalar-slot dropdowns."""

    def test_pre_pass_rewrites_target_expr_in_place(self):
        dashboard = {
            "templating": {"list": [{"name": "quantile", "current": {"value": "0.95"}}]},
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "targets": [{"refId": "A", "expr": "histogram_quantile($quantile, foo)"}],
                }
            ],
        }
        _substitute_scalar_dropdown_values(dashboard)
        self.assertEqual(
            dashboard["panels"][0]["targets"][0]["expr"],
            "histogram_quantile(0.95, foo)",
        )

    def test_pre_pass_noop_without_numeric_dropdowns(self):
        dashboard = {
            "templating": {"list": [{"name": "instance", "current": {"value": "host-a"}}]},
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "targets": [{"refId": "A", "expr": 'rate(foo{instance="$instance"}[5m])'}],
                }
            ],
        }
        _substitute_scalar_dropdown_values(dashboard)
        self.assertEqual(
            dashboard["panels"][0]["targets"][0]["expr"],
            'rate(foo{instance="$instance"}[5m])',
        )


def _native_rule_pack():
    """A rule pack that mirrors a native-PROMQL-capable target (post-#158)."""
    rule_pack = RulePackConfig()
    rule_pack.native_promql = True
    for feature in (PROMQL_COMMAND_V0, PROMQL_HISTOGRAM_QUANTILE, PROMQL_LABEL_MATCHER_PARAMS):
        set_runtime_feature(rule_pack, feature, supported=True, source="test", confidence="verified")
    return rule_pack


class TestDropdownMatchesHardcodedEndToEnd(unittest.TestCase):
    """A scalar dropdown must migrate exactly as well as the hardcoded value."""

    def _status(self, expr, templating):
        dashboard = {
            "title": "t",
            "uid": "u",
            "templating": {"list": templating},
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "title": "p",
                    "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                    "targets": [{"refId": "A", "expr": expr}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as out_dir:
            out = translate_dashboard(dashboard, output_dir=out_dir, rule_pack=_native_rule_pack())
        result = out[0] if isinstance(out, tuple) else out
        panel = next(pr for pr in result.panel_results if pr.grafana_type == "timeseries")
        return panel.status, list(panel.reasons or [])

    def _assert_parity(self, hardcoded, dropdown, templating):
        hard_status, _ = self._status(hardcoded, [])
        drop_status, _ = self._status(dropdown, templating)
        self.assertEqual(
            drop_status,
            hard_status,
            f"dropdown form {dropdown!r} ({drop_status}) should match "
            f"hardcoded form {hardcoded!r} ({hard_status})",
        )
        # And neither should be the silent dropdown-driven failure.
        self.assertNotEqual(drop_status, "not_feasible")

    def test_histogram_quantile_percentile(self):
        self._assert_parity(
            "histogram_quantile(0.95, sum by (le)(rate(k6_http_req_duration_seconds[5m])))",
            "histogram_quantile($quantile, sum by (le)(rate(k6_http_req_duration_seconds[5m])))",
            [{"name": "quantile", "current": {"value": "0.95"}}],
        )

    def test_histogram_quantile_percentile_over_hundred(self):
        self._assert_parity(
            "histogram_quantile(95/100, sum(rate(request_duration_ms_bucket[1m])) by (le))",
            "histogram_quantile($percentile/100, sum(rate(request_duration_ms_bucket[1m])) by (le))",
            [{"name": "percentile", "current": {"value": "95"}}],
        )

    def test_vector_value(self):
        self._assert_parity(
            "vector(0.95)",
            "vector($value)",
            [{"name": "value", "current": {"value": "0.95"}}],
        )

    def test_topk_top_n(self):
        self._assert_parity(
            "topk(5, sum by (pod)(rate(http_requests_total[5m])))",
            "topk($top_n, sum by (pod)(rate(http_requests_total[5m])))",
            [{"name": "top_n", "current": {"value": "5"}}],
        )

    def test_clamp_max_threshold(self):
        self._assert_parity(
            "clamp_max(up, 5)",
            "clamp_max(up, $max)",
            [{"name": "max", "current": {"value": "5"}}],
        )

    def test_topk_top_n_leading_modifier(self):
        # The leading ``by (…)`` aggregation form must migrate as well as the
        # hardcoded value, just like the trailing form.
        self._assert_parity(
            "topk by (pod) (5, rate(http_requests_total[5m]))",
            "topk by (pod) ($top_n, rate(http_requests_total[5m]))",
            [{"name": "top_n", "current": {"value": "5"}}],
        )


if __name__ == "__main__":
    unittest.main()
