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


def _pack_query(panel_id):
    path = Path(pkg.__file__).parent / "grafana_15661_k8s_dashboard" / "pack.yaml"
    pack = yaml.safe_load(path.read_text())
    for override in pack["panel"]["query_overrides"]:
        if override["panel_id"] == panel_id:
            return override["esql_query"]
    raise AssertionError(panel_id)


def test_15661_device_regex_is_unanchored():
    # ES|QL RLIKE is always anchored and reads ^ as a literal character.
    for panel_id in (73, 52):
        query = _pack_query(panel_id)
        assert 'RLIKE "/dev/.*"' in query
        assert 'RLIKE "^' not in query


def test_15661_workload_counts_survive_a_missing_kind():
    for panel_id in (51, 88):
        query = _pack_query(panel_id)
        assert "dep + ds + sts" not in query
        assert "COALESCE(dep, 0) + COALESCE(ds, 0) + COALESCE(sts, 0)" in query
    assert "nodes - COALESCE(custom_taints, 0)" in _pack_query(88)


def test_15661_node_panels_keep_the_source_container_filter():
    # The source filters container!="" only on node panels; "POD" is not excluded.
    for panel_id in (44, 45, 71, 75, 76, 79, 52):
        assert '!= "POD"' not in _pack_query(panel_id)


def test_15661_pod_network_does_not_filter_network_series_by_container():
    # cAdvisor reports pod network on the sandbox, never on app containers.
    for panel_id in (47, 77, 16):
        query = _pack_query(panel_id)
        assert "WHERE {{label:container}} != \"\"" not in query
        assert "WHERE ?Container IS NULL" not in query
    assert "kube_pod_container_info" in _pack_query(77)
    assert "kube_pod_container_info" in _pack_query(16)


def test_15661_restarts_take_node_from_the_pod():
    query = _pack_query(47)
    assert "kube_pod_container_status_restarts_total:counter}} IS NOT NULL" in query.split("\n")[2]
    assert "node_name = MAX({{label:node}})" in query


def test_15661_tables_take_the_newest_bucket_with_a_value():
    # LAST(x, time_bucket) returns NULL when the newest bucket has one sample and
    # irate has nothing to compare. The sort key is precomputed per column because
    # an inline CASE sort key fails in Elasticsearch with a ClassCastException.
    for panel_id in (44, 51, 52, 47, 87, 88):
        query = _pack_query(panel_id)
        assert "LAST(" in query
        assert ", time_bucket)" not in query.replace("CASE(", "").split("LAST(", 1)[1].split("\n", 1)[0]
        assert "__t_" in query


def test_15661_pod_table_keeps_container_and_node_as_row_columns():
    # The datatable rows come from the final STATS BY; container and node are
    # rebuilt after the pod merge and must be grouped again to stay text columns.
    last_stats = [line for line in _pack_query(47).splitlines() if line.startswith("| STATS")][-1]
    assert last_stats.endswith("BY {{label:namespace}}, {{label:pod}}, {{label:container}}, {{label:node}}")
