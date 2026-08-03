# Datadog Source Adapter

## Overview

The Datadog adapter translates Datadog dashboards through widget planning,
metric-query parsing, formula translation, and log-search conversion. Its
current first-class flow is extraction, normalization, translation,
capability-aware preflight, IR-first artifact emission (`DashboardIR` → native
Dashboards API payload), emitted-query validation, native review artifacts,
optional upload, post-upload smoke validation, verification packets, and
reporting via the shared Kibana target runtime.

Datadog verification now includes live source execution for single-query metric
widgets when `DD_API_KEY` and `DD_APP_KEY` are available (directly or through
`--env-file`). Log queries and multi-query metric widgets still fall back to
target/runtime evidence today.

For log queries, boolean composition now uses a `Lark` grammar as the primary
parser path, while the existing tokenization and atom extraction logic preserve
Datadog-specific field/filter handling.

## Entry Points

| Surface | Command |
|---|---|
| Dedicated CLI | `.venv/bin/datadog-migrate ...` |
| Module entry point | `.venv/bin/python -m observability_migration.adapters.source.datadog.cli ...` |
| Unified CLI | `.venv/bin/obs-migrate migrate --source datadog ...` |
| Shared upload CLI | `.venv/bin/obs-migrate upload ...` |

## Supported Assets

| Asset | Status |
|---|---|
| Dashboards | Full extraction from files and API |
| Widgets | 15+ types (timeseries, toplist, table, query_value, ...) |
| Metric queries | Parsed AST → ES\|QL |
| Log queries | Datadog log search DSL → ES\|QL WHERE / KQL |
| Formulas | Arithmetic expression translation |
| Template variables | Kibana dashboard controls emitted; query-level semantics still approximate |
| Events / markers | Preserved in normalization, not emitted as first-class target assets |
| Links / drilldowns | Not yet first-class |
| Compilation | Removed. `--compile` / `--no-compile` / `--legacy-import` now exit `2`; the typed-API upload never consumed NDJSON |
| Preflight | Capability-aware field safety checks with live `_field_caps` |
| Upload | First-class `--upload` or shared `obs-migrate upload` |
| Validation / smoke | First-class `--validate --es-url` and post-upload `--smoke` |
| Verification packets | First-class semantic gates and packets, with live metric source execution where configured |
| Manifest / rollout | First-class `migration_manifest.json` and `rollout_plan.json` |
| Monitors | First-class extraction; emits and validates Kibana rule payloads for a narrow field-cap-validated subset |

### Live Extraction Scope

Live extraction is available through `--source api` on the dedicated CLI and
`--input-mode api` on `obs-migrate migrate --source datadog`.

The current API path:
- pulls dashboard objects from the Datadog Dashboards API
- can also pull monitor objects from the Datadog Monitors API when
  `--assets alerts` or `--assets all` is selected (`--fetch-monitors` remains
  the deprecated dedicated alias)
- requires the optional `datadog-api-client` dependency (`.venv/bin/pip install -e ".[datadog]"`)
- supports `--env-file` and optional `--dashboard-ids` on both the dedicated CLI and unified `obs-migrate migrate`
- uses the dashboard list returned by the Datadog API when no dashboard ID list is supplied

Widgets, formulas, and event-marker details are normalized from the dashboard
payloads that were pulled. Monitors are now first-class alert-migration inputs,
while broader Datadog product surfaces beyond dashboards and monitors are still
not first-class migration inputs.

## Execution Pipeline

The dedicated Datadog CLI is a more explicit **normalize -> plan -> translate
-> emit** pipeline than Grafana. It now continues through first-class emitted
query validation, upload, smoke validation, verification packets, migration
manifest output, rollout planning, and live metric source execution when
Datadog credentials are configured.

```text
field profile setup
  -> optional live target field-capability discovery
  -> extract dashboards
  -> normalize_dashboard()
  -> optional capability-aware preflight
  -> plan_widget()
  -> translate_widget()
  -> generate_dashboard_artifacts() (assemble DashboardIR; derive native payload)
  -> optional emitted-query validation
  -> persist native/IR review artifacts
  -> optional upload (typed API, native_dashboard derived from IR)
  -> optional post-upload smoke validation
  -> optional live metric source execution during verification
  -> verification packets and semantic gates
  -> report / manifest / rollout plan
```

