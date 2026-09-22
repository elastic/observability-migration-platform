# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline coverage for the live filter-semantics gate's driver.

The gate itself needs a cluster (``parity-rig/verifier/filter_semantics_gate``),
but its driver -- does a wrong row count or a rejected query become a finding?
-- must be testable in ``make test`` with no docker, the way
``live_validate``'s classifier is. The HTTP layer is injected.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier.filter_semantics_gate import (  # noqa: E402
    FIELD_TYPES,
    SHAPES,
    run_gate,
)


def _fake_request(row_counts):
    """Return a request fn answering ``_query`` from ``row_counts`` by predicate."""

    def request(es_url, method, path, body=None, ndjson=False):
        if path != "/_query":
            return 200, {}
        predicate = body["query"].split("| WHERE ", 1)[1].split(" | STATS", 1)[0]
        outcome = row_counts(predicate)
        if isinstance(outcome, str):
            return 400, json.dumps(
                {"error": {"root_cause": [{"reason": outcome}]}}
            )
        return 200, {"values": [[outcome]]}

    return request


def _expected_by_shape():
    return {shape: len(expected) for shape, _kwargs, expected in SHAPES}


def test_gate_passes_when_every_shape_returns_its_expected_count():
    expected = _expected_by_shape()

    def counts(predicate):
        # Answer each predicate with whatever its shape expects. The driver
        # walks shapes in order per (profile, field_type), so recover the
        # expectation from the call sequence instead of parsing ES|QL.
        return counts.sequence.pop(0)

    per_pass = [expected[shape] for shape, _k, _e in SHAPES]
    counts.sequence = per_pass * (len(FIELD_TYPES) * 7)

    result = run_gate("http://es.invalid", request=_fake_request(counts))
    assert result.ok, [f.render() for f in result.findings]
    assert result.executed == len(SHAPES) * len(FIELD_TYPES) * 7


def test_a_wrong_row_count_becomes_a_finding():
    """The silent class: valid ES|QL that selects the wrong rows."""

    def counts(predicate):
        return 0  # everything matches nothing

    result = run_gate(
        "http://es.invalid", profiles=["passthrough"], request=_fake_request(counts)
    )
    assert not result.ok
    # Only shapes that legitimately expect 0 rows would pass; none do.
    assert len(result.findings) == len(SHAPES) * len(FIELD_TYPES)
    assert all(f.actual == 0 for f in result.findings)


def test_a_rejected_query_becomes_a_finding_carrying_the_reason():
    """The loud class: the type error this gate was built for."""
    reason = (
        'first argument of [http.response.status_code == "500"] is [numeric] '
        "so second argument must also be [numeric] but was [keyword]"
    )

    def counts(predicate):
        return reason

    result = run_gate(
        "http://es.invalid", profiles=["otel"], request=_fake_request(counts)
    )
    assert not result.ok
    assert all(f.actual == "query rejected" for f in result.findings)
    assert all("is [numeric]" in f.detail for f in result.findings)


def test_findings_render_the_predicate_for_triage():
    def counts(predicate):
        return 99

    result = run_gate(
        "http://es.invalid", profiles=["otel"], request=_fake_request(counts)
    )
    rendered = result.findings[0].render()
    assert "otel" in rendered
    assert "got 99" in rendered
    assert "http.response.status_code" in rendered


def test_every_shape_declares_a_reachable_expectation():
    """Guard the gate's own table: expectations must be a subset of the seed."""
    from verifier.filter_semantics_gate import SEEDED

    for shape, _kwargs, expected in SHAPES:
        assert expected, f"{shape} expects no rows"
        assert set(expected) <= set(SEEDED), shape
