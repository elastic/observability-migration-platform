# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the Grafana dashboard -> AssetSelectionMetadata mapper."""

from __future__ import annotations

from datetime import UTC, datetime

from observability_migration.adapters.source.grafana.extract import (
    selection_metadata_from_grafana_dashboard,
)


def test_extracts_folder_tags_updated_starred_from_meta():
    dash = {
        "title": "Node Exporter",
        "uid": "u1",
        "tags": ["linux", "prod"],
        "_grafana_meta": {
            "folderTitle": "Infra",
            "updated": "2026-01-10T00:00:00Z",
            "isStarred": True,
        },
        "panels": [],
    }
    meta = selection_metadata_from_grafana_dashboard(dash)
    assert meta.folder == "Infra"
    assert meta.tags == ["linux", "prod"]
    assert meta.updated_at == datetime(2026, 1, 10, tzinfo=UTC)
    assert meta.starred is True
    assert meta.team is None


def test_datasources_collected_from_panels_and_rows():
    dash = {
        "title": "X",
        "tags": [],
        "_grafana_meta": {},
        "panels": [
            {
                "type": "timeseries",
                "datasource": {"type": "prometheus", "uid": "p1"},
                "targets": [{"datasource": {"type": "prometheus", "uid": "p1"}}],
            },
            {
                "type": "row",
                "panels": [{"datasource": {"type": "elasticsearch", "uid": "e1"}}],
            },
        ],
    }
    meta = selection_metadata_from_grafana_dashboard(dash)
    assert set(meta.datasources) == {"prometheus", "elasticsearch"}


def test_string_datasource_supported():
    dash = {"tags": [], "_grafana_meta": {}, "panels": [{"datasource": "Prometheus"}]}
    meta = selection_metadata_from_grafana_dashboard(dash)
    assert "Prometheus" in meta.datasources


def test_missing_meta_marks_folder_and_starred_unavailable():
    dash = {"title": "X", "tags": ["a"], "panels": []}
    meta = selection_metadata_from_grafana_dashboard(dash)
    assert meta.folder is None
    assert meta.starred is None
    assert meta.updated_at is None
    # tags are a first-class dashboard concept: present-but-empty, not unavailable
    assert meta.tags == ["a"]


def test_missing_tags_is_empty_list():
    dash = {"title": "X", "_grafana_meta": {}, "panels": []}
    meta = selection_metadata_from_grafana_dashboard(dash)
    assert meta.tags == []
