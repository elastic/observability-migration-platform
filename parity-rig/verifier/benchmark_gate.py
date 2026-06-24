"""PM benchmark-history regression gate.

The internal benchmark UI tracks exactly these run-level success signals:

* dashboards migrated %
* dashboards clean %
* panels migrated %
* panels clean %
* panels verified %
* duration

This module turns a ``benchmark_history.json``-style file into a merge gate: the
latest run is compared with the most recent compatible baseline (same requested
Grafana/Datadog counts and same schema-discovery class), and configurable drops
in the success percentages or duration increases become CI failures.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUCCESS_METRICS = (
    "dashboard_migration_pct",
    "dashboard_clean_pct",
    "panel_migration_pct",
    "panel_clean_pct",
    "panel_verified_pct",
)


@dataclass
class BenchmarkGateResult:
    ok: bool
    current_index: int
    baseline_index: int | None
    current_hash: str = ""
    baseline_hash: str = ""
    regressions: list[dict[str, Any]] = field(default_factory=list)
    skipped_reason: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "current_index": self.current_index,
            "baseline_index": self.baseline_index,
            "current_hash": self.current_hash,
            "baseline_hash": self.baseline_hash,
            "regressions": list(self.regressions),
            "skipped_reason": self.skipped_reason,
        }


def load_history(path: Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError("benchmark history must be a JSON array")
    return [item for item in data if isinstance(item, dict)]


def _aggregate_metrics(parts: list[dict[str, Any]]) -> dict[str, Any]:
    panels_total = panels_ok = panels_warn = panels_nf = panels_manual = panels_skipped = 0
    dashboards = dashboards_ok = dashboards_warn = dashboards_failed = 0
    verification_green = verification_yellow = verification_red = 0
    for item in parts:
        panels_total += int(item.get("panels_total") or 0)
        panels_ok += int(item.get("panels_ok") or 0)
        panels_warn += int(item.get("panels_warn") or 0)
        panels_nf += int(item.get("panels_nf") or 0)
        panels_manual += int(item.get("panels_manual") or 0)
        panels_skipped += int(item.get("panels_skipped") or 0)
        dashboards += int(item.get("dashboards") or 0)
        dashboards_ok += int(item.get("dashboards_ok") or 0)
        dashboards_warn += int(item.get("dashboards_warn") or 0)
        dashboards_failed += int(item.get("dashboards_failed") or 0)
        verification_green += int(item.get("verification_green") or 0)
        verification_yellow += int(item.get("verification_yellow") or 0)
        verification_red += int(item.get("verification_red") or 0)
    migrated = panels_ok + panels_warn
    verified_total = verification_green + verification_yellow + verification_red
    return {
        "panels_total": panels_total,
        "panels_ok": panels_ok,
        "panels_warn": panels_warn,
        "panels_nf": panels_nf,
        "panels_manual": panels_manual,
        "panels_skipped": panels_skipped,
        "dashboards": dashboards,
        "dashboards_ok": dashboards_ok,
        "dashboards_warn": dashboards_warn,
        "dashboards_failed": dashboards_failed,
        "panel_migration_pct": round((migrated / max(panels_total, 1)) * 100, 1),
        "panel_clean_pct": round((panels_ok / max(migrated, 1)) * 100, 1),
        "dashboard_migration_pct": round(((dashboards_ok + dashboards_warn) / max(dashboards, 1)) * 100, 1),
        "dashboard_clean_pct": round((dashboards_ok / max(dashboards_ok + dashboards_warn, 1)) * 100, 1),
        "panel_verified_pct": round((verification_green / max(verified_total, 1)) * 100, 1),
        "verification_green": verification_green,
        "verification_yellow": verification_yellow,
        "verification_red": verification_red,
    }


def run_metrics(run: dict[str, Any]) -> dict[str, Any]:
    """Return the PM UI metric object for a benchmark run."""
    overall = run.get("overall")
    if isinstance(overall, dict) and overall:
        return dict(overall)
    parts = [part for part in (run.get("grafana"), run.get("datadog")) if isinstance(part, dict)]
    return _aggregate_metrics(parts)


def _config_key(run: dict[str, Any]) -> tuple[Any, Any]:
    cfg = run.get("config") if isinstance(run.get("config"), dict) else {}
    return cfg.get("grafana"), cfg.get("datadog")


def _schema_key(run: dict[str, Any]) -> bool:
    # Same convention as the UI: only explicit false means "not schema-aware".
    return run.get("schema_discovery") is not False


def _hash(run: dict[str, Any]) -> str:
    cli = run.get("cli_version") if isinstance(run.get("cli_version"), dict) else {}
    return str(cli.get("hash") or "")


def compatible_baseline_index(history: list[dict[str, Any]], current_index: int) -> int | None:
    current = history[current_index]
    key = _config_key(current)
    schema = _schema_key(current)
    for idx in range(current_index - 1, -1, -1):
        candidate = history[idx]
        if _config_key(candidate) == key and _schema_key(candidate) == schema:
            return idx
    return None


def evaluate_history(
    history: list[dict[str, Any]],
    *,
    current_index: int | None = None,
    max_drop_pp: float = 0.0,
    max_duration_increase_pct: float | None = None,
) -> BenchmarkGateResult:
    if not history:
        return BenchmarkGateResult(ok=True, current_index=-1, baseline_index=None, skipped_reason="empty history")
    if current_index is None:
        current_index = len(history) - 1
    baseline_index = compatible_baseline_index(history, current_index)
    current = history[current_index]
    if baseline_index is None:
        return BenchmarkGateResult(
            ok=True,
            current_index=current_index,
            baseline_index=None,
            current_hash=_hash(current),
            skipped_reason="no compatible baseline",
        )
    baseline = history[baseline_index]
    current_metrics = run_metrics(current)
    baseline_metrics = run_metrics(baseline)
    regressions: list[dict[str, Any]] = []
    for metric in SUCCESS_METRICS:
        before = float(baseline_metrics.get(metric) or 0.0)
        after = float(current_metrics.get(metric) or 0.0)
        drop = round(before - after, 3)
        if drop > max_drop_pp:
            regressions.append({"metric": metric, "baseline": before, "current": after, "drop_pp": drop})
    if max_duration_increase_pct is not None:
        before = float(baseline.get("duration_s") or 0.0)
        after = float(current.get("duration_s") or 0.0)
        if before > 0:
            increase = round(((after - before) / before) * 100, 3)
            if increase > max_duration_increase_pct:
                regressions.append({
                    "metric": "duration_s",
                    "baseline": before,
                    "current": after,
                    "increase_pct": increase,
                })
    return BenchmarkGateResult(
        ok=not regressions,
        current_index=current_index,
        baseline_index=baseline_index,
        current_hash=_hash(current),
        baseline_hash=_hash(baseline),
        regressions=regressions,
    )


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verifier.benchmark_gate",
        description="Gate PM benchmark history for migration-success regressions.",
    )
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--current-index", type=int)
    parser.add_argument("--max-drop-pp", type=float, default=0.0)
    parser.add_argument("--max-duration-increase-pct", type=float)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    result = evaluate_history(
        load_history(args.history),
        current_index=args.current_index,
        max_drop_pp=args.max_drop_pp,
        max_duration_increase_pct=args.max_duration_increase_pct,
    )
    payload = result.to_jsonable()
    if args.output:
        args.output.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

