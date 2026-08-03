# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import argparse
import importlib
import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from observability_migration.adapters.source.datadog import cli as datadog_cli
from observability_migration.adapters.source.grafana import cli as grafana_cli
from observability_migration.app import cli as app_cli


def _parse_or_fail(parse_fn, argv):
    try:
        return parse_fn(argv)
    except SystemExit as exc:  # pragma: no cover - exercised in red phase
        raise AssertionError(f"parser rejected arguments {argv!r}") from exc


def _require_attr(obj, name):
    value = getattr(obj, name, None)
    if value is None:  # pragma: no cover - exercised in red phase
        raise AssertionError(f"{obj.__name__}.{name} is missing")
    return value


def _load_cli_contract_module():
    module_name = "observability_migration.core.cli_contract"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised in red phase
        raise AssertionError(f"{module_name} is missing") from exc


_FLAG_RE = re.compile(r"--[a-z0-9][a-z0-9-]*")


def _help_flags(parse_fn) -> set[str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            parse_fn(["--help"])
        except SystemExit:
            pass
    return set(_FLAG_RE.findall(buf.getvalue())) - {"--help"}


def _unified_migrate_flags() -> set[str]:
    parser = app_cli._build_parser()
    migrate = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            migrate = action.choices["migrate"]
            break
    assert migrate is not None
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            migrate.parse_args(["--help"])
        except SystemExit:
            pass
    return set(_FLAG_RE.findall(buf.getvalue())) - {"--help"}


# Shared migration contract that must exist on unified + both dedicated CLIs.
SHARED_MIGRATE_FLAGS = frozenset(
    {
        "--assets",
        "--input-mode",
        "--input-dir",
        "--output-dir",
        "--data-view",
        "--logs-index",
        "--field-profile",
        "--translation-mode",
        "--ca-cert",
        "--insecure",
        "--validate",
        "--upload",
        "--create-alert-rules",
        "--no-draft-alert-rules",
        "--es-url",
        "--es-api-key",
        "--kibana-url",
        "--kibana-api-key",
        "--smoke",
        "--smoke-output",
        "--browser-audit",
        "--capture-screenshots",
        "--select-folder",
        "--select-tag",
        "--select-datasource",
        "--select-team",
        "--select-updated-after",
        "--select-updated-before",
        "--select-starred",
    }
)

GRAFANA_ONLY_FLAGS = frozenset(
    {
        "--esql-index",
        "--fetch-alerts",
        "--alert-uids",
        "--alert-folder",
        "--grafana-url",
        "--grafana-user",
        "--grafana-pass",
        "--grafana-token",
    }
)

DATADOG_ONLY_FLAGS = frozenset(
    {
        "--env-file",
        "--dashboard-ids",
        "--monitor-ids",
        "--monitor-query",
        "--source-execution",
        "--fetch-monitors",
    }
)


class UnifiedCliAssetContractTests(unittest.TestCase):
    def test_unified_migrate_parser_has_assets_flag(self):
        parser = app_cli._build_parser()
        args = _parse_or_fail(
            parser.parse_args,
            ["migrate", "--source", "datadog", "--assets", "alerts"],
        )
        self.assertEqual(args.assets, "alerts")

    def test_grafana_parser_has_assets_flag(self):
        args = _parse_or_fail(grafana_cli.parse_args, ["--assets", "all"])
        self.assertEqual(args.assets, "all")

    def test_datadog_parser_has_assets_flag(self):
        args = _parse_or_fail(datadog_cli.parse_args, ["--assets", "dashboards"])
        self.assertEqual(args.assets, "dashboards")


class SharedMigrateFlagParityTests(unittest.TestCase):
    """Lock the shared migrate surface across unified / grafana / datadog."""

    @classmethod
    def setUpClass(cls):
        cls.unified = _unified_migrate_flags()
        cls.grafana = _help_flags(grafana_cli.parse_args)
        cls.datadog = _help_flags(datadog_cli.parse_args)

    def test_shared_flags_present_on_all_three_clis(self):
        for flag in sorted(SHARED_MIGRATE_FLAGS):
            with self.subTest(flag=flag):
                self.assertIn(flag, self.unified, f"unified missing {flag}")
                self.assertIn(flag, self.grafana, f"grafana-migrate missing {flag}")
                self.assertIn(flag, self.datadog, f"datadog-migrate missing {flag}")

    def test_esql_index_is_grafana_only(self):
        self.assertIn("--esql-index", self.unified)
        self.assertIn("--esql-index", self.grafana)
        self.assertNotIn("--esql-index", self.datadog)

    def test_deprecated_alert_alias_spelling_per_cli(self):
        # Unified + Grafana use --fetch-alerts; Datadog dedicated uses --fetch-monitors.
        self.assertIn("--fetch-alerts", self.unified)
        self.assertIn("--fetch-alerts", self.grafana)
        self.assertNotIn("--fetch-monitors", self.unified)
        self.assertIn("--fetch-monitors", self.datadog)
        self.assertNotIn("--fetch-alerts", self.datadog)

    def test_grafana_only_flags_absent_from_datadog(self):
        for flag in sorted(GRAFANA_ONLY_FLAGS - {"--fetch-alerts"}):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, self.datadog)

    def test_datadog_only_flags_absent_from_grafana(self):
        for flag in sorted(DATADOG_ONLY_FLAGS):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, self.grafana)

    def test_shared_semantic_defaults(self):
        unified = app_cli._build_parser().parse_args(["migrate", "--source", "grafana"])
        grafana = grafana_cli.parse_args([])
        datadog = datadog_cli.parse_args([])

        self.assertEqual(unified.assets, "dashboards")
        self.assertEqual(grafana.assets, "dashboards")
        self.assertEqual(datadog.assets, "dashboards")

        self.assertEqual(unified.field_profile, "otel")
        self.assertEqual(grafana.field_profile, "otel")
        self.assertEqual(datadog.field_profile, "otel")

        self.assertEqual(unified.translation_mode, "auto")
        self.assertEqual(grafana.translation_mode, "auto")
        self.assertEqual(datadog.translation_mode, "auto")

        self.assertEqual(unified.input_mode, "files")
        self.assertEqual(grafana.input_mode, "files")
        self.assertEqual(datadog.input_mode, "files")

        # data-view: unified empty → adapter default; grafana hard-defaults;
        # datadog None → profile-derived.
        self.assertEqual(unified.data_view, "")
        self.assertEqual(grafana.data_view, "metrics-*")
        self.assertIsNone(datadog.data_view)

    def test_space_flag_naming_is_source_specific(self):
        # Unified exposes --space-id; Grafana dedicated uses --shadow-space;
        # Datadog dedicated uses --space-id. Forwarding is covered in test_app_cli.
        self.assertIn("--space-id", self.unified)
        self.assertIn("--shadow-space", self.grafana)
        self.assertNotIn("--space-id", self.grafana)
        self.assertIn("--space-id", self.datadog)
        self.assertNotIn("--shadow-space", self.datadog)


