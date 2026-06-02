# Kibana Target Runtime

## Overview

The shared Kibana target runtime starts once a source adapter has produced
dashboard YAML. It provides YAML emission helpers, compile/upload functions,
layout validation hooks, and supporting utilities that other pipelines reuse.

Today the shared target code lives in `observability_migration/targets/kibana/`.
It now includes the registered Kibana `TargetAdapter`, shared compile/upload
entry points, and the shared post-upload smoke runtime. Source-aware emitted
query validation still remains in source adapters because it needs
source-specific rewrite logic.

For a current survey of Kibana ES|QL commands, functions, editor behavior, and
translation-relevant opportunities, see `docs/targets/kibana-esql-capabilities.md`.
For the concrete implementation follow-up in this repo, see
`docs/targets/kibana-esql-upgrade-matrix.md`.

## Current Module Map

| Responsibility | Primary location | Notes |
|---|---|---|
| YAML emission | `observability_migration/targets/kibana/emit/` | Shared by all sources |
| Display enrichment | `observability_migration/targets/kibana/emit/display.py` | Common panel display helpers |
| ES\|QL shape helpers | `observability_migration/targets/kibana/emit/esql_utils.py` | Field extraction and query-shape helpers |
| Registered target adapter | `observability_migration/targets/kibana/adapter.py` | Shared `TargetAdapter` for compile/upload/smoke/cluster orchestration |
| Compile / upload / layout validation | `observability_migration/targets/kibana/compile.py` | Resolves `kb-dashboard-cli` installed-first (uvx fallback); lint/layout run in-process |
| Serverless API helpers | `observability_migration/targets/kibana/serverless.py` | Serverless-safe dashboard listing, data view CRUD, deletion workaround |
| Shared smoke validation | `observability_migration/targets/kibana/smoke.py` | Post-upload saved-object validation and browser audit |
| Unified compile / upload / cluster CLI | `observability_migration/app/cli.py` | `obs-migrate compile`, `obs-migrate upload`, `obs-migrate cluster` |
| Grafana query validation | `observability_migration/adapters/source/grafana/esql_validate.py` | Source-aware runtime validation against Elasticsearch |
| Grafana smoke wrapper | `observability_migration/adapters/source/grafana/validate_uploaded_dashboards.py` | Backward-compatible CLI surface for the shared smoke runtime |

## Shared Compile And Upload Flow

`observability_migration/targets/kibana/compile.py` exposes the shared runtime
functions:

- `compile_yaml()` and `compile_all()` compile dashboard YAML to NDJSON.
- `upload_yaml()` compiles and uploads a dashboard through `kb-dashboard-cli`.
- `lint_dashboard_yaml()` runs the in-process YAML lint gate
  (`observability_migration.targets.kibana.lint`).
- `validate_compiled_layout()` runs the in-process layout validator
  (`observability_migration.targets.kibana.layout`).
- `sync_result_queries_to_yaml()` keeps emitted YAML aligned with post-validation query rewrites.

Compilation and upload shell out to `kb-dashboard-cli`, resolved
**installed-first**: if the console script is on `PATH` (the `[kibana]` extra,
installed via `pip install ".[kibana]"`, which requires Python 3.12+) it is
used directly; otherwise the runtime falls back to a pinned
`uvx --from kb-dashboard-cli==<version> kb-dashboard-cli`. Lint and layout
validation now run **in-process** inside the package and no longer shell out to
repo scripts.

```bash
# installed extra (3.12+):
kb-dashboard-cli compile --input-file dashboard.yaml --output-dir compiled/
# or via the pinned uvx fallback (3.11):
uvx kb-dashboard-cli compile --input-file dashboard.yaml --output-dir compiled/
```

### `obs-migrate upload` Input Shape

`obs-migrate upload` expects a directory of **dashboard YAML files**. Internally
it recompiles each YAML through `uvx kb-dashboard-cli compile --upload`, which
means the upload step does not consume the NDJSON written by
`obs-migrate compile`. The accepted shapes are:

- A directory containing `*.yaml` dashboard files directly (e.g. `migration_output/dashboards/yaml`).
- A dashboard artifacts directory that holds a `yaml/` subdirectory (e.g. `migration_output/dashboards`).
- The compiled sibling of a dashboard artifacts directory, because the command falls back to the sibling `yaml/` directory (e.g. `migration_output/dashboards/compiled`).

Prefer `--yaml-dir` in new scripts. The legacy alias `--compiled-dir` is still
accepted for backward compatibility and behaves identically, but its name is
misleading because NDJSON input is never consumed.

## Command Coverage

Compile/upload/cluster command examples are centralized in `docs/command-contract.md`.

Use that doc for:
- `obs-migrate compile`
- `obs-migrate upload`
- `obs-migrate cluster ...`
- source-specific smoke command examples

## Alert Rule Creation

Three entry points create Kibana alerting rules via `POST /api/alerting/rule`:

