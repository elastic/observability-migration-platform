# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline contract tests for the Kubernetes / Views / Global interaction scenario."""

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
K8S_MANIFEST = REPO_ROOT / "parity-rig" / "interaction-scenarios" / "k8s-views-global.yaml"
K8S_SOURCE = REPO_ROOT / "infra/grafana/dashboards/k8s-views-global.json"
K8S_CONTROL_SCHEMA = (
    REPO_ROOT / "infra/grafana/dashboards/control_schemas/k8s-views-global.json"
)

_NON_QUERY_ROW_TITLES = (
    "Overview",
    "Resources",
    "Kubernetes",
    "Network",
)

_QUERY_PANEL_TITLES = (
    "Global CPU  Usage",
    "Global RAM Usage",
    "Nodes",
    "Kubernetes Resource Count",
    "Namespaces",
    "CPU Usage",
    "RAM Usage",
    "Running Pods",
    "Cluster CPU Utilization",
    "Cluster Memory Utilization",
    "CPU Utilization by namespace",
    "Memory Utilization by namespace",
    "CPU Utilization by instance",
    "Memory Utilization by instance",
    "CPU Throttled seconds by namespace",
    "CPU Core Throttled by instance",
    "Kubernetes Pods QoS classes",
    "Kubernetes Pods Status Reason",
    "OOM Events by namespace",
    "Container Restarts by namespace",
    "Global Network Utilization by device",
    "Network Saturation - Packets dropped",
    "Network Received by namespace",
    "Total Network Received (with all virtual devices) by instance",
    "Network Received (without loopback)  by instance",
    "Network Received (loopback only) by instance",
)

_LIVE_QUERY_PANEL_TITLES = (
    "Global CPU  Usage",
    "Nodes",
    "Kubernetes Resource Count",
    "Namespaces",
    "CPU Usage",
    "Running Pods",
    "Cluster CPU Utilization",
    "CPU Utilization by instance",
    "CPU Throttled seconds by namespace",
    "CPU Core Throttled by instance",
    "Kubernetes Pods Status Reason",
    "OOM Events by namespace",
    "Container Restarts by namespace",
)

_JOB_AFFECTED_STABLE_IDS = (
    "0.1",
    "0.5",
    "0.6",
    "1.0",
    "1.1",
    "1.4",
    "1.5",
    "1.7",
    "3.1",
)

_CLUSTER_ONLY_STABLE_IDS = (
    "0.0",
    "0.2",
    "0.3",
    "0.4",
    "0.7",
    "1.2",
    "1.3",
    "1.6",
    "2.0",
    "2.1",
    "2.2",
    "2.3",
    "3.0",
    "3.2",
    "3.3",
    "3.4",
    "3.5",
)


def _panel_dependency_sets(
    artifact_root: Path,
) -> tuple[list[str], list[str], dict[str, str]]:
    queries = _native_queries(artifact_root)
    stable_panels = load_stable_panels_from_ir(
        artifact_root,
        dashboard_title="Kubernetes / Views / Global",
    )
    title_to_stable = {title: stable_id for stable_id, title in stable_panels}
    job_titles = sorted(
        {
            title
            for title, query in queries
            if "job" in _VALUE_PARAM_TOKEN.findall(query)
        }
    )
    cluster_only_titles = sorted(
        {
            title
            for title, query in queries
            if "cluster" in _VALUE_PARAM_TOKEN.findall(query)
            and "job" not in _VALUE_PARAM_TOKEN.findall(query)
        }
    )
    return (
        job_titles,
        cluster_only_titles,
        title_to_stable,
    )


def _migrate_k8s(tmp_path: Path) -> Path:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()
    dashboard = json.loads(K8S_SOURCE.read_text(encoding="utf-8"))
    (input_dir / K8S_SOURCE.name).write_text(json.dumps(dashboard), encoding="utf-8")
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
        str(K8S_CONTROL_SCHEMA),
    ]
    es_url = os.environ.get("K8S_INTERACTION_ES_URL")
    if es_url is None:
        es_url = "" if os.environ.get("K8S_INTERACTION_OFFLINE", "1") == "1" else os.environ.get("ES_URL", "http://localhost:9200")
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
def k8s_artifacts() -> Path:
    with tempfile.TemporaryDirectory(prefix="k8s-interaction-") as tmpdir:
        yield _migrate_k8s(Path(tmpdir))


def test_k8s_manifest_strict_loads() -> None:
    scenario = load_scenario(K8S_MANIFEST)

    assert scenario.id == "k8s-views-global"
    assert scenario.source_path == "infra/grafana/dashboards/k8s-views-global.json"
    assert scenario.control_schema_path == (
        "infra/grafana/dashboards/control_schemas/k8s-views-global.json"
    )
    assert scenario.dashboard_title == "Kubernetes / Views / Global"
    assert {control.key for control in scenario.controls} == {
        "cluster",
        "job",
        "datasource",
        "resolution",
    }
    assert {combination.id for combination in scenario.combinations} == {
        "cluster-and-job",
        "cluster-and-second-job",
        "third-cluster-and-job",
    }


