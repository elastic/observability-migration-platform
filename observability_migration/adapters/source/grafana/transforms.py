# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Extract and classify Grafana transformations as structured redesign tasks.

Grafana transformations (joins, calculations, regex extracts, filters, etc.)
cannot be automatically translated to Kibana.  This module extracts them from
panel JSON, classifies each by type and complexity, and emits structured
redesign tasks that appear in the migration manifest and preflight report.
"""

from __future__ import annotations

from typing import Any

TRANSFORM_COMPLEXITY = {
    "merge": "medium",
    "seriesToColumns": "medium",
    "seriesToRows": "low",
    "filterByName": "low",
    "filterByRefId": "low",
    "filterFieldsByName": "low",
    "organize": "low",
    "sortBy": "low",
    "reduce": "medium",
    "calculateField": "medium",
    "configFromData": "high",
    "groupBy": "medium",
    "concatenate": "low",
    "labelsToFields": "medium",
    "extractFields": "medium",
    "renameByRegex": "medium",
    "convertFieldType": "low",
    "joinByField": "medium",
    "histogram": "medium",
    "groupingToMatrix": "high",
    "prepareTimeSeries": "low",
    "limit": "low",
    "filterByValue": "medium",
    "joinByLabels": "medium",
    "regression": "high",
    "partitionByValues": "medium",
    "formatTime": "low",
    "formatString": "low",
    "rowsToFields": "medium",
    "spatial": "high",
}

KIBANA_ALTERNATIVES = {
    "filterByName": "Use ES|QL KEEP/DROP to select columns",
    "filterFieldsByName": "Use ES|QL KEEP/DROP to select columns",
    "filterByRefId": "Not needed — Kibana panels reference a single query",
    "organize": "Use ES|QL RENAME and KEEP for column ordering",
    "sortBy": "Use ES|QL SORT",
    "reduce": "Use ES|QL STATS aggregation",
    "calculateField": "Use ES|QL EVAL for calculated columns",
    "merge": "Use ES|QL ENRICH or Lens formula layer",
    "seriesToColumns": "Use ES|QL STATS ... BY with PIVOT-like aggregation",
    "seriesToRows": "Use ES|QL MV_EXPAND or restructure the query",
    "groupBy": "Use ES|QL STATS ... BY",
    "concatenate": "Use ES|QL CONCAT function in EVAL",
    "labelsToFields": "Labels become columns naturally in ES|QL output",
    "renameByRegex": "Use ES|QL RENAME",
    "convertFieldType": "Use ES|QL TO_* type conversion functions",
    "joinByField": "Use ES|QL ENRICH or Kibana runtime fields",
    "histogram": "Use ES|QL BUCKET function",
    "limit": "Use ES|QL LIMIT",
    "filterByValue": "Use ES|QL WHERE clause",
    "prepareTimeSeries": "Not needed — ES|QL time series are natively structured",
    "formatTime": "Use ES|QL DATE_FORMAT function",
}


def extract_transformations(panel: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract transformations from a Grafana panel."""
    raw = panel.get("transformations") or []
    extracted: list[dict[str, Any]] = []
    for idx, transform in enumerate(raw):
        transform_id = str(transform.get("id", "") or "")
        options = transform.get("options") or {}
        disabled = bool(transform.get("disabled", False))

        entry: dict[str, Any] = {
            "index": idx,
            "id": transform_id,
            "disabled": disabled,
            "complexity": TRANSFORM_COMPLEXITY.get(transform_id, "high"),
            "kibana_alternative": KIBANA_ALTERNATIVES.get(transform_id, "Manual redesign required"),
        }

        if transform_id == "calculateField":
            entry["details"] = {
                "mode": options.get("mode", ""),
                "alias": options.get("alias", ""),
            }
        elif transform_id in ("filterByName", "filterFieldsByName"):
            entry["details"] = {
                "fields": list((options.get("include") or {}).get("names", []) or []),
            }
        elif transform_id == "organize":
            entry["details"] = {
                "renames": options.get("renameByName", {}),
                "excludes": options.get("excludeByName", {}),
            }
        elif transform_id in ("merge", "joinByField"):
            entry["details"] = {
                "field": options.get("byField", ""),
            }
        elif transform_id == "groupBy":
            entry["details"] = {
                "fields": options.get("fields", {}),
            }
        elif transform_id == "sortBy":
            sort_items = options.get("sort", [])
            entry["details"] = {
                "fields": [s.get("field", "") for s in sort_items] if isinstance(sort_items, list) else [],
            }
        else:
            raw_keys = sorted(options.keys()) if isinstance(options, dict) else []
            entry["details"] = {"option_keys": raw_keys[:10]}

        extracted.append(entry)

    return extracted


def build_redesign_tasks(
    panel_title: str,
    dashboard_title: str,
    transformations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert extracted transformations into actionable redesign task entries."""
    tasks: list[dict[str, Any]] = []
    for transform in transformations:
        if transform.get("disabled"):
            continue
        tasks.append({
            "dashboard": dashboard_title,
            "panel": panel_title,
            "task_type": "transformation_redesign",
            "transform_id": transform["id"],
            "complexity": transform["complexity"],
            "kibana_alternative": transform["kibana_alternative"],
            "details": transform.get("details", {}),
            "description": (
                f"Panel '{panel_title}' uses Grafana transformation "
                f"'{transform['id']}' ({transform['complexity']} complexity). "
                f"Kibana alternative: {transform['kibana_alternative']}"
            ),
        })
    return tasks


def build_transform_summary(
    all_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize transformation redesign tasks across all panels."""
    if not all_tasks:
        return {"total": 0, "by_complexity": {}, "by_type": {}}

    by_complexity: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for task in all_tasks:
        c = task.get("complexity", "high")
        by_complexity[c] = by_complexity.get(c, 0) + 1
        t = task.get("transform_id", "unknown")
        by_type[t] = by_type.get(t, 0) + 1

    return {
        "total": len(all_tasks),
        "by_complexity": dict(sorted(by_complexity.items(), key=lambda x: -x[1])),
        "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
    }


__all__ = [
    "build_redesign_tasks",
    "build_transform_summary",
    "extract_transformations",
]
