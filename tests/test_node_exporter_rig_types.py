# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_scraper_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "parity-rig/curated/grafana_763_redis_exporter/redis_scraper.py"
    )
    spec = spec_from_file_location("redis_scraper", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_apply_metric_type_overrides_promotes_known_node_counters():
    scraper = _load_scraper_module()

    metric_types = {
        "node_vmstat_pgpgin": "untyped",
        "node_netstat_Tcp_InSegs": "untyped",
        "process_virtual_memory_bytes": "gauge",
        "node_cpu_seconds_total": "counter",
    }

    resolved = scraper.apply_metric_type_overrides("node.prometheus", metric_types)

    assert resolved["node_vmstat_pgpgin"] == "counter"
    assert resolved["node_netstat_Tcp_InSegs"] == "counter"
    assert resolved["process_virtual_memory_bytes"] == "gauge"
    assert resolved["node_cpu_seconds_total"] == "counter"


def test_apply_metric_type_overrides_promotes_known_mysql_counters():
    scraper = _load_scraper_module()
    metric_types = {
        "mysql_global_status_questions": "untyped",
        "mysql_global_status_threads_connected": "gauge",
    }
    resolved = scraper.apply_metric_type_overrides("mysql.prometheus", metric_types)
    assert resolved["mysql_global_status_questions"] == "counter"
    assert resolved["mysql_global_status_threads_connected"] == "gauge"


def test_apply_metric_type_overrides_leaves_unrelated_datasets_unchanged():
    scraper = _load_scraper_module()
    metric_types = {"custom_exporter_events": "untyped"}
    resolved = scraper.apply_metric_type_overrides("custom.prometheus", metric_types)
    assert resolved is metric_types


def test_build_bulk_body_can_add_stable_base_labels() -> None:
    scraper = _load_scraper_module()

    body = scraper.build_bulk_body(
        groups={(("role", "master"),): {"redis_up": 1.0}},
        timestamp="2026-08-05T00:00:00.000Z",
        dataset="redis.prometheus",
        job="redis_exporter",
        instance="redis:6379",
        extra_base_labels={"pod": "redis-0"},
    )

    lines = body.strip().splitlines()
    assert len(lines) == 2
    payload = __import__("json").loads(lines[1])
    assert payload["labels"]["pod"] == "redis-0"
    assert payload["labels"]["namespace"] == "default"
    assert payload["labels"]["instance"] == "redis:6379"
