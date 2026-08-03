# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for shared asset contracts.

Verifies that all canonical IRs can be instantiated, serialized,
and composed into a DashboardIR.
"""

import unittest

from observability_migration.core.assets import (
    AlertingIR,
    AnnotationIR,
    AssetStatus,
    ControlIR,
    DashboardIR,
    LinkIR,
    PanelIR,
    QueryIR,
    TargetQueryPlan,
    TransformIR,
    VisualIR,
    build_alerting_ir_from_grafana,
    build_operational_ir,
    build_query_ir,
    infer_output_shape,
)


class TestAssetStatus(unittest.TestCase):
    def test_grafana_mapping(self):
        self.assertEqual(AssetStatus.from_grafana("migrated"), AssetStatus.TRANSLATED)
        self.assertEqual(AssetStatus.from_grafana("migrated_with_warnings"), AssetStatus.TRANSLATED_WITH_WARNINGS)
        self.assertEqual(AssetStatus.from_grafana("requires_manual"), AssetStatus.MANUAL_REQUIRED)
        self.assertEqual(AssetStatus.from_grafana("not_feasible"), AssetStatus.NOT_FEASIBLE)
        self.assertEqual(AssetStatus.from_grafana("unknown_status"), AssetStatus.NOT_FEASIBLE)

    def test_datadog_mapping(self):
        self.assertEqual(AssetStatus.from_datadog("ok"), AssetStatus.TRANSLATED)
        self.assertEqual(AssetStatus.from_datadog("warning"), AssetStatus.TRANSLATED_WITH_WARNINGS)
        self.assertEqual(AssetStatus.from_datadog("blocked"), AssetStatus.NOT_FEASIBLE)

    def test_string_value(self):
        self.assertEqual(AssetStatus.TRANSLATED.value, "translated")
        self.assertEqual(str(AssetStatus.TRANSLATED), "AssetStatus.TRANSLATED")


class TestQueryIR(unittest.TestCase):
    def test_build_query_ir_from_context(self):
        class FakeContext:
            query_language = "promql"
            promql_expr = "rate(http_requests_total[5m])"
            clean_expr = ""
            panel_type = "timeseries"
            datasource_type = "prometheus"
            datasource_uid = ""
            datasource_name = ""
            metric_name = "http_requests_total"
            inner_func = "rate"
            range_window = "5m"
            outer_agg = ""
            group_labels = []
            label_filters = []
            index = "metrics-*"
            esql_query = ""
            output_metric_field = ""
            output_group_fields = []
            source_type = ""
            metadata = {}
            warnings = ["Approximation: counter resets not tracked"]
            fragment = None

        qir = build_query_ir(FakeContext())
        self.assertEqual(qir.source_language, "promql")
        self.assertEqual(qir.output_shape, "time_series")
        self.assertEqual(len(qir.semantic_losses), 1)

    def test_gauge_fallback_warning_is_semantic_loss(self):
        # The rate()/irate()/increase() gauge degrade rewrites the panel to a
        # different function family (AVG_OVER_TIME / MAX_OVER_TIME), changing
        # the value scale of the result. That is a semantic loss and must
        # surface in semantic_losses, not just warnings.
        class FakeContext:
            query_language = "promql"
            promql_expr = "sum(rate(http_request_duration_seconds_bucket[5m])) by (le)"
            clean_expr = ""
            panel_type = "heatmap"
            datasource_type = "prometheus"
            datasource_uid = ""
            datasource_name = ""
            metric_name = "http_request_duration_seconds_bucket"
            inner_func = "rate"
            range_window = "5m"
            outer_agg = "sum"
            group_labels = ["le"]
            label_filters = []
            index = "metrics-*"
            esql_query = ""
            output_metric_field = ""
            output_group_fields = []
            source_type = ""
            metadata = {}
            warnings = [
                "Source PromQL used rate() but http_request_duration_seconds_bucket "
                "is typed as gauge in the target index; rendered as AVG_OVER_TIME "
                "instead. Fix the ingest mapping to mark this field as a counter "
                "to get a true rate.",
                "Source PromQL used increase() but node_vmstat_oom_kill is typed "
                "as gauge in the target index; rendered as MAX_OVER_TIME "
                "(cumulative ceiling) instead. Fix the ingest mapping to mark "
                "this field as a counter to recover the true increase over the "
                "window.",
            ]
            fragment = None

        qir = build_query_ir(FakeContext())
        self.assertEqual(qir.semantic_losses, qir.warnings)

    def test_infer_output_shape_table(self):
        self.assertEqual(infer_output_shape("table", [], "promql"), "table")

    def test_infer_output_shape_single_value(self):
        self.assertEqual(infer_output_shape("stat", [], "promql"), "single_value")

    def test_to_dict(self):
        qir = QueryIR(source_language="promql")
        d = qir.to_dict()
        self.assertIn("source_language", d)
        self.assertIn("semantic_losses", d)


class TestVisualIR(unittest.TestCase):
    def test_from_yaml_panel(self):
        yaml_panel = {
            "title": "CPU Usage",
            "size": {"w": 12, "h": 8},
            "position": {"x": 0, "y": 0},
            "esql": {"type": "xy", "query": "FROM metrics"},
        }
        vir = VisualIR.from_yaml_panel(yaml_panel, grafana_type="timeseries")
        self.assertEqual(vir.title, "CPU Usage")
        self.assertEqual(vir.layout.w, 12)
        self.assertEqual(vir.presentation.kind, "esql")

    def test_to_yaml_panel(self):
        vir = VisualIR(
            title="Test",
            layout=VisualIR.__dataclass_fields__["layout"].default_factory(),
        )
        vir.layout.w = 12
        vir.layout.h = 8
        panel = vir.to_yaml_panel()
        self.assertEqual(panel["title"], "Test")
        self.assertEqual(panel["size"]["w"], 12)


class TestPanelIRYamlRoundTrip(unittest.TestCase):
    def test_leaf_panel_round_trips_esql_config_verbatim(self):
        entry = {
            "title": "CPU Usage",
            "size": {"w": 24, "h": 8},
            "position": {"x": 0, "y": 0},
            "esql": {
                "type": "xy",
                "query": "FROM metrics-* | STATS avg(value) BY time_bucket",
                "primary": {"field": "avg(value)"},
                "dimension": {"field": "time_bucket", "data_type": "date"},
            },
        }
        panel = PanelIR.from_yaml_panel_entry(entry)
        self.assertEqual(panel.kind, "panel")
        self.assertEqual(panel.title, "CPU Usage")
        self.assertIsNotNone(panel.visual)
        self.assertEqual(panel.visual.presentation.kind, "esql")
        self.assertEqual(panel.to_yaml_panel_entry(), entry)

    def test_leaf_panel_round_trips_hide_title(self):
        entry = {
            "title": "Uptime",
            "size": {"w": 12, "h": 6},
            "position": {"x": 0, "y": 0},
            "esql": {"type": "metric", "query": "FROM metrics-*"},
            "hide_title": True,
        }
        panel = PanelIR.from_yaml_panel_entry(entry)
        self.assertTrue(panel.hide_title)
        self.assertEqual(panel.to_yaml_panel_entry(), entry)

    def test_markdown_panel_round_trips(self):
        entry = {
            "title": "Notes",
            "size": {"w": 24, "h": 4},
            "position": {"x": 0, "y": 0},
            "markdown": {"content": "*(migrated text panel)*"},
        }
        panel = PanelIR.from_yaml_panel_entry(entry)
        self.assertEqual(panel.visual.presentation.kind, "markdown")
        self.assertEqual(panel.to_yaml_panel_entry(), entry)

    def test_lens_panel_round_trips(self):
        # Datadog still emits a small number of Lens-backed panels; the IR
        # round-trip must preserve the ``lens`` block verbatim so Phase 2
        # Datadog emit is not lossier than the YAML path.
        entry = {
            "title": "Lens Metric",
            "size": {"w": 12, "h": 6},
            "position": {"x": 0, "y": 0},
            "lens": {
                "type": "metric",
                "data_view": "metrics-*",
                "primary": {"aggregation": "average", "field": "value"},
            },
        }
        panel = PanelIR.from_yaml_panel_entry(entry)
        self.assertEqual(panel.visual.presentation.kind, "lens")
        self.assertEqual(panel.to_yaml_panel_entry(), entry)

    def test_section_round_trips_nested_leaf_panels(self):
        entry = {
            "title": "Overview",
            "section": {
                "collapsed": True,
                "panels": [
                    {
                        "title": "Panel A",
                        "size": {"w": 12, "h": 8},
                        "position": {"x": 0, "y": 0},
                        "esql": {"type": "metric", "query": "FROM metrics-*"},
                    },
                    {
                        "title": "Panel B",
                        "size": {"w": 12, "h": 8},
                        "position": {"x": 12, "y": 0},
                        "esql": {"type": "gauge", "query": "FROM metrics-*"},
                    },
                ],
            },
        }
        panel = PanelIR.from_yaml_panel_entry(entry)
        self.assertEqual(panel.kind, "section")
        self.assertTrue(panel.collapsed)
        self.assertEqual(len(panel.children), 2)
        self.assertEqual(panel.to_yaml_panel_entry(), entry)


class TestControlIRYamlRoundTrip(unittest.TestCase):
    def test_esql_control_preserves_unmodeled_keys_via_source_extension(self):
        raw = {
            "type": "esql",
            "label": "Region",
            "variable_name": "region",
            "variable_type": "values",
            "query": "FROM metrics-* | STATS BY region",
            "defaults": ["us-east"],
            "multiple": False,
        }
        control = ControlIR.from_yaml_control(raw)
        self.assertEqual(control.variable_name, "region")
        self.assertEqual(control.selected_options, ["us-east"])
        self.assertFalse(control.multiple)
        self.assertEqual(control.to_yaml_control(), raw)

    def test_control_label_override_after_polish_flows_through(self):
        raw = {"type": "esql", "label": "region", "variable_name": "region", "query": "FROM metrics-*"}
        control = ControlIR.from_yaml_control(raw)
        control.label = "Region"
        rendered = control.to_yaml_control()
        self.assertEqual(rendered["label"], "Region")
        self.assertEqual(rendered["variable_name"], "region")

    def test_options_list_control_round_trips(self):
        raw = {
            "type": "options_list_control",
            "label": "Host",
            "data_view_id": "metrics-*",
            "field_name": "host.name",
            "defaults": ["host-a", "host-b"],
        }
        control = ControlIR.from_yaml_control(raw)
        self.assertEqual(control.data_view, "metrics-*")
        self.assertEqual(control.field_name, "host.name")
        self.assertEqual(control.to_yaml_control(), raw)

    def test_synthesized_control_without_source_extension_builds_from_typed_fields(self):
        control = ControlIR(
            kind="esql",
            label="job",
            variable_name="job",
            query="FROM metrics-* | STATS BY job",
            selected_options=[".*"],
            multiple=False,
        )
        rendered = control.to_yaml_control()
        self.assertEqual(rendered["type"], "esql")
        self.assertEqual(rendered["variable_name"], "job")
        self.assertEqual(rendered["defaults"], [".*"])
        self.assertEqual(rendered["multiple"], False)
        self.assertEqual(rendered["label"], "job")


class TestDashboardIRYamlRoundTrip(unittest.TestCase):
    def _sample_dashboard_dict(self):
        return {
            "name": "Sample Dashboard",
            "description": "Migrated from Grafana",
            "minimum_kibana_version": "8.15.0",
            "settings": {"sync": {"cursor": True}},
            "panels": [
                {
                    "title": "Panel A",
                    "size": {"w": 24, "h": 8},
                    "position": {"x": 0, "y": 0},
                    "esql": {"type": "metric", "query": "FROM metrics-*"},
                },
                {
                    "title": "Section 1",
                    "section": {
                        "collapsed": False,
                        "panels": [
                            {
                                "title": "Panel B",
                                "size": {"w": 24, "h": 8},
                                "position": {"x": 0, "y": 0},
                                "esql": {"type": "datatable", "query": "FROM metrics-*"},
                            },
                        ],
                    },
                },
            ],
            "filters": [{"exists": "host.name"}],
            "controls": [
                {
                    "type": "esql",
                    "label": "Region",
                    "variable_name": "region",
                    "query": "FROM metrics-* | STATS BY region",
                    "defaults": ["us-east"],
                },
            ],
        }

    def test_round_trip_is_lossless(self):
        raw = self._sample_dashboard_dict()
        dashboard_ir = DashboardIR.from_yaml_dict(raw, source_adapter="grafana")
        self.assertEqual(dashboard_ir.source_adapter, "grafana")
        self.assertEqual(len(dashboard_ir.panels), 2)
        self.assertEqual(dashboard_ir.panels[1].kind, "section")
        self.assertEqual(len(dashboard_ir.controls), 1)
        self.assertEqual(dashboard_ir.to_yaml_dict(), raw)

    def test_from_yaml_dict_defaults_on_missing_dashboard(self):
        dashboard_ir = DashboardIR.from_yaml_dict({})
        self.assertEqual(dashboard_ir.title, "")
        self.assertEqual(dashboard_ir.panels, [])
        self.assertEqual(dashboard_ir.controls, [])

    def test_to_yaml_dict_omits_empty_optional_sections(self):
        dashboard_ir = DashboardIR(title="Minimal")
        rendered = dashboard_ir.to_yaml_dict()
        self.assertEqual(rendered, {"name": "Minimal", "panels": []})


class TestOperationalIR(unittest.TestCase):
    def test_build_operational_ir(self):

        class FakeResult:
            status = "migrated"
            confidence = 0.95
            source_panel_id = "42"
            readiness = "ready"
            recommended_target = "esql"
            post_validation_action = ""
            post_validation_message = ""
            datasource_type = "prometheus"
            datasource_uid = "prom-1"
            datasource_name = "Prometheus"
            query_language = "promql"
            runtime_rollups = []

        oir = build_operational_ir(
            FakeResult(),
            dashboard_title="Test",
        )
        self.assertEqual(oir.status, "migrated")
        self.assertEqual(oir.lineage.dashboard_title, "Test")
        self.assertEqual(oir.confidence, 0.95)


class TestAlertingIR(unittest.TestCase):
    def test_build_from_grafana(self):
        air = build_alerting_ir_from_grafana({
            "alert_name": "High CPU",
            "dashboard_uid": "abc",
            "panel": "CPU Panel",
            "suggested_kibana_rule_type": "threshold",
            "conditions_description": ["avg() gt [80]"],
            "frequency": "1m",
            "no_data_state": "alerting",
        })
        self.assertEqual(air.kind, "grafana_legacy")
        self.assertEqual(air.target_candidate, "threshold")
        self.assertEqual(air.no_data_policy, "alerting")

    def test_status_default(self):
        air = AlertingIR()
        self.assertEqual(air.status, AssetStatus.MANUAL_REQUIRED)


class TestDashboardIR(unittest.TestCase):
    def test_composition(self):
        dash = DashboardIR(
            title="My Dashboard",
            source_adapter="grafana",
            panels=[
                PanelIR(panel_id="1", title="Panel 1", status=AssetStatus.TRANSLATED),
                PanelIR(panel_id="2", title="Panel 2", status=AssetStatus.NOT_FEASIBLE),
            ],
            controls=[ControlIR(name="interval")],
            alerts=[AlertingIR(name="Alert 1")],
            annotations=[AnnotationIR(name="Deploy")],
            links=[LinkIR(title="Home")],
            transforms=[TransformIR(kind="filterByName")],
        )
        d = dash.to_dict()
        self.assertEqual(len(d["panels"]), 2)
        self.assertEqual(d["panels"][0]["status"], "translated")
        self.assertEqual(len(d["controls"]), 1)
        self.assertEqual(len(d["alerts"]), 1)

    def test_source_extension(self):
        dash = DashboardIR(
            title="Test",
            source_extension={"grafana_uid": "abc123"},
        )
        d = dash.to_dict()
        self.assertEqual(d["source_extension"]["grafana_uid"], "abc123")


class TestTargetQueryPlan(unittest.TestCase):
    def test_basic(self):
        plan = TargetQueryPlan(
            target_index="metrics-*",
            target_query="FROM metrics-* | STATS count()",
            target_language="esql",
        )
        d = plan.to_dict()
        self.assertEqual(d["target_language"], "esql")


class TestTransformIR(unittest.TestCase):
    def test_to_dict_status(self):
        t = TransformIR(kind="filterByName", status=AssetStatus.MANUAL_REQUIRED)
        d = t.to_dict()
        self.assertEqual(d["status"], "manual_required")


if __name__ == "__main__":
    unittest.main()
