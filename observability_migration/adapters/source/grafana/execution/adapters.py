"""Source-side HTTP execution adapters for Prometheus and Loki."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests


@dataclass
class SourceQueryResult:
    status: str = "error"
    rows: int = 0
    columns: list[str] = field(default_factory=list)
    values: list[list[Any]] = field(default_factory=list)
    result_type: str = ""
    error: str = ""
    query_time_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "columns": self.columns,
            "values": list(self.values[:10]),
            "result_type": self.result_type,
            "query_time_ms": self.query_time_ms,
            "metadata": dict(self.metadata),
        }


_TIMEOUT = 30


def _prometheus_instant_query(base_url: str, query: str, *, timeout: int = _TIMEOUT) -> SourceQueryResult:
    url = urljoin(base_url.rstrip("/") + "/", "api/v1/query")
    t0 = time.monotonic()
    try:
        resp = requests.get(url, params={"query": query}, timeout=timeout)
        elapsed = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        return SourceQueryResult(error=str(exc), query_time_ms=int((time.monotonic() - t0) * 1000))

    if body.get("status") != "success":
        return SourceQueryResult(error=body.get("error", "unknown error"), query_time_ms=elapsed)

    data = body.get("data", {})
    result_type = data.get("resultType", "")
    result_data = data.get("result", [])

    if result_type == "vector":
        columns = ["__name__", "value", "timestamp"] + _extract_label_keys(result_data)
        return SourceQueryResult(
            status="pass",
            rows=len(result_data),
            columns=columns,
            values=[[s.get("metric", {})] + list(s.get("value", [])) for s in result_data],
            result_type=result_type,
            query_time_ms=elapsed,
            metadata={"series": len(result_data)},
        )
    if result_type == "matrix":
        columns = ["__name__", "values", "timestamps"] + _extract_label_keys(result_data)
        total_samples = sum(len(s.get("values", [])) for s in result_data)
        return SourceQueryResult(
            status="pass",
            rows=total_samples,
            columns=columns,
            values=[[s.get("metric", {}), len(s.get("values", []))] for s in result_data],
            result_type=result_type,
            query_time_ms=elapsed,
            metadata={"series": len(result_data), "point_count": total_samples},
        )
    if result_type == "scalar":
        return SourceQueryResult(
            status="pass",
            rows=1,
            columns=["value", "timestamp"],
            values=[result_data],
            result_type=result_type,
            query_time_ms=elapsed,
            metadata={"series": 1},
        )
    return SourceQueryResult(
        status="pass",
        rows=len(result_data) if isinstance(result_data, list) else 1,
        columns=[],
        values=result_data if isinstance(result_data, list) else [result_data],
        result_type=result_type,
        query_time_ms=elapsed,
        metadata={},
    )


def _prometheus_range_query(
    base_url: str, query: str, *, start: str = "", end: str = "", step: str = "60s", timeout: int = _TIMEOUT,
) -> SourceQueryResult:
    url = urljoin(base_url.rstrip("/") + "/", "api/v1/query_range")
    now = int(time.time())
    params: dict[str, str] = {
        "query": query,
        "start": start or str(now - 3600),
        "end": end or str(now),
        "step": step,
    }
    t0 = time.monotonic()
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        elapsed = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        return SourceQueryResult(error=str(exc), query_time_ms=int((time.monotonic() - t0) * 1000))

    if body.get("status") != "success":
        return SourceQueryResult(error=body.get("error", "unknown error"), query_time_ms=elapsed)

    data = body.get("data", {})
    result_type = data.get("resultType", "")
    result_data = data.get("result", [])
    total_samples = sum(len(s.get("values", [])) for s in result_data) if isinstance(result_data, list) else 0
    columns = ["__name__", "values", "timestamps"] + _extract_label_keys(result_data)
    return SourceQueryResult(
        status="pass",
        rows=total_samples,
        columns=columns,
        values=[[s.get("metric", {}), len(s.get("values", []))] for s in result_data] if isinstance(result_data, list) else [],
        result_type=result_type,
        query_time_ms=elapsed,
        metadata={"series": len(result_data) if isinstance(result_data, list) else 0, "point_count": total_samples},
    )


def _loki_query(base_url: str, query: str, *, limit: int = 1000, timeout: int = _TIMEOUT) -> SourceQueryResult:
    url = urljoin(base_url.rstrip("/") + "/", "loki/api/v1/query_range")
    now = int(time.time())
    params: dict[str, str] = {
        "query": query,
        "start": str(now - 3600),
        "end": str(now),
        "limit": str(limit),
    }
    t0 = time.monotonic()
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        elapsed = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        return SourceQueryResult(error=str(exc), query_time_ms=int((time.monotonic() - t0) * 1000))

    if body.get("status") != "success":
        return SourceQueryResult(error=body.get("errorType", body.get("error", "unknown error")), query_time_ms=elapsed)

    data = body.get("data", {})
    result_type = data.get("resultType", "")
    result_data = data.get("result", [])
    if result_type == "streams":
        total_entries = sum(len(s.get("values", [])) for s in result_data)
        label_keys = _extract_label_keys(result_data, label_field="stream")
        return SourceQueryResult(
            status="pass",
            rows=total_entries,
            columns=["timestamp", "line"] + label_keys,
            result_type=result_type,
            query_time_ms=elapsed,
            metadata={"streams": len(result_data), "entry_count": total_entries},
        )
    if result_type == "matrix":
        total_samples = sum(len(s.get("values", [])) for s in result_data)
        columns = ["__name__", "values", "timestamps"] + _extract_label_keys(result_data)
        return SourceQueryResult(
            status="pass",
            rows=total_samples,
            columns=columns,
            result_type=result_type,
            query_time_ms=elapsed,
            metadata={"series": len(result_data), "point_count": total_samples},
        )
    return SourceQueryResult(
        status="pass",
        rows=len(result_data) if isinstance(result_data, list) else 0,
        columns=[],
        result_type=result_type,
        query_time_ms=elapsed,
        metadata={},
    )


def _extract_label_keys(result_data: list, *, label_field: str = "metric") -> list[str]:
    keys: set[str] = set()
    for item in (result_data or [])[:50]:
        if isinstance(item, dict):
            labels = item.get(label_field, {})
            if isinstance(labels, dict):
                keys.update(labels.keys())
    return sorted(keys)


def execute_source_query(
    query: str,
    query_language: str,
    *,
    prometheus_url: str = "",
    loki_url: str = "",
    range_window: str = "",
    timeout: int = _TIMEOUT,
) -> SourceQueryResult:
    lang = query_language.lower()
    if lang == "promql" and prometheus_url:
        if range_window:
            return _prometheus_range_query(prometheus_url, query, timeout=timeout)
        return _prometheus_instant_query(prometheus_url, query, timeout=timeout)
    if lang == "logql" and loki_url:
        return _loki_query(loki_url, query, timeout=timeout)
    return SourceQueryResult(error="No adapter URL configured for this query language")
