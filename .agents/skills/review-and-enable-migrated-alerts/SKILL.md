---
name: review-and-enable-migrated-alerts
description: Use when obs-migrate created Kibana alerting rules and the user asks whether they can enable them, verify them, review connectors/actions, audit migrated rules, or safely roll alert rules into production.
---

# Review and enable migrated alerts

**Audience:** operators of the published `obs-migrate` CLI (PyPI/`uvx`), using public docs and their real source + Elastic/Kibana — not a repo lab harness.

Goal: keep migrated alert rules safe. `obs-migrate` creates emitted Kibana rules **disabled** and tagged `obs-migration`; enabling them is a deliberate production decision after query, threshold, connector, and rollback review.

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


## Inputs

| What you need | File / command |
|---|---|
| Alert comparison payloads | Grafana: `<output-dir>/alerts/alert_comparison_results.json`; Datadog: `<output-dir>/alerts/monitor_comparison_results.json` |
| Alert translation results (always for alert runs) | Datadog: `<output-dir>/alerts/monitor_migration_results.json` (tiers/kinds even when nothing was uploaded) |
| Rule creation results (only with `--create-alert-rules`) | Grafana: `<output-dir>/alerts/alert_rule_upload_results.json`; Datadog: `<output-dir>/alerts/monitor_rule_upload_results.json` |
| Which assets ran | `<output-dir>/run_summary.json` (`ran.alerts`, `alerts.total`, `alerts.by_automation_tier`) |
| Self-cleaning write proof | `obs-migrate verify-alert-rules --comparison <...>` (needs **emitted** rule payloads — see below) |
| Read-only rule audit | `obs-migrate audit-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"` |
| Disable migrated rules if needed | `obs-migrate audit-rules ... --disable-enabled` |
| Delete migrated rules if backing out | `obs-migrate delete-rules` dry run, then `--confirm` after user approval |

## Review sequence

1. **Confirm alerts were in scope** — read `run_summary.json`. If `ran.alerts: false`, stop; there are no migrated alert rules to enable from this run.
2. **Read comparison / migration results first** — open `alert_comparison_results.json` or `monitor_comparison_results.json` (and Datadog `monitor_migration_results.json`). Identify rules with semantic losses, unsupported constructs, `manual_required` / non-emitted tiers, or missing queries. **Do not enable** monitors that never produced an emitted Kibana rule payload.
3. **Read upload results when present** — open `alert_rule_upload_results.json` or `monitor_rule_upload_results.json` (only written when the migrate used `--create-alert-rules`). Separate created, failed, and skipped rules. Do not enable a rule that failed or was skipped. If upload results are missing, fall back to live `audit-rules` for what already exists in Kibana.
4. **Run a self-cleaning verification when payloads exist**:

   ```bash
   obs-migrate verify-alert-rules \
     --comparison <output-dir>/alerts/monitor_comparison_results.json \
     --kibana-url "$KIBANA_ENDPOINT" \
     --kibana-api-key "$KEY"
   ```

   Use Grafana `alert_comparison_results.json` for Grafana. This creates rules disabled, checks they did not come back enabled, then deletes them unless `--keep-rules`.

   If the command prints `{"error": "no_emitted_rule_payloads"}`, the comparison file has **nothing creatable** (common when Datadog monitors are all `manual_required`). That is not a cluster failure — treat as DO NOT ENABLE / rebuild those monitors, not as a verify pass.
5. **Audit persisted migrated rules**:

   ```bash
   obs-migrate audit-rules \
     --kibana-url "$KIBANA_ENDPOINT" \
     --kibana-api-key "$KEY"
   ```

   `audit-rules` is read-only unless `--disable-enabled` is passed. JSON includes `migrated_rules_seen`, `enabled_migrated_rule_ids`, `disabled_migrated_rule_ids`. Exit is non-zero while enabled migrated rules remain (or remediation fails).
6. **Review connectors/actions manually** — confirm each rule's connector exists, credentials work, destination is production-correct, escalation policy is accepted, and message templates still make sense in Kibana. The migration can create rule shells; connector/action parity is not automatically proven unless the artifacts and Kibana review show it.
7. **Canary before bulk enablement** — enable one low-risk rule first (in Kibana UI), watch execution history for several cycles, then enable by tier/owner. Keep source alerts running during overlap.

> **Time field (fallback only).** Migrated `.es-query` rules always carry `params.timeField: "@timestamp"` in the created rule — confirm with `GET /api/alerting/rule/{id}`. That persisted value is what Kibana uses to bound each evaluation to the lookback window. The rule wizard's **Select a time field** step only *displays* `@timestamp` once Kibana can resolve the rule's target index/data view; if that index is missing or empty (e.g. `Unknown index "metrics-..."`, **Test query** disabled), the wizard may show the field as unset even though the persisted value is correct — in that state, fix the target data rather than re-saving from the wizard. Only set the field manually for rules created **before** migrations included it.

## Enablement decision

- **READY TO ENABLE** — comparison clean enough for owner, upload succeeded (or audit shows disabled migrated rules), `verify-alert-rules` passed **or** was N/A because only non-emitted monitors remain and those are explicitly excluded, connectors/actions reviewed, rollback path known.
- **ENABLE WITH CONDITIONS** — owner accepts semantic losses or muted/no-action canary period.
- **DO NOT ENABLE** — `no_emitted_rule_payloads` / all `manual_required`, rule failed/skipped creation, comparison has unresolved semantic gaps, connector routing unknown, target data/field mapping is unresolved, or rollback owner is missing.

## Rollback / safety

- If migrated rules are unexpectedly enabled, disable them with:

  ```bash
  obs-migrate audit-rules \
    --kibana-url "$KIBANA_ENDPOINT" \
    --kibana-api-key "$KEY" \
    --disable-enabled
  ```

- To remove migrated rules, dry-run first (`would_delete_count` / `would_delete_rule_ids`):

  ```bash
  obs-migrate delete-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
  obs-migrate delete-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --confirm
  ```

## Honest limits / Do NOT enable

- **Do NOT enable migrated alert rules solely because they were created.** Creation proves the payload was accepted, not that production notifications are safe.
- **Do NOT claim connectors/actions are migrated perfectly without inspecting the rule and destination.** Notification semantics may need manual review.
- **Do NOT treat `verify-alert-rules` as a persistent enablement step.** It is self-cleaning unless `--keep-rules`; it proves create/disabled/cleanup behavior.
- **Do NOT treat `no_emitted_rule_payloads` as a green verify.** It means there was nothing to create.
- **Do NOT run `delete-rules --confirm` without explicit user approval.** Dry run first.
- **Do NOT disable rules with `audit-rules --disable-enabled` unless the user wants a mutating safety action.**

## See also

- `install-obs-migrate` — install/doctor when the CLI is missing or not Ready.
- `evaluate-o11y-permissions` — prove the Kibana key can read/create alert rules.
- `migrate-all-supported-assets` / `migrate-selected-assets` — create rules disabled with `--create-alert-rules`.
- `prepare-production-cutover` — include alert-rule readiness in the final go/no-go.
- `revert-migration` — target-side rollback for migrated rules.
- `obs-migrate verify-alert-rules --help`, `obs-migrate audit-rules --help`, `obs-migrate delete-rules --help` — authoritative installed-package flags.
