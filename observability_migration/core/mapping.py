# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Rule-mapping engine for alert/monitor migration.

Takes AlertingIR instances and produces Kibana rule payloads for safely
mappable cases, or downgrades the automation tier and records semantic
losses for cases that cannot be automatically translated.
"""

from __future__ import annotations

import math
import re
from typing import Any

from observability_migration.core.assets.alerting import AlertingIR
from observability_migration.core.assets.status import AssetStatus

# ---- Kibana rule type IDs ----
ES_QUERY_RULE_TYPE = ".es-query"
INDEX_THRESHOLD_RULE_TYPE = ".index-threshold"
CUSTOM_THRESHOLD_RULE_TYPE = "observability.rules.custom_threshold"

# Kibana enforces a minimum rule schedule of 60s; alert-delay math divides the
# pending period by the effective check interval, so an unknown/empty schedule
# falls back to this same floor.
_DEFAULT_SCHEDULE_SECONDS = 60
# Prometheus-style duration units, which Grafana provisioning reuses. ``ms`` and
# ``y`` are included so long pending periods written that way (e.g. ``120000ms``,
# ``1y``) are parsed rather than silently dropped.
_DURATION_UNIT_SECONDS: dict[str, float] = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}
# ``ms`` is listed before the single-letter units so it is matched greedily.
_DURATION_TOKEN_RE = re.compile(r"(\d+)\s*(ms|[smhdwy])", re.IGNORECASE)


def _duration_to_seconds(value: str) -> float | None:
    """Parse a Grafana/Prometheus duration to seconds.

    Accepts compact duration strings (``"5m"``, ``"1h30m"``, ``"120000ms"``,
    ``"1y"``) and bare integers (treated as seconds). Returns ``None`` when the
    input is empty or cannot be parsed, so callers can distinguish an explicit
    zero (``"0"`` / ``"0s"`` -> ``0``) from invalid/unsupported input.
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    compact = re.sub(r"\s+", "", text)
    tokens = _DURATION_TOKEN_RE.findall(compact)
    if not tokens or "".join(f"{n}{u}" for n, u in tokens) != compact:
        return None
    return sum(int(n) * _DURATION_UNIT_SECONDS[u] for n, u in tokens)


def compute_alert_delay(pending_period: str, schedule_interval: str) -> tuple[int | None, str | None] | None:
    """Convert a Grafana pending period into a Kibana ``alert_delay`` count.

    Grafana's pending period (the rule ``for`` field) holds the alert in a
    ``Pending`` state until the condition has breached continuously for that
    duration. Kibana models the same intent with "Alert delay" — fire only after
    ``N`` consecutive matching checks — where a check happens once per rule
    schedule.

    Grafana/Prometheus first marks the alert pending on the evaluation where the
    condition is *first* met, then only fires on a later evaluation once the
    pending period has elapsed. Kibana's ``alert_delay.active`` counts the
    consecutive matching runs *including* that first one, so the conversion is
    ``N = ceil(pending / schedule) + 1`` for a non-zero pending period.

    Returns one of:

    - ``None`` — the source supplied no pending period, so the Kibana default is
      left untouched (no ``alert_delay``, no note).
    - ``(active, note)`` — ``active`` is the consecutive-match count to emit, or
      ``None`` to omit ``alert_delay`` (non-empty but unparseable pending period);
      ``note`` is a semantic-loss note to record, or ``None``.
    """
    text = str(pending_period or "").strip()
    if not text:
        return None
    pending_seconds = _duration_to_seconds(text)
    if pending_seconds is None:
        # Non-empty but unparseable (e.g. an unsupported unit): don't guess a
        # delay. Leave the Kibana default in place and surface the dropped
        # pending period rather than silently firing on the first match.
        note = (
            f"Grafana pending period '{pending_period}' could not be parsed; "
            "alert delay was not set (migrated rule may fire earlier than the source)"
        )
        return (None, note)
    if pending_seconds <= 0:
        # Grafana skips the Pending state for a 0 pending period: fire immediately.
        return (1, None)
    interval_seconds = _duration_to_seconds(schedule_interval) or _DEFAULT_SCHEDULE_SECONDS
    active = math.ceil(pending_seconds / interval_seconds) + 1
    note = None
    if pending_seconds < interval_seconds:
        note = (
            f"Grafana pending period '{pending_period}' is shorter than the "
            f"{schedule_interval or '1m'} check interval; alert delay rounded up to "
            f"{active} consecutive matches"
        )
    return (active, note)

# ---- Fidelity classification ----

