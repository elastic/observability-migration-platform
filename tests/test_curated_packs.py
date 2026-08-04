# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the curated dashboard pack registry and resolution engine."""

import json
from pathlib import Path


from observability_migration.adapters.source.grafana.curated_packs import (
    find_curated_pack,
    load_curated_registry,
)
from observability_migration.adapters.source.grafana.panels import (
    _apply_panel_layout_overrides_recursively,
    _materialize_curated_query_override,
    _retarget_esql_param_controls_to_panel_bindings,
    translate_dashboard,
    translate_panel,
)
from observability_migration.adapters.source.grafana.rules import (
    RulePackConfig,
    resolve_pack_for_dashboard,
)
from observability_migration.adapters.source.grafana.schema import SchemaResolver

# ---------------------------------------------------------------------------
# Registry loader
# ---------------------------------------------------------------------------

def test_registry_loads():
    entries = load_curated_registry()
    assert isinstance(entries, list)
    assert len(entries) >= 3


def test_registry_entry_has_required_fields():
    entries = load_curated_registry()
    for entry in entries:
        assert "gnet_id" in entry, f"Missing gnet_id in {entry}"
        assert "name" in entry
        assert "path" in entry
        assert "gnet_revision" in entry
        assert "dashboard_sha256" in entry


# ---------------------------------------------------------------------------
# find_curated_pack — by gnetId
# ---------------------------------------------------------------------------

def test_find_763_by_gnet_id():
    entry = find_curated_pack(gnet_id=763, title="", tags=[])
    assert entry is not None
    assert entry["gnet_id"] == 763
    assert entry["name"] == "grafana_763_redis_exporter"


def test_find_18405_by_gnet_id():
    entry = find_curated_pack(gnet_id=18405, title="", tags=[])
    assert entry is not None
    assert entry["gnet_id"] == 18405
    assert entry["name"] == "grafana_18405_redis_enterprise"


def test_find_1860_by_gnet_id():
    entry = find_curated_pack(gnet_id=1860, title="", tags=[])
    assert entry is not None
    assert entry["gnet_id"] == 1860
    assert entry["name"] == "grafana_1860_node_exporter_full"


def test_find_18406_by_gnet_id():
    entry = find_curated_pack(gnet_id=18406, title="", tags=[])
    assert entry is not None
    assert entry["gnet_id"] == 18406
    assert entry["name"] == "grafana_18406_redis_cloud"


def test_find_returns_none_for_unknown():
    entry = find_curated_pack(gnet_id=99999, title="Unknown Dashboard", tags=[])
    assert entry is None


def test_find_returns_none_when_no_args():
    entry = find_curated_pack(gnet_id=None, title="", tags=[])
    assert entry is None


# ---------------------------------------------------------------------------
# find_curated_pack — title+tags fallback
# ---------------------------------------------------------------------------

def test_find_763_by_title_fallback():
    entry = find_curated_pack(
        gnet_id=None,
        title="Redis Dashboard for Prometheus Redis Exporter 1.x",
        tags=["prometheus", "redis"],
    )
    assert entry is not None
    assert entry["gnet_id"] == 763


def test_find_18405_by_title_fallback():
    entry = find_curated_pack(
        gnet_id=None,
        title="Redis Enterprise: Cluster Status",
        tags=[],
    )
    assert entry is not None
    assert entry["gnet_id"] == 18405


def test_find_1860_by_title_fallback():
    entry = find_curated_pack(
        gnet_id=None,
        title="Node Exporter Full",
        tags=["prometheus"],
    )
    assert entry is not None
    assert entry["gnet_id"] == 1860


def test_find_18406_by_title_fallback():
    entry = find_curated_pack(
        gnet_id=None,
        title="Redis Cloud: Subscription Status",
        tags=["RLEC"],
    )
    assert entry is not None
    assert entry["gnet_id"] == 18406


def test_title_fallback_case_insensitive():
    entry = find_curated_pack(
        gnet_id=None,
        title="redis dashboard for prometheus redis exporter 1.x",
        tags=["redis"],
    )
    assert entry is not None
    assert entry["gnet_id"] == 763


def test_gnet_id_takes_precedence_over_title():
    # If gnetId matches, title mismatch is irrelevant
    entry = find_curated_pack(
        gnet_id=763,
        title="Completely Wrong Title",
        tags=[],
    )
    assert entry is not None
    assert entry["gnet_id"] == 763


# ---------------------------------------------------------------------------
# resolve_pack_for_dashboard
# ---------------------------------------------------------------------------


def test_resolve_pack_known_dashboard_merges_metric_kinds():
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": ["redis"]}
    base = RulePackConfig()
    resolved = resolve_pack_for_dashboard(dashboard, base)
    assert resolved.metric_kinds.get("redis_commands_total") == "counter"
    assert resolved.metric_kinds.get("redis_memory_used_bytes") == "gauge"


