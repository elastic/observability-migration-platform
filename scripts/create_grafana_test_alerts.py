# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Create realistic Grafana Unified Alerting rules for migration testing."""

from __future__ import annotations

import argparse
import os

import requests

GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:23000")
GRAFANA_USER = os.getenv("GRAFANA_USER", "admin")
GRAFANA_PASS = os.getenv("GRAFANA_PASS", "admin")
session = requests.Session()
session.auth = (GRAFANA_USER, GRAFANA_PASS)
session.headers.update({"Content-Type": "application/json"})

PROM_UID = "PBFA97CFB590B2093"
LOKI_UID = "P8E80F9AEF21F6940"
FOLDER_UID = "dfhraua26uneob"

ALERT_RULES = [
    {
        "title": "High CPU Usage",
        "ruleGroup": "infrastructure",
        "condition": "C",
        "for": "5m",
        "annotations": {
            "summary": "CPU usage is above 80% on {{ $labels.instance }}",
            "description": "Instance {{ $labels.instance }} has had CPU usage above 80% for more than 5 minutes.",
        },
        "labels": {"severity": "warning", "team": "infra"},
        "data": [
            {
                "refId": "A",
                "datasourceUid": PROM_UID,
                "model": {
                    "refId": "A",
                    "expr": '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
                    "instant": False,
                    "range": True,
                    "intervalMs": 1000,
                    "maxDataPoints": 43200,
                },
                "relativeTimeRange": {"from": 600, "to": 0},
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "B",
                    "type": "reduce",
                    "expression": "A",
                    "reducer": "last",
                    "conditions": [{"evaluator": {"type": "gt", "params": [0]}}],
                },
                "relativeTimeRange": {"from": 0, "to": 0},
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "expression": "B",
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": [80]},
                            "operator": {"type": "and"},
                            "reducer": {"type": "last"},
                        }
                    ],
                },
                "relativeTimeRange": {"from": 0, "to": 0},
            },
        ],
    },
    {
        "title": "Memory Usage Critical",
        "ruleGroup": "infrastructure",
        "condition": "C",
        "for": "10m",
        "annotations": {
            "summary": "Memory usage is above 90% on {{ $labels.instance }}",
        },
        "labels": {"severity": "critical", "team": "infra"},
        "data": [
            {
                "refId": "A",
                "datasourceUid": PROM_UID,
                "model": {
                    "refId": "A",
                    "expr": "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100",
                    "instant": False,
                    "range": True,
                },
                "relativeTimeRange": {"from": 600, "to": 0},
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "B",
                    "type": "reduce",
                    "expression": "A",
                    "reducer": "last",
                },
                "relativeTimeRange": {"from": 0, "to": 0},
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "expression": "B",
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": [90]},
                            "operator": {"type": "and"},
                            "reducer": {"type": "last"},
                        }
                    ],
                },
                "relativeTimeRange": {"from": 0, "to": 0},
            },
        ],
    },
    {
        "title": "Disk Space Low",
        "ruleGroup": "infrastructure",
        "condition": "C",
        "for": "15m",
        "annotations": {
            "summary": "Disk space below 20% on {{ $labels.mountpoint }}",
        },
        "labels": {"severity": "warning"},
        "data": [
            {
                "refId": "A",
                "datasourceUid": PROM_UID,
                "model": {
                    "refId": "A",
                    "expr": '(node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes) * 100',
                    "instant": False,
                    "range": True,
                },
                "relativeTimeRange": {"from": 600, "to": 0},
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "B",
                    "type": "reduce",
                    "expression": "A",
                    "reducer": "last",
                },
                "relativeTimeRange": {"from": 0, "to": 0},
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "expression": "B",
                    "conditions": [
                        {
                            "evaluator": {"type": "lt", "params": [20]},
                            "operator": {"type": "and"},
                            "reducer": {"type": "last"},
                        }
                    ],
                },
                "relativeTimeRange": {"from": 0, "to": 0},
            },
        ],
    },
    {
        "title": "Prometheus Target Down",
        "ruleGroup": "monitoring",
        "condition": "C",
        "for": "3m",
        "annotations": {
            "summary": "Target {{ $labels.job }}/{{ $labels.instance }} is down",
        },
        "labels": {"severity": "critical", "team": "platform"},
        "data": [
            {
                "refId": "A",
                "datasourceUid": PROM_UID,
                "model": {
                    "refId": "A",
                    "expr": "up == 0",
                    "instant": True,
                    "range": False,
                },
                "relativeTimeRange": {"from": 60, "to": 0},
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "B",
                    "type": "reduce",
                    "expression": "A",
                    "reducer": "last",
                },
                "relativeTimeRange": {"from": 0, "to": 0},
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "expression": "B",
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": [0]},
                            "operator": {"type": "and"},
                            "reducer": {"type": "last"},
                        }
                    ],
                },
                "relativeTimeRange": {"from": 0, "to": 0},
            },
        ],
    },
    {
        "title": "High Error Rate in Logs",
        "ruleGroup": "application",
        "condition": "C",
        "for": "5m",
        "annotations": {
            "summary": "Error rate in logs exceeds threshold",
        },
        "labels": {"severity": "warning", "team": "app"},
        "data": [
            {
                "refId": "A",
                "datasourceUid": LOKI_UID,
                "model": {
                    "refId": "A",
                    "expr": 'count_over_time({job=~".+"} |= "error" [5m])',
                    "instant": False,
                    "range": True,
                },
                "relativeTimeRange": {"from": 600, "to": 0},
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "B",
                    "type": "reduce",
                    "expression": "A",
                    "reducer": "last",
                },
                "relativeTimeRange": {"from": 0, "to": 0},
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "model": {
                    "refId": "C",
                    "type": "threshold",
                    "expression": "B",
                    "conditions": [
                        {
                            "evaluator": {"type": "gt", "params": [100]},
                            "operator": {"type": "and"},
                            "reducer": {"type": "last"},
                        }
                    ],
                },
                "relativeTimeRange": {"from": 0, "to": 0},
            },
        ],
    },
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--grafana-url",
        default=GRAFANA_URL,
        help="Grafana base URL",
    )
    parser.add_argument(
        "--user",
        default=GRAFANA_USER,
        help="Grafana basic-auth username",
    )
    parser.add_argument(
        "--password",
        default=GRAFANA_PASS,
        help="Grafana basic-auth password",
    )
    parser.add_argument(
        "--folder-uid",
        default=FOLDER_UID,
        help="Grafana folder UID that will receive test rules",
    )
    parser.add_argument(
        "--folder-title",
        default="Migration Test Alerts",
        help="Folder title used when the requested folder UID is missing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rules that would be created without making API calls",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds",
    )
    return parser.parse_args(argv)


