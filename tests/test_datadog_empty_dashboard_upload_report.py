# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A Datadog dashboard with no widgets must not be reported as a failed upload.

Companion to ``test_upload_empty_source_dashboard``, which pins the shared
upload path's verdict. This pins what the Datadog run *says* about it: the
console line, and the ``runtime_summary``/``upload`` blocks a CI gate reads.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout

from observability_migration.adapters.source.datadog.models import DashboardResult
from observability_migration.adapters.source.datadog.report import print_report


def _empty_source_result() -> DashboardResult:
    dashboard = DashboardResult(dashboard_id="pm8-e44-cb4", dashboard_title="Scratch")
    dashboard.total_widgets = 0
    dashboard.upload_attempted = True
    dashboard.uploaded = False
    dashboard.upload_skipped_reason = (
        "the source dashboard has no panels, so there was nothing to upload"
    )
    return dashboard


def _failed_upload_result() -> DashboardResult:
    dashboard = DashboardResult(dashboard_id="bad-000-001", dashboard_title="Broken")
    dashboard.total_widgets = 9
    dashboard.upload_attempted = True
    dashboard.uploaded = False
    dashboard.upload_error = "Broken: empty"
    return dashboard


def _printed(dashboard: DashboardResult) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        print_report([dashboard])
    return buffer.getvalue()


def test_an_empty_source_dashboard_is_reported_as_skipped():
    out = _printed(_empty_source_result())
    assert "Upload: skipped" in out
    assert "UPLOAD ERROR" not in out
    assert "Upload: fail" not in out


def test_the_skip_says_why_nothing_was_uploaded():
    assert "no panels" in _printed(_empty_source_result())


def test_a_real_upload_failure_is_still_reported_as_a_failure():
    out = _printed(_failed_upload_result())
    assert "Upload: fail" in out
    assert "UPLOAD ERROR" in out


def test_runtime_summary_records_a_skip_not_a_failure():
    summary = _empty_source_result().build_runtime_summary()
    assert summary["upload"]["status"] == "skipped"
    assert not summary["upload"]["error"]


def test_runtime_summary_still_fails_a_real_upload_failure():
    summary = _failed_upload_result().build_runtime_summary()
    assert summary["upload"]["status"] == "fail"


def test_an_empty_source_is_not_recorded_as_uploaded():
    """Skipped is not success: nothing reached Kibana."""
    assert _empty_source_result().build_runtime_summary()["upload"]["status"] != "pass"
