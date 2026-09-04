# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the curated-pack field-profile portability linter."""

from __future__ import annotations

import glob
import json
import os
import re
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


def _migrate(
    input_dir: str,
    out_dir: Path,
    profile: str,
    index: str,
    extra: list[str] | None = None,
) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    cmd = [
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
    ]
    if extra:
        cmd.extend(extra)
    subprocess.run(
        cmd,
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
    ("1471", "kubernetes-apps"),
    ("3831", "kubernetes-cluster-autoscaler-via-prometheus"),
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


# ---------------------------------------------------------------------------
# Always-on field-profile coverage (no /tmp fixtures).
#
# The corpus migrates above skip when /tmp/community is absent, so they do not
# gate PRs. The tests below exercise the same leakage contract against in-repo
# packs and a synthetic engine-only dashboard, so every profile is checked in
# `make test`.
# ---------------------------------------------------------------------------

_ALL_PROFILES = [
    "otel",
    "prometheus_native",
    "prometheus_metrics",
    "prometheus_remote_write",
    "passthrough",
]

_PACK_ROOT = ROOT / "observability_migration" / "adapters" / "source" / "grafana" / "curated_packs"

# Authored curated ES|QL must not bake in a single profile's physical fields.
# `TS metrics-*` is an index pattern (hyphen, not a dotted field) and is allowed.
_AUTHORED_HARDCODED_NS = (
    re.compile(r"(?<![\w.{])labels\.[A-Za-z_]"),
    re.compile(r"(?<![\w.{])prometheus\.labels\."),
    re.compile(r"(?<![\w.{])prometheus\.metrics\."),
    re.compile(r"(?<![\w.{])metrics\.[A-Za-z_]"),
    re.compile(r"(?<![\w.{])prometheus\.[A-Za-z_][\w]*\.(counter|value|rate)\b"),
    re.compile(r"(?<![\w.{])k8s\.[A-Za-z_]"),
)

_PLACEHOLDER_LEFT = re.compile(r"\{\{\s*(?:control|label|metric):")


def _grafana_pack_yamls() -> list[Path]:
    return sorted(_PACK_ROOT.glob("grafana_*/pack.yaml"))


def _authored_esql_queries(pack) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for override in pack.panel_query_overrides:
        query = str(override.get("esql_query") or "")
        if query.strip():
            title = str(override.get("title_match") or "")
            out.append((title, query))
    return out


def test_curated_pack_esql_overrides_author_canonical_fields():
    """Pack YAML ES|QL must use {{label:}} / {{metric:}} tokens, not labels.*."""
    from observability_migration.adapters.source.grafana.rules import load_rule_pack_files

    scanned = 0
    for pack_yaml in _grafana_pack_yamls():
        pack = load_rule_pack_files([str(pack_yaml)])
        for title, query in _authored_esql_queries(pack):
            stripped = re.sub(r"\{\{[^}]+\}\}", "", query)
            hits = [
                f"{pattern.pattern}: {match.group(0)}"
                for pattern in _AUTHORED_HARDCODED_NS
                for match in pattern.finditer(stripped)
            ]
            assert hits == [], (
                f"{pack_yaml.parent.name} {title!r} hardcodes a profile "
                f"namespace in authored ES|QL: {hits}"
            )
            scanned += 1
    assert scanned >= 10, f"expected curated ES|QL overrides, scanned {scanned}"


@pytest.mark.parametrize("profile", _ALL_PROFILES)
def test_curated_pack_esql_overrides_portable_per_profile(profile):
    """Every shipped pack's curated ES|QL expands cleanly for each profile."""
    from observability_migration.adapters.source.grafana.panels import (
        _materialize_curated_query_override,
    )
    from observability_migration.adapters.source.grafana.rules import load_rule_pack_files
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    scanned = 0
    for pack_yaml in _grafana_pack_yamls():
        pack = load_rule_pack_files([str(pack_yaml)])
        resolver = SchemaResolver(pack, field_profile=profile)
        for title, query in _authored_esql_queries(pack):
            rendered = _materialize_curated_query_override(query, resolver)
            leftover = _PLACEHOLDER_LEFT.findall(rendered)
            assert leftover == [], (
                f"{pack_yaml.parent.name} {title!r} left placeholders under "
                f"{profile}: {leftover}"
            )
            violations = check_profile_leakage(rendered, profile)
            assert violations == [], (
                f"{pack_yaml.parent.name} {title!r} leaked under {profile}: "
                f"{violations}"
            )
            scanned += 1
    assert scanned >= 10, f"expected curated ES|QL overrides, scanned {scanned}"


_1471_PODS_PANEL = {
    "id": 7,
    "type": "graph",
    "title": "Number of pods",
    "targets": [
        {
            "expr": (
                'count(count(container_memory_usage_bytes{container_name="$container",'
                ' namespace="$namespace"}) by (pod_name))'
            ),
            "legendFormat": "pods",
            "refId": "A",
        },
        {
            "expr": (
                'count(count(container_memory_usage_bytes{container_name="$container",'
                ' namespace="$namespace"}) by (kubernetes_io_hostname))'
            ),
            "legendFormat": "hosts",
            "refId": "B",
        },
    ],
    "gridPos": {"x": 0, "y": 0, "w": 24, "h": 7},
}


@pytest.mark.parametrize(
    "profile,pod,container,metric",
    [
        ("otel", "k8s.pod.name", "k8s.container.name", "container_memory_usage_bytes"),
        ("prometheus_native", "labels.pod", "labels.container", "metrics.container_memory_usage_bytes"),
        (
            "prometheus_metrics",
            "prometheus.labels.pod",
            "prometheus.labels.container",
            "prometheus.metrics.container_memory_usage_bytes",
        ),
        (
            "prometheus_remote_write",
            "prometheus.labels.pod",
            "prometheus.labels.container",
            "prometheus.container_memory_usage_bytes.value",
        ),
        ("passthrough", "pod_name", "container_name", "container_memory_usage_bytes"),
    ],
)
def test_1471_curated_query_expands_per_profile(profile, pod, container, metric):
    """1471's {{label:}} / {{metric:}} tokens follow the operator's field profile."""
    from observability_migration.adapters.source.grafana.panels import translate_panel
    from observability_migration.adapters.source.grafana.rules import (
        RulePackConfig,
        resolve_pack_for_dashboard,
    )
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    resolved = resolve_pack_for_dashboard(
        {"gnetId": 1471, "title": "Kubernetes App Metrics", "tags": []},
        RulePackConfig(),
    )
    resolver = SchemaResolver(resolved, field_profile=profile)
    yaml_panel, result = translate_panel(
        _1471_PODS_PANEL,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
        section_title="Pod count",
    )
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert pod in query, query
    assert container in query, query
    assert metric in query, query
    assert "pod_name" not in query or profile == "passthrough"
    assert "kubernetes_io_hostname" not in query or profile == "passthrough"
    assert check_profile_leakage(query, profile) == [], query


@pytest.mark.parametrize(
    "profile,container_field,namespace_field",
    [
        ("otel", "k8s.container.name", "k8s.namespace.name"),
        ("prometheus_native", "labels.container", "labels.namespace"),
        ("prometheus_metrics", "prometheus.labels.container", "prometheus.labels.namespace"),
        ("prometheus_remote_write", "prometheus.labels.container", "prometheus.labels.namespace"),
        ("passthrough", "container_name", "namespace"),
    ],
)
def test_1471_controls_follow_field_profile(profile, container_field, namespace_field):
    from observability_migration.adapters.source.grafana.panels import translate_dashboard
    from observability_migration.adapters.source.grafana.rules import (
        RulePackConfig,
        resolve_pack_for_dashboard,
    )
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    dashboard = {
        "gnetId": 1471,
        "title": "Kubernetes App Metrics",
        "tags": [],
        "templating": {
            "list": [
                {
                    "name": "namespace",
                    "type": "query",
                    "query": (
                        "label_values(container_memory_usage_bytes"
                        '{namespace=~".+",container_name!="POD"},namespace)'
                    ),
                },
                {
                    "name": "container",
                    "type": "query",
                    "query": (
                        "label_values(container_memory_usage_bytes"
                        '{namespace=~"$namespace",container_name!="POD"},container_name)'
                    ),
                },
            ]
        },
        "panels": [_1471_PODS_PANEL],
    }
    resolved = resolve_pack_for_dashboard(dashboard, RulePackConfig())
    resolver = SchemaResolver(resolved, field_profile=profile)
    result = translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=resolved,
        resolver=resolver,
    )
    controls = result.dashboard_ir.to_yaml_dict().get("controls") or []
    by_name = {c.get("variable_name"): c for c in controls}
    assert "namespace" in by_name and "container" in by_name, controls
    ns_blob = json.dumps(by_name["namespace"])
    c_blob = json.dumps(by_name["container"])
    assert container_field in c_blob, c_blob
    assert namespace_field in ns_blob, ns_blob
    if profile != "passthrough":
        assert "container_name" not in c_blob
        assert "container_name" not in ns_blob


