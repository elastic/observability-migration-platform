---
name: report-migration-coverage
description: Produce a shareable migration coverage report — counts, % migrated cleanly, what needs review or is blocked, plus a rough manual-effort estimate — from the artifacts a completed obs-migrate run already wrote. Use when the user asks for "a migration report", "coverage summary", "how much migrated", "what's left", or "a summary I can send my manager". Read-only: reads existing artifacts; does not re-run a migration or touch any cluster. For per-panel "why didn't this migrate / how do I rebuild it" use explain-migration-gaps; to prove panels are numerically correct use validate-side-by-side.
---

# Report migration coverage

Goal: compose a **shareable coverage summary** from artifacts a completed `obs-migrate migrate` run already wrote on disk — read-only; do not re-run migration or touch any cluster.

## Inputs (artifact table)

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. Every artifact below is written by a normal migrate run — no source checkout required.

| What you want | File | Field(s) |
|---|---|---|
| Verdict + scorecard | `<output-dir>/dashboards/migration_summary.md` | verdict, scorecard, per-dashboard table, must-fix worklist |
| Counts | `<output-dir>/dashboards/migration_manifest.json` | clean: `summary.migrated` (Grafana) / `summary.ok` (Datadog); warnings: `summary.migrated_with_warnings` (Grafana) / `summary.warning` (Datadog); shared: `summary.requires_manual`, `summary.not_feasible`; Datadog-only: `summary.blocked` |
| Which asset families ran | `<output-dir>/run_summary.json` | `ran.dashboards`, `ran.alerts`, `dashboards.total` |
| Optional field-mapping appendix | command `obs-migrate schema-report --artifact-dir <output-dir>/dashboards --output schema_change_report.md` | per-panel source→target field table |

## Workflow

1. **Locate the output dir** — the `--output-dir` from the user's migrate run (or ask which run they mean if several exist).
2. **Read the headline verdict** — open `<output-dir>/dashboards/migration_summary.md` for the human-readable scorecard and must-fix worklist.
3. **Read the counts** — open `<output-dir>/dashboards/migration_manifest.json` and read the `summary` object.
4. **Compute coverage** — from `summary` (clean/warning key names differ by source — pick the right pair for the run):
   - **Grafana:** clean = `summary.migrated`, warnings = `summary.migrated_with_warnings`.
   - **Datadog:** clean = `summary.ok`, warnings = `summary.warning`.
   - **Both:** `summary.requires_manual`, `summary.not_feasible`; Datadog also `summary.blocked`.
   - **Coverage %** = clean count / `summary.panels`.
   - **Needs review** = warnings count + `summary.requires_manual`.
   - **Blocked** = `summary.not_feasible` (+ `summary.blocked` on Datadog).
5. **Check scope** — read `<output-dir>/run_summary.json` to see whether dashboards and/or alerts actually ran (`ran.dashboards`, `ran.alerts`) and how many dashboards were in scope (`dashboards.total`).
6. **Optional appendix** — if the audience cares about field/schema gaps, run `obs-migrate schema-report --artifact-dir <output-dir>/dashboards --output schema_change_report.md` and attach or link the table.
7. **Assemble the shareable summary** — one short narrative (verdict + coverage % + needs-review + blocked counts + alert scope note) backed by the artifact paths above.

## Manual-effort estimate

Give a **rough bucket estimate**, not false precision:

- **`requires_manual`** — moderate rebuild each (partial translation; human must finish or replace the panel).
- **`not_feasible`** (+ **`blocked`** on Datadog) — full manual rebuild each (engine could not produce a usable panel).

State the counts from `summary` and multiply by the bucket above (e.g. "12 requires_manual ≈ moderate rework each; 4 not_feasible ≈ full rebuild each"). Avoid implying exact person-hours unless the user supplies their own velocity.

## Degrade gracefully / Honest limits

- **exit 0 does not mean every panel is perfect; warnings and blocked panels are listed, never hidden.** Trust `migration_summary.md` and `migration_manifest.json`, not the exit code alone.
- **Alerts coverage depends on what ran.** If the migrate command used `--assets dashboards` only, `run_summary.json` shows `ran.alerts: false` — do not claim alert migration coverage without reading that file.
- **Datadog and Grafana use different clean/warning keys in `summary`.** Grafana writes `summary.migrated` and `summary.migrated_with_warnings`; Datadog writes `summary.ok` and `summary.warning` (not `migrated` / `migrated_with_warnings`) and adds `summary.blocked`. `summary.requires_manual` and `summary.not_feasible` are shared — read the JSON for the source you are reporting on.
- **This skill does not prove panels render correctly** — empty uploaded panels may be missing telemetry, not a translation bug. Numerical proof is `validate-side-by-side`; per-panel gap explanations are `explain-migration-gaps`.

## See also

- `explain-migration-gaps` skill — turn each panel `reason` into manual rebuild guidance.
- `validate-side-by-side` skill — prove migrated panels match source numerically.
- `docs/command-contract.md` — artifact paths and migrate flags for the installed version.
