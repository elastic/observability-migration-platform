---
name: prepare-production-cutover
description: Use when the user asks whether an obs-migrate Grafana/Datadog migration is ready for production cutover, wants a final go/no-go, needs a board/customer-ready cutover checklist, or asks what must be validated before switching users from the source observability stack to Kibana.
---

# Prepare production cutover

Goal: turn migration artifacts and existing validation skills into a **go/no-go cutover decision**. This is the final gate before users stop relying on the source Grafana/Datadog dashboards or alerts. Do not rerun migration just to look decisive; read the artifacts, validate the risky paths, and keep a rollback plan visible.

## Required inputs

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout.

| What you need | Where to get it |
|---|---|
| Asset scope | `<output-dir>/run_summary.json` (`ran.dashboards`, `ran.alerts`) |
| Dashboard coverage | `report-migration-coverage` over `<output-dir>/dashboards/migration_summary.md` and `migration_manifest.json` |
| Numeric/structural parity | `validate-side-by-side` over `<output-dir>/dashboards/comparison_report.json` |
| Gap explanations | `explain-migration-gaps` for `requires_manual`, `not_feasible`, `FAIL`, `SKIP`, or unexpected `STRUCTURAL` panels |
| Alert rule safety | `review-and-enable-migrated-alerts` over `<output-dir>/alerts/*_comparison_results.json` and rule-upload results |
| Back-out path | `revert-migration` for dashboard ids and migrated alert rules |

## Cutover sequence

1. **State scope first** — read `run_summary.json`. If `ran.alerts: false`, do not claim alert cutover readiness. If dashboards-only was requested, make the cutover decision dashboards-only.
2. **Get the coverage headline** — use `report-migration-coverage`. Record clean %, needs-review count, blocked count, and manual-effort buckets. Exit code alone is not evidence.
3. **Validate critical dashboards** — run or read `validate-side-by-side`. Numeric proof applies only where the native PROMQL oracle applies; `STRUCTURAL`, `SKIP`, and `ERROR` are not numeric proof.
4. **Classify every gap** — use `explain-migration-gaps` for non-clean panels and parity failures. A cutover can proceed only if the owner accepts each unresolved gap.
5. **Confirm data/field readiness** — if panels are empty or queries hit missing fields, use `remediate-field-mapping-gaps` before cutover. Do not label a schema mismatch as a product success.
6. **Review alert rules before enabling** — use `review-and-enable-migrated-alerts`. Migrated rules are created disabled; enabling is a separate human gate.
7. **Write the rollback plan** — identify dashboard ids to remove, migrated-rule markers (`obs-migration` / `[migrated] ...`), and who can execute `revert-migration`.
8. **Issue the go/no-go** — one of: `GO`, `GO WITH CONDITIONS`, or `NO-GO`. Include the evidence that justifies it.

## Go / no-go rules

- **GO** only when dashboard coverage and parity meet the user's stated bar, alert-rule review is complete for any alert cutover, and rollback steps are known.
- **GO WITH CONDITIONS** when remaining gaps are documented, accepted by owners, and not on critical paths.
- **NO-GO** when critical panels are `not_feasible`, parity `FAIL` is unresolved, alert rules have not been reviewed, required fields are missing, or rollback ownership is unclear.

## Cutover readout template

Use a short, auditable readout:

```text
Cutover decision: GO WITH CONDITIONS
Scope: dashboards=true, alerts=false from <output-dir>/run_summary.json
Coverage: <clean>/<total> clean; <needs-review> need review; <blocked> blocked
Validation: <strict/fuzzy/structural/fail/skip summary>; numeric proof only where native oracle applied
Open gaps: <accepted/manual/no-go items>
Rollback: dashboards by id via revert-migration; migrated rules by obs-migration marker via delete-rules dry run + confirm
```

## Honest limits / Do NOT

- **Do NOT say "production ready" from coverage alone.** Coverage reports migration outcome; cutover also needs validation, alert review, data/schema readiness, and rollback.
- **Do NOT hide structural-only validation.** Structural rows are useful evidence, not numeric proof.
- **Do NOT claim alert readiness if `run_summary.json` says alerts did not run** or if migrated rules have not been reviewed/enabled deliberately.
- **Do NOT skip rollback planning.** A cutover without a target-side back-out path is not ready.
- **Do NOT run destructive revert commands without explicit user approval.**

## See also

- `report-migration-coverage` — coverage headline and manual-effort buckets.
- `validate-side-by-side` — numeric/structural dashboard parity.
- `explain-migration-gaps` — why panels failed or need manual rebuild.
- `remediate-field-mapping-gaps` — fix missing fields / empty panels before cutover.
- `review-and-enable-migrated-alerts` — alert-rule review and enablement.
- `revert-migration` — target-side rollback.
