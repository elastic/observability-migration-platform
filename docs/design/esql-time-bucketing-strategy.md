# ES|QL Time-Bucketing Strategy — Design

**Status:** implemented (2026-08-12). §4.1/§4.2 landed in `panels.py`
(`_rule_pack_for_panel`); §4.3's premise was corrected during implementation
(see note below) rather than requiring a code change; part of §5 step 5
(curated-pack cleanup) landed alongside the engine change once live-bug
auditing turned up more instances of the same class of bug. See §7 for the
as-built summary.
**Scope:** how migrated dashboard queries (Grafana PromQL and Datadog) pick
`TBUCKET`/`BUCKET` width in ES|QL, and how that interacts with `RATE`/`IRATE`/
`INCREASE`. Triggered by a live bug: Node Exporter Full's "Interrupts Detail"
panel rendered legends but no chart data after the 2026-08-12 legend fix (see
`node-exporter-1860-curation-workplan.md`) — root cause was the default
adaptive bucket count landing below the source scrape interval.

## Goal

Stop picking bucket counts that can land narrower than the source's real
sample cadence, without losing the "resolution grows/shrinks with the
dashboard time picker" property that already fixed the worse bug in
`docs/design/open-problems.md` §0e (rate reading 2–5× high on long ranges).

---

## 1. Problem statement

Grafana keeps two independently-sized clocks for a Prometheus panel:

```text
step             ≈ (range / panel width), floored by Min interval/scrape   — chart resolution
$__rate_interval = max(step + scrape, 4 × scrape)                          — rate() lookback
```

Our ES|QL translation collapses both into **one** knob: the `BY time_bucket =
TBUCKET(N, ?_tstart, ?_tend)` grouping. `RATE`/`IRATE`/`INCREASE` are emitted
**windowless** on purpose (`counter_range_window` postprocessor, `promql.py`
`_range_call`) so they compute over *the bucket itself* — this is what fixed
the long-range 2–5× overread in open-problems.md §0e. But it also means the
bucket now has to satisfy Grafana's *rate-lookback* floor (`≥ ~4×scrape`), not
just its *chart-resolution* target — and today it never does, because `N` is a
single static constant with no scrape awareness.

### Live confirmation (2026-08-12, ES 9.5.0-SNAPSHOT, 10s scrape, 15m range)

Re-tested with an explicit `WHERE @timestamp >= ?_tstart AND @timestamp <=
?_tend` guard (important: `TBUCKET` does **not** filter rows, only sizes
buckets, so an unguarded probe can silently include historical data and give a
false read on bucket width — this cost one iteration during the investigation):

