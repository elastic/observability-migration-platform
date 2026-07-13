# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Matrix coverage for every ``obs-migrate`` subcommand + metrics-index wiring.

Layers
------
1. **Help matrix** — every top-level subcommand (and every ``cluster`` action)
   accepts ``--help`` and prints a non-empty usage string.
2. **Parse matrix** — each subcommand accepts a minimal valid argv (no network).
3. **Migrate index forwarding** — unified ``migrate`` forwards divergent
   ``--data-view`` / ``--esql-index`` into the Grafana adapter argv.
4. **Migrate emission** — a one-panel dashboard migrated through the Grafana
   CLI with divergent flags emits queries against ``--esql-index`` (native
   PROMQL and ES|QL), not the broader ``--data-view``.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from observability_migration.adapters.source.grafana import cli as grafana_cli
from observability_migration.app import cli as app_cli

# Keep in lockstep with ``obs-migrate --help`` positional choices.
OBS_MIGRATE_SUBCOMMANDS = (
    "migrate",
    "doctor",
    "compile",
    "upload",
    "cluster",
    "verify-panels",
    "verify-visual",
    "extensions",
    "schema-report",
    "audit-rules",
    "delete-rules",
    "verify-alert-rules",
    "list-samples",
    "seed-sample-data",
    "remove-sample-data",
    "compare",
    "verify",
)

CLUSTER_ACTIONS = (
    "list-dashboards",
    "ensure-data-views",
    "delete-dashboards",
    "detect-serverless",
)

CONCRETE_INDEX = "metrics-alloy.prometheus-default"
BROAD_DATA_VIEW = "metrics-*"


class ObsMigrateHelpMatrixTests(unittest.TestCase):
    def _capture_help(self, *argv: str) -> str:
        parser = app_cli._build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as cm:
            parser.parse_args([*argv, "--help"] if argv else ["--help"])
        self.assertIn(cm.exception.code, (0, None), argv)
        return stdout.getvalue()

    def test_root_help_lists_every_subcommand(self):
        text = self._capture_help()
        for name in OBS_MIGRATE_SUBCOMMANDS:
            self.assertIn(name, text, f"root help missing subcommand {name}")

    def test_every_subcommand_help_exits_clean(self):
        parser = app_cli._build_parser()
        for name in OBS_MIGRATE_SUBCOMMANDS:
            with self.subTest(command=name):
                stdout = io.StringIO()
                with redirect_stdout(stdout), self.assertRaises(SystemExit) as cm:
                    parser.parse_args([name, "--help"])
                self.assertIn(cm.exception.code, (0, None), name)
                help_text = stdout.getvalue()
                self.assertIn("usage:", help_text.lower(), name)
                self.assertGreater(len(help_text), 40, name)

    def test_every_cluster_action_help_exits_clean(self):
        parser = app_cli._build_parser()
        for action in CLUSTER_ACTIONS:
            with self.subTest(action=action):
                stdout = io.StringIO()
                with redirect_stdout(stdout), self.assertRaises(SystemExit) as cm:
                    parser.parse_args(["cluster", action, "--help"])
                # Some argparse versions treat `cluster ACTION --help` as action help.
                self.assertIn(cm.exception.code, (0, None), action)
                self.assertIn("usage:", stdout.getvalue().lower(), action)


