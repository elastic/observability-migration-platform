# Kibana Target Runtime

## Overview

The shared Kibana target runtime starts once a source adapter has produced
dashboard artifacts. For Grafana and Datadog that means a semantic
`DashboardIR` plus the derived native Dashboards API payload and on-disk YAML.
Every dashboard migration run also persists the native payload and the IR as
review artifacts (`dashboards/native/*.native.json`, `dashboards/ir/*.ir.json`)
so they can be inspected before upload -- see "Native Dashboard-as-Code Review
Artifacts" below. The target package provides YAML emission helpers, native
API mapping/upload, compile/upload functions, layout validation hooks, and
supporting utilities that other pipelines reuse.

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
| YAML emission helpers | `observability_migration/targets/kibana/emit/` | Shared panel/layout helpers used while adapters assemble IR/YAML |
| Display enrichment | `observability_migration/targets/kibana/emit/display.py` | Common panel display helpers |
| ES\|QL shape helpers | `observability_migration/targets/kibana/emit/esql_utils.py` | Field extraction and query-shape helpers |
| Registered target adapter | `observability_migration/targets/kibana/adapter.py` | Shared `TargetAdapter` for compile/upload/smoke/cluster orchestration |
| Compile / upload / layout validation | `observability_migration/targets/kibana/compile.py` | Resolves `kb-dashboard-cli` installed-first (uvx fallback); lint/layout run in-process; post-validation IR rebuild |
| Native Dashboards API mapping / upload | `observability_migration/targets/kibana/dashboards_api.py` | `native_dashboard_from_ir` / `native_dashboard_from_yaml`; default typed `PUT /api/dashboards/{id}` with per-dashboard legacy fallback |
| Native Dashboard-as-Code review artifacts | `observability_migration/targets/kibana/native_artifacts.py` | Persists the exact typed API payload (`native/*.native.json`) and semantic IR (`ir/*.ir.json`) for review before upload |
| Serverless API helpers | `observability_migration/targets/kibana/serverless.py` | Serverless-safe dashboard listing, data view CRUD, deletion workaround |
| Shared smoke validation | `observability_migration/targets/kibana/smoke.py` | Post-upload saved-object validation and browser audit |
| Unified compile / upload / cluster CLI | `observability_migration/app/cli.py` | `obs-migrate compile`, `obs-migrate upload`, `obs-migrate cluster` |
| Grafana query validation | `observability_migration/adapters/source/grafana/esql_validate.py` | Source-aware runtime validation against Elasticsearch |
| Grafana smoke wrapper | `observability_migration/adapters/source/grafana/validate_uploaded_dashboards.py` | Backward-compatible CLI surface for the shared smoke runtime |

## Shared Compile And Upload Flow

`observability_migration/targets/kibana/compile.py` exposes the shared runtime
functions:

- `compile_yaml()` and `compile_all()` compile dashboard YAML to NDJSON.
- `upload_yaml()` compiles and uploads a dashboard through `kb-dashboard-cli`
  (legacy path).
- `dashboards_api.native_dashboard_from_ir()` / `native_dashboard_from_yaml()`
  build the typed API payload; `upload_native_dashboard()` /
  `upload_yaml_files()` upsert with stable dashboard IDs, with caller-provided
  legacy fallback for rejected or empty dashboards.
- `native_artifacts.write_native_artifact()` / `write_ir_artifact()` persist
  that same typed API payload and its source `DashboardIR` to disk for
  review; `dashboards_api.upload_native_artifact()` deploys a persisted
  artifact later with no re-mapping and no legacy fallback.
- `lint_dashboard_yaml()` runs the in-process YAML lint gate
  (`observability_migration.targets.kibana.lint`).
- `validate_compiled_layout()` runs the in-process layout validator
  (`observability_migration.targets.kibana.layout`).
- `sync_result_queries_to_yaml()` rebuilds `DashboardIR` after post-validation
  query rewrites and re-derives both native payload and on-disk YAML.

The **default** upload path is the typed Dashboards API (no
`kb-dashboard-cli`). Compilation and the `--legacy-import` fallback shell out
to `kb-dashboard-cli`, resolved **installed-first**: if the console script is
on `PATH` (the `[kibana]` extra, installed via `pip install ".[kibana]"`, which
requires Python 3.12+) it is used directly; otherwise the runtime falls back to
a pinned `uvx --from kb-dashboard-cli==0.4.1 kb-dashboard-cli`. Lint and layout
validation run **in-process** inside the package and no longer shell out to
repo scripts.

```bash
# installed extra (3.12+):
kb-dashboard-cli compile --input-file dashboard.yaml --output-dir compiled/
# or via the pinned uvx fallback (3.11):
uvx --from kb-dashboard-cli==0.4.1 kb-dashboard-cli compile --input-file dashboard.yaml --output-dir compiled/
```

