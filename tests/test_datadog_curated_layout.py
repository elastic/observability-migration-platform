# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Tests for Datadog curated layout packs."""
import json
from pathlib import Path

import yaml
from e2e.test_layout_invariants import _iter_datadog_widgets, _layout_violations

from observability_migration.adapters.source.datadog.curated_packs import load_curated_pack
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import (
    _apply_curated_layout,
    _curated_spec_candidates,
    _iter_leaf_panels,
    generate_dashboard_yaml,
)
from observability_migration.adapters.source.datadog.models import TranslationResult
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget

REPO_ROOT = Path(__file__).resolve().parents[1]
REDIS_SOURCE = REPO_ROOT / "infra" / "datadog" / "dashboards" / "integrations" / "redis.json"


def _generate_redis_dashboard() -> tuple[dict, list[TranslationResult]]:
    """Migrate the shipped Redis dashboard and return (yaml doc, panel results).

    Coverage has to be asserted against the *generated* document: the generator
    de-duplicates and back-fills panel titles before a curated pack is applied,
    so a pack keyed on Datadog source titles can look complete on paper and still
    match nothing.
    """
    raw = json.loads(REDIS_SOURCE.read_text(encoding="utf-8"))
    normalized = normalize_dashboard(raw)
    widgets = _iter_datadog_widgets(normalized.widgets)
    results = [translate_widget(w, plan_widget(w), OTEL_PROFILE) for w in widgets]
    yaml_str = generate_dashboard_yaml(
        normalized,
        results,
        data_view=OTEL_PROFILE.metric_index,
        logs_index=OTEL_PROFILE.logs_index,
        field_map=OTEL_PROFILE,
    )
    return yaml.safe_load(yaml_str), results


def _generated_sections(doc: dict) -> dict[str, list[dict]]:
    return {
        panel.get("title", ""): (panel["section"].get("panels") or [])
        for panel in doc["dashboards"][0].get("panels", [])
        if isinstance(panel.get("section"), dict)
    }


def test_load_redis_overview_pack():
    pack = load_curated_pack("Redis - Overview")
    assert pack is not None
    assert "sections" in pack
    titles = [s["title"] for s in pack["sections"]]
    assert "Overview" in titles
    assert "Performance Metrics" in titles


def test_no_pack_for_unknown_dashboard():
    assert load_curated_pack("Some Random Dashboard XYZ") is None


def test_redis_overview_overview_section_layout():
    pack = load_curated_pack("Redis - Overview")
    overview = next(s for s in pack["sections"] if s["title"] == "Overview")
    panels = {p["title"]: p for p in overview["panels"] if p.get("title")}
    # 4 stats should be in a single row at y=0
    assert panels["Hit rate"]["position"]["y"] == 0
    assert panels["Blocked clients"]["position"]["y"] == 0
    assert panels["Redis keyspace"]["position"]["y"] == 0
    assert panels["Unsaved changes"]["position"]["y"] == 0
    # They should span 12 cols each, covering the full 48
    widths = [panels[t]["size"]["w"] for t in ["Hit rate", "Blocked clients", "Redis keyspace", "Unsaved changes"]]
    assert sum(widths) == 48


def test_about_redis_is_collapsed():
    pack = load_curated_pack("Redis - Overview")
    about = next(s for s in pack["sections"] if s["title"] == "About Redis")
    assert about.get("collapsed") is True


def test_logs_section_side_by_side():
    pack = load_curated_pack("Redis - Overview")
    logs = next(s for s in pack["sections"] if s["title"] == "Logs")
    panels = {p["title"]: p for p in logs["panels"]}
    assert panels["Error Logs"]["size"]["w"] == 24
    assert panels["All Logs"]["size"]["w"] == 24
    assert panels["Error Logs"]["position"]["x"] == 0
    assert panels["All Logs"]["position"]["x"] == 24
    assert panels["Error Logs"]["position"]["y"] == panels["All Logs"]["position"]["y"]