def test_resolve_pack_18405_merges_metric_kinds():
    dashboard = {"gnetId": 18405, "title": "Redis Enterprise...", "tags": []}
    base = RulePackConfig()
    resolved = resolve_pack_for_dashboard(dashboard, base)
    # bdb_total_req is a pre-computed ops/sec gauge in Redis Enterprise Prometheus;
    # no rate() is applied in the source PromQL so we classify it as gauge.
    assert resolved.metric_kinds.get("bdb_total_req") == "gauge"
    assert resolved.metric_kinds.get("bdb_used_memory") == "gauge"


def test_resolve_pack_1860_merges_metric_kinds_and_query_override():
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    base = RulePackConfig()
    resolved = resolve_pack_for_dashboard(dashboard, base)
    assert resolved.metric_kinds.get("node_vmstat_pgpgin") == "counter"
    assert resolved.metric_kinds.get("process_virtual_memory_bytes") == "gauge"
    override_titles = {override.get("title_match") for override in resolved.panel_query_overrides}
    assert "Processes Memory" in override_titles
    assert "Sys Load" in override_titles
    assert "Root FS Used" in override_titles
    assert "RootFS Total" in override_titles
    assert "CPU Basic" in override_titles
    assert "Memory Basic" in override_titles
    assert "Network Traffic Basic" in override_titles
    assert "Disk Space Used Basic" in override_titles


def test_resolve_pack_18406_merges_metric_kinds():
    dashboard = {"gnetId": 18406, "title": "Redis Cloud...", "tags": []}
    base = RulePackConfig()
    resolved = resolve_pack_for_dashboard(dashboard, base)
    assert resolved.metric_kinds.get("bdb_total_req") == "gauge"
    assert resolved.metric_kinds.get("bdb_used_memory") == "gauge"


def test_resolve_pack_unknown_dashboard_returns_base_unchanged():
    dashboard = {"gnetId": 99999, "title": "My Custom Dashboard", "tags": []}
    base = RulePackConfig()
    resolved = resolve_pack_for_dashboard(dashboard, base)
    assert resolved is base


def test_resolve_pack_user_pack_wins_on_collision():
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": ["redis"]}
    base = RulePackConfig()
    base.metric_kinds["redis_commands_total"] = "gauge"  # user override
    resolved = resolve_pack_for_dashboard(dashboard, base)
    assert resolved.metric_kinds["redis_commands_total"] == "gauge"  # user wins


def test_resolve_pack_no_curated_flag_skips_curated():
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": ["redis"]}
    base = RulePackConfig()
    resolved = resolve_pack_for_dashboard(dashboard, base, no_curated=True)
    assert resolved is base


def test_resolve_pack_label_candidates_from_curated():
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": ["redis"]}
    base = RulePackConfig()
    resolved = resolve_pack_for_dashboard(dashboard, base)
    assert "service.instance.id" in resolved.label_candidates.get("instance", [])


def test_resolve_pack_no_gnet_id_uses_title_fallback():
    # Dashboard without gnetId but matching title
    dashboard = {
        "title": "Redis Dashboard for Prometheus Redis Exporter 1.x",
        "tags": ["redis", "prometheus"],
    }
    base = RulePackConfig()
    resolved = resolve_pack_for_dashboard(dashboard, base)
    assert resolved.metric_kinds.get("redis_commands_total") == "counter"


def test_resolve_pack_stamps_curated_pack_name():
    """Resolved curated pack carries _curated_pack_name for manifest reporting."""
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": ["redis"]}
    base = RulePackConfig()
    resolved = resolve_pack_for_dashboard(dashboard, base)
    assert getattr(resolved, "_curated_pack_name", "") == "grafana_763_redis_exporter"


def test_resolve_pack_base_has_no_curated_pack_name():
    """Base pack returned for unknown dashboards has no _curated_pack_name."""
    dashboard = {"gnetId": 99999, "title": "Unknown", "tags": []}
    base = RulePackConfig()
    resolved = resolve_pack_for_dashboard(dashboard, base)
    assert resolved is base
    assert getattr(resolved, "_curated_pack_name", "") == ""


# ---------------------------------------------------------------------------
# redis_memory_ratio_rule — gauge query correctness
# ---------------------------------------------------------------------------

