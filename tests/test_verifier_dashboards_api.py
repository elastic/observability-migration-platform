# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the typed Kibana Dashboards API conformance oracle."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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


def _esql_panel(config, *, title="P"):
    """A report panel carrying an arbitrary emitted ``esql`` presentation config."""
    return {
        "title": title,
        "visual_ir": {
            "layout": {"x": 0, "y": 0, "w": 24, "h": 8},
            "presentation": {"kind": "esql", "config": {"query": "FROM metrics-*", **config}},
        },
    }


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

    def test_visual_ir_panels_delegate_to_production_mapper(self, monkeypatch) -> None:
        calls = []
        production_panel = {
            "grid": {"x": 9, "y": 8, "w": 7, "h": 6},
            "type": "vis",
            "config": {"type": "metric", "title": "from production"},
        }

        def fake_production_map(panel):
            calls.append(panel)
            return SimpleNamespace(api_panel=production_panel, reason="", kind="metric")

        monkeypatch.setattr(dashboards_api, "_production_map_panel", fake_production_map, raising=False)

        api_panel, findings = dashboards_api.api_panel_from_report_panel("D", _panel(chart_type="metric"))

        assert calls, "verifier should call production mapper for standard visual_ir panels"
        assert findings == []
        assert api_panel == production_panel

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

    def test_datatable_maps_to_data_table_payload(self) -> None:
        # Verified live on 9.5.0: data_table has an ES|QL variant. The emitted
        # config carries ``metrics`` + ``breakdowns``; map to API metrics/rows.
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D",
            _esql_panel(
                {
                    "type": "datatable",
                    "metrics": [{"field": "value"}],
                    "breakdowns": [{"field": "service.name"}],
                }
            ),
        )
        assert findings == []
        assert api_panel is not None
        cfg = api_panel["config"]
        assert cfg["type"] == "data_table"
        assert cfg["data_source"]["type"] == "esql"
        assert cfg["metrics"] == [{"column": "value"}]
        assert cfg["rows"] == [{"column": "service.name"}]

    def test_datatable_defaults_a_row_when_no_columns(self) -> None:
        # data_table requires at least one of metrics/rows to render.
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D", _esql_panel({"type": "datatable"})
        )
        assert findings == []
        assert api_panel is not None
        cfg = api_panel["config"]
        assert "metrics" not in cfg
        assert cfg["rows"] == [{"column": "value"}]

    def test_heatmap_maps_to_heatmap_payload(self) -> None:
        # Emitted heatmap config carries ``x_axis``/``y_axis``/``metric``.
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D",
            _esql_panel(
                {
                    "type": "heatmap",
                    "x_axis": {"field": "time_bucket"},
                    "y_axis": {"field": "le"},
                    "metric": {"field": "bucket"},
                }
            ),
        )
        assert findings == []
        assert api_panel is not None
        cfg = api_panel["config"]
        assert cfg["type"] == "heatmap"
        assert cfg["data_source"]["type"] == "esql"
        assert cfg["x"] == {"column": "time_bucket"}
        assert cfg["metric"] == {"column": "bucket"}
        assert cfg["y"] == {"column": "le"}

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
        # legacy_metric is the one vis type with no ES|QL variant on 9.5.0.
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D", _panel(chart_type="legacy_metric")
        )
        assert api_panel is None
        assert len(findings) == 1
        assert findings[0].category == "unsupported_by_api_oracle"
        assert findings[0].severity == "info"


