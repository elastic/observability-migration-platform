# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Curated pack for Grafana 11455, K8s / Storage / Volumes / Namespace."""

from pathlib import Path

import yaml

from observability_migration.adapters.source.grafana import curated_packs as pkg
from observability_migration.adapters.source.grafana.curated_packs import find_curated_pack
from observability_migration.adapters.source.grafana.panels import translate_dashboard, translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig, resolve_pack_for_dashboard
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from tests.test_curated_packs import dashboard_schema_errors

_NAMESPACE_ALL = '?namespace == ".*"'


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
    assert len(manifest["panels"]) == 16
    assert not {73, 74, 75, 76, 77} & {panel["id"] for panel in manifest["panels"]}
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


def test_11455_used_bytes_keeps_namespace_per_claim():
    panel = {
        "id": 19,
        "type": "graph",
        "title": "All Running PVCs Used Bytes",
        "targets": [{"expr": 'max by (persistentvolumeclaim) (kubelet_volume_stats_used_bytes{namespace="$namespace"})', "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "kubelet_volume_stats_used_bytes" in query
    assert _NAMESPACE_ALL in query
    # Same-named claims in different namespaces must not merge when the
    # namespace control matches all.
    by_clause = query.split("BY time_bucket = TBUCKET", 1)[1].split("\n", 1)[0]
    assert "namespace" in by_clause
    assert '" ("' in query
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
    # A 24-hour bucket starts at local midnight, before the default 6-hour
    # range, so Lens clips its only point. The daily and weekly windows slide
    # over hourly buckets instead.
    assert "TBUCKET(24 hours)" not in daily and "TBUCKET(24 hours)" not in weekly
    assert "TBUCKET(1 hour)" in daily and "TBUCKET(1 hour)" in weekly
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
    assert _NAMESPACE_ALL in query
    # The claim variable is hidden in Grafana; binding it would surface an
    # unscoped claim control.
    assert "?persistentvolumeclaim" not in query
    assert yaml_panel["esql"]["type"] == "line"


def test_11455_pvc_stats_lists_claims_without_kubelet_stats():
    panel = {
        "id": 8,
        "type": "table",
        "title": "PVC Stats",
        "targets": [{"expr": 'kube_persistentvolumeclaim_info{namespace="$namespace"}', "refId": "E", "instant": True}],
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
    }
    result, query, yaml_panel = _translate(panel)
    assert result.status == "migrated_with_warnings", result.reasons
    assert "kube_persistentvolumeclaim_info" in query
    assert "NULLS LAST" in query
    # ES|QL prunes an unread aggregate, and the TS STATS then drops series
    # whose remaining aggregates are all null, so the info aggregate must be
    # read by a later stage for pending claims to survive.
    assert "WHERE info IS NOT NULL" in query
    assert yaml_panel["esql"]["type"] == "datatable"


def _rev6_dashboard():
    """Revision-6 templating verbatim, plus the panels the controls touch."""

    def _panel(panel_id, title, panel_type, expr, x=0, y=0, **extra):
        return {
            "id": panel_id,
            "type": panel_type,
            "title": title,
            "datasource": "$DS_OPENSHIFT_PROMETHEUS",
            "targets": [{"expr": expr, "refId": "A"}],
            "gridPos": {"x": x, "y": y, "w": 6, "h": 7},
            **extra,
        }

    used = 'kubelet_volume_stats_used_bytes {namespace="$namespace"}'
    claim_used = (
        'max by (persistentvolumeclaim,namespace) (kubelet_volume_stats_used_bytes '
        '{namespace="$namespace", persistentvolumeclaim="$persistentvolumeclaim"})'
    )
    clones = [
        _panel(pid, "$persistentvolumeclaim", "graph", claim_used, x=6 * (i + 1), y=20, repeatPanelId=20,
               scopedVars={"persistentvolumeclaim": {"text": f"logging-es-{i}", "value": f"logging-es-{i}"}})
        for i, pid in enumerate((73, 74, 75, 76, 77))
    ]
    return {
        "gnetId": 11455,
        "title": "K8s / Storage / Volumes / Namespace",
        "tags": ["openshift", "k8s", "storage"],
        "templating": {
            "list": [
                {"name": "DS_OPENSHIFT_PROMETHEUS", "type": "datasource", "query": "prometheus",
                 "current": {"text": "openshift-prometheus", "value": "openshift-prometheus"}, "hide": 0},
                {"name": "namespace", "type": "query", "query": "label_values(kube_namespace_created,namespace)",
                 "current": {}, "includeAll": False, "allValue": "", "multi": False, "options": [], "hide": 0},
                {"name": "pvc_percent_used_warning_threshold", "type": "textbox", "query": "80",
                 "current": {"text": "80", "value": "80"},
                 "options": [{"selected": True, "text": "80", "value": "80"}], "hide": 0},
                {"name": "persistentvolumeclaim", "type": "query",
                 "query": 'label_values(kubelet_volume_stats_used_bytes {namespace="$namespace"}, persistentvolumeclaim)',
                 "current": {}, "includeAll": True, "allValue": "", "multi": False, "options": [], "hide": 2},
            ]
        },
        "panels": [
            _panel(10, "Running PVCs Above % Used Warning Threshold", "singlestat", used),
            _panel(65, "Running PVCs Above % Used Warning Threshold", "graph", used, y=7),
            _panel(8, "PVC Stats", "table", 'kube_persistentvolumeclaim_info{namespace="$namespace"}', y=14),
            _panel(20, "$persistentvolumeclaim", "graph", claim_used, y=20, repeat="persistentvolumeclaim",
                   repeatDirection="h",
                   scopedVars={"persistentvolumeclaim": {"text": "logging-es-1", "value": "logging-es-1"}}),
            *clones,
        ],
    }


def _leaves(panels):
    for panel in panels:
        if "section" in panel:
            yield from _leaves(panel["section"]["panels"])
        else:
            yield panel


def test_11455_rev6_controls_and_repeat_clones():
    resolved, resolver = _resolve()
    result = translate_dashboard(
        _rev6_dashboard(),
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    yaml_dict = result.dashboard_ir.to_yaml_dict()
    controls = {control.get("variable_name"): control for control in yaml_dict["controls"]}
    # The hidden repeat variable gets no control; namespace opens on "all".
    assert "persistentvolumeclaim" not in controls
    assert controls["namespace"]["default"] == ".*"

    leaves = list(_leaves(yaml_dict["panels"]))
    titles = [panel.get("title") for panel in leaves]
    # The five saved repeat clones are not migrated.
    assert titles.count("Used and capacity") == 1, titles
    # The two same-titled threshold panels keep their own overrides.
    assert "PVCs above 80% used" in titles
    assert "PVCs above 80% over time" in titles
    by_title = {panel["title"]: panel for panel in leaves}
    assert by_title["PVCs above 80% used"]["esql"]["type"] == "metric"
    assert by_title["PVCs above 80% over time"]["esql"]["type"] == "line"

    for panel in leaves:
        query = (panel.get("esql") or {}).get("query") or ""
        if "?namespace" in query:
            assert _NAMESPACE_ALL in query, panel["title"]
        assert "?persistentvolumeclaim" not in query, panel["title"]
    errors = dashboard_schema_errors(yaml_dict["panels"])
    assert errors == [], errors
