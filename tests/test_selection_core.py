# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the shared, source-agnostic metadata selection core."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from observability_migration.core.selection import (
    AssetSelectionMetadata,
    SelectionCriteria,
    apply_cli_selection,
    criteria_from_args,
    filter_assets,
    matches,
    parse_selection_datetime,
)


def _meta(**overrides):
    base = dict(
        folder="Production",
        tags=["env:prod", "team:infra"],
        datasources=["prometheus"],
        team="infra",
        updated_at=datetime(2026, 1, 15, tzinfo=UTC),
        starred=True,
    )
    base.update(overrides)
    return AssetSelectionMetadata(**base)


# --- is_empty / no-op -------------------------------------------------------

def test_empty_criteria_is_empty():
    assert SelectionCriteria().is_empty is True


def test_empty_criteria_matches_everything_without_warnings():
    matched, warnings = matches(_meta(), SelectionCriteria())
    assert matched is True
    assert warnings == []


# --- folder -----------------------------------------------------------------

def test_folder_exact_case_insensitive_match():
    matched, _ = matches(_meta(folder="Prod"), SelectionCriteria(folders=["prod"]))
    assert matched is True


def test_folder_non_match():
    matched, warnings = matches(_meta(folder="Staging"), SelectionCriteria(folders=["prod"]))
    assert matched is False
    assert warnings == []


def test_folder_or_within_dimension():
    crit = SelectionCriteria(folders=["a", "b"])
    assert matches(_meta(folder="b"), crit)[0] is True
    assert matches(_meta(folder="c"), crit)[0] is False


# --- AND across dimensions --------------------------------------------------

def test_and_across_dimensions():
    crit = SelectionCriteria(folders=["Production"], tags=["env:staging"])
    # folder matches but tag does not -> overall no match
    matched, _ = matches(_meta(), crit)
    assert matched is False


# --- tags / datasources membership -----------------------------------------

def test_tag_membership_case_insensitive():
    matched, _ = matches(_meta(tags=["env:prod", "Team:Infra"]),
                         SelectionCriteria(tags=["team:infra"]))
    assert matched is True


def test_datasource_or_membership():
    crit = SelectionCriteria(datasources=["elasticsearch", "prometheus"])
    assert matches(_meta(datasources=["prometheus"]), crit)[0] is True
    assert matches(_meta(datasources=["loki"]), crit)[0] is False


# --- team -------------------------------------------------------------------

def test_team_exact_case_insensitive():
    assert matches(_meta(team="Infra"), SelectionCriteria(teams=["infra"]))[0] is True
    assert matches(_meta(team="payments"), SelectionCriteria(teams=["infra"]))[0] is False


# --- updated window ---------------------------------------------------------

def test_updated_after_inclusive_boundary():
    boundary = datetime(2026, 1, 15, tzinfo=UTC)
    crit = SelectionCriteria(updated_after=boundary)
    assert matches(_meta(updated_at=boundary), crit)[0] is True
    assert matches(_meta(updated_at=datetime(2026, 1, 14, tzinfo=UTC)), crit)[0] is False


def test_updated_before_inclusive_boundary():
    boundary = datetime(2026, 1, 15, tzinfo=UTC)
    crit = SelectionCriteria(updated_before=boundary)
    assert matches(_meta(updated_at=boundary), crit)[0] is True
    assert matches(_meta(updated_at=datetime(2026, 1, 16, tzinfo=UTC)), crit)[0] is False


def test_updated_window_both_bounds():
    crit = SelectionCriteria(
        updated_after=datetime(2026, 1, 1, tzinfo=UTC),
        updated_before=datetime(2026, 1, 31, tzinfo=UTC),
    )
    assert matches(_meta(updated_at=datetime(2026, 1, 15, tzinfo=UTC)), crit)[0] is True
    assert matches(_meta(updated_at=datetime(2026, 2, 1, tzinfo=UTC)), crit)[0] is False


# --- starred ----------------------------------------------------------------

def test_starred_true_match():
    assert matches(_meta(starred=True), SelectionCriteria(starred=True))[0] is True
    assert matches(_meta(starred=False), SelectionCriteria(starred=True))[0] is False


# --- degrade gracefully: unavailable dimension -> keep + warn ---------------

def test_unavailable_folder_keeps_asset_with_warning():
    matched, warnings = matches(
        _meta(folder=None),
        SelectionCriteria(folders=["prod"]),
        label="datadog dashboard",
    )
    assert matched is True
    assert len(warnings) == 1
    assert "folder" in warnings[0].lower()
    assert "datadog dashboard" in warnings[0]


def test_unavailable_dimension_does_not_block_other_dimensions():
    # team unavailable (warn + keep on team), but tag genuinely does not match -> no match
    matched, warnings = matches(
        _meta(team=None, tags=["env:prod"]),
        SelectionCriteria(teams=["infra"], tags=["env:staging"]),
    )
    assert matched is False
    assert any("team" in w.lower() for w in warnings)


