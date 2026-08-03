# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Self-test for the render-audit gate (positive + negative controls).

A gate that never fails is worthless. This mirrors the Layer-12 invariant
self-test and the verifier's mutation tests: start from a realistic CLEAN canary
render (which MUST pass — the positive control), then corrupt each panel in turn
with a real Kibana render-error marker and assert the gate (a) flips to fail and
(b) names the corrupted panel. It also pins the data-gap path: a missing-field
corruption is a warn, not a hard fail.
"""

from __future__ import annotations

import pytest

from observability_migration.targets.kibana.render_audit import classify_render_per_panel

# A realistic clean canary render (per-panel ``(title, rendered_text)``), shaped
# like the live snapshots captured against Kibana 9.5.0.
_CLEAN_PANELS: list[tuple[str, str]] = [
    ("canary timeseries", "line chart instance_1 instance_2 instance_3"),
    ("canary barchart", "bar chart instance_1 instance_2"),
    ("canary gauge", "Bullet chart 8.809"),
    ("canary stat", "6.983"),
    ("canary table", "instance grid instance_2 35,258.0"),
    ("canary piechart", "sunburst instance_1 instance_2 33%"),
    ("canary heatmap", "line chart le_1 le_2 le_3"),
    ("canary text", "Canary Kitchen-sink dashboard for migration render verification."),
]

# Real Kibana render-error markers (from BROWSER_ERROR_PATTERNS).
_CORRUPTIONS = {
    "lens_error_occurred": "An error occurred while loading this panel",
    "embeddable_error": "div embPanel__error dashboardPanelError",
    "invalid_column": "Provided column name or index is invalid: deadbeef-0000",
    "error_loading_data": "Error loading data",
}


def test_positive_control_clean_canary_passes():
    # If this ever fails, every corruption test below is vacuous.
    verdict = classify_render_per_panel(_CLEAN_PANELS, available_fields=["instance", "le"])
    assert verdict.status == "pass"
    assert all(p.status == "rendered" for p in verdict.panels)


@pytest.mark.parametrize("panel_idx", range(len(_CLEAN_PANELS)))
@pytest.mark.parametrize("corruption", list(_CORRUPTIONS.values()), ids=list(_CORRUPTIONS))
def test_each_corrupted_panel_is_caught_and_named(panel_idx, corruption):
    panels = list(_CLEAN_PANELS)
    title = panels[panel_idx][0]
    panels[panel_idx] = (title, corruption)

    # No available_fields => an "invalid column" cannot be excused as a field gap,
    # so every corruption is an unexplained render_error => hard fail.
    verdict = classify_render_per_panel(panels)

    assert verdict.status == "fail", f"gate missed corrupted panel {title!r}"
    errored = [p.title for p in verdict.panels if p.status == "error"]
    assert title in errored, f"{title!r} not flagged among {errored}"
    # The other panels must still read as rendered (no over-flagging).
    assert sum(1 for p in verdict.panels if p.status == "error") == 1


def test_field_gap_corruption_warns_not_fails():
    # An "invalid column" on a panel whose breakdown field is absent from the
    # target is a data-readiness warn, NOT a translator failure.
    panels = list(_CLEAN_PANELS)
    panels[0] = ("canary timeseries", _CORRUPTIONS["invalid_column"])
    verdict = classify_render_per_panel(
        panels,
        breakdown_by_title={"canary timeseries": ["method"]},
        available_fields=["instance", "le"],
    )
    assert verdict.status == "warn"
    gap = [p for p in verdict.panels if p.error_class == "field_gap"]
    assert [p.title for p in gap] == ["canary timeseries"]


# Issue #282: a late-bound ``by ($grouping)`` panel emits a STABLE breakdown
# accessor — the aliased column ``grouping`` from ``STATS ... BY grouping =
# ??grouping`` — not the field the viewer selects (``exporter``/``transport``/
# ``receiver``). That alias is always present in the panel's own output, so an
# "invalid column" here is a genuine Lens accessor bug (the #282 render failure
# mode), never a data/field gap.
_LATE_BOUND_GROUPING_PANEL = ("otel spans late-bound grouping", "line chart exporter transport receiver")


def test_late_bound_grouping_clean_render_passes():
    # The stable ``grouping`` alias is present, so the panel renders cleanly.
    verdict = classify_render_per_panel(
        [_LATE_BOUND_GROUPING_PANEL],
        breakdown_by_title={_LATE_BOUND_GROUPING_PANEL[0]: ["grouping"]},
        available_fields=["grouping", "exporter", "transport", "receiver"],
    )
    assert verdict.status == "pass"


def test_late_bound_grouping_invalid_column_is_render_error_not_field_gap():
    # If the breakdown accessor is ever wired to the wrong column (the #282 bug),
    # Kibana raises "invalid column". Because the stable ``grouping`` alias IS in
    # the target, this cannot be excused as a field gap — it must hard-fail as a
    # render_error so the accessor-wiring regression is caught.
    panels = [(_LATE_BOUND_GROUPING_PANEL[0], _CORRUPTIONS["invalid_column"])]
    verdict = classify_render_per_panel(
        panels,
        breakdown_by_title={_LATE_BOUND_GROUPING_PANEL[0]: ["grouping"]},
        available_fields=["grouping", "exporter", "transport", "receiver"],
    )
    assert verdict.status == "fail"
    hard = [p for p in verdict.panels if p.error_class == "render_error"]
    assert [p.title for p in hard] == [_LATE_BOUND_GROUPING_PANEL[0]]


def test_console_render_error_is_caught():
    verdict = classify_render_per_panel(
        _CLEAN_PANELS,
        console_errors=["Error: Provided column name or index is invalid: abc"],
    )
    assert verdict.status == "fail"


def test_server_5xx_is_caught():
    verdict = classify_render_per_panel(
        _CLEAN_PANELS,
        failed_requests=["POST /api/dashboards 503 Service Unavailable"],
    )
    assert verdict.status == "fail"
