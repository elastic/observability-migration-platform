#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""
Deep-dive analysis of a combined_migration_report.json produced by
run_grafana_com_corpus.py.

Usage
-----
    python scripts/analyse_grafana_com_corpus.py /tmp/grafana-com-corpus/combined_migration_report.json
    python scripts/analyse_grafana_com_corpus.py /tmp/grafana-com-corpus/combined_migration_report.json --show-traces
    python scripts/analyse_grafana_com_corpus.py /tmp/grafana-com-corpus/combined_migration_report.json --json out.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KNOWN_PANEL_TYPES = {
    "timeseries", "graph", "stat", "singlestat", "gauge", "bargauge",
    "table", "table-old", "text", "logs", "heatmap", "piechart", "barchart",
    "row", "news", "dashlist", "alertlist", "nodeGraph", "canvas",
    "grafana-piechart-panel",
}

_NF_STATUSES = {"not_feasible", "requires_manual"}

_STACK_LOC_RE = re.compile(r'File "([^"]+)", line (\d+), in (\w+)')


def _classify_crash(trace: str) -> str:
    """Extract the most relevant location from a Python traceback."""
    if not trace:
        return "unknown"
    # Find last project frame (not venv)
    frames = _STACK_LOC_RE.findall(trace)
    project_frames = [(fi, ln, fn) for fi, ln, fn in frames if ".venv" not in fi and "site-packages" not in fi]
    if project_frames:
        fi, ln, fn = project_frames[-1]
        short = fi.split("observability_migration/")[-1] if "observability_migration/" in fi else Path(fi).name
        return f"{short}:{ln}  in {fn}"
    # Fall back to exception type + last line
    lines = [ln.strip() for ln in trace.splitlines() if ln.strip()]
    return lines[-1][:120] if lines else "unknown"


