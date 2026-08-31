# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for GitHub issue #382.

A nested aggregation whose inner aggregation has no ``by()`` — ``max(sum(m))`` —
substituted the panel's display hints into the *inner* grouping. That is not a
cosmetic difference: in a nested aggregation the inner grouping decides what the
outer aggregation reduces over, so ``max(sum(m))`` reduced one sum *per label
value* and reported the largest single value instead of the collapsed total. Two
namespaces summing to 3 and 5 gave 8 in Grafana and 5 in Kibana, and the panel
was reported ``feasible`` with no warning.

The invariant these tests lock: a nested aggregation's grouping comes from the
source expression's own ``by()`` clauses and nothing else. Display hints
(``legendFormat`` tokens, legacy table column patterns, dashboard-wide label
inference) are series aliases for the query's *output*; they must never change
what it computes. The sibling ``count(count by (...) (m))`` branch had the same
defect on its *outer* grouping, where ``COUNT_DISTINCT(cpu) BY cpu`` collapsed
the CPU count to a constant 1; ``NestedCountCountTests`` covers the direct
emitter and ``FusedMeasureSpecTests`` the multi-target measure-spec path.

``LabelReplaceOverNestedAggTests`` covers the one caller that *relied* on the
leak: ``label_replace`` asks for its source label by appending it to
``preferred_group_labels``. Closing the leak means that request can no longer be
granted for a nested aggregation, so those forms fail closed instead of emitting
an ``EVAL`` over a column the query already aggregated away.
"""

from __future__ import annotations

import re
import unittest

from observability_migration.adapters.source.grafana import panels, rules, schema

_KEYWORD = {"keyword": {"type": "keyword", "searchable": True, "aggregatable": True}}
_DOUBLE = {"double": {"type": "double", "aggregatable": True}}

# Proven target fields, so a legend-derived label would clear every "is this a
# real dimension?" guard the flat paths apply. The bug was never gated on
# schema discovery (see ``test_offline_run_gains_no_inner_grouping_either``),
# but seeding makes the emitted field names concrete.
_FIELDS = {
    "metrics.kubelet_volume_stats_used_bytes": _DOUBLE,
    "metrics.node_cpu_seconds_total": _DOUBLE,
    "labels.exported_namespace": _KEYWORD,
    "labels.namespace": _KEYWORD,
    "labels.cpu": _KEYWORD,
}

_USED_BYTES = "kubelet_volume_stats_used_bytes"

# ``node_load1`` gives the binary tests an ungrouped sibling operand to fuse a
# nested COUNT_DISTINCT against.
_BINARY_FIELDS = dict(_FIELDS, **{"metrics.node_load1": _DOUBLE})

# ``BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)`` (FROM) and
# ``BY time_bucket = TBUCKET(20, ?_tstart, ?_tend)`` (TS) both carry commas
# inside the call, so the bucket term is removed before splitting on commas.
_BUCKET_TERM = re.compile(r"time_bucket\s*=\s*T?BUCKET\([^)]*\)")


def _resolver(fields=None):
    rule_pack = rules.RulePackConfig()
    if fields is None:
        return rule_pack, schema.SchemaResolver(rule_pack)
    resolver = schema.SchemaResolver(rule_pack, field_profile="prometheus_native")
    resolver._discovery_attempted = True
    resolver._field_cache = dict(fields)
    resolver._discovered_mappings = {}
    resolver._schema_profile_cache_id = None
    return rule_pack, resolver


def _translate(expr, legend="", panel_type="timeseries", fields=_FIELDS, styles=None):
    """Translate a one-target panel, returning its ``PanelResult``.

    Goes through ``translate_panel`` rather than the translator directly because
    ``preferred_group_labels`` is derived from the panel (legendFormat, legacy
    table styles) — the hint extraction is part of what is under test.
    """
    rule_pack, resolver = _resolver(fields)
    panel = {
        "id": 1,
        "type": panel_type,
        "title": "Volume Used",
        "datasource": {"type": "prometheus", "uid": "p1"},
        "targets": [{"expr": expr, "legendFormat": legend, "refId": "A"}],
    }
    if styles is not None:
        panel["styles"] = styles
    _native, result = panels.translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    return result


def _translate_targets(targets, panel_type="timeseries", fields=_FIELDS):
    """Translate a multi-target panel, which routes through the measure-spec path.

    Fused targets keep a shared ``legendFormat``, so this is the path where the
    hint reaches ``_build_measure_spec`` rather than the direct emitter.
    """
    rule_pack, resolver = _resolver(fields)
    panel = {
        "id": 1,
        "type": panel_type,
        "title": "Volume Used",
        "datasource": {"type": "prometheus", "uid": "p1"},
        "targets": [
            {"expr": expr, "legendFormat": legend, "refId": chr(65 + i)}
            for i, (expr, legend) in enumerate(targets)
        ],
    }
    _native, result = panels.translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    return result


def _stats_lines(query):
    return [line for line in (query or "").splitlines() if line.lstrip().startswith("| STATS")]


def _grouping_dims(stats_line):
    """A ``| STATS`` line's grouping dimensions, excluding the time bucket."""
    if " BY " not in stats_line:
        return []
    by_clause = _BUCKET_TERM.sub("", stats_line.split(" BY ", 1)[1])
    return [part.strip() for part in by_clause.split(",") if part.strip()]