| Stage | Primary code | What happens |
|---|---|---|
| Setup | `cli.py`, `field_map.py` | Load the selected field profile, apply dataset/index overrides, derive dataset filters |
| Capability discovery | `field_map.py` | Optionally load live target `_field_caps` from Elasticsearch when `--es-url` is present |
| Extract | `extract.py` | Read dashboards from files or Datadog API |
| Normalize | `normalize.py` | Convert raw Datadog JSON into `NormalizedDashboard` / `NormalizedWidget` |
| Optional preflight | `preflight.py` | Check mapped fields and capability risks before translation when `--preflight` is requested |
| Plan | `planner.py` | Choose `lens`, `esql`, `esql_with_kql`, `markdown`, `group`, or `blocked` for each widget |
| Translate | `translate.py` | Translate metric, log, and formula queries according to the widget plan |
| Emit | `generate.py` | Assemble `DashboardIR`, then derive the native Dashboards API payload from it (controls included) |
| Optional validate | `grafana/esql_validate.py`, `datadog/cli.py` | Validate emitted ES|QL with live Elasticsearch, auto-apply safe fixes, and regenerate artifacts via `generate_dashboard_artifacts` |
| Native/IR review artifacts | `targets/kibana/native_artifacts.py` | Persist `dashboards/native/*.native.json`, `dashboards/ir/*.ir.json`, and `dashboards/native/index.json` after final IR/native regeneration so review artifacts match an immediate upload |
| Optional upload | `targets/kibana/adapter.py`, `dashboards_api.py`, `native_artifacts.py` | Dedicated Datadog CLI uploads the in-memory `native_dashboard` derived from the IR; shared `obs-migrate upload --artifact-dir` uploads the persisted `native/*.native.json` byte-for-byte. There is no YAML input and no fallback renderer |
| Optional smoke | `targets/kibana/adapter.py`, `targets/kibana/smoke.py` | Inspect uploaded dashboards in Kibana, validate runnable panel ES|QL, and merge smoke/browser rollups back into results |
| Verification | `verification.py`, `execution.py` | Build semantic gates, compare target execution with live Datadog metric evidence when configured, and persist `OperationalIR` snapshots |
| Report / artifacts | `report.py`, `manifest.py`, `rollout.py` | Save `migration_report.json`, `migration_manifest.json`, `rollout_plan.json`, smoke/validation evidence, and per-dashboard/widget status details |

Important detail: Datadog planning is an explicit public stage in the runtime.
That is why the adapter exposes planner registries and why `TranslationResult.trace`
can show both planning and translation rule IDs.

## Field Profiles

Datadog uses dotted metric names (`system.cpu.user`) and short tag keys
(`host`, `env`, `service`). Elasticsearch field names depend on the ingestion
pipeline — OTel Collector, Prometheus remote-write, Elastic Agent, or a custom
setup all produce different field paths. Field profiles bridge this gap:
a profile tells the translator how to rename every Datadog metric name and
tag key into the correct Elasticsearch field.

### Alignment with Grafana

Grafana and Datadog share the same operator mental model for `--field-profile`:

1. **Choose a planned profile** — assets-first migration works before telemetry
   exists; emitted queries follow that profile's mapping rules.
2. **Emit field names for the plan** — offline runs do not require `--es-url`.
3. **With `--es-url`, verify against live `_field_caps`** — readiness uses
   `confirmed` / `missing` / `unknown` on `target_readiness_contract.json`;
   live caps do not silently remap to a different layout.

> **An offline run reporting a high success rate does not mean the panels will
> have data.** Without `--es-url`, every mapped field's readiness status is
> `unknown` — the run only proves the queries *translated*, not that the target
> fields *exist*. Always finish with `--es-url --preflight` (add `--es-api-key`
> if security is enabled): that flips each field to `confirmed` or `missing`, so
> the underscore-flattened placeholders that no real index contains show up as
> `missing` in `target_readiness_contract.json` instead of hiding behind a green
> success rate.

Datadog differs in one important way: there is **no `--field-profile auto`**.
Always pick an explicit built-in profile or YAML path. Wrong profile →
missing-field warnings in preflight; emitted queries still follow the chosen
plan.

### How Field Profiles Work

A profile supplies:

