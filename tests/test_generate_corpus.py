# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
import pathlib
import tempfile
import unittest

from observability_migration.adapters.source.grafana import corpus as generate_corpus


class CorpusGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.profile = {}
        self.rule_pack = generate_corpus.migrate.RulePackConfig()
        self.resolver = generate_corpus.migrate.SchemaResolver(self.rule_pack)

    def test_collect_promql_demand_extracts_metrics_and_labels(self):
        manifest = generate_corpus.CorpusManifest()
        expr = 'sum(rate(alertmanager_notifications_total{instance=~"$instance"}[5m])) by (integration,status)'
        metrics = generate_corpus.collect_demand_from_promql(
            expr,
            "Alertmanager::Notifications",
            manifest,
            self.resolver,
            self.rule_pack,
            self.profile,
        )
        self.assertEqual(metrics, {"alertmanager_notifications_total"})
        metric = manifest.metrics["alertmanager_notifications_total"]
        self.assertEqual(metric.kind, "counter")
        self.assertIn("integration", metric.labels)
        self.assertIn("status", metric.labels)
        self.assertIn("service.instance.id", metric.labels)

    def test_rate_usage_upgrades_ambiguous_metric_to_counter(self):
        manifest = generate_corpus.CorpusManifest()
        generate_corpus.collect_demand_from_promql(
            "sum(otelcol_process_cpu_seconds) by (exporter)",
            "AWS::CPU",
            manifest,
            self.resolver,
            self.rule_pack,
            self.profile,
        )
        self.assertEqual(manifest.metrics["otelcol_process_cpu_seconds"].kind, "gauge")

        generate_corpus.collect_demand_from_promql(
            "sum(rate(otelcol_process_cpu_seconds[5m])) by (exporter)",
            "AWS::CPU Rate",
            manifest,
            self.resolver,
            self.rule_pack,
            self.profile,
        )
        self.assertEqual(manifest.metrics["otelcol_process_cpu_seconds"].kind, "counter")

    def test_failed_report_scope_limits_to_failed_panels(self):
        report = {
            "dashboards": [
                {
                    "title": "Alertmanager",
                    "panels": [
                        {
                            "title": "Failed panel",
                            "promql": 'count(alertmanager_build_info{instance=~"$instance"})',
                            "esql": "",
                        },
                        {
                            "title": "Healthy panel",
                            "promql": 'sum(node_memory_MemTotal_bytes{instance=~"$instance"})',
                            "esql": "",
                        },
                    ],
                }
            ],
            "validation": {
                "records": [
                    {
                        "dashboard": "Alertmanager",
                        "panel": "Failed panel",
                        "status": "fail",
                        "analysis": {
                            "unknown_columns": [
                                {"name": "alertmanager_build_info", "role": "metric", "suggested_fields": []},
                                {"name": "status", "role": "label", "suggested_fields": []},
                            ],
                            "unknown_indexes": [],
                            "counter_mismatch_metrics": [],
                        },
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = pathlib.Path(tmpdir) / "migration_report.json"
            report_path.write_text(json.dumps(report))
            manifest = generate_corpus.CorpusManifest()
            generate_corpus.collect_demand_from_report(
                report_path,
                manifest,
                self.resolver,
                self.rule_pack,
                self.profile,
                scope="failed",
            )
        self.assertIn("alertmanager_build_info", manifest.metrics)
        self.assertNotIn("node_memory_MemTotal_bytes", manifest.metrics)
        self.assertIn("status", manifest.labels)

    def test_metric_generation_keeps_counters_monotonic(self):
        manifest = generate_corpus.CorpusManifest(
            metrics={
                "alertmanager_notifications_total": generate_corpus.MetricDemand(
                    name="alertmanager_notifications_total",
                    kind="counter",
                    labels={"service.instance.id", "integration"},
                )
            }
        )
        docs, _ = generate_corpus.generate_metric_documents(
            manifest,
            self.profile,
            points=5,
            step_seconds=60,
            cap=3,
        )
        values = {}
        for doc in docs:
            series_key = doc["service"]["instance"]["id"] + "|" + doc["integration"]
            values.setdefault(series_key, []).append(doc["alertmanager_notifications_total"])
        self.assertTrue(values)
        for series in values.values():
            self.assertEqual(series, sorted(series))

    def test_metrics_template_marks_counter_and_dimensions(self):
        manifest = generate_corpus.CorpusManifest(
            metrics={
                "foo_total": generate_corpus.MetricDemand(
                    name="foo_total",
                    kind="counter",
                    labels={"service.instance.id", "status"},
                ),
                "bar_gauge": generate_corpus.MetricDemand(
                    name="bar_gauge",
                    kind="gauge",
                    labels={"service.instance.id"},
                ),
            }
        )
        template = generate_corpus._build_metrics_template(
            "metrics-prometheusreceiver.otel-synthetic",
            manifest,
            {"foo_total", "bar_gauge"},
            {"service.instance.id", "status"},
        )
        props = template["template"]["mappings"]["properties"]
        self.assertEqual(props["foo_total"]["time_series_metric"], "counter")
        self.assertEqual(props["bar_gauge"]["time_series_metric"], "gauge")
        self.assertTrue(props["service"]["properties"]["instance"]["properties"]["id"]["time_series_dimension"])

    def test_log_generation_includes_search_terms(self):
        manifest = generate_corpus.CorpusManifest(
            logs=generate_corpus.LogDemand(
                fields={"status", "service.name"},
                search_terms={"error", "timeout"},
                panels={"Loki::Errors"},
            )
        )
        docs, fields = generate_corpus.generate_log_documents(
            manifest,
            self.profile,
            points=3,
            step_seconds=60,
        )
        self.assertEqual(len(docs), 6)
        self.assertIn("status", fields)
        self.assertTrue(any("error timeout" in doc["message"] for doc in docs))


if __name__ == "__main__":
    unittest.main()
