# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the curated-pack field-profile portability linter."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier.profile_leakage import (  # noqa: E402
    check_profile_leakage,
    extract_esql_queries,
)

# Native byte-identical goldens: the prometheus_native migration output is the
# invariant the field-profile portability change must never perturb. The source
# fixtures are the pinned /tmp checkouts used by the live-migrate flow; skip
# (never fail) when they are absent, matching the repo convention for tests that
# shell out to the CLI against on-disk fixtures.
_NATIVE_BASELINE_DIR = ROOT / "tests" / "fixtures" / "field_profile_native_baseline"
_NATIVE_INDEX = "metrics-k8s.prometheus-default"


def _migrate(input_dir: str, out_dir: Path, profile: str, index: str) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    subprocess.run(
        [
            sys.executable,
            "-m",
            "observability_migration.app.cli",
            "migrate",
            "--source",
            "grafana",
            "--input-mode",
            "files",
            "--input-dir",
            input_dir,
            "--output-dir",
            str(out_dir),
            "--assets",
            "dashboards",
            "--field-profile",
            profile,
            "--esql-index",
            index,
            "--data-view",
            index,
        ],
        check=True,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
    )


def _migrate_native(input_dir: str, out_dir: Path) -> None:
    _migrate(input_dir, out_dir, "prometheus_native", _NATIVE_INDEX)


