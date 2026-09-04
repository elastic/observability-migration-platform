# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for GitHub issue #376.

Element-wise PromQL arithmetic between two *different* metric names with no
explicit matcher (``A / B``) was routed to the native ``PROMQL`` ES|QL command,
where Elasticsearch silently returns zero rows: its implicit vector-matching key
includes ``__name__``, which real PromQL excludes when matching binary-operator
operands. The panel scored ``migrated`` (confidence 0.9, no warning) and
rendered empty.

The expectations below encode behaviour verified live against Elasticsearch
9.5.0-SNAPSHOT over an index containing every metric named here. Row counts from
that run:

    kube_statefulset_status_replicas_ready                      960 rows
    kube_statefulset_status_replicas                            960 rows
    ready / total                             (distinct names)    0 rows
    ready / total * 100                       (as migrated)       0 rows
    total / total                             (same name)       960 rows
    sum(ready) / sum(total)                   (aggregated)      480 rows
    total * 2                                 (vector⊗scalar)   960 rows
    rate(rx[5m]) / rate(tx[5m])               (distinct names)    0 rows
    (ready * 1) / (total * 1)                 (scalar-scaled)     0 rows
    vector(1) / ready                         (nameless vector) 960 rows

The separating rule is which constructs drop ``__name__``: aggregation
operators and ``vector()`` do; function calls and scalar arithmetic do not.
Elasticsearch differs from Prometheus on the latter two, which is why
``rate(A[5m]) / rate(B[5m])`` also fails.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana.panels import (
    _promql_has_unmatchable_vector_match,
    can_use_native_promql,
)
from observability_migration.adapters.source.grafana.promql import (
    promql_has_unmatchable_distinct_metric_binop as detect,
)

READY = "kube_statefulset_status_replicas_ready"
TOTAL = "kube_statefulset_status_replicas"


class DetectorUnmatchableShapesTests(unittest.TestCase):
    """Shapes that Elasticsearch resolves to zero rows must be detected."""

    def test_bare_distinct_metric_ratio(self):
        self.assertTrue(detect(f"{READY} / {TOTAL}"))

    def test_reported_expression_with_selectors_and_scalar_tail(self):
        self.assertTrue(
            detect(
                f'{READY}{{namespace=~"monitoring"}}'
                f'/{TOTAL}{{namespace=~"monitoring"}}*100'
            )
        )

    def test_subtraction_between_distinct_metrics(self):
        self.assertTrue(detect("node_time_seconds - node_boot_time_seconds"))

    def test_one_minus_ratio(self):
        self.assertTrue(detect(f"1 - {READY} / {TOTAL}"))

    def test_range_functions_do_not_drop_the_metric_name(self):
        # Prometheus drops __name__ after rate(); Elasticsearch does not.
        self.assertTrue(
            detect(
                "rate(container_network_receive_bytes_total[5m])"
                " / rate(container_network_transmit_bytes_total[5m])"
            )
        )

    def test_instant_functions_do_not_drop_the_metric_name(self):
        self.assertTrue(detect(f"abs({READY}) / abs({TOTAL})"))
        self.assertTrue(detect(f"round({READY}) / round({TOTAL})"))
        self.assertTrue(detect(f"clamp_max({READY}, 5) / clamp_max({TOTAL}, 5)"))

    def test_over_time_functions_do_not_drop_the_metric_name(self):
        self.assertTrue(
            detect(f"avg_over_time({READY}[5m]) / avg_over_time({TOTAL}[5m])")
        )

    def test_scalar_arithmetic_does_not_drop_the_metric_name(self):
        # Prometheus drops __name__ for vector⊗scalar; Elasticsearch does not.
        self.assertTrue(detect(f"({READY} * 1) / ({TOTAL} * 1)"))

    def test_unary_negation_propagates_the_metric_name(self):
        self.assertTrue(detect(f"-{READY} / {TOTAL}"))

    def test_nested_operand_is_detected_inside_an_aggregation(self):
        # sum(A / B): the aggregation is fine, the inner division is not.
        self.assertTrue(detect(f"sum({READY} / {TOTAL})"))

    def test_nested_operand_is_detected_under_a_scalar_tail(self):
        self.assertTrue(detect(f"({READY} / {TOTAL}) * 100"))

    def test_mixed_same_and_distinct_names_detects_the_distinct_pair(self):
        self.assertTrue(detect(f"({READY} / {READY}) / {TOTAL}"))

    def test_three_way_distinct_chain(self):
        self.assertTrue(
            detect(
                "kube_hpa_status_current_replicas / kube_hpa_spec_max_replicas"
                " / kube_hpa_spec_min_replicas"
            )
        )

    def test_mixed_wrapper_and_bare_operand(self):
        self.assertTrue(detect(f"{READY} / avg_over_time({TOTAL}[5m])"))


