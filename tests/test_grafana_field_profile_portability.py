# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Always-on Grafana field-profile emit + leakage coverage.

How people typically scrape Prometheus into Grafana, and the Elastic layout
that matches that ingest once they switch:

- node_exporter host checks (``node_cpu_seconds_total``, label ``instance``)
  → native ES ``/_prometheus`` (``prometheus_native``), Fleet Prometheus
  ``use_types`` (``prometheus_remote_write``), classic Metricbeat
  (``prometheus_metrics``), or OTel Collector (``otel``).
- cAdvisor / kubelet (``container_memory_usage_bytes``, labels ``namespace`` /
  ``pod``) → OTel ``k8s.*`` vs Prometheus ``labels.*`` /
  ``prometheus.labels.*``.
- redis_exporter / app RED (``redis_memory_used_bytes``,
  ``http_requests_total``) → the same Prometheus-shaped layouts, or keep
  source names (``passthrough``).

Unlike Datadog, Grafana metric *names* stay Prometheus-shaped under ``otel``
(no dot-to-underscore flatten). Labels still remap to OTel candidates
(``instance`` → ``service.instance.id``). These tests migrate one compact
in-repo dashboard under every built-in profile and assert the emitted ES|QL
uses that layout's field spellings with no cross-profile leakage.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier.profile_leakage import (  # noqa: E402
    check_profile_leakage,
    extract_esql_queries,
)

_FIXTURE_DIR = ROOT / "tests" / "fixtures" / "grafana_field_profile"
_INDEX = "metrics-gffp-default"

_ALL_PROFILES = [
    "otel",
    "prometheus_native",
    "prometheus_metrics",
    "prometheus_remote_write",
    "passthrough",
]


def _migrate(out_dir: Path, profile: str, index: str = _INDEX) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    cmd = [
        sys.executable,
        "-m",
        "observability_migration.app.cli",
        "migrate",
        "--source",
        "grafana",
        "--input-mode",
        "files",
        "--input-dir",
        str(_FIXTURE_DIR),
        "--output-dir",
        str(out_dir),
        "--assets",
        "dashboards",
        "--field-profile",
        profile,
        "--esql-index",
        index,
        "--data-view",
        index,
        "--no-curated-packs",
        "--translation-mode",
        "esql",
    ]
    subprocess.run(cmd, check=True, cwd=str(ROOT), env=env, capture_output=True)


def _native_json(out_dir: Path) -> dict:
    paths = list((out_dir / "dashboards" / "native").glob("*.native.json"))
    assert paths, f"no native artifacts under {out_dir}"
    return json.loads(paths[0].read_text())


def _joined_queries(out_dir: Path) -> str:
    return "\n".join(extract_esql_queries(_native_json(out_dir)))


@pytest.mark.parametrize("profile", _ALL_PROFILES)
def test_grafana_ingest_stories_no_profile_leakage(tmp_path, profile):
    out = tmp_path / profile
    _migrate(out, profile)
    queries = extract_esql_queries(_native_json(out))
    assert queries, f"{profile}: expected emitted ES|QL"
    violations: list[str] = []
    for query in queries:
        violations.extend(check_profile_leakage(query, profile))
    assert violations == [], f"{profile} leaked: {violations}"


def test_otel_keeps_prom_metric_names_and_maps_labels_to_otel(tmp_path):
    """OTel Collector ingest: Prometheus metric names, OTel label candidates."""
    out = tmp_path / "otel"
    _migrate(out, "otel")
    joined = _joined_queries(out)
    assert "node_cpu_seconds_total" in joined
    assert "container_memory_usage_bytes" in joined
    assert "redis_memory_used_bytes" in joined
    assert "http_requests_total" in joined
    assert "service.instance.id" in joined
    assert "k8s.namespace.name" in joined
    assert "k8s.pod.name" in joined
    assert "service.name" in joined
    assert "labels.instance" not in joined
    assert "metrics.node_cpu_seconds_total" not in joined
    assert "prometheus.labels." not in joined
    assert "prometheus.metrics." not in joined


