# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""YAML generation tests for the Grafana → Kibana migration pipeline.

Three complementary test classes:

1. TestGrafanaYAMLStructure
   Translates every workable panel in each real dashboard file and asserts
   that the required YAML schema keys are present for each chart type.
   Run against the full dashboard corpus so structural gaps surface early.

2. TestGrafanaYAMLFieldContracts
   For each migrated panel, checks that every field name referenced in the
   YAML spec (dimension, metrics, breakdowns, primary, metric) actually
   appears in the query's final output columns.  Uses a pipeline-tracking
   helper that follows STATS → EVAL → KEEP/DROP so EVAL-aliased fields are
   not reported as missing.  Skips native-PROMQL queries (they use the
   PROMQL() function and column names are determined at runtime).

3. TestGrafanaYAMLSnapshots
   Captures a compact snapshot of each panel's YAML shape (chart type +
   spec fields + status) for the diverse-panels-test.json dashboard.
   Running with UPDATE_SNAPSHOTS=1 regenerates golden files; subsequent
   runs detect regressions.

Updating snapshots
------------------
    UPDATE_SNAPSHOTS=1 python -m pytest tests/test_grafana_yaml_generation.py -v

Review the diffs with ``git diff tests/snapshots/grafana_yaml/`` before
committing.
"""

from __future__ import annotations

import difflib
import json
import os
import pathlib
import unittest
from typing import Any

from observability_migration.adapters.source.grafana.panels import (
    SKIP_PANEL_TYPES,
    _flatten_dashboard_panels,
    translate_panel,
)
from observability_migration.targets.kibana.emit.esql_utils import (
    split_esql_pipeline,
    split_top_level_assignment,
    split_top_level_keyword,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_DASHBOARD_DIR = _REPO_ROOT / "infra" / "grafana" / "dashboards"
_SNAPSHOT_DIR = pathlib.Path(__file__).parent / "snapshots" / "grafana_yaml"
UPDATE_SNAPSHOTS = os.environ.get("UPDATE_SNAPSHOTS") == "1"

DASHBOARD_FILES: list[pathlib.Path] = sorted(_DASHBOARD_DIR.glob("*.json"))

# Required YAML schema keys for each ES|QL chart type.
REQUIRED_KEYS: dict[str, list[str]] = {
    "line":      ["dimension", "metrics"],
    "bar":       ["dimension", "metrics"],
    "area":      ["dimension", "metrics"],
    "metric":    ["primary"],
    "gauge":     ["metric"],
    "datatable": ["metrics"],
    "pie":       ["metrics", "breakdowns"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_dashboard(path: pathlib.Path) -> dict:
    with path.open() as fh:
        return json.load(fh)


def _workable_panels(dashboard: dict) -> list[dict]:
    """Return panels that are translated (not skipped rows/row-headers)."""
    flat = _flatten_dashboard_panels(dashboard)
    return [p for p in flat if p.get("type") not in SKIP_PANEL_TYPES and p.get("type") != "row"]


def _split_csv_top_level(text: str) -> list[str]:
    """Split on commas at depth-0 (parens + brackets), stripping whitespace."""
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


def _final_output_columns(query: str) -> set[str]:
    """Return the column names emitted by the last stage of an ES|QL pipeline.

    Walks STATS (resets columns), EVAL (adds aliases), KEEP (restricts to
    listed columns), and DROP (removes listed columns).  Native-PROMQL
    queries (those using the PROMQL() function command) return an empty set
    so callers can skip the contract check.
    """
    commands = split_esql_pipeline(query)
    if not commands:
        return set()

    # Native PROMQL queries use a `PROMQL(...)` command — column names are
    # runtime-determined, so we cannot validate them statically.
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
                    cols.add(alias)
            for part in _split_csv_top_level(by_text):
                alias, expr = split_top_level_assignment(part)
                field = alias or (expr or "").strip()
                if field:
                    cols.add(field)
        elif cl.startswith("eval "):
            for part in _split_csv_top_level(cmd[5:].strip()):
                alias, _ = split_top_level_assignment(part)
                if alias:
                    cols.add(alias)
        elif cl.startswith("keep "):
            fields = {f.strip() for f in _split_csv_top_level(cmd[5:].strip()) if f.strip()}
            cols = fields
        elif cl.startswith("drop "):
            fields = {f.strip() for f in _split_csv_top_level(cmd[5:].strip()) if f.strip()}
            cols -= fields
    return cols


def _spec_fields(esql_block: dict) -> set[str]:
    """Collect all field names referenced in a YAML esql spec block."""
    fields: set[str] = set()

    def _add(v: Any) -> None:
        if isinstance(v, dict):
            f = v.get("field")
            if f:
                fields.add(f)
        elif isinstance(v, str) and v:
            fields.add(v)

    _add(esql_block.get("dimension"))
    _add(esql_block.get("breakdown"))
    _add(esql_block.get("primary"))
    _add(esql_block.get("metric"))
    for item in esql_block.get("metrics", []):
        _add(item)
    for item in esql_block.get("breakdowns", []):
        _add(item)
    # gauge-injected constant columns (_gauge_min etc.) — ignore these
    fields.discard("_gauge_min")
    fields.discard("_gauge_max")
    fields.discard("_gauge_goal")
    return fields


def _snapshot_text(title: str, grafana_type: str, result: Any, esql_block: dict) -> str:
    """Render a compact, human-readable snapshot of one panel's YAML shape."""
    lines = [
        f"title: {title}",
        f"grafana_type: {grafana_type}",
        f"status: {result.status}",
        f"chart_type: {esql_block.get('type', 'none')}",
    ]
    if "dimension" in esql_block:
        d = esql_block["dimension"]
        lines.append(f"dimension: {d.get('field') if isinstance(d, dict) else d}")
    if "metrics" in esql_block:
        lines.append(f"metrics: {[m.get('field') for m in esql_block['metrics']]}")
    if "breakdown" in esql_block:
        b = esql_block["breakdown"]
        lines.append(f"breakdown: {b.get('field') if isinstance(b, dict) else b}")
    if "breakdowns" in esql_block:
        lines.append(f"breakdowns: {[b.get('field') for b in esql_block['breakdowns']]}")
    if "primary" in esql_block:
        p = esql_block["primary"]
        lines.append(f"primary: {p.get('field') if isinstance(p, dict) else p}")
    if "metric" in esql_block:
        m = esql_block["metric"]
        lines.append(f"metric: {m.get('field') if isinstance(m, dict) else m}")
    if result.kibana_type == "markdown":
        lines.append("chart_type: markdown")
    for w in getattr(result, "reasons", []):
        lines.append(f"warning: {w}")
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


