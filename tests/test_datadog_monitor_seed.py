from __future__ import annotations

import unittest


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


def _datadog_log_measure_monitor():
    return {
        "id": 67891,
        "name": "Checkout latency p99 is high",
        "type": "log alert",
        "query": 'logs("service:checkout status:error").index("*").rollup("pc99", "@duration").last("10m") > 250',
        "message": "Checkout latency p99 is high",
        "options": {
            "thresholds": {"critical": 250},
            "notify_no_data": False,
        },
    }


class TestDatadogMonitorSeedRequirements(unittest.TestCase):
    def test_metric_monitor_contributes_metric_and_dimensions(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.monitor_seed import (
            extract_monitor_seed_requirements,
        )

        field_map = load_profile("otel")
        requirements = extract_monitor_seed_requirements(
            [_datadog_live_metric_query_alert()],
            field_map,
        )

        self.assertEqual(requirements.metric_fields["pipelines_component_errors_total"], "counter")
        self.assertEqual(requirements.log_measure_fields, {})
        self.assertEqual(
            requirements.dimensions,
            {"component_type", "component_id", "pipeline_id", "host.name", "worker_uuid"},
        )

    def test_log_measure_monitor_contributes_log_measure_and_dimensions(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.monitor_seed import (
            extract_monitor_seed_requirements,
        )

        field_map = load_profile("otel")
        requirements = extract_monitor_seed_requirements(
            [_datadog_log_measure_monitor()],
            field_map,
        )

        self.assertEqual(requirements.metric_fields, {})
        self.assertEqual(requirements.log_measure_fields["duration"], "gauge")
        self.assertIn("service.name", requirements.dimensions)

    def test_unsupported_monitor_does_not_contribute_requirements(self):
        from observability_migration.adapters.source.datadog.field_map import load_profile
        from observability_migration.adapters.source.datadog.monitor_seed import (
            extract_monitor_seed_requirements,
        )

        field_map = load_profile("otel")
        requirements = extract_monitor_seed_requirements(
            [_datadog_service_check_monitor()],
            field_map,
        )

        self.assertEqual(requirements.metric_fields, {})
        self.assertEqual(requirements.log_measure_fields, {})
        self.assertEqual(requirements.dimensions, set())
