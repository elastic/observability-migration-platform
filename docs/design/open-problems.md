# Open Problems

Known-unsolved issues in the migration engine, with the evidence gathered so far
so the next person does not repeat the investigation. Each entry says how it was
found, what is proven, and what is still unknown.

Everything here is reproducible against the curated rig
(`parity-rig/curated/grafana_763_redis_exporter/docker-compose.yml`, ES on
`:9201`) and the pinned community corpus
(`python scripts/fetch_community_corpus.py --output-dir <dir>`, 69 dashboards).

---

## 0. ~~Scalar panels ignore Grafana's `reduceOptions.calcs`~~ FIXED

**Status:** FIXED in aae227c. lastNotNull/last -> LAST(field, time_bucket), mean -> AVG, min -> MIN, everything else keeps MAX; LAST only when a single field is kept, because the MAX default exists for null-safety across multi-target rows. Verified against Prometheus: CPU Busy 79.1 -> 1.833 (Prometheus 1.938), SWAP Used 0.1976 (Prometheus 0.19760207).

`reduceOptions` is never read anywhere in the codebase. Every stat/gauge/bargauge
panel collapses its series with a hardcoded `MAX`, whatever the dashboard asked
for. Node Exporter Full's "CPU Busy" declares
`reduceOptions.calcs: ["lastNotNull"]`, so Grafana shows the LATEST value; we show
the peak.

Measured on the rig, same window:

| | CPU Busy |
|---|---|
| Prometheus (`lastNotNull`, what Grafana draws) | **1.87%** |
| ours (`MAX` over 100 buckets) | **79.1%** |

Both are "a real number from real data", which is why no gate caught it — the
numeric gate compares series shape and the values are legitimately present.

