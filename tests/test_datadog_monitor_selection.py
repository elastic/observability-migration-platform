# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Integration tests for --select-* filtering on the Datadog monitor pipeline."""

from __future__ import annotations

import json
from unittest.mock import patch

from observability_migration.adapters.source.datadog import cli as datadog_cli


def _write_monitors(tmp_path):
    monitors_dir = tmp_path / "monitors"
    monitors_dir.mkdir()
    (monitors_dir / "monitors.json").write_text(json.dumps({"monitors": [
        {
            "id": 1, "name": "Infra M", "type": "query alert",
            "query": "avg(last_5m):avg:system.cpu.user{*} > 1",
            "tags": ["team:infra"], "message": "m",
            "options": {"thresholds": {"critical": 1}},
        },
        {
            "id": 2, "name": "Pay M", "type": "query alert",
            "query": "avg(last_5m):avg:system.cpu.user{*} > 2",
            "tags": ["team:payments"], "message": "m",
            "options": {"thresholds": {"critical": 2}},
        },
    ]}))


def _run(tmp_path, out_dir, *select_args):
    argv = [
        "--source", "files",
        "--input-dir", str(tmp_path),
        "--output-dir", str(out_dir),
        "--assets", "alerts",
        "--field-profile", "otel",
        *select_args,
    ]
    with patch.object(datadog_cli, "_load_live_field_capabilities"):
        datadog_cli.main(argv)


def test_select_tag_narrows_monitors(tmp_path, capsys):
    _write_monitors(tmp_path)
    _run(tmp_path, tmp_path / "out", "--select-tag", "team:infra")
    out = capsys.readouterr().out
    assert "Selected 1 of 2" in out
    assert "Total: 1" in out


def test_select_folder_on_monitors_warns_and_keeps_all(tmp_path, capsys):
    # Monitors cannot supply folder -> degrade gracefully (keep + warn).
    _write_monitors(tmp_path)
    _run(tmp_path, tmp_path / "out", "--select-folder", "Prod")
    out = capsys.readouterr().out
    assert "WARN" in out and "folder" in out.lower()
    assert "Total: 2" in out
