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
    _section_panel_type_summary,
    _section_per_dashboard_traces,
    _verdict,
    _to_json,
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

    def test_panel_type_summary_tracks_dashboards_api_family(self):
        audit = DashboardAudit(
            source="grafana",
            file_name="types.json",
            dashboard_title="Types",
            total_panels=3,
            panels=[
                PanelAudit(
                    source_type="grafana",
                    source_panel_type="timeseries",
                    kibana_type="line",
                    dashboards_api_type="vis:xy",
                    status="migrated",
                ),
                PanelAudit(
                    source_type="grafana",
                    source_panel_type="timeseries",
                    kibana_type="line",
                    dashboards_api_type="vis:xy",
                    status="migrated_with_warnings",
                ),
                PanelAudit(
                    source_type="grafana",
                    source_panel_type="text",
                    kibana_type="markdown",
                    dashboards_api_type="markdown",
                    status="skipped",
                ),
            ],
        )

        summary = _section_panel_type_summary([audit])

        self.assertIn(
            "| grafana | `timeseries` | `line` | `vis:xy` | 2 | 1 | 1 | 0 | 0 | 0 | 0 |",
            summary,
        )
        self.assertIn(
            "| grafana | `text` | `markdown` | `markdown` | 1 | 0 | 0 | 0 | 0 | 1 | 0 |",
            summary,
        )

    def test_json_audit_emits_dashboards_api_type(self):
        audit = DashboardAudit(
            source="datadog",
            file_name="types.json",
            dashboard_title="Types",
            panels=[
                PanelAudit(
                    source_type="datadog",
                    source_panel_type="query_value",
                    kibana_type="metric",
                    dashboards_api_type="vis:metric",
                    status="ok",
                )
            ],
        )

        payload = _to_json([audit])

        self.assertIn('"source_type": "datadog"', payload)
        self.assertIn('"dashboards_api_type": "vis:metric"', payload)

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


class DatadogAuditControlsTests(unittest.TestCase):
    """The Datadog branch reports controls from the same IR Grafana reads.

    It used to call ``generate_dashboard_yaml``, which discards the
    ``DashboardIR`` it builds internally, so every Datadog dashboard was
    audited as ``controls = 0`` — including dashboards whose template
    variables the translator had turned into real Kibana controls.
    """

    def _dashboard_with_template_variables(self) -> dict:
        return {
            "id": "controls-audit-dd-1",
            "title": "Datadog Controls Audit",
            "layout_type": "ordered",
            "template_variables": [
                {
                    "name": "host",
                    "tag": "host",
                    "prefix": "host",
                    "default": "*",
                    "defaults": ["*"],
                    "available_values": ["web-1", "web-2"],
                },
                # No tag/prefix and unresolvable by name: dropped on purpose,
                # so the count is "controls the translator could build", not
                # "template variables declared".
                {"name": "scope", "default": "*"},
            ],
            "widgets": [
                {
                    "id": 1,
                    "definition": {
                        "type": "timeseries",
                        "title": "CPU",
                        "requests": [
                            {
                                "queries": [
                                    {
                                        "data_source": "metrics",
                                        "name": "query1",
                                        "query": "avg:system.cpu.user{$host} by {host}",
                                    }
                                ],
                                "formulas": [{"formula": "query1"}],
                                "response_format": "timeseries",
                            }
                        ],
                    },
                    "layout": {"x": 0, "y": 0, "w": 4, "h": 2},
                }
            ],
        }

    def test_datadog_audit_reports_translated_controls(self):
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "dash.json"
            source.write_text(
                json.dumps(self._dashboard_with_template_variables()),
                encoding="utf-8",
            )

            audit = audit_pipeline._audit_datadog_dashboard(source, "metrics-otel-default")

        self.assertEqual(len(audit.template_variables), 2)
        self.assertEqual(
            len(audit.controls),
            1,
            f"expected the resolvable template variable to be audited: {audit.controls}",
        )
        control = audit.controls[0]
        self.assertEqual(control.get("variable_name"), "host")
        self.assertEqual(control.get("variable_type"), "datadog_template")
        self.assertEqual(control.get("available_options"), ["web-1", "web-2"])
        # The trace docs render ``label`` + ``type``; both must survive.
        self.assertEqual(control.get("label"), "host")
        self.assertEqual(control.get("type"), "options")

    def test_datadog_audit_still_emits_yaml(self):
        """The switch to ``generate_dashboard_artifacts`` keeps the YAML view."""
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "dash.json"
            source.write_text(
                json.dumps(self._dashboard_with_template_variables()),
                encoding="utf-8",
            )

            audit = audit_pipeline._audit_datadog_dashboard(source, "metrics-otel-default")

        self.assertIn("dashboards:", audit.yaml_content)
        self.assertNotIn("YAML generation failed", audit.yaml_content)


if __name__ == "__main__":
    unittest.main()
