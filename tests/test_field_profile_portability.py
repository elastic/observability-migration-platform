# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the curated-pack field-profile portability linter."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier.profile_leakage import check_profile_leakage  # noqa: E402


def test_leakage_flags_labels_prefix_under_otel():
    q = "TS metrics-* | WHERE labels.pod IS NOT NULL | STATS c = COUNT(*)"
    violations = check_profile_leakage(q, "otel")
    assert any("labels.pod" in v for v in violations)


def test_leakage_flags_prometheus_labels_under_native():
    q = "TS metrics-* | WHERE prometheus.labels.instance IS NOT NULL"
    violations = check_profile_leakage(q, "prometheus_native")
    assert any("prometheus.labels.instance" in v for v in violations)


def test_leakage_clean_native_labels():
    q = "TS metrics-* | WHERE labels.pod IS NOT NULL | KEEP `labels.pod`"
    assert check_profile_leakage(q, "prometheus_native") == []


def test_leakage_clean_prometheus_metrics():
    q = "TS metrics-* | WHERE prometheus.labels.pod IS NOT NULL | STATS s = SUM(prometheus.metrics.foo)"
    assert check_profile_leakage(q, "prometheus_metrics") == []


def test_source_label_names_loads_from_pack_query_block():
    from observability_migration.adapters.source.grafana.extension_schema import (
        GrafanaRulePackModel,
    )

    payload = GrafanaRulePackModel.model_validate(
        {"query": {"source_label_names": {"pod": "pod_name", "instance": "kubernetes_io_hostname"}}}
    )
    assert payload.query.source_label_names["pod"] == "pod_name"
    assert payload.query.source_label_names["instance"] == "kubernetes_io_hostname"


def test_source_label_names_defaults_empty_on_rule_pack_config():
    from observability_migration.adapters.source.grafana.rules import RulePackConfig

    assert RulePackConfig().source_label_names == {}


def test_source_label_names_populated_by_pack_loader(tmp_path):
    from observability_migration.adapters.source.grafana.rules import load_rule_pack_files

    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "query:\n"
        "  source_label_names:\n"
        "    pod: pod_name\n"
        "    instance: kubernetes_io_hostname\n",
        encoding="utf-8",
    )

    pack = load_rule_pack_files([str(rules_file)])

    assert pack.source_label_names == {
        "pod": "pod_name",
        "instance": "kubernetes_io_hostname",
    }


def _resolver(profile, *, label_rewrites=None, label_candidates=None, source_label_names=None):
    from observability_migration.adapters.source.grafana.rules import RulePackConfig
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    pack = RulePackConfig()
    pack.label_rewrites = label_rewrites or {}
    pack.label_candidates = label_candidates or {}
    pack.source_label_names = source_label_names or {}
    return SchemaResolver(pack, field_profile=profile)


# Pack-declared canonical labels. Values are candidate *target fields* (OTel
# spellings), matching how real packs populate ``label_candidates`` and the
# resolver's documented resolution priority (user label_candidates prepend).
# The resolver then namespaces per profile (labels.* / prometheus.labels.*)
# rather than echoing the raw candidate.
CANON = {"pod": ["k8s.pod.name"], "instance": ["service.instance.id"]}


@pytest.mark.parametrize("profile,expected", [
    ("prometheus_native", "labels.pod"),
    ("prometheus_metrics", "prometheus.labels.pod"),
    ("prometheus_remote_write", "prometheus.labels.pod"),
    ("otel", "k8s.pod.name"),
])
def test_canonical_label_resolves_per_profile(profile, expected):
    r = _resolver(profile, label_candidates=CANON)
    assert r.resolve_label("pod") == expected


@pytest.mark.parametrize("profile,expected", [
    ("prometheus_native", "labels.pod"),
    ("prometheus_metrics", "prometheus.labels.pod"),
    ("otel", "k8s.pod.name"),
])
def test_heapster_rewrite_recurses_to_canonical(profile, expected):
    # pod_name -> pod (canonical), then profile namespacing
    r = _resolver(profile, label_rewrites={"pod_name": "pod"}, label_candidates=CANON)
    assert r.resolve_label("pod_name") == expected


def test_passthrough_is_source_faithful():
    r = _resolver("passthrough", label_candidates=CANON,
                  source_label_names={"pod": "pod_name"})
    assert r.resolve_label("pod") == "pod_name"       # canonical placeholder -> source
    assert r.resolve_label("pod_name") == "pod_name"  # raw source stays source


def test_concrete_rewrite_target_is_literal_escape_hatch():
    # target is NOT a canonical label -> returned verbatim (today's behavior)
    r = _resolver("otel", label_rewrites={"weird": "some.concrete.field"})
    assert r.resolve_label("weird") == "some.concrete.field"


