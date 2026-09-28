# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A Datadog tag filter must be rendered at the target field's actual type.

Datadog tag values are untyped strings in the source query: the widget
``sum:shop.http.requests{http.response.status_code:500}`` carries ``"500"``.
The Elasticsearch field it maps to has a real type, and ES|QL refuses to
compare across families:

    first argument of [http.response.status_code == "500"] is [numeric]
    so second argument must also be [numeric] but was [keyword]

OTel semantic conventions map ``http.response.status_code`` to ``long``, so
every panel and monitor filtering on an HTTP status failed at query time
against a correctly mapped target. ``FieldMapProfile`` already knows the
target type from live ``_field_caps``; the metric scope renderer has to use it,
the way the log-query renderer already does.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
from observability_migration.adapters.source.datadog.models import ScopeBoolOp, TagFilter
from observability_migration.adapters.source.datadog.translate import (
    _metric_scope_to_esql,
    _tag_filter_to_esql,
)
from observability_migration.core.verification.field_capabilities import FieldCapability


def _profile(**caps: str):
    """A passthrough profile carrying live metric field caps of the given types."""
    profile = deepcopy(BUILTIN_PROFILES["passthrough"])
    profile.metric_field_caps = {
        name.replace("__", "."): FieldCapability(name=name.replace("__", "."), type=field_type)
        for name, field_type in caps.items()
    }
    return profile


STATUS = "http.response.status_code"


def _render(filt, profile):
    return _tag_filter_to_esql(filt, profile, context="metric")


# --- the reported failure -------------------------------------------------


def test_numeric_target_field_compares_numerically():
    """The exact "Shop — Checkout" failure: a long-mapped status code."""
    profile = _profile(http__response__status_code="long")
    assert _render(TagFilter(key=STATUS, value="500"), profile) == f"{STATUS} == 500"


def test_numeric_target_field_negated_compares_numerically():
    profile = _profile(http__response__status_code="long")
    assert _render(TagFilter(key=STATUS, value="500", negated=True), profile) == f"{STATUS} != 500"


@pytest.mark.parametrize("field_type", ["integer", "short", "double", "scaled_float"])
def test_every_numeric_family_member_compares_numerically(field_type):
    profile = _profile(http__response__status_code=field_type)
    assert _render(TagFilter(key=STATUS, value="500"), profile) == f"{STATUS} == 500"


# --- the same field reached through the other value shapes ---------------


def test_numeric_target_field_or_list_compares_numerically():
    """``status:500|503`` expands to OR'd equalities; each one needs the cast."""
    profile = _profile(http__response__status_code="long")
    assert _render(TagFilter(key=STATUS, value="500|503"), profile) == (
        f"({STATUS} == 500 OR {STATUS} == 503)"
    )


def test_numeric_target_field_in_list_compares_numerically():
    """``status IN (500, 503)`` renders a native ES|QL IN list."""
    profile = _profile(http__response__status_code="long")
    filt = TagFilter(key=STATUS, value="500|503", is_in_list=True)
    assert _render(filt, profile) == f"{STATUS} IN (500, 503)"


def test_numeric_target_field_wildcard_matches_as_text():
    """ES|QL LIKE takes a string, so a numeric field is cast rather than quoted.

    The pattern keeps the ``*`` glob -- see
    ``tests/test_datadog_tag_filter_wildcards.py`` for why ``%`` is wrong.
    """
    profile = _profile(http__response__status_code="long")
    assert _render(TagFilter(key=STATUS, value="5*"), profile) == (
        f'TO_STRING({STATUS}) LIKE "5*"'
    )


# --- string fields and unknown types must not regress --------------------


def test_keyword_target_field_still_compares_as_a_string():
    profile = _profile(service__name="keyword")
    assert _render(TagFilter(key="service.name", value="shop-lab"), profile) == (
        'service.name == "shop-lab"'
    )


def test_keyword_target_field_holding_digits_still_compares_as_a_string():
    """A digit-only value against a keyword field stays quoted."""
    profile = _profile(http__response__status_code="keyword")
    assert _render(TagFilter(key=STATUS, value="500"), profile) == f'{STATUS} == "500"'


def test_unknown_target_type_compares_via_to_string():
    """Offline runs have no caps. TO_STRING is valid against keyword *and* numeric."""
    profile = _profile()
    assert _render(TagFilter(key=STATUS, value="500"), profile) == (
        f'TO_STRING({STATUS}) == "500"'
    )


def test_unknown_target_type_keeps_non_numeric_values_unquoted_by_field():
    """A non-numeric value is unambiguous: no cast, no behavior change."""
    profile = _profile()
    assert _render(TagFilter(key="service.name", value="shop-lab"), profile) == (
        'service.name == "shop-lab"'
    )


# --- monitors go through the same renderer -------------------------------


def test_monitor_scope_on_a_numeric_field_compares_numerically():
    """`Shop — HTTP 5xx` is an alert rule, translated through the same path."""
    profile = _profile(http__response__status_code="long", service__name="keyword")
    scope = ScopeBoolOp(
        op="AND",
        children=[
            TagFilter(key="service.name", value="shop-lab"),
            TagFilter(key=STATUS, value="500"),
        ],
    )
    assert _metric_scope_to_esql(scope, profile, context="metric") == (
        f'(service.name == "shop-lab" AND {STATUS} == 500)'
    )


# --- metric_map attribute filters share the same renderer problem ---------


def test_metric_map_attribute_filter_on_a_numeric_field_compares_numerically():
    """A rule pack's ``attribute_filter`` reaches ES|QL through the same defect.

    ``metric_map`` entries let an operator pin a target attribute, e.g.
    ``attribute_filter: {http.response.status_code: "500"}``. The value is a
    string in YAML exactly like a tag value, so it needs the same typing.
    """
    from observability_migration.adapters.source.datadog.translate import (
        _metric_map_attribute_where_clauses,
    )

    profile = _profile(http__response__status_code="long", service__name="keyword")
    profile.merge_metric_map(
        {
            "shop.http.requests": {
                "target": "shop.http.requests",
                "attribute_filter": {STATUS: "500", "service.name": "shop-lab"},
            }
        }
    )
    clauses = _metric_map_attribute_where_clauses(profile, "shop.http.requests")
    assert f"{STATUS} == 500" in clauses
    assert 'service.name == "shop-lab"' in clauses
