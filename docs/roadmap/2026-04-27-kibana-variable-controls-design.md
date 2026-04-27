# Kibana Variable Controls in the Migration Pipeline (Phase B)

Status: Approved design (2026-04-27)
Authors: Subham Sarkar, Composer (AI pairing)
Scope: Phase B (`?value` parameter controls). Phase 2 (`??field`, `??function`, `TBUCKET`) is enumerated as a constraint section, not implemented here.

## 1. Motivation

Kibana 8.18+ introduced ES|QL **variable controls**: ES|QL parameters (`?value`, `??field`, `??function`, `TBUCKET`) wired into individual visualizations through dashboard-level controls. Reference: [Kibana dashboard interactivity with variable controls — Elastic Labs](https://www.elastic.co/search-labs/blog/kibana-dashboard-interactivity-variable-controls-overview).

Today the migration pipeline emits dashboard `controls:` of `type: options` / `range`. Real-world output (e.g. `Kubernetes / Views / Global`) shows two such controls bound to `k8s.cluster.name` and `service.name`, neither referenced by any of the dashboard's 30 ES|QL panels — the controls render in the UI but cannot filter the panel queries. The migration trace doc records **165** panels carrying the warning *"Variable-driven label filters applied via Kibana dashboard controls"* across the audited 5-dashboard Grafana corpus, and **43** equivalent warnings on the Datadog side.

This is the largest single fidelity gap in the dashboard pipeline. Phase B closes it for the well-defined subset of variable usage that admits a provably-equivalent ES|QL parameter.

## 2. Goals and non-goals

### Goals

- Emit `ESQLQuerySingleSelectControl` / `ESQLQueryMultiSelectControl` for each Grafana query variable and Datadog template variable that passes the feasibility classifier.
- Rewrite the corresponding panel ES|QL to use `WHERE field == ?varname` (single-value) or `WHERE MV_CONTAINS(?varname, field)` (multi-value).
- Per-dashboard `minimum_kibana_version`: `9.1.0` baseline; `9.3.0` only when at least one accepted multi-value variable is present.
- Replace today's misleading classic-control warning with a truthful per-panel record (`variable.bound`, `variable.unbound.classic_only`, etc.).
- Verify round-trip through `kb-dashboard-cli` and live Kibana via a CI-gated smoke test.

### Non-goals (out-of-scope for this spec)

- Datasource (`FROM`) parameterization — blocked by upstream Kibana.
- "Any" / "All" pseudo-values — blocked by upstream Kibana.
- `LIKE` filter parameterization (Datadog wildcard tags) — blocked by upstream Kibana.
- `??field` / `??function` / `TBUCKET` controls — phase 2; constraints captured in §11.
- Sibling-panel merging — phase 2 only, behind opt-in.
- Dashboard-level KQL/Lucene `query` parameterization — different code path; we emit ES|QL panels only.
- Browser/UI-render verification — out of scope per Q5.
- Removing today's classic `type: options` / `range` controls for non-eligible variables — kept for potential KQL/Lens panels added by users post-migration.
- A `--disable-esql-variable-controls` CLI flag — replaced by an emergency env-var toggle.
- Any change to existing `?_tstart` / `?_tend` time-range placeholder behavior.

## 3. Decision log

| Decision | Resolution |
|---|---|
| Rollout posture | No flag. Classifier-gated, default ON. Smoke test is a hard CI gate. Single env-var emergency disable: `OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS=1`. |
| Classifier scope | Grafana + Datadog. Single-value AND multi-value (multi via `MV_CONTAINS`). |
| Coexistence with classic controls | Replace classic with `ESQL*` for accepted variables; keep classic for rejected variables with truthful warning text. |
| Kibana floor | Per-dashboard. `9.1.0` default; `9.3.0` only if at least one accepted multi-value variable. |
| Smoke test scope | Compile + upload + saved-object read-back. No browser test. |
| Architectural approach | Mid-pipeline classifier and parameterization (Approach B); single pass; conflict-resolution by first-observed mapping. |
| `LIMIT 1000` on options query | Hard-coded in v1. |
| `WHERE <field> IS NOT NULL` on options query | Added (defense-in-depth). |
| Smoke fixtures | 4: Grafana single + multi, Datadog single + multi. |

## 4. Architecture

A new pipeline step — **variable feasibility classification** — runs once per dashboard at the boundary between `normalize` and per-panel translation, in both adapters.

```
┌──────────────────┐   ┌────────────────────────┐   ┌────────────────────────────┐   ┌──────────────────┐
│ Source dashboard │──▶│ Adapter normalize      │──▶│ NEW: VariableClassifier    │──▶│ Translation +    │
│ (Grafana JSON /  │   │ (existing)             │   │ Inputs:                    │   │ control emission │
│  Datadog JSON)   │   │                        │   │ - dashboard variables      │   │ (existing, with  │
└──────────────────┘   └────────────────────────┘   │ - first-pass field probe   │   │ binding-map      │
                                                    │ Output:                    │   │ injection)       │
                                                    │ VariableBindingMap         │   └────────────────────┘
                                                    │   var_name → Binding|None  │              │
                                                    │   - field: str             │              ▼
                                                    │   - multi: bool            │   ┌──────────────────┐
                                                    │   - reject_reason: str?    │   │ Dashboard YAML   │
                                                    │ ESQL_floor: 9.1.0 | 9.3.0  │   │  + ESQL* controls│
                                                    └────────────────────────────┘   │  + ?param panels │
                                                                                     └──────────────────┘
```

Two new modules and four existing-module touch points:

| File | Role | Change |
|---|---|---|
| `observability_migration/core/variable_classifier.py` (new) | classifier core | Types, acceptance rules, reason enum, `compute_min_kibana_version`. |
| `observability_migration/core/variable_control_verifier.py` (new) | post-translation gate | Invariant checks; downgrades binding map post-translation. |
| `observability_migration/adapters/source/grafana/promql.py` | matcher rewriting | `_matcher_to_esql` / `_frag_filters` consult binding map; emit `?varname` instead of broadening/dropping. |
| `observability_migration/adapters/source/grafana/panels.py::translate_variables` | control emission | Emit `ESQLQuerySingleSelectControl` / `ESQLQueryMultiSelectControl` for accepted variables. |
| `observability_migration/adapters/source/datadog/translate.py::_tag_filter_to_esql` | matcher rewriting | Same logic on Datadog side. |
| `observability_migration/adapters/source/datadog/generate.py::_build_controls_from_template_vars` | control emission | Symmetric Datadog control emission. |
| `observability_migration/core/reporting/report.py` | telemetry | New `variables` and `panel_parameterizations` blocks in `migration_report.json`. |

`ESQL_floor` is computed from the binding map by `compute_min_kibana_version`: `9.3.0` if any accepted binding is multi-value, else `9.1.0`. Written to the dashboard's `minimum_kibana_version` at YAML assembly.

## 5. Variable classifier

### 5.1 Data shape

```python
@dataclass(frozen=True)
class AcceptedBinding:
    field: str               # canonical ES|QL field name (post-profile resolution)
    multi: bool              # True iff the variable is multi-select
    options_query: str       # ES|QL query used by the ESQLQuerySingle/MultiSelectControl

@dataclass(frozen=True)
class RejectedBinding:
    reason: str              # closed enum, see §5.4

VariableBindingMap = dict[str, AcceptedBinding | RejectedBinding]
```

### 5.2 Acceptance rules — Grafana

A Grafana variable is **accepted** iff **all** of the following hold:

1. `type == "query"`. Other types reject with `unsupported_variable_type`.
2. The variable is **not** the driver of any panel `repeat`. Reject `drives_repeat`.
3. The `definition` (or legacy `query`) parses as `label_values(<metric_or_selector>, <label>)` yielding a single `<label>`. Else reject `unknown_definition_shape`.
4. `<label>` resolves to a **single** ES|QL field via the active field profile. Reject `field_resolution_ambiguous` (0 or >1 candidates) or `field_resolution_failed`.
5. Across **every** panel that references the variable in a matcher, the matcher field resolves to the **same** ES|QL field. Reject `inconsistent_field_use` on first conflict.
6. The matcher operator is `=` or `=~` with no regex metacharacters in the template. Reject `regex_template`.
7. `includeAll`/`*` semantics are off **or** the variable is multi-select. `includeAll: true` with single-select → reject `include_all_unsupported`.
8. `multi: true` requires `MV_CONTAINS` → marks the dashboard floor at `9.3.0`.
9. The variable name is a valid ES|QL identifier (matches `[A-Za-z_][A-Za-z0-9_]*`) and is not an ES|QL reserved word. Reject `invalid_variable_name` or `reserved_identifier`.

### 5.3 Acceptance rules — Datadog

1. The template variable has `tag` set. Reject `no_tag_field` for `scope`-only vars.
2. The default value(s) do not contain `*`. Reject `wildcard_default`.
3. The tag resolves to a single ES|QL field. Same `field_resolution_*` rejections.
4. Every panel that uses the template variable in a tag filter consistently uses the same canonical ES|QL field.
5. `multiple` is set if `default == "*"` or the available values contain multiple entries.
6. Variable used inside an OR-list mixed with non-template values → reject `mixed_or_branches`.

### 5.4 Closed reason enum

```
unsupported_variable_type
drives_repeat
unknown_definition_shape
field_resolution_ambiguous
field_resolution_failed
inconsistent_field_use
regex_template
include_all_unsupported
multi_value_with_eq_operator
data_view_split
native_promql_panel
no_tag_field
wildcard_default
mixed_or_branches
invalid_variable_name
reserved_identifier
verifier_failed_field_consistency
verifier_failed_operator_consistency
verifier_failed_leftover_token
verifier_failed_missing_param
verifier_failed_over_application
verifier_failed_data_view_split
```

The enum lives in `observability_migration/core/variable_classifier.py` as a `Final[Sequence[str]]` plus a `Literal[...]` for static checks. Adding a reason requires extending this list — preventing drift between message templates and reality.

### 5.5 Options-query template

For accepted bindings the `options_query` is generated deterministically:

```
FROM <data_view>
| WHERE <field> IS NOT NULL
| STATS BY <field>
| KEEP <field>
| LIMIT 1000
```

`<data_view>` is the data view shared by all panels referencing the variable (rejection on `data_view_split` ensures uniqueness). `LIMIT 1000` is a perf bound.

## 6. Matcher rewriting

### 6.1 Per-operator rules — Grafana

| Source matcher | Variable kind | ES\|QL emitted |
|---|---|---|
| `field="$x"` | single | `WHERE <field> == ?x` |
| `field=~"$x"` | single | `WHERE <field> == ?x` (regex without metas = literal) |
| `field!="$x"` | single | `WHERE <field> != ?x` |
| `field!~"$x"` | single | `WHERE <field> != ?x` |
| `field=~"$x"` | multi | `WHERE MV_CONTAINS(?x, <field>)` |
| `field!~"$x"` | multi | `WHERE NOT MV_CONTAINS(?x, <field>)` |
| `field="$x"` | multi | reject `multi_value_with_eq_operator` |
| `field=~"prefix-$x.*"` (any regex meta in template) | any | reject `regex_template` |

### 6.2 Per-operator rules — Datadog

| Source filter | Variable kind | ES\|QL emitted |
|---|---|---|
| `tag:$x` (single value) | single | `WHERE <field> == ?x` |
| `tag:$x.value` (single value) | single | `WHERE <field> == ?x` |
| `tag:$x` with multi defaults | multi | `WHERE MV_CONTAINS(?x, <field>)` |
| `tag:$x` followed by `*` | any | reject `wildcard_default` |
| Inside an `or:` list mixing `$x` with non-template values | any | reject `mixed_or_branches` |

### 6.3 What the rewriter never touches

- `legendFormat`, `displayName`, panel titles — display strings, not ES|QL.
- The `options_query` itself — emitted later from the binding map.
- ES|QL fragments inside `PROMQL value=(...)` native-PromQL panels — variable would survive into wrapped PromQL with different semantics. Reject `native_promql_panel`.
- Datadog `LIKE` paths in `_tag_filter_to_esql` — already classifier-rejected.
- Grafana repeat-driver variables — already classifier-rejected.

## 7. Verifier (post-translation correctness gate)

A new `observability_migration/core/variable_control_verifier.py` runs after translation but before YAML serialization. It receives `(dashboard, binding_map, panel_translation_records)` and returns a possibly-mutated binding map where any variable that fails an invariant becomes `RejectedBinding(reason="verifier_failed_<which>")`.

Hard correctness invariants:

1. **Field consistency.** Per-panel observed field must match `binding.field`. First conflict downgrades.
2. **Operator consistency.** Mixed exact-match and multi-value family usages downgrade.
3. **No leftover `$x`.** No literal `$<varname>` (word-boundary match) may remain in any panel ES|QL.
4. **`?x` accounted for.** Every panel listed as referencing `$x` must contain `?x` in its compiled ES|QL.
5. **No `?x` over-application.** No panel that did *not* reference `$x` may contain `?x` in the output.
6. **`data_view` consistency.** All panels referencing `$x` must use the same `data_view`.

If a variable is downgraded, panels that already had `?x` injected are walked again and `?x` is replaced back to the original lossy behavior (broaden to `=~".*"` or drop with `had_vars=True`). The translation record carries both parameterized and rejected fragments; the finalize step picks one based on the post-verifier binding map.

The verifier is implemented as a list of pure functions `(records, binding_map) -> list[Downgrade]`. Phase 2 adds entries without modifying existing ones.

## 8. YAML emission

### 8.1 Single-select (Kibana 9.1.0+)

```yaml
- type: esql
  variable_name: instance
  variable_type: values
  multiple: false
  label: instance
  query: |
    FROM metrics-*
    | WHERE service.instance.id IS NOT NULL
    | STATS BY service.instance.id
    | KEEP service.instance.id
    | LIMIT 1000
```

### 8.2 Multi-select (Kibana 9.3.0+)

```yaml
- type: esql
  variable_name: instance
  variable_type: multi_values
  multiple: true
  label: instance
  query: |
    FROM metrics-*
    | WHERE service.instance.id IS NOT NULL
    | STATS BY service.instance.id
    | KEEP service.instance.id
    | LIMIT 1000
```

### 8.3 Field-by-field rules

- **`variable_name`** matches the source name exactly (case-sensitive). Sanitization is rejected — a non-identifier name fails the classifier with `invalid_variable_name`.
- **`label`** copies the source variable's user-facing label, falling back to `variable_name`.
- **`type: esql`**, **`variable_type`** fixed by kind (`values` vs `multi_values`).
- **`query`** generated from §5.5 template.
- **`default`** populated only when source has a non-empty, non-`*` default value.
- **`id`** omitted; Kibana auto-assigns.
- **`width`** omitted; defaults to `medium`.

### 8.4 Control ordering

Top-level `controls:` array contains:
1. Accepted-variable `ESQL*` controls in source variable order.
2. Rejected-variable classic controls (`type: options` / `range`) after.

Parameterized controls render visually first; legacy controls follow.

## 9. Per-dashboard version floor

```python
def compute_min_kibana_version(binding_map: VariableBindingMap) -> str:
    has_multi_value = any(
        isinstance(b, AcceptedBinding) and b.multi
        for b in binding_map.values()
    )
    return "9.3.0" if has_multi_value else "9.1.0"
```

Rules:

1. The result is written before any panel/control is serialized; no path may downgrade after.
2. Per-dashboard, never global. Two dashboards in the same run can have different floors.
3. Rejected multi-value variables do not lift the floor (they fall back to classic controls which work on 9.1.0).
4. Verifier-driven downgrade re-runs the function on the post-verifier binding map; a downgraded multi-value variable yields a 9.1.0 floor.
5. The migration report records the decision and reason per dashboard:

   ```json
   {
     "dashboard": "Kubernetes / Views / Global",
     "minimum_kibana_version": "9.3.0",
     "version_floor_reason": "multi_value_binding(job)"
   }
   ```

## 10. Warning catalog and telemetry

### 10.1 Warning IDs

| ID | Replaces | Template | Severity |
|---|---|---|---|
| `variable.bound` | (new) | `"filter applied via ES|QL parameter ?{var} (field={field}, kind={kind})"` | info |
| `variable.unbound.classic_only` | "Variable-driven label filters applied via Kibana dashboard controls" | `"variable {var} not bound to translated ES|QL panel queries (reason: {reason}); the dashboard's classic control still applies to any KQL/Lens panels added manually"` | warning |
| `variable.unbound.dropped` | "Dropped variable-driven label filters during migration" | `"variable {var} dropped during translation (reason: {reason}); no equivalent filter applied"` | warning |
| `variable.verifier_downgraded` | (new) | `"variable {var} accepted by classifier but downgraded post-translation (verifier failure: {invariant}); falling back to classic control"` | warning |

`variable.bound.partial` is not emitted — its absence is a correctness invariant guaranteed by the verifier.

### 10.2 Aggregator change

`docs/sources/grafana-trace.md` aggregates "Top Warning Patterns" by exact-string match. Phase B switches the aggregator to use the structured warning ID. One-line change to the trace doc generator; non-breaking for JSON consumers.

### 10.3 Migration-report shape

Per-dashboard entries in `migration_report.json` gain:

```json
{
  "variables": {
    "accepted": [
      {"name": "instance", "field": "service.instance.id", "multi": false}
    ],
    "accepted_fields": [],
    "accepted_functions": [],
    "accepted_intervals": [],
    "rejected": [
      {"name": "namespace", "reason": "include_all_unsupported"},
      {"name": "datasource", "reason": "unsupported_variable_type"}
    ],
    "verifier_downgraded": []
  },
  "panel_parameterizations": {
    "?instance": 12,
    "?cluster": 0
  }
}
```

`accepted_fields`/`accepted_functions`/`accepted_intervals` are empty in phase B and populated in phase 2. Existing keys are not removed or renamed; consumers that parse `accepted` keep working.

## 11. Phase 2 plan and constraints

Phase 2 covers `??field`, `??function`, and `TBUCKET` controls. It lands after phase B is stable in production. Phase B must preserve:

1. **Sibling-panel detection.** Per-panel translation records are kept in memory across the dashboard translation (already required by the verifier).
2. **Column-name inference compatibility.** `extract_esql_columns` parses `STATS` to discover columns. Phase B never inserts `?param` inside `STATS`; only in `WHERE`. Translation records carry a `phase_b_parameterized: true` flag so phase 2's column-inference branch knows where to expect parameters.
3. **`minimum_kibana_version` extensibility.** `compute_min_kibana_version` takes a `VariableBindingMap`; phase 2 extends with new accepted-binding variants.
4. **No silent panel merging.** Phase B is 1:1 source-panel to target-panel. Phase 2 may merge sibling panels, but only behind explicit per-dashboard opt-in.
5. **Verifier extensibility.** Implemented as a list of checks; phase 2 appends.

Phase 2 risks (called out for future-proofing, not addressed here):

- Panel merging is a behavior change, not a faithful translation. Defaults must be off.
- `??function` semantics: 0-ary vs 1-ary aggregators must group separately.
- `TBUCKET` interval bounds: must be derived from the dashboard's typical time range.
- Column-name parameterization (alias on the left of `=` in `STATS x = AVG(...)`) is unsupported; the verifier must enforce that phase 2 never emits a parameter in that position.

## 12. Test strategy

### Layer 1 — Hermetic unit tests (~70 tests, sub-second, no I/O)

- Classifier acceptance rules: one test per `reason` code (~22 tests).
- Field/operator-consistency conflict detection.
- Matcher rewriter table: one test per row of §6.1 and §6.2.
- Verifier invariants: one negative test per invariant (§7).
- Verifier idempotency.
- `compute_min_kibana_version`: 5 cases.
- Warning catalog: one test per template ID.
- YAML emission: snapshot tests for both control shapes.

### Layer 2 — Hermetic integration tests (~15 tests, ~2m total)

For each `infra/grafana/dashboards/*.json` and `infra/datadog/dashboards/*.json`:

- Every accepted variable appears as `?<varname>` in at least one emitted panel.
- No accepted variable's source name `$<varname>` survives anywhere.
- Every rejected variable appears as a classic control or is absent (for unsupported types).
- Dashboard `minimum_kibana_version` matches `compute_min_kibana_version(binding_map)`.
- `migration_report.json` `variables` block validates against a JSON schema.

Plus a regression baseline at `tests/fixtures/regression/grafana_corpus_phase_b.json` (re-blessed only with intent), an end-to-end no-leftovers check, and idempotency (two runs produce byte-identical YAML).

### Layer 3 — Live-Kibana smoke tests (4 tests, ~5m total)

Lives in `tests/e2e/test_variable_controls_smoke.py`. Marked `@pytest.mark.live_kibana`. Fixtures under `tests/fixtures/variable_controls/`:

- `grafana_single_value.json`
- `grafana_multi_value.json`
- `datadog_single_value.json`
- `datadog_multi_value.json`

Per fixture, 5 stages: translate → compile → assert NDJSON shape → upload → read-back → cleanup.

Credentials via env vars `KIBANA_URL` and `KIBANA_API_KEY`. The mark-config logic:

- If env vars missing AND `OBS_MIGRATION_LIVE_KIBANA_REQUIRED=1` (CI default): fail loudly.
- Else: `pytest.skip` (local dev).

### CI wiring

```yaml
unit-tests          (Layer 1 + most of Layer 2; ~30s)   ← required for merge
integration-tests   (rest of Layer 2; ~2m)              ← required for merge
live-kibana-smoke   (Layer 3; ~5m)                      ← required for merge to main
```

`live-kibana-smoke` is `needs: [unit-tests]` so we never burn live-Kibana minutes on PRs that already failed hermetic checks.

## 13. Risks and rollback

### 13.1 Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | `kb-dashboard-cli` upstream regresses on variable-control shapes | medium | high | Pin a known-good `kb-dashboard-cli`; the live-Kibana smoke test catches regressions. |
| 2 | Kibana 9.x server-side parsing changes | low | high | Smoke read-back catches it; emergency disable via `OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS=1`. |
| 3 | Classifier accepts a variable the verifier downgrades | high (initial) | low | Diagnostic, not a bug; visible in migration report. |
| 4 | Multi-value dashboard uploaded to Kibana < 9.3.0 | low | medium | Per-dashboard `minimum_kibana_version` contract; pre-upload guard in `targets/kibana/serverless.py` GETs `/api/status` and refuses to upload below the floor. |
| 5 | Reserved-identifier collision | very low | medium | Classifier rejects with `reserved_identifier`. |
| 6 | `LIMIT 1000` truncates a dropdown | low | low | Documented; revisit post-release if needed. |
| 7 | `data_view` mismatch across panels | medium | medium | Classifier rejects with `data_view_split`. |
| 8 | Live-Kibana smoke flakiness | medium | medium | Single retry on transient HTTP 5xx/timeouts; no retries on assertion failures. |
| 9 | Migration-report schema break for downstream consumers | low | medium | New keys added; existing keys preserved; integration test enforces. |
| 10 | Phase 2 design tension forces phase B refactor | low | medium | §11 enumerates constraints; reviewed during this brainstorm. |

### 13.2 Rollback

1. **Immediate, no code change:** set `OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS=1`. The classifier short-circuits to "all variables rejected", restoring exactly today's behavior. Documented in `docs/targets/kibana.md`.
2. **Hotfix:** the implementation plan structures phase B as a small final commit that activates the classifier in the translator pipeline (preceded by additive commits that introduce the classifier, verifier, and emission code without wiring them into translation). Reverting that single activation commit returns the pipeline to today's behavior without touching the new modules. The implementation plan must preserve this structure.
3. **Full revert:** revert the merge of the `feature/kibana-variable-controls` branch. No schema or saved-object migration is needed — schema already supports the shapes — so revert is purely code-level.

## 14. Out-of-scope (firm)

- Datasource (`FROM`) parameterization.
- "Any" / "All" pseudo-values.
- `LIKE` filter parameterization.
- `??field` / `??function` / `TBUCKET` controls (phase 2).
- Sibling-panel merging (phase 2 only).
- Dashboard-level KQL/Lucene `query` parameterization.
- Browser/UI-render verification.
- Auto-bumping Kibana floor globally to 9.3.0.
- Removing classic `type: options` / `range` controls for non-eligible variables.
- A `--disable-esql-variable-controls` CLI flag.
- Any change to `?_tstart` / `?_tend` time-range placeholder behavior.
