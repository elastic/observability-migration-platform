# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the alert/monitor migration pipeline.

Covers:
- AlertingIR builders (Grafana legacy, Grafana unified, Datadog monitors)
- Fidelity classification and semantic loss tracking
- Mapping engine (tier selection, rule type selection, payload building)
- Grafana extract helpers (session auth, unified alerting)
- Datadog extract helpers (monitor file loading)
- Kibana alerting client (preflight, validation)
- CLI flag wiring
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from observability_migration.core.assets.alerting import (
    AlertingIR,
    build_alerting_ir_from_datadog,
    build_alerting_ir_from_grafana,
    build_alerting_ir_from_grafana_unified,
)
from observability_migration.core.assets.status import AssetStatus
from observability_migration.core.mapping import (
    ES_QUERY_RULE_TYPE,
    MANUAL_ONLY_KINDS,
    build_custom_threshold_rule_params,
    build_es_query_rule_params,
    build_index_threshold_rule_params,
    classify_automation_tier,
    map_alert_to_kibana_payload,
    map_alerts_batch,
    record_semantic_losses,
    select_target_rule_type,
)
from observability_migration.core.reporting.report import MigrationResult

# =====================================================================
# Fixtures
# =====================================================================


def _grafana_legacy_alert_task():
    return {
        "task_type": "alert_migration",
        "dashboard": "Node Exporter",
        "dashboard_uid": "abc123",
        "panel": "CPU Usage",
        "alert_name": "High CPU",
        "alert_type": "legacy",
        "suggested_kibana_rule_type": "threshold",
        "frequency": "60s",
        "pending_for": "5m",
        "no_data_state": "no_data",
        "exec_error_state": "alerting",
        "conditions": [
            {
                "evaluator_type": "gt",
                "evaluator_params": [80],
                "operator": "and",
                "query_ref": "A",
                "reducer": "avg",
            }
        ],
        "conditions_description": ["avg() gt [80] on ref A"],
        "notification_channels": ["uid-slack-1"],
    }


def _grafana_legacy_prometheus_alert_task():
    task = copy.deepcopy(_grafana_legacy_alert_task())
    task["source_queries"] = [
        {
            "ref_id": "A",
            "datasource_uid": "prometheus",
            "datasource_type": "prometheus",
            "datasource_name": "Prometheus",
            "expr": '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
        }
    ]
    task["datasource_map"] = {
        "prometheus": {
            "uid": "prometheus",
            "type": "prometheus",
            "name": "Prometheus",
        }
    }
    return task


def _grafana_legacy_prometheus_range_alert_task(
    evaluator_type: str = "outside_range",
    params: list[float] | None = None,
):
    task = copy.deepcopy(_grafana_legacy_prometheus_alert_task())
    bounds = list(params or [20, 80])
    task["alert_name"] = f"CPU {evaluator_type}"
    task["conditions"][0]["evaluator_type"] = evaluator_type
    task["conditions"][0]["evaluator_params"] = bounds
    task["conditions_description"] = [
        f"avg() {evaluator_type} [{', '.join(str(item) for item in bounds)}] on ref A"
    ]
    return task


def _grafana_unified_rule():
    return {
        "uid": "rule-uid-1",
        "title": "Memory pressure alert",
        "ruleGroup": "resource-alerts",
        "folderUID": "folder-1",
        "condition": "C",
        "for": "5m",
        "noDataState": "NoData",
        "execErrState": "Error",
        "isPaused": False,
        "labels": {"severity": "warning", "team": "infra"},
        "annotations": {
            "summary": "Memory is above threshold",
            "__dashboardUid__": "dash-123",
            "__panelId__": "42",
        },
        "data": [
            {
                "refId": "A",
                "datasourceUid": "prometheus",
                "relativeTimeRange": {"from": 300, "to": 0},
                "model": {"expr": "node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes < 0.1"},
            },
            {
                "refId": "B",
                "datasourceUid": "-100",
                "relativeTimeRange": {"from": 0, "to": 0},
                "model": {"type": "reduce", "reducer": "last"},
            },
            {
                "refId": "C",
                "datasourceUid": "-100",
                "relativeTimeRange": {"from": 0, "to": 0},
                "model": {"type": "threshold", "conditions": [{"evaluator": {"type": "lt", "params": [0.1]}}]},
            },
        ],
    }


def _grafana_unified_prometheus_rule():
    return {
        "uid": "rule-prom-1",
        "title": "High CPU usage",
        "ruleGroup": "resource-alerts",
        "folderUID": "folder-1",
        "condition": "C",
        "for": "5m",
        "noDataState": "NoData",
        "execErrState": "Error",
        "isPaused": False,
        "labels": {"severity": "warning", "team": "infra"},
        "annotations": {"summary": "CPU is above threshold"},
        "data": [
            {
                "refId": "A",
                "datasourceUid": "prometheus",
                "relativeTimeRange": {"from": 300, "to": 0},
                "model": {
                    "expr": '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
                },
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "relativeTimeRange": {"from": 0, "to": 0},
                "model": {"type": "reduce", "reducer": "last"},
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "relativeTimeRange": {"from": 0, "to": 0},
                "model": {"type": "threshold", "conditions": [{"evaluator": {"type": "gt", "params": [80]}}]},
            },
        ],
    }


def _grafana_unified_prometheus_safe_rule():
    rule = copy.deepcopy(_grafana_unified_prometheus_rule())
    rule["uid"] = "rule-prom-safe-1"
    rule["title"] = "High CPU usage safe subset"
    rule["noDataState"] = "OK"
    rule["labels"] = {}
    rule["annotations"] = {"summary": "CPU is above threshold"}
    return rule


def _grafana_unified_prometheus_safe_with_labels_rule():
    rule = _grafana_unified_prometheus_safe_rule()
    rule["uid"] = "rule-prom-safe-labels-1"
    rule["title"] = "High CPU usage safe labels subset"
    rule["labels"] = {"severity": "warning", "team": "infra"}
    return rule


def _grafana_unified_prometheus_topk_safe_rule():
    rule = _grafana_unified_prometheus_safe_rule()
    rule["uid"] = "rule-prom-topk-safe-1"
    rule["title"] = "Top user CPU safe subset"
    rule["annotations"] = {"summary": "Top user CPU instances are above threshold"}
    rule["data"][0]["model"] = {
        "expr": 'topk(5, avg by(instance) (rate(node_cpu_seconds_total{mode="user"}[5m])))',
        "instant": True,
        "range": False,
    }
    rule["data"][2]["model"]["conditions"][0]["evaluator"]["params"] = [0.02]
    return rule


def _grafana_unified_prometheus_bottomk_safe_rule():
    rule = _grafana_unified_prometheus_safe_rule()
    rule["uid"] = "rule-prom-bottomk-safe-1"
    rule["title"] = "Lowest idle CPU safe subset"
    rule["annotations"] = {"summary": "Lowest idle CPU instances are below threshold"}
    rule["data"][0]["model"] = {
        "expr": 'bottomk(5, avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
        "instant": True,
        "range": False,
    }
    rule["data"][2]["model"]["conditions"][0]["evaluator"] = {"type": "lt", "params": [0.95]}
    return rule


def _grafana_unified_unsupported_prometheus_rule():
    rule = copy.deepcopy(_grafana_unified_prometheus_rule())
    rule["uid"] = "rule-prom-unsupported-1"
    rule["title"] = "Topk CPU usage"
    rule["data"][0]["model"]["expr"] = "topk(5, node_cpu_seconds_total)"
    return rule


def _grafana_unified_multi_source_prometheus_rule():
    rule = copy.deepcopy(_grafana_unified_prometheus_rule())
    rule["uid"] = "rule-prom-multi-source-1"
    rule["title"] = "Combined CPU usage"
    rule["condition"] = "D"
    rule["data"] = [
        copy.deepcopy(rule["data"][0]),
        {
            "refId": "B",
            "datasourceUid": "prometheus",
            "relativeTimeRange": {"from": 300, "to": 0},
            "model": {
                "expr": 'avg by(instance) (rate(node_cpu_seconds_total{mode="user"}[5m]))',
            },
        },
        {
            "refId": "C",
            "datasourceUid": "__expr__",
            "relativeTimeRange": {"from": 0, "to": 0},
            "model": {"type": "reduce", "reducer": "last"},
        },
        {
            "refId": "D",
            "datasourceUid": "__expr__",
            "relativeTimeRange": {"from": 0, "to": 0},
            "model": {"type": "threshold", "conditions": [{"evaluator": {"type": "gt", "params": [80]}}]},
        },
    ]
    return rule


def _grafana_unified_logql_rule():
    return {
        "uid": "rule-log-1",
        "title": "Error log spike",
        "ruleGroup": "log-alerts",
        "folderUID": "folder-1",
        "condition": "C",
        "for": "5m",
        "noDataState": "NoData",
        "execErrState": "Error",
        "isPaused": False,
        "labels": {"severity": "warning", "team": "app"},
        "annotations": {"summary": "Error logs are spiking"},
        "data": [
            {
                "refId": "A",
                "datasourceUid": "loki",
                "relativeTimeRange": {"from": 300, "to": 0},
                "model": {
                    "expr": 'count_over_time({job=~".+"} |= "error" [5m])',
                },
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "relativeTimeRange": {"from": 0, "to": 0},
                "model": {"type": "reduce", "reducer": "last"},
            },
            {
                "refId": "C",
                "datasourceUid": "__expr__",
                "relativeTimeRange": {"from": 0, "to": 0},
                "model": {"type": "threshold", "conditions": [{"evaluator": {"type": "gt", "params": [100]}}]},
            },
        ],
    }


def _datadog_metric_monitor():
    return {
        "id": 12345,
        "name": "High CPU on web hosts",
        "type": "metric alert",
        "query": "avg(last_5m):avg:system.cpu.user{env:production} by {host} > 90",
        "message": "CPU is above 90%! @slack-infra-alerts @pagerduty-oncall",
        "tags": ["env:production", "team:infra"],
        "priority": 2,
        "multi": True,
        "options": {
            "thresholds": {"critical": 90, "warning": 80},
            "notify_no_data": True,
            "no_data_timeframe": 10,
            "renotify_interval": 60,
            "evaluation_delay": 300,
            "require_full_window": True,
            "notify_by": ["host"],
        },
    }


def _datadog_change_query_alert():
    return {
        "id": 12346,
        "name": "CPU changed sharply",
        "type": "query alert",
        "query": "pct_change(avg(last_5m),30m_ago):avg:system.cpu.user{env:production} by {host} > 10",
        "message": "CPU changed sharply",
        "tags": ["env:production", "team:infra"],
        "multi": True,
        "options": {
            "thresholds": {"critical": 10},
            "notify_no_data": False,
        },
    }


def _datadog_log_monitor():
    return {
        "id": 67890,
        "name": "Error log spike",
        "type": "log alert",
        "query": 'logs("status:error").index("main").rollup("count").last("5m") > 100',
        "message": "Error logs spiking!",
        "tags": ["team:backend"],
        "options": {
            "thresholds": {"critical": 100},
            "notify_no_data": False,
        },
    }


def _datadog_log_measure_monitor():
    return {
        "id": 67891,
        "name": "Checkout latency p99 is high",
        "type": "log alert",
        "query": 'logs("service:checkout status:error").index("*").rollup("pc99", "@duration").last("10m") > 250',
        "message": "Checkout latency p99 is high",
        "tags": ["team:backend"],
        "options": {
            "thresholds": {"critical": 250},
            "notify_no_data": False,
        },
    }


def _datadog_log_avg_measure_monitor():
    return {
        "id": 67892,
        "name": "Checkout latency avg is high",
        "type": "log alert",
        "query": 'logs("service:checkout status:error").index("*").rollup("avg", "@duration").last("10m") > 150',
        "message": "Checkout latency avg is high",
        "tags": ["team:backend"],
        "options": {
            "thresholds": {"critical": 150},
            "notify_no_data": False,
        },
    }


def _datadog_service_check_monitor():
    return {
        "id": 35767150,
        "name": "[Auto] Clock in sync with NTP",
        "type": "service check",
        "query": '"ntp.in_sync".over("*").last(2).count_by_status()',
        "message": "Clock drift detected",
        "options": {
            "thresholds": {
                "critical": 1.0,
                "ok": 1.0,
                "warning": 1.0,
            }
        },
        "multi": True,
    }


def _datadog_live_metric_query_alert():
    return {
        "id": 221632609,
        "name": "{{component_type.name}} component is producing errors",
        "type": "query alert",
        "query": (
            "sum(last_15m):sum:pipelines.component_errors_total"
            "{!component_id:_*,pipeline_id:46a24f86-9fd2-11f0-9e3d-da7ad0900002} "
            "by {component_type,component_id,host,worker_uuid} > 0"
        ),
        "message": "Component errors detected",
        "options": {
            "include_tags": True,
            "new_group_delay": 60,
            "notify_no_data": False,
            "thresholds": {"critical": 0.0},
            "timeout_h": 1,
        },
        "multi": True,
    }


def _datadog_as_rate_query_alert():
    return {
        "id": 45678,
        "name": "Checkout request rate is high",
        "type": "query alert",
        "query": (
            "sum(last_5m):sum:http.requests{env:prod,service:checkout} "
            "by {service}.as_rate() > 5"
        ),
        "message": "Checkout request rate is high",
        "options": {
            "thresholds": {"critical": 5},
            "notify_no_data": False,
        },
        "multi": True,
    }


def _datadog_rollup_query_alert():
    return {
        "id": 45679,
        "name": "CPU rollup is high",
        "type": "query alert",
        "query": (
            "avg(last_15m):avg:system.cpu.user{env:production} "
            "by {host}.rollup(avg, 60) > 80"
        ),
        "message": "CPU rollup is high",
        "options": {
            "thresholds": {"critical": 80},
            "notify_no_data": False,
        },
        "multi": True,
    }


def _datadog_default_zero_query_alert():
    return {
        "id": 45680,
        "name": "[Kubernetes] Pod {{pod_name.name}} is CrashloopBackOff on namespace {{kube_namespace.name}}",
        "type": "query alert",
        "query": (
            "max(last_10m):default_zero(max:kubernetes_state.container.status_report.count.waiting"
            "{reason:crashloopbackoff} by {kube_cluster_name,kube_namespace,pod_name}) >= 1"
        ),
        "message": "Pod is crashlooping",
        "options": {
            "thresholds": {"critical": 1},
            "notify_no_data": False,
            "require_full_window": False,
        },
        "multi": True,
    }


def _datadog_formula_ratio_query_alert():
    return {
        "id": 45681,
        "name": "[Redis] High memory consumption",
        "type": "query alert",
        "query": "avg(last_5m):100 * avg:redis.mem.used{*} / avg:redis.mem.maxmemory{*} > 90",
        "message": "Redis memory usage is high",
        "options": {
            "thresholds": {"critical": 90, "warning": 70},
            "notify_no_data": False,
        },
    }


def _datadog_formula_as_count_error_rate_query_alert():
    return {
        "id": 456811,
        "name": "Error Rate",
        "type": "query alert",
        "query": (
            "sum(last_5m):sum:shopist.checkouts.failed{env:prod} by {region}.as_count() / "
            "(sum:shopist.checkouts.failed{env:prod} by {region}.as_count() + "
            "sum:shopist.checkouts.success{env:prod} by {region}.as_count()) > 0.5"
        ),
        "message": "The error rate is currently high",
        "options": {
            "thresholds": {"critical": 0.5},
            "notify_no_data": False,
        },
        "multi": True,
    }


def _datadog_shifted_formula_week_before_query_alert():
    return {
        "id": 456812,
        "name": "[Seasonal threshold] Amount of connection",
        "type": "query alert",
        "query": (
            "sum(last_10m):sum:nginx.requests.total_count{env:prod} by {datacenter} / "
            "week_before(sum:nginx.requests.total_count{env:prod} by {datacenter}) <= 0.9"
        ),
        "message": "The amount of connection is lower than week before",
        "options": {
            "thresholds": {"critical": 0.9},
            "notify_no_data": False,
        },
        "multi": True,
    }


def _datadog_shifted_formula_timeshift_query_alert():
    return {
        "id": 456813,
        "name": "CPU shifted ratio",
        "type": "query alert",
        "query": (
            "avg(last_5m):avg:system.cpu.user{env:production} by {host} / "
            "timeshift(avg:system.cpu.user{env:production} by {host}, -300) > 0.5"
        ),
        "message": "CPU shifted ratio is high",
        "options": {
            "thresholds": {"critical": 0.5},
            "notify_no_data": False,
        },
        "multi": True,
    }


def _datadog_anomaly_query_alert():
    return {
        "id": 45682,
        "name": "[Postgres] Replication delay is abnormally high on {{host.name}}",
        "type": "query alert",
        "query": (
            "avg(last_1h):anomalies(avg:postgresql.replication_delay{*}, 'basic', 2, "
            "direction='above', alert_window='last_15m', interval=20, count_default_zero='true') >= 1"
        ),
        "message": "Replication delay anomaly detected",
        "options": {
            "thresholds": {"critical": 1, "critical_recovery": 0},
            "threshold_windows": {
                "recovery_window": "last_15m",
                "trigger_window": "last_15m",
            },
            "notify_no_data": False,
            "require_full_window": True,
        },
    }