The mapping is small and mechanical: `lastNotNull`/`last` -> take the final
bucket (`SORT time_bucket DESC | LIMIT 1`), `mean` -> AVG, `min` -> MIN,
`max` -> MAX (today's behaviour, correct only when the dashboard asked for it).
Every scalar panel across every dashboard is affected.

---

## 0c. ~~`labels.ip` silently discards documents~~ FIXED

Elasticsearch types a field named `ip` as the **ip datatype** (ECS `match_ip`),
node_exporter publishes `node_udp_queues{ip="v4"}`, "v4" is not an IP address, and
the WHOLE document was rejected into the failure store -- while the bulk response
still returned 201, so the scraper reported "0 errors" while losing 3459 documents.

The fix is that `labels` must be redeclared in FULL. A partial
`{"properties": {"ip": ...}}` loses to the composed passthrough definition, and
dynamic templates do not win either (verified: ordered ahead of ECS's `match_ip`
and still rejected). Repeating `type`/`priority`/`time_series_dimension` alongside
the subfield replaces the definition outright and the type sticks.

Applied in both templates that can govern these streams -- the rig scraper's and
`apply_counter_mappings.py`'s, the latter at priority 600 where it silently won.

Result: node ingestion `errors 4 -> 0`, failure store `3459 -> 0`,
`labels.ip` and `metrics.node_udp_queues` both present, and Node Exporter Full
goes from 107 to 108 panels returning data.

A detail that wasted an attempt: a hand-made probe document is rejected with
"timestamp outside of ranges of currently writable indices" if its timestamp is
not recent -- a TSDB write-window rule, nothing to do with mappings. It looks
identical to the mapping failure in the bulk response.

---

## 0e. ~~A rate is wrong whenever the bucket is wider than its window~~ FIXED

**Status:** FIXED. Widest-reaching defect found in this pass.

I previously wrote that `RATE(field, window)` "honours its window argument" and
that bucket width does not affect it. **That was wrong, and the test behind it was
insensitive** -- I varied only the bucket width, never the window, against a
counter that increases linearly, so the slope came out the same either way.

Reading the implementation
(`RateDoubleGroupingAggregatorFunction`, `x-pack/plugin/esql/compute`) explains
why. The rate is `(lastValue - firstValue) / (lastTsSec - firstTsSec)` over the
samples **in the bucket**, then extrapolated to the bucket boundaries with a
PromQL-inspired rule: it extrapolates fully when samples reach within ~10% of the
average sample spacing of the boundary, and otherwise only by half that spacing.
It is not a Prometheus-style independent lookback per step.

So window and bucket width interact. Measured:

| range / bucketing | emitted window | result | correct |
|---|---|---|---|
| 50 min, TBUCKET(20) = 2.5 min | `1m` (< bucket) | 4.759 | no |
| 50 min, TBUCKET(20) = 2.5 min | `5m` | 0.982 | yes |
| **12 h, TBUCKET(100) = 7.2 min** | **`5m` (< bucket)** | **1.955** | **no, ~2x** |
| 12 h, TBUCKET(100) = 7.2 min | `10m` (>= bucket) | 0.984 | yes |

We emit a hardcoded `5m` with an ADAPTIVE `TBUCKET(100, ?_tstart, ?_tend)`. The
bucket therefore grows with the dashboard's time range, and any range beyond
about 8 hours makes buckets wider than 5 minutes -- at which point **every rate
panel on the dashboard reads roughly double**. A 12-hour view is entirely
ordinary.

**Fix applied:** drop the window argument entirely. Reading
`AggregateFunction.java` settles it -- the default is
`NO_WINDOW = Literal.timeDuration(Source.EMPTY, Duration.ZERO)`, and the word
"window" never appears in the aggregator: `computeRate` works from
`tbucketStart`/`tbucketEnd`. Omitting the window therefore makes the rate
bucket-aligned by construction, at every range, with no window/bucket pair to
keep in sync. `RATE`/`IRATE`/`INCREASE` are now emitted windowless
(`_range_call` in `promql.py`, used by every fragment emitter, plus the
`counter_range_window` postprocessor as a backstop). The `*_OVER_TIME` family
takes its window as a real lookback and keeps it.

Verified against Prometheus on the rig (true idle rate 0.984), with the adaptive
bucket that dashboards actually emit:

| range | prom | `RATE(x, 5m)` | `RATE(x)` |
|---|---|---|---|
| 1 h | 0.985 | 0.944 (4%) | 0.966 (2%) |
| 6 h | 0.984 | 0.970 (1%) | 0.970 (1%) |
| **12 h** | 0.984 | **1.945 (98%)** | 0.980 (0%) |
| **24 h** | 0.984 | **5.702 (480%)** | 0.953 (3%) |

**Reproducing this requires the adaptive `TBUCKET(100, ?_tstart, ?_tend)`.** A
fixed `TBUCKET(432 seconds)` at the same 12 h range and same `5m` window reads
0.979 -- fine. A fixed-width probe hides the defect completely, and that nearly
led me to dismiss it a second time. Sweep several dashboard ranges *with the
adaptive form*.

Two gates keyed off the window and had silently stopped firing for windowless
calls; both now accept either form: the structural oracle's `_BARE_TS_VALUE_ARG`
(`STATS_CASE_BARE_TS_MIX`) and the CASE-sibling wrapper in `promql.py`, which
must wrap a bare counter beside a CASE-wrapped one or Elasticsearch rejects the
mix.

---

## 0d. ~~Boundary buckets produce garbage rates, and `LAST` picks one of them~~ FIXED for ungrouped scalars

**Status:** FIXED for ungrouped scalar panels. A scalar panel collapsing a
range function now takes the penultimate bucket -- ES|QL has no OFFSET, so
`SORT DESC | LIMIT 2 | SORT ASC | LIMIT 1` reaches it, degrading to the only
bucket when there is just one. CPU Busy no longer disagrees with Prometheus.

STILL OPEN for GROUPED panels: a pie or bar with a rate has one row per
group, so LIMIT 1 would keep a single slice. Those need a per-group fix and
still read their boundary bucket.

Note on method: RATE semantics here were established empirically (three
bucket widths, identical results), NOT by reading the Elasticsearch source.

A gauge or stat over a rate collapses with `LAST(value, time_bucket)` (correct --
the panel declares `lastNotNull`). But the FINAL bucket of the window is a
boundary bucket, and a rate computed there is not merely coarse, it is wrong.

Measured over the same 7 minutes, `100 * (1 - avg(rate(idle[5m])))`:

| engine | values |
|---|---|
| Prometheus, 60 s steps | 1.707, 1.729, 1.811, 1.823, 1.837, 1.813 |
| ours, per bucket | 1.682, 1.711, 1.696, **22.549** |

Interior buckets track Prometheus closely. The boundary bucket reads 22.5. In
Kibana the dashboard window ends at "now", so the final bucket is ALWAYS a
boundary bucket -- which is why "CPU Busy" read 9.33 over one window and 1.72 over
another, and why it looked like a harness defect when it is not. `dashboard_qa.py`
reproduces the panel exactly; both return the same number.

This is the same family as the fixed `TBUCKET(1)` bug: a rate evaluated over an
incomplete span. Prometheus never has this problem because each step is an
independent 5-minute lookback rather than a bucket of samples.

Fix direction: when a scalar panel collapses a range function with LAST, it must
not read the final bucket. Verify against Prometheus after -- interior buckets
already agree, so the target is known.

**RETRACTED:** the claim that RATE "honours its window argument" and is unaffected
by bucket width was based on a test that never varied the window. See 0e -- the
two interact, and a bucket wider than the window roughly doubles the result.

---

## 1a. Kubernetes / Views / Namespaces "Overview": two panels ignore source geometry

**Status:** diagnosed, not fixed. The only finding `scripts/dashboard_qa.py`
reports across the 69-dashboard corpus.

The row is flush in Grafana (every column ends at 12) and ragged in ours. The
first three panels transform faithfully; the last two do not:

| panel | source (24-col) | expected (x2) | emitted |
|---|---|---|---|
| usage on total cluster CPU | x=0 w=6 h=7 | x=0 w=12 | x=0 w=12 h=13 OK |
| usage on total cluster RAM | x=6 w=6 h=7 | x=12 w=12 | x=12 w=12 h=13 OK |
| Kubernetes Resource Count | x=12 w=12 h=11 | x=24 w=24 | x=24 w=24 h=21 OK |
| CPU Usage in cores | x=0 y=8 w=6 h=4 | x=0 w=12 | **x=0 w=24** |
| RAM Usage in bytes | x=6 y=8 w=6 h=4 | x=12 w=12 | **x=24 y=21 w=24** |

Both come out full width and stacked, which is the signature of
`_apply_even_distribution_fallback` (it spreads a band evenly across the
48 columns) rather than `_apply_faithful_coordinate_transform`. Worth checking
first whether those two panels reach layout without `_grafana_w`/`_grafana_h` --
the fallback is chosen when ANY panel in the group lacks them, and these two are
the ones whose geometry is lost.

Not a regression from the band-uniform height pass: that pass took corpus
raggedness from 3 rows to this 1.

---

## 0b. ~~Per-type min-height bumps break the source's row proportions~~ FIXED

**Status:** confirmed defect with a worked example. Not fixed — the fix touches
layout for every dashboard and needs verification I could not complete.

Node Exporter Full's first row, "Quick CPU / Mem / Disk", is ragged: the
right-hand stat block overhangs the gauge row by one grid unit.

The faithful coordinate transform is NOT the problem. It is exactly right:

| stage | gauges | CPU Cores | RootFS Total | result |
|---|---|---|---|---|
| Grafana source (30px rows) | h=4 | h=2 | h=2 | right block 2+2 = 4 == 4, flush |
| after 1.5x scale (30px -> 20px) | 6 | 3 | 3 | 3+3 = 6 == 6, still flush |
| after per-type min-height bumps | **8** | **3** | **6** | 3+6 = **9** vs **8**, ragged |

Two separate things go wrong in the bump pass
(`_apply_kibana_native_layout`, panels.py):

1. `PANEL_SIZE_CONSTRAINTS` imposes a legibility floor per visualisation type
   (gauge min_h=8, metric min_h=6). Applied on top of faithfully scaled
   coordinates it overrides the proportions the dashboard author chose — the
   gauges go 6 -> 8 purely because they are gauges.
2. The bump is attempted panel-by-panel and **rejected when it would overlap a
   neighbour**, which makes the breakage asymmetric. `CPU Cores` cannot grow
   3 -> 6 because `RootFS Total` sits directly below it, so it stays 3; `RootFS
   Total` has nothing below it, so it grows 3 -> 6. Two panels with identical
   source geometry end up different heights.

The layout is otherwise sound: checked every row for overlaps, overflow past 48
columns and zero-size panels — 0 issues — and x/w scale correctly (x2).

Fix direction: when source geometry is present the transform is already faithful,
so a legibility floor should either be applied to a whole band at once (bump every
panel in the band and reflow what is below) or not applied at all, falling back to
a hard minimum (~3) that only rescues genuinely unreadable tiles. Either way it
must be verified across the corpus for new overlaps, which is why it is not done
here.

---

## 0b. ~~rate() degraded to AVG_OVER_TIME reports a number that is not a rate~~ WITHDRAWN — I was wrong

I filed this as an active bug affecting 17 corpus panels. It is not happening.

My detection was "source contains rate() AND output contains AVG_OVER_TIME",
which is wrong for multi-target panels: those panels have a rate()'d counter
target AND a separate plain-gauge target, and the AVG_OVER_TIME belongs to the
gauge one, where averaging over the bucket is exactly right.

Checking whether the rate()'d metric is itself the degraded one:

| measure | count |
|---|---|
| loose heuristic (what I reported) | 14 |
| a rate()'d metric actually degraded to AVG_OVER_TIME | **0** |

Zero on the community corpus and zero across Node Exporter Full, MySQL Overview,
PostgreSQL and Redis.

The reason is the policy in `_should_degrade_counter_range_func`, which is doing
its job: rate()/irate() are counter-only in PromQL, so the source asserting one
is treated as authoritative and is NOT degraded on a live-caps gauge vote. Only
an explicit rule-pack `metric_kinds: gauge` pin or a cross-index type conflict
forces the degrade.

The underlying observation still holds as a latent hazard — IF the fallback ever
fires for a genuinely rate()'d metric, `AVG_OVER_TIME` would report the average
cumulative value rather than a rate (measured on `mysql_global_status_queries`:
2437 vs a true 0.62/sec). The correct form is
`(LAST_OVER_TIME(x) - FIRST_OVER_TIME(x)) / bucket_seconds`, both of which work
on gauge-typed fields. Worth doing if the policy is ever loosened; not urgent,
because today the policy prevents it.

Lesson recorded because it cost real effort: measure the thing you are claiming,
not a proxy for it.

---

## 1. ~~Negated literal label matchers drop series with an absent label~~ FIXED

Resolved once the CASE-shape normaliser was fixed -- that interaction, not the
semantics, was what blocked the first attempt. `!=` and `!~` now read the label
through `COALESCE(field, "")` like the parameterized matchers do.

Measured on the node index: 2640 documents carry `process_open_fds` and none
carry `release`; `labels.release != "prod"` matched 0 of them, the fixed form
matches all 2640 -- which is what Prometheus does, since an absent label is "".

## 2. ~~Structural oracle does not treat STATS BY keys as defined columns~~ FIXED

`parse_stats_grouping` now contributes a STATS stage's grouping keys to the
defined-column set, alongside its aggregate aliases. Verified it still reports a
genuinely undefined column (the dropped-outer-STATS shape) rather than going
permissive.

With this the community corpus reaches **0 structural-oracle errors** across
1323 emitted queries.

The translator still emits a degenerate `EVAL namespace = namespace` that the
following `KEEP` discards. Harmless noise, worth removing separately.

---

## 3. Bucket alignment on `$interval`-driven ratio panels

**Status:** intermittent, currently not reproducing.

Node Exporter Dashboard EN "Disk R/W Time (Reference: less than 100ms)(beta)":
`rate(node_disk_read_time_seconds_total[$interval]) / rate(node_disk_reads_completed_total[$interval])`.

Seen once as a single FAIL out of 2510 comparisons -- both sides returned exactly
one series but no time bucket overlapped. The last several corpus runs report
0 FAIL, so it depends on where the window falls relative to scrape boundaries.

Not chased further because it stopped reproducing, and a fix aimed at a
non-reproducing symptom is a fix aimed at nothing. If it returns, the question is
whether `$interval` resolves differently on the two sides or whether `TBUCKET`
and the oracle's `step` disagree at that granularity.

---

## 4. Counters that Prometheus exports as `untyped`

**Status:** working as designed; documented here because it looks like a bug.

`rate()` is counter-only in PromQL, so the source asserts the field is a counter.
Real exporters emit many counters as `# TYPE untyped` — node_exporter does for
`node_netstat_*` and `node_vmstat_*`. Elasticsearch then maps them as gauges
(counter detection keys off the `_total` suffix), and `RATE()` on a gauge is
rejected.

The translator deliberately stays source-faithful and warns with the fix
instructions — 190 such warnings on the community corpus. The parity oracle
classifies the resulting failure as `DATA_GAP`, not `ERROR`.

There is no translator-side fix: the ingest mapping has to mark the field as a
counter, or the rule pack has to pin `metric_kinds: <metric>: gauge`. What is
worth considering is whether the CLI should offer to emit that rule-pack pin.

---

## 5. Fusion cannot express a nested aggregation

**Status:** now refuses correctly, but the panel degrades.

`_merge_pretranslated_xy_queries` models a single STATS. A target whose output
column comes from a second STATS (`min(sum(x) by (instance))`) is refused rather
than silently emitted broken. The panel then falls back to a non-fused path,
which is correct but may show fewer merged series than the source intended.

Making the merge carry an outer aggregate would fix the shape properly. Affects
Kubernetes Resource Requests "CPU Cores" and "Memory".

---

## 6. Datadog `key` control is inert

**Status:** data gap, not a defect. Re-check when Datadog-shaped data is seeded.

`labels.key` is absent from the target, so the control renders and is selectable
but filters nothing. Confirmed not a translator bug: 0 of 29 panels bind `?host`
or `?key`, so switching the control type would produce an inert control either
way.

---

## 7. ~~Render audit reports no per-panel detail~~ NOT A GAP (corrected)

My earlier note here was wrong. Per-panel attribution exists and works; it
requires `--migration-out <dir>/dashboards`, and the audit was **degrading
silently** without it -- returning `panels: []` with no hint that a flag would
have answered the question.

With the flag, on the Datadog canary: 24 panels attributed, 21 rendered, 1 empty,
2 error -- and the 2 errors are exactly `log_stream` / `list_stream`, matching the
independent per-query analysis precisely.

The audit now prints a notice when it falls back, and when `--migration-out`
points at a directory with no `migration_report.json`.

---

## 8. ~~Datadog `--field-profile prometheus` vs `prometheus_native`~~ FIXED

Discovery already held the live field names; nothing compared them to the chosen
profile. `_warn_on_field_profile_mismatch` now warns when the profile's
`metric_prefix` matches none of the discovered fields, and names the profile the
index actually looks like.

Still worth considering: making this a hard failure under `--strict`, and giving
the Grafana path the same check (it has richer discovery, but no equivalent
prefix-vs-index assertion).

---

## 9. Refusals are disclosed, not silent — verified

87 of 1647 corpus packets carry a PromQL source query and emit nothing. All 87
are `status: not_feasible` with `semantic_gate: Red` and a written reason, so
none of them is silent loss. Breakdown:

| pattern | count | why refused |
|---|---|---|
| `on()` / `ignoring()` vector matching | 45 | genuinely unaligned joins |
| `histogram_quantile` on a bare `_bucket` | 21 | see below |
| other / template-heavy / `label_join` | 21 | assorted |

**Do not "fix" the histogram_quantile group by guessing a grouping.**
`histogram_quantile(q, sum by (le) (rate(x_bucket[5m])))` translates fine. The
bare `histogram_quantile(q, rate(x_bucket[5m]))` is refused, and that is correct:
the bare form computes a quantile *per label-set*, so translating it requires
choosing a grouping the source never stated. Picking one would produce numbers
that look plausible and are wrong — the exact failure mode the rest of this
document is about. The emitted reason already tells the operator to add
`sum by (le)`.

---

## Four-dashboard audit — result and the ceiling

Audited panel by panel against the live rig (migrate -> upload -> execute every
query -> browser render audit with per-panel attribution).

| dashboard | panels with data | browser render |
|---|---|---|
| Redis (763) | 13/13 | 13/13, `pass` |
| PostgreSQL Exporter | 6/6 (from 2/6) | 6/6, `pass` |
| Datadog DB Overview | 14/14 (from 9/14) | 14/14, `pass` |
| MySQL Overview | 30/36 (from 11/36) | 30 rendered, 6 error |
| Node Exporter Full | 105/124 (from 99) | 18/19 visible, 0 errors |

Node Exporter Full renders 19 in the browser because 124 of its panels sit in 14
rows that ship **collapsed** — faithful to the Grafana source.

**Every panel now renders; none show an error card.** MySQL Overview is 36/36
rendered, `pass`. Node Exporter Full is 18 rendered + 1 empty of the 19 visible,
0 errors (124 of its panels live in 14 rows that ship collapsed, faithful to the
source).

Panels whose metrics are genuinely absent from the deployment -- MySQL query cache
(removed in MySQL 8), `rdsosmetrics_*` (AWS RDS only),
`innodb_additional_mem_pool_size` (removed in 5.7), `mysql_info_schema_threads`,
and node_exporter's hardware sensors (hwmon, cooling device, power supply, CPU
scaling) which need host access a container lacks -- are curated by
`scripts/curate_absent_metric_panels.py` into a note in the same grid slot, naming
the missing metric and what to ingest.

