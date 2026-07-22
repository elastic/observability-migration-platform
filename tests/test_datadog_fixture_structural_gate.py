# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import json
from pathlib import Path

from observability_migration.adapters.source.datadog.esql_structural_oracle import (
    ESQL_EMITTING_BACKENDS,
    check_datadog_esql_structure,
    structural_errors,
)
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "infra" / "datadog" / "dashboards"


def test_all_infra_datadog_fixtures_are_structurally_clean():
    failures: list[str] = []
    paths = sorted(FIXTURE_DIR.rglob("*.json"))
    assert paths, f"no Datadog fixtures under {FIXTURE_DIR}"
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        try:
            nd = normalize_dashboard(raw)
        except Exception as exc:  # pragma: no cover
            failures.append(f"{path}: normalize crashed: {exc}")
            continue
        for widget in nd.widgets:
            try:
                plan = plan_widget(widget)
                result = translate_widget(widget, plan, OTEL_PROFILE)
            except Exception as exc:  # pragma: no cover
                failures.append(
                    f"{path.name}:{widget.title}: translate crashed: {exc}"
                )
                continue
            if result.backend not in ESQL_EMITTING_BACKENDS:
                continue
            if result.status not in {"ok", "warning"}:
                continue
            errs = structural_errors(
                check_datadog_esql_structure(
                    result.esql_query or "",
                    status=result.status,
                    backend=result.backend,
                )
            )
            for err in errs:
                failures.append(
                    f"{path.relative_to(FIXTURE_DIR)} :: {widget.title!r} :: "
                    f"{err.rule_id.value}: {err.message}"
                )
    assert not failures, "structural oracle failures:\n" + "\n".join(failures[:50])
