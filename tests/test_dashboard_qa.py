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
# multi-value parameter detection (issue #353)
# --------------------------------------------------------------------------- #
def test_is_multi_value_param_recognises_bare_mv_contains():
    query = 'FROM metrics-* | WHERE MV_CONTAINS(?instance, ".*") OR MV_CONTAINS(?instance, instance)'
    assert qa._is_multi_value_param("instance", query) is True


def test_is_multi_value_param_recognises_to_string_wrapped_mv_contains():
    """Real translator output now wraps the parameter in ``TO_STRING(...)``
    (issue #353's type-safety guardrail). Missing this shape here would
    silently bind a multi-select parameter as a scalar string instead of a
    list, breaking the QA harness's own query execution for every
    multi-select-controlled panel."""
    query = (
        'FROM metrics-* | WHERE MV_CONTAINS(TO_STRING(?instance), ".*") '
        "OR MV_CONTAINS(TO_STRING(?instance), instance)"
    )
    assert qa._is_multi_value_param("instance", query) is True


def test_is_multi_value_param_false_for_a_scalar_binding():
    query = "FROM metrics-* | WHERE instance == ?instance"
    assert qa._is_multi_value_param("instance", query) is False


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


# --------------------------------------------------------------------------- #
# comparison instant
# --------------------------------------------------------------------------- #
def test_bucket_end_advances_the_compared_instant_by_one_bucket():
    """`time_bucket` is the bucket START, but its value describes the whole span.

    LAST_OVER_TIME returns the last sample in the bucket, near its END, so asking
    Prometheus about the start compared two instants a full bucket apart.
    """
    stamps = ["2026-08-02T10:00:00.000Z", "2026-08-02T10:07:00.000Z", "2026-08-02T10:14:00.000Z"]
    assert qa._bucket_end(stamps, stamps[-2]) == "2026-08-02T10:14:00+00:00Z".replace("+00:00", "")


def test_bucket_end_degrades_to_the_start_when_it_cannot_tell():
    """A single bucket, or an unparseable stamp, must not break the comparison."""
    assert qa._bucket_end(["2026-08-02T10:00:00.000Z"], "2026-08-02T10:00:00.000Z") == \
        "2026-08-02T10:00:00.000Z"
    assert qa._bucket_end(["not-a-date", "also-not"], "not-a-date") == "not-a-date"


def test_match_series_does_not_call_a_defect_what_the_reference_cannot_hold_still():
    """A series whose own reference moves more than the tolerance is uncomparable.

    ES and Prometheus scrape independently, so for a metric that swings between
    consecutive scrapes they hold different samples and no comparison instant
    helps. node_scrape_collector_duration_seconds read 9.5e-05 in ES and 3.53e-04
    in Prometheus four seconds apart; counting that as a translation defect let
    one panel dominate the dashboard's disagreements.
    """
    ours = [({"collector": "arp"}, 0.000095), ({"collector": "cpu"}, 5.0)]
    theirs = [({"collector": "arp"}, 0.000353), ({"collector": "cpu"}, 10.0)]
    noise = {(("collector", "arp"),): 3.7, (("collector", "cpu"),): 0.001}
    agree, differ, unmatched, _ = qa._match_series(ours, theirs, 0.05, noise)
    assert (agree, differ, unmatched) == (0, 1, 1)   # arp excused, cpu still a defect


def test_match_series_without_noise_data_behaves_as_before():
    ours = [({"d": "x"}, 1.0)]
    theirs = [({"d": "x"}, 2.0)]
    assert qa._match_series(ours, theirs, 0.05)[1] == 1


# --------------------------------------------------------------------------- #
# PROMQL passthrough comparison
# --------------------------------------------------------------------------- #
def _make_es_stub(columns, rows):
    """Return a _es-compatible callable that returns the given table."""
    def _stub(_url, _body, _key=""):
        return {
            "columns": [{"name": c} for c in columns],
            "values": rows,
        }
    return _stub


def test_promql_passthrough_uses_last_step_not_penultimate(monkeypatch):
    """PROMQL @timestamp is the evaluation instant, not a bucket start.

    The 5m lookback window ends AT @timestamp.  The last step is valid (PROMQL
    uses a fixed lookback, not bucket bounds), so we use it -- not the
    penultimate.  Using the penultimate and then advancing by the step width
    compared ES's value at t_{n-1} against Prometheus at t_n: two different
    5m windows, causing ~12% error on CPU Busy.
    """
    t1 = "2026-08-02T10:00:00.000Z"
    t2 = "2026-08-02T10:01:00.000Z"
    t3 = "2026-08-02T10:02:00.000Z"
    # Three PROMQL steps; the last one has value 3.0 (distinct from 1.0 and 2.0).
    rows = [[t1, 1.0], [t2, 2.0], [t3, 3.0]]
    monkeypatch.setattr(qa, "_es", _make_es_stub(["@timestamp", "value"], rows))

    query = "PROMQL index=metrics-* step=1m value=(avg(rate(x[5m])))"
    series, chosen = qa._our_last_bucket_series(
        "http://es", query, t1, t3, ""
    )
    # The value from the LAST step (3.0), not the penultimate (2.0).
    assert series == [({}, 3.0)], series
    # The comparison instant is t3 itself -- NOT advanced by _bucket_end.
    assert chosen == t3, chosen


def test_esql_ts_uses_penultimate_bucket_and_advances_to_end(monkeypatch):
    """ES|QL TS queries: time_bucket is the bucket START; the final bucket is
    partial (rate reads low), so we take the penultimate and advance to its end.

    The end of the penultimate bucket equals the start of the last bucket, so
    both sides compare at the same LAST_OVER_TIME instant.
    """
    t1 = "2026-08-02T10:00:00.000Z"
    t2 = "2026-08-02T10:07:00.000Z"
    t3 = "2026-08-02T10:14:00.000Z"
    rows = [[t1, 1.0], [t2, 2.0], [t3, 3.0]]
    monkeypatch.setattr(qa, "_es", _make_es_stub(["time_bucket", "value"], rows))

    query = "TS metrics-*\n| STATS value = MAX(LAST_OVER_TIME(x)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)"
    series, chosen = qa._our_last_bucket_series(
        "http://es", query, t1, t3, ""
    )
    # Value comes from the PENULTIMATE bucket (2.0), not the last (3.0).
    assert series == [({}, 2.0)], series
    # Comparison instant is the END of the penultimate bucket = start of last = t3.
    assert "10:14" in chosen, chosen

