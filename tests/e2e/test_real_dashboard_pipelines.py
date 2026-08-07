# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""End-to-end translation checks using real shipped dashboards."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

import yaml

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import generate_dashboard_yaml
from observability_migration.adapters.source.datadog.models import NormalizedDashboard, TranslationResult
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.adapters.source.grafana.panels import translate_dashboard
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.core.reporting.report import MigrationResult

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAFANA_DASHBOARD_DIR = REPO_ROOT / "infra" / "grafana" / "dashboards"
DATADOG_DASHBOARD_DIR = REPO_ROOT / "infra" / "datadog" / "dashboards"


def _percentile_widget(title: str, query: str, x: int, y: int) -> dict[str, Any]:
    return {
        "definition": {
            "type": "timeseries",
            "title": title,
            "requests": [{"q": query, "display_type": "line"}],
        },
        "layout": {"x": x, "y": y, "width": 6, "height": 4},
    }


# Regression fixture for issue #144: Datadog percentile/max timeseries queries
# used to compile into XYLensFormulaMetric blocks that failed kb-dashboard-cli
# schema validation (formula required; aggregation/field rejected). This mirrors
# the "Latency p95" dashboard from the issue, covering every affected
# aggregation: p50/p75/p95/p99 (percentile), max (other-aggregated), and avg.
ISSUE_144_PERCENTILE_DASHBOARD: dict[str, Any] = {
    "title": "Latency percentiles (issue 144)",
    "description": "Percentile/max timeseries regression coverage",
    "widgets": [
        _percentile_widget("p50 duration", "p50:trace.http.request.duration{*} by {service}", 0, 0),
        _percentile_widget("p75 duration", "p75:trace.http.request.duration{*} by {service}", 6, 0),
        _percentile_widget("p95 by resource", "p95:trace.http.request.duration{*} by {resource_name}", 0, 4),
        _percentile_widget("p99 by service", "p99:trace.http.request.duration{*} by {service}", 6, 4),
        _percentile_widget("Max duration", "max:trace.http.request.duration{*} by {resource_name}", 0, 8),
        _percentile_widget("Avg duration", "avg:trace.http.request.duration{*} by {service}", 6, 8),
    ],
    "template_variables": [{"name": "env", "default": "*", "prefix": "env"}],
}


def _leaf_panels(panels: list[dict]) -> list[dict]:
    leaves: list[dict] = []
    stack = list(panels)
    while stack:
        panel = stack.pop(0)
        section = panel.get("section")
        if isinstance(section, dict):
            stack = list(section.get("panels") or []) + stack
            continue
        leaves.append(panel)
    return leaves


def _panels_by_title(yaml_doc: dict) -> dict[str, dict]:
    dashboards = yaml_doc.get("dashboards") or []
    if not dashboards:
        return {}
    return {
        panel.get("title", f"panel-{idx}"): panel
        for idx, panel in enumerate(_leaf_panels(dashboards[0].get("panels") or []))
    }


def _iter_datadog_widgets(widgets: list[Any]) -> list[Any]:
    ordered: list[Any] = []
    for widget in widgets or []:
        ordered.append(widget)
        ordered.extend(_iter_datadog_widgets(getattr(widget, "children", []) or []))
    return ordered


def _translate_grafana_dashboard(
    filename: str,
    *,
    native_promql: bool = False,
) -> tuple[MigrationResult, dict[str, Any]]:
    """Translate a shipped Grafana dashboard and return its kb-dashboard-core doc.

    ``translate_dashboard`` writes nothing to disk; the document shape the
    assertions below walk is derived in memory from the semantic IR, which is
    the same source the native Dashboards API payload is built from.
    """
    rule_pack = RulePackConfig()
    rule_pack.native_promql = native_promql
    resolver = SchemaResolver(rule_pack)
    dashboard = json.loads((GRAFANA_DASHBOARD_DIR / filename).read_text(encoding="utf-8"))
    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    yaml_doc = {"dashboards": [result.dashboard_ir.to_yaml_dict()]}
    return result, yaml_doc


