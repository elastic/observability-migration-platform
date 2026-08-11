# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Contract tests for Grafana dashboard translation.

These cover translator invariants that still matter after the old dashboard-
YAML snapshot fixture layer was removed:

1. Every migrated panel exposes the required chart-spec keys for its chart type.
2. Every field referenced by the emitted chart spec exists in the query output.
3. Regressions around instant panels, stat thresholds, and summary datatables
   remain covered without depending on snapshot goldens.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from observability_migration.adapters.source.grafana.panels import (
    SKIP_PANEL_TYPES,
    _flatten_dashboard_panels,
    translate_dashboard,
    translate_panel,
)
from observability_migration.targets.kibana.compile import _iter_leaf_panels
from observability_migration.targets.kibana.emit.esql_utils import (
    split_esql_pipeline,
    split_top_level_assignment,
    split_top_level_keyword,
)

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_DASHBOARD_DIR = _REPO_ROOT / "infra" / "grafana" / "dashboards"
DASHBOARD_FILES: list[pathlib.Path] = sorted(_DASHBOARD_DIR.glob("*.json"))

REQUIRED_KEYS: dict[str, list[str]] = {
    "line": ["dimension", "metrics"],
    "bar": ["dimension", "metrics"],
    "area": ["dimension", "metrics"],
    "metric": ["primary"],
    "gauge": ["metric"],
    "datatable": ["metrics"],
    "pie": ["metrics", "breakdowns"],
}


