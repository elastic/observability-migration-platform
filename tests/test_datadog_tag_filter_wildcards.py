# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""ES|QL ``LIKE`` wildcards are ``*`` and ``?``, not SQL's ``%`` and ``_``.

The metric tag-filter renderer used to translate a Datadog glob into SQL
wildcards (``*`` -> ``%``, ``?`` -> ``_``). ES|QL treats ``%`` and ``_`` as
*literal characters*, so those patterns are valid ES|QL that quietly matches
the wrong rows -- no error, no warning, nothing for the ES|QL execution gate or
the schema gate to catch:

* ``{host:web-*}``  -> ``host.name LIKE "web-%"``      matches **nothing**;
  the panel renders empty.
* ``{!host:canary*}`` -> ``host.name NOT LIKE "canary%"`` matches
  **everything**; the exclusion silently does nothing and the panel shows the
  canary hosts it was supposed to drop.

Verified against Elasticsearch 9.6.0 (see the module docstring of
``tests/test_datadog_numeric_tag_filters.py`` for the type-family half of this
renderer). ``log_parser`` and the template-variable path always emitted ``*``;
only this one branch disagreed.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
from observability_migration.adapters.source.datadog.models import TagFilter
from observability_migration.adapters.source.datadog.translate import _tag_filter_to_esql


def _render(filt, profile=None):
    profile = profile or deepcopy(BUILTIN_PROFILES["passthrough"])
    return _tag_filter_to_esql(filt, profile, context="metric")


def test_trailing_glob_keeps_the_esql_wildcard():
    assert _render(TagFilter(key="host.name", value="web-*")) == 'host.name LIKE "web-*"'


def test_negated_glob_keeps_the_esql_wildcard():
    """The exclusion has to actually exclude."""
    assert _render(TagFilter(key="host.name", value="canary*", negated=True)) == (
        'host.name NOT LIKE "canary*"'
    )


def test_leading_and_inner_globs_keep_the_esql_wildcard():
    assert _render(TagFilter(key="host.name", value="*-canary-*")) == (
        'host.name LIKE "*-canary-*"'
    )


def test_single_char_glob_keeps_the_esql_wildcard():
    assert _render(TagFilter(key="host.name", value="web-?")) == 'host.name LIKE "web-?"'


def test_glob_inside_an_or_list_keeps_the_esql_wildcard():
    assert _render(TagFilter(key="host.name", value="web-*|db-*")) == (
        '(host.name LIKE "web-*" OR host.name LIKE "db-*")'
    )


def test_mixed_literal_and_glob_or_list():
    assert _render(TagFilter(key="host.name", value="web-1|db-*")) == (
        '(host.name == "web-1" OR host.name LIKE "db-*")'
    )


@pytest.mark.parametrize("literal", ["100%", "a_b", "50%-off"])
def test_sql_wildcard_characters_in_a_value_stay_literal(literal):
    """``%``/``_`` carry no meaning in ES|QL, so a value containing them is exact."""
    assert _render(TagFilter(key="host.name", value=literal)) == (
        f'host.name == "{literal}"'
    )


def test_template_variable_glob_matches_the_explicit_glob_form():
    """The template path already emitted ``*``; both branches must now agree."""
    template = _render(TagFilter(key="host.name", value="$host-suffix"))
    assert template == 'host.name LIKE "*-suffix"', template
