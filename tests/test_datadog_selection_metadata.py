# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for Datadog dashboard/monitor -> AssetSelectionMetadata mappers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from observability_migration.adapters.source.datadog.extract import (
    selection_metadata_from_datadog_dashboard,
    selection_metadata_from_datadog_monitor,
)

_SAMPLE = Path(__file__).parent.parent / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"


def test_dashboard_tags_team_and_datadog_datasource():
    raw = json.loads(_SAMPLE.read_text())
    meta = selection_metadata_from_datadog_dashboard(raw)
    assert meta.tags == ["env:prod", "team:infra"]
    assert meta.team == "infra"  # derived from the team: tag
    assert meta.datasources == ["datadog"]


def test_dashboard_folder_and_starred_unavailable():
    raw = json.loads(_SAMPLE.read_text())
    meta = selection_metadata_from_datadog_dashboard(raw)
    # Datadog dashboard folders live in the Dashboard Lists API (not fetched);
    # starred/popularity is not in the get_dashboard payload.
    assert meta.folder is None
    assert meta.starred is None


def test_dashboard_updated_at_from_modified_at_epoch():
    raw = {"tags": [], "modified_at": 1767225600}  # 2026-01-01T00:00:00Z
    meta = selection_metadata_from_datadog_dashboard(raw)
    assert meta.updated_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_dashboard_updated_at_from_modified_at_iso():
    raw = {"tags": [], "modified_at": "2026-01-01T00:00:00+00:00"}
    meta = selection_metadata_from_datadog_dashboard(raw)
    assert meta.updated_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_dashboard_no_team_tag_gives_none_team():
    raw = {"tags": ["env:prod"]}
    meta = selection_metadata_from_datadog_dashboard(raw)
    assert meta.team is None


def test_monitor_tags_team_updated():
    raw = {"id": 1, "tags": ["team:payments", "env:prod"], "modified": "2026-02-01T00:00:00Z"}
    meta = selection_metadata_from_datadog_monitor(raw)
    assert meta.tags == ["team:payments", "env:prod"]
    assert meta.team == "payments"
    assert meta.updated_at == datetime(2026, 2, 1, tzinfo=UTC)
    assert meta.folder is None
    assert meta.datasources is None
    assert meta.starred is None
