# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import audit_pipeline
from scripts.audit_pipeline import (
    DashboardAudit,
    PanelAudit,
    _section_dashboard_summary,
    _section_per_dashboard_traces,
    _verdict,
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

    def test_omitted_or_unbound_template_filter_is_not_classified_correct(self):
        for warning in (
            "Scope filter with template variable could not be bound exactly",
            "Datadog $scope template variable was omitted",
        ):
            with self.subTest(warning=warning):
                panel = PanelAudit(
                    status="warning",
                    translated_query="FROM metrics-* | STATS value = AVG(cpu)",
                    warnings=[warning],
                )
                self.assertEqual(_verdict(panel), "MINOR_ISSUE")

    def test_partial_source_update_does_not_overwrite_shared_trace(self):
        audit = self._nested_datadog_audit()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "pipeline-trace.md"
            output_path.write_text("combined cross-source trace", encoding="utf-8")
            missing_datadog_template = Path(tmpdir) / "missing-datadog-template.md"
            with (
                mock.patch.object(audit_pipeline, "_run_audit", return_value=[audit]),
                mock.patch.object(audit_pipeline, "DOCS_OUTPUT_PATH", output_path),
                mock.patch.object(
                    audit_pipeline,
                    "DATADOG_TEMPLATE_PATH",
                    missing_datadog_template,
                ),
                mock.patch.object(
                    sys,
                    "argv",
                    ["audit_pipeline.py", "--source", "datadog", "--update-docs"],
                ),
            ):
                audit_pipeline.main()

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "combined cross-source trace",
            )


class GrafanaAuditControlsTests(unittest.TestCase):
    """Controls are read from ``result.dashboard_ir``, not from emitted YAML.

    The audit used to re-parse the dashboard YAML the translator had just
    written and reported ``controls = []`` whenever that read failed, so a
    dashboard with template variables looked like a dashboard with none.
    """

    def _dashboard_with_a_template_variable(self) -> dict:
        return {
            "title": "Controls Audit",
            "uid": "controls-audit-1",
            "schemaVersion": 30,
            "templating": {
                "list": [
                    {
                        "name": "instance",
                        "type": "query",
                        "datasource": {"type": "prometheus"},
                        "query": "label_values(up, instance)",
                        "multi": True,
                        "current": {"text": "All", "value": "$__all"},
                    }
                ]
            },
            "panels": [
                {
                    "title": "Up",
                    "type": "stat",
                    "gridPos": {"w": 12, "h": 8, "x": 0, "y": 0},
                    "targets": [{"refId": "A", "expr": "up", "instant": True}],
                }
            ],
        }

    def test_grafana_audit_reports_declared_controls(self):
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "dash.json"
            source.write_text(
                json.dumps(self._dashboard_with_a_template_variable()),
                encoding="utf-8",
            )

            audit = audit_pipeline._audit_grafana_dashboard(source, "metrics-*")

        self.assertTrue(
            audit.controls,
            "expected the audit to report the dashboard's template variable",
        )
        names = {
            str(control.get("variable_name") or control.get("field_name") or "")
            for control in audit.controls
        }
        self.assertIn("instance", names)


if __name__ == "__main__":
    unittest.main()
