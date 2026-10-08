# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Compositing a multi-tag breakdown twice must not corrupt the query.

Lens XY and heatmap each take one categorical field, so a query grouped by two
or more tags gets a synthetic ``CONCAT`` column spliced in. That splice mutates
``TranslationResult.esql_query`` in place, so re-entering the panel builder with
the same result composites the *output* of the first pass: the synthetic column
appears among the grouping dimensions and is concatenated into its own
definition.

The result is not merely redundant, it is unrunnable -- ES|QL rejects
``EVAL series_group = CONCAT(..., TO_STRING(series_group))`` with
``Unknown column [series_group]``, because an EVAL cannot read the column it is
defining. That was observed on a real migrated Datadog dashboard, where the two
affected panels failed to render.
"""
from __future__ import annotations

from observability_migration.adapters.source.datadog.generate import (
    _composite_y_column,
)

_BASE = (
    "FROM metrics-*\n"
    "| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend\n"
    "| STATS value = SUM(spans) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend),"
    " deployment.environment, service.name\n"
    "| SORT time_bucket"
)


def _dims_after(query: str, dims: list[str]) -> list[str]:
    """The dimensions a second pass would infer: the originals plus whatever
    synthetic column the first pass projected."""
    return [*dims, "series_group"] if "EVAL series_group" in query else list(dims)


def test_a_second_pass_does_not_add_a_second_eval():
    once, _ = _composite_y_column(_BASE, ["deployment.environment", "service.name"],
                                  name="series_group")
    twice, _ = _composite_y_column(once, _dims_after(once, ["deployment.environment", "service.name"]),
                                   name="series_group")
    assert twice.count("EVAL series_group") == 1


def test_a_second_pass_never_defines_a_column_from_itself():
    once, _ = _composite_y_column(_BASE, ["deployment.environment", "service.name"],
                                  name="series_group")
    twice, _ = _composite_y_column(once, _dims_after(once, ["deployment.environment", "service.name"]),
                                   name="series_group")
    assert "TO_STRING(series_group)" not in twice


def test_a_second_pass_leaves_the_query_unchanged():
    once, name = _composite_y_column(_BASE, ["deployment.environment", "service.name"],
                                     name="series_group")
    twice, name2 = _composite_y_column(once, _dims_after(once, ["deployment.environment", "service.name"]),
                                       name="series_group")
    assert twice == once
    assert name2 == name == "series_group"


def test_the_synthetic_column_is_never_an_input_to_itself_even_if_passed_in():
    """A caller that infers dimensions from the composited query passes the
    synthetic column back in; it must not become one of its own operands."""
    once, _ = _composite_y_column(_BASE, ["deployment.environment", "series_group"],
                                  name="series_group")
    assert "TO_STRING(series_group)" not in once


def test_the_heatmap_y_column_is_idempotent_too():
    once, _ = _composite_y_column(_BASE, ["deployment.environment", "service.name"])
    twice, _ = _composite_y_column(once, ["deployment.environment", "service.name", "y_group"])
    assert twice.count("EVAL y_group") == 1
    assert "TO_STRING(y_group)" not in twice


def test_a_first_pass_still_composites_normally():
    once, name = _composite_y_column(_BASE, ["deployment.environment", "service.name"],
                                     name="series_group")
    assert once.count("EVAL series_group") == 1
    assert 'TO_STRING(deployment.environment)' in once
    assert 'TO_STRING(service.name)' in once
    assert name == "series_group"
