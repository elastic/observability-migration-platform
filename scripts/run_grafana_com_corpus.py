#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""
Download the top-N most-downloaded dashboards from grafana.com and run them
through the grafana-migrate pipeline (dry-run: translate + compile, no upload).

Usage
-----
    python scripts/run_grafana_com_corpus.py [--top N] [--output-dir DIR] [--batch-size N]

Prerequisites
-------------
    .venv with requirements.txt + package installed (pip install -e .)

The script does NOT require Elasticsearch or Kibana credentials — it runs the
pipeline in offline mode (no --validate, no --upload).

Outputs (default: /tmp/grafana-com-corpus/)
-------------------------------------------
    inputs/                     downloaded dashboard JSON files
    batch-001/dashboards/       per-batch migration output
    combined_migration_report.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
GRAFANA_COM_API = "https://grafana.com/api/dashboards"
DEFAULT_OUTPUT = Path("/tmp/grafana-com-corpus")
DEFAULT_TOP = 150
DEFAULT_BATCH = 50
REQUEST_DELAY = 0.15  # seconds between grafana.com download requests


# ---------------------------------------------------------------------------
# Fetch dashboard list
# ---------------------------------------------------------------------------

def fetch_top_dashboards(n: int) -> list[dict]:
    """Return top-N dashboards sorted by downloads from grafana.com."""
    resp = requests.get(
        GRAFANA_COM_API,
        params={"orderBy": "downloads", "direction": "desc", "page": "1"},
        timeout=30,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return items[:n]


# ---------------------------------------------------------------------------
# Download individual dashboard JSON
# ---------------------------------------------------------------------------

def download_dashboard(dashboard_id: int, slug: str, out_dir: Path) -> Path | None:
    """Download a single dashboard JSON from grafana.com.  Returns path or None on failure."""
    url = f"{GRAFANA_COM_API}/{dashboard_id}/revisions/latest/download"
    fname = f"{dashboard_id:05d}_{slug}.json"
    dest = out_dir / fname
    if dest.exists():
        return dest  # already cached from a previous run
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # Only save if it looks like a real dashboard
        if not ("panels" in data or "rows" in data):
            print(f"  SKIP  {fname} — no panels/rows key")
            return None
        dest.write_text(json.dumps(data))
        return dest
    except Exception as exc:
        print(f"  WARN  {fname} — download failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Run migration CLI on a batch of input files
# ---------------------------------------------------------------------------

def run_batch(input_dir: Path, batch_num: int, output_root: Path) -> dict:
    """Run the grafana-migrate CLI on all JSON files in input_dir."""
    batch_out = output_root / f"batch-{batch_num:03d}"
    batch_out.mkdir(parents=True, exist_ok=True)

    print(f"\n--- Batch {batch_num:3d}  (input: {input_dir}) ---", flush=True)

    result = subprocess.run(
        [
            str(PYTHON), "-m",
            "observability_migration.adapters.source.grafana.cli",
            "--source", "files",
            "--input-dir", str(input_dir),
            "--output-dir", str(batch_out),
            "--assets", "dashboards",
            "--native-promql",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=600,
    )

    # Echo progress lines
    for line in (result.stdout + result.stderr).splitlines():
        stripped = line.rstrip()
        if any(tok in stripped for tok in (
            "[", "Migrat", "migrat", "✓", "⚠", "✗", "WARN", "ERROR",
            "translation_error", "compile_error", "fully", "partial",
        )):
            print(f"  {stripped}", flush=True)

    report_path = batch_out / "dashboards" / "migration_report.json"
    if report_path.exists():
        return json.loads(report_path.read_text())

    if result.returncode != 0:
        print(f"  BATCH ERROR (exit {result.returncode})")
        if result.stderr:
            print(result.stderr[-1000:])
    return {}


# ---------------------------------------------------------------------------
# Aggregate and print summary
# ---------------------------------------------------------------------------

def aggregate_and_print(all_reports: list[dict], top: int) -> None:
    all_dashboards = [d for r in all_reports for d in r.get("dashboards", [])]
    total = len(all_dashboards)
    if not total:
        print("\nNo dashboards found in reports.")
        return

    panel_status: Counter = Counter()
    dash_status: Counter = Counter()
    crash_list: list[tuple[str, str]] = []
    compile_errors: list[tuple[str, str]] = []
    grafana_type_nf: Counter = Counter()
    reasons_nf: Counter = Counter()

    for d in all_dashboards:
        title = d.get("title") or "?"
        te = d.get("translation_error") or ""
        ce = d.get("compile_error") or ""
        nf = d.get("not_feasible", 0) or 0
        mn = d.get("manual", 0) or d.get("requires_manual", 0) or 0
        m  = d.get("migrated", 0) or 0
        w  = d.get("warnings", 0) or d.get("migrated_with_warnings", 0) or 0

        if te:
            dash_status["translation_error"] += 1
            crash_list.append((title, te[:200]))
        elif ce:
            dash_status["compile_error"] += 1
            compile_errors.append((title, ce[:200]))
        elif nf == 0 and mn == 0:
            dash_status["fully_migrated"] += 1
        elif m + w > 0:
            dash_status["partially_migrated"] += 1
        else:
            dash_status["needs_work"] += 1

        for pr in d.get("panels", []):
            st = pr.get("status", "unknown")
            panel_status[st] += 1
            if st in ("not_feasible", "requires_manual"):
                grafana_type_nf[pr.get("grafana_type", "?")] += 1
                for r in pr.get("reasons", []):
                    reasons_nf[r[:120]] += 1

    total_panels = sum(panel_status.values())
    skipped = panel_status.get("skipped", 0)
    countable = total_panels - skipped
    auto = panel_status.get("migrated", 0) + panel_status.get("migrated_with_warnings", 0)
    pct = auto / countable * 100 if countable else 0.0

    print("\n" + "=" * 70)
    print(f"  grafana.com top-{top} corpus — migration results")
    print("=" * 70)
    print(f"\n  Dashboards run : {total}")
    print(f"  Panels total   : {total_panels}  ({skipped} skipped/structural)")
    print(f"  Coverage       : {auto}/{countable}  ({pct:.1f}% of non-skipped panels)")

    print("\n  Dashboard breakdown:")
    for k, v in sorted(dash_status.items(), key=lambda x: -x[1]):
        print(f"    {k:<28s}  {v:>4d}  ({v / total * 100:.1f}%)")

    print("\n  Panel status:")
    for k, v in sorted(panel_status.items(), key=lambda x: -x[1]):
        bar = "█" * int(v / max(total_panels, 1) * 40)
        print(f"    {k:<28s}  {v:>5d}  ({v / max(total_panels, 1) * 100:5.1f}%)  {bar}")

    if grafana_type_nf:
        print("\n  Panel types with not_feasible / requires_manual (top 20):")
        for pt, cnt in grafana_type_nf.most_common(20):
            print(f"    {pt:<35s}  {cnt:>4d}")

    if reasons_nf:
        print("\n  Top failure reasons (top 25):")
        for reason, cnt in reasons_nf.most_common(25):
            print(f"    [{cnt:>3d}]  {reason}")

    if crash_list:
        print(f"\n  Translation errors / crashes ({len(crash_list)}):")
        for title, snippet in crash_list[:15]:
            first_line = snippet.splitlines()[-1] if snippet else "(no detail)"
            print(f"    {title[:50]:<50s}  {first_line[:60]}")
        if len(crash_list) > 15:
            print(f"    … and {len(crash_list) - 15} more")

    if compile_errors:
        print(f"\n  Compile errors ({len(compile_errors)}):")
        for title, snippet in compile_errors[:10]:
            first_line = snippet.splitlines()[-1] if snippet else "(no detail)"
            print(f"    {title[:50]:<50s}  {first_line[:60]}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, metavar="N",
                        help=f"Number of top-downloaded dashboards to fetch (default {DEFAULT_TOP})")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH, metavar="N",
                        help=f"Dashboards per CLI invocation (default {DEFAULT_BATCH})")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT,
                        help=f"Output root directory (default {DEFAULT_OUTPUT})")
    parser.add_argument("--keep-inputs", action="store_true",
                        help="Keep downloaded input JSON files after the run")
    args = parser.parse_args()

    if not PYTHON.exists():
        sys.exit(
            f"ERROR: Python venv not found at {PYTHON}\n"
            "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e ."
        )

    # Fetch dashboard list
    print(f"Fetching top-{args.top} dashboards from grafana.com …")
    items = fetch_top_dashboards(args.top)
    print(f"  → {len(items)} dashboards listed")

    # Prepare output dirs
    args.output_dir.mkdir(parents=True, exist_ok=True)
    inputs_dir = args.output_dir / "inputs"
    inputs_dir.mkdir(exist_ok=True)

    # Download dashboards
    print("\nDownloading dashboard JSON files …")
    downloaded: list[Path] = []
    for i, item in enumerate(items, 1):
        did = item.get("id")
        slug = item.get("slug", f"dash-{did}")
        dest = download_dashboard(did, slug, inputs_dir)
        if dest:
            downloaded.append(dest)
        if i % 10 == 0:
            print(f"  {i}/{len(items)} downloaded ({len(downloaded)} ok) …", flush=True)
        time.sleep(REQUEST_DELAY)

    print(f"\n  → {len(downloaded)}/{len(items)} dashboards downloaded successfully")

    if not downloaded:
        sys.exit("ERROR: No dashboards downloaded.")

    # Run pipeline in batches
    batches = list(batched(downloaded, args.batch_size))
    print(f"\nRunning migration pipeline: {len(downloaded)} dashboards in {len(batches)} batch(es) …")

    all_reports = []
    for i, batch in enumerate(batches, 1):
        # Copy batch files into a temp input dir
        import tempfile
        with tempfile.TemporaryDirectory(prefix="grafana-com-batch-") as tmp:
            tmp_path = Path(tmp)
            for f in batch:
                shutil.copy2(f, tmp_path / f.name)
            report = run_batch(tmp_path, i, args.output_dir)
        if report:
            all_reports.append(report)

    # Save combined report
    combined = {"dashboards": [d for r in all_reports for d in r.get("dashboards", [])]}
    out = args.output_dir / "combined_migration_report.json"
    out.write_text(json.dumps(combined, indent=2))
    print(f"\nCombined report saved: {out}")

    # Print aggregate summary
    aggregate_and_print(all_reports, args.top)

    if not args.keep_inputs:
        shutil.rmtree(inputs_dir, ignore_errors=True)

    # Exit non-zero if any crashes
    crash_count = sum(
        1 for d in combined.get("dashboards", [])
        if d.get("translation_error") or d.get("compile_error")
    )
    if crash_count:
        sys.exit(f"\n{crash_count} dashboard(s) had errors. Review combined report: {out}")


if __name__ == "__main__":
    main()
