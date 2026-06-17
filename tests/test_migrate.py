# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
import pathlib
import re
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
    _esql_field=promql._esql_field,
)


class DatasourceIdentityTests(unittest.TestCase):
    """analyze_panel_targets must treat the datasource UID as the authoritative
    identity when present (issue #56). The four scenarios below mirror the
    behaviour table in that issue so the comparison cannot silently regress in
    either direction.
    """

    @staticmethod
    def _panel(*datasources):
        return {
            "type": "graph",
            "targets": [
                {"refId": chr(ord("A") + idx), "datasource": ds}
                for idx, ds in enumerate(datasources)
            ],
        }

    def test_same_uid_with_missing_type_is_not_mixed(self):
        panel = self._panel(
            {"type": "cloudwatch", "uid": "cw"},
            {"uid": "cw"},
        )
        self.assertFalse(manifest.analyze_panel_targets(panel)["mixed_datasource"])

    def test_same_type_different_uid_is_mixed(self):
        panel = self._panel(
            {"type": "cloudwatch", "uid": "cw-1"},
            {"type": "cloudwatch", "uid": "cw-2"},
        )
        self.assertTrue(manifest.analyze_panel_targets(panel)["mixed_datasource"])

    def test_different_uid_and_language_is_mixed(self):
        panel = {
            "type": "graph",
            "targets": [
                {"refId": "A", "expr": "rate(http_total[5m])",
                 "datasource": {"type": "prometheus", "uid": "prom"}},
                {"refId": "B", "expr": '{service="api"} |~ "error"',
                 "datasource": {"type": "loki", "uid": "loki"}},
            ],
        }
        self.assertTrue(manifest.analyze_panel_targets(panel)["mixed_datasource"])

    def test_legacy_no_uid_same_type_and_name_is_not_mixed(self):
        panel = self._panel(
            {"type": "prometheus", "name": "Prom"},
            {"type": "prometheus", "name": "Prom"},
        )
        self.assertFalse(manifest.analyze_panel_targets(panel)["mixed_datasource"])

    def test_legacy_no_uid_same_name_different_type_is_mixed(self):
        panel = self._panel(
            {"type": "prometheus", "name": "shared"},
            {"type": "loki", "name": "shared"},
        )
        self.assertTrue(manifest.analyze_panel_targets(panel)["mixed_datasource"])


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

    def test_count_scalar_normalized_to_scalar_count(self):
        # count_scalar() was removed in Prometheus 2.0; it is semantically
        # identical to scalar(count()). Normalise it at preprocessing so the
        # AST parser does not choke on the unknown function (issue #63).
        clean = migrate.preprocess_grafana_macros("count_scalar(up)", self.rule_pack)
        self.assertEqual(clean, "scalar(count(up))")

    def test_count_scalar_normalized_with_label_matcher_braces(self):
        # The argument can contain its own braces/parens; the rewrite must keep
        # the whole balanced argument intact.
        clean = migrate.preprocess_grafana_macros(
            'count_scalar(node_cpu{mode="user", alias="a1"})', self.rule_pack
        )
        self.assertEqual(clean, 'scalar(count(node_cpu{mode="user", alias="a1"}))')

    def test_count_scalar_no_longer_fails_to_parse(self):
        # Full expression from issue #63 (Bind DNS dashboard #1666, panel 16).
        # The count_scalar normalisation must remove the AST parse failure and
        # the "Could not extract metric name" follow-on so the expression parses
        # like its scalar(count()) equivalent. (Whether this particular
        # grouped/scalar arithmetic then fully translates is a separate,
        # pre-existing limitation surfaced honestly as a divergent-grouping
        # not-feasible, not a count_scalar parse failure.)
        expr = (
            'sum(rate(node_cpu{alias="a1"}[120s])) by (mode) * 100 '
            '/ count_scalar(node_cpu{mode="user", alias="a1"})'
        )
        translated = self.translate(expr)
        joined = " ".join(translated.warnings)
        self.assertNotIn("unknown function with name 'count_scalar'", joined)
        self.assertNotIn("Could not extract metric name", joined)

    def test_count_scalar_divides_ungrouped_metric_is_feasible(self):
        # When the count_scalar() result divides an ungrouped aggregate (the
        # scalar broadcasts cleanly), the normalised expression translates.
        expr = "sum(rate(node_cpu[120s])) / count_scalar(node_cpu)"
        translated = self.translate(expr)
        self.assertEqual(translated.feasibility, "feasible")
        self.assertNotIn(
            "unknown function with name 'count_scalar'",
            " ".join(translated.warnings),
        )

    def test_range_seconds_macro_is_preserved_as_duration_suffix(self):
        clean = migrate.preprocess_grafana_macros("sum(rate(http_requests_total[${__range_s}s]))", self.rule_pack)
        self.assertEqual(clean, "sum(rate(http_requests_total[3600s]))")

        query = panels.build_native_promql_query("sum(rate(http_requests_total[${__range_s}s]))", index="metrics-*")
        self.assertIn("[3600s]", query)
        self.assertNotIn("[1s]", query)

    def test_unitless_range_seconds_macro_in_range_selector_gets_seconds_suffix(self):
        clean = migrate.preprocess_grafana_macros("sum(rate(http_requests_total[$__range_s]))", self.rule_pack)
        self.assertEqual(clean, "sum(rate(http_requests_total[3600s]))")

    def test_range_seconds_macro_translates_cleanly_on_esql_path(self):
        result = self.translate("sum(rate(http_requests_total[${__range_s}s]))")

        self.assertEqual(result.feasibility, "feasible")
        self.assertEqual(result.clean_expr, "sum(rate(http_requests_total[3600s]))")
        self.assertIn("RATE(http_requests_total, 1h)", result.esql_query)
        self.assertFalse(
            any(warning.startswith("AST parse failed") for warning in result.warnings),
            result.warnings,
        )
        self.assertNotIn("Could not extract metric name", result.warnings)

    def test_range_milliseconds_macro_is_preserved_as_duration_suffix(self):
        clean = migrate.preprocess_grafana_macros("sum(rate(http_requests_total[${__range_ms}ms]))", self.rule_pack)
        self.assertEqual(clean, "sum(rate(http_requests_total[3600000ms]))")

        query = panels.build_native_promql_query("sum(rate(http_requests_total[${__range_ms}ms]))", index="metrics-*")
        self.assertIn("[3600000ms]", query)

    def test_unitless_range_milliseconds_macro_in_range_selector_gets_milliseconds_suffix(self):
        clean = migrate.preprocess_grafana_macros("sum(rate(http_requests_total[$__range_ms]))", self.rule_pack)
        self.assertEqual(clean, "sum(rate(http_requests_total[3600000ms]))")

    def test_topk_template_limit_reports_specific_not_feasible_reason(self):
        result = self.translate("topk($top_n, rate(process_cpu_seconds_total[$__rate_interval]))")

        self.assertEqual(result.feasibility, "not_feasible")
        self.assertIn(
            "topk() with a Grafana template-variable limit cannot be translated automatically; "
            "top-N time-series requires manual redesign",
            result.warnings,
        )
        self.assertNotIn("Could not extract metric name", result.warnings)

    def test_topk_builtin_template_limit_reports_specific_not_feasible_reason(self):
        result = self.translate("topk($__range_s, rate(process_cpu_seconds_total[$__rate_interval]))")

        self.assertEqual(result.feasibility, "not_feasible")
        self.assertIn(
            "topk() with a Grafana template-variable limit cannot be translated automatically; "
            "top-N time-series requires manual redesign",
            result.warnings,
        )

    def test_bottomk_template_limit_reports_specific_not_feasible_reason(self):
        result = self.translate("bottomk($top_n, rate(process_cpu_seconds_total[$__rate_interval]))")

        self.assertEqual(result.feasibility, "not_feasible")
        self.assertIn(
            "bottomk() with a Grafana template-variable limit cannot be translated automatically; "
            "top-N time-series requires manual redesign",
            result.warnings,
        )
        self.assertNotIn("Could not extract metric name", result.warnings)

    def test_grouping_template_variable_reports_specific_not_feasible_reason(self):
        result = self.translate(
            "max(otelcol_exporter_queue_size) by (exporter $grouping) "
            "/ min(otelcol_exporter_queue_size) by (exporter $grouping)"
        )

        self.assertEqual(result.feasibility, "not_feasible")
        self.assertIn(
            "BY/WITHOUT clause contains Grafana template variable ($grouping); "
            "grouping dimension is unknown at migration time and requires manual redesign",
            result.warnings,
        )
        self.assertNotIn("Could not extract metric name", result.warnings)

    def test_grouping_template_variable_is_not_hidden_by_native_promql_path(self):
        expr = "sum(rate(http_requests_total[5m])) by (${grouping})"
        self.assertFalse(panels.can_use_native_promql(expr))
        with self.assertRaises(ValueError):
            panels.build_native_promql_query(expr, index="metrics-*")

        panel = {
            "title": "Dynamic grouping",
            "type": "graph",
            "targets": [
                {
                    "refId": "A",
                    "expr": expr,
                }
            ],
        }

        _yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.status, "not_feasible")
        self.assertIn(
            "BY/WITHOUT clause contains Grafana template variable ($grouping); "
            "grouping dimension is unknown at migration time and requires manual redesign",
            result.reasons,
        )
        self.assertNotIn("Native PROMQL", " ".join(result.notes))

    def test_grouping_guardrail_ignores_template_text_inside_string_literals(self):
        result = self.translate('sum(rate(http_requests_total{job=~"foo by ($grouping)"}[5m]))')

        self.assertNotIn(
            "BY/WITHOUT clause contains Grafana template variable ($grouping); "
            "grouping dimension is unknown at migration time and requires manual redesign",
            result.warnings,
        )

    def test_parse_failure_does_not_emit_generic_metric_name_warning(self):
        result = self.translate("sum(rate([label___range_ss]))")

        self.assertEqual(result.feasibility, "not_feasible")
        self.assertTrue(
            any(warning.startswith("AST parse failed") for warning in result.warnings),
            result.warnings,
        )
        self.assertNotIn("Could not extract metric name", result.warnings)

    def test_rule_pack_runtime_feature_profile_records_support_metadata(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            is_feature_supported,
            set_runtime_feature,
        )

        self.assertFalse(is_feature_supported(self.rule_pack, PROMQL_LABEL_MATCHER_PARAMS))

        set_runtime_feature(
            self.rule_pack,
            PROMQL_LABEL_MATCHER_PARAMS,
            supported=True,
            source="probe",
            confidence="verified",
            level="syntax",
            reason="target accepted PromQL label matcher params",
        )

        self.assertTrue(is_feature_supported(self.rule_pack, PROMQL_LABEL_MATCHER_PARAMS))
        self.assertEqual(
            self.rule_pack.runtime_features[PROMQL_LABEL_MATCHER_PARAMS],
            {
                "supported": True,
                "source": "probe",
                "confidence": "verified",
                "level": "syntax",
                "reason": "target accepted PromQL label matcher params",
            },
        )

    def test_binds_esql_named_params_accepts_either_capability(self):
        """Issue #132: ES|QL ``?var`` binding is enabled by EITHER the broad
        ``esql_named_param_binding`` capability OR the narrower native PROMQL
        ``promql_label_matcher_params`` capability."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            ESQL_NAMED_PARAM_BINDING,
            PROMQL_LABEL_MATCHER_PARAMS,
            binds_esql_named_params,
            set_runtime_feature,
        )

        self.assertFalse(binds_esql_named_params(self.rule_pack))

        set_runtime_feature(
            self.rule_pack, ESQL_NAMED_PARAM_BINDING, supported=True, source="probe"
        )
        self.assertTrue(binds_esql_named_params(self.rule_pack))

        other = migrate.RulePackConfig()
        self.assertFalse(binds_esql_named_params(other))
        set_runtime_feature(
            other, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe"
        )
        self.assertTrue(binds_esql_named_params(other))

    def test_native_promql_rejects_exact_template_label_matcher(self):
        self.assertFalse(panels.can_use_native_promql('cpu{host="$host"}'))
        with self.assertRaises(ValueError):
            panels.build_native_promql_query('cpu{host="$host"}', index="metrics-*")

    def test_native_promql_rejects_regex_template_label_matcher(self):
        self.assertFalse(panels.can_use_native_promql('cpu{service=~"$services"}'))
        with self.assertRaises(ValueError):
            panels.build_native_promql_query('cpu{service=~"$services"}', index="metrics-*")

    def test_native_promql_rejects_braced_template_label_matcher(self):
        self.assertFalse(panels.can_use_native_promql('cpu{host="${host}"}'))
        with self.assertRaises(ValueError):
            panels.build_native_promql_query('cpu{host="${host}"}', index="metrics-*")

    def test_native_promql_allows_template_label_matcher_when_runtime_feature_supported(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
        )

        runtime_features = {PROMQL_LABEL_MATCHER_PARAMS: True}

        self.assertTrue(
            panels.can_use_native_promql(
                'cpu{host="$host",service=~"$services"}',
                runtime_features=runtime_features,
            )
        )
        query = panels.build_native_promql_query(
            'cpu{host="$host",service=~"$services"}',
            index="metrics-*",
            runtime_features=runtime_features,
        )

        self.assertIn('cpu{host=?host, service=~?services}', query)
        self.assertNotIn('=~".*"', query)

    def test_native_promql_equality_matcher_on_match_all_var_uses_regex(self):
        """PR #133 review: on the native PROMQL path an equality matcher
        (``label="$var"``) whose variable defaults its control to the regex
        match-all (".*") must be loosened to ``label=~?var`` so the default
        selects every series, instead of ``label=?var`` which exact-matches the
        literal string ".*" and renders empty on first load."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
        )

        runtime_features = {PROMQL_LABEL_MATCHER_PARAMS: True}

        # Without a regex-default declaration the exact operator is preserved.
        plain = panels.build_native_promql_query(
            'cpu{host="$host"}', index="metrics-*", runtime_features=runtime_features
        )
        self.assertIn("cpu{host=?host}", plain)

        # Declared as regex-default -> equality is loosened to a regex match.
        loosened = panels.build_native_promql_query(
            'cpu{host="$host"}',
            index="metrics-*",
            runtime_features=runtime_features,
            regex_default_params={"host"},
        )
        self.assertIn("cpu{host=~?host}", loosened)
        # A regex matcher on a non-match-all var is untouched.
        mixed = panels.build_native_promql_query(
            'cpu{host="$host",svc=~"$svc"}',
            index="metrics-*",
            runtime_features=runtime_features,
            regex_default_params={"host"},
        )
        self.assertIn("cpu{host=~?host, svc=~?svc}", mixed)

    def test_esql_drops_exact_template_label_matcher_with_warning(self):
        result = self.translate('cpu{host="$host"}')

        self.assertEqual(result.feasibility, "feasible")
        self.assertNotIn("?host", result.esql_query)
        self.assertIn("Dropped variable-driven label filters during migration", result.warnings)

    def test_esql_drops_regex_template_label_matcher_with_warning(self):
        result = self.translate('cpu{service=~"$services"}')

        self.assertEqual(result.feasibility, "feasible")
        self.assertNotIn("?services", result.esql_query)
        self.assertIn("Dropped variable-driven label filters during migration", result.warnings)

    def test_esql_drops_negative_template_label_matchers_with_warning(self):
        exact_result = self.translate('cpu{host!="$host"}')
        regex_result = self.translate('cpu{service!~"$services"}')

        self.assertNotIn("?host", exact_result.esql_query)
        self.assertNotIn("?services", regex_result.esql_query)
        self.assertIn("Dropped variable-driven label filters during migration", exact_result.warnings)
        self.assertIn("Dropped variable-driven label filters during migration", regex_result.warnings)

    def test_esql_drops_braced_template_label_matcher_with_warning(self):
        result = self.translate('cpu{host="${host}"}')

        self.assertEqual(result.feasibility, "feasible")
        self.assertNotIn("?host", result.esql_query)
        self.assertIn("Dropped variable-driven label filters during migration", result.warnings)

    def test_esql_drops_bracket_template_label_matcher_with_warning(self):
        result = self.translate('cpu{instance=~"[[instance]]"}')

        self.assertEqual(result.feasibility, "feasible")
        self.assertNotIn("?instance", result.esql_query)
        self.assertIn("Dropped variable-driven label filters during migration", result.warnings)

    def test_esql_drops_multiple_template_label_matchers_with_warning(self):
        result = self.translate('cpu{host="$host",service=~"$services"}')

        self.assertEqual(result.feasibility, "feasible")
        self.assertNotIn("?host", result.esql_query)
        self.assertNotIn("?services", result.esql_query)
        self.assertIn("Dropped variable-driven label filters during migration", result.warnings)

    def test_panel_translation_drops_template_label_matcher_with_warning(self):
        panel = {
            "title": "CPU by host",
            "type": "graph",
            "targets": [{"refId": "A", "expr": 'cpu{host="$host"}'}],
        }

        yaml_panel, result = self.translate_panel(panel)

        # Panel-only translation has no dashboard control context, so dropping
        # the variable-driven matcher is surfaced as a warning.
        self.assertEqual(result.status, "migrated_with_warnings")
        self.assertNotIn("?host", yaml_panel["esql"]["query"])
        self.assertIn("Dropped variable-driven label filters during migration", result.reasons)

    def test_panel_template_label_matcher_falls_back_to_esql_with_static_legend(self):
        panel = {
            "title": "CPU by host",
            "type": "graph",
            "targets": [
                {
                    "refId": "A",
                    "expr": 'sum(cpu{host="$host"}) by (host)',
                    "legendFormat": "Selected host CPU",
                }
            ],
        }
        rule_pack = rules.RulePackConfig(native_promql=True)

        yaml_panel, result = panels.translate_panel(
            panel,
            esql_index="metrics-*",
            datasource_index="metrics-*",
            rule_pack=rule_pack,
            resolver=self.resolver,
        )

        self.assertEqual(result.status, "migrated_with_warnings")
        self.assertNotIn("PROMQL", yaml_panel["esql"]["query"])
        self.assertNotIn("?host", yaml_panel["esql"]["query"])
        self.assertEqual(yaml_panel["esql"]["metrics"][0]["label"], "Selected host CPU")
        self.assertIn(
            "Native PROMQL skipped: target does not support PromQL label matcher params yet",
            result.notes,
        )

    def test_dashboard_control_variable_fallback_does_not_emit_unbound_esql_param(self):
        """When native PROMQL skips a template label matcher, the ES|QL fallback
        must still upload as a runnable dashboard.

        Grafana query variables are emitted as ordinary Kibana dashboard filter
        controls. Those controls do not bind ``?host``-style ES|QL variables, so
        panel queries must not retain unbound query parameters.
        """
        dashboard = {
            "title": "Template Controls",
            "uid": "template-controls",
            "templating": {
                "list": [
                    {
                        "type": "query",
                        "name": "host",
                        "label": "Host",
                        "query": "label_values(cpu, host)",
                    }
                ]
            },
            "panels": [
                {
                    "id": 1,
                    "title": "CPU by host",
                    "type": "graph",
                    "targets": [
                        {
                            "refId": "A",
                            "expr": 'sum(cpu{host="$host"}) by (host)',
                        }
                    ],
                }
            ],
        }
        rule_pack = rules.RulePackConfig(native_promql=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            _result, yaml_path = panels.translate_dashboard(
                dashboard,
                tmpdir,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=rule_pack,
                resolver=self.resolver,
            )
            doc = yaml.safe_load(pathlib.Path(yaml_path).read_text())

        rendered = yaml.dump(doc)
        self.assertIn("controls:", rendered)
        self.assertNotIn("?host", rendered)

    def test_panel_template_label_matcher_uses_native_when_runtime_feature_supported(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
        )

        panel = {
            "title": "CPU by host",
            "type": "graph",
            "targets": [{"refId": "A", "expr": 'cpu{host="$host"}'}],
        }
        rule_pack = rules.RulePackConfig(
            native_promql=True,
            runtime_features={PROMQL_LABEL_MATCHER_PARAMS: True},
        )

        yaml_panel, result = panels.translate_panel(
            panel,
            esql_index="metrics-*",
            datasource_index="metrics-*",
            rule_pack=rule_pack,
            resolver=self.resolver,
        )

        self.assertEqual(result.status, "migrated")
        self.assertIn("PROMQL", yaml_panel["esql"]["query"])
        self.assertIn("cpu{host=?host}", yaml_panel["esql"]["query"])

    def test_multi_target_ts_query_uses_timeseries_aggregates_for_all_metrics(self):
        self.seed_field_caps({
            "process_virtual_memory_bytes": {"double": {"aggregatable": True, "time_series_metric": "gauge"}},
            "process_resident_memory_max_bytes": {"double": {"aggregatable": True, "time_series_metric": "gauge"}},
            "process_virtual_memory_max_bytes": {"double": {"aggregatable": True, "time_series_metric": "gauge"}},
            "instance": {"keyword": {"aggregatable": True, "searchable": True, "time_series_dimension": True}},
            "job": {"keyword": {"aggregatable": True, "searchable": True, "time_series_dimension": True}},
        })
        panel = {
            "title": "Processes Memory",
            "type": "timeseries",
            "targets": [
                {"refId": "A", "expr": 'irate(process_virtual_memory_bytes{instance="$node",job="$job"}[$__rate_interval])'},
                {"refId": "B", "expr": 'process_resident_memory_max_bytes{instance="$node",job="$job"}'},
                {"refId": "C", "expr": 'irate(process_virtual_memory_bytes{instance="$node",job="$job"}[$__rate_interval])'},
                {"refId": "D", "expr": 'irate(process_virtual_memory_max_bytes{instance="$node",job="$job"}[$__rate_interval])'},
            ],
        }

        yaml_panel, result = self.translate_panel(panel)

        self.assertNotEqual(result.status, "requires_manual")
        query = yaml_panel["esql"]["query"]
        # Whatever the grouping, the STATS must be internally consistent: it must
        # NOT mix a bare time-series aggregate with a regular aggregate, or ES
        # rejects it at runtime ("Cannot mix time-series aggregate and regular
        # aggregate in the same TimeSeriesAggregate").
        stats_line = next(
            (line for line in query.splitlines() if "STATS" in line),
            "",
        )
        has_bare_ts = bool(
            re.search(r"(?:STATS|,)\s*[A-Za-z0-9_.`]+\s*=\s*[A-Z]+_OVER_TIME\(", stats_line)
        )
        has_wrapped_ts = bool(
            re.search(r"=\s*(?:AVG|SUM|MIN|MAX|COUNT)\(\s*[A-Z]+_OVER_TIME\(", stats_line)
        )
        self.assertFalse(
            has_bare_ts and has_wrapped_ts,
            f"STATS mixes bare and wrapped time-series aggregates: {stats_line!r}",
        )

    def test_missing_legend_label_is_dropped_from_translated_query(self):
        self.seed_field_caps({
            "node_interrupts_total": {"double": {"aggregatable": True, "time_series_metric": "gauge"}},
            "instance": {"keyword": {"aggregatable": True, "searchable": True, "time_series_dimension": True}},
            "job": {"keyword": {"aggregatable": True, "searchable": True, "time_series_dimension": True}},
            "type": {"keyword": {"aggregatable": True, "searchable": True, "time_series_dimension": True}},
        })
        panel = {
            "title": "Interrupts Detail",
            "type": "timeseries",
            "targets": [
                {
                    "refId": "A",
                    "expr": 'irate(node_interrupts_total{instance="$node",job="$job"}[$__rate_interval])',
                    "legendFormat": "{{ type }} - {{ info }}",
                }
            ],
        }

        yaml_panel, result = self.translate_panel(panel)

        self.assertNotEqual(result.status, "requires_manual")
        query = yaml_panel["esql"]["query"]
        self.assertIn("type", query)
        self.assertNotIn(", info", query)
        self.assertNotIn("COALESCE(info", query)

    def test_scalar_template_variable_in_arithmetic_becomes_literal_with_warning(self):
        self.seed_field_caps({
            "prometheus_target_interval_length_seconds": {
                "double": {"aggregatable": True, "time_series_metric": "gauge"}
            },
            "instance": {"keyword": {"aggregatable": True, "searchable": True, "time_series_dimension": True}},
            "quantile": {"keyword": {"aggregatable": True, "searchable": True, "time_series_dimension": True}},
        })

        result = self.translate(
            'prometheus_target_interval_length_seconds{instance="$instance",quantile="0.99"} - $scrape_interval'
        )

        self.assertEqual(result.feasibility, "feasible")
        self.assertIn(" - 0", result.esql_query)
        self.assertNotIn("?scrape_interval", result.esql_query)
        self.assertNotIn("prometheus.label_scrape_interval.value", result.esql_query)
        self.assertIn(
            "Grafana variable $scrape_interval used as scalar arithmetic value was replaced with literal 0",
            result.warnings,
        )

    def test_filtered_ts_stats_inline_case_inside_timeseries_aggregate(self):
        expr = promql._inline_filters_into_stats_expr(
            "SUM_OVER_TIME(machine_cpu_cores, 5m)",
            ['resource == "cpu"'],
        )

        self.assertEqual(
            expr,
            'SUM_OVER_TIME(CASE((resource == "cpu"), machine_cpu_cores, NULL), 5m)',
        )

    def test_filtered_last_over_time_sum_is_not_inlined_as_regular_aggregate(self):
        expr = promql._inline_filters_into_stats_expr(
            "SUM(LAST_OVER_TIME(kube_pod_container_resource_requests))",
            ['resource == "cpu"'],
            timeseries_window="5m",
        )

        self.assertIsNone(expr)

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

    def test_static_string_label_filter_is_dropped_when_target_field_is_numeric(self):
        """Node Exporter labels like `device`/`fstype` can collide with numeric
        target metric fields. Emitting string predicates against those fields
        makes the migrated panel fail at runtime, so drop the filter with a
        semantic-loss warning instead."""
        self.seed_field_caps({
            "node_filesystem_size_bytes": {"float": {"aggregatable": True, "searchable": True}},
            "device": {"float": {"aggregatable": True, "searchable": True}},
        })

        translated = self.translate('node_filesystem_size_bytes{device!~"rootfs"}')

        self.assertNotIn("device RLIKE", translated.esql_query)
        self.assertIn("node_filesystem_size_bytes", translated.esql_query)
        self.assertIn("Dropped label filters with incompatible target field types during migration", translated.warnings)

    def test_total_suffix_keeps_rate_and_warns_when_live_field_is_plain_numeric(self):
        """Counter-only rate() wins over live caps: the telemetry contract
        locks rate()-ed fields as counters (seed-sample-data ingests them as
        counter_double), so degrading to AVG_OVER_TIME here bakes in a
        translation that hard-fails once the ingest follows the contract.
        Keep RATE and surface the live-caps disagreement as a warning that
        points at the ingest fix (or the explicit rule-pack gauge pin)."""
        self.seed_field_caps({
            "node_cpu_guest_seconds_total": {"float": {"aggregatable": True, "searchable": True}},
        })

        translated = self.translate("sum(rate(node_cpu_guest_seconds_total[5m]))")

        self.assertIn("RATE(node_cpu_guest_seconds_total", translated.esql_query)
        self.assertNotIn("AVG_OVER_TIME(node_cpu_guest_seconds_total", translated.esql_query)
        self.assertTrue(
            any("currently types this field as gauge" in w for w in translated.warnings),
            f"expected a target-disagreement warning, got: {translated.warnings}",
        )

    def test_binary_ratio_keeps_irate_and_warns_per_operand(self):
        """Counter/gauge decisions must apply per operand in binary formulas:
        both operands keep their source-faithful IRATE, and the live-caps
        disagreement warning fires only for the operand the target refutes."""
        self.seed_field_caps({
            "node_cpu_guest_seconds_total": {"float": {"aggregatable": True, "searchable": True}},
            "node_cpu_seconds_total": {"double": {"aggregatable": True, "searchable": True, "time_series_metric": "counter"}},
            "instance": {
                "keyword": {"aggregatable": True, "searchable": True, "time_series_dimension": True}
            },
            "mode": {
                "keyword": {"aggregatable": True, "searchable": True, "time_series_dimension": True}
            },
        })

        translated = self.translate(
            'sum by(instance) (irate(node_cpu_guest_seconds_total{instance="$node",job="$job", mode="user"}[1m])) '
            '/ on(instance) group_left sum by (instance)((irate(node_cpu_seconds_total{instance="$node",job="$job"}[1m])))'
        )

        self.assertIn("IRATE(node_cpu_guest_seconds_total", translated.esql_query)
        self.assertIn("IRATE(node_cpu_seconds_total", translated.esql_query)
        self.assertNotIn("AVG_OVER_TIME", translated.esql_query)
        disagreements = [w for w in translated.warnings if "currently types this field as gauge" in w]
        self.assertEqual(len(disagreements), 1, f"expected one disagreement warning, got: {translated.warnings}")
        self.assertIn("node_cpu_guest_seconds_total", disagreements[0])

    def test_conflicting_group_field_is_dropped_when_target_wildcard_cannot_group_it(self):
        """A broad metrics-* data view can contain a label in one stream and a
        metric in another. ES|QL TS rejects grouping on that field, so keep the
        usable group dimensions and warn about the dropped one."""
        self.seed_field_caps({
            "node_memory_MemTotal_bytes": {
                "double": {"aggregatable": True, "searchable": True, "time_series_metric": "gauge"}
            },
            "instance": {
                "keyword": {"aggregatable": True, "searchable": True, "time_series_dimension": True}
            },
            "job": {
                "keyword": {
                    "aggregatable": True,
                    "searchable": True,
                    "time_series_dimension": True,
                    "indices": [".ds-metrics-prometheus-default-000001"],
                },
                "double": {
                    "aggregatable": True,
                    "searchable": True,
                    "time_series_metric": "gauge",
                    "indices": [".ds-metrics-generic-default-000001"],
                },
            },
        })

        translated = self.translate("avg(node_memory_MemTotal_bytes) by (instance, job)")

        self.assertIn("BY time_bucket = TBUCKET(5 minute), instance", translated.esql_query)
        self.assertNotIn(", job", translated.esql_query)
        self.assertIn("Dropped grouping fields with incompatible target field types during migration", translated.warnings)

    def test_clamp_wrapper_uses_real_output_field_when_panel_drops_unmigrated_target(self):
        panel = {
            "id": 22,
            "title": "$node_name - Overall CPU Utilization",
            "type": "graph",
            "targets": [
                {
                    "refId": "A",
                    "expr": (
                        'clamp_max(avg by (node_name,mode) ((avg by (mode) ( '
                        '(clamp_max(rate(node_cpu_seconds_total{node_name=~"$node_name",mode!="idle"}[$interval]),1)) '
                        'or (clamp_max(irate(node_cpu_seconds_total{node_name=~"$node_name",mode!="idle"}[5m]),1)) '
                        '))*100 or (max_over_time(node_cpu_average{node_name=~"$node_name",mode=~"user|system|wait|steal|irq|nice"}[$interval]) '
                        'or max_over_time(node_cpu_average{node_name=~"$node_name", mode=~"user|system|wait|steal|irq|nice"}[5m]))),100)'
                    ),
                    "legendFormat": "{{mode}}",
                },
                {
                    "refId": "B",
                    "expr": (
                        'clamp_max(max by () ((sum by (cpu) ( '
                        '(clamp_max(rate(node_cpu_seconds_total{node_name=~"$node_name",mode!="idle",mode!="iowait"}[$interval]),1)) '
                        'or (clamp_max(irate(node_cpu_seconds_total{node_name=~"$node_name",mode!="idle",mode!="iowait"}[5m]),1)) '
                        ')*100) or (max_over_time(node_cpu_average{node_name=~"$node_name", mode=~"user|system|wait|steal|irq|nice"}[$interval]) '
                        'or max_over_time(node_cpu_average{node_name=~"$node_name", mode=~"user|system|wait|steal|irq|nice"}[5m]))),100)'
                    ),
                    "legendFormat": "Max Core Utilization",
                },
            ],
        }

        yaml_panel, result = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]

        self.assertEqual(result.status, "migrated_with_warnings")
        self.assertNotIn("EVAL value = LEAST(value", query)
        self.assertIn("EVAL node_cpu_seconds_total = LEAST(node_cpu_seconds_total, 100)", query)

    def test_topk_avg_by_range_fallback_keeps_ranking_shape(self):
        translated = self.translate(
            'topk(5,(avg by (service_name) ('
            'max_over_time(mysql_global_status_max_used_connections{service_name=~"$service_name"}[$interval]) '
            'or max_over_time(mysql_global_status_max_used_connections{service_name=~"$service_name"}[5m])'
            ')))'
        )

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("STATS _bucket_value = AVG(MAX_OVER_TIME(mysql_global_status_max_used_connections, 5m))", translated.esql_query)
        self.assertIn("STATS value = LAST(_bucket_value, time_bucket) BY service.name", translated.esql_query)
        self.assertIn("LIMIT 5", translated.esql_query)
        self.assertIn("Translated grouped topk() as latest-bucket ES|QL top N", translated.warnings)

    def test_grouped_rate_inside_formula_is_wrapped_for_ts_validation(self):
        translated = self.translate(
            '(sum(rate(process_cpu_seconds_total{job=~".*exporter.*",node_name=~"$node_name"}[$interval]) '
            'or irate(process_cpu_seconds_total{job=~".*exporter.*",node_name=~"$node_name"}[5m])) by (node_name)) '
            '/ count(node_cpu_seconds_total{job=~".*exporter.*",node_name=~"$node_name"}) by (node_name) * 100'
        )

        self.assertEqual(translated.feasibility, "feasible")
        self.assertIn("AVG(RATE(process_cpu_seconds_total, 5m))", translated.esql_query)
        self.assertNotIn("= RATE(process_cpu_seconds_total, 5m) BY", translated.esql_query)

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

    def test_resolver_reports_offline_schema_discovery_status(self):
        resolver = migrate.SchemaResolver(self.rule_pack)

        resolver.schema_profile()

        self.assertEqual(
            resolver.discovery_status(),
            {"status": "offline", "error": "", "field_count": 0},
        )

    def test_resolver_reports_empty_schema_discovery_status(self):
        resolver = migrate.SchemaResolver(self.rule_pack, es_url="https://example.es")
        response = mock.Mock(status_code=200)
        response.json.return_value = {"fields": {}}

        with mock.patch.object(schema.requests, "get", return_value=response):
            resolver.schema_profile()

        self.assertEqual(
            resolver.discovery_status(),
            {"status": "empty", "error": "", "field_count": 0},
        )

    def test_resolver_reports_failed_schema_discovery_status(self):
        resolver = migrate.SchemaResolver(self.rule_pack, es_url="https://example.es")
        response = mock.Mock(status_code=401, text="Unauthorized")

        with mock.patch.object(schema.requests, "get", return_value=response):
            resolver.schema_profile()

        self.assertEqual(resolver.discovery_status()["status"], "error")
        self.assertIn("401", resolver.discovery_status()["error"])

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

    # --- prometheus_native profile (/_prometheus/api/v1/write endpoint) ---

    def test_resolver_detects_prometheus_native_profile(self):
        """Profile detection: `metrics.*` + `labels.*` fields trigger the
        `prometheus_native` profile (native /_prometheus endpoint layout)."""
        self.seed_field_caps({
            "metrics.http_requests_total": {"double": {"aggregatable": True, "time_series_metric": "counter"}},
            "metrics.process_cpu_seconds_total": {"double": {"aggregatable": True, "time_series_metric": "counter"}},
            "labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "labels.job": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
        })
        self.assertEqual(self.resolver.schema_profile(), "prometheus_native")

    def test_prometheus_native_profile_requires_both_metrics_and_labels(self):
        """Native profile is NOT triggered by `metrics.*` alone — `labels.*` is
        also required to avoid false-positives from arbitrary custom indices."""
        self.seed_field_caps({
            "metrics.http_requests_total": {"double": {"aggregatable": True, "time_series_metric": "counter"}},
            "instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
        })
        self.assertIsNone(self.resolver.schema_profile())

    def test_prometheus_remote_write_profile_wins_over_native_when_both_present(self):
        """Fleet profile takes priority when both Fleet and native patterns coexist."""
        self.seed_field_caps({
            "prometheus.labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "prometheus.http_requests_total.counter": {"long": {"aggregatable": True, "time_series_metric": "counter"}},
            "metrics.http_requests_total": {"double": {"aggregatable": True, "time_series_metric": "counter"}},
            "labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
        })
        self.assertEqual(self.resolver.schema_profile(), "prometheus_remote_write")

    def test_resolve_metric_field_prefixes_metrics_dot_for_native_profile(self):
        self.seed_field_caps({
            "metrics.http_requests_total": {"double": {"aggregatable": True, "time_series_metric": "counter"}},
            "labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
        })
        self.assertEqual(
            self.resolver.resolve_metric_field("http_requests_total", prefer="counter"),
            "metrics.http_requests_total",
        )
        # prefer is irrelevant for native layout (no suffix variants) — always prefixed
        self.assertEqual(
            self.resolver.resolve_metric_field("process_cpu_seconds_total", prefer="gauge"),
            "metrics.process_cpu_seconds_total",
        )

    def test_resolve_label_namespaces_to_labels_dot_for_native_profile(self):
        self.seed_field_caps({
            "metrics.http_requests_total": {"double": {"aggregatable": True, "time_series_metric": "counter"}},
            "labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "labels.job": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
        })
        self.assertEqual(self.resolver.resolve_label("instance"), "labels.instance")
        self.assertEqual(self.resolver.resolve_label("job"), "labels.job")
        self.assertEqual(self.resolver.resolve_control_field("instance"), "labels.instance")

    def test_is_counter_uses_metrics_prefix_field_cap_for_native_profile(self):
        """is_counter() must check `metrics.<name>` capability, not bare name."""
        self.seed_field_caps({
            "metrics.http_requests_total": {"double": {"aggregatable": True, "time_series_metric": "counter"}},
            "metrics.process_resident_memory_bytes": {"double": {"aggregatable": True, "time_series_metric": "gauge"}},
            "labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
        })
        self.assertTrue(self.resolver.is_counter("http_requests_total"))
        self.assertFalse(self.resolver.is_counter("process_resident_memory_bytes"))

    def test_is_counter_respects_native_profile_gauge_cap_for_total_suffix(self):
        self.seed_field_caps({
            "metrics.http_requests_total": {"double": {"aggregatable": True, "time_series_metric": "gauge"}},
            "labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
        })

        self.assertFalse(self.resolver.is_counter("http_requests_total"))

    def test_translator_emits_metrics_and_labels_prefixed_fields_for_native_profile(self):
        """End-to-end: counter rate against a native /_prometheus endpoint target
        must produce ES|QL referencing `metrics.*` metric fields and `labels.*`
        dimension fields, never bare names or prometheus.* nesting."""
        self.seed_field_caps({
            "metrics.http_requests_total": {"double": {"aggregatable": True, "time_series_metric": "counter"}},
            "labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            "labels.method": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
        })
        translated = self.translate(
            'sum(rate(http_requests_total{instance="i-1"}[5m])) by (instance, method)'
        )
        self.assertIn("metrics.http_requests_total", translated.esql_query)
        self.assertIn('labels.instance == "i-1"', translated.esql_query)
        self.assertIn("labels.instance", translated.esql_query)
        self.assertIn("labels.method", translated.esql_query)
        self.assertNotIn("prometheus.labels.", translated.esql_query)
        self.assertNotIn("prometheus.http_requests_total", translated.esql_query)

    def test_resolve_label_returns_labels_prefix_even_when_label_not_in_cache(self):
        """For native profile: labels not yet observed in the field cache must
        still resolve to `labels.<name>`, not fall through to wrong OTel candidates
        (e.g. service.instance.id) which don't exist in this layout."""
        self.seed_field_caps({
            # Only one metric field — enough to trigger native profile detection
            # once a labels.* field is also present.
            "metrics.http_requests_total": {"double": {"aggregatable": True, "time_series_metric": "counter"}},
            "labels.job": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            # NOTE: labels.instance deliberately NOT present in cache.
        })
        # Must return labels.instance, not service.instance.id or bare 'instance'.
        self.assertEqual(self.resolver.resolve_label("instance"), "labels.instance")
        self.assertEqual(self.resolver.resolve_label("namespace"), "labels.namespace")
        self.assertEqual(self.resolver.resolve_label("unknown_label"), "labels.unknown_label")

    def test_build_discovered_mappings_skipped_for_native_profile(self):
        """_build_discovered_mappings must not populate OTel entries for native
        profile — native indices have no OTel fields and scanning them is wasted
        work that could also produce stale fallbacks."""
        self.seed_field_caps({
            "metrics.http_requests_total": {"double": {"aggregatable": True, "time_series_metric": "counter"}},
            "labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
            # Simulate an OTel field that happens to be in the cache (edge case):
            # even if present, it must NOT be recorded as a discovered mapping.
            "service.instance.id": {"keyword": {"aggregatable": True}},
        })
        # Explicitly invoke _build_discovered_mappings the way _discover_fields does.
        self.resolver._build_discovered_mappings()
        # No OTel candidates should have been mapped.
        self.assertEqual(self.resolver._discovered_mappings, {})
        # resolve_label must still return the namespaced form, not the OTel field.
        self.assertEqual(self.resolver.resolve_label("instance"), "labels.instance")

    def test_translator_gauge_metric_uses_ts_with_metrics_prefix_for_native_profile(self):
        """Gauge metrics in native profile must use TS (issue #8: FROM against a
        TSDS sums every per-sample doc and inflates the value) and still
        reference the `metrics.` prefixed field name."""
        self.seed_field_caps({
            "metrics.process_resident_memory_bytes": {"double": {"aggregatable": True, "time_series_metric": "gauge"}},
            "labels.instance": {"keyword": {"aggregatable": True, "time_series_dimension": True}},
        })
        translated = self.translate(
            'avg(process_resident_memory_bytes) by (instance)'
        )
        self.assertIn("metrics.process_resident_memory_bytes", translated.esql_query)
        self.assertIn("TS metrics-*", translated.esql_query)
        # TS source command uses TBUCKET; FROM uses BUCKET — presence of TBUCKET
        # confirms the gauge metric correctly took the TS path.
        self.assertIn("TBUCKET", translated.esql_query)
        self.assertNotIn("FROM metrics-*", translated.esql_query)

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

    def test_set_or_between_same_metric_with_disjoint_filters_unifies_where(self):
        """``A{f1} or A{f2}`` over the same metric is set union; rewrite as a
        single ``WHERE (f1 OR f2)`` to preserve every series.

        Parity finding: ``http_requests_total{status=~"4.."} or
        http_requests_total{status=~"5.."}`` previously translated to an
        ES|QL pipeline that filtered only the left side and dropped both
        the right operand and every breakdown label, collapsing 18 series
        into a single empty-label row.
        """
        expr = (
            'http_requests_total{instance="i",status=~"4.."} '
            'or http_requests_total{instance="i",status=~"5.."}'
        )
        translated = self.translate(expr)
        self.assertEqual(translated.feasibility, "feasible")
        esql = translated.esql_query
        # Both filters must survive the rewrite.
        self.assertIn('"4.."', esql)
        self.assertIn('"5.."', esql)
        # And both must appear inside the same WHERE OR clause, not be
        # silently dropped.
        where_lines = [line for line in esql.splitlines() if line.lstrip().startswith("| WHERE")]
        joined_where = "\n".join(where_lines)
        self.assertIn('"4.."', joined_where)
        self.assertIn('"5.."', joined_where)

    def test_set_or_between_different_metrics_uses_left_operand_fallback(self):
        """``A or B`` between two different metrics now translates the left
        operand with an explicit fallback warning rather than refusing."""
        translated = self.translate(
            "http_requests_total or http_other_total",
        )
        self.assertNotEqual(translated.feasibility, "not_feasible")
        self.assertIn("http_requests_total", translated.esql_query or "")
        reasons = " ".join(getattr(translated, "warnings", []) or [])
        self.assertRegex(reasons, r"(?i)or.*fallback|fallback.*or|left operand")

    def test_set_and_between_metrics_is_not_feasible(self):
        translated = self.translate("http_requests_total and http_other_total")
        self.assertEqual(translated.feasibility, "not_feasible")

    def test_set_unless_between_metrics_is_not_feasible(self):
        translated = self.translate("http_requests_total unless http_other_total")
        self.assertEqual(translated.feasibility, "not_feasible")

    def test_set_or_same_metric_preserves_legend_driven_breakdowns(self):
        """When the panel's legend format declares breakdown labels, the
        set-or rewrite must keep them in BY so we get one series per
        label tuple (the parity-rig finding for the 4xx-or-5xx panel)."""
        expr = (
            'http_requests_total{instance="i",status=~"4.."} '
            'or http_requests_total{instance="i",status=~"5.."}'
        )
        translated = self.translate(
            expr,
            translation_hints={
                "preferred_group_labels": ["method", "path", "status"],
                "preferred_group_labels_origin": "legend",
            },
        )
        self.assertEqual(translated.feasibility, "feasible")
        esql = translated.esql_query
        self.assertIn('"4.."', esql)
        self.assertIn('"5.."', esql)
        stats_lines = [
            line for line in esql.splitlines() if line.lstrip().startswith("| STATS")
        ]
        self.assertTrue(stats_lines, f"no STATS line found in:\n{esql}")
        joined_stats = "\n".join(stats_lines)
        for label in ("method", "path", "status"):
            self.assertIn(label, joined_stats)

    def test_rate_on_gauge_typed_field_keeps_irate_and_warns(self):
        """Some Prometheus counters don't end in ``_total`` (kernel vmstat,
        netstat, etc.) and a stale or heuristic ingest can type them as
        ``gauge`` in live field caps. rate()/irate() are counter-only in
        PromQL, and the telemetry contract locks rate()-ed fields as
        counters (seed-sample-data ingests them as ``counter_double``), so
        degrading to AVG_OVER_TIME on live caps bakes in a translation that
        hard-fails once the ingest follows the contract - and silently
        changes the panel's value scale meanwhile.

        Keep the source-faithful IRATE and surface the live-caps
        disagreement as a warning pointing at the ingest fix; an explicit
        rule-pack ``metric_kinds: gauge`` pin remains the escape hatch for
        targets where the gauge typing is intentional (see
        TestCounterOnlyRangeFuncTrustsSource in test_grafana_extended).
        """
        self.seed_field_caps({
            "node_vmstat_pgpgin": {
                "double": {
                    "type": "double",
                    "searchable": True,
                    "aggregatable": True,
                    "time_series_metric": "gauge",
                },
            },
        })

        translated = self.translate("irate(node_vmstat_pgpgin[5m])")
        esql = translated.esql_query

        self.assertIn("IRATE(node_vmstat_pgpgin", esql)
        self.assertNotIn("AVG_OVER_TIME(node_vmstat_pgpgin", esql)

        # A loud warning explains the source-vs-target disagreement.
        self.assertTrue(
            any("currently types this field as gauge" in w for w in translated.warnings),
            f"expected a target-disagreement warning, got: {translated.warnings}",
        )

    def test_rate_on_suffixless_counter_emits_rate_when_target_unknown(self):
        """rate()/irate() are counter-only in PromQL: a gauge cannot be rated.
        When the metric has no counter-name suffix (node_vmstat_oom_kill,
        node_netstat_Icmp_InErrors) AND the target schema offers no proof it is
        a gauge (offline migrate, or field absent from caps), the source rate()
        is authoritative -> emit a true RATE. This pairs with the telemetry
        contract seeding the field as counter, so the emitted ES|QL is valid and
        numerically correct instead of collapsing to AVG_OVER_TIME (~0.998 err).
        """
        # No field caps seeded -> resolver cannot prove gauge (offline case).
        translated = self.translate("rate(node_vmstat_oom_kill[5m])")
        esql = translated.esql_query
        self.assertIn("RATE(node_vmstat_oom_kill", esql)
        self.assertNotIn("AVG_OVER_TIME(node_vmstat_oom_kill", esql)

    def test_irate_on_suffixless_counter_emits_irate_when_target_unknown(self):
        translated = self.translate("irate(node_netstat_Icmp_InErrors[5m])")
        esql = translated.esql_query
        self.assertIn("IRATE(node_netstat_Icmp_InErrors", esql)
        self.assertNotIn("AVG_OVER_TIME(node_netstat_Icmp_InErrors", esql)

    def test_rate_still_degrades_when_rule_pack_pins_gauge(self):
        """Honest degradation is preserved behind an explicit, user-asserted
        signal: a rule-pack ``metric_kinds: gauge`` pin degrades rate() to
        AVG_OVER_TIME (with the loud "rendered as" warning). Live caps alone
        no longer degrade - they can be stale and contradict the telemetry
        contract's counter lock (see
        test_rate_on_gauge_typed_field_keeps_irate_and_warns)."""
        self.rule_pack.metric_kinds["node_vmstat_oom_kill"] = "gauge"
        self.seed_field_caps({
            "node_vmstat_oom_kill": {
                "double": {
                    "type": "double",
                    "searchable": True,
                    "aggregatable": True,
                    "time_series_metric": "gauge",
                },
            },
        })
        translated = self.translate("rate(node_vmstat_oom_kill[5m])")
        esql = translated.esql_query
        self.assertNotIn("RATE(node_vmstat_oom_kill", esql)
        self.assertIn("AVG_OVER_TIME(node_vmstat_oom_kill", esql)
        self.assertTrue(
            any("rendered as AVG_OVER_TIME" in w for w in translated.warnings),
            f"expected the degrade warning, got: {translated.warnings}",
        )

    def test_increase_on_suffixless_metric_still_degrades_when_target_unknown(self):
        """increase() is NOT forced to counter the way rate()/irate() are: it can
        be misused on a real gauge, and ES|QL INCREASE() also requires counter
        typing. With no counter suffix and no target proof, keep the conservative
        gauge degradation (consistent with the contract's soft-counter treatment
        of increase())."""
        translated = self.translate("increase(weird_unknown_metric[5m])")
        esql = translated.esql_query
        self.assertNotIn("INCREASE(weird_unknown_metric", esql)
        self.assertIn("MAX_OVER_TIME(weird_unknown_metric", esql)

    def test_rate_on_counter_typed_field_still_uses_RATE(self):
        """Regression guard: degradation must only fire for gauge-typed
        fields, not for proper counters."""
        self.seed_field_caps({
            "http_requests_total": {
                "double": {
                    "type": "double",
                    "searchable": True,
                    "aggregatable": True,
                    "time_series_metric": "counter",
                },
            },
        })

        translated = self.translate("rate(http_requests_total[5m])")
        esql = translated.esql_query
        self.assertIn("RATE(http_requests_total", esql)

    def test_native_promql_gate_skips_counter_func_on_gauge(self):
        """When the source PromQL applies a counter-style range function
        to a gauge-typed field, the panel-level native-PROMQL gate must
        fall through to ES|QL translation (where the gauge fallback can
        degrade honestly) instead of emitting a ``PROMQL value=(irate(X))``
        that hard-fails at render time."""
        from observability_migration.adapters.source.grafana.panels import (
            _native_promql_has_counter_func_on_gauge,
        )
        self.seed_field_caps({
            "node_vmstat_oom_kill": {
                "double": {
                    "type": "double",
                    "searchable": True,
                    "aggregatable": True,
                    "time_series_metric": "gauge",
                },
            },
            "http_requests_total": {
                "double": {
                    "type": "double",
                    "searchable": True,
                    "aggregatable": True,
                    "time_series_metric": "counter",
                },
            },
        })
        # Counter-style range function on a gauge-typed field: gate fires.
        self.assertTrue(_native_promql_has_counter_func_on_gauge(
            "irate(node_vmstat_oom_kill[5m])", self.resolver,
        ))
        # Counter-style range function on a counter-typed field: gate
        # doesn't fire; native PROMQL is still preferred.
        self.assertFalse(_native_promql_has_counter_func_on_gauge(
            "rate(http_requests_total[5m])", self.resolver,
        ))
        # No counter-style range function: gate doesn't fire.
        self.assertFalse(_native_promql_has_counter_func_on_gauge(
            "avg_over_time(node_vmstat_oom_kill[5m])", self.resolver,
        ))

    def test_set_or_same_metric_promotes_distinguishing_labels_to_BY(self):
        """When the operands of ``A{X=~"a"} or A{X=~"b"}`` differ on
        label X, the rewrite must add X to BY even if the legend doesn't
        mention it. Otherwise the rate is averaged across X-values and
        the union loses the per-X series PromQL would have produced.
        """
        expr = (
            'http_requests_total{instance="i",status=~"4.."} '
            'or http_requests_total{instance="i",status=~"5.."}'
        )
        # Caller asks to group by method+path only (legend format omits
        # status, mirroring the express-prometheus-middleware panel).
        translated = self.translate(
            expr,
            translation_hints={
                "preferred_group_labels": ["method", "path"],
                "preferred_group_labels_origin": "legend",
            },
        )
        self.assertEqual(translated.feasibility, "feasible")
        stats_lines = [
            line for line in translated.esql_query.splitlines()
            if line.lstrip().startswith("| STATS")
        ]
        joined_stats = "\n".join(stats_lines)
        # The differing label must be promoted to BY despite the legend
        # omitting it; otherwise the rate would be averaged across
        # statuses and PromQL parity would degrade.
        for label in ("method", "path", "status"):
            self.assertIn(label, joined_stats)

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

        # known_gauge is a proven TSDS gauge (TS); unknown_gauge now also assumes TSDS
        # (migration default) -> both operands share TS, so the arithmetic stays on TS
        # instead of being demoted to a common FROM. AVG is multiplicity-invariant so the
        # result is correct either way; TS additionally avoids inflating any SUM/COUNT.
        self.assertNotEqual(translated.feasibility, "not_feasible")
        self.assertIn("TS metrics-*", translated.esql_query)
        self.assertIn("AVG(known_gauge)", translated.esql_query)
        self.assertIn("AVG(unknown_gauge)", translated.esql_query)

    def test_mixed_known_and_unknown_gauge_arithmetic_demotes_to_from_when_opt_out(self):
        # With assume_tsds_gauges=False, unknown_gauge stays FROM while known_gauge is a
        # proven TSDS gauge (TS); mixed sources are reconciled down to a common FROM.
        self.rule_pack.assume_tsds_gauges = False
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
        # The TS command forbids COUNT(*); count the filtered metric field
        # instead so the query is valid at runtime (not just offline-feasible).
        stats_idx = translated.esql_query.index("| STATS up_count = COUNT(up)")
        self.assertLess(where_idx, stats_idx)
        self.assertNotIn("COUNT(*)", translated.esql_query)
        self.assertNotIn("| WHERE up_count == 1", translated.esql_query)

    def test_xy_panel_with_extra_grouping_dimension_warns(self):
        # A query grouped by two non-time dimensions can only show one as the XY
        # breakdown; the dropped dimension must be surfaced, not hidden.
        panel = {
            "title": "Pods",
            "type": "timeseries",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"refId": "A", "expr": "sum(kube_pod_info) by (namespace, pod)"}],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(result.status, "migrated_with_warnings")
        # Only one field is used as the visual breakdown.
        self.assertIn("breakdown", yaml_panel["esql"])
        self.assertTrue(
            any("not on the chart" in w for w in result.reasons),
            f"Expected dropped-dimension warning, got {result.reasons}",
        )

    def test_xy_panel_with_single_grouping_dimension_does_not_warn(self):
        # A single non-time grouping dimension maps cleanly to the breakdown;
        # there must be no dropped-dimension warning.
        panel = {
            "title": "Requests",
            "type": "timeseries",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"refId": "A", "expr": 'sum(rate(http_requests_total[5m])) by (job)'}],
        }
        _, result = self.translate_panel(panel)
        self.assertFalse(
            any("not on the chart" in w for w in result.reasons),
            f"Did not expect dropped-dimension warning, got {result.reasons}",
        )

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

    def test_topk_without_labels_translates_with_fallback(self):
        # Ungrouped topk now uses single-bucket LIMIT fallback
        translated = self.translate("topk(5, rate(foo_total[5m]))")
        self.assertNotEqual(translated.feasibility, "not_feasible", translated.warnings)
        self.assertIn("LIMIT 5", translated.esql_query)

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

    def test_stdvar_aggregation_is_marked_not_feasible(self):
        # stdvar() is population variance; ES|QL has no variance aggregation and
        # STATS cannot square STDDEV() inline. It must not silently degrade to AVG.
        translated = self.translate("stdvar(node_cpu_seconds_total) by (instance)")
        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertTrue(
            any("stdvar" in w for w in translated.warnings),
            f"Expected stdvar warning, got {translated.warnings}",
        )
        self.assertNotIn("AVG(", translated.esql_query or "")

    def test_group_aggregation_is_marked_not_feasible(self):
        # group() returns the constant 1 per series group (label-set extraction),
        # discarding the value. Translating it as AVG(metric) returns real values
        # and is semantically wrong, so it must degrade to not_feasible.
        translated = self.translate("group(up) by (job)")
        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertTrue(
            any("group" in w for w in translated.warnings),
            f"Expected group warning, got {translated.warnings}",
        )
        self.assertNotIn("AVG(up)", translated.esql_query or "")

    def test_stddev_aggregation_uses_valid_esql_std_dev_function(self):
        # ES|QL's standard-deviation aggregation is STD_DEV (with an underscore);
        # STDDEV does not exist and is rejected by the cluster. The translator
        # must emit the valid function name.
        translated = self.translate("stddev by (job) (http_request_duration_seconds)")
        self.assertNotEqual(translated.feasibility, "not_feasible", translated.warnings)
        self.assertIn("STD_DEV(", translated.esql_query or "")
        # The invalid no-underscore form must never be emitted.
        import re as _re

        self.assertIsNone(
            _re.search(r"\bSTDDEV\s*\(", translated.esql_query or ""),
            f"Emitted invalid STDDEV function: {translated.esql_query}",
        )

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

    def test_unknown_gauge_selector_assumes_tsds_direct_ts(self):
        # Migration default: an unproven bare gauge assumes TSDS -> TS direct-gauge
        # (STATS field = field), which preserves per-series rows. No FROM+AVG collapse,
        # so no honest-loss warning (the series are retained, nothing is dropped).
        translated = self.translate("node_systemd_units")

        self.assertEqual(translated.source_type, "TS")
        self.assertIn("TS metrics-*", translated.esql_query)
        self.assertIn(
            "| STATS node_systemd_units = node_systemd_units BY time_bucket = TBUCKET(5 minute)",
            translated.esql_query,
        )
        self.assertNotIn("AVG(node_systemd_units)", translated.esql_query)
        self.assertFalse(any("Collapsed all series" in warning for warning in translated.warnings))

    def test_unknown_gauge_selector_keeps_avg_fallback_when_opt_out(self):
        # Escape hatch: with assume_tsds_gauges=False the bare gauge falls back to the
        # FROM+AVG collapse and the honest loss warning fires.
        self.rule_pack.assume_tsds_gauges = False
        translated = self.translate("node_systemd_units")

        self.assertEqual(translated.source_type, "FROM")
        self.assertIn("FROM metrics-*", translated.esql_query)
        self.assertIn("AVG(node_systemd_units)", translated.esql_query)
        self.assertTrue(any("Collapsed all series" in warning for warning in translated.warnings))

    def test_issue8_simple_sum_gauge_on_proven_tsds_uses_ts(self):
        # Issue #8: sum(gauge_metric) on a TSDS must emit TS, not FROM. With FROM
        # against a TSDS, SUM(field) aggregates every per-sample doc in the bucket,
        # giving inflated results (e.g. ~120 GB on a 8 GB host across one 5-min
        # bucket). With TS, SUM aggregates one value per series within each bucket.
        self.seed_field_caps(
            {
                "node_memory_MemTotal_bytes": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                }
            }
        )

        translated = self.translate("sum(node_memory_MemTotal_bytes)")

        self.assertEqual(translated.source_type, "TS")
        self.assertIn("TS metrics-*", translated.esql_query)
        self.assertIn("SUM(node_memory_MemTotal_bytes)", translated.esql_query)
        self.assertIn("TBUCKET", translated.esql_query)
        self.assertNotIn("FROM metrics-*", translated.esql_query)

    def test_issue8_simple_avg_gauge_grouped_on_proven_tsds_uses_ts(self):
        # avg by(instance)(gauge_metric) on TSDS must emit TS with grouping.
        self.seed_field_caps(
            {
                "node_memory_MemTotal_bytes": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                }
            }
        )

        translated = self.translate("avg by (instance) (node_memory_MemTotal_bytes)")

        self.assertEqual(translated.source_type, "TS")
        self.assertIn("TS metrics-*", translated.esql_query)
        self.assertIn("AVG(node_memory_MemTotal_bytes)", translated.esql_query)
        self.assertIn("instance", translated.esql_query)
        self.assertIn("TBUCKET", translated.esql_query)
        self.assertNotIn("FROM metrics-*", translated.esql_query)

    def test_issue8_bare_gauge_with_preferred_grouping_on_proven_tsds_uses_ts(self):
        # Bare gauge selector that picks up panel-level preferred_group_labels
        # (e.g. from a `{{instance}}` legendFormat) must use TS — PR #19's
        # ``_can_use_direct_ts_gauge`` returns False as soon as group_fields are
        # present. The fix relaxes that for proven TSDS gauges since TS supports
        # ``BY TBUCKET(...), label`` grouping.
        self.seed_field_caps(
            {
                "node_memory_MemTotal_bytes": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                }
            }
        )

        translated = self.translate(
            "node_memory_MemTotal_bytes",
            panel_type="timeseries",
            translation_hints={"preferred_group_labels": ["instance"]},
        )

        self.assertEqual(translated.source_type, "TS")
        self.assertIn("TS metrics-*", translated.esql_query)
        self.assertIn("instance", translated.esql_query)
        self.assertNotIn("FROM metrics-*", translated.esql_query)

    def test_issue8_multi_target_gauge_fusion_uses_ts(self):
        # Multi-target gauge fusion (translate.py: _build_multi_target_series_query)
        # disables ``allow_direct_ts_gauge`` because ``STATS field = field`` cannot
        # be CASE-wrapped per target. But ``AVG(field)`` can — so proven TSDS
        # gauges must still take the TS path to avoid per-sample inflation.
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
            "id": 204,
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

        self.assertEqual(result.query_ir["source_type"], "TS")
        self.assertIn("TS metrics-*", query)
        self.assertIn("TBUCKET", query)
        self.assertNotIn("FROM metrics-*", query)
        # Per-target CASE filters must still appear so each target keeps its series.
        self.assertIn('CASE((state == "active"', query)
        self.assertIn('CASE((state == "failed"', query)

    def test_reserved_word_legend_alias_is_backtick_quoted(self):
        # A Grafana legendFormat that collides with an ES|QL reserved keyword
        # (here "IN", the membership operator) must be emitted as a
        # backtick-quoted column alias. Without quoting, ES|QL rejects
        # ``EVAL IN = ...`` with ``mismatched input 'IN'`` and the panel 400s
        # in Kibana. Reproduces the HAProxy "Front - Data transfer" panel
        # (legends IN / OUT). "OUT" is not reserved and must stay bare.
        self.seed_field_caps(
            {
                "haproxy_frontend_bytes_in_total": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "counter",
                    }
                },
                "haproxy_frontend_bytes_out_total": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "counter",
                    }
                },
            }
        )
        panel = {
            "id": 301,
            "type": "timeseries",
            "title": "Front - Data transfer",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {
                    "expr": "rate(haproxy_frontend_bytes_in_total[$__rate_interval]) * 8",
                    "refId": "A",
                    "legendFormat": "IN",
                },
                {
                    "expr": "rate(haproxy_frontend_bytes_out_total[$__rate_interval]) * 8",
                    "refId": "B",
                    "legendFormat": "OUT",
                },
            ],
        }
        yaml_panel, _result = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]

        # The reserved word must never appear as a bare ``EVAL`` / ``STATS`` /
        # ``KEEP`` identifier (that is the parse error). It must be backticked.
        self.assertNotRegex(query, r"\bEVAL IN\b")
        self.assertIn("`IN`", query)
        # The non-reserved sibling alias stays unquoted.
        self.assertIn("OUT", query)
        self.assertNotIn("`OUT`", query)

    def test_issue8_pre_agg_filter_on_proven_tsds_gauge_uses_ts(self):
        # Issue #8 (pre_agg_filter path): sum(gauge_metric > threshold) on a
        # proven TSDS gauge must use TS. Under FROM, the WHERE filter still
        # admits every per-sample doc, then SUM inflates the result.
        self.seed_field_caps(
            {
                "node_filesystem_size_bytes": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "gauge",
                    }
                }
            }
        )

        translated = self.translate("sum(node_filesystem_size_bytes > 1000)")

        self.assertEqual(translated.source_type, "TS")
        self.assertIn("TS metrics-*", translated.esql_query)
        self.assertIn("SUM(node_filesystem_size_bytes)", translated.esql_query)
        self.assertIn("node_filesystem_size_bytes > 1000", translated.esql_query)
        self.assertNotIn("FROM metrics-*", translated.esql_query)

    def test_issue8_pre_agg_filter_unknown_gauge_assumes_tsds_ts(self):
        # Migration default (assume_tsds_gauges=True): with no field caps we still
        # assume the target is a TSDS we provisioned, so a gauge sum() pre-agg-filter
        # uses TS. FROM+SUM would inflate by the per-bucket sample count.
        translated = self.translate("sum(unknown_metric > 0)")

        self.assertEqual(translated.source_type, "TS")
        self.assertIn("TS metrics-*", translated.esql_query)
        self.assertIn("SUM(unknown_metric)", translated.esql_query)
        self.assertNotIn("FROM metrics-*", translated.esql_query)

    def test_issue8_unknown_gauge_aggregation_assumes_tsds_ts(self):
        # Migration default: target clusters we set up ingest metrics as TSDS, so a
        # gauge sum() with no field caps assumes TSDS and uses TS (not FROM, which
        # over-counts multi-sample docs). Set assume_tsds_gauges=False to force FROM.
        translated = self.translate("sum(unknown_gauge_metric)")

        self.assertEqual(translated.source_type, "TS")
        self.assertIn("TS metrics-*", translated.esql_query)
        self.assertIn("SUM(unknown_gauge_metric)", translated.esql_query)
        self.assertNotIn("FROM metrics-*", translated.esql_query)

    def test_issue8_unknown_gauge_aggregation_keeps_from_when_opt_out(self):
        # Escape hatch: a deployment targeting a known non-TSDS index can disable the
        # assumption, restoring the conservative FROM fallback for unproven gauges.
        self.rule_pack.assume_tsds_gauges = False
        translated = self.translate("sum(unknown_gauge_metric)")

        self.assertEqual(translated.source_type, "FROM")
        self.assertIn("FROM metrics-*", translated.esql_query)
        self.assertIn("SUM(unknown_gauge_metric)", translated.esql_query)

    def test_issue8_disproven_gauge_keeps_from_even_when_assuming_tsds(self):
        # If the resolver positively DISPROVES TSDS-gauge (conflicting types), respect
        # that and use FROM even with assume_tsds_gauges=True — the assumption only
        # applies when we have no information, never overrides evidence.
        self.seed_field_caps(
            {
                "conflicted_metric": {
                    "double": {"type": "double", "searchable": True, "aggregatable": True},
                    "long": {"type": "long", "searchable": True, "aggregatable": True},
                }
            }
        )
        translated = self.translate("sum(conflicted_metric)")

        self.assertEqual(translated.source_type, "FROM")
        self.assertIn("SUM(conflicted_metric)", translated.esql_query)

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

        # Force the FROM path so this exercises the FROM-panel branch of the
        # blocked-evaluation-not-overridden logic specifically.
        self.rule_pack.assume_tsds_gauges = False
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
        # Bare gauge, no series labels: the default-AVG path is taken and the honest
        # collapse warning is emitted (no explicit aggregation was given in the source).
        self.assertTrue(
            any("Collapsed all series" in warning for warning in translated.warnings)
        )

    def test_collapse_summary_uses_null_safe_aggregate_for_multi_series_ts(self):
        """When the translator generates a multi-target TS query and then
        collapses it down to a single per-bucket row for a gauge/stat
        panel, it must use a null-safe aggregate (not ``LAST``) so values
        that come from different per-series rows in the same bucket are
        all captured.

        Surfaced by reviewing the Node Exporter Full "Pressure" bar
        chart: the panel's three IRATE(...) computations each only emit
        non-null values on their own metric's row inside a bucket. The
        previous ``STATS ... = LAST(field, time_bucket)`` collapse
        picked an arbitrary row, frequently one where the requested
        field was null, leaving the panel rendering all-null bars."""
        from observability_migration.adapters.source.grafana.promql import (
            _collapse_summary_ts_query,
        )
        parts = [
            "TS metrics-*",
            "| WHERE A IS NOT NULL OR B IS NOT NULL OR C IS NOT NULL",
            "| STATS A = IRATE(A, 5m), B = IRATE(B, 5m), C = IRATE(C, 5m) "
            "BY time_bucket = TBUCKET(5 minute)",
        ]
        out = _collapse_summary_ts_query(parts, ["time_bucket"], ["A", "B", "C"])
        self.assertEqual(out, [])
        collapsed = "\n".join(parts)
        # Must not use ``LAST(field, time_bucket)`` here because the
        # upstream rows have one non-null column each. Use a null-safe
        # aggregate instead.
        self.assertNotIn("LAST(A, time_bucket)", collapsed)
        self.assertNotIn("LAST(B, time_bucket)", collapsed)
        self.assertNotIn("LAST(C, time_bucket)", collapsed)
        # Must still collapse to one row per time_bucket.
        self.assertIn("time_bucket = MAX(time_bucket)", collapsed)

    def test_native_promql_empty_legendformat_does_not_dump_ts_tuple(self):
        """When a Grafana panel uses native PROMQL and has no
        ``legendFormat`` (or one with no ``{{label}}`` placeholders), the
        translator used to emit::

            | EVAL _ts = COALESCE(_timeseries, "")
            | EVAL label = CASE(_ts == "", "series", REPLACE(...))
            | KEEP step, value, label

        which renders the legend as the raw stringified label tuple
        (``labels__name__=node_..., cluster=..., instance=..., job=..., replica=A``).
        Surfaced by reviewing screenshots of the uploaded Node Exporter
        Full dashboard - panels like ``Sockstat Used``, ``Memory Bounce``,
        ``Time PLL Adjust`` rendered with that ugly tuple as their only
        legend entry.

        Expected behaviour: emit no synthetic ``label`` column when there's
        no usable legend text, so Lens renders a single unlabeled series
        (mirroring what Grafana itself shows for an empty legendFormat).
        """
        from observability_migration.adapters.source.grafana.panels import (
            build_native_promql_query,
        )
        # No legend_labels (no placeholders) and no static legend text
        # => no synthetic label column.
        query = build_native_promql_query(
            "node_sockstat_sockets_used", index="metrics-*", legend_labels=None,
        )
        self.assertNotIn('EVAL label = CASE', query)
        self.assertNotIn('REPLACE(REPLACE(_ts', query)

    def test_native_promql_ratio_between_distinct_metrics_stays_native(self):
        """A panel computing a ratio/difference between two distinct
        metrics (e.g. the Disk Usage panel
        ``1 - es_fs_path_available_bytes / es_fs_path_total_bytes``)
        must migrate to a native PROMQL lens with the original
        expression preserved — Kibana's PROMQL preview evaluates the
        implicit label-set match natively, so there is no need to fall
        through to a same-bucket ES|QL approximation (issue #138).
        """
        expr = (
            "1 - node_filesystem_avail_bytes{fstype=\"ext4\"} "
            "/ node_filesystem_size_bytes{fstype=\"ext4\"}"
        )
        panel = {
            "title": "Disk Usage",
            "type": "timeseries",
            "targets": [{"refId": "A", "expr": expr}],
        }
        rule_pack = rules.RulePackConfig(native_promql=True)

        yaml_panel, result = panels.translate_panel(
            panel,
            esql_index="metrics-*",
            datasource_index="metrics-*",
            rule_pack=rule_pack,
            resolver=self.resolver,
        )

        query = yaml_panel["esql"]["query"]
        self.assertIn("PROMQL", query)
        # The native command reuses both metric names from the original
        # expression; the ES|QL approximation would collapse them into a
        # single computed_value STATS pipeline instead.
        self.assertIn("node_filesystem_avail_bytes", query)
        self.assertIn("node_filesystem_size_bytes", query)
        self.assertNotIn("STATS", query)
        # No approximation: the same-bucket ES|QL fallback warning must
        # not be attached.
        joined = " ".join(result.notes) + " ".join(result.reasons)
        self.assertNotIn(
            "Approximated PromQL arithmetic using same-bucket ES|QL math",
            joined,
        )

    def test_native_promql_static_legendformat_uses_literal_label(self):
        """A non-empty legendFormat with no placeholders (e.g.
        ``"Pages out ops"``) is a fixed series label. The translator
        should emit ``EVAL label = "Pages out ops"`` and keep that
        column in KEEP so Lens uses the author's text rather than the
        raw label tuple."""
        from observability_migration.adapters.source.grafana.panels import (
            build_native_promql_query,
        )
        query = build_native_promql_query(
            "node_vmstat_pgpgout", index="metrics-*",
            legend_labels=None, legend_format="Pages out ops",
        )
        self.assertIn('EVAL label = "Pages out ops"', query)
        self.assertIn("| KEEP step, value, label", query)
        # Must NOT emit the _ts dump.
        self.assertNotIn('REPLACE(REPLACE(_ts', query)

    def test_same_metric_collapse_handles_regex_and_negated_matchers(self):
        """The Node Exporter Full "CPU Basic" panel has 6 targets that all
        wrap ``node_cpu_seconds_total`` with different ``mode`` matchers,
        mixing equality (``mode="system"``), regex (``mode=~".*irq"``),
        and negation (``mode!="idle"``). The translator used to refuse
        the collapse because of the non-equality matchers, dropping 5
        of 6 targets silently. After the fix, every target's matchers
        contribute a clause to a single combined ``WHERE`` and ``mode``
        is added to ``BY`` so the panel renders one series per resulting
        mode (matching what Grafana shows)."""
        self.seed_field_caps(
            {
                "node_cpu_seconds_total": {
                    "double": {
                        "type": "double",
                        "searchable": True,
                        "aggregatable": True,
                        "time_series_metric": "counter",
                    }
                }
            }
        )
        panel = {
            "id": 200,
            "type": "timeseries",
            "title": "CPU Basic",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": 'sum(irate(node_cpu_seconds_total{mode="system"}[5m]))', "refId": "A"},
                {"expr": 'sum(irate(node_cpu_seconds_total{mode="user"}[5m]))', "refId": "B"},
                {"expr": 'sum(irate(node_cpu_seconds_total{mode=~".*irq"}[5m]))', "refId": "C"},
                {"expr": 'sum(irate(node_cpu_seconds_total{mode!="idle"}[5m]))', "refId": "D"},
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]
        # All four matcher values must contribute to the unified WHERE.
        self.assertIn('"system"', query)
        self.assertIn('"user"', query)
        self.assertIn('".*irq"', query)
        # The negated matcher should appear with a negation operator.
        self.assertIn('"idle"', query)
        # `mode` must be in the BY clause to keep one series per matching
        # mode value (matching the dashboard's intent).
        stats_lines = [ln for ln in query.splitlines() if ln.lstrip().startswith("| STATS")]
        self.assertTrue(stats_lines, f"no STATS line in:\n{query}")
        joined_stats = "\n".join(stats_lines)
        self.assertIn("mode", joined_stats)
        # No "only N could be migrated" warning should appear.
        self.assertFalse(
            any("could be migrated" in r for r in result.reasons),
            f"unexpected drop-target warning in: {result.reasons!r}",
        )

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
        # Issue #8: proven TSDS gauge takes the TS path (TBUCKET), not FROM (BUCKET).
        self.assertIn("BY time_bucket = TBUCKET(5 minute), state", query)
        self.assertNotIn("=  BY state", query)
        self.assertTrue(any("Collapsed 2 same-metric targets into BY state" in r for r in result.reasons))
        self.assertTrue(any("No explicit aggregation" in r for r in result.reasons))
        self.assertFalse(any("only 1 could be migrated" in r for r in result.reasons))
        self.assertEqual(result.query_ir["source_type"], "TS")
        self.assertEqual(result.target_query_contract["canonical_target"], "ts")
        self.assertEqual(result.contract_evaluation["status"], "exact_now")
        self.assertEqual(result.fulfillment_plan["status"], "not_required")
        self.assertEqual(result.fulfillment_plan["actions"], [])
        self.assertIn("TS metrics-*", result.query_ir["target_query"])
        self.assertEqual(
            result.query_ir["source_expression"],
            'node_systemd_units{instance="$node",job="$job",state="active"} ||| '
            'node_systemd_units{instance="$node",job="$job",state="failed"}',
        )

    def test_same_metric_collapse_records_per_target_provenance(self):
        """The same-metric collapse maps targets to LABEL VALUES (one BY column),
        not output columns, so its per-target provenance must carry
        (ref_id, source_expr, label_column, label_value) for equality-matcher
        targets - the parity oracle scopes the translated response to the rows
        whose key carries that value. Without it these panels are stuck at
        SKIP (the last 10 multi-query corpus panels)."""
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
            "id": 101,
            "type": "graph",
            "title": "Systemd Units",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": 'node_systemd_units{instance="$node",job="$job",state="active"}', "refId": "A"},
                {"expr": 'node_systemd_units{instance="$node",job="$job",state="failed"}', "refId": "B"},
            ],
        }
        _yaml_panel, result = self.translate_panel(panel)
        targets = result.query_ir["metadata"].get("collapsed_targets")
        self.assertIsNotNone(targets, "expected per-target provenance in metadata")
        self.assertEqual(
            targets,
            [
                {"ref_id": "A",
                 "source_expr": 'node_systemd_units{instance="$node",job="$job",state="active"}',
                 "label_column": "state", "label_value": "active"},
                {"ref_id": "B",
                 "source_expr": 'node_systemd_units{instance="$node",job="$job",state="failed"}',
                 "label_column": "state", "label_value": "failed"},
            ],
        )

    def test_same_metric_collapse_marks_nonequality_targets_unsupported(self):
        """Regex / negated distinguishing matchers cannot be re-implemented
        client-side without false-verdict risk; their provenance entries say
        so explicitly instead of being silently absent."""
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
            "id": 102,
            "type": "graph",
            "title": "Systemd Units",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": 'node_systemd_units{state="active"}', "refId": "A"},
                {"expr": 'node_systemd_units{state=~"fail.*"}', "refId": "B"},
            ],
        }
        _yaml_panel, result = self.translate_panel(panel)
        targets = result.query_ir["metadata"].get("collapsed_targets")
        self.assertIsNotNone(targets, "expected per-target provenance in metadata")
        by_ref = {t["ref_id"]: t for t in targets}
        self.assertEqual(by_ref["A"].get("label_value"), "active")
        self.assertNotIn("label_value", by_ref["B"])
        self.assertIn("non-equality", by_ref["B"].get("unsupported_reason", ""))

    def test_unfused_primary_target_records_whole_query_provenance(self):
        """When fusion keeps only the primary target, the translated query IS
        that target's translation - the parity oracle can verify it whole.
        The dropped siblings must surface as explicitly unverifiable instead
        of hiding inside a joined source_query the oracle can only SKIP."""
        panel = {
            "id": 103,
            "type": "graph",
            "title": "CPU Usage",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": ('sum by (instance) (rate(node_cpu_seconds_total{mode!="idle"}[5m])) '
                          '/ on(instance) sum by (instance) (rate(node_cpu_seconds_total[5m]))'),
                 "refId": "A"},
                {"expr": 'avg(sum by (core) (rate(windows_cpu_time_total{mode!="idle"}[5m])))',
                 "refId": "B"},
            ],
        }
        _yaml_panel, result = self.translate_panel(panel)
        targets = result.query_ir["metadata"].get("collapsed_targets")
        self.assertIsNotNone(targets, "expected unfused-primary provenance in metadata")
        by_ref = {t["ref_id"]: t for t in targets}
        self.assertTrue(by_ref["A"].get("whole_translated"))
        self.assertIn("rate(node_cpu_seconds_total", by_ref["A"]["source_expr"])
        self.assertIn("primary target only", by_ref["B"].get("unsupported_reason", ""))

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
        # Issue #8: multi-target fused gauges on a proven TSDS now take the TS path.
        self.assertEqual(result.query_ir["source_type"], "TS")
        self.assertEqual(result.target_query_contract["canonical_target"], "ts")
        self.assertEqual(result.contract_evaluation["status"], "exact_now")
        self.assertEqual(result.fulfillment_plan["status"], "not_required")
        self.assertEqual(result.fulfillment_plan["actions"], [])
        self.assertIn("TS metrics-*", result.query_ir["target_query"])

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

    def test_multi_target_merge_records_per_target_provenance(self):
        """A merged multi-target panel must record which source sub-query
        produced which output column. Without this the parity oracle cannot
        verify multi-query panels at all: the packet's source_query is a
        '|||' join whose order need not match the output columns (targets can
        be deduplicated or dropped), so 91/223 corpus panels were stuck at
        SKIP."""
        panel = {
            "id": 909,
            "type": "graph",
            "title": "CPU and memory",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {"expr": 'cpu_usage{state="active"}', "refId": "A", "legendFormat": "cpu_usage"},
                {"expr": 'memory_usage{state="active"}', "refId": "B", "legendFormat": "memory_usage"},
            ],
        }

        _yaml_panel, result = self.translate_panel(panel)

        targets = result.query_ir["metadata"].get("collapsed_targets")
        self.assertIsNotNone(targets, "expected per-target provenance in metadata")
        self.assertEqual([t["ref_id"] for t in targets], ["A", "B"])
        self.assertEqual(
            [t["source_expr"] for t in targets],
            ['cpu_usage{state="active"}', 'memory_usage{state="active"}'],
        )
        # Each value_column must be one of the merged query's output columns.
        fields = result.query_ir["metadata"].get("multi_series_metric_fields", [])
        for t in targets:
            self.assertIn(t["value_column"], fields)
        self.assertEqual(len({t["value_column"] for t in targets}), 2)

    def test_lossless_multi_target_merge_does_not_warn(self):
        panel = {
            "id": 908,
            "type": "graph",
            "title": "Network I/O",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "fieldConfig": {"defaults": {"unit": "Bps"}},
            "seriesOverrides": [],
            "targets": [
                {
                    "expr": "sum(rate(container_network_receive_bytes_total[1m]))",
                    "refId": "A",
                    "legendFormat": "receive",
                },
                {
                    "expr": "sum(rate(container_network_transmit_bytes_total[1m]))",
                    "refId": "B",
                    "legendFormat": "transmit",
                },
            ],
        }

        yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.status, "migrated")
        self.assertNotIn("Merged compatible panel targets into a single ES|QL query", result.reasons)
        self.assertEqual(
            [metric["field"] for metric in yaml_panel["esql"]["metrics"]],
            ["receive", "transmit"],
        )

    def test_series_override_yaxis_preserved_on_merged_metric(self):
        panel = {
            "id": 909,
            "type": "graph",
            "title": "Network I/O",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "yaxes": [
                {"format": "bytes", "show": True},
                {"format": "Bps", "show": True},
            ],
            "seriesOverrides": [{"alias": "transmit", "yaxis": 2}],
            "targets": [
                {
                    "expr": "sum(rate(container_network_receive_bytes_total[1m]))",
                    "refId": "A",
                    "legendFormat": "receive",
                },
                {
                    "expr": "sum(rate(container_network_transmit_bytes_total[1m]))",
                    "refId": "B",
                    "legendFormat": "transmit",
                },
            ],
        }

        yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.status, "migrated")
        metrics_by_field = {metric["field"]: metric for metric in yaml_panel["esql"]["metrics"]}
        self.assertNotIn("axis", metrics_by_field["receive"])
        self.assertEqual(metrics_by_field["transmit"].get("axis"), "right")
        self.assertEqual(metrics_by_field["receive"].get("format"), {"type": "bytes"})
        self.assertEqual(metrics_by_field["transmit"].get("format"), {"type": "bytes", "suffix": "/s"})
        self.assertNotIn("Merged compatible panel targets into a single ES|QL query", result.reasons)

    def test_regex_series_override_yaxis_preserved_on_merged_metric(self):
        panel = {
            "id": 910,
            "type": "graph",
            "title": "Network I/O",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "fieldConfig": {"defaults": {"unit": "Bps"}},
            "seriesOverrides": [{"alias": "/trans.*/", "yaxis": 2}],
            "targets": [
                {
                    "expr": "sum(rate(container_network_receive_bytes_total[1m]))",
                    "refId": "A",
                    "legendFormat": "receive",
                },
                {
                    "expr": "sum(rate(container_network_transmit_bytes_total[1m]))",
                    "refId": "B",
                    "legendFormat": "transmit",
                },
            ],
        }

        yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.status, "migrated")
        metrics_by_field = {metric["field"]: metric for metric in yaml_panel["esql"]["metrics"]}
        self.assertEqual(metrics_by_field["transmit"].get("axis"), "right")

    def test_real_k8s_loopback_network_merge_is_clean(self):
        panel = {
            "id": 911,
            "type": "timeseries",
            "title": "Network Received (loopback only) by instance",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "fieldConfig": {"defaults": {"unit": "Bps"}},
            "targets": [
                {
                    "expr": (
                        'sum(rate(node_network_receive_bytes_total{device="lo", cluster="$cluster", '
                        'job="$job"}[$__rate_interval])) by (instance)'
                    ),
                    "refId": "A",
                    "legendFormat": "Received bytes in {{ instance }}",
                },
                {
                    "expr": (
                        '- sum(rate(node_network_transmit_bytes_total{device="lo", cluster="$cluster", '
                        'job="$job"}[$__rate_interval])) by (instance)'
                    ),
                    "refId": "B",
                    "legendFormat": "Transmitted bytes in {{ instance }}",
                },
            ],
        }

        yaml_panel, result = self.translate_panel(panel)

        self.assertEqual(result.status, "migrated_with_warnings")
        self.assertIn("Dropped variable-driven label filters during migration", result.reasons)
        self.assertEqual(
            [metric["field"] for metric in yaml_panel["esql"]["metrics"]],
            ["Received_bytes_in", "Transmitted_bytes_in"],
        )
        self.assertIn("node_network_receive_bytes_total", yaml_panel["esql"]["query"])
        self.assertIn("node_network_transmit_bytes_total", yaml_panel["esql"]["query"])
        self.assertNotIn("Merged compatible panel targets", str(result.reasons))

    def test_real_prometheus_yaxis_override_becomes_right_axis_metric(self):
        panel = {
            "id": 912,
            "type": "graph",
            "title": "Rule evaulation duration",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "seriesOverrides": [{"alias": "Queue length", "yaxis": 2}],
            "targets": [
                {
                    "expr": 'sum(prometheus_evaluator_duration_seconds{instance="$instance"}) by (instance, quantile)',
                    "refId": "B",
                    "legendFormat": "Queue length",
                }
            ],
            "yaxes": [
                {"format": "s", "min": "0", "show": True},
                {"format": "short", "min": "0", "show": True},
            ],
        }

        yaml_panel, result = self.translate_panel(panel)

        # Grouped by (instance, quantile): one dimension drives the XY
        # breakdown and the other is surfaced as a dropped-dimension warning.
        self.assertEqual(result.status, "migrated_with_warnings")
        self.assertTrue(
            any("not on the chart" in w for w in result.reasons),
            f"Expected dropped-dimension warning, got {result.reasons}",
        )
        metric = yaml_panel["esql"]["metrics"][0]
        self.assertEqual(metric["label"], "Queue length")
        self.assertEqual(metric["axis"], "right")
        self.assertEqual(metric["format"], {"type": "number", "compact": True})

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
        self.assertIn(", sensor", query)
        self.assertIn('EVAL legend = CONCAT(', query)
        self.assertIn('COALESCE(TO_STRING(chip_name), "")', query)
        self.assertIn('COALESCE(TO_STRING(sensor), "")', query)
        self.assertEqual(yaml_panel["esql"].get("breakdown", {}).get("field"), "legend")
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
        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertTrue(
            any("divergent filters/groupings" in w for w in translated.warnings),
            translated.warnings,
        )

    def test_agg_over_ratio_of_range_funcs_is_not_feasible(self):
        # sum(increase(A) / increase(B)) computes a per-element ratio then
        # aggregates — semantically distinct from sum(A)/sum(B) and cannot
        # be expressed accurately in ES|QL.
        expr = (
            'sum(increase(prometheus_tsdb_compaction_duration_sum{instance="$instance"}[30m]) '
            '/ increase(prometheus_tsdb_compaction_duration_count{instance="$instance"}[30m])) by (instance)'
        )
        translated = self.translate(expr, panel_type="graph")
        self.assertEqual(translated.feasibility, "not_feasible")

    def test_sum_over_metric_subtraction_applies_linearity(self):
        # Bug A fix: sum(A - B) = sum(A) - sum(B) by linearity of SUM.
        # Previously both metrics were collapsed to only the left operand.
        expr = (
            "sum(node_memory_MemTotal_bytes{cluster=\"$cluster\", job=\"$job\"} "
            "- node_memory_MemAvailable_bytes{cluster=\"$cluster\", job=\"$job\"})"
        )
        translated = self.translate(expr, panel_type="timeseries")
        self.assertEqual(translated.feasibility, "feasible")
        q = translated.esql_query or ""
        self.assertIn("node_memory_MemTotal_bytes", q)
        self.assertIn("node_memory_MemAvailable_bytes", q)

    def test_nested_agg_preserves_label_filter(self):
        # Bug B fix: avg(sum by(cpu)(rate(metric{mode!~"idle"}[5m]))) should
        # retain the mode filter in the generated WHERE clause.
        expr = 'avg(sum by (cpu) (rate(node_cpu_seconds_total{mode!~"idle|iowait|steal"}[5m])))'
        translated = self.translate(expr, panel_type="timeseries")
        self.assertEqual(translated.feasibility, "feasible")
        q = translated.esql_query or ""
        self.assertIn("RLIKE", q)
        self.assertIn("idle", q)
        self.assertIn("| STATS inner_val = SUM(RATE(node_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute), cpu", q)
        self.assertIn("| STATS node_cpu_seconds_total_avg = AVG(inner_val) BY time_bucket", q)

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

    def test_run_esql_query_supplies_placeholder_values_for_dashboard_params(self):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"columns": [{"name": "value"}], "values": [[1]]}
        session = mock.Mock()
        session.post.return_value = response

        result = esql_validate._run_esql_query(
            "TS metrics-* | WHERE cluster == ?cluster | WHERE node RLIKE ?node",
            "http://localhost:9200",
            session=session,
        )

        self.assertTrue(result["ok"])
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["params"], [{"cluster": ".*"}, {"node": ".*"}])

    def test_run_esql_query_can_limit_validation_result_size(self):
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"columns": [{"name": "value"}], "values": [[1]]}
        session = mock.Mock()
        session.post.return_value = response

        result = esql_validate._run_esql_query(
            "TS metrics-* | STATS value = AVG(cpu)",
            "http://localhost:9200",
            session=session,
            result_limit=1,
        )

        self.assertTrue(result["ok"])
        payload = session.post.call_args.kwargs["json"]
        self.assertTrue(payload["query"].endswith("| LIMIT 1"))

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

    def test_validate_query_with_fixes_does_not_mark_param_dependent_empty_as_manual(self):
        class StubResolver:
            _index_pattern = "metrics-*"

            def concrete_index_candidates(self):
                return ["metrics-prometheus-synthetic"]

        query = (
            "TS metrics-*\n"
            "| WHERE instance == ?node\n"
            "| STATS x = AVG(foo) BY time_bucket = TBUCKET(5 minute)"
        )

        def fake_run(candidate_query, _es_url, **kwargs):
            if "metrics-prometheus-synthetic" in candidate_query:
                return {"ok": True, "error": "", "rows": 0, "columns": ["x"], "values": []}
            return {"ok": False, "error": "Unknown index [metrics-*]", "rows": 0, "columns": []}

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run):
            result = migrate.validate_query_with_fixes(query, "http://localhost:9200", StubResolver())

        self.assertEqual(result["status"], "fixed")
        self.assertEqual(result["analysis"]["result_rows"], 0)
        self.assertTrue(result["analysis"]["param_dependent_rows"])

    def test_narrow_limit_caps_candidates_probed(self):
        """narrow_limit prevents unbounded index-probing when the resolver has many candidates."""
        probed = []

        class StubResolver:
            _index_pattern = "metrics-*"

            def concrete_index_candidates(self):
                return [f"metrics-stream-{i}" for i in range(20)]

        query = "FROM metrics-*\n| STATS v = AVG(metric)"

        def fake_run(q, _url, **kw):
            if "metrics-*" in q:
                return {"ok": False, "error": "Unknown index [metrics-*]", "rows": 0, "columns": []}
            probed.append(q)
            return {"ok": False, "error": "some error", "rows": 0, "columns": []}

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run):
            migrate.validate_query_with_fixes(
                query, "http://localhost:9200", StubResolver(),
                narrow_limit=5,
            )

        self.assertLessEqual(len(probed), 5, "narrowing must probe at most narrow_limit candidates")

    def test_narrow_probe_uses_short_timeout(self):
        """Narrowing probes must use probe_timeout (3 s), not the full 15 s validation timeout."""
        probe_timeouts = []

        class StubResolver:
            _index_pattern = "metrics-*"

            def concrete_index_candidates(self):
                return ["metrics-foo", "metrics-bar"]

        query = "FROM metrics-*\n| STATS v = AVG(metric)"

        def fake_run(q, _url, **kw):
            if "metrics-*" in q:
                return {"ok": False, "error": "Unknown index [metrics-*]", "rows": 0, "columns": []}
            probe_timeouts.append(kw.get("timeout"))
            return {"ok": False, "error": "some error", "rows": 0, "columns": []}

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run):
            migrate.validate_query_with_fixes(
                query, "http://localhost:9200", StubResolver(),
                narrow_timeout=3,
            )

        self.assertTrue(probe_timeouts, "at least one narrowing probe should have run")
        self.assertTrue(
            all(t == 3 for t in probe_timeouts),
            f"all narrowing probes must use narrow_timeout=3, got: {probe_timeouts}",
        )

    def test_narrow_probing_skipped_for_column_only_errors(self):
        """Narrow-index probing must not run when the error is Unknown column (not Unknown index).

        Field-mapping errors cannot be fixed by trying a different index pattern; running
        narrow probing on every fix iteration is the root cause of the ~75 s/panel spiral
        seen on Mixin / Compute Resources / Workload dashboards.
        """
        narrow_calls = []

        class StubResolver:
            _index_pattern = "metrics-*"

            def concrete_index_candidates(self):
                return ["metrics-foo", "metrics-bar"]

            def _candidate_fields(self, col):
                return []

            def field_exists(self, f):
                return False

        query = "FROM metrics-*\n| STATS v = AVG(cpu_usage)"
        call_count = [0]

        def fake_run(q, _url, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"ok": False, "error": "Unknown column [cpu_usage]", "rows": 0, "columns": []}
            return {"ok": True, "error": "", "rows": 1, "columns": [], "values": [], "metadata": {}}

        def fake_narrow(q, url, resolver, **kw):
            narrow_calls.append(q)
            return None

        def fake_fix(q, err, resolver):
            return q.replace("cpu_usage", "system.cpu.usage")

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run), \
             mock.patch.object(esql_validate, "_try_narrow_index_pattern", side_effect=fake_narrow), \
             mock.patch.object(esql_validate, "_try_fix_esql_field_error", side_effect=fake_fix):
            result = migrate.validate_query_with_fixes(
                query, "http://localhost:9200", StubResolver(),
            )

        self.assertEqual(result["status"], "fixed")
        self.assertEqual(len(narrow_calls), 0,
                         "narrow probing must not run for Unknown column errors")

    def test_narrow_probing_runs_at_most_once_per_index_pattern(self):
        """Narrow-index probing must not re-run for the same index pattern across fix iterations.

        When narrow probing fails to find a better index, retrying it on every subsequent
        field-fix iteration wastes (max_candidates x probe_timeout) seconds per iteration.
        """
        narrow_calls = []

        class StubResolver:
            _index_pattern = "metrics-*"

            def concrete_index_candidates(self):
                return ["metrics-foo", "metrics-bar"]

            def _candidate_fields(self, col):
                return []

            def field_exists(self, f):
                return False

        query = "FROM metrics-*\n| STATS v = AVG(cpu_usage)"
        call_count = [0]

        def fake_run(q, _url, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: unknown index error (triggers narrow probing)
                return {"ok": False, "error": "Unknown index [metrics-*] and Unknown column [cpu_usage]", "rows": 0, "columns": []}
            if call_count[0] == 2:
                # After field fix: column error only
                return {"ok": False, "error": "Unknown column [cpu_usage_v2]", "rows": 0, "columns": []}
            return {"ok": True, "error": "", "rows": 1, "columns": [], "values": [], "metadata": {}}

        def fake_narrow(q, url, resolver, **kw):
            narrow_calls.append(q)
            return None  # No candidate found

        fix_count = [0]

        def fake_fix(q, err, resolver):
            fix_count[0] += 1
            if fix_count[0] == 1:
                return q.replace("cpu_usage", "cpu_usage_v2")
            return q.replace("cpu_usage_v2", "system.cpu.usage")

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run), \
             mock.patch.object(esql_validate, "_try_narrow_index_pattern", side_effect=fake_narrow), \
             mock.patch.object(esql_validate, "_try_fix_esql_field_error", side_effect=fake_fix):
            result = migrate.validate_query_with_fixes(
                query, "http://localhost:9200", StubResolver(),
            )

        self.assertEqual(result["status"], "fixed")
        self.assertEqual(len(narrow_calls), 1,
                         f"narrow probing must run at most once per index pattern, ran {len(narrow_calls)}x")

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

    def test_validate_query_with_fixes_rewrites_ts_count_star_to_count_field(self):
        query = (
            "TS metrics-*\n"
            "| WHERE up == 1\n"
            "| STATS up_count = COUNT(*)"
        )

        def fake_run(candidate_query, _es_url, **kwargs):
            if "COUNT(up)" in candidate_query:
                return {"ok": True, "error": "", "rows": 1, "columns": ["up_count"], "values": [[2]]}
            return {
                "ok": False,
                "error": "Found 1 problem\nline 3:20: count_star [COUNT(*)] can't be used with TS command; use count on a field instead",
                "rows": 0,
                "columns": [],
            }

        with mock.patch.object(esql_validate, "_run_esql_query", side_effect=fake_run):
            result = migrate.validate_query_with_fixes(query, "http://localhost:9200", resolver=None)

        self.assertEqual(result["status"], "fixed")
        self.assertIn("COUNT(up)", result["query"])

    def test_run_esql_query_materializes_dashboard_time_params_for_validation(self):
        captured = {}

        def fake_post(url, json, params, headers, timeout, **kwargs):
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

    def test_query_variable_skips_conflicting_control_fields(self):
        resolver = migrate.SchemaResolver(self.rule_pack)
        resolver._discovery_attempted = True
        resolver._field_cache = {
            "job": {
                "keyword": {
                    "type": "keyword",
                    "aggregatable": True,
                    "searchable": True,
                    "time_series_dimension": True,
                    "indices": [".ds-metrics-prometheus-default-000001"],
                },
                "double": {
                    "type": "double",
                    "aggregatable": True,
                    "searchable": True,
                    "time_series_metric": "gauge",
                    "indices": [".ds-metrics-generic-default-000001"],
                },
            }
        }
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "job",
                "label": "Job",
                "query": "label_values(node_uname_info, job)",
            }],
            datasource_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=resolver,
        )
        self.assertEqual(controls, [])

    def test_live_missing_metric_field_is_not_marked_migrated(self):
        self.seed_field_caps({
            "some_other_metric": {"double": {"type": "double", "aggregatable": True, "searchable": True}},
        })
        self.resolver._discovery_status = "ok"

        translated = self.translate("missing_metric_total")

        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertIn("Target field missing_metric_total is missing from live schema discovery", translated.warnings)

    def test_template_variable_metric_name_is_not_marked_migrated(self):
        translated = self.translate("$metric")

        self.assertEqual(translated.feasibility, "not_feasible")
        self.assertIn(
            "PromQL metric name comes from Grafana template variable ($metric); "
            "dynamic metric selection requires manual redesign",
            translated.warnings,
        )

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

    def test_query_variable_emits_esql_param_control_when_feature_supported(self):
        """Issue #107: when the target binds Grafana template variables as
        native ES|QL parameters (``?var``), the control must DEFINE that ES|QL
        variable, not emit a generic options/range data-view filter (which
        leaves the panel queries failing with "Unknown query parameter")."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        set_runtime_feature(
            self.rule_pack,
            PROMQL_LABEL_MATCHER_PARAMS,
            supported=True,
            source="probe",
        )
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
        self.assertEqual(len(controls), 1)
        control = controls[0]
        self.assertEqual(control["type"], "esql")
        self.assertEqual(control["variable_name"], "node")
        self.assertEqual(control["variable_type"], "values")
        self.assertIs(control["multiple"], False)
        # ES|QL binding controls populate values from a query over the resolved
        # field and never carry generic data-view filter keys.
        self.assertNotIn("field", control)
        self.assertNotIn("data_view", control)
        self.assertIn("FROM metrics-*", control["query"])
        self.assertIn("service.instance.id", control["query"])

    def test_query_variable_esql_param_control_is_single_select_even_when_multi(self):
        """A multi-select Grafana variable still binds a scalar ES|QL parameter
        (``== ?var`` / ``RLIKE ?var``), so the emitted control is single-select
        to keep the query valid (issue #107)."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        set_runtime_feature(
            self.rule_pack,
            PROMQL_LABEL_MATCHER_PARAMS,
            supported=True,
            source="probe",
        )
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
        self.assertEqual(controls[0]["type"], "esql")
        self.assertIs(controls[0]["multiple"], False)

    def test_query_variable_control_default_is_match_all_for_include_all(self):
        """Issue #131: a control with no default selection leaves the bound
        ?param unset, so Kibana errors with "Parameter [?var] value not found".
        An includeAll variable must default to a regex match-all ("All")."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        set_runtime_feature(
            self.rule_pack, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe"
        )
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "node",
                "label": "Instance",
                "multi": True,
                "includeAll": True,
                "query": "label_values(node_uname_info,instance)",
            }],
            datasource_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertEqual(controls[0]["default"], ".*")

    def test_query_variable_control_default_mirrors_current_value(self):
        """A concrete Grafana ``current`` selection is mirrored as the control's
        default so the migrated dashboard opens on the same value (issue #131)."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        set_runtime_feature(
            self.rule_pack, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe"
        )
        controls = migrate.translate_variables(
            [{
                "type": "query",
                "name": "node",
                "label": "Instance",
                "multi": False,
                "current": {"text": "host-01", "value": "host-01"},
                "query": "label_values(node_uname_info,instance)",
            }],
            datasource_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertEqual(controls[0]["default"], "host-01")

    def test_esql_path_preserves_template_matcher_as_param_when_capability_on(self):
        """Issue #64: when the target binds ``?var`` params, the ES|QL path must
        preserve a $var label matcher as a native parameter instead of silently
        dropping it (which queried all data, the "ES|QL looks fine" illusion)."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        set_runtime_feature(
            self.rule_pack, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe"
        )
        result = self.translate('sum(cpu{host=~"$host"}) by (host)')

        self.assertEqual(result.feasibility, "feasible")
        self.assertIn("?host", result.esql_query)
        self.assertNotIn(
            "Dropped variable-driven label filters during migration",
            result.warnings,
        )

    def test_esql_path_still_drops_template_matcher_when_capability_off(self):
        """Capability-off targets cannot bind ``?var``; the ES|QL path must keep
        dropping the matcher (and warn) so dashboards still upload (issue #100)."""
        result = self.translate('sum(cpu{host=~"$host"}) by (host)')

        self.assertEqual(result.feasibility, "feasible")
        self.assertNotIn("?host", result.esql_query)
        self.assertIn(
            "Dropped variable-driven label filters during migration",
            result.warnings,
        )

    def test_esql_path_preserves_template_matcher_with_only_esql_named_param_binding(self):
        """Issue #132: the ES|QL ``?var`` path only needs ES|QL named-parameter
        binding, not the native PROMQL command. A ``--no-native-promql`` run
        against a target advertising ``esql_named_param_binding`` (and NOT
        ``promql_label_matcher_params``) must still preserve the matcher."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            ESQL_NAMED_PARAM_BINDING,
            PROMQL_LABEL_MATCHER_PARAMS,
            is_feature_supported,
            set_runtime_feature,
        )

        set_runtime_feature(
            self.rule_pack, ESQL_NAMED_PARAM_BINDING, supported=True, source="probe"
        )
        # The native PROMQL label-matcher capability is explicitly NOT set, to
        # prove the ES|QL path no longer depends on it.
        self.assertFalse(
            is_feature_supported(self.rule_pack, PROMQL_LABEL_MATCHER_PARAMS)
        )
        result = self.translate('sum(cpu{host=~"$host"}) by (host)')

        self.assertEqual(result.feasibility, "feasible")
        self.assertIn("?host", result.esql_query)
        self.assertNotIn(
            "Dropped variable-driven label filters during migration",
            result.warnings,
        )

    def test_equality_matcher_on_match_all_var_emits_regex_not_literal(self):
        """PR #133 review: an includeAll variable defaults its binding control
        to the regex match-all (".*"). An equality matcher (``field == ?var``)
        would compare the field against the literal string ".*" and render
        empty on first load, so it must be emitted as a regex match instead so
        the match-all default selects every series."""
        from observability_migration.adapters.source.grafana.promql import (
            _grafana_param_value,
            _matcher_to_esql,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        set_runtime_feature(
            self.rule_pack, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe"
        )
        matcher = {"label": "host", "op": "=", "value": _grafana_param_value("host")}

        # No match-all default declared -> exact equality preserved.
        self.assertEqual(
            _matcher_to_esql(matcher, self.resolver),
            f"{self.resolver.resolve_label('host')} == ?host",
        )

        # Declared as a regex-default param -> equality becomes a regex match.
        self.rule_pack._regex_default_param_names = {"host"}
        self.assertEqual(
            _matcher_to_esql(matcher, self.resolver),
            f"{self.resolver.resolve_label('host')} RLIKE ?host",
        )

    def test_dashboard_equality_matcher_on_include_all_var_renders_regex(self):
        """End-to-end: a ``{label="$var"}`` equality matcher whose variable is
        includeAll must compile to ``RLIKE ?var`` (not ``== ?var``) so the
        control's ".*" default selects every series on first load (PR #133)."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        rule_pack = rules.RulePackConfig()
        set_runtime_feature(
            rule_pack, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe"
        )
        resolver = migrate.SchemaResolver(rule_pack)
        dashboard = {
            "title": "Equality All",
            "uid": "equality-all",
            "templating": {
                "list": [
                    {
                        "type": "query",
                        "name": "host",
                        "label": "Host",
                        "multi": True,
                        "includeAll": True,
                        "query": "label_values(cpu, host)",
                    }
                ]
            },
            "panels": [
                {
                    "id": 1,
                    "title": "CPU",
                    "type": "graph",
                    "targets": [{"refId": "A", "expr": 'sum(cpu{host="$host"})'}],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            _result, yaml_path = panels.translate_dashboard(
                dashboard,
                tmpdir,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=rule_pack,
                resolver=resolver,
            )
            doc = yaml.safe_load(pathlib.Path(yaml_path).read_text())

        rendered = yaml.dump(doc)
        self.assertIn("RLIKE ?host", rendered)
        self.assertNotIn("== ?host", rendered)
        controls = doc["dashboards"][0].get("controls", [])
        binding = next(c for c in controls if c.get("variable_name") == "host")
        self.assertEqual(binding["default"], ".*")

    def test_dashboard_esql_named_param_binding_preserves_var_with_single_control(self):
        """Issue #132 end-to-end: a ``--no-native-promql`` target that only
        advertises ``esql_named_param_binding`` must preserve ``?var`` AND emit
        exactly one ES|QL binding control for it (not a generic data-view
        control plus a synthesized duplicate)."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            ESQL_NAMED_PARAM_BINDING,
            set_runtime_feature,
        )

        rule_pack = rules.RulePackConfig()
        set_runtime_feature(
            rule_pack, ESQL_NAMED_PARAM_BINDING, supported=True, source="probe"
        )
        resolver = migrate.SchemaResolver(rule_pack)
        dashboard = {
            "title": "ESQL Named Param",
            "uid": "esql-named-param",
            "templating": {
                "list": [
                    {
                        "type": "query",
                        "name": "host",
                        "label": "Host",
                        "multi": False,
                        "current": {"text": "host-01", "value": "host-01"},
                        "query": "label_values(cpu, host)",
                    }
                ]
            },
            "panels": [
                {
                    "id": 1,
                    "title": "CPU",
                    "type": "graph",
                    "targets": [{"refId": "A", "expr": 'sum(cpu{host=~"$host"}) by (host)'}],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            _result, yaml_path = panels.translate_dashboard(
                dashboard,
                tmpdir,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=rule_pack,
                resolver=resolver,
            )
            doc = yaml.safe_load(pathlib.Path(yaml_path).read_text())

        self.assertIn("?host", yaml.dump(doc))
        controls = doc["dashboards"][0].get("controls", [])
        host_controls = [c for c in controls if c.get("variable_name") == "host"]
        self.assertEqual(len(host_controls), 1)
        self.assertEqual(host_controls[0]["type"], "esql")
        # No generic data-view control should leak alongside the ES|QL binding.
        self.assertTrue(all(c.get("type") == "esql" for c in controls if c.get("label") == "Host"))

    def test_dashboard_native_equality_matcher_on_include_all_var_uses_regex(self):
        """End-to-end native PROMQL: a ``{label="$var"}`` equality matcher whose
        variable is includeAll must render as ``label=~?var`` so the control's
        ".*" default selects every series on first load (PR #133)."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        rule_pack = rules.RulePackConfig(native_promql=True)
        set_runtime_feature(
            rule_pack, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe"
        )
        resolver = migrate.SchemaResolver(rule_pack)
        dashboard = {
            "title": "Native Equality All",
            "uid": "native-equality-all",
            "templating": {
                "list": [
                    {
                        "type": "query",
                        "name": "host",
                        "label": "Host",
                        "multi": True,
                        "includeAll": True,
                        "query": "label_values(cpu, host)",
                    }
                ]
            },
            "panels": [
                {
                    "id": 1,
                    "title": "CPU",
                    "type": "graph",
                    "targets": [{"refId": "A", "expr": 'sum(cpu{host="$host"})'}],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            _result, yaml_path = panels.translate_dashboard(
                dashboard,
                tmpdir,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=rule_pack,
                resolver=resolver,
            )
            doc = yaml.safe_load(pathlib.Path(yaml_path).read_text())

        rendered = yaml.dump(doc)
        self.assertIn("host=~?host", rendered)
        self.assertNotIn("host=?host", rendered)

    def test_dashboard_equality_matcher_on_concrete_var_keeps_exact_match(self):
        """A variable with a concrete ``current`` value defaults its control to
        that value, so an equality matcher must stay exact (``== ?var``) and
        not be loosened into a regex match (PR #133)."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        rule_pack = rules.RulePackConfig()
        set_runtime_feature(
            rule_pack, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe"
        )
        resolver = migrate.SchemaResolver(rule_pack)
        dashboard = {
            "title": "Equality Concrete",
            "uid": "equality-concrete",
            "templating": {
                "list": [
                    {
                        "type": "query",
                        "name": "host",
                        "label": "Host",
                        "multi": False,
                        "current": {"text": "host-01", "value": "host-01"},
                        "query": "label_values(cpu, host)",
                    }
                ]
            },
            "panels": [
                {
                    "id": 1,
                    "title": "CPU",
                    "type": "graph",
                    "targets": [{"refId": "A", "expr": 'sum(cpu{host="$host"})'}],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            _result, yaml_path = panels.translate_dashboard(
                dashboard,
                tmpdir,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=rule_pack,
                resolver=resolver,
            )
            doc = yaml.safe_load(pathlib.Path(yaml_path).read_text())

        rendered = yaml.dump(doc)
        self.assertIn("== ?host", rendered)
        self.assertNotIn("RLIKE ?host", rendered)
        controls = doc["dashboards"][0].get("controls", [])
        binding = next(c for c in controls if c.get("variable_name") == "host")
        self.assertEqual(binding["default"], "host-01")

    def test_dashboard_emits_binding_control_for_custom_variable_param(self):
        """Issue #131: a Grafana ``custom`` variable referenced as ?var in a
        native PROMQL panel must get a binding control. Previously custom
        variables were routed to the time-picker rule and got no control, so the
        emitted ?var stayed unbound and the panel failed to render."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        rule_pack = rules.RulePackConfig(native_promql=True)
        set_runtime_feature(
            rule_pack, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe"
        )
        resolver = migrate.SchemaResolver(rule_pack)
        dashboard = {
            "title": "Custom Var",
            "uid": "custom-var",
            "templating": {
                "list": [
                    {
                        "type": "custom",
                        "name": "health_status",
                        "label": "Health",
                        "includeAll": True,
                        "query": "Healthy,Progressing,Degraded",
                    }
                ]
            },
            "panels": [
                {
                    "id": 1,
                    "title": "Apps",
                    "type": "graph",
                    "targets": [
                        {
                            "refId": "A",
                            "expr": 'sum(argocd_app_info{health_status=~"$health_status"})',
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            _result, yaml_path = panels.translate_dashboard(
                dashboard,
                tmpdir,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=rule_pack,
                resolver=resolver,
            )
            doc = yaml.safe_load(pathlib.Path(yaml_path).read_text())

        rendered = yaml.dump(doc)
        self.assertIn("?health_status", rendered)
        controls = doc["dashboards"][0].get("controls", [])
        control_names = {
            c.get("variable_name") for c in controls if c.get("type") == "esql"
        }
        self.assertIn("health_status", control_names)
        binding = next(c for c in controls if c.get("variable_name") == "health_status")
        self.assertEqual(binding["default"], ".*")

    def test_dashboard_emits_a_control_for_every_emitted_param(self):
        """Issue #131: every ?var a panel emits must have a binding control, so
        no panel can load with an unbound parameter. Covers a query variable
        that the control translator skips (unresolved field) but whose ?var is
        still emitted on the native PROMQL path."""
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        rule_pack = rules.RulePackConfig(native_promql=True)
        set_runtime_feature(
            rule_pack, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe"
        )
        resolver = migrate.SchemaResolver(rule_pack)
        dashboard = {
            "title": "Sync Status",
            "uid": "sync-status",
            "templating": {"list": []},
            "panels": [
                {
                    "id": 1,
                    "title": "By sync status",
                    "type": "graph",
                    "targets": [
                        {
                            "refId": "A",
                            "expr": 'sum(argocd_app_info{sync_status=~"$sync_status"})',
                        }
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            _result, yaml_path = panels.translate_dashboard(
                dashboard,
                tmpdir,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=rule_pack,
                resolver=resolver,
            )
            doc = yaml.safe_load(pathlib.Path(yaml_path).read_text())

        emitted = panels._collect_emitted_param_names(doc["dashboards"][0]["panels"])
        controls = doc["dashboards"][0].get("controls", [])
        bound = {c.get("variable_name") for c in controls if c.get("type") == "esql"}
        self.assertTrue(emitted)
        self.assertTrue(
            emitted.issubset(bound),
            f"unbound params: {emitted - bound}",
        )

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

    def test_detect_target_runtime_features_uses_capability_names(self):
        from observability_migration.adapters.source.grafana.cli import (
            _detect_target_runtime_features,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_COMMAND_V0,
            PROMQL_LABEL_MATCHER_PARAMS,
        )

        with (
            mock.patch(
                "observability_migration.adapters.source.grafana.cli._detect_promql_support",
                return_value=True,
            ),
            mock.patch(
                "observability_migration.adapters.source.grafana.cli.requests.get",
            ) as get,
            mock.patch(
                "observability_migration.adapters.source.grafana.cli.requests.post",
            ) as post,
        ):
            get.return_value = SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "nodes": {
                        "node-1": {
                            "capabilities": [
                                PROMQL_LABEL_MATCHER_PARAMS,
                            ]
                        }
                    }
                },
                text="",
            )
            post.return_value = SimpleNamespace(status_code=200, json=lambda: {"columns": [{"name": "value"}]}, text="")

            profile = _detect_target_runtime_features("https://es.example", "apikey")

        self.assertTrue(profile[PROMQL_COMMAND_V0]["supported"])
        self.assertEqual(profile[PROMQL_COMMAND_V0]["source"], "probe")
        self.assertTrue(profile[PROMQL_LABEL_MATCHER_PARAMS]["supported"])
        self.assertEqual(profile[PROMQL_LABEL_MATCHER_PARAMS]["source"], "capabilities+probe")
        post.assert_called_once()

    def test_detect_target_runtime_features_probe_rejection_overrides_capability(self):
        from observability_migration.adapters.source.grafana.cli import (
            _detect_target_runtime_features,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
        )

        with (
            mock.patch(
                "observability_migration.adapters.source.grafana.cli._detect_promql_support",
                return_value=True,
            ),
            mock.patch(
                "observability_migration.adapters.source.grafana.cli.requests.get",
            ) as get,
            mock.patch(
                "observability_migration.adapters.source.grafana.cli.requests.post",
            ) as post,
        ):
            get.return_value = SimpleNamespace(
                status_code=200,
                json=lambda: {"nodes": {"node-1": {"capabilities": [PROMQL_LABEL_MATCHER_PARAMS]}}},
                text="",
            )
            post.return_value = SimpleNamespace(
                status_code=400,
                json=lambda: {"error": "mismatched input '?_job' expecting STRING"},
                text="mismatched input '?_job' expecting STRING",
            )

            profile = _detect_target_runtime_features("https://es.example", "apikey")

        self.assertFalse(profile[PROMQL_LABEL_MATCHER_PARAMS]["supported"])
        self.assertEqual(profile[PROMQL_LABEL_MATCHER_PARAMS]["source"], "capabilities+probe")

    def test_detect_target_runtime_features_disables_label_params_on_parser_rejection(self):
        from observability_migration.adapters.source.grafana.cli import (
            _detect_target_runtime_features,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
        )

        with (
            mock.patch(
                "observability_migration.adapters.source.grafana.cli._detect_promql_support",
                return_value=True,
            ),
            mock.patch(
                "observability_migration.adapters.source.grafana.cli.requests.get",
            ) as get,
            mock.patch(
                "observability_migration.adapters.source.grafana.cli.requests.post",
            ) as post,
        ):
            get.return_value = SimpleNamespace(status_code=410, json=lambda: {}, text="api_not_available_exception")
            post.return_value = SimpleNamespace(
                status_code=400,
                json=lambda: {"error": "mismatched input '?_job' expecting STRING"},
                text="mismatched input '?_job' expecting STRING",
            )

            profile = _detect_target_runtime_features("https://es.example", "apikey")

        self.assertFalse(profile[PROMQL_LABEL_MATCHER_PARAMS]["supported"])
        self.assertEqual(profile[PROMQL_LABEL_MATCHER_PARAMS]["source"], "probe")
        self.assertIn("rejects", profile[PROMQL_LABEL_MATCHER_PARAMS]["reason"])

    def test_detect_esql_named_param_binding_supported_and_rejected(self):
        """Issue #132: a 200 means the target binds ES|QL named params; a parser
        rejection (or transport failure) leaves it unsupported so the engine
        keeps the safe fallback of dropping ``?var`` filters."""
        from observability_migration.adapters.source.grafana.cli import (
            _detect_esql_named_param_binding,
        )

        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.requests.post",
            return_value=SimpleNamespace(status_code=200, json=lambda: {"columns": [{"name": "probe"}]}, text=""),
        ):
            state = _detect_esql_named_param_binding("https://es.example", "apikey")
        self.assertTrue(state["supported"])

        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.requests.post",
            return_value=SimpleNamespace(status_code=400, json=lambda: {}, text="unknown token '?p'"),
        ):
            state = _detect_esql_named_param_binding("https://es.example", "apikey")
        self.assertFalse(state["supported"])

        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.requests.post",
            side_effect=ConnectionError("network"),
        ):
            state = _detect_esql_named_param_binding("https://es.example", "apikey")
        self.assertFalse(state["supported"])
        self.assertEqual(state["confidence"], "inconclusive")

        self.assertEqual(_detect_esql_named_param_binding(""), {})

    def test_parse_args_defaults_native_promql_to_auto(self):
        from observability_migration.adapters.source.grafana.cli import parse_args

        args = parse_args([])

        self.assertEqual(args.native_promql_flag, "auto")

    def test_parse_args_native_promql_flags_override_auto_default(self):
        from observability_migration.adapters.source.grafana.cli import parse_args

        self.assertEqual(
            parse_args(["--native-promql"]).native_promql_flag,
            "force_on",
        )
        self.assertEqual(
            parse_args(["--no-native-promql"]).native_promql_flag,
            "force_off",
        )

    def test_apply_native_promql_records_runtime_feature_profile(self):
        from observability_migration.adapters.source.grafana.cli import (
            _apply_native_promql_to_rule_pack,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            ESQL_NAMED_PARAM_BINDING,
            PROMQL_COMMAND_V0,
            PROMQL_LABEL_MATCHER_PARAMS,
            is_feature_supported,
        )

        args = SimpleNamespace(
            dataset_filter="",
            es_url="https://es.example",
            es_api_key="apikey",
            native_promql_flag="auto",
        )
        rule_pack = rules.RulePackConfig()
        profile = {
            PROMQL_COMMAND_V0: {"supported": True, "source": "probe", "confidence": "verified"},
            PROMQL_LABEL_MATCHER_PARAMS: {"supported": False, "source": "probe", "confidence": "verified"},
        }
        esql_state = {"supported": True, "source": "probe", "confidence": "verified"}
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_target_runtime_features",
            return_value=dict(profile),
        ), mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_esql_named_param_binding",
            return_value=esql_state,
        ):
            _apply_native_promql_to_rule_pack(rule_pack, args)

        self.assertTrue(rule_pack.native_promql)
        self.assertEqual(
            rule_pack.runtime_features,
            {**profile, ESQL_NAMED_PARAM_BINDING: esql_state},
        )
        self.assertFalse(is_feature_supported(rule_pack, PROMQL_LABEL_MATCHER_PARAMS))

    def test_apply_native_promql_auto_default_clears_default_dataset_filter(self):
        from observability_migration.adapters.source.grafana.cli import (
            _apply_native_promql_to_rule_pack,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_COMMAND_V0,
        )

        args = SimpleNamespace(
            dataset_filter="",
            es_url="https://es.example",
            es_api_key="apikey",
            native_promql_flag="auto",
        )
        rule_pack = rules.RulePackConfig()
        profile = {
            PROMQL_COMMAND_V0: {"supported": True, "source": "probe", "confidence": "verified"},
        }
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_target_runtime_features",
            return_value=profile,
        ):
            _apply_native_promql_to_rule_pack(rule_pack, args)

        self.assertTrue(rule_pack.native_promql)
        self.assertEqual(rule_pack.metrics_dataset_filter, "")

    def test_apply_native_promql_auto_without_es_url_defaults_to_native(self):
        """An offline run (no ``--es-url`` to probe) still defaults to native
        PROMQL and clears the default ``"prometheus"`` dataset filter, with no
        cluster probe attempted."""
        from observability_migration.adapters.source.grafana.cli import (
            _apply_native_promql_to_rule_pack,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_COMMAND_V0,
        )

        args = SimpleNamespace(
            dataset_filter="",
            es_url="",
            es_api_key="",
            native_promql_flag="auto",
        )
        rule_pack = rules.RulePackConfig()
        self.assertEqual(rule_pack.metrics_dataset_filter, "prometheus")
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_target_runtime_features",
        ) as detect:
            _apply_native_promql_to_rule_pack(rule_pack, args)
            detect.assert_not_called()

        self.assertTrue(rule_pack.native_promql)
        self.assertEqual(rule_pack.metrics_dataset_filter, "")
        self.assertEqual(
            rule_pack.runtime_features[PROMQL_COMMAND_V0],
            {
                "supported": True,
                "source": "default",
                "confidence": "unverified",
                "level": "runtime",
                "reason": "no --es-url configured; native PROMQL assumed for offline migration",
            },
        )

    def test_apply_native_promql_force_on_records_detected_subfeatures(self):
        from observability_migration.adapters.source.grafana.cli import (
            _apply_native_promql_to_rule_pack,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            ESQL_NAMED_PARAM_BINDING,
            PROMQL_COMMAND_V0,
            PROMQL_LABEL_MATCHER_PARAMS,
        )

        args = SimpleNamespace(
            dataset_filter="",
            es_url="https://es.example",
            es_api_key="apikey",
            native_promql_flag="force_on",
        )
        rule_pack = rules.RulePackConfig()
        profile = {
            PROMQL_COMMAND_V0: {"supported": True, "source": "probe", "confidence": "verified"},
            PROMQL_LABEL_MATCHER_PARAMS: {"supported": True, "source": "capabilities", "confidence": "verified"},
        }
        esql_state = {"supported": True, "source": "probe", "confidence": "verified"}
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_target_runtime_features",
            return_value=dict(profile),
        ), mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_esql_named_param_binding",
            return_value=esql_state,
        ):
            _apply_native_promql_to_rule_pack(rule_pack, args)

        self.assertTrue(rule_pack.native_promql)
        self.assertEqual(
            rule_pack.runtime_features,
            {**profile, ESQL_NAMED_PARAM_BINDING: esql_state},
        )

    def test_apply_native_promql_force_off_probes_esql_named_param_binding(self):
        """Issue #132: a deliberate --no-native-promql run must still probe the
        target for ES|QL named-parameter binding (it does not need the PROMQL
        command) so the pure-ES|QL path can preserve ``?var`` filters, without
        running the native PROMQL probe."""
        from observability_migration.adapters.source.grafana.cli import (
            _apply_native_promql_to_rule_pack,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            ESQL_NAMED_PARAM_BINDING,
            is_feature_supported,
        )

        args = SimpleNamespace(
            dataset_filter="",
            es_url="https://es.example",
            es_api_key="apikey",
            native_promql_flag="force_off",
        )
        rule_pack = rules.RulePackConfig()
        esql_state = {"supported": True, "source": "probe", "confidence": "verified"}
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_target_runtime_features",
        ) as detect_native, mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_esql_named_param_binding",
            return_value=esql_state,
        ) as detect_esql:
            _apply_native_promql_to_rule_pack(rule_pack, args)

        detect_native.assert_not_called()
        detect_esql.assert_called_once()
        self.assertFalse(rule_pack.native_promql)
        self.assertTrue(is_feature_supported(rule_pack, ESQL_NAMED_PARAM_BINDING))

    def test_apply_native_promql_offline_assumes_esql_named_param_binding(self):
        """Issue #132: an offline run cannot probe, but ES|QL named-parameter
        binding is a stable core feature so it is assumed (like native PROMQL),
        keeping ``?var`` label filters on offline --no-native-promql runs."""
        from observability_migration.adapters.source.grafana.cli import (
            _apply_native_promql_to_rule_pack,
        )
        from observability_migration.adapters.source.grafana.runtime_features import (
            ESQL_NAMED_PARAM_BINDING,
            is_feature_supported,
        )

        args = SimpleNamespace(
            dataset_filter="",
            es_url="",
            es_api_key="",
            native_promql_flag="force_off",
        )
        rule_pack = rules.RulePackConfig()
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_esql_named_param_binding",
        ) as detect_esql:
            _apply_native_promql_to_rule_pack(rule_pack, args)
            detect_esql.assert_not_called()

        self.assertFalse(rule_pack.native_promql)
        self.assertTrue(is_feature_supported(rule_pack, ESQL_NAMED_PARAM_BINDING))
        self.assertEqual(
            rule_pack.runtime_features[ESQL_NAMED_PARAM_BINDING]["source"], "default"
        )

    def test_run_validation_jobs_parallel_preserves_report_order(self):
        from observability_migration.adapters.source.grafana.cli import (
            _run_validation_jobs,
        )

        first_result = SimpleNamespace(dashboard_title="A", dashboard_uid="a")
        second_result = SimpleNamespace(dashboard_title="B", dashboard_uid="b")
        first_panel = SimpleNamespace(esql_query="FROM one", title="One", source_panel_id="1")
        second_panel = SimpleNamespace(esql_query="FROM two", title="Two", source_panel_id="2")

        def fake_validate(query, *_args, **_kwargs):
            return {
                "status": "pass",
                "query": query,
                "error": "",
                "fix_attempts": [],
                "analysis": {},
            }

        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.validate_query_with_fixes",
            side_effect=fake_validate,
        ) as validate:
            outputs = _run_validation_jobs(
                [(first_result, first_panel), (second_result, second_panel)],
                es_url="http://localhost:9200",
                resolver=object(),
                es_api_key=None,
                narrow_limit=10,
                workers=2,
            )

        self.assertEqual([item[2]["query"] for item in outputs], ["FROM one", "FROM two"])
        self.assertEqual(validate.call_count, 2)

    def test_run_validation_jobs_prewarms_resolver_caches_before_parallel_work(self):
        from observability_migration.adapters.source.grafana.cli import (
            _run_validation_jobs,
        )

        resolver = mock.Mock()
        panel = SimpleNamespace(esql_query="FROM one", title="One", source_panel_id="1")
        result = SimpleNamespace(dashboard_title="A", dashboard_uid="a")

        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.validate_query_with_fixes",
            return_value={
                "status": "pass",
                "query": "FROM one",
                "error": "",
                "fix_attempts": [],
                "analysis": {},
            },
        ):
            _run_validation_jobs(
                [(result, panel)],
                es_url="http://localhost:9200",
                resolver=resolver,
                es_api_key=None,
                narrow_limit=10,
                workers=2,
            )

        resolver._discover_fields.assert_called_once()
        resolver._discover_concrete_indexes.assert_called_once()

    def test_run_validation_jobs_deduplicates_identical_queries(self):
        from observability_migration.adapters.source.grafana.cli import (
            _run_validation_jobs,
        )

        first_result = SimpleNamespace(dashboard_title="A", dashboard_uid="a")
        second_result = SimpleNamespace(dashboard_title="B", dashboard_uid="b")
        first_panel = SimpleNamespace(esql_query="FROM shared", title="One", source_panel_id="1")
        second_panel = SimpleNamespace(esql_query="FROM shared", title="Two", source_panel_id="2")

        with mock.patch(
            "observability_migration.adapters.source.grafana.cli.validate_query_with_fixes",
            return_value={
                "status": "pass",
                "query": "FROM shared",
                "error": "",
                "fix_attempts": [],
                "analysis": {},
            },
        ) as validate:
            outputs = _run_validation_jobs(
                [(first_result, first_panel), (second_result, second_panel)],
                es_url="http://localhost:9200",
                resolver=object(),
                es_api_key=None,
                narrow_limit=10,
                workers=2,
            )

        self.assertEqual(len(outputs), 2)
        self.assertEqual([item[2]["query"] for item in outputs], ["FROM shared", "FROM shared"])
        self.assertEqual(validate.call_count, 1)

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
            "observability_migration.adapters.source.grafana.cli._detect_target_runtime_features",
            return_value={"promql_command_v0": {"supported": True}},
        ):
            self.assertTrue(_resolve_native_promql(args))
        with mock.patch(
            "observability_migration.adapters.source.grafana.cli._detect_target_runtime_features",
            return_value={"promql_command_v0": {"supported": False}},
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

    def test_resolve_native_promql_auto_without_es_url_defaults_to_native(self):
        """With no cluster to probe, ``auto`` optimistically defaults to native
        PROMQL (highest-fidelity path). ``--no-native-promql`` is the opt-out."""
        from observability_migration.adapters.source.grafana.cli import (
            _resolve_native_promql,
        )
        args = SimpleNamespace(
            native_promql_flag="auto",
            es_url="",
            es_api_key="",
        )
        self.assertTrue(_resolve_native_promql(args))

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
        # Null-safe MAX collapse (previously LAST(computed_value, time_bucket));
        # see ``test_collapse_summary_uses_null_safe_aggregate_for_multi_series_ts``.
        self.assertIn("MAX(computed_value)", yaml_panel["esql"]["query"])
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
        # Null-safe MAX collapse; see test_collapse_summary_uses_null_safe_*.
        self.assertIn("MAX(Uptime)", yaml_panel["esql"]["query"])
        self.assertIn("MAX(Memory)", yaml_panel["esql"]["query"])

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
        # Null-safe MAX collapse; see test_collapse_summary_uses_null_safe_*.
        self.assertIn(
            "| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)",
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

    def test_native_promql_gauge_records_emitted_query_with_gauge_bounds(self):
        """Issue #109: a native-PROMQL gauge appends ``| EVAL _gauge_*`` to its
        emitted query, but ``panel_result.esql_query`` was set to the *bare*
        ``PROMQL …`` command. The validate-stage ``sync_result_queries_to_yaml``
        then overwrites the YAML query with that bare command, stripping the
        ``EVAL`` and orphaning the min/max/goal accessors. The recorded
        ``esql_query`` must match the emitted query so the sync is a no-op."""
        self.rule_pack.native_promql = True
        panel = {
            "title": "CPU Busy",
            "type": "gauge",
            "gridPos": {"w": 6, "h": 6, "x": 0, "y": 0},
            "fieldConfig": {
                "defaults": {
                    "unit": "percent",
                    "min": 0,
                    "max": 100,
                    "thresholds": {
                        "mode": "percentage",
                        "steps": [
                            {"value": None, "color": "green"},
                            {"value": 85, "color": "red"},
                        ],
                    },
                }
            },
            "targets": [
                {
                    "refId": "A",
                    "expr": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))',
                    "instant": True,
                }
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        self.assertEqual(yaml_panel["esql"]["type"], "gauge")
        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.lstrip().startswith("PROMQL "))
        self.assertIn("_gauge_min = 0, _gauge_max = 100, _gauge_goal = 85", query)
        self.assertEqual(yaml_panel["esql"]["minimum"], {"field": "_gauge_min"})
        self.assertEqual(yaml_panel["esql"]["maximum"], {"field": "_gauge_max"})
        self.assertEqual(yaml_panel["esql"]["goal"], {"field": "_gauge_goal"})
        # The bug: esql_query must carry the same gauge bounds as the emitted
        # query so the validate-stage resync does not strip them.
        self.assertIn("_gauge_min", result.esql_query)
        self.assertEqual(result.esql_query, query)

    def test_sync_drops_gauge_bounds_when_query_no_longer_produces_columns(self):
        """Issue #109 safety net: if a downstream resync ever replaces a gauge
        query with one that no longer produces the ``_gauge_*`` columns, the
        now-orphaned min/max/goal accessors must be dropped so the panel
        degrades gracefully instead of erroring with "Provided column name or
        index is invalid"."""
        result = migrate.MigrationResult("Dashboard", "uid")
        panel = migrate.PanelResult("CPU Busy", "gauge", "gauge", "migrated", 0.9)
        # Resynced query carries no ``| EVAL _gauge_*`` (columns absent).
        panel.esql_query = "PROMQL index=metrics-* step=1m value=(avg(rate(node_cpu_seconds_total[5m])))"
        result.panel_results = [panel]
        result.yaml_panel_results = [panel]

        payload = {
            "dashboards": [{
                "name": "Dashboard",
                "panels": [
                    {
                        "title": "CPU Busy",
                        "esql": {
                            "type": "gauge",
                            "query": (
                                "PROMQL index=metrics-* step=1m value=(avg(rate(node_cpu_seconds_total[5m])))"
                                "\n| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 85"
                            ),
                            "metric": {"field": "value"},
                            "minimum": {"field": "_gauge_min"},
                            "maximum": {"field": "_gauge_max"},
                            "goal": {"field": "_gauge_goal"},
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

        esql = rewritten["dashboards"][0]["panels"][0]["esql"]
        self.assertNotIn("minimum", esql)
        self.assertNotIn("maximum", esql)
        self.assertNotIn("goal", esql)
        self.assertEqual(esql["metric"], {"field": "value"})

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
        # Null-safe MAX collapse; see test_collapse_summary_uses_null_safe_*.
        self.assertIn(
            "| STATS time_bucket = MAX(time_bucket), node_cpu_seconds_total = MAX(node_cpu_seconds_total)",
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

    def test_same_uid_targets_with_missing_type_are_not_mixed_datasource(self):
        panel = {
            "title": "AWS EC2",
            "type": "graph",
            "gridPos": {"w": 24, "h": 8, "x": 0, "y": 0},
            "targets": [
                {
                    "refId": "A",
                    "namespace": "AWS/EC2",
                    "metricName": "CPUUtilization",
                    "datasource": {"type": "cloudwatch", "uid": "cloudwatch-uid-abc"},
                },
                {
                    "refId": "B",
                    "namespace": "AWS/EC2",
                    "metricName": "NetworkIn",
                    "datasource": {"uid": "cloudwatch-uid-abc"},
                },
            ],
        }

        _, result = self.translate_panel(panel)

        self.assertEqual(result.status, "requires_manual")
        self.assertEqual(result.readiness, "manual_only")
        self.assertEqual(result.reasons, ["No PromQL expression found in panel targets"])
        self.assertFalse(any("mixed datasource" in note.lower() for note in result.notes))

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
        """L1 starts from the faithful coord transform (Grafana
        ``y=4`` -> Kibana ``y=round(4*1.5)=6``), but the downstream
        ``kb-dashboard-cli`` compile step refuses any panel overlap
        in the YAML. So after L1+L2+style-guide post-processing, a
        final ``_resolve_panel_overlaps`` pass pushes overlapping
        panels' y values down to the bottom of their conflicting
        neighbour. That keeps the migration compile-clean for source
        dashboards that have overlapping or "stacked" panels.
        """
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
        # Grafana y=0 -> Kibana y=0 (after min-y normalisation)
        self.assertEqual(panels[0]["position"], {"x": 0, "y": 0})
        # Top has h=12 (after L1 scale 8*1.5=12), so Bottom is pushed
        # down to y=12 to avoid overlap. The Grafana 4-row stacking
        # intent is preserved in spirit (Bottom is below Top), just
        # without the literal pixel-level overlap that compile would
        # reject.
        top_h = panels[0]["size"]["h"]
        self.assertGreaterEqual(panels[1]["position"]["y"], top_h)

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

    def test_target_translation_hints_stash_multi_label_legend_template(self):
        """The raw legendFormat must be plumbed into translation hints so the
        panel emitter can later build a composite breakdown column."""
        from observability_migration.adapters.source.grafana.panels import (
            _target_translation_hints,
        )
        panel = {"type": "timeseries"}
        target = {"legendFormat": "{{ method }} {{ path }} - {{ status }}"}
        hints = _target_translation_hints(panel, panel_type="timeseries", target=target)
        self.assertEqual(hints.get("legend_format_template"),
                         "{{ method }} {{ path }} - {{ status }}")

    def test_target_translation_hints_skip_single_label_legend_template(self):
        """Single-label legends don't need the composite-breakdown treatment;
        existing single-field breakdown already works correctly."""
        from observability_migration.adapters.source.grafana.panels import (
            _target_translation_hints,
        )
        panel = {"type": "timeseries"}
        target = {"legendFormat": "{{ method }}"}
        hints = _target_translation_hints(panel, panel_type="timeseries", target=target)
        self.assertNotIn("legend_format_template", hints)

    def test_apply_composite_legend_to_xy_panel_inserts_concat(self):
        from observability_migration.adapters.source.grafana.panels import (
            _apply_composite_legend_to_xy_panel,
        )
        panel = {
            "esql": {
                "type": "line",
                "query": (
                    "PROMQL index=metrics-* value=(http_requests_total)\n"
                    "| EVAL method = MV_FIRST(SPLIT(_timeseries, \"\"))\n"
                    "| EVAL path = MV_FIRST(SPLIT(_timeseries, \"\"))\n"
                    "| EVAL status = MV_FIRST(SPLIT(_timeseries, \"\"))\n"
                    "| KEEP step, value, method, path, status"
                ),
                "breakdown": {"field": "method"},
            }
        }
        result = _apply_composite_legend_to_xy_panel(
            panel,
            legend_format_template="{{ method }} {{ path }} - {{ status }}",
            legend_labels=["method", "path", "status"],
        )
        query = result["esql"]["query"]
        self.assertIn('EVAL legend = CONCAT(', query)
        self.assertIn('COALESCE(TO_STRING(method), "")', query)
        self.assertIn('COALESCE(TO_STRING(path), "")', query)
        self.assertIn('COALESCE(TO_STRING(status), "")', query)
        self.assertIn('" - "', query)
        last_keep = query.strip().splitlines()[-1]
        self.assertIn("step", last_keep)
        self.assertIn("value", last_keep)
        self.assertIn("legend", last_keep)
        # The per-label columns (method/path/status) must remain in KEEP
        # alongside ``legend`` so downstream consumers can distinguish
        # series; Lens uses ``breakdown.field = "legend"`` and ignores
        # the others when rendering.
        for label in ("method", "path", "status"):
            self.assertIn(label, last_keep.split('|', 1)[1])
        self.assertEqual(result["esql"]["breakdown"]["field"], "legend")

    def test_apply_composite_legend_resolves_prometheus_labels_prefix(self):
        """Translated ES|QL path uses prometheus.labels.X column names."""
        from observability_migration.adapters.source.grafana.panels import (
            _apply_composite_legend_to_xy_panel,
        )
        panel = {
            "esql": {
                "type": "line",
                "query": (
                    "TS metrics-prometheus.remote_write-express\n"
                    "| STATS http_requests_total = AVG(RATE(prometheus.http_requests_total.counter, 5m)) "
                    "BY time_bucket = TBUCKET(5 minute), prometheus.labels.method, "
                    "prometheus.labels.path, prometheus.labels.status\n"
                    "| SORT time_bucket ASC"
                ),
                "breakdown": {"field": "prometheus.labels.method"},
            }
        }
        result = _apply_composite_legend_to_xy_panel(
            panel,
            legend_format_template="{{ method }} {{ path }} - {{ status }}",
            legend_labels=["method", "path", "status"],
        )
        query = result["esql"]["query"]
        self.assertIn('COALESCE(TO_STRING(prometheus.labels.method), "")', query)
        self.assertIn('COALESCE(TO_STRING(prometheus.labels.path), "")', query)
        self.assertIn('COALESCE(TO_STRING(prometheus.labels.status), "")', query)
        self.assertEqual(result["esql"]["breakdown"]["field"], "legend")

    def test_apply_composite_legend_no_op_when_label_missing_from_query(self):
        from observability_migration.adapters.source.grafana.panels import (
            _apply_composite_legend_to_xy_panel,
        )
        panel = {
            "esql": {
                "type": "line",
                "query": "FROM metrics-* | STATS x = COUNT(*) BY time, method | KEEP time, x, method",
                "breakdown": {"field": "method"},
            }
        }
        result = _apply_composite_legend_to_xy_panel(
            panel,
            legend_format_template="{{ method }} {{ status }}",
            legend_labels=["method", "status"],
        )
        self.assertNotIn("EVAL legend", result["esql"]["query"])
        self.assertEqual(result["esql"]["breakdown"]["field"], "method")

    def test_apply_composite_legend_skip_for_single_label_template(self):
        from observability_migration.adapters.source.grafana.panels import (
            _apply_composite_legend_to_xy_panel,
        )
        panel = {
            "esql": {
                "type": "line",
                "query": "FROM metrics-* | STATS x = COUNT(*) BY time, method | KEEP time, x, method",
                "breakdown": {"field": "method"},
            }
        }
        result = _apply_composite_legend_to_xy_panel(
            panel,
            legend_format_template="{{ method }}",
            legend_labels=["method"],
        )
        self.assertNotIn("EVAL legend", result["esql"]["query"])

    def test_apply_composite_legend_escapes_quotes_in_literal_text(self):
        from observability_migration.adapters.source.grafana.panels import (
            _apply_composite_legend_to_xy_panel,
        )
        panel = {
            "esql": {
                "type": "line",
                "query": "FROM metrics-* | KEEP time, value, a, b",
                "breakdown": {"field": "a"},
            }
        }
        result = _apply_composite_legend_to_xy_panel(
            panel,
            legend_format_template='{{ a }} "literal" {{ b }}',
            legend_labels=["a", "b"],
        )
        query = result["esql"]["query"]
        self.assertIn('\\"literal\\"', query)

    def test_split_esql_pipeline_handles_triple_quoted_strings(self):
        """ES|QL ``\"\"\"...\"\"\"`` raw strings must not flip the quote state on
        each individual ``\"`` character. Otherwise pipeline stages after a
        triple-quoted literal get swallowed.
        """
        from observability_migration.targets.kibana.emit.esql_utils import split_esql_pipeline
        query = (
            'FROM metrics-*\n'
            '| EVAL x = REPLACE(_timeseries, """.*"method":"([^"]+)".*""", "$1")\n'
            '| KEEP step, x'
        )
        stages = split_esql_pipeline(query)
        self.assertEqual(len(stages), 3)
        self.assertTrue(stages[0].startswith("FROM metrics-"))
        self.assertTrue(stages[1].startswith("EVAL x ="))
        self.assertTrue(stages[2].startswith("KEEP"))

    def test_split_esql_pipeline_handles_pipe_inside_triple_quoted_string(self):
        """A ``|`` inside a ``\"\"\"...\"\"\"`` literal must not split the pipeline."""
        from observability_migration.targets.kibana.emit.esql_utils import split_esql_pipeline
        query = 'FROM metrics-* | EVAL y = REPLACE(x, """foo|bar""", "z") | KEEP y'
        stages = split_esql_pipeline(query)
        self.assertEqual(len(stages), 3)
        self.assertIn("foo|bar", stages[1])

    def test_split_esql_pipeline_handles_consecutive_triple_quoted_strings(self):
        """Two triple-quoted literals in a row must each open + close cleanly."""
        from observability_migration.targets.kibana.emit.esql_utils import split_esql_pipeline
        query = (
            'FROM x\n'
            '| EVAL a = REPLACE(b, """A""", "x"), '
            'c = REPLACE(d, """B""", "y")\n'
            '| KEEP a, c'
        )
        stages = split_esql_pipeline(query)
        self.assertEqual(len(stages), 3)

    def test_split_esql_pipeline_native_promql_keep_line_is_visible(self):
        """End-to-end: the native PROMQL emission's trailing KEEP line must be
        recoverable from the splitter so downstream column extraction works.
        """
        from observability_migration.targets.kibana.emit.esql_utils import split_esql_pipeline
        query = (
            'PROMQL index=metrics-* step=1m value=(http_requests_total)\n'
            '| EVAL _ts = COALESCE(_timeseries, "")\n'
            '| EVAL _raw_method = CASE(_ts == "", "unknown", '
            'REPLACE(_ts, """.*"method":"([^"]+)".*""", "$1"))\n'
            '| EVAL method = CASE(STARTS_WITH(_raw_method, "{"), '
            'REPLACE(REPLACE(_ts, """[{}""]""", ""), ",", ", "), _raw_method)\n'
            '| EVAL _raw_path = CASE(_ts == "", "unknown", '
            'REPLACE(_ts, """.*"path":"([^"]+)".*""", "$1"))\n'
            '| EVAL path = CASE(STARTS_WITH(_raw_path, "{"), '
            'REPLACE(REPLACE(_ts, """[{}""]""", ""), ",", ", "), _raw_path)\n'
            '| EVAL _raw_status = CASE(_ts == "", "unknown", '
            'REPLACE(_ts, """.*"status":"([^"]+)".*""", "$1"))\n'
            '| EVAL status = CASE(STARTS_WITH(_raw_status, "{"), '
            'REPLACE(REPLACE(_ts, """[{}""]""", ""), ",", ", "), _raw_status)\n'
            '| KEEP step, value, method, path, status'
        )
        stages = split_esql_pipeline(query)
        self.assertEqual(len(stages), 9)
        self.assertTrue(stages[-1].lower().startswith("keep "))

    def test_composite_legend_helper_applies_after_pipeline_fix(self):
        """End-to-end: the composite-legend helper must successfully resolve
        ``method``/``path``/``status`` against the native PROMQL output and emit
        ``EVAL legend = CONCAT(...)`` plus ``breakdown.field = \"legend\"``. Before
        the pipeline-splitter fix this test failed because ``_extract_keep_columns``
        returned ``[]``.
        """
        from observability_migration.adapters.source.grafana.panels import (
            _apply_composite_legend_to_xy_panel,
        )
        panel = {
            "esql": {
                "type": "line",
                "query": (
                    'PROMQL index=metrics-* step=1m value=(http_requests_total)\n'
                    '| EVAL _ts = COALESCE(_timeseries, "")\n'
                    '| EVAL _raw_method = CASE(_ts == "", "unknown", '
                    'REPLACE(_ts, """.*"method":"([^"]+)".*""", "$1"))\n'
                    '| EVAL method = CASE(STARTS_WITH(_raw_method, "{"), '
                    'REPLACE(REPLACE(_ts, """[{}""]""", ""), ",", ", "), _raw_method)\n'
                    '| EVAL _raw_path = CASE(_ts == "", "unknown", '
                    'REPLACE(_ts, """.*"path":"([^"]+)".*""", "$1"))\n'
                    '| EVAL path = CASE(STARTS_WITH(_raw_path, "{"), '
                    'REPLACE(REPLACE(_ts, """[{}""]""", ""), ",", ", "), _raw_path)\n'
                    '| EVAL _raw_status = CASE(_ts == "", "unknown", '
                    'REPLACE(_ts, """.*"status":"([^"]+)".*""", "$1"))\n'
                    '| EVAL status = CASE(STARTS_WITH(_raw_status, "{"), '
                    'REPLACE(REPLACE(_ts, """[{}""]""", ""), ",", ", "), _raw_status)\n'
                    '| KEEP step, value, method, path, status'
                ),
                "breakdown": {"field": "method"},
            }
        }
        result = _apply_composite_legend_to_xy_panel(
            panel,
            legend_format_template="{{ method }} {{ path }} - {{ status }}",
            legend_labels=["method", "path", "status"],
        )
        self.assertEqual(result["esql"]["breakdown"]["field"], "legend")
        self.assertIn('EVAL legend = CONCAT(', result["esql"]["query"])
        self.assertIn('COALESCE(TO_STRING(method), "")', result["esql"]["query"])
        self.assertIn('COALESCE(TO_STRING(path), "")', result["esql"]["query"])
        self.assertIn('COALESCE(TO_STRING(status), "")', result["esql"]["query"])


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

    def test_report_includes_runtime_feature_profile(self):
        from observability_migration.adapters.source.grafana.runtime_features import PROMQL_COMMAND_V0
        from observability_migration.core.reporting.report import save_detailed_report

        result = migrate.MigrationResult("Dash", "uid-1")
        result.runtime_features = {
            PROMQL_COMMAND_V0: {
                "supported": True,
                "source": "default",
                "confidence": "unverified",
                "level": "runtime",
                "reason": "no --es-url configured; native PROMQL assumed for offline migration",
            }
        }
        import os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            path = f.name
        save_detailed_report([result], [], path)
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["runtime_features"], result.runtime_features)
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
        _, group_panels, _is_row, _collapsed = groups[0]
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
        _, group_panels, _is_row, _collapsed = groups[0]
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
        # Issue #23: source collapsed=true must round-trip to section.collapsed=true.
        self.assertIs(sections[0]["section"]["collapsed"], True)

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
        """After Kibana-native layout (the no-geometry fallback path),
        height is determined by panel type via ``KIBANA_TYPE_HEIGHT``
        and then clamped to the L2 per-type minimum (so a ``metric``
        gets ``max(KIBANA_TYPE_HEIGHT['metric']=5, L2 min h=6) = 6``)."""
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout
        panels = [
            {"title": "Chart", "esql": {"type": "line"}, "size": {"w": 48, "h": 8}, "position": {"x": 0, "y": 0}, "_grafana_row_y": 0, "_grafana_row_x": 0},
            {"title": "Metric", "esql": {"type": "metric"}, "size": {"w": 48, "h": 8}, "position": {"x": 0, "y": 0}, "_grafana_row_y": 5, "_grafana_row_x": 0},
        ]
        result = _apply_kibana_native_layout(panels)
        self.assertEqual(result[0]["size"]["h"], 12, "line chart should be h=12")
        # Metric: KIBANA_TYPE_HEIGHT=5 -> L2 clamp -> 6
        self.assertEqual(result[1]["size"]["h"], 6, "metric should be h=6 (L2 min)")

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
        # ``Head chunks count`` uses a single-metric instant vector, so
        # it still routes through native PROMQL and the value metric is
        # relabelled with the panel title.
        head_chunks = panels_by_title["Head chunks count"]
        self.assertEqual(head_chunks["esql"]["metrics"][0]["label"], "Head chunk count")
        # ``Length of head block`` does ``A - B`` between two distinct
        # metric vectors (``prometheus_tsdb_head_max_time`` minus
        # ``prometheus_tsdb_head_min_time``). The distinct-metric
        # subtraction itself stays native now (#138), but this panel's
        # label matchers use a Grafana template variable
        # (``{instance="$instance"}``), which ``can_use_native_promql``
        # rejects, so it falls through to ES|QL translation — which
        # performs the subtraction at bucket level and stores the result
        # in a ``computed_value`` field. The native-PROMQL value-
        # relabelling path therefore doesn't apply.
        head_block = panels_by_title["Length of head block"]
        head_block_query = head_block["esql"].get("query", "")
        self.assertNotIn("PROMQL index=", head_block_query)
        self.assertIn("computed_value", head_block_query)


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
        # metric: KIBANA_TYPE_HEIGHT=5 -> L2 min_h=6 -> 6
        self.assertEqual(panels[1]["size"]["h"], 6)

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
        """Single-row group: position is shifted so the topmost panel
        sits at y=0; relative x is preserved at scale=2."""
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

        # bar's L2 min_w=8 *would* bump Pressure's right edge to
        # x=8, but CPU Busy sits at x=6..12 -- collision-aware L2
        # keeps Pressure at w=6 to preserve the side-by-side layout
        # the author chose. Height bumps are independent: gauge h=6
        # has no vertical neighbour to collide with so it bumps to
        # L2 gauge min_h=8.
        self.assertEqual(panels[0]["size"], {"w": 6, "h": 6}, "bar w stays at 6 (collision with CPU Busy)")
        self.assertEqual(panels[1]["size"], {"w": 6, "h": 8}, "gauge h bumps to L2 min_h=8")
        # Both panels share Grafana y=1 -> they're the topmost, so
        # both shift to Kibana y=0 (after min-y normalization).
        self.assertEqual(panels[0]["position"], {"x": 0, "y": 0})
        self.assertEqual(panels[1]["position"], {"x": 6, "y": 0})
        self.assertNotIn("_grafana_w", panels[0])
        self.assertNotIn("_grafana_h", panels[0])

    def test_style_guide_does_not_stretch_2d_grid_rows(self):
        """``apply_style_guide_layout._fill_simple_row`` must not
        rescale a row that is part of a 2D grid (panels below sharing
        the same x-range). Otherwise the row's right-edge panels get
        pushed further right, colliding with the rows below.

        Reproduces the bug in node-exporter-full pre-fix: the wide
        top row was 30 cols, scaled to 48 cols by ``_fill_simple_row``,
        which moved CPU Cores from x=18 to x=37 and broke alignment
        with RootFS Total at x=18 below it.
        """
        from observability_migration.targets.kibana.emit.layout import (
            apply_style_guide_layout,
        )

        doc = {"dashboards": [{
            "panels": [
                # Top row: 5 panels totalling 30 cols (less than 48)
                {"title": "A", "position": {"x": 0,  "y": 0}, "size": {"w": 6, "h": 6}},
                {"title": "B", "position": {"x": 6,  "y": 0}, "size": {"w": 6, "h": 6}},
                {"title": "C", "position": {"x": 12, "y": 0}, "size": {"w": 6, "h": 6}},
                {"title": "D", "position": {"x": 18, "y": 0}, "size": {"w": 4, "h": 3}},
                {"title": "E", "position": {"x": 22, "y": 0}, "size": {"w": 8, "h": 3}},
                # Below-row: stat tiles at the right share x with D/E
                {"title": "D2", "position": {"x": 18, "y": 3}, "size": {"w": 4, "h": 3}},
                {"title": "E2", "position": {"x": 22, "y": 3}, "size": {"w": 8, "h": 3}},
            ],
        }]}

        apply_style_guide_layout(doc)
        panels = {p["title"]: p for p in doc["dashboards"][0]["panels"]}

        # D must stay at x=18 (not pushed by row-stretch); D2 below
        # must still align at x=18 underneath it.
        self.assertEqual(panels["D"]["position"]["x"], 18,
                         "row stretch must not move D out from under D2")
        self.assertEqual(panels["D2"]["position"]["x"], 18,
                         "D2 keeps its source x position")
        # And the source widths are preserved (no stretch).
        self.assertEqual(panels["A"]["size"]["w"], 6,
                         "row width must not be stretched -- 2D grid below should suppress _fill_simple_row")

    def test_style_guide_still_stretches_pure_1d_row(self):
        """When there is no 2D grid below it, the row IS stretched to
        the full 48 cols (the original purpose of
        ``_fill_simple_row``). This is the negative control for the
        2D-grid check.
        """
        from observability_migration.targets.kibana.emit.layout import (
            apply_style_guide_layout,
        )

        doc = {"dashboards": [{
            "panels": [
                {"title": "A", "position": {"x": 0,  "y": 0}, "size": {"w": 12, "h": 6}},
                {"title": "B", "position": {"x": 12, "y": 0}, "size": {"w": 12, "h": 6}},
                # No below-row panels -> still a pure 1D row
            ],
        }]}

        apply_style_guide_layout(doc)
        panels = {p["title"]: p for p in doc["dashboards"][0]["panels"]}
        # Both widths scaled up to fill 48 cols (24+24).
        total_w = panels["A"]["size"]["w"] + panels["B"]["size"]["w"]
        self.assertEqual(total_w, 48, "pure 1D row should still be stretched to 48 cols")

    def test_style_guide_stretches_row_with_full_width_panel_above(self):
        """A full-width panel ABOVE a simple 1D row must NOT suppress the
        fill.  Previously, _row_has_overlapping_x_neighbours checked above
        panels too, so a full-width header caused false positives.  After
        the fix only panels strictly BELOW the row are checked.
        """
        from observability_migration.targets.kibana.emit.layout import (
            apply_style_guide_layout,
        )

        doc = {"dashboards": [{
            "panels": [
                # Full-width chart at y=0
                {"title": "Header", "position": {"x": 0, "y": 0}, "size": {"w": 48, "h": 8}},
                # Simple 1D row at y=8 — total=24, should be stretched to 48
                {"title": "A", "position": {"x": 0,  "y": 8}, "size": {"w": 12, "h": 6}},
                {"title": "B", "position": {"x": 12, "y": 8}, "size": {"w": 12, "h": 6}},
            ],
        }]}

        apply_style_guide_layout(doc)
        panels = {p["title"]: p for p in doc["dashboards"][0]["panels"]}
        total_w = panels["A"]["size"]["w"] + panels["B"]["size"]["w"]
        self.assertEqual(
            total_w, 48,
            "row below a full-width header should still be stretched (no 2D grid below it)",
        )

    def test_style_guide_still_blocks_stretch_when_2d_grid_is_below(self):
        """A full-width header ABOVE plus a 2D grid BELOW: the row must
        NOT be stretched because the 2D grid below would break.
        """
        from observability_migration.targets.kibana.emit.layout import (
            apply_style_guide_layout,
        )

        doc = {"dashboards": [{
            "panels": [
                # Full-width chart above
                {"title": "Header", "position": {"x": 0, "y": 0}, "size": {"w": 48, "h": 8}},
                # Row to check: 3 panels totalling 30 cols (needs fill)
                {"title": "A", "position": {"x": 0,  "y": 8}, "size": {"w": 10, "h": 4}},
                {"title": "B", "position": {"x": 10, "y": 8}, "size": {"w": 10, "h": 4}},
                {"title": "C", "position": {"x": 20, "y": 8}, "size": {"w": 10, "h": 4}},
                # 2D grid BELOW: panel at same x as C
                {"title": "C2", "position": {"x": 20, "y": 12}, "size": {"w": 10, "h": 4}},
            ],
        }]}

        apply_style_guide_layout(doc)
        panels = {p["title"]: p for p in doc["dashboards"][0]["panels"]}
        self.assertEqual(
            panels["A"]["size"]["w"], 10,
            "2D grid below must suppress row stretch even when full-width header is above",
        )

    def test_kibana_native_layout_l2_yields_to_2d_grid(self):
        """L2 per-type minimums must NOT break the 2D grid the
        source author authored.

        Reproduces the ``node-exporter-full`` "Quick CPU / Mem / Disk"
        section: 6 wide gauges along the top, with two short stat
        tiles in the corner that the author *deliberately* sized to
        h=3 so they could stack two-deep beside a tall neighbour.
        Before the collision-aware fix, L2's metric ``min_h=6``
        bumped each short tile to h=6, blowing through the gauge
        below it and forcing the overlap resolver to cascade panels
        to y=8/y=6 -- producing the ugly "right-side dangling stat
        tile cluster" layout in the screenshot at 2026-05-13 01:24.
        """
        from observability_migration.adapters.source.grafana.panels import (
            _apply_kibana_native_layout,
        )

        # Tall gauge on the left + two stacked short stats on its
        # right. Heights 8 and (3 + 3) tile to the same 8 rows.
        panels = [
            {"title": "GaugeLeft", "esql": {"type": "gauge"},
             "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": 0,
             "_grafana_w": 12, "_grafana_h": 6},
            {"title": "StatTop", "esql": {"type": "metric"},
             "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": 12,
             "_grafana_w": 8, "_grafana_h": 3},
            {"title": "StatBottom", "esql": {"type": "metric"},
             "size": {}, "position": {},
             "_grafana_row_y": 3, "_grafana_row_x": 12,
             "_grafana_w": 8, "_grafana_h": 3},
        ]
        _apply_kibana_native_layout(panels)

        gauge, top, bot = panels
        # StatTop's min_h=6 *would* push its bottom to y=6,
        # overlapping StatBottom at y=5..8 (scaled). Collision-
        # aware L2 keeps the source-author-chosen short height for
        # StatTop so the 2D grid remains intact.
        self.assertLessEqual(
            top["size"]["h"], 5,
            "StatTop must keep its short height when StatBottom is below it; "
            f"got h={top['size']['h']} which would clash",
        )
        # StatBottom has nothing below in this group so the L2 min_h
        # can be applied to it.
        self.assertGreaterEqual(top["position"]["y"] + top["size"]["h"], bot["position"]["y"])
        # And the gauge keeps its faithful height (no false collision).
        self.assertGreater(gauge["size"]["h"], 0)

    def test_kibana_native_layout_preserves_relative_y_spacing(self):
        """L1 universal fix: when multiple Grafana visual rows are
        present, preserve their *relative* spacing in Kibana instead
        of stacking them sequentially with a y-cursor.

        Grafana ``y`` values map to Kibana via the row scale
        ``GRAFANA_ROW_HEIGHT_PX / KIBANA_ROW_HEIGHT_PX = 30/20 = 1.5``,
        and the whole group is shifted so the topmost panel sits at
        Kibana y=0. This means two panels at Grafana y=1 and y=3 stay
        2 rows apart in Grafana (so 3 rows apart in Kibana after the
        1.5x scale) instead of collapsing to "stacked with no gap".
        """
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout

        panels = [
            # Top row at Grafana y=1
            {"title": "TopL", "esql": {"type": "bar"},
             "size": {}, "position": {},
             "_grafana_row_y": 1, "_grafana_row_x": 0,
             "_grafana_w": 12, "_grafana_h": 4},
            {"title": "TopR", "esql": {"type": "bar"},
             "size": {}, "position": {},
             "_grafana_row_y": 1, "_grafana_row_x": 12,
             "_grafana_w": 12, "_grafana_h": 4},
            # Lower row at Grafana y=10 (9 rows lower, after a TALL
            # gap)
            {"title": "BotL", "esql": {"type": "bar"},
             "size": {}, "position": {},
             "_grafana_row_y": 10, "_grafana_row_x": 0,
             "_grafana_w": 12, "_grafana_h": 8},
        ]
        _apply_kibana_native_layout(panels)
        # Scale x by 2, scale y by 1.5.
        # min Grafana y = 1, so all panels shift down by round(1*1.5)=2.
        # TopL/TopR: y=1 -> round(1*1.5)=2 -> 2-2=0
        # BotL:      y=10 -> round(10*1.5)=15 -> 15-2=13
        self.assertEqual(panels[0]["position"]["y"], 0, "TopL")
        self.assertEqual(panels[1]["position"]["y"], 0, "TopR")
        self.assertEqual(panels[2]["position"]["y"], 13, "BotL")
        # Sanity: heights also scale by 1.5
        self.assertEqual(panels[0]["size"]["h"], 6)
        self.assertEqual(panels[2]["size"]["h"], 12)

    def test_kibana_native_layout_single_panel(self):
        """Trivial case: a single panel always lands at y=0 regardless
        of its Grafana y, because it's the only panel in the group so
        min-y normalization shifts it to zero."""
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout

        panels = [{
            "title": "Solo", "esql": {"type": "bar"},
            "size": {}, "position": {},
            "_grafana_row_y": 7, "_grafana_row_x": 5,
            "_grafana_w": 8, "_grafana_h": 6,
        }]
        _apply_kibana_native_layout(panels)
        self.assertEqual(panels[0]["position"]["y"], 0)
        self.assertEqual(panels[0]["position"]["x"], 10)  # 5*2

    def test_kibana_native_layout_preserves_grafana_layered_panels(self):
        """Two panels at the same Grafana y but different heights
        (one tall, one short) should both start at the same Kibana y
        — Kibana's grid layout handles their different heights
        naturally without our code packing them."""
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout

        panels = [
            # Tall panel
            {"title": "Tall", "esql": {"type": "bar"},
             "size": {}, "position": {},
             "_grafana_row_y": 4, "_grafana_row_x": 0,
             "_grafana_w": 12, "_grafana_h": 10},
            # Short panel, same y
            {"title": "Short", "esql": {"type": "metric"},
             "size": {}, "position": {},
             "_grafana_row_y": 4, "_grafana_row_x": 12,
             "_grafana_w": 12, "_grafana_h": 4},
        ]
        _apply_kibana_native_layout(panels)
        self.assertEqual(panels[0]["position"]["y"], 0)
        self.assertEqual(panels[1]["position"]["y"], 0)
        # Heights differ - the L1 transform does NOT pack them; their
        # different bottoms stay different (Kibana renders them as-is)
        self.assertEqual(panels[0]["size"]["h"], 15)  # round(10*1.5)
        # Metric has a 5-row default applied by _normalize_tile_size
        # so we don't assert the exact short panel height here.

    def test_kibana_native_layout_keeps_touching_panels_touching(self):
        """L1 edge-alignment: two Grafana panels that exactly touch
        (Grafana ``y=25,h=6`` followed by ``y=31``) must remain
        exactly touching in Kibana — not overlapping, not gapped.

        Without edge alignment (independently scaling y and h with
        banker's rounding), this case used to produce a 1-row
        overlap which the downstream ``kb-dashboard-cli`` compile
        step rejects.
        """
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout

        panels = [
            {"title": "Top", "esql": {"type": "bar"},
             "size": {}, "position": {},
             "_grafana_row_y": 25, "_grafana_row_x": 0,
             "_grafana_w": 24, "_grafana_h": 6},
            {"title": "Bottom", "esql": {"type": "bar"},
             "size": {}, "position": {},
             "_grafana_row_y": 31, "_grafana_row_x": 0,
             "_grafana_w": 12, "_grafana_h": 4},
        ]
        _apply_kibana_native_layout(panels)
        top_bottom = panels[0]["position"]["y"] + panels[0]["size"]["h"]
        bot_top = panels[1]["position"]["y"]
        self.assertEqual(
            top_bottom, bot_top,
            f"touching panels must remain touching after L1 scaling "
            f"(top bottom={top_bottom}, bottom top={bot_top})",
        )

    def test_kibana_native_layout_preserves_grafana_vertical_gaps(self):
        """L1 universal layout: when the Grafana author left a gap
        between two rows (eg. y=0..4 row, then y=10 row), that gap
        is preserved (proportionally) in Kibana instead of being
        collapsed by a y-cursor.

        Compare to the legacy y-cursor behaviour where two rows
        always stacked immediately regardless of the source spacing.
        """
        from observability_migration.adapters.source.grafana.panels import _apply_kibana_native_layout

        panels = [
            {"title": "TopRow", "esql": {"type": "bar"},
             "size": {}, "position": {},
             "_grafana_row_y": 0, "_grafana_row_x": 0,
             "_grafana_w": 24, "_grafana_h": 4},
            # Big gap: y=4 to y=10 is empty in Grafana
            {"title": "BotRow", "esql": {"type": "bar"},
             "size": {}, "position": {},
             "_grafana_row_y": 10, "_grafana_row_x": 0,
             "_grafana_w": 24, "_grafana_h": 4},
        ]
        _apply_kibana_native_layout(panels)
        top_bottom = panels[0]["position"]["y"] + panels[0]["size"]["h"]
        bot_top = panels[1]["position"]["y"]
        gap = bot_top - top_bottom
        # Grafana gap is from y=4 to y=10 = 6 rows. Scaled by 1.5,
        # the Kibana gap should be 9 rows. The legacy y-cursor
        # behaviour would produce gap=0 (immediate stacking).
        self.assertEqual(
            gap, 9,
            f"Grafana 6-row vertical gap should scale to a 9-row "
            f"Kibana gap (got {gap}); a gap of 0 means the y-cursor "
            f"regression has returned.",
        )


    def test_fill_simple_row_bails_when_all_panels_at_hard_min(self):
        """When every panel in the row is already at HARD_MIN_W and the
        total still exceeds 48, the adjustment loop can't shrink any
        further.  The function must bail out (leave panels unchanged)
        instead of writing overflow coordinates.

        13 panels x w=4 = 52 cols.  52 is in [24, 72] so the range
        guard doesn't catch it; the overflow guard at the end must.
        """
        from observability_migration.targets.kibana.emit.layout import (
            _fill_simple_row,
        )

        panels = [
            {"title": f"P{i}", "position": {"x": i * 4, "y": 0}, "size": {"w": 4, "h": 6}}
            for i in range(13)
        ]
        original_widths = [p["size"]["w"] for p in panels]
        original_xs = [p["position"]["x"] for p in panels]

        _fill_simple_row(panels)

        # Panels must be unchanged — no overflow written.
        self.assertEqual([p["size"]["w"] for p in panels], original_widths)
        self.assertEqual([p["position"]["x"] for p in panels], original_xs)

    def test_fill_simple_row_total_below_range_is_left_unchanged(self):
        """Row totalling less than 50% of 48 cols (< 24) is left alone."""
        from observability_migration.targets.kibana.emit.layout import _fill_simple_row

        panels = [
            {"title": "A", "position": {"x": 0, "y": 0}, "size": {"w": 8, "h": 6}},
            {"title": "B", "position": {"x": 8, "y": 0}, "size": {"w": 4, "h": 6}},
        ]
        _fill_simple_row(panels)
        # total=12 < 24 (50% of 48) → unchanged
        self.assertEqual(panels[0]["size"]["w"], 8)
        self.assertEqual(panels[1]["size"]["w"], 4)

    def test_is_simple_contiguous_row_tolerates_missing_keys(self):
        """_is_simple_contiguous_row must not raise KeyError when a
        panel is missing 'position' or 'size' keys."""
        from observability_migration.targets.kibana.emit.layout import (
            _is_simple_contiguous_row,
        )

        panels_no_pos = [
            {},
            {"size": {"w": 12}},
        ]
        # Should return False (x=0 ≤ 2, but gap check sees missing w → w=0
        # so prev_end=0, curr=0, gap=0 ≤ 2 → True) — important: must not raise
        try:
            result = _is_simple_contiguous_row(panels_no_pos)
            self.assertIsInstance(result, bool)
        except (KeyError, TypeError, AttributeError) as exc:
            self.fail(f"_is_simple_contiguous_row raised {type(exc).__name__}: {exc}")

    def test_kibana_type_height_ge_l2_min_h_for_all_types(self):
        """KIBANA_TYPE_HEIGHT values must be >= the corresponding
        _TYPE_SIZE_CONSTRAINTS min_h so the fallback path never produces
        heights that L2 immediately has to correct.

        If this test fails, update KIBANA_TYPE_HEIGHT to match min_h.
        """
        from observability_migration.adapters.source.grafana.panels import (
            _TYPE_SIZE_CONSTRAINTS,
            KIBANA_TYPE_HEIGHT,
        )

        for vtype, (_min_w, min_h, _max_h) in _TYPE_SIZE_CONSTRAINTS.items():
            height = KIBANA_TYPE_HEIGHT.get(vtype)
            if height is not None:
                self.assertGreaterEqual(
                    height,
                    min_h,
                    f"KIBANA_TYPE_HEIGHT['{vtype}']={height} < _TYPE_SIZE_CONSTRAINTS min_h={min_h}; "
                    "update KIBANA_TYPE_HEIGHT to eliminate the mismatch",
                )


class L4RepeatPanelExpansionTests(unittest.TestCase):
    """L4 universal fix: expand ``repeat: "$var"`` panels into N
    concrete clones (one per variable value), with PromQL,
    title, and ``gridPos`` updated per clone.

    Before L4, repeated Grafana panels were collapsed: the variable
    became a single-select Kibana control, and only one panel was
    emitted. The author's "show me one chart per instance" intent
    was lost entirely.

    L4 resolves variable values from:

    * ``variable["options"]`` (custom vars / explicit lists), then
    * ``variable["current"]["text"]``  (the last-resolved set
      Grafana cached when the dashboard was saved).

    Variables that can't be resolved this way (unconfigured query
    vars) keep the single-panel behaviour and emit a warning. The
    expansion is capped at 8 panels to prevent dashboard explosion
    for high-cardinality vars; the rest are dropped with a warning.
    """

    def setUp(self):
        from observability_migration.adapters.source.grafana import (
            rules as rules_mod,
        )
        from observability_migration.adapters.source.grafana import (
            schema as schema_mod,
        )
        self.rule_pack = rules_mod.RulePackConfig()
        self.resolver = schema_mod.SchemaResolver(self.rule_pack)

    def _translate(self, dashboard):
        from observability_migration.adapters.source.grafana import (
            panels as panels_mod,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result, yaml_path = panels_mod.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            with open(yaml_path) as f:
                doc = yaml.safe_load(f)
        return result, doc["dashboards"][0]

    def _walk_leaves(self, panels):
        for p in panels or []:
            if isinstance(p, dict) and "section" in p:
                yield from self._walk_leaves(p["section"].get("panels") or [])
            elif isinstance(p, dict):
                yield p

    def test_custom_variable_repeat_fans_out_three_panels(self):
        """A panel with ``repeat: instance`` against a custom var
        with 3 options should produce 3 leaf panels with
        substituted titles."""
        dashboard = {
            "title": "Repeat Custom", "uid": "rep-custom-1", "schemaVersion": 39,
            "templating": {"list": [{
                "name": "instance", "type": "custom",
                "query": "alpha, beta, gamma",
                "options": [
                    {"text": "alpha", "value": "alpha"},
                    {"text": "beta", "value": "beta"},
                    {"text": "gamma", "value": "gamma"},
                ],
            }]},
            "panels": [
                {"id": 1, "type": "stat", "title": "CPU on $instance",
                 "repeat": "instance",
                 "gridPos": {"x": 0, "y": 0, "w": 8, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "rate(node_cpu{instance=\"$instance\"}[5m])", "refId": "A"}]},
            ],
        }
        _, dash = self._translate(dashboard)
        leaves = list(self._walk_leaves(dash.get("panels") or []))
        titles = [p.get("title") for p in leaves]
        self.assertEqual(
            titles,
            ["CPU on alpha", "CPU on beta", "CPU on gamma"],
            "Each clone must substitute $instance in the panel title",
        )

    def test_repeat_substitutes_promql_variable_references(self):
        """Each clone's PromQL must reference the clone's specific
        variable value, not the literal ``$instance``."""
        dashboard = {
            "title": "T", "uid": "rep-promql-1", "schemaVersion": 39,
            "templating": {"list": [{
                "name": "instance", "type": "custom",
                "options": [
                    {"text": "a", "value": "a"},
                    {"text": "b", "value": "b"},
                ],
            }]},
            "panels": [
                {"id": 1, "type": "stat", "title": "x",
                 "repeat": "instance",
                 "gridPos": {"x": 0, "y": 0, "w": 8, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up{instance=\"$instance\"}", "refId": "A"}]},
            ],
        }
        _, dash = self._translate(dashboard)
        leaves = list(self._walk_leaves(dash.get("panels") or []))
        queries = [
            (p.get("esql") or {}).get("query", "")
            for p in leaves
            if p.get("esql")
        ]
        # We can't predict the exact ESQL, but each query should
        # mention the clone's value and not the literal $instance.
        a_clone = next((q for q in queries if "service.instance.id" in q and '"a"' in q), None)
        b_clone = next((q for q in queries if "service.instance.id" in q and '"b"' in q), None)
        self.assertIsNotNone(a_clone, f"clone for instance=a missing; queries={queries!r}")
        self.assertIsNotNone(b_clone, f"clone for instance=b missing; queries={queries!r}")
        # Neither clone should still contain the unresolved variable.
        for q in queries:
            self.assertNotIn("$instance", q, "ESQL must not contain the unresolved Grafana variable")

    def test_high_cardinality_cap_at_eight(self):
        """Variables with >8 values are capped to 8 with a warning."""
        from observability_migration.adapters.source.grafana import (
            panels as panels_mod,
        )
        values = [{"text": f"v{i}", "value": f"v{i}"} for i in range(12)]
        dashboard = {
            "title": "T", "uid": "rep-cap-1", "schemaVersion": 39,
            "templating": {"list": [{
                "name": "instance", "type": "custom", "options": values,
            }]},
            "panels": [
                {"id": 1, "type": "stat", "title": "$instance",
                 "repeat": "instance",
                 "gridPos": {"x": 0, "y": 0, "w": 8, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A"}]},
            ],
        }
        result, dash = self._translate(dashboard)
        leaves = list(self._walk_leaves(dash.get("panels") or []))
        titles = [p.get("title") for p in leaves]
        self.assertEqual(
            len(titles), panels_mod.L4_REPEAT_EXPANSION_CAP,
            f"Expected exactly {panels_mod.L4_REPEAT_EXPANSION_CAP} clones, "
            f"got {len(titles)}: {titles!r}",
        )
        # A skip result should record the cap-warning for the
        # operator.
        cap_warning_titles = [
            pr.title for pr in result.panel_results
            if pr.status == "skipped" and "repeat" in (pr.warnings or [""])[0].lower()
        ]
        self.assertTrue(cap_warning_titles, "expected a skip-result mentioning the repeat cap")

    def test_unresolvable_query_var_keeps_single_panel_with_warning(self):
        """Query vars without resolved current/options stay as a
        single Kibana panel with an explanatory warning. (We don't
        hit Elasticsearch from the translator for label values
        in v1 of L4.)"""
        dashboard = {
            "title": "T", "uid": "rep-unresolvable-1", "schemaVersion": 39,
            "templating": {"list": [{
                "name": "instance", "type": "query",
                "query": "label_values(up, instance)",
                # No options, no current value resolved.
            }]},
            "panels": [
                {"id": 1, "type": "stat", "title": "u: $instance",
                 "repeat": "instance",
                 "gridPos": {"x": 0, "y": 0, "w": 8, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A"}]},
            ],
        }
        result, dash = self._translate(dashboard)
        leaves = list(self._walk_leaves(dash.get("panels") or []))
        # Single panel preserved (legacy behaviour for this case)
        self.assertEqual(len(leaves), 1)
        # A skip warning was recorded
        unresolvable_warnings = [
            pr for pr in result.panel_results
            if pr.status == "skipped"
            and any("unresolvable" in w.lower() or "could not resolve" in w.lower()
                    for w in (pr.warnings or []))
        ]
        self.assertTrue(
            unresolvable_warnings,
            "expected a skipped PanelResult mentioning the unresolvable variable",
        )

    def test_repeat_uses_current_text_when_options_missing(self):
        """For query vars that have a cached ``current`` set
        (multi-value), we fall back to those values."""
        dashboard = {
            "title": "T", "uid": "rep-current-1", "schemaVersion": 39,
            "templating": {"list": [{
                "name": "instance", "type": "query",
                "query": "label_values(up, instance)",
                "current": {
                    "text": ["host-1", "host-2"],
                    "value": ["host-1", "host-2"],
                },
            }]},
            "panels": [
                {"id": 1, "type": "stat", "title": "$instance",
                 "repeat": "instance",
                 "gridPos": {"x": 0, "y": 0, "w": 8, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A"}]},
            ],
        }
        _, dash = self._translate(dashboard)
        leaves = list(self._walk_leaves(dash.get("panels") or []))
        titles = [p.get("title") for p in leaves]
        self.assertEqual(titles, ["host-1", "host-2"])

    def test_horizontal_repeat_lays_out_left_to_right(self):
        """``repeatDirection: h`` clones get laid out
        left-to-right wrapping at the Grafana 24-col width;
        downstream L1 scales them to the Kibana 48-col grid."""
        dashboard = {
            "title": "T", "uid": "rep-h-1", "schemaVersion": 39,
            "templating": {"list": [{
                "name": "instance", "type": "custom",
                "options": [
                    {"text": "a", "value": "a"},
                    {"text": "b", "value": "b"},
                    {"text": "c", "value": "c"},
                ],
            }]},
            "panels": [
                {"id": 1, "type": "stat", "title": "$instance",
                 "repeat": "instance",
                 "repeatDirection": "h",
                 "gridPos": {"x": 0, "y": 0, "w": 8, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A"}]},
            ],
        }
        _, dash = self._translate(dashboard)
        leaves = list(self._walk_leaves(dash.get("panels") or []))
        xs = sorted(p["position"]["x"] for p in leaves)
        # Three panels of w=8 (Grafana cols) -> w=16 (Kibana cols
        # after the 2x scale). Laid out horizontally they should
        # occupy x=0, 16, 32 in Kibana coordinates.
        self.assertEqual(xs, [0, 16, 32], f"horizontal lay-out fan-out, got x={xs}")


class L3RowAwareSectioningTests(unittest.TestCase):
    """L3 universal fix: every explicit Grafana ``type: row`` panel
    (modern schema) and every legacy ``rows[]`` entry (schemaVersion
    14) becomes a Kibana ``section`` in the emitted YAML, even when
    the source row has an empty/missing title.

    Before L3 the emitter only created a section when the row title
    was truthy; otherwise the panels were flattened into the top
    level with a ``_offset_yaml_panels`` y shift. That silently
    discarded the author's grouping intent for any dashboard that
    organises panels into untitled rows (a real-world quirk in
    auto-generated dashboards from Helm charts, Prometheus
    rule-driven dashboards, etc.).
    """

    def setUp(self):
        from observability_migration.adapters.source.grafana import (
            rules as rules_mod,
        )
        from observability_migration.adapters.source.grafana import (
            schema as schema_mod,
        )
        self.rule_pack = rules_mod.RulePackConfig()
        self.resolver = schema_mod.SchemaResolver(self.rule_pack)

    def _translate(self, dashboard):
        from observability_migration.adapters.source.grafana import (
            panels as panels_mod,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            _, yaml_path = panels_mod.translate_dashboard(
                dashboard,
                pathlib.Path(tmpdir),
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=self.rule_pack,
                resolver=self.resolver,
            )
            with open(yaml_path) as f:
                doc = yaml.safe_load(f)
        return doc["dashboards"][0]["panels"]

    def test_titled_row_becomes_section(self):
        """Baseline (was already working): a titled row produces a
        section entry with the row's title."""
        dashboard = {
            "title": "T", "uid": "titled-1", "schemaVersion": 39,
            "panels": [
                {"id": 1, "type": "row", "title": "Health",
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
                {"id": 2, "type": "stat", "title": "Up",
                 "gridPos": {"x": 0, "y": 1, "w": 12, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A"}]},
            ],
        }
        top = self._translate(dashboard)
        sections = [n for n in top if isinstance(n, dict) and "section" in n]
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["title"], "Health")
        self.assertEqual(len(sections[0]["section"]["panels"]), 1)

    def test_untitled_explicit_row_still_becomes_section(self):
        """Untitled row (``title: ""``) used to silently flatten its
        children into the top level. L3: it now produces a section
        with a fallback title so the grouping is preserved."""
        dashboard = {
            "title": "T", "uid": "untitled-row-1", "schemaVersion": 39,
            "panels": [
                {"id": 1, "type": "stat", "title": "Outside",
                 "gridPos": {"x": 0, "y": 0, "w": 12, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A"}]},
                {"id": 2, "type": "row", "title": "",
                 "gridPos": {"x": 0, "y": 4, "w": 24, "h": 1}},
                {"id": 3, "type": "stat", "title": "Inside",
                 "gridPos": {"x": 0, "y": 5, "w": 12, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A"}]},
            ],
        }
        top = self._translate(dashboard)
        sections = [n for n in top if isinstance(n, dict) and "section" in n]
        flat = [n for n in top if isinstance(n, dict) and "section" not in n]
        # "Outside" is before any row -> stays flat.
        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0]["title"], "Outside")
        # "Inside" was under the untitled row -> goes into a section
        # with a synthesised title (not the empty string).
        self.assertEqual(len(sections), 1)
        self.assertTrue(
            bool(sections[0].get("title", "").strip()),
            "Synthesised section title must not be empty",
        )
        self.assertEqual(len(sections[0]["section"]["panels"]), 1)
        self.assertEqual(sections[0]["section"]["panels"][0]["title"], "Inside")

    def test_collapsed_row_with_empty_title_becomes_section(self):
        """Collapsed rows with empty titles (children nested in
        ``panels[]``) follow the same rule."""
        dashboard = {
            "title": "T", "uid": "collapsed-1", "schemaVersion": 39,
            "panels": [
                {"id": 1, "type": "row", "title": "",
                 "collapsed": True,
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1},
                 "panels": [
                     {"id": 2, "type": "stat", "title": "Inner",
                      "gridPos": {"x": 0, "y": 1, "w": 12, "h": 4},
                      "datasource": {"type": "prometheus", "uid": "p"},
                      "targets": [{"expr": "up", "refId": "A"}]},
                 ]},
            ],
        }
        top = self._translate(dashboard)
        sections = [n for n in top if isinstance(n, dict) and "section" in n]
        self.assertEqual(len(sections), 1)
        self.assertTrue(bool(sections[0].get("title", "").strip()))
        # Issue #23: collapsed state must round-trip from Grafana to Kibana.
        self.assertIs(sections[0]["section"]["collapsed"], True)

    def test_issue23_modern_row_collapsed_true_emits_collapsed_section(self):
        """Modern (schemaVersion >= 14) ``type: row`` panel with
        ``collapsed: true`` — its nested children open as a closed
        section in Kibana, mirroring the source state."""
        dashboard = {
            "title": "T", "uid": "issue23-modern-true", "schemaVersion": 39,
            "panels": [
                {"id": 1, "type": "row", "title": "Closed in source",
                 "collapsed": True,
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1},
                 "panels": [
                     {"id": 2, "type": "stat", "title": "S",
                      "gridPos": {"x": 0, "y": 1, "w": 12, "h": 4},
                      "datasource": {"type": "prometheus", "uid": "p"},
                      "targets": [{"expr": "up", "refId": "A"}]},
                 ]},
            ],
        }
        top = self._translate(dashboard)
        sections = [n for n in top if isinstance(n, dict) and "section" in n]
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["title"], "Closed in source")
        self.assertIs(sections[0]["section"]["collapsed"], True)

    def test_issue23_modern_row_collapsed_false_emits_open_section(self):
        """Modern row with ``collapsed: false`` (the Grafana default for
        rows whose children sit beside them at top-level) — the section
        opens expanded."""
        dashboard = {
            "title": "T", "uid": "issue23-modern-false", "schemaVersion": 39,
            "panels": [
                {"id": 1, "type": "row", "title": "Open in source",
                 "collapsed": False,
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
                {"id": 2, "type": "stat", "title": "S",
                 "gridPos": {"x": 0, "y": 1, "w": 12, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A"}]},
            ],
        }
        top = self._translate(dashboard)
        sections = [n for n in top if isinstance(n, dict) and "section" in n]
        self.assertEqual(len(sections), 1)
        self.assertIs(sections[0]["section"]["collapsed"], False)

    def test_issue23_modern_row_without_collapsed_field_defaults_to_open(self):
        """Row with no ``collapsed`` key at all — defaults to open
        (matches Grafana's own default behaviour: ``collapsed`` is an
        explicit opt-in to the closed state)."""
        dashboard = {
            "title": "T", "uid": "issue23-modern-missing", "schemaVersion": 39,
            "panels": [
                {"id": 1, "type": "row", "title": "No flag",
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
                {"id": 2, "type": "stat", "title": "S",
                 "gridPos": {"x": 0, "y": 1, "w": 12, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A"}]},
            ],
        }
        top = self._translate(dashboard)
        sections = [n for n in top if isinstance(n, dict) and "section" in n]
        self.assertEqual(len(sections), 1)
        self.assertIs(sections[0]["section"]["collapsed"], False)

    def test_issue23_legacy_row_collapse_true_emits_collapsed_section(self):
        """Legacy (schemaVersion < 14) ``rows[]`` entries use the
        ``collapse`` (singular, no -d) field name — confirmed in the
        infra/grafana fixture prometheus-all.json. The translator must
        honour the legacy field too.

        Two-panel row used to bypass the existing single-panel-legacy-row
        flattening heuristic (``_normalize_panel_group`` force-flattens
        legacy rows with ≤ 1 panel as visual clutter).
        """
        dashboard = {
            "title": "T", "uid": "issue23-legacy-true", "schemaVersion": 14,
            "rows": [
                {"title": "Legacy closed",
                 "collapse": True,
                 "panels": [
                     {"id": 1, "type": "stat", "title": "S1", "span": 6,
                      "datasource": {"type": "prometheus", "uid": "p"},
                      "targets": [{"expr": "up", "refId": "A"}]},
                     {"id": 2, "type": "stat", "title": "S2", "span": 6,
                      "datasource": {"type": "prometheus", "uid": "p"},
                      "targets": [{"expr": "up", "refId": "B"}]},
                 ]},
            ],
        }
        top = self._translate(dashboard)
        sections = [n for n in top if isinstance(n, dict) and "section" in n]
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["title"], "Legacy closed")
        self.assertIs(sections[0]["section"]["collapsed"], True)

    def test_issue23_legacy_row_collapse_false_emits_open_section(self):
        """Legacy row with ``collapse: false`` — section opens
        expanded. Matches every row in prometheus-all.json that uses
        the default open state."""
        dashboard = {
            "title": "T", "uid": "issue23-legacy-false", "schemaVersion": 14,
            "rows": [
                {"title": "Legacy open",
                 "collapse": False,
                 "panels": [
                     {"id": 1, "type": "stat", "title": "S1", "span": 6,
                      "datasource": {"type": "prometheus", "uid": "p"},
                      "targets": [{"expr": "up", "refId": "A"}]},
                     {"id": 2, "type": "stat", "title": "S2", "span": 6,
                      "datasource": {"type": "prometheus", "uid": "p"},
                      "targets": [{"expr": "up", "refId": "B"}]},
                 ]},
            ],
        }
        top = self._translate(dashboard)
        sections = [n for n in top if isinstance(n, dict) and "section" in n]
        self.assertEqual(len(sections), 1)
        self.assertIs(sections[0]["section"]["collapsed"], False)

    def test_issue23_repeat_row_expansion_preserves_collapsed_flag(self):
        """``_expand_repeat_panels`` recurses into row containers via
        ``dict(panel)`` which preserves all keys including
        ``collapsed``. This test pins that invariant so future refactors
        don't accidentally drop the flag on the inner expansion path."""
        dashboard = {
            "title": "T", "uid": "issue23-repeat", "schemaVersion": 39,
            "templating": {"list": [
                {"name": "env", "type": "custom",
                 "options": [{"text": "p", "value": "p"}]},
            ]},
            "panels": [
                {"id": 1, "type": "row", "title": "Closed with repeat inside",
                 "collapsed": True,
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1},
                 "panels": [
                     {"id": 2, "type": "stat", "title": "Inner $env",
                      "repeat": "env",
                      "gridPos": {"x": 0, "y": 1, "w": 12, "h": 4},
                      "datasource": {"type": "prometheus", "uid": "p"},
                      "targets": [{"expr": "up", "refId": "A"}]},
                 ]},
            ],
        }
        top = self._translate(dashboard)
        sections = [n for n in top if isinstance(n, dict) and "section" in n]
        self.assertEqual(len(sections), 1)
        self.assertIs(sections[0]["section"]["collapsed"], True)

    def test_panels_before_any_row_stay_flat(self):
        """Panels that genuinely precede every row (the author chose
        to put them at the top of the dashboard, not in a row) stay
        as flat top-level panels. L3 only wraps panels that belong to
        an explicit row container."""
        dashboard = {
            "title": "T", "uid": "no-row-1", "schemaVersion": 39,
            "panels": [
                {"id": 1, "type": "stat", "title": "Header",
                 "gridPos": {"x": 0, "y": 0, "w": 24, "h": 4},
                 "datasource": {"type": "prometheus", "uid": "p"},
                 "targets": [{"expr": "up", "refId": "A"}]},
            ],
        }
        top = self._translate(dashboard)
        # No row -> no section
        sections = [n for n in top if isinstance(n, dict) and "section" in n]
        self.assertEqual(len(sections), 0)
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["title"], "Header")


class L2PerTypeMinimumsTests(unittest.TestCase):
    """L2 universal fix: every panel type gets per-type
    width/height minimums (and a max where one makes sense) enforced
    by :func:`_normalize_tile_size`, regardless of what the L1
    coordinate transform produced.

    The current floor of "metric width >= 4" and "datatable height >=
    5" is far too sparse: ``node-exporter-full`` has 11 metric tiles
    at h=3 (60px tall on Kibana's 20px row height — unreadable).
    """

    def _normalize(self, esql_type, w, h, **extra):
        from observability_migration.adapters.source.grafana.panels import (
            _normalize_tile_size,
        )
        panel = {
            "title": "T",
            "size": {"w": w, "h": h},
            "position": {"x": 0, "y": 0},
            **extra,
        }
        if esql_type == "markdown":
            panel["markdown"] = {"content": "x"}
        else:
            panel["esql"] = {"type": esql_type}
        _normalize_tile_size(panel, esql_type)
        return panel["size"]

    # --- metric ----------------------------------------------------

    def test_metric_h3_bumped_to_min_6(self):
        """The most common L2 defect: stat tiles with h=3 (60px)
        render unreadably small. Bump to the per-type min of 6."""
        self.assertEqual(self._normalize("metric", 4, 3)["h"], 6)

    def test_metric_h_above_min_is_unchanged(self):
        self.assertEqual(self._normalize("metric", 6, 8)["h"], 8)

    def test_metric_h_above_max_is_clamped(self):
        """Metrics don't benefit from going beyond ~12 rows tall;
        they show one value plus a sparkline at most."""
        self.assertEqual(self._normalize("metric", 6, 30)["h"], 12)

    def test_metric_w_below_min_is_bumped(self):
        """Pre-existing MIN_PANEL_WIDTH=4 enforcement is preserved."""
        self.assertEqual(self._normalize("metric", 2, 6)["w"], 4)

    # --- gauge -----------------------------------------------------

    def test_gauge_min_size(self):
        """Gauges need room for the dial; min_w=6, min_h=8."""
        size = self._normalize("gauge", 4, 4)
        self.assertEqual(size["w"], 6)
        self.assertEqual(size["h"], 8)

    def test_gauge_max_h(self):
        self.assertEqual(self._normalize("gauge", 12, 30)["h"], 16)

    # --- datatable -------------------------------------------------

    def test_datatable_min_w_bumped(self):
        """Datatables need at least ~12 cols to show columns."""
        self.assertEqual(self._normalize("datatable", 6, 10)["w"], 12)

    def test_datatable_min_h_bumped(self):
        self.assertEqual(self._normalize("datatable", 24, 4)["h"], 8)

    def test_datatable_max_h(self):
        self.assertEqual(self._normalize("datatable", 24, 30)["h"], 24)

    # --- bar / xy / line / area ------------------------------------

    def test_chart_types_min_h(self):
        """Charts (bar/xy/line/area) need at least h=6 to show
        their data clearly."""
        for t in ("bar", "line", "area", "xy"):
            with self.subTest(panel_type=t):
                self.assertEqual(self._normalize(t, 12, 4)["h"], 6, t)

    def test_chart_types_min_w(self):
        for t in ("bar", "line", "area", "xy"):
            with self.subTest(panel_type=t):
                self.assertEqual(self._normalize(t, 6, 12)["w"], 8, t)

    def test_chart_types_max_h(self):
        for t in ("bar", "line", "area", "xy"):
            with self.subTest(panel_type=t):
                self.assertEqual(self._normalize(t, 12, 30)["h"], 24, t)

    # --- markdown / text ------------------------------------------

    def test_markdown_min_size(self):
        size = self._normalize("markdown", 2, 1)
        self.assertEqual(size["w"], 4)
        self.assertEqual(size["h"], 2)

    def test_markdown_unbounded_max_h(self):
        """Long-form markdown can be tall by design; we don't clamp."""
        self.assertEqual(self._normalize("markdown", 48, 30)["h"], 30)

    # --- pie / heatmap / treemap ----------------------------------

    def test_pie_min_h(self):
        self.assertEqual(self._normalize("pie", 12, 4)["h"], 8)


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
        q = build_native_promql_query('node_filesystem_avail_bytes {instance="node-1"}', index="metrics-*")
        self.assertIn('node_filesystem_avail_bytes{instance="node-1"}', q)

    def test_native_promql_rejects_double_bracket_label_variable(self):
        self.assertFalse(panels.can_use_native_promql('rate(foo{instance=~"[[instance]]"}[5m])'))
        with self.assertRaises(ValueError):
            panels.build_native_promql_query('rate(foo{instance=~"[[instance]]"}[5m])', index="metrics-*")

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

    def test_topk_without_labels_uses_single_bucket_fallback(self):
        # Ungrouped topk now translates via single-bucket LIMIT fallback
        panel = self._make_panel("topk(5, http_requests_total)")
        _yaml_panel, result = self.translate_panel(panel)
        self.assertNotEqual(result.status, "not_feasible", result.reasons)

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
        # Both targets are gauges and now assume TSDS -> TS/TBUCKET. The test's intent is
        # group-label resolution: ``device`` is broken out, ``interface`` is not.
        self.assertIn("BY time_bucket = TBUCKET(5 minute), device", query)
        self.assertNotIn(", interface", query)
        self.assertEqual(result.query_ir["source_type"], "TS")
        self.assertEqual(result.target_query_contract["canonical_target"], "promql")
        # TS is the time-series-faithful source the PromQL contract wants, so the gauge
        # aggregation is now contract-exact rather than a forced FROM degradation.
        self.assertEqual(result.contract_evaluation["status"], "exact_now")
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
        expr = 'node_filesystem_avail_bytes {instance="node-1"}'
        panel = self._make_panel(expr)
        _, result = self.translate_panel(panel)
        self.assertEqual(
            result.query_ir.get("clean_expression"),
            'node_filesystem_avail_bytes{instance="node-1"}',
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
                # Ungrouped topk now translates via single-bucket fallback
                self.assertNotEqual(
                    topk_panels[0].status, "skipped",
                    "topk panel should not be skipped",
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


class TestMigrationResultTranslationError(unittest.TestCase):
    """MigrationResult must carry a translation_error field (issue #37)."""

    def test_translation_error_defaults_to_empty_string(self):
        result = migrate.MigrationResult(
            dashboard_title="test",
            dashboard_uid="abc",
        )
        self.assertEqual(result.translation_error, "")

    def test_translation_error_survives_round_trip_in_report(self):
        import json
        import os
        import tempfile
        result = migrate.MigrationResult(
            dashboard_title="broken",
            dashboard_uid="xyz",
            translation_error="Traceback (most recent call last):\n  TypeError: boom",
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        self.addCleanup(os.unlink, path)
        report.save_detailed_report([result], [], path)
        with open(path) as f:
            data = json.loads(f.read())
        dashboard_entry = data["dashboards"][0]
        self.assertEqual(dashboard_entry["translation_error"],
                         "Traceback (most recent call last):\n  TypeError: boom")


class TestEsqlFieldEscaping(unittest.TestCase):
    """_esql_field must backtick-quote field names with special characters."""

    def test_plain_field_unchanged(self):
        self.assertEqual(migrate._esql_field("container_memory_working_set_bytes"), "container_memory_working_set_bytes")

    def test_dotted_field_unchanged(self):
        self.assertEqual(migrate._esql_field("prometheus.metrics.value"), "prometheus.metrics.value")

    def test_recording_rule_metric_with_colons_is_quoted(self):
        raw = "prometheus.node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate.value"
        result = migrate._esql_field(raw)
        self.assertTrue(result.startswith("`") and result.endswith("`"), f"Expected backtick-quoted, got: {result}")
        self.assertIn(raw, result)

    def test_field_with_hyphen_is_quoted(self):
        result = migrate._esql_field("my-field.value")
        self.assertTrue(result.startswith("`") and result.endswith("`"), f"Expected backtick-quoted, got: {result}")

    def test_empty_string_unchanged(self):
        self.assertEqual(migrate._esql_field(""), "")

    def test_none_returns_none(self):
        self.assertIsNone(migrate._esql_field(None))

    def test_recording_rule_esql_does_not_contain_bare_colon_in_stats(self):
        """Integration: recording-rule metric must be backtick-quoted in the generated ES|QL."""
        rp = migrate.RulePackConfig()
        resolver = migrate.SchemaResolver(rp)
        # Seed an empty field cache so the resolver is "online" but has no profile
        # → resolve_metric_field returns the metric name unchanged → _esql_field quotes it.
        resolver._discovery_attempted = True
        resolver._field_cache = {}
        resolver._discovered_mappings = {}
        metric = "node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate"
        result = migrate.translate_promql_to_esql(
            f"sum({metric}{{namespace='default'}})",
            esql_index="metrics-*",
            panel_type="graph",
            rule_pack=rp,
            resolver=resolver,
        )
        # The STATS clause must backtick-quote the colon-bearing field name
        self.assertIn(f"`{metric}`", result.esql_query, f"Expected backtick-quoted field in: {result.esql_query}")
        # No bare colon-bearing field outside backticks
        import re
        bare = re.search(rf"(?<!`)\b{re.escape(metric)}\b", result.esql_query)
        self.assertIsNone(bare, f"Bare colon field found in: {result.esql_query}")


class TestMetricsPathLabelIgnored(unittest.TestCase):
    """metrics_path Prometheus scrape label must be dropped, not emitted as a WHERE filter."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def _translate(self, expr):
        return migrate.translate_promql_to_esql(
            expr,
            esql_index="metrics-*",
            panel_type="graph",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    def test_metrics_path_in_ignored_labels_by_default(self):
        self.assertIn("metrics_path", self.rule_pack.ignored_labels)
        self.assertIn("__metrics_path__", self.rule_pack.ignored_labels)

    def test_origin_prometheus_still_ignored(self):
        self.assertIn("origin_prometheus", self.rule_pack.ignored_labels)

    def test_metrics_path_filter_not_emitted(self):
        result = self._translate(
            'sum(rate(container_network_receive_bytes_total{job="kubelet",metrics_path="/metrics/cadvisor"}[5m]))'
        )
        self.assertNotIn("metrics_path", result.esql_query, f"metrics_path leaked into ES|QL: {result.esql_query}")

    def test_double_underscore_metrics_path_filter_not_emitted(self):
        result = self._translate(
            'sum(rate(container_network_receive_bytes_total{job="kubelet",__metrics_path__="/metrics/cadvisor"}[5m]))'
        )
        self.assertNotIn("__metrics_path__", result.esql_query, f"__metrics_path__ leaked into ES|QL: {result.esql_query}")

    def test_legitimate_label_job_is_kept(self):
        result = self._translate(
            'sum(rate(container_network_receive_bytes_total{job="kubelet",metrics_path="/metrics/cadvisor"}[5m]))'
        )
        self.assertIn("kubelet", result.esql_query, f"Expected job filter to remain, got: {result.esql_query}")


class TestJoinStrippingEnablesMultiTargetFusion(unittest.TestCase):
    """group_left join-wrapped aggregations must not silently drop join semantics.

    PromQL expressions like sum(rate(A) * on(ns,pod) group_left(w,wt) B) by (pod)
    use the RHS both for label enrichment and filtering. Stripping that RHS can
    keep a query renderable but changes numeric values or selected series.
    """

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    _JOIN_TMPL = (
        'sum(rate({metric}{{job="kubelet"}}[5m])'
        " * on(namespace,pod) group_left(workload,workload_type)"
        " namespace_workload_pod:kube_pod_owner:relabel{{workload='myapp'}}) by (pod)"
    )

    def _translate(self, expr, panel_type="graph"):
        return migrate.translate_promql_to_esql(
            expr,
            esql_index="metrics-*",
            panel_type=panel_type,
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    def test_join_wrapped_rate_translates_to_valid_esql(self):
        """A single join-wrapped rate target must not silently drop the RHS."""
        expr = self._JOIN_TMPL.format(metric="container_network_receive_bytes_total")
        result = self._translate(expr)
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertTrue(any("vector-matching join" in w for w in result.warnings), result.warnings)

    def test_six_network_targets_fuse_into_single_query(self):
        """Current Network Usage: 6 join-wrapped network targets must fuse into a single multi-STATS query."""
        metrics = [
            "container_network_receive_bytes_total",
            "container_network_transmit_bytes_total",
            "container_network_receive_packets_total",
            "container_network_transmit_packets_total",
            "container_network_receive_packets_dropped_total",
            "container_network_transmit_packets_dropped_total",
        ]
        panel = {
            "title": "Current Network Usage",
            "type": "table",
            "targets": [
                {"expr": self._JOIN_TMPL.format(metric=m), "refId": chr(65 + i)}
                for i, m in enumerate(metrics)
            ],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {},
        }
        yaml_panel, result = migrate.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertNotIn("esql", yaml_panel)
        self.assertEqual(result.status, "not_feasible")
        self.assertTrue(any("vector-matching join" in reason for reason in result.reasons), result.reasons)

    def test_ratio_of_join_wrapped_targets_produces_eval(self):
        """CPU usage / CPU requests ratio must translate to a STATS + EVAL expression."""
        ratio_expr = (
            "sum("
            "  node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate{namespace='default'}"
            "  * on(namespace,pod) group_left(workload,workload_type)"
            "  namespace_workload_pod:kube_pod_owner:relabel{workload='myapp'}"
            ") by (pod)"
            " / "
            "sum("
            "  kube_pod_container_resource_requests{job='kube-state-metrics',namespace='default',resource='cpu'}"
            "  * on(namespace,pod) group_left(workload,workload_type)"
            "  namespace_workload_pod:kube_pod_owner:relabel{workload='myapp'}"
            ") by (pod)"
        )
        result = self._translate(ratio_expr)
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertTrue(any("vector-matching join" in w for w in result.warnings), result.warnings)


class TestImageLabelOtelMapping(unittest.TestCase):
    """The Prometheus 'image' label must map to OTel 'container.image.name'."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def test_image_label_in_otel_candidates(self):
        self.assertIn("image", migrate.SchemaResolver.PROM_TO_OTEL_CANDIDATES)
        self.assertIn("container.image.name", migrate.SchemaResolver.PROM_TO_OTEL_CANDIDATES["image"])

    def test_image_filter_maps_to_otel_field_with_schema_discovery(self):
        """With field discovery, image!="" must become WHERE container.image.name != ""."""
        self.resolver._discovery_attempted = True
        self.resolver._field_cache = {
            "container.image.name": {"keyword": {"aggregatable": True, "searchable": True}},
            "prometheus.labels.namespace": {"keyword": {"aggregatable": True, "searchable": True}},
        }
        self.resolver._build_discovered_mappings()
        result = migrate.translate_promql_to_esql(
            'sum(container_memory_working_set_bytes{container!="",image!=""}) by (pod)',
            esql_index="metrics-*",
            panel_type="graph",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )
        self.assertIn("container.image.name", result.esql_query, f"Expected OTel field, got:\n{result.esql_query}")
        self.assertNotIn('image != ""', result.esql_query, f"Raw 'image' label leaked into:\n{result.esql_query}")


class TestBareJoinStrippingEnablesMultiTargetFusion(unittest.TestCase):
    """family='join' (bare A * on(x) group_left(y) B without outer aggregation)
    must participate in multi-target fusion.

    Unlike the family='unknown' case (outer agg wrapping a join), bare joins
    land as family='join' in the fragment parser.  _build_formula_plan must
    strip the join RHS and delegate to the left_frag so that:
      1. A single target translates to a valid query using the primary metric
      2. Two bare-join targets fuse into a single multi-STATS query
      3. join_labels (e.g. chip_name) are preserved as group fields
    """

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def _panel(self, targets):
        return {
            "title": "Bare Join Multi-Target",
            "type": "timeseries",
            "targets": [{"expr": expr, "refId": chr(65 + i)} for i, expr in enumerate(targets)],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {},
        }

    def test_single_bare_join_translates(self):
        """A bare join target must produce a valid FROM query with the primary metric."""
        panel = self._panel([
            'node_hwmon_temp_celsius{instance="host"} * on(chip) group_left(chip_name) node_hwmon_chip_names{instance="host"}',
        ])
        yaml_panel, result = migrate.translate_panel(
            panel, datasource_index="metrics-*", esql_index="metrics-*",
            rule_pack=self.rule_pack, resolver=self.resolver,
        )
        query = yaml_panel.get("esql", {}).get("query", "")
        self.assertIn("node_hwmon_temp_celsius", query)
        self.assertNotIn("node_hwmon_chip_names", query)
        self.assertIn("Dropped group_left label enrichment", str(result.reasons))

    def test_two_bare_join_targets_fuse_into_single_query(self):
        """Two bare-join targets with the same join structure must fuse."""
        panel = self._panel([
            'node_hwmon_temp_celsius{instance="host"} * on(chip) group_left(chip_name) node_hwmon_chip_names{instance="host"}',
            'node_hwmon_temp_crit_celsius{instance="host"} * on(chip) group_left(chip_name) node_hwmon_chip_names{instance="host"}',
        ])
        yaml_panel, result = migrate.translate_panel(
            panel, datasource_index="metrics-*", esql_index="metrics-*",
            rule_pack=self.rule_pack, resolver=self.resolver,
        )
        esql_metrics = [m["field"] for m in yaml_panel.get("esql", {}).get("metrics", [])]
        self.assertEqual(len(esql_metrics), 2, f"Expected 2 fused metrics, got {esql_metrics}")
        query = yaml_panel["esql"]["query"]
        self.assertIn("node_hwmon_temp_celsius", query)
        self.assertIn("node_hwmon_temp_crit_celsius", query)
        stats_lines = [ln for ln in query.splitlines() if ln.strip().startswith("| STATS")]
        self.assertEqual(len(stats_lines), 1, f"Expected single STATS, got {stats_lines}")
        self.assertNotIn("Merged compatible panel targets", str(result.reasons))

    def test_join_on_labels_preserved_as_group_fields(self):
        """The on() matching labels (e.g. chip) must appear in the BY clause.

        frag.extra['join_labels'] stores the on() matching labels, not the
        group_left() carry labels.  chip_name appears in the BY clause only
        when the panel legendFormat drives it via preferred_group_labels; chip
        (the on-matching label) is what the fusion pipeline inherits.
        """
        panel = {
            "title": "Bare Join Multi-Target",
            "type": "timeseries",
            "targets": [
                {"expr": 'node_hwmon_temp_celsius * on(chip) group_left(chip_name) node_hwmon_chip_names',
                 "refId": "A", "legendFormat": "{{chip_name}} temp"},
                {"expr": 'node_hwmon_temp_crit_celsius * on(chip) group_left(chip_name) node_hwmon_chip_names',
                 "refId": "C", "legendFormat": "{{chip_name}} crit"},
            ],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "options": {},
        }
        yaml_panel, _ = migrate.translate_panel(
            panel, datasource_index="metrics-*", esql_index="metrics-*",
            rule_pack=self.rule_pack, resolver=self.resolver,
        )
        query = yaml_panel.get("esql", {}).get("query", "")
        # preferred_group_labels drives chip_name into the BY clause via legendFormat
        self.assertIn("chip_name", query, f"chip_name missing from BY clause:\n{query}")

    def test_join_slash_ratio_not_affected(self):
        """family='join' with binary_op='/' (ratio join) is NOT stripped — it has
        its own dedicated handler in translate.py and needs special treatment."""
        expr = (
            "sum by(instance)(irate(node_cpu_guest_seconds_total{mode='user'}[1m]))"
            " / on(instance) group_left"
            " sum by(instance)(irate(node_cpu_seconds_total[1m]))"
        )
        result = migrate.translate_promql_to_esql(
            expr, esql_index="metrics-*", panel_type="graph",
            rule_pack=self.rule_pack, resolver=self.resolver,
        )
        self.assertNotEqual(result.feasibility, "not_feasible",
                            f"Join ratio should still translate, got: {result.esql_query}")


class TestMatcherAliasSuffixVariableFilters(unittest.TestCase):
    """_matcher_alias_suffix must distinguish operands of a ratio expression
    that share variable-driven matchers (=~".*" / label_*) but differ only
    in a static filter (e.g. status!~"[4-5].*")."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def test_nginx_success_rate_is_feasible(self):
        """NGINX Controller Success Rate ratio must translate to a single STATS query."""
        expr = (
            'sum(rate(nginx_ingress_controller_requests{'
            'controller_pod=~"$controller",controller_class=~"$controller_class",'
            'controller_namespace=~"$namespace",status!~"[4-5].*"}'
            '[$__rate_interval])) by (controller, controller_namespace)'
            ' / sum(rate(nginx_ingress_controller_requests{'
            'controller_pod=~"$controller",controller_class=~"$controller_class",'
            'controller_namespace=~"$namespace"}'
            '[$__rate_interval])) by (controller, controller_namespace)'
        )
        result = migrate.translate_promql_to_esql(
            expr, esql_index="metrics-*", panel_type="timeseries",
            rule_pack=self.rule_pack, resolver=self.resolver,
        )
        self.assertNotEqual(
            result.feasibility, "not_feasible",
            f"NGINX success rate should be feasible; got warnings={result.warnings}",
        )
        query = result.esql_query or ""
        stats_lines = [ln for ln in query.splitlines() if ln.strip().startswith("| STATS")]
        self.assertEqual(len(stats_lines), 1, f"Expected single STATS; got:\n{query}")
        # Both operands should appear in the STATS line
        self.assertIn("CASE(", query, f"Expected CASE-wrapped filter in STATS:\n{query}")

    def test_static_filter_only_operand_aliases_differ(self):
        """Two same-metric ratio operands that share only variable-driven matchers
        must produce distinct aliases so _build_shared_measure_pipeline can merge them."""
        from observability_migration.adapters.source.grafana.promql import (
            _matcher_alias_suffix,
            _parse_fragment,
            preprocess_grafana_macros,
        )
        expr = (
            'sum(rate(http_requests_total{namespace=~"$ns",status!~"5.*"}[5m])) by (job)'
            ' / sum(rate(http_requests_total{namespace=~"$ns"}[5m])) by (job)'
        )
        preprocessed = preprocess_grafana_macros(expr, self.rule_pack)
        frag = _parse_fragment(preprocessed)
        left_frag = frag.extra.get("left_frag")
        right_frag = frag.extra.get("right_frag")
        left_suffix = _matcher_alias_suffix(left_frag)
        right_suffix = _matcher_alias_suffix(right_frag)
        self.assertNotEqual(
            left_suffix, right_suffix,
            f"Operand alias suffixes must differ; both are {left_suffix!r}",
        )
        # Left operand has the static status filter — it should appear in its suffix
        self.assertIn("status", left_suffix, f"Left suffix should contain 'status': {left_suffix!r}")

    def test_variable_matcher_alias_suffix_uses_label_not_internal_param(self):
        from observability_migration.adapters.source.grafana.promql import (
            _matcher_alias_suffix,
            _parse_fragment,
            preprocess_grafana_macros,
        )

        frag = _parse_fragment(
            preprocess_grafana_macros(
                'rate(nginx_ingress_controller_requests{controller_pod=~"$controller"}[5m])',
                self.rule_pack,
            )
        )

        self.assertEqual(_matcher_alias_suffix(frag), "controller_pod_rate")

    def test_metric_kind_override_drives_non_suffix_counter_rate(self):
        self.rule_pack.metric_kinds["nginx_ingress_controller_requests"] = "counter"
        result = migrate.translate_promql_to_esql(
            'sum(rate(nginx_ingress_controller_requests{controller_pod=~"$controller"}[5m])) by (controller)',
            esql_index="metrics-*",
            panel_type="timeseries",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

        self.assertNotIn("AVG_OVER_TIME(nginx_ingress_controller_requests, 5m)", result.esql_query)
        self.assertIn("RATE(nginx_ingress_controller_requests, 5m)", result.esql_query)


class TestScalarAggregationHoisting(unittest.TestCase):
    """agg(X op k) where k is a scalar literal must translate by hoisting
    the constant out: agg(X op k) → agg(X) op k.  The not_feasible path
    must only fire for true two-vector operands."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def _translate(self, expr):
        return migrate.translate_promql_to_esql(
            expr, esql_index="metrics-*", rule_pack=self.rule_pack, resolver=self.resolver,
        )

    def test_avg_over_time_times_100(self):
        """avg(avg_over_time(up) * 100) must translate, not be not_feasible."""
        r = self._translate('avg(avg_over_time(up{job=~"$job"}[$interval]) * 100)')
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertIn("* 100", r.esql_query or "", f"scalar not in EVAL:\n{r.esql_query}")

    def test_max_rate_times_8(self):
        """max(rate(A[t]) * 8) — bytes→bits conversion — must translate."""
        r = self._translate(
            'max(rate(node_network_receive_bytes_total{job=~"$job"}[$interval])*8) by (instance)'
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertIn("* 8", r.esql_query or "", f"scalar not in EVAL:\n{r.esql_query}")
        stats_lines = [ln for ln in (r.esql_query or "").splitlines() if "| STATS" in ln]
        self.assertEqual(len(stats_lines), 1, f"Expected single STATS:\n{r.esql_query}")

    def test_sum_rate_divided_by_scalar(self):
        """sum(rate(A[5m]) / 1000) — unit conversion — must translate."""
        r = self._translate('sum(rate(http_requests_total[5m]) / 1000) by (job)')
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertIn("/ 1000", r.esql_query or "", f"scalar not in EVAL:\n{r.esql_query}")

    def test_scalar_on_left_commutes(self):
        """sum(8 * rate(A[5m])) — scalar on left — must translate."""
        r = self._translate('sum(8 * rate(http_requests_total[5m])) by (job)')
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")

    def test_true_two_series_still_not_feasible(self):
        """max(A / B) with two distinct metrics must remain not_feasible."""
        r = self._translate(
            'max(node_filesystem_size_bytes / node_filesystem_avail_bytes)'
        )
        self.assertEqual(
            r.feasibility, "not_feasible",
            f"Two-series ratio should stay not_feasible; got:\n{r.esql_query}",
        )


class TestUnaryMinusOverBinaryExpr(unittest.TestCase):
    """-(A op B) must translate correctly, not fail with 'Could not extract metric name'.

    Root cause: _copy_fragment_summary does not copy left_frag/right_frag from
    the child's extra dict, so wrapping a binary_expr in UnaryExpr lost the
    sub-fragment structure entirely.  Fix rewrites -(A op B) as 0 - (A op B)
    so _make_binary_fragment preserves the full structure."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def _translate(self, expr):
        return migrate.translate_promql_to_esql(
            expr, esql_index="metrics-*", rule_pack=self.rule_pack, resolver=self.resolver,
        )

    def test_negate_sum_of_irates(self):
        """-(irate(A) + irate(B)) — butterfly-chart negation — must translate."""
        r = self._translate(
            "-(irate(node_network_transmit_errs_total[5m])"
            " + irate(node_network_transmit_drop_total[5m]))"
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")

    def test_negate_sum_of_rates(self):
        """-(rate(A) + rate(B)) — alternate form — must translate."""
        r = self._translate(
            "-(rate(http_requests_bytes_total[5m]) + rate(http_other_bytes_total[5m]))"
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")

    def test_negate_sum_with_comparison(self):
        """-(irate(A) + irate(B)) < 0 — negation + filter — must translate."""
        r = self._translate(
            "-(irate(node_network_transmit_errs_total[5m])"
            " + irate(node_network_transmit_drop_total[5m])) < 0"
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")

    def test_negate_single_metric_still_works(self):
        """-(irate(A)) — single-metric unary minus — must still translate."""
        r = self._translate("-(irate(node_disk_written_bytes_total[5m]))")
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")

    def test_negate_grouped_rate_preserves_sign(self):
        """- sum(rate(A)) by (label) must preserve the negative sign."""
        r = self._translate(
            '- sum(rate(node_network_transmit_bytes_total{device!~"(veth|azv|lxc).*"}[5m])) by (device)'
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        q = r.esql_query or ""
        self.assertIn("| EVAL computed_value = (0 - node_network_transmit_bytes_total", q)
        self.assertIn("| KEEP time_bucket, device, computed_value", q)

    def test_negate_scalar_still_works(self):
        """Unary minus on scalar literal must produce negative scalar."""
        r = self._translate("sum(-1 * rate(http_requests_total[5m])) by (job)")
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")


class TestBinaryExprJoinLHS(unittest.TestCase):
    """(A op B) * ON(x) GROUP_LEFT(y) C must translate, not fail 'Could not extract metric name'.

    Root cause: join_family_rule's binary_op=='*' branch used
    ``left_frag.metric or frag.metric`` which is empty when the LHS is itself a
    binary_expr.  Fix delegates to _build_formula_plan so the arithmetic is
    handled correctly and the join RHS (label enrichment) is still stripped."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def _translate(self, expr):
        return migrate.translate_promql_to_esql(
            expr, esql_index="metrics-*", rule_pack=self.rule_pack, resolver=self.resolver,
        )

    def test_difference_times_label_join(self):
        """(A - B) * ON(instance) GROUP_LEFT(nodename) C — memory used * uname."""
        r = self._translate(
            "(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes)"
            " * ON(instance) GROUP_LEFT(nodename) node_uname_info"
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertIn("Dropped group_left label enrichment", " ".join(r.warnings))
        self.assertIn("computed_value", r.esql_query or "")

    def test_filesystem_used_times_error_flag(self):
        """(size - avail) * on(...) group_left() error_flag — filesystem used bytes."""
        r = self._translate(
            "(node_filesystem_size_bytes - node_filesystem_avail_bytes)"
            " * on(instance, device, mountpoint, fstype) group_left()"
            " node_filesystem_device_error"
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")

    def test_simple_join_lhs_still_works(self):
        """A * ON(instance) GROUP_LEFT(nodename) B — simple single-metric LHS unchanged."""
        r = self._translate(
            "node_hwmon_temp_celsius * ON(chip) GROUP_LEFT(chip_name) node_hwmon_chip_names"
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertIn("Dropped group_left label enrichment", " ".join(r.warnings))


class TestPhantomGrafanaVarStripping(unittest.TestCase):
    """rate(A) * $trends must translate, not fail with 'divergent filters'.

    Root cause: preprocess_grafana_macros converts bare $var tokens to
    label_var, so ``$trends`` becomes a simple_metric named ``label_trends``.
    The formula plan sees two different series (TS rate + FROM gauge) which
    can't be merged.  Fix detects bare label_* simple_metrics with no matchers
    and strips them from multiplicative binary ops."""

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def _translate(self, expr):
        return migrate.translate_promql_to_esql(
            expr, esql_index="metrics-*", rule_pack=self.rule_pack, resolver=self.resolver,
        )

    def test_rate_times_dollar_trends(self):
        """rate(A) * $trends — ClickHouse-style trend toggle must translate."""
        r = self._translate(
            'rate(ClickHouseProfileEvents_ReadBackoff{instance=~"$instance"}[5m]) * $trends'
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertTrue(
            any("trends" in w and "dropped" in w.lower() for w in r.warnings),
            f"Expected $trends-dropped warning, got {r.warnings}",
        )

    def test_dollar_trends_times_rate_commutative(self):
        """$trends * rate(A) — commutative form also works."""
        r = self._translate(
            '$trends * rate(ClickHouseProfileEvents_Query{instance=~"$instance"}[5m])'
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")

    def test_normal_scalar_multiplication_unaffected(self):
        """rate(A) * 8 — real numeric scalar still uses scalar hoisting, not phantom strip."""
        r = self._translate("rate(node_network_receive_bytes_total[5m]) * 8")
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertFalse(
            any("dropped" in w.lower() and "variable" in w.lower() for w in r.warnings),
            f"Scalar * 8 should not trigger phantom-var warning: {r.warnings}",
        )

    def test_real_metric_ratio_unaffected(self):
        """sum(rate(A)) / sum(rate(A)) — same-metric ratio must still merge."""
        r = self._translate(
            "sum(rate(http_requests_total[5m])) / sum(rate(http_requests_total[5m]))"
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")


class TestJoinAggScalarDiv(unittest.TestCase):
    """sum(A * group_right B / k) pattern — Podman container memory dashboards.

    Aggregating over the result of a vector-matching multiplication is not
    linear. Stripping the join RHS would keep a query shape but produce wrong
    values, so the migration must require manual redesign.
    """

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def _translate(self, expr):
        return migrate.translate_promql_to_esql(
            expr, esql_index="metrics-*", rule_pack=self.rule_pack, resolver=self.resolver,
        )

    def test_sum_join_div_1024_twice(self):
        """sum by(name)(A * group_right B / 1024 / 1024) — two scalar divisions."""
        r = self._translate(
            "sum by(name)(podman_container_info"
            " * on(id) group_right(name) podman_container_memory_bytes"
            " / 1024 / 1024)"
        )
        self.assertEqual(r.feasibility, "not_feasible")
        self.assertTrue(any("vector-matching join" in w for w in r.warnings), r.warnings)

    def test_sum_join_div_1024_once(self):
        """sum(A * group_right B / 1024) — single scalar division."""
        r = self._translate(
            "sum(container_info * on(id) group_right(name) memory_bytes / 1024)"
        )
        self.assertEqual(r.feasibility, "not_feasible")
        self.assertTrue(any("vector-matching join" in w for w in r.warnings), r.warnings)

    def test_max_join_times_rate_div_scalar(self):
        """max(rate(A) * group_left(label) B / 8) — group_left variant."""
        r = self._translate(
            "max(rate(node_network_receive_bytes_total[5m])"
            " * on(instance) group_left(nodename) node_uname_info / 8)"
        )
        self.assertEqual(r.feasibility, "not_feasible")
        self.assertTrue(any("vector-matching join" in w for w in r.warnings), r.warnings)

    def test_sum_over_two_series_still_not_feasible(self):
        """sum(A / B) with two real series must remain not_feasible."""
        r = self._translate(
            "sum(node_filesystem_avail_bytes / node_filesystem_size_bytes)"
        )
        self.assertEqual(
            r.feasibility,
            "not_feasible",
            "per-element division between two real series should stay not_feasible",
        )


class TestAnchoredVariableMatcherQuality(unittest.TestCase):
    """Correctness fixes A & C: anchored-variable matchers and real regex anchors.

    Bug A: namespace=~"^$Namespace$" preprocesses to "^label_Namespace$";
    the leading "^" previously bypassed startswith("label_") and leaked into
    RLIKE as WHERE namespace RLIKE "^label_Namespace$".

    Bug C: status!~".*cam(era)?$" — trailing "$" is a PromQL regex
    end-anchor. ES|QL RLIKE treats it as a literal, so it must be stripped.
    """

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def _translate(self, expr):
        return migrate.translate_promql_to_esql(
            expr, esql_index="metrics-*", rule_pack=self.rule_pack, resolver=self.resolver,
        )

    def test_anchored_var_matcher_becomes_param_filter(self):
        """^$Namespace$ strips anchors and is dropped with a clear warning."""
        r = self._translate(
            'kube_pod_status_phase{namespace=~"^$Namespace$",phase="Running"} > 0'
        )
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertNotIn(
            "label_Namespace",
            r.esql_query or "",
            "preprocessed variable label should not appear in WHERE RLIKE clause",
        )
        self.assertNotIn("?Namespace", r.esql_query or "")
        self.assertIn("Dropped variable-driven label filters during migration", r.warnings)

    def test_real_regex_end_anchor_stripped_for_esql(self):
        """status!~".*cam(era)?$" — end-anchor must not become literal $."""
        r = self._translate('http_requests_total{service="web",status!~".*cam(era)?$"}')
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertIn(
            'RLIKE ".*cam(era)?"',
            r.esql_query or "",
            "PromQL regex end-anchor should be stripped for ES|QL RLIKE",
        )
        self.assertNotIn('RLIKE ".*cam(era)?$"', r.esql_query or "")

    def test_dollar_end_anchor_without_word_char_not_a_var(self):
        """Regex "end$" — bare end-anchor is stripped, not treated as a var."""
        r = self._translate('http_requests_total{status!~".*end$"}')
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertIn('RLIKE ".*end"', r.esql_query or "")
        self.assertNotIn('RLIKE ".*end$"', r.esql_query or "")


class TestGroupByVarLabelDropped(unittest.TestCase):
    """Correctness fix B: label_* entries from by($Var) must not appear in BY.

    Grafana template variables in by() clauses (e.g. by (namespace, $Env))
    are preprocessed to label_Env.  These phantom labels must be silently
    dropped from the STATS BY clause to avoid non-existent field references.
    """

    def setUp(self):
        self.rule_pack = migrate.RulePackConfig()
        self.resolver = migrate.SchemaResolver(self.rule_pack)

    def _translate(self, expr):
        return migrate.translate_promql_to_esql(
            expr, esql_index="metrics-*", rule_pack=self.rule_pack, resolver=self.resolver,
        )

    def test_preprocessed_label_var_not_in_by(self):
        """sum(M) by (namespace, label_Env) — label_Env dropped from STATS BY."""
        r = self._translate("sum(kube_pod_info) by (namespace, label_Env)")
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertNotIn(
            "label_Env",
            r.esql_query or "",
            "preprocessed Grafana variable must not appear in BY clause",
        )
        self.assertIn(
            "k8s.namespace.name",
            r.esql_query or "",
            "real namespace label should still be resolved and present",
        )

    def test_real_label_not_affected(self):
        """sum(M) by (namespace, pod) — real labels must not be dropped."""
        r = self._translate("sum(kube_pod_info) by (namespace, pod)")
        self.assertNotEqual(r.feasibility, "not_feasible", f"warnings={r.warnings}")
        self.assertIn("k8s.namespace.name", r.esql_query or "")


class TestLogQLVariablePrefixDropped(unittest.TestCase):
    """Correctness fix D: (?i)$var in LogQL search filters.

    _build_log_message_filter must strip leading inline regex flags (like
    (?i)) before checking for variable references, so that
    "(?i)label_searchable_pattern" is dropped rather than emitted as
    RLIKE ".*(?i)label_searchable_pattern.*".
    """

    def setUp(self):
        from observability_migration.adapters.source.grafana.promql import (
            _build_log_message_filter,
        )
        self.rule_pack = migrate.RulePackConfig()
        self._fn = lambda s: _build_log_message_filter(s, self.rule_pack)

    def test_case_insensitive_label_var_dropped(self):
        """(?i)label_pattern — inline flag + preprocessed var → None."""
        self.assertIsNone(self._fn("(?i)label_searchable_pattern"))

    def test_case_insensitive_dollar_var_dropped(self):
        """(?i)$pattern — inline flag + raw Grafana var → None."""
        self.assertIsNone(self._fn("(?i)$searchable_pattern"))

    def test_bare_label_var_dropped(self):
        """label_foo (no flag) — preprocessed var → None."""
        self.assertIsNone(self._fn("label_foo"))

    def test_case_insensitive_literal_kept(self):
        """(?i)error — real literal with flag → non-None RLIKE filter."""
        result = self._fn("(?i)error")
        self.assertIsNotNone(result)
        self.assertIn("error", result)

    def test_bare_literal_kept(self):
        """error — plain literal → LIKE filter."""
        result = self._fn("error")
        self.assertIsNotNone(result)
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
