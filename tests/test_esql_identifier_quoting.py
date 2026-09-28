# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Every dotted path segment must be quoted independently, and only once.

Two defects made four of the seven Datadog field profiles emit ES|QL that
Elasticsearch refuses to parse. Both were found by running the full
migrate -> seed -> ``verifier.live_validate`` cycle per profile against a real
cluster, and both forms below were verified against Elasticsearch 9.6.0.

**Nested backticks** (``prometheus`` / ``prometheus_metrics`` /
``prometheus_native``). ``generate._quote_esql_identifier`` treated its input
as one token: its "already quoted" guard only matched a *fully* quoted string,
so a per-segment-quoted name got wrapped again::

    TO_STRING(`prometheus.labels.`client-id``)
    -> line 1:51: token recognition error at: '-'

The tag prefix (``prometheus.labels.``) is prepended to a name whose last
segment already needed quoting because of the hyphen. Correct is per-segment:
``prometheus.labels.`client-id```.

**Reserved-word segments** (``elastic_agent``). ``system.network.in.bytes``
contains ``in``, an ES|QL keyword::

    STATS v = SUM(system.network.in.bytes)
    -> line 1:49: no viable alternative at input 'SUM(system.network.in'

The Grafana adapter already carried ``_ESQL_RESERVED_IDENTIFIERS`` for exactly
this; the Datadog adapter's ``_SAFE_IDENTIFIER_RE`` accepted any ``\\w+``.
Probing all 77 ES|QL keywords against a live cluster showed 19 are rejected as
a bare dotted segment -- three of which (``nulls``, ``on``, ``with``) were
missing from the Grafana set too.
"""

from __future__ import annotations

import pytest

from observability_migration.targets.kibana.emit.esql_utils import (
    ESQL_RESERVED_IDENTIFIERS,
    esql_identifier,
)

# Verified rejected as a bare dotted segment against Elasticsearch 9.6.0.
EMPIRICALLY_REJECTED_SEGMENTS = [
    "and", "asc", "by", "desc", "false", "first", "in", "is", "last", "like",
    "not", "null", "nulls", "on", "or", "rlike", "true", "where", "with",
]


@pytest.mark.parametrize("word", EMPIRICALLY_REJECTED_SEGMENTS)
def test_every_rejected_word_is_in_the_reserved_set(word):
    assert word in ESQL_RESERVED_IDENTIFIERS


@pytest.mark.parametrize("word", EMPIRICALLY_REJECTED_SEGMENTS)
def test_a_reserved_segment_is_quoted_in_place(word):
    assert esql_identifier(f"system.network.{word}.bytes") == (
        f"system.network.`{word}`.bytes"
    )


def test_the_elastic_agent_field_that_failed_live():
    assert esql_identifier("system.network.in.bytes") == "system.network.`in`.bytes"


# --- idempotence: quoting an already-quoted name must not nest -----------


def test_quoting_is_idempotent_for_a_per_segment_quoted_name():
    once = esql_identifier("prometheus.labels.client-id")
    assert once == "prometheus.labels.`client-id`"
    assert esql_identifier(once) == once, "second pass must not add backticks"


def test_quoting_is_idempotent_for_a_reserved_segment():
    once = esql_identifier("system.network.in.bytes")
    assert esql_identifier(once) == once


def test_the_prometheus_profile_field_that_failed_live():
    """The exact emitted form, and that re-quoting it is a no-op."""
    for prefix in ("prometheus.labels", "labels"):
        expected = f"{prefix}.`client-id`"
        assert esql_identifier(f"{prefix}.client-id") == expected
        assert esql_identifier(expected) == expected


# --- ordinary names are untouched ----------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "host",
        "host.name",
        "system.cpu.user",
        "prometheus.labels.instance",
        "service.name",
        "_tsid",
        "prometheus.metrics.http_requests_total",
    ],
)
def test_safe_names_are_not_quoted(name):
    assert esql_identifier(name) == name


@pytest.mark.parametrize(
    "name,expected",
    [
        ("labels.5xx", "labels.`5xx`"),
        ("a.b-c", "a.`b-c`"),
        ("a.with space", "a.`with space`"),
        ("`already quoted`", "`already quoted`"),
    ],
)
def test_unsafe_segments_are_quoted(name, expected):
    assert esql_identifier(name) == expected


def test_an_embedded_backtick_is_escaped_by_doubling():
    assert esql_identifier("a.we`ird") == "a.`we``ird`"


def test_empty_input_is_passed_through():
    assert esql_identifier("") == ""


def test_at_prefixed_segments_stay_quoted():
    """Both forms are valid ES|QL; staying quoted matches prior output."""
    assert esql_identifier("@pspReference") == "`@pspReference`"
    assert esql_identifier(esql_identifier("@pspReference")) == "`@pspReference`"