def test_redis_memory_ratio_uses_ts_source():
    """gauge query must use TS (not FROM) to avoid inflating values from scrape docs."""
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": ["redis"]}
    base = RulePackConfig()
    rule_pack = resolve_pack_for_dashboard(dashboard, base)
    rule_pack.metrics_index = "metrics-redis.prometheus-default"

    gauge_panel = {
        "type": "gauge",
        "title": "Memory Usage",
        "targets": [
            {
                "expr": 'sum(100 * (redis_memory_used_bytes{instance=~"$instance"} / redis_memory_max_bytes{instance=~"$instance"}))',
                "refId": "A",
            }
        ],
        "fieldConfig": {
            "defaults": {
                "min": 0, "max": 100,
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": "green", "value": None},
                    {"color": "orange", "value": 80},
                    {"color": "red", "value": 95},
                ]},
            }
        },
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}},
    }

    yaml_panel, result = translate_panel(
        gauge_panel,
        datasource_index="metrics-redis.prometheus-default",
        rule_pack=rule_pack,
    )

    assert result.status != "not_feasible", f"Expected feasible, got not_feasible: {result.reasons}"
    assert "esql" in yaml_panel, "Expected esql panel, got markdown"
    query = yaml_panel["esql"]["query"]

    # The 763 curated pack carries a hand-written query override for Memory Usage
    # that computes the ratio in a single STATS clause — semantically exact for
    # the prometheus_native TSDB layout (vs auto-translation which uses per-doc
    # arithmetic inside SUM, introducing join-faithfulness risk).
    assert query.startswith("TS "), f"query should use TS source: {query[:60]}"
    assert "LAST_OVER_TIME(redis_memory_used_bytes)" in query, query
    assert "LAST_OVER_TIME(redis_memory_max_bytes)" in query, query
    assert "STATS value = LAST(value, time_bucket)" in query, query
    assert "MV_CONTAINS(?instance" in query, f"should preserve multi-select binding: {query}"
    assert 'MV_CONTAINS(?instance, ".*")' in query, query
    assert result.status == "migrated", f"status_override should set migrated, got: {result.status}"


def test_find_14091_by_gnet_id():
    entry = find_curated_pack(gnet_id=14091, title="", tags=[])
    assert entry is not None
    assert entry["gnet_id"] == 14091
    assert entry["name"] == "grafana_14091_redis_exporter_quickstart"


def test_find_14091_by_title_fallback():
    entry = find_curated_pack(
        gnet_id=None, title="Redis Exporter Quickstart and Dashboard", tags=[]
    )
    assert entry is not None
    assert entry["gnet_id"] == 14091


