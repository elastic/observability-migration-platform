#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Verify emitted alert-rule payloads upload to Kibana disabled by default."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path = [path for path in sys.path if path != str(ROOT)]
sys.path.insert(0, str(ROOT))

from observability_migration.targets.kibana.alerting import (  # noqa: E402, I001
    cleanup_rules,
    collect_emitted_rule_payloads,
    create_rule,
    list_rules,
    run_alerting_preflight,
)


DEFAULT_COMPARISON_PATHS = [
    ROOT / "examples/alerting/generated/grafana/alert_comparison_results.json",
    ROOT / "examples/alerting/generated/datadog/monitor_comparison_results.json",
]
DEFAULT_NAME_PREFIX = "[verification "


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_emitted_payloads(paths: list[Path]) -> list[dict[str, Any]]:
    reports = [_load_json(path) for path in paths]
    return collect_emitted_rule_payloads(*reports)


def _list_all_rules(
    kibana_url: str,
    *,
    api_key: str = "",
    space_id: str = "",
    per_page: int = 100,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    all_rules: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = list_rules(
            kibana_url,
            api_key=api_key,
            space_id=space_id,
            per_page=per_page,
            page=page,
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


def _matching_rule_ids(rules: list[dict[str, Any]], marker: str, name_prefix: str) -> list[str]:
    matching: list[str] = []
    for rule in rules:
        rule_id = str(rule.get("id", "") or "")
        if not rule_id:
            continue
        tags = rule.get("tags", [])
        name = str(rule.get("name", "") or "")
        if marker in tags or name.startswith(name_prefix):
            matching.append(rule_id)
    return matching


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--comparison",
        dest="comparison_paths",
        action="append",
        default=[],
        help="Comparison JSON path to read. May be provided multiple times.",
    )
    parser.add_argument(
        "--kibana-url",
        default=os.getenv("KIBANA_ENDPOINT", os.getenv("KIBANA_URL", "")),
        help="Kibana URL (defaults to KIBANA_ENDPOINT or KIBANA_URL).",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("KIBANA_API_KEY", os.getenv("KEY", "")),
        help="Kibana API key (defaults to KIBANA_API_KEY or KEY).",
    )
    parser.add_argument("--space-id", default="", help="Optional Kibana space ID.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of emitted payloads to verify.")
    parser.add_argument("--keep-rules", action="store_true", help="Keep verification rules instead of deleting them.")
    parser.add_argument(
        "--name-prefix",
        default=DEFAULT_NAME_PREFIX,
        help="Prefix for temporary verification rule names.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    comparison_paths = [Path(path) for path in args.comparison_paths] if args.comparison_paths else DEFAULT_COMPARISON_PATHS

    if not args.kibana_url:
        print("ERROR: Kibana URL is required (--kibana-url or KIBANA_ENDPOINT/KIBANA_URL).", file=sys.stderr)
        return 2
    if not args.api_key:
        print("ERROR: Kibana API key is required (--api-key or KIBANA_API_KEY/KEY).", file=sys.stderr)
        return 2

    missing = [str(path) for path in comparison_paths if not path.exists()]
    if missing:
        print(json.dumps({"error": "missing_comparison_files", "paths": missing}, indent=2))
        return 2

    payloads = _load_emitted_payloads(comparison_paths)
    if args.limit > 0:
        payloads = payloads[: args.limit]
    if not payloads:
        print(json.dumps({"error": "no_emitted_rule_payloads"}, indent=2))
        return 2

    preflight = run_alerting_preflight(args.kibana_url, api_key=args.api_key, space_id=args.space_id)
    preflight_health = preflight.get("health", {}) if isinstance(preflight, dict) else {}
    preflight_unreachable = (
        isinstance(preflight_health, dict)
        and bool(preflight_health.get("error"))
        and int(preflight.get("rule_types_count", 0) or 0) == 0
        and int(preflight.get("connector_types_count", 0) or 0) == 0
    )
    if preflight_unreachable:
        summary = {
            "comparison_paths": [
                str(path.relative_to(ROOT)) if path.is_absolute() and path.is_relative_to(ROOT) else str(path)
                for path in comparison_paths
            ],
            "candidate_payloads": len(payloads),
            "created_rules": 0,
            "creation_errors": [],
            "enabled_true_in_create_response": [],
            "enabled_true_in_rule_listing": [],
            "preflight": {
                "rule_types_count": preflight.get("rule_types_count"),
                "connector_types_count": preflight.get("connector_types_count"),
                "can_create_es_query_rules": preflight.get("can_create_es_query_rules"),
                "can_create_index_threshold_rules": preflight.get("can_create_index_threshold_rules"),
                "can_create_custom_threshold_rules": preflight.get("can_create_custom_threshold_rules"),
                "health_error": preflight_health.get("error", ""),
            },
            "marker": "",
            "keep_rules": bool(args.keep_rules),
            "cleanup": {"deleted_count": 0, "failed_rule_ids": []},
            "error": "preflight_unreachable",
        }
        print(json.dumps(summary, indent=2))
        return 2

    marker = f"obs-migration-live-verify-{int(time.time())}"
    created: list[dict[str, Any]] = []
    creation_errors: list[dict[str, Any]] = []
    enabled_true_in_create_response: list[dict[str, Any]] = []
    enabled_true_in_rule_listing: list[dict[str, Any]] = []

    try:
        for idx, item in enumerate(payloads, start=1):
            payload = item["payload"]
            result = create_rule(
                args.kibana_url,
                rule_type_id=str(payload.get("rule_type_id", "") or ""),
                name=f"{args.name_prefix}{idx}] {payload.get('name', item['name'])}",
                consumer=str(payload.get("consumer", "stackAlerts") or "stackAlerts"),
                schedule_interval=str((payload.get("schedule") or {}).get("interval", "1m") or "1m"),
                params=payload.get("params") or {},
                actions=payload.get("actions") or [],
                enabled=bool(payload.get("enabled", False)),
                tags=[*(payload.get("tags") or []), marker],
                api_key=args.api_key,
                space_id=args.space_id,
            )
            if result.get("error"):
                creation_errors.append(
                    {
                        "alert_id": item["alert_id"],
                        "name": item["name"],
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

        listed_rules = _list_all_rules(
            args.kibana_url,
            api_key=args.api_key,
            space_id=args.space_id,
        )
        listed_by_id = {str(rule.get("id", "") or ""): rule for rule in listed_rules}
        for item in created:
            listed = listed_by_id.get(item["id"])
            if listed and listed.get("enabled"):
                enabled_true_in_rule_listing.append({"id": item["id"], "name": item["name"]})
    finally:
        cleanup_result: dict[str, Any]
        if args.keep_rules:
            cleanup_result = {"deleted_count": 0, "failed_rule_ids": []}
        else:
            cleanup_result = cleanup_rules(
                args.kibana_url,
                [item["id"] for item in created if item["id"]],
                api_key=args.api_key,
                space_id=args.space_id,
            )
            remaining = _matching_rule_ids(
                _list_all_rules(args.kibana_url, api_key=args.api_key, space_id=args.space_id),
                marker,
                args.name_prefix,
            )
            if remaining:
                sweep = cleanup_rules(
                    args.kibana_url,
                    remaining,
                    api_key=args.api_key,
                    space_id=args.space_id,
                )
                cleanup_result = {
                    "deleted_count": cleanup_result["deleted_count"] + sweep["deleted_count"],
                    "failed_rule_ids": [*cleanup_result["failed_rule_ids"], *sweep["failed_rule_ids"]],
                }

    summary = {
        "comparison_paths": [str(path.relative_to(ROOT)) if path.is_absolute() and path.is_relative_to(ROOT) else str(path) for path in comparison_paths],
        "candidate_payloads": len(payloads),
        "created_rules": len(created),
        "creation_errors": creation_errors,
        "enabled_true_in_create_response": enabled_true_in_create_response,
        "enabled_true_in_rule_listing": enabled_true_in_rule_listing,
        "preflight": {
            "rule_types_count": preflight.get("rule_types_count"),
            "connector_types_count": preflight.get("connector_types_count"),
            "can_create_es_query_rules": preflight.get("can_create_es_query_rules"),
            "can_create_index_threshold_rules": preflight.get("can_create_index_threshold_rules"),
            "can_create_custom_threshold_rules": preflight.get("can_create_custom_threshold_rules"),
        },
        "marker": marker,
        "keep_rules": bool(args.keep_rules),
        "cleanup": cleanup_result,
    }
    print(json.dumps(summary, indent=2))

    if creation_errors or enabled_true_in_create_response or enabled_true_in_rule_listing or cleanup_result["failed_rule_ids"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
