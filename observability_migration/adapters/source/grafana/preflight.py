# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Customer preflight validation: pre-ingest readiness assessment."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Source-side probes (Prometheus / Loki metadata — no ES data needed)
# ---------------------------------------------------------------------------

def probe_source_metric_inventory(
    prometheus_url: str,
    required_metrics: set[str] | None = None,
    required_labels: set[str] | None = None,
    *,
    timeout: int = 15,
    verify: bool | str = True,
) -> dict[str, Any]:
    """Query Prometheus metadata to build a metric and label inventory.

    Cross-references against *required_metrics* and *required_labels* (from
    the translated queries) so the preflight report can say "these 12 metrics
    your dashboards reference don't exist in Prometheus."
    """
    result: dict[str, Any] = {
        "status": "not_configured",
        "available_metrics": [],
        "available_labels": [],
        "metrics_found": [],
        "metrics_missing": [],
        "labels_found": [],
        "labels_missing": [],
        "error": "",
    }
    if not prometheus_url:
        return result

    base = prometheus_url.rstrip("/")

    try:
        resp = requests.get(
            f"{base}/api/v1/label/__name__/values", timeout=timeout, verify=verify,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") == "success":
            result["available_metrics"] = sorted(body.get("data", []))
    except Exception as exc:
        result["error"] = f"metric inventory: {exc}"
        result["status"] = "error"
        return result

    try:
        resp = requests.get(f"{base}/api/v1/labels", timeout=timeout, verify=verify)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") == "success":
            result["available_labels"] = sorted(body.get("data", []))
    except Exception as exc:
        result["error"] = f"label inventory: {exc}"

    available_metric_set = set(result["available_metrics"])
    available_label_set = set(result["available_labels"])

    if required_metrics:
        result["metrics_found"] = sorted(required_metrics & available_metric_set)
        result["metrics_missing"] = sorted(required_metrics - available_metric_set)
    if required_labels:
        result["labels_found"] = sorted(required_labels & available_label_set)
        result["labels_missing"] = sorted(required_labels - available_label_set)

    result["status"] = "ok"
    return result


# ---------------------------------------------------------------------------
# Target infrastructure readiness (no data needed — just cluster shape)
# ---------------------------------------------------------------------------

def probe_target_readiness(
    es_url: str,
    required_index_patterns: list[str] | None = None,
    *,
    timeout: int = 10,
    es_api_key: str | None = None,
    verify: bool | str = True,
) -> dict[str, Any]:
    """Check Elasticsearch cluster health, index templates, and data streams.

    None of this requires actual document data — it validates that the
    infrastructure is ready to *receive* data.
    """
    result: dict[str, Any] = {
        "status": "not_configured",
        "cluster_health": {},
        "index_templates": {},
        "data_streams": {},
        "errors": [],
    }
    if not es_url:
        return result

    base = es_url.rstrip("/")
    headers: dict[str, str] = {}
    if es_api_key:
        headers["Authorization"] = f"ApiKey {es_api_key}"

    try:
        resp = requests.get(f"{base}/_cluster/health", headers=headers, timeout=timeout, verify=verify)
        if resp.status_code == 200:
            health = resp.json()
            result["cluster_health"] = {
                "status": health.get("status", "unknown"),
                "number_of_nodes": health.get("number_of_nodes", 0),
                "number_of_data_nodes": health.get("number_of_data_nodes", 0),
                "active_shards": health.get("active_shards", 0),
            }
        elif resp.status_code == 410:
            result["cluster_health"] = {
                "status": "serverless",
                "number_of_nodes": 0,
                "number_of_data_nodes": 0,
                "active_shards": 0,
                "unsupported": True,
                "message": "Cluster health API is not available on Elasticsearch Serverless.",
            }
        else:
            result["errors"].append(f"cluster health: HTTP {resp.status_code}")
    except Exception as exc:
        result["errors"].append(f"cluster health: {exc}")
        result["status"] = "error"
        return result

    for pattern in required_index_patterns or []:
        tpl_key = pattern.replace("*", "").rstrip("-")
        try:
            resp = requests.get(
                f"{base}/_index_template/{tpl_key}*", headers=headers, timeout=timeout, verify=verify,
            )
            if resp.status_code == 200:
                templates = resp.json().get("index_templates", [])
                result["index_templates"][pattern] = {
                    "found": len(templates),
                    "names": [t.get("name", "") for t in templates[:10]],
                }
            else:
                result["index_templates"][pattern] = {
                    "found": 0, "names": [],
                }
        except Exception as exc:
            result["errors"].append(f"index template {pattern}: {exc}")

        try:
            resp = requests.get(
                f"{base}/_data_stream/{pattern}", headers=headers, timeout=timeout, verify=verify,
            )
            if resp.status_code == 200:
                streams = resp.json().get("data_streams", [])
                result["data_streams"][pattern] = {
                    "found": len(streams),
                    "names": [s.get("name", "") for s in streams[:10]],
                }
            else:
                result["data_streams"][pattern] = {"found": 0, "names": []}
        except Exception as exc:
            result["errors"].append(f"data stream {pattern}: {exc}")

    result["status"] = "ok" if not result["errors"] else "partial"
    return result


# ---------------------------------------------------------------------------
# Datasource audit + dashboard complexity scoring (pure offline)
# ---------------------------------------------------------------------------

def _is_grafana_variable_ref(value: str) -> bool:
    """Check if a datasource type/name is a Grafana template variable reference."""
    stripped = value.strip()
    return stripped.startswith("$") or stripped.startswith("${") or stripped.startswith("[[")


def _is_uid_like(value: str) -> bool:
    """Heuristic: alphanumeric strings that are likely Grafana datasource UIDs."""
    stripped = value.strip()
    if len(stripped) < 8:
        return False
    allowed = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-")
    if not all(c in allowed for c in stripped):
        return False
    has_digit = any(c.isdigit() for c in stripped)
    has_alpha = any(c.isalpha() for c in stripped)
    if not (has_digit and has_alpha):
        return False
    if stripped.lower() in _KNOWN_MIGRATABLE_TYPES | _KNOWN_NON_MIGRATABLE_TYPES:
        return False
    return True


_KNOWN_MIGRATABLE_TYPES = {
    "prometheus", "loki", "elasticsearch", "grafana-elasticsearch-datasource",
    "", "unknown",
}

_KNOWN_NON_MIGRATABLE_TYPES = {
    "influxdb", "mysql", "postgres", "graphite", "cloudwatch", "stackdriver",
    "opentsdb", "mssql", "tempo", "jaeger", "zipkin",
}


def build_datasource_audit(results: list[Any]) -> dict[str, Any]:
    """Inventory datasource distribution and flag non-migratable types."""
    ds_counter: Counter = Counter()
    ds_panels: dict[str, int] = {}
    non_migratable: list[dict[str, str]] = []
    variable_refs: Counter = Counter()

    seen_non_migratable: set[str] = set()
    for result in results:
        for pr in getattr(result, "panel_results", []) or []:
            ds_type = str(getattr(pr, "datasource_type", "") or "").lower()
            ds_name = str(getattr(pr, "datasource_name", "") or "") or ds_type
            key = f"{ds_type}:{ds_name}"
            ds_counter[key] += 1
            ds_panels[key] = ds_panels.get(key, 0) + 1

            if _is_grafana_variable_ref(ds_type) or _is_grafana_variable_ref(ds_name):
                variable_refs[key] += 1
                continue
            if _is_uid_like(ds_type):
                continue
            if ds_type in _KNOWN_MIGRATABLE_TYPES:
                continue

            if key not in seen_non_migratable:
                seen_non_migratable.add(key)
                non_migratable.append({
                    "type": ds_type,
                    "name": ds_name,
                    "dashboard": str(getattr(result, "dashboard_title", "")),
                })

    type_summary: Counter = Counter()
    for key, count in ds_counter.items():
        ds_type = key.split(":")[0] or "unknown"
        if _is_grafana_variable_ref(ds_type):
            type_summary["variable_ref"] += count
        elif _is_uid_like(ds_type):
            type_summary["uid_ref"] += count
        else:
            type_summary[ds_type] += count

    return {
        "datasource_types": dict(type_summary.most_common()),
        "datasource_details": dict(ds_counter.most_common()),
        "variable_refs": dict(variable_refs.most_common()),
        "non_migratable": non_migratable,
        "non_migratable_panels": sum(
            ds_panels.get(f"{item['type']}:{item['name']}", 0)
            for item in non_migratable
        ),
    }


def build_dashboard_complexity(results: list[Any]) -> list[dict[str, Any]]:
    """Score each dashboard by migration complexity factors."""
    scored: list[dict[str, Any]] = []
    for result in results:
        inv = getattr(result, "inventory", {}) or {}
        factors: list[str] = []
        score = 0

        panel_count = result.total_panels
        score += panel_count

        nf = result.not_feasible
        manual = result.requires_manual
        if nf:
            score += nf * 5
            factors.append(f"{nf} not-feasible panels")
        if manual:
            score += manual * 3
            factors.append(f"{manual} requires-manual panels")

        transformations = sum(
            (getattr(pr, "inventory", {}) or {}).get("transformations", 0)
            for pr in getattr(result, "panel_results", []) or []
        )
        if transformations:
            score += transformations * 4
            factors.append(f"{transformations} Grafana transformations")

        links = sum(
            (getattr(pr, "inventory", {}) or {}).get("links", 0)
            for pr in getattr(result, "panel_results", []) or []
        )
        if links:
            score += links * 2
            factors.append(f"{links} panel links")

        repeaters = sum(
            1
            for pr in getattr(result, "panel_results", []) or []
            if (getattr(pr, "inventory", {}) or {}).get("has_repeat")
        )
        if repeaters:
            score += repeaters * 3
            factors.append(f"{repeaters} repeating panels")

        variables = inv.get("variables", 0) or 0
        if variables > 5:
            score += (variables - 5) * 2
            factors.append(f"{variables} template variables")

        annotations = inv.get("annotations", 0) or 0
        if annotations:
            score += annotations * 2
            factors.append(f"{annotations} annotations")

        mixed_ds = sum(
            1
            for pr in getattr(result, "panel_results", []) or []
            if any(
                "mixes datasource" in str(note).lower()
                for note in (getattr(pr, "notes", []) or [])
            )
        )
        if mixed_ds:
            score += mixed_ds * 5
            factors.append(f"{mixed_ds} mixed-datasource panels")

        scored.append({
            "dashboard": result.dashboard_title,
            "uid": result.dashboard_uid,
            "panels": panel_count,
            "complexity_score": score,
            "factors": factors,
        })

    scored.sort(key=lambda x: -x["complexity_score"])
    return scored


# ---------------------------------------------------------------------------
# Helpers for extracting referenced metrics/labels from QueryIR
# ---------------------------------------------------------------------------

_FIELD_TOKEN_RE = re.compile(r"\b([A-Za-z_:][A-Za-z0-9_:]*)\s*(?=\{|\[)")
_BARE_FIELD_TOKEN_RE = re.compile(r"\b([A-Za-z_:][A-Za-z0-9_:]*)\b(?!\s*\()")
_LABEL_FILTER_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*(?:!?=~?|=)")
_DERIVED_METRIC_NAMES = {"computed_value", "constant_value", "log_count", "value"}
_PROMQL_KEYWORDS = {
    "abs",
    "and",
    "avg",
    "bool",
    "bottomk",
    "by",
    "ceil",
    "count",
    "count_values",
    "floor",
    "group",
    "group_left",
    "group_right",
    "histogram_quantile",
    "ignoring",
    "irate",
    "label_join",
    "label_replace",
    "le",
    "max",
    "min",
    "offset",
    "on",
    "or",
    "predict_linear",
    "quantile",
    "rate",
    "round",
    "scalar",
    "stddev",
    "stdvar",
    "sum",
    "time",
    "topk",
    "unless",
    "vector",
    "without",
}
_PROMQL_SET_OPERATORS = frozenset({
    "or",
    "and",
    "unless",
    "bool",
    "on",
    "ignoring",
    "group_left",
    "group_right",
})
_COUNTER_SUFFIXES = ("_total", "_count", "_sum")
_GAUGE_RESOURCE_TOKENS = ("resource_limit", "resource_limits", "resource_request", "resource_requests")


def _add_required_field(
    required_fields: dict[str, dict[str, Any]],
    field_name: str,
    role: str,
    *,
    source_field: str | None = None,
) -> None:
    field_name = str(field_name or "").strip()
    if not field_name or field_name in {"@timestamp", "time_bucket", "timestamp_bucket", "step", "__name__"}:
        return
    source_field = str(source_field or field_name).strip()
    entry = required_fields.setdefault(
        field_name,
        {
            "target_field": field_name,
            "source_fields": set(),
            "roles": set(),
            "panels": 0,
        },
    )
    if source_field:
        entry["source_fields"].add(source_field)
    entry["roles"].add(role)
    entry["panels"] += 1


def _metric_candidates(query_ir: dict[str, Any]) -> set[str]:
    candidates = {
        str(query_ir.get("source_metric", "") or "").strip(),
        str(query_ir.get("metric", "") or "").strip(),
    }
    output_metric = str(query_ir.get("output_metric_field", "") or "").strip()
    candidates.discard("")
    if output_metric in candidates:
        candidates.discard(output_metric)
    candidates.difference_update(_DERIVED_METRIC_NAMES)

    expression = str(query_ir.get("clean_expression", "") or query_ir.get("source_expression", "") or "")
    candidates.update(match.group(1) for match in _FIELD_TOKEN_RE.finditer(expression))
    # Strip non-metric token regions before the bare-identifier scan: labels
    # inside `by(...)` / `without(...)` aggregation clauses and inside PromQL
    # vector-match modifiers `on(...)` / `ignoring(...)` /
    # `group_left(...)` / `group_right(...)`, Grafana interpolation tokens
    # like `$__rate_interval` / `$cluster`, and the contents of bracketed
    # time ranges like `[$__rate_interval]`. These can otherwise be picked
    # up as metric names.
    expression_stripped = re.sub(
        r"\b(?:by|without)\s*\(([^()]*)\)",
        "",
        expression,
        flags=re.IGNORECASE,
    )
    expression_stripped = re.sub(
        r"\b(?:on|ignoring|group_left|group_right)\s*\(([^()]*)\)",
        "",
        expression_stripped,
        flags=re.IGNORECASE,
    )
    expression_stripped = re.sub(r"\$[A-Za-z_][A-Za-z0-9_]*", "", expression_stripped)
    expression_stripped = re.sub(r"\[[^\]]*\]", "", expression_stripped)
    expression_without_labels = re.sub(r"\{[^{}]*\}", "", expression_stripped)
    expression_without_literals = re.sub(r'"[^"]*"', '""', expression_without_labels)
    candidates.update(
        match.group(1)
        for match in _BARE_FIELD_TOKEN_RE.finditer(expression_without_literals)
        if match.group(1).lower() not in _PROMQL_KEYWORDS
        and match.group(1).lower() not in _PROMQL_SET_OPERATORS
    )
    return {
        item
        for item in candidates
        if item
        and item not in {"scalar", "vector"}
        and item.lower() not in _PROMQL_SET_OPERATORS
    }


def _label_filter_field(filter_expr: Any) -> str:
    match = _LABEL_FILTER_RE.match(str(filter_expr or ""))
    return match.group(1) if match else ""


def _looks_like_counter_metric(metric_name: str) -> bool:
    metric_name = str(metric_name or "")
    if any(token in metric_name for token in _GAUGE_RESOURCE_TOKENS):
        return False
    return metric_name.endswith(_COUNTER_SUFFIXES)


def _collect_referenced_metrics(results: list[Any]) -> set[str]:
    """Collect all metric names referenced in source PromQL expressions."""
    metrics: set[str] = set()
    for result in results:
        for pr in getattr(result, "panel_results", []) or []:
            query_ir = getattr(pr, "query_ir", {}) or {}
            if not isinstance(query_ir, dict):
                continue
            metrics.update(_metric_candidates(query_ir))
    return metrics


def _collect_referenced_labels(results: list[Any]) -> set[str]:
    """Collect all label names used in source PromQL group-by and filters."""
    labels: set[str] = set()
    skip = {"@timestamp", "time_bucket", "__name__"}
    for result in results:
        for pr in getattr(result, "panel_results", []) or []:
            query_ir = getattr(pr, "query_ir", {}) or {}
            if not isinstance(query_ir, dict):
                continue
            for field in query_ir.get("source_group_fields", []) or []:
                if field and field not in skip:
                    labels.add(field)
            for field in query_ir.get("source_filter_fields", []) or []:
                if field and field not in skip:
                    labels.add(field)
    return labels


# ---------------------------------------------------------------------------
# Schema contract (existing)
# ---------------------------------------------------------------------------

def build_target_schema_contract(
    results: list[Any],
    resolver: Any = None,
) -> dict[str, Any]:
    """Extract required target indexes, fields, types, and counters from all QueryIR results."""
    required_indexes: Counter = Counter()
    required_fields: dict[str, dict[str, Any]] = {}
    counter_expectations: dict[str, int] = {}
    counter_sources: dict[str, str] = {}
    unresolved_labels: Counter = Counter()
    unresolved_variables: Counter = Counter()
    feature_gaps: list[str] = []
    schema_profile = None
    field_capabilities_index = ""
    field_capabilities_discovery = {"status": "not_attempted", "error": "", "field_count": 0}
    if resolver:
        schema_profile_fn = getattr(resolver, "schema_profile", None)
        if callable(schema_profile_fn):
            schema_profile = schema_profile_fn()
        field_capabilities_index = str(getattr(resolver, "_index_pattern", "") or "")
        discovery_status_fn = getattr(resolver, "discovery_status", None)
        if callable(discovery_status_fn):
            field_capabilities_discovery = discovery_status_fn()

    seen_features: set[str] = set()

    for result in results:
        variables = getattr(result, "inventory", {}).get("variables", 0) or 0
        if variables:
            for pr in getattr(result, "panel_results", []) or []:
                query_ir = getattr(pr, "query_ir", {}) or {}
                if not isinstance(query_ir, dict):
                    continue
                target_query = str(query_ir.get("target_query", "") or "")
                for token in ("$", "[[", "{{"):
                    if token in target_query:
                        unresolved_variables[f"{getattr(pr, 'title', '')}:{token}"] += 1

        for pr in getattr(result, "panel_results", []) or []:
            query_ir = getattr(pr, "query_ir", {}) or {}
            if not isinstance(query_ir, dict):
                continue

            target_index = str(query_ir.get("target_index", "") or "")
            if target_index:
                required_indexes[target_index] += 1

            metric_fields = _metric_candidates(query_ir)
            for metric_field in sorted(metric_fields):
                target_metric = metric_field
                if resolver and hasattr(resolver, "resolve_metric_field"):
                    prefer = "counter" if (
                        str(query_ir.get("source_type", "") or "") == "TS"
                        and _looks_like_counter_metric(metric_field)
                    ) else None
                    target_metric = resolver.resolve_metric_field(metric_field, prefer=prefer)
                _add_required_field(
                    required_fields,
                    target_metric,
                    "metric",
                    source_field=metric_field,
                )

            for group_field in (
                list(query_ir.get("source_group_fields", []) or [])
                + list(query_ir.get("group_labels", []) or [])
            ):
                target_group = group_field
                if resolver and hasattr(resolver, "resolve_label"):
                    target_group = resolver.resolve_label(group_field)
                _add_required_field(
                    required_fields,
                    target_group,
                    "group_by",
                    source_field=group_field,
                )

            for filter_expr in query_ir.get("label_filters", []) or []:
                source_filter = _label_filter_field(filter_expr)
                target_filter = source_filter
                if resolver and hasattr(resolver, "resolve_label"):
                    target_filter = resolver.resolve_label(source_filter)
                _add_required_field(
                    required_fields,
                    target_filter,
                    "filter",
                    source_field=source_filter,
                )
            for filter_field in query_ir.get("source_filter_fields", []) or []:
                target_filter = filter_field
                if resolver and hasattr(resolver, "resolve_label"):
                    target_filter = resolver.resolve_label(filter_field)
                _add_required_field(
                    required_fields,
                    target_filter,
                    "filter",
                    source_field=filter_field,
                )

            source_type = str(query_ir.get("source_type", "") or "")
            if source_type == "TS":
                for metric_field in metric_fields:
                    if not _looks_like_counter_metric(metric_field):
                        continue
                    target_metric = metric_field
                    if resolver and hasattr(resolver, "resolve_metric_field"):
                        target_metric = resolver.resolve_metric_field(metric_field, prefer="counter")
                    counter_sources[target_metric] = metric_field
                    counter_expectations[target_metric] = (
                        counter_expectations.get(target_metric, 0) + 1
                    )

            for loss in query_ir.get("semantic_losses", []) or []:
                loss_str = str(loss)
                if loss_str not in seen_features:
                    seen_features.add(loss_str)
                    feature_gaps.append(loss_str)

            for warning in getattr(pr, "reasons", []) or []:
                warning_lower = str(warning).lower()
                if "unresolved" in warning_lower or "unknown" in warning_lower:
                    unresolved_labels[str(warning)] += 1

    field_status: dict[str, dict[str, Any]] = {}
    for field_name, info in required_fields.items():
        status = "unknown"
        field_type = None
        if resolver:
            exists = resolver.field_exists(field_name)
            if exists is True:
                status = "confirmed"
                field_type = resolver.field_type(field_name)
            elif exists is False:
                status = "missing"
        field_status[field_name] = {
            "status": status,
            "type": field_type,
            "target_field": info.get("target_field", field_name),
            "source_fields": sorted(info.get("source_fields", {field_name})),
            "roles": sorted(info["roles"]),
            "panels": info["panels"],
        }

    counter_status: dict[str, dict[str, Any]] = {}
    for metric_name, count in sorted(
        counter_expectations.items(), key=lambda x: -x[1],
    ):
        is_counter = None
        if resolver:
            is_counter = resolver.is_counter(counter_sources.get(metric_name, metric_name))
        counter_status[metric_name] = {
            "source_field": counter_sources.get(metric_name, metric_name),
            "target_field": metric_name,
            "expected_counter": True,
            "confirmed_counter": is_counter,
            "panels": count,
        }

    confirmed = sum(1 for v in field_status.values() if v["status"] == "confirmed")
    missing = sum(1 for v in field_status.values() if v["status"] == "missing")
    unknown = sum(1 for v in field_status.values() if v["status"] == "unknown")

    return {
        "schema_profile": schema_profile,
        "field_capabilities_index": field_capabilities_index,
        "field_capabilities_discovery": field_capabilities_discovery,
        "required_indexes": dict(required_indexes.most_common()),
        "required_fields": field_status,
        "counter_expectations": counter_status,
        "unresolved_variables": dict(unresolved_variables.most_common()),
        "feature_gaps": feature_gaps[:50],
        "totals": {
            "indexes": len(required_indexes),
            "fields": len(field_status),
            "fields_confirmed": confirmed,
            "fields_missing": missing,
            "fields_unknown": unknown,
            "counters_expected": len(counter_status),
        },
    }


def build_target_contract_summary(results: list[Any]) -> dict[str, Any]:
    """Summarize target query contract outcomes across all translated panels."""
    status_counter: Counter = Counter()
    action_kinds: Counter = Counter()

    for result in results:
        for panel in getattr(result, "panel_results", []) or []:
            evaluation = getattr(panel, "contract_evaluation", {}) or {}
            status = str(evaluation.get("status", "") or "")
            if status:
                status_counter[status] += 1

            plan = getattr(panel, "fulfillment_plan", {}) or {}
            for action in plan.get("actions", []) or []:
                kind = str(action.get("kind", "") or "")
                if kind:
                    action_kinds[kind] += 1

    return {
        "totals": dict(status_counter),
        "action_kinds": dict(action_kinds),
    }


def build_preflight_report(
    results: list[Any],
    validation_summary: dict[str, Any],
    validation_records: list[dict[str, Any]],
    verification_payload: dict[str, Any],
    schema_contract: dict[str, Any],
    target_contract_summary: dict[str, Any] | None = None,
    *,
    source_urls_configured: bool = False,
    target_url_configured: bool = False,
    source_inventory: dict[str, Any] | None = None,
    target_readiness: dict[str, Any] | None = None,
    datasource_audit: dict[str, Any] | None = None,
    complexity_scores: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a customer-facing preflight validation report."""
    source_inventory = source_inventory or {}
    target_readiness = target_readiness or {}
    datasource_audit = datasource_audit or {}
    complexity_scores = complexity_scores or []
    target_contract_summary = target_contract_summary or {}

    total_panels = sum(r.total_panels for r in results)
    green = sum(
        1 for r in results for pr in r.panel_results
        if (pr.verification_packet or {}).get("semantic_gate") == "Green"
    )
    yellow = sum(
        1 for r in results for pr in r.panel_results
        if (pr.verification_packet or {}).get("semantic_gate") == "Yellow"
    )
    red = sum(
        1 for r in results for pr in r.panel_results
        if (pr.verification_packet or {}).get("semantic_gate") == "Red"
    )

    ready = sum(
        1 for r in results for pr in r.panel_results
        if getattr(pr, "readiness", "") in {"ready", "elastic_ready"}
    )
    needs_mapping = sum(
        1 for r in results for pr in r.panel_results
        if getattr(pr, "readiness", "") == "metrics_mapping_needed"
    )
    needs_fielding = sum(
        1 for r in results for pr in r.panel_results
        if getattr(pr, "readiness", "") == "logs_fielding_needed"
    )
    manual_only = sum(
        1 for r in results for pr in r.panel_results
        if getattr(pr, "readiness", "") == "manual_only"
    )

    source_passed = 0
    source_failed = 0
    source_not_configured = 0
    for r in results:
        for pr in r.panel_results:
            src = (pr.verification_packet or {}).get("source_execution", {})
            status = src.get("status", "")
            if status == "pass":
                source_passed += 1
            elif status == "fail":
                source_failed += 1
            elif status == "not_configured":
                source_not_configured += 1

    totals = schema_contract.get("totals", {})
    missing_fields = [
        name
        for name, info in schema_contract.get("required_fields", {}).items()
        if info.get("status") == "missing"
    ]
    unconfirmed_counters = [
        name
        for name, info in schema_contract.get("counter_expectations", {}).items()
        if info.get("confirmed_counter") is False
    ]

    blockers: list[str] = []
    actions: list[str] = []

    if red > 0:
        blockers.append(
            f"{red} panels are Red-gated and require manual redesign "
            "or missing data before deployment"
        )
    if missing_fields:
        sample = ", ".join(missing_fields[:10])
        suffix = f" (and {len(missing_fields) - 10} more)" if len(missing_fields) > 10 else ""
        blockers.append(
            f"{len(missing_fields)} required fields missing from target index: {sample}{suffix}"
        )

    missing_metrics = source_inventory.get("metrics_missing", [])
    if missing_metrics:
        sample = ", ".join(missing_metrics[:10])
        suffix = f" (and {len(missing_metrics) - 10} more)" if len(missing_metrics) > 10 else ""
        blockers.append(
            f"{len(missing_metrics)} metrics referenced in dashboards not found "
            f"in Prometheus: {sample}{suffix}"
        )

    cluster_status = (target_readiness.get("cluster_health") or {}).get("status", "")
    if cluster_status == "red":
        blockers.append(
            "Elasticsearch cluster health is RED — resolve cluster issues before ingest"
        )

    non_mig_panels = datasource_audit.get("non_migratable_panels", 0)
    if non_mig_panels:
        non_mig_types = [
            item["type"] for item in datasource_audit.get("non_migratable", [])
        ]
        blockers.append(
            f"{non_mig_panels} panels use non-migratable datasources "
            f"({', '.join(sorted(set(non_mig_types))[:5])})"
        )

    if unconfirmed_counters:
        sample = ", ".join(unconfirmed_counters[:10])
        actions.append(
            f"{len(unconfirmed_counters)} metrics expected as counter type but not confirmed: {sample}"
        )
    if needs_mapping > 0:
        actions.append(
            f"{needs_mapping} panels need metrics field mapping before target validation"
        )
    if needs_fielding > 0:
        actions.append(
            f"{needs_fielding} panels need log field mapping before target validation"
        )
    if source_failed > 0:
        actions.append(
            f"{source_failed} panels failed source-side validation; source queries may need review"
        )

    missing_labels = source_inventory.get("labels_missing", [])
    if missing_labels:
        sample = ", ".join(missing_labels[:10])
        actions.append(
            f"{len(missing_labels)} labels referenced in queries not found "
            f"in Prometheus: {sample}"
        )

    if cluster_status == "yellow":
        actions.append(
            "Elasticsearch cluster health is YELLOW — some replicas may be missing"
        )
    empty_templates = [
        pat for pat, info in (target_readiness.get("index_templates") or {}).items()
        if info.get("found", 0) == 0
    ]
    if empty_templates:
        actions.append(
            f"No index templates found for: {', '.join(empty_templates)}. "
            "Ensure ingest pipelines or OTel collector are configured to create them."
        )
    empty_streams = [
        pat for pat, info in (target_readiness.get("data_streams") or {}).items()
        if info.get("found", 0) == 0
    ]
    if empty_streams:
        actions.append(
            f"No data streams yet for: {', '.join(empty_streams)}. "
            "They will be created on first ingest if templates exist."
        )

    high_complexity = [
        s for s in complexity_scores if s.get("complexity_score", 0) >= 50
    ]
    if high_complexity:
        names = ", ".join(s["dashboard"] for s in high_complexity[:5])
        actions.append(
            f"{len(high_complexity)} dashboards scored high complexity "
            f"(>=50) and will need extra manual review: {names}"
        )

    if not target_url_configured:
        actions.append(
            "Target Elasticsearch URL was not provided; "
            "pass --es-url for runtime query validation"
        )
    if not source_urls_configured:
        actions.append(
            "Source URLs (--prometheus-url, --loki-url) were not provided; "
            "source-side validation was skipped"
        )

    if target_url_configured and source_urls_configured:
        evidence_level = "full"
    elif target_url_configured:
        evidence_level = "target_only"
    elif source_urls_configured:
        evidence_level = "source_only"
    else:
        evidence_level = "static_analysis"

    return {
        "mode": "preflight",
        "evidence_level": evidence_level,
        "summary": {
            "dashboards": len(results),
            "total_panels": total_panels,
            "semantic_gates": {"green": green, "yellow": yellow, "red": red},
            "readiness": {
                "ready": ready,
                "needs_metrics_mapping": needs_mapping,
                "needs_log_fielding": needs_fielding,
                "manual_only": manual_only,
            },
            "source_validation": {
                "passed": source_passed,
                "failed": source_failed,
                "not_configured": source_not_configured,
            },
            "target_validation": validation_summary.get("counts", {}),
            "schema_contract_totals": totals,
            "target_contract_totals": target_contract_summary.get("totals", {}),
        },
        "source_metric_inventory": {
            "status": source_inventory.get("status", "not_configured"),
            "available_metrics_count": len(source_inventory.get("available_metrics", [])),
            "available_labels_count": len(source_inventory.get("available_labels", [])),
            "metrics_found": len(source_inventory.get("metrics_found", [])),
            "metrics_missing": source_inventory.get("metrics_missing", []),
            "labels_found": len(source_inventory.get("labels_found", [])),
            "labels_missing": source_inventory.get("labels_missing", []),
        },
        "target_readiness": target_readiness,
        "datasource_audit": datasource_audit,
        "complexity_scores": complexity_scores,
        "schema_contract": schema_contract,
        "target_contract_summary": target_contract_summary,
        "blockers": blockers,
        "actions": actions,
        "customer_action_summary": _build_action_summary(
            results,
            blockers,
            actions,
            evidence_level,
            schema_contract,
            target_contract_summary=target_contract_summary,
            source_inventory=source_inventory,
            target_readiness=target_readiness,
            datasource_audit=datasource_audit,
        ),
    }


def _build_action_summary(
    results: list[Any],
    blockers: list[str],
    actions: list[str],
    evidence_level: str,
    schema_contract: dict[str, Any],
    target_contract_summary: dict[str, Any] | None = None,
    *,
    source_inventory: dict[str, Any] | None = None,
    target_readiness: dict[str, Any] | None = None,
    datasource_audit: dict[str, Any] | None = None,
) -> str:
    source_inventory = source_inventory or {}
    target_readiness = target_readiness or {}
    datasource_audit = datasource_audit or {}
    target_contract_summary = target_contract_summary or {}

    lines = ["PREFLIGHT VALIDATION SUMMARY", "=" * 40, ""]

    total = sum(r.total_panels for r in results)
    green = sum(
        1 for r in results for pr in r.panel_results
        if (pr.verification_packet or {}).get("semantic_gate") == "Green"
    )
    lines.append(f"Dashboards: {len(results)}")
    if evidence_level == "full":
        readiness_text = f"{green} ready for deployment"
    elif evidence_level == "static_analysis":
        readiness_text = f"{green} Green by static analysis"
    else:
        readiness_text = f"{green} Green with {evidence_level} evidence"
    lines.append(f"Panels: {total} ({readiness_text})")
    lines.append(f"Evidence level: {evidence_level}")
    lines.append("")

    cluster_health = target_readiness.get("cluster_health", {})
    if cluster_health:
        if cluster_health.get("unsupported"):
            lines.append(
                f"Target cluster: {cluster_health.get('status', '?').upper()} "
                "(cluster health API not available)"
            )
        else:
            lines.append(
                f"Target cluster: {cluster_health.get('status', '?').upper()} "
                f"({cluster_health.get('number_of_data_nodes', '?')} data nodes, "
                f"{cluster_health.get('active_shards', '?')} active shards)"
            )
        lines.append("")

    inv_status = source_inventory.get("status", "not_configured")
    if inv_status == "ok":
        avail_metrics = len(source_inventory.get("available_metrics", []))
        avail_labels = len(source_inventory.get("available_labels", []))
        found = len(source_inventory.get("metrics_found", []))
        missing = len(source_inventory.get("metrics_missing", []))
        lines.append(
            f"Source inventory: {avail_metrics} metrics, {avail_labels} labels in Prometheus"
        )
        lines.append(
            f"  Referenced metrics: {found} found, {missing} missing"
        )
        lines.append("")

    ds_types = datasource_audit.get("datasource_types", {})
    if ds_types:
        parts = [f"{t}: {c}" for t, c in ds_types.items()]
        lines.append(f"Datasource distribution: {', '.join(parts)}")
        non_mig = datasource_audit.get("non_migratable_panels", 0)
        if non_mig:
            lines.append(f"  Non-migratable panels: {non_mig}")
        lines.append("")

    if blockers:
        lines.append("BLOCKERS:")
        for b in blockers:
            lines.append(f"  - {b}")
        lines.append("")

    if actions:
        lines.append("ACTION ITEMS:")
        for a in actions:
            lines.append(f"  - {a}")
        lines.append("")

    required_indexes = list(schema_contract.get("required_indexes", {}).keys())
    if required_indexes:
        lines.append(
            f"REQUIRED TARGET INDEXES: {', '.join(required_indexes[:15])}"
        )
        lines.append("")

    contract_totals = target_contract_summary.get("totals", {})
    action_kinds = target_contract_summary.get("action_kinds", {})
    nonzero_statuses = [
        (status, count)
        for status, count in sorted(contract_totals.items())
        if count
    ]
    risky_contract_statuses = {
        "blocked",
        "degraded_if_forced",
        "exact_after_fulfillment",
    }
    has_risky_contract_totals = any(
        contract_totals.get(status, 0) for status in risky_contract_statuses
    )
    if nonzero_statuses or action_kinds:
        for status, count in nonzero_statuses:
            lines.append(f"CONTRACT STATUS {status}: {count}")
        for kind, count in sorted(action_kinds.items()):
            lines.append(f"  - {kind}: {count}")
        lines.append("")

    if not blockers and not actions and not has_risky_contract_totals:
        lines.append(
            "All preflight checks passed. "
            "Ready for target ingest and deployment testing."
        )

    return "\n".join(lines)


def save_preflight_report(
    report: dict[str, Any], output_path: str | Path,
) -> None:
    output_path = Path(output_path)
    with output_path.open("w") as fh:
        json.dump(report, fh, indent=2)


def save_preflight_json(
    contract: dict[str, Any], output_path: str | Path,
) -> None:
    output_path = Path(output_path)
    with output_path.open("w") as fh:
        json.dump(contract, fh, indent=2)


__all__ = [
    "build_dashboard_complexity",
    "build_datasource_audit",
    "build_preflight_report",
    "build_target_contract_summary",
    "build_target_schema_contract",
    "probe_source_metric_inventory",
    "probe_target_readiness",
    "save_preflight_json",
    "save_preflight_report",
]
