---
name: revert-migration
description: Use when the user wants to "undo the migration", "delete the dashboards I just uploaded", "remove the migrated alert rules", "roll back", "clean up Kibana", or "start over" — removes some or all of the Kibana assets a migration created (uploaded dashboards and/or migrated alerting rules). Operates only on the TARGET (Kibana); it never deletes anything from the source Grafana/Datadog.
---

# Revert a migration (remove generated Kibana assets)

Goal: cleanly remove the assets a migration put **into Kibana** — dashboards, alerting rules, or both — so the user can back out or redo. This is a **target-only, destructive** operation; nothing is touched in the source Grafana/Datadog.

## Which command form to use (package vs. repo)

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. Every command below ships in the installed wheel — no `scripts/`, `infra/`, or `examples/` directory is required.

## Safety first

- This deletes target assets. **Confirm with the user** what to remove (which dashboards, alerts, or everything) before running a destructive step.
- Dashboard deletion has no dry-run or `--confirm`: list dashboards first, confirm the exact IDs with the user, then run `delete-dashboards`. Alert-rule deletion is safer by default because `delete-rules` is dry-run unless `--confirm`.
- Reverting does **not** touch the source. The original Grafana/Datadog dashboards and monitors are unaffected.

## Reverting dashboards

There is no "delete by migration tag" for dashboards — you delete **by id**. Find the ids first, then delete the ones you want.

```bash
export KIBANA_ENDPOINT="https://...kbn..." KEY="<api-key>"

# 1. List dashboards in Kibana to find the ids to remove (read-only):
obs-migrate cluster list-dashboards \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# 2. Delete the chosen dashboards by id:
obs-migrate cluster delete-dashboards \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --dashboard-ids id1,id2,id3
```

- `delete-dashboards` **requires** `--dashboard-ids`; there is no "delete all" switch. To remove everything, list first and pass the full id set.
- **Placeholder caveat:** `delete-dashboards` **clears** each dashboard into a `[DELETED]` placeholder rather than removing the saved object outright. The command prints a note saying so — relay it; the user may still see `[DELETED]` shells in the saved-objects list.

## Reverting alerting rules

Migrated rules are tagged `obs-migration` (or named `[migrated] ...`). `obs-migrate delete-rules` finds them by that marker and removes them — **dry-run by default**, so you can preview the exact set before deleting.

```bash
# 1. Dry run: show which migrated rules WOULD be deleted (no changes):
obs-migrate delete-rules \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# 2. Confirm: actually delete them:
obs-migrate delete-rules \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --confirm
```

Exit codes: `2` if the cluster is unreachable, `1` if any delete fails, `0` otherwise (including a clean dry run).

In very large spaces, `delete-rules` may hit its rule-listing scan limit before it sees every rule. If that happens it returns `rule_listing_truncated`, exits `2`, and does **not** delete anything. Increase `--max-pages` and rerun the dry run before passing `--confirm`.

### Disable instead of delete

If the user wants to **stop the rules from firing without removing them** (e.g. keep them for review, or re-enable later), disable rather than delete:

```bash
obs-migrate audit-rules \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --disable-enabled
```

`audit-rules --disable-enabled` disables the enabled migrated rules but leaves them in place. Use `delete-rules --confirm` only when the user truly wants them gone.

## Full revert (dashboards + rules)

For a complete back-out, do both — preview each, then execute:

```bash
# Dashboards: list, then delete by id (see above).
# Rules: dry-run, then delete with --confirm:
obs-migrate delete-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"          # preview
obs-migrate delete-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --confirm # execute
```

After reverting, the user can re-run the migration cleanly (e.g. via migrate-selected-assets or migrate-all-supported-assets) without duplicate assets.

## Honest limits (tell the user)

- **Dashboards delete by id, not by tag.** There is no migration-tag filter for dashboards and no "delete all" flag — list and pass ids.
- **Dashboard deletion has no dry-run or `--confirm`.** The safe preview is `cluster list-dashboards`; once `delete-dashboards --dashboard-ids ...` runs, it mutates the target.
- **Dashboards become `[DELETED]` placeholders**, not fully removed objects — that is the command's Serverless-safe behavior, not a bug.
- **`delete-rules` only matches migrated rules** (tag `obs-migration` / name `[migrated] ...`). Rules created another way are not in scope; hand-built Kibana rules are untouched.
- **Source is never modified.** Reverting cleans Kibana only; the user's Grafana/Datadog assets remain as-is.
- **Custom-CA / self-signed clusters:** `cluster` and `delete-rules` accept `--ca-cert <path>` (env `OBS_MIGRATE_CA_CERT`) or `--insecure` (env `OBS_MIGRATE_INSECURE`, testing only).

## Do NOT

- Do **not** run a destructive delete without confirming the scope with the user first.
- Do **not** claim `delete-dashboards` fully removed the objects — it leaves `[DELETED]` placeholders.
- Do **not** invent a "delete all dashboards" or "delete by tag" dashboard flag — neither exists; use ids.
- Do **not** use `delete-rules` when the user only wants rules paused — use `audit-rules --disable-enabled` for disable-without-delete.
- Do **not** touch the source vendor to "undo" — revert is target-only.

## See also

- `migrate-selected-assets` skill — re-migrate a chosen subset after reverting.
- `migrate-all-supported-assets` skill — re-run a full migration after a clean revert.
- `obs-migrate cluster --help`, `obs-migrate delete-rules --help`, `obs-migrate audit-rules --help` — authoritative flags for the installed version.
- `docs/command-contract.md` — cluster, delete-rules, and audit-rules contracts and the Serverless delete caveat (online docs / repo).
