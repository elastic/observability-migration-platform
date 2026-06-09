# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Integration tests for --select-* filtering on the Grafana dashboard pipeline."""

from __future__ import annotations

import json

import pytest

from observability_migration.adapters.source.grafana import cli as grafana_cli


def _write_dashboards(tmp_path):
    (tmp_path / "infra.json").write_text(json.dumps({
        "title": "Infra Dash", "uid": "u-infra", "tags": ["linux"],
        "schemaVersion": 30, "panels": [],
    }))
    (tmp_path / "web.json").write_text(json.dumps({
        "title": "Web Dash", "uid": "u-web", "tags": ["web"],
        "schemaVersion": 30, "panels": [],
    }))


def _run(tmp_path, out_dir, *select_args):
    grafana_cli.main([
        "--source", "files",
        "--input-dir", str(tmp_path),
        "--output-dir", str(out_dir),
        "--assets", "dashboards",
        "--field-profile", "otel",
        *select_args,
    ])


def test_select_tag_narrows_to_matching_dashboard(tmp_path, capsys):
    _write_dashboards(tmp_path)
    _run(tmp_path, tmp_path / "out", "--select-tag", "linux")
    out = capsys.readouterr().out
    assert "Found 1 dashboards" in out
    assert "Selected 1 of 2" in out


def test_select_tag_matching_nothing_exits_1(tmp_path):
    _write_dashboards(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, tmp_path / "out", "--select-tag", "nope")
    assert exc.value.code == 1


def test_select_team_on_grafana_dashboard_warns_and_keeps_all(tmp_path, capsys):
    # Grafana dashboards have no first-class team -> degrade gracefully.
    _write_dashboards(tmp_path)
    _run(tmp_path, tmp_path / "out", "--select-team", "infra")
    out = capsys.readouterr().out
    assert "WARN" in out and "team" in out.lower()
    assert "Found 2 dashboards" in out
