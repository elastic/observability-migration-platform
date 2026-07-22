# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Typed metric_map entries, classification, and resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

CLASS_EXACT = "exact"
CLASS_REQUIRES_TRANSFORM = "requires_transform"
CLASS_NONE = "none"

VALID_TRANSFORMS = frozenset({"none", "drop_rate", "to_rate"})


@dataclass(frozen=True)
class MetricMapEntry:
    """One source-metric → target-field mapping."""

    target: str
    transform: str = "none"
    attribute_filter: dict[str, str] = field(default_factory=dict)
    unit_scale: float | None = None
    provenance: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", str(self.target or "").strip())
        object.__setattr__(self, "transform", str(self.transform or "none").strip().lower() or "none")
        filters = {
            str(key).strip(): str(value)
            for key, value in dict(self.attribute_filter or {}).items()
            if str(key).strip()
        }
        object.__setattr__(self, "attribute_filter", filters)
        object.__setattr__(self, "provenance", str(self.provenance or "").strip())
        if self.unit_scale is not None:
            object.__setattr__(self, "unit_scale", float(self.unit_scale))
        if not self.target:
            raise ValueError("metric_map entry target must be non-empty")
        if self.transform not in VALID_TRANSFORMS:
            raise ValueError(
                f"metric_map transform must be one of {sorted(VALID_TRANSFORMS)}, "
                f"got {self.transform!r}"
            )


@dataclass(frozen=True)
class MetricMapResult:
    """Outcome of resolving a source metric through metric_map."""

    source: str
    target: str
    applied: bool
    klass: str
    gap_reason: str = ""
    warnings: tuple[str, ...] = ()
    unit_scale: float | None = None
    mapped_from: str = ""
    entry: MetricMapEntry | None = None

    @property
    def is_gap(self) -> bool:
        return bool(self.gap_reason) or (self.klass == CLASS_REQUIRES_TRANSFORM and not self.applied)


def classify_metric_map_entry(entry: MetricMapEntry) -> str:
    """Classify an entry for correctness (v1: exact vs requires_transform)."""
    if (
        entry.transform != "none"
        or entry.attribute_filter
        or (entry.unit_scale is not None and entry.unit_scale != 1.0)
    ):
        return CLASS_REQUIRES_TRANSFORM
    return CLASS_EXACT


def parse_metric_map_entry(raw: Any, *, source_key: str = "") -> MetricMapEntry:
    """Normalize a YAML/JSON metric_map value into MetricMapEntry."""
    if isinstance(raw, MetricMapEntry):
        return raw
    if isinstance(raw, str):
        return MetricMapEntry(target=raw)
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"metric_map[{source_key!r}] must be a string or mapping, got {type(raw).__name__}"
        )
    payload = dict(raw)
    unknown = set(payload) - {"target", "transform", "attribute_filter", "unit_scale", "provenance"}
    if unknown:
        raise ValueError(
            f"metric_map[{source_key!r}] has unknown keys: {sorted(unknown)}"
        )
    target = payload.get("target", "")
    attribute_filter = payload.get("attribute_filter") or {}
    if not isinstance(attribute_filter, Mapping):
        raise ValueError(f"metric_map[{source_key!r}].attribute_filter must be a mapping")
    return MetricMapEntry(
        target=str(target),
        transform=str(payload.get("transform", "none") or "none"),
        attribute_filter={str(k): str(v) for k, v in attribute_filter.items()},
        unit_scale=payload.get("unit_scale"),
        provenance=str(payload.get("provenance", "") or ""),
    )


def normalize_metric_map(raw: Mapping[str, Any] | None) -> dict[str, MetricMapEntry]:
    """Parse a full metric_map mapping."""
    normalized: dict[str, MetricMapEntry] = {}
    for key, value in dict(raw or {}).items():
        source = str(key).strip()
        if not source:
            raise ValueError("metric_map keys must be non-empty source metric names")
        normalized[source] = parse_metric_map_entry(value, source_key=source)
    return normalized


def resolve_metric_map(
    source_metric: str,
    metric_map: Mapping[str, MetricMapEntry] | None,
) -> MetricMapResult | None:
    """Resolve ``source_metric`` through ``metric_map``.

    Returns ``None`` when the source is unmapped.
    For class-1 (exact, optional unit_scale): ``applied=True`` with target.
    For class-2 (transform / attribute_filter): ``applied=False`` with an
    explicit gap reason — never a silent bare rename (v1).
    """
    source = str(source_metric or "").strip()
    if not source or not metric_map or source not in metric_map:
        return None
    entry = metric_map[source]
    klass = classify_metric_map_entry(entry)
    if klass == CLASS_REQUIRES_TRANSFORM:
        reasons = []
        if entry.transform != "none":
            reasons.append(
                f"transform={entry.transform!r} is not applied in v1 "
                "(requires semantic transform support)"
            )
        if entry.attribute_filter:
            reasons.append(
                "attribute_filter is not applied in v1 "
                "(requires WHERE injection support)"
            )
        if entry.unit_scale is not None and entry.unit_scale != 1.0:
            reasons.append(
                f"unit_scale={entry.unit_scale!r} is not applied in v1 "
                "(requires expression scaling support)"
            )
        gap = (
            f"metric_map[{source!r} → {entry.target!r}] requires transform; "
            + "; ".join(reasons)
        )
        return MetricMapResult(
            source=source,
            target=source,
            applied=False,
            klass=klass,
            gap_reason=gap,
            warnings=(gap,),
            unit_scale=entry.unit_scale,
            mapped_from=source,
            entry=entry,
        )
    return MetricMapResult(
        source=source,
        target=entry.target,
        applied=True,
        klass=CLASS_EXACT,
        warnings=(),
        unit_scale=entry.unit_scale,
        mapped_from=source,
        entry=entry,
    )
