# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for the native Kibana Dashboards API YAML mapping.

Pure-dict fixtures, no network. Covers every ES|QL chart-type builder, markdown,
YAML section reconstruction, control -> ``pinned_panels`` mapping, the
``field`` -> ``column`` translation, and the >100-panel sectioning cap.
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

import requests

from observability_migration.core.assets.native_dashboard import (
    NativeControl,
    NativeDashboard,
    NativeGrid,
    NativePanel,
    NativeSection,
)
from observability_migration.targets.kibana import dashboards_api as api

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _leaf(esql: dict, title: str = "Panel", **extra) -> dict:
    panel = {"title": title, "size": {"w": 24, "h": 12}, "position": {"x": 0, "y": 0}, "esql": esql}
    panel.update(extra)
    return panel


def _map(esql: dict, title: str = "Panel"):
    result = api.map_yaml_panel(_leaf(esql, title))
    assert result.api_panel is not None, result.reason
    return result.api_panel


# --------------------------------------------------------------------------- #
# Grid translation
# --------------------------------------------------------------------------- #

def test_grid_from_yaml_maps_size_and_position():
    panel = {"title": "p", "size": {"w": 36, "h": 9}, "position": {"x": 12, "y": 6}}
    assert api._grid_from_yaml(panel) == {"x": 12, "y": 6, "w": 36, "h": 9}


def test_grid_from_yaml_defaults():
    assert api._grid_from_yaml({}) == {"x": 0, "y": 0, "w": 24, "h": 8}


# --------------------------------------------------------------------------- #
# 11 chart-type builders + markdown
# --------------------------------------------------------------------------- #

def test_metric_builder():
    panel = _map({"type": "metric", "query": "FROM m", "primary": {"field": "up_count"}})
    assert panel["type"] == "vis"
    cfg = panel["config"]
    assert cfg["type"] == "metric"
    assert cfg["data_source"] == {"type": "esql", "query": "FROM m"}
    assert cfg["metrics"] == [{"type": "primary", "column": "up_count"}]


def test_gauge_builder():
    cfg = _map({"type": "gauge", "query": "FROM m", "metric": {"field": "pct"}})["config"]
    assert cfg["type"] == "gauge"
    assert cfg["metric"] == {"column": "pct"}


def test_line_becomes_xy():
    cfg = _map({
        "type": "line", "query": "FROM m",
        "dimension": {"field": "step"},
        "metrics": [{"field": "value"}],
        "breakdown": {"field": "job"},
    })["config"]
    assert cfg["type"] == "xy"
    layer = cfg["layers"][0]
    assert layer["type"] == "line"
    assert layer["x"] == {"column": "step"}
    assert layer["y"] == [{"column": "value"}]
    assert layer["breakdown_by"] == {"column": "job"}


def test_bar_becomes_xy():
    cfg = _map({"type": "bar", "query": "FROM m", "dimension": {"field": "handler"},
                "metrics": [{"field": "value"}]})["config"]
    assert cfg["type"] == "xy"
    assert cfg["layers"][0]["type"] == "bar"


def test_area_becomes_xy():
    cfg = _map({"type": "area", "query": "FROM m", "dimension": {"field": "t"},
                "metrics": [{"field": "v"}]})["config"]
    assert cfg["layers"][0]["type"] == "area"


def test_xy_stacked_mode():
    cfg = _map({"type": "bar", "query": "FROM m", "mode": "stacked",
                "dimension": {"field": "h"}, "metrics": [{"field": "v"}]})["config"]
    assert cfg["layers"][0]["type"] == "bar_stacked"


def test_xy_percentage_mode():
    cfg = _map({"type": "area", "query": "FROM m", "mode": "percentage",
                "dimension": {"field": "h"}, "metrics": [{"field": "v"}]})["config"]
    assert cfg["layers"][0]["type"] == "area_percentage"


def test_line_stacked_mode_stays_valid_line_series():
    cfg = _map({"type": "line", "query": "FROM m", "mode": "stacked",
                "dimension": {"field": "h"}, "metrics": [{"field": "v"}]})["config"]
    assert cfg["layers"][0]["type"] == "line"


def test_pie_builder_with_breakdowns():
    cfg = _map({"type": "pie", "query": "FROM m", "metrics": [{"field": "reqs"}],
                "breakdowns": [{"field": "handler"}]})["config"]
    assert cfg["type"] == "pie"
    assert cfg["metrics"] == [{"column": "reqs"}]
    assert cfg["group_by"] == [{"column": "handler"}]


def test_heatmap_builder():
    cfg = _map({"type": "heatmap", "query": "FROM m", "x_axis": {"field": "time_bucket"},
                "y_axis": {"field": "le"}, "metric": {"field": "bucket"}})["config"]
    assert cfg["type"] == "heatmap"
    assert cfg["x"] == {"column": "time_bucket"}
    assert cfg["y"] == {"column": "le"}
    assert cfg["metric"] == {"column": "bucket"}


def test_treemap_builder():
    cfg = _map({"type": "treemap", "query": "FROM m", "metrics": [{"field": "v"}],
                "breakdowns": [{"field": "g"}]})["config"]
    assert cfg["type"] == "treemap"
    assert cfg["metrics"] == [{"column": "v"}]
    assert cfg["group_by"] == [{"column": "g"}]


def test_treemap_builder_accepts_singular_metric():
    cfg = _map({"type": "treemap", "query": "FROM m", "metric": {"field": "v"},
                "breakdowns": [{"field": "g"}]})["config"]
    assert cfg["metrics"] == [{"column": "v"}]


def test_waffle_builder():
    cfg = _map({"type": "waffle", "query": "FROM m", "metrics": [{"field": "v"}]})["config"]
    assert cfg["type"] == "waffle"


def test_pie_builder_caps_group_by_at_three_dimensions():
    # The typed Dashboards API rejects a pie/treemap/waffle ``group_by`` with
    # more than 3 non-collapsed dimensions ("The number of non-collapsed
    # group_by dimensions must not exceed 3"). Extra breakdown fields must be
    # dropped rather than produce a payload the API rejects outright.
    cfg = _map({
        "type": "pie", "query": "FROM m", "metrics": [{"field": "v"}],
        "breakdowns": [{"field": f"dim{i}"} for i in range(9)],
    })["config"]
    assert cfg["group_by"] == [{"column": "dim0"}, {"column": "dim1"}, {"column": "dim2"}]


def test_treemap_builder_caps_group_by_at_three_dimensions():
    cfg = _map({
        "type": "treemap", "query": "FROM m", "metrics": [{"field": "v"}],
        "breakdowns": [{"field": f"dim{i}"} for i in range(5)],
    })["config"]
    assert len(cfg["group_by"]) == 3


def test_waffle_builder_caps_group_by_at_three_dimensions():
    cfg = _map({
        "type": "waffle", "query": "FROM m", "metrics": [{"field": "v"}],
        "breakdowns": [{"field": f"dim{i}"} for i in range(4)],
    })["config"]
    assert len(cfg["group_by"]) == 3


def test_datatable_builder():
    cfg = _map({"type": "datatable", "query": "FROM m", "metrics": [{"field": "message"}],
                "breakdowns": [{"field": "@timestamp"}, {"field": "service.name"}]})["config"]
    assert cfg["type"] == "data_table"
    assert cfg["metrics"] == [{"column": "message"}]
    assert cfg["rows"] == [{"column": "@timestamp"}, {"column": "service.name"}]


def test_tag_cloud_builder():
    cfg = _map({"type": "tag_cloud", "query": "FROM m", "metric": {"field": "count"},
                "breakdown": {"field": "term"}})["config"]
    assert cfg["type"] == "tag_cloud"
    assert cfg["metric"] == {"column": "count"}
    assert cfg["tag_by"] == {"column": "term"}


def test_mosaic_builder():
    cfg = _map({"type": "mosaic", "query": "FROM m", "metric": {"field": "v"},
                "breakdowns": [{"field": "a"}, {"field": "b"}]})["config"]
    assert cfg["type"] == "mosaic"
    assert cfg["metric"] == {"column": "v"}
    assert cfg["group_by"] == [{"column": "a"}]
    assert "group_breakdown_by" not in cfg


def test_gauge_builder_preserves_bounds_and_shape():
    cfg = _map({
        "type": "gauge",
        "query": "FROM m",
        "metric": {"field": "pct", "label": "CPU %"},
        "minimum": {"field": "_min"},
        "maximum": {"field": "_max"},
        "goal": {"field": "_goal"},
        "appearance": {"shape": "arc"},
    })["config"]
    assert cfg["metric"] == {
        "column": "pct",
        "label": "CPU %",
        "min": {"column": "_min"},
        "max": {"column": "_max"},
        "goal": {"column": "_goal"},
    }
    assert cfg["styling"] == {"shape": {"type": "arc"}}


def test_region_map_builder():
    cfg = _map({"type": "region_map", "query": "FROM m", "metric": {"field": "v"},
                "region": {"field": "country"}})["config"]
    assert cfg["type"] == "region_map"
    assert cfg["metric"] == {"column": "v"}
    assert cfg["region"] == {"column": "country"}


def test_region_map_falls_back_to_breakdown_for_region():
    cfg = _map({"type": "region_map", "query": "FROM m", "metric": {"field": "v"},
                "breakdown": {"field": "geo.country_iso_code"}})["config"]
    assert cfg["region"] == {"column": "geo.country_iso_code"}


