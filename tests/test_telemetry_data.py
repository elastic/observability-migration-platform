# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import datetime
import json
import re
import tempfile
import unittest
from pathlib import Path

from observability_migration.core.telemetry_contract import build_telemetry_contract
from observability_migration.core.telemetry_data import (
    _contract_index_patterns,
    _expand_patterns,
    _value_profile,
    concrete_stream_name,
    generate_documents,
    ingest_documents,
    plan_index_template,
    purge_foreign_streams,
)


class ValueProfileTests(unittest.TestCase):
    def test_bytes_metric_is_gib_scale(self):
        p = _value_profile("node_memory_MemTotal_bytes")
        self.assertEqual(p.unit, "bytes")
        self.assertGreaterEqual(p.base, 1 << 30)  # at least 1 GiB

    def test_load_metric_is_small(self):
        p = _value_profile("node_load1")
        self.assertEqual(p.unit, "load")
        self.assertLessEqual(p.base + p.span, 8.0)

    def test_epoch_seconds_metric_is_flagged(self):
        self.assertEqual(_value_profile("node_time_seconds").unit, "epoch_seconds")
        self.assertEqual(_value_profile("node_boot_time_seconds").unit, "epoch_seconds")

    def test_ratio_metric_is_bounded(self):
        p = _value_profile("node_cpu_utilization_ratio")
        self.assertEqual(p.unit, "ratio")
        self.assertLessEqual(p.base + p.span, 1.0)

    def test_unknown_metric_uses_generic_legacy_band(self):
        p = _value_profile("some_unknown_widget_gauge")
        self.assertEqual(p.unit, "generic")
        # legacy formula base = 10 + abs(hash(name)) % 500
        self.assertEqual(p.base, 10 + abs(hash("some_unknown_widget_gauge")) % 500)


class GaugeMagnitudeTests(unittest.TestCase):
    def _docs(self, fields, now=None):
        contract = {"streams": {"metrics-*": {"fields": fields}}}
        now = now or datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC)
        return [d for _, d in generate_documents(contract, now=now, data_hours=1, interval_sec=3600)]

    def test_bytes_gauge_is_gib_scale_in_documents(self):
        fields = {"node_memory_MemTotal_bytes": {"role": "metric", "metric_kind": "gauge"}}
        vals = [d["node_memory_MemTotal_bytes"] for d in self._docs(fields)]
        self.assertTrue(vals)
        self.assertTrue(all(v >= (1 << 30) for v in vals), vals)

    def test_load_gauge_stays_small(self):
        fields = {"node_load1": {"role": "metric", "metric_kind": "gauge"}}
        vals = [d["node_load1"] for d in self._docs(fields)]
        self.assertTrue(all(0.0 <= v <= 8.0 for v in vals), vals)

    def test_epoch_seconds_near_timestamp(self):
        now = datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC)
        epoch = now.timestamp()
        fields = {"node_time_seconds": {"role": "metric", "metric_kind": "gauge"}}
        vals = [d["node_time_seconds"] for d in self._docs(fields, now=now)]
        # within 90 days before the document timestamp, never in the future
        self.assertTrue(all(epoch - 90 * 86400 <= v <= epoch + 1 for v in vals), vals)

    def test_unknown_gauge_value_unchanged_from_legacy(self):
        # Guard: an unrecognised gauge keeps the exact legacy formula output.
        # The legacy band was base + combo_idx*3 + 25*_diurnal(hour) + rng.random();
        # the generic profile (base = 10 + hash%500, span = 25) must reproduce it
        # bit-for-bit. We recompute in-process (hash is per-process salted) using
        # the real first document's hour and the first Random(42) draw.
        import random as _random

        from observability_migration.core.telemetry_data import _diurnal, _document_timestamps

        name = "widget_things"
        fields = {name: {"role": "metric", "metric_kind": "gauge"}}
        now = datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC)
        got = self._docs(fields, now=now)[0][name]

        first_ts = _document_timestamps(now, data_hours=1, interval_sec=3600)[0]
        hour = first_ts.hour + first_ts.minute / 60.0
        base = 10 + abs(hash(name)) % 500
        first_draw = _random.Random(42).random()
        expected = round(base + 0 * 3 + 25 * _diurnal(hour) + first_draw, 4)
        self.assertEqual(got, expected)


