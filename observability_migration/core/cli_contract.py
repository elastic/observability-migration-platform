# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

ASSET_CHOICES = ("dashboards", "alerts", "all")


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