def test_markdown_panel():
    result = api.map_yaml_panel({"title": "Notes", "size": {"w": 24, "h": 8},
                                 "position": {"x": 0, "y": 0},
                                 "markdown": {"content": "# Hi"}})
    assert result.api_panel is not None
    assert result.api_panel["type"] == "markdown"
    assert result.api_panel["config"]["content"] == "# Hi"
    assert result.api_panel["config"]["settings"] == {}


def test_markdown_hide_title():
    result = api.map_yaml_panel({"title": "N", "markdown": {"content": "x"}, "hide_title": True})
    assert result.api_panel["config"]["hide_title"] is True


# --------------------------------------------------------------------------- #
# field -> column translation and unmappable panels
# --------------------------------------------------------------------------- #

def test_field_to_column_translation():
    col = api._column({"field": "my_field", "label": "ignored", "format": {}})
    assert col == {"column": "my_field"}


def test_api_format_keeps_only_valid_shapes():
    assert api._api_format({"type": "number", "decimals": 1, "suffix": "/s", "compact": True, "pattern": "0"}) == {
        "type": "number",
        "decimals": 1,
        "suffix": "/s",
        "compact": True,
    }
    assert api._api_format({"type": "bytes", "decimals": 2, "suffix": "B", "compact": True}) == {
        "type": "bytes",
        "decimals": 2,
        "suffix": "B",
    }
    assert api._api_format({"type": "duration", "from": "milliseconds", "to": "seconds", "suffix": "s"}) == {
        "type": "duration",
        "from": "milliseconds",
        "to": "seconds",
        "suffix": "s",
    }
    assert api._api_format({"type": "custom", "pattern": "0.0a"}) == {"type": "custom", "pattern": "0.0a"}
    # An incomplete from/to pair is defaulted rather than dropped: the
    # multi-column format schema (xy/data_table/etc.) requires both, so a
    # partial pair must still produce a valid duration format.
    assert api._api_format({"type": "duration", "from": "milliseconds"}) == {
        "type": "duration", "from": "milliseconds", "to": "humanize",
    }
    assert api._api_format({"type": "duration"}) == {
        "type": "duration", "from": "seconds", "to": "humanize",
    }
    assert api._api_format({"type": "date", "pattern": "YYYY"}) is None


def test_api_color_accepts_hex_and_api_shapes():
    assert api._api_color("#54B399") == {"type": "static", "color": "#54B399"}
    assert api._api_color({"type": "auto", "ignored": True}) == {"type": "auto"}
    assert api._api_color({"type": "static", "color": "#6092C0", "ignored": True}) == {
        "type": "static",
        "color": "#6092C0",
    }
    assert api._api_color({
        "type": "dynamic",
        "range": "absolute",
        "steps": [{"gte": 0, "lt": 80, "color": "#54B399"}, {"gte": 80, "color": "#E7664C"}],
    }) == {
        "type": "dynamic",
        "range": "absolute",
        "steps": [{"gte": 0, "lt": 80, "color": "#54B399"}, {"gte": 80, "color": "#E7664C"}],
    }
    assert api._api_color({"type": "dynamic", "steps": [{"gte": 0, "color": "#54B399"}]}) is None


def test_xy_builder_preserves_api_safe_columns_legend_axis_and_horizontal_stacking():
    cfg = _map({
        "type": "bar",
        "query": "FROM metrics-*",
        "mode": "stacked",
        "orientation": "horizontal",
        "dimension": {
            "field": "bucket",
            "label": "Bucket",
            "format": {"type": "duration", "from": "seconds", "to": "minutes"},
        },
        "metrics": [{
            "field": "latency",
            "label": "Latency",
            "format": {"type": "number", "decimals": 1, "compact": True},
            "color": "#54B399",
            "axis": "y2",
        }],
        "breakdowns": [{
            "field": "service",
            "label": "Service",
            "collapse_by": "sum",
            "color": {"mode": "gradient", "palette": "default"},
        }],
        "legend": {"visible": "show", "position": "right", "truncate_labels": True},
        "appearance": {
            "axis": {
                "x": {"scale": "ordinal", "title": {"text": "Bucket", "visible": True}},
                "y2": {"scale": "log", "title": {"text": "Latency", "visible": True}},
            },
        },
    })["config"]

    assert cfg["layers"][0]["type"] == "bar_horizontal_stacked"
    assert cfg["layers"][0]["x"] == {
        "column": "bucket",
        "label": "Bucket",
        "format": {"type": "duration", "from": "seconds", "to": "minutes"},
    }
    assert cfg["layers"][0]["y"] == [{
        "column": "latency",
        "label": "Latency",
        "format": {"type": "number", "decimals": 1, "compact": True},
        "color": {"type": "static", "color": "#54B399"},
        "axis": "y2",
    }]
    assert cfg["layers"][0]["breakdown_by"] == {
        "column": "service",
        "label": "Service",
        "collapse_by": "sum",
        "color": {"mode": "gradient", "palette": "default"},
    }
    assert cfg["legend"] == {
        "visibility": "visible",
        "placement": "outside",
        "position": "right",
        "layout": {"type": "grid", "truncate": {"enabled": True}},
    }
    assert cfg["axis"] == {
        "x": {"scale": "ordinal", "title": {"text": "Bucket", "visible": True}},
        "y2": {"scale": "log", "title": {"text": "Latency", "visible": True}},
    }


def test_xy_builder_translates_yaml_right_axis_to_api_y2():
    cfg = _map({
        "type": "line",
        "query": "FROM metrics-*",
        "dimension": {"field": "bucket"},
        "metrics": [{"field": "latency", "axis": "right"}],
    })["config"]
    assert cfg["layers"][0]["y"] == [{"column": "latency", "axis": "y2"}]


def test_xy_builder_accepts_yaml_schema_appearance_axes():
    cfg = _map({
        "type": "line",
        "query": "FROM metrics-*",
        "dimension": {"field": "time_bucket"},
        "metrics": [{"field": "value"}],
        "appearance": {
            "y_left_axis": {
                "title": "Throughput",
                "scale": "log",
                "extent": {"mode": "custom", "min": 0.0, "max": 100.0},
            },
            "y_right_axis": {"title": "Errors"},
        },
    })["config"]

    assert cfg["axis"] == {
        "y": {
            "scale": "log",
            "title": {"text": "Throughput", "visible": True},
            "domain": {"type": "custom", "min": 0.0, "max": 100.0},
        },
        "y2": {"title": {"text": "Errors", "visible": True}},
    }


def test_metric_builder_preserves_secondary_breakdown_and_styling():
    cfg = _map({
        "type": "metric",
        "query": "FROM m",
        "primary": {
            "field": "requests",
            "label": "Requests",
            "subtitle": "Last hour",
            "format": {"type": "number", "decimals": 0},
            "color": "#54B399",
        },
        "secondary": {
            "field": "errors",
            "label": "Errors",
            "format": {"type": "percent", "decimals": 2},
            "color": {"type": "none"},
        },
        "breakdowns": [{"field": "service", "label": "Service", "collapse_by": "max"}],
        "styling": {"primary": {"position": "bottom"}},
    })["config"]

    assert cfg["metrics"] == [
        {
            "type": "primary",
            "column": "requests",
            "label": "Requests",
            "subtitle": "Last hour",
            "format": {"type": "number", "decimals": 0},
            "color": {"type": "static", "color": "#54B399"},
        },
        {
            "type": "secondary",
            "column": "errors",
            "label": "Errors",
            "format": {"type": "percent", "decimals": 2},
            "color": {"type": "none"},
        },
    ]
    assert cfg["breakdown_by"] == {"column": "service", "label": "Service", "collapse_by": "max"}
    assert cfg["styling"] == {"primary": {"position": "bottom"}}


def test_gauge_builder_preserves_bounds_shape_ticks_and_drops_invalid_thresholds():
    cfg = _map({
        "type": "gauge",
        "query": "FROM m",
        "metric": {
            "field": "cpu",
            "label": "CPU",
            "subtitle": "avg",
            "format": {"type": "percent", "decimals": 1},
            "color": {"thresholds": [{"value": 90, "color": "#E7664C"}]},
            "ticks": {"visible": True},
            "title": {"text": "CPU usage", "visible": True},
        },
        "minimum": {"field": "min_cpu", "label": "Min"},
        "maximum": {"field": "max_cpu", "label": "Max"},
        "goal": {"field": "goal_cpu", "label": "Goal"},
        "appearance": {"shape": "vertical_bullet"},
    })["config"]

    assert cfg["metric"] == {
        "column": "cpu",
        "label": "CPU",
        "subtitle": "avg",
        "format": {"type": "percent", "decimals": 1},
        "ticks": {"visible": True},
        "title": {"text": "CPU usage", "visible": True},
        "min": {"column": "min_cpu", "label": "Min"},
        "max": {"column": "max_cpu", "label": "Max"},
        "goal": {"column": "goal_cpu", "label": "Goal"},
    }
    assert cfg["styling"] == {"shape": {"type": "bullet", "orientation": "vertical"}}


def test_gauge_builder_maps_yaml_top_level_threshold_color_to_metric_color():
    cfg = _map({
        "type": "gauge",
        "query": "FROM m",
        "metric": {"field": "cpu"},
        "color": {
            "range_min": 0,
            "thresholds": [
                {"up_to": 70, "color": "#54B399"},
                {"up_to": 90, "color": "#D6BF57"},
                {"up_to": 100, "color": "#E7664C"},
            ],
        },
    })["config"]
    assert cfg["metric"]["color"] == {
        "type": "dynamic",
        "range": "absolute",
        "steps": [
            {"color": "#54B399", "gte": 0, "lt": 70},
            {"color": "#D6BF57", "gte": 70, "lt": 90},
            {"color": "#E7664C", "gte": 90},
        ],
    }


