---
name: migrate-selected-assets
description: Use when the user wants to migrate "these specific dashboards", "only my critical alerts", "just the monitors matching X", "this folder/team's dashboards", or otherwise scope a real migration to a selection — migrates a chosen SUBSET of a user's Grafana/Datadog dashboards and/or alerting rules into Kibana, not just one (that is try-one-source-dashboard) and not everything (that is migrate-all-supported-assets). Routes by the engine's selectors — uniform `--select-folder/-tag/-datasource/-team/-updated-after/-before/-starred` metadata flags, plus Datadog ids/query — and is honest about which dimensions a given source/asset cannot supply.
---

# Migrate selected dashboards / alerting rules

Goal: migrate a **deliberately chosen subset** of the user's source assets into Kibana — more than the single-dashboard trial, less than a full sweep. Scope with the uniform `--select-*` metadata flags (folder / tag / datasource / team / last-updated / starred), available on `obs-migrate migrate` for **both** sources and **both** dashboards and alerts. Some dimensions a given source/asset genuinely cannot supply — those **degrade gracefully** (the asset is kept and a `WARN` names the skipped dimension), so be explicit about what filters effectively and what passes through.

This skill writes the selected assets to the target. It is otherwise read-only on the source.

## Which command form to use (package vs. repo)

Assume the user **installed the package** (`obs-migrate`, `grafana-migrate`, `datadog-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. Every command and artifact below ships in the installed wheel — no `scripts/`, `infra/`, or `examples/` directory is required.

## The selection surface (read this before scoping)

Two layers of selectors, combinable:

**1. Uniform metadata `--select-*` flags** (both sources, both dashboards and alerts). Each is repeatable or comma-separated; values **OR within a flag**, and flags **AND together**. Client-side filter applied after extraction.

| Flag | Selects on |
|---|---|
| `--select-folder NAME` | Folder (Grafana dashboard folder) |
| `--select-tag TAG` | Tags / labels (e.g. `team:infra`, `env:prod`) |
| `--select-datasource TYPE` | Datasource type (e.g. `prometheus`, `elasticsearch`) |
| `--select-team TEAM` | Team/owner (from a `team:` tag or Grafana `team` label) |
| `--select-updated-after WHEN` / `--select-updated-before WHEN` | Last-updated (ISO date/datetime or epoch sec/millis) |
| `--select-starred` | Starred / popular (Grafana dashboards) |

Matching is case-insensitive **exact** (not prefix/glob). A selector for a dimension a source/asset can't supply **does not drop the asset** — it's kept with a `WARN` (see availability below).

**2. Source-native id/query selectors** (Datadog only), useful for precise picks or pushing the filter to the Datadog side:

| Source | Dashboards | Alerts / monitors |
|---|---|---|
| **Datadog** | `--dashboard-ids id1,id2,...` (live API) | `--monitor-ids id1,id2,...` **or** `--monitor-query "<search>"` (Datadog-side search) |
| **Grafana** | — (use `--select-*` or a curated `--input-dir`) | — (use `--select-*`) |

### Which `--select-*` dimensions are effective per source/asset

| Dimension | Grafana dashboards | Grafana alerts | Datadog dashboards | Datadog monitors |
|---|---|---|---|---|
| folder | ✅ | legacy: ✅ (via dashboard) · unified: ⚠️ degrade | ⚠️ degrade (Dashboard Lists not fetched) | ⚠️ degrade |
| tag | ✅ | ✅ (labels) | ✅ | ✅ |
| datasource | ✅ | ✅ | ⚠️ (`datadog`) | ⚠️ degrade |
| team | ⚠️ degrade (no first-class team) | ✅ (`team` label) | ✅ (`team:` tag) | ✅ (`team:` tag) |
| updated-after/before | ✅ | ⚠️ if rule carries `updated` | ✅ (`modified_at`) | ✅ (`modified`) |
| starred | ✅ | ⚠️ degrade | ⚠️ degrade | ⚠️ degrade |

⚠️ degrade = the asset is **kept** and a `WARN` is printed; the dimension simply doesn't narrow that source/asset.

## Step 1 — Scope to the selection

### By metadata (`--select-*`, either source, dashboards or alerts)

```bash
# Grafana dashboards in the "Production" folder, tagged team:infra, updated this year:
obs-migrate migrate \
  --source grafana --input-mode api \
  --grafana-url "$GRAFANA_URL" --grafana-token "$GRAFANA_TOKEN" \
  --select-folder Production --select-tag team:infra \
  --select-updated-after 2026-01-01 \
  --output-dir selected_out --assets dashboards --data-view "metrics-*"

# Datadog dashboards + monitors for the payments team (team: tag), one sweep:
obs-migrate migrate \
  --source datadog --input-mode api --env-file datadog_creds.env \
  --select-team payments \
  --output-dir selected_out --assets all --field-profile otel --data-view "metrics-*"
```

`--select-*` flags combine with the id/query selectors below (AND). Watch the run output: `Selected N of M …` confirms the narrowing, and any `WARN: … selection requested but unavailable …` tells you a dimension degraded for that source/asset (per the availability table above) — relay those lines to the user.

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

### Grafana dashboards — by `--select-*` or a curated input directory

Grafana API mode has **no `--dashboard-ids`**. Either scope with `--select-folder/-tag/-datasource/-starred` against the live API (above), or migrate a hand-picked set by putting just those dashboards' exported JSON in one directory and running a files migration (`--select-*` still applies on top if you want):

```bash
# Collect only the chosen dashboard exports into ./selected_dashboards/ first, then:
obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir ./selected_dashboards \
  --output-dir selected_out \
  --assets dashboards \
  --data-view "metrics-*"
```

Export each chosen dashboard's JSON from the Grafana UI (*Share → Export → Save to file*) or `GET /api/dashboards/uid/<uid>`, into the same directory. The selection **is** the directory contents.

### Grafana alerts — subset with `--select-*`

Grafana alert selection now works via the metadata flags. Legacy (panel-embedded) alerts inherit their dashboard's folder/tags/datasource, so `--select-folder`/`--select-tag` scope them through the dashboard. Unified alerting rules filter on their **labels** (`--select-tag`, `--select-team`) and datasource; their folder is exposed only as a UID, so `--select-folder` **degrades** on unified rules (kept + `WARN`). There is still no per-rule **id** selector — scope by metadata, not by an invented id flag.

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

- **Some dimensions degrade per source/asset.** `--select-*` is uniform, but not every dimension is supplyable everywhere (see the availability table): Datadog dashboard folders live in the Dashboard-Lists API the engine doesn't fetch; Datadog/Grafana-unified starred and Datadog-monitor folder/datasource aren't available; Grafana dashboards have no first-class team. Those **degrade gracefully** (asset kept + `WARN`) rather than dropping assets — surface the `WARN` lines, don't pretend the filter applied.
- **Selectors are exact + client-side.** Matching is case-insensitive exact (no prefix/glob); filtering happens after extraction. A `--select-*` set that matches nothing for **dashboards** exits non-zero; for alerts it yields an empty alert set.
- **`--monitor-query` is Datadog-side.** Its expressiveness is Datadog's, not ours; it composes with `--select-*` (which then narrows further client-side).
- **No id selector for Grafana.** Grafana has no `--dashboard-ids` or alert-id flag — scope Grafana by `--select-*` or a curated `--input-dir`.
- **Created rules are disabled.** A selected alert migration does not arm anything; rules are disabled and tagged `obs-migration` until a human enables them.
- **Degrade gracefully (panels too):** unsupported panels/rules in the selection are surfaced as `requires_manual` / `not_feasible` with reasons — relay them, never hide them.

## Do NOT

- Do **not** invent selectors beyond the real surface (a Grafana `--dashboard-ids` / alert-id flag, or `--select-*` dimensions not listed above). Confirm with `obs-migrate migrate --help` if unsure.
- Do **not** claim a degraded dimension filtered anything — if the run printed a `WARN` that a dimension was unavailable, that source/asset was **not** narrowed on it.
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
