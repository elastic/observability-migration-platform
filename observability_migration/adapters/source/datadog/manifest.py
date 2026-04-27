"""Datadog migration manifest helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from observability_migration.core.reporting.report import _ir_to_dict


def build_migration_manifest(results: list[Any]) -> dict[str, Any]:
    dashboards: list[dict[str, Any]] = []
    flat_panels: list[dict[str, Any]] = []

    for result in results:
        runtime_summary = result.build_runtime_summary()
        dashboard_entry = {
            "dashboard_id": getattr(result, "dashboard_id", ""),
            "title": getattr(result, "dashboard_title", ""),
            "source_file": getattr(result, "source_file", ""),
            "yaml_path": getattr(result, "yaml_path", ""),
            "compiled_path": getattr(result, "compiled_path", ""),
            "uploaded_space": getattr(result, "uploaded_space", ""),
            "uploaded_kibana_url": getattr(result, "uploaded_kibana_url", ""),
            "kibana_saved_object_id": getattr(result, "kibana_saved_object_id", ""),
            "preflight_passed": bool(getattr(result, "preflight_passed", True)),
            "preflight_issues": list(getattr(result, "preflight_issues", []) or []),
            "validation_summary": dict(getattr(result, "validation_summary", {}) or {}),
            "verification_summary": dict(getattr(result, "verification_summary", {}) or {}),
            "runtime_summary": runtime_summary,
            "panels": [],
        }
        for panel_result in getattr(result, "panel_results", []) or []:
            panel_entry = {
                "dashboard_id": getattr(result, "dashboard_id", ""),
                "dashboard_title": getattr(result, "dashboard_title", ""),
                "source_panel_id": getattr(panel_result, "source_panel_id", "") or getattr(panel_result, "widget_id", ""),
                "title": getattr(panel_result, "title", ""),
                "datadog_widget_type": getattr(panel_result, "dd_widget_type", ""),
                "kibana_type": getattr(panel_result, "kibana_type", ""),
                "status": getattr(panel_result, "status", ""),
                "backend": getattr(panel_result, "backend", ""),
                "confidence": getattr(panel_result, "confidence", 0.0),
                "query_language": getattr(panel_result, "query_language", ""),
                "warnings": list(getattr(panel_result, "warnings", []) or []),
                "semantic_losses": list(getattr(panel_result, "semantic_losses", []) or []),
                "reasons": list(getattr(panel_result, "reasons", []) or []),
                "source_queries": list(getattr(panel_result, "source_queries", []) or []),
                "query_ir": dict(getattr(panel_result, "query_ir", {}) or {}),
                "operational_ir": _ir_to_dict(getattr(panel_result, "operational_ir", {})),
                "target_candidates": list(getattr(panel_result, "target_candidates", []) or []),
                "recommended_target": getattr(panel_result, "recommended_target", ""),
                "verification_packet": dict(getattr(panel_result, "verification_packet", {}) or {}),
                "runtime_rollups": list(getattr(panel_result, "runtime_rollups", []) or []),
            }
            dashboard_entry["panels"].append(panel_entry)
            flat_panels.append(panel_entry)
        dashboards.append(dashboard_entry)

    return {
        "summary": {
            "dashboards": len(dashboards),
            "panels": len(flat_panels),
            "ok": sum(1 for panel in flat_panels if panel["status"] == "ok"),
            "warning": sum(1 for panel in flat_panels if panel["status"] == "warning"),
            "requires_manual": sum(1 for panel in flat_panels if panel["status"] == "requires_manual"),
            "not_feasible": sum(1 for panel in flat_panels if panel["status"] == "not_feasible"),
            "blocked": sum(1 for panel in flat_panels if panel["status"] == "blocked"),
            "green": sum(1 for panel in flat_panels if (panel.get("verification_packet") or {}).get("semantic_gate") == "Green"),
            "yellow": sum(1 for panel in flat_panels if (panel.get("verification_packet") or {}).get("semantic_gate") == "Yellow"),
            "red": sum(1 for panel in flat_panels if (panel.get("verification_packet") or {}).get("semantic_gate") == "Red"),
            "uploaded_ok": sum(1 for dashboard in dashboards if (dashboard.get("runtime_summary", {}).get("upload") or {}).get("status") == "pass"),
            "smoke_ok": sum(1 for dashboard in dashboards if (dashboard.get("runtime_summary", {}).get("smoke") or {}).get("status") == "pass"),
        },
        "dashboards": dashboards,
        "panels": flat_panels,
    }


def save_migration_manifest(results: list[Any], output_path: str | Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(build_migration_manifest(results), fh, indent=2)


__all__ = ["build_migration_manifest", "save_migration_manifest"]
