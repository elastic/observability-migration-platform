---
name: migrate-all-supported-assets
description: Use when the user has decided to fully switch and wants to "migrate everything", "do the whole environment", "migrate all my dashboards and alerts", or a complete cutover — bulk-migrates EVERY supported dashboard and/or alerting rule from a connected Grafana or Datadog environment into Kibana in one sweep, and reports exactly which assets could not migrate. For a chosen subset use migrate-selected-assets; for a single proof dashboard use try-one-source-dashboard.
---

# Migrate every supported dashboard / alerting rule

Goal: migrate **everything the engine supports** from a source in one run, then give the user a straight account of what landed and **what could not migrate**. This is the full-sweep step for teams that have already assessed readiness and decided to switch.

This skill writes all supported assets to the target. It is otherwise read-only on the source.

## Which command form to use (package vs. repo)

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. Every command and artifact below ships in the installed wheel — no `scripts/`, `infra/`, or `examples/` directory is required.

## Core fact: "all" is the absence of a selector

There is no `--all` flag. A bulk migration is simply `--assets all` (or `--assets dashboards` / `--assets alerts`) **with no id/query selector** — the engine then processes the full set the source returns and reports the ones it cannot translate. Unsupported assets are **surfaced, never silently dropped** (degrade-gracefully).

- `--assets dashboards` → every supported dashboard, no alert artifacts.
- `--assets alerts` → every supported rule/monitor, no dashboard YAML.
- `--assets all` → both isolated pipelines in one command (the union).

## Step 1 — Run the full migration

Datadog, everything, live API (creates rules disabled + tagged in the same run):

```bash
export ELASTICSEARCH_ENDPOINT="https://...es..." KIBANA_ENDPOINT="https://...kbn..." KEY="<api-key>"
export DD_API_KEY="..." DD_APP_KEY="..." DD_SITE="datadoghq.com"

obs-migrate migrate \
  --source datadog \
  --input-mode api \
  --env-file datadog_creds.env \
  --output-dir full_out \
  --assets all \
  --field-profile otel \
  --data-view "metrics-*" \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY" \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --upload \
  --create-alert-rules
```

Grafana, everything, live API:

```bash
export GRAFANA_URL="https://grafana.example.com" GRAFANA_USER="..." GRAFANA_PASS="..."

obs-migrate migrate \
  --source grafana \
  --input-mode api \
  --output-dir full_out \
  --assets all \
  --data-view "metrics-*" \
  --esql-index "metrics-*" \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY" \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --upload \
  --create-alert-rules
```

- `--upload` recompiles and pushes dashboards through `kb-dashboard-cli`; `--create-alert-rules` creates the emitted rules **disabled** and tagged `obs-migration`.
- Drop `--upload` / `--create-alert-rules` for a dry, target-aware translation pass first; add them once the readout looks right. You can also split into two runs: produce artifacts, inspect, then `obs-migrate upload`.
- File-based sources: swap `--input-mode api` for `--input-mode files --input-dir <dir>` pointing at all exported dashboard JSON.
- **Custom-CA / self-signed clusters:** `--ca-cert <path>` (env `OBS_MIGRATE_CA_CERT`) verifies against a private CA; `--insecure` (env `OBS_MIGRATE_INSECURE`) skips verification for testing only. Both cover source, Elasticsearch, Kibana, and the Node upload step.

## Step 2 — Read what landed and what did NOT

The whole point of a bulk run is the **coverage report**. Read it; do not assume exit 0 means every asset is perfect.

| What you want | File | Field(s) |
|---|---|---|
| Human-readable verdict + per-dashboard table + must-fix worklist | `full_out/dashboards/migration_summary.md` | verdict, scorecard, must-fix list, grouped warnings |
| Dashboard coverage buckets | `full_out/dashboards/migration_report.json` | `summary` (counts), readiness buckets |
| Per-panel status / why a panel didn't migrate | `full_out/dashboards/migration_manifest.json` | `panels[].status`, `panels[].reasons`; Grafana type: `panels[].grafana_type`; Datadog widget type: `panels[].datadog_widget_type` |
| Which asset families ran | `full_out/run_summary.json` | top-level summary |
| Alert rule creation results | `full_out/alerts/monitor_rule_upload_results.json` (Datadog) / `full_out/alerts/alert_rule_upload_results.json` (Grafana) | created / failed / skipped |

The dashboards that **cannot** migrate appear as non-clean entries in `migration_summary.md` and as `requires_manual` / `not_feasible` panels (with `reasons`) in the manifest. Alerts that cannot be emitted are reported in the alert artifacts. Relay these explicitly — that list is the deliverable, not an afterthought.

## Step 3 — Verify and hand off the gaps

1. State the **coverage**: how many dashboards/panels and rules migrated cleanly vs. need rework, straight from the summary/report.
2. For broken/empty uploaded panels, hand off to the `debug-uploaded-kibana-dashboard` skill (it captures the real ES|QL Kibana runs and classifies the failure).
3. For panels that didn't migrate at all, the "explain the gaps" skill (Phase E) turns each `reason` into manual rebuild guidance once it lands.
4. Migrated rules are **disabled**; review with `obs-migrate audit-rules` before enabling.

## Honest limits (tell the user)

- **Exit 0 ≠ everything is perfect.** The run can succeed while individual panels are `requires_manual` / empty. Trust the report, not just the exit code.
- **Grafana API extraction is capped at 500 dashboards** per search request — a very large org may not be fully covered in one API pass; note it and consider file exports for the remainder.
- **No `--es-url` ⇒ no live validation.** A target-less run still translates and reports, but does not prove panels render against real data.
- **Created rules are disabled.** A bulk alert migration arms nothing; rules are disabled and tagged `obs-migration` until enabled by a human.
- **Empty panels are often missing data, not a bug.** A clean translation renders empty without matching telemetry in the target; lighting panels up with synthetic data is a separate capability.
- **Degrade gracefully:** unsupported assets are surfaced with reasons, never hidden — that is the contract.

## Do NOT

- Do **not** report "migrated everything" without reading the coverage report and naming what could not migrate.
- Do **not** treat exit 0 as proof every panel is correct.
- Do **not** claim a bulk alert migration enabled rules — it creates them disabled.
- Do **not** invent flags. Bulk = `--assets all` with no selector; confirm with `obs-migrate migrate --help`.
- Do **not** cite manifest fields you have not confirmed (e.g. `grafana_type` exists on Grafana manifest panels; open the JSON when unsure).

## See also

- `assess-migration-readiness` skill — run this first to know how much will migrate (and at what evidence level) before sweeping.
- `migrate-selected-assets` skill — migrate a chosen subset instead of everything.
- `debug-uploaded-kibana-dashboard` skill — diagnose individual broken uploaded panels after the sweep.
- `revert-migration` skill — remove the generated dashboards/rules if the user backs out.
- `obs-migrate migrate --help` — authoritative asset/upload flags for the installed version.
- `docs/command-contract.md` — asset-scope contract and artifact descriptions (online docs / repo).
