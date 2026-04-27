"""Live Datadog source execution helpers for verification packets.

Supports three execution paths:
- Single metric queries via ``/api/v1/query``
- Multi-query metric widgets (each query executed independently, results merged)
- Log search queries via ``/api/v2/logs/events/search``
"""

from __future__ import annotations

import time
from typing import Any

import requests

from observability_migration.adapters.source.grafana.execution.adapters import SourceQueryResult
from observability_migration.adapters.source.grafana.execution.source import SourceExecutionSummary

_TIMEOUT = 30


def build_source_execution_summary(
    panel_result: Any,
    *,
    api_key: str = "",
    app_key: str = "",
    site: str = "datadoghq.com",
    timeout: int = _TIMEOUT,
) -> SourceExecutionSummary:
    query_language = str(getattr(panel_result, "query_language", "") or "").lower()
    source_queries = [
        str(item or "").strip()
        for item in (getattr(panel_result, "source_queries", []) or [])
        if str(item or "").strip()
    ]
    query = source_queries[0] if source_queries else ""

    if query_language == "datadog_log":
        return _build_log_execution(
            source_queries, query_language=query_language,
            api_key=api_key, app_key=app_key, site=site, timeout=timeout,
            panel_result=panel_result,
        )

    if query_language != "datadog_metric":
        return SourceExecutionSummary(
            status="not_applicable",
            adapter="none",
            query_language=query_language,
            query=query,
            reason="No Datadog source-side execution adapter applies to this panel",
        )

    if not api_key or not app_key:
        return SourceExecutionSummary(
            status="not_configured",
            adapter="datadog_metrics_http",
            query_language=query_language,
            query=query,
            reason="Datadog API credentials are missing; set DD_API_KEY/DD_APP_KEY or pass --env-file",
        )

    if not source_queries:
        return SourceExecutionSummary(
            status="skip",
            adapter="datadog_metrics_http",
            query_language=query_language,
            query="",
            reason="Source query is empty; cannot execute",
        )

    if len(source_queries) == 1:
        return _build_single_metric_execution(
            query, query_language=query_language,
            api_key=api_key, app_key=app_key, site=site, timeout=timeout,
            panel_result=panel_result,
        )

    return _build_multi_metric_execution(
        source_queries, query_language=query_language,
        api_key=api_key, app_key=app_key, site=site, timeout=timeout,
        panel_result=panel_result,
    )


def _build_single_metric_execution(
    query: str,
    *,
    query_language: str,
    api_key: str,
    app_key: str,
    site: str,
    timeout: int,
    panel_result: Any,
) -> SourceExecutionSummary:
    result = _execute_metric_query(query, api_key=api_key, app_key=app_key, site=site, timeout=timeout)
    if result.status != "pass":
        return SourceExecutionSummary(
            status="fail",
            adapter="datadog_metrics_http",
            query_language=query_language,
            query=query,
            reason=result.error or "Datadog source query execution failed",
            result_summary=result.to_summary(),
        )

    normalized = _normalize_metric_result_for_panel(result, panel_result)
    return SourceExecutionSummary(
        status="pass",
        adapter="datadog_metrics_http",
        query_language=query_language,
        query=query,
        reason="",
        result_summary=normalized.to_summary(),
    )


