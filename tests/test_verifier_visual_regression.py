# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for parity-rig/verifier/visual_regression.py.

The module shells out to ``agent-browser`` and Grafana's REST API; both
are mocked here so the tests run offline in milliseconds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERIFIER_PARENT = ROOT / "parity-rig"
if str(VERIFIER_PARENT) not in sys.path:
    sys.path.insert(0, str(VERIFIER_PARENT))

from verifier import visual_regression as vr  # noqa: E402

# --------------------------------------------------------------------- #
#  URL builders                                                          #
# --------------------------------------------------------------------- #


class TestBuildGrafanaSoloUrl:
    def test_basic_pattern(self):
        url = vr.build_grafana_solo_url(
            "http://localhost:23000",
            "zJfYJhynk",
            "express-prometheus-middleware",
            66,
        )
        assert url.startswith(
            "http://localhost:23000/d-solo/zJfYJhynk/express-prometheus-middleware?"
        )
        assert "panelId=66" in url
        assert "kiosk=tv" in url
        assert "from=now-1h" in url
        assert "to=now" in url

    def test_trims_trailing_slash(self):
        url = vr.build_grafana_solo_url(
            "http://localhost:23000/", "abc", "slug", 1
        )
        assert "//d-solo" not in url
        assert "/d-solo/abc/slug?" in url

    def test_custom_time_range(self):
        url = vr.build_grafana_solo_url(
            "http://localhost:23000",
            "abc",
            "slug",
            7,
            from_="2026-05-01T00:00:00Z",
            to="2026-05-02T00:00:00Z",
        )
        assert "from=2026-05-01T00%3A00%3A00Z" in url
        assert "to=2026-05-02T00%3A00%3A00Z" in url


class TestBuildKibanaExpandedPanelUrl:
    def test_basic_pattern(self):
        url = vr.build_kibana_expanded_panel_url(
            "https://kb.example/",
            "dash-id",
            "panel-uuid",
        )
        # Rison parentheses must NOT be URL-encoded
        assert "/app/dashboards#/view/dash-id?_g=(" in url
        assert "_a=(expandedPanelId:panel-uuid)" in url
        assert "time:(from:now-1h,to:now)" in url


# --------------------------------------------------------------------- #
#  Grafana panel discovery                                              #
# --------------------------------------------------------------------- #


class TestListGrafanaPanels:
    def test_flattens_rows(self):
        fake_response = mock.Mock()
        fake_response.raise_for_status = mock.Mock()
        fake_response.json = mock.Mock(
            return_value={
                "dashboard": {
                    "panels": [
                        {
                            "id": 1,
                            "title": "Row 1",
                            "type": "row",
                            "panels": [
                                {"id": 11, "title": "Inside row 1a", "type": "timeseries"},
                                {"id": 12, "title": "Inside row 1b", "type": "stat"},
                            ],
                        },
                        {"id": 2, "title": "Top-level panel", "type": "gauge"},
                        {"id": 3, "title": "Row 2", "type": "row", "panels": []},
                    ]
                }
            }
        )
        session = mock.Mock()
        session.get = mock.Mock(return_value=fake_response)
        panels = vr.list_grafana_panels(
            "http://localhost:23000", "uid", session=session
        )
        assert {p["id"] for p in panels} == {11, 12, 2}
        assert {p["title"] for p in panels} == {
            "Inside row 1a",
            "Inside row 1b",
            "Top-level panel",
        }

    def test_skips_panels_without_id(self):
        fake_response = mock.Mock()
        fake_response.raise_for_status = mock.Mock()
        fake_response.json = mock.Mock(
            return_value={
                "dashboard": {
                    "panels": [{"title": "no id panel", "type": "stat"}]
                }
            }
        )
        session = mock.Mock(get=mock.Mock(return_value=fake_response))
        panels = vr.list_grafana_panels(
            "http://localhost:23000", "uid", session=session
        )
        assert panels == []