def _datadog_exclude_null_query_alert():
    return {
        "id": 45683,
        "name": "CPU exclude_null wrapper",
        "type": "query alert",
        "query": (
            "avg(last_5m):exclude_null("
            "avg:system.cpu.user{env:production,service:exclude-null-demo} by {host}"
            ") > 80"
        ),
        "message": "CPU is high",
        "options": {
            "thresholds": {"critical": 80},
            "notify_no_data": False,
        },
    }


def _datadog_calendar_shift_query_alert():
    return {
        "id": 456814,
        "name": "CPU calendar_shift ratio",
        "type": "query alert",
        "query": (
            "avg(last_5m):avg:system.cpu.user{env:production,service:calendar-shift-demo} by {host} / "
            'calendar_shift(avg:system.cpu.user{env:production,service:calendar-shift-demo} by {host}, "-1d", "UTC") > 1.2'
        ),
        "message": "CPU is higher than previous UTC day",
        "options": {
            "thresholds": {"critical": 1.2},
            "notify_no_data": False,
        },
        "multi": True,
    }


def _datadog_calendar_shift_month_query_alert():
    return {
        "id": 456816,
        "name": "CPU calendar_shift month ratio",
        "type": "query alert",
        "query": (
            "avg(last_5m):avg:system.cpu.user{env:production,service:calendar-shift-month-demo} by {host} / "
            'calendar_shift(avg:system.cpu.user{env:production,service:calendar-shift-month-demo} by {host}, "-1mo", "UTC") > 1.1'
        ),
        "message": "CPU is higher than previous UTC month",
        "options": {
            "thresholds": {"critical": 1.1},
            "notify_no_data": False,
        },
        "multi": True,
    }


def _datadog_calendar_shift_timezone_query_alert():
    monitor = _datadog_calendar_shift_query_alert()
    monitor["id"] = 456815
    monitor["name"] = "CPU calendar_shift timezone-sensitive"
    monitor["query"] = monitor["query"].replace('"UTC"', '"America/New_York"')
    return monitor


def _datadog_calendar_shift_no_dst_timezone_query_alert():
    monitor = _datadog_calendar_shift_query_alert()
    monitor["id"] = 456818
    monitor["name"] = "CPU calendar_shift stable-offset timezone"
    monitor["query"] = monitor["query"].replace('"UTC"', '"Asia/Kolkata"')
    return monitor


def _datadog_calendar_shift_no_dst_month_timezone_query_alert():
    monitor = _datadog_calendar_shift_month_query_alert()
    monitor["id"] = 456819
    monitor["name"] = "CPU calendar_shift month stable-offset timezone"
    monitor["query"] = monitor["query"].replace('"UTC"', '"Asia/Tokyo"')
    return monitor


def _datadog_exclude_null_rollup_query_alert():
    return {
        "id": 456817,
        "name": "CPU exclude_null rollup wrapper",
        "type": "query alert",
        "query": (
            "avg(last_5m):exclude_null("
            "avg:system.cpu.user{env:production,service:exclude-null-demo} by {host}.rollup(avg, 60)"
            ") > 80"
        ),
        "message": "CPU is high with rollup",
        "options": {
            "thresholds": {"critical": 80},
            "notify_no_data": False,
        },
    }


def _datadog_composite_monitor():
    return {
        "id": 99999,
        "name": "Composite: CPU and Memory",
        "type": "composite",
        "query": "12345 && 67890",
        "message": "Both monitors triggered",
        "options": {},
    }


# =====================================================================
# AlertingIR builder tests
# =====================================================================


class TestBuildAlertingIRFromGrafana(unittest.TestCase):
    def test_legacy_alert_basic_fields(self):
        task = _grafana_legacy_alert_task()
        ir = build_alerting_ir_from_grafana(task)
        self.assertEqual(ir.kind, "grafana_legacy")
        self.assertEqual(ir.name, "High CPU")
        self.assertIn("abc123", ir.alert_id)
        self.assertEqual(ir.evaluation_window, "60s")
        self.assertEqual(ir.no_data_policy, "no_data")
        self.assertEqual(ir.target_candidate, "threshold")
        self.assertEqual(ir.source_extension["alert_type"], "legacy")

    def test_legacy_alert_notification_channels(self):
        task = _grafana_legacy_alert_task()
        ir = build_alerting_ir_from_grafana(task)
        self.assertEqual(len(ir.actions), 1)
        self.assertIn("uid-slack-1", ir.actions[0]["notification_channels"])

    def test_legacy_alert_condition_summary(self):
        task = _grafana_legacy_alert_task()
        ir = build_alerting_ir_from_grafana(task)
        self.assertIn("avg()", ir.condition_summary)

    def test_legacy_alert_conditions_preserved(self):
        task = _grafana_legacy_alert_task()
        ir = build_alerting_ir_from_grafana(task)
        self.assertEqual(ir.source_extension["conditions"][0]["query_ref"], "A")

    def test_legacy_alert_source_queries_preserved_when_present(self):
        task = _grafana_legacy_prometheus_alert_task()
        ir = build_alerting_ir_from_grafana(task)
        self.assertEqual(ir.source_extension["source_queries"][0]["datasource_type"], "prometheus")
        self.assertIn("node_cpu_seconds_total", ir.source_extension["source_queries"][0]["expr"])