def test_resolve_pack_14091_merges_metric_kinds():
    dashboard = {"gnetId": 14091, "title": "Redis Exporter Quickstart", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    assert resolved.metric_kinds.get("redis_keyspace_hits_total") == "counter"
    assert resolved.metric_kinds.get("redis_connected_clients") == "gauge"


def test_resolve_pack_14091_maps_renamed_fragmentation_metric():
    """Revision 1 targets the pre-rename exporter metric name.

    Current oliver006/redis_exporter exposes redis_mem_fragmentation_ratio; the
    pack carries the rename so operators do not need --metric-map-file. The
    target must be fully qualified because metric_map targets are verbatim (no
    field-profile prefix is prepended).
    """
    dashboard = {"gnetId": 14091, "title": "Redis Exporter Quickstart", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    entry = (resolved.metric_map or {}).get("redis_memory_fragmentation_ratio")
    target = getattr(entry, "target", entry)
    assert target == "metrics.redis_mem_fragmentation_ratio"


def test_prometheus_native_label_candidates_come_first_in_redis_packs():
    """Offline runs take the FIRST candidate without probing the target.

    All three Redis packs describe Prometheus scrapes, so labels.<name> must lead;
    an OTel-first order silently emits service.name / db.name for a
    prometheus_native deployment (observed on 18405/18406 before this was fixed).
    """
    expected_first = {
        763: [("instance", "labels.instance"), ("job", "labels.job")],
        18405: [("cluster", "labels.cluster"), ("bdb", "labels.bdb")],
        18406: [("cluster", "labels.cluster"), ("bdb", "labels.bdb")],
        14091: [("instance", "labels.instance"), ("job", "labels.job")],
        11835: [("instance", "labels.instance"), ("job", "labels.job")],
    }
    for gnet_id, pairs in expected_first.items():
        resolved = resolve_pack_for_dashboard(
            {"gnetId": gnet_id, "title": "", "tags": []}, RulePackConfig()
        )
        for label, first in pairs:
            candidates = (resolved.label_candidates or {}).get(label)
            assert candidates, f"{gnet_id}: no candidates for {label}"
            assert candidates[0] == first, (
                f"{gnet_id}: {label} resolves to {candidates[0]} offline, expected {first}"
            )


def test_763_pack_pins_labels_instance_for_queries_and_controls():
    resolved = resolve_pack_for_dashboard(
        {"gnetId": 763, "title": "Redis...", "tags": ["redis"]},
        RulePackConfig(),
    )
    assert resolved.label_rewrites.get("instance") == "labels.instance"
    assert resolved.control_field_overrides.get("instance") == "labels.instance"


# ---------------------------------------------------------------------------
# Pack 11835 — Redis Exporter (helm stable/redis-ha)
# ---------------------------------------------------------------------------


def test_find_11835_by_gnet_id():
    entry = find_curated_pack(gnet_id=11835, title="", tags=[])
    assert entry is not None
    assert entry["gnet_id"] == 11835
    assert entry["name"] == "grafana_11835_redis_exporter_helm"


def test_find_11835_by_title_fallback():
    entry = find_curated_pack(
        gnet_id=None,
        title="Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha)",
        tags=["redis", "prometheus"],
    )
    assert entry is not None
    assert entry["gnet_id"] == 11835


def test_resolve_pack_11835_classifies_all_metrics():
    dashboard = {"gnetId": 11835, "title": "Redis...", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    # counters
    assert resolved.metric_kinds.get("redis_commands_processed_total") == "counter"
    assert resolved.metric_kinds.get("redis_keyspace_hits_total") == "counter"
    assert resolved.metric_kinds.get("redis_commands_total") == "counter"
    # gauges
    assert resolved.metric_kinds.get("redis_memory_used_bytes") == "gauge"
    assert resolved.metric_kinds.get("redis_db_keys") == "gauge"
    assert resolved.metric_kinds.get("redis_db_keys_expiring") == "gauge"


def test_resolve_pack_11835_stamps_curated_pack_name():
    dashboard = {"gnetId": 11835, "title": "Redis...", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    assert getattr(resolved, "_curated_pack_name", "") == "grafana_11835_redis_exporter_helm"


def test_resolve_pack_763_loads_curated_layout_overrides():
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": ["redis"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    overrides = {item["title_match"]: item for item in resolved.panel_layout_overrides}
    assert overrides["Max Uptime"]["size"]["w"] == 6
    assert overrides["Memory Usage"]["position"]["x"] == 12
    assert overrides["Hits / Misses per Sec"]["position"]["x"] == 34


def test_763_curated_pack_keeps_only_instance_control():
    dashboard = json.loads(
        (
            Path("parity-rig/curated/grafana_763_redis_exporter/grafana_provisioning/dashboards")
            / "redis_763.json"
        ).read_text(encoding="utf-8")
    )
    rule_pack = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-redis.prometheus-default",
        esql_index="metrics-redis.prometheus-default",
        rule_pack=rule_pack,
    )
    payload = result.dashboard_ir.to_yaml_dict()
    controls = payload.get("controls", [])

    assert {control.get("variable_name") for control in controls} == {"instance"}
    assert not any("namespace" in warning for warning in result.control_warnings)
    instance_control = controls[0]
    assert instance_control.get("label") == "instance"
    assert "redis_up IS NOT NULL" in str(instance_control.get("query") or "")
    assert (
        "labels.instance" in str(instance_control.get("query") or "")
        or " instance " in f" {instance_control.get('query') or ''} "
    )


def test_11835_curated_pack_keeps_only_instance_control():
    dashboard = json.loads(
        Path("infra/grafana/dashboards/redis-11835.json").read_text(encoding="utf-8")
    )
    rule_pack = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-redis.prometheus-default",
        esql_index="metrics-redis.prometheus-default",
        rule_pack=rule_pack,
    )
    payload = result.dashboard_ir.to_yaml_dict()
    controls = payload.get("controls", [])

    assert {control.get("variable_name") for control in controls} == {"instance"}
    assert not any(
        "namespace" in warning or "pod_name" in warning for warning in result.control_warnings
    )
    instance_control = controls[0]
    assert instance_control.get("label") == "instance"
    assert "redis_up IS NOT NULL" in str(instance_control.get("query") or "")
    assert (
        "labels.instance" in str(instance_control.get("query") or "")
        or " instance " in f" {instance_control.get('query') or ''} "
    )


# ---------------------------------------------------------------------------
# Panel query override — mechanism tests
# ---------------------------------------------------------------------------

_SIMPLE_METRIC_ESQL = (
    "TS metrics-*\n"
    "| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend\n"
    "| STATS value = MAX(LAST_OVER_TIME(some_metric))"
)


def test_panel_query_override_fires_for_matching_title():
    """When a pack has a panel_query_override, the curated ES|QL fires for title match."""
    pack = RulePackConfig()
    pack.panel_query_overrides = [{
        "title_match": "My Ratio Panel",
        "esql_query": _SIMPLE_METRIC_ESQL,
        "status_override": "migrated",
    }]

    panel = {
        "type": "singlestat",
        "title": "My Ratio Panel",
        "targets": [{"expr": "some_metric", "refId": "A"}],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=pack)
    assert result.status == "migrated", f"Expected migrated, got {result.status}: {result.reasons}"
    assert yaml_panel is not None and "esql" in yaml_panel, "Expected curated ES|QL panel spec"
    assert result.confidence == 1.0


def test_panel_query_override_preserves_gauge_shape_from_source_panel():
    pack = RulePackConfig()
    pack.panel_query_overrides = [{
        "title_match": "Sys Load",
        "esql_query": _SIMPLE_METRIC_ESQL,
        "status_override": "migrated",
    }]

    panel = {
        "type": "gauge",
        "title": "Sys Load",
        "targets": [{"expr": "node_load1", "refId": "A"}],
        "fieldConfig": {
            "defaults": {
                "min": 0,
                "max": 100,
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "red", "value": 85},
                    ],
                },
            }
        },
    }

    yaml_panel, result = translate_panel(panel, rule_pack=pack)

    assert result.status == "migrated"
    assert yaml_panel is not None and "esql" in yaml_panel
    assert yaml_panel["esql"].get("appearance", {}).get("shape") == "arc"
    assert yaml_panel["esql"].get("maximum") == {"field": "_gauge_max"}
    assert yaml_panel["esql"].get("goal") == {"field": "_gauge_goal"}


