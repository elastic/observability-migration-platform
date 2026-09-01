# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the curated dashboard pack registry and resolution engine."""

import json
import tomllib
from pathlib import Path

import pytest

from observability_migration.adapters.source.grafana.curated_packs import (
    find_curated_pack,
    load_curated_registry,
)
from observability_migration.adapters.source.grafana.extension_schema import (
    validate_rule_pack_payload,
)
from observability_migration.adapters.source.grafana.panels import (
    _apply_panel_layout_overrides_recursively,
    _label_placeholder_value_metric,
    _materialize_curated_query_override,
    _omit_absent_optional_metrics_from_curated_query,
    _panel_static_legend_label,
    _prefix_native_metric_fields,
    _resolve_control_scope_metric,
    _retarget_esql_param_controls_to_panel_bindings,
    _strip_optional_metric_token_from_curated_esql,
    translate_dashboard,
    translate_panel,
)
from observability_migration.adapters.source.grafana.promql import (
    _parse_fragment,
    _reduce_or_operands,
)
from observability_migration.adapters.source.grafana.rules import (
    RulePackConfig,
    _merge_curated_into_base,
    load_rule_pack_files,
    resolve_pack_for_dashboard,
)
from observability_migration.adapters.source.grafana.schema import SchemaResolver

DASHBOARD_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "docs" / "dashboards" / "schema.json"
)


def dashboard_schema_errors(panels: list[dict]) -> list[str]:
    """Validate *panels* as a dashboard against the vendored Kibana schema.

    Same schema and validator as the ``tests/e2e`` schema gate, applied to a
    single hand-built dashboard so a layout-override regression is caught in the
    fast unit gate instead of only after a corpus run.
    """
    import jsonschema

    schema = json.loads(DASHBOARD_SCHEMA_PATH.read_text())
    doc = {"dashboards": [{"name": "layout-override-probe", "panels": panels}]}
    return [
        f"{'/'.join(str(part) for part in error.path)}: {error.message}"
        for error in jsonschema.Draft202012Validator(schema).iter_errors(doc)
    ]


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


def test_registry_title_hints_are_unique():
    """The title fallback matches a dashboard that stripped its ``gnetId`` (and
    possibly its tags) on exact ``title_hint`` alone. Two packs sharing a
    ``title_hint`` would make that fallback pick one arbitrarily, so keep them
    distinct (case-insensitive)."""
    entries = load_curated_registry()
    titles = [
        (entry.get("title_hint") or "").strip().lower()
        for entry in entries
        if (entry.get("title_hint") or "").strip()
    ]
    assert len(set(titles)) == len(titles), f"duplicate title_hint in registry: {titles}"


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