def _metric_resolver(profile, metric_map):
    from observability_migration.adapters.source.grafana.rules import RulePackConfig
    from observability_migration.adapters.source.grafana.schema import SchemaResolver
    from observability_migration.core.metric_mapping import normalize_metric_map

    pack = RulePackConfig()
    pack.metric_map = normalize_metric_map(metric_map)
    return SchemaResolver(pack, field_profile=profile)


@pytest.mark.parametrize("profile,expected", [
    ("prometheus_native", "metrics.pg_database_size_bytes"),
    ("prometheus_metrics", "prometheus.metrics.pg_database_size_bytes"),
    ("otel", "pg_database_size_bytes"),
])
def test_metric_map_target_is_namespaced(profile, expected):
    # metric_map target is a BARE logical metric name; the resolver namespaces
    # it per active profile instead of returning it verbatim.
    r = _metric_resolver(profile, {"pg_database_size": "pg_database_size_bytes"})
    assert r.resolve_metric_field("pg_database_size") == expected


def test_applied_metric_map_records_resolved_native_field():
    # ``_metric_map_applied`` feeds migration_report.json's metric-rename map,
    # which ``obs-migrate compare`` replays single-hop into the parity oracle.
    # It MUST record the fully-resolved (profile-namespaced) field the emitted
    # ES|QL actually queries -- not the bare target -- or the reference PromQL
    # for a renamed metric qualifies to a field that addresses nothing.
    r = _metric_resolver("prometheus_native", {"pg_database_size": "pg_database_size_bytes"})
    assert r.resolve_metric_field("pg_database_size") == "metrics.pg_database_size_bytes"
    assert r._metric_map_applied["pg_database_size"] == "metrics.pg_database_size_bytes"


def test_applied_metric_map_records_bare_field_under_otel():
    # Under otel the resolved field is the bare logical name, so the recorded
    # rename target is bare too (no profile namespace to qualify into).
    r = _metric_resolver("otel", {"pg_database_size": "pg_database_size_bytes"})
    assert r.resolve_metric_field("pg_database_size") == "pg_database_size_bytes"
    assert r._metric_map_applied["pg_database_size"] == "pg_database_size_bytes"


# One representative de-prefixed metric_map target per shipped pack, paired with
# the OLD verbatim (metrics.-prefixed) spelling those packs used to author.
# De-prefix + native namespacing MUST net to the exact same native field, i.e.
# the engine change is byte-identical for prometheus_native.
_PACK_NATIVE_IDENTITY = [
    # (dashboard, source_metric, old_prefixed_native_field)
    ({"gnetId": 12485, "title": "PostgreSQL Exporter", "tags": []},
     "pg_database_size", "metrics.pg_database_size_bytes"),
    ({"gnetId": 14114, "title": "PostgreSQL Exporter Quickstart and Dashboard",
      "tags": ["postgres"]},
     "pg_stat_bgwriter_buffers_alloc", "metrics.pg_stat_bgwriter_buffers_alloc_total"),
    ({"gnetId": 6417, "title": "Kubernetes Cluster (Prometheus)",
      "tags": ["kubernetes", "kubernetes-app"]},
     "node_filesystem_size", "metrics.node_filesystem_size_bytes"),
    ({"gnetId": 8171, "title": "Kubernetes Nodes", "tags": ["nodes", "prometheus"]},
     "node_nfsd_disk_bytes_read_total", "metrics.node_disk_read_bytes_total"),
    ({"gnetId": 7362, "title": "MySQL Overview", "tags": ["Percona", "MySQL"]},
     "mysql_info_schema_threads", "metrics.mysql_info_schema_processlist_threads"),
    ({"gnetId": 14091, "title": "Redis Exporter Quickstart", "tags": []},
     "redis_memory_fragmentation_ratio", "metrics.redis_mem_fragmentation_ratio"),
]


@pytest.mark.parametrize("dashboard,source,old_native", _PACK_NATIVE_IDENTITY)
def test_shipped_pack_metric_map_native_identity(dashboard, source, old_native):
    from observability_migration.adapters.source.grafana.rules import (
        RulePackConfig,
        resolve_pack_for_dashboard,
    )
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved, field_profile="prometheus_native")
    assert resolver.resolve_metric_field(source) == old_native


@pytest.mark.parametrize("profile,expected", [
    ("prometheus_native", "labels.deployment"),
    ("prometheus_metrics", "prometheus.labels.deployment"),
    ("otel", "k8s.deployment.name"),
])
def test_control_field_override_is_canonical(profile, expected):
    from observability_migration.adapters.source.grafana.rules import RulePackConfig
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    pack = RulePackConfig()
    pack.control_field_overrides = {"Deployment": "deployment"}
    r = SchemaResolver(pack, field_profile=profile)
    assert r.resolve_control_field("Deployment") == expected
