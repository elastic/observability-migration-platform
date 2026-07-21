# Harness empirical validation campaign — design

**Date:** 2026-07-20  
**Status:** Approved — executing on `feat/301-translation-harness-datadog` (#302)  
**Issue:** https://github.com/elastic/observability-migration-platform/issues/301  

## Goal

Before landing PR2 (alerts) and PR3 (broader Grafana), measure how well the
existing translation correctness harness (Grafana v1 + Datadog PR1) catches
real failures on real dashboards, and what gaps remain.

## Sequence (locked)

1. **V0 — Offline oracle campaign** on Jul-17 corpus + infra fixtures  
2. **V1 — Scoped live smoke** via `serverless_creds.env` / `datadog_creds.env`  
3. **Report** on #302; shape **PR2/PR3** from findings (separate PRs)

## Corpus

| Source | Offline inputs |
|---|---|
| Grafana | `live_panel_check_20260717_151502/grafana_selected/*.json`, prior smoke JSON |
| Datadog | `infra/datadog/dashboards/**/*.json`, prior smoke JSON, optional live API pull |
| Known fails | Node Exporter ClassCast / Unknown-column panels from RESULTS.md |

## Classification

Each panel/query outcome:

| Label | Meaning |
|---|---|
| `caught_by_oracle` | Structural ERROR offline |
| `harness_gap` | Prior/live `real_bug` shape but oracle clean |
| `data_gap` | Empty / field missing after seed |
| `would_need_live` | Needs ES execution to judge |
| `clean` | Feasible + oracle-clean |

## Non-goals

- Full PR2/PR3 implementation inside this campaign  
- Committing secrets or entire `live_panel_check_*` trees  
- Semantic numeric parity claims from structure alone  

## Deliverable

`docs/superpowers/specs/2026-07-20-harness-empirical-validation-report.md`
plus any committed regression seeds justified by `real_bug` findings.
