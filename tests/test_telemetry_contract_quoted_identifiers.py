# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A backtick-quoted identifier segment belongs to its dotted field path.

ES|QL requires a backtick around any identifier segment that does not start
with a letter or underscore, so the translator correctly emits
``consul.raft.leader.lastContact.`95percentile```. The contract's field
extractor matched ``[A-Za-z_@][\\w.@-]*`` first, which greedily swallowed the
trailing dot and stopped at the backtick, then matched the quoted segment
separately. One real field therefore became two wrong ones:

* ``consul.raft.leader.lastContact.`` — a name with a trailing dot
* ``95percentile`` — an orphaned segment

Consequences, all observed on the in-repo Datadog corpus:

1. ``plan_index_template`` expands the trailing-dot name into an object path
   that also has to be a ``double`` leaf, so Elasticsearch rejects the whole
   template (``can't merge a non object mapping [...] with an object
   mapping``). ``obs-migrate seed-sample-data`` then fails for the *entire*
   stream, which blocks the documented live/render validation path.
2. The real field is absent from the contract, so the panel needing it would
   have no data even if seeding succeeded.
3. ``target_readiness_contract.json`` reports field names to the operator that
   do not exist in either the source or the target.

Affected fields in the in-repo corpus: ``system.load.1`` / ``.15``,
``haproxy.backend.response.5xx``, ``haproxy.frontend.response.5xx``,
``redis.slowlog.micros.95percentile``,
``consul.raft.leader.lastContact.95percentile``.
"""

from __future__ import annotations

import pytest

from observability_migration.core.telemetry_contract import (
    _extract_query_field_candidates,
    _normalize_field,
)

# --- the extractor --------------------------------------------------------


def test_quoted_trailing_segment_stays_part_of_its_path():
    fields = _extract_query_field_candidates(
        "STATS a = SUM(consul.raft.leader.lastContact.`95percentile`) BY host"
    )
    assert "consul.raft.leader.lastContact.95percentile" in fields
    assert "consul.raft.leader.lastContact." not in fields
    assert "95percentile" not in fields


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("SUM(system.load.`1`)", "system.load.1"),
        ("SUM(system.load.`15`)", "system.load.15"),
        ("SUM(haproxy.backend.response.`5xx`)", "haproxy.backend.response.5xx"),
        ("SUM(haproxy.frontend.response.`1xx`)", "haproxy.frontend.response.1xx"),
        ("SUM(redis.slowlog.micros.`95percentile`)", "redis.slowlog.micros.95percentile"),
    ],
)
def test_every_corpus_shape_round_trips(expression, expected):
    assert expected in _extract_query_field_candidates(expression)


def test_no_extracted_field_has_an_empty_path_segment():
    """The invariant: a field name can never start/end with a dot."""
    expression = (
        "STATS a = SUM(system.load.`1`), b = SUM(haproxy.backend.response.`5xx`), "
        "c = AVG(redis.slowlog.micros.`95percentile`) BY host, `weird name`"
    )
    for field in _extract_query_field_candidates(expression):
        assert not field.startswith("."), field
        assert not field.endswith("."), field
        assert ".." not in field, field


def test_quoted_middle_segment_stays_part_of_its_path():
    fields = _extract_query_field_candidates("SUM(metrics.`5xx`.count)")
    assert "metrics.5xx.count" in fields


def test_a_fully_quoted_name_containing_dots_is_one_field():
    """Backticks around the whole name mean the dots are part of the name."""
    fields = _extract_query_field_candidates("SUM(`labels.weird.name`)")
    assert "labels.weird.name" in fields


def test_unquoted_dotted_paths_are_unchanged():
    fields = _extract_query_field_candidates("SUM(system.cpu.user) BY host.name")
    assert "system.cpu.user" in fields
    assert "host.name" in fields


def test_quoted_segment_with_keyword_subfield_still_strips_keyword():
    fields = _extract_query_field_candidates("BY haproxy.backend.`5xx`.keyword")
    assert "haproxy.backend.5xx" in fields


# --- the normalizer -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("`95percentile`", "95percentile"),
        ("consul.raft.`95percentile`", "consul.raft.95percentile"),
        ("`labels.weird`", "labels.weird"),
        ("plain.field", "plain.field"),
        ("plain.field.keyword", "plain.field"),
        ("`quoted`.keyword", "quoted"),
    ],
)
def test_normalize_field_handles_quoted_segments(raw, expected):
    assert _normalize_field(raw) == expected


def test_normalize_field_unescapes_doubled_backticks():
    assert _normalize_field("`we``ird`") == "we`ird"