def _assert_native_byte_identical(input_dir: str, golden: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _migrate_native(input_dir, out)
    produced = glob.glob(str(out / "dashboards" / "native" / "*.native.json"))
    assert produced, f"migration emitted no native dashboards from {input_dir}"
    with open(produced[0], encoding="utf-8") as fh:
        new_data = json.load(fh)
    with open(golden, encoding="utf-8") as fh:
        gold_data = json.load(fh)
    assert new_data == gold_data


@pytest.mark.skipif(
    not Path("/tmp/gnet-741").is_dir(),
    reason="requires the pinned /tmp/gnet-741 Grafana source fixture",
)
def test_741_native_is_byte_identical(tmp_path):
    _assert_native_byte_identical(
        "/tmp/gnet-741", _NATIVE_BASELINE_DIR / "741.native.json", tmp_path
    )


@pytest.mark.skipif(
    not Path("/tmp/gnet-8171").is_dir(),
    reason="requires the pinned /tmp/gnet-8171 Grafana source fixture",
)
def test_8171_native_is_byte_identical(tmp_path):
    _assert_native_byte_identical(
        "/tmp/gnet-8171", _NATIVE_BASELINE_DIR / "8171.native.json", tmp_path
    )


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


@pytest.mark.parametrize("profile,expected_metric", [
    # A metric_map-renamed metric inside a native PROMQL ``value=(…)`` command
    # must be namespaced for the active profile, exactly like a non-renamed
    # metric. Returning the bare metric_map target (regression) emitted e.g.
    # ``sum(pg_database_size_bytes{…})`` under native, which addresses no field
    # in a ``metrics.*``-namespaced index and rendered the panel empty.
    ("prometheus_native", "metrics.pg_database_size_bytes"),
    ("prometheus_metrics", "prometheus.metrics.pg_database_size_bytes"),
    ("otel", "pg_database_size_bytes"),
    ("passthrough", "pg_database_size_bytes"),
])
def test_native_promql_metric_map_target_is_namespaced(profile, expected_metric):
    from observability_migration.adapters.source.grafana.panels import (
        _prefix_native_metric_fields,
    )

    r = _metric_resolver(profile, {"pg_database_size": "pg_database_size_bytes"})
    out = _prefix_native_metric_fields(
        'sum(pg_database_size{instance=~"$Instance"})', r
    )
    assert out == f'sum({expected_metric}{{instance=~"$Instance"}})'


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


# End-to-end per-pack profile-leakage: migrate each converted Kubernetes pack's
# source dashboard SOLO under every non-native profile and assert the emitted
# ES|QL never references a field namespaced for a *different* profile (e.g.
# ``labels.pod`` under otel). Native spelling is already guarded byte-identical
# by the goldens above; these cover the portability direction. Sourced from the
# pinned community corpus; skip (never fail) when it is absent.
_COMMUNITY_DIR = Path("/tmp/community")

# (pack id, community source dashboard basename)
_K8S_PACK_CORPUS = [
    ("741", "deployment-metrics"),
    ("8171", "kubernetes-nodes"),
    ("6417", "kubernetes-cluster-prometheus"),
    ("315", "kubernetes-cluster-monitoring-via-prometheus-315"),
    ("315-1621", "kubernetes-cluster-monitoring-via-prometheus-1621"),
]

_LEAKAGE_PROFILES = [
    "otel",
    "prometheus_metrics",
    "prometheus_remote_write",
    "passthrough",
]


@pytest.mark.parametrize("pack_id,corpus_basename", _K8S_PACK_CORPUS)
@pytest.mark.parametrize("profile", _LEAKAGE_PROFILES)
def test_k8s_pack_no_profile_leakage(pack_id, corpus_basename, profile, tmp_path):
    source = _COMMUNITY_DIR / f"{corpus_basename}.json"
    if not source.is_file():
        pytest.skip(f"requires the pinned community corpus fixture {source}")
    solo = tmp_path / "src"
    solo.mkdir()
    shutil.copy(source, solo / source.name)
    out = tmp_path / "out"
    _migrate(str(solo), out, profile, "metrics-*")
    violations: list[str] = []
    for native in glob.glob(str(out / "dashboards" / "native" / "*.native.json")):
        with open(native, encoding="utf-8") as fh:
            data = json.load(fh)
        for query in extract_esql_queries(data):
            violations += check_profile_leakage(query, profile)
    assert violations == [], f"pack {pack_id} leaked under {profile}: {violations}"


# Same end-to-end profile-leakage check for the converted Redis packs. The two
# packs with a pinned community source dashboard are covered here; skip (never
# fail) when that corpus fixture is absent.
_REDIS_PACK_CORPUS = [
    ("763", "redis-exporter-763"),
    ("11835", "redis-dashboard-for-prometheus-redis-exporter-helm-stable-redis-ha"),
]


@pytest.mark.parametrize("pack_id,corpus_basename", _REDIS_PACK_CORPUS)
@pytest.mark.parametrize("profile", _LEAKAGE_PROFILES)
def test_redis_pack_no_profile_leakage(pack_id, corpus_basename, profile, tmp_path):
    source = _COMMUNITY_DIR / f"{corpus_basename}.json"
    if not source.is_file():
        pytest.skip(f"requires the pinned community corpus fixture {source}")
    solo = tmp_path / "src"
    solo.mkdir()
    shutil.copy(source, solo / source.name)
    out = tmp_path / "out"
    _migrate(str(solo), out, profile, "metrics-*")
    violations: list[str] = []
    for native in glob.glob(str(out / "dashboards" / "native" / "*.native.json")):
        with open(native, encoding="utf-8") as fh:
            data = json.load(fh)
        for query in extract_esql_queries(data):
            violations += check_profile_leakage(query, profile)
    assert violations == [], f"pack {pack_id} leaked under {profile}: {violations}"


# Same end-to-end profile-leakage check for Postgres / MySQL / Node-Exporter packs.
_PG_MYSQL_NODE_PACK_CORPUS = [
    ("12485", "postgresql-exporter"),
    ("14114", "postgres-overview"),
    ("9628", "postgresql-database"),
    ("7362", "mysql-overview"),
    ("1860", "node-exporter-full"),
]


@pytest.mark.parametrize("pack_id,corpus_basename", _PG_MYSQL_NODE_PACK_CORPUS)
@pytest.mark.parametrize("profile", _LEAKAGE_PROFILES)
def test_pg_mysql_node_pack_no_profile_leakage(pack_id, corpus_basename, profile, tmp_path):
    source = _COMMUNITY_DIR / f"{corpus_basename}.json"
    if not source.is_file():
        pytest.skip(f"requires the pinned community corpus fixture {source}")
    solo = tmp_path / "src"
    solo.mkdir()
    shutil.copy(source, solo / source.name)
    out = tmp_path / "out"
    _migrate(str(solo), out, profile, "metrics-*")
    violations: list[str] = []
    for native in glob.glob(str(out / "dashboards" / "native" / "*.native.json")):
        with open(native, encoding="utf-8") as fh:
            data = json.load(fh)
        for query in extract_esql_queries(data):
            violations += check_profile_leakage(query, profile)
    assert violations == [], f"pack {pack_id} leaked under {profile}: {violations}"