def _measure_aliases(stats_line):
    """The ``alias =`` names a ``| STATS`` line assigns, before its ``BY``."""
    body = stats_line.split(" BY ")[0]
    return re.findall(r"([\w.]+)\s*=", body)


def _inner_grouping(query):
    """Grouping dimensions of the inner (first) ``STATS`` of a nested aggregation."""
    lines = _stats_lines(query)
    assert len(lines) == 2, f"expected a two-stage nested aggregation, got: {query}"
    return _grouping_dims(lines[0])


class LegendMustNotChangeInnerGroupingTests(unittest.TestCase):
    """The reported bug: a legendFormat token became an inner ``by()``."""

    def test_ungrouped_inner_aggregation_stays_ungrouped(self):
        result = _translate(f"max (sum ({_USED_BYTES}))", "{{exported_namespace}}")
        self.assertEqual(_inner_grouping(result.esql_query), [])

    def test_legend_does_not_change_the_query_at_all(self):
        """A legendFormat is a display hint, so it must be a no-op on the query.

        Byte equality against the same expression with no legend is the crispest
        statement of the invariant: whatever the legend says, the arithmetic is
        the source expression's.
        """
        with_legend = _translate(f"max (sum ({_USED_BYTES}))", "{{exported_namespace}}")
        without_legend = _translate(f"max (sum ({_USED_BYTES}))", "")
        self.assertEqual(with_legend.esql_query, without_legend.esql_query)

    def test_outer_aggregation_reduces_over_the_collapsed_total(self):
        """``MAX`` must reduce the single collapsed ``SUM``, not one per label.

        This is the arithmetic the issue is about: grouping the inner ``SUM`` by
        namespace makes ``MAX`` return the largest namespace (5) instead of the
        total (8).
        """
        result = _translate(f"max (sum ({_USED_BYTES}))", "{{exported_namespace}}")
        inner, outer = _stats_lines(result.esql_query)
        self.assertIn(f"inner_val = SUM(metrics.{_USED_BYTES})", inner)
        self.assertEqual(_grouping_dims(inner), [])
        self.assertIn("MAX(inner_val)", outer)

    def test_no_label_is_reported_as_migrated_cleanly(self):
        """Dropping a hint that names no source dimension is not a semantic gap.

        PromQL's ``sum(m)`` emits one label-less series, so there is nothing to
        warn about — matching how the already-correct flat path behaves.
        """
        result = _translate(f"max (sum ({_USED_BYTES}))", "{{exported_namespace}}")
        self.assertEqual(result.status, "migrated")
        self.assertEqual(_inner_grouping(result.esql_query), [])

    def test_legend_leaks_nothing_when_the_outer_has_its_own_by(self):
        """An outer ``by()`` does not license a legend-derived inner grouping.

        The inner aggregation is still ungrouped, so the inner ``SUM`` must stay
        collapsed. (Guards against a provenance check keyed on the *outer*
        grouping, which is non-empty here.)
        """
        result = _translate(
            f"max by (namespace) (sum ({_USED_BYTES}))", "{{exported_namespace}}"
        )
        self.assertEqual(_inner_grouping(result.esql_query), [])

    def test_phantom_legend_token_does_not_become_a_by_column(self):
        """``{{input}}`` names no field, so it must not reach the BY clause.

        The hand-rolled inner grouping skipped the usability filtering the flat
        paths apply, so a placeholder that is not a real dimension was emitted as
        a column and would fail at query time on top of changing the math.
        """
        result = _translate(f"max (sum ({_USED_BYTES}))", "{{input}}")
        self.assertEqual(_inner_grouping(result.esql_query), [])
        self.assertNotIn("input", result.esql_query)

    def test_offline_run_gains_no_inner_grouping_either(self):
        """Offline (no schema discovery) was affected too, and worse.

        With no field caps the label resolved to a bare ``exported_namespace``,
        a column that exists under no profile.
        """
        result = _translate(
            f"max (sum ({_USED_BYTES}))", "{{exported_namespace}}", fields=None
        )
        self.assertEqual(_inner_grouping(result.esql_query), [])
        self.assertNotIn("exported_namespace", result.esql_query)


