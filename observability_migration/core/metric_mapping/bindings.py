# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Metric map binding layer for emitter planning."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .entries import CLASS_EXACT, MetricMapEntry, MetricMapResult

_PROMQL_SAFE = re.compile(r"^[a-zA-Z_.:][a-zA-Z0-9_.:]*$")


@dataclass(frozen=True)
class MetricBinding:
    """Resolved metric mapping with emitter-oriented fields."""

    source: str
    target_field: str
    applied: bool
    klass: str
    target_filters: dict[str, str]
    source_filter: dict[str, str]
    target_index: str
    transform: str
    unit_scale: float | None
    gap_reason: str = ""
    warnings: tuple[str, ...] = ()
    entry: MetricMapEntry | None = None
    native_promql_compatible: bool = False


def _looks_promql_safe(name: str) -> bool:
    return bool(_PROMQL_SAFE.match(str(name or "").strip()))


def binding_from_result(result: MetricMapResult) -> MetricBinding:
    """Project a :class:`MetricMapResult` into emitter planning shape."""
    entry = result.entry
    target_filters = dict(entry.attribute_filter) if entry else {}
    source_filter = dict(entry.source_filter) if entry else {}
    target_index = entry.target_index if entry else ""
    transform = entry.transform if entry else "none"
    unit_scale = entry.unit_scale if entry else result.unit_scale
    native_promql_compatible = (
        result.klass == CLASS_EXACT
        and result.applied
        and not target_filters
        and not source_filter
        and transform == "none"
        and (unit_scale is None or unit_scale == 1.0)
        and _looks_promql_safe(result.target)
    )
    return MetricBinding(
        source=result.source,
        target_field=result.target,
        applied=result.applied,
        klass=result.klass,
        target_filters=target_filters,
        source_filter=source_filter,
        target_index=target_index,
        transform=transform,
        unit_scale=unit_scale,
        gap_reason=result.gap_reason,
        warnings=result.warnings,
        entry=entry,
        native_promql_compatible=native_promql_compatible,
    )


def plan_rate_transform(
    *,
    source_has_rate: bool,
    transform: str,
    target_is_counter: bool | None,
) -> tuple[str, str]:
    """Return ``(action, gap_reason)``.

    ``action`` is one of ``keep_source_rate``, ``drop_rate``, ``to_rate``,
    ``none``, or ``gap``.
    """
    normalized = str(transform or "none").strip().lower() or "none"
    if normalized == "none":
        if source_has_rate:
            return "keep_source_rate", ""
        return "none", ""
    if normalized == "drop_rate":
        if source_has_rate:
            if target_is_counter is False:
                return "drop_rate", ""
            if target_is_counter is True:
                return "gap", "drop_rate incompatible with counter target"
            return "gap", "drop_rate requires known target counter/gauge kind"
        return "none", ""
    if normalized == "to_rate":
        if source_has_rate:
            return "keep_source_rate", ""
        if target_is_counter is True:
            return "to_rate", ""
        if target_is_counter is False:
            return "gap", "to_rate incompatible with gauge target"
        return "gap", "to_rate requires known target counter/gauge kind"
    return "gap", f"unsupported transform={normalized!r}"
