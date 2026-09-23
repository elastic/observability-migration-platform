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


# ---------------------------------------------------------------------------
# The profile list must come from the registry, not a hand-kept copy
# ---------------------------------------------------------------------------

def test_datadog_coverage_includes_every_built_in_profile():
    """A hand-maintained list silently omitted ``elastic_agent`` and
    ``default`` while carrying both spellings of the Prometheus layout, so the
    fail-closed rule check never saw the profiles that had no rules."""
    from observability_migration.adapters.source.datadog.field_map import (
        BUILTIN_PROFILES,
    )
    from scripts.run_cross_profile_corpus import PROFILES_BY_SOURCE

    missing = set(BUILTIN_PROFILES) - set(PROFILES_BY_SOURCE["datadog"])
    assert not missing, f"profiles escaping the cross-profile gate: {sorted(missing)}"


def test_grafana_coverage_includes_every_selectable_profile():
    from observability_migration.adapters.source.grafana.cli import (
        _GRAFANA_FIELD_PROFILES,
    )
    from scripts.run_cross_profile_corpus import PROFILES_BY_SOURCE

    # ``auto`` resolves to a concrete profile at migrate time and is checked
    # through whichever that is.
    selectable = set(_GRAFANA_FIELD_PROFILES) - {"auto"}
    missing = selectable - set(PROFILES_BY_SOURCE["grafana"])
    assert not missing, f"profiles escaping the cross-profile gate: {sorted(missing)}"


def test_every_covered_profile_has_leakage_rules():
    """The gate fails closed on a profile with no rules, so a covered profile
    without them would block the run rather than pass it vacuously -- either
    way the rules have to exist."""
    from verifier.profile_leakage import profiles_without_rules

    from scripts.run_cross_profile_corpus import PROFILES_BY_SOURCE

    for source, profiles in PROFILES_BY_SOURCE.items():
        unruled = profiles_without_rules(profiles)
        assert not unruled, f"{source}: no leakage rules for {unruled}"


def test_elastic_agent_rejects_foreign_namespaces():
    """It emits the native/ECS spellings (``host.name``), so a Prometheus or
    dotted-metric namespace in its output is a leak."""
    from verifier.profile_leakage import check_profile_leakage

    for query in (
        'FROM metrics-* | WHERE labels.pod == "x"',
        "FROM metrics-* | STATS AVG(metrics.system_cpu)",
        'FROM metrics-* | WHERE prometheus.labels.job == "x"',
    ):
        assert check_profile_leakage(query, "elastic_agent"), query


def test_default_profile_rejects_foreign_namespaces():
    from verifier.profile_leakage import check_profile_leakage

    assert check_profile_leakage('FROM metrics-* | WHERE labels.pod == "x"', "default")


def test_the_native_profiles_accept_their_own_spellings():
    """The rules must not fire on the layout the profile actually emits."""
    from verifier.profile_leakage import check_profile_leakage

    query = 'FROM metrics-* | WHERE host.name == "h" AND deployment.environment == "prod"'
    for profile in ("elastic_agent", "default", "otel"):
        assert not check_profile_leakage(query, profile), profile