class ExplicitInnerByIsPreservedTests(unittest.TestCase):
    """Explicit inner ``by()`` labels are semantics, not hints — they stay."""

    def test_inner_by_label_is_honored(self):
        result = _translate(f"max (sum by (exported_namespace) ({_USED_BYTES}))", "")
        self.assertEqual(
            _inner_grouping(result.esql_query), ["labels.exported_namespace"]
        )

    def test_inner_by_wins_over_a_disagreeing_legend(self):
        """The source's grouping decides; the legend cannot add to or replace it."""
        result = _translate(
            f"max (sum by (namespace) ({_USED_BYTES}))", "{{exported_namespace}}"
        )
        self.assertEqual(_inner_grouping(result.esql_query), ["labels.namespace"])

    def test_multi_label_inner_by_is_honored(self):
        result = _translate(
            f"max (sum by (namespace, cpu) ({_USED_BYTES}))", "{{exported_namespace}}"
        )
        self.assertEqual(
            _inner_grouping(result.esql_query), ["labels.namespace", "labels.cpu"]
        )

    def test_inner_by_is_honored_on_the_range_function_branch(self):
        """``avg(sum by (cpu) (rate(...)))`` takes the TS two-stage branch."""
        result = _translate("avg (sum by (cpu) (rate (node_cpu_seconds_total[5m])))", "")
        self.assertEqual(_inner_grouping(result.esql_query), ["labels.cpu"])


class RangeFunctionBranchTests(unittest.TestCase):
    """The TS branch (``nested agg`` over ``rate``/``*_over_time``) is affected too."""

    def test_legend_does_not_group_the_inner_rate_aggregation(self):
        result = _translate(
            "avg (sum (rate (node_cpu_seconds_total[5m])))", "{{cpu}}"
        )
        inner, outer = _stats_lines(result.esql_query)
        self.assertIn("inner_val = SUM(RATE(metrics.node_cpu_seconds_total))", inner)
        self.assertEqual(_grouping_dims(inner), [])
        self.assertIn("AVG(inner_val)", outer)


class SummaryPanelBranchTests(unittest.TestCase):
    """The scalar branch collapses to one value and never had a breakdown column.

    A bargauge is the one summary panel that keeps legend-derived hints, so it is
    where the summary branch's inner grouping was reachable.
    """

    def test_bargauge_reduces_over_the_collapsed_total(self):
        result = _translate(
            f"max (sum ({_USED_BYTES}))", "{{exported_namespace}}", panel_type="bargauge"
        )
        # Nested aggregations lower through ``TS`` + a time bucket (#380/#381),
        # so the summary panel adds a third stage that collapses the bucketed
        # series to the one number it displays.
        inner, outer, collapse = _stats_lines(result.esql_query)
        self.assertIn(f"inner_val = SUM(metrics.{_USED_BYTES})", inner)
        self.assertEqual(_grouping_dims(inner), [])
        self.assertIn("MAX(inner_val)", outer)
        self.assertIn("LAST(", collapse)
        self.assertNotIn("exported_namespace", result.esql_query or "")


