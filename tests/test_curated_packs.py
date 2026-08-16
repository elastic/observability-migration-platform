# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the curated dashboard pack registry and resolution engine."""

import json
import tomllib
from pathlib import Path

from observability_migration.adapters.source.grafana.curated_packs import (
    find_curated_pack,
    load_curated_registry,
)
from observability_migration.adapters.source.grafana.panels import (
    _apply_panel_layout_overrides_recursively,
    _label_placeholder_value_metric,
    _materialize_curated_query_override,
    _omit_absent_optional_metrics_from_curated_query,
    _panel_static_legend_label,
    _retarget_esql_param_controls_to_panel_bindings,
    _strip_optional_metric_token_from_curated_esql,
    translate_dashboard,
    translate_panel,
)
from observability_migration.adapters.source.grafana.rules import (
    RulePackConfig,
    _merge_curated_into_base,
    load_rule_pack_files,
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


def test_registry_gnet_ids_are_unique():
    """Duplicate ``gnet_id``s would make exact-match pack lookup ambiguous."""
    entries = load_curated_registry()
    gnet_ids = [entry["gnet_id"] for entry in entries]
    assert len(set(gnet_ids)) == len(gnet_ids), f"duplicate gnet_id in registry: {gnet_ids}"


def test_registry_pack_names_and_paths_are_unique():
    entries = load_curated_registry()
    names = [entry["name"] for entry in entries]
    paths = [entry["path"] for entry in entries]
    assert len(set(names)) == len(names), f"duplicate pack name in registry: {names}"
    assert len(set(paths)) == len(paths), f"duplicate pack path in registry: {paths}"


def test_registry_provenance_pin_fields_are_well_formed():
    """Issue #350: ``gnet_revision``/``dashboard_sha256`` are maintainer-verified
    provenance pins (see registry.yaml's header comment and
    ``scripts/verify_curated_pack_pins.py``). Guard their *shape* offline;
    verifying they still match grafana.com needs network and is a separate,
    explicit maintainer command."""
    entries = load_curated_registry()
    for entry in entries:
        gnet_id = entry["gnet_id"]
        revision = entry["gnet_revision"]
        digest = entry["dashboard_sha256"]
        assert isinstance(gnet_id, int) and gnet_id > 0, f"bad gnet_id: {entry}"
        assert isinstance(revision, int) and revision >= 1, f"bad gnet_revision: {entry}"
        assert isinstance(digest, str) and len(digest) == 64, f"dashboard_sha256 must be 64 hex chars: {entry}"
        assert all(c in "0123456789abcdef" for c in digest), f"dashboard_sha256 must be lowercase hex: {entry}"


def test_registry_dashboard_sha256_values_are_unique():
    """Each pack pins a distinct dashboard revision; an accidental copy-paste
    of another entry's hash would silently defeat the provenance check."""
    entries = load_curated_registry()
    digests = [entry["dashboard_sha256"] for entry in entries]
    assert len(set(digests)) == len(digests), "duplicate dashboard_sha256 across registry entries"


def test_fidelity_manifest_gnet_revision_matches_registry():
    """Issue #350: ``fidelity_manifest.yaml`` duplicates ``gnet_id``/``gnet_revision``
    as free-standing documentation alongside the registry's copy. Nothing in the
    codebase reads this duplicate at runtime, so a registry re-pin (like this
    issue's own 11835 fix) can silently leave it stale -- guard the two copies
    stay in sync instead of relying on a maintainer to remember both."""
    import yaml

    from observability_migration.adapters.source.grafana import (
        curated_packs as _curated_packs_pkg,
    )

    packs_dir = Path(_curated_packs_pkg.__file__).parent
    entries = load_curated_registry()
    for entry in entries:
        manifest_path = packs_dir / str(entry["path"]) / "fidelity_manifest.yaml"
        assert manifest_path.exists(), f"missing fidelity_manifest.yaml for {entry['path']}"
        manifest = yaml.safe_load(manifest_path.read_text()) or {}
        assert manifest.get("gnet_id") == entry["gnet_id"], (
            f"{entry['path']}/fidelity_manifest.yaml gnet_id "
            f"({manifest.get('gnet_id')}) != registry.yaml ({entry['gnet_id']})"
        )
        assert manifest.get("gnet_revision") == entry["gnet_revision"], (
            f"{entry['path']}/fidelity_manifest.yaml gnet_revision "
            f"({manifest.get('gnet_revision')}) != registry.yaml ({entry['gnet_revision']})"
        )


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
    assert resolved.metric_kinds.get("node_netstat_TcpExt_TCPRcvQDrop") == "counter"
    assert resolved.metric_kinds.get("process_virtual_memory_bytes") == "gauge"
    override_titles = {override.get("title_match") for override in resolved.panel_query_overrides}
    assert "RAM Used" in override_titles
    assert "Pressure" in override_titles
    assert "SWAP Used" in override_titles
    assert "Uptime" in override_titles
    assert "Processes Memory" in override_titles
    assert "Sys Load" in override_titles
    assert "Root FS Used" in override_titles
    assert "RootFS Total" in override_titles
    assert "CPU Basic" in override_titles
    assert "TCP Errors" in override_titles
    assert "Memory Basic" in override_titles
    assert "Network Traffic Basic" in override_titles
    assert "Disk Space Used Basic" in override_titles
    assert "Interrupts Detail" in override_titles
    layout_titles = {override.get("title_match") for override in resolved.panel_layout_overrides}
    assert "CPU / Memory / Net / Disk" in layout_titles
    assert "Network Traffic" in layout_titles


def test_curated_rate_overrides_do_not_use_sub_scrape_adaptive_tbucket_100():
    """Curated RATE/IRATE overrides must not reintroduce the blank-chart bucket.

    TBUCKET(100, ?_tstart, ?_tend) can choose buckets at or below scrape
    cadence on short dashboard ranges, leaving RATE/IRATE with fewer than two
    samples per bucket. This invariant covers the pack-level cleanup in
    addition to the panel-specific Interrupts Detail assertion below.
    """
    dashboards = [
        {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]},
        {"gnetId": 763, "title": "Redis...", "tags": []},
        {"gnetId": 11835, "title": "Redis...", "tags": []},
    ]

    offenders = []
    for dashboard in dashboards:
        resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
        for override in resolved.panel_query_overrides:
            query = str(override.get("esql_query") or "")
            if "RATE(" not in query and "IRATE(" not in query and "INCREASE(" not in query:
                continue
            if "TBUCKET(100" in query or "BUCKET(@timestamp, 100" in query:
                offenders.append(f"{dashboard['gnetId']}:{override.get('title_match')}")

    assert offenders == []


