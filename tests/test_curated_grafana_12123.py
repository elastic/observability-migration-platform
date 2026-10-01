# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Curated pack for Grafana 12123, Kubernetes / Kubelet."""

from pathlib import Path

import yaml

from observability_migration.adapters.source.grafana import curated_packs as pkg
from observability_migration.adapters.source.grafana.curated_packs import find_curated_pack
from observability_migration.adapters.source.grafana.panels import translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig, resolve_pack_for_dashboard
from observability_migration.adapters.source.grafana.schema import SchemaResolver


def _translate(panel):
    dashboard = {
        "gnetId": 12123,
        "title": "Kubernetes / Kubelet",
        "tags": ["kubernetes-mixin"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=SchemaResolver(resolved),
    )
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    return result, query, yaml_panel


def test_12123_registry_entry_present():
    entry = find_curated_pack(gnet_id=12123, title="", tags=[])
    assert entry is not None
    assert entry["name"] == "grafana_12123_k8s_kubelet"
    assert entry["gnet_revision"] == 1
    assert entry["dashboard_sha256"].startswith("ccd78bc8")


def test_12123_fidelity_rows_name_every_panel():
    path = Path(pkg.__file__).parent / "grafana_12123_k8s_kubelet" / "fidelity_manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    assert manifest["gnet_revision"] == 1
    assert len(manifest["panels"]) == 24
    allowed = {"PERFECT", "APPROXIMATE", "BEST_EFFORT"}
    for panel in manifest["panels"]:
        assert panel["fidelity"] in allowed
        assert panel["grafana_type"]
        assert panel["kibana_type"]
        assert panel["notes"]
        if panel["fidelity"] == "PERFECT":
            assert "delta" not in panel
        else:
            assert panel["delta"] == panel["notes"]


def test_12123_up_ignores_instance_and_keeps_the_kubelet_job():
    panel = {
        "id": 2,
        "type": "singlestat",
        "title": "Up",
        "targets": [{"expr": 'sum(up{job="kubelet", metrics_path="/metrics"})', "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 4, "h": 7},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated", result.reasons
    assert '?instance' not in query
    assert '== "kubelet"' in query
    assert "metrics_path" not in query
    assert yaml_panel["esql"]["type"] == "metric"


def test_12123_running_pods_follow_the_instance_control():
    panel = {
        "id": 3,
        "type": "singlestat",
        "title": "Running Pods",
        "targets": [{"expr": 'sum(kubelet_running_pod_count{instance=~"$instance"})', "refId": "A"}],
        "gridPos": {"x": 4, "y": 0, "w": 4, "h": 7},
    }
    result, query, _yaml = _translate(panel)
    assert result.status == "migrated", result.reasons
    assert "?instance" in query
    assert '== "kubelet"' in query


def test_12123_operation_duration_is_a_bucket_quantile():
    panel = {
        "id": 10,
        "type": "graph",
        "title": "Operation duration 99th quantile",
        "targets": [{"expr": "histogram_quantile(0.99, sum(rate(kubelet_runtime_operations_duration_seconds_bucket[5m])) by (le))", "refId": "A"}],
        "gridPos": {"x": 0, "y": 14, "w": 24, "h": 7},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "histogram_quantile" not in query
    assert "0.989999999" in query
    assert "le" in query
    assert yaml_panel["esql"]["type"] == "line"


def test_12123_pod_start_duration_uses_buckets_not_the_count_series():
    panel = {
        "id": 12,
        "type": "graph",
        "title": "Pod Start Duration",
        "targets": [{"expr": "histogram_quantile(0.99, sum(rate(kubelet_pod_start_duration_seconds_count[5m])) by (le))", "refId": "A"}],
        "gridPos": {"x": 12, "y": 21, "w": 12, "h": 7},
    }
    _result, query, _yaml = _translate(panel)
    assert "kubelet_pod_start_duration_seconds_bucket" in query
    assert "kubelet_pod_worker_duration_seconds_bucket" in query
    assert "kubelet_pod_start_duration_seconds_count" not in query


def test_12123_rpc_rate_keeps_status_classes():
    panel = {
        "id": 21,
        "type": "graph",
        "title": "RPC Rate",
        "targets": [{"expr": 'sum(rate(rest_client_requests_total{code=~"2.."}[5m]))', "refId": "A"}],
        "gridPos": {"x": 0, "y": 63, "w": 24, "h": 7},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated", result.reasons
    assert '"2xx"' in query and '"5xx"' in query
    assert yaml_panel["esql"]["type"] == "line"
