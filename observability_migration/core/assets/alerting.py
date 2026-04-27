# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Canonical alerting IR — shared envelope for alerts and monitors.

Wraps both Grafana legacy alerts and Datadog monitors under one
operational envelope without faking a universal condition AST.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .status import AssetStatus

_REL_LAST_RE = re.compile(
    r"(?:\blast_\s*(\d+\s*[smhdw])\b|\blast\s*\(\s*['\"]?(\d+\s*[smhdw])['\"]?\s*\))",
    re.IGNORECASE,
)


@dataclass
class AlertingIR:
    """Source-agnostic alerting / monitor asset.

    ``kind`` distinguishes the alert family (e.g. ``grafana_legacy``,
    ``datadog_monitor``). Source-specific condition detail lives in
    ``source_extension`` rather than a fake unified condition AST.
    """

    version: int = 1
    alert_id: str = ""
    name: str = ""
    kind: str = ""
    source_ref: str = ""
    condition_summary: str = ""
    evaluation_window: str = ""
    severity: str = ""
    no_data_policy: str = ""

    actions: list[dict[str, Any]] = field(default_factory=list)
    linked_assets: list[str] = field(default_factory=list)

    status: AssetStatus = AssetStatus.MANUAL_REQUIRED
    manual_required: bool = True
    target_candidate: str = ""
    losses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_extension: dict[str, Any] = field(default_factory=dict)

    automation_tier: str = ""  # "automated", "draft_requires_review", "manual_required"
    target_rule_type: str = ""  # emitted rule type, e.g. "es-query", "index-threshold"
    selected_target_rule_type: str = ""  # candidate rule type before emission checks
    payload_emitted: bool = False
    payload_status: str = ""  # emitted, blocked_manual_review, blocked_no_source_faithful_query, ...
    payload_status_reason: str = ""
    target_rule_payload: dict[str, Any] = field(default_factory=dict)
    target_connector_refs: list[str] = field(default_factory=list)
    schedule_interval: str = ""  # e.g. "1m", "5m"
    pending_period: str = ""  # Grafana "for" / evaluation stability window
    group_by: list[str] = field(default_factory=list)
    translated_query: str = ""  # ES|QL or KQL query for the target rule
    translated_query_provenance: str = ""  # e.g. translated_esql, native_promql, manual_verified
    notification_summary: str = ""  # human-readable notification routing summary

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


def build_alerting_ir_from_grafana(alert_task: dict[str, Any]) -> AlertingIR:
    """Build an AlertingIR from a Grafana alert migration task dict."""
    return AlertingIR(
        alert_id=str(alert_task.get("dashboard_uid", "") or "") + "/" + str(alert_task.get("panel", "") or ""),
        name=str(alert_task.get("alert_name", "") or ""),
        kind="grafana_legacy",
        source_ref=str(alert_task.get("panel", "") or ""),
        condition_summary="; ".join(alert_task.get("conditions_description", [])),
        evaluation_window=str(alert_task.get("frequency", "") or ""),
        severity="",
        no_data_policy=str(alert_task.get("no_data_state", "") or ""),
        actions=[{"notification_channels": alert_task.get("notification_channels", [])}],
        linked_assets=[str(alert_task.get("panel", ""))],
        target_candidate=str(alert_task.get("suggested_kibana_rule_type", "") or ""),
        source_extension={
            "alert_type": alert_task.get("alert_type", "legacy"),
            "exec_error_state": alert_task.get("exec_error_state", ""),
            "pending_for": alert_task.get("pending_for", ""),
            "conditions": alert_task.get("conditions", []),
            "source_queries": list(alert_task.get("source_queries", []) or []),
            "datasource_map": dict(alert_task.get("datasource_map", {}) or {}),
        },
    )


def _parse_datadog_query_time_window(query: str) -> str:
    if not query:
        return ""
    query_text = str(query or "").strip()
    change_match = re.search(
        r"^(?:change|pct_change)\(\s*(?:avg|sum|min|max)\(\s*(last_\d+\s*[smhdw])\s*\)\s*,\s*((?:last_)?\d+\s*[smhdw](?:_ago)?)\s*\)",
        query_text,
        re.IGNORECASE,
    )
    if change_match:
        total_seconds = (
            _datadog_span_to_seconds(change_match.group(1))
            + _datadog_span_to_seconds(change_match.group(2))
        )
        return _datadog_seconds_to_window(total_seconds)
    m = _REL_LAST_RE.search(query_text)
    if not m:
        return ""
    base_window = (m.group(1) or m.group(2) or "").replace(" ", "")
    shift_seconds = _datadog_formula_shift_seconds(query_text)
    if shift_seconds <= 0:
        return base_window
    total_seconds = _datadog_span_to_seconds(base_window) + shift_seconds
    return _datadog_seconds_to_window(total_seconds)