def test_datatable_builder_preserves_rich_columns_and_split_metrics_by():
    cfg = _map({
        "type": "datatable",
        "query": "FROM m",
        "metrics": [{
            "field": "bytes",
            "label": "Bytes",
            "format": {"type": "bytes", "decimals": 1},
            "width": 120,
            "alignment": "right",
            "visible": True,
            "summary": {"type": "sum", "label": "Total"},
        }],
        "breakdowns": [{
            "field": "service",
            "label": "Service",
            "width": 180,
            "alignment": "left",
            "visible": False,
            "collapse_by": "min",
            "click_filter": True,
        }],
        "split_metrics_by": [{"field": "env", "label": "Environment", "format": {"type": "custom", "pattern": "0"}}],
    })["config"]

    assert cfg["metrics"] == [{
        "column": "bytes",
        "label": "Bytes",
        "format": {"type": "bytes", "decimals": 1},
        "width": 120,
        "alignment": "right",
        "visible": True,
        "summary": {"type": "sum", "label": "Total"},
    }]
    assert cfg["rows"] == [{
        "column": "service",
        "label": "Service",
        "width": 180,
        "alignment": "left",
        "visible": False,
        "collapse_by": "min",
        "click_filter": True,
    }]
    assert cfg["split_metrics_by"] == [{"column": "env", "label": "Environment", "format": {"type": "custom", "pattern": "0"}}]


def test_heatmap_builder_preserves_labels_legend_axis_and_cells():
    cfg = _map({
        "type": "heatmap",
        "query": "FROM m",
        "x_axis": {"field": "hour", "label": "Hour", "format": {"type": "number", "decimals": 0}},
        "y_axis": {"field": "service", "label": "Service"},
        "metric": {"field": "requests", "label": "Requests", "color": {"type": "auto"}},
        "legend": {"visible": "hide", "truncate_labels": 2},
        "appearance": {
            "axis": {
                "x": {"scale": "ordinal", "title": {"text": "Hour", "visible": True}},
                "y": {"title": {"text": "Service", "visible": True}},
            },
            "cells": {"labels": {"visible": True}},
        },
    })["config"]

    assert cfg["x"] == {"column": "hour", "label": "Hour", "format": {"type": "number", "decimals": 0}}
    assert cfg["y"] == {"column": "service", "label": "Service"}
    assert cfg["metric"] == {"column": "requests", "label": "Requests", "color": {"type": "auto"}}
    assert cfg["legend"] == {"visibility": "hidden", "truncate_after_lines": 2}
    assert cfg["axis"] == {
        "x": {"scale": "ordinal", "title": {"text": "Hour", "visible": True}},
        "y": {"title": {"text": "Service", "visible": True}},
    }
    assert cfg["styling"] == {"cells": {"labels": {"visible": True}}}


def test_partition_builders_preserve_rich_metric_group_legend_and_styling():
    base = {
        "query": "FROM m",
        "metric": {
            "field": "requests",
            "label": "Requests",
            "format": {"type": "number", "decimals": 0},
            "color": {"type": "auto"},
        },
        "breakdowns": [{
            "field": "service",
            "label": "Service",
            "format": {"type": "custom", "pattern": "0"},
            "color": {"mode": "categorical", "palette": "default", "mapping": [{"values": ["api"], "color": "#54B399"}]},
            "collapse_by": "avg",
        }],
        "legend": {"visible": "auto", "truncate_labels": 3, "nested": True},
        "styling": {"values": {"visible": True, "mode": "percentage", "percent_decimals": 1}},
    }

    pie = _map({"type": "pie", **base})["config"]
    tag_cloud = _map({"type": "tag_cloud", **base})["config"]
    region_map = _map({"type": "region_map", **base})["config"]
    mosaic = _map({"type": "mosaic", **base, "breakdowns": [*base["breakdowns"], {"field": "team"}]})["config"]

    assert pie["metrics"] == [{
        "column": "requests",
        "label": "Requests",
        "format": {"type": "number", "decimals": 0},
        "color": {"type": "auto"},
    }]
    assert pie["group_by"] == [{
        "column": "service",
        "label": "Service",
        "format": {"type": "custom", "pattern": "0"},
        "color": {"mode": "categorical", "palette": "default", "mapping": [{"values": ["api"], "color": "#54B399"}]},
        "collapse_by": "avg",
    }]
    assert pie["legend"] == {"visibility": "auto", "truncate_after_lines": 3, "nested": True}
    assert pie["styling"] == {"values": {"visible": True, "mode": "percentage", "percent_decimals": 1}}
    assert tag_cloud["tag_by"] == pie["group_by"][0]
    assert region_map["region"] == {"column": "service", "label": "Service"}
    assert mosaic["group_by"] == [pie["group_by"][0]]
    assert "group_breakdown_by" not in mosaic


def test_controls_support_defaults_single_select_options_and_range():
    assert api.map_yaml_control({
        "type": "options",
        "label": "Service",
        "data_view": "metrics-*",
        "field": "service.name",
        "defaults": ["api", "worker"],
        "multiple": False,
    }) == {
        "type": "options_list_control",
        "config": {
            "title": "Service",
            "data_view_id": "metrics-*",
            "field_name": "service.name",
            "selected_options": ["api", "worker"],
            "single_select": True,
        },
    }
    assert api.map_yaml_control({
        "type": "range",
        "label": "Latency",
        "data_view_id": "metrics-*",
        "field_name": "latency",
        "default": [10, 50],
    }) == {
        "type": "range_slider_control",
        "config": {
            "title": "Latency",
            "data_view_id": "metrics-*",
            "field_name": "latency",
            "value": ["10", "50"],
        },
    }


def test_esql_without_query_is_unmapped():
    result = api.map_yaml_panel(_leaf({"type": "metric", "primary": {"field": "v"}}))
    assert result.api_panel is None
    assert "no query" in result.reason


def test_unknown_chart_type_is_unmapped():
    result = api.map_yaml_panel(_leaf({"type": "sankey", "query": "FROM m"}))
    assert result.api_panel is None
    assert "no API builder" in result.reason


def test_panel_without_esql_or_markdown_is_unmapped():
    result = api.map_yaml_panel({"title": "x"})
    assert result.api_panel is None


# --------------------------------------------------------------------------- #
# Controls -> pinned_panels
# --------------------------------------------------------------------------- #

def test_control_values_from_query():
    control = {
        "type": "esql", "label": "instance", "variable_name": "instance",
        "variable_type": "values", "query": "FROM metrics-* | STATS x", "multiple": False,
        "default": ".*",
    }
    pinned = api.map_yaml_control(control)
    assert pinned == {
        "type": "esql_control",
        "config": {
            "control_type": "VALUES_FROM_QUERY",
            "title": "instance",
            "variable_name": "instance",
            "variable_type": "values",
            "esql_query": "FROM metrics-* | STATS x",
            "selected_options": [".*"],
            "single_select": True,
        },
    }


def test_control_static_values():
    control = {"variable_name": "env", "label": "Env", "available_options": ["prod", "dev"]}
    pinned = api.map_yaml_control(control)
    assert pinned["config"]["control_type"] == "STATIC_VALUES"
    assert pinned["config"]["available_options"] == ["prod", "dev"]
    assert pinned["config"]["selected_options"] == []


def test_control_fields_choices_map_to_static_values():
    # Issue #282: a late-bound grouping control (``??var``) carries its field
    # options under ``choices`` and must map to a STATIC_VALUES fields control.
    control = {
        "type": "esql",
        "label": "Group by",
        "variable_name": "grouping",
        "variable_type": "fields",
        "choices": ["exporter", "transport"],
        "default": "exporter",
    }
    pinned = api.map_yaml_control(control)
    assert pinned == {
        "type": "esql_control",
        "config": {
            "control_type": "STATIC_VALUES",
            "title": "Group by",
            "variable_name": "grouping",
            "variable_type": "fields",
            "available_options": ["exporter", "transport"],
            "selected_options": ["exporter"],
        },
    }


def test_control_without_variable_name_dropped():
    assert api.map_yaml_control({"query": "FROM m"}) is None


# --------------------------------------------------------------------------- #
# Section reconstruction + full dashboard payload
# --------------------------------------------------------------------------- #

def test_section_reconstruction():
    dashboard = {
        "name": "D",
        "panels": [
            _leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top"),
            {
                "title": "System Metrics",
                "section": {
                    "collapsed": True,
                    "panels": [
                        _leaf({"type": "line", "query": "FROM m", "dimension": {"field": "t"},
                               "metrics": [{"field": "v"}]}, "cpu"),
                        {"title": "Notes", "markdown": {"content": "hi"}},
                    ],
                },
            },
        ],
    }
    payload, counts, reasons = api.build_dashboard_payload_from_yaml(dashboard)
    assert payload["title"] == "D"
    assert len(payload["panels"]) == 2
    leaf, section = payload["panels"]
    assert leaf["type"] == "vis"
    assert "type" not in section  # sections carry no type discriminator
    assert section["title"] == "System Metrics"
    assert section["collapsed"] is True
    assert "y" in section["grid"]
    assert len(section["panels"]) == 2
    assert counts["mapped"] == 3
    assert counts["sections"] == 1
    assert reasons == {}


