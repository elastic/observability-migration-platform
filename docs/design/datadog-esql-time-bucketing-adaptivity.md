# Datadog ES|QL Time-Bucketing Adaptivity — Design

**Status:** implemented (2026-08-12). Follow-up to
`docs/design/esql-time-bucketing-strategy.md` §4.4/§6 item 3, which
deliberately scoped Datadog's `TS`-rate path out of that PR ("Datadog's `TS`
path has its own separate fixed-window constant... and deserves its own
live-verified default rather than inheriting Grafana's numbers by analogy").
This document does that live verification and extends the same 75/20
adaptive-bucket pattern to both Datadog query paths.

**Scope:** unify Datadog's `FROM`-path bucket default (`BUCKET(@timestamp, 50,
...)`, `translate.py` `TIME_BUCKET_EXPR`) with the Grafana-side 75/20 split,
and replace the `TS`-path's fixed `TBUCKET(5 minute)` / `RATE(field, 5
minute)` with adaptive, windowless bucketing — the same fix already proven
for Grafana's PromQL `rate()`/`irate()` in `docs/design/open-problems.md` §0e.

## 1. Problem statement

Datadog's two ES|QL emission paths disagree with each other and with
Grafana's now-unified default:

| Path | Bucket | Rate window |
|---|---|---|
| Datadog `FROM` (`translate.py` `TIME_BUCKET_EXPR`) | `BUCKET(@timestamp, 50, ?_tstart, ?_tend)` | — |
| Datadog `TS` rate path | `TBUCKET(5 minute)` fixed | `RATE(field, 5 minute)` (window kept) |
| Grafana (both paths, post-unification) | `TBUCKET(75\|20, ?_tstart, ?_tend)` | windowless |

Two independent problems fall out of this:

- **`FROM`(50) never adopted the Kibana Lens/Grafana-unified 75/20 split.**
  Same "one adaptive count for everything, tuned per whether the query needs
  rate safety" property Grafana already has is missing here.
- **`TS`'s fixed `5 minute` bucket/window never adapts to the dashboard
  range.** Live-tested below: it is rate-safe at realistic Datadog cadences,
  but resolution is wrong in both directions — too coarse on short ranges
  (a 15-minute dashboard gets 3-4 buckets total), and unboundedly numerous on
  long ranges (a 7-day dashboard would get ~2016 buckets, far past Kibana
  Lens's own point budget).

Unlike Grafana's original bug (an *adaptive* bucket that could land
narrower than the source cadence), Datadog's `TS` bucket and window are
already textually identical (`TBUCKET(5 minute)` / `RATE(field, 5 minute)`),
so a window/bucket **mismatch** was never possible here — the defect is pure
non-adaptivity, not silent divergence. That changes the fix: adopting an
adaptive *count* form for the bucket removes the ability to keep an explicit
matching duration for the window (ES picks the width internally; we don't
know it ahead of the query), so the window argument has to be dropped
entirely — converging on the same windowless `RATE`/`INCREASE` form Grafana
already uses, rather than Datadog's own bespoke "windowed but fixed" shape.

## 2. Live confirmation (2026-08-12, ES 9.5.0-SNAPSHOT)

Reproduced the same live-verification methodology as
`esql-time-bucketing-strategy.md` §1: created a real `time_series`-mode data
stream (`metrics-parity_verify-default`, counter-mapped field, matching what
`seed-sample-data`'s telemetry contract would create) on the `redis-rig`
ES/Kibana stack, ingested synthetic monotonic-counter documents at two
cadences representative of real Datadog agent/integration flush intervals,
queried windowless `RATE(field)` grouped by `TBUCKET(N, ?_tstart, ?_tend)`
across Datadog's own dashboard-range presets, then tore the verification
stream down.

### 15s cadence (fast agent flush)

| `TBUCKET(N, …)` | 5m range | 15m range | 1h range | 2h range |
|---|---|---|---|---|
| **75** (Kibana Lens default) | 15s width — **0/21 non-null** | 30s, 31/31 | 60s, 61/61 | 300s, 25/25 |
| 50 | 10s width — 20/21 non-null | 30s, 31/31 | 300s, 13/13 | 300s, 25/25 |
| **20** | 30s width — **11/11** | 60s, 16/16 | 300s, 13/13 | 600s, 13/13 |
| 10 | 30s width — 11/11 | 300s, 4/4 | 600s, 7/7 | 1800s, 5/5 |

`N=75` reproduces exactly the same empty-chart bug class documented for
Grafana at this cadence; `N=20` stays fully non-null across every tested
range.

### 60s cadence (a common slower Datadog integration flush)

| `TBUCKET(N, …)` | 5m range | 15m range | 1h range | 2h range |
|---|---|---|---|---|
| 10 / 15 / 20 / 50 / 75 | 60s width — **0/6 non-null, every N** | 16/16 (N≤50) | 13/13 or 61/61 | 13/13 or 25/25 |

**Finding:** at Datadog's shortest supported global-range preset (5
minutes) against a 60s-cadence counter, *no* value of `N` fixes it — the
range contains only 5 samples total, so any split finer than the whole
window leaves ≤1 sample per bucket. This is a data-cadence floor (not enough
raw samples in view for a rate to be computable at all), not something the
bucket-count knob can solve, and lowering `N` below 20 doesn't rescue this
corner (`N=10` fails identically) while it does cost resolution everywhere
else. This is the same class of residual gap
`esql-time-bucketing-strategy.md` §4.4 already accepted for Grafana ("N=20
is deliberately conservative enough... not a live scrape-interval discovery
fix") — not a new regression introduced by this change.

**Conclusion:** `N=20` is independently live-verified as the right rate-safe
default for Datadog, for the same reason it was right for Grafana — not
inherited by unverified analogy.

## 3. Proposed design

### 3.1 Unify the `FROM`-path chart-resolution default to N=75

```python
# translate.py
_ADAPTIVE_CHART_BUCKETS = 75   # matches Grafana's panels.py constant of the same name and its
                                # underlying Kibana Lens AUTO_TARGET_NUMBER_OF_BUCKETS precedent