class ObsMigrateParseMatrixTests(unittest.TestCase):
    """Minimal valid parse for each command — no network, no side effects."""

    def setUp(self):
        self.parser = app_cli._build_parser()

    def test_doctor_parses(self):
        self.assertEqual(self.parser.parse_args(["doctor"]).command, "doctor")

    def test_migrate_parses_divergent_data_view_and_esql_index(self):
        args = self.parser.parse_args(
            [
                "migrate",
                "--source",
                "grafana",
                "--input-mode",
                "files",
                "--input-dir",
                "/tmp/in",
                "--output-dir",
                "/tmp/out",
                "--data-view",
                BROAD_DATA_VIEW,
                "--esql-index",
                CONCRETE_INDEX,
                "--translation-mode",
                "native",
            ]
        )
        self.assertEqual(args.command, "migrate")
        self.assertEqual(args.data_view, BROAD_DATA_VIEW)
        self.assertEqual(args.esql_index, CONCRETE_INDEX)
        self.assertEqual(args.translation_mode, "native")

    def test_compile_parses(self):
        args = self.parser.parse_args(
            ["compile", "--yaml-dir", "/tmp/yaml", "--output-dir", "/tmp/ndjson"]
        )
        self.assertEqual(args.command, "compile")

    def test_upload_parses_yaml_dir(self):
        args = self.parser.parse_args(
            [
                "upload",
                "--yaml-dir",
                "/tmp/yaml",
                "--kibana-url",
                "https://kb.example",
                "--kibana-api-key",
                "k",
            ]
        )
        self.assertEqual(args.command, "upload")

    def test_cluster_actions_parse(self):
        for action in CLUSTER_ACTIONS:
            with self.subTest(action=action):
                argv = ["cluster", action, "--kibana-url", "https://kb.example"]
                if action == "delete-dashboards":
                    argv.extend(["--dashboard-ids", "a,b"])
                args = self.parser.parse_args(argv)
                self.assertEqual(args.command, "cluster")
                self.assertEqual(args.action, action)

    def test_verify_panels_parses(self):
        args = self.parser.parse_args(
            [
                "verify-panels",
                "--migration-out",
                "/tmp/out",
                "--output",
                "/tmp/report.json",
            ]
        )
        self.assertEqual(args.command, "verify-panels")

    def test_verify_visual_parses(self):
        args = self.parser.parse_args(
            [
                "verify-visual",
                "--migration-out",
                "/tmp/out",
                "--grafana-uid",
                "abc",
                "--grafana-slug",
                "my-dash",
                "--kibana-url",
                "https://kb.example",
                "--kibana-dash-id",
                "dash-1",
                "--output-dir",
                "/tmp/visual-out",
                "--report",
                "/tmp/visual.json",
            ]
        )
        self.assertEqual(args.command, "verify-visual")

    def test_extensions_parses(self):
        args = self.parser.parse_args(["extensions", "--source", "grafana"])
        self.assertEqual(args.command, "extensions")

    def test_schema_report_parses(self):
        args = self.parser.parse_args(
            ["schema-report", "--artifact-dir", "/tmp/dashboards"]
        )
        self.assertEqual(args.command, "schema-report")

    def test_audit_rules_parses(self):
        args = self.parser.parse_args(
            ["audit-rules", "--kibana-url", "https://kb.example", "--kibana-api-key", "k"]
        )
        self.assertEqual(args.command, "audit-rules")

    def test_delete_rules_parses(self):
        args = self.parser.parse_args(
            ["delete-rules", "--kibana-url", "https://kb.example", "--kibana-api-key", "k"]
        )
        self.assertEqual(args.command, "delete-rules")

    def test_verify_alert_rules_parses(self):
        args = self.parser.parse_args(
            [
                "verify-alert-rules",
                "--kibana-url",
                "https://kb.example",
                "--kibana-api-key",
                "k",
                "--comparison",
                "/tmp/alerts/alert_comparison_results.json",
            ]
        )
        self.assertEqual(args.command, "verify-alert-rules")

    def test_list_samples_parses(self):
        self.assertEqual(self.parser.parse_args(["list-samples"]).command, "list-samples")

    def test_seed_sample_data_parses(self):
        args = self.parser.parse_args(
            [
                "seed-sample-data",
                "--artifact-dir",
                "/tmp/dashboards",
                "--es-url",
                "https://es.example",
                "--api-key",
                "k",
            ]
        )
        self.assertEqual(args.command, "seed-sample-data")

    def test_remove_sample_data_parses(self):
        args = self.parser.parse_args(
            [
                "remove-sample-data",
                "--artifact-dir",
                "/tmp/dashboards",
                "--es-url",
                "https://es.example",
                "--api-key",
                "k",
            ]
        )
        self.assertEqual(args.command, "remove-sample-data")

    def test_compare_parses(self):
        args = self.parser.parse_args(
            [
                "compare",
                "--artifact-dir",
                "/tmp/dashboards",
                "--es-url",
                "https://es.example",
                "--api-key",
                "k",
                "--index",
                CONCRETE_INDEX,
            ]
        )
        self.assertEqual(args.command, "compare")
        self.assertEqual(args.index, CONCRETE_INDEX)

    def test_verify_parses(self):
        args = self.parser.parse_args(
            [
                "verify",
                "--artifact-dir",
                "/tmp/dashboards",
                "--es-url",
                "https://es.example",
                "--api-key",
                "k",
            ]
        )
        self.assertEqual(args.command, "verify")


