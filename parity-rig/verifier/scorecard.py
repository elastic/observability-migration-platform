"""Layer 13 - fidelity scorecard and regression ratchet.

Turns the Layer-9 invariant findings for a migration output into a compact,
comparable scorecard and enforces a one-way ratchet against a committed
baseline: error counts (overall and per category) may only stay the same or
improve. Any increase fails, which is what lets a corpus run guard against
fidelity regressions in CI.

Usage::

    # write/refresh a baseline from a migration output dir
    python -m verifier.scorecard --migration-out OUT/dashboards \
        --baseline parity-rig/benchmark/fidelity_baseline.json --update

    # check a fresh run against the committed baseline (CI gate)
    python -m verifier.scorecard --migration-out OUT/dashboards \
        --baseline parity-rig/benchmark/fidelity_baseline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import invariants
from .invariants import Finding, InvariantCategory, Severity

SCORECARD_VERSION = 1


def build_scorecard(findings: list[Finding], *, panels_total: int = 0) -> dict[str, Any]:
    """Summarize findings into a comparable scorecard structure."""
    by_category: dict[str, dict[str, int]] = {
        c.value: {"total": 0, "errors": 0} for c in InvariantCategory
    }
    errors = 0
    for f in findings:
        bucket = by_category.setdefault(f.category.value, {"total": 0, "errors": 0})
        bucket["total"] += 1
        if f.severity is Severity.ERROR:
            bucket["errors"] += 1
            errors += 1
    return {
        "version": SCORECARD_VERSION,
        "totals": {
            "panels": panels_total,
            "findings": len(findings),
            "errors": errors,
        },
        "by_category": by_category,
    }


def _count_panels(report: dict[str, Any]) -> int:
    return sum(len(d.get("panels", [])) for d in report.get("dashboards", []))


def scorecard_for_migration(
    migration_dir: Path,
    *,
    columns_oracle: invariants.ColumnsOracle | None = None,
) -> dict[str, Any]:
    report = json.loads((Path(migration_dir) / "migration_report.json").read_text())
    findings = invariants.lint_report(report, columns_oracle=columns_oracle)
    return build_scorecard(findings, panels_total=_count_panels(report))


def compare_to_baseline(
    current: dict[str, Any], baseline: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Ratchet check: ``current`` errors must not exceed ``baseline`` errors,
    and panel coverage (the denominator) must not silently shrink.

    Returns ``(ok, regressions)``. Improvements (fewer errors, more panels) are
    always allowed. A drop in ``totals.panels`` is a regression: a collapsing
    denominator makes an error-only ratchet look "clean" (0/0) while coverage
    silently erodes. Intentional corpus shrinks re-baseline with ``--update``.
    """
    regressions: list[str] = []
    cur_total = current.get("totals", {}).get("errors", 0)
    base_total = baseline.get("totals", {}).get("errors", 0)
    if cur_total > base_total:
        regressions.append(
            f"total errors increased: {base_total} -> {cur_total}"
        )
    cur_panels = current.get("totals", {}).get("panels", 0)
    base_panels = baseline.get("totals", {}).get("panels", 0)
    if cur_panels < base_panels:
        regressions.append(
            f"panel coverage dropped: {base_panels} -> {cur_panels}"
        )
    cur_cats = current.get("by_category", {})
    base_cats = baseline.get("by_category", {})
    for category, bucket in cur_cats.items():
        cur_err = bucket.get("errors", 0)
        base_err = base_cats.get(category, {}).get("errors", 0)
        if cur_err > base_err:
            regressions.append(
                f"{category} errors increased: {base_err} -> {cur_err}"
            )
    return (not regressions, regressions)


def load_baseline(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def save_baseline(path: Path, scorecard: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(scorecard, indent=2) + "\n")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verifier.scorecard",
        description="Fidelity scorecard + regression ratchet (Layer 13).",
    )
    p.add_argument("--migration-out", type=Path, required=True,
                   help="Migration output dir containing migration_report.json.")
    p.add_argument("--baseline", type=Path, required=True,
                   help="Path to the committed baseline scorecard JSON.")
    p.add_argument("--update", action="store_true",
                   help="Write the current scorecard to --baseline (refresh).")
    p.add_argument("--es-url", type=str, default="")
    p.add_argument("--api-key", type=str, default="")
    p.add_argument("--live-oracle", action="store_true",
                   help="Resolve columns via the live cluster (needs --es-url/--api-key).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    oracle = None
    if args.live_oracle and args.es_url and args.api_key:
        oracle = invariants.make_es_columns_oracle(args.es_url, args.api_key)
    current = scorecard_for_migration(args.migration_out, columns_oracle=oracle)

    if args.update:
        save_baseline(args.baseline, current)
        print(f"wrote baseline {args.baseline} (errors={current['totals']['errors']})")
        return 0

    if not args.baseline.exists():
        print(f"error: baseline {args.baseline} not found; run with --update first",
              file=sys.stderr)
        return 2

    baseline = load_baseline(args.baseline)
    ok, regressions = compare_to_baseline(current, baseline)
    print(
        f"scorecard: panels={current['totals']['panels']} "
        f"findings={current['totals']['findings']} "
        f"errors={current['totals']['errors']} "
        f"(baseline errors={baseline.get('totals', {}).get('errors', 0)})"
    )
    if not ok:
        print("FIDELITY REGRESSION:", file=sys.stderr)
        for r in regressions:
            print(f"  - {r}", file=sys.stderr)
        return 1
    print("ok: no fidelity regression vs baseline")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
