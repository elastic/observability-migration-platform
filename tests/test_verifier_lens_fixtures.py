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
        result = lens_fixtures.validate_fixture_dir(tmp_path, {"xy:line", "metric"})
        assert result["ok"]
        assert result["coverage"]["missing"] == []

