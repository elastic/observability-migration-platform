# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for GitHub issue #440.

Elasticsearch evaluates PromQL vector matching -- ``on(...)``, ``ignoring(...)``,
``group_left``, ``group_right`` -- on Serverless and Stack 9.6
(elastic/elasticsearch#155634). Migration used to reject all four
unconditionally, so panels that used them always fell back to the ES|QL
join/ratio approximation even on a capable target.

The support is narrower than Prometheus's: Elasticsearch only plans an
explicitly matched binary operation when it can determine each operand's label
set *statically*. Otherwise it fails analysis with ``vector matching requires
operands with concrete label sets`` -- in Kibana a hard panel error, which is
strictly worse than the ES|QL approximation the panel has today. So routing is
gated on both the target capability and the operand shape.

Every expectation below was cross-checked live against Elasticsearch
9.6.0-SNAPSHOT over an index holding every metric named here (accept = HTTP 200,
reject = HTTP 400 naming the concrete-label-set rule):

    sum by (d) (A)          / on(d) sum by (d) (B)      accept
    sum(A)                  / on(d) sum(B)              accept
    sum(rate(A[5m]))        / ignoring(d) sum(rate(B))  accept
    abs(sum by (d) (A))     / on(d) sum by (d) (B)      accept
    quantile by (d)(.9, A)  / on(d) sum by (d) (B)      accept
    vector(1)               / on(d) sum by (d) (B)      accept
    sum by(d)(A)/sum by(d)(B) / on(d) sum by (d)(B)     accept
    A                       / on(d) sum by (d) (B)      reject
    rate(A[5m])             / on(d) sum by (d) (B)      reject
    avg_over_time(A[5m])    / on(d) sum by (d) (B)      reject
    sum without (d) (A)     / on(d) sum by (d) (B)      reject
    A / B                   / on(d) sum by (d) (B)      reject
    A                       / ignoring() B              reject
    sum by (d)(A) and/or/unless on(d) sum by (d)(B)     reject

and the same run confirmed the probe itself: HTTP 200 on 9.6.0-SNAPSHOT, HTTP
400 (``VectorBinaryArithmetic queries with group modifiers are not supported at
this time``) on 9.5.0.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.panels import (
    _promql_vector_matching_is_native_feasible as feasible,
)
from observability_migration.adapters.source.grafana.panels import (
    can_use_native_promql,
)
from observability_migration.adapters.source.grafana.promql import (
    promql_vector_matching_has_indeterminate_operand as indeterminate,
)
from observability_migration.adapters.source.grafana.runtime_features import (
    PROMQL_VECTOR_MATCHING,
)

RX = "node_network_receive_bytes_total"
TX = "node_network_transmit_bytes_total"

CAPABLE = {PROMQL_VECTOR_MATCHING: True}


class ConcreteOperandShapeTests(unittest.TestCase):
    """Shapes Elasticsearch can plan must be reported feasible.

    Under-reporting here is not a correctness bug but it is the whole point of
    the issue: every one of these otherwise stays on the ES|QL approximation.
    """

    def test_aggregation_with_by_on_both_sides(self):
        self.assertTrue(feasible(f"sum by (device) ({RX}) / on(device) sum by (device) ({TX})"))

    def test_aggregation_with_no_modifier_collapses_to_a_known_empty_label_set(self):
        self.assertTrue(feasible(f"sum({RX}) / on(device) sum({TX})"))

    def test_empty_by_list_is_still_an_explicit_label_set(self):
        self.assertTrue(feasible(f"sum by () ({RX}) / on() sum by () ({TX})"))

    def test_range_function_inside_the_aggregation_is_fine(self):
        # The aggregation pins the label set; what it aggregates does not matter.
        self.assertTrue(
            feasible(
                f"sum by (device) (rate({RX}[5m]))"
                f" / on(device) sum by (device) (rate({TX}[5m]))"
            )
        )

    def test_issue_report_ignoring_over_unaggregated_sums(self):
        self.assertTrue(
            feasible(f"sum(rate({RX}[5m])) / ignoring(device) sum(rate({TX}[5m]))")
        )

    def test_instant_function_propagates_a_concrete_operand(self):
        for wrapper in ("abs", "round", "ceil", "floor"):
            with self.subTest(wrapper=wrapper):
                self.assertTrue(
                    feasible(
                        f"{wrapper}(sum by (device) ({RX}))"
                        f" / on(device) sum by (device) ({TX})"
                    )
                )

    def test_function_with_a_scalar_argument_propagates_the_vector_argument(self):
        self.assertTrue(
            feasible(
                f"clamp_max(sum by (device) ({RX}), 5)"
                f" / on(device) sum by (device) ({TX})"
            )
        )
        self.assertTrue(
            feasible(
                f"histogram_quantile(0.9, sum by (device, le) ({RX}))"
                f" / on(device) sum by (device) ({TX})"
            )
        )

    def test_unary_minus_and_parens_propagate(self):
        self.assertTrue(feasible(f"-sum by (device)({RX}) / on(device) sum by (device)({TX})"))
        self.assertTrue(
            feasible(f"(sum by (device)({RX})) / on(device) (sum by (device)({TX}))")
        )

    def test_scalar_arithmetic_on_an_operand_propagates(self):
        self.assertTrue(
            feasible(f"(sum by (device)({RX}) * 1) / on(device) sum by (device)({TX})")
        )

    def test_vector_literal_is_labelless_and_therefore_concrete(self):
        self.assertTrue(feasible(f"sum by (device)({RX}) / on(device) vector(1)"))

    def test_nested_unmatched_arithmetic_between_concrete_operands(self):
        self.assertTrue(
            feasible(
                f"(sum by (device)({RX}) / sum by (device)({TX}))"
                f" / on(device) sum by (device)({TX})"
            )
        )

    def test_quantile_and_other_aggregation_operators_pin_their_label_set(self):
        for agg in ("avg", "min", "max", "count", "stddev"):
            with self.subTest(agg=agg):
                self.assertTrue(
                    feasible(
                        f"{agg} by (device)({RX}) / on(device) {agg} by (device)({TX})"
                    )
                )
        self.assertTrue(
            feasible(f"quantile by (device)(0.9, {RX}) / on(device) sum by (device)({TX})")
        )

    def test_group_modifiers_over_concrete_operands(self):
        self.assertTrue(
            feasible(
                f"sum by (device)({RX}) * on(device) group_left() sum by (device)({TX})"
            )
        )
        self.assertTrue(
            feasible(
                f"sum by (device)({RX}) * on(device) group_right() sum by (device)({TX})"
            )
        )

    def test_comparison_operator_with_a_matcher(self):
        self.assertTrue(
            feasible(f"sum by (device)({RX}) > on(device) sum by (device)({TX})")
        )

    def test_matcher_free_expressions_are_never_blocked_by_this_gate(self):
        # No explicit matcher means nothing for this predicate to reject; the
        # #376 detector owns implicit matching.
        self.assertTrue(feasible(f"{RX} / {TX}"))
        self.assertTrue(feasible(f"rate({RX}[5m])"))

    def test_grafana_rate_interval_macro_is_resolved_before_analysis(self):
        expr = (
            f"sum by (device) (rate({RX}[$__rate_interval]))"
            f" / on(device) sum by (device) (rate({TX}[$__rate_interval]))"
        )
        # The raw form does not parse, so the AST-only detector cannot see it...
        self.assertTrue(indeterminate(expr))
        # ...but the wrapper analyses the macro-resolved form that is emitted.
        self.assertTrue(feasible(expr))


class IndeterminateOperandShapeTests(unittest.TestCase):
    """Shapes Elasticsearch rejects must keep the ES|QL fallback.

    A false positive here is the damaging direction: it replaces a working
    approximation with a panel that hard-errors in Kibana.
    """

    def test_bare_selector_operand(self):
        self.assertFalse(feasible(f"sum by (device)({RX}) / on(device) {TX}"))
        self.assertFalse(feasible(f"{RX} / on(device) sum by (device)({TX})"))

    def test_issue_report_group_left_over_bare_selectors(self):
        # Panel 4 of the issue. Prometheus evaluates this; Elasticsearch does
        # not, because neither operand has a statically known label set.
        self.assertFalse(
            feasible(
                "node_hwmon_temp_celsius * on(chip) group_left(chip_name)"
                " node_hwmon_chip_names"
            )
        )

    def test_range_function_over_a_bare_selector(self):
        for func in ("rate", "irate", "increase", "delta", "deriv"):
            with self.subTest(func=func):
                self.assertFalse(
                    feasible(f"{func}({RX}[5m]) / on(device) sum by (device)({TX})")
                )

    def test_over_time_function_over_a_bare_selector(self):
        self.assertFalse(
            feasible(f"avg_over_time({RX}[5m]) / on(device) sum by (device)({TX})")
        )

    def test_without_is_not_a_known_label_set(self):
        # "every label except d" cannot be enumerated without reading the data.
        self.assertFalse(
            feasible(f"sum without (device)({RX}) / on(device) sum by (device)({TX})")
        )
        self.assertFalse(
            feasible(f"sum by (device)({RX}) / on(device) sum without (device)({TX})")
        )

    def test_without_stays_indeterminate_through_a_wrapping_function(self):
        self.assertFalse(
            feasible(f"abs(sum without (device)({RX})) / on(device) sum by (device)({TX})")
        )

    def test_nested_arithmetic_between_bare_selectors(self):
        self.assertFalse(
            feasible(f"({RX} / {TX}) / on(device) sum by (device)({TX})")
        )

    def test_an_indeterminate_inner_match_rejects_the_whole_expression(self):
        # Elasticsearch rejects the query if *any* matched operation fails, so
        # a feasible outer match does not rescue an infeasible inner one.
        self.assertFalse(
            feasible(
                f"(rate({RX}[5m]) / on(device) rate({TX}[5m]))"
                f" / on(device) sum by (device)({TX})"
            )
        )

    def test_empty_matcher_parens_still_require_concrete_operands(self):
        self.assertFalse(feasible(f"{RX} / ignoring() {TX}"))
        self.assertFalse(feasible(f"{RX} / on() {TX}"))

    def test_scalar_operand_is_not_an_instant_vector(self):
        # Elasticsearch: "vector matching only allowed between instant vectors".
        self.assertFalse(feasible(f"sum by (device)({RX}) / on(device) 5"))
        self.assertFalse(feasible(f"sum by (device)({RX}) / on(device) time()"))

    def test_topk_and_bottomk_preserve_the_inner_series_labels(self):
        for agg in ("topk", "bottomk"):
            with self.subTest(agg=agg):
                self.assertFalse(
                    feasible(f"{agg}(5, {RX}) / on(device) sum by (device)({TX})")
                )

    def test_set_operators_with_a_matcher_are_rejected_outright(self):
        # elasticsearch#158181: "set operator [or] with on/ignoring is not
        # supported at this time". Deliberately out of scope for #440.
        for op in ("or", "and", "unless"):
            with self.subTest(op=op):
                self.assertFalse(
                    feasible(
                        f"sum by (device)({RX}) {op} on(device) sum by (device)({TX})"
                    )
                )
                self.assertFalse(
                    feasible(
                        f"sum by (device)({RX}) {op} ignoring(device) sum by (device)({TX})"
                    )
                )

    def test_unparseable_and_empty_expressions_keep_the_fallback(self):
        for expr in ("", "   ", None, "not ) valid ( promql", "a / on(x"):
            with self.subTest(expr=expr):
                self.assertFalse(feasible(expr))

    def test_detector_is_conservative_on_unparseable_input(self):
        # The promql.py predicate answers "indeterminate" so a parse failure can
        # never open the native path.
        self.assertTrue(indeterminate("not ) valid ( promql"))
        self.assertTrue(indeterminate(""))
        self.assertTrue(indeterminate(None))


class NativeEligibilityGateTests(unittest.TestCase):
    """``can_use_native_promql`` needs *both* halves: a capable target and a
    plannable shape."""

    FEASIBLE = f"sum by (device) ({RX}) / on(device) sum by (device) ({TX})"
    INFEASIBLE = f"sum by (device) ({RX}) / on(device) {TX}"

    def test_capable_target_and_concrete_operands_go_native(self):
        self.assertTrue(can_use_native_promql(self.FEASIBLE, runtime_features=CAPABLE))

    def test_no_runtime_features_keeps_the_pre_issue_behaviour(self):
        # An offline run (no --es-url) has no probe result, so nothing changes.
        self.assertFalse(can_use_native_promql(self.FEASIBLE))
        self.assertFalse(can_use_native_promql(self.FEASIBLE, runtime_features={}))

    def test_target_without_the_feature_keeps_the_esql_fallback(self):
        self.assertFalse(
            can_use_native_promql(
                self.FEASIBLE, runtime_features={PROMQL_VECTOR_MATCHING: False}
            )
        )

    def test_capable_target_still_declines_an_indeterminate_shape(self):
        self.assertFalse(can_use_native_promql(self.INFEASIBLE, runtime_features=CAPABLE))

    def test_unsafe_policy_disables_the_feature(self):
        # ``is_feature_supported`` refuses a feature parked behind a fallback /
        # review / block policy even when ``supported`` is True.
        for policy in ("fallback", "review", "block"):
            with self.subTest(policy=policy):
                self.assertFalse(
                    can_use_native_promql(
                        self.FEASIBLE,
                        runtime_features={
                            PROMQL_VECTOR_MATCHING: {
                                "supported": True,
                                "policy": policy,
                            }
                        },
                    )
                )

    def test_other_unsupported_constructs_still_win(self):
        # The feature relaxes the matcher gate only; unrelated blockers stand.
        self.assertFalse(
            can_use_native_promql(
                f"label_replace(sum by (device)({RX}), \"d\", \"$1\", \"device\", \"(.*)\")"
                f" / on(device) sum by (device)({TX})",
                runtime_features=CAPABLE,
            )
        )
        self.assertFalse(
            can_use_native_promql(
                f"sum by (device)({RX}) / on(device) topk(5, {TX})",
                runtime_features=CAPABLE,
            )
        )
        self.assertFalse(
            can_use_native_promql(
                f"sum by (device)({RX} @ 1603774568) / on(device) sum by (device)({TX})",
                runtime_features=CAPABLE,
            )
        )

    def test_matcher_free_expressions_are_unaffected_by_the_feature(self):
        for expr in ("up", f"rate({RX}[5m])", f"sum({RX}) / sum({TX})"):
            with self.subTest(expr=expr):
                self.assertEqual(
                    can_use_native_promql(expr),
                    can_use_native_promql(expr, runtime_features=CAPABLE),
                )

    def test_a_label_value_containing_the_matcher_token_is_not_treated_as_syntax(self):
        expr = f'sum by (device) ({RX}{{job="ignoring(x)"}})'
        self.assertTrue(can_use_native_promql(expr))

    def test_a_comment_between_the_matcher_and_its_parenthesis_does_not_hide_it(self):
        # PromQL allows a comment there, so ``on # note\n(device)`` parses as
        # ``on(device)``. The gate must see it: otherwise the expression skips
        # both halves and an incapable target gets a native query it answers with
        # a hard error instead of an ES|QL panel.
        for matcher in ("on", "ignoring"):
            expr = (
                f"sum by (device) ({RX}) / {matcher} # which labels\n"
                f"(device) sum by (device) ({TX})"
            )
            with self.subTest(matcher=matcher):
                self.assertFalse(can_use_native_promql(expr, runtime_features={}))
                # A capable target declines too, and that is the safe answer:
                # ``_clean_promql_for_native`` flattens the expression to one
                # line, which would fold the operand after the comment into it.
                # The shape check runs on that flattened text and reports an
                # indeterminate operand, so the panel keeps ES|QL either way.
                self.assertFalse(can_use_native_promql(expr, runtime_features=CAPABLE))

    def test_a_comment_does_not_hide_an_unconditionally_blocked_construct(self):
        # Same root cause, other gate: the sanitizer feeds every structural
        # check, so a commented-out parenthesis must not smuggle ``topk`` past
        # ``_PROMQL_UNSUPPORTED_RE`` either.
        expr = f"sum by (device) ({RX}) / on(device) topk # keep\n(5, {TX})"
        self.assertFalse(can_use_native_promql(expr, runtime_features=CAPABLE))

    def test_a_hash_inside_a_label_value_is_not_read_as_a_comment(self):
        # Comment stripping tracks quote state, so the closing brace survives and
        # the matcher after it is still seen. Backticks count: they are PromQL raw
        # strings, and blanking only ''/"" would truncate the expression here and
        # let an incapable target through.
        for quote in ('"', "'", "`"):
            expr = (
                f"sum by (device) ({RX}{{job={quote}a#b{quote}}})"
                f" / on(device) sum by (device) ({TX})"
            )
            with self.subTest(quote=quote):
                self.assertFalse(can_use_native_promql(expr, runtime_features={}))
                self.assertTrue(can_use_native_promql(expr, runtime_features=CAPABLE))

    def test_comment_handling_does_not_weaken_the_flattening_based_gates(self):
        # ``_promql_has_known_server_bug`` and ``_promql_has_unsupported_comparison``
        # scan ``_clean_promql_for_native`` output, where newlines are already gone
        # and a comment's extent is no longer knowable. Stripping comments there
        # would eat the operand after the newline and hide the construct, so those
        # gates keep scanning the flattened text with comments intact.
        for expr in (
            f"sum({RX}) # note\nor sum({TX})",
            f"sum({RX}) # note\nand sum({TX})",
            f"sum({RX}) # note\nunless sum({TX})",
            f"{RX} # note\n== {TX}",
        ):
            with self.subTest(expr=expr):
                self.assertFalse(can_use_native_promql(expr, runtime_features={}))
                self.assertFalse(can_use_native_promql(expr, runtime_features=CAPABLE))


class CapabilityProbeTests(unittest.TestCase):
    """The probe decides the target half, and must fail closed."""

    def _probe(self, **response):
        from observability_migration.adapters.source.grafana.cli import (
            _detect_promql_vector_matching,
        )

        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.requests.post",
            return_value=SimpleNamespace(json=lambda: {}, text="", **response),
        ):
            return _detect_promql_vector_matching("https://es.example", "apikey")

    def test_probe_expression_exercises_every_gated_construct(self):
        from observability_migration.adapters.source.grafana.cli import (
            _PROMQL_VECTOR_MATCHING_PROBE,
        )

        for token in ("on(", "ignoring(", "group_left("):
            with self.subTest(token=token):
                self.assertIn(token, _PROMQL_VECTOR_MATCHING_PROBE)
        # Every operand is ``vector(1)``, so the probe needs no index and no
        # data and can never be refused for an indeterminate operand. A probe
        # over metric selectors would report "unsupported" on a capable target.
        value = _PROMQL_VECTOR_MATCHING_PROBE.split("value=(", 1)[1]
        self.assertEqual(value.count("vector(1)"), 3)
        self.assertNotIn("{", value)

    def test_http_200_enables_the_feature(self):
        state = self._probe(status_code=200)
        self.assertTrue(state["supported"])
        self.assertEqual(state["confidence"], "verified")

    def test_http_400_is_a_verified_rejection(self):
        # What Elasticsearch 9.5.0 answers.
        state = self._probe(status_code=400)
        self.assertFalse(state["supported"])
        self.assertEqual(state["confidence"], "verified")

    def test_auth_error_is_inconclusive_and_unsupported(self):
        for status in (401, 403):
            with self.subTest(status=status):
                state = self._probe(status_code=status)
                self.assertFalse(state["supported"])
                self.assertEqual(state["confidence"], "inconclusive")

    def test_transient_failure_is_inconclusive_and_unsupported(self):
        from observability_migration.adapters.source.grafana.cli import (
            _detect_promql_vector_matching,
        )

        state = self._probe(status_code=503)
        self.assertFalse(state["supported"])
        self.assertEqual(state["confidence"], "inconclusive")

        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.requests.post",
            side_effect=ConnectionError("network"),
        ):
            state = _detect_promql_vector_matching("https://es.example", "apikey")
        self.assertFalse(state["supported"])
        self.assertEqual(state["confidence"], "inconclusive")

        self.assertEqual(_detect_promql_vector_matching(""), {})

    def test_runtime_profile_records_the_feature(self):
        from observability_migration.adapters.source.grafana.cli import (
            _detect_target_runtime_features,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            is_feature_supported,
        )

        for probe_state, expected in (
            ({"supported": True, "source": "probe"}, True),
            ({"supported": False, "source": "probe"}, False),
        ):
            with self.subTest(supported=expected):
                with (
                    mock.patch(
                        "observability_migration.adapters.source.grafana.cli."
                        "_detect_promql_support",
                        return_value=True,
                    ),
                    mock.patch(
                        "observability_migration.adapters.source.grafana.cli."
                        "_detect_es_version",
                        return_value=(9, 6),
                    ),
                    mock.patch(
                        "observability_migration.adapters.source.grafana.cli."
                        "_detect_promql_vector_matching",
                        return_value=probe_state,
                    ),
                    mock.patch(
                        "observability_migration.adapters.source.grafana.cli.requests.get",
                        return_value=SimpleNamespace(
                            status_code=200, json=lambda: {"nodes": {}}, text=""
                        ),
                    ),
                    mock.patch(
                        "observability_migration.adapters.source.grafana.cli.requests.post"
                    ),
                ):
                    profile = _detect_target_runtime_features("https://es.example", "key")
                self.assertEqual(
                    is_feature_supported(profile, PROMQL_VECTOR_MATCHING), expected
                )

    def test_feature_is_absent_when_the_promql_command_itself_is_unavailable(self):
        from observability_migration.adapters.source.grafana.cli import (
            _detect_target_runtime_features,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            is_feature_supported,
        )

        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_promql_support",
            return_value=False,
        ):
            profile = _detect_target_runtime_features("https://es.example", "key")

        self.assertFalse(is_feature_supported(profile, PROMQL_VECTOR_MATCHING))


