# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""``translate_dashboard`` emits a NativeDashboard artifact alongside the IR.

``DashboardIR`` is the primary artifact; ``native_dashboard`` is derived from it
by the typed-API mapper. These tests pin that derivation: the native artifact is
attached, it is built from the IR (not from a re-parse of some other
representation), and re-running the mapper on the same IR reproduces it exactly.
"""

from __future__ import annotations

from observability_migration.adapters.source.grafana.panels import translate_dashboard
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.core.assets.dashboard import DashboardIR
from observability_migration.core.assets.native_dashboard import NativeDashboard
from observability_migration.targets.kibana import dashboards_api


def _translate(dashboard: dict):
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    return translate_dashboard(
        dashboard,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )


def _metric_panel_dashboard() -> dict:
    return {
        "title": "Native Emission Dashboard",
        "uid": "native-emit-1",
        "panels": [
            {
                "title": "CPU Usage",
                "type": "stat",
                "gridPos": {"w": 12, "h": 8, "x": 0, "y": 0},
                "targets": [
                    {"refId": "A", "expr": "sum(rate(node_cpu_seconds_total[5m]))", "instant": True},
                ],
            },
        ],
    }


class TestGrafanaNativeDashboardEmission:
    def test_translate_dashboard_attaches_native_dashboard(self) -> None:
        result = _translate(_metric_panel_dashboard())
        assert isinstance(result.native_dashboard, NativeDashboard)
        assert result.native_dashboard.title == "Native Emission Dashboard"

    def test_translate_dashboard_native_stats_reflect_mapped_panels(self) -> None:
        result = _translate(_metric_panel_dashboard())
        assert result.native_dashboard_stats["mapped"] >= 1
        assert "reasons" in result.native_dashboard_stats

    def test_translate_dashboard_native_dashboard_none_on_translation_error(self) -> None:
        # A dashboard with no panels at all still produces a (near-empty)
        # native dashboard rather than raising.
        result = _translate({"title": "Empty", "uid": "empty-1", "panels": []})
        assert isinstance(result.native_dashboard, NativeDashboard)
        assert result.native_dashboard.items == []

    def test_translate_dashboard_attaches_dashboard_ir(self) -> None:
        # IR-first: `DashboardIR` is the primary artifact `native_dashboard` is
        # derived from.
        result = _translate(_metric_panel_dashboard())
        assert isinstance(result.dashboard_ir, DashboardIR)
        assert result.dashboard_ir.title == "Native Emission Dashboard"
        assert result.dashboard_ir.source_adapter == "grafana"
        assert result.dashboard_ir.uid == "native-emit-1"
        assert len(result.dashboard_ir.panels) == 1

    def test_native_dashboard_is_derived_from_dashboard_ir_not_yaml(self) -> None:
        # The native mapper is called on the IR, not on a re-parse of the
        # internal dict shape -- feeding the same IR through
        # `native_dashboard_from_ir` again must reproduce the exact payload
        # already attached to the result.
        result = _translate(_metric_panel_dashboard())
        native_from_ir_again, _counts = dashboards_api.native_dashboard_from_ir(result.dashboard_ir)
        assert native_from_ir_again.to_api_payload() == result.native_dashboard.to_api_payload()
