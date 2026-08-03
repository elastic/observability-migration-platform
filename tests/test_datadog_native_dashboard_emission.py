# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""``generate_dashboard_artifacts`` emits DashboardIR + NativeDashboard.

IR-first Phase 2 (mirrors ``tests/test_grafana_native_dashboard_emission.py``):
the native artifact and the YAML string are both *derived* from the same
semantic ``DashboardIR``, so they cannot drift from each other.
"""

from __future__ import annotations

import json
import pathlib

import yaml

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import (
    dashboard_yaml_from_ir,
    generate_dashboard_artifacts,
    generate_dashboard_yaml,
)
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.core.assets.dashboard import DashboardIR
from observability_migration.core.assets.native_dashboard import NativeDashboard
from observability_migration.targets.kibana.dashboards_api import (
    build_payload_from_yaml,
    native_dashboard_from_ir,
)

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


def _artifacts(dashboard, results):
    """Return ``(yaml_string, native, stats, dashboard_ir)`` for inspection.

    The migration pipeline builds only ``(native, stats, dashboard_ir)`` -- the
    YAML export stopped being an artifact format, so production no longer pays
    to serialise it. Tests that inspect the derived document derive it here from
    the returned IR, exactly the way ``generate_dashboard_yaml`` does.
    """
    native, stats, dashboard_ir = generate_dashboard_artifacts(
        dashboard,
        results,
        data_view=OTEL_PROFILE.metric_index,
        metrics_dataset_filter=OTEL_PROFILE.metrics_dataset_filter,
        logs_dataset_filter=OTEL_PROFILE.logs_dataset_filter,
        logs_index=OTEL_PROFILE.logs_index,
        field_map=OTEL_PROFILE,
    )
    return dashboard_yaml_from_ir(dashboard_ir), native, stats, dashboard_ir


class TestDatadogNativeDashboardEmission:
    def test_generate_dashboard_artifacts_returns_native_dashboard(self) -> None:
        dashboard, results = _load(_first_fixture())
        yaml_string, native, stats, dashboard_ir = _artifacts(dashboard, results)
        assert isinstance(yaml_string, str)
        assert isinstance(native, NativeDashboard)
        assert isinstance(dashboard_ir, DashboardIR)
        assert native.title == dashboard.title
        assert "mapped" in stats
        assert "reasons" in stats

    def test_generate_dashboard_artifacts_attaches_dashboard_ir(self) -> None:
        dashboard, results = _load(_first_fixture())
        _yaml_string, _native, _stats, dashboard_ir = _artifacts(dashboard, results)
        assert dashboard_ir.source_adapter == "datadog"
        assert dashboard_ir.title == dashboard.title
        assert dashboard_ir.uid == str(dashboard.id or "")

    def test_generate_dashboard_artifacts_matches_yaml_bridge_payload(self) -> None:
        dashboard, results = _load(_first_fixture())
        yaml_string, native, _stats, dashboard_ir = _artifacts(dashboard, results)
        doc = yaml.safe_load(yaml_string)
        bridged_payload, _bridged_stats = build_payload_from_yaml(doc)
        assert native.to_api_payload() == bridged_payload
        # And the YAML string is exactly the IR's derived export.
        assert doc == {"dashboards": [dashboard_ir.to_yaml_dict()]}

    def test_native_dashboard_is_derived_from_dashboard_ir(self) -> None:
        dashboard, results = _load(_first_fixture())
        _yaml_string, native, _stats, dashboard_ir = _artifacts(dashboard, results)
        native_again, _counts = native_dashboard_from_ir(dashboard_ir)
        assert native_again.to_api_payload() == native.to_api_payload()

    def test_generate_dashboard_artifacts_yaml_matches_generate_dashboard_yaml(self) -> None:
        """The inspection helper's YAML is the export of the artifacts' own IR."""
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
        tuple_yaml, _native, _stats, _dashboard_ir = _artifacts(dashboard, results)
        assert plain_yaml == tuple_yaml

    def test_derived_yaml_is_obtainable_on_demand_and_tracks_the_ir(self) -> None:
        """The pipeline stops building the YAML string; the document is still exact.

        ``generate_dashboard_artifacts`` no longer serialises a YAML string that
        both production call sites threw away. That is only safe while the
        document stays derivable from the IR the pipeline *does* return, so
        assert exactly that: the artifacts tuple carries no string member, and
        the on-demand export equals the IR's own ``to_yaml_dict``. Without this,
        the change could rot into an inspection helper that reports a stale
        document while the uploaded dashboard has moved on.
        """
        dashboard, results = _load(_first_fixture())
        artifacts = generate_dashboard_artifacts(
            dashboard,
            results,
            data_view=OTEL_PROFILE.metric_index,
            field_map=OTEL_PROFILE,
        )
        assert len(artifacts) == 3, "the discarded YAML string must not come back"
        assert not any(isinstance(item, str) for item in artifacts)
        native, _stats, dashboard_ir = artifacts

        on_demand = dashboard_yaml_from_ir(dashboard_ir)
        assert yaml.safe_load(on_demand) == {"dashboards": [dashboard_ir.to_yaml_dict()]}
        # The document and the uploaded payload are still the same dashboard.
        bridged_payload, _bridged_stats = build_payload_from_yaml(yaml.safe_load(on_demand))
        assert native.to_api_payload() == bridged_payload


def test_no_emitted_panel_carries_a_single_step_dynamic_palette():
    """No integration dashboard may emit a one-step dynamic colour palette.

    Kibana rejects it with ``[metrics.0.color.0.steps.1]: At least one of
    "gte", "lt", or "lte" must be provided`` and DROPS the whole panel from the
    saved dashboard -- silently, because the upload path only reads ``id`` off a
    2xx response. It cost 6 panels across Apache, Kubernetes (2), MongoDB (2)
    and Redis before ``_dynamic_palette`` collapsed the single-step case to a
    static colour. Asserted over the whole corpus rather than one widget so a
    new emitter path cannot reintroduce the shape somewhere else.
    """
    import glob
    import json as _json

    from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
    from observability_migration.adapters.source.datadog.generate import (
        generate_dashboard_artifacts,
    )
    from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
    from observability_migration.adapters.source.datadog.planner import plan_widget
    from observability_migration.adapters.source.datadog.translate import translate_widget

    def _widgets(widgets):
        for widget in widgets:
            yield widget
            yield from _widgets(widget.children)

    def _colors(node):
        """Yield every ``color`` object anywhere in the emitted payload."""
        if isinstance(node, dict):
            if isinstance(node.get("color"), dict):
                yield node["color"]
            for value in node.values():
                yield from _colors(value)
        elif isinstance(node, list):
            for item in node:
                yield from _colors(item)

    offenders = []
    sources = sorted(glob.glob("infra/datadog/dashboards/integrations/*.json"))
    assert sources, "expected Datadog integration dashboards to be present"
    for path in sources:
        with open(path, encoding="utf-8") as handle:
            raw = _json.load(handle)
        if "widgets" not in raw:
            continue
        normalized = normalize_dashboard(raw)
        results = [
            translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
            for widget in _widgets(normalized.widgets)
        ]
        native, _stats, _ir = generate_dashboard_artifacts(
            normalized, results, field_map=OTEL_PROFILE
        )
        for color in _colors(native.to_api_payload()):
            steps = color.get("steps")
            if isinstance(steps, list) and len(steps) == 1:
                offenders.append(f"{normalized.title}: {color}")

    assert not offenders, (
        "single-step dynamic colour palettes are rejected by Kibana and cause the "
        f"panel to be dropped from the saved dashboard: {offenders}"
    )