class SiblingInvariantTests(unittest.TestCase):
    def _docs(self, fields):
        contract = {"streams": {"metrics-*": {"fields": fields}}}
        now = datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC)
        return [d for _, d in generate_documents(contract, now=now, data_hours=1, interval_sec=3600)]

    def test_mem_available_not_above_total(self):
        fields = {
            "node_memory_MemTotal_bytes": {"role": "metric", "metric_kind": "gauge"},
            "node_memory_MemAvailable_bytes": {"role": "metric", "metric_kind": "gauge"},
        }
        for d in self._docs(fields):
            self.assertLessEqual(
                d["node_memory_MemAvailable_bytes"], d["node_memory_MemTotal_bytes"], d
            )

    def test_swap_free_not_above_total(self):
        fields = {
            "node_memory_SwapTotal_bytes": {"role": "metric", "metric_kind": "gauge"},
            "node_memory_SwapFree_bytes": {"role": "metric", "metric_kind": "gauge"},
        }
        for d in self._docs(fields):
            self.assertLessEqual(
                d["node_memory_SwapFree_bytes"], d["node_memory_SwapTotal_bytes"], d
            )

    def test_boot_time_not_after_now(self):
        fields = {
            "node_time_seconds": {"role": "metric", "metric_kind": "gauge"},
            "node_boot_time_seconds": {"role": "metric", "metric_kind": "gauge"},
        }
        for d in self._docs(fields):
            self.assertLessEqual(
                d["node_boot_time_seconds"], d["node_time_seconds"], d
            )


class ControlOnlyDimensionSeedingTests(unittest.TestCase):
    def test_control_only_dimension_gets_seeded_values(self):
        # nodename is a dashboard control field; it co-occurs with no metric in
        # any requirement, yet the control dropdown needs real values.
        stream = {
            "fields": {
                "node_cpu_seconds_total": {"role": "metric", "metric_kind": "counter"},
                "cpu": {"role": "dimension", "type_family": "keyword", "metric_kind": ""},
                "nodename": {"role": "dimension", "type_family": "keyword", "metric_kind": ""},
            },
            "control_fields": ["nodename"],
            "group_fields": [],
            "required_values": {},
            "required_patterns": {},
            "requirements": [
                {
                    "source": "yaml:node_exporter.yaml",
                    "index": "metrics-*",
                    "metrics": ["node_cpu_seconds_total"],
                    "dimensions": ["cpu"],
                    "control_fields": ["nodename"],
                    "group_fields": [],
                    "required_values": {},
                    "required_patterns": {},
                }
            ],
        }
        contract = {"streams": {"metrics-*": stream}}
        now = datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC)
        docs = [d for _, d in generate_documents(contract, now=now, data_hours=1, interval_sec=3600)]
        seeded = {d.get("nodename") for d in docs if d.get("nodename")}
        self.assertTrue(seeded, "nodename was never seeded with a value")


class DimensionlessMetricSeedingTests(unittest.TestCase):
    def test_dimensionless_metric_seeds_identity_dimensions(self):
        # A bare metric like ``up`` whose queries reference no dimensions must
        # still seed documents carrying at least one time_series_dimension
        # value: TSDB routing rejects dimensionless documents wholesale
        # ("Error extracting routing: source didn't contain any routing
        # fields"), silently un-seeding the metric. Real Prometheus series
        # always carry identity labels, so fall back to the stream's identity
        # dimensions (instance/job).
        stream = {
            "fields": {
                "up": {"role": "metric", "metric_kind": "gauge"},
                "node_cpu_seconds_total": {"role": "metric", "metric_kind": "counter"},
                "instance": {"role": "dimension", "type_family": "keyword", "metric_kind": ""},
                "job": {"role": "dimension", "type_family": "keyword", "metric_kind": ""},
            },
            "control_fields": [],
            "group_fields": [],
            "required_values": {},
            "required_patterns": {},
            "requirements": [
                {
                    "source": "verification_packet:Home:Target Health Status",
                    "index": "metrics-*",
                    "metrics": ["up"],
                    "dimensions": [],
                    "control_fields": [],
                    "group_fields": [],
                    "required_values": {},
                    "required_patterns": {},
                },
                {
                    "source": "verification_packet:NEF:CPU",
                    "index": "metrics-*",
                    "metrics": ["node_cpu_seconds_total"],
                    "dimensions": ["instance"],
                    "control_fields": [],
                    "group_fields": [],
                    "required_values": {},
                    "required_patterns": {},
                },
            ],
        }
        contract = {"streams": {"metrics-*": stream}}
        now = datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC)
        docs = [d for _, d in generate_documents(contract, now=now, data_hours=1, interval_sec=3600)]
        up_docs = [d for d in docs if "up" in d]
        self.assertTrue(up_docs, "up was never seeded")
        for doc in up_docs:
            self.assertTrue(
                doc.get("instance") or doc.get("job"),
                f"dimensionless doc would fail TSDB routing: {doc}",
            )


