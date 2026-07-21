# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Fixture gate for LogQL panels in infra/grafana dashboards (issue #301 PR3)."""

from __future__ import annotations

import json
from pathlib import Path

from observability_migration.adapters.source.grafana.broader_surface_gate import (
    check_logql_emission,
    gate_bugs,
)
from observability_migration.adapters.source.grafana.panels import translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "infra" / "grafana" / "dashboards"
LOGQL_FIXTURES = (
    "diverse-panels-test.json",
    "multi-pattern-coverage.json",
)


def _walk(panels, out):
    for panel in panels or []:
        if not isinstance(panel, dict):
            continue
        if panel.get("type") == "row":
            _walk(panel.get("panels"), out)
            continue
        out.append(panel)
        _walk(panel.get("panels"), out)


def _is_loki_panel(panel: dict) -> bool:
    ds = panel.get("datasource")
    if isinstance(ds, dict) and str(ds.get("type") or "").lower() == "loki":
        return True
    for target in panel.get("targets") or []:
        if not isinstance(target, dict):
            continue
        tds = target.get("datasource")
        if isinstance(tds, dict) and str(tds.get("type") or "").lower() == "loki":
            return True
    return False


def _collect_loki_panels():
    panels = []
    for name in LOGQL_FIXTURES:
        path = FIXTURE_DIR / name
        dash = json.loads(path.read_text(encoding="utf-8"))
        found = []
        _walk(dash.get("panels"), found)
        for row in dash.get("rows") or []:
            _walk(row.get("panels"), found)
        for panel in found:
            if _is_loki_panel(panel):
                panels.append((name, panel))
    return panels


def test_infra_logql_panels_pass_offline_gate():
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    loki_panels = _collect_loki_panels()
    assert loki_panels, "expected at least one Loki panel in pinned LogQL fixtures"
    failures = []
    for fixture_name, panel in loki_panels:
        yaml_panel, result = translate_panel(
            panel,
            datasource_index=rule_pack.logs_index,
            esql_index=rule_pack.logs_index,
            rule_pack=rule_pack,
            resolver=resolver,
        )
        if result.status in {"requires_manual", "skipped"}:
            continue
        query = (yaml_panel or {}).get("esql", {}).get("query") or ""
        bugs = gate_bugs(
            check_logql_emission(
                query,
                logs_index=rule_pack.logs_index,
                feasibility="feasible",
            )
        )
        for bug in bugs:
            failures.append(
                f"{fixture_name} :: {panel.get('title')!r} :: {bug.rule_id.value}: {bug.message}"
            )
    assert not failures, "LogQL fixture gate failures:\n" + "\n".join(failures)


def test_corrupting_from_stage_makes_logql_gate_fail():
    findings = check_logql_emission(
        "| WHERE message LIKE \"*error*\"\n| KEEP message\n",
        feasibility="feasible",
    )
    assert gate_bugs(findings)
