# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Cross-adapter parity for shared metric_map building blocks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from observability_migration.adapters.source.datadog.field_map import FieldMapProfile, load_profile
from observability_migration.adapters.source.datadog.models import MetricQuery, WidgetQuery
from observability_migration.adapters.source.datadog.preflight import build_target_readiness_contract
from observability_migration.adapters.source.grafana.panels import (
    _metric_map_bypass_note,
    build_native_promql_query,
)
from observability_migration.adapters.source.grafana.preflight import build_target_schema_contract
from observability_migration.adapters.source.grafana.rules import RulePackConfig, load_rule_pack_files
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.core.metric_mapping import (
    CLASS_EXACT,
    CLASS_REQUIRES_TRANSFORM,
    normalize_metric_map,
    resolve_metric_map,
)


class MetricMapParityTests(unittest.TestCase):
    def test_shared_resolve_used_by_both_adapters(self):
        entries = normalize_metric_map({"a.b": "x.y"})
        shared = resolve_metric_map("a.b", entries)
        self.assertTrue(shared and shared.applied and shared.target == "x.y")

        pack = RulePackConfig(metric_map=entries)
        grafana = SchemaResolver(pack, field_profile="otel")
        self.assertEqual(grafana.resolve_metric_field("a.b"), "x.y")

        dd = FieldMapProfile(metric_map=entries)
        self.assertEqual(dd.map_metric("a.b"), "x.y")

    def test_class2_applies_on_both_adapters(self):
        entries = normalize_metric_map(
            {
                "src.metric": {
                    "target": "dst.metric",
                    "transform": "to_rate",
                }
            }
        )
        shared = resolve_metric_map("src.metric", entries)
        self.assertEqual(shared.klass, CLASS_REQUIRES_TRANSFORM)
        self.assertTrue(shared.applied)
        self.assertEqual(shared.target, "dst.metric")
        self.assertEqual(shared.gap_reason, "")

        grafana = SchemaResolver(RulePackConfig(metric_map=entries), field_profile="otel")
        self.assertEqual(grafana.resolve_metric_field("src.metric"), "dst.metric")
        self.assertFalse(grafana.metric_map_gaps())
        self.assertEqual(grafana.metric_map_applied()["src.metric"], "dst.metric")
        self.assertTrue(grafana.metric_map_warnings())

        dd = FieldMapProfile(metric_map=entries)
        self.assertEqual(dd.map_metric("src.metric"), "dst.metric")
        self.assertFalse(dd.metric_map_gaps())
        self.assertEqual(dd.metric_map_applied()["src.metric"], "dst.metric")
        self.assertTrue(dd.metric_map_warnings())

    def test_unit_scale_applies_on_both_adapters(self):
        entries = normalize_metric_map(
            {
                "src.metric": {
                    "target": "dst.metric",
                    "unit_scale": 0.001,
                }
            }
        )
        shared = resolve_metric_map("src.metric", entries)
        self.assertEqual(shared.klass, CLASS_REQUIRES_TRANSFORM)
        self.assertTrue(shared.applied)
        self.assertEqual(shared.target, "dst.metric")
        self.assertEqual(shared.gap_reason, "")

        grafana = SchemaResolver(RulePackConfig(metric_map=entries), field_profile="otel")
        self.assertEqual(grafana.resolve_metric_field("src.metric"), "dst.metric")
        self.assertFalse(grafana.metric_map_gaps())

        dd = FieldMapProfile(metric_map=entries)
        self.assertEqual(dd.map_metric("src.metric"), "dst.metric")
        self.assertFalse(dd.metric_map_gaps())

    def test_grafana_rule_pack_loads_metric_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pack.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "query": {
                            "metric_map": {
                                "container_memory_working_set_bytes": "container.memory.working_set",
                                "net_rx": {
                                    "target": "k8s.pod.network.io",
                                    "attribute_filter": {"network.direction": "receive"},
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            pack = load_rule_pack_files([str(path)])
            self.assertIn("container_memory_working_set_bytes", pack.metric_map)
            exact = resolve_metric_map("container_memory_working_set_bytes", pack.metric_map)
            self.assertEqual(exact.klass, CLASS_EXACT)
            class2 = resolve_metric_map("net_rx", pack.metric_map)
            self.assertEqual(class2.klass, CLASS_REQUIRES_TRANSFORM)
            self.assertTrue(class2.applied)
            self.assertEqual(class2.target, "k8s.pod.network.io")

    def test_datadog_yaml_profile_loads_rich_metric_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "name": "custom",
                        "metric_map": {
                            "system.cpu.user": "system.cpu.user.pct",
                            "system.net.bytes_rcvd": {
                                "target": "system.network.in.bytes",
                                "transform": "to_rate",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            profile = load_profile(str(path))
            self.assertEqual(profile.map_metric("system.cpu.user"), "system.cpu.user.pct")
            self.assertEqual(profile.map_metric("system.net.bytes_rcvd"), "system.network.in.bytes")
            self.assertFalse(profile.metric_map_gaps())
            self.assertTrue(profile.metric_map_warnings())

    def test_grafana_contract_records_mapped_from(self):
        """Grafana's required_target_contract shows which source metric a rename came from."""
        resolver = SchemaResolver(
            RulePackConfig(
                metric_map=normalize_metric_map(
                    {"http_requests_total": "http.server.request.duration"}
                )
            ),
            field_profile="otel",
        )
        result = SimpleNamespace(
            panel_results=[
                SimpleNamespace(
                    query_ir={
                        "source_metric": "http_requests_total",
                        "source_expression": "sum(http_requests_total)",
                        "source_type": "FROM",
                    },
                    reasons=[],
                )
            ]
        )
        contract = build_target_schema_contract([result], resolver=resolver)
        entry = contract["required_fields"]["http.server.request.duration"]
        self.assertEqual(entry["source_fields"], ["http_requests_total"])
        self.assertEqual(entry["mapped_from"], "http_requests_total")
        self.assertIn("metric_map", contract)
        self.assertEqual(contract["metric_map"]["totals"]["applied"], 1)

        # Unchanged (no rename) fields must not claim a mapped_from.
        unchanged_result = SimpleNamespace(
            panel_results=[
                SimpleNamespace(
                    query_ir={
                        "source_metric": "up",
                        "source_expression": "sum(up)",
                        "source_type": "FROM",
                    },
                    reasons=[],
                )
            ]
        )
        unchanged_contract = build_target_schema_contract(
            [unchanged_result],
            resolver=SchemaResolver(RulePackConfig()),
        )
        unchanged_entry = unchanged_contract["required_fields"]["up"]
        self.assertNotIn("mapped_from", unchanged_entry)

    def test_datadog_contract_records_mapped_from_for_metric_map_rename(self):
        """Datadog's target_readiness_contract mirrors Grafana's mapped_from field (parity)."""
        field_map = FieldMapProfile(
            name="test",
            metric_map=normalize_metric_map({"system.cpu.user": "system.cpu.user.pct"}),
        )
        dashboard = SimpleNamespace(
            widgets=[
                SimpleNamespace(
                    id="1",
                    children=[],
                    queries=[
                        WidgetQuery(
                            metric_query=MetricQuery(metric="system.cpu.user"),
                        )
                    ],
                )
            ]
        )
        contract = build_target_readiness_contract([dashboard], field_map)
        entry = contract["required_fields"]["system.cpu.user.pct"]
        self.assertEqual(entry["mapped_from"], "system.cpu.user")
        self.assertIn("metric_map", contract)

        # A field with no dots survives the default dot->underscore mapping
        # unchanged, so it must not claim a mapped_from either.
        dashboard_unchanged = SimpleNamespace(
            widgets=[
                SimpleNamespace(
                    id="1",
                    children=[],
                    queries=[WidgetQuery(metric_query=MetricQuery(metric="uptime"))],
                )
            ]
        )
        unchanged_contract = build_target_readiness_contract([dashboard_unchanged], field_map)
        (unchanged_entry,) = unchanged_contract["required_fields"].values()
        self.assertNotIn("mapped_from", unchanged_entry)

    def test_native_promql_bypass_note_flags_class2_metric(self):
        """Native PROMQL warns for class-2 metric_map entries that need ES|QL."""
        pack = RulePackConfig(
            metric_map=normalize_metric_map(
                {
                    "http_requests_total": "http.server.request.duration",
                    "net_rx": {
                        "target": "k8s.pod.network.io",
                        "attribute_filter": {"network.direction": "receive"},
                    },
                }
            )
        )
        note = _metric_map_bypass_note(["http_requests_total", "net_rx", "up"], pack)
        self.assertIsNotNone(note)
        self.assertIn("net_rx", note)
        self.assertNotIn("http_requests_total", note)
        self.assertIn("class-2", note)
        self.assertIn("--translation-mode esql", note)
        self.assertIsNone(_metric_map_bypass_note(["up"], pack))
        self.assertIsNone(_metric_map_bypass_note(["http_requests_total"], RulePackConfig()))

    def test_native_promql_class1_metric_map_rewrites_selector(self):
        resolver = SchemaResolver(
            RulePackConfig(
                metric_map=normalize_metric_map(
                    {"http_requests_total": "http.server.request.duration"}
                )
            ),
            field_profile="otel",
        )
        query = build_native_promql_query(
            "sum(http_requests_total)",
            index="metrics-*",
            kibana_type="metric",
            resolver=resolver,
        )
        self.assertIn("http.server.request.duration", query)
        self.assertNotIn("http_requests_total", query)


if __name__ == "__main__":
    unittest.main()
