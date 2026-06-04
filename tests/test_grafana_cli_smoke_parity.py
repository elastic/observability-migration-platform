# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import contextlib
import io
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

    def test_parse_args_accepts_input_mode_alias_for_source(self):
        args = grafana_cli.parse_args(["--input-mode", "api"])

        self.assertEqual(args.input_mode, "api")
        self.assertEqual(args.source, "api")

    def test_parse_args_rejects_conflicting_source_and_input_mode(self):
        with self.assertRaises(SystemExit):
            grafana_cli.parse_args(["--source", "files", "--input-mode", "api"])

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
            preflight=True,
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

    def test_alert_payload_preflight_skipped_without_preflight_flag(self):
        args = SimpleNamespace(
            preflight=False,
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
            side_effect=AssertionError("preflight should not run"),
        ), mock.patch.object(
            grafana_alert_pipeline,
            "validate_rule_payload",
            side_effect=AssertionError("payload validation requires preflight"),
        ):
            lookup, preflight = grafana_alert_pipeline.build_payload_validation_lookup(
                args,
                mapping_batch,
            )

        self.assertEqual(preflight, None)
        self.assertEqual(lookup, {})

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
    def test_dashboards_only_clears_stale_dashboard_yaml_before_compile(self):
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
            yaml_path = yaml_dir / "current-dashboard.yaml"
            yaml_path.write_text("dashboard: current\n", encoding="utf-8")
            return MigrationResult(dashboard["title"], dashboard["uid"]), yaml_path

        compiled_yaml_names = []

        def _fake_compile_all(yaml_dir, _compiled_dir):
            compiled_yaml_names[:] = [yaml_file.name for yaml_file in sorted(yaml_dir.glob("*.yaml"))]
            return [(name, True, "") for name in compiled_yaml_names]

        with tempfile.TemporaryDirectory() as tmpdir:
            stale_yaml_dir = Path(tmpdir) / "dashboards" / "yaml"
            stale_yaml_dir.mkdir(parents=True)
            (stale_yaml_dir / "stale-from-other-grafana.yaml").write_text(
                "dashboard: stale\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                grafana_cli,
                "_load_configured_rule_pack",
                return_value=rule_pack,
            ), mock.patch.object(
                grafana_cli,
                "SchemaResolver",
                return_value=resolver,
            ), mock.patch.object(
                grafana_cli,
                "extract_dashboards_from_grafana",
                return_value=[{"title": "Current Dashboard", "uid": "current-uid"}],
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
                        "api",
                        "--output-dir",
                        tmpdir,
                    ]
                )

        self.assertEqual(compiled_yaml_names, ["current-dashboard.yaml"])

    def test_dashboards_only_empty_input_exits_with_clean_message(self):
        """An empty --input-dir should exit(1) with a helpful message instead of
        silently reporting "0/0 dashboards compiled successfully"."""
        rule_pack = SimpleNamespace(
            logs_index="",
            native_promql=False,
            metrics_dataset_filter="",
            logs_dataset_filter="",
        )
        resolver = mock.Mock()
        resolver._field_cache = {}
        resolver._discovered_mappings = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            empty_input = Path(tmpdir) / "empty_input"
            empty_input.mkdir()
            stderr = io.StringIO()
            with mock.patch.object(
                grafana_cli,
                "_load_configured_rule_pack",
                return_value=rule_pack,
            ), mock.patch.object(
                grafana_cli,
                "SchemaResolver",
                return_value=resolver,
            ), contextlib.redirect_stderr(stderr), self.assertRaises(
                SystemExit
            ) as ctx:
                grafana_cli.main(
                    [
                        "--assets",
                        "dashboards",
                        "--source",
                        "files",
                        "--input-dir",
                        str(empty_input),
                        "--output-dir",
                        tmpdir,
                    ]
                )

        self.assertEqual(ctx.exception.code, 1)
        message = stderr.getvalue()
        self.assertIn("no Grafana dashboards found", message)
        self.assertIn(str(empty_input), message)

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
            verify=True,
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

    def test_dashboards_only_writes_markdown_summary(self):
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
            (compiled_leaf / "compiled_dashboards.ndjson").write_text("{}\n", encoding="utf-8")
            return [("demo-dashboard.yaml", True, "")]

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            grafana_cli, "_load_configured_rule_pack", return_value=rule_pack,
        ), mock.patch.object(
            grafana_cli, "SchemaResolver", return_value=resolver,
        ), mock.patch.object(
            grafana_cli, "extract_dashboards_from_files",
            return_value=[{"title": "Demo Dashboard", "uid": "demo-uid"}],
        ), mock.patch.object(
            grafana_cli, "translate_dashboard", side_effect=_fake_translate_dashboard,
        ), mock.patch.object(
            grafana_cli, "_collect_feature_gap_artifacts",
            return_value={
                "dashboard_links": [], "panel_links": [], "annotations": [],
                "transform_tasks": [], "alert_tasks": [],
                "links_summary": {"dashboard_links": 0, "panel_links": 0, "manual_wiring_needed": 0},
                "annotations_summary": {"total": 0, "candidate_event_annotations": 0, "manual_needed": 0},
                "transform_summary": {"total": 0, "by_complexity": {}},
                "alert_summary": {"total": 0, "by_kibana_type": {}},
            },
        ), mock.patch.object(
            grafana_cli, "lint_dashboard_yaml", return_value=(True, ""),
        ), mock.patch.object(
            grafana_cli, "compile_all", side_effect=_fake_compile_all,
        ), mock.patch.object(
            grafana_cli, "validate_compiled_layout", return_value=(True, ""),
        ), mock.patch.object(
            grafana_cli, "detect_space_id_from_kibana_url", return_value="",
        ), mock.patch.object(
            grafana_cli, "annotate_results_with_verification", return_value={},
        ), mock.patch.object(
            grafana_cli, "save_detailed_report",
        ), mock.patch.object(
            grafana_cli, "save_migration_manifest",
        ), mock.patch.object(
            grafana_cli, "save_verification_packets",
        ), mock.patch.object(
            grafana_cli, "build_rollout_plan", return_value={},
        ), mock.patch.object(
            grafana_cli, "save_rollout_plan",
        ), mock.patch.object(
            grafana_cli, "generate_review_queue", return_value=[],
        ), mock.patch.object(
            grafana_cli, "print_report",
        ):
            grafana_cli.main(
                ["--assets", "dashboards", "--source", "files",
                 "--input-dir", tmpdir, "--output-dir", tmpdir]
            )
            summary_path = Path(tmpdir) / "dashboards" / "migration_summary.md"
            schema_report_path = Path(tmpdir) / "dashboards" / "schema_change_report.md"
            telemetry_contract_path = Path(tmpdir) / "dashboards" / "telemetry_contract.json"
            summary_exists = summary_path.exists()
            schema_report_exists = schema_report_path.exists()
            telemetry_contract_exists = telemetry_contract_path.exists()
            summary_text = summary_path.read_text(encoding="utf-8") if summary_exists else ""

        self.assertTrue(summary_exists)
        self.assertIn("# Migration Summary — Grafana → Kibana", summary_text)
        self.assertTrue(schema_report_exists)
        self.assertTrue(telemetry_contract_exists)

    def test_markdown_summary_failure_is_non_fatal(self):
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
            (compiled_leaf / "compiled_dashboards.ndjson").write_text("{}\n", encoding="utf-8")
            return [("demo-dashboard.yaml", True, "")]

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            grafana_cli, "_load_configured_rule_pack", return_value=rule_pack,
        ), mock.patch.object(
            grafana_cli, "SchemaResolver", return_value=resolver,
        ), mock.patch.object(
            grafana_cli, "extract_dashboards_from_files",
            return_value=[{"title": "Demo Dashboard", "uid": "demo-uid"}],
        ), mock.patch.object(
            grafana_cli, "translate_dashboard", side_effect=_fake_translate_dashboard,
        ), mock.patch.object(
            grafana_cli, "_collect_feature_gap_artifacts",
            return_value={
                "dashboard_links": [], "panel_links": [], "annotations": [],
                "transform_tasks": [], "alert_tasks": [],
                "links_summary": {"dashboard_links": 0, "panel_links": 0, "manual_wiring_needed": 0},
                "annotations_summary": {"total": 0, "candidate_event_annotations": 0, "manual_needed": 0},
                "transform_summary": {"total": 0, "by_complexity": {}},
                "alert_summary": {"total": 0, "by_kibana_type": {}},
            },
        ), mock.patch.object(
            grafana_cli, "lint_dashboard_yaml", return_value=(True, ""),
        ), mock.patch.object(
            grafana_cli, "compile_all", side_effect=_fake_compile_all,
        ), mock.patch.object(
            grafana_cli, "validate_compiled_layout", return_value=(True, ""),
        ), mock.patch.object(
            grafana_cli, "detect_space_id_from_kibana_url", return_value="",
        ), mock.patch.object(
            grafana_cli, "annotate_results_with_verification", return_value={},
        ), mock.patch.object(
            grafana_cli, "save_detailed_report",
        ), mock.patch.object(
            grafana_cli, "save_migration_manifest",
        ), mock.patch.object(
            grafana_cli, "save_verification_packets",
        ), mock.patch.object(
            grafana_cli, "build_rollout_plan", return_value={},
        ), mock.patch.object(
            grafana_cli, "save_rollout_plan",
        ), mock.patch.object(
            grafana_cli, "generate_review_queue", return_value=[],
        ), mock.patch.object(
            grafana_cli, "print_report",
        ), mock.patch.object(
            grafana_cli, "save_markdown_summary",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise despite the summary renderer blowing up.
            grafana_cli.main(
                ["--assets", "dashboards", "--source", "files",
                 "--input-dir", tmpdir, "--output-dir", tmpdir]
            )
            # The run still completes and writes its root run summary.
            run_summary_exists = (Path(tmpdir) / "run_summary.json").exists()
            summary_md_exists = (Path(tmpdir) / "dashboards" / "migration_summary.md").exists()

        self.assertTrue(run_summary_exists)
        self.assertFalse(summary_md_exists)

    def test_schema_report_failure_is_non_fatal(self):
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
            (compiled_leaf / "compiled_dashboards.ndjson").write_text("{}\n", encoding="utf-8")
            return [("demo-dashboard.yaml", True, "")]

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            grafana_cli, "_load_configured_rule_pack", return_value=rule_pack,
        ), mock.patch.object(
            grafana_cli, "SchemaResolver", return_value=resolver,
        ), mock.patch.object(
            grafana_cli, "extract_dashboards_from_files",
            return_value=[{"title": "Demo Dashboard", "uid": "demo-uid"}],
        ), mock.patch.object(
            grafana_cli, "translate_dashboard", side_effect=_fake_translate_dashboard,
        ), mock.patch.object(
            grafana_cli, "_collect_feature_gap_artifacts",
            return_value={
                "dashboard_links": [], "panel_links": [], "annotations": [],
                "transform_tasks": [], "alert_tasks": [],
                "links_summary": {"dashboard_links": 0, "panel_links": 0, "manual_wiring_needed": 0},
                "annotations_summary": {"total": 0, "candidate_event_annotations": 0, "manual_needed": 0},
                "transform_summary": {"total": 0, "by_complexity": {}},
                "alert_summary": {"total": 0, "by_kibana_type": {}},
            },
        ), mock.patch.object(
            grafana_cli, "lint_dashboard_yaml", return_value=(True, ""),
        ), mock.patch.object(
            grafana_cli, "compile_all", side_effect=_fake_compile_all,
        ), mock.patch.object(
            grafana_cli, "validate_compiled_layout", return_value=(True, ""),
        ), mock.patch.object(
            grafana_cli, "detect_space_id_from_kibana_url", return_value="",
        ), mock.patch.object(
            grafana_cli, "annotate_results_with_verification", return_value={},
        ), mock.patch.object(
            grafana_cli, "save_detailed_report",
        ), mock.patch.object(
            grafana_cli, "save_migration_manifest",
        ), mock.patch.object(
            grafana_cli, "save_verification_packets",
        ), mock.patch.object(
            grafana_cli, "build_rollout_plan", return_value={},
        ), mock.patch.object(
            grafana_cli, "save_rollout_plan",
        ), mock.patch.object(
            grafana_cli, "generate_review_queue", return_value=[],
        ), mock.patch.object(
            grafana_cli, "print_report",
        ), mock.patch.object(
            grafana_cli, "write_schema_report_artifacts",
            side_effect=RuntimeError("schema failed"),
        ):
            grafana_cli.main(
                ["--assets", "dashboards", "--source", "files",
                 "--input-dir", tmpdir, "--output-dir", tmpdir]
            )
            run_summary_exists = (Path(tmpdir) / "run_summary.json").exists()
            summary_md_exists = (Path(tmpdir) / "dashboards" / "migration_summary.md").exists()
            schema_report_exists = (Path(tmpdir) / "dashboards" / "schema_change_report.md").exists()

        self.assertTrue(run_summary_exists)
        self.assertTrue(summary_md_exists)
        self.assertFalse(schema_report_exists)

    def test_upload_routes_through_kibana_target_adapter(self):
        rule_pack = SimpleNamespace(
            logs_index="",
            native_promql=False,
            metrics_dataset_filter="",
            logs_dataset_filter="",
        )
        resolver = mock.Mock()
        resolver._field_cache = {}
        resolver._discovered_mappings = {}
        adapter = mock.Mock()
        adapter.upload_dashboard.return_value = {
            "success": True,
            "output": "ok",
            "space_id": "shadow",
            "kibana_url": "https://kibana.example/s/shadow",
        }

        def _fake_translate_dashboard(dashboard, yaml_dir, **_kwargs):
            yaml_path = yaml_dir / "demo-dashboard.yaml"
            yaml_path.write_text("dashboards: []\n", encoding="utf-8")
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
            "KibanaTargetAdapter",
            return_value=adapter,
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
                    "--upload",
                    "--kibana-url",
                    "https://kibana.example",
                    "--kibana-api-key",
                    "secret",
                    "--shadow-space",
                    "shadow",
                ]
            )

            yaml_path = Path(tmpdir) / "dashboards" / "yaml" / "demo-dashboard.yaml"
            compiled_dir = Path(tmpdir) / "dashboards" / "compiled" / "demo-dashboard"

        adapter.upload_dashboard.assert_called_once_with(
            yaml_path,
            compiled_dir,
            kibana_url="https://kibana.example",
            space_id="shadow",
            kibana_api_key="secret",
            verify=True,
        )

    def test_lint_failure_skips_only_failing_yaml_before_compile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = Path(tmpdir) / "yaml"
            compiled_dir = Path(tmpdir) / "compiled"
            yaml_dir.mkdir()
            good_yaml = yaml_dir / "good.yaml"
            bad_yaml = yaml_dir / "bad.yaml"
            other_good_yaml = yaml_dir / "other-good.yaml"
            for yaml_path in (good_yaml, bad_yaml, other_good_yaml):
                yaml_path.write_text("dashboards: []\n", encoding="utf-8")

            lint_results = {
                good_yaml.name: (True, ""),
                bad_yaml.name: (False, "bad threshold order"),
                other_good_yaml.name: (True, ""),
            }
            compile_calls = []

            def _fake_compile_yaml(yaml_path, output_dir):
                compile_calls.append((Path(yaml_path).name, Path(output_dir).name))
                return True, f"compiled {Path(yaml_path).name}"

            with mock.patch.object(grafana_cli, "compile_yaml", side_effect=_fake_compile_yaml):
                compile_results = grafana_cli._compile_linted_yaml_files(
                    sorted(yaml_dir.glob("*.yaml")),
                    lint_results,
                    compiled_dir,
                )

        self.assertEqual(
            compile_calls,
            [("good.yaml", "good"), ("other-good.yaml", "other-good")],
        )
        self.assertEqual(
            compile_results,
            [
                ("bad.yaml", False, "Dashboard YAML lint failed before compile.\nbad threshold order"),
                ("good.yaml", True, "compiled good.yaml"),
                ("other-good.yaml", True, "compiled other-good.yaml"),
            ],
        )

    def test_layout_validation_runs_when_partial_lint_batch_still_compiles_dashboards(self):
        results = [
            MigrationResult("Bad", "bad-uid"),
            MigrationResult("Good", "good-uid"),
        ]
        compile_results = [
            ("bad.yaml", False, "Dashboard YAML lint failed before compile."),
            ("good.yaml", True, "compiled good.yaml"),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(
                grafana_cli,
                "validate_compiled_layout",
                return_value=(True, ""),
            ) as mock_validate:
                layout_ok, layout_output = grafana_cli._validate_compiled_layout_after_compile(
                    results,
                    compile_results,
                    Path(tmpdir),
                )

        self.assertTrue(layout_ok)
        self.assertEqual(layout_output, "")
        mock_validate.assert_called_once()
        self.assertTrue(all(result.layout_validated for result in results))
        self.assertTrue(all(result.layout_error == "" for result in results))


class TestTranslateDashboardResilient(unittest.TestCase):
    """_translate_dashboard_resilient must not propagate exceptions (issue #37)."""

    def _make_minimal_dashboard(self, title="My Dashboard"):
        return {
            "title": title,
            "uid": "abc123",
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "title": "Panel 1",
                    "targets": [{"expr": "rate(http_requests_total[5m])", "refId": "A",
                                 "datasource": {"type": "prometheus"}}],
                    "fieldConfig": {"defaults": {}, "overrides": []},
                    "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
                }
            ],
        }

    def test_exception_in_translate_returns_stub_result(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        dashboard = self._make_minimal_dashboard("Exploding Dashboard")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "observability_migration.adapters.source.grafana.cli.translate_dashboard",
                side_effect=RuntimeError("simulated crash"),
            ):
                result, yaml_path = grafana_cli._translate_dashboard_resilient(
                    dashboard,
                    Path(tmpdir),
                    datasource_index="metrics-*",
                    esql_index="metrics-*",
                    rule_pack=None,
                    resolver=None,
                )

        self.assertIsNone(yaml_path)
        self.assertEqual(result.dashboard_title, "Exploding Dashboard")
        self.assertIn("simulated crash", result.translation_error)
        self.assertEqual(result.migrated, 0)

    def test_success_passes_through_unchanged(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        dashboard = self._make_minimal_dashboard("Good Dashboard")
        fake_result = MigrationResult(dashboard_title="Good Dashboard", dashboard_uid="abc123")
        fake_path = Path("/tmp/good.yaml")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "observability_migration.adapters.source.grafana.cli.translate_dashboard",
                return_value=(fake_result, fake_path),
            ):
                result, yaml_path = grafana_cli._translate_dashboard_resilient(
                    dashboard,
                    Path(tmpdir),
                    datasource_index="metrics-*",
                    esql_index="metrics-*",
                    rule_pack=None,
                    resolver=None,
                )

        self.assertEqual(result, fake_result)
        self.assertEqual(yaml_path, fake_path)

    def test_stub_result_does_not_crash_yaml_path_lookups(self):
        """Stub results from failed translation must not crash any code that iterates dashboard_outputs."""
        bad_dashboard = self._make_minimal_dashboard("Bad")
        bad_dashboard["uid"] = "bad123"

        # Stub the bad dashboard
        bad_result = MigrationResult(
            dashboard_title="Bad",
            dashboard_uid="bad123",
            translation_error="Traceback:\n  RuntimeError: boom",
        )

        # dashboard_outputs format: [(result, yaml_path, raw_dashboard)]
        dashboard_outputs = [
            (bad_result, None, bad_dashboard),
        ]

        # Simulate the lint loop — this should not raise
        yaml_lint_results = {}  # empty, no yaml produced for failed dashboard
        for result, yaml_path, _dashboard in dashboard_outputs:
            if yaml_path is None:
                continue
            result.yaml_linted = True
            lint_ok, lint_output = yaml_lint_results.get(
                Path(yaml_path).name, (False, "missing")
            )
            result.yaml_lint_error = "" if lint_ok else lint_output

        # Verify the stub result was not modified and translation_error intact
        self.assertEqual(bad_result.translation_error, "Traceback:\n  RuntimeError: boom")
        self.assertFalse(getattr(bad_result, "yaml_linted", False))