The native API path is the **default** on `obs-migrate upload`,
`obs-migrate migrate`, `grafana-migrate`, and `datadog-migrate`. Migrate
`--upload` prefers the in-memory `native_dashboard` already derived from
`DashboardIR` (the same payload persisted to `native/*.native.json`, see
below); standalone `obs-migrate upload --artifact-dir …` prefers that
persisted native artifact when present, else maps YAML through
`native_dashboard_from_yaml`. All three produce the same typed API panels
(sections, controls/`pinned_panels`, markdown, `links`, `image`, and all 11
ES\|QL visualization families) and use `PUT /api/dashboards/{id}` for
idempotent deploys. When
mapping from YAML, a dashboard that is rejected or contains no API-mappable
content falls back to the legacy `kb-dashboard-cli` import path; a rejected
*native* artifact does not, since there is no YAML to silently re-derive it
from (pass `--artifact-format yaml` explicitly for that fallback). Pass
`--legacy-import` to force the legacy compile+import path for every
dashboard; it always requires YAML (it forces `--artifact-format yaml`).

In short: native IR is the new source of truth for dashboard upload. The YAML,
compile, and saved-object import surfaces are compatibility paths for review,
linting, legacy automation, and explicit fallback workflows; they are not the
default deployment contract anymore.

### Native Dashboard-as-Code Review Artifacts

Every dashboard migration run persists two artifacts per dashboard, whether or
not `--upload` is passed, so the typed API payload can be reviewed before it
is ever sent to Kibana -- restoring the pre-typed-API "compile, inspect,
upload" workflow without reviving the legacy YAML-to-NDJSON compile step (see
`docs/architecture/asset-model.md`):

- `dashboards/native/<stem>.native.json` -- exactly `NativeDashboard.to_api_payload()`,
  wrapped in a small envelope (`kind`, `version`, `dashboard_id`, `title`,
  `payload`, `mapping`). This is what `obs-migrate upload --artifact-format
  native` sends to Kibana, unchanged.
- `dashboards/ir/<stem>.ir.json` -- the semantic `DashboardIR` both the native
  payload and the on-disk YAML are derived from.
- `dashboards/native/index.json` -- one row per dashboard in the run
  (`stem`, `title`, `dashboard_id`, `native_path`, `ir_path`).

Both `MigrationResult` (Grafana) and `DashboardResult` (Datadog) expose the
written paths as `native_artifact_path` / `ir_artifact_path`, and
`migration_manifest.json` includes them per dashboard.

### `obs-migrate upload` Input Shape

`obs-migrate upload` takes `--artifact-dir <path>`: the dashboard artifact
directory, or directly its `native/` or `yaml/` child. `--artifact-format`
(`auto` default, `native`, or `yaml`) picks the representation -- `auto`
prefers the reviewed native artifacts when present, else falls back to
mapping YAML through Kibana's typed Dashboards API. With `--legacy-import`
(which forces `--artifact-format yaml`), it instead recompiles every YAML
through the `kb-dashboard-cli` resolution path and imports the resulting
saved objects. The accepted shapes are:

- A directory containing `*.native.json` review artifacts directly (e.g. `migration_output/dashboards/native`).
- A directory containing `*.yaml` dashboard files directly (e.g. `migration_output/dashboards/yaml`).
- A dashboard artifacts directory that holds `native/` and/or `yaml/` subdirectories (e.g. `migration_output/dashboards`).
- The compiled sibling of a dashboard artifacts directory, because the command falls back to the sibling `yaml/` directory (e.g. `migration_output/dashboards/compiled`).

When `--artifact-format auto` sees both native artifacts and YAML under an
artifact root, it requires their stems to match exactly. A partial/mixed tree
(for example, one missing `*.native.json`) is rejected with
`mixed_native_yaml_artifacts` instead of uploading a silent subset. Point
directly at the `native/` directory for an intentional native-only subset, or
pass `--artifact-format yaml` when you intentionally want the YAML mapping path.

`--yaml-dir` remains accepted as a compatibility alias for `--artifact-dir ...
--artifact-format yaml`. The older `--compiled-dir` alias is still accepted
for backward compatibility and behaves identically to `--yaml-dir`, but its
name is misleading because NDJSON input is never consumed; prefer
`--artifact-dir`/`--yaml-dir` in new scripts.

### Dashboard Controls (ES|QL `pinned_panels`)

Source template variables become Kibana `pinned_panels` controls via
`map_yaml_control`. ES|QL controls carry a `variable_type`:

- **`values`** controls bind a value parameter (`WHERE field == ?var`). A
  query-driven control maps to `VALUES_FROM_QUERY`; a fixed option list maps to
  `STATIC_VALUES` (`available_options`).