class TelemetryDataTests(unittest.TestCase):
    def test_generate_documents_satisfies_logs_is_not_null_presence_field(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir)
            (artifact_dir / "verification_packets.json").write_text(
                json.dumps(
                    {
                        "packets": [
                            {
                                "dashboard": "Logs",
                                "panel": "Errors present",
                                "translated_query": (
                                    "FROM logs-*\n"
                                    "| WHERE error.message IS NOT NULL\n"
                                    "| STATS count = COUNT(*)"
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            contract = build_telemetry_contract(artifact_dir)

        docs = [
            doc
            for _index, doc in generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1,
                interval_sec=3600,
            )
        ]

        self.assertTrue(docs)
        self.assertTrue(
            any("error.message" in doc for doc in docs),
            "log presence field was mapped but never seeded",
        )

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

    def test_histogram_bucket_counter_is_cumulative_across_le(self):
        # A Prometheus cumulative histogram requires bucket(le=v) to be
        # monotonically non-decreasing as v increases, at a fixed timestamp and
        # non-le dimension set. Independent per-le counters break
        # histogram_quantile(); the seeder must make *_bucket cumulative over le.
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "request_duration_seconds_bucket": {
                            "role": "metric",
                            "metric_kind": "counter",
                        },
                        "le": {"role": "dimension"},
                    },
                    "required_patterns": {"le": ["0.1|0.5|1|2.5|5|10"]},
                }
            }
        }

        docs = list(
            generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1,
                interval_sec=3600,
                max_combinations=50,
            )
        )

        # All six le boundaries must be present (cap must not drop buckets).
        seeded_le = {doc["le"] for _i, doc in docs}
        self.assertEqual(seeded_le, {"0.1", "0.5", "1", "2.5", "5", "10"})

        # At each timestamp, sort by numeric le and assert non-decreasing values.
        by_ts: dict[str, list[tuple[float, float]]] = {}
        for _index, doc in docs:
            by_ts.setdefault(doc["@timestamp"], []).append(
                (float(doc["le"]), doc["request_duration_seconds_bucket"])
            )
        self.assertTrue(by_ts)
        for ts, pairs in by_ts.items():
            ordered = [value for _le, value in sorted(pairs)]
            self.assertEqual(
                ordered,
                sorted(ordered),
                msg=f"bucket values not cumulative across le at {ts}: {sorted(pairs)}",
            )

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

    def test_generate_documents_scopes_dimensions_to_cooccurring_metrics(self):
        # Multiple dashboards collapse into one ``metrics-*`` stream, so the stream
        # carries every metric and every dimension. A document for ``node_cpu`` must
        # only carry the dimensions that co-occur with it in a query (cpu/mode), not
        # unrelated ones harvested from other dashboards (balancer/chip). Otherwise
        # legends and breakdowns cross-contaminate and per-dimension cardinality
        # collapses under the combination cap.
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "node_cpu_seconds_total": {"role": "metric", "metric_kind": "counter"},
                        "apache_proxy_balancer_busy": {"role": "metric", "metric_kind": "gauge"},
                        "cpu": {"role": "dimension"},
                        "mode": {"role": "dimension"},
                        "balancer": {"role": "dimension"},
                        "chip": {"role": "dimension"},
                    },
                    "group_fields": ["cpu", "mode", "balancer", "chip"],
                    "required_values": {},
                    "required_patterns": {},
                    "requirements": [
                        {"metrics": ["node_cpu_seconds_total"], "dimensions": ["cpu", "mode"],
                         "group_fields": ["cpu", "mode"]},
                        {"metrics": ["apache_proxy_balancer_busy"], "dimensions": ["balancer", "chip"],
                         "group_fields": ["balancer", "chip"]},
                    ],
                }
            }
        }
        docs = [
            doc for _, doc in generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1, interval_sec=3600,
            )
        ]
        cpu_docs = [d for d in docs if "node_cpu_seconds_total" in d]
        bal_docs = [d for d in docs if "apache_proxy_balancer_busy" in d]
        self.assertTrue(cpu_docs)
        self.assertTrue(bal_docs)
        # node_cpu docs carry cpu/mode and NOT balancer/chip.
        for d in cpu_docs:
            self.assertIn("cpu", d)
            self.assertNotIn("balancer", d, f"phantom dimension leaked onto node_cpu doc: {sorted(d)}")
            self.assertNotIn("chip", d)
        # balancer docs carry balancer/chip and NOT cpu/mode.
        for d in bal_docs:
            self.assertIn("balancer", d)
            self.assertNotIn("cpu", d, f"phantom dimension leaked onto balancer doc: {sorted(d)}")
        # The two metrics are not co-located in the same document.
        self.assertFalse(any("node_cpu_seconds_total" in d and "apache_proxy_balancer_busy" in d for d in docs))

    def test_generate_documents_dimension_cardinality_not_collapsed_by_unrelated_dims(self):
        # With scoping, node_cpu's ``cpu`` should reach its full default cardinality
        # (3 values) instead of being starved to 1 by the global combination cap
        # being consumed by dozens of unrelated dimensions.
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "node_cpu_seconds_total": {"role": "metric", "metric_kind": "counter"},
                        "cpu": {"role": "dimension"},
                        **{f"d{i}": {"role": "dimension"} for i in range(20)},
                    },
                    "group_fields": ["cpu", *[f"d{i}" for i in range(20)]],
                    "required_values": {},
                    "required_patterns": {},
                    "requirements": [
                        {"metrics": ["node_cpu_seconds_total"], "dimensions": ["cpu"], "group_fields": ["cpu"]},
                    ],
                }
            }
        }
        docs = [
            doc for _, doc in generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1, interval_sec=3600, max_combinations=12,
            )
        ]
        cpu_values = {d.get("cpu") for d in docs if "node_cpu_seconds_total" in d}
        self.assertGreaterEqual(len(cpu_values), 3, f"cpu cardinality collapsed: {cpu_values}")

    def test_group_field_coverage_survives_high_cardinality_sibling(self):
        # node_cpu groups by cpu AND mode; mode has 8 real values which exhaust the
        # combination cap, leaving cpu pinned to a single value under a plain
        # cartesian product. Every group field must reach its full value set so the
        # legend shows all cpus and all modes.
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "node_cpu_seconds_total": {"role": "metric", "metric_kind": "counter"},
                        "cpu": {"role": "dimension"},
                        "mode": {"role": "dimension"},
                    },
                    "group_fields": ["cpu", "mode"],
                    "required_values": {
                        "mode": ["user", "system", "idle", "iowait", "irq", "softirq", "steal", "nice"],
                    },
                    "required_patterns": {},
                }
            }
        }
        docs = [
            doc for _, doc in generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1, interval_sec=3600, max_combinations=12,
            )
        ]
        cpu_values = {d.get("cpu") for _, d in [(None, d) for d in docs]}
        mode_values = {d.get("mode") for d in docs}
        self.assertGreaterEqual(len(cpu_values), 3, f"cpu cardinality collapsed by mode: {cpu_values}")
        self.assertGreaterEqual(len(mode_values), 8, f"mode coverage lost: {mode_values}")

    def test_relabeled_required_value_is_not_seeded_verbatim(self):
        # instance="$host:$port" is relabeled to ``label_host:label_port`` and recorded
        # as an exact required_value (not a pattern). It is not a real instance value;
        # seeding it verbatim produces a weird legend. It must be dropped in favour of
        # clean defaults.
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "node_load1": {"role": "metric", "metric_kind": "gauge"},
                        "instance": {"role": "dimension"},
                    },
                    "group_fields": ["instance"],
                    "required_values": {"instance": ["label_host:label_port", "1:1"]},
                    "required_patterns": {},
                }
            }
        }
        docs = [
            doc for _, doc in generate_documents(
                contract,
                now=datetime.datetime(2026, 4, 15, 6, 0, tzinfo=datetime.UTC),
                data_hours=1, interval_sec=3600,
            )
        ]
        instances = {d.get("instance") for d in docs}
        self.assertTrue(instances)
        for value in instances:
            self.assertNotIn("label_", value, f"relabeled required_value leaked: {value!r}")


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

    def test_character_class_alternation_does_not_leak_raw_regex(self):
        # node-exporter disk panels filter device=~"[a-z]+|nvme[0-9]+n[0-9]+|
        # mmcblk[0-9]+". The alternatives are regexes, not literals, so emitting
        # them verbatim seeds a single series legended with the raw regex string.
        # Fall back to clean default device values instead.
        values = _expand_patterns("device", ["[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+"])
        self.assertTrue(values, "must still seed *something* so the panel has data")
        for v in values:
            self.assertNotRegex(v, r"[\[\]()|+^$\\]", f"regex metachars leaked into value {v!r}")

    def test_regex_alternation_values_satisfy_the_source_regex(self):
        # The whole point of seeding device=~"[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+"
        # is that a panel filtering on that regex finds data. Clean-but-non-matching
        # values (e.g. "device_1") leave the panel empty (Prometheus =~ is a FULL
        # match). Every seeded value MUST fullmatch the source regex.
        pattern = "[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+"
        values = _expand_patterns("device", [pattern])
        self.assertTrue(values)
        compiled = re.compile(pattern)
        for v in values:
            self.assertTrue(
                compiled.fullmatch(v),
                f"seeded value {v!r} does not fullmatch source regex {pattern!r}",
            )

    def test_bare_character_class_value_satisfies_regex(self):
        # A bare "[a-z]+" should seed a lowercase-letters value (e.g. a disk like
        # "sda"), not "device_1" (which contains a digit/underscore and fails [a-z]+).
        pattern = "[a-z]+"
        values = _expand_patterns("device", [pattern])
        self.assertTrue(values)
        compiled = re.compile(pattern)
        for v in values:
            self.assertTrue(compiled.fullmatch(v), f"{v!r} must fullmatch {pattern!r}")

    def test_literal_with_digit_class_satisfies_regex(self):
        # eth[0-9]+ / nvme[0-9]+n[0-9]+ should yield matching interface/disk names.
        for pattern in ("eth[0-9]+", "nvme[0-9]+n[0-9]+", "mmcblk[0-9]+"):
            values = _expand_patterns("device", [pattern])
            self.assertTrue(values, f"no values for {pattern!r}")
            compiled = re.compile(pattern)
            for v in values:
                self.assertTrue(compiled.fullmatch(v), f"{v!r} must fullmatch {pattern!r}")

    def test_partial_literal_regex_does_not_leak_metachars(self):
        # ``.*irq`` previously became the nonsense literal "sampleirq". A trailing
        # literal stem is fine, but the value must not contain regex metachars and
        # must not be a munged "sample"-prefixed token.
        values = _expand_patterns("mode", [".*irq"])
        self.assertTrue(values)
        for v in values:
            self.assertNotRegex(v, r"[\[\]()|+^$\\*.]", f"regex metachars leaked into value {v!r}")
            self.assertNotIn("sample", v)

    def test_wildcard_flanked_literal_seeds_matching_substring_value(self):
        # ``WHERE <dim> RLIKE ".*Foo.*"`` is a substring filter. A clean default
        # (e.g. "checkout") does not contain "Foo", so the panel returns zero
        # rows. The literal core must be seeded so a fullmatch succeeds.
        for pattern in (".*Foo.*", "app_.+"):
            values = _expand_patterns("service_name", [pattern])
            self.assertTrue(values, f"no values for {pattern!r}")
            compiled = re.compile(pattern)
            for v in values:
                self.assertTrue(compiled.fullmatch(v), f"{v!r} must fullmatch {pattern!r}")

    def test_one_or_more_flanked_literal_seeds_matching_value(self):
        # ``.+`` requires at least one character, unlike ``.*``. Keep enough
        # padding around the literal core that the seeded value still fullmatches.
        pattern = ".+Foo.+"
        values = _expand_patterns("service_name", [pattern])
        self.assertTrue(values)
        compiled = re.compile(pattern)
        for v in values:
            self.assertTrue(compiled.fullmatch(v), f"{v!r} must fullmatch {pattern!r}")

    def test_relabeled_template_var_value_is_not_seeded_literally(self):
        # ``instance="$node:$port"`` is relabeled to ``label_node:label_port`` by the
        # migrator. That is not a real label value; seeding it produces series no
        # panel query matches. Fall back to default instance values.
        values = _expand_patterns("instance", ["label_node:label_port"])
        self.assertTrue(values)
        for v in values:
            self.assertNotIn("label_", v, f"relabeled template var leaked into value {v!r}")


