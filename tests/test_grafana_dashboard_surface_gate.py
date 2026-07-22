# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline gate for Grafana dashboard controls + links (issue #301 PR3)."""

from __future__ import annotations

import json
from pathlib import Path

from observability_migration.adapters.source.grafana.broader_surface_gate import (
    check_dashboard_surface,
    gate_bugs,
)
from observability_migration.adapters.source.grafana.links import (
    build_links_summary,
    translate_dashboard_links,
)
from observability_migration.adapters.source.grafana.panels import translate_variables
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "infra" / "grafana" / "dashboards"
SURFACE_FIXTURES = (
    "node-exporter-full.json",
    "prometheus-all.json",
)


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _source_variable_count(dashboard: dict) -> int:
    templating = dashboard.get("templating") or {}
    items = templating.get("list") if isinstance(templating, dict) else templating
    return len([v for v in (items or []) if isinstance(v, dict)])


def test_pinned_dashboards_preserve_controls_and_links():
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    failures = []
    for name in SURFACE_FIXTURES:
        dash = _load(name)
        source_vars = _source_variable_count(dash)
        source_links = len(dash.get("links") or [])
        assert source_vars > 0, f"{name} expected templating variables"
        assert source_links > 0, f"{name} expected dashboard links"

        template_list = (dash.get("templating") or {}).get("list") or []
        controls = translate_variables(
            template_list,
            datasource_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        dashboard_links = translate_dashboard_links(dash)
        summary = build_links_summary(dashboard_links, {})
        bugs = gate_bugs(
            check_dashboard_surface(
                source_variable_count=source_vars,
                translated_control_count=len(controls),
                source_dashboard_link_count=source_links,
                links_summary=summary,
            )
        )
        for bug in bugs:
            failures.append(f"{name} :: {bug.rule_id.value}: {bug.message}")
    assert not failures, "dashboard surface gate failures:\n" + "\n".join(failures)


def test_corrupting_controls_makes_surface_gate_fail():
    bugs = gate_bugs(
        check_dashboard_surface(
            source_variable_count=3,
            translated_control_count=0,
            source_dashboard_link_count=0,
            links_summary={"dashboard_links": 0},
        )
    )
    assert any(b.rule_id.value == "CONTROLS_SILENT_DROP" for b in bugs)


def test_corrupting_links_summary_makes_surface_gate_fail():
    bugs = gate_bugs(
        check_dashboard_surface(
            source_variable_count=0,
            translated_control_count=0,
            source_dashboard_link_count=2,
            links_summary={"dashboard_links": 0},
        )
    )
    assert any(b.rule_id.value == "LINKS_SILENT_DROP" for b in bugs)
