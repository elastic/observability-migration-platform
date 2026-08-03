# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for the render-audit classifier (offline core of the live gate)."""

from __future__ import annotations

from observability_migration.targets.kibana.render_audit import (
    breakdown_fields_by_panel,
    classify_panel,
    classify_render,
    classify_render_per_panel,
    find_render_error_markers,
)

# A trimmed but representative clean Kibana dashboard a11y snapshot.
_CLEAN_SNAPSHOT = """
RootWebArea "obs-migrate canary (kitchen sink)"
  region "canary timeseries" -> Lens visualization xychart
  region "canary metric" -> 1,234
  region "canary datatable" -> table with rows
  region "canary markdown" -> Canary kitchen-sink dashboard
"""

_ERROR_SNAPSHOT = """
RootWebArea "obs-migrate canary (kitchen sink)"
  region "canary timeseries" -> div embPanel__error "An error occurred while loading this panel"
  region "canary metric" -> 1,234
"""


def test_clean_snapshot_passes():
    verdict = classify_render(_CLEAN_SNAPSHOT)
    assert verdict.status == "pass"
    assert verdict.rendered_error_markers == []
    assert verdict.reasons == []


def test_lens_error_embeddable_fails():
    verdict = classify_render(_ERROR_SNAPSHOT)
    assert verdict.status == "fail"
    assert any("embPanel__error" in m or "An error occurred" in m
               for m in verdict.rendered_error_markers)


def test_console_esql_error_fails_even_with_clean_dom():
    verdict = classify_render(
        _CLEAN_SNAPSHOT,
        console_errors=["[ES|QL] Unknown column 'foo' in query"],
    )
    assert verdict.status == "fail"
    assert verdict.console_errors


def test_unrelated_console_noise_does_not_fail():
    verdict = classify_render(
        _CLEAN_SNAPSHOT,
        console_errors=["Deprecation warning: some unrelated browser API"],
    )
    assert verdict.status == "pass"
    assert verdict.console_errors == []


def test_server_5xx_fails():
    verdict = classify_render(
        _CLEAN_SNAPSHOT,
        failed_requests=["POST /api/dashboards 503 Service Unavailable"],
    )
    assert verdict.status == "fail"
    assert verdict.server_errors


def test_non_5xx_failed_request_warns_not_fails():
    verdict = classify_render(
        _CLEAN_SNAPSHOT,
        failed_requests=["GET /api/foo 404 Not Found"],
        screenshot_ok=True,
    )
    # 404 is not a render failure; clean DOM + no 5xx/console => pass.
    assert verdict.status == "pass"


def test_missing_screenshot_warns_when_otherwise_clean():
    verdict = classify_render(_CLEAN_SNAPSHOT, screenshot_ok=False)
    assert verdict.status == "warn"
    assert "screenshot missing or empty" in verdict.reasons


def test_csp_and_404_console_noise_does_not_fail():
    # Regression: a CSP violation referencing kibana.estccdn.com and a 404
    # resource load are benign platform noise, not render failures. (The old
    # broad "kibana" keyword filter wrongly failed on the CSP message.)
    verdict = classify_render(
        _CLEAN_SNAPSHOT,
        console_errors=[
            "Executing inline script violates the following Content Security "
            "Policy directive 'script-src 'self' kibana.estccdn.com'.",
            "Failed to load resource: the server responded with a status of 404 ()",
        ],
    )
    assert verdict.status == "pass"
    assert verdict.console_errors == []


def test_bare_invalid_column_console_error_fails():
    # Regression: the real render error "Provided column name or index is invalid"
    # fires in the console without the words kibana/esql/lens; it must still fail.
    verdict = classify_render(
        "clean dom no markers",
        console_errors=["Error: Provided column name or index is invalid: abc-123"],
    )
    assert verdict.status == "fail"
    assert verdict.console_errors


def test_find_render_error_markers_lists_distinct_patterns():
    markers = find_render_error_markers(
        "dashboardPanelError and embPanel__error both present"
    )
    assert "dashboardPanelError" in markers
    assert "embPanel__error" in markers


# --- Per-panel verdicts + field-gap classification -------------------------

_INVALID_COLUMN = "Provided column name or index is invalid: f4b867ee"


def test_classify_panel_rendered():
    r = classify_panel("canary table", "instance_2 35,258.0 grid rows")
    assert r.status == "rendered"
    assert r.error_class == ""


def test_classify_panel_empty():
    r = classify_panel("canary gauge", "No results found")
    assert r.status == "empty"


