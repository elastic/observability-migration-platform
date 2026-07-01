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
   Captures a compact snapshot of each panel's YAML shape for the
   diverse-panels-test.json dashboard: chart type, spec field names, status,
   and the visual-fidelity attributes that change how a panel *looks* even
   when the numbers are right (stacking mode, axis title/bounds, gauge shape,
   gauge colour range/thresholds — issue #224).
   TestGrafanaControlsSnapshot does the same for the dashboard's controls
   (type, resolved field, multiple), translated through the metric-aware path
   so the snapshot freezes the fixed field (`service.instance.id`).
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
import re
import tempfile
import unittest
from functools import cache
from types import SimpleNamespace
from typing import Any

import yaml

from observability_migration.adapters.source.grafana.panels import (
    SKIP_PANEL_TYPES,
    _extract_variable_source_field,
    _flatten_dashboard_panels,
    _variable_query_text,
    translate_dashboard,
    translate_panel,
)
from observability_migration.targets.kibana.compile import _iter_leaf_panels
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
    # Visual-fidelity attributes (issue #224): these change how a panel *looks*
    # even when the numeric spec is identical, so a regression here would
    # otherwise pass the field-name-only checks above silently.
    if "mode" in esql_block:
        lines.append(f"mode: {esql_block['mode']}")
    appearance = esql_block.get("appearance")
    if isinstance(appearance, dict):
        if "shape" in appearance:
            lines.append(f"gauge_shape: {appearance['shape']}")
        axis = appearance.get("y_left_axis")
        if isinstance(axis, dict):
            if "title" in axis:
                lines.append(f"axis_title: {axis['title']}")
            extent = axis.get("extent")
            if isinstance(extent, dict):
                lines.append(
                    f"axis_extent: {extent.get('mode')} "
                    f"[{extent.get('min')}, {extent.get('max')}]"
                )
    color = esql_block.get("color")
    if isinstance(color, dict):
        if "range_min" in color or "range_max" in color:
            lines.append(f"gauge_range: [{color.get('range_min')}, {color.get('range_max')}]")
        thresholds = color.get("thresholds")
        if thresholds:
            rendered = ", ".join(
                f"{t.get('up_to')}:{t.get('color')}" for t in thresholds
            )
            lines.append(f"gauge_thresholds: {rendered}")
    if result.kibana_type == "markdown":
        lines.append("chart_type: markdown")
    for w in getattr(result, "reasons", []):
        lines.append(f"warning: {w}")
    return "\n".join(lines) + "\n"


def _controls_snapshot_text(controls: list[dict]) -> str:
    """Render a compact snapshot of a dashboard's controls (issue #224).

    One line per control capturing its ``type``, resolved ``field``, and
    ``multiple`` flag — the dropdown/variable fidelity the per-panel snapshots
    don't see.
    """
    if not controls:
        return "(no controls)\n"
    lines = [
        f"control: type={c.get('type')} field={c.get('field')} multiple={c.get('multiple')}"
        for c in controls
    ]
    return "\n".join(lines) + "\n"


def _snapshot_dashboard_id(path: pathlib.Path) -> str:
    """Return the snapshot directory name for a Grafana dashboard fixture."""
    rel = path.relative_to(_DASHBOARD_DIR).with_suffix("")
    return "__".join(rel.parts)


def _snapshot_dashboard_paths() -> list[pathlib.Path]:
    """Dashboard fixtures that own Grafana YAML snapshots."""
    return list(DASHBOARD_FILES)


def _snapshot_slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "_", text.lower()).strip("._-")
    return slug or "untitled"


def _snapshot_panel_path(
    dashboard_path: pathlib.Path,
    title: str,
    used_slugs: dict[str, int],
) -> pathlib.Path:
    slug = _snapshot_slug(title)
    count = used_slugs.get(slug, 0) + 1
    used_slugs[slug] = count
    if count > 1:
        slug = f"{slug}__{count}"
    return _SNAPSHOT_DIR / _snapshot_dashboard_id(dashboard_path) / f"{slug}.txt"


def _snapshot_rule_pack_and_resolver():
    """Return an offline resolver for deterministic dashboard snapshots.

    The field cache is intentionally narrow. It advertises only the label
    fields needed by fixture controls plus the `up` metric/co-occurrence pair
    that proves the metric-aware `instance` -> `service.instance.id` path.
    Other metric-scoped controls fall back offline without an ES probe.
    """
    from observability_migration.adapters.source.grafana.rules import RulePackConfig
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    label_fields = {"instance", "service.instance.id"}
    for dashboard_path in _snapshot_dashboard_paths():
        dashboard = _load_dashboard(dashboard_path)
        for variable in dashboard.get("templating", {}).get("list", []) or []:
            query_text = _variable_query_text(variable)
            field = _extract_variable_source_field(query_text)
            if field:
                label_fields.add(field)

    keyword = {"keyword": {"type": "keyword", "aggregatable": True, "searchable": True}}
    resolver._discovery_attempted = True
    resolver._discovery_status = "offline"
    resolver._discovery_error = ""
    resolver._field_cache = {field: keyword for field in sorted(label_fields)}
    resolver._field_cache["up"] = {"double": {"type": "double", "aggregatable": True}}
    resolver._cooccurrence_cache = {
        ("up", "instance"): False,
        ("up", "service.instance.id"): True,
    }
    resolver.resolve_metric_field = lambda name, **kw: name
    return rule_pack, resolver


