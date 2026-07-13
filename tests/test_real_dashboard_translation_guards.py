# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Real-dashboard translation guards that run in the regular unit suite.

These are deliberately broader than single-query unit tests: they translate
popular checked-in Grafana and Datadog dashboard fixtures and assert invariants
that should hold for every emitted query/control pair.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import generate_dashboard_yaml
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.adapters.source.grafana.panels import translate_dashboard
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.runtime_features import (
    ESQL_NAMED_PARAM_BINDING,
    set_runtime_feature,
)
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import translate_promql_to_esql

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAFANA_DASHBOARDS = REPO_ROOT / "infra" / "grafana" / "dashboards"
DATADOG_DASHBOARDS = REPO_ROOT / "infra" / "datadog" / "dashboards"
TIME_PARAMS = {"_tstart", "_tend"}
GRAFANA_TEMPLATE_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[^}]+\}|\[\[[^\]]+\]\])")
ESQL_PARAM_RE = re.compile(r"\?([A-Za-z_][A-Za-z0-9_]*)")


def _leaf_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []
    stack = list(panels or [])
    while stack:
        panel = stack.pop(0)
        section = panel.get("section")
        if isinstance(section, dict):
            stack = list(section.get("panels") or []) + stack
            continue
        leaves.append(panel)
    return leaves


def _esql_panels(yaml_doc: dict[str, Any]) -> list[dict[str, Any]]:
    dashboard = (yaml_doc.get("dashboards") or [{}])[0]
    return [panel for panel in _leaf_panels(dashboard.get("panels") or []) if panel.get("esql")]


def _control_names(yaml_doc: dict[str, Any]) -> set[str]:
    dashboard = (yaml_doc.get("dashboards") or [{}])[0]
    names: set[str] = set()
    for control in dashboard.get("controls") or []:
        if not isinstance(control, dict):
            continue
        for key in ("variable_name", "name", "field_name", "field"):
            value = str(control.get(key) or "")
            if value:
                names.add(value)
    return names


def _assert_sort_before_limit(query: str, context: str) -> None:
    stages = [line.strip() for line in str(query).splitlines() if line.strip().startswith("|")]
    sort_indices = [
        idx for idx, stage in enumerate(stages) if stage[1:].strip().lower().startswith("sort ")
    ]
    limit_indices = [
        idx for idx, stage in enumerate(stages) if stage[1:].strip().lower().startswith("limit ")
    ]
    if sort_indices and limit_indices:
        assert max(sort_indices) < min(limit_indices), f"{context}: SORT must precede LIMIT\n{query}"


