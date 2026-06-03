---
name: evaluate-o11y-permissions
description: Verify that the user's credentials have the permissions needed to run an obs-migrate / mig-to-kbn migration end-to-end — read/export on the source (Grafana/Datadog) and write on the Elastic/Kibana target. Use when the user asks whether their credentials/API key has the right permissions, roles, or privileges to export from their source or to import dashboards / create alert rules into Kibana, or wants to check access before committing to a migration.
---

# Evaluate migration permissions (source + target)

Goal: give the user confidence their credentials can perform every step **before** they invest in a migration. Separate non-mutating probes from checks that change target state, and be honest about what each proves.

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
set -a && source grafana_creds.env && set +a
KIBANA_URL= .venv/bin/grafana-migrate --source api --output-dir /tmp/perm-src --assets dashboards
```

- **Pulled dashboards:** source read permission is sufficient.
- **401/403:** the source user/token lacks read access (or is wrong).

Note on Grafana alerts: Grafana alert artifacts are derived from dashboard JSON during migration, **not** fetched as a separate API asset. Do not treat `--assets alerts` as a distinct source *permission* probe for Grafana. For Datadog, monitor read is a real separate scope — `--assets alerts` with `datadog-migrate` exercises the Monitors API.

`--assets` takes exactly one value: `dashboards`, `alerts`, or `all`. It is **not** a comma list — to exercise both dashboard and monitor reads in one Datadog run use `--assets all`, not `--assets dashboards,alerts`.

## Target permission checks — non-mutating first

These do **not** create or modify dashboards/rules. Run these to validate the Kibana API key safely:

```bash
set -a && source serverless_creds.env && set +a   # ELASTICSEARCH_ENDPOINT, KIBANA_ENDPOINT, KEY

# 1. API key auth + serverless detection (also reveals the delete limitation below)
.venv/bin/obs-migrate cluster detect-serverless --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# 2. Saved-object READ (via _export on Serverless)
.venv/bin/obs-migrate cluster list-dashboards --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# 3. ES read for field validation
curl -sf -H "Authorization: ApiKey $KEY" "$ELASTICSEARCH_ENDPOINT/metrics-*/_field_caps?fields=*" >/dev/null && echo "ES read OK"

# 4. Alerting read (only if migrating alerts) — no CLI wrapper, use curl
curl -s -H "Authorization: ApiKey $KEY" "$KIBANA_ENDPOINT/api/alerting/_health"
```

`ensure-data-views` creates/updates data views, so treat it as a **mutating** check:

```bash
# Data-view CREATE/UPDATE (changes target state — only run if you intend to create them)
.venv/bin/obs-migrate cluster ensure-data-views \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --data-view-patterns "metrics-*,logs-*"
```

## Target write proof — these CHANGE target state

Only run these when the user accepts that they create objects. State this explicitly before running.

```bash
# Dashboard import proof (creates a dashboard in Kibana; does not self-clean)
.venv/bin/obs-migrate upload \
  --yaml-dir examples/alerting/generated/grafana/dashboards/yaml \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# Alert-rule create proof — self-cleaning round trip (creates then DELETES unless --keep-rules)
.venv/bin/python scripts/verify_alert_rule_uploads.py \
  --kibana-url "$KIBANA_ENDPOINT" --api-key "$KEY" --limit 1
```

`verify_alert_rule_uploads.py` creates rules with a temporary marker tag and deletes them on exit unless `--keep-rules` is passed (`--limit N` caps how many payloads it verifies). Use `scripts/audit_migrated_rules.py` to read migrated rules without creating anything.

## Serverless caveats (call these out)

- Saved-object `GET`/`_find`/direct `DELETE` are blocked on Serverless. Listing uses `_export`; "delete" rewrites objects to `[DELETED]` placeholders via re-import. So a user can lack nothing and still be unable to hard-delete — that is the platform, not a permission gap.
- Migration-created rules are **disabled** by default and tagged `obs-migration`.

## Do NOT

- Do **not** present a state-changing command (`upload`, `ensure-data-views`, rule creation) as a "safe permission check" without saying it mutates the target.
- Do **not** invent flags, endpoints, or privilege names. `obs-migrate doctor` checks local tool resolution, **not** credentials/permissions.
- Do **not** claim Grafana `--assets alerts` proves a separate source alert-read permission.

## See also

- `connect-to-o11y-source` skill — source setup and reachability.
- `observability_migration/targets/kibana/serverless.py` — exact Serverless endpoint constraints.
- `docs/command-contract.md` — `cluster` actions and the alert upload flow.
