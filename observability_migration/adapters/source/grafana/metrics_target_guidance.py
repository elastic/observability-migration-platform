# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Operator guidance for Grafana metrics target selection (issue #284).

``obs-migrate`` moves asset definitions, not telemetry. Empty or ERROR panels
after a "successful" migrate are often an index/data-plane problem:

- **Migrate-first:** assets are pointed at a planned stream before dual-write.
- **Data-first:** telemetry already exists; migrate must pin a concrete stream
  instead of a mixed ``metrics-*`` wildcard.

This module turns those timelines into migrate-time warnings. It does not
auto-select a stream from Grafana datasource UIDs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

_WILDCARD_TOKENS = ("*", "?", ",")


def is_wildcard_index(pattern: str | None) -> bool:
    token = str(pattern or "").strip()
    return bool(token) and any(ch in token for ch in _WILDCARD_TOKENS)


def metrics_query_target(data_view: str | None, esql_index: str | None) -> str:
    for candidate in (esql_index, data_view):
        token = str(candidate or "").strip()
        if token:
            return token
    return "metrics-*"


def backend_family(stream_name: str | None) -> str:
    """Best-effort ingest-family label from a concrete metrics data-stream name."""
    name = str(stream_name or "").strip().lower()
    if not name:
        return "other"
    if "prometheus" in name or "prom" in name.split("-"):
        return "prometheus"
    if "datadog" in name or re.search(r"(^|[-.])dd([-.]|$)", name):
        return "datadog"
    if "otel" in name or "opentelemetry" in name:
        return "otel"
    if "generic" in name:
        return "generic"
    return "other"


@dataclass(frozen=True)
class MetricsTargetGuidance:
    query_index: str
    data_view: str
    messages: list[str] = field(default_factory=list)
    blocking: bool = False


def _unique_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        token = str(item or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def assess_metrics_target(
    *,
    data_view: str | None,
    esql_index: str | None,
    es_url: str | None,
    concrete_streams: Sequence[str] | None = None,
    tsdb_conflict_fields: Sequence[str] | None = None,
) -> MetricsTargetGuidance:
    """Return operator-facing messages for the chosen metrics target.

    Pure function — callers supply live stream candidates / conflict fields when
    ``--es-url`` discovery is available.
    """
    data_view_token = str(data_view or "").strip() or "metrics-*"
    esql_token = str(esql_index or "").strip()
    query_index = metrics_query_target(data_view_token, esql_token)
    es_configured = bool(str(es_url or "").strip())
    streams = _unique_preserve(concrete_streams or [])
    conflicts = _unique_preserve(tsdb_conflict_fields or [])
    messages: list[str] = []

    if not es_configured:
        messages.append(
            "Timeline: migrate-first (no --es-url). You are committing assets to "
            f"planned metrics target '{query_index}' before live field discovery. "
            "Empty panels until matching telemetry lands are expected — not a "
            "translator failure. Prefer a concrete stream name now (not a mixed "
            "wildcard). After ingest starts, re-run with --es-url/--es-api-key "
            "and --preflight against that same stream."
        )
        if is_wildcard_index(query_index):
            messages.append(
                f"Planned target '{query_index}' is a wildcard. On clusters that "
                "later host Prometheus + Datadog + OTel under metrics-*, validate/"
                "compare can look broken even when translation is fine. Set both "
                "--data-view and --esql-index to the concrete stream your ingest "
                "will create (example: metrics-prometheus-default)."
            )

    if esql_token and esql_token != data_view_token:
        messages.append(
            "UI bind vs query target differ: Kibana --data-view "
            f"'{data_view_token}' while metrics queries + discovery use "
            f"--esql-index '{esql_token}'. Native PROMQL and ES|QL both read "
            f"'{query_index}'. Prefer setting both flags to the same concrete "
            "stream unless you intentionally want a broader UI data view."
        )

    if es_configured and is_wildcard_index(query_index):
        if len(streams) >= 2 and len({backend_family(s) for s in streams}) >= 2:
            listed = ", ".join(streams[:8])
            more = "" if len(streams) <= 8 else f" (+{len(streams) - 8} more)"
            messages.append(
                f"Mixed metrics backends under wildcard '{query_index}': "
                f"{listed}{more}. Treat failures here as target/index readiness, "
                "not panel translator bugs. Pin both --data-view and --esql-index "
                "to one concrete stream (usually the Prometheus one for Grafana "
                "PromQL dashboards)."
            )
        elif len(streams) == 1:
            messages.append(
                f"Wildcard '{query_index}' currently resolves to '{streams[0]}'. "
                "Pin both --data-view and --esql-index to that concrete stream so "
                "migrate/validate/compare stay stable if more backends appear later."
            )
        elif not streams:
            messages.append(
                f"Wildcard '{query_index}' is set with --es-url, but no concrete "
                "data streams were discovered yet (migrate-first / empty target). "
                "Keep using a planned concrete stream name, or wait until ingest "
                "creates streams and re-run discovery."
            )

    if conflicts:
        sample = ", ".join(conflicts[:6])
        more = "" if len(conflicts) <= 6 else f" (+{len(conflicts) - 6} more)"
        messages.append(
            "TSDB metadata conflicts under the metrics target "
            f"({sample}{more}). Queries like TS on a mixed wildcard can fail with "
            "dimension/metric merge errors. This is index readiness — narrow to a "
            "clean concrete stream before trusting live_validate/compare."
        )

    return MetricsTargetGuidance(
        query_index=query_index,
        data_view=data_view_token,
        messages=messages,
        blocking=False,
    )


def print_metrics_target_guidance(guidance: MetricsTargetGuidance) -> None:
    if not guidance.messages:
        return
    print("\n" + "!" * 70)
    print("WARNING: metrics target / data-plane readiness")
    print("!" * 70)
    for message in guidance.messages:
        for line in message.splitlines() or [message]:
            print(f"  {line}")
    print(
        "  Operator rule: ingest path → concrete stream → set both "
        "--data-view and --esql-index → then migrate/verify."
    )


def tsdb_conflict_fields_from_field_cache(field_cache: dict | None) -> list[str]:
    """Return field names that carry both dimension and metric TSDB roles."""
    conflicts: list[str] = []
    if not isinstance(field_cache, dict):
        return conflicts
    for name, types in field_cache.items():
        if not isinstance(types, dict):
            continue
        has_dim = False
        has_met = False
        for meta in types.values():
            if not isinstance(meta, dict):
                continue
            if meta.get("time_series_dimension"):
                has_dim = True
            if meta.get("time_series_metric"):
                has_met = True
        if has_dim and has_met:
            conflicts.append(str(name))
    return sorted(conflicts)


__all__ = [
    "MetricsTargetGuidance",
    "assess_metrics_target",
    "backend_family",
    "is_wildcard_index",
    "metrics_query_target",
    "print_metrics_target_guidance",
    "tsdb_conflict_fields_from_field_cache",
]
