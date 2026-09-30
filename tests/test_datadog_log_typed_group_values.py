# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grouped Datadog log values must be parsed and typed like single ones.

`field:(a OR b)` was split on `OR` and each member handed to the predicate
renderer as a raw substring, so quoting and target typing were both skipped.
Verified against Elasticsearch 9.6.0, every emitted form below was rejected:

| emitted                      | Elasticsearch                                   |
|------------------------------|-------------------------------------------------|
| `status == "\"error\""`      | matches nothing -- the value contains quote marks |
| `http.status_code LIKE "5*"` | `argument of [...] must be [string], found ... ` |
| `http.status_code == error`  | `Unknown column [error]`                          |
| `http.status_code == "500"`  | `is [numeric] so second argument must also be`    |

The last is the exact error this whole branch started from, reached through
the log path instead of the tag path. The tag path already decides this
correctly (`_tag_comparison_mode`, whose docstring claims to mirror the log
path); these tests pin that the two actually agree.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
from observability_migration.adapters.source.datadog.log_parser import (
    log_ast_to_esql_where,
    parse_log_query,
)
from observability_migration.core.verification.field_capabilities import FieldCapability

_NUMERIC = {"http.status_code": FieldCapability(name="http.status_code", type="long")}
_KEYWORD = {"http.status_code": FieldCapability(name="http.status_code", type="keyword")}


def _where(query, caps=None, profile_name="passthrough"):
    profile = deepcopy(BUILTIN_PROFILES[profile_name])
    if caps is not None:
        profile.log_field_caps = caps
    return log_ast_to_esql_where(parse_log_query(query).ast, profile)


# --- quoting --------------------------------------------------------------

def test_quoted_group_members_do_not_keep_their_quote_characters():
    where = _where('status:("error" OR "warn")')
    assert '\\"' not in where, where
    assert 'status == "error"' in where
    assert 'status == "warn"' in where


def test_a_quoted_member_with_a_space_survives_as_one_value():
    where = _where('status:("not found" OR ok)')
    assert 'status == "not found"' in where, where


def test_a_quoted_member_containing_or_is_not_split():
    where = _where('status:("a OR b" OR c)')
    assert 'status == "a OR b"' in where, where
    assert 'status == "c"' in where


# --- typing: numeric target ----------------------------------------------

def test_a_wildcard_against_a_numeric_field_is_cast_to_string():
    """`LIKE` needs a string operand; ES rejects it on a numeric column."""
    where = _where("@http.status_code:5*", _NUMERIC)
    assert where == 'TO_STRING(http.status_code) LIKE "5*"', where


def test_a_quoted_numeric_member_compares_numerically_on_a_numeric_field():
    where = _where('@http.status_code:("500" OR "503")', _NUMERIC)
    assert "http.status_code == 500" in where, where
    assert '"500"' not in where


def test_a_non_numeric_member_against_a_numeric_field_is_cast_not_bare():
    """A bare word is a *column reference* to ES|QL (`Unknown column [error]`)."""
    where = _where("@http.status_code:(500 OR error)", _NUMERIC)
    assert "== error" not in where, where
    assert "TO_STRING(http.status_code)" in where, where


def test_a_mixed_group_keeps_one_left_hand_side():
    """Mixing families must not emit a numeric compare beside a cast one."""
    where = _where("@http.status_code:(500 OR error)", _NUMERIC)
    assert where.count("TO_STRING(http.status_code)") == 2, where


# --- typing: keyword target ----------------------------------------------

def test_numeric_looking_members_stay_quoted_on_a_keyword_field():
    where = _where("@http.status_code:(500 OR 503)", _KEYWORD)
    assert 'http.status_code == "500"' in where, where
    assert "== 500" not in where.replace('== "500"', "")


def test_a_wildcard_on_a_keyword_field_is_not_cast():
    where = _where("@http.status_code:5*", _KEYWORD)
    assert where == 'http.status_code LIKE "5*"', where


# --- typing: unknown target ----------------------------------------------

def test_numeric_looking_members_are_cast_when_the_type_is_unknown():
    """Valid against both a keyword and a numeric mapping."""
    where = _where("@http.status_code:(500 OR 503)")
    assert "TO_STRING(http.status_code)" in where, where


# --- negation and existing behaviour --------------------------------------

def test_a_negated_group_excludes_every_member():
    """`-field:(a OR b)` parses as a NOT over the group, so it reads
    `NOT (a OR b)` rather than `!= a AND != b`. Equivalent; pinned as
    behaviour, not shape."""
    where = _where("-status:(error OR warn)")
    assert where.startswith("NOT ("), where
    assert 'status == "error"' in where
    assert 'status == "warn"' in where


def test_single_values_are_unchanged():
    assert _where("status:(error)") == 'status == "error"'
    assert _where("status:(error OR warn)") == '(status == "error" OR status == "warn")'


@pytest.mark.parametrize("query", [
    "status:(error OR warn)",
    '@http.status_code:(500 OR 503)',
    '@http.status_code:("500" OR "503")',
    "@http.status_code:5*",
    'status:("error" OR "warn")',
])
def test_no_emitted_clause_contains_an_escaped_quote(query):
    assert '\\"' not in _where(query, _NUMERIC)
