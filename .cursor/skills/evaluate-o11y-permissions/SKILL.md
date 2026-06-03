---
name: evaluate-o11y-permissions
description: Verify that the user's credentials have the permissions needed to run an obs-migrate / mig-to-kbn migration end-to-end — read/export on the source (Grafana/Datadog) and write on the Elastic/Kibana target. Use when the user asks whether their credentials/API key has the right permissions, roles, or privileges to export from their source or to import dashboards / create alert rules into Kibana, or wants to check access before committing to a migration.
---

# Evaluate migration permissions (source + target)

Goal: give the user confidence their credentials can perform every step **before** they invest in a migration. Separate non-mutating probes from checks that change target state, and be honest about what each proves.

## Which command form to use (package vs. repo)

Assume the user **installed the package** (`pip install 'obs-migrate[all]'`): `obs-migrate`/`grafana-migrate`/`datadog-migrate` are on `PATH`. Prefix `.venv/bin/` only for a repo checkout. Helper **scripts** (`scripts/verify_alert_rule_uploads.py`, `scripts/audit_migrated_rules.py`) ship **only in the repo**, not in the package — for package users, prefer the built-in CLI paths shown below and do not tell them to run a `scripts/...` file they do not have. Likewise `examples/` YAML does not exist for them; use their own migrated output.

## Mental model (state this to the user)

- **The source (Grafana/Datadog) is read-only.** The tool never writes back to the source. So the only source permission that matters is **read/search/export of dashboards** (and, for Datadog, monitors). If `connect-to-o11y-source` succeeded, source read is already proven.
- **The target (Elastic/Kibana) is where write permission matters.** The migration needs an API key that can:
  - **import** saved objects — `POST /api/saved_objects/_import` (dashboards)
  - **read** saved objects — `POST /api/saved_objects/_export` (listing)
  - **manage data views** — `GET/POST/DELETE /api/data_views/...`
  - **create alert rules** (only if migrating alerts) — `POST /api/alerting/rule`
  - **read** target indices for field validation — ES `_field_caps`

## Source permission check (non-mutating)

Reading dashboards is the proof. (See the `connect-to-o11y-source` skill for full setup.)

```bash
export GRAFANA_URL="https://grafana.example.com" GRAFANA_USER="..." GRAFANA_PASS="..."
KIBANA_URL= grafana-migrate --source api --output-dir /tmp/perm-src --assets dashboards
```

- **Pulled dashboards:** source read permission is sufficient.
- **401/403:** the source user/token lacks read access (or is wrong).

Note on Grafana alerts: Grafana alert artifacts are derived from dashboard JSON during migration, **not** fetched as a separate API asset. Do not treat `--assets alerts` as a distinct source *permission* probe for Grafana. For Datadog, monitor read is a real separate scope — `--assets alerts` with `datadog-migrate` exercises the Monitors API.

`--assets` takes exactly one value: `dashboards`, `alerts`, or `all`. It is **not** a comma list — to exercise both dashboard and monitor reads in one Datadog run use `--assets all`, not `--assets dashboards,alerts`.

## Target permission checks — non-mutating first

These do **not** create or modify dashboards/rules. Run these to validate the Kibana API key safely:

Export your target endpoints/key first (any names work; this skill uses `KIBANA_ENDPOINT`, `ELASTICSEARCH_ENDPOINT`, `KEY`):

```bash
export KIBANA_ENDPOINT="https://...kb..." ELASTICSEARCH_ENDPOINT="https://...es..." KEY="<api-key>"

# 1. API key auth + serverless detection (also reveals the delete limitation below)
obs-migrate cluster detect-serverless --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# 2. Saved-object READ (via _export on Serverless)
obs-migrate cluster list-dashboards --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# 3. ES read for field validation
curl -sf -H "Authorization: ApiKey $KEY" "$ELASTICSEARCH_ENDPOINT/metrics-*/_field_caps?fields=*" >/dev/null && echo "ES read OK"

# 4. Alerting read (only if migrating alerts) — no CLI wrapper, use curl
curl -s -H "Authorization: ApiKey $KEY" "$KIBANA_ENDPOINT/api/alerting/_health"
```

`ensure-data-views` creates/updates data views, so treat it as a **mutating** check:

```bash
# Data-view CREATE/UPDATE (changes target state — only run if you intend to create them)
obs-migrate cluster ensure-data-views \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --data-view-patterns "metrics-*,logs-*"
```

## Target write proof — these CHANGE target state

Only run these when the user accepts that they create objects. State this explicitly before running.

```bash
# Dashboard import proof (creates a dashboard in Kibana; does not self-clean).
# Use the user's OWN migrated YAML from a prior `obs-migrate migrate` run.
obs-migrate upload \
  --yaml-dir <their-output-dir>/dashboards \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# Alert-rule create proof via the shipped CLI: migrate alerts and create the
# rules DISABLED (tagged obs-migration). This is the package-native write check.
obs-migrate migrate --source grafana --input-mode api \
  --output-dir /tmp/perm-alerts --assets alerts \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --create-alert-rules
```

Created rules are **disabled by default** and tagged `obs-migration`; review them in the Kibana UI (or via `GET /api/alerting/rules/_find?search=obs-migration`) and delete any you do not want. The import proof leaves a dashboard behind — delete it with `obs-migrate cluster delete-dashboards` afterward if it was only a test.

> Repo checkout only: `scripts/verify_alert_rule_uploads.py --limit 1` does a self-cleaning create→delete round trip, and `scripts/audit_migrated_rules.py` reads migrated rules without creating anything. These scripts are **not** in the installed package — do not reference them for package users.

## Serverless caveats (call these out)

- Saved-object `GET`/`_find`/direct `DELETE` are blocked on Serverless. Listing uses `_export`; "delete" rewrites objects to `[DELETED]` placeholders via re-import. So a user can lack nothing and still be unable to hard-delete — that is the platform, not a permission gap.
- Migration-created rules are **disabled** by default and tagged `obs-migration`.

## Do NOT

- Do **not** present a state-changing command (`upload`, `ensure-data-views`, rule creation) as a "safe permission check" without saying it mutates the target.
- Do **not** invent flags, endpoints, or privilege names. `obs-migrate doctor` checks local tool resolution, **not** credentials/permissions.
- Do **not** claim Grafana `--assets alerts` proves a separate source alert-read permission.
- Do **not** point package users at `scripts/...` files or `examples/...` YAML — those exist only in a repo checkout.

## See also

- `connect-to-o11y-source` skill — source setup and reachability.
- `obs-migrate cluster --help` and `obs-migrate migrate --help` — authoritative target/alerting flags for the installed version.
- `docs/command-contract.md` — `cluster` actions and the alert upload flow (online docs / repo).