# Canonical automation-tier values. These strings are the shared vocabulary for
# every breakdown (console/run summary, detailed results, comparison), so keep a
# single source of truth to prevent drift between the places that emit them.
AUTOMATED_TIER = "automated"
DRAFT_REVIEW_TIER = "draft_requires_review"
MANUAL_REQUIRED_TIER = "manual_required"

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
        return MANUAL_REQUIRED_TIER

    if ir.kind == "grafana_legacy":
        if _has_source_faithful_query(ir) and _has_simple_threshold_condition(ir):
            return AUTOMATED_TIER
        return MANUAL_REQUIRED_TIER

    if ir.kind == "grafana_unified":
        if _grafana_unified_is_strict_exact_query_subset(ir):
            return AUTOMATED_TIER
        if _has_source_faithful_query(ir):
            return DRAFT_REVIEW_TIER
        return MANUAL_REQUIRED_TIER

    if ir.kind == "datadog_metric":
        if not _has_source_faithful_query(ir):
            return MANUAL_REQUIRED_TIER
        if ir.warnings:
            return MANUAL_REQUIRED_TIER
        if _has_simple_threshold_condition(ir):
            return AUTOMATED_TIER
        return DRAFT_REVIEW_TIER

    if ir.kind == "datadog_log":
        if ir.warnings:
            return MANUAL_REQUIRED_TIER
        if _has_source_faithful_query(ir):
            return DRAFT_REVIEW_TIER
        return MANUAL_REQUIRED_TIER

    if ir.kind in DRAFT_REVIEW_KINDS:
        return DRAFT_REVIEW_TIER

    return MANUAL_REQUIRED_TIER


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
    # Grafana classic alerting range operators use exclusive bounds
    # (``Lower < v && v < Upper``), matching the unified threshold path and
    # ``within_range``/``outside_range`` in ``pkg/expr/threshold.go`` (issue #204).
    if eval_type == "within_range":
        return f"value > {lower} AND value < {upper}"
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
    """Resolve the index for native PROMQL alert queries.

    Mirrors the dashboard path (``panels.py``), which bakes the configured
    data view straight into the ``PROMQL index=`` command and only falls back
    to ``metrics-prometheus-*`` when no data view is configured at all.
    Previously this treated the default ``metrics-*`` as "unset" and forced
    ``metrics-prometheus-*``, so migrated alerts queried an index that often
    did not hold the telemetry and silently never fired (issue #181).
    """
    index = str(data_view or "").strip()
    return index or "metrics-prometheus-*"


# Single-value comparison operators: evaluator type -> ES|QL comparator.
_THRESHOLD_COMPARATORS = {
    "gt": ">",
    "lt": "<",
    "gte": ">=",
    "lte": "<=",
    "eq": "==",
    "ne": "!=",
}

# Two-bound range operators: evaluator type -> ES|QL clause template. Semantics
# mirror Grafana ``pkg/expr/threshold.go`` exactly, preserving the distinction
# between the exclusive ``within/outside_range`` operators and their inclusive
# ``*_range_included`` variants (issue #204).
_THRESHOLD_RANGE_CLAUSES = {
    "within_range": "value > {lo} AND value < {hi}",
    "outside_range": "value < {lo} OR value > {hi}",
    "within_range_included": "value >= {lo} AND value <= {hi}",
    "outside_range_included": "value <= {lo} OR value >= {hi}",
}


def _threshold_evaluator_where_clause(eval_type: str, params: Any) -> str:
    """Render a Grafana threshold evaluator as an ES|QL ``value`` clause.

    Supports all 10 standard Grafana threshold operators. Returns ``""`` when
    the evaluator type is unknown or its parameters are malformed.
    """
    eval_type = str(eval_type or "").strip().lower()
    params = params if isinstance(params, list) else []

    comparator = _THRESHOLD_COMPARATORS.get(eval_type)
    if comparator:
        threshold = _coerce_float(params[0] if params else None)
        if threshold is None:
            return ""
        return f"value {comparator} {threshold}"

    template = _THRESHOLD_RANGE_CLAUSES.get(eval_type)
    if template is None or len(params) < 2:
        return ""
    lower = _coerce_float(params[0])
    upper = _coerce_float(params[1])
    if lower is None or upper is None:
        return ""
    return template.format(lo=lower, hi=upper)


def _grafana_unified_simple_threshold_where_clause(ir: AlertingIR) -> str:
    ext = ir.source_extension or {}
    data_list = ext.get("data")
    if not isinstance(data_list, list):
        return ""
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
        return _threshold_evaluator_where_clause(
            evaluator.get("type", ""), evaluator.get("params")
        )
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