# --------------------------------------------------------------------- #
#  Kibana panel discovery from migration YAML                           #
# --------------------------------------------------------------------- #


class TestListKibanaPanelsFromMigration:
    def test_reads_yaml_dashboards(self, tmp_path):
        yaml_dir = tmp_path / "yaml"
        yaml_dir.mkdir()
        (yaml_dir / "dash.yaml").write_text(
            """
dashboards:
  - title: My Dashboard
    panels:
      - id: panel-a
        title: First panel
        type: lens
      - id: panel-b
        title: Second panel
        type: lens
      - id: panel-c
        title: ""  # untitled is skipped
        type: lens
"""
        )
        panels = vr.list_kibana_panels_from_migration(tmp_path)
        assert {p["id"] for p in panels} == {"panel-a", "panel-b"}
        titles = {p["title"] for p in panels}
        assert titles == {"First panel", "Second panel"}

    def test_returns_empty_list_when_no_yaml(self, tmp_path):
        assert vr.list_kibana_panels_from_migration(tmp_path) == []

    def test_backfills_ids_from_compiled_ndjson(self, tmp_path):
        """The YAML doesn't carry Kibana panel UUIDs. We cross-reference
        with the compiled NDJSON's ``panelsJSON.panelIndex`` keyed by
        title so the Kibana solo-panel URL builder has a valid id."""
        yaml_dir = tmp_path / "yaml"
        yaml_dir.mkdir()
        (yaml_dir / "dash.yaml").write_text(
            """
dashboards:
  - name: My dash
    panels:
      - title: Panel A
        type: lens
      - title: Panel B
        type: markdown
"""
        )
        compiled = tmp_path / "compiled" / "my_dash"
        compiled.mkdir(parents=True)
        panels_json = json.dumps([
            {
                "panelIndex": "uuid-a",
                "embeddableConfig": {"attributes": {"title": "Panel A"}},
            },
            {
                "panelIndex": "uuid-b",
                "embeddableConfig": {"savedVis": {"title": "Panel B"}},
            },
        ])
        (compiled / "compiled_dashboards.ndjson").write_text(
            json.dumps({
                "type": "dashboard",
                "attributes": {"panelsJSON": panels_json},
            }) + "\n"
        )

        panels = vr.list_kibana_panels_from_migration(tmp_path)
        by_title = {p["title"]: p for p in panels}
        assert by_title["Panel A"]["id"] == "uuid-a"
        assert by_title["Panel B"]["id"] == "uuid-b"

    def test_recurses_into_section_panels(self, tmp_path):
        """Migration YAML nests panels inside ``section.panels`` when
        the source dashboard had Grafana rows. Discovery must walk
        these so we don't silently lose 90%+ of the panel set on
        section-heavy dashboards like express-prometheus-middleware."""
        yaml_dir = tmp_path / "yaml"
        yaml_dir.mkdir()
        (yaml_dir / "dash.yaml").write_text(
            """
dashboards:
  - name: With sections
    panels:
      - title: HTTP Requests
        section:
          panels:
            - title: Count by class
              type: markdown
            - title: Request count
              type: lens
      - title: System Metrics
        section:
          panels:
            - title: CPU usage
              type: lens
      - title: Top-level chart
        type: lens
"""
        )
        panels = vr.list_kibana_panels_from_migration(tmp_path)
        titles = {p["title"] for p in panels}
        assert titles == {
            "Count by class",
            "Request count",
            "CPU usage",
            "Top-level chart",
        }
        # Section containers themselves are excluded
        assert "HTTP Requests" not in titles
        assert "System Metrics" not in titles


# --------------------------------------------------------------------- #
#  agent-browser subprocess driver                                      #
# --------------------------------------------------------------------- #


