# Grafana PromQL Translation Correctness Harness — design

**Date:** 2026-07-17  
**Status:** Implemented  
**Scope (v1):** Grafana PromQL → ES|QL (every emitter path)  
**Deferred:** Datadog + alerts/variables/full migratable surface — tracked in a
follow-up GitHub issue (see *Deferred scope*).

Extends the confidence pyramid in
`docs/superpowers/specs/2026-06-25-migration-confidence-pyramid-design.md` and
`docs/testing.md`. This design does **not** replace Tier 4 live authority; it
closes the offline hole where sibling emitters can emit ES-illegal or
self-inconsistent queries that unit tests never assert.

## Problem

Recent Node Exporter / join-ratio failures shared one pattern:

1. A **shared ES invariant** was violated (CASE + bare `IRATE`, bare + wrapped
   `*_OVER_TIME`, EVAL RHS after STATS alias rename).
2. The bug lived on **one emitter path** while a sibling path was already fixed
   (formula fusion vs pretranslated merge vs `join_family_rule`).
3. Existing gates either asserted **intent** (“feasible”, “has CASE numerator”)
   or **visual Layer-9** wiring — not **ES|QL structural legality** of fused
   `STATS` / `EVAL` pipelines.
4. Live smoke caught the bug, but only when someone ran it; disposition often
   misfiles “Unknown column” as a data gap.

We already have many tests and fuzzers. Volume ≠ coverage of the failure class.

## Goal

A **single Translation Correctness Harness** for Grafana PromQL that:

1. Runs a **shared structural oracle** on every emitted ES|QL query, regardless
   of which code path produced it.
2. Proves **path completeness** — every registered emitter is exercised and
   checked by that oracle.
3. **Self-evolves** — live / smoke / render failures that look like translator
   bugs become committed regression seeds automatically (with human triage).
4. Stays **PR-fast offline** for the structural layer; live ES remains Tier 4.

Success is a ratchet, not “100% perfect translations.” Valid-but-wrong
semantics and pure data gaps remain Tier 4 / compare concerns.

## Non-goals (v1)

- Datadog metric/log/formula translation oracle (deferred issue).
- Alert rule translation harness (deferred issue).
- Replacing numeric parity (`compare` / `corpus_gate`) or render audit.
- Claiming semantic perfection from offline structure alone.
- Requiring live Elasticsearch on every PR.

## Approaches considered

| Approach | Pros | Cons |
|---|---|---|
| **A. Bolt more unit regressions only** | Cheap | Keeps path-skew; we just lived this |
| **B. Shared finalize + structural oracle + seed intake (recommended)** | Closes the class; reuses pyramid; PR-fast | Needs discipline on emitter registry |
| **C. Full live ES validate on every fixture every PR** | Strongest oracle | Slow, flaky, credential-gated; fails on data gaps |

**Recommendation:** **B**, with a thin nightly live slice (existing Tier 4) as
the intake feed for new seeds.

## Architecture

```
                    ┌─────────────────────────────┐
                    │  translate_promql_to_esql / │
                    │  translate_panel / merge    │
                    └─────────────┬───────────────┘
                                  │ every emitter
                                  ▼
                    ┌─────────────────────────────┐
                    │  esql_structural_oracle     │  ← NEW (Tier 3, every PR)
                    │  (STATS/EVAL/shape rules)   │
                    └─────────────┬───────────────┘
                                  │
          ┌───────────────────────┼───────────────────────┐
          ▼                       ▼                       ▼
   fixture corpus          emitter path matrix      Hypothesis (optional)
   (infra/grafana +        (one cell per            shape-preserving
    bug_seed panels)        registered path)         PromQL → oracle
                                  │
                                  ▼
                    ┌─────────────────────────────┐
                    │  seed intake (nightly)      │  ← NEW (Tier 4 → Tier 1)
                    │  live_validate / smoke /    │
                    │  render_error → regression  │
                    │  fixture + oracle assert    │
                    └─────────────────────────────┘
```

### Component 1 — `esql_structural_oracle` (offline)

Pure function: `(esql_query: str, *, source_type: str | None) -> list[Finding]`.

**v1 ERROR rules (translator bugs if violated):**

| ID | Rule |
|---|---|
| `STATS_CASE_BARE_TS_MIX` | In one `STATS`, if any assignment contains `CASE(`, no bare `RATE\|IRATE\|INCREASE\|DELTA\|DERIV\|*_OVER_TIME(ident, window)` may remain |
| `STATS_BARE_WRAPPED_OVER_TIME_MIX` | Must not mix bare `X_OVER_TIME(...)` with `AGG(X_OVER_TIME(...))` in one `STATS` |
| `EVAL_UNDEFINED_COLUMN` | `EVAL legend = simple_ident` where `simple_ident` was not defined by prior `STATS`/`EVAL` LHS |
| `EMPTY_FEASIBLE_QUERY` | `feasibility == feasible` but query blank / missing `STATS` when metric panel expects one |

**v1 WARNING rules (triage, not hard-fail initially):**

| ID | Rule |
|---|---|
| `MIXED_IRATE_AVG_OVER_TIME` | Same `STATS` mixes counter-range funcs with bare gauge `*_OVER_TIME` (monitor; may be legal) |

Findings reuse the Layer-9 `Finding` shape where practical, or a sibling
dataclass in `observability_migration/adapters/source/grafana/` so package
tests can import without parity-rig path hacks.

Hook points (must all call the oracle before returning success):

