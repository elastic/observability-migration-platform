# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for GitHub issue #443.

A PromQL ``#`` comment ends at its newline. ``_clean_promql_for_native``
collapsed newlines to spaces *before* anything removed comments, so the comment
lost its terminator and swallowed the rest of the expression. The truncated text
was then what got emitted as the native ``PROMQL`` query, and what every gate
that reads the cleaned text analysed.

That single root cause produced two opposite failures:

1. **Truncated emission.** ``sum(rate(a[5m])) # note\\n+ sum(rate(b[5m]))``
   reached Elasticsearch as ``sum(rate(a[5m]))`` — a valid query computing the
   wrong thing, still scored ``migrated`` with no warning. Where the remainder
   was left unparseable it hard-errored in Kibana instead.
2. **Comment prose read as query structure.** The routing gates scanned text
   that still contained the comment, so prose words decided whether a panel
   could go native: ``# or maybe ...`` tripped the ``or`` server-bug gate,
   ``# try histogram_quantile(...)`` tripped the histogram gate, and so on —
   each needlessly degrading a perfectly native-able panel to ES|QL. In the
   other direction a comment *hid* a real construct: it truncated the text the
   issue-#376 vector-matching guard parses, so a distinct-metric element-wise
   ratio slipped onto the native path the guard exists to refuse.

The same omission affected the ES|QL path, whose cleaner
(``preprocess_grafana_macros``) also kept comments: the complexity classifier
scans its output, so a comment merely *mentioning* ``predict_linear`` reported
"predict_linear has no ES|QL equivalent" against a plain ``sum(rate(...))``.

The fix strips comments quote-aware in both cleaners, before newlines are
flattened, so the emitted query and every gate see the expression Prometheus
itself would evaluate. Quote state must be tracked across all three PromQL
string forms (double, single, and backquoted raw) because a ``#`` inside a label
value is data, not a comment — verified against ``promql-parser``, which parses
``foo{path=`/a#b`}`` as the label value ``/a#b``.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana import panels
from observability_migration.adapters.source.grafana.panels import (
    _clean_promql_for_native,
    _promql_has_known_server_bug,
    _promql_has_unmatchable_vector_match,
    build_native_promql_query,
    can_use_native_promql,
)
from observability_migration.adapters.source.grafana.promql import (
    _strip_promql_comments,
    classify_promql_complexity,
    preprocess_grafana_macros,
)
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver

# The expression from the issue report: two aggregated rate terms joined across
# a line break, with a comment on the first line.
REPORTED_EXPR = (
    "sum(rate(node_cpu_seconds_total[5m])) # cpu rate\n"
    "+ sum(rate(node_disk_reads_completed_total[5m]))"
)


