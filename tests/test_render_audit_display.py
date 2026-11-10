# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Render-audit display findings (#483): what Kibana actually drew.

Fixtures are Elastic Charts debug states (``data-ech-debug-state``) modelled
on Grafana 763: the "0 ms" axes of #480 and the "(null)" legends of #481.
"""

from observability_migration.targets.kibana.render_audit import (
    audit_chart_state,
    check_chart_state,
    display_unit_by_panel,
)


def _chart(*, y_labels=None, legend=(), y_title=""):
    chart = {"legend": {"items": [{"name": name} for name in legend]}}
    if y_labels is not None:
        chart["axes"] = {"y": [{"labels": list(y_labels), "title": y_title}]}
    return chart


def _kinds(findings):
    return [finding.kind for finding in findings]


def test_all_identical_ticks_are_unreadable():
    charts = [_chart(y_labels=["0 ms"] * 5, legend=["get"])]
    findings = check_chart_state("Average Time Spent by Command / sec", charts, source_unit="s")
    assert _kinds(findings) == ["unreadable_axis"]
    assert "0 ms" in findings[0].detail


def test_fitted_second_ticks_are_clean():
    charts = [_chart(y_labels=["0.00030 s", "0.00025 s", "0.00020 s"], legend=["get"])]
    assert check_chart_state("Average Time Spent by Command / sec", charts, source_unit="s") == []


def test_single_tick_is_not_unreadable():
    assert check_chart_state("P", [_chart(y_labels=["0 ms"])]) == []


def test_null_legend_entries_are_flagged():
    charts = [_chart(y_labels=["0 B", "500 B"], legend=["(null) - used", "(null) - max"])]
    findings = check_chart_state("Total Memory Usage", charts, source_unit="bytes")
    assert _kinds(findings) == ["null_legend", "null_legend"]


def test_empty_legend_name_is_flagged():
    findings = check_chart_state("P", [_chart(y_labels=["0 B"], legend=["  "])])
    assert _kinds(findings) == ["null_legend"]


def test_real_legend_names_are_clean():
    charts = [_chart(y_labels=["0 B", "500 B"], legend=["redis-a:6379 - used", "redis-a:6379 - max"])]
    assert check_chart_state("Total Memory Usage", charts, source_unit="bytes") == []


def test_blank_breakdown_segment_legend_is_flagged():
    """Real Kibana 9.6 Lens XY/area legend for a null breakdown dimension: the
    empty breakdown value is spliced into the static " - <metric>" suffix,
    giving " - used" / " - max" -- non-empty after strip, no "(null)" substring,
    but still a blank segment once split on " - " (#481, live-reproduced)."""
    charts = [_chart(y_labels=["0 B", "500 B"], legend=[" - used", " - max"])]
    findings = check_chart_state("Total Memory Usage", charts, source_unit="bytes")
    assert _kinds(findings) == ["null_legend", "null_legend"]


def test_unit_mismatch_when_ticks_carry_no_unit():
    charts = [_chart(y_labels=["0", "500", "1000"], legend=["used"])]
    findings = check_chart_state("Total Memory Usage", charts, source_unit="bytes")
    assert _kinds(findings) == ["unit_mismatch"]


def test_unit_mismatch_clean_on_compact_bytes_ticks():
    """Kibana's real bytes NumeralFormat renders compact ticks with no space
    before the suffix ("0.00B", "0.50B") -- confirmed on the live "Network
    I/O" panel's real axis tick. No tick here carries a "KB"/"MB"/... literal
    that would already match the old regex's non-bare alternatives, so this
    isolates the bare-"B" digit-adjacency defect. A correctly-formatted bytes
    panel must not be flagged unit_mismatch just because the digit is adjacent
    to the unit."""
    charts = [_chart(y_labels=["0.00B", "0.50B", "0.99B"], legend=["used"])]
    assert check_chart_state("Network I/O", charts, source_unit="bytes") == []


def test_unit_mismatch_clean_on_compact_bits_ticks():
    charts = [_chart(y_labels=["0.00b", "1.00Kb"], legend=["used"])]
    assert check_chart_state("P", charts, source_unit="bits") == []


def test_unit_mismatch_clean_on_kb_scaled_bits_ticks():
    """Real Kibana bits NumeralFormat ("0,0.00bitd") renders a scaled compact
    tick as digit + scale-letter + bare "b" directly adjacent, no space
    ("1.00Kb") -- the old regex required "Kbit"/"Mbit"/"Gbit" (full word) or a
    bare digit-adjacent "b" with nothing in between, so "Kb" never matched and
    this false-fired unit_mismatch on the curated 1860 pack's "Network
    Traffic Basic"/"Network Traffic" panels (source unit bps)."""
    charts = [_chart(y_labels=["1.00Kb", "2.00Kb"], legend=["used"])]
    assert check_chart_state("P", charts, source_unit="bps") == []


def test_unit_mismatch_clean_on_mb_per_sec_bits_ticks():
    charts = [_chart(y_labels=["20Mb/s", "40Mb/s"], legend=["used"])]
    assert check_chart_state("P", charts, source_unit="bps") == []


def test_unit_mismatch_clean_on_bit_word_ticks():
    """A digit-adjacent "bit" (no scale letter) must also match -- the old
    \\bbits?\\b word-boundary form never matched because a preceding digit
    leaves no boundary before "b" (the same defect the bytes regex was fixed
    for)."""
    charts = [_chart(y_labels=["0.00bit", "200.00bit"], legend=["used"])]
    assert check_chart_state("P", charts, source_unit="bits") == []


def test_unit_mismatch_still_fires_on_plain_numbers_for_bits_source():
    """The fix must not loosen the regex so a bare number without any unit
    letter is mistaken for a carried bits unit."""
    charts = [_chart(y_labels=["0", "500"], legend=["used"])]
    findings = check_chart_state("P", charts, source_unit="bits")
    assert _kinds(findings) == ["unit_mismatch"]


def test_unit_mismatch_percent():
    charts = [_chart(y_labels=["0", "50", "100"], legend=["used"])]
    assert _kinds(check_chart_state("P", charts, source_unit="percentunit")) == ["unit_mismatch"]
    charts = [_chart(y_labels=["0 %", "50 %", "100 %"], legend=["used"])]
    assert check_chart_state("P", charts, source_unit="percentunit") == []


def test_dual_axis_passes_when_any_axis_carries_the_unit():
    chart = {
        "axes": {"y": [
            {"labels": ["0", "10", "20"], "title": "count"},
            {"labels": ["0 B", "1 KB", "2 KB"], "title": "size"},
        ]},
        "legend": {"items": []},
    }
    assert check_chart_state("P", [chart], source_unit="bytes") == []


def test_gauge_without_y_axis_only_checks_the_legend():
    charts = [_chart(legend=["Memory Usage"])]  # no axes key at all
    assert check_chart_state("Memory Usage", charts, source_unit="percent") == []
    charts = [_chart(legend=["(null)"])]
    assert _kinds(check_chart_state("Memory Usage", charts, source_unit="percent")) == ["null_legend"]


def test_unmapped_unit_is_not_checked():
    charts = [_chart(y_labels=["0", "5", "10"], legend=["x"])]
    assert check_chart_state("P", charts, source_unit="furlongs") == []
    assert check_chart_state("P", charts, source_unit="") == []


def test_audit_warns_by_default_and_fails_on_request():
    captured = [{"title": "P", "charts": [_chart(y_labels=["0 ms"] * 4, legend=["x"])]}]
    warn = audit_chart_state(captured, source_unit_by_title={"P": "s"})
    assert warn.status == "warn"
    assert warn.panels[0].findings[0].kind == "unreadable_axis"
    fail = audit_chart_state(captured, source_unit_by_title={"P": "s"}, fail_on_display=True)
    assert fail.status == "fail"


def test_audit_records_skips():
    """A no-chart panel (table/markdown) is recorded skipped but is benign;
    an EXPLICIT skip reason (the driver's "unrendered title" / "capture
    timed out" markers) is a readiness signal and raises the verdict to warn
    -- mirroring how the render verdict raises pass->warn for an unrendered
    title (#483 final-review Fix 2c)."""
    captured = [
        {"title": "Table", "charts": []},
        {"title": "Hidden", "charts": [], "skipped": "panel title did not render"},
    ]
    verdict = audit_chart_state(captured)
    assert verdict.status == "warn"
    assert verdict.panels[0].skipped
    assert verdict.panels[1].skipped == "panel title did not render"


def test_audit_passes_when_every_skip_is_a_benign_no_chart_panel():
    """Tables/markdown panels have no Elastic Charts element at all -- that is
    expected, not a readiness gap, and must NOT by itself warn."""
    captured = [
        {"title": "Table", "charts": []},
        {"title": "Markdown", "charts": []},
    ]
    verdict = audit_chart_state(captured)
    assert verdict.status == "pass"
    assert all(p.skipped for p in verdict.panels)


def test_audit_passes_on_empty_capture_list():
    """Nothing was captured and nothing was expected of this call -- the
    caller (the driver) is responsible for synthesizing skip entries for
    report panels the capture never saw."""
    verdict = audit_chart_state([])
    assert verdict.status == "pass"
    assert verdict.panels == []


def test_audit_warns_on_explicit_skip_even_with_no_findings_elsewhere():
    captured = [
        {"title": "Clean", "charts": [_chart(y_labels=["0 B", "500 B"], legend=["used"])]},
        {"title": "TimedOut", "charts": [], "skipped": "chart-state capture timed out "
                                                         "waiting for panels to finish rendering"},
    ]
    verdict = audit_chart_state(captured, source_unit_by_title={"Clean": "bytes"})
    assert verdict.status == "warn"


def test_audit_notes_when_report_predates_display_unit():
    captured = [{"title": "P", "charts": [_chart(y_labels=["0", "1"], legend=["x"])]}]
    verdict = audit_chart_state(captured, unit_source_available=False)
    assert verdict.status == "pass"
    assert any("display_unit" in note for note in verdict.notes)


def test_display_unit_by_panel_reads_source_units():
    report = {"dashboards": [{"panels": [
        {"title": "A", "display_unit": {"source_unit": "s", "status": "kept"}},
        {"title": "B", "display_unit": {}},
        {"title": "C"},
    ]}]}
    assert display_unit_by_panel(report) == {"A": "s"}
