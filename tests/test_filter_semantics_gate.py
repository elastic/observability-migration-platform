# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline coverage for the live filter-semantics gate's driver.

The gate itself needs a cluster (``parity-rig/verifier/filter_semantics_gate``),
but its driver -- does a wrong row set, a rejected query, or a failed seed
become a finding? -- must be testable in ``make test`` with no docker, the way
``live_validate``'s classifier is. The HTTP layer is injected.

Two properties here came out of review and are the point of most of this file:

* the gate must compare the *selected rows*, not their count, or a predicate
  that selects the wrong document of the right cardinality passes;
* the gate must only ever touch indices it created itself, under names no
  other run can collide with, and must clean them up even when a query blows up.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier.filter_semantics_gate import (  # noqa: E402
    FIELD_TYPES,
    SEEDED,
    SHAPES,
    run_gate,
)

_PROFILE_COUNT = 7


class FakeES:
    """Records every request and answers ``_query`` from ``rows_for``.

    ``rows_for(predicate)`` returns the values a query selects, or a string to
    reject the query with that reason.
    """

    def __init__(self, rows_for):
        self.rows_for = rows_for
        self.calls: list[tuple[str, str]] = []

    def __call__(self, es_url, method, path, body=None, ndjson=False):
        self.calls.append((method, path))
        if path == "/_query":
            outcome = self.rows_for(body["query"])
            if isinstance(outcome, str):
                return 400, json.dumps({"error": {"root_cause": [{"reason": outcome}]}})
            return 200, {"values": [[value] for value in outcome]}
        return 200, {}

    # -- helpers the assertions read ------------------------------------
    @property
    def created(self) -> list[str]:
        return [p.lstrip("/") for m, p in self.calls if m == "PUT"]

    @property
    def deleted(self) -> list[str]:
        return [p.lstrip("/") for m, p in self.calls if m == "DELETE"]


def _correct_rows(order):
    """Answer each query with exactly the rows its shape expects."""
    sequence = list(order)

    def rows_for(_query):
        return sequence.pop(0)

    return rows_for


def _expected_sequence():
    per_pass = [list(expected) for _shape, _kwargs, expected in SHAPES]
    return per_pass * (len(FIELD_TYPES) * _PROFILE_COUNT)


# ---------------------------------------------------------------------------
# Exact row set, not cardinality
# ---------------------------------------------------------------------------

def test_gate_passes_when_every_shape_selects_exactly_its_expected_rows():
    es = FakeES(_correct_rows(_expected_sequence()))
    result = run_gate("http://es.invalid", request=es)
    assert result.ok, [f.render() for f in result.findings]
    assert result.executed == len(SHAPES) * len(FIELD_TYPES) * _PROFILE_COUNT


def test_the_right_number_of_wrong_rows_is_still_a_finding():
    """The hole this closes: a predicate selecting a different document of the
    same cardinality used to pass, because only ``COUNT(*)`` was compared."""

    def rows_for(query):
        # "eq" expects exactly ("500",). Hand back one row that is not it.
        return ["200"]

    result = run_gate("http://es.invalid", profiles=["passthrough"], request=es_of(rows_for))
    assert not result.ok
    eq = [f for f in result.findings if f.shape == "eq"]
    assert eq, "a same-count/wrong-row selection must be reported"
    assert "500" in str(eq[0].expected)
    assert "200" in str(eq[0].actual)


def test_row_order_does_not_matter():
    """ES|QL row order is not part of the contract; the *set* is."""
    # Hand every query its expected rows, reversed. A comparison that kept
    # order would fail all of them.
    order = [list(reversed(expected)) for _s, _k, expected in SHAPES] * (
        len(FIELD_TYPES) * _PROFILE_COUNT
    )
    result = run_gate("http://es.invalid", request=es_of(_correct_rows(order)))
    assert result.ok, [f.render() for f in result.findings]


def test_numeric_rows_compare_equal_to_their_string_expectation():
    """A ``long`` mapping returns ints; the expectation table is strings."""
    order = [
        [int(v) for v in expected] for _s, _k, expected in SHAPES
    ] * (len(FIELD_TYPES) * _PROFILE_COUNT)
    result = run_gate("http://es.invalid", request=es_of(_correct_rows(order)))
    assert result.ok, [f.render() for f in result.findings]


