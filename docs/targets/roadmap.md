# Roadmap

Tracked improvements — deferred items awaiting infrastructure, external data, or deliberate design decisions; and completed items with live-test instructions.

---

## ✅ Native `/_prometheus` endpoint schema profile

**Status:** Implemented in `schema.py` (2026-05-15)  
**File:** `observability_migration/adapters/source/grafana/schema.py`

### What was wrong

When a live `SchemaResolver` connected to a `metrics-*.prometheus-*` index (Elastic's native
`/_prometheus/api/v1/write` endpoint), field resolution fell back to bare metric names and OTel
candidates — both wrong for this layout:

| What was emitted | What ES actually stored |
|---|---|
| `http_requests_total` | `metrics.http_requests_total` |
| `service.instance.id` | `labels.instance` |
| `is_counter = False` for all fields | `time_series_metric: counter` on `_total` metrics |

### What was fixed

Added a third schema profile `"prometheus_native"` detected by the presence of both `metrics.*`
and `labels.*` fields in the field capabilities response:

- `resolve_metric_field` → returns `f"metrics.{metric_name}"` (no suffix variants)
- `resolve_label` → returns `f"labels.{label}"` unconditionally (OTel candidates skipped)
- `is_counter` → checks capability of `metrics.<name>` field for non-suffix metrics
- `_build_discovered_mappings` → skipped entirely (native indices have no OTel fields)
- Fleet `prometheus_remote_write` profile takes priority when both patterns coexist

### How to live-test

```bash
# Requires parity-rig to be running with express-app metrics flowing to
# metrics-express.prometheus-parity via /_prometheus remote write.
bash scripts/run_parity_native_profile.sh --window-minutes 15
```

`run_parity_native_profile.sh` migrates the express-prometheus dashboard with
`--no-native-promql` (forcing `TS`/`FROM` ES|QL), then runs the parity harness which
executes translated queries against the native endpoint and diffs against Prometheus.
Expected: all counter panels (`RATE`) and gauge panels (`AVG`) return `STRICT_PASS` (≤1% error).

### Field naming reference

| Prometheus element | Native endpoint ES field | Fleet `prometheus.remote_write` field |
|---|---|---|
| metric value (`__name__`) | `metrics.<metric_name>` | `prometheus.<metric>.{counter,value,rate}` |
| label `<name>` | `labels.<name>` | `prometheus.labels.<name>` |
| counter type | `time_series_metric: counter` (auto by name suffix) | `.counter` leaf field |

---

## Datadog translator: FROM → TS promotion for counter metrics

**Status:** Deferred — requires Datadog metric metadata  
**File:** `observability_migration/adapters/source/datadog/translate.py`  
**Function:** `_build_timeseries_esql()`

### Background: FROM vs TS in ES|QL

Sourced from Elastic official documentation — verified against docs, **not yet live-tested against a running cluster**.

#### `FROM` (general-purpose source command)

- Works on any index, TSDB or otherwise.
- Time filter must be explicit: `@timestamp >= ?_tstart AND @timestamp < ?_tend`.
- Bucketing via `BUCKET(@timestamp, 50, ?_tstart, ?_tend)`.
- Aggregations: `AVG()`, `SUM()`, `MIN()`, `MAX()`, `COUNT()` — standard non-time-series functions.
- **No access to time series aggregation functions** (`RATE`, `AVG_OVER_TIME`, etc.).
- Produces correct results for gauge metrics (point-in-time values that can go up or down).

#### `TS` (time series source command)

