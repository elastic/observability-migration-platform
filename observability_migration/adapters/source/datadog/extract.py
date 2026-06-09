# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Dashboard extraction from Datadog (file-based and API)."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from observability_migration.core.selection import (
    AssetSelectionMetadata,
    parse_selection_datetime,
)


def _safe_parse_dt(value: Any) -> Any:
    """Best-effort datetime parse; return None on anything unusable."""
    if value is None or value == "":
        return None
    try:
        return parse_selection_datetime(str(value))
    except (ValueError, TypeError, OverflowError):
        return None


def _team_from_tags(tags: list[str]) -> str | None:
    """Derive a team from a ``team:<value>`` tag, if present."""
    for tag in tags:
        if isinstance(tag, str) and tag.casefold().startswith("team:"):
            return tag.split(":", 1)[1]
    return None


def selection_metadata_from_datadog_dashboard(raw: dict[str, Any]) -> AssetSelectionMetadata:
    """Map a raw Datadog dashboard dict into the source-agnostic selection view.

    ``folder`` (Dashboard Lists API, not fetched) and ``starred`` (absent from
    the ``get_dashboard`` payload) are ``None`` -> degrade gracefully.
    """
    tags = [str(t) for t in (raw.get("tags") or [])]
    return AssetSelectionMetadata(
        folder=None,
        tags=tags,
        datasources=["datadog"],
        team=_team_from_tags(tags),
        updated_at=_safe_parse_dt(raw.get("modified_at")),
        starred=None,
    )


def selection_metadata_from_datadog_monitor(raw: dict[str, Any]) -> AssetSelectionMetadata:
    """Map a raw Datadog monitor dict into the source-agnostic selection view.

    folder/datasource/starred are ``None`` (not supplied by monitors).
    """
    tags = [str(t) for t in (raw.get("tags") or [])]
    return AssetSelectionMetadata(
        folder=None,
        tags=tags,
        datasources=None,
        team=_team_from_tags(tags),
        updated_at=_safe_parse_dt(raw.get("modified")),
        starred=None,
    )


