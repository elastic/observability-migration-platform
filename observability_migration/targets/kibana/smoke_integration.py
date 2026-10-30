# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

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
        return {
            "merged": 0, "smoke_failed": 0, "browser_failed": 0, "empty_result": 0,
            "not_runtime_checked": 0, "null_series": 0, "control_unbound": 0,
            "control_null_param": 0, "unreadable_axis": 0,
        }

    merged = 0
    smoke_failed = 0
    browser_failed = 0
    empty_result = 0
    not_runtime_checked = 0
    null_series = 0
    control_unbound = 0
    control_null_param = 0
    unreadable_axis = 0

    for result in results:
        smoke_dashboard = _match_smoke_dashboard(result, smoke_by_id, smoke_by_title)
        if not smoke_dashboard:
            continue
        if getattr(result, "kibana_saved_object_id", "") != str(smoke_dashboard.get("id", "") or ""):
            setattr(result, "kibana_saved_object_id", str(smoke_dashboard.get("id", "") or ""))
        smoke_panels = _panel_buckets(smoke_dashboard)
        # Panels filtered by a control that has no options: the filter is
        # unusable, and native PROMQL ones fail while it has no selection.
        unbound_titles = {
            title
            for control in smoke_dashboard.get("empty_controls", []) or []
            for title in control.get("dependent_panels", []) or []
        }
        unreadable_titles = {
            str(panel.get("panel", "")) for panel in smoke_dashboard.get("unreadable_axis_panels", []) or []
        }
        null_param_titles = {
            title
            for control in smoke_dashboard.get("empty_controls", []) or []
            for title in control.get("native_promql_dependents", []) or []
        }
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
            elif status == "null_series":
                if "null_series" not in rollups:
                    rollups.append("null_series")
                    null_series += 1
                    merged += 1
            if title in unbound_titles and "control_unbound" not in rollups:
                rollups.append("control_unbound")
                control_unbound += 1
                merged += 1
            if title in unreadable_titles and "unreadable_axis" not in rollups:
                rollups.append("unreadable_axis")
                unreadable_axis += 1
                merged += 1
            if title in null_param_titles and "control_null_param" not in rollups:
                rollups.append("control_null_param")
                control_null_param += 1
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
        "null_series": null_series,
        "control_unbound": control_unbound,
        "control_null_param": control_null_param,
        "unreadable_axis": unreadable_axis,
    }


__all__ = ["load_smoke_report", "merge_smoke_into_results"]
