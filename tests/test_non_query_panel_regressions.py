# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression coverage for native non-query dashboard panels."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from observability_migration.adapters.source.datadog.generate import _build_yaml_panel
from observability_migration.adapters.source.datadog.models import NormalizedWidget
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.adapters.source.datadog.verification import (
    _build_target_candidates as build_datadog_target_candidates,
)
from observability_migration.adapters.source.grafana.links import (
    build_links_panel,
    translate_dashboard_links,
)
from observability_migration.adapters.source.grafana.panels import translate_dashboard
from observability_migration.adapters.source.grafana.verification import (
    build_target_candidates,
)
from observability_migration.core.reporting.report import PanelResult
from observability_migration.targets.kibana import dashboards_api


def test_links_mapper_preserves_title_and_false_new_tab():
    result = dashboards_api.map_yaml_panel(
        {
            "title": "Dashboard Links",
            "size": {"w": 48, "h": 3},
            "position": {"x": 0, "y": 6},
            "links": {
                "items": [
                    {
                        "label": "Docs",
                        "url": "https://example.com/docs",
                        "new_tab": False,
                    },
                    {
                        "label": "Status",
                        "url": "https://status.example.com",
                        "new_tab": True,
                    },
                ]
            },
        }
    )

    assert result.api_panel is not None
    assert result.api_panel["config"]["title"] == "Dashboard Links"
    assert result.api_panel["config"]["links"][0]["options"]["open_in_new_tab"] is False
    assert result.api_panel["config"]["links"][1]["options"]["open_in_new_tab"] is True


def test_links_yaml_mapper_preserves_hide_title():
    result = dashboards_api.map_yaml_panel(
        {
            "title": "Dashboard Links",
            "hide_title": True,
            "links": {"items": [{"url": "https://example.com/docs"}]},
        }
    )

    assert result.api_panel is not None
    assert result.api_panel["config"]["hide_title"] is True


def test_image_yaml_mapper_preserves_native_image_configuration():
    result = dashboards_api.map_yaml_panel(
        {
            "title": "Architecture",
            "size": {"w": 24, "h": 8},
            "position": {"x": 0, "y": 0},
            "image": {
                "from_url": "https://example.com/architecture.png",
                "fit": "cover",
                "background_color": "#000000",
                "description": "System architecture",
            },
        }
    )

    assert result.api_panel is not None
    assert result.api_panel["type"] == "image"
    assert result.api_panel["config"] == {
        "title": "Architecture",
        "image_config": {
            "src": {"type": "url", "url": "https://example.com/architecture.png"},
            "object_fit": "cover",
            "background_color": "#000000",
            "alt_text": "System architecture",
        },
    }


def test_image_yaml_mapper_preserves_hide_title():
    result = dashboards_api.map_yaml_panel(
        {
            "title": "Architecture",
            "hide_title": True,
            "image": {"from_url": "https://example.com/architecture.png"},
        }
    )

    assert result.api_panel is not None
    assert result.api_panel["config"]["hide_title"] is True


def test_relative_grafana_dashboard_link_stays_manual():
    translated = translate_dashboard_links(
        {
            "links": [
                {
                    "type": "link",
                    "title": "Local dashboard",
                    "url": "/d/other-dashboard",
                }
            ]
        }
    )

    assert translated[0]["kibana_action"] == "manual_navigation"
    assert build_links_panel(translated) is None


def test_grafana_dashboard_link_with_inline_variable_stays_manual():
    translated = translate_dashboard_links(
        {
            "links": [
                {
                    "type": "link",
                    "title": "Environment runbook",
                    "url": "https://example.com/runbooks/$environment",
                }
            ]
        }
    )

    assert translated[0]["kibana_action"] == "manual_navigation"
    assert build_links_panel(translated) is None


def test_links_panel_verification_recommends_native_links():
    panel = PanelResult(
        title="Dashboard Links",
        grafana_type="dashboard_links",
        kibana_type="links",
        status="migrated",
        confidence=1.0,
    )

    candidates = build_target_candidates(panel)

    assert candidates[0]["target"] == "native_links_panel"


def test_synthesized_links_panel_is_included_in_target_panel_denominator():
    dashboard = {
        "title": "Docs",
        "uid": "docs",
        "panels": [
            {
                "id": 1,
                "title": "Read me",
                "type": "text",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 4},
                "options": {"mode": "markdown", "content": "Hello"},
            }
        ],
        "links": [
            {
                "type": "link",
                "title": "Runbook",
                "url": "https://example.com/runbook",
            }
        ],
    }

    with tempfile.TemporaryDirectory() as output_dir:
        result, _ = translate_dashboard(dashboard, Path(output_dir))

    rows = sum(1 for panel in result.panel_results if panel.grafana_type == "row")
    disposition_total = (
        result.migrated
        + result.migrated_with_warnings
        + result.requires_manual
        + result.not_feasible
        + result.skipped
        - rows
    )
    assert result.total_panels - rows == disposition_total == 2


def test_datadog_image_zoom_sizing_maps_to_cover():
    widget = NormalizedWidget(
        id="image-1",
        title="Architecture",
        widget_type="image",
        raw_definition={
            "url": "https://example.com/architecture.png",
            "sizing": "zoom",
        },
    )
    plan = plan_widget(widget)
    result = translate_widget(widget, plan, field_map=None)  # type: ignore[arg-type]

    panel = _build_yaml_panel(widget, result, data_view="metrics-*")

    assert panel is not None
    assert panel["image"]["fit"] == "cover"


def test_datadog_image_verification_recommends_native_image():
    result = SimpleNamespace(
        status="ok",
        backend="image",
        kibana_type="image",
        dd_widget_type="image",
        query_language="datadog_widget",
    )

    candidates = build_datadog_target_candidates(result)

    assert candidates[0]["target"] == "native_image_panel"


def test_datadog_image_scale_down_is_warning():
    widget = NormalizedWidget(
        id="image-2",
        title="Topology",
        widget_type="image",
        raw_definition={
            "url": "https://example.com/topology.png",
            "sizing": "scale-down",
        },
    )
    plan = plan_widget(widget)
    result = translate_widget(widget, plan, field_map=None)  # type: ignore[arg-type]

    panel = _build_yaml_panel(widget, result, data_view="metrics-*")

    assert panel is not None
    assert panel["image"]["fit"] == "contain"
    assert result.status == "warning"
    assert "image sizing scale-down approximated as contain" in result.warnings


def test_grafana_link_context_forwarding_loss_is_reported():
    dashboard = {
        "title": "Context links",
        "uid": "context-links",
        "panels": [],
        "links": [
            {
                "type": "link",
                "title": "Runbook",
                "url": "https://example.com/runbook",
                "includeVars": True,
                "keepTime": True,
            }
        ],
    }

    with tempfile.TemporaryDirectory() as output_dir:
        result, _ = translate_dashboard(dashboard, Path(output_dir))

    links_result = next(panel for panel in result.panel_results if panel.kibana_type == "links")
    assert links_result.status == "migrated_with_warnings"
    assert any("template variables" in reason for reason in links_result.reasons)
    assert any("time range" in reason for reason in links_result.reasons)
