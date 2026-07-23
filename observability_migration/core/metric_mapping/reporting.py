# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Metric map summary artifacts for contracts and migration reports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def build_metric_map_summary(
    *,
    applied: dict[str, str],
    gaps: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    """Build a JSON-serializable metric_map summary."""
    return {
        "applied": [
            {"source": source, "target": target}
            for source, target in sorted(applied.items())
        ],
        "gaps": list(gaps),
        "warnings": list(warnings),
        "totals": {
            "applied": len(applied),
            "gaps": len(gaps),
            "warnings": len(warnings),
        },
    }


def _coerce_mapping(value: Any) -> dict[str, str]:
    """Return a plain str->str dict, ignoring non-mapping inputs (e.g. Mocks)."""
    if isinstance(value, Mapping):
        return {str(key): str(val) for key, val in value.items()}
    return {}


def _coerce_sequence(value: Any) -> list[str]:
    """Return a list of strings, ignoring non-sequence inputs (e.g. Mocks)."""
    if isinstance(value, str) or not isinstance(value, Sequence):
        return []
    return [str(item) for item in value]


def metric_map_summary_from_tracker(tracker: Any) -> dict[str, Any] | None:
    """Extract metric_map summary from a resolver or field-map profile."""
    if tracker is None:
        return None

    applied_fn = getattr(tracker, "metric_map_applied", None)
    gaps_fn = getattr(tracker, "metric_map_gaps", None)
    warnings_fn = getattr(tracker, "metric_map_warnings", None)
    if not any(callable(item) for item in (applied_fn, gaps_fn, warnings_fn)):
        return None

    applied = _coerce_mapping(applied_fn() if callable(applied_fn) else None)
    gaps = _coerce_sequence(gaps_fn() if callable(gaps_fn) else None)
    warnings = _coerce_sequence(warnings_fn() if callable(warnings_fn) else None)

    metric_map = getattr(tracker, "metric_map", None)
    if metric_map is None:
        rule_pack = getattr(tracker, "_rule_pack", None)
        if rule_pack is not None:
            metric_map = getattr(rule_pack, "metric_map", None)

    if not applied and not gaps and not warnings and not metric_map:
        return None

    return build_metric_map_summary(applied=applied, gaps=gaps, warnings=warnings)


def attach_metric_map_to_contract(contract: dict[str, Any], tracker: Any) -> dict[str, Any]:
    """Add top-level ``metric_map`` when the tracker has map data."""
    summary = metric_map_summary_from_tracker(tracker)
    if summary is not None:
        contract["metric_map"] = summary
    return contract