def test_k8s_manifest_has_no_uuid_panel_ids_or_embedded_queries() -> None:
    raw = yaml.safe_load(K8S_MANIFEST.read_text(encoding="utf-8"))
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
        unaffected = control["assertions"].get("unaffected_panels")
        if isinstance(unaffected, list):
            for panel_id in unaffected:
                assert not re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    panel_id,
                    flags=re.IGNORECASE,
                )


def test_k8s_native_mapping_and_controls(k8s_artifacts: Path) -> None:
    native_path = sorted((k8s_artifacts / "native").glob("*.native.json"))[0]
    artifact = json.loads(native_path.read_text(encoding="utf-8"))
    mapping = artifact["mapping"]

    assert mapping["mapped"] == 26
    assert mapping["unmapped"] == 0
    assert mapping["controls"] == 2
    assert [
        control["config"]["variable_name"]
        for control in artifact["payload"]["pinned_panels"]
        if control.get("type") == "esql_control"
    ] == ["cluster", "job"]


def test_k8s_cluster_affects_all_live_query_panels(k8s_artifacts: Path) -> None:
    queries = _native_queries(k8s_artifacts)
    assert len(queries) == len(_LIVE_QUERY_PANEL_TITLES)
    assert {title for title, _query in queries} == set(_LIVE_QUERY_PANEL_TITLES)
    cluster_panels = [
        title for title, query in queries if "cluster" in _VALUE_PARAM_TOKEN.findall(query)
    ]
    assert len(cluster_panels) == len(_LIVE_QUERY_PANEL_TITLES)

    contract = derive_panel_contract(
        [
            (f"panel-{index}", title, query)
            for index, (title, query) in enumerate(queries)
        ],
        control_keys=("cluster", "job"),
    )
    assert len(contract.all_query_panels) == len(_LIVE_QUERY_PANEL_TITLES)
    assert len(contract.by_control["cluster"]) == len(_LIVE_QUERY_PANEL_TITLES)


def test_k8s_job_and_cluster_partition_all_query_panels(k8s_artifacts: Path) -> None:
    job_titles, cluster_only_titles, _title_to_stable = _panel_dependency_sets(k8s_artifacts)
    assert job_titles
    assert cluster_only_titles
    assert len(job_titles) + len(cluster_only_titles) == len(_LIVE_QUERY_PANEL_TITLES)

    scenario = load_scenario(K8S_MANIFEST)
    job = next(control for control in scenario.controls if control.key == "job")
    assert job.assertions.affected_panels == "query_dependency"
    assert job.assertions.unaffected_panels == ()


def test_k8s_baseline_warning_panels_are_cataloged(k8s_artifacts: Path) -> None:
    report = json.loads((k8s_artifacts / "migration_report.json").read_text(encoding="utf-8"))
    dashboard = report["dashboards"][0]
    warned = {
        panel["title"]
        for panel in dashboard["panels"]
        if panel.get("status") == "migrated_with_warnings"
    }
    migrated = {
        panel["title"]
        for panel in dashboard["panels"]
        if panel.get("status") == "migrated"
    }
    assert set(_QUERY_PANEL_TITLES) <= (warned | migrated)
    assert not (set(_QUERY_PANEL_TITLES) & {
        panel["title"]
        for panel in dashboard["panels"]
        if panel.get("status") == "skipped"
    })


def test_k8s_non_query_row_panels_are_skipped(k8s_artifacts: Path) -> None:
    report = json.loads((k8s_artifacts / "migration_report.json").read_text(encoding="utf-8"))
    dashboard = report["dashboards"][0]
    skipped = {
        panel["title"]
        for panel in dashboard["panels"]
        if panel.get("status") == "skipped"
    }
    assert skipped == set(_NON_QUERY_ROW_TITLES)
    assert report["summary"]["total_panels"] == 30
    assert report["summary"]["skipped"] == 4