- **Targets TSDB indices only** — will error or produce no results on regular indices.
  ([ES|QL TS command reference](https://www.elastic.co/docs/reference/query-languages/esql/commands/ts))
- Syntax: `TS index_pattern [METADATA fields]`
- Time range is implicit — driven by Kibana's time picker, not `?_tstart`/`?_tend` params.
- Bucketing via `TBUCKET(@timestamp, interval)` — no explicit start/end needed.
- Unlocks the full suite of **time series aggregation functions** (GA in 9.4+):
  `RATE`, `IRATE`, `INCREASE`, `AVG_OVER_TIME`, `SUM_OVER_TIME`, `MAX_OVER_TIME`,
  `MIN_OVER_TIME`, `DELTA`, `IDELTA`, `DERIV`, `COUNT_OVER_TIME`,
  `COUNT_DISTINCT_OVER_TIME`, `FIRST_OVER_TIME`, `LAST_OVER_TIME`,
  `PERCENTILE_OVER_TIME`, `STDDEV_OVER_TIME`, `VARIANCE_OVER_TIME`, `ABSENT_OVER_TIME`, `PRESENT_OVER_TIME`.
  ([Time series aggregation functions reference](https://www.elastic.co/docs/reference/query-languages/esql/functions-operators/time-series-aggregation-functions))
- Uses a **two-tier aggregation model**: inner function runs per time series, outer function
  aggregates across groups (e.g. `STATS SUM(RATE(requests)) BY TBUCKET(1 hour), host`).
- If no inner function is specified, `LAST_OVER_TIME()` is used implicitly.
- Performance: orders of magnitude faster than `FROM` on TSDB-backed data.

#### `RATE()` — counter metrics only

- Signature: `RATE(field [, window])` → `double`
- **Only works on `counter_double`, `counter_integer`, `counter_long` field types.**
  ([RATE function reference](https://www.elastic.co/docs/reference/query-languages/esql/functions-operators/time-series-aggregation-functions/rate))
- Calculates per-second average rate of increase; handles counter resets (e.g. service restarts).
- **Cannot be used with gauge fields** — the field must be mapped with `time_series_metric: counter`.
- Only valid inside `STATS` under a `TS` source command.

#### Known limitation: `index.look_back_time` is capped at 7d

([Time series index settings reference](https://www.elastic.co/docs/reference/elasticsearch/index-settings/time-series))

| Setting | Default | Min | Max |
|---|---|---|---|
| `index.look_back_time` | `2h` | `1m` | **`7d`** |
| `index.look_ahead_time` | `30m` | `1m` | `2h` |

**Implication for seeding**: A TSDB backing index created today will only accept documents
with `@timestamp >= now - 7d`. Documents older than 7 days are **rejected at ingest time**.
This means Phase 2 historical seeding (14d of data) cannot be indexed into TSDB streams.
Panels that compare `NOW()-7d` vs `NOW()-14d` (e.g. the nginx "Change in overall requests per
second" panel) **cannot be fully validated against TSDB-backed data** without a different strategy.

> **Needs live testing**: Verify that ES actually rejects documents outside the time window
> vs. creating a new backing index. Behaviour may differ between self-managed and Serverless.

---

### Problem

The Datadog translator always emits `FROM metrics-*` with `BUCKET(@timestamp, 50, ?_tstart, ?_tend)`.
For metrics that Datadog ships as raw cumulative counters, the semantically correct ES|QL is:

```esql
TS metrics-*
| STATS value = MAX(RATE(kubernetes_cpu_usage_total)) BY TBUCKET(5m), host.name
```

Using `FROM` + `SUM` on a raw counter produces a running total, not a rate — wrong for timeseries panels.

### Why it isn't done yet

Datadog pre-aggregates many metrics at the agent before shipping to Elastic:

| Datadog metric | Agent behaviour | What arrives in ES | Correct ES|QL |
|---|---|---|---|
| `system.cpu.user` | Agent computes % rate per interval | Gauge (0–100%) | `FROM` + `AVG` |
| `kubernetes.cpu.requests` | Resource limit (state, not rate) | Gauge | `FROM` + `SUM` |
| `system.net.bytes_rcvd` | Agent sends bytes/interval (rate) | Gauge-like | `FROM` + `SUM` |
| `kubernetes.cpu.usage.total` | **Potentially** raw nanoseconds | Counter? | Would need `TS` + `RATE` |
| `kubernetes.network.rx_bytes` | Depends on Datadog agent version | Ambiguous | **Needs live testing** |

The root issue is we have no authoritative source for which metrics are raw counters vs.
pre-aggregated rates. Using `TS` + `RATE()` on a pre-aggregated rate would double-derive
the rate — wrong in the opposite direction.

> **Needs live testing**: Connect to a Datadog-integrated Elasticsearch cluster, run both
> `FROM` and `TS` queries against the same metric, and compare whether `RATE()` produces
> meaningful values vs. noise. Start with `kubernetes.cpu.usage.total`.

### Infrastructure that already exists

- `_metric_is_count_like(metric_name)` — suffix heuristic (`_total`, `_count`, `.total`, `.count`).
- `MetricQuery.as_rate` / `MetricQuery.as_count` — parsed from Datadog query syntax.
- Both are currently **ignored** by `_build_timeseries_esql()`.

### How to do it properly with Datadog API access

The [Datadog Metrics Metadata API](https://docs.datadoghq.com/api/latest/metrics/#get-metric-metadata)
returns authoritative type per metric:

```
GET /api/v1/metrics/<metric_name>
→ { "type": "count" | "gauge" | "rate" | "distribution" }
```

**Decision table** (to be live-tested before implementing):

| Datadog type | Arrives in ES as | Target ES|QL |
|---|---|---|
| `count` | Raw cumulative counter | `TS` + `RATE(field)` |
| `rate` | Pre-divided per-second value | `FROM` + `AVG(field)` |
| `gauge` | Point-in-time value | `FROM` + `AVG/SUM(field)` |
| `distribution` | Percentile data | `FROM` + `PERCENTILE(field, p)` |

> **All rows need live-test confirmation** — especially `count`, where Datadog docs say
> "cumulative" but the agent may or may not send raw values depending on whether `as_rate()`
> was applied in the Datadog query.

**Implementation plan** (post live testing):

1. **Fetch metadata during migration** — add `DatadogMetricResolver` (mirrors
   `observability_migration/adapters/source/grafana/preflight.py`'s `MetricResolver`) that
   calls `/api/v1/metrics/<name>` with `DD-API-KEY` + `DD-APPLICATION-KEY` and caches results.

2. **Classify at translation time** — pass resolver into `_build_timeseries_esql()` and
   switch `FROM`/`TS` + aggregation function based on resolved type.

3. **Fallback without credentials** — keep `_metric_is_count_like()` as the offline heuristic
   with a logged warning. Preserve resource-gauge carve-out (reuse `_GAUGE_RESOURCE_TOKENS`
   from the Grafana translator).

4. **CLI flag** — `--dd-api-key` / `--dd-app-key` passed to `datadog-migrate`.

5. **YAML contract update** — when a metric is promoted to TS, emit `metric_kind: counter`
   in the YAML so `setup_telemetry_data.py` seeds it with `time_series_metric: counter` mapping.

6. **`TBUCKET` note** — `TS` queries do not take `?_tstart`/`?_tend` parameters; time range
   comes from Kibana's picker. The static validator's `_zero_row_cause()` already handles this.

### What needs live testing before any of this is shipped

| Question | How to test |
|---|---|
| Does `RATE()` error on a `gauge`-mapped field or return wrong data? | Map a field as `gauge`, run `TS … RATE(field)`, observe error vs. result |
| Does `FROM` on a TSDB index return the same rows as `TS`? | Run both queries on the same TSDB index and diff |
| Do Datadog `count` metrics arrive as raw counters or pre-divided rates? | Ingest real Datadog data, compare raw field values vs. Datadog UI |
| What happens when `look_back_time=7d` and we index a 14d-old document? | Attempt ingest, check for rejection vs. new backing index creation |
| Does `TS` on a non-TSDB index produce an error or empty results? | Run `TS non-tsdb-index` and observe |

---
