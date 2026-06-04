# Datadog Source Adapter

## Overview

The Datadog adapter translates Datadog dashboards through widget planning,
metric-query parsing, formula translation, and log-search conversion. Its
current first-class flow is extraction, normalization, translation,
capability-aware preflight, YAML generation, emitted-query validation, optional
compile, optional upload, post-upload smoke validation, verification packets,
and reporting via the shared Kibana target runtime.

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
| Compilation | Always performed by unified `obs-migrate migrate`; explicit `--compile` on the dedicated CLI |
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
  -> generate_dashboard_yaml()
  -> optional emitted-query validation
  -> optional compile
  -> optional upload
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
| Emit YAML | `generate.py` | Build Kibana YAML, dashboard controls, and output files |
| Optional validate | `grafana/esql_validate.py`, `datadog/cli.py` | Validate emitted ES|QL with live Elasticsearch, auto-apply safe fixes, and regenerate placeholder-safe YAML for failures |
| Optional compile | `targets/kibana/compile.py` | Compile generated YAML to NDJSON when `--compile` is requested |
| Optional upload | `targets/kibana/compile.py` | Dedicated Datadog CLI can upload after compile; shared `obs-migrate upload` still works too |
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

### How Field Profiles Work

A profile supplies:

| Property | Purpose |
|---|---|
| `metric_map` | Explicit Datadog metric name → ES field overrides (e.g. `system.cpu.user` → `system.cpu.user.pct`) |
| `tag_map` | Datadog tag / log attribute → ES field name (e.g. `host` → `host.name`) |
| `metric_prefix` / `metric_suffix` | Default prefix/suffix applied to unmapped metrics after `.` → `_` conversion |
| `tag_prefix` | Default prefix applied to unmapped tags |
| `metric_index` / `logs_index` | Default Elasticsearch index patterns for metrics and logs |
| `timestamp_field` | Timestamp field name (default `@timestamp`) |
| `metrics_dataset_filter` / `logs_dataset_filter` | Auto-derived or explicit `data_stream.dataset` filter values |

**Translation behavior for metrics:** When a Datadog metric name is encountered,
the translator first checks `metric_map` for an explicit override. If none
exists, it converts dots to underscores (`system.cpu.user` → `system_cpu_user`)
and applies `metric_prefix` and `metric_suffix`.

**Translation behavior for tags:** When a Datadog tag key is encountered, the
translator checks `tag_map` for an explicit mapping. If none exists, it applies
`tag_prefix` (if set) or keeps the original tag name.

### Built-in Profiles

| Profile | Default metric index | Metric prefix | Description |
|---|---|---|---|
| `otel` (default) | `metrics-*` | _(none)_ | OpenTelemetry Collector field names |
| `prometheus` | `metrics-prometheus-*` | `prometheus.metrics.` | Prometheus remote-write field names |
| `elastic_agent` | `metrics-*` | _(none)_ | Elastic Agent / Metricbeat integration field names |
| `passthrough` | `metrics-*` | _(none)_ | Keep Datadog names as-is (dots still convert to underscores for metrics) |

### Tag Mapping (Shared Baseline)

All profiles except `passthrough` share a common tag mapping baseline:

| Datadog tag | Elasticsearch field |
|---|---|
| `host` | `host.name` (`instance` for `prometheus` profile) |
| `env` | `deployment.environment` |
| `service` | `service.name` |
| `version` | `service.version` |
| `source` | `service.name` |
| `status` | `log.level` (only in log context; kept as `status` in metric queries) |
| `container_name` | `container.name` |
| `container_id` | `container.id` |
| `pod_name` | `kubernetes.pod.name` |
| `kube_namespace` | `kubernetes.namespace` |
| `kube_cluster_name` | `kubernetes.cluster.name` |
| `kube_deployment` | `kubernetes.deployment.name` |
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
| Prometheus → remote_write → Elasticsearch | `prometheus` |
| Elastic Agent / Metricbeat → Elasticsearch | `elastic_agent` |
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
target fields, and field `status` (`confirmed`, `missing`, or `unknown`).
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
- shared compile/upload/cluster commands
- extension catalog and template commands

