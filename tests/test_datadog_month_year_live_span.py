# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Datadog month and year live spans must survive translation.

Datadog's live-span vocabulary includes ``1mo``/``3mo``/``6mo``/``1y`` beside
the ``m``/``h``/``d``/``w`` units. The span parser only accepted the
single-letter units, so a widget scoped to a month or a year fell back to the
one-hour default -- a real dashboard's "compared to last month" panels came
through with a 1-hour window.

``m`` means *minutes* and ``mo`` means *month* in this vocabulary, so the two
must not be confused in either direction.
"""
from __future__ import annotations

import pytest

from observability_migration.adapters.source.datadog.translate import (
    _datadog_span_to_seconds,
    _seconds_to_esql_span,
)

_DAY = 86400


@pytest.mark.parametrize(
    ("span", "seconds"),
    [
        ("1mo", 30 * _DAY),
        ("3mo", 90 * _DAY),
        ("6mo", 180 * _DAY),
        ("1y", 365 * _DAY),
        ("2y", 730 * _DAY),
    ],
)
def test_month_and_year_spans_are_understood(span, seconds):
    assert _datadog_span_to_seconds(span) == seconds


@pytest.mark.parametrize(
    ("span", "seconds"),
    [
        ("1m", 60),
        ("30m", 1800),
        ("1h", 3600),
        ("4h", 4 * 3600),
        ("1d", _DAY),
        ("1w", 7 * _DAY),
        ("30s", 30),
    ],
)
def test_the_existing_units_are_unchanged(span, seconds):
    assert _datadog_span_to_seconds(span) == seconds


def test_minutes_are_not_read_as_months():
    """``m`` is minutes; only ``mo`` is a month."""
    assert _datadog_span_to_seconds("1m") == 60


def test_an_unparseable_span_is_still_zero():
    assert _datadog_span_to_seconds("last tuesday") == 0
    assert _datadog_span_to_seconds("") == 0
    assert _datadog_span_to_seconds("1mon") == 0


def test_a_month_span_renders_as_an_esql_duration():
    assert _seconds_to_esql_span(_datadog_span_to_seconds("1mo")) == "30 days"


def test_a_year_span_renders_as_an_esql_duration():
    assert _seconds_to_esql_span(_datadog_span_to_seconds("1y")) == "365 days"
