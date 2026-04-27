"""Pydantic validation for Grafana rule-pack extensions."""

from __future__ import annotations

import re
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

QUERY_OVERRIDE_FIELDS = {
    "default_rate_window",
    "default_gauge_agg",
    "ts_time_filter",
    "from_time_filter",
    "ts_bucket",
    "from_bucket",
    "logs_index",
    "metrics_dataset_filter",
    "logs_dataset_filter",
    "logs_message_field",
    "logs_timestamp_field",
    "logs_limit",
}


def _normalize_mapping_lists(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    normalized: dict[str, list[str]] = {}
    for key, item in value.items():
        if isinstance(item, list):
            normalized[key] = item
        else:
            normalized[key] = [item]
    return normalized


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatternRuleModel(_StrictModel):
    pattern: str
    reason: str

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regex pattern: {exc}") from exc
        return value


class IndexRewriteRuleModel(_StrictModel):
    match: str
    replace: str


class QueryConfigModel(_StrictModel):
    not_feasible_patterns: list[PatternRuleModel] = Field(default_factory=list)
    warning_patterns: list[PatternRuleModel] = Field(default_factory=list)
    counter_suffixes: list[str] = Field(default_factory=list)
    default_rate_window: str | None = None
    default_gauge_agg: str | None = None
    ts_time_filter: str | None = None
    from_time_filter: str | None = None
    ts_bucket: str | None = None
    from_bucket: str | None = None
    logs_index: str | None = None
    metrics_dataset_filter: str | None = None
    logs_dataset_filter: str | None = None
    logs_message_field: str | None = None
    logs_timestamp_field: str | None = None
    logs_limit: int | None = None
    label_rewrites: dict[str, str] = Field(default_factory=dict)
    label_candidates: dict[str, list[str]] = Field(default_factory=dict)
    ignored_labels: list[str] = Field(default_factory=list)
    index_rewrites: list[IndexRewriteRuleModel] = Field(default_factory=list)

    @field_validator("label_candidates", mode="before")
    @classmethod
    def normalize_label_candidates(cls, value: Any) -> Any:
        return _normalize_mapping_lists(value)


class PanelConfigModel(_StrictModel):
    type_map: dict[str, str] = Field(default_factory=dict)
    skip_types: list[str] = Field(default_factory=list)


class ControlsConfigModel(_StrictModel):
    field_overrides: dict[str, str] = Field(default_factory=dict)


class SchemaConfigModel(_StrictModel):
    label_candidates: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("label_candidates", mode="before")
    @classmethod
    def normalize_label_candidates(cls, value: Any) -> Any:
        return _normalize_mapping_lists(value)


class DashboardConfigModel(_StrictModel):
    default_rate_window: str | None = None
    default_gauge_agg: str | None = None
    ts_time_filter: str | None = None
    from_time_filter: str | None = None
    ts_bucket: str | None = None
    from_bucket: str | None = None
    logs_index: str | None = None
    metrics_dataset_filter: str | None = None
    logs_dataset_filter: str | None = None
    logs_message_field: str | None = None
    logs_timestamp_field: str | None = None
    logs_limit: int | None = None


class GrafanaRulePackModel(_StrictModel):
    query: QueryConfigModel = Field(default_factory=QueryConfigModel)
    panel: PanelConfigModel = Field(
        default_factory=PanelConfigModel,
        validation_alias=AliasChoices("panel", "panels"),
    )
    controls: ControlsConfigModel = Field(default_factory=ControlsConfigModel)
    schema_config: SchemaConfigModel = Field(default_factory=SchemaConfigModel, validation_alias="schema")
    dashboard: DashboardConfigModel = Field(default_factory=DashboardConfigModel)
    generated_metadata: dict[str, Any] | None = Field(default=None, validation_alias="_generated")
    validation_hints: dict[str, Any] | None = Field(default=None, validation_alias="_validation_hints")


def normalize_rule_pack_payload(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(raw or {})
    if "panels" in data and "panel" not in data:
        data["panel"] = data["panels"]
    data.pop("panels", None)

    if "query" not in data:
        query_payload = {}
        for field_name in list(data):
            if field_name in QUERY_OVERRIDE_FIELDS or field_name in {
                "not_feasible_patterns",
                "warning_patterns",
                "counter_suffixes",
                "label_rewrites",
                "label_candidates",
                "ignored_labels",
                "index_rewrites",
            }:
                query_payload[field_name] = data.pop(field_name)
        if query_payload:
            data["query"] = query_payload
    return data


def validate_rule_pack_payload(
    raw: dict[str, Any] | None,
    *,
    source: str = "rule pack",
) -> GrafanaRulePackModel:
    try:
        return GrafanaRulePackModel.model_validate(normalize_rule_pack_payload(raw))
    except ValidationError as exc:  # pragma: no cover - exercised via loaders/tests
        raise ValueError(f"Invalid Grafana rule pack in {source}: {exc}") from exc


__all__ = [
    "DashboardConfigModel",
    "GrafanaRulePackModel",
    "IndexRewriteRuleModel",
    "PanelConfigModel",
    "PatternRuleModel",
    "QueryConfigModel",
    "SchemaConfigModel",
    "normalize_rule_pack_payload",
    "validate_rule_pack_payload",
]