@cache
def _render_snapshot_dashboard(path: pathlib.Path) -> tuple[dict[str, Any], tuple[Any, ...]]:
    """Translate a dashboard fixture through the dashboard-level YAML path."""
    dashboard = _load_dashboard(path)
    rule_pack, resolver = _snapshot_rule_pack_and_resolver()
    with tempfile.TemporaryDirectory() as tmpdir:
        result, yaml_path = translate_dashboard(
            dashboard,
            pathlib.Path(tmpdir),
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

    rendered = (payload.get("dashboards") or [{}])[0]
    leaf_panels = list(_iter_leaf_panels(rendered.get("panels") or []))
    panel_results = tuple(result.yaml_panel_results)
    if len(leaf_panels) != len(panel_results):
        raise AssertionError(
            f"{path.name}: rendered {len(leaf_panels)} leaf panel(s), "
            f"but migration result has {len(panel_results)} emitted panel result(s)"
        )
    return rendered, panel_results


def _dashboard_snapshot_texts(path: pathlib.Path) -> dict[pathlib.Path, str]:
    rendered, panel_results = _render_snapshot_dashboard(path)
    leaf_panels = list(_iter_leaf_panels(rendered.get("panels") or []))
    used_slugs: dict[str, int] = {}
    snapshots: dict[pathlib.Path, str] = {}

    for panel, result in zip(leaf_panels, panel_results):
        title = str(panel.get("title") or getattr(result, "title", "") or "untitled")
        esql_block = panel.get("esql", {}) if isinstance(panel.get("esql"), dict) else {}
        snapshot_path = _snapshot_panel_path(path, title, used_slugs)
        snapshots[snapshot_path] = _snapshot_text(
            title,
            str(getattr(result, "grafana_type", "")),
            result,
            esql_block,
        )

    controls_path = _SNAPSHOT_DIR / _snapshot_dashboard_id(path) / "_controls.txt"
    snapshots[controls_path] = _controls_snapshot_text(rendered.get("controls") or [])
    return snapshots


def _expected_snapshot_paths() -> set[pathlib.Path]:
    paths: set[pathlib.Path] = set()
    for dashboard_path in _snapshot_dashboard_paths():
        paths.update(_dashboard_snapshot_texts(dashboard_path))
    return paths


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
# Test class 2c: snapshot extractor renders visual-fidelity attributes (#224)
# ---------------------------------------------------------------------------

def _fake_result(status="migrated", kibana_type="lens", reasons=()):
    return SimpleNamespace(status=status, kibana_type=kibana_type, reasons=list(reasons))


class TestSnapshotExtractorVisualFidelity(unittest.TestCase):
    """`_snapshot_text` must capture attributes that change how a panel *looks*
    even when the numbers are right (issue #224): stacking mode, axis title and
    bounds, gauge shape, and gauge colour range + thresholds.
    """

    def test_stacking_mode_is_captured(self):
        block = {"type": "bar", "mode": "stacked"}
        text = _snapshot_text("Bar", "barchart", _fake_result(), block)
        self.assertIn("mode: stacked", text)

    def test_axis_title_is_captured(self):
        block = {"type": "line", "appearance": {"y_left_axis": {"title": "CPU %"}}}
        text = _snapshot_text("Line", "timeseries", _fake_result(), block)
        self.assertIn("axis_title: CPU %", text)

    def test_axis_bounds_are_captured(self):
        block = {
            "type": "line",
            "appearance": {"y_left_axis": {"extent": {"mode": "custom", "min": 0.0, "max": 100.0}}},
        }
        text = _snapshot_text("Line", "timeseries", _fake_result(), block)
        self.assertIn("axis_extent: custom [0.0, 100.0]", text)

    def test_gauge_shape_is_captured(self):
        block = {"type": "gauge", "appearance": {"shape": "arc"}}
        text = _snapshot_text("Gauge", "gauge", _fake_result(), block)
        self.assertIn("gauge_shape: arc", text)

    def test_gauge_color_range_is_captured(self):
        block = {"type": "gauge", "color": {"range_min": 0, "range_max": 100}}
        text = _snapshot_text("Gauge", "gauge", _fake_result(), block)
        self.assertIn("gauge_range: [0, 100]", text)

    def test_gauge_color_thresholds_are_captured(self):
        block = {
            "type": "gauge",
            "color": {
                "thresholds": [
                    {"up_to": 70, "color": "#54B399"},
                    {"up_to": 90, "color": "#D6BF57"},
                ]
            },
        }
        text = _snapshot_text("Gauge", "gauge", _fake_result(), block)
        self.assertIn("gauge_thresholds: 70:#54B399, 90:#D6BF57", text)

    def test_absent_attributes_emit_no_lines(self):
        block = {"type": "datatable", "metrics": [{"field": "ALERTS"}]}
        text = _snapshot_text("Table", "table", _fake_result(), block)
        for key in ("mode:", "axis_title:", "axis_extent:", "gauge_shape:", "gauge_range:", "gauge_thresholds:"):
            self.assertNotIn(key, text)


# ---------------------------------------------------------------------------
# Test class 2d: controls snapshot extractor (#224)
# ---------------------------------------------------------------------------

class TestControlsSnapshotExtractor(unittest.TestCase):
    """`_controls_snapshot_text` freezes each dashboard control's type, resolved
    field, and multiple flag (issue #224) — the dropdowns the panel snapshots
    don't cover."""

    def test_renders_type_field_and_multiple_per_control(self):
        controls = [
            {"type": "options", "field": "service.instance.id", "multiple": True},
            {"type": "options", "field": "service.name", "multiple": False},
        ]
        text = _controls_snapshot_text(controls)
        self.assertEqual(
            text,
            "control: type=options field=service.instance.id multiple=True\n"
            "control: type=options field=service.name multiple=False\n",
        )

    def test_empty_controls_render_marker(self):
        self.assertEqual(_controls_snapshot_text([]), "(no controls)\n")


# ---------------------------------------------------------------------------
# Test class 3: snapshot ownership for all Grafana dashboard fixtures (#250)
# ---------------------------------------------------------------------------

@unittest.skipIf(UPDATE_SNAPSHOTS, "snapshot ownership is checked after regeneration")
class TestGrafanaYAMLSnapshotCoverage(unittest.TestCase):
    """Snapshot fixtures and golden files must stay in lockstep.

    Adding a Grafana dashboard JSON fixture should create an owned snapshot
    directory, and every golden file should be produced by the discovered
    dashboard-level snapshot harness.
    """

    def test_snapshot_dashboard_directories_match_fixture_files(self):
        expected = {_snapshot_dashboard_id(path) for path in _snapshot_dashboard_paths()}
        actual = {
            path.name
            for path in _SNAPSHOT_DIR.iterdir()
            if path.is_dir()
        }
        self.assertEqual(actual, expected)

    def test_snapshot_files_match_rendered_dashboards(self):
        expected = {
            path.relative_to(_SNAPSHOT_DIR)
            for path in _expected_snapshot_paths()
        }
        actual = {
            path.relative_to(_SNAPSHOT_DIR)
            for path in _SNAPSHOT_DIR.glob("**/*.txt")
        }
        self.assertEqual(actual, expected)


class TestGrafanaYAMLSnapshots(unittest.TestCase):
    """Dashboard-discovered YAML shape snapshots for Grafana fixtures.

    Each dashboard fixture owns a snapshot directory. The harness renders the
    dashboard through ``translate_dashboard`` once, then snapshots every emitted
    leaf panel plus the dashboard controls from that same modelled target.

    To regenerate:
        UPDATE_SNAPSHOTS=1 python -m pytest tests/test_grafana_yaml_generation.py::TestGrafanaYAMLSnapshots -v
    """

    def _run_snapshot(self, dashboard_path: pathlib.Path) -> None:
        for snap_path, actual in _dashboard_snapshot_texts(dashboard_path).items():
            with self.subTest(snapshot=str(snap_path.relative_to(_SNAPSHOT_DIR))):
                snap_path.parent.mkdir(parents=True, exist_ok=True)
                if UPDATE_SNAPSHOTS or not snap_path.exists():
                    snap_path.write_text(actual, encoding="utf-8")
                    if not UPDATE_SNAPSHOTS:
                        self.fail(
                            f"Created new snapshot {snap_path.relative_to(_SNAPSHOT_DIR)}. "
                            "Run again (or with UPDATE_SNAPSHOTS=1) to pass."
                        )
                    continue

                expected = snap_path.read_text(encoding="utf-8")
                if actual != expected:
                    self.fail(
                        f"Snapshot mismatch for {snap_path.relative_to(_SNAPSHOT_DIR)}.\n"
                        "To update: UPDATE_SNAPSHOTS=1 pytest tests/test_grafana_yaml_generation.py\n"
                        f"\n{_diff(expected, actual)}"
                    )


def _make_snapshot_test(dashboard_path: pathlib.Path):
    def test_method(self):
        self._run_snapshot(dashboard_path)

    test_method.__name__ = f"test_{_snapshot_dashboard_id(dashboard_path).replace('-', '_')}"
    test_method.__doc__ = f"YAML shape snapshots for {dashboard_path.name}"
    return test_method


for _dashboard_path in _snapshot_dashboard_paths():
    _test_name = f"test_{_snapshot_dashboard_id(_dashboard_path).replace('-', '_')}"
    setattr(TestGrafanaYAMLSnapshots, _test_name, _make_snapshot_test(_dashboard_path))
