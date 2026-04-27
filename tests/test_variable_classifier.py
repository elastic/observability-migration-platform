from dataclasses import dataclass

from observability_migration.core import variable_classifier as vc


def test_accepted_binding_is_frozen():
    binding = vc.AcceptedBinding(
        field="service.instance.id", multi=False, options_query="FROM x"
    )
    try:
        binding.field = "other"  # type: ignore[misc]
    except Exception as exc:
        assert isinstance(exc, (AttributeError, TypeError))
    else:
        raise AssertionError("AcceptedBinding must be frozen")


def test_rejected_binding_requires_known_reason():
    binding = vc.RejectedBinding(reason="unsupported_variable_type")
    assert binding.reason in vc.REJECT_REASONS


def test_rejected_binding_unknown_reason_is_caught_at_construction():
    import pytest
    with pytest.raises(ValueError):
        vc.RejectedBinding(reason="not_a_real_reason")


def test_reject_reasons_tuple_and_literal_agree():
    from typing import get_args
    assert set(get_args(vc.RejectReason)) == set(vc.REJECT_REASONS)


def test_every_reject_reason_constructs_a_rejected_binding():
    for reason in vc.REJECT_REASONS:
        binding = vc.RejectedBinding(reason=reason)
        assert binding.reason == reason


def test_variable_binding_map_accepts_heterogeneous_values():
    bm: vc.VariableBindingMap = {
        "accepted_var": vc.AcceptedBinding(
            field="service.instance.id", multi=False, options_query="FROM x"
        ),
        "rejected_var": vc.RejectedBinding(reason="include_all_unsupported"),
    }
    assert isinstance(bm["accepted_var"], vc.AcceptedBinding)
    assert isinstance(bm["rejected_var"], vc.RejectedBinding)


def test_compute_min_kibana_version_empty_map():
    assert vc.compute_min_kibana_version({}) == "9.1.0"


def test_compute_min_kibana_version_single_value_only():
    bm = {"x": vc.AcceptedBinding(field="f", multi=False, options_query="FROM y")}
    assert vc.compute_min_kibana_version(bm) == "9.1.0"


def test_compute_min_kibana_version_one_multi_value():
    bm = {
        "x": vc.AcceptedBinding(field="f", multi=True, options_query="FROM y"),
        "y": vc.AcceptedBinding(field="g", multi=False, options_query="FROM y"),
    }
    assert vc.compute_min_kibana_version(bm) == "9.3.0"


def test_compute_min_kibana_version_rejected_multi_does_not_lift_floor():
    bm = {"x": vc.RejectedBinding(reason="include_all_unsupported")}
    assert vc.compute_min_kibana_version(bm) == "9.1.0"


def test_build_options_query_shape():
    q = vc.build_options_query(data_view="metrics-*", field="service.instance.id")
    assert q == (
        "FROM metrics-*\n"
        "| WHERE service.instance.id IS NOT NULL\n"
        "| STATS BY service.instance.id\n"
        "| KEEP service.instance.id\n"
        "| LIMIT 1000"
    )


def test_build_options_query_is_deterministic():
    a = vc.build_options_query(data_view="logs-*", field="host.name")
    b = vc.build_options_query(data_view="logs-*", field="host.name")
    assert a == b


def test_build_options_query_rejects_empty_inputs():
    import pytest
    with pytest.raises(ValueError):
        vc.build_options_query(data_view="", field="host.name")
    with pytest.raises(ValueError):
        vc.build_options_query(data_view="metrics-*", field="")


def _grafana_var(**overrides):
    base = {
        "name": "instance",
        "label": "instance",
        "type": "query",
        "definition": "label_values(up, instance)",
        "multi": False,
        "includeAll": False,
        "hide": 0,
    }
    base.update(overrides)
    return base


def _grafana_panel_using(var_name, *, op="=", value_template=None, field="instance"):
    template = value_template if value_template is not None else f"${var_name}"
    return {
        "type": "timeseries",
        "datasource": {"type": "prometheus", "uid": "x"},
        "targets": [{"expr": f'metric{{{field}{op}"{template}"}}', "refId": "A"}],
    }


class _StubResolver:
    def __init__(self, mapping=None):
        self._mapping = (
            mapping if mapping is not None else {"instance": "service.instance.id"}
        )

    def resolve_label(self, label):
        return self._mapping.get(label)

    def resolve_control_field(self, label):
        return self._mapping.get(label)

    def field_exists(self, field):
        return field in self._mapping.values()


def test_grafana_unsupported_variable_type_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(type="custom")],
        panels=[],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert isinstance(bm["instance"], vc.RejectedBinding)
    assert bm["instance"].reason == "unsupported_variable_type"


def test_grafana_drives_repeat_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[],
        resolver=_StubResolver(),
        repeat_variable_names={"instance"},
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "drives_repeat"


def test_grafana_unknown_definition_shape_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(definition="query_result(up)")],
        panels=[],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "unknown_definition_shape"


def test_grafana_field_resolution_failed_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[],
        resolver=_StubResolver(mapping={}),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "field_resolution_failed"


def test_grafana_inconsistent_field_use_rejects():
    panels = [
        _grafana_panel_using("instance", field="instance"),
        _grafana_panel_using("instance", field="other_instance"),
    ]
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=panels,
        resolver=_StubResolver(mapping={
            "instance": "service.instance.id",
            "other_instance": "host.name",
        }),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "inconsistent_field_use"


