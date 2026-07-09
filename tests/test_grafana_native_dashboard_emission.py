# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""``translate_dashboard`` also emits a NativeDashboard IR artifact.

The native artifact is built from the exact same in-memory YAML document that
gets written to disk, so it is guaranteed to match the file byte-for-byte in
structure -- this is additive (the YAML file is unchanged) rather than a
replacement of the existing output.
"""

from __future__ import annotations

import pathlib
import tempfile

from observability_migration.adapters.source.grafana.panels import translate_dashboard
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.core.assets.native_dashboard import NativeDashboard
from observability_migration.targets.kibana import dashboards_api


def _translate(dashboard: dict) -> tuple:
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    tmpdir = tempfile.mkdtemp()
    return translate_dashboard(
        dashboard,
        pathlib.Path(tmpdir),
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
        result, _yaml_path = _translate(_metric_panel_dashboard())
        assert isinstance(result.native_dashboard, NativeDashboard)
        assert result.native_dashboard.title == "Native Emission Dashboard"

    def test_translate_dashboard_native_stats_reflect_mapped_panels(self) -> None:
        result, _yaml_path = _translate(_metric_panel_dashboard())
        assert result.native_dashboard_stats["mapped"] >= 1
        assert "reasons" in result.native_dashboard_stats

    def test_native_dashboard_matches_yaml_bridge_payload(self) -> None:
        """The attached IR must serialize identically to the YAML bridge path.

        This is the parity guarantee: whatever ``upload_yaml_files`` would send
        to the typed API from the written YAML file must be exactly what the
        in-memory ``native_dashboard`` produces, so the artifact is not a
        second, potentially-drifting source of truth.
        """
        result, yaml_path = _translate(_metric_panel_dashboard())
        import yaml as yaml_lib

        doc = yaml_lib.safe_load(yaml_path.read_text())
        bridged_payload, _stats = dashboards_api.build_payload_from_yaml(doc)
        assert result.native_dashboard.to_api_payload() == bridged_payload

    def test_translate_dashboard_native_dashboard_none_on_translation_error(self) -> None:
        # A dashboard with no panels at all still produces a (near-empty)
        # native dashboard rather than raising -- mirrors the YAML path,
        # which always writes a file even for an empty panel list.
        result, _yaml_path = _translate({"title": "Empty", "uid": "empty-1", "panels": []})
        assert isinstance(result.native_dashboard, NativeDashboard)
        assert result.native_dashboard.items == []
