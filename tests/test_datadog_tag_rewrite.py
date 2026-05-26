# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Tests for binding-map-aware Datadog tag-filter rewriting (Task 9)."""

from observability_migration.adapters.source.datadog import translate as dd
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.models import TagFilter
from observability_migration.core import variable_classifier as vc


def _bm_single():
    return {
        "host": vc.AcceptedBinding(
            field="host.name", multi=False, options_query="FROM x"
        )
    }


def _bm_multi():
    return {
        "host": vc.AcceptedBinding(
            field="host.name", multi=True, options_query="FROM x"
        )
    }


def test_single_tag_template_emits_param():
    flt = TagFilter(key="host", value="$host", negated=False)
    out = dd._tag_filter_to_esql(
        flt, OTEL_PROFILE, context="metric", binding_map=_bm_single()
    )
    assert out == "host.name == ?host"


def test_multi_tag_template_emits_mv_contains():
    flt = TagFilter(key="host", value="$host", negated=False)
    out = dd._tag_filter_to_esql(
        flt, OTEL_PROFILE, context="metric", binding_map=_bm_multi()
    )
    assert out == "MV_CONTAINS(?host, host.name)"


def test_dot_value_template_also_works():
    flt = TagFilter(key="host", value="$host.value", negated=False)
    out = dd._tag_filter_to_esql(
        flt, OTEL_PROFILE, context="metric", binding_map=_bm_single()
    )
    assert out == "host.name == ?host"


def test_no_binding_falls_through_to_legacy_like():
    flt = TagFilter(key="host", value="$host*", negated=False)
    out = dd._tag_filter_to_esql(
        flt, OTEL_PROFILE, context="metric", binding_map=None
    )
    assert "LIKE" in out
    assert "?host" not in out


def test_negated_single_value_emits_neq():
    flt = TagFilter(key="host", value="$host", negated=True)
    out = dd._tag_filter_to_esql(
        flt, OTEL_PROFILE, context="metric", binding_map=_bm_single()
    )
    assert out == "host.name != ?host"


def test_negated_multi_value_emits_not_mv_contains():
    flt = TagFilter(key="host", value="$host", negated=True)
    out = dd._tag_filter_to_esql(
        flt, OTEL_PROFILE, context="metric", binding_map=_bm_multi()
    )
    assert out == "NOT MV_CONTAINS(?host, host.name)"