class NestedCountCountTests(unittest.TestCase):
    """``count(count by (...) (m))`` counts distinct label values — a scalar.

    This branch emits one ``COUNT_DISTINCT`` ``STATS`` rather than a two-stage
    nested one, so it has its own grouping decision. A legend token must not be
    able to select the counted field, steer the expression into the
    COUNT_DISTINCT shape at all, or reach the outer ``BY``: grouping
    ``COUNT_DISTINCT(cpu)`` by ``cpu`` self-cancels to a constant 1.
    """

    @staticmethod
    def _count_grouping(query):
        stats = _stats_lines(query)
        assert len(stats) == 1, f"expected one COUNT_DISTINCT stage, got: {query}"
        return _grouping_dims(stats[0])

    def test_legend_does_not_trigger_count_distinct(self):
        """``count(count(m))`` is a scalar 1 in PromQL.

        The legend used to select the branch *and* its field, emitting the
        self-cancelling ``COUNT_DISTINCT(labels.cpu) BY labels.cpu`` plus a
        warning describing an approximation that the source never asked for.
        """
        result = _translate(
            "count (count (node_cpu_seconds_total))", "{{cpu}}", panel_type="bargauge"
        )
        self.assertNotIn("COUNT_DISTINCT", result.esql_query)
        self.assertIn("COUNT(*)", result.esql_query)
        self.assertFalse(
            [r for r in (result.reasons or []) if "COUNT_DISTINCT" in r],
            result.reasons,
        )

    def test_explicit_inner_by_still_reaches_count_distinct(self):
        """A timeseries panel, so the legend really does contribute a hint.

        (A ``stat`` panel would pass vacuously — it contributes no legend hints
        at all, which once hid the outer-grouping leak below.)
        """
        result = _translate(
            "count (count by (cpu) (node_cpu_seconds_total))",
            "{{cpu}}",
            panel_type="timeseries",
        )
        self.assertIn("COUNT_DISTINCT(labels.cpu)", result.esql_query)

    def test_legend_does_not_group_the_count_distinct(self):
        """The counted field must not also become the grouping key.

        ``COUNT_DISTINCT(cpu) BY cpu`` is 1 for every CPU, so the panel reported
        a flat 1 instead of the CPU count.
        """
        result = _translate(
            "count (count by (cpu) (node_cpu_seconds_total))",
            "{{cpu}}",
            panel_type="timeseries",
        )
        self.assertEqual(self._count_grouping(result.esql_query), [])

    def test_legend_does_not_change_the_count_distinct_query(self):
        with_legend = _translate(
            "count (count by (cpu) (node_cpu_seconds_total))",
            "{{cpu}}",
            panel_type="timeseries",
        )
        without_legend = _translate(
            "count (count by (cpu) (node_cpu_seconds_total))", "", panel_type="timeseries"
        )
        self.assertEqual(with_legend.esql_query, without_legend.esql_query)

    def test_summary_panel_count_distinct_stays_a_single_value(self):
        result = _translate(
            "count (count by (cpu) (node_cpu_seconds_total))",
            "{{cpu}}",
            panel_type="bargauge",
        )
        self.assertEqual(self._count_grouping(result.esql_query), [])

    def test_explicit_outer_by_still_groups_the_count_distinct(self):
        """Source grouping is preserved; only the hint is refused."""
        result = _translate(
            "count by (namespace) (count by (namespace, cpu) (node_cpu_seconds_total))",
            "{{cpu}}",
            panel_type="timeseries",
        )
        self.assertIn("COUNT_DISTINCT(labels.cpu)", result.esql_query)
        self.assertEqual(
            self._count_grouping(result.esql_query), ["labels.namespace"]
        )

    def test_legacy_table_pattern_does_not_group_the_count_distinct(self):
        """Non-legend hint origins are merged even past an explicit ``by()``.

        ``_merge_group_fields`` only lets an explicit grouping win outright for
        ``legend``-origin hints, so a legacy table's column pattern needs its own
        coverage here.
        """
        result = _translate(
            "count by (namespace) (count by (namespace, cpu) (node_cpu_seconds_total))",
            "",
            panel_type="table-old",
            styles=[{"pattern": "cpu"}],
        )
        self.assertEqual(
            self._count_grouping(result.esql_query), ["labels.namespace"]
        )


class NonLegendDisplayHintTests(unittest.TestCase):
    """Other display-hint origins are equally unfit as an inner grouping.

    A legacy table's ``styles`` column patterns are recorded as preferred group
    labels with no ``legend`` origin tag, so a provenance check that only
    rejected ``origin == "legend"`` would still corrupt these panels.
    """

    def test_legacy_table_column_pattern_does_not_group_the_inner_aggregation(self):
        result = _translate(
            f"max (sum ({_USED_BYTES}))",
            "",
            panel_type="table-old",
            styles=[{"pattern": "exported_namespace"}],
        )
        self.assertEqual(_inner_grouping(result.esql_query), [])


