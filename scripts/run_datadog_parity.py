#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""DD↔ES parity test orchestrator.

End-to-end flow:

1. Seed deterministic synthetic data into both Datadog (/api/v2/series)
   and Elasticsearch (bulk index into metrics-parity.test-default).
2. Wait for DD ingestion to settle.
3. For each test case, run the DD query against /api/v1/query and the
   translated ES|QL against /_query.
4. Normalize both responses into Series, diff with tolerance, classify.
5. Write parity-rig/datadog/parity_report.json and a markdown summary.

Credentials are loaded from env (DD_API_KEY, DD_APP_KEY, DD_SITE for
Datadog; ELASTICSEARCH_ENDPOINT, KEY for Elastic), which is how
datadog_creds.env and serverless_creds.env get sourced.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE  # noqa: E402
from observability_migration.adapters.source.datadog.models import (  # noqa: E402
    NormalizedWidget,
    WidgetFormula,
    WidgetQuery,
)
from observability_migration.adapters.source.datadog.parity.dd_client import DDClient  # noqa: E402
from observability_migration.adapters.source.datadog.parity.diff import (  # noqa: E402
    diff_series,
    normalize_dd_response,
    normalize_esql_response,
    run_esql,
    verdict_for,
)
from observability_migration.adapters.source.datadog.parity.seeder import (  # noqa: E402
    constant,
    ensure_es_datastream,
    generate_series,
    push_to_datadog,
    push_to_elasticsearch,
)
from observability_migration.adapters.source.datadog.planner import plan_widget  # noqa: E402
from observability_migration.adapters.source.datadog.query_parser import parse_metric_query  # noqa: E402
from observability_migration.adapters.source.datadog.translate import translate_widget  # noqa: E402

OUTPUT_DIR = REPO_ROOT / "parity-rig" / "datadog"
DATA_STREAM = "metrics-parity.test-default"