class DetectorMatchableShapesTests(unittest.TestCase):
    """Shapes Elasticsearch resolves normally must NOT be blocked.

    Over-blocking would push working panels onto the same-bucket ES|QL
    approximation for no reason, so each of these is pinned.
    """

    def test_same_metric_name_on_both_sides(self):
        self.assertFalse(detect(f"{TOTAL} / {TOTAL}"))

    def test_same_metric_name_under_different_selectors(self):
        # The canonical Prometheus error-rate ratio.
        self.assertFalse(
            detect(
                'rate(http_requests_total{code=~"5.."}[5m])'
                " / rate(http_requests_total[5m])"
            )
        )

    def test_aggregation_drops_the_metric_name_on_both_sides(self):
        self.assertFalse(detect(f"sum({READY}) / sum({TOTAL})"))

    def test_grouped_aggregation_drops_the_metric_name(self):
        self.assertFalse(
            detect(f"sum by (namespace) ({READY}) / sum by (namespace) ({TOTAL})")
        )

    def test_aggregation_over_rate_drops_the_metric_name(self):
        self.assertFalse(
            detect(
                "sum(rate(container_network_receive_bytes_total[5m]))"
                " / sum(rate(container_network_transmit_bytes_total[5m]))"
            )
        )

    def test_other_aggregation_operators_drop_the_metric_name(self):
        for agg in ("avg", "min", "max", "count"):
            with self.subTest(agg=agg):
                self.assertFalse(detect(f"{agg}({READY}) / {agg}({TOTAL})"))

    def test_vector_scalar_arithmetic(self):
        self.assertFalse(detect(f"{TOTAL} * 2"))
        self.assertFalse(detect(f"{TOTAL} * 100 / 2"))

    def test_vector_literal_is_nameless_and_matches_anything(self):
        self.assertFalse(detect(f"vector(1) / {READY}"))
        self.assertFalse(detect(f"{READY} / vector(1)"))

    def test_single_metric_expressions(self):
        self.assertFalse(detect(READY))
        self.assertFalse(detect("rate(container_cpu_usage_seconds_total[5m])"))

    def test_set_operators_are_not_flagged(self):
        # Distinct names are normal for or/and/unless, and those are blocked
        # from the native path separately.
        for op in ("or", "and", "unless"):
            with self.subTest(op=op):
                self.assertFalse(detect(f"{READY} {op} {TOTAL}"))

    def test_explicit_vector_matching_is_not_flagged(self):
        # on()/ignoring() fail loudly with a 400, so they never fail silently
        # and are rejected by the unsupported-construct gate instead.
        self.assertFalse(detect(f"{READY} / on(statefulset) {TOTAL}"))
        self.assertFalse(detect(f"{READY} / ignoring(__name__) {TOTAL}"))
        self.assertFalse(detect(f"{READY} * on(statefulset) group_left() {TOTAL}"))

    def test_mixed_aggregation_against_a_bare_metric_is_not_flagged(self):
        """``sum(A) / B`` is a loud 400 on Elasticsearch, not a silent empty.

        Live 9.5.0-SNAPSHOT: ``aggregate function [...] not allowed outside
        STATS``. Flagging it would hide a visible failure behind the same-bucket
        approximation; leaving it native keeps the 400. ``vector(1) / A``
        (nameless vs named) does return rows, so empty-vs-non-empty name sets
        are not themselves unmatchable.
        """
        self.assertFalse(detect(f"sum({READY}) / {TOTAL}"))
        self.assertFalse(detect(f"sum by (namespace) ({READY}) / {TOTAL}"))
        self.assertFalse(detect(f"{READY} / sum({TOTAL})"))

    def test_name_regex_selector_is_not_flagged(self):
        """``{__name__=~\"...\"} / metric`` is a loud 400, not a silent empty.

        Live 9.5.0-SNAPSHOT: ``regex label selectors on __name__ are not
        supported at this time``. Exact ``{__name__=\"A\"} / B`` *is* detected
        because both sides are determinate disjoint names.
        """
        self.assertFalse(detect(f'{{__name__=~"{READY}"}} / {TOTAL}'))
        self.assertTrue(detect(f'{{__name__="{READY}"}} / {TOTAL}'))

    def test_metricless_selector_is_indeterminate_not_flagged(self):
        # Elasticsearch rejects these outright ("__name__ label selector is
        # required"); do not guess at their name set.
        self.assertFalse(detect(f'{{job="k8s"}} / {READY}'))

    def test_unparseable_and_empty_expressions_are_safe(self):
        for expr in ("", "   ", None, "not ) valid ( promql", "a / b[",):
            with self.subTest(expr=expr):
                self.assertFalse(detect(expr))


