"""Pydantic validation for Datadog extension inputs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class FieldMapProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "custom"
    metric_index: str = "metrics-*"
    logs_index: str = "logs-*"
    log_index_map: dict[str, str] = Field(default_factory=dict)
    timestamp_field: str = "@timestamp"
    metrics_dataset_filter: str = ""
    logs_dataset_filter: str = ""
    metric_map: dict[str, str] = Field(default_factory=dict)
    tag_map: dict[str, str] = Field(default_factory=dict)
    metric_prefix: str = ""
    metric_suffix: str = ""
    tag_prefix: str = ""

    @field_validator("metric_map", "tag_map", "log_index_map")
    @classmethod
    def validate_string_mapping(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, mapped_value in value.items():
            if not str(key).strip():
                raise ValueError("mapping keys must be non-empty")
            if not str(mapped_value).strip():
                raise ValueError("mapping values must be non-empty")
            normalized[str(key)] = str(mapped_value)
        return normalized


def validate_field_profile_payload(
    raw: dict[str, Any] | None,
    *,
    source: str = "field profile",
) -> FieldMapProfileModel:
    try:
        return FieldMapProfileModel.model_validate(raw or {})
    except ValidationError as exc:  # pragma: no cover - exercised via loaders/tests
        raise ValueError(f"Invalid Datadog field profile in {source}: {exc}") from exc


__all__ = [
    "FieldMapProfileModel",
    "validate_field_profile_payload",
]
