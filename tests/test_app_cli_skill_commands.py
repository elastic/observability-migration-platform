# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the package-native discovery/verification subcommands.

These cover the ``schema-report``, ``audit-rules``, ``delete-rules``,
``verify-alert-rules`` and ``list-samples`` subcommands that expose previously
repo-only scripts through the installed ``obs-migrate`` CLI.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from observability_migration.app import cli as app_cli


class SchemaReportSubcommandTests(unittest.TestCase):
    def test_parser_accepts_repeatable_artifact_dirs_and_outputs(self):
        parser = app_cli._build_parser()
        args = parser.parse_args(
            [
                "schema-report",
                "--artifact-dir", "out/a/dashboards",
                "--artifact-dir", "out/b/dashboards",
                "--output", "schema.md",
                "--contract-out", "telemetry_contract.json",
            ]
        )
        self.assertEqual(args.command, "schema-report")
        self.assertEqual(args.artifact_dir, ["out/a/dashboards", "out/b/dashboards"])
        self.assertEqual(args.output, "schema.md")
        self.assertEqual(args.contract_out, "telemetry_contract.json")

    def test_run_schema_report_writes_markdown_without_contract_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_md = Path(tmpdir) / "schema.md"
            args = SimpleNamespace(
                artifact_dir=[str(Path(tmpdir) / "dashboards")],
                output=str(out_md),
                contract_out="",
            )
            with (
                patch.object(
                    app_cli, "build_schema_change_report", return_value="# Telemetry Schema Change Report\n"
                ) as mock_report,
                patch.object(app_cli, "build_telemetry_contract", return_value={"k": "v"}) as mock_single,
                patch.object(app_cli, "build_combined_telemetry_contract") as mock_combined,
                patch.object(app_cli, "write_telemetry_contract") as mock_write,
                redirect_stdout(io.StringIO()),
            ):
                app_cli._run_schema_report(args)

            self.assertTrue(out_md.exists())
            self.assertIn("Telemetry Schema Change Report", out_md.read_text(encoding="utf-8"))
            mock_report.assert_called_once()
            # The markdown report does not require building the contract object;
            # contract builders only run when --contract-out is requested.
            mock_single.assert_not_called()
            mock_combined.assert_not_called()
            mock_write.assert_not_called()

    def test_run_schema_report_combines_multiple_dirs_and_writes_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_md = Path(tmpdir) / "schema.md"
            contract = Path(tmpdir) / "contract.json"
            args = SimpleNamespace(
                artifact_dir=[str(Path(tmpdir) / "a"), str(Path(tmpdir) / "b")],
                output=str(out_md),
                contract_out=str(contract),
            )
            with (
                patch.object(app_cli, "build_schema_change_report", return_value="# report\n"),
                patch.object(app_cli, "build_telemetry_contract") as mock_single,
                patch.object(app_cli, "build_combined_telemetry_contract", return_value={"combined": True}) as mock_combined,
                patch.object(app_cli, "write_telemetry_contract") as mock_write,
                redirect_stdout(io.StringIO()),
            ):
                app_cli._run_schema_report(args)

            mock_single.assert_not_called()
            mock_combined.assert_called_once()
            mock_write.assert_called_once()


