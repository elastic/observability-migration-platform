#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""
Analyse the migration_report.json produced by a batch run against the
DataDog integrations-core corpus.

Usage:
    python scripts/analyse_integrations_core_corpus.py \
        /tmp/dd-integrations-corpus-output/dashboards/migration_report.json
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _panel_status(pr: dict) -> str:
    return pr.get("status", "unknown")


def _widget_type(pr: dict) -> str:
    return pr.get("widget_type") or pr.get("source_widget_type") or ""


def main(report_path: str) -> None:
    data = json.loads(Path(report_path).read_text())
    dashboards = data.get("dashboards", [])

    total_dashboards = len(dashboards)
    dash_status: Counter[str] = Counter()

    panel_status: Counter[str] = Counter()
    unsupported_widget_types: Counter[str] = Counter()
    unsupported_by_integration: defaultdict[str, list[str]] = defaultdict(list)

    per_integration: dict[str, dict] = {}

    for d in dashboards:
        title = d.get("title") or d.get("dashboard_title") or "?"
        # Derive integration name from source file path if present
        source = d.get("source_file") or d.get("_source_file") or ""
        integration = Path(source).stem.split("__")[0] if source else title.split()[0].lower()

        d_migrated = d.get("migrated", 0)
        d_warnings = d.get("migrated_with_warnings", 0)
        d_manual = d.get("requires_manual", 0)
        d_nf = d.get("not_feasible", 0)
        d_skipped = d.get("skipped", 0)
        d_total = d.get("total_widgets", 0) or (d_migrated + d_warnings + d_manual + d_nf + d_skipped)
        d_err = bool(d.get("compile_error"))

        if d_err:
            dash_status["translation_error"] += 1
        elif d_nf == 0 and d_manual == 0:
            dash_status["fully_migrated"] += 1
        elif d_nf + d_manual < d_total:
            dash_status["partially_migrated"] += 1
        else:
            dash_status["needs_work"] += 1

        per_integration[integration] = {
            "title": title,
            "migrated": d_migrated,
            "warnings": d_warnings,
            "manual": d_manual,
            "not_feasible": d_nf,
            "skipped": d_skipped,
            "total": d_total,
            "error": d_err,
        }

        for pr in d.get("panels", []):
            st = _panel_status(pr)
            panel_status[st] += 1
            wt = pr.get("dd_widget_type") or _widget_type(pr)
            if st in ("not_feasible", "requires_manual") and wt:
                unsupported_widget_types[wt] += 1
                unsupported_by_integration[integration].append(wt)

    total_panels = sum(panel_status.values())
    skipped_panels = panel_status.get("skipped", 0)
    countable = total_panels - skipped_panels
    # panel status "ok" = clean migration, "warning" = migrated with warnings
    auto_migrated = panel_status.get("ok", 0) + panel_status.get("warning", 0)
    coverage_pct = (auto_migrated / countable * 100) if countable else 0.0

    print("=" * 70)
    print("  DataDog integrations-core corpus migration report")
    print("=" * 70)
    print(f"\n  Dashboards : {total_dashboards}")
    print(f"  Panels     : {total_panels}  ({skipped_panels} skipped/structural)")
    print("\n  Dashboard breakdown:")
    for k, v in sorted(dash_status.items(), key=lambda x: -x[1]):
        pct = v / total_dashboards * 100 if total_dashboards else 0
        print(f"    {k:<25s} {v:>4d}  ({pct:.1f}%)")

    print("\n  Panel breakdown:")
    for k, v in sorted(panel_status.items(), key=lambda x: -x[1]):
        pct = v / total_panels * 100 if total_panels else 0
        bar = "█" * int(pct / 2)
        print(f"    {k:<25s} {v:>5d}  ({pct:5.1f}%)  {bar}")

    print(f"\n  Auto-migration coverage: {auto_migrated}/{countable} non-skipped panels  ({coverage_pct:.1f}%)")

    if unsupported_widget_types:
        print("\n  Top unsupported widget types (not_feasible + requires_manual):")
        for wt, cnt in unsupported_widget_types.most_common(20):
            print(f"    {wt:<40s} {cnt:>4d}")

    # Fully-migrated: no errors, no not_feasible, no requires_manual
    fully = [i for i, s in per_integration.items() if not s["error"] and s["not_feasible"] == 0 and s["manual"] == 0]
    errors = [i for i, s in per_integration.items() if s["error"]]
    print(f"\n  Integrations with 100% auto-migration: {len(fully)}/{total_dashboards}")
    if errors:
        print(f"  Translation errors (crashes): {len(errors)}")
        for e in sorted(errors)[:10]:
            print(f"    {e}")

    # Integrations with most not-feasible panels
    worst = sorted(
        [(i, s["not_feasible"], s["total"]) for i, s in per_integration.items() if s["not_feasible"] > 0],
        key=lambda x: -x[1],
    )
    if worst:
        print("\n  Integrations with most not-feasible panels (top 15):")
        for integration, nf, total in worst[:15]:
            pct = nf / total * 100 if total else 0
            print(f"    {integration:<35s}  {nf:>3d}/{total:<3d}  ({pct:.0f}%)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <migration_report.json>")
        sys.exit(1)
    main(sys.argv[1])