def test_performance_metrics_layout():
    pack = load_curated_pack("Redis - Overview")
    perf = next(s for s in pack["sections"] if s["title"] == "Performance Metrics")
    named = {p["title"]: p for p in perf["panels"] if p.get("title")}
    # Latency chart is wide
    assert named["Latency by Host"]["size"]["w"] == 36
    assert named["Latency by Host"]["position"]["x"] == 0
    # CPU and replication delay are full width
    assert named["Average CPU usage"]["size"]["w"] == 48
    assert named["Average replication delay (offset)"]["size"]["w"] == 48


def test_shipped_redis_pack_covers_every_generated_panel():
    # A pack only moves the panels it matches, so partial coverage silently mixes
    # curated coordinates with auto-generated ones. Every declared section must
    # therefore account for every leaf panel of the real generated dashboard.
    doc, results = _generate_redis_dashboard()
    pack = load_curated_pack("Redis - Overview")
    sections = _generated_sections(doc)

    unmatched_specs: dict[str, list[dict]] = {}
    uncovered_panels: dict[str, list[str]] = {}
    for section_spec in pack["sections"]:
        section_title = section_spec["title"]
        assert section_title in sections, (
            f"pack declares section '{section_title}', generated sections: {sorted(sections)}"
        )
        sec_panels = sections[section_title]
        covered: set[int] = set()
        for entry in section_spec.get("panels", []):
            candidates = _curated_spec_candidates(sec_panels, entry)
            nth = int(entry.get("nth", 0))
            if nth >= len(candidates):
                unmatched_specs.setdefault(section_title, []).append(entry)
                continue
            covered.add(id(candidates[nth]))
        stranded = [
            str(leaf.get("title", ""))
            for leaf in _iter_leaf_panels(sec_panels)
            if id(leaf) not in covered
        ]
        if stranded:
            uncovered_panels[section_title] = stranded

    assert unmatched_specs == {}, f"pack specs that matched no panel: {unmatched_specs}"
    assert uncovered_panels == {}, f"generated panels no pack spec matched: {uncovered_panels}"
    # ...and the in-generator coverage guard stayed silent for the same run.
    guard_warnings = [w for r in results for w in r.warnings if "Curated layout pack" in w]
    assert guard_warnings == []


def test_generated_redis_dashboard_has_no_layout_overlaps():
    doc, _results = _generate_redis_dashboard()
    assert _layout_violations(doc) == []


def test_kind_selector_matches_markdown_regardless_of_generated_title():
    # Note titles are generated (widget-id or ordinal based) and unstable, so a
    # pack must be able to say "the nth markdown panel in this section".
    doc = {
        "dashboards": [
            {
                "name": "Kind Selector Dashboard",
                "panels": [
                    {
                        "title": "Sec",
                        "section": {
                            "collapsed": False,
                            "panels": [
                                {
                                    "title": "Datadog note 8013519185925578",
                                    "markdown": {"content": "first note"},
                                    "size": {"w": 16, "h": 10},
                                    "position": {"x": 0, "y": 0},
                                },
                                {
                                    "title": "Some chart",
                                    "esql": {"type": "line", "query": "FROM metrics-*"},
                                    "size": {"w": 16, "h": 10},
                                    "position": {"x": 8, "y": 0},
                                },
                                {
                                    "title": "Datadog note 18",
                                    "markdown": {"content": "second note"},
                                    "size": {"w": 16, "h": 10},
                                    "position": {"x": 16, "y": 0},
                                },
                            ],
                        },
                    }
                ],
            }
        ]
    }
    pack = {
        "sections": [
            {
                "title": "Sec",
                "panels": [
                    {"kind": "markdown", "nth": 0, "size": {"w": 24, "h": 8}, "position": {"x": 0, "y": 0}},
                    {"kind": "markdown", "nth": 1, "size": {"w": 24, "h": 8}, "position": {"x": 24, "y": 0}},
                    {"title": "Some chart", "size": {"w": 48, "h": 12}, "position": {"x": 0, "y": 8}},
                ],
            }
        ]
    }
    results = [
        TranslationResult(widget_id="a", title="Datadog note 8013519185925578"),
        TranslationResult(widget_id="b", title="Some chart"),
        TranslationResult(widget_id="c", title="Datadog note 18"),
    ]
    _apply_curated_layout(doc, pack, results)

    placed = {
        leaf["title"]: (leaf["position"]["x"], leaf["position"]["y"], leaf["size"]["w"], leaf["size"]["h"])
        for leaf in doc["dashboards"][0]["panels"][0]["section"]["panels"]
    }
    assert placed["Datadog note 8013519185925578"] == (0, 0, 24, 8)
    assert placed["Datadog note 18"] == (24, 0, 24, 8)
    assert placed["Some chart"] == (0, 8, 48, 12)
    # Full coverage means the safety net never fires.
    assert [w for r in results for w in r.warnings] == []
    assert _layout_violations(doc) == []