def _datadog_metric_family_kind(query: str) -> str:
    expression = str(query or "").strip().lower()
    if ":" in expression:
        expression = expression.split(":", 1)[1].strip()
    if expression.startswith("anomalies(") or expression.startswith("anomaly("):
        return "datadog_anomaly_alert"
    if expression.startswith("forecast("):
        return "datadog_forecast"
    if expression.startswith("outliers(") or expression.startswith("outlier("):
        return "datadog_outlier"
    return ""


def _datadog_kind(monitor_type: str, query: str = "") -> str:
    t = (monitor_type or "").lower().strip()
    if t in ("metric alert", "query alert"):
        family_kind = _datadog_metric_family_kind(query)
        if family_kind:
            return family_kind
        return "datadog_metric"
    if t == "log alert":
        return "datadog_log"
    if t == "composite":
        return "datadog_composite"
    if t == "service check":
        return "datadog_service_check"
    if not t:
        return "datadog_monitor"
    safe = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return f"datadog_{safe}"


def _datadog_span_to_seconds(raw: str) -> int:
    match = re.fullmatch(
        r"(?:last_)?(?P<amount>\d+)\s*(?P<unit>[smhdw])(?:_ago)?",
        str(raw or "").strip(),
        re.IGNORECASE,
    )
    if not match:
        return 0
    amount = int(match.group("amount"))
    unit = str(match.group("unit") or "").lower()
    unit_seconds = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }
    return amount * unit_seconds.get(unit, 0)


def _datadog_formula_shift_seconds(query: str) -> int:
    query_text = str(query or "")
    shift_seconds = 0
    for token, seconds in {
        "hour_before(": 3600,
        "day_before(": 86400,
        "week_before(": 7 * 86400,
        "month_before(": 28 * 86400,
    }.items():
        if token in query_text.lower():
            shift_seconds = max(shift_seconds, seconds)
    for match in re.finditer(
        r"timeshift\([^,]+,\s*(-?\d+(?:\.\d+)?)\s*\)",
        query_text,
        re.IGNORECASE,
    ):
        try:
            shift_seconds = max(shift_seconds, int(abs(float(match.group(1)))))
        except (TypeError, ValueError):
            continue
    search_from = 0
    lowered = query_text.lower()
    marker = "calendar_shift("
    while True:
        start = lowered.find(marker, search_from)
        if start < 0:
            break
        end = _find_matching_call_paren(query_text, start + len(marker) - 1)
        if end < 0:
            break
        args = _split_balanced_args(query_text[start + len(marker):end])
        if len(args) == 3:
            shift_seconds = max(
                shift_seconds,
                _datadog_calendar_shift_seconds(args[1], args[2]),
            )
        search_from = end + 1
    return shift_seconds