class TestRunAgentBrowserBatch:
    def test_missing_binary_returns_failure(self):
        with mock.patch.object(vr.shutil, "which", return_value=None):
            ok, _stdout, stderr = vr._run_agent_browser_batch(["open about:blank"])
        assert ok is False
        assert "agent-browser binary missing" in stderr

    def test_constructs_command_with_state_when_present(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{}")
        fake_completed = mock.Mock(
            returncode=0,
            stdout='[{"success":true,"command":["open","about:blank"]}]',
            stderr="",
        )
        with mock.patch.object(vr.shutil, "which", return_value="/usr/local/bin/agent-browser"), \
             mock.patch.object(vr.subprocess, "run", return_value=fake_completed) as run_mock:
            ok, _stdout, _ = vr._run_agent_browser_batch(
                ["open about:blank"], state_file=state_file
            )
        assert ok is True
        invoked_cmd = run_mock.call_args[0][0]
        assert "--state" in invoked_cmd
        assert str(state_file) in invoked_cmd
        assert "batch" in invoked_cmd
        assert "--json" in invoked_cmd
        assert "--bail" in invoked_cmd

    def test_omits_state_flag_when_file_absent(self, tmp_path):
        state_file = tmp_path / "missing.json"
        fake_completed = mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch.object(vr.shutil, "which", return_value="/usr/local/bin/agent-browser"), \
             mock.patch.object(vr.subprocess, "run", return_value=fake_completed) as run_mock:
            vr._run_agent_browser_batch(["open about:blank"], state_file=state_file)
        invoked_cmd = run_mock.call_args[0][0]
        assert "--state" not in invoked_cmd

    def test_returncode_nonzero_is_failure(self):
        fake_completed = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch.object(vr.shutil, "which", return_value="/usr/local/bin/agent-browser"), \
             mock.patch.object(vr.subprocess, "run", return_value=fake_completed):
            ok, _, stderr = vr._run_agent_browser_batch(["open about:blank"])
        assert ok is False
        assert "boom" in stderr

    def test_step_success_false_is_failure(self):
        fake_completed = mock.Mock(
            returncode=0,
            stdout='[{"success":true,"command":["open","ok"]},'
                   '{"success":false,"command":["click","x"],"error":"not found"}]',
            stderr="",
        )
        with mock.patch.object(vr.shutil, "which", return_value="/usr/local/bin/agent-browser"), \
             mock.patch.object(vr.subprocess, "run", return_value=fake_completed):
            ok, _, stderr = vr._run_agent_browser_batch(["open ok", "click x"])
        assert ok is False
        assert "not found" in stderr

    def test_invalid_json_stdout_is_failure(self):
        fake_completed = mock.Mock(returncode=0, stdout="not json at all", stderr="")
        with mock.patch.object(vr.shutil, "which", return_value="/usr/local/bin/agent-browser"), \
             mock.patch.object(vr.subprocess, "run", return_value=fake_completed):
            ok, _, stderr = vr._run_agent_browser_batch(["open ok"])
        assert ok is False
        assert "not JSON" in stderr


class TestCaptureGrafanaPanel:
    def _write_real_size_png(self, path: Path) -> None:
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (vr.MIN_REAL_SCREENSHOT_BYTES + 1))

    def test_returns_path_on_success(self, tmp_path):
        out_png = tmp_path / "g.png"

        def fake_run(commands, **_kwargs):
            self._write_real_size_png(out_png)
            return True, '[{"success":true}]', ""

        with mock.patch.object(vr, "_run_agent_browser_batch", side_effect=fake_run):
            path, notes = vr.capture_grafana_panel(
                "http://localhost:23000", "uid", "slug", 7, out_png
            )
        assert path == out_png
        assert notes == []

    def test_returns_none_on_subprocess_failure(self, tmp_path):
        out_png = tmp_path / "g.png"
        with mock.patch.object(
            vr, "_run_agent_browser_batch",
            return_value=(False, "", "agent-browser binary missing"),
        ):
            path, notes = vr.capture_grafana_panel(
                "http://localhost:23000", "uid", "slug", 7, out_png
            )
        assert path is None
        assert vr.NOTE_GRAFANA_MISSING in notes

    def test_returns_none_on_tiny_screenshot(self, tmp_path):
        out_png = tmp_path / "g.png"

        def fake_run(commands, **_kwargs):
            out_png.write_bytes(b"x" * 100)  # below threshold
            return True, "[]", ""

        with mock.patch.object(vr, "_run_agent_browser_batch", side_effect=fake_run):
            path, notes = vr.capture_grafana_panel(
                "http://localhost:23000", "uid", "slug", 7, out_png
            )
        assert path is None
        assert vr.NOTE_TINY_SCREENSHOT in notes