That is the deliberate answer to a real presentation gap: ES|QL rejects an unknown
column, so Kibana shows a red error where Grafana draws an empty chart. Rewriting
the query to emit NULL would render empty but bake the absence in, so the panel
could never self-heal. The note keeps the board clean AND keeps the fix reversible
-- re-run the migration once the metric lands and the real panel returns.

Detection is verified against live field caps, not just the migration report:
the report's own validator missed two MySQL panels because their query lives
under `config.layers[].data_source`, not `config.data_source`.

---

## Current verified state

Everything below was verified end to end against the live rig -- migrate, upload,
execute every emitted query, browser render audit with per-panel attribution --
not by reading routing tables.

**Grafana**
- Community corpus (69 dashboards, 1323 emitted queries): **0 structural-oracle
  errors**, down from 15.
- Numeric parity gate (2510 comparisons): **0 FAIL, 0 ERROR**, 236 passes
  (136 STRICT / 87 SHAPE / 13 FUZZY).
- Panel-type canary (`infra/grafana/dashboards/kitchen-sink-canary.json`),
  all 16 supported types: **16/16 rendered** in Kibana.
- Curated Redis 763 pack: **13/13 rendered**, render audit `pass`.

**Datadog**
- All fixtures (26 emitted queries): **0 structural-oracle errors**.
- Widget canary (`infra/datadog/dashboards/kitchen-sink-canary.json`),
  all 23 planner-routed types plus a nested group: **22/22 non-log widgets
  rendered**; the 2 log widgets fail only because the rig has no `logs-*` index
  (confirmed absent via `_resolve/index`).
