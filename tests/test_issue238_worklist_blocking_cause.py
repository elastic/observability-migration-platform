# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for issue #238.

The ``migration_summary.md`` must-fix worklist described not_feasible /
requires_manual Datadog panels by their **target-mapping label** ("timeseries →
esql XY panel") instead of the **actual blocking cause**, which is already
captured in the panel's ``warnings`` / ``semantic_losses``. That made the human
worklist non-actionable. For attention rows the builder must prefer the
warning/semantic-loss text and only fall back to the mapping label when a panel
carries neither (e.g. planner refusals whose cause lives only in ``reasons``).
"""

import unittest

from observability_migration.adapters.source.datadog.models import (
    DashboardResult,
    TranslationResult,
)
from observability_migration.adapters.source.datadog.report import build_summary_view
from observability_migration.core.reporting.summary_md import render_markdown


def _view_for(panel: TranslationResult):
    dr = DashboardResult(dashboard_id="sqlserver", dashboard_title="SQLServer-Overview")
    dr.panel_results = [panel]
    return build_summary_view([dr], run_id="r1")


class WorklistBlockingCauseTests(unittest.TestCase):
    def test_not_feasible_prefers_blocking_cause_over_mapping_label(self):
        # File Size Growth: the mapping label lives in ``reasons`` while the real
        # causes (forecast + string-literal) live in ``warnings``/``losses``.
        panel = TranslationResult(
            title="File Size Growth",
            kibana_type="xy",
            status="not_feasible",
            reasons=["timeseries → esql XY panel"],
            warnings=[
                "formula function 'forecast' has no ES|QL equivalent — panel may need manual redesign",
                "translation error: string literal 'linear' is not allowed in an expression position",
            ],
            semantic_losses=["string literal 'linear' is not allowed in an expression position"],
        )
        view = _view_for(panel)
        item = next(a for a in view.attention if a.panel == "File Size Growth")
        # The mapping label must NOT be the stated reason.
        self.assertNotIn("timeseries → esql XY panel", item.reasons)
        joined = " ".join(item.reasons)
        self.assertIn("forecast", joined)
        # And the rendered worklist surfaces the real cause, not the label.
        md = render_markdown(view)
        self.assertIn("formula function 'forecast'", md)
        self.assertNotIn("timeseries → esql XY panel", md)

    def test_requires_manual_prefers_blocking_cause(self):
        panel = TranslationResult(
            title="File I/O",
            kibana_type="table",
            status="requires_manual",
            reasons=["table → esql table"],
            warnings=[
                "manual review needed: multi-query widgets with different "
                "request aggregators are not translated safely yet"
            ],
            semantic_losses=[
                "multi-query widgets with different request aggregators are not translated safely yet"
            ],
        )
        view = _view_for(panel)
        item = next(a for a in view.attention if a.panel == "File I/O")
        self.assertNotIn("table → esql table", item.reasons)
        self.assertIn("request aggregators", " ".join(item.reasons))

    def test_falls_back_to_reasons_when_no_warning_or_loss(self):
        # Planner refusals (unsupported widget type) carry their cause only in
        # ``reasons`` — the builder must still surface it.
        panel = TranslationResult(
            title="Geomap",
            kibana_type="markdown",
            status="not_feasible",
            reasons=["unsupported widget type: geomap"],
            warnings=[],
            semantic_losses=[],
        )
        view = _view_for(panel)
        item = next(a for a in view.attention if a.panel == "Geomap")
        self.assertEqual(item.reasons, ["unsupported widget type: geomap"])


if __name__ == "__main__":
    unittest.main()