| Property | Purpose |
|---|---|
| `metric_map` | Explicit Datadog metric name → ES field overrides (string rename or rich `{target, transform?, attribute_filter?, unit_scale?}`). Prefer the source-neutral `--metric-map-file` CLI flag for operator-authored metric renames; profile-embedded `metric_map` remains available for full custom profiles. Class-2 (`transform` / `attribute_filter` / non-1 `unit_scale`) applies the target rename and emits filter, scale, and rate-transform semantics in ES|QL. |
| `tag_map` | Datadog metric-tag → ES field name (e.g. `host` → `host.name`). Also settable via the source-neutral `--metric-map-file` (a top-level `tag_map:` block), which merges over the active profile's `tag_map` without authoring a full profile. |
| `log_tag_map` | Optional log-only attribute map; when set, unmapped log attributes stay unchanged instead of using `tag_prefix` |
| `metric_prefix` / `metric_suffix` | Default prefix/suffix applied to unmapped metrics after `.` → `_` conversion |
| `tag_prefix` | Default prefix applied to unmapped tags |
| `metric_index` / `logs_index` | Default Elasticsearch index patterns for metrics and logs |
| `timestamp_field` | Timestamp field name (default `@timestamp`) |
| `metrics_dataset_filter` / `logs_dataset_filter` | Auto-derived or explicit `data_stream.dataset` filter values |

**Translation behavior for metrics:** When a Datadog metric name is encountered,
the translator resolves `metric_map` through the shared metric-mapping core.
Exact entries from `--metric-map-file` override entries embedded in the selected
field profile. Class-2 entries (`transform`,
`attribute_filter`, or non-1 `unit_scale`) apply the declared target and emit
attribute filters, unit scaling, and rate-transform semantics in ES|QL. If no
applicable map entry exists, it converts
dots to underscores (`system.cpu.user` → `system_cpu_user`)
and applies `metric_prefix` and `metric_suffix`.

> **`otel` is not a Datadog → OTel metric dictionary.** The built-in `otel`
> profile ships a rich **tag** baseline (`host` → `host.name`, `service` →
> `service.name`, the Kubernetes keys below) but **zero** metric-name overrides.
> So metric *names* are only dot-to-underscore flattened
> (`kubernetes.cpu.usage.total` → `kubernetes_cpu_usage_total`), which will **not**
> match an OTel-native index that stores dotted semantic-convention names like
> `k8s.pod.cpu.usage`. Selecting `otel` does not translate Datadog metric names
> into OTel semconv. To align metric names, supply `--metric-map-file` (or a
> profile-embedded `metric_map`); the same file's optional `tag_map:` block
> renames tags/attributes too. To confirm they exist, run `--es-url
> --preflight`. `elastic_agent` is the only built-in profile with metric-name
> overrides, and only for common `system.*` metrics.

### Agent → OTel collection switch (temporality and units)

When the same logical metric moves from the Datadog Agent to an OTel collector
(or any path that changes **temporality** or **units**), Class-1 renames alone
are not enough:

| Hazard | Symptom | Remedy via `--metric-map-file` |
|---|---|---|
| Cumulative vs delta / rate | Rate panels too high/low or empty after switch | `transform: to_rate` when the target is a cumulative counter; `transform: drop_rate` when the target is already a pre-rated gauge |
| Unit scale (e.g. nanocores ↔ cores, bytes ↔ KiB) | Charts off by a constant factor | `unit_scale:` (multiply the emitted aggregate) |
| Counter typing unknown offline | Translator cannot prove `RATE()` is safe | Run with `--es-url` so `_field_caps` can confirm `time_series_metric=counter`, or set `metric_kinds` / map transform explicitly |

Preflight `target_readiness_contract.json` records `counter_expectations` for
rate-bearing widgets when live caps are available. Panel warnings also call
out approximate delta rates and point at `--metric-map-file` for Agent→OTel
switches.

**Translation behavior for tags:** Metric queries check `tag_map`, then apply
`tag_prefix` (if set) or keep the original tag name. Log queries use
`log_tag_map` when the profile provides one; unmapped log attributes then stay
unchanged instead of inheriting the metric `tag_prefix`. The built-in
Prometheus profiles therefore keep ECS / OTel log fields rather than emitting
`prometheus.labels.*` or `labels.*` paths against `logs-*`.