def test_grafana_regex_template_rejects():
    panel = _grafana_panel_using("instance", op="=~", value_template="prefix-$instance.*")
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[panel],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "regex_template"


def test_grafana_include_all_single_select_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(includeAll=True, multi=False)],
        panels=[_grafana_panel_using("instance")],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "include_all_unsupported"


def test_grafana_invalid_variable_name_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(name="bad-name")],
        panels=[],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["bad-name"].reason == "invalid_variable_name"


def test_grafana_reserved_identifier_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(name="where")],
        panels=[_grafana_panel_using("where")],
        resolver=_StubResolver(mapping={"where": "service.instance.id"}),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["where"].reason == "reserved_identifier"


def test_grafana_accepts_simple_single_value():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[_grafana_panel_using("instance")],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    binding = bm["instance"]
    assert isinstance(binding, vc.AcceptedBinding)
    assert binding.field == "service.instance.id"
    assert binding.multi is False
    assert "service.instance.id" in binding.options_query


def test_grafana_populates_default_values_from_resolver():
    """When the resolver supports cluster discovery, the binding carries
    real distinct-values so Kibana renders the dashboard pre-populated."""
    class _ResolverWithFetch:
        def __init__(self):
            self._mapping = {"instance": "service.instance.id"}

        def resolve_label(self, label):
            return self._mapping.get(label)

        def resolve_control_field(self, label):
            return self._mapping.get(label)

        def field_exists(self, field):
            return True

        def fetch_distinct_field_values(self, field, *, limit=20):
            assert field == "service.instance.id"
            return ["host-1", "host-2", "host-3"]

    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[_grafana_panel_using("instance")],
        resolver=_ResolverWithFetch(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    binding = bm["instance"]
    assert isinstance(binding, vc.AcceptedBinding)
    assert binding.default_values == ("host-1", "host-2", "host-3")


def test_grafana_default_values_empty_when_fetcher_absent():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[_grafana_panel_using("instance")],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    binding = bm["instance"]
    assert isinstance(binding, vc.AcceptedBinding)
    assert binding.default_values == ()


def test_grafana_accepts_multi_value():
    panel = _grafana_panel_using("instance", op="=~")
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(multi=True)],
        panels=[panel],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    binding = bm["instance"]
    assert isinstance(binding, vc.AcceptedBinding)
    assert binding.multi is True


def test_grafana_data_view_split_rejects():
    panels = [
        {**_grafana_panel_using("instance"), "datasource": {"uid": "metrics"}},
        {**_grafana_panel_using("instance"), "datasource": {"uid": "logs"}},
    ]
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=panels,
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
        panel_data_view=lambda p: "metrics-*" if p["datasource"]["uid"] == "metrics" else "logs-*",
    )
    assert bm["instance"].reason == "data_view_split"


@dataclass
class _StubTV:
    name: str
    tag: str = ""
    default: str = ""
    defaults: list = None  # type: ignore[assignment]
    prefix: str = ""

    def __post_init__(self):
        if self.defaults is None:
            self.defaults = []


class _StubFieldMap:
    def __init__(self, mapping=None):
        self._mapping = mapping if mapping is not None else {"host": "host.name"}

    def map_tag(self, tag, context=""):
        return self._mapping.get(tag)


def _datadog_widget_filter(tag, value):
    return {"requests": [{"q": f"avg:metric{{{tag}:{value}}}"}]}


def test_datadog_no_tag_field_rejects():
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="scope", tag="")],
        widgets=[],
        field_map=_StubFieldMap(),
        data_view="metrics-*",
    )
    assert bm["scope"].reason == "no_tag_field"


def test_datadog_wildcard_default_rejects():
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="host", tag="host", default="*")],
        widgets=[],
        field_map=_StubFieldMap(),
        data_view="metrics-*",
    )
    assert bm["host"].reason == "wildcard_default"


def test_datadog_field_resolution_failed_rejects():
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="region", tag="region")],
        widgets=[],
        field_map=_StubFieldMap(mapping={}),
        data_view="metrics-*",
    )
    assert bm["region"].reason == "field_resolution_failed"


def test_datadog_accepts_single_tag():
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="host", tag="host")],
        widgets=[_datadog_widget_filter("host", "$host")],
        field_map=_StubFieldMap(),
        data_view="metrics-*",
    )
    binding = bm["host"]
    assert isinstance(binding, vc.AcceptedBinding)
    assert binding.field == "host.name"
    assert binding.multi is False


def test_datadog_accepts_multi_when_default_star():
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="host", tag="host", default="*", defaults=["a", "b"])],
        widgets=[_datadog_widget_filter("host", "$host")],
        field_map=_StubFieldMap(),
        data_view="metrics-*",
    )
    assert isinstance(bm["host"], vc.AcceptedBinding)
    assert bm["host"].multi is True


def test_disable_env_var_short_circuits_grafana(monkeypatch):
    monkeypatch.setenv("OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS", "1")
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[_grafana_panel_using("instance")],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert isinstance(bm["instance"], vc.RejectedBinding)
    assert bm["instance"].reason == "unsupported_variable_type"


def test_disable_env_var_short_circuits_datadog(monkeypatch):
    monkeypatch.setenv("OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS", "1")
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="host", tag="host")],
        widgets=[_datadog_widget_filter("host", "$host")],
        field_map=_StubFieldMap(),
        data_view="metrics-*",
    )
    assert bm["host"].reason == "unsupported_variable_type"
