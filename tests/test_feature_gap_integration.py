# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest
from unittest import mock

from observability_migration.adapters.source.grafana import annotations as annotations_module
from observability_migration.adapters.source.grafana import manifest as manifest_module
from observability_migration.adapters.source.grafana import panels as panels_module
from observability_migration.adapters.source.grafana import rules as rules_module
from observability_migration.adapters.source.grafana import schema as schema_module
from observability_migration.adapters.source.grafana import smoke_integration
from observability_migration.adapters.source.grafana.alerts import extract_alerts_from_dashboard
from observability_migration.core.reporting.report import MigrationResult, PanelResult
from observability_migration.targets.kibana import compile as compile_module


class KibanaSpaceHelpersTests(unittest.TestCase):
    def test_kibana_url_for_space_preserves_existing_space_without_override(self):
        self.assertEqual(
            compile_module.kibana_url_for_space("http://localhost:5601/s/observability", ""),
            "http://localhost:5601/s/observability",
        )

    def test_kibana_url_for_space_applies_override(self):
        self.assertEqual(
            compile_module.kibana_url_for_space("http://localhost:5601/s/observability", "shadow"),
            "http://localhost:5601/s/shadow",
        )
        self.assertEqual(
            compile_module.detect_space_id_from_kibana_url("http://localhost:5601/s/observability"),
            "observability",
        )

    def test_upload_yaml_includes_api_key_when_provided(self):
        import observability_migration.targets.kibana.compile as _canonical_compile
        with mock.patch.object(_canonical_compile, "_run_command", return_value=(True, "")) as run_command:
            compile_module.upload_yaml(
                "dash.yaml",
                "out",
                "http://localhost:5601",
                space_id="shadow",
                kibana_api_key="secret-key",
            )

        args = run_command.call_args.args[0]
        self.assertIn("--kibana-api-key", args)
        self.assertIn("secret-key", args)


class PieFallbackTests(unittest.TestCase):
    def test_pie_without_categorical_breakdown_falls_back_to_bar(self):
        rule_pack = rules_module.RulePackConfig()
        resolver = schema_module.SchemaResolver(rule_pack)
        panel = {
            "id": 1,
            "type": "piechart",
            "title": "Traffic Distribution",
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [{"expr": "avg(http_requests_total)", "refId": "A"}],
            "options": {"pieType": "pie"},
        }

        yaml_panel, result = panels_module.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )

        self.assertEqual(result.kibana_type, "pie")
        self.assertEqual(yaml_panel["esql"]["type"], "bar")
        self.assertTrue(
            any("Approximated pie chart as bar chart" in reason for reason in result.reasons),
        )


class SmokeMergeTests(unittest.TestCase):
    def test_merge_smoke_matches_dashboard_title_and_preserves_duplicate_panel_titles(self):
        result = MigrationResult("System Overview", "grafana-uid-1")
        panel_a = PanelResult("CPU Usage", "timeseries", "line", "migrated", 1.0)
        panel_b = PanelResult("CPU Usage", "timeseries", "line", "migrated", 1.0)
        panel_c = PanelResult("Disk Free", "stat", "metric", "migrated", 1.0)
        result.panel_results = [panel_a, panel_b, panel_c]

        smoke_report = {
            "dashboards": [
                {
                    "id": "kibana-dashboard-123",
                    "title": "System Overview",
                    "browser_audit": {"status": "clean"},
                    "panels": [
                        {"panel": "CPU Usage", "status": "fail"},
                        {"panel": "CPU Usage", "status": "empty"},
                        {"panel": "Disk Free", "status": "pass"},
                    ],
                }
            ]
        }

        summary = smoke_integration.merge_smoke_into_results([result], smoke_report)

        self.assertEqual(summary["smoke_failed"], 1)
        self.assertEqual(summary["empty_result"], 1)
        self.assertIn("smoke_failed", panel_a.runtime_rollups)
        self.assertIn("empty_result", panel_b.runtime_rollups)
        self.assertEqual(panel_c.runtime_rollups, [])


class ManifestWiringTests(unittest.TestCase):
    def test_manifest_includes_feature_gap_fields(self):
        result = MigrationResult("System Overview", "grafana-uid-1")
        result.dashboard_links = [{"kibana_action": "manual_navigation", "title": "Docs"}]
        result.annotations = [{"kibana_action": "candidate_event_annotation", "name": "Deploys"}]
        result.alert_migration_tasks = [{"alert_type": "legacy", "suggested_kibana_rule_type": "threshold"}]
        result.feature_gap_summary = {"links": {"dashboard_links": 1}}

        panel = PanelResult("CPU Usage", "timeseries", "line", "migrated", 1.0)
        panel.link_migrations = [{"kibana_action": "url_drilldown", "title": "Runbook"}]
        panel.transformation_redesign_tasks = [{"transform_id": "calculateField", "complexity": "medium"}]
        result.panel_results = [panel]

        payload = manifest_module.build_migration_manifest([result])

        self.assertIn("feature_gaps", payload)
        self.assertEqual(payload["feature_gaps"]["links"]["dashboard_links"], 1)
        self.assertEqual(payload["feature_gaps"]["links"]["panel_links"], 1)
        self.assertEqual(payload["feature_gaps"]["annotations"]["candidate_event_annotations"], 1)
        self.assertEqual(payload["feature_gaps"]["alert_migration"]["total"], 1)
        self.assertEqual(payload["dashboards"][0]["dashboard_links"][0]["title"], "Docs")
        self.assertEqual(payload["panels"][0]["link_migrations"][0]["title"], "Runbook")

    def test_manifest_includes_runtime_feature_profile(self):
        from observability_migration.adapters.source.grafana.runtime_features import PROMQL_COMMAND_V0

        result = MigrationResult("Native PromQL", "grafana-uid-2")
        result.runtime_features = {
            PROMQL_COMMAND_V0: {
                "supported": True,
                "source": "default",
                "confidence": "unverified",
                "level": "runtime",
                "reason": "no --es-url configured; native PROMQL assumed for offline migration",
            }
        }

        payload = manifest_module.build_migration_manifest([result])

        self.assertEqual(payload["runtime_features"], result.runtime_features)


class AlertTraversalTests(unittest.TestCase):
    def test_extract_alerts_from_row_panels(self):
        dashboard = {
            "title": "Alerts",
            "uid": "grafana-uid-2",
            "rows": [
                {
                    "title": "Row A",
                    "panels": [
                        {
                            "id": 101,
                            "title": "CPU Alert",
                            "alert": {
                                "name": "CPU High",
                                "conditions": [
                                    {
                                        "evaluator": {"type": "gt", "params": [80]},
                                        "reducer": {"type": "avg"},
                                        "query": {"params": ["A", "5m", "now"]},
                                        "operator": {"type": "and"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        alerts = extract_alerts_from_dashboard(dashboard)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["source_panel_id"], "101")
        self.assertEqual(alerts[0]["name"], "CPU High")


class AnnotationSummaryTests(unittest.TestCase):
    def test_candidate_event_annotations_are_not_counted_as_auto_translated(self):
        annotations = annotations_module.translate_annotations(
            {
                "annotations": {
                    "list": [
                        {
                            "name": "Scale-up",
                            "datasource": {"type": "prometheus"},
                            "expr": "kube_deployment_status_replicas_updated",
                        }
                    ]
                }
            }
        )

        summary = annotations_module.build_annotations_summary(annotations)

        self.assertEqual(summary["auto_translated"], 0)
        self.assertEqual(summary["candidate_event_annotations"], 1)


if __name__ == "__main__":
    unittest.main()