class MacroResolutionTests(unittest.TestCase):
    """The panel-level wrapper resolves Grafana macros before parsing."""

    def test_macro_range_is_resolved_before_analysis(self):
        expr = (
            "rate(node_network_receive_bytes_total[$__rate_interval])"
            " / rate(node_network_transmit_bytes_total[$__rate_interval])"
        )
        # The raw form does not parse, so the AST-only detector cannot see it...
        self.assertFalse(detect(expr))
        # ...but the wrapper analyses the macro-resolved form the native command
        # is actually built from.
        self.assertTrue(_promql_has_unmatchable_vector_match(expr))

    def test_template_variable_in_matcher_still_parses(self):
        expr = f'{READY}{{namespace=~"$namespace"}} / {TOTAL}{{namespace=~"$namespace"}}'
        self.assertTrue(_promql_has_unmatchable_vector_match(expr))

    def test_wrapper_is_safe_on_empty_input(self):
        self.assertFalse(_promql_has_unmatchable_vector_match(""))
        self.assertFalse(_promql_has_unmatchable_vector_match(None))


class NativeEligibilityTests(unittest.TestCase):
    """``can_use_native_promql`` is the single gate both dashboards and alerts
    consult, so the decision has to land there."""

    def test_distinct_metric_arithmetic_is_not_native_eligible(self):
        self.assertFalse(can_use_native_promql(f"{READY} / {TOTAL}"))

    def test_aggregated_ratio_stays_native_eligible(self):
        self.assertTrue(can_use_native_promql(f"sum({READY}) / sum({TOTAL})"))

    def test_same_metric_ratio_stays_native_eligible(self):
        self.assertTrue(can_use_native_promql(f"{TOTAL} / {TOTAL}"))


def _unified_rule_with_expr(expr):
    """A minimal Grafana unified rule whose query is *expr* with a threshold."""
    return {
        "uid": "rule-376",
        "title": "Memory pressure alert",
        "ruleGroup": "resource-alerts",
        "folderUID": "folder-1",
        "condition": "C",
        "for": "5m",
        "noDataState": "NoData",
        "execErrState": "Error",
        "isPaused": False,
        "labels": {"severity": "warning"},
        "annotations": {"summary": "Memory is above threshold"},
        "data": [
            {
                "refId": "A",
                "datasourceUid": "prometheus",
                "relativeTimeRange": {"from": 300, "to": 0},
                "model": {"expr": expr},
            },
            {
                "refId": "B",
                "datasourceUid": "-100",
                "relativeTimeRange": {"from": 0, "to": 0},
                "model": {"type": "reduce", "reducer": "last"},
            },
            {
                "refId": "C",
                "datasourceUid": "-100",
                "relativeTimeRange": {"from": 0, "to": 0},
                "model": {
                    "type": "threshold",
                    "conditions": [{"evaluator": {"type": "lt", "params": [0.1]}}],
                },
            },
        ],
    }