def _translate_grafana_fixture(filename: str) -> tuple[Any, dict[str, Any]]:
    dashboard = json.loads((GRAFANA_DASHBOARDS / filename).read_text(encoding="utf-8"))
    rule_pack = _grafana_rule_pack_with_named_params()
    resolver = SchemaResolver(rule_pack)
    with tempfile.TemporaryDirectory() as tmpdir:
        result, yaml_path = translate_dashboard(
            dashboard,
            Path(tmpdir),
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        yaml_doc = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
    return result, yaml_doc


def _grafana_rule_pack_with_named_params() -> RulePackConfig:
    rule_pack = RulePackConfig()
    set_runtime_feature(
        rule_pack,
        ESQL_NAMED_PARAM_BINDING,
        supported=True,
        source="unit-test",
        confidence="assumed",
    )
    return rule_pack


def _translate_real_promql(
    expr: str,
    *,
    panel_type: str = "timeseries",
    field_types: dict[str, str] | None = None,
) -> Any:
    rule_pack = _grafana_rule_pack_with_named_params()
    resolver = SchemaResolver(rule_pack)
    if field_types:
        original_field_type = resolver.field_type

        def field_type(field: str) -> str:
            normalized = str(field or "").strip("`")
            return field_types.get(normalized, original_field_type(field))

        resolver.field_type = field_type
    return translate_promql_to_esql(
        expr,
        esql_index="metrics-*",
        panel_type=panel_type,
        rule_pack=rule_pack,
        resolver=resolver,
    )


def _iter_datadog_widgets(widgets: list[Any]) -> list[Any]:
    ordered: list[Any] = []
    for widget in widgets or []:
        ordered.append(widget)
        ordered.extend(_iter_datadog_widgets(getattr(widget, "children", []) or []))
    return ordered


def _translate_datadog_fixture(relative_path: str) -> tuple[list[Any], dict[str, Any]]:
    raw = json.loads((DATADOG_DASHBOARDS / relative_path).read_text(encoding="utf-8"))
    normalized = normalize_dashboard(raw)
    widgets = _iter_datadog_widgets(normalized.widgets)
    results = [translate_widget(widget, plan_widget(widget), OTEL_PROFILE) for widget in widgets]
    yaml_str = generate_dashboard_yaml(
        normalized,
        results,
        data_view=OTEL_PROFILE.metric_index,
        metrics_dataset_filter=OTEL_PROFILE.metrics_dataset_filter,
        logs_dataset_filter=OTEL_PROFILE.logs_dataset_filter,
        logs_index=OTEL_PROFILE.logs_index,
        field_map=OTEL_PROFILE,
    )
    return results, yaml.safe_load(yaml_str)


class GrafanaRealDashboardTranslationGuardTests(unittest.TestCase):
    def test_real_grafana_dashboards_have_bound_params_and_valid_stage_order(self) -> None:
        # Only first-party / already-noticed committed fixtures are used here;
        # the grafana.com marketplace dashboards are fetched on demand from the
        # pinned community_corpus.json rather than committed.
        fixtures = [
            "k8s-views-global.json",
            "node-exporter-full.json",
            "prometheus-all.json",
        ]
        failures: list[str] = []
        query_count = 0
        for fixture in fixtures:
            _result, yaml_doc = _translate_grafana_fixture(fixture)
            controls = _control_names(yaml_doc)
            for panel in _esql_panels(yaml_doc):
                title = str(panel.get("title") or "?")
                query = str((panel.get("esql") or {}).get("query") or "")
                if not query:
                    continue
                query_count += 1
                context = f"{fixture} / {title}"
                if match := GRAFANA_TEMPLATE_RE.search(query):
                    failures.append(f"{context}: leaked Grafana template token {match.group(0)!r}\n{query}")
                missing = sorted(set(ESQL_PARAM_RE.findall(query)) - TIME_PARAMS - controls)
                if missing:
                    failures.append(
                        f"{context}: emitted unbound ES|QL params {missing}; controls={sorted(controls)}\n{query}"
                    )
                try:
                    _assert_sort_before_limit(query, context)
                except AssertionError as exc:
                    failures.append(str(exc))

        self.assertGreaterEqual(query_count, 100)
        self.assertEqual(failures, [], "\n\n".join(failures))


class GrafanaRealPromQLTranslationGuardTests(unittest.TestCase):
    def test_blackbox_regex_template_filter_becomes_named_param(self) -> None:
        result = _translate_real_promql(
            'avg(probe_dns_lookup_time_seconds{instance=~"$target"})',
            panel_type="timeseries",
        )

        self.assertNotEqual(result.feasibility, "not_feasible", result.warnings)
        self.assertIn("RLIKE ?target", result.esql_query)
        self.assertNotIn("$target", result.esql_query)

    def test_flagger_histogram_quantile_unknown_histogram_type_degrades_honestly(self) -> None:
        result = _translate_real_promql(
            (
                "histogram_quantile(0.95, "
                'sum(irate(istio_request_duration_milliseconds_bucket{reporter="destination",'
                'destination_workload=~"$primary", destination_workload_namespace=~"$namespace",'
                'account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])) by (cluster,le))'
            ),
            panel_type="timeseries",
        )

        self.assertEqual(result.feasibility, "not_feasible")
        self.assertTrue(
            any("field type could not be determined" in warning for warning in result.warnings),
            result.warnings,
        )

    def test_flagger_histogram_quantile_scalar_unit_conversion_when_histogram_field_known(self) -> None:
        result = _translate_real_promql(
            (
                "histogram_quantile(0.95, "
                'sum(irate(istio_request_duration_milliseconds_bucket{reporter="destination",'
                'destination_workload=~"$primary", destination_workload_namespace=~"$namespace",'
                'account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])) by (cluster,le)) / 1000'
            ),
            panel_type="timeseries",
            field_types={"istio_request_duration_milliseconds": "histogram"},
        )

        self.assertNotEqual(result.feasibility, "not_feasible", result.warnings)
        self.assertIn("PERCENTILE", result.esql_query)
        self.assertIn("/ 1000", result.esql_query)
        for param in ("primary", "namespace", "awsaccount", "cluster"):
            self.assertIn(f"?{param}", result.esql_query)
        self.assertNotRegex(result.esql_query, GRAFANA_TEMPLATE_RE)

    def test_k8s_mixed_os_namespace_join_prefers_linux_operand_with_warning(self) -> None:
        result = _translate_real_promql(
            (
                'sum(rate(container_cpu_usage_seconds_total{image!="", cluster="$cluster"}[$__rate_interval])) by (namespace)\n'
                "+ on (namespace)\n"
                '(sum(rate(windows_container_cpu_usage_seconds_total{container_id!="", cluster="$cluster"}[$__rate_interval]) '
                '* on (container_id) group_left (container, pod, namespace) '
                'max by ( container, container_id, pod, namespace) (kube_pod_container_info{container_id!="", cluster="$cluster"}) '
                'OR kube_namespace_created{cluster="$cluster"} * 0) by (namespace))'
            ),
            panel_type="timeseries",
        )

        self.assertNotEqual(result.feasibility, "not_feasible", result.warnings)
        self.assertIn("container_cpu_usage_seconds_total", result.esql_query)
        self.assertIn("namespace", result.esql_query)
        self.assertNotIn("windows_container_cpu_usage_seconds_total", result.esql_query)
        self.assertTrue(any("mixed-OS" in warning for warning in result.warnings), result.warnings)

    def test_node_exporter_nested_core_count_uses_distinct_cpu_denominator(self) -> None:
        result = _translate_real_promql(
            (
                'sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[$__rate_interval])) '
                '/ scalar(count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu)))'
            ),
            panel_type="timeseries",
        )

        self.assertNotEqual(result.feasibility, "not_feasible", result.warnings)
        self.assertIn("COUNT_DISTINCT(cpu)", result.esql_query)
        self.assertNotRegex(result.esql_query, GRAFANA_TEMPLATE_RE)


class DatadogRealDashboardTranslationGuardTests(unittest.TestCase):
    def test_real_datadog_integration_dashboards_emit_clean_esql_queries(self) -> None:
        fixtures = [
            "integrations/apache.json",
            "integrations/kafka.json",
            "integrations/mysql.json",
            "integrations/nginx_overview.json",
            "integrations/postgres.json",
            "integrations/rabbitmq.json",
        ]
        failures: list[str] = []
        migrated_results = 0
        query_count = 0
        for fixture in fixtures:
            results, yaml_doc = _translate_datadog_fixture(fixture)
            migrated_results += sum(1 for result in results if result.status in {"ok", "warning"})
            for panel in _esql_panels(yaml_doc):
                title = str(panel.get("title") or "?")
                query = str((panel.get("esql") or {}).get("query") or "")
                if not query:
                    continue
                query_count += 1
                context = f"{fixture} / {title}"
                if "$" in query:
                    failures.append(f"{context}: leaked Datadog template variable syntax\n{query}")
                try:
                    _assert_sort_before_limit(query, context)
                except AssertionError as exc:
                    failures.append(str(exc))

        self.assertGreaterEqual(migrated_results, 30)
        self.assertGreaterEqual(query_count, 30)
        self.assertEqual(failures, [], "\n\n".join(failures))


if __name__ == "__main__":
    unittest.main()
