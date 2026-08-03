# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline contract tests for the Redis 11835 interaction scenario."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from observability_migration.core.telemetry_contract import (
    _extract_esql_values_bound_field,
    build_telemetry_contract,
)
from observability_migration.core.telemetry_data import generate_documents
from observability_migration.targets.kibana.interaction_audit import (
    _VALUE_PARAM_TOKEN,
    CapabilityCategory,
)
from observability_migration.targets.kibana.interaction_audit_local import (
    derive_panel_contract,
    load_stable_panels_from_ir,
)
from observability_migration.targets.kibana.interaction_scenarios import (
    DiscoveredControl,
    build_execution_plan,
    load_scenario,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REDIS_MANIFEST = REPO_ROOT / "parity-rig" / "interaction-scenarios" / "redis-11835.yaml"
REDIS_SOURCE = REPO_ROOT / "infra/grafana/dashboards/redis-11835.json"
REDIS_CONTROL_SCHEMA = REPO_ROOT / "infra/grafana/dashboards/control_schemas/redis-11835.json"

_REDIS_BASELINE_WARNING_PANELS = (
    "Memory Usage",
    "Network I/O",
    "Expiring vs Not-Expiring Keys",
    "Expired / Evicted",
    "Command Calls / sec",
)

_QUERY_PANEL_TITLES = (
    "Uptime",
    "Clients",
    "Memory Usage",
    "Commands Executed / sec",
    "Hits / Misses per Sec",
    "Total Memory Usage",
    "Network I/O",
    "Total Items per DB",
    "Expiring vs Not-Expiring Keys",
    "Expired / Evicted",
    "Command Calls / sec",
    "Redis connected clients",
)


def _migrate_redis(tmp_path: Path) -> Path:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()
    dashboard = json.loads(REDIS_SOURCE.read_text(encoding="utf-8"))
    (input_dir / REDIS_SOURCE.name).write_text(json.dumps(dashboard), encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "observability_migration.adapters.source.grafana.cli",
        "--source",
        "files",
        "--input-dir",
        str(input_dir),
        "--output-dir",
        str(output_dir),
        "--assets",
        "dashboards",
        "--control-schema",
        str(REDIS_CONTROL_SCHEMA),
    ]
    es_url = os.environ.get("REDIS_INTERACTION_ES_URL")
    if es_url is None:
        es_url = "" if os.environ.get("REDIS_INTERACTION_OFFLINE", "1") == "1" else os.environ.get("ES_URL", "http://localhost:9200")
    if es_url:
        cmd.extend(["--es-url", es_url])
    completed = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return output_dir / "dashboards"


def _native_queries(artifact_root: Path) -> list[tuple[str, str]]:
    native_path = sorted((artifact_root / "native").glob("*.native.json"))[0]
    artifact = json.loads(native_path.read_text(encoding="utf-8"))
    parsed: list[tuple[str, str]] = []

    def _collect_queries(value: object, bucket: list[str]) -> None:
        if isinstance(value, dict):
            data_source = value.get("data_source")
            if isinstance(data_source, dict):
                query = data_source.get("query")
                if isinstance(query, str) and query.strip():
                    bucket.append(query)
            for child in value.values():
                _collect_queries(child, bucket)
        elif isinstance(value, list):
            for child in value:
                _collect_queries(child, bucket)

    def walk(panels: list[object]) -> None:
        for panel in panels:
            if not isinstance(panel, dict):
                continue
            nested = panel.get("panels")
            if isinstance(nested, list):
                walk(nested)
                continue
            config = panel.get("config")
            if not isinstance(config, dict):
                continue
            title = str(config.get("title") or "")
            panel_queries: list[str] = []
            _collect_queries(config, panel_queries)
            for query in panel_queries:
                parsed.append((title, query))

    walk(artifact["payload"].get("panels") or [])
    return parsed


@pytest.fixture(scope="module")
def redis_artifacts() -> Path:
    with tempfile.TemporaryDirectory(prefix="redis-interaction-") as tmpdir:
        yield _migrate_redis(Path(tmpdir))


def test_redis_manifest_strict_loads() -> None:
    scenario = load_scenario(REDIS_MANIFEST)

    assert scenario.id == "redis-11835"
    assert scenario.source_path == "infra/grafana/dashboards/redis-11835.json"
    assert scenario.control_schema_path == (
        "infra/grafana/dashboards/control_schemas/redis-11835.json"
    )
    assert (
        scenario.dashboard_title
        == "Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha)"
    )
    assert {control.key for control in scenario.controls} == {
        "namespace",
        "pod_name",
        "instance",
        "DS_PROMETHEUS",
        "gap_chained_controls",
    }
    assert {combination.id for combination in scenario.combinations} == {
        "namespace-and-pod_name",
        "namespace-and-instance",
        "all-three",
    }


def test_redis_manifest_has_no_uuid_panel_ids_or_embedded_queries() -> None:
    raw = yaml.safe_load(REDIS_MANIFEST.read_text(encoding="utf-8"))
    serialized = yaml.safe_dump(raw)
    assert "FROM metrics-*" not in serialized
    assert "TS metrics-*" not in serialized
    for control in raw["controls"]:
        affected = control["assertions"].get("affected_panels")
        if isinstance(affected, list):
            for panel_id in affected:
                assert not re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    panel_id,
                    flags=re.IGNORECASE,
                )


