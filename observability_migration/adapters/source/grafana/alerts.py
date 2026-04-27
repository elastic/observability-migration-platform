"""Extract Grafana legacy alert rules and model them as migration tasks.

Grafana alerting and Kibana alerting have fundamentally different models:

- **Grafana legacy alerts** are embedded in panels (``panel.alert``).
- **Kibana alerting** uses rule types (ES query, threshold, anomaly detection)
  tied to connectors and actions.

This module extracts legacy alert definitions from dashboard JSON and produces
structured migration task descriptors that tell the customer exactly what
needs to be recreated in Kibana.
"""

from __future__ import annotations

from typing import Any

from .manifest import analyze_panel_targets


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _extract_legacy_alert(panel: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a Grafana legacy alert from a panel's ``alert`` field."""
    alert = panel.get("alert")
    if not alert or not isinstance(alert, dict):
        return None

    name = str(alert.get("name", "") or panel.get("title", "") or "")
    message = str(alert.get("message", "") or "")
    frequency = str(alert.get("frequency", "") or "")
    pending_for = str(alert.get("for", "") or "")
    conditions = alert.get("conditions") or []
    notifications = alert.get("notifications") or []
    no_data_state = str(alert.get("noDataState", "") or "")
    exec_error_state = str(alert.get("executionErrorState", "") or "")

    parsed_conditions: list[dict[str, Any]] = []
    for cond in conditions:
        if not isinstance(cond, dict):
            continue
        evaluator = cond.get("evaluator") or {}
        operator_info = cond.get("operator") or {}
        query_info = cond.get("query") or {}
        reducer_info = cond.get("reducer") or {}
        parsed_conditions.append({
            "evaluator_type": evaluator.get("type", ""),
            "evaluator_params": evaluator.get("params", []),
            "operator": operator_info.get("type", ""),
            "query_ref": query_info.get("params", [""])[0] if query_info.get("params") else "",
            "query_from": query_info.get("params", ["", ""])[1] if len(query_info.get("params", [])) > 1 else "",
            "query_to": query_info.get("params", ["", "", ""])[2] if len(query_info.get("params", [])) > 2 else "",
            "reducer": reducer_info.get("type", ""),
        })

    notification_channels = [
        str(n.get("uid", "") or n.get("id", "") or "")
        for n in notifications if isinstance(n, dict)
    ]

    return {
        "alert_type": "legacy",
        "name": name,
        "message": message,
        "frequency": frequency,
        "pending_for": pending_for,
        "no_data_state": no_data_state,
        "exec_error_state": exec_error_state,
        "conditions": parsed_conditions,
        "notification_channels": notification_channels,
    }


def _iter_dashboard_panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    panels: list[dict[str, Any]] = []
    for panel in dashboard.get("panels", []) or []:
        panels.append(panel)
        panels.extend(panel.get("panels", []) or [])
    for row in dashboard.get("rows", []) or []:
        panels.extend(row.get("panels", []) or [])
    return panels


def extract_alerts_from_dashboard(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract legacy panel-embedded alert definitions from a dashboard."""
    alerts: list[dict[str, Any]] = []
    dashboard_title = str(dashboard.get("title", "") or "")
    dashboard_uid = str(dashboard.get("uid", "") or "")

    all_panels = _iter_dashboard_panels(dashboard)
    for panel in all_panels:
        legacy = _extract_legacy_alert(panel)
        if legacy:
            panel_target_info = analyze_panel_targets(panel)
            source_queries = []
            datasource_map: dict[str, dict[str, str]] = {}
            for target in panel_target_info.get("targets", []) or []:
                if not isinstance(target, dict):
                    continue
                datasource = target.get("datasource") if isinstance(target.get("datasource"), dict) else {}
                expr = str(target.get("query_text", "") or "")
                if not expr:
                    continue
                datasource_uid = str(datasource.get("uid", "") or "")
                datasource_name = str(datasource.get("name", "") or "")
                datasource_key = datasource_uid or datasource_name or str(datasource.get("type", "") or "")
                source_queries.append(
                    {
                        "ref_id": str(target.get("ref_id", "") or ""),
                        "datasource_uid": datasource_uid,
                        "datasource_type": str(datasource.get("type", "") or ""),
                        "datasource_name": datasource_name,
                        "expr": expr,
                    }
                )
                if datasource_key:
                    datasource_map[datasource_key] = {
                        "uid": datasource_uid,
                        "type": str(datasource.get("type", "") or ""),
                        "name": datasource_name,
                    }
            legacy["source_panel_title"] = str(panel.get("title", "") or "")
            legacy["source_panel_id"] = str(panel.get("id", "") or "")
            legacy["dashboard_title"] = dashboard_title
            legacy["dashboard_uid"] = dashboard_uid
            legacy["source_queries"] = source_queries
            legacy["datasource_map"] = datasource_map
            alerts.append(legacy)

    return alerts


def _suggest_kibana_rule_type(alert: dict[str, Any]) -> str:
    """Suggest the closest Kibana rule type for a Grafana alert."""
    conditions = alert.get("conditions") or []
    if not conditions:
        return "threshold"
    for cond in conditions:
        eval_type = str(cond.get("evaluator_type", "")).lower()
        if eval_type in ("gt", "lt", "within_range", "outside_range"):
            return "threshold"
    return "es_query"


def build_alert_migration_tasks(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert extracted alerts into structured migration task descriptors."""
    tasks: list[dict[str, Any]] = []
    for alert in alerts:
        suggested_type = _suggest_kibana_rule_type(alert)
        conditions_desc = []
        for cond in alert.get("conditions", []):
            reducer = cond.get("reducer", "")
            eval_type = cond.get("evaluator_type", "")
            params = cond.get("evaluator_params", [])
            param_str = ", ".join(str(p) for p in params)
            conditions_desc.append(
                f"{reducer}() {eval_type} [{param_str}] on ref {cond.get('query_ref', '?')}"
            )

        tasks.append({
            "task_type": "alert_migration",
            "dashboard": alert.get("dashboard_title", ""),
            "dashboard_uid": alert.get("dashboard_uid", ""),
            "panel": alert.get("source_panel_title", ""),
            "alert_name": alert.get("name", ""),
            "alert_type": alert.get("alert_type", "legacy"),
            "suggested_kibana_rule_type": suggested_type,
            "frequency": alert.get("frequency", ""),
            "pending_for": alert.get("pending_for", ""),
            "no_data_state": alert.get("no_data_state", ""),
            "exec_error_state": alert.get("exec_error_state", ""),
            "conditions": alert.get("conditions", []),
            "conditions_description": conditions_desc,
            "notification_channels": alert.get("notification_channels", []),
            "source_queries": list(alert.get("source_queries", []) or []),
            "datasource_map": dict(alert.get("datasource_map", {}) or {}),
            "description": (
                f"Alert '{alert.get('name', '')}' on panel '{alert.get('source_panel_title', '')}' "
                f"→ Kibana {suggested_type} rule. "
                f"Conditions: {'; '.join(conditions_desc) or 'none extracted'}. "
                f"Notifications to: {', '.join(alert.get('notification_channels', [])) or 'none'}."
            ),
        })
    return tasks


def build_alert_summary(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize alert migration tasks."""
    if not tasks:
        return {"total": 0, "by_type": {}, "by_kibana_type": {}}

    by_type: dict[str, int] = {}
    by_kibana: dict[str, int] = {}
    for task in tasks:
        t = task.get("alert_type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1
        k = task.get("suggested_kibana_rule_type", "unknown")
        by_kibana[k] = by_kibana.get(k, 0) + 1

    return {
        "total": len(tasks),
        "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
        "by_kibana_type": dict(sorted(by_kibana.items(), key=lambda x: -x[1])),
    }


def build_alert_migration_results(
    alert_irs: list[Any],
    *,
    total_alerts: int | None = None,
    total_legacy: int | None = None,
    total_unified: int | None = None,
) -> dict[str, Any]:
    """Build the raw alert migration results artifact."""
    by_tier: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_target_rule_type: dict[str, int] = {}
    by_selected_target_rule_type: dict[str, int] = {}

    for ir in alert_irs:
        tier = str(getattr(ir, "automation_tier", "") or "")
        if tier:
            by_tier[tier] = by_tier.get(tier, 0) + 1

        kind = str(getattr(ir, "kind", "") or "")
        if kind:
            by_kind[kind] = by_kind.get(kind, 0) + 1

        target_rule_type = str(getattr(ir, "target_rule_type", "") or "")
        if target_rule_type:
            by_target_rule_type[target_rule_type] = by_target_rule_type.get(target_rule_type, 0) + 1

        selected_target_rule_type = str(getattr(ir, "selected_target_rule_type", "") or "")
        if selected_target_rule_type:
            by_selected_target_rule_type[selected_target_rule_type] = (
                by_selected_target_rule_type.get(selected_target_rule_type, 0) + 1
            )

    resolved_total = len(alert_irs) if total_alerts is None else total_alerts
    resolved_legacy = (
        sum(1 for ir in alert_irs if str(getattr(ir, "kind", "") or "") == "grafana_legacy")
        if total_legacy is None
        else total_legacy
    )
    resolved_unified = (
        sum(1 for ir in alert_irs if str(getattr(ir, "kind", "") or "") == "grafana_unified")
        if total_unified is None
        else total_unified
    )

    return {
        "total": resolved_total,
        "legacy_alerts": resolved_legacy,
        "unified_alerts": resolved_unified,
        "by_automation_tier": by_tier,
        "by_target_rule_type": by_target_rule_type,
        "by_selected_target_rule_type": by_selected_target_rule_type,
        "by_kind": by_kind,
        "alerts": [ir.to_dict() for ir in alert_irs],
    }


def build_alert_comparison_results(
    raw_alerts: list[dict[str, Any]],
    alert_irs: list[Any],
    mapping_batch: dict[str, Any],
    *,
    payload_validation_by_alert_id: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a review-friendly source-vs-target artifact for Grafana alerts."""
    mapping_results = mapping_batch.get("results", []) if isinstance(mapping_batch, dict) else []
    summary = mapping_batch.get("summary", {}) if isinstance(mapping_batch, dict) else {}
    payload_validation_lookup = payload_validation_by_alert_id or {}

    alerts: list[dict[str, Any]] = []
    by_kind: dict[str, int] = {}

    for idx, ir in enumerate(alert_irs):
        raw = raw_alerts[idx] if idx < len(raw_alerts) and isinstance(raw_alerts[idx], dict) else {}
        mapping_entry = mapping_results[idx]["mapping"] if idx < len(mapping_results) else {}
        target_payload = mapping_entry.get("rule_payload", {}) if isinstance(mapping_entry, dict) else {}
        validation_errors = list(mapping_entry.get("validation_errors", []) or []) if isinstance(mapping_entry, dict) else []
        automation_tier = mapping_entry.get("automation_tier", "") if isinstance(mapping_entry, dict) else ""
        query = str(getattr(ir, "translated_query", "") or "")
        if not query:
            query = str(
                (((target_payload or {}).get("params") or {}).get("esqlQuery") or {}).get("esql", "")
                or ""
            )
        provenance = str(getattr(ir, "translated_query_provenance", "") or "")
        if not provenance and query.startswith("PROMQL "):
            provenance = "native_promql"

        source_queries = []
        if isinstance(getattr(ir, "source_extension", {}), dict):
            source_queries = list(getattr(ir, "source_extension", {}).get("source_queries", []) or [])

        primary_expr = ""
        if source_queries and isinstance(source_queries[0], dict):
            primary_expr = str(source_queries[0].get("expr", "") or "")

        blocked_reasons: list[str] = []
        if automation_tier == "manual_required":
            for warning in getattr(ir, "warnings", []) or []:
                _append_unique(blocked_reasons, str(warning))
        for error in validation_errors:
            _append_unique(blocked_reasons, str(error))

        if getattr(ir, "kind", "") == "grafana_legacy":
            source = {
                "type": "legacy",
                "dashboard": raw.get("dashboard", ""),
                "panel": raw.get("panel", ""),
                "query": primary_expr,
                "condition": "; ".join(raw.get("conditions_description", []) or []),
                "frequency": raw.get("frequency", ""),
                "pending_for": raw.get("pending_for", ""),
                "no_data_state": raw.get("no_data_state", ""),
                "exec_error_state": raw.get("exec_error_state", ""),
                "notification_channels": list(raw.get("notification_channels", []) or []),
                "source_queries": source_queries,
            }
        else:
            source = {
                "type": "unified",
                "query": primary_expr,
                "condition": raw.get("condition", "") or getattr(ir, "metadata", {}).get("grafana_condition", ""),
                "pending_for": raw.get("for", "") or getattr(ir, "pending_period", ""),
                "no_data_state": raw.get("noDataState", "") or getattr(ir, "no_data_policy", ""),
                "exec_error_state": raw.get("execErrState", "") or getattr(ir, "metadata", {}).get("exec_err_state", ""),
                "labels": dict(raw.get("labels", {}) or {}),
                "annotations": dict(raw.get("annotations", {}) or {}),
                "rule_group": raw.get("ruleGroup", ""),
                "folder_uid": raw.get("folderUID", ""),
                "source_queries": source_queries,
            }

        alerts.append(
            {
                "alert_id": getattr(ir, "alert_id", ""),
                "name": getattr(ir, "name", ""),
                "kind": getattr(ir, "kind", ""),
                "source": source,
                "translation": {
                    "query": query,
                    "provenance": provenance,
                    "warnings": list(getattr(ir, "warnings", []) or []),
                    "group_by": list(getattr(ir, "group_by", []) or []),
                },
                "target": {
                    "automation_tier": automation_tier,
                    "target_rule_type": mapping_entry.get("target_rule_type", "") if isinstance(mapping_entry, dict) else "",
                    "selected_target_rule_type": (
                        mapping_entry.get("selected_target_rule_type", "") if isinstance(mapping_entry, dict) else ""
                    ),
                    "payload_emitted": (
                        bool(mapping_entry.get("payload_emitted")) if isinstance(mapping_entry, dict) else bool(target_payload)
                    ),
                    "payload_status": mapping_entry.get("payload_status", "") if isinstance(mapping_entry, dict) else "",
                    "payload_status_reason": (
                        mapping_entry.get("payload_status_reason", "") if isinstance(mapping_entry, dict) else ""
                    ),
                    "review_gates": (
                        dict(mapping_entry.get("review_gates", {}) or {}) if isinstance(mapping_entry, dict) else {}
                    ),
                    "rule_payload": target_payload,
                    "valid": bool(mapping_entry.get("valid")) if isinstance(mapping_entry, dict) else False,
                    "validation_errors": validation_errors,
                    "payload_validation": payload_validation_lookup.get(str(getattr(ir, "alert_id", "") or ""), {}),
                },
                "semantic_losses": list(mapping_entry.get("losses", []) or []) if isinstance(mapping_entry, dict) else [],
                "blocked_reasons": blocked_reasons,
            }
        )
        kind = str(getattr(ir, "kind", "") or "")
        by_kind[kind] = by_kind.get(kind, 0) + 1

    return {
        "total": len(alerts),
        "summary": {
            "by_automation_tier": dict(summary.get("by_automation_tier", {}) or {}),
            "by_target_rule_type": dict(summary.get("by_target_rule_type", {}) or {}),
            "by_selected_target_rule_type": dict(summary.get("by_selected_target_rule_type", {}) or {}),
            "by_kind": by_kind,
        },
        "alerts": alerts,
    }


__all__ = [
    "build_alert_comparison_results",
    "build_alert_migration_results",
    "build_alert_migration_tasks",
    "build_alert_summary",
    "extract_alerts_from_dashboard",
]