### Built-in Profiles

| Profile | Default metric index | Metric prefix | Tag prefix / notes | Description |
|---|---|---|---|---|
| `otel` (default) | `metrics-*` | _(none)_ | ECS / OTel semantic **tag** maps | OTel Collector **tag/attribute** names; metric names are only flattened (no OTel semconv metric renames — see note above) |
| `prometheus` | `metrics-prometheus-*` | `prometheus.metrics.` | `prometheus.labels.*` (`host` → `prometheus.labels.instance`) | Metricbeat / Agent Prometheus **remote_write** integration layout |
| `prometheus_native` | `metrics-*.prometheus-*` | `metrics.` | `labels.*` (`host` → `labels.instance`) | Elasticsearch native `/_prometheus` remote-write layout |
| `elastic_agent` | `metrics-*` | _(none)_ | ECS / Elastic Agent maps | Elastic Agent / Metricbeat **system** integration field names |
| `passthrough` | `metrics-*` | _(none)_ | _(none)_ | Keep Datadog names as-is (dots still convert to underscores for metrics) |

> **Breaking change:** `--field-profile prometheus` now maps metric-query tags
> to `prometheus.labels.*` (including `host` → `prometheus.labels.instance`)
> instead of ECS/bare fields. Use `prometheus_native` for `labels.*` on native
> `/_prometheus` data streams. Log queries continue to use ECS / OTel fields.

### Tag Mapping (Shared Baseline)

`otel` and `elastic_agent` share a common ECS-oriented tag baseline (with
`elastic_agent` preferring `kubernetes.*` and `otel` preferring `k8s.*` for
several Kubernetes keys). Prometheus profiles use their label paths for metric
queries, but their log queries use the OTel baseline:

| Datadog tag | `otel` / `elastic_agent` | `prometheus` metric query | `prometheus_native` metric query | Prometheus-profile log query |
|---|---|---|---|---|
| `host` | `host.name` | `prometheus.labels.instance` | `labels.instance` | `host.name` |
| `env` | `deployment.environment` | `prometheus.labels.env` | `labels.env` | `deployment.environment` |
| `service` | `service.name` | `prometheus.labels.service` | `labels.service` | `service.name` |
| `kube_namespace` | `k8s.namespace.name` / `kubernetes.namespace` | `prometheus.labels.kube_namespace` | `labels.kube_namespace` | `k8s.namespace.name` |
| other tags | profile-specific maps | `prometheus.labels.<tag>` | `labels.<tag>` | Original tag (or custom `log_tag_map`) |

Shared `otel` / `elastic_agent` baseline details:

| Datadog tag | Elasticsearch field |
|---|---|
| `host` | `host.name` |
| `env` | `deployment.environment` |
| `service` | `service.name` |
| `version` | `service.version` |
| `source` | `service.name` |
| `status` | `log.level` (only in log context; kept as `status` in metric queries) |
| `container_name` | `container.name` |
| `container_id` | `container.id` |
| `pod_name` | `kubernetes.pod.name` (`otel`: `k8s.pod.name`) |
| `kube_namespace` | `kubernetes.namespace` (`otel`: `k8s.namespace.name`) |
| `kube_cluster_name` | `kubernetes.cluster.name` (`otel`: `k8s.cluster.name`) |
| `kube_deployment` | `kubernetes.deployment.name` (`otel`: `k8s.deployment.name`) |
| `image_name` | `container.image.name` |
| `image_tag` | `container.image.tag` |

### Elastic Agent Metric Overrides

The `elastic_agent` profile also provides explicit metric-name overrides for
common system metrics:

| Datadog metric | Elastic Agent field |
|---|---|
| `system.cpu.user` | `system.cpu.user.pct` |
| `system.cpu.system` | `system.cpu.system.pct` |
| `system.cpu.idle` | `system.cpu.idle.pct` |
| `system.cpu.iowait` | `system.cpu.iowait.pct` |
| `system.mem.usable` | `system.memory.actual.used.bytes` |
| `system.mem.total` | `system.memory.total` |
| `system.disk.in_use` | `system.filesystem.used.pct` |
| `system.net.bytes_rcvd` | `system.network.in.bytes` |
| `system.net.bytes_sent` | `system.network.out.bytes` |

### Choosing a Profile

