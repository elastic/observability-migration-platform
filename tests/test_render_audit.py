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
                {"title": "stat", "yaml_panel": {"esql": {"metrics": [{"field": "value"}]}}},
            ],
        }],
    }
    out = breakdown_fields_by_panel(report)
    assert out["ts"] == ["method"]          # value/step excluded
    assert out["tbl"] == ["instance", "mode"]
    assert "stat" not in out                 # no breakdown -> not listed
