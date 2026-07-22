# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression checks for user-facing metric_map examples."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from observability_migration.adapters.source.datadog.field_map import load_profile
from observability_migration.adapters.source.grafana.rules import load_rule_pack_files

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
        "### Reusing existing OTEL metrics with `metric_map`",
        "query:",
        "metric_map:",
        "container_memory_working_set_bytes: container.memory.working_set",
        "container_network_receive_bytes_total:",
        "attribute_filter: { network.direction: receive }",
        "--translation-mode esql",
        "--rules-file ./my-grafana-otel-map.yaml",
        "### Datadog metric-map profile override example",
        "metric_index: metrics-otel-*",
        "system.cpu.user: system.cpu.user.pct",
        "system.net.bytes_rcvd:",
        ".venv/bin/obs-migrate migrate",
        "--source datadog",
        "--field-profile ./my-dd-otel-profile.yaml",
        "--data-view metrics-otel-*",
        "Class-2",
        "target_readiness_contract.json",
    ]

    for fragment in expected_fragments:
        assert fragment in text

    metric_map_section = text.split("### Reusing existing OTEL metrics with `metric_map`", 1)[1]
    metric_map_section = metric_map_section.split("`--logs-index` is the log analog", 1)[0]
    assert "datadog-migrate" not in metric_map_section
    assert "grafana-migrate" not in metric_map_section


def test_command_contract_metric_map_yaml_examples_load():
    text = _command_contract()
    grafana_yaml = _yaml_block_after(text, "#### Grafana existing-OTEL example")
    datadog_yaml = _yaml_block_after(text, "#### Datadog metric-map profile override example")

    with tempfile.TemporaryDirectory() as tmp:
        grafana_path = Path(tmp) / "my-grafana-otel-map.yaml"
        datadog_path = Path(tmp) / "my-dd-otel-profile.yaml"
        grafana_path.write_text(grafana_yaml, encoding="utf-8")
        datadog_path.write_text(datadog_yaml, encoding="utf-8")

        pack = load_rule_pack_files([str(grafana_path)])
        self_cpu = pack.metric_map["container_memory_working_set_bytes"]
        self_network = pack.metric_map["container_network_receive_bytes_total"]
        assert self_cpu.target == "container.memory.working_set"
        assert self_network.target == "k8s.pod.network.io"
        assert self_network.attribute_filter == {"network.direction": "receive"}

        profile = load_profile(str(datadog_path))
        assert profile.metric_index == "metrics-otel-*"
        assert profile.map_metric("system.cpu.user") == "system.cpu.user.pct"
        # Class-2 example remains a v1 gap, not a silent target rename.
        assert profile.map_metric("system.net.bytes_rcvd") == "system_net_bytes_rcvd"
        assert profile.metric_map_gaps()


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