| Your ingestion pipeline | Recommended profile |
|---|---|
| OTel Collector → Elasticsearch | `otel` (default) |
| Metricbeat / Agent Prometheus remote_write → Elasticsearch | `prometheus` |
| Elasticsearch native `/_prometheus` remote write | `prometheus_native` |
| Elastic Agent / Metricbeat system integrations → Elasticsearch | `elastic_agent` |
| Custom pipeline or unknown | Start with `passthrough`, then iterate |

### Using a Built-in Profile

```bash
.venv/bin/datadog-migrate \
  --source files \
  --input-dir infra/datadog/dashboards \
  --output-dir datadog_migration_output \
  --field-profile otel
```

### Using a Custom YAML Profile

Create a YAML file with your custom mappings:

```yaml
name: my_custom_profile
metric_index: metrics-custom-*
logs_index: logs-custom-*
timestamp_field: "@timestamp"
metrics_dataset_filter: ""
logs_dataset_filter: ""

metric_map:
  system.cpu.user: my.cpu.user.pct
  system.mem.usable: my.memory.used.bytes

tag_map:
  host: host.name
  env: deployment.environment
  service: service.name
  kube_namespace: kubernetes.namespace

metric_prefix: ""
metric_suffix: ""
tag_prefix: ""
```

Then pass the path:

```bash
.venv/bin/datadog-migrate \
  --source files \
  --input-dir infra/datadog/dashboards \
  --output-dir datadog_migration_output \
  --field-profile ./my-field-profile.yaml
```

Custom profiles are schema-validated before load using Pydantic. A concrete
starter example lives at `examples/datadog-field-profile.example.yaml`.

### Emitting a Starter Template

To generate a validated starter profile from the runtime contract:

```bash
.venv/bin/obs-migrate extensions --source datadog --format yaml --template-out custom-field-profile.yaml
```

If you want environment overlays before exporting YAML, a matching starter CUE
example lives at `examples/cue/datadog-field-profile.cue`.

### Live Field Capability Discovery

When `--es-url` is provided, the profile can load live `_field_caps` from
Elasticsearch. This enables type-aware translation decisions and preflight
checks — the translator can verify whether a mapped field actually exists,
is numeric and aggregatable, or has conflicting types across indices.

The dashboard pipeline also writes
`<output-dir>/dashboards/schema_change_report.md`,
`<output-dir>/dashboards/telemetry_contract.json`, and
`<output-dir>/dashboards/target_readiness_contract.json`. The schema report is
the per-panel source-field -> target-field table. The readiness contract records
the active `field_profile`, metric/log index patterns, source fields, resolved
target fields, and field `status` (`confirmed`, `missing`, or `unknown`); each
entry also carries `mapped_from` when the target field differs from its
source field(s) — including default dot-to-underscore renames and explicit
`metric_map` exact renames — mirroring the Grafana `required_target_contract.json`
`mapped_from` field.
`unknown` means live field caps were unavailable; it is not proof that a field
exists.

`--data-view` is an explicit override. When omitted, the selected field profile
keeps its own metric index (for example, `prometheus` keeps
`metrics-prometheus-*` instead of being overwritten by the OTel default
`metrics-*`).

## Command Coverage

Datadog command examples and the canonical shared migration contract are
centralized in `docs/command-contract.md` to avoid drift.

Use that doc for:
- dedicated Datadog migration flows (`datadog-migrate`)
- the curated demo wrapper (`scripts/run_datadog_demo.sh`) for local or serverless smoke validation with small generated data
- unified `obs-migrate migrate --source datadog`
- the asset scope contract (`--assets {dashboards,alerts,all}` plus the
  deprecated `--fetch-monitors` / unified `--fetch-alerts` aliases)
- shared upload/cluster commands, and the table of removed dashboard-YAML surfaces
- extension catalog and template commands

## Datadog-Specific Notes

- `--assets {dashboards,alerts,all}` is the canonical selector on both the
  dedicated and unified migration surfaces. `--fetch-monitors` remains only as
  a deprecated compatibility alias on the dedicated CLI, while unified
  `--fetch-alerts` forwards to the same alert pipeline. Using either alias
  always emits a deprecation warning; if the requested asset selection is
  `dashboards`, including explicit `--assets dashboards`, runtime normalization
  upgrades the run to `--assets all`.
