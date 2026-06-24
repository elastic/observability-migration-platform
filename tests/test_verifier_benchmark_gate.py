# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for PM benchmark-history regression gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import benchmark_gate  # noqa: E402


def _run(
    *,
    h: str,
    grafana: int = 500,
    datadog: int = 671,
    schema: bool = True,
    duration: float = 60.0,
    overall: dict | None = None,
):
    return {
        "cli_version": {"hash": h, "date": f"2026-06-{h[-2:]}"},
        "config": {"grafana": grafana, "datadog": datadog},
        "schema_discovery": schema,
        "duration_s": duration,
        "overall": overall if overall is not None else {
            "dashboard_migration_pct": 100.0,
            "dashboard_clean_pct": 80.0,
            "panel_migration_pct": 75.0,
            "panel_clean_pct": 70.0,
            "panel_verified_pct": 65.0,
            "panels_total": 100,
            "panels_ok": 70,
            "panels_warn": 5,
            "dashboards": 10,
            "dashboards_ok": 8,
            "dashboards_warn": 2,
            "verification_green": 65,
            "verification_yellow": 25,
            "verification_red": 10,
        },
    }


class TestBenchmarkGate:
    def test_detects_success_metric_drop(self) -> None:
        history = [
            _run(h="aaa001"),
            _run(h="bbb002", overall={**_run(h="x")["overall"], "panel_clean_pct": 60.0}),
        ]
        result = benchmark_gate.evaluate_history(history)
        assert not result.ok
        assert result.baseline_index == 0
        assert result.regressions == [
            {"metric": "panel_clean_pct", "baseline": 70.0, "current": 60.0, "drop_pp": 10.0}
        ]

    def test_allows_config_mismatch_by_skipping_baseline(self) -> None:
        history = [_run(h="aaa001", grafana=100), _run(h="bbb002", grafana=500)]
        result = benchmark_gate.evaluate_history(history)
        assert result.ok
        assert result.skipped_reason == "no compatible baseline"

    def test_skips_schema_boundary(self) -> None:
        history = [_run(h="aaa001", schema=False), _run(h="bbb002", schema=True)]
        result = benchmark_gate.evaluate_history(history)
        assert result.ok
        assert result.skipped_reason == "no compatible baseline"

    def test_respects_drop_tolerance(self) -> None:
        history = [
            _run(h="aaa001"),
            _run(h="bbb002", overall={**_run(h="x")["overall"], "panel_verified_pct": 64.6}),
        ]
        assert benchmark_gate.evaluate_history(history, max_drop_pp=0.5).ok
        assert not benchmark_gate.evaluate_history(history, max_drop_pp=0.3).ok

    def test_duration_increase_budget(self) -> None:
        history = [_run(h="aaa001", duration=60.0), _run(h="bbb002", duration=75.0)]
        result = benchmark_gate.evaluate_history(history, max_duration_increase_pct=20.0)
        assert not result.ok
        assert result.regressions == [
            {"metric": "duration_s", "baseline": 60.0, "current": 75.0, "increase_pct": 25.0}
        ]

    def test_first_run_is_ok(self) -> None:
        result = benchmark_gate.evaluate_history([_run(h="aaa001")])
        assert result.ok
        assert result.skipped_reason == "no compatible baseline"

    def test_uses_previous_different_hash_not_same_hash_rerun(self) -> None:
        history = [
            _run(h="aaa001", overall={**_run(h="x")["overall"], "panel_clean_pct": 70.0}),
            _run(h="bbb002", overall={**_run(h="x")["overall"], "panel_clean_pct": 60.0}),
            _run(h="bbb002", overall={**_run(h="x")["overall"], "panel_clean_pct": 59.0}),
        ]
        result = benchmark_gate.evaluate_history(history)
        assert not result.ok
        assert result.baseline_index == 0
        assert result.baseline_hash == "aaa001"

    def test_can_allow_same_hash_baseline_explicitly(self) -> None:
        history = [
            _run(h="aaa001", overall={**_run(h="x")["overall"], "panel_clean_pct": 70.0}),
            _run(h="bbb002", overall={**_run(h="x")["overall"], "panel_clean_pct": 60.0}),
            _run(h="bbb002", overall={**_run(h="x")["overall"], "panel_clean_pct": 59.0}),
        ]
        result = benchmark_gate.evaluate_history(history, require_different_hash=False)
        assert not result.ok
        assert result.baseline_index == 1
        assert result.baseline_hash == "bbb002"

    def test_count_drop_catches_denominator_regression(self) -> None:
        before = _run(h="aaa001")["overall"]
        after = {**before, "panels_total": 80}
        result = benchmark_gate.evaluate_history([
            _run(h="aaa001", overall=before),
            _run(h="bbb002", overall=after),
        ])
        assert not result.ok
        assert {"metric": "panels_total", "baseline": 100, "current": 80, "drop": 20} in result.regressions

    def test_verification_total_drop_is_a_regression(self) -> None:
        before = _run(h="aaa001")["overall"]
        after = {**before, "verification_green": 40, "verification_yellow": 10, "verification_red": 0}
        result = benchmark_gate.evaluate_history([
            _run(h="aaa001", overall=before),
            _run(h="bbb002", overall=after),
        ], max_drop_pp=100)  # ignore pct drop; assert the denominator signal still fires
        assert not result.ok
        assert {"metric": "verification_total", "baseline": 100, "current": 50, "drop": 50} in result.regressions

    def test_count_drop_tolerance(self) -> None:
        before = _run(h="aaa001")["overall"]
        after = {**before, "panels_total": 98}
        history = [_run(h="aaa001", overall=before), _run(h="bbb002", overall=after)]
        assert benchmark_gate.evaluate_history(history, max_count_drop=2).ok
        assert not benchmark_gate.evaluate_history(history, max_count_drop=1).ok

    def test_current_index_out_of_range(self) -> None:
        try:
            benchmark_gate.evaluate_history([_run(h="aaa001")], current_index=3)
        except IndexError as exc:
            assert "out of range" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected IndexError")

    def test_aggregates_grafana_and_datadog_when_overall_missing(self) -> None:
        run = {
            "config": {"grafana": 1, "datadog": 1},
            "grafana": {
                "dashboards": 1, "dashboards_ok": 1, "dashboards_warn": 0,
                "panels_total": 10, "panels_ok": 8, "panels_warn": 1,
                "verification_green": 7, "verification_yellow": 2, "verification_red": 1,
            },
            "datadog": {
                "dashboards": 1, "dashboards_ok": 0, "dashboards_warn": 1,
                "panels_total": 10, "panels_ok": 2, "panels_warn": 8,
                "verification_green": 5, "verification_yellow": 5, "verification_red": 0,
            },
        }
        metrics = benchmark_gate.run_metrics(run)
        assert metrics["panel_migration_pct"] == 95.0
        assert metrics["panel_clean_pct"] == 52.6
        assert metrics["dashboard_migration_pct"] == 100.0
        assert metrics["dashboard_clean_pct"] == 50.0
        assert metrics["panel_verified_pct"] == 60.0

    def test_load_history_requires_array(self, tmp_path: Path) -> None:
        path = tmp_path / "history.json"
        path.write_text(json.dumps({"not": "a list"}))
        try:
            benchmark_gate.load_history(path)
        except ValueError as exc:
            assert "JSON array" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")