class TestLabelReplaceTranslation(unittest.TestCase):
    """label_replace must translate to ES|QL EVAL (9 panels)."""

    _INDEX = "metrics-*"

    def _translate(self, expr):
        from observability_migration.adapters.source.grafana.rules import RulePackConfig
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        rp = RulePackConfig()
        return translate_promql_to_esql(expr, esql_index=self._INDEX, rule_pack=rp)

    def test_label_replace_copy_whole_label(self):
        # passthrough regex "(.*)" with "$1" keeps the source label available
        # through aggregation, then copies it to the new label.
        ctx = self._translate(
            'label_replace(rate(http_requests_total[5m]), "host", "$1", "instance", "(.*)")'
        )
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("BY time_bucket = TBUCKET(5 minute), instance", ctx.esql_query)
        self.assertIn("EVAL host = instance", ctx.esql_query)

    def test_label_replace_constant_value(self):
        # no capture group in replacement → EVAL env = "production"
        ctx = self._translate(
            'label_replace(rate(http_requests_total[5m]), "env", "production", "job", ".*")'
        )
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn('EVAL env = "production"', ctx.esql_query)

    def test_label_replace_regex_extract(self):
        # $1 with a literal-prefix capture → anchored GROK (ES|QL has no
        # REGEXP_EXTRACT function). GROK must be fully anchored to match PromQL's
        # full-value regex semantics.
        ctx = self._translate(
            'label_replace(rate(http_requests_total[5m]), "short", "$1", "job", "prefix-(.*)")'
        )
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("BY time_bucket = TBUCKET(5 minute), job", ctx.esql_query)
        # REGEXP_EXTRACT is not a valid ES|QL function and must never be emitted.
        self.assertNotIn("REGEXP_EXTRACT", ctx.esql_query)
        self.assertIn("GROK job", ctx.esql_query)
        self.assertIn("%{GREEDYDATA:short}", ctx.esql_query)
        # Anchored so it matches PromQL's fully-anchored regex behavior.
        self.assertIn('"^prefix-%{GREEDYDATA:short}$"', ctx.esql_query)

    def test_label_replace_regex_extract_suffix(self):
        # $1 capture before a literal suffix → anchored GROK.
        ctx = self._translate(
            'label_replace(rate(http_requests_total[5m]), "short", "$1", "job", "(.*)-worker")'
        )
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertNotIn("REGEXP_EXTRACT", ctx.esql_query)
        self.assertIn('"^%{GREEDYDATA:short}-worker$"', ctx.esql_query)

    def test_label_replace_regex_extract_complex_falls_back(self):
        # A capture pattern with regex metacharacters in the literal portion is
        # not safely GROK-expressible → degrade gracefully (no invalid function).
        ctx = self._translate(
            'label_replace(rate(http_requests_total[5m]), "short", "$1", "job", "(.*)\\\\.(svc|local)")'
        )
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertNotIn("REGEXP_EXTRACT", ctx.esql_query)
        self.assertTrue(
            any("label_replace" in w.lower() for w in ctx.warnings),
            f"Expected a label_replace warning, got: {ctx.warnings}",
        )

    def test_label_replace_complex_falls_back_gracefully(self):
        # $1-$2 multi-group replacement → not supported, graceful fallback
        ctx = self._translate(
            'label_replace(rate(http_requests_total[5m]), "new", "$1-$2", "job", "(a)-(b)")'
        )
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertTrue(
            any("label_replace" in w.lower() for w in ctx.warnings),
            f"Expected a label_replace warning, got: {ctx.warnings}",
        )


