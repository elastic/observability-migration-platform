#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Build the Datadog semantic gap register from all shipped dashboards.

Walks every dashboard in infra/datadog/dashboards/ (including
integrations/) and writes a JSON record of every widget that landed at
status='not_feasible', so the honest gap surface is auditable across
releases.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE  # noqa: E402
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard  # noqa: E402
from observability_migration.adapters.source.datadog.planner import plan_widget  # noqa: E402
from observability_migration.adapters.source.datadog.translate import translate_widget  # noqa: E402

DASHBOARD_DIR = REPO_ROOT / "infra" / "datadog" / "dashboards"
OUTPUT_PATH = REPO_ROOT / "e2e_datadog_run" / "semantic_gap_register.json"


def _walk_dashboards() -> list[Path]:
    paths = list(DASHBOARD_DIR.glob("*.json"))
    paths.extend((DASHBOARD_DIR / "integrations").glob("*.json"))
    return sorted(paths)


def _truncate(s: str, n: int = 120) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def build_register() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    counts_by_reason: dict[str, int] = {}
    counts_by_widget_type: dict[str, int] = {}
    for path in _walk_dashboards():
        raw = json.loads(path.read_text(encoding="utf-8"))
        normalized = normalize_dashboard(raw)
        for widget in normalized.widgets:
            result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
            if result.status != "not_feasible":
                continue
            reason = result.reasons[0] if result.reasons else "unspecified"
            counts_by_reason[reason] = counts_by_reason.get(reason, 0) + 1
            counts_by_widget_type[result.dd_widget_type] = (
                counts_by_widget_type.get(result.dd_widget_type, 0) + 1
            )
            rows.append(
                {
                    "dashboard": str(path.relative_to(REPO_ROOT)),
                    "widget_id": result.widget_id,
                    "title": result.title,
                    "widget_type": result.dd_widget_type,
                    "backend": result.backend,
                    "source_queries": [_truncate(q) for q in result.source_queries],
                    "reasons": result.reasons,
                    "warnings": result.warnings,
                }
            )
    return {
        "summary": {
            "total_not_feasible": len(rows),
            "by_reason": counts_by_reason,
            "by_widget_type": counts_by_widget_type,
        },
        "widgets": rows,
    }


def main() -> int:
    register = build_register()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(register, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    print(
        f"Wrote {OUTPUT_PATH.relative_to(REPO_ROOT)} — "
        f"{register['summary']['total_not_feasible']} not_feasible widgets"
    )
    print("By reason:")
    for reason, count in sorted(
        register["summary"]["by_reason"].items(), key=lambda kv: -kv[1]
    ):
        print(f"  {count:>4}  {reason}")
    print("By widget type:")
    for wt, count in sorted(
        register["summary"]["by_widget_type"].items(), key=lambda kv: -kv[1]
    ):
        print(f"  {count:>4}  {wt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
