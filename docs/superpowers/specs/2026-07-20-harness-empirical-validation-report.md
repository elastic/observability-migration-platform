# Harness empirical validation report

**Date:** 2026-07-20  
**Branch:** feat/301-translation-harness-datadog (#302)  
**Campaign:** V0 offline (complete) → V1 scoped live (in progress)

## Key finding (V0)

The two Node Exporter Full panels that failed live on 2026-07-17 are now
**caught offline** by the Grafana structural oracle on current #302 code:

| Panel | Live error (Jul 17) | Oracle rule |
|---|---|---|
| CPU spent seconds in guests (VMs) | ClassCast / ReferenceAttribute | `STATS_CASE_BARE_TS_MIX` |
| CPU Frequency Scaling | Unknown column `[node_cpu_scaling_frequency_hertz]` | `EVAL_UNDEFINED_COLUMN` |

**Harness gap count for those known fails: 0.** The Grafana PromQL harness is
earning its keep. Datadog V0 re-translation of `infra/datadog` + prior smoke
queries produced no structural ERRORs (post-seed Datadog smoke was already
0 runtime errors on Jul 17).

## V0 classification counts

**Total panel observations:** 450 (prior smoke + fresh fixture translations)

| Classification | Count |
|---|---:|
| `caught_by_oracle` | 2 |
| `clean` | 441 |
| `data_gap` | 7 |
| `harness_gap` | 0 |

| Source | Count |
|---|---:|
| grafana | 359 |
| datadog | 91 |

Artifacts (local, not committed): `harness_empirical_20260720/v0_*.json`,
`scripts/run_harness_empirical_v0.py`.

## Implications for PR2 / PR3

- **PR2 (alerts):** still required — this campaign is dashboard ES|QL only.
- **PR3 (broader Grafana):** LogQL / variables / native PromQL not in V0; let V1
  live results decide urgency.
- **Translator fixes:** the two oracle hits are still live bugs until emitters
  are repaired — oracle prevents silent regression but does not auto-fix.

## V1 (next)

Scoped live smoke: Node Exporter Full + Diverse Panel Types Test; 2–3 Datadog
account dashboards via API; seed if needed; compare live fails vs oracle.