# argv → runner for ``main()`` dispatch. Exit-code commands use sys.exit(runner()).
_DISPATCH_CASES: list[tuple[list[str], str, bool]] = [
    (["migrate", "--source", "grafana", "--input-mode", "files", "--input-dir", "/tmp/in", "--output-dir", "/tmp/out"], "_run_migrate", False),
    (["doctor"], "_run_doctor", False),
    (["compile", "--yaml-dir", "/tmp/yaml", "--output-dir", "/tmp/ndjson"], "_run_compile", False),
    (["upload", "--yaml-dir", "/tmp/yaml", "--kibana-url", "https://kb.example", "--kibana-api-key", "k"], "_run_upload", False),
    (["cluster", "list-dashboards", "--kibana-url", "https://kb.example"], "_run_cluster", False),
    (["verify-panels", "--migration-out", "/tmp/out", "--output", "/tmp/r.json"], "_run_verify_panels", False),
    (
        [
            "verify-visual",
            "--migration-out",
            "/tmp/out",
            "--grafana-uid",
            "u",
            "--grafana-slug",
            "s",
            "--kibana-url",
            "https://kb.example",
            "--kibana-dash-id",
            "d",
            "--output-dir",
            "/tmp/v",
            "--report",
            "/tmp/r.json",
        ],
        "_run_verify_visual",
        False,
    ),
    (["extensions", "--source", "grafana"], "_run_extensions", False),
    (["schema-report", "--artifact-dir", "/tmp/d"], "_run_schema_report", True),
    (["audit-rules", "--kibana-url", "https://kb.example", "--kibana-api-key", "k"], "_run_audit_rules", True),
    (["delete-rules", "--kibana-url", "https://kb.example", "--kibana-api-key", "k"], "_run_delete_rules", True),
    (
        [
            "verify-alert-rules",
            "--kibana-url",
            "https://kb.example",
            "--kibana-api-key",
            "k",
            "--comparison",
            "/tmp/c.json",
        ],
        "_run_verify_alert_rules",
        True,
    ),
    (["list-samples"], "_run_list_samples", True),
    (
        [
            "seed-sample-data",
            "--artifact-dir",
            "/tmp/d",
            "--es-url",
            "https://es.example",
            "--api-key",
            "k",
        ],
        "_run_seed_sample_data",
        True,
    ),
    (
        [
            "remove-sample-data",
            "--artifact-dir",
            "/tmp/d",
            "--es-url",
            "https://es.example",
            "--api-key",
            "k",
        ],
        "_run_remove_sample_data",
        True,
    ),
    (
        [
            "compare",
            "--artifact-dir",
            "/tmp/d",
            "--es-url",
            "https://es.example",
            "--api-key",
            "k",
        ],
        "_run_compare",
        True,
    ),
    (
        [
            "verify",
            "--artifact-dir",
            "/tmp/d",
            "--es-url",
            "https://es.example",
            "--api-key",
            "k",
        ],
        "_run_verify",
        True,
    ),
]


