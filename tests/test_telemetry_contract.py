# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from observability_migration.core.telemetry_contract import (
    build_combined_telemetry_contract,
    build_schema_change_report,
    build_telemetry_contract,
)


class TelemetryContractTests(unittest.TestCase):
    def test_build_contract_from_yaml_and_verification_packets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "title": "Dash",
                                "panels": [
                                    {
                                        "title": "CPU",
                                        "esql": {
                                            "query": (
                                                "FROM metrics-*\n"
                                                "| WHERE @timestamp >= NOW() - 14 days "
                                                "AND service.name == \"api\"\n"
                                                "| STATS value = SUM(system_cpu_user) "
                                                "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), "
                                                "service.name"
                                            )
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "translated_query": (
                                    "FROM logs-*\n"
                                    "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend "
                                    "AND log.level.keyword == \"error\"\n"
                                    "| STATS _bucket_value = SUM(system_cpu_user) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), service.name.keyword\n"
                                    "| STATS value = LAST(_bucket_value, time_bucket) BY service.name.keyword"
                                ),
                                "semantic_gate": "Green",
                                "dashboard": "Dash",
                                "panel": "Errors",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        self.assertEqual(contract["version"], 1)
        self.assertIn("metrics-*", contract["streams"])
        self.assertIn("logs-*", contract["streams"])
        self.assertEqual(contract["streams"]["metrics-*"]["minimum_lookback"], "14 days")
        self.assertEqual(contract["streams"]["metrics-*"]["fields"]["system_cpu_user"]["role"], "metric")
        self.assertEqual(contract["streams"]["metrics-*"]["fields"]["system_cpu_user"]["metric_kind"], "gauge")
        self.assertEqual(contract["streams"]["metrics-*"]["fields"]["service.name"]["role"], "dimension")
        self.assertEqual(contract["streams"]["logs-*"]["fields"]["log.level"]["role"], "dimension")
        self.assertEqual(contract["streams"]["logs-*"]["fields"]["service.name"]["role"], "dimension")
        self.assertNotIn("_bucket_value", contract["streams"]["logs-*"]["fields"])
        self.assertNotIn("service.name.keyword", contract["streams"]["logs-*"]["fields"])
        self.assertEqual(contract["summary"]["metric_fields"], 1)
        self.assertGreaterEqual(contract["summary"]["dimension_fields"], 2)

    def test_contract_extracts_generic_data_requirements_from_esql_lens_and_promql(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "title": "Dash",
                                "panels": [
                                    {
                                        "title": "Errors",
                                        "esql": {
                                            "query": (
                                                "FROM logs-*\n"
                                                "| WHERE @timestamp >= NOW() - 1 hour "
                                                "AND log.level == \"error\" "
                                                "AND service.name == \"checkout\"\n"
                                                "| STATS count = COUNT(*) BY "
                                                "time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), http.url"
                                            )
                                        },
                                    },
                                    {
                                        "title": "Latency",
                                        "lens": {
                                            "primary": {
                                                "field": "http_request_duration_seconds_sum",
                                                "aggregation": "sum",
                                            },
                                            "breakdown": {"field": "http.route"},
                                        },
                                    },
                                    {
                                        "title": "5xx",
                                        "esql": {
                                            "query": (
                                                "PROMQL index=metrics-* step=1m "
                                                "value=(sum(rate(http_requests_total{"
                                                "http.response.status_code=~\"5..\","
                                                "http.request.method=\"POST\"}[5m])))"
                                            )
                                        },
                                    },
                                ],
                            }
                        ]
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        logs = contract["streams"]["logs-*"]
        metrics = contract["streams"]["metrics-*"]
        self.assertEqual(logs["minimum_lookback"], "1 hour")
        self.assertIn("http.url", logs["group_fields"])
        self.assertEqual(logs["required_values"]["log.level"], ["error"])
        self.assertEqual(logs["required_values"]["service.name"], ["checkout"])
        self.assertIn("http_request_duration_seconds_sum", metrics["fields"])
        self.assertIn("http.route", metrics["group_fields"])
        self.assertEqual(metrics["required_patterns"]["http.response.status_code"], ["5.."])
        self.assertEqual(metrics["required_values"]["http.request.method"], ["POST"])
        self.assertIn("http_requests_total", metrics["fields"])
        self.assertTrue(metrics["requires_native_promql"])

    def test_contract_finds_parent_verification_packets_when_given_yaml_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = Path(tmpdir) / "dashboards" / "yaml"
            yaml_dir.mkdir(parents=True)
            (Path(tmpdir) / "dashboards" / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "translated_query": (
                                    "FROM metrics-*\n"
                                    "| STATS value = SUM(packet_only_metric) BY packet_only_dimension"
                                )
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(yaml_dir)

        stream = contract["streams"]["metrics-*"]
        self.assertIn("packet_only_metric", stream["fields"])
        self.assertIn("packet_only_dimension", stream["fields"])

    def test_promql_discovery_handles_bare_metrics_ranges_and_negative_matchers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "panels": [
                                    {
                                        "esql": {
                                            "query": (
                                                "PROMQL index=metrics-* step=1m "
                                                "value=(sum(rate(http_requests_total{"
                                                "http.response.status_code!~\"5..\"}[30m])) "
                                                "/ count(up == 1))"
                                            )
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        stream = contract["streams"]["metrics-*"]
        self.assertIn("http_requests_total", stream["fields"])
        self.assertIn("up", stream["fields"])
        self.assertIn("http.response.status_code", stream["fields"])
        self.assertEqual(stream["minimum_lookback"], "30 minutes")
        self.assertNotIn("http.response.status_code", stream["required_patterns"])

    def test_promql_group_labels_are_not_classified_as_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "panels": [
                                    {
                                        "esql": {
                                            "query": (
                                                "PROMQL index=metrics-* step=1m "
                                                "value=(sum by (service.name, http.route) "
                                                "(rate(http_requests_total[5m])))"
                                            )
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertEqual(fields["http_requests_total"]["role"], "metric")
        self.assertEqual(fields["service.name"]["role"], "dimension")
        self.assertEqual(fields["http.route"]["role"], "dimension")

    def test_dashboard_controls_and_filters_are_contract_dimensions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "panels": [
                                    {
                                        "lens": {
                                            "primary": {
                                                "field": "system_cpu_user",
                                                "aggregation": "average",
                                            },
                                            "breakdown": {"field": "host.name"},
                                            "data_view": "metrics-*",
                                        }
                                    }
                                ],
                                "filters": [
                                    {"field": "data_stream.dataset", "equals": "generic"},
                                    {"field": "deployment.environment", "equals": "production"},
                                ],
                                "controls": [
                                    {
                                        "type": "options",
                                        "label": "env",
                                        "data_view": "metrics-*",
                                        "field": "deployment.environment",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        stream = contract["streams"]["metrics-*"]
        self.assertEqual(stream["fields"]["deployment.environment"]["role"], "dimension")
        self.assertIn("deployment.environment", stream["control_fields"])
        self.assertEqual(stream["required_values"]["deployment.environment"], ["production"])

    def test_dashboard_control_fields_are_available_on_all_streams(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "panels": [
                                    {
                                        "esql": {
                                            "query": (
                                                "FROM logs-*\n"
                                                "| WHERE log.level == \"error\"\n"
                                                "| STATS count = COUNT(*) BY service.name"
                                            )
                                        }
                                    },
                                    {
                                        "lens": {
                                            "primary": {
                                                "field": "system_cpu_user",
                                                "aggregation": "average",
                                            },
                                            "data_view": "metrics-*",
                                        }
                                    },
                                ],
                                "controls": [
                                    {
                                        "type": "options",
                                        "label": "env",
                                        "data_view": "metrics-*",
                                        "field": "deployment.environment",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        self.assertIn("deployment.environment", contract["streams"]["metrics-*"]["fields"])
        self.assertIn("deployment.environment", contract["streams"]["logs-*"]["fields"])
        self.assertIn("deployment.environment", contract["streams"]["logs-*"]["control_fields"])

    def test_combined_contract_merges_multiple_artifact_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = Path(tmpdir) / "first" / "dashboards" / "yaml"
            second = Path(tmpdir) / "second" / "dashboards" / "yaml"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "a.yaml").write_text(
                yaml.safe_dump({"dashboards": [{"panels": [{"esql": {"query": "FROM metrics-*\n| STATS value = SUM(first_metric)"}}]}]}),
                encoding="utf-8",
            )
            (second / "b.yaml").write_text(
                yaml.safe_dump({"dashboards": [{"panels": [{"esql": {"query": "FROM logs-*\n| WHERE log.level == \"error\"\n| STATS count = COUNT(*)"}}]}]}),
                encoding="utf-8",
            )

            contract = build_combined_telemetry_contract([first.parent, second.parent])

        self.assertIn("metrics-*", contract["streams"])
        self.assertIn("logs-*", contract["streams"])
        self.assertIn("first_metric", contract["streams"]["metrics-*"]["fields"])
        self.assertEqual(contract["artifact_dirs"], [str(first.parent), str(second.parent)])

    def test_schema_change_report_shows_source_and_target_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir()
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Service",
                                "panel": "Latency",
                                "source_queries": ["avg:trace.http.request.duration{env:prod} by {service}"],
                                "translated_query": (
                                    "FROM metrics-*\n"
                                    "| WHERE deployment.environment == \"prod\"\n"
                                    "| STATS value = AVG(trace_http_request_duration) BY service.name"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = build_schema_change_report(artifact_dir)

        self.assertIn("trace.http.request.duration", report)
        self.assertIn("env", report)
        self.assertIn("trace_http_request_duration", report)
        self.assertIn("deployment.environment", report)
        self.assertNotRegex(report, r"avg:trace\.http\.request\.duration")
        self.assertNotRegex(report, r"\| by\b")
        self.assertNotRegex(report, r"trace\.http\.request\.duration\.")

    def test_schema_change_report_handles_lens_panels_without_translated_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "host_cpu.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "title": "Host CPU",
                                "panels": [
                                    {
                                        "title": "CPU user %",
                                        "lens": {
                                            "type": "line",
                                            "data_view": "metrics-*",
                                            "primary": {
                                                "field": "system_cpu_user",
                                                "aggregation": "average",
                                            },
                                            "breakdown": {"field": "host.name"},
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Host CPU",
                                "panel": "CPU user %",
                                "source_queries": ["avg:system.cpu.user{*} by {host}"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = build_schema_change_report(artifact_dir)

        self.assertIn("CPU user %", report)
        self.assertIn("system_cpu_user", report)
        self.assertIn("host.name", report)
        self.assertIn("metrics-*", report)
        self.assertNotIn("| n/a |", report)

    def test_schema_change_report_uses_yaml_dashboard_name_when_title_missing(self):
        """kb-dashboard-cli emits dashboards keyed by `name`; the report must
        not lose dashboard titles just because the YAML omits the legacy
        `title` field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "host.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "name": "Host metrics",
                                "panels": [
                                    {
                                        "title": "CPU user %",
                                        "lens": {
                                            "data_view": "metrics-*",
                                            "primary": {
                                                "field": "system_cpu_user",
                                                "aggregation": "average",
                                            },
                                            "breakdown": {"field": "host.name"},
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = build_schema_change_report(artifact_dir)

        self.assertIn("Host metrics", report)
        empty_dashboard_rows = [
            line
            for line in report.splitlines()
            if line.startswith("|")
            and "CPU user %" in line
            and line.split("|")[1].strip() == ""
        ]
        self.assertFalse(
            empty_dashboard_rows,
            f"dashboard column should not be empty when YAML uses `name`: {empty_dashboard_rows!r}",
        )

    def test_schema_change_report_filters_esql_pipeline_keywords_and_scaffolding(self):
        """ES|QL command keywords and translator scaffolding aliases must not
        leak into the target-fields column."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir()
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "App",
                                "panel": "Errors",
                                "source_queries": [
                                    "sum(rate(app_errors_total[5m])) by (service)"
                                ],
                                "translated_query": (
                                    "PROMQL index=metrics-* step=1m "
                                    "value=(sum(rate(app_errors_total[5m])) by (service))\n"
                                    "| EVAL _ts = @timestamp, _raw_value = value, "
                                    "_per_series_value = value, _timeseries = label\n"
                                    "| STATS _bucket_value = SUM(_raw_value) BY label\n"
                                    "| KEEP step, value, label, _gauge_min, _gauge_max"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = build_schema_change_report(artifact_dir)

        target_cells: list[str] = []
        for line in report.splitlines():
            if line.startswith("|") and "Errors" in line:
                cells = [cell.strip() for cell in line.split("|")[1:-1]]
                if len(cells) >= 5:
                    target_cells.append(cells[4])
        self.assertTrue(target_cells, "expected at least one Errors row")

        target_tokens = {
            token.strip()
            for cell in target_cells
            for token in cell.split(",")
            if token.strip() and token.strip() != "n/a"
        }
        forbidden = {
            "EVAL",
            "KEEP",
            "STATS",
            "WHERE",
            "SORT",
            "LIMIT",
            "BY",
            "ASC",
            "DESC",
            "FROM",
            "step",
            "label",
            "unknown",
            "_ts",
            "_raw_value",
            "_per_series_value",
            "_timeseries",
            "_bucket_value",
            "_gauge_min",
            "_gauge_max",
        }
        leaked = target_tokens & forbidden
        self.assertFalse(
            leaked,
            f"target column leaked translator scaffolding tokens: {sorted(leaked)!r}; "
            f"full target tokens: {sorted(target_tokens)!r}",
        )
        self.assertIn("app_errors_total", target_tokens)
        self.assertIn("service", target_tokens)

    def test_schema_change_report_filters_grafana_and_promql_meta_tokens_from_source(
        self,
    ):
        """Grafana template variables and PromQL meta-labels/operators must
        not leak into the source-fields column."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir()
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Node",
                                "panel": "CPU",
                                "source_queries": [
                                    "sum by (instance) (rate(node_cpu_seconds_total{"
                                    "__name__=\"node_cpu_seconds_total\", "
                                    "mode!=\"idle\"}[$__rate_interval])) "
                                    "* on(instance) group_left(nodename) node_uname_info"
                                ],
                                "translated_query": (
                                    "PROMQL index=metrics-* step=1m "
                                    "value=(sum by (instance) "
                                    "(rate(node_cpu_seconds_total[5m])))"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = build_schema_change_report(artifact_dir)

        source_cells: list[str] = []
        for line in report.splitlines():
            if line.startswith("|") and "Node" in line and "CPU" in line:
                cells = [cell.strip() for cell in line.split("|")[1:-1]]
                if len(cells) >= 5:
                    source_cells.append(cells[2])
        self.assertTrue(source_cells, "expected at least one Node CPU row")

        source_tokens = {
            token.strip()
            for cell in source_cells
            for token in cell.split(",")
            if token.strip() and token.strip() != "n/a"
        }
        forbidden = {
            "__name__",
            "__rate_interval",
            "__interval",
            "__range",
            "__interval_ms",
            "__rate_interval_ms",
            "group_left",
            "group_right",
            "ignoring",
            "on",
            "aggregation_interval",
            "scrape_interval",
        }
        leaked = source_tokens & forbidden
        self.assertFalse(
            leaked,
            f"source column leaked PromQL/Grafana meta tokens: {sorted(leaked)!r}; "
            f"full source tokens: {sorted(source_tokens)!r}",
        )
        self.assertIn("node_cpu_seconds_total", source_tokens)
        self.assertIn("instance", source_tokens)
        self.assertIn("mode", source_tokens)
        self.assertIn("nodename", source_tokens)

    def test_schema_change_report_combines_multiple_artifacts_into_single_document(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = Path(tmpdir) / "first"
            second = Path(tmpdir) / "second"
            first.mkdir()
            second.mkdir()
            (first / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "First",
                                "panel": "P1",
                                "source_queries": ["sum(rate(http_requests_total[5m]))"],
                                "translated_query": (
                                    "PROMQL index=metrics-* step=1m value=(sum(rate(http_requests_total[5m])))"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (second / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Second",
                                "panel": "P2",
                                "source_queries": ["avg:trace.http.request.hits{*} by {service}"],
                                "translated_query": (
                                    "FROM metrics-*\n"
                                    "| STATS value = AVG(trace_http_request_hits) BY service.name"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = build_schema_change_report([first, second])

        self.assertEqual(report.count("# Telemetry Schema Change Report"), 1)
        self.assertIn("## Summary", report)
        self.assertIn("Artifact directories:", report)
        self.assertIn("First", report)
        self.assertIn("Second", report)
        self.assertIn("metrics-*", report)
        self.assertIn("Total panels", report)


if __name__ == "__main__":
    unittest.main()