class StripPromqlCommentsTests(unittest.TestCase):
    """The helper implements the Prometheus lexer rule: an unquoted ``#`` runs
    to end of line, and a ``#`` inside a string literal is data."""

    def test_trailing_comment_is_removed_and_newline_kept(self):
        # The newline must survive: it is what keeps the two operands apart
        # once the caller collapses whitespace.
        self.assertEqual(
            _strip_promql_comments("sum(foo) # note\n+ sum(bar)"),
            "sum(foo) \n+ sum(bar)",
        )

    def test_leading_comment_line_is_removed(self):
        self.assertEqual(
            _strip_promql_comments("# leading note\nsum(foo)"),
            "\nsum(foo)",
        )

    def test_comment_without_trailing_newline_is_removed(self):
        self.assertEqual(_strip_promql_comments("sum(foo) # tail"), "sum(foo) ")

    def test_comment_on_every_line_is_removed(self):
        self.assertEqual(
            _strip_promql_comments("sum(foo) # a\n+ sum(bar) # b\n+ sum(baz)"),
            "sum(foo) \n+ sum(bar) \n+ sum(baz)",
        )

    def test_hash_inside_double_quoted_value_is_data(self):
        expr = 'foo{path="/a#b"} + bar'
        self.assertEqual(_strip_promql_comments(expr), expr)

    def test_hash_inside_single_quoted_value_is_data(self):
        expr = "foo{path='/a#b'} + bar"
        self.assertEqual(_strip_promql_comments(expr), expr)

    def test_hash_inside_backquoted_raw_string_is_data(self):
        # PromQL's third string form. ``_strip_promql_string_literals`` does not
        # know it, so the comment scan must track it itself or a raw label value
        # containing ``#`` would be truncated as if it were a comment.
        expr = "foo{path=`/a#b`} + bar"
        self.assertEqual(_strip_promql_comments(expr), expr)

    def test_comment_after_a_closed_string_is_still_a_comment(self):
        self.assertEqual(
            _strip_promql_comments('foo{path="/a#b"} # note\n+ bar'),
            'foo{path="/a#b"} \n+ bar',
        )

    def test_escaped_quote_does_not_end_the_string_early(self):
        # Without escape handling the scan would leave string state at the
        # escaped quote and then delete the real label value as a "comment".
        expr = 'foo{msg="say \\"#1\\""} + bar'
        self.assertEqual(_strip_promql_comments(expr), expr)

    def test_expression_without_a_hash_is_returned_unchanged(self):
        expr = 'sum by (job) (rate(http_requests_total{env="prod"}[5m]))'
        self.assertIs(_strip_promql_comments(expr), expr)

    def test_comment_only_expression_becomes_blank(self):
        self.assertEqual(_strip_promql_comments("# just a note").strip(), "")

    def test_is_idempotent(self):
        once = _strip_promql_comments(REPORTED_EXPR)
        self.assertEqual(_strip_promql_comments(once), once)

    def test_blank_and_none_are_tolerated(self):
        self.assertEqual(_strip_promql_comments(""), "")
        self.assertEqual(_strip_promql_comments(None), "")

    def test_crlf_comment_keeps_the_newline(self):
        # The ``\\r`` belongs to the comment's line, so it goes with the
        # comment; the ``\\n`` must stay to keep the operands apart.
        self.assertEqual(
            _strip_promql_comments("sum(foo) # note\r\n+ sum(bar)"),
            "sum(foo) \n+ sum(bar)",
        )

    def test_unterminated_string_does_not_delete_the_remainder(self):
        # Already-invalid PromQL. The scan stays in string state, which errs
        # toward keeping text rather than silently truncating it.
        expr = 'foo{path="unterminated # not a comment'
        self.assertEqual(_strip_promql_comments(expr), expr)

    def test_trailing_backslash_does_not_drop_characters(self):
        expr = 'foo{path="a\\'
        self.assertEqual(_strip_promql_comments(expr), expr)

    def test_quote_of_another_form_inside_a_string_is_data(self):
        # The inner ``'`` and ``\u0060`` must not be read as quote delimiters, or the
        # scan would leave string state early and treat the value's ``#`` as a
        # comment.
        expr = "foo{msg=\"it's a `raw` #1\"} + bar"
        self.assertEqual(_strip_promql_comments(expr), expr)

    def test_hash_inside_a_comment_is_part_of_the_comment(self):
        self.assertEqual(
            _strip_promql_comments("sum(foo) ## double hash\n+ sum(bar)"),
            "sum(foo) \n+ sum(bar)",
        )

    def test_quote_inside_a_comment_does_not_open_string_state(self):
        # An apostrophe in prose would otherwise swallow the next line as a
        # string literal and keep the comment text.
        self.assertEqual(
            _strip_promql_comments("sum(foo) # don't use bar\n+ sum(bar)"),
            "sum(foo) \n+ sum(bar)",
        )

    def test_escaped_quote_inside_a_backquoted_string_is_handled(self):
        expr = "foo{path=`a\\`b#c`} + bar"
        self.assertEqual(_strip_promql_comments(expr), expr)


class NativeEmissionTests(unittest.TestCase):
    """The emitted native query must carry the whole expression."""

    def test_reported_expression_keeps_both_operands(self):
        cleaned = _clean_promql_for_native(REPORTED_EXPR)
        self.assertEqual(
            cleaned,
            "sum(rate(node_cpu_seconds_total[5m])) "
            "+ sum(rate(node_disk_reads_completed_total[5m]))",
        )

    def test_cleaned_output_never_carries_a_comment_marker(self):
        # A ``#`` surviving into the emitted text is the bug: Elasticsearch
        # would discard everything after it.
        self.assertNotIn("#", _clean_promql_for_native(REPORTED_EXPR))

    def test_emitted_promql_command_contains_both_metrics(self):
        query = build_native_promql_query(
            REPORTED_EXPR, index="metrics-prometheus-*", kibana_type="line"
        )
        self.assertNotIn("#", query)
        self.assertIn("node_cpu_seconds_total", query)
        self.assertIn("node_disk_reads_completed_total", query)

    def test_hash_in_a_label_value_survives_cleaning(self):
        cleaned = _clean_promql_for_native('sum(foo{path="/a#b"})')
        self.assertEqual(cleaned, 'sum(foo{path="/a#b"})')

    def test_leading_comment_line_does_not_reach_the_emitted_query(self):
        cleaned = _clean_promql_for_native("# rate of requests\nsum(rate(foo_total[5m]))")
        self.assertEqual(cleaned, "sum(rate(foo_total[5m]))")


