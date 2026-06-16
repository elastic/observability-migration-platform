# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from observability_migration.core.telemetry_contract import (
    _extract_group_fields,
    build_combined_telemetry_contract,
    build_schema_change_report,
    build_telemetry_contract,
    merge_metric_kind_overrides,
    metric_kinds_from_field_caps,
    metric_kinds_from_prometheus_metadata,
    write_schema_report_artifacts,
)


class TelemetryContractTests(unittest.TestCase):
    def test_write_schema_report_artifacts_writes_default_report_and_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir()
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Service",
                                "panel": "CPU",
                                "source_queries": ["avg:system.cpu.user{host:web01} by {host}"],
                                "translated_query": (
                                    "FROM metrics-*\n"
                                    "| STATS value = AVG(system_cpu_user) BY host.name"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            artifacts = write_schema_report_artifacts(artifact_dir)

            report_text = artifacts["schema_report"].read_text(encoding="utf-8")
            contract = json.loads(artifacts["telemetry_contract"].read_text(encoding="utf-8"))

        self.assertIn("system.cpu.user", report_text)
        self.assertIn("system_cpu_user", report_text)
        self.assertEqual(contract["summary"]["streams"], 1)

    def test_write_schema_report_artifacts_removes_partial_report_on_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir()
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Service",
                                "panel": "CPU",
                                "source_queries": ["avg:system.cpu.user{*}"],
                                "translated_query": "FROM metrics-* | STATS value = AVG(system_cpu_user)",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            report_path = artifact_dir / "schema_change_report.md"
            contract_path = artifact_dir / "telemetry_contract.json"

            with mock.patch(
                "observability_migration.core.telemetry_contract.write_telemetry_contract",
                side_effect=RuntimeError("contract failed"),
            ), self.assertRaisesRegex(RuntimeError, "contract failed"):
                write_schema_report_artifacts(artifact_dir)

            report_exists = report_path.exists()
            contract_exists = contract_path.exists()

        self.assertFalse(report_exists)
        self.assertFalse(contract_exists)

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

    def test_contract_extracts_required_values_from_kql_function_filters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "logs.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "title": "Logs",
                                "panels": [
                                    {
                                        "title": "Redis errors",
                                        "esql": {
                                            "query": (
                                                "FROM logs-generic-default\n"
                                                "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend "
                                                'AND KQL("(service.name: redis) AND (log.level: error)")\n'
                                                "| KEEP @timestamp, message, log.level, service.name"
                                            )
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        logs = contract["streams"]["logs-generic-default"]
        self.assertEqual(logs["required_values"]["service.name"], ["redis"])
        self.assertEqual(logs["required_values"]["log.level"], ["error"])

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

    def test_contract_preserves_source_promql_requirements_from_manualized_packets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "node.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "title": "Node",
                                "panels": [
                                    {
                                        "title": "CPU Busy",
                                        "markdown": {"content": "Manual review required"},
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
                                "dashboard": "Node",
                                "panel": "CPU Busy",
                                "source_query": (
                                    '100 * (1 - avg(rate(node_cpu_seconds_total{'
                                    'mode="idle", instance="$node"}[5m])))'
                                ),
                                "query_ir": {
                                    "source_language": "promql",
                                    "target_index": "metrics-prometheus-default",
                                    "target_query": "ROW manual = \"placeholder\"",
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        stream = contract["streams"]["metrics-prometheus-default"]
        self.assertEqual(stream["fields"]["node_cpu_seconds_total"]["role"], "metric")
        self.assertEqual(stream["fields"]["mode"]["role"], "dimension")
        self.assertEqual(stream["required_values"]["mode"], ["idle"])

    def test_contract_does_not_treat_datadog_source_queries_as_promql(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Redis",
                                "panel": "Logs",
                                "source_query": "service:redis status:error",
                                "query_ir": {
                                    "source_language": "datadog",
                                    "target_index": "logs-generic-default",
                                    "target_query": (
                                        "FROM logs-generic-default\n"
                                        "| WHERE KQL(\"service.name: redis\")"
                                    ),
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        fields = contract["streams"]["logs-generic-default"]["fields"]
        self.assertIn("service.name", fields)
        self.assertNotIn("service:redis", fields)
        self.assertNotIn("status:error", fields)

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

    def test_extract_group_fields_captures_grok_timeseries_legend_labels(self):
        # Bind9-style panels group by a legendFormat-only label (e.g. {{type}})
        # that is NOT in the PromQL body. The translator extracts it from the
        # native _timeseries JSON with `GROK _timeseries "...%{DATA:type}..."`.
        # That GROK target IS the grouping dimension and must be captured so the
        # seeder seeds it (otherwise the migrated panel groups on a field that
        # was never seeded -> empty / wrong-shape panel).
        query = (
            'PROMQL index=metrics-* step=1m '
            'value=(rate(bind_incoming_queries_total{instance="1:1"}[5m]))\n'
            '| GROK _timeseries """"type":"%{DATA:type}\\""""\n'
            "| KEEP step, value, type"
        )
        self.assertIn("type", _extract_group_fields(query))

    def test_extract_group_fields_ignores_label_replace_grok_on_other_source(self):
        # A label_replace-style GROK extracts a COMPUTED column from a label
        # column (not the _timeseries blob) and must NOT be treated as a seeded
        # grouping dimension.
        query = (
            'PROMQL index=metrics-* step=1m value=(up)\n'
            '| GROK instance "^%{GREEDYDATA:host}:%{GREEDYDATA:port}$"\n'
            "| KEEP step, value, host"
        )
        self.assertNotIn("host", _extract_group_fields(query))

    def test_contract_seeds_legend_only_grok_label_as_group_field(self):
        # End to end: a panel whose ONLY grouping is a legendFormat label must
        # land that label in both the stream group_fields and the per-metric
        # requirement, so _metric_families scopes it onto the metric and the
        # seeder generates it.
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
                                                'PROMQL index=metrics-* step=1m '
                                                'value=(rate(bind_incoming_queries_total{instance="1:1"}[5m]))\n'
                                                '| GROK _timeseries """"type":"%{DATA:type}\\""""\n'
                                                "| KEEP step, value, type"
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
        self.assertIn("type", stream["group_fields"])
        self.assertEqual(stream["fields"]["type"]["role"], "dimension")
        # The per-metric requirement must carry `type` so family-scoping seeds it
        # alongside bind_incoming_queries_total.
        type_reqs = [
            r for r in stream["requirements"]
            if "bind_incoming_queries_total" in (r.get("metrics") or [])
            and "type" in (r.get("group_fields") or [])
        ]
        self.assertTrue(
            type_reqs,
            "expected a requirement pairing bind_incoming_queries_total with the 'type' group field",
        )

    def test_grouped_field_wins_over_metric_collision_in_contract(self):
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
                                                "FROM metrics-*\n"
                                                "| STATS value = AVG(mode) BY mode"
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
        self.assertEqual(fields["mode"]["role"], "dimension")
        self.assertEqual(fields["mode"].get("metric_kind", ""), "")

    def test_contract_skips_translator_scaffold_aliases_as_metrics(self):
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
                                                "FROM metrics-*\n"
                                                "| STATS computed_value = AVG(node_memory_MemAvailable_bytes)\n"
                                                "| EVAL inner_val = computed_value\n"
                                                "| STATS value = SUM(inner_val)\n"
                                                "| STATS value = AVG(COUNT_OVER_TIME)"
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
        self.assertIn("node_memory_MemAvailable_bytes", fields)
        self.assertNotIn("computed_value", fields)
        self.assertNotIn("inner_val", fields)
        self.assertNotIn("COUNT_OVER_TIME", fields)

    def test_contract_does_not_extract_rlike_operator_as_metric(self):
        # RLIKE is an ES|QL regex-match operator. It appears inside generated
        # aggregations such as AVG(CASE((NOT (fstype RLIKE "tmpfs")), metric, ...))
        # (node-exporter "Filesystem in ReadOnly / Error"). The metric extractor
        # must treat it as a keyword, never harvest it as a gauge field.
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
                                                "TS metrics-*\n"
                                                '| WHERE NOT (device RLIKE "rootfs")\n'
                                                "| STATS node_filesystem_device_error_B = "
                                                'AVG(CASE((NOT (fstype RLIKE "tmpfs")), '
                                                "node_filesystem_device_error, 0)) "
                                                "BY time_bucket = TBUCKET(5 minute)"
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
        self.assertIn("node_filesystem_device_error", fields)
        self.assertNotIn("RLIKE", fields)
        self.assertNotIn("fstype", set(fields) - {f for f, i in fields.items() if i["role"] == "dimension"})

    def test_contract_does_not_harvest_regex_value_tokens_as_metrics(self):
        # node-exporter disk panels filter device=~"[a-z]+|nvme[0-9]+n[0-9]+|
        # mmcblk[0-9]+". The identifier-before-bracket scan must not treat the
        # regex-internal tokens nvme/n/mmcblk (each followed by "[0-9]") as metric
        # names; only the real metric(s) outside the label-matcher value count.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Node Exporter Full",
                                "panel": "Disk IOps",
                                "source_language": "promql",
                                "source_query": (
                                    'rate(node_disk_reads_completed_total{instance="$node",'
                                    'job="$job",device=~"[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+"}'
                                    "[$__rate_interval])"
                                ),
                                "translated_query": (
                                    "TS metrics-*\n"
                                    '| WHERE device RLIKE "[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+"\n'
                                    "| STATS node_disk_reads_completed_total = "
                                    "AVG(RATE(node_disk_reads_completed_total, 5m)) "
                                    "BY time_bucket = TBUCKET(5 minute), device"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertIn("node_disk_reads_completed_total", fields)
        for token in ("nvme", "mmcblk", "n"):
            self.assertNotIn(token, fields, f"regex token {token!r} leaked as a metric field")

    def test_contract_does_not_treat_eval_legend_alias_as_metric(self):
        # Multi-query panels alias real metrics to legend display names via
        # ``EVAL CPU = <metric>_A`` and then re-aggregate the alias in a second
        # STATS (``STATS CPU = MAX(CPU)``). CPU/Mem/Irq are derived ES|QL
        # columns, not source index fields -- they must never be seeded as
        # metrics (node-exporter "Pressure" panel leaked CPU/Mem/Irq/I_O).
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
                                                "TS metrics-*\n"
                                                "| STATS node_pressure_cpu_waiting_seconds_total_A = "
                                                "RATE(node_pressure_cpu_waiting_seconds_total, 5m) "
                                                "BY time_bucket = TBUCKET(5 minute)\n"
                                                "| EVAL CPU = node_pressure_cpu_waiting_seconds_total_A\n"
                                                "| STATS time_bucket = MAX(time_bucket), CPU = MAX(CPU)\n"
                                                "| KEEP time_bucket, CPU"
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
        self.assertIn("node_pressure_cpu_waiting_seconds_total", fields)
        self.assertNotIn("CPU", fields)

    def test_contract_seeds_presence_only_is_not_null_field_as_metric(self):
        # A "presence" panel references a metric solely via ``WHERE <field> IS
        # NOT NULL`` (no aggregation on it). Without capturing it the synthetic
        # stream lacks the column and the live seed fails with
        # ``Unknown column [<field>]``. It must be seeded as a gauge metric.
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
                                                "FROM metrics-*\n"
                                                "| WHERE kube_pod_info IS NOT NULL\n"
                                                "| STATS c = COUNT_DISTINCT(pod)"
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
        self.assertIn("kube_pod_info", fields)
        self.assertEqual(fields["kube_pod_info"]["role"], "metric")
        self.assertEqual(fields["kube_pod_info"]["metric_kind"], "gauge")
        self.assertIn("pod", fields)
        self.assertEqual(fields["pod"]["role"], "dimension")

    def test_contract_does_not_seed_post_stats_null_guard_aliases(self):
        # Datadog formula queries null-guard aggregation aliases after ``STATS``.
        # Those aliases are derived columns, not source telemetry fields; seeding
        # them would pollute the synthetic stream with phantom metric columns.
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
                                                "FROM metrics-*\n"
                                                "| STATS q1 = AVG(redis.mem.used), "
                                                "q2 = AVG(redis.mem.maxmemory) BY host.name\n"
                                                "| WHERE q1 IS NOT NULL AND q2 IS NOT NULL\n"
                                                "| EVAL value = CASE(q2 == 0, NULL, ((100 * q1) / q2))\n"
                                                "| WHERE value IS NOT NULL\n"
                                                "| WHERE value > 90.0"
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
        self.assertIn("redis.mem.used", fields)
        self.assertIn("redis.mem.maxmemory", fields)
        self.assertNotIn("q1", fields)
        self.assertNotIn("q2", fields)
        self.assertNotIn("value", fields)
        self.assertEqual(fields["host.name"]["role"], "dimension")

    def test_contract_captures_rlike_filter_pattern(self):
        # ``WHERE <dimension> RLIKE "app_.*"`` must yield a required pattern so
        # the seeder synthesizes a matching dimension value; otherwise the
        # seeded values never match the regex and the panel returns zero rows.
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
                                                "FROM metrics-*\n"
                                                '| WHERE service_name RLIKE "app_.*"\n'
                                                "| WHERE node_load1 IS NOT NULL\n"
                                                "| STATS v = AVG(node_load1) BY "
                                                "time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), "
                                                "service_name"
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
        self.assertEqual(stream["required_patterns"]["service_name"], ["app_.*"])
        self.assertEqual(stream["fields"]["service_name"]["role"], "dimension")

    def test_contract_skips_parenthesized_negated_rlike_filter_pattern(self):
        # PromQL ``!~`` emits ``NOT (<field> RLIKE "...")``. That rejected regex
        # must not become a required pattern, or the seeder will synthesize only
        # values the panel immediately filters out.
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
                                                "TS metrics-*\n"
                                                '| WHERE NOT (device RLIKE "rootfs")\n'
                                                "| STATS v = AVG(node_filesystem_avail_bytes) BY "
                                                "time_bucket = TBUCKET(5 minute), device"
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
        self.assertNotIn("device", stream["required_patterns"])
        self.assertEqual(stream["fields"]["device"]["role"], "dimension")

    def test_contract_skips_all_parenthesized_negated_rlike_filter_patterns(self):
        # When multiple regex terms live inside one negated group, none should
        # be promoted to required patterns.
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
                                                "TS metrics-*\n"
                                                '| WHERE NOT (device RLIKE "rootfs" OR device RLIKE "tmpfs")\n'
                                                "| STATS v = AVG(node_filesystem_avail_bytes) BY "
                                                "time_bucket = TBUCKET(5 minute), device"
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
        self.assertNotIn("device", stream["required_patterns"])
        self.assertEqual(stream["fields"]["device"]["role"], "dimension")

    def test_contract_keeps_rlike_pattern_after_string_literal_with_not_paren(self):
        # A prior comparison value containing ``NOT (`` must not open a negation
        # frame that suppresses a genuinely non-negated RLIKE later in the query.
        # The negation scan blanks quoted spans, so the ``device`` pattern is
        # kept; without that guard it would be silently dropped — reintroducing
        # the very false-negative this fix targets.
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
                                                '| WHERE msg == "reject NOT (allowed" '
                                                'AND device RLIKE "eth.*"\n'
                                                "| STATS v = COUNT(*) BY device"
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

        stream = contract["streams"]["logs-*"]
        self.assertEqual(stream["required_patterns"]["device"], ["eth.*"])
        self.assertEqual(stream["required_values"]["msg"], ["reject NOT (allowed"])

    def test_contract_ignores_is_not_null_inside_string_literal(self):
        # ``IS NOT NULL`` appearing inside a quoted value is not a presence
        # predicate; the identifier before it must not be seeded as a phantom
        # gauge field.
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
                                                '| WHERE host == "x phantom IS NOT NULL y"\n'
                                                "| STATS v = COUNT(*) BY host"
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

        stream = contract["streams"]["logs-*"]
        self.assertNotIn("phantom", stream["fields"])

    def test_contract_does_not_seed_pure_is_null_absence_field(self):
        # A pure ``IS NULL`` (absence) filter wants rows where the field is
        # missing. Seeding it as an always-present gauge would make the panel
        # match zero rows — a fresh false-negative — so only ``IS NOT NULL``
        # (presence) is captured.
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
                                                "FROM metrics-*\n"
                                                "| WHERE kube_pod_info IS NULL\n"
                                                "| STATS c = COUNT(*) BY namespace"
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
        self.assertNotIn("kube_pod_info", stream["fields"])

    def test_contract_skips_parenthesized_negated_equality_filter(self):
        # Datadog ``LogNot`` emits ``NOT (<field> == "x")`` for ordinary queries.
        # The excluded value must not be seeded, exactly as for negated RLIKE.
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
                                                '| WHERE NOT (status == "active")\n'
                                                "| STATS v = COUNT(*) BY status"
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

        stream = contract["streams"]["logs-*"]
        self.assertNotIn("status", stream["required_values"])
        self.assertEqual(stream["fields"]["status"]["role"], "dimension")

    def test_contract_skips_bare_not_prefixed_rlike_filter(self):
        # A bare ``NOT <field> RLIKE "x"`` (no grouping parens) is still a
        # negation; its regex must not become a required pattern.
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
                                                '| WHERE NOT host RLIKE "db.*"\n'
                                                "| STATS v = COUNT(*) BY host"
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

        stream = contract["streams"]["logs-*"]
        self.assertNotIn("host", stream["required_patterns"])

    def test_contract_keeps_positive_rlike_alongside_negated_group(self):
        # When a positive matcher and a separate negated group coexist, only the
        # negated one is dropped; the positive RLIKE is still seeded. Pins the
        # paren-stack ``pop`` logic that a regression would otherwise mask.
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
                                                '| WHERE NOT (device RLIKE "rootfs") '
                                                'AND service_name RLIKE "app_.*"\n'
                                                "| STATS v = COUNT(*) BY device, service_name"
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

        stream = contract["streams"]["logs-*"]
        self.assertNotIn("device", stream["required_patterns"])
        self.assertEqual(stream["required_patterns"]["service_name"], ["app_.*"])

    def test_contract_extracts_like_and_equality_but_not_negated_variants(self):
        # The rewritten comparison regex must still honor ``==`` and ``LIKE`` and
        # still skip ``!=`` and ``NOT LIKE`` for non-negated matchers.
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
                                                '| WHERE env == "prod" AND region != "eu" '
                                                'AND service LIKE "web*" AND team NOT LIKE "ops*"\n'
                                                "| STATS v = COUNT(*) BY env, region, service, team"
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

        stream = contract["streams"]["logs-*"]
        self.assertEqual(stream["required_values"]["env"], ["prod"])
        self.assertEqual(stream["required_patterns"]["service"], ["web"])
        self.assertNotIn("region", stream["required_values"])
        self.assertNotIn("team", stream["required_patterns"])

    def test_contract_does_not_extract_duration_unit_as_metric(self):
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
                                                "TS metrics-*\n"
                                                "| WHERE label_metric IS NOT NULL\n"
                                                "| STATS v = AVG(COUNT_OVER_TIME(label_metric, 1m))"
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
        self.assertIn("label_metric", fields)
        self.assertNotIn("m", fields)

    def test_contract_rejects_composite_template_required_values(self):
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
                                                "value=(up{instance=\"$host:$port\", job=\"node\"})"
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
        self.assertNotIn("$host:$port", stream["required_values"].get("instance", []))
        self.assertEqual(stream["required_values"]["job"], ["node"])

    def test_contract_keeps_literal_label_prefixed_dimension_values(self):
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
                                                "value=(up{tier=\"label_value\"})"
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
        self.assertEqual(stream["required_values"]["tier"], ["label_value"])

    def test_label_evidence_wins_when_field_also_leaked_as_metric_name(self):
        # A CPU panel ``node_cpu_seconds_total{mode="idle"}`` leaks the label key
        # ``mode`` as a metric name in one panel while another panel groups BY
        # mode. Explicit BY evidence is authoritative, so mode must be a keyword
        # dimension (a numeric mapping would make the grouping panel return no
        # rows).
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
                                                "FROM metrics-*\n"
                                                "| STATS value = AVG(mode)"
                                            )
                                        }
                                    },
                                    {
                                        "esql": {
                                            "query": (
                                                "FROM metrics-*\n"
                                                "| STATS value = AVG(node_cpu_seconds_total) BY mode"
                                            )
                                        }
                                    },
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertEqual(fields["mode"]["role"], "dimension")
        self.assertEqual(fields["node_cpu_seconds_total"]["role"], "metric")

    def test_string_literal_comparison_values_are_not_extracted_as_metrics(self):
        # Generated ES|QL like ``AVG(CASE((mode == "idle"), RATE(metric), NULL))``
        # must not extract the quoted label values (idle/system) as metric
        # fields; only the real source metric is a field.
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
                                                "TS metrics-*\n"
                                                "| WHERE node_cpu_seconds_total IS NOT NULL\n"
                                                "| STATS a = AVG(CASE((mode == \"system\"), "
                                                "RATE(node_cpu_seconds_total, 5m), NULL)), "
                                                "b = AVG(CASE((mode == \"idle\"), "
                                                "RATE(node_cpu_seconds_total, 5m), NULL)) "
                                                "BY time_bucket = TBUCKET(5 minute), cpu"
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
        self.assertEqual(fields["node_cpu_seconds_total"]["role"], "metric")
        self.assertNotIn("idle", fields)
        self.assertNotIn("system", fields)

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

    def test_schema_change_report_extracts_target_fields_inside_esql_aggregate_expressions(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir()
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Node",
                                "panel": "Uptime",
                                "source_queries": ["time() - node_boot_time_seconds"],
                                "translated_query": (
                                    "FROM metrics-*\n"
                                    "| WHERE node_boot_time_seconds IS NOT NULL\n"
                                    "| STATS start_time_ms = MAX(node_boot_time_seconds * 1000)\n"
                                    "| EVAL node_boot_time_seconds_uptime_seconds = "
                                    "DATE_DIFF(\"seconds\", TO_DATETIME(start_time_ms), NOW())\n"
                                    "| KEEP node_boot_time_seconds_uptime_seconds"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = build_schema_change_report(artifact_dir)

        row = next(line for line in report.splitlines() if line.startswith("| Node | Uptime |"))
        cells = [cell.strip() for cell in row.split("|")[1:-1]]
        target_tokens = {
            token.strip()
            for token in cells[4].split(",")
            if token.strip() and token.strip() != "n/a"
        }
        self.assertIn("node_boot_time_seconds", target_tokens)
        self.assertNotIn("start_time_ms", target_tokens)
        self.assertNotIn("node_boot_time_seconds_uptime_seconds", target_tokens)

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

    def test_schema_change_report_filters_datadog_formula_tokens_from_source(self):
        """Datadog formulas and log search values must not be reported as
        source schema fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir()
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Datadog",
                                "panel": "Top CPU",
                                "source_queries": [
                                    "top(avg:apache.performance.cpu_load{*} by {host}, 10, 'mean', 'desc')"
                                ],
                                "translated_query": (
                                    "FROM metrics-*\n"
                                    "| STATS value = AVG(apache_performance_cpu_load) BY host.name"
                                ),
                            },
                            {
                                "dashboard": "Datadog",
                                "panel": "Oplog usage",
                                "source_queries": [
                                    "default_zero(avg:mongodb.oplog.usedsizemb{*}) "
                                    "/ default_zero(avg:mongodb.oplog.logsizemb{*})"
                                ],
                                "translated_query": (
                                    "FROM metrics-*\n"
                                    "| STATS used = AVG(mongodb_oplog_usedsizemb), "
                                    "size = AVG(mongodb_oplog_logsizemb)"
                                ),
                            },
                            {
                                "dashboard": "Datadog",
                                "panel": "Data stream latency",
                                "source_queries": [
                                    "count(v: v>=0):data_streams.latency{direction:out,"
                                    "pathway_type:full,type:kafka,$topic,$env} "
                                    "by {service,env}.as_rate().rollup(10)"
                                ],
                                "translated_query": (
                                    "FROM metrics-*\n"
                                    "| WHERE direction == \"out\" AND pathway_type == \"full\"\n"
                                    "| STATS value = COUNT(data_streams_latency) "
                                    "BY service.name, deployment.environment"
                                ),
                            },
                            {
                                "dashboard": "Datadog",
                                "panel": "Apache logs",
                                "source_queries": ["source:apache status:error"],
                                "translated_query": (
                                    "FROM logs-*\n"
                                    "| WHERE service.name == \"apache\" AND log.level == \"error\""
                                ),
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = build_schema_change_report(artifact_dir)

        source_cells: list[str] = []
        for line in report.splitlines():
            if line.startswith("|") and "Datadog" in line:
                cells = [cell.strip() for cell in line.split("|")[1:-1]]
                if len(cells) >= 5:
                    source_cells.append(cells[2])
        self.assertTrue(source_cells, "expected Datadog schema-report rows")

        source_tokens = {
            token.strip()
            for cell in source_cells
            for token in cell.split(",")
            if token.strip() and token.strip() != "n/a"
        }
        forbidden = {
            "desc",
            "mean",
            "zero",
            "v",
            "apache",
            "source:apache",
            "status:error",
            ":data_streams.latency",
        }
        leaked = source_tokens & forbidden
        self.assertFalse(
            leaked,
            f"source column leaked Datadog formula/log tokens: {sorted(leaked)!r}; "
            f"full source tokens: {sorted(source_tokens)!r}",
        )
        self.assertIn("apache.performance.cpu_load", source_tokens)
        self.assertIn("host", source_tokens)
        self.assertIn("mongodb.oplog.usedsizemb", source_tokens)
        self.assertIn("mongodb.oplog.logsizemb", source_tokens)
        self.assertIn("data_streams.latency", source_tokens)
        self.assertIn("direction", source_tokens)
        self.assertIn("pathway_type", source_tokens)
        self.assertIn("type", source_tokens)
        self.assertIn("service", source_tokens)
        self.assertIn("env", source_tokens)
        self.assertIn("source", source_tokens)
        self.assertIn("status", source_tokens)

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


    def test_kube_resource_requests_classified_as_gauge_not_counter(self):
        # kube_pod_container_resource_requests contains the substring "requests"
        # but it is a K8s gauge (current CPU/memory allocation), not an HTTP request
        # counter. A bare sum() in PromQL — without rate()/increase() — must not
        # cause this field to land in the index template as counter_double, because
        # SUM(CASE(...counter_double...)) is rejected by ES|QL at runtime.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "K8s Global",
                                "panel": "Global CPU Usage",
                                "translated_query": (
                                    "FROM metrics-*\n"
                                    "| WHERE kube_pod_container_resource_requests IS NOT NULL\n"
                                    "| STATS v = SUM(CASE((resource == \"cpu\"),"
                                    " kube_pod_container_resource_requests, NULL))"
                                    " BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)"
                                ),
                                "source_query": (
                                    "sum(kube_pod_container_resource_requests{resource=\"cpu\"})"
                                    " / sum(machine_cpu_cores)"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertIn("kube_pod_container_resource_requests", fields)
        self.assertEqual(
            fields["kube_pod_container_resource_requests"]["metric_kind"],
            "gauge",
            "kube_pod_container_resource_requests must be gauge; 'requests' in the name "
            "refers to K8s resource allocation, not an HTTP request counter",
        )

    def test_http_request_counter_still_classified_as_counter(self):
        # Removing the broad "requests" hint must not break real HTTP request counters
        # that don't carry a _total suffix (e.g. nginx_http_requests) when the
        # translated query itself is a PROMQL query with rate() context.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "NGINX",
                                "panel": "Requests",
                                "translated_query": (
                                    "PROMQL index=metrics-* step=1m"
                                    " value=(rate(nginx_http_requests[5m]))"
                                ),
                                "source_query": "rate(nginx_http_requests[5m])",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertIn("nginx_http_requests", fields)
        self.assertEqual(
            fields["nginx_http_requests"]["metric_kind"],
            "counter",
            "nginx_http_requests used inside rate() must remain counter",
        )

    def test_source_rate_counter_wins_over_translated_avg_over_time_gauge(self):
        # node-exporter counters without a counter-name suffix (node_vmstat_oom_kill,
        # node_netstat_Icmp_InErrors) are wrapped in rate() at source. The translator,
        # seeing them typed as gauge, degrades rate() -> AVG_OVER_TIME — which then
        # votes gauge in the contract and locks in the misclassification (circular).
        # The source rate()/irate() signal is counter-only in PromQL and must win, so
        # the field is seeded as a counter and the next translation emits a true RATE.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Node Exporter Full",
                                "panel": "OOM Killer",
                                "source_language": "promql",
                                "source_query": (
                                    'rate(node_vmstat_oom_kill{instance="$node",job="$job"}'
                                    "[$__rate_interval])"
                                ),
                                "translated_query": (
                                    "TS metrics-*\n"
                                    "| WHERE node_vmstat_oom_kill IS NOT NULL\n"
                                    "| STATS node_vmstat_oom_kill = "
                                    "AVG(AVG_OVER_TIME(node_vmstat_oom_kill, 5m)) "
                                    "BY time_bucket = TBUCKET(5 minute), service.instance.id"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertEqual(
            fields["node_vmstat_oom_kill"]["metric_kind"],
            "counter",
            "source rate() must classify the field as counter despite AVG_OVER_TIME in the translation",
        )

    def test_translated_max_over_time_still_downgrades_increase_misuse_to_gauge(self):
        # Guard the legitimate gauge-override: increase() can be MISused on a real
        # gauge (it is not counter-only the way rate()/irate() are). When only an
        # increase() source signal competes with a MAX_OVER_TIME gauge translation,
        # gauge must still win so the seeded mapping is aggregatable in FROM mode.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "X",
                                "panel": "Y",
                                "source_language": "promql",
                                "source_query": "increase(weird_gauge_level[5m])",
                                "translated_query": (
                                    "TS metrics-*\n"
                                    "| STATS weird_gauge_level = "
                                    "MAX(MAX_OVER_TIME(weird_gauge_level, 5m)) "
                                    "BY time_bucket = TBUCKET(5 minute)"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            contract = build_telemetry_contract(artifact_dir)

        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertEqual(
            fields["weird_gauge_level"]["metric_kind"],
            "gauge",
            "increase()+MAX_OVER_TIME must remain gauge (increase can be misused on gauges)",
        )


class DimensionValueHygieneTests(unittest.TestCase):
    def test_template_variables_and_placeholders_are_not_seeded_values(self):
        """Unsubstituted template vars / migrator placeholders must not become
        required dimension values — seeding them produces unqueryable series."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "title": "Node",
                                "panels": [
                                    {
                                        "title": "Network",
                                        "esql": {
                                            "query": (
                                                "TS metrics-*\n"
                                                "| WHERE instance == \"$instance\" "
                                                "AND job == \"__obs_migration_param_job\" "
                                                "AND env == \"production\"\n"
                                                "| STATS v = AVG(IRATE(node_network_receive_bytes_total, 5m)) "
                                                "BY time_bucket = TBUCKET(5 minute), device"
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

        metrics = contract["streams"]["metrics-*"]
        required = metrics.get("required_values") or {}
        # The template var and placeholder must be dropped entirely...
        self.assertNotIn("instance", required)
        self.assertNotIn("job", required)
        # ...while genuine literal values are still captured.
        self.assertEqual(required["env"], ["production"])

    def test_legacy_bracket_template_variable_is_filtered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "title": "PromQL",
                                "panels": [
                                    {
                                        "title": "CPU",
                                        "esql": {
                                            "query": (
                                                "PROMQL index=metrics-* step=1m "
                                                "value=(node_cpu_seconds_total{instance=\"[[instance]]\","
                                                "mode=\"idle\"})"
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

        metrics = contract["streams"]["metrics-*"]
        required = metrics.get("required_values") or {}
        self.assertNotIn("instance", required)
        self.assertEqual(required["mode"], ["idle"])


class MetricKindGaugeOverrideTests(unittest.TestCase):
    def test_node_memory_hugepages_total_is_gauge_not_counter(self):
        """``node_memory_HugePages_Total`` ends in ``_Total`` but is a gauge; it
        must not be classified as a counter (which collides with the sibling
        ``_Free`` gauge and trips a mapping ambiguity at index time)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "title": "Node",
                                "panels": [
                                    {
                                        "title": "HugePages",
                                        "esql": {
                                            "query": (
                                                "TS metrics-*\n"
                                                "| STATS a = AVG(node_memory_HugePages_Total), "
                                                "b = AVG(node_memory_HugePages_Free) "
                                                "BY time_bucket = TBUCKET(5 minute)"
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

        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertEqual(fields["node_memory_HugePages_Total"]["metric_kind"], "gauge")
        self.assertEqual(fields["node_memory_HugePages_Free"]["metric_kind"], "gauge")


class MetricKindOverrideTests(unittest.TestCase):
    def _write_packet(self, tmpdir, translated_query, source_query=""):
        artifact_dir = Path(tmpdir) / "dashboards"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "verification_packets.json").write_text(
            json.dumps(
                {
                    "packets": [
                        {
                            "dashboard": "D",
                            "panel": "P",
                            "translated_query": translated_query,
                            "source_query": source_query,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return artifact_dir

    def test_override_forces_gauge_even_over_rate_context(self):
        # A metric used inside rate() classifies as counter by query context AND by
        # the _total suffix heuristic. An explicit override must still win.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = self._write_packet(
                tmpdir,
                "PROMQL index=metrics-* step=1m value=(rate(weird_gauge_total[5m]))",
            )
            contract = build_telemetry_contract(
                artifact_dir, metric_kind_overrides={"weird_gauge_total": "gauge"}
            )
        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertEqual(fields["weird_gauge_total"]["metric_kind"], "gauge")
        self.assertEqual(fields["weird_gauge_total"]["kind_source"], "override")

    def test_override_forces_counter_over_heuristic_gauge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = self._write_packet(
                tmpdir,
                "FROM metrics-* | STATS SUM(kube_pod_container_resource_requests) BY pod",
            )
            contract = build_telemetry_contract(
                artifact_dir,
                metric_kind_overrides={"kube_pod_container_resource_requests": "counter"},
            )
        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertEqual(
            fields["kube_pod_container_resource_requests"]["metric_kind"], "counter"
        )

    def test_override_ignores_dimension_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = self._write_packet(
                tmpdir,
                "FROM metrics-* | STATS AVG(node_load1) BY pod",
            )
            # "pod" is a dimension; an override naming it must not turn it into a metric.
            contract = build_telemetry_contract(
                artifact_dir, metric_kind_overrides={"pod": "counter"}
            )
        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertEqual(fields["pod"]["role"], "dimension")
        self.assertEqual(fields["pod"].get("metric_kind", ""), "")

    def test_combined_contract_applies_overrides(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = self._write_packet(
                tmpdir,
                "PROMQL index=metrics-* step=1m value=(rate(weird_gauge_total[5m]))",
            )
            contract = build_combined_telemetry_contract(
                [artifact_dir], metric_kind_overrides={"weird_gauge_total": "gauge"}
            )
        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertEqual(fields["weird_gauge_total"]["metric_kind"], "gauge")


class FieldRelationshipTests(unittest.TestCase):
    def _contract_from_query(self, translated_query):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {"packets": [{"dashboard": "D", "panel": "P", "translated_query": translated_query}]}
                ),
                encoding="utf-8",
            )
            return build_telemetry_contract(artifact_dir)

    def test_ratio_records_denominator_relationship(self):
        contract = self._contract_from_query(
            "PROMQL index=metrics-* step=1m"
            " value=(sum(node_memory_used)/sum(node_memory_total))"
        )
        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertIn(
            {"type": "ratio_denominator", "field": "node_memory_total"},
            fields["node_memory_used"].get("relationships", []),
        )
        # The denominator itself carries no bound.
        self.assertNotIn("relationships", fields["node_memory_total"])

    def test_no_relationships_key_when_no_ratio(self):
        contract = self._contract_from_query(
            "FROM metrics-* | STATS AVG(node_load1) BY pod"
        )
        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertNotIn("relationships", fields["node_load1"])


class MetricKindFromSourceTests(unittest.TestCase):
    def test_prometheus_metadata_maps_counter_and_gauge(self):
        metadata = {
            "status": "success",
            "data": {
                "node_cpu_seconds_total": [{"type": "counter", "help": "", "unit": ""}],
                "node_load1": [{"type": "gauge", "help": "", "unit": ""}],
                "http_request_duration_seconds": [{"type": "histogram"}],
            },
        }
        self.assertEqual(
            metric_kinds_from_prometheus_metadata(metadata),
            {"node_cpu_seconds_total": "counter", "node_load1": "gauge"},
        )

    def test_prometheus_metadata_accepts_unwrapped_data(self):
        self.assertEqual(
            metric_kinds_from_prometheus_metadata({"x_total": [{"type": "counter"}]}),
            {"x_total": "counter"},
        )

    def test_prometheus_metadata_skips_conflicting_types(self):
        metadata = {"data": {"weird": [{"type": "counter"}, {"type": "gauge"}]}}
        self.assertEqual(metric_kinds_from_prometheus_metadata(metadata), {})

    def test_field_caps_maps_counter_double_and_gauge(self):
        field_caps = {
            "fields": {
                "requests_total": {"counter_double": {"time_series_metric": "counter"}},
                "memory_usage": {"double": {"time_series_metric": "gauge"}},
                "plain_value": {"double": {}},
            }
        }
        self.assertEqual(
            metric_kinds_from_field_caps(field_caps),
            {"requests_total": "counter", "memory_usage": "gauge"},
        )

    def test_merge_overrides_earliest_source_wins(self):
        rule_pack = {"shared": "counter"}
        metadata = {"shared": "gauge", "from_meta": "gauge"}
        field_caps = {"shared": "gauge", "from_meta": "counter", "from_caps": "counter"}
        self.assertEqual(
            merge_metric_kind_overrides(rule_pack, metadata, field_caps),
            {"shared": "counter", "from_meta": "gauge", "from_caps": "counter"},
        )

    def test_merge_overrides_ignores_empty_sources(self):
        self.assertEqual(
            merge_metric_kind_overrides(None, {}, {"a": "counter"}),
            {"a": "counter"},
        )

    def test_field_caps_used_as_contract_override(self):
        # The whole point: field-caps ground truth feeds the override map and wins.
        overrides = metric_kinds_from_field_caps(
            {"fields": {"kube_pod_container_resource_requests": {"double": {"time_series_metric": "gauge"}}}}
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "D",
                                "panel": "P",
                                "translated_query": (
                                    "PROMQL index=metrics-* step=1m"
                                    " value=(rate(kube_pod_container_resource_requests[5m]))"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            contract = build_telemetry_contract(
                artifact_dir, metric_kind_overrides=overrides
            )
        fields = contract["streams"]["metrics-*"]["fields"]
        self.assertEqual(
            fields["kube_pod_container_resource_requests"]["metric_kind"], "gauge"
        )


class KeywordMultifieldTests(unittest.TestCase):
    """A dimension referenced as ``<field>.keyword`` (common in migrated Datadog
    queries) must be flagged so the seeder emits the matching keyword sub-field;
    otherwise the query fails with ``Unknown column [<field>.keyword]``."""

    def _dd_dashboard(self) -> dict:
        return {
            "dashboards": [
                {
                    "title": "DD",
                    "panels": [
                        {
                            "title": "Throughput by env",
                            "esql": {
                                "query": (
                                    "FROM metrics-*\n"
                                    "| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend\n"
                                    "| STATS v = SUM(kafka_net_bytes_in_rate) "
                                    "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), "
                                    "deployment.environment.keyword\n"
                                    "| KEEP time_bucket, deployment.environment.keyword, v"
                                )
                            },
                        }
                    ],
                }
            ]
        }

    def test_dimension_referenced_with_keyword_suffix_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            yaml_dir = artifact_dir / "yaml"
            yaml_dir.mkdir(parents=True)
            (yaml_dir / "dash.yaml").write_text(
                yaml.safe_dump(self._dd_dashboard(), sort_keys=False),
                encoding="utf-8",
            )
            contract = build_telemetry_contract(artifact_dir)

        fields = contract["streams"]["metrics-*"]["fields"]
        # The base dimension is collected (suffix stripped) ...
        self.assertIn("deployment.environment", fields)
        self.assertEqual(fields["deployment.environment"]["role"], "dimension")
        # ... and flagged so a keyword sub-field is seeded.
        self.assertTrue(fields["deployment.environment"].get("keyword_multifield"))
        # The bare ``.keyword`` name is never a standalone field.
        self.assertNotIn("deployment.environment.keyword", fields)

    def test_keyword_multifield_flag_survives_contract_combination(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = Path(tmpdir) / "a" / "dashboards" / "yaml"
            second = Path(tmpdir) / "b" / "dashboards" / "yaml"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "dash.yaml").write_text(
                yaml.safe_dump(self._dd_dashboard(), sort_keys=False),
                encoding="utf-8",
            )
            # Second artifact references the same dimension WITHOUT the suffix;
            # the merged flag must still be set (true wins).
            (second / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "title": "DD2",
                                "panels": [
                                    {
                                        "title": "Plain env",
                                        "esql": {
                                            "query": (
                                                "FROM metrics-*\n"
                                                "| STATS v = SUM(system_net_bytes_rcvd) "
                                                "BY deployment.environment"
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
            combined = build_combined_telemetry_contract([first.parent, second.parent])

        fields = combined["streams"]["metrics-*"]["fields"]
        self.assertTrue(fields["deployment.environment"].get("keyword_multifield"))


if __name__ == "__main__":
    unittest.main()