def _json_safe_api_value(value: Any) -> Any:
    """Convert Datadog API model values into plain JSON-safe data."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe_api_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_api_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_api_value(item) for item in value]
    return value


def _apply_sdk_tls(config: Any, verify: bool | str = True) -> None:
    """Apply a requests-style TLS verify value to Datadog API client config."""
    if verify is False:
        config.verify_ssl = False
    elif isinstance(verify, str) and verify.strip():
        config.ssl_ca_cert = verify.strip()


def extract_dashboards_from_files(input_dir: str) -> list[dict[str, Any]]:
    """Load Datadog dashboard JSON files from a directory.

    Accepts both single-dashboard exports (with `widgets` at top level)
    and list exports (array of dashboards).
    """
    dashboards: list[dict[str, Any]] = []
    input_path = Path(input_dir)

    if not input_path.exists():
        raise FileNotFoundError(f"input directory not found: {input_dir}")

    json_files = sorted(input_path.rglob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"no JSON files found in: {input_dir}")

    for fpath in json_files:
        try:
            raw = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"  WARN: skipping {fpath.name}: {exc}")
            continue

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    if "widgets" in item:
                        item["_source_file"] = str(fpath)
                        dashboards.append(item)
                    elif "dashboard" in item and isinstance(item.get("dashboard"), dict):
                        inner = item["dashboard"]
                        inner["_source_file"] = str(fpath)
                        dashboards.append(inner)
        elif isinstance(raw, dict):
            raw["_source_file"] = str(fpath)
            if "widgets" in raw:
                dashboards.append(raw)
            elif "dashboard" in raw:
                inner = raw["dashboard"]
                inner["_source_file"] = str(fpath)
                dashboards.append(inner)
            else:
                print(f"  WARN: skipping {fpath.name}: no 'widgets' key found")
        else:
            print(f"  WARN: skipping {fpath.name}: unexpected structure")

    return dashboards


def extract_dashboards_from_api(
    api_key: str,
    app_key: str,
    site: str = "datadoghq.com",
    dashboard_ids: list[str] | None = None,
    verify: bool | str = True,
) -> list[dict[str, Any]]:
    """Pull dashboards from the Datadog API using the official Python client.

    Requires `datadog-api-client` to be installed.
    """
    try:
        from datadog_api_client import ApiClient, Configuration
        from datadog_api_client.v1.api.dashboards_api import DashboardsApi
    except ImportError as exc:
        raise ImportError(
            "datadog-api-client is required for API extraction. "
            "Install with: pip install -e '.[datadog]' "
            "or pip install datadog-api-client"
        ) from exc

    config = Configuration()
    config.api_key["apiKeyAuth"] = api_key
    config.api_key["appKeyAuth"] = app_key
    config.server_variables["site"] = site
    _apply_sdk_tls(config, verify)

    dashboards: list[dict[str, Any]] = []

    with ApiClient(config) as api_client:
        api = DashboardsApi(api_client)

        if dashboard_ids:
            ids_to_fetch = dashboard_ids
        else:
            summary = api.list_dashboards()
            ids_to_fetch = [
                d.id for d in (summary.dashboards or [])
            ]

        for dash_id in ids_to_fetch:
            try:
                dash = api.get_dashboard(dash_id)
                raw = _json_safe_api_value(dash.to_dict())
                raw["_dd_id"] = dash_id
                dashboards.append(raw)
            except Exception as exc:
                print(f"  WARN: failed to fetch dashboard {dash_id}: {exc}")

    return dashboards


def load_credentials_from_env(env_file: str | None = None) -> dict[str, str]:
    """Load Datadog credentials from environment or a .env file."""
    if env_file and Path(env_file).exists():
        for line in Path(env_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ[key.strip()] = val.strip()

    return {
        "api_key": os.environ.get("DD_API_KEY", ""),
        "app_key": os.environ.get("DD_APP_KEY", ""),
        "site": os.environ.get("DD_SITE", "datadoghq.com"),
    }


def extract_monitors_from_api(
    api_key: str,
    app_key: str,
    site: str = "datadoghq.com",
    monitor_ids: list[str | int] | None = None,
    monitor_query: str = "",
    verify: bool | str = True,
) -> list[dict[str, Any]]:
    """Pull monitors from the Datadog API using the official Python client.

    Requires `datadog-api-client` to be installed.

    If ``monitor_ids`` is non-empty, each ID is fetched with ``get_monitor``.
    Otherwise, if ``monitor_query`` is non-empty, ``search_monitors`` is used
    (paginated). Otherwise all monitors are listed via ``list_monitors_with_pagination``.
    """
    try:
        from datadog_api_client import ApiClient, Configuration
        from datadog_api_client.v1.api.monitors_api import MonitorsApi
    except ImportError as exc:
        raise ImportError(
            "datadog-api-client is required for API extraction. "
            "Install with: pip install -e '.[datadog]' "
            "or pip install datadog-api-client"
        ) from exc

    config = Configuration()
    config.api_key["apiKeyAuth"] = api_key
    config.api_key["appKeyAuth"] = app_key
    config.server_variables["site"] = site
    _apply_sdk_tls(config, verify)

    monitors: list[dict[str, Any]] = []

    with ApiClient(config) as api_client:
        api = MonitorsApi(api_client)

        if monitor_ids:
            for mid in monitor_ids:
                try:
                    monitor_id = int(mid)
                except (TypeError, ValueError):
                    print(f"  WARN: invalid monitor id (expected integer): {mid!r}")
                    continue
                try:
                    mon = api.get_monitor(monitor_id)
                    raw = _json_safe_api_value(mon.to_dict())
                    raw["_dd_id"] = str(monitor_id)
                    monitors.append(raw)
                except Exception as exc:
                    print(f"  WARN: failed to fetch monitor {mid}: {exc}")
            return monitors

        query = monitor_query.strip()
        if query:
            page = 0
            per_page = 100
            while True:
                try:
                    resp = api.search_monitors(
                        query=query, page=page, per_page=per_page
                    )
                except Exception as exc:
                    print(f"  WARN: search_monitors failed (page={page}): {exc}")
                    break
                batch = resp.monitors or []
                if not batch:
                    break
                for item in batch:
                    try:
                        monitors.append(_json_safe_api_value(item.to_dict()))
                    except Exception as exc:
                        mid = getattr(item, "id", None)
                        print(f"  WARN: failed to serialize search monitor {mid}: {exc}")
                meta = resp.metadata
                if meta is not None and getattr(meta, "page_count", None) is not None:
                    if page + 1 >= int(meta.page_count):
                        break
                if len(batch) < per_page:
                    break
                page += 1
            return monitors

        for mon in api.list_monitors_with_pagination():
            try:
                monitors.append(_json_safe_api_value(mon.to_dict()))
            except Exception as exc:
                mid = getattr(mon, "id", None)
                print(f"  WARN: failed to serialize monitor {mid}: {exc}")

    return monitors


def extract_monitors_from_files(input_dir: str) -> list[dict[str, Any]]:
    """Load Datadog monitor JSON files from a directory.

    Accepts single monitor objects (with ``id`` and ``type`` fields),
    arrays of monitors, or wrapped ``{"monitors": [...]}`` exports.
    """
    monitors: list[dict[str, Any]] = []
    input_path = Path(input_dir)

    if not input_path.exists():
        raise FileNotFoundError(f"input directory not found: {input_dir}")

    json_files = sorted(input_path.rglob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"no JSON files found in: {input_dir}")

    for fpath in json_files:
        try:
            raw = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"  WARN: skipping {fpath.name}: {exc}")
            continue

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    item["_source_file"] = str(fpath)
                    monitors.append(item)
        elif isinstance(raw, dict):
            if "monitors" in raw and isinstance(raw["monitors"], list):
                for item in raw["monitors"]:
                    if isinstance(item, dict):
                        item["_source_file"] = str(fpath)
                        monitors.append(item)
            elif "id" in raw and "type" in raw:
                raw["_source_file"] = str(fpath)
                monitors.append(raw)
            else:
                print(
                    f"  WARN: skipping {fpath.name}: "
                    "expected 'monitors' array or a monitor object with 'id' and 'type'"
                )
        else:
            print(f"  WARN: skipping {fpath.name}: unexpected structure")

    return monitors


__all__ = [
    "extract_dashboards_from_api",
    "extract_dashboards_from_files",
    "extract_monitors_from_api",
    "extract_monitors_from_files",
    "load_credentials_from_env",
]