- Dashboard artifacts are written under `<output-dir>/dashboards`: native review
  artifacts (`native/*.native.json`, `native/index.json`), IR review artifacts
  (`ir/*.ir.json`), reports, manifests, and rollout evidence — no `yaml/` and no
  `compiled/` directory. Alert artifacts are written under
  `<output-dir>/alerts`; Datadog also writes a root `run_summary.json`.
- `--field-profile` selects a built-in mapping profile or a custom YAML profile.
  There is no `auto` profile — pick the plan that matches your ingest route,
  then verify with `--es-url` (`confirmed` / `missing` / `unknown` on
  `target_readiness_contract.json`).
- `--env-file` loads Datadog API credentials for API extraction and live metric
  source execution during verification.
- `--ca-cert <path>` (env `OBS_MIGRATE_CA_CERT`) and `--insecure` (env
  `OBS_MIGRATE_INSECURE`) control TLS verification for all outbound connections
  (Elasticsearch, Kibana, and the Node upload step). Prefer `--ca-cert` for
  private CAs; `--insecure` disables verification for testing only.
- `--source api --dashboard-ids ...` scopes live Datadog dashboard extraction
  on the dedicated CLI; unified `obs-migrate migrate --source datadog
  --input-mode api --dashboard-ids ...` exposes the same scoping.
- `--monitor-ids` and `--monitor-query` scope monitor extraction during
  alert-capable runs.
- In file mode, keep monitor JSON under `<input-dir>/monitors/`. When
  `--assets alerts` is selected, dashboard JSON files are not required because
  dashboard extraction is skipped.
- `--create-alert-rules` runs after an alert-capable asset selection and writes
  `<output-dir>/alerts/monitor_rule_upload_results.json`. If it was requested but
  no rules were created (no `--kibana-api-key`, unreachable alerting preflight)
  the run exits non-zero and records the reason under `alerts.rule_creation` in
  `run_summary.json`; a missing `--kibana-url` or non-alert `--assets` is
  rejected up front with exit `2`.
- `--compile` / `--no-compile` / `--legacy-import` were **removed** from both the
  dedicated `datadog-migrate` CLI and unified `obs-migrate migrate --source
  datadog`; they now exit `2` with a message naming the replacement. The
  typed-API upload never consumed the compiled NDJSON artifact. Datadog's
  `runtime_summary.layout` survives, but it is now populated only by the
  post-upload smoke layout check, not by a compile-time layout validator.
- `obs-migrate extensions --source datadog --template-out ...` emits a
  validated starter field-profile template, and
  `examples/cue/datadog-field-profile.cue` remains the optional CUE authoring
  example.
- `image` widgets with a real absolute `http(s)` URL map to a native Kibana
  `image` panel (see `docs/targets/kibana.md#links-and-image-panels`) via
  `planner.py::image_widget_rule`. Relative/internal Datadog asset URLs (e.g.
  `/static/...`) would 404 in Kibana and still degrade to the previous
  markdown-embed placeholder. CSS-compatible `sizing` values map to Kibana
  `fit`; deprecated Datadog aliases map as `fit` → `contain`, `zoom` →
  `cover`, and `center` → `none` (`scale-down` degrades to `contain` with a
  warning because Kibana has no exact equivalent).

## Per-Widget Planning And Translation

The Datadog path is now organized around executable stages:

1. `normalize.py`: turn raw Datadog dashboards into `NormalizedDashboard` and `NormalizedWidget`.
2. `planner.py`: run registry-backed planning rules that choose `lens`, `esql`, `esql_with_kql`, `markdown`, `image`, `group`, or `blocked`.
3. `preflight.py`: resolve mapped target fields and surface capability risks before translation.
4. `translate.py`: run registry-backed metric, log, and Lens translation rules.
5. `generate.py`: assemble `DashboardIR` and derive the native Dashboards API
   payload from it, then hand off to the review-artifact and report steps. The
   run writes `dashboards/native/` and `dashboards/ir/` only — never a
   `dashboards/yaml/` or `dashboards/compiled/` directory, and a stale one from
   an older release is swept first.

### Layout Derivation And Curated Layout Packs

