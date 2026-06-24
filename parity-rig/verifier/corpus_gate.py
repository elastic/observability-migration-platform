"""Frozen-corpus semantic parity gate.

``obs-migrate compare`` writes per-panel verdicts for the native-PROMQL vs ES|QL
semantic oracle. This module turns one or more compare reports into a small CI
gate: semantic FAIL/ERROR counts must stay within explicit budgets, and
SHAPE_PASS can be bounded separately for known approximations.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CorpusGateResult:
    total: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True
    reasons: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "counts": dict(self.counts),
            "failures": list(self.failures),
            "ok": self.ok,
            "reasons": list(self.reasons),
        }


def load_compare_report(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def evaluate_reports(
    reports: list[dict[str, Any]],
    *,
    max_fail: int = 0,
    max_error: int = 0,
    max_shape_pass: int | None = None,
) -> CorpusGateResult:
    result = CorpusGateResult()
    for report in reports:
        for panel in report.get("panels", []):
            verdict = str(panel.get("verdict") or "")
            if not verdict:
                continue
            result.total += 1
            result.counts[verdict] = result.counts.get(verdict, 0) + 1
            if verdict in {"FAIL", "ERROR", "SOURCE_FAIL"}:
                result.failures.append(panel)

    fail_count = result.counts.get("FAIL", 0) + result.counts.get("SOURCE_FAIL", 0)
    error_count = result.counts.get("ERROR", 0)
    shape_count = result.counts.get("SHAPE_PASS", 0)
    if fail_count > max_fail:
        result.reasons.append(f"FAIL/SOURCE_FAIL count {fail_count} exceeds budget {max_fail}")
    if error_count > max_error:
        result.reasons.append(f"ERROR count {error_count} exceeds budget {max_error}")
    if max_shape_pass is not None and shape_count > max_shape_pass:
        result.reasons.append(f"SHAPE_PASS count {shape_count} exceeds budget {max_shape_pass}")
    result.ok = not result.reasons
    return result


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verifier.corpus_gate",
        description="Gate one or more obs-migrate compare reports for corpus CI.",
    )
    parser.add_argument("--report", action="append", type=Path, required=True)
    parser.add_argument("--max-fail", type=int, default=0)
    parser.add_argument("--max-error", type=int, default=0)
    parser.add_argument("--max-shape-pass", type=int)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    reports = [load_compare_report(path) for path in args.report]
    result = evaluate_reports(
        reports,
        max_fail=args.max_fail,
        max_error=args.max_error,
        max_shape_pass=args.max_shape_pass,
    )
    payload = result.to_jsonable()
    if args.output:
        args.output.write_text(json.dumps(payload, indent=2))
    print(json.dumps({"ok": result.ok, "counts": result.counts, "reasons": result.reasons}, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

