# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest

from scripts.audit_pipeline import (
    DashboardAudit,
    PanelAudit,
    _section_dashboard_summary,
    _section_per_dashboard_traces,
    generate_pipeline_trace_md,
)


class PipelineTraceSummaryTests(unittest.TestCase):
    def _nested_datadog_audit(self) -> DashboardAudit:
        return DashboardAudit(
            source="datadog",
            file_name="nested.json",
            dashboard_title="Nested widgets",
            total_panels=2,
            status_counts={
                "migrated": 1,
                "migrated_with_warnings": 1,
                "requires_manual": 1,
                "not_feasible": 1,
            },
            panels=[
                PanelAudit(status="migrated"),
                PanelAudit(status="migrated_with_warnings"),
                PanelAudit(status="requires_manual"),
                PanelAudit(status="not_feasible"),
            ],
        )

    def test_dashboard_summary_uses_audited_panel_count(self):
        audit = self._nested_datadog_audit()

        summary = _section_dashboard_summary([audit], source="datadog")

        self.assertIn("| datadog | Nested widgets | 4 | 1 | 1 | 1 | 1 | 0 |", summary)
        self.assertIn("**1 dashboards, 4 panels** audited from `infra/datadog/dashboards/`.", summary)

    def test_per_dashboard_traces_use_audited_panel_count(self):
        audit = self._nested_datadog_audit()

        traces = _section_per_dashboard_traces([audit])

        self.assertIn("**File:** `nested.json` — **Panels:** 4", traces)

    def test_standalone_pipeline_trace_uses_audited_panel_count(self):
        audit = self._nested_datadog_audit()

        trace_doc = generate_pipeline_trace_md([audit])

        self.assertIn("| datadog | Nested widgets | 4 | 1 | 1 | 1 | 1 | 0 |", trace_doc)
        self.assertIn("**File:** `nested.json` — **Panels:** 4", trace_doc)


if __name__ == "__main__":
    unittest.main()
