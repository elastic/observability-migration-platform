# Datadog Redis - Overview — Panel Query Audit

**Dashboard:** Datadog Redis - Overview (migrated from Datadog source)  
**Data window:** 2026-08-03 · Index: `metrics-generic-default` (1,815 synthetic docs) + `logs-generic-default` (968 docs)  
**Controls:** `host` (Any), `key` (Any)

**Synthetic data note:** All metric values come from the seeded sample-data generator. Absolute values (e.g., blocked clients at 312, primary-link-down seconds) are synthetic and not operationally meaningful. Structural correctness (field names, breakdown, aggregation) is what is being validated.

---

## Panel-by-Panel Analysis

### Overview Section

#### Hit rate (stat)

| | |
|---|---|
| **Datadog formula** | `(avg:keyspace_hits / (hits + misses)) * 100` |
| **ES\|QL** | `AVG(hits), AVG(misses) → EVAL (q1/(q1+q2))*100 → AVG` |
| **Live value** | 22.6% |
| **Verdict** | **CORRECT** |

Scalar stat. Hit rate formula faithfully translated.

---

#### Blocked clients (stat)

| | |
|---|---|
| **Datadog metric** | `sum:redis.clients.blocked{…}`, `aggregator: max` |
| **ES\|QL** | `SUM per time bucket → MAX across buckets` |
| **Live value** | 313.5 |
| **Verdict** | **CORRECT** |

Datadog source specifies `aggregator: max` — the ES|QL `MAX(_bucket_value)` is a faithful translation. Datadog's `query_value` `aggregator` controls how the time series is collapsed to a scalar, so `max` = peak value over the window, not the current value. The translation is correct.

---

#### Redis keyspace (stat)

| | |
|---|---|
| **Datadog metric** | `sum:redis.keys{…}`, `aggregator: max` |
| **ES\|QL** | `SUM per time bucket → MAX across buckets` |
| **Live value** | 1,608.6 |
| **Verdict** | **CORRECT** |

Same faithful translation of `aggregator: max`. Datadog shows the peak keyspace size over the selected window.

---

#### Unsaved changes (stat)

| | |
|---|---|
| **Datadog metric** | `sum:redis.rdb.changes_since_last{…}`, `aggregator: avg` |
| **ES\|QL** | `SUM per time bucket → AVG across buckets` |
| **Live value** | 371.4 |
| **Verdict** | **CORRECT** |

Datadog source specifies `aggregator: avg`. The translation faithfully emits `AVG(_bucket_value)`.

---

#### Primary link down (stat)

| | |
|---|---|
| **Datadog metric** | `sum:redis.replication.master_link_down_since_seconds{…}`, `aggregator: avg` |
| **ES\|QL** | `SUM per time bucket → AVG across buckets` |
| **Live value** | 8,015,841,494 secs (~254 years) |
| **Verdict** | **CORRECT** (translation) / **DATA QUALITY** (synthetic values) |

Translation faithfully applies `aggregator: avg` from the source JSON. The absurd value of ~8B seconds is a synthetic seeder artifact — the seeder generated random large values for this field. On real Redis data this panel would show 0 (link up) or a small number of seconds (link temporarily down). The translation itself is not at fault.

---

### Performance Metrics Section

#### Latency by Host (line)

| | |
|---|---|
| **Datadog metric** | `avg:redis.info.latency_ms by {host}` |
| **ES\|QL** | `AVG → EVAL alias → by host.name` |
| **Live value** | 345 ms (host-2); 88 rows (4 hosts × 22 buckets) |
| **Verdict** | **CORRECT** |

Breakdown by `host.name` matches Datadog's `by {host}`.

---

#### Slowlog duration (toplist → datatable)

| | |
|---|---|
| **Datadog formula** | `top(sum:slowlog.95pct/1000, 10, mean, desc)` |
| **ES\|QL** | `SUM → EVAL /1000, datatable` |
| **Live value** | 2.2 ms (name_1/command_1); 198 rows |
| **Verdict** | **APPROXIMATE** |

Datadog's `top(N, mean, desc)` ranking is not applied; ES|QL emits all rows unsorted. Toplist ordering semantics are lost — a busy Redis with many slow queries would show an unsorted table rather than the worst offenders at the top.

---

#### Slowlog query rates (toplist → datatable)

