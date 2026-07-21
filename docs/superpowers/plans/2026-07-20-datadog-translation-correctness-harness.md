# Datadog Translation Correctness Harness (PR1 / #301) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an offline Datadog metric/log/formula → ES|QL structural oracle, translator-path emitter matrix, `infra/datadog` fixture corpus gate, Datadog-aware seed intake, and `docs/testing.md` multi-source layout — so Datadog sibling emit routes cannot regress the same structural failure class Grafana already guards.

**Architecture:** A Datadog-local `esql_structural_oracle` reuses Grafana’s shared STATS/EVAL checker and adds Datadog-only `MISSING_FROM` / empty-feasible rules. An emitter registry covers the four ES|QL translator rules (`metric_single_query`, `metric_formula`, `log_direct_esql`, `log_kql_bridge`). Fixture dashboards and seed intake mirror the Grafana harness. Production `translate_widget` stays degrade-graceful; oracle hard-fails in tests/CI only. PR2 (alerts) and PR3 (broader Grafana) are out of this plan.

**Tech Stack:** Python 3.11+, pytest, existing Datadog normalize/plan/translate modules, Grafana `check_esql_structure`, `scripts/intake_translation_seeds.py`.

**Spec:** `docs/superpowers/specs/2026-07-20-translation-correctness-harness-extension-design.md`  
**Issue:** https://github.com/elastic/observability-migration-platform/issues/301  
**Branch:** `feat/301-translation-harness-datadog`

## Global Constraints

- This plan is **PR1 Datadog dashboard ES|QL only** (no alert oracle, no LogQL/variables, no shared package extraction).
- Oracle hard-fails in **tests/CI only**; do not raise from production `translate_widget`.
- Skip oracle for backends `markdown`, `blocked`, `group`, `image`, `lens`.
- Emitter registry = four translator rules that emit ES|QL text — not every `_build_*_esql` helper.
- Fixture gate: `infra/datadog/dashboards/**/*.json` only (no community corpus in PR CI).
- Do not commit secrets or `live_panel_check_*` artifact dirs.
- Update `docs/testing.md` in the same change set as operator-visible harness behavior.
- `docs/superpowers/` is gitignored — use `git add -f` for specs/plans under that tree.
- Prefer Native Dashboard-as-Code assertions when touching panel shape; this harness asserts ES|QL text + translation status, not YAML schema.

## File structure

| Path | Responsibility |
|---|---|
| `observability_migration/adapters/source/datadog/esql_structural_oracle.py` | Datadog oracle wrapper + `MISSING_FROM` / empty-feasible |
| `observability_migration/adapters/source/datadog/esql_emitters.py` | Emitter path registry + helper symbol map |
| `tests/test_datadog_esql_structural_oracle.py` | Unit tests for Datadog rules + shared-rule passthrough |
| `tests/test_datadog_esql_emitter_matrix.py` | One cell per emitter + registry completeness |
| `tests/test_datadog_fixture_structural_gate.py` | Translate all infra Datadog fixtures; oracle ERROR = fail |
| `scripts/intake_translation_seeds.py` | Datadog `source` + Datadog `rule_hint` oracle |
| `tests/test_translation_seed_intake.py` | Add Datadog mutation self-test |
| `docs/testing.md` | Multi-source harness section (Grafana + Datadog) |

---

### Task 1: Datadog structural oracle

**Files:**
- Create: `observability_migration/adapters/source/datadog/esql_structural_oracle.py`
- Modify: `observability_migration/adapters/source/grafana/esql_structural_oracle.py` (add `MISSING_FROM` to `StructuralRuleId`)
- Create: `tests/test_datadog_esql_structural_oracle.py`

**Interfaces:**
- Consumes: Grafana `check_esql_structure`, `StructuralFinding`, `StructuralRuleId`, `StructuralSeverity`, `structural_errors`
- Produces:
  - `ESQL_EMITTING_BACKENDS: frozenset[str] = frozenset({"esql", "esql_with_kql"})`
  - `def check_datadog_esql_structure(query: str, *, status: str | None = None, backend: str | None = None) -> list[StructuralFinding]`
  - Re-export `structural_errors` for callers
  - `StructuralRuleId.MISSING_FROM` (added on Grafana enum; Datadog wrapper owns FROM check logic)