- `sample_dashboard.json` execution errors are all `Unknown column` for Datadog
  system metrics against a redis index -- data gaps, not defects.

Every lossy conversion is disclosed with a reason rather than applied silently,
which is the behaviour to preserve:

| widget | becomes | disclosed as |
|---|---|---|
| `geomap` | markdown placeholder | `requires_manual` — needs Kibana Maps |
| `check_status` / `manage_status` | markdown placeholder | `requires_manual` — Synthetics/Alerts |
| `hostmap` | table | warning — "data-preserving ES|QL table" |
| `scatterplot` | XY | warning |
| `change` | table | warning — "comparison shift" |
| `bar_chart` / `toplist` | table | deliberate: same grouped-scalar data shape |
| `group` | (structural) | skipped |

Not yet reviewed to this depth: Grafana alert rules, and Datadog widgets driven
by log/APM/RUM data sources rather than metrics.

---

## Fixed this cycle — kept as cautionary notes

These are resolved. They are recorded because each one looked like something
other than what it was.

- **Oracle control bindings were merged across dashboards.** `job` from the
  PostgreSQL pack (`postgres`) overwrote Node Exporter Full's (`.*`), so those
  panels were filtered by another dashboard's default, matched nothing, and were
  reported as translation FAILs. Four corpus FAILs, one of which compared at 0.0
  relative error once fixed — the translation had been exact all along.
