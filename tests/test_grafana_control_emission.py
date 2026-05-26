# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
from observability_migration.adapters.source.grafana.panels import translate_variables
from observability_migration.core import variable_classifier as vc


def _resolver():
    class R:
        def resolve_control_field(self, label):
            return {"instance": "service.instance.id"}.get(label, label)

        def resolve_label(self, label):
            return self.resolve_control_field(label)

        def field_exists(self, field):
            return True

        def field_capability(self, field):
            return None
    return R()


def test_accepted_single_value_emits_esql_control():
    bm = {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=False,
        options_query="FROM metrics-*\n| LIMIT 1000",
    )}
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)"}],
        datasource_index="metrics-*",
        resolver=_resolver(),
        repeat_variable_names=None,
        binding_map=bm,
    )
    assert controls == [{
        "type": "esql",
        "variable_name": "instance",
        "variable_type": "values",
        "multiple": False,
        "label": "Instance",
        "query": "FROM metrics-*\n| LIMIT 1000",
    }]


def test_accepted_multi_value_emits_multi_select():
    bm = {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=True, options_query="FROM x",
    )}
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)",
                        "multi": True}],
        datasource_index="metrics-*",
        resolver=_resolver(),
        binding_map=bm,
    )
    assert controls[0]["variable_type"] == "multi_values"
    assert controls[0]["multiple"] is True


def test_accepted_with_default_values_emits_default_list_for_multi():
    bm = {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=True,
        options_query="FROM x", default_values=("a", "b", "c"),
    )}
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)",
                        "multi": True}],
        datasource_index="metrics-*",
        resolver=_resolver(),
        binding_map=bm,
    )
    assert controls[0]["default"] == ["a", "b", "c"]


def test_accepted_with_default_values_emits_first_for_single():
    bm = {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=False,
        options_query="FROM x", default_values=("first-host",),
    )}
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)"}],
        datasource_index="metrics-*",
        resolver=_resolver(),
        binding_map=bm,
    )
    assert controls[0]["default"] == "first-host"


def test_accepted_without_default_values_omits_default_key():
    bm = {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=True, options_query="FROM x",
    )}
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)",
                        "multi": True}],
        datasource_index="metrics-*",
        resolver=_resolver(),
        binding_map=bm,
    )
    assert "default" not in controls[0]


def test_rejected_variable_emits_classic_options():
    bm = {"instance": vc.RejectedBinding(reason="include_all_unsupported")}
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)"}],
        datasource_index="metrics-*",
        resolver=_resolver(),
        binding_map=bm,
    )
    assert controls[0]["type"] == "options"


def test_no_binding_map_uses_legacy_behavior():
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)"}],
        datasource_index="metrics-*",
        resolver=_resolver(),
    )
    assert controls[0]["type"] == "options"


def test_accepted_variables_emitted_before_rejected():
    bm = {
        "rej": vc.RejectedBinding(reason="include_all_unsupported"),
        "acc": vc.AcceptedBinding(field="f", multi=False, options_query="FROM x"),
    }
    controls = translate_variables(
        template_list=[
            {"name": "rej", "type": "query", "definition": "label_values(up, rej)"},
            {"name": "acc", "type": "query", "definition": "label_values(up, acc)"},
        ],
        datasource_index="metrics-*",
        resolver=_resolver(),
        binding_map=bm,
    )
    assert controls[0]["variable_name"] == "acc"
    assert controls[1].get("type") == "options"
