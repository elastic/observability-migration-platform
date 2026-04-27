# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Property-based parser checks for Datadog query syntaxes."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from observability_migration.adapters.source.datadog.log_parser import (
    log_ast_to_esql_where,
    log_ast_to_kql,
    parse_log_query,
)
from observability_migration.adapters.source.datadog.query_parser import (
    ParseError,
    parse_formula,
    parse_metric_query,
)

_PRINTABLE_QUERY_TEXT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Po", "Zs"),
        whitelist_characters="@$_",
    ),
    max_size=80,
)

_IDENTIFIER = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789._",
    min_size=1,
    max_size=20,
).filter(lambda value: value[0].isalpha())
_FORMULA_IDENTIFIER = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_",
    min_size=1,
    max_size=20,
).filter(lambda value: value[0].isalpha())

_TAG_VALUE = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-*",
    min_size=1,
    max_size=12,
)

_TAG_FILTER = st.builds(
    lambda negated, key, value: f"{'!' if negated else ''}{key}:{value}",
    st.booleans(),
    _IDENTIFIER,
    _TAG_VALUE,
)
_FORMULA_NUMBER = st.one_of(
    st.integers(min_value=0, max_value=1000).map(str),
    st.tuples(
        st.integers(min_value=0, max_value=1000),
        st.integers(min_value=0, max_value=99),
    ).map(lambda pair: f"{pair[0]}.{pair[1]:02d}"),
)


@st.composite
def _formula_inputs(draw):
    base = st.one_of(
        _FORMULA_IDENTIFIER.map(lambda name: (name, frozenset({name}))),
        _FORMULA_NUMBER.map(lambda number: (number, frozenset())),
    )
    expr = st.recursive(
        base,
        lambda children: st.one_of(
            children.map(lambda child: (f"-({child[0]})", child[1])),
            children.map(lambda child: (f"abs({child[0]})", child[1])),
            children.map(lambda child: (f"per_second({child[0]})", child[1])),
            st.tuples(children, _FORMULA_NUMBER).map(
                lambda pair: (f"clamp_min({pair[0][0]}, {pair[1]})", pair[0][1])
            ),
            st.tuples(children, st.sampled_from(["+", "-", "*", "/"]), children).map(
                lambda parts: (
                    f"({parts[0][0]} {parts[1]} {parts[2][0]})",
                    parts[0][1] | parts[2][1],
                )
            ),
        ),
        max_leaves=10,
    )
    return draw(expr)


@given(_PRINTABLE_QUERY_TEXT)
@settings(max_examples=200, deadline=None)
def test_parse_log_query_handles_arbitrary_text(raw: str):
    query = parse_log_query(raw)
    assert query.raw == raw
    if query.ast is not None:
        assert isinstance(log_ast_to_kql(query.ast), str)
        assert isinstance(log_ast_to_esql_where(query.ast), str)


@given(
    agg=st.sampled_from(["avg", "sum", "min", "max", "count", "last"]),
    metric=_IDENTIFIER,
    filters=st.lists(_TAG_FILTER, min_size=0, max_size=3),
    group_by=st.lists(_IDENTIFIER, min_size=0, max_size=2, unique=True),
    as_rate=st.booleans(),
)
@settings(max_examples=120, deadline=None)
def test_parse_metric_query_handles_structured_queries(
    agg: str,
    metric: str,
    filters: list[str],
    group_by: list[str],
    as_rate: bool,
):
    scope = ",".join(filters) if filters else "*"
    raw = f"{agg}:{metric}{{{scope}}}"
    if group_by:
        raw += " by {" + ",".join(group_by) + "}"
    if as_rate:
        raw += ".as_rate()"

    query = parse_metric_query(raw)

    assert query.space_agg == agg
    assert query.metric == metric
    assert query.group_by == group_by
    assert query.as_rate is as_rate


@given(_PRINTABLE_QUERY_TEXT)
@settings(max_examples=200, deadline=None)
def test_parse_metric_query_rejects_or_parses_without_crashing(raw: str):
    try:
        query = parse_metric_query(raw)
    except ParseError:
        return

    assert query.raw == raw.strip()


@given(_formula_inputs())
@settings(max_examples=150, deadline=None)
def test_parse_formula_handles_generated_expressions(payload: tuple[str, frozenset[str]]):
    raw, expected_refs = payload

    formula = parse_formula(raw)

    assert formula.raw == raw.strip()
    assert formula.ast is not None
    assert set(formula.referenced_queries) == set(expected_refs)


@given(_PRINTABLE_QUERY_TEXT)
@settings(max_examples=200, deadline=None)
def test_parse_formula_rejects_or_parses_without_crashing(raw: str):
    try:
        formula = parse_formula(raw)
    except ParseError:
        return

    assert formula.raw == raw.strip()
