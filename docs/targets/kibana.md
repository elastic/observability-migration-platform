# Kibana Target Runtime

## Overview

The shared Kibana target runtime starts once a source adapter has produced
dashboard artifacts. For Grafana and Datadog that means a semantic
`DashboardIR` plus the native Dashboards API payload derived from it. Every
dashboard migration run persists both as review artifacts
(`dashboards/native/*.native.json`, `dashboards/ir/*.ir.json`) so they can be
inspected before upload -- see "Native Dashboard-as-Code Review Artifacts"
below. The target package provides panel/layout emission helpers, native API
mapping and upload, and supporting utilities that other pipelines reuse.

**Dashboard YAML has been removed.** Nothing in the engine renders, writes,
lints, or compiles a kb-dashboard YAML document, and nothing shells out to
`kb-dashboard-cli` or `kb-dashboard-lint`. There is exactly one deploy path:
the typed Kibana Dashboards API (`PUT /api/dashboards/{id}`). See "Removed:
the dashboard-YAML surfaces" in `docs/command-contract.md` for the
operator-facing list of removed flags and their replacements.

Today the shared target code lives in `observability_migration/targets/kibana/`.
It now includes the registered Kibana `TargetAdapter`, the shared upload entry
points, and the shared post-upload smoke runtime. Source-aware emitted
query validation still remains in source adapters because it needs
source-specific rewrite logic.

## Current Module Map

| Responsibility | Primary location | Notes |
|---|---|---|
| Panel/layout emission helpers | `observability_migration/targets/kibana/emit/` | Shared panel/layout helpers used while adapters assemble the `DashboardIR` |
| Display enrichment | `observability_migration/targets/kibana/emit/display.py` | Common panel display helpers |
| ES\|QL shape helpers | `observability_migration/targets/kibana/emit/esql_utils.py` | Field extraction and query-shape helpers |
| Registered target adapter | `observability_migration/targets/kibana/adapter.py` | Shared `TargetAdapter` for upload/smoke/cluster orchestration |
| Space-URL helpers + post-validation IR sync | `observability_migration/targets/kibana/compile.py` | `detect_space_id_from_kibana_url`, `kibana_url_for_space`, `sync_result_queries_to_ir` (the YAML render/compile/legacy-upload functions were removed) |
| Native Dashboards API mapping / upload | `observability_migration/targets/kibana/dashboards_api.py` | `native_dashboard_from_ir`; typed `PUT /api/dashboards/{id}`, no fallback renderer |
| Native Dashboard-as-Code review artifacts | `observability_migration/targets/kibana/native_artifacts.py` | Persists the exact typed API payload (`native/*.native.json`) and semantic IR (`ir/*.ir.json`) for review before upload |
| Serverless API helpers | `observability_migration/targets/kibana/serverless.py` | Serverless-safe dashboard listing, data view CRUD, deletion workaround |
| Shared smoke validation | `observability_migration/targets/kibana/smoke.py` | Post-upload saved-object validation and browser audit |
| Render audit | `observability_migration/targets/kibana/render_audit*.py` | Default-state panel render truth in real Kibana |
| Interaction audit | `observability_migration/targets/kibana/interaction_*.py` | Playwright control selection + affected-query evidence (9.5+) |
| Unified upload / cluster CLI | `observability_migration/app/cli.py` | `obs-migrate upload`, `obs-migrate cluster` (`obs-migrate compile` was removed) |
| Grafana query validation | `observability_migration/adapters/source/grafana/esql_validate.py` | Source-aware runtime validation against Elasticsearch |
| Grafana smoke wrapper | `observability_migration/adapters/source/grafana/validate_uploaded_dashboards.py` | Backward-compatible CLI surface for the shared smoke runtime |

## Shared Upload Flow

`observability_migration/targets/kibana/compile.py` (kept under its historical
name) exposes the shared runtime functions that survived the YAML removal:

- `detect_space_id_from_kibana_url()` / `kibana_url_for_space()` resolve the
  Kibana space to deploy into.
