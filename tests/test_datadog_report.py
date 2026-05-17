# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for Datadog's print_report and its group-vs-widget accounting.

Datadog group / powerpack widgets are structural containers (analogous to
Grafana row containers): their children are real widgets but the group itself
just lays them out and has no Kibana equivalent. The translator records groups
with ``status == "skipped"`` and ``dd_widget_type`` in {"group", "powerpack"}.
These tests pin the report shape that surfaces structural groups separately
from renderable widgets:

```
Elements:            12 total (10 widgets + 2 groups)
Renderable widgets:  10
  OK: 8  Warning: 1  Manual: 1  NF: 0  Skip: 0  Blocked: 0
Groups:              2 (structural, not migrated)
```
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from observability_migration.adapters.source.datadog.models import (
    DashboardResult,
    TranslationResult,
)
from observability_migration.adapters.source.datadog.report import print_report


def _widget(title: str, dd_type: str, status: str, warnings: list[str] | None = None) -> TranslationResult:
    return TranslationResult(
        title=title,
        dd_widget_type=dd_type,
        kibana_type="lens",
        status=status,
        warnings=list(warnings or []),
    )


def _group(title: str) -> TranslationResult:
    # Mirrors translate.py: plan.backend == "group" → status = "skipped".
    return TranslationResult(
        title=title,
        dd_widget_type="group",
        kibana_type="",
        status="skipped",
    )


def _powerpack(title: str) -> TranslationResult:
    return TranslationResult(
        title=title,
        dd_widget_type="powerpack",
        kibana_type="",
        status="skipped",
    )


def _capture_report(*results: DashboardResult) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_report(list(results))
    return buf.getvalue()


def _make_dashboard(title: str, widgets: list[TranslationResult]) -> DashboardResult:
    dr = DashboardResult(dashboard_title=title, source_file=f"{title}.json")
    dr.panel_results = widgets
    dr.total_widgets = len(widgets)
    dr.recompute_counts()
    return dr


class DatadogPrintReportGroupAccountingTests(unittest.TestCase):
    def test_per_dashboard_block_separates_groups_from_widgets(self):
        # 12 elements = 10 widgets + 2 groups; 8 OK + 1 warn + 1 manual = 10.
        widgets = (
            [_widget(f"w{i}", "timeseries", "ok") for i in range(8)]
            + [_widget("w-warn", "timeseries", "warning", warnings=["ts"])]
            + [_widget("w-manual", "timeseries", "requires_manual")]
            + [_group("Section A"), _group("Section B")]
        )
        dr = _make_dashboard("dd-mixed", widgets)
        output = _capture_report(dr)

        self.assertIn("Elements: 12 total (10 widgets + 2 groups)", output)
        self.assertIn("Renderable widgets: 10", output)
        # OK / Warning / Manual / NF / Skip / Blocked stay panel-only.
        self.assertIn("OK: 8  Warning: 1  Manual: 1  NF: 0", output)
        self.assertIn("Groups: 2 (structural, not migrated)", output)
        # The legacy "Panels: 12" line must not survive.
        self.assertNotIn("Panels:  12", output)
        self.assertNotIn("Panels: 12", output)

    def test_powerpack_widgets_are_counted_as_groups(self):
        # powerpack widgets share the "group" backend and the same structural
        # role — they should fold into the Groups count.
        widgets = [
            _widget("w0", "timeseries", "ok"),
            _group("Section"),
            _powerpack("Pack"),
        ]
        dr = _make_dashboard("dd-pack", widgets)
        output = _capture_report(dr)

        self.assertIn("Elements: 3 total (1 widget + 2 groups)", output)
        self.assertIn("Renderable widgets: 1", output)
        self.assertIn("Groups: 2 (structural, not migrated)", output)

    def test_dashboard_with_no_groups_omits_groups_line(self):
        widgets = [_widget("w0", "timeseries", "ok")]
        dr = _make_dashboard("dd-flat", widgets)
        output = _capture_report(dr)

        self.assertIn("Elements: 1 total (1 widget)", output)
        self.assertIn("Renderable widgets: 1", output)
        self.assertNotIn("Groups:", output)

    def test_totals_aggregate_groups_separately(self):
        # Two dashboards: dd-A has 10 widgets + 2 groups, dd-B has 5 widgets, no groups.
        a = _make_dashboard(
            "dd-A",
            [_widget(f"w{i}", "timeseries", "ok") for i in range(10)]
            + [_group("g1"), _group("g2")],
        )
        b = _make_dashboard(
            "dd-B",
            [_widget(f"w{i}", "timeseries", "ok") for i in range(5)],
        )
        output = _capture_report(a, b)

        # TOTALS section reports across both dashboards.
        self.assertIn("TOTALS: 2 dashboards, 17 elements (15 widgets + 2 groups)", output)
        self.assertIn("OK: 15", output)
        # Success rate is relative to renderable widgets, not elements.
        # 15 / 15 = 100.0%.
        self.assertIn("Success rate: 100.0%", output)
        # Old phrasing must not survive.
        self.assertNotIn("17 widgets", output)


if __name__ == "__main__":
    unittest.main()