def test_redis_native_mapping_and_controls(redis_artifacts: Path) -> None:
    native_path = sorted((redis_artifacts / "native").glob("*.native.json"))[0]
    artifact = json.loads(native_path.read_text(encoding="utf-8"))
    mapping = artifact["mapping"]

    assert mapping["mapped"] == 12
    assert mapping["unmapped"] == 0
    assert mapping["controls"] == 3
    assert [
        control["config"]["variable_name"]
        for control in artifact["payload"]["pinned_panels"]
        if control.get("type") == "esql_control"
    ] == ["namespace", "pod_name", "instance"]


def test_redis_instance_affects_all_twelve_query_panels(redis_artifacts: Path) -> None:
    queries = _native_queries(redis_artifacts)
    assert len(queries) == 12
    assert {title for title, _query in queries} == set(_QUERY_PANEL_TITLES)
    instance_panels = [
        title for title, query in queries if "instance" in _VALUE_PARAM_TOKEN.findall(query)
    ]
    assert len(instance_panels) == 12

    contract = derive_panel_contract(
        [
            (f"panel-{index}", title, query)
            for index, (title, query) in enumerate(queries)
        ],
        control_keys=("namespace", "pod_name", "instance"),
    )
    assert len(contract.all_query_panels) == 12
    assert len(contract.by_control["instance"]) == 12
    assert contract.by_control.get("namespace") is None
    assert contract.by_control.get("pod_name") is None


def test_redis_baseline_warning_panels_are_cataloged(redis_artifacts: Path) -> None:
    report = json.loads((redis_artifacts / "migration_report.json").read_text(encoding="utf-8"))
    dashboard = report["dashboards"][0]
    warned = {
        panel["title"]
        for panel in dashboard["panels"]
        if panel.get("status") == "migrated_with_warnings"
    }
    assert set(_REDIS_BASELINE_WARNING_PANELS) <= warned
    assert len(warned) >= len(_REDIS_BASELINE_WARNING_PANELS)


def test_redis_execution_plan_covers_every_option_and_gaps(redis_artifacts: Path) -> None:
    scenario = load_scenario(REDIS_MANIFEST)
    contract = build_telemetry_contract(redis_artifacts)
    stream = contract["streams"]["metrics-*"]
    documents = [doc for _, doc in generate_documents(contract)]
    discovered = {
        "namespace": sorted(
            {
                doc.get("namespace") or doc.get("k8s.namespace.name")
                for doc in documents
                if doc.get("namespace") or doc.get("k8s.namespace.name")
            }
        ),
        "pod_name": sorted(
            {
                doc.get("pod") or doc.get("k8s.pod.name")
                for doc in documents
                if doc.get("pod") or doc.get("k8s.pod.name")
            }
        ),
        "instance": sorted(
            {
                doc.get("service.instance.id")
                for doc in documents
                if doc.get("service.instance.id")
            }
        ),
    }
    plan = build_execution_plan(
        scenario,
        [
            DiscoveredControl("namespace", "Namespace", tuple(discovered["namespace"])),
            DiscoveredControl("pod_name", "Pod Name", tuple(discovered["pod_name"])),
            DiscoveredControl("instance", "instance", tuple(discovered["instance"])),
        ],
    )
    option_steps = [step for step in plan if step.kind == "option"]
    assert len(option_steps) == sum(len(values) for values in discovered.values())
    assert {step.control_key for step in plan if step.kind == "coverage_gap"} == {
        "DS_PROMETHEUS",
        "gap_chained_controls",
    }
    assert {step.id for step in plan if step.kind == "combination"} == {
        "namespace-and-pod_name",
        "namespace-and-instance",
        "all-three",
    }
    assert stream["control_fields"]
    assert "service.instance.id" in stream["control_fields"]
    assert contract["streams"]["metrics-*"]["fields"]["redis_up"]["role"] == "metric"