| | |
|---|---|
| **Datadog metric** | `sum:slowlog.micros.count by {command,name}` |
| **ES\|QL** | `SUM → AVG by command,name → SORT DESC` |
| **Live value** | 2,106 (command_3/name_2); 8 rows |
| **Verdict** | **CORRECT** |

Toplist ordering preserved via `SORT DESC`. Null rows filtered. 8 command+name combinations returned.

---

#### Average Replication delay (line)

| | |
|---|---|
| **Datadog metric** | `avg:redis.replication.delay` |
| **ES\|QL** | `AVG → EVAL alias` |
| **Live value** | 159.3 offsets/bucket (22 rows) |
| **Verdict** | **CORRECT** |

Single line series, correct shape.

---

#### Average CPU usage (line)

| | |
|---|---|
| **Datadog metric** | `avg:redis.cpu.sys`, `avg:redis.cpu.user` |
| **ES\|QL** | `AVG both → fused, EVAL aliases` |
| **Live value** | sys=52.4, user=201.6 (22 rows each) |
| **Verdict** | **CORRECT** |

Two metrics fused into one query; breakdown labels preserved. System and User lines both render correctly.

---

#### Cache hit rate — stat

| | |
|---|---|
| **Datadog formula** | `(avg:hits/(hits+misses))*100` |
| **ES\|QL** | Same formula as Overview Hit rate |
| **Live value** | 22.6% |
| **Verdict** | **CORRECT** |

Duplicate of Overview Hit rate stat.

---

#### Cache hit rate — line

| | |
|---|---|
| **Datadog formula** | Same formula |
| **ES\|QL** | Same + timeseries |
| **Live value** | 22.6% per bucket (22 rows) |
| **Verdict** | **CORRECT** |

Timeseries version, correct shape.

---

### Memory Section

#### Pct Memory Used by Host (line)

| | |
|---|---|
| **Datadog formula** | `(avg:mem.used / max:mem.maxmemory)*100 by {host}` |
| **ES\|QL** | `AVG(used), MAX(maxmemory) → EVAL pct by host.name` |
| **Live value** | 20.9% (host-2); 88 rows |
| **Verdict** | **CORRECT** |

Datadog uses `max:` aggregation for `maxmemory`; ES|QL uses `MAX()` — correct aggregation match.

---

#### Evictions (line)

| | |
|---|---|
| **Datadog formula** | `per_minute(sum:redis.keys.evicted) by {host}` |
| **ES\|QL** | `SUM(evicted) / bucket_span_seconds * 60 by host` |
| **Live value** | 3.4 evictions/min (host-2); 88 rows |
| **Verdict** | **CORRECT** |

`per_minute()` is correctly translated: the translator emits `(SUM(...) / bucket_span_seconds) * 60` at line 1647 of `translate.py` — the `* 60` multiplier IS applied. The display unit (evictions per minute) matches Datadog.

---

#### Total allocated memory (line)

| | |
|---|---|
| **Datadog metric** | `sum:redis.mem.rss` |
| **ES\|QL** | `SUM → line` |
| **Live value** | 1,727.8/bucket (22 rows) |
| **Verdict** | **CORRECT** |

RSS sum, single series.

---

#### Fragmentation ratio — stat

| | |
|---|---|
| **Datadog metric** | `avg:redis.mem.fragmentation_ratio` |
| **ES\|QL** | `AVG → AVG of bucket AVGs` |
| **Live value** | 1.064 |
| **Verdict** | **CORRECT** |

Double-AVG is a no-op for uniform synthetic data. Acceptable for the stat display.

---

#### Fragmentation ratio — line

| | |
|---|---|
| **Datadog metric** | Same |
| **ES\|QL** | `AVG → timeseries` |
| **Live value** | 1.27/bucket (22 rows) |
| **Verdict** | **CORRECT** |

---

### Connections Section

#### Connected clients (line)

| | |
|---|---|
| **Datadog metric** | `sum:redis.net.clients` |
| **ES\|QL** | `SUM → line` |
| **Live value** | 1,722/bucket (22 rows) |
| **Verdict** | **CORRECT** |

---

#### Blocked clients (line)

| | |
|---|---|
| **Datadog metric** | `sum:redis.clients.blocked` |
| **ES\|QL** | `SUM → line` |
| **Live value** | 312.8/bucket (22 rows) |
| **Verdict** | **CORRECT** |

