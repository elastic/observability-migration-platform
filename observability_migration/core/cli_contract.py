# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

ASSET_CHOICES = ("dashboards", "alerts", "all")

_DOCS_URL = "https://github.com/elastic/observability-migration-platform/blob/main/docs/command-contract.md"

# Surfaces removed with the dashboard-YAML artifact path. argparse would report
# these as "unrecognized arguments" / "invalid choice", which reads like a typo;
# an operator upgrading a script that used them needs to be told the surface is
# gone and what replaced it. Shared by ``obs-migrate`` and the ``grafana-migrate``
# / ``datadog-migrate`` compatibility aliases so all three say the same thing.
REMOVED_FLAGS: dict[str, str] = {
    "--yaml-dir": (
        "Dashboard YAML is no longer an upload input. Point --artifact-dir at "
        "the migration's dashboard artifact directory (which holds native/ and "
        "ir/) instead."
    ),
    "--compiled-dir": (
        "Dashboard YAML/NDJSON is no longer an upload input. Point "
        "--artifact-dir at the migration's dashboard artifact directory "
        "(which holds native/ and ir/) instead."
    ),
    "--artifact-format": (
        "There is only one artifact format now: the native Dashboard-as-Code "
        "payload under native/. Drop the flag."
    ),
    "--legacy-import": (
        "The legacy kb-dashboard-cli compile + saved-objects _import path has "
        "been removed. Uploads go through the typed Kibana Dashboards API "
        "(PUT /api/dashboards/{id}). Drop the flag."
    ),
    "--compile": (
        "Compiling dashboard YAML to NDJSON has been removed; a migration no "
        "longer writes a 'compiled/' directory. The typed Dashboards API "
        "upload never consumed it. Drop the flag."
    ),
    "--no-compile": (
        "Compiling dashboard YAML to NDJSON has been removed, so there is "
        "nothing to opt out of. Drop the flag."
    ),
    # Was a hidden no-op meaning "use the typed API", which is now the only
    # path. Never documented, but scripts may still pass it.
    "--use-dashboards-api": (
        "The typed Kibana Dashboards API is the only upload path now, so this "
        "flag no longer selects anything. Drop the flag."
    ),
    "--kibana-promql-control-params": (
        "Native PROMQL control-param binding is preferred by default and "
        "auto-confirmed when --kibana-url reports Kibana 9.5+ (Kibana < 9.5 "
        "keeps the ES|QL fallback). Drop the flag."
    ),
}

REMOVED_COMMANDS: dict[str, str] = {
    "compile": (
        "'obs-migrate compile' compiled dashboard YAML to Kibana NDJSON via "
        "kb-dashboard-cli. Dashboard YAML is no longer produced or consumed: "
        "'obs-migrate migrate' writes native/*.native.json and ir/*.ir.json, "
        "and 'obs-migrate upload --artifact-dir <dir>' deploys them through "
        "the typed Kibana Dashboards API."
    ),
}


def reject_removed_surfaces(argv: list[str], *, prog: str = "obs-migrate") -> None:
    """Exit 2 with a targeted message on a surface removed with the YAML path.

    Checked before argparse so the operator gets "this was removed, use X"
    instead of "unrecognized arguments", which reads like a typo.
    """
    if argv and argv[0] in REMOVED_COMMANDS:
        print(
            f"{prog}: the '{argv[0]}' command has been removed.\n"
            f"  {REMOVED_COMMANDS[argv[0]]}\n"
            f"  Full command reference: {_DOCS_URL}",
            file=sys.stderr,
        )
        sys.exit(2)
    for token in argv:
        flag = token.split("=", 1)[0]
        if flag in REMOVED_FLAGS:
            print(
                f"{prog}: the '{flag}' option has been removed.\n"
                f"  {REMOVED_FLAGS[flag]}\n"
                f"  Full command reference: {_DOCS_URL}",
                file=sys.stderr,
            )
            sys.exit(2)


@dataclass(frozen=True)
class AssetSelection:
    dashboards: bool
    alerts: bool
    label: str


def resolve_asset_selection(*, assets: str) -> AssetSelection:
    if assets == "dashboards":
        return AssetSelection(dashboards=True, alerts=False, label=assets)
    if assets == "alerts":
        return AssetSelection(dashboards=False, alerts=True, label=assets)
    if assets == "all":
        return AssetSelection(dashboards=True, alerts=True, label=assets)
    raise ValueError(f"Unsupported assets value: {assets}")


def normalize_requested_assets(
    *,
    assets: str,
    fetch_alerts: bool,
    fetch_monitors: bool,
) -> AssetSelection:
    normalized = assets
    if fetch_alerts or fetch_monitors:
        warnings.warn(
            "--fetch-alerts/--fetch-monitors are deprecated; use --assets all or --assets alerts explicitly",
            FutureWarning,
            stacklevel=2,
        )
    if normalized == "dashboards" and (fetch_alerts or fetch_monitors):
        normalized = "all"
    return resolve_asset_selection(assets=normalized)


def dashboard_output_dir(base_dir: Path) -> Path:
    return base_dir / "dashboards"


def alert_output_dir(base_dir: Path) -> Path:
    return base_dir / "alerts"
