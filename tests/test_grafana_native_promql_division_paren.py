# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Native ``PROMQL`` must not emit ``/(``.

Found sweeping the curated packs offline and smoking them on Elasticsearch 9.6:
PostgreSQL Exporter "Commit Ratio" and "Shared Buffer Hits" failed with
``mismatched input ')' expecting {<EOF>, '|'}``. The ES ``PROMQL`` command
rejects a division directly followed by ``(`` (``a/(b)``) but parses ``a/ (b)``,
``a / (b)`` and ``a*(b)``. With ``--es-url`` the live parse gate hid this by
degrading those panels to the ES|QL approximation.
"""

from observability_migration.adapters.source.grafana.panels import (
    _clean_promql_for_native,
    _space_division_before_paren,
    build_native_promql_query,
)


def test_division_before_paren_gets_a_space():
    expr = "sum(a{instance=~?Instance})/(sum(a{instance=~?Instance}) + sum(b))"
    assert _space_division_before_paren(expr) == (
        "sum(a{instance=~?Instance})/ (sum(a{instance=~?Instance}) + sum(b))"
    )


def test_quoted_label_values_are_untouched():
    for expr in ('a{path=~"/(api|web)"}/(b)', "a{path=~'/(x)'}/(b)", "a{path=~`/(x)`}/(b)"):
        out = _space_division_before_paren(expr)
        assert out.split("}", 1)[0] == expr.split("}", 1)[0]
        assert out.endswith("}/ (b)")


def test_escaped_quote_does_not_end_the_string():
    expr = r'a{p=~"x\"/(y"}/(b)'
    assert _space_division_before_paren(expr) == r'a{p=~"x\"/(y"}/ (b)'


def test_other_operators_and_spaced_division_are_unchanged():
    for expr in ("a*(b)", "a / (b)", "a/ (b)", "a/b", "(a)/2"):
        assert _space_division_before_paren(expr) == expr


def test_emitted_native_query_has_no_division_paren():
    expr = (
        "(sum(pg_stat_database_xact_commit{instance=~\"$Instance\"})"
        "/(sum(pg_stat_database_xact_commit{instance=~\"$Instance\"})"
        " + sum(pg_stat_database_xact_rollback{instance=~\"$Instance\"})))"
    )
    assert "/(" not in _clean_promql_for_native(expr)
    query = build_native_promql_query(
        expr.replace("$Instance", ".*"), index="metrics-*", kibana_type="gauge", adaptive_step=True
    )
    assert "/(" not in query
    assert "/ (sum(pg_stat_database_xact_commit" in query
