# Open Problems

Known-unsolved issues in the migration engine, with the evidence gathered so far
so the next person does not repeat the investigation. Each entry says how it was
found, what is proven, and what is still unknown.

Everything here is reproducible against the curated rig
(`parity-rig/curated/grafana_763_redis_exporter/docker-compose.yml`, ES on
`:9201`) and the pinned community corpus
(`python scripts/fetch_community_corpus.py --output-dir <dir>`, 69 dashboards).

---

## 0. Kubernetes / Views / Namespaces "Overview" row is ragged

**Status:** real defect, found by `scripts/dashboard_qa.py`, not yet fixed.

The Grafana source row is flush -- a 7+4 stack beside an 11-high panel, every
column ending at 12. We emit columns 0..23 ending at 21 and 24..47 at 29, so the
right-hand panel hangs eight rows below its neighbours.

The band-uniform height pass reduced corpus-wide raggedness from 3 rows to this
one, so it is a residual rather than a regression. Something after that pass
(`_compact_vertical_gaps`, or a width bump changing band membership) is moving
the panels apart again; the band scale itself preserves flushness by
construction.

Reproduce: `python scripts/dashboard_qa.py --migration-out <out>/dashboards
--skip queries --skip render`.

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
