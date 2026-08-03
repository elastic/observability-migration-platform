---
name: prepare-production-cutover
description: Use when the user asks whether an obs-migrate Grafana/Datadog migration is ready for production cutover, wants a final go/no-go, needs a board/customer-ready cutover checklist, or asks what must be validated before switching users from the source observability stack to Kibana.
---

# Prepare production cutover

**Audience:** operators of the published `obs-migrate` CLI (PyPI/`uvx`), using public docs and their real source + Elastic/Kibana — not a repo lab harness.

Goal: turn migration artifacts and existing validation skills into a **go/no-go cutover decision**. This is the final gate before users stop relying on the source Grafana/Datadog dashboards or alerts. Do not rerun migration just to look decisive; read the artifacts, validate the risky paths, and keep a rollback plan visible.

## Prerequisites (install)

These skills help **operators** of the published CLI (not a repo checkout).
If `obs-migrate` is missing or `doctor` is not **Ready**, follow
`install-obs-migrate` first — that skill owns PyPI/`uvx`/pip, extras, and
Python/`uv` gotchas. Do not invent alternate install commands here.

```bash
uvx --from 'elastic-observability-migration[all]' obs-migrate doctor
# After a persistent install, the same check is: obs-migrate doctor
```

Source/Elastic credentials: `connect-to-o11y-source` (and your env exports).


## Required inputs

| What you need | Where to get it |
|---|---|
| Asset scope | `<output-dir>/run_summary.json` (`ran.dashboards`, `ran.alerts`) |
| Dashboard coverage | `report-migration-coverage` over `<output-dir>/dashboards/migration_summary.md` and `migration_manifest.json` |
| Native review artifacts | `<output-dir>/dashboards/native/*.native.json` (typed upload payload; `dashboard_id` for deep links / revert); `<output-dir>/dashboards/ir/*.ir.json` (translator decisions, inspection-only) |
| Numeric/structural parity | `validate-side-by-side` / `obs-migrate compare` over comparison report |
| Live ES\|QL / runtime emptiness (operator) | `obs-migrate verify` (optional `--compare`) and/or `grafana-validate-uploaded` on critical uploaded dashboards |
| UI render truth (optional deeper) | Browser check in Kibana view mode; full render-audit / `verifier.*` gates in `https://github.com/elastic/observability-migration-platform/blob/main/docs/testing.md` are lab/repo extras — not required to issue a go/no-go for most operators |
| Gap explanations | `explain-migration-gaps` for warned / manual / not_feasible / compare `FAIL` / `SKIP` / unexpected `STRUCTURAL` or `SOURCE_DRIFT` |
| Alert rule safety | `review-and-enable-migrated-alerts` over `<output-dir>/alerts/*_comparison_results.json` and rule-upload results; live `obs-migrate audit-rules` |
| Back-out path | `revert-migration` for dashboard ids and migrated alert rules |

## Cutover sequence