- `sync_result_queries_to_ir()` rebuilds `DashboardIR` after post-validation
  query rewrites and re-derives the native payload from it. Nothing is written
  to disk here. The rebuild goes through the internal **in-memory dict shape**
  (`DashboardIR.to_yaml_dict()`, no file involved), which can only carry
  `compile.YAML_ROUND_TRIPPED_IR_FIELDS`, so every other IR field --
  dashboard identity (`uid`/`folder`/`tags`), lineage and the referenced asset
  collections -- is carried across from the pre-rebuild IR by
  `carry_over_non_yaml_ir_fields()`. Adding a `DashboardIR` field means
  classifying it in `compile.YAML_ROUND_TRIPPED_IR_FIELDS` or
  `compile.IR_FIELDS_CARRIED_ACROSS_YAML_REBUILD`; an exhaustiveness test
  fails until you do.

Removed from this module with the YAML surface: `dashboard_yaml_text`,
`write_dashboard_yaml`, `compile_yaml`, `compile_all`, `upload_yaml`, and the
`lint_dashboard_yaml` / `validate_compiled_layout` wrappers. The `*_yaml_*`
names that remain (`YAML_ROUND_TRIPPED_IR_FIELDS`,
`carry_over_non_yaml_ir_fields`) describe that internal dict shape, **not** a
file format.

Upload itself lives in `dashboards_api.py` and `adapter.py`:

- `dashboards_api.native_dashboard_from_ir()` builds the typed API payload from
  a `DashboardIR`; `upload_native_dashboard()` upserts it with a stable
  dashboard ID.
- `native_artifacts.write_native_artifact()` / `write_ir_artifact()` persist
  that same typed API payload and its source `DashboardIR` to disk for
  review; `dashboards_api.upload_native_artifact()` deploys a persisted
  artifact later with no re-mapping and no fallback renderer.
- `dashboards_api.iter_payload_leaf_panels()` and `payload_panel_queries()`
  read a built payload back: leaf panels (recursing one level into sections)
  and the ES|QL each one carries, including per-layer queries on an `xy` panel.
  They are what the structural guard in `tests/native_payload_guard.py` uses to
  check the shipped payload against the IR it came from.
- `native_dashboard_from_yaml()`, `build_dashboard_payload_from_yaml()`,
  `build_payload_from_yaml()`, `map_yaml_panel()`, `map_yaml_control()`, and
  `map_yaml_filters()` are retained, but despite their names they map the
  internal dict shape produced by `DashboardIR.to_yaml_dict()` -- they never
  read a YAML file. Treat them as the dict-shape mapper.

There is one upload path on `obs-migrate upload`, `obs-migrate migrate`,
`grafana-migrate`, and `datadog-migrate`: the typed Dashboards API. Migrate
`--upload` uploads the in-memory `native_dashboard` already derived from
`DashboardIR` (the same payload persisted to `native/*.native.json`, see
below); standalone `obs-migrate upload --artifact-dir …` uploads that persisted
native artifact byte-for-byte. Both produce the same typed API panels
(sections, controls/`pinned_panels`, markdown, `links`, `image`, and all 11
ES\|QL visualization families) and use `PUT /api/dashboards/{id}` for
idempotent deploys. A rejected payload is a **terminal failure** -- there is no
second renderer to silently re-derive the dashboard from, and the
`--legacy-import` compile+`_import` fallback was removed.

An accepted (2xx) upload is not automatically a clean one: Kibana drops panels it
cannot transform and still answers `200`, with no `warnings` key on the PUT body.
`_record_panel_loss` therefore compares the leaf panels sent against the ones the
response echoes in `data.panels` and downgrades the result to status `lossy` when
fewer came back, then issues a single follow-up `GET /api/dashboards/{id}` — only on
a detected mismatch — to attach Kibana's own `warnings[].message` to each dropped
panel. `lossy` is a failure (it never counts as `uploaded_ok`) and, like `conflict`,
is terminal. See `docs/command-contract.md` for the operator-facing contract.

#### Dashboard ids and title collisions