class _RecordingRequest:
    """Minimal RequestFn stub that serves canned ``GET /_data_stream`` listings
    and records DELETE calls so the purge logic can be asserted in isolation."""

    def __init__(self, listings: dict[str, dict]):
        self._listings = listings
        self.deletes: list[str] = []

    def __call__(self, method, path, body=None, content_type="application/json"):
        if method == "GET" and path.startswith("/_data_stream/"):
            pattern = path[len("/_data_stream/") :]
            return self._listings.get(pattern, {"data_streams": []})
        if method == "DELETE" and path.startswith("/_data_stream/"):
            self.deletes.append(path[len("/_data_stream/") :])
            return {"acknowledged": True}
        return {}


class PurgeForeignStreamsTests(unittest.TestCase):
    def test_contract_index_patterns_reduce_to_type_wildcards(self):
        contract = {
            "streams": {
                "metrics-prometheus-*": {},
                "metrics-*": {},
                "logs-*": {},
            }
        }
        self.assertEqual(
            sorted(_contract_index_patterns(contract)), ["logs-*", "metrics-*"]
        )

    def test_purge_removes_only_non_seeder_streams(self):
        request = _RecordingRequest(
            {
                "metrics-*": {
                    "data_streams": [
                        {
                            "name": "metrics-generic-default",
                            "template": "telemetry-data-metrics-generic-default",
                        },
                        {
                            "name": "metrics-express.prometheus-parity",
                            "template": "metrics-prometheus@template",
                        },
                        {
                            "name": "metrics-parity.test-default",
                            "template": "parity-metrics-parity.test-default",
                        },
                    ]
                },
                "logs-*": {
                    "data_streams": [
                        {
                            "name": "logs-generic-default",
                            "template": "telemetry-data-logs-generic-default",
                        },
                    ]
                },
            }
        )
        contract = {"streams": {"metrics-*": {}, "logs-*": {}}}

        deleted = purge_foreign_streams(contract, request)

        self.assertEqual(
            sorted(deleted),
            ["metrics-express.prometheus-parity", "metrics-parity.test-default"],
        )
        # Seeder-owned streams are never deleted.
        self.assertNotIn("metrics-generic-default", request.deletes)
        self.assertNotIn("logs-generic-default", request.deletes)

    def test_purge_is_noop_when_only_seeder_streams_exist(self):
        request = _RecordingRequest(
            {
                "metrics-*": {
                    "data_streams": [
                        {
                            "name": "metrics-generic-default",
                            "template": "telemetry-data-metrics-generic-default",
                        }
                    ]
                }
            }
        )
        deleted = purge_foreign_streams({"streams": {"metrics-*": {}}}, request)
        self.assertEqual(deleted, [])
        self.assertEqual(request.deletes, [])

    def test_purge_dedupes_streams_seen_through_multiple_patterns(self):
        # Same foreign stream is returned for both wildcards; delete it once.
        foreign = {
            "name": "metrics-express.prometheus-parity",
            "template": "metrics-prometheus@template",
        }
        request = _RecordingRequest(
            {
                "metrics-*": {"data_streams": [foreign]},
                "metrics-prometheus-*": {"data_streams": [foreign]},
            }
        )
        contract = {"streams": {"metrics-*": {}, "metrics-prometheus-*": {}}}
        deleted = purge_foreign_streams(contract, request)
        self.assertEqual(deleted, ["metrics-express.prometheus-parity"])
        self.assertEqual(request.deletes, ["metrics-express.prometheus-parity"])


