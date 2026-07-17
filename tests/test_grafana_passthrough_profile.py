# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana ``--field-profile passthrough`` support.

The Datadog adapter has always exposed named field profiles (``otel``,
``passthrough``, ``elastic_agent``). Grafana maps labels dynamically through the
``SchemaResolver`` and historically hard-rejected every profile except ``otel``.
The ``passthrough`` profile disables automatic OTel/Prometheus normalization and
emits source label/metric names verbatim, while still honoring explicit
rule-pack overrides.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from observability_migration.adapters.source.grafana import alert_pipeline, panels
from observability_migration.adapters.source.grafana import cli as grafana_cli
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)
from observability_migration.core.assets.alerting import (
    build_alerting_ir_from_grafana_unified,
)
from observability_migration.core.mapping import build_es_query_rule_params
from observability_migration.core.reporting.report import print_field_discovery_warning


def _passthrough_resolver(rule_pack=None, field_cache=None):
    resolver = SchemaResolver(
        rule_pack or RulePackConfig(),
        es_url="https://es",
        index_pattern="metrics-*",
        passthrough=True,
    )
    # Seed live caps that WOULD trigger OTel/native remapping under `otel`, so a
    # passing test proves passthrough short-circuits before discovery/mapping.
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = dict(field_cache or {})
    return resolver


def _prometheus_alert_rule(expr):
    return {
        "uid": "passthrough-rule",
        "title": "Passthrough alert",
        "ruleGroup": "group",
        "folderUID": "folder",
        "condition": "C",
        "for": "5m",
        "noDataState": "OK",
        "execErrState": "Error",
        "isPaused": False,
        "labels": {},
        "annotations": {},
        "data": [
            {
                "refId": "A",
                "datasourceUid": "prometheus",
                "relativeTimeRange": {"from": 300, "to": 0},
                "model": {"expr": expr},
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "relativeTimeRange": {"from": 0, "to": 0},
                "model": {
                    "type": "threshold",
                    "conditions": [
                        {
                            "evaluator": {
                                "type": "gt",
                                "params": [80],
                            }
                        }
                    ],
                },
            },
        ],
    }