def test_classify_panel_render_error_without_field_context():
    # No available_fields supplied -> cannot attribute to a field gap -> hard error.
    r = classify_panel("canary timeseries", _INVALID_COLUMN)
    assert r.status == "error"
    assert r.error_class == "render_error"


def test_classify_panel_field_gap_when_breakdown_field_missing():
    # Today's real scenario: panel breaks down by `method`, which the target
    # data lacks -> field_gap (data-readiness), not a render bug.
    r = classify_panel(
        "canary timeseries", _INVALID_COLUMN,
        breakdown_fields=["method"], available_fields=["instance", "handler", "le"],
    )
    assert r.status == "error"
    assert r.error_class == "field_gap"
    assert r.missing_fields == ["method"]


def test_classify_panel_field_gap_when_named_unknown_column_missing():
    # Filter/log fields named in ``Unknown column [field]`` (not just breakdowns)
    # are data-readiness gaps when absent from the target index.
    text = (
        "Unexpected error from Elasticsearch\n"
        "Found 1 problem\nline 2:106: Unknown column [http.status_code]"
    )
    r = classify_panel(
        "NGINX Error logs",
        text,
        available_fields=["message", "service.name", "host.name"],
    )
    assert r.status == "error"
    assert r.error_class == "field_gap"
    assert r.missing_fields == ["http.status_code"]


def test_classify_panel_render_error_when_named_unknown_column_present():
    # Field is in the target but Lens still errors -> keep hard render_error.
    text = (
        "Unexpected error from Elasticsearch\n"
        "Unknown column [http.status_code]"
    )
    r = classify_panel(
        "NGINX Error logs",
        text,
        available_fields=["http.status_code", "message"],
    )
    assert r.status == "error"
    assert r.error_class == "render_error"


def test_classify_panel_render_error_when_breakdown_field_present():
    # Breakdown field exists but Lens still errors -> a real render bug.
    r = classify_panel(
        "canary timeseries", _INVALID_COLUMN,
        breakdown_fields=["instance"], available_fields=["instance", "handler"],
    )
    assert r.status == "error"
    assert r.error_class == "render_error"


def test_per_panel_field_gap_warns_not_fails():
    verdict = classify_render_per_panel(
        [("canary timeseries", _INVALID_COLUMN), ("canary table", "instance_2 rows")],
        breakdown_by_title={"canary timeseries": ["method"]},
        available_fields=["instance"],
    )
    assert verdict.status == "warn"
    assert any(p.error_class == "field_gap" for p in verdict.panels)


def test_per_panel_unexplained_render_error_fails():
    verdict = classify_render_per_panel(
        [("canary timeseries", _INVALID_COLUMN)],
        breakdown_by_title={"canary timeseries": ["instance"]},
        available_fields=["instance"],
    )
    assert verdict.status == "fail"


def test_per_panel_all_clean_passes():
    verdict = classify_render_per_panel(
        [("a", "line chart instance_1"), ("b", "Kitchen-sink markdown")],
        available_fields=["instance"],
    )
    assert verdict.status == "pass"
    assert [p.status for p in verdict.panels] == ["rendered", "rendered"]


def test_breakdown_fields_by_panel_extracts_breakdown_only():
    report = {
        "dashboards": [{
            "panels": [
                {"title": "ts", "yaml_panel": {"esql": {
                    "dimension": {"field": "step"},
                    "metrics": [{"field": "value"}],
                    "breakdown": {"field": "method"},
                }}},
                {"title": "tbl", "yaml_panel": {"esql": {
                    "breakdown": [{"field": "instance"}, {"field": "mode"}],
                }}},
                {"title": "heatmap", "yaml_panel": {"esql": {
                    "y_axis": {"field": "le"},
                }}},
                {"title": "datatable", "yaml_panel": {"esql": {
                    "breakdowns": [{"field": "pod"}, {"field": "namespace"}],
                }}},
                {"title": "stat", "yaml_panel": {"esql": {"metrics": [{"field": "value"}]}}},
            ],
        }],
    }
    out = breakdown_fields_by_panel(report)
    assert out["ts"] == ["method"]          # value/step excluded
    assert out["tbl"] == ["instance", "mode"]
    assert out["heatmap"] == ["le"]
    assert out["datatable"] == ["pod", "namespace"]
    assert "stat" not in out                 # no breakdown -> not listed


# --- Empty-state classification (#3) ---------------------------------------

