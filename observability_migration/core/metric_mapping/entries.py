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
SCAFFOLD_PROVENANCE = "scaffold"

_CLASS2_APPLY_WARNING = (
    "class-2 mapping applied; emitter must honor "
    "attribute_filter/transform/unit_scale"
)

_ENTRY_KEYS = frozenset(
    {
        "target",
        "transform",
        "attribute_filter",
        "unit_scale",
        "provenance",
        "source_filter",
        "target_index",
        "variants",
    }
)


@dataclass(frozen=True)
class MetricMapEntry:
    """One source-metric → target-field mapping."""

    target: str
    transform: str = "none"
    attribute_filter: dict[str, str] = field(default_factory=dict)
    unit_scale: float | None = None
    provenance: str = ""
    source_filter: dict[str, str] = field(default_factory=dict)
    target_index: str = ""
    variants: tuple[MetricMapEntry, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", str(self.target or "").strip())
        object.__setattr__(self, "transform", str(self.transform or "none").strip().lower() or "none")
        filters = {
            str(key).strip(): str(value)
            for key, value in dict(self.attribute_filter or {}).items()
            if str(key).strip()
        }
        object.__setattr__(self, "attribute_filter", filters)
        source_filters = {
            str(key).strip(): str(value)
            for key, value in dict(self.source_filter or {}).items()
            if str(key).strip()
        }
        object.__setattr__(self, "source_filter", source_filters)
        object.__setattr__(self, "provenance", str(self.provenance or "").strip())
        object.__setattr__(self, "target_index", str(self.target_index or "").strip())
        if self.unit_scale is not None:
            object.__setattr__(self, "unit_scale", float(self.unit_scale))
        if not self.target and not self.variants:
            if self.provenance != SCAFFOLD_PROVENANCE:
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


def _parse_variants(raw_variants: Any, *, source_key: str) -> tuple[MetricMapEntry, ...]:
    if raw_variants is None:
        return ()
    if not isinstance(raw_variants, list):
        raise ValueError(f"metric_map[{source_key!r}].variants must be a list")
    parsed: list[MetricMapEntry] = []
    for index, item in enumerate(raw_variants):
        variant = parse_metric_map_entry(item, source_key=f"{source_key}.variants[{index}]")
        if not variant.target:
            raise ValueError(
                f"metric_map[{source_key!r}].variants[{index}] target must be non-empty"
            )
        parsed.append(variant)
    return tuple(parsed)


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
    unknown = set(payload) - _ENTRY_KEYS
    if unknown:
        raise ValueError(
            f"metric_map[{source_key!r}] has unknown keys: {sorted(unknown)}"
        )
    target = payload.get("target", "")
    attribute_filter = payload.get("attribute_filter") or {}
    if not isinstance(attribute_filter, Mapping):
        raise ValueError(f"metric_map[{source_key!r}].attribute_filter must be a mapping")
    source_filter = payload.get("source_filter") or {}
    if not isinstance(source_filter, Mapping):
        raise ValueError(f"metric_map[{source_key!r}].source_filter must be a mapping")
    variants = _parse_variants(payload.get("variants"), source_key=source_key)
    provenance = str(payload.get("provenance", "") or "")
    if not str(target).strip() and not variants and provenance != SCAFFOLD_PROVENANCE:
        raise ValueError(f"metric_map[{source_key!r}] target must be non-empty")
    return MetricMapEntry(
        target=str(target),
        transform=str(payload.get("transform", "none") or "none"),
        attribute_filter={str(k): str(v) for k, v in attribute_filter.items()},
        unit_scale=payload.get("unit_scale"),
        provenance=provenance,
        source_filter={str(k): str(v) for k, v in source_filter.items()},
        target_index=str(payload.get("target_index", "") or ""),
        variants=variants,
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


def _select_variant(
    entry: MetricMapEntry,
    source_labels: Mapping[str, str] | None,
) -> MetricMapEntry | None:
    if not entry.variants:
        return entry
    labels = dict(source_labels or {})
    for variant in entry.variants:
        if all(labels.get(key) == value for key, value in variant.source_filter.items()):
            return variant
    return None


def _class2_warnings(entry: MetricMapEntry) -> tuple[str, ...]:
    notes: list[str] = [_CLASS2_APPLY_WARNING]
    if entry.transform != "none":
        notes.append(f"transform={entry.transform!r} must be honored by emitter")
    if entry.attribute_filter:
        notes.append("attribute_filter must be honored by emitter")
    if entry.unit_scale is not None and entry.unit_scale != 1.0:
        notes.append(f"unit_scale={entry.unit_scale!r} must be honored by emitter")
    return tuple(notes)


def resolve_metric_map(
    source_metric: str,
    metric_map: Mapping[str, MetricMapEntry] | None,
    source_labels: Mapping[str, str] | None = None,
) -> MetricMapResult | None:
    """Resolve ``source_metric`` through ``metric_map``.

    Returns ``None`` when the source is unmapped.
    For class-1 (exact, optional unit_scale): ``applied=True`` with target.
    For class-2 (transform / attribute_filter / unit_scale): ``applied=True``
    with target when the emitter can honor the semantics; ``klass`` remains
    ``requires_transform`` and warnings note emitter obligations.
    """
    source = str(source_metric or "").strip()
    if not source or not metric_map or source not in metric_map:
        return None
    root_entry = metric_map[source]
    entry = _select_variant(root_entry, source_labels)
    if entry is None:
        gap = (
            f"metric_map[{source!r}] has variants but none matched "
            f"source_labels={dict(source_labels or {})!r}"
        )
        return MetricMapResult(
            source=source,
            target=source,
            applied=False,
            klass=CLASS_REQUIRES_TRANSFORM,
            gap_reason=gap,
            warnings=(gap,),
            mapped_from=source,
            entry=root_entry,
        )
    if not entry.target:
        gap = (
            f"metric_map[{source!r}] is a scaffold placeholder; "
            "fill target before use"
        )
        return MetricMapResult(
            source=source,
            target=source,
            applied=False,
            klass=CLASS_NONE,
            gap_reason=gap,
            warnings=(gap,),
            mapped_from=source,
            entry=entry,
        )
    klass = classify_metric_map_entry(entry)
    if klass == CLASS_REQUIRES_TRANSFORM:
        return MetricMapResult(
            source=source,
            target=entry.target,
            applied=True,
            klass=klass,
            gap_reason="",
            warnings=_class2_warnings(entry),
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
