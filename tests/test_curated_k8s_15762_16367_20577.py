# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Curated packs for Grafana 15762, 16367, and 20577."""

import itertools
import json
from pathlib import Path

from observability_migration.adapters.source.grafana.curated_packs import find_curated_pack
from observability_migration.adapters.source.grafana.panels import (
    translate_dashboard,
    translate_panel,
    translate_variables,
)
from observability_migration.adapters.source.grafana.rules import RulePackConfig, resolve_pack_for_dashboard
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.targets.kibana.dashboards_api import build_dashboard_payload_from_ir

DASHBOARD_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "dashboards" / "schema.json"


def _errors(panels):
    import jsonschema

    schema = json.loads(DASHBOARD_SCHEMA_PATH.read_text())
    doc = {"dashboards": [{"name": "pack-probe", "panels": panels}]}
    return [
        f"{'/'.join(str(part) for part in error.path)}: {error.message}"
        for error in jsonschema.Draft202012Validator(schema).iter_errors(doc)
    ]


def _resolve(gnet_id, title, tags):
    dashboard = {"gnetId": gnet_id, "title": title, "tags": tags}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    return resolved, SchemaResolver(resolved)


def _translate(gnet_id, title, tags, panel):
    resolved, resolver = _resolve(gnet_id, title, tags)
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    query = ((yaml_panel or {}).get("esql") or {}).get("query") or ""
    return result, query, yaml_panel


def _leaf_panels(panels):
    found = []
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            found.extend(_leaf_panels(section.get("panels") or []))
            continue
        found.append(panel)
    return found


def test_15762_registry_entry_present():
    entry = find_curated_pack(gnet_id=15762, title="", tags=[])
    assert entry is not None
    assert entry["name"] == "grafana_15762_k8s_system_coredns"
    assert entry["gnet_revision"] == 22


