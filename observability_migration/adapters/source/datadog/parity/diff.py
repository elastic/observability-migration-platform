# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Parity diff: compare DD timeseries against ES|QL timeseries.

The two stores return data in very different shapes, so we normalize
each to a list of (tag_key, [(timestamp_seconds, value), ...]) tuples
before computing per-bucket numeric error.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Series:
    """Normalized timeseries: a tag identity and a sorted point list."""

    tag_key: str  # canonical tag-set, e.g. "host=h1,service=web"
    points: list[tuple[int, float]]  # (unix_seconds, value)


@dataclass
class DiffOutcome:
    panel_title: str
    dd_query: str
    es_query: str
    dd_series_count: int
    es_series_count: int
    matched_tag_keys: int
    only_in_dd: list[str] = field(default_factory=list)
    only_in_es: list[str] = field(default_factory=list)
    per_series_max_rel_error: float = 0.0
    per_series_max_abs_error: float = 0.0
    verdict: str = "UNKNOWN"
    note: str = ""


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


def normalize_dd_response(
    response: dict[str, Any],
    *,
    tag_remap: dict[str, str] | None = None,
) -> list[Series]:
    """Extract series from /api/v1/query response.

    DD returns:
        {"series": [{"scope": "host:h1,service:web",
                     "pointlist": [[ts_ms, value], ...]}, ...]}

    tag_remap, when supplied, rewrites DD tag names to the equivalent
    ES field names (e.g. host → host.name) so that diffing matches
    series across the two stores after the OTel field profile has
    renamed dimensions on the ES side.
    """

    out: list[Series] = []
    for s in response.get("series") or []:
        tag_key = _canonical_dd_scope(s.get("scope", ""), tag_remap=tag_remap)
        pts: list[tuple[int, float]] = []
        for pair in s.get("pointlist") or []:
            if not isinstance(pair, list) or len(pair) < 2:
                continue
            ts_ms, value = pair[0], pair[1]
            if value is None:
                continue
            pts.append((int(ts_ms // 1000), float(value)))
        out.append(Series(tag_key=tag_key, points=sorted(pts)))
    return out


def normalize_esql_response(
    response: dict[str, Any],
    *,
    value_col: str,
    time_col: str = "time_bucket",
    group_cols: list[str] | None = None,
) -> list[Series]:
    """Extract series from an ES|QL response.

    ES|QL response shape:
        {"columns": [{"name": "time_bucket"}, {"name": "host.name"}, {"name": "query1"}],
         "values": [["2026-05-21T12:00:00.000Z", "h1", 0.42], ...]}
    """

    cols = response.get("columns") or []
    rows = response.get("values") or []
    col_index = {c.get("name", ""): i for i, c in enumerate(cols)}
    if value_col not in col_index:
        return []
    series_by_tag: dict[str, list[tuple[int, float]]] = {}
    for row in rows:
        if time_col in col_index:
            ts = _iso_to_unix(row[col_index[time_col]])
        else:
            ts = 0
        value = row[col_index[value_col]]
        if value is None:
            continue
        tag_parts = []
        for col_name in sorted(group_cols or []):
            if col_name in col_index:
                tag_parts.append(f"{col_name}={row[col_index[col_name]]}")
        tag_key = ",".join(tag_parts)
        series_by_tag.setdefault(tag_key, []).append((int(ts), float(value)))
    return [Series(tag_key=k, points=sorted(v)) for k, v in series_by_tag.items()]


def _canonical_dd_scope(
    scope: str,
    *,
    tag_remap: dict[str, str] | None = None,
) -> str:
    """Normalize 'host:h1,service:web' to 'host=h1,service=web' (sorted).

    If tag_remap is provided, rewrite each tag key through it before
    canonicalizing (so 'host' becomes 'host.name' when matching the
    OTel ES profile).
    """

    parts = []
    for chunk in scope.split(","):
        chunk = chunk.strip()
        if not chunk or chunk == "*":
            continue
        if ":" in chunk:
            k, v = chunk.split(":", 1)
            k = k.strip()
            if tag_remap and k in tag_remap:
                k = tag_remap[k]
            parts.append(f"{k}={v.strip()}")
        else:
            parts.append(chunk)
    return ",".join(sorted(parts))


def _iso_to_unix(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        # ES|QL emits ISO 8601 with milliseconds, e.g. 2026-05-21T12:00:00.000Z
        import datetime

        s = value.rstrip("Z")
        try:
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            return int(dt.timestamp())
        except ValueError:
            return 0
    return 0


# ---------------------------------------------------------------------------
# ES|QL HTTP client
# ---------------------------------------------------------------------------


def run_esql(
    *,
    es_endpoint: str,
    api_key: str,
    query: str,
    params: list[Any] | None = None,
) -> dict[str, Any]:
    """POST /_query (ES|QL). Returns the parsed JSON response."""

    body = {"query": query}
    if params:
        body["params"] = params
    headers = {
        "Authorization": f"ApiKey {api_key}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        url=f"{es_endpoint.rstrip('/')}/_query",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30.0, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"ES|QL → {exc.code} {exc.reason}: {body_text[:500]}"
        ) from exc


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def diff_series(
    dd_series: list[Series],
    es_series: list[Series],
    *,
    rel_tolerance: float = 0.05,
    abs_tolerance: float = 0.01,
    time_window_seconds: int = 90,
) -> tuple[float, float, list[str], list[str]]:
    # When the DD query filters by a tag (e.g. {host:h1}) without a group-by,
    # DD's scope echoes the filter while ES|QL's projection has no tag column.
    # Treat the single-series case as equivalent: align them by index.
    if (
        len(dd_series) == 1
        and len(es_series) == 1
        and dd_series[0].tag_key
        and not es_series[0].tag_key
    ):
        dd_series = [Series(tag_key="", points=dd_series[0].points)]
    """Compare DD and ES series tag-by-tag.

    Returns (max_relative_error, max_absolute_error, only_in_dd_tags,
    only_in_es_tags). Each series's points are aligned within a
    time_window_seconds tolerance — DD and ES bucket boundaries
    can differ by up to one bucket span.
    """

    dd_by_tag = {s.tag_key: s for s in dd_series}
    es_by_tag = {s.tag_key: s for s in es_series}

    only_in_dd = sorted(set(dd_by_tag) - set(es_by_tag))
    only_in_es = sorted(set(es_by_tag) - set(dd_by_tag))

    max_rel = 0.0
    max_abs = 0.0
    for tag_key in sorted(set(dd_by_tag) & set(es_by_tag)):
        for dd_ts, dd_val in dd_by_tag[tag_key].points:
            best = _nearest_point(es_by_tag[tag_key].points, dd_ts, time_window_seconds)
            if best is None:
                continue
            _, es_val = best
            abs_err = abs(dd_val - es_val)
            denom = max(abs(dd_val), abs(es_val), 1e-9)
            rel_err = abs_err / denom
            max_abs = max(max_abs, abs_err)
            max_rel = max(max_rel, rel_err)

    return max_rel, max_abs, only_in_dd, only_in_es


def _nearest_point(
    points: list[tuple[int, float]], target_ts: int, window: int
) -> tuple[int, float] | None:
    best: tuple[int, float] | None = None
    best_dt = window + 1
    for ts, value in points:
        dt = abs(ts - target_ts)
        if dt <= window and dt < best_dt:
            best = (ts, value)
            best_dt = dt
    return best


def verdict_for(
    *,
    max_rel: float,
    max_abs: float,
    only_in_dd: list[str],
    only_in_es: list[str],
    rel_strict: float = 0.01,
    rel_fuzzy: float = 0.05,
) -> tuple[str, str]:
    """Compute a verdict and short note from diff metrics."""

    if only_in_dd or only_in_es:
        return "SHAPE_MISMATCH", (
            f"series tag-set mismatch — only_in_dd={len(only_in_dd)} "
            f"only_in_es={len(only_in_es)}"
        )
    if max_rel <= rel_strict:
        return "STRICT_PASS", f"max_rel={max_rel:.4f}"
    if max_rel <= rel_fuzzy:
        return "FUZZY_PASS", f"max_rel={max_rel:.4f}"
    return "FAIL_DIVERGENT", f"max_rel={max_rel:.4f}, max_abs={max_abs:.4f}"
