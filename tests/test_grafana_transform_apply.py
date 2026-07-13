# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for applying Grafana panel transformations into ES|QL."""

from __future__ import annotations

import json
import pathlib
import unittest
from types import SimpleNamespace

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.transforms import (
    apply_transformations_to_esql,
    build_redesign_tasks,
    extract_transformations,
    mark_applied_transformations,
)


class TransformApplyUnitTests(unittest.TestCase):
    def test_reduce_row_mean_and_organize_exclude(self):
        query = (
            "TS metrics-*\n"
            "| STATS Real_Linux = AVG(a), Real_Windows = AVG(b), Requests = SUM(c) "
            "BY time_bucket = TBUCKET(5 minute)\n"
            "| KEEP time_bucket, Real_Linux, Real_Windows, Requests\n"
            "| SORT time_bucket ASC"
        )
        translation = SimpleNamespace(
            esql_query=query,
            output_metric_field="Real_Linux",
            output_group_fields=["time_bucket"],
            metadata={
                "multi_series_metric_fields": ["Real_Linux", "Real_Windows", "Requests"],
                "multi_series_metric_labels": {
                    "Real_Linux": "Real Linux",
                    "Real_Windows": "Real Windows",
                    "Requests": "Requests",
                },
            },
        )
        panel = {
            "targets": [
                {"refId": "A", "legendFormat": "Real Linux"},
                {"refId": "B", "legendFormat": "Real Windows"},
                {"refId": "C", "legendFormat": "Requests"},
            ],
            "transformations": [
                {
                    "id": "calculateField",
                    "options": {
                        "alias": "Real",
                        "mode": "reduceRow",
                        "reduce": {
                            "include": ["Real Linux", "Real Windows"],
                            "reducer": "mean",
                        },
                    },
                },
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {
                            "Real Linux": True,
                            "Real Windows": True,
                            "Time": True,
                        },
                        "renameByName": {},
                    },
                },
            ],
        }
        rewritten, result = apply_transformations_to_esql(panel, translation, esql_query=query)
        self.assertEqual(result.applied_indices, [0, 1])
        self.assertIn("EVAL", rewritten)
        self.assertIn("Real", rewritten)
        # Mean is inlined — no __tx_* helpers that can leak after KEEP strip.
        self.assertNotIn("__tx_sum", rewritten)
        self.assertIn("COALESCE(Real_Linux, 0) + COALESCE(Real_Windows, 0)", rewritten)
        # Pipeline order: STATS → EVAL(Real) → KEEP (without source OS cols) → SORT
        stages = [line.strip() for line in rewritten.splitlines() if line.strip().startswith("|")]
        eval_idx = next(i for i, s in enumerate(stages) if s.upper().startswith("| EVAL"))
        keep_idx = next(i for i, s in enumerate(stages) if s.upper().startswith("| KEEP"))
        self.assertLess(eval_idx, keep_idx, rewritten)
        keep_body = stages[keep_idx]
        self.assertIn("Real", keep_body)
        self.assertNotIn("Real_Linux", keep_body)
        self.assertNotIn("Real_Windows", keep_body)
        self.assertIn("Real", result.updated_metric_fields)
        self.assertNotIn("Real_Linux", result.updated_metric_fields)
        self.assertEqual(result.updated_metric_label_hints.get("Real"), "Real")

    def test_replace_fields_mean_all_metrics(self):
        query = (
            "TS metrics-*\n"
            "| STATS Linux = AVG(a), Windows = AVG(b) BY time_bucket = TBUCKET(5 minute)\n"
            "| KEEP time_bucket, Linux, Windows\n"
            "| SORT time_bucket ASC"
        )
        translation = SimpleNamespace(
            esql_query=query,
            output_metric_field="Linux",
            output_group_fields=["time_bucket"],
            metadata={
                "multi_series_metric_fields": ["Linux", "Windows"],
                "multi_series_metric_labels": {"Linux": "Linux", "Windows": "Windows"},
            },
        )
        panel = {
            "targets": [
                {"refId": "A", "legendFormat": "Linux"},
                {"refId": "B", "legendFormat": "Windows"},
            ],
            "transformations": [
                {
                    "id": "calculateField",
                    "options": {
                        "alias": "CPU usage in %",
                        "mode": "reduceRow",
                        "reduce": {"reducer": "mean"},
                        "replaceFields": True,
                    },
                }
            ],
        }
        rewritten, result = apply_transformations_to_esql(panel, translation, esql_query=query)
        self.assertEqual(result.applied_indices, [0])
        self.assertEqual(len(result.updated_metric_fields), 1)
        metric = result.updated_metric_fields[0]
        self.assertIn(metric, rewritten)
        keep = rewritten.split("| KEEP")[-1]
        self.assertNotIn("Linux", keep)
        self.assertNotIn("Windows", keep)
        self.assertEqual(result.updated_metric_label_hints[metric], "CPU usage in %")

    def test_unresolved_include_skips_transform(self):
        query = (
            "TS metrics-*\n"
            "| STATS Requests = SUM(c) BY time_bucket = TBUCKET(5 minute)\n"
            "| KEEP time_bucket, Requests"
        )
        translation = SimpleNamespace(
            esql_query=query,
            output_metric_field="Requests",
            output_group_fields=["time_bucket"],
            metadata={
                "multi_series_metric_fields": ["Requests"],
                "multi_series_metric_labels": {"Requests": "Requests"},
            },
        )
        panel = {
            "targets": [{"refId": "A", "legendFormat": "Requests"}],
            "transformations": [
                {
                    "id": "calculateField",
                    "options": {
                        "alias": "Real",
                        "mode": "reduceRow",
                        "reduce": {
                            "include": ["Missing Series"],
                            "reducer": "mean",
                        },
                    },
                }
            ],
        }
        rewritten, result = apply_transformations_to_esql(panel, translation, esql_query=query)
        self.assertEqual(result.applied_indices, [])
        self.assertTrue(result.skipped)
        self.assertEqual(rewritten, query)

    def test_sort_by_stays_before_limit_when_limit_transform_runs_first(self):
        query = (
            "TS metrics-*\n"
            "| STATS Requests = SUM(c) BY time_bucket = TBUCKET(5 minute)\n"
            "| KEEP time_bucket, Requests"
        )
        translation = SimpleNamespace(
            esql_query=query,
            output_metric_field="Requests",
            output_group_fields=["time_bucket"],
            metadata={
                "multi_series_metric_fields": ["Requests"],
                "multi_series_metric_labels": {"Requests": "Requests"},
            },
        )
        panel = {
            "targets": [{"refId": "A", "legendFormat": "Requests"}],
            "transformations": [
                {"id": "limit", "options": {"limitValue": 10}},
                {
                    "id": "sortBy",
                    "options": {"sort": [{"field": "Requests", "desc": True}]},
                },
            ],
        }
        rewritten, result = apply_transformations_to_esql(panel, translation, esql_query=query)
        self.assertEqual(result.applied_indices, [0, 1])
        stages = [line.strip() for line in rewritten.splitlines() if line.strip().startswith("|")]
        sort_idx = next(i for i, stage in enumerate(stages) if stage.upper().startswith("| SORT"))
        limit_idx = next(i for i, stage in enumerate(stages) if stage.upper().startswith("| LIMIT"))
        self.assertLess(sort_idx, limit_idx, rewritten)

    def test_mark_applied_filters_redesign_tasks(self):
        panel = {
            "transformations": [
                {"id": "calculateField", "options": {"mode": "reduceRow", "alias": "Real"}},
                {"id": "merge", "options": {}},
            ]
        }
        entries = mark_applied_transformations(extract_transformations(panel), [0])
        tasks = build_redesign_tasks("Panel", "Dash", entries)
        self.assertEqual([t["transform_id"] for t in tasks], ["merge"])
        self.assertEqual(entries[0]["status"], "applied_in_esql")


class TransformApplyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.rp = rules.RulePackConfig(native_promql=False)
        self.resolver = schema.SchemaResolver(self.rp)

    def translate(self, panel):
        return panels.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=self.rp,
            resolver=self.resolver,
        )

    def test_k8s_network_saturation_applies_chained_transforms(self):
        """Corpus panel with 2x calculateField + organize on a fully fused
        multi-target query — the happy path for transform application."""
        dashboard = json.loads(
            (
                pathlib.Path(__file__).resolve().parent.parent
                / "infra/grafana/dashboards/k8s-views-global.json"
            ).read_text()
        )

        def walk(items):
            for panel in items or []:
                yield panel
                yield from walk(panel.get("panels"))

        panel = next(
            p
            for p in walk(dashboard.get("panels"))
            if p.get("title") == "Network Saturation - Packets dropped"
        )
        yaml_panel, result = self.translate(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(
            result.applied_transform_indices,
            [0, 1, 2],
            "expected both calculateField transforms and organize to apply",
        )
        query = yaml_panel["esql"]["query"]
        metric_fields = [m["field"] for m in yaml_panel["esql"].get("metrics", [])]
        self.assertIn("Packets_dropped_receive", metric_fields)
        self.assertIn("Packets_dropped_transmit", metric_fields)
        # Source Linux/Windows series are excluded by organize after the means.
        keep_tail = query.split("| KEEP")[-1] if "| KEEP" in query else query
        self.assertNotIn("Linux_Packets_dropped_receive", keep_tail)
        self.assertNotIn("Windows_Packets_dropped_receive", keep_tail)
        self.assertTrue(
            any("Applied Grafana transformation 'calculateField'" in r for r in result.reasons),
            result.reasons,
        )


if __name__ == "__main__":
    unittest.main()
