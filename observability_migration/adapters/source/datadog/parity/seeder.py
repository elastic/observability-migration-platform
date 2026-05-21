# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Synthetic data seeder for DD↔ES parity tests.

Generates a deterministic timeseries (timestamp, tag-set, value) and
pushes the same logical data to both Datadog and Elasticsearch with
matching timestamps. Field naming uses the OTel profile mapping so that
DD `parity.cpu` becomes ES `parity_cpu` and DD `host` tag becomes ES
`host.name`, matching what the translation pipeline emits.
"""

from __future__ import annotations

import json
import math
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass

from .dd_client import DDClient, DDDistributionSeries, DDPoint, DDSeries


@dataclass
class ParitySeries:
    """Logical series with the same content on both DD and ES sides."""

    dd_metric: str          # DD metric name, e.g. "parity.cpu"
    es_field: str           # ES field name, e.g. "parity_cpu"
    tag_value_map: dict[str, str]  # DD tag-set, e.g. {"host": "h1", "service": "web"}
    es_tag_map: dict[str, str]     # ES field map for tags, e.g. {"host.name": "h1"}
    points: list[tuple[int, float]]  # (unix_seconds, value) pairs


def generate_series(
    *,
    dd_metric: str,
    es_field: str,
    tags: dict[str, str],
    es_tag_fields: dict[str, str],
    start_ts: int,
    end_ts: int,
    interval_seconds: int,
    value_fn: callable,
) -> ParitySeries:
    """Build a ParitySeries by sampling value_fn(idx, ts) over the window."""

    points: list[tuple[int, float]] = []
    ts = start_ts
    idx = 0
    while ts <= end_ts:
        points.append((ts, float(value_fn(idx, ts))))
        ts += interval_seconds
        idx += 1
    return ParitySeries(
        dd_metric=dd_metric,
        es_field=es_field,
        tag_value_map=tags,
        es_tag_map=es_tag_fields,
        points=points,
    )


def push_distribution_to_datadog(
    client: DDClient,
    series_list: Iterable[ParitySeries],
    *,
    samples_per_point: int = 10,
) -> dict:
    """Submit each ParitySeries to DD as a distribution series.

    Each point's value becomes a `samples_per_point`-element list, so DD
    can compute percentile aggregations (p50/p75/p90/p95/p99) that
    require distribution-typed metrics. ES side stores one doc per
    sample-bag entry (samples expanded into separate docs) so
    PERCENTILE(metric, 95) gives the same answer.
    """

    dd_series = []
    for s in series_list:
        tag_strs = [f"{k}:{v}" for k, v in sorted(s.tag_value_map.items())]
        dd_series.append(
            DDDistributionSeries(
                metric=s.dd_metric,
                points=[(ts, [v] * samples_per_point) for ts, v in s.points],
                tags=tag_strs,
            )
        )
    return client.submit_distribution(dd_series)


def push_to_datadog(client: DDClient, series_list: Iterable[ParitySeries]) -> dict:
    """Submit all series to DD via /api/v2/series.

    DD's series API accepts up to 500KB per request; for the small
    synthetic loads in the parity rig we send everything in one call.
    """

    dd_series = []
    for s in series_list:
        tag_strs = [f"{k}:{v}" for k, v in sorted(s.tag_value_map.items())]
        dd_series.append(
            DDSeries(
                metric=s.dd_metric,
                points=[DDPoint(timestamp=ts, value=v) for ts, v in s.points],
                tags=tag_strs,
                metric_type=3,  # gauge
            )
        )
    return client.submit_series(dd_series)


def _es_request(
    *,
    es_endpoint: str,
    api_key: str,
    method: str,
    path: str,
    body: bytes | None,
    content_type: str = "application/json",
) -> dict:
    headers = {
        "Authorization": f"ApiKey {api_key}",
        "Content-Type": content_type,
    }
    req = urllib.request.Request(
        url=f"{es_endpoint.rstrip('/')}{path}",
        data=body,
        method=method,
        headers=headers,
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30.0, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:  # surface ES errors instead of swallowing
        body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"ES {method} {path} → {exc.code} {exc.reason}: {body_text[:500]}"
        ) from exc


def ensure_es_datastream(*, es_endpoint: str, api_key: str, data_stream: str) -> None:
    """Idempotent: create data stream + matching index template if missing."""

    template_name = f"parity-{data_stream}"
    template = {
        "index_patterns": [data_stream],
        "data_stream": {},
        "template": {
            "mappings": {
                "dynamic_templates": [
                    {
                        "metric_doubles": {
                            "match": "parity_*",
                            "mapping": {"type": "double"},
                        }
                    }
                ],
                "properties": {
                    "@timestamp": {"type": "date"},
                    "host.name": {"type": "keyword"},
                    "service.name": {"type": "keyword"},
                },
            },
        },
        "priority": 500,
    }
    _es_request(
        es_endpoint=es_endpoint,
        api_key=api_key,
        method="PUT",
        path=f"/_index_template/{template_name}",
        body=json.dumps(template).encode("utf-8"),
    )
    try:
        _es_request(
            es_endpoint=es_endpoint,
            api_key=api_key,
            method="PUT",
            path=f"/_data_stream/{data_stream}",
            body=None,
        )
    except RuntimeError as exc:
        # already-exists is fine — idempotent create.
        if "resource_already_exists" not in str(exc):
            raise


def push_to_elasticsearch(
    *,
    es_endpoint: str,
    api_key: str,
    data_stream: str,
    series_list: Iterable[ParitySeries],
) -> int:
    """Bulk-index one ES doc per (series, timestamp) pair.

    Each doc contains @timestamp, the metric field (es_field), and the
    mapped tag fields. Returns the count of docs indexed.
    """

    body_parts: list[str] = []
    count = 0
    for s in series_list:
        for ts, value in s.points:
            doc: dict = {
                "@timestamp": _iso8601(ts),
                s.es_field: value,
            }
            for field, value_str in s.es_tag_map.items():
                _set_dotted(doc, field, value_str)
            body_parts.append(json.dumps({"create": {"_index": data_stream}}))
            body_parts.append(json.dumps(doc))
            count += 1
    body = ("\n".join(body_parts) + "\n").encode("utf-8")
    result = _es_request(
        es_endpoint=es_endpoint,
        api_key=api_key,
        method="POST",
        path="/_bulk?refresh=wait_for",
        body=body,
        content_type="application/x-ndjson",
    )
    errors = [
        item for item in result.get("items", [])
        if not (200 <= next(iter(item.values())).get("status", 500) < 300)
    ]
    if errors:
        raise RuntimeError(f"ES bulk had {len(errors)} errors: {errors[:3]}")
    return count


def _iso8601(unix_seconds: int) -> str:
    """RFC 3339 timestamp in UTC, second precision."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(unix_seconds))


def _set_dotted(doc: dict, field_path: str, value: str) -> None:
    """Set doc[a.b.c] = value, creating nested dicts as needed."""

    parts = field_path.split(".")
    node = doc
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


# ---------------------------------------------------------------------------
# Common synthetic value generators
# ---------------------------------------------------------------------------


def linear_ramp(start: float, slope_per_step: float):
    """Returns f(idx, ts) = start + idx*slope."""

    def _fn(idx: int, ts: int) -> float:
        return start + slope_per_step * idx
    return _fn


def sine_wave(amplitude: float, period_seconds: int, offset: float = 0.0):
    """Returns f(idx, ts) = offset + amplitude * sin(2*pi*ts/period)."""

    def _fn(idx: int, ts: int) -> float:
        return offset + amplitude * math.sin(2 * math.pi * ts / period_seconds)
    return _fn


def constant(value: float):
    """Returns f(idx, ts) = value."""

    def _fn(idx: int, ts: int) -> float:
        return value
    return _fn
