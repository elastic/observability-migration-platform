# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the dashboard QA harness itself.

A checker nobody checks is worse than no checker: it reports PASS and is believed.
Every case below is either a defect this harness was written to catch (asserted
to fail) or a shape that previously tripped it wrongly (asserted to pass).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "dashboard_qa", Path(__file__).resolve().parents[1] / "scripts" / "dashboard_qa.py"
)
qa = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(qa)


def _panel(title, x, y, w, h, ptype="xy", **config):
    return {
        "grid": {"x": x, "y": y, "w": w, "h": h},
        "type": "vis",
        "config": {"type": ptype, "title": title, **config},
    }


def _row(title, panels, collapsed=False):
    return {"title": title, "collapsed": collapsed, "panels": panels}


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #
def test_layout_flags_the_ragged_row_it_was_written_for():
    """The real pre-fix geometry of Node Exporter Full's first row.

    Six gauges h=12 beside a stack of 6 and 6 is flush. The bug produced 8 beside
    3 and 6: the stack overhung the gauges by a row because the per-type floor was
    applied per panel and rejected on collision.
    """
    ragged = _row("Quick CPU / Mem / Disk", [
        _panel("Pressure", 0, 0, 36, 8, "gauge"),
        _panel("CPU Cores", 36, 0, 12, 3, "metric"),
        _panel("RootFS Total", 36, 3, 12, 6, "metric"),
    ])
    issues = qa.check_layout({"panels": [ragged]})
    assert any("hangs below its neighbours" in i for i in issues), issues


def test_layout_accepts_the_fixed_flush_row():
    flush = _row("Quick CPU / Mem / Disk", [
        _panel("Pressure", 0, 0, 36, 12, "gauge"),
        _panel("CPU Cores", 36, 0, 12, 6, "metric"),
        _panel("RootFS Total", 36, 6, 12, 6, "metric"),
    ])
    assert qa.check_layout({"panels": [flush]}) == []


def test_layout_accepts_a_half_full_last_line():
    """Filling left to right leaves the last line short on the RIGHT.

    That is ordinary authoring -- Node Exporter Full's "Storage Disk" row is
    exactly this in Grafana itself -- and an earlier version of the check called
    it a failure.
    """
    trailing = _row("Storage Disk", [
        _panel("A", 0, 0, 24, 20), _panel("B", 24, 0, 24, 20),
        _panel("C", 0, 20, 24, 20),          # last line, left half only
    ])
    assert qa.check_layout({"panels": [trailing]}) == []


def test_layout_accepts_a_last_line_that_sits_on_the_right():
    """Node Exporter Full's "System Processes" ends with one half-width panel at
    x=24, with nothing beside it -- and it is authored that way in Grafana itself.

    An earlier version of the check keyed off "the deepest columns start at x=0",
    which assumed authors always fill left to right, and so called this ragged.
    """
    trailing_right = _row("System Processes", [
        _panel("A", 0, 0, 24, 15), _panel("B", 24, 0, 24, 15),
        _panel("C", 0, 15, 24, 15), _panel("D", 24, 15, 24, 15),
        _panel("Threads", 24, 30, 24, 15),   # last line, RIGHT half only
    ])
    assert qa.check_layout({"panels": [trailing_right]}) == []

def test_layout_flags_overlap_and_overflow_and_zero_size():
    bad = _row("Row", [
        _panel("A", 0, 0, 24, 6),
        _panel("B", 12, 0, 24, 6),      # overlaps A
        _panel("C", 40, 12, 16, 6),     # 40+16 > 48
        _panel("D", 0, 24, 0, 6),       # zero width
    ])
    issues = qa.check_layout({"panels": [bad]})
    assert any("overlaps" in i for i in issues), issues
    assert any("overflows" in i for i in issues), issues
    assert any("zero-size" in i for i in issues), issues


