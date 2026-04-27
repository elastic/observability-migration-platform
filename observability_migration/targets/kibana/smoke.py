"""Shared Kibana post-upload smoke validation helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import requests

from observability_migration.adapters.source.grafana import smoke as grafana_smoke

DEFAULT_SAVED_OBJECTS_PER_PAGE = grafana_smoke.DEFAULT_SAVED_OBJECTS_PER_PAGE


def run_smoke_report(
    *,
    kibana_url: str,
    es_url: str,
    kibana_api_key: str = "",
    es_api_key: str = "",
    space_id: str = "",
    output_path: str | Path = "",
    dashboard_titles: list[str] | None = None,
    dashboard_ids: list[str] | None = None,
    timeout: int = 30,
    saved_objects_per_page: int = DEFAULT_SAVED_OBJECTS_PER_PAGE,
    capture_screenshots: bool = False,
    browser_audit: bool = False,
    screenshot_dir: str = "",
    browser_audit_dir: str = "",
    chrome_binary: str = "",
    time_from: str = "now-1h",
    time_to: str = "now",
    window_width: int = 1600,
    window_height: int = 2200,
    virtual_time_budget_ms: int = 30000,
    screenshot_retries: int = 1,
) -> dict[str, Any]:
    """Inspect uploaded Kibana dashboards and return a smoke report payload."""

    output_value = str(output_path or "uploaded_dashboard_smoke_report.json")
    args = SimpleNamespace(
        kibana_url=kibana_url,
        es_url=es_url,
        kibana_api_key=kibana_api_key,
        es_api_key=es_api_key,
        space_id=space_id,
        output=output_value,
        timeout=timeout,
        saved_objects_per_page=saved_objects_per_page,
        dashboard_title=list(dashboard_titles or []),
        dashboard_id=list(dashboard_ids or []),
        capture_screenshots=capture_screenshots,
        browser_audit=browser_audit,
        screenshot_dir=screenshot_dir,
        browser_audit_dir=browser_audit_dir,
        chrome_binary=chrome_binary,
        time_from=time_from,
        time_to=time_to,
        window_width=window_width,
        window_height=window_height,
        virtual_time_budget_ms=virtual_time_budget_ms,
        screenshot_retries=screenshot_retries,
    )

    session = requests.Session()
    session.headers.update({"kbn-xsrf": "true"})
    if kibana_api_key:
        session.headers.update({"Authorization": f"ApiKey {kibana_api_key}"})

    dashboards = []
    for item in grafana_smoke.load_dashboards(
        session,
        kibana_url,
        space_id,
        timeout,
        per_page=saved_objects_per_page,
    ):
        if not grafana_smoke.should_include_dashboard(item, args.dashboard_title, args.dashboard_id):
            continue
        attributes = item.get("attributes", {}) or {}
        saved_object = item if attributes.get("panelsJSON") else grafana_smoke.load_dashboard(
            session,
            kibana_url,
            space_id,
            item["id"],
            timeout,
        )
        screenshot = grafana_smoke.capture_dashboard_screenshot(saved_object, args) if capture_screenshots else None
        browser_info = grafana_smoke.capture_browser_audit(saved_object, args) if browser_audit else None
        dashboards.append(
            grafana_smoke.inspect_dashboard(
                saved_object,
                es_url,
                timeout,
                screenshot=screenshot,
                browser_audit=browser_info,
                es_api_key=es_api_key,
            )
        )

    if not dashboards:
        raise ValueError("No dashboards matched the requested smoke filters.")

    payload = {
        "summary": grafana_smoke.build_summary(dashboards),
        "dashboards": dashboards,
    }
    if output_path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


__all__ = [
    "DEFAULT_SAVED_OBJECTS_PER_PAGE",
    "run_smoke_report",
]