Datadog widgets carry a free-form 12-column grid position. `generate.py`
rescales each source row proportionally onto Kibana's 48-column grid, then
resolves any residual overlap and applies the shared style-guide layout pass.
That keeps the source grouping recognizable and is checked by a geometry gate
(no overlaps, `x + w <= 48`, no sub-minimum widths), but it also inherits the
source's asymmetric splits and ragged heights.

For a small number of high-traffic dashboards, a bundled **curated layout pack**
replaces that auto-derived geometry with a hand-tuned Kibana-native layout. The
pack is matched by dashboard title and applies automatically — there is no flag
and no operator step — and it changes **layout only**: panel size/position and
section collapsed state, never a query, panel type, or field mapping. Dashboards
with no matching pack are unaffected. `Redis - Overview` is the only dashboard
with a pack today, and the Datadog pack directory is not yet declared as package
data, so the curated layout currently applies from a repo checkout or editable
install rather than a released wheel (see the *Packaging* note in the design doc
below).

Pack format, selector semantics, and the authoring/validation loop:
`../design/curated-dashboard-packs.md#datadog-curated-layout-packs`. Note that
these Datadog layout packs are a different mechanism from the Grafana curated
*rule* packs described in the same document, which override query semantics and
panel fidelity instead.

### Hostmap Fallback

Datadog hostmaps store their metric requests under keyed `fill`/`size`
objects rather than the standard request array. Those queries are normalized
and, when they include a host/category grouping, emitted as a grouped Kibana
datatable. This preserves the target dimension and metric values while
explicitly warning that Datadog's tile layout and value-based coloring are not
available. An ungrouped hostmap still requires manual redesign because no
host/value table can be constructed.

### Template-Variable Filter Limitations

Tag-backed metric template variables can become Kibana options-list controls,
but Datadog also supports variable shapes that do not have a faithful direct
equivalent:

- A variable referenced only by log widgets binds to the logs data view and
  uses log-field mapping. Metric-only and unreferenced variables bind to the
  metrics data view. A variable shared by metric and log widgets still needs
  review because one Kibana options-list control cannot target two data views.
- Source `available_values` and preselected defaults are retained in the typed
  `ControlIR`. Kibana options-list controls populate from target field values,
  so `available_values` is provenance rather than an enforced static
  allow-list in the emitted control.
- `$scope` represents an entire Datadog scope expression rather than one tag
  field. It is omitted with an explicit manual-recreation warning; the
  migration does not claim that a nonexistent single control replaces it.
- Template variables inside Datadog log filters are removed from executable
  ES|QL because the substitution cannot be bound exactly. The panel is marked
  with a warning and the filter must be recreated in Kibana rather than being
  silently reported as a clean translation.

### Dynamic Group-By Template Variables

A metric grouping such as `by {host}` maps to the corresponding target field
(`host.name` in the OTel profile). A grouping that is itself a Datadog
template variable, such as `by {$grouping}`, cannot be resolved to a stable
target field during migration. It is therefore emitted as
`requires_manual` with no executable ES|QL rather than querying a literal
`` `$grouping` `` field that does not exist. Choose a fixed Datadog tag before
migration or recreate the selector as a Kibana field control.

### Formula Translation Specifics

The translator handles Datadog formulas at three layers:

- **Pointwise functions** (`abs`, `ceil`, `floor`, `round`, `default_zero`, `exclude_null`, `per_second`, `per_minute`, `per_hour`) map directly to ES|QL expressions in the `EVAL` stage.
- **Derivative functions** (`rate`, `diff`, `monotonic_diff`) take one of two paths depending on the target field's live `_field_caps`:
  - **TS|QL path (preferred, counter-typed targets)**: when `time_series_metric_kind == "counter"` or `type ∈ {counter_long, counter_integer, counter_double}`, the translator emits `TS index | STATS rate_alias = RATE(metric, 5 minute) BY TBUCKET(5 minute)` (or `INCREASE(...)` for `diff`/`monotonic_diff`). This is the native ES|QL time-series aggregation — same pattern the Grafana adapter uses for PromQL `rate()`. Mirrors Datadog counter-rate semantics directly.
  - **FROM + FIRST/LAST path (fallback, gauges)**: when no counter capability is detected, the `STATS` clause emits `FIRST(metric, @timestamp)` and `LAST(metric, @timestamp)` alongside the standard aggregation, and `EVAL` computes `(last − first) / bucket_span_seconds` for `rate()` or `(last − first)` for `diff()`. A per-aggregation `WHERE metric IS NOT NULL` guard skips rows where the target column is null (needed when multiple metrics share the index).