def _matcher_panel(idx, expr, title):
    return {
        "id": idx,
        "type": "timeseries",
        "title": title,
        "targets": [{"expr": expr, "refId": "A", "datasource": {"type": "prometheus"}}],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "gridPos": {"x": 0, "y": idx * 8, "w": 24, "h": 8},
    }


class PanelEmissionTests(unittest.TestCase):
    """End-to-end: the issue's dashboard, on a capable and an incapable target."""

    # The issue's four panels, with the verdict each one must get on a capable
    # target.
    PANELS = [
        (1, "rate(node_cpu_seconds_total[5m])", "Control (no matcher)", True),
        (
            2,
            'sum by (instance) (rate(node_cpu_seconds_total{mode="user"}[5m]))'
            " / on(instance) sum by (instance) (rate(node_cpu_seconds_total[5m]))",
            "on() aligned ratio",
            True,
        ),
        (
            3,
            "sum(rate(node_network_receive_bytes_total[5m]))"
            " / ignoring(device) sum(rate(node_network_transmit_bytes_total[5m]))",
            "ignoring()",
            True,
        ),
        (
            4,
            "node_hwmon_temp_celsius * on(chip) group_left(chip_name)"
            " node_hwmon_chip_names",
            "on() + group_left",
            False,
        ),
    ]

    def _dashboard(self):
        return {
            "title": "PromQL on/ignoring eval",
            "uid": "on-ignoring-eval",
            "panels": [_matcher_panel(i, expr, title) for i, expr, title, _ in self.PANELS],
        }

    def _translate(self, *, capable):
        rule_pack = rules.RulePackConfig()
        rule_pack.native_promql = True
        if capable:
            from observability_migration.adapters.source.grafana.runtime_features import (
                set_runtime_feature,
            )

            set_runtime_feature(
                rule_pack, PROMQL_VECTOR_MATCHING, supported=True, source="test"
            )
        resolver = schema.SchemaResolver(rule_pack)
        result = panels.translate_dashboard(
            self._dashboard(),
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        return result, result.dashboard_ir.to_yaml_dict()

    def test_capable_target_emits_native_promql_for_plannable_matchers(self):
        _result, dash = self._translate(capable=True)
        for (_idx, _expr, title, expect_native), panel in zip(
            self.PANELS, dash["panels"], strict=True
        ):
            query = panel["esql"]["query"]
            with self.subTest(panel=title):
                if expect_native:
                    self.assertTrue(
                        query.lstrip().startswith("PROMQL index="),
                        f"{title} should be native, got: {query[:120]}",
                    )
                else:
                    self.assertNotIn("PROMQL index=", query)

    def test_matchers_survive_verbatim_in_the_native_query(self):
        _result, dash = self._translate(capable=True)
        ratio = dash["panels"][1]["esql"]["query"]
        self.assertIn("on(instance)", ratio)
        self.assertIn("sum by (instance)", ratio)
        ignoring = dash["panels"][2]["esql"]["query"]
        self.assertIn("ignoring(device)", ignoring)

    def test_incapable_target_keeps_every_matcher_panel_on_esql(self):
        _result, dash = self._translate(capable=False)
        for (_idx, _expr, title, _), panel in zip(
            self.PANELS[1:], dash["panels"][1:], strict=True
        ):
            with self.subTest(panel=title):
                self.assertNotIn("PROMQL index=", panel["esql"]["query"])
        # The matcher-free control panel is native either way.
        self.assertTrue(dash["panels"][0]["esql"]["query"].startswith("PROMQL index="))

    def test_skipped_panels_say_which_half_of_the_gate_declined(self):
        capable, _ = self._translate(capable=True)
        incapable, _ = self._translate(capable=False)

        def notes_for(result, title):
            for panel in result.panel_results:
                if panel.title == title:
                    return " ".join(panel.notes or [])
            raise AssertionError(f"no panel titled {title!r}")

        self.assertIn(
            "determine statically", notes_for(capable, "on() + group_left")
        )
        self.assertIn(
            "does not evaluate PromQL vector matching",
            notes_for(incapable, "on() aligned ratio"),
        )

    def test_no_matching_note_when_something_else_blocked_the_panel(self):
        # A matcher panel can fail native emission for an unrelated reason — here
        # both operands are plannable ``sum by`` aggregations, but ``topk`` is
        # blocked outright. Telling this operator to reshape their operands would
        # point them at the wrong fix, so the note stays silent.
        expr = f"sum by (device) ({RX}) / on(device) sum by (device) (topk(5, {TX}))"
        self.assertTrue(panels._promql_vector_matching_is_native_feasible(expr))
        self.assertFalse(can_use_native_promql(expr, runtime_features=CAPABLE))
        self.assertEqual(panels._native_vector_matching_skip_note(expr, CAPABLE), "")

    def test_no_matching_note_for_a_matcher_free_expression(self):
        self.assertEqual(
            panels._native_vector_matching_skip_note(f"sum({RX})", {}), ""
        )

    def test_inconclusive_probe_does_not_claim_the_target_lacks_the_feature(self):
        note = panels._native_vector_matching_skip_note(
            f"sum by (device) ({RX}) / on(device) sum by (device) ({TX})",
            {
                PROMQL_VECTOR_MATCHING: {
                    "supported": False,
                    "confidence": "inconclusive",
                    "reason": "target probe skipped due to auth error",
                }
            },
        )
        self.assertIn("could not verify", note)
        self.assertIn("auth error", note)
        self.assertNotIn("does not evaluate", note)

    def test_note_tolerates_a_bare_bool_feature_value(self):
        # Rule packs and tests set features as plain bools, not probe-state dicts.
        expr = f"sum by (device) ({RX}) / on(device) sum by (device) ({TX})"
        for value in (False, True):
            with self.subTest(value=value):
                note = panels._native_vector_matching_skip_note(
                    expr, {PROMQL_VECTOR_MATCHING: value}
                )
                self.assertNotIn("could not verify", note)
        self.assertEqual(
            panels._native_vector_matching_skip_note(
                expr, {PROMQL_VECTOR_MATCHING: True}
            ),
            "",
        )

    def test_native_matcher_panels_raise_the_artifact_version_floor(self):
        _result, dash = self._translate(capable=True)
        self.assertEqual(dash["minimum_kibana_version"], "9.6.0")

        _result, dash = self._translate(capable=False)
        self.assertEqual(dash["minimum_kibana_version"], panels.MINIMUM_KIBANA_VERSION)

    def test_version_floor_ignores_matcher_tokens_outside_native_promql(self):
        esql = {
            "esql": {
                "query": 'FROM metrics-* | EVAL label = "group_left" | KEEP label'
            }
        }
        literal = {
            "esql": {
                "query": 'PROMQL index=metrics-* step=60s value=(up{job="on(x)"})'
            }
        }
        for panel in (esql, literal):
            self.assertEqual(
                panels._dashboard_minimum_kibana_version([panel]),
                panels.MINIMUM_KIBANA_VERSION,
            )


class AlertRoutingTests(unittest.TestCase):
    """Alerts keep their pre-issue routing.

    ``_generate_esql_for_alert`` consults ``can_use_native_promql`` without a
    runtime-feature profile (the same reason native ``histogram_quantile`` never
    reaches alerts), so an alert on a matched expression is unchanged by #440. It
    is pinned because the shared gate now has a path that *can* return True for
    these expressions.
    """

    def test_matched_expression_alert_does_not_become_native(self):
        expr = (
            "sum by (instance) (rate(node_cpu_seconds_total[5m]))"
            " / on(instance) sum by (instance) (rate(node_cpu_seconds_total[5m])) < 0.1"
        )
        self.assertFalse(can_use_native_promql(expr))


if __name__ == "__main__":
    unittest.main()