- **`fields`** controls bind an *identifier* parameter (`STATS ... BY ??var`)
  for late-bound grouping (Grafana `by ($var)`, issue #282). The control's
  selectable dimensions are carried under `choices` in the YAML/IR control and
  map to a `STATIC_VALUES` ES|QL control with `variable_type: fields`. The lint
  gate requires a `??var` identifier to be bound specifically by a `fields`
  control — a same-named `values` control does **not** satisfy it, because a
  value is not a valid identifier for `STATS ... BY`. The converse is also
  enforced: a `fields` control does not bind a `?var` value parameter.

### `links` And `image` Panels

Beyond markdown and the 11 ES\|QL visualization families, the native mapper
(`map_yaml_panel` / `map_panel` in `targets/kibana/dashboards_api.py`) also
maps two non-query panel types, driven by the kb-dashboard-core YAML keys of
the same name:

- **`links`** -- a YAML `links: {layout, items: [...]}` block (each item
  either `url`-keyed or `dashboard`-keyed) maps to the native `type: "links"`
  panel, with `url` items becoming `externalLink`s and `dashboard` items
  becoming `dashboardLink`s (`new_tab`/`encode`/`with_filters`/`with_time` map
  onto the native `options` bag; `open_in_new_tab` is always emitted for URL
  links because the YAML and native API defaults differ). Grafana synthesizes
  one of these panels per dashboard from `dashboard.links[]` entries of type
  `"link"` with an absolute HTTP(S) URL and no inline Grafana variables -- see
  `adapters/source/grafana/links.py::build_links_panel`.
  Tag-driven `type: "dashboards"` links, relative URLs, and variable-bearing
  URLs have no safely resolvable destination at translation time and stay
  manual-navigation notes instead. Grafana `includeVars`/`keepTime` requests
  are preserved as explicit migration warnings because native external links
  cannot forward that context.
- **`image`** -- a YAML `image: {from_url, fit, background_color,
  description}` block maps to the native `type: "image"` panel
  (`image_config.src` as a `url` source; `fit` maps 1:1 onto
  `object_fit`). Datadog `image` widgets with a real absolute `http(s)` URL
  use this path (`adapters/source/datadog/planner.py::image_widget_rule`);
  relative/internal Datadog asset URLs still degrade to a markdown embed
  (link only, since Kibana cannot resolve them).

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
| `obs-migrate migrate --assets alerts --create-alert-rules ...` or `obs-migrate migrate --assets all --create-alert-rules ...` (also via the dedicated Grafana/Datadog source CLIs) | Canonical production path. Use `--assets alerts` for rules-only runs or `--assets all` when the same command should also migrate dashboards. | Both fully-automated and draft (review-required) translations are created **disabled** and tagged `obs-migration`; every created rule that is not positively fully-automated (draft today, plus any future review-required tier) additionally carries `obs-migration-review` so a successful translation always lands an inspectable rule rather than being silently skipped. Pass `--no-draft-alert-rules` to opt out of draft creation and create only fully-automated rules. Rules are skipped only when no faithful rule can be created — `manual_required` translations and payloads missing a `rule_type_id` — or when the alerting preflight is unreachable (in which case nothing is created). An `alert_rule_upload_results.json` / `monitor_rule_upload_results.json` summary is written to the output dir. Rules persist until you review and enable/delete them. |
| Legacy `--fetch-alerts` / `--fetch-monitors` compatibility aliases | Deprecated compatibility guidance for older scripts. Using the alias always emits a deprecation warning; if the requested asset selection is `dashboards`, including explicit `--assets dashboards`, runtime normalization upgrades the run to `--assets all`. | After normalization, the alias follows the same alert-rule creation path as the matching `--assets alerts` or `--assets all` run. |
| `scripts/verify_alert_rule_uploads.py` | Destructive round-trip verifier for test harnesses and CI. | Creates rules with a timestamped marker tag and **deletes them on exit** unless `--keep-rules` is passed. Useful to prove the emitted payloads would succeed without persisting anything. |

Under the hood both entry points share `observability_migration.targets.kibana.alerting.create_rules_from_payloads`, which runs the alerting preflight, skips payloads when the alerting stack is unreachable, skips `manual_required` tiers (controllable via its `creatable_tiers` argument), and records every skipped/failed rule in the returned summary.

Use `scripts/audit_migrated_rules.py` (or `cluster`-level queries against `GET /api/alerting/rules/_find`) to review migrated rules before enabling them. To list only the rules that still need review, filter on the `obs-migration-review` tag.

## Validation Boundaries

- **Pre-upload query validation** currently lives in source adapters because it needs source-aware query rewrite and manualization logic before compile/upload.
- **YAML lint and compiled-layout validation** are shared target checks and run through `targets/kibana/compile.py`.
- **Post-upload smoke validation** is now shared under `targets/kibana/smoke.py`, with a Grafana wrapper retained for backward-compatible CLI usage.

## Current Structural Gaps

- Source-aware emitted-query validation is still source-located because it depends on Grafana- and Datadog-specific query rewrite logic.
- Datadog now reuses the registered Kibana target adapter for compile/upload/smoke and emits first-class manifest/rollout artifacts. The remaining Datadog parity gap is broader source execution coverage beyond simple metric widgets.
- The shared target adapter does not yet own source-aware pre-upload fixup loops; those still sit at the source boundary.

## Notes By Source

- Grafana uses the full target path: emit, optional runtime validation, lint, optional compile (`--compile` or legacy import), optional upload, verification artifacts, and optional smoke merge.
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