def test_prometheus_native_namespaces_metrics_and_labels(tmp_path):
    """Native ES /_prometheus write: metrics.* + labels.*."""
    out = tmp_path / "prometheus_native"
    _migrate(out, "prometheus_native")
    joined = _joined_queries(out)
    assert "metrics.node_cpu_seconds_total" in joined
    assert "metrics.container_memory_usage_bytes" in joined
    assert "metrics.redis_memory_used_bytes" in joined
    assert "metrics.http_requests_total" in joined
    assert "labels.instance" in joined
    assert "labels.namespace" in joined
    assert "labels.pod" in joined
    assert "labels.job" in joined
    assert "service.instance.id" not in joined
    assert "k8s.namespace.name" not in joined
    assert "prometheus.metrics." not in joined
    assert "prometheus.labels." not in joined


def test_prometheus_metrics_uses_nested_prometheus_namespace(tmp_path):
    """Classic Metricbeat remote_write: prometheus.metrics.* + prometheus.labels.*."""
    out = tmp_path / "prometheus_metrics"
    _migrate(out, "prometheus_metrics")
    joined = _joined_queries(out)
    assert "prometheus.metrics.node_cpu_seconds_total" in joined
    assert "prometheus.labels.instance" in joined
    assert "prometheus.labels.namespace" in joined
    assert "prometheus.labels.pod" in joined
    stripped = joined.replace("prometheus.metrics.", "").replace("prometheus.labels.", "")
    assert "metrics.node_cpu_seconds_total" not in stripped
    assert "labels.instance" not in stripped
    assert "service.instance.id" not in joined


def test_prometheus_remote_write_uses_typed_leaves(tmp_path):
    """Fleet / Metricbeat use_types: prometheus.<metric>.{counter,value}."""
    out = tmp_path / "prometheus_remote_write"
    _migrate(out, "prometheus_remote_write")
    joined = _joined_queries(out)
    assert "prometheus.node_cpu_seconds_total.counter" in joined
    assert "prometheus.http_requests_total.counter" in joined
    assert "prometheus.container_memory_usage_bytes.value" in joined
    assert "prometheus.redis_memory_used_bytes.value" in joined
    assert "prometheus.labels.instance" in joined
    assert "prometheus.labels.pod" in joined
    assert "prometheus.metrics." not in joined
    assert "metrics.node_cpu_seconds_total" not in joined
    assert "service.instance.id" not in joined


def test_passthrough_keeps_prometheus_metric_and_label_names(tmp_path):
    """Custom pipeline that stored Prometheus names unchanged."""
    out = tmp_path / "passthrough"
    _migrate(out, "passthrough")
    joined = _joined_queries(out)
    assert "node_cpu_seconds_total" in joined
    assert "container_memory_usage_bytes" in joined
    assert "redis_memory_used_bytes" in joined
    assert "http_requests_total" in joined
    assert ", instance" in joined
    assert ", namespace, pod" in joined
    assert ", job" in joined
    assert "service.instance.id" not in joined
    assert "k8s.namespace.name" not in joined
    assert "labels.instance" not in joined
    assert "metrics.node_cpu_seconds_total" not in joined


def test_cli_migrate_grafana_profiles_write_distinct_native(tmp_path):
    """Each profile must change the query, not only the CLI flag."""
    signatures: dict[str, str] = {}
    for profile in ("otel", "prometheus_native", "passthrough"):
        out = tmp_path / profile
        _migrate(out, profile, index=f"metrics-gf{profile[:3]}-default")
        signatures[profile] = _joined_queries(out)
    assert signatures["otel"] != signatures["prometheus_native"]
    assert signatures["otel"] != signatures["passthrough"]
    assert signatures["prometheus_native"] != signatures["passthrough"]
