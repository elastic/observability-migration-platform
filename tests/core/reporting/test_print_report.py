# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for print_report's panel-vs-row accounting.

The migration tool used to fold Grafana ``type=="row"`` containers into the
``Total panels found`` count and the verification gate's ``Green`` tally, which
made the summary self-inconsistent with the compilation table (panels column
included rows but OK/Warn/Man/NF columns did not). These tests pin the corrected
behaviour: rows are reported separately and never contribute to panel-derived
metrics.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from observability_migration.core.reporting.report import (
    MigrationResult,
    PanelResult,
    print_report,
)


def _panel(title: str, status: str, gate: str = "Green") -> PanelResult:
    return PanelResult(
        title=title,
        grafana_type="graph",
        kibana_type="lens",
        status=status,
        confidence=1.0,
        verification_packet={"semantic_gate": gate},
    )


def _row(title: str) -> PanelResult:
    # Mirrors how translate_dashboard() records a Grafana row container:
    # grafana_type="row", kibana_type="section", status="skipped".
    return PanelResult(
        title=title,
        grafana_type="row",
        kibana_type="section",
        status="skipped",
        confidence=1.0,
        verification_packet={"semantic_gate": "Green"},
    )


def _argocd_fixture() -> MigrationResult:
    """Reproduce the ArgoCD dashboard 14584 shape: 41 panels + 9 rows = 50 items."""
    result = MigrationResult(
        dashboard_title="ArgoCD",
        dashboard_uid="argocd",
    )
    panels: list[PanelResult] = []
    for i in range(38):
        panels.append(_panel(f"panel-{i}", "migrated", gate="Green"))
    for i in range(3):
        panels.append(_panel(f"warn-{i}", "migrated_with_warnings", gate="Yellow"))
    for i in range(9):
        panels.append(_row(f"row-{i}"))
    result.panel_results = panels
    result.total_panels = 50  # includes rows, the data-model invariant we leave alone
    result.migrated = 38
    result.migrated_with_warnings = 3
    result.requires_manual = 0
    result.not_feasible = 0
    result.skipped = 9
    result.compiled = True
    return result


def _capture_report(result: MigrationResult) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_report([result], [(result.dashboard_title, True, "")])
    return buf.getvalue()


class PrintReportRowAccountingTests(unittest.TestCase):
    def test_total_panels_excludes_rows(self):
        # 50 elements - 9 rows = 41 actual panels.
        output = _capture_report(_argocd_fixture())
        self.assertIn("Total panels found:  41", output)
        self.assertNotIn("Total panels found:  50", output)

    def test_migrated_percentage_is_relative_to_panel_count(self):
        # 38/41 = 92.7% — not 38/50 = 76.0%.
        output = _capture_report(_argocd_fixture())
        self.assertIn("Migrated:          38 (92.7%)", output)
        self.assertNotIn("(76.0%)", output)

    def test_skipped_rows_line_is_replaced_with_row_containers_stat(self):
        # The old "Skipped (rows): 9 (18.0%)" line conflates rows with skipped
        # panels and reports a percentage that is meaningless. Replace it with
        # a row-only stat (no percentage of "panels found").
        output = _capture_report(_argocd_fixture())
        self.assertNotIn("Skipped (rows):", output)
        self.assertIn("Row containers:    9 (structural, not migrated)", output)

    def test_verification_gate_excludes_row_semantic_gates(self):
        # Rows trip _semantic_gate()'s default Green branch because their
        # status is "skipped". They must not be counted in the gate roll-up
        # — the gate is a panel-quality signal, not a row presence signal.
        output = _capture_report(_argocd_fixture())
        self.assertIn("Verification gate: 38 Green / 3 Yellow / 0 Red", output)
        self.assertNotIn("47 Green", output)

    def test_compilation_table_panels_column_excludes_rows(self):
        # Per-dashboard "Panels" column must match OK + Warn + Man + NF so
        # the table reads consistently.
        output = _capture_report(_argocd_fixture())
        # The exact row format: title(<40), panels(>6), ok(>5), warn(>5),
        # man(>5), nf(>5), compiled(>10). With panels=41 / ok=38 / warn=3:
        self.assertIn(
            "ArgoCD                                       41    38     3     0     0        YES",
            output,
        )
        self.assertNotIn(
            "ArgoCD                                       50    38     3     0     0        YES",
            output,
        )

    def test_dashboard_with_only_rows_reports_zero_panels(self):
        # Edge case: a dashboard with nothing but row containers reports 0 panels
        # and "Row containers: N". No divide-by-zero in percentages.
        result = MigrationResult(dashboard_title="rows-only", dashboard_uid="ro")
        result.panel_results = [_row(f"r-{i}") for i in range(3)]
        result.total_panels = 3
        result.skipped = 3
        result.compiled = True

        output = _capture_report(result)
        self.assertIn("Total panels found:  0", output)
        self.assertIn("Row containers:    3 (structural, not migrated)", output)
        # No NaN/error and no Verification gate row (since no Green/Yellow/Red panels).
        self.assertNotIn("nan", output.lower())

    def test_dashboard_with_no_rows_omits_row_containers_line(self):
        # Don't add visual noise when there are no rows to report.
        result = MigrationResult(dashboard_title="no-rows", dashboard_uid="nr")
        result.panel_results = [_panel("p", "migrated", gate="Green")]
        result.total_panels = 1
        result.migrated = 1
        result.compiled = True

        output = _capture_report(result)
        self.assertNotIn("Row containers", output)
        self.assertIn("Total panels found:  1", output)


if __name__ == "__main__":
    unittest.main()