class MainDispatchMatrixTests(unittest.TestCase):
    """Every subcommand reaches its runner through ``main()`` (no network)."""

    def test_main_dispatches_every_subcommand(self):
        covered = {argv[0] for argv, _, _ in _DISPATCH_CASES}
        self.assertEqual(
            covered,
            set(OBS_MIGRATE_SUBCOMMANDS),
            "dispatch matrix must cover every top-level subcommand",
        )
        for argv, runner_name, exits in _DISPATCH_CASES:
            with self.subTest(command=argv[0], runner=runner_name):
                with patch.object(app_cli, runner_name, return_value=0) as mock_runner:
                    if exits:
                        with self.assertRaises(SystemExit) as cm:
                            app_cli.main(argv)
                        self.assertEqual(cm.exception.code, 0)
                    else:
                        app_cli.main(argv)
                    mock_runner.assert_called_once()

    def test_main_dispatches_every_cluster_action(self):
        for action in CLUSTER_ACTIONS:
            with self.subTest(action=action):
                argv = ["cluster", action, "--kibana-url", "https://kb.example"]
                if action == "delete-dashboards":
                    argv.extend(["--dashboard-ids", "a,b"])
                with patch.object(app_cli, "_run_cluster") as mock_runner:
                    app_cli.main(argv)
                    mock_runner.assert_called_once()
                    called_args = mock_runner.call_args.args[0]
                    self.assertEqual(called_args.action, action)


class MigrateIndexForwardingTests(unittest.TestCase):
    @patch("observability_migration.adapters.source.grafana.cli.main")
    def test_unified_migrate_forwards_divergent_indexes(self, mock_main):
        args = SimpleNamespace(
            input_mode="files",
            input_dir="/tmp/in",
            output_dir="/tmp/out",
            data_view=BROAD_DATA_VIEW,
            field_profile="otel",
            assets="dashboards",
            esql_index=CONCRETE_INDEX,
            logs_index="",
            fetch_alerts=False,
            create_alert_rules=False,
            no_draft_alert_rules=False,
            grafana_token="",
            grafana_url="",
            grafana_user="",
            grafana_pass="",
            validate=False,
            upload=False,
            legacy_import=False,
            preflight=False,
            es_url="",
            es_api_key="",
            kibana_url="",
            kibana_api_key="",
            space_id="",
            rules_file=[],
            plugin=[],
            polish_metadata=False,
            dataset_filter="",
            logs_dataset_filter="",
            translation_mode="native",
            smoke_report="",
            smoke=False,
            browser_audit=False,
            capture_screenshots=False,
            smoke_output="",
            smoke_timeout=30,
            chrome_binary="",
            ca_cert="",
            insecure=False,
            alert_uids="",
            alert_folder="",
            select_folder=[],
            select_tag=[],
            select_datasource=[],
            select_team=[],
            select_updated_after="",
            select_updated_before="",
            select_starred=False,
        )
        original = list(sys.argv)
        try:
            app_cli._run_grafana_migration(args)
            forwarded = list(sys.argv)
        finally:
            sys.argv = original
        mock_main.assert_called_once_with()
        self.assertIn("--data-view", forwarded)
        self.assertIn(BROAD_DATA_VIEW, forwarded)
        self.assertIn("--esql-index", forwarded)
        self.assertIn(CONCRETE_INDEX, forwarded)
        self.assertIn("--translation-mode", forwarded)
        self.assertIn("native", forwarded)
        # Order: esql-index value is the concrete stream.
        esql_at = forwarded.index("--esql-index")
        self.assertEqual(forwarded[esql_at + 1], CONCRETE_INDEX)