def test_panel_query_override_case_insensitive():
    """title_match comparison is case-insensitive."""
    pack = RulePackConfig()
    pack.panel_query_overrides = [{
        "title_match": "MEMORY USAGE",
        "esql_query": _SIMPLE_METRIC_ESQL,
        "status_override": "migrated",
    }]

    panel = {
        "type": "singlestat",
        "title": "Memory Usage",
        "targets": [{"expr": "some_metric", "refId": "A"}],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=pack)
    assert result.status == "migrated"
    assert yaml_panel is not None and "esql" in yaml_panel


def test_panel_query_override_nonmatching_title_falls_through():
    """A panel with a different title is NOT intercepted by the override."""
    pack = RulePackConfig()
    pack.panel_query_overrides = [{
        "title_match": "Memory Usage",
        "esql_query": _SIMPLE_METRIC_ESQL,
        "status_override": "migrated",
    }]

    panel = {
        "type": "singlestat",
        "title": "CPU Usage",
        "targets": [{"expr": "some_other_metric", "refId": "A"}],
    }

    yaml_panel, _result = translate_panel(panel, rule_pack=pack)
    # Falls through to normal translation — must NOT use the curated ES|QL
    if yaml_panel and "esql" in yaml_panel:
        query = yaml_panel["esql"].get("query", "")
        assert "some_metric" not in query


def test_panel_query_override_user_wins_over_curated():
    """When user pack has same title_match, user query wins over curated."""
    from observability_migration.adapters.source.grafana.rules import _merge_curated_into_base

    curated = RulePackConfig()
    curated.panel_query_overrides = [{
        "title_match": "Memory Usage",
        "esql_query": "TS metrics-*\n| STATS value = MAX(LAST_OVER_TIME(used))\n-- curated",
        "status_override": "migrated",
    }]
    curated._curated_pack_name = "test_curated"

    user = RulePackConfig()
    user.panel_query_overrides = [{
        "title_match": "Memory Usage",
        "esql_query": "TS metrics-*\n| STATS value = MAX(LAST_OVER_TIME(used))\n-- user override",
        "status_override": "migrated",
    }]

    merged = _merge_curated_into_base(curated, user)
    assert len(merged.panel_query_overrides) == 1, (
        "Deduplication by title_match should keep only one entry"
    )
    assert "user override" in merged.panel_query_overrides[0]["esql_query"]


def test_panel_query_override_merge_keeps_both_different_titles():
    """Overrides with different title_match values both survive the merge."""
    from observability_migration.adapters.source.grafana.rules import _merge_curated_into_base

    curated = RulePackConfig()
    curated.panel_query_overrides = [
        {"title_match": "Memory Usage", "esql_query": "TS metrics-*\n| STATS v = MAX(LAST_OVER_TIME(m))", "status_override": "migrated"},
    ]
    curated._curated_pack_name = "test_curated"

    user = RulePackConfig()
    user.panel_query_overrides = [
        {"title_match": "CPU Usage", "esql_query": "TS metrics-*\n| STATS v = MAX(LAST_OVER_TIME(cpu))", "status_override": "migrated"},
    ]

    merged = _merge_curated_into_base(curated, user)
    titles = {o["title_match"] for o in merged.panel_query_overrides}
    assert titles == {"Memory Usage", "CPU Usage"}


def test_panel_layout_override_user_wins_over_curated():
    from observability_migration.adapters.source.grafana.rules import _merge_curated_into_base

    curated = RulePackConfig()
    curated.panel_layout_overrides = [{
        "title_match": "Memory Usage",
        "position": {"x": 12, "y": 0},
        "size": {"w": 8},
    }]
    curated._curated_pack_name = "test_curated"

    user = RulePackConfig()
    user.panel_layout_overrides = [{
        "title_match": "Memory Usage",
        "position": {"x": 20, "y": 1},
        "size": {"w": 10, "h": 12},
    }]

    merged = _merge_curated_into_base(curated, user)
    assert merged.panel_layout_overrides == user.panel_layout_overrides


def test_panel_layout_overrides_apply_inside_sections():
    panels = [
        {
            "title": "Redis",
            "section": {
                "collapsed": False,
                "panels": [
                    {
                        "title": "Uptime",
                        "position": {"x": 0, "y": 0},
                        "size": {"w": 4, "h": 10},
                    },
                    {
                        "title": "Clients",
                        "position": {"x": 4, "y": 0},
                        "size": {"w": 4, "h": 10},
                    },
                ],
            },
        }
    ]
    overrides = [
        {"title_match": "Uptime", "position": {"x": 0, "y": 0}, "size": {"w": 6}},
        {"title_match": "Clients", "position": {"x": 6, "y": 0}, "size": {"w": 6}},
    ]

    _apply_panel_layout_overrides_recursively(panels, overrides)

    inner = panels[0]["section"]["panels"]
    assert inner[0]["size"]["w"] == 6
    assert inner[1]["position"]["x"] == 6


