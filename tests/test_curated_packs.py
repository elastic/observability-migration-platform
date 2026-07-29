# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the curated dashboard pack registry and resolution engine."""


from observability_migration.adapters.source.grafana.curated_packs import (
    find_curated_pack,
    load_curated_registry,
)
from observability_migration.adapters.source.grafana.rules import (
    RulePackConfig,
    resolve_pack_for_dashboard,
)

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
