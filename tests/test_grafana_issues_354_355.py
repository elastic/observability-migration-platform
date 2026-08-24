# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for GitHub issues #354 and #355.

#354 — a multi-target panel whose targets carry *different* multi-placeholder
``legendFormat`` templates hard-coded every series to the first target's
literal text.

#355 — a bare (no ``by()``) PromQL aggregation sharing its metric with a
grouped sibling target was unioned onto the grouped ``BY`` fields, turning a
cross-series Min/Avg/Max into per-group duplicates of the raw series.
"""

from __future__ import annotations

import re
import unittest
from types import SimpleNamespace

from observability_migration.adapters.source.grafana import panels, rules, schema

_KEYWORD = {"keyword": {"type": "keyword", "searchable": True, "aggregatable": True}}
_DOUBLE = {"double": {"type": "double", "aggregatable": True}}


def _translate(panel):
    rp = rules.RulePackConfig()
    return panels.translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=schema.SchemaResolver(rp),
    )


def _translate_with_schema(panel, fields):
    """Translate *panel* against a resolver whose target fields are proven.

    Legend-derived grouping is only kept once ``field_exists`` confirms every
    resolved field, so a target that declares no ``by()`` of its own can only
    reach a grouped translation through a resolver like this one. Seeding
    follows ``tests/test_multi_target_merge_aliases.py``.
    """
    rp = rules.RulePackConfig()
    resolver = schema.SchemaResolver(rp, field_profile="prometheus_native")
    resolver._discovery_attempted = True
    resolver._field_cache = dict(fields)
    resolver._discovered_mappings = {}
    resolver._schema_profile_cache_id = None
    return panels.translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=resolver,
    )


def _summary_stats_lines(query):
    """Every ``| STATS`` line in *query* computing a cross-series summary
    aggregate (the Min/Avg/Max trio issue #355 is about)."""
    return [ln for ln in query.splitlines() if ln.startswith("| STATS") and "Min = " in ln]


def _grouping_dims(stats_line):
    """A ``| STATS`` line's grouping dimensions, excluding the time bucket."""
    by_clause = stats_line.split(" BY ", 1)[1] if " BY " in stats_line else ""
    by_clause = re.sub(r"time_bucket = TBUCKET\([^)]*\)", "", by_clause)
    return [part.strip() for part in by_clause.split(",") if part.strip()]


class ClearDisagreeingFusedLegendTemplateTests(unittest.TestCase):
    """Unit tests for the #354 helper itself."""

    def _translation(self, template):
        return SimpleNamespace(metadata={"legend_format_template": template})

    def test_disagreeing_templates_clear_primarys_template(self):
        primary = self._translation("Weighted IO time {{device}} {{instance}}")
        fused = [primary, self._translation("Write time {{device}} {{instance}}")]
        panels._clear_disagreeing_fused_legend_template(primary, fused)
        self.assertIsNone(primary.metadata["legend_format_template"])

    def test_three_way_disagreement_clears_primarys_template(self):
        primary = self._translation("Weighted IO time {{device}} {{instance}}")
        fused = [
            primary,
            self._translation("Write time {{device}} {{instance}}"),
            self._translation("Read time {{device}} {{instance}}"),
        ]
        panels._clear_disagreeing_fused_legend_template(primary, fused)
        self.assertIsNone(primary.metadata["legend_format_template"])

    def test_agreeing_templates_are_preserved(self):
        template = "{{instance}} {{mountpoint}}"
        primary = self._translation(template)
        fused = [primary, self._translation(template)]
        panels._clear_disagreeing_fused_legend_template(primary, fused)
        self.assertEqual(primary.metadata["legend_format_template"], template)

    def test_single_target_is_a_no_op(self):
        primary = self._translation("{{instance}} {{mountpoint}}")
        panels._clear_disagreeing_fused_legend_template(primary, [primary])
        self.assertEqual(primary.metadata["legend_format_template"], "{{instance}} {{mountpoint}}")

    def test_no_template_is_a_no_op(self):
        primary = self._translation(None)
        fused = [primary, self._translation("{{instance}}")]
        panels._clear_disagreeing_fused_legend_template(primary, fused)
        self.assertIsNone(primary.metadata["legend_format_template"])


class DiskIoMultiTargetLegendTests(unittest.TestCase):
    """Integration coverage using issue #354's exact "Disk IO" repro
    (dashboard 9852): three targets, three different metrics, three
    different multi-placeholder ``legendFormat`` templates."""

    def _disk_io_panel(self):
        return {
            "id": 1,
            "type": "timeseries",
            "title": "Disk IO",
            "targets": [
                {
                    "refId": "A",
                    "expr": "rate(node_disk_io_time_weighted_seconds_total[5m])",
                    "legendFormat": "Weighted IO time {{device}} {{instance}}",
                },
                {
                    "refId": "B",
                    "expr": "rate(node_disk_write_time_seconds_total[5m])",
                    "legendFormat": "Write time {{device}} {{instance}}",
                },
                {
                    "refId": "C",
                    "expr": "rate(node_disk_read_time_seconds_total[5m])",
                    "legendFormat": "Read time {{device}} {{instance}}",
                },
            ],
        }

    def test_all_three_metrics_survive_fusion(self):
        _native, result = _translate(self._disk_io_panel())
        self.assertIn(result.status, {"migrated", "migrated_with_warnings"})
        query = result.esql_query or ""
        self.assertIn("node_disk_io_time_weighted_seconds_total", query)
        self.assertIn("node_disk_write_time_seconds_total", query)
        self.assertIn("node_disk_read_time_seconds_total", query)

    def test_no_series_is_labelled_with_another_targets_metric_name(self):
        """The original bug: every series' legend was hard-coded to target 0's
        literal text ("Weighted IO time ..."), even Write/Read series. Once the
        composite ``EVAL legend`` is suppressed on disagreement, no such
        cross-contaminating literal can appear in the query at all."""
        _native, result = _translate(self._disk_io_panel())
        query = result.esql_query or ""
        self.assertNotIn('CONCAT("Weighted IO time', query)

    def test_memory_style_matching_single_placeholder_templates_unaffected(self):
        """The dashboard's own working case (per the issue): targets whose
        templates differ only by a static prefix over a *single* shared label
        placeholder never enter the composite-legend path, so this fix must
        not change their (already-correct) behavior."""
        panel = {
            "id": 2,
            "type": "timeseries",
            "title": "Memory",
            "targets": [
                {
                    "refId": "A",
                    "expr": "node_memory_Active_bytes",
                    "legendFormat": "{{instance}} - Memory active",
                },
                {
                    "refId": "B",
                    "expr": "node_memory_Buffers_bytes",
                    "legendFormat": "{{instance}} - Memory buffers",
                },
            ],
        }
        _native, result = _translate(panel)
        self.assertIn(result.status, {"migrated", "migrated_with_warnings"})
        query = result.esql_query or ""
        self.assertIn("node_memory_Active_bytes", query)
        self.assertIn("node_memory_Buffers_bytes", query)


class BareAggregationScopeSplitTests(unittest.TestCase):
    """Integration coverage using issue #355's CPU-iowait Min/Avg/Max shape:
    a grouped per-``cpu`` target sharing its metric with three bare (no
    ``by()``) aggregations that Grafana always draws as one cross-series
    line each."""

    def _io_wait_panel(self):
        return {
            "id": 1,
            "type": "timeseries",
            "title": "IO Wait per core",
            "targets": [
                {
                    "refId": "A",
                    "expr": 'sum(rate(node_cpu_seconds_total{mode="iowait"}[5m])) by (cpu)',
                    "legendFormat": "CPU {{cpu}}",
                },
                {
                    "refId": "B",
                    "expr": 'min(rate(node_cpu_seconds_total{mode="iowait"}[5m]))',
                    "legendFormat": "Min",
                },
                {
                    "refId": "C",
                    "expr": 'avg(rate(node_cpu_seconds_total{mode="iowait"}[5m]))',
                    "legendFormat": "Avg",
                },
                {
                    "refId": "D",
                    "expr": 'max(rate(node_cpu_seconds_total{mode="iowait"}[5m]))',
                    "legendFormat": "Max",
                },
            ],
        }

    def test_bare_aggregations_are_rendered_as_a_separate_layer(self):
        native, result = _translate(self._io_wait_panel())
        self.assertEqual(result.status, "migrated_with_warnings")
        layers = native["esql"].get("layers") or []
        self.assertEqual(len(layers), 1)
        bare_layer_query = layers[0]["query"]
        # The bare layer's BY clause has no grouping dimension beyond
        # time_bucket -- no per-cpu duplication (the STATS line ends right
        # after the time bucket, with nothing appended before the newline).
        self.assertRegex(bare_layer_query, r"BY time_bucket = TBUCKET\([^)]*\)\n")
        self.assertIn("Min = MIN(RATE(node_cpu_seconds_total))", bare_layer_query)
        self.assertIn("Avg = AVG(RATE(node_cpu_seconds_total))", bare_layer_query)
        self.assertIn("Max = MAX(RATE(node_cpu_seconds_total))", bare_layer_query)

    def test_grouped_layer_keeps_its_own_per_cpu_grouping(self):
        native, _result = _translate(self._io_wait_panel())
        primary_query = native["esql"]["query"]
        self.assertIn("BY time_bucket = TBUCKET", primary_query)
        self.assertIn("cpu", primary_query)
        # The grouped layer must not also carry Min/Avg/Max (that was the bug:
        # they used to be unioned into the SAME per-cpu STATS).
        self.assertNotIn("Min", primary_query)
        self.assertNotIn("Avg", primary_query)
        self.assertNotIn("Max", primary_query)

    def test_warning_names_the_changed_semantics(self):
        _native, result = _translate(self._io_wait_panel())
        joined = " ".join(result.reasons or [])
        self.assertIn("Min, Avg, Max", joined)
        self.assertIn("separate summary layer aggregated across every series", joined)

    def test_qos_style_unrelated_metric_broadcast_keeps_todays_union(self):
        """The unrelated-metric case (different bare-aggregation metric than
        the grouped sibling) must NOT be split -- there is no shared
        per-series computation to separate out. Covered in depth by
        ``tests/test_grafana_qos_union_by.py``; this only pins that the
        #355 split does not misfire on it."""
        panel = {
            "id": 3,
            "type": "timeseries",
            "title": "Pods QoS",
            "targets": [
                {
                    "refId": "A",
                    "expr": "sum(kube_pod_status_qos_class) by (qos_class)",
                    "legendFormat": "QoS",
                },
                {
                    "refId": "B",
                    "expr": "sum(kube_pod_info)",
                    "legendFormat": "Total",
                },
            ],
        }
        native, result = _translate(panel)
        self.assertNotIn("layers", native["esql"])
        joined = " ".join(result.reasons or [])
        self.assertIn("Unioned BY fields", joined)

    def test_unrelated_bare_target_does_not_disable_a_valid_same_metric_split(self):
        """A THIRD, unrelated-metric bare target (the QoS/Total broadcast
        shape) sharing the panel with a genuine same-metric bare/grouped pair
        must not disable the split for the pair that *does* match -- only
        the unrelated target should stay on the union path alongside the
        grouped series."""
        panel = {
            "id": 4,
            "type": "timeseries",
            "title": "Mixed",
            "targets": [
                {
                    "refId": "A",
                    "expr": 'sum(rate(node_cpu_seconds_total{mode="iowait"}[5m])) by (cpu)',
                    "legendFormat": "CPU {{cpu}}",
                },
                {
                    "refId": "B",
                    "expr": 'avg(rate(node_cpu_seconds_total{mode="iowait"}[5m]))',
                    "legendFormat": "Avg",
                },
                {
                    "refId": "C",
                    "expr": "sum(kube_pod_info)",
                    "legendFormat": "Total",
                },
            ],
        }
        native, _result = _translate(panel)
        layers = native["esql"].get("layers") or []
        self.assertEqual(len(layers), 1, native["esql"])
        bare_layer_query = layers[0]["query"]
        # The Avg bare aggregate is still split into its own summary layer
        # despite the unrelated "Total" broadcast sharing the panel.
        self.assertRegex(bare_layer_query, r"BY time_bucket = TBUCKET\([^)]*\)\n")
        self.assertIn("Avg = AVG(RATE(node_cpu_seconds_total))", bare_layer_query)
        primary_query = native["esql"]["query"]
        # "Total" (unrelated metric) stays unioned with the grouped CPU series.
        self.assertIn("Total", primary_query)
        self.assertNotIn("Avg", primary_query)

    def test_singleton_bare_layer_keeps_its_static_legend(self):
        """When exactly one bare target shares its metric with the grouped
        target (e.g. a per-core breakdown plus a single "Total"/"Min" summary
        line, without siblings like Avg/Max), the resulting summary layer
        must still carry that target's own Grafana legend text as its column
        identity, the same way a multi-bare-target group already does."""
        panel = {
            "id": 5,
            "type": "timeseries",
            "title": "Solo Bare",
            "targets": [
                {
                    "refId": "A",
                    "expr": 'sum(rate(node_cpu_seconds_total{mode="iowait"}[5m])) by (cpu)',
                    "legendFormat": "CPU {{cpu}}",
                },
                {
                    "refId": "B",
                    "expr": 'min(rate(node_cpu_seconds_total{mode="iowait"}[5m]))',
                    "legendFormat": "Min",
                },
            ],
        }
        native, _result = _translate(panel)
        layers = native["esql"].get("layers") or []
        self.assertEqual(len(layers), 1, native["esql"])
        bare_layer_query = layers[0]["query"]
        self.assertIn("Min = MIN(RATE(node_cpu_seconds_total))", bare_layer_query)
        self.assertEqual(layers[0]["metrics"], [{"field": "Min"}])

    def test_split_is_order_independent_when_bare_target_is_listed_first(self):
        """Grafana target order is not guaranteed: a dashboard may list the
        bare summary target before the grouped breakdown target. The split
        must produce the same result either way, and the grouped layer's own
        main-metric label must not inherit the bare target's static legend
        just because it happened to be ``fused_series[0]``."""
        bare_first_panel = {
            "id": 6,
            "type": "timeseries",
            "title": "Bare First",
            "targets": [
                {
                    "refId": "A",
                    "expr": 'avg(rate(node_cpu_seconds_total{mode="iowait"}[5m]))',
                    "legendFormat": "Avg",
                },
                {
                    "refId": "B",
                    "expr": 'sum(rate(node_cpu_seconds_total{mode="iowait"}[5m])) by (cpu)',
                    "legendFormat": "CPU {{cpu}}",
                },
            ],
        }
        grouped_first_panel = {
            "id": 7,
            "type": "timeseries",
            "title": "Grouped First",
            "targets": list(reversed(bare_first_panel["targets"])),
        }
        bare_first_native, _ = _translate(bare_first_panel)
        grouped_first_native, _ = _translate(grouped_first_panel)
        self.assertEqual(bare_first_native["esql"]["query"], grouped_first_native["esql"]["query"])
        self.assertEqual(bare_first_native["esql"]["layers"], grouped_first_native["esql"]["layers"])
        # The main (grouped) panel's own metric label must not become "Avg"
        # just because the bare "Avg" target happened to be primary.
        main_metrics = bare_first_native["esql"].get("metrics") or []
        self.assertTrue(main_metrics)
        self.assertNotEqual(main_metrics[0].get("label"), "Avg")


class IoWaitSchemaInferredGroupingTests(unittest.TestCase):
    """Issue #355's panel exactly as dashboard 9852 ships it.

    ``BareAggregationScopeSplitTests`` above declares the per-series grouping
    with an explicit ``sum(...) by (cpu)``, which reaches the split straight
    from the parsed ``by()`` clause. The real "IO Wait per core" panel has no
    ``by()`` at all: its ``instance``/``cpu``/``mode`` grouping is recovered
    from the ``{{ instance }} CPU {{ cpu }}/{{ mode }}`` legend and only
    survives once the resolver proves those labels are real target fields.
    That is a second, schema-dependent route into the split, so a regression
    that only broke *it* would pass the explicit-``by()`` fixture above.
    """

    _EXPR = (
        'rate(node_cpu_seconds_total{cpu=~"$CPU", instance=~"$Node", '
        'mode="iowait"}[$RateInterval])'
    )
    _FIELDS = {
        "labels.instance": _KEYWORD,
        "labels.cpu": _KEYWORD,
        "labels.mode": _KEYWORD,
        "metrics.node_cpu_seconds_total": _DOUBLE,
    }

    def _io_wait_panel(self):
        """Verbatim from grafana.com dashboard 9852 revision 1, panel id 2."""
        return {
            "id": 2,
            "type": "graph",
            "title": "IO Wait per core",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {
                    "refId": "A",
                    "expr": self._EXPR,
                    "legendFormat": "{{ instance }} CPU {{ cpu }}/{{ mode }}",
                },
                {"refId": "B", "expr": f"min({self._EXPR})", "legendFormat": "Min"},
                {"refId": "C", "expr": f"avg({self._EXPR})", "legendFormat": "Avg"},
                {"refId": "D", "expr": f"max({self._EXPR})", "legendFormat": "Max"},
            ],
        }

    def test_schema_inferred_grouping_still_splits_the_bare_aggregations(self):
        native, result = _translate_with_schema(self._io_wait_panel(), self._FIELDS)
        self.assertEqual(result.status, "migrated_with_warnings")
        layers = native["esql"].get("layers") or []
        self.assertEqual(len(layers), 1, native["esql"])
        summary_query = layers[0]["query"]
        self.assertIn("Min = MIN(RATE(metrics.node_cpu_seconds_total))", summary_query)
        self.assertIn("Avg = AVG(RATE(metrics.node_cpu_seconds_total))", summary_query)
        self.assertIn("Max = MAX(RATE(metrics.node_cpu_seconds_total))", summary_query)
        summary_lines = _summary_stats_lines(summary_query)
        self.assertEqual(len(summary_lines), 1, summary_query)
        self.assertEqual(_grouping_dims(summary_lines[0]), [])

    def test_grouped_layer_keeps_every_legend_derived_dimension(self):
        native, _result = _translate_with_schema(self._io_wait_panel(), self._FIELDS)
        primary_query = native["esql"]["query"]
        stats = next(line for line in primary_query.splitlines() if line.startswith("| STATS"))
        for field in ("labels.instance", "labels.cpu", "labels.mode"):
            self.assertIn(field, stats)
        # Series identity for the ten per-core lines rides on the composite
        # breakdown column, not on the summary layer's aggregates.
        self.assertEqual(native["esql"].get("breakdown"), {"field": "series_group"})
        self.assertNotIn("Min", primary_query)
        self.assertNotIn("Avg", primary_query)
        self.assertNotIn("Max", primary_query)

    def test_warning_names_the_changed_semantics(self):
        _native, result = _translate_with_schema(self._io_wait_panel(), self._FIELDS)
        joined = " ".join(result.reasons or [])
        self.assertIn("Min, Avg, Max", joined)
        self.assertIn("separate summary layer aggregated across every series", joined)

    def test_summary_aggregates_are_never_grouped_per_series(self):
        """The #355 bug was Min/Avg/Max landing inside the per-core ``BY``. That
        must not happen on any translation of this panel, including one whose
        grouping the resolver cannot prove -- there, no target is grouped at
        all, so there is nothing for the aggregates to be grouped by either."""
        for label, (native, _result) in {
            "proven schema": _translate_with_schema(self._io_wait_panel(), self._FIELDS),
            "no schema discovery": _translate(self._io_wait_panel()),
        }.items():
            esql = native["esql"]
            queries = [esql["query"], *(layer["query"] for layer in esql.get("layers") or [])]
            summary_lines = [ln for query in queries for ln in _summary_stats_lines(query)]
            self.assertTrue(summary_lines, f"{label}: no summary aggregate emitted: {queries}")
            for line in summary_lines:
                self.assertEqual(_grouping_dims(line), [], f"{label}: {line}")


if __name__ == "__main__":
    unittest.main()