def test_15762_absent_build_info_does_not_list_every_job():
    resolved, resolver = _resolve(15762, "Kubernetes / System / CoreDNS", ["kubernetes"])
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = {
        "service.name": {"keyword": {"type": "keyword", "aggregatable": True}},
        "up": {"double": {"type": "double"}},
        "process_cpu_seconds_total": {"double": {"type": "double"}},
    }
    controls = translate_variables(
        [{
            "type": "query",
            "name": "job",
            "multi": True,
            "current": {},
            "query": 'label_values(coredns_build_info{cluster="$cluster"}, job)',
        }],
        datasource_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert len(controls) == 1
    assert "__no_matching_series__" in controls[0]["query"]
    assert "coredns_build_info" not in controls[0]["query"]
    assert controls[0]["query"].startswith("ROW ")
    assert "FROM " not in controls[0]["query"]
    assert controls[0]["default"] == ["__no_matching_series__"]


def test_15762_absent_build_info_scopes_job_to_request_counter():
    from observability_migration.adapters.source.grafana.runtime_features import (
        ESQL_NAMED_PARAM_BINDING,
        set_runtime_feature,
    )

    resolved, resolver = _resolve(15762, "Kubernetes / System / CoreDNS", ["kubernetes"])
    set_runtime_feature(
        resolved,
        ESQL_NAMED_PARAM_BINDING,
        supported=True,
        source="probe",
        confidence="verified",
        reason="live cluster binds named params",
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = {
        "service.name": {"keyword": {"type": "keyword", "aggregatable": True}},
        "up": {"double": {"type": "double"}},
        "coredns_dns_requests_total": {"double": {"type": "double"}},
    }
    controls = translate_variables(
        [{
            "type": "query",
            "name": "job",
            "multi": True,
            "current": {},
            "query": 'label_values(coredns_build_info{cluster="$cluster"}, job)',
        }],
        datasource_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert len(controls) == 1
    assert "coredns_dns_requests_total" in controls[0]["query"]
    assert "__no_matching_series__" not in controls[0]["query"]
    assert controls[0].get("default") != ["__no_matching_series__"]


def test_15762_packet_size_is_sum_over_count_and_heatmaps_keep_finite_buckets():
    _result, query, yaml_panel = _translate(
        15762,
        "Kubernetes / System / CoreDNS",
        ["kubernetes"],
        {
            "id": 7,
            "type": "timeseries",
            "title": "CoreDNS - Average Packet Size  ($protocol)",
            "fieldConfig": {"defaults": {"unit": "bytes"}},
            "targets": [{"expr": "sum(rate(coredns_dns_request_size_bytes_sum[5m]))", "refId": "A"}],
            "gridPos": {"x": 12, "y": 11, "w": 12, "h": 8},
        },
    )
    assert "coredns_dns_request_size_bytes_sum" in query
    assert "coredns_dns_request_size_bytes_count" in query
    assert "total / n" in query
    assert "?protocol" in query
    assert yaml_panel["title"] == "CoreDNS - Average Packet Size"
    assert yaml_panel["esql"]["type"] == "area"

    _result, heatmap, yaml_panel = _translate(
        15762,
        "Kubernetes / System / CoreDNS",
        ["kubernetes"],
        {
            "id": 28,
            "type": "heatmap",
            "title": "CoreDNS - DNS request size",
            "targets": [{"expr": "sum(increase(coredns_dns_request_size_bytes_bucket[5m])) by (le)", "refId": "A"}],
            "gridPos": {"x": 12, "y": 43, "w": 12, "h": 10},
        },
    )
    assert '!= "0"' in heatmap
    assert "TO_DOUBLE" in heatmap
    # +Inf is filtered before the conversion so TO_DOUBLE never fails (and
    # never attaches an evaluation warning to the panel).
    assert heatmap.index('!= "+Inf"') < heatmap.index("TO_DOUBLE")
    assert "cumulative" in " ".join(str(item) for item in (_result.reasons or []) + (_result.notes or []))
    assert yaml_panel["esql"]["type"] == "heatmap"
    assert yaml_panel["esql"]["y_axis"]["field"] == "bucket"
    assert yaml_panel["esql"]["metric"]["field"] == "observations"


def test_15762_cache_size_is_a_count_not_bytes():
    _result, query, yaml_panel = _translate(
        15762,
        "Kubernetes / System / CoreDNS",
        ["kubernetes"],
        {
            "id": 15,
            "type": "timeseries",
            "title": "CoreDNS - Cache Size",
            "fieldConfig": {"defaults": {"unit": "bytes"}},
            "targets": [{"expr": "sum(coredns_cache_entries) by (type)", "refId": "A"}],
            "gridPos": {"x": 12, "y": 35, "w": 12, "h": 8},
        },
    )
    assert "coredns_cache_entries" in query
    metrics = yaml_panel["esql"]["metrics"]
    assert metrics[0]["format"]["type"] == "number"
    assert metrics[0]["format"].get("compact") is True
    assert "bytes" not in metrics[0]["format"].get("type", "")


def test_15762_dashboard_matches_the_schema():
    panels = []
    specs = [
        (25, "stat", "CoreDNS - Health Status", 0, 0, 24, 3),
        (19, "timeseries", "CoreDNS - CPU Usage by instance", 0, 3, 12, 8),
        (21, "timeseries", "CoreDNS - Memory Usage by instance", 12, 3, 12, 8),
        (9, "timeseries", "CoreDNS - Total DNS Requests ($protocol)", 0, 11, 12, 8),
        (7, "timeseries", "CoreDNS - Average Packet Size  ($protocol)", 12, 11, 12, 8),
        (2, "timeseries", "CoreDNS - Requests by type", 0, 19, 12, 8),
        (4, "timeseries", "CoreDNS - Requests by return code", 12, 19, 12, 8),
        (23, "timeseries", "CoreDNS - Total Forward Requests", 0, 27, 12, 8),
        (13, "timeseries", "CoreDNS - DNS Errors", 12, 27, 12, 8),
        (17, "timeseries", "CoreDNS - Cache Hits / Misses", 0, 35, 12, 8),
        (15, "timeseries", "CoreDNS - Cache Size", 12, 35, 12, 8),
        (27, "heatmap", "CoreDNS - DNS request duration", 0, 43, 12, 10),
        (28, "heatmap", "CoreDNS - DNS request size", 12, 43, 12, 10),
        (29, "heatmap", "CoreDNS - DNS response size", 0, 53, 12, 10),
    ]
    for panel_id, panel_type, title, x, y, w, h in specs:
        panels.append({
            "id": panel_id,
            "type": panel_type,
            "title": title,
            "fieldConfig": {"defaults": {"custom": {"fillOpacity": 25, "stacking": {"mode": "none"}}}},
            "targets": [{"expr": "up", "refId": "A"}],
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
        })
    resolved, resolver = _resolve(15762, "Kubernetes / System / CoreDNS", ["kubernetes", "prometheus"])
    result = translate_dashboard(
        {"gnetId": 15762, "title": "Kubernetes / System / CoreDNS", "tags": ["kubernetes"], "panels": panels},
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    yaml_panels = result.dashboard_ir.to_yaml_dict()["panels"]
    leaves = _leaf_panels(yaml_panels)
    assert len(leaves) == 14
    assert all((panel.get("esql") or {}).get("type") for panel in leaves)
    assert _errors(yaml_panels) == []


def test_16367_quota_table_uses_raw_cadvisor_and_requests():
    entry = find_curated_pack(gnet_id=16367, title="", tags=[])
    assert entry["name"] == "grafana_16367_k8s_node_pods"
    assert entry["gnet_revision"] == 1
    _result, query, yaml_panel = _translate(
        16367,
        "Kubernetes / Compute Resources / Node (Pods)",
        ["kubernetes-mixin"],
        {
            "id": 2,
            "type": "table-old",
            "title": "CPU Quota",
            "targets": [{"expr": "sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)", "refId": "A"}],
            "gridPos": {"x": 0, "y": 9, "w": 24, "h": 7},
        },
    )
    assert "container_cpu_usage_seconds_total" in query
    assert "kube_pod_container_resource_requests" in query
    assert "kube_pod_container_resource_limits" in query
    assert "node_namespace_pod_container" not in query
    assert "* 100" in query
    assert yaml_panel["esql"]["type"] == "datatable"
    assert "?node" in query
    # cAdvisor and kube-state-metrics are separate series: requests/limits are
    # their own FORK branches, scoped to the cpu resource.
    assert "FORK" in query
    assert query.count('== "cpu"') == 2
    assert "container_cpu_usage_seconds_total}} IS NOT NULL" not in query.split("FORK")[0]

    _result, memory, _yaml = _translate(
        16367,
        "Kubernetes / Compute Resources / Node (Pods)",
        ["kubernetes-mixin"],
        {
            "id": 4,
            "type": "table-old",
            "title": "Memory Quota",
            "targets": [{"expr": "sum(container_memory_working_set_bytes) by (pod)", "refId": "A"}],
            "gridPos": {"x": 0, "y": 25, "w": 24, "h": 7},
        },
    )
    assert "FORK" in memory
    assert memory.count('== "memory"') == 2
    assert '== "cpu"' not in memory


def _translate_16367():
    def chart(panel_id, title, y):
        return {
            "id": panel_id,
            "type": "graph",
            "title": title,
            "yaxes": [{"format": "bytes" if "Memory" in title else "short", "show": True}, {"show": False}],
            "legend": {"show": True, "rightSide": True},
            "targets": [{"expr": "up", "refId": "A"}],
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 7},
        }

    panels = [
        {"id": 5, "type": "row", "title": "CPU Usage", "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0}, "panels": []},
        chart(1, "CPU Usage", 1),
        {"id": 6, "type": "row", "title": "CPU Quota", "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 8}, "panels": []},
        {"id": 2, "type": "table-old", "title": "CPU Quota", "targets": [{"expr": "up", "refId": "A"}], "gridPos": {"x": 0, "y": 9, "w": 24, "h": 7}},
        {"id": 7, "type": "row", "title": "Memory Usage", "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 16}, "panels": []},
        chart(3, "Memory Usage (w/o cache)", 17),
        {"id": 8, "type": "row", "title": "Memory Quota", "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 24}, "panels": []},
        {"id": 4, "type": "table-old", "title": "Memory Quota", "targets": [{"expr": "up", "refId": "A"}], "gridPos": {"x": 0, "y": 25, "w": 24, "h": 7}},
    ]
    resolved, resolver = _resolve(16367, "Kubernetes / Compute Resources / Node (Pods)", ["kubernetes-mixin"])
    return translate_dashboard(
        {"gnetId": 16367, "title": "Kubernetes / Compute Resources / Node (Pods)", "tags": ["kubernetes-mixin"], "panels": panels},
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )


def test_16367_dashboard_matches_the_schema():
    result = _translate_16367()
    yaml_panels = result.dashboard_ir.to_yaml_dict()["panels"]
    leaves = _leaf_panels(yaml_panels)
    assert {panel["title"] for panel in leaves} == {
        "CPU Usage",
        "CPU Quota",
        "Memory Usage (w/o cache)",
        "Memory Quota",
    }
    cpu = next(panel for panel in leaves if panel["title"] == "CPU Usage")
    assert cpu["esql"]["type"] == "line"
    assert "max capacity" in cpu["esql"]["query"]
    assert _errors(yaml_panels) == []


def test_16367_layout_keeps_the_source_row_order():
    # Three of the four source rows hold a single panel whose title repeats the
    # row title, so they are flattened to the top level and the pack's layout
    # positions are dashboard-absolute. With section-relative y they all land on
    # y 0, and the overlap resolver breaks the tie by title -- CPU Quota first.
    result = _translate_16367()
    top_level = result.dashboard_ir.to_yaml_dict()["panels"]
    assert [
        (panel["title"], "section" if panel.get("section") else "panel")
        for panel in top_level
    ] == [
        ("CPU Usage", "panel"),
        ("CPU Quota", "panel"),
        ("Memory Usage", "section"),
        ("Memory Quota", "panel"),
    ]
    section = next(panel for panel in top_level if panel.get("section"))
    assert [child["title"] for child in section["section"]["panels"]] == [
        "Memory Usage (w/o cache)"
    ]

    # Every top-level panel is full width, so "bottom of one is at or above the
    # top of the next" is both strictly increasing y and non-overlapping.
    stacked = [panel for panel in top_level if not panel.get("section")]
    assert {panel["position"]["x"] for panel in stacked} == {0}
    assert {panel["size"]["w"] for panel in stacked} == {48}
    for upper, lower in itertools.pairwise(stacked):
        assert upper["position"]["y"] + upper["size"]["h"] <= lower["position"]["y"]

    # The native payload is what Kibana receives: a section's y is derived from
    # the panels before it, so the section must clear CPU Quota and still leave
    # Memory Quota strictly below it (a tie lets Kibana reorder the two).
    payload, _counts, _reasons = build_dashboard_payload_from_ir(result.dashboard_ir)
    native = [
        (
            panel.get("title") if "panels" in panel else (panel.get("config") or {}).get("title"),
            panel["grid"],
        )
        for panel in payload["panels"]
    ]
    assert [title for title, _grid in native] == [
        "CPU Usage",
        "CPU Quota",
        "Memory Usage",
        "Memory Quota",
    ]
    grids = dict(native)
    assert grids["Memory Usage"]["y"] >= grids["CPU Quota"]["y"] + grids["CPU Quota"]["h"]
    assert grids["Memory Usage"]["y"] < grids["Memory Quota"]["y"]


def test_20577_registry_and_cloudwatch_panels_are_esql():
    entry = find_curated_pack(gnet_id=20577, title="AWS - EKS", tags=["aws", "eks"])
    assert entry["name"] == "grafana_20577_aws_eks"
    assert entry["gnet_revision"] == 1
    _result, query, yaml_panel = _translate(
        20577,
        "AWS - EKS",
        ["aws", "eks"],
        {
            "id": 25,
            "type": "gauge",
            "title": "Current Node CPU %",
            "datasource": {"type": "cloudwatch", "uid": "cw"},
            "fieldConfig": {
                "defaults": {
                    "unit": "percent",
                    "thresholds": {
                        "mode": "absolute",
                        "steps": [
                            {"color": "green", "value": None},
                            {"color": "yellow", "value": 60},
                            {"color": "orange", "value": 80},
                            {"color": "red", "value": 90},
                        ],
                    },
                }
            },
            "targets": [{"metricName": "node_cpu_utilization", "namespace": "ContainerInsights", "refId": "A"}],
            "gridPos": {"x": 0, "y": 7, "w": 6, "h": 5},
        },
    )
    assert "node_cpu_utilization" in query
    assert yaml_panel["esql"]["type"] == "gauge"
    assert "4xxErrors" not in query

    _result, logs, table = _translate(
        20577,
        "AWS - EKS",
        ["aws"],
        {
            "id": 14,
            "type": "table",
            "title": "[EKS Cluster Logs] - ${datasource}",
            "datasource": {"type": "cloudwatch", "uid": "cw"},
            "targets": [{"queryMode": "Logs", "expression": "fields @message", "refId": "A"}],
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
        },
    )
    assert '== "eks-cluster"' in logs
    assert table["esql"]["type"] == "datatable"


def test_20577_dashboard_matches_the_schema():
    gauge = {
        "unit": "percent",
        "thresholds": {
            "mode": "absolute",
            "steps": [
                {"color": "green", "value": None},
                {"color": "yellow", "value": 60},
                {"color": "red", "value": 90},
            ],
        },
    }
    specs = [
        (24, "stat", "Current Nodes", None),
        (18, "stat", "Current Pods", None),
        (17, "stat", "Current Containers", None),
        (31, "stat", "Nodes - Max Bytes", "decbytes"),
        (37, "stat", "Nodes - Max Filesystem Utilization", "percent"),
        (35, "stat", "Pods - Max Bytes Sent", "decbytes"),
        (33, "stat", "Pods - Max Bytes Received", "decbytes"),
        (32, "stat", "Container Restarts", None),
        (30, "stat", "Nodes - Total Bytes", "decbytes"),
        (36, "stat", "Pods - Total Bytes Sent", "decbytes"),
        (34, "stat", "Pods - Total Bytes Received", "decbytes"),
        (25, "gauge", "Current Node CPU %", gauge),
        (26, "gauge", "Current Node Mem %", gauge),
        (27, "gauge", "Current Pod CPU %", gauge),
        (28, "gauge", "Current Pod Mem %", gauge),
        (16, "timeseries", "Node Utilization %'s", "percent"),
        (19, "timeseries", "Node Utilization %'s (by IP)", "percent"),
        (23, "timeseries", "Node Network Total Bytes", "decbytes"),
        (22, "timeseries", "Pods - Historical", None),
        (20, "timeseries", "Containers - Historical", None),
        (39, "timeseries", "Container Restarts", None),
        (38, "timeseries", "Average Filesystem Utilization - By Node", "percent"),
        (14, "table", "[EKS Cluster Logs] - ${datasource}", None),
        (1, "table", "[Fluentbit Cloudwatch Logs] -  ${datasource}", None),
        (4, "timeseries", "S3 - Error Counts", None),
        (11, "timeseries", "S3 - Latency", "ms"),
        (13, "timeseries", "CloudFront Requests", None),
        (5, "timeseries", "CloudFront Error Rates (by Range)", None),
        (12, "timeseries", "CloudFront Error Rates (by Code)", None),
        (6, "timeseries", "SQS Messages", None),
        (10, "timeseries", "Panel Title", None),
    ]
    rows = [
        (15, "EKS (Summary)", False),
        (29, "EKS (Historical)", False),
        (40, "EKS (Additional Alerts)", True),
        (2, "EKS (Logs)", True),
        (3, "S3", True),
        (7, "Cloudfront", True),
        (8, "SQS", True),
        (9, "Secrets Manager", True),
    ]
    panels = []
    for row_id, title, collapsed in rows:
        panels.append({
            "id": row_id,
            "type": "row",
            "title": title,
            "collapsed": collapsed,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": 0},
            "panels": [],
        })
    for panel_id, panel_type, title, unit in specs:
        field_config = {"defaults": unit} if isinstance(unit, dict) else {"defaults": {"unit": unit} if unit else {}}
        panels.append({
            "id": panel_id,
            "type": panel_type,
            "title": title,
            "datasource": {"type": "cloudwatch", "uid": "cw"},
            "fieldConfig": field_config,
            "targets": [{"metricName": "cluster_node_count", "refId": "A"}],
            "gridPos": {"x": 0, "y": 1, "w": 6, "h": 6},
        })
    resolved, resolver = _resolve(20577, "AWS - EKS", ["aws", "eks"])
    result = translate_dashboard(
        {"gnetId": 20577, "title": "AWS - EKS", "tags": ["aws", "eks"], "panels": panels},
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    yaml_panels = result.dashboard_ir.to_yaml_dict()["panels"]
    leaves = _leaf_panels(yaml_panels)
    assert len(leaves) == 31
    assert all(panel.get("esql") for panel in leaves)
    renamed = next(panel for panel in leaves if panel["title"] == "Node CPU % by node")
    assert renamed["esql"]["type"] == "bar"
    assert renamed["esql"]["mode"] == "unstacked"
    secrets = next(panel for panel in leaves if panel["title"] == "Secrets Manager")
    assert "secretsmanager_resource_count" in secrets["esql"]["query"]
    logs = [panel for panel in leaves if panel["title"] in {"EKS cluster logs", "Fluent Bit logs"}]
    assert len(logs) == 2
    for panel in logs:
        # One row per log event, not one row per distinct message.
        assert "STATS" not in panel["esql"]["query"]
    # CloudWatch data is not in the prometheus dataset.
    assert resolved.metrics_dataset_filter == ""
    assert not any(
        (item or {}).get("field") == "data_stream.dataset"
        for item in (result.dashboard_ir.to_yaml_dict().get("filters") or [])
    )
    assert _errors(yaml_panels) == []




def test_primary_format_only_restamps_a_single_series_xy():
    # Existing packs set primary_format on panels that can emit several XY
    # series; those columns keep their own formats. Only the metric primary or
    # the one metric of a single-series XY chart takes the pack format.
    resolved, resolver = _resolve(15762, "Kubernetes / System / CoreDNS", ["kubernetes"])
    base = "TS metrics-* | WHERE {{metric:coredns_cache_hits_total:counter}} IS NOT NULL"
    resolved.panel_query_overrides.extend([
        {
            "title_match": "Probe two series",
            "kibana_type_override": "line",
            "primary_format": "bytes",
            "esql_query": base + " | STATS a = SUM(RATE({{metric:coredns_cache_hits_total:counter}})), b = SUM(RATE({{metric:coredns_cache_hits_total:counter}})) BY time_bucket = TBUCKET(75, ?_tstart, ?_tend) | KEEP time_bucket, a, b",
        },
        {
            "title_match": "Probe one series",
            "kibana_type_override": "line",
            "primary_format": "bytes",
            "esql_query": base + " | STATS a = SUM(RATE({{metric:coredns_cache_hits_total:counter}})) BY time_bucket = TBUCKET(75, ?_tstart, ?_tend) | KEEP time_bucket, a",
        },
    ])
    formats = {}
    for panel_id, title in ((901, "Probe two series"), (902, "Probe one series")):
        yaml_panel, _result = translate_panel(
            {
                "id": panel_id,
                "type": "timeseries",
                "title": title,
                "targets": [{"expr": "up", "refId": "A"}],
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            },
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=resolved,
            resolver=resolver,
        )
        formats[title] = [(metric.get("format") or {}).get("type") for metric in yaml_panel["esql"]["metrics"]]
    assert formats["Probe one series"] == ["bytes"]
    assert len(formats["Probe two series"]) == 2
    assert "bytes" not in formats["Probe two series"]
