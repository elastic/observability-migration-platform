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
        # Gauge emit: no counter-without-rate LAST_OVER_TIME path.
        self.assertNotIn("Counter referenced without rate()", " ".join(map(str, result.warnings)))
        self.assertRegex(result.esql_query, r"SUM\(\s*(TO_DOUBLE\()?target\.bytes\)?")
        self.assertNotIn(", 5m)", result.esql_query)

    def test_drop_rate_strips_rate_inside_topk(self) -> None:
        """topk(sum(rate(...))) must honor drop_rate (Views/Cluster TOP CPU)."""
        result = self._translate_result(
            'topk(5, sum(rate(container_cpu_usage_seconds_total{image!=""}[2m])) by (pod_name))',
            {
                "container_cpu_usage_seconds_total": {
                    "target": "container.cpu.usage",
                    "transform": "drop_rate",
                }
            },
            metric_kinds={"container.cpu.usage": "gauge"},
        )
        self.assertEqual(result.feasibility, "feasible", result.warnings)
        assert result.esql_query is not None
        self.assertIn("container.cpu.usage", result.esql_query)
        self.assertNotIn("RATE(", result.esql_query)

    def test_drop_rate_strips_rate_when_target_kind_unknown(self) -> None:
        """drop_rate must strip RATE even when metric_kinds/caps are silent.

        Reproduces Views / Nodes ``sum(rate(container_cpu_usage_seconds_total))``
        → ``SUM(RATE(container.cpu.usage, 5m))`` when discovery had not yet
        typed the OTel gauge.
        """
        result = self._translate_result(
            'sum(rate(container_cpu_usage_seconds_total{image!=""}[$__rate_interval])) by (pod)',
            {
                "container_cpu_usage_seconds_total": {
                    "target": "container.cpu.usage",
                    "transform": "drop_rate",
                }
            },
        )
        self.assertEqual(result.feasibility, "feasible", result.warnings)
        assert result.esql_query is not None
        self.assertIn("container.cpu.usage", result.esql_query)
        self.assertNotIn("RATE(", result.esql_query)
        self.assertNotIn("drop_rate requires known target counter/gauge kind", " ".join(map(str, result.warnings)))

    def test_mapped_gauge_target_strips_rate_without_explicit_drop_rate(self) -> None:
        """Rename-only map onto a known gauge must not emit RATE(gauge)."""
        result = self._translate_result(
            "sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)",
            {"container_cpu_usage_seconds_total": {"target": "container.cpu.usage"}},
            metric_kinds={"container.cpu.usage": "gauge"},
        )
        self.assertEqual(result.feasibility, "feasible", result.warnings)
        assert result.esql_query is not None
        self.assertIn("container.cpu.usage", result.esql_query)
        self.assertNotIn("RATE(", result.esql_query)

    def test_drop_rate_on_recording_rule_forces_gauge_emit(self) -> None:
        """Pre-rated recording rules have no rate() AST node; drop_rate must
        still force gauge emit so multi-target panels don't inflate siblings."""
        result = self._translate_result(
            'sum(node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate{pod="x"}) by (container)',
            {
                "node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate": {
                    "target": "container.cpu.usage",
                    "transform": "drop_rate",
                }
            },
            metric_kinds={"container.cpu.usage": "gauge"},
        )
        self.assertEqual(result.feasibility, "feasible", result.warnings)
        assert result.esql_query is not None
        self.assertIn("container.cpu.usage", result.esql_query)
        self.assertNotIn("Counter referenced without rate()", " ".join(map(str, result.warnings)))
        self.assertNotIn("SUM_OVER_TIME(container.cpu.usage", result.esql_query)

    def test_multi_target_drop_rate_does_not_inflate_gauge_siblings(self) -> None:
        result = self._translate_result(
            'sum(node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate) by (container)',
            {
                "node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate": {
                    "target": "container.cpu.usage",
                    "transform": "drop_rate",
                },
                "kube_pod_container_resource_requests_cpu_cores": {
                    "target": "k8s.container.cpu_request",
                },
            },
            metric_kinds={
                "container.cpu.usage": "gauge",
                "k8s.container.cpu_request": "gauge",
            },
        )
        # Translate the request series the same way the multi-target panel does
        # by exercising normalize on a mixed panel via a binary-free second expr
        # through the shared pipeline — assert request emit path alone first.
        req = self._translate_result(
            "sum(kube_pod_container_resource_requests_cpu_cores) by (container)",
            {
                "kube_pod_container_resource_requests_cpu_cores": {
                    "target": "k8s.container.cpu_request",
                }
            },
            metric_kinds={"k8s.container.cpu_request": "gauge"},
        )
        self.assertEqual(result.feasibility, "feasible", result.warnings)
        self.assertEqual(req.feasibility, "feasible", req.warnings)
        assert req.esql_query is not None
        self.assertNotIn("SUM_OVER_TIME(k8s.container.cpu_request", req.esql_query)

    def test_mapped_drop_rate_prefers_target_gauge_caps_for_ts(self) -> None:
        """Source recording-rule name may still be typed counter in ES; after
        drop_rate→gauge map, TS decisions must use the *target* gauge caps so
        multi-target fusion is not demoted to FROM (which inflates SUM)."""
        from observability_migration.adapters.source.grafana.panels import (
            _build_multi_target_series_query,
        )

        metric_map = {
            "node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate": {
                "target": "container.cpu.usage",
                "transform": "drop_rate",
            },
            "kube_pod_container_resource_requests_cpu_cores": {
                "target": "k8s.container.cpu_request",
            },
        }
        rule_pack = RulePackConfig()
        rule_pack.metric_map.update(normalize_metric_map(metric_map))
        rule_pack.metric_kinds.update(
            {
                "container.cpu.usage": "gauge",
                "k8s.container.cpu_request": "gauge",
            }
        )
        resolver = SchemaResolver(rule_pack, field_profile="otel")
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            # Leftover Prom recording-rule field still present as counter.
            "node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate": {
                "double": {
                    "aggregatable": True,
                    "searchable": True,
                    "time_series_metric": "counter",
                }
            },
            "container.cpu.usage": {
                "double": {
                    "aggregatable": True,
                    "searchable": True,
                    "time_series_metric": "gauge",
                }
            },
            "k8s.container.cpu_request": {
                "double": {
                    "aggregatable": True,
                    "searchable": True,
                    "time_series_metric": "gauge",
                }
            },
            "k8s.container.name": {
                "keyword": {"aggregatable": True, "searchable": True},
            },
        }

        usage = translate_promql_to_esql(
            'sum(node_namespace_pod_container:container_cpu_usage_seconds_total:sum_rate) by (container)',
            datasource_index="metrics-*",
            panel_type="timeseries",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        req = translate_promql_to_esql(
            "sum(kube_pod_container_resource_requests_cpu_cores) by (container)",
            datasource_index="metrics-*",
            panel_type="timeseries",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        self.assertEqual(usage.feasibility, "feasible", usage.warnings)
        self.assertEqual(req.feasibility, "feasible", req.warnings)
        self.assertTrue(
            (usage.esql_query or "").startswith("TS "),
            usage.esql_query,
        )
        usage.metadata["target_ref_id"] = "A"
        req.metadata["target_ref_id"] = "B"
        merged = _build_multi_target_series_query([usage, req])
        self.assertIsNotNone(merged)
        assert merged is not None
        self.assertTrue(merged["query"].startswith("TS "), merged["query"])
        self.assertNotIn("SUM_OVER_TIME(k8s.container.cpu_request", merged["query"])
        self.assertNotIn("BUCKET(@timestamp", merged["query"])

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