class TestPassthroughLabelResolution(unittest.TestCase):
    def test_label_emitted_verbatim_even_when_otel_alias_exists(self):
        # `instance` normally remaps to `service.instance.id` under `otel`.
        resolver = _passthrough_resolver(
            field_cache={"instance": {}, "service.instance.id": {}}
        )
        self.assertEqual(resolver.resolve_label("instance"), "instance")
        self.assertEqual(resolver.resolve_label("namespace"), "namespace")

    def test_metric_scoped_label_does_not_probe_or_remap(self):
        resolver = _passthrough_resolver(
            field_cache={"instance": {}, "service.instance.id": {}}
        )

        with (
            patch.object(resolver, "_discover_fields") as discover,
            patch.object(resolver, "_cooccurring_candidates") as probe,
        ):
            self.assertEqual(
                resolver.resolve_label("instance", metric_field="redis_up"),
                "instance",
            )

        discover.assert_not_called()
        probe.assert_not_called()

    def test_label_rewrites_still_win(self):
        rule_pack = RulePackConfig()
        rule_pack.label_rewrites = {"instance": "host.name"}
        resolver = _passthrough_resolver(rule_pack=rule_pack)
        self.assertEqual(resolver.resolve_label("instance"), "host.name")

    def test_ignored_labels_still_dropped(self):
        rule_pack = RulePackConfig()
        rule_pack.ignored_labels = ["origin_prometheus"]
        resolver = _passthrough_resolver(rule_pack=rule_pack)
        self.assertIsNone(resolver.resolve_label("origin_prometheus"))

    def test_control_field_overrides_still_win(self):
        rule_pack = RulePackConfig()
        rule_pack.control_field_overrides = {"node": "host.name"}
        resolver = _passthrough_resolver(rule_pack=rule_pack)
        self.assertEqual(resolver.resolve_control_field("node"), "host.name")
        # A control without an override falls through to verbatim passthrough.
        self.assertEqual(resolver.resolve_control_field("job"), "job")

    def test_metric_field_emitted_verbatim_on_native_shaped_cache(self):
        # A `metrics.<name>` cache would make `otel` emit `metrics.node_up`;
        # passthrough must keep the bare Prometheus metric name.
        resolver = _passthrough_resolver(
            field_cache={"metrics.node_up": {}, "labels.instance": {}}
        )
        self.assertEqual(resolver.resolve_metric_field("node_up"), "node_up")

    def test_counter_classification_ignores_remote_write_field(self):
        resolver = _passthrough_resolver(
            field_cache={
                "prometheus.requests.counter": {
                    "long": {"type": "long", "time_series_metric": "counter"}
                },
                "prometheus.labels.instance": {"keyword": {"type": "keyword"}},
            }
        )

        # Strict passthrough emits `requests`, so a counter capability attached
        # only to `prometheus.requests.counter` must not influence the query
        # operator selected for the bare field.
        self.assertFalse(resolver.is_counter("requests"))

    def test_counter_classification_ignores_native_field(self):
        resolver = _passthrough_resolver(
            field_cache={
                "metrics.requests": {
                    "double": {"type": "double", "time_series_metric": "counter"}
                },
                "labels.instance": {"keyword": {"type": "keyword"}},
            }
        )

        self.assertFalse(resolver.is_counter("requests"))

    def test_live_discovery_skips_automatic_mapping_build(self):
        resolver = SchemaResolver(
            RulePackConfig(),
            es_url="https://es",
            index_pattern="metrics-*",
            passthrough=True,
        )
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {
            "fields": {
                "service.instance.id": {"keyword": {"type": "keyword"}},
            }
        }

        with (
            patch(
                "observability_migration.adapters.source.grafana.schema.requests.get",
                return_value=response,
            ),
            patch.object(resolver, "_build_discovered_mappings") as build_mappings,
        ):
            summary = resolver.field_resolution_summary()

        build_mappings.assert_not_called()
        self.assertEqual(summary["field_profile"], "passthrough")
        self.assertFalse(summary["automatic_mapping"])
        self.assertEqual(summary["label_mappings"], 0)

    def test_passthrough_fallback_warning_does_not_claim_otel_mapping(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_field_discovery_warning(
                {
                    "status": "offline",
                    "field_profile": "passthrough",
                    "index_pattern": "metrics-*",
                    "otel_fallback": True,
                }
            )

        message = output.getvalue()
        self.assertIn("strict passthrough", message.lower())
        self.assertNotIn("queries fall back to OTel field defaults", message)

    def test_live_passthrough_flags_missing_bare_fields(self):
        # Live caps exist but only under OTel aliases. Emitting bare ``instance``
        # must mark the run as unverified so the empty-panel warning fires.
        resolver = _passthrough_resolver(
            field_cache={"service.instance.id": {"keyword": {"type": "keyword"}}}
        )
        self.assertEqual(resolver.resolve_label("instance"), "instance")
        summary = resolver.field_resolution_summary()
        self.assertTrue(summary["otel_fallback"])
        self.assertEqual(summary["field_profile"], "passthrough")
        self.assertIsNone(summary["schema_profile"])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_field_discovery_warning(summary)
        message = output.getvalue()
        self.assertIn("strict passthrough", message.lower())
        self.assertIn("raw source fields are missing", message.lower())
        self.assertNotIn("was not recognized", message)

    def test_live_passthrough_clears_fallback_when_bare_fields_exist(self):
        resolver = _passthrough_resolver(
            field_cache={"instance": {"keyword": {"type": "keyword"}}}
        )
        self.assertEqual(resolver.resolve_label("instance"), "instance")
        summary = resolver.field_resolution_summary()
        self.assertFalse(summary["otel_fallback"])
        self.assertFalse(summary["automatic_mapping"])

    def test_cli_discovery_status_identifies_passthrough_mode(self):
        resolver = _passthrough_resolver(
            field_cache={"service.instance.id": {"keyword": {"type": "keyword"}}}
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            grafana_cli._print_schema_discovery_status(
                resolver,
                field_profile="passthrough",
            )

        message = output.getvalue()
        self.assertIn("field_profile=passthrough", message)
        self.assertIn("automatic mapping disabled", message)
        self.assertNotIn("label mappings", message)

    def test_prime_cooccurrence_is_noop_and_probes_nothing(self):
        resolver = _passthrough_resolver(field_cache={"instance": {}})
        with patch.object(resolver, "_cooccurring_candidates") as probe:
            resolver.prime_label_cooccurrence(["instance"], "node_up")
            probe.assert_not_called()


class TestGrafanaFieldProfileValidation(unittest.TestCase):
    def test_parse_args_accepts_passthrough(self):
        args = grafana_cli.parse_args(["--field-profile", "passthrough"])
        self.assertEqual(args.field_profile, "passthrough")

    def test_passthrough_is_accepted(self):
        args = SimpleNamespace(field_profile="passthrough")
        # Must not raise.
        grafana_cli._validate_field_profile(args)

    def test_otel_is_accepted(self):
        args = SimpleNamespace(field_profile="otel")
        grafana_cli._validate_field_profile(args)

    def test_unsupported_profile_still_rejected(self):
        for profile in ("prometheus", "elastic_agent", "custom.yaml"):
            with self.subTest(profile=profile):
                args = SimpleNamespace(field_profile=profile)
                with self.assertRaises(SystemExit) as ctx:
                    grafana_cli._validate_field_profile(args)
                self.assertEqual(ctx.exception.code, 2)


class TestGrafanaPassthroughIntegration(unittest.TestCase):
    def test_dashboard_resolver_inherits_passthrough(self):
        args = SimpleNamespace(
            field_profile="passthrough",
            es_url="https://es",
            esql_index="metrics-*",
            data_view="metrics-ui-*",
            es_api_key="key",
        )
        rule_pack = RulePackConfig()

        with patch.object(grafana_cli, "SchemaResolver") as resolver_class:
            grafana_cli._build_dashboard_schema_resolver(
                args,
                rule_pack,
                verify="/tmp/test-ca.pem",
            )

        resolver_class.assert_called_once_with(
            rule_pack,
            es_url="https://es",
            index_pattern="metrics-*",
            es_api_key="key",
            verify="/tmp/test-ca.pem",
            field_profile="passthrough",
        )

    def test_alert_resolver_inherits_passthrough(self):
        args = SimpleNamespace(
            field_profile="passthrough",
            es_url="https://es",
            esql_index="metrics-*",
            data_view="metrics-*",
            es_api_key="key",
        )
        rule_pack = RulePackConfig()

        with (
            patch.object(
                grafana_cli,
                "_load_configured_rule_pack",
                return_value=rule_pack,
            ),
            patch.object(grafana_cli, "_apply_native_promql_to_rule_pack"),
            patch.object(grafana_cli, "_resolve_tls_from_args", return_value=False),
        ):
            resolver = alert_pipeline._build_alert_schema_resolver(args)

        self.assertIsNotNone(resolver)
        self.assertTrue(resolver._passthrough)
        self.assertFalse(resolver._verify)

    def test_alternate_index_resolver_inherits_passthrough_and_tls(self):
        rule_pack = RulePackConfig()
        parent = SchemaResolver(
            rule_pack,
            es_url="https://es",
            index_pattern="metrics-primary-*",
            es_api_key="key",
            verify="/tmp/test-ca.pem",
            passthrough=True,
        )

        alternate = panels._resolver_for_index(
            parent,
            rule_pack,
            "metrics-alternate-*",
        )

        self.assertIsNot(alternate, parent)
        self.assertTrue(alternate._passthrough)
        self.assertEqual(alternate._verify, "/tmp/test-ca.pem")

    def test_promql_translation_emits_bare_metric_and_labels(self):
        rule_pack = RulePackConfig()
        resolver = _passthrough_resolver(
            rule_pack=rule_pack,
            field_cache={
                "metrics.redis_up": {"double": {"type": "double"}},
                "service.instance.id": {"keyword": {"type": "keyword"}},
                "service.name": {"keyword": {"type": "keyword"}},
            },
        )

        result = translate_promql_to_esql(
            'sum(redis_up{instance=~"redis.*"}) by (job)',
            esql_index="metrics-*",
            panel_type="timeseries",
            rule_pack=rule_pack,
            resolver=resolver,
        )

        self.assertIn("redis_up", result.esql_query)
        self.assertIn("instance RLIKE", result.esql_query)
        self.assertIn(", job", result.esql_query)
        self.assertNotIn("metrics.redis_up", result.esql_query)
        self.assertNotIn("service.instance.id", result.esql_query)
        self.assertNotIn("service.name", result.esql_query)

    def test_native_promql_label_rewrite_falls_back_to_esql(self):
        rule_pack = RulePackConfig(
            native_promql=True,
            label_rewrites={"instance": "host_name"},
        )
        resolver = _passthrough_resolver(
            rule_pack=rule_pack,
            field_cache={
                "http_requests_total": {
                    "double": {"type": "double", "time_series_metric": "counter"}
                },
                "host_name": {"keyword": {"type": "keyword"}},
            },
        )
        panel = {
            "type": "timeseries",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {
                    "refId": "A",
                    "expr": 'rate(http_requests_total{instance="node-1"}[5m])',
                }
            ],
        }

        _yaml_panel, result = panels.translate_panel(
            panel,
            rule_pack=rule_pack,
            resolver=resolver,
        )

        self.assertNotIn("PROMQL ", result.esql_query)
        self.assertIn("host_name", result.esql_query)
        self.assertNotIn("instance ==", result.esql_query)

    def test_native_promql_ignored_label_falls_back_to_esql(self):
        rule_pack = RulePackConfig(
            native_promql=True,
            ignored_labels=["metrics_path"],
        )
        resolver = _passthrough_resolver(
            rule_pack=rule_pack,
            field_cache={
                "http_requests_total": {
                    "double": {"type": "double", "time_series_metric": "counter"}
                },
            },
        )
        panel = {
            "type": "timeseries",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {
                    "refId": "A",
                    "expr": 'rate(http_requests_total{metrics_path="/metrics"}[5m])',
                }
            ],
        }

        _yaml_panel, result = panels.translate_panel(
            panel,
            rule_pack=rule_pack,
            resolver=resolver,
        )

        self.assertNotIn("PROMQL ", result.esql_query)
        self.assertNotIn("metrics_path", result.esql_query)

    def test_alert_label_rewrite_falls_back_to_esql(self):
        rule_pack = RulePackConfig(label_rewrites={"mode": "cpu_mode"})
        resolver = _passthrough_resolver(
            rule_pack=rule_pack,
            field_cache={
                "node_cpu_seconds_total": {
                    "double": {"type": "double", "time_series_metric": "counter"}
                },
                "cpu_mode": {"keyword": {"type": "keyword"}},
            },
        )
        ir = build_alerting_ir_from_grafana_unified(
            _prometheus_alert_rule(
                'rate(node_cpu_seconds_total{mode="idle"}[5m])'
            ),
            datasource_map={
                "prometheus": {
                    "type": "prometheus",
                    "name": "Prometheus",
                }
            },
        )

        params = build_es_query_rule_params(
            ir,
            data_view="metrics-*",
            resolver=resolver,
        )
        query = params["esqlQuery"]["esql"]

        self.assertNotIn("PROMQL ", query)
        self.assertIn("cpu_mode", query)
        self.assertNotRegex(query, r"\| WHERE mode ==")
        self.assertIn(
            "| WHERE node_cpu_seconds_total > 80.0",
            query,
        )

    def test_alerts_only_discovers_and_reports_passthrough_fields(self):
        resolver = SchemaResolver(
            RulePackConfig(),
            es_url="https://es",
            index_pattern="metrics-*",
            passthrough=True,
        )
        response = unittest.mock.Mock(status_code=200)
        response.json.return_value = {
            "fields": {
                "service.instance.id": {
                    "keyword": {"type": "keyword"},
                }
            }
        }
        args = SimpleNamespace(
            es_url="https://es",
            data_view="metrics-*",
            preflight=False,
            create_alert_rules=False,
        )

        def map_batch(_alerts, *, data_view, resolver):
            self.assertEqual(data_view, "metrics-*")
            self.assertTrue(resolver._discovery_attempted)
            self.assertEqual(resolver.resolve_label("instance"), "instance")
            return {
                "results": [],
                "summary": {"by_automation_tier": {}},
            }

        with (
            tempfile.TemporaryDirectory() as output_dir,
            patch(
                "observability_migration.adapters.source.grafana.schema.requests.get",
                return_value=response,
            ),
            patch.object(
                alert_pipeline,
                "load_unified_alerting_resources",
                return_value={},
            ),
            patch.object(
                alert_pipeline,
                "_build_alert_schema_resolver",
                return_value=resolver,
            ),
            patch.object(alert_pipeline, "map_alerts_batch", side_effect=map_batch),
            patch.object(alert_pipeline, "write_alert_artifacts"),
            patch.object(alert_pipeline, "create_rules_if_requested"),
        ):
            summary = alert_pipeline.run_alert_pipeline(
                args,
                output_dir=Path(output_dir),
            )

        self.assertEqual(
            summary["field_discovery"]["field_profile"],
            "passthrough",
        )
        self.assertEqual(summary["field_discovery"]["status"], "ok")
        self.assertTrue(summary["field_discovery"]["otel_fallback"])

    def test_count_comparison_uses_bare_instance_dimension(self):
        # count(<metric> <cmp> <value>) collapses to one row per series using an
        # instance dimension. Under passthrough that dimension must stay the
        # source label ``instance``, not the OTel default ``service.instance.id``.
        rule_pack = RulePackConfig()
        rule_pack.assume_tsds_gauges = False
        resolver = _passthrough_resolver(
            rule_pack=rule_pack,
            field_cache={
                "up": {"double": {"type": "double"}},
                "service.instance.id": {"keyword": {"type": "keyword"}},
            },
        )

        result = translate_promql_to_esql(
            "count(up == 0)",
            esql_index="metrics-*",
            panel_type="stat",
            rule_pack=rule_pack,
            resolver=resolver,
        )

        self.assertIn(
            "| STATS series_present = COUNT(*) BY instance\n",
            result.esql_query,
        )
        self.assertNotIn("service.instance.id", result.esql_query)

    def test_count_comparison_grouped_keeps_bare_instance_dimension(self):
        rule_pack = RulePackConfig()
        rule_pack.assume_tsds_gauges = False
        resolver = _passthrough_resolver(
            rule_pack=rule_pack,
            field_cache={
                "up": {"double": {"type": "double"}},
                "job": {"keyword": {"type": "keyword"}},
                "service.instance.id": {"keyword": {"type": "keyword"}},
                "service.name": {"keyword": {"type": "keyword"}},
            },
        )

        result = translate_promql_to_esql(
            "count(up == 0) by (job)",
            esql_index="metrics-*",
            panel_type="stat",
            rule_pack=rule_pack,
            resolver=resolver,
        )

        self.assertRegex(
            result.esql_query,
            r"\| STATS series_present = COUNT\(\*\) BY .*job.*instance",
        )
        self.assertNotIn("service.instance.id", result.esql_query)
        self.assertNotIn("service.name", result.esql_query)


if __name__ == "__main__":
    unittest.main()
