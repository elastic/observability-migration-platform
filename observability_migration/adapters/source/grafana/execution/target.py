from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TargetExecutionSummary:
    status: str = "not_run"
    adapter: str = "elasticsearch_esql"
    query: str = ""
    materialized_query: str = ""
    target_index: str = ""
    result_summary: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    fix_attempts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_target_execution_summary(
    panel_result: Any,
    validation_record: dict[str, Any] | None = None,
) -> TargetExecutionSummary:
    validation_record = validation_record or {}
    analysis = validation_record.get("analysis") or {}
    query_ir = getattr(panel_result, "query_ir", {}) or {}
    target_index = str(
        analysis.get("target_index")
        or (query_ir.get("target_index") if isinstance(query_ir, dict) else "")
        or ""
    )
    return TargetExecutionSummary(
        status=str(validation_record.get("status", "not_run") or "not_run"),
        query=str(validation_record.get("query") or getattr(panel_result, "esql_query", "") or ""),
        materialized_query=str(analysis.get("materialized_query", "") or ""),
        target_index=target_index,
        result_summary={
            "rows": analysis.get("result_rows"),
            "columns": list(analysis.get("result_columns", []) or []),
            "values": list(analysis.get("result_values", []) or []),
            "metadata": dict(analysis.get("result_metadata", {}) or {}),
            "sample_window": dict(analysis.get("sample_window", {}) or {}),
        },
        error=str(validation_record.get("error", "") or ""),
        fix_attempts=[str(item) for item in (validation_record.get("fix_attempts", []) or [])],
    )
