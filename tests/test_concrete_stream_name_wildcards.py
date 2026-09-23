# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A data stream name can never contain a wildcard.

``concrete_stream_name`` turns a profile's index *pattern* into the concrete
data stream the seeder creates. The ``-*`` suffix branch fired before the
general wildcard branch, so a pattern with an **embedded** wildcard kept it:

    metrics-*.prometheus-*   ->   metrics-*.prometheus-default

That is the ``prometheus_native`` profile's metric index. Elasticsearch will
not create a data stream with a ``*`` in its name, so ``seed-sample-data``
could not seed that profile at all — and ``verifier.live_validate`` then
reported 246 of 261 queries as ``data_gap`` (well-formed queries, no index),
which reads as "telemetry not seeded yet" rather than as a bug.

Found by running migrate -> seed -> live_validate for all seven Datadog field
profiles: six seeded cleanly, ``prometheus_native`` was the only one that
could not.
"""

from __future__ import annotations

import fnmatch

import pytest

from observability_migration.core.telemetry_data import concrete_stream_name


@pytest.mark.parametrize(
    "pattern,expected",
    [
        # The profile that failed.
        ("metrics-*.prometheus-*", "metrics-generic.prometheus-default"),
        ("logs-*.otel-*", "logs-generic.otel-default"),
        # Unchanged: the shapes that already worked.
        ("metrics-*", "metrics-generic-default"),
        ("logs-*", "logs-generic-default"),
        ("traces-*", "traces-generic-default"),
        ("metrics-prometheus-*", "metrics-prometheus-default"),
        ("metrics-generic-default", "metrics-generic-default"),
    ],
)
def test_pattern_resolves_to_a_wildcard_free_stream_name(pattern, expected):
    assert concrete_stream_name(pattern, {}) == expected


@pytest.mark.parametrize(
    "pattern",
    [
        "metrics-*",
        "metrics-*.prometheus-*",
        "metrics-prometheus-*",
        "logs-*",
        "logs-*.otel-*",
        "metrics-*.*-*",
        "metrics-?-*",
    ],
)
def test_no_pattern_can_produce_a_wildcard_in_the_name(pattern):
    """The invariant, independent of the expected strings above."""
    name = concrete_stream_name(pattern, {})
    assert "*" not in name, name
    assert "?" not in name, name


def test_an_explicit_dataset_requirement_still_wins():
    stream = {"required_values": {"data_stream.dataset": ["redis"]}}
    assert concrete_stream_name("metrics-*.prometheus-*", stream).startswith("metrics-redis")


# ---------------------------------------------------------------------------
# The generated name must be matched by the pattern it came from
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pattern",
    [
        "metrics-*",
        "logs-*",
        "traces-*",
        "metrics-*.prometheus-*",
        "metrics-?-*",
        "metrics-ab?-*",
        "metrics-prom?-*",
        "metrics-a?c-*",
        "logs-?*",
        "metrics-**",
        "metrics-??-*",
    ],
)
def test_the_generated_stream_is_matched_by_its_own_pattern(pattern):
    """The whole point of the name is that the dashboard's index pattern finds
    it. ``?`` matches exactly one character, so substituting a multi-character
    run for it produced a stream the pattern could not match -- seeded data
    that every panel reading that pattern would miss.
    """
    name = concrete_stream_name(pattern, {})
    assert fnmatch.fnmatchcase(name, pattern), f"{name!r} is not matched by {pattern!r}"


@pytest.mark.parametrize(
    "pattern",
    ["metrics-*", "metrics-?-*", "metrics-*.prometheus-*", "logs-?*"],
)
def test_the_generated_stream_carries_no_wildcard(pattern):
    """Elasticsearch refuses to create a data stream whose name contains one."""
    name = concrete_stream_name(pattern, {})
    assert "*" not in name and "?" not in name, name
