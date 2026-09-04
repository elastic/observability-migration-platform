# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for print_report's panel-vs-row accounting.

The migration tool used to fold Grafana ``type=="row"`` containers into the
``Total panels found`` count and the verification gate's ``Green`` tally, which
made the summary self-inconsistent with the per-dashboard table (panels column
included rows but OK/Warn/Man/NF columns did not). These tests pin the
corrected shape:

```
Elements:            50 total (41 panels + 9 rows)
Renderable panels:   41
  Migrated:          38 (92.7%)
  ...
  Skipped:           0 (0.0%)
Verification gate:   38 Green / 3 Yellow / 0 Red

Dashboard  Panels  OK  Warn  Man  NF  Skip  Rows
ArgoCD         41  38     3    0   0     0     9
```

Rows are surfaced inline on the ``Elements`` summary line and as a dedicated
``Rows`` column in the per-dashboard table. They never contribute to
panel-derived metrics (percentages, Verification gate counts).
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from observability_migration.core.reporting.report import (
    MigrationResult,
    PanelResult,
    build_runtime_summary,
    print_report,
    save_detailed_report,
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
    return result


def _capture_report(*results: MigrationResult) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_report(list(results))
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

    def test_per_dashboard_table_has_rows_column(self):
        # Per-dashboard table: Panels = OK + Warn + Man + NF + Skip, and a
        # ``Rows`` column surfaces structural containers without conflating
        # them with panel counts.
        output = _capture_report(_argocd_fixture())
        # Header ends with Skip then Rows.
        self.assertIn(
            f"{'Dashboard':<40} {'Panels':>6} {'OK':>5} {'Warn':>5} {'Man':>5} {'NF':>5} {'Skip':>5} {'Rows':>5}",
            output,
        )
        # Data row: Panels=41, Skip=0, Rows=9.
        self.assertIn(
            f"{'ArgoCD':<40} {41:>6} {38:>5} {3:>5} {0:>5} {0:>5} {0:>5} {9:>5}",
            output,
        )

    def test_no_rows_uses_panels_only_in_elements_summary(self):
        # Datadog-like / row-less dashboard: only "(N panels)" in the parenthetical,
        # no "+ 0 rows".
        result = MigrationResult(dashboard_title="no-rows", dashboard_uid="nr")
        result.panel_results = [_panel("p", "migrated", gate="Green")]
        result.total_panels = 1
        result.migrated = 1

        output = _capture_report(result)
        self.assertIn("Elements:            1 total (1 panel)", output)
        self.assertNotIn("+ 0 rows", output)
        # ``Rows`` column still present (predictable output) and shows 0.
        self.assertIn(
            f"{'no-rows':<40} {1:>6} {1:>5} {0:>5} {0:>5} {0:>5} {0:>5} {0:>5}",
            output,
        )

    def test_rows_only_dashboard_reports_zero_panels(self):
        # Edge case: a dashboard with only row containers reports 0 panels
        # and no divide-by-zero in percentages.
        result = MigrationResult(dashboard_title="rows-only", dashboard_uid="ro")
        result.panel_results = [_row(f"r-{i}") for i in range(3)]
        result.total_panels = 3
        result.skipped = 3

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

        output = _capture_report(result)
        # Elements: 1 panel + 1 panel-skip = 2 renderable panels, + 1 row.
        self.assertIn("Elements:            3 total (2 panels + 1 row)", output)
        self.assertIn("Renderable panels:   2", output)
        self.assertIn("Migrated:          1 (50.0%)", output)
        self.assertIn("Skipped:           1 (50.0%)", output)
        # Table: panels=2, skip=1, rows=1.
        self.assertIn(
            f"{'mixed-skips':<40} {2:>6} {1:>5} {0:>5} {0:>5} {0:>5} {1:>5} {1:>5}",
            output,
        )


class PrintReportFieldDiscoveryWarningTests(unittest.TestCase):
    """Issue #256: when target schema discovery is offline/empty/error/
    unrecognized, label and index resolution falls back to OTel defaults and
    panels can render empty. The final report must surface an unmistakable,
    top-of-summary warning with the remediation — and stay silent on a normal,
    verified run so it doesn't cry wolf."""

    def _run(self, field_discovery):
        result = MigrationResult(dashboard_title="d", dashboard_uid="d")
        result.panel_results = [_panel("p", "migrated", gate="Green")]
        result.total_panels = 1
        result.migrated = 1
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_report([result], field_discovery=field_discovery)
        return buf.getvalue()

    def test_offline_fallback_emits_warning_and_remediation(self):
        out = self._run(
            {"otel_fallback": True, "status": "offline", "index_pattern": "metrics-*"}
        )
        self.assertIn("WARNING: migrated panels may render empty", out)
        self.assertIn("did not run (no --es-url provided)", out)
        # Remediation names both flags the user must point at their data.
        self.assertIn("--es-url", out)
        self.assertIn("--esql-index", out)
        # OTel default example so the cause is concrete.
        self.assertIn("service.name", out)
        # The warning lands before the dashboard counts (top-of-summary).
        self.assertLess(
            out.index("may render empty"), out.index("Dashboards processed")
        )

    def test_offline_named_prometheus_profile_describes_layout_not_otel(self):
        # A named Prometheus profile offline emits that layout's fixed
        # namespaced spellings deterministically (labels.* / metrics.*); it does
        # NOT fall through to OTel service.name. The warning must describe the
        # real emitted layout, not misinform the operator about an OTel fallback.
        out = self._run(
            {
                "otel_fallback": True,
                "status": "offline",
                "field_profile": "prometheus_native",
                "index_pattern": "metrics-k8s.prometheus-default",
            }
        )
        self.assertIn("WARNING: migrated panels may render empty", out)
        self.assertIn("prometheus_native", out)
        self.assertIn("labels.", out)
        self.assertIn("--es-url", out)
        # Must not claim an OTel fallback that never happened offline.
        self.assertNotIn("service.name", out)
        self.assertNotIn("OTel field defaults", out)

    def test_offline_remote_write_profile_names_prometheus_layout(self):
        out = self._run(
            {
                "otel_fallback": True,
                "status": "offline",
                "field_profile": "prometheus_remote_write",
                "index_pattern": "metrics-*",
            }
        )
        self.assertIn("prometheus_remote_write", out)
        self.assertIn("prometheus.labels.", out)
        self.assertNotIn("service.name", out)

    def test_offline_otel_profile_still_names_otel_default(self):
        # otel offline legitimately emits OTel guesses — keep the concrete
        # service.name example for that profile.
        out = self._run(
            {
                "otel_fallback": True,
                "status": "offline",
                "field_profile": "otel",
                "index_pattern": "metrics-*",
            }
        )
        self.assertIn("service.name", out)

    def test_empty_fallback_names_index(self):
        out = self._run(
            {"otel_fallback": True, "status": "empty", "index_pattern": "metrics-prod-*"}
        )
        self.assertIn("found no fields under 'metrics-prod-*'", out)

    def test_error_fallback_includes_cause(self):
        out = self._run(
            {
                "otel_fallback": True,
                "status": "error",
                "index_pattern": "metrics-*",
                "error": "HTTP 401",
            }
        )
        self.assertIn("target schema discovery failed: HTTP 401", out)

    def test_known_profile_missing_field_warns_without_unrecognized_language(self):
        # PR #262: a recognized profile that is missing some dashboard fields
        # must warn, but must not claim the schema was "not recognized".
        out = self._run(
            {
                "otel_fallback": True,
                "status": "ok",
                "schema_profile": "prometheus_remote_write",
                "index_pattern": "metrics-*",
            }
        )
        self.assertIn("may render empty", out)
        self.assertIn("prometheus_remote_write", out)
        self.assertIn("missing some fields", out)
        self.assertNotIn("was not recognized", out)

    def test_unrecognized_schema_fallback_warns(self):
        out = self._run(
            {
                "otel_fallback": True,
                "status": "ok",
                "schema_profile": None,
                "index_pattern": "metrics-*",
            }
        )
        self.assertIn("was not recognized", out)
        self.assertIn("may render empty", out)

    def test_no_warning_when_resolution_verified(self):
        out = self._run(
            {
                "otel_fallback": False,
                "status": "ok",
                "schema_profile": "prometheus_remote_write",
                "index_pattern": "metrics-*",
            }
        )
        self.assertNotIn("may render empty", out)

    def test_no_warning_when_field_discovery_absent(self):
        # Back-compat: callers that don't pass field_discovery print no warning.
        out = self._run(None)
        self.assertNotIn("may render empty", out)


class UploadPanelLossReportingTests(unittest.TestCase):
    """Panels Kibana dropped behind an HTTP 200 must reach the operator report.

    A count of failed uploads is not enough: nothing in it says *which* panels
    the "successful" dashboards are missing.
    """

    @staticmethod
    def _uploaded_result(dropped: list[dict] | None) -> MigrationResult:
        result = _argocd_fixture()
        result.upload_attempted = True
        result.uploaded = not dropped
        result.upload_error = "Dash: lossy" if dropped else ""
        result.upload_dropped_panels = list(dropped or [])
        return result

    def test_dropped_panels_are_named_in_the_migration_report(self):
        output = _capture_report(
            self._uploaded_result(
                [
                    {
                        "title": "Memory usage",
                        "reason": "Unable to transform panel config. Error: [color]",
                        "section": "Overview",
                        "grid": {"x": 12, "y": 0, "w": 12, "h": 6},
                    }
                ]
            )
        )
        self.assertIn("UPLOAD DATA LOSS", output)
        self.assertIn("ArgoCD: Memory usage [section Overview]", output)
        self.assertIn("Unable to transform panel config", output)
        self.assertIn("1 panel(s) are missing from the uploaded dashboard(s)", output)
        # A lossy upload never counts as uploaded.
        self.assertIn("Upload results:      0/1", output)

    def test_clean_upload_prints_no_data_loss_section(self):
        output = _capture_report(self._uploaded_result(None))
        self.assertNotIn("UPLOAD DATA LOSS", output)
        self.assertIn("Upload results:      1/1", output)

    def test_runtime_summary_carries_the_dropped_panels(self):
        dropped = [{"title": "Memory usage", "reason": "boom", "section": "", "grid": {}}]
        summary = build_runtime_summary(self._uploaded_result(dropped))
        self.assertEqual(summary["upload"]["status"], "fail")
        self.assertEqual(summary["upload"]["dropped_panels"], dropped)

    def test_runtime_summary_dropped_panels_is_empty_on_a_clean_upload(self):
        summary = build_runtime_summary(self._uploaded_result(None))
        self.assertEqual(summary["upload"]["status"], "pass")
        self.assertEqual(summary["upload"]["dropped_panels"], [])


class CompileReportingRemovedTests(unittest.TestCase):
    """Drift guard: the dashboard-YAML compile path no longer exists, so no
    reporting surface may claim compile / YAML-lint / compiled-layout status."""

    def test_console_report_has_no_compilation_surface(self):
        output = _capture_report(_argocd_fixture())
        self.assertNotIn("Compilation results:", output)
        self.assertNotIn("COMPILATION ERRORS:", output)
        self.assertNotIn("Compiled", output)

    def test_detailed_report_and_runtime_summary_drop_compile_stages(self):
        result = _argocd_fixture()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "migration_report.json"
            save_detailed_report([result], output_path)
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        for key in ("compiled_ok", "yaml_lint_ok", "layout_ok"):
            self.assertNotIn(key, payload["summary"])
        for key in ("compiled", "compile_error", "compiled_path", "yaml_path"):
            self.assertNotIn(key, payload["dashboards"][0])
        # Upload is the only remaining runtime stage.
        self.assertEqual(list(build_runtime_summary(result)), ["upload"])


if __name__ == "__main__":
    unittest.main()
