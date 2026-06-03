# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Kibana Serverless alerting and connector API client.

Provides rule-type discovery, connector management, rule lifecycle,
and capability preflight for the alert migration pipeline.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from observability_migration.core.http import apply_tls
from observability_migration.targets.kibana.compile import kibana_url_for_space

logger = logging.getLogger(__name__)


def _api_base(kibana_url: str, space_id: str = "") -> str:
    base = kibana_url_for_space(kibana_url, space_id).rstrip("/")
    if space_id and not base.endswith(f"/s/{space_id}"):
        base = f"{base}/s/{space_id}"
    return base


def _session(api_key: str = "", verify: bool | str = True) -> requests.Session:
    session = requests.Session()
    apply_tls(session, verify)
    session.headers.update({"kbn-xsrf": "true"})
    if api_key:
        session.headers["Authorization"] = f"ApiKey {api_key}"
    return session


# ---------------------------------------------------------------------------
# Discovery / preflight
# ---------------------------------------------------------------------------

def get_alerting_health(
    kibana_url: str, *, api_key: str = "", space_id: str = "", timeout: int = 15,
    verify: bool | str = True,
) -> dict[str, Any]:
    """GET /api/alerting/_health — alerting subsystem health."""
    session = _session(api_key, verify=verify)
    base = _api_base(kibana_url, space_id)
    try:
        resp = session.get(f"{base}/api/alerting/_health", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("alerting health check failed: %s", exc)
        return {"error": str(exc)}


def list_rule_types(
    kibana_url: str, *, api_key: str = "", space_id: str = "", timeout: int = 15,
    verify: bool | str = True,
) -> list[dict[str, Any]]:
    """GET /api/alerting/rule_types — discover available rule families."""
    session = _session(api_key, verify=verify)
    base = _api_base(kibana_url, space_id)
    try:
        resp = session.get(f"{base}/api/alerting/rule_types", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("failed to list rule types: %s", exc)
        return []


def list_connector_types(
    kibana_url: str, *, api_key: str = "", space_id: str = "", timeout: int = 15,
    verify: bool | str = True,
) -> list[dict[str, Any]]:
    """GET /api/actions/connector_types — discover available connector families."""
    session = _session(api_key, verify=verify)
    base = _api_base(kibana_url, space_id)
    try:
        resp = session.get(f"{base}/api/actions/connector_types", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("failed to list connector types: %s", exc)
        return []


def list_connectors(
    kibana_url: str, *, api_key: str = "", space_id: str = "", timeout: int = 15,
    verify: bool | str = True,
) -> list[dict[str, Any]]:
    """GET /api/actions/connectors — list all existing connectors."""
    session = _session(api_key, verify=verify)
    base = _api_base(kibana_url, space_id)
    try:
        resp = session.get(f"{base}/api/actions/connectors", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("failed to list connectors: %s", exc)
        return []


def run_alerting_preflight(
    kibana_url: str, *, api_key: str = "", space_id: str = "", timeout: int = 15,
    verify: bool | str = True,
) -> dict[str, Any]:
    """Run a full alerting capability preflight against the target Kibana.

    Returns a structured report including:
    - health status
    - available rule type IDs
    - available connector type IDs
    - existing connector count
    - whether key rule families are present
    """
    health = get_alerting_health(kibana_url, api_key=api_key, space_id=space_id, timeout=timeout, verify=verify)
    rule_types = list_rule_types(kibana_url, api_key=api_key, space_id=space_id, timeout=timeout, verify=verify)
    connector_types = list_connector_types(kibana_url, api_key=api_key, space_id=space_id, timeout=timeout, verify=verify)
    connectors = list_connectors(kibana_url, api_key=api_key, space_id=space_id, timeout=timeout, verify=verify)

    rule_type_ids = {rt.get("id", "") for rt in rule_types}
    enabled_connector_types = {
        ct.get("id", "") for ct in connector_types
        if ct.get("enabled") and ct.get("enabled_in_config")
    }

    KEY_RULE_FAMILIES = {
        "es-query": ".es-query",
        "index-threshold": ".index-threshold",
        "custom-threshold": "observability.rules.custom_threshold",
        "metric-threshold": "metrics.alert.threshold",
        "log-threshold": "logs.alert.document.count",
    }

    rule_family_availability = {}
    for family_name, rule_type_id in KEY_RULE_FAMILIES.items():
        rule_family_availability[family_name] = rule_type_id in rule_type_ids

    return {
        "health": health,
        "rule_types_count": len(rule_types),
        "rule_type_ids": sorted(rule_type_ids),
        "connector_types_count": len(connector_types),
        "enabled_connector_type_ids": sorted(enabled_connector_types),
        "existing_connectors": len(connectors),
        "existing_connector_ids": [c.get("id", "") for c in connectors],
        "rule_family_availability": rule_family_availability,
        "can_create_es_query_rules": rule_family_availability.get("es-query", False),
        "can_create_index_threshold_rules": rule_family_availability.get("index-threshold", False),
        "can_create_custom_threshold_rules": rule_family_availability.get("custom-threshold", False),
    }


# ---------------------------------------------------------------------------
# Connector lifecycle
# ---------------------------------------------------------------------------

def create_connector(
    kibana_url: str,
    *,
    connector_type_id: str,
    name: str,
    config: dict[str, Any] | None = None,
    secrets: dict[str, Any] | None = None,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 15,
    verify: bool | str = True,
) -> dict[str, Any]:
    """POST /api/actions/connector — create a new connector."""
    session = _session(api_key, verify=verify)
    base = _api_base(kibana_url, space_id)
    body: dict[str, Any] = {
        "connector_type_id": connector_type_id,
        "name": name,
    }
    if config:
        body["config"] = config
    if secrets:
        body["secrets"] = secrets
    try:
        resp = session.post(f"{base}/api/actions/connector", json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("failed to create connector '%s': %s", name, exc)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Rule lifecycle
# ---------------------------------------------------------------------------

def create_rule(
    kibana_url: str,
    *,
    rule_type_id: str,
    name: str,
    consumer: str = "alerts",
    schedule_interval: str = "1m",
    params: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    enabled: bool = False,
    tags: list[str] | None = None,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 15,
    verify: bool | str = True,
) -> dict[str, Any]:
    """POST /api/alerting/rule — create a new alerting rule.

    Rules are created disabled by default for safety.
    """
    session = _session(api_key, verify=verify)
    base = _api_base(kibana_url, space_id)
    body: dict[str, Any] = {
        "rule_type_id": rule_type_id,
        "name": name,
        "consumer": consumer,
        "schedule": {"interval": schedule_interval},
        "params": params or {},
        "actions": actions or [],
        "enabled": enabled,
        "tags": tags or ["obs-migration"],
    }
    try:
        resp = session.post(f"{base}/api/alerting/rule", json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("failed to create rule '%s': %s", name, exc)
        return {"error": str(exc)}


def list_rules(
    kibana_url: str, *, api_key: str = "", space_id: str = "", timeout: int = 15,
    per_page: int = 100, page: int = 1,
    verify: bool | str = True,
) -> dict[str, Any]:
    """GET /api/alerting/rules/_find — list existing rules."""
    session = _session(api_key, verify=verify)
    base = _api_base(kibana_url, space_id)
    try:
        resp = session.get(
            f"{base}/api/alerting/rules/_find",
            params={"per_page": per_page, "page": page},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("failed to list rules: %s", exc)
        return {"error": str(exc), "data": [], "total": 0}


def delete_rule(
    kibana_url: str,
    rule_id: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 15,
    verify: bool | str = True,
) -> bool:
    """DELETE /api/alerting/rule/{id}."""
    session = _session(api_key, verify=verify)
    base = _api_base(kibana_url, space_id)
    try:
        resp = session.delete(f"{base}/api/alerting/rule/{rule_id}", timeout=timeout)
        return resp.status_code == 204
    except Exception as exc:
        logger.warning("failed to delete rule %s: %s", rule_id, exc)
        return False


def enable_rule(
    kibana_url: str,
    rule_id: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 15,
    verify: bool | str = True,
) -> bool:
    """POST /api/alerting/rule/{id}/_enable."""
    session = _session(api_key, verify=verify)
    base = _api_base(kibana_url, space_id)
    try:
        resp = session.post(f"{base}/api/alerting/rule/{rule_id}/_enable", timeout=timeout)
        return resp.status_code in {200, 204}
    except Exception as exc:
        logger.warning("failed to enable rule %s: %s", rule_id, exc)
        return False


def disable_rule(
    kibana_url: str,
    rule_id: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 15,
    verify: bool | str = True,
) -> bool:
    """POST /api/alerting/rule/{id}/_disable."""
    session = _session(api_key, verify=verify)
    base = _api_base(kibana_url, space_id)
    try:
        resp = session.post(f"{base}/api/alerting/rule/{rule_id}/_disable", timeout=timeout)
        return resp.status_code in {200, 204}
    except Exception as exc:
        logger.warning("failed to disable rule %s: %s", rule_id, exc)
        return False


def collect_emitted_rule_payloads(*comparison_reports: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect emitted Kibana rule payloads from comparison report documents."""
    collected: list[dict[str, Any]] = []
    for report in comparison_reports:
        if not isinstance(report, dict):
            continue
        for source_type in ("alerts", "monitors"):
            rows = report.get(source_type)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                target = row.get("target")
                if not isinstance(target, dict) or not target.get("payload_emitted"):
                    continue
                payload = target.get("rule_payload")
                if not isinstance(payload, dict) or not payload:
                    continue
                collected.append(
                    {
                        "source_type": source_type,
                        "alert_id": str(row.get("alert_id", "") or ""),
                        "name": str(row.get("name", "") or payload.get("name", "") or "unnamed"),
                        "kind": str(row.get("kind", "") or ""),
                        "payload": payload,
                    }
                )
    return collected


DEFAULT_MIGRATED_RULE_TAG = "obs-migration"
DEFAULT_MIGRATED_RULE_NAME_PREFIX = "[migrated] "


def _normalize_rule_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize rule items from either collect_emitted_rule_payloads or
    map_alerts_batch into a common internal shape: {alert_id, name, kind,
    payload, automation_tier, payload_emitted}.
    """
    normalized: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if "payload" in item and isinstance(item["payload"], dict):
            payload = item["payload"]
            normalized.append(
                {
                    "alert_id": str(item.get("alert_id", "") or ""),
                    "name": str(item.get("name", "") or payload.get("name", "") or "unnamed"),
                    "kind": str(item.get("kind", "") or ""),
                    "payload": payload,
                    "automation_tier": str(item.get("automation_tier", "") or ""),
                    "payload_emitted": True,
                }
            )
            continue
        mapping = item.get("mapping")
        if isinstance(mapping, dict):
            payload = mapping.get("rule_payload") or {}
            if not isinstance(payload, dict) or not payload:
                continue
            if not mapping.get("payload_emitted"):
                continue
            normalized.append(
                {
                    "alert_id": str(item.get("alert_id", "") or ""),
                    "name": str(item.get("name", "") or payload.get("name", "") or "unnamed"),
                    "kind": str(item.get("kind", "") or ""),
                    "payload": payload,
                    "automation_tier": str(mapping.get("automation_tier", "") or ""),
                    "payload_emitted": True,
                }
            )
    return normalized


def _preflight_unreachable(preflight: dict[str, Any] | None) -> bool:
    if not isinstance(preflight, dict):
        return False
    health = preflight.get("health", {})
    if not isinstance(health, dict):
        return False
    has_health_error = bool(health.get("error"))
    rule_types_count = int(preflight.get("rule_types_count", 0) or 0)
    connector_types_count = int(preflight.get("connector_types_count", 0) or 0)
    return has_health_error and rule_types_count == 0 and connector_types_count == 0


def create_rules_from_payloads(
    kibana_url: str,
    rule_items: list[dict[str, Any]],
    *,
    api_key: str = "",
    space_id: str = "",
    preflight: dict[str, Any] | None = None,
    marker_tag: str = DEFAULT_MIGRATED_RULE_TAG,
    name_prefix: str = DEFAULT_MIGRATED_RULE_NAME_PREFIX,
    enabled: bool = False,
    only_automated: bool = True,
    timeout: int = 15,
    verify: bool | str = True,
    create_rule_fn: Any | None = None,
) -> dict[str, Any]:
    """Create Kibana alerting rules from a batch of emitted rule payloads.

    Accepts the shape returned by either `collect_emitted_rule_payloads` or
    `map_alerts_batch()["results"]`. Rules are created disabled by default.
    Every created rule is tagged with `marker_tag` so it can be audited and
    cleaned up later via `audit_migrated_rules` or `cleanup_rules`.

    Parameters
    ----------
    only_automated:
        When True (default), skip rule items whose `automation_tier` is not
        `automated`. Items without a tier are still attempted so that callers
        passing raw emitted-payload lists (which do not carry the tier) work.
    enabled:
        Forwarded to `create_rule`. Keep False by default for safety.

    Returns
    -------
    dict
        Summary document suitable for serialization, with `created`, `failed`,
        `skipped`, per-item details, and the preflight snapshot used.
    """
    creator = create_rule_fn or create_rule
    preflight_unreachable = _preflight_unreachable(preflight)
    items = _normalize_rule_items(rule_items)

    preflight_snapshot = {
        "rule_types_count": (preflight or {}).get("rule_types_count"),
        "connector_types_count": (preflight or {}).get("connector_types_count"),
        "can_create_es_query_rules": (preflight or {}).get("can_create_es_query_rules"),
        "can_create_index_threshold_rules": (preflight or {}).get("can_create_index_threshold_rules"),
        "can_create_custom_threshold_rules": (preflight or {}).get("can_create_custom_threshold_rules"),
        "health_error": ((preflight or {}).get("health") or {}).get("error", "") if isinstance(preflight, dict) else "",
    }

    summary: dict[str, Any] = {
        "candidate_payloads": len(items),
        "created": [],
        "failed": [],
        "skipped": [],
        "marker_tag": marker_tag,
        "name_prefix": name_prefix,
        "enabled": bool(enabled),
        "preflight": preflight_snapshot,
        "preflight_unreachable": preflight_unreachable,
        "summary": {"created": 0, "failed": 0, "skipped": 0},
    }

    if preflight_unreachable:
        for item in items:
            summary["skipped"].append(
                {
                    "alert_id": item["alert_id"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "reason": "preflight_unreachable",
                }
            )
        summary["summary"] = {
            "created": 0,
            "failed": 0,
            "skipped": len(items),
        }
        return summary

    for item in items:
        tier = item.get("automation_tier", "")
        if only_automated and tier and tier != "automated":
            summary["skipped"].append(
                {
                    "alert_id": item["alert_id"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "reason": f"automation_tier_not_automated:{tier}",
                }
            )
            continue

        payload = item["payload"]
        rule_type_id = str(payload.get("rule_type_id", "") or "")
        if not rule_type_id:
            summary["skipped"].append(
                {
                    "alert_id": item["alert_id"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "reason": "missing_rule_type_id",
                }
            )
            continue

        rule_name_source = str(payload.get("name") or item["name"] or "")
        if name_prefix and not rule_name_source.startswith(name_prefix):
            rule_name = f"{name_prefix}{rule_name_source}"
        else:
            rule_name = rule_name_source
        existing_tags = [str(t) for t in (payload.get("tags") or []) if str(t)]
        tags = list(existing_tags)
        if marker_tag and marker_tag not in tags:
            tags.append(marker_tag)

        response = creator(
            kibana_url,
            rule_type_id=rule_type_id,
            name=rule_name,
            consumer=str(payload.get("consumer", "stackAlerts") or "stackAlerts"),
            schedule_interval=str((payload.get("schedule") or {}).get("interval", "1m") or "1m"),
            params=payload.get("params") or {},
            actions=payload.get("actions") or [],
            enabled=bool(enabled),
            tags=tags,
            api_key=api_key,
            space_id=space_id,
            timeout=timeout,
            verify=verify,
        )

        if not isinstance(response, dict) or response.get("error"):
            summary["failed"].append(
                {
                    "alert_id": item["alert_id"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "rule_type_id": rule_type_id,
                    "error": str((response or {}).get("error", "unknown")),
                }
            )
            continue

        summary["created"].append(
            {
                "id": str(response.get("id", "") or ""),
                "alert_id": item["alert_id"],
                "name": str(response.get("name", "") or rule_name),
                "rule_type_id": rule_type_id,
                "enabled": bool(response.get("enabled", False)),
                "kind": item["kind"],
            }
        )

    summary["summary"] = {
        "created": len(summary["created"]),
        "failed": len(summary["failed"]),
        "skipped": len(summary["skipped"]),
    }
    return summary


def cleanup_rules(
    kibana_url: str,
    rule_ids: list[str],
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 15,
    verify: bool | str = True,
    delete_rule_fn: Any | None = None,
) -> dict[str, Any]:
    """Delete a batch of rules and summarize boolean delete results."""
    deleter = delete_rule_fn or delete_rule
    deleted_count = 0
    failed_rule_ids: list[str] = []
    for rule_id in rule_ids:
        ok = bool(
            deleter(
                kibana_url,
                rule_id,
                api_key=api_key,
                space_id=space_id,
                timeout=timeout,
                verify=verify,
            )
        )
        if ok:
            deleted_count += 1
        else:
            failed_rule_ids.append(rule_id)
    return {
        "deleted_count": deleted_count,
        "failed_rule_ids": failed_rule_ids,
    }


DEFAULT_VERIFY_NAME_PREFIX = "[verification "


def _list_all_rules(
    kibana_url: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 15,
    per_page: int = 100,
    max_pages: int = 20,
    verify: bool | str = True,
    list_rules_fn: Any | None = None,
) -> list[dict[str, Any]]:
    """Page through every rule in the space and return them flattened."""
    lister = list_rules_fn or list_rules
    all_rules: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = lister(
            kibana_url,
            api_key=api_key,
            space_id=space_id,
            timeout=timeout,
            per_page=per_page,
            page=page,
            verify=verify,
        )
        if not isinstance(payload, dict):
            break
        page_rules = payload.get("data", [])
        if not isinstance(page_rules, list) or not page_rules:
            break
        all_rules.extend(rule for rule in page_rules if isinstance(rule, dict))
        total = int(payload.get("total", len(all_rules)) or len(all_rules))
        if len(all_rules) >= total:
            break
    return all_rules


def _matching_verification_rule_ids(
    rules: list[dict[str, Any]], marker: str, name_prefix: str
) -> list[str]:
    """Return ids of rules carrying the verification marker tag or name prefix."""
    matching: list[str] = []
    for rule in rules:
        rule_id = str(rule.get("id", "") or "")
        if not rule_id:
            continue
        tags = rule.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        name = str(rule.get("name", "") or "")
        if marker in tags or name.startswith(name_prefix):
            matching.append(rule_id)
    return matching


def verify_emitted_rule_uploads(
    kibana_url: str,
    payloads: list[dict[str, Any]],
    *,
    api_key: str = "",
    space_id: str = "",
    keep_rules: bool = False,
    name_prefix: str = DEFAULT_VERIFY_NAME_PREFIX,
    timeout: int = 15,
    verify: bool | str = True,
    preflight: dict[str, Any] | None = None,
    marker: str = "",
    create_rule_fn: Any | None = None,
    list_rules_fn: Any | None = None,
    delete_rule_fn: Any | None = None,
    run_preflight_fn: Any | None = None,
) -> dict[str, Any]:
    """Create emitted alert-rule payloads in Kibana, confirm they land disabled, then clean up.

    This is the packaged form of the alert-rule upload round trip: it creates
    each payload (tagged with a unique ``marker`` so it can be swept up), checks
    that none came back enabled in either the create response or a fresh
    listing, and — unless ``keep_rules`` is set — deletes everything it created
    (plus any stragglers carrying the marker/name prefix).

    Parameters
    ----------
    payloads:
        The shape returned by :func:`collect_emitted_rule_payloads` (each item
        has ``payload``, ``alert_id``, ``name``).
    preflight:
        Optional pre-computed preflight snapshot. When omitted, this calls
        :func:`run_alerting_preflight`. If the cluster is unreachable, no rules
        are created and the summary carries ``error == "preflight_unreachable"``.

    Returns
    -------
    dict
        A JSON-serializable summary with ``candidate_payloads``,
        ``created_rules``, ``creation_errors``,
        ``enabled_true_in_create_response``, ``enabled_true_in_rule_listing``,
        ``preflight``, ``marker``, ``keep_rules`` and ``cleanup``.
    """
    creator = create_rule_fn or create_rule
    preflight_runner = run_preflight_fn or run_alerting_preflight

    if preflight is None:
        preflight = preflight_runner(
            kibana_url, api_key=api_key, space_id=space_id, verify=verify,
        )
    preflight_snapshot = {
        "rule_types_count": (preflight or {}).get("rule_types_count"),
        "connector_types_count": (preflight or {}).get("connector_types_count"),
        "can_create_es_query_rules": (preflight or {}).get("can_create_es_query_rules"),
        "can_create_index_threshold_rules": (preflight or {}).get("can_create_index_threshold_rules"),
        "can_create_custom_threshold_rules": (preflight or {}).get("can_create_custom_threshold_rules"),
    }

    if _preflight_unreachable(preflight):
        return {
            "candidate_payloads": len(payloads),
            "created_rules": 0,
            "creation_errors": [],
            "enabled_true_in_create_response": [],
            "enabled_true_in_rule_listing": [],
            "preflight": {
                **preflight_snapshot,
                "health_error": ((preflight or {}).get("health") or {}).get("error", ""),
            },
            "marker": "",
            "keep_rules": bool(keep_rules),
            "cleanup": {"deleted_count": 0, "failed_rule_ids": []},
            "error": "preflight_unreachable",
        }

    marker = marker or f"obs-migration-live-verify-{int(time.time())}"
    created: list[dict[str, Any]] = []
    creation_errors: list[dict[str, Any]] = []
    enabled_true_in_create_response: list[dict[str, Any]] = []
    enabled_true_in_rule_listing: list[dict[str, Any]] = []

    try:
        for idx, item in enumerate(payloads, start=1):
            payload = item["payload"]
            result = creator(
                kibana_url,
                rule_type_id=str(payload.get("rule_type_id", "") or ""),
                name=f"{name_prefix}{idx}] {payload.get('name', item.get('name', ''))}",
                consumer=str(payload.get("consumer", "stackAlerts") or "stackAlerts"),
                schedule_interval=str((payload.get("schedule") or {}).get("interval", "1m") or "1m"),
                params=payload.get("params") or {},
                actions=payload.get("actions") or [],
                enabled=bool(payload.get("enabled", False)),
                tags=[*(payload.get("tags") or []), marker],
                api_key=api_key,
                space_id=space_id,
                timeout=timeout,
                verify=verify,
            )
            if result.get("error"):
                creation_errors.append(
                    {
                        "alert_id": item.get("alert_id", ""),
                        "name": item.get("name", ""),
                        "rule_type_id": payload.get("rule_type_id", ""),
                        "error": result["error"],
                    }
                )
                continue
            created.append(
                {
                    "id": str(result.get("id", "") or ""),
                    "name": str(result.get("name", "") or ""),
                    "enabled": bool(result.get("enabled", False)),
                }
            )
            if result.get("enabled"):
                enabled_true_in_create_response.append({"id": result.get("id", ""), "name": result.get("name", "")})

        listed_by_id = {
            str(rule.get("id", "") or ""): rule
            for rule in _list_all_rules(
                kibana_url,
                api_key=api_key,
                space_id=space_id,
                timeout=timeout,
                verify=verify,
                list_rules_fn=list_rules_fn,
            )
        }
        for item in created:
            listed = listed_by_id.get(item["id"])
            if listed and listed.get("enabled"):
                enabled_true_in_rule_listing.append({"id": item["id"], "name": item["name"]})
    finally:
        if keep_rules:
            cleanup_result: dict[str, Any] = {"deleted_count": 0, "failed_rule_ids": []}
        else:
            cleanup_result = cleanup_rules(
                kibana_url,
                [item["id"] for item in created if item["id"]],
                api_key=api_key,
                space_id=space_id,
                timeout=timeout,
                verify=verify,
                delete_rule_fn=delete_rule_fn,
            )
            remaining = _matching_verification_rule_ids(
                _list_all_rules(
                    kibana_url,
                    api_key=api_key,
                    space_id=space_id,
                    timeout=timeout,
                    verify=verify,
                    list_rules_fn=list_rules_fn,
                ),
                marker,
                name_prefix,
            )
            if remaining:
                sweep = cleanup_rules(
                    kibana_url,
                    remaining,
                    api_key=api_key,
                    space_id=space_id,
                    timeout=timeout,
                    verify=verify,
                    delete_rule_fn=delete_rule_fn,
                )
                cleanup_result = {
                    "deleted_count": cleanup_result["deleted_count"] + sweep["deleted_count"],
                    "failed_rule_ids": [*cleanup_result["failed_rule_ids"], *sweep["failed_rule_ids"]],
                }

    return {
        "candidate_payloads": len(payloads),
        "created_rules": len(created),
        "creation_errors": creation_errors,
        "enabled_true_in_create_response": enabled_true_in_create_response,
        "enabled_true_in_rule_listing": enabled_true_in_rule_listing,
        "preflight": preflight_snapshot,
        "marker": marker,
        "keep_rules": bool(keep_rules),
        "cleanup": cleanup_result,
    }


def collect_migrated_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return rules created by the migration workflow."""
    migrated: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        tags = rule.get("tags")
        if not isinstance(tags, list):
            tags = []
        name = str(rule.get("name", "") or "")
        if "obs-migration" in tags or name.startswith("[migrated]"):
            migrated.append(rule)
    return migrated


def audit_migrated_rules(
    kibana_url: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 15,
    per_page: int = 100,
    max_pages: int = 20,
    disable_enabled: bool = False,
    verify: bool | str = True,
    list_rules_fn: Any | None = None,
    disable_rule_fn: Any | None = None,
) -> dict[str, Any]:
    """List migrated rules and optionally disable the enabled subset."""
    lister = list_rules_fn or list_rules
    disabler = disable_rule_fn or disable_rule

    all_rules: list[dict[str, Any]] = []
    errors: list[str] = []
    total_available: int | None = None
    pages_scanned = 0
    for page in range(1, max_pages + 1):
        payload = lister(
            kibana_url,
            api_key=api_key,
            space_id=space_id,
            timeout=timeout,
            per_page=per_page,
            page=page,
            verify=verify,
        )
        if not isinstance(payload, dict):
            break
        error = str(payload.get("error", "") or "").strip()
        if error:
            errors.append(error)
            break
        page_rules = payload.get("data", [])
        if not isinstance(page_rules, list) or not page_rules:
            break
        pages_scanned = page
        all_rules.extend(rule for rule in page_rules if isinstance(rule, dict))
        total = int(payload.get("total", len(all_rules)) or len(all_rules))
        total_available = total
        if len(all_rules) >= total:
            break

    migrated_rules = collect_migrated_rules(all_rules)
    enabled_migrated_rules = [rule for rule in migrated_rules if bool(rule.get("enabled"))]
    disabled_migrated_rules = [rule for rule in migrated_rules if not bool(rule.get("enabled"))]

    remediation = {
        "requested": bool(disable_enabled),
        "attempted_rule_ids": [],
        "disabled_rule_ids": [],
        "failed_rule_ids": [],
    }
    if disable_enabled:
        for rule in enabled_migrated_rules:
            rule_id = str(rule.get("id", "") or "")
            if not rule_id:
                continue
            remediation["attempted_rule_ids"].append(rule_id)
            ok = bool(
                disabler(
                    kibana_url,
                    rule_id,
                    api_key=api_key,
                    space_id=space_id,
                    timeout=timeout,
                    verify=verify,
                )
            )
            if ok:
                remediation["disabled_rule_ids"].append(rule_id)
            else:
                remediation["failed_rule_ids"].append(rule_id)

    listing_truncated = total_available is not None and len(all_rules) < total_available and not errors
    listing_warning = ""
    if listing_truncated:
        listing_warning = (
            f"Inspected {len(all_rules)} of {total_available} rules after {pages_scanned} page(s). "
            "Increase --max-pages to inspect every rule before acting on this result."
        )

    return {
        "total_rules_seen": len(all_rules),
        "total_rules_available": total_available if total_available is not None else len(all_rules),
        "pages_scanned": pages_scanned,
        "listing_truncated": listing_truncated,
        "listing_warning": listing_warning,
        "migrated_rules_seen": len(migrated_rules),
        "migrated_rule_ids": [str(rule.get("id", "") or "") for rule in migrated_rules],
        "enabled_migrated_rule_ids": [str(rule.get("id", "") or "") for rule in enabled_migrated_rules],
        "disabled_migrated_rule_ids": [str(rule.get("id", "") or "") for rule in disabled_migrated_rules],
        "enabled_migrated_rules": enabled_migrated_rules,
        "disabled_migrated_rules": disabled_migrated_rules,
        "errors": errors,
        "remediation": remediation,
    }


# ---------------------------------------------------------------------------
# Dry-run validation
# ---------------------------------------------------------------------------

def validate_rule_payload(
    rule_type_id: str,
    params: dict[str, Any],
    preflight: dict[str, Any],
) -> dict[str, Any]:
    """Validate a rule payload against the preflight capability report.

    Returns {"valid": bool, "errors": [...], "warnings": [...]}.
    Does NOT make any API calls — purely local structural validation.
    """
    errors: list[str] = []
    warnings: list[str] = []

    available = preflight.get("rule_family_availability", {})

    RULE_TYPE_MAP = {
        ".es-query": "es-query",
        ".index-threshold": "index-threshold",
        "observability.rules.custom_threshold": "custom-threshold",
    }
    family_key = RULE_TYPE_MAP.get(rule_type_id, "")
    if family_key and not available.get(family_key, False):
        errors.append(f"Rule type '{rule_type_id}' is not available in the target Kibana")

    if not rule_type_id:
        errors.append("rule_type_id is required")
    if not params:
        errors.append("params must not be empty")

    if (
        rule_type_id == ".es-query"
        and "esqlQuery" not in params
        and "searchType" not in params
        and "esQuery" not in params
    ):
        warnings.append("ES query rule params should contain esqlQuery, esQuery, or searchType")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


__all__ = [
    "audit_migrated_rules",
    "cleanup_rules",
    "collect_emitted_rule_payloads",
    "collect_migrated_rules",
    "create_connector",
    "create_rule",
    "delete_rule",
    "disable_rule",
    "enable_rule",
    "get_alerting_health",
    "list_connector_types",
    "list_connectors",
    "list_rule_types",
    "list_rules",
    "run_alerting_preflight",
    "validate_rule_payload",
    "verify_emitted_rule_uploads",
]
