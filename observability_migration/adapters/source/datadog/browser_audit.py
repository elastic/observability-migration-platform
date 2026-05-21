# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Helpers for the Datadog browser audit step.

The audit pipeline lives partly outside Python (Chrome DevTools MCP is
driven by the agent), so this module handles:

- discovering uploaded dashboards from e2e run output
- classifying browser audit findings
- aggregating per-dashboard reports into a summary
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UploadedDashboard:
    slug: str
    kibana_url: str
    saved_object_id: str
    dashboard_title: str
    output_dir: Path


@dataclass
class BrowserAuditFinding:
    slug: str
    status: str
    screenshot_path: str
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    panels_visible_estimate: int = 0


CONSOLE_ERROR_KEYWORDS = ("kibana", "esql", "es|ql", "lens")


def discover_uploaded_dashboards(run_root: Path) -> list[UploadedDashboard]:
    """Walk each <slug>/dashboards/migration_report.json and return one
    UploadedDashboard per dashboard that successfully uploaded."""

    uploaded: list[UploadedDashboard] = []
    for slug_dir in sorted(run_root.glob("dd-*")):
        report_path = slug_dir / "dashboards" / "migration_report.json"
        if not report_path.exists():
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for dashboard in report.get("dashboards", []):
            upload = dashboard.get("upload", {})
            if not upload.get("uploaded"):
                continue
            uploaded.append(
                UploadedDashboard(
                    slug=slug_dir.name,
                    kibana_url=upload.get("kibana_url", ""),
                    saved_object_id=upload.get("saved_object_id", ""),
                    dashboard_title=dashboard.get("title", slug_dir.name),
                    output_dir=slug_dir / "dashboards",
                )
            )
    return uploaded


def classify(
    *,
    console_errors: Iterable[str],
    failed_requests: Iterable[str],
    screenshot_path: Path,
) -> str:
    """Return 'pass' / 'warn' / 'fail' for a single browser audit run."""

    console_list = list(console_errors)
    filtered = [c for c in console_list if any(k in c.lower() for k in CONSOLE_ERROR_KEYWORDS)]
    if filtered:
        return "fail"
    fail_list = list(failed_requests)
    fivexx = [r for r in fail_list if " 5" in r]
    if fivexx:
        return "fail"
    if not screenshot_path.exists() or screenshot_path.stat().st_size < 1024:
        return "warn"
    if fail_list:
        return "warn"
    return "pass"


def write_finding(finding: BrowserAuditFinding, output_dir: Path) -> Path:
    report_path = output_dir / "browser_audit_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "slug": finding.slug,
                "status": finding.status,
                "screenshot_path": finding.screenshot_path,
                "console_errors": finding.console_errors,
                "failed_requests": finding.failed_requests,
                "panels_visible_estimate": finding.panels_visible_estimate,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return report_path


def write_summary(findings: list[BrowserAuditFinding], summary_path: Path) -> None:
    summary = {
        "total": len(findings),
        "pass": sum(1 for f in findings if f.status == "pass"),
        "warn": sum(1 for f in findings if f.status == "warn"),
        "fail": sum(1 for f in findings if f.status == "fail"),
        "dashboards": [
            {
                "slug": f.slug,
                "status": f.status,
                "screenshot": f.screenshot_path,
                "console_error_count": len(f.console_errors),
                "failed_request_count": len(f.failed_requests),
                "panels_visible_estimate": f.panels_visible_estimate,
            }
            for f in findings
        ],
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