- **Per-target filters were ANDed across a fused panel.** No document carries
  every target's metric, so the query matched nothing and the panel rendered
  *empty rather than erroring* — nothing to notice, nothing to grep.
- **The comparison window ignored available data.** 174 of 356 panels reported
  "no overlapping time buckets" purely because the window reached past the data.
  Indistinguishable at a glance from a mass translator failure.
- **The CASE-shape normaliser recreated the shape it exists to remove.** A comma
  inside a CASE condition (`COALESCE(label, "")`) hid the outer shape from a
  `[^,]+` regex.

The through-line: every one of these produced a *plausible* result rather than an
error. A gate whose failures are not trustworthy gets ignored, so prefer
refusing, warning, or classifying honestly over emitting something that looks
like it worked.

---

## 0f. Grafana's default reducer was read as "unspecified", not as lastNotNull

**Status:** FIXED (partially -- see the Sys Load caveat below).

`reduceOptions.calcs` is absent on most scalar panels, and `_panel_reduce_calc`
returned `""` for them. Two things then went wrong at once:

  * the summary collapse fell back to `MAX`, so a gauge showed the range PEAK;
  * the scalar bucket optimisation replaced the adaptive bucket with
    `TBUCKET(1, ...)`, and over ONE whole-range bucket `AVG(field)` is the range
    MEAN, not the current value.