`PUT /api/dashboards/{id}` is an *upsert*, so the dashboard id is what decides
whether an upload creates a dashboard or replaces one. The id is the slug of the
dashboard title (`obs-migrate-<title-slug>`), which keeps re-migrating the same
dashboard idempotent, and which is why the derivation is not changed lightly:
every already-uploaded dashboard is addressed by it.

A title slug alone is not unique. Two source dashboards with the same title
resolve to one id, so the second upload replaces the first and reports a routine
`updated`. `DashboardIR.id_disambiguator` closes that: when a run allocates a
dashboard's artifact stem against a title already taken, the token that made the
*stem* unique is also appended to the dashboard id, so artifact
`shared_title_dash-beta` carries id `obs-migrate-shared-title-dash-beta`. The
token is empty for a unique title, so unique-titled dashboards keep exactly the
id they have always had. Every disambiguation is printed in the migrate output,
naming both the id used and the plain title slug it is no longer.

Behind that, the upload keeps a per-run ledger of the ids it has written. A
second payload resolving to an id the run already uploaded is **not** sent: it is
reported with status `duplicate_id`, which is a failure on the same terms as
`lossy` (never `uploaded_ok`, fails the exit code). A loud stop is preferable to
a run that claims two dashboards and leaves one.

#### Control data views

A control's `data_view_id` starts life as an index pattern (`metrics-*`), which
upload rewrites to the saved-object id Kibana assigned that pattern. Ensuring the
patterns the payload references is what makes that lookup complete; when a value
still cannot be resolved, the raw pattern is left in place and Kibana renders the
control as "An error occurred". Those controls are reported per control on
`UploadResult.unresolved_data_views` (and in the upload record), and warned about
by name. Two fallbacks are correct and stay quiet: a value that is already a real
saved-object id, and a data view whose title is its own id. Before warning, the
adapter re-checks against every data view in the space, so a data view the
operator created but this upload had no reason to ensure is not reported.

Both upload entry points ensure the patterns their payload actually references,
on top of the `metrics-prometheus-*` / `metrics-*` / `logs-*` floor:
`upload_dashboard` (the migrate pipeline) from the in-memory payload, and
`upload` (`obs-migrate upload --artifact-dir`) from the union across the whole
batch of `native/*.native.json` — one ensure round-trip for the batch, not one
per artifact. A batch that references nothing beyond the defaults issues exactly
the request it always did. Without this, a reviewed artifact naming
`metrics-*.prometheus-*` (the Datadog `prometheus_native` profile) had no data
view to resolve against and shipped carrying the raw pattern.

If a referenced pattern cannot be ensured — a bad pattern, a missing privilege,
any target error — the pattern is retried on its own so the failure is
attributed to it rather than costing the batch its other data views, the
target's reason is printed once, and every artifact that references that pattern
is reported with status `data_view_unavailable`: not `uploaded_ok`, exit `1`, the
reason carried in the upload record's `output` and in the summary's
`data_views_unavailable`. Same discipline as `lossy` — the upload happened, the
result is knowably incomplete, and a run that shipped a control which renders an
error does not get to exit `0`.

In short: `DashboardIR` is the source of truth, `native/*.native.json` is the
artifact, and the typed Dashboards API is the only deployment contract. The
YAML, compile, and saved-object import surfaces no longer exist.

### Native Dashboard-as-Code Review Artifacts

Every dashboard migration run persists two artifacts per dashboard, whether or
not `--upload` is passed, so the typed API payload can be reviewed before it
is ever sent to Kibana. This is the "generate, inspect, upload" workflow with
no compile step at all (see `docs/architecture/asset-model.md`):

- `dashboards/native/<stem>.native.json` -- exactly `NativeDashboard.to_api_payload()`,
  wrapped in a small envelope (`kind`, `version`, `dashboard_id`, `title`,
  `payload`, `mapping`). This is what `obs-migrate upload --artifact-dir` sends
  to Kibana, unchanged.
- `dashboards/ir/<stem>.ir.json` -- the semantic `DashboardIR` the native
  payload is derived from.
- `dashboards/native/index.json` -- one row per dashboard in the run
  (`stem`, `title`, `dashboard_id`, `native_path`, `ir_path`).

