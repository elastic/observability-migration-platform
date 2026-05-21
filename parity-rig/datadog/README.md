# Datadog ↔ Elasticsearch Parity Rig

End-to-end correctness harness for the Datadog → Kibana translation
pipeline. Seeds the **same** synthetic metric data into both Datadog
and Elasticsearch, then runs each test case's source Datadog query
against DD and the translated ES|QL against ES, and diffs the values
returned by both stores.

## What it proves

For a parity case to pass `STRICT_PASS` (max relative error ≤ 1 %), the
translation pipeline must have:

- Preserved the aggregation function (`avg` → `AVG`, `sum` → `SUM`,
  `max` → `MAX`).
- Preserved the metric identity (`parity.gauge1` → `parity_gauge1`).
- Preserved the group-by dimensions (`by {host}` → `BY host.name`,
  with OTel tag-map applied).
- Preserved the tag filter semantics (`{host:h1}` →
  `WHERE host.name == "h1"`).

## Architecture

```
                ┌─────────────────────┐
                │  parity test cases  │
                └──────────┬──────────┘
                           │
                ┌──────────▼──────────────────┐
                │  Synthetic data generator   │
                │  (one ParitySeries per      │
                │   logical metric+tag-set)   │
                └─────┬──────────────┬────────┘
                      │              │
                      ▼              ▼
            ┌──────────────┐  ┌──────────────────────┐
            │  Datadog     │  │  Elasticsearch        │
            │  /api/v2/    │  │  bulk index into      │
            │   series     │  │  metrics-parity.test- │
            │              │  │  default              │
            └──────┬───────┘  └────────┬─────────────┘
                   │ wait 45s          │
                   ▼                   │
            ┌──────────────┐  ┌────────▼─────────────┐
            │  /api/v1/    │  │  POST /_query        │
            │   query      │  │   (ES|QL)            │
            │  (DD)        │  │  with translated     │
            │              │  │  query               │
            └──────┬───────┘  └────────┬─────────────┘
                   │                   │
                   └────────┬──────────┘
                            ▼
                  ┌────────────────────┐
                  │  diff_series:      │
                  │  align by tag-set, │
                  │  diff per-point    │
                  │  with tolerance    │
                  └─────────┬──────────┘
                            ▼
                ┌────────────────────────┐
                │  parity_report.json    │
                │  verdict per case      │
                └────────────────────────┘
```

## Verdicts

| Verdict | Meaning |
|---|---|
| `STRICT_PASS` | max relative error ≤ 1 % |
| `FUZZY_PASS` | max relative error ≤ 5 % (typical for bucket-boundary drift) |
| `KNOWN_GAP` | case has a documented `known_gap` reason; would have failed without it |
| `SHAPE_MISMATCH` | series tag-sets don't align between DD and ES |
| `FAIL_DIVERGENT` | values diverge beyond fuzzy tolerance |
| `ERROR` | exception during seeding or querying |

A run exits with status 0 if every case is `STRICT_PASS`, `FUZZY_PASS`, or `KNOWN_GAP`. Anything else exits non-zero.

## Current coverage

The default suite ships 10 cases:

**Single-query aggregation parity** — 4 STRICT_PASS:
- `avg` with tag filter
- `avg` by group-by
- `min` by group-by
- `max` by group-by

**Filter shapes** — 2 STRICT_PASS:
- AND of two tag filters (`{host:h1,service:web}`)
- NOT filter (`{!env:dev}`) — verifies OTel tag mapping translates `env` → `deployment.environment`

**Multi-dimension group-by** — 1 STRICT_PASS:
- `avg by {host, service}`

**Formula coverage** — 1 STRICT_PASS:
- `query1 / query2` ratio formula across two AVG queries

**Documented gaps (KNOWN_GAP)** — 2:
- **`p95:` percentile**: DD's percentile aggregators require *distribution-typed* metric submission, not gauges. The ES|QL translation is correct in shape (`PERCENTILE(metric, 95)`) but cannot be validated end-to-end without distribution data. To verify percentiles, submit a distribution metric type with multiple values per timestamp.
- **`rate()` formula**: DD's `rate()` is `(value[t] − value[t-1]) / Δt` — a true derivative between adjacent samples. The Phase 1 translation emits `value / bucket_span_seconds`, which matches DD's `per_second()` semantics but **not** `rate()`. The widget is unblocked (no longer `not_feasible`) but produces different numbers than DD. A real fix needs ES|QL time-series aggregations (`metrics-*` TS mode) or a window/LAG construct that ES|QL doesn't yet expose in standard mode.

## Running

```bash
# 1. Source both credential files (gitignored)
#    datadog_creds.env: DD_API_KEY, DD_APP_KEY, DD_SITE
#    serverless_creds.env: ELASTICSEARCH_ENDPOINT, KEY

# 2. Run the rig
bash scripts/run_datadog_parity.sh

# Output: parity-rig/datadog/parity_report.json
```

## Adding test cases

Edit `_build_cases()` in `scripts/run_datadog_parity.py`. Each case
needs:

- One or more `generate_series(...)` entries with matching DD metric
  name (`parity.x`), ES field name (`parity_x`), and tag set.
- A test case dict with the DD query and (for group-by queries) the
  ES `es_group_cols`.

For values to compare strictly across bucket-size differences (DD ~60s,
ES|QL `BUCKET(@timestamp, 50, ...)` ~72s), use `constant(value)` from
`seeder.py` and prefer `avg`/`max`/`min` aggregations over `sum`.

## Known approximations

- **Bucket boundaries**: DD and ES|QL use independent bucketing
  algorithms. For non-constant series, expect FUZZY_PASS rather than
  STRICT_PASS.
- **rate() / diff()**: per-bucket rate semantics are approximated on
  the ES side as `value / bucket_span_seconds`. The parity verdict
  reflects this drift.
- **DD ingestion latency**: a 45-second `wait_for_ingestion()` settle
  gives DD enough time for synthetic points to land. Increase via
  `DD_SETTLE_SECONDS` if your tenant is slower.
