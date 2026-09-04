# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Always-on Datadog field-profile emit + leakage coverage.

How people typically collect Datadog telemetry, and the Elastic layout that
matches that ingest once they switch:

- Datadog Agent host checks (``system.cpu.user``) → Elastic Agent system
  integration (``elastic_agent``) or OTel Collector (``otel``).
- Kubernetes cluster agent (``kubernetes.pods.running``, tags
  ``kube_namespace`` / ``pod_name``) → OTel ``k8s.*`` vs Elastic Agent
  ``kubernetes.*``.
- Integration / DogStatsD (``redis.mem.used``, ``myapp.http.requests``) →
  Prometheus remote-write (``prometheus`` / ``prometheus_native``) or keep
  Datadog names (``passthrough``).

Datadog has no ``auto`` profile: the operator must pick the layout that
matches the ingest route. These tests migrate one compact in-repo dashboard
under every built-in profile and assert the emitted ES|QL uses that layout's
field spellings with no cross-profile leakage.
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

_FIXTURE_DIR = ROOT / "tests" / "fixtures" / "datadog_field_profile"
_INDEX = "metrics-ddfp.prometheus-default"

# Grafana leakage linter keys ``prometheus_metrics`` for the Metricbeat layout;
# Datadog's built-in name for the same layout is ``prometheus``.
_LEAKAGE_PROFILE = {
    "prometheus": "prometheus_metrics",
    "prometheus_native": "prometheus_native",
    "otel": "otel",
    "passthrough": "passthrough",
    "elastic_agent": "otel",  # same forbidden prefixes; extra checks below
}

_ALL_PROFILES = [
    "otel",
    "elastic_agent",
    "prometheus_native",
    "prometheus",
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
        "datadog",
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
        "--data-view",
        index,
    ]
    subprocess.run(cmd, check=True, cwd=str(ROOT), env=env, capture_output=True)


def _native_json(out_dir: Path) -> dict:
    paths = list((out_dir / "dashboards" / "native").glob("*.native.json"))
    assert paths, f"no native artifacts under {out_dir}"
    return json.loads(paths[0].read_text())


def _joined_queries(out_dir: Path) -> str:
    return "\n".join(extract_esql_queries(_native_json(out_dir)))


def _native_blob(out_dir: Path) -> str:
    """Queries plus control field names — template vars become controls."""
    return json.dumps(_native_json(out_dir))


@pytest.mark.parametrize("profile", _ALL_PROFILES)
def test_datadog_ingest_stories_no_profile_leakage(tmp_path, profile):
    out = tmp_path / profile
    _migrate(out, profile)
    queries = extract_esql_queries(_native_json(out))
    assert queries, f"{profile}: expected emitted ES|QL"
    leakage_key = _LEAKAGE_PROFILE[profile]
    violations: list[str] = []
    for query in queries:
        violations.extend(check_profile_leakage(query, leakage_key))
    assert violations == [], f"{profile} leaked: {violations}"


def test_otel_maps_tags_to_ecs_but_flattens_metric_names(tmp_path):
    """OTel Collector ingest: tags become ECS/OTel, metric names do not.

    ``system.cpu.user`` is *not* rewritten to ``system.cpu.utilization``;
    it is only dotted-to-underscore flattened. That is the documented
    Datadog ``otel`` contract — metric renames need ``--metric-map-file``.
    """
    out = tmp_path / "otel"
    _migrate(out, "otel")
    joined = _joined_queries(out)
    blob = _native_blob(out)
    assert "host.name" in joined
    assert "deployment.environment" in blob  # env template var → control
    assert "k8s.namespace.name" in joined
    assert "k8s.pod.name" in joined
    assert "service.name" in joined
    assert "system_cpu_user" in joined
    assert "kubernetes_pods_running" in joined
    assert "redis_mem_used" in joined
    assert "myapp_http_requests" in joined
    # Must not pretend this is the Elastic Agent system integration.
    assert "system.cpu.user.pct" not in joined
    assert "system.memory.actual.used.bytes" not in joined
    assert "kubernetes.namespace" not in joined
    assert "kubernetes.pod.name" not in joined
    # Must not emit Prometheus-shaped fields.
    assert "labels.instance" not in joined
    assert "metrics.system_cpu_user" not in joined


