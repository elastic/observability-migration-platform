# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Every field profile must render filter literals at the target field's type.

The existing portability suites
(``test_datadog_field_profile_portability.py``,
``test_grafana_field_profile_portability.py``) prove each profile emits the
right field *names*. Nothing proved the *values* compared against those fields
were rendered at the right type, which is how
``http.response.status_code == "500"`` reached a customer demo: a profile can
namespace a tag perfectly and still emit a literal Elasticsearch refuses.

This matrix runs every filter shape under every built-in profile of both
sources and asserts two invariants:

1. A literal compared against a caps-confirmed **numeric** field is never
   quoted (ES|QL: "first argument ... is [numeric] so second argument must
   also be [numeric]").
2. A literal compared against a caps-confirmed **string** field is always
   quoted (the mirror error: "is [keyword] ... but was [integer]").

Both error strings were reproduced against Elasticsearch 9.6.0; the shapes
asserted here were executed against that cluster and returned rows.
"""

from __future__ import annotations

import re
from copy import deepcopy

import pytest

from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
from observability_migration.adapters.source.datadog.models import TagFilter
from observability_migration.adapters.source.datadog.translate import _tag_filter_to_esql
from observability_migration.adapters.source.grafana.promql import _matcher_to_esql
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.core.verification.field_capabilities import FieldCapability

DD_PROFILES = sorted(BUILTIN_PROFILES)
GRAFANA_PROFILES = [
    "otel",
    "prometheus_remote_write",
    "prometheus_metrics",
    "prometheus_native",
    "passthrough",
]
NUMERIC_TYPES = ["long", "integer", "short", "byte", "double", "float", "scaled_float"]
STRING_TYPES = ["keyword", "constant_keyword", "wildcard"]

TAG = "http.response.status_code"


# --------------------------------------------------------------------------
# Datadog
# --------------------------------------------------------------------------


def _dd(profile_name, field_type=None, tag=TAG):
    """Return ``(profile, mapped_field)`` with ``tag`` typed as ``field_type``."""
    profile = deepcopy(BUILTIN_PROFILES[profile_name])
    mapped = profile.map_tag(tag, context="metric")
    profile.metric_field_caps = (
        {mapped: FieldCapability(name=mapped, type=field_type)} if field_type else {}
    )
    return profile, mapped


def _render(filt, profile):
    return _tag_filter_to_esql(filt, profile, context="metric")


# Every value shape a Datadog scope can produce, as
# (id, TagFilter kwargs, expected-against-numeric, expected-against-keyword).
DD_SHAPES = [
    ("eq", dict(value="500"), "{f} == 500", '{f} == "500"'),
    ("neq", dict(value="500", negated=True), "{f} != 500", '{f} != "500"'),
    (
        "or-list",
        dict(value="500|503"),
        "({f} == 500 OR {f} == 503)",
        '({f} == "500" OR {f} == "503")',
    ),
    (
        "neg-or-list",
        dict(value="500|503", negated=True),
        "({f} != 500 AND {f} != 503)",
        '({f} != "500" AND {f} != "503")',
    ),
    (
        "in-list",
        dict(value="500|503", is_in_list=True),
        "{f} IN (500, 503)",
        '{f} IN ("500", "503")',
    ),
    (
        "not-in-list",
        dict(value="500|503", is_in_list=True, negated=True),
        "{f} NOT IN (500, 503)",
        '{f} NOT IN ("500", "503")',
    ),
    ("glob", dict(value="5*"), 'TO_STRING({f}) LIKE "5*"', '{f} LIKE "5*"'),
    (
        "neg-glob",
        dict(value="5*", negated=True),
        'TO_STRING({f}) NOT LIKE "5*"',
        '{f} NOT LIKE "5*"',
    ),
]


@pytest.mark.parametrize("profile_name", DD_PROFILES)
@pytest.mark.parametrize("shape_id,kwargs,numeric_expected,_kw", DD_SHAPES)
def test_datadog_numeric_field_never_gets_a_quoted_literal(
    profile_name, shape_id, kwargs, numeric_expected, _kw
):
    profile, mapped = _dd(profile_name, "long")
    result = _render(TagFilter(key=TAG, **kwargs), profile)
    assert result == numeric_expected.format(f=mapped), result


@pytest.mark.parametrize("profile_name", DD_PROFILES)
@pytest.mark.parametrize("shape_id,kwargs,_num,keyword_expected", DD_SHAPES)
def test_datadog_keyword_field_always_gets_a_quoted_literal(
    profile_name, shape_id, kwargs, _num, keyword_expected
):
    profile, mapped = _dd(profile_name, "keyword")
    result = _render(TagFilter(key=TAG, **kwargs), profile)
    assert result == keyword_expected.format(f=mapped), result


@pytest.mark.parametrize("profile_name", DD_PROFILES)
@pytest.mark.parametrize("field_type", NUMERIC_TYPES)
def test_datadog_every_numeric_mapping_across_every_profile(profile_name, field_type):
    profile, mapped = _dd(profile_name, field_type)
    assert _render(TagFilter(key=TAG, value="500"), profile) == f"{mapped} == 500"


@pytest.mark.parametrize("profile_name", DD_PROFILES)
@pytest.mark.parametrize("field_type", STRING_TYPES)
def test_datadog_every_string_mapping_across_every_profile(profile_name, field_type):
    profile, mapped = _dd(profile_name, field_type)
    assert _render(TagFilter(key=TAG, value="500"), profile) == f'{mapped} == "500"'


@pytest.mark.parametrize("profile_name", DD_PROFILES)
def test_datadog_unknown_type_is_comparable_against_both_mappings(profile_name):
    """No caps: the predicate must be valid whether the field is keyword or numeric."""
    profile, mapped = _dd(profile_name, None)
    assert _render(TagFilter(key=TAG, value="500"), profile) == (
        f'TO_STRING({mapped}) == "500"'
    )


@pytest.mark.parametrize("profile_name", DD_PROFILES)
def test_datadog_non_numeric_value_is_unaffected_by_profile(profile_name):
    profile, mapped = _dd(profile_name, "keyword", tag="service.name")
    assert _render(TagFilter(key="service.name", value="shop-lab"), profile) == (
        f'{mapped} == "shop-lab"'
    )


# --------------------------------------------------------------------------
# Grafana
# --------------------------------------------------------------------------


def _grafana(profile_name, label, field_type=None):
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es",
        index_pattern="metrics-*",
        field_profile=profile_name,
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    mapped = resolver.resolve_label(label)
    resolver._field_cache = (
        {mapped: {field_type: {"type": field_type}}} if field_type else {}
    )
    return resolver, mapped


@pytest.mark.parametrize("profile_name", GRAFANA_PROFILES)
@pytest.mark.parametrize("field_type", NUMERIC_TYPES)
def test_grafana_numeric_le_compares_numerically_under_every_profile(
    profile_name, field_type
):
    resolver, mapped = _grafana(profile_name, "le", field_type)
    result = _matcher_to_esql({"label": "le", "op": "=", "value": "0.5"}, resolver)
    assert result == f"{mapped} == 0.5", result


@pytest.mark.parametrize("profile_name", GRAFANA_PROFILES)
def test_grafana_keyword_le_stays_quoted_under_every_profile(profile_name):
    resolver, mapped = _grafana(profile_name, "le", "keyword")
    result = _matcher_to_esql({"label": "le", "op": "=", "value": "0.5"}, resolver)
    assert result == f'{mapped} == "0.5"', result


@pytest.mark.parametrize("profile_name", GRAFANA_PROFILES)
def test_grafana_numeric_le_regex_casts_the_field(profile_name):
    """ES|QL RLIKE needs a string argument."""
    resolver, mapped = _grafana(profile_name, "le", "double")
    result = _matcher_to_esql({"label": "le", "op": "=~", "value": "0\\.5"}, resolver)
    assert result == f'TO_STRING({mapped}) RLIKE "0\\\\.5"', result


@pytest.mark.parametrize("profile_name", GRAFANA_PROFILES)
def test_grafana_infinite_histogram_bucket_stays_valid(profile_name):
    """``le="+Inf"`` is a real boundary but not an ES|QL numeric literal."""
    resolver, mapped = _grafana(profile_name, "le", "double")
    result = _matcher_to_esql({"label": "le", "op": "=", "value": "+Inf"}, resolver)
    assert result == f'TO_STRING({mapped}) == "+Inf"', result


@pytest.mark.parametrize("profile_name", GRAFANA_PROFILES)
def test_grafana_non_le_numeric_matcher_is_still_dropped_with_a_warning(profile_name):
    """Unchanged behavior: the drop is reported by the panel warning."""
    resolver, _ = _grafana(profile_name, "status_code", "long")
    result = _matcher_to_esql(
        {"label": "status_code", "op": "=", "value": "500"}, resolver
    )
    assert result is None, result


# --------------------------------------------------------------------------
# Cross-source invariant
# --------------------------------------------------------------------------

_QUOTED_AGAINST_BARE_FIELD = re.compile(r'(?<!TO_STRING\()\b[\w.`]+\s*(?:==|!=)\s*"')


@pytest.mark.parametrize("profile_name", DD_PROFILES)
@pytest.mark.parametrize("shape_id,kwargs,_num,_kw", DD_SHAPES)
def test_no_datadog_shape_compares_a_numeric_field_to_a_quoted_literal(
    profile_name, shape_id, kwargs, _num, _kw
):
    """The invariant, stated independently of the expected strings above."""
    profile, mapped = _dd(profile_name, "long")
    result = _render(TagFilter(key=TAG, **kwargs), profile)
    bare_compares = [
        fragment
        for fragment in result.split(" OR ") + result.split(" AND ")
        if f"{mapped} ==" in fragment or f"{mapped} !=" in fragment
    ]
    for fragment in bare_compares:
        assert '"' not in fragment, f"{shape_id}/{profile_name}: {result}"
