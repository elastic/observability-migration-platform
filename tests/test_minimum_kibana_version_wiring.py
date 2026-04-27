"""Verify the dashboard floor is derived from the binding map per spec §9."""

from __future__ import annotations

import pathlib
import tempfile

import yaml

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import generate_dashboard_yaml
from observability_migration.adapters.source.datadog.models import NormalizedDashboard
from observability_migration.adapters.source.grafana import panels as grafana_panels
from observability_migration.core import variable_classifier as vc


def _empty_datadog_dashboard() -> NormalizedDashboard:
    return NormalizedDashboard(
        id="x",
        title="Test",
        description="",
        widgets=[],
        template_variables=[],
    )


def test_datadog_floor_default_91():
    yaml_text = generate_dashboard_yaml(
        dashboard=_empty_datadog_dashboard(),
        results=[],
        field_map=OTEL_PROFILE,
        data_view="metrics-*",
        logs_index="logs-*",
    )
    doc = yaml.safe_load(yaml_text)
    assert doc["dashboards"][0]["minimum_kibana_version"] == "9.1.0"


def test_datadog_floor_lifts_to_93_when_multi_value_accepted():
    bm = {
        "host": vc.AcceptedBinding(
            field="host.name", multi=True, options_query="FROM x"
        )
    }
    yaml_text = generate_dashboard_yaml(
        dashboard=_empty_datadog_dashboard(),
        results=[],
        field_map=OTEL_PROFILE,
        data_view="metrics-*",
        logs_index="logs-*",
        binding_map=bm,
    )
    doc = yaml.safe_load(yaml_text)
    assert doc["dashboards"][0]["minimum_kibana_version"] == "9.3.0"


def test_datadog_floor_stays_91_for_single_value_only():
    bm = {
        "host": vc.AcceptedBinding(
            field="host.name", multi=False, options_query="FROM x"
        )
    }
    yaml_text = generate_dashboard_yaml(
        dashboard=_empty_datadog_dashboard(),
        results=[],
        field_map=OTEL_PROFILE,
        data_view="metrics-*",
        logs_index="logs-*",
        binding_map=bm,
    )
    doc = yaml.safe_load(yaml_text)
    assert doc["dashboards"][0]["minimum_kibana_version"] == "9.1.0"


def test_datadog_floor_stays_91_when_binding_map_omitted():
    yaml_text = generate_dashboard_yaml(
        dashboard=_empty_datadog_dashboard(),
        results=[],
        field_map=OTEL_PROFILE,
        data_view="metrics-*",
        logs_index="logs-*",
    )
    doc = yaml.safe_load(yaml_text)
    assert doc["dashboards"][0]["minimum_kibana_version"] == "9.1.0"


def test_datadog_floor_stays_91_when_only_rejected_bindings():
    bm = {"foo": vc.RejectedBinding(reason="unsupported_variable_type")}
    yaml_text = generate_dashboard_yaml(
        dashboard=_empty_datadog_dashboard(),
        results=[],
        field_map=OTEL_PROFILE,
        data_view="metrics-*",
        logs_index="logs-*",
        binding_map=bm,
    )
    doc = yaml.safe_load(yaml_text)
    assert doc["dashboards"][0]["minimum_kibana_version"] == "9.1.0"


def _grafana_translate_to_yaml(binding_map=None) -> dict:
    """Run Grafana ``translate_dashboard`` for an empty dashboard and return the YAML doc."""
    dashboard = {"title": "Empty", "uid": "empty", "panels": [], "templating": {"list": []}}
    with tempfile.TemporaryDirectory() as tmpdir:
        kwargs = {}
        if binding_map is not None:
            kwargs["binding_map"] = binding_map
        _result, yaml_path = grafana_panels.translate_dashboard(
            dashboard,
            pathlib.Path(tmpdir),
            datasource_index="metrics-*",
            esql_index="metrics-*",
            **kwargs,
        )
        return yaml.safe_load(yaml_path.read_text())


def test_grafana_floor_default_91():
    doc = _grafana_translate_to_yaml()
    assert doc["dashboards"][0]["minimum_kibana_version"] == "9.1.0"


def test_grafana_floor_lifts_to_93_when_multi_value_accepted():
    bm = {
        "host": vc.AcceptedBinding(
            field="host.name", multi=True, options_query="FROM x"
        )
    }
    doc = _grafana_translate_to_yaml(binding_map=bm)
    assert doc["dashboards"][0]["minimum_kibana_version"] == "9.3.0"


def test_grafana_floor_stays_91_for_single_value_only():
    bm = {
        "host": vc.AcceptedBinding(
            field="host.name", multi=False, options_query="FROM x"
        )
    }
    doc = _grafana_translate_to_yaml(binding_map=bm)
    assert doc["dashboards"][0]["minimum_kibana_version"] == "9.1.0"
