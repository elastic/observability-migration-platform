# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Class-2 metric_map fields must appear in emitted Grafana ES|QL."""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import translate_promql_to_esql
from observability_migration.core.metric_mapping import normalize_metric_map


class GrafanaClass2EsqlEmitTests(unittest.TestCase):
    def _translate(self, expr: str, metric_map: dict) -> str:
        rule_pack = RulePackConfig()
        rule_pack.metric_map.update(normalize_metric_map(metric_map))
        resolver = SchemaResolver(rule_pack, field_profile="otel")
        result = translate_promql_to_esql(
            expr,
            datasource_index="metrics-*",
            panel_type="timeseries",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        self.assertEqual(result.feasibility, "feasible", result.warnings)
        assert result.esql_query is not None
        return result.esql_query

    def test_attribute_filter_emits_where_clause(self) -> None:
        esql = self._translate(
            "sum(container_network_receive_bytes_total)",
            {
                "container_network_receive_bytes_total": {
                    "target": "k8s.pod.network.io",
                    "attribute_filter": {"network.direction": "receive"},
                }
            },
        )
        self.assertIn('network.direction == "receive"', esql)
        self.assertIn("k8s.pod.network.io", esql)
        self.assertIn("SUM(LAST_OVER_TIME(k8s.pod.network.io))", esql)

    def test_unit_scale_multiplies_stats_expression(self) -> None:
        esql = self._translate(
            "sum(source_bytes)",
            {
                "source_bytes": {
                    "target": "target.kilobytes",
                    "unit_scale": 0.001,
                }
            },
        )
        self.assertIn("target.kilobytes", esql)
        self.assertIn("* 0.001", esql)

    def test_variants_select_attribute_filter_from_source_labels(self) -> None:
        esql = self._translate(
            'sum(container_network_bytes_total{direction="receive"})',
            {
                "container_network_bytes_total": {
                    "variants": [
                        {
                            "source_filter": {"direction": "receive"},
                            "target": "k8s.pod.network.io",
                            "attribute_filter": {"network.direction": "receive"},
                        },
                        {
                            "source_filter": {"direction": "transmit"},
                            "target": "k8s.pod.network.io",
                            "attribute_filter": {"network.direction": "transmit"},
                        },
                    ]
                }
            },
        )
        self.assertIn("k8s.pod.network.io", esql)
        self.assertIn('network.direction == "receive"', esql)
        self.assertNotIn('network.direction == "transmit"', esql)
        self.assertNotIn('| WHERE direction == "receive"', esql)

    def test_topk_applies_unit_scale(self) -> None:
        esql = self._translate(
            "topk(5, sum(source_bytes) by (pod))",
            {
                "source_bytes": {
                    "target": "target.kilobytes",
                    "unit_scale": 0.001,
                }
            },
        )
        self.assertIn("target.kilobytes", esql)
        self.assertIn("* 0.001", esql)

    def test_target_index_overrides_from_clause(self) -> None:
        esql = self._translate(
            "sum(source_bytes)",
            {
                "source_bytes": {
                    "target": "target.bytes",
                    "target_index": "metrics-custom-*",
                }
            },
        )
        self.assertTrue(
            "FROM metrics-custom-*" in esql or "TS metrics-custom-*" in esql,
            esql,
        )
        self.assertIn("target.bytes", esql)

    def test_histogram_quantile_applies_target_index(self) -> None:
        esql = self._translate(
            'histogram_quantile(0.95, sum(rate(source_bucket{route="/"}[5m])) by (le))',
            {
                "source": {
                    "target": "target.histogram",
                    "target_index": "metrics-histogram-*",
                }
            },
        )
        self.assertIn("TS metrics-histogram-*", esql)
        self.assertIn("target.histogram", esql)


if __name__ == "__main__":
    unittest.main()
