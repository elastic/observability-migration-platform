#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Emit a plan of dashboards to audit via Chrome DevTools MCP.

Reads e2e_datadog_run/<slug>/dashboards/migration_report.json for each
slug, finds uploaded dashboards, and writes
e2e_datadog_run/browser_audit_plan.json. The agent iterates the plan,
drives Chrome to each URL, captures screenshot + console + network,
and writes per-dashboard browser_audit_report.json via the helpers in
observability_migration.adapters.source.datadog.browser_audit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from observability_migration.adapters.source.datadog.browser_audit import (  # noqa: E402
    discover_uploaded_dashboards,
)

RUN_ROOT = REPO_ROOT / "e2e_datadog_run"
PLAN_PATH = RUN_ROOT / "browser_audit_plan.json"


def main() -> int:
    uploaded = discover_uploaded_dashboards(RUN_ROOT)
    plan = {
        "total": len(uploaded),
        "dashboards": [
            {
                "slug": d.slug,
                "title": d.dashboard_title,
                "kibana_url": d.kibana_url,
                "saved_object_id": d.saved_object_id,
                "output_dir": str(d.output_dir.relative_to(REPO_ROOT)),
                "screenshot_path": str(
                    (d.output_dir / "browser_screenshot.png").relative_to(REPO_ROOT)
                ),
            }
            for d in uploaded
        ],
    }
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(f"Wrote {PLAN_PATH.relative_to(REPO_ROOT)} — {len(uploaded)} dashboards to audit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
