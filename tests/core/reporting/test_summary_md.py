# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the human-readable Markdown migration summary renderer."""

import unittest

from observability_migration.core.reporting.summary_md import (
    AttentionItem,
    DashboardRow,
    GapSummary,
    GapTask,
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


def _mixed_view() -> SummaryView:
    return SummaryView(
        source="grafana",
        element_noun="panel",
        run_id="run9",
        timestamp=1780000000.0,
        totals=SummaryTotals(
            dashboards=1,
            elements_total=120,
            migrated=5,
            warnings=111,
            manual=2,
            not_feasible=2,
            skipped=0,
            green=5,
            yellow=111,
            red=4,
            compiled_ok=1,
            compiled_total=1,
            uploaded_ok=0,
            upload_attempted=0,
        ),
        dashboards=[
            DashboardRow(
                title="Node | Exporter",  # pipe in title -> must be escaped
                elements=120,
                migrated=5,
                warnings=111,
                manual=2,
                not_feasible=2,
                compiled=True,
                compile_error="",
                risk_score=353,
                rollout_state="report_only",
            )
        ],
        attention=[
            AttentionItem(
                dashboard="Node | Exporter",
                panel="CPU Basic",
                status="not_feasible",
                reasons=["PromQL arithmetic with divergent filters cannot be translated"],
                source_query="sum(irate(node_cpu_seconds_total{mode=`system`}[5m])) / scalar(...)",
            ),
            AttentionItem(
                dashboard="Node | Exporter",
                panel="Memory",
                status="requires_manual",
                reasons=["needs review"],
                source_query="",
            ),
        ],
        warnings=[
            AttentionItem(
                dashboard="Node | Exporter",
                panel=f"p{i}",
                status="warning",
                reasons=["label matcher params need manual review"],
            )
            for i in range(40)
        ]
        + [
            AttentionItem(
                dashboard="Node | Exporter",
                panel=f"t{i}",
                status="warning",
                reasons=["transform redesign needed"],
            )
            for i in range(8)
        ],
        gaps=GapSummary(
            links={},
            annotations={},
            transformations={"total": 1},
            alerts={},
            tasks=[
                GapTask(
                    category="transformation",
                    dashboard="K8s",
                    item="Global CPU",
                    detail="calculateField",
                    kibana_alternative="Use ES|QL EVAL for calculated columns",
                    complexity="medium",
                )
            ],
        ),
    )


class RenderMixedRunTests(unittest.TestCase):
    def test_mixed_run_verdict_and_worklist(self):
        md = render_markdown(_mixed_view())
        self.assertIn("⚠️", md)
        self.assertIn("Review recommended", md)
        # Must-fix shows both items in full with badges
        self.assertIn("## 🔴 Must-fix worklist", md)
        self.assertIn("**✗ CPU Basic**", md)
        self.assertIn("**? Memory**", md)
        # Source query rendered, backticks neutralized inside the code span
        self.assertIn("node_cpu_seconds_total", md)
        self.assertNotIn("mode=`system`", md)

    def test_warnings_grouped_with_counts_in_details(self):
        md = render_markdown(_mixed_view())
        self.assertIn("## ⚠ Warnings", md)
        # Inside the HTML <details>/<summary>, pipes are not table delimiters,
        # so the dashboard name renders verbatim (not escaped).
        self.assertIn("<details><summary>Node | Exporter — 48 warnings</summary>", md)
        self.assertIn("label matcher params need manual review ×40", md)
        self.assertIn("transform redesign needed ×8", md)

    def test_table_escapes_pipe_in_title(self):
        md = render_markdown(_mixed_view())
        self.assertIn("| Node \\| Exporter |", md)

    def test_gaps_section_lists_kibana_alternative(self):
        md = render_markdown(_mixed_view())
        self.assertIn("## 🔌 Non-panel gaps", md)
        self.assertIn("Use ES|QL EVAL for calculated columns", md)
        self.assertIn("_(medium)_", md)


class RenderErrorRunTests(unittest.TestCase):
    def test_compile_failure_yields_blocking_verdict(self):
        view = _clean_view()
        view.totals.compiled_ok = 1  # of 2 -> one failed
        md = render_markdown(view)
        self.assertIn("❌", md)
        self.assertIn("Blocking errors", md)


class RenderNounTests(unittest.TestCase):
    def test_widget_noun_used_for_datadog(self):
        view = _clean_view()
        view.source = "datadog"
        view.element_noun = "widget"
        md = render_markdown(view)
        self.assertIn("Datadog → Kibana", md)
        self.assertIn("Widgets", md)  # scorecard/table header
        self.assertNotIn("Panels", md)


if __name__ == "__main__":
    unittest.main()
