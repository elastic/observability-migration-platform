# Translation Correctness Harness Extension — design

**Date:** 2026-07-20  
**Status:** Approved for implementation (phased)  
**Issue:** https://github.com/elastic/observability-migration-platform/issues/301  
**Depends on:** Grafana PromQL harness v1 —
`docs/superpowers/specs/2026-07-17-grafana-translation-correctness-harness-design.md`
(implemented)

Extends the confidence pyramid in
`docs/superpowers/specs/2026-06-25-migration-confidence-pyramid-design.md` and
`docs/testing.md` so Grafana-only coverage is not mistaken for “all
translations.”

## Problem

Harness v1 closed Grafana PromQL emitter-path skew (CASE + bare `IRATE`, bare
vs wrapped `*_OVER_TIME`, EVAL after STATS alias rename). Datadog widgets,
alert rules, and non-PromQL Grafana surfaces use different planners/emitters
and were explicitly deferred. Without a follow-on, CI can look green while
Datadog/alerts regress on sibling paths the same way Grafana did.

## Goal

Ship a **phased multi-source Translation Correctness Harness**:

1. **PR1 — Datadog dashboard ES|QL** structural oracle + emitter matrix +
   fixture gate + seed intake (this branch’s primary deliverable).
2. **PR2 — Alert rule offline gate** for Grafana alerting rules and Datadog
   monitors → Kibana rules, with disposition (`real_bug` vs data/config gap).
3. **PR3 — Broader Grafana surface** (LogQL / claimed datasources, variables /
   controls / links, native PromQL passthrough smoke).
4. **Later — shared `translation_oracle` package** only after Grafana and
   Datadog both prove the pattern in CI.

Success is a ratchet, not perfect semantic parity. Tier 4
(live_validate / compare / render) remains authority for numeric and visual
truth.

## Non-goals

- Claiming perfect semantic parity from offline structure alone.
- Blocking or rewriting the Grafana PromQL v1 harness.
- Extracting a shared oracle package in PR1.
- Merging Layer-9 visual invariants into the structural oracle.
- Full community-corpus structural gate on every Datadog PR (PR1 uses
  `infra/datadog` fixtures; pinned community corpus stays optional/nightly).

## Approaches considered

| Approach | Pros | Cons |
|---|---|---|
| **A. Source-local Datadog harness** (mirror Grafana under `adapters/source/datadog/`; reuse shared STATS/EVAL checks via import; Datadog-only rules local) | Matches v1; defers shared package; PR-fast | Temporary Datadog→Grafana import |
| **B. Extract shared package in PR1** | Clean API early | Premature; slows the Datadog ratchet |
| **C. Docs/registry only** | Tiny diff | Misses the failure class #301 tracks |

**Decision:** **A** for PR1; shared package after both sources are proven.

### Datadog oracle depth (PR1)

| Option | Decision |
|---|---|
| Thin mirror + Datadog-specific `FROM` / empty-feasible rules; grow from seeds | **Chosen** |
| Broad day-one contracts (formula WHERE shapes, KEEP/profile, widget→shape) | Deferred — overlaps field-profile oracle |
| Oracle-light (matrix only, no new rules) | Rejected — misses structural class |

### Datadog emitter registry grain (PR1)

| Option | Decision |
|---|---|
| Widget `_build_*_esql` builders | Deferred until seeds prove skew |
| **Translator / planner ES\|QL emit routes** | **Chosen** |
| Hybrid (planner mandatory, builders optional) | Equivalent start; register translator rules only |

## Roadmap

```
PR1 Datadog ES|QL harness
        │
        ▼
PR2 Alert offline gate ──────────────► docs/testing.md multi-source layout
        │
        ▼
PR3 Broader Grafana (LogQL / vars / native PromQL smoke)
        │
        ▼
Later: extract shared translation_oracle (optional)
```

---

## PR1 — Datadog dashboard ES|QL harness

### Architecture