def test_controls_become_pinned_panels():
    dashboard = {
        "name": "D",
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}})],
        "controls": [
            {"variable_name": "instance", "label": "instance", "query": "FROM m", "variable_type": "values"},
        ],
    }
    payload, counts, _ = api.build_dashboard_payload_from_yaml(dashboard)
    assert "pinned_panels" in payload
    assert payload["pinned_panels"][0]["type"] == "esql_control"
    assert counts["controls"] == 1


def test_description_carried_over():
    payload, _, _ = api.build_dashboard_payload_from_yaml(
        {"name": "D", "description": "hello", "panels": []},
    )
    assert payload["description"] == "hello"


def test_build_payload_from_yaml_doc_wrapper():
    doc = {"dashboards": [{
        "name": "Wrapped",
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}})],
    }]}
    payload, stats = api.build_payload_from_yaml(doc)
    assert payload["title"] == "Wrapped"
    assert stats["mapped"] == 1
    assert "reasons" in stats


def test_stable_dashboard_id_prefers_source_id():
    assert api._stable_dashboard_id({
        "name": "Stable Dashboard",
        "id": "source-id",
        "panels": [],
    }) == "source-id"


def test_stable_dashboard_id_generates_deterministic_id_from_name():
    id_a = api._stable_dashboard_id({"name": "Stable Dashboard", "panels": []})
    id_b = api._stable_dashboard_id({"name": "Stable Dashboard", "panels": []})
    assert id_a == id_b
    assert id_a.startswith("obs-migrate-")


def test_unmapped_reasons_tracked():
    dashboard = {
        "name": "D",
        "panels": [
            _leaf({"type": "sankey", "query": "FROM m"}, "bad"),
            _leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "good"),
        ],
    }
    _, counts, reasons = api.build_dashboard_payload_from_yaml(dashboard)
    assert counts["mapped"] == 1
    assert counts["unmapped"] == 1
    assert any("no API builder" in r for r in reasons)


# --------------------------------------------------------------------------- #
# NativeDashboard IR — payload builders are thin wrappers around it
# --------------------------------------------------------------------------- #