def _load_dashboard(path: pathlib.Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _workable_panels(dashboard: dict) -> list[dict]:
    flat = _flatten_dashboard_panels(dashboard)
    return [p for p in flat if p.get("type") not in SKIP_PANEL_TYPES and p.get("type") != "row"]


def _split_csv_top_level(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in text:
        if ch in ("(", "["):
            depth += 1
            current.append(ch)
        elif ch in (")", "]"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def _strip_backticks(identifier: str) -> str:
    text = identifier.strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        return text[1:-1]
    return text


def _final_output_columns(query: str) -> set[str]:
    commands = split_esql_pipeline(query)
    if not commands:
        return set()
    if any("promql(" in c.lower() for c in commands):
        return set()

    cols: set[str] = set()
    for cmd in commands:
        cl = cmd.lower()
        if cl.startswith("stats "):
            body, by_text = split_top_level_keyword(cmd[6:].strip(), "BY")
            cols = set()
            for part in _split_csv_top_level(body):
                alias, _ = split_top_level_assignment(part)
                if alias:
                    cols.add(_strip_backticks(alias))
            for part in _split_csv_top_level(by_text):
                alias, expr = split_top_level_assignment(part)
                field = alias or (expr or "").strip()
                if field:
                    cols.add(_strip_backticks(field))
        elif cl.startswith("eval "):
            for part in _split_csv_top_level(cmd[5:].strip()):
                alias, _ = split_top_level_assignment(part)
                if alias:
                    cols.add(_strip_backticks(alias))
        elif cl.startswith("keep "):
            cols = {
                _strip_backticks(f.strip())
                for f in _split_csv_top_level(cmd[5:].strip())
                if f.strip()
            }
        elif cl.startswith("drop "):
            cols -= {
                _strip_backticks(f.strip())
                for f in _split_csv_top_level(cmd[5:].strip())
                if f.strip()
            }
    return cols


def _spec_fields(esql_block: dict) -> set[str]:
    fields: set[str] = set()

    def _add(value):
        if isinstance(value, dict):
            field = value.get("field")
            if field:
                fields.add(field)
        elif isinstance(value, str) and value:
            fields.add(value)

    _add(esql_block.get("dimension"))
    _add(esql_block.get("breakdown"))
    _add(esql_block.get("primary"))
    _add(esql_block.get("metric"))
    for item in esql_block.get("metrics", []):
        _add(item)
    for item in esql_block.get("breakdowns", []):
        _add(item)
    fields.discard("_gauge_min")
    fields.discard("_gauge_max")
    fields.discard("_gauge_goal")
    return fields


def _render_dashboard(path: pathlib.Path) -> dict[str, object]:
    dashboard = _load_dashboard(path)
    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
    )
    return result.dashboard_ir.to_yaml_dict()


class TestGrafanaStructure(unittest.TestCase):
    def _check_dashboard(self, path: pathlib.Path) -> None:
        dash = _load_dashboard(path)
        failures: list[str] = []
        for panel in _workable_panels(dash):
            yp, result = translate_panel(panel)
            if result.status not in ("migrated", "migrated_with_warnings") or not yp:
                continue
            esql = yp.get("esql", {})
            chart_type = esql.get("type")
            if not chart_type:
                continue
            missing = [k for k in REQUIRED_KEYS.get(chart_type, []) if k not in esql]
            if missing:
                failures.append(f"  {panel.get('title')!r} ({chart_type}): missing {missing}")
        if failures:
            self.fail(f"{path.name}: {len(failures)} structural issue(s):\n" + "\n".join(failures))


def _make_structure_test(dashboard_path: pathlib.Path):
    def test_method(self):
        self._check_dashboard(dashboard_path)

    test_method.__name__ = f"test_{dashboard_path.stem.replace('-', '_').replace('.', '_')}"
    return test_method


for _dp in DASHBOARD_FILES:
    setattr(TestGrafanaStructure, f"test_{_dp.stem.replace('-', '_')}", _make_structure_test(_dp))


class TestGrafanaFieldContracts(unittest.TestCase):
    def _check_dashboard(self, path: pathlib.Path) -> None:
        dash = _load_dashboard(path)
        failures: list[str] = []
        for panel in _workable_panels(dash):
            yp, result = translate_panel(panel)
            if result.status not in ("migrated", "migrated_with_warnings") or not yp:
                continue
            esql = yp.get("esql", {})
            chart_type = esql.get("type")
            if not chart_type:
                continue
            output_cols = _final_output_columns(esql.get("query", ""))
            if not output_cols:
                continue
            missing = _spec_fields(esql) - output_cols
            if missing:
                failures.append(
                    f"  {panel.get('title')!r} ({chart_type}): "
                    f"spec fields {sorted(missing)} missing from output {sorted(output_cols)}"
                )
        if failures:
            self.fail(
                f"{path.name}: {len(failures)} field contract violation(s):\n" + "\n".join(failures)
            )


def _make_contract_test(dashboard_path: pathlib.Path):
    def test_method(self):
        self._check_dashboard(dashboard_path)

    test_method.__name__ = f"test_{dashboard_path.stem.replace('-', '_')}"
    return test_method


for _dp in DASHBOARD_FILES:
    setattr(TestGrafanaFieldContracts, f"test_{_dp.stem.replace('-', '_')}", _make_contract_test(_dp))


def _instant_panel(panel_type: str, expr: str = "time() - process_start_time_seconds") -> dict:
    return {
        "id": 1,
        "type": panel_type,
        "title": f"{panel_type} instant",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [{"refId": "A", "expr": expr, "instant": True}],
        "gridPos": {"h": 8, "w": 6, "x": 0, "y": 0},
    }


class TestInstantSingleValuePanels(unittest.TestCase):
    def _assert_no_phantom_dimension(self, panel: dict) -> None:
        yp, result = translate_panel(panel)
        self.assertIn(result.status, ("migrated", "migrated_with_warnings"))
        esql = yp.get("esql", {})
        chart_type = esql.get("type")
        self.assertTrue(chart_type, f"panel produced no esql block: {result.status}")
        output_cols = _final_output_columns(esql.get("query", ""))
        self.assertTrue(output_cols, "expected legacy ES|QL with static columns")
        spec_flds = _spec_fields(esql)
        missing = spec_flds - output_cols
        self.assertFalse(
            missing,
            f"{panel.get('title')!r} ({chart_type}): spec fields {sorted(missing)} "
            f"absent from query output {sorted(output_cols)}",
        )
        self.assertNotIn("time_bucket", spec_flds)

    def test_stat_instant_uptime_maps_to_single_value(self):
        self._assert_no_phantom_dimension(_instant_panel("stat"))

    def test_gauge_instant_uptime_maps_to_single_value(self):
        self._assert_no_phantom_dimension(_instant_panel("gauge"))

    def test_timeseries_with_instant_query_degrades_to_metric(self):
        panel = _instant_panel("timeseries")
        self._assert_no_phantom_dimension(panel)
        yp, _ = translate_panel(panel)
        self.assertEqual(yp["esql"]["type"], "metric")


class TestStatThresholdColor(unittest.TestCase):
    @staticmethod
    def _stat_panel(*, color_mode="value", steps=None):
        panel = _instant_panel("stat")
        panel["fieldConfig"] = {
            "defaults": {
                "thresholds": {
                    "mode": "absolute",
                    "steps": steps if steps is not None else [
                        {"value": None, "color": "green"},
                        {"value": 80, "color": "red"},
                    ],
                }
            }
        }
        panel["options"] = {"colorMode": color_mode}
        return panel

    def test_stat_thresholds_map_to_primary_dynamic_color(self):
        from observability_migration.targets.kibana.dashboards_api import _api_color

        yp, _ = translate_panel(self._stat_panel())
        esql = yp["esql"]
        self.assertEqual(esql["type"], "metric")
        color = esql["primary"].get("color")
        self.assertIsInstance(color, dict)
        self.assertEqual(color["apply_to"], "value")
        colors = [t["color"] for t in color["thresholds"]]
        self.assertIn("#E7664C", colors)

        native = _api_color(color)
        self.assertEqual(native["type"], "dynamic")
        self.assertEqual(native["range"], "absolute")
        step_colors = [s["color"] for s in native["steps"]]
        self.assertIn("#54B399", step_colors)
        self.assertIn("#E7664C", step_colors)
        red = next(s for s in native["steps"] if s["color"] == "#E7664C")
        self.assertEqual(red.get("gte"), 80)

    def test_stat_color_mode_none_suppresses_color(self):
        yp, _ = translate_panel(self._stat_panel(color_mode="none"))
        self.assertNotIn("color", yp["esql"]["primary"])

    def test_stat_without_thresholds_has_no_color(self):
        panel = _instant_panel("stat")
        yp, _ = translate_panel(panel)
        self.assertNotIn("color", yp["esql"]["primary"])


class TestSummaryTableKeepsTimeColumn(unittest.TestCase):
    def _datatable_panels(self, dashboard_stem: str) -> dict[str, dict]:
        path = next(p for p in DASHBOARD_FILES if p.stem == dashboard_stem)
        rendered = _render_dashboard(path)
        return {
            str(p.get("title")): p["esql"]
            for p in _iter_leaf_panels(rendered.get("panels") or [])
            if isinstance(p.get("esql"), dict) and p["esql"].get("type") == "datatable"
        }

    def test_active_alerts_table_surfaces_time_bucket_row(self):
        block = self._datatable_panels("diverse-panels-test")["Active Alerts"]
        fields = [b.get("field") for b in block.get("breakdowns") or []]
        self.assertIn("time_bucket", fields)
        self.assertIn("time_bucket", block.get("query", ""))
        time_row = next(b for b in block["breakdowns"] if b.get("field") == "time_bucket")
        self.assertEqual(time_row.get("data_type"), "date")

    def test_target_health_table_surfaces_time_bucket_row(self):
        block = self._datatable_panels("home")["Target Health Status"]
        fields = [b.get("field") for b in block.get("breakdowns") or []]
        self.assertIn("time_bucket", fields)