- **Multi-query formulas with different filters** (e.g. `count:x{direction:in} / count:x{direction:out}`) translate via per-aggregation `WHERE` clauses inside `STATS`: each query's tag filters are attached to its own aggregation expression. The outer `WHERE` becomes the `TIME_FILTER` plus an `OR` of the spec filters. Different groupings are still surfaced as `requires_manual` because the resolution between divergent group sets is semantically ambiguous.
- **Direct-reference table formulas with different request reducers** apply
  each reducer independently (for example `AVG` for message-rate columns and
  `LAST` for a lag column) after the shared time-bucket stage. Composite
  formulas that mix queries with incompatible reducers remain blocked because
  reducer ordering would be ambiguous.
- **Value-filtered count aggregators** such as
  `count(v: v>=0):metric{scope} by {service}` retain the numeric predicate as
  an ES|QL metric filter before `COUNT(*)`. Function-chain behavior such as
  `.as_rate()` and `.rollup(10)` then follows the existing warned rate/rollup
  approximation path instead of forcing manual review.
- **`top(query, N, agg, order)`** parses (the formula tokenizer accepts string-literal arguments) and unwraps to the query reference with a warning that top-N filtering relies on panel-level sort/limit.

### Parity Harness

`parity-rig/datadog/` contains an end-to-end correctness harness (`scripts/run_datadog_parity.sh`) that seeds deterministic synthetic data into both Datadog and Elasticsearch and diffs the values returned by source DD queries vs translated ES|QL. See `parity-rig/datadog/README.md` for verdicts and the default test cases.

## Executable Rule Catalog

Datadog now exports a real extension catalog from live registries rather than a descriptive placeholder. That means:

- `obs-migrate extensions --source datadog` lists rule IDs that the runtime can actually fire.
- `TranslationResult.trace` records the Datadog planning and translation rule IDs that fired for each panel.
- The catalog and trace share the same stable rule IDs, which makes extension work and debugging much easier.

The current registry groups are:

- `planner_prechecks`
- `metric_planners`
- `log_planners`
- `metric_translators`
- `log_translators`
- `lens_translators`

Preflight is already executable and reported, but it is not yet exposed through a public registry.

## Current Boundaries

- The Datadog migrate flow now supports first-class preflight, validate, upload, smoke validation, migration manifest and rollout outputs, and verification packets.
- Live target `_field_caps` and emitted-query validation are integrated, but the safe-fix validation helper still reuses shared logic that currently lives in the Grafana-side module layout.
- Verification can now execute simple Datadog metric queries live for measured source-vs-target comparison, but logs and multi-query metric widgets still fall back to target/runtime evidence.
- Datadog monitors are first-class extraction inputs, but the main Datadog migration command currently stops at emitted/validated Kibana rule payloads for monitor shapes we can parse faithfully and verify against the configured field profile plus live target `_field_caps`.
- Broader Datadog product surfaces such as drilldowns, APM, RUM, network, security, and CI are still not first-class migration inputs.
- Unified `obs-migrate migrate --source datadog --input-mode api` forwards
  `--env-file` and `--dashboard-ids` for scoped live dashboard extraction.

## Adapter Location

`observability_migration/adapters/source/datadog/`

Important modules:

- `adapter.py`: adapter registration for the unified CLI.
- `cli.py`: Datadog-specific orchestration and reporting.
- `extract.py`: file and API extraction plus credential loading.
- `normalize.py`: raw Datadog dashboard normalization.
- `planner.py`: widget planning and execution-path selection.
- `query_parser.py`, `log_parser.py`, `translate.py`: query and formula translation.
- `field_map.py`: built-in field profiles and custom profile loading.
- `curated_packs/`: per-dashboard curated Kibana layout packs, auto-matched by
  dashboard title (see
  [Layout Derivation And Curated Layout Packs](#layout-derivation-and-curated-layout-packs)).

---

**See also:** [Datadog Pipeline Trace](datadog-trace.md) — auto-generated per-dashboard translation traces | [Shared Pipeline Overview](../pipeline-trace.md)
