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


# --------------------------------------------------------------------------- #
# 5. A title that is a strict PREFIX of a sibling title in the SAME dashboard
# --------------------------------------------------------------------------- #
#
# ``_ensure_unique_leaf_panel_titles`` disambiguates a repeated Datadog widget
# title by appending ``(widget <id>)``, so every duplicated title is BY
# CONSTRUCTION a strict prefix of its disambiguated sibling. Searching the DOM in
# report order let the short one match inside the long one's rendered title text
# and take a zero-length chunk, while its real region was absorbed by whichever
# panel preceded it. Live in Docker - Overview: ``Running containers by image``
# matched at offset 82147 inside ``Running containers by image (widget 27)`` and
# its own region (100347-116809) was attributed to ``Datadog event timeline 10``.
#
# A zero-length chunk is not a visible failure — ``classify_panel`` reads it as a
# clean ``rendered`` panel with no data — so the misattribution surfaced as a
# phantom green record. Segmentation must therefore let the most specific title
# claim its DOM text first.


def _chunks(segments):
    return {title: chunk for title, chunk in segments}


def test_a_prefix_title_does_not_steal_its_longer_siblings_chunk():
    dom = (
        'StaticText "Running containers by image" line chart '
        'button "redis; Click: to show, x" '
        'StaticText "Running containers by image (widget 27)" '
        'StaticText "No results found"'
    )
    segments, unmatched = segment_panels(
        dom, ["Running containers by image", "Running containers by image (widget 27)"]
    )
    assert unmatched == []
    assert len(segments) == 2
    by_title = _chunks(segments)
    assert all(chunk for chunk in by_title.values()), f"zero-length chunk: {segments}"
    assert "redis" in by_title["Running containers by image"]
    assert "No results found" not in by_title["Running containers by image"]
    assert "No results found" in by_title["Running containers by image (widget 27)"]


def test_the_disambiguated_sibling_rendered_first_still_resolves():
    """The live Docker DOM order: ``(widget 27)`` draws before the bare title."""
    dom = (
        'StaticText "Running containers by image (widget 27)" '
        'StaticText "No results found" '
        'StaticText "Running containers by image" line chart '
        'button "redis; Click: to show, x"'
    )
    segments, unmatched = segment_panels(
        dom, ["Running containers by image", "Running containers by image (widget 27)"]
    )
    assert unmatched == []
    by_title = _chunks(segments)
    assert all(chunk for chunk in by_title.values()), f"zero-length chunk: {segments}"
    assert "No results found" in by_title["Running containers by image (widget 27)"]
    assert "redis" in by_title["Running containers by image"]
    assert "redis" not in by_title["Running containers by image (widget 27)"]


def test_a_three_way_prefix_chain_resolves_most_specific_first():
    """Docker carries all three: ``Running containers`` < ``... by image`` < ``... (widget 27)``."""
    dom = (
        'StaticText "Running containers by image (widget 27)" StaticText "No results found" '
        'StaticText "Running containers by image" button "redis; Click: to show, x" '
        'StaticText "Running containers" StaticText "4"'
    )
    titles = [
        "Running containers by image",
        "Running containers",
        "Running containers by image (widget 27)",
    ]
    segments, unmatched = segment_panels(dom, titles)
    assert unmatched == []
    assert len(segments) == 3
    by_title = _chunks(segments)
    assert all(chunk for chunk in by_title.values()), f"zero-length chunk: {segments}"
    assert "No results found" in by_title["Running containers by image (widget 27)"]
    assert "redis" in by_title["Running containers by image"]
    assert '"4"' in by_title["Running containers"]