def test_empty_panel_with_query_is_unexpected_empty():
    r = classify_panel("ts", "No results found", expects_data=True)
    assert r.status == "empty"
    assert r.error_class == "unexpected_empty"


def test_empty_panel_without_query_is_benign():
    r = classify_panel("markdown", "No results found", expects_data=False)
    assert r.status == "empty"
    assert r.error_class == ""


def test_empty_panel_with_missing_metric_is_data_gap():
    r = classify_panel(
        "ts", "No results found", expects_data=True,
        referenced_metrics=["nonexistent_metric"], available_metrics=["http_requests_total"],
    )
    assert r.status == "empty"
    assert r.error_class == "data_gap"
    assert r.missing_fields == ["nonexistent_metric"]


def test_empty_panel_with_present_metric_is_unexpected_empty():
    r = classify_panel(
        "ts", "N/A", expects_data=True,
        referenced_metrics=["http_requests_total"], available_metrics=["http_requests_total"],
    )
    assert r.error_class == "unexpected_empty"


def test_per_panel_unexpected_empty_warns():
    verdict = classify_render_per_panel(
        [("ts", "No results found"), ("md", "Kitchen-sink")],
        expects_data_titles={"ts"},
    )
    assert verdict.status == "warn"
    assert [p.error_class for p in verdict.panels if p.title == "ts"] == ["unexpected_empty"]


def test_per_panel_benign_empty_does_not_warn():
    verdict = classify_render_per_panel(
        [("md", "No results found")],  # no query -> benign
        expects_data_titles=set(),
    )
    assert verdict.status == "pass"


def test_expects_data_by_panel_excludes_markdown():
    from observability_migration.targets.kibana.render_audit import expects_data_by_panel
    report = {"dashboards": [{"panels": [
        {"title": "ts", "kibana_type": "line", "yaml_panel": {"esql": {"query": "TS metrics-* | STATS x=COUNT()"}}},
        {"title": "md", "kibana_type": "markdown", "yaml_panel": {"esql": {"query": ""}}},
        {"title": "txt", "yaml_panel": {"esql": {"type": "markdown", "query": "# hi"}}},
    ]}]}
    titles = expects_data_by_panel(report)
    assert titles == {"ts"}


# --- Render-verdict regression ratchet (#5) --------------------------------

from observability_migration.targets.kibana.render_audit import (  # noqa: E402
    PanelRenderResult,
    diff_render_snapshots,
    render_snapshot,
)


def test_render_snapshot_serializes_state_with_error_class():
    panels = [
        PanelRenderResult("ts", "rendered"),
        PanelRenderResult("bar", "error", "render_error"),
        PanelRenderResult("gauge", "empty", "unexpected_empty"),
    ]
    snap = render_snapshot(panels)
    assert snap == {"ts": "rendered", "bar": "error:render_error", "gauge": "empty:unexpected_empty"}


def test_diff_no_regression_when_stable_or_improved():
    base = {"ts": "rendered", "bar": "error:render_error"}
    current = {"ts": "rendered", "bar": "rendered"}  # bar improved
    assert diff_render_snapshots(base, current) == []


def test_diff_flags_rendered_to_error_regression():
    base = {"ts": "rendered"}
    current = {"ts": "error:render_error"}
    regressions = diff_render_snapshots(base, current)
    assert regressions == ["ts: rendered -> error:render_error"]


def test_diff_flags_rendered_to_empty_regression():
    regressions = diff_render_snapshots({"ts": "rendered"}, {"ts": "empty:unexpected_empty"})
    assert regressions == ["ts: rendered -> empty:unexpected_empty"]


def test_diff_flags_disappeared_panel():
    regressions = diff_render_snapshots({"ts": "rendered", "x": "rendered"}, {"ts": "rendered"})
    assert regressions == ["x: panel disappeared (was rendered)"]


def test_diff_allows_new_panels():
    assert diff_render_snapshots({"ts": "rendered"}, {"ts": "rendered", "new": "rendered"}) == []


# --- Interaction (controls/filters) audit (#6) -----------------------------

from observability_migration.targets.kibana.render_audit import (  # noqa: E402
    interaction_regression,
)


def test_interaction_regression_attributes_to_control():
    before = {"panel_a": "rendered", "panel_b": "rendered"}
    after = {"panel_a": "rendered", "panel_b": "error:render_error"}
    findings = interaction_regression(before, after, control_label="cluster")
    assert findings == ["control 'cluster': panel_b: rendered -> error:render_error"]