class TestSafeDisplayMetadata:
    def _map(self, config):
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D", _esql_panel(config)
        )
        assert findings == []
        assert api_panel is not None
        return api_panel["config"]

    def test_defaults_incomplete_duration_format_instead_of_dropping(self) -> None:
        # Verified live on Serverless 9.5.0: the API accepts a bare/partial
        # ``duration`` format on both the single-value metric schema and the
        # multi-column schema, so an incomplete ``from``/``to`` pair should be
        # defaulted, not silently dropped (that previously desynced this
        # oracle from the real API, which never rejects this shape).
        cfg = self._map(
            {
                "type": "metric",
                "primary": {
                    "field": "latency",
                    "label": "Latency",
                    "format": {"type": "duration", "from": "milliseconds"},
                },
            }
        )

        assert cfg["metrics"] == [{
            "type": "primary",
            "column": "latency",
            "label": "Latency",
            "format": {"type": "duration", "from": "milliseconds", "to": "humanize"},
        }]

    def test_xy_preserves_y2_legend_axis_horizontal_bar_and_breakdowns_list(self) -> None:
        cfg = self._map(
            {
                "type": "bar",
                "mode": "stacked",
                "horizontal": True,
                "dimension": {
                    "field": "time_bucket",
                    "label": "Time",
                    "format": {"type": "custom", "pattern": "HH:mm"},
                },
                "metrics": [
                    {
                        "field": "requests",
                        "label": "Requests",
                        "format": {"type": "number", "decimals": 0},
                        "color": {"type": "static", "color": "#54B399"},
                    },
                    {
                        "field": "errors",
                        "label": "Errors",
                        "axis": "y2",
                        "format": {"type": "percent", "decimals": 2},
                        "color": {"type": "auto"},
                    },
                ],
                "breakdowns": [
                    {
                        "field": "service.name",
                        "label": "Service",
                        "collapse_by": "sum",
                        "format": {"type": "custom", "pattern": "short"},
                        "color": {
                            "mode": "categorical",
                            "palette": "default",
                            "mapping": [],
                        },
                    }
                ],
                "legend": {"visibility": "hidden", "placement": "outside", "position": "right"},
                "axis": {
                    "x": {"title": {"text": "Time", "visible": True}, "scale": "temporal"},
                    "y": {"domain": {"type": "fit"}, "title": {"text": "Requests", "visible": True}},
                    "y2": {
                        "domain": {"type": "custom", "min": 0, "max": 100},
                        "title": {"text": "Errors", "visible": True},
                    },
                },
            }
        )

        layer = cfg["layers"][0]
        assert layer["type"] == "bar_horizontal_stacked"
        assert layer["x"] == {
            "column": "time_bucket",
            "label": "Time",
            "format": {"type": "custom", "pattern": "HH:mm"},
        }
        assert layer["y"] == [
            {
                "column": "requests",
                "label": "Requests",
                "format": {"type": "number", "decimals": 0},
                "color": {"type": "static", "color": "#54B399"},
            },
            {
                "column": "errors",
                "label": "Errors",
                "axis": "y2",
                "format": {"type": "percent", "decimals": 2},
                "color": {"type": "auto"},
            },
        ]
        assert layer["breakdown_by"] == {
            "column": "service.name",
            "label": "Service",
            "collapse_by": "sum",
            "format": {"type": "custom", "pattern": "short"},
        }
        assert cfg["legend"] == {"visibility": "hidden", "placement": "outside", "position": "right"}
        assert cfg["axis"]["y2"]["domain"] == {"type": "custom", "min": 0, "max": 100}

    def test_metric_preserves_secondary_and_breakdown_by(self) -> None:
        cfg = self._map(
            {
                "type": "metric",
                "primary": {
                    "field": "requests",
                    "label": "Requests",
                    "subtitle": "Total",
                    "format": {"type": "number", "decimals": 0},
                    "color": {"type": "static", "color": "#54B399"},
                    "apply_color_to": "value",
                },
                "secondary": {
                    "field": "error_rate",
                    "label": "Error rate",
                    "format": {"type": "percent", "decimals": 2},
                    "color": {"type": "none"},
                },
                "breakdown_by": {
                    "field": "service.name",
                    "label": "Service",
                    "columns": 4,
                    "collapse_by": "max",
                },
            }
        )

        assert cfg["metrics"] == [
            {
                "type": "primary",
                "column": "requests",
                "label": "Requests",
                "subtitle": "Total",
                "format": {"type": "number", "decimals": 0},
                "color": {"type": "static", "color": "#54B399"},
            },
            {
                "type": "secondary",
                "column": "error_rate",
                "label": "Error rate",
                "format": {"type": "percent", "decimals": 2},
                "color": {"type": "none"},
            },
        ]
        assert cfg["breakdown_by"] == {
            "column": "service.name",
            "label": "Service",
            "collapse_by": "max",
        }

    def test_gauge_preserves_bounds_ticks_title_and_shape(self) -> None:
        cfg = self._map(
            {
                "type": "gauge",
                "metric": {
                    "field": "memory_pct",
                    "label": "Memory",
                    "subtitle": "Used",
                    "format": {"type": "percent", "decimals": 1},
                    "color": {"type": "auto"},
                    "ticks": {"visible": True, "mode": "bands"},
                    "title": {"text": "Memory usage", "visible": True},
                },
                "minimum": {"field": "min_pct", "label": "Min"},
                "maximum": {"field": "max_pct", "label": "Max"},
                "goal": {"field": "goal_pct", "label": "Goal"},
                "styling": {"shape": {"type": "bullet", "orientation": "horizontal"}},
            }
        )

        assert cfg["metric"] == {
            "column": "memory_pct",
            "label": "Memory",
            "subtitle": "Used",
            "format": {"type": "percent", "decimals": 1},
            "color": {"type": "auto"},
            "min": {"column": "min_pct", "label": "Min"},
            "max": {"column": "max_pct", "label": "Max"},
            "goal": {"column": "goal_pct", "label": "Goal"},
            "ticks": {"visible": True, "mode": "bands"},
            "title": {"text": "Memory usage", "visible": True},
        }
        assert cfg["styling"] == {"shape": {"type": "bullet", "orientation": "horizontal"}}

    def test_datatable_preserves_split_metrics_by_and_rich_columns(self) -> None:
        cfg = self._map(
            {
                "type": "datatable",
                "metrics": [
                    {
                        "field": "requests",
                        "label": "Requests",
                        "format": {"type": "number", "decimals": 0},
                        "color": {"type": "auto"},
                        "alignment": "right",
                        "apply_color_to": "background",
                        "summary": {"type": "sum", "label": "Total"},
                        "width": 120,
                        "visible": True,
                    }
                ],
                "breakdowns": [
                    {
                        "field": "service.name",
                        "label": "Service",
                        "format": {"type": "custom", "pattern": "short"},
                        "alignment": "left",
                        "collapse_by": "avg",
                        "width": 200,
                    }
                ],
                "split_metrics_by": [
                    {
                        "field": "region",
                        "label": "Region",
                        "format": {"type": "custom", "pattern": "short"},
                    }
                ],
                "styling": {
                    "paging": 20,
                    "row_numbers": {"visible": True},
                    "sort_by": {"column_type": "metric", "index": 0, "direction": "desc"},
                },
            }
        )

        assert cfg["metrics"] == [
            {
                "column": "requests",
                "label": "Requests",
                "format": {"type": "number", "decimals": 0},
                "color": {"type": "auto"},
                "alignment": "right",
                "apply_color_to": "background",
                "summary": {"type": "sum", "label": "Total"},
                "width": 120,
                "visible": True,
            }
        ]
        assert cfg["rows"] == [
            {
                "column": "service.name",
                "label": "Service",
                "format": {"type": "custom", "pattern": "short"},
                "alignment": "left",
                "collapse_by": "avg",
                "width": 200,
            }
        ]
        assert cfg["split_metrics_by"] == [
            {
                "column": "region",
                "label": "Region",
                "format": {"type": "custom", "pattern": "short"},
            }
        ]
        assert "styling" not in cfg

    def test_heatmap_preserves_legend_and_axis(self) -> None:
        cfg = self._map(
            {
                "type": "heatmap",
                "x_axis": {
                    "field": "time_bucket",
                    "label": "Time",
                    "format": {"type": "custom", "pattern": "HH:mm"},
                },
                "y_axis": {"field": "le", "label": "Bucket"},
                "metric": {
                    "field": "count",
                    "label": "Count",
                    "format": {"type": "number", "decimals": 0},
                    "color": {"type": "auto"},
                },
                "legend": {"visibility": "visible", "size": "m", "truncate_after_lines": 2},
                "axis": {
                    "x": {
                        "scale": "temporal",
                        "sort": "asc",
                        "title": {"text": "Time", "visible": True},
                    },
                    "y": {"sort": "desc", "title": {"text": "Bucket", "visible": True}},
                },
            }
        )

        assert cfg["x"] == {
            "column": "time_bucket",
            "label": "Time",
            "format": {"type": "custom", "pattern": "HH:mm"},
        }
        assert cfg["metric"] == {
            "column": "count",
            "label": "Count",
            "format": {"type": "number", "decimals": 0},
            "color": {"type": "auto"},
        }
        assert cfg["legend"] == {"visibility": "visible", "truncate_after_lines": 2}
        assert cfg["axis"]["x"]["scale"] == "temporal"
        assert cfg["axis"]["y"]["title"] == {"text": "Bucket", "visible": True}

    def test_partition_metadata_and_mosaic_omits_group_breakdown_by(self) -> None:
        pie = self._map(
            {
                "type": "pie",
                "metrics": [
                    {
                        "field": "requests",
                        "label": "Requests",
                        "format": {"type": "number", "decimals": 0},
                        "color": {"type": "static", "color": "#54B399"},
                    }
                ],
                "breakdowns": [
                    {
                        "field": "service.name",
                        "label": "Service",
                        "format": {"type": "custom", "pattern": "short"},
                        "collapse_by": "sum",
                        "color": {
                            "mode": "categorical",
                            "palette": "default",
                            "mapping": [],
                        },
                    }
                ],
                "legend": {"visibility": "auto"},
                "styling": {
                    "donut_hole": "m",
                    "values": {"visible": True, "mode": "percentage", "percent_decimals": 1},
                },
            }
        )
        mosaic = self._map(
            {
                "type": "mosaic",
                "metric": {
                    "field": "requests",
                    "label": "Requests",
                    "format": {"type": "number", "decimals": 0},
                },
                "breakdowns": [{"field": "service.name"}, {"field": "region"}],
                "group_breakdown_by": [{"field": "region"}],
            }
        )

        assert pie["metrics"] == [
            {
                "column": "requests",
                "label": "Requests",
                "format": {"type": "number", "decimals": 0},
                "color": {"type": "static", "color": "#54B399"},
            }
        ]
        assert pie["group_by"] == [
            {
                "column": "service.name",
                "label": "Service",
                "format": {"type": "custom", "pattern": "short"},
                "collapse_by": "sum",
            }
        ]
        assert pie["legend"] == {"visibility": "auto"}
        assert pie["styling"]["values"] == {
            "visible": True,
            "mode": "percentage",
            "percent_decimals": 1,
        }
        assert mosaic["group_by"] == [{"column": "service.name"}]
        assert "group_breakdown_by" not in mosaic