def _extract_promql_failure(reasons: list[str]) -> str | None:
    for r in reasons:
        if "PromQL" in r or "promql" in r or "unsupported" in r.lower():
            return r[:120]
    return None


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyse(data: dict, show_traces: bool = False) -> dict:
    dashboards = data.get("dashboards", [])
    total_dashboards = len(dashboards)

    # Dashboard-level
    dash_status: Counter = Counter()
    crash_details: list[dict] = []
    compile_error_details: list[dict] = []
    layout_error_details: list[dict] = []

    # Panel-level
    panel_status: Counter = Counter()
    grafana_type_nf: Counter = Counter()
    grafana_type_all: Counter = Counter()
    unknown_panel_types: Counter = Counter()
    reasons_nf: Counter = Counter()
    reasons_manual: Counter = Counter()
    promql_failures: Counter = Counter()
    datasource_type_nf: Counter = Counter()

    per_dashboard: list[dict] = []

    for d in dashboards:
        title = d.get("title") or "?"
        source = d.get("source_file") or ""
        te = d.get("translation_error") or ""
        ce = d.get("compile_error") or ""
        nf = d.get("not_feasible", 0) or 0
        mn = d.get("manual", 0) or d.get("requires_manual", 0) or 0
        w  = d.get("warnings", 0) or d.get("migrated_with_warnings", 0) or 0
        m  = d.get("migrated", 0) or 0
        sk = d.get("skipped", 0) or 0
        total_p = d.get("total_panels", 0) or (m + w + mn + nf + sk)

        runtime = d.get("runtime_summary", {})
        layout_err = (runtime.get("layout") or {}).get("error") or ""
        layout_ok  = (runtime.get("layout") or {}).get("status") == "pass"

        if te:
            dash_status["translation_error"] += 1
            crash_details.append({
                "title": title,
                "source": source,
                "trace": te,
                "location": _classify_crash(te),
            })
        elif ce:
            dash_status["compile_error"] += 1
            compile_error_details.append({
                "title": title,
                "source": source,
                "error": ce[:300],
            })
        elif nf == 0 and mn == 0:
            dash_status["fully_migrated"] += 1
        elif m + w > 0:
            dash_status["partially_migrated"] += 1
        else:
            dash_status["needs_work"] += 1

        if layout_err and not layout_ok:
            layout_error_details.append({"title": title, "error": layout_err[:200]})

        panel_nf_list: list[str] = []
        panel_manual_list: list[str] = []

        for pr in d.get("panels", []):
            st = pr.get("status", "unknown")
            gt = pr.get("grafana_type") or pr.get("grafana-type") or "?"
            panel_status[st] += 1
            grafana_type_all[gt] += 1

            if gt not in _KNOWN_PANEL_TYPES and gt != "?":
                unknown_panel_types[gt] += 1

            if st in _NF_STATUSES:
                grafana_type_nf[gt] += 1
                ds_type = pr.get("datasource_type") or ""
                if ds_type:
                    datasource_type_nf[ds_type] += 1

                pf = _extract_promql_failure(pr.get("reasons", []))
                if pf:
                    promql_failures[pf] += 1

                for r in pr.get("reasons", []):
                    if st == "not_feasible":
                        reasons_nf[r[:120]] += 1
                        panel_nf_list.append(f"{gt}: {r[:80]}")
                    else:
                        reasons_manual[r[:120]] += 1
                        panel_manual_list.append(f"{gt}: {r[:80]}")

        per_dashboard.append({
            "title": title,
            "source": source,
            "total": total_p,
            "migrated": m,
            "warnings": w,
            "manual": mn,
            "not_feasible": nf,
            "skipped": sk,
            "translation_error": bool(te),
            "compile_error": bool(ce),
            "nf_reasons": panel_nf_list[:5],
        })

    total_panels = sum(panel_status.values())
    skipped = panel_status.get("skipped", 0)
    countable = total_panels - skipped
    auto = panel_status.get("migrated", 0) + panel_status.get("migrated_with_warnings", 0)
    coverage = auto / countable * 100 if countable else 0.0

    return {
        "total_dashboards": total_dashboards,
        "total_panels": total_panels,
        "skipped_panels": skipped,
        "countable_panels": countable,
        "auto_migrated_panels": auto,
        "coverage_pct": coverage,
        "dash_status": dict(dash_status),
        "panel_status": dict(panel_status),
        "grafana_type_all": dict(grafana_type_all.most_common(30)),
        "grafana_type_nf": dict(grafana_type_nf.most_common(30)),
        "unknown_panel_types": dict(unknown_panel_types.most_common(30)),
        "reasons_nf": dict(reasons_nf.most_common(30)),
        "reasons_manual": dict(reasons_manual.most_common(30)),
        "promql_failures": dict(promql_failures.most_common(20)),
        "datasource_type_nf": dict(datasource_type_nf.most_common(20)),
        "crash_details": crash_details,
        "compile_error_details": compile_error_details,
        "layout_error_details": layout_error_details,
        "per_dashboard": per_dashboard,
    }


# ---------------------------------------------------------------------------
# Print
# ---------------------------------------------------------------------------

def _bar(v: int, total: int, width: int = 35) -> str:
    if total == 0:
        return ""
    return "█" * max(1, int(v / total * width))