---

#### Connected replicas (line)

| | |
|---|---|
| **Datadog metric** | `sum:redis.net.slaves` |
| **ES\|QL** | `SUM → line` |
| **Live value** | 2,288/bucket (22 rows) |
| **Verdict** | **CORRECT** |

---

#### Rejected connections (line)

| | |
|---|---|
| **Datadog metric** | `diff(sum:redis.net.rejected)` |
| **ES\|QL** | `(LAST(metric, @timestamp) - FIRST(metric, @timestamp)) per bucket` |
| **Live value** | Oscillates −1 to +1 (synthetic data artifact) |
| **Verdict** | **CORRECT** (translation) / **DATA QUALITY** (synthetic values) |

`diff()` is correctly translated — the translator emits `(query1_last - query1_first)` using `FIRST`/`LAST` aggregations (translate.py:1684). This is the correct per-bucket delta semantic. The oscillating values (including negatives) are a synthetic seeder artifact: the seeder generates random independent values per document, so `LAST - FIRST` over a bucket can be positive or negative. On real Redis data, `redis.net.rejected` would be a monotonically non-decreasing counter, giving non-negative deltas.

---

#### Commands per second (stat)

| | |
|---|---|
| **Datadog metric** | `sum:redis.net.commands{…}`, `aggregator: avg` |
| **ES\|QL** | `SUM per time bucket → AVG across buckets` |
| **Live value** | 814.8 |
| **Verdict** | **CORRECT** |

`redis.net.commands` is a rate gauge — Datadog already reports it as commands/sec. `aggregator: avg` reduces the time series to the average rate over the window. The ES|QL translation faithfully applies `AVG(_bucket_value)`. The display unit `command/second` is a Datadog UI annotation on the metric type, not computed by the aggregation.

---

### Key Metrics Section

#### Total keys (line)

| | |
|---|---|
| **Datadog metric** | `sum:redis.keys` |
| **ES\|QL** | `SUM → line` |
| **Live value** | 1,607/bucket (22 rows) |
| **Verdict** | **CORRECT** |

---

#### Current total (stat)

| | |
|---|---|
| **Datadog metric** | `sum:redis.keys` |
| **ES\|QL** | `SUM → LAST(value, time_bucket)` |
| **Live value** | 320.6 |
| **Verdict** | **CORRECT** |

`LAST` gives the current snapshot value, matching Datadog's current-value stat display.

---

#### Expired keys (line)

| | |
|---|---|
| **Datadog metric** | `sum:redis.keys.expired` |
| **ES\|QL** | `SUM → line` |
| **Live value** | 1,731/bucket (22 rows) |
| **Verdict** | **CORRECT** |

---

#### Keys with expiration (stat)

| | |
|---|---|
| **Datadog metric** | `sum:redis.expires` |
| **ES\|QL** | `SUM → LAST` |
| **Live value** | 390.8 |
| **Verdict** | **CORRECT** |

---

#### Key length distribution (line)

| | |
|---|---|
| **Datadog metric** | `sum:redis.key_length by {key}` |
| **ES\|QL** | `SUM by key → line` |
| **Live value** | 2,158 (key_2); 88 rows |
| **Verdict** | **CORRECT** |

---

### Logs Section

#### Error Logs (datatable)

| | |
|---|---|
| **Datadog query** | log search `log.level:error service:redis` |
| **ES\|QL** | `FROM logs-* … KQL(log.level:error AND service.name:redis)` |
| **Live value** | Rows from `logs-generic-default` with `level:error`, `service:redis` |
| **Verdict** | **CORRECT** (translation) |

Synthetic seeder generated error-level log docs — table is populated. Query translation is correct.

---

#### All Logs (datatable)

| | |
|---|---|
| **Datadog query** | log search `service:redis` |
| **ES\|QL** | `FROM logs-* … KQL(service.name:redis)` |
| **Live value** | All seeded log docs |
| **Verdict** | **CORRECT** (translation) |

---

## Summary Table

