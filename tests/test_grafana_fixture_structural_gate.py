# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import json
from pathlib import Path

from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    check_esql_structure,
    structural_errors,
)
from observability_migration.adapters.source.grafana.panels import translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "infra" / "grafana" / "dashboards"


def _walk(panels, out):
    for p in panels or []:
        if p.get("type") == "row":
            _walk(p.get("panels"), out)
            continue
        out.append(p)
        _walk(p.get("panels"), out)


def test_all_infra_grafana_fixtures_are_structurally_clean():
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    failures = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        dash = json.loads(path.read_text())
        panels = []
        _walk(dash.get("panels"), panels)
        for row in dash.get("rows") or []:
            _walk(row.get("panels"), panels)
        for panel in panels:
            if panel.get("type") in {"row", "text", "news", "dashlist", "alertlist"}:
                continue
            try:
                yaml_panel, result = translate_panel(
                    panel,
                    datasource_index="metrics-*",
                    esql_index="metrics-*",
                    rule_pack=rule_pack,
                    resolver=resolver,
                )
            except Exception as exc:  # pragma: no cover
                failures.append(f"{path.name}:{panel.get('title')}: translate crashed: {exc}")
                continue
            if result.status in {"requires_manual", "skipped"}:
                continue
            query = (yaml_panel or {}).get("esql", {}).get("query") or ""
            if not query:
                continue
            errs = structural_errors(
                check_esql_structure(query, feasibility="feasible", require_stats_for_feasible=False)
            )
            for err in errs:
                failures.append(
                    f"{path.name} :: {panel.get('title')!r} :: {err.rule_id.value}: {err.message}"
                )
    assert not failures, "structural oracle failures:\n" + "\n".join(failures[:50])
