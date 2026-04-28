# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import datetime
import unittest

from observability_migration.core.telemetry_data import (
    concrete_stream_name,
    generate_documents,
    plan_index_template,
)


class TelemetryDataTests(unittest.TestCase):
    def test_concrete_stream_name_preserves_dataset_when_known(self):
        self.assertEqual(concrete_stream_name("metrics-prometheus-*"), "metrics-prometheus-default")
        self.assertEqual(concrete_stream_name("metrics-*"), "metrics-generic-default")
        self.assertEqual(concrete_stream_name("logs-*"), "logs-generic-default")

    def test_required_data_stream_dataset_selects_matching_concrete_stream(self):
        contract = {
            "streams": {
                "metrics-*": {
                    "required_values": {"data_stream.dataset": ["prometheus"]},
                    "fields": {
                        "http_requests_total": {"role": "metric", "metric_kind": "counter"},
                        "data_stream.dataset": {"role": "dimension"},
                    },
                }
            }
        }

        template = plan_index_template("metrics-*", contract["streams"]["metrics-*"])
        docs = list(
            generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1,
                interval_sec=3600,
            )
        )

        self.assertEqual(template["index_patterns"], ["metrics-prometheus-default"])
        self.assertEqual(
            template["template"]["mappings"]["properties"]["data_stream.dataset"]["value"],
            "prometheus",
        )
        self.assertTrue(docs)
        self.assertTrue(all(index == "metrics-prometheus-default" for index, _doc in docs))
        self.assertTrue(all(doc["data_stream.dataset"] == "prometheus" for _index, doc in docs))

    def test_plan_index_template_maps_generated_control_dimensions(self):
        stream = {
            "fields": {
                "http_requests_total": {"role": "metric", "metric_kind": "counter"},
            },
            "control_fields": ["service.name"],
            "required_values": {"deployment.environment": ["production"]},
            "group_fields": ["http.route"],
        }

        template = plan_index_template("metrics-*", stream)
        props = template["template"]["mappings"]["properties"]

        self.assertTrue(props["service.name"]["time_series_dimension"])
        self.assertTrue(props["deployment.environment"]["time_series_dimension"])
        self.assertTrue(props["http.route"]["time_series_dimension"])

    def test_plan_index_template_caps_tsdb_lookback_at_serverless_limit(self):
        stream = {
            "minimum_lookback": "14 days",
            "fields": {
                "http_requests_total": {"role": "metric", "metric_kind": "counter"},
                "service.name": {"role": "dimension"},
            },
        }

        template = plan_index_template("metrics-*", stream)

        self.assertEqual(
            template["template"]["settings"]["index"]["look_back_time"],
            "7d",
        )

    def test_generate_documents_does_not_treat_metric_names_as_dimensions(self):
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "cpu": {"role": "metric", "metric_kind": "gauge"},
                        "host.name": {"role": "dimension"},
                    },
                    "group_fields": ["cpu", "host.name"],
                }
            }
        }

        template = plan_index_template("metrics-*", contract["streams"]["metrics-*"])
        docs = list(
            generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1,
                interval_sec=3600,
            )
        )

        props = template["template"]["mappings"]["properties"]
        self.assertNotIn("time_series_dimension", props["cpu"])
        self.assertIsInstance(docs[0][1]["cpu"], float)
        self.assertGreaterEqual(len(docs), 6)

    def test_generate_documents_covers_required_values_beyond_max_combinations(self):
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "node_cpu_seconds_total": {"role": "metric", "metric_kind": "counter"},
                        "mode": {"role": "dimension"},
                        "http.response.status_code": {"role": "dimension"},
                    },
                    "required_values": {"mode": ["idle", "system"]},
                    "required_patterns": {"http.response.status_code": ["2.."]},
                }
            }
        }

        docs = list(
            generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1,
                interval_sec=3600,
                max_combinations=1,
            )
        )
        metric_docs = [doc for index, doc in docs if index == "metrics-generic-default"]

        self.assertTrue(any(doc["mode"] == "system" for doc in metric_docs))
        self.assertTrue(any(doc["http.response.status_code"] == "200" for doc in metric_docs))

    def test_generate_documents_adds_dense_recent_points_for_short_rate_windows(self):
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "node_disk_reads_completed_total": {
                            "role": "metric",
                            "metric_kind": "counter",
                        },
                        "device": {"role": "dimension"},
                    },
                }
            }
        }

        docs = list(
            generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=2,
                interval_sec=3600,
                max_combinations=1,
            )
        )
        timestamps = sorted({doc["@timestamp"] for _index, doc in docs})
        recent_timestamps = [
            timestamp
            for timestamp in timestamps
            if timestamp >= "2026-04-15T05:00:00.000Z"
        ]

        self.assertGreaterEqual(len(recent_timestamps), 60)
        self.assertIn("2026-04-15T05:59:00.000Z", recent_timestamps)

    def test_plan_index_template_maps_metrics_and_dimensions_without_source_families(self):
        stream = {
            "requires_native_promql": True,
            "fields": {
                "http_requests_total": {
                    "role": "metric",
                    "metric_kind": "counter",
                    "requires_native_promql": True,
                },
                "service.name": {"role": "dimension"},
                "http.response.status_code": {"role": "dimension"},
            }
        }

        template = plan_index_template("metrics-*", stream)
        props = template["template"]["mappings"]["properties"]

        self.assertEqual(template["index_patterns"], ["metrics-generic-default"])
        self.assertEqual(template["priority"], 1000)
        self.assertEqual(props["http_requests_total"]["type"], "double")
        self.assertEqual(props["http_requests_total"]["time_series_metric"], "counter")
        self.assertTrue(props["service.name"]["time_series_dimension"])
        self.assertTrue(props["http.response.status_code"]["time_series_dimension"])
        self.assertEqual(
            template["template"]["settings"]["index"]["routing_path"],
            ["http.response.status_code", "service.name"],
        )

    def test_plan_index_template_keeps_non_promql_metrics_plain_numeric(self):
        stream = {
            "fields": {
                "trace_http_request_errors": {"role": "metric", "metric_kind": "counter"},
                "service.name": {"role": "dimension"},
            }
        }

        template = plan_index_template("metrics-*", stream)
        props = template["template"]["mappings"]["properties"]

        self.assertEqual(props["trace_http_request_errors"], {"type": "double"})

    def test_plan_index_template_only_marks_promql_metrics_as_time_series_metrics(self):
        stream = {
            "requires_native_promql": True,
            "fields": {
                "http_requests_total": {
                    "role": "metric",
                    "metric_kind": "counter",
                    "requires_native_promql": True,
                },
                "trace_http_request_errors": {
                    "role": "metric",
                    "metric_kind": "counter",
                },
                "service.name": {"role": "dimension"},
            },
        }

        template = plan_index_template("metrics-generic-*", stream)
        props = template["template"]["mappings"]["properties"]

        self.assertEqual(props["http_requests_total"]["time_series_metric"], "counter")
        self.assertEqual(props["trace_http_request_errors"], {"type": "double"})

    def test_generate_documents_satisfies_literals_patterns_groups_and_metric_kinds(self):
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "http_requests_total": {"role": "metric", "metric_kind": "counter"},
                        "http.response.status_code": {"role": "dimension"},
                        "http.request.method": {"role": "dimension"},
                        "http.route": {"role": "dimension"},
                        "deployment.environment": {"role": "dimension"},
                    },
                    "group_fields": ["http.route"],
                    "required_values": {
                        "http.request.method": ["POST"],
                        "deployment.environment": ["production"],
                    },
                    "required_patterns": {"http.response.status_code": ["5.."]},
                },
                "logs-*": {
                    "fields": {
                        "log.level": {"role": "dimension"},
                        "service.name": {"role": "dimension"},
                        "http.url": {"role": "dimension"},
                    },
                    "group_fields": ["http.url"],
                    "required_values": {
                        "log.level": ["error"],
                        "service.name": ["checkout"],
                    },
                    "required_patterns": {},
                },
            }
        }

        docs = list(
            generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1,
                interval_sec=3600,
            )
        )

        metric_docs = [doc for index, doc in docs if index == "metrics-generic-default"]
        log_docs = [doc for index, doc in docs if index == "logs-generic-default"]
        self.assertTrue(metric_docs)
        self.assertTrue(log_docs)
        self.assertTrue(any(doc["http.request.method"] == "POST" for doc in metric_docs))
        self.assertTrue(any(str(doc["http.response.status_code"]).startswith("5") for doc in metric_docs))
        self.assertTrue(any(doc["http.route"] for doc in metric_docs))
        self.assertTrue(any(doc["deployment.environment"] == "production" for doc in metric_docs))
        self.assertTrue(any(doc["log.level"] == "error" for doc in log_docs))
        self.assertTrue(any(doc["service.name"] == "checkout" for doc in log_docs))
        first_counter = metric_docs[0]["http_requests_total"]
        last_counter = metric_docs[-1]["http_requests_total"]
        self.assertGreater(last_counter, first_counter)

    def test_generate_documents_provides_default_environment_filter_values(self):
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "system_cpu_user": {"role": "metric", "metric_kind": "gauge"},
                        "deployment.environment": {"role": "dimension"},
                        "host.name": {"role": "dimension"},
                        "http.route": {"role": "dimension"},
                        "k8s.namespace.name": {"role": "dimension"},
                        "service.name": {"role": "dimension"},
                    },
                    "control_fields": ["deployment.environment"],
                    "group_fields": ["host.name", "http.route", "k8s.namespace.name", "service.name"],
                    "required_values": {},
                    "required_patterns": {},
                }
            }
        }

        docs = list(
            generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1,
                interval_sec=3600,
            )
        )

        environments = {doc["deployment.environment"] for _, doc in docs}
        self.assertIn("production", environments)
        self.assertIn("staging", environments)
        self.assertIn("development", environments)


if __name__ == "__main__":
    unittest.main()