def test_layout_accepts_a_normal_two_by_two_grid():
    grid = _row("Row", [
        _panel("A", 0, 0, 24, 12), _panel("B", 24, 0, 24, 12),
        _panel("C", 0, 12, 24, 12), _panel("D", 24, 12, 24, 12),
    ])
    assert qa.check_layout({"panels": [grid]}) == []


# --------------------------------------------------------------------------- #
# ui
# --------------------------------------------------------------------------- #
def test_ui_flags_an_axis_titled_with_an_internal_column():
    """`label` is the synthetic column the bargauge path builds.

    Kibana titles the axis after the column, so leaving it captions the panel
    with our internal name -- which is what the bargauge did on screen.
    """
    panel = _panel("Pressure", 0, 0, 24, 12, "xy",
                   axis={"x": {"title": {"text": "label"}}})
    issues = qa.check_ui({"panels": [_row("Row", [panel])]})
    assert any("internal column" in i for i in issues), issues


def test_ui_accepts_a_meaningful_axis_title():
    panel = _panel("CPU", 0, 0, 24, 12, "xy", axis={"y": {"title": {"text": "%"}}})
    assert qa.check_ui({"panels": [_row("Row", [panel])]}) == []


def test_ui_does_not_flag_non_visualization_tiles():
    """These carry their type on the panel and have no visualization at all.

    Flagging them was this harness's own false positive: every curated
    absent-metric note and the dashboard-links tile came back as a failure.
    """
    payload = {"panels": [_row("Row", [
        {"grid": {"x": 0, "y": 0, "w": 24, "h": 6}, "type": "markdown",
         "config": {"title": "Note", "content": "text"}},
        {"grid": {"x": 24, "y": 0, "w": 24, "h": 3}, "type": "links",
         "config": {"title": "Dashboard Links"}},
        {"grid": {"x": 0, "y": 6, "w": 24, "h": 6}, "type": "image",
         "config": {"title": "image widget",
                    "image_config": {"src": {"type": "url", "url": "http://x/y.png"}}}},
    ])]}
    assert qa.check_ui(payload) == []


def test_ui_flags_a_metric_panel_with_no_metric():
    panel = _panel("Empty metric", 0, 0, 12, 6, "metric")
    issues = qa.check_ui({"panels": [_row("Row", [panel])]})
    assert any("no metric configured" in i for i in issues), issues


# --------------------------------------------------------------------------- #
# payload traversal
# --------------------------------------------------------------------------- #
def test_panel_queries_reads_layers_not_just_the_direct_source():
    """Multi-series panels keep their query per layer.

    Reading only config.data_source missed every xy panel -- two on MySQL
    Overview stayed red while the harness called them fine.
    """
    panel = {
        "config": {
            "title": "Multi",
            "layers": [
                {"data_source": {"query": "TS a | STATS x = MAX(m1)"}},
                {"data_source": {"query": "TS a | STATS y = MAX(m2)"}},
            ],
        }
    }
    assert qa.panel_queries(panel) == [
        "TS a | STATS x = MAX(m1)",
        "TS a | STATS y = MAX(m2)",
    ]


def test_iter_panels_descends_into_rows_and_reports_the_row():
    payload = {"panels": [
        _row("Row A", [_panel("P1", 0, 0, 24, 6)]),
        _row("Row B", [_panel("P2", 0, 0, 24, 6)], collapsed=True),
    ]}
    found = [(row, qa.panel_title(p)) for row, p in qa.iter_panels(payload)]
    assert found == [("Row A", "P1"), ("Row B", "P2")]


def test_expand_rows_reports_and_expands_only_collapsed_rows():
    """Collapsed rows never render, so the browser audit cannot see inside them."""
    payload = {"panels": [
        _row("Open", [_panel("P1", 0, 0, 24, 6)]),
        _row("Shut", [_panel("P2", 0, 0, 24, 6)], collapsed=True),
        _row("Also shut", [_panel("P3", 0, 0, 24, 6)], collapsed=True),
    ]}
    assert qa.expand_rows(payload) == 2
    assert all(not row.get("collapsed") for row in payload["panels"])
