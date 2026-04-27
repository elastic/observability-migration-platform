from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import _build_controls_from_template_vars
from observability_migration.adapters.source.datadog.models import TemplateVariable
from observability_migration.core import variable_classifier as vc


def test_accepted_emits_esql_control():
    bm = {"host": vc.AcceptedBinding(
        field="host.name", multi=False,
        options_query="FROM metrics-*\n| LIMIT 1000",
    )}
    controls = _build_controls_from_template_vars(
        template_vars=[TemplateVariable(name="host", tag="host")],
        data_view="metrics-*",
        field_map=OTEL_PROFILE,
        binding_map=bm,
    )
    assert controls[0]["type"] == "esql"
    assert controls[0]["variable_name"] == "host"
    assert controls[0]["multiple"] is False


def test_accepted_multi_emits_multi_select():
    bm = {"host": vc.AcceptedBinding(
        field="host.name", multi=True, options_query="FROM x",
    )}
    controls = _build_controls_from_template_vars(
        template_vars=[TemplateVariable(name="host", tag="host")],
        data_view="metrics-*",
        field_map=OTEL_PROFILE,
        binding_map=bm,
    )
    assert controls[0]["variable_type"] == "multi_values"
    assert controls[0]["multiple"] is True


def test_rejected_emits_classic_options():
    bm = {"host": vc.RejectedBinding(reason="wildcard_default")}
    controls = _build_controls_from_template_vars(
        template_vars=[TemplateVariable(name="host", tag="host")],
        data_view="metrics-*",
        field_map=OTEL_PROFILE,
        binding_map=bm,
    )
    assert controls[0]["type"] == "options"


def test_no_binding_map_uses_legacy():
    controls = _build_controls_from_template_vars(
        template_vars=[TemplateVariable(name="host", tag="host")],
        data_view="metrics-*",
        field_map=OTEL_PROFILE,
    )
    assert controls[0]["type"] == "options"


def test_accepted_emitted_before_rejected():
    bm = {
        "rej": vc.RejectedBinding(reason="wildcard_default"),
        "acc": vc.AcceptedBinding(field="f", multi=False, options_query="FROM x"),
    }
    controls = _build_controls_from_template_vars(
        template_vars=[
            TemplateVariable(name="rej", tag="rej"),
            TemplateVariable(name="acc", tag="acc"),
        ],
        data_view="metrics-*",
        field_map=OTEL_PROFILE,
        binding_map=bm,
    )
    assert controls[0]["variable_name"] == "acc"
    assert controls[1].get("type") == "options"
