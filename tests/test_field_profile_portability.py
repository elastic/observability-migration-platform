# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the curated-pack field-profile portability linter."""

from __future__ import annotations

import sys
from pathlib import Path

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
