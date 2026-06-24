"""Mutation tests for verifier failure modes.

Mutation testing answers: "if this class of migration bug reappears, does the
verifier actually fail?" Each mutation corrupts an otherwise-good report in a
targeted way and records whether the expected finding category appears.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from . import invariants


@dataclass
class MutationResult:
    mutation: str
    expected_category: str
    passed: bool
    observed_categories: list[str]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "mutation": self.mutation,
            "expected_category": self.expected_category,
            "passed": self.passed,
            "observed_categories": list(self.observed_categories),
        }


def _first_panel(report: dict[str, Any]) -> dict[str, Any] | None:
    for dash in report.get("dashboards", []):
        for panel in dash.get("panels", []):
            if isinstance(panel, dict):
                return panel
    return None


def _config(panel: dict[str, Any]) -> dict[str, Any]:
    vir = panel.get("visual_ir") if isinstance(panel.get("visual_ir"), dict) else {}
    pres = vir.get("presentation") if isinstance(vir, dict) else {}
    if not isinstance(pres, dict):
        return {}
    cfg = pres.get("config")
    return cfg if isinstance(cfg, dict) else {}


def mutate_break_accessor(report: dict[str, Any]) -> dict[str, Any]:
    mutated = copy.deepcopy(report)
    panel = _first_panel(mutated)
    if panel:
        cfg = _config(panel)
        cfg.setdefault("breakdown", {})["field"] = "__missing_accessor__"
    return mutated


def mutate_break_composite_legend(report: dict[str, Any]) -> dict[str, Any]:
    mutated = copy.deepcopy(report)
    panel = _first_panel(mutated)
    if panel:
        cfg = _config(panel)
        cfg["breakdown"] = {"field": "legend"}
        cfg["query"] = str(cfg.get("query") or "").replace("legend", "legend_missing")
    return mutated


def mutate_silent_placeholder(report: dict[str, Any]) -> dict[str, Any]:
    mutated = copy.deepcopy(report)
    panel = _first_panel(mutated)
    if panel:
        panel["status"] = "migrated"
        panel["grafana_type"] = "timeseries"
        panel["reasons"] = []
        panel["post_validation_action"] = ""
        vir = panel.setdefault("visual_ir", {})
        vir["presentation"] = {
            "kind": "markdown",
            "config": {"content": "Migration Required"},
        }
    return mutated


_MUTATIONS = {
    "break_accessor": (mutate_break_accessor, "ACCESSOR_BROKEN"),
    "break_composite_legend": (mutate_break_composite_legend, "BREAKDOWN_LEGEND_MISMATCH"),
    "silent_placeholder": (mutate_silent_placeholder, "PLACEHOLDER_DROPPED"),
}


def run_invariant_mutations(report: dict[str, Any]) -> list[MutationResult]:
    results: list[MutationResult] = []
    for name, (mutator, expected) in _MUTATIONS.items():
        mutated = mutator(report)
        findings = invariants.lint_report(mutated)
        observed = sorted({finding.category.value for finding in findings})
        results.append(
            MutationResult(
                mutation=name,
                expected_category=expected,
                passed=expected in observed,
                observed_categories=observed,
            )
        )
    return results


def summarize(results: list[MutationResult]) -> dict[str, Any]:
    failed = [result.to_jsonable() for result in results if not result.passed]
    return {"total": len(results), "passed": len(results) - len(failed), "failed": failed}

