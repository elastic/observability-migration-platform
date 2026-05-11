# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import yaml

from observability_migration.adapters.source.grafana import (
    assistant,
    esql_validate,
    links,
    manifest,
    panels,
    polish,
    promql,
    rules,
    schema,
    translate,
    verification,
)
from observability_migration.core.reporting import report as report
from observability_migration.targets.kibana import compile as compile_module

migrate = SimpleNamespace(
    RulePackConfig=rules.RulePackConfig,
    SchemaResolver=schema.SchemaResolver,
    translate_promql_to_esql=translate.translate_promql_to_esql,
    translate_panel=panels.translate_panel,
    preprocess_grafana_macros=promql.preprocess_grafana_macros,
    _parse_fragment=promql._parse_fragment,
    build_rule_catalog=rules.build_rule_catalog,
    analyze_validation_error=esql_validate.analyze_validation_error,
    build_suggested_rule_pack=esql_validate.build_suggested_rule_pack,
    validate_query_with_fixes=esql_validate.validate_query_with_fixes,
    MigrationResult=report.MigrationResult,
    PanelResult=report.PanelResult,
    sync_result_queries_to_yaml=compile_module.sync_result_queries_to_yaml,
    mark_panel_requires_manual_after_validation=report.mark_panel_requires_manual_after_validation,
    mark_panel_requires_manual_after_failed_validation=report.mark_panel_requires_manual_after_failed_validation,
    translate_variables=panels.translate_variables,
    _infer_controls_data_view=panels._infer_controls_data_view,
    _infer_dashboard_filters=panels._infer_dashboard_filters,
    _safe_alias=promql._safe_alias,
    translate_dashboard=panels.translate_dashboard,
    annotate_results_with_verification=verification.annotate_results_with_verification,
    save_migration_manifest=manifest.save_migration_manifest,
    apply_metadata_polish=polish.apply_metadata_polish,
    apply_review_explanations=assistant.apply_review_explanations,
    build_runtime_summary=report.build_runtime_summary,
)