def test_a_rejected_query_becomes_a_finding_carrying_the_reason():
    """The loud class: the type error this gate was built for."""
    reason = (
        'first argument of [http.response.status_code == "500"] is [numeric] '
        "so second argument must also be [numeric] but was [keyword]"
    )
    result = run_gate(
        "http://es.invalid", profiles=["otel"], request=es_of(lambda q: reason)
    )
    assert not result.ok
    assert all(f.actual == "query rejected" for f in result.findings)
    assert all("is [numeric]" in f.detail for f in result.findings)


def test_findings_render_the_predicate_for_triage():
    result = run_gate(
        "http://es.invalid", profiles=["otel"], request=es_of(lambda q: ["999"])
    )
    rendered = result.findings[0].render()
    assert "otel" in rendered
    assert "http.response.status_code" in rendered


# ---------------------------------------------------------------------------
# Index lifecycle: only ever touch what this run made
# ---------------------------------------------------------------------------

def test_indices_are_unique_per_run():
    """Two runs must not share an index name, or concurrent runs erase each
    other's data mid-flight."""
    first = es_of(_correct_rows(_expected_sequence()))
    second = es_of(_correct_rows(_expected_sequence()))
    run_gate("http://es.invalid", request=first)
    run_gate("http://es.invalid", request=second)
    assert first.created and second.created
    assert not set(first.created) & set(second.created)


def test_nothing_is_deleted_before_it_is_created():
    """A pre-emptive DELETE on a predictable name is how this gate could erase
    an unrelated index on whatever endpoint it was pointed at."""
    es = es_of(_correct_rows(_expected_sequence()))
    run_gate("http://es.invalid", request=es)
    for index in es.deleted:
        assert index in es.created, f"deleted {index} without creating it"


def test_every_created_index_is_cleaned_up():
    es = es_of(_correct_rows(_expected_sequence()))
    run_gate("http://es.invalid", request=es)
    assert sorted(es.deleted) == sorted(es.created)


def test_cleanup_still_happens_when_a_query_raises():
    """Cleanup belongs in ``finally``; a transport error must not leak indices."""

    def rows_for(_query):
        raise RuntimeError("connection reset")

    es = es_of(rows_for)
    try:
        run_gate("http://es.invalid", profiles=["otel"], request=es)
    except RuntimeError:
        pass
    assert es.created, "nothing was created, so the test proves nothing"
    assert sorted(es.deleted) == sorted(es.created)


def test_a_failed_index_creation_is_reported_not_ignored():
    """Seeding errors used to be discarded, so the gate could 'pass' against an
    index that was never written."""

    class FailingCreate(FakeES):
        def __call__(self, es_url, method, path, body=None, ndjson=False):
            if method == "PUT":
                self.calls.append((method, path))
                return 400, json.dumps(
                    {"error": {"root_cause": [{"reason": "illegal mapping"}]}}
                )
            return super().__call__(es_url, method, path, body, ndjson)

    es = FailingCreate(_correct_rows(_expected_sequence()))
    result = run_gate("http://es.invalid", profiles=["otel"], request=es)
    assert not result.ok
    assert any("illegal mapping" in (f.detail or "") for f in result.findings)


def test_a_failed_bulk_is_reported_not_ignored():
    class FailingBulk(FakeES):
        def __call__(self, es_url, method, path, body=None, ndjson=False):
            if "_bulk" in path:
                self.calls.append((method, path))
                return 200, {"errors": True, "items": [
                    {"index": {"error": {"reason": "mapper_parsing_exception"}}}
                ]}
            return super().__call__(es_url, method, path, body, ndjson)

    es = FailingBulk(_correct_rows(_expected_sequence()))
    result = run_gate("http://es.invalid", profiles=["otel"], request=es)
    assert not result.ok
    assert any("mapper_parsing" in (f.detail or "") for f in result.findings)


# ---------------------------------------------------------------------------
# The gate's own table
# ---------------------------------------------------------------------------

def test_every_shape_declares_a_reachable_expectation():
    for shape, _kwargs, expected in SHAPES:
        assert expected, f"{shape} expects no rows"
        assert set(expected) <= set(SEEDED), shape


def test_seeded_values_are_distinct_so_they_identify_a_row():
    """The comparison uses the seeded value as each document's identity."""
    assert len(set(SEEDED)) == len(SEEDED)


def es_of(rows_for) -> FakeES:
    return FakeES(rows_for)
