#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""CLI entry point for dashboard interaction scenario audits."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from observability_migration.targets.kibana.interaction_driver import (  # noqa: E402
    PlaywrightKibanaBrowser,
    SettlePolicy,
)
from observability_migration.targets.kibana.interaction_runner import (  # noqa: E402
    InteractionRunner,
    PanelContract,
    RunConfig,
    format_runtime_error,
    load_panel_contract,
    validate_run_artifact_paths,
)
from observability_migration.targets.kibana.interaction_scenarios import (  # noqa: E402
    ManifestError,
    load_scenario,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute dashboard interaction scenarios against Kibana.",
    )
    parser.add_argument("--manifest", required=True, help="Scenario manifest YAML path")
    parser.add_argument("--dashboard-url", required=True, help="Dashboard URL to open")
    parser.add_argument(
        "--artifact-root",
        default="artifacts/interaction-audit",
        help="Root directory for run artifacts",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Run identifier (default: UTC timestamp)",
    )
    parser.add_argument(
        "--panel-contract",
        default="",
        help="Optional JSON file with all_query_panels and by_control mappings",
    )
    parser.add_argument("--headed", action="store_true", help="Run browser headed")
    parser.add_argument(
        "--user-data-dir",
        default="",
        help="Persistent Chromium user data directory",
    )
    parser.add_argument(
        "--executable-path",
        default="",
        help="Custom Chromium executable path",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Settle timeout in seconds",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=100,
        help="Settle poll interval in milliseconds",
    )
    parser.add_argument(
        "--stable-polls",
        type=int,
        default=3,
        help="Required stable polls before settle completes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    manifest_path = Path(args.manifest)
    try:
        scenario = load_scenario(manifest_path)
    except ManifestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.panel_contract:
        try:
            panel_contract = load_panel_contract(args.panel_contract)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        panel_contract = PanelContract()

    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    config = RunConfig(
        dashboard_url=args.dashboard_url,
        artifact_root=Path(args.artifact_root),
        run_id=run_id,
        settle_policy=SettlePolicy(
            timeout_seconds=args.timeout_seconds,
            poll_interval_ms=args.poll_interval_ms,
            stable_polls=args.stable_polls,
        ),
    )

    try:
        _, run_root = validate_run_artifact_paths(
            config.artifact_root,
            scenario.id,
            run_id,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    browser = PlaywrightKibanaBrowser()
    report = None
    try:
        browser.start(
            headless=not args.headed,
            user_data_dir=args.user_data_dir,
            executable_path=args.executable_path,
        )
        report = InteractionRunner(
            browser,
            scenario,
            panel_contract,
            config,
        ).run()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        print(format_runtime_error(exc), file=sys.stderr)
        return 1
    finally:
        browser.close()

    report_path = run_root / "report.json"
    print(str(report_path))
    if report is None:
        return 1
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
