# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Curated pack for Grafana 11455, K8s / Storage / Volumes / Namespace."""

from pathlib import Path

import yaml

from observability_migration.adapters.source.grafana import curated_packs as pkg
from observability_migration.adapters.source.grafana.curated_packs import find_curated_pack
from observability_migration.adapters.source.grafana.panels import translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig, resolve_pack_for_dashboard
from observability_migration.adapters.source.grafana.schema import SchemaResolver


def _resolve():
    dashboard = {
        "gnetId": 11455,
        "title": "K8s / Storage / Volumes / Namespace",
        "tags": ["openshift", "k8s", "storage"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    return resolved, SchemaResolver(resolved)


def _translate(panel):
    resolved, resolver = _resolve()
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    return result, query, yaml_panel


def test_11455_registry_entry_present():
    entry = find_curated_pack(gnet_id=11455, title="", tags=[])
    assert entry is not None
    assert entry["name"] == "grafana_11455_k8s_storage_volumes_namespace"
    assert entry["gnet_revision"] == 6
    assert entry["dashboard_sha256"].startswith("f655d91f")


def test_11455_fidelity_rows_name_every_panel():
    path = Path(pkg.__file__).parent / "grafana_11455_k8s_storage_volumes_namespace" / "fidelity_manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    assert manifest["gnet_revision"] == 6
    assert len(manifest["panels"]) == 21
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


def test_11455_fill_tile_is_namespace_scoped_daily_growth():
    panel = {
        "id": 59,
        "type": "singlestat",
        "title": "Volumes Full in Week Based on Daily Use Rate",
        "targets": [{"expr": "predict_linear(kubelet_volume_stats_available_bytes[1d], 604800) < 0", "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 14, "h": 3},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "predict_linear" not in query
    assert "24 hours" in query
    assert "?namespace" in query
    assert "openshift" not in query
    assert "available / growth < 7" in query
    assert yaml_panel["esql"]["type"] == "metric"


def test_11455_current_table_stays_on_infrastructure_namespaces():
    panel = {
        "id": 63,
        "type": "table",
        "title": "Volumes Full in Week Based on Daily Use Rate - Current",
        "targets": [{"expr": "predict_linear(kubelet_volume_stats_available_bytes[1w], 604800) < 0", "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 8, "h": 14},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "168 hours" in query
    assert "24 hours" not in query
    assert "openshift-.*" in query
    assert "?namespace" not in query
    assert "available / growth * 7" in query
    assert yaml_panel["esql"]["type"] == "datatable"


def test_11455_bound_pvcs_use_phase_in_the_namespace():
    panel = {
        "id": 4,
        "type": "singlestat",
        "title": "Bound PVCs",
        "targets": [{"expr": 'sum(pv_collector_bound_pvc_count{exported_namespace="$namespace"})', "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 2, "h": 4},
    }
    result, query, _yaml = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "pv_collector_bound_pvc_count" not in query
    assert 'phase == "Bound"' in query
    assert "?namespace" in query


def test_11455_percent_chart_multiplies_by_100():
    panel = {
        "id": 16,
        "type": "graph",
        "title": "Running PVCs % Used",
        "targets": [{"expr": "kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes * 100", "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
    }
    _result, query, yaml_panel = _translate(panel)
    assert "* 100" in query
    assert "?namespace" in query
    assert yaml_panel["esql"]["type"] == "line"


def test_11455_used_bytes_is_a_clean_namespace_gauge():
    panel = {
        "id": 19,
        "type": "graph",
        "title": "All Running PVCs Used Bytes",
        "targets": [{"expr": 'max by (persistentvolumeclaim) (kubelet_volume_stats_used_bytes{namespace="$namespace"})', "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated", result.reasons
    assert "kubelet_volume_stats_used_bytes" in query
    assert "?namespace" in query
    assert yaml_panel["esql"]["type"] == "line"


def test_11455_use_rate_windows_stay_distinct():
    def _panel(title, window, panel_id):
        return {
            "id": panel_id,
            "type": "graph",
            "title": title,
            "targets": [{"expr": f"rate(kubelet_volume_stats_used_bytes[{window}])", "refId": "A"}],
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 6},
        }

    _, hourly, _ = _translate(_panel("Hourly Volume Use Rate", "1h", 43))
    _, daily, _ = _translate(_panel("Daily Volume Use Rate", "1d", 49))
    _, weekly, weekly_panel = _translate(_panel("Weekly Volume Use Rate", "1w", 50))
    assert "1 hour" in hourly and "3600" in hourly and "?namespace" in hourly
    assert "24 hours" in daily and "86400" in daily
    assert "168 hours" in weekly and "604800" in weekly
    assert weekly_panel["esql"]["type"] == "line"


def test_11455_claim_chart_plots_used_and_capacity():
    panel = {
        "id": 20,
        "type": "graph",
        "title": "$persistentvolumeclaim",
        "targets": [
            {"expr": "kubelet_volume_stats_used_bytes", "legendFormat": "Used", "refId": "A"},
            {"expr": "kubelet_volume_stats_capacity_bytes", "legendFormat": "Capacity", "refId": "B"},
        ],
        "gridPos": {"x": 0, "y": 0, "w": 6, "h": 7},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "MV_ZIP" in query
    assert '"Used"' in query and '"Capacity"' in query
    assert "?namespace" in query
    assert yaml_panel["esql"]["type"] == "line"