class AuditRulesSubcommandTests(unittest.TestCase):
    def test_parser_defaults(self):
        parser = app_cli._build_parser()
        args = parser.parse_args(
            ["audit-rules", "--kibana-url", "https://kbn", "--kibana-api-key", "KEY"]
        )
        self.assertEqual(args.command, "audit-rules")
        self.assertEqual(args.kibana_url, "https://kbn")
        self.assertEqual(args.kibana_api_key, "KEY")
        self.assertEqual(args.per_page, 100)
        self.assertEqual(args.max_pages, 20)
        self.assertFalse(args.disable_enabled)

    def test_run_audit_rules_returns_one_when_enabled_rules_remain(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            disable_enabled=False,
        )
        with patch.object(
            app_cli,
            "audit_migrated_rules",
            return_value={
                "enabled_migrated_rule_ids": ["rule-1"],
                "remediation": {"failed_rule_ids": []},
            },
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(app_cli._run_audit_rules(args), 1)

    def test_run_audit_rules_returns_two_on_errors(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            disable_enabled=False,
        )
        with patch.object(
            app_cli,
            "audit_migrated_rules",
            return_value={
                "enabled_migrated_rule_ids": [],
                "remediation": {"failed_rule_ids": []},
                "errors": ["connection refused"],
            },
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(app_cli._run_audit_rules(args), 2)

    def test_run_audit_rules_returns_zero_when_clean(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            disable_enabled=True,
        )
        with patch.object(
            app_cli,
            "audit_migrated_rules",
            return_value={
                "enabled_migrated_rule_ids": ["rule-1"],
                "remediation": {"failed_rule_ids": []},
            },
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(app_cli._run_audit_rules(args), 0)

    def test_run_audit_rules_threads_tls_verify(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            disable_enabled=False,
            ca_cert="/tmp/ca.pem",
            insecure=False,
        )
        with patch.object(
            app_cli,
            "audit_migrated_rules",
            return_value={
                "enabled_migrated_rule_ids": [],
                "remediation": {"failed_rule_ids": []},
            },
        ) as mock_audit, redirect_stdout(io.StringIO()):
            self.assertEqual(app_cli._run_audit_rules(args), 0)

        self.assertEqual(mock_audit.call_args.kwargs.get("verify"), "/tmp/ca.pem")


class VerifyAlertRulesSubcommandTests(unittest.TestCase):
    def test_parser_requires_comparison_and_defaults(self):
        parser = app_cli._build_parser()
        args = parser.parse_args(
            [
                "verify-alert-rules",
                "--comparison", "out/alerts/alert_comparison_results.json",
                "--kibana-url", "https://kbn",
                "--kibana-api-key", "KEY",
            ]
        )
        self.assertEqual(args.command, "verify-alert-rules")
        self.assertEqual(args.comparison_paths, ["out/alerts/alert_comparison_results.json"])
        self.assertEqual(args.limit, 0)
        self.assertFalse(args.keep_rules)

    def test_run_verify_alert_rules_returns_two_when_no_payloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            comparison = Path(tmpdir) / "comparison.json"
            comparison.write_text(json.dumps({"rows": []}), encoding="utf-8")
            args = SimpleNamespace(
                comparison_paths=[str(comparison)],
                kibana_url="https://kbn",
                kibana_api_key="KEY",
                space_id="",
                limit=0,
                keep_rules=False,
                name_prefix="[verification ",
            )
            with patch.object(app_cli, "collect_emitted_rule_payloads", return_value=[]), redirect_stdout(io.StringIO()):
                self.assertEqual(app_cli._run_verify_alert_rules(args), 2)

    def test_run_verify_alert_rules_delegates_and_reports_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            comparison = Path(tmpdir) / "comparison.json"
            comparison.write_text(json.dumps({"rows": []}), encoding="utf-8")
            args = SimpleNamespace(
                comparison_paths=[str(comparison)],
                kibana_url="https://kbn",
                kibana_api_key="KEY",
                space_id="",
                limit=0,
                keep_rules=False,
                name_prefix="[verification ",
                ca_cert="/tmp/ca.pem",
                insecure=False,
            )
            clean_summary = {
                "candidate_payloads": 1,
                "created_rules": 1,
                "creation_errors": [],
                "enabled_true_in_create_response": [],
                "enabled_true_in_rule_listing": [],
                "preflight": {},
                "marker": "m",
                "keep_rules": False,
                "cleanup": {"deleted_count": 1, "failed_rule_ids": []},
            }
            with (
                patch.object(app_cli, "collect_emitted_rule_payloads", return_value=[{"payload": {}, "alert_id": "a", "name": "n"}]),
                patch.object(app_cli, "verify_emitted_rule_uploads", return_value=clean_summary) as mock_verify,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(app_cli._run_verify_alert_rules(args), 0)
            mock_verify.assert_called_once()
            self.assertEqual(mock_verify.call_args.kwargs.get("verify"), "/tmp/ca.pem")

    def test_run_verify_alert_rules_returns_two_on_preflight_unreachable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            comparison = Path(tmpdir) / "comparison.json"
            comparison.write_text(json.dumps({"rows": []}), encoding="utf-8")
            args = SimpleNamespace(
                comparison_paths=[str(comparison)],
                kibana_url="https://kbn",
                kibana_api_key="KEY",
                space_id="",
                limit=0,
                keep_rules=False,
                name_prefix="[verification ",
            )
            with (
                patch.object(app_cli, "collect_emitted_rule_payloads", return_value=[{"payload": {}, "alert_id": "a", "name": "n"}]),
                patch.object(
                    app_cli,
                    "verify_emitted_rule_uploads",
                    return_value={
                        "candidate_payloads": 1,
                        "created_rules": 0,
                        "creation_errors": [],
                        "enabled_true_in_create_response": [],
                        "enabled_true_in_rule_listing": [],
                        "preflight": {},
                        "marker": "",
                        "keep_rules": False,
                        "cleanup": {"deleted_count": 0, "failed_rule_ids": []},
                        "error": "preflight_unreachable",
                    },
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(app_cli._run_verify_alert_rules(args), 2)


class DeleteRulesSubcommandTests(unittest.TestCase):
    def test_parser_defaults_to_dry_run(self):
        parser = app_cli._build_parser()
        args = parser.parse_args(
            ["delete-rules", "--kibana-url", "https://kbn", "--kibana-api-key", "KEY"]
        )
        self.assertEqual(args.command, "delete-rules")
        self.assertEqual(args.kibana_url, "https://kbn")
        self.assertEqual(args.kibana_api_key, "KEY")
        self.assertEqual(args.per_page, 100)
        self.assertEqual(args.max_pages, 20)
        self.assertFalse(args.confirm)

    def test_dry_run_lists_but_does_not_delete(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            confirm=False,
        )
        with (
            patch.object(
                app_cli,
                "audit_migrated_rules",
                return_value={
                    "migrated_rule_ids": ["rule-1", "rule-2"],
                    "errors": [],
                },
            ),
            patch.object(app_cli, "cleanup_rules") as mock_cleanup,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(app_cli._run_delete_rules(args), 0)
        mock_cleanup.assert_not_called()

    def test_dry_run_prints_candidate_rule_ids(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            confirm=False,
        )
        stdout = io.StringIO()
        with (
            patch.object(
                app_cli,
                "audit_migrated_rules",
                return_value={"migrated_rule_ids": ["rule-1", "rule-2"], "errors": []},
            ),
            redirect_stdout(stdout),
        ):
            self.assertEqual(app_cli._run_delete_rules(args), 0)

        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["would_delete_count"], 2)
        self.assertEqual(payload["would_delete_rule_ids"], ["rule-1", "rule-2"])

    def test_confirm_deletes_migrated_rules(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            confirm=True,
        )
        with (
            patch.object(
                app_cli,
                "audit_migrated_rules",
                return_value={
                    "migrated_rule_ids": ["rule-1", "rule-2"],
                    "errors": [],
                },
            ),
            patch.object(
                app_cli,
                "cleanup_rules",
                return_value={"deleted_count": 2, "failed_rule_ids": []},
            ) as mock_cleanup,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(app_cli._run_delete_rules(args), 0)
        mock_cleanup.assert_called_once()
        self.assertEqual(mock_cleanup.call_args.args[1], ["rule-1", "rule-2"])

    def test_confirm_with_no_rules_is_clean_noop(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            confirm=True,
        )
        with (
            patch.object(
                app_cli,
                "audit_migrated_rules",
                return_value={"migrated_rule_ids": [], "errors": []},
            ),
            patch.object(
                app_cli,
                "cleanup_rules",
                return_value={"deleted_count": 0, "failed_rule_ids": []},
            ) as mock_cleanup,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(app_cli._run_delete_rules(args), 0)
        mock_cleanup.assert_called_once()
        self.assertEqual(mock_cleanup.call_args.args[1], [])

    def test_confirm_returns_one_when_a_delete_fails(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            confirm=True,
        )
        with (
            patch.object(
                app_cli,
                "audit_migrated_rules",
                return_value={"migrated_rule_ids": ["rule-1"], "errors": []},
            ),
            patch.object(
                app_cli,
                "cleanup_rules",
                return_value={"deleted_count": 0, "failed_rule_ids": ["rule-1"]},
            ),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(app_cli._run_delete_rules(args), 1)

    def test_confirm_refuses_to_delete_when_listing_is_truncated(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=1,
            max_pages=2,
            confirm=True,
        )
        stdout = io.StringIO()
        with (
            patch.object(
                app_cli,
                "audit_migrated_rules",
                return_value={
                    "migrated_rule_ids": ["rule-1", "rule-2"],
                    "errors": [],
                    "listing_truncated": True,
                    "listing_warning": "Increase --max-pages to inspect every rule.",
                },
            ),
            patch.object(app_cli, "cleanup_rules") as mock_cleanup,
            redirect_stdout(stdout),
        ):
            self.assertEqual(app_cli._run_delete_rules(args), 2)
        mock_cleanup.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["listing_truncated"])
        self.assertIn("Increase --max-pages", payload["listing_warning"])

    def test_returns_two_on_listing_errors(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            confirm=True,
        )
        with (
            patch.object(
                app_cli,
                "audit_migrated_rules",
                return_value={"migrated_rule_ids": [], "errors": ["connection refused"]},
            ),
            patch.object(app_cli, "cleanup_rules") as mock_cleanup,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(app_cli._run_delete_rules(args), 2)
        mock_cleanup.assert_not_called()

    def test_forwards_scope_and_pagination_args_to_listing_and_cleanup(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="ops",
            per_page=25,
            max_pages=7,
            confirm=True,
        )
        with (
            patch.object(
                app_cli,
                "audit_migrated_rules",
                return_value={"migrated_rule_ids": ["rule-1"], "errors": []},
            ) as mock_audit,
            patch.object(
                app_cli,
                "cleanup_rules",
                return_value={"deleted_count": 1, "failed_rule_ids": []},
            ) as mock_cleanup,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(app_cli._run_delete_rules(args), 0)

        self.assertEqual(mock_audit.call_args.kwargs["api_key"], "KEY")
        self.assertEqual(mock_audit.call_args.kwargs["space_id"], "ops")
        self.assertEqual(mock_audit.call_args.kwargs["per_page"], 25)
        self.assertEqual(mock_audit.call_args.kwargs["max_pages"], 7)
        self.assertEqual(mock_cleanup.call_args.kwargs["api_key"], "KEY")
        self.assertEqual(mock_cleanup.call_args.kwargs["space_id"], "ops")

    def test_threads_tls_verify_to_both_calls(self):
        args = SimpleNamespace(
            kibana_url="https://kbn",
            kibana_api_key="KEY",
            space_id="",
            per_page=100,
            max_pages=20,
            confirm=True,
            ca_cert="/tmp/ca.pem",
            insecure=False,
        )
        with (
            patch.object(
                app_cli,
                "audit_migrated_rules",
                return_value={"migrated_rule_ids": ["rule-1"], "errors": []},
            ) as mock_audit,
            patch.object(
                app_cli,
                "cleanup_rules",
                return_value={"deleted_count": 1, "failed_rule_ids": []},
            ) as mock_cleanup,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(app_cli._run_delete_rules(args), 0)
        self.assertEqual(mock_audit.call_args.kwargs.get("verify"), "/tmp/ca.pem")
        self.assertEqual(mock_cleanup.call_args.kwargs.get("verify"), "/tmp/ca.pem")

    def test_main_dispatches_delete_rules(self):
        with (
            patch.object(app_cli, "_run_delete_rules", return_value=0) as mock_run,
            self.assertRaises(SystemExit) as cm,
        ):
            app_cli.main(["delete-rules", "--kibana-url", "https://kbn", "--kibana-api-key", "KEY"])
        self.assertEqual(cm.exception.code, 0)
        mock_run.assert_called_once()


class SkillCommandHelpTests(unittest.TestCase):
    def _help_text(self, command: str) -> str:
        parser = app_cli._build_parser()
        stdout = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(stdout):
            parser.parse_args([command, "--help"])
        return stdout.getvalue()

    def test_schema_report_help_mentions_artifact_dir(self):
        self.assertIn("--artifact-dir", self._help_text("schema-report"))

    def test_audit_rules_help_mentions_disable_enabled(self):
        self.assertIn("--disable-enabled", self._help_text("audit-rules"))

    def test_delete_rules_help_mentions_confirm(self):
        help_text = self._help_text("delete-rules")
        self.assertIn("--confirm", help_text)

    def test_verify_alert_rules_help_mentions_comparison(self):
        help_text = self._help_text("verify-alert-rules")
        normalized_help = help_text.replace("-\n", "").replace("\n", " ")
        self.assertIn("--comparison", help_text)
        self.assertIn("alerts/monitor_comparison_results.json", normalized_help)


class ListSamplesSubcommandTests(unittest.TestCase):
    def test_list_samples_prints_json_catalog(self):
        from observability_migration.app import cli
        from observability_migration.sample_dashboards.catalog import list_samples

        parser = cli._build_parser()
        args = parser.parse_args(["list-samples"])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = cli._run_list_samples(args)

        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertIsInstance(payload, list)
        self.assertTrue(payload)
        first = payload[0]
        for key in (
            "id",
            "source",
            "title",
            "description",
            "input_dir",
            "expected_unsupported",
            "run",
        ):
            self.assertIn(key, first)
        self.assertTrue(Path(first["input_dir"]).is_absolute())
        self.assertTrue(Path(first["input_dir"]).is_dir())
        self.assertEqual(len(payload), len(list_samples()))
        self.assertEqual({e["source"] for e in payload}, {s.source for s in list_samples()})
        self.assertIn("obs-migrate migrate", first["run"])
        self.assertIn("--input-mode files", first["run"])
        self.assertIn(first["input_dir"], first["run"])

    def test_main_dispatches_list_samples(self):
        from observability_migration.app import cli

        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
            cli.main(["list-samples"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertTrue(json.loads(stdout.getvalue()))


class SeedSampleDataSubcommandTests(unittest.TestCase):
    def test_main_dispatches_seed_and_threads_args_and_tls(self):
        from observability_migration.app import cli

        captured = {}

        def fake_make_es_request(es_url, api_key, *, verify=True, timeout=120):
            captured["es_url"] = es_url
            captured["api_key"] = api_key
            captured["verify"] = verify
            return "REQ"

        class FakeSummary:
            ok = 5
            errors = 0
            docs_per_stream = {"logs-generic-default": 5}
            error_samples: list = []

        def fake_seed(artifact_dirs, request, **kwargs):
            captured["artifact_dirs"] = [str(p) for p in artifact_dirs]
            captured["request"] = request
            captured["kwargs"] = kwargs
            return FakeSummary()

        with tempfile.TemporaryDirectory() as artifact_dir:
            with (
                mock.patch.object(cli, "make_es_request", side_effect=fake_make_es_request),
                mock.patch.object(cli, "seed_sample_data", side_effect=fake_seed),
                self.assertRaises(SystemExit) as ctx,
            ):
                cli.main([
                    "seed-sample-data",
                    "--artifact-dir", artifact_dir,
                    "--es-url", "https://es.test",
                    "--api-key", "k",
                    "--insecure",
                    "--max-combinations", "3",
                ])

            self.assertEqual(ctx.exception.code, 0)
            self.assertEqual(captured["es_url"], "https://es.test")
            self.assertEqual(captured["verify"], False)  # --insecure
            self.assertEqual(captured["request"], "REQ")
            self.assertEqual(captured["artifact_dirs"], [artifact_dir])
            self.assertEqual(captured["kwargs"]["max_combinations"], 3)

    def test_missing_es_url_returns_2(self):
        from observability_migration.app import cli

        with tempfile.TemporaryDirectory() as artifact_dir, self.assertRaises(SystemExit) as ctx:
            cli.main(["seed-sample-data", "--artifact-dir", artifact_dir, "--api-key", "k"])
        self.assertEqual(ctx.exception.code, 2)


class RemoveSampleDataSubcommandTests(unittest.TestCase):
    def _patch(self, cli, captured):
        def fake_make_es_request(es_url, api_key, *, verify=True, timeout=120):
            captured["verify"] = verify
            return "REQ"

        class FakeRemoveSummary:
            def __init__(self, dry_run):
                self.dry_run = dry_run
                self.deleted_streams = ["logs-generic-default"]
                self.skipped_not_owned = ["metrics-foreign-default"]
                self.deleted_templates = ["telemetry-data-logs-generic-default"]
                self.errors: list = []

        def fake_remove(artifact_dirs, request, *, dry_run=True):
            captured["artifact_dirs"] = [str(p) for p in artifact_dirs]
            captured["request"] = request
            captured["dry_run"] = dry_run
            return FakeRemoveSummary(dry_run)

        return (
            mock.patch.object(cli, "make_es_request", side_effect=fake_make_es_request),
            mock.patch.object(cli, "remove_sample_data", side_effect=fake_remove),
        )

    def test_dry_run_is_default(self):
        from observability_migration.app import cli

        captured = {}
        p1, p2 = self._patch(cli, captured)
        with tempfile.TemporaryDirectory() as artifact_dir:
            with p1, p2, self.assertRaises(SystemExit) as ctx:
                cli.main([
                    "remove-sample-data",
                    "--artifact-dir", artifact_dir,
                    "--es-url", "https://es.test",
                    "--api-key", "k",
                ])
            self.assertEqual(ctx.exception.code, 0)
            self.assertTrue(captured["dry_run"])  # no --confirm => dry run
            self.assertEqual(captured["request"], "REQ")
            self.assertEqual(captured["artifact_dirs"], [artifact_dir])

    def test_confirm_disables_dry_run_and_threads_tls(self):
        from observability_migration.app import cli

        captured = {}
        p1, p2 = self._patch(cli, captured)
        with tempfile.TemporaryDirectory() as artifact_dir:
            with p1, p2, self.assertRaises(SystemExit) as ctx:
                cli.main([
                    "remove-sample-data",
                    "--artifact-dir", artifact_dir,
                    "--es-url", "https://es.test",
                    "--api-key", "k",
                    "--confirm",
                    "--insecure",
                ])
            self.assertEqual(ctx.exception.code, 0)
            self.assertFalse(captured["dry_run"])  # --confirm => real delete
            self.assertEqual(captured["verify"], False)  # --insecure

    def test_missing_creds_returns_2(self):
        from observability_migration.app import cli

        with tempfile.TemporaryDirectory() as artifact_dir, self.assertRaises(SystemExit) as ctx:
            cli.main(["remove-sample-data", "--artifact-dir", artifact_dir, "--es-url", "https://es.test"])
        self.assertEqual(ctx.exception.code, 2)


class CompareSubcommandTests(unittest.TestCase):
    def _artifact_dir(self, tmp, packets):
        d = Path(tmp) / "dashboards"
        d.mkdir(parents=True)
        (d / "verification_packets.json").write_text(json.dumps({"summary": {}, "packets": packets}), encoding="utf-8")
        return d

    def test_compare_threads_tls_and_writes_report(self):
        from observability_migration.app import cli

        captured = {}

        def fake_make_es_request(es_url, api_key, *, verify=True, timeout=120):
            captured["verify"] = verify
            return "REQ"

        class V:  # fake Comparison
            def __init__(self, verdict):
                self._v = verdict
                self.max_relative_error = 0.0
                self.compared_points = 3
                self.notes = []
                self.skipped_reason = ""
                self.fail_reason = ""
                self.translated_error = ""
                self.native_error = ""
                self.native_series = 1
                self.translated_series = 1
                self.common_series = 1
            def verdict(self):
                return self._v

        with tempfile.TemporaryDirectory() as tmp:
            art = self._artifact_dir(tmp, [
                {"dashboard": "D", "panel": "P1", "source_language": "promql",
                 "source_query": "go_goroutines", "translated_query": "TS metrics-*", "semantic_gate": "Green"},
            ])
            out = Path(tmp) / "comparison_report.json"
            with (
                mock.patch.object(cli, "make_es_request", side_effect=fake_make_es_request),
                mock.patch.object(cli, "native_promql_available", return_value=True),
                mock.patch.object(cli, "compare_panel", return_value=V("STRICT_PASS")),
                self.assertRaises(SystemExit) as ctx,
            ):
                cli.main(["compare", "--artifact-dir", str(art), "--es-url", "https://es.test",
                          "--api-key", "k", "--insecure", "--report-out", str(out)])
            self.assertEqual(ctx.exception.code, 0)
            self.assertFalse(captured["verify"])
            report = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["panels"], 1)
            self.assertEqual(report["panels"][0]["verdict"], "STRICT_PASS")

    def test_compare_structural_only_when_no_oracle(self):
        from observability_migration.app import cli

        with tempfile.TemporaryDirectory() as tmp:
            art = self._artifact_dir(tmp, [
                {"dashboard": "D", "panel": "P1", "source_language": "datadog", "semantic_gate": "Green",
                 "source_query": "", "translated_query": "FROM metrics-*"},
            ])
            with (
                mock.patch.object(cli, "make_es_request", return_value="REQ"),
                mock.patch.object(cli, "native_promql_available", return_value=False),
                self.assertRaises(SystemExit) as ctx,
            ):
                cli.main(["compare", "--artifact-dir", str(art), "--es-url", "https://es.test", "--api-key", "k",
                          "--report-out", str(Path(tmp) / "comparison_report.json")])
            self.assertEqual(ctx.exception.code, 0)  # structural-only never fails the run

    def test_compare_fail_verdict_exits_1(self):
        from observability_migration.app import cli

        class V:
            max_relative_error = 0.9
            compared_points = 3
            notes: list = []
            skipped_reason = ""
            fail_reason = ""
            translated_error = ""
            native_error = ""
            native_series = 1
            translated_series = 1
            common_series = 1
            def verdict(self):
                return "FAIL"

        with tempfile.TemporaryDirectory() as tmp:
            art = self._artifact_dir(tmp, [
                {"dashboard": "D", "panel": "P1", "source_language": "promql",
                 "source_query": "x", "translated_query": "TS metrics-*", "semantic_gate": "Red"},
            ])
            with (
                mock.patch.object(cli, "make_es_request", return_value="REQ"),
                mock.patch.object(cli, "native_promql_available", return_value=True),
                mock.patch.object(cli, "compare_panel", return_value=V()),
                self.assertRaises(SystemExit) as ctx,
            ):
                cli.main(["compare", "--artifact-dir", str(art), "--es-url", "https://es.test", "--api-key", "k",
                          "--report-out", str(Path(tmp) / "comparison_report.json")])
            self.assertEqual(ctx.exception.code, 1)

    def test_compare_fail_row_carries_diagnostics(self):
        # A FAIL with an empty reason and no series counts is undebuggable;
        # the report must carry the comparator's fail_reason, the
        # native/translated/common series counts, and the notes.
        from observability_migration.app import cli

        class V:
            max_relative_error = 0.0
            compared_points = 0
            notes = ["native re-aggregated 9->1 series (sum) to match translated label subset"]
            skipped_reason = ""
            fail_reason = "series keys did not align (native 9, translated 1 series)"
            translated_error = ""
            native_error = ""
            native_series = 9
            translated_series = 1
            common_series = 0
            def verdict(self):
                return "FAIL"

        with tempfile.TemporaryDirectory() as tmp:
            art = self._artifact_dir(tmp, [
                {"dashboard": "D", "panel": "P1", "source_language": "promql",
                 "source_query": "x", "translated_query": "TS metrics-*", "semantic_gate": "Red"},
            ])
            out = Path(tmp) / "comparison_report.json"
            with (
                mock.patch.object(cli, "make_es_request", return_value="REQ"),
                mock.patch.object(cli, "native_promql_available", return_value=True),
                mock.patch.object(cli, "compare_panel", return_value=V()),
                self.assertRaises(SystemExit) as ctx,
            ):
                cli.main(["compare", "--artifact-dir", str(art), "--es-url", "https://es.test", "--api-key", "k",
                          "--report-out", str(out)])
            self.assertEqual(ctx.exception.code, 1)
            row = json.loads(out.read_text(encoding="utf-8"))["panels"][0]
            self.assertEqual(row["reason"], "series keys did not align (native 9, translated 1 series)")
            self.assertEqual(row["native_series"], 9)
            self.assertEqual(row["translated_series"], 1)
            self.assertEqual(row["common_series"], 0)
            self.assertEqual(row["notes"], V.notes)

    def test_compare_multi_target_provenance_emits_per_target_rows(self):
        # A merged multi-target panel with per-target provenance must be
        # verified one target at a time (each sub-query against its own
        # output column) instead of a single multi-query SKIP.
        from observability_migration.app import cli

        calls = []

        class V:
            max_relative_error = 0.0
            compared_points = 3
            notes: list = []
            skipped_reason = ""
            fail_reason = ""
            translated_error = ""
            native_error = ""
            native_series = 1
            translated_series = 1
            common_series = 1
            def verdict(self):
                return "STRICT_PASS"

        def fake_compare(request, **kwargs):
            calls.append(kwargs)
            return V()

        merged_esql = ("FROM metrics-* | STATS cpu_usage = AVG(cpu_usage), "
                       "memory_usage = AVG(memory_usage) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)")
        with tempfile.TemporaryDirectory() as tmp:
            art = self._artifact_dir(tmp, [
                {"dashboard": "D", "panel": "P1", "source_language": "promql",
                 "source_query": "cpu_usage ||| memory_usage",
                 "translated_query": merged_esql, "semantic_gate": "Green",
                 "query_ir": {"metadata": {"collapsed_targets": [
                     {"ref_id": "A", "source_expr": "cpu_usage", "value_column": "cpu_usage"},
                     {"ref_id": "B", "source_expr": "memory_usage", "value_column": "memory_usage"},
                 ]}}},
            ])
            out = Path(tmp) / "comparison_report.json"
            with (
                mock.patch.object(cli, "make_es_request", return_value="REQ"),
                mock.patch.object(cli, "native_promql_available", return_value=True),
                mock.patch.object(cli, "compare_panel", side_effect=fake_compare),
                self.assertRaises(SystemExit) as ctx,
            ):
                cli.main(["compare", "--artifact-dir", str(art), "--es-url", "https://es.test", "--api-key", "k",
                          "--report-out", str(out)])
            self.assertEqual(ctx.exception.code, 0)
            rows = json.loads(out.read_text(encoding="utf-8"))["panels"]
            self.assertEqual(len(rows), 2)
            self.assertEqual([r.get("target") for r in rows], ["A", "B"])
            self.assertTrue(all(r["verdict"] == "STRICT_PASS" for r in rows))
            self.assertEqual([c["source_query"] for c in calls], ["cpu_usage", "memory_usage"])
            self.assertEqual([c["translated_value_column"] for c in calls], ["cpu_usage", "memory_usage"])
            self.assertEqual(calls[0]["translated_ignore_columns"], frozenset({"memory_usage"}))
            self.assertEqual(calls[1]["translated_ignore_columns"], frozenset({"cpu_usage"}))

    def test_compare_negated_target_negates_native_reference(self):
        # Grafana draws some targets below the axis (negate_result); the merged
        # ES|QL emits ``-1 * expr`` for them. The oracle must negate its native
        # reference too, or every such target reads as a 200% mismatch.
        from observability_migration.app import cli

        calls = []

        class V:
            max_relative_error = 0.0
            compared_points = 3
            notes: list = []
            skipped_reason = ""
            fail_reason = ""
            translated_error = ""
            native_error = ""
            native_series = 1
            translated_series = 1
            common_series = 1
            def verdict(self):
                return "STRICT_PASS"

        def fake_compare(request, **kwargs):
            calls.append(kwargs)
            return V()

        with tempfile.TemporaryDirectory() as tmp:
            art = self._artifact_dir(tmp, [
                {"dashboard": "D", "panel": "Net", "source_language": "promql",
                 "source_query": "rx_total ||| tx_total",
                 "translated_query": "FROM metrics-* | STATS rx = SUM(rx_total), tx = SUM(tx_total) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
                 "semantic_gate": "Green",
                 "query_ir": {"metadata": {"collapsed_targets": [
                     {"ref_id": "A", "source_expr": "rx_total", "value_column": "rx"},
                     {"ref_id": "B", "source_expr": "tx_total", "value_column": "tx", "negated": True},
                 ]}}},
            ])
            with (
                mock.patch.object(cli, "make_es_request", return_value="REQ"),
                mock.patch.object(cli, "native_promql_available", return_value=True),
                mock.patch.object(cli, "compare_panel", side_effect=fake_compare),
                self.assertRaises(SystemExit),
            ):
                cli.main(["compare", "--artifact-dir", str(art), "--es-url", "https://es.test", "--api-key", "k",
                          "--report-out", str(Path(tmp) / "comparison_report.json")])
            self.assertEqual([c["source_query"] for c in calls], ["rx_total", "-(tx_total)"])

    def test_compare_same_metric_provenance_emits_label_scoped_rows(self):
        # Same-metric collapsed panels: per-target rows scoped by label value;
        # targets whose distinguishing matcher cannot be replayed client-side
        # surface as SKIP with the recorded reason, not silently vanish.
        from observability_migration.app import cli

        calls = []

        class V:
            max_relative_error = 0.0
            compared_points = 3
            notes: list = []
            skipped_reason = ""
            fail_reason = ""
            translated_error = ""
            native_error = ""
            native_series = 1
            translated_series = 1
            common_series = 1
            def verdict(self):
                return "STRICT_PASS"

        def fake_compare(request, **kwargs):
            calls.append(kwargs)
            return V()

        with tempfile.TemporaryDirectory() as tmp:
            art = self._artifact_dir(tmp, [
                {"dashboard": "D", "panel": "Units", "source_language": "promql",
                 "source_query": 'm{state="active"} ||| m{state=~"f.*"}',
                 "translated_query": "TS metrics-* | STATS v = AVG(m) BY time_bucket = TBUCKET(5 minute), state",
                 "semantic_gate": "Green",
                 "query_ir": {"metadata": {"collapsed_targets": [
                     {"ref_id": "A", "source_expr": 'm{state="active"}',
                      "label_column": "state", "label_value": "active"},
                     {"ref_id": "B", "source_expr": 'm{state=~"f.*"}',
                      "unsupported_reason": "distinguishing matcher is non-equality or compound; per-target comparison is not supported"},
                 ]}}},
            ])
            out = Path(tmp) / "comparison_report.json"
            with (
                mock.patch.object(cli, "make_es_request", return_value="REQ"),
                mock.patch.object(cli, "native_promql_available", return_value=True),
                mock.patch.object(cli, "compare_panel", side_effect=fake_compare),
                self.assertRaises(SystemExit) as ctx,
            ):
                cli.main(["compare", "--artifact-dir", str(art), "--es-url", "https://es.test", "--api-key", "k",
                          "--report-out", str(out)])
            self.assertEqual(ctx.exception.code, 0)
            rows = json.loads(out.read_text(encoding="utf-8"))["panels"]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["target"], "A")
            self.assertEqual(rows[0]["verdict"], "STRICT_PASS")
            self.assertEqual(rows[1]["target"], "B")
            self.assertEqual(rows[1]["verdict"], "SKIP")
            self.assertIn("non-equality", rows[1]["reason"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["source_query"], 'm{state="active"}')
            self.assertEqual(calls[0]["translated_label_filter"], ("state", "active"))

    def test_compare_surfaces_live_source_comparison_verdicts(self):
        # Packets produced with --source-execution --validate carry live
        # source-vs-target verdicts; compare must surface them as their own
        # mode instead of hiding them behind STRUCTURAL, and material drift
        # must fail the run like a numeric FAIL would.
        from observability_migration.app import cli

        def pkt(panel, status, reason="", diff="", counterexamples=None):
            return {"dashboard": "DD", "panel": panel, "source_language": "datadog_metric",
                    "source_query": "", "translated_query": "FROM metrics-*",
                    "semantic_gate": "Green",
                    "comparison": {"status": status, "comparator_family": "single_value",
                                   "reason": reason, "diff_summary": diff,
                                   "tolerance_used": {}, "counterexamples": counterexamples or []}}

        with tempfile.TemporaryDirectory() as tmp:
            art = self._artifact_dir(tmp, [
                pkt("ok", "within_tolerance", reason="matched", diff="0.1% drift"),
                pkt("warn", "drift", reason="above tolerance", diff="7% drift"),
                pkt("bad", "material_drift", reason="way off", diff="60% drift"),
                pkt("broken", "target_broken", reason="target failed",
                    counterexamples=["Unknown column [datadog_apm_host]"]),
                pkt("plain", "not_attempted", reason="Live comparison was not requested"),
            ])
            out = Path(tmp) / "comparison_report.json"
            with (
                mock.patch.object(cli, "make_es_request", return_value="REQ"),
                mock.patch.object(cli, "native_promql_available", return_value=False),
                self.assertRaises(SystemExit) as ctx,
            ):
                cli.main(["compare", "--artifact-dir", str(art), "--es-url", "https://es.test", "--api-key", "k",
                          "--report-out", str(out)])
            self.assertEqual(ctx.exception.code, 1)  # material_drift fails the run
            rows = {r["panel"]: r for r in json.loads(out.read_text(encoding="utf-8"))["panels"]}
            self.assertEqual(rows["ok"]["mode"], "live_source")
            self.assertEqual(rows["ok"]["verdict"], "SOURCE_PASS")
            self.assertEqual(rows["warn"]["verdict"], "SOURCE_DRIFT")
            self.assertEqual(rows["bad"]["verdict"], "SOURCE_FAIL")
            self.assertEqual(rows["broken"]["verdict"], "ERROR")
            self.assertIn("Unknown column", rows["broken"]["reason"])
            self.assertEqual(rows["plain"]["mode"], "structural")
            self.assertEqual(rows["plain"]["verdict"], "STRUCTURAL")

    def test_compare_invalid_packets_json_exits_2(self):
        from observability_migration.app import cli

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "dashboards"
            d.mkdir(parents=True)
            (d / "verification_packets.json").write_text("{not valid json", encoding="utf-8")
            with (
                mock.patch.object(cli, "make_es_request", return_value="REQ"),
                self.assertRaises(SystemExit) as ctx,
            ):
                cli.main(["compare", "--artifact-dir", str(d), "--es-url", "https://es.test", "--api-key", "k",
                          "--report-out", str(Path(tmp) / "comparison_report.json")])
            self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