def _seconds_to_step_duration(seconds: float) -> str:
    """Render a step in seconds as the most compact Prometheus-style duration."""
    total = max(1, int(seconds))
    if total % 3600 == 0:
        return f"{total // 3600}h"
    if total % 60 == 0:
        return f"{total // 60}m"
    return f"{total}s"


def _grafana_unified_primary_range_seconds(ir: AlertingIR) -> float | None:
    """Lookback window (seconds) of the primary Grafana query, if exported."""
    ext = ir.source_extension or {}
    data_list = ext.get("data")
    if not isinstance(data_list, list):
        return None
    for item in data_list:
        if not isinstance(item, dict):
            continue
        datasource_uid = str(item.get("datasourceUid", "") or "").strip()
        if not datasource_uid or datasource_uid in {"__expr__", "-100"}:
            continue
        raw_range = item.get("relativeTimeRange")
        rng = raw_range if isinstance(raw_range, dict) else {}
        from_seconds = _coerce_float(rng.get("from"))
        return from_seconds if from_seconds and from_seconds > 0 else None
    return None


def _grafana_unified_promql_step(ir: AlertingIR) -> tuple[str, str] | None:
    """Derive the migrated PROMQL range step from Grafana alert metadata.

    Grafana range alert queries carry the operator's chosen resolution as
    ``intervalMs`` (the step) and, for auto interval, ``maxDataPoints`` over the
    query window (``relativeTimeRange.from``). A migrated range alert must walk
    ``step=`` buckets at that same resolution, otherwise a rule evaluated every
    second in Grafana silently becomes a 1-minute rule in Kibana (issue #209).

    Returns ``(step, provenance)`` where ``provenance`` is ``"source"`` when the
    step comes straight from ``intervalMs`` and ``"inferred"`` when it is
    computed from the window and ``maxDataPoints`` (Grafana's auto resolution).
    Returns ``None`` when no resolution metadata is present so the caller keeps
    the documented default step.
    """
    if ir.kind != "grafana_unified":
        return None
    model = _grafana_unified_primary_source_model(ir)
    if not model:
        return None

    interval_ms = _coerce_float(model.get("intervalMs"))
    if interval_ms is not None and interval_ms > 0:
        # Round up sub-second / fractional-second intervals so the migrated step
        # never under-shoots the source resolution (the native step is in whole
        # seconds at minimum); e.g. 1500ms -> 2s, 500ms -> 1s.
        return _seconds_to_step_duration(max(1, math.ceil(interval_ms / 1000.0))), "source"

    max_data_points = _coerce_float(model.get("maxDataPoints"))
    range_seconds = _grafana_unified_primary_range_seconds(ir)
    if (
        max_data_points is not None
        and max_data_points > 0
        and range_seconds is not None
        and range_seconds > 0
    ):
        step_seconds = max(1, math.ceil(range_seconds / max_data_points))
        return _seconds_to_step_duration(step_seconds), "inferred"

    return None


def _record_promql_step_provenance(ir: AlertingIR, step: str, provenance: str) -> None:
    """Note on the IR whether the emitted ``step=`` was sourced or inferred."""
    ir.metadata["promql_step"] = step
    ir.metadata["promql_step_provenance"] = provenance
    if provenance == "inferred":
        ir.metadata["promql_step_note"] = (
            f"PROMQL range step={step} was inferred from the source query window "
            "and maxDataPoints (Grafana auto interval); confirm it matches the "
            "intended evaluation resolution."
        )
    else:
        ir.metadata["promql_step_note"] = (
            f"PROMQL range step={step} taken from the source query interval (intervalMs)."
        )


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
    if str(getattr(fragment, "family", "") or "") == "topk":
        # bottomk is now translated through the same topk_frag path with
        # topk_sort_asc=True; family is "topk" for both.
        agg_name = "bottomk" if fragment.extra.get("topk_sort_asc") else "topk"
    else:
        agg_name = str(getattr(fragment, "outer_agg", "") or "").strip().lower()
    if agg_name not in {"topk", "bottomk"}:
        return None
    # For the topk_frag path (family=="topk"), group_labels reflects the inner
    # agg's by() clause — needed for correct translation, not a rejection signal.
    # Only guard the legacy outer-agg path where group_labels meant explicit
    # grouping on the bottomk itself.
    if agg_name == "bottomk" and not fragment.extra.get("topk_sort_asc") and getattr(fragment, "group_labels", None):
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
        if not ir.schedule_interval:
            losses.append(
                "Evaluation interval could not be resolved from the source group; "
                "applying default schedule (1m)"
            )
        clamped_from = ext.get("schedule_interval_clamped_from")
        if clamped_from:
            losses.append(
                f"Source evaluation interval ({clamped_from}) is below Kibana's "
                f"minimum schedule interval; raised to {ir.schedule_interval}"
            )

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


