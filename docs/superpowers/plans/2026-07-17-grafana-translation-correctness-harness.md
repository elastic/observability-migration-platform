# Grafana Translation Correctness Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an offline Grafana PromQL → ES|QL structural oracle, emitter-path matrix, fixture corpus gate, disposition fix for alias-shaped Unknown column, and a seed-intake mutation self-test — so the recent STATS/EVAL failure class cannot regress on one emitter while another stays clean.

**Architecture:** A pure `esql_structural_oracle` in the Grafana adapter package checks every emitted ES|QL string in tests/CI. An emitter registry + matrix proves each fusion path is exercised. Fixture dashboards are translated and oracle-checked in PR CI. Nightly/live failures feed committed seeds via a script with a mutation self-test. Production translate stays degrade-graceful (shared `_finalize_fused_stats_assignments` already repairs); the oracle does not crash operators.

**Tech Stack:** Python 3.11+, pytest, existing Grafana translate/panels/promql modules, `docs/testing.md`, Hypothesis (optional property hook).

**Spec:** `docs/superpowers/specs/2026-07-17-grafana-translation-correctness-harness-design.md`  
**Deferred:** https://github.com/elastic/observability-migration-platform/issues/301

## Global Constraints

- v1 is **Grafana PromQL → ES|QL only** (no Datadog/alerts in this plan).
- Oracle hard-fails in **tests/CI only**; production does not raise on oracle miss.
- Skip oracle when query starts with `PROMQL` (native passthrough).
- Prefer shared finalize repair in emitters; oracle catches skipped repair.
- Do not commit secrets or live smoke artifact dirs.
- Update `docs/testing.md` in the same change set as operator-visible harness behavior.
- Mirror skill edits are N/A; docs yes.

## File structure

| Path | Responsibility |
|---|---|
| `observability_migration/adapters/source/grafana/esql_structural_oracle.py` | Pure oracle: rules → `StructuralFinding` list |
| `observability_migration/adapters/source/grafana/esql_emitters.py` | Emitter path registry + path-token helpers |
| `tests/test_esql_structural_oracle.py` | Unit tests for each ERROR/WARNING rule |
| `tests/test_grafana_esql_emitter_matrix.py` | One cell per emitter + registry completeness |
| `tests/test_grafana_fixture_structural_gate.py` | Translate all infra fixtures; oracle ERROR = fail |
| `tests/fixtures/translation_seeds/` | Committed regression seeds (JSON) |
| `scripts/intake_translation_seeds.py` | Propose seeds from live/smoke JSON reports |
| `tests/test_translation_seed_intake.py` | Mutation self-test for intake |
| `observability_migration/core/verification/disposition.py` (+ callers) | Alias-shaped Unknown column ≠ self-heal data gap |
| `docs/testing.md` | Document harness tiers / how to add a seed |

---

### Task 1: Structural oracle core (ERROR rules)

**Files:**
- Create: `observability_migration/adapters/source/grafana/esql_structural_oracle.py`
- Create: `tests/test_esql_structural_oracle.py`

**Interfaces:**
- Produces:
  - `StructuralSeverity` enum: `error`, `warning`
  - `StructuralRuleId` str enum including `STATS_CASE_BARE_TS_MIX`, `STATS_BARE_WRAPPED_OVER_TIME_MIX`, `EVAL_UNDEFINED_COLUMN`, `EMPTY_FEASIBLE_QUERY`, `MIXED_IRATE_AVG_OVER_TIME`
  - `@dataclass StructuralFinding(rule_id: StructuralRuleId, severity: StructuralSeverity, message: str, evidence: dict)`
  - `def check_esql_structure(query: str, *, feasibility: str | None = None, require_stats_for_feasible: bool = False) -> list[StructuralFinding]`
  - `def structural_errors(findings: list[StructuralFinding]) -> list[StructuralFinding]`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_esql_structural_oracle.py
from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    StructuralRuleId,
    check_esql_structure,
    structural_errors,
)