| `TBUCKET(N, …)` | Width chosen | `IRATE` non-null rows |
|---|---|---|
| **100** (current TS default, `panels.py` `_NATIVE_ESQL_ADAPTIVE_TBUCKET`) | **10s** | **0 — chart is empty** |
| 75 (Kibana Lens's own ES|QL auto-interval constant) | 30s | 120 |
| 50 (current `FROM`-path default, `rules.py` `from_bucket`) | 30s | 120 |
| 20 (hand-tuned curated-pack override) | 60s | 60 |

This matches ES's documented bucket-picking algorithm exactly: for a target
count it walks a fixed "human" rounding ladder (`…1h→30m→10m→5m→1m→30s→10s→5s→1s…`)
and picks the finest rung that still yields ≤N buckets. 900s/100=9s raw → finest
rung ≤100 buckets is 10s (90 buckets). 900s/20=45s raw → 30s gives 30>20, so it
bumps to 60s (16 buckets).

**The current default (`N=100`) reliably produces dead rate panels whenever the
default/short dashboard range divided by 100 lands at or below the source
scrape interval** — which is common: 15m/30m/1h default ranges against typical
10–15s Prometheus scrapes.

---

## 2. Confirmed engine semantics (corrected from initial research pass)

Verified against the local Kibana checkout, current published Elastic docs,
and live queries against ES 9.5.0-SNAPSHOT — **not** against the
`/Users/subhamsarkar/py-grok/ESQL_REAL_SOURCE_2025/elasticsearch` checkout,
which is pinned to `main` @ 2025-11-18 and predates some of this behavior. Two
claims from that source read did not hold up and are corrected here so they
don't leak into implementation:

1. **`TBUCKET(N, from, to)` — the count+range form — is real, not a
   misreading of `BUCKET`.** GA since 9.4. Confirmed both via current
   published docs and live execution against our stack. (The stale checkout's
   `TBucket.java` only had the 2-arg duration constructor; that's simply an
   older snapshot, not evidence the form doesn't exist.)
2. **There is no hard engine-side rejection of a `RATE(field, window)` whose
   window is smaller than, or not a multiple of, its `TBUCKET` span.** Tested
   live: 30s/90s/120s windows against a 60s bucket all executed without error,
   each producing a numerically *different* result. So a window/bucket
   mismatch is a **silent wrong number**, not a compile-time safety net —
   consistent with why we removed the window argument entirely rather than
   trying to keep it in sync (open-problems.md §0e).

What *is* confirmed and load-bearing for this design:

- `TBUCKET`/`BUCKET` count-mode picks the finest rung of a fixed rounding
  ladder that yields ≤N buckets over the given range — not "range/N" division.
  No minimum width is enforced; you can legally get sub-scrape-interval
  buckets, and nothing warns you.
- `TBUCKET` (and `BUCKET`'s span mode) **never filters rows** — `from`/`to`
  only pick a width. Row filtering is a separate `WHERE @timestamp` concern
  (see §5).
- `RATE`/`INCREASE` extrapolate to the boundaries of whichever time-bucket
  grouping is present (PromQL-style edge extrapolation); with no time-bucket
  grouping there's no extrapolation at all. `IRATE` only ever looks at the
  last two raw samples in a group — it has no window/bucket interaction, but
  it still needs ≥2 samples inside whatever grouping key it's given, so a
  too-fine bucket starves it the same way.
- Kibana's own Lens ES|QL bridge uses **`BUCKET(col, 75, ?_tstart, ?_tend)`**
  for its "auto" interval (`x-pack/platform/plugins/shared/lens/public/datasources/form_based/date_histogram_esql.ts:21`),
  deliberately *not* the classic `histogram:barTarget` default of 50, so the
  client-computed interval label agrees with what the server will actually
  pick. This is Kibana's own precedent for a "one adaptive count for
  everything" default, and it's neither of our two current constants (50/100).

---

## 3. How the source systems handle this (for parity reference)

### Grafana / PromQL

```text
$__interval      = f(range, panel width / maxDataPoints, Min interval, ≤11k pts)
$__rate_interval = max($__interval + scrape, 4 × scrape)
```

`step` and rate lookback are independent; `rate()`/`irate()` never see `step`.
Community dashboards almost always use `$__rate_interval` (or a literal `5m`)
for the lookback, decoupled from chart resolution. Grafana's floor guarantees
the lookback window is always ≥4 scrapes even when the chart itself is drawn
at native scrape resolution.

### Datadog

Always rollups before space-aggregating; defaults coarsen by wall-clock window
per an official discrete table (past hour → 20s, past day → 5m, past week →
1h, …), point budget ~1500 (timeseries) / ~150 (query value). `.rollup(N)`
lets an operator pin an explicit width; `diff()`/`monotonic_diff()`/`as_rate()`
all operate on the *rolled-up* series, so rollup width is part of their
semantic contract, not just a display knob.

### What this repo does today (both sources)

| Path | Bucket | Rate window |
|---|---|---|
| Grafana TS XY (`panels.py` `_NATIVE_ESQL_ADAPTIVE_TBUCKET`) | `TBUCKET(75, ?_tstart, ?_tend)` (20 when rate-safe) | windowless `RATE`/`IRATE` |
| Grafana `FROM` (`panels.py` adaptive override of `from_bucket`) | `BUCKET(@timestamp, 75, ?_tstart, ?_tend)` (20 when rate-safe) | — |
| Grafana scalar panels (safe reducers) | `TBUCKET(1, ?_tstart, ?_tend)` | avoided for range functions (`_panel_uses_range_function`) |
| Native `PROMQL` emission path | `buckets=50` | `$__rate_interval` kept genuinely adaptive/windowless |
| Datadog `FROM` (`translate.py` `TIME_BUCKET_EXPR`) | `BUCKET(@timestamp, 75, ?_tstart, ?_tend)` (20 when rate-safe) | — |
| Datadog `TS` rate path | `TBUCKET(20, ?_tstart, ?_tend)` adaptive | windowless `RATE(field)` / `INCREASE(field)` |
| Curated packs (node-exporter-1860, redis-763) | hand-tuned `TBUCKET(20)` / `TBUCKET(2 minute)` | manual, discovered per-dashboard by audit |

Two structural problems originally fell out of this table (both now resolved —
Grafana via this document's rollout; Datadog via
[`datadog-esql-time-bucketing-adaptivity.md`](./datadog-esql-time-bucketing-adaptivity.md)):

- **`FROM`(50) and `TS`(100) used to disagree** on the "same" adaptive default
  with no shared constant. Both Grafana paths now share `_ADAPTIVE_CHART_BUCKETS
  = 75` / `_ADAPTIVE_RATE_BUCKETS = 20`.
- **Datadog's `TS` rate path never adapted to the dashboard range** (fixed `5
  minute`) while the `FROM` path did (`BUCKET(...,50,…)`). Both Datadog paths
  now use the same adaptive 75/20 split; the `TS` rate path is windowless.

---

## 4. Proposed design

### 4.1 Unify the "chart resolution" default to N=75

Replace the `FROM`-path 50 and `TS`-path 100 defaults with a single shared
constant, anchored to Kibana's own Lens ES|QL precedent rather than inventing
a new number:

```python
# panels.py / rules.py
_ADAPTIVE_CHART_BUCKETS = 75   # matches Kibana Lens AUTO_TARGET_NUMBER_OF_BUCKETS
```

Applies to gauge/gauge-like panels and any panel with no counter range
function — unchanged behavior class, just one number instead of two.

### 4.2 Give counter-range panels a coarser, rate-safe default: N=20

`panels.py` already has `_panel_uses_range_function()` — the exact helper that
currently protects the *scalar*-panel `TBUCKET(1)` optimization from breaking
`rate()`-based reducers. Reuse it on the **XY-panel** path too: when a panel's
targets include `rate`/`irate`/`increase`/`delta`/`deriv`/`*_over_time`, select
`TBUCKET(20, ?_tstart, ?_tend)` instead of `TBUCKET(75, ?_tstart, ?_tend)`.

`N=20` is not arbitrary — it's the value curated-pack authors already
converged on empirically for Node Exporter Full's CPU Busy/Pressure panels,
and it reproduced cleanly in §1's live re-test (60s width, 60/60 non-null
`IRATE` rows on a 10s-scrape host). On the repo's default 15m range this gives
a bucket ≈4× a 15s scrape (matching Grafana's own `4×scrape` floor
conceptually, not by derivation) and ≈6× our lab's 10s scrape.

```text
current:  TBUCKET(100, ...)  → dead below ~1000s / 100 = 10s scrape-equivalent range
proposed: TBUCKET(20, ...)   → dead only below ~1000s / 20  = 50s scrape-equivalent range
```

This does not fully solve the problem (see §4.4) — it moves the failure
threshold from "any scrape ≥9s on a 15m range" to "any scrape ≥45s on a 15m
range," which covers the overwhelming majority of real Prometheus/Datadog
scrape cadences (10–60s) without needing live scrape discovery.

### 4.3 Add a defensive `WHERE @timestamp` bound to every emitted query

New finding from this investigation, not previously flagged: `node-exporter-1860`'s
curated overrides have **no explicit `@timestamp` filter** — they rely
entirely on Kibana injecting the dashboard's global time range at render time
for a Lens/ES|QL panel (a real, documented Kibana behavior: *"the range can be
derived automatically from the `@timestamp` filter that Kibana adds to the
query"*). `redis-763`'s curated overrides **do** add an explicit `WHERE
@timestamp >= ?_tstart AND @timestamp <= ?_tend`. Both work today only because
every current consumer is a live Kibana dashboard render.

Since `TBUCKET`/`BUCKET` never filter rows on their own, any future non-dashboard
consumer of these ES|QL strings (a validation script, an export, a
programmatic `_query` replay) would silently scan **all history** for a
1860-style query while a 763-style query stays correctly bounded. Proposal:
make the explicit `WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend`
guard **mandatory** in the generic panel-emission path (not just something
individual curated-pack authors remember to add), so it's uniform across
Grafana and Datadog, generic and curated. This is pure defense-in-depth; no
behavior change for existing dashboard renders.

### 4.4 Explicitly out of scope for v1 (flagged, not solved)

- **Live scrape-interval discovery.** `SchemaResolver` already does live
  field-caps discovery for `live_optional_metrics`; extending it to sample
  actual scrape cadence and choose N dynamically is the "real" fix but adds
  new discovery machinery and a fallback-when-offline story. Tracked as
  follow-up; N=20 is deliberately conservative enough to not need it for the
  common case.
- **Datadog `TS` rate path adaptivity.** Originally out of scope here; now
  resolved in
  [`datadog-esql-time-bucketing-adaptivity.md`](./datadog-esql-time-bucketing-adaptivity.md)
  (adaptive windowless `TBUCKET(20, ?_tstart, ?_tend)` + unified `FROM`-path
  75/20 split).
- **Per-dashboard/per-panel configurability.** No new `pack.yaml` knob is
  proposed; N=75/20 become the new generic defaults, and existing curated
  `panel_query_overrides` (which hardcode their own `TBUCKET(...)`) are
  untouched by this change — they already encode a hand-verified answer.

---

## 5. Rollout plan

1. **Engine change** — add `_ADAPTIVE_CHART_BUCKETS = 75` and
   `_ADAPTIVE_RATE_BUCKETS = 20` (naming TBD) to `panels.py`; extend
   `_rule_pack_for_panel` to pick between them using the existing
   `_panel_uses_range_function` check, for the non-scalar (XY/timeseries)
   case. Unify `rules.py`'s `from_bucket` default to the same 75/20 split so
   `FROM` and `TS` no longer diverge for no reason.
2. **Defensive `@timestamp` bound** — add the explicit `WHERE @timestamp >=
   ?_tstart AND @timestamp <= ?_tend` to the generic emission path (both
   Grafana and Datadog), matching `redis-763`'s existing pattern.
3. **Tests** — unit coverage in `tests/test_grafana_issues_316_319.py` /
   `tests/test_curated_packs.py` asserting: a rate-family XY panel gets
   `TBUCKET(20,...)`, a gauge/no-rate XY panel gets `TBUCKET(75,...)`, scalar
   panels are unaffected, and dashboard artifacts continue to omit redundant
   `@timestamp` guards because Kibana injects the dashboard time picker at
   render time (`tests/test_grafana_extended.py`).
4. **Regression gates** (per `docs/contributing/dev-commands.md`) — full
   `verifier.live_validate`, `obs-migrate compare` + `verifier.corpus_gate`,
   `verifier.benchmark_gate`, `verifier.scorecard`, and `render_audit_driver`
   against the pinned corpus before merge. Watch denominators
   (`panels_total`, `verification_total`), not just percentages — this change
   should only ever *remove* empty-chart failures, never introduce new
   render/schema failures.
5. **Curated-pack cleanup (follow-up, separate PR)** — once the generic path
   defaults to `TBUCKET(20,...)` for rate panels, re-audit whether
   `node-exporter-1860`'s hand-tuned `TBUCKET(20)` overrides (CPU Busy,
   Pressure) and `redis-763`'s `TBUCKET(2 minute)` overrides can be retired in
   favor of the generic default, shrinking pack-specific surface area. Do not
   bundle this with step 1–4; the curated overrides also carry other
   panel-specific logic (penultimate-bucket collapse, PSI unpivoting) that
   must be re-verified independently before removal.

## 6. Open questions (resolved during implementation)

1. **Should `_ADAPTIVE_RATE_BUCKETS` be 20 (proven) or derived (e.g.
   `_ADAPTIVE_CHART_BUCKETS / 4`)?** Went with the bare, separately-documented
   constant (`20`), not a derived expression. `75/4 = 18.75` isn't the value
   that was actually live-verified (`20`, §1's live table) — deriving it would
   silently change the proven number if `_ADAPTIVE_CHART_BUCKETS` is ever
   retuned. Two independent constants, each with a comment explaining its own
   provenance, is more honest than a formula that implies a relationship that
   was never tested.
2. **Does the `@timestamp` guard from §4.3 need to special-case existing
   curated overrides with bespoke `WHERE` ordering?** §4.3 was too broad as
   originally written. The translator's intermediate ES|QL generally carries
   `| WHERE {rp.ts_time_filter}` / `| WHERE {rp.from_time_filter}` (default
   `@timestamp >= ?_tstart AND @timestamp <= ?_tend`), but
   `_normalize_esql_panel_query()` deliberately strips that dashboard time
   filter from native dashboard artifacts because Kibana applies the global
   time picker implicitly at render time. This behavior is test-pinned by
   `tests/test_grafana_extended.py::test_dashboard_esql_omits_*timestamp*`.
   Raw artifact validation is the place that needs an explicit guard:
   `scripts/validate_panels_from_artifacts.py` re-injects the same time filter
   before calling `_query`, so non-dashboard validation does not scan all
   history. Curated `panel_query_override` strings may still include a
   hand-written `@timestamp` guard when they are meant to be replayed directly,
   but the dashboard-native output does not require one for correctness.
3. **Is Datadog `TS`-rate adaptivity in scope now?** Originally no — left out
   of scope per §4.4 rather than ported opportunistically, because Datadog's
   `TS` path deserved its own live-verified default rather than inheriting
   Grafana's numbers by analogy.
   **Resolved in `docs/design/datadog-esql-time-bucketing-adaptivity.md`**:
   independently live-verified `N=20` is also the right Datadog default, and
   both Datadog paths are now unified (`FROM` 50→75/20, `TS` fixed `5 minute`
   → adaptive windowless) on the same pattern.

## 7. As-built summary

- **§4.1/§4.2 (engine defaults):** `panels.py` now defines
  `_ADAPTIVE_CHART_BUCKETS = 75` and `_ADAPTIVE_RATE_BUCKETS = 20`, and
  `_rule_pack_for_panel` picks between `TBUCKET(75,...)` /`TBUCKET(20,...)`
  (and the `BUCKET(@timestamp, 75/20, ...)` FROM-path mirror) based on
  `_panel_uses_range_function`, for every panel not already pinned by an
  explicit Grafana `interval` or collapsed by the scalar-panel optimization.
  This is a **generic-engine** change — it affects every dashboard migrated
  through the normal (non-curated) path, not just node-exporter-1860.
- **§4.3 (defensive `@timestamp` bound):** corrected, not implemented as a
  dashboard-artifact change. Dashboard-native ES|QL intentionally omits
  redundant `@timestamp` guards and relies on Kibana's injected time picker;
  raw validation scripts re-inject the guard before `_query`. The curated-pack
  cleanup therefore focused on the bucket-width bug itself:
  `node-exporter-1860` (6 panels: TCP Errors, Average Queue Size, Network
  Traffic Multicast/Frame/Carrier/Colls — all `AVG(IRATE(...))
  BY TBUCKET(100,...)`, the exact bug class that motivated this doc) and
  `redis-11835`'s Network I/O panel (`SUM(RATE(...)) BY TBUCKET(100,...)`,
  which also gained a hand-written `WHERE @timestamp` guard to match
  `redis-763`'s equivalent panel). The remaining 17 gauge-only
  (`LAST_OVER_TIME`-only, no rate function) curated overrides at
  `TBUCKET(100,...)` across `node-exporter-1860`/`redis-763`/`redis-11835`
  were bumped to `TBUCKET(75,...)` for consistency with the new generic
  default — cosmetic-only, no correctness change, since a non-rate reducer
  is order-independent of bucket width.
- **§5 step 5 (curated-pack cleanup):** partially landed as a side effect of
  the audit above (the 6+1 rate-function bucket-width fixes), not as a full
  removal of the overrides themselves — they still carry other
  panel-specific logic (custom legends, PSI unpivoting, penultimate-bucket
  collapse) that is out of scope for this change and must be re-verified
  independently before any override is deleted outright.
- **Tests:** `tests/test_grafana_issues_316_319.py` pins the 75/20 split
  (TS and FROM paths) directly against `_rule_pack_for_panel`;
  `tests/test_curated_packs.py`/`tests/test_migrate.py`/
  `tests/test_grafana_extended.py` expectations updated for the new default
  bucket counts. Full suite green (5872 passed), `make lint` and `make
  typecheck` clean.

## Definition of done

- [x] `panels.py`/`rules.py` emit one of exactly two adaptive bucket counts for
  non-scalar panels — 75 (default) or 20 (counter range functions) — with no
  remaining `TBUCKET(100,...)` in generated output for the generic path.
- [x] Dashboard-native ES|QL keeps the existing, test-pinned behavior: Kibana
  applies the dashboard time picker implicitly, so emitted dashboard queries
  omit redundant `@timestamp` guards. Raw validation paths re-inject
  `@timestamp >= ?_tstart AND @timestamp <= ?_tend` before `_query`, so
  non-dashboard checks are still bounded.
- [x] New unit tests pin the N-selection logic; full test suite (`make test`
  equivalent) is green with no new empty/error panels.
- [x] This document's §4.4 scope-out items are tracked rather than silently
  dropped: Datadog `TS`-rate adaptivity is resolved in
  `docs/design/datadog-esql-time-bucketing-adaptivity.md`. Live scrape/flush
  discovery and per-pack configurability remain open, shared follow-ups
  (not yet filed as separate issues as of this writing).