class TestBuildAlertingIRFromGrafanaUnified(unittest.TestCase):
    def test_unified_rule_basic_fields(self):
        rule = _grafana_unified_rule()
        ir = build_alerting_ir_from_grafana_unified(rule)
        self.assertEqual(ir.kind, "grafana_unified")
        self.assertEqual(ir.name, "Memory pressure alert")
        self.assertEqual(ir.alert_id, "rule-uid-1")
        self.assertEqual(ir.no_data_policy, "NoData")
        self.assertEqual(ir.pending_period, "5m")
        self.assertEqual(ir.automation_tier, "draft_requires_review")
        self.assertEqual(ir.target_rule_type, "es-query")

    def test_unified_rule_evaluation_window(self):
        rule = _grafana_unified_rule()
        ir = build_alerting_ir_from_grafana_unified(rule)
        self.assertEqual(ir.evaluation_window, "5m")

    def test_unified_rule_source_extension(self):
        rule = _grafana_unified_rule()
        ir = build_alerting_ir_from_grafana_unified(rule)
        self.assertIn("data", ir.source_extension)
        self.assertIn("labels", ir.source_extension)
        self.assertEqual(ir.source_extension["labels"]["severity"], "warning")

    def test_unified_rule_no_uid_uses_title(self):
        rule = _grafana_unified_rule()
        rule.pop("uid")
        ir = build_alerting_ir_from_grafana_unified(rule)
        self.assertEqual(ir.alert_id, "Memory pressure alert")

    def test_unified_rule_hour_window(self):
        rule = _grafana_unified_rule()
        rule["data"][0]["relativeTimeRange"]["from"] = 3600
        ir = build_alerting_ir_from_grafana_unified(rule)
        self.assertEqual(ir.evaluation_window, "1h")

    def test_unified_rule_preserves_datasource_metadata_when_provided(self):
        rule = _grafana_unified_prometheus_rule()
        ir = build_alerting_ir_from_grafana_unified(
            rule,
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        self.assertEqual(ir.source_extension["datasource_map"]["prometheus"]["type"], "prometheus")


class TestBuildAlertingIRFromDatadog(unittest.TestCase):
    def test_metric_monitor_basic_fields(self):
        mon = _datadog_metric_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        self.assertEqual(ir.kind, "datadog_metric")
        self.assertEqual(ir.name, "High CPU on web hosts")
        self.assertEqual(ir.alert_id, "12345")
        self.assertEqual(ir.severity, "2")
        self.assertEqual(ir.no_data_policy, "notify")

    def test_metric_monitor_evaluation_window(self):
        mon = _datadog_metric_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        self.assertEqual(ir.evaluation_window, "5m")

    def test_metric_monitor_automation_tier(self):
        mon = _datadog_metric_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        self.assertEqual(ir.automation_tier, "draft_requires_review")

    def test_metric_monitor_target_rule_type(self):
        mon = _datadog_metric_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        self.assertEqual(ir.target_rule_type, "custom-threshold")

    def test_anomaly_query_alert_maps_to_manual_only_kind(self):
        ir = build_alerting_ir_from_datadog(_datadog_anomaly_query_alert())
        self.assertEqual(ir.kind, "datadog_anomaly_alert")

    def test_forecast_query_alert_maps_to_manual_only_kind(self):
        ir = build_alerting_ir_from_datadog(
            {
                "id": 901,
                "name": "Forecasted CPU saturation",
                "type": "query alert",
                "query": "avg(last_1h):forecast(avg:system.cpu.user{env:production} by {host}, 'linear', 1, interval=60) > 80",
                "options": {"thresholds": {"critical": 80}},
            }
        )
        self.assertEqual(ir.kind, "datadog_forecast")

    def test_outlier_query_alert_maps_to_manual_only_kind(self):
        ir = build_alerting_ir_from_datadog(
            {
                "id": 902,
                "name": "Outlier CPU host",
                "type": "query alert",
                "query": "avg(last_30m):outliers(avg:system.cpu.user{env:production} by {host}, 'dbscan', 7) > 0",
                "options": {"thresholds": {"critical": 0}},
            }
        )
        self.assertEqual(ir.kind, "datadog_outlier")

    def test_log_monitor_kind(self):
        mon = _datadog_log_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        self.assertEqual(ir.kind, "datadog_log")
        self.assertEqual(ir.target_rule_type, "es-query")

    def test_composite_monitor(self):
        mon = _datadog_composite_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        self.assertEqual(ir.kind, "datadog_composite")
        self.assertEqual(ir.automation_tier, "manual_required")

    def test_notification_summary_from_message(self):
        mon = _datadog_metric_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        self.assertIn("@slack-infra-alerts", ir.notification_summary)

    def test_no_data_policy_false(self):
        mon = _datadog_log_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        self.assertEqual(ir.no_data_policy, "no_notify")

    def test_empty_monitor(self):
        ir = build_alerting_ir_from_datadog({})
        self.assertEqual(ir.kind, "datadog_monitor")
        self.assertEqual(ir.name, "")

    def test_condition_summary_includes_thresholds(self):
        mon = _datadog_metric_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        self.assertIn("thresholds=", ir.condition_summary)

    def test_metric_monitor_with_field_profile_gets_translated_query(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_metric_monitor(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("AVG(system.cpu.user.pct)", ir.translated_query)
        self.assertIn('deployment.environment == "production"', ir.translated_query)
        self.assertIn("host.name", ir.translated_query)
        self.assertIn("| WHERE value > 90.0", ir.translated_query)

    def test_change_query_alert_gets_exact_translated_query_and_expanded_window(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_change_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertEqual(ir.evaluation_window, "35m")
        self.assertIn("FROM metrics-*", ir.translated_query)
        self.assertIn('deployment.environment == "production"', ir.translated_query)
        self.assertIn("current_value = AVG(system.cpu.user.pct)", ir.translated_query)
        self.assertIn("previous_value = AVG(system.cpu.user.pct)", ir.translated_query)
        self.assertIn("@timestamp >= NOW() - 5 minutes", ir.translated_query)
        self.assertIn("@timestamp >= NOW() - 35 minutes", ir.translated_query)
        self.assertIn("@timestamp < NOW() - 30 minutes", ir.translated_query)
        self.assertIn("host.name", ir.translated_query)
        self.assertIn("((current_value - previous_value) / previous_value) * 100", ir.translated_query)
        self.assertIn("| WHERE value > 10.0", ir.translated_query)

    def test_change_query_alert_accepts_last_style_shift_syntax(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        monitor = _datadog_change_query_alert()
        monitor["query"] = monitor["query"].replace("30m_ago", "last_30m")
        ir = build_alerting_ir_from_datadog(monitor, field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertEqual(ir.evaluation_window, "35m")

    def test_live_metric_query_alert_with_custom_metric_mapping_gets_translated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        field_map.metric_map["pipelines.component_errors_total"] = "pipelines.component_errors_total"
        ir = build_alerting_ir_from_datadog(_datadog_live_metric_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("SUM(pipelines.component_errors_total)", ir.translated_query)
        self.assertIn('pipeline_id == "46a24f86-9fd2-11f0-9e3d-da7ad0900002"', ir.translated_query)
        self.assertIn("component_type", ir.translated_query)
        self.assertIn("component_id", ir.translated_query)
        self.assertIn("host.name", ir.translated_query)
        self.assertIn("worker_uuid", ir.translated_query)
        self.assertIn("| WHERE value > 0.0", ir.translated_query)

    def test_live_metric_query_alert_with_missing_live_fields_stays_untranslated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.core.verification.field_capabilities import FieldCapability

        field_map = load_profile("otel")
        field_map.metric_field_caps = {
            "host.name": FieldCapability(name="host.name", type="keyword"),
        }
        field_map.field_caps = dict(field_map.metric_field_caps)

        ir = build_alerting_ir_from_datadog(_datadog_live_metric_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query, "")
        self.assertEqual(ir.translated_query_provenance, "")

    def test_query_alert_with_as_rate_gets_translated_query(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_as_rate_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("SUM(http_requests)", ir.translated_query)
        self.assertIn("/ 300", ir.translated_query)
        self.assertIn("http_requests", ir.translated_query)
        self.assertIn("service.name", ir.translated_query)
        self.assertIn("| WHERE value > 5.0", ir.translated_query)
        self.assertFalse(any("rate semantics approximated" in warning for warning in ir.warnings))

    def test_query_alert_with_as_count_and_outer_avg_uses_metric_space_aggregation(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        monitor = {
            "id": 91001,
            "name": "High 5xx rate",
            "type": "query alert",
            "query": "avg(last_5m):sum:trace.http.request.errors{service:sample-api}.as_count() > 25",
        }

        ir = build_alerting_ir_from_datadog(monitor, field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("SUM(trace_http_request_errors)", ir.translated_query)
        self.assertIn('service.name == "sample-api"', ir.translated_query)
        self.assertIn("| WHERE value > 25.0", ir.translated_query)

    def test_query_alert_with_p95_and_outer_avg_uses_percentile_metric_aggregation(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        monitor = {
            "id": 91003,
            "name": "p95 latency regression",
            "type": "query alert",
            "query": "avg(last_10m):p95:trace.http.request.duration{service:sample-api} > 1.5",
        }

        ir = build_alerting_ir_from_datadog(monitor, field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("PERCENTILE(trace_http_request_duration, 95)", ir.translated_query)
        self.assertIn('service.name == "sample-api"', ir.translated_query)
        self.assertIn("| WHERE value > 1.5", ir.translated_query)

    def test_query_alert_with_rollup_gets_translated_query(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_rollup_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("rollup_bucket = BUCKET(", ir.translated_query)
        self.assertIn("rollup_value = AVG(system.cpu.user.pct)", ir.translated_query)
        self.assertIn("STATS value = AVG(rollup_value)", ir.translated_query)
        self.assertIn('deployment.environment == "production"', ir.translated_query)
        self.assertIn("host.name", ir.translated_query)
        self.assertIn("| WHERE value > 80.0", ir.translated_query)
        self.assertFalse(any("rollup interval is approximated" in warning for warning in ir.warnings))

    def test_query_alert_with_default_zero_wrapper_gets_translated_without_warning(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_default_zero_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("COALESCE(", ir.translated_query)
        self.assertIn("kubernetes_state_container_status_report_count_waiting", ir.translated_query)
        self.assertIn("k8s.cluster.name", ir.translated_query)
        self.assertIn("k8s.namespace.name", ir.translated_query)
        self.assertIn("k8s.pod.name", ir.translated_query)
        self.assertIn("| WHERE value >= 1.0", ir.translated_query)
        self.assertFalse(any("default_zero" in warning for warning in ir.warnings))

    def test_formula_query_alert_gets_translated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        field_map.metric_map["redis.mem.used"] = "redis.mem.used"
        field_map.metric_map["redis.mem.maxmemory"] = "redis.mem.maxmemory"
        ir = build_alerting_ir_from_datadog(_datadog_formula_ratio_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("FROM metrics-*", ir.translated_query)
        self.assertIn("q1 = AVG(redis.mem.used)", ir.translated_query)
        self.assertIn("q2 = AVG(redis.mem.maxmemory)", ir.translated_query)
        self.assertIn("| WHERE q1 IS NOT NULL AND q2 IS NOT NULL", ir.translated_query)
        self.assertIn("CASE(q2 == 0, NULL, ((100 * q1) / q2))", ir.translated_query)
        self.assertIn("| WHERE value > 90.0", ir.translated_query)

    def test_formula_query_alert_with_mismatched_group_by_stays_untranslated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        field_map.metric_map["redis.mem.used"] = "redis.mem.used"
        field_map.metric_map["redis.mem.maxmemory"] = "redis.mem.maxmemory"
        monitor = _datadog_formula_ratio_query_alert()
        monitor["query"] = (
            "avg(last_5m):100 * avg:redis.mem.used{*} by {host} / "
            "avg:redis.mem.maxmemory{*} > 90"
        )
        ir = build_alerting_ir_from_datadog(monitor, field_map=field_map)

        self.assertEqual(ir.translated_query, "")
        self.assertEqual(ir.translated_query_provenance, "")

    def test_formula_as_count_error_rate_query_alert_gets_translated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        field_map.metric_map["shopist.checkouts.failed"] = "shopist.checkouts.failed_count"
        field_map.metric_map["shopist.checkouts.success"] = "shopist.checkouts.success_count"
        field_map.tag_map["region"] = "cloud.region"
        ir = build_alerting_ir_from_datadog(
            _datadog_formula_as_count_error_rate_query_alert(),
            field_map=field_map,
        )

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("FROM metrics-*", ir.translated_query)
        self.assertIn("q1 = SUM(shopist.checkouts.failed_count)", ir.translated_query)
        self.assertIn("q2 = SUM(shopist.checkouts.success_count)", ir.translated_query)
        self.assertIn('deployment.environment == "prod"', ir.translated_query)
        self.assertIn("BY cloud.region", ir.translated_query)
        self.assertIn("| WHERE q1 IS NOT NULL AND q2 IS NOT NULL", ir.translated_query)
        self.assertIn("CASE((q1 + q2) == 0, NULL, (q1 / (q1 + q2)))", ir.translated_query)
        self.assertIn("| WHERE value > 0.5", ir.translated_query)

    def test_shifted_formula_week_before_query_alert_gets_translated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        field_map.metric_map["nginx.requests.total_count"] = "nginx.requests.total_count"
        field_map.tag_map["datacenter"] = "cloud.region"
        ir = build_alerting_ir_from_datadog(
            _datadog_shifted_formula_week_before_query_alert(),
            field_map=field_map,
        )

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertEqual(ir.evaluation_window, "10090m")
        self.assertIn("FROM metrics-*", ir.translated_query)
        self.assertIn(
            'q1 = SUM(nginx.requests.total_count) WHERE deployment.environment == "prod" AND @timestamp >= NOW() - 10 minutes',
            ir.translated_query,
        )
        self.assertIn(
            'q2 = SUM(nginx.requests.total_count) WHERE deployment.environment == "prod" AND @timestamp >= NOW() - 10090 minutes AND @timestamp < NOW() - 7 days',
            ir.translated_query,
        )
        self.assertIn("BY cloud.region", ir.translated_query)
        self.assertIn("CASE(q2 == 0, NULL, (q1 / q2))", ir.translated_query)
        self.assertIn("| WHERE value <= 0.9", ir.translated_query)

    def test_shifted_formula_timeshift_query_alert_gets_translated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(
            _datadog_shifted_formula_timeshift_query_alert(),
            field_map=field_map,
        )

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertEqual(ir.evaluation_window, "10m")
        self.assertIn(
            'q2 = AVG(system_cpu_user) WHERE deployment.environment == "production" AND @timestamp >= NOW() - 10 minutes AND @timestamp < NOW() - 5 minutes',
            ir.translated_query,
        )
        self.assertIn("CASE(q2 == 0, NULL, (q1 / q2))", ir.translated_query)
        self.assertIn("| WHERE value > 0.5", ir.translated_query)

    def test_anomaly_query_alert_stays_untranslated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_anomaly_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query, "")
        self.assertEqual(ir.translated_query_provenance, "")

    def test_exclude_null_query_alert_gets_translated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_exclude_null_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("AVG(system.cpu.user.pct)", ir.translated_query)
        self.assertIn('deployment.environment == "production"', ir.translated_query)
        self.assertIn('service.name == "exclude-null-demo"', ir.translated_query)
        self.assertIn("host.name IS NOT NULL", ir.translated_query)
        self.assertIn('host.name != "N/A"', ir.translated_query)
        self.assertIn("BY host.name", ir.translated_query)
        self.assertIn("| WHERE value > 80.0", ir.translated_query)

    def test_calendar_shift_query_alert_gets_translated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertEqual(ir.evaluation_window, "1445m")
        self.assertIn("FROM metrics-*", ir.translated_query)
        self.assertIn(
            'q1 = AVG(system.cpu.user.pct) WHERE deployment.environment == "production" AND service.name == "calendar-shift-demo" AND @timestamp >= NOW() - 5 minutes',
            ir.translated_query,
        )
        self.assertIn(
            'q2 = AVG(system.cpu.user.pct) WHERE deployment.environment == "production" AND service.name == "calendar-shift-demo" AND @timestamp >= NOW() - 1445 minutes AND @timestamp < NOW() - 1 days',
            ir.translated_query,
        )
        self.assertIn("BY host.name", ir.translated_query)
        self.assertIn("CASE(q2 == 0, NULL, (q1 / q2))", ir.translated_query)
        self.assertIn("| WHERE value > 1.2", ir.translated_query)

    def test_calendar_shift_month_query_alert_gets_translated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_month_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertEqual(ir.evaluation_window, "46085m")
        self.assertIn(
            'q2 = AVG(system.cpu.user.pct) WHERE deployment.environment == "production" AND service.name == "calendar-shift-month-demo" AND @timestamp >= NOW() - 1 month - 5 minutes AND @timestamp < NOW() - 1 month',
            ir.translated_query,
        )
        self.assertIn("| WHERE value > 1.1", ir.translated_query)

    def test_calendar_shift_query_alert_with_timezone_stays_untranslated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_timezone_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query, "")
        self.assertEqual(ir.translated_query_provenance, "")
        self.assertFalse(ir.metadata.get("parse_degraded"))
        self.assertTrue(any("formula monitor requires manual review" in warning.lower() for warning in ir.warnings))
        self.assertFalse(any("parse degraded" in warning.lower() for warning in ir.warnings))

    def test_calendar_shift_query_alert_with_stable_offset_timezone_gets_translated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_no_dst_timezone_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertEqual(ir.evaluation_window, "1445m")
        self.assertIn('service.name == "calendar-shift-demo"', ir.translated_query)
        self.assertIn("@timestamp < NOW() - 1 days", ir.translated_query)

    def test_calendar_shift_month_query_alert_with_stable_offset_timezone_gets_translated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(
            _datadog_calendar_shift_no_dst_month_timezone_query_alert(),
            field_map=field_map,
        )

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertEqual(ir.evaluation_window, "46085m")
        self.assertIn("@timestamp >= NOW() - 1 month - 5 minutes", ir.translated_query)

    def test_exclude_null_rollup_query_alert_gets_translated_without_rollup_warning(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_exclude_null_rollup_query_alert(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("host.name IS NOT NULL", ir.translated_query)
        self.assertIn('host.name != "N/A"', ir.translated_query)
        self.assertFalse(any("rollup interval is approximated" in warning for warning in ir.warnings))

    def test_log_monitor_with_field_profile_gets_translated_query(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_log_monitor(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("FROM logs-*", ir.translated_query)
        self.assertIn('log.level == "error"', ir.translated_query)
        self.assertIn("STATS value = COUNT(*)", ir.translated_query)
        self.assertIn("| WHERE value > 100.0", ir.translated_query)

    def test_log_monitor_with_explicit_index_mapping_gets_exact_target_index(self):
        from observability_migration.adapters.source.datadog.field_map import FieldMapProfile

        field_map = FieldMapProfile(
            name="custom",
            logs_index="logs-*",
            log_index_map={"main": "logs-generic-default"},
        )
        ir = build_alerting_ir_from_datadog(_datadog_log_monitor(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("FROM logs-generic-default", ir.translated_query)
        self.assertFalse(
            any("approximated via the configured logs index" in warning for warning in ir.warnings)
        )

    def test_log_monitor_with_measure_rollup_gets_translated_query(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_log_measure_monitor(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertEqual(ir.evaluation_window, "10m")
        self.assertIn("FROM logs-*", ir.translated_query)
        self.assertIn('service.name == "checkout"', ir.translated_query)
        self.assertIn('log.level == "error"', ir.translated_query)
        self.assertIn("PERCENTILE(duration, 99)", ir.translated_query)
        self.assertIn("| WHERE value > 250.0", ir.translated_query)

    def test_log_monitor_with_avg_measure_rollup_gets_translated_query(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_log_avg_measure_monitor(), field_map=field_map)

        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("AVG(duration)", ir.translated_query)
        self.assertIn("| WHERE value > 150.0", ir.translated_query)

    def test_log_monitor_with_unbalanced_group_is_not_source_faithful(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        monitor = _datadog_log_monitor()
        monitor["query"] = (
            'logs("service:checkout AND (status:error OR host:api").index("*").rollup("count").last("5m") > 3'
        )
        ir = build_alerting_ir_from_datadog(monitor, field_map=field_map)

        self.assertEqual(ir.translated_query, "")
        self.assertEqual(ir.translated_query_provenance, "")
        self.assertTrue(any("log search parse" in warning.lower() for warning in ir.warnings))
        self.assertTrue(ir.metadata.get("parse_degraded"))
        diagnostics = list(ir.metadata.get("parser_diagnostics", []) or [])
        diagnostic_codes = {str(item.get("code", "")) for item in diagnostics if isinstance(item, dict)}
        self.assertIn("LOG_BOOLEAN_FALLBACK", diagnostic_codes)

    def test_log_measure_monitor_with_missing_live_field_stays_untranslated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.core.verification.field_capabilities import FieldCapability

        field_map = load_profile("otel")
        field_map.log_field_caps = {
            "service.name": FieldCapability(name="service.name", type="keyword", searchable=True),
            "log.level": FieldCapability(name="log.level", type="keyword", searchable=True),
        }
        field_map.field_caps = dict(field_map.log_field_caps)

        ir = build_alerting_ir_from_datadog(_datadog_log_measure_monitor(), field_map=field_map)

        self.assertEqual(ir.translated_query, "")
        self.assertEqual(ir.translated_query_provenance, "")

    def test_service_check_monitor_with_field_profile_stays_untranslated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_service_check_monitor(), field_map=field_map)

        self.assertEqual(ir.translated_query, "")
        self.assertEqual(ir.translated_query_provenance, "")


# =====================================================================
# Fidelity classification tests
# =====================================================================


class TestClassifyAutomationTier(unittest.TestCase):
    def test_grafana_legacy_simple_threshold_is_manual(self):
        ir = AlertingIR(kind="grafana_legacy", source_extension={"alert_type": "legacy"})
        self.assertEqual(classify_automation_tier(ir), "manual_required")

    def test_grafana_unified_prometheus_is_draft_review(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        self.assertEqual(classify_automation_tier(ir), "draft_requires_review")

    def test_grafana_unified_prometheus_safe_subset_is_automated(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_safe_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_grafana_unified_prometheus_safe_static_labels_are_automated(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_safe_with_labels_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_grafana_unified_prometheus_safe_topk_subset_is_automated(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_topk_safe_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_grafana_unified_prometheus_safe_bottomk_subset_is_automated(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_bottomk_safe_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_grafana_legacy_prometheus_query_is_automated(self):
        ir = build_alerting_ir_from_grafana(_grafana_legacy_prometheus_alert_task())
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_grafana_legacy_prometheus_outside_range_is_automated(self):
        ir = build_alerting_ir_from_grafana(_grafana_legacy_prometheus_range_alert_task("outside_range"))
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_grafana_legacy_prometheus_with_malformed_range_is_manual(self):
        ir = build_alerting_ir_from_grafana(
            _grafana_legacy_prometheus_range_alert_task("within_range", params=[20]),
        )
        self.assertEqual(classify_automation_tier(ir), "manual_required")

    def test_datadog_metric_simple_is_manual_without_translation(self):
        ir = AlertingIR(kind="datadog_metric", condition_summary="avg:system.cpu{host:*} > 90")
        self.assertEqual(classify_automation_tier(ir), "manual_required")

    def test_datadog_metric_with_translated_query_is_automated(self):
        ir = AlertingIR(
            kind="datadog_metric",
            condition_summary="avg:system.cpu{host:*} > 90",
            translated_query="FROM metrics-* | STATS cpu = AVG(system.cpu.user.pct) BY host.name | WHERE cpu > 90",
            translated_query_provenance="translated_esql",
        )
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_datadog_metric_formula_without_translation_is_manual(self):
        ir = AlertingIR(kind="datadog_metric", condition_summary="formula(a / b)")
        self.assertEqual(classify_automation_tier(ir), "manual_required")

    def test_datadog_exclude_null_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_exclude_null_query_alert(), field_map=field_map)
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_datadog_calendar_shift_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_query_alert(), field_map=field_map)
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_datadog_calendar_shift_month_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_month_query_alert(), field_map=field_map)
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_datadog_calendar_shift_query_alert_with_timezone_is_manual(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_timezone_query_alert(), field_map=field_map)
        self.assertEqual(classify_automation_tier(ir), "manual_required")

    def test_datadog_calendar_shift_query_alert_with_stable_offset_timezone_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_no_dst_timezone_query_alert(), field_map=field_map)
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_datadog_exclude_null_rollup_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_exclude_null_rollup_query_alert(), field_map=field_map)
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_datadog_default_zero_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_default_zero_query_alert(), field_map=field_map)
        self.assertEqual(classify_automation_tier(ir), "automated")

    def test_datadog_composite_is_manual(self):
        ir = AlertingIR(kind="datadog_composite")
        self.assertEqual(classify_automation_tier(ir), "manual_required")

    def test_datadog_log_is_manual_without_translation(self):
        ir = AlertingIR(kind="datadog_log")
        self.assertEqual(classify_automation_tier(ir), "manual_required")

    def test_datadog_parse_degraded_translation_is_manual(self):
        ir = AlertingIR(
            kind="datadog_log",
            translated_query="FROM logs-* | STATS value = COUNT(*)",
            translated_query_provenance="translated_esql",
            metadata={"parse_degraded": True},
        )
        self.assertEqual(classify_automation_tier(ir), "manual_required")

    def test_unknown_kind_is_manual(self):
        ir = AlertingIR(kind="some_unknown_source")
        self.assertEqual(classify_automation_tier(ir), "manual_required")

    def test_all_manual_only_kinds(self):
        for kind in MANUAL_ONLY_KINDS:
            ir = AlertingIR(kind=kind)
            self.assertEqual(classify_automation_tier(ir), "manual_required", f"Failed for {kind}")

    def test_datadog_manual_monitor_types_map_to_manual_only_kinds(self):
        manual_monitor_types = [
            "event alert",
            "apm alert",
            "rum alert",
            "synthetics alert",
            "ci alert",
            "slo alert",
            "audit alert",
            "cost alert",
            "network alert",
            "watchdog alert",
        ]
        for monitor_type in manual_monitor_types:
            ir = build_alerting_ir_from_datadog(
                {
                    "id": 1,
                    "name": monitor_type,
                    "type": monitor_type,
                    "query": "unsupported()",
                    "options": {},
                }
            )
            self.assertIn(ir.kind, MANUAL_ONLY_KINDS, f"{monitor_type} produced unmapped kind {ir.kind}")
            self.assertEqual(classify_automation_tier(ir), "manual_required")


class TestRecordSemanticLosses(unittest.TestCase):
    def test_datadog_renotify_loss(self):
        ir = build_alerting_ir_from_datadog(_datadog_metric_monitor())
        losses = record_semantic_losses(ir)
        self.assertTrue(any("renotify" in loss for loss in losses))

    def test_datadog_notification_handles_loss(self):
        ir = build_alerting_ir_from_datadog(_datadog_metric_monitor())
        losses = record_semantic_losses(ir)
        self.assertTrue(any("notification handles" in loss for loss in losses))

    def test_datadog_evaluation_delay_loss(self):
        ir = build_alerting_ir_from_datadog(_datadog_metric_monitor())
        losses = record_semantic_losses(ir)
        self.assertTrue(any("evaluation_delay" in loss for loss in losses))

    def test_grafana_notification_channel_loss(self):
        task = _grafana_legacy_alert_task()
        ir = build_alerting_ir_from_grafana(task)
        losses = record_semantic_losses(ir)
        self.assertTrue(any("notification channel" in loss for loss in losses))

    def test_grafana_unified_simple_reduce_threshold_rule_has_no_multi_query_loss(self):
        rule = _grafana_unified_rule()
        ir = build_alerting_ir_from_grafana_unified(rule)
        losses = record_semantic_losses(ir)
        self.assertFalse(any("Multi-query" in loss for loss in losses))

    def test_grafana_unified_safe_subset_has_no_review_losses(self):
        rule = _grafana_unified_prometheus_safe_rule()
        ir = build_alerting_ir_from_grafana_unified(rule)
        losses = record_semantic_losses(ir)
        self.assertFalse(any("no-data policy" in loss for loss in losses))
        self.assertFalse(any("alert labels" in loss for loss in losses))
        self.assertFalse(any("Dashboard-linked" in loss for loss in losses))

    def test_grafana_unified_safe_static_labels_have_no_label_loss(self):
        rule = _grafana_unified_prometheus_safe_with_labels_rule()
        ir = build_alerting_ir_from_grafana_unified(rule)
        losses = record_semantic_losses(ir)
        self.assertFalse(any("alert labels" in loss for loss in losses))

    def test_grafana_unified_literal_dollar_label_has_no_label_loss(self):
        rule = _grafana_unified_prometheus_safe_rule()
        rule["labels"] = {"cost_center": "cost$center"}
        ir = build_alerting_ir_from_grafana_unified(rule)
        losses = record_semantic_losses(ir)
        self.assertFalse(any("alert labels" in loss for loss in losses))

    def test_grafana_unified_templated_label_reports_loss(self):
        rule = _grafana_unified_prometheus_safe_rule()
        rule["labels"] = {"instance": "{{ $labels.instance }}"}
        ir = build_alerting_ir_from_grafana_unified(rule)
        losses = record_semantic_losses(ir)
        self.assertTrue(any("alert labels" in loss for loss in losses))

    def test_grafana_unified_safe_topk_subset_has_no_review_losses(self):
        rule = _grafana_unified_prometheus_topk_safe_rule()
        ir = build_alerting_ir_from_grafana_unified(rule)
        losses = record_semantic_losses(ir)
        self.assertFalse(any("no-data policy" in loss for loss in losses))
        self.assertFalse(any("alert labels" in loss for loss in losses))
        self.assertFalse(any("Dashboard-linked" in loss for loss in losses))

    def test_grafana_unified_multiple_source_queries_report_multi_query_loss(self):
        rule = _grafana_unified_multi_source_prometheus_rule()
        ir = build_alerting_ir_from_grafana_unified(rule)
        losses = record_semantic_losses(ir)
        self.assertTrue(any("Multi-query" in loss for loss in losses))

    def test_grafana_unified_literal_dashboard_link_reports_loss(self):
        rule = _grafana_unified_rule()
        ir = build_alerting_ir_from_grafana_unified(rule)
        losses = record_semantic_losses(ir)
        self.assertTrue(any("Dashboard-linked" in loss for loss in losses))

    def test_grafana_unified_templated_dashboard_link_reports_loss(self):
        rule = _grafana_unified_rule()
        rule["annotations"]["__dashboardUid__"] = "{{ $dashboard }}"
        ir = build_alerting_ir_from_grafana_unified(rule)
        losses = record_semantic_losses(ir)
        self.assertTrue(any("Dashboard-linked" in loss for loss in losses))

    def test_no_data_policy_loss(self):
        ir = AlertingIR(kind="grafana_legacy", no_data_policy="Alerting")
        losses = record_semantic_losses(ir)
        self.assertTrue(any("no-data policy" in loss for loss in losses))

    def test_no_losses_for_clean_alert(self):
        ir = AlertingIR(kind="grafana_legacy", no_data_policy="", source_extension={"alert_type": "legacy"})
        losses = record_semantic_losses(ir)
        self.assertEqual(len(losses), 0)

    def test_parse_degraded_alert_records_parser_loss(self):
        ir = AlertingIR(kind="datadog_log", metadata={"parse_degraded": True})
        losses = record_semantic_losses(ir)
        self.assertTrue(any("degraded parse" in loss.lower() for loss in losses))


# =====================================================================
# Mapping engine tests
# =====================================================================


class TestSelectTargetRuleType(unittest.TestCase):
    def test_grafana_legacy_without_query_has_no_target(self):
        ir = AlertingIR(kind="grafana_legacy")
        self.assertEqual(select_target_rule_type(ir), "")

    def test_grafana_unified_prometheus_gets_es_query(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        self.assertEqual(select_target_rule_type(ir), ES_QUERY_RULE_TYPE)

    def test_grafana_legacy_prometheus_gets_es_query(self):
        ir = build_alerting_ir_from_grafana(_grafana_legacy_prometheus_alert_task())
        self.assertEqual(select_target_rule_type(ir), ES_QUERY_RULE_TYPE)

    def test_datadog_metric_without_translation_has_no_target(self):
        ir = AlertingIR(kind="datadog_metric")
        self.assertEqual(select_target_rule_type(ir), "")

    def test_datadog_log_without_translation_has_no_target(self):
        ir = AlertingIR(kind="datadog_log")
        self.assertEqual(select_target_rule_type(ir), "")

    def test_datadog_metric_with_translation_gets_es_query(self):
        ir = AlertingIR(
            kind="datadog_metric",
            translated_query="FROM metrics-* | STATS doc_count = COUNT(*)",
            translated_query_provenance="translated_esql",
        )
        self.assertEqual(select_target_rule_type(ir), ES_QUERY_RULE_TYPE)

    def test_manual_only_returns_empty(self):
        ir = AlertingIR(kind="datadog_composite")
        self.assertEqual(select_target_rule_type(ir), "")

    def test_respects_preflight_availability(self):
        ir = AlertingIR(
            kind="datadog_metric",
            translated_query="FROM metrics-* | STATS doc_count = COUNT(*) | WHERE doc_count > 0",
            translated_query_provenance="translated_esql",
        )
        preflight = {"rule_family_availability": {"custom-threshold": False, "es-query": True}}
        self.assertEqual(select_target_rule_type(ir, preflight), ES_QUERY_RULE_TYPE)

    def test_no_target_when_all_unavailable(self):
        ir = AlertingIR(kind="grafana_legacy")
        preflight = {"rule_family_availability": {"es-query": False, "index-threshold": False}}
        self.assertEqual(select_target_rule_type(ir, preflight), "")


class TestBuildRuleParams(unittest.TestCase):
    def test_es_query_params_basic(self):
        ir = AlertingIR(
            evaluation_window="10m",
            translated_query="FROM metrics-* | STATS doc_count = COUNT(*) | WHERE doc_count > 10",
            translated_query_provenance="translated_esql",
        )
        params = build_es_query_rule_params(ir)
        self.assertEqual(params["searchType"], "esqlQuery")
        self.assertEqual(params["timeWindowSize"], 10)
        self.assertEqual(params["timeWindowUnit"], "m")
        self.assertEqual(params["threshold"], [0])
        self.assertEqual(params["thresholdComparator"], ">")

    def test_es_query_params_hours(self):
        ir = AlertingIR(
            evaluation_window="2h",
            translated_query="FROM metrics-* | STATS doc_count = COUNT(*) | WHERE doc_count > 0",
            translated_query_provenance="translated_esql",
        )
        params = build_es_query_rule_params(ir)
        self.assertEqual(params["timeWindowSize"], 2)
        self.assertEqual(params["timeWindowUnit"], "h")

    def test_es_query_params_use_native_promql_for_supported_grafana_prometheus_rules(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        params = build_es_query_rule_params(ir)
        self.assertEqual(params["threshold"], [0])
        self.assertEqual(params["thresholdComparator"], ">")
        self.assertTrue(params["esqlQuery"]["esql"].startswith("PROMQL "))
        self.assertIn("node_cpu_seconds_total", params["esqlQuery"]["esql"])
        self.assertIn("| WHERE value > 80.0", params["esqlQuery"]["esql"])

    def test_es_query_params_use_bounded_where_clause_for_grafana_legacy_within_range(self):
        ir = build_alerting_ir_from_grafana(
            _grafana_legacy_prometheus_range_alert_task("within_range", params=[20, 80]),
        )
        params = build_es_query_rule_params(ir)

        self.assertTrue(params["esqlQuery"]["esql"].startswith("PROMQL "))
        self.assertIn("| WHERE value >= 20.0 AND value <= 80.0", params["esqlQuery"]["esql"])

    def test_es_query_params_use_outside_band_clause_for_grafana_legacy_outside_range(self):
        ir = build_alerting_ir_from_grafana(
            _grafana_legacy_prometheus_range_alert_task("outside_range", params=[20, 80]),
        )
        params = build_es_query_rule_params(ir)

        self.assertTrue(params["esqlQuery"]["esql"].startswith("PROMQL "))
        self.assertIn("| WHERE value < 20.0 OR value > 80.0", params["esqlQuery"]["esql"])

    def test_es_query_params_return_empty_for_unsupported_logql_alert(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_logql_rule(),
            datasource_map={"loki": {"type": "loki", "name": "Loki"}},
        )
        self.assertEqual(build_es_query_rule_params(ir), {})

    def test_index_threshold_params_with_group_by(self):
        ir = AlertingIR(group_by=["host.name"])
        params = build_index_threshold_rule_params(ir)
        self.assertEqual(params["groupBy"], "top")
        self.assertEqual(params["termField"], "host.name")

    def test_custom_threshold_params_with_group_by(self):
        ir = AlertingIR(group_by=["service.name", "host.name"])
        params = build_custom_threshold_rule_params(ir)
        self.assertEqual(params["groupBy"], ["service.name", "host.name"])


class TestMapAlertToKibanaPayload(unittest.TestCase):
    def test_grafana_legacy_without_source_query_is_manual(self):
        task = _grafana_legacy_alert_task()
        ir = build_alerting_ir_from_grafana(task)
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "manual_required")
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_payload"], {})

    def test_grafana_legacy_prometheus_with_source_query_is_valid(self):
        task = _grafana_legacy_prometheus_alert_task()
        ir = build_alerting_ir_from_grafana(task)
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertTrue(result["rule_payload"]["params"]["esqlQuery"]["esql"].startswith("PROMQL "))

    def test_grafana_legacy_prometheus_outside_range_is_valid(self):
        task = _grafana_legacy_prometheus_range_alert_task("outside_range", params=[20, 80])
        ir = build_alerting_ir_from_grafana(task)
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertIn(
            "| WHERE value < 20.0 OR value > 80.0",
            result["rule_payload"]["params"]["esqlQuery"]["esql"],
        )

    def test_grafana_legacy_prometheus_malformed_range_stays_manual(self):
        task = _grafana_legacy_prometheus_range_alert_task("within_range", params=[20])
        ir = build_alerting_ir_from_grafana(task)
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "manual_required")
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_payload"], {})

    def test_grafana_unified_prometheus_with_native_promql_is_review_only(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "draft_requires_review")
        self.assertTrue(result["valid"])
        self.assertEqual(ir.status, AssetStatus.DRAFT_REVIEW)
        self.assertTrue(result["rule_payload"]["params"]["esqlQuery"]["esql"].startswith("PROMQL "))

    def test_grafana_unified_prometheus_safe_subset_is_automated(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_safe_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(ir.status, AssetStatus.TRANSLATED)
        self.assertTrue(result["rule_payload"]["params"]["esqlQuery"]["esql"].startswith("PROMQL "))

    def test_grafana_unified_prometheus_safe_static_labels_are_preserved_as_tags(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_safe_with_labels_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertIn("grafana_label:severity=warning", result["rule_payload"]["tags"])
        self.assertIn("grafana_label:team=infra", result["rule_payload"]["tags"])

    def test_grafana_unified_literal_dollar_label_is_preserved_as_tag(self):
        rule = _grafana_unified_prometheus_safe_rule()
        rule["labels"] = {"cost_center": "cost$center"}
        ir = build_alerting_ir_from_grafana_unified(
            rule,
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["review_gates"]["static_labels"])
        self.assertIn("grafana_label:cost_center=cost$center", result["rule_payload"]["tags"])

    def test_grafana_unified_templated_label_stays_review_only(self):
        rule = _grafana_unified_prometheus_safe_rule()
        rule["labels"] = {"instance": "{{ $labels.instance }}"}
        ir = build_alerting_ir_from_grafana_unified(
            rule,
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "draft_requires_review")
        self.assertFalse(result["review_gates"]["static_labels"])

    def test_grafana_unified_prometheus_safe_topk_subset_is_automated(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_topk_safe_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)
        query = result["rule_payload"]["params"]["esqlQuery"]["esql"]

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("PROMQL index=metrics-prometheus-* step=1m value=(", query)
        self.assertIn('avg by (instance) (rate(node_cpu_seconds_total{mode="user"}[5m]))', query)
        self.assertIn("| STATS value = LAST(value, step) BY instance", query)
        self.assertIn("| SORT value DESC", query)
        self.assertIn("| LIMIT 5", query)
        self.assertIn("| WHERE value > 0.02", query)

    def test_grafana_unified_prometheus_safe_bottomk_subset_is_automated(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_bottomk_safe_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)
        query = result["rule_payload"]["params"]["esqlQuery"]["esql"]

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("PROMQL index=metrics-prometheus-* step=1m value=(", query)
        self.assertIn('avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))', query)
        self.assertIn("| STATS value = LAST(value, step) BY instance", query)
        self.assertIn("| SORT value ASC", query)
        self.assertIn("| LIMIT 5", query)
        self.assertIn("| WHERE value < 0.95", query)

    def test_grafana_unified_loki_is_manual(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_logql_rule(),
            datasource_map={"loki": {"type": "loki", "name": "Loki"}},
        )
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "manual_required")
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_payload"], {})

    def test_grafana_unified_review_gates_identify_no_data_only_blocker(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)

        gates = result["review_gates"]
        self.assertFalse(gates["strict_subset_ready"])
        self.assertFalse(gates["exact_no_data_policy"])
        self.assertTrue(gates["no_data_only_blocks_strict_automation"])
        self.assertTrue(gates["source_faithful_query"])
        self.assertTrue(gates["supported_provenance"])
        self.assertTrue(gates["explicit_threshold"])
        self.assertTrue(gates["single_source_query"])
        self.assertTrue(gates["simple_expression_graph"])
        self.assertTrue(gates["static_labels"])
        self.assertTrue(gates["dashboard_link_safe"])

    def test_grafana_unified_dashboard_annotations_are_preserved_as_tags(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "draft_requires_review")
        self.assertTrue(result["valid"])
        self.assertIn("grafana_dashboard_uid:dash-123", result["rule_payload"]["tags"])
        self.assertIn("grafana_panel_id:42", result["rule_payload"]["tags"])
        self.assertFalse(result["review_gates"]["dashboard_link_safe"])
        self.assertFalse(result["review_gates"]["strict_subset_ready"])

    def test_grafana_unified_safe_dashboard_link_requires_review_but_keeps_tags(self):
        rule = _grafana_unified_prometheus_safe_rule()
        rule["annotations"] = {
            "summary": "CPU is above threshold",
            "__dashboardUid__": "dash-123",
            "__panelId__": "42",
        }
        ir = build_alerting_ir_from_grafana_unified(
            rule,
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "draft_requires_review")
        self.assertTrue(result["valid"])
        self.assertIn("grafana_dashboard_uid:dash-123", result["rule_payload"]["tags"])
        self.assertIn("grafana_panel_id:42", result["rule_payload"]["tags"])
        self.assertFalse(result["review_gates"]["dashboard_link_safe"])
        self.assertFalse(result["review_gates"]["strict_subset_ready"])

    def test_grafana_unified_prometheus_outside_native_boundary_is_manual(self):
        ir = build_alerting_ir_from_grafana_unified(
            _grafana_unified_unsupported_prometheus_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "manual_required")
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_payload"], {})

    def test_manual_composite_monitor(self):
        mon = _datadog_composite_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "manual_required")
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_payload"], {})
        self.assertEqual(ir.status, AssetStatus.MANUAL_REQUIRED)

    def test_preflight_downgrade(self):
        task = _grafana_legacy_alert_task()
        ir = build_alerting_ir_from_grafana(task)
        preflight = {"rule_family_availability": {"es-query": False, "index-threshold": False}}
        result = map_alert_to_kibana_payload(ir, preflight=preflight)
        self.assertFalse(result["valid"])

    def test_datadog_metric_without_translation_is_manual(self):
        ir = build_alerting_ir_from_datadog(_datadog_metric_monitor())
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "manual_required")
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_payload"], {})

    def test_datadog_service_check_manual_reason_names_manual_only_family(self):
        ir = build_alerting_ir_from_datadog(_datadog_service_check_monitor())
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "manual_required")
        self.assertFalse(result["valid"])
        self.assertIn("service check", result["payload_status_reason"].lower())

    def test_datadog_metric_with_explicit_translated_query_is_valid(self):
        ir = build_alerting_ir_from_datadog(_datadog_metric_monitor())
        ir.translated_query = "FROM metrics-* | STATS errors = SUM(pipelines.component_errors_total) | WHERE errors > 0"
        ir.translated_query_provenance = "translated_esql"
        result = map_alert_to_kibana_payload(ir)
        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertEqual(result["rule_payload"]["params"]["esqlQuery"]["esql"], ir.translated_query)

    def test_datadog_metric_with_profile_translation_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_metric_monitor(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertIn("AVG(system.cpu.user.pct)", result["rule_payload"]["params"]["esqlQuery"]["esql"])

    def test_change_query_alert_with_exact_translation_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_change_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertEqual(result["rule_payload"]["params"]["timeWindowSize"], 35)
        self.assertEqual(result["rule_payload"]["params"]["timeWindowUnit"], "m")
        self.assertIn("current_value = AVG(system.cpu.user.pct)", result["rule_payload"]["params"]["esqlQuery"]["esql"])

    def test_datadog_log_with_approximated_index_is_manual(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_log_monitor(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "manual_required")
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_payload"], {})

    def test_datadog_log_with_explicit_index_mapping_is_review_only(self):
        from observability_migration.adapters.source.datadog.field_map import FieldMapProfile

        field_map = FieldMapProfile(
            name="custom",
            logs_index="logs-*",
            log_index_map={"main": "logs-generic-default"},
        )
        ir = build_alerting_ir_from_datadog(_datadog_log_monitor(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "draft_requires_review")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertIn("FROM logs-generic-default", result["rule_payload"]["params"]["esqlQuery"]["esql"])

    def test_datadog_log_measure_monitor_with_warning_free_translation_is_review_only(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_log_measure_monitor(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "draft_requires_review")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertIn("PERCENTILE(duration, 99)", result["rule_payload"]["params"]["esqlQuery"]["esql"])
        self.assertEqual(result["rule_payload"]["params"]["timeWindowSize"], 10)
        self.assertEqual(result["rule_payload"]["params"]["timeWindowUnit"], "m")

    def test_datadog_as_rate_query_with_profile_translation_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_as_rate_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)

    def test_datadog_rollup_query_with_profile_translation_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_rollup_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)

    def test_datadog_default_zero_query_with_profile_translation_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_default_zero_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)

    def test_datadog_formula_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        field_map.metric_map["redis.mem.used"] = "redis.mem.used"
        field_map.metric_map["redis.mem.maxmemory"] = "redis.mem.maxmemory"
        ir = build_alerting_ir_from_datadog(_datadog_formula_ratio_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertIn("q1 = AVG(redis.mem.used)", result["rule_payload"]["params"]["esqlQuery"]["esql"])

    def test_datadog_formula_as_count_error_rate_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        field_map.metric_map["shopist.checkouts.failed"] = "shopist.checkouts.failed_count"
        field_map.metric_map["shopist.checkouts.success"] = "shopist.checkouts.success_count"
        field_map.tag_map["region"] = "cloud.region"
        ir = build_alerting_ir_from_datadog(
            _datadog_formula_as_count_error_rate_query_alert(),
            field_map=field_map,
        )
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertIn("q1 = SUM(shopist.checkouts.failed_count)", result["rule_payload"]["params"]["esqlQuery"]["esql"])

    def test_datadog_shifted_formula_week_before_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        field_map.metric_map["nginx.requests.total_count"] = "nginx.requests.total_count"
        field_map.tag_map["datacenter"] = "cloud.region"
        ir = build_alerting_ir_from_datadog(
            _datadog_shifted_formula_week_before_query_alert(),
            field_map=field_map,
        )
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertEqual(result["rule_payload"]["params"]["timeWindowSize"], 10090)
        self.assertEqual(result["rule_payload"]["params"]["timeWindowUnit"], "m")

    def test_datadog_anomaly_query_alert_is_manual(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_anomaly_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "manual_required")
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_payload"], {})
        self.assertIn("anomaly alert monitors are intentionally manual-only", result["payload_status_reason"])

    def test_datadog_exclude_null_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_exclude_null_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertIn("host.name IS NOT NULL", result["rule_payload"]["params"]["esqlQuery"]["esql"])
        self.assertIn('host.name != "N/A"', result["rule_payload"]["params"]["esqlQuery"]["esql"])

    def test_datadog_calendar_shift_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertEqual(result["rule_payload"]["params"]["timeWindowSize"], 1445)
        self.assertEqual(result["rule_payload"]["params"]["timeWindowUnit"], "m")
        self.assertIn("NOW() - 1 days", result["rule_payload"]["params"]["esqlQuery"]["esql"])

    def test_datadog_calendar_shift_month_query_alert_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_month_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertEqual(result["rule_payload"]["params"]["timeWindowSize"], 46085)
        self.assertEqual(result["rule_payload"]["params"]["timeWindowUnit"], "m")
        self.assertIn("NOW() - 1 month - 5 minutes", result["rule_payload"]["params"]["esqlQuery"]["esql"])

    def test_datadog_calendar_shift_query_alert_with_timezone_is_manual(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_timezone_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "manual_required")
        self.assertFalse(result["valid"])
        self.assertEqual(result["rule_payload"], {})
        self.assertEqual(result["selected_target_rule_type"], "")
        self.assertEqual(result["target_rule_type"], "")
        self.assertFalse(result["payload_emitted"])
        self.assertIn("formula monitor requires manual review", result["payload_status_reason"].lower())
        self.assertNotIn("no source-faithful target query", result["payload_status_reason"].lower())
        self.assertFalse(ir.metadata.get("parse_degraded"))
        self.assertFalse(any("parse degraded" in warning.lower() for warning in ir.warnings))

    def test_datadog_calendar_shift_query_alert_with_stable_offset_timezone_is_automated(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_calendar_shift_no_dst_timezone_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertIn("NOW() - 1 days", result["rule_payload"]["params"]["esqlQuery"]["esql"])

    def test_datadog_exclude_null_rollup_query_alert_is_automated_with_translation(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_exclude_null_rollup_query_alert(), field_map=field_map)
        result = map_alert_to_kibana_payload(ir)

        self.assertEqual(result["automation_tier"], "automated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["rule_payload"]["rule_type_id"], ES_QUERY_RULE_TYPE)
        self.assertEqual(result["selected_target_rule_type"], ES_QUERY_RULE_TYPE)
        self.assertEqual(result["target_rule_type"], ES_QUERY_RULE_TYPE)
        self.assertTrue(result["payload_emitted"])
        self.assertEqual(ir.translated_query_provenance, "translated_esql")
        self.assertIn("host.name IS NOT NULL", ir.translated_query)


class TestMapAlertsBatch(unittest.TestCase):
    def test_batch_summary(self):
        alerts = [
            build_alerting_ir_from_grafana(_grafana_legacy_alert_task()),
            build_alerting_ir_from_grafana_unified(
                _grafana_unified_prometheus_rule(),
                datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
            ),
            build_alerting_ir_from_datadog(_datadog_metric_monitor()),
            build_alerting_ir_from_datadog(_datadog_composite_monitor()),
        ]
        result = map_alerts_batch(alerts)
        self.assertEqual(result["summary"]["total"], 4)
        self.assertIn("draft_requires_review", result["summary"]["by_automation_tier"])
        self.assertIn("manual_required", result["summary"]["by_automation_tier"])
        self.assertTrue(len(result["summary"]["unique_semantic_losses"]) > 0)

    def test_batch_summary_distinguishes_selected_vs_emitted_rule_types(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        alerts = [
            build_alerting_ir_from_datadog(_datadog_exclude_null_query_alert(), field_map=field_map),
            build_alerting_ir_from_datadog(_datadog_exclude_null_rollup_query_alert(), field_map=field_map),
        ]

        result = map_alerts_batch(alerts)

        self.assertEqual(result["summary"]["by_target_rule_type"].get(ES_QUERY_RULE_TYPE), 2)
        self.assertEqual(result["summary"]["by_selected_target_rule_type"].get(ES_QUERY_RULE_TYPE), 2)


# =====================================================================
# Monitor comparison artifact tests
# =====================================================================


class TestDatadogMonitorComparisonArtifact(unittest.TestCase):
    def test_build_monitor_comparison_results_includes_source_translation_and_target(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.report import build_monitor_comparison_results

        raw_monitor = _datadog_as_rate_query_alert()
        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(raw_monitor, field_map=field_map)
        mapping_batch = map_alerts_batch([ir])

        comparison = build_monitor_comparison_results([raw_monitor], [ir], mapping_batch)

        self.assertEqual(comparison["total"], 1)
        row = comparison["monitors"][0]
        self.assertEqual(row["source"]["query"], raw_monitor["query"])
        self.assertEqual(row["source"]["type"], raw_monitor["type"])
        self.assertEqual(row["translation"]["query"], ir.translated_query)
        self.assertEqual(row["translation"]["provenance"], ir.translated_query_provenance)
        self.assertEqual(
            row["target"]["rule_payload"],
            mapping_batch["results"][0]["mapping"]["rule_payload"],
        )
        self.assertEqual(row["target"]["automation_tier"], "automated")
        self.assertFalse(any("approximated" in reason for reason in row["blocked_reasons"]))
        self.assertIn("semantic_losses", row)

    def test_build_monitor_comparison_results_includes_payload_validation(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.report import build_monitor_comparison_results

        raw_monitor = _datadog_log_measure_monitor()
        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(raw_monitor, field_map=field_map)
        mapping_batch = map_alerts_batch([ir])

        comparison = build_monitor_comparison_results(
            [raw_monitor],
            [ir],
            mapping_batch,
            payload_validation_by_alert_id={
                ir.alert_id: {"valid": True, "errors": [], "warnings": ["schema-ok"]}
            },
        )

        row = comparison["monitors"][0]
        self.assertEqual(row["target"]["payload_validation"]["valid"], True)
        self.assertEqual(row["target"]["payload_validation"]["warnings"], ["schema-ok"])

    def test_build_monitor_comparison_results_records_blocked_reasons(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.report import build_monitor_comparison_results
        from observability_migration.core.verification.field_capabilities import FieldCapability

        raw_monitor = _datadog_log_measure_monitor()
        field_map = load_profile("otel")
        field_map.log_field_caps = {
            "service.name": FieldCapability(name="service.name", type="keyword", searchable=True),
            "log.level": FieldCapability(name="log.level", type="keyword", searchable=True),
        }
        field_map.field_caps = dict(field_map.log_field_caps)

        ir = build_alerting_ir_from_datadog(raw_monitor, field_map=field_map)
        mapping_batch = map_alerts_batch([ir])
        comparison = build_monitor_comparison_results([raw_monitor], [ir], mapping_batch)

        row = comparison["monitors"][0]
        self.assertEqual(row["target"]["automation_tier"], "manual_required")
        self.assertTrue(row["blocked_reasons"])
        self.assertTrue(any("duration" in reason for reason in row["blocked_reasons"]))

    def test_build_monitor_comparison_results_includes_parser_diagnostics(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.report import build_monitor_comparison_results

        raw_monitor = _datadog_log_monitor()
        raw_monitor["query"] = (
            'logs("service:checkout AND (status:error OR host:api").index("*").rollup("count").last("5m") > 3'
        )
        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(raw_monitor, field_map=field_map)
        mapping_batch = map_alerts_batch([ir])
        comparison = build_monitor_comparison_results([raw_monitor], [ir], mapping_batch)

        row = comparison["monitors"][0]
        self.assertTrue(row["translation"]["parse_degraded"])
        codes = {
            str(item.get("code", ""))
            for item in row["translation"]["parser_diagnostics"]
            if isinstance(item, dict)
        }
        self.assertIn("LOG_BOOLEAN_FALLBACK", codes)

    def test_build_monitor_comparison_results_uses_service_check_block_reason(self):
        from observability_migration.adapters.source.datadog.report import build_monitor_comparison_results

        raw_monitor = _datadog_service_check_monitor()
        ir = build_alerting_ir_from_datadog(raw_monitor)
        mapping_batch = map_alerts_batch([ir])
        comparison = build_monitor_comparison_results([raw_monitor], [ir], mapping_batch)

        row = comparison["monitors"][0]
        self.assertTrue(row["blocked_reasons"])
        self.assertTrue(any("service check" in reason.lower() for reason in row["blocked_reasons"]))

    def test_build_monitor_comparison_results_exposes_selected_vs_emitted_rule_type(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.report import build_monitor_comparison_results

        raw_monitor = _datadog_exclude_null_rollup_query_alert()
        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(raw_monitor, field_map=field_map)
        mapping_batch = map_alerts_batch([ir])

        comparison = build_monitor_comparison_results([raw_monitor], [ir], mapping_batch)

        row = comparison["monitors"][0]
        self.assertEqual(row["target"]["selected_target_rule_type"], ES_QUERY_RULE_TYPE)
        self.assertEqual(row["target"]["target_rule_type"], ES_QUERY_RULE_TYPE)
        self.assertTrue(row["target"]["payload_emitted"])
        self.assertFalse(any("No suitable target rule type" in reason for reason in row["blocked_reasons"]))


class TestDatadogMonitorMigrationResultsArtifact(unittest.TestCase):
    def test_build_monitor_migration_results_includes_emitted_and_selected_rule_type_counts(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.report import build_monitor_migration_results

        field_map = load_profile("elastic_agent")
        irs = [
            build_alerting_ir_from_datadog(_datadog_exclude_null_query_alert(), field_map=field_map),
            build_alerting_ir_from_datadog(_datadog_exclude_null_rollup_query_alert(), field_map=field_map),
        ]
        map_alerts_batch(irs)

        results = build_monitor_migration_results(irs)

        self.assertEqual(results["by_target_rule_type"].get("es-query"), 2)
        self.assertEqual(results["by_selected_target_rule_type"].get("es-query"), 2)
        translated = next(row for row in results["monitors"] if row["name"] == "CPU exclude_null rollup wrapper")
        self.assertEqual(translated["target_rule_type"], "es-query")
        self.assertEqual(translated["selected_target_rule_type"], "es-query")
        self.assertEqual(translated["payload_emitted"], True)


# =====================================================================
# Grafana alert comparison artifact tests
# =====================================================================


class TestGrafanaAlertComparisonArtifact(unittest.TestCase):
    def test_build_alert_comparison_results_includes_native_promql_payload(self):
        from observability_migration.adapters.source.grafana.alerts import build_alert_comparison_results

        raw_rule = _grafana_unified_prometheus_rule()
        datasource_map = {"prometheus": {"type": "prometheus", "name": "Prometheus"}}
        ir = build_alerting_ir_from_grafana_unified(raw_rule, datasource_map=datasource_map)
        mapping_batch = map_alerts_batch([ir])

        comparison = build_alert_comparison_results([raw_rule], [ir], mapping_batch)

        self.assertEqual(comparison["total"], 1)
        row = comparison["alerts"][0]
        self.assertEqual(row["source"]["query"], raw_rule["data"][0]["model"]["expr"])
        self.assertEqual(row["translation"]["provenance"], "native_promql")
        self.assertTrue(row["translation"]["query"].startswith("PROMQL "))
        self.assertEqual(row["target"]["automation_tier"], "draft_requires_review")

    def test_build_alert_comparison_results_records_manual_block_reason(self):
        from observability_migration.adapters.source.grafana.alerts import build_alert_comparison_results

        raw_rule = _grafana_unified_logql_rule()
        datasource_map = {"loki": {"type": "loki", "name": "Loki"}}
        ir = build_alerting_ir_from_grafana_unified(raw_rule, datasource_map=datasource_map)
        mapping_batch = map_alerts_batch([ir])

        comparison = build_alert_comparison_results([raw_rule], [ir], mapping_batch)

        row = comparison["alerts"][0]
        self.assertEqual(row["target"]["automation_tier"], "manual_required")
        self.assertTrue(row["blocked_reasons"])
        self.assertTrue(any("No source-faithful target query" in reason for reason in row["blocked_reasons"]))

    def test_build_alert_comparison_results_includes_review_gates(self):
        from observability_migration.adapters.source.grafana.alerts import build_alert_comparison_results

        raw_rule = _grafana_unified_prometheus_rule()
        datasource_map = {"prometheus": {"type": "prometheus", "name": "Prometheus"}}
        ir = build_alerting_ir_from_grafana_unified(raw_rule, datasource_map=datasource_map)
        mapping_batch = map_alerts_batch([ir])

        comparison = build_alert_comparison_results([raw_rule], [ir], mapping_batch)

        row = comparison["alerts"][0]
        self.assertIn("review_gates", row["target"])
        self.assertFalse(row["target"]["review_gates"]["strict_subset_ready"])
        self.assertFalse(row["target"]["review_gates"]["exact_no_data_policy"])
        self.assertTrue(row["target"]["review_gates"]["no_data_only_blocks_strict_automation"])


class TestGrafanaAlertMigrationResultsArtifact(unittest.TestCase):
    def test_build_alert_migration_results_includes_emitted_and_selected_rule_type_counts(self):
        from observability_migration.adapters.source.grafana.alerts import build_alert_migration_results

        emitted = build_alerting_ir_from_grafana_unified(
            _grafana_unified_prometheus_rule(),
            datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
        )
        blocked = build_alerting_ir_from_grafana_unified(
            _grafana_unified_logql_rule(),
            datasource_map={"loki": {"type": "loki", "name": "Loki"}},
        )
        alerts = [emitted, blocked]
        map_alerts_batch(alerts)

        results = build_alert_migration_results(
            alerts,
            total_alerts=len(alerts),
            total_legacy=0,
            total_unified=len(alerts),
        )

        self.assertEqual(results["by_target_rule_type"].get("es-query"), 1)
        self.assertEqual(results["by_selected_target_rule_type"].get("es-query"), 1)
        blocked_row = next(row for row in results["alerts"] if row["name"] == blocked.name)
        self.assertEqual(blocked_row["target_rule_type"], "")
        self.assertEqual(blocked_row["selected_target_rule_type"], "")
        self.assertEqual(blocked_row["payload_emitted"], False)


# =====================================================================
# Alert support report coverage contract tests
# =====================================================================


class TestAlertSupportReportCoverageContract(unittest.TestCase):
    def _build_grafana_example_comparison(self):
        from observability_migration.adapters.source.grafana.alerts import (
            build_alert_comparison_results,
            build_alert_migration_tasks,
            extract_alerts_from_dashboard,
        )
        from observability_migration.adapters.source.grafana.extract import (
            extract_all_alerting_resources_from_files,
            extract_dashboards_from_files,
        )

        examples_dir = Path(__file__).resolve().parents[1] / "examples" / "alerting" / "grafana"
        dashboards = extract_dashboards_from_files(str(examples_dir))
        legacy_tasks = []
        for dashboard in dashboards:
            legacy_tasks.extend(build_alert_migration_tasks(extract_alerts_from_dashboard(dashboard)))

        unified = extract_all_alerting_resources_from_files(str(examples_dir))
        datasource_map = unified.get("datasources", {}) or {}
        raw_inputs = list(legacy_tasks) + list(unified.get("alert_rules", []) or [])
        irs = [build_alerting_ir_from_grafana(task) for task in legacy_tasks]
        irs.extend(
            build_alerting_ir_from_grafana_unified(rule, datasource_map=datasource_map)
            for rule in (unified.get("alert_rules", []) or [])
        )
        mapping_batch = map_alerts_batch(irs)
        return build_alert_comparison_results(raw_inputs, irs, mapping_batch)

    def _build_datadog_example_comparison(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.report import build_monitor_comparison_results

        monitors_path = Path(__file__).resolve().parents[1] / "examples" / "alerting" / "monitors" / "datadog_monitors.json"
        profile_path = Path(__file__).resolve().parents[1] / "examples" / "datadog-field-profile.example.yaml"
        raw_monitors = json.loads(monitors_path.read_text(encoding="utf-8"))
        field_map = load_profile(str(profile_path))
        irs = [build_alerting_ir_from_datadog(monitor, field_map=field_map) for monitor in raw_monitors]
        mapping_batch = map_alerts_batch(irs)
        return build_monitor_comparison_results(raw_monitors, irs, mapping_batch)

    def test_support_summary_exposes_grouped_and_detailed_family_breakdowns_for_both_sources(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        for source_name in ("grafana", "datadog"):
            self.assertIn("grouped_family_breakdown", summary[source_name])
            self.assertIn("detailed_family_breakdown", summary[source_name])
            self.assertIn("missing_expected_detailed_families", summary[source_name])
            self.assertIn("parser_diagnostic_breakdown", summary[source_name])
            self.assertEqual(summary[source_name]["missing_expected_detailed_families"], [], source_name)

    def test_support_summary_rolls_up_parser_diagnostic_codes(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            {"total": 0, "summary": {"by_automation_tier": {}}, "alerts": []},
            {
                "total": 1,
                "summary": {"by_automation_tier": {"manual_required": 1}},
                "monitors": [
                    {
                        "name": "broken log monitor",
                        "kind": "datadog_log",
                        "source": {"query": "service:web AND (status:error OR host:api"},
                        "translation": {
                            "warnings": ["Datadog log search parse degraded; manual review required"],
                            "parser_diagnostics": [
                                {
                                    "code": "LOG_BOOLEAN_FALLBACK",
                                    "message": "Datadog log parser used boolean fallback and may alter precedence",
                                    "degraded": True,
                                }
                            ],
                        },
                        "target": {"automation_tier": "manual_required"},
                        "semantic_losses": [],
                        "blocked_reasons": [],
                    }
                ],
            },
        )

        self.assertEqual(summary["datadog"]["parser_diagnostic_breakdown"][0]["code"], "LOG_BOOLEAN_FALLBACK")
        self.assertEqual(summary["datadog"]["parser_diagnostic_breakdown"][0]["count"], 1)

    def test_support_summary_marks_formula_family_with_supported_datadog_case(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        self.assertTrue(
            any(
                row["family"] == "Formula-style metric/query alerts"
                and row["automation_tier"] == "automated"
                for row in summary["datadog"]["detailed_family_breakdown"]
            )
        )

    def test_support_summary_marks_shifted_formula_family_with_supported_datadog_case(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        self.assertTrue(
            any(
                row["family"] == "Shifted formula metric/query alerts"
                and row["automation_tier"] == "automated"
                for row in summary["datadog"]["detailed_family_breakdown"]
            )
        )

    def test_support_summary_removes_exclude_null_manual_warning_case(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        self.assertFalse(
            any(
                row["family"] == "Metric/query alerts with exclude_null()"
                and row["automation_tier"] == "manual_required"
                for row in summary["datadog"]["detailed_family_breakdown"]
            )
        )

    def test_support_summary_marks_exclude_null_family_with_supported_datadog_case(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        self.assertTrue(
            any(
                row["family"] == "Metric/query alerts with exclude_null()"
                and row["automation_tier"] == "automated"
                for row in summary["datadog"]["detailed_family_breakdown"]
            )
        )

    def test_support_summary_keeps_manual_calendar_shift_boundary_visible(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        self.assertTrue(
            any(
                row["family"] == "Shifted formula metric/query alerts"
                and row["automation_tier"] == "manual_required"
                for row in summary["datadog"]["detailed_family_breakdown"]
            )
        )

    def test_support_summary_shows_multiple_automated_legacy_dashboard_alerts(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        matching_rows = [
            row
            for row in summary["grafana"]["detailed_family_breakdown"]
            if row["family"] == "Legacy dashboard alerts" and row["automation_tier"] == "automated"
        ]
        self.assertTrue(matching_rows)
        self.assertGreater(matching_rows[0]["count"], 1)

    def test_support_summary_marks_prometheus_native_promql_with_automated_case(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        self.assertTrue(
            any(
                row["family"] == "Prometheus native PromQL"
                and row["automation_tier"] == "automated"
                for row in summary["grafana"]["detailed_family_breakdown"]
            )
        )

    def test_support_summary_includes_automated_prometheus_native_promql_safe_labels_case(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        matching_rows = [
            row
            for row in summary["grafana"]["detailed_family_breakdown"]
            if row["family"] == "Prometheus native PromQL" and row["automation_tier"] == "automated"
        ]
        self.assertTrue(matching_rows)
        self.assertGreater(matching_rows[0]["count"], 1)

    def test_support_summary_marks_promql_topk_and_bottomk_with_automated_cases(self):
        from scripts.generate_alert_support_report import build_support_summary

        summary = build_support_summary(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        self.assertTrue(
            any(
                row["family"] == "PromQL topk()"
                and row["automation_tier"] == "automated"
                for row in summary["grafana"]["detailed_family_breakdown"]
            )
        )
        self.assertTrue(
            any(
                row["family"] == "PromQL bottomk()"
                and row["automation_tier"] == "automated"
                for row in summary["grafana"]["detailed_family_breakdown"]
            )
        )

    def test_rendered_report_includes_detailed_family_coverage_for_both_sources(self):
        from scripts.generate_alert_support_report import render_markdown_report

        markdown = render_markdown_report(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        self.assertIn("## Detailed Family Coverage", markdown)
        self.assertIn("### Grafana", markdown)
        self.assertIn("### Datadog", markdown)
        self.assertIn("Change query alerts", markdown)
        self.assertIn("Shifted formula metric/query alerts", markdown)
        self.assertIn("PromQL topk()", markdown)
        self.assertIn("APM monitors", markdown)

    def test_rendered_report_distinguishes_selected_vs_emitted_rule_type(self):
        from scripts.generate_alert_support_report import render_markdown_report

        markdown = render_markdown_report(
            self._build_grafana_example_comparison(),
            self._build_datadog_example_comparison(),
        )

        self.assertIn("#### `CPU exclude_null rollup wrapper`", markdown)
        self.assertIn("- Selected rule type: `.es-query`", markdown)
        self.assertIn("- Emitted rule type: `none`", markdown)
        self.assertIn("- Payload emitted: `no`", markdown)

    def test_support_report_reason_summary_calls_out_no_data_only_grafana_review_case(self):
        from scripts.generate_alert_support_report import _reason_summary

        comparison = self._build_grafana_example_comparison()
        row = next(item for item in comparison["alerts"] if item["name"] == "High CPU usage")

        self.assertIn(
            "only exact no-data parity blocks automated promotion",
            _reason_summary(row).lower(),
        )

    def test_prepare_datadog_input_dir_synthesizes_placeholder_dashboard(self):
        import scripts.generate_alert_support_report as report

        with tempfile.TemporaryDirectory() as tmpdir:
            generated_dir = Path(tmpdir) / "generated"
            monitors_path = Path(tmpdir) / "datadog_monitors.json"
            monitors_path.write_text("[]", encoding="utf-8")

            with (
                patch.object(report, "GENERATED_DIR", generated_dir),
                patch.object(report, "DATADOG_MONITORS_FILE", monitors_path),
            ):
                staging_dir = report._prepare_datadog_input_dir()
            self.assertEqual(staging_dir, generated_dir / "_staging" / "datadog")
            support_dashboard = json.loads((staging_dir / "support_dashboard.json").read_text(encoding="utf-8"))
            self.assertEqual(support_dashboard["title"], "Datadog Alert Support Fixtures")
            self.assertEqual(support_dashboard["widgets"], [])
            copied_monitors = json.loads(
                (staging_dir / "monitors" / "datadog_monitors.json").read_text(encoding="utf-8")
            )
            self.assertEqual(copied_monitors, [])


# =====================================================================
# Monitor verification tests
# =====================================================================


class TestDatadogMonitorVerification(unittest.TestCase):
    def test_validate_monitor_queries_uses_validator_for_translated_monitors(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.verification import validate_monitor_queries

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_log_measure_monitor(), field_map=field_map)

        def _fake_validate(query, es_url, resolver, max_attempts=8, es_api_key=None, verify=True):
            return {
                "status": "pass",
                "query": query,
                "error": "",
                "analysis": {"target_index": "logs-*", "result_rows": 3, "result_columns": ["value"]},
                "fix_attempts": [],
            }

        records = validate_monitor_queries(
            [ir],
            es_url="http://example-elasticsearch:9200",
            validate_query_fn=_fake_validate,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["alert_id"], ir.alert_id)
        self.assertEqual(records[0]["status"], "pass")
        self.assertEqual(records[0]["query"], ir.translated_query)

    def test_build_monitor_verification_lookup_includes_target_execution(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.verification import (
            build_monitor_verification_lookup,
        )

        field_map = load_profile("otel")
        ir = build_alerting_ir_from_datadog(_datadog_log_measure_monitor(), field_map=field_map)
        lookup = build_monitor_verification_lookup(
            [ir],
            [
                {
                    "alert_id": ir.alert_id,
                    "status": "pass",
                    "query": ir.translated_query,
                    "error": "",
                    "analysis": {
                        "target_index": "logs-*",
                        "result_rows": 2,
                        "result_columns": ["value"],
                        "result_values": [[42]],
                    },
                    "fix_attempts": [],
                }
            ],
        )

        self.assertIn(ir.alert_id, lookup)
        self.assertEqual(lookup[ir.alert_id]["validation"]["status"], "pass")
        self.assertEqual(lookup[ir.alert_id]["target_execution"]["status"], "pass")
        self.assertEqual(lookup[ir.alert_id]["target_execution"]["target_index"], "logs-*")

# =====================================================================
# AssetStatus tests
# =====================================================================


class TestAssetStatusDraftReview(unittest.TestCase):
    def test_draft_review_exists(self):
        self.assertEqual(AssetStatus.DRAFT_REVIEW.value, "draft_review")

    def test_draft_review_between_translated_and_manual(self):
        values = list(AssetStatus)
        translated_idx = values.index(AssetStatus.TRANSLATED_WITH_WARNINGS)
        draft_idx = values.index(AssetStatus.DRAFT_REVIEW)
        manual_idx = values.index(AssetStatus.MANUAL_REQUIRED)
        self.assertTrue(translated_idx < draft_idx < manual_idx)


# =====================================================================
# AlertingIR serialization tests
# =====================================================================


class TestAlertingIRSerialization(unittest.TestCase):
    def test_to_dict_includes_new_fields(self):
        ir = AlertingIR(
            alert_id="test-1",
            name="Test Alert",
            kind="grafana_legacy",
            automation_tier="automated",
            target_rule_type="es-query",
            selected_target_rule_type="es-query",
            payload_emitted=True,
            payload_status="emitted",
            schedule_interval="1m",
            translated_query="FROM metrics-* | WHERE cpu > 90",
            translated_query_provenance="translated_esql",
        )
        d = ir.to_dict()
        self.assertEqual(d["automation_tier"], "automated")
        self.assertEqual(d["target_rule_type"], "es-query")
        self.assertEqual(d["selected_target_rule_type"], "es-query")
        self.assertEqual(d["payload_emitted"], True)
        self.assertEqual(d["payload_status"], "emitted")
        self.assertEqual(d["schedule_interval"], "1m")
        self.assertEqual(d["translated_query"], "FROM metrics-* | WHERE cpu > 90")
        self.assertEqual(d["translated_query_provenance"], "translated_esql")
        self.assertEqual(d["status"], "manual_required")

    def test_to_dict_round_trip(self):
        mon = _datadog_metric_monitor()
        ir = build_alerting_ir_from_datadog(mon)
        d = ir.to_dict()
        serialized = json.dumps(d)
        deserialized = json.loads(serialized)
        self.assertEqual(deserialized["kind"], "datadog_metric")
        self.assertEqual(deserialized["alert_id"], "12345")

    def test_to_dict_after_mapping_distinguishes_selected_vs_emitted_rule_type(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile

        field_map = load_profile("elastic_agent")
        ir = build_alerting_ir_from_datadog(_datadog_exclude_null_rollup_query_alert(), field_map=field_map)
        map_alert_to_kibana_payload(ir)

        d = ir.to_dict()
        self.assertEqual(d["selected_target_rule_type"], "es-query")
        self.assertEqual(d["target_rule_type"], "es-query")
        self.assertEqual(d["payload_emitted"], True)
        self.assertEqual(d["payload_status"], "emitted")


# =====================================================================
# Grafana extract helper tests
# =====================================================================


class TestGrafanaExtractSession(unittest.TestCase):
    def test_session_with_basic_auth(self):
        from observability_migration.adapters.source.grafana.extract import _grafana_session
        session = _grafana_session("http://grafana:3000", user="admin", password="secret")
        self.assertIsNotNone(session.auth)

    def test_session_with_token(self):
        from observability_migration.adapters.source.grafana.extract import _grafana_session
        session = _grafana_session("http://grafana:3000", token="my-token")
        self.assertIn("Authorization", session.headers)
        self.assertIn("Bearer", session.headers["Authorization"])

    def test_session_token_takes_precedence(self):
        from observability_migration.adapters.source.grafana.extract import _grafana_session
        session = _grafana_session("http://grafana:3000", user="admin", password="pass", token="tok")
        self.assertIn("Bearer", session.headers.get("Authorization", ""))


class TestGrafanaExtractAllAlertingResources(unittest.TestCase):
    @patch("observability_migration.adapters.source.grafana.extract._fetch_unified_provisioning_json")
    @patch("observability_migration.adapters.source.grafana.extract._grafana_session")
    def test_returns_all_keys(self, mock_session, mock_fetch):
        mock_fetch.return_value = []
        result = {}
        from observability_migration.adapters.source.grafana.extract import extract_all_alerting_resources
        result = extract_all_alerting_resources("http://grafana:3000")
        self.assertIn("alert_rules", result)
        self.assertIn("contact_points", result)
        self.assertIn("notification_policies", result)
        self.assertIn("mute_timings", result)
        self.assertIn("templates", result)
        self.assertIn("datasources", result)

    def test_loads_unified_resources_from_files(self):
        from observability_migration.adapters.source.grafana.extract import extract_all_alerting_resources_from_files

        with tempfile.TemporaryDirectory() as tmpdir:
            alerts_dir = Path(tmpdir) / "alerts"
            alerts_dir.mkdir()
            (alerts_dir / "grafana_alert_rules.json").write_text(
                json.dumps([_grafana_unified_prometheus_rule(), _grafana_unified_logql_rule()]),
                encoding="utf-8",
            )
            (alerts_dir / "grafana_datasources.json").write_text(
                json.dumps(
                    [
                        {"uid": "prometheus", "type": "prometheus", "name": "Prometheus"},
                        {"uid": "loki", "type": "loki", "name": "Loki"},
                    ]
                ),
                encoding="utf-8",
            )
            result = extract_all_alerting_resources_from_files(tmpdir)

        self.assertEqual(len(result["alert_rules"]), 2)
        self.assertEqual(result["datasources"]["prometheus"]["type"], "prometheus")
        self.assertEqual(result["datasources"]["loki"]["name"], "Loki")
        self.assertEqual(result["contact_points"], [])


# =====================================================================
# filter_unified_alert_rules tests
# =====================================================================


class TestFilterUnifiedAlertRules(unittest.TestCase):
    def setUp(self):
        from observability_migration.adapters.source.grafana.extract import filter_unified_alert_rules
        self.filter = filter_unified_alert_rules
        self.rules = [
            {"uid": "rule-1", "folderUID": "folder-a", "title": "Alpha"},
            {"uid": "rule-2", "folderUID": "folder-a", "title": "Beta"},
            {"uid": "rule-3", "folderUID": "folder-b", "title": "Gamma"},
        ]

    def test_no_filters_returns_all(self):
        result = self.filter(self.rules)
        self.assertEqual(result, self.rules)

    def test_filter_by_single_uid(self):
        result = self.filter(self.rules, uids=["rule-1"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["uid"], "rule-1")

    def test_filter_by_multiple_uids(self):
        result = self.filter(self.rules, uids=["rule-1", "rule-3"])
        self.assertEqual([r["uid"] for r in result], ["rule-1", "rule-3"])

    def test_filter_by_folder(self):
        result = self.filter(self.rules, folder_uids=["folder-a"])
        self.assertEqual(len(result), 2)
        self.assertTrue(all(r["folderUID"] == "folder-a" for r in result))

    def test_filter_by_uid_and_folder_intersection(self):
        result = self.filter(self.rules, uids=["rule-1", "rule-2"], folder_uids=["folder-a"])
        self.assertEqual([r["uid"] for r in result], ["rule-1", "rule-2"])

    def test_uid_and_folder_no_intersection(self):
        result = self.filter(self.rules, uids=["rule-3"], folder_uids=["folder-a"])
        self.assertEqual(result, [])

    def test_unknown_uid_returns_empty(self):
        result = self.filter(self.rules, uids=["does-not-exist"])
        self.assertEqual(result, [])

    def test_empty_input_returns_empty(self):
        result = self.filter([], uids=["rule-1"])
        self.assertEqual(result, [])

    def test_non_dict_entries_skipped(self):
        rules_with_garbage = self.rules + ["not-a-dict", None]  # type: ignore[list-item]
        result = self.filter(rules_with_garbage, uids=["rule-1"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["uid"], "rule-1")


# =====================================================================
# Datadog monitor file extraction tests
# =====================================================================


class TestDatadogMonitorFileExtraction(unittest.TestCase):
    def test_single_monitor_file(self):
        from observability_migration.adapters.source.datadog.extract import extract_monitors_from_files
        with tempfile.TemporaryDirectory() as tmpdir:
            mon = _datadog_metric_monitor()
            path = Path(tmpdir) / "monitor.json"
            path.write_text(json.dumps(mon))
            result = extract_monitors_from_files(tmpdir)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["name"], "High CPU on web hosts")
            self.assertIn("_source_file", result[0])

    def test_array_of_monitors(self):
        from observability_migration.adapters.source.datadog.extract import extract_monitors_from_files
        with tempfile.TemporaryDirectory() as tmpdir:
            monitors = [_datadog_metric_monitor(), _datadog_log_monitor()]
            path = Path(tmpdir) / "monitors.json"
            path.write_text(json.dumps(monitors))
            result = extract_monitors_from_files(tmpdir)
            self.assertEqual(len(result), 2)

    def test_wrapped_monitors(self):
        from observability_migration.adapters.source.datadog.extract import extract_monitors_from_files
        with tempfile.TemporaryDirectory() as tmpdir:
            wrapped = {"monitors": [_datadog_metric_monitor()]}
            path = Path(tmpdir) / "export.json"
            path.write_text(json.dumps(wrapped))
            result = extract_monitors_from_files(tmpdir)
            self.assertEqual(len(result), 1)

    def test_missing_directory(self):
        from observability_migration.adapters.source.datadog.extract import extract_monitors_from_files
        with self.assertRaises(FileNotFoundError):
            extract_monitors_from_files("/nonexistent/path")

    def test_json_safe_api_value_converts_datetimes(self):
        from datetime import date, datetime

        from observability_migration.adapters.source.datadog.extract import _json_safe_api_value

        raw = {
            "updated_at": datetime(2026, 4, 1, 12, 34, 56),
            "nested": [{"date": date(2026, 4, 1)}],
        }

        normalized = _json_safe_api_value(raw)

        self.assertEqual(normalized["updated_at"], "2026-04-01T12:34:56")
        self.assertEqual(normalized["nested"][0]["date"], "2026-04-01")
        json.dumps(normalized)


class TestDatadogAlertPipeline(unittest.TestCase):
    def test_run_alert_pipeline_writes_alert_artifacts_to_output_dir(self):
        from observability_migration.adapters.source.datadog.alert_pipeline import (
            run_alert_pipeline,
        )
        from observability_migration.adapters.source.datadog.field_map import load_profile

        with tempfile.TemporaryDirectory() as tmpdir:
            monitors_dir = Path(tmpdir) / "monitors"
            monitors_dir.mkdir()
            (monitors_dir / "monitor.json").write_text(
                json.dumps(_datadog_metric_monitor()),
                encoding="utf-8",
            )
            output_dir = Path(tmpdir) / "alerts"

            summary = run_alert_pipeline(
                SimpleNamespace(
                    source="files",
                    input_dir=tmpdir,
                    data_view="metrics-*",
                    validate=False,
                    es_url="",
                    es_api_key="",
                    kibana_url="",
                    kibana_api_key="",
                    space_id="",
                    create_alert_rules=False,
                    monitor_ids="",
                    monitor_query="",
                ),
                field_map=load_profile("otel"),
                output_dir=output_dir,
                dd_creds={},
            )
            self.assertEqual(summary["total"], 1)
            self.assertEqual(summary["artifacts_dir"], str(output_dir))
            self.assertTrue((output_dir / "raw_monitors" / "datadog_monitors.json").exists())
            self.assertTrue((output_dir / "monitor_migration_results.json").exists())
            self.assertTrue((output_dir / "monitor_comparison_results.json").exists())
            self.assertTrue((output_dir / "monitor_verification_results.json").exists())

    def test_alerting_preflight_threads_tls_verify(self):
        from observability_migration.adapters.source.datadog import alert_pipeline

        args = SimpleNamespace(
            preflight=True,
            kibana_url="https://kibana.example",
            kibana_api_key="secret",
            space_id="space-a",
            ca_cert="/tmp/ca.pem",
            insecure=False,
        )
        mapping_batch = {"results": []}

        with patch(
            "observability_migration.adapters.source.datadog.alert_pipeline.run_alerting_preflight",
            return_value={},
        ) as mock_preflight:
            alert_pipeline.build_payload_validation_lookup(args, mapping_batch)

        mock_preflight.assert_called_once()
        self.assertEqual(mock_preflight.call_args.kwargs.get("verify"), "/tmp/ca.pem")

    def test_create_rules_threads_tls_verify(self):
        from observability_migration.adapters.source.datadog import alert_pipeline

        args = SimpleNamespace(
            create_alert_rules=True,
            kibana_url="https://kibana.example",
            kibana_api_key="secret",
            space_id="space-a",
            ca_cert="/tmp/ca.pem",
            insecure=False,
        )
        upload_result = {
            "summary": {"created": 0, "failed": 0, "skipped": 0},
            "failed": [],
            "preflight_unreachable": False,
        }

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "observability_migration.adapters.source.datadog.alert_pipeline.create_rules_from_payloads",
            return_value=upload_result,
        ) as mock_create:
            alert_pipeline.create_rules_if_requested(
                args=args,
                output_dir=Path(tmpdir),
                mapping_batch={"results": []},
                payload_preflight={},
            )

        mock_create.assert_called_once()
        self.assertEqual(mock_create.call_args.kwargs.get("verify"), "/tmp/ca.pem")


class TestGrafanaAlertPipeline(unittest.TestCase):
    def test_run_alert_pipeline_writes_alert_artifacts_to_output_dir(self):
        from observability_migration.adapters.source.grafana.alert_pipeline import (
            run_alert_pipeline,
        )

        dashboard = {
            "title": "Alerts",
            "uid": "grafana-uid-1",
            "panels": [
                {
                    "id": 101,
                    "title": "CPU Alert",
                    "alert": {
                        "name": "CPU High",
                        "conditions": [
                            {
                                "evaluator": {"type": "gt", "params": [80]},
                                "reducer": {"type": "avg"},
                                "query": {"params": ["A", "5m", "now"]},
                                "operator": {"type": "and"},
                            }
                        ],
                    },
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "alerts"

            summary = run_alert_pipeline(
                SimpleNamespace(
                    source="files",
                    input_dir=tmpdir,
                    data_view="metrics-*",
                    grafana_token="",
                    kibana_url="",
                    kibana_api_key="",
                    space_id="",
                    create_alert_rules=False,
                ),
                output_dir=output_dir,
                raw_dashboards=[dashboard],
            )

            self.assertEqual(summary["total"], 1)
            self.assertTrue((output_dir / "raw_alerts" / "grafana_dashboards.json").exists())
            self.assertTrue((output_dir / "alert_migration_results.json").exists())
            self.assertTrue((output_dir / "alert_comparison_results.json").exists())

    def test_api_unified_alert_extraction_uses_grafana_pass_and_tls_verify(self):
        from observability_migration.adapters.source.grafana import alert_pipeline

        args = SimpleNamespace(
            source="api",
            grafana_url="https://grafana.example",
            grafana_user="user-a",
            grafana_pass="pass-a",
            grafana_token="token-a",
            ca_cert="/tmp/ca.pem",
            insecure=False,
        )

        with patch(
            "observability_migration.adapters.source.grafana.alert_pipeline.extract_all_alerting_resources",
            return_value={"alert_rules": []},
        ) as mock_extract:
            alert_pipeline.load_unified_alerting_resources(args)

        mock_extract.assert_called_once_with(
            "https://grafana.example",
            user="user-a",
            password="pass-a",
            token="token-a",
            verify="/tmp/ca.pem",
        )

    def test_load_unified_alerting_resources_filters_by_alert_uids(self):
        from observability_migration.adapters.source.grafana import alert_pipeline

        rules = [
            {"uid": "rule-1", "folderUID": "folder-a", "title": "Keep"},
            {"uid": "rule-2", "folderUID": "folder-a", "title": "Skip"},
        ]
        args = SimpleNamespace(
            source="files",
            input_dir="",
            grafana_token="",
            alert_uids="rule-1",
            alert_folder="",
        )
        with patch(
            "observability_migration.adapters.source.grafana.alert_pipeline.extract_all_alerting_resources_from_files",
            return_value={"alert_rules": rules},
        ):
            result = alert_pipeline.load_unified_alerting_resources(args)

        self.assertEqual(len(result["alert_rules"]), 1)
        self.assertEqual(result["alert_rules"][0]["uid"], "rule-1")

    def test_load_unified_alerting_resources_filters_by_alert_folder(self):
        from observability_migration.adapters.source.grafana import alert_pipeline

        rules = [
            {"uid": "rule-1", "folderUID": "folder-a", "title": "Keep"},
            {"uid": "rule-2", "folderUID": "folder-b", "title": "Skip"},
        ]
        args = SimpleNamespace(
            source="files",
            input_dir="",
            grafana_token="",
            alert_uids="",
            alert_folder="folder-a",
        )
        with patch(
            "observability_migration.adapters.source.grafana.alert_pipeline.extract_all_alerting_resources_from_files",
            return_value={"alert_rules": rules},
        ):
            result = alert_pipeline.load_unified_alerting_resources(args)

        self.assertEqual(len(result["alert_rules"]), 1)
        self.assertEqual(result["alert_rules"][0]["folderUID"], "folder-a")

    def test_load_unified_alerting_resources_no_filter_returns_all(self):
        from observability_migration.adapters.source.grafana import alert_pipeline

        rules = [
            {"uid": "rule-1", "folderUID": "folder-a", "title": "A"},
            {"uid": "rule-2", "folderUID": "folder-b", "title": "B"},
        ]
        args = SimpleNamespace(
            source="files",
            input_dir="",
            grafana_token="",
            alert_uids="",
            alert_folder="",
        )
        with patch(
            "observability_migration.adapters.source.grafana.alert_pipeline.extract_all_alerting_resources_from_files",
            return_value={"alert_rules": rules},
        ):
            result = alert_pipeline.load_unified_alerting_resources(args)

        self.assertEqual(len(result["alert_rules"]), 2)


# =====================================================================
# Kibana alerting client tests
# =====================================================================


class TestKibanaAlertingPreflight(unittest.TestCase):
    def test_validate_rule_payload_missing_type(self):
        from observability_migration.targets.kibana.alerting import validate_rule_payload
        result = validate_rule_payload("", {}, {})
        self.assertFalse(result["valid"])
        self.assertTrue(any("rule_type_id" in e for e in result["errors"]))

    def test_validate_rule_payload_unavailable_type(self):
        from observability_migration.targets.kibana.alerting import validate_rule_payload
        preflight = {"rule_family_availability": {"es-query": False}}
        result = validate_rule_payload(".es-query", {"esqlQuery": {}}, preflight)
        self.assertFalse(result["valid"])
        self.assertTrue(any("not available" in e for e in result["errors"]))

    def test_validate_rule_payload_valid(self):
        from observability_migration.targets.kibana.alerting import validate_rule_payload
        preflight = {"rule_family_availability": {"es-query": True}}
        result = validate_rule_payload(".es-query", {"esqlQuery": {"esql": "FROM metrics-*"}}, preflight)
        self.assertTrue(result["valid"])

    def test_run_alerting_preflight_structure(self):
        from observability_migration.targets.kibana.alerting import run_alerting_preflight
        with patch("observability_migration.targets.kibana.alerting._session") as mock_sess:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {}
            mock_resp.raise_for_status = MagicMock()
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_sess.return_value = mock_session

            result = run_alerting_preflight("http://kibana:5601", api_key="test")
            self.assertIn("health", result)
            self.assertIn("rule_family_availability", result)
            self.assertIn("can_create_es_query_rules", result)

    def test_collect_emitted_rule_payloads_filters_non_emitted_rows(self):
        from observability_migration.targets.kibana.alerting import collect_emitted_rule_payloads

        grafana_report = {
            "alerts": [
                {
                    "alert_id": "grafana-1",
                    "name": "Grafana alert",
                    "kind": "grafana_legacy",
                    "target": {
                        "payload_emitted": True,
                        "rule_payload": {"rule_type_id": ".es-query", "enabled": False},
                    },
                },
                {
                    "alert_id": "grafana-2",
                    "name": "Blocked alert",
                    "kind": "grafana_unified",
                    "target": {
                        "payload_emitted": False,
                        "rule_payload": {},
                    },
                },
            ]
        }
        datadog_report = {
            "monitors": [
                {
                    "alert_id": "datadog-1",
                    "name": "Datadog monitor",
                    "kind": "datadog_metric",
                    "target": {
                        "payload_emitted": True,
                        "rule_payload": {"rule_type_id": ".es-query", "enabled": False},
                    },
                }
            ]
        }

        payloads = collect_emitted_rule_payloads(grafana_report, datadog_report)
        self.assertEqual([item["alert_id"] for item in payloads], ["grafana-1", "datadog-1"])
        self.assertEqual([item["source_type"] for item in payloads], ["alerts", "monitors"])
        self.assertTrue(all(item["payload"]["enabled"] is False for item in payloads))

    def test_cleanup_rules_tracks_boolean_delete_results(self):
        from observability_migration.targets.kibana.alerting import cleanup_rules

        calls = []

        def _fake_delete(kibana_url, rule_id, *, api_key="", space_id="", timeout=15, verify=True):
            calls.append((kibana_url, rule_id, api_key, space_id, timeout))
            return rule_id != "rule-2"

        result = cleanup_rules(
            "http://kibana:5601",
            ["rule-1", "rule-2"],
            api_key="secret",
            delete_rule_fn=_fake_delete,
        )

        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["failed_rule_ids"], ["rule-2"])
        self.assertEqual([call[1] for call in calls], ["rule-1", "rule-2"])

    def test_collect_migrated_rules_matches_tagged_or_named_rules(self):
        from observability_migration.targets.kibana.alerting import collect_migrated_rules

        rules = [
            {"id": "rule-1", "name": "[migrated] CPU high", "enabled": True, "tags": []},
            {"id": "rule-2", "name": "Manual log check", "enabled": False, "tags": ["obs-migration"]},
            {"id": "rule-3", "name": "Something else", "enabled": True, "tags": ["other"]},
        ]

        migrated = collect_migrated_rules(rules)
        self.assertEqual([rule["id"] for rule in migrated], ["rule-1", "rule-2"])

    def test_create_rules_from_payloads_from_emitted_shape_creates_rules(self):
        from observability_migration.targets.kibana.alerting import (
            create_rules_from_payloads,
        )

        calls: list[dict[str, Any]] = []

        def _fake_create(kibana_url, **kwargs):
            calls.append({"kibana_url": kibana_url, **kwargs})
            return {"id": f"rule-{len(calls)}", "name": kwargs["name"], "enabled": kwargs["enabled"]}

        items = [
            {
                "source_type": "alerts",
                "alert_id": "grafana-1",
                "name": "CPU high",
                "kind": "grafana_unified",
                "payload": {
                    "rule_type_id": ".es-query",
                    "name": "CPU high",
                    "consumer": "alerts",
                    "schedule": {"interval": "1m"},
                    "params": {"esqlQuery": {"esql": "FROM metrics-*"}},
                    "actions": [],
                    "tags": ["legacy-grafana"],
                    "enabled": True,
                },
            },
        ]

        result = create_rules_from_payloads(
            "http://kibana:5601",
            items,
            api_key="key",
            create_rule_fn=_fake_create,
        )

        self.assertEqual(result["summary"], {"created": 1, "failed": 0, "skipped": 0})
        self.assertEqual(len(result["created"]), 1)
        created_call = calls[0]
        self.assertEqual(created_call["rule_type_id"], ".es-query")
        self.assertEqual(created_call["name"], "[migrated] CPU high")
        self.assertFalse(created_call["enabled"], "rules must be created disabled by default")
        self.assertIn("obs-migration", created_call["tags"])
        self.assertIn("legacy-grafana", created_call["tags"])

    def test_create_rules_from_payloads_from_mapping_shape_skips_non_automated(self):
        from observability_migration.targets.kibana.alerting import (
            create_rules_from_payloads,
        )

        calls: list[dict[str, Any]] = []

        def _fake_create(kibana_url, **kwargs):
            calls.append(kwargs)
            return {"id": "rule-x", "name": kwargs["name"], "enabled": kwargs["enabled"]}

        items = [
            {
                "alert_id": "a1",
                "name": "auto",
                "kind": "grafana_unified",
                "mapping": {
                    "payload_emitted": True,
                    "automation_tier": "automated",
                    "rule_payload": {
                        "rule_type_id": ".es-query",
                        "name": "auto",
                        "params": {},
                    },
                },
            },
            {
                "alert_id": "a2",
                "name": "draft",
                "kind": "grafana_legacy",
                "mapping": {
                    "payload_emitted": True,
                    "automation_tier": "draft_requires_review",
                    "rule_payload": {
                        "rule_type_id": ".es-query",
                        "name": "draft",
                        "params": {},
                    },
                },
            },
            {
                "alert_id": "a3",
                "name": "no-payload",
                "kind": "grafana_unified",
                "mapping": {
                    "payload_emitted": False,
                    "automation_tier": "automated",
                    "rule_payload": {},
                },
            },
        ]

        result = create_rules_from_payloads(
            "http://kibana:5601",
            items,
            api_key="key",
            create_rule_fn=_fake_create,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["summary"]["created"], 1)
        self.assertEqual(result["summary"]["skipped"], 1)
        self.assertEqual(result["skipped"][0]["alert_id"], "a2")
        self.assertIn("draft_requires_review", result["skipped"][0]["reason"])

    def test_create_rules_from_payloads_records_failures(self):
        from observability_migration.targets.kibana.alerting import (
            create_rules_from_payloads,
        )

        def _fake_create(kibana_url, **kwargs):
            return {"error": "400 Bad Request"}

        items = [
            {
                "alert_id": "a1",
                "name": "n",
                "kind": "grafana_unified",
                "payload": {"rule_type_id": ".es-query", "name": "n", "params": {}},
            }
        ]

        result = create_rules_from_payloads(
            "http://kibana:5601",
            items,
            create_rule_fn=_fake_create,
        )

        self.assertEqual(result["summary"], {"created": 0, "failed": 1, "skipped": 0})
        self.assertEqual(result["failed"][0]["error"], "400 Bad Request")

    def test_create_rules_from_payloads_does_not_double_apply_name_prefix(self):
        from observability_migration.targets.kibana.alerting import (
            create_rules_from_payloads,
        )

        calls: list[dict[str, Any]] = []

        def _fake_create(kibana_url, **kwargs):
            calls.append(kwargs)
            return {"id": "rule-x", "name": kwargs["name"], "enabled": kwargs["enabled"]}

        items = [
            {
                "alert_id": "a1",
                "name": "Already prefixed",
                "kind": "grafana_legacy",
                "payload": {
                    "rule_type_id": ".es-query",
                    "name": "[migrated] Already prefixed",
                    "params": {},
                },
            }
        ]

        result = create_rules_from_payloads(
            "http://kibana:5601",
            items,
            create_rule_fn=_fake_create,
        )

        self.assertEqual(result["summary"]["created"], 1)
        self.assertEqual(calls[0]["name"], "[migrated] Already prefixed")

    def test_create_rules_from_payloads_skips_all_when_preflight_unreachable(self):
        from observability_migration.targets.kibana.alerting import (
            create_rules_from_payloads,
        )

        def _fake_create(*_args, **_kwargs):
            raise AssertionError("create_rule must not be called when preflight is unreachable")

        items = [
            {
                "alert_id": "a1",
                "name": "n",
                "kind": "grafana_unified",
                "payload": {"rule_type_id": ".es-query", "name": "n", "params": {}},
            }
        ]

        preflight = {
            "health": {"error": "connection refused"},
            "rule_types_count": 0,
            "connector_types_count": 0,
        }

        result = create_rules_from_payloads(
            "http://kibana:5601",
            items,
            preflight=preflight,
            create_rule_fn=_fake_create,
        )

        self.assertTrue(result["preflight_unreachable"])
        self.assertEqual(result["summary"], {"created": 0, "failed": 0, "skipped": 1})
        self.assertEqual(result["skipped"][0]["reason"], "preflight_unreachable")

    def test_audit_migrated_rules_optionally_disables_enabled_rules(self):
        from observability_migration.targets.kibana.alerting import audit_migrated_rules

        listed_pages = {
            1: {
                "data": [
                    {"id": "rule-1", "name": "[migrated] CPU high", "enabled": True, "tags": []},
                    {"id": "rule-2", "name": "Manual log check", "enabled": False, "tags": ["obs-migration"]},
                ],
                "total": 3,
            },
            2: {
                "data": [
                    {"id": "rule-3", "name": "Something else", "enabled": True, "tags": ["other"]},
                ],
                "total": 3,
            },
            3: {"data": [], "total": 3},
        }

        list_calls = []
        disable_calls = []

        def _fake_list(kibana_url, *, api_key="", space_id="", timeout=15, per_page=100, page=1, verify=True):
            list_calls.append(page)
            return listed_pages[page]

        def _fake_disable(kibana_url, rule_id, *, api_key="", space_id="", timeout=15, verify=True):
            disable_calls.append(rule_id)
            return True

        result = audit_migrated_rules(
            "http://kibana:5601",
            api_key="secret",
            disable_enabled=True,
            list_rules_fn=_fake_list,
            disable_rule_fn=_fake_disable,
        )

        self.assertEqual(list_calls, [1, 2])
        self.assertEqual(result["migrated_rule_ids"], ["rule-1", "rule-2"])
        self.assertEqual(result["enabled_migrated_rule_ids"], ["rule-1"])
        self.assertEqual(result["disabled_migrated_rule_ids"], ["rule-2"])
        self.assertEqual(result["remediation"]["attempted_rule_ids"], ["rule-1"])
        self.assertEqual(result["remediation"]["disabled_rule_ids"], ["rule-1"])
        self.assertEqual(disable_calls, ["rule-1"])

    def test_audit_migrated_rules_reports_truncated_listing(self):
        from observability_migration.targets.kibana.alerting import audit_migrated_rules

        def _fake_list(kibana_url, *, api_key="", space_id="", timeout=15, per_page=100, page=1, verify=True):
            return {
                "data": [{"id": f"rule-{page}", "name": "[migrated] CPU high", "enabled": False, "tags": []}],
                "total": 3,
            }

        result = audit_migrated_rules(
            "http://kibana:5601",
            per_page=1,
            max_pages=2,
            list_rules_fn=_fake_list,
        )

        self.assertTrue(result["listing_truncated"])
        self.assertEqual(result["total_rules_available"], 3)
        self.assertEqual(result["total_rules_seen"], 2)
        self.assertIn("Increase --max-pages", result["listing_warning"])


# =====================================================================
# CLI flag tests
# =====================================================================


class TestUnifiedCliFetchAlertsFlag(unittest.TestCase):
    def test_migrate_parser_has_fetch_alerts(self):
        from observability_migration.app.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["migrate", "--source", "grafana", "--fetch-alerts"])
        self.assertTrue(args.fetch_alerts)

    def test_migrate_parser_has_grafana_token(self):
        from observability_migration.app.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["migrate", "--source", "grafana", "--grafana-token", "tok123"])
        self.assertEqual(args.grafana_token, "tok123")

    def test_migrate_parser_has_monitor_ids(self):
        from observability_migration.app.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["migrate", "--source", "datadog", "--monitor-ids", "1,2,3"])
        self.assertEqual(args.monitor_ids, "1,2,3")

    def test_migrate_parser_has_create_alert_rules(self):
        from observability_migration.app.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args([
            "migrate",
            "--source",
            "grafana",
            "--fetch-alerts",
            "--create-alert-rules",
        ])
        self.assertTrue(args.create_alert_rules)

    def test_create_alert_rules_forwarded_to_grafana_legacy_cli(self):
        from observability_migration.app.cli import _run_grafana_migration

        captured: dict[str, list[str]] = {}

        def _fake_main():
            captured["argv"] = list(sys.argv)

        with patch(
            "observability_migration.adapters.source.grafana.cli.main",
            side_effect=_fake_main,
        ):
            with self.assertWarnsRegex(
                FutureWarning,
                "--fetch-alerts/--fetch-monitors are deprecated",
            ):
                _run_grafana_migration(
                    SimpleNamespace(
                        input_mode="files",
                        input_dir="/tmp/input",
                        output_dir="/tmp/output",
                        data_view="metrics-*",
                        assets="dashboards",
                        esql_index="",
                        logs_index="",
                        native_promql=False,
                        include="dashboards",
                        rules_file=[],
                        plugin=[],
                        polish_metadata=False,
                        preflight=False,
                        dataset_filter="",
                        logs_dataset_filter="",
                        smoke_report="",
                        fetch_alerts=True,
                        create_alert_rules=True,
                        grafana_token="",
                        smoke=False,
                        browser_audit=False,
                        capture_screenshots=False,
                        smoke_output="",
                        smoke_timeout=30,
                        chrome_binary="",
                        validate=False,
                        upload=False,
                        es_url="",
                        es_api_key="",
                        kibana_url="",
                        kibana_api_key="",
                        space_id="",
                    ),
                )
        self.assertIn("--assets", captured["argv"])
        asset_index = captured["argv"].index("--assets")
        self.assertEqual(captured["argv"][asset_index + 1], "all")
        self.assertNotIn("--fetch-alerts", captured["argv"])
        self.assertIn("--create-alert-rules", captured["argv"])

    def test_create_alert_rules_forwarded_to_datadog_legacy_cli(self):
        from observability_migration.app.cli import _run_datadog_migration

        captured: dict[str, list[str]] = {}

        def _fake_main():
            captured["argv"] = list(sys.argv)

        with patch(
            "observability_migration.adapters.source.datadog.cli.main",
            side_effect=_fake_main,
        ):
            with self.assertWarnsRegex(
                FutureWarning,
                "--fetch-alerts/--fetch-monitors are deprecated",
            ):
                _run_datadog_migration(
                    SimpleNamespace(
                        input_mode="files",
                        input_dir="/tmp/input",
                        output_dir="/tmp/output",
                        data_view="metrics-*",
                        assets="dashboards",
                        logs_index="",
                        field_profile="otel",
                        validate=False,
                        upload=False,
                        preflight=False,
                        dataset_filter="",
                        logs_dataset_filter="",
                        es_url="",
                        es_api_key="",
                        kibana_url="",
                        kibana_api_key="",
                        space_id="",
                        fetch_alerts=True,
                        create_alert_rules=True,
                        monitor_ids="",
                        monitor_query="",
                        env_file="",
                        smoke=False,
                        browser_audit=False,
                        capture_screenshots=False,
                        smoke_output="",
                        smoke_timeout=30,
                        chrome_binary="",
                    ),
                )
        self.assertIn("--assets", captured["argv"])
        asset_index = captured["argv"].index("--assets")
        self.assertEqual(captured["argv"][asset_index + 1], "all")
        self.assertNotIn("--fetch-monitors", captured["argv"])
        self.assertIn("--create-alert-rules", captured["argv"])


def _load_cli_contract_module():
    module_name = "observability_migration.core.cli_contract"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised in red phase
        raise AssertionError(f"{module_name} is missing") from exc


class LegacyFlagContractTests(unittest.TestCase):
    def test_fetch_alerts_alias_normalizes_to_all_assets(self):
        cli_contract = _load_cli_contract_module()
        normalize_requested_assets = getattr(cli_contract, "normalize_requested_assets", None)
        self.assertIsNotNone(
            normalize_requested_assets,
            "observability_migration.core.cli_contract.normalize_requested_assets is missing",
        )

        with self.assertWarnsRegex(
            FutureWarning,
            "--fetch-alerts/--fetch-monitors are deprecated",
        ):
            selection = normalize_requested_assets(
                assets="dashboards",
                fetch_alerts=True,
                fetch_monitors=False,
            )
        self.assertEqual(selection.label, "all")

    def test_fetch_monitors_alias_normalizes_to_all_assets(self):
        cli_contract = _load_cli_contract_module()
        normalize_requested_assets = getattr(cli_contract, "normalize_requested_assets", None)
        self.assertIsNotNone(
            normalize_requested_assets,
            "observability_migration.core.cli_contract.normalize_requested_assets is missing",
        )

        with self.assertWarnsRegex(
            FutureWarning,
            "--fetch-alerts/--fetch-monitors are deprecated",
        ):
            selection = normalize_requested_assets(
                assets="dashboards",
                fetch_alerts=False,
                fetch_monitors=True,
            )
        self.assertEqual(selection.label, "all")


class TestGrafanaCliFetchAlertsFlag(unittest.TestCase):
    def test_grafana_parser_has_fetch_alerts(self):
        from observability_migration.adapters.source.grafana.cli import parse_args
        args = parse_args(["--fetch-alerts"])
        self.assertTrue(args.fetch_alerts)

    def test_grafana_parser_has_grafana_token(self):
        from observability_migration.adapters.source.grafana.cli import parse_args
        args = parse_args(["--grafana-token", "my-token"])
        self.assertEqual(args.grafana_token, "my-token")

    def test_grafana_parser_has_create_alert_rules(self):
        from observability_migration.adapters.source.grafana.cli import parse_args
        args = parse_args(["--fetch-alerts", "--create-alert-rules"])
        self.assertTrue(args.create_alert_rules)

    def test_grafana_parser_has_alert_uids(self):
        from observability_migration.adapters.source.grafana.cli import parse_args
        args = parse_args(["--alert-uids", "rule-uid-1,rule-uid-2"])
        self.assertEqual(args.alert_uids, "rule-uid-1,rule-uid-2")

    def test_grafana_parser_has_alert_folder(self):
        from observability_migration.adapters.source.grafana.cli import parse_args
        args = parse_args(["--alert-folder", "folder-uid-a"])
        self.assertEqual(args.alert_folder, "folder-uid-a")

    def test_grafana_parser_alert_uids_defaults_to_empty(self):
        from observability_migration.adapters.source.grafana.cli import parse_args
        args = parse_args([])
        self.assertEqual(args.alert_uids, "")

    def test_grafana_parser_alert_folder_defaults_to_empty(self):
        from observability_migration.adapters.source.grafana.cli import parse_args
        args = parse_args([])
        self.assertEqual(args.alert_folder, "")


class TestDatadogCliFetchMonitorsFlag(unittest.TestCase):
    def test_datadog_parser_has_fetch_monitors(self):
        from observability_migration.adapters.source.datadog.cli import parse_args
        args = parse_args(["--fetch-monitors"])
        self.assertTrue(args.fetch_monitors)

    def test_datadog_parser_has_monitor_ids(self):
        from observability_migration.adapters.source.datadog.cli import parse_args
        args = parse_args(["--monitor-ids", "111,222"])
        self.assertEqual(args.monitor_ids, "111,222")

    def test_datadog_parser_has_monitor_query(self):
        from observability_migration.adapters.source.datadog.cli import parse_args
        args = parse_args(["--monitor-query", "tag:env:prod"])
        self.assertEqual(args.monitor_query, "tag:env:prod")

    def test_datadog_parser_has_create_alert_rules(self):
        from observability_migration.adapters.source.datadog.cli import parse_args
        args = parse_args(["--fetch-monitors", "--create-alert-rules"])
        self.assertTrue(args.create_alert_rules)


# =====================================================================
# MigrationResult alert fields tests
# =====================================================================


class TestMigrationResultAlertFields(unittest.TestCase):
    def test_default_alert_fields(self):
        result = MigrationResult("Test", "uid-1")
        self.assertEqual(result.alert_results, [])
        self.assertEqual(result.alert_summary, {})

    def test_alert_fields_can_be_populated(self):
        result = MigrationResult("Test", "uid-1")
        ir = build_alerting_ir_from_datadog(_datadog_metric_monitor())
        result.alert_results = [ir.to_dict()]
        result.alert_summary = {"total": 1, "automated": 0, "draft_review": 1, "manual_required": 0}
        self.assertEqual(len(result.alert_results), 1)
        self.assertEqual(result.alert_summary["total"], 1)
        self.assertIn("selected_target_rule_type", result.alert_results[0])
        self.assertIn("payload_emitted", result.alert_results[0])


# =====================================================================
# Integration-level tests
# =====================================================================


class TestEndToEndAlertMapping(unittest.TestCase):
    def test_full_pipeline_mixed_alerts(self):
        """Test that a mixed batch of alerts produces correct tier distribution."""
        alerts = [
            build_alerting_ir_from_grafana(_grafana_legacy_alert_task()),
            build_alerting_ir_from_grafana_unified(
                _grafana_unified_prometheus_rule(),
                datasource_map={"prometheus": {"type": "prometheus", "name": "Prometheus"}},
            ),
            build_alerting_ir_from_datadog(_datadog_metric_monitor()),
            build_alerting_ir_from_datadog(_datadog_log_monitor()),
            build_alerting_ir_from_datadog(_datadog_composite_monitor()),
        ]
        batch_result = map_alerts_batch(alerts)
        summary = batch_result["summary"]

        self.assertEqual(summary["total"], 5)
        self.assertGreater(summary["by_automation_tier"].get("draft_requires_review", 0), 0)
        self.assertGreater(summary["by_automation_tier"].get("manual_required", 0), 0)

        for item in batch_result["results"]:
            mapping = item["mapping"]
            if mapping["automation_tier"] == "manual_required":
                self.assertFalse(mapping["valid"])
                self.assertEqual(mapping["rule_payload"], {})
            else:
                self.assertTrue(mapping["valid"])
                self.assertIn("rule_type_id", mapping["rule_payload"])

    def test_all_payloads_are_disabled(self):
        """Safety: all generated payloads must have enabled=False."""
        alerts = [
            build_alerting_ir_from_grafana(_grafana_legacy_alert_task()),
            build_alerting_ir_from_datadog(_datadog_metric_monitor()),
        ]
        batch_result = map_alerts_batch(alerts)
        for item in batch_result["results"]:
            payload = item["mapping"]["rule_payload"]
            if payload:
                self.assertFalse(payload.get("enabled", True))

    def test_migration_tags_present(self):
        """All generated payloads should have obs-migration tag."""
        ir = build_alerting_ir_from_grafana(_grafana_legacy_alert_task())
        result = map_alert_to_kibana_payload(ir)
        if result["rule_payload"]:
            self.assertIn("obs-migration", result["rule_payload"]["tags"])


if __name__ == "__main__":
    unittest.main()
