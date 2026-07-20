# Harness empirical validation report

**Date:** 2026-07-20  
**Branch:** feat/301-translation-harness-datadog (#302)  
**Campaign:** V0 offline + V1 scoped live (complete)

## Executive summary

| Question | Answer |
|---|---|
| Does the Grafana structural harness catch the Jul-17 Node Exporter bugs? | **Partially.** On frozen Jul-17 smoke queries, both fails are `caught_by_oracle`. On **current** code after re-migrate + seed, only **1** live fail remains, and it is a **harness gap** (oracle-clean ClassCast on `TBUCKET`). |
| Is Datadog PR1 oracle earning its keep on this corpus? | **No structural hits** on infra fixtures or APM smoke queries; post-seed Datadog smoke had **0 runtime errors** (4 empties = data). Oracle still valuable as a ratchet for future regressions. |
| How good is testing now? | Strong for the **CASE + bare IRATE / EVAL alias** class. Weak for **ClassCast / Bucket grouping** and anything alert/LogQL/variable-shaped (PR2/PR3). |

## V0 — Offline (prior smoke + fresh fixture translate)

**450** panel observations.

| Classification | Count |
|---|---:|
| `caught_by_oracle` | 2 |
| `clean` | 441 |
| `data_gap` | 7 |
| `harness_gap` | 0 |

Oracle hits on Jul-17 after-seed smoke queries:

| Panel | Rule |
|---|---|
| CPU spent seconds in guests (VMs) | `STATS_CASE_BARE_TS_MIX` |
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

**Remaining live fail (harness gap):**

| Panel | Live error | Oracle |
|---|---|---|
| CPU spent seconds in guests (VMs) | `ReferenceAttribute cannot be cast to … grouping.Bucket` | **clean** (`[]`) |

Current emitted query uses CASE-wrapped IRATE on all measures and
`BY time_bucket = TBUCKET(5 minute), host.name`. Structural STATS/EVAL rules
do not model the Bucket cast failure — **new oracle rule or translator fix
needed** (not covered by PR1 Datadog work).

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

- **Pass:** Grafana Tier-3 structural gate correctly flags the historical
  CASE/bare and EVAL-alias failure classes on frozen queries.
- **Gap:** ClassCast involving `TBUCKET` / grouping Bucket is invisible to the
  current oracle — highest-value follow-up rule for Grafana PromQL harness.
- **Datadog:** PR1 matrix/fixture gate is green; this live corpus did not
  stress Datadog path skew. Keep seed intake ready for the first Datadog
  ClassCast/Unknown-column `real_bug`.

### PR2 — Alert offline gate (still needed)

Not exercised by V0/V1. Proceed with thin alert oracle as designed; do not
block on dashboard ClassCast.

### PR3 — Broader Grafana surface

Prioritize after/alongside a **TBUCKET/Bucket ClassCast** investigation:
either a translator fix or a new structural/WARNING rule. LogQL / variables /
native PromQL smoke remain deferred unless V1-like runs show those gaps.

### Translator fix candidate (out of harness-only scope)

Repair Node Exporter join-ratio / multi-measure `TBUCKET` emission that still
ClassCasts live despite CASE wrapping — confirm via the query in
`harness_empirical_20260720/grafana_out/dashboards/uploaded_dashboard_smoke_report_after_seed.json`.

## Non-goals honored

- No secrets or full `live_panel_check_*` / `harness_empirical_*` trees committed
- No claim of semantic parity from structure alone
- PR2/PR3 code not landed in this campaign
