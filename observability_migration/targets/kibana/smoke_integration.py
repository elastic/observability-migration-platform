"""Merge Kibana smoke-test results back into migration result objects."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def load_smoke_report(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    with path.open() as fh:
        return json.load(fh)


def _build_smoke_dashboard_indexes(
    smoke_report: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    for dashboard in smoke_report.get("dashboards", []):
        dashboard_id = str(dashboard.get("id", "") or "")
        dashboard_title = str(dashboard.get("title", "") or "")
        if dashboard_id and dashboard_id not in by_id:
            by_id[dashboard_id] = dashboard
        if dashboard_title and dashboard_title not in by_title:
            by_title[dashboard_title] = dashboard
    return by_id, by_title


def _panel_buckets(smoke_dashboard: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for panel in smoke_dashboard.get("panels", []):
        panel_title = str(panel.get("panel", "") or "")
        buckets.setdefault(panel_title, []).append(copy.deepcopy(panel))
    return buckets


def _match_smoke_dashboard(
    result: Any,
    by_id: dict[str, dict[str, Any]],
    by_title: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    kibana_saved_object_id = str(getattr(result, "kibana_saved_object_id", "") or "")
    if kibana_saved_object_id and kibana_saved_object_id in by_id:
        return by_id[kibana_saved_object_id]
    dashboard_title = str(getattr(result, "dashboard_title", "") or "")
    if dashboard_title and dashboard_title in by_title:
        return by_title[dashboard_title]
    return None


def merge_smoke_into_results(
    results: list[Any],
    smoke_report: dict[str, Any],
) -> dict[str, Any]:
    smoke_by_id, smoke_by_title = _build_smoke_dashboard_indexes(smoke_report)
    if not smoke_by_id and not smoke_by_title:
        return {"merged": 0, "smoke_failed": 0, "browser_failed": 0, "empty_result": 0, "not_runtime_checked": 0}

    merged = 0
    smoke_failed = 0
    browser_failed = 0
    empty_result = 0
    not_runtime_checked = 0

    for result in results:
        smoke_dashboard = _match_smoke_dashboard(result, smoke_by_id, smoke_by_title)
        if not smoke_dashboard:
            continue
        if getattr(result, "kibana_saved_object_id", "") != str(smoke_dashboard.get("id", "") or ""):
            setattr(result, "kibana_saved_object_id", str(smoke_dashboard.get("id", "") or ""))
        smoke_panels = _panel_buckets(smoke_dashboard)
        for panel_result in getattr(result, "panel_results", []) or []:
            title = str(getattr(panel_result, "title", ""))
            matches = smoke_panels.get(title, [])
            if not matches:
                continue
            smoke_panel = matches.pop(0)

            rollups = list(getattr(panel_result, "runtime_rollups", []) or [])
            status = smoke_panel.get("status", "")

            if status == "fail":
                if "smoke_failed" not in rollups:
                    rollups.append("smoke_failed")
                    smoke_failed += 1
                    merged += 1
            elif status == "empty":
                if "empty_result" not in rollups:
                    rollups.append("empty_result")
                    empty_result += 1
                    merged += 1
            elif status == "not_runtime_checked":
                if "not_runtime_checked" not in rollups:
                    rollups.append("not_runtime_checked")
                    not_runtime_checked += 1
                    merged += 1

            panel_result.runtime_rollups = rollups

    for result in results:
        smoke_dashboard = _match_smoke_dashboard(result, smoke_by_id, smoke_by_title)
        if not smoke_dashboard:
            continue
        audit = smoke_dashboard.get("browser_audit", {})
        if audit.get("status") == "error":
            for panel_result in getattr(result, "panel_results", []) or []:
                rollups = list(getattr(panel_result, "runtime_rollups", []) or [])
                if "browser_failed" not in rollups:
                    rollups.append("browser_failed")
                    browser_failed += 1
                    merged += 1
                panel_result.runtime_rollups = rollups

    return {
        "merged": merged,
        "smoke_failed": smoke_failed,
        "browser_failed": browser_failed,
        "empty_result": empty_result,
        "not_runtime_checked": not_runtime_checked,
    }


__all__ = ["load_smoke_report", "merge_smoke_into_results"]
