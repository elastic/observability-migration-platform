# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""YAML generation tests for the Datadog -> Kibana migration pipeline.

These tests mirror the Grafana YAML harness, but run against the Datadog
dashboard corpus because Datadog has more independent YAML emission logic.
Snapshots intentionally capture compact panel shapes rather than full YAML.
"""

from __future__ import annotations

import difflib
import json
import os
import pathlib
import unittest
from functools import cache
from typing import Any

import yaml

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import (
    _infer_dimensions,
    _infer_metrics,
    generate_dashboard_yaml,
)
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.targets.kibana.emit.esql_utils import extract_esql_shape

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_DASHBOARD_DIR = _REPO_ROOT / "infra" / "datadog" / "dashboards"
_SNAPSHOT_DIR = pathlib.Path(__file__).parent / "snapshots" / "datadog_yaml"
UPDATE_SNAPSHOTS = os.environ.get("UPDATE_SNAPSHOTS") == "1"

DASHBOARD_FILES = sorted(_DASHBOARD_DIR.glob("*.json")) + sorted((_DASHBOARD_DIR / "integrations").glob("*.json"))

ESQL_REQUIRED_KEYS: dict[str, list[str]] = {
    "line": ["dimension", "metrics"],
    "bar": ["dimension", "metrics"],
    "area": ["dimension", "metrics"],
    "metric": ["primary"],
    "gauge": ["metric"],
    "pie": ["metrics", "breakdowns"],
    "treemap": ["metric", "breakdowns"],
    "heatmap": ["x_axis", "metric"],
}

LENS_REQUIRED_KEYS: dict[str, list[str]] = {
    "line": ["dimension", "metrics"],
    "bar": ["dimension", "metrics"],
    "area": ["dimension", "metrics"],
    "metric": ["primary"],
    "pie": ["metrics", "breakdown"],
}


def _dashboard_id(path: pathlib.Path) -> str:
    rel = path.relative_to(_DASHBOARD_DIR).with_suffix("")
    return "__".join(rel.parts).replace("-", "_").replace(".", "_")


@cache
def _render_dashboard(path: pathlib.Path) -> tuple[dict[str, Any], list[Any], dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    dashboard = normalize_dashboard(raw)
    widgets = _iter_widgets(dashboard.widgets)
    results = [translate_widget(widget, plan_widget(widget), OTEL_PROFILE) for widget in widgets]
    payload = yaml.safe_load(
        generate_dashboard_yaml(
            dashboard,
            results,
            data_view=OTEL_PROFILE.metric_index,
            metrics_dataset_filter=OTEL_PROFILE.metrics_dataset_filter,
            logs_dataset_filter=OTEL_PROFILE.logs_dataset_filter,
            logs_index=OTEL_PROFILE.logs_index,
            field_map=OTEL_PROFILE,
        )
    ) or {}
    return dashboard, results, payload["dashboards"][0]


def _iter_widgets(widgets: list[Any]) -> list[Any]:
    ordered: list[Any] = []
    for widget in widgets or []:
        ordered.append(widget)
        ordered.extend(_iter_widgets(getattr(widget, "children", []) or []))
    return ordered


def _iter_leaf_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []
    for panel in panels or []:
        section = panel.get("section")
        if isinstance(section, dict):
            leaves.extend(_iter_leaf_panels(section.get("panels") or []))
        else:
            leaves.append(panel)
    return leaves


def _add_spec_field(fields: set[str], value: Any) -> None:
    if isinstance(value, dict):
        field = value.get("field")
        if field:
            fields.add(str(field))
    elif isinstance(value, str) and value:
        fields.add(value)


def _spec_fields(block: dict[str, Any]) -> set[str]:
    fields: set[str] = set()
    for key in ("dimension", "breakdown", "x_axis", "y_axis", "primary", "metric"):
        _add_spec_field(fields, block.get(key))
    for key in ("metrics", "breakdowns"):
        for item in block.get(key) or []:
            _add_spec_field(fields, item)
    return fields


def _output_columns(query: str) -> set[str]:
    shape = extract_esql_shape(query)
    return set(shape.projected_fields or list(shape.group_fields) + list(shape.metric_fields))


def _field_names(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    names = []
    for item in items:
        if isinstance(item, dict) and item.get("field"):
            names.append(str(item["field"]))
    return names


def _snapshot_text(path: pathlib.Path) -> str:
    dashboard, results, rendered = _render_dashboard(path)
    leaves = _iter_leaf_panels(rendered.get("panels") or [])
    status_counts: dict[str, int] = {}
    for result in results:
        status_counts[result.status] = status_counts.get(result.status, 0) + 1

    lines = [
        f"dashboard: {dashboard.title}",
        f"source: {path.relative_to(_REPO_ROOT)}",
        f"widgets: {len(_iter_widgets(dashboard.widgets))}",
        f"panels: {len(leaves)}",
        f"controls: {len(rendered.get('controls') or [])}",
        f"filters: {len(rendered.get('filters') or [])}",
        f"statuses: {dict(sorted(status_counts.items()))}",
    ]
    for panel in leaves:
        if "esql" in panel:
            block = panel["esql"]
            details = [
                f"title={panel.get('title', '')!r}",
                "kind=esql",
                f"type={block.get('type', '')}",
                f"dimension={(block.get('dimension') or {}).get('field', '') if isinstance(block.get('dimension'), dict) else ''}",
                f"metrics={_field_names(block.get('metrics'))}",
                f"breakdowns={_field_names(block.get('breakdowns'))}",
                f"primary={(block.get('primary') or {}).get('field', '') if isinstance(block.get('primary'), dict) else ''}",
                f"metric={(block.get('metric') or {}).get('field', '') if isinstance(block.get('metric'), dict) else ''}",
            ]
            if isinstance(block.get("x_axis"), dict):
                details.append(f"x_axis={block['x_axis'].get('field', '')}")
            if isinstance(block.get("y_axis"), dict):
                details.append(f"y_axis={block['y_axis'].get('field', '')}")
        elif "lens" in panel:
            block = panel["lens"]
            details = [
                f"title={panel.get('title', '')!r}",
                "kind=lens",
                f"type={block.get('type', '')}",
                f"dimension={(block.get('dimension') or {}).get('field', '') if isinstance(block.get('dimension'), dict) else ''}",
                f"metrics={_field_names(block.get('metrics'))}",
                f"primary={(block.get('primary') or {}).get('field', '') if isinstance(block.get('primary'), dict) else ''}",
            ]
        else:
            details = [
                f"title={panel.get('title', '')!r}",
                "kind=markdown",
            ]
        lines.append("- " + "; ".join(details))
    return "\n".join(lines) + "\n"


def _diff(expected: str, actual: str) -> str:
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile="expected",
            tofile="actual",
        )
    )


class TestDatadogYAMLStructure(unittest.TestCase):
    def _check_dashboard(self, path: pathlib.Path) -> None:
        _dashboard, _results, rendered = _render_dashboard(path)
        failures: list[str] = []
        for panel in _iter_leaf_panels(rendered.get("panels") or []):
            title = panel.get("title", "<untitled>")
            if not any(key in panel for key in ("esql", "lens", "markdown")):
                failures.append(f"  {title!r}: missing esql/lens/markdown block")
                continue
            if "esql" in panel:
                block = panel["esql"]
                chart_type = block.get("type", "")
                if not block.get("query"):
                    failures.append(f"  {title!r} ({chart_type}): missing query")
                if chart_type == "datatable":
                    if not block.get("metrics") and not block.get("breakdowns"):
                        failures.append(f"  {title!r} ({chart_type}): missing metrics or breakdowns")
                    continue
                missing = [key for key in ESQL_REQUIRED_KEYS.get(chart_type, []) if key not in block]
                if missing:
                    failures.append(f"  {title!r} ({chart_type}): missing required key(s) {missing}")
            elif "lens" in panel:
                block = panel["lens"]
                chart_type = block.get("type", "")
                missing = [key for key in LENS_REQUIRED_KEYS.get(chart_type, []) if key not in block]
                if missing:
                    failures.append(f"  {title!r} lens ({chart_type}): missing required key(s) {missing}")
        if failures:
            self.fail(f"{path.name}: {len(failures)} structural issue(s):\n" + "\n".join(failures))


class TestDatadogYAMLFieldContracts(unittest.TestCase):
    def _check_dashboard(self, path: pathlib.Path) -> None:
        _dashboard, _results, rendered = _render_dashboard(path)
        failures: list[str] = []
        for panel in _iter_leaf_panels(rendered.get("panels") or []):
            block = panel.get("esql")
            if not isinstance(block, dict):
                continue
            output_cols = _output_columns(str(block.get("query") or ""))
            if not output_cols:
                continue
            missing = _spec_fields(block) - output_cols
            if missing:
                failures.append(
                    f"  {panel.get('title', '<untitled>')!r} ({block.get('type', '')}): "
                    f"field(s) {sorted(missing)} referenced in spec but absent from query output {sorted(output_cols)}"
                )
        if failures:
            self.fail(f"{path.name}: {len(failures)} field contract violation(s):\n" + "\n".join(failures))


class TestDatadogYAMLShapeInvariants(unittest.TestCase):
    def _check_dashboard(self, path: pathlib.Path) -> None:
        _dashboard, results, rendered = _render_dashboard(path)
        failures: list[str] = []
        for result in results:
            if not result.esql_query:
                continue
            shape = extract_esql_shape(result.esql_query)
            if not shape.projected_fields and not shape.metric_fields:
                failures.append(f"  {result.title!r}: ES|QL shape parser produced no output fields")
                continue
            if _infer_dimensions(result) != list(shape.group_fields):
                failures.append(
                    f"  {result.title!r}: inferred dimensions {_infer_dimensions(result)!r} "
                    f"do not match shape group fields {list(shape.group_fields)!r}"
                )
            if shape.metric_fields and _infer_metrics(result) != list(shape.metric_fields):
                failures.append(
                    f"  {result.title!r}: inferred metrics {_infer_metrics(result)!r} "
                    f"do not match shape metric fields {list(shape.metric_fields)!r}"
                )

        for panel in _iter_leaf_panels(rendered.get("panels") or []):
            block = panel.get("esql")
            if not isinstance(block, dict):
                continue
            query = str(block.get("query") or "")
            if not query:
                continue
            shape = extract_esql_shape(query)
            if not shape.projected_fields and not shape.metric_fields:
                failures.append(f"  {panel.get('title', '<untitled>')!r}: emitted query has no parsed output fields")
        if failures:
            self.fail(f"{path.name}: {len(failures)} shape invariant issue(s):\n" + "\n".join(failures))


class TestDatadogYAMLLensContracts(unittest.TestCase):
    def _check_dashboard(self, path: pathlib.Path) -> None:
        _dashboard, _results, rendered = _render_dashboard(path)
        failures: list[str] = []
        for panel in _iter_leaf_panels(rendered.get("panels") or []):
            lens = panel.get("lens")
            if not isinstance(lens, dict):
                continue
            title = panel.get("title", "<untitled>")
            if not lens.get("data_view"):
                failures.append(f"  {title!r}: lens panel missing data_view")
            for metric in lens.get("metrics") or []:
                if not metric.get("aggregation"):
                    failures.append(f"  {title!r}: lens metric missing aggregation")
                if not metric.get("field"):
                    failures.append(f"  {title!r}: lens metric missing field")
            primary = lens.get("primary")
            if isinstance(primary, dict):
                if not primary.get("aggregation"):
                    failures.append(f"  {title!r}: lens primary metric missing aggregation")
                if not primary.get("field"):
                    failures.append(f"  {title!r}: lens primary metric missing field")
        if failures:
            self.fail(f"{path.name}: {len(failures)} lens contract issue(s):\n" + "\n".join(failures))


class TestDatadogYAMLSnapshots(unittest.TestCase):
    def _run_snapshot(self, path: pathlib.Path) -> None:
        actual = _snapshot_text(path)
        snap_path = _SNAPSHOT_DIR / f"{_dashboard_id(path)}.txt"
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        if UPDATE_SNAPSHOTS or not snap_path.exists():
            snap_path.write_text(actual, encoding="utf-8")
            if not UPDATE_SNAPSHOTS:
                self.fail(
                    f"Created new snapshot for {path.name}. "
                    "Run again with UPDATE_SNAPSHOTS=1 to accept it."
                )
            return
        expected = snap_path.read_text(encoding="utf-8")
        if actual != expected:
            self.fail(
                f"Snapshot mismatch for {path.name}.\n"
                "To update: UPDATE_SNAPSHOTS=1 pytest tests/test_datadog_yaml_generation.py\n"
                f"\n{_diff(expected, actual)}"
            )


def _make_structure_test(dashboard_path: pathlib.Path):
    def test_method(self):
        self._check_dashboard(dashboard_path)

    test_method.__name__ = f"test_{_dashboard_id(dashboard_path)}"
    test_method.__doc__ = f"All panels in {dashboard_path.name} have required YAML schema keys"
    return test_method


def _make_contract_test(dashboard_path: pathlib.Path):
    def test_method(self):
        self._check_dashboard(dashboard_path)

    test_method.__name__ = f"test_{_dashboard_id(dashboard_path)}"
    test_method.__doc__ = f"All ES|QL spec fields in {dashboard_path.name} exist in query output columns"
    return test_method


def _make_snapshot_test(dashboard_path: pathlib.Path):
    def test_method(self):
        self._run_snapshot(dashboard_path)

    test_method.__name__ = f"test_{_dashboard_id(dashboard_path)}"
    test_method.__doc__ = f"YAML shape snapshot for {dashboard_path.name}"
    return test_method


for _dashboard_path in DASHBOARD_FILES:
    _test_name = f"test_{_dashboard_id(_dashboard_path)}"
    setattr(TestDatadogYAMLStructure, _test_name, _make_structure_test(_dashboard_path))
    setattr(TestDatadogYAMLFieldContracts, _test_name, _make_contract_test(_dashboard_path))
    setattr(TestDatadogYAMLShapeInvariants, _test_name, _make_contract_test(_dashboard_path))
    setattr(TestDatadogYAMLLensContracts, _test_name, _make_contract_test(_dashboard_path))
    setattr(TestDatadogYAMLSnapshots, _test_name, _make_snapshot_test(_dashboard_path))