def _env(name: str, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise SystemExit(f"ERROR: ${name} is not set. Source datadog_creds.env and serverless_creds.env first.")
    return value


def _translate_dd_query(dd_query: str, *, widget_type: str = "timeseries") -> str:
    """Run the translation pipeline on a single DD query string."""

    mq = parse_metric_query(dd_query)
    wq = WidgetQuery(
        name="query1",
        data_source="metrics",
        raw_query=dd_query,
        metric_query=mq,
        query_type="metric",
    )
    widget = NormalizedWidget(
        id="parity-1",
        widget_type=widget_type,
        title="parity",
        queries=[wq],
        formulas=[WidgetFormula(raw="query1")],
    )
    result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
    if not result.esql_query:
        raise RuntimeError(f"translation produced no ES|QL for {dd_query!r}: {result.warnings}")
    return result.esql_query


def _instantiate_esql(query: str, *, start_unix: int, end_unix: int) -> str:
    """Replace ?_tstart / ?_tend placeholders with concrete bounds and
    swap the data-view glob for our seed data stream."""

    import datetime

    def _iso(unix_ts: int) -> str:
        return datetime.datetime.fromtimestamp(unix_ts, tz=datetime.UTC).isoformat().replace("+00:00", "Z")

    out = query
    out = out.replace("?_tstart", f'"{_iso(start_unix)}"')
    out = out.replace("?_tend", f'"{_iso(end_unix)}"')
    out = out.replace("FROM metrics-*", f"FROM {DATA_STREAM}")
    return out


def _run_case(
    *,
    case: dict[str, Any],
    dd: DDClient,
    es_endpoint: str,
    es_key: str,
    start_unix: int,
    end_unix: int,
) -> dict[str, Any]:
    """Run a single parity case and return a result row."""

    title = case["title"]
    dd_query = case["dd_query"]
    es_query = _instantiate_esql(
        _translate_dd_query(dd_query, widget_type=case.get("widget_type", "timeseries")),
        start_unix=start_unix,
        end_unix=end_unix,
    )

    dd_resp = dd.query_timeseries(query=dd_query, from_ts=start_unix, to_ts=end_unix)
    dd_series = normalize_dd_response(dd_resp, tag_remap=OTEL_PROFILE.tag_map)

    es_resp = run_esql(es_endpoint=es_endpoint, api_key=es_key, query=es_query)
    es_series = normalize_esql_response(
        es_resp,
        value_col=case.get("es_value_col", "query1"),
        group_cols=case.get("es_group_cols") or [],
    )

    max_rel, max_abs, only_in_dd, only_in_es = diff_series(dd_series, es_series)
    verdict, note = verdict_for(
        max_rel=max_rel, max_abs=max_abs,
        only_in_dd=only_in_dd, only_in_es=only_in_es,
    )
    return {
        "title": title,
        "dd_query": dd_query,
        "es_query": es_query,
        "dd_series_count": len(dd_series),
        "es_series_count": len(es_series),
        "matched_tag_keys": len(set(s.tag_key for s in dd_series) & set(s.tag_key for s in es_series)),
        "only_in_dd": only_in_dd,
        "only_in_es": only_in_es,
        "max_relative_error": round(max_rel, 6),
        "max_absolute_error": round(max_abs, 6),
        "verdict": verdict,
        "note": note,
    }


def _build_cases(start_unix: int, end_unix: int, step_seconds: int) -> tuple[list, list[dict[str, Any]]]:
    """Construct the synthetic series and the parity test cases that
    will exercise them.

    Returns (series_list, cases).
    """

    series = []
    # Use distinct metric names per case so cross-case data doesn't bleed
    # into each other's aggregations. Use constant values so AVG is
    # bucket-size invariant (DD and ES use different default bucket sizes).
    # Case 1: single host, gauge filtered by host — avg matches.
    series.append(generate_series(
        dd_metric="parity.gauge1", es_field="parity_gauge1",
        tags={"host": "h1"}, es_tag_fields={"host.name": "h1"},
        start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
        value_fn=constant(42.0),
    ))
    # Case 2: avg by host, two hosts with distinct constant values.
    for host, value in [("h1", 30.0), ("h2", 60.0)]:
        series.append(generate_series(
            dd_metric="parity.gauge2", es_field="parity_gauge2",
            tags={"host": host}, es_tag_fields={"host.name": host},
            start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
            value_fn=constant(value),
        ))
    # Case 3: max by service, two services with distinct constant values.
    for service, value in [("web", 100.0), ("api", 200.0)]:
        series.append(generate_series(
            dd_metric="parity.gauge3", es_field="parity_gauge3",
            tags={"service": service}, es_tag_fields={"service.name": service},
            start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
            value_fn=constant(value),
        ))

    cases = [
        {
            "title": "single-series avg gauge with tag filter",
            "dd_query": "avg:parity.gauge1{host:h1}",
        },
        {
            "title": "avg by host group-by",
            "dd_query": "avg:parity.gauge2{*} by {host}",
            "es_group_cols": ["host.name"],
        },
        {
            "title": "max by service group-by",
            "dd_query": "max:parity.gauge3{*} by {service}",
            "es_group_cols": ["service.name"],
        },
    ]
    return series, cases


def main() -> int:
    dd_api_key = _env("DD_API_KEY")
    dd_app_key = _env("DD_APP_KEY", required=False)
    dd_site = _env("DD_SITE", required=False, default="datadoghq.com")
    es_endpoint = _env("ELASTICSEARCH_ENDPOINT")
    es_key = _env("KEY")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1h window ending 60s ago (DD requires a small replay buffer).
    now = int(time.time()) - 60
    start_unix = now - 60 * 60
    end_unix = now
    step_seconds = 60

    print(f"Window: {start_unix} → {end_unix} (1h, {step_seconds}s steps)")

    series, cases = _build_cases(start_unix, end_unix, step_seconds)
    total_points = sum(len(s.points) for s in series)
    print(f"Generated {len(series)} series, {total_points} total points")

    print("Seeding Datadog…")
    dd = DDClient(api_key=dd_api_key, app_key=dd_app_key, site=dd_site)
    dd_resp = push_to_datadog(dd, series)
    print(f"  DD response: {json.dumps(dd_resp)[:200]}")

    print(f"Ensuring ES data stream {DATA_STREAM} exists…")
    ensure_es_datastream(es_endpoint=es_endpoint, api_key=es_key, data_stream=DATA_STREAM)

    print("Seeding Elasticsearch…")
    n = push_to_elasticsearch(
        es_endpoint=es_endpoint, api_key=es_key,
        data_stream=DATA_STREAM, series_list=series,
    )
    print(f"  ES indexed: {n} docs")

    settle = int(os.environ.get("DD_SETTLE_SECONDS", "45"))
    print(f"Waiting {settle}s for DD ingestion…")
    dd.wait_for_ingestion(settle_seconds=settle)

    print(f"\nRunning {len(cases)} parity case(s)…")
    rows: list[dict[str, Any]] = []
    for case in cases:
        try:
            row = _run_case(
                case=case, dd=dd,
                es_endpoint=es_endpoint, es_key=es_key,
                start_unix=start_unix, end_unix=end_unix,
            )
        except Exception as exc:
            row = {
                "title": case["title"],
                "dd_query": case["dd_query"],
                "es_query": "",
                "verdict": "ERROR",
                "note": str(exc)[:300],
            }
        rows.append(row)
        v = row.get("verdict", "?")
        n = row.get("note", "")
        print(f"  [{v}] {row['title']}: {n}")

    out = {
        "window": {"start": start_unix, "end": end_unix, "step_seconds": step_seconds},
        "data_stream": DATA_STREAM,
        "results": rows,
        "summary": {
            v: sum(1 for r in rows if r.get("verdict") == v)
            for v in {r.get("verdict", "?") for r in rows}
        },
    }
    json_path = OUTPUT_DIR / "parity_report.json"
    json_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {json_path.relative_to(REPO_ROOT)}")
    return 0 if all(r.get("verdict") in {"STRICT_PASS", "FUZZY_PASS"} for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
