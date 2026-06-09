---
name: try-one-source-dashboard
description: Use when the user wants to "try one of my dashboards", "migrate just one", "do a single dashboard end-to-end", "prove it on one real dashboard first", or wants one real proof-of-value before committing — fully migrates ONE of the user's own Grafana/Datadog dashboards into Kibana end-to-end for a side-by-side comparison before bulk migration. For a count/type inventory use scan-o11y-environment; for a migrate/no-migrate verdict use assess-migration-readiness; for diagnosing a single broken uploaded panel use debug-uploaded-kibana-dashboard.
---

# Try one of my dashboards (single end-to-end migration)

Goal: take **one** dashboard the user already owns, migrate it all the way into Kibana, and set up a **side-by-side** so they can judge fidelity before committing to a bulk migration. This is the "prove it on something real" step — narrower than a full migration, higher-signal than inventory or a readiness verdict.

This skill writes exactly one dashboard to the target. It is otherwise read-only on the source.

## Which command form to use (package vs. repo)

Assume the user **installed the package** (`obs-migrate`, `grafana-migrate`, `datadog-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. Every artifact and command below ships in the installed wheel — no `scripts/`, `infra/`, or `examples/` directory is required.

## Step 1 — Scope to a single dashboard

How you pin to one dashboard differs by source. **This is the most common place to get it wrong**, so be explicit with the user about which path applies.

### Datadog — by dashboard id (live API)

Datadog exposes a dashboard id selector. Pass exactly one id:

```bash
export DD_API_KEY="..." DD_APP_KEY="..." DD_SITE="datadoghq.com"
obs-migrate migrate \
  --source datadog \
  --input-mode api \
  --dashboard-ids abc-def-123 \
  --output-dir try_one_out \
  --assets dashboards \
  --field-profile otel \
  --data-view "metrics-*"
```

The `--dashboard-ids` selector scopes the live Datadog API run to the one dashboard you want to try. The dedicated `datadog-migrate --source api --dashboard-ids <id>` form also works, but prefer the unified `obs-migrate migrate` command in new package-first guidance.

### Grafana — by single-dashboard export (no API id selector)

Grafana API mode has **no `--dashboard-ids`** selector. To migrate exactly one Grafana dashboard, point a files run at a directory that contains only that one dashboard's exported JSON:

```bash
# Put just the one dashboard export in its own directory first.
obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir ./one_dashboard \
  --output-dir try_one_out \
  --assets dashboards \
  --data-view "metrics-*"
```

If the user only has live Grafana access, have them export the single dashboard's JSON (Grafana UI: *Share → Export → Save to file*, or the `/api/dashboards/uid/<uid>` endpoint) into an otherwise empty directory, then run the files command above. Do not pretend an API id selector exists for Grafana.

Use `--assets dashboards` here — a single-dashboard trial is about the dashboard. (Alerts are a separate Phase D skill.)

## Step 2 — Add the target and upload

For a real side-by-side you want the dashboard **in Kibana**, ideally validated against live data. Add the target endpoints you have, then upload.

```bash
export ELASTICSEARCH_ENDPOINT="https://...es..." KIBANA_ENDPOINT="https://...kbn..." KEY="<api-key>"

# Re-run Step 1 with live target discovery / query validation:
#   ...append: --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY"
#   (Grafana uses native PromQL by default; with --es-url it verifies target
#    support and downgrades to ES|QL translation if unsupported)

# Then upload the one migrated dashboard:
obs-migrate upload \
  --yaml-dir try_one_out/dashboards \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY"
```

- `obs-migrate upload` recompiles the YAML internally via `kb-dashboard-cli` and accepts either the `yaml/` directory or the dashboard artifacts directory that contains a sibling `yaml/` (so `try_one_out/dashboards` works).
- Adding `--es-url` during Step 1 turns on live target field discovery and emitted-query validation; without it the run stays in offline analysis and panels may look empty for lack of data, not because the translation is wrong.
- **Custom-CA / self-signed clusters:** all CLIs accept `--ca-cert <path>` (env `OBS_MIGRATE_CA_CERT`) to verify against a private CA, or `--insecure` (env `OBS_MIGRATE_INSECURE`) to skip verification for testing only. These apply to source, Elasticsearch, Kibana, and the Node upload step.

## Step 3 — Do the side-by-side

1. **Read the verdict first:** `try_one_out/dashboards/migration_summary.md` — human-readable verdict, scorecard, per-panel table, and must-fix worklist for the one dashboard.
2. **Drill per panel:** `try_one_out/dashboards/migration_manifest.json` → `panels[].status`, `panels[].grafana_type` (Datadog: widget type), `panels[].reasons`, and `panels[].verification_packet.semantic_gate`.
3. **Open both dashboards:** the original in Grafana/Datadog and the uploaded one in Kibana, panel by panel. Compare numbers, series count, and shape.
4. **For any panel that looks wrong or empty in Kibana**, hand off to the `debug-uploaded-kibana-dashboard` skill — it captures the exact ES|QL Kibana is running and classifies the failure (real translator bug vs. missing data vs. Kibana visual mapping).

## Honest limits (tell the user)

- **Grafana single-dashboard = a one-file export.** There is no Grafana API id selector; scope by `--input-dir` containing only that dashboard.
- **Empty panels are often missing data, not a bug.** A clean translation can still render empty if the target cluster has no matching telemetry. Lighting panels up with synthetic data is a separate capability (the "preview with sample data" skill / engine flow); say so rather than blaming the translator.
- **No `--es-url` ⇒ no live validation.** Treat an offline run as directional; it does not prove panels render against real data.
- **Degrade gracefully:** unsupported panels are surfaced as `requires_manual` / `not_feasible` with reasons — relay those, never hide them.

## Do NOT

- Do **not** invent a Grafana API `--dashboard-ids` flag — it does not exist; use a single-dashboard files export.
- Do **not** claim panels are validated when the run had no `--es-url` and no upload smoke.
- Do **not** bulk-migrate here. Selecting many dashboards (by folder/tag/datasource/team) or migrating everything is the Phase D "migrate selected / every supported dashboard" skill.
- Do **not** cite manifest fields you have not confirmed (e.g. `grafana_type` exists on Grafana manifest panels; open the JSON when unsure).

## See also

- `scan-o11y-environment` skill — inventory of what exists (pick the dashboard to try from here).
- `assess-migration-readiness` skill — feasibility verdict and evidence level before trying.
- `debug-uploaded-kibana-dashboard` skill — diagnose a single broken uploaded panel.
- Revert: the dedicated "revert the migration" skill removes generated assets once it lands; until then, `obs-migrate cluster delete-dashboards --dashboard-ids <id>` clears the one uploaded dashboard.
- `obs-migrate upload --help`, `datadog-migrate --help`, `grafana-migrate --help` — confirm flags for the installed version.
- `docs/command-contract.md` — upload, cluster, and live-extraction flags and artifacts (online docs / repo).
