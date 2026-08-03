# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Mixed metric_map target_index panels emit per-index XY layers."""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana.panels import translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.core.metric_mapping import normalize_metric_map
from observability_migration.targets.kibana.dashboards_api import _cfg_xy


class CrossIndexXyLayerTests(unittest.TestCase):
    def _setup(self, metric_map: dict, fields: list[str]):
        rule_pack = RulePackConfig()
        rule_pack.metric_map.update(normalize_metric_map(metric_map))
        rule_pack.metric_kinds.update({name: "gauge" for name in fields})
        resolver = SchemaResolver(rule_pack, field_profile="otel")
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            name: {
                "double": {
                    "aggregatable": True,
                    "searchable": True,
                    "time_series_metric": "gauge",
                }
            }
            for name in fields
        }
        return rule_pack, resolver

    def test_mixed_target_index_emits_cross_index_layers(self) -> None:
        metric_map = {
            "source_metric_a": {
                "target": "target.field.a",
                "target_index": "metrics-a-*",
            },
            "source_metric_b": {
                "target": "target.field.b",
                "target_index": "metrics-b-*",
            },
        }
        rule_pack, resolver = self._setup(
            metric_map,
            ["target.field.a", "target.field.b"],
        )
        panel = {
            "id": 1,
            "type": "timeseries",
            "title": "Cross Stream",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": "sum(source_metric_a)", "refId": "A", "legendFormat": "a"},
                {"expr": "sum(source_metric_b)", "refId": "B", "legendFormat": "b"},
            ],
        }

        yaml_panel, result = translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )

        self.assertIn(result.status, {"migrated", "migrated_with_warnings"}, result.reasons)
        self.assertTrue(
            any("distinct data streams" in str(reason) for reason in (result.reasons or [])),
            result.reasons,
        )
        esql = yaml_panel.get("esql") or {}
        self.assertEqual(esql.get("type"), "line")
        layers = esql.get("layers") or []
        self.assertEqual(len(layers), 1, esql)
        primary_query = str(esql.get("query") or "")
        layer_query = str(layers[0].get("query") or "")
        combined = f"{primary_query}\n{layer_query}"
        self.assertIn("metrics-a-*", combined)
        self.assertIn("metrics-b-*", combined)
        self.assertTrue(
            ("metrics-a-*" in primary_query and "metrics-b-*" in layer_query)
            or ("metrics-b-*" in primary_query and "metrics-a-*" in layer_query),
            combined,
        )

        native = _cfg_xy("Cross Stream", esql, primary_query)
        native_layers = native.get("layers") or []
        self.assertEqual(len(native_layers), 2, native)
        queries = [
            str((layer.get("data_source") or {}).get("query") or "")
            for layer in native_layers
        ]
        self.assertTrue(any("metrics-a-*" in q for q in queries), queries)
        self.assertTrue(any("metrics-b-*" in q for q in queries), queries)

    def test_same_target_index_still_fuses_single_query(self) -> None:
        metric_map = {
            "source_metric_a": {
                "target": "target.field.a",
                "target_index": "metrics-shared-*",
            },
            "source_metric_b": {
                "target": "target.field.b",
                "target_index": "metrics-shared-*",
            },
        }
        rule_pack, resolver = self._setup(
            metric_map,
            ["target.field.a", "target.field.b"],
        )
        panel = {
            "id": 2,
            "type": "timeseries",
            "title": "Same Stream",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": "sum(source_metric_a)", "refId": "A"},
                {"expr": "sum(source_metric_b)", "refId": "B"},
            ],
        }

        yaml_panel, result = translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )

        self.assertIn(result.status, {"migrated", "migrated_with_warnings"}, result.reasons)
        esql = yaml_panel.get("esql") or {}
        self.assertFalse(esql.get("layers"), esql)
        query = str(esql.get("query") or "")
        self.assertIn("metrics-shared-*", query)
        self.assertNotIn(
            "distinct data streams",
            " ".join(str(r) for r in (result.reasons or [])),
        )


if __name__ == "__main__":
    unittest.main()