# ---------------------------------------------------------------------------
# Test class 1: structural schema validation
# ---------------------------------------------------------------------------

class TestGrafanaYAMLStructure(unittest.TestCase):
    """Every migrated panel in every real dashboard must have the required
    YAML keys for its chart type.  One test method per dashboard file."""

    def _check_dashboard(self, path: pathlib.Path) -> None:
        dash = _load_dashboard(path)
        panels = _workable_panels(dash)
        failures: list[str] = []

        for panel in panels:
            yp, result = translate_panel(panel)
            if result.status not in ("migrated", "migrated_with_warnings"):
                continue
            if not yp:
                continue
            esql = yp.get("esql", {})
            ct = esql.get("type")
            if not ct:
                continue  # markdown/text panels have no esql block
            reqs = REQUIRED_KEYS.get(ct, [])
            missing = [k for k in reqs if k not in esql]
            if missing:
                failures.append(
                    f"  {panel.get('title')!r} ({ct}): missing required key(s) {missing}"
                )

        if failures:
            self.fail(
                f"{path.name}: {len(failures)} structural issue(s):\n" + "\n".join(failures)
            )


def _make_structure_test(dashboard_path: pathlib.Path):
    def test_method(self):
        self._check_dashboard(dashboard_path)
    test_method.__name__ = f"test_{dashboard_path.stem.replace('-', '_').replace('.', '_')}"
    test_method.__doc__ = f"All panels in {dashboard_path.name} have required YAML schema keys"
    return test_method


