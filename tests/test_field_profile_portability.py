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