Behavior:
- If `backend` is set and not in `ESQL_EMITTING_BACKENDS`, return `[]`.
- If `status in {"ok", "warning"}` and stripped query is empty → ERROR `EMPTY_FEASIBLE_QUERY`.
- If query is non-empty: run Grafana `check_esql_structure(query)`, then if no pipeline stage starts with `FROM` → ERROR `MISSING_FROM`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_datadog_esql_structural_oracle.py
from observability_migration.adapters.source.datadog.esql_structural_oracle import (
    check_datadog_esql_structure,
)
from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    StructuralRuleId,
    structural_errors,
)


def test_skips_non_esql_backends():
    assert check_datadog_esql_structure("", status="ok", backend="markdown") == []
    assert check_datadog_esql_structure("", status="ok", backend="lens") == []


def test_empty_ok_status_is_error():
    errs = structural_errors(
        check_datadog_esql_structure("", status="ok", backend="esql")
    )
    assert any(f.rule_id == StructuralRuleId.EMPTY_FEASIBLE_QUERY for f in errs)


def test_missing_from_is_error():
    q = "| STATS value = AVG(system.cpu.user) BY host"
    errs = structural_errors(
        check_datadog_esql_structure(q, status="ok", backend="esql")
    )
    assert any(f.rule_id == StructuralRuleId.MISSING_FROM for f in errs)


def test_clean_from_query_passes():
    q = (
        "FROM metrics-*\n"
        "| WHERE system.cpu.user IS NOT NULL\n"
        "| STATS value = AVG(system.cpu.user) BY host\n"
    )
    assert structural_errors(
        check_datadog_esql_structure(q, status="ok", backend="esql")
    ) == []


def test_shared_eval_undefined_still_errors():
    q = (
        "FROM metrics-*\n"
        "| STATS freq_B = AVG(freq) BY host\n"
        "| EVAL CPU = freq\n"
    )
    errs = structural_errors(
        check_datadog_esql_structure(q, status="ok", backend="esql")
    )
    assert any(f.rule_id == StructuralRuleId.EVAL_UNDEFINED_COLUMN for f in errs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_datadog_esql_structural_oracle.py -v`  
Expected: FAIL (import error / missing `MISSING_FROM` / missing module)

- [ ] **Step 3: Minimal implementation**

1. Add `MISSING_FROM = "MISSING_FROM"` to `StructuralRuleId` in
   `observability_migration/adapters/source/grafana/esql_structural_oracle.py`.
2. Create Datadog module (Elastic-2.0 header required):

```python
# observability_migration/adapters/source/datadog/esql_structural_oracle.py
"""Offline ES|QL structural oracle for Datadog-emitted queries."""

from __future__ import annotations

import re

from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    StructuralFinding,
    StructuralRuleId,
    StructuralSeverity,
    check_esql_structure,
    structural_errors,
)

ESQL_EMITTING_BACKENDS = frozenset({"esql", "esql_with_kql"})


def check_datadog_esql_structure(
    query: str,
    *,
    status: str | None = None,
    backend: str | None = None,
) -> list[StructuralFinding]:
    if backend is not None and backend not in ESQL_EMITTING_BACKENDS:
        return []

    text = (query or "").strip()
    findings: list[StructuralFinding] = []

    if status in {"ok", "warning"} and not text:
        findings.append(
            StructuralFinding(
                rule_id=StructuralRuleId.EMPTY_FEASIBLE_QUERY,
                severity=StructuralSeverity.error,
                message="Feasible Datadog translation produced an empty ES|QL query",
                evidence={"status": status, "backend": backend},
            )
        )
        return findings

    if text:
        findings.extend(check_esql_structure(text))
        stages = _split_pipeline_stages(text)
        has_from = any(stage.upper().startswith("FROM") for stage in stages)
        if not has_from:
            findings.append(
                StructuralFinding(
                    rule_id=StructuralRuleId.MISSING_FROM,
                    severity=StructuralSeverity.error,
                    message="Datadog ES|QL query is missing a FROM stage",
                    evidence={"query": query},
                )
            )

    return findings


def _split_pipeline_stages(query: str) -> list[str]:
    text = query.strip()
    if not text:
        return []
    parts = re.split(r"\n\s*\|\s*", text)
    stages: list[str] = []
    first = parts[0].strip()
    if first:
        stages.append(first)
    stages.extend(part.strip() for part in parts[1:] if part.strip())
    return stages


