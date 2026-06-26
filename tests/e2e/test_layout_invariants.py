# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Layout-invariant regression gate over the bundled real dashboards.

Migrates every shipped Grafana and Datadog dashboard and asserts the compiled
panel geometry is structurally sound *per section*:

  - no two panels overlap,
  - no panel runs past the 48-column grid (x + w <= 48) or has negative coords,
  - no panel is narrower than the 4-column readability floor.

Each Kibana section owns its own coordinate space (panels are positioned
relative to the section), so the checks are scoped per section — exactly how
Kibana renders them. These invariants are source-agnostic and catch layout
regressions (e.g. a single stat tile ballooning to full width, or a y-cursor
desync splitting a row into overlapping pieces) that query-level and schema
gates miss.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import generate_dashboard_yaml
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.adapters.source.grafana.panels import translate_dashboard
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAFANA_DIR = REPO_ROOT / "infra" / "grafana" / "dashboards"
DATADOG_DIR = REPO_ROOT / "infra" / "datadog" / "dashboards"

GRID_COLUMNS = 48
HARD_MIN_W = 4


def _sections(panels: list[dict]) -> list[list[dict]]:
    """Group leaf panels by their owning section (root panels form one group).

    Each section has an independent coordinate space, so geometry checks must be
    scoped to it. Sections are kept distinct by identity, not title (real
    dashboards routinely have several untitled sections that all start at y=0).
    """
    groups: list[list[dict]] = []
    root: list[dict] = []

    def walk(plist: list[dict], current: list[dict]) -> None:
        for panel in plist:
            section = panel.get("section")
            if isinstance(section, dict):
                inner: list[dict] = []
                groups.append(inner)
                walk(section.get("panels") or [], inner)
            else:
                current.append(panel)

    walk(panels, root)
    if root:
        groups.insert(0, root)
    return groups


def _rect(panel: dict) -> tuple[int, int, int, int]:
    pos = panel.get("position", {}) or {}
    size = panel.get("size", {}) or {}
    return (
        int(pos.get("x", 0) or 0),
        int(pos.get("y", 0) or 0),
        int(size.get("w", 0) or 0),
        int(size.get("h", 0) or 0),
    )


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def _layout_violations(yaml_doc: dict) -> list[str]:
    problems: list[str] = []
    dashboards = yaml_doc.get("dashboards") or []
    if not dashboards:
        return ["no dashboard emitted"]
    for section in _sections(dashboards[0].get("panels") or []):
        rects: list[tuple[str, tuple[int, int, int, int]]] = []
        for panel in section:
            title = panel.get("title", "?")
            x, y, w, h = _rect(panel)
            if w <= 0 or h <= 0:
                problems.append(f"'{title}': non-positive size w={w} h={h}")
            if x < 0 or y < 0:
                problems.append(f"'{title}': negative coord x={x} y={y}")
            if x + w > GRID_COLUMNS:
                problems.append(f"'{title}': overflow x+w={x + w} > {GRID_COLUMNS}")
            if 0 < w < HARD_MIN_W:
                problems.append(f"'{title}': width {w} below {HARD_MIN_W}")
            rects.append((title, (x, y, w, h)))
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                if _overlaps(rects[i][1], rects[j][1]):
                    problems.append(
                        f"overlap '{rects[i][0]}'{rects[i][1]} vs '{rects[j][0]}'{rects[j][1]}"
                    )
    return problems


def _iter_datadog_widgets(widgets: list) -> list:
    ordered: list = []
    for widget in widgets or []:
        ordered.append(widget)
        ordered.extend(_iter_datadog_widgets(getattr(widget, "children", []) or []))
    return ordered


class LayoutInvariantTests(unittest.TestCase):
    def test_self_check_detects_overlap_and_overflow(self):
        # Guards the gate itself: a hand-built broken layout must be flagged,
        # so a future "0 violations" result can be trusted as real.
        bad = {"dashboards": [{"panels": [
            {"title": "A", "position": {"x": 0, "y": 0}, "size": {"w": 24, "h": 12}},
            {"title": "B", "position": {"x": 10, "y": 0}, "size": {"w": 24, "h": 12}},
            {"title": "C", "position": {"x": 0, "y": 20}, "size": {"w": 60, "h": 12}},
        ]}]}
        problems = _layout_violations(bad)
        self.assertTrue(any("overlap" in p for p in problems), problems)
        self.assertTrue(any("overflow" in p for p in problems), problems)

    def test_grafana_dashboards_have_sound_layout(self):
        rule_pack = RulePackConfig()
        resolver = SchemaResolver(rule_pack)
        import tempfile

        for path in sorted(GRAFANA_DIR.glob("*.json")):
            with self.subTest(dashboard=path.name):
                dashboard = json.loads(path.read_text(encoding="utf-8"))
                with tempfile.TemporaryDirectory() as td:
                    _result, yaml_path = translate_dashboard(
                        dashboard, Path(td),
                        datasource_index="metrics-*", esql_index="metrics-*",
                        rule_pack=rule_pack, resolver=resolver,
                    )
                    yaml_doc = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8"))
                problems = _layout_violations(yaml_doc)
                self.assertEqual(problems, [], f"{path.name}: {problems}")

    def test_datadog_dashboards_have_sound_layout(self):
        files = [DATADOG_DIR / "sample_dashboard.json"] + sorted(
            (DATADOG_DIR / "integrations").glob("*.json")
        )
        for path in files:
            with self.subTest(dashboard=path.name):
                raw = json.loads(path.read_text(encoding="utf-8"))
                normalized = normalize_dashboard(raw)
                widgets = _iter_datadog_widgets(normalized.widgets)
                results = [translate_widget(w, plan_widget(w), OTEL_PROFILE) for w in widgets]
                yaml_str = generate_dashboard_yaml(
                    normalized, results,
                    data_view=OTEL_PROFILE.metric_index,
                    logs_index=OTEL_PROFILE.logs_index,
                    field_map=OTEL_PROFILE,
                )
                problems = _layout_violations(yaml.safe_load(yaml_str))
                self.assertEqual(problems, [], f"{path.name}: {problems}")


if __name__ == "__main__":
    unittest.main()
