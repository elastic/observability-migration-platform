# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression checks for user-facing metric_map examples."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from observability_migration.adapters.source.datadog.cli import _load_configured_field_map
from observability_migration.adapters.source.grafana.cli import _load_configured_rule_pack
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.core.metric_mapping import load_metric_map_files

DOC = Path("docs/command-contract.md")


def _command_contract() -> str:
    return DOC.read_text(encoding="utf-8")


def _yaml_block_after(text: str, heading: str) -> str:
    start = text.index(heading)
    match = re.search(r"```yaml\n(.*?)\n```", text[start:], flags=re.DOTALL)
    assert match is not None
    return match.group(1)


def test_command_contract_has_metric_map_operator_examples():
    text = _command_contract()

    expected_fragments = [
        "### Reusing existing OTEL metrics with `--metric-map-file`",
        "metric_map:",
        "container_memory_working_set_bytes: container.memory.working_set",
        "container_network_receive_bytes_total:",
        "attribute_filter: { network.direction: receive }",
        "--metric-map-file ./my-otel-metric-map.yaml",
        "#### Grafana existing-OTEL example",
        "#### Datadog existing-OTEL example",
        "system.cpu.user: system.cpu.user.pct",
        "system.net.bytes_rcvd:",
        ".venv/bin/obs-migrate migrate",
        "--source grafana",
        "--source datadog",
        "--field-profile otel",
        "--data-view metrics-otel-*",
        "--esql-index metrics-otel-*",
        "Class-2",
        "target_readiness_contract.json",
        "Advanced alternatives",
    ]

    for fragment in expected_fragments:
        assert fragment in text

    metric_map_section = text.split(
        "### Reusing existing OTEL metrics with `--metric-map-file`", 1
    )[1]
    metric_map_section = metric_map_section.split("`--logs-index` is the log analog", 1)[0]
    assert "datadog-migrate" not in metric_map_section
    assert "grafana-migrate" not in metric_map_section
    assert "--rules-file ./my-grafana-otel-map.yaml" not in metric_map_section
    assert "--field-profile ./my-dd-otel-profile.yaml" not in metric_map_section
    # Grafana and Datadog examples should not require an explicit translation-mode
    # for the metric-map path; --metric-map-file auto-selects ES|QL on Grafana.
    assert "--translation-mode esql" not in metric_map_section


def test_command_contract_metric_map_yaml_examples_load_and_apply():
    text = _command_contract()
    metric_map_yaml = _yaml_block_after(text, "Example `my-otel-metric-map.yaml`")

    with tempfile.TemporaryDirectory() as tmp:
        metric_map_path = Path(tmp) / "my-otel-metric-map.yaml"
        metric_map_path.write_text(metric_map_yaml, encoding="utf-8")

        entries = load_metric_map_files([str(metric_map_path)])
        assert entries["container_memory_working_set_bytes"].target == (
            "container.memory.working_set"
        )
        assert entries["container_network_receive_bytes_total"].target == "k8s.pod.network.io"
        assert entries["container_network_receive_bytes_total"].attribute_filter == {
            "network.direction": "receive"
        }
        assert entries["system.cpu.user"].target == "system.cpu.user.pct"
        assert entries["system.net.bytes_rcvd"].target == "system.network.in.bytes"

        grafana_pack = _load_configured_rule_pack(
            argparse.Namespace(
                rules_file=[],
                metric_map_file=[str(metric_map_path)],
                logs_index="",
                dataset_filter="",
                logs_dataset_filter="",
                plugin=[],
            )
        )
        resolver = SchemaResolver(grafana_pack, field_profile="otel")
        assert resolver.resolve_metric_field("container_memory_working_set_bytes") == (
            "container.memory.working_set"
        )
        # Class-2 remains a gap: source metric is not silently renamed.
        assert resolver.resolve_metric_field("container_network_receive_bytes_total") == (
            "container_network_receive_bytes_total"
        )
        assert resolver.metric_map_gaps()

        datadog_map = _load_configured_field_map(
            argparse.Namespace(
                field_profile="otel",
                metric_map_file=[str(metric_map_path)],
                data_view="metrics-otel-*",
                logs_index="",
                dataset_filter="",
                logs_dataset_filter="",
            )
        )
        assert datadog_map.map_metric("system.cpu.user") == "system.cpu.user.pct"
        assert datadog_map.map_metric("system.net.bytes_rcvd") == "system_net_bytes_rcvd"
        assert datadog_map.metric_map_gaps()


def test_command_contract_examples_prefer_unified_cli():
    text = _command_contract()
    bash_examples = "\n".join(
        match.group(1)
        for match in re.finditer(r"```bash\n(.*?)\n```", text, flags=re.DOTALL)
    )

    dedicated_example_spellings = [
        ".venv/bin/grafana-migrate",
        ".venv/bin/datadog-migrate",
        "python -m observability_migration.adapters.source.grafana.cli",
        "python -m observability_migration.adapters.source.datadog.cli",
    ]

    for spelling in dedicated_example_spellings:
        assert spelling not in bash_examples


def test_obs_migrate_forwards_metric_map_file_to_both_sources():
    from observability_migration.app import cli as app_cli

    captured: dict[str, list[str]] = {}

    def _fake_grafana_main():
        captured["grafana"] = list(sys.argv)

    def _fake_datadog_main():
        captured["datadog"] = list(sys.argv)

    base = dict(
        input_mode="files",
        input_dir="/tmp/in",
        output_dir="/tmp/out",
        data_view="metrics-*",
        esql_index="",
        logs_index="",
        assets="dashboards",
        field_profile="otel",
        metric_map_file=["/tmp/map.yaml"],
        rules_file=[],
        plugin=[],
        polish_metadata=False,
        validate=False,
        upload=False,
        compile=False,
        legacy_import=False,
        use_dashboards_api=False,
        preflight=False,
        dataset_filter="",
        logs_dataset_filter="",
        es_url="",
        es_api_key="",
        kibana_url="",
        kibana_api_key="",
        space_id="",
        fetch_alerts=False,
        create_alert_rules=False,
        no_draft_alert_rules=False,
        alert_uids="",
        alert_folder="",
        translation_mode="auto",
        smoke=False,
        browser_audit=False,
        capture_screenshots=False,
        smoke_output="",
        smoke_report="",
        smoke_timeout=30,
        chrome_binary="",
        grafana_url="",
        grafana_user="",
        grafana_pass="",
        grafana_token="",
        select_folder=[],
        select_tag=[],
        select_datasource=[],
        select_team=[],
        select_updated_after="",
        select_updated_before="",
        select_starred=False,
        ca_cert="",
        insecure=False,
        monitor_ids="",
        monitor_query="",
        env_file="",
        dashboard_ids="",
        source_execution=False,
    )

    with patch(
        "observability_migration.adapters.source.grafana.cli.main",
        side_effect=_fake_grafana_main,
    ):
        app_cli._run_grafana_migration(SimpleNamespace(**base))

    with patch(
        "observability_migration.adapters.source.datadog.cli.main",
        side_effect=_fake_datadog_main,
    ):
        app_cli._run_datadog_migration(SimpleNamespace(**base))

    assert "--metric-map-file" in captured["grafana"]
    assert "/tmp/map.yaml" in captured["grafana"]
    assert "--metric-map-file" in captured["datadog"]
    assert "/tmp/map.yaml" in captured["datadog"]