Grafana defaults stat/gauge/bargauge to `lastNotNull`, so an absent `calcs` is
not "unspecified" -- it is `lastNotNull`, and neither fallback expresses it.
`_panel_reduce_calc` now returns `lastNotNull` for scalar panel types, and
`_reduce_calc_survives_one_bucket` keeps the adaptive bucket unless the reducer
is order-independent (mean/min/max/sum/count).

Measured on the rig, Node Exporter Full at a 12 h range, both builds using
`--field-profile auto`: 311 comparisons before and after, disagreements 54 -> 53.
So this is a correctness repair with no denominator loss, but a SMALL net win --
one panel.

**It did not fix the panel that motivated it.** "Sys Load" still reads 3.15
against Prometheus's 7.8. The bucket is now `BUCKET(@timestamp, 50, ...)` with a
`LAST` collapse as intended, so the remaining error is a DIFFERENT defect:
`AVG(metrics.node_load1)` averages across every exporter instance in the index
while Prometheus's `scalar(node_load1{instance="$node"})` picks one, and
`COUNT_DISTINCT(labels.cpu)` spans instances the same way. Cross-instance
aggregation on a panel whose PromQL scopes to a single instance is the open
problem; see also 0d for grouped boundary buckets.

**Method note.** The first measurement of this change looked like a huge win
(disagreements 55 -> 8) and was WRONG: it compared a `--field-profile auto` build
against a `prometheus_native` one, so the profile, not the code, produced most of
the difference. It also hid a 311 -> 161 DENOMINATOR COLLAPSE -- the non-auto
profile dropped `BY labels.collector` grouping and took "Node Exporter Scrape
Time" from 48 series to 1. Always hold the field profile fixed across a
before/after, and always read the denominator, not just the disagreement count.

