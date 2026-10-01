# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Curated pack for Grafana 15758 Kubernetes / Views / Namespaces."""

from __future__ import annotations

from pathlib import Path

import yaml

from observability_migration.adapters.source.grafana.curated_packs import find_curated_pack
from observability_migration.adapters.source.grafana.panels import translate_dashboard, translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig, resolve_pack_for_dashboard
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from tests.test_curated_packs import dashboard_schema_errors

PACK = (
    Path(__file__).resolve().parents[1]
    / "observability_migration/adapters/source/grafana/curated_packs"
    / "grafana_15758_k8s_views_namespaces"
)

_RESOURCE_SERIES = (
    "Running Pods",
    "Services",
    "Ingresses",
    "Deployments",
    "Statefulsets",
    "Daemonsets",
    "Persistent Volume Claims",
    "Horizontal Pod Autoscalers",
    "Configmaps",
    "Secrets",
    "Network Policies",
)


def _load():
    return (
        yaml.safe_load((PACK / "pack.yaml").read_text()),
        yaml.safe_load((PACK / "fidelity_manifest.yaml").read_text()),
    )


def _resolve():
    dashboard = {
        "gnetId": 15758,
        "title": "Kubernetes / Views / Namespaces",
        "tags": ["kubernetes"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    return resolved, SchemaResolver(resolved)


def _panel(panel_id: int, title: str, grafana_type: str) -> dict:
    panel = {
        "id": panel_id,
        "type": grafana_type,
        "title": title,
        "targets": [{"expr": "up", "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
    }
    if grafana_type == "gauge":
        panel["fieldConfig"] = {"defaults": {"unit": "percentunit", "min": 0, "max": 1}}
    return panel


def _translate(panel: dict):
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


def test_15758_registry_entry_present():
    entry = find_curated_pack(gnet_id=15758, title="", tags=[])
    assert entry is not None
    assert entry["name"] == "grafana_15758_k8s_views_namespaces"
    assert entry["gnet_revision"] == 46
    assert entry["dashboard_sha256"] == (
        "2d970440c28ca6b674fb497f4f38ce2ee89effccb9e784447bf4450ca37aa0a8"
    )


def test_15758_fidelity_rows_name_every_panel():
    pack, manifest = _load()
    panels = manifest["panels"]
    assert len(panels) == 25
    assert {row["fidelity"] for row in panels} <= {"PERFECT", "APPROXIMATE", "BEST_EFFORT"}
    overrides = {int(row["panel_id"]): row for row in pack["panel"]["query_overrides"]}
    assert set(overrides) == {int(row["panel_id"]) for row in panels}
    for row in panels:
        assert row["grafana_type"]
        assert row["kibana_type"]
        assert row.get("notes")
        override = overrides[int(row["panel_id"])]
        if row["fidelity"] == "PERFECT":
            assert "delta" not in row
            assert "approximation_note" not in override
        else:
            assert row["delta"] == row["notes"]
            assert override["approximation_note"] == row["notes"]


def test_15758_cpu_gauge_divides_namespace_usage_by_cluster_cores():
    result, query, yaml_panel = _translate(
        _panel(46, "Namespace(s) usage on total cluster CPU in %", "gauge")
    )
    assert result.status == "migrated_with_warnings", result.reasons
    assert "container_cpu_usage_seconds_total" in query
    assert "machine_cpu_cores" in query
    assert 'k8s.container.name != ""' in query
    assert "MV_COUNT(?namespace)" in query
    assert '?namespace == ""' not in query
    assert "denom = SUM(LAST_OVER_TIME(machine_cpu_cores))" in query
    assert "busy / denom" in query
    assert "* 100" not in query
    assert "TBUCKET(30 minutes)" in query
    assert "?_tend - 1 hour" in query
    assert yaml_panel["esql"]["type"] == "gauge"
    assert yaml_panel["esql"]["metric"]["format"]["type"] == "percent"
    assert yaml_panel["esql"]["minimum"]["field"] == "_gauge_min"
    assert yaml_panel["esql"]["maximum"]["field"] == "_gauge_max"


def test_15758_cpu_stat_lists_real_requests_limits_and_cluster_total():
    _result, query, yaml_panel = _translate(
        _panel(62, "Namespace(s) CPU Usage in cores", "stat")
    )
    assert "MV_APPEND(\"Real\", \"Requests\")" in query
    assert "MV_APPEND(\"Limits\", \"Cluster Total\")" in query
    assert "shown_total = SUM(LAST_OVER_TIME(machine_cpu_cores))" in query
    assert " and " not in query
    assert '?namespace == ""' not in query
    assert "MV_ZIP" in query
    assert yaml_panel["esql"]["type"] == "metric"


def test_15758_owner_filter_is_a_pod_prefix_and_namespace_is_multi():
    _result, query, _yaml_panel = _translate(_panel(29, "CPU usage by Pod", "timeseries"))
    assert "MV_COUNT(?namespace)" in query
    assert "MV_CONTAINS(TO_STRING(?namespace)" in query
    assert '?namespace == ""' not in query
    assert "MV_EXPAND __owner" in query
    assert "STARTS_WITH(TO_STRING(k8s.pod.name), TO_STRING(__owner))" in query
    assert 'k8s.container.name != ""' in query
    assert "RATE(container_cpu_usage_seconds_total)" in query


def test_15758_status_reason_stays_cluster_wide():
    result, query, _yaml_panel = _translate(
        _panel(72, "Kubernetes Pods Status Reason", "timeseries")
    )
    assert result.status == "migrated", result.reasons
    assert "kube_pod_status_reason" in query
    assert "?namespace" not in query
    assert "?created_by" not in query


def test_15758_unavailable_replicas_drop_the_owner_prefix():
    _result, query, _yaml_panel = _translate(
        _panel(8, "Replicas unavailable by deployment", "timeseries")
    )
    assert "kube_deployment_status_replicas_unavailable" in query
    assert "?created_by" not in query
    assert "k8s.pod.name" not in query


def test_15758_pods_by_state_scopes_only_ready_and_running():
    _result, query, _yaml_panel = _translate(_panel(5, "Nb of pods by state", "timeseries"))
    assert "Ready = SUM(ready)" in query
    assert "Running = SUM(running)" in query
    assert "Waiting = SUM(waiting)" in query
    assert "`Restarts Total` = SUM(restarts)" in query
    assert "Terminated = SUM(terminated)" in query
    assert "RATE(" not in query
    assert "MAX(CASE(__match, ready, NULL))" in query
    assert "MAX(waiting)" in query
    waiting = query.split("waiting = MAX(waiting)", 1)[1]
    assert "STARTS_WITH" not in waiting


def test_15758_resource_count_names_the_source_series():
    _result, query, _yaml_panel = _translate(
        _panel(32, "Kubernetes Resource Count", "timeseries")
    )
    for name in _RESOURCE_SERIES:
        assert name in query


def test_15758_network_mirrors_transmit_without_a_container_filter():
    _result, query, _yaml_panel = _translate(
        _panel(78, "Network - Bandwidth by pod", "timeseries")
    )
    assert "transmitted = -1 * tx" in query
    assert "k8s.container.name" not in query
    assert "MV_EXPAND __owner" in query
    assert "STARTS_WITH(TO_STRING(k8s.pod.name), TO_STRING(__owner))" in query


def test_15758_overview_layout_matches_the_kibana_grid():
    pack, manifest = _load()
    by_id = {int(row["panel_id"]): row for row in manifest["panels"]}
    sections: list[dict] = []
    current: dict | None = None
    for override in pack["panel"]["layout_overrides"]:
        if "panel_id" not in override:
            current = {"title": override["title_match"], "panels": []}
            sections.append(current)
            continue
        assert current is not None
        current["panels"].append(override)
    panels = []
    for index, section in enumerate(sections):
        panels.append(
            {
                "id": 9000 + index,
                "type": "row",
                "title": section["title"],
                "gridPos": {"x": 0, "y": index * 100, "w": 24, "h": 1},
            }
        )
        for slot, override in enumerate(section["panels"]):
            meta = by_id[int(override["panel_id"])]
            panel = _panel(int(override["panel_id"]), meta["title"], meta["grafana_type"])
            panel["gridPos"] = {"x": 0, "y": index * 100 + 1 + slot, "w": 12, "h": 8}
            panels.append(panel)
    dashboard = {
        "gnetId": 15758,
        "title": "Kubernetes / Views / Namespaces",
        "tags": ["kubernetes", "prometheus"],
        "templating": {
            "list": [
                {
                    "name": "cluster",
                    "type": "query",
                    "query": "label_values(kube_node_info, cluster)",
                    "multi": False,
                },
                {
                    "name": "namespace",
                    "type": "query",
                    "query": "label_values(kube_pod_info, namespace)",
                    "multi": True,
                    "includeAll": True,
                    "allValue": ".*",
                },
                {
                    "name": "created_by",
                    "type": "query",
                    "query": "label_values(kube_pod_info, created_by_name)",
                    "multi": True,
                    "includeAll": True,
                    "allValue": ".*",
                },
                {"name": "resolution", "type": "custom", "query": "30s"},
            ]
        },
        "panels": panels,
    }
    resolved, resolver = _resolve()
    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    yaml_dict = result.dashboard_ir.to_yaml_dict()
    overview = next(panel for panel in yaml_dict["panels"] if panel["title"] == "Overview")
    by_title = {panel["title"]: panel for panel in overview["section"]["panels"]}
    cpu = by_title["Namespace(s) usage on total cluster CPU in %"]
    ram = by_title["Namespace(s) usage on total cluster RAM in %"]
    count = by_title["Kubernetes Resource Count"]
    cores = by_title["Namespace(s) CPU Usage in cores"]
    assert cpu["size"] == {"w": 12, "h": 14}
    assert cpu["position"] == {"x": 0, "y": 0}
    assert ram["position"] == {"x": 12, "y": 0}
    assert count["size"] == {"w": 24, "h": 22}
    assert count["position"] == {"x": 24, "y": 0}
    assert cores["position"] == {"x": 0, "y": 14}
    controls = {control.get("variable_name") for control in yaml_dict["controls"]}
    assert {"cluster", "namespace", "created_by"} <= controls
    assert "resolution" not in controls
    errors = dashboard_schema_errors(yaml_dict["panels"])
    assert errors == [], errors
