# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Minimal Datadog API wrapper for parity testing.

Covers two endpoints:
- POST /api/v2/series — submit metric points
- GET  /api/v1/query  — query timeseries data
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class DDPoint:
    timestamp: int
    value: float


@dataclass
class DDSeries:
    """One series to submit via /api/v2/series."""

    metric: str
    points: list[DDPoint]
    tags: list[str]
    metric_type: int = 0  # 0=unspecified, 1=count, 2=rate, 3=gauge
    interval: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "type": self.metric_type,
            "interval": self.interval,
            "points": [{"timestamp": p.timestamp, "value": p.value} for p in self.points],
            "resources": [],
            "tags": list(self.tags),
        }


@dataclass
class DDDistributionSeries:
    """One distribution series to submit via /api/v1/distribution_points.

    Each `points` entry is `[timestamp_seconds, [value, value, ...]]` —
    a list of raw observations at that bucket, from which DD computes
    p50/p75/p90/p95/p99 etc.
    """

    metric: str
    points: list[tuple[int, list[float]]]
    tags: list[str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "points": [[ts, list(values)] for ts, values in self.points],
            "tags": list(self.tags),
        }


class DDClient:
    """Thin Datadog HTTP API wrapper.

    Use site='datadoghq.com' (US1) by default; matches DD_SITE env var.
    """

    def __init__(
        self,
        *,
        api_key: str,
        app_key: str,
        site: str = "datadoghq.com",
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("DD_API_KEY is required")
        self.api_key = api_key
        self.app_key = app_key or ""
        self.site = site
        self.timeout = timeout
        self.base = f"https://api.{site}"

    def _headers(self, *, include_app_key: bool) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "DD-API-KEY": self.api_key,
        }
        if include_app_key and self.app_key:
            h["DD-APPLICATION-KEY"] = self.app_key
        return h

    def submit_distribution(
        self, series: list[DDDistributionSeries]
    ) -> dict[str, Any]:
        """POST /api/v1/distribution_points — submit distribution-typed
        metric points. Distribution metrics are required for DD percentile
        aggregators (p50/p75/p90/p95/p99); regular gauge submissions
        return empty series for these queries.
        """

        payload = {"series": [s.to_payload() for s in series]}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base}/api/v1/distribution_points",
            data=body,
            method="POST",
            headers=self._headers(include_app_key=False),
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def submit_series(self, series: list[DDSeries]) -> dict[str, Any]:
        """POST /api/v2/series — submit metric points.

        Returns the parsed JSON response.
        """

        payload = {"series": [s.to_payload() for s in series]}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base}/api/v2/series",
            data=body,
            method="POST",
            headers=self._headers(include_app_key=False),
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def query_timeseries(
        self,
        *,
        query: str,
        from_ts: int,
        to_ts: int,
    ) -> dict[str, Any]:
        """GET /api/v1/query — query a DD metric expression over a window.

        from_ts and to_ts are unix seconds. Returns the parsed JSON
        response. The response shape is:

            {
              "status": "ok",
              "series": [{"scope": "...", "expression": "...",
                          "pointlist": [[ts_ms, value], ...]}],
              ...
            }
        """

        params = {"query": query, "from": str(from_ts), "to": str(to_ts)}
        url = f"{self.base}/api/v1/query?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url=url, method="GET", headers=self._headers(include_app_key=True)
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def wait_for_ingestion(self, *, settle_seconds: float = 30.0) -> None:
        """Block until ingested points are likely queryable.

        DD's ingestion pipeline takes ~10-60s for points to land. Default
        wait is 30 seconds which works for the deterministic single-host
        seeds the parity rig uses.
        """

        time.sleep(settle_seconds)