## Datadog-Specific Notes

- `--assets {dashboards,alerts,all}` is the canonical selector on both the
  dedicated and unified migration surfaces. `--fetch-monitors` remains only as
  a deprecated compatibility alias on the dedicated CLI, while unified
  `--fetch-alerts` forwards to the same alert pipeline. Using either alias
  always emits a deprecation warning; if the requested asset selection is
  `dashboards`, including explicit `--assets dashboards`, runtime normalization
  upgrades the run to `--assets all`.
- Dashboard artifacts are written under `<output-dir>/dashboards`; alert
  artifacts are written under `<output-dir>/alerts`; Datadog also writes a root
  `run_summary.json`.
- `--field-profile` selects a built-in mapping profile or a custom YAML profile.
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
  `<output-dir>/alerts/monitor_rule_upload_results.json`.
- `--compile` is opt-in on the dedicated `datadog-migrate` CLI; unified
  `obs-migrate migrate --source datadog` compiles dashboard output by default
  when the dashboard pipeline runs.
- `obs-migrate extensions --source datadog --template-out ...` emits a
  validated starter field-profile template, and
  `examples/cue/datadog-field-profile.cue` remains the optional CUE authoring
  example.

## Per-Widget Planning And Translation

The Datadog path is now organized around executable stages:

1. `normalize.py`: turn raw Datadog dashboards into `NormalizedDashboard` and `NormalizedWidget`.
2. `planner.py`: run registry-backed planning rules that choose `lens`, `esql`, `esql_with_kql`, `markdown`, `group`, or `blocked`.
3. `preflight.py`: resolve mapped target fields and surface capability risks before translation.
4. `translate.py`: run registry-backed metric, log, and Lens translation rules.
5. `generate.py`: emit kb-dashboard YAML and hand off to report/compile steps.

### Formula Translation Specifics

The translator handles Datadog formulas at three layers:

- **Pointwise functions** (`abs`, `ceil`, `floor`, `round`, `default_zero`, `exclude_null`, `per_second`, `per_minute`, `per_hour`) map directly to ES|QL expressions in the `EVAL` stage.
- **Derivative functions** (`rate`, `diff`, `monotonic_diff`) take one of two paths depending on the target field's live `_field_caps`:
  - **TS|QL path (preferred, counter-typed targets)**: when `time_series_metric_kind == "counter"` or `type ∈ {counter_long, counter_integer, counter_double}`, the translator emits `TS index | STATS rate_alias = RATE(metric, 5 minute) BY TBUCKET(5 minute)` (or `INCREASE(...)` for `diff`/`monotonic_diff`). This is the native ES|QL time-series aggregation — same pattern the Grafana adapter uses for PromQL `rate()`. Mirrors Datadog counter-rate semantics directly.
  - **FROM + FIRST/LAST path (fallback, gauges)**: when no counter capability is detected, the `STATS` clause emits `FIRST(metric, @timestamp)` and `LAST(metric, @timestamp)` alongside the standard aggregation, and `EVAL` computes `(last − first) / bucket_span_seconds` for `rate()` or `(last − first)` for `diff()`. A per-aggregation `WHERE metric IS NOT NULL` guard skips rows where the target column is null (needed when multiple metrics share the index).
- **Multi-query formulas with different filters** (e.g. `count:x{direction:in} / count:x{direction:out}`) translate via per-aggregation `WHERE` clauses inside `STATS`: each query's tag filters are attached to its own aggregation expression. The outer `WHERE` becomes the `TIME_FILTER` plus an `OR` of the spec filters. Different groupings are still surfaced as `requires_manual` because the resolution between divergent group sets is semantically ambiguous.
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

- The Datadog migrate flow now supports first-class preflight, validate, compile, upload, smoke validation, migration manifest and rollout outputs, and verification packets.
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

---

**See also:** [Datadog Pipeline Trace](datadog-trace.md) — auto-generated per-dashboard translation traces | [Shared Pipeline Overview](../pipeline-trace.md)
