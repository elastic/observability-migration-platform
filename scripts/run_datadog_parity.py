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
    push_distribution_to_datadog,
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

    return _translate_widget_spec(
        queries=[("query1", dd_query)],
        formula_raw="query1",
        widget_type=widget_type,
    )


def _translate_widget_spec(
    *,
    queries: list[tuple[str, str]],
    formula_raw: str,
    widget_type: str = "timeseries",
) -> str:
    """Build a NormalizedWidget with the given queries + formula and
    return the translated ES|QL.

    queries: list of (query_name, dd_query_string).
    formula_raw: e.g. "query1", "query1 / query2", "rate(query1)".
    """

    from observability_migration.adapters.source.datadog.query_parser import parse_formula

    widget_queries = []
    for name, raw in queries:
        widget_queries.append(
            WidgetQuery(
                name=name,
                data_source="metrics",
                raw_query=raw,
                metric_query=parse_metric_query(raw),
                query_type="metric",
            )
        )
    wf = WidgetFormula(raw=formula_raw)
    wf.expression = parse_formula(formula_raw)
    widget = NormalizedWidget(
        id="parity-1",
        widget_type=widget_type,
        title="parity",
        queries=widget_queries,
        formulas=[wf],
    )
    result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
    if not result.esql_query:
        raise RuntimeError(
            f"translation produced no ES|QL for queries={queries} formula={formula_raw!r}: "
            f"status={result.status} reasons={result.reasons}"
        )
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
    if "es_widget" in case:
        spec = case["es_widget"]
        raw_es = _translate_widget_spec(
            queries=spec["queries"],
            formula_raw=spec["formula"],
            widget_type=spec.get("widget_type", "timeseries"),
        )
    else:
        raw_es = _translate_dd_query(
            dd_query, widget_type=case.get("widget_type", "timeseries"),
        )
    es_query = _instantiate_esql(raw_es, start_unix=start_unix, end_unix=end_unix)

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
    known_gap = case.get("known_gap")
    if known_gap and verdict not in {"STRICT_PASS", "FUZZY_PASS"}:
        verdict = "KNOWN_GAP"
        note = known_gap
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


def _build_distribution_series(start_unix: int, end_unix: int, step_seconds: int) -> list:
    """Distribution-typed series for percentile cases. DD requires
    distribution submission (not gauge) for p50/p75/p90/p95/p99 to
    return any data."""

    series = []
    for host, value in [("h1", 30.0), ("h2", 60.0)]:
        series.append(generate_series(
            dd_metric="parity.dist1", es_field="parity_dist1",
            tags={"host": host}, es_tag_fields={"host.name": host},
            start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
            value_fn=constant(value),
        ))
    return series


