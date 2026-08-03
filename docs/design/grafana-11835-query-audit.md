# gnetId=11835 — Panel Query Audit

**Dashboard:** Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha)  
**Source:** https://grafana.com/grafana/dashboards/11835  
**Data window:** 2026-08-03 · Instance: `redis:6379`  
**Migration target:** `metrics-redis.prometheus-default` (7.3M docs, `prometheus_native` field profile)

---

## Panel-by-Panel Analysis

### Panel 1 — Uptime

| | |
|---|---|
| **Grafana type** | singlestat → metric |
| **PromQL** | `max(max_over_time(redis_uptime_in_seconds{instance=~"$instance"}[$__interval]))` |
| **ES\|QL** | `MAX(MAX_OVER_TIME(metrics.redis_uptime_in_seconds, 5m))` → `LAST` |
| **Live value** | 439,271 s (~5.08 days) |
| **Verdict** | **CORRECT** |

Faithful translation. 5m fixed window vs adaptive `$__interval` is conservative but correct for a monotone gauge.

---

### Panel 2 — Clients

| | |
|---|---|
| **Grafana type** | singlestat → metric |
| **PromQL** | `redis_connected_clients{instance=~"$instance"}` (no aggregation — raw series) |
| **ES\|QL** | `MAX(LAST_OVER_TIME(metrics.redis_connected_clients))` → `LAST` |
| **Live value** | 1 |
| **Verdict** | **CORRECT** |

Grafana's singlestat on a raw series picks the last value; `MAX(LAST_OVER_TIME)` → `LAST` is equivalent with a single instance in window.

---

### Panel 3 — Memory Usage _(curated override)_

| | |
|---|---|
| **Grafana type** | singlestat → metric |
| **PromQL** | `100 * (redis_memory_used_bytes / redis_memory_max_bytes)` |
| **ES\|QL (curated)** | see below |
| **Live value** | 1.23% |
| **Manual check** | 1,293,264 / 104,857,600 × 100 = **1.23%** ✓ |
| **Verdict** | **CORRECT** (via curated override) |

```esql
TS metrics-redis.prometheus-default
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| WHERE labels.instance == ?instance
| STATS value = MAX(LAST_OVER_TIME(metrics.redis_memory_used_bytes))
           / MAX(LAST_OVER_TIME(metrics.redis_memory_max_bytes)) * 100.0
```

**Note on `maxmemory = 0`:** When Redis runs with `maxmemory_policy: noeviction`, `redis_memory_max_bytes = 0`. Division by zero in ES|QL returns `null`, displayed as N/A — the same behavior as Grafana.

**Structural note:** `labels.instance == ?instance` uses strict equality. The control defaults to selecting `redis:6379` explicitly in practice, so it works — but it is fragile if a user sets the control to the `.*` regex default. Other panels use `RLIKE` for this filter.

---

### Panel 4 — Commands Executed / sec

| | |
|---|---|
| **Grafana type** | graph → xy |
| **PromQL** | `rate(redis_commands_processed_total{instance=~"$instance"}[1m])` |
| **ES\|QL** | `AVG(RATE(metrics.redis_commands_processed_total)) BY time_bucket, labels.instance` |
| **Live value** | ~31.5 commands/s, stable across 12 buckets |
| **Verdict** | **CORRECT** |

`AVG(RATE(...))` over a single TSDB dimension group equals the RATE value directly. Breakdown by `labels.instance` matches the implicit per-instance series in PromQL.

---

### Panel 5 — Hits / Misses per Sec

| | |
|---|---|
| **Grafana type** | graph → xy |
| **PromQL** | `irate(redis_keyspace_hits_total[5m])` + `irate(redis_keyspace_misses_total[5m])` |
| **ES\|QL** | `AVG(IRATE(hits)), AVG(IRATE(misses)) BY time_bucket, labels.instance` |
| **Live value** | hits=10.0/s, misses=10.0/s (stable) |
| **Verdict** | **CORRECT** |

Two targets fused. IRATE semantics preserved (2-sample, same as PromQL irate).

---

### Panel 6 — Total Memory Usage

| | |
|---|---|
| **Grafana type** | graph → xy |
| **PromQL** | `redis_memory_used_bytes` + `redis_memory_max_bytes` (raw gauge, legend: `used` / `max`) |
| **ES\|QL** | `AVG(LAST_OVER_TIME(used)), AVG(LAST_OVER_TIME(max)) BY time_bucket, labels.instance` |
| **Live value** | used=1,293,264 B, max=104,857,600 B (flat — Redis at steady state) |
| **Verdict** | **CORRECT** |

`LAST_OVER_TIME` is the correct gauge read. `AVG` over a unique TSDB dimension group is a no-op. Two targets correctly fused into a 2-metric + breakdown timeseries.

---

### Panel 7 — Network I/O

| | |
|---|---|
| **Grafana type** | graph → xy |
| **PromQL** | `rate(redis_net_input_bytes_total[5m])` + `rate(redis_net_output_bytes_total[5m])` (no `by` clause) |
| **ES\|QL** | `RATE(input), RATE(output) BY time_bucket` — no breakdown |
| **Live value** | input ~1,153 B/s, output ~5,253 B/s |
| **Verdict** | **CORRECT** |

No `by` clause in PromQL → correctly no `breakdown_by` in Kibana; exactly 2 legend entries. Output ~4.5× input is plausible.

---

### Panel 8 — Total Items per DB

| | |
|---|---|
| **Grafana type** | graph → xy |
| **PromQL** | `sum(redis_db_keys{instance=~"$instance"}) by (db)` |
| **ES\|QL** | `SUM(metrics.redis_db_keys) BY time_bucket, labels.db` |
| **Live value** | 192 rows (16 db labels × 12 buckets); db0=1, all others=0 |
| **Verdict** | **CORRECT** |

