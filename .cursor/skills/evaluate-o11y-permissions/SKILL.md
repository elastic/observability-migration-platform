---
name: evaluate-o11y-permissions
description: Use when the user asks whether their credentials/API key has the right permissions, roles, or privileges to export from their source or to import dashboards / create alert rules into Kibana, or wants to check access before committing to a migration — verifies the credentials have what an obs-migrate / mig-to-kbn migration needs end-to-end — read/export on the source (Grafana/Datadog) and write on the Elastic/Kibana target.
---

# Evaluate migration permissions (source + target)

Goal: give the user confidence their credentials can perform every step **before** they invest in a migration. Separate non-mutating probes from checks that change target state, and be honest about what each proves.

## Which command form to use (package vs. repo)

Assume the user **installed the package** (`pip install 'obs-migrate[all]'`): `obs-migrate`/`grafana-migrate`/`datadog-migrate` are on `PATH`. Prefix `.venv/bin/` only for a repo checkout. The alert round-trip and rule-audit checks are shipped as the `obs-migrate verify-alert-rules` and `obs-migrate audit-rules` subcommands (use these), so package users do **not** need any `scripts/...` file. `examples/` YAML also does not exist for them; use their own migrated output.

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

# 4. Alerting read (only if migrating alerts) — package-native, read-only:
#    lists migrated rules (tagged obs-migration) and proves alerting-read access.
obs-migrate audit-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
```

`audit-rules` is **read-only by default** (it only lists migrated rules; it disables nothing unless you pass `--disable-enabled`). On a target with no migrated rules yet it simply reports zero — that still proves the key can reach and read the Alerting API. (Raw equivalent if you prefer: `curl -s -H "Authorization: ApiKey $KEY" "$KIBANA_ENDPOINT/api/alerting/_health"`.)

**Custom-CA / self-signed targets:** every `obs-migrate` command above (`cluster ...`, `audit-rules`, `upload`, `verify-alert-rules`) accepts the global TLS flags `--ca-cert <bundle>` (env `OBS_MIGRATE_CA_CERT`) and `--insecure` (env `OBS_MIGRATE_INSECURE`). If a probe fails with a TLS/`CERTIFICATE_VERIFY_FAILED` error rather than a 401/403, that's a trust problem, not a permission gap — add `--ca-cert` (keeps verification on; preferred) or, only with explicit user consent, `--insecure`. For the raw `curl` field-caps check, the analogous escapes are `curl --cacert <bundle>` or `curl -k`.

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

# Alert-rule write proof — SELF-CLEANING round trip (package-native).
# Creates the emitted rules DISABLED, confirms none came back enabled, then
# DELETES them (unless --keep-rules). Needs a comparison report from a prior
# alert-capable migration (e.g. <their-output-dir>/alerts/alert_comparison_results.json
# for Grafana, or <their-output-dir>/alerts/monitor_comparison_results.json for Datadog). --limit caps it.
obs-migrate verify-alert-rules \
  --comparison <their-output-dir>/alerts/alert_comparison_results.json \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --limit 1
```

`verify-alert-rules` is the preferred alert write check because it cleans up after itself. If the user has no comparison report yet (no alert migration run), the alternative is `obs-migrate migrate --source grafana --input-mode api --output-dir /tmp/perm-alerts --assets alerts --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --create-alert-rules`, which creates rules **disabled** and tagged `obs-migration` but does **not** self-clean — afterward, audit and disable/remove them with `obs-migrate audit-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --disable-enabled` and delete in the Kibana UI. The dashboard import proof also leaves a dashboard behind — delete it with `obs-migrate cluster delete-dashboards` if it was only a test.

## Serverless caveats (call these out)

- Saved-object `GET`/`_find`/direct `DELETE` are blocked on Serverless. Listing uses `_export`; "delete" rewrites objects to `[DELETED]` placeholders via re-import. So a user can lack nothing and still be unable to hard-delete — that is the platform, not a permission gap.
- Migration-created rules are **disabled** by default and tagged `obs-migration`.

## Do NOT

- Do **not** present a state-changing command (`upload`, `ensure-data-views`, rule creation) as a "safe permission check" without saying it mutates the target.
- Do **not** invent flags, endpoints, or privilege names. `obs-migrate doctor` checks local tool resolution, **not** credentials/permissions.
- Do **not** claim Grafana `--assets alerts` proves a separate source alert-read permission.
- Do **not** point package users at `scripts/...` files or `examples/...` YAML for the alert checks — use `obs-migrate verify-alert-rules` / `obs-migrate audit-rules` (shipped) and the user's own migrated output instead.
- Do **not** describe `audit-rules` (without `--disable-enabled`) as mutating — it only reads.

## See also

- `connect-to-o11y-source` skill — source setup and reachability.
- `obs-migrate verify-alert-rules --help` and `obs-migrate audit-rules --help` — the self-cleaning alert write proof and the read-only rule audit (shipped in the package).
- `obs-migrate cluster --help` and `obs-migrate migrate --help` — authoritative target/alerting flags for the installed version.
- `docs/command-contract.md` — `cluster` actions and the alert upload flow (online docs / repo).