for _dp in DASHBOARD_FILES:
    setattr(TestGrafanaYAMLStructure, f"test_{_dp.stem.replace('-', '_')}", _make_structure_test(_dp))


# ---------------------------------------------------------------------------
# Test class 2: field-reference contract
# ---------------------------------------------------------------------------

class TestGrafanaYAMLFieldContracts(unittest.TestCase):
    """Field names referenced in the YAML spec (dimension, metrics, etc.)
    must exist in the query's actual output columns.  Native-PROMQL queries
    are skipped since their column names are runtime-determined.

    One test method per dashboard file."""

    def _check_dashboard(self, path: pathlib.Path) -> None:
        dash = _load_dashboard(path)
        panels = _workable_panels(dash)
        failures: list[str] = []

        for panel in panels:
            yp, result = translate_panel(panel)
            if result.status not in ("migrated", "migrated_with_warnings"):
                continue
            if not yp:
                continue
            esql = yp.get("esql", {})
            ct = esql.get("type")
            if not ct:
                continue
            query = esql.get("query", "")
            output_cols = _final_output_columns(query)
            if not output_cols:
                continue  # native PROMQL or unknown — skip

            spec_flds = _spec_fields(esql)
            missing = spec_flds - output_cols
            if missing:
                failures.append(
                    f"  {panel.get('title')!r} ({ct}): "
                    f"field(s) {sorted(missing)} referenced in spec but absent from query output "
                    f"{sorted(output_cols)}"
                )

        if failures:
            self.fail(
                f"{path.name}: {len(failures)} field contract violation(s):\n" + "\n".join(failures)
            )


def _make_contract_test(dashboard_path: pathlib.Path):
    def test_method(self):
        self._check_dashboard(dashboard_path)
    test_method.__name__ = f"test_{dashboard_path.stem.replace('-', '_')}"
    test_method.__doc__ = f"All spec fields in {dashboard_path.name} exist in query output columns"
    return test_method


for _dp in DASHBOARD_FILES:
    setattr(TestGrafanaYAMLFieldContracts, f"test_{_dp.stem.replace('-', '_')}", _make_contract_test(_dp))


# ---------------------------------------------------------------------------
# Test class 2b: instant / single-value panel regression (issue #127)
# ---------------------------------------------------------------------------

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
    """Regression for issue #127.

    A panel whose translated ES|QL collapses to a single row (no time
    dimension, no group columns) must never be emitted as an XY chart whose
    ``dimension`` (x-axis / xAccessor) references a ``time_bucket`` column the
    query does not output. Such queries must degrade to a single-value
    visualization. Exercised on the legacy ES|QL path (the default for
    ``translate_panel``), which is where the phantom dimension was injected.
    """

    def _assert_no_phantom_dimension(self, panel: dict) -> None:
        yp, result = translate_panel(panel)
        self.assertIn(result.status, ("migrated", "migrated_with_warnings"))
        esql = yp.get("esql", {})
        ct = esql.get("type")
        self.assertTrue(ct, f"panel produced no esql block: {result.status}")
        query = esql.get("query", "")
        output_cols = _final_output_columns(query)
        # Legacy ES|QL (not native PROMQL): output columns are statically known.
        self.assertTrue(output_cols, "expected legacy ES|QL with static columns")
        spec_flds = _spec_fields(esql)
        missing = spec_flds - output_cols
        self.assertFalse(
            missing,
            f"{panel.get('title')!r} ({ct}): spec field(s) {sorted(missing)} "
            f"absent from query output {sorted(output_cols)}; query={query!r}",
        )
        self.assertNotIn(
            "time_bucket",
            spec_flds,
            f"{panel.get('title')!r} ({ct}): phantom time_bucket dimension emitted",
        )

    def test_stat_instant_uptime_maps_to_single_value(self):
        self._assert_no_phantom_dimension(_instant_panel("stat"))

    def test_gauge_instant_uptime_maps_to_single_value(self):
        self._assert_no_phantom_dimension(_instant_panel("gauge"))

    def test_timeseries_with_instant_query_degrades_to_metric(self):
        panel = _instant_panel("timeseries")
        self._assert_no_phantom_dimension(panel)
        yp, _ = translate_panel(panel)
        # A line chart cannot plot a single value with no x-axis; it must
        # degrade to a metric visualization rather than invent a time axis.
        self.assertEqual(yp["esql"]["type"], "metric")