def _build_multi_metric_execution(
    queries: list[str],
    *,
    query_language: str,
    api_key: str,
    app_key: str,
    site: str,
    timeout: int,
    panel_result: Any,
) -> SourceExecutionSummary:
    per_query_timeout = max(timeout // len(queries), 5)
    all_results: list[SourceQueryResult] = []
    errors: list[str] = []
    for query in queries:
        result = _execute_metric_query(
            query, api_key=api_key, app_key=app_key, site=site, timeout=per_query_timeout,
        )
        all_results.append(result)
        if result.status != "pass":
            errors.append(result.error or f"query failed: {query[:80]}")

    if errors and len(errors) == len(queries):
        return SourceExecutionSummary(
            status="fail",
            adapter="datadog_metrics_http",
            query_language=query_language,
            query=queries[0],
            reason=f"All {len(queries)} metric queries failed: {errors[0]}",
            result_summary=all_results[0].to_summary() if all_results else {},
        )

    merged = _merge_metric_results(all_results, queries)
    normalized = _normalize_metric_result_for_panel(merged, panel_result)
    reason = ""
    if errors:
        reason = f"{len(errors)}/{len(queries)} queries failed; merged from passing queries only"
    return SourceExecutionSummary(
        status="pass",
        adapter="datadog_metrics_http",
        query_language=query_language,
        query="; ".join(queries),
        reason=reason,
        result_summary=normalized.to_summary(),
    )


def _merge_metric_results(results: list[SourceQueryResult], queries: list[str]) -> SourceQueryResult:
    total_rows = 0
    total_points = 0
    total_series = 0
    merged_values: list[list[Any]] = []
    merged_columns = ["metric", "timestamp", "value", "scope", "query_index"]
    query_time_ms = 0
    for idx, result in enumerate(results):
        if result.status != "pass":
            continue
        total_rows += result.rows
        total_series += result.metadata.get("series", 0)
        total_points += result.metadata.get("point_count", 0)
        query_time_ms += result.query_time_ms
        for row in result.values:
            extended = list(row) + [idx] if isinstance(row, (list, tuple)) else [row, idx]
            merged_values.append(extended)
    return SourceQueryResult(
        status="pass",
        rows=total_rows,
        columns=merged_columns,
        values=merged_values[:20],
        result_type="timeseries",
        query_time_ms=query_time_ms,
        metadata={
            "series": total_series,
            "point_count": total_points,
            "queries_executed": len(results),
            "queries_passed": sum(1 for r in results if r.status == "pass"),
        },
    )


def _build_log_execution(
    queries: list[str],
    *,
    query_language: str,
    api_key: str,
    app_key: str,
    site: str,
    timeout: int,
    panel_result: Any,
) -> SourceExecutionSummary:
    query = queries[0] if queries else ""
    if not api_key or not app_key:
        return SourceExecutionSummary(
            status="not_configured",
            adapter="datadog_logs_http",
            query_language=query_language,
            query=query,
            reason="Datadog API credentials are missing; set DD_API_KEY/DD_APP_KEY or pass --env-file",
        )
    if not query:
        return SourceExecutionSummary(
            status="skip",
            adapter="datadog_logs_http",
            query_language=query_language,
            query="",
            reason="Log query is empty; cannot execute",
        )
    result = _execute_log_query(query, api_key=api_key, app_key=app_key, site=site, timeout=timeout)
    if result.status != "pass":
        return SourceExecutionSummary(
            status="fail",
            adapter="datadog_logs_http",
            query_language=query_language,
            query=query,
            reason=result.error or "Datadog log query execution failed",
            result_summary=result.to_summary(),
        )
    return SourceExecutionSummary(
        status="pass",
        adapter="datadog_logs_http",
        query_language=query_language,
        query=query,
        reason="",
        result_summary=result.to_summary(),
    )


def _execute_metric_query(
    query: str,
    *,
    api_key: str,
    app_key: str,
    site: str,
    timeout: int,
) -> SourceQueryResult:
    base_url = _datadog_api_base(site)
    now = int(time.time())
    params = {
        "from": str(now - 3600),
        "to": str(now),
        "query": query,
    }
    t0 = time.monotonic()
    try:
        resp = requests.get(
            f"{base_url}/api/v1/query",
            params=params,
            headers=_auth_headers(api_key, app_key),
            timeout=timeout,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        body = resp.json()
    except Exception as exc:
        return SourceQueryResult(error=str(exc), query_time_ms=int((time.monotonic() - t0) * 1000))

    if resp.status_code != 200:
        error = str(body.get("errors") or body.get("error") or f"HTTP {resp.status_code}")
        return SourceQueryResult(error=error, query_time_ms=elapsed)

    if str(body.get("status", "") or "").lower() not in {"ok", "success"}:
        error = str(body.get("errors") or body.get("error") or "unknown error")
        return SourceQueryResult(error=error, query_time_ms=elapsed)

    series = list(body.get("series") or [])
    latest_rows: list[list[Any]] = []
    point_count = 0
    for entry in series:
        points = [point for point in (entry.get("pointlist") or []) if _valid_point(point)]
        point_count += len(points)
        if not points:
            continue
        latest_ts, latest_value = points[-1][0], points[-1][1]
        latest_rows.append(
            [
                str(entry.get("metric", "") or ""),
                latest_ts,
                latest_value,
                str(entry.get("scope", "") or ""),
            ]
        )

    metadata: dict[str, Any] = {
        "series": len(series),
        "point_count": point_count,
    }
    if len(latest_rows) == 1:
        metadata["latest_value"] = latest_rows[0][2]

    return SourceQueryResult(
        status="pass",
        rows=point_count,
        columns=["metric", "timestamp", "value", "scope"],
        values=latest_rows,
        result_type="timeseries",
        query_time_ms=elapsed,
        metadata=metadata,
    )


def _execute_log_query(
    query: str,
    *,
    api_key: str,
    app_key: str,
    site: str,
    timeout: int,
) -> SourceQueryResult:
    base_url = _datadog_api_base(site)
    now = int(time.time())
    body = {
        "filter": {
            "query": query,
            "from": f"{now - 3600}000",
            "to": f"{now}000",
        },
        "page": {"limit": 10},
    }
    t0 = time.monotonic()
    try:
        resp = requests.post(
            f"{base_url}/api/v2/logs/events/search",
            json=body,
            headers={**_auth_headers(api_key, app_key), "Content-Type": "application/json"},
            timeout=timeout,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        resp_body = resp.json()
    except Exception as exc:
        return SourceQueryResult(error=str(exc), query_time_ms=int((time.monotonic() - t0) * 1000))

    if resp.status_code != 200:
        error = str(resp_body.get("errors") or resp_body.get("error") or f"HTTP {resp.status_code}")
        return SourceQueryResult(error=error, query_time_ms=elapsed)

    data_items = list(resp_body.get("data") or [])
    rows: list[list[Any]] = []
    for item in data_items:
        attrs = item.get("attributes", {})
        rows.append([
            str(attrs.get("timestamp", "")),
            str(attrs.get("service", "")),
            str(attrs.get("status", "")),
            str(attrs.get("message", ""))[:200],
        ])

    total_matched = len(data_items)
    meta = resp_body.get("meta", {})
    page_info = meta.get("page", {}) if isinstance(meta, dict) else {}
    if isinstance(page_info, dict) and page_info.get("after"):
        total_matched = max(total_matched, 10)

    return SourceQueryResult(
        status="pass",
        rows=total_matched,
        columns=["timestamp", "service", "status", "message"],
        values=rows,
        result_type="event_rows",
        query_time_ms=elapsed,
        metadata={
            "logs_matched": total_matched,
            "sampled": len(rows),
            "has_more": bool(isinstance(page_info, dict) and page_info.get("after")),
        },
    )


def _normalize_metric_result_for_panel(result: SourceQueryResult, panel_result: Any) -> SourceQueryResult:
    kibana_type = str(getattr(panel_result, "kibana_type", "") or "").lower()
    metadata = dict(result.metadata)

    if kibana_type == "metric":
        latest_value = _latest_numeric_value(result)
        scalar_values = [[latest_value]] if latest_value is not None else []
        if latest_value is not None:
            metadata["latest_value"] = latest_value
        return SourceQueryResult(
            status=result.status,
            rows=1 if scalar_values else 0,
            columns=["value"],
            values=scalar_values,
            result_type="scalar",
            error=result.error,
            query_time_ms=result.query_time_ms,
            metadata=metadata,
        )

    if kibana_type in {"table", "partition", "treemap"}:
        return SourceQueryResult(
            status=result.status,
            rows=len(result.values),
            columns=list(result.columns),
            values=list(result.values),
            result_type="categorical_distribution",
            error=result.error,
            query_time_ms=result.query_time_ms,
            metadata=metadata,
        )

    return result


def _latest_numeric_value(result: SourceQueryResult) -> float | None:
    latest = result.metadata.get("latest_value")
    if latest is not None:
        try:
            return float(latest)
        except (TypeError, ValueError):
            pass
    for row in result.values:
        if not isinstance(row, (list, tuple)):
            continue
        for item in reversed(row):
            try:
                if isinstance(item, bool):
                    continue
                return float(item)
            except (TypeError, ValueError):
                continue
    return None


def _auth_headers(api_key: str, app_key: str) -> dict[str, str]:
    return {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
    }


def _datadog_api_base(site: str) -> str:
    cleaned = str(site or "datadoghq.com").strip().rstrip("/")
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return cleaned
    if cleaned.startswith("api."):
        return f"https://{cleaned}"
    return f"https://api.{cleaned}"


def _valid_point(point: Any) -> bool:
    return (
        isinstance(point, (list, tuple))
        and len(point) >= 2
        and point[0] is not None
        and point[1] is not None
    )


__all__ = ["build_source_execution_summary"]
