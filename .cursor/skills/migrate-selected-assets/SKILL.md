---
name: migrate-selected-assets
description: Migrate a chosen SUBSET of a user's Grafana/Datadog dashboards and/or alerting rules into Kibana — not just one (that is try-one-source-dashboard) and not everything (that is migrate-all-supported-assets). Use when the user wants to migrate "these specific dashboards", "only my critical alerts", "just the monitors matching X", "this folder/team's dashboards", or otherwise scope a real migration to a selection. Routes by the engine's actual selectors (Datadog ids/query; Grafana file-scoping) and is honest about selection it cannot do.
---

# Migrate selected dashboards / alerting rules

Goal: migrate a **deliberately chosen subset** of the user's source assets into Kibana — more than the single-dashboard trial, less than a full sweep. The hard part is **scoping**: the engine's selection surface is narrower than "browse by folder/tag/team", so pick the right selector for the source and be explicit about what cannot be filtered.

This skill writes the selected assets to the target. It is otherwise read-only on the source.

## Which command form to use (package vs. repo)

Assume the user **installed the package** (`obs-migrate`, `grafana-migrate`, `datadog-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. Every command and artifact below ships in the installed wheel — no `scripts/`, `infra/`, or `examples/` directory is required.

## The selection surface (read this before scoping)

There is **no** "select by folder / tag / datasource / team / popularity" flag. What actually exists, per source and asset family:

| Source | Dashboards | Alerts / monitors |
|---|---|---|
| **Datadog** | `--dashboard-ids id1,id2,...` (live API) | `--monitor-ids id1,id2,...` **or** `--monitor-query "<search>"` |
| **Grafana** | **No API id selector** — scope by `--input-dir` containing only the chosen dashboard exports | **No selector** — alert extraction is all-or-nothing |

So a "selected" migration is: Datadog → id/query lists; Grafana dashboards → a curated input directory; Grafana alerts → cannot be subset (say so, and offer migrate-all-supported-assets instead, or post-filter in Kibana).

## Step 1 — Scope to the selection

### Datadog dashboards — by id

```bash
export DD_API_KEY="..." DD_APP_KEY="..." DD_SITE="datadoghq.com"
obs-migrate migrate \
  --source datadog \
  --input-mode api \
  --dashboard-ids abc-def-123,ghi-jkl-456 \
  --output-dir selected_out \
  --assets dashboards \
  --field-profile otel \
  --data-view "metrics-*"
```

### Datadog alerts — by id list or search query

```bash
# Explicit monitor ids:
obs-migrate migrate \
  --source datadog --input-mode api \
  --env-file datadog_creds.env \
  --monitor-ids 12345678,23456789 \
  --output-dir selected_out \
  --assets alerts --field-profile otel --data-view "metrics-*"

# Or a Datadog monitor search query (e.g. by tag/team/name in Datadog's own syntax):
obs-migrate migrate \
  --source datadog --input-mode api \
  --env-file datadog_creds.env \
  --monitor-query "team:payments status:alert" \
  --output-dir selected_out \
  --assets alerts --field-profile otel --data-view "metrics-*"
```

`--monitor-query` is passed to Datadog's monitor search — it filters **on the Datadog side** using Datadog's query syntax, so "by team/tag" works only to the extent Datadog itself supports it. The engine does not re-implement filtering.

### Grafana dashboards — by curated input directory

Grafana API mode has **no `--dashboard-ids`**. To migrate a chosen set, put just those dashboards' exported JSON in one directory and run a files migration:

```bash
# Collect only the chosen dashboard exports into ./selected_dashboards/ first, then:
obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir ./selected_dashboards \
  --output-dir selected_out \
  --assets dashboards \
  --native-promql \
  --data-view "metrics-*"
```

Export each chosen dashboard's JSON from the Grafana UI (*Share → Export → Save to file*) or `GET /api/dashboards/uid/<uid>`, into the same directory. The selection **is** the directory contents.

### Grafana alerts — cannot be subset

Grafana alert extraction pulls the unified alerting resources as a whole; there is no per-rule selector. If the user wants only some Grafana alerts, either migrate them all (migrate-all-supported-assets) and curate in Kibana afterward, or migrate by folder on the Grafana side before export if their setup allows. **Do not invent a Grafana monitor/alert id flag.**

## Step 2 — Add the target, validate, upload

Add the target endpoints you have, re-run Step 1 with live validation, then upload. For alerts, `--create-alert-rules` creates the emitted rules **disabled** and tagged `obs-migration`.

```bash
export ELASTICSEARCH_ENDPOINT="https://...es..." KIBANA_ENDPOINT="https://...kbn..." KEY="<api-key>"

# Dashboards: re-run Step 1 appending live discovery, then upload:
#   ...append: --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY"
obs-migrate upload \
  --yaml-dir selected_out/dashboards \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY"

# Alerts (selected): create the rules in one shot, disabled + tagged:
obs-migrate migrate \
  --source datadog --input-mode api --env-file datadog_creds.env \
  --monitor-ids 12345678,23456789 \
  --output-dir selected_out --assets alerts \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --create-alert-rules
```

- `obs-migrate upload` recompiles YAML internally via `kb-dashboard-cli` and accepts either the `yaml/` directory or the dashboard artifacts dir with a sibling `yaml/` (so `selected_out/dashboards` works).
- `--create-alert-rules` requires an alert-capable selection (`--assets alerts` or `all`) plus `--kibana-url` and `--kibana-api-key`. Rules land **disabled** — enable them in Kibana (or audit with `obs-migrate audit-rules`) after review.
- **Custom-CA / self-signed clusters:** every CLI here accepts `--ca-cert <path>` (env `OBS_MIGRATE_CA_CERT`) to verify against a private CA, or `--insecure` (env `OBS_MIGRATE_INSECURE`) for testing only. They cover source, Elasticsearch, Kibana, and the Node upload step.

## Step 3 — Confirm the selection landed

- **Dashboards:** read `selected_out/dashboards/migration_summary.md` (verdict, scorecard, per-dashboard table, must-fix worklist); drill into `selected_out/dashboards/migration_manifest.json` (`dashboards[]`, `panels[].status`, `panels[].reasons`). Confirm the count matches what you selected.
- **Alerts:** the rule-creation summary is `selected_out/alerts/monitor_rule_upload_results.json` (Datadog) or `selected_out/alerts/alert_rule_upload_results.json` (Grafana). Then `obs-migrate audit-rules --kibana-url ... --kibana-api-key ...` lists the migrated rules in Kibana and reports which are enabled.

## Honest limits (tell the user)

- **No metadata selection.** Folder / tag / datasource / team / "most used" selection does not exist in the engine. Datadog id/query and Grafana input-dir scoping are the only levers; relay that plainly.
- **Grafana = file scoping only.** Selecting Grafana dashboards means curating an input directory of exports; selecting Grafana alerts is not possible at all.
- **`--monitor-query` is Datadog-side.** Its expressiveness is Datadog's, not ours; a query that returns nothing yields an empty selection, not an error about the flag.
- **Created rules are disabled.** A selected alert migration does not arm anything; rules are disabled and tagged `obs-migration` until a human enables them.
- **Degrade gracefully:** unsupported panels/rules in the selection are surfaced as `requires_manual` / `not_feasible` with reasons — relay them, never hide them.

## Do NOT

- Do **not** invent selectors that don't exist (`--folder`, `--tag`, `--team`, a Grafana `--dashboard-ids`, or a Grafana alert id flag). Confirm with `obs-migrate migrate --help` if unsure.
- Do **not** claim a selected alert migration enabled rules — it creates them disabled.
- Do **not** treat an offline run (no `--es-url`, no upload smoke) as proof the selected panels render against real data.
- Do **not** bulk-migrate here. Migrating everything supported is migrate-all-supported-assets; a single proof dashboard is try-one-source-dashboard.

## See also

- `scan-o11y-environment` skill — inventory the assets so the user knows what to select.
- `assess-migration-readiness` skill — feasibility verdict + evidence level before committing the selection.
- `try-one-source-dashboard` skill — one dashboard end-to-end for a side-by-side.
- `migrate-all-supported-assets` skill — migrate everything supported (use when selection isn't needed).
- `revert-migration` skill — remove the selected assets if the user changes their mind.
- `obs-migrate migrate --help` — authoritative selector list for the installed version.
- `docs/command-contract.md` — asset-scope contract, selectors, and artifact paths (online docs / repo).
