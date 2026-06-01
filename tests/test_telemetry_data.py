# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import datetime
import unittest

from observability_migration.core.telemetry_data import (
    _expand_patterns,
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

    def test_generate_documents_uses_concrete_stream_dataset_for_ambiguous_filters(self):
        contract = {
            "streams": {
                "metrics-*": {
                    "required_values": {"data_stream.dataset": ["prometheus", "datadog"]},
                    "fields": {
                        "data_stream.dataset": {"role": "dimension"},
                    },
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

        self.assertTrue(docs)
        self.assertTrue(all(index == "metrics-generic-default" for index, _doc in docs))
        self.assertTrue(all(doc["data_stream.dataset"] == "generic" for _index, doc in docs))

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
        # index.look_back_time max is 7d per ES docs; anything above is capped.
        # ref: elastic.co/docs/reference/elasticsearch/index-settings/time-series
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

    def test_generate_documents_populates_metrics_for_custom_metric_index_names(self):
        contract = {
            "streams": {
                "mig-dd-e2e": {
                    "fields": {
                        "system_cpu_user": {"role": "metric", "metric_kind": "gauge"},
                        "system_net_bytes_rcvd": {"role": "metric", "metric_kind": "counter"},
                        "host.name": {"role": "dimension"},
                    },
                    "required_values": {"host.name": ["web01"]},
                }
            }
        }

        template = plan_index_template("mig-dd-e2e", contract["streams"]["mig-dd-e2e"])
        docs = list(
            generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1,
                interval_sec=3600,
                max_combinations=1,
            )
        )

        props = template["template"]["mappings"]["properties"]
        metric_docs = [doc for index, doc in docs if index == "mig-dd-e2e"]
        self.assertEqual(props["data_stream.type"]["value"], "metrics")
        self.assertIn("mode", template["template"]["settings"]["index"])
        self.assertNotIn("message", props)
        self.assertTrue(props["host.name"]["time_series_dimension"])
        self.assertTrue(metric_docs)
        self.assertIsInstance(metric_docs[0]["system_cpu_user"], float)
        self.assertIsInstance(metric_docs[0]["system_net_bytes_rcvd"], float)
        self.assertNotIn("message", metric_docs[0])

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

    def test_generate_documents_covers_required_filter_combinations(self):
        contract = {
            "streams": {
                "logs-generic-default": {
                    "fields": {
                        "service.name": {"role": "dimension"},
                        "http.status_code": {"role": "dimension"},
                    },
                    "required_values": {
                        "service.name": ["app", "nginx"],
                        "http.status_code": ["404", "500"],
                    },
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
        log_docs = [doc for index, doc in docs if index == "logs-generic-default"]

        self.assertTrue(
            any(
                doc["service.name"] == "nginx" and doc["http.status_code"] == "404"
                for doc in log_docs
            )
        )

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

    def test_plan_index_template_tags_all_metrics_with_tsdb_type(self):
        stream = {
            "fields": {
                "trace_http_request_errors": {"role": "metric", "metric_kind": "counter"},
                "service.name": {"role": "dimension"},
            }
        }

        template = plan_index_template("metrics-*", stream)
        props = template["template"]["mappings"]["properties"]

        # All metrics in a TSDB stream get time_series_metric so the engine
        # can enforce counter/gauge semantics at query time.
        self.assertEqual(
            props["trace_http_request_errors"],
            {"type": "double", "time_series_metric": "counter"},
        )

    def test_plan_index_template_types_all_metrics_in_mixed_stream(self):
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

        # Both PROMQL-native and non-PROMQL metrics get time_series_metric typed.
        self.assertEqual(props["http_requests_total"]["time_series_metric"], "counter")
        self.assertEqual(props["trace_http_request_errors"]["time_series_metric"], "counter")

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


class CoherentGenerationTests(unittest.TestCase):
    def test_ratio_numerator_never_exceeds_denominator(self):
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "node_memory_used": {
                            "role": "metric",
                            "metric_kind": "gauge",
                            "relationships": [
                                {"type": "ratio_denominator", "field": "node_memory_total"}
                            ],
                        },
                        "node_memory_total": {"role": "metric", "metric_kind": "gauge"},
                        "host.name": {"role": "dimension"},
                    },
                    "required_values": {"host.name": ["a", "b"]},
                }
            }
        }
        docs = [
            doc
            for index, doc in generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=2,
                interval_sec=600,
                max_combinations=4,
            )
            if index == "metrics-generic-default"
        ]
        self.assertTrue(docs)
        for doc in docs:
            self.assertLessEqual(doc["node_memory_used"], doc["node_memory_total"])


class ExpandPatternsTests(unittest.TestCase):
    def test_alternation_yields_each_alternative(self):
        # Grafana multi-value template variables translate to regex alternations.
        # Each alternative is a real value the dashboard filters on.
        self.assertEqual(
            _expand_patterns("deployment.environment", ["prod|staging|dev"]),
            ["prod", "staging", "dev"],
        )

    def test_parenthesized_alternation_is_unwrapped(self):
        self.assertEqual(
            _expand_patterns("k8s.namespace.name", ["(team-a|team-b)"]),
            ["team-a", "team-b"],
        )

    def test_prefix_glob_yields_distinct_concrete_values(self):
        values = _expand_patterns("k8s.pod.name", ["nginx-.*"])
        self.assertGreaterEqual(len(values), 2)
        self.assertEqual(len(values), len(set(values)), "values must be distinct")
        self.assertTrue(all(v.startswith("nginx-") for v in values))
        # The old literal-munge behaviour produced exactly one "nginx-sample".
        self.assertNotIn("nginx-sample", values)

    def test_status_code_class_still_maps_to_concrete_code(self):
        # Regression guard: ``2..``/``5xx`` style classes must keep resolving to a
        # concrete status code so existing status-code coverage tests hold.
        self.assertEqual(_expand_patterns("http.response.status_code", ["2.."]), ["200"])
        self.assertEqual(_expand_patterns("http.response.status_code", ["5xx"]), ["500"])

    def test_pure_wildcard_falls_back_to_default_values(self):
        values = _expand_patterns("service.name", [".*"])
        self.assertEqual(len(values), 1)
        self.assertNotIn(".", values[0])


if __name__ == "__main__":
    unittest.main()
