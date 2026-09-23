# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A histogram ``le`` matcher must compare at the target field's type.

``_matcher_has_incompatible_target_field`` drops a label matcher when live
field caps prove the target field is numeric -- ES|QL rejects
``<numeric> == <keyword>`` -- and the panel records the
"Dropped label filters with incompatible target field types" warning.

``le`` is deliberately exempt from that guard (``_FLOAT_LABEL_NAMES``) so
histogram boundaries survive, but it was still rendered as a quoted string.
That combination is the worst of both: the matcher is kept *and* emitted as
``le == "0.5"``, which fails at query time with the same
``verification_exception`` the guard exists to prevent -- and, being exempt,
without even the warning. Every ``histogram_quantile`` / heatmap panel whose
target maps ``le`` as a number hit this.

Verified against Elasticsearch 9.6.0: ``le == "0.5"`` on a ``double`` field is
rejected; ``le == 0.5`` returns the row.
"""

from __future__ import annotations

import pytest

from observability_migration.adapters.source.grafana.promql import _matcher_to_esql
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver


def _resolver(caps, field_profile="passthrough"):
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es",
        index_pattern="metrics-*",
        field_profile=field_profile,
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = dict(caps)
    return resolver


NUMERIC_LE = {"le": {"double": {"type": "double"}}}
KEYWORD_LE = {"le": {"keyword": {"type": "keyword"}}}


def test_numeric_le_compares_numerically():
    resolver = _resolver(NUMERIC_LE)
    assert _matcher_to_esql({"label": "le", "op": "=", "value": "0.5"}, resolver) == "le == 0.5"


def test_numeric_le_integer_boundary_needs_no_string_alternation():
    """``le == 1`` already equals a stored ``1.0`` numerically.

    The ``(le == "1" OR le == "1.0")`` alternation only exists because string
    equality cannot see that ``"1"`` and ``"1.0"`` are the same boundary.
    """
    resolver = _resolver(NUMERIC_LE)
    assert _matcher_to_esql({"label": "le", "op": "=", "value": "1"}, resolver) == "le == 1"


def test_numeric_le_inequality_compares_numerically():
    resolver = _resolver(NUMERIC_LE)
    result = _matcher_to_esql({"label": "le", "op": "!=", "value": "0.5"}, resolver)
    assert "0.5" in result and '"0.5"' not in result, result


@pytest.mark.parametrize("field_type", ["long", "float", "scaled_float", "half_float"])
def test_every_numeric_le_mapping_compares_numerically(field_type):
    resolver = _resolver({"le": {field_type: {"type": field_type}}})
    assert _matcher_to_esql({"label": "le", "op": "=", "value": "0.5"}, resolver) == "le == 0.5"


# --- the keyword-`le` exporters must not regress -------------------------


def test_keyword_le_keeps_the_string_alternation():
    """A keyword ``le`` still needs the "1" / "1.0" spelling alternation."""
    resolver = _resolver(KEYWORD_LE)
    assert _matcher_to_esql({"label": "le", "op": "=", "value": "1"}, resolver) == (
        '(le == "1" OR le == "1.0")'
    )


def test_keyword_le_decimal_boundary_stays_a_string():
    resolver = _resolver(KEYWORD_LE)
    assert _matcher_to_esql({"label": "le", "op": "=", "value": "0.5"}, resolver) == (
        'le == "0.5"'
    )


def test_le_without_field_caps_keeps_the_string_alternation():
    """Offline runs have no caps; the pre-existing string form is unchanged."""
    resolver = _resolver({})
    assert _matcher_to_esql({"label": "le", "op": "=", "value": "1"}, resolver) == (
        '(le == "1" OR le == "1.0")'
    )
