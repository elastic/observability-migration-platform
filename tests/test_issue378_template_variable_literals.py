# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for issue #378 — literal-valued Grafana template variables in PromQL.

A ``textbox`` (free text) or ``constant`` (fixed, hidden) template variable is a
*value the dashboard author typed*, not a selector over series, so Grafana
interpolates it into the PromQL string before Prometheus ever sees the query.

Before this fix the generic ``$var → label_var`` fallback turned such a variable
into a bare PromQL metric selector. In a binary operand
(``... >= ($threshold / 100)``) that selector is parsed as a genuine metric and
survives into the emitted ES|QL as the column ``metrics.label_threshold``, which
can never exist: Elasticsearch rejects the query with ``Unknown column`` and
Kibana renders a red error tile. ``--validate`` then excused the failure as
not-yet-ingested telemetry and shipped the panel as ``migrated_with_warnings``.

Three behaviors are covered here:

1. Literal-valued variables are inlined before any translation path runs, so
   the panel migrates exactly as well as the same panel with the value
   hardcoded.
2. A template variable that still reaches the emitted ES|QL as a ``label_<var>``
   column degrades to ``not_feasible`` (an honest "Manual review required"
   placeholder) instead of shipping a query that reads a phantom column.
3. The live-validation self-heal excuse is evidence-based: an ``Unknown column``
   whose name derives from a source template variable is a translation error,
   never a data-readiness gap.