1. **State scope first** — read `run_summary.json`. If `ran.alerts: false`, do not claim alert cutover readiness. If dashboards-only was requested, make the cutover decision dashboards-only.
2. **Get the coverage headline** — use `report-migration-coverage`. Record clean %, warned, needs-review, blocked, Datadog `skipped` (if any), and manual-effort buckets. Exit code alone is not evidence.
3. **Review native artifacts** — spot-check `dashboards/native/*.native.json` for critical dashboards before trusting upload.
4. **Validate critical dashboards** — run or read `validate-side-by-side` / `obs-migrate compare`. Numeric proof applies only where the native PROMQL oracle (or Datadog `SOURCE_*` live packets) applies; `STRUCTURAL`, `SKIP`, `ERROR`, and bare `SOURCE_DRIFT` are not cutover proof by themselves.
5. **Prove panels execute (and spot-check UI)** — run `obs-migrate verify` and/or `grafana-validate-uploaded` on the critical uploaded set, then open those dashboards in Kibana view mode. ES|QL success alone does not prove Lens chrome is perfect; treat empty UI with successful `/_query` as a separate UI issue.
6. **Classify every gap** — use `explain-migration-gaps`. Accepted approximations (warned panels that still render) can be `GO WITH CONDITIONS` if owners accept them; hard `not_feasible` on a critical path is `NO-GO`.
7. **Confirm data/field readiness** — if panels are empty or queries hit missing fields, use `remediate-field-mapping-gaps` / `prepare-target-telemetry` before cutover. Do not label a schema mismatch as a product success.
8. **Review alert rules before enabling** — use `review-and-enable-migrated-alerts`. Migrated rules are created disabled; enabling is a separate human gate. Confirm with `audit-rules` that migrated rules are still disabled before go-live.
9. **Write the rollback plan** — identify `dashboard_id`s to remove, migrated-rule markers (`obs-migration` / `[migrated] ...`), and who can execute `revert-migration`. On Serverless, expect `[DELETED]` placeholders after dashboard clear.
10. **Issue the go/no-go** — one of: `GO`, `GO WITH CONDITIONS`, or `NO-GO`. Include the evidence that justifies it.

## Go / no-go rules

- **GO** only when dashboard coverage, parity, **and render/runtime truth** meet the user's stated bar, alert-rule review is complete for any alert cutover, and rollback steps are known.
- **GO WITH CONDITIONS** when remaining gaps are documented, accepted by owners (including accepted approximations), and not on critical paths.
- **NO-GO** when critical panels are hard `not_feasible`, unresolved parity `FAIL` / `SOURCE_FAIL`, unresolved `render_error` on critical panels, alert rules have not been reviewed, required fields are missing, or rollback ownership is unclear. Do **not** NO-GO solely because a non-critical panel has an accepted approximation warning.

## Cutover readout template

```text
Cutover decision: GO WITH CONDITIONS
Scope: dashboards=true, alerts=false from <output-dir>/run_summary.json
Coverage: <clean>/<total> clean; <warned> warned (accepted?); <needs-review> need review; <blocked> blocked
Validation: compare=<...>; verify/grafana-validate-uploaded=<...>; render_audit=<pass/fail/not run>
Open gaps: <accepted approximations / manual / no-go items>
Rollback: dashboards by id via revert-migration (Serverless [DELETED] placeholders); migrated rules via delete-rules dry run + confirm
```

## Honest limits / Do NOT

- **Do NOT say "production ready" from coverage alone.** Coverage reports migration outcome; cutover also needs validation, render/runtime checks, alert review, data/schema readiness, and rollback.
- **Do NOT hide structural-only validation.** Structural rows are useful evidence, not numeric proof.
- **Do NOT treat every warned panel as a blocker** — triage with `explain-migration-gaps`.
- **Do NOT claim alert readiness if `run_summary.json` says alerts did not run** or if migrated rules have not been reviewed/enabled deliberately.
- **Do NOT skip rollback planning.** A cutover without a target-side back-out path is not ready.
- **Do NOT run destructive revert commands without explicit user approval.**

## See also

- `install-obs-migrate` — install/doctor when the CLI is missing or not Ready.
- `report-migration-coverage` — coverage headline and manual-effort buckets.
- `validate-side-by-side` — numeric/structural dashboard parity.
- `explain-migration-gaps` — why panels warned, failed, or need manual rebuild.
- `remediate-field-mapping-gaps` — fix missing fields / empty panels before cutover.
- `prepare-target-telemetry` — field profile / seed before claiming empty panels are bugs.
- `review-and-enable-migrated-alerts` — alert-rule review and enablement.
- `revert-migration` — target-side rollback.
- `https://github.com/elastic/observability-migration-platform/blob/main/docs/testing.md` — layered verifier + render-audit gates.
- `https://github.com/elastic/observability-migration-platform/blob/main/docs/command-contract.md` — verify / upload / compare commands.