class TestAuthRedirectDetection:
    def test_matches_capture_url(self):
        assert vr._looks_like_auth_redirect(
            "https://kb.example/internal/security/capture-url?next=%2Fapp%2Fdashboards"
        )

    def test_matches_cloud_saml_kibana(self):
        assert vr._looks_like_auth_redirect(
            "https://kb.example/?auth_provider_hint=cloud-saml-kibana"
        )

    def test_matches_oauth_path(self):
        assert vr._looks_like_auth_redirect("https://kb.example/oauth/callback")

    def test_real_dashboard_url_is_not_auth(self):
        url = "https://kb.example/app/dashboards#/view/dash-id?_g=(time:(from:now-1h,to:now))"
        assert not vr._looks_like_auth_redirect(url)


def _ok_open_response(landed_url: str) -> str:
    """Return a stdout payload mimicking a successful ``open`` step."""
    return json.dumps(
        [{"command": ["open", landed_url], "result": {"url": landed_url}, "success": True}]
    )


class TestCaptureKibanaPanel:
    REAL_LANDING = "https://kb.example/app/dashboards#/view/dash?_a=()"
    AUTH_LANDING = (
        "https://kb.example/internal/security/capture-url"
        "?next=%2Fapp%2Fdashboards%3Fauth_provider_hint%3Dcloud-saml-kibana#/view/dash"
    )

    def _write_real_size_png(self, path: Path) -> None:
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (vr.MIN_REAL_SCREENSHOT_BYTES + 1))

    def test_returns_path_on_success(self, tmp_path):
        out_png = tmp_path / "k.png"

        def fake_run(commands, **_kwargs):
            self._write_real_size_png(out_png)
            return True, _ok_open_response(self.REAL_LANDING), ""

        with mock.patch.object(vr, "_run_agent_browser_batch", side_effect=fake_run):
            path, notes = vr.capture_kibana_panel(
                "https://kb.example", "dash", "panel-uuid", out_png
            )
        assert path == out_png
        assert notes == []

    def test_auth_redirect_url_immediately_tags_auth_required(self, tmp_path):
        """When the post-navigation URL matches our auth-redirect
        substrings we short-circuit *before* attempting the fall-back
        capture (this is the common case in CI and we don't want to
        waste another browser invocation on it)."""
        out_png = tmp_path / "k.png"

        call_count = {"n": 0}

        def fake_run(commands, **_kwargs):
            call_count["n"] += 1
            # Selector capture fails because we landed on the SAML page;
            # the function should inspect the URL and short-circuit.
            return False, _ok_open_response(self.AUTH_LANDING), "selector not found"

        with mock.patch.object(vr, "_run_agent_browser_batch", side_effect=fake_run):
            path, notes = vr.capture_kibana_panel(
                "https://kb.example", "dash", "panel-uuid", out_png
            )
        assert path is None
        assert vr.NOTE_KIBANA_AUTH in notes
        assert call_count["n"] == 1, "should not retry once auth-redirect detected"

    def test_fallback_real_size_means_render_failed(self, tmp_path):
        """Selector capture fails on a real (non-auth) page and the
        fall-back produces a real-sized PNG -> Kibana rendered, but the
        expanded-panel selector wasn't there. Investigate the URL or
        panel id."""
        out_png = tmp_path / "k.png"
        fallback_png = out_png.with_suffix(".fallback.png")
        call_count = {"n": 0}

        def fake_run(commands, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return False, _ok_open_response(self.REAL_LANDING), "selector lookup failed"
            fallback_png.parent.mkdir(parents=True, exist_ok=True)
            fallback_png.write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"\x00" * (vr.MIN_REAL_SCREENSHOT_BYTES + 1)
            )
            return True, _ok_open_response(self.REAL_LANDING), ""

        with mock.patch.object(vr, "_run_agent_browser_batch", side_effect=fake_run):
            path, notes = vr.capture_kibana_panel(
                "https://kb.example", "dash", "panel-uuid", out_png
            )
        assert path is None
        assert "kibana_render_failed" in notes
        assert vr.NOTE_KIBANA_AUTH not in notes

    def test_complete_subprocess_failure_tags_capture_failed(self, tmp_path):
        """When the subprocess fails AND no PNG is produced AND the URL
        is not a known auth flow, we tag the generic capture_failed
        note so the operator gets a different diagnostic path."""
        out_png = tmp_path / "k.png"
        # _extract_open_url returns "" on empty stdout, which doesn't
        # match any auth substring -> we fall through to fall-back,
        # which also fails -> capture_failed.
        with mock.patch.object(
            vr, "_run_agent_browser_batch", return_value=(False, "", "binary missing")
        ):
            path, notes = vr.capture_kibana_panel(
                "https://kb.example", "dash", "panel-uuid", out_png
            )
        assert path is None
        assert vr.NOTE_KIBANA_MISSING in notes