| # | Panel | Section | Verdict | Live value | Notes |
|---|-------|---------|---------|-----------|-------|
| 1 | Hit rate (stat) | Overview | **CORRECT** | 22.6% | |
| 2 | Blocked clients (stat) | Overview | **CORRECT** | 313.5 | aggregator:max faithfully translated |
| 3 | Redis keyspace (stat) | Overview | **CORRECT** | 1,608.6 | aggregator:max faithfully translated |
| 4 | Unsaved changes (stat) | Overview | **CORRECT** | 371.4 | aggregator:avg faithfully translated |
| 5 | Primary link down (stat) | Overview | **DATA QUALITY** | 8.0B secs | Translation correct; seeder generates huge values |
| 6 | Latency by Host | Performance | **CORRECT** | 345 ms | Breakdown by host ✓ |
| 7 | Slowlog duration | Performance | **APPROXIMATE** | 2.2 ms | toplist ordering lost |
| 8 | Slowlog query rates | Performance | **CORRECT** | 2,106 | SORT DESC preserves ordering |
| 9 | Replication delay | Performance | **CORRECT** | 159.3 offsets | |
| 10 | Average CPU usage | Performance | **CORRECT** | sys=52, user=201 | 2-metric fusion ✓ |
| 11 | Cache hit rate (stat) | Performance | **CORRECT** | 22.6% | |
| 12 | Cache hit rate (line) | Performance | **CORRECT** | 22.6%/bucket | |
| 13 | Pct Memory by Host | Memory | **CORRECT** | 20.9% | avg/max agg match |
| 14 | Evictions | Memory | **CORRECT** | 3.4/min | per_minute correctly applies * 60 |
| 15 | Total allocated memory | Memory | **CORRECT** | 1,727.8/bucket | |
| 16 | Fragmentation ratio (stat) | Memory | **CORRECT** | 1.064 | |
| 17 | Fragmentation ratio (line) | Memory | **CORRECT** | 1.27/bucket | |
| 18 | Connected clients | Connections | **CORRECT** | 1,722/bucket | |
| 19 | Blocked clients (line) | Connections | **CORRECT** | 312.8/bucket | |
| 20 | Connected replicas | Connections | **CORRECT** | 2,288/bucket | |
| 21 | Rejected connections | Connections | **DATA QUALITY** | oscillates −1 to +1 | diff() correct (last-first); seeder non-monotonic |
| 22 | Commands per second (stat) | Connections | **CORRECT** | 814.8 | aggregator:avg faithfully translated |
| 23 | Total keys (line) | Keys | **CORRECT** | 1,607/bucket | |
| 24 | Current total (stat) | Keys | **CORRECT** | 320.6 | LAST ✓ |
| 25 | Expired keys | Keys | **CORRECT** | 1,731/bucket | |
| 26 | Keys with expiration (stat) | Keys | **CORRECT** | 390.8 | |
| 27 | Key length distribution | Keys | **CORRECT** | 2,158 | by {key} ✓ |
| 28 | Error Logs | Logs | **CORRECT** | seeded rows | translation correct |
| 29 | All Logs | Logs | **CORRECT** | seeded rows | translation correct |

**29/29 panels return data. 26 CORRECT, 1 APPROXIMATE, 2 DATA QUALITY.**

**Correction note:** An earlier version of this audit incorrectly classified `diff()`, `per_minute()`, and several stat panels as WRONG or APPROXIMATE. Review of `translate.py` shows all three are faithfully translated: `diff()` emits `(LAST - FIRST)` per bucket (line 1684), `per_minute()` applies `* 60` (line 1647), and stat panel `MAX`/`AVG` reducers directly mirror Datadog's own `aggregator` field in the source JSON. The anomalous UI values (8B-second Primary link down, oscillating Rejected connections) are synthetic seeder artifacts, not translator bugs.

---

## Actionable Fixes

| Priority | Panel | Issue | Fix |
|---|---|---|---|
| **Low** | 7 — Slowlog duration | `top(N, mean, desc)` ranking lost; all rows returned unsorted | Add `SORT value DESC` + note the N limit is not enforced |
| **Data quality** | 5 — Primary link down | Synthetic seeder generates unrealistically large values (~8B secs) | Seeder should generate 0 for this field (link is up), or omit it entirely |
| **Data quality** | 21 — Rejected connections | Synthetic seeder generates non-monotonic values so `LAST - FIRST` oscillates negative | Seeder should generate monotonically non-decreasing values for counter fields |
