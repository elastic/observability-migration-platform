# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Integration tests for --select-* filtering on the Datadog dashboard pipeline."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from observability_migration.adapters.source.datadog import cli as datadog_cli


def _write_dashboards(tmp_path):
    (tmp_path / "infra.json").write_text(json.dumps({
        "id": "d-infra", "title": "Infra Dash", "tags": ["team:infra"], "widgets": [],
    }))
    (tmp_path / "payments.json").write_text(json.dumps({
        "id": "d-pay", "title": "Payments Dash", "tags": ["team:payments"], "widgets": [],
    }))


def _run(tmp_path, out_dir, *select_args):
    argv = [
        "--source", "files",
        "--input-dir", str(tmp_path),
        "--output-dir", str(out_dir),
        "--assets", "dashboards",
        "--field-profile", "otel",
        *select_args,
    ]
    with patch.object(datadog_cli, "_load_live_field_capabilities"):
        datadog_cli.main(argv)


def test_select_tag_narrows_to_matching_dashboard(tmp_path, capsys):
    _write_dashboards(tmp_path)
    _run(tmp_path, tmp_path / "out", "--select-tag", "team:infra")
    out = capsys.readouterr().out
    assert "Processing: Infra Dash" in out
    assert "Processing: Payments Dash" not in out
    assert "Selected 1 of 2" in out


def test_select_tag_matching_nothing_exits_1(tmp_path):
    _write_dashboards(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, tmp_path / "out", "--select-tag", "team:nope")
    assert exc.value.code == 1


def test_select_folder_on_datadog_warns_and_keeps_all(tmp_path, capsys):
    # Datadog dashboards cannot supply folder -> degrade gracefully (keep + warn).
    _write_dashboards(tmp_path)
    _run(tmp_path, tmp_path / "out", "--select-folder", "Prod")
    out = capsys.readouterr().out
    assert "WARN" in out and "folder" in out.lower()
    assert "Processing: Infra Dash" in out
    assert "Processing: Payments Dash" in out


def test_bad_select_date_exits_1(tmp_path):
    _write_dashboards(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, tmp_path / "out", "--select-updated-after", "garbage")
    assert exc.value.code == 1
