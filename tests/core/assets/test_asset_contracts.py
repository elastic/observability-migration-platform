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
