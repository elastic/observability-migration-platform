# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
from observability_migration.core import variable_classifier as vc
from observability_migration.core import variable_control_verifier as ver


def _accepted(field="service.instance.id", multi=False):
    return vc.AcceptedBinding(field=field, multi=multi, options_query="FROM x")


def _record(panel_id, var_name, *, observed_field=None, observed_op=None,
            esql="", source_refs=None, data_view="metrics-*"):
    return ver.PanelTranslationRecord(
        panel_id=panel_id,
        compiled_esql=esql,
        source_var_refs=set(source_refs if source_refs is not None else [var_name]),
        observed_fields={var_name: observed_field} if observed_field else {},
        observed_ops={var_name: observed_op} if observed_op else {},
        data_view=data_view,
    )


def test_field_consistency_downgrade():
    bm = {"x": _accepted()}
    records = [
        _record("p1", "x", observed_field="service.instance.id"),
        _record("p2", "x", observed_field="host.name"),
    ]
    out = ver.verify_bindings(records, bm)
    assert isinstance(out["x"], vc.RejectedBinding)
    assert out["x"].reason == "verifier_failed_field_consistency"


def test_operator_consistency_downgrade():
    bm = {"x": _accepted(multi=True)}
    records = [
        _record("p1", "x", observed_op="exact_match"),
        _record("p2", "x", observed_op="multi_value"),
    ]
    out = ver.verify_bindings(records, bm)
    assert out["x"].reason == "verifier_failed_operator_consistency"


def test_leftover_token_downgrade():
    bm = {"x": _accepted()}
    records = [_record("p1", "x", esql="WHERE a == ?x AND b == \"$x\"")]
    out = ver.verify_bindings(records, bm)
    assert out["x"].reason == "verifier_failed_leftover_token"


def test_missing_param_downgrade():
    bm = {"x": _accepted()}
    records = [_record("p1", "x", source_refs=["x"], esql="WHERE a IS NOT NULL")]
    out = ver.verify_bindings(records, bm)
    assert out["x"].reason == "verifier_failed_missing_param"


def test_over_application_downgrade():
    bm = {"x": _accepted()}
    records = [
        _record("p1", "x", source_refs=[], esql="WHERE a == ?x"),
    ]
    out = ver.verify_bindings(records, bm)
    assert out["x"].reason == "verifier_failed_over_application"


def test_data_view_split_downgrade():
    bm = {"x": _accepted()}
    records = [
        _record("p1", "x", data_view="metrics-*", esql="WHERE a == ?x"),
        _record("p2", "x", data_view="logs-*", esql="WHERE a == ?x"),
    ]
    out = ver.verify_bindings(records, bm)
    assert out["x"].reason == "verifier_failed_data_view_split"


def test_idempotent_when_no_failures():
    bm = {"x": _accepted()}
    records = [_record("p1", "x", observed_field="service.instance.id",
                       esql="WHERE a == ?x")]
    out1 = ver.verify_bindings(records, bm)
    out2 = ver.verify_bindings(records, out1)
    assert out1 == out2