# --------------------------------------------------------------------- #
#  Aggregation                                                          #
# --------------------------------------------------------------------- #


class TestAggregateScores:
    def test_empty_returns_zeros(self):
        median, p95, mx, n = vr.aggregate_scores([])
        assert (median, p95, mx, n) == (0.0, 0.0, 0.0, 0)

    def test_ignores_panels_without_both_screenshots(self):
        panels = [
            vr.PanelComparison(title="ok", grafana_screenshot="g", kibana_screenshot="k", diff_score=0.10),
            vr.PanelComparison(title="missing-kib", grafana_screenshot="g", kibana_screenshot="", diff_score=0.99),
            vr.PanelComparison(title="missing-graf", grafana_screenshot="", kibana_screenshot="k", diff_score=0.99),
            vr.PanelComparison(title="also-ok", grafana_screenshot="g", kibana_screenshot="k", diff_score=0.20),
        ]
        median, p95, mx, n = vr.aggregate_scores(panels)
        assert n == 2
        assert median == pytest.approx(0.15, abs=1e-9)
        # With only 2 scores p95 == max == 0.20
        assert p95 == pytest.approx(0.20, abs=1e-9)
        assert mx == pytest.approx(0.20, abs=1e-9)

    def test_median_p95_max_typical_distribution(self):
        scores = [0.05, 0.07, 0.10, 0.12, 0.15, 0.18, 0.22, 0.28, 0.34, 0.45]
        panels = [
            vr.PanelComparison(title=f"p{i}", grafana_screenshot="g", kibana_screenshot="k", diff_score=s)
            for i, s in enumerate(scores)
        ]
        median, p95, mx, n = vr.aggregate_scores(panels)
        assert n == 10
        # statistics.median over an even-sized list interpolates
        assert median == pytest.approx(0.165, abs=1e-9)
        # p95 idx = round(0.95 * 9) = 9 -> last element
        assert p95 == 0.45
        assert mx == 0.45


# --------------------------------------------------------------------- #
#  Filename slugification                                               #
# --------------------------------------------------------------------- #


