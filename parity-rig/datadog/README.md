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
| `SHAPE_MISMATCH` | series tag-sets don't align between DD and ES |
| `FAIL_DIVERGENT` | values diverge beyond fuzzy tolerance |
| `ERROR` | exception during seeding or querying |

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
