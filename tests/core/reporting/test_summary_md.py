# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the human-readable Markdown migration summary renderer."""

import unittest

from observability_migration.core.reporting.summary_md import (
    DashboardRow,
    GapSummary,
    SummaryTotals,
    SummaryView,
    render_markdown,
)


def _clean_view() -> SummaryView:
    return SummaryView(
        source="grafana",
        element_noun="panel",
        run_id="abc123",
        timestamp=1780000000.0,
        totals=SummaryTotals(
            dashboards=2,
            elements_total=10,
            migrated=10,
            warnings=0,
            manual=0,
            not_feasible=0,
            skipped=0,
            green=10,
            yellow=0,
            red=0,
            compiled_ok=2,
            compiled_total=2,
            uploaded_ok=0,
            upload_attempted=0,
        ),
        dashboards=[
            DashboardRow(
                title="Alpha",
                elements=6,
                migrated=6,
                warnings=0,
                manual=0,
                not_feasible=0,
                compiled=True,
                compile_error="",
                risk_score=0,
                rollout_state="report_only",
            ),
            DashboardRow(
                title="Beta",
                elements=4,
                migrated=4,
                warnings=0,
                manual=0,
                not_feasible=0,
                compiled=True,
                compile_error="",
                risk_score=0,
                rollout_state="report_only",
            ),
        ],
        attention=[],
        warnings=[],
        gaps=GapSummary(links={}, annotations={}, transformations={}, alerts={}, tasks=[]),
    )


class RenderCleanRunTests(unittest.TestCase):
    def test_clean_run_has_ok_verdict_and_no_worklist_sections(self):
        md = render_markdown(_clean_view())
        # Title + verdict
        self.assertIn("# Migration Summary — Grafana → Kibana", md)
        self.assertIn("✅", md)
        self.assertIn("2/2 compiled", md)  # per-dashboard compiled count in header
        # Scorecard present
        self.assertIn("Migrated", md)
        self.assertIn("10", md)
        # Per-dashboard table present
        self.assertIn("| Alpha |", md)
        self.assertIn("| Beta |", md)
        # No worklist sections on a clean run
        self.assertNotIn("Must-fix", md)
        self.assertNotIn("Warnings", md)
        self.assertNotIn("Non-panel gaps", md)


if __name__ == "__main__":
    unittest.main()
