#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Benchmark the migration engine over a pinned corpus and score it honestly.

This orchestrator drives the *real shipping pipeline* -- it does not reimplement
translation -- so the scorecard reflects exactly what ``obs-migrate`` produces:

  1. (optional) fetch the version-pinned corpus (scripts/fetch_benchmark_corpus.py)
  2. ``obs-migrate migrate --source grafana`` over the corpus -> verification_packets.json
  3. STATIC scorecard: aggregate per-panel ``status`` + ``semantic_gate`` +
     ``source_language`` from verification_packets.json (offline, no cluster).
  4. ORACLE scorecard (when --es-url/--api-key given): ``obs-migrate compare``
     runs each panel's translated ES|QL against Elasticsearch's native PROMQL
     command on the same seeded index/window and diffs per bucket. Verdicts:
     STRICT_PASS (<=1% max rel err), FUZZY_PASS (<=5%), SHAPE_PASS, FAIL, SKIP,
     ERROR (numeric); STRUCTURAL for non-PromQL / no-oracle panels.

The two scorecards measure different things and are NEVER conflated: static
coverage = "how much translates/compiles cleanly"; oracle parity = "of the
PromQL panels we can mathematically check, how many match". A panel that
migrates statically can still FAIL the oracle, and vice versa.

Each run is tagged with --label and written to <out>/scorecard.json (+ .md).
Use --compare-to <other scorecard.json> to print a before/after delta, which is
the point of running this once on clean HEAD and once after fixes.

Usage:
    # Static-only baseline (offline, fast)
    python scripts/benchmark_corpus.py --label baseline \\
        --corpus-dir /tmp/bench-corpus --out /tmp/bench/baseline --fetch

    # Full numeric oracle (needs a seeded Elasticsearch)
    set -a && . ./serverless_creds.env && set +a
    python scripts/benchmark_corpus.py --label baseline \\
        --corpus-dir /tmp/bench-corpus --out /tmp/bench/baseline --fetch \\
        --es-url "$ELASTICSEARCH_ENDPOINT" --api-key "$KEY" --seed

    # Delta against an earlier scorecard
    python scripts/benchmark_corpus.py --label after ... \\
        --compare-to /tmp/bench/baseline/scorecard.json
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Numeric oracle verdicts that count as a "match" for the headline parity rate.
ORACLE_PASS = {"STRICT_PASS", "FUZZY_PASS"}
# Verdicts that mean the oracle actually ran and compared numbers.
ORACLE_COMPARED = {"STRICT_PASS", "FUZZY_PASS", "SHAPE_PASS", "FAIL"}


def _venv_bin(name: str) -> str:
    candidate = ROOT / ".venv" / "bin" / name
    return str(candidate) if candidate.exists() else name


def _run(cmd: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, env=env, check=check, text=True, capture_output=True)


# ---------------------------------------------------------------------------
# Static scorecard (from verification_packets.json)
# ---------------------------------------------------------------------------
def static_scorecard(packets_path: Path) -> dict[str, Any]:
    data = json.loads(packets_path.read_text(encoding="utf-8"))
    packets = data.get("packets", []) if isinstance(data, dict) else []
    status = Counter()
    gate = Counter()
    language = Counter()
    dashboards: set[str] = set()
    promql_translated = 0
    for pkt in packets:
        status[pkt.get("status", "unknown")] += 1
        gate[pkt.get("semantic_gate", "unknown")] += 1
        language[pkt.get("source_language", "unknown")] += 1
        dashboards.add(pkt.get("dashboard", ""))
        if pkt.get("source_language") == "promql" and pkt.get("translated_query"):
            promql_translated += 1
    total = len(packets)
    # "Clean" = green semantic gate (no known losses / warnings).
    green = gate.get("Green", 0)
    return {
        "dashboards": len([d for d in dashboards if d]),
        "panels": total,
        "promql_panels_translated": promql_translated,
        "semantic_gate": dict(gate),
        "status": dict(status),
        "source_language": dict(language),
        "green_rate": round(green / total, 4) if total else 0.0,
    }


# ---------------------------------------------------------------------------
# Oracle scorecard (from comparison_report.json)
# ---------------------------------------------------------------------------
def oracle_scorecard(report_path: Path) -> dict[str, Any]:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    rows = data.get("panels", [])
    verdicts = Counter(r.get("verdict", "unknown") for r in rows)
    compared = sum(verdicts.get(v, 0) for v in ORACLE_COMPARED)
    passed = sum(verdicts.get(v, 0) for v in ORACLE_PASS)
    # Top error reasons help triage what to fix next.
    error_reasons = Counter(
        (r.get("reason", "") or "")[:80]
        for r in rows
        if r.get("verdict") in {"FAIL", "ERROR"} and r.get("reason")
    )
    return {
        "oracle_available": data.get("oracle_available", False),
        "verdicts": dict(verdicts),
        "numerically_compared": compared,
        "numerically_passed": passed,
        "parity_rate_of_compared": round(passed / compared, 4) if compared else 0.0,
        "top_failure_reasons": error_reasons.most_common(8),
    }


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------
def run_fetch(corpus_dir: Path, slice_name: str, manifest: str | None) -> None:
    cmd = [_venv_bin("python"), str(ROOT / "scripts" / "fetch_benchmark_corpus.py"),
           "--output-dir", str(corpus_dir)]
    if slice_name:
        cmd += ["--slice", slice_name]
    if manifest:
        cmd += ["--manifest", manifest]
    _run(cmd)