# ---------------------------------------------------------------------------
# Curated override dropped-source-metric disclosure (issue #349)
# ---------------------------------------------------------------------------

def test_curated_override_downgrades_when_source_metric_dropped():
    """``status_override: migrated`` must act as a ceiling, not an
    unconditional assignment: if the panel's own targets reference a metric
    the hand-written override never emits, the panel must downgrade to
    ``migrated_with_warnings`` and name the dropped metric, matching the
    tool's own behavior for non-pack panels ("Target telemetry missing")."""
    rule_pack = RulePackConfig(
        panel_query_overrides=[
            {
                "title_match": "Two Series",
                "esql_query": (
                    "TS metrics-*\n"
                    "| WHERE {{metric:foo_total:counter}} IS NOT NULL\n"
                    "| STATS value = MAX(LAST_OVER_TIME({{metric:foo_total:counter}}))\n"
                    "| KEEP value"
                ),
                "status_override": "migrated",
            }
        ]
    )
    resolver = SchemaResolver(rule_pack)
    resolver._field_cache = {
        "foo_total": {"double": {"type": "double"}},
        "bar_total": {"double": {"type": "double"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "gauge",
        "title": "Two Series",
        "targets": [
            {"expr": "foo_total", "refId": "A"},
            {"expr": "bar_total", "refId": "B"},
        ],
    }

    _yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)

    assert result.status == "migrated_with_warnings", result.reasons
    assert result.confidence <= 0.6
    assert any(
        "bar_total" in reason and "curated override" in reason
        for reason in result.reasons
    ), result.reasons


def test_curated_override_status_ceiling_not_downgraded_when_no_gap():
    """Sanity companion: when the override legitimately covers every source
    metric, ``status_override: migrated`` must NOT be downgraded."""
    rule_pack = RulePackConfig(
        panel_query_overrides=[
            {
                "title_match": "One Series",
                "esql_query": (
                    "TS metrics-*\n"
                    "| WHERE {{metric:foo_total:counter}} IS NOT NULL\n"
                    "| STATS value = MAX(LAST_OVER_TIME({{metric:foo_total:counter}}))\n"
                    "| KEEP value"
                ),
                "status_override": "migrated",
            }
        ]
    )
    resolver = SchemaResolver(rule_pack)
    resolver._field_cache = {"foo_total": {"double": {"type": "double"}}}
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "gauge",
        "title": "One Series",
        "targets": [{"expr": "foo_total", "refId": "A"}],
    }

    _yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)

    assert result.status == "migrated"
    assert result.confidence == 1.0
    assert result.reasons == []


