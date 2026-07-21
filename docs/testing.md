# Testing & Quality Infrastructure

The canonical reference for how migration correctness is verified in this repo.
For contributor setup and the minimal pre-PR checklist see `../CONTRIBUTING.md`;
for the runnable command inventory see `command-contract.md`. This document
explains the **why and how** of every gate.

## The confidence pyramid

No single check proves "the dashboard migrated correctly", so gates are layered —
each answers a different question, and **real Kibana is the ultimate authority**
(the offline schema/lint is only a fast pre-filter for a YAML→JSON compiler that
has its own bugs).

```
 Tier 4  LIVE        live_validate · dashboards_api · render audit · interaction
 (authority)         audit · compare/corpus_gate · benchmark_gate
                     (nightly + on-demand)
 ─────────────────────────────────────────────────────────────────────────────
 Tier 3  OFFLINE     fidelity ratchet · Kibana-schema gate · invariant linter
 (every PR)          · mutation self-test
 ─────────────────────────────────────────────────────────────────────────────
 Tier 2  OFFLINE     supported-type registry · panel matrices · kitchen-sink
 (every PR)          canary
 ─────────────────────────────────────────────────────────────────────────────
 Tier 1  OFFLINE     unit tests · core-IR tests · semantic suites · snapshots
 (every PR)
```

Tiers 1–3 are fully offline and run on every PR (fast, deterministic). Tier 4
needs a real Elasticsearch + Kibana and runs nightly / on demand.

## Quick start

```bash
make test       # Tier 1–2 unit suite (excludes e2e); ~30s
make lint       # ruff + source-license-header check
make typecheck  # targeted mypy
.venv/bin/python -m pytest tests/e2e/ -q   # Tier 3 e2e (ratchet, schema gate, ...)
```

CI mapping (`.github/workflows/`):

| Workflow | Runs | Gates |
|---|---|---|
| `tests.yml` | every PR | ruff, mypy, pytest (3.11–3.13, `--cov-fail-under=75`), e2e, packaging smoke |
| `nightly-live-gates.yml` | nightly + manual, secret-gated | `live_validate` + `dashboards_api` against the real cluster |
| `render-audit-local.yml` | nightly + manual | render audit against a local no-SSO Kibana |
| `dashboard-interaction-audit.yml` | nightly + manual (not PR) | control interactivity vs local no-SSO Kibana 9.5+ |

---

## Tier 1 — Logic

| What | Where | Proves |
|---|---|---|
| Unit tests | `tests/test_*.py` | translation/IR logic is correct |
| Core-IR units | `tests/test_grafana_ir_units.py` | `_parse_fragment`, `DashboardLineage`, IR dataclass contracts (directly, not via snapshot churn) |
| Grafana semantic suite | `tests/test_grafana_semantic_accuracy.py` | aggregation / metric / group-by / time-bucket preserved (asserts the structured `esql` block, robust to native-PROMQL vs ES\|QL) |
| Datadog semantic suite | `tests/e2e/test_datadog_semantic_accuracy.py` | same properties for Datadog |
| Snapshots | `tests/snapshots/`, `tests/test_*_snapshots.py` | emitted ES\|QL / YAML is byte-stable |

**Updating snapshots** (only when the output change is intentional):

```bash
UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest tests/test_promql_esql_snapshots.py
```

---

## Tier 2 — Type coverage

"100% translation coverage" is machine-enforced: a supported panel/widget type
cannot ship — or lose coverage — untested.

| What | Where |
|---|---|
| Supported-type registry | `observability_migration/core/coverage/supported_types.py` |
| Registry cross-check (both ways) | `tests/core/coverage/test_supported_types.py` |
| Grafana panel matrix | `tests/test_panel_matrix.py` |
| Datadog panel matrix | `tests/test_datadog_panel_matrix.py` |
| Kitchen-sink canary | `observability_migration/core/coverage/canary.py`, `tests/test_canary.py` |

The cross-check compares the registry to the code's real routing
(`grafana.panels.PANEL_TYPE_MAP`; the `@register`'d widget types in
`datadog.planner`) in **both** directions. The matrices enumerate
`{type} × {query family/agg} × {by-arity}` and lint each cell through the *real*
pipeline with the Layer-9 invariants. The canary is one generated dashboard
covering every chart-bearing Kibana target; it must migrate clean and validate
against the schema, and is the fixture the live render audit uploads.