def test_native_dashboard_from_yaml_returns_native_dashboard_instance():
    dashboard = {
        "name": "D",
        "description": "hello",
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
        "controls": [
            {"variable_name": "env", "label": "Env", "available_options": ["prod", "dev"]},
        ],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert isinstance(native, NativeDashboard)
    assert native.title == "D"
    assert native.description == "hello"
    assert len(native.items) == 1
    assert len(native.controls) == 1
    assert counts.mapped == 1
    assert counts.controls == 1


def test_native_dashboard_from_yaml_reconstructs_sections():
    dashboard = {
        "name": "D",
        "panels": [
            _leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top"),
            {
                "title": "System Metrics",
                "section": {
                    "collapsed": True,
                    "panels": [
                        _leaf({"type": "line", "query": "FROM m", "dimension": {"field": "t"},
                               "metrics": [{"field": "v"}]}, "cpu"),
                    ],
                },
            },
        ],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert len(native.items) == 2
    section = native.items[1]
    assert isinstance(section, NativeSection)
    assert section.title == "System Metrics"
    assert section.collapsed is True
    assert counts.sections == 1


def test_native_dashboard_from_ir_matches_native_dashboard_from_yaml():
    """IR-first parity: `native_dashboard_from_ir` must not be a lossier or
    differently-shaped mapping than the YAML path for the same dashboard --
    panels, a nested section, a control, and a filter all round-trip through
    `DashboardIR` and map identically either way.
    """
    from observability_migration.core.assets.dashboard import DashboardIR

    dashboard = {
        "name": "D",
        "description": "hello",
        "filters": [{"field": "data_stream.dataset", "equals": "prometheus"}],
        "panels": [
            _leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top"),
            {
                "title": "System Metrics",
                "section": {
                    "collapsed": True,
                    "panels": [
                        _leaf(
                            {"type": "line", "query": "FROM m", "dimension": {"field": "t"}, "metrics": [{"field": "v"}]},
                            "cpu",
                        ),
                    ],
                },
            },
        ],
        "controls": [
            {"variable_name": "env", "label": "Env", "available_options": ["prod", "dev"]},
        ],
    }

    yaml_native, yaml_counts = api.native_dashboard_from_yaml(dashboard)
    dashboard_ir = DashboardIR.from_yaml_dict(dashboard, source_adapter="grafana")
    ir_native, ir_counts = api.native_dashboard_from_ir(dashboard_ir)

    assert ir_native.to_api_payload() == yaml_native.to_api_payload()
    assert ir_counts.as_dicts() == yaml_counts.as_dicts()


def test_native_dashboard_from_ir_returns_native_dashboard_instance():
    from observability_migration.core.assets.dashboard import DashboardIR

    dashboard_ir = DashboardIR.from_yaml_dict({
        "name": "D",
        "description": "hello",
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
        "controls": [
            {"variable_name": "env", "label": "Env", "available_options": ["prod", "dev"]},
        ],
    })
    native, counts = api.native_dashboard_from_ir(dashboard_ir)
    assert isinstance(native, NativeDashboard)
    assert native.title == "D"
    assert native.description == "hello"
    assert len(native.items) == 1
    assert len(native.controls) == 1
    assert counts.mapped == 1
    assert counts.controls == 1


def test_native_dashboard_from_yaml_preserves_phrase_filter():
    dashboard = {
        "name": "Filtered Dashboard",
        "filters": [{"field": "data_stream.dataset", "equals": "prometheus"}],
        "panels": [_leaf({"type": "metric", "query": "FROM metrics-*", "primary": {"field": "count"}}, "Count")],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert native.filters == [
        {"type": "condition", "condition": {"field": "data_stream.dataset", "operator": "is", "value": "prometheus"}},
    ]
    payload = native.to_api_payload()
    assert payload["filters"] == native.filters
    assert counts.reasons.get("dropped_unsupported_dashboard_filter", 0) == 0


def test_native_dashboard_from_yaml_maps_exists_phrases_and_range_filters():
    dashboard = {
        "name": "D",
        "filters": [
            {"exists": "host.name"},
            {"field": "service.name", "in": ["api", "worker"]},
            {"field": "duration", "gte": 10, "lte": 100},
        ],
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
    }
    native, _counts = api.native_dashboard_from_yaml(dashboard)
    assert native.filters == [
        {"type": "condition", "condition": {"field": "host.name", "operator": "exists"}},
        {
            "type": "condition",
            "condition": {"field": "service.name", "operator": "is_one_of", "value": ["api", "worker"]},
        },
        {
            "type": "condition",
            "condition": {"field": "duration", "operator": "range", "value": {"gte": 10, "lte": 100}},
        },
    ]


def test_native_dashboard_from_yaml_maps_negated_and_group_filters():
    dashboard = {
        "name": "D",
        "filters": [
            {"not": {"field": "status", "equals": "error"}},
            {"and": [{"field": "env", "equals": "prod"}, {"exists": "host.name"}]},
        ],
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert native.filters == [
        {
            "type": "condition",
            "condition": {"field": "status", "operator": "is", "value": "error"},
            "negate": True,
        },
        {
            "type": "group",
            "group": {
                "operator": "and",
                "conditions": [
                    {"field": "env", "operator": "is", "value": "prod"},
                    {"field": "host.name", "operator": "exists"},
                ],
            },
        },
    ]
    assert counts.reasons.get("dropped_unsupported_dashboard_filter", 0) == 0


def test_native_dashboard_from_yaml_drops_unsupported_filter_with_reason():
    # A ``not`` wrapping a nested ``and``/``or`` *inside* another group's
    # conditions list has no ``negate`` slot in the API's recursive group
    # schema (only the top-level group wrapper and leaf conditions have one).
    dashboard = {
        "name": "D",
        "filters": [
            {"and": [{"not": {"and": [{"field": "a", "equals": 1}]}}]},
        ],
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert native.filters == []
    assert counts.reasons.get("dropped_unsupported_dashboard_filter", 0) >= 1


def test_native_dashboard_from_yaml_preserves_top_level_negated_group():
    # A top-level ``not`` wrapping a group IS representable: the wrapper carries
    # ``negate`` for groups exactly as it does for leaf conditions.
    dashboard = {
        "name": "D",
        "filters": [{"not": {"and": [{"field": "env", "equals": "prod"}]}}],
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert native.filters == [
        {
            "type": "group",
            "group": {"operator": "and", "conditions": [{"field": "env", "operator": "is", "value": "prod"}]},
            "negate": True,
        },
    ]
    assert counts.reasons.get("dropped_unsupported_dashboard_filter", 0) == 0


def test_native_dashboard_from_yaml_omits_disabled_group_member_without_dropping():
    # A disabled member is inactive; it must not become an active constraint,
    # and omitting it (the nested shape has no ``disabled`` slot) is not a
    # semantic gap, so it must not be counted as a dropped filter.
    dashboard = {
        "name": "D",
        "filters": [
            {"and": [{"field": "env", "equals": "prod"}, {"field": "host", "equals": "a", "disabled": True}]},
        ],
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert native.filters == [
        {
            "type": "group",
            "group": {"operator": "and", "conditions": [{"field": "env", "operator": "is", "value": "prod"}]},
        },
    ]
    assert counts.reasons.get("dropped_unsupported_dashboard_filter", 0) == 0


def test_native_dashboard_from_yaml_drops_whole_group_with_unrepresentable_member():
    # An ``and`` with one unrepresentable member must drop the WHOLE filter --
    # emitting only the surviving conjunct would silently match a broader set.
    dashboard = {
        "name": "D",
        "filters": [
            {"and": [{"field": "env", "equals": "prod"}, {"dsl": {"match_all": {}}}]},
        ],
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert native.filters == []
    assert counts.reasons.get("dropped_unsupported_dashboard_filter", 0) >= 1


def test_native_dashboard_from_yaml_drops_malformed_leaf_without_crashing():
    # A non-list ``in`` and a field-less ``equals`` must be dropped+counted,
    # never crash the build or char-split a string into per-character terms.
    dashboard = {
        "name": "D",
        "filters": [
            {"field": "svc", "in": "api"},
            {"equals": "prod"},
        ],
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert native.filters == []
    assert counts.reasons.get("dropped_unsupported_dashboard_filter", 0) == 2


def test_native_dashboard_from_report_preserves_filters():
    # Report-path parity: dashboard-level filters must not be silently dropped
    # on the report path either.
    dashboard = {
        "title": "D",
        "filters": [{"field": "data_stream.dataset", "equals": "prometheus"}],
        "panels": [],
    }
    native, _counts = api.native_dashboard_from_report(dashboard)
    assert native.filters == [
        {"type": "condition", "condition": {"field": "data_stream.dataset", "operator": "is", "value": "prometheus"}},
    ]


def test_native_dashboard_from_yaml_negated_filter_reads_disabled_and_alias_from_inner():
    # A NegateFilter is ``{not: <filter>}`` only (schema: NegateFilter has no
    # ``disabled``/``alias`` of its own); those live on the wrapped filter. A
    # disabled negated filter must stay marked disabled (inactive), never be
    # shipped as an active NOT constraint, and its inner alias must survive.
    dashboard = {
        "name": "D",
        "filters": [
            {"not": {"field": "host", "equals": "db1", "disabled": True, "alias": "not db1"}},
        ],
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert native.filters == [
        {
            "type": "condition",
            "condition": {"field": "host", "operator": "is", "value": "db1"},
            "negate": True,
            "disabled": True,
            "label": "not db1",
        },
    ]
    assert counts.reasons.get("dropped_unsupported_dashboard_filter", 0) == 0


def test_native_dashboard_from_yaml_skips_disabled_negated_group_member():
    # A disabled negated group member carries ``disabled`` on its inner filter;
    # it is inactive and must be skipped, leaving only the active conjunct --
    # not emitted as an active NOT constraint that narrows the query.
    dashboard = {
        "name": "D",
        "filters": [
            {"and": [
                {"field": "env", "equals": "prod"},
                {"not": {"field": "host", "equals": "db1", "disabled": True}},
            ]},
        ],
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
    }
    native, counts = api.native_dashboard_from_yaml(dashboard)
    assert native.filters == [
        {
            "type": "group",
            "group": {"operator": "and", "conditions": [{"field": "env", "operator": "is", "value": "prod"}]},
        },
    ]
    assert counts.reasons.get("dropped_unsupported_dashboard_filter", 0) == 0


def test_build_dashboard_payload_from_yaml_matches_native_dashboard_to_api_payload():
    dashboard = {
        "name": "D",
        "panels": [_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "top")],
    }
    payload, _counts, _reasons = api.build_dashboard_payload_from_yaml(dashboard)
    native, _native_counts = api.native_dashboard_from_yaml(dashboard)
    assert payload == native.to_api_payload()


def test_native_dashboard_from_report_returns_native_dashboard_instance():
    dashboard = {
        "title": "D",
        "panels": [
            {
                "title": "Notes",
                "visual_ir": {
                    "layout": {"x": 0, "y": 0, "w": 12, "h": 4},
                    "presentation": {"kind": "markdown", "config": {"content": "hi"}},
                },
            },
        ],
    }
    native, counts = api.native_dashboard_from_report(dashboard)
    assert isinstance(native, NativeDashboard)
    assert native.title == "D"
    assert len(native.items) == 1
    assert counts.mapped == 1


def test_native_dashboard_from_report_skips_grafana_rows():
    dashboard = {
        "title": "D",
        "panels": [
            {"grafana_type": "row", "title": "Row"},
            {
                "title": "Notes",
                "visual_ir": {
                    "layout": {"x": 0, "y": 0, "w": 12, "h": 4},
                    "presentation": {"kind": "markdown", "config": {"content": "hi"}},
                },
            },
        ],
    }
    native, counts = api.native_dashboard_from_report(dashboard)
    assert len(native.items) == 1
    assert counts.mapped == 1


def test_build_dashboard_payload_matches_native_dashboard_to_api_payload():
    dashboard = {
        "title": "D",
        "panels": [
            {
                "title": "Notes",
                "visual_ir": {
                    "layout": {"x": 0, "y": 0, "w": 12, "h": 4},
                    "presentation": {"kind": "markdown", "config": {"content": "hi"}},
                },
            },
        ],
    }
    payload, _counts, _reasons = api.build_dashboard_payload(dashboard)
    native, _native_counts = api.native_dashboard_from_report(dashboard)
    assert payload == native.to_api_payload()


# --------------------------------------------------------------------------- #
# >100-panel sectioning cap
# --------------------------------------------------------------------------- #

def test_flat_panels_under_latest_cap_are_not_sectionized():
    panels = [
        _leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, f"p{i}")
        for i in range(250)
    ]
    payload, counts, _ = api.build_dashboard_payload_from_yaml({"name": "Big", "panels": panels})
    assert len(payload["panels"]) == 250
    assert all(item["type"] == "vis" for item in payload["panels"])
    assert counts["mapped"] == 250


def test_top_level_item_cap_respected():
    # 1,050 real sections -> capped at 1,000 top-level items.
    panels = [
        {"title": f"s{i}", "section": {"collapsed": False, "panels": []}}
        for i in range(1050)
    ]
    payload, _, _ = api.build_dashboard_payload_from_yaml({"name": "Many", "panels": panels})
    assert len(payload["panels"]) == 1000


def test_mixed_sections_and_loose_panels_over_total_cap_records_drops():
    panels = []
    for i in range(600):
        panels.append({"title": f"s{i}", "section": {"collapsed": False, "panels": [
            _leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, "inner"),
        ]}})
    for i in range(300):
        panels.append(_leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, f"loose{i}"))
    payload, counts, reasons = api.build_dashboard_payload_from_yaml({"name": "Mixed", "panels": panels})
    assert len(payload["panels"]) <= 1000
    assert "dropped_over_item_cap" not in reasons

    def _count_leaves(items):
        total = 0
        for it in items:
            if "panels" in it and "type" not in it:
                total += len(it["panels"])
            else:
                total += 1
        return total
    assert _count_leaves(payload["panels"]) == 500
    assert counts["mapped"] == 500
    assert reasons.get("dropped_over_total_item_cap") == 500


def test_over_cap_that_cannot_coalesce_records_drop_reason():
    # 1,050 real sections cannot be merged; overflow must be recorded, not silent.
    panels = [
        {"title": f"s{i}", "section": {"collapsed": False, "panels": []}}
        for i in range(1050)
    ]
    payload, _counts, reasons = api.build_dashboard_payload_from_yaml({"name": "TooMany", "panels": panels})
    assert len(payload["panels"]) == 1000
    assert reasons.get("dropped_over_item_cap") == 50


def test_section_panel_cap_respected():
    inner = [
        _leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, f"p{i}")
        for i in range(1050)
    ]
    dashboard = {"name": "D", "panels": [{"title": "S", "section": {"panels": inner}}]}
    payload, _, _ = api.build_dashboard_payload_from_yaml(dashboard)
    assert len(payload["panels"][0]["panels"]) == 999


# --------------------------------------------------------------------------- #
# Upload transport
# --------------------------------------------------------------------------- #

def test_upload_yaml_files_uses_put_with_stable_dashboard_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "dash.yaml"
        yaml_path.write_text(
            "dashboards:\n"
            "- name: Stable Dashboard\n"
            "  panels:\n"
            "  - title: Count\n"
            "    esql:\n"
            "      type: metric\n"
            "      query: FROM metrics-* | STATS count = COUNT(*)\n"
            "      primary: {field: count}\n",
            encoding="utf-8",
        )

        response = mock.Mock(status_code=200)
        response.json.return_value = {"id": "obs-migrate-stable-dashboard"}
        session = mock.Mock()
        session.put.return_value = response

        with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
            results = api.upload_yaml_files([str(yaml_path)], "https://kibana.example", api_key="k")

    assert results[0].status == "updated"
    session.put.assert_called_once()
    assert session.put.call_args.args[0].endswith("/api/dashboards/obs-migrate-stable-dashboard")
    session.post.assert_not_called()


def test_upload_yaml_files_fallback_receives_only_rejected_dashboard():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "dash.yaml"
        yaml_path.write_text(
            "dashboards:\n"
            "- name: Good\n"
            "  panels:\n"
            "  - title: Count\n"
            "    esql:\n"
            "      type: metric\n"
            "      query: FROM metrics-* | STATS count = COUNT(*)\n"
            "      primary: {field: count}\n"
            "- name: Bad\n"
            "  panels:\n"
            "  - title: Broken\n"
            "    esql:\n"
            "      type: sankey\n"
            "      query: FROM metrics-*\n",
            encoding="utf-8",
        )

        ok_response = mock.Mock(status_code=200)
        ok_response.json.return_value = {"id": "good"}
        session = mock.Mock()
        session.put.return_value = ok_response
        fallback_calls = []

        def fallback(path, dashboard):
            fallback_calls.append((Path(path).name, dashboard["name"]))

        with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
            api.upload_yaml_files([str(yaml_path)], "https://kibana.example", api_key="k", fallback=fallback)

    assert fallback_calls == [("dash.yaml", "Bad")]
    session.put.assert_called_once()


def test_upload_yaml_files_zero_leaf_with_controls_falls_back():
    # A dashboard whose panels all fail to map but that still carries a mapped
    # control is degenerate: controls filter nothing without panels, so
    # uploading it would create a panel-less dashboard while silently dropping
    # the source panels. The emptiness gate keys only on leaf panels (not
    # pinned_panels), so this must be classified "empty" and routed to the
    # legacy-import fallback rather than PUT.
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "dash.yaml"
        yaml_path.write_text(
            "dashboards:\n"
            "- name: ControlsOnly\n"
            "  panels:\n"
            "  - title: Broken\n"
            "    esql:\n"
            "      type: sankey\n"
            "      query: FROM metrics-*\n"
            "  controls:\n"
            "  - type: esql\n"
            "    label: instance\n"
            "    variable_name: instance\n"
            "    query: FROM metrics-* | STATS x\n",
            encoding="utf-8",
        )

        # Guard against a vacuous test: the payload must genuinely be the
        # zero-leaf-but-has-controls shape this regression targets.
        payload, _counts, _reasons = api.build_dashboard_payload_from_yaml(
            {
                "name": "ControlsOnly",
                "panels": [{"title": "Broken", "esql": {"type": "sankey", "query": "FROM metrics-*"}}],
                "controls": [
                    {"type": "esql", "label": "instance", "variable_name": "instance", "query": "FROM metrics-* | STATS x"}
                ],
            }
        )
        assert not api._payload_has_leaf_panels(payload)
        assert payload.get("pinned_panels")

        session = mock.Mock()
        fallback_calls = []

        def fallback(path, dashboard):
            fallback_calls.append((Path(path).name, dashboard["name"]))

        with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
            results = api.upload_yaml_files(
                [str(yaml_path)], "https://kibana.example", api_key="k", fallback=fallback,
            )

    assert results[0].status == "empty"
    assert fallback_calls == [("dash.yaml", "ControlsOnly")]
    session.put.assert_not_called()


def test_upload_yaml_files_retries_transient_5xx_before_succeeding():
    # A slow/overloaded cluster can return a transient 503 on an otherwise
    # valid payload. Without a retry, this permanently downgrades a good
    # dashboard to the legacy _import fallback (see the 20-30 dashboard
    # deep-check: ~58% of otherwise-valid dashboards fell back this way).
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "dash.yaml"
        yaml_path.write_text(
            "dashboards:\n"
            "- name: Flaky\n"
            "  panels:\n"
            "  - title: Count\n"
            "    esql:\n"
            "      type: metric\n"
            "      query: FROM metrics-* | STATS count = COUNT(*)\n"
            "      primary: {field: count}\n",
            encoding="utf-8",
        )
        transient = mock.Mock(status_code=503)
        ok_response = mock.Mock(status_code=200)
        ok_response.json.return_value = {"id": "flaky"}
        session = mock.Mock()
        session.put.side_effect = [transient, ok_response]
        fallback = mock.Mock()

        with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session), \
                mock.patch("observability_migration.targets.kibana.dashboards_api.time.sleep"):
            results = api.upload_yaml_files(
                [str(yaml_path)], "https://kibana.example", api_key="k", fallback=fallback,
            )

    assert session.put.call_count == 2
    assert results[0].status == "updated"
    fallback.assert_not_called()


def test_upload_yaml_files_falls_back_after_exhausting_retries_on_persistent_5xx():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "dash.yaml"
        yaml_path.write_text(
            "dashboards:\n"
            "- name: AlwaysDown\n"
            "  panels:\n"
            "  - title: Count\n"
            "    esql:\n"
            "      type: metric\n"
            "      query: FROM metrics-* | STATS count = COUNT(*)\n"
            "      primary: {field: count}\n",
            encoding="utf-8",
        )
        always_down = mock.Mock(status_code=503)
        session = mock.Mock()
        session.put.return_value = always_down
        fallback = mock.Mock()

        with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session), \
                mock.patch("observability_migration.targets.kibana.dashboards_api.time.sleep"):
            results = api.upload_yaml_files(
                [str(yaml_path)], "https://kibana.example", api_key="k", fallback=fallback,
            )

    assert session.put.call_count == 3
    assert results[0].status == "rejected"
    fallback.assert_called_once()


def test_upload_yaml_files_does_not_retry_genuine_client_rejection():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "dash.yaml"
        yaml_path.write_text(
            "dashboards:\n"
            "- name: Bad Payload\n"
            "  panels:\n"
            "  - title: Count\n"
            "    esql:\n"
            "      type: metric\n"
            "      query: FROM metrics-* | STATS count = COUNT(*)\n"
            "      primary: {field: count}\n",
            encoding="utf-8",
        )
        bad_request = mock.Mock(status_code=400)
        bad_request.json.return_value = {"message": "schema violation"}
        session = mock.Mock()
        session.put.return_value = bad_request
        fallback = mock.Mock()

        with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session), \
                mock.patch("observability_migration.targets.kibana.dashboards_api.time.sleep") as sleep_mock:
            results = api.upload_yaml_files(
                [str(yaml_path)], "https://kibana.example", api_key="k", fallback=fallback,
            )

    # A genuine 4xx schema rejection retrying would not change the outcome;
    # fail fast instead of wasting time/backoff on a non-transient error.
    assert session.put.call_count == 1
    sleep_mock.assert_not_called()
    assert results[0].status == "rejected"
    fallback.assert_called_once()


def test_upload_native_dashboard_retries_transient_failures():
    from observability_migration.core.assets.native_dashboard import NativeDashboard, NativeGrid, NativePanel

    native_dashboard = NativeDashboard(
        title="Retry Me",
        dashboard_id="retry-me",
        items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
    )
    timeout_exc = requests.exceptions.ConnectTimeout("connect timed out")
    ok_response = mock.Mock(status_code=201)
    ok_response.json.return_value = {"id": "retry-me"}
    session = mock.Mock()
    session.put.side_effect = [timeout_exc, ok_response]

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session), \
            mock.patch("observability_migration.targets.kibana.dashboards_api.time.sleep"):
        result = api.upload_native_dashboard(native_dashboard, "https://kibana.example", api_key="k")

    assert session.put.call_count == 2
    assert result.status == "created"
    assert result.dashboard_id == "retry-me"


def test_upload_native_dashboard_reports_rejected_when_every_attempt_raises():
    from observability_migration.core.assets.native_dashboard import NativeDashboard, NativeGrid, NativePanel

    native_dashboard = NativeDashboard(
        title="Always Times Out",
        dashboard_id="always-times-out",
        items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
    )
    session = mock.Mock()
    session.put.side_effect = requests.exceptions.ConnectTimeout("connect timed out")

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session), \
            mock.patch("observability_migration.targets.kibana.dashboards_api.time.sleep"):
        # Must not raise: a persistent network failure should be reported as
        # a rejected UploadResult so the caller's existing fallback path can
        # degrade to legacy import, instead of crashing the whole batch run.
        result = api.upload_native_dashboard(native_dashboard, "https://kibana.example", api_key="k")

    assert session.put.call_count == 3
    assert result.status == "rejected"
    assert "timed out" in result.message