# Grafana auto-inserts this no-op "does the query return a value?" math step
# when a Prometheus/Mimir alerting rule is imported as a Grafana-managed rule:
# ``is_number($X) || is_nan($X) || is_inf($X)``. It carries no alerting logic —
# any NaN/Inf/non-number is already excluded by the downstream threshold — so it
# can be ignored without changing when the alert fires.
_VALIDITY_STEP_TERM = re.compile(r"^is_(number|nan|inf)\(\s*\$\{?\w+\}?\s*\)$", re.IGNORECASE)


def _is_grafana_validity_noop_expression(model: dict[str, Any]) -> bool:
    if str(model.get("type", "") or "").strip().lower() != "math":
        return False
    expression = str(model.get("expression", "") or "").strip()
    if not expression:
        return False
    terms = [term.strip() for term in expression.split("||")]
    if len(terms) != 3:
        return False
    functions: set[str] = set()
    variables: set[str] = set()
    for term in terms:
        match = _VALIDITY_STEP_TERM.match(term)
        if not match:
            return False
        functions.add(match.group(1).lower())
        variables.add(re.sub(r"[${}]", "", term[term.index("(") + 1 : term.rindex(")")]).strip())
    return functions == {"number", "nan", "inf"} and len(variables) == 1


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
        if _is_grafana_validity_noop_expression(model):
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


def _generate_esql_for_alert(ir: AlertingIR, data_view: str, resolver: Any = None) -> str:
    """Generate a source-faithful query for an alert rule when possible.

    *resolver* is the target ``SchemaResolver``; when supplied it lets the native
    PROMQL builder rewrite bare metric selectors to their ``metrics.<name>``
    field on OTel Collector indices, exactly as the dashboard path does (#270).
    Without it the query keeps the bare name (unchanged, graceful passthrough).
    """
    if ir.kind not in {"grafana_unified", "grafana_legacy"} or not _has_source_faithful_query(ir):
        return ""

    # Issue #230: a control-bound label-matcher variable (``{instance=~"$instance"}``)
    # would be emitted as a named param trapped *inside* the opaque PROMQL command
    # string (``{instance=~?instance}``). A dashboard panel falls through to native
    # ES|QL where a control binds the param as a visible ``... RLIKE ?var`` clause,
    # but an alert rule has no control to bind it — the param stays unbound and the
    # rule fails at evaluation with "Parameter [?instance] value not found". Degrade
    # to no source-faithful query instead. (``can_use_native_promql`` already
    # rejects these today because the alert path passes no runtime features; this
    # keeps the decision explicit and durable if that ever changes.)
    try:
        from observability_migration.adapters.source.grafana.panels import (
            _promql_label_matcher_has_template_variable,
            _promql_uses_rule_pack_label_overrides,
            _record_passthrough_native_labels,
        )
        primary_expr = str(_primary_source_query(ir).get("expr", "") or "")
        has_control_bound_matcher = _promql_label_matcher_has_template_variable(primary_expr)
        _record_passthrough_native_labels(primary_expr, resolver)
        requires_esql_for_label_rules = bool(
            getattr(resolver, "_passthrough", False)
            and _promql_uses_rule_pack_label_overrides(
                primary_expr,
                getattr(resolver, "_rule_pack", None),
            )
        )
    except ImportError:
        has_control_bound_matcher = False
        requires_esql_for_label_rules = False
    if has_control_bound_matcher:
        return ""

    if requires_esql_for_label_rules:
        from observability_migration.adapters.source.grafana.promql import (
            _esql_identifier,
        )
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )

        translated = translate_promql_to_esql(
            primary_expr,
            datasource_index=data_view,
            esql_index=data_view,
            panel_type="stat",
            rule_pack=getattr(resolver, "_rule_pack", None),
            resolver=resolver,
            query_language="promql",
        )
        query = str(getattr(translated, "esql_query", "") or "").strip()
        if getattr(translated, "feasibility", "") == "not_feasible" or not query:
            return ""
        if _promql_expr_has_comparison(primary_expr):
            return query
        if not _has_explicit_threshold(ir):
            return ""
        where_clause = _threshold_where_clause_from_source(ir)
        output_field = str(
            getattr(translated, "output_metric_field", "") or "value"
        )
        where_clause = re.sub(
            r"\bvalue\b",
            _esql_identifier(output_field),
            where_clause,
        )
        return f"{query} | WHERE {where_clause}" if where_clause else ""

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
            resolver=resolver,
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

    # A ``Type: Instant`` Grafana query evaluates only the most recent value, so
    # the migrated alert must emit the instant ``time=?_tend`` selector rather
    # than the ``step=`` range form. The range form walks every bucket in the
    # lookback window and fires if the threshold was crossed at *any* point, so a
    # value that already recovered still over-fires (issue #200). ``?_tend`` is
    # bound by Kibana's es-query rule executor to the evaluation window end, i.e.
    # "now" at runtime.
    instant = _grafana_unified_source_is_instant_like(ir)
    # Range alerts must keep the source query's resolution: derive ``step=`` from
    # the exported interval metadata and only fall back to the default when none
    # is present (issue #209). Instant alerts never emit ``step=`` (issue #200).
    step_info = None if instant else _grafana_unified_promql_step(ir)
    query = build_native_promql_query(
        expr,
        index=_default_promql_index(data_view),
        kibana_type="metric",
        instant=instant,
        step=step_info[0] if step_info else None,
        resolver=resolver,
    )
    if step_info:
        _record_promql_step_provenance(ir, step_info[0], step_info[1])

    if _promql_expr_has_comparison(expr):
        return query
    if not _has_explicit_threshold(ir):
        return ""

    where_clause = _threshold_where_clause_from_source(ir)
    if not where_clause:
        return ""
    return f"{query} | WHERE {where_clause}"


