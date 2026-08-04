# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Census of every ratio-over-a-count in the gate layer.

Four gates once reported success on a zero denominator (``0c4f3a2``). Fixing them
one by one leaves the next one to be found by hand, so this makes the *set*
enumerable: an AST scan finds every division whose denominator is a count, and
each site must be classified as

* :class:`Guarded` — a verdict depends on it and something refuses a zero
  denominator (named, and cross-checked against ``EMPTY_INPUT_GATES`` or against a
  ``raise`` in the same module);
* :class:`Ratcheted` — it feeds a percentage compared against a committed
  baseline, where a collapsing denominator is itself a gated regression (the
  named metric must really be in the gate's metric tuples);
* :class:`DisplayOnly` — no verdict depends on it, so ``0%`` for an empty run is
  the honest answer.

An unclassified site fails; so does a classification whose site has gone. The
point is not the classification, it is that adding a ratio to a gate forces the
author to say which of the three it is.

**Scope: the gate layer** — ``scripts/``, ``parity-rig/verifier/`` and
``observability_migration/core/reporting/``. That is where a number turns into a
pass/fail or into a figure someone quotes. Arithmetic inside the translator
(rollup spans, series diffs, grid geometry) is not a gate denominator and is
deliberately out of scope; widening the scan there would bury the 15 sites that
matter under a hundred that do not.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from tests.vacuity.registry import EMPTY_INPUT_GATES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

GATE_LAYER = (
    "scripts",
    "parity-rig/verifier",
    "observability_migration/core/reporting",
)

# A denominator is count-like when it is a length, or is named like a tally.
_COUNT_LIKE = re.compile(
    # a length, or a floor over one (``max(panels_total, 1)`` is the idiom that
    # turns a zero denominator into a silent 0.0 rather than an error)
    r"^(?:len\(|max\()"
    r"|^\b(?:total|totals|count|counts|denominator|panels|records|widgets|dashboards"
    r"|queries|entries|sent|verified|checked|examined)\w*\b"
    r"|^\w*_(?:total|count|counts|panels|dashboards)\b",
    re.IGNORECASE,
)
# Path joins (``out_dir / "x.json"``) and float-epsilon floors
# (``max(abs(v), 1e-9)``) are divisions in the AST but not ratios over a count.
_NOT_A_COUNT = re.compile(r"['\"]|\babs\(|\d*\.\d|e-\d")


@dataclass(frozen=True)
class Site:
    module: str
    function: str
    denominator: str

    def __str__(self) -> str:
        return f"{self.module}::{self.function}() denominator {self.denominator!r}"


@dataclass(frozen=True)
class Guarded:
    why: str
    #: A ``EMPTY_INPUT_GATES`` id that proves the refusal is tested.
    gate: str = ""
    #: Or the exception the enclosing module raises instead.
    raises: str = ""


@dataclass(frozen=True)
class Ratcheted:
    why: str
    #: The metric this ratio is emitted as; must be gated against a baseline.
    metric: str = ""


@dataclass(frozen=True)
class DisplayOnly:
    why: str


CENSUS: dict[Site, Guarded | Ratcheted | DisplayOnly] = {
    Site(
        "scripts/validate_panel_queries.py",
        "broken_percentage",
        "total",
    ): Guarded(
        why="the broken-panel percentage the MAX_BROKEN_PCT threshold is compared against",
        gate="scripts/validate_panel_queries.py::broken_percentage",
        raises="EmptyCorpusError",
    ),
    Site(
        "parity-rig/verifier/benchmark_gate.py",
        "_aggregate_metrics",
        "max(panels_total, 1)",
    ): Ratcheted(
        why="panels_total is itself a COUNT_METRIC, so a denominator drop is a regression",
        metric="panels_total",
    ),
    Site(
        "parity-rig/verifier/benchmark_gate.py",
        "_aggregate_metrics",
        "max(migrated, 1)",
    ): Ratcheted(
        why=(
            "emitted as panel_clean_pct; if migrated collapses so does panels_ok, and "
            "the percentage drops rather than dividing out to a flattering number"
        ),
        metric="panel_clean_pct",
    ),
    Site(
        "parity-rig/verifier/benchmark_gate.py",
        "_aggregate_metrics",
        "max(dashboards, 1)",
    ): Ratcheted(
        why="dashboards is itself a COUNT_METRIC",
        metric="dashboards",
    ),
    Site(
        "parity-rig/verifier/benchmark_gate.py",
        "_aggregate_metrics",
        "max(dashboards_ok + dashboards_warn, 1)",
    ): Ratcheted(
        why="emitted as dashboard_clean_pct, compared against the baseline",
        metric="dashboard_clean_pct",
    ),
    Site(
        "parity-rig/verifier/benchmark_gate.py",
        "_aggregate_metrics",
        "max(verified_total, 1)",
    ): Ratcheted(
        why=(
            "emitted as panel_verified_pct, and the denominator is reported as "
            "verification_total, itself a COUNT_METRIC — the denominator-drop case "
            "CLAUDE.md calls out explicitly"
        ),
        metric="verification_total",
    ),
    Site(
        "parity-rig/verifier/visual_diff.py",
        "_aggregate",
        "len(score_list)",
    ): Guarded(
        why="mean visual-diff score; an empty score list returns the zero summary above",
        raises="",
    ),
    Site(
        "scripts/parity_promql_esql_oracle.py",
        "compute_diff",
        "len(rel)",
    ): Guarded(
        why="mean relative error; guarded by `if rel else 0.0` with the count returned alongside",
        raises="",
    ),
    Site(
        "scripts/benchmark_corpus.py",
        "static_scorecard",
        "total",
    ): DisplayOnly(
        why=(
            "green_rate is reported in the corpus scorecard and the before/after table "
            "only; the regression gate reads panels_total / verification_total through "
            "benchmark_gate, never this field"
        ),
    ),
    Site(
        "scripts/audit_pipeline.py",
        "_section_appendix_stats",
        "total",
    ): DisplayOnly(
        why="percentages in a generated trace-doc appendix; no verdict reads them",
    ),
    Site(
        "observability_migration/core/reporting/report.py",
        "pct",
        "total",
    ): DisplayOnly(
        why="report formatting helper; an empty run legitimately prints 0%",
    ),
    Site(
        "observability_migration/core/reporting/summary_md.py",
        "_pct",
        "total",
    ): DisplayOnly(
        why="summary-markdown formatting helper; an empty run legitimately prints 0%",
    ),
}


class _RatioVisitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self._functions: list[str] = []
        self.sites: list[Site] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div | ast.FloorDiv):
            denominator = ast.unparse(node.right)
            if not _NOT_A_COUNT.search(denominator) and _COUNT_LIKE.match(denominator):
                self.sites.append(
                    Site(
                        module=self.module,
                        function=self._functions[-1] if self._functions else "<module>",
                        denominator=denominator,
                    )
                )
        self.generic_visit(node)


