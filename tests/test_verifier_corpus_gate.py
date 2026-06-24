# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the frozen-corpus semantic parity gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import corpus_gate  # noqa: E402


def _report(verdicts: list[str]) -> dict:
    return {
        "panels": [
            {"dashboard": "D", "panel": f"P{i}", "verdict": verdict, "reason": ""}
            for i, verdict in enumerate(verdicts)
        ]
    }


class TestCorpusGate:
    def test_passes_when_within_budgets(self) -> None:
        result = corpus_gate.evaluate_reports(
            [_report(["STRICT_PASS", "FUZZY_PASS", "SHAPE_PASS"])],
            max_shape_pass=1,
        )
        assert result.ok
        assert result.counts["STRICT_PASS"] == 1
        assert result.counts["SHAPE_PASS"] == 1

    def test_fails_on_fail_verdict(self) -> None:
        result = corpus_gate.evaluate_reports([_report(["STRICT_PASS", "FAIL"])])
        assert not result.ok
        assert "FAIL/SOURCE_FAIL count 1 exceeds budget 0" in result.reasons
        assert len(result.failures) == 1
        assert result.failures[0]["verdict"] == "FAIL"

    def test_fails_on_error_verdict(self) -> None:
        result = corpus_gate.evaluate_reports([_report(["ERROR"])])
        assert not result.ok
        assert "ERROR count 1 exceeds budget 0" in result.reasons

    def test_shape_pass_budget(self) -> None:
        result = corpus_gate.evaluate_reports(
            [_report(["SHAPE_PASS", "SHAPE_PASS"])],
            max_shape_pass=1,
        )
        assert not result.ok
        assert "SHAPE_PASS count 2 exceeds budget 1" in result.reasons

    def test_combines_multiple_reports(self) -> None:
        result = corpus_gate.evaluate_reports(
            [_report(["STRICT_PASS"]), _report(["FUZZY_PASS", "SKIP"])]
        )
        assert result.ok
        assert result.total == 3
        assert result.counts == {"STRICT_PASS": 1, "FUZZY_PASS": 1, "SKIP": 1}

    def test_load_compare_report(self, tmp_path: Path) -> None:
        path = tmp_path / "compare.json"
        path.write_text(json.dumps(_report(["STRICT_PASS"])))
        assert corpus_gate.load_compare_report(path)["panels"][0]["verdict"] == "STRICT_PASS"