class KeywordMultifieldMappingTests(unittest.TestCase):
    def test_dimension_with_keyword_multifield_emits_subfield(self):
        contract = {
            "streams": {
                "metrics-*": {
                    "fields": {
                        "http_requests_total": {
                            "role": "metric",
                            "metric_kind": "counter",
                        },
                        "deployment.environment": {
                            "role": "dimension",
                            "keyword_multifield": True,
                        },
                        "host.name": {"role": "dimension"},
                    }
                }
            }
        }
        props = plan_index_template(
            "metrics-*", contract["streams"]["metrics-*"]
        )["template"]["mappings"]["properties"]

        env = props["deployment.environment"]
        self.assertEqual(env["type"], "keyword")
        self.assertTrue(env.get("time_series_dimension"))
        self.assertEqual(env["fields"]["keyword"]["type"], "keyword")
        # A dimension without the flag gets no sub-field.
        self.assertNotIn("fields", props["host.name"])


class IngestAccountingTests(unittest.TestCase):
    """``ingest_documents`` must never silently lose documents.

    A ``_bulk`` request can fail as a whole (HTTP 4xx/5xx, or a 413 payload-too-
    large) and come back as an error *envelope* with no per-item ``items`` array.
    The old accounting only counted docs that appeared in ``items``; when a batch
    returned no items, those docs were neither ``ok`` nor ``errors`` -- they
    vanished, and the seed reported success while most data never landed. That is
    the bug that made the oracle benchmark read a false 0% parity.
    """

    @staticmethod
    def _docs(n: int):
        for i in range(n):
            yield "metrics-generic-default", {"@timestamp": "2026-06-05T00:00:00.000Z", "v": float(i)}

    def test_bulk_error_envelope_without_items_is_counted_as_errors(self):
        def request(method, path, body=None, content_type="application/json"):
            # Simulate a batch the server rejected wholesale (no per-item results).
            return {"error": {"status": 413, "reason": "request entity too large"}}

        summary = ingest_documents(self._docs(10), request, batch_docs=10)

        # All 10 attempted docs must be accounted for as failures, not lost.
        self.assertEqual(summary.ok, 0)
        self.assertEqual(summary.errors, 10)
        self.assertTrue(summary.error_samples)

    def test_oversized_batch_is_split_and_retried_so_docs_land(self):
        # The server rejects any batch larger than 4 docs (e.g. payload limit /
        # transient 429), but accepts smaller ones. A robust ingester must split
        # and retry so the data still lands instead of being written off.
        def request(method, path, body=None, content_type="application/json"):
            lines = body.decode().strip().split("\n") if isinstance(body, (bytes, bytearray)) else []
            doc_lines = len(lines) // 2
            if doc_lines > 4:
                return {"error": {"status": 413, "reason": "request entity too large"}}
            return {"items": [{"create": {"status": 201}} for _ in range(doc_lines)]}

        summary = ingest_documents(self._docs(10), request, batch_docs=10)

        self.assertEqual(summary.ok, 10)
        self.assertEqual(summary.errors, 0)

    def test_attempted_equals_ok_plus_errors(self):
        # Docs whose index name starts with "bad-" are unrecoverably rejected
        # (the envelope has no items even for a single doc); "metrics-" docs
        # succeed. Every attempted doc must land in exactly one of ok/errors.
        def request(method, path, body=None, content_type="application/json"):
            lines = body.decode().strip().split("\n") if isinstance(body, (bytes, bytearray)) else []
            actions = [json.loads(line) for line in lines[0::2]]
            indices = [(a.get("create") or a.get("index") or {}).get("_index", "") for a in actions]
            if any(ix.startswith("bad-") for ix in indices):
                # Unrecoverable: fails even when split to a single doc.
                return {"error": {"status": 400, "reason": "mapping conflict"}}
            return {"items": [{"create": {"status": 201}} for _ in indices]}

        def mixed_docs():
            for i in range(5):
                yield "metrics-generic-default", {"@timestamp": "2026-06-05T00:00:00.000Z", "v": float(i)}
            for i in range(5):
                yield "bad-stream", {"@timestamp": "2026-06-05T00:00:00.000Z", "v": float(i)}

        summary = ingest_documents(mixed_docs(), request, batch_docs=10)

        attempted = sum(summary.docs_per_stream.values())
        self.assertEqual(attempted, 10)
        self.assertEqual(summary.ok + summary.errors, attempted)
        self.assertEqual(summary.errors, 5)
        self.assertEqual(summary.ok, 5)


if __name__ == "__main__":
    unittest.main()