class TestTopkUngrouped(unittest.TestCase):
    """topk without explicit by() clause must translate when preferred_group_labels provided."""

    _INDEX = "metrics-*"

    def _translate(self, expr, hints=None):
        from observability_migration.adapters.source.grafana.rules import RulePackConfig
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        rp = RulePackConfig()
        return translate_promql_to_esql(
            expr,
            esql_index=self._INDEX,
            rule_pack=rp,
            translation_hints=hints or {},
        )

    def test_topk_with_preferred_group_labels(self):
        ctx = self._translate(
            "topk(5, rate(http_requests_total[5m]))",
            hints={"preferred_group_labels": ["job"]},
        )
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("LIMIT 5", ctx.esql_query)
        self.assertIn("SORT", ctx.esql_query)

    def test_topk_aggregate_syntax_with_preferred_labels(self):
        ctx = self._translate(
            "topk(3, sum(rate(http_requests_total[5m])))",
            hints={"preferred_group_labels": ["job"]},
        )
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("LIMIT 3", ctx.esql_query)

    def test_topk_no_labels_single_bucket_fallback(self):
        ctx = self._translate("topk(5, rate(http_requests_total[5m]))")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("LIMIT 5", ctx.esql_query)

    def test_topk_grouped_still_works(self):
        ctx = self._translate("topk(3, sum by (job) (rate(http_requests_total[5m])))")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("LIMIT 3", ctx.esql_query)
        self.assertIn("job", ctx.esql_query)


