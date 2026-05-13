# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for parity-rig/verifier/visual_diff.py.

Covers:
  - the pair_panels_by_title helper (matched + unpaired)
  - diff_screenshots subprocess invocation, JSON output parsing,
    text-output fallback, threshold validation, missing-file handling,
    and graceful degradation when ``agent-browser`` is missing
  - the CLI ``--report`` JSON contains an aggregate (min/max/mean/
    panels_above_threshold) and per-panel rows

The mocked ``agent-browser`` stdout shapes are documented in
``parity-rig/verifier/visual_diff.py`` near ``_PERCENT_RE`` /
``_extract_json``: we match the JSON ``--json`` shape (one JSON object
per batch step on its own line) plus the human-readable
``Diff: 4.2% (...) -> diff.png`` fallback. A real agent-browser
invocation could not be reached in this environment; the parser is
designed to be tolerant of multiple plausible field names so the test
fixtures stay representative.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import visual_diff  # noqa: E402

# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #


def _png(path: Path, byte: bytes = b"\x89PNG\r\n\x1a\nfake") -> Path:
    """Drop a non-empty placeholder so .exists() passes; agent-browser
    is mocked so the actual bytes are never inspected."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(byte)
    return path


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["agent-browser"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# --------------------------------------------------------------------- #
# pair_panels_by_title
# --------------------------------------------------------------------- #


class TestPairPanelsByTitle:
    def test_all_titles_in_both_maps_yield_pairs(self, tmp_path):
        g_a = _png(tmp_path / "g" / "A.png")
        g_b = _png(tmp_path / "g" / "B.png")
        k_a = _png(tmp_path / "k" / "A.png")
        k_b = _png(tmp_path / "k" / "B.png")
        result = visual_diff.pair_panels_by_title(
            {"A": g_a, "B": g_b}, {"A": k_a, "B": k_b}
        )
        assert result.pairs == [
            ("A", g_a, k_a),
            ("B", g_b, k_b),
        ]
        assert result.unpaired_panels == []

    def test_panel_missing_from_kibana_is_unpaired_kibana(self, tmp_path):
        g_a = _png(tmp_path / "g" / "A.png")
        result = visual_diff.pair_panels_by_title({"A": g_a}, {})
        assert result.pairs == []
        assert result.unpaired_panels == [("A", "kibana")]

    def test_panel_missing_from_grafana_is_unpaired_grafana(self, tmp_path):
        k_x = _png(tmp_path / "k" / "X.png")
        result = visual_diff.pair_panels_by_title({}, {"X": k_x})
        assert result.pairs == []
        assert result.unpaired_panels == [("X", "grafana")]

    def test_partially_overlapping_maps(self, tmp_path):
        g_a = _png(tmp_path / "g" / "A.png")
        g_b = _png(tmp_path / "g" / "B.png")
        k_a = _png(tmp_path / "k" / "A.png")
        k_c = _png(tmp_path / "k" / "C.png")
        result = visual_diff.pair_panels_by_title(
            {"A": g_a, "B": g_b}, {"A": k_a, "C": k_c}
        )
        assert result.pairs == [("A", g_a, k_a)]
        # Sorted alphabetically, both unpaired entries surface.
        assert sorted(result.unpaired_panels) == [
            ("B", "kibana"),
            ("C", "grafana"),
        ]

    def test_pairs_are_sorted_for_deterministic_output(self, tmp_path):
        g_b = _png(tmp_path / "g" / "B.png")
        g_a = _png(tmp_path / "g" / "A.png")
        k_b = _png(tmp_path / "k" / "B.png")
        k_a = _png(tmp_path / "k" / "A.png")
        result = visual_diff.pair_panels_by_title(
            {"B": g_b, "A": g_a}, {"B": k_b, "A": k_a}
        )
        assert [t for t, _, _ in result.pairs] == ["A", "B"]


# --------------------------------------------------------------------- #
# diff_screenshots — subprocess + parsing
# --------------------------------------------------------------------- #


class TestDiffScreenshots:
    def test_returns_zero_when_agent_browser_is_missing(self, tmp_path, caplog):
        baseline = _png(tmp_path / "b.png")
        candidate = _png(tmp_path / "c.png")
        out = tmp_path / "out" / "d.png"
        with patch.object(shutil, "which", return_value=None):
            score, path = visual_diff.diff_screenshots(
                baseline, candidate, out, threshold=0.15
            )
        assert score == 0.0
        assert path == ""

    def test_parses_json_score_field(self, tmp_path):
        baseline = _png(tmp_path / "b.png")
        candidate = _png(tmp_path / "c.png")
        out = tmp_path / "out" / "d.png"
        # agent-browser batch --json prints one JSON object per step,
        # newline-separated. The diff step is the last one.
        stdout = (
            json.dumps({"success": True, "data": {"url": "file://..."}})
            + "\n"
            + json.dumps(
                {
                    "success": True,
                    "data": {
                        "score": 0.042,
                        "output": str(out),
                        "threshold": 0.15,
                    },
                }
            )
            + "\n"
        )
        with patch.object(shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(subprocess, "run", return_value=_completed(stdout=stdout)):
            score, path = visual_diff.diff_screenshots(
                baseline, candidate, out, threshold=0.15
            )
        assert score == pytest.approx(0.042)
        assert path == str(out)

    def test_parses_agent_browser_027_mismatchPercentage_and_diffPath(self, tmp_path):
        """Real agent-browser 0.27 output format. Without this parser
        the harness silently scored everything 0.0 even when the
        diff was 66.44%."""
        baseline = _png(tmp_path / "b.png")
        candidate = _png(tmp_path / "c.png")
        out = tmp_path / "diff.png"
        stdout = json.dumps(
            {
                "success": True,
                "error": None,
                "data": {
                    "diffPath": str(out),
                    "differentPixels": 490716,
                    "dimensionMismatch": None,
                    "match": False,
                    "mismatchPercentage": 66.44,
                    "totalPixels": 738560,
                },
            }
        )
        with patch.object(shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(subprocess, "run", return_value=_completed(stdout=stdout)):
            score, path = visual_diff.diff_screenshots(
                baseline, candidate, out, threshold=0.15
            )
        # 66.44% must be normalised to 0..1
        assert score == pytest.approx(0.6644, abs=1e-4)
        assert path == str(out)

    def test_normalises_percentage_score_above_one(self, tmp_path):
        baseline = _png(tmp_path / "b.png")
        candidate = _png(tmp_path / "c.png")
        out = tmp_path / "d.png"
        stdout = json.dumps({"data": {"diffPercentage": 12.5, "output": str(out)}})
        with patch.object(shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(subprocess, "run", return_value=_completed(stdout=stdout)):
            score, _ = visual_diff.diff_screenshots(baseline, candidate, out)
        # 12.5% in normalised form is 0.125.
        assert score == pytest.approx(0.125)

    def test_falls_back_to_text_parsing_when_no_json(self, tmp_path):
        baseline = _png(tmp_path / "b.png")
        candidate = _png(tmp_path / "c.png")
        out = tmp_path / "d.png"
        stdout = (
            "Opened file:///tmp/c.png\n"
            "Diff: 8.4% (672 pixels) -> /tmp/visual/diff.png\n"
        )
        with patch.object(shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(subprocess, "run", return_value=_completed(stdout=stdout)):
            score, path = visual_diff.diff_screenshots(baseline, candidate, out)
        assert score == pytest.approx(0.084)
        assert path == "/tmp/visual/diff.png"

    def test_subprocess_failure_propagates(self, tmp_path):
        baseline = _png(tmp_path / "b.png")
        candidate = _png(tmp_path / "c.png")
        out = tmp_path / "d.png"
        with patch.object(shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(
                 subprocess,
                 "run",
                 side_effect=subprocess.CalledProcessError(
                     returncode=2, cmd=["agent-browser"], stderr="boom"
                 ),
             ):
            with pytest.raises(subprocess.CalledProcessError):
                visual_diff.diff_screenshots(baseline, candidate, out)

    def test_missing_baseline_raises(self, tmp_path):
        candidate = _png(tmp_path / "c.png")
        with pytest.raises(FileNotFoundError):
            visual_diff.diff_screenshots(
                tmp_path / "nope.png", candidate, tmp_path / "d.png"
            )

    def test_missing_candidate_raises(self, tmp_path):
        baseline = _png(tmp_path / "b.png")
        with pytest.raises(FileNotFoundError):
            visual_diff.diff_screenshots(
                baseline, tmp_path / "nope.png", tmp_path / "d.png"
            )

    @pytest.mark.parametrize("bad_threshold", [-0.01, 1.01, 2.0, -1.0])
    def test_threshold_out_of_range_raises(self, tmp_path, bad_threshold):
        baseline = _png(tmp_path / "b.png")
        candidate = _png(tmp_path / "c.png")
        with pytest.raises(ValueError):
            visual_diff.diff_screenshots(
                baseline, candidate, tmp_path / "d.png", threshold=bad_threshold
            )

    def test_command_invokes_agent_browser_with_expected_args(self, tmp_path):
        baseline = _png(tmp_path / "b.png")
        candidate = _png(tmp_path / "c.png")
        out = tmp_path / "out" / "d.png"
        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _completed(stdout=json.dumps({"score": 0.0}))

        with patch.object(shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(subprocess, "run", side_effect=fake_run):
            visual_diff.diff_screenshots(baseline, candidate, out, threshold=0.2)

        cmd = captured["cmd"]
        assert cmd[0] == "/usr/bin/agent-browser"
        assert "batch" in cmd
        assert "--json" in cmd
        diff_step = next(
            (s for s in cmd if isinstance(s, str) and s.startswith("diff screenshot")),
            "",
        )
        assert "-t 0.2" in diff_step
        assert str(baseline) in diff_step
        assert str(out) in diff_step
        # candidate is opened as a file:// URL in the prior step.
        open_step = next(
            (s for s in cmd if isinstance(s, str) and s.startswith("open ")), ""
        )
        assert candidate.resolve().as_uri() in open_step
        assert captured["kwargs"].get("check") is True
        assert out.parent.is_dir()


# --------------------------------------------------------------------- #
# CLI report
# --------------------------------------------------------------------- #


class TestVisualDiffCLI:
    def _seed_dirs(self, tmp_path: Path, titles: list[str]):
        g_dir = tmp_path / "g"
        k_dir = tmp_path / "k"
        for t in titles:
            _png(g_dir / f"{t}.png")
            _png(k_dir / f"{t}.png")
        return g_dir, k_dir

    def test_report_has_aggregate_min_max_mean(self, tmp_path):
        g_dir, k_dir = self._seed_dirs(tmp_path, ["A", "B", "C"])
        out_dir = tmp_path / "out"
        report = tmp_path / "report.json"

        scores_iter = iter([0.01, 0.20, 0.05])

        def fake_diff(baseline, candidate, output_path, threshold=0.15):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"diff")
            return next(scores_iter), str(output_path)

        with patch.object(visual_diff, "diff_screenshots", side_effect=fake_diff):
            rc = visual_diff.main(
                [
                    "--grafana-dir", str(g_dir),
                    "--kibana-dir", str(k_dir),
                    "--output-dir", str(out_dir),
                    "--threshold", "0.15",
                    "--report", str(report),
                ]
            )
        assert rc == 0
        payload = json.loads(report.read_text())
        agg = payload["aggregate"]
        assert agg["count"] == 3
        assert agg["min"] == pytest.approx(0.01)
        assert agg["max"] == pytest.approx(0.20)
        assert agg["mean"] == pytest.approx((0.01 + 0.20 + 0.05) / 3)
        # Only the 0.20 score crosses the 0.15 threshold.
        assert agg["panels_above_threshold"] == 1

        titles = [p["title"] for p in payload["panels"]]
        assert titles == ["A", "B", "C"]
        assert payload["unpaired_panels"] == []

    def test_report_records_unpaired_panels(self, tmp_path):
        g_dir = tmp_path / "g"
        k_dir = tmp_path / "k"
        _png(g_dir / "A.png")
        _png(g_dir / "Only-Grafana.png")
        _png(k_dir / "A.png")
        _png(k_dir / "Only-Kibana.png")
        out_dir = tmp_path / "out"
        report = tmp_path / "report.json"

        with patch.object(
            visual_diff,
            "diff_screenshots",
            return_value=(0.0, str(out_dir / "A.diff.png")),
        ):
            visual_diff.main(
                [
                    "--grafana-dir", str(g_dir),
                    "--kibana-dir", str(k_dir),
                    "--output-dir", str(out_dir),
                    "--report", str(report),
                ]
            )
        payload = json.loads(report.read_text())
        unpaired = {(u["title"], u["missing_side"]) for u in payload["unpaired_panels"]}
        assert unpaired == {
            ("Only-Grafana", "kibana"),
            ("Only-Kibana", "grafana"),
        }

    def test_subprocess_error_per_panel_does_not_kill_run(self, tmp_path):
        g_dir, k_dir = self._seed_dirs(tmp_path, ["A", "B"])
        out_dir = tmp_path / "out"
        report = tmp_path / "report.json"

        def fake_diff(baseline, candidate, output_path, threshold=0.15):
            if "A" in str(baseline):
                raise subprocess.CalledProcessError(
                    returncode=2,
                    cmd=["agent-browser"],
                    stderr="agent-browser crashed",
                )
            return 0.05, str(output_path)

        with patch.object(visual_diff, "diff_screenshots", side_effect=fake_diff):
            rc = visual_diff.main(
                [
                    "--grafana-dir", str(g_dir),
                    "--kibana-dir", str(k_dir),
                    "--output-dir", str(out_dir),
                    "--report", str(report),
                ]
            )
        assert rc == 0
        payload = json.loads(report.read_text())
        by_title = {p["title"]: p for p in payload["panels"]}
        assert by_title["A"]["score"] is None
        assert "agent-browser crashed" in by_title["A"]["error"]
        assert by_title["B"]["score"] == pytest.approx(0.05)
        # Aggregate ignores the failing row.
        assert payload["aggregate"]["count"] == 1

    def test_output_dir_must_be_explicit(self, tmp_path):
        g_dir, k_dir = self._seed_dirs(tmp_path, ["A"])
        with pytest.raises(SystemExit):
            visual_diff.main(
                [
                    "--grafana-dir", str(g_dir),
                    "--kibana-dir", str(k_dir),
                    "--report", str(tmp_path / "r.json"),
                ]
            )


# --------------------------------------------------------------------- #
# diff_screenshots — paths with spaces
# --------------------------------------------------------------------- #


class TestDiffScreenshotsPathsWithSpaces:
    def test_batch_command_quotes_paths_containing_spaces(self, tmp_path):
        """Paths with spaces must be double-quoted in the batch command string
        so that agent-browser's own argument parser does not split on the space."""
        # Use a directory name that contains a space.
        spaced_dir = tmp_path / "my baseline"
        baseline = _png(spaced_dir / "b.png")
        candidate = _png(tmp_path / "c.png")
        out = tmp_path / "my output" / "d.png"
        captured: dict[str, Any] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(stdout=json.dumps({"score": 0.0}))

        with patch.object(shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(subprocess, "run", side_effect=fake_run):
            visual_diff.diff_screenshots(baseline, candidate, out, threshold=0.15)

        cmd = captured["cmd"]
        diff_step = next(
            (s for s in cmd if isinstance(s, str) and s.startswith("diff screenshot")),
            "",
        )
        # The baseline path (which contains a space) must appear in double-quotes.
        assert f'"{baseline}"' in diff_step, (
            f"baseline path not quoted in diff step: {diff_step!r}"
        )
        # The output path (which also contains a space) must appear in double-quotes.
        assert f'"{out}"' in diff_step, (
            f"output path not quoted in diff step: {diff_step!r}"
        )