def run_migrate(corpus_dir: Path, out_dir: Path, native_promql: str) -> Path:
    cmd = [_venv_bin("obs-migrate"), "migrate", "--source", "grafana",
           "--input-dir", str(corpus_dir), "--output-dir", str(out_dir), "--compile"]
    if native_promql == "off":
        cmd.append("--no-native-promql")
    elif native_promql == "on":
        cmd.append("--native-promql")
    _run(cmd, check=False)  # migrate exits non-zero on partial failures; we score from artifacts
    packets = out_dir / "dashboards" / "verification_packets.json"
    if not packets.exists():
        raise SystemExit(f"ERROR: migrate produced no verification_packets.json at {packets}")
    return packets


def run_seed(artifact_dir: Path, es_url: str, api_key: str, extra: list[str]) -> None:
    cmd = [_venv_bin("obs-migrate"), "seed-sample-data", "--artifact-dir", str(artifact_dir),
           "--es-url", es_url, "--api-key", api_key, *extra]
    _run(cmd, check=False)


def run_compare(artifact_dir: Path, es_url: str, api_key: str, report_out: Path,
                window_minutes: int, step_seconds: int, index: str, extra: list[str]) -> Path:
    cmd = [_venv_bin("obs-migrate"), "compare", "--artifact-dir", str(artifact_dir),
           "--es-url", es_url, "--api-key", api_key, "--report-out", str(report_out),
           "--window-minutes", str(window_minutes), "--step-seconds", str(step_seconds), *extra]
    if index:
        cmd += ["--index", index]
    _run(cmd, check=False)  # FAIL verdicts exit 1; we want the report regardless
    if not report_out.exists():
        raise SystemExit(f"ERROR: compare produced no report at {report_out}")
    return report_out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def render_md(card: dict[str, Any]) -> str:
    s = card["static"]
    lines = [
        f"# Benchmark scorecard: {card['label']}",
        "",
        f"- Generated: {card['generated']}",
        f"- Corpus slice: {card.get('slice') or '(provided dir)'}  |  git: {card.get('git_sha','?')}",
        "",
        "## Static coverage (offline, from verification_packets.json)",
        "",
        f"- Dashboards: **{s['dashboards']}**  |  Panels: **{s['panels']}**",
        f"- PromQL panels translated: **{s['promql_panels_translated']}**",
        f"- Green semantic gate: **{s['green_rate'] * 100:.1f}%**",
        "",
        "| Semantic gate | Panels |",
        "|---|---|",
    ]
    for k, v in sorted(s["semantic_gate"].items()):
        lines.append(f"| {k} | {v} |")
    lines += ["", "| Status | Panels |", "|---|---|"]
    for k, v in sorted(s["status"].items()):
        lines.append(f"| {k} | {v} |")

    o = card.get("oracle")
    if o:
        lines += [
            "",
            "## Numeric oracle parity (our ES|QL vs native PROMQL on seeded data)",
            "",
            f"- Oracle available: **{o['oracle_available']}**",
            f"- Numerically compared panels: **{o['numerically_compared']}**",
            f"- Passed (STRICT+FUZZY): **{o['numerically_passed']}**",
            f"- Parity rate of compared: **{o['parity_rate_of_compared'] * 100:.1f}%**",
            "",
            "| Verdict | Panels |",
            "|---|---|",
        ]
        for k, v in sorted(o["verdicts"].items()):
            lines.append(f"| {k} | {v} |")
        if o["top_failure_reasons"]:
            lines += ["", "### Top failure/error reasons", ""]
            for reason, n in o["top_failure_reasons"]:
                lines.append(f"- ({n}) {reason}")
    else:
        lines += ["", "## Numeric oracle parity", "", "_Not run (no --es-url/--api-key)._"]
    return "\n".join(lines) + "\n"