class TestAllChartTypes:
    """Every emitted chart family has an ES|QL Dashboards API variant on 9.5.0."""

    def test_supported_set_covers_all_eleven_families(self) -> None:
        # 11 API chart families: xy (line/bar/area), metric, gauge, pie, treemap,
        # waffle, heatmap, data_table, tag_cloud, mosaic, region_map.
        supported = dashboards_api._SUPPORTED_ESQL_TYPES
        for config_type in (
            "line", "bar", "area", "metric", "gauge", "pie", "treemap",
            "waffle", "heatmap", "datatable", "tagcloud", "mosaic", "region_map",
        ):
            assert config_type in supported, config_type
        # data_table / tag_cloud spellings are also accepted as aliases.
        assert "data_table" in supported
        assert "tag_cloud" in supported

    def _map(self, config):
        api_panel, findings = dashboards_api.api_panel_from_report_panel(
            "D", _esql_panel(config)
        )
        assert findings == []
        assert api_panel is not None
        assert api_panel["type"] == "vis"
        return api_panel["config"]

    def test_line_maps_to_xy_line_series(self) -> None:
        cfg = self._map(
            {
                "type": "line",
                "dimension": {"field": "time_bucket"},
                "metrics": [{"field": "value"}],
                "breakdown": {"field": "service.name"},
            }
        )
        assert cfg["type"] == "xy"
        layer = cfg["layers"][0]
        assert layer["type"] == "line"
        assert layer["x"] == {"column": "time_bucket"}
        assert layer["y"] == [{"column": "value"}]
        assert layer["breakdown_by"] == {"column": "service.name"}

    def test_bar_stacked_mode_maps_to_bar_stacked_series(self) -> None:
        cfg = self._map(
            {
                "type": "bar",
                "mode": "stacked",
                "dimension": {"field": "handler"},
                "metrics": [{"field": "value"}],
            }
        )
        assert cfg["layers"][0]["type"] == "bar_stacked"

    def test_area_maps_to_xy_area_series(self) -> None:
        cfg = self._map(
            {
                "type": "area",
                "dimension": {"field": "time_bucket"},
                "metrics": [{"field": "a"}, {"field": "b"}],
            }
        )
        layer = cfg["layers"][0]
        assert layer["type"] == "area"
        assert layer["y"] == [{"column": "a"}, {"column": "b"}]

    def test_metric_maps_to_primary_metric(self) -> None:
        cfg = self._map({"type": "metric", "primary": {"field": "uptime"}})
        assert cfg["type"] == "metric"
        assert cfg["metrics"] == [{"type": "primary", "column": "uptime"}]

    def test_gauge_maps_to_gauge_metric(self) -> None:
        cfg = self._map({"type": "gauge", "metric": {"field": "mem"}})
        assert cfg["type"] == "gauge"
        assert cfg["metric"] == {"column": "mem"}

    def test_pie_maps_metrics_and_group_by(self) -> None:
        cfg = self._map(
            {
                "type": "pie",
                "metrics": [{"field": "requests"}],
                "breakdowns": [{"field": "handler"}],
            }
        )
        assert cfg["type"] == "pie"
        assert cfg["metrics"] == [{"column": "requests"}]
        assert cfg["group_by"] == [{"column": "handler"}]

    def test_treemap_uses_partition_shape(self) -> None:
        cfg = self._map(
            {"type": "treemap", "metrics": [{"field": "v"}], "breakdowns": [{"field": "g"}]}
        )
        assert cfg["type"] == "treemap"
        assert cfg["metrics"] == [{"column": "v"}]
        assert cfg["group_by"] == [{"column": "g"}]

    def test_waffle_uses_partition_shape(self) -> None:
        cfg = self._map({"type": "waffle", "metrics": [{"field": "v"}]})
        assert cfg["type"] == "waffle"
        assert cfg["metrics"] == [{"column": "v"}]
        assert "group_by" not in cfg

    def test_tagcloud_maps_metric_and_tag_by(self) -> None:
        cfg = self._map(
            {"type": "tagcloud", "metric": {"field": "count"}, "breakdown": {"field": "term"}}
        )
        assert cfg["type"] == "tag_cloud"
        assert cfg["metric"] == {"column": "count"}
        assert cfg["tag_by"] == {"column": "term"}

    def test_mosaic_uses_single_group_to_avoid_runtime_panel_drop(self) -> None:
        cfg = self._map(
            {
                "type": "mosaic",
                "metric": {"field": "v"},
                "breakdowns": [{"field": "a"}, {"field": "b"}],
            }
        )
        assert cfg["type"] == "mosaic"
        assert cfg["metric"] == {"column": "v"}
        assert cfg["group_by"] == [{"column": "a"}]
        assert "group_breakdown_by" not in cfg

    def test_region_map_maps_metric_and_region(self) -> None:
        cfg = self._map(
            {"type": "region_map", "metric": {"field": "v"}, "region": {"field": "country"}}
        )
        assert cfg["type"] == "region_map"
        assert cfg["metric"] == {"column": "v"}
        assert cfg["region"] == {"column": "country"}

    def test_region_map_falls_back_to_breakdown_for_region(self) -> None:
        cfg = self._map(
            {"type": "region_map", "metric": {"field": "v"}, "breakdown": {"field": "geo"}}
        )
        assert cfg["region"] == {"column": "geo"}


