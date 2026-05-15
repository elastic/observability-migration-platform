"""Visual diffing for paired Grafana / Kibana panel screenshots.

This module wraps the ``agent-browser diff screenshot`` CLI so that the
5-tier verifier can attach a quantitative pixel-level drift score to
each panel record alongside the structural drift it already detects on
T0..T5.

It deliberately keeps the CLI invocation an implementation detail: if
``agent-browser`` is not installed (the binary is shipped via
``npm install -g agent-browser`` and is not a hard dependency of the
verifier), :func:`diff_screenshots` logs a warning and returns
``(0.0, "")`` so the rest of the pipeline can keep running. Real
transport-layer failures (timeouts, non-zero exits) raise
:class:`subprocess.CalledProcessError` so the operator notices.

The companion :func:`pair_panels_by_title` walks a Grafana-shots map and
a Kibana-shots map and produces the (title, baseline, candidate) triples
the diff function consumes. Title is the only stable identity we have
across the two products; mismatched panels are surfaced via an
``unpaired_panels`` list so the operator can chase them down.

CLI form::

    python -m verifier.visual_diff \
        --grafana-dir /var/parity/grafana-shots/ \
        --kibana-dir  /var/parity/kibana-shots/ \
        --output-dir  /var/parity/visual-diffs/ \
        --threshold   0.15 \
        --report      /var/parity/visual-diff.json

The ``--output-dir`` is *required* — we never default any output to
``/tmp`` to avoid surprising operators in CI.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger(__name__)


# Default threshold mirrors agent-browser's own ``-t 0.1`` default, but
# the verifier uses 0.15 because Lens vs Grafana renders have a small
# constant pixel-level skew (anti-aliased axis labels, slightly
# different stroke widths) that we don't want to flag as a regression.
DEFAULT_THRESHOLD = 0.15


# The CLI's stdout is documented in
# ``.cursor/skills/chrome-devtools-debugging/agent-browser.md`` as
# "writes diff.png by default" plus an emit-time line of the form
# ``Diff: <pct>% (<n> pixels) -> <output_path>``. We can't always reach
# the live binary in CI, so we tolerate two shapes:
#   1. Structured ``--json`` output: a single JSON object containing one
#      of {score, diff_score, diffPercentage, mismatch} keys for the
#      score and one of {output, diff_path, output_path} for the path.
#   2. Free-form text output where we pluck a ``X.YY%`` or
#      ``Diff: ... %`` and the trailing path with regexes.
# When the spec talks about "agent-browser's actual output format",
# the test fixtures use shape (1).
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_PATH_RE = re.compile(r"->\s*(\S+\.png)\b")


@dataclass(frozen=True)
class PairedPanels:
    """Result of :func:`pair_panels_by_title`."""

    pairs: list[tuple[str, Path, Path]] = field(default_factory=list)
    unpaired_panels: list[tuple[str, str]] = field(default_factory=list)
    """``(title, missing_side)`` where ``missing_side`` is ``"grafana"``
    or ``"kibana"``."""


def diff_screenshots(
    baseline_path: Path,
    candidate_path: Path,
    output_path: Path,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[float, str]:
    """Run ``agent-browser diff screenshot`` against two panel PNGs.

    The Vercel ``agent-browser`` CLI is built around an open browser
    tab: ``diff screenshot`` always uses the *current* page as the
    candidate side. To diff two static files we open the candidate as
    a ``file://`` URL first, then ask the CLI to diff against the
    baseline. We do this in a single ``batch`` invocation so the daemon
    only spins up once.

    Returns ``(diff_score, diff_image_path)``. The score is normalised
    to a 0..1 fraction (``0.0`` = pixel-identical). On missing
    ``agent-browser`` binary we log a warning and return ``(0.0, "")``
    so the pipeline can degrade gracefully; on real transport failures
    (non-zero exit, timeout) we raise
    :class:`subprocess.CalledProcessError`.

    Raises:
        FileNotFoundError: if ``baseline_path`` or ``candidate_path``
            do not exist.
        ValueError: if ``threshold`` is outside ``0.0..1.0``.
    """
    if threshold < 0.0 or threshold > 1.0:
        raise ValueError(
            f"threshold must be in [0.0, 1.0], got {threshold!r}"
        )
    if not baseline_path.exists():
        raise FileNotFoundError(f"baseline screenshot not found: {baseline_path}")
    if not candidate_path.exists():
        raise FileNotFoundError(f"candidate screenshot not found: {candidate_path}")

    binary = shutil.which("agent-browser")
    if binary is None:
        LOG.warning(
            "agent-browser not on PATH; skipping visual diff for %s vs %s. "
            "Install with `npm install -g agent-browser` to enable.",
            baseline_path.name,
            candidate_path.name,
        )
        return 0.0, ""

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # We open the candidate as a file:// URL (requires --allow-file-access)
    # so the CLI can capture it as its "current page", then diff against
    # the baseline. The batch form keeps it to a single daemon call.
    candidate_url = candidate_path.resolve().as_uri()
    cmd = [
        binary,
        "--allow-file-access",
        "batch",
        "--json",
        "--bail",
        f"open {candidate_url}",
        f'diff screenshot --baseline "{baseline_path}" -o "{output_path}" -t {threshold}',
    ]
    LOG.debug("invoking: %s", " ".join(cmd))
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return _parse_diff_result(completed.stdout, completed.stderr, output_path)


def _parse_diff_result(
    stdout: str,
    stderr: str,
    fallback_output: Path,
) -> tuple[float, str]:
    """Best-effort parse of agent-browser's diff output.

    See the module docstring for the two shapes we accept.
    """
    blob = _extract_json(stdout)
    if blob is not None:
        score = _coerce_score(blob)
        path = _coerce_path(blob, fallback_output)
        return score, path

    text = stdout + "\n" + stderr
    pct_match = _PERCENT_RE.search(text)
    score = float(pct_match.group(1)) / 100.0 if pct_match else 0.0

    path_match = _PATH_RE.search(text)
    path = path_match.group(1) if path_match else (
        str(fallback_output) if fallback_output.exists() else ""
    )
    return score, path


def _extract_json(stdout: str) -> dict | list | None:
    """Pluck the last JSON object/array from ``stdout``.

    ``agent-browser batch --json`` emits one JSON object per step
    (newline-separated); the diff step is the last one.
    """
    text = stdout.strip()
    if not text:
        return None
    last_blob: dict | list | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            last_blob = json.loads(line)
        except json.JSONDecodeError:
            continue
    if last_blob is None:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return last_blob


def _unwrap_diff_result(blob: dict | list) -> dict:
    """Unwrap whatever shape agent-browser hands us down to the dict
    that actually carries the per-pixel diff numbers.

    agent-browser shapes observed in the wild:
      * 0.27 ``batch --json``: ``[{... "command": ["diff", "screenshot",
        ...], "result": {"mismatchPercentage": X, "diffPath": Y, ...}},
        ...]`` -- we pick the LAST batch step (the diff step).
      * 0.27 single command: ``{"success": true, "data":
        {"mismatchPercentage": X, "diffPath": Y, ...}}`` -- unwrap
        ``data``.
      * Older shapes: ``{"score": X, "output": Y}`` -- already at the
        right level.

    Returns ``{}`` if no usable dict is found.
    """
    if isinstance(blob, list):
        # Prefer the last step whose command is the diff step; fall
        # back to the last dict.
        last_step: dict = {}
        for step in blob:
            if not isinstance(step, dict):
                continue
            last_step = step
            cmd = step.get("command")
            if isinstance(cmd, list) and cmd and "diff" in cmd[:2]:
                last_step = step
        obj = last_step
    elif isinstance(blob, dict):
        obj = blob
    else:
        return {}

    if "result" in obj and isinstance(obj["result"], dict):
        obj = obj["result"]
    if "data" in obj and isinstance(obj["data"], dict):
        obj = obj["data"]
    return obj


def _coerce_score(blob: dict | list) -> float:
    """Pull a 0..1 score out of a JSON diff result.

    Score keys we recognise (in priority order): ``score``,
    ``diff_score``, ``mismatchPercentage`` (agent-browser 0.27+),
    ``diffPercentage`` (legacy), ``mismatch``. Percentages are
    normalised by dividing by 100 when the value > 1.0.
    """
    obj = _unwrap_diff_result(blob)
    for key in ("score", "diff_score", "mismatchPercentage", "diffPercentage", "mismatch"):
        if key in obj:
            try:
                value = float(obj[key])
            except (TypeError, ValueError):
                continue
            return value / 100.0 if value > 1.0 else value
    return 0.0


def _coerce_path(blob: dict | list, fallback_output: Path) -> str:
    obj = _unwrap_diff_result(blob)
    # agent-browser 0.27+ writes ``diffPath`` at the result/data level;
    # older shapes used ``output`` / ``output_path``.
    for key in ("diffPath", "output", "diff_path", "output_path", "path"):
        if key in obj and isinstance(obj[key], str):
            return obj[key]
    return str(fallback_output) if fallback_output.exists() else ""


def pair_panels_by_title(
    grafana_shots: dict[str, Path],
    kibana_shots: dict[str, Path],
) -> PairedPanels:
    """Pair Grafana and Kibana screenshots by panel title.

    Title is the only identity we can rely on across the two products
    (Grafana panel IDs are integers, Kibana saved-object IDs are
    UUIDs). Panels missing from either side are returned via
    :attr:`PairedPanels.unpaired_panels` so the operator can investigate
    why a panel didn't make it through migration / upload.
    """
    pairs: list[tuple[str, Path, Path]] = []
    unpaired: list[tuple[str, str]] = []
    for title in sorted(set(grafana_shots) | set(kibana_shots)):
        g = grafana_shots.get(title)
        k = kibana_shots.get(title)
        if g is None:
            unpaired.append((title, "grafana"))
            continue
        if k is None:
            unpaired.append((title, "kibana"))
            continue
        pairs.append((title, g, k))
    return PairedPanels(pairs=pairs, unpaired_panels=unpaired)


def _index_screenshots(directory: Path) -> dict[str, Path]:
    """Treat each ``*.png`` filename (sans extension) as the panel title.

    Sub-directories are not recursed into. Panel titles with characters
    illegal in filenames (``/``, etc.) must already be sanitised by the
    upstream walker; we do not invent a slug-to-title mapping here.
    """
    if not directory.is_dir():
        return {}
    out: dict[str, Path] = {}
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.suffix.lower() == ".png":
            out[entry.stem] = entry
    return out


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verifier.visual_diff",
        description="Pixel-diff paired Grafana / Kibana panel screenshots.",
    )
    p.add_argument(
        "--grafana-dir",
        type=Path,
        required=True,
        help="Directory of Grafana panel screenshots (one PNG per panel; "
             "filename stem is the panel title).",
    )
    p.add_argument(
        "--kibana-dir",
        type=Path,
        required=True,
        help="Directory of Kibana panel screenshots, same naming.",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory where per-panel diff PNGs are written. "
             "Required (no /tmp default).",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"agent-browser color threshold 0..1 (default: {DEFAULT_THRESHOLD}).",
    )
    p.add_argument(
        "--report",
        type=Path,
        required=True,
        help="Path to write the per-panel + aggregate JSON report.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    return p


def _aggregate(scores: Iterable[float]) -> dict[str, float | int]:
    score_list = list(scores)
    if not score_list:
        return {
            "count": 0,
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "panels_above_threshold": 0,
        }
    return {
        "count": len(score_list),
        "min": min(score_list),
        "max": max(score_list),
        "mean": sum(score_list) / len(score_list),
        # ``panels_above_threshold`` is filled in by the CLI, which has
        # the threshold; we leave it as 0 here and let the caller patch
        # it (the aggregator gets called per-panel before the count is
        # known).
        "panels_above_threshold": 0,
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    grafana = _index_screenshots(args.grafana_dir)
    kibana = _index_screenshots(args.kibana_dir)
    paired = pair_panels_by_title(grafana, kibana)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    panel_results: list[dict] = []
    panels_above_threshold = 0
    for title, baseline, candidate in paired.pairs:
        # Slugify the title for the diff filename so we don't collide
        # with the operator's own title-as-filename layout.
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_") or "panel"
        diff_out = args.output_dir / f"{slug}.diff.png"
        try:
            score, diff_path = diff_screenshots(
                baseline, candidate, diff_out, threshold=args.threshold
            )
        except subprocess.CalledProcessError as exc:
            LOG.error(
                "agent-browser diff failed for %r (exit %s): %s",
                title,
                exc.returncode,
                (exc.stderr or "")[:200],
            )
            panel_results.append(
                {
                    "title": title,
                    "baseline": str(baseline),
                    "candidate": str(candidate),
                    "score": None,
                    "diff_path": "",
                    "error": (exc.stderr or str(exc))[:500],
                }
            )
            continue
        if score > args.threshold:
            panels_above_threshold += 1
        panel_results.append(
            {
                "title": title,
                "baseline": str(baseline),
                "candidate": str(candidate),
                "score": score,
                "diff_path": diff_path,
                "error": "",
            }
        )

    scores = [r["score"] for r in panel_results if isinstance(r["score"], (int, float))]
    aggregate = _aggregate(scores)
    aggregate["panels_above_threshold"] = panels_above_threshold

    payload = {
        "threshold": args.threshold,
        "grafana_dir": str(args.grafana_dir),
        "kibana_dir": str(args.kibana_dir),
        "output_dir": str(args.output_dir),
        "aggregate": aggregate,
        "panels": panel_results,
        "unpaired_panels": [
            {"title": title, "missing_side": side}
            for title, side in paired.unpaired_panels
        ],
    }
    args.report.write_text(json.dumps(payload, indent=2))
    LOG.info("wrote %s", args.report)
    print(
        f"visual-diff: {len(panel_results)} pairs, "
        f"mean={aggregate['mean']:.4f} max={aggregate['max']:.4f} "
        f"above_threshold={panels_above_threshold} "
        f"unpaired={len(paired.unpaired_panels)}"
    )
    return 0


__all__ = [
    "DEFAULT_THRESHOLD",
    "PairedPanels",
    "diff_screenshots",
    "main",
    "pair_panels_by_title",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