def test_curated_override_ignores_hidden_target_when_checking_dropped_metrics():
    """A ``hide: true`` target is a disabled/legacy alternate query Grafana
    itself never renders (e.g. Node Exporter Full's real "RAM Used" panel
    keeps an old MemFree-based formula hidden behind a visible
    MemAvailable-based one for older node_exporter compatibility). The
    dropped-metric check must only compare against targets a user actually
    sees, or every such compatibility fallback falsely downgrades an
    otherwise-clean curated override."""
    rule_pack = RulePackConfig(
        panel_query_overrides=[
            {
                "title_match": "RAM Used",
                "esql_query": (
                    "TS metrics-*\n"
                    "| WHERE {{metric:mem_available:gauge}} IS NOT NULL\n"
                    "| STATS value = MAX(LAST_OVER_TIME({{metric:mem_available:gauge}}))\n"
                    "| KEEP value"
                ),
                "status_override": "migrated",
            }
        ]
    )
    resolver = SchemaResolver(rule_pack)
    resolver._field_cache = {
        "mem_free": {"double": {"type": "double"}},
        "mem_available": {"double": {"type": "double"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "gauge",
        "title": "RAM Used",
        "targets": [
            {"expr": "mem_free", "refId": "A", "hide": True},
            {"expr": "mem_available", "refId": "B"},
        ],
    }

    _yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)

    assert result.status == "migrated"
    assert result.confidence == 1.0
    assert result.reasons == []


def test_1860_pressure_panel_includes_irq_series():
    """node_pressure_irq_stalled_seconds_total (issue #349) must be part of
    the curated Pressure override, not silently dropped."""
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    resolver._field_cache = {
        "node_pressure_cpu_waiting_seconds_total": {"double": {"type": "double"}},
        "node_pressure_memory_waiting_seconds_total": {"double": {"type": "double"}},
        "node_pressure_io_waiting_seconds_total": {"double": {"type": "double"}},
        "node_pressure_irq_stalled_seconds_total": {"double": {"type": "double"}},
        "instance": {"keyword": {"type": "keyword"}},
        "job": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "bargauge",
        "title": "Pressure",
        "targets": [
            {"expr": "irate(node_pressure_cpu_waiting_seconds_total[$__rate_interval])", "refId": "A"},
            {"expr": "irate(node_pressure_memory_waiting_seconds_total[$__rate_interval])", "refId": "B"},
            {"expr": "irate(node_pressure_io_waiting_seconds_total[$__rate_interval])", "refId": "C"},
            {"expr": "irate(node_pressure_irq_stalled_seconds_total[$__rate_interval])", "refId": "D"},
        ],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=resolved, resolver=resolver)

    assert result.status == "migrated", f"got {result.status}: {result.reasons}"
    query = (yaml_panel or {}).get("esql", {}).get("query", "")
    assert "node_pressure_irq_stalled_seconds_total" in query
    assert '"Irq"' in query


def test_1860_cpu_panel_includes_guest_series():
    """node_cpu_guest_seconds_total (issue #349) must be part of the curated
    CPU override, not silently dropped."""
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    resolver._field_cache = {
        "node_cpu_seconds_total": {"double": {"type": "double"}},
        "node_cpu_guest_seconds_total": {"double": {"type": "double"}},
        "instance": {"keyword": {"type": "keyword"}},
        "job": {"keyword": {"type": "keyword"}},
        "cpu": {"keyword": {"type": "keyword"}},
        "mode": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    targets = [
        {
            "expr": f'avg(irate(node_cpu_seconds_total{{mode="{mode}"}}[$__rate_interval])) by (mode) * 100',
            "refId": chr(65 + i),
        }
        for i, mode in enumerate(
            ["system", "user", "nice", "iowait", "irq", "softirq", "steal", "idle"]
        )
    ]
    targets.append(
        {"expr": "avg(irate(node_cpu_guest_seconds_total[$__rate_interval])) * 100", "refId": "I"}
    )
    panel = {"type": "timeseries", "title": "CPU", "targets": targets}

    yaml_panel, result = translate_panel(panel, rule_pack=resolved, resolver=resolver)

    assert result.status == "migrated", f"got {result.status}: {result.reasons}"
    query = (yaml_panel or {}).get("esql", {}).get("query", "")
    assert "node_cpu_guest_seconds_total" in query
    assert "Guest -" in query


def test_1860_pressure_omits_irq_when_field_caps_absent():
    """PSI irq is not on every kernel. Referencing the unknown column makes
    Elasticsearch reject the whole Pressure panel, so the override must drop
    it via live_optional_metrics when field-caps prove it absent."""
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    resolver._field_cache = {
        "node_pressure_cpu_waiting_seconds_total": {"double": {"type": "double"}},
        "node_pressure_memory_waiting_seconds_total": {"double": {"type": "double"}},
        "node_pressure_io_waiting_seconds_total": {"double": {"type": "double"}},
        "instance": {"keyword": {"type": "keyword"}},
        "job": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "bargauge",
        "title": "Pressure",
        "targets": [
            {"expr": "irate(node_pressure_cpu_waiting_seconds_total[$__rate_interval])", "refId": "A"},
            {"expr": "irate(node_pressure_memory_waiting_seconds_total[$__rate_interval])", "refId": "B"},
            {"expr": "irate(node_pressure_io_waiting_seconds_total[$__rate_interval])", "refId": "C"},
        ],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=resolved, resolver=resolver)

    assert result.status == "migrated", f"got {result.status}: {result.reasons}"
    query = (yaml_panel or {}).get("esql", {}).get("query", "")
    assert "node_pressure_cpu_waiting_seconds_total" in query
    assert "irq_stalled" not in query
    assert not any("curated override" in reason for reason in result.reasons)


def test_1860_cpu_omits_guest_when_field_caps_absent():
    """Guest is a distinct exporter metric. An unknown-column reference in the
    same STATS as the eight CPU modes would take down the whole CPU panel."""
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    resolver._field_cache = {
        "node_cpu_seconds_total": {"double": {"type": "double"}},
        "instance": {"keyword": {"type": "keyword"}},
        "job": {"keyword": {"type": "keyword"}},
        "cpu": {"keyword": {"type": "keyword"}},
        "mode": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    targets = [
        {
            "expr": f'avg(irate(node_cpu_seconds_total{{mode="{mode}"}}[$__rate_interval])) by (mode) * 100',
            "refId": chr(65 + i),
        }
        for i, mode in enumerate(
            ["system", "user", "nice", "iowait", "irq", "softirq", "steal", "idle"]
        )
    ]
    panel = {"type": "timeseries", "title": "CPU", "targets": targets}

    yaml_panel, result = translate_panel(panel, rule_pack=resolved, resolver=resolver)

    assert result.status == "migrated", f"got {result.status}: {result.reasons}"
    query = (yaml_panel or {}).get("esql", {}).get("query", "")
    assert "node_cpu_seconds_total" in query
    assert "guest" not in query.lower()
    assert not any("curated override" in reason for reason in result.reasons)


def test_curated_override_does_not_flag_stripped_optional_metrics():
    """A live_optional metric that field-caps proved absent is stripped so the
    rest of the override can render. That is not a pack omission, so
    status_override: migrated must not be downgraded with a "missing from
    curated override" reason (TCP Errors / TCPRcvQDrop)."""
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    resolver._field_cache = {
        "metrics.node_netstat_TcpExt_ListenOverflows": {"double": {"type": "double"}},
        "metrics.node_netstat_TcpExt_ListenDrops": {"double": {"type": "double"}},
        "metrics.node_netstat_TcpExt_TCPSynRetrans": {"double": {"type": "double"}},
        "metrics.node_netstat_Tcp_RetransSegs": {"double": {"type": "double"}},
        "metrics.node_netstat_Tcp_InErrs": {"double": {"type": "double"}},
        "metrics.node_netstat_Tcp_OutRsts": {"double": {"type": "double"}},
        "metrics.node_netstat_TcpExt_TCPOFOQueue": {"double": {"type": "double"}},
        "labels.instance": {"keyword": {"type": "keyword"}},
        "labels.job": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "timeseries",
        "title": "TCP Errors",
        "targets": [
            {"expr": "irate(node_netstat_TcpExt_ListenOverflows[5m])", "refId": "A"},
            {"expr": "irate(node_netstat_TcpExt_ListenDrops[5m])", "refId": "B"},
            {"expr": "irate(node_netstat_TcpExt_TCPSynRetrans[5m])", "refId": "C"},
            {"expr": "irate(node_netstat_Tcp_RetransSegs[5m])", "refId": "D"},
            {"expr": "irate(node_netstat_Tcp_InErrs[5m])", "refId": "E"},
            {"expr": "irate(node_netstat_Tcp_OutRsts[5m])", "refId": "F"},
            {"expr": "irate(node_netstat_TcpExt_TCPRcvQDrop[5m])", "refId": "G"},
            {"expr": "irate(node_netstat_TcpExt_TCPOFOQueue[5m])", "refId": "H"},
        ],
    }

    _yaml_panel, result = translate_panel(panel, rule_pack=resolved, resolver=resolver)

    assert result.status == "migrated", f"got {result.status}: {result.reasons}"
    assert not any("curated override" in reason for reason in result.reasons), result.reasons
    assert "TCPRcvQDrop" not in ((_yaml_panel or {}).get("esql") or {}).get("query", "")


def test_1860_interrupts_detail_uses_interrupt_cpu_legend():
    """Interrupts Detail must legend by interrupt/cpu, not empty type/info GROK."""
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    panel = {
        "type": "timeseries",
        "title": "Interrupts Detail",
        "targets": [
            {
                "expr": 'irate(node_interrupts_total{instance="$node",job="$job"}[$__rate_interval])',
                "legendFormat": "{{ type }} - {{ info }}",
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
    assert "IRATE(" in query
    assert "TBUCKET(20" in query
    assert "TBUCKET(100" not in query
    assert "labels.interrupt" in query
    assert "labels.cpu" in query
    assert 'CONCAT(COALESCE(TO_STRING(labels.interrupt)' in query or (
        "labels.interrupt" in query and "CPU" in query
    )
    assert '"type"' not in query
    assert '"info"' not in query
    assert "GROK" not in query


def test_1860_interrupts_detail_degrades_to_markdown_when_metric_absent_live():
    """node_interrupts_total is the only metric in the override; if field-caps
    prove it absent, the panel must degrade to a missing-telemetry markdown
    instead of silently shipping an ES|QL query that can never match."""
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    assert "node_interrupts_total" in resolved.live_optional_metrics

    resolver = SchemaResolver(resolved)
    resolver._field_cache = {"labels.instance": {"keyword": {"type": "keyword"}}}
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "timeseries",
        "title": "Interrupts Detail",
        "targets": [
            {
                "expr": 'irate(node_interrupts_total{instance="$node",job="$job"}[$__rate_interval])',
                "legendFormat": "{{ type }} - {{ info }}",
                "refId": "A",
            }
        ],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=resolved, resolver=resolver)

    assert "markdown" in (yaml_panel or {})
    assert "node_interrupts_total" in yaml_panel["markdown"]["content"]
    assert result.status == "migrated_with_warnings"


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


def test_merge_curated_scalar_user_default_still_wins_when_explicit():
    curated = RulePackConfig(default_rate_window="10m")
    user = RulePackConfig(default_rate_window="5m")
    user._explicit_scalar_fields.add("default_rate_window")

    merged = _merge_curated_into_base(curated, user)

    assert merged.default_rate_window == "5m"


def test_load_rule_pack_marks_explicit_scalar_even_when_equal_to_default(tmp_path):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text("query:\n  default_rate_window: 5m\n", encoding="utf-8")

    pack = load_rule_pack_files([str(rules_file)])

    assert "default_rate_window" in pack._explicit_scalar_fields


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


def test_grafana_curated_pack_plugins_ship_as_package_data():
    repo = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["tool"]["setuptools"]["package-data"]
    patterns = declared.get("observability_migration.adapters.source.grafana.curated_packs", [])

    assert any(pattern.endswith("*.py") for pattern in patterns), (
        "Grafana curated-pack plugin.py files are loaded by path at runtime, so "
        "they must be declared in [tool.setuptools.package-data] for wheel installs."
    )


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
    # ``?instance`` is wrapped in ``TO_STRING(...)`` (issue #353) so the
    # guardrail still type-checks if Kibana ever infers ``?instance`` as a
    # non-keyword array.
    assert "MV_CONTAINS(TO_STRING(?instance)" in query, f"should preserve multi-select binding: {query}"
    assert 'MV_CONTAINS(TO_STRING(?instance), ".*")' in query, query
    assert result.status == "migrated", f"status_override should set migrated, got: {result.status}"
    # Dial domain 0-100 must survive sync: emitted query carries ``_gauge_*``
    # and ``panel_result.esql_query`` must match so validate does not strip them.
    assert yaml_panel["esql"].get("maximum") == {"field": "_gauge_max"}
    assert "_gauge_max = 100" in query, query
    assert result.esql_query == query


def test_1860_cpu_busy_curated_override_avoids_boundary_bucket_last():
    """The node-exporter-full CPU Busy gauge must skip the final partial rate bucket."""
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    cpu_busy_panel = {
        "type": "gauge",
        "title": "CPU Busy",
        "targets": [
            {
                "expr": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))',
                "refId": "A",
            }
        ],
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}},
    }

    yaml_panel, result = translate_panel(cpu_busy_panel, rule_pack=resolved)

    assert result.status == "migrated", (
        f"Expected migrated via curated override, got {result.status}: {result.reasons}"
    )
    assert yaml_panel is not None and "esql" in yaml_panel, "Expected ES|QL panel spec"
    query = yaml_panel["esql"].get("query", "")
    assert "AVG(IRATE(" in query
    assert "| SORT time_bucket DESC" in query
    assert "| LIMIT 2" in query
    assert "| SORT time_bucket ASC" in query
    assert "| LIMIT 1" in query
    assert "WHERE computed_value IS NOT NULL" in query
    assert "TBUCKET(20," in query
    assert "STATS computed_value = LAST(computed_value, time_bucket)" not in query


def test_1860_disk_space_used_basic_labels_composite_value_metric():
    """Curated composite-series overrides must label their ``value`` column (#351).

    The "Disk Space Used Basic" override fuses ``node_filesystem_avail_bytes``
    and ``node_filesystem_size_bytes`` into one ``value`` column broken down
    by ``series_group`` (mountpoint). With no ``label`` set, Lens falls back
    to the raw column name ("value") as the y-axis title; the panel title is
    the same fallback the single-target native-PROMQL path already uses.
    """
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    panel = {
        "type": "timeseries",
        "title": "Disk Space Used Basic",
        "fieldConfig": {"defaults": {"unit": "percent"}},
        "targets": [{"expr": "node_filesystem_avail_bytes", "refId": "A"}],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=resolved)

    assert result.status == "migrated", (
        f"Expected migrated via curated override, got {result.status}: {result.reasons}"
    )
    metrics = yaml_panel["esql"]["metrics"]
    assert [m.get("field") for m in metrics] == ["value"]
    assert metrics[0].get("label") == "Disk Space Used Basic"


def test_1860_curated_composite_value_metric_prefers_static_legend_label():
    """Curated overrides keep the single-target static-legend precedence."""
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    panel = {
        "type": "timeseries",
        "title": "Disk Space Used Basic",
        "fieldConfig": {"defaults": {"unit": "percent"}},
        "targets": [
            {
                "expr": "node_filesystem_avail_bytes",
                "refId": "A",
                "legendFormat": "Disk Used",
            }
        ],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=resolved)

    assert result.status == "migrated"
    assert yaml_panel["esql"]["metrics"][0].get("label") == "Disk Used"


def test_panel_static_legend_label_rejects_mixed_static_and_dynamic_legends():
    panel = {
        "targets": [
            {"legendFormat": "Disk Used"},
            {"legendFormat": "{{ mountpoint }}"},
        ]
    }

    assert _panel_static_legend_label(panel) == ""


def test_placeholder_label_falls_back_to_title_when_visible_legends_disagree():
    """Fused series cannot pick one target's legend when visible legends differ."""
    yaml_panel = {"esql": {"metrics": [{"field": "value"}]}}
    _label_placeholder_value_metric(
        yaml_panel,
        title="Network Traffic Basic",
        legend_format=_panel_static_legend_label(
            {
                "targets": [
                    {"legendFormat": "recv {{device}}"},
                    {"legendFormat": "trans {{device}}"},
                ]
            }
        ),
    )

    assert yaml_panel["esql"]["metrics"][0]["label"] == "Network Traffic Basic"


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


def test_763_curated_pack_preserves_namespace_and_instance_controls():
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

    assert {control.get("variable_name") for control in controls} == {"namespace", "instance"}
    # Cascade parent: no panel binds ?namespace, but the instance control query
    # does — so namespace must stay without the "renders but changes no panel"
    # inert-control warning.
    assert not any(
        "variable 'namespace' has a Kibana control" in warning
        for warning in result.control_warnings
    ), result.control_warnings
    namespace_control = next(control for control in controls if control.get("variable_name") == "namespace")
    instance_control = next(control for control in controls if control.get("variable_name") == "instance")
    assert namespace_control.get("label") == "namespace"
    assert "redis_up IS NOT NULL" in str(namespace_control.get("query") or "")
    assert "labels.namespace" in str(namespace_control.get("query") or "")
    assert instance_control.get("label") == "instance"
    assert "redis_up IS NOT NULL" in str(instance_control.get("query") or "")
    assert "?namespace" in str(instance_control.get("query") or "")

def test_11835_curated_pack_preserves_source_control_graph():
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

    assert {control.get("variable_name") for control in controls} == {
        "namespace",
        "pod_name",
        "instance",
    }
    # Cascade parents (namespace → pod_name → instance) bind each other via
    # control populate queries, so they must not get the inert-control warning.
    assert not any(
        "variable 'namespace' has a Kibana control" in warning
        for warning in result.control_warnings
    ), result.control_warnings
    assert not any(
        "variable 'pod_name' has a Kibana control" in warning
        for warning in result.control_warnings
    ), result.control_warnings
    namespace_control = next(control for control in controls if control.get("variable_name") == "namespace")
    pod_control = next(control for control in controls if control.get("variable_name") == "pod_name")
    instance_control = next(control for control in controls if control.get("variable_name") == "instance")
    assert namespace_control.get("label") == "Namespace"
    assert "namespace IS NOT NULL" in str(namespace_control.get("query") or "")
    assert pod_control.get("label") == "Pod Name"
    assert "?namespace" in str(pod_control.get("query") or "")
    assert instance_control.get("label") == "instance"
    assert "?namespace" in str(instance_control.get("query") or "")
    assert "?pod_name" in str(instance_control.get("query") or "")
    assert (
        "service.instance.id" in str(instance_control.get("query") or "")
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
    # Issue #109 class: curated overrides must record the emitted query (with
    # ``| EVAL _gauge_*``) so validate-stage sync does not strip dial bounds.
    query = yaml_panel["esql"]["query"]
    assert "_gauge_min = 0, _gauge_max = 100, _gauge_goal = 85" in query
    assert result.esql_query == query


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


def test_panel_layout_overrides_can_flip_section_collapsed_state():
    panels = [
        {
            "title": "CPU / Memory / Net / Disk",
            "section": {
                "collapsed": True,
                "panels": [
                    {
                        "title": "CPU",
                        "position": {"x": 0, "y": 0},
                        "size": {"w": 24, "h": 8},
                    }
                ],
            },
        }
    ]
    overrides = [{"title_match": "CPU / Memory / Net / Disk", "collapsed": False}]

    _apply_panel_layout_overrides_recursively(panels, overrides)

    assert panels[0]["section"]["collapsed"] is False


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


def test_omit_absent_optional_metric_from_curated_tcp_errors_override():
    """TCPRcvQDrop is live_optional; absent field-caps must not hard-fail TCP Errors."""
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    rule_pack = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    override = next(
        item
        for item in rule_pack.panel_query_overrides
        if item.get("title_match") == "TCP Errors"
    )
    raw_query = override["esql_query"]
    assert "TCPRcvQDrop" in raw_query

    present = {
        "metrics.node_netstat_TcpExt_ListenOverflows": {"double": {"type": "double"}},
        "metrics.node_netstat_TcpExt_ListenDrops": {"double": {"type": "double"}},
        "metrics.node_netstat_TcpExt_TCPSynRetrans": {"double": {"type": "double"}},
        "metrics.node_netstat_Tcp_RetransSegs": {"double": {"type": "double"}},
        "metrics.node_netstat_Tcp_InErrs": {"double": {"type": "double"}},
        "metrics.node_netstat_Tcp_OutRsts": {"double": {"type": "double"}},
        "metrics.node_netstat_TcpExt_TCPOFOQueue": {"double": {"type": "double"}},
        "labels.instance": {"keyword": {"type": "keyword"}},
        "labels.job": {"keyword": {"type": "keyword"}},
    }
    resolver = SchemaResolver(rule_pack)
    resolver._field_cache = dict(present)
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    stripped = _omit_absent_optional_metrics_from_curated_query(
        raw_query,
        rule_pack.live_optional_metrics,
        resolver,
    )
    assert "TCPRcvQDrop" not in stripped
    assert "ListenOverflows" in stripped
    assert "TCPOFOQueue" in stripped
    assert "{{metric:node_netstat_TcpExt_ListenOverflows" in stripped

    # End-to-end: panel still migrates with a valid ES|QL query (no TCPRcvQDrop).
    panel = {
        "type": "timeseries",
        "title": "TCP Errors",
        "targets": [
            {
                "expr": 'irate(node_netstat_TcpExt_ListenOverflows{instance=~"$node"}[5m])',
                "refId": "A",
                "legendFormat": "ListenOverflows",
            }
        ],
    }
    yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)
    assert result.status in {"migrated", "migrated_with_warnings"}
    query = (yaml_panel or {}).get("esql", {}).get("query", "")
    assert "ListenOverflows" in query
    assert "TCPRcvQDrop" not in query
    assert "markdown" not in (yaml_panel or {})


def test_curated_tcp_errors_keeps_optional_metric_when_field_caps_present():
    dashboard = {"gnetId": 1860, "title": "Node Exporter Full", "tags": ["prometheus"]}
    rule_pack = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    override = next(
        item
        for item in rule_pack.panel_query_overrides
        if item.get("title_match") == "TCP Errors"
    )
    resolver = SchemaResolver(rule_pack)
    resolver._field_cache = {
        "metrics.node_netstat_TcpExt_ListenOverflows": {"double": {"type": "double"}},
        "metrics.node_netstat_TcpExt_ListenDrops": {"double": {"type": "double"}},
        "metrics.node_netstat_TcpExt_TCPSynRetrans": {"double": {"type": "double"}},
        "metrics.node_netstat_Tcp_RetransSegs": {"double": {"type": "double"}},
        "metrics.node_netstat_Tcp_InErrs": {"double": {"type": "double"}},
        "metrics.node_netstat_Tcp_OutRsts": {"double": {"type": "double"}},
        "metrics.node_netstat_TcpExt_TCPRcvQDrop": {"double": {"type": "double"}},
        "metrics.node_netstat_TcpExt_TCPOFOQueue": {"double": {"type": "double"}},
        "labels.instance": {"keyword": {"type": "keyword"}},
        "labels.job": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    kept = _omit_absent_optional_metrics_from_curated_query(
        override["esql_query"],
        rule_pack.live_optional_metrics,
        resolver,
    )
    assert "TCPRcvQDrop" in kept


def test_strip_optional_metric_handles_nested_stats_assignment():
    query = (
        "TS metrics-*"
        " | WHERE {{metric:required_metric:counter}} IS NOT NULL"
        " OR {{metric:optional_metric:counter}} IS NOT NULL"
        " | STATS required = AVG(IRATE({{metric:required_metric:counter}})),"
        " optional = COALESCE(AVG(IRATE({{metric:optional_metric:counter}})), 0)"
        " | KEEP required, optional"
    )

    stripped = _strip_optional_metric_token_from_curated_esql(query, "optional_metric")

    assert "required = AVG(IRATE({{metric:required_metric:counter}}))" in stripped
    assert "optional =" not in stripped
    assert ", 0)" not in stripped
    assert "KEEP required" in stripped


def test_strip_optional_metric_handles_first_assignment_and_quoted_alias():
    query = (
        "TS metrics-*"
        " | WHERE {{metric:optional_metric:counter}} IS NOT NULL"
        " OR {{metric:required_metric:counter}} IS NOT NULL"
        " | STATS `optional alias` = AVG(IRATE({{metric:optional_metric:counter}})),"
        " required = AVG(IRATE({{metric:required_metric:counter}}))"
        " | KEEP `optional alias`, required"
    )

    stripped = _strip_optional_metric_token_from_curated_esql(query, "optional_metric")

    assert "`optional alias`" not in stripped
    assert "STATS required = AVG(IRATE({{metric:required_metric:counter}}))" in stripped
    assert "KEEP required" in stripped


def test_strip_optional_metric_handles_last_assignment():
    query = (
        "TS metrics-*"
        " | WHERE {{metric:required_metric:counter}} IS NOT NULL"
        " OR {{metric:optional_metric:counter}} IS NOT NULL"
        " | STATS required = AVG(IRATE({{metric:required_metric:counter}})),"
        " optional = AVG(IRATE({{metric:optional_metric:counter}}))"
        " | KEEP required, optional"
    )

    stripped = _strip_optional_metric_token_from_curated_esql(query, "optional_metric")

    assert "required = AVG(IRATE({{metric:required_metric:counter}}))" in stripped
    assert "optional = AVG(IRATE({{metric:optional_metric:counter}}))" not in stripped
    assert "KEEP required" in stripped


def test_curated_override_with_only_absent_optional_metric_becomes_missing_telemetry_markdown():
    rule_pack = RulePackConfig(
        live_optional_metrics=["optional_metric"],
        panel_query_overrides=[
            {
                "title_match": "Optional Only",
                "kibana_type_override": "metric",
                "esql_query": (
                    "TS metrics-*"
                    " | WHERE {{metric:optional_metric:counter}} IS NOT NULL"
                    " | STATS optional = AVG(IRATE({{metric:optional_metric:counter}}))"
                    " | KEEP optional"
                ),
            }
        ],
    )
    resolver = SchemaResolver(rule_pack)
    resolver._field_cache = {"labels.instance": {"keyword": {"type": "keyword"}}}
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "timeseries",
        "title": "Optional Only",
        "targets": [{"expr": 'sum(optional_metric{instance=~"$instance"})', "refId": "A"}],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)

    assert "markdown" in (yaml_panel or {})
    assert "optional_metric" in yaml_panel["markdown"]["content"]
    assert result.status == "migrated_with_warnings"


def test_strip_optional_metric_handles_singleton_assignment():
    query = (
        "TS metrics-*"
        " | WHERE {{metric:optional_metric:counter}} IS NOT NULL"
        " | STATS optional = AVG(IRATE({{metric:optional_metric:counter}}))"
        " | KEEP optional"
    )

    stripped = _strip_optional_metric_token_from_curated_esql(query, "optional_metric")

    assert stripped == ""


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


def test_live_optional_metric_is_dropped_without_downgrading_multi_target_panel():
    rule_pack = RulePackConfig(live_optional_metrics=["redis_blocked_clients"])
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
    assert not any("Dropped series whose live target metrics are absent" in reason for reason in result.reasons)
    assert not any("only 1 could be migrated" in reason for reason in result.reasons)


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


def test_esql_param_control_retargets_when_panel_binds_via_to_string_wrapped_mv_contains():
    """issue #353: the field-binding scanner must still recognize
    ``MV_CONTAINS(TO_STRING(?var), field)`` (the type-safe multi-select
    guardrail shape), not just the bare ``MV_CONTAINS(?var, field)`` form."""
    controls = [
        {
            "type": "esql",
            "label": "instance",
            "variable_name": "instance",
            "variable_type": "multi_values",
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
                    "TS metrics-* | WHERE (MV_COUNT(?instance) == 0 OR "
                    'MV_CONTAINS(TO_STRING(?instance), ".*") OR '
                    "MV_CONTAINS(TO_STRING(?instance), instance)) "
                    "| WHERE redis_up IS NOT NULL | STATS value = COUNT(*)"
                )
            }
        }
    ]

    rewritten = _retarget_esql_param_controls_to_panel_bindings(controls, panels)
    query = rewritten[0]["query"]
    assert "`labels.instance`" not in query
    assert "BY instance" in query
    assert rewritten[0]["_resolved_field_name"] == "instance"


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