def test_a_prefix_skips_every_occurrence_of_its_sibling_not_just_the_claimed_one():
    """The real DOM is HTML: each title appears three times per panel.

    Kibana writes the title into the header ``<span>``, the wrapper's
    ``data-title`` and an svg ``aria-label``. Rejecting only the occurrence the
    longer title *claimed* leaves the other two open, and live in Docker the bare
    title landed in the twin's ``data-title`` — still the twin's region, and the
    verdict that follows is plausible enough to pass unnoticed because the twin
    runs the same query.
    """
    dom = (
        '<span data-test-subj="embeddablePanelTitle">'
        "Running containers by image (widget 27)</span>"
        '<div data-title="Running containers by image (widget 27)">'
        '<svg aria-label="Running containers by image (widget 27)">bar chart</svg>'
        '<button>redis; Click: to show, x</button></div>'
        '<span data-test-subj="embeddablePanelTitle">Running containers by image</span>'
        '<div data-title="Running containers by image">'
        '<div class="embeddableError">An error occurred while loading this panel</div></div>'
    )
    segments, unmatched = segment_panels(
        dom, ["Running containers by image", "Running containers by image (widget 27)"]
    )
    assert unmatched == []
    by_title = _chunks(segments)
    assert "embeddableError" in by_title["Running containers by image"]
    assert "embeddableError" not in by_title["Running containers by image (widget 27)"]
    # The twin's own rendered content must stay with the twin.
    for marker in ("bar chart", "redis"):
        assert marker in by_title["Running containers by image (widget 27)"], (
            f"the twin lost {marker!r} to its prefix: "
            f"{by_title['Running containers by image'][:120]!r}"
        )
        assert marker not in by_title["Running containers by image"]


def test_a_duplicate_pair_and_a_prefixed_sibling_coexist():
    """``Pods``, ``Pods`` and ``Pods (widget 12)``: the rules must not fight.

    The two bare panels take successive DOM occurrences (d8bf920's cursor), and
    the disambiguated one takes its own text rather than lending its prefix to a
    bare sibling.
    """
    dom = (
        'StaticText "Pods" StaticText "7" '
        'StaticText "Pods (widget 12)" StaticText "No results found" '
        'StaticText "Pods" button "kube-1; Click: to show, x"'
    )
    segments, unmatched = segment_panels(dom, ["Pods", "Pods", "Pods (widget 12)"])
    assert unmatched == []
    assert len(segments) == 3
    assert [t for t, _c in segments] == ["Pods", "Pods (widget 12)", "Pods"]
    chunks = [c for _t, c in segments]
    assert all(chunks), f"zero-length chunk: {segments}"
    assert '"7"' in chunks[0] and "kube-1" not in chunks[0]
    assert "No results found" in chunks[1]
    assert "kube-1" in chunks[2]


def test_a_prefix_present_only_inside_its_sibling_is_reported_not_swallowed():
    """The short panel really did not draw: say so, don't hand it an empty chunk.

    An empty chunk reads as a clean ``rendered`` panel, which is how this bug
    hid. ``unmatched`` is the honest answer and the driver surfaces it.
    """
    dom = 'StaticText "Pods desired (widget 6)" StaticText "No results found"'
    segments, unmatched = segment_panels(dom, ["Pods desired", "Pods desired (widget 6)"])
    assert [t for t, _c in segments] == ["Pods desired (widget 6)"]
    assert all(chunk for _t, chunk in segments), f"zero-length chunk: {segments}"
    assert unmatched == ["Pods desired"]


def test_segmentation_never_yields_an_empty_chunk_for_kubernetes_prefix_families():
    """Kubernetes' real prefix families: 16 pairs across ``Pods``/``Ready``.

    The DOM is laid out in the REVERSE of report order, because report order is
    widget order and DOM order is layout order — the two agreeing is luck, and
    when they disagree report-order search sends ``Pods`` into ``Pods unavailable
    (widget 9)``.
    """
    titles = [
        "Pods", "Ready", "Pods ready", "Pods desired", "Pods desired (widget 6)",
        "Pods desired (widget 7)", "Pods available", "Pods available (widget 5)",
        "Pods unavailable", "Pods unavailable (widget 9)", "Ready (widget 38)",
        "Ready (widget 30)", "Ready state by node", "Not ready", "Not ready (widget 31)",
    ]
    dom = " ".join(
        f'StaticText "{t}" StaticText "{i}"'
        for i, t in reversed(list(enumerate(titles)))
    )
    segments, unmatched = segment_panels(dom, titles)
    assert unmatched == []
    assert sorted(t for t, _c in segments) == sorted(titles)
    empty = [t for t, chunk in segments if not chunk]
    assert empty == [], f"zero-length chunks: {empty}"
    for i, title in enumerate(titles):
        chunk = _chunks(segments)[title]
        assert f'"{i}"' in chunk, f"{title} took the wrong region: {chunk!r}"