def test_k8s_execution_plan_covers_every_option_and_gaps(k8s_artifacts: Path) -> None:
    scenario = load_scenario(K8S_MANIFEST)
    contract = build_telemetry_contract(k8s_artifacts)
    stream = contract["streams"]["metrics-*"]
    documents = [doc for _, doc in generate_documents(contract)]
    discovered = {
        "cluster": sorted(
            {
                doc.get("cluster")
                for doc in documents
                if doc.get("cluster")
            }
        ),
        "job": sorted(
            {
                doc.get("job")
                for doc in documents
                if doc.get("job")
            }
        ),
    }
    plan = build_execution_plan(
        scenario,
        [
            DiscoveredControl("cluster", "cluster", tuple(discovered["cluster"])),
            DiscoveredControl("job", "job", tuple(discovered["job"])),
        ],
    )
    option_steps = [step for step in plan if step.kind == "option"]
    assert len(option_steps) == sum(len(values) for values in discovered.values())
    assert {step.control_key for step in plan if step.kind == "coverage_gap"} == {
        "datasource",
        "resolution",
    }
    combo_steps = [step for step in plan if step.kind == "combination"]
    assert {step.id for step in combo_steps} == {
        "cluster-and-job",
        "cluster-and-second-job",
        "third-cluster-and-job",
    }
    for step in combo_steps:
        assert step.selections["cluster"] in discovered["cluster"]
        assert step.selections["job"] in discovered["job"]
    assert stream["control_fields"]
    assert "cluster" in set(stream["control_fields"])
    assert "job" in set(stream["control_fields"])
    assert discovered["cluster"]
    assert discovered["job"]
    assert {"cluster_1", "cluster_2", "cluster_3"} <= set(discovered["cluster"])
    assert {"job_1", "job_2", "job_3"} <= set(discovered["job"])


def test_k8s_stable_panel_identities_are_deterministic(k8s_artifacts: Path) -> None:
    stable_panels = load_stable_panels_from_ir(
        k8s_artifacts,
        dashboard_title="Kubernetes / Views / Global",
    )
    assert len(stable_panels) == 26
    assert {title for _panel_id, title in stable_panels} == set(_QUERY_PANEL_TITLES)
    stable_by_title = {title: panel_id for panel_id, title in stable_panels}
    assert stable_by_title["Global CPU  Usage"] == "0.0"
    assert stable_by_title["CPU Usage"] == "0.5"
    assert stable_by_title["Network Received by namespace"] == "3.2"
    assert all("." in panel_id for panel_id, _title in stable_panels)


def test_k8s_gap_and_source_only_capabilities() -> None:
    scenario = load_scenario(K8S_MANIFEST)
    by_key = {control.key: control for control in scenario.controls}
    assert by_key["datasource"].capability is CapabilityCategory.SOURCE_ONLY
    assert by_key["resolution"].capability is CapabilityCategory.SOURCE_ONLY
    assert by_key["cluster"].assertions.affected_panels == "all_query_panels"
    assert by_key["cluster"].assertions.expect_data_change is True
    assert by_key["job"].assertions.affected_panels == "query_dependency"
    assert by_key["job"].assertions.unaffected_panels == ()
    assert by_key["job"].assertions.expect_data_change is False


def test_k8s_source_only_controls_do_not_bind_panel_queries(k8s_artifacts: Path) -> None:
    queries = _native_queries(k8s_artifacts)
    for _title, query in queries:
        assert "?datasource" not in query
        assert "?resolution" not in query


def test_k8s_native_control_value_queries(k8s_artifacts: Path) -> None:
    native_path = sorted((k8s_artifacts / "native").glob("*.native.json"))[0]
    artifact = json.loads(native_path.read_text(encoding="utf-8"))
    controls = {
        panel["config"]["variable_name"]: panel["config"]["esql_query"]
        for panel in artifact["payload"]["pinned_panels"]
        if panel.get("type") == "esql_control"
    }
    assert "BY cluster" in controls["cluster"] or "BY `cluster`" in controls["cluster"]
    assert "k8s.cluster.name" not in controls["cluster"]
    assert "BY job" in controls["job"] or "BY `job`" in controls["job"]
    assert "service.name" not in controls["job"]
    assert "?cluster" in controls["job"]


def test_k8s_cluster_and_job_query_bindings(k8s_artifacts: Path) -> None:
    queries = _native_queries(k8s_artifacts)
    cluster_query = next(query for title, query in queries if title == "Global CPU  Usage")
    job_query = next(
        query
        for title, query in queries
        if "job" in _VALUE_PARAM_TOKEN.findall(query)
    )
    # ``cluster`` is single-select (multi=False) -> scalar RLIKE binding.
    assert "RLIKE ?cluster" in cluster_query
    # ``job`` is multi=True in the source dashboard, so it binds through
    # MV_CONTAINS and the control stays multi-select. A scalar RLIKE position
    # could only ever hold one value and would force single-select.
    assert "MV_CONTAINS(?job" in job_query
    assert "RLIKE ?job" not in job_query
    assert "cluster" in cluster_query
    assert "job" in job_query


def test_esql_control_bound_field_extraction_for_k8s_controls(k8s_artifacts: Path) -> None:
    native_path = sorted((k8s_artifacts / "native").glob("*.native.json"))[0]
    artifact = json.loads(native_path.read_text(encoding="utf-8"))
    controls = {
        panel["config"]["variable_name"]: panel["config"]["esql_query"]
        for panel in artifact["payload"]["pinned_panels"]
        if panel.get("type") == "esql_control"
    }
    assert _extract_esql_values_bound_field(controls["cluster"]) in {
        "k8s.cluster.name",
        "cluster",
    }
    assert _extract_esql_values_bound_field(controls["job"]) in {
        "service.name",
        "job",
    }