These are the **only** dashboard artifacts a run writes: there is no
`dashboards/yaml/` and no `dashboards/compiled/`. A `yaml/` or `compiled/`
directory left behind by an older release is swept on the next run, so stale
artifacts cannot sit next to fresh `native/` ones.

Both `MigrationResult` (Grafana) and `DashboardResult` (Datadog) expose the
written paths as `native_artifact_path` / `ir_artifact_path`, and
`migration_manifest.json` includes them per dashboard.

### `obs-migrate upload` Input Shape

`obs-migrate upload` takes exactly one input: `--artifact-dir <path>`
(required). Every `native/*.native.json` under it is sent to
`PUT /api/dashboards/{id}` byte-for-byte -- no re-mapping, no second renderer,
no fallback. The accepted shapes are:

- A directory containing `*.native.json` review artifacts directly (e.g. `migration_output/dashboards/native`).
- A dashboard artifacts directory that holds `native/` (e.g. `migration_output/dashboards`).
- Any sibling child of the artifact root, which still resolves to `../native/`.

If no `native/*.native.json` is found, `upload` exits `1` and says so rather
than uploading nothing quietly.

**Removed input flags.** These now exit `2` with a message naming the
replacement rather than being silently ignored:

| Removed flag | Replacement |
|---|---|
| `--yaml-dir <dir>` | `--artifact-dir <dashboards>` |
| `--compiled-dir <dir>` | `--artifact-dir <dashboards>` |
| `--artifact-format {auto,native,yaml}` | Drop it. There is one format: the native payload under `native/`. |
| `--legacy-import` | Drop it. Uploads go through the typed Dashboards API. |

The `obs-migrate compile` subcommand is removed as well; nothing consumes
NDJSON. See `docs/command-contract.md` for the full removal table.

### Dashboard Controls (ES|QL `pinned_panels`)

Source template variables become Kibana `pinned_panels` controls via
`map_yaml_control`. ES|QL controls carry a `variable_type`:

- **`values`** controls bind a value parameter (`WHERE field == ?var`). A
  query-driven control maps to `VALUES_FROM_QUERY`; a fixed option list maps to
  `STATIC_VALUES` (`available_options`).