class MigrateEmissionConsistencyTests(unittest.TestCase):
    """Real Grafana CLI migrate (offline) with divergent index flags."""

    def test_grafana_cli_migrate_emits_esql_index_for_native_and_fallback(self):
        dashboard = {
            "uid": "idx-consistency",
            "title": "Index Consistency Fixture",
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "title": "HTTP rate native",
                    "targets": [
                        {
                            "refId": "A",
                            "expr": "sum(rate(http_requests_total[5m]))",
                            "legendFormat": "requests",
                        }
                    ],
                    "fieldConfig": {"defaults": {}, "overrides": []},
                    "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                },
                {
                    "id": 2,
                    "type": "timeseries",
                    "title": "HTTP rate grouped esql",
                    "targets": [
                        {
                            "refId": "A",
                            "expr": "sum by (instance) (rate(http_requests_total[5m]))",
                            "legendFormat": "{{instance}}",
                        }
                    ],
                    "fieldConfig": {"defaults": {}, "overrides": []},
                    "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
                },
            ],
            "templating": {"list": []},
            "time": {"from": "now-1h", "to": "now"},
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            in_dir = root / "in"
            out_dir = root / "out"
            in_dir.mkdir()
            (in_dir / "dashboard.json").write_text(json.dumps(dashboard), encoding="utf-8")

            argv = [
                "grafana-migrate",
                "--source",
                "files",
                "--input-dir",
                str(in_dir),
                "--output-dir",
                str(out_dir),
                "--assets",
                "dashboards",
                "--data-view",
                BROAD_DATA_VIEW,
                "--esql-index",
                CONCRETE_INDEX,
                "--translation-mode",
                "native",
                "--field-profile",
                "otel",
            ]
            original = list(sys.argv)
            try:
                sys.argv = argv
                # Offline migrate: no --es-url. Should still emit indexes.
                grafana_cli.main()
            finally:
                sys.argv = original

            yaml_files = list((out_dir / "dashboards" / "yaml").glob("*.yaml"))
            self.assertTrue(yaml_files, "expected migrated YAML")
            text = yaml_files[0].read_text(encoding="utf-8")
            # Concrete stream must appear for query targets.
            self.assertIn(CONCRETE_INDEX, text)
            # Native PROMQL must not keep the broad data-view as PROMQL index.
            self.assertNotIn(f"PROMQL index={BROAD_DATA_VIEW}", text)
            self.assertIn(
                f"PROMQL index={CONCRETE_INDEX}",
                text,
                f"native panel must PROMQL against concrete index:\n{text}",
            )
            # Grouped panel falls back to ES|QL; YAML may fold the query string.
            self.assertRegex(
                text,
                rf"(TS|FROM) {re.escape(CONCRETE_INDEX)}\b",
                msg=f"grouped panel must ES|QL against concrete index:\n{text}",
            )


class DedicatedSourceCliHelpTests(unittest.TestCase):
    def test_grafana_cli_help(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as cm:
            grafana_cli.parse_args(["--help"])
        self.assertIn(cm.exception.code, (0, None))
        text = stdout.getvalue()
        self.assertIn("--data-view", text)
        self.assertIn("--esql-index", text)

    def test_datadog_cli_help(self):
        from observability_migration.adapters.source.datadog import cli as datadog_cli

        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as cm:
            datadog_cli.parse_args(["--help"])
        self.assertIn(cm.exception.code, (0, None))
        text = stdout.getvalue()
        self.assertIn("--data-view", text)
        self.assertIn("--field-profile", text)

    def test_grafana_cli_parses_divergent_indexes(self):
        args = grafana_cli.parse_args(
            [
                "--input-mode",
                "files",
                "--input-dir",
                "/tmp/in",
                "--output-dir",
                "/tmp/out",
                "--data-view",
                BROAD_DATA_VIEW,
                "--esql-index",
                CONCRETE_INDEX,
                "--translation-mode",
                "native",
            ]
        )
        self.assertEqual(args.data_view, BROAD_DATA_VIEW)
        self.assertEqual(args.esql_index, CONCRETE_INDEX)

    def test_datadog_cli_parses_data_view(self):
        from observability_migration.adapters.source.datadog import cli as datadog_cli

        args = datadog_cli.parse_args(
            [
                "--input-mode",
                "files",
                "--input-dir",
                "/tmp/in",
                "--output-dir",
                "/tmp/out",
                "--data-view",
                BROAD_DATA_VIEW,
                "--field-profile",
                "otel",
            ]
        )
        self.assertEqual(args.data_view, BROAD_DATA_VIEW)


if __name__ == "__main__":
    unittest.main()