class FusedMeasureSpecTests(unittest.TestCase):
    """``_build_measure_spec`` builds the same nested shapes for fused targets.

    Multi-target panels deliberately keep a shared ``legendFormat``, so the hint
    reaches the measure-spec path even though the direct emitter never sees it.
    The invariant has to hold on both or the defect just moves.
    """

    _COUNT_TARGETS = (
        "count (count by (cpu) (node_cpu_seconds_total))",
        f"count (count by (cpu) ({_USED_BYTES}))",
    )

    def test_shared_legend_does_not_group_fused_count_distinct(self):
        result = _translate_targets([(expr, "{{cpu}}") for expr in self._COUNT_TARGETS])
        stats = _stats_lines(result.esql_query)
        self.assertEqual(len(stats), 1, result.esql_query)
        self.assertIn("COUNT_DISTINCT(labels.cpu)", stats[0])
        self.assertEqual(_grouping_dims(stats[0]), [])

    def test_shared_legend_only_changes_series_aliases(self):
        """The legend may still name the series — it just cannot group them.

        Asserting both halves matters: identical groupings show the hint was
        refused as a dimension, and differing measure aliases show it was still
        honored as a display hint rather than discarded outright.
        """
        with_legend = _translate_targets(
            [(expr, "{{cpu}}") for expr in self._COUNT_TARGETS]
        )
        without_legend = _translate_targets([(expr, "") for expr in self._COUNT_TARGETS])
        legend_stats = _stats_lines(with_legend.esql_query)[0]
        plain_stats = _stats_lines(without_legend.esql_query)[0]
        self.assertEqual(_grouping_dims(legend_stats), _grouping_dims(plain_stats))
        self.assertNotEqual(_measure_aliases(legend_stats), _measure_aliases(plain_stats))
        self.assertIn("cpu", _measure_aliases(legend_stats)[0])


class BinaryOverNestedAggTests(unittest.TestCase):
    """A binary expression must not launder a hint into a nested reduction.

    A formula plans each operand separately and then fuses them into one
    pipeline, so there are two ways the hint can come back: the planner can
    re-offer it as a proven ``sibling_binary`` grouping, and the shared pipeline
    can union the operands' groupings. Either one restores
    ``COUNT_DISTINCT(cpu) BY cpu``, so the assertion here is about the emitted
    query, not about which mechanism refused it — a panel that cannot express
    both operands' groupings at once is allowed to fail closed instead.
    """

    _CASES = (
        "scalar(node_load1) / count (count by (cpu) (node_cpu_seconds_total))",
        "node_load1 / count (count by (cpu) (node_cpu_seconds_total))",
        "sum by (cpu) (node_cpu_seconds_total) "
        "/ count (count by (cpu) (node_cpu_seconds_total))",
    )

    def test_legend_never_groups_a_nested_count_distinct(self):
        for expr in self._CASES:
            with self.subTest(expr=expr):
                result = _translate(expr, "{{cpu}}", fields=_BINARY_FIELDS)
                query = result.esql_query or ""
                if result.status == "not_feasible":
                    continue
                for line in _stats_lines(query):
                    if "COUNT_DISTINCT" in line:
                        self.assertNotIn("labels.cpu", _grouping_dims(line), query)

    def test_ungrouped_sibling_still_fuses_without_a_legend(self):
        """Control: the shape itself is translatable; only the hint is refused."""
        result = _translate(self._CASES[0], "", fields=_BINARY_FIELDS)
        self.assertNotEqual(result.status, "not_feasible")
        stats = _stats_lines(result.esql_query)
        self.assertEqual(len(stats), 1, result.esql_query)
        self.assertIn("COUNT_DISTINCT(labels.cpu)", stats[0])
        self.assertEqual(_grouping_dims(stats[0]), [])


