# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Curated pack for Grafana 12123, Kubernetes / Kubelet."""

from pathlib import Path

import pytest
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
    assert result.status == "migrated_with_warnings", result.reasons
    assert '?instance' not in query
    assert '== "kubelet"' in query
    assert "metrics_path" not in query
    assert yaml_panel["esql"]["type"] == "metric"


def test_12123_up_counts_each_instance_once_and_shows_the_range_minimum():
    # kube-prometheus scrapes each kubelet on several endpoints with job=kubelet;
    # one value per instance keeps Up at the kubelet count.
    panel = {
        "id": 2,
        "type": "singlestat",
        "title": "Up",
        "targets": [{"expr": 'sum(up{job="kubelet", metrics_path="/metrics"})', "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 4, "h": 7},
    }
    result, query, _yaml = _translate(panel)
    lines = [line.strip() for line in query.splitlines()]
    per_instance = next(line for line in lines if "LAST_OVER_TIME" in line)
    assert per_instance.startswith("| STATS up = MAX(LAST_OVER_TIME(")
    assert "TBUCKET(75, ?_tstart, ?_tend)" in per_instance
    assert "| STATS up = SUM(up) BY time_bucket" in lines
    assert "| STATS up = MIN(up)" in lines
    assert any("metrics_path" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("panel_id", "title", "metric"),
    [
        (3, "Running Pods", "kubelet_running_pod_count"),
        (4, "Running Container", "kubelet_running_container_count"),
        (5, "Actual Volume Count", "volume_manager_total_volumes"),
        (6, "Desired Volume Count", "volume_manager_total_volumes"),
    ],
)
def test_12123_singlestats_show_the_range_minimum_per_bucket(panel_id, title, metric):
    # The source valueName is "min". Summing per time bucket also drops series
    # that stopped reporting earlier in the range.
    panel = {
        "id": panel_id,
        "type": "singlestat",
        "title": title,
        "targets": [{"expr": f'sum({metric}{{instance=~"$instance"}})', "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 4, "h": 7},
    }
    result, query, _yaml = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert f"SUM(LAST_OVER_TIME({metric})) BY time_bucket = TBUCKET(75, ?_tstart, ?_tend)" in query
    assert "| STATS value = MIN(value)" in query
    assert "\n        \n" not in query and "\n\n" not in query.strip()


def test_12123_config_error_is_one_five_minute_change_per_kubelet():
    panel = {
        "id": 7,
        "type": "singlestat",
        "title": "Config Error Count",
        "targets": [{"expr": 'sum(rate(kubelet_node_config_error{instance=~"$instance"}[5m]))', "refId": "A"}],
        "gridPos": {"x": 20, "y": 0, "w": 4, "h": 7},
    }
    result, query, _yaml = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    lines = [line.strip() for line in query.splitlines()]
    window = lines.index("| WHERE @timestamp >= ?_tend - 5 minutes")
    stats = next(i for i, line in enumerate(lines) if line.startswith("| STATS"))
    assert window < stats
    assert lines[stats] == "| STATS errors = SUM(DELTA(kubelet_node_config_error)) / 300"
    assert "TBUCKET" not in query


def test_12123_running_pods_follow_the_instance_control():
    panel = {
        "id": 3,
        "type": "singlestat",
        "title": "Running Pods",
        "targets": [{"expr": 'sum(kubelet_running_pod_count{instance=~"$instance"})', "refId": "A"}],
        "gridPos": {"x": 4, "y": 0, "w": 4, "h": 7},
    }
    result, query, _yaml = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
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
    assert "TBUCKET(75, ?_tstart, ?_tend), service.instance.id, operation_type, le\n" in query
    assert any("rate window follows" in reason for reason in result.reasons)
    assert yaml_panel["esql"]["type"] == "line"


def test_12123_pod_start_duration_uses_buckets_not_the_count_series():
    panel = {
        "id": 12,
        "type": "graph",
        "title": "Pod Start Duration",
        "targets": [{"expr": "histogram_quantile(0.99, sum(rate(kubelet_pod_start_duration_seconds_count[5m])) by (le))", "refId": "A"}],
        "gridPos": {"x": 12, "y": 21, "w": 12, "h": 7},
    }
    result, query, _yaml = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "kubelet_pod_start_duration_seconds_bucket" in query
    assert "kubelet_pod_worker_duration_seconds_bucket" in query
    assert "kubelet_pod_start_duration_seconds_count" not in query


def test_12123_pod_start_duration_keeps_each_histogram_on_its_own_bounds():
    # A pod or worker row with no value at an le bound is dropped, so the +Inf
    # fallback is the kind's own top bound and a missing histogram has no series.
    panel = {
        "id": 12,
        "type": "graph",
        "title": "Pod Start Duration",
        "targets": [{"expr": "histogram_quantile(0.99, sum(rate(kubelet_pod_worker_duration_seconds_bucket[5m])) by (instance, le))", "refId": "A"}],
        "gridPos": {"x": 12, "y": 21, "w": 12, "h": 7},
    }
    _result, query, _yaml = _translate(panel)
    lines = [line.strip() for line in query.splitlines()]
    assert 'COALESCE(TO_STRING(pod), "0")' not in query
    assert 'COALESCE(TO_STRING(pod), "")' in query
    drop = lines.index("| WHERE cumulative IS NOT NULL")
    top = next(i for i, line in enumerate(lines) if "top = MAX(bound)" in line)
    assert drop < top


def test_12123_rate_panels_disclose_the_bucket_rate_window():
    path = Path(pkg.__file__).parent / "grafana_12123_k8s_kubelet" / "fidelity_manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    by_id = {panel["id"]: panel for panel in manifest["panels"]}
    for panel_id in (8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 24):
        assert by_id[panel_id]["fidelity"] == "APPROXIMATE", panel_id
        assert "rate window follows the dashboard time range" in by_id[panel_id]["notes"], panel_id
    assert {pid for pid, panel in by_id.items() if panel["fidelity"] == "PERFECT"} == {23, 25}


def test_12123_manifest_notes_match_pack_notes():
    base = Path(pkg.__file__).parent / "grafana_12123_k8s_kubelet"
    pack = yaml.safe_load((base / "pack.yaml").read_text())
    manifest = yaml.safe_load((base / "fidelity_manifest.yaml").read_text())
    by_id = {panel["id"]: panel for panel in manifest["panels"]}
    for override in pack["panel"]["query_overrides"]:
        note = override.get("approximation_note")
        if note:
            assert by_id[override["panel_id"]]["notes"] == note, override["title_match"]
        else:
            assert by_id[override["panel_id"]]["fidelity"] == "PERFECT", override["title_match"]


def test_12123_cgroup_rate_discloses_the_instance_in_the_legend():
    panel = {
        "id": 16,
        "type": "graph",
        "title": "Cgroup manager operation rate",
        "targets": [{"expr": "sum(rate(kubelet_cgroup_manager_duration_seconds_count[5m])) by (instance, operation_type)", "refId": "A", "legendFormat": "{{operation_type}}"}],
        "gridPos": {"x": 0, "y": 35, "w": 12, "h": 7},
    }
    result, query, _yaml = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "CONCAT(TO_STRING(service.instance.id), \" \", TO_STRING(operation_type))" in query
    assert any("legend" in reason for reason in result.reasons)


def test_12123_rpc_rate_keeps_status_classes():
    panel = {
        "id": 21,
        "type": "graph",
        "title": "RPC Rate",
        "targets": [{"expr": 'sum(rate(rest_client_requests_total{code=~"2.."}[5m]))', "refId": "A"}],
        "gridPos": {"x": 0, "y": 63, "w": 24, "h": 7},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert '"2xx"' in query and '"5xx"' in query
    assert yaml_panel["esql"]["type"] == "line"