def print_report(r: dict, show_traces: bool = False) -> None:
    td = r["total_dashboards"]
    tp = r["total_panels"]
    sk = r["skipped_panels"]
    co = r["countable_panels"]
    au = r["auto_migrated_panels"]
    cv = r["coverage_pct"]

    W = 72
    print("=" * W)
    print("  grafana.com corpus — detailed failure analysis")
    print("=" * W)
    print(f"\n  Dashboards analysed : {td}")
    print(f"  Panels total        : {tp}  ({sk} skipped/structural rows)")
    print(f"  Auto-migration      : {au}/{co} non-skipped  ({cv:.1f}%)")

    # ── Dashboard status ──────────────────────────────────────────────────
    print(f"\n{'─' * W}")
    print("  DASHBOARD STATUS")
    print(f"{'─' * W}")
    ds = r["dash_status"]
    for k, v in sorted(ds.items(), key=lambda x: -x[1]):
        print(f"  {k:<28s}  {v:>4d}  ({v / td * 100:.1f}%)  {_bar(v, td, 25)}")

    # ── Panel status ──────────────────────────────────────────────────────
    print(f"\n{'─' * W}")
    print("  PANEL STATUS")
    print(f"{'─' * W}")
    ps = r["panel_status"]
    for k, v in sorted(ps.items(), key=lambda x: -x[1]):
        print(f"  {k:<28s}  {v:>5d}  ({v / max(tp, 1) * 100:5.1f}%)  {_bar(v, tp)}")

    # ── Panel types encountered ───────────────────────────────────────────
    print(f"\n{'─' * W}")
    print("  GRAFANA PANEL TYPES IN CORPUS (all)")
    print(f"{'─' * W}")
    for pt, cnt in list(r["grafana_type_all"].items())[:25]:
        note = ""
        if pt in {"row"}:
            note = "  [structural — skipped]"
        elif pt not in _KNOWN_PANEL_TYPES and pt != "?":
            note = "  *** UNKNOWN — not in PANEL_TYPE_MAP ***"
        print(f"  {pt:<38s}  {cnt:>4d}{note}")

    # ── Unknown panel types ───────────────────────────────────────────────
    if r["unknown_panel_types"]:
        print(f"\n{'─' * W}")
        print("  UNKNOWN PANEL TYPES (not in PANEL_TYPE_MAP / SKIP_PANEL_TYPES)")
        print("  → These produce 'not_feasible' with 'Unknown Grafana panel type' reason")
        print(f"{'─' * W}")
        for pt, cnt in r["unknown_panel_types"].items():
            print(f"  {pt:<40s}  {cnt:>4d} occurrences")

    # ── Not-feasible by panel type ────────────────────────────────────────
    print(f"\n{'─' * W}")
    print("  NOT-FEASIBLE + REQUIRES-MANUAL : breakdown by panel type")
    print(f"{'─' * W}")
    for pt, cnt in list(r["grafana_type_nf"].items())[:25]:
        print(f"  {pt:<38s}  {cnt:>4d}")

    # ── Not-feasible reasons ──────────────────────────────────────────────
    print(f"\n{'─' * W}")
    print("  TOP FAILURE REASONS  (not_feasible)")
    print(f"{'─' * W}")
    for reason, cnt in list(r["reasons_nf"].items())[:30]:
        print(f"  [{cnt:>3d}]  {reason}")

    if r["reasons_manual"]:
        print(f"\n{'─' * W}")
        print("  TOP FAILURE REASONS  (requires_manual)")
        print(f"{'─' * W}")
        for reason, cnt in list(r["reasons_manual"].items())[:15]:
            print(f"  [{cnt:>3d}]  {reason}")

    # ── PromQL failures ───────────────────────────────────────────────────
    if r["promql_failures"]:
        print(f"\n{'─' * W}")
        print("  PROMQL TRANSLATION FAILURES")
        print(f"{'─' * W}")
        for msg, cnt in r["promql_failures"].items():
            print(f"  [{cnt:>3d}]  {msg}")

    # ── Datasource type nf ────────────────────────────────────────────────
    if r["datasource_type_nf"]:
        print(f"\n{'─' * W}")
        print("  DATASOURCE TYPES on not_feasible panels")
        print(f"{'─' * W}")
        for ds_type, cnt in r["datasource_type_nf"].items():
            print(f"  {ds_type:<40s}  {cnt:>4d}")

    # ── Crashes / translation errors ──────────────────────────────────────
    crashes = r["crash_details"]
    if crashes:
        print(f"\n{'─' * W}")
        print(f"  TRANSLATION ERRORS / CRASHES  ({len(crashes)} total)")
        print(f"{'─' * W}")
        # Group by crash location
        by_loc: dict[str, list[str]] = defaultdict(list)
        for c in crashes:
            by_loc[c["location"]].append(c["title"])
        for loc, titles in sorted(by_loc.items(), key=lambda x: -len(x[1])):
            print(f"\n  [{len(titles):>2d}x]  {loc}")
            for t in titles[:3]:
                print(f"          → {t[:65]}")
            if len(titles) > 3:
                print(f"          … and {len(titles) - 3} more")
        if show_traces:
            print("\n  Full stack traces:")
            for c in crashes[:5]:
                print(f"\n  ── {c['title']} ──")
                print(c["trace"])

    # ── Compile errors ────────────────────────────────────────────────────
    ce_list = r["compile_error_details"]
    if ce_list:
        print(f"\n{'─' * W}")
        print(f"  COMPILE ERRORS  ({len(ce_list)} total)")
        print(f"{'─' * W}")
        by_err: dict[str, list[str]] = defaultdict(list)
        for c in ce_list:
            key = c["error"].splitlines()[-1][:100] if c["error"] else "?"
            by_err[key].append(c["title"])
        for err, titles in sorted(by_err.items(), key=lambda x: -len(x[1])):
            print(f"\n  [{len(titles):>2d}x]  {err}")
            for t in titles[:3]:
                print(f"          → {t[:65]}")

    # ── Layout errors ─────────────────────────────────────────────────────
    le_list = r["layout_error_details"]
    if le_list:
        print(f"\n{'─' * W}")
        print(f"  LAYOUT VALIDATION ERRORS  ({len(le_list)} total)")
        print(f"{'─' * W}")
        by_err: dict[str, list[str]] = defaultdict(list)
        for c in le_list:
            key = c["error"][:100]
            by_err[key].append(c["title"])
        for err, titles in sorted(by_err.items(), key=lambda x: -len(x[1])):
            print(f"\n  [{len(titles):>2d}x]  {err}")
            for t in titles[:3]:
                print(f"          → {t[:65]}")

    # ── Worst dashboards ──────────────────────────────────────────────────
    worst = sorted(
        [p for p in r["per_dashboard"] if not p["translation_error"] and not p["compile_error"] and p["not_feasible"] > 0],
        key=lambda x: -(x["not_feasible"] + x["manual"]),
    )
    if worst:
        print(f"\n{'─' * W}")
        print("  DASHBOARDS WITH MOST NOT-FEASIBLE PANELS (top 20)")
        print(f"{'─' * W}")
        for p in worst[:20]:
            tot = max(p["total"], 1)
            nf_pct = (p["not_feasible"] + p["manual"]) / tot * 100
            print(f"  {p['title'][:50]:<52s}  nf={p['not_feasible']:>3d}  manual={p['manual']:>3d}  ({nf_pct:.0f}%)")
            for rr in p.get("nf_reasons", [])[:2]:
                print(f"      ↳ {rr[:70]}")

    print(f"\n{'=' * W}")
    print("  END OF REPORT")
    print(f"{'=' * W}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("report", help="Path to combined_migration_report.json")
    parser.add_argument("--show-traces", action="store_true",
                        help="Print full Python tracebacks for crash cases")
    parser.add_argument("--json", metavar="OUT", default="",
                        help="Also write structured analysis to this JSON file")
    args = parser.parse_args()

    path = Path(args.report)
    if not path.exists():
        sys.exit(f"ERROR: report not found: {path}")

    data = json.loads(path.read_text())
    result = analyse(data, show_traces=args.show_traces)

    print_report(result, show_traces=args.show_traces)

    if args.json:
        out = Path(args.json)
        # Remove per_dashboard (large) from JSON output unless needed
        out.write_text(json.dumps({k: v for k, v in result.items() if k != "per_dashboard"}, indent=2))
        print(f"Structured analysis written to {out}")


if __name__ == "__main__":
    main()