"""

from __future__ import annotations

import re
import unittest
import unittest.mock

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana import promql as promql_module
from observability_migration.adapters.source.grafana.panels import (
    _INLINED_LITERAL_KEY,
    _INLINED_LITERAL_PARTIAL_KEY,
    _literal_variable_values,
    _substitute_literal_variable_values,
)
from observability_migration.adapters.source.grafana.promql import (
    promql_literal_value,
    substitute_literal_template_vars,
    template_vars_in_label_selectors,
)
from observability_migration.adapters.source.grafana.translate import (
    _template_variable_placeholder_columns,
)
from observability_migration.core.reporting.report import PanelResult
from observability_migration.core.verification.disposition import (
    unknown_column_is_source_template_variable,
    validation_failure_self_heals,
)

# The issue's reproduction: grafana.com dashboard 11454, panel 39
# "Running PVCs Above % Used Warning Threshold Stats - Current".
PVC_EXPR_TEMPLATE = (
    "(max by (persistentvolumeclaim,namespace) (kubelet_volume_stats_used_bytes )) "
    "/ (max by (persistentvolumeclaim,namespace) (kubelet_volume_stats_capacity_bytes )) "
    ">= ({threshold} / 100)"
)
PVC_THRESHOLD_VAR = "pvc_percent_used_warning_threshold"


def _threshold_templating() -> list[dict]:
    """A fresh templating list per call.

    The pre-pass tags the variable dict it inlined, so a shared literal would
    leak that marker between tests.
    """
    return [
        {
            "type": "textbox",
            "name": PVC_THRESHOLD_VAR,
            "label": "PVC % Used Warning Threshold",
            "query": "80",
            "current": {"text": "80", "value": "80"},
        }
    ]


def _dashboard(expr: str, templating: list[dict], panel_type: str = "timeseries") -> dict:
    return {
        "uid": "u-378",
        "title": "issue 378",
        "templating": {"list": templating},
        "panels": [
            {
                "id": 39,
                "type": panel_type,
                "title": "p",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                "targets": [
                    {"refId": "A", "expr": expr, "datasource": {"type": "prometheus"}}
                ],
            }
        ],
    }


def _translate(expr: str, templating: list[dict], panel_type: str = "timeseries"):
    """Migrate a one-panel dashboard the way a real run does (with a resolver)."""
    rule_pack = rules.RulePackConfig()
    result = panels.translate_dashboard(
        _dashboard(expr, templating, panel_type),
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=schema.SchemaResolver(rule_pack),
    )
    return result.panel_results[0], result


class TestPromqlLiteralValue(unittest.TestCase):
    """Which template-variable values are safe to splice into PromQL."""

    def test_accepts_numbers(self):
        self.assertEqual(promql_literal_value("80"), "80")
        self.assertEqual(promql_literal_value("0.95"), "0.95")
        self.assertEqual(promql_literal_value(" 1e3 "), "1e3")
        self.assertEqual(promql_literal_value("-1.5"), "-1.5")
        self.assertEqual(promql_literal_value(1024), "1024")

    def test_accepts_durations(self):
        self.assertEqual(promql_literal_value("5m"), "5m")
        self.assertEqual(promql_literal_value("1h30m"), "1h30m")
        self.assertEqual(promql_literal_value("500ms"), "500ms")
        self.assertEqual(promql_literal_value("1y2w3d"), "1y2w3d")

    def test_rejects_durations_prometheus_cannot_parse(self):
        # Compound units must descend, so these are duration-*shaped* but would
        # emit a query that is broken in a new way.
        for value in ("1m1h", "30m1h", "1s1s", "5m5m"):
            with self.subTest(value=value):
                self.assertIsNone(promql_literal_value(value))

    def test_accepts_metric_and_recording_rule_names(self):
        self.assertEqual(
            promql_literal_value("node_cpu_seconds_total"), "node_cpu_seconds_total"
        )
        self.assertEqual(promql_literal_value("job:rate:sum"), "job:rate:sum")

    def test_rejects_values_that_need_a_promql_parse_to_place(self):
        for value in (
            ".*",  # regex fragment
            "prod|staging",  # regex alternation
            "a,b",  # comma list (multi-select snapshot)
            "sum(rate(foo[5m]))",  # whole sub-expression
            '"quoted"',
            "kube-system",  # hyphen is subtraction in PromQL
            "5 m",
            "",
            "   ",
        ):
            with self.subTest(value=value):
                self.assertIsNone(promql_literal_value(value))

    def test_rejects_promql_keywords(self):
        # Identifier-shaped, but inlining one would rewrite query structure.
        for value in (
            "and",
            "or",
            "unless",
            "atan2",
            "by",
            "without",
            "on",
            "ignoring",
            "group_left",
            "offset",
            "bool",
            "inf",
            "nan",
        ):
            with self.subTest(value=value):
                self.assertIsNone(promql_literal_value(value))

    def test_rejects_non_scalar_types(self):
        self.assertIsNone(promql_literal_value(None))
        self.assertIsNone(promql_literal_value(True))
        self.assertIsNone(promql_literal_value(["80"]))
        self.assertIsNone(promql_literal_value({"value": "80"}))


class TestSubstituteLiteralTemplateVars(unittest.TestCase):
    """Unit tests for the position-aware literal substitution primitive."""

    VALUES = {"threshold": "80", "window": "10m", "suffix": "seconds_total"}

    def _sub(self, expr):
        return substitute_literal_template_vars(expr, self.VALUES)

    def test_plain_token_in_arithmetic(self):
        self.assertEqual(self._sub("foo >= ($threshold / 100)"), "foo >= (80 / 100)")

    def test_braced_token(self):
        self.assertEqual(self._sub("foo * ${threshold}"), "foo * 80")

    def test_bracket_token(self):
        self.assertEqual(self._sub("foo * [[threshold]]"), "foo * 80")

    def test_value_preserving_format_modifier(self):
        self.assertEqual(self._sub("foo * ${threshold:raw}"), "foo * 80")
        self.assertEqual(self._sub("foo * ${threshold:text}"), "foo * 80")

    def test_quoting_format_modifier_is_left_untouched(self):
        # ``:json`` / ``:singlequote`` add their own quoting; inlining the bare
        # value would silently drop it.
        for fmt in ("json", "singlequote", "doublequote", "percentencode", "csv"):
            with self.subTest(fmt=fmt):
                expr = f"foo * ${{threshold:{fmt}}}"
                self.assertEqual(self._sub(expr), expr)

    def test_range_window(self):
        self.assertEqual(
            self._sub("rate(http_requests_total[$window])"),
            "rate(http_requests_total[10m])",
        )

    def test_glued_suffix_completes_the_metric_name(self):
        self.assertEqual(
            self._sub("node_cpu_${suffix}"), "node_cpu_seconds_total"
        )

    def test_label_selector_is_left_untouched(self):
        # A variable-driven label matcher becomes a bound ES|QL ``?var``
        # parameter with a Kibana control; freezing it here would lose that.
        expr = 'sum(rate(foo{job="$threshold", env=~"$window"}[5m]))'
        self.assertEqual(self._sub(expr), expr)

    def test_selector_is_skipped_but_the_rest_is_not(self):
        self.assertEqual(
            self._sub('foo{job="$threshold"} * $threshold'),
            'foo{job="$threshold"} * 80',
        )

    def test_string_literal_is_left_untouched(self):
        expr = 'label_replace(foo, "dst", "$threshold", "src", "(.*)")'
        self.assertEqual(self._sub(expr), expr)

    def test_single_quoted_string_literal_is_left_untouched(self):
        expr = "label_replace(foo, 'dst', '$threshold', 'src', '(.*)')"
        self.assertEqual(self._sub(expr), expr)

    def test_backtick_raw_string_is_left_untouched(self):
        # PromQL backticks are raw strings; they must be skipped like the other
        # two quote styles rather than substituted inconsistently.
        expr = "label_replace(foo, `dst`, `$threshold`, `src`, `(.*)`)"
        self.assertEqual(self._sub(expr), expr)

    def test_backtick_matcher_value_is_left_untouched(self):
        expr = "sum(rate(foo{job=`$threshold`}[5m]))"
        self.assertEqual(self._sub(expr), expr)

    def test_escaped_quote_does_not_end_the_string_early(self):
        expr = r'label_replace(foo, "d\"st", "$threshold", "src", "(.*)")'
        self.assertEqual(self._sub(expr), expr)

    def test_backslash_in_a_backtick_string_is_literal(self):
        # A raw string does not process escapes, so the trailing backtick still
        # closes it and the following scalar slot is still inlined.
        self.assertEqual(
            self._sub(r"label_replace(foo, `d\`, `s`, `x`, `(.*)`) * $threshold"),
            r"label_replace(foo, `d\`, `s`, `x`, `(.*)`) * 80",
        )

    def test_unknown_variable_is_left_untouched(self):
        expr = "foo >= $unknown"
        self.assertEqual(self._sub(expr), expr)

    def test_grafana_macros_are_left_untouched(self):
        expr = "rate(foo[$__rate_interval]) * $__range_s"
        self.assertEqual(self._sub(expr), expr)

    def test_empty_values_is_noop(self):
        expr = "foo >= $threshold"
        self.assertEqual(substitute_literal_template_vars(expr, {}), expr)

    def test_unterminated_selector_does_not_swallow_the_rest(self):
        # Malformed input must not silently drop later substitutions.
        self.assertEqual(self._sub("foo{job= * $threshold"), "foo{job= * 80")


class TestTemplateVarsInLabelSelectors(unittest.TestCase):
    """Which positions really keep a variable interactive after migration."""

    def test_quoted_matcher_values_are_reported(self):
        self.assertEqual(
            template_vars_in_label_selectors('sum(rate(x{a="$a", b=~"$b"}[5m])) > $t'),
            {"a", "b"},
        )

    def test_variable_outside_a_selector_is_not_a_matcher(self):
        self.assertEqual(template_vars_in_label_selectors("foo > $t"), set())
        self.assertEqual(
            template_vars_in_label_selectors('label_replace(foo, "d", "$t", "s", "(.*)")'),
            set(),
        )

    def test_unquoted_matcher_value_is_not_reported(self):
        # PromQL requires a quoted matcher value, and every matcher regex in the
        # pipeline demands one, so an unquoted ``$job`` becomes no control.
        self.assertEqual(template_vars_in_label_selectors("up{job=$job} > $t"), set())

    def test_backtick_matcher_value_is_not_reported(self):
        # The matcher regexes across the pipeline accept only ``"`` and ``'``, so
        # a backtick value never becomes a bound ``?var`` with a control; the
        # caller must not claim it stayed interactive.
        self.assertEqual(template_vars_in_label_selectors("up{job=`$job`}"), set())
        self.assertEqual(
            template_vars_in_label_selectors('up{job=`$job`, env="$env"}'), {"env"}
        )

    def test_braced_and_bracket_tokens(self):
        self.assertEqual(
            template_vars_in_label_selectors('up{a="${a:raw}", b="[[b]]"}'), {"a", "b"}
        )

    def test_unterminated_selector_does_not_hang_or_over_report(self):
        self.assertEqual(template_vars_in_label_selectors("up{"), set())
        self.assertEqual(template_vars_in_label_selectors('up{job="$job"'), set())

    def test_partial_matcher_value_is_not_reported(self):
        # Parameterization needs the whole value to be one variable, so a value
        # the variable only contributes part of stays a literal string and no
        # control is created for it.
        self.assertEqual(template_vars_in_label_selectors('up{job="shard-$n"}'), set())
        self.assertEqual(template_vars_in_label_selectors('up{job=~"$job.*"}'), set())

    def test_builtin_variable_is_not_a_user_control(self):
        self.assertEqual(template_vars_in_label_selectors('up{job="$__range"}'), set())

    def test_non_string_input(self):
        self.assertEqual(template_vars_in_label_selectors(None), set())

    def test_agrees_with_the_rewrite_that_creates_the_parameter(self):
        # The claim this helper backs ("still interactive as a Kibana control")
        # is only true when the matcher rewrite really binds a parameter, so the
        # two must agree exactly — assert that against the rewrite itself rather
        # than against hand-written expectations.
        param_value = re.compile(
            re.escape(promql_module._GRAFANA_PARAM_VALUE_PREFIX) + r"([A-Za-z_][A-Za-z0-9_]*)"
        )
        for expr in (
            'up{job="$job"}',
            "up{job='$job'}",
            "up{job=$job}",
            "up{job=`$job`}",
            'up{job=`$j`, env="$e"}',
            'up{job="{$job}"}',
            'up{job="shard-$n"}',
            'up{job=~"$job.*"}',
            'up{job=~"^$job$"}',
            'up{job!~"$job"}',
            'up{job="${job:raw}"}',
            'up{job="[[job]]"}',
            'up{job="$__range"}',
            'rate(x{ns="$ns"}[$__rate_interval])',
            'label_replace(f, "d", "$t", "s", ".*")',
            'sum(a{ns="$ns"}) / sum(b{ns="$ns", pod="$pod"})',
            'up{job="a,b", env="$e"}',
            'up{job="}", env="$e"}',
            "up{job=`}`, env=\"$e\"}",
            'up{ job = "$job" }',
            "up{",
            'up{job="unterminated',
            'up{job="$job"} + label_replace(up{env="$env"}, "d", "$1", "s", "(.*)")',
        ):
            with self.subTest(expr=expr):
                bound = set(
                    param_value.findall(
                        promql_module._parameterize_grafana_label_matchers(expr)
                    )
                )
                self.assertEqual(template_vars_in_label_selectors(expr), bound)


class TestLiteralVariableValueResolution(unittest.TestCase):
    """Which templating entries contribute a literal, and from which field."""

    def test_textbox_prefers_current_selection(self):
        variables = [
            {
                "type": "textbox",
                "name": "threshold",
                "query": "80",
                "current": {"text": "90", "value": "90"},
            }
        ]
        self.assertEqual(_literal_variable_values(variables), {"threshold": "90"})

    def test_textbox_falls_back_to_declared_default(self):
        variables = [{"type": "textbox", "name": "threshold", "query": "80"}]
        self.assertEqual(_literal_variable_values(variables), {"threshold": "80"})

    def test_empty_current_falls_back_to_declared_default(self):
        variables = [
            {
                "type": "textbox",
                "name": "threshold",
                "query": "80",
                "current": {"text": "", "value": ""},
            }
        ]
        self.assertEqual(_literal_variable_values(variables), {"threshold": "80"})

    def test_unsafe_current_is_dropped_rather_than_replaced_by_the_default(self):
        # The dashboard renders with ``1 or vector(1)``, so inlining the ``80``
        # default would compute the panel against a number nobody selected.
        # Dropping it leaves ``$threshold`` for the phantom-column guard.
        variables = [
            {
                "type": "textbox",
                "name": "threshold",
                "query": "80",
                "current": {"text": "1 or vector(1)", "value": "1 or vector(1)"},
            }
        ]
        self.assertEqual(_literal_variable_values(variables), {})

    def test_current_text_is_used_when_value_is_absent(self):
        variables = [
            {"type": "textbox", "name": "threshold", "query": "80", "current": {"text": "90"}}
        ]
        self.assertEqual(_literal_variable_values(variables), {"threshold": "90"})

    def test_constant_uses_its_query_value(self):
        variables = [
            {"type": "constant", "name": "divisor", "query": "1024", "hide": 2}
        ]
        self.assertEqual(_literal_variable_values(variables), {"divisor": "1024"})

    def test_other_variable_types_are_excluded(self):
        # ``query`` / ``custom`` / ``interval`` / ``datasource`` variables select
        # over real series or are handled by their own translators.
        variables = [
            {"type": "query", "name": "instance", "current": {"value": "host-a"}},
            {"type": "custom", "name": "top_n", "current": {"value": "5"}},
            {"type": "interval", "name": "step", "current": {"value": "5m"}},
            {"type": "datasource", "name": "ds", "current": {"value": "prom"}},
            {"name": "untyped", "current": {"value": "7"}},
        ]
        self.assertEqual(_literal_variable_values(variables), {})

    def test_non_literal_textbox_value_is_excluded(self):
        variables = [
            {"type": "textbox", "name": "filter", "current": {"value": ".*"}, "query": ".*"}
        ]
        self.assertEqual(_literal_variable_values(variables), {})


class TestDashboardPrePass(unittest.TestCase):
    """The pre-pass rewrites target expressions and tags what it inlined."""

    def test_rewrites_target_expr_in_place_and_tags_the_variable(self):
        variable = {
            "type": "textbox",
            "name": PVC_THRESHOLD_VAR,
            "query": "80",
            "current": {"text": "80", "value": "80"},
        }
        dashboard = _dashboard(
            PVC_EXPR_TEMPLATE.format(threshold=f"${PVC_THRESHOLD_VAR}"), [variable]
        )
        _substitute_literal_variable_values(dashboard)
        self.assertEqual(
            dashboard["panels"][0]["targets"][0]["expr"],
            PVC_EXPR_TEMPLATE.format(threshold="80"),
        )
        self.assertEqual(variable[_INLINED_LITERAL_KEY], "80")

    def test_noop_without_literal_valued_variables(self):
        expr = 'rate(foo{instance="$instance"}[5m])'
        variable = {"type": "query", "name": "instance", "current": {"value": "host-a"}}
        dashboard = _dashboard(expr, [variable])
        _substitute_literal_variable_values(dashboard)
        self.assertEqual(dashboard["panels"][0]["targets"][0]["expr"], expr)
        self.assertNotIn(_INLINED_LITERAL_KEY, variable)

    def test_matcher_only_variable_is_not_tagged_as_inlined(self):
        # Nothing was frozen, so the report must keep the plain "no Kibana
        # control equivalent" wording rather than claim a literal was used.
        expr = 'sum(rate(http_requests_total{namespace="$ns"}[5m]))'
        variable = {"type": "textbox", "name": "ns", "query": "prod"}
        dashboard = _dashboard(expr, [variable])
        _substitute_literal_variable_values(dashboard)
        self.assertEqual(dashboard["panels"][0]["targets"][0]["expr"], expr)
        self.assertNotIn(_INLINED_LITERAL_KEY, variable)

    def test_fully_inlined_variable_is_not_tagged_partial(self):
        variable = {"type": "textbox", "name": "t", "current": {"value": "80"}}
        dashboard = _dashboard("foo > $t", [variable])
        _substitute_literal_variable_values(dashboard)
        self.assertEqual(variable[_INLINED_LITERAL_KEY], "80")
        self.assertNotIn(_INLINED_LITERAL_PARTIAL_KEY, variable)

    def test_variable_used_as_both_matcher_and_scalar_is_tagged_partial(self):
        # Grafana drove both from one input; after migration the matcher keeps an
        # interactive control while the scalar is frozen, so the split must be
        # recorded for the warning to be honest.
        variable = {"type": "textbox", "name": "t", "current": {"value": "80"}}
        dashboard = _dashboard('foo{t="$t"} > $t', [variable])
        _substitute_literal_variable_values(dashboard)
        self.assertEqual(dashboard["panels"][0]["targets"][0]["expr"], 'foo{t="$t"} > 80')
        self.assertEqual(variable[_INLINED_LITERAL_KEY], "80")
        self.assertTrue(variable[_INLINED_LITERAL_PARTIAL_KEY])

    def test_matcher_in_another_panel_still_tags_partial(self):
        # The frozen value and the surviving matcher can live in different
        # panels; the tag is per variable, so the split must be seen across the
        # whole dashboard, not only within one target.
        variable = {"type": "textbox", "name": "t", "current": {"value": "80"}}
        dashboard = _dashboard("foo > $t", [variable])
        dashboard["panels"].append(
            {
                "id": 40,
                "type": "timeseries",
                "title": "p2",
                "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
                "targets": [{"refId": "A", "expr": 'bar{t="$t"}'}],
            }
        )
        _substitute_literal_variable_values(dashboard)
        self.assertEqual(variable[_INLINED_LITERAL_KEY], "80")
        self.assertTrue(variable[_INLINED_LITERAL_PARTIAL_KEY])

    def test_unquoted_matcher_is_not_mistaken_for_a_surviving_matcher(self):
        # Nothing parameterizes an unquoted matcher value, so claiming the
        # variable stayed interactive there would be false.
        variable = {"type": "textbox", "name": "t", "current": {"value": "80"}}
        dashboard = _dashboard("foo{t=$t} > $t", [variable])
        _substitute_literal_variable_values(dashboard)
        self.assertEqual(variable[_INLINED_LITERAL_KEY], "80")
        self.assertNotIn(_INLINED_LITERAL_PARTIAL_KEY, variable)

    def test_string_argument_is_not_mistaken_for_a_surviving_matcher(self):
        # ``label_replace``'s replacement is a string argument, not a filter, so
        # nothing there becomes a Kibana control and the warning must not say so.
        variable = {"type": "textbox", "name": "t", "current": {"value": "80"}}
        dashboard = _dashboard('label_replace(foo, "d", "$t", "s", "(.*)") > $t', [variable])
        _substitute_literal_variable_values(dashboard)
        self.assertEqual(variable[_INLINED_LITERAL_KEY], "80")
        self.assertNotIn(_INLINED_LITERAL_PARTIAL_KEY, variable)


class TestIssuePanelMigratesLikeTheHardcodedForm(unittest.TestCase):
    """The reported panel must migrate as well as the same panel with 80 inline."""

    def test_threshold_is_inlined_not_read_as_a_metric_column(self):
        panel, _result = _translate(
            PVC_EXPR_TEMPLATE.format(threshold=f"${PVC_THRESHOLD_VAR}"),
            _threshold_templating(),
            panel_type="table",
        )
        query = panel.esql_query or ""
        self.assertIn("(80 / 100)", query)
        self.assertNotIn(f"label_{PVC_THRESHOLD_VAR}", query)
        self.assertNotEqual(panel.status, "not_feasible")

    def test_matches_the_hardcoded_form(self):
        templated, _ = _translate(
            PVC_EXPR_TEMPLATE.format(threshold=f"${PVC_THRESHOLD_VAR}"),
            _threshold_templating(),
            panel_type="table",
        )
        hardcoded, _ = _translate(
            PVC_EXPR_TEMPLATE.format(threshold="80"), [], panel_type="table"
        )
        self.assertEqual(templated.status, hardcoded.status)
        self.assertEqual(templated.esql_query, hardcoded.esql_query)

    def test_control_warning_discloses_the_frozen_value(self):
        _panel, result = _translate(
            PVC_EXPR_TEMPLATE.format(threshold=f"${PVC_THRESHOLD_VAR}"),
            _threshold_templating(),
            panel_type="table",
        )
        joined = " ".join(result.control_warnings)
        self.assertIn(PVC_THRESHOLD_VAR, joined)
        self.assertIn("80", joined)
        self.assertIn("inlined", joined)


class TestLiteralVariablesEndToEnd(unittest.TestCase):
    """Other literal-valued shapes the same pre-pass now handles."""

    def test_constant_divisor_is_inlined(self):
        panel, _ = _translate(
            "node_memory_MemFree_bytes / $divisor",
            [{"type": "constant", "name": "divisor", "query": "1024", "hide": 2}],
        )
        self.assertIn("1024", panel.esql_query or "")
        self.assertNotIn("label_divisor", panel.esql_query or "")

    def test_textbox_duration_becomes_the_rate_window(self):
        panel, _ = _translate(
            "rate(http_requests_total[$window])",
            [{"type": "textbox", "name": "window", "query": "10m"}],
        )
        self.assertNotEqual(panel.status, "not_feasible")
        self.assertNotIn("label_window", panel.esql_query or "")

    def test_textbox_metric_name_is_inlined(self):
        # Previously blocked by ``dynamic_metric_name_rule``; the author's typed
        # metric name is known, so Grafana's own interpolation applies.
        panel, _ = _translate(
            "sum($metric)",
            [{"type": "textbox", "name": "metric", "query": "up"}],
        )
        self.assertNotEqual(panel.status, "not_feasible")
        self.assertIn("up", panel.esql_query or "")

    def test_textbox_label_matcher_still_binds_an_interactive_control(self):
        # Regression guard: the ``?var`` + Kibana control path is strictly
        # better than a frozen literal, so the pre-pass must not touch it.
        panel, result = _translate(
            'sum(rate(http_requests_total{namespace="$ns"}[5m]))',
            [{"type": "textbox", "name": "ns", "query": "prod"}],
        )
        self.assertIn("?ns", panel.esql_query or "")
        controls = (result.dashboard_ir.to_yaml_dict() or {}).get("controls") or []
        self.assertTrue(
            any(control.get("variable_name") == "ns" for control in controls), controls
        )
        joined = " ".join(result.control_warnings)
        self.assertIn("no direct Kibana control equivalent", joined)
        self.assertNotIn("inlined", joined)

    def test_mixed_matcher_and_scalar_use_discloses_the_split(self):
        # One Grafana input drove both the filter and the threshold; only the
        # filter stays interactive, so the warning must not claim the value is
        # simply frozen everywhere.
        _panel, result = _translate(
            'sum(rate(http_requests_total{code="$t"}[5m])) > $t',
            [{"type": "textbox", "name": "t", "current": {"value": "80"}}],
        )
        joined = " ".join(result.control_warnings)
        self.assertIn("inlined", joined)
        self.assertIn("stayed interactive", joined)


class TestPlaceholderColumnDetection(unittest.TestCase):
    """``label_<var>`` columns are only blamed on real template variables."""

    def test_detects_profile_prefixed_placeholder(self):
        self.assertEqual(
            _template_variable_placeholder_columns(
                "foo >= ($threshold / 100)",
                "TS metrics-*\n| EVAL v = (foo / metrics.label_threshold)",
            ),
            ["threshold"],
        )

    def test_detects_bare_placeholder(self):
        self.assertEqual(
            _template_variable_placeholder_columns(
                "foo >= ${threshold}", "TS m\n| EVAL v = label_threshold"
            ),
            ["threshold"],
        )

    def test_ignores_a_target_metric_genuinely_named_label_something(self):
        # kube-state-metrics really does expose ``label_*`` series; with no
        # ``$label_topology`` in the source there is nothing to blame.
        self.assertEqual(
            _template_variable_placeholder_columns(
                "sum(label_topology)", "TS m\n| STATS v = SUM(metrics.label_topology)"
            ),
            [],
        )

    def test_ignores_grafana_builtin_macros(self):
        self.assertEqual(
            _template_variable_placeholder_columns(
                "rate(foo[$__rate_interval])", "TS m\n| STATS v = RATE(label___rate_interval)"
            ),
            [],
        )

    def test_does_not_match_a_longer_column_name_by_prefix(self):
        self.assertEqual(
            _template_variable_placeholder_columns(
                "foo * $pvc", "TS m\n| EVAL v = metrics.label_pvc_percent_used"
            ),
            [],
        )

    def test_variable_bound_as_an_esql_parameter_is_not_flagged(self):
        self.assertEqual(
            _template_variable_placeholder_columns(
                'rate(foo{ns="$ns"}[5m])', "TS m\n| WHERE labels.ns == ?ns"
            ),
            [],
        )

    def test_real_label_metric_beside_a_same_named_matcher_variable_is_not_flagged(self):
        # ``label_threshold`` is a genuine kube-state-metrics series and
        # ``$threshold`` only filters a label, so macro preprocessing created no
        # placeholder. Blaming the variable here would manualize a good panel.
        self.assertEqual(
            _template_variable_placeholder_columns(
                'foo + label_threshold{job="$threshold"}',
                "TS m\n| STATS v = MAX(metrics.label_threshold) BY labels.job",
            ),
            [],
        )

    def test_real_label_metric_plus_a_scalar_use_of_the_variable_is_flagged(self):
        # Both at once: preprocessing adds a *second* ``label_threshold``, so the
        # emitted column count no longer matches the source and the scalar use is
        # still a phantom.
        self.assertEqual(
            _template_variable_placeholder_columns(
                "label_threshold + $threshold",
                "TS m\n| EVAL v = (metrics.label_threshold + metrics.label_threshold)",
            ),
            ["threshold"],
        )

    def test_variable_literally_named_var_is_flagged(self):
        # ``var`` is a legal Grafana variable name and must not be mistaken for
        # the "name could not be determined" fallback.
        self.assertEqual(
            _template_variable_placeholder_columns(
                "foo >= ($var / 100)",
                "TS m\n| EVAL v = (foo >= (metrics.label_var / 100))",
            ),
            ["var"],
        )

    def test_recording_rule_column_sharing_the_placeholder_prefix_is_not_flagged(self):
        # The variable did become a placeholder in the cleaned PromQL, but the
        # emitted query only reads ``label_threshold:rate5m`` — a different
        # field, so there is no phantom column to blame it for.
        self.assertEqual(
            _template_variable_placeholder_columns(
                "sum(label_threshold:rate5m) * $threshold",
                "TS m\n| STATS v = SUM(metrics.label_threshold:rate5m)",
            ),
            [],
        )

    def test_placeholder_embedded_in_a_recording_rule_name_is_flagged(self):
        # ``metric:$threshold:rate5m`` becomes the single identifier
        # ``metric:label_threshold:rate5m``, which is just as unresolvable as the
        # bare placeholder. The emitted column is backtick-quoted because of the
        # ``:`` segments.
        self.assertEqual(
            _template_variable_placeholder_columns(
                "foo + metric:$threshold:rate5m",
                "TS m\n| EVAL v = (metrics.foo + `metric:label_threshold:rate5m`)",
            ),
            ["threshold"],
        )

    def test_detects_the_remote_write_column_shape(self):
        # ``--field-profile prometheus_remote_write`` emits
        # ``prometheus.<metric>.value``; the suffix is not part of the PromQL
        # name, so it must not hide the placeholder.
        for column in (
            "prometheus.label_threshold.value",
            "prometheus.label_threshold.counter",
            "prometheus.label_threshold.rate",
        ):
            with self.subTest(column=column):
                self.assertEqual(
                    _template_variable_placeholder_columns(
                        "foo >= ($threshold / 100)", f"TS m\n| EVAL v = {column}"
                    ),
                    ["threshold"],
                )

    def test_bound_parameter_is_not_a_column_read(self):
        # ``?label_v`` is a value supplied with the request, not a field, so it
        # can never be a missing column.
        self.assertEqual(
            _template_variable_placeholder_columns(
                "foo + $v", "TS m\n| WHERE labels.x == ?label_v"
            ),
            [],
        )

    def test_column_the_query_defines_itself_is_not_a_column_read(self):
        # A computed column is not looked up in the index, so reading it later in
        # the pipeline is legitimate.
        for query in (
            "TS m\n| EVAL label_v = 1\n| KEEP label_v",
            "TS m\n| STATS label_v = COUNT(*)",
            "TS m\n| RENAME metrics.other AS label_v\n| KEEP label_v",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    _template_variable_placeholder_columns("foo + $v", query), []
                )

    def test_self_aliased_placeholder_is_still_a_column_read(self):
        # The renderer's ordinary shape aliases a metric to its own name, so the
        # right-hand side reads the index field even though the left-hand side
        # looks like a definition. Treating the name as merely "computed" here
        # would silently disarm the guard for the panel in the issue.
        self.assertEqual(
            _template_variable_placeholder_columns(
                "foo >= ($threshold / 100)",
                "TS m\n| WHERE label_threshold IS NOT NULL\n"
                "| STATS label_threshold = AVG(LAST_OVER_TIME(label_threshold)) BY ns\n"
                "| EVAL v = (foo / (label_threshold / 100))",
            ),
            ["threshold"],
        )

    def test_read_before_a_later_computed_definition_still_counts(self):
        # The WHERE really does read the index field; a later stage redefining
        # the same name cannot retroactively make that read harmless.
        self.assertEqual(
            _template_variable_placeholder_columns(
                "foo + $v",
                "TS m\n| WHERE label_v > 0\n| EVAL label_v = 1\n| KEEP label_v",
            ),
            ["v"],
        )
        self.assertEqual(
            _template_variable_placeholder_columns(
                "foo + $v", "TS m\n| STATS a = MAX(label_v), label_v = 1"
            ),
            ["v"],
        )

    def test_comparison_is_not_mistaken_for_an_assignment(self):
        # ``>=`` / ``==`` must keep reading as a column reference, or the guard
        # would miss the shape from the issue itself.
        for query in (
            "TS m\n| WHERE metrics.label_threshold >= 5",
            "TS m\n| WHERE metrics.label_threshold == 5",
            "TS m\n| WHERE metrics.label_threshold != 5",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    _template_variable_placeholder_columns(
                        "foo >= ($threshold / 100)", query
                    ),
                    ["threshold"],
                )

    def test_placeholder_only_inside_an_esql_string_is_not_a_column(self):
        self.assertEqual(
            _template_variable_placeholder_columns(
                "foo >= ($threshold / 100)",
                'TS m\n| EVAL note = "label_threshold"',
            ),
            [],
        )


class TestPhantomColumnIsManualizedNotShipped(unittest.TestCase):
    """A variable whose value cannot be inlined must not ship a phantom column."""

    def test_query_variable_used_as_a_scalar_degrades_to_not_feasible(self):
        panel, _ = _translate(
            PVC_EXPR_TEMPLATE.format(threshold=f"${PVC_THRESHOLD_VAR}"),
            [
                {
                    "type": "query",
                    "name": PVC_THRESHOLD_VAR,
                    "query": "label_values(kubelet_volume_stats_used_bytes, threshold)",
                }
            ],
            panel_type="table",
        )
        self.assertEqual(panel.status, "not_feasible")
        self.assertNotIn(f"label_{PVC_THRESHOLD_VAR}", panel.esql_query or "")
        joined = " ".join(panel.reasons or [])
        self.assertIn(f"${PVC_THRESHOLD_VAR}", joined)
        self.assertIn("can never exist", joined)


class TestLlmRecoveryCannotReintroduceThePhantomColumn(unittest.TestCase):
    """The LLM last resort runs after the validators, so it re-checks itself."""

    def _translate_with_llm(self, esql_query):
        from observability_migration.adapters.source.grafana import translate

        calls = []

        def fake_llm(**kwargs):
            calls.append(kwargs)
            return {"esql_query": esql_query, "source_type": "TS"}

        with unittest.mock.patch.object(translate, "attempt_llm_translation", fake_llm):
            context = translate.translate_promql_to_esql(
                "foo >= ($threshold / 100)",
                "metrics-*",
                esql_index="metrics-*",
                llm_endpoint="http://llm.invalid",
                llm_model="test-model",
            )
        self.assertTrue(calls, "the LLM path must actually have been reached")
        return context

    def test_recovery_reading_the_placeholder_is_refused(self):
        # The LLM is prompted with the *cleaned* PromQL, where the variable has
        # already become ``label_threshold``, so it readily returns it as a
        # field. Accepting that would ship the very column the guard rejected.
        context = self._translate_with_llm(
            "TS metrics-*\n| EVAL v = (metrics.foo / metrics.label_threshold)"
        )
        self.assertEqual(context.feasibility, "not_feasible")
        self.assertNotEqual(context.parser_backend, "llm")

    def test_recovery_avoiding_the_placeholder_is_still_accepted(self):
        context = self._translate_with_llm("TS metrics-*\n| STATS v = MAX(metrics.foo)")
        self.assertEqual(context.feasibility, "feasible")
        self.assertEqual(context.parser_backend, "llm")


class TestValidationGateIsEvidenceBased(unittest.TestCase):
    """A template-variable column is a translation error, not a data gap."""

    PLACEHOLDER_FAILURE = {
        "error": "line 2:123: Unknown column [metrics.label_threshold]",
        "esql_query": "TS metrics-*\n| EVAL v = (foo / metrics.label_threshold)",
        "analysis": {
            "unknown_columns": [
                {"name": "metrics.label_threshold", "role": "metric"}
            ]
        },
    }

    def test_column_from_a_template_variable_is_recognized(self):
        self.assertTrue(
            unknown_column_is_source_template_variable(
                "metrics.label_threshold", "foo >= ($threshold / 100)"
            )
        )
        self.assertTrue(
            unknown_column_is_source_template_variable(
                "label_threshold", "foo >= ${threshold:raw}"
            )
        )
        self.assertTrue(
            unknown_column_is_source_template_variable(
                "label_threshold", "foo >= [[threshold]]"
            )
        )

    def test_column_without_a_matching_source_variable_is_not_recognized(self):
        self.assertFalse(
            unknown_column_is_source_template_variable(
                "metrics.label_threshold", "sum(label_threshold)"
            )
        )
        self.assertFalse(
            unknown_column_is_source_template_variable(
                "metrics.kubelet_volume_stats_used_bytes", "foo >= ($threshold / 100)"
            )
        )
        self.assertFalse(
            unknown_column_is_source_template_variable("label_threshold", "")
        )

    def test_source_that_already_names_the_column_abstains(self):
        # ``label_threshold`` may be a real metric whose telemetry has not
        # arrived; refusing to self-heal would manualize a working panel. The
        # adapter-side guard is the precise check, so core stays conservative.
        self.assertFalse(
            unknown_column_is_source_template_variable(
                "metrics.label_threshold", 'foo + label_threshold{job="$threshold"}'
            )
        )

    def test_recording_rule_prefix_does_not_count_as_naming_the_column(self):
        self.assertTrue(
            unknown_column_is_source_template_variable(
                "metrics.label_threshold",
                "sum(label_threshold:rate5m) >= ($threshold / 100)",
            )
        )

    def test_remote_write_column_shape_is_recognized(self):
        # ``--field-profile prometheus_remote_write`` reports the failing column
        # with its value suffix, which must not read as a different field.
        for column in (
            "prometheus.label_threshold.value",
            "prometheus.label_threshold.counter",
            "prometheus.label_threshold.rate",
        ):
            with self.subTest(column=column):
                self.assertTrue(
                    unknown_column_is_source_template_variable(
                        column, "foo >= ($threshold / 100)"
                    )
                )

    def test_name_only_inside_a_source_string_does_not_trigger_abstention(self):
        # ``label_threshold`` here is a matcher *value* the source compares
        # against, not a metric it reads, so the abstention must not fire and the
        # phantom column must still be refused.
        self.assertTrue(
            unknown_column_is_source_template_variable(
                "metrics.label_threshold", 'foo{job="label_threshold"} + $threshold'
            )
        )

    def test_unterminated_source_string_does_not_trigger_abstention(self):
        # A malformed quote must not smuggle the name out of the string and
        # excuse the phantom column.
        self.assertTrue(
            unknown_column_is_source_template_variable(
                "metrics.label_threshold", 'foo{job="label_threshold} + $threshold'
            )
        )

    def test_unrelated_dotted_suffix_is_not_recognized(self):
        self.assertFalse(
            unknown_column_is_source_template_variable(
                "prometheus.label_threshold.histogram", "foo >= ($threshold / 100)"
            )
        )

    def test_self_heal_is_refused_for_a_template_variable_column(self):
        self.assertFalse(
            validation_failure_self_heals(
                self.PLACEHOLDER_FAILURE,
                source_expression="foo >= ($threshold / 100)",
            )
        )

    def test_genuinely_missing_metric_still_self_heals(self):
        self.assertTrue(
            validation_failure_self_heals(
                {
                    "error": "line 2:20: Unknown column [metrics.kubelet_volume_stats_used_bytes]",
                    "esql_query": "TS metrics-*\n| STATS v = MAX(metrics.kubelet_volume_stats_used_bytes)",
                    "analysis": {
                        "unknown_columns": [
                            {
                                "name": "metrics.kubelet_volume_stats_used_bytes",
                                "role": "metric",
                            }
                        ]
                    },
                },
                source_expression="max(kubelet_volume_stats_used_bytes) >= ($threshold / 100)",
            )
        )

    def test_omitting_the_source_expression_keeps_the_previous_behavior(self):
        self.assertTrue(validation_failure_self_heals(self.PLACEHOLDER_FAILURE))

    def test_failed_validation_outcome_manualizes_the_panel(self):
        from observability_migration.adapters.source.grafana.cli import (
            _apply_failed_validation_outcome,
        )

        panel = PanelResult("Panel", "table", "table", "migrated", 0.9)
        panel.promql_expr = "foo >= ($threshold / 100)"
        panel.esql_query = self.PLACEHOLDER_FAILURE["esql_query"]
        outcome = _apply_failed_validation_outcome(panel, self.PLACEHOLDER_FAILURE)
        self.assertEqual(outcome, "placeholder")
        self.assertEqual(panel.status, "requires_manual")
        self.assertEqual(panel.kibana_type, "markdown")
        self.assertTrue(panel.post_validation_action.startswith("placeholder_"))

    def test_failed_validation_outcome_still_self_heals_a_missing_metric(self):
        from observability_migration.adapters.source.grafana.cli import (
            _apply_failed_validation_outcome,
        )

        panel = PanelResult("Panel", "graph", "line", "migrated", 0.9)
        panel.promql_expr = "irate(foo_total[5m])"
        panel.esql_query = "TS metrics-*\n| STATS foo = IRATE(foo_total)"
        outcome = _apply_failed_validation_outcome(
            panel,
            {
                "error": "line 2:20: Unknown column [foo_total]",
                "analysis": {"unknown_columns": [{"name": "foo_total", "role": "metric"}]},
            },
        )
        self.assertEqual(outcome, "self_heal")
        self.assertEqual(panel.kibana_type, "line")


if __name__ == "__main__":
    unittest.main()