def test_elastic_agent_rewrites_system_metrics_and_uses_kubernetes_ecs(tmp_path):
    """Elastic Agent / Metricbeat system integration layout."""
    out = tmp_path / "elastic_agent"
    _migrate(out, "elastic_agent")
    joined = _joined_queries(out)
    assert "system.cpu.user.pct" in joined
    assert "system.memory.actual.used.bytes" in joined
    assert "host.name" in joined
    assert "kubernetes.namespace" in joined
    assert "kubernetes.pod.name" in joined
    # Unmapped integration / custom metrics stay flattened (no metric_map).
    assert "redis_mem_used" in joined
    assert "kubernetes_pods_running" in joined
    assert "myapp_http_requests" in joined
    # OTel k8s.* spellings belong to the otel profile, not elastic_agent.
    assert "k8s.namespace.name" not in joined
    assert "k8s.pod.name" not in joined
    assert "labels.instance" not in joined


def test_prometheus_native_namespaces_metrics_and_labels(tmp_path):
    """Native ES /_prometheus write: metrics.* + labels.* (host → labels.instance)."""
    out = tmp_path / "prometheus_native"
    _migrate(out, "prometheus_native")
    joined = _joined_queries(out)
    blob = _native_blob(out)
    assert "metrics.system_cpu_user" in joined
    assert "metrics.kubernetes_pods_running" in joined
    assert "metrics.redis_mem_used" in joined
    assert "labels.instance" in joined
    assert "labels.env" in blob  # env template var → control
    assert "labels.kube_namespace" in joined
    assert "labels.pod_name" in joined
    assert "labels.service" in joined
    assert "host.name" not in joined
    assert "k8s.namespace.name" not in joined
    assert "prometheus.metrics." not in joined
    assert "prometheus.labels." not in joined


def test_prometheus_metricbeat_uses_prometheus_metrics_namespace(tmp_path):
    """Metricbeat / Agent Prometheus remote_write: prometheus.metrics.* + prometheus.labels.*."""
    out = tmp_path / "prometheus"
    _migrate(out, "prometheus")
    joined = _joined_queries(out)
    assert "prometheus.metrics.system_cpu_user" in joined
    assert "prometheus.labels.instance" in joined
    assert "prometheus.labels.kube_namespace" in joined
    stripped = joined.replace("prometheus.metrics.", "").replace("prometheus.labels.", "")
    assert "metrics.system_cpu_user" not in stripped
    assert "labels.instance" not in stripped
    assert "host.name" not in joined


def test_passthrough_keeps_datadog_metric_and_tag_names(tmp_path):
    """Custom pipeline that stored Datadog names unchanged."""
    out = tmp_path / "passthrough"
    _migrate(out, "passthrough")
    joined = _joined_queries(out)
    assert "system.cpu.user" in joined
    assert "system.mem.usable" in joined
    assert "kubernetes.pods.running" in joined
    assert "redis.mem.used" in joined
    assert "myapp.http.requests" in joined
    # Tags stay bare Datadog keys (STATS BY host / kube_namespace / service).
    assert ", host" in joined
    assert "kube_namespace" in joined
    assert ", service" in joined
    assert "host.name" not in joined
    assert "k8s.namespace.name" not in joined
    assert "labels.instance" not in joined
    assert "system.cpu.user.pct" not in joined
    assert "system_cpu_user" not in joined


def test_cli_migrate_datadog_profiles_write_distinct_native(tmp_path):
    """Each profile must change the query, not only the CLI flag."""
    signatures: dict[str, str] = {}
    for profile in ("otel", "prometheus_native", "passthrough"):
        out = tmp_path / profile
        _migrate(out, profile, index=f"metrics-dd{profile[:3]}.prometheus-default")
        signatures[profile] = _joined_queries(out)
    assert signatures["otel"] != signatures["prometheus_native"]
    assert signatures["otel"] != signatures["passthrough"]
    assert signatures["prometheus_native"] != signatures["passthrough"]
