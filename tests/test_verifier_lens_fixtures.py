# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for LensConfigBuilder fixture oracle scaffold."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import lens_fixtures  # noqa: E402


def _write_fixture(
    path: Path,
    name: str,
    chart_type: str,
    *,
    series_type: str = "",
    data_source: str = "esql",
    attributes: dict | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "name": name,
                "chart_type": chart_type,
                "series_type": series_type,
                "data_source": data_source,
                "attributes": attributes if attributes is not None else {"state": {}},
            }
        )
    )


class TestLensFixtures:
    def test_load_fixture_and_coverage_key(self, tmp_path: Path) -> None:
        path = tmp_path / "xy-line.json"
        _write_fixture(path, "xy-line", "xy", series_type="line")
        fixture = lens_fixtures.load_fixture(path)
        assert fixture.name == "xy-line"
        assert fixture.coverage_key == "xy:line"

    def test_load_raw_metric_lens_attributes(self, tmp_path: Path) -> None:
        path = tmp_path / "metric-basic-esql.json"
        path.write_text(json.dumps({
            "title": "Basic Count Metric",
            "visualizationType": "lnsMetric",
            "state": {
                "datasourceStates": {"textBased": {"layers": {}}},
                "visualization": {"layerId": "layer_0"},
            },
        }))
        fixture = lens_fixtures.load_fixture(path)
        assert fixture.source_format == "raw_attributes"
        assert fixture.chart_type == "metric"
        assert fixture.data_source == "esql"
        assert fixture.coverage_key == "metric"

    def test_load_raw_xy_lens_attributes_infers_series_type(self, tmp_path: Path) -> None:
        path = tmp_path / "xy-chart-bar-dataview.json"
        path.write_text(json.dumps({
            "title": "Events Over Time",
            "visualizationType": "lnsXY",
            "state": {
                "datasourceStates": {"formBased": {"layers": {}}},
                "visualization": {
                    "layers": [{"seriesType": "bar"}],
                },
            },
        }))
        fixture = lens_fixtures.load_fixture(path)
        assert fixture.source_format == "raw_attributes"
        assert fixture.chart_type == "xy"
        assert fixture.series_type == "bar"
        assert fixture.data_source == "data_view"
        assert fixture.coverage_key == "xy:bar"

    def test_layer_series_type_wins_over_preferred_series_type(self, tmp_path: Path) -> None:
        path = tmp_path / "xy-chart-bar-esql.json"
        path.write_text(json.dumps({
            "visualizationType": "lnsXY",
            "state": {
                "datasourceStates": {"textBased": {"layers": {}}},
                "visualization": {
                    "preferredSeriesType": "line",
                    "layers": [{"seriesType": "bar"}],
                },
            },
        }))
        assert lens_fixtures.load_fixture(path).coverage_key == "xy:bar"

    def test_validate_fixture_contract(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        _write_fixture(path, "bad", "", data_source="other", attributes={})
        fixture = lens_fixtures.load_fixture(path)
        errors = lens_fixtures.validate_fixture(fixture)
        assert "missing chart_type" in errors
        assert "unsupported data_source 'other'" in errors
        assert "missing attributes" in errors

    def test_coverage_report_missing_required_type(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path / "xy-line.json", "xy-line", "xy", series_type="line")
        result = lens_fixtures.validate_fixture_dir(
            tmp_path, {"xy:line", "xy:bar", "metric"}
        )
        assert not result["ok"]
        assert result["coverage"]["missing"] == ["metric", "xy:bar"]

    def test_coverage_report_ok(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path / "xy-line.json", "xy-line", "xy", series_type="line")
        _write_fixture(tmp_path / "metric.json", "metric", "metric")
        (tmp_path / "report.json").write_text(json.dumps({"ok": True}))
        result = lens_fixtures.validate_fixture_dir(tmp_path, {"xy:line", "metric"})
        assert result["ok"]
        assert result["fixtures"] == 2
        assert result["coverage"]["missing"] == []
        assert result["by_source_format"] == {"wrapper": 2}
        assert result["by_data_source"] == {"esql": 2}

    def test_validate_real_generator_raw_fixture_dir_shape(self, tmp_path: Path) -> None:
        (tmp_path / "metric-basic-esql.json").write_text(json.dumps({
            "visualizationType": "lnsMetric",
            "state": {
                "datasourceStates": {
                    "textBased": {
                        "layers": {
                            "layer_0": {
                                "columns": [{"columnId": "metric_accessor"}],
                                "allColumns": [{"columnId": "metric_accessor"}],
                            }
                        }
                    }
                },
                "visualization": {"metricAccessor": "metric_accessor"},
            },
        }))
        (tmp_path / "xy-chart-esql.json").write_text(json.dumps({
            "visualizationType": "lnsXY",
            "state": {
                "datasourceStates": {
                    "textBased": {
                        "layers": {
                            "layer_0": {
                                "columns": [
                                    {"columnId": "x_accessor"},
                                    {"columnId": "y_accessor"},
                                ],
                                "allColumns": [
                                    {"columnId": "x_accessor"},
                                    {"columnId": "y_accessor"},
                                ],
                            }
                        }
                    }
                },
                "visualization": {
                    "preferredSeriesType": "line",
                    "layers": [
                        {
                            "xAccessor": "x_accessor",
                            "accessors": ["y_accessor"],
                            "seriesType": "line",
                            "yConfig": [{"forAccessor": "y_accessor"}],
                        }
                    ],
                },
            },
        }))
        result = lens_fixtures.validate_fixture_dir(tmp_path, {"metric", "xy:line"})
        assert result["ok"]
        assert result["by_source_format"] == {"raw_attributes": 2}
        assert result["by_data_source"] == {"esql": 2}

    def test_raw_fixture_missing_accessor_is_invalid(self, tmp_path: Path) -> None:
        (tmp_path / "xy-chart-esql.json").write_text(json.dumps({
            "visualizationType": "lnsXY",
            "state": {
                "datasourceStates": {
                    "textBased": {
                        "layers": {
                            "layer_0": {
                                "columns": [{"columnId": "x_accessor"}],
                                "allColumns": [{"columnId": "x_accessor"}],
                            }
                        }
                    }
                },
                "visualization": {
                    "preferredSeriesType": "line",
                    "layers": [
                        {
                            "xAccessor": "x_accessor",
                            "accessors": ["missing_y"],
                            "seriesType": "line",
                            "yConfig": [{"forAccessor": "missing_y"}],
                        }
                    ],
                },
            },
        }))
        result = lens_fixtures.validate_fixture_dir(tmp_path, {"xy:line"})
        assert not result["ok"]
        errors = result["errors"]["xy-chart-esql"]
        assert "visualization references missing columnId 'missing_y'" in errors

    def test_dataview_fixture_accessor_validation_is_skipped(self, tmp_path: Path) -> None:
        # formBased/data-view fixtures use different column structures; this
        # oracle only validates raw textBased column IDs.
        (tmp_path / "xy-chart-dataview.json").write_text(json.dumps({
            "visualizationType": "lnsXY",
            "state": {
                "datasourceStates": {"formBased": {"layers": {}}},
                "visualization": {
                    "preferredSeriesType": "line",
                    "layers": [{"xAccessor": "not_in_text_based", "accessors": ["also_missing"], "seriesType": "line"}],
                },
            },
        }))
        result = lens_fixtures.validate_fixture_dir(tmp_path, {"xy:line"})
        assert result["ok"]