Grafana `by (db)` → ES|QL `BY labels.db` — exact match. This dashboard breaks by `db` only (unlike gnetId=763 which also breaks by `instance`). 192 rows = 16 db-label combinations × 12 buckets as expected.

---

### Panel 9 — Expiring vs Not-Expiring Keys

| | |
|---|---|
| **Grafana type** | graph → xy |
| **PromQL** | `sum(redis_db_keys) - sum(redis_db_keys_expiring)` + `sum(redis_db_keys_expiring)` |
| **ES\|QL** | `SUM(keys) - SUM(expiring) EVAL not_expiring` — no breakdown |
| **Live value** | total=1, expiring=1, not_expiring=0 (all 12 buckets identical) |
| **Verdict** | **CORRECT** |

Scalar arithmetic with no `by` clause matches PromQL. Values are plausible: 1 key, it is expiring.

---

### Panel 10 — Expired / Evicted

| | |
|---|---|
| **Grafana type** | graph → xy |
| **PromQL** | `sum(rate(redis_expired_keys_total[5m])) by (instance)` + `sum(rate(redis_evicted_keys_total[5m])) by (instance)` |
| **ES\|QL** | `SUM(RATE(expired)), SUM(RATE(evicted)) BY time_bucket, labels.instance` |
| **Live value** | expired=0, evicted=0 (all buckets) |
| **Verdict** | **CORRECT** |

Zero evictions expected (noeviction policy). Zero expirations in this window is consistent with panel 9 showing 1 expiring key with very slow turnover.

---

### Panel 11 — Command Calls / sec _(stacked area)_

| | |
|---|---|
| **Grafana type** | graph → xy (stacked area) |
| **PromQL** | `topk(5, irate(redis_commands_total{instance=~"$instance"}[1m]))` |
| **ES\|QL** | `AVG(IRATE(metrics.redis_commands_total)) BY time_bucket, labels.cmd` |
| **Live value** | 132 rows (11 cmds × 12 buckets); top cmds: `get`=20/s, `set`=10/s |
| **Verdict** | **APPROXIMATE** |

`topk(5, ...)` in PromQL limits to the 5 highest-rate commands per window. ES|QL emits all 11 commands because ES|QL has no `topk()` window function. With this Redis instance only ~5-6 commands have non-trivial rates so the visual difference is minor, but a busy Redis with many command types would produce a cluttered stacked chart. The stacked-area rendering type is correctly translated.

---

### Panel 12 — Redis Connected Clients _(timeseries)_

| | |
|---|---|
| **Grafana type** | graph → xy |
| **PromQL** | `redis_connected_clients{instance="$instance"}` (exact match) |
| **ES\|QL** | `AVG(LAST_OVER_TIME(metrics.redis_connected_clients)) BY time_bucket, labels.instance` with RLIKE filter |
| **Live value** | 1 client, stable across all 12 buckets |
| **Verdict** | **CORRECT** |

`AVG(LAST_OVER_TIME)` over a unique TSDB dimension is the raw gauge value. Value=1 is consistent with Panel 2's stat. The exact-match `instance="$instance"` in PromQL is translated to an RLIKE pattern — slightly looser but no practical difference for well-formed instance names.

---

## Summary Table

| # | Panel | Grafana type | Status | Verdict | Live value | Notes |
|---|-------|-------------|--------|---------|-----------|-------|
| 1 | Uptime | singlestat | migrated | **CORRECT** | 439,271 s | MAX_OVER_TIME → LAST |
| 2 | Clients | singlestat | migrated | **CORRECT** | 1 | MAX(LAST_OVER_TIME) → LAST |
| 3 | Memory Usage | singlestat | migrated | **CORRECT** | 1.23% | Curated override; == vs RLIKE fragile on `.*` |
| 4 | Commands/sec | graph | migrated | **CORRECT** | ~31.5/s | AVG(RATE) = RATE for single instance |
| 5 | Hits/Misses | graph | migrated | **CORRECT** | 10/10 s⁻¹ | IRATE semantics preserved |
| 6 | Total Memory | graph | migrated | **CORRECT** | 1.23 MB / 100 MB | LAST_OVER_TIME gauge read |
| 7 | Network I/O | graph | migrated | **CORRECT** | 1.15 / 5.25 KB/s | No breakdown matches no-`by` PromQL |
| 8 | Items per DB | graph | migrated | **CORRECT** | db0=1, rest=0 | `by (db)` → `BY labels.db` exact |
| 9 | Expiring Keys | graph | migrated | **CORRECT** | 1 expiring, 0 not | Scalar arithmetic, no breakdown |
| 10 | Expired/Evicted | graph | migrated | **CORRECT** | 0/0 | SUM(RATE) by instance; noeviction confirms |
| 11 | Command Calls | graph | migrated_with_warnings | **APPROXIMATE** | 11 cmds returned | `topk(5)` untranslatable → all cmds shown |
| 12 | Connected Clients | graph | migrated | **CORRECT** | 1 | Timeseries version of Panel 2 |

**12/12 panels return live data. 11 CORRECT, 1 APPROXIMATE.**

---

## Actionable Gaps

| Priority | Panel | Issue | Fix |
|---|---|---|---|
| Low | 11 — Command Calls | `topk(5)` semantics lost; all commands returned instead of top 5 | No ES|QL equivalent; document in fidelity manifest as APPROXIMATE |
| Low | 3 — Memory Usage | `labels.instance == ?instance` breaks if control sends `.*` as literal | Switch override filter to `RLIKE` pattern used by all other panels |
