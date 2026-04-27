import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from observability_migration.adapters.source.grafana import alert_pipeline as grafana_alert_pipeline
from observability_migration.adapters.source.grafana import cli as grafana_cli
from observability_migration.core.reporting.report import MigrationResult, PanelResult


class GrafanaCliSmokeParityTests(unittest.TestCase):
    def test_parse_args_defaults_field_profile_to_otel(self):
        args = grafana_cli.parse_args([])

        self.assertEqual(args.field_profile, "otel")

    def test_parse_args_accepts_otel_field_profile(self):
        args = grafana_cli.parse_args(["--field-profile", "otel"])

        self.assertEqual(args.field_profile, "otel")

    def test_validate_field_profile_rejects_unsupported_profile(self):
        args = SimpleNamespace(field_profile="prometheus")

        with self.assertRaises(SystemExit) as ctx:
            grafana_cli._validate_field_profile(args)

        self.assertEqual(ctx.exception.code, 2)

    def test_parse_args_accepts_integrated_smoke_options(self):
        args = grafana_cli.parse_args(
            [
                "--smoke",
                "--browser-audit",
                "--capture-screenshots",
                "--smoke-output",
                "smoke.json",
                "--smoke-timeout",
                "45",
                "--time-from",
                "now-24h",
                "--time-to",
                "now-5m",
                "--chrome-binary",
                "/usr/bin/chrome",
            ]
        )

        self.assertTrue(args.smoke)
        self.assertTrue(args.browser_audit)
        self.assertTrue(args.capture_screenshots)
        self.assertEqual(args.smoke_output, "smoke.json")
        self.assertEqual(args.smoke_timeout, 45)
        self.assertEqual(args.time_from, "now-24h")
        self.assertEqual(args.time_to, "now-5m")
        self.assertEqual(args.chrome_binary, "/usr/bin/chrome")

    def test_normalize_execution_flags_auto_enables_upload_but_not_validate_for_smoke(self):
        args = SimpleNamespace(
            preflight=False,
            upload=False,
            validate=False,
            smoke=True,
            browser_audit=False,
            capture_screenshots=False,
            smoke_report="",
            smoke_output="",
            es_url="https://example.es",
            kibana_url="https://kibana.example",
        )

        auto_enabled_upload, auto_enabled_validate = grafana_cli._normalize_execution_flags(args)

        self.assertTrue(auto_enabled_upload)
        self.assertFalse(auto_enabled_validate)
        self.assertTrue(args.upload)
        self.assertFalse(args.validate)

    def test_normalize_execution_flags_rejects_browser_audit_without_smoke(self):
        args = SimpleNamespace(
            preflight=False,
            upload=False,
            validate=False,
            smoke=False,
            browser_audit=True,
            capture_screenshots=False,
            smoke_report="",
            smoke_output="",
            es_url="https://example.es",
            kibana_url="https://kibana.example",
        )

        with self.assertRaises(SystemExit) as ctx:
            grafana_cli._normalize_execution_flags(args)

        self.assertEqual(ctx.exception.code, 2)

    def test_smoke_uploaded_dashboards_calls_kibana_smoke_with_output_artifacts(self):
        result = MigrationResult(
            dashboard_title="Dash",
            dashboard_uid="uid-1",
            uploaded=True,
            panel_results=[
                PanelResult(
                    title="CPU",
                    grafana_type="graph",
                    kibana_type="xy",
                    status="migrated",
                    confidence=1.0,
                )
            ],
        )
        smoke_payload = {
            "summary": {
                "runtime_error_panels": 0,
                "empty_panels": 0,
                "not_runtime_checked_panels": 0,
                "dashboards_with_layout_issues": 0,
                "dashboards_with_browser_errors": 0,
            },
            "dashboards": [
                {
                    "id": "kibana-1",
                    "title": "Dash",
                    "status": "pass",
                    "failing_panels": [],
                    "empty_panels": [],
                    "not_runtime_checked_panels": [],
                    "layout": {"overlaps": [], "invalid_sizes": [], "out_of_bounds": []},
                    "browser_audit": {"status": "clean", "issues": []},
                    "panels": [{"panel": "CPU", "status": "pass"}],
                }
            ],
        }
        args = SimpleNamespace(
            kibana_url="https://kibana.example",
            kibana_api_key="secret-kb",
            es_url="https://example.es",
            es_api_key="secret-es",
            shadow_space="shadow",
            smoke_output="",
            smoke_timeout=45,
            time_from="now-24h",
            time_to="now-5m",
            browser_audit=True,
            capture_screenshots=True,
            chrome_binary="/usr/bin/chrome",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(grafana_cli, "run_smoke_report", return_value=smoke_payload) as mock_smoke:
                with mock.patch.object(
                    grafana_cli,
                    "merge_smoke_into_results",
                    return_value={"merged": 1, "smoke_failed": 0, "browser_failed": 0, "empty_result": 0, "not_runtime_checked": 0},
                ) as mock_merge:
                    state = grafana_cli._smoke_uploaded_dashboards([result], Path(tmpdir), args)

        self.assertEqual(
            state["output_path"],
            str(Path(tmpdir) / "uploaded_dashboard_smoke_report.json"),
        )
        smoke_kwargs = mock_smoke.call_args.kwargs
        self.assertEqual(smoke_kwargs["space_id"], "shadow")
        self.assertEqual(smoke_kwargs["dashboard_titles"], ["Dash"])
        self.assertEqual(
            smoke_kwargs["browser_audit_dir"],
            str(Path(tmpdir) / "browser_qa"),
        )
        self.assertEqual(
            smoke_kwargs["screenshot_dir"],
            str(Path(tmpdir) / "dashboard_qa"),
        )
        self.assertEqual(smoke_kwargs["timeout"], 45)
        self.assertEqual(smoke_kwargs["time_from"], "now-24h")
        self.assertEqual(smoke_kwargs["time_to"], "now-5m")
        self.assertTrue(smoke_kwargs["browser_audit"])
        self.assertTrue(smoke_kwargs["capture_screenshots"])
        self.assertEqual(smoke_kwargs["chrome_binary"], "/usr/bin/chrome")
        mock_merge.assert_called_once_with([result], smoke_payload)


class GrafanaAlertSpaceSelectionTests(unittest.TestCase):
    def test_alert_payload_preflight_uses_shadow_space(self):
        args = SimpleNamespace(
            kibana_url="https://kibana.example",
            kibana_api_key="secret-kb",
            shadow_space="shadow",
        )
        mapping_batch = {
            "results": [
                {
                    "alert_id": "alert-1",
                    "mapping": {
                        "rule_payload": {
                            "rule_type_id": ".index-threshold",
                            "params": {"aggType": "count"},
                        }
                    },
                }
            ]
        }

        with mock.patch.object(
            grafana_alert_pipeline,
            "run_alerting_preflight",
            return_value={"connectors": []},
        ) as mock_preflight, mock.patch.object(
            grafana_alert_pipeline,
            "validate_rule_payload",
            return_value={"ok": True},
        ):
            lookup, preflight = grafana_alert_pipeline.build_payload_validation_lookup(
                args,
                mapping_batch,
            )

        self.assertEqual(preflight, {"connectors": []})
        self.assertEqual(lookup, {"alert-1": {"ok": True}})
        self.assertEqual(mock_preflight.call_args.kwargs["space_id"], "shadow")

    def test_alert_rule_creation_uses_shadow_space(self):
        args = SimpleNamespace(
            create_alert_rules=True,
            kibana_url="https://kibana.example",
            kibana_api_key="secret-kb",
            shadow_space="shadow",
        )
        mapping_batch = {"results": [{"mapping": {"rule_payload": {"name": "CPU"}}}]}

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            grafana_alert_pipeline,
            "create_rules_from_payloads",
            return_value={
                "summary": {"created": 1, "failed": 0, "skipped": 0},
                "failed": [],
            },
        ) as mock_create:
            grafana_alert_pipeline.create_rules_if_requested(
                args=args,
                output_dir=Path(tmpdir),
                mapping_batch=mapping_batch,
                payload_preflight={"connectors": []},
            )

        self.assertEqual(mock_create.call_args.kwargs["space_id"], "shadow")