def _build_cases(start_unix: int, end_unix: int, step_seconds: int) -> tuple[list, list[dict[str, Any]]]:
    """Construct the synthetic series and the parity test cases that
    will exercise them.

    Returns (series_list, cases).
    """

    series = []
    # Use distinct metric names per case so cross-case data doesn't bleed
    # into each other's aggregations. Use constant values so AVG/MIN/MAX
    # are bucket-size invariant (DD and ES use different default bucket
    # sizes).

    # gauge1: single host, gauge filtered by host — avg matches.
    series.append(generate_series(
        dd_metric="parity.gauge1", es_field="parity_gauge1",
        tags={"host": "h1"}, es_tag_fields={"host.name": "h1"},
        start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
        value_fn=constant(42.0),
    ))
    # gauge2: two hosts with distinct constant values — for avg/min by host.
    for host, value in [("h1", 30.0), ("h2", 60.0)]:
        series.append(generate_series(
            dd_metric="parity.gauge2", es_field="parity_gauge2",
            tags={"host": host}, es_tag_fields={"host.name": host},
            start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
            value_fn=constant(value),
        ))
    # gauge3: two services with distinct constant values — for max by service.
    for service, value in [("web", 100.0), ("api", 200.0)]:
        series.append(generate_series(
            dd_metric="parity.gauge3", es_field="parity_gauge3",
            tags={"service": service}, es_tag_fields={"service.name": service},
            start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
            value_fn=constant(value),
        ))
    # gauge4: combined host x service grid for multi-dim group-by and
    # multi-tag AND filter; constant values per combination.
    for host, service, value in [
        ("h1", "web", 11.0),
        ("h1", "api", 12.0),
        ("h2", "web", 21.0),
        ("h2", "api", 22.0),
    ]:
        series.append(generate_series(
            dd_metric="parity.gauge4", es_field="parity_gauge4",
            tags={"host": host, "service": service},
            es_tag_fields={"host.name": host, "service.name": service},
            start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
            value_fn=constant(value),
        ))
    # gauge5: includes a "dev" env tag we will exclude with NOT filter.
    for env, value in [("prod", 70.0), ("dev", 7.0)]:
        series.append(generate_series(
            dd_metric="parity.gauge5", es_field="parity_gauge5",
            tags={"env": env, "host": "h1"},
            es_tag_fields={"deployment.environment": env, "host.name": "h1"},
            start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
            value_fn=constant(value),
        ))
    # rate-counter: a counter-like metric incrementing at 100 units per
    # 60-second step. rate(sum:counter) ≈ 100/60 = 1.6667/s regardless of
    # bucket size, because the increment density per unit time is constant.
    series.append(generate_series(
        dd_metric="parity.counter", es_field="parity_counter",
        tags={"host": "h1"}, es_tag_fields={"host.name": "h1"},
        start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
        value_fn=constant(100.0),
    ))
    # ratio-pair: numerator 50, denominator 10 ⇒ ratio = 5.0.
    series.append(generate_series(
        dd_metric="parity.numerator", es_field="parity_numerator",
        tags={"host": "h1"}, es_tag_fields={"host.name": "h1"},
        start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
        value_fn=constant(50.0),
    ))
    series.append(generate_series(
        dd_metric="parity.denominator", es_field="parity_denominator",
        tags={"host": "h1"}, es_tag_fields={"host.name": "h1"},
        start_ts=start_unix, end_ts=end_unix, interval_seconds=step_seconds,
        value_fn=constant(10.0),
    ))

    cases = [
        # --- single-query aggregation parity -----------------------------
        {
            "title": "avg with tag filter (no group-by)",
            "dd_query": "avg:parity.gauge1{host:h1}",
        },
        {
            "title": "avg by host",
            "dd_query": "avg:parity.gauge2{*} by {host}",
            "es_group_cols": ["host.name"],
        },
        {
            "title": "min by host",
            "dd_query": "min:parity.gauge2{*} by {host}",
            "es_group_cols": ["host.name"],
        },
        {
            "title": "max by service",
            "dd_query": "max:parity.gauge3{*} by {service}",
            "es_group_cols": ["service.name"],
        },
        {
            "title": "p95 by host on distribution metric",
            "dd_query": "p95:parity.dist1{*} by {host}",
            "es_group_cols": ["host.name"],
            "known_gap": "DD distribution submission via /api/v1/"
                         "distribution_points alone doesn't make `p95:` "
                         "queryable — DD requires the metric to also be "
                         "registered as distribution-typed at the org "
                         "metadata level (manual one-time setup per "
                         "metric). The ES PERCENTILE(metric, 95) "
                         "translation is correct in shape; validating "
                         "it end-to-end would require either pre-"
                         "registering the metric type or querying via "
                         "the distribution-derived suffix "
                         "(metric.95percentile).",
        },

        # --- filter shape parity ----------------------------------------
        {
            "title": "AND of two tag filters",
            "dd_query": "avg:parity.gauge4{host:h1,service:web}",
            "rel_fuzzy_only": False,
        },
        {
            "title": "NOT filter excludes dev",
            "dd_query": "avg:parity.gauge5{!env:dev}",
        },

        # --- multi-dimension group-by ----------------------------------
        {
            "title": "avg by {host, service}",
            "dd_query": "avg:parity.gauge4{*} by {host,service}",
            "es_group_cols": ["host.name", "service.name"],
        },

        # --- formula coverage ------------------------------------------
        {
            "title": "rate() formula on constant-rate counter",
            # DD query expressing rate() at the formula level: we send the
            # raw aggregation to DD and ask for rate via the formula in
            # the ES translation. DD's /api/v1/query accepts inline
            # rate() syntax over the metric query, so the DD side matches.
            "dd_query": "rate(sum:parity.counter{host:h1})",
            "es_widget": {
                "queries": [("query1", "sum:parity.counter{host:h1}")],
                "formula": "rate(query1)",
            },
            "es_value_col": "rate_query1",
            # rate() now uses FIRST/LAST within each bucket to compute the
            # true derivative (last - first) / bucket_span_seconds.
            # Expected to match DD exactly for constant counters.
        },
        {
            "title": "query1 / query2 ratio formula",
            "dd_query": "avg:parity.numerator{host:h1} / avg:parity.denominator{host:h1}",
            "es_widget": {
                "queries": [
                    ("query1", "avg:parity.numerator{host:h1}"),
                    ("query2", "avg:parity.denominator{host:h1}"),
                ],
                "formula": "query1 / query2",
            },
            "es_value_col": "query1_query2",
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
    dist_series = _build_distribution_series(start_unix, end_unix, step_seconds)
    total_points = sum(len(s.points) for s in series)
    print(f"Generated {len(series)} gauge series + {len(dist_series)} distribution series, {total_points} total points")

    print("Seeding Datadog (gauge series via /api/v2/series)…")
    dd = DDClient(api_key=dd_api_key, app_key=dd_app_key, site=dd_site)
    dd_resp = push_to_datadog(dd, series)
    print(f"  DD gauge response: {json.dumps(dd_resp)[:200]}")
    print("Seeding Datadog (distribution series via /api/v1/distribution_points)…")
    dist_resp = push_distribution_to_datadog(dd, dist_series)
    print(f"  DD distribution response: {json.dumps(dist_resp)[:200]}")

    print(f"Ensuring ES data stream {DATA_STREAM} exists…")
    ensure_es_datastream(es_endpoint=es_endpoint, api_key=es_key, data_stream=DATA_STREAM)

    print("Seeding Elasticsearch…")
    n = push_to_elasticsearch(
        es_endpoint=es_endpoint, api_key=es_key,
        data_stream=DATA_STREAM, series_list=list(series) + list(dist_series),
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
    ok = {"STRICT_PASS", "FUZZY_PASS", "KNOWN_GAP"}
    return 0 if all(r.get("verdict") in ok for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