```
              translate_widget / METRIC_TRANSLATORS / LOG_TRANSLATORS
                                    │
                                    ▼
                    datadog.esql_structural_oracle
                    (shared STATS/EVAL via Grafana check
                     + Datadog MISSING_FROM / EMPTY_FEASIBLE)
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          ▼                         ▼                         ▼
   fixture corpus            emitter path matrix         seed intake
   (infra/datadog)           (4 ES|QL translator         (Datadog reports
                              routes)                     → translation_seeds)
```

### Component 1 — Structural oracle

**Module:** `observability_migration/adapters/source/datadog/esql_structural_oracle.py`

- Call Grafana `check_esql_structure` for shared ERROR rules
  (`STATS_CASE_BARE_TS_MIX`, `STATS_BARE_WRAPPED_OVER_TIME_MIX`,
  `EVAL_UNDEFINED_COLUMN`, and existing WARNING lane).
- Datadog-only ERROR rules (v1):
  - `MISSING_FROM` — feasible ES|QL query text must contain a top-level `FROM`
    stage (Datadog metric/log emitters always start from an index/data stream).
  - `EMPTY_FEASIBLE_QUERY` — translation status in `{ok, warning}` with blank
    `esql_query` when the backend is an ES|QL-emitting backend.
- **Skip** non-query backends: `markdown`, `blocked`, `group`, `image`, `lens`
  (Lens is not ES|QL text; do not apply STATS rules).
- Hook points: tests assert oracle on matrix cells, fixture gate, and seeds.
  Production remains degrade-graceful (no user-facing crash on oracle miss).

### Component 2 — Emitter path matrix

**Module:** `observability_migration/adapters/source/datadog/esql_emitters.py`  
**Test:** `tests/test_datadog_esql_emitter_matrix.py`

| Emitter id | Translator rule / helper |
|---|---|
| `metric_single_query` | `datadog.translate.metric_single_query` / `metric_single_query_rule` |
| `metric_formula` | `datadog.translate.metric_formula` / `metric_formula_rule` |
| `log_direct_esql` | `datadog.translate.log_direct_esql` / `log_direct_esql_rule` |
| `log_kql_bridge` | `datadog.translate.log_kql_bridge` / `log_kql_bridge_rule` |

Each cell:

1. Minimal normalized widget fixture that forces that translator rule.
2. Assert path token (trace entry / rule id / provenance).
3. Run structural oracle; hard-fail on ERROR findings.

CI fails if registry names ≠ matrix cells, or if registered helper symbols are
missing from Datadog source (same spirit as Grafana `EMITTER_HELPER_SYMBOLS`).

Widget-level builders (`_build_timeseries_esql`, change widget, etc.) are **not**
mandatory registry entries in PR1; add them when seed intake shows path skew.

### Component 3 — Fixture corpus gate

**Test:** `tests/test_datadog_fixture_structural_gate.py`

- Translate leaf widgets under `infra/datadog/dashboards/**/*.json`.
- For ES|QL-emitting backends with status `ok` / `warning`, require
  oracle-clean queries.
- Placeholders / `not_feasible` / `requires_manual` / non-ES|QL backends do not
  fail the structural gate (disposition stays separate).
- Pinned Datadog community corpus structural sweep is **out of PR CI** for v1;
  may run nightly later.

### Component 4 — Seed intake (non-Grafana)

- Extend `scripts/intake_translation_seeds.py` to accept `source: "datadog"`
  reports (panel/widget title, disposition, `esql_query`, source queries).
- Write seeds under `tests/fixtures/translation_seeds/` with a `source` field.
- Reuse `unknown_column_looks_like_alias_bug` when `esql_query` is present.
- Add a Datadog mutation self-test (synthetic alias-shaped Unknown column →
  seed proposed) alongside the existing Grafana intake test.
- Human triage still required before committing seeds (no silent corpus rot).

### Component 5 — Docs

- Update `docs/testing.md` with a multi-source harness section (Grafana table
  remains; add Datadog parallel; point at PR2/PR3 as follow-ups).
- Keep this design doc as the roadmap for #301.

---

## PR2 — Alert rule offline gate

**Status:** Implemented (thin v1) on the #301 branch.

Separate oracle from dashboard ES|QL structure:

- **Module:** `observability_migration/core/verification/alert_offline_gate.py`
- **Inputs:** Grafana alerting rules + Datadog monitors → `map_alerts_batch`
  Kibana rule payloads.
