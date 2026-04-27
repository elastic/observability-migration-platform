from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .adapters import execute_source_query


@dataclass
class SourceExecutionSummary:
    status: str = "not_attempted"
    adapter: str = ""
    query_language: str = ""
    query: str = ""
    reason: str = ""
    result_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_source_execution_summary(
    panel_result: Any,
    *,
    prometheus_url: str = "",
    loki_url: str = "",
) -> SourceExecutionSummary:
    query_language = str(getattr(panel_result, "query_language", "") or "").lower()

    if query_language == "esql":
        return SourceExecutionSummary(
            status="not_applicable",
            adapter="elasticsearch_native",
            query_language=query_language,
            query=str(getattr(panel_result, "esql_query", "") or ""),
            reason="Source is already Elasticsearch; no separate source adapter needed",
        )

    if query_language == "promql":
        adapter = "prometheus_http"
        source_query = str(getattr(panel_result, "promql_expr", "") or "")
        if not prometheus_url:
            return SourceExecutionSummary(
                status="not_configured",
                adapter=adapter,
                query_language=query_language,
                query=source_query,
                reason=f"{adapter} adapter recognized; pass --prometheus-url to enable live source execution",
            )
        return _execute_live(adapter, query_language, source_query, prometheus_url=prometheus_url)

    if query_language == "logql":
        adapter = "loki_http"
        source_query = str(getattr(panel_result, "promql_expr", "") or "")
        if not loki_url:
            return SourceExecutionSummary(
                status="not_configured",
                adapter=adapter,
                query_language=query_language,
                query=source_query,
                reason=f"{adapter} adapter recognized; pass --loki-url to enable live source execution",
            )
        return _execute_live(adapter, query_language, source_query, loki_url=loki_url)

    return SourceExecutionSummary(
        status="not_applicable",
        adapter="none",
        query_language=query_language,
        query="",
        reason="No source-side execution adapter applies to this query language",
    )


def _execute_live(
    adapter: str,
    query_language: str,
    query: str,
    *,
    prometheus_url: str = "",
    loki_url: str = "",
) -> SourceExecutionSummary:
    if not query:
        return SourceExecutionSummary(
            status="skip",
            adapter=adapter,
            query_language=query_language,
            query="",
            reason="Source query is empty; cannot execute",
        )
    result = execute_source_query(
        query,
        query_language,
        prometheus_url=prometheus_url,
        loki_url=loki_url,
    )
    if result.status == "pass":
        return SourceExecutionSummary(
            status="pass",
            adapter=adapter,
            query_language=query_language,
            query=query,
            reason="",
            result_summary=result.to_summary(),
        )
    return SourceExecutionSummary(
        status="fail",
        adapter=adapter,
        query_language=query_language,
        query=query,
        reason=result.error or "Source query execution failed",
        result_summary=result.to_summary(),
    )
