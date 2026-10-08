# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""The preflight verdict word must mean the same thing everywhere it is shown.

A dashboard whose preflight raised only warnings used to be announced as
``Preflight: issues`` while the run was in progress and ``Preflight: pass`` in
the summary that followed, from the same ``PreflightResult``. An operator
reading both has no way to tell which one to act on.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout

from observability_migration.adapters.source.datadog.cli import _print_preflight_summary
from observability_migration.adapters.source.datadog.models import DashboardResult
from observability_migration.adapters.source.datadog.preflight import (
    PreflightIssue,
    PreflightResult,
    preflight_status_label,
)
from observability_migration.adapters.source.datadog.report import print_report


def _result(levels: list[str]) -> PreflightResult:
    result = PreflightResult()
    for index, level in enumerate(levels):
        result.add(
            PreflightIssue(
                level=level,
                category="field",
                message=f"field f{index} is absent from the target",
                widget_id=f"w{index}",
                field_name=f"f{index}",
            )
        )
    return result


def _cli_verdict(result: PreflightResult) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        _print_preflight_summary(result)
    line = buffer.getvalue().splitlines()[0]
    return line.split("Preflight:")[1].split()[0]


def _report_verdict(result: PreflightResult) -> str | None:
    """The verdict the end-of-run report prints, or None when it prints none.

    The report only reaches its preflight line when the dashboard carried
    issues, so a clean preflight is silent there by design.
    """
    dashboard = DashboardResult(dashboard_id="d1", dashboard_title="Shop")
    dashboard.preflight_passed = result.passed
    dashboard.preflight_issues = [
        {
            "level": issue.level,
            "category": issue.category,
            "message": issue.message,
            "widget_id": issue.widget_id,
            "field_name": issue.field_name,
        }
        for issue in result.issues
    ]
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        print_report([dashboard])
    lines = [ln for ln in buffer.getvalue().splitlines() if "Preflight:" in ln]
    return lines[0].split("Preflight:")[1].split()[0] if lines else None


def test_warning_only_preflight_reads_the_same_in_cli_and_report():
    """52 warnings and no blocking issue is a pass, and says so in both places."""
    result = _result(["warn"] * 52)
    assert result.passed is True
    assert _cli_verdict(result) == _report_verdict(result) == "pass"


def test_blocking_preflight_reads_as_issues_in_cli_and_report():
    result = _result(["block", "warn"])
    assert result.passed is False
    assert _cli_verdict(result) == _report_verdict(result) == "issues"


def test_clean_preflight_reads_as_pass_and_the_report_stays_silent():
    result = _result([])
    assert _cli_verdict(result) == "pass"
    assert _report_verdict(result) is None


def test_info_only_preflight_is_not_announced_as_a_failure():
    """Info issues carry no verdict weight — they are counted, not failed on."""
    result = _result(["info", "info"])
    assert result.passed is True
    assert _cli_verdict(result) == "pass"


def test_verdict_label_is_derived_from_the_same_predicate_as_the_manifest():
    """``preflight.passed`` is what the manifest records, so it is what the
    printed word must follow."""
    assert preflight_status_label(True) == "pass"
    assert preflight_status_label(False) == "issues"
