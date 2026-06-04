---
name: review-and-enable-migrated-alerts
description: Use when obs-migrate created Kibana alerting rules and the user asks whether they can enable them, verify them, review connectors/actions, audit migrated rules, or safely roll alert rules into production.
---

# Review and enable migrated alerts

Goal: keep migrated alert rules safe. `obs-migrate` creates emitted Kibana rules **disabled** and tagged `obs-migration`; enabling them is a deliberate production decision after query, threshold, connector, and rollback review.

## Inputs

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout.

| What you need | File / command |
|---|---|
| Alert comparison payloads | Grafana: `<output-dir>/alerts/alert_comparison_results.json`; Datadog: `<output-dir>/alerts/monitor_comparison_results.json` |
| Rule creation results | Grafana: `<output-dir>/alerts/alert_rule_upload_results.json`; Datadog: `<output-dir>/alerts/monitor_rule_upload_results.json` |
| Which assets ran | `<output-dir>/run_summary.json` (`ran.alerts`) |
| Self-cleaning write proof | `obs-migrate verify-alert-rules --comparison <...>` |
| Read-only rule audit | `obs-migrate audit-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"` |
| Disable migrated rules if needed | `obs-migrate audit-rules ... --disable-enabled` |
| Delete migrated rules if backing out | `obs-migrate delete-rules` dry run, then `--confirm` after user approval |

## Review sequence

1. **Confirm alerts were in scope** — read `run_summary.json`. If `ran.alerts: false`, stop; there are no migrated alert rules to enable from this run.
2. **Read comparison results first** — open `alert_comparison_results.json` or `monitor_comparison_results.json`. Identify rules with semantic losses, unsupported constructs, missing queries, or manual notes.
3. **Read upload results** — open `alert_rule_upload_results.json` or `monitor_rule_upload_results.json`. Separate created, failed, and skipped rules. Do not enable a rule that failed or was skipped.
4. **Run a self-cleaning verification when possible**:

   ```bash
   obs-migrate verify-alert-rules \
     --comparison <output-dir>/alerts/alert_comparison_results.json \
     --kibana-url "$KIBANA_ENDPOINT" \
     --kibana-api-key "$KEY"
   ```

   Use the Datadog `monitor_comparison_results.json` path for Datadog. This creates rules disabled, checks they did not come back enabled, then deletes them unless `--keep-rules`.
5. **Audit persisted migrated rules**:

   ```bash
   obs-migrate audit-rules \
     --kibana-url "$KIBANA_ENDPOINT" \
     --kibana-api-key "$KEY"
   ```

   `audit-rules` is read-only unless `--disable-enabled` is passed. It lists rules tagged `obs-migration` or named `[migrated] ...` and reports enabled state.
6. **Review connectors/actions manually** — confirm each rule's connector exists, credentials work, destination is production-correct, escalation policy is accepted, and message templates still make sense in Kibana. The migration can create rule shells; connector/action parity is not automatically proven unless the artifacts and Kibana review show it.
7. **Canary before bulk enablement** — enable one low-risk rule first, watch execution history for several cycles, then enable by tier/owner. Keep source alerts running during overlap.

## Enablement decision

- **READY TO ENABLE** — comparison clean enough for owner, upload succeeded, `verify-alert-rules` passed or existing rules audit clean, connectors/actions reviewed, rollback path known.
- **ENABLE WITH CONDITIONS** — owner accepts semantic losses or muted/no-action canary period.
- **DO NOT ENABLE** — rule failed/skipped creation, comparison has unresolved semantic gaps, connector routing unknown, target data/field mapping is unresolved, or rollback owner is missing.

## Rollback / safety

- If migrated rules are unexpectedly enabled, disable them with:

  ```bash
  obs-migrate audit-rules \
    --kibana-url "$KIBANA_ENDPOINT" \
    --kibana-api-key "$KEY" \
    --disable-enabled
  ```

- To remove migrated rules, dry-run first:

  ```bash
  obs-migrate delete-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
  obs-migrate delete-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --confirm
  ```

## Honest limits / Do NOT enable

- **Do NOT enable migrated alert rules solely because they were created.** Creation proves the payload was accepted, not that production notifications are safe.
- **Do NOT claim connectors/actions are migrated perfectly without inspecting the rule and destination.** Notification semantics may need manual review.
- **Do NOT treat `verify-alert-rules` as a persistent enablement step.** It is self-cleaning unless `--keep-rules`; it proves create/disabled/cleanup behavior.
- **Do NOT run `delete-rules --confirm` without explicit user approval.** Dry run first.
- **Do NOT disable rules with `audit-rules --disable-enabled` unless the user wants a mutating safety action.**

## See also

- `evaluate-o11y-permissions` — prove the Kibana key can read/create alert rules.
- `migrate-all-supported-assets` / `migrate-selected-assets` — create rules disabled with `--create-alert-rules`.
- `prepare-production-cutover` — include alert-rule readiness in the final go/no-go.
- `revert-migration` — target-side rollback for migrated rules.
- `obs-migrate verify-alert-rules --help`, `obs-migrate audit-rules --help`, `obs-migrate delete-rules --help` — authoritative installed-package flags.