_ENGINE_ONLY_PANEL = {
    "id": 1,
    "type": "timeseries",
    "title": "Memory by pod",
    "targets": [
        {
            "expr": 'sum(container_memory_usage_bytes{namespace=~"$namespace"}) by (pod)',
            "legendFormat": "{{pod}}",
            "refId": "A",
        }
    ],
    "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
}


@pytest.mark.parametrize(
    "profile,pod,metric",
    [
        ("otel", "k8s.pod.name", "container_memory_usage_bytes"),
        ("prometheus_native", "labels.pod", "metrics.container_memory_usage_bytes"),
        ("prometheus_metrics", "prometheus.labels.pod", "prometheus.metrics.container_memory_usage_bytes"),
        (
            "prometheus_remote_write",
            "prometheus.labels.pod",
            "prometheus.container_memory_usage_bytes.value",
        ),
        ("passthrough", "pod", "container_memory_usage_bytes"),
    ],
)
def test_engine_only_panel_follows_field_profile(profile, pod, metric):
    """A dashboard that matches no curated pack still namespaces per --field-profile."""
    from observability_migration.adapters.source.grafana.curated_packs import find_curated_pack
    from observability_migration.adapters.source.grafana.panels import translate_panel
    from observability_migration.adapters.source.grafana.rules import RulePackConfig
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    assert find_curated_pack(None, "Engine-only field profile probe", []) is None
    pack = RulePackConfig()
    resolver = SchemaResolver(pack, field_profile=profile)
    yaml_panel, result = translate_panel(
        _ENGINE_ONLY_PANEL,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=pack,
        resolver=resolver,
    )
    query = (yaml_panel.get("esql") or {}).get("query") or ""
    assert result.status in {"migrated", "migrated_with_warnings"}, result.reasons
    assert query, result.reasons
    assert pod in query, query
    assert metric in query, query
    assert check_profile_leakage(query, profile) == [], query


