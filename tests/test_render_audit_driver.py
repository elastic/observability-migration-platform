# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for the render-audit driver (browser plumbing, offline).

The verdict logic is tested in test_render_audit.py; here we test the headless
command construction and the orchestration via an injected DOM fetcher (no real
browser, no cluster).
"""

from __future__ import annotations

from observability_migration.targets.kibana.render_audit_driver import (
    audit_dashboard_render,
    build_render_audit_command,
)

_CLEAN_DOM = '<div class="lnsExpressionRenderer">chart rendered</div>'
_ERROR_DOM = '<div class="embPanel__error">An error occurred while loading this panel</div>'


def test_command_includes_persistent_profile_and_dump_dom():
    cmd = build_render_audit_command(
        "/usr/bin/chrome", "https://kb/app/dashboards#/view/abc", "/tmp/profile"
    )
    assert cmd[0] == "/usr/bin/chrome"
    assert "--headless=new" in cmd
    assert "--user-data-dir=/tmp/profile" in cmd
    assert "--dump-dom" in cmd
    # URL is the final argument.
    assert cmd[-1] == "https://kb/app/dashboards#/view/abc"


def test_command_omits_profile_flag_when_no_dir():
    cmd = build_render_audit_command("/usr/bin/chrome", "https://kb/x", "")
    assert not any(c.startswith("--user-data-dir=") for c in cmd)


def test_audit_passes_on_clean_dom():
    verdict = audit_dashboard_render(
        "https://kb", "dash-1", space_id="default", dom_fetcher=lambda _url: _CLEAN_DOM
    )
    assert verdict.status == "pass"
    assert verdict.rendered_error_markers == []


def test_audit_fails_on_error_embeddable_dom():
    verdict = audit_dashboard_render(
        "https://kb", "dash-1", dom_fetcher=lambda _url: _ERROR_DOM
    )
    assert verdict.status == "fail"
    assert verdict.rendered_error_markers


def test_audit_fetches_the_built_dashboard_url():
    seen = {}

    def fetch(url: str) -> str:
        seen["url"] = url
        return _CLEAN_DOM

    audit_dashboard_render("https://kb", "dash-xyz", space_id="team-a", dom_fetcher=fetch)
    assert "dash-xyz" in seen["url"]
    assert "team-a" in seen["url"]


def test_audit_control_interactions_detects_break_from_a_control():
    from observability_migration.targets.kibana.render_audit_driver import (
        audit_control_interactions,
    )
    # Selecting the 'cluster' control breaks panel_b; 'job' is harmless.
    states = {
        "baseline": {"panel_a": "rendered", "panel_b": "rendered"},
        "cluster": {"panel_a": "rendered", "panel_b": "error:render_error"},
        "job": {"panel_a": "rendered", "panel_b": "rendered"},
    }
    selected = {"current": "baseline"}

    def capture():
        return states[selected["current"]]

    def select(name):
        selected["current"] = name

    findings = audit_control_interactions(
        [{"variable_name": "cluster", "label": "cluster"},
         {"variable_name": "job", "label": "job"}],
        capture_render_snapshot=capture,
        select_control_nondefault=select,
    )
    assert findings == ["control 'cluster': panel_b: rendered -> error:render_error"]


def test_audit_control_interactions_clean_when_controls_safe():
    from observability_migration.targets.kibana.render_audit_driver import (
        audit_control_interactions,
    )
    snap = {"panel_a": "rendered"}
    findings = audit_control_interactions(
        [{"variable_name": "x", "label": "x"}],
        capture_render_snapshot=lambda: snap,
        select_control_nondefault=lambda _name: None,
    )
    assert findings == []


def _cli_args(**over):
    import argparse
    base = dict(kibana_url="https://kb", dashboard_id="d1", space="", user_data_dir="",
                time_from="now-1h", time_to="now", fail_on_error=True)
    base.update(over)
    return argparse.Namespace(**base)


def test_cli_returns_0_on_clean_render():
    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli
    rc = run_audit_cli(_cli_args(), dom_fetcher=lambda _u: "line chart instance_1 rendered")
    assert rc == 0


def test_cli_returns_1_on_render_error_when_fail_on_error():
    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli
    rc = run_audit_cli(
        _cli_args(fail_on_error=True),
        dom_fetcher=lambda _u: "embPanel__error An error occurred while loading this panel",
    )
    assert rc == 1


def test_cli_returns_0_on_render_error_without_fail_flag():
    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli
    rc = run_audit_cli(
        _cli_args(fail_on_error=False),
        dom_fetcher=lambda _u: "embPanel__error",
    )
    assert rc == 0