class LabelReplaceOverNestedAggTests(unittest.TestCase):
    """``label_replace`` requests its source label through the same hint channel.

    It appends the label to ``preferred_group_labels`` and then appends ``EVAL
    dst = <src>`` to whatever the inner rule emitted. That is only a request: a
    nested aggregation resolves its grouping from the source expression, and a
    two-stage nested aggregation's outer ``STATS`` drops the inner grouping
    regardless. Emitting the ``EVAL`` anyway referenced a column the query no
    longer had — an "Unknown column" failure at query time on a panel reported as
    migrated, so these forms must fail closed instead.
    """

    _LR_NESTED = (
        f'label_replace(max (sum ({_USED_BYTES})), "ns", "$1", "namespace", "(.*)")'
    )

    def test_nested_aggregation_fails_closed(self):
        result = _translate(self._LR_NESTED, "")
        self.assertEqual(result.status, "not_feasible")

    def test_failure_names_the_lost_label(self):
        result = _translate(self._LR_NESTED, "")
        reasons = " ".join(result.reasons or [])
        self.assertIn("namespace", reasons)
        self.assertIn("label_replace()", reasons)

    def test_no_query_references_a_dropped_column(self):
        """The old output aggregated the label away, then read it back."""
        result = _translate(self._LR_NESTED, "")
        self.assertNotIn("EVAL ns = labels.namespace", result.esql_query or "")

    def test_explicit_inner_by_does_not_rescue_it(self):
        """The outer ``STATS`` drops the inner grouping either way."""
        result = _translate(
            f'label_replace(max (sum by (namespace) ({_USED_BYTES})), "ns", "$1", '
            '"namespace", "(.*)")',
            "",
        )
        self.assertEqual(result.status, "not_feasible")

    def test_nested_count_distinct_fails_closed(self):
        result = _translate(
            'label_replace(count (count by (cpu) (node_cpu_seconds_total)), "c", "$1", '
            '"cpu", "(.*)")',
            "",
        )
        self.assertEqual(result.status, "not_feasible")
        self.assertNotIn("EVAL c = labels.cpu", result.esql_query or "")

    def test_flat_grouped_label_replace_still_migrates(self):
        """The label is a real grouping key here, so the EVAL is valid."""
        result = _translate(
            f'label_replace(sum by (namespace) ({_USED_BYTES}), "ns", "$1", '
            '"namespace", "(.*)")',
            "",
        )
        self.assertNotEqual(result.status, "not_feasible")
        self.assertIn("labels.namespace", result.esql_query)

    def test_literal_replacement_needs_no_source_label(self):
        """No ``$1``, so nothing has to survive the aggregation."""
        result = _translate(
            f'label_replace(sum ({_USED_BYTES}), "ns", "static", "", "")', ""
        )
        self.assertNotEqual(result.status, "not_feasible")

    def test_unaggregated_expression_keeps_every_field(self):
        result = _translate(
            f'label_replace({_USED_BYTES}, "ns", "$1", "namespace", "(.*)")', ""
        )
        self.assertNotEqual(result.status, "not_feasible")

    def test_identity_copy_needs_no_surviving_column(self):
        """An identity copy emits nothing, so it reads no column to lose.

        ``label_replace(x, "labels.namespace", "$1", "namespace", "(.*)")``
        renames the label onto itself once ``namespace`` resolves to
        ``labels.namespace``. The clause has to be built before the
        survival check or this valid no-op is rejected for a column it never
        references.
        """
        result = _translate(
            f'label_replace(max (sum ({_USED_BYTES})), "labels.namespace", "$1", '
            '"namespace", "(.*)")',
            "",
        )
        self.assertNotEqual(result.status, "not_feasible")
        self.assertNotIn("EVAL", result.esql_query or "")

    def test_constant_replacement_over_a_nested_aggregation_survives(self):
        """No capture group, so the EVAL is a literal and reads no column."""
        result = _translate(
            f'label_replace(max (sum ({_USED_BYTES})), "ns", "prod", "namespace", "(.*)")',
            "",
        )
        self.assertNotEqual(result.status, "not_feasible")
        self.assertIn('EVAL ns = "prod"', result.esql_query)


class FlatGroupingIsUnaffectedTests(unittest.TestCase):
    """The single-level path already implemented this policy; keep it that way."""

    def test_flat_ungrouped_aggregation_keeps_ignoring_the_legend(self):
        result = _translate(f"sum ({_USED_BYTES})", "{{exported_namespace}}")
        stats = _stats_lines(result.esql_query)
        self.assertEqual(len(stats), 1, result.esql_query)
        self.assertEqual(_grouping_dims(stats[0]), [])

    def test_flat_explicit_by_still_groups(self):
        result = _translate(f"sum by (namespace) ({_USED_BYTES})", "")
        stats = _stats_lines(result.esql_query)
        self.assertEqual(_grouping_dims(stats[0]), ["labels.namespace"])


if __name__ == "__main__":
    unittest.main()