```

Applies to any Datadog widget whose query/formula does not need rate safety
(§3.2). No cadence sensitivity to live-verify here — this constant governs
chart smoothness for order-independent reducers (avg/sum/min/max/percentile),
not correctness, matching how Grafana adopted it without a live table either.

### 3.2 Give rate/diff widgets a coarser, rate-safe default: N=20

```python
# translate.py
_ADAPTIVE_RATE_BUCKETS = 20   # live-verified above; matches Grafana's panels.py constant
```

New helper, mirroring Grafana's `_panel_uses_range_function` but scoped to a
Datadog widget (which has queries *and* formulas, not a single PromQL expr):

```python
def _widget_uses_rate_function(widget: NormalizedWidget) -> bool:
    """Whether this widget's queries or formulas need rate-safe bucketing.

    True when any query applies as_rate()/per_second()/per_minute()/
    per_hour()/derivative() (_needs_rate), or any formula calls rate()/
    diff()/monotonic_diff() (_DERIVATIVE_FORMULA_FNS) — whether or not that
    resolves to the TS|QL path (a gauge-typed rate() still falls through to
    the FROM+FIRST/LAST fallback, which needs the same bucket floor: LAST==
    FIRST inside a too-fine bucket silently reads as a wrong rate, not a
    null one, which is worse than Grafana's visible failure mode).
    """
    for wq in widget.queries:
        mq = wq.metric_query
        if mq and (mq.as_rate or _needs_rate(mq)):
            return True
    for wf in widget.formulas:
        if _formula_calls_derivative_fn(wf.expression):
            return True
    return False