def _find_matching_call_paren(text: str, open_paren_index: int) -> int:
    depth = 0
    brace_depth = 0
    in_quote = False
    quote_char = ""
    for idx in range(open_paren_index, len(text)):
        ch = text[idx]
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
            continue
        if in_quote and ch == quote_char:
            in_quote = False
            continue
        if in_quote:
            continue
        if ch == "{":
            brace_depth += 1
            continue
        if ch == "}":
            brace_depth = max(brace_depth - 1, 0)
            continue
        if ch == "(":
            depth += 1
            continue
        if ch == ")" and brace_depth == 0:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _split_balanced_args(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    paren_depth = 0
    brace_depth = 0
    in_quote = False
    quote_char = ""
    for ch in text:
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
            current.append(ch)
            continue
        if in_quote and ch == quote_char:
            in_quote = False
            current.append(ch)
            continue
        if in_quote:
            current.append(ch)
            continue
        if ch == "(":
            paren_depth += 1
            current.append(ch)
            continue
        if ch == ")":
            paren_depth = max(paren_depth - 1, 0)
            current.append(ch)
            continue
        if ch == "{":
            brace_depth += 1
            current.append(ch)
            continue
        if ch == "}":
            brace_depth = max(brace_depth - 1, 0)
            current.append(ch)
            continue
        if ch == "," and paren_depth == 0 and brace_depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return parts


def _datadog_calendar_shift_seconds(raw_shift: str, raw_timezone: str) -> int:
    timezone_name = str(raw_timezone or "").strip().strip("'\"")
    if not _datadog_calendar_shift_timezone_is_exact(timezone_name):
        return 0
    shift = str(raw_shift or "").strip().strip("'\"")
    match = re.fullmatch(r"-(?P<amount>\d+)(?P<unit>d|w|mo)", shift, re.IGNORECASE)
    if not match:
        return 0
    amount = int(match.group("amount"))
    unit = str(match.group("unit") or "").lower()
    if unit == "d":
        return amount * 86400
    if unit == "w":
        return amount * 7 * 86400
    if unit == "mo":
        return amount * 32 * 86400
    return 0


@cache
def _datadog_calendar_shift_timezone_is_exact(timezone_name: str) -> bool:
    normalized = str(timezone_name or "").strip()
    if not normalized:
        return False
    if normalized.upper() == "UTC":
        return True
    try:
        tzinfo = ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        return False

    current_year = datetime.now(UTC).year
    offsets = {
        datetime(year, month, 15, 12, 0, tzinfo=UTC).astimezone(tzinfo).utcoffset()
        for year in range(current_year - 1, current_year + 11)
        for month in range(1, 13)
    }
    return len(offsets) == 1


def _datadog_seconds_to_window(total_seconds: int) -> str:
    if total_seconds <= 0:
        return ""
    if total_seconds % 3600 == 0:
        return f"{total_seconds // 3600}h"
    if total_seconds % 60 == 0:
        return f"{total_seconds // 60}m"
    return f"{total_seconds}s"


def _datadog_automation_tier(monitor_type: str) -> str:
    t = (monitor_type or "").lower().strip()
    if t in ("metric alert", "query alert", "log alert"):
        return "draft_requires_review"
    if t == "composite":
        return "manual_required"
    return "manual_required"


def _datadog_target_rule_type(monitor_type: str) -> str:
    t = (monitor_type or "").lower().strip()
    if t in ("metric alert", "query alert"):
        return "custom-threshold"
    return "es-query"


def _datadog_no_data_policy(options: dict[str, Any]) -> str:
    if not isinstance(options, dict):
        return ""
    raw = options.get("notify_no_data")
    if raw is True:
        return "notify"
    if raw is False:
        return "no_notify"
    if raw is None:
        return ""
    return str(raw)


def _summarize_datadog_condition(monitor: dict[str, Any]) -> str:
    parts: list[str] = []
    q = monitor.get("query")
    if q:
        parts.append(str(q))
    opts = monitor.get("options")
    if isinstance(opts, dict):
        th = opts.get("thresholds")
        if th:
            parts.append(f"thresholds={th}")
    return "; ".join(parts) if parts else ""


def build_alerting_ir_from_datadog(
    monitor: dict[str, Any],
    field_map: Any | None = None,
) -> AlertingIR:
    """Build an AlertingIR from a raw Datadog monitor API-style dict."""
    mtype = str(monitor.get("type", "") or "")
    query = str(monitor.get("query", "") or "")
    kind = _datadog_kind(mtype, query)
    opts = monitor.get("options") if isinstance(monitor.get("options"), dict) else {}
    name = str(monitor.get("name", "") or "")
    alert_id = str(monitor.get("id", "") or monitor.get("monitor_id", "") or "")

    automation_tier = _datadog_automation_tier(mtype)
    target_rule_type = _datadog_target_rule_type(mtype)
    eval_win = _parse_datadog_query_time_window(query)
    no_data = _datadog_no_data_policy(opts)
    condition_summary = _summarize_datadog_condition(monitor)
    message = str(monitor.get("message", "") or "")
    priority = monitor.get("priority")
    severity = str(priority) if priority is not None else ""

    metadata: dict[str, Any] = {
        "datadog_type": mtype,
        "tags": monitor.get("tags") or [],
    }
    if monitor.get("multi") is not None:
        metadata["multi"] = monitor.get("multi")

    ir = AlertingIR(
        alert_id=alert_id,
        name=name,
        kind=kind,
        source_ref=str(monitor.get("id", "") or ""),
        condition_summary=condition_summary,
        evaluation_window=eval_win,
        severity=severity,
        no_data_policy=no_data,
        metadata=metadata,
        source_extension=dict(monitor),
        automation_tier=automation_tier,
        target_rule_type=target_rule_type,
        notification_summary=message[:2000] if message else "",
    )

    if field_map is not None:
        try:
            from observability_migration.adapters.source.datadog.monitor_translate import (
                translate_monitor_to_alert_query,
            )
        except ImportError:
            return ir

        translation = translate_monitor_to_alert_query(monitor, field_map)
        if translation.translated_query:
            ir.translated_query = translation.translated_query
            ir.translated_query_provenance = translation.translated_query_provenance
            ir.group_by = list(translation.group_by)
        if translation.warnings:
            ir.warnings.extend(w for w in translation.warnings if w not in ir.warnings)
        if translation.parse_diagnostics:
            ir.metadata["parser_diagnostics"] = list(translation.parse_diagnostics)
        if translation.parse_degraded:
            ir.metadata["parse_degraded"] = True

    return ir


def _grafana_unified_evaluation_window(rule: dict[str, Any]) -> str:
    data = rule.get("data")
    if not isinstance(data, list) or not data:
        return ""
    first = data[0]
    if not isinstance(first, dict):
        return ""
    rtr = first.get("relativeTimeRange")
    if not isinstance(rtr, dict):
        return ""
    from_sec = rtr.get("from")
    if from_sec is None:
        return ""
    try:
        s = int(from_sec)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    if s % 3600 == 0:
        return f"{s // 3600}h"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


def _grafana_source_queries(
    data: list[dict[str, Any]],
    datasource_map: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    queries: list[dict[str, Any]] = []
    used_datasources: dict[str, dict[str, Any]] = {}
    ds_map = datasource_map if isinstance(datasource_map, dict) else {}

    for item in data:
        if not isinstance(item, dict):
            continue
        datasource_uid = str(item.get("datasourceUid", "") or "")
        if not datasource_uid or datasource_uid == "__expr__":
            continue
        model = item.get("model") if isinstance(item.get("model"), dict) else {}
        expr = str(model.get("expr", "") or "")
        ds_meta = ds_map.get(datasource_uid, {}) if isinstance(ds_map.get(datasource_uid), dict) else {}
        queries.append(
            {
                "ref_id": str(item.get("refId", "") or ""),
                "datasource_uid": datasource_uid,
                "datasource_type": str(ds_meta.get("type", "") or ""),
                "datasource_name": str(ds_meta.get("name", "") or ""),
                "expr": expr,
            }
        )
        if ds_meta:
            used_datasources[datasource_uid] = {
                "uid": datasource_uid,
                "type": str(ds_meta.get("type", "") or ""),
                "name": str(ds_meta.get("name", "") or ""),
            }

    return queries, used_datasources


def build_alerting_ir_from_grafana_unified(
    rule: dict[str, Any],
    datasource_map: dict[str, dict[str, Any]] | None = None,
) -> AlertingIR:
    """Build an AlertingIR from a Grafana Unified Alerting provisioned rule dict."""
    title = str(rule.get("title", "") or "")
    uid = str(rule.get("uid", "") or "")
    alert_id = uid if uid else title
    condition = str(rule.get("condition", "") or "")
    data = rule.get("data") if isinstance(rule.get("data"), list) else []
    labels = rule.get("labels") if isinstance(rule.get("labels"), dict) else {}

    annotations = rule.get("annotations")
    if not isinstance(annotations, dict):
        annotations = {}

    cond_bits = [title, f"condition={condition}"] if condition else [title]
    condition_summary = "; ".join(cond_bits)

    pending = str(rule.get("for", "") or "")
    source_queries, used_datasources = _grafana_source_queries(data, datasource_map)

    source_extension: dict[str, Any] = {
        "data": data,
        "labels": dict(labels),
        "annotations": dict(annotations),
        "source_queries": source_queries,
        "datasource_map": used_datasources,
    }

    ann_summary = " ".join(f"{k}={v}" for k, v in annotations.items() if v)[:2000]

    return AlertingIR(
        alert_id=alert_id,
        name=title,
        kind="grafana_unified",
        source_ref=uid,
        condition_summary=condition_summary,
        evaluation_window=_grafana_unified_evaluation_window(rule),
        severity="",
        no_data_policy=str(rule.get("noDataState", "") or ""),
        metadata={
            "exec_err_state": str(rule.get("execErrState", "") or ""),
            "grafana_condition": condition,
            "datasource_types": sorted(
                {q["datasource_type"] for q in source_queries if q.get("datasource_type")}
            ),
        },
        source_extension=source_extension,
        automation_tier="draft_requires_review",
        target_rule_type="es-query",
        pending_period=pending,
        notification_summary=ann_summary,
    )


__all__ = [
    "AlertingIR",
    "build_alerting_ir_from_datadog",
    "build_alerting_ir_from_grafana",
    "build_alerting_ir_from_grafana_unified",
]