class TestSlugForTitle:
    def test_basic_words(self):
        assert vr._slug_for_title("HTTP Requests by status") == "http-requests-by-status"

    def test_collapses_consecutive_non_alnum(self):
        assert vr._slug_for_title("Foo!!!  --  Bar") == "foo-bar"

    def test_strips_edge_dashes(self):
        assert vr._slug_for_title("---x---") == "x"

    def test_empty_falls_back_to_untitled(self):
        assert vr._slug_for_title("") == "untitled"
        assert vr._slug_for_title("???") == "untitled"

    def test_truncates_long_titles(self):
        long_title = "A" * 200
        slug = vr._slug_for_title(long_title)
        assert len(slug) == 80


# --------------------------------------------------------------------- #
#  End-to-end run with mocked subprocess + HTTP                          #
# --------------------------------------------------------------------- #


class TestRunDashboardEndToEnd:
    """The happy path: 2 paired panels, both capture cleanly, diff scores
    flow through to the aggregate."""

    def _make_migration_yaml(self, tmp_path: Path) -> Path:
        yaml_dir = tmp_path / "yaml"
        yaml_dir.mkdir()
        (yaml_dir / "dash.yaml").write_text(
            """
dashboards:
  - title: Migrated
    panels:
      - id: kib-panel-1
        title: Alpha
        type: lens
      - id: kib-panel-2
        title: Beta
        type: lens
"""
        )
        return tmp_path

    def test_pairs_titles_captures_diffs_aggregates(self, tmp_path):
        out_dir = tmp_path / "out"
        migration_out = self._make_migration_yaml(tmp_path)

        # Fake Grafana API
        fake_resp = mock.Mock()
        fake_resp.raise_for_status = mock.Mock()
        fake_resp.json = mock.Mock(
            return_value={
                "dashboard": {
                    "panels": [
                        {"id": 10, "title": "Alpha", "type": "timeseries"},
                        {"id": 20, "title": "Beta", "type": "stat"},
                        {"id": 30, "title": "Only in Grafana", "type": "gauge"},
                    ]
                }
            }
        )
        session_mock = mock.Mock(get=mock.Mock(return_value=fake_resp))

        # Fake subprocess: every capture produces a real-sized PNG.
        def fake_batch(commands, state_file=None, session="visual-rig", timeout=60):
            # The last command in a capture is `screenshot ...`. Pull
            # the destination path out and write a PNG there.
            for cmd in commands:
                if cmd.startswith("screenshot"):
                    parts = cmd.split()
                    target = Path(parts[-1])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (vr.MIN_REAL_SCREENSHOT_BYTES + 1))
            return True, "[]", ""

        # Fake visual_diff.diff_screenshots -> deterministic per-title scores
        score_table = {"alpha": 0.12, "beta": 0.08}

        def fake_diff(baseline, candidate, output_path, threshold):
            # The slug appears in both filenames; pick it out
            for key, score in score_table.items():
                if key in baseline.name:
                    return score, str(output_path)
            return 0.0, str(output_path)

        with mock.patch.object(vr.requests, "Session", return_value=session_mock), \
             mock.patch.object(vr, "_run_agent_browser_batch", side_effect=fake_batch), \
             mock.patch("verifier.visual_diff.diff_screenshots", side_effect=fake_diff):
            report = vr.run_dashboard(
                grafana_url="http://localhost:23000",
                grafana_uid="g-uid",
                grafana_slug="g-slug",
                kibana_url="https://kb.example",
                kibana_dashboard_id="k-id",
                migration_out=migration_out,
                output_dir=out_dir,
            )

        # Pairing: Alpha and Beta are paired; "Only in Grafana" is unpaired
        assert {p.title for p in report.panels} == {"Alpha", "Beta"}
        assert report.unpaired_grafana == ["Only in Grafana"]
        assert report.unpaired_kibana == []

        # Both captures succeeded -> 2 captured pairs, 0 skipped
        assert report.captured_pairs == 2
        assert report.skipped_pairs == 0

        # Median across {0.08, 0.12} is 0.10
        assert report.median_score == pytest.approx(0.10, abs=1e-9)
        assert report.max_score == 0.12

        # File outputs created
        assert (out_dir / "grafana").exists()
        assert (out_dir / "kibana").exists()
        assert (out_dir / "diffs").exists()

    def test_kibana_auth_bounce_skips_pairs_but_not_panels(self, tmp_path):
        """When Kibana auth bounces, panels are still recorded but
        excluded from the score aggregate. The Grafana side still
        captures cleanly."""
        out_dir = tmp_path / "out"
        migration_out = self._make_migration_yaml(tmp_path)

        fake_resp = mock.Mock()
        fake_resp.raise_for_status = mock.Mock()
        fake_resp.json = mock.Mock(
            return_value={
                "dashboard": {
                    "panels": [
                        {"id": 10, "title": "Alpha", "type": "timeseries"},
                        {"id": 20, "title": "Beta", "type": "stat"},
                    ]
                }
            }
        )
        session_mock = mock.Mock(get=mock.Mock(return_value=fake_resp))

        def fake_batch(commands, state_file=None, session="visual-rig", timeout=60):
            shot_cmd = next((c for c in commands if c.startswith("screenshot")), "")
            shot_parts = shot_cmd.split()
            open_cmd = next((c for c in commands if c.startswith("open ")), "")
            open_url = open_cmd.split(maxsplit=1)[1] if " " in open_cmd else ""
            open_response = json.dumps(
                [{"command": ["open", open_url], "result": {"url": open_url}, "success": True}]
            )

            if session.startswith("visual-rig-grafana"):
                target = Path(shot_parts[-1])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(
                    b"\x89PNG\r\n\x1a\n" + b"\x00" * (vr.MIN_REAL_SCREENSHOT_BYTES + 1)
                )
                return True, open_response, ""

            # Kibana side: simulate a SAML bounce. We craft a stdout
            # blob whose `open` step records an auth URL so
            # capture_kibana_panel's URL inspection fires.
            saml_url = "https://kb.example/internal/security/capture-url?next=/app/dashboards"
            auth_response = json.dumps(
                [{"command": ["open", saml_url], "result": {"url": saml_url}, "success": True}]
            )
            return False, auth_response, "selector not found"

        with mock.patch.object(vr.requests, "Session", return_value=session_mock), \
             mock.patch.object(vr, "_run_agent_browser_batch", side_effect=fake_batch), \
             mock.patch("verifier.visual_diff.diff_screenshots") as diff_mock:
            report = vr.run_dashboard(
                grafana_url="http://localhost:23000",
                grafana_uid="g-uid",
                grafana_slug="g-slug",
                kibana_url="https://kb.example",
                kibana_dashboard_id="k-id",
                migration_out=migration_out,
                output_dir=out_dir,
            )

        assert report.captured_pairs == 0
        assert report.skipped_pairs == 2
        assert report.median_score == 0.0
        diff_mock.assert_not_called()

        for panel in report.panels:
            assert vr.NOTE_KIBANA_AUTH in panel.notes


# --------------------------------------------------------------------- #
#  Argparser smoke                                                      #
# --------------------------------------------------------------------- #


class TestArgparser:
    def test_required_flags(self):
        parser = vr.build_argparser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_full_flag_set_parses(self, tmp_path):
        parser = vr.build_argparser()
        args = parser.parse_args(
            [
                "--migration-out", str(tmp_path / "mig"),
                "--grafana-uid", "g-uid",
                "--grafana-slug", "g-slug",
                "--kibana-url", "https://kb.example",
                "--kibana-dash-id", "k-id",
                "--output-dir", str(tmp_path / "out"),
                "--report", str(tmp_path / "out/report.json"),
                "--threshold", "0.2",
                "--from", "now-30m",
                "--to", "now",
            ]
        )
        assert args.grafana_uid == "g-uid"
        assert args.kibana_dash_id == "k-id"
        assert args.threshold == 0.2
        assert args.from_ == "now-30m"
