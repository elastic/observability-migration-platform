# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from observability_migration.adapters.source.grafana import cli, manifest, preflight


class GrafanaPreflightReportTests(unittest.TestCase):
    def test_serverless_cluster_health_410_is_reported_as_unsupported_not_error(self):
        response = mock.Mock(status_code=410)

        with mock.patch.object(preflight.requests, "get", return_value=response):
            readiness = preflight.probe_target_readiness(
                "https://example.es",
                required_index_patterns=[],
                es_api_key="test-key",
            )

        self.assertEqual(readiness["status"], "ok")
        self.assertEqual(readiness["errors"], [])
        self.assertEqual(readiness["cluster_health"]["status"], "serverless")
        self.assertTrue(readiness["cluster_health"]["unsupported"])

    def test_static_analysis_summary_does_not_claim_green_panels_are_deployment_ready(self):
        panel = SimpleNamespace(
            readiness="",
            verification_packet={
                "semantic_gate": "Green",
                "source_execution": {"status": "not_configured"},
            },
        )
        result = SimpleNamespace(total_panels=1, panel_results=[panel])

        report = preflight.build_preflight_report(
            [result],
            validation_summary={},
            validation_records=[],
            verification_payload={},
            schema_contract={"required_indexes": {}, "required_fields": {}, "counter_expectations": {}, "totals": {}},
            source_urls_configured=False,
            target_url_configured=False,
        )

        summary = report["customer_action_summary"]
        self.assertEqual(report["summary"]["readiness"]["ready"], 0)
        self.assertIn("Panels: 1 (1 Green by static analysis)", summary)
        self.assertNotIn("ready for deployment", summary)

    def test_serverless_action_summary_does_not_report_zero_data_nodes(self):
        panel = SimpleNamespace(
            readiness="elastic_ready",
            verification_packet={
                "semantic_gate": "Green",
                "source_execution": {"status": "not_configured"},
            },
        )
        result = SimpleNamespace(total_panels=1, panel_results=[panel])

        report = preflight.build_preflight_report(
            [result],
            validation_summary={},
            validation_records=[],
            verification_payload={},
            schema_contract={"required_indexes": {}, "required_fields": {}, "counter_expectations": {}, "totals": {}},
            source_urls_configured=False,
            target_url_configured=True,
            target_readiness={
                "cluster_health": {
                    "status": "serverless",
                    "unsupported": True,
                    "message": "Cluster health API is not available on Elasticsearch Serverless.",
                },
            },
        )

        summary = report["customer_action_summary"]
        self.assertIn("Target cluster: SERVERLESS (cluster health API not available)", summary)
        self.assertNotIn("0 data nodes", summary)
        self.assertNotIn("0 active shards", summary)

    def test_schema_contract_uses_source_fields_not_derived_output_aliases(self):
        panel = SimpleNamespace(
            query_ir={
                "target_index": "metrics-*",
                "source_type": "TS",
                "metric": "http_requests_total",
                "group_labels": ["service.name"],
                "label_filters": ['service.name=~"api"', 'env="prod"'],
                "output_metric_field": "computed_rate",
                "output_group_fields": ["time_bucket", "service"],
                "semantic_losses": [],
            },
            reasons=[],
        )
        result = SimpleNamespace(inventory={}, panel_results=[panel])

        contract = preflight.build_target_schema_contract([result])

        self.assertEqual(contract["required_indexes"], {"metrics-*": 1})
        self.assertEqual(set(contract["required_fields"]), {"http_requests_total", "service.name", "env"})
        self.assertEqual(contract["required_fields"]["http_requests_total"]["roles"], ["metric"])
        self.assertEqual(contract["required_fields"]["service.name"]["roles"], ["filter", "group_by"])
        self.assertEqual(contract["required_fields"]["env"]["roles"], ["filter"])
        self.assertEqual(set(contract["counter_expectations"]), {"http_requests_total"})
        self.assertNotIn("computed_rate", contract["required_fields"])
        self.assertNotIn("time_bucket", contract["required_fields"])
        self.assertNotIn("service", contract["required_fields"])

    def test_schema_contract_ignores_computed_value_alias_when_source_expression_has_metrics(self):
        panel = SimpleNamespace(
            query_ir={
                "target_index": "metrics-*",
                "source_type": "TS",
                "metric": "computed_value",
                "source_expression": "sum(rate(foo_total[5m])) / sum(rate(bar_total[5m]))",
                "output_metric_field": "computed_value",
                "output_group_fields": ["time_bucket"],
                "semantic_losses": [],
            },
            reasons=[],
        )
        result = SimpleNamespace(inventory={}, panel_results=[panel])

        contract = preflight.build_target_schema_contract([result])

        self.assertEqual(set(contract["required_fields"]), {"foo_total", "bar_total"})
        self.assertEqual(set(contract["counter_expectations"]), {"foo_total", "bar_total"})
        self.assertNotIn("computed_value", contract["required_fields"])
        self.assertNotIn("computed_value", contract["counter_expectations"])

    def test_schema_contract_does_not_treat_histogram_bucket_label_as_metric(self):
        panel = SimpleNamespace(
            query_ir={
                "target_index": "metrics-*",
                "source_type": "TS",
                "source_expression": (
                    "histogram_quantile(0.99, "
                    "sum by (le) (rate(http_request_duration_seconds_bucket[5m])))"
                ),
                "output_metric_field": "value",
                "output_group_fields": ["step"],
                "semantic_losses": [],
            },
            reasons=[],
        )
        result = SimpleNamespace(inventory={}, panel_results=[panel])

        contract = preflight.build_target_schema_contract([result])

        self.assertEqual(set(contract["required_fields"]), {"http_request_duration_seconds_bucket"})
        self.assertNotIn("le", contract["required_fields"])
        self.assertNotIn("histogram_quantile", contract["required_fields"])
        self.assertNotIn("sum", contract["required_fields"])

    def test_counter_expectations_ignore_gauge_like_fields_in_ts_queries(self):
        panel = SimpleNamespace(
            query_ir={
                "target_index": "metrics-*",
                "source_type": "TS",
                "source_expression": (
                    "rate(container_cpu_usage_seconds_total[1m]) "
                    "/ kube_pod_container_resource_limits_cpu_cores"
                ),
                "output_metric_field": "value",
                "output_group_fields": ["step"],
                "semantic_losses": [],
            },
            reasons=[],
        )
        result = SimpleNamespace(inventory={}, panel_results=[panel])

        contract = preflight.build_target_schema_contract([result])

        self.assertEqual(
            set(contract["required_fields"]),
            {
                "container_cpu_usage_seconds_total",
                "kube_pod_container_resource_limits_cpu_cores",
            },
        )
        self.assertEqual(set(contract["counter_expectations"]), {"container_cpu_usage_seconds_total"})

    def test_schema_contract_checks_native_profile_resolved_target_fields(self):
        class NativeResolver:
            _index_pattern = "metrics-native-*"

            def schema_profile(self):
                return "prometheus_native"

            def discovery_status(self):
                return {"status": "ok", "error": "", "field_count": 3}

            def resolve_metric_field(self, metric_name, *, prefer=None):
                return f"metrics.{metric_name}"

            def resolve_label(self, label):
                return f"labels.{label}"

            def field_exists(self, field_name):
                return field_name in {
                    "metrics.http_requests_total",
                    "labels.service",
                    "labels.env",
                }

            def field_type(self, field_name):
                return "double" if field_name.startswith("metrics.") else "keyword"

            def is_counter(self, metric_name):
                return metric_name == "http_requests_total"

        panel = SimpleNamespace(
            query_ir={
                "target_index": "metrics-native-*",
                "source_type": "TS",
                "metric": "http_requests_total",
                "group_labels": ["service"],
                "label_filters": ['env="prod"'],
                "output_metric_field": "computed_rate",
                "semantic_losses": [],
            },
            reasons=[],
        )
        result = SimpleNamespace(inventory={}, panel_results=[panel])

        contract = preflight.build_target_schema_contract([result], NativeResolver())

        self.assertEqual(contract["schema_profile"], "prometheus_native")
        self.assertEqual(contract["field_capabilities_index"], "metrics-native-*")
        self.assertEqual(
            contract["field_capabilities_discovery"],
            {"status": "ok", "error": "", "field_count": 3},
        )
        self.assertIn("metrics.http_requests_total", contract["required_fields"])
        self.assertIn("labels.service", contract["required_fields"])
        self.assertIn("labels.env", contract["required_fields"])
        self.assertNotIn("http_requests_total", contract["required_fields"])
        metric = contract["required_fields"]["metrics.http_requests_total"]
        self.assertEqual(metric["source_fields"], ["http_requests_total"])
        self.assertEqual(metric["target_field"], "metrics.http_requests_total")
        self.assertEqual(metric["status"], "confirmed")
        self.assertEqual(metric["type"], "double")
        self.assertEqual(
            contract["counter_expectations"]["metrics.http_requests_total"]["source_field"],
            "http_requests_total",
        )
        self.assertTrue(
            contract["counter_expectations"]["metrics.http_requests_total"]["confirmed_counter"],
        )

    def test_schema_contract_checks_remote_write_profile_resolved_target_fields(self):
        class RemoteWriteResolver:
            _index_pattern = "metrics-prometheus.remote_write-*"

            def schema_profile(self):
                return "prometheus_remote_write"

            def resolve_metric_field(self, metric_name, *, prefer=None):
                suffix = "counter" if prefer == "counter" else "value"
                return f"prometheus.{metric_name}.{suffix}"

            def resolve_label(self, label):
                return f"prometheus.labels.{label}"

            def field_exists(self, field_name):
                return field_name in {
                    "prometheus.http_requests_total.counter",
                    "prometheus.labels.service",
                }

            def field_type(self, field_name):
                return "double" if field_name.endswith(".counter") else "keyword"

            def is_counter(self, metric_name):
                return metric_name == "http_requests_total"

        panel = SimpleNamespace(
            query_ir={
                "target_index": "metrics-prometheus.remote_write-*",
                "source_type": "TS",
                "metric": "http_requests_total",
                "group_labels": ["service"],
                "output_metric_field": "computed_rate",
                "semantic_losses": [],
            },
            reasons=[],
        )
        result = SimpleNamespace(inventory={}, panel_results=[panel])

        contract = preflight.build_target_schema_contract([result], RemoteWriteResolver())

        self.assertEqual(contract["schema_profile"], "prometheus_remote_write")
        self.assertIn("prometheus.http_requests_total.counter", contract["required_fields"])
        self.assertIn("prometheus.labels.service", contract["required_fields"])
        self.assertNotIn("http_requests_total", contract["required_fields"])
        self.assertEqual(
            contract["required_fields"]["prometheus.labels.service"]["source_fields"],
            ["service"],
        )

    def test_native_promql_panels_are_not_reported_as_metrics_mapping_needed(self):
        panel = SimpleNamespace(
            status="migrated",
            query_language="promql",
            datasource_type="prometheus",
            notes=["Native PROMQL: original PromQL reused via ES|QL PROMQL command"],
            query_ir={"family": "native_promql"},
        )

        self.assertEqual(manifest.classify_panel_readiness(panel), "elastic_ready")


