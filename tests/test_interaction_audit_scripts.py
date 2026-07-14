# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from observability_migration.targets.kibana import (
    interaction_audit_local as local,
)
from observability_migration.targets.kibana.interaction_runner import (
    load_panel_contract,
)

ROOT = Path(__file__).resolve().parents[1]
LOCAL_RUNNER = ROOT / "scripts" / "run_interaction_audit_local.sh"
RENDER_RUNNER = ROOT / "scripts" / "run_render_audit_local.sh"


def test_local_runner_seeds_before_schema_aware_final_migration():
    script = LOCAL_RUNNER.read_text(encoding="utf-8")

    bootstrap = script.index('--output-dir "$bootstrap_root"')
    final = script.index('--output-dir "$final_root"')
    first_seed = script.index("setup_telemetry_data.py")
    live_validate = script.index("validate-final")
    browser = script.index("scripts/run_interaction_audit.py")

    assert bootstrap < final < first_seed < live_validate < browser
    assert "--upload" in script
    assert "--ensure-data-views" in script
    assert "--panel-contract" in script
    assert "--control-schema" in script


def test_local_runner_defaults_and_stack_ownership_are_explicit():
    script = LOCAL_RUNNER.read_text(encoding="utf-8")

    assert 'STACK_VERSION="${STACK_VERSION:-9.5.0-SNAPSHOT}"' in script
    assert (
        'SCENARIOS="${SCENARIOS:-synthetic-controls,redis-11835,k8s-views-global}"'
        in script
    )
    assert 'ES_URL="${ES_URL:-http://localhost:9200}"' in script
    assert 'KIBANA_URL="${KIBANA_URL:-http://localhost:5601}"' in script
    assert 'ARTIFACT_ROOT="${ARTIFACT_ROOT:-$PROJECT_ROOT/interaction-audit-artifacts}"' in script
    assert "KEEP_WORK" in script
    assert "docker compose" not in script
    assert "docker down" not in script
    assert 'rm -rf "$ARTIFACT_ROOT"' not in script


def test_local_runner_rejects_unknown_scenarios_and_old_stack_versions():
    assert local.parse_scenario_selection("synthetic-controls") == ("synthetic-controls",)
    with pytest.raises(ValueError, match="unknown scenario"):
        local.parse_scenario_selection("unknown")
    with pytest.raises(ValueError, match="below required"):
        local.require_stack_version("9.4.9")
    local.require_stack_version("9.5.0-SNAPSHOT")


def test_render_runner_points_to_interaction_target_without_running_it():
    script = RENDER_RUNNER.read_text(encoding="utf-8")

    assert "make interaction-audit-local" in script
    assert "run_interaction_audit.py" not in script


