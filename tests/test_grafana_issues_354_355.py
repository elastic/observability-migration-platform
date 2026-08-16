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

import unittest
from types import SimpleNamespace

from observability_migration.adapters.source.grafana import panels, rules, schema


def _translate(panel):
    rp = rules.RulePackConfig()
    return panels.translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=schema.SchemaResolver(rp),
    )


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


if __name__ == "__main__":
    unittest.main()