class RoutingGateCommentBlindnessTests(unittest.TestCase):
    """Comment prose must not decide whether a panel can go native.

    Each expression below is a plain aggregated rate that the native path
    supports. Only the comment text differs, and on the unfixed code each one
    tripped a different gate and pushed the panel onto the ES|QL fallback.
    """

    def _assert_native(self, comment: str):
        expr = f"sum(rate(foo_total[5m])) # {comment}"
        self.assertTrue(
            can_use_native_promql(expr),
            msg=f"comment {comment!r} must not block native emission",
        )

    def test_comment_mentioning_or(self):
        self._assert_native("or maybe use something else")

    def test_comment_mentioning_and(self):
        self._assert_native("cpu and memory")

    def test_comment_mentioning_unless(self):
        self._assert_native("unless the exporter is down")

    def test_comment_mentioning_histogram_quantile(self):
        self._assert_native("try histogram_quantile(0.9, x) here")

    def test_comment_mentioning_topk(self):
        self._assert_native("consider topk(5, foo)")

    def test_comment_mentioning_an_empty_selector(self):
        self._assert_native("note {}")

    def test_comment_mentioning_a_template_variable(self):
        self._assert_native('was job=~"$job"')

    def test_comment_mentioning_a_range_after_a_paren(self):
        self._assert_native("see rate(bar) [5m]")

    def test_server_bug_gate_ignores_comment_prose(self):
        self.assertFalse(
            _promql_has_known_server_bug("sum(rate(foo_total[5m])) # or maybe")
        )


class RealConstructsStillRejectedTests(unittest.TestCase):
    """Making the gates comment-blind must not make them expression-blind."""

    def test_real_or_is_still_rejected(self):
        self.assertFalse(can_use_native_promql("foo or bar"))

    def test_real_and_is_still_rejected(self):
        self.assertFalse(can_use_native_promql("foo and bar > 0"))

    def test_real_unless_is_still_rejected(self):
        self.assertFalse(can_use_native_promql("foo unless bar"))

    def test_real_topk_is_still_rejected(self):
        self.assertFalse(can_use_native_promql("topk(5, http_requests_total)"))

    def test_real_empty_metricless_selector_is_still_rejected(self):
        self.assertFalse(can_use_native_promql("{}"))

    def test_real_nested_aggregation_is_still_rejected(self):
        self.assertFalse(can_use_native_promql("count(count(node_cpu) by (cpu))"))

    def test_real_nested_comparison_is_still_rejected(self):
        self.assertFalse(can_use_native_promql("count(up == 1)"))

    def test_real_range_on_nonselector_is_still_rejected(self):
        self.assertFalse(
            can_use_native_promql("irate(sum by (vhost) (rabbitmq_queue_messages_ready)[5m])")
        )

    def test_real_construct_behind_a_comment_is_still_rejected(self):
        # The construct is on the *second* line, so stripping the first line's
        # comment must not take the expression with it.
        self.assertFalse(can_use_native_promql("sum(foo) # note\nor sum(bar)"))

    def test_real_template_variable_matcher_is_still_rejected(self):
        self.assertFalse(can_use_native_promql('cpu{host="$host"} # host cpu'))


class VectorMatchingGuardTests(unittest.TestCase):
    """A comment must not hide a construct a correctness guard exists to catch.

    Elasticsearch keeps ``__name__`` in the implicit vector-matching key, so
    ``A / B`` over distinct metric names returns zero rows (issue #376) and must
    degrade to ES|QL. The guard parses the *cleaned* text, which a comment used
    to truncate down to ``A`` alone — a shape the guard happily accepts.
    """

    PLAIN = "node_ready_total / node_total"
    COMMENTED = "node_ready_total # ready\n/ node_total"

    def test_plain_ratio_is_refused(self):
        self.assertTrue(_promql_has_unmatchable_vector_match(self.PLAIN))
        self.assertFalse(can_use_native_promql(self.PLAIN))

    def test_commented_ratio_is_refused_identically(self):
        self.assertTrue(_promql_has_unmatchable_vector_match(self.COMMENTED))
        self.assertFalse(can_use_native_promql(self.COMMENTED))

    def test_comment_does_not_change_the_routing_decision(self):
        self.assertEqual(
            can_use_native_promql(self.PLAIN),
            can_use_native_promql(self.COMMENTED),
        )