- **Checks:** enablement safety (`enabled=False`); non-empty
  `params.esqlQuery.esql` when `payload_status=emitted`; required payload
  fields; empty `actions` placeholder (non-empty → `config_gap`); nested
  dashboard ES|QL structural oracle on non-`PROMQL(...)` queries.
- **Disposition:** `real_bug` hard-fails CI; `expected_manual` /
  `draft_review` / `config_gap` / `ok` do not. Must not treat
  `manual_required` or `parse_degraded` emissions as success.
- **Corpus:** `tests/test_alert_fixture_offline_gate.py` over
  `examples/alerting/`.
- **Docs:** `docs/testing.md` alert offline gate section.

## PR3 — Broader Grafana surface (follow-on)

- LogQL / other claimed datasource query types: structural or smoke gate where
  translation is claimed.
- Dashboard variables / controls / links: extend interaction + render-audit
  coverage; no new PromQL STATS fusion rules.
- Native PromQL passthrough: smoke only (ES|QL structural oracle already skips
  `PROMQL(...)`).

## Shared package (later)

Only after PR1 Datadog harness is green in CI alongside Grafana v1: extract a
shared `translation_oracle` API so sources plug adapters rather than
Datadog importing Grafana’s checker. Not part of PR1.

## Integration with existing pyramid

| Tier | Existing | Extension adds |
|---|---|---|
| 1 | Unit / seeds | Datadog translation seeds |
| 2 | Panel matrices | Datadog ES\|QL emitter matrix |
| 3 | Grafana structural oracle / Layer-9 | Datadog structural oracle + fixture gate; later alert offline gate |
| 4 | live_validate / render / compare | Datadog seed intake feed |

Field-profile consistency oracle
(`docs/superpowers/specs/2026-07-16-datadog-field-profile-oracle-design.md`)
stays separate: profile/KEEP/readiness invariants are not duplicated in the
structural harness.

## Success criteria

### PR1 (this branch)

- [ ] Datadog PR CI runs structural oracle + emitter matrix (4 routes).
- [ ] Re-introducing empty feasible ES|QL or missing `FROM` on a matrix path
      fails offline.
- [ ] `infra/datadog` fixture gate is oracle-clean for feasible ES|QL widgets.
- [ ] Seed intake proposes ≥1 Datadog seed from a synthetic alias-shaped
      failure (mutation self-test).
- [ ] `docs/testing.md` documents Grafana + Datadog harness layout.

### Full issue #301 (across PRs)

- [ ] Alert translations have an offline gate with disposition.
- [ ] Broader Grafana surface gates exist where we claim translation.
- [ ] Seed intake works for at least one non-Grafana source (satisfied by PR1).

## Implementation sketch (PR1 ordered)

1. Datadog `esql_structural_oracle` + unit tests (shared + `MISSING_FROM` /
   empty feasible).
2. `esql_emitters` registry + emitter matrix tests.
3. Fixture corpus structural gate over `infra/datadog`.
4. Extend seed intake + Datadog mutation self-test.
5. Update `docs/testing.md` (+ this design already checked in).

## Risks

| Risk | Mitigation |
|---|---|
| False positives on legal Datadog ES\|QL | Start with proven empty/`FROM` + shared Grafana rules; WARNING lane for speculative rules |
| Registry too coarse (miss widget builder skew) | Seed intake + optional later registry growth |
| Fixture gate noise from `requires_manual` | Only assert oracle on ES\|QL-emitting ok/warning |
| Cross-import Grafana↔Datadog | Temporary; extract shared package after both green |

## Open questions (resolved)

1. **Scope for this branch?** Full #301 surface as multi-PR sequence starting
   with Datadog. **Decided:** C (multi-PR), PR1 = Datadog.
2. **Oracle depth?** Thin mirror + Datadog `FROM`/empty. **Decided:** A.
3. **Emitter grain?** Translator ES\|QL routes, not every `_build_*`.
   **Decided:** B.
4. **Shared package in PR1?** No.
5. **Oracle in production translate?** Tests/CI hard-fail only (match Grafana).