def build_es_query_rule_params(
    ir: AlertingIR, data_view: str = "metrics-*", resolver: Any = None
) -> dict[str, Any]:
    """Build Kibana ES query rule params from an AlertingIR."""
    query = str(ir.translated_query or "").strip()
    if not query:
        query = _generate_esql_for_alert(ir, data_view, resolver=resolver)
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
    resolver: Any = None,
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

    if tier == MANUAL_REQUIRED_TIER or not rule_type:
        # When ``not rule_type`` but ``tier`` is non-manual, the block is carried
        # by ``status`` only: ``automation_tier`` is intentionally left at its
        # classified value and the same value is returned below, so the two tier
        # breakdowns still agree. This is distinct from the empty-``params``
        # downgrade further down, which must rewrite ``automation_tier``.
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
        params = build_es_query_rule_params(ir, data_view=data_view, resolver=resolver)
    elif rule_type == INDEX_THRESHOLD_RULE_TYPE:
        params = build_index_threshold_rule_params(ir, index=data_view)
    elif rule_type == CUSTOM_THRESHOLD_RULE_TYPE:
        params = build_custom_threshold_rule_params(ir, data_view_id=data_view)
    else:
        params = {}

    if not params:
        ir.status = AssetStatus.MANUAL_REQUIRED
        ir.manual_required = True
        # Persist the downgrade on the rule record so every artifact rebuilt
        # from it (detailed results, comparison) agrees with the console/run
        # summary, which counts this returned tier.
        ir.automation_tier = MANUAL_REQUIRED_TIER
        payload_status_reason = "No source-faithful target query could be produced"
        ir.payload_status = "blocked_no_source_faithful_query"
        ir.payload_status_reason = payload_status_reason
        return {
            "rule_payload": {},
            "automation_tier": MANUAL_REQUIRED_TIER,
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

    # Carry over Grafana's pending period as Kibana's "Alert delay" so migrated
    # rules fire only after a sustained breach rather than on the first check.
    pending_period = ir.pending_period or str((ir.source_extension or {}).get("pending_for", "") or "")
    alert_delay = compute_alert_delay(pending_period, schedule)
    if alert_delay is not None:
        active, note = alert_delay
        if active is not None:
            payload["alert_delay"] = {"active": active}
        if note and note not in losses:
            losses.append(note)

    ir.target_rule_payload = payload
    ir.target_rule_type = normalized_rule_type
    ir.payload_emitted = True
    ir.payload_status = "emitted"
    ir.payload_status_reason = ""

    if tier == AUTOMATED_TIER:
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
    resolver: Any = None,
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
        mapping = map_alert_to_kibana_payload(
            ir, preflight=preflight, data_view=data_view, resolver=resolver
        )
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