_PREFIX_REPORT = {
    "dashboards": [
        {
            "title": "Docker - Overview",
            "panels": [
                {
                    "title": "Running containers by image",
                    "kibana_type": "bar",
                    "yaml_panel": {"esql": {
                        "type": "bar",
                        "query": "FROM metrics-* | STATS c=MAX(docker_containers_running) BY docker_image",
                        "breakdown": {"field": "docker_image"},
                    }},
                },
                {
                    "title": "Running containers by image (widget 27)",
                    "kibana_type": "bar",
                    "yaml_panel": {"esql": {
                        "type": "bar",
                        "query": "FROM metrics-* | STATS c=MAX(docker_containers_running) BY docker_image",
                        "breakdown": {"field": "docker_image"},
                    }},
                },
            ],
        }
    ]
}


def test_the_prefixed_panel_is_judged_against_its_own_rendered_region(capsys):
    """End to end: the bare title's verdict comes from its OWN chunk.

    Pre-fix it took a zero-length chunk and was reported ``rendered`` — a phantom
    — while the region that actually errored went to its sibling.
    """
    dom = (
        'StaticText "Running containers by image (widget 27)" bar chart '
        'button "redis; Click: to show, x" '
        'StaticText "Running containers by image" '
        'embPanel__error An error occurred while loading this panel'
    )
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "migration_report.json").write_text(json.dumps(_PREFIX_REPORT))
        rc = run_audit_cli(
            _cli_args(
                dashboard_id="obs-migrate-docker-overview",
                migration_out=tmp,
                fail_on_error=False,
            ),
            dom_fetcher=lambda _u: dom,
            field_fetcher=lambda _index: {"@timestamp", "host.name"},
        )
    assert rc == 0
    panels = {p["title"]: p for p in _stdout_json(capsys)["render"]["panels"]}
    assert set(panels) == {
        "Running containers by image", "Running containers by image (widget 27)"
    }
    assert panels["Running containers by image"]["status"] == "error", (
        "the bare title took a zero-length chunk and read as a clean render: "
        f"{panels['Running containers by image']}"
    )
    assert panels["Running containers by image (widget 27)"]["status"] == "rendered"


def test_a_title_that_did_not_render_is_reported_even_when_others_warn(capsys):
    """``unmatched`` must reach ``render.reasons`` regardless of the verdict.

    The unmatched list was only reported when the verdict was still ``pass``, so a
    dashboard with any data-readiness warn swallowed "this panel never drew" —
    the same silence that let the prefix bug hide.
    """
    dom = (
        'StaticText "Running containers by image (widget 27)" '
        'StaticText "No results found"'
    )
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "migration_report.json").write_text(json.dumps(_PREFIX_REPORT))
        rc = run_audit_cli(
            _cli_args(
                dashboard_id="obs-migrate-docker-overview",
                migration_out=tmp,
                fail_on_error=False,
            ),
            dom_fetcher=lambda _u: dom,
            field_fetcher=lambda _index: {"@timestamp", "host.name"},
        )
    assert rc == 0
    render = _stdout_json(capsys)["render"]
    assert render["status"] == "warn"
    assert any("Running containers by image" in r and "did not render" in r
               for r in render["reasons"]), (
        f"a panel that never drew was not reported: {render['reasons']}"
    )
