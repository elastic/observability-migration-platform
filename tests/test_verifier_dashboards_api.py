# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the typed Kibana Dashboards API conformance oracle."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import dashboards_api  # noqa: E402


def _panel(
    *,
    title: str = "Requests",
    kind: str = "esql",
    chart_type: str = "line",
    query: str = "FROM metrics-* | STATS value = COUNT() BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), service.name",
):
    config = {"content": "hello"} if kind == "markdown" else {
        "type": chart_type,
        "query": query,
        "dimension": {"field": "time_bucket"},
        "metrics": [{"field": "value"}],
        "breakdown": {"field": "service.name"},
    }
    return {
        "title": title,
        "visual_ir": {
            "layout": {"x": 1, "y": 2, "w": 24, "h": 8},
            "presentation": {"kind": kind, "config": config},
        },
    }


def _report(panels):
    return {"dashboards": [{"title": "D", "panels": panels}]}


class TestPanelMapping:
    def test_xy_panel_maps_to_typed_vis_payload(self) -> None:
        api_panel, findings = dashboards_api.api_panel_from_report_panel("D", _panel())
        assert findings == []
        assert api_panel is not None
        assert api_panel["type"] == "vis"
        assert api_panel["grid"] == {"x": 1, "y": 2, "w": 24, "h": 8}
        cfg = api_panel["config"]
        assert cfg["type"] == "xy"
        layer = cfg["layers"][0]
        assert layer["type"] == "line"
        assert layer["data_source"]["type"] == "esql"
        assert layer["x"]["column"] == "time_bucket"
        assert layer["y"] == [{"column": "value"}]
        assert layer["breakdown_by"] == {"column": "service.name"}

    def test_metric_panel_maps_to_metric_payload(self) -> None:
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D", _panel(chart_type="metric")
        )
        assert findings == []
        assert api_panel is not None
        assert api_panel["config"]["type"] == "metric"
        assert api_panel["config"]["metrics"] == [{"type": "primary", "column": "value"}]

    def test_markdown_maps_to_markdown_panel(self) -> None:
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D", _panel(kind="markdown")
        )
        assert findings == []
        assert api_panel is not None
        assert api_panel["type"] == "markdown"
        assert api_panel["config"]["content"] == "hello"

    def test_gauge_panel_maps_to_gauge_payload(self) -> None:
        # Shape confirmed accepted by the native Dashboards API on 9.5.0:
        # config{type:gauge, data_source:esql, metric:{column}}.
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D", _panel(chart_type="gauge")
        )
        assert findings == []
        assert api_panel is not None
        cfg = api_panel["config"]
        assert cfg["type"] == "gauge"
        assert cfg["data_source"]["type"] == "esql"
        assert cfg["metric"] == {"column": "value"}

    def test_pie_panel_maps_to_pie_payload_with_group_by(self) -> None:
        # config{type:pie, data_source:esql, metrics:[{column}], group_by:[{column}]}.
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D", _panel(chart_type="pie")
        )
        assert findings == []
        assert api_panel is not None
        cfg = api_panel["config"]
        assert cfg["type"] == "pie"
        assert cfg["data_source"]["type"] == "esql"
        assert cfg["metrics"] == [{"column": "value"}]
        assert cfg["group_by"] == [{"column": "service.name"}]

    def test_pie_without_breakdown_omits_group_by(self) -> None:
        panel = _panel(chart_type="pie")
        panel["visual_ir"]["presentation"]["config"].pop("breakdown", None)
        api_panel, findings = dashboards_api.api_panel_from_report_panel("D", panel)
        assert findings == []
        assert api_panel is not None
        assert "group_by" not in api_panel["config"]

    def test_datatable_remains_unmapped_on_9_5(self) -> None:
        # The native Dashboards API on 9.5.0 has no ES|QL data_table variant
        # (its branches require a data_view source), so it stays an honest gap.
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D", _panel(chart_type="datatable")
        )
        assert api_panel is None
        assert len(findings) == 1
        assert findings[0].category == "unsupported_by_api_oracle"

    def test_datadog_esql_query_panel_maps_without_visual_ir(self) -> None:
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D",
            {
                "title": "Datadog Requests",
                "kibana_type": "xy",
                "esql_query": "FROM metrics-* | STATS value = AVG(metric) BY time_bucket, service.name",
                "query_ir": {
                    "output_metric_field": "value",
                    "output_group_fields": ["time_bucket", "service.name"],
                },
            },
        )
        assert findings == []
        assert api_panel is not None
        layer = api_panel["config"]["layers"][0]
        assert layer["data_source"]["query"].startswith("FROM metrics-*")
        assert layer["x"] == {"column": "time_bucket"}
        assert layer["y"] == [{"column": "value"}]
        assert layer["breakdown_by"] == {"column": "service.name"}

    def test_explicit_markdown_with_retained_query_maps_as_markdown(self) -> None:
        panel = _panel(kind="markdown")
        panel["esql_query"] = "FROM stale-* | LIMIT 1"
        api_panel, findings = dashboards_api.api_panel_from_report_panel("D", panel)
        assert findings == []
        assert api_panel is not None
        assert api_panel["type"] == "markdown"

    def test_datadog_yaml_panel_config_maps_without_visual_ir(self) -> None:
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D",
            {
                "title": "Datadog Requests",
                "kibana_type": "xy",
                "esql_query": "FROM metrics-* | STATS value = AVG(metric) BY time_bucket, service.name",
                "yaml_panel": {
                    "esql": {
                        "type": "line",
                        "query": "FROM metrics-* | STATS value = AVG(metric) BY time_bucket, service.name",
                        "dimension": {"field": "time_bucket"},
                        "metrics": [{"field": "value"}],
                        "breakdown": {"field": "service.name"},
                    }
                },
            },
        )
        assert findings == []
        assert api_panel is not None
        layer = api_panel["config"]["layers"][0]
        assert layer["breakdown_by"] == {"column": "service.name"}

    def test_unsupported_chart_is_info_not_guess(self) -> None:
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D", _panel(chart_type="heatmap")
        )
        assert api_panel is None
        assert len(findings) == 1
        assert findings[0].category == "unsupported_by_api_oracle"
        assert findings[0].severity == "info"