def discover_ratio_sites() -> set[Site]:
    found: set[Site] = set()
    for directory in GATE_LAYER:
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            visitor = _RatioVisitor(str(path.relative_to(REPO_ROOT)))
            visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
            found.update(visitor.sites)
    return found


def test_the_scan_still_finds_ratios():
    """A scan that finds nothing would classify nothing and pass."""
    found = discover_ratio_sites()
    assert len(found) >= 10, (
        f"the ratio scan found only {len(found)} site(s) across {GATE_LAYER}; it has "
        f"gone blind and would pass whatever the gate layer does"
    )


def test_every_gate_layer_ratio_is_classified():
    unclassified = sorted(str(site) for site in discover_ratio_sites() - set(CENSUS))
    assert not unclassified, (
        "a new ratio-over-a-count appeared in the gate layer and is unclassified:\n  "
        + "\n  ".join(unclassified)
        + "\n\nClassify each in tests/vacuity/test_ratio_denominators.py::CENSUS as "
        "Guarded (a verdict depends on it — and register the refusal in "
        "EMPTY_INPUT_GATES), Ratcheted (it feeds a baseline-compared metric), or "
        "DisplayOnly (nothing decides anything on it). Four gates once reported "
        "success on a zero denominator; this is the decision that stops the fifth."
    )


def test_no_stale_census_entries():
    """A classification whose site is gone hides the next one behind stale rows."""
    found = discover_ratio_sites()
    stale = sorted(str(site) for site in set(CENSUS) - found)
    assert not stale, (
        "these CENSUS entries no longer match any code (renamed or removed):\n  "
        + "\n  ".join(stale)
    )


def test_guarded_sites_name_a_real_guard():
    registered = {gate.gate for gate in EMPTY_INPUT_GATES}
    for site, classification in CENSUS.items():
        if not isinstance(classification, Guarded):
            continue
        assert classification.why, f"{site}: Guarded needs a why"
        if classification.gate:
            assert classification.gate in registered, (
                f"{site} claims gate {classification.gate!r}, which is not in "
                f"EMPTY_INPUT_GATES: {sorted(registered)}"
            )
        if classification.raises:
            source = (REPO_ROOT / site.module).read_text(encoding="utf-8")
            assert f"raise {classification.raises}" in source, (
                f"{site} claims it raises {classification.raises}, which the module "
                f"never raises"
            )


def test_ratcheted_sites_name_a_gated_metric():
    """A ratio is only safely ratcheted if its metric is actually compared."""
    import sys

    if str(REPO_ROOT / "parity-rig") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "parity-rig"))
    from verifier import benchmark_gate

    gated = set(benchmark_gate.SUCCESS_METRICS) | set(benchmark_gate.COUNT_METRICS)
    for site, classification in CENSUS.items():
        if not isinstance(classification, Ratcheted):
            continue
        assert classification.metric in gated, (
            f"{site} is classified Ratcheted on {classification.metric!r}, but that "
            f"metric is in neither SUCCESS_METRICS nor COUNT_METRICS, so nothing "
            f"compares it against a baseline: {sorted(gated)}"
        )


def test_display_only_sites_state_why_nothing_decides_on_them():
    for site, classification in CENSUS.items():
        if isinstance(classification, DisplayOnly):
            assert len(classification.why) > 40, (
                f"{site}: DisplayOnly needs a reason nobody has to re-derive"
            )