__all__ = [
    "ESQL_EMITTING_BACKENDS",
    "check_datadog_esql_structure",
    "structural_errors",
    "StructuralFinding",
    "StructuralRuleId",
    "StructuralSeverity",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_datadog_esql_structural_oracle.py tests/test_esql_structural_oracle.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add \
  observability_migration/adapters/source/grafana/esql_structural_oracle.py \
  observability_migration/adapters/source/datadog/esql_structural_oracle.py \
  tests/test_datadog_esql_structural_oracle.py
git commit -m "$(cat <<'EOF'
feat(datadog): add ES|QL structural oracle for harness (#301)

Reuse Grafana STATS/EVAL checks and add Datadog MISSING_FROM / empty-feasible
rules so offline CI catches empty or FROM-less feasible queries.
EOF
)"
```

---

### Task 2: Emitter path matrix

**Files:**
- Create: `observability_migration/adapters/source/datadog/esql_emitters.py`
- Create: `tests/test_datadog_esql_emitter_matrix.py`

**Interfaces:**
- Consumes: `plan_widget`, `translate_widget`, `OTEL_PROFILE`, parsers, models, `check_datadog_esql_structure`
- Produces:
  - `DATADOG_ESQL_EMITTERS: tuple[str, ...]`
  - `EMITTER_HELPER_SYMBOLS: dict[str, str]`
  - `EMITTER_RULE_IDS: dict[str, str]`

- [ ] **Step 1: Write failing registry + matrix tests**

```python
# tests/test_datadog_esql_emitter_matrix.py
from __future__ import annotations

from pathlib import Path

from observability_migration.adapters.source.datadog.esql_emitters import (
    DATADOG_ESQL_EMITTERS,
    EMITTER_HELPER_SYMBOLS,
    EMITTER_RULE_IDS,
)
from observability_migration.adapters.source.datadog.esql_structural_oracle import (
    check_datadog_esql_structure,
    structural_errors,
)
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.log_parser import parse_log_query
from observability_migration.adapters.source.datadog.models import (
    NormalizedWidget,
    WidgetFormula,
    WidgetQuery,
)
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.query_parser import (
    parse_formula,
    parse_metric_query,
)
from observability_migration.adapters.source.datadog.translate import translate_widget

MATRIX_CELLS: tuple[str, ...] = (
    "metric_single_query",
    "metric_formula",
    "log_direct_esql",
    "log_kql_bridge",
)

EMITTER_MATRIX_TESTS: dict[str, str] = {
    "metric_single_query": "test_emitter_metric_single_query",
    "metric_formula": "test_emitter_metric_formula",
    "log_direct_esql": "test_emitter_log_direct_esql",
    "log_kql_bridge": "test_emitter_log_kql_bridge",
}


def test_registry_symbols_exist_in_source():
    text = Path("observability_migration/adapters/source/datadog/translate.py").read_text()
    for emitter_id, symbol in EMITTER_HELPER_SYMBOLS.items():
        assert f"def {symbol}" in text, emitter_id


def test_every_emitter_has_matrix_cell():
    assert set(MATRIX_CELLS) == set(DATADOG_ESQL_EMITTERS)
    assert set(EMITTER_MATRIX_TESTS) == set(MATRIX_CELLS)
    for emitter_id, test_name in EMITTER_MATRIX_TESTS.items():
        assert callable(globals().get(test_name)), emitter_id


def _assert_path_and_oracle(result, emitter_id: str) -> None:
    rule_id = EMITTER_RULE_IDS[emitter_id]
    assert any(entry.get("rule") == rule_id for entry in result.trace), (
        emitter_id,
        result.trace,
    )
    assert result.backend in {"esql", "esql_with_kql"}, result.backend
    assert result.status in {"ok", "warning"}, (result.status, result.warnings)
    errs = structural_errors(
        check_datadog_esql_structure(
            result.esql_query or "",
            status=result.status,
            backend=result.backend,
        )
    )
    assert errs == [], (emitter_id, result.esql_query, errs)


def test_emitter_metric_single_query():
    mq = parse_metric_query("avg:system.cpu.user{*} by {host}")
    wq = WidgetQuery(
        name="query1",
        data_source="metrics",
        raw_query="avg:system.cpu.user{*} by {host}",
        metric_query=mq,
        query_type="metric",
    )
    widget = NormalizedWidget(
        id="1", widget_type="timeseries", title="CPU", queries=[wq]
    )
    result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
    _assert_path_and_oracle(result, "metric_single_query")


def test_emitter_metric_formula():
    mq = parse_metric_query("avg:system.cpu.user{*}")
    wq = WidgetQuery(
        name="query1",
        data_source="metrics",
        raw_query="avg:system.cpu.user{*}",
        metric_query=mq,
        query_type="metric",
    )
    wf = WidgetFormula(raw="query1 * 100")
    wf.expression = parse_formula("query1 * 100")
    widget = NormalizedWidget(
        id="1",
        widget_type="query_value",
        title="CPU %",
        queries=[wq],
        formulas=[wf],
    )
    result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
    _assert_path_and_oracle(result, "metric_formula")


def test_emitter_log_direct_esql():
    lq = parse_log_query("service:web AND status:error")
    wq = WidgetQuery(
        name="query1",
        data_source="logs",
        raw_query="service:web AND status:error",
        log_query=lq,
        query_type="log",
    )
    widget = NormalizedWidget(
        id="1", widget_type="timeseries", title="Errors", queries=[wq]
    )
    result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
    assert result.backend == "esql"
    _assert_path_and_oracle(result, "log_direct_esql")


def test_emitter_log_kql_bridge():
    # Free-text LogTerm forces esql_with_kql via planner._choose_log_backend
    lq = parse_log_query("connection refused")
    wq = WidgetQuery(
        name="query1",
        data_source="logs",
        raw_query="connection refused",
        log_query=lq,
        query_type="log",
    )
    widget = NormalizedWidget(
        id="1", widget_type="list_stream", title="Free text", queries=[wq]
    )
    result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
    assert result.backend == "esql_with_kql"
    _assert_path_and_oracle(result, "log_kql_bridge")
```

If a log `widget_type` does not plan to the expected backend, adjust the fixture to match working cases in `tests/test_datadog_test_plan.py` (do not expand the registry).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_datadog_esql_emitter_matrix.py -v`  
Expected: FAIL (missing `esql_emitters` module)

- [ ] **Step 3: Create registry module**

```python
# observability_migration/adapters/source/datadog/esql_emitters.py
"""Registry of Datadog → ES|QL translator paths for the translation harness."""

from __future__ import annotations

DATADOG_ESQL_EMITTERS: tuple[str, ...] = (
    "metric_single_query",
    "metric_formula",
    "log_direct_esql",
    "log_kql_bridge",
)

EMITTER_HELPER_SYMBOLS: dict[str, str] = {
    "metric_single_query": "metric_single_query_rule",
    "metric_formula": "metric_formula_rule",
    "log_direct_esql": "log_direct_esql_rule",
    "log_kql_bridge": "log_kql_bridge_rule",
}

EMITTER_RULE_IDS: dict[str, str] = {
    "metric_single_query": "datadog.translate.metric_single_query",
    "metric_formula": "datadog.translate.metric_formula",
    "log_direct_esql": "datadog.translate.log_direct_esql",
    "log_kql_bridge": "datadog.translate.log_kql_bridge",
}
```

- [ ] **Step 4: Run matrix tests; fix fixtures if needed**

Run: `.venv/bin/pytest tests/test_datadog_esql_emitter_matrix.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add \
  observability_migration/adapters/source/datadog/esql_emitters.py \
  tests/test_datadog_esql_emitter_matrix.py
git commit -m "$(cat <<'EOF'
feat(datadog): add ES|QL emitter path matrix (#301)

Register the four translator ES|QL routes and assert each is exercised with
an oracle-clean query so path skew cannot hide in CI.
EOF
)"
```

---

### Task 3: Fixture corpus structural gate

**Files:**
- Create: `tests/test_datadog_fixture_structural_gate.py`

**Interfaces:**
- Consumes: `normalize_dashboard`, `plan_widget`, `translate_widget`, `OTEL_PROFILE`, `check_datadog_esql_structure`, `ESQL_EMITTING_BACKENDS`

- [ ] **Step 1: Write gate test**

```python
# tests/test_datadog_fixture_structural_gate.py
from __future__ import annotations

import json
from pathlib import Path

from observability_migration.adapters.source.datadog.esql_structural_oracle import (
    ESQL_EMITTING_BACKENDS,
    check_datadog_esql_structure,
    structural_errors,
)
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "infra" / "datadog" / "dashboards"


def test_all_infra_datadog_fixtures_are_structurally_clean():
    failures: list[str] = []
    paths = sorted(FIXTURE_DIR.rglob("*.json"))
    assert paths, f"no Datadog fixtures under {FIXTURE_DIR}"
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        try:
            nd = normalize_dashboard(raw)
        except Exception as exc:  # pragma: no cover
            failures.append(f"{path}: normalize crashed: {exc}")
            continue
        for widget in nd.widgets:
            try:
                plan = plan_widget(widget)
                result = translate_widget(widget, plan, OTEL_PROFILE)
            except Exception as exc:  # pragma: no cover
                failures.append(
                    f"{path.name}:{widget.title}: translate crashed: {exc}"
                )
                continue
            if result.backend not in ESQL_EMITTING_BACKENDS:
                continue
            if result.status not in {"ok", "warning"}:
                continue
            errs = structural_errors(
                check_datadog_esql_structure(
                    result.esql_query or "",
                    status=result.status,
                    backend=result.backend,
                )
            )
            for err in errs:
                failures.append(
                    f"{path.relative_to(FIXTURE_DIR)} :: {widget.title!r} :: "
                    f"{err.rule_id.value}: {err.message}"
                )
    assert not failures, "structural oracle failures:\n" + "\n".join(failures[:50])
```

- [ ] **Step 2: Run gate**

Run: `.venv/bin/pytest tests/test_datadog_fixture_structural_gate.py -v`  
Expected: PASS on current fixtures. If FAIL, fix real translator bugs in a focused commit — do not weaken `MISSING_FROM` to greenwash.

- [ ] **Step 3: Commit**

```bash
git add tests/test_datadog_fixture_structural_gate.py
git commit -m "$(cat <<'EOF'
test(datadog): add infra fixture structural oracle gate (#301)

Translate every infra Datadog dashboard widget and hard-fail on structural
ES|QL ERROR for feasible ES|QL-emitting backends.
EOF
)"
```

---

### Task 4: Datadog seed intake + mutation self-test

**Files:**
- Modify: `scripts/intake_translation_seeds.py` (`_rule_hint` to dispatch by source)
- Modify: `tests/test_translation_seed_intake.py`

**Interfaces:**
- Consumes: `check_datadog_esql_structure` when `source == "datadog"`
- Produces: seeds with `"source": "datadog"` and correct `rule_hint`

- [ ] **Step 1: Write failing Datadog intake tests**

Append to `tests/test_translation_seed_intake.py`:

```python
_DATADOG_GOOD_QUERY = (
    "FROM metrics-*\n"
    "| STATS freq_B = AVG(system.cpu.user) BY host\n"
    "| EVAL CPU = freq_B\n"
)


def corrupt_datadog_break_eval_alias(query: str) -> str:
    return query.replace("EVAL CPU = freq_B", "EVAL CPU = system.cpu.user")


def test_datadog_oracle_flags_alias_corruption():
    from observability_migration.adapters.source.datadog.esql_structural_oracle import (
        check_datadog_esql_structure,
    )

    bad = corrupt_datadog_break_eval_alias(_DATADOG_GOOD_QUERY)
    errs = structural_errors(
        check_datadog_esql_structure(bad, status="ok", backend="esql")
    )
    assert any(e.rule_id == StructuralRuleId.EVAL_UNDEFINED_COLUMN for e in errs)


def test_datadog_intake_proposes_seed_for_structural_mutation(tmp_path):
    bad_query = corrupt_datadog_break_eval_alias(_DATADOG_GOOD_QUERY)
    report = {
        "source": "datadog",
        "panels": [
            {
                "title": "Datadog CPU Alias Bug",
                "status": "fail",
                "disposition": "real_bug",
                "error": "Unknown column [system.cpu.user]",
                "esql_query": bad_query,
                "targets": [{"query": "avg:system.cpu.user{*}"}],
            }
        ],
    }
    report_path = tmp_path / "dd_smoke.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--report",
            str(report_path),
            "--out-dir",
            str(tmp_path / "seeds"),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    proposals = json.loads(proc.stdout)
    assert len(proposals) == 1
    seed = proposals[0]
    assert seed["source"] == "datadog"
    assert seed["panel_title"] == "Datadog CPU Alias Bug"
    assert seed["disposition"] == "real_bug"
    assert seed["esql_query"] == bad_query
    assert seed["rule_hint"] == StructuralRuleId.EVAL_UNDEFINED_COLUMN.value
```

- [ ] **Step 2: Run new tests**

Run: `.venv/bin/pytest tests/test_translation_seed_intake.py::test_datadog_intake_proposes_seed_for_structural_mutation tests/test_translation_seed_intake.py::test_datadog_oracle_flags_alias_corruption -v`

- [ ] **Step 3: Update intake script**

Replace `_rule_hint` in `scripts/intake_translation_seeds.py`:

```python
def _rule_hint(esql_query: str, *, source: str = "grafana") -> str:
    if source == "datadog":
        from observability_migration.adapters.source.datadog.esql_structural_oracle import (
            check_datadog_esql_structure,
            structural_errors as dd_structural_errors,
        )

        errs = dd_structural_errors(
            check_datadog_esql_structure(esql_query or "", status="ok", backend="esql")
        )
    else:
        errs = structural_errors(check_esql_structure(esql_query or ""))
    return errs[0].rule_id.value if errs else ""
```

Update `propose_seed` to call `_rule_hint(esql_query, source=source)`.

- [ ] **Step 4: Run full intake suite**

Run: `.venv/bin/pytest tests/test_translation_seed_intake.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/intake_translation_seeds.py tests/test_translation_seed_intake.py
git commit -m "$(cat <<'EOF'
feat(harness): support Datadog source in translation seed intake (#301)

Route rule hints through the Datadog structural oracle and add a mutation
self-test so non-Grafana live failures can become committed seeds.
EOF
)"
```

---

### Task 5: Document multi-source harness in `docs/testing.md`

**Files:**
- Modify: `docs/testing.md`
- Optional: `git add -f` update to Grafana design deferred pointer citing the extension design

- [ ] **Step 1: Update `docs/testing.md`**

Under Tier 3, keep the Grafana harness table and add a **Datadog ES|QL structural harness (offline)** subsection:

| Piece | Module / test | What it proves |
|---|---|---|
| Structural oracle | `observability_migration/adapters/source/datadog/esql_structural_oracle.py` | Shared STATS/EVAL + `MISSING_FROM` / empty feasible; skips non-ES\|QL backends |
| Emitter path matrix | `.../datadog/esql_emitters.py`, `tests/test_datadog_esql_emitter_matrix.py` | Four translator routes oracle-clean |
| Fixture corpus gate | `tests/test_datadog_fixture_structural_gate.py` | `infra/datadog/dashboards/**/*.json` |
| Seed intake | `scripts/intake_translation_seeds.py` with `source: datadog` | Non-Grafana regression seeds |

Note follow-ons (alerts, broader Grafana, shared package) → issue #301 and
`docs/superpowers/specs/2026-07-20-translation-correctness-harness-extension-design.md`.

In the seed-intake example, mention `"source": "datadog"` reports are accepted.

- [ ] **Step 2: Spot-check that documented paths exist**

- [ ] **Step 3: Commit**

```bash
git add docs/testing.md
git commit -m "$(cat <<'EOF'
docs: document Datadog translation correctness harness (#301)

Extend the Tier-3 testing narrative so Grafana-only coverage is not mistaken
for full multi-source translation correctness.
EOF
)"
```

---

## Spec coverage checklist (self-review)

| Spec requirement | Task |
|---|---|
| Datadog structural oracle (shared + `MISSING_FROM` / empty) | Task 1 |
| Emitter matrix (4 translator routes) | Task 2 |
| `infra/datadog` fixture gate | Task 3 |
| Seed intake for Datadog | Task 4 |
| `docs/testing.md` multi-source layout | Task 5 |
| PR2 alerts / PR3 broader Grafana / shared package | Explicitly **out of plan** (roadmap only) |

## Placeholder / consistency scan

- No TBD/TODO steps.
- `StructuralRuleId.MISSING_FROM` added in Task 1; Datadog statuses use `ok`/`warning`.
- Emitter ids match translator rule function names and `datadog.translate.*` ids.

---

## Out of scope (do not implement in this plan)

- Alert rule offline gate (PR2)
- LogQL / variables / native PromQL smoke (PR3)
- Extract shared `translation_oracle` package
- Community corpus structural gate in PR CI
- Production fail-closed oracle inside `translate_widget`
