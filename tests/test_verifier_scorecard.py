# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the Layer-13 fidelity scorecard + regression ratchet."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import scorecard  # noqa: E402
from verifier.invariants import Finding, InvariantCategory, Severity  # noqa: E402


def _finding(category: InvariantCategory, severity: Severity = Severity.ERROR) -> Finding:
    return Finding(category, severity, "panel", "dash", "msg")


class TestBuildScorecard:
    def test_counts_errors_and_warnings(self) -> None:
        findings = [
            _finding(InvariantCategory.ACCESSOR_BROKEN),
            _finding(InvariantCategory.ACCESSOR_BROKEN),
            _finding(InvariantCategory.VISUAL_SEMANTIC_DRIFT, Severity.WARNING),
        ]
        card = scorecard.build_scorecard(findings, panels_total=10)
        assert card["totals"]["panels"] == 10
        assert card["totals"]["findings"] == 3
        assert card["totals"]["errors"] == 2
        assert card["by_category"]["ACCESSOR_BROKEN"]["errors"] == 2
        assert card["by_category"]["VISUAL_SEMANTIC_DRIFT"]["errors"] == 0
        assert card["by_category"]["VISUAL_SEMANTIC_DRIFT"]["total"] == 1


class TestRatchet:
    def _card(self, errors_by_cat: dict[str, int]) -> dict:
        findings = []
        for cat, n in errors_by_cat.items():
            findings.extend(_finding(InvariantCategory(cat)) for _ in range(n))
        return scorecard.build_scorecard(findings)

    def test_equal_is_ok(self) -> None:
        base = self._card({"ACCESSOR_BROKEN": 2})
        cur = self._card({"ACCESSOR_BROKEN": 2})
        ok, regressions = scorecard.compare_to_baseline(cur, base)
        assert ok and not regressions

    def test_improvement_is_ok(self) -> None:
        base = self._card({"ACCESSOR_BROKEN": 3})
        cur = self._card({"ACCESSOR_BROKEN": 1})
        ok, regressions = scorecard.compare_to_baseline(cur, base)
        assert ok and not regressions

    def test_regression_total_fails(self) -> None:
        base = self._card({"ACCESSOR_BROKEN": 1})
        cur = self._card({"ACCESSOR_BROKEN": 2})
        ok, regressions = scorecard.compare_to_baseline(cur, base)
        assert not ok
        assert any("total errors increased" in r for r in regressions)

    def test_new_category_regression_fails(self) -> None:
        base = self._card({"ACCESSOR_BROKEN": 1})
        cur = self._card({"ACCESSOR_BROKEN": 1, "PLACEHOLDER_DROPPED": 1})
        ok, regressions = scorecard.compare_to_baseline(cur, base)
        assert not ok
        assert any("PLACEHOLDER_DROPPED" in r for r in regressions)


class TestScorecardForMigration:
    def _write_report(self, tmp_path: Path, panels: list[dict]) -> Path:
        report = {"dashboards": [{"title": "D", "uid": "u", "panels": panels}]}
        (tmp_path / "migration_report.json").write_text(json.dumps(report))
        return tmp_path

    def _broken_panel(self) -> dict:
        return {
            "title": "broken",
            "status": "migrated",
            "reasons": [],
            "post_validation_action": "",
            "esql": "TS metrics-* | STATS v = AVG(rate) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), verb",
            "query_ir": {"output_shape": "time_series", "output_group_fields": ["time_bucket", "verb"]},
            "visual_ir": {
                "presentation": {
                    "kind": "esql",
                    "config": {
                        "type": "line",
                        "query": "TS metrics-* | STATS v = AVG(rate) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), verb",
                        "dimension": {"field": "time_bucket"},
                        "metrics": [{"field": "v"}],
                        "breakdown": {"field": "ghost"},
                    },
                }
            },
        }

    def test_scorecard_and_roundtrip(self, tmp_path: Path) -> None:
        migration_dir = self._write_report(tmp_path, [self._broken_panel()])
        card = scorecard.scorecard_for_migration(migration_dir)
        assert card["totals"]["panels"] == 1
        assert card["totals"]["errors"] >= 1

        baseline_path = tmp_path / "baseline.json"
        scorecard.save_baseline(baseline_path, card)
        loaded = scorecard.load_baseline(baseline_path)
        ok, regressions = scorecard.compare_to_baseline(card, loaded)
        assert ok and not regressions