def _ensure_folder_uid(
    session_obj: requests.Session,
    grafana_url: str,
    requested_uid: str,
    folder_title: str,
    timeout: float,
) -> str:
    response = session_obj.get(f"{grafana_url}/api/folders", timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    folders = payload if isinstance(payload, list) else []

    for folder in folders:
        if str(folder.get("uid", "") or "") == requested_uid:
            return requested_uid
    for folder in folders:
        if str(folder.get("title", "") or "").strip() == folder_title:
            return str(folder.get("uid", "") or "")

    create = session_obj.post(
        f"{grafana_url}/api/folders",
        json={"title": folder_title},
        timeout=timeout,
    )
    if create.status_code in (200, 201):
        payload = create.json() if create.content else {}
        created_uid = str(payload.get("uid", "") or "").strip()
        if created_uid:
            return created_uid
        response = session_obj.get(f"{grafana_url}/api/folders", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        folders = payload if isinstance(payload, list) else []
        for folder in folders:
            if str(folder.get("title", "") or "").strip() == folder_title:
                resolved_uid = str(folder.get("uid", "") or "").strip()
                if resolved_uid:
                    return resolved_uid
        raise requests.RequestException(
            f"Grafana folder create response for '{folder_title}' did not include a uid"
        )
    if create.status_code == 412:
        # Race-safe fallback when folder already exists by title.
        response = session_obj.get(f"{grafana_url}/api/folders", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        folders = payload if isinstance(payload, list) else []
        for folder in folders:
            if str(folder.get("title", "") or "").strip() == folder_title:
                return str(folder.get("uid", "") or "")
    create.raise_for_status()
    return requested_uid


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session.auth = (args.user, args.password)
    target_folder_uid = args.folder_uid
    if not args.dry_run:
        try:
            target_folder_uid = _ensure_folder_uid(
                session,
                args.grafana_url,
                args.folder_uid,
                args.folder_title,
                args.timeout,
            )
        except requests.RequestException as exc:
            print(f"  FAILED (folder setup): {exc}")
            return 1

    created = 0
    failed = 0

    for rule_def in ALERT_RULES:
        payload = {
            "folderUID": target_folder_uid,
            "ruleGroup": rule_def["ruleGroup"],
            "title": rule_def["title"],
            "condition": rule_def["condition"],
            "noDataState": "NoData",
            "execErrState": "Error",
            "for": rule_def["for"],
            "annotations": rule_def.get("annotations", {}),
            "labels": rule_def.get("labels", {}),
            "data": rule_def["data"],
        }

        if args.dry_run:
            print(f"  DRY RUN: would create {rule_def['title']}")
            created += 1
            continue

        try:
            resp = session.post(
                f"{args.grafana_url}/api/v1/provisioning/alert-rules",
                json=payload,
                timeout=args.timeout,
            )
        except requests.RequestException as exc:
            print(f"  FAILED (request error): {rule_def['title']}")
            print(f"    {exc}")
            return 1

        if resp.status_code in (200, 201):
            result = resp.json() if resp.content else {}
            title = result.get("title", rule_def["title"])
            uid = result.get("uid", "unknown")
            print(f"  Created: {title} (uid={uid})")
            created += 1
        else:
            print(f"  FAILED ({resp.status_code}): {rule_def['title']}")
            print(f"    {resp.text[:300]}")
            failed += 1

    print(f"\nCreated {created}/{len(ALERT_RULES)} alert rules")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
