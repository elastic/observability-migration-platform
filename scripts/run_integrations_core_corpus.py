#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""
Run the Datadog migration CLI against the DataDog integrations-core corpus
in batches, then print an aggregated summary.

Prerequisites
-------------
Sparse-clone integrations-core once:

    git clone --depth=1 --filter=blob:none --sparse \\
        https://github.com/DataDog/integrations-core.git /tmp/dd-integrations-core
    cd /tmp/dd-integrations-core
    git sparse-checkout set --no-cone '*/assets/dashboards'

Usage
-----
    python scripts/run_integrations_core_corpus.py [--batch-size N] [--source-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
DEFAULT_SOURCE = Path("/tmp/dd-integrations-core")
DEFAULT_OUTPUT = Path("/tmp/dd-integrations-corpus-output")


def find_dashboards(source_dir: Path) -> list[Path]:
    files = sorted(source_dir.rglob("*/assets/dashboards/*.json"))
    if not files:
        sys.exit(f"ERROR: no dashboard JSON files found under {source_dir}")
    return files


def batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def run_batch(batch: list[Path], batch_num: int, output_root: Path) -> dict:
    batch_output = output_root / f"batch-{batch_num:03d}"
    batch_output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="dd-corpus-batch-") as tmp_input:
        # Copy files into flat temp dir with unique names
        for src in batch:
            integration = src.parts[src.parts.index("assets") - 1] if "assets" in src.parts else src.stem
            dest = Path(tmp_input) / f"{integration}__{src.name}"
            shutil.copy2(src, dest)

        print(f"\n--- Batch {batch_num:3d}  ({len(batch)} dashboards) ---", flush=True)

        result = subprocess.run(
            [
                str(PYTHON), "-m",
                "observability_migration.adapters.source.datadog.cli",
                "--source", "files",
                "--input-dir", tmp_input,
                "--output-dir", str(batch_output),
                "--assets", "dashboards",
                "--field-profile", "otel",
                "--data-view", "metrics-*",
                "--env-file", "/dev/null",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

    # Print concise status lines from CLI output
    for line in (result.stdout + result.stderr).splitlines():
        if any(tok in line for tok in ("[1/", "[2/", "Migrated", "migrated", "✓", "⚠", "✗", "WARN", "ERROR")):
            print(f"  {line.rstrip()}", flush=True)

    report_path = batch_output / "dashboards" / "migration_report.json"
    if report_path.exists():
        return json.loads(report_path.read_text())
    if result.returncode != 0:
        print(f"  BATCH ERROR (exit {result.returncode})")
        print(result.stderr[-800:] if result.stderr else "(no stderr)")
    return {}


def aggregate(all_reports: list[dict]) -> None:
    all_dashboards = [d for r in all_reports for d in r.get("dashboards", [])]
    total = len(all_dashboards)
    if not total:
        print("\nNo dashboards in reports.")
        return

    panel_status: Counter = Counter()
    dash_status: Counter = Counter()
    unsupported: Counter = Counter()
    crash_list: list[str] = []

    for d in all_dashboards:
        m   = d.get("migrated", 0)
        w   = d.get("migrated_with_warnings", 0)
        mn  = d.get("requires_manual", 0)
        nf  = d.get("not_feasible", 0)
        err = bool(d.get("compile_error"))

        title = d.get("title") or d.get("dashboard_title") or "?"
        if err:
            dash_status["translation_error"] += 1
            crash_list.append(title)
        elif nf == 0 and mn == 0:
            dash_status["fully_migrated"] += 1
        elif m + w > 0:
            dash_status["partially_migrated"] += 1
        else:
            dash_status["needs_work"] += 1

        for pr in d.get("panels", []):
            st = pr.get("status", "unknown")
            panel_status[st] += 1
            wt = pr.get("dd_widget_type") or pr.get("widget_type") or ""
            if st in ("not_feasible", "requires_manual") and wt:
                unsupported[wt] += 1

    # "ok" = clean migration, "warning" = migrated with warnings; "skipped" excluded from denominator
    total_panels = sum(panel_status.values())
    skipped_panels = panel_status.get("skipped", 0)
    countable = total_panels - skipped_panels
    auto = panel_status.get("ok", 0) + panel_status.get("warning", 0)
    pct = auto / countable * 100 if countable else 0.0

    print("\n" + "=" * 62)
    print("  integrations-core corpus — aggregated results")
    print("=" * 62)
    print(f"\n  Dashboards : {total}")
    print(f"  Panels     : {total_panels}  ({skipped_panels} skipped/structural)")
    print(f"  Coverage   : {auto}/{countable}  ({pct:.1f}% of non-skipped)")

    print("\n  Dashboard status:")
    for k, v in sorted(dash_status.items(), key=lambda x: -x[1]):
        print(f"    {k:<26s}  {v:>4d}  ({v / total * 100:.1f}%)")

    print("\n  Panel status:")
    for k, v in sorted(panel_status.items(), key=lambda x: -x[1]):
        bar = "█" * int(v / total_panels * 40)
        print(f"    {k:<26s}  {v:>5d}  ({v / total_panels * 100:5.1f}%)  {bar}")


    if unsupported:
        print("\n  Top unsupported widget types (not_feasible + requires_manual):")
        for wt, cnt in unsupported.most_common(20):
            print(f"    {wt:<42s}  {cnt:>4d}")

    if crash_list:
        print(f"\n  Translation errors ({len(crash_list)}):")
        for t in crash_list[:15]:
            print(f"    {t}")
        if len(crash_list) > 15:
            print(f"    … and {len(crash_list) - 15} more")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-size", type=int, default=100, metavar="N")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    dashboards = find_dashboards(args.source_dir)
    batches = list(batched(dashboards, args.batch_size))
    total_batches = len(batches)

    print(f"Found {len(dashboards)} dashboards → {total_batches} batch(es) of ≤{args.batch_size}")

    if args.output_dir.exists():
        shutil.rmtree(args.output_dir)
    args.output_dir.mkdir(parents=True)

    all_reports = []
    for i, batch in enumerate(batches, 1):
        report = run_batch(batch, i, args.output_dir)
        if report:
            all_reports.append(report)

    print(f"\n{'='*62}")
    print(f"  All {total_batches} batch(es) done")
    print(f"{'='*62}")

    aggregate(all_reports)

    # Save combined report
    combined = {"dashboards": [d for r in all_reports for d in r.get("dashboards", [])]}
    out = args.output_dir / "combined_migration_report.json"
    out.write_text(json.dumps(combined, indent=2))
    print(f"\n  Combined report → {out}")


if __name__ == "__main__":
    main()