class TestPayloadAndValidation:
    def test_build_dashboard_payload_collects_supported_panels(self) -> None:
        payload, findings = dashboards_api.build_dashboard_payload(
            _report([_panel(), _panel(chart_type="heatmap")])
        )
        assert payload["title"] == "vf-conformance-D"
        assert len(payload["panels"]) == 1
        assert dashboards_api.mapped_panel_count(payload) == 1
        assert len(findings) == 1
        assert findings[0].category == "unsupported_by_api_oracle"

    def test_unsupported_budget_can_fail_low_coverage(self) -> None:
        payload, findings = dashboards_api.build_dashboard_payload(
            _report([_panel(), _panel(chart_type="heatmap"), _panel(chart_type="heatmap")])
        )
        findings = dashboards_api.apply_coverage_budget(
            findings,
            mapped_panels=dashboards_api.mapped_panel_count(payload),
            max_unsupported=1,
            min_mapped_panels=2,
        )
        cats = [finding.category for finding in findings]
        assert "unsupported_budget_exceeded" in cats
        assert "mapped_panel_budget_not_met" in cats

    def test_summary_reports_mapped_and_unsupported_counts(self) -> None:
        payload, findings = dashboards_api.build_dashboard_payload(
            _report([_panel(), _panel(chart_type="heatmap")])
        )
        summary = dashboards_api.summarize(
            findings, mapped_panels=dashboards_api.mapped_panel_count(payload)
        )
        assert summary["mapped_panels"] == 1
        assert summary["unsupported"] == 1
        assert summary["errors"] == 0

    def test_validate_payload_deletes_successful_scratch_dashboard(self) -> None:
        calls = []

        def api_call(method, path, body=None):
            calls.append((method, path, body))
            if method == "POST":
                return 200, {"id": "scratch-1"}
            return 204, {}

        findings = dashboards_api.validate_payload(
            {"title": "t", "panels": [_panel()]}, api_call=api_call
        )
        assert findings == []
        assert calls[0][0:2] == ("POST", "/api/dashboards")
        assert calls[1][0:2] == ("DELETE", "/api/dashboards/scratch-1")

    def test_validate_payload_reports_api_400(self) -> None:
        def api_call(method, path, body=None):
            return 400, {"message": "panel config rejected"}

        findings = dashboards_api.validate_payload(
            {"title": "t", "panels": [_panel()]}, api_call=api_call
        )
        assert len(findings) == 1
        assert findings[0].category == "dashboards_api_rejected"
        assert findings[0].severity == "error"
        assert "panel config rejected" in findings[0].message

    def test_validate_payload_per_panel_pinpoints_rejected_panel(self) -> None:
        calls = []

        def api_call(method, path, body=None):
            calls.append((method, path, body))
            title = body["panels"][0]["config"].get("title") if method == "POST" else ""
            if title == "bad":
                return 400, {"message": "bad panel rejected"}
            if method == "POST":
                return 200, {"id": f"scratch-{title}"}
            return 204, {}

        good, _ = dashboards_api.api_panel_from_report_panel("D", _panel(title="good"))
        bad, _ = dashboards_api.api_panel_from_report_panel("D", _panel(title="bad"))
        findings = dashboards_api.validate_payload_per_panel(
            {"title": "dash", "panels": [good, bad]}, api_call=api_call
        )
        assert len(findings) == 1
        assert findings[0].category == "dashboards_api_rejected"
        assert findings[0].panel == "bad"
        assert findings[0].evidence["panel_index"] == 1
        assert ("DELETE", "/api/dashboards/scratch-good", None) in calls
        assert all(call[1] != "/api/dashboards/scratch-bad" for call in calls)

    def test_validate_report_supports_per_panel_mode(self) -> None:
        post_count = 0

        def api_call(method, path, body=None):
            nonlocal post_count
            if method == "POST":
                post_count += 1
                return 200, {"id": f"scratch-{post_count}"}
            return 204, {}

        findings = dashboards_api.validate_report(
            _report([_panel(title="a"), _panel(title="b")]),
            api_call=api_call,
            per_panel=True,
        )
        assert findings == []
        assert post_count == 2

    def test_validate_report_combines_local_and_remote_findings(self) -> None:
        def api_call(method, path, body=None):
            return 200, {"id": "scratch-1"}

        findings = dashboards_api.validate_report(
            _report([_panel(), _panel(chart_type="heatmap")]), api_call=api_call
        )
        assert [f.category for f in findings] == ["unsupported_by_api_oracle"]
        assert dashboards_api.summarize(findings)["errors"] == 0

