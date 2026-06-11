# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Source-agnostic metadata selection for `obs-migrate migrate`.

This module is the pure core behind the ``--select-*`` flags. It knows nothing
about Grafana, Datadog, HTTP, or argparse: callers map a raw asset into an
``AssetSelectionMetadata`` view and ask whether it ``matches`` a
``SelectionCriteria``.

Match semantics:
- Values within a single dimension are OR'd; dimensions are AND'd together.
- folder / team / tag / datasource compare case-insensitively (exact, not prefix).
- ``updated_after`` / ``updated_before`` are inclusive bounds (tz-aware UTC).

Degrade gracefully (never silently hide gaps): when a criteria dimension is set
but the asset cannot supply it (the corresponding ``AssetSelectionMetadata``
field is ``None``), the asset is *kept* and a warning is emitted naming the
dimension and the asset label. A supplied-but-empty value (``""`` / ``[]`` /
``False``) is a genuine non-match, not a degradation.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

T = TypeVar("T")

_MILLIS_THRESHOLD = 10**12

# (flag, dest) pairs for the repeatable/comma-separated list selectors.
_LIST_SELECTORS = (
    ("--select-folder", "select_folder"),
    ("--select-tag", "select_tag"),
    ("--select-datasource", "select_datasource"),
    ("--select-team", "select_team"),
)
_DATE_SELECTORS = (
    ("--select-updated-after", "select_updated_after"),
    ("--select-updated-before", "select_updated_before"),
)


@dataclass
class AssetSelectionMetadata:
    """Normalized, source-agnostic metadata used for selection.

    A field set to ``None`` means the source/asset cannot supply that
    dimension (triggers keep-and-warn). An empty list / empty string / ``False``
    means the dimension is supplied but empty/false.
    """

    folder: str | None = None
    tags: list[str] | None = field(default_factory=list)
    datasources: list[str] | None = field(default_factory=list)
    team: str | None = None
    updated_at: datetime | None = None
    starred: bool | None = None


@dataclass
class SelectionCriteria:
    """Parsed ``--select-*`` criteria."""

    folders: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    datasources: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)
    updated_after: datetime | None = None
    updated_before: datetime | None = None
    starred: bool | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.folders
            or self.tags
            or self.datasources
            or self.teams
            or self.updated_after is not None
            or self.updated_before is not None
            or self.starred is not None
        )


def _ci(values: Iterable[str]) -> set[str]:
    return {v.casefold() for v in values}


def matches(
    meta: AssetSelectionMetadata,
    criteria: SelectionCriteria,
    *,
    label: str = "asset",
) -> tuple[bool, list[str]]:
    """Return ``(matched, warnings)`` for one asset against the criteria.

    ``warnings`` names every requested dimension the asset could not supply;
    those dimensions do not narrow the selection (the asset is kept).
    """
    if criteria.is_empty:
        return True, []

    matched = True
    warnings: list[str] = []

    def _unavailable(dimension: str) -> None:
        warnings.append(
            f"{dimension} selection requested but unavailable for {label}; "
            f"kept without {dimension} filtering"
        )

    # Scalar string dimensions (folder, team).
    for dim, value, wanted in (
        ("folder", meta.folder, criteria.folders),
        ("team", meta.team, criteria.teams),
    ):
        if not wanted:
            continue
        if value is None:
            _unavailable(dim)
        elif value.casefold() not in _ci(wanted):
            matched = False

    # List membership dimensions (tags, datasources).
    for dim, values, wanted in (
        ("tag", meta.tags, criteria.tags),
        ("datasource", meta.datasources, criteria.datasources),
    ):
        if not wanted:
            continue
        if values is None:
            _unavailable(dim)
        elif not (_ci(values) & _ci(wanted)):
            matched = False

    # Updated window.
    if criteria.updated_after is not None or criteria.updated_before is not None:
        if meta.updated_at is None:
            _unavailable("updated")
        else:
            if criteria.updated_after is not None and meta.updated_at < criteria.updated_after:
                matched = False
            if criteria.updated_before is not None and meta.updated_at > criteria.updated_before:
                matched = False

    # Starred / popularity.
    if criteria.starred is not None:
        if meta.starred is None:
            _unavailable("starred")
        elif meta.starred != criteria.starred:
            matched = False

    return matched, warnings


def filter_assets(
    items: Iterable[T],
    get_meta: Callable[[T], AssetSelectionMetadata],
    criteria: SelectionCriteria,
    *,
    label: str = "asset",
) -> tuple[list[T], list[str]]:
    """Filter ``items`` by ``criteria``, returning ``(kept, deduped_warnings)``."""
    kept: list[T] = []
    seen_warnings: dict[str, None] = {}
    for item in items:
        ok, warnings = matches(get_meta(item), criteria, label=label)
        for w in warnings:
            seen_warnings.setdefault(w, None)
        if ok:
            kept.append(item)
    return kept, list(seen_warnings)