class GrafanaAssetIsolationTests(unittest.TestCase):
    def test_alerts_only_api_forwards_grafana_token_for_legacy_dashboard_reads(self):
        alert_pipeline = ModuleType(
            "observability_migration.adapters.source.grafana.alert_pipeline"
        )
        alert_pipeline.run_alert_pipeline = mock.Mock(
            side_effect=RuntimeError("grafana-alert-pipeline-called")
        )

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
            sys.modules,
            {
                "observability_migration.adapters.source.grafana.alert_pipeline": alert_pipeline,
            },
        ), mock.patch.object(
            grafana_cli,
            "extract_dashboards_from_grafana",
            return_value=[],
        ) as mock_extract_api:
            with self.assertRaisesRegex(RuntimeError, "grafana-alert-pipeline-called"):
                grafana_cli.main(
                    [
                        "--assets",
                        "alerts",
                        "--source",
                        "api",
                        "--grafana-token",
                        "token-123",
                        "--output-dir",
                        tmpdir,
                    ]
                )

        mock_extract_api.assert_called_once_with(
            grafana_cli.GRAFANA_URL,
            grafana_cli.GRAFANA_USER,
            grafana_cli.GRAFANA_PASS,
            token="token-123",
        )
        alert_pipeline.run_alert_pipeline.assert_called_once()

    @mock.patch(
        "observability_migration.adapters.source.grafana.cli.load_rule_pack_files",
        side_effect=AssertionError(
            "dashboard rule-pack setup should be skipped for --assets alerts"
        ),
    )
    @mock.patch(
        "observability_migration.adapters.source.grafana.cli.load_python_plugins",
        side_effect=AssertionError(
            "dashboard plugin setup should be skipped for --assets alerts"
        ),
    )
    def test_alerts_only_skips_dashboard_rule_pack_setup(
        self,
        mock_load_plugins,
        mock_load_rule_pack,
    ):
        alert_pipeline = ModuleType(
            "observability_migration.adapters.source.grafana.alert_pipeline"
        )
        alert_pipeline.run_alert_pipeline = mock.Mock(
            side_effect=RuntimeError("grafana-alert-pipeline-called")
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            dashboard = {
                "title": "Alerts",
                "uid": "grafana-uid-setup",
                "panels": [],
            }
            (Path(tmpdir) / "dashboard.json").write_text(
                json.dumps(dashboard),
                encoding="utf-8",
            )
            with mock.patch.dict(
                sys.modules,
                {
                    "observability_migration.adapters.source.grafana.alert_pipeline": alert_pipeline,
                },
            ):
                with self.assertRaisesRegex(RuntimeError, "grafana-alert-pipeline-called"):
                    grafana_cli.main(
                        [
                            "--assets",
                            "alerts",
                            "--source",
                            "files",
                            "--input-dir",
                            tmpdir,
                            "--output-dir",
                            tmpdir,
                        ]
                    )

        mock_load_rule_pack.assert_not_called()
        mock_load_plugins.assert_not_called()
        alert_pipeline.run_alert_pipeline.assert_called_once()

    @mock.patch(
        "observability_migration.adapters.source.grafana.cli.translate_dashboard",
        side_effect=AssertionError(
            "dashboard translation should be skipped for --assets alerts"
        ),
    )
    def test_alerts_only_skips_dashboard_translation(
        self,
        mock_translate,
    ):
        alert_pipeline = ModuleType(
            "observability_migration.adapters.source.grafana.alert_pipeline"
        )
        alert_pipeline.run_alert_pipeline = mock.Mock(
            side_effect=RuntimeError("grafana-alert-pipeline-called")
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            dashboard = {
                "title": "Alerts",
                "uid": "grafana-uid-1",
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
            (Path(tmpdir) / "dashboard.json").write_text(
                json.dumps(dashboard),
                encoding="utf-8",
            )
            rule_pack = SimpleNamespace(
                logs_index="",
                native_promql=False,
                metrics_dataset_filter="",
                logs_dataset_filter="",
            )
            resolver = mock.Mock()
            resolver._field_cache = {}
            resolver._discovered_mappings = {}

            with mock.patch.dict(
                sys.modules,
                {
                    "observability_migration.adapters.source.grafana.alert_pipeline": alert_pipeline,
                },
            ), mock.patch.object(
                grafana_cli,
                "load_rule_pack_files",
                return_value=rule_pack,
            ), mock.patch.object(
                grafana_cli,
                "load_python_plugins",
            ), mock.patch.object(
                grafana_cli,
                "SchemaResolver",
                return_value=resolver,
            ), mock.patch.object(
                sys,
                "argv",
                [
                    "grafana-cli",
                    "--assets",
                    "alerts",
                    "--source",
                    "files",
                    "--input-dir",
                    tmpdir,
                    "--output-dir",
                    tmpdir,
                ],
            ):
                with self.assertRaisesRegex(RuntimeError, "grafana-alert-pipeline-called"):
                    grafana_cli.main()

        mock_translate.assert_not_called()
        alert_pipeline.run_alert_pipeline.assert_called_once()
        self.assertEqual(
            alert_pipeline.run_alert_pipeline.call_args.kwargs["output_dir"],
            Path(tmpdir) / "alerts",
        )
        raw_dashboards = alert_pipeline.run_alert_pipeline.call_args.kwargs["raw_dashboards"]
        self.assertEqual(len(raw_dashboards), 1)
        self.assertEqual(raw_dashboards[0]["title"], dashboard["title"])

    def test_dashboards_only_writes_root_run_summary(self):
        rule_pack = SimpleNamespace(
            logs_index="",
            native_promql=False,
            metrics_dataset_filter="",
            logs_dataset_filter="",
        )
        resolver = mock.Mock()
        resolver._field_cache = {}
        resolver._discovered_mappings = {}

        def _fake_translate_dashboard(dashboard, yaml_dir, **_kwargs):
            yaml_path = yaml_dir / "demo-dashboard.yaml"
            yaml_path.write_text("dashboard: true\n", encoding="utf-8")
            return MigrationResult(dashboard["title"], dashboard["uid"]), yaml_path

        def _fake_compile_all(_yaml_dir, compiled_dir):
            compiled_leaf = compiled_dir / "demo-dashboard"
            compiled_leaf.mkdir(parents=True, exist_ok=True)
            (compiled_leaf / "compiled_dashboards.ndjson").write_text(
                "{}\n",
                encoding="utf-8",
            )
            return [("demo-dashboard.yaml", True, "")]

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            grafana_cli,
            "_load_configured_rule_pack",
            return_value=rule_pack,
        ), mock.patch.object(
            grafana_cli,
            "SchemaResolver",
            return_value=resolver,
        ), mock.patch.object(
            grafana_cli,
            "extract_dashboards_from_files",
            return_value=[{"title": "Demo Dashboard", "uid": "demo-uid"}],
        ), mock.patch.object(
            grafana_cli,
            "translate_dashboard",
            side_effect=_fake_translate_dashboard,
        ), mock.patch.object(
            grafana_cli,
            "_collect_feature_gap_artifacts",
            return_value={
                "dashboard_links": [],
                "panel_links": [],
                "annotations": [],
                "transform_tasks": [],
                "alert_tasks": [],
                "links_summary": {
                    "dashboard_links": 0,
                    "panel_links": 0,
                    "manual_wiring_needed": 0,
                },
                "annotations_summary": {
                    "total": 0,
                    "candidate_event_annotations": 0,
                    "manual_needed": 0,
                },
                "transform_summary": {"total": 0, "by_complexity": {}},
                "alert_summary": {"total": 0, "by_kibana_type": {}},
            },
        ), mock.patch.object(
            grafana_cli,
            "lint_dashboard_yaml",
            return_value=(True, ""),
        ), mock.patch.object(
            grafana_cli,
            "compile_all",
            side_effect=_fake_compile_all,
        ), mock.patch.object(
            grafana_cli,
            "validate_compiled_layout",
            return_value=(True, ""),
        ), mock.patch.object(
            grafana_cli,
            "detect_space_id_from_kibana_url",
            return_value="",
        ), mock.patch.object(
            grafana_cli,
            "annotate_results_with_verification",
            return_value={},
        ), mock.patch.object(
            grafana_cli,
            "save_detailed_report",
        ), mock.patch.object(
            grafana_cli,
            "save_migration_manifest",
        ), mock.patch.object(
            grafana_cli,
            "save_verification_packets",
        ), mock.patch.object(
            grafana_cli,
            "build_rollout_plan",
            return_value={},
        ), mock.patch.object(
            grafana_cli,
            "save_rollout_plan",
        ), mock.patch.object(
            grafana_cli,
            "generate_review_queue",
            return_value=[],
        ), mock.patch.object(
            grafana_cli,
            "print_report",
        ):
            grafana_cli.main(
                [
                    "--assets",
                    "dashboards",
                    "--source",
                    "files",
                    "--input-dir",
                    tmpdir,
                    "--output-dir",
                    tmpdir,
                ]
            )

            run_summary = json.loads(
                (Path(tmpdir) / "run_summary.json").read_text(encoding="utf-8")
            )
            yaml_output_path = Path(tmpdir) / "dashboards" / "yaml" / "demo-dashboard.yaml"
            yaml_output_exists = yaml_output_path.exists()

        self.assertEqual(run_summary["requested_assets"], "dashboards")
        self.assertEqual(run_summary["ran"], {"dashboards": True, "alerts": False})
        self.assertEqual(
            run_summary["dashboards"]["artifacts_dir"],
            str(Path(tmpdir) / "dashboards"),
        )
        self.assertTrue(yaml_output_exists)


if __name__ == "__main__":
    unittest.main()