class AssetCompositionTests(unittest.TestCase):
    def test_all_assets_runs_both_pipelines(self):
        cli_contract = _load_cli_contract_module()
        resolve_asset_selection = _require_attr(cli_contract, "resolve_asset_selection")
        selection = resolve_asset_selection(assets="all")
        self.assertTrue(selection.dashboards)
        self.assertTrue(selection.alerts)


class AssetNormalizationContractTests(unittest.TestCase):
    def test_fetch_alerts_alias_warns_and_normalizes_to_all(self):
        cli_contract = _load_cli_contract_module()
        normalize_requested_assets = _require_attr(cli_contract, "normalize_requested_assets")

        with self.assertWarnsRegex(
            FutureWarning,
            "--fetch-alerts/--fetch-monitors are deprecated",
        ):
            selection = normalize_requested_assets(
                assets="dashboards",
                fetch_alerts=True,
                fetch_monitors=False,
            )

        self.assertEqual(selection.label, "all")
        self.assertTrue(selection.dashboards)
        self.assertTrue(selection.alerts)

    def test_explicit_alerts_selection_still_warns_for_fetch_alerts_alias(self):
        cli_contract = _load_cli_contract_module()
        normalize_requested_assets = _require_attr(cli_contract, "normalize_requested_assets")

        with self.assertWarnsRegex(
            FutureWarning,
            "--fetch-alerts/--fetch-monitors are deprecated",
        ):
            selection = normalize_requested_assets(
                assets="alerts",
                fetch_alerts=True,
                fetch_monitors=False,
            )

        self.assertEqual(selection.label, "alerts")
        self.assertFalse(selection.dashboards)
        self.assertTrue(selection.alerts)

    def test_explicit_all_selection_still_warns_for_fetch_monitors_alias(self):
        cli_contract = _load_cli_contract_module()
        normalize_requested_assets = _require_attr(cli_contract, "normalize_requested_assets")

        with self.assertWarnsRegex(
            FutureWarning,
            "--fetch-alerts/--fetch-monitors are deprecated",
        ):
            selection = normalize_requested_assets(
                assets="all",
                fetch_alerts=False,
                fetch_monitors=True,
            )

        self.assertEqual(selection.label, "all")
        self.assertTrue(selection.dashboards)
        self.assertTrue(selection.alerts)


class AssetOutputDirectoryTests(unittest.TestCase):
    def test_dashboard_output_dir_uses_dashboards_subdirectory(self):
        cli_contract = _load_cli_contract_module()
        dashboard_output_dir = _require_attr(cli_contract, "dashboard_output_dir")

        self.assertEqual(
            dashboard_output_dir(Path("migration_output")),
            Path("migration_output") / "dashboards",
        )

    def test_alert_output_dir_uses_alerts_subdirectory(self):
        cli_contract = _load_cli_contract_module()
        alert_output_dir = _require_attr(cli_contract, "alert_output_dir")

        self.assertEqual(
            alert_output_dir(Path("migration_output")),
            Path("migration_output") / "alerts",
        )


if __name__ == "__main__":
    unittest.main()