def parse_selection_datetime(value: str) -> datetime:
    """Parse an ISO-8601 string or epoch (seconds/millis) into tz-aware UTC.

    Raises ``ValueError`` on anything unrecognized.
    """
    text = value.strip()
    if not text:
        raise ValueError("empty datetime value")

    if text.lstrip("-").isdigit():
        epoch = int(text)
        if abs(epoch) >= _MILLIS_THRESHOLD:
            epoch = epoch // 1000
        return datetime.fromtimestamp(epoch, tz=UTC)

    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _split_csv(values: list[str]) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        out.extend(part.strip() for part in raw.split(",") if part.strip())
    return out


def criteria_from_args(args: Any) -> SelectionCriteria:
    """Build ``SelectionCriteria`` from parsed CLI args.

    Expects ``select_folder/select_tag/select_datasource/select_team`` as lists,
    ``select_updated_after/select_updated_before`` as strings, and
    ``select_starred`` as a bool. Raises ``ValueError`` on an unparseable date.
    """
    after = getattr(args, "select_updated_after", "") or ""
    before = getattr(args, "select_updated_before", "") or ""
    return SelectionCriteria(
        folders=_split_csv(getattr(args, "select_folder", []) or []),
        tags=_split_csv(getattr(args, "select_tag", []) or []),
        datasources=_split_csv(getattr(args, "select_datasource", []) or []),
        teams=_split_csv(getattr(args, "select_team", []) or []),
        updated_after=parse_selection_datetime(after) if after else None,
        updated_before=parse_selection_datetime(before) if before else None,
        starred=True if getattr(args, "select_starred", False) else None,
    )


def apply_cli_selection(
    items: list[T],
    get_meta: Callable[[T], AssetSelectionMetadata],
    criteria: SelectionCriteria,
    *,
    label: str,
    kind: str,
    printer: Callable[[str], None] = print,
) -> list[T]:
    """Filter ``items`` for a CLI run, emitting degrade warnings and a summary.

    Returns the kept items. When ``criteria`` is empty this is a no-op (returns
    all items, prints nothing). Warnings (degraded dimensions) are printed once
    each, followed by a ``Selected N of M`` summary line. Callers decide what to
    do with an empty result.
    """
    if criteria.is_empty:
        return list(items)
    kept, warnings = filter_assets(items, get_meta, criteria, label=label)
    for warning in warnings:
        printer(f"  WARN: {warning}")
    printer(f"  Selected {len(kept)} of {len(items)} {kind} matching selection criteria")
    return kept


def add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the uniform ``--select-*`` metadata flags on a parser.

    Shared by the unified ``obs-migrate migrate`` parser and the per-source CLIs
    so the flag surface is defined in exactly one place.
    """
    group = parser.add_argument_group(
        "metadata selection",
        "Scope the migration by asset metadata (client-side filter). Repeatable "
        "or comma-separated; values OR within a flag, AND across flags. Dimensions "
        "a source/asset cannot supply are skipped with a warning (asset kept).",
    )
    group.add_argument("--select-folder", action="append", default=[], metavar="NAME",
                       help="Only migrate assets in these folders (Grafana folders).")
    group.add_argument("--select-tag", action="append", default=[], metavar="TAG",
                       help="Only migrate assets carrying these tags.")
    group.add_argument("--select-datasource", action="append", default=[], metavar="TYPE",
                       help="Only migrate assets using these datasource types.")
    group.add_argument("--select-team", action="append", default=[], metavar="TEAM",
                       help="Only migrate assets owned by these teams (from a team: tag).")
    group.add_argument("--select-updated-after", default="", metavar="WHEN",
                       help="Only migrate assets updated at/after this ISO date or epoch.")
    group.add_argument("--select-updated-before", default="", metavar="WHEN",
                       help="Only migrate assets updated at/before this ISO date or epoch.")
    group.add_argument("--select-starred", action="store_true",
                       help="Only migrate starred/popular assets (Grafana dashboards).")


def selection_args_to_argv(args: Any) -> list[str]:
    """Render parsed ``--select-*`` args back into argv for the source-CLI bridge."""
    argv: list[str] = []
    for flag, dest in _LIST_SELECTORS:
        for value in getattr(args, dest, []) or []:
            argv.extend([flag, value])
    for flag, dest in _DATE_SELECTORS:
        value = getattr(args, dest, "") or ""
        if value:
            argv.extend([flag, value])
    if getattr(args, "select_starred", False):
        argv.append("--select-starred")
    return argv