class TestPayloadAndValidation:
    def test_build_dashboard_payload_collects_supported_panels(self) -> None:
        payload, findings = dashboards_api.build_dashboard_payload(
            _report([_panel(), _panel(chart_type="legacy_metric")])
        )
        assert payload["title"] == "vf-conformance-D"
        assert len(payload["panels"]) == 1
        assert dashboards_api.mapped_panel_count(payload) == 1
        assert len(findings) == 1
        assert findings[0].category == "unsupported_by_api_oracle"

    def test_build_dashboard_payload_submits_dashboard_filters(self) -> None:
        # Regression guard: the live conformance oracle must actually POST the
        # dashboard-level ``filters`` block (mapped through the production
        # mapper) so a filter shape Kibana rejects is caught, rather than the
        # filters silently never being part of the validated payload.
        report = {
            "dashboards": [
                {
                    "title": "D",
                    "filters": [{"field": "data_stream.dataset", "equals": "prometheus"}],
                    "panels": [_panel()],
                }
            ]
        }
        payload, _findings = dashboards_api.build_dashboard_payload(report)
        assert payload["filters"] == [
            {
                "type": "condition",
                "condition": {"field": "data_stream.dataset", "operator": "is", "value": "prometheus"},
            },
        ]

    def test_build_dashboard_payload_omits_filters_when_absent(self) -> None:
        payload, _findings = dashboards_api.build_dashboard_payload(_report([_panel()]))
        assert "filters" not in payload

    def test_unsupported_budget_can_fail_low_coverage(self) -> None:
        payload, findings = dashboards_api.build_dashboard_payload(
            _report([_panel(), _panel(chart_type="legacy_metric"), _panel(chart_type="legacy_metric")])
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
            _report([_panel(), _panel(chart_type="legacy_metric")])
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
            _report([_panel(), _panel(chart_type="legacy_metric")]), api_call=api_call
        )
        assert [f.category for f in findings] == ["unsupported_by_api_oracle"]
        assert dashboards_api.summarize(findings)["errors"] == 0

    def test_make_kibana_api_call_omits_authorization_for_empty_key(self, monkeypatch) -> None:
        seen = {}

        class Response:
            status_code = 200
            text = "{}"

            def json(self):
                return {"id": "scratch"}

        def fake_request(method, url, *, headers, json, timeout):
            seen["headers"] = headers
            return Response()

        monkeypatch.setattr(dashboards_api.requests, "request", fake_request)

        call = dashboards_api.make_kibana_api_call("http://localhost:5601", "")
        status, _body = call("POST", "/api/dashboards", {"title": "t"})

        assert status == 200
        assert "Authorization" not in seen["headers"]
        assert seen["headers"]["kbn-xsrf"] == "true"

