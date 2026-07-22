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

    def _translate_result(self, expr: str, metric_map: dict, *, metric_kinds: dict | None = None):
        rule_pack = RulePackConfig()
        rule_pack.metric_map.update(normalize_metric_map(metric_map))
        if metric_kinds:
            rule_pack.metric_kinds.update(metric_kinds)
        resolver = SchemaResolver(rule_pack, field_profile="otel")
        return translate_promql_to_esql(
            expr,
            datasource_index="metrics-*",
            panel_type="timeseries",
            rule_pack=rule_pack,
            resolver=resolver,
        )

    def test_variant_mismatch_surfaces_panel_warning(self) -> None:
        result = self._translate_result(
            'sum(net_bytes{direction="transmit"})',
            {
                "net_bytes": {
                    "variants": [
                        {
                            "source_filter": {"direction": "receive"},
                            "target": "k8s.pod.network.io",
                            "attribute_filter": {"network.direction": "receive"},
                        }
                    ]
                }
            },
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertTrue(
            any("none matched" in str(w) for w in result.warnings),
            result.warnings,
        )
        assert result.esql_query is not None
        self.assertNotIn("k8s.pod.network.io", result.esql_query)

    def test_to_rate_emits_rate_when_target_is_counter(self) -> None:
        result = self._translate_result(
            "sum(source_bytes)",
            {
                "source_bytes": {
                    "target": "target.bytes",
                    "transform": "to_rate",
                }
            },
            metric_kinds={"target.bytes": "counter"},
        )
        self.assertEqual(result.feasibility, "feasible", result.warnings)
        assert result.esql_query is not None
        self.assertIn("RATE(", result.esql_query)
        self.assertIn("target.bytes", result.esql_query)

    def test_drop_rate_strips_rate_when_target_is_gauge(self) -> None:
        result = self._translate_result(
            "sum(rate(source_bytes[5m]))",
            {
                "source_bytes": {
                    "target": "target.bytes",
                    "transform": "drop_rate",
                }
            },
            metric_kinds={"target.bytes": "gauge"},
        )
        self.assertEqual(result.feasibility, "feasible", result.warnings)
        assert result.esql_query is not None
        self.assertIn("target.bytes", result.esql_query)
        self.assertNotIn("RATE(", result.esql_query)
        self.assertIn("LAST_OVER_TIME(", result.esql_query)

    def test_to_rate_unknown_kind_warns_and_does_not_invent_rate(self) -> None:
        result = self._translate_result(
            "sum(source_bytes)",
            {
                "source_bytes": {
                    "target": "target.bytes",
                    "transform": "to_rate",
                }
            },
        )
        self.assertEqual(result.feasibility, "feasible")
        self.assertTrue(
            any("to_rate requires known" in str(w) for w in result.warnings),
            result.warnings,
        )
        assert result.esql_query is not None
        self.assertNotIn("RATE(", result.esql_query)

    def test_mixed_target_index_warns_and_keeps_default(self) -> None:
        result = self._translate_result(
            "sum(source_a) + sum(source_b)",
            {
                "source_a": {
                    "target": "target.a",
                    "target_index": "metrics-a-*",
                },
                "source_b": {
                    "target": "target.b",
                    "target_index": "metrics-b-*",
                },
            },
        )
        self.assertEqual(result.feasibility, "feasible", result.warnings)
        self.assertTrue(
            any("target_index values differ" in str(w) for w in result.warnings),
            result.warnings,
        )
        assert result.esql_query is not None
        self.assertIn("metrics-*", result.esql_query)
        self.assertNotIn("metrics-a-*", result.esql_query)
        self.assertNotIn("metrics-b-*", result.esql_query)


if __name__ == "__main__":
    unittest.main()