def test_interaction_regression_clean_when_no_change():
    snap = {"panel_a": "rendered"}
    assert interaction_regression(snap, snap, control_label="job") == []


# --- Element-level extraction (legends / chart type / data) -----------------

from observability_migration.targets.kibana.render_audit import (  # noqa: E402
    check_panel_elements,
    extract_panel_elements,
)

# Real a11y chunks captured from a live Kibana 9.5.0 canary render.
_TS_CHUNK = '''button "instance_2; Click: to show, ⌘ + Click: to hide"
button "instance_3; Click: to show, ⌘ + Click: to hide"
button "instance_1; Click: to show, ⌘ + Click: to hide"
StaticText "Chart type" StaticText ":" StaticText "line chart"'''
_PIE_CHUNK = 'StaticText "sunburst chart" row StaticText "instance_2" StaticText "2.327" StaticText "33%"'
_GAUGE_CHUNK = 'graphics-document StaticText "Chart type" StaticText ":" StaticText "Bullet chart"'
_METRIC_CHUNK = 'heading "canary stat" StaticText "6.983"'
_TABLE_CHUNK = 'grid "canary table" columnheader "instance" gridcell "instance_2" gridcell "35,258.0"'
_TABLE_HTML_CHUNK = (
    '<div data-title="canary table"><div data-test-subj="lnsDataTable">'
    '<div class="euiDataGrid" role="grid" aria-label="canary table">'
    '<div role="columnheader" title="time_bucket"></div></div></div></div>'
)
_HEATMAP_CHUNK = 'button "le_1; Click: to show, ⌘ + Click: to hide" StaticText "Chart type" StaticText ":" StaticText "line chart"'
_MARKDOWN_HTML_CHUNK = (
    '<div data-title="canary text"><div class="visualization markdownVis">'
    '<div data-test-subj="markdownBody" class="kbnMarkdown__body">'
    '<h1>Canary</h1></div></div></div>'
)


def test_extract_line_chart_with_legend():
    el = extract_panel_elements("ts", _TS_CHUNK)
    assert el.status == "rendered"
    assert el.chart_kind == "xy"
    assert el.legend_entries == ["instance_2", "instance_3", "instance_1"]
    assert el.has_data is True


def test_extract_pie_sunburst():
    el = extract_panel_elements("pie", _PIE_CHUNK)
    assert el.chart_kind == "partition"
    assert el.has_data is True


def test_extract_gauge_bullet():
    el = extract_panel_elements("gauge", _GAUGE_CHUNK)
    assert el.chart_kind == "gauge"


def test_extract_metric_value():
    el = extract_panel_elements("stat", _METRIC_CHUNK)
    assert el.chart_kind == "metric"
    assert el.has_data is True


def test_extract_table_grid():
    el = extract_panel_elements("table", _TABLE_CHUNK)
    assert el.chart_kind == "datatable"
    assert el.has_data is True


def test_extract_table_grid_from_dump_dom_html():
    el = extract_panel_elements("table", _TABLE_HTML_CHUNK)
    assert el.chart_kind == "datatable"
    assert el.has_data is True


def test_extract_markdown_from_dump_dom_html():
    el = extract_panel_elements("text", _MARKDOWN_HTML_CHUNK)
    assert el.chart_kind == "markdown"
    assert el.has_data is True


def test_extract_error_and_empty_and_loading():
    assert extract_panel_elements("e", "Provided column name or index is invalid: x").status == "error"
    assert extract_panel_elements("m", "No results found").status == "empty"
    assert extract_panel_elements("l", 'progressbar "Loading"').status == "loading"


def test_check_flags_wrong_chart_kind_and_missing_legend():
    el = extract_panel_elements("ts", _TS_CHUNK)
    assert check_panel_elements(el, expected_kind="xy", expects_breakdown=True) == []
    # expected a gauge but rendered xy
    assert check_panel_elements(el, expected_kind="gauge") == ["ts: rendered as xy, expected gauge"]


def test_check_flags_breakdown_panel_without_legend():
    el = extract_panel_elements("bar", 'StaticText "Chart type" StaticText ":" StaticText "bar chart"')
    findings = check_panel_elements(el, expected_kind="xy", expects_breakdown=True)
    assert findings == ["bar: breakdown panel has no legend series"]


# --- Dashboard-level element audit (segmentation + aggregation) (a) ----------

from observability_migration.targets.kibana.render_audit import (  # noqa: E402
    audit_dashboard_elements,
    expected_kind_by_panel,
    segment_panels,
)

