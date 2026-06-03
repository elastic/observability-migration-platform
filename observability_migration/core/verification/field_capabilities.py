# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared target field capability model and helper predicates."""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

NUMERIC_FIELD_TYPES = frozenset(
    {
        "byte",
        "short",
        "integer",
        "long",
        "unsigned_long",
        "half_float",
        "float",
        "double",
        "scaled_float",
        "counter_long",
        "counter_double",
    }
)
TEXT_FIELD_TYPES = frozenset({"text", "match_only_text", "semantic_text"})
KEYWORD_FIELD_TYPES = frozenset({"keyword", "constant_keyword", "wildcard", "version"})
DATE_FIELD_TYPES = frozenset({"date", "date_nanos"})
BOOLEAN_FIELD_TYPES = frozenset({"boolean"})
IP_FIELD_TYPES = frozenset({"ip"})
GEO_FIELD_TYPES = frozenset({"geo_point", "geo_shape", "point", "shape"})
VECTOR_FIELD_TYPES = frozenset({"dense_vector", "sparse_vector"})


def infer_type_family(exact_type: str) -> str:
    field_type = str(exact_type or "").strip().lower()
    if not field_type:
        return "unknown"
    if field_type in NUMERIC_FIELD_TYPES:
        return "numeric"
    if field_type in TEXT_FIELD_TYPES:
        return "text"
    if field_type in KEYWORD_FIELD_TYPES:
        return "keyword"
    if field_type in DATE_FIELD_TYPES:
        return "date"
    if field_type in BOOLEAN_FIELD_TYPES:
        return "boolean"
    if field_type in IP_FIELD_TYPES:
        return "ip"
    if field_type in GEO_FIELD_TYPES:
        return "geo"
    if field_type in VECTOR_FIELD_TYPES:
        return "vector"
    return "other"


def preferred_exact_type(field_types: list[str] | tuple[str, ...] | set[str]) -> str:
    exact_types = {str(field_type or "").strip().lower() for field_type in (field_types or []) if field_type}
    if not exact_types:
        return ""
    ordered_groups = (
        ("counter_double", "counter_long"),
        tuple(sorted(NUMERIC_FIELD_TYPES - {"counter_double", "counter_long"})),
        tuple(sorted(TEXT_FIELD_TYPES)),
        tuple(sorted(KEYWORD_FIELD_TYPES)),
        tuple(sorted(DATE_FIELD_TYPES)),
        tuple(sorted(BOOLEAN_FIELD_TYPES)),
        tuple(sorted(IP_FIELD_TYPES)),
        tuple(sorted(GEO_FIELD_TYPES)),
        tuple(sorted(VECTOR_FIELD_TYPES)),
    )
    for group in ordered_groups:
        for field_type in group:
            if field_type in exact_types:
                return field_type
    return sorted(exact_types)[0]


def _unique_preserve_order(values):
    seen = set()
    ordered = []
    for value in values or []:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _infer_time_series_metric_kind(field_caps_entry: dict[str, dict]) -> str:
    kinds = set()
    for field_type, metadata in (field_caps_entry or {}).items():
        exact_type = str(field_type or "").strip().lower()
        if exact_type in {"counter_double", "counter_long"}:
            kinds.add("counter")
        if isinstance(metadata, dict):
            time_series_metric = str(metadata.get("time_series_metric", "") or "").strip().lower()
            if time_series_metric:
                kinds.add(time_series_metric)
    if len(kinds) == 1:
        return next(iter(kinds))
    return ""


@dataclass
class FieldCapability:
    name: str
    type: str = ""
    searchable: bool = True
    aggregatable: bool = True
    indices: list[str] = field(default_factory=list)
    conflicting_types: list[str] = field(default_factory=list)
    type_family: str = ""
    time_series_metric_kind: str = ""

    def __post_init__(self):
        self.type = str(self.type or "").strip().lower()
        self.indices = _unique_preserve_order(self.indices)
        normalized_conflicts = [
            str(field_type or "").strip().lower()
            for field_type in (self.conflicting_types or [])
            if field_type
        ]
        self.conflicting_types = sorted(set(normalized_conflicts))
        if not self.type and self.conflicting_types:
            self.type = preferred_exact_type(self.conflicting_types)
        self.type_family = str(self.type_family or "").strip().lower() or infer_type_family(self.type)
        self.time_series_metric_kind = str(self.time_series_metric_kind or "").strip().lower()


@dataclass
class FieldUsageAssessment:
    field_name: str
    display_name: str
    usage: str
    capability: FieldCapability | None = None
    required_type_family: str = ""
    warnings: list[str] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return self.capability is not None

    @property
    def type_family(self) -> str:
        return str((self.capability.type_family if self.capability else "") or "")