def test_curated_query_override_materializes_control_and_metric_placeholders():
    class _FakeResolver:
        def resolve_control_field(self, name, metric_field=None):
            return "instance" if name == "instance" else name

        def resolve_label(self, name, metric_field=None):
            return f"labels.{name}"

        def resolve_metric_field(self, name, prefer=None, source_labels=None):
            return f"metrics.{name}"

    query = (
        "TS metrics-*\n"
        '| WHERE MV_CONTAINS(?instance, "{{control:instance}}")\n'
        "| WHERE {{metric:redis_memory_used_bytes:gauge}} IS NOT NULL\n"
    )
    rendered = _materialize_curated_query_override(query, _FakeResolver())
    assert "{{" not in rendered
    assert "MV_CONTAINS(?instance, \"instance\")" in rendered
    assert "metrics.redis_memory_used_bytes IS NOT NULL" in rendered


def test_missing_live_metric_target_is_dropped_from_multi_target_panel():
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    resolver._field_cache = {
        "redis_connected_clients": {"double": {"type": "double"}},
        "instance": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "timeseries",
        "title": "Connected/Blocked Clients",
        "targets": [
            {"expr": 'sum(redis_connected_clients{instance=~"$instance"})', "refId": "A"},
            {"expr": 'sum(redis_blocked_clients{instance=~"$instance"})', "refId": "B"},
        ],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)
    query = (yaml_panel or {}).get("esql", {}).get("query", "")
    assert "redis_connected_clients" in query
    assert "redis_blocked_clients" not in query
    assert result.status == "migrated_with_warnings"
    assert any("Dropped series whose live target metrics are absent" in reason for reason in result.reasons)


def test_missing_live_metric_single_target_becomes_non_error_markdown():
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    resolver._field_cache = {
        "redis_commands_total": {"double": {"type": "double"}},
        "instance": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "timeseries",
        "title": "Total Time Spent by Command / sec",
        "targets": [
            {
                "expr": 'sum(irate(redis_commands_duration_seconds_total{instance=~"$instance"}[1m])) by (cmd) != 0',
                "refId": "A",
            }
        ],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)
    assert "markdown" in (yaml_panel or {})
    assert "redis_commands_duration_seconds_total" in yaml_panel["markdown"]["content"]
    assert result.status == "migrated_with_warnings"


def test_esql_param_control_retargets_to_single_panel_bound_field():
    controls = [
        {
            "type": "esql",
            "label": "instance",
            "variable_name": "instance",
            "variable_type": "values",
            "query": (
                "FROM metrics-* | WHERE redis_up IS NOT NULL AND `labels.instance` IS NOT NULL "
                '| STATS count = COUNT(*) BY `labels.instance` | EVAL options = MV_APPEND(".*", `labels.instance`) '
                '| MV_EXPAND options | STATS count = COUNT(*) BY options | KEEP options '
                '| RENAME options AS `labels.instance` | SORT `labels.instance` ASC | LIMIT 1000'
            ),
            "_resolved_field_name": "labels.instance",
        }
    ]
    panels = [
        {
            "esql": {
                "query": (
                    "TS metrics-* | WHERE (instance RLIKE ?instance OR (instance IS NULL AND \"\" RLIKE ?instance)) "
                    "| WHERE redis_up IS NOT NULL | STATS value = COUNT(*)"
                )
            }
        }
    ]

    rewritten = _retarget_esql_param_controls_to_panel_bindings(controls, panels)
    query = rewritten[0]["query"]
    assert "`labels.instance`" not in query
    assert "BY instance" in query
    assert "instance IS NOT NULL" in query
    assert rewritten[0]["_resolved_field_name"] == "instance"


def test_esql_param_control_keeps_original_when_panel_bindings_disagree():
    controls = [
        {
            "type": "esql",
            "label": "instance",
            "variable_name": "instance",
            "variable_type": "values",
            "query": "FROM metrics-* | WHERE `labels.instance` IS NOT NULL | STATS count = COUNT(*) BY `labels.instance` | KEEP `labels.instance` | LIMIT 1000",
            "_resolved_field_name": "labels.instance",
        }
    ]
    panels = [
        {"esql": {"query": "TS metrics-* | WHERE instance RLIKE ?instance | STATS value = COUNT(*)"}},
        {"esql": {"query": "TS metrics-* | WHERE host.name RLIKE ?instance | STATS value = COUNT(*)"}},
    ]

    rewritten = _retarget_esql_param_controls_to_panel_bindings(controls, panels)
    assert rewritten[0]["query"] == controls[0]["query"]
    assert rewritten[0]["_resolved_field_name"] == "labels.instance"


def test_11835_memory_usage_panel_uses_curated_override():
    """The 11835 pack's Memory Usage singlestat uses the curated ES|QL, status=migrated."""
    dashboard = {"gnetId": 11835, "title": "Redis...", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    memory_panel = {
        "type": "singlestat",
        "title": "Memory Usage",
        "targets": [{
            "expr": 'redis_memory_used_bytes{instance=~"$instance"} / redis_memory_max_bytes{instance=~"$instance"} * 100',
            "refId": "A",
        }],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}},
    }

    yaml_panel, result = translate_panel(memory_panel, rule_pack=resolved)
    assert result.status == "migrated", (
        f"Expected migrated via curated override, got {result.status}: {result.reasons}"
    )
    assert yaml_panel is not None and "esql" in yaml_panel, "Expected ES|QL panel spec"
    assert result.confidence == 1.0
    query = yaml_panel["esql"].get("query", "")
    assert "redis_memory_used_bytes" in query
    assert "redis_memory_max_bytes" in query
    assert "?instance" in query  # instance filter preserved from original PromQL