def test_blank_folder_is_supplied_not_unavailable():
    # folder="" means supplied-but-empty -> genuine non-match, no warning
    matched, warnings = matches(_meta(folder=""), SelectionCriteria(folders=["prod"]))
    assert matched is False
    assert warnings == []


def test_unavailable_starred_keeps_asset_with_warning():
    matched, warnings = matches(_meta(starred=None), SelectionCriteria(starred=True),
                                label="datadog dashboard")
    assert matched is True
    assert any("starred" in w.lower() for w in warnings)


# --- filter_assets ----------------------------------------------------------

def test_filter_assets_keeps_matches_and_dedupes_warnings():
    items = [
        {"folder": "prod", "tags": []},
        {"folder": "staging", "tags": []},
        {"folder": None, "tags": []},
    ]

    def get_meta(item):
        return AssetSelectionMetadata(
            folder=item["folder"], tags=item["tags"], datasources=[],
            team=None, updated_at=None, starred=None,
        )

    crit = SelectionCriteria(folders=["prod"])
    kept, warnings = filter_assets(items, get_meta, crit, label="dd dashboard")
    # prod matches; staging no; None (unavailable) kept+warn
    assert {i["folder"] for i in kept} == {"prod", None}
    # warning de-duped to a single folder-unavailable message
    assert len(warnings) == 1
    assert "folder" in warnings[0].lower()


# --- parse_selection_datetime ----------------------------------------------

def test_parse_iso_date():
    assert parse_selection_datetime("2026-01-01") == datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_iso_datetime_with_z():
    assert parse_selection_datetime("2026-01-01T12:30:00Z") == datetime(
        2026, 1, 1, 12, 30, 0, tzinfo=UTC
    )


def test_parse_epoch_seconds():
    # 2026-01-01T00:00:00Z == 1767225600
    assert parse_selection_datetime("1767225600") == datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_epoch_millis():
    assert parse_selection_datetime("1767225600000") == datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_invalid_raises():
    with pytest.raises(ValueError):
        parse_selection_datetime("not-a-date")


# --- criteria_from_args -----------------------------------------------------

def test_criteria_from_args_comma_splits_and_parses():
    args = SimpleNamespace(
        select_folder=["Prod,Staging"],
        select_tag=["team:infra"],
        select_datasource=[],
        select_team=["infra"],
        select_updated_after="2026-01-01",
        select_updated_before="",
        select_starred=True,
    )
    crit = criteria_from_args(args)
    assert crit.folders == ["Prod", "Staging"]
    assert crit.tags == ["team:infra"]
    assert crit.teams == ["infra"]
    assert crit.updated_after == datetime(2026, 1, 1, tzinfo=UTC)
    assert crit.updated_before is None
    assert crit.starred is True
    assert crit.is_empty is False


def test_criteria_from_args_empty_is_empty():
    args = SimpleNamespace(
        select_folder=[], select_tag=[], select_datasource=[], select_team=[],
        select_updated_after="", select_updated_before="", select_starred=False,
    )
    assert criteria_from_args(args).is_empty is True


def test_criteria_from_args_bad_date_raises():
    args = SimpleNamespace(
        select_folder=[], select_tag=[], select_datasource=[], select_team=[],
        select_updated_after="garbage", select_updated_before="", select_starred=False,
    )
    with pytest.raises(ValueError):
        criteria_from_args(args)


# --- apply_cli_selection ----------------------------------------------------

def _ds_meta(item):
    return AssetSelectionMetadata(
        folder=item.get("folder"), tags=item.get("tags", []), datasources=[],
        team=None, updated_at=None, starred=None,
    )


def test_apply_cli_selection_empty_criteria_returns_all_without_output():
    items = [{"tags": ["a"]}, {"tags": ["b"]}]
    lines = []
    kept = apply_cli_selection(items, _ds_meta, SelectionCriteria(),
                               label="dashboard", kind="dashboard(s)", printer=lines.append)
    assert kept == items
    assert lines == []  # no-op: no summary printed


def test_apply_cli_selection_filters_and_reports():
    items = [{"tags": ["keep"]}, {"tags": ["drop"]}]
    lines = []
    kept = apply_cli_selection(items, _ds_meta, SelectionCriteria(tags=["keep"]),
                               label="dashboard", kind="dashboard(s)", printer=lines.append)
    assert kept == [{"tags": ["keep"]}]
    assert any("Selected 1 of 2 dashboard(s)" in line for line in lines)


def test_apply_cli_selection_prints_degrade_warning_and_keeps():
    items = [{"folder": None, "tags": []}]
    lines = []
    kept = apply_cli_selection(items, _ds_meta, SelectionCriteria(folders=["Prod"]),
                               label="datadog dashboard", kind="dashboard(s)",
                               printer=lines.append)
    assert kept == items  # unavailable folder -> kept
    assert any("WARN" in line and "folder" in line.lower() for line in lines)
