# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the translation-provenance breakdown in the migration summary.

The summary must reveal, per dashboard and in total, how many panels are
native-PROMQL (numerically oracle-verifiable), how many are ES|QL-translated
(structural-only unless separately validated), and how many are placeholders
(``not_feasible``). Rendered identically for Grafana and Datadog.
"""

import unittest

from observability_migration.core.reporting.summary_md import (
    DashboardRow,
    GapSummary,
    PanelProvenance,
    SummaryTotals,
    SummaryView,
    classify_panel_provenance,
    render_markdown,
)


def _totals(**overrides) -> SummaryTotals:
    base = dict(
        dashboards=1,
        elements_total=0,
        migrated=0,
        warnings=0,
        manual=0,
        not_feasible=0,
        skipped=0,
        green=0,
        yellow=0,
        red=0,
        compiled_ok=1,
        compiled_total=1,
        uploaded_ok=0,
        upload_attempted=0,
    )
    base.update(overrides)
    return SummaryTotals(**base)


class ClassifyProvenanceTests(unittest.TestCase):
    """The shared classifier maps a panel-shaped object to a provenance bucket."""

    def test_native_via_query_prefix(self):
        self.assertEqual(
            classify_panel_provenance(
                status="migrated",
                query="PROMQL index=metrics-* step=1m value=(rate(http_requests_total[5m]))",
                query_ir={},
            ),
            "native",
        )

    def test_native_via_structured_family_marker(self):
        # Prefer the structured marker even if the query text is absent.
        self.assertEqual(
            classify_panel_provenance(
                status="migrated",
                query="",
                query_ir={"family": "native_promql"},
            ),
            "native",
        )

    def test_esql_for_from_pipeline(self):
        self.assertEqual(
            classify_panel_provenance(
                status="migrated",
                query="FROM metrics-* | STATS avg(value) BY host",
                query_ir={"family": "range_agg"},
            ),
            "esql",
        )

    def test_placeholder_for_not_feasible(self):
        # not_feasible wins even if a stale query string is present.
        self.assertEqual(
            classify_panel_provenance(
                status="not_feasible",
                query="PROMQL index=metrics-* value=(x)",
                query_ir={"family": "native_promql"},
            ),
            "placeholder",
        )


class RenderProvenanceTests(unittest.TestCase):
    def _mixed_grafana_view(self) -> SummaryView:
        return SummaryView(
            source="grafana",
            element_noun="panel",
            run_id="r1",
            timestamp=1780000000.0,
            totals=_totals(
                dashboards=2,
                elements_total=10,
                migrated=8,
                not_feasible=1,
                native_promql=5,
                esql_translated=4,
                placeholder=1,
            ),
            dashboards=[
                DashboardRow(
                    title="Alpha",
                    elements=6,
                    migrated=5,
                    warnings=0,
                    manual=0,
                    not_feasible=1,
                    compiled=True,
                    compile_error="",
                    risk_score=None,
                    rollout_state="",
                    native_promql=3,
                    esql_translated=2,
                    placeholder=1,
                ),
                DashboardRow(
                    title="Beta",
                    elements=4,
                    migrated=3,
                    warnings=0,
                    manual=0,
                    not_feasible=0,
                    compiled=True,
                    compile_error="",
                    risk_score=None,
                    rollout_state="",
                    native_promql=2,
                    esql_translated=2,
                    placeholder=0,
                ),
            ],
            attention=[],
            warnings=[],
            gaps=GapSummary(),
        )

    def test_grafana_provenance_section_and_totals(self):
        md = render_markdown(self._mixed_grafana_view())
        # Dedicated section header
        self.assertIn("## Translation provenance", md)
        # Top-level totals line carries each bucket count
        self.assertIn("5", md)  # native total
        self.assertIn("4", md)  # esql total
        # Verifiability note distinguishes native (oracle) vs ES|QL (structural)
        self.assertIn("oracle", md.lower())
        self.assertIn("structural", md.lower())

    def test_grafana_per_dashboard_provenance_counts(self):
        md = render_markdown(self._mixed_grafana_view())
        # Per-dashboard breakdown: Alpha 3 native / 2 esql / 1 placeholder
        self.assertRegex(md, r"Alpha[^\n]*\b3\b[^\n]*\b2\b[^\n]*\b1\b")
        # Beta 2 native / 2 esql / 0 placeholder
        self.assertRegex(md, r"Beta[^\n]*\b2\b[^\n]*\b2\b[^\n]*\b0\b")

    def test_datadog_run_shows_zero_native(self):
        view = SummaryView(
            source="datadog",
            element_noun="widget",
            run_id="dd1",
            timestamp=1780000000.0,
            totals=_totals(
                dashboards=1,
                elements_total=7,
                migrated=6,
                not_feasible=1,
                native_promql=0,
                esql_translated=6,
                placeholder=1,
            ),
            dashboards=[
                DashboardRow(
                    title="DD Host",
                    elements=7,
                    migrated=6,
                    warnings=0,
                    manual=0,
                    not_feasible=1,
                    compiled=True,
                    compile_error="",
                    risk_score=None,
                    rollout_state="",
                    native_promql=0,
                    esql_translated=6,
                    placeholder=1,
                )
            ],
            attention=[],
            warnings=[],
            gaps=GapSummary(),
        )
        md = render_markdown(view)
        self.assertIn("## Translation provenance", md)
        # All ES|QL: explicitly says 0 native and notes no native panels exist
        self.assertRegex(md, r"[Nn]ative[^\n]*\b0\b")
        self.assertIn("0 native", md)


class GrafanaBuilderProvenanceTests(unittest.TestCase):
    """build_summary_view must derive provenance from PanelResult data."""

    def _result(self):
        from observability_migration.core.reporting.report import (
            MigrationResult,
            PanelResult,
        )

        r = MigrationResult("Alpha", "alpha-uid")
        r.source_file = "alpha.json"
        r.compiled = True

        native = PanelResult("CPU", "timeseries", "xy", "migrated", 1.0)
        native.esql_query = "PROMQL index=metrics-* step=1m value=(rate(cpu[5m]))"
        native.query_ir = {"family": "native_promql"}

        esql = PanelResult("Mem", "timeseries", "xy", "migrated_with_warnings", 0.5)
        esql.esql_query = "FROM metrics-* | STATS avg(mem) BY host"
        esql.query_ir = {"family": "range_agg"}

        nf = PanelResult("Ratio", "timeseries", "xy", "not_feasible", 0.0)
        nf.reasons = ["divergent groupings"]
        nf.promql_expr = "sum(a)/sum(b)"

        row = PanelResult("Section", "row", "", "skipped", 0.0)  # excluded

        r.panel_results = [native, esql, nf, row]
        r.total_panels = 4
        r.migrated = 1
        r.migrated_with_warnings = 1
        r.not_feasible = 1
        r.skipped = 1
        return r

    def test_grafana_view_carries_provenance_counts(self):
        from observability_migration.core.reporting.report import build_summary_view

        view = build_summary_view(
            [self._result()],
            [("alpha.yaml", True, "")],
            run_id="r1",
        )
        self.assertEqual(view.totals.native_promql, 1)
        self.assertEqual(view.totals.esql_translated, 1)
        self.assertEqual(view.totals.placeholder, 1)
        d = view.dashboards[0]
        self.assertEqual(d.native_promql, 1)
        self.assertEqual(d.esql_translated, 1)
        self.assertEqual(d.placeholder, 1)
        # Rendered markdown surfaces the breakdown for a real-shaped view
        md = render_markdown(view)
        self.assertIn("## Translation provenance", md)


class DatadogBuilderProvenanceTests(unittest.TestCase):
    """The Datadog builder must show 0 native and the ES|QL/placeholder split."""

    def _dashboard_result(self):
        from observability_migration.adapters.source.datadog.models import (
            DashboardResult,
            TranslationResult,
        )

        ok = TranslationResult(
            title="Requests",
            kibana_type="xy",
            status="ok",
            esql_query="FROM metrics-* | STATS sum(value) BY service",
            query_ir={"family": "range_agg"},
        )
        warn = TranslationResult(
            title="Latency",
            kibana_type="xy",
            status="ok",
            warnings=["approximate"],
            esql_query="FROM metrics-* | STATS avg(value)",
            query_ir={},
        )
        nf = TranslationResult(
            title="Forecast",
            kibana_type="markdown",
            status="not_feasible",
            reasons=["forecast not supported"],
        )
        grp = TranslationResult(title="Group", kibana_type="group", status="ok")

        dr = DashboardResult(dashboard_id="dd1", dashboard_title="DD Host")
        dr.panel_results = [ok, warn, nf, grp]
        return dr

    def test_datadog_view_zero_native(self):
        from observability_migration.adapters.source.datadog.report import (
            build_summary_view,
        )

        view = build_summary_view([self._dashboard_result()], run_id="dd1")
        self.assertEqual(view.totals.native_promql, 0)
        self.assertEqual(view.totals.esql_translated, 2)
        self.assertEqual(view.totals.placeholder, 1)
        md = render_markdown(view)
        self.assertIn("0 native", md)


class PanelProvenanceExportTests(unittest.TestCase):
    def test_provenance_constants_exported(self):
        # The bucket labels are a small enum-like helper so callers stay aligned.
        self.assertEqual(PanelProvenance.NATIVE, "native")
        self.assertEqual(PanelProvenance.ESQL, "esql")
        self.assertEqual(PanelProvenance.PLACEHOLDER, "placeholder")


if __name__ == "__main__":
    unittest.main()
