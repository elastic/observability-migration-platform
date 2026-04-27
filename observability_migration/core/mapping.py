# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Rule-mapping engine for alert/monitor migration.

Takes AlertingIR instances and produces Kibana rule payloads for safely
mappable cases, or downgrades the automation tier and records semantic
losses for cases that cannot be automatically translated.
"""

from __future__ import annotations

import re
from typing import Any

from observability_migration.core.assets.alerting import AlertingIR
from observability_migration.core.assets.status import AssetStatus

# ---- Kibana rule type IDs ----
ES_QUERY_RULE_TYPE = ".es-query"
INDEX_THRESHOLD_RULE_TYPE = ".index-threshold"
CUSTOM_THRESHOLD_RULE_TYPE = "observability.rules.custom_threshold"

# ---- Fidelity classification ----

AUTOMATED_KINDS = {"grafana_legacy", "datadog_metric"}
DRAFT_REVIEW_KINDS = {"grafana_unified", "datadog_log"}
MANUAL_ONLY_KINDS = {
    "datadog_composite",
    "datadog_service_check",
    # Keep both historic short names and the current _datadog_kind() outputs.
    "datadog_event",
    "datadog_event_alert",
    "datadog_rum",
    "datadog_rum_alert",
    "datadog_apm",
    "datadog_apm_alert",
    "datadog_synthetics",
    "datadog_synthetics_alert",
    "datadog_ci",
    "datadog_ci_alert",
    "datadog_slo",
    "datadog_slo_alert",
    "datadog_audit",
    "datadog_audit_alert",
    "datadog_cost",
    "datadog_cost_alert",
    "datadog_network",
    "datadog_network_alert",
    "datadog_watchdog",
    "datadog_watchdog_alert",
    "datadog_forecast",
    "datadog_outlier",
    "datadog_anomaly_alert",
}


def classify_automation_tier(ir: AlertingIR) -> str:
    """Determine the automation tier for an alert IR.

    Returns one of: "automated", "draft_requires_review", "manual_required".
    """
    if ir.kind in MANUAL_ONLY_KINDS:
        return "manual_required"

    if ir.kind == "grafana_legacy":
        if _has_source_faithful_query(ir) and _has_simple_threshold_condition(ir):
            return "automated"
        return "manual_required"

    if ir.kind == "grafana_unified":
        if _grafana_unified_is_strict_exact_query_subset(ir):
            return "automated"
        if _has_source_faithful_query(ir):
            return "draft_requires_review"
        return "manual_required"

    if ir.kind == "datadog_metric":
        if not _has_source_faithful_query(ir):
            return "manual_required"
        if ir.warnings:
            return "manual_required"
        if _has_simple_threshold_condition(ir):
            return "automated"
        return "draft_requires_review"

    if ir.kind == "datadog_log":
        if ir.warnings:
            return "manual_required"
        if _has_source_faithful_query(ir):
            return "draft_requires_review"
        return "manual_required"

    if ir.kind in DRAFT_REVIEW_KINDS:
        return "draft_requires_review"

    return "manual_required"


def _has_simple_threshold_condition(ir: AlertingIR) -> bool:
    """Check if the alert has a simple threshold condition amenable to automation."""
    ext = ir.source_extension or {}

    if ir.kind == "grafana_legacy":
        conditions = ext.get("conditions") if isinstance(ext.get("conditions"), list) else []
        if not conditions:
            alert_type = ext.get("alert_type", "")
            return alert_type == "legacy"
        if len(conditions) != 1:
            return False
        condition = conditions[0]
        return isinstance(condition, dict) and bool(_legacy_condition_where_clause(condition))

    if ir.kind == "datadog_metric":
        query = ir.condition_summary or ""
        if "formula(" in query.lower() or "||" in query or "&&" in query:
            return False
        return True

    return False


def _normalized_no_data_policy(value: str) -> str:
    return str(value or "").strip().lower()


def _grafana_unified_no_data_is_exact(ir: AlertingIR) -> bool:
    return _normalized_no_data_policy(ir.no_data_policy) in {"", "ok"}


def _grafana_safe_label_tags(labels: Any) -> list[str] | None:
    if labels is None:
        return []
    if not isinstance(labels, dict):
        return None

    tags: list[str] = []
    for key, value in sorted(labels.items()):
        key_text = str(key or "").strip()
        value_text = str(value or "").strip()
        if not key_text or not value_text:
            return None
        if any(token in key_text or token in value_text for token in ("{{", "}}", "{", "}")):
            return None
        tags.append(f"grafana_label:{key_text}={value_text}")
    return tags


def _grafana_safe_dashboard_link_tags(annotations: Any) -> list[str] | None:
    if annotations is None:
        return []
    if not isinstance(annotations, dict):
        return None

    dashboard_uid = str(annotations.get("__dashboardUid__", "") or "").strip()
    panel_id = str(annotations.get("__panelId__", "") or "").strip()
    if not dashboard_uid and not panel_id:
        return []
    if not dashboard_uid:
        return None
    if any(token in dashboard_uid or token in panel_id for token in ("{{", "}}", "$", "{", "}")):
        return None

    tags = [f"grafana_dashboard_uid:{dashboard_uid}"]
    if panel_id:
        if not re.fullmatch(r"\d+", panel_id):
            return None
        tags.append(f"grafana_panel_id:{panel_id}")
    return tags


def _grafana_unified_review_gates(ir: AlertingIR) -> dict[str, bool]:
    if ir.kind != "grafana_unified":
        return {}

    ext = ir.source_extension or {}
    source_queries = ext.get("source_queries")
    data = ext.get("data")
    annotations = ext.get("annotations")
    translated_provenance = str(
        ir.translated_query_provenance or ir.metadata.get("translated_query_provenance", "")
    ).strip().lower()

    gates = {
        "source_faithful_query": _has_source_faithful_query(ir),
        "supported_provenance": (
            not translated_provenance or translated_provenance in {"native_promql", "translated_esql"}
        ),
        "exact_no_data_policy": _grafana_unified_no_data_is_exact(ir),
        "explicit_threshold": _has_explicit_threshold(ir),
        "single_source_query": isinstance(source_queries, list) and len(source_queries) == 1,
        "simple_expression_graph": isinstance(data, list) and not _grafana_unified_has_complex_expression_graph(data),
        "static_labels": _grafana_safe_label_tags(ext.get("labels")) is not None,
        "dashboard_link_safe": not (
            isinstance(annotations, dict)
            and (annotations.get("__dashboardUid__") or annotations.get("__panelId__"))
        ),
    }
    non_no_data_gates = [
        "source_faithful_query",
        "supported_provenance",
        "explicit_threshold",
        "single_source_query",
        "simple_expression_graph",
        "static_labels",
        "dashboard_link_safe",
    ]
    gates["no_data_only_blocks_strict_automation"] = (
        not gates["exact_no_data_policy"] and all(gates[key] for key in non_no_data_gates)
    )
    gates["strict_subset_ready"] = gates["exact_no_data_policy"] and all(
        gates[key] for key in non_no_data_gates
    )
    return gates


def _grafana_unified_is_strict_exact_query_subset(ir: AlertingIR) -> bool:
    gates = _grafana_unified_review_gates(ir)
    return bool(gates and gates.get("strict_subset_ready"))


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _legacy_condition_where_clause(condition: dict[str, Any]) -> str:
    eval_type = str(condition.get("evaluator_type", "")).lower()
    params = condition.get("evaluator_params", []) if isinstance(condition.get("evaluator_params"), list) else []
    comp_map = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
    if eval_type in comp_map:
        threshold = _coerce_float(params[0] if params else None)
        if threshold is None:
            return ""
        return f"value {comp_map[eval_type]} {threshold}"

    if eval_type not in {"within_range", "outside_range"} or len(params) < 2:
        return ""
    lower = _coerce_float(params[0])
    upper = _coerce_float(params[1])
    if lower is None or upper is None or lower > upper:
        return ""
    if eval_type == "within_range":
        return f"value >= {lower} AND value <= {upper}"
    return f"value < {lower} OR value > {upper}"


def _primary_source_query(ir: AlertingIR) -> dict[str, str]:
    """Return the first non-expression source query for an alert, if present."""
    ext = ir.source_extension or {}
    queries = ext.get("source_queries")
    if isinstance(queries, list):
        for item in queries:
            if not isinstance(item, dict):
                continue
            expr = str(item.get("expr", "") or "")
            if not expr:
                continue
            return {
                "expr": expr,
                "datasource_uid": str(item.get("datasource_uid", "") or ""),
                "datasource_type": str(item.get("datasource_type", "") or ""),
                "datasource_name": str(item.get("datasource_name", "") or ""),
            }

    data_list = ext.get("data")
    if not isinstance(data_list, list):
        return {}

    raw_datasource_map = ext.get("datasource_map")
    datasource_map: dict[str, Any] = raw_datasource_map if isinstance(raw_datasource_map, dict) else {}
    for item in data_list:
        if not isinstance(item, dict):
            continue
        datasource_uid = str(item.get("datasourceUid", "") or "")
        if not datasource_uid or datasource_uid == "__expr__":
            continue
        raw_model = item.get("model")
        model = raw_model if isinstance(raw_model, dict) else {}
        expr = str(model.get("expr", "") or "")
        raw_ds_meta = datasource_map.get(datasource_uid)
        ds_meta = raw_ds_meta if isinstance(raw_ds_meta, dict) else {}
        if expr:
            return {
                "expr": expr,
                "datasource_uid": datasource_uid,
                "datasource_type": str(ds_meta.get("type", "") or ""),
                "datasource_name": str(ds_meta.get("name", "") or ""),
            }
    return {}


def _source_query_language(source_query: dict[str, str]) -> str:
    expr = str(source_query.get("expr", "") or "")
    datasource_type = str(source_query.get("datasource_type", "") or "").lower()
    if not expr:
        return "unknown"
    if "loki" in datasource_type:
        return "logql"
    if "prom" in datasource_type or "mimir" in datasource_type:
        return "promql"
    if "|=" in expr or "|~" in expr:
        return "logql"
    return "promql"


def _has_explicit_threshold(ir: AlertingIR) -> bool:
    ext = ir.source_extension or {}

    if ir.kind == "grafana_legacy":
        raw_conditions = ext.get("conditions")
        conditions: list[Any] = raw_conditions if isinstance(raw_conditions, list) else []
        if len(conditions) != 1:
            return False
        condition = conditions[0]
        return isinstance(condition, dict) and bool(_legacy_condition_where_clause(condition))

    if ir.kind == "grafana_unified":
        data_list = ext.get("data")
        if not isinstance(data_list, list):
            return False
        for item in data_list:
            if not isinstance(item, dict):
                continue
            raw_model = item.get("model")
            model = raw_model if isinstance(raw_model, dict) else {}
            if model.get("type") == "threshold":
                return True
        return False

    if ir.kind.startswith("datadog_"):
        query = str(ext.get("query", "") or "")
        if re.search(r"(>=|<=|==|!=|>|<)\s*-?\d+(?:\.\d+)?\s*$", query):
            return True
        raw_opts = ext.get("options")
        opts = raw_opts if isinstance(raw_opts, dict) else {}
        raw_thresholds = opts.get("thresholds")
        thresholds = raw_thresholds if isinstance(raw_thresholds, dict) else {}
        return bool(thresholds)

    return False


def _promql_expr_has_comparison(expr: str) -> bool:
    stripped = re.sub(r'"(?:\\.|[^"])*"', '""', str(expr or ""))
    stripped = re.sub(r"\{[^{}]*\}", "{}", stripped)
    return bool(re.search(r"(==|!=|>=|<=|(?<![=!~<>])>(?![=])|(?<![=!~<>])<(?![=]))", stripped))


def _default_promql_index(data_view: str) -> str:
    index = str(data_view or "").strip()
    if not index or index == "metrics-*":
        return "metrics-prometheus-*"
    return index


def _grafana_unified_simple_threshold_where_clause(ir: AlertingIR) -> str:
    ext = ir.source_extension or {}
    data_list = ext.get("data")
    if not isinstance(data_list, list):
        return ""
    comp_map = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
    for item in data_list:
        if not isinstance(item, dict):
            continue
        raw_model = item.get("model")
        model = raw_model if isinstance(raw_model, dict) else {}
        if model.get("type") != "threshold":
            continue
        conditions = model.get("conditions")
        if not isinstance(conditions, list) or len(conditions) != 1:
            return ""
        condition = conditions[0]
        if not isinstance(condition, dict):
            return ""
        raw_evaluator = condition.get("evaluator")
        evaluator = raw_evaluator if isinstance(raw_evaluator, dict) else {}
        comparator = comp_map.get(str(evaluator.get("type", "") or "").strip().lower())
        if not comparator:
            return ""
        params = evaluator.get("params")
        threshold = _coerce_float(params[0] if isinstance(params, list) and params else None)
        if threshold is None:
            return ""
        return f"value {comparator} {threshold}"
    return ""


def _grafana_unified_primary_source_model(ir: AlertingIR) -> dict[str, Any]:
    ext = ir.source_extension or {}
    data_list = ext.get("data")
    if not isinstance(data_list, list):
        return {}
    for item in data_list:
        if not isinstance(item, dict):
            continue
        datasource_uid = str(item.get("datasourceUid", "") or "").strip()
        if not datasource_uid or datasource_uid in {"__expr__", "-100"}:
            continue
        raw_model = item.get("model")
        return raw_model if isinstance(raw_model, dict) else {}
    return {}


def _grafana_unified_source_is_instant_like(ir: AlertingIR) -> bool:
    model = _grafana_unified_primary_source_model(ir)
    if not model:
        return False
    return bool(model.get("instant")) or ("range" in model and model.get("range") is False)


def _promql_rank_limit(expr: str, agg_name: str) -> int | None:
    match = re.match(
        rf"^\s*{re.escape(agg_name)}(?:\s+(?:by|without)\s*\([^)]*\))?\s*\(\s*(\d+)\s*,",
        str(expr or ""),
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        limit = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else None


def _grafana_unified_exact_topk_bottomk_spec(ir: AlertingIR) -> dict[str, Any] | None:
    if ir.kind != "grafana_unified":
        return None

    ext = ir.source_extension or {}
    data = ext.get("data")
    if not isinstance(data, list) or _grafana_unified_has_complex_expression_graph(data):
        return None

    source_query = _primary_source_query(ir)
    expr = str(source_query.get("expr", "") or "")
    if not expr or _source_query_language(source_query) != "promql":
        return None

    ds_identity = " ".join(
        [
            str(source_query.get("datasource_type", "") or ""),
            str(source_query.get("datasource_uid", "") or ""),
            str(source_query.get("datasource_name", "") or ""),
        ]
    ).lower()
    if "prom" not in ds_identity and "mimir" not in ds_identity:
        return None

    if not _grafana_unified_source_is_instant_like(ir):
        return None

    threshold_where = _grafana_unified_simple_threshold_where_clause(ir)
    if not threshold_where:
        return None

    try:
        from observability_migration.adapters.source.grafana.panels import (
            _native_promql_result_shape,
            can_use_native_promql,
        )
        from observability_migration.adapters.source.grafana.promql import PromQLFragment, _parse_fragment
    except ImportError:
        return None

    fragment = _parse_fragment(expr)
    agg_name = (
        "topk"
        if str(getattr(fragment, "family", "") or "") == "topk"
        else str(getattr(fragment, "outer_agg", "") or "").strip().lower()
    )
    if agg_name not in {"topk", "bottomk"}:
        return None
    if agg_name != "topk" and getattr(fragment, "group_labels", None):
        return None

    inner_fragment = fragment.extra.get("inner_frag")
    inner_expr = str(fragment.extra.get("topk_value_expr", "") or "").strip()
    if not inner_expr and isinstance(inner_fragment, PromQLFragment):
        inner_expr = str(getattr(inner_fragment, "raw_expr", "") or "").strip()
    if not inner_expr or not can_use_native_promql(inner_expr):
        return None

    limit = fragment.extra.get("topk_limit") if agg_name == "topk" else _promql_rank_limit(expr, agg_name)
    if limit is None:
        return None

    _, group_cols = _native_promql_result_shape(inner_expr)
    if not group_cols:
        return None

    return {
        "agg_name": agg_name,
        "inner_expr": inner_expr,
        "group_cols": list(group_cols),
        "limit": limit,
        "threshold_where": threshold_where,
    }


def _has_source_faithful_query(ir: AlertingIR) -> bool:
    if bool((ir.metadata or {}).get("parse_degraded")):
        return False

    translated = str(ir.translated_query or "").strip()
    translated_provenance = str(
        ir.translated_query_provenance or ir.metadata.get("translated_query_provenance", "")
    ).strip().lower()
    if translated and translated_provenance in {"translated_esql", "native_promql", "manual_verified"}:
        return True

    if ir.kind == "grafana_unified" and _grafana_unified_exact_topk_bottomk_spec(ir):
        return True

    if ir.kind not in {"grafana_unified", "grafana_legacy"}:
        return False

    source_query = _primary_source_query(ir)
    expr = str(source_query.get("expr", "") or "")
    if not expr or _source_query_language(source_query) != "promql":
        return False

    ds_identity = " ".join(
        [
            str(source_query.get("datasource_type", "") or ""),
            str(source_query.get("datasource_uid", "") or ""),
            str(source_query.get("datasource_name", "") or ""),
        ]
    ).lower()
    if "prom" not in ds_identity and "mimir" not in ds_identity:
        return False

    try:
        from observability_migration.adapters.source.grafana.panels import can_use_native_promql
    except ImportError:
        return False

    return bool(can_use_native_promql(expr))


def record_semantic_losses(ir: AlertingIR) -> list[str]:
    """Identify and record semantic losses for an alert IR."""
    losses: list[str] = []

    if ir.no_data_policy and (
        (ir.kind == "grafana_unified" and not _grafana_unified_no_data_is_exact(ir))
        or (ir.kind != "grafana_unified" and ir.no_data_policy not in ("", "no_notify"))
    ):
        losses.append(f"no-data policy '{ir.no_data_policy}' may not have exact Kibana equivalent")

    ext = ir.source_extension or {}

    if bool((ir.metadata or {}).get("parse_degraded")):
        losses.append("Parser diagnostics indicate degraded parse; source-faithful translation is not trusted")

    if ir.kind.startswith("datadog_"):
        opts = ext.get("options", {}) if isinstance(ext.get("options"), dict) else {}
        if opts.get("renotify_interval"):
            losses.append("Datadog renotify_interval has no direct Kibana equivalent")
        if opts.get("threshold_windows"):
            losses.append("Datadog recovery/trigger threshold windows not directly portable")
        if opts.get("notify_by"):
            losses.append("Datadog notify_by grouping may differ from Kibana group-by behavior")
        if opts.get("evaluation_delay"):
            losses.append("Datadog evaluation_delay not natively supported in Kibana rules")
        if opts.get("require_full_window") is True:
            losses.append("Datadog require_full_window semantics differ from Kibana evaluation")
        msg = str(ext.get("message", "") or "")
        if any(handle in msg for handle in ("@slack-", "@pagerduty-", "@webhook-", "@opsgenie-")):
            losses.append("Datadog notification handles in message require manual connector setup")

    if ir.kind == "grafana_legacy":
        if ext.get("exec_error_state") and ext.get("exec_error_state") != "alerting":
            losses.append(f"Grafana exec_error_state '{ext.get('exec_error_state')}' may differ in Kibana")
        channels = []
        for action in ir.actions or []:
            channels.extend(action.get("notification_channels", []))
        if channels:
            losses.append("Grafana notification channel UIDs require manual connector resolution")

    if ir.kind == "grafana_unified":
        data = ext.get("data", [])
        if isinstance(data, list) and _grafana_unified_has_complex_expression_graph(data):
            losses.append("Multi-query unified alerting rule may lose expression graph semantics")
        labels = ext.get("labels", {})
        if labels and _grafana_safe_label_tags(labels) is None:
            losses.append("Grafana alert labels not directly portable to Kibana rule tags")
        annotations = ext.get("annotations", {})
        if annotations.get("__dashboardUid__") or annotations.get("__panelId__"):
            losses.append("Dashboard-linked alert annotation requires manual Kibana linkage")

    ir.losses = losses
    return losses


def _manual_only_family_reason(ir: AlertingIR) -> str:
    if ir.kind == "datadog_service_check":
        return (
            "Datadog service check monitors use status-count semantics and require manual migration"
        )
    if ir.kind == "datadog_composite":
        return "Datadog composite monitors depend on cross-monitor state and require manual migration"
    if ir.kind in MANUAL_ONLY_KINDS:
        family = str(ir.kind or "").replace("datadog_", "").replace("_", " ").strip()
        return f"Datadog {family} monitors are intentionally manual-only in the current migration policy"
    return ""


def _manual_boundary_reason(ir: AlertingIR) -> str:
    reason = _manual_only_family_reason(ir)
    if reason:
        return reason
    for warning in ir.warnings or []:
        text = str(warning or "").strip()
        if text:
            return text
    return ""


def _grafana_unified_has_complex_expression_graph(data: list[Any]) -> bool:
    datasource_query_count = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        datasource_uid = str(item.get("datasourceUid", "") or "").strip()
        raw_model = item.get("model")
        model = raw_model if isinstance(raw_model, dict) else {}
        model_type = str(model.get("type", "") or "").strip().lower()
        if datasource_uid not in {"__expr__", "-100"}:
            datasource_query_count += 1
            continue
        if model_type and model_type not in {"reduce", "threshold"}:
            return True
    return datasource_query_count > 1


def select_target_rule_type(ir: AlertingIR, preflight: dict[str, Any] | None = None) -> str:
    """Select the best Kibana rule type for an alert IR.

    Only returns a rule type when a source-faithful target query is available.
    The current correctness-first path emits `.es-query` rules only.

    Returns the rule_type_id string or empty string if no suitable target.
    """
    availability: dict[str, Any] = {}
    if preflight:
        availability = preflight.get("rule_family_availability", {})

    if ir.kind in MANUAL_ONLY_KINDS or not _has_source_faithful_query(ir):
        return ""

    if availability.get("es-query", True):
        return ES_QUERY_RULE_TYPE
    return ""


def _extract_source_expression(ir: AlertingIR) -> str:
    """Extract the primary source query expression from AlertingIR source_extension."""
    return str(_primary_source_query(ir).get("expr", "") or "")


def _extract_threshold_from_source(ir: AlertingIR) -> tuple[str, float]:
    """Extract (comparator, value) from the Grafana unified alert threshold step."""
    ext = ir.source_extension or {}

    if ir.kind.startswith("datadog_"):
        query = str(ext.get("query", "") or "")
        match = re.search(r"(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$", query)
        if match:
            return match.group(1), float(match.group(2))
        raw_opts = ext.get("options")
        opts = raw_opts if isinstance(raw_opts, dict) else {}
        raw_thresholds = opts.get("thresholds")
        thresholds = raw_thresholds if isinstance(raw_thresholds, dict) else {}
        for key in ("critical", "warning"):
            if key in thresholds:
                try:
                    return ">", float(thresholds[key])
                except (TypeError, ValueError):
                    continue
        return ">", 0.0

    if ir.kind == "grafana_legacy":
        raw_conditions = ext.get("conditions")
        conditions: list[Any] = raw_conditions if isinstance(raw_conditions, list) else []
        if len(conditions) == 1 and isinstance(conditions[0], dict):
            cond = conditions[0]
            eval_type = str(cond.get("evaluator_type", "")).lower()
            params = cond.get("evaluator_params", []) if isinstance(cond.get("evaluator_params"), list) else []
            val = params[0] if params else 0
            comp_map = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
            if eval_type in comp_map:
                return comp_map[eval_type], float(val)
        return ">", 0.0

    data_list = ext.get("data", [])
    for d in data_list:
        if not isinstance(d, dict):
            continue
        raw_model = d.get("model")
        model = raw_model if isinstance(raw_model, dict) else {}
        if model.get("type") == "threshold":
            for cond in model.get("conditions", []):
                ev = cond.get("evaluator", {})
                ev_type = ev.get("type", "gt")
                params = ev.get("params", [0])
                val = params[0] if params else 0
                comp_map = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
                return comp_map.get(ev_type, ">"), float(val)
    return ">", 0.0


def _threshold_where_clause_from_source(ir: AlertingIR) -> str:
    ext = ir.source_extension or {}
    if ir.kind == "grafana_legacy":
        raw_conditions = ext.get("conditions")
        conditions: list[Any] = raw_conditions if isinstance(raw_conditions, list) else []
        if len(conditions) != 1 or not isinstance(conditions[0], dict):
            return ""
        return _legacy_condition_where_clause(conditions[0])

    if ir.kind == "grafana_unified":
        where_clause = _grafana_unified_simple_threshold_where_clause(ir)
        if where_clause:
            return where_clause
        return ""

    comparator, threshold_val = _extract_threshold_from_source(ir)
    return f"value {comparator} {threshold_val}"


def _generate_esql_for_alert(ir: AlertingIR, data_view: str) -> str:
    """Generate a source-faithful query for an alert rule when possible."""
    if ir.kind not in {"grafana_unified", "grafana_legacy"} or not _has_source_faithful_query(ir):
        return ""

    exact_rank_spec = _grafana_unified_exact_topk_bottomk_spec(ir)
    if exact_rank_spec:
        try:
            from observability_migration.adapters.source.grafana.panels import build_native_promql_query
        except ImportError:
            return ""
        base_query = build_native_promql_query(
            exact_rank_spec["inner_expr"],
            index=_default_promql_index(data_view),
            kibana_type="metric",
        )
        query = "\n".join(
            [
                base_query,
                "| SORT step ASC",
                (
                    f"| STATS value = LAST(value, step) BY "
                    f"{', '.join(exact_rank_spec['group_cols'])}"
                ),
                f"| SORT value {'DESC' if exact_rank_spec['agg_name'] == 'topk' else 'ASC'}",
                f"| LIMIT {exact_rank_spec['limit']}",
                f"| WHERE {exact_rank_spec['threshold_where']}",
            ]
        )
        ir.translated_query = query
        ir.translated_query_provenance = "translated_esql"
        ir.group_by = list(exact_rank_spec["group_cols"])
        return query

    source_query = _primary_source_query(ir)
    expr = str(source_query.get("expr", "") or "")
    if not expr:
        return ""

    try:
        from observability_migration.adapters.source.grafana.panels import build_native_promql_query
    except ImportError:
        return ""

    query = build_native_promql_query(
        expr,
        index=_default_promql_index(data_view),
        kibana_type="metric",
    )

    if _promql_expr_has_comparison(expr):
        return query
    if not _has_explicit_threshold(ir):
        return ""

    where_clause = _threshold_where_clause_from_source(ir)
    if not where_clause:
        return ""
    return f"{query} | WHERE {where_clause}"


def build_es_query_rule_params(ir: AlertingIR, data_view: str = "metrics-*") -> dict[str, Any]:
    """Build Kibana ES query rule params from an AlertingIR."""
    query = str(ir.translated_query or "").strip()
    if not query:
        query = _generate_esql_for_alert(ir, data_view)
    if not query:
        return {}

    params: dict[str, Any] = {
        "searchType": "esqlQuery",
        "esqlQuery": {"esql": query},
        "timeField": "@timestamp",
        "timeWindowSize": 5,
        "timeWindowUnit": "m",
        "threshold": [0],
        "thresholdComparator": ">",
        "size": 100,
    }

    window = ir.evaluation_window
    if window:
        try:
            if window.endswith("m"):
                params["timeWindowSize"] = int(window[:-1])
                params["timeWindowUnit"] = "m"
            elif window.endswith("h"):
                params["timeWindowSize"] = int(window[:-1])
                params["timeWindowUnit"] = "h"
            elif window.endswith("s"):
                params["timeWindowSize"] = int(window[:-1])
                params["timeWindowUnit"] = "s"
        except (ValueError, TypeError):
            pass

    return params


def build_index_threshold_rule_params(ir: AlertingIR, index: str = "metrics-*") -> dict[str, Any]:
    """Build Kibana index threshold rule params from an AlertingIR."""
    params: dict[str, Any] = {
        "index": [index],
        "timeField": "@timestamp",
        "aggType": "count",
        "groupBy": "all",
        "termSize": 5,
        "timeWindowSize": 5,
        "timeWindowUnit": "m",
        "threshold": [0],
        "thresholdComparator": ">",
    }

    if ir.group_by:
        params["groupBy"] = "top"
        params["termField"] = ir.group_by[0] if ir.group_by else ""

    return params


def build_custom_threshold_rule_params(ir: AlertingIR, data_view_id: str = "metrics-*") -> dict[str, Any]:
    """Build Kibana custom threshold rule params from an AlertingIR."""
    params: dict[str, Any] = {
        "criteria": [
            {
                "comparator": ">",
                "threshold": [0],
                "metrics": [
                    {"name": "A", "aggType": "count"},
                ],
                "timeSize": 5,
                "timeUnit": "m",
            }
        ],
        "searchConfiguration": {
            "index": data_view_id,
            "query": {"query": "", "language": "kuery"},
        },
    }

    if ir.group_by:
        params["groupBy"] = ir.group_by

    return params


def map_alert_to_kibana_payload(
    ir: AlertingIR,
    *,
    preflight: dict[str, Any] | None = None,
    data_view: str = "metrics-*",
) -> dict[str, Any]:
    """Map an AlertingIR to a complete Kibana rule creation payload.

    Returns a dict with:
    - "rule_payload": the Kibana API request body (or empty dict if not mappable)
    - "automation_tier": final tier after analysis
    - "target_rule_type": emitted rule type ID
    - "selected_target_rule_type": candidate rule type ID before emission checks
    - "payload_emitted": whether a Kibana rule payload was produced
    - "losses": semantic losses
    - "valid": whether the payload is valid for creation
    - "validation_errors": list of validation issues
    """
    losses = record_semantic_losses(ir)
    tier = classify_automation_tier(ir)
    rule_type = select_target_rule_type(ir, preflight)
    review_gates = _grafana_unified_review_gates(ir) if ir.kind == "grafana_unified" else {}
    normalized_rule_type = rule_type.replace(".", "").replace("_", "-") if rule_type else ""

    ir.automation_tier = tier
    ir.selected_target_rule_type = normalized_rule_type
    ir.target_rule_type = ""
    ir.payload_emitted = False
    ir.payload_status = ""
    ir.payload_status_reason = ""
    ir.target_rule_payload = {}
    ir.losses = losses

    if tier == "manual_required" or not rule_type:
        ir.status = AssetStatus.MANUAL_REQUIRED
        ir.manual_required = True
        if rule_type:
            payload_status = "blocked_manual_review"
            payload_status_reason = (
                "Translated query is available, but payload emission is intentionally blocked because "
                "the alert remains manual_required"
            )
            validation_errors: list[str] = []
        elif _has_source_faithful_query(ir):
            payload_status = "blocked_no_target_rule_type"
            payload_status_reason = "No suitable target rule type is available for the source-faithful query"
            validation_errors = [payload_status_reason]
        else:
            payload_status = "blocked_no_source_faithful_query"
            payload_status_reason = _manual_boundary_reason(ir) or "No source-faithful target query could be produced"
            validation_errors = [payload_status_reason]
        ir.payload_status = payload_status
        ir.payload_status_reason = payload_status_reason
        return {
            "rule_payload": {},
            "automation_tier": tier,
            "target_rule_type": "",
            "selected_target_rule_type": rule_type,
            "payload_emitted": False,
            "payload_status": payload_status,
            "payload_status_reason": payload_status_reason,
            "losses": losses,
            "review_gates": review_gates,
            "valid": False,
            "validation_errors": validation_errors,
        }

    if rule_type == ES_QUERY_RULE_TYPE:
        params = build_es_query_rule_params(ir, data_view=data_view)
    elif rule_type == INDEX_THRESHOLD_RULE_TYPE:
        params = build_index_threshold_rule_params(ir, index=data_view)
    elif rule_type == CUSTOM_THRESHOLD_RULE_TYPE:
        params = build_custom_threshold_rule_params(ir, data_view_id=data_view)
    else:
        params = {}

    if not params:
        ir.status = AssetStatus.MANUAL_REQUIRED
        ir.manual_required = True
        payload_status_reason = "No source-faithful target query could be produced"
        ir.payload_status = "blocked_no_source_faithful_query"
        ir.payload_status_reason = payload_status_reason
        return {
            "rule_payload": {},
            "automation_tier": "manual_required",
            "target_rule_type": "",
            "selected_target_rule_type": rule_type,
            "payload_emitted": False,
            "payload_status": "blocked_no_source_faithful_query",
            "payload_status_reason": payload_status_reason,
            "losses": losses,
            "review_gates": review_gates,
            "valid": False,
            "validation_errors": [payload_status_reason],
        }

    schedule = ir.schedule_interval or "1m"

    CONSUMER_MAP = {
        ES_QUERY_RULE_TYPE: "stackAlerts",
        INDEX_THRESHOLD_RULE_TYPE: "stackAlerts",
        CUSTOM_THRESHOLD_RULE_TYPE: "observability",
    }
    consumer = CONSUMER_MAP.get(rule_type, "stackAlerts")
    extra_tags: list[str] = []
    if ir.kind == "grafana_unified":
        label_tags = _grafana_safe_label_tags((ir.source_extension or {}).get("labels")) or []
        dashboard_tags = _grafana_safe_dashboard_link_tags((ir.source_extension or {}).get("annotations"))
        extra_tags = [*label_tags]
        if dashboard_tags:
            extra_tags.extend(dashboard_tags)

    payload = {
        "rule_type_id": rule_type,
        "name": f"[migrated] {ir.name}" if ir.name else "[migrated] unnamed",
        "consumer": consumer,
        "schedule": {"interval": schedule},
        "params": params,
        "actions": [],
        "enabled": False,
        "tags": ["obs-migration", f"source:{ir.kind}", *extra_tags],
    }

    ir.target_rule_payload = payload
    ir.target_rule_type = normalized_rule_type
    ir.payload_emitted = True
    ir.payload_status = "emitted"
    ir.payload_status_reason = ""

    if tier == "automated":
        ir.status = AssetStatus.TRANSLATED
        ir.manual_required = False
    else:
        ir.status = AssetStatus.DRAFT_REVIEW
        ir.manual_required = False

    validation_errors = []
    if not params:
        validation_errors.append("Empty params generated")

    return {
        "rule_payload": payload,
        "automation_tier": tier,
        "target_rule_type": rule_type,
        "selected_target_rule_type": rule_type,
        "payload_emitted": True,
        "payload_status": "emitted",
        "payload_status_reason": "",
        "losses": losses,
        "review_gates": review_gates,
        "valid": len(validation_errors) == 0,
        "validation_errors": validation_errors,
    }


def map_alerts_batch(
    alerts: list[AlertingIR],
    *,
    preflight: dict[str, Any] | None = None,
    data_view: str = "metrics-*",
) -> dict[str, Any]:
    """Map a batch of AlertingIR instances and return a summary.

    Returns:
    - "results": list of per-alert mapping dicts
    - "summary": aggregate counts by tier and rule type
    """
    results = []
    by_tier: dict[str, int] = {}
    by_rule_type: dict[str, int] = {}
    by_selected_rule_type: dict[str, int] = {}
    total_losses: list[str] = []

    for ir in alerts:
        mapping = map_alert_to_kibana_payload(ir, preflight=preflight, data_view=data_view)
        results.append({
            "alert_id": ir.alert_id,
            "name": ir.name,
            "kind": ir.kind,
            "mapping": mapping,
        })
        tier = mapping["automation_tier"]
        by_tier[tier] = by_tier.get(tier, 0) + 1
        rt = mapping["target_rule_type"]
        if rt:
            by_rule_type[rt] = by_rule_type.get(rt, 0) + 1
        selected_rt = mapping.get("selected_target_rule_type", "")
        if selected_rt:
            by_selected_rule_type[selected_rt] = by_selected_rule_type.get(selected_rt, 0) + 1
        total_losses.extend(mapping["losses"])

    unique_losses: dict[str, int] = {}
    for loss in total_losses:
        unique_losses[loss] = unique_losses.get(loss, 0) + 1

    return {
        "results": results,
        "summary": {
            "total": len(alerts),
            "by_automation_tier": by_tier,
            "by_target_rule_type": by_rule_type,
            "by_selected_target_rule_type": by_selected_rule_type,
            "unique_semantic_losses": dict(sorted(unique_losses.items(), key=lambda x: -x[1])),
        },
    }


__all__ = [
    "AUTOMATED_KINDS",
    "CUSTOM_THRESHOLD_RULE_TYPE",
    "DRAFT_REVIEW_KINDS",
    "ES_QUERY_RULE_TYPE",
    "INDEX_THRESHOLD_RULE_TYPE",
    "MANUAL_ONLY_KINDS",
    "build_custom_threshold_rule_params",
    "build_es_query_rule_params",
    "build_index_threshold_rule_params",
    "classify_automation_tier",
    "map_alert_to_kibana_payload",
    "map_alerts_batch",
    "record_semantic_losses",
    "select_target_rule_type",
]
