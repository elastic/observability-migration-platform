# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Curated pack for Grafana 15661, K8S Dashboard."""

from pathlib import Path

import yaml

from observability_migration.adapters.source.grafana import curated_packs as pkg
from observability_migration.adapters.source.grafana.curated_packs import find_curated_pack
from observability_migration.adapters.source.grafana.panels import translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig, resolve_pack_for_dashboard
from observability_migration.adapters.source.grafana.schema import SchemaResolver


def _translate(panel):
    dashboard = {
        "gnetId": 15661,
        "title": "K8S Dashboard",
        "tags": ["Prometheus", "Kubernetes"],
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


def test_15661_registry_entry_present():
    entry = find_curated_pack(gnet_id=15661, title="", tags=[])
    assert entry is not None
    assert entry["name"] == "grafana_15661_k8s_dashboard"
    assert entry["gnet_revision"] == 2
    assert entry["dashboard_sha256"].startswith("21cbac15")


def test_15661_fidelity_rows_name_every_panel():
    path = Path(pkg.__file__).parent / "grafana_15661_k8s_dashboard" / "fidelity_manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    assert manifest["gnet_revision"] == 2
    assert len(manifest["panels"]) == 33
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


def test_15661_memory_ratio_uses_allocatable_and_the_node_control():
    panel = {
        "id": 44,
        "type": "bargauge",
        "title": "Node Memory Ratio",
        "targets": [{"expr": "sum(container_memory_working_set_bytes)", "refId": "A"}],
        "gridPos": {"x": 0, "y": 1, "w": 4, "h": 4},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated", result.reasons
    assert "?Node" in query
    assert "container_memory_working_set_bytes" in query
    assert "kube_node_status_allocatable" in query
    assert "labels." not in query
    assert yaml_panel["esql"]["type"] == "bar"


def test_15661_heap_series_is_omitted():
    panel = {
        "id": 27,
        "type": "timeseries",
        "title": "Pod Container Memory Usage (Associatable Nodes)",
        "targets": [{"expr": "cass_jvm_heap", "refId": "C"}],
        "gridPos": {"x": 8, "y": 50, "w": 8, "h": 9},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "cass_jvm" not in query
    assert "container_memory_working_set_bytes" in query
    assert "container_memory_rss" in query
    assert yaml_panel["esql"]["type"] == "line"


def test_15661_cluster_stat_folds_taint_keys():
    panel = {
        "id": 88,
        "type": "stat",
        "title": "",
        "targets": [
            {"expr": "count(kube_node_info)", "legendFormat": "Workload", "refId": "F"},
        ],
        "gridPos": {"x": 0, "y": 9, "w": 24, "h": 2},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "Normal Node" in query
    assert "kube_node_spec_taint" in query
    assert yaml_panel["esql"]["type"] == "metric"


def test_15661_namespace_cpu_keeps_the_half_core_threshold():
    panel = {
        "id": 86,
        "type": "timeseries",
        "title": "Namespaces CPU Usage kernel(>0.5)",
        "targets": [{"expr": "sum(irate(container_cpu_usage_seconds_total[2m])) by (namespace)>0.5", "refId": "A"}],
        "gridPos": {"x": 6, "y": 33, "w": 9, "h": 8},
    }
    result, query, _yaml_panel = _translate(panel)
    assert result.status == "migrated", result.reasons
    assert "> 0.5" in query
    assert "IRATE" in query