# --------------------------------------------------------------------------- #
# 409 conflict classification (cross-space shareable-id collision)
# --------------------------------------------------------------------------- #

def test_upload_native_dashboard_classifies_409_as_conflict_not_rejected():
    # Dashboards are a shareable saved-object type, so a deterministic name-based
    # id that already exists in another space returns 409. That is an id-ownership
    # collision, not a payload defect: it must be classified "conflict" (a terminal
    # failure) so the caller skips the legacy kb-dashboard-cli fallback, which
    # could not resolve it and would only re-introduce the compiler dependency.
    from observability_migration.core.assets.native_dashboard import NativeDashboard, NativeGrid, NativePanel

    native_dashboard = NativeDashboard(
        title="Already Elsewhere",
        dashboard_id="already-elsewhere",
        items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
    )
    conflict = mock.Mock(status_code=409)
    conflict.json.return_value = {"message": "Saved object [dashboard/already-elsewhere] conflict"}
    session = mock.Mock()
    session.put.return_value = conflict

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_dashboard(native_dashboard, "https://kibana.example", api_key="k")

    # A single call: 409 is not a retryable status, and it is not "rejected".
    assert session.put.call_count == 1
    assert result.status == "conflict"
    assert result.http_status == 409
    assert "conflict" in result.message


def test_upload_yaml_files_does_not_fall_back_to_legacy_on_conflict():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "dash.yaml"
        yaml_path.write_text(
            "dashboards:\n"
            "- name: Shared\n"
            "  panels:\n"
            "  - title: Count\n"
            "    esql:\n"
            "      type: metric\n"
            "      query: FROM metrics-* | STATS count = COUNT(*)\n"
            "      primary: {field: count}\n",
            encoding="utf-8",
        )

        conflict = mock.Mock(status_code=409)
        conflict.json.return_value = {"message": "Saved object [dashboard/x] conflict"}
        session = mock.Mock()
        session.put.return_value = conflict
        fallback_calls = []

        def fallback(path, dashboard):
            fallback_calls.append((Path(path).name, dashboard["name"]))

        with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
            results = api.upload_yaml_files([str(yaml_path)], "https://kibana.example", api_key="k", fallback=fallback)

    assert results[0].status == "conflict"
    # A conflict is terminal: the legacy/compiler fallback must not be invoked.
    assert fallback_calls == []