def test_case_bare_irate_mix_is_error():
    q = (
        "TS metrics-*\n"
        '| STATS a = SUM(IRATE(CASE((mode == "user"), m, NULL), 1m)), '
        "b = SUM(IRATE(other, 1m)) BY time_bucket = TBUCKET(5 minute)\n"
    )
    errs = structural_errors(check_esql_structure(q))
    assert any(f.rule_id == StructuralRuleId.STATS_CASE_BARE_TS_MIX for f in errs)

def test_case_true_wrap_is_clean():
    q = (
        "TS metrics-*\n"
        '| STATS a = SUM(IRATE(CASE((mode == "user"), m, NULL), 1m)), '
        "b = SUM(IRATE(CASE(true, other, NULL), 1m)) BY time_bucket = TBUCKET(5 minute)\n"
    )
    assert structural_errors(check_esql_structure(q)) == []

def test_bare_and_wrapped_over_time_mix_is_error():
    q = (
        "TS metrics-*\n"
        "| STATS a = AVG(AVG_OVER_TIME(x, 5m)), b = AVG_OVER_TIME(y, 5m) "
        "BY time_bucket = TBUCKET(5 minute), instance\n"
    )
    errs = structural_errors(check_esql_structure(q))
    assert any(f.rule_id == StructuralRuleId.STATS_BARE_WRAPPED_OVER_TIME_MIX for f in errs)

def test_eval_undefined_column_is_error():
    q = (
        "TS metrics-*\n"
        "| STATS freq_B = MAX(LAST_OVER_TIME(freq)) BY time_bucket = TBUCKET(5 minute)\n"
        "| EVAL CPU = freq\n"
    )
    errs = structural_errors(check_esql_structure(q))
    assert any(f.rule_id == StructuralRuleId.EVAL_UNDEFINED_COLUMN for f in errs)

def test_eval_renamed_alias_is_clean():
    q = (
        "TS metrics-*\n"
        "| STATS freq_B = MAX(LAST_OVER_TIME(freq)) BY time_bucket = TBUCKET(5 minute)\n"
        "| EVAL CPU = freq_B\n"
    )
    assert structural_errors(check_esql_structure(q)) == []

def test_promql_passthrough_skipped():
    assert check_esql_structure("PROMQL index=metrics-* value=(up)") == []