- **`fields`** controls bind an *identifier* parameter (`STATS ... BY ??var`)
  for late-bound grouping (Grafana `by ($var)`, issue #282). The control's
  selectable dimensions are carried under `choices` on the IR control and
  map to a `STATIC_VALUES` ES|QL control with `variable_type: fields`.
  `lint.unbound_param_findings` requires a `??var` identifier to be bound
  specifically by a `fields` control — a same-named `values` control does
  **not** satisfy it, because a value is not a valid identifier for
  `STATS ... BY`. The converse is also enforced: a `fields` control does not
  bind a `?var` value parameter. That check reads the in-memory dict shape (or
  one rebuilt from `ir/*.ir.json`), so it survived the YAML removal and is
  still applied by the interaction audit.

### `links` And `image` Panels

Beyond markdown and the 11 ES\|QL visualization families, the native mapper
(`map_yaml_panel` / `map_panel` in `targets/kibana/dashboards_api.py`) also
maps two non-query panel types, driven by the internal dict-shape keys of
the same name (named after the kb-dashboard-core schema they descend from):

- **`links`** -- a `links: {layout, items: [...]}` block (each item
  either `url`-keyed or `dashboard`-keyed) maps to the native `type: "links"`
  panel, with `url` items becoming `externalLink`s and `dashboard` items
  becoming `dashboardLink`s (`new_tab`/`encode`/`with_filters`/`with_time` map
  onto the native `options` bag; `open_in_new_tab` is always emitted for URL
  links because the dict-shape and native API defaults differ). Grafana synthesizes
  one of these panels per dashboard from `dashboard.links[]` entries of type
  `"link"` with an absolute HTTP(S) URL and no inline Grafana variables -- see
  `adapters/source/grafana/links.py::build_links_panel`.
  Tag-driven `type: "dashboards"` links, relative URLs, and variable-bearing
  URLs have no safely resolvable destination at translation time and stay
  manual-navigation notes instead. Grafana `includeVars`/`keepTime` requests
  are preserved as explicit migration warnings because native external links
  cannot forward that context.
- **`image`** -- an `image: {from_url, fit, background_color,
  description}` block maps to the native `type: "image"` panel
  (`image_config.src` as a `url` source; `fit` maps 1:1 onto
  `object_fit`). Datadog `image` widgets with a real absolute `http(s)` URL
  use this path (`adapters/source/datadog/planner.py::image_widget_rule`);
  relative/internal Datadog asset URLs still degrade to a markdown embed
  (link only, since Kibana cannot resolve them).

## Command Coverage

Upload/cluster command examples are centralized in `docs/command-contract.md`.

Use that doc for:
- `obs-migrate upload`
- `obs-migrate cluster ...`
- source-specific smoke command examples
- the full list of removed dashboard-YAML surfaces (`obs-migrate compile`,
  `--yaml-dir`, `--compiled-dir`, `--artifact-format`, `--legacy-import`,
  `--compile`/`--no-compile`) and their replacements

## Alert Rule Creation

Three entry points create Kibana alerting rules via `POST /api/alerting/rule`:

| Entry point | When to use | Behavior |
|---|---|---|
| `obs-migrate migrate --assets alerts --create-alert-rules ...` or `obs-migrate migrate --assets all --create-alert-rules ...` (also via the dedicated Grafana/Datadog source CLIs) | Canonical production path. Use `--assets alerts` for rules-only runs or `--assets all` when the same command should also migrate dashboards. | Both fully-automated and draft (review-required) translations are created **disabled** and tagged `obs-migration`; every created rule that is not positively fully-automated (draft today, plus any future review-required tier) additionally carries `obs-migration-review` so a successful translation always lands an inspectable rule rather than being silently skipped. Pass `--no-draft-alert-rules` to opt out of draft creation and create only fully-automated rules. Rules are skipped only when no faithful rule can be created — `manual_required` translations and payloads missing a `rule_type_id`. An `alert_rule_upload_results.json` / `monitor_rule_upload_results.json` summary is written to the output dir. Rules persist until you review and enable/delete them. If creation was requested but never happened at all — no `--kibana-api-key`, or an unreachable alerting preflight — the run **exits non-zero** and records the reason under `alerts.rule_creation` in `run_summary.json`, so asking for rules and getting none can no longer look like a clean run. A missing `--kibana-url` or a non-alert `--assets` is rejected up front with exit `2`. |
| Legacy `--fetch-alerts` / `--fetch-monitors` compatibility aliases | Deprecated compatibility guidance for older scripts. Using the alias always emits a deprecation warning; if the requested asset selection is `dashboards`, including explicit `--assets dashboards`, runtime normalization upgrades the run to `--assets all`. | After normalization, the alias follows the same alert-rule creation path as the matching `--assets alerts` or `--assets all` run. |
| `scripts/verify_alert_rule_uploads.py` | Destructive round-trip verifier for test harnesses and CI. | Creates rules with a timestamped marker tag and **deletes them on exit** unless `--keep-rules` is passed. Useful to prove the emitted payloads would succeed without persisting anything. |

Under the hood both entry points share `observability_migration.targets.kibana.alerting.create_rules_from_payloads`, which runs the alerting preflight, skips payloads when the alerting stack is unreachable, skips `manual_required` tiers (controllable via its `creatable_tiers` argument), and records every skipped/failed rule in the returned summary.

Use `scripts/audit_migrated_rules.py` (or `cluster`-level queries against `GET /api/alerting/rules/_find`) to review migrated rules before enabling them. To list only the rules that still need review, filter on the `obs-migration-review` tag.

## Validation Boundaries

- **Pre-upload query validation** currently lives in source adapters because it needs source-aware query rewrite and manualization logic before upload.
- **The YAML lint stage and the compiled-layout validation stage are gone.** `targets/kibana/lint.py` and `targets/kibana/layout.py` are retained as library code — `lint.py` also hosts `unbound_param_findings`, which the interaction audit uses — but no user-facing command calls `lint_dashboard_yaml` or `validate_compiled_layout` any more.
- **Post-upload smoke validation** is now shared under `targets/kibana/smoke.py`, with a Grafana wrapper retained for backward-compatible CLI usage.
- **Structural payload guards** live in `tests/native_payload_guard.py`: `assert_payload_matches_ir` (the load-bearing check — the shipped payload versus the `DashboardIR` it was built from) and `assert_payload_matches_dict_shape_bridge` (a second construction through the in-memory dict shape, which pins the dashboard-level derivations). Neither reads or writes YAML.

### Payload Fields Kibana Does Not Store

The typed Dashboards API accepts some fields it then discards. A field that is
accepted-and-dropped is worse than useless: it looks like fidelity in the
payload, is invisible in Kibana, and shows up as a false divergence in any
PUT-then-GET round-trip check. The emitter therefore does not send them.

- **`data_table` metric colour is never emitted.** No colour shape survives a
  table metric. Probed live on 9.5 against a throwaway dashboard: a multi-step
  `dynamic` palette returns HTTP 200 but is stored as `color: null`; `dynamic`
  with `apply_to: cell`/`text` is rejected with HTTP 400; `dynamic` with
  `range: percentage` is accepted and not persisted; `static` is rejected;
  `auto` is accepted and not persisted. When the source *did* carry conditional
  formatting (e.g. a Datadog conditional format on a table column) this is a
  real fidelity loss, so it is recorded rather than silently dropped: the
  mapper counts `dropped_unsupported_datatable_metric_color` into
  `NativeMappingCounts.reasons` — the same channel dropped dashboard filters
  use — which lands in `native/<stem>.native.json` under `mapping.reasons` and
  is rendered as an `--upload` warning naming the column count and telling the
  operator to restyle those columns in Kibana. Table *row* colours are
  unaffected: those do persist as a categorical/gradient colour mapping.
- **An inert second y-axis is not emitted.** Datadog XY widgets declare all
  three axes with a hidden title whether or not any series uses the right axis;
  a `config.axis.y2` that styles an axis nothing is plotted on is discarded by
  Kibana. It is suppressed only when no series targets the right axis *and* the
  `y2` block carries nothing of its own — a panel that really does plot on the
  right axis keeps its `y2`, hidden title included.
- **An empty axis title text is not emitted.** `title: {"text": "", ...}` names
  nothing and Kibana stores no `text` for it; only the `visible` request is
  sent.

## Current Structural Gaps

- Source-aware emitted-query validation is still source-located because it depends on Grafana- and Datadog-specific query rewrite logic.
- Datadog now reuses the registered Kibana target adapter for upload/smoke and emits first-class manifest/rollout artifacts. The remaining Datadog parity gap is broader source execution coverage beyond simple metric widgets.
- The shared target adapter does not yet own source-aware pre-upload fixup loops; those still sit at the source boundary.

## Notes By Source

- Grafana uses the full target path: translate to `DashboardIR`, optional runtime validation, native review artifacts, optional upload, verification artifacts, and optional smoke merge. Its console pipeline is 5 stages (`[1/5]` extract, `[2/5]` translate, `[3/5]` verification-packet ES|QL validation, `[4/5]` write native review artifacts, `[5/5]` report) plus an unnumbered rollout-plan step; the old `[4/7]` YAML lint and `[5/7]` YAML→NDJSON compile stages were removed.
- Datadog reuses the shared IR/native emission, first-class dedicated upload (`--upload`), shared smoke validation (`--smoke`), manifest/rollout artifacts, and verification packets. Preflight is first-class (`--preflight` with capability-aware field checks when `--es-url` is provided), while source-aware query validation remains Datadog-located because it can rewrite emitted queries safely before upload. `datadog-migrate --compile` / `--no-compile` / `--legacy-import` were removed and now exit `2` (`core/cli_contract.reject_removed_surfaces` gives `obs-migrate`, `grafana-migrate`, and `datadog-migrate` the same message).

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
