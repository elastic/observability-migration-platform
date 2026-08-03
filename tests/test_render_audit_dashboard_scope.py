# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""The render audit must segment a DOM by the audited dashboard's titles only.

``--migration-out`` names a whole migration run, so its ``migration_report.json``
carries every dashboard of that run; ``--dashboard-id`` names ONE of them. The
audit used to build its title matcher from the whole report, so a stray DOM text
match could attribute a chunk of the audited dashboard's DOM to a *different*
dashboard's panel metadata — the wrong breakdown fields, the wrong metric, the
wrong index. Observed live: a Docker panel reported as ``Untitled`` matched
against metadata whose columns are ``docker_image``/``docker_containers_running``.

This is the render audit's copy of the verifier bug fixed in 07e5829, where a
global title-keyed dict compared Kafka's ``Error Logs`` against Redis's query and
fabricated five of six drift findings. A misattributing join is worse than a
missing one: it answers confidently and wrongly. So the join is scoped per
dashboard, duplicate titles *within* one dashboard resolve in DOM order, and a
dashboard that cannot be matched in the report reports that fact instead of
quietly falling back to every title in the run.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from observability_migration.targets.kibana.render_audit import segment_panels
from observability_migration.targets.kibana.render_audit_driver import run_audit_cli


def _cli_args(**over):
    base = dict(
        kibana_url="https://kb", dashboard_id="d1", space="", user_data_dir="",
        time_from="now-1h", time_to="now", fail_on_error=True, elements=False,
        migration_out="", es_url="", es_api_key="", es_index="metrics-*",
        insecure=False, agent_browser=False, chrome_no_sandbox=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _stdout_json(capsys):
    """The audit's JSON verdict off stdout (warnings belong on stderr)."""
    return json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------- #
# 1. A title belonging to dashboard B must not claim a chunk of dashboard A
# --------------------------------------------------------------------------- #
#
# "Container CPU" is a Docker panel. It also appears as plain text inside the
# Apache dashboard's DOM (a legend label, a table cell, a markdown note). With a
# report-wide matcher the Apache DOM was segmented by it, and the chunk was then
# judged against Docker's breakdown field / metric / index.

_TWO_DASHBOARD_REPORT = {
    "dashboards": [
        {
            "title": "Apache - Overview",
            "panels": [
                {
                    "title": "Requests per second",
                    "kibana_type": "line",
                    "yaml_panel": {"esql": {
                        "type": "line",
                        "query": "FROM metrics-* | STATS c=AVG(apache_net_request_per_s) BY host.name",
                        "breakdown": {"field": "host.name"},
                    }},
                },
            ],
        },
        {
            "title": "Docker - Overview",
            "panels": [
                {
                    "title": "Container CPU",
                    "kibana_type": "line",
                    "yaml_panel": {"esql": {
                        "type": "line",
                        "query": "FROM logs-* | STATS c=AVG(docker_containers_running) BY docker_image",
                        "breakdown": {"field": "docker_image"},
                    }},
                },
            ],
        },
    ]
}

# Apache's DOM: its own panel plus the words "Container CPU" appearing as
# ordinary rendered text after it.
_APACHE_DOM = (
    'StaticText "Requests per second" button "web-1; Click: to show, x" '
    'StaticText "Container CPU" StaticText "No results found"'
)


def test_a_foreign_dashboards_title_does_not_segment_the_audited_dom(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "migration_report.json").write_text(json.dumps(_TWO_DASHBOARD_REPORT))
        rc = run_audit_cli(
            _cli_args(
                dashboard_id="obs-migrate-apache-overview",
                migration_out=tmp,
                fail_on_error=False,
            ),
            dom_fetcher=lambda _u: _APACHE_DOM,
            field_fetcher=lambda _index: {"@timestamp", "host.name"},
        )
    assert rc == 0
    titles = [p["title"] for p in _stdout_json(capsys)["render"]["panels"]]
    assert titles == ["Requests per second"], (
        f"Docker's panel title segmented Apache's DOM: {titles}"
    )


def test_scoping_keeps_the_audited_dashboards_own_metadata():
    """The scoped report is the audited dashboard's, not the run's."""
    from observability_migration.targets.kibana.render_audit import (
        panel_titles_in_order,
        scope_report_to_dashboard,
    )
    scoped, note = scope_report_to_dashboard(
        _TWO_DASHBOARD_REPORT, "obs-migrate-docker-overview"
    )
    assert note == ""
    assert scoped is not None
    assert [d["title"] for d in scoped["dashboards"]] == ["Docker - Overview"]
    assert panel_titles_in_order(scoped) == ["Container CPU"]


def test_identity_keys_cover_the_uploaded_dashboard_id_and_the_title():
    from observability_migration.targets.kibana.render_audit import dashboard_identity_keys
    keys = dashboard_identity_keys(_TWO_DASHBOARD_REPORT["dashboards"][1])
    assert "Docker - Overview" in keys
    assert "obs-migrate-docker-overview" in keys


# --------------------------------------------------------------------------- #
# 2. Duplicate titles WITHIN one dashboard still resolve, in order
# --------------------------------------------------------------------------- #
#
# Kubernetes ships same-titled widget pairs (``Pods``, ``Containers``,
# ``Deployments``, ``DaemonSets``). A title-keyed dict collapses them, so the
# second panel silently vanishes from the audit; a naive title *list* makes both
# segments start at the first occurrence, so the second gets an empty chunk and
# is reported as a phantom render.

_DUP_DOM = (
    'StaticText "Pods" StaticText "No results found" '
    'StaticText "Pods" button "kube-1; Click: to show, x"'
)


def test_duplicate_titles_within_one_dashboard_resolve_in_order():
    segments, unmatched = segment_panels(_DUP_DOM, ["Pods", "Pods"])
    assert unmatched == []
    assert len(segments) == 2, f"duplicate title collapsed: {segments}"
    assert segments[0][0] == segments[1][0] == "Pods"
    assert "No results found" in segments[0][1]
    assert "kube-1" in segments[1][1]
    assert "kube-1" not in segments[0][1]


def test_a_duplicate_title_that_rendered_once_reports_the_missing_one():
    segments, unmatched = segment_panels('StaticText "Pods" line chart', ["Pods", "Pods"])
    assert len(segments) == 1
    assert unmatched == ["Pods"]


def test_panel_titles_in_order_keeps_duplicates():
    from observability_migration.targets.kibana.render_audit import panel_titles_in_order
    report = {"dashboards": [{"title": "Kubernetes - Overview", "panels": [
        {"title": "Pods", "kibana_type": "metric"},
        {"title": "Containers", "kibana_type": "metric"},
        {"title": "Pods", "kibana_type": "xy"},
        {"title": "Banner", "kibana_type": "group", "status": "skipped"},
    ]}]}
    assert panel_titles_in_order(report) == ["Pods", "Containers", "Pods"]


# --------------------------------------------------------------------------- #
# 3. An unmatchable dashboard reports the fact; it does NOT use all titles
# --------------------------------------------------------------------------- #


def test_unmatched_dashboard_reports_it_instead_of_using_every_title(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "migration_report.json").write_text(json.dumps(_TWO_DASHBOARD_REPORT))
        rc = run_audit_cli(
            _cli_args(
                dashboard_id="obs-migrate-some-other-dashboard",
                migration_out=tmp,
                fail_on_error=False,
            ),
            dom_fetcher=lambda _u: _APACHE_DOM,
            field_fetcher=lambda _index: {"@timestamp", "host.name"},
        )
    assert rc == 0
    captured = capsys.readouterr()
    render = json.loads(captured.out)["render"]
    assert render["panels"] == [], (
        "an unmatchable dashboard borrowed the whole report's titles: "
        f"{[p['title'] for p in render['panels']]}"
    )
    assert any("per-panel attribution unavailable" in r for r in render["reasons"]), (
        f"the unmatchable dashboard was not reported: {render['reasons']}"
    )
    assert render["status"] == "warn"
    assert "per-panel attribution unavailable" in captured.err


def test_scope_report_to_dashboard_notes_an_unmatchable_dashboard():
    from observability_migration.targets.kibana.render_audit import scope_report_to_dashboard
    scoped, note = scope_report_to_dashboard(_TWO_DASHBOARD_REPORT, "obs-migrate-nope")
    assert scoped is None
    assert "per-panel attribution unavailable" in note
    assert "obs-migrate-nope" in note


def test_a_single_dashboard_report_is_used_even_when_the_id_does_not_match():
    """``--migration-out`` documents a single-dashboard dir; nothing to confuse."""
    from observability_migration.targets.kibana.render_audit import (
        panel_titles_in_order,
        scope_report_to_dashboard,
    )
    report = {"dashboards": [{"panels": [{"title": "Only panel", "kibana_type": "line"}]}]}
    scoped, note = scope_report_to_dashboard(report, "whatever-id")
    assert note == ""
    assert scoped is not None
    assert panel_titles_in_order(scoped) == ["Only panel"]


def test_two_dashboards_claiming_one_key_are_not_guessed_between():
    from observability_migration.targets.kibana.render_audit import scope_report_to_dashboard
    report = {"dashboards": [
        {"title": "Shared Title", "panels": [{"title": "A", "kibana_type": "line"}]},
        {"title": "Shared Title", "panels": [{"title": "B", "kibana_type": "line"}]},
    ]}
    scoped, note = scope_report_to_dashboard(report, "obs-migrate-shared-title")
    assert scoped is None
    assert "per-panel attribution unavailable" in note


# --------------------------------------------------------------------------- #
# 4. No --migration-out still works
# --------------------------------------------------------------------------- #


def test_no_migration_out_still_classifies_the_whole_dashboard(capsys):
    rc = run_audit_cli(
        _cli_args(migration_out="", fail_on_error=True),
        dom_fetcher=lambda _u: "line chart web-1 rendered",
    )
    assert rc == 0
    render = _stdout_json(capsys)["render"]
    assert render["status"] == "pass"
    assert render["panels"] == []


def test_no_migration_out_still_fails_on_a_render_marker():
    rc = run_audit_cli(
        _cli_args(migration_out="", fail_on_error=True),
        dom_fetcher=lambda _u: "embPanel__error An error occurred while loading this panel",
    )
    assert rc == 1