def test_763_hits_misses_panel_uses_curated_override():
    """The 763 pack's hit/miss rates must use buckets wide enough for IRATE."""
    dashboard = {"gnetId": 763, "title": "Redis...", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    panel = {
        "type": "timeseries",
        "title": "Hits / Misses per Sec",
        "targets": [
            {
                "expr": 'irate(redis_keyspace_hits_total{instance=~"$instance"}[5m])',
                "legendFormat": "hits, {{ instance }}",
                "refId": "A",
            },
            {
                "expr": 'irate(redis_keyspace_misses_total{instance=~"$instance"}[5m])',
                "legendFormat": "misses, {{ instance }}",
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
    assert "AVG(IRATE(" in query
    assert "redis_keyspace_hits_total" in query
    assert "redis_keyspace_misses_total" in query
    assert "labels.instance" in query
    assert "| KEEP time_bucket, `labels.instance`, hits, misses" in query


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


def test_schema_validates_pack_with_collapsed_layout_override():
    from observability_migration.adapters.source.grafana.extension_schema import validate_rule_pack_payload

    raw = {
        "panel": {
            "layout_overrides": [
                {
                    "title_match": "Network Traffic",
                    "collapsed": False,
                }
            ]
        }
    }
    payload = validate_rule_pack_payload(raw)
    assert len(payload.panel.layout_overrides) == 1
    assert payload.panel.layout_overrides[0].title_match == "Network Traffic"
    assert payload.panel.layout_overrides[0].collapsed is False


def test_panel_query_override_loaded_from_pack_yaml_round_trip():
    """load_rule_pack_files parses query_overrides into RulePackConfig.panel_query_overrides."""
    import os
    import tempfile

    from observability_migration.adapters.source.grafana.rules import load_rule_pack_files

    yaml_content = (
        "query:\n"
        "  metric_kinds:\n"
        "    some_gauge: gauge\n"
        "  live_optional_metrics:\n"
        "    - optional_series_total\n"
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
        assert pack.live_optional_metrics == ["optional_series_total"]
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
        "      collapsed: false\n"
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
        assert override["collapsed"] is False
    finally:
        os.unlink(tmp_path)
