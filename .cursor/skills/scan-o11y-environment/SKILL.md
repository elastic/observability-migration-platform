---
name: scan-o11y-environment
description: Use when the user wants to scan, inventory, list, or take stock of their source environment, asks "what do I have / how many dashboards / what datasources / what panel types", or wants an overview before committing to a migration — produces an inventory of what exists in a connected Grafana or Datadog environment (dashboard/asset counts, panel/widget types, datasource distribution including non-migratable datasources, and folder organization). For a migrate/no-migrate verdict per asset, use assess-migration-readiness instead.
---

# Scan an o11y environment (inventory)

Goal: tell the user **what they have and what shape it is** — counts, types, datasources, organization — so they can decide scope. This is descriptive inventory, **not** a readiness verdict (that is the `assess-migration-readiness` skill).

## Core fact

There is **no standalone scan/inventory command.** Inventory is produced as a by-product of a **source-only migration run**: point it at the source, write to a throwaway output dir, do not upload, then read the report artifacts. No Elastic/Kibana target is required.

## Which command form to use (package vs. repo)

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. The output artifacts below are written by the CLI itself, so they exist for package users too — no `scripts/`, `infra/`, or `examples/` directory is needed.

## Run a source-only inventory pass

Grafana via live API (uses `--preflight` to also produce the datasource audit):

```bash
export GRAFANA_URL="https://grafana.example.com" GRAFANA_USER="..." GRAFANA_PASS="..."
obs-migrate migrate \
  --source grafana --input-mode api \
  --output-dir grafana_inventory \
  --assets all \
  --preflight
```

Datadog:

```bash
export DD_API_KEY="..." DD_APP_KEY="..." DD_SITE="datadoghq.com"
obs-migrate migrate \
  --source datadog --input-mode api \
  --output-dir datadog_inventory \
  --assets all
```

(If the user has exported dashboard JSON files instead of live API access, swap `--input-mode api` for `--input-mode files --input-dir <their-dashboards-dir>`.)

Use `--assets all` to inventory both dashboards and alerts/monitors; use `--assets dashboards` if you only care about dashboards. No `--upload`, `--es-url`, or `--kibana-url` is needed for inventory.

## Where to read the inventory

All paths are under `<output-dir>/dashboards/` unless noted. Field names below are the real keys emitted by the engine.

| What you want | File | Field(s) |
|---|---|---|
| Human-readable overview | `migration_summary.md` | verdict, scorecard, per-dashboard table |
| Dashboard count | `migration_report.json` | `summary.dashboards` |
| Panel count + per-panel type | `migration_manifest.json` | `summary.panels`; per panel `panels[].grafana_type` (Datadog: widget type) |
| Per-dashboard inventory (links, variables, annotations, rows, panels, folder) | `migration_manifest.json` | `dashboards[].inventory` (`links`, `annotations`, `variables`, `rows`, `panels`, `folder_title`) |
| Datasource distribution (Grafana, needs `--preflight`) | `preflight_report.json` | `datasource_audit.datasource_types`, `datasource_audit.datasource_details` |
| Datasources that **cannot** migrate | `preflight_report.json` | `datasource_audit.non_migratable`, `datasource_audit.non_migratable_panels` |
| Run scope (which asset families ran) | `<output-dir>/run_summary.json` | top-level summary |

Non-migratable datasources flagged today include InfluxDB, MySQL/Postgres/MSSQL, Graphite, CloudWatch, Stackdriver, OpenTSDB, and the trace backends (Tempo/Jaeger/Zipkin). Migratable: Prometheus, Loki, Elasticsearch.

## Honest limits (tell the user)

- **Grafana API extraction is capped at 500 dashboards** per search request.
- **Dashboard tags are not summarized** in these artifacts today — only folder grouping (`folder_title`) is indexed. If the user asks for a tag breakdown, say it is not currently produced.
- The datasource audit is a Grafana-side artifact gated behind `--preflight`; Datadog inventory comes from the manifest/report rather than a datasource audit.

## Do NOT

- Do **not** claim there is a dedicated `scan`/`inventory` subcommand.
- Do **not** cite fields you have not confirmed exist (e.g. there is `grafana_type`, not a guaranteed `kibana_type`, on manifest panels; folder is `folder_title`). When unsure of a field, open the JSON and check.
- Do **not** turn this into a readiness/feasibility verdict — route that to `assess-migration-readiness`.

## See also

- `assess-migration-readiness` skill — what will vs. won't migrate.
- `obs-migrate migrate --help` — confirm `--preflight` and asset flags for the installed version.
- `docs/command-contract.md` — artifact descriptions (online docs / repo).