def test_empty_feasible_query_is_error():
    errs = structural_errors(
        check_esql_structure("", feasibility="feasible", require_stats_for_feasible=True)
    )
    assert any(f.rule_id == StructuralRuleId.EMPTY_FEASIBLE_QUERY for f in errs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_esql_structural_oracle.py -q --tb=line`  
Expected: FAIL (module missing)

- [ ] **Step 3: Implement oracle**

Implement `esql_structural_oracle.py`:

- Split pipeline on `\n| ` / `| `.
- Parse `STATS` assignments with top-level CSV split (reuse
  `observability_migration.adapters.source.grafana.promql._split_top_level_csv`
  or the canonical `split_top_level` from `esql_utils` if already imported there —
  prefer importing `_split_top_level_csv` from promql to avoid drift).
- Reuse the same bare-TS regex spirit as `_BARE_TS_VALUE_ARG` in `promql.py`
  (do not silently diverge; either import the compiled regex or duplicate with a
  comment linking to it).
- Detect wrapped form: `=\s*(AVG|SUM|MIN|MAX|COUNT)\(\s*[A-Z_]+_OVER_TIME\(`.
- Detect bare OVER_TIME assignment: `=\s*[A-Z_]+_OVER_TIME\(`.
- Track defined columns from STATS LHS and EVAL LHS; for `EVAL x = ident` only
  (RHS fullmatch `[A-Za-z_][A-Za-z0-9_.]*`), require ident ∈ defined.
- If query strip upper-starts with `PROMQL`, return `[]`.
- `MIXED_IRATE_AVG_OVER_TIME`: WARNING only when STATS has both a
  `RATE|IRATE|INCREASE(` and a bare `*_OVER_TIME(` — never elevate to ERROR in v1.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_esql_structural_oracle.py -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add observability_migration/adapters/source/grafana/esql_structural_oracle.py \
  tests/test_esql_structural_oracle.py
git commit -m "$(cat <<'EOF'
feat(grafana): add ES|QL structural oracle for fused STATS/EVAL invariants

Offline checker for CASE+bare TS mixes, bare vs wrapped OVER_TIME, and
EVAL references to undefined columns after STATS alias rename.
EOF
)"
```

---

### Task 2: Wire oracle into existing merge/join regression tests

**Files:**
- Modify: `tests/test_multi_target_merge_aliases.py`
- Modify: `tests/test_grafana_extended.py` (filtered-ratio honesty test only if useful)
- Modify: `tests/test_migrate.py` (`test_binary_ratio_keeps_irate_and_warns_per_operand`)

**Interfaces:**
- Consumes: `check_esql_structure`, `structural_errors` from Task 1

- [ ] **Step 1: Add oracle asserts to merge/join tests**

At end of each successful translation/merge assertion in
`tests/test_multi_target_merge_aliases.py`, add:

```python
from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    structural_errors,
    check_esql_structure,
)

assert structural_errors(check_esql_structure(query)) == []
```

Do the same for the join-ratio test and the Node Exporter fixture smoke loop.

- [ ] **Step 2: Run related tests**

Run:  
`.venv/bin/python -m pytest tests/test_multi_target_merge_aliases.py tests/test_esql_structural_oracle.py -q`  
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_multi_target_merge_aliases.py
git commit -m "$(cat <<'EOF'
test(grafana): assert structural oracle on merge and join-ratio regressions

EOF
)"
```

---

### Task 3: Emitter path registry + matrix

**Files:**
- Create: `observability_migration/adapters/source/grafana/esql_emitters.py`
- Create: `tests/test_grafana_esql_emitter_matrix.py`

**Interfaces:**
- Produces:
  - `GRAFANA_ESQL_EMITTERS: tuple[str, ...]` frozen ordered names:
    - `single_target_formula`
    - `join_family_ratio`
    - `shared_measure_pipeline`
    - `pretranslated_xy_merge`
    - `same_metric_collapse` (only if a reliable path token exists; otherwise omit from v1 registry and note in matrix docstring — do not invent a fake cell)
  - `EMITTER_HELPER_SYMBOLS: dict[str, str]` mapping emitter id → defining module attribute name for the completeness grep (e.g. `pretranslated_xy_merge` → `_merge_pretranslated_xy_queries`)

- [ ] **Step 1: Write failing matrix test skeleton**

```python
# tests/test_grafana_esql_emitter_matrix.py
from observability_migration.adapters.source.grafana.esql_emitters import (
    GRAFANA_ESQL_EMITTERS,
    EMITTER_HELPER_SYMBOLS,
)
from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    check_esql_structure,
    structural_errors,
)

def test_registry_symbols_exist_in_source():
    from pathlib import Path
    root = Path("observability_migration/adapters/source/grafana")
    text = "\n".join(p.read_text() for p in root.glob("*.py"))
    for emitter_id, symbol in EMITTER_HELPER_SYMBOLS.items():
        assert f"def {symbol}" in text or f"{symbol} =" in text, emitter_id

def test_every_emitter_has_matrix_cell():
    # Import cell map defined below in this module
    assert set(MATRIX_CELLS) == set(GRAFANA_ESQL_EMITTERS)
```

Plus one pytest parametrize or explicit test per emitter that:

1. Builds the minimal PromQL/panel for that path.
2. Asserts a path token (warning substring or metadata key) proving the path.
3. Runs `structural_errors(check_esql_structure(query)) == []`.

**Minimal path tokens (use these):**

| Emitter | How to force path | Token |
|---|---|---|
| `join_family_ratio` | `/ on(x) group_left` ratio PromQL via `translate_promql_to_esql` | `numerator =` and `denominator =` in query |
| `shared_measure_pipeline` | Multi-target panel that formula-fuses (two simple rates, same BY) | warning absent of pretranslated fuse string; or provenance without `whole_translated` |
| `pretranslated_xy_merge` | Multi-target that falls into merge (guest CPU two join-ratio targets, or frequency gauges) | warning contains `Fused multi-target panel from independently translated` |
| `single_target_formula` | Simple `sum(rate(http_requests_total[5m]))` | single STATS, no fuse warning |

- [ ] **Step 2: Run matrix test — expect fail (module missing)**

Run: `.venv/bin/python -m pytest tests/test_grafana_esql_emitter_matrix.py -q --tb=line`  
Expected: FAIL

- [ ] **Step 3: Implement `esql_emitters.py` + fill MATRIX_CELLS tests**

Keep fixtures inline in the test file (small dicts), not large JSON.

- [ ] **Step 4: Run matrix — expect pass**

Run: `.venv/bin/python -m pytest tests/test_grafana_esql_emitter_matrix.py -q`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add observability_migration/adapters/source/grafana/esql_emitters.py \
  tests/test_grafana_esql_emitter_matrix.py
git commit -m "$(cat <<'EOF'
feat(grafana): add ES|QL emitter path matrix and registry guard

EOF
)"
```

---

### Task 4: Fixture corpus structural gate

**Files:**
- Create: `tests/test_grafana_fixture_structural_gate.py`

**Interfaces:**
- Consumes: `translate_panel`, `check_esql_structure`, `structural_errors`

- [ ] **Step 1: Write the gate test**

```python
# tests/test_grafana_fixture_structural_gate.py
from pathlib import Path
import json
from observability_migration.adapters.source.grafana.panels import translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    check_esql_structure,
    structural_errors,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "infra" / "grafana" / "dashboards"

def _walk(panels, out):
    for p in panels or []:
        if p.get("type") == "row":
            _walk(p.get("panels"), out)
            continue
        out.append(p)
        _walk(p.get("panels"), out)

def test_all_infra_grafana_fixtures_are_structurally_clean():
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    failures = []
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        dash = json.loads(path.read_text())
        panels = []
        _walk(dash.get("panels"), panels)
        for row in dash.get("rows") or []:
            _walk(row.get("panels"), panels)
        for panel in panels:
            if panel.get("type") in {"row", "text", "news", "dashlist", "alertlist"}:
                continue
            try:
                yaml_panel, result = translate_panel(
                    panel,
                    datasource_index="metrics-*",
                    esql_index="metrics-*",
                    rule_pack=rule_pack,
                    resolver=resolver,
                )
            except Exception as exc:  # pragma: no cover
                failures.append(f"{path.name}:{panel.get('title')}: translate crashed: {exc}")
                continue
            if result.status in {"requires_manual", "skipped"}:
                continue
            query = (yaml_panel or {}).get("esql", {}).get("query") or ""
            if not query:
                continue
            errs = structural_errors(
                check_esql_structure(query, feasibility="feasible", require_stats_for_feasible=False)
            )
            for err in errs:
                failures.append(
                    f"{path.name} :: {panel.get('title')!r} :: {err.rule_id.value}: {err.message}"
                )
    assert not failures, "structural oracle failures:\n" + "\n".join(failures[:50])
```

If the gate finds pre-existing WARNINGS-only issues, ignore (use `structural_errors` only).  
If it finds pre-existing ERRORs on fixtures, **fix the emitter** (do not weaken the oracle) unless the query is intentionally non-TS markdown — then skip those panel types.

- [ ] **Step 2: Run gate**

Run: `.venv/bin/python -m pytest tests/test_grafana_fixture_structural_gate.py -q --tb=line`  
Expected: PASS (or FAIL with list → fix emitters before continuing)

- [ ] **Step 3: Commit**

```bash
git add tests/test_grafana_fixture_structural_gate.py
# plus any emitter fixes required
git commit -m "$(cat <<'EOF'
test(grafana): gate infra fixtures through ES|QL structural oracle

EOF
)"
```

---

### Task 5: Disposition — alias-shaped Unknown column is not self-heal

**Files:**
- Modify: `observability_migration/core/verification/disposition.py`
- Modify: callers/tests that treat all Unknown column as data gap — search `validation_failure_self_heals`, `unknown_columns`, and verifier disposition classifiers under `parity-rig/verifier/` and `tests/test_obs_migrate_verify.py`
- Create/extend: `tests/test_disposition.py` or existing disposition tests

**Interfaces:**
- Produces:
  - `def unknown_column_looks_like_alias_bug(column_name: str, esql_query: str | None) -> bool`  
    True when `column_name` appears as a STATS LHS **prefix** that was renamed away (e.g. query has `freq_B =` / `EVAL CPU = freq_B` but error cites `freq`), or column appears in EVAL RHS but not as any STATS/EVAL LHS.
  - Update `validation_failure_self_heals` to return False when any unknown column looks like an alias bug **and** the esql query is provided on the validation_result (e.g. `validation_result.get("esql_query")` or analysis field — add a documented optional key `esql_query` without breaking callers that omit it).

- [ ] **Step 1: Write failing unit tests for alias detection**

```python
def test_unknown_column_matching_pre_rename_stats_alias_is_not_self_heal():
    query = (
        "| STATS freq_B = MAX(LAST_OVER_TIME(freq)) BY time_bucket = TBUCKET(5 minute)\n"
        "| EVAL CPU = freq\n"
    )
    # Even if analysis lists unknown column freq, this is translator-shaped.
    assert unknown_column_looks_like_alias_bug("freq", query) is True

def test_unknown_metric_not_in_query_still_self_heals():
    query = "| STATS x = AVG(RATE(http_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute)\n"
    assert unknown_column_looks_like_alias_bug("http_requests_total", query) is False
```

Clarify the second case: if the metric is in the query as a field arg, Unknown column may still be data gap — `unknown_column_looks_like_alias_bug` should be True only for **output-column** confusion (EVAL/STATS alias), not physical metric absence. Implement:

- True if `column` is used as a simple EVAL RHS and is not in the defined-column set inferred from the query.
- False if `column` never appears as an identifier in EVAL/STATS LHS or simple EVAL RHS (pure missing metric → leave self-heal behavior).

- [ ] **Step 2: Run — expect fail**

- [ ] **Step 3: Implement + update `validation_failure_self_heals`**

```python
def validation_failure_self_heals(validation_result):
    analysis = (validation_result or {}).get("analysis") or {}
    if analysis.get("counter_mismatch_metrics"):
        return False
    query = (validation_result or {}).get("esql_query")
    for col in analysis.get("unknown_columns") or []:
        name = col.get("name", "") if isinstance(col, dict) else str(col)
        if query and unknown_column_looks_like_alias_bug(name, query):
            return False
    return bool(analysis.get("unknown_columns") or analysis.get("unknown_indexes"))
```

- [ ] **Step 4: Run disposition + verify tests**

Run: `.venv/bin/python -m pytest tests/test_disposition.py tests/test_obs_migrate_verify.py -q --tb=line`  
(adjust paths to whatever tests exist)

- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
fix(verify): do not treat alias-shaped Unknown column as self-healing data gap

EOF
)"
```

---

### Task 6: Seed intake script + mutation self-test

**Files:**
- Create: `scripts/intake_translation_seeds.py`
- Create: `tests/fixtures/translation_seeds/.gitkeep` (and one example seed if useful)
- Create: `tests/test_translation_seed_intake.py`

**Interfaces:**
- Produces CLI:
  - `python scripts/intake_translation_seeds.py --report <live_validate_or_smoke.json> --out-dir tests/fixtures/translation_seeds --dry-run`
  - Writes `tests/fixtures/translation_seeds/<slug>.json` with keys:
    `id`, `source`, `panel_title`, `promql_or_targets`, `esql_query`, `error`, `disposition`, `rule_hint`
  - Exit 0 on dry-run with printed proposals; exit 1 if `--check` and new seeds would be written (for nightly “uncommitted seeds” mode)

- [ ] **Step 1: Write mutation self-test**

```python
def test_intake_proposes_seed_for_structural_mutation(tmp_path):
    # Synthesize a smoke report entry with real_bug + ClassCast / Unknown column
    # alias error and a panel snippet; run intake in dry-run; assert one proposal.
    ...
```

Include a helper that corrupts a known-good query (strip CASE(true) wrap or break EVAL alias) and shows the oracle would ERROR — proving intake + oracle link (like render-audit self-test).

- [ ] **Step 2: Implement script (stdlib argparse + json only)**

Do not call live ES from the script; only parse report JSON.

Minimal report schema accepted (document in script docstring):

```json
{
  "panels": [
    {
      "title": "CPU Frequency Scaling",
      "status": "fail",
      "disposition": "real_bug",
      "error": "Unknown column [node_cpu_scaling_frequency_hertz]",
      "esql_query": "...",
      "targets": [{"expr": "..."}]
    }
  ]
}
```

Skip entries with `disposition` in `{data_gap, field_gap}` unless
`unknown_column_looks_like_alias_bug` flips them.

- [ ] **Step 3: Run self-test**

Run: `.venv/bin/python -m pytest tests/test_translation_seed_intake.py -q`  
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add scripts/intake_translation_seeds.py tests/test_translation_seed_intake.py \
  tests/fixtures/translation_seeds/.gitkeep
git commit -m "$(cat <<'EOF'
feat(grafana): add translation seed intake script and mutation self-test

EOF
)"
```

---

### Task 7: Optional property hook + docs

**Files:**
- Modify: `tests/test_promql_property.py`
- Modify: `docs/testing.md`
- Modify: `docs/superpowers/specs/2026-07-17-grafana-translation-correctness-harness-design.md` (status → Implemented when done)

**Interfaces:**
- Consumes: `check_esql_structure`, `structural_errors`

- [ ] **Step 1: In property test, after a feasible translation, assert oracle clean**

```python
from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    check_esql_structure,
    structural_errors,
)
# inside the hypothesis example, when feasibility is feasible:
errs = structural_errors(check_esql_structure(result.esql_query or ""))
assert not errs, errs
```

Keep Hypothesis `max_examples` unchanged for PR time.

- [ ] **Step 2: Document in `docs/testing.md`**

Add a short subsection under Tier 3:

- What `esql_structural_oracle` checks
- Emitter matrix location
- Fixture structural gate
- How to add a seed (`scripts/intake_translation_seeds.py`, commit under `tests/fixtures/translation_seeds/`)
- Link to deferred issue #301

- [ ] **Step 3: Run property + docs-adjacent tests if any**

Run:  
`.venv/bin/python -m pytest tests/test_promql_property.py tests/test_esql_structural_oracle.py tests/test_grafana_esql_emitter_matrix.py tests/test_grafana_fixture_structural_gate.py tests/test_translation_seed_intake.py -q`  
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_promql_property.py docs/testing.md \
  docs/superpowers/specs/2026-07-17-grafana-translation-correctness-harness-design.md
git commit -m "$(cat <<'EOF'
docs(testing): document Grafana ES|QL structural harness and seed intake

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `esql_structural_oracle` ERROR rules | Task 1 |
| Skip `PROMQL` passthrough | Task 1 |
| Wire into merge/join regressions | Task 2 |
| Emitter path matrix + registry | Task 3 |
| Fixture corpus gate | Task 4 |
| Disposition alias Unknown column | Task 5 |
| Seed intake + mutation self-test | Task 6 |
| Property layer optional | Task 7 |
| `docs/testing.md` | Task 7 |
| Datadog/alerts/full surface | Deferred #301 (no task) |

## Plan self-review

- No TBD/placeholder steps remaining.
- Types consistent: `StructuralFinding` / `check_esql_structure` / `structural_errors` used throughout.
- Production fail-open honored (oracle in tests, not raising from `translate_*`).
- Fixture gate may surface pre-existing ERRORs — task instructs fixing emitters, not weakening rules.
