# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Operator guidance for --data-view / --esql-index selection (issue #284).

Covers both timelines:
- migrate-first (no --es-url yet): commit to a planned concrete target
- data-first (--es-url): warn on mixed wildcards / UI-vs-query divergence
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from observability_migration.adapters.source.grafana import cli as grafana_cli
from observability_migration.adapters.source.grafana import metrics_target_guidance as mtg


class BackendFamilyTests(unittest.TestCase):
    def test_classifies_common_metric_stream_families(self):
        self.assertEqual(mtg.backend_family("metrics-prometheus-default"), "prometheus")
        self.assertEqual(mtg.backend_family("metrics-alloy.prometheus-default"), "prometheus")
        self.assertEqual(mtg.backend_family("metrics-datadog-default"), "datadog")
        self.assertEqual(mtg.backend_family("metrics-otel-default"), "otel")
        self.assertEqual(mtg.backend_family("metrics-generic-default"), "generic")
        self.assertEqual(mtg.backend_family("metrics-foo-bar"), "other")


class AssessMetricsTargetTests(unittest.TestCase):
    def test_migrate_first_without_es_url_warns_planned_target(self):
        guidance = mtg.assess_metrics_target(
            data_view="metrics-*",
            esql_index="",
            es_url="",
            concrete_streams=None,
        )
        text = "\n".join(guidance.messages)
        self.assertTrue(guidance.messages)
        self.assertIn("migrate-first", text.lower())
        self.assertIn("metrics-*", text)
        self.assertIn("--es-url", text)
        self.assertIn("concrete", text.lower())

    def test_divergent_data_view_and_esql_index_warns_ui_vs_query(self):
        guidance = mtg.assess_metrics_target(
            data_view="metrics-*",
            esql_index="metrics-prometheus-default",
            es_url="https://es.example",
            concrete_streams=["metrics-prometheus-default"],
        )
        text = "\n".join(guidance.messages)
        self.assertIn("metrics-prometheus-default", text)
        self.assertIn("metrics-*", text)
        self.assertRegex(text.lower(), r"ui|data.view|bind")
        self.assertRegex(text.lower(), r"query|esql-index")

    def test_mixed_backends_under_wildcard_recommends_concrete_stream(self):
        guidance = mtg.assess_metrics_target(
            data_view="metrics-*",
            esql_index="metrics-*",
            es_url="https://es.example",
            concrete_streams=[
                "metrics-prometheus-default",
                "metrics-datadog-default",
                "metrics-otel-default",
            ],
        )
        text = "\n".join(guidance.messages)
        self.assertIn("metrics-prometheus-default", text)
        self.assertIn("metrics-datadog-default", text)
        self.assertRegex(text.lower(), r"mixed|multiple")
        self.assertIn("--esql-index", text)
        self.assertIn("--data-view", text)

    def test_single_concrete_stream_under_wildcard_still_suggests_pinning(self):
        guidance = mtg.assess_metrics_target(
            data_view="metrics-*",
            esql_index="",
            es_url="https://es.example",
            concrete_streams=["metrics-prometheus-default"],
        )
        text = "\n".join(guidance.messages)
        self.assertIn("metrics-prometheus-default", text)
        self.assertRegex(text.lower(), r"pin|set both|concrete")

    def test_both_flags_already_concrete_emits_no_mixed_warning(self):
        guidance = mtg.assess_metrics_target(
            data_view="metrics-prometheus-default",
            esql_index="metrics-prometheus-default",
            es_url="https://es.example",
            concrete_streams=["metrics-prometheus-default"],
        )
        text = "\n".join(guidance.messages).lower()
        self.assertNotRegex(text, r"mixed|multiple backend")
        self.assertFalse(guidance.blocking)

    def test_tsdb_conflict_fields_warn_as_index_readiness(self):
        guidance = mtg.assess_metrics_target(
            data_view="metrics-*",
            esql_index="metrics-*",
            es_url="https://es.example",
            concrete_streams=["metrics-prometheus-default", "metrics-datadog-default"],
            tsdb_conflict_fields=["host.name", "service"],
        )
        text = "\n".join(guidance.messages)
        self.assertIn("host.name", text)
        self.assertRegex(text.lower(), r"tsdb|dimension|metric|readiness|index")


class PrintAndCliWiringTests(unittest.TestCase):
    def test_print_metrics_target_guidance_writes_operator_banner(self):
        guidance = mtg.MetricsTargetGuidance(
            query_index="metrics-*",
            data_view="metrics-*",
            messages=["Pin --esql-index to a concrete stream."],
            blocking=False,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            mtg.print_metrics_target_guidance(guidance)
        out = buf.getvalue()
        self.assertIn("WARNING", out)
        self.assertIn("metrics target", out.lower())
        self.assertIn("Pin --esql-index", out)

    def test_cli_helper_uses_resolver_candidates_when_es_url_set(self):
        args = SimpleNamespace(
            data_view="metrics-*",
            esql_index="",
            es_url="https://es.example",
        )
        resolver = mock.Mock()
        resolver.concrete_index_candidates.return_value = [
            "metrics-prometheus-default",
            "metrics-datadog-default",
        ]
        resolver.tsdb_conflict_fields.return_value = []
        buf = io.StringIO()
        with redirect_stdout(buf):
            grafana_cli._print_metrics_target_operator_guidance(args, resolver)
        out = buf.getvalue()
        self.assertIn("metrics-prometheus-default", out)
        self.assertIn("metrics-datadog-default", out)
        resolver.concrete_index_candidates.assert_called()

    def test_offline_migrate_stdout_includes_migrate_first_warning(self):
        dashboard = {
            "uid": "mtg-offline",
            "title": "Migrate First Fixture",
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "title": "HTTP",
                    "targets": [{"refId": "A", "expr": "sum(rate(http_requests_total[5m]))"}],
                    "fieldConfig": {"defaults": {}, "overrides": []},
                    "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                }
            ],
            "templating": {"list": []},
            "time": {"from": "now-1h", "to": "now"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            in_dir = root / "in"
            out_dir = root / "out"
            in_dir.mkdir()
            (in_dir / "d.json").write_text(json.dumps(dashboard), encoding="utf-8")
            argv = [
                "grafana-migrate",
                "--source",
                "files",
                "--input-dir",
                str(in_dir),
                "--output-dir",
                str(out_dir),
                "--assets",
                "dashboards",
                "--data-view",
                "metrics-*",
                "--translation-mode",
                "native",
                "--field-profile",
                "otel",
            ]
            buf = io.StringIO()
            original = list(sys.argv)
            try:
                sys.argv = argv
                with redirect_stdout(buf):
                    grafana_cli.main()
            finally:
                sys.argv = original
            out = buf.getvalue()
            self.assertIn("metrics target / data-plane readiness", out)
            self.assertIn("migrate-first", out.lower())
            self.assertIn("metrics-*", out)


class FieldCacheConflictHelperTests(unittest.TestCase):
    def test_tsdb_conflict_fields_from_field_cache(self):
        conflicts = mtg.tsdb_conflict_fields_from_field_cache(
            {
                "host.name": {
                    "keyword": {"type": "keyword", "time_series_dimension": True},
                    "long": {"type": "long", "time_series_metric": "gauge"},
                },
                "ok": {"keyword": {"type": "keyword", "time_series_dimension": True}},
            }
        )
        self.assertEqual(conflicts, ["host.name"])
