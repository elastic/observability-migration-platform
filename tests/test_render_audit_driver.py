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
                time_from="now-1h", time_to="now", fail_on_error=True, elements=False, migration_out="")
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


def test_cli_elements_mode_runs_per_panel_audit(tmp_path):
    import json as _json

    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli
    # a migration report with a heatmap panel, and a DOM that rendered it as xy
    report = {"dashboards": [{"panels": [
        {"title": "canary heatmap", "yaml_panel": {"esql": {"type": "heatmap", "breakdown": {"field": "le"}}}},
    ]}]}
    (tmp_path / "migration_report.json").write_text(_json.dumps(report))
    args = _cli_args(elements=True, migration_out=str(tmp_path), fail_on_error=False)
    dom = 'StaticText "canary heatmap" button "a; Click: to show, x" StaticText "Chart type" StaticText ":" StaticText "line chart"'
    rc = run_audit_cli(args, dom_fetcher=lambda _u: dom)
    assert rc == 0  # render itself is clean; element mismatch is a warn, not a fail


def _field_gap_report(tmp_path):
    """A migration report with one query-bearing panel broken down by ``method``."""
    import json as _json
    report = {"dashboards": [{"panels": [
        {"title": "HTTP by method", "kibana_type": "line", "yaml_panel": {"esql": {
            "type": "line", "query": "FROM metrics-* | STATS c=COUNT(*) BY method",
            "breakdown": {"field": "method"}}}},
    ]}]}
    (tmp_path / "migration_report.json").write_text(_json.dumps(report))
    return str(tmp_path)


# Reviewer (PR #234, giorgi/stefans): a "Provided column name or index is invalid"
# marker on a panel whose breakdown field is absent from the target is a
# data-readiness field_gap (warn), NOT a translator render_error. The CLI must
# segment panels and feed the migration metadata into classify_render_per_panel
# so --fail-on-error does not exit 1 on that case.
_FIELD_GAP_DOM = (
    'StaticText "HTTP by method" StaticText "Provided column name or index is invalid"'
)


def test_cli_field_gap_warns_and_does_not_fail(tmp_path):
    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli
    args = _cli_args(migration_out=_field_gap_report(tmp_path), fail_on_error=True)
    # target data lacks the "method" breakdown field -> field_gap (warn)
    rc = run_audit_cli(
        args,
        dom_fetcher=lambda _u: _FIELD_GAP_DOM,
        field_fetcher=lambda: {"@timestamp", "value", "host.name"},
    )
    assert rc == 0


def test_cli_field_gap_with_duplicate_marker_in_one_panel_does_not_fail(tmp_path):
    # PR #234 review: the same Kibana error string can appear twice inside one
    # panel's dumped DOM (nested nodes). A single proven field_gap panel must
    # stay a warn — the unsegmented-error guard must count markers OUTSIDE
    # segmented panels, not compare a global occurrence count to the number of
    # error panels (which promotes duplicate in-panel field-gap text to a fail).
    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli
    dom = (
        'StaticText "HTTP by method" '
        'StaticText "Provided column name or index is invalid" '
        'StaticText "Provided column name or index is invalid"'
    )
    args = _cli_args(migration_out=_field_gap_report(tmp_path), fail_on_error=True)
    rc = run_audit_cli(
        args,
        dom_fetcher=lambda _u: dom,
        field_fetcher=lambda: {"@timestamp", "value", "host.name"},
    )
    assert rc == 0


def test_cli_render_error_with_present_field_still_fails(tmp_path):
    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli
    args = _cli_args(migration_out=_field_gap_report(tmp_path), fail_on_error=True)
    # the breakdown field IS present -> the marker is a real render bug (fail)
    rc = run_audit_cli(
        args,
        dom_fetcher=lambda _u: _FIELD_GAP_DOM,
        field_fetcher=lambda: {"@timestamp", "value", "method"},
    )
    assert rc == 1


def test_cli_render_marker_fails_when_no_field_metadata(tmp_path):
    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli
    args = _cli_args(migration_out=_field_gap_report(tmp_path), fail_on_error=True)
    # without target field caps a marker can't be proven a field_gap -> render_error
    rc = run_audit_cli(
        args,
        dom_fetcher=lambda _u: _FIELD_GAP_DOM,
        field_fetcher=lambda: None,
    )
    assert rc == 1


def test_cli_unsegmented_render_error_still_fails(tmp_path):
    import json as _json

    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli

    report = {"dashboards": [{"panels": [
        {"title": "Expected title", "kibana_type": "line", "yaml_panel": {"esql": {
            "type": "line", "query": "FROM metrics-* | STATS c=COUNT()"}}},
    ]}]}
    (tmp_path / "migration_report.json").write_text(_json.dumps(report))
    args = _cli_args(migration_out=str(tmp_path), fail_on_error=True)

    rc = run_audit_cli(
        args,
        dom_fetcher=lambda _u: "embPanel__error An error occurred while loading this panel",
        field_fetcher=lambda: {"@timestamp"},
    )

    assert rc == 1


def test_cli_extra_hidden_marker_is_not_swallowed_by_visible_field_gap(tmp_path):
    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli

    args = _cli_args(migration_out=_field_gap_report(tmp_path), fail_on_error=True)
    dom = (
        'StaticText "HTTP by method" StaticText "Provided column name or index is invalid" '
        'Hidden untitled panel Provided column name or index is invalid'
    )

    rc = run_audit_cli(
        args,
        dom_fetcher=lambda _u: dom,
        field_fetcher=lambda: {"@timestamp", "value", "host.name"},
    )

    assert rc == 1