def _translate_datadog_raw(
    raw: dict[str, Any],
    output_dir: Path | None = None,
    *,
    yaml_stem: str = "datadog_dashboard",
) -> tuple[NormalizedDashboard, list[TranslationResult], Path | None, dict[str, Any]]:
    """Translate an in-memory Datadog dashboard dict (no infra file needed)."""
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
    yaml_path = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = output_dir / f"{yaml_stem}.yaml"
        yaml_path.write_text(yaml_str, encoding="utf-8")
    return normalized, results, yaml_path, yaml.safe_load(yaml_str)


def _translate_datadog_dashboard(
    relative_path: str,
    output_dir: Path | None = None,
) -> tuple[NormalizedDashboard, list[TranslationResult], Path | None, dict[str, Any]]:
    raw = json.loads((DATADOG_DASHBOARD_DIR / relative_path).read_text(encoding="utf-8"))
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
    yaml_path = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        yaml_filename = f"{Path(relative_path).stem.replace(' ', '_') or 'datadog_dashboard'}.yaml"
        yaml_path = output_dir / yaml_filename
        yaml_path.write_text(yaml_str, encoding="utf-8")
    yaml_doc = yaml.safe_load(yaml_str)
    return normalized, results, yaml_path, yaml_doc


def _status_counts(results: list[TranslationResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


class TestGrafanaRealDashboardPipelines(unittest.TestCase):
    def test_diverse_panels_dashboard_preserves_mixed_semantics(self):
        result, yaml_doc = _translate_grafana_dashboard("diverse-panels-test.json")

        panels = _panels_by_title(yaml_doc)
        controls = yaml_doc["dashboards"][0].get("controls") or []

        # 11 source panels plus one rendered links panel synthesized from
        # dashboard-level external-link metadata.
        self.assertEqual(result.total_panels, 12)
        self.assertEqual(len(controls), 1)

        # A Grafana heatmap now emits a native Kibana heatmap (x=time bucket,
        # y=le, color=metric) rather than degrading to a line chart.
        heatmap = panels["Request Latency Heatmap"]["esql"]
        self.assertEqual(heatmap["type"], "heatmap")
        self.assertEqual(heatmap["x_axis"]["field"], "time_bucket")
        self.assertEqual(heatmap["y_axis"]["field"], "le")
        self.assertEqual(heatmap["metric"]["field"], "http_request_duration_seconds_bucket")

        traffic = panels["Traffic Distribution"]["esql"]
        self.assertEqual(traffic["type"], "pie")
        self.assertEqual(traffic["breakdowns"][0]["field"], "handler")
        self.assertNotIn("$instance", traffic["query"])

        top_endpoints = panels["Top Endpoints"]["esql"]
        self.assertEqual(top_endpoints["type"], "bar")
        self.assertEqual(top_endpoints["dimension"]["field"], "handler")
        self.assertIn("| SORT value DESC", top_endpoints["query"])
        self.assertIn("| LIMIT 10", top_endpoints["query"])

        app_logs = panels["Application Logs"]["esql"]
        self.assertEqual(app_logs["type"], "datatable")
        self.assertIn('service.name == "app"', app_logs["query"])
        self.assertIn('message LIKE "*error*"', app_logs["query"])

    def test_k8s_views_global_keeps_sections_and_metrics(self):
        result, yaml_doc = _translate_grafana_dashboard("k8s-views-global.json")

        top_panels = yaml_doc["dashboards"][0].get("panels") or []
        section_titles = [panel.get("title") for panel in top_panels if "section" in panel]
        leaf_panels = _panels_by_title(yaml_doc)

        self.assertEqual(result.total_panels, 30)
        self.assertEqual(section_titles, ["Overview", "Resources", "Kubernetes", "Network"])
        self.assertEqual(len(yaml_doc["dashboards"][0].get("controls") or []), 2)
        # Multi-series summary bargauge → metric tiles with label breakdown
        # (see ``bargauge_panel_rule``), not a category bar chart.
        cpu = leaf_panels["Global CPU  Usage"]["esql"]
        self.assertEqual(cpu["type"], "metric")
        self.assertEqual(cpu["breakdown"]["field"], "label")
        self.assertEqual(leaf_panels["Nodes"]["esql"]["type"], "metric")

    def test_prometheus_all_keeps_metric_and_area_panels(self):
        result, yaml_doc = _translate_grafana_dashboard("prometheus-all.json")

        panels = _panels_by_title(yaml_doc)

        # 44 source panels plus one rendered links panel synthesized from
        # dashboard-level external-link metadata.
        self.assertEqual(result.total_panels, 45)
        self.assertEqual(len(yaml_doc["dashboards"][0].get("controls") or []), 1)
        self.assertEqual(panels["Uptime"]["esql"]["type"], "metric")
        self.assertEqual(panels["Query elapsed time"]["esql"]["type"], "area")
        self.assertIn("prometheus_engine_query_duration_seconds", panels["Query elapsed time"]["esql"]["query"])


class TestDatadogRealDashboardPipelines(unittest.TestCase):
    def test_postgres_dashboard_translates_all_widgets(self):
        _, results, _, yaml_doc = _translate_datadog_dashboard("integrations/postgres.json")

        panels = _panels_by_title(yaml_doc)
        counts = _status_counts(results)

        # All 9 widgets translate successfully. They scope on the unbound
        # `$scope` template variable, so each carries a "bind via Kibana
        # controls" warning rather than a clean "ok".
        self.assertEqual(counts.get("ok", 0) + counts.get("warning", 0), 9)
        self.assertEqual(len(yaml_doc["dashboards"][0].get("panels") or []), 9)

        connections = panels["Connections"]["esql"]
        self.assertEqual(connections["type"], "line")
        self.assertIn("postgresql", connections["query"])

    def test_redis_overview_is_honestly_skipped_when_only_groups_exist(self):
        normalized, results, _, yaml_doc = _translate_datadog_dashboard("integrations/redis.json")

        self.assertEqual(len(normalized.widgets), 7)
        self.assertGreater(len(results), len(normalized.widgets))
        self.assertTrue(results)
        self.assertTrue(any(result.status == "skipped" for result in results))
        self.assertTrue(any(result.status in {"ok", "warning", "requires_manual"} for result in results))
        self.assertGreater(len(_leaf_panels(yaml_doc["dashboards"][0].get("panels") or [])), 20)

    def test_docker_dashboard_has_mixed_statuses_and_not_feasible(self):
        _, results, _, yaml_doc = _translate_datadog_dashboard("integrations/docker.json")

        counts = _status_counts(results)

        self.assertGreater(counts.get("not_feasible", 0), 0)
        total_panels = len(yaml_doc["dashboards"][0].get("panels") or [])
        self.assertGreater(total_panels, 20)

    def test_issue_144_percentile_and_max_emit_native_esql_metrics(self):
        """Issue #144: percentile/max timeseries must emit schema-valid
        native ES|QL metrics (never a legacy Lens formula metric).
        """
        _, results, _, yaml_doc = _translate_datadog_raw(ISSUE_144_PERCENTILE_DASHBOARD)

        panels = _panels_by_title(yaml_doc)
        # Every percentile/max/avg widget must translate cleanly into a
        # renderable panel. ("blocked" is a plan backend, not a status: the
        # translator only ever emits ok/warning/skipped/requires_manual/
        # not_feasible, mapping a blocked backend to not_feasible.) Guard the
        # statuses that actually signal a failed/degraded translation.
        counts = _status_counts(results)
        self.assertEqual(counts.get("not_feasible", 0), 0, counts)
        self.assertEqual(counts.get("requires_manual", 0), 0, counts)
        self.assertEqual(counts.get("skipped", 0), 0, counts)

        expected_percentiles = {
            "p50 duration": 50,
            "p75 duration": 75,
            "p95 by resource": 95,
            "p99 by service": 99,
        }
        for title, pct in expected_percentiles.items():
            panel = panels[title]
            self.assertNotIn("lens", panel, title)
            self.assertEqual(panel["esql"]["type"], "line", title)
            self.assertIn(
                f"PERCENTILE(trace_http_request_duration, {pct})",
                panel["esql"]["query"],
                title,
            )
            self.assertEqual(panel["esql"]["dimension"]["field"], "time_bucket", title)
            self.assertEqual([metric["field"] for metric in panel["esql"]["metrics"]], ["value"], title)

        max_panel = panels["Max duration"]
        self.assertNotIn("lens", max_panel)
        self.assertIn("MAX(trace_http_request_duration)", max_panel["esql"]["query"])

        avg_panel = panels["Avg duration"]
        self.assertNotIn("lens", avg_panel)
        self.assertIn("AVG(trace_http_request_duration)", avg_panel["esql"]["query"])