def print_delta(before: dict[str, Any], after: dict[str, Any]) -> None:
    print("\n=== DELTA (after - before) ===", file=sys.stderr)
    bs, as_ = before["static"], after["static"]

    def line(name: str, b, a):
        d = a - b
        sign = "+" if d > 0 else ""
        print(f"  {name:34s} {b!s:>8} -> {a!s:>8}  ({sign}{d})", file=sys.stderr)

    line("panels", bs["panels"], as_["panels"])
    line("promql_panels_translated", bs["promql_panels_translated"], as_["promql_panels_translated"])
    line("green_gate_rate(%)", round(bs["green_rate"] * 100, 1), round(as_["green_rate"] * 100, 1))
    bo, ao = before.get("oracle"), after.get("oracle")
    if bo and ao:
        line("numerically_compared", bo["numerically_compared"], ao["numerically_compared"])
        line("numerically_passed", bo["numerically_passed"], ao["numerically_passed"])
        line("parity_rate_of_compared(%)",
             round(bo["parity_rate_of_compared"] * 100, 1), round(ao["parity_rate_of_compared"] * 100, 1))
        all_verdicts = set(bo["verdicts"]) | set(ao["verdicts"])
        for v in sorted(all_verdicts):
            line(f"verdict:{v}", bo["verdicts"].get(v, 0), ao["verdicts"].get(v, 0))


def git_sha() -> str:
    try:
        return _run(["git", "rev-parse", "--short", "HEAD"], check=False).stdout.strip() or "?"
    except Exception:
        return "?"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", required=True, help="Run label (e.g. 'baseline' or 'after-fixes').")
    p.add_argument("--corpus-dir", required=True, help="Directory of Grafana dashboard JSON to benchmark.")
    p.add_argument("--out", required=True, help="Output directory for migrate artifacts + scorecard.")
    p.add_argument("--fetch", action="store_true", help="Fetch the pinned corpus into --corpus-dir first.")
    p.add_argument("--slice", default="", help="Corpus slice for --fetch (default: manifest default).")
    p.add_argument("--manifest", default="", help="Corpus manifest path for --fetch.")
    p.add_argument("--native-promql", choices=["auto", "on", "off"], default="auto",
                   help="Forwarded to migrate. 'off' forces ES|QL translation (exercises the translator).")
    p.add_argument("--es-url", default="", help="Elasticsearch endpoint for the oracle (enables compare).")
    p.add_argument("--api-key", default="", help="Elasticsearch API key for the oracle.")
    p.add_argument("--seed", action="store_true", help="Seed synthetic data before compare (recommended).")
    p.add_argument("--seed-purge", dest="seed_purge", action="store_true", default=True,
                   help="Purge foreign streams before seeding so leftover data can't shadow the wildcard (default on).")
    p.add_argument("--no-seed-purge", dest="seed_purge", action="store_false",
                   help="Keep pre-existing streams when seeding (disables the default purge).")
    p.add_argument("--window-minutes", type=int, default=60)
    p.add_argument("--step-seconds", type=int, default=300)
    p.add_argument("--index", default="", help="Override oracle index pattern (default: infer per panel).")
    p.add_argument("--insecure", action="store_true", help="Pass --insecure to seed/compare (TLS).")
    p.add_argument("--compare-to", default="", help="Path to a prior scorecard.json to print a delta against.")
    args = p.parse_args(argv)

    corpus_dir = Path(args.corpus_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.fetch:
        run_fetch(corpus_dir, args.slice, args.manifest or None)

    packets = run_migrate(corpus_dir, out_dir, args.native_promql)
    card: dict[str, Any] = {
        "label": args.label,
        "generated": datetime.now(UTC).isoformat(),
        "git_sha": git_sha(),
        "slice": args.slice,
        "corpus_dir": str(corpus_dir),
        "static": static_scorecard(packets),
    }

    tls_extra = ["--insecure"] if args.insecure else []
    if args.es_url and args.api_key:
        artifact_dir = out_dir / "dashboards"
        if args.seed:
            # Seed enough history to cover the compare window, and purge foreign
            # streams by default so leftover/experiment data from a prior run
            # cannot shadow the contract's wildcard and starve the oracle of
            # comparable points. Cadence matches the compare step so every
            # bucket the oracle samples has a seeded point.
            seed_hours = max(1, math.ceil(args.window_minutes / 60))
            seed_extra = [
                "--data-hours", str(seed_hours),
                "--interval-sec", str(args.step_seconds),
                *tls_extra,
            ]
            if args.seed_purge:
                seed_extra.append("--purge-foreign-streams")
            run_seed(artifact_dir, args.es_url, args.api_key, seed_extra)
        report = run_compare(
            artifact_dir, args.es_url, args.api_key, out_dir / "comparison_report.json",
            args.window_minutes, args.step_seconds, args.index, tls_extra,
        )
        card["oracle"] = oracle_scorecard(report)

    scorecard_path = out_dir / "scorecard.json"
    scorecard_path.write_text(json.dumps(card, indent=2), encoding="utf-8")
    (out_dir / "scorecard.md").write_text(render_md(card), encoding="utf-8")
    print(json.dumps({"label": card["label"], "static": card["static"], "oracle": card.get("oracle")}, indent=2))
    print(f"\nscorecard -> {scorecard_path}", file=sys.stderr)

    if args.compare_to:
        before = json.loads(Path(args.compare_to).read_text(encoding="utf-8"))
        print_delta(before, card)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
