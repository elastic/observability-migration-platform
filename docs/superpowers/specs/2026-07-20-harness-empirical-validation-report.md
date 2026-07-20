# Harness empirical validation report

**Date:** 2026-07-20 (updated 2026-07-21)  
**Branch:** feat/301-translation-harness-datadog (#302)  
**Campaign:** V0 offline + V1 scoped live + ClassCast root-cause fix

## Executive summary

| Question | Answer |
|---|---|
| Does the Grafana structural harness catch the Jul-17 Node Exporter bugs? | **Yes on frozen Jul-17 queries** (`caught_by_oracle`). On current code, the remaining guest-CPU ClassCast was a **translator bug** (illegal `IRATE(CASE(...))`), not a TBUCKET grouping bug. |
| Is Datadog PR1 oracle earning its keep on this corpus? | **No structural hits** on infra fixtures or APM smoke queries; post-seed Datadog smoke had **0 runtime errors** (4 empties = data). Oracle still valuable as a ratchet for future regressions. |
| How good is testing now? | Strong for **CASE placement / EVAL alias** after the outer-CASE fix + `STATS_TS_CASE_VALUE_ARG`. Alert/LogQL/variable surfaces remain PR2/PR3. |

## V0 — Offline (prior smoke + fresh fixture translate)

**450** panel observations.

| Classification | Count |
|---|---:|
| `caught_by_oracle` | 2 |
| `clean` | 441 |
| `data_gap` | 7 |
| `harness_gap` | 0 |

Oracle hits on Jul-17 after-seed smoke queries:

| Panel | Rule (historical) |
|---|---|
| CPU spent seconds in guests (VMs) | `STATS_CASE_BARE_TS_MIX` (then); now modeled as `STATS_TS_CASE_VALUE_ARG` on `IRATE(CASE(...))` |
| CPU Frequency Scaling | `EVAL_UNDEFINED_COLUMN` |

Script: `scripts/run_harness_empirical_v0.py`  
Local artifacts: `harness_empirical_20260720/v0_*.json` (not committed).

## V1 — Scoped live (serverless staging)

Creds: local `serverless_creds.env` / `datadog_creds.env` (not committed).

### Grafana — Node Exporter Full + Diverse Panel Types Test

| Stage | runtime_error_panels | empty | notes |
|---|---:|---:|---|
| Migrate + smoke (pre-seed) | 121 | 3 | Matches Jul-17 pre-seed pattern |
| After `seed-sample-data` (now-6h) | **1** | 0 | Diverse: 0 fails; Node Exporter: 1 fail |

**Remaining live fail at V1 (misattributed then as harness gap):**

| Panel | Live error | Oracle then |
|---|---|---|
| CPU spent seconds in guests (VMs) | `ReferenceAttribute cannot be cast to … grouping.Bucket` | clean under old rules |

### Root cause (confirmed 2026-07-21 live)

Not `TBUCKET` / grouping by itself. Elasticsearch ClassCasts when a
time-series range function takes **CASE as its value argument**:

| Shape | Live result |
|---|---|
| `IRATE(CASE(cond, field, NULL), window)` | **ClassCast** (`ReferenceAttribute` → `Bucket`) |
| `CASE(cond, IRATE(field, window), NULL)` | **OK** |
| Bare `IRATE(field, window)` next to outer CASE | **OK** |

The prior “fix” that wrapped bare siblings as `IRATE(CASE(true, field, NULL), …)`
**caused** the ClassCast class.

### Translator + oracle fix (same PR)

- `promql._inline_filters_into_stats_expr` / `_rewrite_ts_inner_case_to_outer_case`
  emit outer CASE around `IRATE`/`RATE`/…, never CASE inside the TS value arg.
- New ERROR rule `STATS_TS_CASE_VALUE_ARG`; old `STATS_CASE_BARE_TS_MIX` kept for
  enum compat but is no longer the primary detector (outer CASE + bare is legal).
- Live re-check on staging: guest-CPU join-ratio query returns rows; old inner-CASE
  shape still ClassCasts.

**CPU Frequency Scaling:** pass after re-migrate + seed (Jul-17 Unknown-column
fail no longer reproduces on this target).

### Datadog — APM Monitoring (+ 2 empty account dashboards)

| Stage | runtime_error_panels | empty |
|---|---:|---:|
| Migrate + smoke (pre-seed) | 29 | 0 |
| After seed | **0** | 4 |

No Datadog structural oracle hits on failing queries (none remaining after seed).
Empty panels are data-readiness, not translator structure.

## Implications

### Harness quality

- **Pass:** Grafana Tier-3 structural gate flags CASE-value-arg and EVAL-alias
  failure classes; live guest-CPU panel now executes after the outer-CASE fix.
- **Datadog:** PR1 matrix/fixture gate is green; this live corpus did not
  stress Datadog path skew. Keep seed intake ready for the first Datadog
  ClassCast/Unknown-column `real_bug`.

### PR2 — Alert offline gate (still needed)

Not exercised by V0/V1. Proceed with thin alert oracle as designed.

### PR3 — Broader Grafana surface

LogQL / variables / native PromQL smoke remain deferred unless later runs show
those gaps.

## Non-goals honored

- No secrets or full `live_panel_check_*` / `harness_empirical_*` trees committed
- No claim of semantic parity from structure alone