def test_interaction_shell_scripts_parse():
    completed = subprocess.run(
        ["bash", "-n", str(LOCAL_RUNNER), str(RENDER_RUNNER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_exact_title_lookup_filters_search_overmatches(monkeypatch):
    monkeypatch.setattr(
        local,
        "_request_json",
        lambda *_args, **_kwargs: {
            "saved_objects": [
                {"id": "exact", "attributes": {"title": "Dashboard A"}},
                {"id": "suffix", "attributes": {"title": "Dashboard A copy"}},
            ]
        },
    )

    assert local.find_dashboard_ids_by_title("http://kibana", "Dashboard A") == ["exact"]


def test_runtime_panel_extraction_handles_direct_and_layer_data_sources():
    payload = {
        "id": "dashboard-id",
        "data": {
            "panels": [
                {
                    "id": "metric-runtime",
                    "config": {
                        "title": "Metric",
                        "data_source": {"type": "esql", "query": "FROM metrics-* | WHERE env == ?env"},
                    },
                },
                {
                    "id": "xy-runtime",
                    "config": {
                        "title": "XY",
                        "type": "xy",
                        "layers": [
                            {
                                "data_source": {
                                    "type": "esql",
                                    "query": "FROM metrics-* | STATS value=AVG(x) BY key=??grouping",
                                }
                            }
                        ],
                    },
                },
                {
                    "id": "markdown-runtime",
                    "type": "markdown",
                    "config": {"title": "Reference", "content": "no query"},
                },
            ]
        },
    }

    panels = local.runtime_query_panels_from_payload(payload)
    assert panels == [
        ("metric-runtime", "Metric", "FROM metrics-* | WHERE env == ?env"),
        (
            "xy-runtime",
            "XY",
            "FROM metrics-* | STATS value=AVG(x) BY key=??grouping",
        ),
    ]
    contract = local.derive_panel_contract(panels, control_keys=("env", "grouping"))
    assert contract.all_query_panels == ("metric-runtime", "xy-runtime")
    assert contract.by_control == {
        "env": ("metric-runtime",),
        "grouping": ("xy-runtime",),
    }


def test_query_dependency_extraction_distinguishes_value_and_identifier_tokens():
    values, identifiers = local.query_control_dependencies(
        "FROM metrics-* | WHERE env == ?environment "
        "| STATS value=??aggregate(metric) BY key=??grouping"
    )

    assert values == ("environment",)
    assert identifiers == ("aggregate", "grouping")


def test_panel_contract_maps_stable_panel_ids_without_rewriting_manifest(tmp_path):
    runtime_ids = {
        "stable-metric": "0f44de29-a498-4dac-bc4f-217a86efdf2c",
        "stable-reference": "ee765efe-981e-4779-bdb2-e48ab9f41e92",
    }
    contract = local.PanelContract(
        all_query_panels=(runtime_ids["stable-metric"],),
        by_control={"environment": (runtime_ids["stable-metric"],)},
        panel_aliases=runtime_ids,
        panel_titles={
            runtime_ids["stable-metric"]: "Metric",
            runtime_ids["stable-reference"]: "Reference",
        },
    )
    path = tmp_path / "panel-contract.json"

    local.write_panel_contract(path, contract)
    loaded = load_panel_contract(path)

    assert loaded.remap_panel_ids(
        (runtime_ids["stable-metric"],),
        {"Metric": "fresh-runtime-id"},
    ) == ("fresh-runtime-id",)
    assert loaded.resolve_panel_ids(("stable-metric", "stable-reference")) == (
        runtime_ids["stable-metric"],
        runtime_ids["stable-reference"],
    )
    assert "stable-metric" not in loaded.all_query_panels


def test_native_artifact_counts_match_payload_and_declared_expectations(tmp_path):
    artifact_root = tmp_path / "dashboards"
    native_dir = artifact_root / "native"
    native_dir.mkdir(parents=True)
    artifact = {
        "kind": "kibana-native-dashboard",
        "version": 1,
        "title": "Synthetic",
        "payload": {
            "title": "Synthetic",
            "panels": [{"id": f"panel-{index}"} for index in range(8)],
            "pinned_panels": [{"id": f"control-{index}"} for index in range(7)],
        },
        "mapping": {"mapped": 8, "unmapped": 0, "controls": 7},
    }
    (native_dir / "synthetic.native.json").write_text(
        json.dumps(artifact),
        encoding="utf-8",
    )

    counts = local.assert_native_mapping(
        artifact_root,
        expected_panels=8,
        expected_controls=7,
        dashboard_title="Synthetic",
    )

    assert counts["mapped"] == 8
    assert counts["unmapped"] == 0
    assert counts["controls"] == 7


def test_runtime_contract_preserves_query_panel_denominator():
    panels = [
        ("panel-a", "A", "FROM metrics-* | WHERE namespace == ?namespace"),
        ("panel-b", "B", "FROM metrics-* | WHERE namespace == ?namespace"),
        ("panel-c", "C", "FROM metrics-*"),
    ]

    contract = local.derive_panel_contract(panels, control_keys=("namespace",))

    assert contract.all_query_panels == ("panel-a", "panel-b", "panel-c")
    assert contract.by_control["namespace"] == ("panel-a", "panel-b")
