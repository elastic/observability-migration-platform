"""Canonical query IR — semantic intent of a query.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

METRIC_LIKE_PANEL_TYPES = {"stat", "singlestat", "gauge", "bargauge"}
TABLE_LIKE_PANEL_TYPES = {"table", "table-old"}
TIME_LIKE_OUTPUT_FIELDS = {"time_bucket", "timestamp_bucket", "step", "@timestamp"}


@dataclass
class QueryIR:
    version: int = 1
    source_language: str = ""
    source_expression: str = ""
    clean_expression: str = ""
    panel_type: str = ""
    datasource_type: str = ""
    datasource_uid: str = ""
    datasource_name: str = ""
    family: str = ""
    metric: str = ""
    range_function: str = ""
    range_window: str = ""
    outer_agg: str = ""
    group_labels: list[str] = field(default_factory=list)
    group_mode: str = "by"
    label_filters: list[str] = field(default_factory=list)
    binary_op: str = ""
    output_shape: str = ""
    source_type: str = ""
    target_index: str = ""
    target_query: str = ""
    output_metric_field: str = ""
    output_group_fields: list[str] = field(default_factory=list)
    summary_mode: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    semantic_losses: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def infer_output_shape(panel_type: str, output_group_fields: list[str], source_language: str) -> str:
    if source_language == "logql" and panel_type == "logs":
        return "event_rows"
    if panel_type in TABLE_LIKE_PANEL_TYPES:
        return "table"
    if panel_type == "logs":
        return "event_rows"
    if any(field in TIME_LIKE_OUTPUT_FIELDS for field in (output_group_fields or [])):
        return "time_series"
    if output_group_fields:
        return "table"
    if panel_type in METRIC_LIKE_PANEL_TYPES:
        return "single_value"
    return "time_series"


def _infer_source_language(context: Any) -> str:
    query_language = str(getattr(context, "query_language", "") or "").strip().lower()
    if query_language:
        return query_language

    datasource_type = str(getattr(context, "datasource_type", "") or "").strip().lower()
    if "loki" in datasource_type:
        return "logql"
    if "elastic" in datasource_type:
        return "esql" if str(getattr(context, "promql_expr", "") or "").strip().upper().startswith(("FROM ", "TS ", "ROW ")) else "elasticsearch"

    frag = getattr(context, "fragment", None)
    family = str(getattr(frag, "family", "") or "").strip().lower()
    if family.startswith("logql"):
        return "logql"
    if family:
        return "promql"
    return "unknown"


def _is_semantic_loss_warning(warning: str) -> bool:
    text = str(warning or "").lower()
    token_matches = (
        "approximat",
        "drop",
        "fallback",
        "manual",
        "incompatible target",
        "only 1 could be migrated",
        "cannot be accurately represented",
        "requires both sides",
        "translation crashed",
    )
    return any(token in text for token in token_matches)


def build_query_ir(context: Any) -> QueryIR:
    frag = getattr(context, "fragment", None)
    warnings = list(getattr(context, "warnings", []) or [])
    group_labels = list(getattr(context, "group_labels", []) or [])
    if not group_labels and frag and getattr(frag, "group_labels", None):
        group_labels = list(frag.group_labels)

    semantic_losses = [
        warning
        for warning in warnings
        if _is_semantic_loss_warning(warning)
    ]
    source_language = _infer_source_language(context)

    return QueryIR(
        source_language=source_language,
        source_expression=str(getattr(context, "promql_expr", "") or ""),
        clean_expression=str(getattr(context, "clean_expr", "") or ""),
        panel_type=str(getattr(context, "panel_type", "") or ""),
        datasource_type=str(getattr(context, "datasource_type", "") or ""),
        datasource_uid=str(getattr(context, "datasource_uid", "") or ""),
        datasource_name=str(getattr(context, "datasource_name", "") or ""),
        family=str(getattr(frag, "family", "") or ""),
        metric=str(getattr(context, "metric_name", "") or getattr(frag, "metric", "") or ""),
        range_function=str(getattr(context, "inner_func", "") or getattr(frag, "range_func", "") or ""),
        range_window=str(getattr(context, "range_window", "") or getattr(frag, "range_window", "") or ""),
        outer_agg=str(getattr(context, "outer_agg", "") or getattr(frag, "outer_agg", "") or ""),
        group_labels=group_labels,
        group_mode=str(getattr(frag, "group_mode", "") or "by"),
        label_filters=[str(item) for item in (getattr(context, "label_filters", []) or [])],
        binary_op=str(getattr(frag, "binary_op", "") or ""),
        output_shape=infer_output_shape(
            str(getattr(context, "panel_type", "") or ""),
            list(getattr(context, "output_group_fields", []) or []),
            source_language,
        ),
        source_type=str(getattr(context, "source_type", "") or ""),
        target_index=str(getattr(context, "index", "") or ""),
        target_query=str(getattr(context, "esql_query", "") or ""),
        output_metric_field=str(getattr(context, "output_metric_field", "") or ""),
        output_group_fields=list(getattr(context, "output_group_fields", []) or []),
        summary_mode=bool((getattr(context, "metadata", {}) or {}).get("summary_mode")),
        metadata=dict(getattr(context, "metadata", {}) or {}),
        warnings=warnings,
        semantic_losses=semantic_losses,
    )