# --------------------------------------------------------------------------- #
# Native control data_view_id resolution (PR #278 review regression)
# --------------------------------------------------------------------------- #

def test_upload_native_dashboard_resolves_pinned_control_data_view_id():
    native_dashboard = NativeDashboard(
        title="Has Control",
        dashboard_id="has-control",
        items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
        controls=[
            NativeControl(
                type="options_list_control",
                config={"title": "Service", "data_view_id": "metrics-*", "field_name": "service.name"},
            )
        ],
    )
    response = mock.Mock(status_code=201)
    response.json.return_value = {"id": "has-control"}
    session = mock.Mock()
    session.put.return_value = response

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_dashboard(
            native_dashboard,
            "https://kibana.example",
            api_key="k",
            # Kibana assigned a different id than the wildcard title used in YAML.
            data_view_ids={"metrics-*": "generated-id"},
        )

    assert result.status == "created"
    sent = json.loads(session.put.call_args[1]["data"])
    assert sent["pinned_panels"][0]["config"]["data_view_id"] == "generated-id"


def test_upload_native_dashboard_leaves_data_view_id_unchanged_without_mapping():
    native_dashboard = NativeDashboard(
        title="Has Control",
        dashboard_id="has-control",
        items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
        controls=[
            NativeControl(
                type="options_list_control",
                config={"title": "Service", "data_view_id": "metrics-*", "field_name": "service.name"},
            )
        ],
    )
    response = mock.Mock(status_code=201)
    response.json.return_value = {"id": "has-control"}
    session = mock.Mock()
    session.put.return_value = response

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_dashboard(native_dashboard, "https://kibana.example", api_key="k")

    assert result.status == "created"
    sent = json.loads(session.put.call_args[1]["data"])
    assert sent["pinned_panels"][0]["config"]["data_view_id"] == "metrics-*"


def test_upload_yaml_files_resolves_pinned_control_data_view_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "dash.yaml"
        yaml_path.write_text(
            "dashboards:\n"
            "- name: Has Control\n"
            "  panels:\n"
            "  - title: Count\n"
            "    esql:\n"
            "      type: metric\n"
            "      query: FROM metrics-* | STATS count = COUNT(*)\n"
            "      primary: {field: count}\n"
            "  controls:\n"
            "  - type: options\n"
            "    label: Service\n"
            "    data_view: metrics-*\n"
            "    field: service.name\n",
            encoding="utf-8",
        )
        response = mock.Mock(status_code=201)
        response.json.return_value = {"id": "has-control"}
        session = mock.Mock()
        session.put.return_value = response

        with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
            results = api.upload_yaml_files(
                [str(yaml_path)],
                "https://kibana.example",
                api_key="k",
                # Kibana assigned a different id than the wildcard title used in YAML.
                data_view_ids={"metrics-*": "generated-id"},
            )

    assert results[0].status == "created"
    sent = json.loads(session.put.call_args[1]["data"])
    assert sent["pinned_panels"][0]["config"]["data_view_id"] == "generated-id"


# --------------------------------------------------------------------------- #
# upload_native_artifact: deploy a persisted review artifact envelope
# --------------------------------------------------------------------------- #

def _native_artifact_envelope(**overrides) -> dict:
    envelope = {
        "kind": "native_dashboard",
        "version": 1,
        "dashboard_id": "obs-migrate-reviewed",
        "title": "Reviewed Dashboard",
        "source_adapter": "grafana",
        "payload": {
            "title": "Reviewed Dashboard",
            "panels": [
                {"grid": {"x": 0, "y": 0, "w": 24, "h": 8}, "type": "vis", "config": {"type": "metric"}},
            ],
        },
        "mapping": {"mapped": 1, "unmapped": 0, "sections": 0, "controls": 0, "reasons": {}},
    }
    envelope.update(overrides)
    return envelope


def test_upload_native_artifact_sends_persisted_payload_unchanged():
    artifact = _native_artifact_envelope()
    response = mock.Mock(status_code=201)
    response.json.return_value = {"id": "obs-migrate-reviewed"}
    session = mock.Mock()
    session.put.return_value = response

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact(artifact, "https://kibana.example", api_key="k")

    assert result.status == "created"
    assert result.mapped == 1
    assert result.dashboard == "Reviewed Dashboard"
    url, kwargs = session.put.call_args
    assert url[0] == "https://kibana.example/api/dashboards/obs-migrate-reviewed"
    sent = json.loads(kwargs["data"])
    assert sent == artifact["payload"]


def test_upload_native_artifact_falls_back_to_stable_id_when_missing():
    artifact = _native_artifact_envelope(dashboard_id="")
    response = mock.Mock(status_code=201)
    response.json.return_value = {"id": "obs-migrate-reviewed-dashboard"}
    session = mock.Mock()
    session.put.return_value = response

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact(artifact, "https://kibana.example", api_key="k")

    assert result.status == "created"
    url, _kwargs = session.put.call_args
    assert url[0] == "https://kibana.example/api/dashboards/obs-migrate-reviewed-dashboard"


def test_upload_native_artifact_reports_unmapped_reasons_from_mapping():
    artifact = _native_artifact_envelope(
        mapping={"mapped": 1, "unmapped": 2, "sections": 0, "controls": 0, "reasons": {"unsupported_type": 2}},
    )
    response = mock.Mock(status_code=200)
    response.json.return_value = {"id": "obs-migrate-reviewed"}
    session = mock.Mock()
    session.put.return_value = response

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact(artifact, "https://kibana.example", api_key="k")

    assert result.status == "updated"
    assert result.mapped == 1
    assert result.unmapped == 2
    assert result.unmapped_reasons == {"unsupported_type": 2}


def test_upload_native_artifact_no_leaf_panels_is_empty_not_rejected():
    artifact = _native_artifact_envelope(payload={"title": "Empty", "panels": []})
    session = mock.Mock()

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact(artifact, "https://kibana.example", api_key="k")

    # No leaf panels means nothing was ever sent -- there is no legacy YAML
    # to fall back to for a native artifact, so this is reported as "empty".
    session.put.assert_not_called()
    assert result.status == "empty"


def test_upload_native_artifact_rejection_has_no_legacy_fallback():
    artifact = _native_artifact_envelope()
    bad_request = mock.Mock(status_code=400)
    bad_request.json.return_value = {"message": "schema violation"}
    session = mock.Mock()
    session.put.return_value = bad_request

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact(artifact, "https://kibana.example", api_key="k")

    assert result.status == "rejected"
    assert "schema violation" in result.message
    # A single call: no retry (4xx is not transient) and no fallback path
    # exists for a persisted native artifact.
    assert session.put.call_count == 1


def test_upload_native_artifact_rejects_non_object_envelope():
    # ``json.loads`` of a ``.native.json`` containing ``[]`` is valid JSON but
    # a list, not the expected envelope; must be a per-record rejection, not
    # an AttributeError crashing the whole staged upload.
    session = mock.Mock()
    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact([], "https://kibana.example", api_key="k")
    assert result.status == "rejected"
    assert "object envelope" in result.message
    session.put.assert_not_called()


def test_upload_native_artifact_rejects_unexpected_kind():
    artifact = _native_artifact_envelope(kind="dashboard_ir")
    session = mock.Mock()
    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact(artifact, "https://kibana.example", api_key="k")
    assert result.status == "rejected"
    assert "kind" in result.message
    session.put.assert_not_called()


def test_upload_native_artifact_rejects_unsupported_version():
    artifact = _native_artifact_envelope(version=999)
    session = mock.Mock()
    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact(artifact, "https://kibana.example", api_key="k")
    assert result.status == "rejected"
    assert "version" in result.message
    session.put.assert_not_called()


def test_upload_native_artifact_rejects_non_object_payload():
    artifact = _native_artifact_envelope(payload=[])
    session = mock.Mock()
    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact(artifact, "https://kibana.example", api_key="k")
    assert result.status == "rejected"
    assert "payload" in result.message
    session.put.assert_not_called()


def test_upload_native_artifact_rejects_non_numeric_mapping_counter():
    # ``mapping.mapped: "corrupt"`` is valid JSON but would raise ValueError
    # in ``int(...)``; must be reported as a rejected record instead.
    artifact = _native_artifact_envelope(
        mapping={"mapped": "corrupt", "unmapped": 0, "reasons": {}},
    )
    session = mock.Mock()
    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact(artifact, "https://kibana.example", api_key="k")
    assert result.status == "rejected"
    assert "mapping.mapped" in result.message
    session.put.assert_not_called()


def test_upload_native_artifact_resolves_pinned_control_data_view_id():
    artifact = _native_artifact_envelope(
        payload={
            "title": "Has Control",
            "panels": [
                {"grid": {"x": 0, "y": 0, "w": 24, "h": 8}, "type": "vis", "config": {"type": "metric"}},
            ],
            "pinned_panels": [
                {
                    "type": "options_list_control",
                    "config": {"title": "Service", "data_view_id": "metrics-*", "field_name": "service.name"},
                }
            ],
        },
    )
    response = mock.Mock(status_code=201)
    response.json.return_value = {"id": "obs-migrate-reviewed"}
    session = mock.Mock()
    session.put.return_value = response

    with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
        result = api.upload_native_artifact(
            artifact,
            "https://kibana.example",
            api_key="k",
            data_view_ids={"metrics-*": "generated-id"},
        )

    assert result.status == "created"
    sent = json.loads(session.put.call_args[1]["data"])
    assert sent["pinned_panels"][0]["config"]["data_view_id"] == "generated-id"


# --------------------------------------------------------------------------- #
# Per-chart metric color allow-lists (schema-verified live on 9.5.0)
# --------------------------------------------------------------------------- #

