# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for print_report's panel-vs-row accounting.

The migration tool used to fold Grafana ``type=="row"`` containers into the
``Total panels found`` count and the verification gate's ``Green`` tally, which
made the summary self-inconsistent with the compilation table (panels column
included rows but OK/Warn/Man/NF columns did not). These tests pin the
corrected shape:

```
Elements:            50 total (41 panels + 9 rows)
Renderable panels:   41
  Migrated:          38 (92.7%)
  ...
  Skipped:           0 (0.0%)
Verification gate:   38 Green / 3 Yellow / 0 Red

Dashboard  Panels  OK  Warn  Man  NF  Rows  Compiled
ArgoCD         41  38     3    0   0     9       YES
```

Rows are surfaced inline on the ``Elements`` summary line and as a dedicated
``Rows`` column in the per-dashboard table. They never contribute to
panel-derived metrics (percentages, Verification gate counts).
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


def _capture_report(*results: MigrationResult) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_report(
            list(results),
            [(r.dashboard_title, True, "") for r in results],
        )
    return buf.getvalue()


class PrintReportRowAccountingTests(unittest.TestCase):
    def test_elements_summary_line_shows_total_and_split(self):
        # Single summary line surfaces the source-side total AND the split, so
        # the reader doesn't have to add anything mentally to verify 41 + 9 = 50.
        output = _capture_report(_argocd_fixture())
        self.assertIn("Elements:            50 total (41 panels + 9 rows)", output)
        # The legacy phrasing must not survive.
        self.assertNotIn("Total panels found:", output)
        self.assertNotIn("Row containers:", output)
        self.assertNotIn("Skipped (rows):", output)

    def test_renderable_panels_line_breaks_down_the_panel_states(self):
        output = _capture_report(_argocd_fixture())
        self.assertIn("Renderable panels:   41", output)
        # Migrated is shown as a percentage of renderable panels, not of elements.
        self.assertIn("Migrated:          38 (92.7%)", output)
        self.assertIn("With warnings:     3 (7.3%)", output)
        self.assertIn("Requires manual:   0 (0.0%)", output)
        self.assertIn("Not feasible:      0 (0.0%)", output)
        # ``Skipped`` is always present, even at zero — the other four states
        # already print at zero, this brings ``Skipped`` to the same convention.
        self.assertIn("Skipped:           0 (0.0%)", output)
        # Sanity: the panel-mix percentage must not be the legacy element-mix
        # one (38 / 50 = 76.0%).
        self.assertNotIn("(76.0%)", output)

    def test_verification_gate_excludes_row_semantic_gates(self):
        # Rows trip _semantic_gate()'s default Green branch because their
        # status is "skipped". They must not be counted in the gate roll-up
        # — the gate is a panel-quality signal, not a row-presence signal.
        output = _capture_report(_argocd_fixture())
        self.assertIn("Verification gate:   38 Green / 3 Yellow / 0 Red", output)
        self.assertNotIn("47 Green", output)

    def test_compilation_table_has_rows_column(self):
        # Per-dashboard table: Panels = OK + Warn + Man + NF + Skip, and a new
        # ``Rows`` column surfaces structural containers without conflating
        # them with panel counts.
        output = _capture_report(_argocd_fixture())
        # Header includes Rows between NF and Compiled.
        self.assertIn(
            f"{'Dashboard':<40} {'Panels':>6} {'OK':>5} {'Warn':>5} {'Man':>5} {'NF':>5} {'Skip':>5} {'Rows':>5} {'Compiled':>10}",
            output,
        )
        # Data row: Panels=41, Skip=0, Rows=9.
        self.assertIn(
            f"{'ArgoCD':<40} {41:>6} {38:>5} {3:>5} {0:>5} {0:>5} {0:>5} {9:>5} {'YES':>10}",
            output,
        )

    def test_no_rows_uses_panels_only_in_elements_summary(self):
        # Datadog-like / row-less dashboard: only "(N panels)" in the parenthetical,
        # no "+ 0 rows".
        result = MigrationResult(dashboard_title="no-rows", dashboard_uid="nr")
        result.panel_results = [_panel("p", "migrated", gate="Green")]
        result.total_panels = 1
        result.migrated = 1
        result.compiled = True

        output = _capture_report(result)
        self.assertIn("Elements:            1 total (1 panel)", output)
        self.assertNotIn("+ 0 rows", output)
        # ``Rows`` column still present (predictable output) and shows 0.
        self.assertIn(
            f"{'no-rows':<40} {1:>6} {1:>5} {0:>5} {0:>5} {0:>5} {0:>5} {0:>5} {'YES':>10}",
            output,
        )

    def test_rows_only_dashboard_reports_zero_panels(self):
        # Edge case: a dashboard with only row containers reports 0 panels
        # and no divide-by-zero in percentages.
        result = MigrationResult(dashboard_title="rows-only", dashboard_uid="ro")
        result.panel_results = [_row(f"r-{i}") for i in range(3)]
        result.total_panels = 3
        result.skipped = 3
        result.compiled = True

        output = _capture_report(result)
        self.assertIn("Elements:            3 total (0 panels + 3 rows)", output)
        self.assertIn("Renderable panels:   0", output)
        # Migrated at 0% — pct() gracefully handles total=0 already, but assert
        # we don't emit NaN or a Python error.
        self.assertNotIn("nan", output.lower())

    def test_non_row_skips_are_reflected_in_skipped_state(self):
        # When a panel is skipped for a non-row reason (variable expansion
        # warning, L4 repeat cap, etc.), it counts in ``Skipped`` not ``Rows``.
        result = MigrationResult(dashboard_title="mixed-skips", dashboard_uid="ms")
        result.panel_results = (
            [_panel("a", "migrated", gate="Green")]
            + [_panel("var-skip", "skipped", gate="Green")]
            + [_row("R")]
        )
        result.total_panels = 3
        result.migrated = 1
        result.skipped = 2  # one panel-skip + one row, the model lumps these
        result.compiled = True

        output = _capture_report(result)
        # Elements: 1 panel + 1 panel-skip = 2 renderable panels, + 1 row.
        self.assertIn("Elements:            3 total (2 panels + 1 row)", output)
        self.assertIn("Renderable panels:   2", output)
        self.assertIn("Migrated:          1 (50.0%)", output)
        self.assertIn("Skipped:           1 (50.0%)", output)
        # Table: panels=2, skip=1, rows=1.
        self.assertIn(
            f"{'mixed-skips':<40} {2:>6} {1:>5} {0:>5} {0:>5} {0:>5} {1:>5} {1:>5} {'YES':>10}",
            output,
        )


if __name__ == "__main__":
    unittest.main()
