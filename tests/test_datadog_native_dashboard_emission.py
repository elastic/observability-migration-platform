# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""``generate_dashboard_artifacts`` also emits a NativeDashboard IR artifact.

Mirrors ``tests/test_grafana_native_dashboard_emission.py``: the native
artifact is built from the exact same in-memory YAML document the string is
dumped from, so it cannot drift from the YAML bridge output.
"""

from __future__ import annotations

import json
import pathlib

import yaml

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import (
    generate_dashboard_artifacts,
    generate_dashboard_yaml,
)
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.core.assets.native_dashboard import NativeDashboard
from observability_migration.targets.kibana.dashboards_api import build_payload_from_yaml

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_DASHBOARD_DIR = _REPO_ROOT / "infra" / "datadog" / "dashboards"


def _iter_widgets(widgets: list) -> list:
    ordered: list = []
    for widget in widgets or []:
        ordered.append(widget)
        ordered.extend(_iter_widgets(getattr(widget, "children", []) or []))
    return ordered


def _load(name: str) -> tuple:
    path = _DASHBOARD_DIR / name
    raw = json.loads(path.read_text(encoding="utf-8"))
    dashboard = normalize_dashboard(raw)
    widgets = _iter_widgets(dashboard.widgets)
    results = [translate_widget(widget, plan_widget(widget), OTEL_PROFILE) for widget in widgets]
    return dashboard, results


def _first_fixture() -> str:
    files = sorted(_DASHBOARD_DIR.glob("*.json"))
    assert files, "expected at least one Datadog dashboard fixture"
    return files[0].name


class TestDatadogNativeDashboardEmission:
    def test_generate_dashboard_artifacts_returns_native_dashboard(self) -> None:
        dashboard, results = _load(_first_fixture())
        yaml_string, native, stats = generate_dashboard_artifacts(
            dashboard,
            results,
            data_view=OTEL_PROFILE.metric_index,
            metrics_dataset_filter=OTEL_PROFILE.metrics_dataset_filter,
            logs_dataset_filter=OTEL_PROFILE.logs_dataset_filter,
            logs_index=OTEL_PROFILE.logs_index,
            field_map=OTEL_PROFILE,
        )
        assert isinstance(yaml_string, str)
        assert isinstance(native, NativeDashboard)
        assert native.title == dashboard.title
        assert "mapped" in stats
        assert "reasons" in stats

    def test_generate_dashboard_artifacts_matches_yaml_bridge_payload(self) -> None:
        dashboard, results = _load(_first_fixture())
        yaml_string, native, _stats = generate_dashboard_artifacts(
            dashboard,
            results,
            data_view=OTEL_PROFILE.metric_index,
            metrics_dataset_filter=OTEL_PROFILE.metrics_dataset_filter,
            logs_dataset_filter=OTEL_PROFILE.logs_dataset_filter,
            logs_index=OTEL_PROFILE.logs_index,
            field_map=OTEL_PROFILE,
        )
        doc = yaml.safe_load(yaml_string)
        bridged_payload, _bridged_stats = build_payload_from_yaml(doc)
        assert native.to_api_payload() == bridged_payload

    def test_generate_dashboard_artifacts_yaml_matches_generate_dashboard_yaml(self) -> None:
        """The YAML string half of the tuple is unchanged from the plain function."""
        dashboard, results = _load(_first_fixture())
        plain_yaml = generate_dashboard_yaml(
            dashboard,
            results,
            data_view=OTEL_PROFILE.metric_index,
            metrics_dataset_filter=OTEL_PROFILE.metrics_dataset_filter,
            logs_dataset_filter=OTEL_PROFILE.logs_dataset_filter,
            logs_index=OTEL_PROFILE.logs_index,
            field_map=OTEL_PROFILE,
        )
        tuple_yaml, _native, _stats = generate_dashboard_artifacts(
            dashboard,
            results,
            data_view=OTEL_PROFILE.metric_index,
            metrics_dataset_filter=OTEL_PROFILE.metrics_dataset_filter,
            logs_dataset_filter=OTEL_PROFILE.logs_dataset_filter,
            logs_index=OTEL_PROFILE.logs_index,
            field_map=OTEL_PROFILE,
        )
        assert plain_yaml == tuple_yaml