class TranslatorRegressionTests(unittest.TestCase):
    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def translate(self, expr, panel_type="graph", translation_hints=None):
        return migrate.translate_promql_to_esql(
            expr,
            esql_index="metrics-*",
            panel_type=panel_type,
            rule_pack=self.rule_pack,
            resolver=self.resolver,
            translation_hints=translation_hints,
        )

    def translate_panel(self, panel):
        return migrate.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    def seed_field_caps(self, fields):
        self.resolver._discovery_attempted = True
        self.resolver._field_cache = fields
        self.resolver._discovered_mappings = {}

    def test_schema_resolver_otel_profile_covers_workload_labels(self):
        self.assertEqual(self.resolver.resolve_label("deployment"), "k8s.deployment.name")
        self.assertEqual(self.resolver.resolve_label("daemonset"), "k8s.daemonset.name")
        self.assertEqual(self.resolver.resolve_label("replicaset"), "k8s.replicaset.name")
        self.assertEqual(self.resolver.resolve_label("statefulset"), "k8s.statefulset.name")
        self.assertEqual(self.resolver.resolve_label("pod_name"), "k8s.pod.name")
        self.assertEqual(self.resolver.resolve_label("namespace_name"), "k8s.namespace.name")
        self.assertEqual(self.resolver.resolve_label("region"), "cloud.region")
        self.assertEqual(self.resolver.resolve_label("availability_zone"), "cloud.availability_zone")

    def test_resolve_label_prefers_source_field_when_target_has_both(self):
        """If the target has both `instance` AND `service.instance.id`, the
        resolver must keep `instance` (source-faithful) instead of rewriting."""
        self.seed_field_caps({
            "instance": {"keyword": {"aggregatable": True, "searchable": True}},
            "service.instance.id": {"keyword": {"aggregatable": True, "searchable": True}},
        })
        self.resolver._build_discovered_mappings()
        self.assertEqual(self.resolver.resolve_label("instance"), "instance")
        self.assertEqual(self.resolver.resolve_control_field("instance"), "instance")

    def test_resolve_label_falls_back_to_otel_when_source_field_absent(self):
        """If the target only has the OTEL field (no `instance`), the resolver
        still rewrites to `service.instance.id`."""
        self.seed_field_caps({
            "service.instance.id": {"keyword": {"aggregatable": True, "searchable": True}},
        })
        self.resolver._build_discovered_mappings()
        self.assertEqual(self.resolver.resolve_label("instance"), "service.instance.id")
        self.assertEqual(self.resolver.resolve_control_field("instance"), "service.instance.id")

    def test_resolve_label_keeps_source_field_when_only_source_present(self):
        """If the target only has `instance`, the resolver keeps `instance`."""
        self.seed_field_caps({
            "instance": {"keyword": {"aggregatable": True, "searchable": True}},
        })
        self.resolver._build_discovered_mappings()
        self.assertEqual(self.resolver.resolve_label("instance"), "instance")
        self.assertEqual(self.resolver.resolve_control_field("instance"), "instance")

    def test_resolve_label_offline_still_returns_otel_candidate(self):
        """When no field cache exists (offline / no es_url), keep the existing
        OTEL-candidate fallback behavior so the offline migration path is
        unchanged."""
        # No seed_field_caps() call → _field_cache stays None.
        # resolve_label() will call _discover_fields() which sets it to {} when
        # there is no es_url. Either way, empty cache means we should fall
        # through to PROM_TO_OTEL_CANDIDATES.
        self.assertEqual(self.resolver.resolve_label("instance"), "service.instance.id")
        self.assertEqual(self.resolver.resolve_label("namespace"), "k8s.namespace.name")
        self.assertEqual(self.resolver.resolve_label("node"), "k8s.node.name")

    def test_resolve_label_user_override_still_wins(self):
        """A user-provided label_rewrites entry trumps everything else."""
        custom_pack = migrate.RulePackConfig(label_rewrites={"instance": "host.name"})
        custom_resolver = migrate.SchemaResolver(custom_pack)
        custom_resolver._discovery_attempted = True
        custom_resolver._field_cache = {
            "instance": {"keyword": {"aggregatable": True, "searchable": True}},
            "service.instance.id": {"keyword": {"aggregatable": True, "searchable": True}},
            "host.name": {"keyword": {"aggregatable": True, "searchable": True}},
        }
        custom_resolver._build_discovered_mappings()
        self.assertEqual(custom_resolver.resolve_label("instance"), "host.name")

    def test_translator_emits_source_faithful_field_in_where_clause(self):
        """End-to-end: a PromQL with `instance="value"` against a target that
        has the `instance` field should produce ESQL using `instance`, not
        `service.instance.id`."""
        self.seed_field_caps({
            "http_requests_total": {"long": {"aggregatable": True, "time_series_metric": "counter"}},
            "instance": {"keyword": {"aggregatable": True, "searchable": True}},
            "service.instance.id": {"keyword": {"aggregatable": True, "searchable": True}},
        })
        self.resolver._build_discovered_mappings()
        translated = self.translate('sum(rate(http_requests_total{instance="prom-1:9100"}[5m])) by (instance)')
        self.assertIn('instance == "prom-1:9100"', translated.esql_query)
        self.assertNotIn("service.instance.id", translated.esql_query)

    def test_resolver_for_index_propagates_es_api_key(self):
        """Alternate-index resolvers (used for controls and logs) must inherit
        the parent resolver's API key so they can run `_field_caps` and pick
        up source-faithful fields. Without the key, the alternate resolver
        operated blind and silently fell back to OTEL-only mappings — the
        exact root cause of elastic/mig-to-kbn#21 (the control bound to
        `instance` ended up pointing at `service.instance.id` while the
        panel WHERE clause correctly used `instance`)."""
        parent = migrate.SchemaResolver(
            self.rule_pack,
            es_url="https://example-cluster.test",
            index_pattern="metrics-prometheus-synthetic",
            es_api_key="apikey-from-parent",
        )
        alt = panels._resolver_for_index(parent, self.rule_pack, "metrics-*")
        self.assertIsNot(alt, parent)
        self.assertEqual(alt._es_url, "https://example-cluster.test")
        self.assertEqual(alt._es_api_key, "apikey-from-parent")
        self.assertEqual(alt._index_pattern, "metrics-*")

    def test_resolver_detects_prometheus_remote_write_profile(self):
        """Profile detection: presence of `prometheus.labels.*` + at least one
        `prometheus.<metric>.counter|value` field triggers the
        `prometheus_remote_write` profile."""
        self.seed_field_caps({
            "prometheus.labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.labels.job": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.http_requests_total.counter": {"long": {"aggregatable": True, "time_series_metric": "counter"}},
            "prometheus.http_requests_total.rate": {"double": {"aggregatable": True, "time_series_metric": "gauge"}},
        })
        self.assertEqual(self.resolver.schema_profile(), "prometheus_remote_write")

    def test_resolver_does_not_detect_profile_when_only_otel_fields(self):
        self.seed_field_caps({
            "service.instance.id": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "http_requests_total": {"long": {"aggregatable": True, "time_series_metric": "counter"}},
        })
        self.assertIsNone(self.resolver.schema_profile())

    def test_resolve_label_namespaces_to_prometheus_labels_when_profile_active(self):
        self.seed_field_caps({
            "prometheus.labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.http_requests_total.counter": {"long": {"aggregatable": True, "time_series_metric": "counter"}},
        })
        self.assertEqual(self.resolver.resolve_label("instance"), "prometheus.labels.instance")
        self.assertEqual(self.resolver.resolve_control_field("instance"), "prometheus.labels.instance")

    def test_resolve_metric_field_picks_counter_suffix_for_counter_metric(self):
        self.seed_field_caps({
            "prometheus.labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.http_requests_total.counter": {"long": {"aggregatable": True, "time_series_metric": "counter"}},
            "prometheus.http_requests_total.rate": {"double": {"aggregatable": True, "time_series_metric": "gauge"}},
        })
        self.assertEqual(
            self.resolver.resolve_metric_field("http_requests_total", prefer="counter"),
            "prometheus.http_requests_total.counter",
        )

    def test_resolve_metric_field_picks_value_suffix_for_gauge_metric(self):
        self.seed_field_caps({
            "prometheus.labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.http_requests_total.counter": {"long": {"aggregatable": True, "time_series_metric": "counter"}},
            "prometheus.process_resident_memory_bytes.value": {"long": {"aggregatable": True, "time_series_metric": "gauge"}},
        })
        self.assertEqual(
            self.resolver.resolve_metric_field("process_resident_memory_bytes", prefer="gauge"),
            "prometheus.process_resident_memory_bytes.value",
        )

    def test_resolve_metric_field_passthrough_when_profile_not_active(self):
        # Plain target with no prometheus.* nesting.
        self.seed_field_caps({
            "http_requests_total": {"long": {"aggregatable": True, "time_series_metric": "counter"}},
            "instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
        })
        self.assertEqual(
            self.resolver.resolve_metric_field("http_requests_total", prefer="counter"),
            "http_requests_total",
        )

    def test_resolve_metric_field_fallback_honors_prefer(self):
        """When the profile is detected (one leaf field exists) but the
        requested metric has no leaf at all, the fallback name must reflect
        the caller's `prefer` so the missing-field signal points at the
        right physical leaf."""
        self.seed_field_caps({
            "prometheus.labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.up.value": {"long": {"aggregatable": True, "time_series_metric": "gauge"}},
        })
        self.assertEqual(
            self.resolver.resolve_metric_field("absent_metric", prefer="counter"),
            "prometheus.absent_metric.counter",
        )
        self.assertEqual(
            self.resolver.resolve_metric_field("absent_metric", prefer="gauge"),
            "prometheus.absent_metric.value",
        )
        self.assertEqual(
            self.resolver.resolve_metric_field("absent_metric", prefer="rate"),
            "prometheus.absent_metric.rate",
        )

    def test_translator_emits_namespaced_fields_against_prometheus_remote_write_profile(self):
        """End-to-end: a PromQL counter rate against a remote_write profile
        target must produce ESQL referencing `prometheus.labels.instance` and
        `prometheus.http_requests_total.counter` in the right places."""
        self.seed_field_caps({
            "prometheus.labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.labels.method": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.labels.path": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.labels.status": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.http_requests_total.counter": {"long": {"aggregatable": True, "time_series_metric": "counter"}},
            "prometheus.http_requests_total.rate": {"double": {"aggregatable": True, "time_series_metric": "gauge"}},
        })
        translated = self.translate(
            'sum(rate(http_requests_total{instance="i-1"}[5m])) by (instance, method)'
        )
        self.assertIn('prometheus.labels.instance == "i-1"', translated.esql_query)
        # The aggregation argument is the namespaced counter field.
        self.assertIn("prometheus.http_requests_total.counter", translated.esql_query)
        # BY uses the namespaced label dimensions.
        self.assertIn("prometheus.labels.instance", translated.esql_query)
        self.assertIn("prometheus.labels.method", translated.esql_query)
        # The aliased output column keeps the bare metric name so legends and
        # breakdowns still match downstream.
        self.assertIn("http_requests_total =", translated.esql_query)

    def test_translator_emits_bare_fields_against_top_level_layout(self):
        """When the target has a top-level layout (no prometheus.* nesting),
        the translator must NOT prepend `prometheus.` (regression guard)."""
        self.seed_field_caps({
            "instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "http_requests_total": {"long": {"aggregatable": True, "time_series_metric": "counter"}},
        })
        translated = self.translate(
            'sum(rate(http_requests_total{instance="i-1"}[5m])) by (instance)'
        )
        self.assertIn('instance == "i-1"', translated.esql_query)
        self.assertIn("http_requests_total", translated.esql_query)
        self.assertNotIn("prometheus.labels.instance", translated.esql_query)
        self.assertNotIn("prometheus.http_requests_total", translated.esql_query)

    def test_dynamic_interval_variable_is_normalized(self):
        clean = migrate.preprocess_grafana_macros(
            "sum(increase(foo_total[$aggregation_interval])) by (instance)",
            self.rule_pack,
        )
        self.assertIn("[5m]", clean)

    def test_joined_uptime_is_parsed_generically(self):
        expr = (
            'time() - (alertmanager_build_info{instance=~"$instance"} '
            '* on (instance, cluster) group_left '
            'process_start_time_seconds{instance=~"$instance"})'
        )
        frag = migrate._parse_fragment(migrate.preprocess_grafana_macros(expr, self.rule_pack))
        self.assertEqual(frag.family, "uptime")
        translated = self.translate(expr)
        self.assertIn("process_start_time_seconds_uptime_seconds", translated.esql_query)

    def test_binary_percent_formula_uses_both_metrics(self):
        translated = self.translate("(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100")
        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("node_memory_MemAvailable_bytes", translated.esql_query)
        self.assertIn("node_memory_MemTotal_bytes", translated.esql_query)
        self.assertIn("| EVAL computed_value =", translated.esql_query)

    def test_mixed_known_and_unknown_gauge_arithmetic_keeps_from_fallback(self):
        self.seed_field_caps(
            {
                "known_gauge": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                }
            }
        )

        translated = self.translate("known_gauge + unknown_gauge")

        self.assertNotEqual(translated.feasibility, "not_feasible")
        self.assertIn("FROM metrics-*", translated.esql_query)
        self.assertIn("AVG(known_gauge)", translated.esql_query)
        self.assertIn("AVG(unknown_gauge)", translated.esql_query)

    def test_post_aggregation_filter_is_preserved(self):
        expr = 'sum(increase(net_conntrack_dialer_conn_failed_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0'
        translated = self.translate(expr)
        self.assertIn("| WHERE net_conntrack_dialer_conn_failed_total > 0", translated.esql_query)

    def test_inner_comparison_filter_is_applied_before_simple_aggregation(self):
        translated = self.translate("sum(foo == 1)", panel_type="stat")
        where_idx = translated.esql_query.index("| WHERE foo == 1")
        stats_idx = translated.esql_query.index("| STATS foo_sum = SUM(foo)")
        self.assertLess(where_idx, stats_idx)
        self.assertNotIn("| WHERE foo_sum == 1", translated.esql_query)

    def test_count_comparison_counts_matching_series(self):
        translated = self.translate("count(up == 1)", panel_type="stat")
        where_idx = translated.esql_query.index("| WHERE up == 1")
        stats_idx = translated.esql_query.index("| STATS up_count = COUNT(*)")
        self.assertLess(where_idx, stats_idx)
        self.assertNotIn("| WHERE up_count == 1", translated.esql_query)

    def test_multi_target_post_filters_are_applied_per_series(self):
        panel = {
            "id": 99,
            "type": "graph",
            "title": "Errors",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": 'sum(increase(foo_errors_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0', "refId": "A"},
                {"expr": 'sum(increase(bar_errors_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0', "refId": "B"},
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.status, "migrated_with_warnings")
        query = yaml_panel["esql"]["query"]
        self.assertIn("foo_errors_total", query)
        self.assertIn("bar_errors_total", query)
        self.assertIn("CASE(", query)
        self.assertNotIn("| WHERE foo_errors_total >", query)
        self.assertNotIn("| WHERE bar_errors_total >", query)
        self.assertEqual(
            result.query_ir["source_expression"],
            'sum(increase(foo_errors_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| '
            'sum(increase(bar_errors_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0',
        )

    def test_histogram_quantile_is_marked_not_feasible(self):
        translated = self.translate('histogram_quantile(0.9, rate(alertmanager_notification_latency_seconds_bucket[5m]))')
        self.assertEqual(translated.feasibility, "not_feasible")

    def test_supported_range_agg_parser_backend(self):
        expr = 'sum(rate(http_requests_total{job="api"}[5m])) by (instance)'
        frag = migrate._parse_fragment(migrate.preprocess_grafana_macros(expr, self.rule_pack))
        self.assertEqual(frag.family, "range_agg")
        self.assertIn(frag.extra.get("parser_backend"), ("ast", "regex"))
        translated = self.translate(expr)
        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("http_requests_total", translated.esql_query)

    def test_otel_dotted_promql_labels_parse_and_translate(self):
        expr = (
            'sum by (service.name) (rate(http_requests_total{'
            'http.response.status_code=~"5..",http.request.method="POST"}[5m]))'
        )

        frag = migrate._parse_fragment(migrate.preprocess_grafana_macros(expr, self.rule_pack))
        self.assertEqual(frag.family, "range_agg")
        self.assertEqual(frag.group_labels, ["service.name"])
        self.assertEqual(
            [matcher["label"] for matcher in frag.matchers],
            ["http.response.status_code", "http.request.method"],
        )

        translated = self.translate(expr)

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("http_requests_total", translated.esql_query)
        self.assertIn('http.response.status_code RLIKE "5.."', translated.esql_query)
        self.assertIn('http.request.method == "POST"', translated.esql_query)
        self.assertIn("service.name", translated.esql_query)

    def test_otel_label_sanitizer_does_not_collide_with_existing_labels(self):
        expr = (
            'sum by (__obs_migration_label_0) (rate(http_requests_total{'
            'service.name="checkout",__obs_migration_label_0="real"}[5m]))'
        )

        frag = migrate._parse_fragment(migrate.preprocess_grafana_macros(expr, self.rule_pack))

        self.assertEqual(frag.group_labels, ["__obs_migration_label_0"])
        self.assertEqual(
            [matcher["label"] for matcher in frag.matchers],
            ["service.name", "__obs_migration_label_0"],
        )

    def test_otel_label_sanitizer_preserves_quoted_matcher_values(self):
        expr = (
            'sum(rate(http_requests_total{service.name="checkout",'
            'job=~"foo by (http.response.status_code)"}[5m]))'
        )

        frag = migrate._parse_fragment(migrate.preprocess_grafana_macros(expr, self.rule_pack))

        self.assertEqual(
            [(matcher["label"], matcher["value"]) for matcher in frag.matchers],
            [
                ("service.name", "checkout"),
                ("job", "foo by (http.response.status_code)"),
            ],
        )

    def test_otel_label_sanitizer_preserves_escaped_quote_commas(self):
        expr = 'sum(rate(http_requests_total{service.name="checkout",job="a\\",b"}[5m]))'

        frag = migrate._parse_fragment(migrate.preprocess_grafana_macros(expr, self.rule_pack))

        self.assertEqual(
            [(matcher["label"], matcher["value"]) for matcher in frag.matchers],
            [
                ("service.name", "checkout"),
                ("job", 'a",b'),
            ],
        )

    def test_topk_is_marked_not_feasible(self):
        translated = self.translate("topk(5, rate(foo_total[5m]))")
        self.assertEqual(translated.feasibility, "not_feasible")

    def test_without_aggregation_is_marked_not_feasible(self):
        translated = self.translate("sum without (instance) (rate(foo_total[5m]))")
        self.assertEqual(translated.feasibility, "not_feasible")

    def test_offset_inside_binary_expression_is_marked_not_feasible(self):
        translated = self.translate("(rate(foo_total[5m] offset 1h) / rate(bar_total[5m])) * 100")
        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertIn("Contains unsupported pattern: offset", translated.warnings)

    def test_subquery_expression_is_marked_not_feasible(self):
        translated = self.translate("max_over_time(rate(foo_total[5m])[1h:])")
        self.assertEqual(translated.feasibility, "not_feasible")
        subquery_warnings = [w for w in translated.warnings if "subquery" in w.lower()]
        self.assertTrue(subquery_warnings, f"Expected subquery warning in {translated.warnings}")

    def test_metric_name_introspection_is_marked_not_feasible(self):
        translated = self.translate('topk(10, count by (__name__)({__name__=~".+"}))', panel_type="bargauge")
        self.assertEqual(translated.feasibility, "not_feasible")

    def test_live_gauge_selector_uses_direct_ts_without_avg_warning(self):
        self.seed_field_caps(
            {
                "node_systemd_units": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                }
            }
        )

        translated = self.translate("node_systemd_units")

        self.assertEqual(translated.source_type, "TS")
        self.assertIn("TS metrics-*", translated.esql_query)
        self.assertIn(
            "| STATS node_systemd_units = node_systemd_units BY time_bucket = TBUCKET(5 minute)",
            translated.esql_query,
        )
        self.assertNotIn("AVG(node_systemd_units)", translated.esql_query)
        self.assertFalse(any("No explicit aggregation" in warning for warning in translated.warnings))

    def test_unknown_gauge_selector_keeps_existing_avg_fallback(self):
        translated = self.translate("node_systemd_units")

        self.assertEqual(translated.source_type, "FROM")
        self.assertIn("FROM metrics-*", translated.esql_query)
        self.assertIn("AVG(node_systemd_units)", translated.esql_query)
        self.assertTrue(any("No explicit aggregation" in warning for warning in translated.warnings))

    def test_native_promql_panel_records_promql_contract(self):
        self.rule_pack.native_promql = True
        panel = {
            "id": 901,
            "type": "graph",
            "title": "Req rate",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "rate(http_requests_total[5m])", "refId": "A"}],
        }

        _yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.target_query_contract["canonical_target"], "promql")
        self.assertEqual(result.contract_evaluation["status"], "exact_now")
        self.assertEqual(result.query_language, "promql")

    def test_multi_target_native_promql_panel_records_contract_artifacts(self):
        self.rule_pack.native_promql = True
        panel = {
            "id": 904,
            "type": "graph",
            "title": "Req rates",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": "sum without (instance) (rate(http_requests_total[5m]))", "refId": "A", "legendFormat": "a"},
                {"expr": "sum without (instance) (rate(http_requests_total[5m]))", "refId": "B", "legendFormat": "b"},
            ],
        }

        _yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.query_ir["family"], "native_promql")
        self.assertEqual(result.target_query_contract["canonical_target"], "promql")
        self.assertEqual(result.contract_evaluation["status"], "exact_now")
        self.assertIn("status", result.fulfillment_plan)

    def test_multi_target_native_promql_mixed_metrics_contract_includes_all_fields(self):
        self.rule_pack.native_promql = True
        panel = {
            "id": 908,
            "type": "graph",
            "title": "Mixed rates",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": "sum without (instance) (rate(cpu_total[5m]))", "refId": "A", "legendFormat": "cpu"},
                {"expr": "sum without (instance) (rate(memory_total[5m]))", "refId": "B", "legendFormat": "memory"},
            ],
        }

        _yaml_panel, result = self.translate_panel(panel)

        field_names = [item["name"] for item in result.target_query_contract.get("field_requirements", [])]
        self.assertEqual(result.query_ir["family"], "native_promql")
        self.assertIn("cpu_total", field_names)
        self.assertIn("memory_total", field_names)

    def test_mixed_tsds_pattern_reports_exact_after_fulfillment(self):
        self.seed_field_caps(
            {
                "node_systemd_units": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                }
            }
        )
        panel = {
            "id": 902,
            "type": "graph",
            "title": "Gauge panel",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "node_systemd_units", "refId": "A"}],
        }

        self.resolver._index_pattern = "metrics-*"
        self.resolver._concrete_index_cache = ["metrics-tsds", "metrics-plain"]

        _yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.target_query_contract["canonical_target"], "ts")
        self.assertEqual(result.contract_evaluation["status"], "exact_after_fulfillment")
        self.assertEqual(result.fulfillment_plan["actions"][0]["kind"], "narrow_index_pattern")

    def test_multiple_concrete_targets_do_not_claim_exact_now_for_ts_contract(self):
        self.seed_field_caps(
            {
                "node_systemd_units": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                }
            }
        )
        panel = {
            "id": 905,
            "type": "graph",
            "title": "Gauge panel",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "node_systemd_units", "refId": "A"}],
        }
        self.resolver._index_pattern = "metrics-*"
        self.resolver._concrete_index_cache = ["metrics-app", "metrics-host"]

        _yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.target_query_contract["canonical_target"], "ts")
        self.assertNotEqual(result.contract_evaluation["status"], "exact_now")

    def test_issue8_tsds_query_prefers_ts_contract_over_from_fallback(self):
        self.seed_field_caps(
            {
                "http_requests_total": {
                    "long": {
                        "type": "long",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "counter",
                    }
                }
            }
        )
        self.resolver._index_pattern = "metrics-*"
        self.resolver._concrete_index_cache = ["metrics-tsds"]
        panel = {
            "id": 909,
            "type": "graph",
            "title": "Request rate",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "sum(rate(http_requests_total[5m]))", "refId": "A"}],
        }

        _yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.target_query_contract["canonical_target"], "ts")
        self.assertEqual(result.contract_evaluation["status"], "exact_now")
        self.assertEqual(result.fulfillment_plan["status"], "not_required")
        self.assertEqual(result.fulfillment_plan["actions"], [])

    def test_issue12_and_13_keep_exact_targets_or_fulfillment_states(self):
        self.seed_field_caps(
            {
                "http_requests_total": {
                    "long": {
                        "type": "long",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "counter",
                    }
                },
                "node_systemd_units": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                },
            }
        )
        self.resolver._index_pattern = "metrics-*"
        self.resolver._concrete_index_cache = ["metrics-tsds"]

        rate_panel = {
            "id": 910,
            "type": "graph",
            "title": "Request rate",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "rate(http_requests_total[5m])", "refId": "A"}],
        }
        gauge_panel = {
            "id": 911,
            "type": "graph",
            "title": "Systemd units",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "node_systemd_units", "refId": "A"}],
        }

        _rate_yaml_panel, rate_result = self.translate_panel(rate_panel)
        _gauge_yaml_panel, gauge_result = self.translate_panel(gauge_panel)

        self.assertEqual(rate_result.target_query_contract["canonical_target"], "ts")
        self.assertEqual(rate_result.contract_evaluation["status"], "exact_now")
        self.assertEqual(rate_result.fulfillment_plan["status"], "not_required")
        self.assertEqual(rate_result.fulfillment_plan["actions"], [])

        self.assertEqual(gauge_result.target_query_contract["canonical_target"], "ts")
        self.assertEqual(gauge_result.contract_evaluation["status"], "exact_now")
        self.assertEqual(gauge_result.fulfillment_plan["status"], "not_required")
        self.assertEqual(gauge_result.fulfillment_plan["actions"], [])

    def test_blocked_evaluation_is_not_overridden_for_from_panel(self):
        from observability_migration.core.assets.target_query_contract import (
            ContractEvaluation,
            FulfillmentPlan,
            TargetQueryContract,
        )

        panel = {
            "id": 912,
            "type": "graph",
            "title": "Blocked panel",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "node_systemd_units", "refId": "A"}],
        }

        blocked_contract = TargetQueryContract(
            canonical_target="promql",
            exactness_class="exact_if_contract_met",
            runtime_requirements={"source_command": "PROMQL"},
            degradation_policy={"fallback": "forbidden"},
        )
        blocked_evaluation = ContractEvaluation(
            status="blocked",
            blocking=["PROMQL runtime is unavailable"],
        )
        blocked_fulfillment = FulfillmentPlan(status="not_required")

        with mock.patch.object(
            translate,
            "_build_metric_contract_artifacts",
            return_value=(blocked_contract, blocked_evaluation, blocked_fulfillment),
        ), mock.patch.object(
            panels,
            "_build_metric_contract_artifacts",
            return_value=(blocked_contract, blocked_evaluation, blocked_fulfillment),
        ):
            _yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.query_ir["source_type"], "FROM")
        self.assertEqual(result.target_query_contract["canonical_target"], "promql")
        self.assertEqual(result.contract_evaluation["status"], "blocked")
        self.assertEqual(result.fulfillment_plan["status"], "not_required")

    def test_enrich_panel_result_preserves_translation_artifacts_when_rebuild_returns_empty(self):
        self.seed_field_caps(
            {
                "cpu_usage": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                },
                "memory_usage": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                },
            }
        )
        panel = {
            "id": 950,
            "type": "graph",
            "title": "CPU and memory",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": 'cpu_usage{state="active"}', "refId": "A", "legendFormat": "cpu_usage"},
                {"expr": 'memory_usage{state="active"}', "refId": "B", "legendFormat": "memory_usage"},
            ],
        }
        self.resolver._index_pattern = "metrics-*"
        self.resolver._concrete_index_cache = ["metrics-tsds"]

        original_build = panels._build_metric_contract_artifacts
        call_log = []

        def fake_build(query_ir, **kwargs):
            call_log.append(query_ir)
            return {}, {}, {}

        panels._build_metric_contract_artifacts = fake_build
        try:
            _yaml_panel, result = self.translate_panel(panel)
        finally:
            panels._build_metric_contract_artifacts = original_build

        self.assertTrue(call_log, "expected panels._build_metric_contract_artifacts to be invoked")
        self.assertEqual(result.target_query_contract.get("canonical_target"), "ts")

    def test_build_metric_contract_artifacts_prefers_multi_series_metric_fields(self):
        from observability_migration.adapters.source.grafana.translate import (
            _build_metric_contract_artifacts,
        )
        from observability_migration.core.assets.query import QueryIR

        query_ir = QueryIR(
            source_language="promql",
            metric="",
            target_index="metrics-*",
            metadata={
                "multi_series_metric_fields": ["cpu_usage", "memory_usage"],
            },
        )

        contract, _evaluation, _fulfillment = _build_metric_contract_artifacts(
            query_ir,
            resolver=None,
            rule_pack=self.rule_pack,
        )

        contract_dict = contract.to_dict() if hasattr(contract, "to_dict") else contract
        field_names = [item["name"] for item in contract_dict["field_requirements"]]
        self.assertEqual(field_names, ["cpu_usage", "memory_usage"])

    def test_build_metric_contract_artifacts_skips_derived_alias_metric_names(self):
        """When the translator rewrote the panel to a synthetic alias like
        `computed_value`, the contract should describe the real source metric
        names from the source expression, not the alias.
        """
        from observability_migration.adapters.source.grafana.translate import (
            _build_metric_contract_artifacts,
        )
        from observability_migration.core.assets.query import QueryIR

        query_ir = QueryIR(
            source_language="promql",
            source_expression=(
                "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100"
            ),
            clean_expression=(
                "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100"
            ),
            metric="computed_value",
            target_index="metrics-*",
            panel_type="timeseries",
        )

        contract, _evaluation, _fulfillment = _build_metric_contract_artifacts(
            query_ir,
            resolver=None,
            rule_pack=self.rule_pack,
        )

        contract_dict = contract.to_dict() if hasattr(contract, "to_dict") else contract
        field_names = sorted(item["name"] for item in contract_dict["field_requirements"])
        self.assertEqual(
            field_names,
            ["node_memory_MemAvailable_bytes", "node_memory_MemTotal_bytes"],
        )
        # And the synthetic alias must never appear as a field requirement.
        self.assertNotIn("computed_value", field_names)

    def test_build_metric_contract_artifacts_skips_derived_alias_for_constant_value(self):
        """Scalar PromQL like `scalar(...)` rewrites to `constant_value`; the
        contract should still describe the underlying metric, not the alias.
        """
        from observability_migration.adapters.source.grafana.translate import (
            _build_metric_contract_artifacts,
        )
        from observability_migration.core.assets.query import QueryIR

        query_ir = QueryIR(
            source_language="promql",
            source_expression="scalar(node_boot_time_seconds)",
            clean_expression="scalar(node_boot_time_seconds)",
            metric="constant_value",
            target_index="metrics-*",
            panel_type="stat",
        )

        contract, _evaluation, _fulfillment = _build_metric_contract_artifacts(
            query_ir,
            resolver=None,
            rule_pack=self.rule_pack,
        )

        contract_dict = contract.to_dict() if hasattr(contract, "to_dict") else contract
        field_names = [item["name"] for item in contract_dict["field_requirements"]]
        self.assertEqual(field_names, ["node_boot_time_seconds"])
        self.assertNotIn("constant_value", field_names)

    def test_build_metric_contract_artifacts_classifies_counter_metric_kind_per_field(self):
        """When the translator rewrote a counter-based formula to a synthetic
        alias, the contract should classify each derived source metric by its
        own name shape, not stamp the planner's gauge template onto a counter.
        """
        from observability_migration.adapters.source.grafana.translate import (
            _build_metric_contract_artifacts,
        )
        from observability_migration.core.assets.query import QueryIR

        query_ir = QueryIR(
            source_language="promql",
            source_expression='1 - avg(irate(node_cpu_seconds_total{mode="idle"}[5m]))',
            clean_expression='1 - avg(irate(node_cpu_seconds_total{mode="idle"}[5m]))',
            metric="computed_value",
            range_function="",
            target_index="metrics-*",
            panel_type="timeseries",
        )

        contract, _evaluation, _fulfillment = _build_metric_contract_artifacts(
            query_ir,
            resolver=None,
            rule_pack=self.rule_pack,
        )

        contract_dict = contract.to_dict() if hasattr(contract, "to_dict") else contract
        reqs = {item["name"]: item for item in contract_dict["field_requirements"]}
        self.assertIn("node_cpu_seconds_total", reqs)
        self.assertEqual(reqs["node_cpu_seconds_total"]["metric_kind"], "counter")

    def test_build_metric_contract_artifacts_classifies_mixed_kinds_per_field(self):
        """A formula combining a counter and a gauge should yield two field
        requirements with distinct metric_kinds, not a single template value.
        """
        from observability_migration.adapters.source.grafana.translate import (
            _build_metric_contract_artifacts,
        )
        from observability_migration.core.assets.query import QueryIR

        query_ir = QueryIR(
            source_language="promql",
            source_expression="rate(http_requests_total[5m]) / node_memory_MemTotal_bytes",
            clean_expression="rate(http_requests_total[5m]) / node_memory_MemTotal_bytes",
            metric="computed_value",
            target_index="metrics-*",
            panel_type="timeseries",
        )

        contract, _evaluation, _fulfillment = _build_metric_contract_artifacts(
            query_ir,
            resolver=None,
            rule_pack=self.rule_pack,
        )

        contract_dict = contract.to_dict() if hasattr(contract, "to_dict") else contract
        reqs = {item["name"]: item for item in contract_dict["field_requirements"]}
        self.assertEqual(reqs["http_requests_total"]["metric_kind"], "counter")
        self.assertEqual(reqs["node_memory_MemTotal_bytes"]["metric_kind"], "gauge")

    def test_native_promql_without_query_preserves_group_mode(self):
        self.rule_pack.native_promql = True
        panel = {
            "id": 906,
            "type": "graph",
            "title": "Req rate",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "sum without (instance) (rate(http_requests_total[5m]))", "refId": "A"}],
        }

        _yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.query_ir["family"], "native_promql")
        self.assertEqual(result.query_ir["group_mode"], "without")

    def test_not_feasible_metric_panel_still_carries_contract_artifacts(self):
        panel = {
            "id": 903,
            "type": "graph",
            "title": "Unsupported metric",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": 'max_over_time(rate(foo_total[5m])[1h:])', "refId": "A"}],
        }

        _yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.status, "not_feasible")
        self.assertTrue(result.target_query_contract)
        self.assertTrue(result.contract_evaluation)
        self.assertTrue(result.fulfillment_plan)
        self.assertIn("canonical_target", result.target_query_contract)

    def test_conflicting_gauge_capability_keeps_avg_fallback(self):
        self.seed_field_caps(
            {
                "ambiguous_gauge": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    },
                    "keyword": {
                        "type": "keyword",
                        "searchable": True,
                        "aggregatable": True,
                    },
                }
            }
        )

        translated = self.translate("ambiguous_gauge")

        self.assertEqual(translated.source_type, "FROM")
        self.assertIn("AVG(ambiguous_gauge)", translated.esql_query)
        self.assertTrue(any("No explicit aggregation" in warning for warning in translated.warnings))

    def test_same_metric_collapse_rebuilds_valid_query(self):
        self.seed_field_caps(
            {
                "node_systemd_units": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                }
            }
        )
        panel = {
            "id": 100,
            "type": "graph",
            "title": "Systemd Units",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": 'node_systemd_units{instance="$node",job="$job",state="active"}', "refId": "A"},
                {"expr": 'node_systemd_units{instance="$node",job="$job",state="failed"}', "refId": "B"},
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]
        self.assertIn("AVG(node_systemd_units)", query)
        self.assertIn("| WHERE node_systemd_units IS NOT NULL", query)
        self.assertIn("BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), state", query)
        self.assertNotIn("=  BY state", query)
        self.assertTrue(any("Collapsed 2 same-metric targets into BY state" in r for r in result.reasons))
        self.assertTrue(any("No explicit aggregation" in r for r in result.reasons))
        self.assertFalse(any("only 1 could be migrated" in r for r in result.reasons))
        self.assertEqual(result.query_ir["source_type"], "FROM")
        self.assertEqual(result.target_query_contract["canonical_target"], "ts")
        self.assertEqual(result.contract_evaluation["status"], "degraded_if_forced")
        self.assertEqual(result.fulfillment_plan["status"], "not_required")
        self.assertEqual(result.fulfillment_plan["actions"], [])
        self.assertIn("FROM metrics-*", result.query_ir["target_query"])
        self.assertEqual(
            result.query_ir["source_expression"],
            'node_systemd_units{instance="$node",job="$job",state="active"} ||| '
            'node_systemd_units{instance="$node",job="$job",state="failed"}',
        )

    def test_multi_target_live_gauge_with_divergent_filters_keeps_all_series(self):
        self.seed_field_caps(
            {
                "node_systemd_units": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                }
            }
        )
        panel = {
            "id": 104,
            "type": "graph",
            "title": "Systemd Units",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": 'node_systemd_units{state="active"}', "refId": "A", "legendFormat": "active"},
                {"expr": 'node_systemd_units{state="failed"}', "refId": "B", "legendFormat": "failed"},
            ],
        }

        yaml_panel, result = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]
        metric_fields = [metric["field"] for metric in yaml_panel["esql"].get("metrics", [])]

        self.assertNotIn("only 1 could be migrated", " ".join(result.reasons))
        self.assertEqual(len(metric_fields), 2)
        self.assertIn("active", metric_fields)
        self.assertIn("failed", metric_fields)
        self.assertIn('CASE((state == "active"', query)
        self.assertIn('CASE((state == "failed"', query)
        self.assertEqual(result.query_ir["source_type"], "FROM")
        self.assertEqual(result.target_query_contract["canonical_target"], "ts")
        self.assertEqual(result.contract_evaluation["status"], "degraded_if_forced")
        self.assertEqual(result.fulfillment_plan["status"], "not_required")
        self.assertEqual(result.fulfillment_plan["actions"], [])
        self.assertIn("FROM metrics-*", result.query_ir["target_query"])

    def test_fused_multi_metric_panel_contract_includes_all_metrics(self):
        self.seed_field_caps(
            {
                "cpu_usage": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                },
                "memory_usage": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                },
            }
        )
        panel = {
            "id": 907,
            "type": "graph",
            "title": "CPU and memory",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": 'cpu_usage{state="active"}', "refId": "A", "legendFormat": "cpu_usage"},
                {"expr": 'memory_usage{state="active"}', "refId": "B", "legendFormat": "memory_usage"},
            ],
        }

        _yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(
            set(result.query_ir["metadata"].get("multi_series_metric_fields", [])),
            {"cpu_usage", "memory_usage"},
        )
        field_names = [item["name"] for item in result.target_query_contract.get("field_requirements", [])]
        self.assertIn("cpu_usage", field_names)
        self.assertIn("memory_usage", field_names)

    def test_timeseries_legend_placeholder_drives_grouping(self):
        panel = {
            "id": 101,
            "type": "graph",
            "title": "Node Exporter Scrape",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {
                    "expr": 'node_scrape_collector_success{instance="$node",job="$job"}',
                    "refId": "A",
                    "legendFormat": "{{collector}} - Scrape success",
                },
                {
                    "expr": 'node_textfile_scrape_error{instance="$node",job="$job"}',
                    "refId": "B",
                    "legendFormat": "{{collector}} - Scrape textfile error (1 = true)",
                },
            ],
        }
        yaml_panel, _ = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]
        self.assertIn(
            "| WHERE node_scrape_collector_success IS NOT NULL OR node_textfile_scrape_error IS NOT NULL",
            query,
        )
        self.assertIn(", collector", query)
        self.assertEqual(yaml_panel["esql"].get("breakdown", {}).get("field"), "collector")

    def test_group_left_join_prefers_legend_label_grouping(self):
        panel = {
            "id": 102,
            "type": "graph",
            "title": "Hardware temperature monitor",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {
                    "expr": 'node_hwmon_temp_celsius{instance="$node",job="$job"} * on(chip) group_left(chip_name) node_hwmon_chip_names{instance="$node",job="$job"}',
                    "refId": "A",
                    "legendFormat": "{{chip_name}} {{sensor}} temp",
                }
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]
        self.assertIn("| WHERE node_hwmon_temp_celsius IS NOT NULL", query)
        self.assertIn(", chip_name", query)
        self.assertEqual(yaml_panel["esql"].get("breakdown", {}).get("field"), "chip_name")
        self.assertTrue(any("Dropped group_left label enrichment" in reason for reason in result.reasons))

    def test_supported_range_functions_stay_bare_with_time_bucket_only(self):
        cases = [
            ("rate(http_requests_total[5m])", "RATE(http_requests_total, 5m)"),
            ("irate(http_requests_total[5m])", "IRATE(http_requests_total, 5m)"),
            ("increase(http_requests_total[5m])", "INCREASE(http_requests_total, 5m)"),
            ("avg_over_time(node_memory_MemFree_bytes[5m])", "AVG_OVER_TIME(node_memory_MemFree_bytes, 5m)"),
            ("sum_over_time(node_memory_MemFree_bytes[5m])", "SUM_OVER_TIME(node_memory_MemFree_bytes, 5m)"),
            ("max_over_time(node_memory_MemFree_bytes[5m])", "MAX_OVER_TIME(node_memory_MemFree_bytes, 5m)"),
            ("min_over_time(node_memory_MemFree_bytes[5m])", "MIN_OVER_TIME(node_memory_MemFree_bytes, 5m)"),
            ("count_over_time(node_memory_MemFree_bytes[5m])", "COUNT_OVER_TIME(node_memory_MemFree_bytes, 5m)"),
            ("delta(node_memory_MemFree_bytes[5m])", "DELTA(node_memory_MemFree_bytes, 5m)"),
            ("deriv(node_memory_MemFree_bytes[5m])", "DERIV(node_memory_MemFree_bytes, 5m)"),
        ]

        for expr, expected_expr in cases:
            with self.subTest(expr=expr):
                translated = self.translate(expr)
                self.assertEqual(translated.source_type, "TS")
                self.assertIn(f"| STATS {translated.output_metric_field} = {expected_expr}", translated.esql_query)
                self.assertIn("BY time_bucket = TBUCKET(5 minute)", translated.esql_query)
                self.assertNotIn(f"AVG({expected_expr})", translated.esql_query)

    def test_grouped_range_agg_wraps_ts_function(self):
        panel = {
            "id": 103,
            "type": "graph",
            "title": "Network Traffic",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {
                    "expr": 'irate(node_network_receive_bytes_total{instance="$node",job="$job"}[5m])',
                    "refId": "A",
                    "legendFormat": "{{device}} receive",
                }
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]
        self.assertIn("AVG(IRATE(node_network_receive_bytes_total, 5m))", query)
        self.assertIn(", device", query)
        self.assertTrue(
            any(
                "requires an outer aggregation when grouping TS functions by label fields" in reason
                for reason in result.reasons
            )
        )

    def test_query_ir_semantic_losses_include_accuracy_warning(self):
        ctx = SimpleNamespace(
            query_language="promql",
            promql_expr="a + on(namespace) b",
            clean_expr="a + on(namespace) b",
            panel_type="graph",
            datasource_type="prometheus",
            datasource_uid="prom",
            datasource_name="prom",
            warnings=["Cross-metric + on(namespace) join cannot be accurately represented in ES|QL"],
            output_group_fields=["time_bucket"],
            source_type="FROM",
            index="metrics-*",
            esql_query="",
            output_metric_field="",
            metadata={},
            fragment=SimpleNamespace(
                family="join",
                metric="a",
                range_func="",
                outer_agg="",
                group_mode="by",
                binary_op="+",
                group_labels=[],
            ),
        )
        query_ir = panels.build_query_ir(ctx)
        self.assertEqual(
            query_ir.semantic_losses,
            ["Cross-metric + on(namespace) join cannot be accurately represented in ES|QL"],
        )

    def test_infer_output_shape_treats_step_as_time_series(self):
        self.assertEqual(
            panels.infer_output_shape("graph", ["step", "instance"], "promql"),
            "time_series",
        )

    def test_logql_stream_selector_still_translates(self):
        translated = self.translate('{service_name="api"} |~ "error"', panel_type="logs")
        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("FROM logs-*", translated.esql_query)
        self.assertIn("message", translated.esql_query)

    def test_logql_stream_selector_uses_available_logs_message_field(self):
        resolver = migrate.SchemaResolver(self.rule_pack)
        resolver.field_exists = lambda field: field in {"body.text", "@timestamp", "service.name"}
        translated = migrate.translate_promql_to_esql(
            '{service_name="api"} |~ "error"',
            esql_index="logs-*",
            panel_type="logs",
            rule_pack=self.rule_pack,
            resolver=resolver,
        )
        self.assertIn("body.text", translated.esql_query)
        self.assertNotIn("message LIKE", translated.esql_query)

    def test_logql_count_over_time_still_translates(self):
        translated = self.translate('sum(count_over_time({service_name="api"}[5m]))', panel_type="timeseries")
        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("log_count = COUNT(*)", translated.esql_query)
        self.assertIn("time_bucket", translated.esql_query)

    def test_scalar_wrapper_and_nested_count_feed_arithmetic(self):
        expr = (
            'scalar(node_load1{instance="$node",job="$job"}) * 100 '
            '/ count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))'
        )
        translated = self.translate(expr, panel_type="stat")
        self.assertIn("COUNT_DISTINCT(cpu)", translated.esql_query)
        self.assertIn("| EVAL computed_value =", translated.esql_query)

    def test_scalar_wrapped_nested_count_denominator_merges_with_rate_numerator(self):
        expr = (
            'sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[$__rate_interval])) '
            '/ scalar(count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu)))'
        )
        translated = self.translate(expr, panel_type="stat")
        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn('| WHERE mode == "system"', translated.esql_query)
        self.assertIn("BY time_bucket = TBUCKET(5 minute), cpu", translated.esql_query)
        self.assertIn("denominator = COUNT(*) BY time_bucket", translated.esql_query)
        self.assertIn("| EVAL computed_value =", translated.esql_query)
        self.assertIn("| KEEP time_bucket, computed_value", translated.esql_query)

    def test_complex_binary_expr_inside_agg_translates(self):
        expr = (
            'sum(increase(prometheus_tsdb_compaction_duration_sum{instance="$instance"}[30m]) '
            '/ increase(prometheus_tsdb_compaction_duration_count{instance="$instance"}[30m])) by (instance)'
        )
        translated = self.translate(expr, panel_type="graph")
        self.assertEqual(translated.feasibility, "feasible")
        self.assertTrue(translated.metric_name, "Should have a metric name")
        self.assertIn("INCREASE", translated.esql_query)

    def test_rule_catalog_exposes_binary_expr_rule(self):
        catalog = migrate.build_rule_catalog(self.rule_pack)
        names = [entry["name"] for entry in catalog["metadata"]["registries"]["query_translators"]]
        self.assertIn("binary_expr_family", names)

    def test_validation_error_analysis_separates_label_and_metric(self):
        query = (
            "TS metrics-*\n"
            "| STATS foo_total = SUM(INCREASE(foo_total, 5m)) BY time_bucket = TBUCKET(5 minute), status\n"
            "| SORT time_bucket ASC"
        )
        error = (
            "Found 2 problems\n"
            "line 2:67: Unknown column [status], did you mean any of [state, tags]?\n"
            "line 2:38: Unknown column [foo_total]"
        )
        analysis = migrate.analyze_validation_error(query, error, resolver=None)
        roles = {entry["name"]: entry["role"] for entry in analysis["unknown_columns"]}
        self.assertEqual(roles["status"], "label")
        self.assertEqual(roles["foo_total"], "metric")
        suggestions = {entry["name"]: entry["suggested_fields"] for entry in analysis["unknown_columns"]}
        self.assertEqual(suggestions["status"], ["state", "tags"])

    def test_generated_rule_pack_filters_empty_candidates(self):
        generated = migrate.build_suggested_rule_pack({
            "suggested_label_candidates": {
                "status": ["state"],
                "quantile": [],
            },
            "missing_indexes": {"logs-*": 2},
            "missing_labels": {"status": 1, "quantile": 3},
            "missing_metrics": {"foo_total": 4},
        })
        self.assertEqual(generated["schema"]["label_candidates"], {"status": ["state"]})
        self.assertEqual(generated["_validation_hints"]["unresolved_labels"], {"quantile": 3})

    def test_validate_query_with_fixes_can_narrow_wildcard_index(self):
        class StubResolver:
            _index_pattern = "metrics-*"

            def concrete_index_candidates(self):
                return ["metrics-prometheus-default", "metrics-prometheus-synthetic"]

        query = (
            "TS metrics-*\n"
            "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
            "| STATS x = IRATE(foo_total, 5m) BY time_bucket = TBUCKET(5 minute)"
        )

        def fake_run(candidate_query, _es_url, **kwargs):
            if "metrics-prometheus-default" in candidate_query:
                return {"ok": False, "error": "counter mismatch", "rows": 0, "columns": []}
            if "metrics-prometheus-synthetic" in candidate_query:
                return {"ok": True, "error": "", "rows": 12, "columns": ["x"]}
            return {"ok": False, "error": "counter mismatch", "rows": 0, "columns": []}

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run):
            result = migrate.validate_query_with_fixes(query, "http://localhost:9200", StubResolver())

        self.assertEqual(result["status"], "fixed")
        self.assertIn("metrics-prometheus-synthetic", result["query"])
        self.assertIn("NOW() - 1 hour", result["analysis"]["materialized_query"])
        self.assertEqual(result["analysis"]["sample_window"]["time_from"], esql_validate.DEFAULT_TSTART_EXPR)

    def test_validate_query_with_fixes_marks_empty_narrowed_query(self):
        class StubResolver:
            _index_pattern = "metrics-*"

            def concrete_index_candidates(self):
                return ["metrics-prometheus-default", "metrics-prometheus-synthetic"]

        query = (
            "TS metrics-*\n"
            "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
            "| STATS x = IRATE(foo_total, 5m) BY time_bucket = TBUCKET(5 minute)"
        )

        def fake_run(candidate_query, _es_url, **kwargs):
            if "metrics-prometheus-default" in candidate_query:
                return {"ok": False, "error": "counter mismatch", "rows": 0, "columns": []}
            if "metrics-prometheus-synthetic" in candidate_query:
                return {"ok": True, "error": "", "rows": 0, "columns": []}
            return {"ok": False, "error": "counter mismatch", "rows": 0, "columns": []}

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run):
            result = migrate.validate_query_with_fixes(query, "http://localhost:9200", StubResolver())

        self.assertEqual(result["status"], "fixed_empty")
        self.assertEqual(result["analysis"]["result_rows"], 0)
        self.assertEqual(result["analysis"]["narrowed_to_index"], "metrics-prometheus-synthetic")

    def test_validate_query_with_fixes_rewrites_known_exporter_failed_metric_name(self):
        class StubResolver:
            def resolve_label(self, label):
                return label

            def field_exists(self, field_name):
                return field_name == "otelcol_exporter_send_failed_spans"

            def _candidate_fields(self, _label):
                return []

        query = (
            "TS metrics-*\n"
            "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
            "| STATS failed = SUM(RATE(otelcol_exporter_enqueue_failed_spans, 5m)) BY time_bucket = TBUCKET(5 minute)"
        )

        def fake_run(candidate_query, _es_url, **kwargs):
            if "otelcol_exporter_send_failed_spans" in candidate_query:
                return {"ok": True, "error": "", "rows": 12, "columns": ["failed"]}
            return {
                "ok": False,
                "error": "Found 1 problem\nline 3:20: Unknown column [otelcol_exporter_enqueue_failed_spans]",
                "rows": 0,
                "columns": [],
            }

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run):
            result = migrate.validate_query_with_fixes(query, "http://localhost:9200", StubResolver())

        self.assertEqual(result["status"], "fixed")
        self.assertIn("otelcol_exporter_send_failed_spans", result["query"])

    def test_validate_query_with_fixes_reuses_http_session_across_attempts(self):
        class StubResolver:
            def resolve_label(self, label):
                return {"missing_field": "fixed_field"}.get(label, label)

            def field_exists(self, field_name):
                return field_name == "fixed_field"

            def _candidate_fields(self, _label):
                return []

        query = "FROM metrics-*\n| STATS value = SUM(missing_field)"

        with mock.patch.object(esql_validate.requests, "Session") as mock_session_cls:
            session = mock.Mock()
            mock_session_cls.return_value = session

            first = mock.Mock()
            first.status_code = 400
            first.headers = {"content-type": "application/json"}
            first.json.return_value = {"error": {"reason": "Unknown column [missing_field]"}}
            second = mock.Mock()
            second.status_code = 200
            second.headers = {"content-type": "application/json"}
            second.json.return_value = {"columns": [{"name": "value"}], "values": [[1]]}
            session.post.side_effect = [first, second]

            result = migrate.validate_query_with_fixes(query, "http://localhost:9200", StubResolver())

        self.assertEqual(result["status"], "fixed")
        self.assertEqual(mock_session_cls.call_count, 1)
        self.assertEqual(session.post.call_count, 2)

    def test_validate_query_with_fixes_rewrites_known_node_interrupt_metric_name(self):
        class StubResolver:
            def resolve_label(self, label):
                return label

            def field_exists(self, field_name):
                return field_name == "node_intr_total"

            def _candidate_fields(self, _label):
                return []

        query = (
            "TS metrics-*\n"
            "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
            "| STATS node_interrupts_total = IRATE(node_interrupts_total, 5m) BY time_bucket = TBUCKET(5 minute)\n"
            "| SORT time_bucket ASC"
        )

        def fake_run(candidate_query, _es_url, **kwargs):
            if "node_intr_total" in candidate_query:
                return {"ok": True, "error": "", "rows": 9, "columns": ["node_interrupts_total", "time_bucket"]}
            return {
                "ok": False,
                "error": "Found 1 problem\nline 3:39: Unknown column [node_interrupts_total]",
                "rows": 0,
                "columns": [],
            }

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run):
            result = migrate.validate_query_with_fixes(query, "http://localhost:9200", StubResolver())

        self.assertEqual(result["status"], "fixed")
        self.assertIn("IRATE(node_intr_total, 5m)", result["query"])

    def test_validate_query_with_fixes_adjusts_tbucket_to_match_short_window(self):
        class StubResolver:
            def resolve_label(self, label):
                return label

            def field_exists(self, _field_name):
                return True

            def _candidate_fields(self, _label):
                return []

        query = (
            "TS metrics-*\n"
            "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
            "| STATS value = SUM(IRATE(node_cpu_guest_seconds_total, 1m)) BY time_bucket = TBUCKET(5 minute)"
        )

        def fake_run(candidate_query, _es_url, **kwargs):
            if "TBUCKET(1 minute)" in candidate_query:
                return {"ok": True, "error": "", "rows": 8, "columns": ["value"]}
            return {
                "ok": False,
                "error": (
                    "Unsupported window [1m] for aggregate function [IRATE(node_cpu_guest_seconds_total, 1m)]; "
                    "the window must be larger than the time bucket [TBUCKET(5 minute)] and an exact multiple of it"
                ),
                "rows": 0,
                "columns": [],
            }

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run):
            result = migrate.validate_query_with_fixes(query, "http://localhost:9200", StubResolver())

        self.assertEqual(result["status"], "fixed")
        self.assertIn("TBUCKET(1 minute)", result["query"])

    def test_run_esql_query_materializes_dashboard_time_params_for_validation(self):
        captured = {}

        def fake_post(url, json, params, headers, timeout):
            captured["url"] = url
            captured["query"] = json["query"]
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"values": [], "columns": []},
                headers={"content-type": "application/json"},
            )

        query = (
            "FROM metrics-*\n"
            "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
            "| STATS value = COUNT(*) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)"
        )

        with mock.patch.object(esql_validate.requests, "post", side_effect=fake_post):
            probe = esql_validate._run_esql_query(query, "http://localhost:9200")

        self.assertTrue(probe["ok"])
        self.assertNotIn("?_tstart", captured["query"])
        self.assertNotIn("?_tend", captured["query"])
        self.assertIn("NOW() - 1 hour", captured["query"])
        self.assertIn("NOW()", captured["query"])

    def test_sync_result_queries_to_yaml_persists_validation_fixes(self):
        result = migrate.MigrationResult("Dashboard", "uid")
        panel = migrate.PanelResult("Panel", "graph", "line", "migrated", 0.85)
        panel.esql_query = "FROM metrics-prometheus-synthetic\n| LIMIT 10"
        result.panel_results = [panel]
        result.yaml_panel_results = [panel]

        payload = {
            "dashboards": [{
                "name": "Dashboard",
                "panels": [
                    {
                        "title": "Panel",
                        "esql": {
                            "type": "datatable",
                            "query": "FROM metrics-*\n| LIMIT 10",
                        },
                    }
                ],
            }]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "dashboard.yaml"
            path.write_text(yaml.dump(payload, sort_keys=False))
            updated = migrate.sync_result_queries_to_yaml(result, path)
            rewritten = yaml.safe_load(path.read_text())

        self.assertTrue(updated)
        self.assertEqual(
            rewritten["dashboards"][0]["panels"][0]["esql"]["query"],
            "FROM metrics-prometheus-synthetic\n| LIMIT 10",
        )
        self.assertEqual(
            panel.visual_ir.presentation.config["query"],
            "FROM metrics-prometheus-synthetic\n| LIMIT 10",
        )

    def test_sync_result_queries_to_yaml_updates_metric_field_after_query_alias_change(self):
        result = migrate.MigrationResult("Dashboard", "uid")
        panel = migrate.PanelResult("Interrupts Detail", "graph", "line", "migrated", 0.85)
        panel.esql_query = (
            "TS metrics-*\n"
            "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
            "| STATS node_intr_total = IRATE(node_intr_total, 5m) BY time_bucket = TBUCKET(5 minute)\n"
            "| SORT time_bucket ASC"
        )
        result.panel_results = [panel]
        result.yaml_panel_results = [panel]

        payload = {
            "dashboards": [{
                "name": "Dashboard",
                "panels": [
                    {
                        "title": "Interrupts Detail",
                        "esql": {
                            "type": "line",
                            "query": (
                                "TS metrics-*\n"
                                "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
                                "| STATS node_interrupts_total = IRATE(node_interrupts_total, 5m) BY time_bucket = TBUCKET(5 minute)\n"
                                "| SORT time_bucket ASC"
                            ),
                            "dimension": {"field": "time_bucket", "data_type": "date"},
                            "metrics": [{"field": "node_interrupts_total"}],
                        },
                    }
                ],
            }]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "dashboard.yaml"
            path.write_text(yaml.dump(payload, sort_keys=False))
            updated = migrate.sync_result_queries_to_yaml(result, path)
            rewritten = yaml.safe_load(path.read_text())

        self.assertTrue(updated)
        panel_payload = rewritten["dashboards"][0]["panels"][0]["esql"]
        self.assertEqual(panel_payload["metrics"][0]["field"], "node_intr_total")
        self.assertEqual(panel_payload["dimension"]["field"], "time_bucket")

    def test_sync_result_queries_to_yaml_uses_emitted_panel_results(self):
        result = migrate.MigrationResult("Dashboard", "uid")
        skipped = migrate.PanelResult("Skipped", "graph", "line", "requires_manual", 0.3)
        emitted = migrate.PanelResult("Panel", "graph", "line", "migrated", 0.85)
        emitted.esql_query = "FROM metrics-prometheus-synthetic\n| LIMIT 20"
        result.panel_results = [skipped, emitted]
        result.yaml_panel_results = [emitted]

        payload = {
            "dashboards": [{
                "name": "Dashboard",
                "panels": [
                    {
                        "title": "Panel",
                        "esql": {
                            "type": "datatable",
                            "query": "FROM metrics-*\n| LIMIT 20",
                        },
                    }
                ],
            }]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "dashboard.yaml"
            path.write_text(yaml.dump(payload, sort_keys=False))
            migrate.sync_result_queries_to_yaml(result, path)
            rewritten = yaml.safe_load(path.read_text())

        self.assertEqual(
            rewritten["dashboards"][0]["panels"][0]["esql"]["query"],
            "FROM metrics-prometheus-synthetic\n| LIMIT 20",
        )

    def test_sync_result_queries_to_yaml_rewrites_empty_fallback_to_markdown(self):
        result = migrate.MigrationResult("Dashboard", "uid")
        panel = migrate.PanelResult("Panel", "graph", "line", "migrated_with_warnings", 0.6)
        panel.promql_expr = "irate(foo_total[5m])"
        panel.esql_query = (
            "TS metrics-prometheus-synthetic\n"
            "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
            "| STATS foo = IRATE(foo_total, 5m)"
        )
        migrate.mark_panel_requires_manual_after_validation(
            panel,
            {"analysis": {"narrowed_to_index": "metrics-prometheus-synthetic"}},
        )
        result.panel_results = [panel]
        result.yaml_panel_results = [panel]

        payload = {
            "dashboards": [{
                "name": "Dashboard",
                "panels": [
                    {
                        "title": "Panel",
                        "esql": {
                            "type": "line",
                            "query": (
                                "TS metrics-*\n"
                                "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
                                "| STATS foo = IRATE(foo_total, 5m)"
                            ),
                        },
                    }
                ],
            }]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "dashboard.yaml"
            path.write_text(yaml.dump(payload, sort_keys=False))
            migrate.sync_result_queries_to_yaml(result, path)
            rewritten = yaml.safe_load(path.read_text())

        panel_payload = rewritten["dashboards"][0]["panels"][0]
        self.assertNotIn("esql", panel_payload)
        self.assertIn("markdown", panel_payload)
        self.assertIn("Manual review required", panel_payload["markdown"]["content"])
        self.assertEqual(panel.visual_ir.presentation.kind, "markdown")
        self.assertIn("Manual review required", panel.visual_ir.presentation.config["content"])

    def test_sync_result_queries_to_yaml_rewrites_failed_validation_to_markdown(self):
        result = migrate.MigrationResult("Dashboard", "uid")
        panel = migrate.PanelResult("Panel", "graph", "line", "migrated_with_warnings", 0.6)
        panel.promql_expr = "irate(foo_total[5m])"
        panel.esql_query = (
            "TS metrics-*\n"
            "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
            "| STATS foo = IRATE(foo_total, 5m)"
        )
        migrate.mark_panel_requires_manual_after_failed_validation(
            panel,
            {"error": "Found 1 problem\nline 3:20: Unknown column [foo_total]"},
        )
        result.panel_results = [panel]
        result.yaml_panel_results = [panel]

        payload = {
            "dashboards": [{
                "name": "Dashboard",
                "panels": [
                    {
                        "title": "Panel",
                        "esql": {
                            "type": "line",
                            "query": (
                                "TS metrics-*\n"
                                "| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend\n"
                                "| STATS foo = IRATE(foo_total, 5m)"
                            ),
                        },
                    }
                ],
            }]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "dashboard.yaml"
            path.write_text(yaml.dump(payload, sort_keys=False))
            migrate.sync_result_queries_to_yaml(result, path)
            rewritten = yaml.safe_load(path.read_text())

        panel_payload = rewritten["dashboards"][0]["panels"][0]
        self.assertNotIn("esql", panel_payload)
        self.assertIn("markdown", panel_payload)
        self.assertIn("failed live ES|QL validation", panel_payload["markdown"]["content"])

    def test_hidden_query_result_variable_is_not_translated_to_control(self):
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "total",
                "label": "total_servers",
                "hide": 2,
                "query": 'query_result(count(node_uname_info{job=~"$job"}))',
            }],
            datasource_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertEqual(controls, [])

    def test_query_variable_skips_missing_control_fields(self):
        resolver = migrate.SchemaResolver(self.rule_pack)
        resolver.field_exists = lambda field: field != "k8s.namespace.name"
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "namespace",
                "label": "namespace",
                "query": "label_values({job='api'}, namespace)",
            }],
            datasource_index="logs-*",
            rule_pack=self.rule_pack,
            resolver=resolver,
        )
        self.assertEqual(controls, [])

    def test_query_variable_uses_label_values_field_not_variable_name(self):
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "node",
                "label": "Instance",
                "query": 'label_values(node_uname_info{job="$job"},instance)',
            }],
            datasource_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertEqual(controls[0]["field"], "service.instance.id")

    def test_query_variable_defaults_to_single_select_control(self):
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "node",
                "label": "Instance",
                "multi": False,
                "query": 'label_values(node_uname_info{job="$job"},instance)',
            }],
            datasource_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertIn("multiple", controls[0])
        self.assertFalse(controls[0]["multiple"])

    def test_query_variable_preserves_multi_select_when_not_repeat_driven(self):
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "node",
                "label": "Instance",
                "multi": True,
                "query": 'label_values(node_uname_info{job="$job"},instance)',
            }],
            datasource_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertTrue(controls[0]["multiple"])

    def test_repeat_driver_variable_forces_single_select_control(self):
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "node",
                "label": "Instance",
                "multi": True,
                "query": 'label_values(node_uname_info{job="$job"},instance)',
            }],
            datasource_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
            repeat_variable_names={"node"},
        )
        self.assertIn("multiple", controls[0])
        self.assertFalse(controls[0]["multiple"])

    def test_controls_data_view_switches_to_logs_for_log_only_dashboard(self):
        inferred = migrate._infer_controls_data_view(
            [
                {"esql": {"query": "FROM logs-*\n| LIMIT 10"}},
                {"esql": {"query": "FROM logs-*\n| LIMIT 10"}},
            ],
            "metrics-*",
            self.rule_pack,
        )
        self.assertEqual(inferred, "logs-*")

    def test_controls_data_view_uses_narrow_index_when_all_panels_share_one(self):
        """When every metric panel targets the same narrow index pattern, the
        controls data view should follow it. Otherwise the controls resolver
        runs `_field_caps` against the broad datasource and may end up with a
        different field shape than the panels actually query — the root cause
        of empty controls on Fleet `prometheus.remote_write` data streams.
        """
        inferred = migrate._infer_controls_data_view(
            [
                {"esql": {"query": "TS metrics-prometheus.remote_write-express\n| STATS …"}},
                {"esql": {"query": "FROM metrics-prometheus.remote_write-express\n| STATS …"}},
            ],
            "metrics-*",
            self.rule_pack,
        )
        self.assertEqual(inferred, "metrics-prometheus.remote_write-express")

    def test_controls_data_view_falls_back_to_datasource_when_panels_target_mixed_indexes(self):
        inferred = migrate._infer_controls_data_view(
            [
                {"esql": {"query": "TS metrics-prometheus-synthetic\n| STATS …"}},
                {"esql": {"query": "TS metrics-otel-default\n| STATS …"}},
            ],
            "metrics-*",
            self.rule_pack,
        )
        self.assertEqual(inferred, "metrics-*")

    def test_dashboard_filters_skipped_when_all_panels_target_narrow_index(self):
        """If every panel targets a concrete (wildcard-free) data stream, the
        dashboard's index pattern is already the constraint; adding a literal
        ``data_stream.dataset == "prometheus"`` match_phrase filter strictly
        cannot help and on Fleet-managed ``prometheus.remote_write`` data
        streams it filters out every document because the actual dataset
        value is ``prometheus.remote_write``."""
        filters = migrate._infer_dashboard_filters(
            [
                {"esql": {"query": "TS metrics-prometheus.remote_write-express\n| STATS …"}},
                {"esql": {"query": "FROM metrics-prometheus.remote_write-express\n| STATS …"}},
            ],
            self.rule_pack,
        )
        self.assertEqual(filters, [])

    def test_dashboard_filters_emitted_when_panels_target_broad_pattern(self):
        """The filter is still useful as a safety net when panels query a
        wildcard pattern; that's the original design intent. Keep the
        existing behavior in that case."""
        filters = migrate._infer_dashboard_filters(
            [
                {"esql": {"query": "TS metrics-*\n| STATS …"}},
                {"esql": {"query": "FROM metrics-prometheus-*\n| STATS …"}},
            ],
            self.rule_pack,
        )
        self.assertEqual(
            filters,
            [{"field": "data_stream.dataset", "equals": "prometheus"}],
        )

    def test_dashboard_filters_skipped_when_metrics_dataset_filter_explicit_empty(self):
        rp = migrate.RulePackConfig()
        rp.metrics_dataset_filter = ""
        filters = migrate._infer_dashboard_filters(
            [
                {"esql": {"query": "TS metrics-*\n| STATS …"}},
            ],
            rp,
        )
        self.assertEqual(filters, [])

    def test_detect_promql_support_true_when_query_returns_columns(self):
        from observability_migration.adapters.source.grafana.cli import (
            _detect_promql_support,
        )
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.requests.post",
        ) as post:
            post.return_value = SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "columns": [{"name": "value"}, {"name": "step"}],
                    "values": [],
                },
                text="",
            )
            self.assertTrue(_detect_promql_support("https://es.example", "apikey"))

    def test_detect_promql_support_false_when_no_handler(self):
        from observability_migration.adapters.source.grafana.cli import (
            _detect_promql_support,
        )
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.requests.post",
        ) as post:
            post.return_value = SimpleNamespace(
                status_code=400,
                json=lambda: {"error": "no handler found for uri [/_query]"},
                text='{"error":"no handler found"}',
            )
            self.assertFalse(_detect_promql_support("https://es.example", "apikey"))

    def test_detect_promql_support_none_on_exception(self):
        from observability_migration.adapters.source.grafana.cli import (
            _detect_promql_support,
        )
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.requests.post",
            side_effect=ConnectionError("network"),
        ):
            self.assertIsNone(_detect_promql_support("https://es.example", "apikey"))

    def test_resolve_native_promql_uses_detection_when_auto(self):
        from observability_migration.adapters.source.grafana.cli import (
            _resolve_native_promql,
        )
        args = SimpleNamespace(
            native_promql_flag="auto",
            es_url="https://es.example",
            es_api_key="apikey",
        )
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_promql_support",
            return_value=True,
        ):
            self.assertTrue(_resolve_native_promql(args))
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_promql_support",
            return_value=False,
        ):
            self.assertFalse(_resolve_native_promql(args))

    def test_resolve_native_promql_force_off_overrides_detection(self):
        from observability_migration.adapters.source.grafana.cli import (
            _resolve_native_promql,
        )
        args = SimpleNamespace(
            native_promql_flag="force_off",
            es_url="https://es.example",
            es_api_key="apikey",
        )
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_promql_support",
        ) as det:
            self.assertFalse(_resolve_native_promql(args))
            det.assert_not_called()

    def test_resolve_native_promql_force_on_overrides_detection(self):
        from observability_migration.adapters.source.grafana.cli import (
            _resolve_native_promql,
        )
        args = SimpleNamespace(
            native_promql_flag="force_on",
            es_url="",
            es_api_key="",
        )
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_promql_support",
        ) as det:
            self.assertTrue(_resolve_native_promql(args))
            det.assert_not_called()

    def test_resolve_native_promql_auto_without_es_url_returns_false(self):
        from observability_migration.adapters.source.grafana.cli import (
            _resolve_native_promql,
        )
        args = SimpleNamespace(
            native_promql_flag="auto",
            es_url="",
            es_api_key="",
        )
        self.assertFalse(_resolve_native_promql(args))

    def test_print_rule_catalog_skips_promql_probe(self):
        """`--print-rule-catalog` is an offline introspection command and
        must not hit the cluster to auto-detect PROMQL support."""
        from observability_migration.adapters.source.grafana.cli import (
            _load_configured_rule_pack,
        )
        args = SimpleNamespace(
            rules_file=[],
            plugin=[],
            logs_index="",
            dataset_filter="",
            logs_dataset_filter="",
            es_url="https://es.example",
            es_api_key="apikey",
            native_promql_flag="auto",
        )
        with mock.patch("observability_migration.adapters.source.grafana.cli._detect_promql_support") as det:
            _load_configured_rule_pack(args)
            det.assert_not_called()

    def test_apply_native_promql_preserves_explicit_dataset_filter(self):
        """When the user passes both ``--native-promql`` and
        ``--dataset-filter foo``, the explicit filter wins. Native-promql
        only clears the default ``"prometheus"`` filter when no explicit
        ``--dataset-filter`` was provided. Pre-refactor behavior is
        preserved.
        """
        from observability_migration.adapters.source.grafana.cli import (
            _apply_native_promql_to_rule_pack,
            _load_configured_rule_pack,
        )
        args = SimpleNamespace(
            rules_file=[],
            plugin=[],
            logs_index="",
            dataset_filter="custom-dataset",
            logs_dataset_filter="",
            es_url="https://es.example",
            es_api_key="apikey",
            native_promql_flag="force_on",
        )
        rule_pack = _load_configured_rule_pack(args)
        self.assertEqual(rule_pack.metrics_dataset_filter, "custom-dataset")
        _apply_native_promql_to_rule_pack(rule_pack, args)
        self.assertTrue(rule_pack.native_promql)
        self.assertEqual(rule_pack.metrics_dataset_filter, "custom-dataset")

    def test_apply_native_promql_clears_default_dataset_filter(self):
        """Without an explicit ``--dataset-filter``, native PromQL clears
        the rule pack's default ``"prometheus"`` filter so the broad
        ``data_stream.dataset`` safety net doesn't fire on native-PromQL
        queries."""
        from observability_migration.adapters.source.grafana.cli import (
            _apply_native_promql_to_rule_pack,
            _load_configured_rule_pack,
        )
        args = SimpleNamespace(
            rules_file=[],
            plugin=[],
            logs_index="",
            dataset_filter="",
            logs_dataset_filter="",
            es_url="",
            es_api_key="",
            native_promql_flag="force_on",
        )
        rule_pack = _load_configured_rule_pack(args)
        self.assertEqual(rule_pack.metrics_dataset_filter, "prometheus")
        _apply_native_promql_to_rule_pack(rule_pack, args)
        self.assertEqual(rule_pack.metrics_dataset_filter, "")

    def test_safe_alias_prefixes_leading_digits(self):
        self.assertEqual(migrate._safe_alias("5m load"), "series_5m_load")
        self.assertEqual(migrate._safe_alias("1m"), "series_1m")

    def test_table_old_summary_uses_style_breakdowns_and_hides_unmapped_value_targets(self):
        panel = {
            "title": "Overview",
            "type": "table-old",
            "gridPos": {"w": 24, "h": 6, "x": 0, "y": 0},
            "styles": [
                {"pattern": "instance", "type": "string"},
                {"pattern": "Value #B", "alias": "Memory", "type": "number"},
                {"pattern": "/.*/", "type": "hidden"},
            ],
            "targets": [
                {
                    "refId": "A",
                    "expr": 'node_uname_info{job="$job"} - 0',
                    "format": "table",
                    "instant": True,
                    "legendFormat": "Hostname",
                },
                {
                    "refId": "B",
                    "expr": 'node_memory_MemTotal_bytes{job="$job"} - 0',
                    "format": "table",
                    "instant": True,
                    "legendFormat": "Memory",
                },
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["type"], "datatable")
        metric_fields = [m["field"] for m in yaml_panel["esql"]["metrics"]]
        self.assertEqual(metric_fields, ["computed_value"])
        breakdown_fields = [b["field"] for b in yaml_panel["esql"]["breakdowns"]]
        self.assertEqual(breakdown_fields, ["service.instance.id"])
        self.assertIn("LAST(computed_value, time_bucket)", yaml_panel["esql"]["query"])
        self.assertNotIn("Hostname", yaml_panel["esql"]["query"])
        self.assertIn("node_memory_MemTotal_bytes", yaml_panel["esql"]["query"])
        self.assertEqual(result.status, "migrated_with_warnings")

    def test_table_old_summary_can_merge_uptime_with_other_metrics(self):
        panel = {
            "title": "Overview",
            "type": "table-old",
            "gridPos": {"w": 24, "h": 6, "x": 0, "y": 0},
            "styles": [
                {"pattern": "instance", "type": "string"},
                {"pattern": "Value #D", "alias": "Uptime", "type": "number"},
                {"pattern": "Value #B", "alias": "Memory", "type": "number"},
                {"pattern": "/.*/", "type": "hidden"},
            ],
            "targets": [
                {
                    "refId": "D",
                    "expr": 'sum(time() - node_boot_time_seconds{job="$job"}) by (instance)',
                    "format": "table",
                    "instant": True,
                    "legendFormat": "Uptime",
                },
                {
                    "refId": "B",
                    "expr": 'node_memory_MemTotal_bytes{job="$job"} - 0',
                    "format": "table",
                    "instant": True,
                    "legendFormat": "Memory",
                },
            ],
        }
        yaml_panel, _ = self.translate_panel(panel)
        self.assertEqual(
            [m["field"] for m in yaml_panel["esql"]["metrics"]],
            ["Uptime", "Memory"],
        )
        self.assertIn('DATE_DIFF("seconds"', yaml_panel["esql"]["query"])
        self.assertIn("LAST(Uptime, time_bucket)", yaml_panel["esql"]["query"])
        self.assertIn("LAST(Memory, time_bucket)", yaml_panel["esql"]["query"])

    def test_table_old_summary_inlines_filter_specific_metrics(self):
        panel = {
            "title": "Overview",
            "type": "table-old",
            "gridPos": {"w": 24, "h": 6, "x": 0, "y": 0},
            "styles": [
                {"pattern": "instance", "type": "string"},
                {"pattern": "Value #A", "alias": "Memory", "type": "number"},
                {"pattern": "Value #B", "alias": "RootFs", "type": "number"},
                {"pattern": "/.*/", "type": "hidden"},
            ],
            "targets": [
                {
                    "refId": "A",
                    "expr": 'node_memory_MemTotal_bytes{job="$job"} - 0',
                    "format": "table",
                    "instant": True,
                },
                {
                    "refId": "B",
                    "expr": 'node_filesystem_size_bytes{job="$job",fstype=~"ext.*|xfs"} - 0',
                    "format": "table",
                    "instant": True,
                },
            ],
        }
        yaml_panel, _ = self.translate_panel(panel)
        self.assertEqual(
            [m["field"] for m in yaml_panel["esql"]["metrics"]],
            ["Memory", "RootFs"],
        )
        self.assertIn("CASE((fstype RLIKE", yaml_panel["esql"]["query"])

    def test_bargauge_summary_becomes_multi_metric_bar_chart(self):
        panel = {
            "title": "",
            "type": "bargauge",
            "gridPos": {"w": 6, "h": 4, "x": 0, "y": 0},
            "targets": [
                {
                    "refId": "A",
                    "expr": '(1 - (node_memory_MemAvailable_bytes{instance="$node"} / node_memory_MemTotal_bytes{instance="$node"})) * 100',
                    "instant": True,
                    "legendFormat": "Used RAM",
                },
                {
                    "refId": "B",
                    "expr": '(1 - ((node_memory_SwapFree_bytes{instance="$node"} + 1) / (node_memory_SwapTotal_bytes{instance="$node"} + 1))) * 100',
                    "instant": True,
                    "legendFormat": "Used SWAP",
                },
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["type"], "bar")
        self.assertEqual(
            [m["field"] for m in yaml_panel["esql"]["metrics"]],
            ["value"],
        )
        self.assertEqual(yaml_panel["esql"]["dimension"]["field"], "label")
        self.assertEqual(yaml_panel["esql"]["legend"]["visible"], "hide")
        self.assertIn("MV_ZIP", yaml_panel["esql"]["query"])
        self.assertIn("label = MV_FIRST(SPLIT(__pairs, \"~\"))", yaml_panel["esql"]["query"])
        self.assertIn("Approximated bargauge as bar chart", result.reasons)

    def test_narrow_xy_panel_defaults_legend_to_bottom(self):
        panel = {
            "title": "CPU Usage",
            "type": "graph",
            "gridPos": {"w": 8, "h": 6, "x": 0, "y": 0},
            "legend": {"show": True},
            "targets": [
                {
                    "refId": "A",
                    "expr": 'sum(rate(foo_total[5m])) by (instance)',
                }
            ],
        }
        yaml_panel, _ = self.translate_panel(panel)
        legend = yaml_panel["esql"].get("legend", {})
        self.assertEqual(legend.get("visible"), "show")
        self.assertIn(legend.get("position", "bottom"), ("bottom", "right"))

    def test_grouped_topk_barchart_becomes_sorted_limited_bar(self):
        panel = {
            "title": "Top Endpoints",
            "type": "barchart",
            "gridPos": {"w": 12, "h": 8, "x": 12, "y": 8},
            "targets": [
                {
                    "refId": "A",
                    "expr": "topk(10, sum(rate(http_requests_total[5m])) by (handler))",
                    "legendFormat": "{{handler}}",
                }
            ],
        }

        yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.status, "migrated_with_warnings")
        self.assertEqual(yaml_panel["esql"]["type"], "bar")
        self.assertEqual(yaml_panel["esql"]["dimension"]["field"], "handler")
        self.assertEqual(yaml_panel["esql"]["metrics"][0]["field"], "value")
        self.assertIn("| KEEP handler, value", yaml_panel["esql"]["query"])
        self.assertIn("| SORT value DESC", yaml_panel["esql"]["query"])
        self.assertIn("| LIMIT 10", yaml_panel["esql"]["query"])

    def test_numeric_series_alias_is_esql_safe_in_summary_table(self):
        panel = {
            "title": "Overview",
            "type": "table-old",
            "gridPos": {"w": 24, "h": 6, "x": 0, "y": 0},
            "styles": [
                {"pattern": "instance", "type": "string"},
                {"pattern": "Value #A", "alias": "Memory", "type": "number"},
                {"pattern": "Value #B", "alias": "5m load", "type": "number"},
                {"pattern": "/.*/", "type": "hidden"},
            ],
            "targets": [
                {
                    "refId": "A",
                    "expr": 'node_memory_MemTotal_bytes{job="$job"} - 0',
                    "format": "table",
                    "instant": True,
                },
                {
                    "refId": "B",
                    "expr": 'node_load5{job="$job"}',
                    "format": "table",
                    "instant": True,
                },
            ],
        }
        yaml_panel, _ = self.translate_panel(panel)
        self.assertIn("| EVAL series_5m_load =", yaml_panel["esql"]["query"])
        metric_fields = [m["field"] for m in yaml_panel["esql"]["metrics"]]
        self.assertIn("series_5m_load", metric_fields)

    def test_summary_ts_query_keeps_bucket_then_collapses_to_single_row(self):
        translated = migrate.translate_promql_to_esql(
            '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
            esql_index="metrics-*",
            panel_type="stat",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
            translation_hints={"summary_mode": True},
        )
        self.assertIn("BY time_bucket = TBUCKET(5 minute)", translated.esql_query)
        self.assertIn("| SORT time_bucket ASC", translated.esql_query)
        self.assertIn(
            "| STATS time_bucket = MAX(time_bucket), computed_value = LAST(computed_value, time_bucket)",
            translated.esql_query,
        )
        self.assertNotIn("| SORT time_bucket DESC", translated.esql_query)
        self.assertNotIn("| LIMIT 1", translated.esql_query)
        self.assertIn("| KEEP time_bucket, computed_value", translated.esql_query)
        self.assertEqual(translated.output_group_fields, [])

    def test_bargauge_rate_summary_collapses_timeseries_bucket(self):
        panel = {
            "title": "Pressure",
            "type": "bargauge",
            "gridPos": {"w": 6, "h": 4, "x": 0, "y": 0},
            "targets": [
                {
                    "refId": "A",
                    "expr": 'irate(node_pressure_cpu_waiting_seconds_total[5m])',
                    "instant": True,
                    "legendFormat": "CPU",
                },
                {
                    "refId": "B",
                    "expr": 'irate(node_pressure_io_waiting_seconds_total[5m])',
                    "instant": True,
                    "legendFormat": "I/O",
                },
            ],
        }
        yaml_panel, _ = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["type"], "bar")
        self.assertIn("Approximated bargauge as bar chart", _.reasons)
        metric_fields = [m["field"] for m in yaml_panel["esql"]["metrics"]]
        self.assertEqual(metric_fields, ["value"])
        self.assertEqual(yaml_panel["esql"]["dimension"]["field"], "label")
        self.assertEqual(yaml_panel["esql"]["legend"]["visible"], "hide")
        self.assertIn("CPU", yaml_panel["esql"]["query"])
        self.assertIn("I/O", yaml_panel["esql"]["query"])
        self.assertNotIn("breakdowns", yaml_panel["esql"])

    def test_nested_count_stat_panel_stays_scalar_metric(self):
        panel = {
            "title": "CPU Cores",
            "type": "stat",
            "gridPos": {"w": 6, "h": 4, "x": 0, "y": 0},
            "targets": [
                {
                    "refId": "A",
                    "expr": 'count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))',
                    "instant": True,
                    "range": False,
                }
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["type"], "metric")
        self.assertEqual(result.kibana_type, "metric")
        self.assertNotIn("breakdown", yaml_panel["esql"])
        self.assertNotIn("breakdowns", yaml_panel["esql"])
        self.assertIn("| WHERE node_cpu_seconds_total IS NOT NULL", yaml_panel["esql"]["query"])
        self.assertIn("| STATS node_cpu_seconds_total_count = COUNT_DISTINCT(cpu)", yaml_panel["esql"]["query"])

    def test_gauge_panel_uses_native_gauge_config(self):
        panel = {
            "title": "CPU Utilisation",
            "type": "gauge",
            "gridPos": {"w": 6, "h": 4, "x": 0, "y": 0},
            "fieldConfig": {
                "defaults": {
                    "min": 0,
                    "max": 100,
                    "thresholds": {
                        "steps": [
                            {"value": None, "color": "green"},
                            {"value": 70, "color": "orange"},
                            {"value": 90, "color": "red"},
                        ]
                    },
                }
            },
            "targets": [
                {
                    "refId": "A",
                    "expr": 'avg(node_load1{job="$job"})',
                    "instant": True,
                }
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIn(result.status, ("migrated", "migrated_with_warnings"))
        self.assertEqual(yaml_panel["esql"]["type"], "gauge")
        self.assertIn("metric", yaml_panel["esql"])
        self.assertEqual(yaml_panel["esql"]["appearance"]["shape"], "arc")
        self.assertEqual(yaml_panel["esql"]["minimum"], {"field": "_gauge_min"})
        self.assertEqual(yaml_panel["esql"]["maximum"], {"field": "_gauge_max"})
        self.assertEqual(yaml_panel["esql"]["goal"], {"field": "_gauge_goal"})
        self.assertIn("| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 70", yaml_panel["esql"]["query"])
        self.assertEqual(
            yaml_panel["esql"]["color"]["thresholds"],
            [
                {"up_to": 70, "color": "#54B399"},
                {"up_to": 90, "color": "#D6BF57"},
                {"up_to": 100, "color": "#E7664C"},
            ],
        )

    def test_bucketed_gauge_keeps_ts_bucket_for_summary_query(self):
        panel = {
            "title": "CPU Busy",
            "type": "gauge",
            "gridPos": {"w": 3, "h": 4, "x": 0, "y": 0},
            "targets": [
                {
                    "refId": "A",
                    "expr": 'avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))',
                    "instant": True,
                }
            ],
        }
        yaml_panel, _ = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]
        self.assertIn("BY time_bucket = TBUCKET(5 minute)", query)
        self.assertIn("| SORT time_bucket ASC", query)
        self.assertIn(
            "| STATS time_bucket = MAX(time_bucket), node_cpu_seconds_total = LAST(node_cpu_seconds_total, time_bucket)",
            query,
        )
        self.assertNotIn("| SORT time_bucket DESC", query)
        self.assertNotIn("| LIMIT 1", query)
        self.assertIn("| KEEP time_bucket, node_cpu_seconds_total", query)

    def test_translation_exposes_query_ir(self):
        translated = self.translate('sum(rate(foo_total{job="api"}[5m])) by (instance)')
        self.assertIsNotNone(translated.query_ir)
        self.assertEqual(translated.query_ir.source_language, "promql")
        self.assertEqual(translated.query_ir.metric, "foo_total")
        self.assertEqual(translated.query_ir.output_shape, "time_series")

    def test_query_variable_uses_range_control_for_numeric_field(self):
        self.resolver._discovery_attempted = True
        self.resolver._field_cache = {"event.duration": {"long": {}}}
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "duration",
                "label": "Duration",
                "query": "label_values(http_requests_total,event.duration)",
            }],
            datasource_index="logs-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertEqual(controls[0]["type"], "range")
        self.assertEqual(controls[0]["field"], "event.duration")

    def test_query_variable_uses_options_control_when_field_types_conflict(self):
        self.resolver._discovery_attempted = True
        self.resolver._field_cache = {
            "event.duration": {
                "long": {"searchable": True, "aggregatable": True},
                "keyword": {"searchable": True, "aggregatable": True},
            }
        }
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "duration",
                "label": "Duration",
                "query": "label_values(http_requests_total,event.duration)",
            }],
            datasource_index="logs-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertEqual(controls[0]["type"], "options")
        self.assertEqual(controls[0]["field"], "event.duration")

    def test_log_message_field_prefers_searchable_text_field(self):
        self.resolver._discovery_attempted = True
        self.resolver._field_cache = {
            "message": {"keyword": {"searchable": False, "aggregatable": True}},
            "event.original": {"keyword": {"searchable": True, "aggregatable": True}},
            "body.text": {"text": {"searchable": True, "aggregatable": False}},
        }
        field_name = translate._resolve_logs_message_field(self.rule_pack, self.resolver)
        self.assertEqual(field_name, "body.text")

    def test_mixed_datasource_panel_is_marked_not_feasible(self):
        panel = {
            "title": "Mixed",
            "type": "graph",
            "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
            "targets": [
                {
                    "refId": "A",
                    "expr": 'sum(rate(http_requests_total[5m])) by (service)',
                    "datasource": {"type": "prometheus", "uid": "prom"},
                },
                {
                    "refId": "B",
                    "expr": '{service="api"} |~ "error"',
                    "datasource": {"type": "loki", "uid": "loki"},
                },
            ],
        }
        _, result = self.translate_panel(panel)
        self.assertEqual(result.status, "not_feasible")
        self.assertEqual(result.readiness, "manual_only")
        self.assertIn("Mixed datasource", result.reasons[0])
        self.assertTrue(any("manual redesign" in note.lower() for note in result.notes))

    def test_dashboard_translation_preserves_original_panel_positions(self):
        dashboard = {
            "title": "Layout",
            "uid": "layout-1",
            "panels": [
                {
                    "id": 2,
                    "title": "Bottom Right",
                    "type": "stat",
                    "gridPos": {"w": 12, "h": 6, "x": 12, "y": 12},
                    "targets": [{"refId": "A", "expr": 'node_load1{job="node"}'}],
                },
                {
                    "id": 1,
                    "title": "Top Left",
                    "type": "graph",
                    "gridPos": {"w": 12, "h": 6, "x": 0, "y": 0},
                    "targets": [{"refId": "A", "expr": 'sum(rate(foo_total[5m])) by (instance)'}],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, yaml_path = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            payload = yaml.safe_load(yaml_path.read_text())
        panels = payload["dashboards"][0]["panels"]
        self.assertEqual(payload["dashboards"][0]["minimum_kibana_version"], "9.1.0")
        self.assertEqual(
            payload["dashboards"][0]["filters"],
            [{"field": "data_stream.dataset", "equals": "prometheus"}],
        )
        self.assertEqual(panels[0]["position"]["y"], 0)
        self.assertEqual(panels[0]["size"]["w"], 24)
        self.assertGreater(panels[1]["position"]["y"], 0, "second panel should be below first")
        self.assertEqual(result.inventory["panels"], 2)

    def test_dashboard_translation_resolves_overlapping_positions(self):
        dashboard = {
            "title": "Overlap",
            "uid": "overlap-1",
            "panels": [
                {
                    "id": 1,
                    "title": "Top",
                    "type": "graph",
                    "gridPos": {"w": 12, "h": 8, "x": 0, "y": 0},
                    "targets": [{"refId": "A", "expr": 'sum(rate(foo_total[5m])) by (instance)'}],
                },
                {
                    "id": 2,
                    "title": "Bottom",
                    "type": "graph",
                    "gridPos": {"w": 12, "h": 8, "x": 0, "y": 4},
                    "targets": [{"refId": "A", "expr": 'sum(rate(bar_total[5m])) by (instance)'}],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            _, yaml_path = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            payload = yaml.safe_load(yaml_path.read_text())
        panels = payload["dashboards"][0]["panels"]
        self.assertEqual(panels[0]["position"], {"x": 0, "y": 0})
        self.assertGreaterEqual(panels[1]["position"]["y"], 8)

    def test_metric_tile_width_is_normalized_to_minimum(self):
        dashboard = {
            "title": "Tiny Tiles",
            "uid": "tiny-tiles-1",
            "panels": [
                {
                    "id": 1,
                    "title": "Tiny",
                    "type": "stat",
                    "gridPos": {"w": 2, "h": 4, "x": 0, "y": 0},
                    "targets": [{"refId": "A", "expr": 'node_load1{job="node"}'}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            _, yaml_path = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            payload = yaml.safe_load(yaml_path.read_text())
        panel = payload["dashboards"][0]["panels"][0]
        self.assertEqual(panel["size"]["w"], 4, "narrow metric tiles are enforced to MIN_PANEL_WIDTH")

    def test_manifest_writer_includes_inventory_and_query_ir(self):
        dashboard = {
            "title": "Manifested",
            "uid": "manifest-1",
            "links": [{"title": "Docs", "url": "https://example.com"}],
            "annotations": {"list": [{"name": "Deploys"}]},
            "templating": {"list": [{"type": "query", "name": "job", "query": "label_values(foo,job)"}]},
            "panels": [
                {
                    "id": 7,
                    "title": "Requests",
                    "type": "graph",
                    "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
                    "links": [{"title": "Panel Docs", "url": "https://example.com/panel"}],
                    "targets": [{"refId": "A", "expr": 'sum(rate(http_requests_total[5m])) by (instance)'}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            migrate.annotate_results_with_verification([result], [])
            manifest_path = pathlib.Path(tmpdir) / "migration_manifest.json"
            migrate.save_migration_manifest([result], manifest_path)
            manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["summary"]["dashboards"], 1)
        self.assertEqual(manifest["dashboards"][0]["inventory"]["links"], 1)
        self.assertEqual(manifest["panels"][0]["inventory"]["links"], 1)
        self.assertEqual(manifest["panels"][0]["query_language"], "promql")
        self.assertEqual(manifest["panels"][0]["readiness"], "metrics_mapping_needed")
        self.assertEqual(manifest["panels"][0]["query_ir"]["source_language"], "promql")
        self.assertEqual(manifest["panels"][0]["visual_ir"]["layout"]["w"], 48)
        self.assertEqual(manifest["panels"][0]["operational_ir"]["lineage"]["dashboard_uid"], "manifest-1")
        self.assertEqual(manifest["panels"][0]["verification_packet"]["semantic_gate"], "Yellow")
        self.assertTrue(manifest["panels"][0]["target_candidates"])

    def test_native_esql_keep_panel_is_reused(self):
        panel = {
            "id": 10,
            "title": "Native Logs",
            "type": "table",
            "datasource": {"type": "elasticsearch", "uid": "es-main"},
            "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
            "targets": [{"refId": "A", "query": "FROM logs-* | KEEP @timestamp, message"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.status, "migrated")
        self.assertEqual(result.query_language, "esql")
        self.assertEqual(result.recommended_target, "native_esql_panel")
        self.assertEqual(yaml_panel["esql"]["query"], "FROM logs-* | KEEP @timestamp, message")
        self.assertEqual(
            [metric["field"] for metric in yaml_panel["esql"]["metrics"]],
            ["@timestamp", "message"],
        )

    def test_native_esql_raw_limit_panel_requires_manual_mapping(self):
        panel = {
            "id": 11,
            "title": "Raw Native Logs",
            "type": "table",
            "datasource": {"type": "elasticsearch", "uid": "es-main"},
            "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
            "targets": [{"refId": "A", "query": "FROM logs-* | LIMIT 10"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.status, "requires_manual")
        self.assertIn("markdown", yaml_panel)

    def test_html_text_panel_is_normalized_to_markdown_and_recommended_as_markdown(self):
        dashboard = {
            "title": "Docs",
            "uid": "docs-1",
            "panels": [
                {
                    "id": 45,
                    "title": "Documentation",
                    "type": "text",
                    "gridPos": {"w": 24, "h": 3, "x": 0, "y": 0},
                    "options": {
                        "mode": "html",
                        "content": "<a href=\"http://www.monitoringartist.com\" target=\"_blank\" title=\"Dashboard maintained by Monitoring Artist - DevOps / Docker / Kubernetes\"><img src=\"https://monitoringartist.github.io/monitoring-artist-logo-grafana.png\" height=\"30px\" /></a> | \n<a target=\"_blank\" href=\"https://github.com/open-telemetry/opentelemetry-collector/blob/main/docs/troubleshooting.md#metrics\">OTEL collector troubleshooting (how to enable telemetry metrics)</a> | \n<a target=\"_blank\" href=\"https://grafana.com/dashboards/15983\">Installed from Grafana.com dashboards</a>",
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, yaml_path = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            payload = yaml.safe_load(yaml_path.read_text())
        content = payload["dashboards"][0]["panels"][0]["markdown"]["content"]
        self.assertEqual(result.panel_results[0].kibana_type, "markdown")
        self.assertEqual(result.panel_results[0].query_language, "text")
        self.assertEqual(result.panel_results[0].recommended_target, "markdown")
        self.assertIn("[Dashboard maintained by Monitoring Artist](http://www.monitoringartist.com)", content)
        self.assertIn(
            "[OTEL collector troubleshooting (how to enable telemetry metrics)](https://github.com/open-telemetry/opentelemetry-collector/blob/main/docs/troubleshooting.md#metrics)",
            content,
        )
        self.assertIn("[Installed from Grafana.com dashboards](https://grafana.com/dashboards/15983)", content)
        self.assertNotIn("<a", content)
        self.assertNotIn("<img", content)

        migrate.annotate_results_with_verification([result], [])
        packet = result.panel_results[0].verification_packet
        self.assertEqual(packet["candidate_targets"][0]["target"], "markdown")
        self.assertEqual(packet["recommended_target"], "markdown")

    def test_html_media_text_panels_become_links(self):
        dashboard = {
            "title": "Media Docs",
            "uid": "media-docs-1",
            "panels": [
                {
                    "id": 1,
                    "title": "Embedded",
                    "type": "text",
                    "gridPos": {"w": 24, "h": 3, "x": 0, "y": 0},
                    "content": '<iframe src="https://fusakla.github.io/Prometheus2-grafana-dashboard/" width="100%"></iframe>',
                    "mode": "html",
                },
                {
                    "id": 2,
                    "title": "Logo",
                    "type": "text",
                    "gridPos": {"w": 24, "h": 3, "x": 0, "y": 4},
                    "content": '<img src="https://cdn.worldvectorlogo.com/logos/prometheus.svg" height="140px">',
                    "mode": "html",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, yaml_path = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            payload = yaml.safe_load(yaml_path.read_text())
        panels = payload["dashboards"][0]["panels"]
        self.assertEqual(
            panels[0]["markdown"]["content"],
            "[Prometheus 2 Grafana Dashboard](https://fusakla.github.io/Prometheus2-grafana-dashboard/)",
        )
        self.assertEqual(
            panels[1]["markdown"]["content"],
            "![Prometheus](https://cdn.worldvectorlogo.com/logos/prometheus.svg)",
        )
        self.assertTrue(all(pr.recommended_target == "markdown" for pr in result.panel_results))

    def test_metadata_polish_humanizes_panel_and_control_labels(self):
        self.resolver._discovery_attempted = True
        self.resolver._field_cache = {"event.duration": {"long": {}}}
        dashboard = {
            "title": "Polish",
            "uid": "polish-1",
            "templating": {
                "list": [
                    {
                        "type": "query",
                        "name": "event.duration",
                        "query": "label_values(foo,event.duration)",
                    }
                ]
            },
            "panels": [
                {
                    "id": 3,
                    "title": "node_memory_memtotal_bytes",
                    "type": "graph",
                    "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
                    "targets": [{"refId": "A", "expr": 'sum(rate(foo_total[5m])) by (instance)'}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, yaml_path = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            summary = migrate.apply_metadata_polish(yaml_path, result, enable_ai=False)
            payload = yaml.safe_load(yaml_path.read_text())
        self.assertEqual(summary["mode"], "heuristic")
        self.assertEqual(payload["dashboards"][0]["panels"][0]["title"], "Node Memory Memtotal Bytes")
        self.assertEqual(payload["dashboards"][0]["controls"][0]["label"], "Event Duration")
        self.assertEqual(result.panel_results[0].metadata_polish["final_title"], "Node Memory Memtotal Bytes")

    def test_apply_metadata_polish_uses_emitted_panel_results(self):
        result = migrate.MigrationResult("Dashboard", "uid")
        manual = migrate.PanelResult("Manual Panel", "graph", "", "not_feasible", 0.0)
        manual.source_panel_id = "1"
        emitted = migrate.PanelResult("foo_total", "graph", "line", "migrated", 0.85)
        emitted.source_panel_id = "2"
        emitted.query_language = "promql"
        emitted.query_ir = {"metric": "foo_total", "output_shape": "time_series"}
        result.panel_results = [manual, emitted]
        result.yaml_panel_results = [emitted]

        payload = {
            "dashboards": [{
                "name": "Dashboard",
                "panels": [
                    {
                        "title": "graph",
                        "esql": {
                            "type": "line",
                            "query": "FROM metrics-*\n| LIMIT 10",
                        },
                    }
                ],
            }]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_path = pathlib.Path(tmpdir) / "dashboard.yaml"
            yaml_path.write_text(yaml.dump(payload, sort_keys=False))
            summary = migrate.apply_metadata_polish(yaml_path, result, enable_ai=False)
            rewritten = yaml.safe_load(yaml_path.read_text())

        self.assertEqual(summary["panel_titles"]["0"], "Foo Total")
        self.assertEqual(rewritten["dashboards"][0]["panels"][0]["title"], "Foo Total")
        self.assertEqual(emitted.title, "Foo Total")
        self.assertEqual(emitted.visual_ir.title, "Foo Total")
        self.assertEqual(manual.title, "Manual Panel")

    def test_verification_packet_marks_green_for_clean_panel(self):
        dashboard = {
            "title": "Verified",
            "uid": "verified-1",
            "panels": [
                {
                    "id": 1,
                    "title": "CPU Rate",
                    "type": "graph",
                    "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
                    "targets": [{"refId": "A", "expr": 'sum(rate(foo_total[5m])) by (instance)'}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
        verification = migrate.annotate_results_with_verification([result], [])
        packet = result.panel_results[0].verification_packet
        self.assertEqual(verification["summary"]["green"], 1)
        self.assertEqual(packet["semantic_gate"], "Green")
        self.assertEqual(packet["candidate_targets"][0]["target"], "native_esql_panel")
        self.assertEqual(packet["sample_window"]["time_from"], esql_validate.DEFAULT_TSTART_EXPR)
        self.assertEqual(packet["target_execution"]["status"], "not_run")
        self.assertEqual(packet["comparison"]["status"], "not_attempted")

    def test_verification_packet_marks_yellow_for_warning_panel(self):
        panel = {
            "title": "",
            "type": "bargauge",
            "gridPos": {"w": 6, "h": 4, "x": 0, "y": 0},
            "targets": [
                {
                    "refId": "A",
                    "expr": '(1 - (node_memory_MemAvailable_bytes{instance="$node"} / node_memory_MemTotal_bytes{instance="$node"})) * 100',
                    "instant": True,
                    "legendFormat": "Used RAM",
                },
                {
                    "refId": "B",
                    "expr": '(1 - ((node_memory_SwapFree_bytes{instance="$node"} + 1) / (node_memory_SwapTotal_bytes{instance="$node"} + 1))) * 100',
                    "instant": True,
                    "legendFormat": "Used SWAP",
                },
            ],
        }
        dashboard = {"title": "Warn", "uid": "warn-1", "panels": [panel]}
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
        migrate.annotate_results_with_verification([result], [])
        packet = result.panel_results[0].verification_packet
        self.assertEqual(packet["semantic_gate"], "Yellow")
        self.assertTrue(any(candidate["target"] == "manual_redesign" for candidate in packet["candidate_targets"]))

    def test_verification_packet_marks_red_on_validation_failure(self):
        dashboard = {
            "title": "Failure",
            "uid": "failure-1",
            "panels": [
                {
                    "id": 1,
                    "title": "Failing",
                    "type": "graph",
                    "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
                    "targets": [{"refId": "A", "expr": 'sum(rate(foo_total[5m])) by (instance)'}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
        validation_records = [
            {
                "dashboard": "Failure",
                "panel": "Failing",
                "status": "fail",
                "error": "Unknown column [foo_total]",
                "analysis": {"unknown_columns": [{"name": "foo_total", "role": "metric", "suggested_fields": []}]},
                "fix_attempts": [],
            }
        ]
        migrate.annotate_results_with_verification([result], validation_records)
        packet = result.panel_results[0].verification_packet
        self.assertEqual(packet["semantic_gate"], "Red")
        self.assertEqual(packet["validation_status"], "fail")
        self.assertEqual(packet["comparison"]["status"], "target_broken")
        self.assertEqual(result.panel_results[0].operational_ir.review.semantic_gate, "Red")

    def test_verification_matches_validation_records_by_source_panel_id(self):
        result = migrate.MigrationResult("Duplicate Titles", "duplicate-1")
        first = migrate.PanelResult("CPU Busy", "graph", "line", "migrated", 0.85)
        first.source_panel_id = "1"
        first.query_language = "promql"
        first.query_ir = {"output_shape": "time_series"}
        second = migrate.PanelResult("CPU Busy", "graph", "line", "migrated", 0.85)
        second.source_panel_id = "2"
        second.query_language = "promql"
        second.query_ir = {"output_shape": "time_series"}
        result.panel_results = [first, second]

        validation_records = [
            {
                "dashboard": "Duplicate Titles",
                "dashboard_uid": "duplicate-1",
                "panel": "CPU Busy",
                "source_panel_id": "2",
                "status": "fail",
                "error": "Unknown column [foo_total]",
                "analysis": {"unknown_columns": [{"name": "foo_total", "role": "metric", "suggested_fields": []}]},
                "fix_attempts": [],
            }
        ]

        migrate.annotate_results_with_verification([result], validation_records)

        self.assertEqual(first.verification_packet["validation_status"], "not_run")
        self.assertEqual(second.verification_packet["validation_status"], "fail")
        self.assertEqual(first.verification_packet["semantic_gate"], "Green")
        self.assertEqual(second.verification_packet["semantic_gate"], "Red")

    def test_verification_packet_includes_compile_and_upload_rollups(self):
        result = migrate.MigrationResult("Compile Failure", "compile-1")
        result.compiled = False
        result.compile_error = "kb-dashboard-cli compile failed"
        result.upload_attempted = True
        result.uploaded = False
        result.upload_error = "Upload skipped because one or more dashboards failed to compile."
        panel = migrate.PanelResult("CPU Busy", "graph", "line", "migrated", 0.85)
        panel.source_panel_id = "1"
        panel.query_language = "promql"
        panel.query_ir = {"output_shape": "time_series"}
        result.panel_results = [panel]

        migrate.annotate_results_with_verification([result], [])

        packet = panel.verification_packet
        self.assertIn("compile_failed", packet["runtime_rollups"])
        self.assertIn("upload_failed", packet["runtime_rollups"])
        self.assertEqual(packet["semantic_gate"], "Red")

    def test_review_explanations_attach_heuristic_panel_notes(self):
        dashboard = {
            "title": "Explain",
            "uid": "explain-1",
            "panels": [
                {
                    "id": 1,
                    "title": "CPU Rate",
                    "type": "graph",
                    "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
                    "targets": [{"refId": "A", "expr": 'sum(rate(foo_total[5m])) by (instance)'}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
        verification = migrate.annotate_results_with_verification([result], [])
        summary = migrate.apply_review_explanations([result], verification, enable_ai=False)
        explanation = result.panel_results[0].review_explanation
        self.assertEqual(summary["mode"], "heuristic")
        self.assertEqual(explanation["mode"], "heuristic")
        self.assertIn("validated cleanly", explanation["summary"])
        self.assertTrue(explanation["suggested_checks"])

    def test_review_explanations_mark_skipped_panels_as_ignored(self):
        dashboard = {
            "title": "Skip Explain",
            "uid": "skip-explain-1",
            "panels": [
                {
                    "id": 1,
                    "title": "News",
                    "type": "news",
                    "gridPos": {"w": 24, "h": 1, "x": 0, "y": 0},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
        verification = migrate.annotate_results_with_verification([result], [])
        migrate.apply_review_explanations([result], verification, enable_ai=False)
        explanation = result.panel_results[0].review_explanation
        self.assertIn("intentionally skipped", explanation["summary"])
        self.assertIn("No review needed", explanation["suggested_checks"][0])

    def test_review_explanations_note_missing_local_ai_configuration(self):
        dashboard = {
            "title": "AI Explain",
            "uid": "ai-explain-1",
            "panels": [
                {
                    "id": 1,
                    "title": "Failing",
                    "type": "graph",
                    "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
                    "targets": [{"refId": "A", "expr": 'sum(rate(foo_total[5m])) by (instance)'}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
        verification = migrate.annotate_results_with_verification(
            [result],
            [
                {
                    "dashboard": "AI Explain",
                    "panel": "Failing",
                    "status": "fail",
                    "error": "Unknown column [foo_total]",
                    "analysis": {"unknown_columns": [{"name": "foo_total"}]},
                    "fix_attempts": [],
                }
            ],
        )
        summary = migrate.apply_review_explanations(
            [result],
            verification,
            enable_ai=True,
            ai_endpoint="",
            ai_model="",
        )
        explanation = result.panel_results[0].review_explanation
        self.assertEqual(summary["mode"], "heuristic")
        self.assertTrue(any("not configured" in note for note in summary["notes"]))
        self.assertEqual(explanation["mode"], "heuristic")
        self.assertIn("Runtime validation failed", explanation["summary"])
        self.assertIn("foo_total", explanation["suggested_checks"][0])

    def test_review_explanations_batch_and_reuse_duplicate_cases(self):
        dashboard = {
            "title": "Batch Explain",
            "uid": "batch-explain-1",
            "panels": [
                {
                    "id": 1,
                    "title": "Failing A",
                    "type": "graph",
                    "gridPos": {"w": 12, "h": 8, "x": 0, "y": 0},
                    "targets": [{"refId": "A", "expr": 'sum(rate(foo_total[5m])) by (instance)'}],
                },
                {
                    "id": 2,
                    "title": "Failing B",
                    "type": "graph",
                    "gridPos": {"w": 12, "h": 8, "x": 12, "y": 0},
                    "targets": [{"refId": "A", "expr": 'sum(rate(foo_total[5m])) by (instance)'}],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _ = migrate.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
        verification = migrate.annotate_results_with_verification(
            [result],
            [
                {
                    "dashboard": "Batch Explain",
                    "panel": "Failing A",
                    "status": "fail",
                    "error": "Unknown column [foo_total]",
                    "analysis": {"unknown_columns": [{"name": "foo_total"}]},
                    "fix_attempts": [],
                },
                {
                    "dashboard": "Batch Explain",
                    "panel": "Failing B",
                    "status": "fail",
                    "error": "Unknown column [foo_total]",
                    "analysis": {"unknown_columns": [{"name": "foo_total"}]},
                    "fix_attempts": [],
                },
            ],
        )
        batch_calls = []

        def fake_batch_request(items, endpoint, model, api_key="", timeout=20):
            batch_calls.append(items)
            return {
                items[0]["id"]: {
                    "summary": "Schema mismatch needs manual review.",
                    "suggested_checks": ["Check the Elasticsearch field mapping."],
                    "notes": [],
                }
            }

        with mock.patch.dict(
            migrate.apply_review_explanations.__globals__,
            {"_local_ai_batch_request": fake_batch_request},
        ):
            summary = migrate.apply_review_explanations(
                [result],
                verification,
                enable_ai=True,
                ai_endpoint="http://localhost:11434/v1",
                ai_model="qwen3.5:35b",
            )

        self.assertEqual(len(batch_calls), 1)
        self.assertEqual(len(batch_calls[0]), 1)
        self.assertEqual(summary["ai_requests"], 1)
        self.assertEqual(summary["unique_ai_cases"], 1)
        self.assertEqual(summary["reused_panels"], 1)
        self.assertEqual(summary["ai_panels"], 2)
        self.assertEqual(result.panel_results[0].review_explanation["mode"], "local_ai")
        self.assertEqual(result.panel_results[1].review_explanation["mode"], "local_ai")

    def test_review_explanations_skip_routine_yellow_panels_for_ai(self):
        panel_result = SimpleNamespace(
            status="migrated_with_warnings",
            reasons=["Grouped series preserved."],
            notes=[],
            promql_expr="sum(foo_total) by (instance)",
            esql_query="FROM metrics-* | STATS foo_total = SUM(value) BY instance",
            verification_packet={
                "dashboard": "Yellow Explain",
                "panel": "Formula",
                "semantic_gate": "Yellow",
                "validation_status": "pass",
                "known_semantic_losses": [],
                "validation": {},
                "candidate_targets": [],
            },
        )
        result = SimpleNamespace(panel_results=[panel_result])
        verification = {}

        def fail_if_called(*args, **kwargs):
            raise AssertionError("routine yellow panels should stay heuristic")

        with mock.patch.dict(
            migrate.apply_review_explanations.__globals__,
            {"_local_ai_batch_request": fail_if_called},
        ):
            summary = migrate.apply_review_explanations(
                [result],
                verification,
                enable_ai=True,
                ai_endpoint="http://localhost:11434/v1",
                ai_model="qwen3.5:35b",
            )

        self.assertEqual(summary["ai_panels"], 0)
        self.assertEqual(summary["heuristic_panels"], 1)
        self.assertEqual(panel_result.review_explanation["mode"], "heuristic")

    def test_build_metric_contract_artifacts_replaces_alias_metadata_with_source_metrics(self):
        """When `multi_series_metric_fields` metadata holds output column aliases
        (set by panel-level fusion), the contract should derive the real source
        metric names from the source expression instead.
        """
        from observability_migration.adapters.source.grafana.translate import (
            _build_metric_contract_artifacts,
        )
        from observability_migration.core.assets.query import QueryIR

        query_ir = QueryIR(
            source_language="promql",
            source_expression=(
                "sum(kube_namespace_labels) ||| sum(kube_pod_container_status_running) "
                "||| sum(kube_pod_container_status_waiting)"
            ),
            clean_expression=(
                "sum(kube_namespace_labels) ||| sum(kube_pod_container_status_running) "
                "||| sum(kube_pod_container_status_waiting)"
            ),
            metric="computed_value",
            target_index="metrics-*",
            panel_type="timeseries",
            metadata={
                "multi_series_metric_fields": [
                    "Namespaces",
                    "Running_Containers",
                    "Waiting_Containers",
                ],
            },
        )

        contract, _evaluation, _fulfillment = _build_metric_contract_artifacts(
            query_ir,
            resolver=None,
            rule_pack=self.rule_pack,
        )

        contract_dict = contract.to_dict() if hasattr(contract, "to_dict") else contract
        names = sorted(item["name"] for item in contract_dict["field_requirements"])
        self.assertEqual(
            names,
            [
                "kube_namespace_labels",
                "kube_pod_container_status_running",
                "kube_pod_container_status_waiting",
            ],
        )
        for alias in ("Namespaces", "Running_Containers", "Waiting_Containers"):
            self.assertNotIn(alias, names)

    def test_metric_candidates_strips_aggregation_and_interpolation_tokens(self):
        """_metric_candidates should drop labels inside `by(...)` / `without(...)`,
        Grafana interpolation tokens like `$__rate_interval`, and bracketed
        time ranges.
        """
        from observability_migration.adapters.source.grafana.preflight import (
            _metric_candidates,
        )

        expr = (
            'sum(rate(node_network_receive_bytes_total{device!~"veth.*",'
            ' cluster="$cluster", job="$job"}[$__rate_interval])) by (device)'
        )
        candidates = _metric_candidates({
            "source_expression": expr,
            "clean_expression": expr,
            "metric": "",
            "source_metric": "",
        })
        self.assertIn("node_network_receive_bytes_total", candidates)
        for label in ("device", "cluster", "job", "__rate_interval"):
            self.assertNotIn(label, candidates)

    def test_metric_candidates_strips_without_clauses(self):
        from observability_migration.adapters.source.grafana.preflight import (
            _metric_candidates,
        )

        expr = "sum without (instance, job) (rate(http_requests_total[5m]))"
        candidates = _metric_candidates({
            "source_expression": expr,
            "clean_expression": expr,
            "metric": "",
            "source_metric": "",
        })
        self.assertIn("http_requests_total", candidates)
        for label in ("instance", "job"):
            self.assertNotIn(label, candidates)

    def test_metric_candidates_strips_on_and_group_modifiers(self):
        """`on(...)` and `group_left`/`group_right` modifiers must not leak
        bare label names into the metric candidate set."""
        from observability_migration.adapters.source.grafana.preflight import _metric_candidates

        expr = (
            'sum by(instance) (irate(node_cpu_guest_seconds_total{instance="$node"}[1m]))'
            ' / on(instance) group_left sum by (instance)'
            '((irate(node_cpu_seconds_total{instance="$node"}[1m])))'
        )
        candidates = _metric_candidates({
            "source_expression": expr,
            "clean_expression": expr,
            "metric": "",
            "source_metric": "",
        })
        self.assertIn("node_cpu_guest_seconds_total", candidates)
        self.assertIn("node_cpu_seconds_total", candidates)
        self.assertNotIn("instance", candidates)

    def test_metric_candidates_strips_ignoring_modifier(self):
        from observability_migration.adapters.source.grafana.preflight import _metric_candidates

        expr = (
            'sum(rate(http_requests_total[5m])) / ignoring(env) group_left'
            ' sum(rate(http_response_total[5m]))'
        )
        candidates = _metric_candidates({
            "source_expression": expr,
            "clean_expression": expr,
            "metric": "",
            "source_metric": "",
        })
        self.assertIn("http_requests_total", candidates)
        self.assertIn("http_response_total", candidates)
        self.assertNotIn("env", candidates)

    def test_metric_candidates_drops_set_operators_OR_AND_UNLESS(self):
        """PromQL set operators (OR/AND/UNLESS, any case) must not be
        treated as metric candidates."""
        from observability_migration.adapters.source.grafana.preflight import _metric_candidates

        expr = (
            'kube_pod_container_info{image!=""} OR kube_pod_container_info'
            '{container_id!=""} AND kube_namespace_created UNLESS kube_pod_info'
        )
        candidates = _metric_candidates({
            "source_expression": expr,
            "clean_expression": expr,
            "metric": "",
            "source_metric": "",
        })
        for metric in ("kube_pod_container_info", "kube_namespace_created", "kube_pod_info"):
            self.assertIn(metric, candidates)
        for op in ("OR", "AND", "UNLESS", "or", "and", "unless"):
            self.assertNotIn(op, candidates)

    def test_metric_candidates_keeps_built_in_alerts_and_count_sum_metrics(self):
        """Capitalized metric names like ALERTS and real `_count`/`_sum` metrics
        must NOT be mistaken for output aliases by the candidate scanner."""
        from observability_migration.adapters.source.grafana.preflight import _metric_candidates

        expr = (
            'ALERTS{alertstate="firing"} or '
            'sum(increase(prometheus_target_sync_length_seconds_count[5m])) or '
            'sum(increase(prometheus_tsdb_compaction_duration_sum[30m]))'
        )
        candidates = _metric_candidates({
            "source_expression": expr,
            "clean_expression": expr,
            "metric": "",
            "source_metric": "",
        })
        for name in ("ALERTS", "prometheus_target_sync_length_seconds_count", "prometheus_tsdb_compaction_duration_sum"):
            self.assertIn(name, candidates)

    def test_metric_candidates_handles_uppercase_by_without_on_ignoring(self):
        """PromQL clauses are case-insensitive in real-world dashboards; the
        stripper must drop labels inside uppercased ``BY``/``WITHOUT``/``ON``/
        ``IGNORING``/``group_left``/``group_right``.
        """
        from observability_migration.adapters.source.grafana.preflight import _metric_candidates
        for clause in ("by", "BY", "By"):
            expr = f"sum(rate(http_requests_total[5m])) {clause} (instance)"
            cands = _metric_candidates({"source_expression": expr, "clean_expression": expr, "metric": "", "source_metric": ""})
            self.assertIn("http_requests_total", cands, f"{clause}: lost the metric name")
            self.assertNotIn("instance", cands, f"{clause}: leaked the by-label")
        for clause in ("without", "WITHOUT"):
            expr = f"sum {clause} (instance) (rate(http_requests_total[5m]))"
            cands = _metric_candidates({"source_expression": expr, "clean_expression": expr, "metric": "", "source_metric": ""})
            self.assertNotIn("instance", cands, f"{clause}: leaked the without-label")
        for clause in ("on", "ON"):
            expr = f"rate(metric_a[5m]) / {clause}(instance) group_left rate(metric_b[5m])"
            cands = _metric_candidates({"source_expression": expr, "clean_expression": expr, "metric": "", "source_metric": ""})
            self.assertNotIn("instance", cands, f"{clause}: leaked the on-label")

    def test_metric_candidates_filters_uppercase_promql_keywords(self):
        from observability_migration.adapters.source.grafana.preflight import _metric_candidates
        # SUM and RATE as bare identifiers should not become candidates even
        # if they appear outside of function call context (rare but possible
        # when migrating partial PromQL fragments).
        expr = "SUM(RATE(http_requests_total[5m]))"
        cands = _metric_candidates({"source_expression": expr, "clean_expression": expr, "metric": "", "source_metric": ""})
        self.assertIn("http_requests_total", cands)
        for name in ("SUM", "Sum", "RATE", "Rate", "sum", "rate"):
            self.assertNotIn(name, cands)

    def test_build_metric_contract_artifacts_omits_all_tsds_when_fields_missing(self):
        """When all required fields are missing from target capabilities, the
        evaluator must not also report 'not all-TSDS' (we have no info about
        index mode in that case).
        """
        from observability_migration.adapters.source.grafana.translate import (
            _build_metric_contract_artifacts,
        )
        from observability_migration.core.assets.query import QueryIR

        query_ir = QueryIR(
            source_language="promql",
            source_expression="count(up == 1)",
            clean_expression="count(up == 1)",
            metric="up_count",
            target_index="metrics-*",
            panel_type="stat",
        )

        _contract, evaluation, _fulfillment = _build_metric_contract_artifacts(
            query_ir,
            resolver=None,
            rule_pack=self.rule_pack,
        )

        unsatisfied = list(evaluation.unsatisfied) if hasattr(evaluation, 'unsatisfied') else list((evaluation or {}).get('unsatisfied', []))
        self.assertFalse(
            any("not all-TSDS" in reason for reason in unsatisfied),
            f"Expected no 'not all-TSDS' reason when all fields are missing, got: {unsatisfied}",
        )

    def test_translation_hints_include_all_legend_labels_when_no_explicit_by(self):
        """All `{{label}}` placeholders in legendFormat must enter
        `preferred_group_labels`, not just the first one."""
        from observability_migration.adapters.source.grafana.panels import (
            _target_translation_hints,
        )

        panel = {"type": "timeseries", "targets": []}
        target = {
            "expr": 'irate(node_interrupts_total{instance="$node"}[5m])',
            "legendFormat": "{{ type }} - {{ info }}",
            "format": "time_series",
        }
        hints = _target_translation_hints(panel, "timeseries", target)
        self.assertEqual(hints.get("preferred_group_labels"), ["type", "info"])
        self.assertEqual(hints.get("preferred_group_labels_origin"), "legend")

    def test_translation_hints_dedupes_repeated_legend_labels(self):
        from observability_migration.adapters.source.grafana.panels import (
            _target_translation_hints,
        )

        panel = {"type": "timeseries", "targets": []}
        target = {
            "expr": 'rate(metric_x[5m])',
            "legendFormat": "{{ a }} on {{ a }} - {{ b }}",
            "format": "time_series",
        }
        hints = _target_translation_hints(panel, "timeseries", target)
        self.assertEqual(hints.get("preferred_group_labels"), ["a", "b"])

    def test_translation_hints_table_style_patterns_do_not_set_legend_origin(self):
        """When panel-style patterns contribute, the origin must NOT be
        marked as legend (so the consumer still unions with explicit by())."""
        from observability_migration.adapters.source.grafana.panels import (
            _target_translation_hints,
        )

        panel = {
            "type": "table",
            "targets": [],
            "styles": [
                {"pattern": "namespace", "type": "string"},
            ],
        }
        target = {
            "expr": 'sum(rate(metric_x{cluster="$cluster"}[5m])) by (namespace)',
            "legendFormat": "{{ namespace }}",
            "format": "table",
        }
        hints = _target_translation_hints(panel, "table", target)
        self.assertIn("namespace", hints.get("preferred_group_labels", []))
        self.assertNotEqual(hints.get("preferred_group_labels_origin"), "legend")

    def test_translator_widens_by_with_multi_label_legend_when_no_explicit_by(self):
        """End-to-end: a multi-label legend on a PromQL with no `by(...)` must
        produce a wider BY clause in the emitted ESQL."""
        translated = self.translate(
            'irate(node_interrupts_total[5m])',
            translation_hints={
                "preferred_group_labels": ["type", "info"],
                "preferred_group_labels_origin": "legend",
            },
        )
        self.assertIn("BY time_bucket", translated.esql_query)
        self.assertIn("type", translated.esql_query)
        self.assertIn("info", translated.esql_query)

    def test_translator_keeps_explicit_by_when_legend_origin_is_legend(self):
        """When PromQL has explicit `by(handler)`, a legend that mentions extra
        labels must NOT widen the BY clause (the operator already chose the
        cardinality).

        Use labels that the OTEL resolver does NOT remap (``handler``, ``info``,
        ``type``); otherwise the test could pass vacuously because the
        candidate label gets rewritten to ``service.instance.id`` etc.
        """
        widened = self.translate(
            'sum(increase(http_requests_total[5m])) by (handler)',
            translation_hints={
                "preferred_group_labels": ["handler", "info", "type"],
                "preferred_group_labels_origin": "legend",
            },
        )
        unwidened = self.translate(
            'sum(increase(http_requests_total[5m])) by (handler)',
            translation_hints=None,
        )
        self.assertIn("BY time_bucket", widened.esql_query)
        self.assertIn("handler", widened.esql_query)
        self.assertNotIn("info", widened.esql_query)
        self.assertNotIn(" type", widened.esql_query.replace(",", " "))
        widened_by_tail = widened.esql_query.split("BY ", 1)[-1]
        unwidened_by_tail = unwidened.esql_query.split("BY ", 1)[-1]
        self.assertEqual(widened_by_tail, unwidened_by_tail)


class TestVisualIRContract(unittest.TestCase):

    def test_safe_int_on_non_numeric_layout(self):
        from observability_migration.core.assets.visual import VisualIR
        yaml_panel = {
            "title": "Bad Layout",
            "size": {"w": "auto", "h": None},
            "position": {"x": "?", "y": ""},
            "esql": {"query": "FROM x", "type": "line"},
        }
        ir = VisualIR.from_yaml_panel(yaml_panel)
        self.assertEqual(ir.layout.x, 0)
        self.assertEqual(ir.layout.y, 0)
        self.assertEqual(ir.layout.w, 0)
        self.assertEqual(ir.layout.h, 0)
        self.assertEqual(ir.presentation.kind, "esql")

    def test_round_trip_from_yaml_to_yaml(self):
        from observability_migration.core.assets.visual import VisualIR
        yaml_panel = {
            "title": "Round Trip",
            "size": {"w": 48, "h": 8},
            "position": {"x": 0, "y": 10},
            "esql": {"query": "FROM metrics-*\n| STATS x = AVG(y)", "type": "line"},
        }
        ir = VisualIR.from_yaml_panel(yaml_panel, kibana_type="line")
        rebuilt = ir.to_yaml_panel()
        self.assertEqual(rebuilt["title"], "Round Trip")
        self.assertEqual(rebuilt["size"]["w"], 48)
        self.assertEqual(rebuilt["position"]["y"], 10)
        self.assertEqual(rebuilt["esql"]["query"], yaml_panel["esql"]["query"])

    def test_empty_yaml_panel_returns_empty_ir(self):
        from observability_migration.core.assets.visual import VisualIR
        ir = VisualIR.from_yaml_panel(None)
        self.assertEqual(ir.title, "")
        self.assertEqual(ir.presentation.kind, "")

    def test_refresh_visual_ir_returns_typed_instance(self):
        from observability_migration.core.assets.visual import VisualIR, refresh_visual_ir
        panel_result = migrate.PanelResult("Panel", "graph", "line", "migrated", 0.9)
        panel_result.query_ir = {"output_shape": "time_series"}
        yaml_panel = {"title": "Test", "size": {"w": 24, "h": 6}, "position": {"x": 0, "y": 0}, "esql": {"query": "FROM x"}}
        ir = refresh_visual_ir(panel_result, yaml_panel)
        self.assertIsInstance(ir, VisualIR)
        self.assertEqual(ir.metadata["output_shape"], "time_series")

    def test_refresh_visual_ir_empty_yaml_returns_empty_ir(self):
        from observability_migration.core.assets.visual import VisualIR, refresh_visual_ir
        panel_result = migrate.PanelResult("Panel", "graph", "line", "skipped", 0.0)
        ir = refresh_visual_ir(panel_result, None)
        self.assertIsInstance(ir, VisualIR)
        self.assertEqual(ir.title, "")

    def test_to_dict_serializes_nested_dataclasses(self):
        from observability_migration.core.assets.visual import VisualIR
        ir = VisualIR(title="T", kibana_type="line")
        d = ir.to_dict()
        self.assertIsInstance(d, dict)
        self.assertIsInstance(d["layout"], dict)
        self.assertEqual(d["layout"]["x"], 0)


class TestOperationalIRContract(unittest.TestCase):

    def test_safe_float_on_bad_confidence(self):
        from observability_migration.core.assets.operational import build_operational_ir
        panel_result = migrate.PanelResult("Panel", "graph", "line", "migrated", "not_a_number")
        ir = build_operational_ir(panel_result)
        self.assertEqual(ir.confidence, 0.0)

    def test_build_with_none_panel_result(self):
        from observability_migration.core.assets.operational import build_operational_ir
        ir = build_operational_ir(None, dashboard_title="Dash")
        self.assertEqual(ir.lineage.dashboard_title, "Dash")
        self.assertEqual(ir.status, "")

    def test_typed_operational_ir_on_panel_result(self):
        from observability_migration.core.assets.operational import OperationalIR, build_operational_ir
        panel_result = migrate.PanelResult("Panel", "graph", "line", "migrated", 0.9)
        panel_result.operational_ir = build_operational_ir(panel_result, semantic_gate="Green")
        self.assertIsInstance(panel_result.operational_ir, OperationalIR)
        self.assertEqual(panel_result.operational_ir.review.semantic_gate, "Green")


class TestSourceExecutionAdapters(unittest.TestCase):

    def test_promql_without_url_returns_not_configured(self):
        from observability_migration.adapters.source.grafana.execution.source import build_source_execution_summary
        panel_result = migrate.PanelResult("Panel", "graph", "line", "migrated", 0.9)
        panel_result.query_language = "promql"
        panel_result.promql_expr = "up{job='node'}"
        summary = build_source_execution_summary(panel_result)
        self.assertEqual(summary.status, "not_configured")
        self.assertEqual(summary.adapter, "prometheus_http")
        self.assertIn("--prometheus-url", summary.reason)

    def test_logql_without_url_returns_not_configured(self):
        from observability_migration.adapters.source.grafana.execution.source import build_source_execution_summary
        panel_result = migrate.PanelResult("Panel", "logs", "table", "migrated", 0.8)
        panel_result.query_language = "logql"
        summary = build_source_execution_summary(panel_result)
        self.assertEqual(summary.status, "not_configured")
        self.assertEqual(summary.adapter, "loki_http")

    def test_esql_returns_not_applicable(self):
        from observability_migration.adapters.source.grafana.execution.source import build_source_execution_summary
        panel_result = migrate.PanelResult("Panel", "graph", "line", "migrated", 0.9)
        panel_result.query_language = "esql"
        panel_result.esql_query = "FROM metrics-*"
        summary = build_source_execution_summary(panel_result)
        self.assertEqual(summary.status, "not_applicable")

    def test_unknown_language_returns_not_applicable(self):
        from observability_migration.adapters.source.grafana.execution.source import build_source_execution_summary
        panel_result = migrate.PanelResult("Panel", "graph", "line", "migrated", 0.9)
        panel_result.query_language = "sql"
        summary = build_source_execution_summary(panel_result)
        self.assertEqual(summary.status, "not_applicable")
        self.assertEqual(summary.adapter, "none")

    def test_empty_promql_with_url_returns_skip(self):
        from observability_migration.adapters.source.grafana.execution.source import build_source_execution_summary
        panel_result = migrate.PanelResult("Panel", "graph", "line", "migrated", 0.9)
        panel_result.query_language = "promql"
        panel_result.promql_expr = ""
        summary = build_source_execution_summary(panel_result, prometheus_url="http://prom:9090")
        self.assertEqual(summary.status, "skip")
        self.assertIn("empty", summary.reason)


class TestValueLevelComparators(unittest.TestCase):

    def test_within_tolerance_matching_row_counts(self):
        from observability_migration.core.verification.comparators import build_comparison_result
        source = {"status": "pass", "result_summary": {"rows": 100, "columns": ["time", "value"]}}
        target = {"status": "pass", "result_summary": {"rows": 105, "columns": ["time", "value"]}}
        result = build_comparison_result(source, target, {"output_shape": "time_series"})
        self.assertEqual(result.status, "within_tolerance")

    def test_drift_on_divergent_row_counts(self):
        from observability_migration.core.verification.comparators import build_comparison_result
        source = {"status": "pass", "result_summary": {"rows": 100, "columns": ["time", "value"]}}
        target = {"status": "pass", "result_summary": {"rows": 50, "columns": ["time", "value"]}}
        result = build_comparison_result(source, target, {"output_shape": "time_series"})
        self.assertEqual(result.status, "drift")

    def test_target_broken_on_fail_status(self):
        from observability_migration.core.verification.comparators import build_comparison_result
        source = {"status": "pass", "result_summary": {"rows": 100, "columns": ["time"]}}
        target = {"status": "fail", "error": "parse error"}
        result = build_comparison_result(source, target, {})
        self.assertEqual(result.status, "target_broken")

    def test_target_only_when_source_not_configured(self):
        from observability_migration.core.verification.comparators import build_comparison_result
        source = {"status": "not_configured", "reason": "adapter not available"}
        target = {"status": "pass", "result_summary": {"rows": 10, "columns": ["x"]}}
        result = build_comparison_result(source, target, {})
        self.assertEqual(result.status, "target_only")

    def test_column_overlap_drift(self):
        from observability_migration.core.verification.comparators import build_comparison_result
        source = {"status": "pass", "result_summary": {"rows": 10, "columns": ["a", "b", "c", "d"]}}
        target = {"status": "pass", "result_summary": {"rows": 10, "columns": ["a"]}}
        result = build_comparison_result(source, target, {"output_shape": "table"})
        self.assertEqual(result.status, "drift")
        self.assertTrue(any("not found" in ce for ce in result.counterexamples))

    def test_scalar_comparison_within_tolerance(self):
        from observability_migration.core.verification.comparators import _compare_scalar
        status, _ = _compare_scalar(100.0, 103.0)
        self.assertEqual(status, "within_tolerance")

    def test_scalar_comparison_material_drift(self):
        from observability_migration.core.verification.comparators import _compare_scalar
        status, _ = _compare_scalar(100.0, 1.0)
        self.assertEqual(status, "material_drift")

    def test_both_zero_rows_within_tolerance(self):
        from observability_migration.core.verification.comparators import _compare_row_counts
        status, _ = _compare_row_counts(0, 0)
        self.assertEqual(status, "within_tolerance")

    def test_comparison_gate_override_for_material_drift(self):
        from observability_migration.core.verification.comparators import comparison_gate_override
        self.assertEqual(comparison_gate_override({"status": "material_drift"}), "Red")
        self.assertEqual(comparison_gate_override({"status": "drift"}), "Yellow")
        self.assertEqual(comparison_gate_override({"status": "within_tolerance"}), "Green")
        self.assertIsNone(comparison_gate_override({"status": "target_only"}))

    def test_variables_expanded_detects_template_tokens(self):
        from observability_migration.core.verification.comparators import build_sample_window
        window = build_sample_window({"source_expression": "rate(metric[$__rate_interval])"})
        self.assertFalse(window.variables_expanded)
        window2 = build_sample_window({"source_expression": "rate(metric[5m])"})
        self.assertTrue(window2.variables_expanded)


class TestVisualIRToYamlRoundTrip(unittest.TestCase):

    def test_esql_panel_survives_round_trip(self):
        from observability_migration.core.assets.visual import VisualIR
        original = {
            "title": "CPU Usage",
            "size": {"w": 24, "h": 8},
            "position": {"x": 0, "y": 0},
            "esql": {
                "query": "FROM metrics-*\n| STATS v = AVG(cpu) BY time_bucket = BUCKET(@timestamp, 20, NOW() - 1h, NOW())",
                "type": "line",
                "primary": {"field": "v"},
                "dimension": {"field": "time_bucket", "data_type": "date"},
            },
        }
        ir = VisualIR.from_yaml_panel(original, kibana_type="line", source_panel_id="1")
        rebuilt = ir.to_yaml_panel()
        self.assertEqual(rebuilt["title"], "CPU Usage")
        self.assertEqual(rebuilt["esql"]["query"], original["esql"]["query"])
        self.assertEqual(rebuilt["esql"]["type"], "line")
        self.assertEqual(rebuilt["size"]["w"], 24)

    def test_markdown_panel_survives_round_trip(self):
        from observability_migration.core.assets.visual import VisualIR
        original = {
            "title": "Notes",
            "size": {"w": 48, "h": 4},
            "position": {"x": 0, "y": 50},
            "markdown": {"content": "**Hello** world"},
        }
        ir = VisualIR.from_yaml_panel(original)
        rebuilt = ir.to_yaml_panel()
        self.assertEqual(rebuilt["markdown"]["content"], "**Hello** world")

    def test_empty_presentation_omits_config_key(self):
        from observability_migration.core.assets.visual import VisualIR
        ir = VisualIR(title="Empty", kibana_type="line")
        rebuilt = ir.to_yaml_panel()
        self.assertNotIn("esql", rebuilt)
        self.assertNotIn("markdown", rebuilt)


class TestTypedPanelResultSerialization(unittest.TestCase):

    def test_ir_to_dict_handles_typed_instances(self):
        from observability_migration.core.assets.operational import OperationalIR
        from observability_migration.core.assets.visual import VisualIR
        from observability_migration.core.reporting.report import _ir_to_dict
        self.assertIsInstance(_ir_to_dict(VisualIR(title="X")), dict)
        self.assertIsInstance(_ir_to_dict(OperationalIR(status="migrated")), dict)
        self.assertIsInstance(_ir_to_dict({}), dict)
        self.assertIsInstance(_ir_to_dict(None), dict)

    def test_panel_result_default_fields_are_typed(self):
        from observability_migration.core.assets.operational import OperationalIR
        from observability_migration.core.assets.visual import VisualIR
        pr = migrate.PanelResult("T", "graph", "line", "migrated", 0.9)
        self.assertIsInstance(pr.visual_ir, VisualIR)
        self.assertIsInstance(pr.operational_ir, OperationalIR)

    def test_report_serializes_typed_ir_to_dict(self):
        from observability_migration.core.assets.operational import OperationalIR
        from observability_migration.core.assets.visual import VisualIR
        from observability_migration.core.reporting.report import save_detailed_report
        pr = migrate.PanelResult("T", "graph", "line", "migrated", 0.9)
        pr.visual_ir = VisualIR(title="Serialized")
        pr.operational_ir = OperationalIR(status="migrated")
        result = migrate.MigrationResult("Dash", "uid-1")
        result.panel_results = [pr]
        result.total_panels = 1
        result.migrated = 1
        import os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name
        save_detailed_report([result], [], path)
        with open(path) as f:
            data = json.load(f)
        panel = data["dashboards"][0]["panels"][0]
        self.assertEqual(panel["visual_ir"]["title"], "Serialized")
        self.assertEqual(panel["operational_ir"]["status"], "migrated")
        os.unlink(path)


class TestDisplayMetadata(unittest.TestCase):
    """Tests for Grafana display metadata extraction."""

    def test_extract_unit_from_field_config(self):
        from observability_migration.targets.kibana.emit.display import extract_grafana_unit
        panel = {"fieldConfig": {"defaults": {"unit": "bytes"}}}
        self.assertEqual(extract_grafana_unit(panel), "bytes")

    def test_extract_unit_from_legacy_yaxes(self):
        from observability_migration.targets.kibana.emit.display import extract_grafana_unit
        panel = {"yaxes": [{"format": "percent"}, {"format": "short"}]}
        self.assertEqual(extract_grafana_unit(panel), "percent")

    def test_extract_unit_prefers_field_config(self):
        from observability_migration.targets.kibana.emit.display import extract_grafana_unit
        panel = {
            "fieldConfig": {"defaults": {"unit": "percentunit"}},
            "yaxes": [{"format": "bytes"}],
        }
        self.assertEqual(extract_grafana_unit(panel), "percentunit")

    def test_extract_unit_empty_panel(self):
        from observability_migration.targets.kibana.emit.display import extract_grafana_unit
        self.assertEqual(extract_grafana_unit({}), "")

    def test_unit_to_yaml_format_bytes(self):
        from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
        fmt = grafana_unit_to_yaml_format("bytes")
        self.assertEqual(fmt, {"type": "bytes"})

    def test_unit_to_yaml_format_percentunit(self):
        from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
        fmt = grafana_unit_to_yaml_format("percentunit")
        self.assertEqual(fmt, {"type": "percent"})

    def test_unit_to_yaml_format_bps(self):
        from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
        fmt = grafana_unit_to_yaml_format("Bps")
        self.assertEqual(fmt["type"], "bytes")
        self.assertIn("suffix", fmt)

    def test_unit_to_yaml_format_duration_seconds(self):
        from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
        fmt = grafana_unit_to_yaml_format("s")
        self.assertEqual(fmt, {"type": "duration"})

    def test_unit_to_yaml_format_none_returns_none(self):
        from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
        self.assertIsNone(grafana_unit_to_yaml_format("none"))
        self.assertIsNone(grafana_unit_to_yaml_format(""))

    def test_unit_to_yaml_format_unknown_returns_none(self):
        from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
        self.assertIsNone(grafana_unit_to_yaml_format("someUnknownUnit"))

    def test_extract_legend_modern(self):
        from observability_migration.targets.kibana.emit.display import extract_legend_config
        panel = {"options": {"legend": {"showLegend": True, "placement": "right"}}}
        legend = extract_legend_config(panel)
        self.assertIs(legend["visible"], True)
        self.assertEqual(legend["visible_str"], "show")
        self.assertEqual(legend["position"], "right")

    def test_extract_legend_modern_hidden(self):
        from observability_migration.targets.kibana.emit.display import extract_legend_config
        panel = {"options": {"legend": {"showLegend": False, "placement": "bottom"}}}
        legend = extract_legend_config(panel)
        self.assertIs(legend["visible"], False)
        self.assertEqual(legend["visible_str"], "hide")

    def test_extract_legend_legacy_right_side(self):
        from observability_migration.targets.kibana.emit.display import extract_legend_config
        panel = {"legend": {"show": True, "rightSide": True}}
        legend = extract_legend_config(panel)
        self.assertIs(legend["visible"], True)
        self.assertEqual(legend["visible_str"], "show")
        self.assertEqual(legend["position"], "right")

    def test_extract_legend_legacy_bottom(self):
        from observability_migration.targets.kibana.emit.display import extract_legend_config
        panel = {"legend": {"show": True, "rightSide": False}}
        legend = extract_legend_config(panel)
        self.assertEqual(legend["position"], "bottom")

    def test_extract_legend_legacy_hidden(self):
        from observability_migration.targets.kibana.emit.display import extract_legend_config
        panel = {"legend": {"show": False}}
        legend = extract_legend_config(panel)
        self.assertIs(legend["visible"], False)
        self.assertEqual(legend["visible_str"], "hide")

    def test_extract_legend_empty_panel(self):
        from observability_migration.targets.kibana.emit.display import extract_legend_config
        self.assertIsNone(extract_legend_config({}))

    def test_extract_axis_label_from_custom(self):
        from observability_migration.targets.kibana.emit.display import extract_axis_config
        panel = {"fieldConfig": {"defaults": {"custom": {"axisLabel": "Memory (bytes)"}}}}
        axis = extract_axis_config(panel)
        self.assertEqual(axis["y_left_axis"]["title"], "Memory (bytes)")

    def test_extract_axis_label_from_legacy_yaxes(self):
        from observability_migration.targets.kibana.emit.display import extract_axis_config
        panel = {"yaxes": [{"label": "Duration (s)", "logBase": 1}]}
        axis = extract_axis_config(panel)
        self.assertEqual(axis["y_left_axis"]["title"], "Duration (s)")

    def test_extract_axis_log_scale_modern(self):
        from observability_migration.targets.kibana.emit.display import extract_axis_config
        panel = {"fieldConfig": {"defaults": {"custom": {"scaleDistribution": {"type": "log"}}}}}
        axis = extract_axis_config(panel)
        self.assertEqual(axis["y_left_axis"]["scale"], "log")

    def test_extract_axis_log_scale_legacy(self):
        from observability_migration.targets.kibana.emit.display import extract_axis_config
        panel = {"yaxes": [{"logBase": 10, "label": None}]}
        axis = extract_axis_config(panel)
        self.assertEqual(axis["y_left_axis"]["scale"], "log")

    def test_extract_axis_extent_both_bounds(self):
        from observability_migration.targets.kibana.emit.display import extract_axis_config
        panel = {"yaxes": [{"min": "0", "max": "100", "label": None}]}
        axis = extract_axis_config(panel)
        extent = axis["y_left_axis"]["extent"]
        self.assertEqual(extent["mode"], "custom")
        self.assertEqual(extent["min"], 0.0)
        self.assertEqual(extent["max"], 100.0)

    def test_extract_axis_extent_min_only_skipped(self):
        from observability_migration.targets.kibana.emit.display import extract_axis_config
        panel = {"yaxes": [{"min": "0", "max": None, "label": None}]}
        axis = extract_axis_config(panel)
        self.assertIsNone(axis)

    def test_extract_axis_empty_returns_none(self):
        from observability_migration.targets.kibana.emit.display import extract_axis_config
        self.assertIsNone(extract_axis_config({}))

    def test_clean_template_dollar_var(self):
        from observability_migration.targets.kibana.emit.display import clean_template_variables
        self.assertEqual(
            clean_template_variables("NGINX Status for $instance"),
            "NGINX Status",
        )

    def test_clean_template_curly_brace(self):
        from observability_migration.targets.kibana.emit.display import clean_template_variables
        self.assertEqual(
            clean_template_variables("{{instance}} accepted"),
            "accepted",
        )

    def test_clean_template_advanced_format_var(self):
        from observability_migration.targets.kibana.emit.display import clean_template_variables
        self.assertEqual(
            clean_template_variables("CPU ${instance:text}"),
            "CPU",
        )

    def test_clean_template_preserves_currency_amounts(self):
        from observability_migration.targets.kibana.emit.display import clean_template_variables
        self.assertEqual(clean_template_variables("Cost $50 per unit"), "Cost $50 per unit")
        self.assertEqual(clean_template_variables("Tier $1"), "Tier $1")

    def test_clean_template_double_bracket(self):
        from observability_migration.targets.kibana.emit.display import clean_template_variables
        self.assertEqual(
            clean_template_variables("CPU Usage [[instance]]"),
            "CPU Usage",
        )

    def test_clean_template_chinese_brackets(self):
        from observability_migration.targets.kibana.emit.display import clean_template_variables
        result = clean_template_variables("Server Overview【JOB：$job，Total：$total】")
        self.assertNotIn("$job", result)
        self.assertNotIn("$total", result)
        self.assertEqual(result, "Server Overview")

    def test_clean_template_preserves_plain_text(self):
        from observability_migration.targets.kibana.emit.display import clean_template_variables
        self.assertEqual(
            clean_template_variables("CPU Usage"),
            "CPU Usage",
        )

    def test_clean_template_empty(self):
        from observability_migration.targets.kibana.emit.display import clean_template_variables
        self.assertEqual(clean_template_variables(""), "")

    def test_link_variable_rewrite_supports_advanced_and_bracket_syntax(self):
        self.assertEqual(
            links._rewrite_grafana_variables_to_kibana("https://x/?host=${host:queryparam}&node=[[node]]"),
            "https://x/?host={{context.host}}&node={{context.node}}",
        )

    def test_humanize_metric_label_from_legend_format(self):
        from observability_migration.targets.kibana.emit.display import humanize_metric_label
        self.assertEqual(
            humanize_metric_label("instance___accepted", "{{instance}} accepted"),
            "accepted",
        )

    def test_humanize_metric_label_cleans_grafana_variable_tokens(self):
        from observability_migration.targets.kibana.emit.display import humanize_metric_label
        self.assertEqual(humanize_metric_label("cpu", "CPU on $node"), "CPU")
        self.assertEqual(humanize_metric_label("cpu", "CPU [[node]]"), "CPU")

    def test_humanize_metric_label_from_field_name(self):
        from observability_migration.targets.kibana.emit.display import humanize_metric_label
        label = humanize_metric_label("container_cpu_usage_seconds_total_calc")
        self.assertIsNotNone(label)
        self.assertNotIn("_", label)

    def test_humanize_metric_label_simple_word(self):
        from observability_migration.targets.kibana.emit.display import humanize_metric_label
        self.assertIsNone(humanize_metric_label("active"))

    def test_humanize_metric_label_empty(self):
        from observability_migration.targets.kibana.emit.display import humanize_metric_label
        self.assertIsNone(humanize_metric_label(""))

    def test_enrich_xy_panel_adds_format_and_legend(self):
        from observability_migration.targets.kibana.emit.display import enrich_yaml_panel_display
        yaml_panel = {
            "esql": {
                "type": "line",
                "query": "FROM metrics-*",
                "dimension": {"field": "time_bucket", "data_type": "date"},
                "metrics": [{"field": "cpu_usage"}],
            }
        }
        grafana_panel = {
            "fieldConfig": {"defaults": {"unit": "percentunit"}},
            "options": {"legend": {"showLegend": True, "placement": "bottom"}},
        }
        enrich_yaml_panel_display(yaml_panel, grafana_panel)
        self.assertEqual(yaml_panel["esql"]["metrics"][0]["format"]["type"], "percent")
        self.assertEqual(yaml_panel["esql"]["legend"]["position"], "bottom")
        self.assertEqual(yaml_panel["esql"]["dimension"]["label"], "Time")

    def test_enrich_metric_panel_adds_format(self):
        from observability_migration.targets.kibana.emit.display import enrich_yaml_panel_display
        yaml_panel = {
            "esql": {
                "type": "metric",
                "query": "FROM metrics-*",
                "primary": {"field": "avg_value"},
            }
        }
        grafana_panel = {"fieldConfig": {"defaults": {"unit": "bytes"}}}
        enrich_yaml_panel_display(yaml_panel, grafana_panel)
        self.assertEqual(yaml_panel["esql"]["primary"]["format"]["type"], "bytes")

    def test_enrich_gauge_panel_adds_format(self):
        from observability_migration.targets.kibana.emit.display import enrich_yaml_panel_display
        yaml_panel = {
            "esql": {
                "type": "gauge",
                "query": "FROM metrics-*",
                "metric": {"field": "cpu_pct"},
            }
        }
        grafana_panel = {"fieldConfig": {"defaults": {"unit": "percent"}}}
        enrich_yaml_panel_display(yaml_panel, grafana_panel)
        fmt = yaml_panel["esql"]["metric"]["format"]
        self.assertEqual(fmt["type"], "number")
        self.assertIn("suffix", fmt)

    def test_enrich_with_metric_labels(self):
        from observability_migration.targets.kibana.emit.display import enrich_yaml_panel_display
        yaml_panel = {
            "esql": {
                "type": "line",
                "query": "FROM metrics-*",
                "dimension": {"field": "time_bucket", "data_type": "date"},
                "metrics": [
                    {"field": "instance___accepted"},
                    {"field": "instance___handled"},
                ],
            }
        }
        grafana_panel = {}
        labels = {
            "instance___accepted": "{{instance}} accepted",
            "instance___handled": "{{instance}} handled",
        }
        enrich_yaml_panel_display(yaml_panel, grafana_panel, metric_labels=labels)
        self.assertEqual(yaml_panel["esql"]["metrics"][0]["label"], "accepted")
        self.assertEqual(yaml_panel["esql"]["metrics"][1]["label"], "handled")

    def test_enrich_pie_legend(self):
        from observability_migration.targets.kibana.emit.display import enrich_yaml_panel_display
        yaml_panel = {
            "esql": {
                "type": "pie",
                "query": "FROM metrics-*",
                "metrics": [{"field": "count"}],
                "breakdowns": [{"field": "category"}],
            }
        }
        grafana_panel = {"options": {"legend": {"showLegend": False}}}
        enrich_yaml_panel_display(yaml_panel, grafana_panel)
        self.assertEqual(yaml_panel["esql"]["legend"]["visible"], "hide")

    def test_enrich_axis_config(self):
        from observability_migration.targets.kibana.emit.display import enrich_yaml_panel_display
        yaml_panel = {
            "esql": {
                "type": "line",
                "query": "FROM metrics-*",
                "metrics": [{"field": "value"}],
            }
        }
        grafana_panel = {
            "fieldConfig": {"defaults": {"custom": {"axisLabel": "CPU %"}}},
            "options": {"legend": {"showLegend": True, "placement": "bottom"}},
        }
        enrich_yaml_panel_display(yaml_panel, grafana_panel)
        self.assertEqual(
            yaml_panel["esql"]["appearance"]["y_left_axis"]["title"], "CPU %"
        )

    def test_enrich_no_esql_is_noop(self):
        from observability_migration.targets.kibana.emit.display import enrich_yaml_panel_display
        yaml_panel = {"markdown": {"content": "Hello"}}
        enrich_yaml_panel_display(yaml_panel, {})
        self.assertNotIn("esql", yaml_panel)

    def test_title_cleanup_in_translate_panel(self):
        """Panel titles with template variables should be cleaned."""
        from observability_migration.targets.kibana.emit.display import clean_template_variables
        self.assertEqual(
            clean_template_variables("NGINX Status for $instance"),
            "NGINX Status",
        )
        self.assertEqual(
            clean_template_variables("Network traffic per $device"),
            "Network traffic",
        )


class TestDisplayUnitMapping(unittest.TestCase):
    """Exhaustive coverage of the Grafana unit → YAML format map."""

    def test_all_byte_units(self):
        from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
        for unit in ("bytes", "decbytes", "kbytes", "mbytes", "gbytes"):
            fmt = grafana_unit_to_yaml_format(unit)
            self.assertEqual(fmt["type"], "bytes", f"Failed for {unit}")

    def test_rate_units_have_suffix(self):
        from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
        for unit in ("Bps", "bps", "KBs", "MBs", "GBs"):
            fmt = grafana_unit_to_yaml_format(unit)
            self.assertIn("suffix", fmt, f"Missing suffix for {unit}")
            self.assertIn("/s", fmt["suffix"])

    def test_time_units(self):
        from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
        self.assertEqual(grafana_unit_to_yaml_format("s")["type"], "duration")
        self.assertEqual(grafana_unit_to_yaml_format("ms")["suffix"], " ms")
        self.assertEqual(grafana_unit_to_yaml_format("µs")["suffix"], " µs")
        self.assertEqual(grafana_unit_to_yaml_format("ns")["suffix"], " ns")

    def test_domain_specific_suffixes(self):
        from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
        self.assertIn("iops", grafana_unit_to_yaml_format("iops")["suffix"])
        self.assertIn("Hz", grafana_unit_to_yaml_format("hertz")["suffix"])
        self.assertIn("°C", grafana_unit_to_yaml_format("celsius")["suffix"])


class TestPanelTypeAndSchemaCoverage(unittest.TestCase):
    """Comprehensive coverage for all Grafana panel types, schema versions,
    layout variants, and display enrichment through the full translate_panel
    and translate_dashboard pipelines.

    Gaps filled:
    - timeseries, singlestat, logs, heatmap, piechart, barchart panel types
    - Legacy rows + span layout (pre-schemaVersion 16)
    - Row panels with collapsed children
    - gridData fallback, missing gridPos defaults
    - Datatable min-height normalization
    - Display integration through translate_panel
    - Section grouping in translate_dashboard
    """

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def translate_panel(self, panel):
        return migrate.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    # ------------------------------------------------------------------
    # Panel type coverage
    # ------------------------------------------------------------------

    def test_timeseries_panel_maps_to_line(self):
        panel = {
            "id": 1,
            "type": "timeseries",
            "title": "CPU Over Time",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "rate(node_cpu_seconds_total[5m])", "refId": "A"}],
            "fieldConfig": {"defaults": {"unit": "percent"}},
            "options": {"legend": {"showLegend": True, "placement": "bottom"}},
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.grafana_type, "timeseries")
        self.assertEqual(result.kibana_type, "line")
        self.assertIn("type", yaml_panel.get("esql", {}))
        self.assertEqual(yaml_panel["esql"]["type"], "line")

    def test_singlestat_panel_maps_to_metric(self):
        panel = {
            "id": 2,
            "type": "singlestat",
            "title": "Total Requests",
            "gridPos": {"x": 0, "y": 0, "w": 6, "h": 4},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "sum(http_requests_total)", "refId": "A", "instant": True}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.grafana_type, "singlestat")
        self.assertEqual(result.kibana_type, "metric")
        self.assertEqual(yaml_panel["esql"]["type"], "metric")

    def test_native_promql_xy_generic_value_metric_uses_panel_title_label(self):
        rule_pack = migrate.RulePackConfig()
        rule_pack.native_promql = True
        resolver = migrate.SchemaResolver(rule_pack)
        panel = {
            "id": 22,
            "type": "graph",
            "title": "Head chunks count",
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{
                "expr": 'sum(prometheus_tsdb_head_chunks{instance=~".*"}) by (instance)',
                "refId": "A",
            }],
            "legend": {"show": False},
        }
        yaml_panel, _result = migrate.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(yaml_panel["esql"]["type"], "line")
        self.assertEqual(yaml_panel["esql"]["metrics"][0]["field"], "value")
        self.assertEqual(yaml_panel["esql"]["metrics"][0]["label"], "Head chunks count")

    def test_heatmap_panel_falls_back_to_line(self):
        panel = {
            "id": 3,
            "type": "heatmap",
            "title": "Latency Heatmap",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "sum(rate(http_request_duration_seconds_bucket[5m])) by (le)", "refId": "A"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.grafana_type, "heatmap")
        self.assertEqual(result.kibana_type, "heatmap")
        esql_type = yaml_panel.get("esql", {}).get("type", "")
        self.assertEqual(esql_type, "line", "heatmap should fall back to line chart via fallback rule")
        self.assertTrue(
            any("Approximated" in r or "no direct" in r for r in result.reasons),
            f"Expected fallback warning, got {result.reasons}",
        )

    def test_piechart_panel_maps_to_pie(self):
        panel = {
            "id": 4,
            "type": "piechart",
            "title": "Traffic Distribution",
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "sum(rate(http_requests_total[5m])) by (handler)", "refId": "A"}],
            "options": {
                "legend": {"displayMode": "table", "placement": "right", "showLegend": True},
                "pieType": "pie",
            },
            "fieldConfig": {"defaults": {"unit": "reqps"}},
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.grafana_type, "piechart")
        self.assertEqual(result.kibana_type, "pie")
        self.assertEqual(yaml_panel["esql"]["type"], "pie")
        self.assertIn("breakdowns", yaml_panel["esql"])

    def test_barchart_panel_maps_to_bar(self):
        panel = {
            "id": 5,
            "type": "barchart",
            "title": "Top Endpoints",
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "sum(rate(http_requests_total[5m])) by (handler)", "refId": "A"}],
            "options": {"orientation": "horizontal"},
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.grafana_type, "barchart")
        self.assertEqual(result.kibana_type, "bar")
        self.assertEqual(yaml_panel["esql"]["type"], "bar")

    def test_logs_panel_maps_to_datatable(self):
        panel = {
            "id": 6,
            "type": "logs",
            "title": "App Errors",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 6},
            "datasource": {"type": "loki", "uid": "loki"},
            "targets": [{"expr": '{job="app"} |= "error"', "refId": "A"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.grafana_type, "logs")
        self.assertIn(result.kibana_type, ("datatable", "markdown"),
                       "logs panels should be datatable or fall back to markdown for untranslatable LogQL")

    def test_unknown_panel_type_is_not_feasible(self):
        panel = {
            "id": 7,
            "type": "flamegraph",
            "title": "CPU Flame",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "targets": [{"expr": "up", "refId": "A"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNone(yaml_panel)
        self.assertEqual(result.status, "not_feasible")
        self.assertTrue(any("Unknown" in r for r in result.reasons))

    def test_no_promql_targets_produces_placeholder(self):
        panel = {
            "id": 8,
            "type": "graph",
            "title": "Empty Panel",
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 6},
            "targets": [],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.status, "requires_manual")
        self.assertIn("markdown", yaml_panel)

    # ------------------------------------------------------------------
    # Layout / grid handling
    # ------------------------------------------------------------------

    def test_griddata_fallback_is_used_when_no_gridpos(self):
        panel = {
            "id": 9,
            "type": "stat",
            "title": "Using gridData",
            "gridData": {"x": 6, "y": 10, "w": 12, "h": 5},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "up", "refId": "A", "instant": True}],
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(yaml_panel["_grafana_row_y"], 10)
        self.assertEqual(yaml_panel["_grafana_row_x"], 6)

    def test_missing_gridpos_uses_full_width_defaults(self):
        panel = {
            "id": 10,
            "type": "graph",
            "title": "No Grid Info",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "rate(foo_total[5m])", "refId": "A"}],
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(yaml_panel["size"]["w"], panels.KIBANA_GRID_COLS)
        self.assertEqual(yaml_panel["position"]["x"], 0)
        self.assertEqual(yaml_panel["position"]["y"], 0)

    def test_datatable_min_height_is_enforced(self):
        dashboard = {
            "title": "Min Height Test",
            "uid": "min-h-1",
            "panels": [
                {
                    "id": 11,
                    "type": "table",
                    "title": "Short Table",
                    "gridPos": {"x": 0, "y": 0, "w": 24, "h": 3},
                    "datasource": {"type": "elasticsearch", "uid": "es"},
                    "targets": [{"query": "FROM logs-* | LIMIT 10", "refId": "A"}],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                dashboard, tmpdir, datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        top_panels = yaml_doc["dashboards"][0]["panels"]
        table_panel = top_panels[0]
        self.assertGreaterEqual(
            table_panel["size"]["h"],
            panels.MIN_DATATABLE_HEIGHT,
            "datatable panels should enforce minimum height via _normalize_tile_size",
        )

    # ------------------------------------------------------------------
    # Legacy rows layout (pre-schemaVersion 16)
    # ------------------------------------------------------------------

    def test_legacy_rows_dashboard_translates_with_sections(self):
        dashboard = {
            "title": "Legacy Rows Dashboard",
            "uid": "legacy-rows-1",
            "schemaVersion": 14,
            "rows": [
                {
                    "title": "Overview",
                    "height": "250px",
                    "panels": [
                        {
                            "id": 1,
                            "type": "singlestat",
                            "title": "Uptime",
                            "span": 4,
                            "datasource": {"type": "prometheus", "uid": "prom"},
                            "targets": [{"expr": "up", "refId": "A", "instant": True}],
                        },
                        {
                            "id": 2,
                            "type": "singlestat",
                            "title": "CPU",
                            "span": 4,
                            "datasource": {"type": "prometheus", "uid": "prom"},
                            "targets": [{"expr": "avg(rate(node_cpu_seconds_total[5m]))", "refId": "A", "instant": True}],
                        },
                        {
                            "id": 3,
                            "type": "singlestat",
                            "title": "Memory",
                            "span": 4,
                            "datasource": {"type": "prometheus", "uid": "prom"},
                            "targets": [{"expr": "node_memory_MemAvailable_bytes", "refId": "A", "instant": True}],
                        },
                    ],
                },
                {
                    "title": "Graphs",
                    "height": 300,
                    "panels": [
                        {
                            "id": 4,
                            "type": "graph",
                            "title": "Network I/O",
                            "span": 12,
                            "datasource": {"type": "prometheus", "uid": "prom"},
                            "targets": [{"expr": "rate(node_network_receive_bytes_total[5m])", "refId": "A"}],
                        },
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                dashboard, tmpdir, datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )

            self.assertTrue(path.exists())
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)

        dashboard_panels = yaml_doc["dashboards"][0]["panels"]
        sections = [p for p in dashboard_panels if "section" in p]
        flat_panels = [p for p in dashboard_panels if "section" not in p]
        self.assertEqual(len(sections), 1, "Only the multi-panel legacy row should remain a section")
        self.assertEqual(len(flat_panels), 1, "Single-panel legacy rows should flatten to top-level panels")

        overview_section = sections[0]
        self.assertEqual(overview_section["title"], "Overview")
        inner_panels = overview_section["section"]["panels"]
        self.assertEqual(len(inner_panels), 3)

        for p in inner_panels:
            self.assertIn("position", p)
            self.assertIn("size", p)
            self.assertGreater(p["size"]["w"], 0, "Span-derived width should be positive")
        self.assertEqual(flat_panels[0]["title"], "Network I/O")

    def test_legacy_row_span_computes_correct_gridpos(self):
        dashboard = {
            "title": "Span Test",
            "uid": "span-test-1",
            "rows": [
                {
                    "title": "Row A",
                    "height": 300,
                    "panels": [
                        {"id": 1, "type": "graph", "title": "Left", "span": 6,
                         "datasource": {"type": "prometheus", "uid": "p"},
                         "targets": [{"expr": "up", "refId": "A"}]},
                        {"id": 2, "type": "graph", "title": "Right", "span": 6,
                         "datasource": {"type": "prometheus", "uid": "p"},
                         "targets": [{"expr": "up", "refId": "A"}]},
                    ],
                }
            ],
        }
        groups = panels._build_section_groups(dashboard)
        self.assertEqual(len(groups), 1)
        _, group_panels = groups[0]
        self.assertEqual(len(group_panels), 2)

        left = group_panels[0]
        right = group_panels[1]
        self.assertEqual(left["gridPos"]["x"], 0)
        self.assertEqual(left["gridPos"]["w"], 12)
        self.assertEqual(right["gridPos"]["x"], 12)
        self.assertEqual(right["gridPos"]["w"], 12)

    def test_legacy_row_height_string_parsed_correctly(self):
        dashboard = {
            "title": "Height Parse",
            "uid": "height-1",
            "rows": [
                {
                    "title": "Tall Row",
                    "height": "300px",
                    "panels": [
                        {"id": 1, "type": "text", "title": "Note", "span": 12,
                         "content": "hello", "options": {"mode": "markdown", "content": "hello"}},
                    ],
                }
            ],
        }
        groups = panels._build_section_groups(dashboard)
        _, group_panels = groups[0]
        grid_h = group_panels[0]["gridPos"]["h"]
        self.assertEqual(grid_h, 10, "300px / 30 = 10 grid units")

    def test_legacy_single_panel_rows_flatten_without_overlap(self):
        dashboard = {
            "title": "Flatten Legacy Rows",
            "uid": "flatten-rows-1",
            "rows": [
                {
                    "title": "CPU",
                    "height": "250px",
                    "panels": [
                        {
                            "id": 1,
                            "type": "graph",
                            "title": "CPU Usage",
                            "span": 12,
                            "datasource": {"type": "prometheus", "uid": "p"},
                            "targets": [{"expr": "rate(node_cpu_seconds_total[5m])", "refId": "A"}],
                        }
                    ],
                },
                {
                    "title": "Memory",
                    "height": "250px",
                    "panels": [
                        {
                            "id": 2,
                            "type": "graph",
                            "title": "Memory Usage",
                            "span": 12,
                            "datasource": {"type": "prometheus", "uid": "p"},
                            "targets": [{"expr": "node_memory_MemAvailable_bytes", "refId": "A"}],
                        }
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, path = migrate.translate_dashboard(
                dashboard, tmpdir, datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)

        self.assertEqual(result.total_panels, 2)
        top_panels = yaml_doc["dashboards"][0]["panels"]
        self.assertEqual(len(top_panels), 2)
        self.assertFalse(any("section" in panel for panel in top_panels))
        self.assertEqual(top_panels[0]["title"], "CPU Usage")
        self.assertEqual(top_panels[1]["title"], "Memory Usage")
        self.assertGreater(top_panels[1]["position"]["y"], top_panels[0]["position"]["y"])

    def test_decorative_repeat_header_text_panel_is_dropped(self):
        dashboard = {
            "title": "Repeat Header",
            "uid": "repeat-header-1",
            "rows": [
                {
                    "title": "Title",
                    "height": "25px",
                    "panels": [
                        {
                            "id": 1,
                            "type": "text",
                            "title": "$node",
                            "repeat": "node",
                            "mode": "html",
                            "content": "",
                            "span": 12,
                        }
                    ],
                },
                {
                    "title": "CPU",
                    "height": "250px",
                    "panels": [
                        {
                            "id": 2,
                            "type": "singlestat",
                            "title": "CPU Cores",
                            "repeat": "node",
                            "span": 12,
                            "datasource": {"type": "prometheus", "uid": "p"},
                            "targets": [{
                                "expr": 'count(node_cpu_seconds_total{instance=~"$node", mode="system"})',
                                "refId": "A",
                                "instant": True,
                            }],
                        }
                    ],
                },
            ],
            "templating": {
                "list": [
                    {
                        "name": "node",
                        "type": "query",
                        "query": "label_values(up, instance)",
                        "multi": True,
                    }
                ]
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, path = migrate.translate_dashboard(
                dashboard, tmpdir, datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)

        top_panels = yaml_doc["dashboards"][0]["panels"]
        self.assertEqual([panel["title"] for panel in top_panels], ["CPU Cores"])
        self.assertNotIn("hide_title", top_panels[0], "Flattened legacy metric panels should keep visible titles")
        self.assertNotIn("label", top_panels[0]["esql"]["primary"], "Panel header should carry the title after restoration")
        self.assertGreaterEqual(result.skipped, 1)
        self.assertIn(
            "$node",
            [panel_result.title for panel_result in result.panel_results if panel_result.status == "skipped"],
        )
        controls = yaml_doc["dashboards"][0]["controls"]
        self.assertFalse(controls[0]["multiple"])

    # ------------------------------------------------------------------
    # Modern row panels with collapsed children
    # ------------------------------------------------------------------

    def test_collapsed_row_children_become_section(self):
        dashboard = {
            "title": "Collapsed Row Test",
            "uid": "collapsed-1",
            "panels": [
                {"id": 1, "type": "stat", "title": "Top Stat",
                 "gridPos": {"x": 0, "y": 0, "w": 12, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A", "instant": True}]},
                {"id": 2, "type": "row", "title": "System Metrics",
                 "collapsed": True,
                 "gridPos": {"x": 0, "y": 4, "w": 24, "h": 1},
                 "panels": [
                     {"id": 3, "type": "graph", "title": "CPU",
                      "gridPos": {"x": 0, "y": 5, "w": 24, "h": 8},
                      "datasource": {"type": "prometheus", "uid": "p"},
                      "targets": [{"expr": "rate(node_cpu_seconds_total[5m])", "refId": "A"}]},
                 ]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                dashboard, tmpdir, datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )

            with open(path) as f:
                yaml_doc = yaml.safe_load(f)

        top_panels = yaml_doc["dashboards"][0]["panels"]
        sections = [p for p in top_panels if "section" in p]
        flat_panels = [p for p in top_panels if "section" not in p]

        self.assertEqual(len(flat_panels), 1, "Top stat should be a flat panel")
        self.assertEqual(len(sections), 1, "Collapsed row should become a section")
        self.assertEqual(sections[0]["title"], "System Metrics")
        self.assertGreaterEqual(len(sections[0]["section"]["panels"]), 1)

    # ------------------------------------------------------------------
    # Display integration through translate_panel
    # ------------------------------------------------------------------

    def test_translate_panel_includes_unit_format(self):
        panel = {
            "id": 20,
            "type": "graph",
            "title": "Network Bytes",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "rate(node_network_receive_bytes_total[5m])", "refId": "A"}],
            "fieldConfig": {"defaults": {"unit": "Bps"}},
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel.get("esql", {})
        metrics = esql.get("metrics", [])
        if metrics:
            self.assertIn("format", metrics[0], "Unit should propagate into metrics[0].format")
            self.assertEqual(metrics[0]["format"]["type"], "bytes")

    def test_translate_panel_includes_legend_for_graph(self):
        panel = {
            "id": 21,
            "type": "graph",
            "title": "CPU",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "rate(node_cpu_seconds_total[5m])", "refId": "A"}],
            "legend": {"show": True, "rightSide": True},
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel.get("esql", {})
        if esql.get("type") in ("line", "bar", "area"):
            self.assertIn("legend", esql, "Legend config should be enriched from Grafana panel")
            self.assertEqual(esql["legend"]["position"], "right")

    def test_translate_panel_axis_label_from_legacy_yaxes(self):
        panel = {
            "id": 22,
            "type": "graph",
            "title": "Requests",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "rate(http_requests_total[5m])", "refId": "A"}],
            "yaxes": [{"label": "req/s", "format": "reqps"}, {"format": "short"}],
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel.get("esql", {})
        appearance = esql.get("appearance", {})
        if appearance:
            self.assertEqual(appearance.get("y_left_axis", {}).get("title"), "req/s")

    # ------------------------------------------------------------------
    # Gauge color/threshold integration
    # ------------------------------------------------------------------

    def test_gauge_thresholds_and_range_propagate(self):
        panel = {
            "id": 30,
            "type": "gauge",
            "title": "Disk Usage",
            "gridPos": {"x": 0, "y": 0, "w": 6, "h": 6},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "100 - (node_filesystem_avail_bytes / node_filesystem_size_bytes) * 100", "refId": "A", "instant": True}],
            "fieldConfig": {
                "defaults": {
                    "unit": "percent",
                    "min": 0,
                    "max": 100,
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "green", "value": None},
                            {"color": "yellow", "value": 70},
                            {"color": "red", "value": 90},
                        ],
                    },
                }
            },
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel.get("esql", {})
        self.assertEqual(esql.get("type"), "gauge")

        self.assertIn("_gauge_min", esql.get("query", ""))
        self.assertIn("_gauge_max", esql.get("query", ""))

        color = esql.get("color", {})
        if color:
            self.assertIn("thresholds", color)
            self.assertGreater(len(color["thresholds"]), 0)

    def test_gauge_thresholds_above_max_are_clamped_to_sorted_range(self):
        panel = {
            "id": 31,
            "type": "gauge",
            "title": "CPU Time",
            "gridPos": {"x": 0, "y": 0, "w": 6, "h": 6},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": 'sum(rate(process_cpu_seconds_total{instance=~"$instance"}[5m]))', "refId": "A", "instant": True}],
            "fieldConfig": {
                "defaults": {
                    "unit": "s",
                    "min": 0,
                    "max": 0.03,
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "green", "value": None},
                            {"color": "yellow", "value": 60},
                            {"color": "red", "value": 85},
                        ],
                    },
                }
            },
        }
        yaml_panel, _result = self.translate_panel(panel)

        color = yaml_panel["esql"].get("color", {})
        thresholds = color.get("thresholds", [])
        threshold_values = [item["up_to"] for item in thresholds]

        self.assertEqual(threshold_values, sorted(threshold_values))
        self.assertEqual(thresholds, [{"up_to": 0.03, "color": "#54B399"}])

    # ------------------------------------------------------------------
    # Pie chart display enrichment
    # ------------------------------------------------------------------

    def test_piechart_legend_propagates(self):
        panel = {
            "id": 31,
            "type": "piechart",
            "title": "Distribution",
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "sum(rate(foo[5m])) by (bar)", "refId": "A"}],
            "options": {"legend": {"showLegend": True, "placement": "right"}},
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel.get("esql", {})
        if esql.get("type") == "pie":
            legend = esql.get("legend", {})
            self.assertEqual(legend.get("visible"), "show")

    # ------------------------------------------------------------------
    # Section grouping edge cases
    # ------------------------------------------------------------------

    def test_flat_dashboard_has_no_sections(self):
        dashboard = {
            "title": "Flat Dashboard",
            "uid": "flat-1",
            "panels": [
                {"id": 1, "type": "stat", "title": "A",
                 "gridPos": {"x": 0, "y": 0, "w": 12, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A", "instant": True}]},
                {"id": 2, "type": "graph", "title": "B",
                 "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "rate(foo_total[5m])", "refId": "A"}]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                dashboard, tmpdir, datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)

        top_panels = yaml_doc["dashboards"][0]["panels"]
        sections = [p for p in top_panels if "section" in p]
        self.assertEqual(len(sections), 0, "Flat dashboard should have no sections")

    def test_multiple_row_panels_create_multiple_sections(self):
        dashboard = {
            "title": "Multi Section",
            "uid": "multi-sec-1",
            "panels": [
                {"id": 1, "type": "row", "title": "Section A",
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}, "panels": []},
                {"id": 2, "type": "graph", "title": "Panel A1",
                 "gridPos": {"x": 0, "y": 1, "w": 24, "h": 8},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "rate(node_cpu_seconds_total[5m])", "refId": "A"}]},
                {"id": 3, "type": "row", "title": "Section B",
                 "gridPos": {"x": 0, "y": 9, "w": 24, "h": 1}, "panels": []},
                {"id": 4, "type": "graph", "title": "Panel B1",
                 "gridPos": {"x": 0, "y": 10, "w": 24, "h": 8},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "rate(node_network_receive_bytes_total[5m])", "refId": "A"}]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                dashboard, tmpdir, datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)

        top_panels = yaml_doc["dashboards"][0]["panels"]
        sections = [p for p in top_panels if "section" in p]
        self.assertGreaterEqual(len(sections), 2, "Two row panels should produce two sections")
        titles = {s["title"] for s in sections}
        self.assertIn("Section A", titles)
        self.assertIn("Section B", titles)

    # ------------------------------------------------------------------
    # Grid scaling: 24-col Grafana → 48-col Kibana
    # ------------------------------------------------------------------

    def test_translate_panel_stores_grafana_grid_metadata(self):
        panel = {
            "id": 40,
            "type": "stat",
            "title": "Scaling Test",
            "gridPos": {"x": 6, "y": 5, "w": 8, "h": 4},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "up", "refId": "A", "instant": True}],
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(yaml_panel["_grafana_row_y"], 5)
        self.assertEqual(yaml_panel["_grafana_row_x"], 6)

    def test_full_width_panel_clamps_to_48(self):
        panel = {
            "id": 41,
            "type": "graph",
            "title": "Full Width",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "rate(foo[5m])", "refId": "A"}],
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(yaml_panel["size"]["w"], 48)

    def test_kibana_native_layout_sets_type_based_height(self):
        """After Kibana-native layout, height is determined by panel type."""
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout
        panels = [
            {"title": "Chart", "esql": {"type": "line"}, "size": {"w": 48, "h": 8}, "position": {"x": 0, "y": 0}, "_grafana_row_y": 0, "_grafana_row_x": 0},
            {"title": "Metric", "esql": {"type": "metric"}, "size": {"w": 48, "h": 8}, "position": {"x": 0, "y": 0}, "_grafana_row_y": 5, "_grafana_row_x": 0},
        ]
        result = _apply_kibana_native_layout(panels)
        self.assertEqual(result[0]["size"]["h"], 12, "line chart should be h=12")
        self.assertEqual(result[1]["size"]["h"], 5, "metric should be h=5")

    # ------------------------------------------------------------------
    # Text panel handling
    # ------------------------------------------------------------------

    def test_text_panel_legacy_content_field(self):
        panel = {
            "id": 50,
            "type": "text",
            "title": "Legacy Text",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4},
            "content": "# Hello World\n\nThis is a legacy text panel.",
            "options": {},
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertIn("markdown", yaml_panel)
        self.assertIn("Hello World", yaml_panel["markdown"]["content"])

    def test_text_panel_modern_options_content(self):
        panel = {
            "id": 51,
            "type": "text",
            "title": "Modern Text",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4},
            "options": {"mode": "markdown", "content": "## Status\n\nAll systems go."},
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertIn("markdown", yaml_panel)
        self.assertIn("Status", yaml_panel["markdown"]["content"])

    def test_empty_title_text_panel_hides_title(self):
        panel = {
            "id": 52,
            "type": "text",
            "title": "",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4},
            "options": {"mode": "markdown", "content": "[Docs](https://example.com)"},
        }
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertTrue(yaml_panel["hide_title"])
        self.assertEqual(yaml_panel["title"], "Untitled")

    # ------------------------------------------------------------------
    # Templating / variables through translate_dashboard
    # ------------------------------------------------------------------

    def test_dashboard_variables_become_controls(self):
        dashboard = {
            "title": "Variables Dashboard",
            "uid": "vars-1",
            "panels": [
                {"id": 1, "type": "graph", "title": "Metric",
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
                 "datasource": {"type": "prometheus", "uid": "prom"},
                 "targets": [{"expr": 'rate(http_requests_total{instance=~"$instance"}[5m])', "refId": "A"}]},
            ],
            "templating": {
                "list": [
                    {
                        "name": "instance",
                        "type": "query",
                        "label": "Instance",
                        "query": "label_values(up, instance)",
                        "datasource": {"type": "prometheus", "uid": "prom"},
                    },
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                dashboard, tmpdir, datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        controls = yaml_doc["dashboards"][0].get("controls", [])
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0]["label"], "Instance")
        self.assertTrue(
            controls[0]["field"],
            "Field should be resolved (may be mapped from 'instance' to ES equivalent)",
        )

    # ------------------------------------------------------------------
    # Inventory counts
    # ------------------------------------------------------------------

    def test_dashboard_result_counts_all_panel_types(self):
        dashboard = {
            "title": "Count Test",
            "uid": "count-1",
            "panels": [
                {"id": 1, "type": "row", "title": "Group", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}, "panels": []},
                {"id": 2, "type": "graph", "title": "P1",
                 "gridPos": {"x": 0, "y": 1, "w": 24, "h": 8},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "rate(a[5m])", "refId": "A"}]},
                {"id": 3, "type": "text", "title": "Note",
                 "gridPos": {"x": 0, "y": 9, "w": 24, "h": 4},
                 "options": {"mode": "markdown", "content": "Hello"}},
                {"id": 4, "type": "news", "title": "Feed",
                 "gridPos": {"x": 0, "y": 13, "w": 24, "h": 4}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _path = migrate.translate_dashboard(
                dashboard, tmpdir, datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
        self.assertEqual(result.total_panels, 4)
        self.assertGreaterEqual(result.skipped, 2, "row + news should be skipped")
        self.assertGreaterEqual(result.migrated, 1, "graph or text should be migrated")


class GraphPanelChartStyleTests(unittest.TestCase):
    """Tests for legacy Grafana ``graph`` panel bar/line detection."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def translate_panel(self, panel):
        return migrate.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    def test_graph_panel_with_bars_true_maps_to_bar(self):
        panel = {
            "id": 1,
            "type": "graph",
            "title": "Request Count",
            "bars": True,
            "lines": False,
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "sum(rate(http_requests_total[5m]))", "refId": "A"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.kibana_type, "bar")
        self.assertEqual(yaml_panel["esql"]["type"], "bar")

    def test_graph_panel_with_lines_true_maps_to_line(self):
        panel = {
            "id": 2,
            "type": "graph",
            "title": "CPU Usage",
            "bars": False,
            "lines": True,
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "rate(node_cpu_seconds_total[5m])", "refId": "A"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.kibana_type, "line")
        self.assertEqual(yaml_panel["esql"]["type"], "line")

    def test_graph_panel_default_style_is_line(self):
        panel = {
            "id": 3,
            "type": "graph",
            "title": "Traffic",
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "rate(net_bytes_total[5m])", "refId": "A"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.kibana_type, "line")

    def test_graph_panel_both_bars_and_lines_stays_line(self):
        panel = {
            "id": 4,
            "type": "graph",
            "title": "Mixed",
            "bars": True,
            "lines": True,
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "up", "refId": "A"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertIsNotNone(yaml_panel)
        self.assertEqual(result.kibana_type, "line",
                         "When both bars and lines are true, line takes precedence")


class LogQLTitleTests(unittest.TestCase):
    """Tests for LogQL-aware panel title coalescing."""

    def test_count_over_time_gets_log_volume_title(self):
        panel = {
            "title": "",
            "type": "graph",
            "targets": [
                {"expr": 'sum(count_over_time({namespace="default"}[5m]))', "refId": "A"}
            ],
        }
        title = panels._coalesce_panel_title(panel)
        self.assertEqual(title, "Log Volume")

    def test_logql_stream_selector_gets_log_events_title(self):
        panel = {
            "title": "",
            "type": "logs",
            "targets": [
                {"expr": '{namespace="default"} |~ "error"', "refId": "A"}
            ],
        }
        title = panels._coalesce_panel_title(panel)
        self.assertEqual(title, "Log Events")

    def test_bytes_over_time_gets_log_bytes_title(self):
        panel = {
            "title": "",
            "type": "graph",
            "targets": [
                {"expr": 'sum(bytes_over_time({job="api"}[5m]))', "refId": "A"}
            ],
        }
        title = panels._coalesce_panel_title(panel)
        self.assertEqual(title, "Log Bytes")

    def test_logql_rate_gets_log_rate_title(self):
        panel = {
            "title": "",
            "type": "graph",
            "targets": [
                {"expr": 'sum(rate({job="api"} |= "error" [5m]))', "refId": "A"}
            ],
        }
        title = panels._coalesce_panel_title(panel)
        self.assertEqual(title, "Log Rate")

    def test_explicit_title_is_preserved(self):
        panel = {
            "title": "My Custom Title",
            "type": "graph",
            "targets": [
                {"expr": 'sum(count_over_time({job="api"}[5m]))', "refId": "A"}
            ],
        }
        title = panels._coalesce_panel_title(panel)
        self.assertEqual(title, "My Custom Title")

    def test_agg_function_only_does_not_become_title(self):
        panel = {
            "title": "",
            "type": "graph",
            "targets": [
                {"expr": 'sum(rate(http_requests_total[5m]))', "refId": "A"}
            ],
        }
        title = panels._coalesce_panel_title(panel)
        self.assertNotEqual(title, "Sum",
                            "Bare aggregation function name should not become a panel title")


class TextboxVariableTests(unittest.TestCase):
    """Tests for textbox and interval variable translation."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()

    def test_textbox_variable_is_handled_not_dropped(self):
        variables = [
            {"name": "search", "type": "textbox", "query": "level=warn"},
        ]
        controls = migrate.translate_variables(variables, "logs-*", rule_pack=self.rule_pack)
        self.assertEqual(len(controls), 0,
                         "Textbox variables should not produce Kibana controls")

    def test_textbox_among_query_variables(self):
        variables = [
            {
                "name": "namespace",
                "type": "query",
                "query": "label_values(kube_pod_info, namespace)",
                "definition": "label_values(kube_pod_info, namespace)",
            },
            {"name": "search", "type": "textbox", "query": "level=warn"},
        ]
        controls = migrate.translate_variables(
            variables, "logs-*", rule_pack=self.rule_pack,
        )
        self.assertEqual(len(controls), 1,
                         "Only the query variable should produce a control")
        self.assertIn("namespace", controls[0]["field"].lower().replace(".", "_"),
                       "namespace variable should resolve to a namespace-related field")

    def test_interval_variable_is_skipped(self):
        variables = [
            {"name": "interval", "type": "interval", "query": "1m,5m,15m,30m,1h"},
        ]
        controls = migrate.translate_variables(variables, "metrics-*", rule_pack=self.rule_pack)
        self.assertEqual(len(controls), 0)

    def test_custom_variable_is_skipped(self):
        variables = [
            {"name": "resolution", "type": "custom", "query": "1,2,5,10"},
        ]
        controls = migrate.translate_variables(variables, "metrics-*", rule_pack=self.rule_pack)
        self.assertEqual(len(controls), 0)


class LokiDashboardIntegrationTests(unittest.TestCase):
    """End-to-end tests for a synthetic LogQL dashboard migration."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)
        self.dashboard = {
            "title": "Synthetic Loki Log Search",
            "uid": "synthetic-logql-coverage",
            "description": "Synthetic LogQL fixture with Prometheus-backed variables",
            "panels": [
                {
                    "id": 6,
                    "type": "graph",
                    "title": "",
                    "bars": True,
                    "lines": False,
                    "gridPos": {"h": 3, "w": 24, "x": 0, "y": 0},
                    "datasource": "${DS_LOKI}",
                    "legend": {
                        "avg": False, "current": False, "max": False,
                        "min": False, "show": False, "total": False,
                        "values": False,
                    },
                    "targets": [
                        {
                            "expr": 'sum(count_over_time({namespace="$namespace", instance=~"$pod"} |~ "$search"[$__interval]))',
                            "refId": "A",
                        }
                    ],
                },
                {
                    "id": 2,
                    "type": "logs",
                    "title": "Logs Panel",
                    "gridPos": {"h": 25, "w": 24, "x": 0, "y": 3},
                    "datasource": "${DS_LOKI}",
                    "options": {
                        "showLabels": False,
                        "showTime": True,
                        "sortOrder": "Descending",
                        "wrapLogMessage": True,
                    },
                    "targets": [
                        {
                            "expr": '{namespace="$namespace", instance=~"$pod"} |~ "$search"',
                            "refId": "A",
                        }
                    ],
                },
                {
                    "id": 4,
                    "type": "text",
                    "title": "",
                    "content": "<div style=\"text-align:center\"> Synthetic log search example </div>",
                    "mode": "html",
                    "gridPos": {"h": 3, "w": 24, "x": 0, "y": 28},
                },
            ],
            "templating": {
                "list": [
                    {
                        "name": "namespace",
                        "type": "query",
                        "definition": "label_values(kube_pod_info, namespace)",
                        "query": "label_values(kube_pod_info, namespace)",
                        "datasource": "${DS_PROMETHEUS}",
                        "hide": 0,
                    },
                    {
                        "name": "pod",
                        "type": "query",
                        "definition": "label_values(container_network_receive_bytes_total{namespace=~\"$namespace\"},pod)",
                        "query": "label_values(container_network_receive_bytes_total{namespace=~\"$namespace\"},pod)",
                        "datasource": "${DS_PROMETHEUS}",
                        "hide": 0,
                        "includeAll": True,
                    },
                    {
                        "name": "search",
                        "type": "textbox",
                        "query": "level=warn",
                        "hide": 0,
                    },
                ],
            },
        }

    def test_log_volume_panel_is_bar_chart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                self.dashboard, tmpdir,
                datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        dash = yaml_doc["dashboards"][0]
        log_volume = dash["panels"][0]
        self.assertEqual(log_volume["esql"]["type"], "bar",
                         "Log volume graph panel with bars:true should become a bar chart")

    def test_log_volume_panel_title_is_log_volume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                self.dashboard, tmpdir,
                datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        dash = yaml_doc["dashboards"][0]
        log_volume = dash["panels"][0]
        self.assertEqual(log_volume["title"], "Log Volume")

    def test_log_volume_panel_legend_hidden(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                self.dashboard, tmpdir,
                datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        dash = yaml_doc["dashboards"][0]
        log_volume = dash["panels"][0]
        legend = log_volume["esql"].get("legend", {})
        self.assertEqual(legend.get("visible"), "hide",
                         "Legend should be hidden matching Grafana's legend.show=false")

    def test_logs_panel_is_datatable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                self.dashboard, tmpdir,
                datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        dash = yaml_doc["dashboards"][0]
        logs_panel = dash["panels"][1]
        self.assertEqual(logs_panel["title"], "Logs Panel")
        self.assertEqual(logs_panel["esql"]["type"], "datatable")
        self.assertIn("SORT @timestamp DESC", logs_panel["esql"]["query"])

    def test_text_panel_preserves_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                self.dashboard, tmpdir,
                datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        dash = yaml_doc["dashboards"][0]
        text_panel = dash["panels"][2]
        self.assertIn("markdown", text_panel)
        self.assertIn("Synthetic log search example", text_panel["markdown"]["content"])

    def test_controls_generated_for_query_variables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                self.dashboard, tmpdir,
                datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        dash = yaml_doc["dashboards"][0]
        controls = dash.get("controls", [])
        control_fields = [c.get("field", "") for c in controls]
        has_namespace = any("namespace" in f for f in control_fields)
        has_pod = any("pod" in f for f in control_fields)
        self.assertTrue(has_namespace,
                        f"Expected a namespace control, got fields: {control_fields}")
        self.assertTrue(has_pod,
                        f"Expected a pod control, got fields: {control_fields}")

    def test_layout_preserves_full_width(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _result, path = migrate.translate_dashboard(
                self.dashboard, tmpdir,
                datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        dash = yaml_doc["dashboards"][0]
        for panel in dash["panels"]:
            self.assertEqual(panel["size"]["w"], 48,
                             f"Panel '{panel['title']}' should be full width (48 cols)")

    def test_panel_count_matches_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _path = migrate.translate_dashboard(
                self.dashboard, tmpdir,
                datasource_index="metrics-*", esql_index="metrics-*",
                rule_pack=self.rule_pack, resolver=self.resolver,
            )
        self.assertEqual(result.total_panels, 3)
        self.assertEqual(result.skipped, 0)


class StackingAndDrawStyleTests(unittest.TestCase):
    """Tests for stacking → area and drawStyle → bar detection."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def translate_panel(self, panel):
        return migrate.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    def test_timeseries_stacked_normal_maps_to_area(self):
        panel = {
            "id": 1, "type": "timeseries", "title": "Memory Stack",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "fieldConfig": {"defaults": {"custom": {"stacking": {"mode": "normal"}}}},
            "targets": [{"expr": "node_memory_MemTotal_bytes", "refId": "A"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.kibana_type, "area")
        self.assertEqual(yaml_panel["esql"]["type"], "area")

    def test_timeseries_stacked_percent_maps_to_area(self):
        panel = {
            "id": 2, "type": "timeseries", "title": "CPU Percent Stack",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "fieldConfig": {"defaults": {"custom": {"stacking": {"mode": "percent"}}}},
            "targets": [{"expr": "node_cpu_seconds_total", "refId": "A"}],
        }
        _yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.kibana_type, "area")

    def test_timeseries_no_stacking_stays_line(self):
        panel = {
            "id": 3, "type": "timeseries", "title": "Normal Line",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "fieldConfig": {"defaults": {"custom": {"stacking": {"mode": "none"}}}},
            "targets": [{"expr": "up", "refId": "A"}],
        }
        _yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.kibana_type, "line")

    def test_timeseries_drawstyle_bars_maps_to_bar(self):
        panel = {
            "id": 4, "type": "timeseries", "title": "Bar Style",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "fieldConfig": {"defaults": {"custom": {"drawStyle": "bars"}}},
            "targets": [{"expr": "up", "refId": "A"}],
        }
        _yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.kibana_type, "bar")

    def test_legacy_graph_stacked_maps_to_area(self):
        panel = {
            "id": 5, "type": "graph", "title": "Stacked CPU",
            "stack": True, "lines": True,
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "targets": [{"expr": "node_cpu_seconds_total", "refId": "A"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.kibana_type, "area")
        self.assertEqual(yaml_panel["esql"]["type"], "area")

    def test_stacked_bar_stays_bar(self):
        """Bars + stack: bars take priority over stacking in legacy graph."""
        panel = {
            "id": 6, "type": "graph", "title": "Stacked Bars",
            "bars": True, "lines": False, "stack": True,
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "targets": [{"expr": "up", "refId": "A"}],
        }
        _yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.kibana_type, "bar",
                         "bars:true should take priority over stack:true")

    def test_stacking_with_percent_stacking_gets_area(self):
        """Stacking + percent should use area chart."""
        panel = {
            "id": 7, "type": "timeseries", "title": "CPU % Stack",
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
            "fieldConfig": {
                "defaults": {
                    "custom": {
                        "stacking": {"mode": "percent"},
                        "fillOpacity": 80,
                    }
                }
            },
            "targets": [
                {"expr": 'avg(rate(node_cpu_seconds_total{mode="system"}[5m]))', "refId": "A"},
                {"expr": 'avg(rate(node_cpu_seconds_total{mode="user"}[5m]))', "refId": "B"},
            ],
        }
        _yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.kibana_type, "area")


class BargaugeTitleTests(unittest.TestCase):
    """Tests for bargauge panel title coalescing when multiple targets exist."""

    def test_multi_target_bargauge_no_title_gets_summary(self):
        panel = {
            "title": "",
            "type": "bargauge",
            "targets": [
                {"refId": "A", "expr": "cpu_usage", "legendFormat": "CPU Busy"},
                {"refId": "B", "expr": "mem_used", "legendFormat": "Used RAM"},
                {"refId": "C", "expr": "io_wait", "legendFormat": "IO Wait"},
            ],
        }
        title = panels._coalesce_panel_title(panel)
        self.assertEqual(title, "Summary",
                         "Multi-legend bargauge with no title should get 'Summary'")

    def test_single_target_bargauge_uses_legend(self):
        panel = {
            "title": "",
            "type": "bargauge",
            "targets": [
                {"refId": "A", "expr": "cpu_usage", "legendFormat": "CPU Busy"},
            ],
        }
        title = panels._coalesce_panel_title(panel)
        self.assertEqual(title, "CPU Busy")

    def test_multi_target_bargauge_with_explicit_title_preserved(self):
        panel = {
            "title": "Resource Usage",
            "type": "bargauge",
            "targets": [
                {"refId": "A", "expr": "cpu_usage", "legendFormat": "CPU"},
                {"refId": "B", "expr": "mem_used", "legendFormat": "RAM"},
            ],
        }
        title = panels._coalesce_panel_title(panel)
        self.assertEqual(title, "Resource Usage")

    def test_table_old_multi_target_gets_summary(self):
        panel = {
            "title": "",
            "type": "table-old",
            "targets": [
                {"refId": "A", "expr": "metric_a", "legendFormat": "Series A"},
                {"refId": "B", "expr": "metric_b", "legendFormat": "Series B"},
            ],
        }
        title = panels._coalesce_panel_title(panel)
        self.assertEqual(title, "Summary")


class NodeExporterDashboardIntegrationTests(unittest.TestCase):
    """End-to-end tests validating both Node Exporter dashboards."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)
        self.dashboard_dir = pathlib.Path(__file__).resolve().parents[1] / "infra" / "grafana" / "dashboards"

    def _translate_dashboard(self, filename):
        with open(self.dashboard_dir / filename) as f:
            dashboard = json.load(f)
        with tempfile.TemporaryDirectory() as tmpdir:
            result, path = migrate.translate_dashboard(
                dashboard, tmpdir,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        return result, yaml_doc

    def test_node_exporter_full_panel_count(self):
        result, _yaml_doc = self._translate_dashboard("node-exporter-full.json")
        self.assertEqual(result.total_panels, 132)
        self.assertGreater(result.migrated + result.migrated_with_warnings, 90,
                           "Most panels should migrate")

    def test_node_exporter_full_has_stacked_area_panels(self):
        _result, yaml_doc = self._translate_dashboard("node-exporter-full.json")
        area_count = 0
        def count_areas(panels):
            nonlocal area_count
            for p in panels:
                if p.get("esql", {}).get("type") == "area":
                    area_count += 1
                if "section" in p:
                    count_areas(p["section"].get("panels", []))
        count_areas(yaml_doc["dashboards"][0]["panels"])
        self.assertGreaterEqual(area_count, 10,
                                "Node Exporter Full has 12 stacked panels that should become area charts")

    def test_node_exporter_full_has_controls(self):
        _, yaml_doc = self._translate_dashboard("node-exporter-full.json")
        controls = yaml_doc["dashboards"][0].get("controls", [])
        self.assertGreaterEqual(len(controls), 1, "Should have at least job/node controls")

    def test_node_exporter_full_has_gauge_panels(self):
        _, yaml_doc = self._translate_dashboard("node-exporter-full.json")
        gauge_count = 0
        def count_gauges(panels):
            nonlocal gauge_count
            for p in panels:
                if p.get("esql", {}).get("type") == "gauge":
                    gauge_count += 1
                if "section" in p:
                    count_gauges(p["section"].get("panels", []))
        count_gauges(yaml_doc["dashboards"][0]["panels"])
        self.assertGreaterEqual(gauge_count, 4, "Should have gauge panels for CPU/RAM/etc.")

    def test_all_node_exporters_compile_without_error(self):
        """Verify the bundled Node Exporter dashboard produces valid YAML."""
        for filename in ["node-exporter-full.json"]:
            result, yaml_doc = self._translate_dashboard(filename)
            dash = yaml_doc["dashboards"][0]
            self.assertTrue(dash.get("name"), f"{filename}: dashboard should have a name")
            self.assertTrue(dash.get("panels"), f"{filename}: dashboard should have panels")
            self.assertGreater(result.total_panels, 0, f"{filename}: should have panels")


class PrometheusDashboardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.rule_pack.native_promql = True
        self.resolver = migrate.SchemaResolver(self.rule_pack)
        self.dashboard_dir = pathlib.Path(__file__).resolve().parents[1] / "infra" / "grafana" / "dashboards"

    def _translate_dashboard(self, filename):
        with open(self.dashboard_dir / filename) as f:
            dashboard = json.load(f)
        with tempfile.TemporaryDirectory() as tmpdir:
            result, path = migrate.translate_dashboard(
                dashboard, tmpdir,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            with open(path) as f:
                yaml_doc = yaml.safe_load(f)
        return result, yaml_doc

    def _walk_panels(self, panels):
        for panel in panels:
            yield panel
            if "section" in panel:
                yield from self._walk_panels(panel["section"].get("panels", []))

    def test_prometheus_empty_title_text_panels_hide_titles(self):
        _, yaml_doc = self._translate_dashboard("prometheus-all.json")
        markdown_panels = [
            panel for panel in self._walk_panels(yaml_doc["dashboards"][0]["panels"])
            if "markdown" in panel and panel.get("title") == "Untitled"
        ]
        self.assertGreaterEqual(len(markdown_panels), 2)
        self.assertTrue(all(panel.get("hide_title") for panel in markdown_panels))

    def test_prometheus_native_promql_value_metrics_are_relabelled(self):
        _, yaml_doc = self._translate_dashboard("prometheus-all.json")
        panels_by_title = {
            panel.get("title"): panel for panel in self._walk_panels(yaml_doc["dashboards"][0]["panels"])
        }
        head_chunks = panels_by_title["Head chunks count"]
        head_block = panels_by_title["Length of head block"]
        self.assertEqual(head_chunks["esql"]["metrics"][0]["label"], "Head chunk count")
        self.assertEqual(head_block["esql"]["metrics"][0]["label"], "Length of head block")


class KibanaNativeLayoutTests(unittest.TestCase):
    """Tests for the Kibana-native layout algorithm."""

    def test_two_panels_same_row_split_evenly(self):
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout
        panels = [
            {"title": "A", "esql": {"type": "line"}, "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": 0},
            {"title": "B", "esql": {"type": "line"}, "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": 12},
        ]
        _apply_kibana_native_layout(panels)
        self.assertEqual(panels[0]["size"]["w"], 24)
        self.assertEqual(panels[1]["size"]["w"], 24)
        self.assertEqual(panels[0]["position"]["x"], 0)
        self.assertEqual(panels[1]["position"]["x"], 24)

    def test_single_panel_gets_full_width(self):
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout
        panels = [
            {"title": "Solo", "esql": {"type": "area"}, "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": 0},
        ]
        _apply_kibana_native_layout(panels)
        self.assertEqual(panels[0]["size"]["w"], 48)

    def test_four_panels_get_quarter_width(self):
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout
        panels = [
            {"title": f"P{i}", "esql": {"type": "metric"}, "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": i * 6}
            for i in range(4)
        ]
        _apply_kibana_native_layout(panels)
        for p in panels:
            self.assertEqual(p["size"]["w"], 12)

    def test_rows_stack_vertically(self):
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout
        panels = [
            {"title": "Row1", "esql": {"type": "line"}, "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": 0},
            {"title": "Row2", "esql": {"type": "metric"}, "size": {}, "position": {},
             "_grafana_row_y": 10, "_grafana_row_x": 0},
        ]
        _apply_kibana_native_layout(panels)
        self.assertEqual(panels[0]["position"]["y"], 0)
        self.assertEqual(panels[0]["size"]["h"], 12)
        self.assertEqual(panels[1]["position"]["y"], 12)
        self.assertEqual(panels[1]["size"]["h"], 5)

    def test_row_height_is_max_of_types(self):
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout
        panels = [
            {"title": "Chart", "esql": {"type": "line"}, "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": 0},
            {"title": "Metric", "esql": {"type": "metric"}, "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": 12},
        ]
        _apply_kibana_native_layout(panels)
        self.assertEqual(panels[0]["size"]["h"], 12)
        self.assertEqual(panels[1]["size"]["h"], 12)

    def test_metadata_tags_cleaned_up(self):
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout
        panels = [
            {"title": "A", "esql": {"type": "line"}, "size": {}, "position": {},
             "_grafana_row_y": 5, "_grafana_row_x": 3},
        ]
        _apply_kibana_native_layout(panels)
        self.assertNotIn("_grafana_row_y", panels[0])
        self.assertNotIn("_grafana_row_x", panels[0])

    def test_eight_panels_distribute_across_48_cols(self):
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout
        panels = [
            {"title": f"G{i}", "esql": {"type": "gauge"}, "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": i * 3}
            for i in range(8)
        ]
        _apply_kibana_native_layout(panels)
        total_w = sum(p["size"]["w"] for p in panels)
        self.assertEqual(total_w, 48)
        self.assertEqual(panels[0]["size"]["w"], 6)

    def test_datatable_gets_tall_height(self):
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout
        panels = [
            {"title": "Table", "esql": {"type": "datatable"}, "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": 0},
        ]
        _apply_kibana_native_layout(panels)
        self.assertEqual(panels[0]["size"]["h"], 15)

    def test_grafana_geometry_metadata_preserves_scaled_tile_dimensions(self):
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout

        panels = [
            {
                "title": "Pressure",
                "esql": {"type": "bar"},
                "size": {},
                "position": {},
                "_grafana_row_y": 1,
                "_grafana_row_x": 0,
                "_grafana_w": 3,
                "_grafana_h": 4,
            },
            {
                "title": "CPU Busy",
                "esql": {"type": "gauge"},
                "size": {},
                "position": {},
                "_grafana_row_y": 1,
                "_grafana_row_x": 3,
                "_grafana_w": 3,
                "_grafana_h": 4,
            },
        ]

        _apply_kibana_native_layout(panels)

        self.assertEqual(panels[0]["size"], {"w": 6, "h": 6})
        self.assertEqual(panels[1]["size"], {"w": 6, "h": 6})
        self.assertEqual(panels[0]["position"], {"x": 0, "y": 0})
        self.assertEqual(panels[1]["position"], {"x": 6, "y": 0})
        self.assertNotIn("_grafana_w", panels[0])
        self.assertNotIn("_grafana_h", panels[0])


class NativePromqlTests(unittest.TestCase):
    """Tests for the native PROMQL ES|QL source command feature."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.rule_pack.native_promql = True
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def _make_panel(self, expr, panel_type="timeseries", datasource_type="prometheus"):
        return {
            "type": panel_type,
            "title": "Test Panel",
            "datasource": {"type": datasource_type, "uid": "prom1"},
            "targets": [{"expr": expr, "refId": "A"}],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }

    def translate_panel(self, panel):
        return migrate.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    # ── basic routing ──

    def test_simple_metric_uses_native_promql(self):
        panel = self._make_panel("up")
        _yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.status, "migrated")
        self.assertIn("PROMQL", result.esql_query)
        self.assertIn("up", result.esql_query)
        self.assertEqual(result.query_language, "promql")

    def test_rate_expression_uses_native_promql(self):
        panel = self._make_panel("rate(http_requests_total[5m])")
        _yaml_panel, result = self.translate_panel(panel)
        self.assertIn("PROMQL", result.esql_query)
        self.assertIn("rate(http_requests_total[5m])", result.esql_query)

    def test_sum_by_uses_native_promql(self):
        panel = self._make_panel('sum by (instance) (rate(http_requests_total[5m]))')
        _yaml_panel, result = self.translate_panel(panel)
        self.assertIn("PROMQL", result.esql_query)
        self.assertIn("sum by (instance)", result.esql_query)
        self.assertEqual(result.query_ir["output_shape"], "time_series")
        self.assertEqual(result.query_ir["output_group_fields"], ["step", "instance"])

    def test_avg_over_time_uses_native_promql(self):
        panel = self._make_panel("avg_over_time(cpu_usage[10m])")
        _yaml_panel, result = self.translate_panel(panel)
        self.assertIn("PROMQL", result.esql_query)
        self.assertIn("step=1m", result.esql_query)

    # ── query builder ──

    def test_build_native_promql_query_structure(self):
        from observability_migration.adapters.source.grafana.panels import build_native_promql_query
        q = build_native_promql_query("rate(foo[5m])", index="metrics-*")
        self.assertTrue(q.startswith("PROMQL"))
        self.assertIn("index=metrics-*", q)
        self.assertIn("step=1m", q)
        self.assertIn("value=(rate(foo[5m]))", q)
        self.assertNotIn("start=", q)
        self.assertNotIn("end=", q)

    def test_build_native_promql_query_keeps_default_step_even_with_range(self):
        from observability_migration.adapters.source.grafana.panels import build_native_promql_query
        q = build_native_promql_query("avg_over_time(mem[15m])", index="idx-*")
        self.assertIn("step=1m", q)

    def test_build_native_promql_query_defaults_step_1m(self):
        from observability_migration.adapters.source.grafana.panels import build_native_promql_query
        q = build_native_promql_query("up", index="metrics-*")
        self.assertIn("step=1m", q)

    def test_build_native_promql_query_custom_index(self):
        from observability_migration.adapters.source.grafana.panels import build_native_promql_query
        q = build_native_promql_query("up", index="my-metrics-*")
        self.assertIn("index=my-metrics-*", q)

    def test_build_native_promql_query_replaces_custom_interval_variable(self):
        from observability_migration.adapters.source.grafana.panels import build_native_promql_query
        q = build_native_promql_query("increase(foo[$aggregation_interval])", index="metrics-*")
        self.assertIn("step=1m", q)
        self.assertIn("value=(increase(foo[5m]))", q)

    def test_build_native_promql_query_normalizes_metric_selector_spacing(self):
        from observability_migration.adapters.source.grafana.panels import build_native_promql_query
        q = build_native_promql_query('node_filesystem_avail_bytes {instance="$node"}', index="metrics-*")
        self.assertIn('node_filesystem_avail_bytes{instance=~".*"}', q)

    def test_build_native_promql_query_replaces_double_bracket_variable(self):
        from observability_migration.adapters.source.grafana.panels import build_native_promql_query
        q = build_native_promql_query('rate(foo{instance=~"[[instance]]"}[5m])', index="metrics-*")
        self.assertIn('instance=~".*"', q)

    def test_build_native_promql_query_collapses_newlines(self):
        from observability_migration.adapters.source.grafana.panels import build_native_promql_query
        q = build_native_promql_query("sum(cluster_autoscaler_nodes_count)\n", index="metrics-*")
        self.assertNotIn("\n", q)
        self.assertIn("value=(sum(cluster_autoscaler_nodes_count))", q)

    # ── can_use_native_promql guard ──

    def test_can_use_simple_expressions(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertTrue(can_use_native_promql("up"))
        self.assertTrue(can_use_native_promql("rate(foo[5m])"))
        self.assertTrue(can_use_native_promql('sum by (job) (rate(http_requests_total[5m]))'))
        self.assertTrue(can_use_native_promql("max(avg_over_time(cpu[10m]))"))

    def test_rejects_topk(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertFalse(can_use_native_promql("topk(5, http_requests_total)"))

    def test_rejects_group_left(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertFalse(can_use_native_promql("foo / on(method) group_left bar"))

    def test_rejects_unless(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertFalse(can_use_native_promql("foo unless bar"))

    def test_accepts_without_modifier(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertTrue(can_use_native_promql("sum without (instance) (rate(foo_total[5m]))"))

    def test_accepts_name_regex(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertTrue(can_use_native_promql('{__name__=~"cpu.*"}'))

    def test_accepts_offset_modifier(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertTrue(can_use_native_promql("foo offset 5m"))

    def test_rejects_at_modifier(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertFalse(can_use_native_promql("foo @ 1603774568"))

    def test_rejects_empty_expression(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertFalse(can_use_native_promql(""))
        self.assertFalse(can_use_native_promql("   "))

    def test_accepts_count_values(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertTrue(can_use_native_promql('count_values("version", build_info)'))

    def test_rejects_bottomk(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertFalse(can_use_native_promql("bottomk(3, http_requests_total)"))

    def test_rejects_label_replace(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertFalse(can_use_native_promql('label_replace(foo{a="1",b="2"}, "dst", "$1", "src", ".*")'))

    def test_rejects_label_join(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertFalse(can_use_native_promql('label_join(foo{a="1",b="2"}, "dst", ":", "src1", "src2")'))

    def test_accepts_toplevel_literal_comparison(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertTrue(can_use_native_promql("rate(foo[5m]) > 0"))
        self.assertTrue(can_use_native_promql("sum(increase(bar[5m])) by (instance) > 0"))
        self.assertTrue(can_use_native_promql("up >= 1"))
        self.assertTrue(can_use_native_promql("up == 1"))
        self.assertTrue(can_use_native_promql("up != 0"))

    def test_accepts_parenthesized_toplevel_literal_comparison(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertTrue(can_use_native_promql("(up == 1)"))

    def test_string_literals_do_not_trigger_set_operator_guard(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertTrue(can_use_native_promql('{job="or"}'))
        self.assertTrue(can_use_native_promql('{job="and"}'))
        self.assertTrue(can_use_native_promql('sum by (job) (foo{env="or"})'))

    def test_rejects_nested_comparison(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertFalse(can_use_native_promql("count(up == 1)"))
        self.assertFalse(can_use_native_promql("sum(kube_pvc{phase=\"Bound\"}==1)"))

    def test_rejects_metric_vs_metric_comparison(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertFalse(can_use_native_promql("metric_a == metric_b"))

    def test_accepts_delta_expression(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        self.assertTrue(can_use_native_promql("sum(delta(foo[30m]))"))

    def test_rejects_known_filesystem_parser_bug_expression(self):
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
        expr = (
            '(node_filesystem_size_bytes{instance=~"$node"}-node_filesystem_free_bytes{instance=~"$node"})'
            ' *100/(node_filesystem_avail_bytes {instance=~"$node"}+'
            '(node_filesystem_size_bytes{instance=~"$node"}-node_filesystem_free_bytes{instance=~"$node"}))'
        )
        self.assertFalse(can_use_native_promql(expr))

    # ── fallback to ES|QL translation ──

    def test_topk_falls_back_to_markdown(self):
        panel = self._make_panel("topk(5, http_requests_total)")
        _yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.status, "not_feasible")

    def test_offset_expr_uses_native_promql(self):
        panel = self._make_panel("rate(foo[5m]) offset 1h")
        _yaml_panel, result = self.translate_panel(panel)
        self.assertIn("PROMQL index=", result.esql_query)
        self.assertIn("offset 1h", result.esql_query)

    def test_native_promql_mode_still_resolves_group_labels_in_esql_fallback(self):
        panel = {
            "type": "timeseries",
            "title": "Network Operational Status",
            "datasource": {"type": "prometheus", "uid": "prom1"},
            "targets": [
                {
                    "expr": 'node_network_up{operstate="up",instance="$node",job="$job"}',
                    "legendFormat": "{{interface}} - Operational state UP",
                    "refId": "A",
                },
                {
                    "expr": 'node_network_carrier{instance="$node",job="$job"}',
                    "legendFormat": "{{device}} - Physical link state",
                    "refId": "B",
                },
            ],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        _yaml_panel, result = self.translate_panel(panel)
        query = result.esql_query or ""
        self.assertNotIn("PROMQL index=", query)
        self.assertIn("BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), device", query)
        self.assertNotIn("BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), interface", query)
        self.assertEqual(result.query_ir["source_type"], "FROM")
        self.assertEqual(result.target_query_contract["canonical_target"], "promql")
        self.assertEqual(result.contract_evaluation["status"], "degraded_if_forced")
        self.assertEqual(result.fulfillment_plan["status"], "not_required")

    # ── flag disabled: normal translation ──

    def test_disabled_flag_uses_esql_translation(self):
        rule_pack = migrate.RulePackConfig()
        resolver = migrate.SchemaResolver(rule_pack)
        panel = self._make_panel("rate(http_requests_total[5m])")
        _yaml_panel, result = migrate.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        if result.esql_query:
            self.assertNotIn("PROMQL index=", result.esql_query)
            self.assertTrue(
                result.esql_query.startswith("TS ") or result.esql_query.startswith("FROM "),
                f"Expected ES|QL FROM/TS, got: {result.esql_query[:60]}",
            )

    # ── YAML output structure ──

    def test_yaml_panel_has_esql_block_with_promql_query(self):
        panel = self._make_panel("rate(http_requests_total[5m])")
        yaml_panel, _result = self.translate_panel(panel)
        self.assertIn("esql", yaml_panel)
        esql_block = yaml_panel["esql"]
        self.assertIn("query", esql_block)
        self.assertIn("PROMQL", esql_block["query"])

    def test_native_timeseries_uses_step_dimension(self):
        panel = self._make_panel("rate(http_requests_total[5m])")
        yaml_panel, _ = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["dimension"]["field"], "step")

    def test_native_grouped_timeseries_sets_breakdown(self):
        panel = self._make_panel("sum by (job) (rate(http_requests_total[5m]))")
        yaml_panel, _ = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["breakdown"]["field"], "job")

    def test_native_postfix_grouped_timeseries_sets_breakdown(self):
        panel = self._make_panel("sum(rate(http_requests_total[5m])) by (handler)")
        yaml_panel, _ = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["breakdown"]["field"], "handler")

    def test_native_aggregated_timeseries_omits_breakdown(self):
        panel = self._make_panel("avg(otelcol_process_memory_rss)")
        yaml_panel, _ = self.translate_panel(panel)
        self.assertNotIn("breakdown", yaml_panel["esql"])

    def test_native_nested_grouping_collapsed_by_outer_agg_omits_breakdown(self):
        panel = self._make_panel("count(count(otelcol_process_cpu_seconds) by (service_instance_id))")
        yaml_panel, _ = self.translate_panel(panel)
        self.assertNotIn("breakdown", yaml_panel["esql"])

    def test_native_pie_sets_breakdowns(self):
        panel = self._make_panel("sum by (handler) (rate(http_requests_total[5m]))", panel_type="piechart")
        yaml_panel, _ = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["type"], "pie")
        self.assertEqual(yaml_panel["esql"]["breakdowns"], [{"field": "handler"}])

    def test_yaml_panel_type_matches_kibana_type(self):
        panel = self._make_panel("rate(foo[5m])", panel_type="timeseries")
        yaml_panel, _result = self.translate_panel(panel)
        esql_block = yaml_panel.get("esql", {})
        self.assertIn(esql_block.get("type"), ("line", "bar", "area"))

    def test_stat_panel_produces_metric_type(self):
        panel = self._make_panel("sum(up)", panel_type="stat")
        yaml_panel, _result = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["type"], "metric")

    def test_gauge_panel_produces_gauge_type(self):
        panel = self._make_panel("avg(cpu_usage)", panel_type="gauge")
        yaml_panel, _result = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["type"], "gauge")

    def test_stat_panel_with_multi_series_skips_native_promql(self):
        panel = self._make_panel("up", panel_type="stat")
        _, result = self.translate_panel(panel)
        self.assertNotEqual(result.query_ir.get("family"), "native_promql")
        self.assertNotIn("PROMQL index=", result.esql_query or "")

    def test_gauge_panel_with_grouped_series_skips_native_promql(self):
        panel = self._make_panel("sum by (job) (rate(http_requests_total[5m]))", panel_type="gauge")
        _, result = self.translate_panel(panel)
        self.assertNotEqual(result.query_ir.get("family"), "native_promql")
        self.assertNotIn("PROMQL index=", result.esql_query or "")

    # ── query IR ──

    def test_query_ir_family_is_native_promql(self):
        panel = self._make_panel("rate(http_requests_total[5m])")
        _, result = self.translate_panel(panel)
        self.assertEqual(result.query_ir.get("family"), "native_promql")

    def test_query_ir_source_language_is_promql(self):
        panel = self._make_panel("rate(http_requests_total[5m])")
        _, result = self.translate_panel(panel)
        self.assertEqual(result.query_ir.get("source_language"), "promql")

    def test_query_ir_source_expression_is_original_promql(self):
        expr = 'sum by (job) (rate(http_requests_total[5m]))'
        panel = self._make_panel(expr)
        _, result = self.translate_panel(panel)
        self.assertEqual(result.query_ir.get("source_expression"), expr)

    def test_query_ir_clean_expression_uses_cleaned_native_promql(self):
        expr = 'node_filesystem_avail_bytes {instance="$node"}'
        panel = self._make_panel(expr)
        _, result = self.translate_panel(panel)
        self.assertEqual(
            result.query_ir.get("clean_expression"),
            'node_filesystem_avail_bytes{instance=~".*"}',
        )

    def test_query_ir_target_query_is_promql_command(self):
        panel = self._make_panel("rate(foo[5m])")
        _, result = self.translate_panel(panel)
        self.assertIn("PROMQL", result.query_ir.get("target_query", ""))

    def test_query_ir_target_index(self):
        panel = self._make_panel("up")
        _, result = self.translate_panel(panel)
        self.assertEqual(result.query_ir.get("target_index"), "metrics-*")

    # ── panel notes ──

    def test_native_promql_note_in_panel_notes(self):
        panel = self._make_panel("up")
        _, result = self.translate_panel(panel)
        notes = result.notes
        self.assertTrue(
            any("Native PROMQL" in n for n in notes),
            f"Expected native PROMQL note, got: {notes}",
        )

    def test_native_promql_bare_variable_note_in_panel_notes(self):
        panel = self._make_panel("up * $scale")
        _, result = self.translate_panel(panel)
        self.assertTrue(
            any("replaced with literal 1" in n for n in result.notes),
            f"Expected bare-variable note, got: {result.notes}",
        )

    # ── confidence ──

    def test_native_promql_confidence_is_high(self):
        panel = self._make_panel("rate(http_requests_total[5m])")
        _, result = self.translate_panel(panel)
        self.assertGreaterEqual(result.confidence, 0.85)

    # ── logql panels are not affected ──

    def test_logql_panel_not_affected_by_native_promql(self):
        panel = {
            "type": "timeseries",
            "title": "Log Rate",
            "datasource": {"type": "loki", "uid": "loki1"},
            "targets": [{"expr": '{job="varlogs"} |= "error"', "refId": "A"}],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        _, result = self.translate_panel(panel)
        if result.esql_query:
            self.assertNotIn("PROMQL index=", result.esql_query)

    # ── multi-target panels ──

    def test_multi_target_xy_panel_merges_targets(self):
        panel = {
            "type": "timeseries",
            "title": "Multi",
            "datasource": {"type": "prometheus", "uid": "prom1"},
            "targets": [
                {"expr": "rate(cpu[5m])", "refId": "A"},
                {"expr": "rate(mem[5m])", "refId": "B"},
            ],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        _, result = self.translate_panel(panel)
        self.assertIn("cpu", result.esql_query)
        self.assertIn("mem", result.esql_query)

    # ── esql panels are not affected ──

    def test_native_esql_panel_takes_priority(self):
        panel = {
            "type": "timeseries",
            "title": "ES|QL Native",
            "datasource": {"type": "elasticsearch", "uid": "es1"},
            "targets": [{"query": "FROM metrics-* | STATS avg(cpu) BY @timestamp", "refId": "A"}],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        _, result = self.translate_panel(panel)
        if result.esql_query:
            self.assertNotIn("PROMQL index=", result.esql_query)

    def test_native_esql_one_line_stats_uses_actual_fields(self):
        panel = {
            "type": "timeseries",
            "title": "Native ES|QL One Line",
            "datasource": {"type": "elasticsearch", "uid": "es1"},
            "targets": [{"query": "FROM metrics-* | STATS avg_cpu = AVG(cpu) BY host", "refId": "A"}],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["metrics"][0]["field"], "avg_cpu")
        self.assertEqual(yaml_panel["esql"]["dimension"]["field"], "host")
        self.assertEqual(result.query_ir["output_group_fields"], ["host"])
        self.assertEqual(result.query_ir["output_metric_field"], "avg_cpu")

    def test_native_esql_scalar_stats_requires_manual_mapping_for_timeseries(self):
        panel = {
            "type": "timeseries",
            "title": "Native ES|QL Scalar",
            "datasource": {"type": "elasticsearch", "uid": "es1"},
            "targets": [{"query": "FROM metrics-* | STATS avg_cpu = AVG(cpu)", "refId": "A"}],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.status, "requires_manual")
        self.assertIn("markdown", yaml_panel)

    def test_native_esql_prefers_time_dimension_even_when_not_first(self):
        panel = {
            "type": "timeseries",
            "title": "Native ES|QL Reordered",
            "datasource": {"type": "elasticsearch", "uid": "es1"},
            "targets": [
                {
                    "query": (
                        "FROM metrics-*\n"
                        "| STATS avg_cpu = AVG(cpu) BY host, time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)"
                    ),
                    "refId": "A",
                }
            ],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        yaml_panel, _ = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["dimension"]["field"], "time_bucket")
        self.assertEqual(yaml_panel["esql"]["breakdown"]["field"], "host")

    def test_native_esql_pie_does_not_use_time_breakdown(self):
        panel = {
            "type": "piechart",
            "title": "Native ES|QL Pie Time",
            "datasource": {"type": "elasticsearch", "uid": "es1"},
            "targets": [
                {
                    "query": (
                        "FROM metrics-*\n"
                        "| STATS avg_cpu = AVG(cpu) BY timestamp_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)"
                    ),
                    "refId": "A",
                }
            ],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["type"], "bar")
        self.assertTrue(
            any("Approximated pie chart as bar chart" in reason for reason in result.reasons),
            result.reasons,
        )

    def test_native_esql_multi_metric_xy_keeps_all_metrics(self):
        panel = {
            "type": "timeseries",
            "title": "ES|QL Native Multi",
            "datasource": {"type": "elasticsearch", "uid": "es1"},
            "targets": [
                {
                    "query": (
                        "FROM metrics-*\n"
                        "| STATS cpu = AVG(cpu_usage), mem = AVG(mem_usage) "
                        "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), instance"
                    ),
                    "refId": "A",
                }
            ],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.status, "migrated")
        self.assertEqual(yaml_panel["esql"]["type"], "line")
        self.assertEqual(
            [metric["field"] for metric in yaml_panel["esql"]["metrics"]],
            ["cpu", "mem"],
        )

    # ── datasource_index passthrough ──

    def test_custom_datasource_index_in_promql_query(self):
        panel = self._make_panel("up")
        _yaml_panel, result = migrate.translate_panel(
            panel,
            datasource_index="my-custom-metrics-*",
            esql_index="my-custom-metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertIn("index=my-custom-metrics-*", result.esql_query)

    # ── dashboard-level integration ──

    def test_translate_dashboard_with_native_promql(self):
        dashboard = {
            "uid": "test-native-promql",
            "title": "Native PromQL Test",
            "panels": [
                {
                    "type": "timeseries",
                    "title": "CPU Rate",
                    "id": 1,
                    "datasource": {"type": "prometheus", "uid": "prom1"},
                    "targets": [{"expr": "rate(cpu_usage[5m])", "refId": "A"}],
                    "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                },
                {
                    "type": "stat",
                    "title": "Up Count",
                    "id": 2,
                    "datasource": {"type": "prometheus", "uid": "prom1"},
                    "targets": [{"expr": "sum(up)", "refId": "A"}],
                    "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
                },
                {
                    "type": "timeseries",
                    "title": "TopK Panel",
                    "id": 3,
                    "datasource": {"type": "prometheus", "uid": "prom1"},
                    "targets": [{"expr": "topk(5, http_requests_total)", "refId": "A"}],
                    "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
                },
            ],
        }
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            result, yaml_path = migrate.translate_dashboard(
                dashboard,
                tmpdir,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            native_panels = [
                pr for pr in result.panel_results
                if pr.query_ir.get("family") == "native_promql"
            ]
            self.assertGreaterEqual(len(native_panels), 2, "CPU Rate and Up Count should use native PROMQL")
            for pr in native_panels:
                self.assertIn("PROMQL", pr.esql_query)
                self.assertEqual(pr.query_ir.get("source_language"), "promql")

            topk_panels = [pr for pr in result.panel_results if "TopK" in pr.title]
            if topk_panels:
                self.assertEqual(
                    topk_panels[0].status, "not_feasible",
                    "topk is unsupported on the ES PROMQL bridge",
                )

            yaml_doc = yaml.safe_load(pathlib.Path(yaml_path).read_text())
            esql_panels = [
                p for p in yaml_doc["dashboards"][0]["panels"]
                if "esql" in p and "PROMQL" in p["esql"].get("query", "")
            ]
            self.assertGreaterEqual(len(esql_panels), 2)

    # ── RulePackConfig default ──

    def test_rule_pack_native_promql_default_false(self):
        rp = migrate.RulePackConfig()
        self.assertFalse(rp.native_promql)

    def test_rule_pack_native_promql_can_be_enabled(self):
        rp = migrate.RulePackConfig()
        rp.native_promql = True
        self.assertTrue(rp.native_promql)


if __name__ == "__main__":
    unittest.main()