_DYNAMIC = {"type": "dynamic", "range": "absolute", "steps": [{"gte": 80, "color": "#E7664C"}]}
_STATIC = {"type": "static", "color": "#54B399"}
_CATEGORICAL = {"mode": "categorical", "palette": "default", "mapping": [{"values": ["a"], "color": "#ffffff"}]}


def test_pie_metric_drops_dynamic_color_keeps_static():
    cfg = _map({"type": "pie", "query": "FROM m", "metrics": [{"field": "v", "color": _DYNAMIC}],
                "breakdowns": [{"field": "g"}]})["config"]
    assert "color" not in cfg["metrics"][0]
    cfg2 = _map({"type": "pie", "query": "FROM m", "metrics": [{"field": "v", "color": _STATIC}],
                 "breakdowns": [{"field": "g"}]})["config"]
    assert cfg2["metrics"][0]["color"] == _STATIC


def test_treemap_metric_drops_dynamic_color():
    cfg = _map({"type": "treemap", "query": "FROM m", "metrics": [{"field": "v", "color": _DYNAMIC}],
                "breakdowns": [{"field": "g"}]})["config"]
    assert "color" not in cfg["metrics"][0]


def test_mosaic_metric_never_has_color():
    cfg = _map({"type": "mosaic", "query": "FROM m", "metric": {"field": "v", "color": _DYNAMIC},
                "breakdowns": [{"field": "g"}]})["config"]
    assert "color" not in cfg["metric"]


def test_heatmap_metric_drops_static_color_keeps_dynamic():
    cfg = _map({"type": "heatmap", "query": "FROM m", "x_axis": {"field": "x"},
                "metric": {"field": "v", "color": _STATIC}})["config"]
    assert "color" not in cfg["metric"]
    cfg2 = _map({"type": "heatmap", "query": "FROM m", "x_axis": {"field": "x"},
                 "metric": {"field": "v", "color": _DYNAMIC}})["config"]
    assert cfg2["metric"]["color"] == _DYNAMIC


def test_metric_primary_drops_categorical_keeps_dynamic():
    cfg = _map({"type": "metric", "query": "FROM m", "primary": {"field": "v", "color": _CATEGORICAL}})["config"]
    assert "color" not in cfg["metrics"][0]
    cfg2 = _map({"type": "metric", "query": "FROM m", "primary": {"field": "v", "color": _DYNAMIC}})["config"]
    assert cfg2["metrics"][0]["color"] == _DYNAMIC


def test_datatable_metric_drops_static_keeps_dynamic():
    cfg = _map({"type": "datatable", "query": "FROM m", "metrics": [{"field": "v", "color": _STATIC}]})["config"]
    assert "color" not in cfg["metrics"][0]
    cfg2 = _map({"type": "datatable", "query": "FROM m", "metrics": [{"field": "v", "color": _DYNAMIC}]})["config"]
    assert cfg2["metrics"][0]["color"] == _DYNAMIC


def test_partition_group_drops_auto_color_keeps_mapping():
    cfg = _map({"type": "pie", "query": "FROM m", "metrics": [{"field": "v"}],
                "breakdowns": [{"field": "g", "color": {"type": "auto"}}]})["config"]
    assert "color" not in cfg["group_by"][0]
    cfg2 = _map({"type": "pie", "query": "FROM m", "metrics": [{"field": "v"}],
                 "breakdowns": [{"field": "g", "color": _CATEGORICAL}]})["config"]
    assert cfg2["group_by"][0]["color"] == _CATEGORICAL


def test_xy_breakdown_drops_dynamic_color_keeps_mapping():
    cfg = _map({"type": "line", "query": "FROM m", "dimension": {"field": "t"}, "metrics": [{"field": "v"}],
                "breakdown": {"field": "svc", "color": _DYNAMIC}})["config"]
    assert "color" not in cfg["layers"][0]["breakdown_by"]


def test_metric_breakdown_never_has_color():
    cfg = _map({"type": "metric", "query": "FROM m", "primary": {"field": "v"},
                "breakdown": {"field": "svc", "color": _CATEGORICAL}})["config"]
    assert "color" not in cfg["breakdown_by"]


def test_gauge_metric_strips_invalid_ticks_and_title_keys():
    cfg = _map({"type": "gauge", "query": "FROM m", "metric": {
        "field": "v",
        "ticks": {"visible": True, "mode": "auto", "bogus": 1},
        "title": {"text": "T", "visible": True, "junk": "x"},
    }})["config"]
    assert cfg["metric"]["ticks"] == {"visible": True, "mode": "auto"}
    assert cfg["metric"]["title"] == {"text": "T", "visible": True}


def test_map_panel_report_markdown_includes_settings():
    panel = {
        "title": "Notes",
        "visual_ir": {"layout": {"x": 0, "y": 0, "w": 12, "h": 4},
                      "presentation": {"kind": "markdown", "config": {"content": "hi"}}},
    }
    result = api.map_panel(panel)
    assert result.api_panel is not None
    assert result.api_panel["config"].get("settings") == {}


def test_selected_options_excludes_bool_defaults():
    control = {"variable_name": "on", "label": "On", "query": "FROM m | KEEP x",
               "default": True}
    pinned = api.map_yaml_control(control)
    assert pinned["config"]["selected_options"] == []


def test_upload_yaml_files_disambiguates_duplicate_titles():
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / "dash.yaml"
        yaml_path.write_text(
            "dashboards:\n"
            "- name: Same Title\n"
            "  panels:\n"
            "  - title: Count\n"
            "    esql: {type: metric, query: FROM m | STATS c = COUNT(*), primary: {field: c}}\n"
            "- name: Same Title\n"
            "  panels:\n"
            "  - title: Count\n"
            "    esql: {type: metric, query: FROM m | STATS c = COUNT(*), primary: {field: c}}\n",
            encoding="utf-8",
        )
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"id": "x"}
        session = mock.Mock()
        session.put.return_value = resp
        with mock.patch("observability_migration.targets.kibana.dashboards_api._session", return_value=session):
            api.upload_yaml_files([str(yaml_path)], "https://kibana.example", api_key="k")
    put_urls = [call.args[0] for call in session.put.call_args_list]
    assert len(put_urls) == 2
    assert len(set(put_urls)) == 2, f"duplicate PUT ids collide: {put_urls}"


# --------------------------------------------------------------------------- #
# Fidelity fixes (native-layer bugs surfaced by the panel-mapping audit)
# --------------------------------------------------------------------------- #

def test_selected_options_reads_preselected_defaults():
    # Bug #1: Datadog template-variable controls carry defaults under
    # ``preselected`` (see datadog generate.py); the mapper must honor them.
    control = {"variable_name": "env", "label": "Env",
               "query": "FROM m | KEEP env", "preselected": ["prod"]}
    pinned = api.map_yaml_control(control)
    assert pinned["config"]["selected_options"] == ["prod"]


def test_options_list_control_reads_preselected_defaults():
    control = {"type": "options", "field_name": "host", "data_view_id": "dv",
               "label": "Host", "preselected": ["web-1", "web-2"]}
    pinned = api.map_yaml_control(control)
    assert pinned["config"]["selected_options"] == ["web-1", "web-2"]


def test_api_format_accepts_bare_duration():
    # Bug #4: a bare ``{type: duration}`` (Datadog ``second`` unit) must survive
    # instead of being dropped.
    #
    # Verified live on Serverless 9.5.0: the single-value metric/gauge format
    # schema accepts a bare ``{type: duration}``, but the multi-column schema
    # (xy/data_table/etc.) *requires* ``from``/``to`` — omitting them 400s the
    # whole panel. Both schemas accept the pair harmlessly, so ``_api_format``
    # always fills in defaults (seconds -> humanize) rather than only
    # forwarding an already-complete pair.
    assert api._api_format({"type": "duration"}) == {
        "type": "duration", "from": "seconds", "to": "humanize",
    }
    assert api._api_format({"type": "duration", "decimals": 1, "suffix": " s"}) == {
        "type": "duration", "decimals": 1, "suffix": " s", "from": "seconds", "to": "humanize",
    }
    assert api._api_format({"type": "duration", "from": "milliseconds"}) == {
        "type": "duration", "from": "milliseconds", "to": "humanize",
    }


def test_heatmap_reads_legend_from_appearance():
    # Bug #5: Datadog emits heatmap legend under ``appearance.legend``.
    cfg = _map({
        "type": "heatmap", "query": "FROM m",
        "x_axis": {"field": "time_bucket"},
        "y_axis": {"field": "service.name"},
        "metric": {"field": "value"},
        "appearance": {"legend": {"visible": "show", "position": "right"}},
    })["config"]
    assert cfg["legend"] == {"visibility": "visible"}


def test_section_panel_cap_records_drop_reason_without_inflating_mapped():
    # Panels beyond the combined dashboard item cap must be recorded as a drop
    # reason and NOT hidden as API-acceptable payload.
    inner = [
        _leaf({"type": "metric", "query": "FROM m", "primary": {"field": "v"}}, f"p{i}")
        for i in range(1050)
    ]
    dashboard = {"name": "D", "panels": [{"title": "S", "section": {"panels": inner}}]}
    payload, counts, reasons = api.build_dashboard_payload_from_yaml(dashboard)
    assert len(payload["panels"][0]["panels"]) == 999
    assert counts["mapped"] == 999
    assert reasons.get("dropped_over_section_panel_cap") == 50
    assert reasons.get("dropped_over_total_item_cap") == 1