# Trimmed real multi-panel snapshot (titles present; gauge title-less to exercise unmatched).
_DASH_SNAP = '''StaticText "canary timeseries"
button "instance_1; Click: to show, x" StaticText "Chart type" StaticText ":" StaticText "line chart"
StaticText "canary table"
grid "canary table" columnheader "instance" gridcell "instance_2"
StaticText "canary heatmap"
button "1.2 - 3.4; Click: to show, x" StaticText "Chart type" StaticText ":" StaticText "Heatmap chart"'''


def test_segment_panels_splits_and_reports_unmatched():
    segs, unmatched = segment_panels(_DASH_SNAP, ["canary timeseries", "canary table", "canary heatmap", "canary gauge"])
    assert [t for t, _ in segs] == ["canary timeseries", "canary table", "canary heatmap"]
    assert unmatched == ["canary gauge"]
    # each chunk contains its own content only
    ts_chunk = dict(segs)["canary timeseries"]
    assert "line chart" in ts_chunk and "Heatmap chart" not in ts_chunk


def test_audit_dashboard_elements_passes_when_kinds_match():
    expected = {"canary timeseries": "xy", "canary table": "datatable", "canary heatmap": "heatmap"}
    verdict = audit_dashboard_elements(_DASH_SNAP, expected_kind_by_title=expected,
                                       breakdown_titles={"canary timeseries", "canary heatmap"})
    assert verdict.status == "pass"
    assert {p.title for p in verdict.panels} == set(expected)


def test_audit_dashboard_elements_flags_wrong_kind_and_unmatched():
    expected = {"canary timeseries": "gauge", "canary gauge": "gauge"}  # ts is really xy; gauge title absent
    verdict = audit_dashboard_elements(_DASH_SNAP, expected_kind_by_title=expected)
    assert verdict.status == "warn"
    joined = " ".join(verdict.reasons)
    assert "rendered as xy, expected gauge" in joined
    assert "canary gauge: panel title did not render" in joined


def test_audit_dashboard_elements_labels_error_as_render_error():
    verdict = audit_dashboard_elements(
        'StaticText "broken" Provided column name or index is invalid',
        expected_kind_by_title={"broken": "xy"},
    )

    assert verdict.status == "warn"
    assert verdict.panels[0].status == "error"
    assert verdict.panels[0].error_class == "render_error"


def test_expected_kind_by_panel_maps_esql_type():
    report = {"dashboards": [{"panels": [
        {"title": "ts", "yaml_panel": {"esql": {"type": "line"}}},
        {"title": "hm", "yaml_panel": {"esql": {"type": "heatmap"}}},
        {"title": "pie", "yaml_panel": {"esql": {"type": "pie"}}},
    ]}]}
    assert expected_kind_by_panel(report) == {"ts": "xy", "hm": "heatmap", "pie": "partition"}


def test_extended_es_error_markers_are_caught():
    # Real community-dashboard render errors must be detected (motivated by the
    # Node Exporter EN live render).
    for marker in [
        "Unexpected error from Elasticsearch: verification_exception",
        "Function [label_replace] is not yet implemented",
        "Output has changed from [[a]] to [[b]]",
    ]:
        assert classify_render(marker).status == "fail", marker


def test_detect_control_warnings_flags_incompatible_selections():
    from observability_migration.targets.kibana.render_audit import detect_control_warnings
    snap = (
        'combobox "Instance" expandable haspopup="listbox" value=".* Incompatible selections (0) 1"\n'
        'combobox "JOB" value=".* Incompatible selections (2) 1"\n'
        'combobox "maxmount" value=".* 1"'  # healthy control, no warning
    )
    warnings = detect_control_warnings(snap)
    assert warnings == [
        "control 'Instance': incompatible selections (0)",
        "control 'JOB': incompatible selections (2)",
    ]


def test_audit_dashboard_elements_includes_control_warnings():
    from observability_migration.targets.kibana.render_audit import audit_dashboard_elements
    snap = (
        'StaticText "p1" StaticText "Chart type" StaticText ":" StaticText "line chart" '
        'button "a; Click: to show, x"\n'
        'combobox "Instance" value=".* Incompatible selections (0) 1"'
    )
    v = audit_dashboard_elements(snap, expected_kind_by_title={"p1": "xy"}, breakdown_titles={"p1"})
    assert v.status == "warn"
    assert any("incompatible selections" in r for r in v.reasons)