```

`TIME_BUCKET_EXPR` becomes a call, `_time_bucket_expr(rate_safe: bool)`,
returning the 75- or 20-bucket form based on this check, threaded through
every `FROM`-path call site that currently references the module-level
`TIME_BUCKET_EXPR` constant (including the `_rate_approx_expr` FIRST/LAST
fallback, which always qualifies as rate-needing by construction).

**As implemented:** rather than a single shared `_widget_uses_rate_function(widget)`
threaded everywhere, `rate_safe` is computed locally at each of the two
FROM-path entry points, since they have different natural signals available:
`_translate_single_metric` computes it once from its single query
(`spec.emits_rate` plus `wq.metric_query.as_rate or _needs_rate(wq.metric_query)`),
while `_translate_formula_metric_widget` computes it from three signals:
per-spec emitted/source rate semantics (`spec.emits_rate`, `as_rate`,
`_needs_rate`), formula-level `_formula_needs_bucket_span(...)` calls
(`per_second`/`per_minute`/`per_hour`/`rate`, including nested args), and
direct derivative refs from `_collect_derivative_query_refs(...)`. Both feed
the same `_time_bucket_expr(rate_safe)` helper and constants described above;
the count-only formula helpers (`_try_translate_formula_reducer`,
`_try_translate_count_formula_pipeline`) pass `rate_safe=False` explicitly,
since count formulas are never rate-related.

### 3.3 Convert the `TS` path to adaptive, windowless bucketing

```python
# was:
window = "5 minute"
by_clause = f"time_bucket = TBUCKET({window})"
...STATS {alias} = {es_agg}({spec.es_metric}, {window}) BY {by_clause}...

# becomes:
by_clause = f"time_bucket = TBUCKET({_ADAPTIVE_RATE_BUCKETS}, ?_tstart, ?_tend)"
...STATS {alias} = {es_agg}({spec.es_metric}) BY {by_clause}...
```

`RATE`/`INCREASE` drop their window argument entirely, matching Grafana's
`counter_range_window` postprocessor precedent (`promql.py` `_range_call`) —
confirmed generic ES|QL behavior, not PromQL-specific: both functions
extrapolate to the boundaries of whichever time-bucket grouping is present
regardless of which source adapter emitted the query.

### 3.4 Explicitly out of scope

- **The 5-minute-range / ≥60s-cadence gap from §2.** Same treatment as
  Grafana's residual gap: documented, not solved. Live scrape/flush-interval
  discovery would be required to close it fully and is tracked as a shared
  follow-up for both sources, not duplicated per-source here.
- **Per-dashboard/per-widget configurability.** No new knob; 75/20 become
  the new Datadog generic defaults, matching Grafana's decision in the
  parent doc.

## 4. Rollout plan

1. **Engine change** — add `_ADAPTIVE_CHART_BUCKETS`/`_ADAPTIVE_RATE_BUCKETS`
   and the `_time_bucket_expr(rate_safe: bool)` helper to `translate.py`;
   thread a locally-computed `rate_safe` through every `FROM`-path
   `TIME_BUCKET_EXPR` call site (see "As implemented" in §3.2); convert the
   `TS`-path `by_clause`/`RATE`/`INCREASE` emission to the adaptive
   windowless form.
2. **Tests** — regenerate the ~30 `tests/snapshots/datadog_to_esql/*.txt`
   fixtures that hardcode `BUCKET(@timestamp, 50, ...)`; update
   `test_datadog_migrate.py`'s `test_rate_formula_uses_ts_rate_when_metric_is_counter_typed`
   and `test_diff_formula_uses_ts_increase_when_metric_is_counter_typed` for
   the windowless form; add new unit coverage pinning the 75/20 FROM-path
   split (rate-formula widget → 20, plain avg/sum widget → 75), mirroring
   `tests/test_grafana_issues_316_319.py`.
3. **Docs** — update `docs/sources/datadog.md` lines 561-562 (TS|QL path
   description currently states the fixed `RATE(metric, 5 minute)` /
   `TBUCKET(5 minute)` form).
4. **Regression gates** — full test suite, `make lint`, `make typecheck`;
   watch for any Datadog corpus panel that regresses from non-null to null
   (would indicate a cadence this design's §2 data didn't cover).
5. **Cross-link back** — update `esql-time-bucketing-strategy.md` §6 item 3
   and its Definition of Done to point at this document as the resolution.

## Definition of done

- [x] `translate.py` emits one of exactly two adaptive bucket counts — 75
  (default) or 20 (rate/diff widgets) — for the `FROM` path, with no
  remaining `BUCKET(@timestamp, 50, ...)` in generated output.
- [x] `TS`-path emission uses `TBUCKET(20, ?_tstart, ?_tend)` with windowless
  `RATE`/`INCREASE`; no remaining fixed `5 minute` bucket/window.
- [x] New unit tests pin the widget-level rate-detection logic; `make test`,
  `make lint`, and `make typecheck` are green for the final tree.
- [x] `docs/sources/datadog.md` and `esql-time-bucketing-strategy.md` updated
  to reflect the as-built behavior.