def field_capability_from_es_field_caps(field_name: str, field_caps_entry: dict[str, dict]) -> FieldCapability:
    entries = {
        str(exact_type or "").strip().lower(): metadata
        for exact_type, metadata in (field_caps_entry or {}).items()
    }
    exact_types = [field_type for field_type in entries if field_type]
    primary_type = preferred_exact_type(exact_types)
    metadata_entries = [metadata for metadata in entries.values() if isinstance(metadata, dict)]
    searchable = all(metadata.get("searchable", True) is not False for metadata in metadata_entries)
    aggregatable = all(metadata.get("aggregatable", True) is not False for metadata in metadata_entries)
    indices = []
    for metadata in metadata_entries:
        indices.extend(metadata.get("indices", []) or [])
    conflicts = sorted(set(exact_types)) if len(set(exact_types)) > 1 else []
    return FieldCapability(
        name=field_name,
        type=primary_type,
        searchable=searchable,
        aggregatable=aggregatable,
        indices=_unique_preserve_order(indices),
        conflicting_types=conflicts,
        time_series_metric_kind=_infer_time_series_metric_kind(entries),
    )


def _build_es_headers(es_api_key: str = "") -> dict[str, str]:
    headers = {}
    if es_api_key:
        headers["Authorization"] = f"ApiKey {es_api_key}"
    return headers


def fetch_field_capabilities(
    es_url: str,
    index_pattern: str,
    es_api_key: str = "",
    timeout: int = 10,
    verify: bool | str = True,
) -> dict[str, FieldCapability]:
    """Fetch and normalize Elasticsearch _field_caps for an index pattern."""
    if not es_url or not index_pattern:
        return {}
    base_url = str(es_url).rstrip("/")
    response = requests.get(
        f"{base_url}/{index_pattern}/_field_caps",
        params={"fields": "*"},
        headers=_build_es_headers(es_api_key),
        timeout=timeout,
        verify=verify,
    )
    response.raise_for_status()
    fields = response.json().get("fields", {}) or {}
    capabilities = {}
    for field_name, field_caps_entry in fields.items():
        capabilities[field_name] = field_capability_from_es_field_caps(field_name, field_caps_entry)
    return capabilities


def is_numeric_field(capability: FieldCapability | None) -> bool:
    return bool(capability and capability.type_family == "numeric")


def is_text_like_field(capability: FieldCapability | None) -> bool:
    return bool(capability and capability.type_family == "text")


def is_keyword_like_field(capability: FieldCapability | None) -> bool:
    return bool(capability and capability.type_family == "keyword")


def is_string_field(capability: FieldCapability | None) -> bool:
    return is_text_like_field(capability) or is_keyword_like_field(capability)


def is_date_like_field(capability: FieldCapability | None) -> bool:
    return bool(capability and capability.type_family == "date")


def is_searchable_field(capability: FieldCapability | None) -> bool:
    return bool(capability and capability.searchable)


def is_aggregatable_field(capability: FieldCapability | None) -> bool:
    return bool(capability and capability.aggregatable)


def has_conflicting_types(capability: FieldCapability | None) -> bool:
    return bool(capability and capability.conflicting_types)


def is_counter_metric_field(capability: FieldCapability | None) -> bool:
    if not capability:
        return False
    return capability.time_series_metric_kind == "counter" or capability.type in {
        "counter_double",
        "counter_long",
    }


def assess_field_usage(
    capability: FieldCapability | None,
    *,
    field_name: str,
    usage: str,
    required_type_family: str = "",
    display_name: str = "",
) -> FieldUsageAssessment:
    display = str(display_name or field_name or "field")
    assessment = FieldUsageAssessment(
        field_name=field_name,
        display_name=display,
        usage=str(usage or "filter"),
        capability=capability,
        required_type_family=str(required_type_family or "").strip().lower(),
    )

    if capability is None:
        assessment.warnings.append(f"field '{display}' not found in target mapping")
        return assessment

    if has_conflicting_types(capability):
        assessment.warnings.append(
            f"field '{display}' has conflicting types across indices: {capability.conflicting_types}"
        )

    if assessment.usage in {"group_by", "aggregate"} and not is_aggregatable_field(capability):
        assessment.blocking_reasons.append(
            f"field '{display}' is not aggregatable but used for {assessment.usage}"
        )

    if assessment.usage == "filter" and not is_searchable_field(capability):
        assessment.warnings.append(f"field '{display}' is not searchable but used as filter")

    if assessment.required_type_family:
        if capability.type_family in {"", "unknown"}:
            assessment.warnings.append(
                f"field '{display}' is used as {assessment.usage} with required type family "
                f"'{assessment.required_type_family}', but the target type is unknown"
            )
        elif capability.type_family != assessment.required_type_family:
            assessment.blocking_reasons.append(
                f"field '{display}' is typed as '{capability.type}' ({capability.type_family}) "
                f"but {assessment.usage} requires '{assessment.required_type_family}'"
            )
    return assessment


__all__ = [
    "DATE_FIELD_TYPES",
    "KEYWORD_FIELD_TYPES",
    "NUMERIC_FIELD_TYPES",
    "TEXT_FIELD_TYPES",
    "FieldCapability",
    "FieldUsageAssessment",
    "assess_field_usage",
    "fetch_field_capabilities",
    "field_capability_from_es_field_caps",
    "has_conflicting_types",
    "infer_type_family",
    "is_aggregatable_field",
    "is_counter_metric_field",
    "is_date_like_field",
    "is_keyword_like_field",
    "is_numeric_field",
    "is_searchable_field",
    "is_string_field",
    "is_text_like_field",
    "preferred_exact_type",
]