def test_registry_pins_match_community_corpus_when_revision_aligns():
    """Packs added in this change must not silently disagree with the committed
    community corpus on the same (gnet_id, revision). Older packs may still
    use a grafana.com canonical pin that predates the corpus hasher (763).
    """
    corpus = json.loads(
        (Path(__file__).resolve().parents[1] / "parity-rig" / "benchmark" / "community_corpus.json").read_text(
            encoding="utf-8"
        )
    )
    by_id_rev = {
        (int(entry["id"]), int(entry["revision"])): str(entry["sha256"])
        for entry in corpus["dashboards"]
    }
    # New packs in this PR. 9628 is pack rev 1 vs corpus rev 8 — no join.
    new_pack_ids = {7362, 9628, 14114, 12485, 315, 6417, 741, 8171}
    mismatches = []
    for entry in load_curated_registry():
        gnet_id = int(entry["gnet_id"])
        if gnet_id not in new_pack_ids:
            continue
        key = (gnet_id, int(entry["gnet_revision"]))
        expected = by_id_rev.get(key)
        if expected is None:
            continue
        actual = str(entry["dashboard_sha256"])
        if actual != expected:
            mismatches.append(
                f"{entry['name']} gnet_id={key[0]} rev={key[1]} "
                f"registry={actual} corpus={expected}"
            )
    assert not mismatches, "registry dashboard_sha256 disagrees with community_corpus.json:\n" + "\n".join(
        mismatches
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


def test_find_763_by_title_fallback_without_tags():
    """Copies/re-imports often strip gnetId and tags; exact title is enough."""
    entry = find_curated_pack(
        gnet_id=None,
        title="Redis Dashboard for Prometheus Redis Exporter 1.x",
        tags=[],
    )
    assert entry is not None
    assert entry["gnet_id"] == 763
    assert entry["name"] == "grafana_763_redis_exporter"


def test_find_763_title_fallback_rejects_unrelated_tags():
    """When the dashboard still has tags, require overlap with tags_hint."""
    entry = find_curated_pack(
        gnet_id=None,
        title="Redis Dashboard for Prometheus Redis Exporter 1.x",
        tags=["mysql"],
    )
    assert entry is None


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


def test_find_7362_by_gnet_id():
    entry = find_curated_pack(gnet_id=7362, title="", tags=[])
    assert entry is not None
    assert entry["gnet_id"] == 7362
    assert entry["name"] == "grafana_7362_mysql_overview"


def test_find_7362_by_title_fallback():
    entry = find_curated_pack(
        gnet_id=None,
        title="MySQL Overview",
        tags=["Percona", "MySQL"],
    )
    assert entry is not None
    assert entry["gnet_id"] == 7362


def test_find_9628_by_gnet_id():
    entry = find_curated_pack(gnet_id=9628, title="", tags=[])
    assert entry is not None
    assert entry["gnet_id"] == 9628
    assert entry["name"] == "grafana_9628_postgresql_database"


def test_find_9628_by_title_fallback():
    entry = find_curated_pack(
        gnet_id=None,
        title="PostgreSQL Database",
        tags=["postgres", "db", "stats"],
    )
    assert entry is not None
    assert entry["gnet_id"] == 9628


def test_find_14114_by_gnet_id():
    entry = find_curated_pack(gnet_id=14114, title="", tags=[])
    assert entry is not None
    assert entry["gnet_id"] == 14114
    assert entry["name"] == "grafana_14114_postgres_exporter_quickstart"


def test_find_14114_by_title_fallback():
    entry = find_curated_pack(
        gnet_id=None,
        title="PostgreSQL Exporter Quickstart and Dashboard",
        tags=["postgres"],
    )
    assert entry is not None
    assert entry["gnet_id"] == 14114


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


def _partial_native_resolver():
    """A prometheus_native resolver whose only knowledge is a partial,
    label-only control-schema merge. ``discovery_status`` stays ``partial`` and
    never becomes ``ok``, so the partial cache proves nothing about metric
    fields (PR #369 review, giorgi-imerlishvili-elastic)."""
    resolver = SchemaResolver(RulePackConfig(), field_profile="prometheus_native")
    resolver.merge_control_schema(
        {"field_cache": {"labels.instance": {"keyword": {"type": "keyword"}}}}
    )
    assert resolver.discovery_status()["status"] == "partial"
    return resolver


def _exhaustive_native_resolver(field_cache):
    """A prometheus_native resolver backed by exhaustive live field-caps."""
    resolver = SchemaResolver(RulePackConfig(), field_profile="prometheus_native")
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = dict(field_cache)
    return resolver


def test_native_prefix_kept_after_partial_control_schema_merge():
    """A prometheus-native Max Connections selector must keep its planned
    ``metrics.`` prefix after a partial (label-only) control-schema merge:
    the partial cache proves nothing about metric fields, so a not-found result
    must not strip the prefix."""
    resolver = _partial_native_resolver()
    rewritten = _prefix_native_metric_fields("pg_settings_max_connections", resolver)
    assert rewritten == "metrics.pg_settings_max_connections"


def test_native_prefix_rejected_when_exhaustive_caps_prove_absent():
    """With exhaustive live field-caps (status ``ok``), a metric that has
    neither a bare nor a ``metrics.`` field must NOT be invented with a
    prefix — the absence-sensitive gate still holds for real field-caps."""
    resolver = _exhaustive_native_resolver(
        {"labels.instance": {"keyword": {"type": "keyword"}}}
    )
    rewritten = _prefix_native_metric_fields("pg_settings_max_connections", resolver)
    assert rewritten == "pg_settings_max_connections"


def test_control_scope_metric_kept_after_partial_control_schema_merge():
    """The 14114 Instance control must stay scoped to ``pg_up`` after a partial
    control-schema merge instead of degrading to an unscoped label_values that
    scans every ``labels.instance`` in mixed ``metrics-*``."""
    resolver = _partial_native_resolver()
    scope = _resolve_control_scope_metric("pg_up", resolver, RulePackConfig())
    assert scope and "pg_up" in scope


def test_control_scope_metric_dropped_when_exhaustive_caps_prove_absent():
    """Exhaustive field-caps that prove the scoping metric absent must still
    drop the scope (scoping on a missing field would empty the control)."""
    resolver = _exhaustive_native_resolver(
        {"labels.instance": {"keyword": {"type": "keyword"}}}
    )
    scope = _resolve_control_scope_metric("pg_up", resolver, RulePackConfig())
    assert scope == ""


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
    # 763 authors canonical label names; the resolver maps `instance` to its
    # OTel spelling under the otel profile (the pack no longer hardcodes any
    # labels.* candidate — that would leak under otel).
    r = SchemaResolver(resolved, field_profile="otel")
    assert r.resolve_label("instance") == "service.instance.id"


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
    target is a bare logical metric name; the resolver namespaces it by the
    active field profile.
    """
    dashboard = {"gnetId": 14091, "title": "Redis Exporter Quickstart", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    entry = (resolved.metric_map or {}).get("redis_memory_fragmentation_ratio")
    target = getattr(entry, "target", entry)
    assert target == "redis_mem_fragmentation_ratio"


def test_resolve_pack_7362_pins_untyped_status_counters_and_processlist_map():
    """mysqld_exporter publishes suffix-less status counters as untyped.

    Without metric_kinds, Elasticsearch stores them as gauges and RATE() 400s.
    The processlist metric was also renamed after this dashboard's revision 5.
    """
    dashboard = {"gnetId": 7362, "title": "MySQL Overview", "tags": ["Percona", "MySQL"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    assert resolved.metric_kinds.get("mysql_global_status_queries") == "counter"
    assert resolved.metric_kinds.get("mysql_global_status_questions") == "counter"
    assert resolved.metric_kinds.get("mysql_global_status_bytes_received") == "counter"
    assert resolved.metric_kinds.get("mysql_info_schema_processlist_threads") == "gauge"
    entry = (resolved.metric_map or {}).get("mysql_info_schema_threads")
    target = getattr(entry, "target", entry)
    assert target == "mysql_info_schema_processlist_threads"
    assert resolved.control_field_overrides.get("host") == "labels.instance"
    titles = {o.get("title_match") for o in resolved.panel_query_overrides}
    assert "Process States" in titles
    assert "MySQL Query Cache Activity" in titles
    assert "CPU Usage / Load" in titles
    assert "mysql_global_variables_query_cache_size" in resolved.live_optional_metrics
    assert "aws_rds_read_latency_average" in resolved.live_optional_metrics
    cpu_override = next(
        o for o in resolved.panel_query_overrides if o.get("title_match") == "CPU Usage / Load"
    )
    assert cpu_override.get("kibana_type_override") == "line"
    titles = {o.get("title_match") for o in resolved.panel_layout_overrides}
    assert "Section 1" in titles
    overview = next(
        o for o in resolved.panel_layout_overrides if o.get("title_match") == "Section 1"
    )
    assert overview.get("title") == "Overview"


def test_7362_hourly_panels_follow_dashboard_time_picker():
    """Grafana pins timeFrom=24h on the hourly charts.

    Mixed ``metrics-*`` Lens 24h windows render ``No results found`` even when
    ``_query`` returns one or two sparse buckets. The pack drops timeFrom so
    these panels follow the dashboard picker like the working sibling MySQL
    rate charts.
    """
    dashboard = {"gnetId": 7362, "title": "MySQL Overview", "tags": ["Percona", "MySQL"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    hourly = next(
        o for o in resolved.panel_query_overrides
        if o.get("title_match") == "MySQL Network Usage Hourly"
    )
    assert hourly.get("drop_time_from") is True
    panel = {
        "id": 1,
        "type": "graph",
        "title": "MySQL Network Usage Hourly",
        "timeFrom": "24h",
        "targets": [
            {
                "expr": "increase(mysql_global_status_bytes_received[1h])",
                "refId": "A",
                "legendFormat": "Received",
            }
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        "fieldConfig": {"defaults": {}, "overrides": []},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    esql = yaml_panel.get("esql") or {}
    assert "time_range" not in esql
    assert "TBUCKET(20" in (esql.get("query") or "")
    assert "mysql_global_status_bytes_received" in (esql.get("query") or "")


def test_7362_cpu_override_binds_busy_pct_and_load():
    dashboard = {"gnetId": 7362, "title": "MySQL Overview", "tags": ["Percona"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    panel = {
        "id": 2,
        "type": "graph",
        "title": "CPU Usage / Load",
        "stack": True,
        "targets": [
            {"expr": 'node_load1{instance="$host"}', "refId": "C", "legendFormat": "Load 1m"}
        ],
        "seriesOverrides": [{"alias": "Load 1m", "yaxis": 2, "stack": False}],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        "yaxes": [
            {"format": "percent", "max": 100, "min": 0},
            {"format": "none", "min": 0},
        ],
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    esql = yaml_panel.get("esql") or {}
    query = esql.get("query") or ""
    assert "CPU_busy_pct" in query
    y_cols = [item.get("field") for item in (esql.get("metrics") or [])]
    assert "CPU_busy_pct" in y_cols
    assert "Load 1m" in y_cols
    assert esql.get("type") == "line"
    load = next(item for item in (esql.get("metrics") or []) if item.get("field") == "Load 1m")
    assert load.get("axis") == "right"
    assert "suffix" not in (load.get("format") or {})


def test_resolve_pack_9628_ignores_helm_release_and_pins_memory_gauges():
    """Revision 1 filters on Helm ``release``; typical scrapes do not store it.

    Grafana also ``rate()``s process RSS/VMS gauges, which Elasticsearch
    rejects as RATE() on double. The pack pins those as gauges and overrides
    Average Memory Usage to LAST_OVER_TIME.
    """
    dashboard = {
        "gnetId": 9628,
        "title": "PostgreSQL Database",
        "tags": ["postgres", "db", "stats"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    assert "release" in resolved.ignored_labels
    assert resolved.metric_kinds.get("process_resident_memory_bytes") == "gauge"
    assert resolved.metric_kinds.get("process_virtual_memory_bytes") == "gauge"
    assert resolved.metric_kinds.get("pg_stat_database_xact_commit") == "counter"
    assert resolved.control_field_overrides.get("instance") == "labels.instance"
    assert resolved.control_field_overrides.get("datname") == "labels.datname"
    titles = {o.get("title_match") for o in resolved.panel_query_overrides}
    assert "Average Memory Usage" in titles
    assert "Start Time" in titles
    assert "Version" in titles
    assert "pg_postmaster_start_time_seconds" in resolved.live_optional_metrics


def test_9628_memory_override_uses_last_over_time():
    dashboard = {"gnetId": 9628, "title": "PostgreSQL Database", "tags": ["postgres"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    panel = {
        "id": 24,
        "type": "graph",
        "title": "Average Memory Usage",
        "targets": [
            {
                "expr": 'avg(rate(process_resident_memory_bytes{instance="$instance"}[5m]))',
                "refId": "A",
                "legendFormat": "Resident Mem",
            }
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        "yaxes": [{"format": "decbytes"}, {"format": "short"}],
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert "LAST_OVER_TIME" in query
    assert "RATE(" not in query
    assert "process_resident_memory_bytes" in query


def test_9628_start_time_override_does_not_yellow_absent_postmaster_metric():
    """postgres_exporter v0.15 dropped pg_postmaster_start_time_seconds.

    The override substitutes process_start_time_seconds. That source metric is
    live_optional, so an absent field-caps hit must not yellow the panel as a
    pack omission.
    """
    dashboard = {"gnetId": 9628, "title": "PostgreSQL Database", "tags": ["postgres"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    resolver._field_cache = {
        "metrics.process_start_time_seconds": {"double": {"type": "double"}},
        "metrics.pg_static": {"double": {"type": "double"}},
        "labels.instance": {"keyword": {"type": "keyword"}},
        "labels.short_version": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    panel = {
        "id": 28,
        "type": "singlestat",
        "title": "Start Time",
        "format": "dateTimeFromNow",
        "targets": [
            {
                "expr": 'pg_postmaster_start_time_seconds{instance="$instance"} * 1000',
                "refId": "A",
            }
        ],
        "gridPos": {"x": 0, "y": 0, "w": 4, "h": 2},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status == "migrated", result.reasons
    assert not any("curated override" in reason for reason in result.reasons)
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert "process_start_time_seconds" in query
    assert "DATE_DIFF" in query
    assert "pg_postmaster_start_time_seconds" not in query


def test_9628_version_metric_displays_version_label_not_static_one():
    """The Version tile must display the PostgreSQL version label
    (``labels.short_version``), not the numeric ``pg_static=1``. The metric
    panel binds the label as a breakdown so the version string is visible
    (PR #369 follow-up, giorgi-imerlishvili-elastic)."""
    dashboard = {"gnetId": 9628, "title": "PostgreSQL Database", "tags": ["postgres"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    resolver._field_cache = {
        "metrics.pg_static": {"double": {"type": "double"}},
        "labels.short_version": {"keyword": {"type": "keyword"}},
        "labels.instance": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    panel = {
        "id": 1,
        "type": "singlestat",
        "title": "Version",
        "targets": [{"expr": "pg_static", "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 4, "h": 2},
    }
    yaml_panel, _result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    esql = yaml_panel.get("esql") or {}
    assert esql.get("type") == "metric"
    assert (esql.get("breakdown") or {}).get("field") == "labels.short_version"
    assert "labels.short_version" in (esql.get("query") or "")


def test_9628_start_time_metric_has_duration_format():
    """The Start Time tile computes elapsed seconds; it must carry a duration
    format so it renders as a duration rather than a raw number (PR #369)."""
    dashboard = {"gnetId": 9628, "title": "PostgreSQL Database", "tags": ["postgres"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    resolver._field_cache = {
        "metrics.process_start_time_seconds": {"double": {"type": "double"}},
        "labels.instance": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    panel = {
        "id": 2,
        "type": "singlestat",
        "title": "Start Time",
        "targets": [{"expr": "process_start_time_seconds", "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 4, "h": 2},
    }
    yaml_panel, _result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    esql = yaml_panel.get("esql") or {}
    assert esql.get("type") == "metric"
    assert (esql.get("primary") or {}).get("format", {}).get("type") == "duration"


def test_7362_cpu_system_panel_surfaces_cross_host_approximation():
    """The 7362 CPU Usage / Load override aggregates across every node exporter
    (``COUNT_DISTINCT(labels.cpu)`` is global; hosts reuse CPU IDs), which can
    exceed 100%. It must surface an approximation warning and downgrade instead
    of reporting green (PR #369 follow-up, giorgi-imerlishvili-elastic)."""
    dashboard = {"gnetId": 7362, "title": "MySQL Overview", "tags": ["mysql"]}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    resolver._field_cache = {
        "metrics.node_cpu_seconds_total": {"double": {"type": "double"}},
        "metrics.node_load1": {"double": {"type": "double"}},
        "labels.cpu": {"keyword": {"type": "keyword"}},
        "labels.mode": {"keyword": {"type": "keyword"}},
        "labels.instance": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    panel = {
        "id": 3,
        "type": "timeseries",
        "title": "CPU Usage / Load",
        "targets": [{"expr": "sum(rate(node_cpu_seconds_total[5m]))", "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
    }
    _yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status == "migrated_with_warnings", result.reasons
    assert any(
        "host" in reason.lower()
        and ("approxim" in reason.lower() or "aggregat" in reason.lower())
        for reason in result.reasons
    ), result.reasons


def test_9628_instance_query_result_becomes_label_values_control():
    """Helm query_result(pg_up{release=...}) has no Kibana populate query.

    The pack plugin rewrites Instance to label_values(pg_up, instance) and
    drops the unused namespace/release cascade so the dashboard still gets
    an Instance control.
    """
    dashboard = {
        "gnetId": 9628,
        "title": "PostgreSQL Database",
        "tags": ["postgres"],
        "templating": {
            "list": [
                {
                    "name": "namespace",
                    "type": "query",
                    "label": "Namespace",
                    "query": "query_result(pg_exporter_last_scrape_duration_seconds)",
                },
                {
                    "name": "release",
                    "type": "query",
                    "label": "Release",
                    "query": 'query_result(pg_exporter_last_scrape_duration_seconds{kubernetes_namespace="$namespace"})',
                },
                {
                    "name": "instance",
                    "type": "query",
                    "label": "Instance",
                    "query": 'query_result(pg_up{release="$release"})',
                },
                {
                    "name": "datname",
                    "type": "query",
                    "label": "Database",
                    "query": "label_values(datname)",
                    "includeAll": True,
                    "multi": True,
                },
            ]
        },
        "panels": [
            {
                "id": 38,
                "type": "singlestat",
                "title": "Max Connections",
                "targets": [
                    {
                        "expr": 'pg_settings_max_connections{instance="$instance"}',
                        "refId": "A",
                    }
                ],
                "gridPos": {"x": 0, "y": 0, "w": 4, "h": 2},
                "format": "none",
            }
        ],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
    )
    payload = result.dashboard_ir.to_yaml_dict()
    controls = payload.get("controls") or []
    names = {control.get("variable_name") for control in controls}
    assert "instance" in names, controls
    assert "namespace" not in names
    assert "release" not in names
    assert not any("query_result" in warning for warning in (result.control_warnings or [])), (
        result.control_warnings
    )
    instance = next(c for c in controls if c.get("variable_name") == "instance")
    query = str(instance.get("query") or "")
    assert "labels.instance" in query or "instance" in query


def test_9628_dashboard_does_not_emit_release_control():
    """Native PROMQL must not resurrect Helm $release as a Kibana control.

    Mixed metrics-* has a kernel ``release`` field; binding it filters Postgres
    series to nothing (Max Connections / CPU / Open FDs empty in view mode).
    """
    from observability_migration.adapters.source.grafana.runtime_features import (
        KIBANA_PROMQL_CONTROL_PARAMS,
        PROMQL_LABEL_MATCHER_PARAMS,
        set_runtime_feature,
    )

    dashboard = {
        "gnetId": 9628,
        "title": "PostgreSQL Database",
        "tags": ["postgres"],
        "templating": {
            "list": [
                {
                    "name": "release",
                    "type": "query",
                    "label": "Release",
                    "query": 'query_result(pg_up{release="x"})',
                },
                {
                    "name": "instance",
                    "type": "query",
                    "label": "Instance",
                    "query": 'query_result(pg_up{release="$release"})',
                },
            ]
        },
        "panels": [
            {
                "id": 38,
                "type": "singlestat",
                "title": "Max Connections",
                "targets": [
                    {
                        "expr": 'pg_settings_max_connections{release="$release", instance="$instance"}',
                        "refId": "A",
                    }
                ],
                "gridPos": {"x": 0, "y": 0, "w": 4, "h": 2},
                "format": "none",
            }
        ],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolved.native_promql = True
    set_runtime_feature(
        resolved, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="test"
    )
    set_runtime_feature(
        resolved, KIBANA_PROMQL_CONTROL_PARAMS, supported=True, source="test"
    )
    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
    )
    payload = result.dashboard_ir.to_yaml_dict()
    names = {c.get("variable_name") for c in (payload.get("controls") or [])}
    assert "release" not in names, names
    max_conn = next(p for p in result.panel_results if p.title == "Max Connections")
    query = max_conn.esql_query or ""
    assert "release" not in query
    assert "?instance" in query or "instance" in query


def test_resolve_pack_14114_pins_counters_and_bgwriter_map():
    """Mixin Buffers names lack OpenMetrics _total; v0.15 exporters add it."""
    dashboard = {
        "gnetId": 14114,
        "title": "PostgreSQL Exporter Quickstart and Dashboard",
        "tags": ["postgres"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    assert resolved.metric_kinds.get("pg_stat_database_xact_commit") == "counter"
    assert resolved.metric_kinds.get("pg_stat_database_numbackends") == "gauge"
    entry = (resolved.metric_map or {}).get("pg_stat_bgwriter_buffers_alloc")
    target = getattr(entry, "target", entry)
    assert target == "pg_stat_bgwriter_buffers_alloc_total"
    assert resolved.control_field_overrides.get("instance") == "labels.instance"
    assert resolved.control_field_overrides.get("db") == "labels.datname"


def test_14114_buffers_override_uses_total_suffix_offline():
    dashboard = {
        "gnetId": 14114,
        "title": "PostgreSQL Exporter Quickstart and Dashboard",
        "tags": ["postgres"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    # Assert the prometheus_native emission (metrics.<name>): metric_map targets
    # are now bare logical names namespaced per profile, so the native output is
    # what pins the metrics.* spelling.
    resolver = SchemaResolver(resolved, field_profile="prometheus_native")
    panel = {
        "id": 2,
        "type": "graph",
        "title": "Buffers",
        "targets": [
            {
                "expr": "irate(pg_stat_bgwriter_buffers_alloc{instance=~'$instance'}[5m])",
                "refId": "A",
                "legendFormat": "buffers_alloc",
            }
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert "IRATE(metrics.pg_stat_bgwriter_buffers_alloc_total)" in query
    assert "IRATE(metrics.pg_stat_bgwriter_buffers_alloc)" not in query


def test_14114_instance_up_becomes_pg_up_control():
    """Mixin Instance lists Prometheus ``up``; Elastic stores postgres as ``pg_up``."""
    dashboard = {
        "gnetId": 14114,
        "title": "PostgreSQL Exporter Quickstart and Dashboard",
        "tags": ["postgres"],
        "templating": {
            "list": [
                {
                    "name": "instance",
                    "type": "query",
                    "label": "instance",
                    "query": 'label_values(up{job=~"postgres.*"},instance)',
                    "includeAll": True,
                    "current": {"selected": False, "text": "All", "value": "$__all"},
                },
                {
                    "name": "job",
                    "type": "query",
                    "label": "job",
                    "query": "label_values(pg_up, job)",
                    "includeAll": False,
                    "current": {"selected": True, "text": "postgres", "value": "postgres"},
                },
            ]
        },
        "panels": [
            {
                "id": 11,
                "type": "singlestat",
                "title": "QPS",
                "targets": [
                    {
                        "expr": (
                            'sum(irate(pg_stat_database_xact_commit{instance=~"$instance"}[5m]))'
                        ),
                        "refId": "A",
                    }
                ],
                "gridPos": {"x": 0, "y": 0, "w": 4, "h": 3},
                "format": "none",
            }
        ],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
    )
    payload = result.dashboard_ir.to_yaml_dict()
    controls = payload.get("controls") or []
    names = {control.get("variable_name") for control in controls}
    assert "instance" in names, controls
    assert "job" not in names, controls
    instance = next(c for c in controls if c.get("variable_name") == "instance")
    query = str(instance.get("query") or "")
    assert "pg_up" in query or "metrics.pg_up" in query
    assert "metrics.up" not in query


def test_find_12485_by_gnet_id():
    entry = find_curated_pack(gnet_id=12485, title="", tags=[])
    assert entry is not None
    assert entry["gnet_id"] == 12485
    assert entry["name"] == "grafana_12485_postgresql_exporter"


def test_resolve_pack_12485_pins_kinds_renames_and_controls():
    dashboard = {"gnetId": 12485, "title": "PostgreSQL Exporter", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    # The two `_count` gauges are the important correctness fix.
    assert resolved.metric_kinds.get("pg_stat_activity_count") == "gauge"
    assert resolved.metric_kinds.get("pg_locks_count") == "gauge"
    assert resolved.metric_kinds.get("pg_stat_database_numbackends") == "gauge"
    # Rated counters stay counters.
    assert resolved.metric_kinds.get("pg_stat_database_xact_commit") == "counter"
    assert resolved.metric_kinds.get("pg_stat_database_tup_fetched") == "counter"
    # v0.15 renames. Targets are bare logical metric names; the resolver
    # namespaces them per active field profile.
    for src, tgt in (
        ("pg_database_size", "pg_database_size_bytes"),
        ("pg_replication_lag", "pg_replication_lag_seconds"),
        ("pg_stat_statements_calls", "pg_stat_statements_calls_total"),
        ("pg_stat_statements_total_time_seconds", "pg_stat_statements_seconds_total"),
    ):
        entry = (resolved.metric_map or {}).get(src)
        assert getattr(entry, "target", entry) == tgt, src
    # Controls keyed by the dashboard's capitalised variable names.
    assert resolved.control_field_overrides.get("Instance") == "labels.instance"
    assert resolved.control_field_overrides.get("Database") == "labels.datname"


def test_12485_database_size_renamed_offline():
    dashboard = {"gnetId": 12485, "title": "PostgreSQL Exporter", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    # Assert the prometheus_native emission (metrics.<name>): metric_map targets
    # are now bare logical names namespaced per profile.
    resolver = SchemaResolver(resolved, field_profile="prometheus_native")
    panel = {
        "id": 37,
        "type": "singlestat",
        "title": "Total database size",
        "targets": [{"expr": 'sum(pg_database_size{instance="$Instance"})', "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 4, "h": 3},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert "metrics.pg_database_size_bytes" in query
    assert "metrics.pg_database_size)" not in query


def test_12485_activity_count_is_gauge_not_rated():
    dashboard = {"gnetId": 12485, "title": "PostgreSQL Exporter", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    panel = {
        "id": 24,
        "type": "graph",
        "title": "Connections by state (stacked)",
        "targets": [{"expr": 'sum by (state) (pg_stat_activity_count{instance="$Instance"})', "refId": "A"}],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert "pg_stat_activity_count" in query
    # gauge → SUM, never RATE/IRATE (the whole point of forcing the _count gauge).
    assert "RATE(" not in query.upper()
    assert "labels.state" in query


def test_12485_instance_and_database_controls_rewritten():
    dashboard = {
        "gnetId": 12485,
        "title": "PostgreSQL Exporter",
        "tags": [],
        "templating": {
            "list": [
                {
                    "name": "Instance",
                    "type": "query",
                    "label": "Instance",
                    "query": 'label_values({job="postgres-exporter"}, instance)',
                    "includeAll": False,
                    "current": {"text": "postgres:5432", "value": "postgres:5432"},
                },
                {
                    "name": "Database",
                    "type": "query",
                    "label": "Database",
                    "query": "label_values(datname)",
                    "includeAll": True,
                    "current": {"text": "All", "value": "$__all"},
                },
                {
                    "name": "Interval",
                    "type": "interval",
                    "query": "30sec,1m,10m,30m,1h,6h,12h,1d",
                    "current": {"text": "1m", "value": "1m"},
                },
            ]
        },
        "panels": [
            {
                "id": 14,
                "type": "singlestat",
                "title": "Transaction rate",
                "targets": [
                    {
                        "expr": 'sum(rate(pg_stat_database_xact_commit{instance="$Instance",datname=~"$Database"}[$Interval]))',
                        "refId": "A",
                    }
                ],
                "gridPos": {"x": 0, "y": 0, "w": 4, "h": 3},
            }
        ],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
    )
    payload = result.dashboard_ir.to_yaml_dict()
    controls = payload.get("controls") or []
    names = {c.get("variable_name") for c in controls}
    # Interval must never become a control; Instance/Database must.
    assert "Interval" not in names, controls
    assert "Instance" in names, controls
    instance = next(c for c in controls if c.get("variable_name") == "Instance")
    iq = str(instance.get("query") or "")
    assert "pg_up" in iq
    assert "postgres-exporter" not in iq
    # Database is a core claim of the pack: the bare ``label_values(datname)``
    # has no metric anchor, so the control must be present AND anchored on the
    # curated per-database gauge (an unanchored ``labels.datname`` query is the
    # broken pre-pack behavior, not an acceptable fallback).
    assert "Database" in names, controls
    database = next(c for c in controls if c.get("variable_name") == "Database")
    dq = str(database.get("query") or "")
    assert "pg_stat_database_numbackends" in dq, dq


def _pinned_12485_repeat_row_dashboard() -> dict:
    """Minimal dashboard in the shape of pinned grafana.com 12485 revision 1.

    Faithful to the parts this test is about: ``Database`` is a
    ``multi``/``includeAll`` query variable with no cached ``current``/
    ``options``, and the ``Database: $Database`` row is a *collapsed repeated*
    row (``repeat: Database``) holding the per-database panels.
    """
    return {
        "gnetId": 12485,
        "title": "PostgreSQL Exporter",
        "tags": [],
        "templating": {
            "list": [
                {
                    "name": "Instance",
                    "type": "query",
                    "label": "Instance",
                    "query": 'label_values({job="postgres-exporter"}, instance)',
                    "includeAll": False,
                    "multi": False,
                    "current": {},
                    "options": [],
                },
                {
                    "name": "Database",
                    "type": "query",
                    "label": "Database",
                    "query": "label_values(datname)",
                    "includeAll": True,
                    "multi": True,
                    "current": {},
                    "options": [],
                },
            ]
        },
        "panels": [
            {
                "id": 2,
                "type": "row",
                "title": "Global Statistics",
                "collapsed": False,
                "panels": [],
                "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1},
            },
            {
                "id": 14,
                "type": "singlestat",
                "title": "Transaction rate",
                "targets": [
                    {
                        "expr": 'sum(rate(pg_stat_database_xact_commit{instance="$Instance"}[5m]))',
                        "refId": "A",
                    }
                ],
                "gridPos": {"x": 0, "y": 1, "w": 4, "h": 3},
            },
            {
                "id": 100,
                "type": "row",
                "title": "Database: $Database",
                "repeat": "Database",
                "collapsed": True,
                "gridPos": {"x": 0, "y": 10, "w": 24, "h": 1},
                "panels": [
                    {
                        "id": 101,
                        "type": "singlestat",
                        "title": "Active clients",
                        "targets": [
                            {
                                "expr": (
                                    'sum(pg_stat_activity_count{instance="$Instance",'
                                    'datname=~"$Database",state="active"})'
                                ),
                                "refId": "A",
                            }
                        ],
                        "gridPos": {"x": 0, "y": 11, "w": 4, "h": 3},
                    }
                ],
            },
        ],
    }


def test_12485_repeated_database_row_becomes_single_select_control():
    """The pinned source repeats the Database row over a multi-select variable.

    Kibana cannot repeat panels, so the engine deliberately emits ONE section
    with a single-select Database control plus an explicit warning. The pack's
    fidelity manifest has to disclose that (see the manifest test below), and
    this test pins the behavior the disclosure describes.
    """
    dashboard = _pinned_12485_repeat_row_dashboard()
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())

    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
    )

    controls = result.dashboard_ir.to_yaml_dict().get("controls") or []
    database = next(
        (c for c in controls if c.get("variable_name") == "Database"), None
    )
    assert database is not None, controls
    assert database.get("multiple") is False, database
    assert any(
        "drives panel repetition" in warning for warning in result.control_warnings
    ), result.control_warnings


def test_12485_fidelity_manifest_discloses_repeated_database_row_gap():
    """Repo rule: an operator-visible structural loss must be disclosed.

    28 PERFECT panel labels and a repopulated Database control must not read as
    "Grafana's repeated per-database rows were preserved" -- they were not.
    """
    import yaml

    from observability_migration.adapters.source.grafana import (
        curated_packs as _curated_packs_pkg,
    )

    manifest_path = (
        Path(_curated_packs_pkg.__file__).parent
        / "grafana_12485_postgresql_exporter"
        / "fidelity_manifest.yaml"
    )
    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    known_gaps = [
        str(gap) for gap in (manifest.get("summary") or {}).get("known_gaps") or []
    ]

    disclosure = [
        gap
        for gap in known_gaps
        if "repeat" in gap.lower() and "single-select" in gap.lower()
    ]
    assert disclosure, (
        "fidelity_manifest.yaml must disclose that the repeated 'Database: "
        f"$Database' row becomes one single-select section; known_gaps={known_gaps}"
    )
    assert "Database" in disclosure[0]


def test_12485_io_override_names_read_and_write():
    dashboard = {"gnetId": 12485, "title": "PostgreSQL Exporter", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    panel = {
        "id": 26,
        "type": "graph",
        "title": "I/O Read/Write time",
        "targets": [
            {
                "expr": 'sum(rate(pg_stat_database_blk_read_time{instance="$Instance"}[1m]))',
                "legendFormat": "blk_read_time",
                "refId": "A",
            },
            {
                "expr": 'sum(rate(pg_stat_database_blk_write_time{instance="$Instance"}[1m]))',
                "legendFormat": "blk_read_time",
                "refId": "B",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 9},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert "Read =" in query
    assert "Write =" in query
    assert "blk_read_time_B" not in query


def test_12485_avg_query_runtime_skips_null_last_bucket():
    dashboard = {"gnetId": 12485, "title": "PostgreSQL Exporter", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    panel = {
        "id": 102,
        "type": "singlestat",
        "title": "Average query runtime",
        "format": "s",
        "valueName": "current",
        "targets": [
            {
                "expr": (
                    'sum((delta(pg_stat_statements_total_time_seconds'
                    '{instance="$Instance"}[5m])))'
                    '/sum((delta(pg_stat_statements_calls'
                    '{instance="$Instance"}[5m])))'
                ),
                "refId": "A",
            },
        ],
        "gridPos": {"x": 8, "y": 7, "w": 4, "h": 3},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    esql = yaml_panel.get("esql") or {}
    query = esql.get("query") or ""
    assert "LAST(value, step)" not in query
    assert "computed_value" in query
    assert "WHERE computed_value IS NOT NULL" in query
    assert "LIMIT 2" in query
    assert "RATE(" in query
    assert "pg_stat_statements_seconds_total" in query
    assert "pg_stat_statements_calls_total" in query
    primary = esql.get("primary") or {}
    assert (primary.get("format") or {}).get("type") == "duration"


def test_12485_deadlocks_override_legends_by_datname_and_scopes_database():
    dashboard = {"gnetId": 12485, "title": "PostgreSQL Exporter", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    panel = {
        "id": 30,
        "type": "graph",
        "title": "Deadlocks by database",
        "legend": {"show": True, "hideZero": True, "hideEmpty": True},
        "targets": [
            {
                "expr": (
                    'sum by (datname) ((rate(pg_stat_database_deadlocks'
                    '{instance="$Instance"}[5m])))'
                ),
                "legendFormat": "{{datname}}",
                "refId": "A",
            },
        ],
        "gridPos": {"x": 12, "y": 37, "w": 12, "h": 5},
    }

    global_yaml, global_result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
        section_title="Global Statistics",
    )
    assert global_result.status in {"migrated", "migrated_with_warnings"}, global_result.reasons
    global_query = (global_yaml.get("esql") or {}).get("query") or ""
    assert "labels.datname" in global_query
    assert "?Database" not in global_query
    assert "LAST(value, step)" not in global_query

    db_yaml, db_result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
        section_title="Database: $Database",
    )
    assert db_result.status in {"migrated", "migrated_with_warnings"}, db_result.reasons
    db_query = (db_yaml.get("esql") or {}).get("query") or ""
    assert "labels.datname" in db_query
    assert "?Database" in db_query


def test_panel_layout_overrides_can_move_legend_right():
    panels = [
        {
            "title": "Locks by state",
            "esql": {
                "type": "bar",
                "mode": "stacked",
                "query": "FROM metrics-*",
                "legend": {"visible": "show", "position": "bottom"},
            },
            "position": {"x": 0, "y": 0},
            "size": {"w": 24, "h": 16},
        }
    ]
    overrides = [
        {
            "title_match": "Locks by state",
            "legend_position": "right",
        }
    ]

    _apply_panel_layout_overrides_recursively(panels, overrides)

    assert panels[0]["esql"]["legend"]["position"] == "right"


def test_12485_layout_fills_kpi_row_and_unhides_gauges():
    dashboard = {"gnetId": 12485, "title": "PostgreSQL Exporter", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    by_title = {}
    for item in resolved.panel_layout_overrides:
        key = (item["title_match"], item.get("section_match") or "")
        by_title[key] = item
    assert by_title[("Total database size", "")]["size"]["w"] == 12
    assert by_title[("Average query runtime", "")]["position"]["x"] == 12
    assert by_title[("Shared Buffers", "")]["position"]["x"] == 24
    assert by_title[("Max Connections", "")]["position"]["x"] == 36
    assert "y" not in by_title[("Total database size", "")].get("position", {})
    assert by_title[("Shared Buffer Hits", "")]["hide_title"] is False
    assert by_title[("Connections used", "")]["hide_title"] is False
    assert by_title[("Commit Ratio", "")]["hide_title"] is False
    assert by_title[("Database", "")]["collapsed"] is False
    assert by_title[("Locks by state", "")]["kibana_type_override"] == "bar"
    assert by_title[("Locks by state", "")]["xy_mode"] == "stacked"
    assert by_title[("Locks by state", "")]["legend_position"] == "right"
    assert by_title[("Replication lag", "")]["size"]["w"] == 48
    assert by_title[("Replication lag", "")]["position"]["y"] == 84
    assert by_title[("I/O Read/Write time", "")]["size"]["h"] == 16
    assert by_title[("Transactions", "Global Statistics")]["position"]["y"] == 36
    assert by_title[("Active clients", "Database")]["position"] == {"x": 0, "y": 0}
    assert by_title[("Transaction rate", "Database")]["size"]["w"] == 16
    assert by_title[("Temporary files by database", "Database")]["position"]["y"] == 58


def test_14114_numbackends_override_drops_name_breakdown():
    dashboard = {
        "gnetId": 14114,
        "title": "PostgreSQL Exporter Quickstart and Dashboard",
        "tags": ["postgres"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved)
    panel = {
        "id": 6,
        "type": "graph",
        "title": "Number of active connections",
        "targets": [
            {
                "expr": 'pg_stat_database_numbackends{datname=~"$db",instance=~"$instance"}',
                "legendFormat": "{{__name__}}",
                "refId": "A",
            }
        ],
        "gridPos": {"x": 12, "y": 14, "w": 12, "h": 7},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    esql = yaml_panel.get("esql") or {}
    query = esql.get("query") or ""
    assert "connections =" in query
    assert "__name__" not in query
    assert "GROK" not in query
    assert "labels.datname" in query
    # Source is one series per (instance, datname); grouping only by datname
    # would MAX-collapse two exporters that share a database name.
    assert "labels.instance" in query
    stats_line = next(
        line for line in query.splitlines() if "STATS connections" in line
    )
    assert "labels.instance" in stats_line
    assert "labels.datname" in stats_line
    assert (esql.get("breakdown") or {}).get("field") == "series_group"
    assert "EVAL series_group = CONCAT(" in query
    qps = next(
        item for item in resolved.panel_layout_overrides if item["title_match"] == "QPS"
    )
    assert qps["size"]["h"] == 14
    assert qps["size"]["w"] == 8
    rows = next(
        item for item in resolved.panel_layout_overrides if item["title_match"] == "Rows"
    )
    assert rows["size"] == {"w": 40, "h": 14}


def test_prometheus_native_label_candidates_come_first_in_redis_packs():
    """Offline prometheus_native runs must emit labels.<name> for every pack.

    These packs describe Prometheus scrapes. They author canonical label names
    and the resolver namespaces them to the prometheus_native layout
    (labels.<name>) offline, so a Prometheus scrape deployment gets the correct
    field without probing the target (previously guaranteed by pinning a
    labels.* candidate first, which leaked under otel).
    """
    expected_native = {
        763: [("instance", "labels.instance"), ("job", "labels.job")],
        18405: [("cluster", "labels.cluster"), ("bdb", "labels.bdb")],
        18406: [("cluster", "labels.cluster"), ("bdb", "labels.bdb")],
        14091: [("instance", "labels.instance"), ("job", "labels.job")],
        11835: [("instance", "labels.instance"), ("job", "labels.job")],
        7362: [("instance", "labels.instance"), ("job", "labels.job")],
        9628: [("instance", "labels.instance"), ("job", "labels.job")],
        14114: [("instance", "labels.instance"), ("job", "labels.job")],
        12485: [("instance", "labels.instance"), ("job", "labels.job")],
    }
    for gnet_id, pairs in expected_native.items():
        resolved = resolve_pack_for_dashboard(
            {"gnetId": gnet_id, "title": "", "tags": []}, RulePackConfig()
        )
        r = SchemaResolver(resolved, field_profile="prometheus_native")
        for label, native in pairs:
            got = r.resolve_label(label)
            assert got == native, (
                f"{gnet_id}: {label} resolves to {got} under prometheus_native, "
                f"expected {native}"
            )


def test_763_pack_authors_canonical_instance_for_queries_and_controls():
    resolved = resolve_pack_for_dashboard(
        {"gnetId": 763, "title": "Redis...", "tags": ["redis"]},
        RulePackConfig(),
    )
    # Canonical authoring: no hardcoded labels.* rewrite, and the control
    # override is the canonical label name. The resolver namespaces per profile.
    assert "instance" not in resolved.label_rewrites
    assert resolved.control_field_overrides.get("instance") == "instance"
    r_native = SchemaResolver(resolved, field_profile="prometheus_native")
    assert r_native.resolve_control_field("instance") == "labels.instance"
    r_otel = SchemaResolver(resolved, field_profile="otel")
    assert r_otel.resolve_control_field("instance") == "service.instance.id"


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


def test_find_11835_by_title_fallback_without_tags():
    """Helm 11835 copies that strip gnetId and tags must still get the pack."""
    entry = find_curated_pack(
        gnet_id=None,
        title="Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha)",
        tags=[],
    )
    assert entry is not None
    assert entry["gnet_id"] == 11835
    assert entry["name"] == "grafana_11835_redis_exporter_helm"


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
    # Canonical authoring: the control resolves the `namespace` label per
    # profile (bare offline / OTel-shaped default; labels.namespace under
    # prometheus_native). Assert the resolved field is bound, not a hardcoded
    # labels.* spelling.
    assert "namespace IS NOT NULL" in str(namespace_control.get("query") or "")
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


def test_panel_layout_override_user_section_does_not_drop_other_section():
    """A user override for one section must not delete the curated sibling."""
    from observability_migration.adapters.source.grafana.rules import _merge_curated_into_base

    curated = RulePackConfig()
    curated.panel_layout_overrides = [
        {"title_match": "Transactions", "section_match": "Global", "size": {"w": 24}},
        {"title_match": "Transactions", "section_match": "Database", "size": {"w": 16}},
        {"title_match": "Locks by state", "xy_mode": "stacked"},
    ]
    curated._curated_pack_name = "test_curated"

    user = RulePackConfig()
    user.panel_layout_overrides = [
        {"title_match": "Transactions", "section_match": "Database", "size": {"w": 12}},
    ]

    merged = _merge_curated_into_base(curated, user)
    by_key = {
        (item["title_match"], item.get("section_match") or ""): item
        for item in merged.panel_layout_overrides
    }
    assert by_key[("Transactions", "Global")]["size"] == {"w": 24}
    assert by_key[("Transactions", "Database")]["size"] == {"w": 12}
    assert by_key[("Locks by state", "")]["xy_mode"] == "stacked"


def test_panel_override_merge_strips_whitespace_keys():
    """Padded user keys must replace the matching curated override, not layer both."""
    from observability_migration.adapters.source.grafana.rules import _merge_curated_into_base

    curated = RulePackConfig()
    curated.panel_query_overrides = [
        {"title_match": "Transactions", "section_match": "Database", "esql_query": "-- curated"},
    ]
    curated.panel_layout_overrides = [
        {"title_match": "Locks by state", "xy_mode": "stacked"},
    ]
    curated._curated_pack_name = "test_curated"

    user = RulePackConfig()
    user.panel_query_overrides = [
        {"title_match": " Transactions ", "section_match": " Database ", "esql_query": "-- user"},
    ]
    user.panel_layout_overrides = [
        {"title_match": " Locks by state ", "xy_mode": "grouped"},
    ]

    merged = _merge_curated_into_base(curated, user)
    assert len(merged.panel_query_overrides) == 1
    assert merged.panel_query_overrides[0]["esql_query"] == "-- user"
    assert len(merged.panel_layout_overrides) == 1
    assert merged.panel_layout_overrides[0]["xy_mode"] == "grouped"


def test_panel_layout_override_user_panel_id_keeps_sibling():
    from observability_migration.adapters.source.grafana.rules import _merge_curated_into_base

    curated = RulePackConfig()
    curated.panel_layout_overrides = [
        {"title_match": "Used", "panel_id": 38, "title": "Memory used"},
        {"title_match": "Used", "panel_id": 40, "title": "CPU used"},
    ]
    curated._curated_pack_name = "test_curated"

    user = RulePackConfig()
    user.panel_layout_overrides = [
        {"title_match": "Used", "panel_id": 38, "title": "Working set"},
    ]

    merged = _merge_curated_into_base(curated, user)
    by_id = {str(item.get("panel_id")): item["title"] for item in merged.panel_layout_overrides}
    assert by_id["38"] == "Working set"
    assert by_id["40"] == "CPU used"
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


def test_panel_layout_overrides_can_unhide_gauge_title():
    panels = [
        {
            "title": "Shared Buffer Hits",
            "hide_title": True,
            "esql": {
                "type": "gauge",
                "metric": {"field": "value", "label": "Shared Buffer Hits"},
            },
            "position": {"x": 0, "y": 0},
            "size": {"w": 8, "h": 14},
        }
    ]
    overrides = [{"title_match": "Shared Buffer Hits", "hide_title": False}]

    _apply_panel_layout_overrides_recursively(panels, overrides)

    assert "hide_title" not in panels[0]
    assert panels[0]["esql"]["metric"]["label"] == " "


def test_panel_layout_overrides_section_match_skips_other_section():
    panels = [
        {
            "title": "Global Statistics",
            "section": {
                "collapsed": False,
                "panels": [
                    {
                        "title": "Transaction rate",
                        "position": {"x": 8, "y": 6},
                        "size": {"w": 8, "h": 5},
                    }
                ],
            },
        },
        {
            "title": "Database",
            "section": {
                "collapsed": True,
                "panels": [
                    {
                        "title": "Transaction rate",
                        "position": {"x": 0, "y": 5},
                        "size": {"w": 8, "h": 5},
                    }
                ],
            },
        },
    ]
    overrides = [
        {
            "title_match": "Transaction rate",
            "section_match": "Database",
            "position": {"x": 32, "y": 0},
            "size": {"w": 16, "h": 8},
        }
    ]

    _apply_panel_layout_overrides_recursively(panels, overrides)

    global_txn = panels[0]["section"]["panels"][0]
    db_txn = panels[1]["section"]["panels"][0]
    assert global_txn["position"] == {"x": 8, "y": 6}
    assert db_txn["position"] == {"x": 32, "y": 0}
    assert db_txn["size"] == {"w": 16, "h": 8}


def test_panel_layout_overrides_can_force_stacked_bar():
    panels = [
        {
            "title": "Locks by state",
            "esql": {"type": "line", "query": "FROM metrics-*"},
            "position": {"x": 0, "y": 0},
            "size": {"w": 24, "h": 16},
        }
    ]
    overrides = [
        {
            "title_match": "Locks by state",
            "kibana_type_override": "bar",
            "xy_mode": "stacked",
        }
    ]

    _apply_panel_layout_overrides_recursively(panels, overrides)

    assert panels[0]["esql"]["type"] == "bar"
    assert panels[0]["esql"]["mode"] == "stacked"


def test_panel_layout_overrides_xy_mode_without_type_override():
    panels = [
        {
            "title": "Locks by state",
            "esql": {"type": "bar", "query": "FROM metrics-*"},
            "position": {"x": 0, "y": 0},
            "size": {"w": 24, "h": 16},
        }
    ]
    overrides = [
        {
            "title_match": "Locks by state",
            "xy_mode": "stacked",
        }
    ]

    _apply_panel_layout_overrides_recursively(panels, overrides)

    assert panels[0]["esql"]["type"] == "bar"
    assert panels[0]["esql"]["mode"] == "stacked"


# ---------------------------------------------------------------------------
# layout_overrides presentation contract (schema-valid or reported)
# ---------------------------------------------------------------------------

def _metric_probe_panel() -> dict:
    return {
        "title": "PostgreSQL Uptime",
        "esql": {
            "type": "metric",
            "query": "FROM metrics-* | STATS value = MAX(uptime)",
            "primary": {"field": "value", "label": "PostgreSQL Uptime"},
        },
        "position": {"x": 0, "y": 0},
        "size": {"w": 12, "h": 8},
    }


def _xy_probe_panel(chart_type: str = "line") -> dict:
    esql = {
        "type": chart_type,
        "query": (
            "FROM metrics-* | STATS value = SUM(locks) BY "
            "time_bucket = TBUCKET(1 hour), `labels.mode`"
        ),
        "dimension": {"field": "time_bucket"},
        "metrics": [{"field": "value"}],
        "breakdown": {"field": "labels.mode"},
    }
    if chart_type in {"bar", "area"}:
        esql["mode"] = "stacked"
    return {
        "title": "Locks by state",
        "esql": esql,
        "position": {"x": 0, "y": 0},
        "size": {"w": 24, "h": 16},
    }


def test_layout_override_presentation_keys_skip_non_xy_panel():
    """``xy_mode``/``legend_position`` on a metric tile would add ``mode``/
    ``legend`` keys that ``ESQLMetricPanelConfig`` (``additionalProperties:
    false``) rejects. Skip and report instead of emitting invalid JSON."""
    panels = [_metric_probe_panel()]
    warnings: list = []

    _apply_panel_layout_overrides_recursively(
        panels,
        [
            {
                "title_match": "PostgreSQL Uptime",
                "xy_mode": "stacked",
                "legend_position": "right",
            }
        ],
        warnings=warnings,
    )

    esql = panels[0]["esql"]
    assert esql["type"] == "metric"
    assert "mode" not in esql
    assert "legend" not in esql
    assert warnings, "a skipped presentation request must be reported"
    message = warnings[0][1]
    assert "PostgreSQL Uptime" in message
    assert "xy_mode" in message and "legend_position" in message
    assert "query_overrides" in message
    assert not dashboard_schema_errors(panels)


def test_layout_override_cannot_change_panel_shape():
    """A late ``type`` flip keeps the XY ``metrics``/``dimension`` columns and
    has no ``primary``, so ``metric`` output would fail the schema both ways."""
    panels = [_xy_probe_panel("line")]
    warnings: list = []

    _apply_panel_layout_overrides_recursively(
        panels,
        [{"title_match": "Locks by state", "kibana_type_override": "metric"}],
        warnings=warnings,
    )

    assert panels[0]["esql"]["type"] == "line"
    assert warnings
    assert "query_overrides" in warnings[0][1]
    assert not dashboard_schema_errors(panels)


def test_layout_override_xy_mode_on_line_panel_is_reported():
    """``ESQLLinePanelConfig`` has no ``mode``; dropping it keeps the panel
    valid, but the ignored stacking request must still be visible."""
    panels = [_xy_probe_panel("line")]
    warnings: list = []

    _apply_panel_layout_overrides_recursively(
        panels,
        [{"title_match": "Locks by state", "xy_mode": "stacked"}],
        warnings=warnings,
    )

    assert panels[0]["esql"]["type"] == "line"
    assert "mode" not in panels[0]["esql"]
    assert warnings
    assert "kibana_type_override" in warnings[0][1]
    assert not dashboard_schema_errors(panels)


def test_layout_override_presentation_output_is_schema_valid():
    """Every presentation override shape -- including the ones that used to
    emit invalid JSON -- validates against ``docs/dashboards/schema.json``."""
    cases = [
        (_metric_probe_panel(), {"xy_mode": "stacked"}),
        (_metric_probe_panel(), {"legend_position": "right"}),
        (_metric_probe_panel(), {"kibana_type_override": "gauge"}),
        (_xy_probe_panel("line"), {"kibana_type_override": "metric"}),
        (_xy_probe_panel("line"), {"kibana_type_override": "datatable"}),
        (_xy_probe_panel("line"), {"xy_mode": "percentage"}),
        (_xy_probe_panel("bar"), {"kibana_type_override": "line"}),
        (_xy_probe_panel("bar"), {"xy_mode": "percentage"}),
        # The real 12485 rule: composition-over-time line -> stacked bar with
        # the lock-mode legend moved out from under the plot.
        (
            _xy_probe_panel("line"),
            {
                "kibana_type_override": "bar",
                "xy_mode": "stacked",
                "legend_position": "right",
            },
        ),
    ]
    failures = []
    for panel, override in cases:
        panels = [panel]
        _apply_panel_layout_overrides_recursively(
            panels, [{"title_match": panel["title"], **override}]
        )
        errors = dashboard_schema_errors(panels)
        if errors:
            failures.append(f"{override} -> {errors}")
    assert not failures, "\n".join(failures)


def test_12485_locks_layout_override_still_emits_stacked_bar():
    """The pack's own line -> stacked bar + right legend must keep working."""
    dashboard = {"gnetId": 12485, "title": "PostgreSQL Exporter", "tags": []}
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    override = next(
        item
        for item in resolved.panel_layout_overrides
        if item["title_match"] == "Locks by state" and not item.get("section_match")
    )
    panels = [_xy_probe_panel("line")]

    _apply_panel_layout_overrides_recursively(panels, [override])

    esql = panels[0]["esql"]
    assert esql["type"] == "bar"
    assert esql["mode"] == "stacked"
    assert esql["legend"] == {"position": "right", "visible": "show"}
    assert not dashboard_schema_errors(panels)


def test_layout_override_rejects_non_xy_kibana_type_override():
    """A cross-shape request is a rule-pack error, not a silent bad emit."""
    with pytest.raises(ValueError) as excinfo:
        validate_rule_pack_payload(
            {
                "panel": {
                    "layout_overrides": [
                        {"title_match": "Uptime", "kibana_type_override": "metric"}
                    ]
                }
            },
            source="probe pack",
        )

    message = str(excinfo.value)
    assert "presentation-only" in message
    assert "query_overrides" in message


def test_layout_override_rejects_xy_mode_on_line_type():
    with pytest.raises(ValueError) as excinfo:
        validate_rule_pack_payload(
            {
                "panel": {
                    "layout_overrides": [
                        {
                            "title_match": "Locks by state",
                            "kibana_type_override": "line",
                            "xy_mode": "stacked",
                        }
                    ]
                }
            },
            source="probe pack",
        )

    assert "no stacking mode" in str(excinfo.value)


def test_layout_override_accepts_xy_family_type_and_stacking():
    payload = validate_rule_pack_payload(
        {
            "panel": {
                "layout_overrides": [
                    {
                        "title_match": "Locks by state",
                        "kibana_type_override": "bar",
                        "xy_mode": "stacked",
                        "legend_position": "right",
                    }
                ]
            }
        }
    )

    override = payload.panel.layout_overrides[0]
    assert override.kibana_type_override == "bar"
    assert override.xy_mode == "stacked"
    assert override.legend_position == "right"


def test_skipped_layout_presentation_override_is_reported_on_the_panel():
    """The skip is an operator-visible gap: the panel does not look the way the
    pack asked, so it must not be reported as a clean ``migrated``."""
    dashboard = {
        "title": "Layout Override Probe",
        "panels": [
            {
                "id": 1,
                "type": "singlestat",
                "title": "Total database size",
                "targets": [{"expr": "sum(pg_database_size_bytes)", "refId": "A"}],
                "gridPos": {"x": 0, "y": 0, "w": 4, "h": 3},
            }
        ],
    }
    rule_pack = RulePackConfig(
        panel_layout_overrides=[
            {"title_match": "Total database size", "xy_mode": "stacked"}
        ]
    )

    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
    )

    panel_result = next(
        item
        for item in result.panel_results
        if item.title == "Total database size"
    )
    assert panel_result.status == "migrated_with_warnings", panel_result.reasons
    assert any(
        "presentation change was skipped" in reason for reason in panel_result.reasons
    ), panel_result.reasons
    assert result.migrated_with_warnings >= 1
    assert not dashboard_schema_errors(
        result.dashboard_ir.to_yaml_dict().get("panels") or []
    )


def test_panel_layout_overrides_can_rename_section_title():
    panels = [
        {
            "title": "Section 1",
            "section": {
                "collapsed": False,
                "panels": [
                    {
                        "title": "MySQL Uptime",
                        "position": {"x": 0, "y": 0},
                        "size": {"w": 6, "h": 6},
                    }
                ],
            },
        }
    ]
    overrides = [{"title_match": "Section 1", "title": "Overview"}]

    _apply_panel_layout_overrides_recursively(panels, overrides)

    assert panels[0]["title"] == "Overview"


def test_panel_layout_overrides_section_match_uses_source_title_after_rename():
    """section_match is the Grafana row title, not the post-override Kibana title."""
    panels = [
        {
            "title": "Section 1",
            "section": {
                "collapsed": False,
                "panels": [
                    {
                        "title": "MySQL Uptime",
                        "position": {"x": 0, "y": 0},
                        "size": {"w": 6, "h": 6},
                    }
                ],
            },
        }
    ]
    overrides = [
        {"title_match": "Section 1", "title": "Overview"},
        {
            "title_match": "MySQL Uptime",
            "section_match": "Section 1",
            "position": {"x": 8, "y": 2},
            "size": {"w": 12, "h": 8},
        },
    ]

    _apply_panel_layout_overrides_recursively(panels, overrides)

    assert panels[0]["title"] == "Overview"
    child = panels[0]["section"]["panels"][0]
    assert child["position"] == {"x": 8, "y": 2}
    assert child["size"] == {"w": 12, "h": 8}


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


def test_optional_or_fallback_keeps_surviving_operand():
    """``foo or optional_b`` where ``foo`` is present and ``optional_b`` is a
    live-optional absent metric must render from the surviving ``foo`` operand
    rather than discarding the translated target and degrading the whole panel
    to missing-telemetry markdown (PR #369 follow-up, giorgi-imerlishvili-elastic).
    """
    rule_pack = RulePackConfig(live_optional_metrics=["optional_b"])
    resolver = SchemaResolver(rule_pack)
    resolver._field_cache = {
        "foo": {"double": {"type": "double"}},
        "instance": {"keyword": {"type": "keyword"}},
    }
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "timeseries",
        "title": "OR Fallback",
        "targets": [{"expr": "foo or optional_b", "refId": "A"}],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)

    assert "markdown" not in (yaml_panel or {}), result.reasons
    query = (yaml_panel or {}).get("esql", {}).get("query", "")
    assert "foo" in query
    assert "optional_b" not in query


def test_ignored_only_metricless_selector_does_not_emit_empty_native_query():
    """``{release="$release"}`` with ``release`` ignored strips to an empty
    metricless selector. That must NOT migrate as a green native
    ``value=({})`` (invalid PromQL); the gap must be surfaced instead
    (PR #369 follow-up, giorgi-imerlishvili-elastic)."""
    rule_pack = RulePackConfig(ignored_labels=["release"])
    resolver = SchemaResolver(rule_pack)
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = {"labels.release": {"keyword": {"type": "keyword"}}}

    panel = {
        "type": "stat",
        "title": "Release",
        "targets": [{"expr": '{release="$release"}', "refId": "A"}],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)

    serialized = json.dumps(yaml_panel or {})
    assert "value=({})" not in serialized
    assert "{}" not in serialized
    assert result.status != "migrated"


def test_all_optional_absent_targets_become_missing_telemetry_markdown():
    """A panel whose only target is live_optional and field-caps-absent must
    degrade to missing-telemetry markdown, not IndexError on translations[0].
    """
    rule_pack = RulePackConfig(live_optional_metrics=["optional_metric"])
    resolver = SchemaResolver(rule_pack)
    resolver._field_cache = {"labels.instance": {"keyword": {"type": "keyword"}}}
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"

    panel = {
        "type": "stat",
        "title": "Optional Only",
        "targets": [{"expr": "sum(optional_metric)", "refId": "A"}],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)

    assert "markdown" in (yaml_panel or {})
    assert "optional_metric" in yaml_panel["markdown"]["content"]
    assert result.status == "migrated_with_warnings"


def test_partial_control_schema_does_not_prove_unlisted_metrics_absent():
    """``merge_control_schema`` is a label-hint fixture, not exhaustive caps.

    Metrics that simply are not listed must still translate (native PROMQL or
    ES|QL), matching ``--control-schema`` without ``--es-url``.
    """
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    resolver.merge_control_schema(
        {
            "field_cache": {
                "cluster": {
                    "keyword": {"type": "keyword", "aggregatable": True, "searchable": True}
                }
            }
        }
    )
    assert resolver.discovery_status()["status"] == "partial"

    panel = {
        "type": "stat",
        "title": "CPU",
        "targets": [{"expr": "sum(node_cpu_seconds_total)", "refId": "A"}],
    }

    yaml_panel, result = translate_panel(panel, rule_pack=rule_pack, resolver=resolver)

    assert "markdown" not in (yaml_panel or {})
    assert not any("Telemetry missing" in str(reason) for reason in (result.reasons or []))
    emitted = yaml_panel or {}
    assert emitted.get("esql") or emitted.get("promql") or "query" in str(emitted)


def test_partial_control_schema_keeps_or_chain_operands():
    """Partial control schemas must not prune unlisted OR fallback metrics."""
    resolver = SchemaResolver(RulePackConfig())
    resolver.merge_control_schema(
        {
            "field_cache": {
                "cluster": {
                    "keyword": {"type": "keyword", "aggregatable": True, "searchable": True}
                }
            }
        }
    )
    assert resolver.discovery_status()["status"] == "partial"

    frag = _parse_fragment(
        "node_network_receive_bytes_total or rdsosmetrics_network_rx_bytes"
    )
    kept, dropped = _reduce_or_operands(frag, resolver)

    assert [operand.metric for operand in kept] == [
        "node_network_receive_bytes_total",
        "rdsosmetrics_network_rx_bytes",
    ]
    assert dropped == []


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
    # Canonical authoring: the grouping label resolves per profile; under the
    # default offline profile it emits the bare `instance` spelling (native
    # byte-identity to labels.instance is guarded by the cross-profile gate).
    assert "| KEEP time_bucket, `instance`, hits, misses" in query


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
    assert "| KEEP time_bucket, `cmd`, computed_value" in query
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
    assert "| KEEP time_bucket, `cmd`, redis_commands_duration_seconds_total" in query
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


def test_schema_validates_pack_with_title_layout_override():
    from observability_migration.adapters.source.grafana.extension_schema import validate_rule_pack_payload

    raw = {
        "panel": {
            "layout_overrides": [
                {
                    "title_match": "Section 1",
                    "title": "Overview",
                }
            ]
        }
    }
    payload = validate_rule_pack_payload(raw)
    assert payload.panel.layout_overrides[0].title == "Overview"


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


# ---------------------------------------------------------------------------
# Grafana 315 — Kubernetes cluster monitoring (cAdvisor)
# ---------------------------------------------------------------------------


def _resolve_315():
    dashboard = {
        "gnetId": 315,
        "title": "Kubernetes cluster monitoring (via Prometheus)",
        "tags": ["kubernetes"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    return resolved, SchemaResolver(resolved)


def test_315_registry_entry_present():
    entry = find_curated_pack(gnet_id=315, title="", tags=[])
    assert entry is not None
    assert entry["name"] == "grafana_315_kubernetes_cadvisor"
    assert entry["gnet_revision"] == 3


def test_315_classifies_cadvisor_counters_and_gauges():
    resolved, _ = _resolve_315()
    assert resolved.metric_kinds["container_cpu_usage_seconds_total"] == "counter"
    assert resolved.metric_kinds["container_network_receive_bytes_total"] == "counter"
    assert resolved.metric_kinds["container_memory_working_set_bytes"] == "gauge"
    assert resolved.metric_kinds["machine_cpu_cores"] == "gauge"


def test_315_rewrites_pre_116_labels_and_ignores_dead_matchers():
    resolved, _ = _resolve_315()
    # label_rewrites are source→canonical; the resolver namespaces per profile.
    assert resolved.label_rewrites["pod_name"] == "pod"
    assert resolved.label_rewrites["container_name"] == "container"
    assert "id" not in resolved.label_rewrites  # identity rewrite dropped
    # Passthrough restores the source-faithful pre-1.16 spellings.
    assert resolved.source_label_names["pod"] == "pod_name"
    assert resolved.source_label_names["container"] == "container_name"
    assert "kubernetes_io_hostname" in resolved.ignored_labels
    assert "image" in resolved.ignored_labels
    # systemd/rkt labels must NOT be ignored (those panels degrade to empty).
    assert "systemd_service_name" not in resolved.ignored_labels
    assert "rkt_container_name" not in resolved.ignored_labels


def test_315_pods_cpu_panel_groups_by_pod_without_dead_matchers():
    resolved, resolver = _resolve_315()
    panel = {
        "id": 1,
        "type": "graph",
        "title": "Pods CPU usage (1m avg)",
        "targets": [
            {
                "expr": (
                    'sum (rate (container_cpu_usage_seconds_total'
                    '{image!="",name=~"^k8s_.*",kubernetes_io_hostname=~"^$Node$"}[1m]))'
                    " by (pod_name)"
                ),
                "legendFormat": "{{ pod_name }}",
                "refId": "A",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert "k8s.pod.name" in query
    assert "RATE(container_cpu_usage_seconds_total)" in query
    # Dead selector labels must be stripped, not filtered on.
    assert "kubernetes_io_hostname" not in query
    assert 'name RLIKE' not in query
    assert "image" not in query


def test_315_containers_override_keeps_k8s_series_and_discloses_drop():
    resolved, resolver = _resolve_315()
    panel = {
        "id": 2,
        "type": "graph",
        "title": "Containers CPU usage (1m avg)",
        "targets": [
            {
                "expr": (
                    'sum (rate (container_cpu_usage_seconds_total'
                    '{image!="",name=~"^k8s_.*",container_name!="POD"}[1m]))'
                    " by (container_name, pod_name)"
                ),
                "legendFormat": "pod: {{ pod_name }} | {{ container_name }}",
                "refId": "A",
            },
            {
                "expr": (
                    'sum (rate (container_cpu_usage_seconds_total'
                    '{image!="",name!~"^k8s_.*"}[1m]))'
                    " by (kubernetes_io_hostname, name, image)"
                ),
                "legendFormat": "docker: {{ name }}",
                "refId": "B",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status == "migrated_with_warnings"
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert "k8s.pod.name" in query and "k8s.container.name" in query
    assert 'k8s.container.name != "POD"' in query
    # The dropped-runtime disclosure must surface as a warning.
    assert any("docker" in r or "rkt" in r for r in result.reasons)


def test_315_system_services_panel_degrades_to_empty_not_aggregate():
    resolved, resolver = _resolve_315()
    panel = {
        "id": 3,
        "type": "graph",
        "title": "System services CPU usage (1m avg)",
        "targets": [
            {
                "expr": (
                    'sum (rate (container_cpu_usage_seconds_total'
                    '{systemd_service_name!="",kubernetes_io_hostname=~"^$Node$"}[1m]))'
                    " by (systemd_service_name)"
                ),
                "legendFormat": "{{ systemd_service_name }}",
                "refId": "A",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    # Curated ES|QL override: systemd_service_name never exists on modern
    # cAdvisor, so grouping/breaking down by it would fail in Lens ("invalid
    # column"). The override filters on an impossible container value so the
    # panel degrades to an honest empty (data_gap) with valid columns — it must
    # NOT reference the non-existent systemd_service_name column, and must NOT
    # collapse into a single misleading aggregate (a BY grouping is retained).
    assert "systemd_service_name" not in query
    assert '"__systemd_service__"' in query
    assert "k8s.container.name" in query
    assert "RATE(container_cpu_usage_seconds_total)" in query


def test_315_pods_memory_override_groups_by_pod_last_over_time():
    resolved, resolver = _resolve_315()
    panel = {
        "id": 4,
        "type": "graph",
        "title": "Pods memory usage",
        "targets": [
            {
                "expr": (
                    "sum (container_memory_working_set_bytes"
                    '{image!="",name=~"^k8s_.*",kubernetes_io_hostname=~"^$Node$"})'
                    " by (pod_name)"
                ),
                "legendFormat": "{{ pod_name }}",
                "refId": "A",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    # Gauge → LAST_OVER_TIME (no illegal SUM(MAX(...)) nested aggregate), grouped
    # by pod with the breakdown accessor aligned to the ES|QL output column.
    assert "LAST_OVER_TIME(container_memory_working_set_bytes)" in query
    assert "k8s.pod.name" in query
    # Root cgroup (id=/) has no pod label; the override excludes it.
    assert "k8s.pod.name IS NOT NULL" in query


def test_315_pods_panels_breakdown_accessor_matches_query_column():
    """Curated ES|QL keeps the Lens breakdown on the resolved pod field.

    The native PROMQL DSL path leaves the breakdown accessor bound to the
    pre-rewrite ``pod_name`` (Lens "invalid column" after the pod_name -> pod
    rewrite). The override must emit the resolved pod field (here the otel
    ``k8s.pod.name``) as an actual query output column and never reference the
    bare ``pod_name``.
    """
    resolved, resolver = _resolve_315()
    for title, expr in (
        (
            "Pods CPU usage (1m avg)",
            "sum (rate (container_cpu_usage_seconds_total[1m])) by (pod_name)",
        ),
        (
            "Pods memory usage",
            "sum (container_memory_working_set_bytes) by (pod_name)",
        ),
    ):
        panel = {
            "id": 9,
            "type": "graph",
            "title": title,
            "targets": [
                {"expr": expr, "legendFormat": "{{ pod_name }}", "refId": "A"}
            ],
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
        }
        yaml_panel, result = translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=resolved,
            resolver=resolver,
        )
        assert result.status in {"migrated", "migrated_with_warnings"}
        esql = yaml_panel.get("esql") or {}
        query = esql.get("query") or ""
        assert "`k8s.pod.name`" in query, f"{title}: {query}"
        # No bare pre-rewrite label token as a standalone identifier.
        assert "pod_name" not in query, f"{title} leaked pod_name: {query}"


def test_315_pods_network_override_names_received_and_sent():
    resolved, resolver = _resolve_315()
    panel = {
        "id": 5,
        "type": "graph",
        "title": "Pods network I/O (1m avg)",
        "targets": [
            {
                "expr": (
                    "sum (rate (container_network_receive_bytes_total"
                    '{image!="",name=~"^k8s_.*"}[1m])) by (pod_name)'
                ),
                "legendFormat": "-> {{ pod_name }}",
                "refId": "A",
            },
            {
                "expr": (
                    "- sum (rate (container_network_transmit_bytes_total"
                    '{image!="",name=~"^k8s_.*"}[1m])) by (pod_name)'
                ),
                "legendFormat": "<- {{ pod_name }}",
                "refId": "B",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert "Received =" in query
    assert "Sent =" in query
    assert "value_B" not in query
    assert "k8s.pod.name" in query


def test_315_all_processes_override_groups_by_cgroup_id():
    resolved, resolver = _resolve_315()
    panel = {
        "id": 6,
        "type": "graph",
        "title": "All processes CPU usage (1m avg)",
        "targets": [
            {
                "expr": (
                    "sum (rate (container_cpu_usage_seconds_total"
                    '{id!="/",kubernetes_io_hostname=~"^$Node$"}[1m])) by (id)'
                ),
                "legendFormat": "{{ id }}",
                "refId": "A",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    # cgroup `id` has no otel candidate, so it resolves to the bare `id`.
    assert "`id`" in query
    assert 'id != "/"' in query
    assert "id IS NOT NULL" in query


# ---------------------------------------------------------------------------
# Grafana 6417 — Kubernetes Cluster (kube-state-metrics)
# ---------------------------------------------------------------------------


def _resolve_6417():
    dashboard = {
        "gnetId": 6417,
        "title": "Kubernetes Cluster (Prometheus)",
        "tags": ["kubernetes", "kubernetes-app"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    return resolved, SchemaResolver(resolved)


def _translate_6417(panel):
    resolved, resolver = _resolve_6417()
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    return result, query


def test_6417_registry_entry_present():
    entry = find_curated_pack(gnet_id=6417, title="", tags=[])
    assert entry is not None
    assert entry["name"] == "grafana_6417_kubernetes_ksm"
    assert entry["gnet_revision"] == 1


def test_6417_maps_old_node_exporter_and_restarts_names():
    resolved, _ = _resolve_6417()

    def _target(name):
        entry = resolved.metric_map[name]
        return getattr(entry, "target", str(entry))

    assert _target("node_filesystem_size").endswith("node_filesystem_size_bytes")
    assert _target("node_filesystem_free").endswith("node_filesystem_free_bytes")
    assert _target("kube_pod_container_status_restarts").endswith(
        "kube_pod_container_status_restarts_total"
    )
    assert resolved.metric_kinds["kube_pod_container_status_restarts_total"] == "counter"
    assert resolved.metric_kinds["node_filesystem_size"] == "gauge"


def test_6417_cluster_cpu_usage_reshapes_resource_split():
    panel = {
        "id": 1,
        "type": "singlestat",
        "title": "Cluster CPU Usage",
        "format": "percentunit",
        "targets": [
            {
                "expr": (
                    'sum(kube_pod_container_resource_requests_cpu_cores{node=~"$node"})'
                    ' / sum(kube_node_status_allocatable_cpu_cores{node=~"$node"})'
                ),
                "refId": "A",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 6, "h": 4},
    }
    result, query = _translate_6417(panel)
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    # The old per-resource metric name is reshaped to the resource= label form.
    assert "kube_node_status_allocatable" in query
    assert 'resource == "cpu"' in query
    assert "kube_pod_container_resource_requests" in query


def test_6417_restarts_delta_uses_counter_total():
    panel = {
        "id": 2,
        "type": "singlestat",
        "title": "Containers Restarts (Last 30 Minutes)",
        "targets": [
            {
                "expr": 'sum(delta(kube_pod_container_status_restarts{namespace="kube-system"}[30m]))',
                "refId": "A",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 6, "h": 3},
    }
    result, query = _translate_6417(panel)
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "kube_pod_container_status_restarts_total" in query
    assert "DELTA" in query


def test_6417_out_of_disk_degrades_to_empty_gap():
    panel = {
        "id": 3,
        "type": "singlestat",
        "title": "Nodes Out of Disk",
        "targets": [
            {
                "expr": (
                    'sum(kube_node_status_condition'
                    '{condition="OutOfDisk", node=~"$node", status="true"})'
                ),
                "refId": "A",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 8, "h": 3},
    }
    result, query = _translate_6417(panel)
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    # The removed OutOfDisk condition is kept as a real (empty) filter, not faked.
    assert 'OutOfDisk' in query


def test_6417_deployment_table_groups_by_deployment():
    panel = {
        "id": 4,
        "type": "table",
        "title": "Deployment Replicas - Up To Date",
        "targets": [
            {
                "expr": 'kube_deployment_status_replicas{namespace=~"$namespace"}',
                "legendFormat": "{{ deployment }}",
                "refId": "A",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 6, "h": 5},
    }
    result, query = _translate_6417(panel)
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "k8s.deployment.name" in query
    assert "MV_CONTAINS(TO_STRING(?namespace)" in query


def test_6417_cluster_cpu_usage_binds_node_control():
    panel = {
        "id": 1,
        "type": "singlestat",
        "title": "Cluster CPU Usage",
        "format": "percentunit",
        "targets": [
            {
                "expr": (
                    'sum(kube_pod_container_resource_requests_cpu_cores{node=~"$node"})'
                    ' / sum(kube_node_status_allocatable_cpu_cores{node=~"$node"})'
                ),
                "refId": "A",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 6, "h": 4},
    }
    result, query = _translate_6417(panel)
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "MV_CONTAINS(TO_STRING(?node)" in query
    assert "k8s.node.name" in query


def test_6417_cpu_requested_reshapes_resource_split():
    panel = {
        "id": 5,
        "type": "singlestat",
        "title": "CPU Cores Requested by Containers",
        "targets": [
            {
                "expr": (
                    'sum(kube_pod_container_resource_requests_cpu_cores'
                    '{namespace=~"$namespace", node=~"$node"})'
                ),
                "refId": "A",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 6, "h": 3},
    }
    result, query = _translate_6417(panel)
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "kube_pod_container_resource_requests" in query
    assert 'resource == "cpu"' in query
    assert "kube_pod_container_resource_requests_cpu_cores" not in query


def test_6417_constant_vars_become_multi_select_label_values_controls():
    """Constant `.*` $node/$namespace must not hydrate to the first concrete value."""
    dashboard = {
        "gnetId": 6417,
        "title": "Kubernetes Cluster (Prometheus)",
        "tags": ["kubernetes", "kubernetes-app"],
        "templating": {
            "list": [
                {
                    "name": "node",
                    "type": "constant",
                    "query": ".*",
                    "current": {"text": ".*", "value": ".*"},
                },
                {
                    "name": "namespace",
                    "type": "constant",
                    "query": ".*",
                    "current": {"text": ".*", "value": ".*"},
                },
            ]
        },
        "panels": [
            {
                "id": 1,
                "type": "singlestat",
                "title": "Number Of Nodes",
                "targets": [
                    {"expr": 'sum(kube_node_info{node=~"$node"})', "refId": "A"}
                ],
                "gridPos": {"x": 0, "y": 0, "w": 8, "h": 3},
            }
        ],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
    )
    payload = result.dashboard_ir.to_yaml_dict()
    controls = payload.get("controls") or []
    by_name = {c.get("variable_name"): c for c in controls}
    assert "node" in by_name and "namespace" in by_name, controls
    node = by_name["node"]
    namespace = by_name["namespace"]
    assert node.get("multiple") is True
    assert namespace.get("multiple") is True
    node_query = str(node.get("query") or "")
    namespace_query = str(namespace.get("query") or "")
    assert "kube_node_info" in node_query
    assert "kube_pod_info" in namespace_query
    assert "node" in node_query
    assert "namespace" in namespace_query


# ---------------------------------------------------------------------------
# Grafana 741 — Kubernetes Deployment metrics
# ---------------------------------------------------------------------------


def _resolve_741():
    dashboard = {
        "gnetId": 741,
        "title": "Kubernetes Deployment metrics",
        "tags": ["kubernetes"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    return resolved, SchemaResolver(resolved)


def _translate_741(panel, *, section_title=""):
    resolved, resolver = _resolve_741()
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
        section_title=section_title,
    )
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    return result, query, yaml_panel


def test_741_registry_entry_present():
    entry = find_curated_pack(gnet_id=741, title="", tags=[])
    assert entry is not None
    assert entry["name"] == "grafana_741_kubernetes_deployment_metrics"
    assert entry["gnet_revision"] == 1


def test_741_rewrites_heapster_labels_and_keeps_node_instance():
    resolved, _ = _resolve_741()
    # label_rewrites are source→canonical; the resolver namespaces per profile.
    assert resolved.label_rewrites["pod_name"] == "pod"
    assert resolved.label_rewrites["io_kubernetes_pod_name"] == "pod"
    assert resolved.label_rewrites["io_kubernetes_container_name"] == "container"
    assert resolved.label_rewrites["kubernetes_io_hostname"] == "instance"
    # Passthrough restores the source-faithful Heapster spellings.
    assert resolved.source_label_names["pod"] == "pod_name"
    assert resolved.source_label_names["container"] == "container_name"
    assert resolved.source_label_names["instance"] == "kubernetes_io_hostname"
    assert "image" in resolved.ignored_labels
    assert "name" in resolved.ignored_labels
    assert "kubernetes_io_hostname" not in resolved.ignored_labels
    assert resolved.metric_kinds["kube_deployment_status_replicas_available"] == "gauge"


def test_741_deployment_cpu_graph_prefix_binds_and_groups_by_pod():
    panel = {
        "id": 17,
        "type": "graph",
        "title": "Deployment CPU usage",
        "targets": [
            {
                "expr": (
                    'sum (rate (container_cpu_usage_seconds_total'
                    '{image!="",name=~"^k8s_.*",pod_name=~"^$Deployment.*$"}[1m]))'
                    " by (io_kubernetes_pod_name)"
                ),
                "legendFormat": "{{ io_kubernetes_pod_name }}",
                "refId": "A",
            }
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    result, query, _ = _translate_741(panel, section_title="Deployment CPU usage")
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "k8s.pod.name" in query
    assert "STARTS_WITH" in query and "?Deployment" in query
    assert "pod_name" not in query
    assert "io_kubernetes" not in query
    assert "RATE(container_cpu_usage_seconds_total)" in query


def test_741_kpi_ratio_uses_deployment_prefix_and_node_instance():
    panel = {
        "id": 4,
        "type": "singlestat",
        "title": "Deployment memory usage",
        "format": "percent",
        "targets": [
            {
                "expr": (
                    'sum (container_memory_working_set_bytes{pod_name=~"^$Deployment.*$"})'
                    ' / sum (machine_memory_bytes{kubernetes_io_hostname=~"^$Node$"}) * 100'
                ),
                "refId": "A",
            }
        ],
        "gridPos": {"x": 0, "y": 0, "w": 4, "h": 4},
    }
    result, query, _ = _translate_741(panel, section_title="Total usage")
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "machine_memory_bytes" in query
    assert "STARTS_WITH" in query and "?Deployment" in query
    assert "?Node" in query
    assert "service.instance.id" in query


def test_741_used_memory_panel_id_disambiguates_duplicate_title():
    panel = {
        "id": 38,
        "type": "singlestat",
        "title": "Used",
        "format": "bytes",
        "targets": [
            {
                "expr": 'sum (container_memory_working_set_bytes{pod_name=~"^$Deployment.*$"})',
                "refId": "A",
            }
        ],
        "gridPos": {"x": 0, "y": 0, "w": 2, "h": 3},
    }
    result, query, _ = _translate_741(panel, section_title="Total usage")
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "STARTS_WITH" in query and "?Deployment" in query
    assert "container_memory_working_set_bytes" in query
    assert "machine_cpu_cores" not in query


def test_741_cpu_used_panel_id_gets_rate_not_memory():
    panel = {
        "id": 40,
        "type": "singlestat",
        "title": "Used",
        "targets": [
            {
                "expr": (
                    'sum (rate (container_cpu_usage_seconds_total'
                    '{pod_name=~"^$Deployment.*$"}[1m]))'
                ),
                "refId": "A",
            }
        ],
        "gridPos": {"x": 4, "y": 4, "w": 2, "h": 3},
    }
    result, query, _ = _translate_741(panel, section_title="Total usage")
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "RATE(container_cpu_usage_seconds_total)" in query
    assert "container_memory_working_set_bytes" not in query


def test_741_total_panel_ids_do_not_cross_wire():
    memory_total = {
        "id": 39,
        "type": "singlestat",
        "title": "Total",
        "format": "bytes",
        "targets": [
            {
                "expr": 'sum (container_memory_working_set_bytes{kubernetes_io_hostname=~"^$Node.*$"})',
                "refId": "A",
            }
        ],
        "gridPos": {"x": 2, "y": 4, "w": 2, "h": 3},
    }
    replicas_total = {
        "id": 43,
        "type": "singlestat",
        "title": "Total",
        "targets": [
            {
                "expr": 'sum(kube_deployment_status_replicas{deployment=~"^$Deployment$"})',
                "refId": "A",
            }
        ],
        "gridPos": {"x": 10, "y": 4, "w": 2, "h": 3},
    }
    _, mem_query, _ = _translate_741(memory_total, section_title="Total usage")
    _, rep_query, _ = _translate_741(replicas_total, section_title="Total usage")
    assert "container_memory_working_set_bytes" in mem_query
    assert "kube_deployment_status_replicas" not in mem_query
    assert "?Node" in mem_query
    assert "kube_deployment_status_replicas" in rep_query
    assert "container_memory_working_set_bytes" not in rep_query


def test_741_network_names_received_and_sent():
    panel = {
        "id": 16,
        "type": "graph",
        "title": "Deployment network I/O",
        "targets": [
            {
                "expr": (
                    'sum (rate (container_network_receive_bytes_total'
                    '{pod_name=~"^$Deployment.*$"}[1m])) by (io_kubernetes_pod_name)'
                ),
                "legendFormat": "-> {{ io_kubernetes_pod_name }}",
                "refId": "A",
            },
            {
                "expr": (
                    '- sum (rate (container_network_transmit_bytes_total'
                    '{pod_name=~"^$Deployment.*$"}[1m])) by (io_kubernetes_pod_name)'
                ),
                "legendFormat": "<- {{ io_kubernetes_pod_name }}",
                "refId": "B",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    result, query, _ = _translate_741(panel)
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "Received =" in query
    assert "Sent =" in query
    assert "value_B" not in query


def test_741_containers_override_keeps_k8s_series_and_discloses_drop():
    panel = {
        "id": 24,
        "type": "graph",
        "title": "Containers CPU usage",
        "targets": [
            {
                "expr": (
                    'sum (rate (container_cpu_usage_seconds_total'
                    '{name=~"^k8s_.*",io_kubernetes_container_name!="POD",'
                    'pod_name=~"^$Deployment.*$"}[1m]))'
                    " by (io_kubernetes_container_name, io_kubernetes_pod_name)"
                ),
                "refId": "A",
            },
            {
                "expr": (
                    'sum (rate (container_cpu_usage_seconds_total'
                    '{name!~"^k8s_.*",pod_name=~"^$Deployment.*$"}[1m])) by (name)'
                ),
                "refId": "B",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    result, query, _ = _translate_741(panel)
    assert result.status == "migrated_with_warnings"
    assert "k8s.pod.name" in query and "k8s.container.name" in query
    assert 'k8s.container.name != "POD"' in query
    assert any("docker" in r or "rkt" in r for r in result.reasons)


def test_741_plugin_rewrites_deployment_and_node_populate():
    dashboard = {
        "gnetId": 741,
        "title": "Kubernetes Deployment metrics",
        "tags": ["kubernetes"],
        "templating": {
            "list": [
                {
                    "name": "Deployment",
                    "type": "query",
                    "query": "label_values(deployment)",
                    "includeAll": True,
                    "allValue": ".*",
                },
                {
                    "name": "Node",
                    "type": "query",
                    "query": "label_values(kubernetes_io_hostname)",
                    "includeAll": True,
                    "allValue": ".*",
                },
            ]
        },
        "rows": [
            {
                "title": "Total usage",
                "panels": [
                    {
                        "id": 37,
                        "type": "singlestat",
                        "title": "Replicas",
                        "targets": [
                            {
                                "expr": (
                                    'sum(kube_deployment_status_replicas_available'
                                    '{deployment=~"^$Deployment$"})'
                                ),
                                "refId": "A",
                            }
                        ],
                        "span": 4,
                    }
                ],
            }
        ],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
    )
    payload = result.dashboard_ir.to_yaml_dict()
    controls = payload.get("controls") or []
    by_name = {c.get("variable_name"): c for c in controls}
    assert "Deployment" in by_name, controls
    assert "Node" in by_name, controls
    assert "kube_deployment_status_replicas" in str(by_name["Deployment"].get("query") or "")
    assert "machine_cpu_cores" in str(by_name["Node"].get("query") or "")
    assert "kubernetes_io_hostname" not in str(by_name["Node"].get("query") or "")


def test_741_layout_renames_duplicate_used_total_by_panel_id():
    resolved, _ = _resolve_741()
    panels = [
        {
            "title": "Used",
            "_source_panel_id": "38",
            "esql": {"type": "metric", "query": "FROM metrics-*", "primary": {"label": "Used"}},
            "position": {"x": 0, "y": 0},
            "size": {"w": 8, "h": 8},
        },
        {
            "title": "Used",
            "_source_panel_id": "40",
            "esql": {"type": "metric", "query": "FROM metrics-*", "primary": {"label": "Used"}},
            "position": {"x": 8, "y": 0},
            "size": {"w": 8, "h": 8},
        },
    ]
    _apply_panel_layout_overrides_recursively(
        panels, resolved.panel_layout_overrides, section_title="Total usage"
    )
    assert panels[0]["title"] == "Memory used"
    assert panels[1]["title"] == "CPU used"
    assert panels[0]["position"] == {"x": 0, "y": 12}
    assert panels[1]["position"] == {"x": 16, "y": 12}
    assert panels[0]["size"]["w"] == 8
    assert panels[1]["size"]["w"] == 8
    assert panels[0]["esql"]["primary"]["label"] == " "


def test_layout_rename_clears_inner_metric_label_before_title_change():
    panels = [
        {
            "title": "Used",
            "esql": {
                "type": "metric",
                "primary": {"field": "computed_value", "label": "Used"},
            },
            "position": {"x": 0, "y": 0},
            "size": {"w": 8, "h": 8},
        }
    ]
    _apply_panel_layout_overrides_recursively(
        panels,
        [{"title_match": "Used", "title": "Memory used", "hide_title": False}],
    )
    assert panels[0]["title"] == "Memory used"
    assert panels[0]["esql"]["primary"]["label"] == " "


def test_741_kpi_row_fills_48_cols_without_overlap():
    resolved, _ = _resolve_741()
    by_key = {}
    for item in resolved.panel_layout_overrides:
        key = (item["title_match"], str(item.get("panel_id") or ""), item.get("section_match") or "")
        by_key[key] = item
    assert by_key[("Deployment memory usage", "4", "Total usage")]["position"] == {"x": 0, "y": 0}
    assert by_key[("Deployment CPU usage", "6", "Total usage")]["position"] == {"x": 16, "y": 0}
    assert by_key[("Replicas", "37", "")]["position"] == {"x": 32, "y": 0}
    assert by_key[("Used", "38", "")]["position"] == {"x": 0, "y": 12}
    assert by_key[("Total", "39", "")]["position"] == {"x": 8, "y": 12}
    assert by_key[("Used", "40", "")]["position"] == {"x": 16, "y": 12}
    assert by_key[("Total", "41", "")]["position"] == {"x": 24, "y": 12}
    assert by_key[("Available", "42", "")]["position"] == {"x": 32, "y": 12}
    assert by_key[("Total", "43", "")]["position"] == {"x": 40, "y": 12}
    assert by_key[("Deployment CPU usage", "17", "")]["position"]["y"] == 24
    assert by_key[("Containers CPU usage", "24", "")]["position"]["y"] == 36


# ---------------------------------------------------------------------------
# Grafana 8171 — Kubernetes Nodes
# ---------------------------------------------------------------------------


def _resolve_8171():
    dashboard = {
        "gnetId": 8171,
        "title": "Kubernetes Nodes",
        "tags": ["nodes", "prometheus"],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    return resolved, SchemaResolver(resolved)


def _translate_8171(panel):
    resolved, resolver = _resolve_8171()
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    return result, query, yaml_panel


def test_8171_registry_entry_present():
    entry = find_curated_pack(gnet_id=8171, title="", tags=[])
    assert entry is not None
    assert entry["name"] == "grafana_8171_kubernetes_nodes"
    assert entry["gnet_revision"] == 1


def test_8171_maps_nfsd_disk_to_node_disk():
    resolved, _ = _resolve_8171()

    def _target(name):
        entry = resolved.metric_map[name]
        return getattr(entry, "target", str(entry))

    assert _target("node_nfsd_disk_bytes_read_total").endswith("node_disk_read_bytes_total")
    assert _target("node_nfsd_disk_bytes_written_total").endswith(
        "node_disk_written_bytes_total"
    )
    assert resolved.metric_kinds["node_cpu_seconds_total"] == "counter"
    assert resolved.metric_kinds["node_load1"] == "gauge"


def test_8171_idle_cpu_override_is_busy_by_cpu():
    panel = {
        "id": 3,
        "type": "graph",
        "title": "Idle CPU",
        "targets": [
            {
                "expr": (
                    '100 - (avg by (cpu) (irate(node_cpu_seconds_total'
                    '{mode="idle", instance="$server"}[5m])) * 100)'
                ),
                "legendFormat": "{{cpu}}",
                "refId": "A",
            }
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    result, query, _ = _translate_8171(panel)
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "Busy =" in query or "Busy =" in query.replace(" ", " ")
    # cpu / mode have no otel candidate, so they resolve to the bare name.
    assert "`cpu`" in query
    assert 'mode == "idle"' in query or "{{label:mode}}" in query
    assert "?server" in query


def test_8171_disk_io_names_read_written_and_uses_node_disk():
    panel = {
        "id": 6,
        "type": "graph",
        "title": "Disk I/O",
        "targets": [
            {
                "expr": 'sum by (instance) (rate(node_nfsd_disk_bytes_read_total{instance="$server"}[2m]))',
                "legendFormat": "read",
                "refId": "A",
            },
            {
                "expr": 'sum by (instance) (rate(node_nfsd_disk_bytes_written_total{instance="$server"}[2m]))',
                "legendFormat": "written",
                "refId": "B",
            },
            {
                "expr": 'sum by (instance) (rate(node_disk_io_time_seconds_total{instance="$server"}[2m]))',
                "legendFormat": "io time",
                "refId": "C",
            },
        ],
        "gridPos": {"x": 0, "y": 0, "w": 18, "h": 7},
    }
    result, query, _ = _translate_8171(panel)
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert "node_disk_read_bytes_total" in query
    assert "node_disk_written_bytes_total" in query
    assert "nfsd" not in query
    assert "Read =" in query
    assert "Written =" in query


def test_8171_network_uses_esql_by_device_not_native_promql_grok():
    """Native PROMQL + GROK _timeseries + KEEP step returns rows but Lens XY
    paints 'No results found'. Pin ES|QL time_bucket + labels.device like CPU Busy.
    """
    receive = {
        "id": 8,
        "type": "graph",
        "title": "Network Received",
        "targets": [
            {
                "expr": (
                    'rate(node_network_receive_bytes_total'
                    '{instance="$server",device!~"lo"}[5m])'
                ),
                "legendFormat": "{{device}}",
                "refId": "A",
            }
        ],
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 7},
    }
    transmit = {
        "id": 9,
        "type": "graph",
        "title": "Network Transmitted",
        "targets": [
            {
                "expr": (
                    'rate(node_network_transmit_bytes_total'
                    '{instance="$server",device!~"lo"}[5m])'
                ),
                "legendFormat": "{{device}}",
                "refId": "A",
            }
        ],
        "gridPos": {"x": 12, "y": 0, "w": 12, "h": 7},
    }
    recv_result, recv_query, recv_panel = _translate_8171(receive)
    xmit_result, xmit_query, xmit_panel = _translate_8171(transmit)
    assert recv_result.status in {"migrated", "migrated_with_warnings"}, recv_result.reasons
    assert xmit_result.status in {"migrated", "migrated_with_warnings"}, xmit_result.reasons
    for query in (recv_query, xmit_query):
        assert query.strip().startswith("TS ")
        assert "PROMQL " not in query
        assert "GROK" not in query
        assert "_timeseries" not in query
        assert "time_bucket" in query
        assert "labels.device" in query
        assert "?server" in query
        assert '"lo"' in query
    assert "node_network_receive_bytes_total" in recv_query
    assert "node_network_transmit_bytes_total" in xmit_query
    assert (recv_panel.get("esql") or {}).get("type") == "line"
    assert (xmit_panel.get("esql") or {}).get("type") == "line"
    recv_bd = ((recv_panel.get("esql") or {}).get("breakdown") or {}).get("field")
    xmit_bd = ((xmit_panel.get("esql") or {}).get("breakdown") or {}).get("field")
    assert recv_bd == "labels.device"
    assert xmit_bd == "labels.device"


def test_8171_does_not_override_memory_usage_by_title():
    resolved, _ = _resolve_8171()
    query_titles = {item["title_match"] for item in resolved.panel_query_overrides}
    layout_titles = {item["title_match"] for item in resolved.panel_layout_overrides}
    assert "Memory Usage" not in query_titles
    assert "Memory Usage" not in layout_titles


def test_8171_layout_renames_idle_cpu_to_cpu_busy():
    resolved, _ = _resolve_8171()
    panels = [
        {
            "title": "Idle CPU",
            "esql": {"type": "line", "query": "FROM metrics-*"},
            "position": {"x": 0, "y": 0},
            "size": {"w": 24, "h": 14},
        }
    ]
    _apply_panel_layout_overrides_recursively(panels, resolved.panel_layout_overrides)
    assert panels[0]["title"] == "CPU Busy"