- `translate_promql_to_esql` completion (feasible / migrated_with_warnings)
- `_build_shared_measure_pipeline` / `_merge_pretranslated_xy_queries` outputs
- `join_family_rule` emitted query
- `translate_panel` ES|QL body (multi-target included)

Production code may **auto-repair** via `_finalize_fused_stats_assignments`
(already landed); the oracle **fails the test** if repair was skipped. Prefer
repair in emitters + oracle as belt-and-suspenders.

### Component 2 — Emitter path matrix

Machine-enforced registry, same spirit as `supported_types.py`:

```python
GRAFANA_ESQL_EMITTERS = [
  "single_target_formula",
  "join_family_ratio",
  "shared_measure_pipeline",
  "pretranslated_xy_merge",
  "same_metric_collapse",
  # ...
]
```

Each entry has:

- a **minimal PromQL / panel fixture** that *must* take that path (assert via
  metadata / warning token / provenance), and
- an oracle run on the result.

CI fails if a registry name has no matrix cell, or if code gains a new public
fusion helper without a registry entry (grep/ast guard similar to panel-type
coverage).

### Component 3 — Fixture corpus gate (offline)

Translate all `infra/grafana/dashboards/*.json` leaf panels + pinned
`bug_seed` panels from `parity-rig/benchmark/community_corpus.json` (at least
Node Exporter Full id 1860 smoke titles).

Hard-fail on any structural ERROR. Already partially covered by
`tests/test_multi_target_merge_aliases.py` smoke slice — generalize.

### Component 4 — Self-evolving seed intake

Nightly / on-demand job (or documented operator script):

1. Run `live_validate` / seeded smoke / render audit on the stratified corpus.
2. Classify failures with existing disposition (`real_bug` vs `data_gap` /
   `field_gap`).
3. For `real_bug` (and render `render_error`):
   - extract source PromQL / panel JSON snippet
   - write under `tests/fixtures/translation_seeds/<id>.json`
   - generate or append an oracle-expecting test (template)
4. Open a draft PR or fail a nightly check listing **uncommitted new seeds**
   until a human merges them (prevents silent corpus rot and secret leakage).

Misclassification guard: seed intake must record the raw ES error string;
`Unknown column [X]` where `X` is a *renamed STATS alias missing from EVAL* is
`real_bug`, not `data_gap`. Extend disposition heuristics accordingly.

### Component 5 — Optional property layer

Extend `tests/test_promql_property.py` so every feasible Hypothesis example also
runs `esql_structural_oracle`. Keep bounds low for PR time; deeper fuzz nightly.

## Integration with existing pyramid

| Tier | Existing | Harness adds |
|---|---|---|
| 1 | Unit / semantic / snapshots | Seed fixtures from intake |
| 2 | Panel matrix / canary | Emitter path matrix |
| 3 | Layer-9 invariants / fidelity ratchet | **ES\|QL structural oracle** on all translations |
| 4 | live_validate / render / compare | Seed intake feed; disposition fix for alias bugs |

Layer-9 stays focused on **visual accessor** consistency. Structural oracle
stays focused on **query text legality / self-consistency**. Do not merge them
into one mega-linter in v1.

## Success criteria

- [ ] Any feasible Grafana PromQL translation in CI is oracle-clean.
- [ ] Emitter registry has ≥1 matrix cell per path; adding a path without a cell
      fails CI.
- [ ] Re-introducing bare `IRATE` beside `IRATE(CASE(` on join-ratio or merge
      fails offline without live ES.
- [ ] Nightly intake can propose ≥1 seed from a synthetic ClassCast / Unknown
      column alias failure (mutation self-test, like render-audit self-test).
- [ ] `docs/testing.md` documents the harness and how to add a seed.

## Implementation sketch (ordered)

1. Land `esql_structural_oracle` + unit tests for the three ERROR rules.
2. Wire oracle into merge / join / shared pipeline / `translate_promql_to_esql`
   test hooks (prefer assert-in-tests first; optional debug assert in code
   behind env flag).
3. Emitter path matrix + registry guard.
4. Fixture corpus structural gate.
5. Disposition heuristic fix for alias-shaped Unknown column.
6. Seed intake script + mutation self-test.
7. Docs update in `docs/testing.md`.

## Deferred scope

Tracked in https://github.com/elastic/observability-migration-platform/issues/301:

- Datadog metric / log / formula structural + emitter matrix
- Alert rule translation harness
- Variables / controls / non-PromQL Grafana query types
- Cross-source shared oracle package (only after both sources prove the pattern)

## Risks

| Risk | Mitigation |
|---|---|
| Oracle false positives on legal ES quirks | Start with rules proven by live ClassCast / Unknown column; WARNING lane for speculative rules |
| Emitter registry drift | AST/grep guard in CI |
| Seed intake noise from data gaps | Require disposition `real_bug` + raw error allowlist |
| PR runtime growth | Corpus gate on fixtures only in PR; full community pins nightly |

## Open questions

1. Should the oracle run **inside** production `translate_*` (fail closed) or
   **only in tests/CI** (fail open in prod, degrade gracefully)?  
   **Decided:** tests/CI hard-fail; production keeps degrade-graceful + shared
   finalize repair (no user-facing crash on oracle miss).
2. Native PromQL passthrough panels — skip structural ES\|QL rules when query
   is `PROMQL ...`?
   **Decided:** yes, skip; separate future native-PromQL smoke.
