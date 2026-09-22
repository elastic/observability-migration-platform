# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""The leakage gate must not pass a profile it has no rules for.

``scripts/run_cross_profile_corpus.py`` hardcoded ``--source grafana``, so the
cross-profile leakage gate had never run against Datadog. Parameterising it by
source exposes a second problem: ``check_profile_leakage`` returns ``[]`` for
an unknown profile, so Datadog's ``prometheus`` (the Metricbeat layout, named
``prometheus_metrics`` on the Grafana side) and ``elastic_agent`` would have
been checked against *no rules at all* and reported a clean pass.

A gate that cannot fail is worse than no gate: it reads as coverage. Datadog's
``prometheus`` is the same physical layout as ``prometheus_metrics``, so it
aliases onto those rules; any profile with genuinely no rules must be visible
rather than silently green.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier.profile_leakage import (  # noqa: E402
    check_profile_leakage,
    profiles_without_rules,
)

sys.path.insert(0, str(ROOT / "scripts"))


def test_datadog_prometheus_aliases_onto_the_metricbeat_rules():
    """`prometheus` (Datadog) == `prometheus_metrics` (Grafana) layout."""
    leaked = "FROM metrics-* | STATS v = AVG(labels.pod)"
    assert check_profile_leakage(leaked, "prometheus")
    assert check_profile_leakage(leaked, "prometheus") == check_profile_leakage(
        leaked, "prometheus_metrics"
    )


def test_a_clean_query_does_not_trip_the_aliased_rules():
    clean = "FROM metrics-prometheus-* | STATS v = AVG(prometheus.metrics.up) BY prometheus.labels.job"
    assert check_profile_leakage(clean, "prometheus") == []


@pytest.mark.parametrize("profile", ["otel", "passthrough", "prometheus_native"])
def test_shared_profiles_still_have_rules(profile):
    assert profile not in profiles_without_rules([profile])


def test_profiles_without_rules_is_reported_not_hidden():
    """The helper the gate uses to refuse a vacuous pass."""
    assert profiles_without_rules(["otel", "made_up_profile"]) == ["made_up_profile"]
    assert profiles_without_rules(["auto"]) == [], "auto is resolved at migrate time"


def test_every_datadog_profile_the_gate_runs_has_rules_or_is_declared():
    """Guards the script's own profile list against silent no-rule entries."""
    from run_cross_profile_corpus import PROFILES_BY_SOURCE

    for source, profiles in PROFILES_BY_SOURCE.items():
        missing = profiles_without_rules(profiles)
        assert not missing, f"{source}: no leakage rules for {missing}"