class AlertRoutingTests(unittest.TestCase):
    """An alert on a distinct-metric ratio must not migrate to a native query
    that can never return rows — such a rule would never fire, which is a
    quieter and more dangerous failure than an empty panel. Route it through
    the ES|QL translator so it stays automated and evaluable.
    """

    def setUp(self):
        from observability_migration.adapters.source.grafana.alert_pipeline import (
            build_unified_alert_irs,
        )

        self.build = build_unified_alert_irs

    def _ir_for(self, expr):
        return self.build(
            {
                "alert_rules": [_unified_rule_with_expr(expr)],
                "rule_groups": [
                    {
                        "folderUid": "folder-1",
                        "title": "resource-alerts",
                        "interval": 300,
                    }
                ],
            }
        )[0]

    def _query_for(self, expr):
        from observability_migration.core.mapping import _generate_esql_for_alert

        return _generate_esql_for_alert(self._ir_for(expr), "metrics-*")

    def test_distinct_metric_alert_uses_esql_not_native_promql(self):
        query = self._query_for(
            "node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes < 0.1"
        )
        self.assertTrue(query, "the rule must keep a source-faithful query")
        self.assertNotIn("PROMQL index=", query)
        self.assertIn("node_memory_MemAvailable_bytes", query)
        self.assertIn("node_memory_MemTotal_bytes", query)
        # The source threshold has to survive the reroute, or the rule fires
        # on every bucket.
        self.assertIn("< 0.1", query)

    def test_aggregated_ratio_alert_still_uses_native_promql(self):
        query = self._query_for(
            "sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes) < 0.1"
        )
        self.assertIn("PROMQL index=", query)

    def test_reroutable_alert_reports_a_source_faithful_query(self):
        from observability_migration.core.mapping import _has_source_faithful_query

        ir = self._ir_for(
            "node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes < 0.1"
        )
        self.assertTrue(_has_source_faithful_query(ir))

    def test_control_bound_matcher_alert_is_not_called_faithful(self):
        """Dashboard ``$namespace`` matchers have no control on an alert rule.

        The generator already returned empty for this shape (unbound ``?namespace``
        would fail evaluation). The faithfulness probe must agree, or the rule
        is advertised as automated with no query.
        """
        from observability_migration.core.mapping import (
            _generate_esql_for_alert,
            _has_source_faithful_query,
        )

        expr = (
            "node_memory_MemAvailable_bytes{namespace=~\"$namespace\"} "
            "/ node_memory_MemTotal_bytes{namespace=~\"$namespace\"} < 0.1"
        )
        ir = self._ir_for(expr)
        self.assertFalse(_has_source_faithful_query(ir))
        self.assertFalse(str(_generate_esql_for_alert(ir, "metrics-*") or "").strip())

    def test_faithfulness_probe_agrees_with_the_query_it_promises(self):
        """The probe is resolver-free; the real call is not.

        ``_has_source_faithful_query`` has no resolver to probe with, so it asks
        the translator without one. A resolver only renames fields and picks
        ``TS`` vs ``FROM``, never the feasibility verdict — pin that, because if
        a resolver could flip it the predicate would go back to promising
        queries that ``_generate_esql_for_alert`` declines to emit.
        """
        from observability_migration.adapters.source.grafana import rules
        from observability_migration.adapters.source.grafana.schema import (
            SchemaResolver,
        )
        from observability_migration.core.mapping import (
            _generate_esql_for_alert,
            _has_source_faithful_query,
        )

        strict_pack = rules.RulePackConfig()
        strict_pack.not_feasible_patterns = rules._pattern_rules(
            [(".*", "a custom pack that rejects everything")]
        )
        resolvers = {
            "none": None,
            "default": SchemaResolver(rules.RulePackConfig()),
            "custom rule pack": SchemaResolver(strict_pack),
            "label rewrites": SchemaResolver(
                rules.RulePackConfig(label_rewrites={"job": "labels.job"})
            ),
        }

        for expr in (
            "node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes < 0.1",
            "changes(node_memory_MemAvailable_bytes[5m]) "
            "/ node_memory_MemTotal_bytes < 0.1",
            "node_memory_MemAvailable_bytes{namespace=~\"$namespace\"} "
            "/ node_memory_MemTotal_bytes{namespace=~\"$namespace\"} < 0.1",
        ):
            ir = self._ir_for(expr)
            promised = _has_source_faithful_query(ir)
            for label, resolver in resolvers.items():
                with self.subTest(expr=expr, resolver=label):
                    ir.translated_query = ""
                    query = _generate_esql_for_alert(ir, "metrics-*", resolver=resolver)
                    self.assertEqual(bool(str(query).strip()), promised)

    def test_untranslatable_distinct_metric_alert_is_not_called_faithful(self):
        """Being detector-positive is not enough to promise a query.

        These expressions are steered off the native path, but the ES|QL
        translator has no form for them either. Claiming a source-faithful
        query here would pick an automated tier and a target rule type for a
        rule that ends up carrying no query at all.
        """
        from observability_migration.adapters.source.grafana.panels import (
            _promql_has_unmatchable_vector_match,
        )
        from observability_migration.core.mapping import (
            _generate_esql_for_alert,
            _has_source_faithful_query,
        )

        for expr in (
            "absent_over_time(node_memory_MemAvailable_bytes[5m]) "
            "/ node_memory_MemTotal_bytes < 0.1",
            "absent(node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes < 0.1",
            "changes(node_memory_MemAvailable_bytes[5m]) "
            "/ node_memory_MemTotal_bytes < 0.1",
            "predict_linear(node_memory_MemAvailable_bytes[5m], 60) "
            "/ node_memory_MemTotal_bytes < 0.1",
        ):
            with self.subTest(expr=expr):
                self.assertTrue(
                    _promql_has_unmatchable_vector_match(expr),
                    "the detector must still steer this off the native path",
                )
                ir = self._ir_for(expr)
                self.assertFalse(_has_source_faithful_query(ir))
                self.assertEqual(_generate_esql_for_alert(ir, "metrics-*"), "")


if __name__ == "__main__":
    unittest.main()
