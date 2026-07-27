# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Operator guidance for Grafana metrics target selection (issue #284).

``obs-migrate`` moves asset definitions, not telemetry. Empty or ERROR panels
after a "successful" migrate are often an index/data-plane problem:

- **Migrate-first:** assets are pointed at a planned stream before dual-write.
- **Data-first:** telemetry already exists; migrate must pin a concrete stream
  instead of a mixed ``metrics-*`` wildcard.

This module turns those timelines into migrate-time messages. It does not
auto-select a stream from Grafana datasource UIDs.

Only a *risky* target speaks up. A run that already pins both flags to one
concrete stream has nothing to fix, and the generic "no ``--es-url``, so fields
are unverified" case is already covered by the field-discovery warning in
``core.reporting.report`` — repeating it here would train operators to skim
past both banners.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .panels import metrics_query_index

_WILDCARD_TOKENS = ("*", "?", ",")
_MAX_LISTED_STREAMS = 8
_MAX_LISTED_CONFLICTS = 6


def is_wildcard_index(pattern: str | None) -> bool:
    token = str(pattern or "").strip()
    return bool(token) and any(ch in token for ch in _WILDCARD_TOKENS)


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
    """Operator-facing findings about the chosen metrics target.

    ``warnings`` are footguns worth a banner; ``notes`` are informational
    consequences of a deliberate choice (e.g. deliberately binding the Kibana
    data view broader than the query target).
    """

    query_index: str
    data_view: str
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def messages(self) -> list[str]:
        return [*self.warnings, *self.notes]


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


def _listed(items: Sequence[str], limit: int) -> str:
    listed = ", ".join(items[:limit])
    if len(items) <= limit:
        return listed
    return f"{listed} (+{len(items) - limit} more)"


def assess_metrics_target(
    *,
    data_view: str | None,
    esql_index: str | None,
    es_url: str | None,
    concrete_streams: Sequence[str] | None = None,
    tsdb_conflict_fields: Sequence[str] | None = None,
    stream_discovery_error: str | None = None,
) -> MetricsTargetGuidance:
    """Return operator-facing findings for the chosen metrics target.

    Pure function — callers supply live stream candidates / conflict fields when
    ``--es-url`` discovery is available. ``stream_discovery_error`` lets a
    caller say "I could not list streams" so an unreadable target is not
    reported as an empty one.
    """
    data_view_token = str(data_view or "").strip() or "metrics-*"
    esql_token = str(esql_index or "").strip()
    query_index = metrics_query_index(data_view_token, esql_token)
    es_configured = bool(str(es_url or "").strip())
    streams = _unique_preserve(concrete_streams or [])
    conflicts = _unique_preserve(tsdb_conflict_fields or [])
    discovery_error = str(stream_discovery_error or "").strip()
    query_is_wildcard = is_wildcard_index(query_index)
    warnings: list[str] = []
    notes: list[str] = []

    if not es_configured and query_is_wildcard:
        warnings.append(
            f"Migrate-first (no --es-url) against wildcard '{query_index}'. You "
            "are committing assets to a planned metrics target before live "
            "field discovery, and a wildcard that later spans Prometheus + "
            "Datadog + OTel makes validate/compare look broken even when the "
            "translation is fine. Set both --data-view and --esql-index to the "
            "concrete stream your ingest will create (example: "
            "metrics-prometheus-default). Empty panels until that telemetry "
            "lands are expected — not a translator failure."
        )

    if esql_token and esql_token != data_view_token:
        if query_is_wildcard and not is_wildcard_index(data_view_token):
            warnings.append(
                f"Query target is broader than the UI bind: --data-view "
                f"'{data_view_token}' is a concrete stream, but native PROMQL "
                f"and ES|QL both read --esql-index '{esql_token}'. The Kibana "
                "data view looks pinned while every panel query still spans the "
                "wildcard. Set --esql-index to the same concrete stream."
            )
        else:
            notes.append(
                f"Metrics queries and schema discovery read --esql-index "
                f"'{esql_token}'; --data-view '{data_view_token}' only binds the "
                "Kibana data view and controls. This is supported — no action "
                "needed if the broader UI bind is deliberate."
            )

    if es_configured and query_is_wildcard:
        if streams:
            families = {backend_family(stream) for stream in streams}
            if len(families) >= 2:
                warnings.append(
                    f"Mixed metrics backends under wildcard '{query_index}': "
                    f"{_listed(streams, _MAX_LISTED_STREAMS)}. Treat failures "
                    "here as target/index readiness, not panel translator bugs. "
                    "Pin both --data-view and --esql-index to one concrete "
                    "stream (usually the Prometheus one for Grafana PromQL "
                    "dashboards)."
                )
            elif len(streams) == 1:
                warnings.append(
                    f"Wildcard '{query_index}' currently resolves to "
                    f"'{streams[0]}'. Pin both --data-view and --esql-index to "
                    "that concrete stream so migrate/validate/compare stay "
                    "stable if more backends appear later."
                )
            else:
                warnings.append(
                    f"Wildcard '{query_index}' spans {len(streams)} streams: "
                    f"{_listed(streams, _MAX_LISTED_STREAMS)}. Even one backend "
                    "family can split metrics across streams, so queries may "
                    "read more than you expect. Pin both --data-view and "
                    "--esql-index to the concrete stream your dashboards read."
                )
        elif discovery_error:
            warnings.append(
                f"Could not list concrete streams under wildcard "
                f"'{query_index}': {discovery_error}. The metrics target is "
                "unverified — this is not evidence that the target is empty. "
                "Check the --es-url/--es-api-key privileges for _resolve/index, "
                "then re-run before trusting live_validate/compare."
            )
        else:
            warnings.append(
                f"Wildcard '{query_index}' is set with --es-url, but the target "
                "has no concrete data streams yet (nothing has been ingested "
                "under this pattern). Point both --data-view and --esql-index at "
                "the stream your ingest will create, or wait until ingest "
                "creates it and re-run discovery."
            )

    if conflicts:
        warnings.append(
            "TSDB metadata conflicts under the metrics target "
            f"({_listed(conflicts, _MAX_LISTED_CONFLICTS)}). Queries like TS on "
            "a mixed wildcard can fail with dimension/metric merge errors. This "
            "is index readiness — narrow to a clean concrete stream before "
            "trusting live_validate/compare."
        )

    return MetricsTargetGuidance(
        query_index=query_index,
        data_view=data_view_token,
        warnings=warnings,
        notes=notes,
    )


def print_metrics_target_guidance(guidance: MetricsTargetGuidance) -> None:
    if guidance.warnings:
        print("\n" + "!" * 70)
        print("WARNING: metrics target / data-plane readiness")
        print("!" * 70)
        for message in guidance.warnings:
            print(f"  {message}")
        print(
            "  Operator rule: ingest path → concrete stream → set both "
            "--data-view and --esql-index → then migrate/verify."
        )
    for message in guidance.notes:
        print(f"\n  Metrics target: {message}")


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
    "print_metrics_target_guidance",
    "tsdb_conflict_fields_from_field_cache",
]