class TestPreflightReport(unittest.TestCase):
    def test_contract_summary_counts_exact_after_fulfillment_panels(self):
        panel = SimpleNamespace(
            query_ir={"target_index": "metrics-*", "source_type": "TS"},
            target_query_contract={"canonical_target": "ts"},
            contract_evaluation={"status": "exact_after_fulfillment"},
            fulfillment_plan={"status": "required", "actions": [{"kind": "narrow_index_pattern"}]},
        )
        result = SimpleNamespace(inventory={}, panel_results=[panel])

        summary = preflight.build_target_contract_summary([result])

        self.assertEqual(summary["totals"]["exact_after_fulfillment"], 1)
        self.assertEqual(summary["action_kinds"]["narrow_index_pattern"], 1)

    def test_customer_action_summary_mentions_fulfillment_actions(self):
        summary = preflight._build_action_summary(
            results=[],
            blockers=[],
            actions=[],
            evidence_level="high",
            schema_contract={"required_indexes": {}, "required_fields": {}, "counter_expectations": {}, "totals": {}},
            target_contract_summary={
                "totals": {
                    "exact_after_fulfillment": 1,
                    "degraded_if_forced": 2,
                    "blocked": 1,
                },
                "action_kinds": {"narrow_index_pattern": 1},
            },
        )

        self.assertIn("CONTRACT STATUS exact_after_fulfillment: 1", summary)
        self.assertIn("CONTRACT STATUS degraded_if_forced: 2", summary)
        self.assertIn("CONTRACT STATUS blocked: 1", summary)
        self.assertIn("narrow_index_pattern", summary)
        self.assertNotIn("All preflight checks passed", summary)


