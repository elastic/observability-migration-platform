# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Integration tests for --select-* filtering on the Grafana alert pipeline.

Grafana alert subsetting did not previously exist; selection makes it possible.
"""

from __future__ import annotations

import json

from observability_migration.adapters.source.grafana import cli as grafana_cli


def _write_unified_rules(tmp_path):
    (tmp_path / "grafana_alert_rules.json").write_text(json.dumps({"alert_rules": [
        {"title": "Infra Rule", "uid": "r-infra", "condition": "A",
         "labels": {"team": "infra"}, "data": []},
        {"title": "Pay Rule", "uid": "r-pay", "condition": "A",
         "labels": {"team": "payments"}, "data": []},
    ]}))


def _run(tmp_path, out_dir, *select_args):
    grafana_cli.main([
        "--source", "files",
        "--input-dir", str(tmp_path),
        "--output-dir", str(out_dir),
        "--assets", "alerts",
        "--field-profile", "otel",
        *select_args,
    ])


def test_select_tag_narrows_unified_alert_rules(tmp_path, capsys):
    _write_unified_rules(tmp_path)
    _run(tmp_path, tmp_path / "out", "--select-tag", "team:infra")
    out = capsys.readouterr().out
    assert "Selected 1 of 2" in out
    assert "unified=1" in out


def test_select_folder_on_unified_rules_warns_and_keeps_all(tmp_path, capsys):
    # Unified rules expose folderUID (not a name) -> folder degrades gracefully.
    _write_unified_rules(tmp_path)
    _run(tmp_path, tmp_path / "out", "--select-folder", "Prod")
    out = capsys.readouterr().out
    assert "WARN" in out and "folder" in out.lower()
    assert "unified=2" in out


def test_select_team_narrows_unified_alert_rules(tmp_path, capsys):
    _write_unified_rules(tmp_path)
    _run(tmp_path, tmp_path / "out", "--select-team", "payments")
    out = capsys.readouterr().out
    assert "unified=1" in out