class EsqlPathCommentBlindnessTests(unittest.TestCase):
    """The ES|QL path cleans through ``preprocess_grafana_macros``, whose result
    is what the complexity classifier and the rule pack's warning patterns
    scan. It has the same obligation as the native cleaner: a comment is not
    query structure.

    This is the path a panel *lands on* when the native gates decline it, so it
    matters more after the routing fix above, not less.
    """

    def test_macro_preprocessing_drops_comments(self):
        cleaned = preprocess_grafana_macros(
            "sum(rate(foo_total[5m])) # cpu rate\n+ sum(rate(bar_total[5m]))"
        )
        self.assertNotIn("#", cleaned)
        self.assertIn("bar_total", cleaned)

    def test_macro_preprocessing_keeps_a_hash_in_a_label_value(self):
        self.assertIn('path="/a#b"', preprocess_grafana_macros('foo{path="/a#b"}'))

    def test_comment_does_not_invent_an_unsupported_function_warning(self):
        # A comment mentioning an untranslatable function used to be reported as
        # if the expression called it.
        plain = classify_promql_complexity("sum(rate(foo_total[5m]))", None)
        commented = classify_promql_complexity(
            "sum(rate(foo_total[5m])) # predict_linear(x, 1h) would be better", None
        )
        self.assertEqual(commented, plain)

    def test_real_unsupported_function_is_still_reported(self):
        complexity, reason = classify_promql_complexity(
            "predict_linear(foo_total[1h], 3600)", None
        )
        self.assertNotEqual(complexity, "feasible")
        self.assertIn("predict_linear", reason)


class OuterNativeRoutingTests(unittest.TestCase):
    """The panel-level native path must be comment-blind too.

    ``can_use_native_promql`` is not the only gate. ``translate_panel`` runs
    several structural checks on the raw target expression *before* reaching
    it — or-collapsing, the empty-selector guard, live metric discovery, and
    the rule-pack label-override check. Each read comment prose as query text
    and declined native emission with a note describing something the query
    never did, so a panel could still be pushed onto the ES|QL fallback by a
    comment even with the routing gate itself fixed.

    These go through ``translate_panel`` rather than the private helpers
    because the bug was in *what text the caller handed them*, which only the
    real call path exercises.
    """

    PLAIN = "sum(rate(node_cpu_seconds_total[5m]))"
    FIELDS = {
        "node_cpu_seconds_total": {
            "double": {"aggregatable": True, "time_series_metric": "counter"}
        }
    }

    def _translate(self, expr, *, passthrough=False, label_rewrites=None):
        """Translate a one-target timeseries panel with live discovery enabled."""
        rule_pack = RulePackConfig()
        rule_pack.native_promql = True
        rule_pack.runtime_features = {
            "promql_command_v0": {"supported": True, "confidence": "verified"}
        }
        if label_rewrites:
            rule_pack.label_rewrites = dict(label_rewrites)
        resolver = SchemaResolver(
            rule_pack,
            es_url="https://es",
            index_pattern="metrics-*",
            field_profile="passthrough" if passthrough else None,
        )
        # Stand in for a successful ``_field_caps`` fetch so the live-missing
        # gate is active: without status "ok" it returns early and the bug
        # this class covers is invisible.
        resolver._discovery_attempted = True
        resolver._field_cache = dict(self.FIELDS)
        resolver._discovery_status = "ok"
        panel = {
            "type": "timeseries",
            "title": "CPU",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"refId": "A", "expr": expr}],
        }
        _yaml_panel, result = panels.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        return result.esql_query or ""

    def _assert_native(self, query):
        self.assertIn("PROMQL", query)
        self.assertNotIn("#", query)
        self.assertIn("node_cpu_seconds_total", query)

    def test_plain_expression_routes_native(self):
        # Control: without a comment this panel goes native, so the assertions
        # below isolate the comment as the only difference.
        self._assert_native(self._translate(self.PLAIN))

    def test_comment_naming_an_absent_metric_does_not_block_native(self):
        # ``_metrics_in_expr`` treated the comment's words as metric names, so
        # live field-caps discovery reported them missing and the panel
        # degraded — for a metric the query never selects.
        self._assert_native(
            self._translate(f"{self.PLAIN} # maybe use missing_metric_total instead")
        )

    def test_comment_containing_an_empty_selector_does_not_block_native(self):
        # The empty-metricless-selector guard (PR #369) scans for ``{}`` after
        # stripping string literals, which left comment text in place.
        self._assert_native(self._translate(f"{self.PLAIN} # note {{}}"))

    def test_comment_containing_an_overridden_label_does_not_block_native(self):
        # The rule pack rewrites ``pod``, which native emission cannot honour,
        # so a query using it must degrade. Merely *mentioning* a ``pod``
        # matcher in prose made the override check degrade the panel anyway.
        self._assert_native(
            self._translate(
                f'{self.PLAIN} # was node_cpu_seconds_total{{pod="x"}} before',
                passthrough=True,
                label_rewrites={"pod": "k8s.pod.name"},
            )
        )

    def test_real_overridden_label_still_degrades(self):
        # The mirror image: an actual rewritten-label matcher must still
        # decline native, or this fix would have traded a false degrade for a
        # query that silently ignores the rule pack's field mapping.
        query = self._translate(
            'sum(rate(node_cpu_seconds_total{pod="x"}[5m]))',
            passthrough=True,
            label_rewrites={"pod": "k8s.pod.name"},
        )
        self.assertNotIn("PROMQL", query)
        self.assertIn("k8s.pod.name", query)