| Entry point | When to use | Behavior |
|---|---|---|
| `obs-migrate migrate --assets alerts --create-alert-rules ...` or `obs-migrate migrate --assets all --create-alert-rules ...` (also via the dedicated Grafana/Datadog source CLIs) | Canonical production path. Use `--assets alerts` for rules-only runs or `--assets all` when the same command should also migrate dashboards. | Rules are created **disabled**, tagged `obs-migration`, and an `alert_rule_upload_results.json` / `monitor_rule_upload_results.json` summary is written to the output dir. Rules persist until you review and enable/delete them. |
| Legacy `--fetch-alerts` / `--fetch-monitors` compatibility aliases | Deprecated compatibility guidance for older scripts. Using the alias always emits a deprecation warning; if the requested asset selection is `dashboards`, including explicit `--assets dashboards`, runtime normalization upgrades the run to `--assets all`. | After normalization, the alias follows the same alert-rule creation path as the matching `--assets alerts` or `--assets all` run. |
| `scripts/verify_alert_rule_uploads.py` | Destructive round-trip verifier for test harnesses and CI. | Creates rules with a timestamped marker tag and **deletes them on exit** unless `--keep-rules` is passed. Useful to prove the emitted payloads would succeed without persisting anything. |

Under the hood both entry points share `observability_migration.targets.kibana.alerting.create_rules_from_payloads`, which runs the alerting preflight, skips payloads when the alerting stack is unreachable, and records every skipped/failed rule in the returned summary.

Use `scripts/audit_migrated_rules.py` (or `cluster`-level queries against `GET /api/alerting/rules/_find`) to review migrated rules before enabling them.

## Validation Boundaries

- **Pre-upload query validation** currently lives in source adapters because it needs source-aware query rewrite and manualization logic before compile/upload.
- **YAML lint and compiled-layout validation** are shared target checks and run through `targets/kibana/compile.py`.
- **Post-upload smoke validation** is now shared under `targets/kibana/smoke.py`, with a Grafana wrapper retained for backward-compatible CLI usage.

## Current Structural Gaps

- Source-aware emitted-query validation is still source-located because it depends on Grafana- and Datadog-specific query rewrite logic.
- Datadog now reuses the registered Kibana target adapter for compile/upload/smoke and emits first-class manifest/rollout artifacts. The remaining Datadog parity gap is broader source execution coverage beyond simple metric widgets.
- The shared target adapter does not yet own source-aware pre-upload fixup loops; those still sit at the source boundary.

## Notes By Source

- Grafana uses the full target path: emit, optional runtime validation, lint, compile, optional upload, verification artifacts, and optional smoke merge.
- Datadog reuses shared YAML emission, optional compile (`--compile`), first-class dedicated upload (`--upload`), shared smoke validation (`--smoke`), manifest/rollout artifacts, and verification packets. Preflight is first-class (`--preflight` with capability-aware field checks when `--es-url` is provided), while source-aware query validation remains Datadog-located because it can rewrite emitted queries safely before compile/upload.

## Elastic Serverless Compatibility

Elastic Serverless Kibana restricts saved-object management to two endpoints:

| Operation | API Endpoint | Available? |
|---|---|---|
| List / export dashboards | `POST /api/saved_objects/_export` | Yes |
| Import / upload dashboards | `POST /api/saved_objects/_import` | Yes (with `overwrite`) |
| Get individual saved object | `GET /api/saved_objects/{type}/{id}` | **No** (400) |
| Find saved objects | `GET /api/saved_objects/_find` | **No** (400) |
| Delete saved object | `DELETE /api/saved_objects/{type}/{id}` | **No** (400) |
| Bulk delete saved objects | `POST /api/saved_objects/_bulk_delete` | **No** (400) |

Data view management has full CRUD:

| Operation | API Endpoint | Available? |
|---|---|---|
| List data views | `GET /api/data_views` | Yes |
| Create data view | `POST /api/data_views/data_view` | Yes |
| Get data view | `GET /api/data_views/data_view/{id}` | Yes |
| Update data view | `POST /api/data_views/data_view/{id}` | Yes |
| Delete data view | `DELETE /api/data_views/data_view/{id}` | Yes |
| Runtime fields | Full CRUD | Yes |

### Workarounds

- **Dashboard listing**: Uses `_export` with `type: ["dashboard"]` (auto-fallback from `_find`).
- **Individual dashboard fetch**: Falls back to `_export` with `objects: [{type: "dashboard", id: "..."}]`.
- **Dashboard deletion**: Re-imports with empty content and `[DELETED]` title prefix. The object remains but is harmless. Full removal requires the Kibana UI.
- **Data views**: Use `--ensure-data-views` on the dedicated source CLIs or `obs-migrate cluster ensure-data-views` for shared target management.

### CLI Surfaces

Use `obs-migrate cluster ...` for shared target-management operations.

Dedicated source CLIs (`grafana-migrate`, `datadog-migrate`) still expose:

```
--list-dashboards          List dashboards in target Kibana and exit
--delete-dashboards IDS    Comma-separated dashboard IDs to clear
--ensure-data-views        Auto-create required data views before upload
```

Unified `obs-migrate migrate` no longer exposes those shortcuts. Use the
dedicated `cluster` subcommand instead:

```bash
obs-migrate cluster list-dashboards    --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
obs-migrate cluster ensure-data-views  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --data-view-patterns "metrics-*,logs-*"
obs-migrate cluster delete-dashboards  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --dashboard-ids "id1,id2"
obs-migrate cluster detect-serverless  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
```

## Location

Shared target package: `observability_migration/targets/kibana/`