class TestGrafanaCliPreflight(unittest.TestCase):
    def test_preflight_cli_writes_target_query_contract_summary(self):
        results = [SimpleNamespace(total_panels=1, panel_results=[])]
        schema_contract = {
            "required_indexes": {"metrics-*": 1},
            "required_fields": {},
            "counter_expectations": {},
            "totals": {},
        }
        target_contract_summary = {
            "totals": {"degraded_if_forced": 2},
            "action_kinds": {"narrow_index_pattern": 1},
        }
        preflight_report = {"customer_action_summary": "summary"}
        args = SimpleNamespace(
            prometheus_url="",
            loki_url="",
            es_url="",
            es_api_key="",
            suggest_rule_pack_out=None,
        )

        with TemporaryDirectory() as tmpdir, \
            mock.patch.object(cli, "_collect_referenced_metrics", return_value=set()), \
            mock.patch.object(cli, "_collect_referenced_labels", return_value=set()), \
            mock.patch.object(cli, "probe_source_metric_inventory", return_value={"status": "not_configured"}), \
            mock.patch.object(cli, "build_target_schema_contract", return_value=schema_contract), \
            mock.patch.object(cli, "build_target_contract_summary", return_value=target_contract_summary) as build_summary, \
            mock.patch.object(cli, "probe_target_readiness", return_value={"status": "not_configured"}), \
            mock.patch.object(cli, "build_datasource_audit", return_value={}), \
            mock.patch.object(cli, "build_dashboard_complexity", return_value=[]), \
            mock.patch.object(cli, "build_preflight_report", return_value=preflight_report) as build_report, \
            mock.patch.object(cli, "save_preflight_report"), \
            mock.patch.object(cli, "save_preflight_json") as save_contract:
            cli._run_preflight_reporting(
                args=args,
                results=results,
                resolver=None,
                base_dir=Path(tmpdir),
                validation_summary={},
                validation_records=[],
                verification_payload={},
            )

        build_summary.assert_called_once_with(results)
        self.assertEqual(
            build_report.call_args.kwargs["target_contract_summary"],
            target_contract_summary,
        )
        self.assertEqual(
            save_contract.call_args_list[1].args,
            (
                target_contract_summary,
                Path(tmpdir) / "target_query_contract_summary.json",
            ),
        )


if __name__ == "__main__":
    unittest.main()