class SourceProvenanceTests(unittest.TestCase):
    """Stripping comments for structure must not rewrite the recorded source.

    ``query_ir.source_expression`` and ``PanelResult.promql_expr`` are
    provenance: the report prints the latter as "Original query" and the
    side-by-side comparison sends it to Prometheus as the source query. An
    operator cross-references that text against their own Grafana panel, so it
    has to stay what they authored — comment included — even though every
    structural pass in between must not see the comment.

    ``tests/test_migrate.py`` already asserts this contract for an
    uncommented expression (``test_query_ir_source_expression_is_original_promql``);
    these pin it for the case the fix could have broken.
    """

    EXPR = "sum(rate(node_cpu_seconds_total[5m])) # cpu rate, keep an eye on this"

    def _translate(self, *, native):
        rule_pack = RulePackConfig()
        rule_pack.native_promql = native
        if native:
            rule_pack.runtime_features = {
                "promql_command_v0": {"supported": True, "confidence": "verified"}
            }
        resolver = SchemaResolver(rule_pack)
        panel = {
            "type": "timeseries",
            "title": "CPU",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"refId": "A", "expr": self.EXPR}],
        }
        _yaml_panel, result = panels.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        return result

    def test_native_path_records_the_authored_expression(self):
        result = self._translate(native=True)
        # Precondition: this really is the native path, so the emitted query
        # went through the comment strip.
        self.assertIn("PROMQL", result.esql_query or "")
        self.assertNotIn("#", result.esql_query or "")
        self.assertEqual(result.promql_expr, self.EXPR)
        self.assertEqual(result.query_ir.get("source_expression"), self.EXPR)

    def test_native_path_clean_expression_is_still_comment_free(self):
        # The cleaned counterpart is structure and must not keep the comment,
        # or the two fields would be indistinguishable.
        result = self._translate(native=True)
        self.assertNotIn("#", result.query_ir.get("clean_expression") or "")

    def test_esql_path_records_the_authored_expression_identically(self):
        # The ES|QL path already behaved this way; asserting both keeps the
        # two paths from drifting apart on what "original" means.
        result = self._translate(native=False)
        self.assertEqual(result.promql_expr, self.EXPR)
        self.assertEqual(result.query_ir.get("source_expression"), self.EXPR)

    def test_both_paths_agree_on_the_recorded_source(self):
        self.assertEqual(
            self._translate(native=True).promql_expr,
            self._translate(native=False).promql_expr,
        )


class CommentOnlyExpressionTests(unittest.TestCase):
    """An expression that is nothing but a comment has no query to emit."""

    def test_comment_only_expression_is_not_native(self):
        # Previously accepted, emitting ``value=(# note)`` — a parse error in
        # Kibana rather than an honest degrade.
        self.assertFalse(can_use_native_promql("# just a note"))

    def test_comment_only_multiline_expression_is_not_native(self):
        self.assertFalse(can_use_native_promql("# note one\n# note two\n"))


if __name__ == "__main__":
    unittest.main()