→ See [Adding a supported panel/widget type](#adding-a-supported-panelwidget-type).

---

## Tier 3 — Offline fidelity & structure

| Gate | Module | Proves | Run |
|---|---|---|---|
| Fidelity ratchet | `verifier.scorecard` | Layer-9 invariant ERROR counts don't regress vs a committed baseline | `tests/e2e/test_fidelity_ratchet.py` |
| Kibana-schema gate | `tests/e2e/test_kibana_schema_gate.py` | emitted YAML validates against the vendored `DashboardConfig` schema (`docs/dashboards/schema.json`) via `jsonschema` | e2e |
| Invariant linter | `verifier.invariants` | broken Lens accessors, merged series, dropped placeholders (Layer-9) | used by the matrices + scorecard |
| Mutation self-test | `verifier.mutations` | the invariant verifier catches deliberate corruptions | `tests/test_verifier_mutations.py` |

Baselines: `parity-rig/benchmark/fidelity_baseline_{grafana,datadog}.json`
(270 / 426 panels, 0 errors). The ratchet re-migrates the committed corpus with
the *current* code and fails if ERROR counts rise. → See
[Refreshing a fidelity baseline](#refreshing-a-fidelity-baseline).

> The vendored `schema.json` comes from the abandoned `kb-dashboard-core`, so
> passing the schema gate is **necessary but not sufficient** — real Kibana
> (Tier 4) is the authority.

### Grafana ES|QL structural harness (offline)

Closes the offline gap where sibling Grafana emitters can produce ES-illegal or
self-inconsistent fused `STATS` / `EVAL` pipelines that unit snapshots never
assert. Spec:
`docs/superpowers/specs/2026-07-17-grafana-translation-correctness-harness-design.md`.

| Piece | Module / test | What it proves |
|---|---|---|
| Structural oracle | `observability_migration/adapters/source/grafana/esql_structural_oracle.py` | ERROR rules on emitted ES\|QL: `STATS_CASE_BARE_TS_MIX`, `STATS_BARE_WRAPPED_OVER_TIME_MIX`, `EVAL_UNDEFINED_COLUMN`, `EMPTY_FEASIBLE_QUERY`; WARNING `MIXED_IRATE_AVG_OVER_TIME`. Skips native `PROMQL(...)` passthrough. |
| Oracle unit tests | `tests/test_esql_structural_oracle.py` | Each rule has a positive/negative fixture |
| Emitter path matrix | `observability_migration/adapters/source/grafana/esql_emitters.py`, `tests/test_grafana_esql_emitter_matrix.py` | Every registered fusion path has a minimal fixture + oracle run |
| Fixture corpus gate | `tests/test_grafana_fixture_structural_gate.py` | All `infra/grafana/dashboards/*.json` leaf panels translate to oracle-clean ES\|QL |
| Property hook (optional) | `tests/test_promql_property.py` | Feasible Hypothesis examples also run the oracle (not `PROMQL` passthrough) |
| Seed intake + mutation self-test | `scripts/intake_translation_seeds.py`, `tests/test_translation_seed_intake.py` | Live/smoke failures become committed regression seeds |

**Adding a regression seed**

1. Capture a live/smoke/render failure report JSON with `disposition: real_bug`
   (alias-shaped `Unknown column` failures are reclassified when the report
   includes `esql_query`).
2. Propose seeds offline:
   ```bash
   .venv/bin/python scripts/intake_translation_seeds.py \
     --report /path/to/report.json \
     --out-dir tests/fixtures/translation_seeds \
     --dry-run
   ```
3. Commit the generated JSON under `tests/fixtures/translation_seeds/` and wire
   an oracle-expecting test (see `tests/test_translation_seed_intake.py` for the
   mutation self-test pattern).

Datadog, alerts, variables, and the full migratable surface are deferred —
https://github.com/elastic/observability-migration-platform/issues/301.

---

## Tier 4 — Live authority

Needs `ELASTICSEARCH_ENDPOINT`, `KIBANA_ENDPOINT`, and an API key (one key works
for both on Serverless). Full command examples are in `command-contract.md`.

| Gate | Module | Proves |
|---|---|---|
| ES\|QL oracle | `verifier.live_validate` | Elasticsearch accepts the emitted ES\|QL (`real_bug` vs `data_gap`) |
| Typed UI contract | `verifier.dashboards_api` | Kibana's native Dashboards API accepts the mapped panels. The oracle maps all 11 ES\|QL visualization families the API exposes (`xy`, metric, gauge, heatmap, tag cloud, region map, data table, pie, mosaic, treemap, waffle), plus markdown. |
| Render audit | `observability_migration.targets.kibana.render_audit_driver` | panels actually render in Kibana (see below) |
| Interaction audit | `targets/kibana/interaction_{audit,scenarios,driver,runner}.py` | control selection reaches intended panels with adapter-specific evidence (see below) |
| Numeric parity | `obs-migrate compare` + `verifier.corpus_gate` | native PROMQL and translated ES\|QL are numerically close |
| Trend guard | `verifier.benchmark_gate` | success metrics + denominators don't drop vs a compatible baseline |

### Render audit (the render-truth gate)

The render audit is the only gate that proves a panel actually *renders* — it
catches Lens accessor / "Provided column name or index is invalid" / empty-state
failures that ES\|QL execution and the schema gate cannot see. It does **not**
prove that dashboard controls change the right queries; that is the interaction
audit below.

- **Verdict logic** (`targets/kibana/render_audit.py`, fully unit-tested): from a
  browser DOM snapshot + console errors + failed requests it produces a
  per-panel verdict.
- **Per-panel classification:**
  - `render_error` — an unexplained Lens/ES\|QL failure → **fail** (real bug).
  - `field_gap` — the panel's breakdown field is absent from the target's fields
    → **warn** (data-readiness, not a translator bug).
  - `data_gap` — a referenced metric is absent → **warn** (expected empty).
  - `unexpected_empty` — a query panel rendered nothing despite no known gap →
    **warn** (verify data/time window or a broken query).
- **Regression ratchet:** `render_snapshot` + `diff_render_snapshots` — the live
  per-panel outcomes must not regress vs a committed baseline.
- **Default-state control coverage:** the local render-audit script uploads
  separate `build_late_bound_grouping_canary` variants and snapshots each
  identifier-control default. Live click automation lives in the interaction
  audit.
- **Self-test:** `tests/test_render_audit_selftest.py` — a clean canary must pass
  and corrupting each panel must make the gate bite (proves it's not vacuous). It
  also pins the late-bound grouping case (issue #282): because a `by ($grouping)`
  panel's breakdown binds the stable `grouping` alias (always present in its own
  output), an "invalid column" there must be a hard `render_error`, never excused
  as a field gap.
- **Late-bound grouping canaries:** `build_late_bound_grouping_canary`
  (`core/coverage/canary.py`) supplies three default-state variants that the
  local render audit uploads (`run_render_audit_local.sh`), one each for
  `exporter`, `transport`, and `receiver`. This proves every identifier choice
  renders without brittle browser clicking, while each variant also covers the
  `by (exporter, $grouping)` collision that degrades to concrete grouping. The
  telemetry contract seeds every field-control choice and never treats
  `??grouping` itself as a physical field.
- **Label-matcher param canaries:** `build_label_matcher_param_canary` uploads
  variants for each `instance` choice so
  `metric{instance="$instance"}` → `?instance` + values control is proven in
  the same local render-audit path (gap A). Dashboard templating alone enables
  named-param binding for offline migrate; a failed live probe still drops
  matchers.

**Auth.** Serverless is behind cloud SAML SSO, which a fresh automated browser
can't pass. Two options:

1. **Persistent Chrome profile** — log in once, then point the driver at the
   profile:
   ```bash
   "<chrome>" --user-data-dir=/tmp/kb-profile "<KIBANA_URL>/login"   # log in, quit
   .venv/bin/python -m observability_migration.targets.kibana.render_audit_driver \
     --kibana-url "$KIBANA_ENDPOINT" --dashboard-id "<id>" \
     --user-data-dir /tmp/kb-profile --fail-on-error
   ```
2. **Local no-SSO Kibana** (CI default — fully automatable):
   ```bash
   STACK_VERSION=9.5.0-SNAPSHOT docker compose -f parity-rig/docker-compose.render-audit.yml up -d --wait
   bash scripts/run_render_audit_local.sh
   docker compose -f parity-rig/docker-compose.render-audit.yml down -v
   ```

### Interaction audit (control-truth gate)

The interaction audit proves that selecting a dashboard control reaches the
intended **affected** panels (and leaves unaffected panels alone) with evidence
appropriate to that adapter. ES|QL controls validate parameter/query contracts;
native action controls validate UI state and panel refresh unless a stronger
contract is documented below. It is Playwright-driven, requires Elastic Stack
**9.5+**, and stays nightly/manual until the suite has a stability history.

- **Static render audit vs interaction audit:** render audit answers "does each
  panel paint without a Lens error at default state?"; interaction audit answers
  "does this control selection rewrite the right ES\|QL and refresh the right
  panels?"
- **Adapters / capabilities** (scenario manifests under
  `parity-rig/interaction-scenarios/`): `esql_value` (including multi-select via
  `multiple: true`), `esql_interval`, `esql_function`, `esql_field`,
  `options_list`, `range_slider`, `query_bar`, `filter_pill`, `time_range`, and
  `panel_filter`. Query-bar steps currently verify the exact entered text plus
  affected/unaffected panel refresh. Kibana translates that text into filter DSL,
  so query-text assertions are rejected until the audit captures a stable filter
  DSL contract. Each control declares a capability:
  - `migrated_live` — expected to work end-to-end after migration.
  - `kibana_only` — Kibana supports it; the migrator does not emit it yet
    (synthetic canary coverage).
  - `source_only` — present in the Grafana source but not a live Kibana control.
  - `migration_gap` — known unsupported translation; must **warn**, never silently
    pass.
- **Coverage policy:** exercise every discoverable option independently; only run
  high-risk combinations that the scenario declares (for example K8s
  `cluster + job`).
- **Two-pass local flow** (`scripts/run_interaction_audit_local.sh`): optional
  bootstrap migrate → live-schema migrate + native upload → seed telemetry from
  the final YAML contract → YAML/schema lint (+ optional live ES\|QL) → resolve
  runtime panel contract → Playwright scenario. The script never starts Docker;
  the caller owns stack lifecycle (same compose file as the render audit).
- **Evidence:** request/panel correlation on ES\|QL traffic, deterministic settle
  (in-flight requests + loading markers), JSON report + optional Playwright
  traces/screenshots under `ARTIFACT_ROOT/<scenario>/<run-id>/`.
- **Results:** `pass` (clean), `warn` (expected gap / data-readiness /
  `migration_gap` / decorative control), `fail` (product or framework bug). Exit
  code is `1` only on `fail`, else `0`. Within a dashboard every interaction is
  collected; a failed earlier scenario (for example Redis) stops the shell loop
  so later dashboards are not reported as validated.
- **Local commands** (see `command-contract.md` for full knobs):
  ```bash
  make setup-browser
  make test-interactions
  STACK_VERSION=9.5.0-SNAPSHOT docker compose -f parity-rig/docker-compose.render-audit.yml up -d --wait
  STACK_VERSION=9.5.0-SNAPSHOT make interaction-audit-local
  SCENARIOS=redis-11835 bash scripts/run_interaction_audit_local.sh
  ```
  Local defaults use a thinner seed; set `FULL=1` for the denser nightly seed.
  `SKIP_MIGRATE=1 KEEP_WORK=1 WORK_DIR=...` reuses a prior final/ tree for
  browser-only iteration. If ports 9200/5601 are busy, use
  `parity-rig/docker-compose.render-audit.alt-ports.yml` with
  `ES_URL=http://localhost:9220 KIBANA_URL=http://localhost:5620`.
- **Serverless:** same persistent Chrome profile pattern as the render audit;
  hand off SSO login once, then point Playwright / the driver at that profile.
  Unattended CI uses the local no-SSO stack only.
- **CI policy:** `.github/workflows/dashboard-interaction-audit.yml` is
  schedule + `workflow_dispatch` only (no `pull_request`). Promote to a required
  PR gate only after **14 consecutive green nightly runs**, no unresolved
  framework flake, and median runtime within the 60-minute workflow budget.
  Artifacts retain for 14 days.

---

## Common tasks

### Adding a supported panel/widget type

1. Add the type to `observability_migration/core/coverage/supported_types.py`
   (`GRAFANA_SUPPORTED_PANEL_TYPES` or `DATADOG_SUPPORTED_WIDGET_TYPES`).
2. Add a matrix cell: `_PANEL_TYPES` in `tests/test_panel_matrix.py` (Grafana) or
   `_WIDGETS` in `tests/test_datadog_panel_matrix.py` (Datadog).
3. Run `make test`. `tests/core/coverage/test_supported_types.py` fails until the
   registry, code routing, and matrix agree.

### Refreshing a fidelity baseline

Only after an **intentional** fidelity change — never to silence an unexpected
regression. Grafana shown; for Datadog use `datadog-migrate` and the
`fidelity_baseline_datadog.json` baseline (full procedure in
`tests/e2e/test_fidelity_ratchet.py`):

```bash
rm -rf /tmp/corpus_g_out && mkdir -p /tmp/corpus_g_in
for f in $(git ls-files infra/grafana/dashboards/); do cp "$f" /tmp/corpus_g_in/; done
.venv/bin/grafana-migrate --source files --input-dir /tmp/corpus_g_in \
  --output-dir /tmp/corpus_g_out --assets dashboards
PYTHONPATH=parity-rig .venv/bin/python -m verifier.scorecard \
  --migration-out /tmp/corpus_g_out/dashboards \
  --baseline parity-rig/benchmark/fidelity_baseline_grafana.json --update
```

### Running the pinned community corpus

69 production dashboards from grafana.com are pinned (by id + revision +
canonical-JSON sha256) in `parity-rig/benchmark/community_corpus.json`. It is a
**stratified** manifest: each entry is tagged `stratum: top` (selected from the
most-downloaded Prometheus-backed dashboards) or `stratum: bug_seed` (an
explicit, permanently-pinned regression seed — curated prior seeds plus
dashboards once exercised as committed fixtures). The third-party JSON is **not
committed** (marketplace-noise rule); fetch it on demand and run the gate:

```bash
.venv/bin/python scripts/fetch_community_corpus.py --output-dir /tmp/community
.venv/bin/grafana-migrate --source files --input-dir /tmp/community \
  --output-dir /tmp/community_out --assets dashboards
PYTHONPATH=parity-rig .venv/bin/python -m verifier.scorecard \
  --migration-out /tmp/community_out/dashboards \
  --baseline parity-rig/benchmark/fidelity_baseline_community.json
```

Baseline reference: 1,640 panels, **0 invariant ERRORs**, 69/69 schema-valid.
This scorecard runs nightly (`.github/workflows/nightly-live-gates.yml`, job
`community-fidelity`) so the committed baseline is backed by a reproducible run,
not just a refreshed JSON file. Bump pins intentionally with `--no-verify`
(refetch) then refresh the baseline with `--update`.
`tests/test_community_corpus.py` guards the manifest offline (shape, strata, and
that the explicit regression seeds are never evicted).

### Element-checking a whole corpus

`render_audit_driver --elements --migration-out <dir>` adds a per-panel element
audit (chart kind vs the emitted type, legend series on xy/heatmap, data
present, and titles that didn't render) on top of the whole-dashboard render
check. To run it across a corpus on the local no-SSO stack, point the render
script at an input dir — it migrates, uploads, seeds, and element-checks every
dashboard:

```bash
.venv/bin/python scripts/fetch_community_corpus.py --output-dir /tmp/community
INPUT_DIR=/tmp/community bash scripts/run_render_audit_local.sh
```

Caveat: a community dashboard renders cleanly only when its metrics are seeded
and its template-variable controls resolve against the seeded label values;
otherwise the element audit honestly reports the resulting empties / data gaps.
It also surfaces real Kibana render errors (e.g. `verification_exception`,
`label_replace is not yet implemented`) that ES|QL execution alone does not show.

### Seeding telemetry for live/render checks

```bash
.venv/bin/python scripts/setup_telemetry_data.py <migration_out>/dashboards \
  --es-endpoint "$ELASTICSEARCH_ENDPOINT" --api-key "$KEY" --data-hours 3
```

> **Footgun:** `--no-recreate` reuses the existing data stream, whose
> `routing_path` may not cover a new contract's dimensions — docs whose only
> dimension is a new label are then rejected ("source didn't contain any routing
> fields"). Re-run without `--no-recreate`, or use
> `telemetry_data.routing_path_gap` to detect the mismatch. A breakdown panel
> that renders empty is usually this (a field/data gap), not a translator bug.

---

## Test file map

| Area | Path |
|---|---|
| Unit / snapshot suites | `tests/` |
| Coverage registry + cross-check | `tests/core/coverage/` |
| Panel matrices | `tests/test_panel_matrix.py`, `tests/test_datadog_panel_matrix.py` |
| Canary | `tests/test_canary.py` |
| Render audit (verdict + driver + self-test) | `tests/test_render_audit*.py` |
| Interaction audit (offline + scenarios) | `tests/test_interaction_*.py`, `tests/test_*_interaction_scenario.py` |
| e2e gates (ratchet, schema, semantic, pipelines) | `tests/e2e/` |
| Verifier gate code | `parity-rig/verifier/` |
| Committed baselines / corpus | `parity-rig/benchmark/` |
| Interaction scenario manifests | `parity-rig/interaction-scenarios/` |
| Coverage / canary engine | `observability_migration/core/coverage/` |
| Render-audit engine | `observability_migration/targets/kibana/render_audit*.py` |
| Interaction-audit engine | `observability_migration/targets/kibana/interaction_*.py` |