### Open: does `prometheus_native` without discovery lose label breakdowns?

Migrating with `--field-profile prometheus_native` (no `--es-url`) emitted
`... BY time_bucket` where `--field-profile auto` emitted
`... BY time_bucket, labels.collector`, costing 47 of 48 series on one panel and
~150 series across the dashboard. This may be deliberate -- without discovery the
translator cannot confirm the label exists -- but it is silent, and an operator
who picks the explicit profile over `auto` gets materially poorer dashboards with
no warning. Not investigated further.

---

## 0g. A bare gauge selector was averaged across TIME, not just across series

**Status:** FIXED.

`node_scrape_collector_duration_seconds{...}` became
`AVG(metrics.node_scrape_collector_duration_seconds) BY time_bucket, collector`.
That aggregate collapses two axes at once. A bare instant-vector selector has a
value at each step -- the most recent sample at or before it -- so the per-bucket
collapse must be `LAST_OVER_TIME` (across TIME) and only the across-SERIES
combine should use the gauge default:

    AVG(LAST_OVER_TIME(field))     not     AVG(field)

The counter branch already did this (`MAX(LAST_OVER_TIME(...))`); the gauge
branches did not. There were THREE near-duplicate copies of this logic --
`promql.py` (`can_use_ts_aggregated_gauge`), `translate.py` (same branch name),
and `translate.py`'s `stats_expression_rule` -- and the first two edits I made hit
copies that the dashboard path does not use. Worth collapsing into one helper.

`LAST_OVER_TIME` is TS-only, so the FROM path still emits the plain aggregate.
That is why "Sys Load" (FROM, binary op with `COUNT_DISTINCT`) still reads its
bucket mean. Open.

**Measured.** Per-collector on the rig, against Prometheus:

| collector | before | after | prometheus |
|---|---|---|---|
| nfs | 0.0123 (5276% off) | -- | 0.000228 |
| arp | -- | 0.000103 | 0.000161 |
| cpu | -- | 0.000187 | 0.000367 |

Before, ours was two orders of magnitude high; after, it is the same order as
Prometheus.

**But the harness's own differ count did NOT improve measurably** -- 52 before,
54 after, and the Scrape Time panel alone swung 43/47/46 across three runs of the
SAME build. `node_scrape_collector_duration_seconds` varies 2-3x between
consecutive scrapes, and the values check compares our last COMPLETE bucket
(~14 min old at a 12 h range) against Prometheus's instant value. No tolerance
will make a noisy per-scrape gauge agree across a 14-minute offset. This panel's
46 series are ~85% of the dashboard's disagreements, so the headline differ count
for Node Exporter Full is dominated by a measurement artifact, not by translation
defects. Either compare at a matched instant or exclude per-scrape-noisy gauges
before reading that number as a quality signal.

### Regression this caused, and the narrowing that fixed it

Wrapping gauges in `LAST_OVER_TIME` tripped the penultimate-bucket collapse,
because its gate matched `[A-Z_]+_OVER_TIME` as well as the counter functions.
"Root FS Used" went blank: at a short window the bucket it stepped back to was
empty. The gate now matches only the DERIVATIVE family (RATE/IRATE/INCREASE/
DELTA/DERIV), whose value in a partial boundary bucket is genuinely wrong. The
last sample inside a partial bucket is a perfectly good last sample.

