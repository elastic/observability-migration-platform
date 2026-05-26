# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
import pytest

from observability_migration.core import variable_warnings as vw


def test_render_bound_warning():
    msg = vw.render(
        "variable.bound", var="instance", field="service.instance.id", kind="single_value"
    )
    assert msg == (
        "filter applied via ES|QL parameter ?instance "
        "(field=service.instance.id, kind=single_value)"
    )


def test_render_unbound_classic_only():
    msg = vw.render(
        "variable.unbound.classic_only", var="instance", reason="include_all_unsupported"
    )
    assert "include_all_unsupported" in msg
    assert "classic control still applies" in msg


def test_render_dropped():
    msg = vw.render("variable.unbound.dropped", var="x", reason="regex_template")
    assert "no equivalent filter applied" in msg


def test_render_verifier_downgraded():
    msg = vw.render(
        "variable.verifier_downgraded", var="x", invariant="leftover_token"
    )
    assert "downgraded post-translation" in msg


def test_render_unknown_id_raises():
    with pytest.raises(KeyError):
        vw.render("variable.bogus", var="x")
