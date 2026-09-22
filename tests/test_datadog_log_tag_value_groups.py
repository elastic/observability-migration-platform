# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""``status:(error OR warn)`` is Datadog log syntax and must parse as a group.

The tokenizer had an explicit alternative for the ``@attribute`` form
(``@field:(a OR b)``) but none for a bare tag/facet, whose value pattern
``[^\\s,)]+`` stops at the closing paren. ``status:(error OR warn)`` therefore
tokenized as ``KV("status:(error")`` + ``OR`` + ``TERM("warn")`` and rendered::

    status == "(error" OR message LIKE "*warn*"

— valid ES|QL that matches the wrong documents: a literal ``(error`` value
that can never match, plus a free-text scan for ``warn``. Verified against
Elasticsearch 9.6.0: the correct query returns 1 row, this returns 0.

Grouping also wrecked boolean structure beyond the one term::

    service:(a OR b) AND status:error
    -> service == "(a" OR (message LIKE "*b*" AND status == "error")

``_parse_kv_filter`` and the renderer already handle a grouped value (that is
how the ``@attribute`` form works); only the tokenizer could not produce the
token. Profile-independent -- every field profile emitted the same wrong query.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
from observability_migration.adapters.source.datadog.log_parser import (
    log_ast_to_esql_where,
    parse_log_query,
)


def _where(query, profile_name="passthrough"):
    profile = deepcopy(BUILTIN_PROFILES[profile_name])
    return log_ast_to_esql_where(parse_log_query(query).ast, profile)


def test_single_value_group_drops_the_parens():
    assert _where("status:(error)") == 'status == "error"'


def test_or_group_becomes_a_real_disjunction_on_the_field():
    where = _where("status:(error OR warn)")
    assert where == '(status == "error" OR status == "warn")', where
    assert "message LIKE" not in where
    assert '"(error"' not in where


def test_three_value_or_group():
    where = _where("status:(error OR warn OR fatal)")
    for value in ("error", "warn", "fatal"):
        assert f'status == "{value}"' in where, where
    assert "message LIKE" not in where


def test_a_group_does_not_break_surrounding_boolean_structure():
    """The AND must still bind both operands, not get swallowed by the OR."""
    where = _where("service:(a OR b) AND status:error")
    assert 'status == "error"' in where
    assert "message LIKE" not in where, where
    assert where.count("service") == 2, where


def test_negated_group_negates_the_whole_group():
    where = _where("-status:(info OR debug)")
    assert where.startswith("NOT ("), where
    assert "message LIKE" not in where, where


def test_the_attribute_form_still_works():
    """``@attr:(a OR b)`` already worked; it must not regress."""
    where = _where("@http.status_code:(500 OR 503)")
    assert "500" in where and "503" in where
    assert "message LIKE" not in where


@pytest.mark.parametrize("profile_name", ["otel", "passthrough", "elastic_agent"])
def test_every_profile_renders_the_group_as_a_disjunction(profile_name):
    where = _where("status:(error OR warn)", profile_name)
    assert "message LIKE" not in where, (profile_name, where)
    assert '"(error"' not in where, (profile_name, where)


def test_ungrouped_equivalent_is_unchanged():
    assert _where("status:error OR status:warn") == 'status == "error" OR status == "warn"'


def test_a_plain_value_is_unaffected():
    assert _where("status:error") == 'status == "error"'
