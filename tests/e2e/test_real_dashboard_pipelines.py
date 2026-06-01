# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""End-to-end translation checks using real shipped dashboards."""

from __future__ import annotations

import json
import shutil
import tempfile
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
from observability_migration.targets.kibana import compile as shared_compile

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAFANA_DASHBOARD_DIR = REPO_ROOT / "infra" / "grafana" / "dashboards"
DATADOG_DASHBOARD_DIR = REPO_ROOT / "infra" / "datadog" / "dashboards"


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
    output_dir: Path,
    *,
    native_promql: bool = False,
) -> tuple[MigrationResult, Path, dict[str, Any]]:
    rule_pack = RulePackConfig()
    rule_pack.native_promql = native_promql
    resolver = SchemaResolver(rule_pack)
    dashboard = json.loads((GRAFANA_DASHBOARD_DIR / filename).read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    result, yaml_path = translate_dashboard(
        dashboard,
        output_dir,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    yaml_doc = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
    return result, Path(yaml_path), yaml_doc


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
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _, yaml_doc = _translate_grafana_dashboard("diverse-panels-test.json", Path(tmpdir))

        panels = _panels_by_title(yaml_doc)
        controls = yaml_doc["dashboards"][0].get("controls") or []

        self.assertEqual(result.total_panels, 11)
        self.assertEqual(len(controls), 1)

        heatmap = panels["Request Latency Heatmap"]["esql"]
        self.assertEqual(heatmap["type"], "line")
        self.assertEqual(heatmap["appearance"]["y_left_axis"]["scale"], "log")

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
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _, yaml_doc = _translate_grafana_dashboard("k8s-views-global.json", Path(tmpdir))

        top_panels = yaml_doc["dashboards"][0].get("panels") or []
        section_titles = [panel.get("title") for panel in top_panels if "section" in panel]
        leaf_panels = _panels_by_title(yaml_doc)

        self.assertEqual(result.total_panels, 30)
        self.assertEqual(section_titles, ["Overview", "Resources", "Kubernetes", "Network"])
        self.assertEqual(len(yaml_doc["dashboards"][0].get("controls") or []), 2)
        self.assertEqual(leaf_panels["Global CPU  Usage"]["esql"]["type"], "bar")
        self.assertEqual(leaf_panels["Nodes"]["esql"]["type"], "metric")

    def test_prometheus_all_keeps_metric_and_area_panels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result, _, yaml_doc = _translate_grafana_dashboard("prometheus-all.json", Path(tmpdir))

        panels = _panels_by_title(yaml_doc)

        self.assertEqual(result.total_panels, 44)
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


@unittest.skipUnless(shutil.which("uvx"), "uvx is required for compile smoke tests")
class TestRealCompileSmoke(unittest.TestCase):
    def _assert_lint_compile_and_layout(self, yaml_dir: Path, yaml_path: Path, expected_dashboard_name: str):
        lint_ok, lint_output = shared_compile.lint_dashboard_yaml(yaml_dir)
        self.assertTrue(lint_ok, lint_output)

        compiled_dir = yaml_dir / "compiled"
        compiled_dir.mkdir(parents=True, exist_ok=True)
        compile_ok, compile_output = shared_compile.compile_yaml(yaml_path, compiled_dir)
        self.assertTrue(compile_ok, compile_output)

        layout_ok, layout_output = shared_compile.validate_compiled_layout(compiled_dir)
        self.assertTrue(layout_ok, layout_output)
        self.assertTrue((compiled_dir / "compiled_dashboards.ndjson").exists())
        self.assertIn(expected_dashboard_name, layout_output)

    def test_grafana_diverse_dashboard_lints_compiles_and_validates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = Path(tmpdir) / "grafana_yaml"
            _, yaml_path, _ = _translate_grafana_dashboard("diverse-panels-test.json", yaml_dir)
            self._assert_lint_compile_and_layout(yaml_dir, yaml_path, "Diverse Panel Types Test")

    def test_datadog_postgres_dashboard_lints_compiles_and_validates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = Path(tmpdir) / "datadog_yaml"
            _, _, yaml_path, _ = _translate_datadog_dashboard("integrations/postgres.json", yaml_dir)
            assert yaml_path is not None
            self._assert_lint_compile_and_layout(yaml_dir, yaml_path, "Postgres - Metrics")