---

## 0h. The values gate counted irreducible sampling noise as translation defects

**Status:** FIXED (in the harness, not the translator).

Two independent problems made the values dimension's number untrustworthy.

**1. It compared two instants a full bucket apart.** `time_bucket` is the bucket's
START, but a bucket's value describes its whole SPAN -- `LAST_OVER_TIME` returns
the last sample in it, near the END. At a 12-hour range that is a ~7-minute
offset. `_bucket_end` now derives the bucket width from the spacing between
consecutive starts and compares there, degrading to the old behaviour when the
spacing cannot be determined.

**2. Some metrics cannot be compared at a point at all.** Elasticsearch and
Prometheus scrape INDEPENDENTLY, so for a metric that swings between consecutive
scrapes they hold genuinely different samples. Measured on the rig for
`node_scrape_collector_duration_seconds{collector="arp"}`:

    ES    16:23:52  9.5e-05      (scrapes every 10s at :02, :12, :22, ...)
    PROM  16:23:56  3.53e-04     (scrapes every 60s at :56.121)

Four seconds apart, 3.7x. No choice of comparison instant fixes that. `_self_noise`
now asks the REFERENCE how much it moves between adjacent instants, and a series
whose own reference moves more than the tolerance is counted UNMATCHED rather than
DIFFERING. This is self-calibrating -- there is no per-metric allowlist to drift.

**Effect at a 12-hour range on Node Exporter Full:**

| | agree | differ | unchecked |
|---|---|---|---|
| before | 256 | 54 | 83 |
| bucket-end only | 262 | 49 | 82 |
| + noise filter | 261 | **9** | 123 |

and the number is now STABLE run to run (it previously swung by 3-5, with the
Scrape Time panel alone reporting 43/46/47 across three runs of the SAME build).

**The cost, stated plainly:** `unchecked` rose from 83 to 123, so the compared set
shrank from ~311 series to ~270. Forty-one series are now excused as uncomparable
rather than verified. That is justified by the evidence above, but it IS reduced
coverage, and the fix for it is rig-side -- align the two scrape streams, or seed
a metric that does not swing -- not a further loosening of the gate.

The 9 that remain are real candidates worth chasing: CPU Busy (12%), Sys Load
(24%, the FROM-path bucket mean of 0f), CPU time in user/system contexts (33%),
and one Scrape Time collector (rapl, 48%).

---

## 0i. PROMQL passthrough compared at a 1-minute-offset instant

**Status:** FIXED (in the harness, not the translator).

Three of the four remaining disagrees after 0h — CPU Busy (12%), CPU time in
user/system contexts (33%), and one Scrape Time series (rapl, 48%) — all use
the PROMQL passthrough form (`PROMQL index=metrics-* step=1m value=(...)`).

The `_our_last_bucket_series` function in `dashboard_qa.py` treated all
time-indexed queries the same:
1. Pick the penultimate step (avoid the partial final bucket).
2. Call `_bucket_end` to advance the comparison instant by one bucket width.

For ES|QL TBUCKET queries this is correct: `time_bucket` is the bucket START,
`LAST_OVER_TIME` returns the last sample near the END, so advancing from start
to end aligns the comparison with what Prometheus would compute at that point.

For PROMQL passthrough, `@timestamp` IS the evaluation instant — the 5m
lookback window ends AT `@timestamp`, not at `@timestamp + step`. Advancing
by one step (1m) asked Prometheus about a completely different 5m window:

    ES evaluates at t:        rate over [t-5m, t]
    Prometheus asked at t+1m: rate over [t-4m, t+1m]

Those windows overlap by 4 minutes and differ by 2 minutes. CPU usage can
change 12–33% in 2 minutes, which is exactly what was measured.

A second error amplified this: the PROMQL last step is valid (the PromQL
lookback is fixed and does not depend on bucket bounds), so taking the
penultimate step and then advancing by 1m compared the value at step n-1
against Prometheus at step n+1 — a 2-step offset rather than 1.

**Fix:** detect PROMQL queries by the first line (`lines[0].startswith("PROMQL")`),
take `stamps[-1]` (the last step), and return that timestamp directly without
`_bucket_end` advancement.  ES|QL TS queries keep the existing
penultimate-bucket + advance logic unchanged.

Sys Load (24%, FROM path) is a different defect — cross-instance AVG on a
panel that scopes to a single instance — and is still open.
