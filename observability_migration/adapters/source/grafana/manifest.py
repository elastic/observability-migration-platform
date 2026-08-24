# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
import re
from pathlib import Path
from typing import Any

from observability_migration.core.reporting.report import _ir_to_dict, build_runtime_summary

ESQL_PREFIX_RE = re.compile(r"^\s*(?:FROM|TS|ROW)\b", re.IGNORECASE)
LOGQL_TOKEN_RE = re.compile(r"\|\~|\|=|\bcount_over_time\s*\(", re.IGNORECASE)


def normalize_datasource(datasource: Any) -> dict[str, str]:
    if isinstance(datasource, dict):
        return {
            "type": str(datasource.get("type") or "").strip(),
            "uid": str(datasource.get("uid") or "").strip(),
            "name": str(datasource.get("name") or datasource.get("uid") or "").strip(),
        }
    if isinstance(datasource, str):
        value = datasource.strip()
        return {
            "type": value.lower(),
            "uid": "",
            "name": value,
        }
    return {"type": "", "uid": "", "name": ""}


def target_query_text(target: dict[str, Any]) -> str:
    for key in ("expr", "query", "rawQuery"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for subkey in ("query", "rawQuery"):
                nested = value.get(subkey)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def infer_query_language(query_text: str, datasource_type: str = "", panel_type: str = "") -> str:
    query_text = (query_text or "").strip()
    datasource_type = (datasource_type or "").strip().lower()
    panel_type = (panel_type or "").strip().lower()

    if not query_text:
        return "unknown"
    # An explicit datasource wins over the panel type. Grafana's Logs panel is a
    # renderer, not a datasource: it is routinely pointed at Prometheus to show a
    # metric as a table. Treating panel_type == "logs" as LogQL before reading the
    # datasource sent such panels down the LogQL path, where labels resolve with
    # OTEL/ECS naming -- a canary panel with a Prometheus datasource and
    # ``sum(redis_db_keys) by (instance)`` emitted ``service.instance.id`` and
    # failed in Kibana with "Unknown column", while every other panel type on the
    # same dashboard correctly used the prometheus_native passthrough field.
    if "loki" in datasource_type:
        return "logql"
    if "elastic" in datasource_type:
        return "esql" if ESQL_PREFIX_RE.match(query_text) else "elasticsearch"
    if "prom" in datasource_type or "mimir" in datasource_type:
        return "promql"
    # No usable datasource hint: the Logs panel type is then the best signal.
    if panel_type == "logs":
        return "logql"
    if ESQL_PREFIX_RE.match(query_text):
        return "esql"
    if LOGQL_TOKEN_RE.search(query_text):
        return "logql"
    return "promql"


def _datasource_identity(meta: dict[str, str]) -> str:
    uid = str(meta.get("uid") or "").strip()
    if uid:
        return f"uid::{uid}"
    return "::".join([
        "legacy",
        str(meta.get("type") or "").strip(),
        str(meta.get("name") or "").strip(),
    ])


def analyze_panel_targets(panel: dict[str, Any]) -> dict[str, Any]:
    targets = []
    for target in panel.get("targets", []):
        datasource = normalize_datasource(target.get("datasource") or panel.get("datasource"))
        query_text = target_query_text(target)
        query_language = infer_query_language(query_text, datasource.get("type", ""), panel.get("type", ""))
        targets.append(
            {
                "ref_id": str(target.get("refId") or "").strip(),
                "datasource": datasource,
                "query_text": query_text,
                "query_language": query_language,
            }
        )

    datasource_identities = {
        _datasource_identity(entry["datasource"])
        for entry in targets
        if any(entry["datasource"].values())
    }
    query_languages = {
        entry["query_language"]
        for entry in targets
        if entry["query_language"] and entry["query_language"] != "unknown"
    }

    return {
        "targets": targets,
        "primary": targets[0] if targets else {
            "ref_id": "",
            "datasource": normalize_datasource(panel.get("datasource")),
            "query_text": "",
            "query_language": "unknown",
        },
        "mixed_datasource": len(datasource_identities) > 1 or len(query_languages) > 1,
        "datasource_types": sorted(
            {
                entry["datasource"].get("type", "")
                for entry in targets
                if entry["datasource"].get("type")
            }
        ),
        "query_languages": sorted(query_languages),
    }


def _value_mapping_count(field_config: Any) -> int:
    """Count Grafana value mappings (value -> display text) on a panel.

    Kibana's panel schema has ``ColorValueMapping`` / ``ColorRangeMapping``,
    which assign *colors*; there is no value -> text equivalent, so these
    cannot be translated and must be surfaced rather than dropped silently.
    """
    if not isinstance(field_config, dict):
        return 0
    defaults = field_config.get("defaults")
    if not isinstance(defaults, dict):
        return 0
    mappings = defaults.get("mappings")
    return len(mappings) if isinstance(mappings, list) else 0


def _field_override_properties(field_config: Any) -> list[dict[str, Any]]:
    if not isinstance(field_config, dict):
        return []
    overrides = field_config.get("overrides")
    if not isinstance(overrides, list):
        return []
    properties: list[dict[str, Any]] = []
    for override in overrides:
        if not isinstance(override, dict):
            continue
        override_properties = override.get("properties")
        if not isinstance(override_properties, list):
            continue
        for prop in override_properties:
            if not isinstance(prop, dict):
                continue
            properties.append(prop)
    return properties


def _override_property_is_supported(prop: dict[str, Any]) -> bool:
    property_id = str(prop.get("id") or "").strip()
    if property_id == "color":
        return True
    if property_id == "custom.axisPlacement":
        return str(prop.get("value") or "").strip() == "hidden"
    if property_id == "custom.fillOpacity":
        return True
    if property_id == "custom.stacking":
        value = prop.get("value")
        if isinstance(value, dict):
            return (
                str(value.get("mode") or "").strip() == "normal"
                and value.get("group") is False
            )
        return False
    if property_id == "custom.transform":
        return str(prop.get("value") or "").strip() == "negative-Y"
    return False


def _override_property_is_cosmetic_gap(prop: dict[str, Any]) -> bool:
    """True for unsupported overrides that change legend/tooltip chrome only.

    Grafana's ``custom.hideFrom`` (commonly paired with ``byValue`` /
    ``allIsZero`` to hide empty series from the legend) has no Kibana XY
    equivalent. Surfacing it with the generic "verify visual mappings"
    note Yellows panels that otherwise migrate cleanly (e.g. Redis Total
    Items per DB). Report it as an informational note instead — same
    pattern as value-mapping display-text loss.
    """
    return str(prop.get("id") or "").strip() == "custom.hideFrom"


def collect_panel_inventory(panel: dict[str, Any]) -> dict[str, Any]:
    field_config = panel.get("fieldConfig") or {}
    overrides = field_config.get("overrides") if isinstance(field_config, dict) else []
    override_properties = _field_override_properties(field_config)
    unsupported = [
        prop for prop in override_properties if not _override_property_is_supported(prop)
    ]
    cosmetic = [prop for prop in unsupported if _override_property_is_cosmetic_gap(prop)]
    actionable = [prop for prop in unsupported if not _override_property_is_cosmetic_gap(prop)]
    return {
        "targets": len(panel.get("targets", [])),
        "links": len(panel.get("links", []) or []),
        "transformations": len(panel.get("transformations", []) or []),
        "field_overrides": len(overrides or []),
        "field_override_properties": len(override_properties),
        "non_color_field_override_properties": len(unsupported),
        "actionable_field_override_properties": len(actionable),
        "cosmetic_field_override_properties": len(cosmetic),
        "value_mappings": _value_mapping_count(field_config),
        "has_repeat": bool(panel.get("repeat")),
        "has_library_panel": bool(panel.get("libraryPanel")),
        "has_description": bool(str(panel.get("description") or "").strip()),
    }


def collect_panel_notes(panel: dict[str, Any], panel_analysis: dict[str, Any] | None = None) -> list[str]:
    inventory = collect_panel_inventory(panel)
    notes = []
    if inventory["links"]:
        notes.append(f"Grafana panel has {inventory['links']} link(s); verify drilldowns manually")
    if inventory["transformations"]:
        notes.append(f"Grafana panel has {inventory['transformations']} transformation(s); manual review recommended")
    if inventory["actionable_field_override_properties"]:
        notes.append(
            "Grafana panel has "
            f"{inventory['field_overrides']} field override(s) including "
            f"{inventory['actionable_field_override_properties']} non-color override property "
            "(e.g. stacking, transforms); verify visual mappings manually"
        )
    elif inventory["cosmetic_field_override_properties"]:
        notes.append(
            "Grafana hide-from-legend/tooltip field override(s) are not applied in Kibana; "
            "matching series still appear in the legend"
        )
    if inventory["value_mappings"]:
        notes.append(
            f"Grafana panel has {inventory['value_mappings']} value mapping(s) "
            "(e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, "
            "not display text, so the raw value is shown instead"
        )
    if inventory["has_repeat"]:
        notes.append("Grafana repeating panel behavior is not preserved automatically")
    if inventory["has_library_panel"]:
        notes.append("Grafana library panel reference detected; verify source ownership manually")
    if panel_analysis and panel_analysis.get("mixed_datasource"):
        notes.append("Panel mixes datasource or query-language types and needs manual redesign")
    return notes


def build_dashboard_inventory(dashboard: dict[str, Any]) -> dict[str, Any]:
    annotations = dashboard.get("annotations") or {}
    annotation_list = annotations.get("list", []) if isinstance(annotations, dict) else []
    templating = dashboard.get("templating") or {}
    variables = templating.get("list", []) if isinstance(templating, dict) else []
    datasource_variables: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for var in variables or []:
        if not isinstance(var, dict):
            continue
        if str(var.get("type") or "").strip().lower() != "datasource":
            continue
        name = str(var.get("name") or "").strip()
        if not name:
            continue
        seen_names.add(name)
        datasource_variables.append({
            "name": name,
            "type": str(var.get("query") or "").strip().lower(),
        })
    # Grafana "Export for sharing externally" declares ${DS_*} in __inputs
    # (name → pluginId), not templating.list. Fill gaps so marketplace
    # dashboards resolve instead of becoming unresolved blockers.
    for item in dashboard.get("__inputs") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip().lower() != "datasource":
            continue
        name = str(item.get("name") or "").strip()
        plugin_id = str(item.get("pluginId") or "").strip().lower()
        if not name or name in seen_names or not plugin_id:
            continue
        seen_names.add(name)
        datasource_variables.append({"name": name, "type": plugin_id})
    return {
        "links": len(dashboard.get("links", []) or []),
        "annotations": len(annotation_list or []),
        "variables": len(variables or []),
        "datasource_variables": datasource_variables,
        "rows": len(dashboard.get("rows", []) or []),
        "panels": len(dashboard.get("panels", []) or []),
        "folder_title": str((dashboard.get("_grafana_meta") or {}).get("folderTitle") or ""),
    }


def classify_panel_readiness(panel_result: Any) -> str:
    if getattr(panel_result, "status", "") == "skipped":
        return "ignored"
    if getattr(panel_result, "status", "") in {"not_feasible", "requires_manual"}:
        return "manual_only"

    query_language = str(getattr(panel_result, "query_language", "") or "").lower()
    datasource_type = str(getattr(panel_result, "datasource_type", "") or "").lower()
    notes = [str(note).lower() for note in (getattr(panel_result, "notes", []) or [])]
    if any("mixes datasource" in note for note in notes):
        return "manual_only"
    query_ir = getattr(panel_result, "query_ir", {}) or {}
    query_family = ""
    if isinstance(query_ir, dict):
        query_family = str(query_ir.get("family", "") or "").lower()
    if query_language == "esql":
        return "elastic_ready"
    if query_language == "promql" and (
        query_family == "native_promql"
        or any("native promql" in note for note in notes)
    ):
        return "elastic_ready"
    if datasource_type == "elasticsearch":
        return "elastic_native_review"
    if query_language == "logql":
        return "logs_fielding_needed"
    if query_language == "promql":
        return "metrics_mapping_needed"
    if query_language == "text":
        return "ready"
    if getattr(panel_result, "status", "") == "migrated":
        return "ready"
    return "review"


def recommend_panel_target(panel_result: Any) -> str:
    query_language = str(getattr(panel_result, "query_language", "") or "").lower()
    grafana_type = str(getattr(panel_result, "grafana_type", "") or "").lower()
    kibana_type = str(getattr(panel_result, "kibana_type", "") or "").lower()
    status = str(getattr(panel_result, "status", "") or "").lower()
    if status == "skipped":
        return "skip"
    if status in {"not_feasible", "requires_manual"}:
        return "manual_redesign"
    if grafana_type == "text":
        return "markdown"
    if query_language == "esql":
        return "native_esql_panel"
    if query_language == "logql":
        return "discover_embed" if grafana_type == "logs" else "esql_table"
    if kibana_type == "datatable":
        return "esql_table"
    if kibana_type == "metric":
        return "native_metric_panel"
    if kibana_type == "gauge":
        return "native_metric_panel"
    if kibana_type == "pie":
        return "native_esql_or_lens"
    if kibana_type in {"bar", "line", "area", "heatmap"}:
        return "native_esql_or_lens"
    if grafana_type in {"table", "table-old"}:
        return "esql_table"
    if grafana_type in {"stat", "singlestat", "gauge", "bargauge"}:
        return "native_metric_panel"
    return "native_esql_or_lens"


def build_migration_manifest(results: list[Any]) -> dict[str, Any]:
    dashboards = []
    flat_panels = []
    all_dashboard_links = []
    all_panel_links = []
    all_annotations = []
    all_alert_tasks = []
    all_transform_tasks = []
    runtime_features: dict[str, Any] = {}
    for result in results:
        runtime_features.update(dict(getattr(result, "runtime_features", {}) or {}))
        runtime_summary = build_runtime_summary(result)
        result.runtime_summary = runtime_summary
        dashboard_links = list(getattr(result, "dashboard_links", []) or [])
        annotations = list(getattr(result, "annotations", []) or [])
        alert_tasks = list(getattr(result, "alert_migration_tasks", []) or [])
        dashboard_entry = {
            "title": getattr(result, "dashboard_title", ""),
            "uid": getattr(result, "dashboard_uid", ""),
            "source_file": getattr(result, "source_file", ""),
            "folder_title": getattr(result, "folder_title", ""),
            "artifact_stem": getattr(result, "artifact_stem", ""),
            "native_artifact_path": getattr(result, "native_artifact_path", ""),
            "ir_artifact_path": getattr(result, "ir_artifact_path", ""),
            "uploaded_space": getattr(result, "uploaded_space", ""),
            "uploaded_kibana_url": getattr(result, "uploaded_kibana_url", ""),
            "curated_pack": getattr(result, "curated_pack", "") or "",
            "runtime_summary": runtime_summary,
            "inventory": getattr(result, "inventory", {}) or {},
            "metadata_polish": getattr(result, "metadata_polish", {}) or {},
            "verification_summary": getattr(result, "verification_summary", {}) or {},
            "review_explanations": getattr(result, "review_explanations", {}) or {},
            "feature_gap_summary": getattr(result, "feature_gap_summary", {}) or {},
            "control_warnings": list(getattr(result, "control_warnings", []) or []),
            "dashboard_links": dashboard_links,
            "annotations": annotations,
            "alert_migration_tasks": alert_tasks,
            "panels": [],
        }
        all_dashboard_links.extend(dashboard_links)
        all_annotations.extend(annotations)
        all_alert_tasks.extend(alert_tasks)
        for panel_result in getattr(result, "panel_results", []) or []:
            link_migrations = list(getattr(panel_result, "link_migrations", []) or [])
            transformation_tasks = list(getattr(panel_result, "transformation_redesign_tasks", []) or [])
            panel_entry = {
                "dashboard_title": getattr(result, "dashboard_title", ""),
                "dashboard_uid": getattr(result, "dashboard_uid", ""),
                "source_panel_id": getattr(panel_result, "source_panel_id", ""),
                "title": getattr(panel_result, "title", ""),
                "grafana_type": getattr(panel_result, "grafana_type", ""),
                "kibana_type": getattr(panel_result, "kibana_type", ""),
                "status": getattr(panel_result, "status", ""),
                "confidence": getattr(panel_result, "confidence", 0.0),
                "datasource_type": getattr(panel_result, "datasource_type", ""),
                "datasource_uid": getattr(panel_result, "datasource_uid", ""),
                "datasource_name": getattr(panel_result, "datasource_name", ""),
                "query_language": getattr(panel_result, "query_language", ""),
                "readiness": getattr(panel_result, "readiness", ""),
                "recommended_target": getattr(panel_result, "recommended_target", ""),
                "reasons": list(getattr(panel_result, "reasons", []) or []),
                "notes": list(getattr(panel_result, "notes", []) or []),
                "inventory": dict(getattr(panel_result, "inventory", {}) or {}),
                "query_ir": dict(getattr(panel_result, "query_ir", {}) or {}),
                "visual_ir": _ir_to_dict(getattr(panel_result, "visual_ir", {})),
                "operational_ir": _ir_to_dict(getattr(panel_result, "operational_ir", {})),
                "metadata_polish": dict(getattr(panel_result, "metadata_polish", {}) or {}),
                "target_candidates": list(getattr(panel_result, "target_candidates", []) or []),
                "verification_packet": dict(getattr(panel_result, "verification_packet", {}) or {}),
                "review_explanation": dict(getattr(panel_result, "review_explanation", {}) or {}),
                "runtime_rollups": list(getattr(panel_result, "runtime_rollups", []) or []),
                "link_migrations": link_migrations,
                "transformation_redesign_tasks": transformation_tasks,
            }
            dashboard_entry["panels"].append(panel_entry)
            flat_panels.append(panel_entry)
            all_panel_links.extend(link_migrations)
            all_transform_tasks.extend(transformation_tasks)
        dashboards.append(dashboard_entry)

    links_summary = {
        "dashboard_links": len(all_dashboard_links),
        "panel_links": len(all_panel_links),
        "url_drilldowns": sum(
            1
            for item in [*all_dashboard_links, *all_panel_links]
            if item.get("kibana_action") == "url_drilldown"
        ),
        "dashboard_drilldowns": sum(
            1 for item in all_panel_links if item.get("kibana_action") == "dashboard_drilldown"
        ),
        "manual_wiring_needed": sum(
            1
            for item in [*all_dashboard_links, *all_panel_links]
            if item.get("kibana_action") not in {"url_drilldown", "dashboard_drilldown"}
        ),
    }
    annotations_summary = {
        "total": len(all_annotations),
        "auto_translated": sum(1 for item in all_annotations if item.get("kibana_action") == "event_annotation"),
        "candidate_event_annotations": sum(
            1 for item in all_annotations if item.get("kibana_action") == "candidate_event_annotation"
        ),
        "manual_needed": sum(1 for item in all_annotations if item.get("kibana_action") == "manual_annotation"),
        "unsupported": sum(1 for item in all_annotations if item.get("kibana_action") == "unsupported"),
    }
    transformation_summary: dict[str, Any] = {"total": len(all_transform_tasks), "by_complexity": {}, "by_type": {}}
    for item in all_transform_tasks:
        complexity = str(item.get("complexity", "high"))
        transform_id = str(item.get("transform_id", "unknown"))
        transformation_summary["by_complexity"][complexity] = transformation_summary["by_complexity"].get(complexity, 0) + 1
        transformation_summary["by_type"][transform_id] = transformation_summary["by_type"].get(transform_id, 0) + 1
    alert_summary: dict[str, Any] = {"total": len(all_alert_tasks), "by_type": {}, "by_kibana_type": {}}
    for item in all_alert_tasks:
        alert_type = str(item.get("alert_type", "unknown"))
        kibana_type = str(item.get("suggested_kibana_rule_type", "unknown"))
        alert_summary["by_type"][alert_type] = alert_summary["by_type"].get(alert_type, 0) + 1
        alert_summary["by_kibana_type"][kibana_type] = alert_summary["by_kibana_type"].get(kibana_type, 0) + 1

    return {
        "summary": {
            "dashboards": len(dashboards),
            "panels": len(flat_panels),
            "migrated": sum(1 for panel in flat_panels if panel["status"] == "migrated"),
            "migrated_with_warnings": sum(1 for panel in flat_panels if panel["status"] == "migrated_with_warnings"),
            "requires_manual": sum(1 for panel in flat_panels if panel["status"] == "requires_manual"),
            "not_feasible": sum(1 for panel in flat_panels if panel["status"] == "not_feasible"),
            # Gate tallies exclude skipped panels (rows / non-migrated) so Green
            # matches the Migrated scorecard rather than the raw element count.
            "green": sum(
                1
                for panel in flat_panels
                if panel.get("status") != "skipped"
                and (panel.get("verification_packet") or {}).get("semantic_gate") == "Green"
            ),
            "yellow": sum(
                1
                for panel in flat_panels
                if panel.get("status") != "skipped"
                and (panel.get("verification_packet") or {}).get("semantic_gate") == "Yellow"
            ),
            "red": sum(
                1
                for panel in flat_panels
                if panel.get("status") != "skipped"
                and (panel.get("verification_packet") or {}).get("semantic_gate") == "Red"
            ),
            "control_warnings": sum(
                len(dashboard.get("control_warnings", []))
                for dashboard in dashboards
            ),
            "uploaded_ok": sum(1 for dashboard in dashboards if (dashboard.get("runtime_summary", {}).get("upload") or {}).get("status") == "pass"),
            "layout_ok": sum(1 for dashboard in dashboards if (dashboard.get("runtime_summary", {}).get("layout") or {}).get("status") == "pass"),
        },
        "feature_gaps": {
            "links": links_summary,
            "annotations": annotations_summary,
            "transformation_redesign": transformation_summary,
            "alert_migration": alert_summary,
        },
        "runtime_features": runtime_features,
        "dashboards": dashboards,
        "panels": flat_panels,
    }


def save_migration_manifest(results: list[Any], output_path: str | Path) -> None:
    output_path = Path(output_path)
    with output_path.open("w") as fh:
        json.dump(build_migration_manifest(results), fh, indent=2)