def test_incomplete_pack_warns_and_still_avoids_overlaps():
    # The safety net: a pack that stops matching a panel must be reported to the
    # operator and must not be able to emit an overlapping section.
    doc = {
        "dashboards": [
            {
                "name": "Redis - Overview",
                "panels": [
                    {
                        "title": "Performance Metrics",
                        "section": {
                            "collapsed": False,
                            "panels": [
                                {
                                    "title": "Latency by Host",
                                    "esql": {"type": "line", "query": "FROM metrics-*"},
                                    "size": {"w": 16, "h": 10},
                                    "position": {"x": 0, "y": 0},
                                },
                                {
                                    "title": "Datadog note 18",
                                    "markdown": {"content": "note"},
                                    "size": {"w": 16, "h": 10},
                                    "position": {"x": 8, "y": 0},
                                },
                            ],
                        },
                    }
                ],
            }
        ]
    }
    pack = {
        "sections": [
            {
                "title": "Performance Metrics",
                "panels": [
                    {"title": "Latency by Host", "size": {"w": 36, "h": 12}, "position": {"x": 0, "y": 0}},
                ],
            }
        ]
    }
    results = [
        TranslationResult(widget_id="a", title="Latency by Host"),
        TranslationResult(widget_id="b", title="Datadog note 18"),
    ]
    _apply_curated_layout(doc, pack, results)

    warnings = [w for r in results for w in r.warnings]
    assert any(
        "does not cover" in w
        and "Redis - Overview" in w
        and "Performance Metrics" in w
        and "Datadog note 18" in w
        for w in warnings
    ), warnings
    # The unmatched note kept its auto size but was pushed clear of the curated panel.
    assert _layout_violations(doc) == []


def test_curated_pack_yaml_ships_as_package_data():
    """Every curated_packs package carrying a pack.yaml must be declared in
    pyproject's package-data.

    Datadog packs are discovered by scanning this package's subdirectories for
    pack.yaml at runtime, so the YAML has to be inside the wheel. It was not:
    package-data declared the Grafana packs only, which made
    ``load_curated_pack`` return None for every pip install while still working
    from a repo checkout -- the curated layout silently did not apply for the
    operators the feature is for. Structural rather than hardcoded to the
    Datadog path so a third pack family cannot repeat it.
    """
    import tomllib

    repo = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["tool"]["setuptools"]["package-data"]

    undeclared = []
    for pack_yaml in (repo / "observability_migration").rglob("curated_packs/*/pack.yaml"):
        package = pack_yaml.parent.parent.relative_to(repo).as_posix().replace("/", ".")
        if not any(pattern.endswith("*.yaml") for pattern in declared.get(package, [])):
            undeclared.append(f"{package} -> {pack_yaml.relative_to(repo).as_posix()}")

    assert not undeclared, (
        "curated pack YAML is not declared in [tool.setuptools.package-data], so it "
        "will be absent from the built wheel and the pack will not apply for pip "
        f"installs: {undeclared}"
    )