def test_redis_stable_panel_identities_are_deterministic(redis_artifacts: Path) -> None:
    stable_panels = load_stable_panels_from_ir(
        redis_artifacts,
        dashboard_title="Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha)",
    )
    assert len(stable_panels) == 12
    assert {title for _panel_id, title in stable_panels} == set(_QUERY_PANEL_TITLES)


def test_redis_gap_and_source_only_capabilities() -> None:
    scenario = load_scenario(REDIS_MANIFEST)
    by_key = {control.key: control for control in scenario.controls}
    assert by_key["DS_PROMETHEUS"].capability is CapabilityCategory.SOURCE_ONLY
    assert by_key["gap_chained_controls"].capability is CapabilityCategory.MIGRATION_GAP
    assert by_key["instance"].assertions.affected_panels == "all_query_panels"
    assert by_key["namespace"].assertions.unaffected_panels == "all_query_panels"
    assert by_key["namespace"].assertions.affected_panels == ()
    assert by_key["namespace"].assertions.query_contains == ()
    assert by_key["namespace"].assertions.allow_incompatible_selections is False
    assert by_key["pod_name"].assertions.unaffected_panels == "all_query_panels"
    assert by_key["pod_name"].assertions.allow_incompatible_selections is False


def test_redis_decorative_controls_do_not_bind_panel_queries(redis_artifacts: Path) -> None:
    queries = _native_queries(redis_artifacts)
    assert len(queries) == 12
    for _title, query in queries:
        assert "?namespace" not in query
        assert "?pod_name" not in query
        assert "instance" in _VALUE_PARAM_TOKEN.findall(query)


def test_redis_namespace_manifest_merges_twelve_unaffected_capture_panels(
    redis_artifacts: Path,
) -> None:
    from observability_migration.targets.kibana.interaction_runner import (
        PanelContract,
        _merge_assertions,
    )

    scenario = load_scenario(REDIS_MANIFEST)
    namespace = next(control for control in scenario.controls if control.key == "namespace")
    stable_panels = load_stable_panels_from_ir(
        redis_artifacts,
        dashboard_title="Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha)",
    )
    contract = PanelContract(
        all_query_panels=tuple(panel_id for panel_id, _title in stable_panels),
        by_control={"instance": tuple(panel_id for panel_id, _title in stable_panels)},
    )
    findings: list = []
    merged = _merge_assertions((namespace,), {"namespace": "default"}, contract, findings)
    assert findings == []
    assert merged.expected_panels == ()
    assert merged.unaffected_panels == contract.all_query_panels
    assert merged.decorative_control_keys == ("namespace",)
    assert merged.query_contains == ()
    assert merged.allow_incompatible_selections is False


def test_redis_native_control_value_queries(redis_artifacts: Path) -> None:
    native_path = sorted((redis_artifacts / "native").glob("*.native.json"))[0]
    artifact = json.loads(native_path.read_text(encoding="utf-8"))
    controls = {
        panel["config"]["variable_name"]: panel["config"]["esql_query"]
        for panel in artifact["payload"]["pinned_panels"]
        if panel.get("type") == "esql_control"
    }
    assert "namespace" in controls["namespace"]
    assert "pod" in controls["pod_name"]
    assert "service.instance.id" in controls["instance"]


def test_redis_dashboard_has_no_text_or_markdown_panels(redis_artifacts: Path) -> None:
    report = json.loads((redis_artifacts / "migration_report.json").read_text(encoding="utf-8"))
    dashboard = report["dashboards"][0]
    non_query = [
        panel["title"]
        for panel in dashboard["panels"]
        if panel.get("status") == "skipped"
    ]
    assert non_query == []


def test_esql_control_bound_field_extraction() -> None:
    query = (
        "FROM metrics-* | WHERE redis_up IS NOT NULL AND `k8s.namespace.name` IS NOT NULL "
        "| STATS count = COUNT(*) BY `k8s.namespace.name`"
    )
    assert _extract_esql_values_bound_field(query) == "k8s.namespace.name"