def test_11835_network_io_panel_uses_curated_override():
    """The 11835 pack's Network I/O panel must stay a two-series counter-rate query."""
    dashboard = {"gnetId": 11835, "title": "Redis...", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    panel = {
        "type": "graph",
        "title": "Network I/O",
        "targets": [
            {
                "expr": 'rate(redis_net_input_bytes_total{instance=~"$instance"}[5m])',
                "legendFormat": "{{input}}",
                "refId": "A",
            },
            {
                "expr": 'rate(redis_net_output_bytes_total{instance=~"$instance"}[5m])',
                "legendFormat": "{{output}}",
                "refId": "B",
            },
        ],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=resolved)
    assert result.status == "migrated", (
        f"Expected migrated via curated override, got {result.status}: {result.reasons}"
    )
    assert yaml_panel is not None and "esql" in yaml_panel, "Expected ES|QL panel spec"
    query = yaml_panel["esql"].get("query", "")
    assert "redis_net_input_bytes_total" in query
    assert "redis_net_output_bytes_total" in query
    assert "AVG(RATE(" not in query
    assert "labels.input" not in query
    assert "| KEEP time_bucket, input, output" in query


def test_763_network_io_panel_uses_curated_override():
    """The 763 pack's Network I/O panel must stay a two-series query without phantom breakdowns."""
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    panel = {
        "type": "graph",
        "title": "Network I/O",
        "targets": [
            {
                "expr": 'sum(rate(redis_net_input_bytes_total{instance=~"$instance"}[5m]))',
                "legendFormat": "input",
                "refId": "A",
            },
            {
                "expr": 'sum(rate(redis_net_output_bytes_total{instance=~"$instance"}[5m]))',
                "legendFormat": "output",
                "refId": "B",
            },
        ],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=resolved)
    assert result.status == "migrated", (
        f"Expected migrated via curated override, got {result.status}: {result.reasons}"
    )
    assert yaml_panel is not None and "esql" in yaml_panel, "Expected ES|QL panel spec"
    query = yaml_panel["esql"].get("query", "")
    assert "@timestamp >= ?_tstart" in query
    assert "TBUCKET(2 minute)" in query
    assert "redis_net_input_bytes_total" in query
    assert "redis_net_output_bytes_total" in query
    assert "labels.input" not in query
    assert "| KEEP time_bucket, input, output" in query


def test_763_average_time_spent_panel_uses_curated_override():
    """The 763 pack's per-command average latency panel must avoid adaptive null buckets."""
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    panel = {
        "type": "graph",
        "title": "Average Time Spent by Command / sec",
        "targets": [
            {
                "expr": 'sum(irate(redis_commands_duration_seconds_total{instance =~ "$instance"}[1m])) by (cmd) / sum(irate(redis_commands_total{instance =~ "$instance"}[1m])) by (cmd)',
                "refId": "A",
            }
        ],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=resolved)
    assert result.status == "migrated", (
        f"Expected migrated via curated override, got {result.status}: {result.reasons}"
    )
    assert yaml_panel is not None and "esql" in yaml_panel, "Expected ES|QL panel spec"
    query = yaml_panel["esql"].get("query", "")
    assert "@timestamp >= ?_tstart" in query
    assert "TBUCKET(2 minute)" in query
    assert "labels.cmd" in query
    assert "computed_value" in query
    assert "redis_commands_duration_seconds_total" in query
    assert "redis_commands_total" in query
    assert "SUM(IRATE(" in query


def test_763_total_time_spent_panel_uses_curated_override():
    """The 763 pack's per-command total latency panel must not emit empty adaptive buckets."""
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    panel = {
        "type": "graph",
        "title": "Total Time Spent by Command / sec",
        "targets": [
            {
                "expr": 'sum(irate(redis_commands_duration_seconds_total{instance=~"$instance"}[1m])) by (cmd) != 0',
                "refId": "A",
            }
        ],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=resolved)
    assert result.status == "migrated", (
        f"Expected migrated via curated override, got {result.status}: {result.reasons}"
    )
    assert yaml_panel is not None and "esql" in yaml_panel, "Expected ES|QL panel spec"
    query = yaml_panel["esql"].get("query", "")
    assert "@timestamp >= ?_tstart" in query
    assert "TBUCKET(2 minute)" in query
    assert "labels.cmd" in query
    assert "redis_commands_duration_seconds_total" in query
    assert "SUM(IRATE(" in query


def test_18405_memory_usage_panel_uses_curated_override():
    """The 18405 pack's Memory Usage stat uses the curated ES|QL, status=migrated."""
    dashboard = {"gnetId": 18405, "title": "Redis Enterprise...", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    memory_panel = {
        "type": "stat",
        "title": "Memory Usage",
        "targets": [{
            "expr": 'bdb_used_memory{cluster=~"$cluster",bdb=~"$bdb"} / bdb_memory_limit{cluster=~"$cluster",bdb=~"$bdb"}',
            "refId": "A",
        }],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}},
    }

    yaml_panel, result = translate_panel(memory_panel, rule_pack=resolved)
    assert result.status == "migrated", (
        f"Expected migrated via curated override, got {result.status}: {result.reasons}"
    )
    assert yaml_panel is not None and "esql" in yaml_panel, "Expected ES|QL panel spec"
    assert result.confidence == 1.0
    query = yaml_panel["esql"].get("query", "")
    assert "bdb_used_memory" in query
    assert "bdb_memory_limit" in query


