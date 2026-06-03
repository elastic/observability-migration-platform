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


if __name__ == "__main__":
    unittest.main()