# ---------------------------------------------------------------------------
# Test class 3: YAML shape snapshots for diverse-panels-test.json
# ---------------------------------------------------------------------------

_DIVERSE_PANELS_PATH = _DASHBOARD_DIR / "diverse-panels-test.json"


class TestGrafanaYAMLSnapshots(unittest.TestCase):
    """Snapshot tests for diverse-panels-test.json — one panel of each chart
    type.  Captures chart type, spec field names, and migration status.

    To regenerate:
        UPDATE_SNAPSHOTS=1 python -m pytest tests/test_grafana_yaml_generation.py::TestGrafanaYAMLSnapshots -v
    """

    @classmethod
    def setUpClass(cls):
        dash = _load_dashboard(_DIVERSE_PANELS_PATH)
        cls._panels: dict[str, tuple[dict | None, Any]] = {}
        for panel in _workable_panels(dash):
            title = panel.get("title", "untitled")
            cls._panels[title] = (panel, *translate_panel(panel))

    def _slug(self, title: str) -> str:
        return title.lower().replace(" ", "_").replace("/", "_").replace("(", "").replace(")", "")

    def _run_snapshot(self, title: str) -> None:
        panel, yp, result = self._panels[title]
        esql_block = (yp or {}).get("esql", {}) if yp else {}
        actual = _snapshot_text(title, panel.get("type", ""), result, esql_block)

        snap_dir = _SNAPSHOT_DIR / "diverse-panels-test"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_path = snap_dir / f"{self._slug(title)}.txt"

        if UPDATE_SNAPSHOTS or not snap_path.exists():
            snap_path.write_text(actual, encoding="utf-8")
            if not UPDATE_SNAPSHOTS:
                self.fail(
                    f"Created new snapshot for '{title}'. "
                    "Run again (or with UPDATE_SNAPSHOTS=1) to pass."
                )
            return

        expected = snap_path.read_text(encoding="utf-8")
        if actual != expected:
            diff = _diff(expected, actual)
            self.fail(
                f"Snapshot mismatch for '{title}'.\n"
                f"To update: UPDATE_SNAPSHOTS=1 pytest tests/test_grafana_yaml_generation.py\n"
                f"\n{diff}"
            )


def _make_snapshot_test(title: str):
    def test_method(self):
        self._run_snapshot(title)
    slug = title.lower().replace(" ", "_").replace("/", "_").replace("(", "").replace(")", "")
    test_method.__name__ = f"test_{slug}"
    test_method.__doc__ = f"YAML shape snapshot for panel '{title}'"
    return test_method


_SNAPSHOT_PANELS = [
    "Request Latency Heatmap",
    "Traffic Distribution",
    "Top Endpoints",
    "CPU Usage",
    "Memory Usage",
    "Uptime",
    "Disk Usage per Mount",
    "Active Alerts",
    "Notes",
    "Application Logs",
]

for _title in _SNAPSHOT_PANELS:
    setattr(TestGrafanaYAMLSnapshots, f"test_{_title.lower().replace(' ', '_').replace('/', '_').replace('(', '').replace(')', '')}", _make_snapshot_test(_title))