def test_18406_memory_usage_panel_uses_curated_override():
    """The 18406 pack's Memory Usage stat uses the curated ES|QL, status=migrated."""
    dashboard = {"gnetId": 18406, "title": "Redis Cloud...", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    memory_panel = {
        "type": "stat",
        "title": "Memory Usage",
        "targets": [{
            "expr": 'bdb_used_memory{cluster=~"$cluster",bdb=~"$bdb"} / bdb_memory_limit{cluster=~"$cluster",bdb=~"$bdb"}',
            "refId": "A",
        }],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}},
    }

    yaml_panel, result = translate_panel(memory_panel, rule_pack=resolved)
    assert result.status == "migrated", (
        f"Expected migrated via curated override, got {result.status}: {result.reasons}"
    )
    assert yaml_panel is not None and "esql" in yaml_panel, "Expected ES|QL panel spec"
    assert result.confidence == 1.0


def test_schema_validates_pack_with_query_overrides():
    """pack.yaml with panel.query_overrides validates against the extension schema."""
    from observability_migration.adapters.source.grafana.extension_schema import validate_rule_pack_payload

    raw = {
        "query": {"metric_kinds": {"some_metric": "gauge"}},
        "panel": {
            "query_overrides": [
                {
                    "title_match": "Memory Usage",
                    "esql_query": _SIMPLE_METRIC_ESQL,
                    "status_override": "migrated",
                }
            ]
        },
    }
    payload = validate_rule_pack_payload(raw)
    assert len(payload.panel.query_overrides) == 1
    assert payload.panel.query_overrides[0].title_match == "Memory Usage"
    assert payload.panel.query_overrides[0].status_override == "migrated"


def test_schema_validates_pack_with_layout_overrides():
    from observability_migration.adapters.source.grafana.extension_schema import validate_rule_pack_payload

    raw = {
        "panel": {
            "layout_overrides": [
                {
                    "title_match": "Memory Usage",
                    "position": {"x": 12, "y": 0},
                    "size": {"w": 8},
                }
            ]
        }
    }
    payload = validate_rule_pack_payload(raw)
    assert len(payload.panel.layout_overrides) == 1
    assert payload.panel.layout_overrides[0].title_match == "Memory Usage"
    assert payload.panel.layout_overrides[0].size.w == 8


def test_panel_query_override_loaded_from_pack_yaml_round_trip():
    """load_rule_pack_files parses query_overrides into RulePackConfig.panel_query_overrides."""
    import os
    import tempfile

    from observability_migration.adapters.source.grafana.rules import load_rule_pack_files

    yaml_content = (
        "query:\n"
        "  metric_kinds:\n"
        "    some_gauge: gauge\n"
        "panel:\n"
        "  query_overrides:\n"
        "    - title_match: 'Memory Usage'\n"
        "      esql_query: |\n"
        "        TS metrics-*\n"
        "        | STATS value = MAX(LAST_OVER_TIME(some_gauge))\n"
        "      status_override: migrated\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = f.name

    try:
        pack = load_rule_pack_files([tmp_path])
        assert len(pack.panel_query_overrides) == 1
        override = pack.panel_query_overrides[0]
        assert override["title_match"] == "Memory Usage"
        assert "some_gauge" in override["esql_query"]
        assert override["status_override"] == "migrated"
    finally:
        os.unlink(tmp_path)


def test_panel_layout_override_loaded_from_pack_yaml_round_trip():
    import os
    import tempfile

    from observability_migration.adapters.source.grafana.rules import load_rule_pack_files

    yaml_content = (
        "panel:\n"
        "  layout_overrides:\n"
        "    - title_match: 'Memory Usage'\n"
        "      position:\n"
        "        x: 12\n"
        "        y: 0\n"
        "      size:\n"
        "        w: 8\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        tmp_path = f.name

    try:
        pack = load_rule_pack_files([tmp_path])
        assert len(pack.panel_layout_overrides) == 1
        override = pack.panel_layout_overrides[0]
        assert override["title_match"] == "Memory Usage"
        assert override["position"]["x"] == 12
        assert override["size"]["w"] == 8
    finally:
        os.unlink(tmp_path)
