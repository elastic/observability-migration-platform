# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import importlib
import unittest
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