def test_k8s_pack_canonical_labels_namespace_under_native():
    from observability_migration.adapters.source.grafana.rules import (
        RulePackConfig,
        resolve_pack_for_dashboard,
    )
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    expected = {
        1471: ["pod", "container", "namespace", "instance"],
        3831: [],
        315: ["pod", "container", "namespace"],
        6417: ["instance", "namespace"],
        741: ["pod", "container", "instance"],
        8171: ["instance"],
    }
    for gnet_id, labels in expected.items():
        resolved = resolve_pack_for_dashboard(
            {"gnetId": gnet_id, "title": "", "tags": []},
            RulePackConfig(),
        )
        resolver = SchemaResolver(resolved, field_profile="prometheus_native")
        otel = SchemaResolver(resolved, field_profile="otel")
        for label in labels:
            native = resolver.resolve_label(label)
            assert native == f"labels.{label}", f"{gnet_id} {label} -> {native}"
            otel_field = otel.resolve_label(label)
            assert not otel_field.startswith("labels."), (
                f"{gnet_id} {label} leaked native spelling under otel: {otel_field}"
            )


def _write_solo_dashboard(tmp_path: Path, payload: dict) -> str:
    src = tmp_path / "src"
    src.mkdir(parents=True)
    (src / "dashboard.json").write_text(json.dumps(payload), encoding="utf-8")
    return str(src)


def _leakage_from_out(out: Path, profile: str) -> list[str]:
    violations: list[str] = []
    produced = glob.glob(str(out / "dashboards" / "native" / "*.native.json"))
    assert produced, f"migration emitted no native dashboards under {profile}"
    for native in produced:
        with open(native, encoding="utf-8") as fh:
            data = json.load(fh)
        for query in extract_esql_queries(data):
            violations += check_profile_leakage(query, profile)
    return violations


@pytest.mark.parametrize("profile", ["otel", "prometheus_native"])
def test_cli_migrate_curated_1471_and_engine_only_no_leakage(profile, tmp_path):
    """CLI --field-profile threads through both a curated pack and engine-only."""
    curated_src = _write_solo_dashboard(
        tmp_path / "curated",
        {
            "gnetId": 1471,
            "title": "Kubernetes App Metrics",
            "schemaVersion": 38,
            "panels": [_1471_PODS_PANEL],
            "templating": {
                "list": [
                    {
                        "name": "namespace",
                        "type": "query",
                        "query": (
                            "label_values(container_memory_usage_bytes"
                            '{container_name!="POD"}, namespace)'
                        ),
                    }
                ]
            },
        },
    )
    engine_src = _write_solo_dashboard(
        tmp_path / "engine",
        {
            "title": "Engine-only field profile probe",
            "uid": "fp-engine-probe",
            "schemaVersion": 38,
            "panels": [_ENGINE_ONLY_PANEL],
        },
    )
    curated_out = tmp_path / "curated-out"
    engine_out = tmp_path / "engine-out"
    extra = ["--translation-mode", "esql"]
    _migrate(curated_src, curated_out, profile, "metrics-*", extra=extra)
    _migrate(engine_src, engine_out, profile, "metrics-*", extra=extra)

    curated_violations = _leakage_from_out(curated_out, profile)
    engine_violations = _leakage_from_out(engine_out, profile)
    assert curated_violations == [], curated_violations
    assert engine_violations == [], engine_violations

    curated_native = glob.glob(str(curated_out / "dashboards" / "native" / "*.native.json"))
    with open(curated_native[0], encoding="utf-8") as fh:
        curated_blob = fh.read()
    if profile == "prometheus_native":
        assert "labels.pod" in curated_blob
        assert "k8s.pod.name" not in curated_blob
    else:
        assert "k8s.pod.name" in curated_blob
        assert "labels.pod" not in curated_blob