class TestValueWrapperTranslations(unittest.TestCase):
    """sort_desc/round/clamp_min must produce correct ES|QL output (quick wins)."""

    _INDEX = "metrics-*"

    def _translate(self, expr):
        from observability_migration.adapters.source.grafana.rules import RulePackConfig
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        rp = RulePackConfig()
        return translate_promql_to_esql(expr, esql_index=self._INDEX, rule_pack=rp)

    def test_sort_desc_emits_sort_value_desc(self):
        ctx = self._translate("sort_desc(sum by (job) (rate(http_requests_total[5m])))")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("SORT", ctx.esql_query)
        self.assertIn("DESC", ctx.esql_query)
        # value-sort warning present
        self.assertTrue(any("sort_desc" in w.lower() for w in ctx.warnings), ctx.warnings)

    def test_sort_asc_emits_sort_value_asc(self):
        ctx = self._translate("sort(sum by (job) (rate(http_requests_total[5m])))")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("SORT", ctx.esql_query)
        self.assertTrue(any("sort" in w.lower() for w in ctx.warnings), ctx.warnings)

    def test_round_emits_round_eval(self):
        ctx = self._translate("round(sum by (job) (rate(http_requests_total[5m])), 2)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("ROUND(", ctx.esql_query)
        self.assertTrue(any("round" in w.lower() for w in ctx.warnings), ctx.warnings)

    def test_clamp_min_emits_greatest_eval(self):
        ctx = self._translate("clamp_min(sum by (job) (rate(http_requests_total[5m])), 0)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("GREATEST(", ctx.esql_query)
        self.assertTrue(any("clamp_min" in w.lower() for w in ctx.warnings), ctx.warnings)

    def test_round_over_topk_inserts_eval_after_last(self):
        """EVAL for round() must appear after STATS value = LAST(...), not before it."""
        ctx = self._translate("round(topk(5, rate(http_requests_total[5m])), 2)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("ROUND(", ctx.esql_query)
        lines = ctx.esql_query.splitlines()
        last_idx = next(i for i, ln in enumerate(lines) if "= LAST(" in ln)
        round_idx = next(i for i, ln in enumerate(lines) if "ROUND(" in ln)
        self.assertGreater(round_idx, last_idx, "ROUND() must appear after STATS value = LAST(...)")

    def test_sort_desc_over_topk_preserves_time_bucket_sort(self):
        """sort_desc(topk()) must not replace the time-bucket SORT needed by LAST()."""
        ctx = self._translate("sort_desc(topk(5, rate(http_requests_total[5m])))")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("SORT time_bucket ASC", ctx.esql_query)
        self.assertIn("SORT value DESC", ctx.esql_query)


class TestPromQLOrFallback(unittest.TestCase):
    """PromQL 'or' between distinct metrics: translate left operand with warning."""

    _INDEX = "metrics-*"

    def _translate(self, expr):
        from observability_migration.adapters.source.grafana.rules import RulePackConfig
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )
        rp = RulePackConfig()
        return translate_promql_to_esql(expr, esql_index=self._INDEX, rule_pack=rp)

    def test_or_between_two_rates_uses_left_operand(self):
        """rate(a) or rate(b) → translates, references left metric, warns about fallback."""
        ctx = self._translate(
            "rate(http_requests_total[5m]) or rate(http_errors_total[5m])"
        )
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("http_requests_total", ctx.esql_query or "")
        self.assertTrue(
            any("or" in w.lower() and ("fallback" in w.lower() or "left" in w.lower())
                for w in ctx.warnings),
            f"Expected or-fallback warning; got: {ctx.warnings}",
        )

    def test_or_with_vector_zero_uses_left_operand(self):
        """rate(a) or vector(0) is a 'default to 0' idiom — translate left side."""
        ctx = self._translate("rate(http_requests_total[5m]) or vector(0)")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("http_requests_total", ctx.esql_query or "")

    def test_multi_metric_or_vector_zero_fallback_is_feasible(self):
        """(sum(A) or vector(0)) + (sum(B) or vector(0)) must migrate (issue #66 Pattern A).

        Each ``or vector(N)`` is a zero-fill fallback; stripping the vector
        operand leaves a translatable multi-metric sum.
        """
        ctx = self._translate(
            "(sum(a_metric) or vector(0)) + (sum(b_metric) or vector(0))"
        )
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("a_metric", ctx.esql_query or "")
        self.assertIn("b_metric", ctx.esql_query or "")
        self.assertNotIn(
            "vector() requires manual redesign", " ".join(ctx.warnings)
        )
        self.assertTrue(
            any("zero-fill" in w.lower() or "vector(" in w.lower() for w in ctx.warnings),
            f"expected a zero-fill approximation warning; got {ctx.warnings}",
        )

    def test_multi_metric_divide_by_or_vector_one_is_feasible(self):
        """sum(A) / (sum(B) or on() vector(1)) must migrate (issue #66 Pattern A)."""
        ctx = self._translate("sum(a_metric) / (sum(b_metric) or on() vector(1))")
        self.assertNotEqual(ctx.feasibility, "not_feasible", ctx.warnings)
        self.assertIn("a_metric", ctx.esql_query or "")
        self.assertIn("b_metric", ctx.esql_query or "")
        self.assertNotIn(
            "vector() requires manual redesign", " ".join(ctx.warnings)
        )

    def test_and_remains_not_feasible(self):
        """PromQL 'and' (set intersection) has no safe ES|QL equivalent."""
        ctx = self._translate("rate(foo_total[5m]) and rate(bar_total[5m])")
        self.assertEqual(ctx.feasibility, "not_feasible")

    def test_unless_remains_not_feasible(self):
        """PromQL 'unless' (set difference) has no safe ES|QL equivalent."""
        ctx = self._translate("rate(foo_total[5m]) unless rate(bar_total[5m])")
        self.assertEqual(ctx.feasibility, "not_feasible")


if __name__ == "__main__":
    unittest.main()
