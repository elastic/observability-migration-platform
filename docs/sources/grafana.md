# Grafana Source Adapter

## Overview

The Grafana adapter is the most mature source path in the platform. It handles
file and API extraction, panel translation, verification artifacts, preflight
reporting, optional upload, and post-upload smoke validation.

Grafana query translation has four paths:

1. **Native PROMQL** (the preferred, highest-fidelity target): wraps
   compatible PromQL in `PROMQL index=... value=(expr)` for highest fidelity on
   Elastic Serverless. When `--es-url` is set, the target is probed and the run
   falls back to ES|QL translation only when the `PROMQL` command is *confirmed*
   unsupported. An inconclusive probe (transport/auth error) keeps native PROMQL
   — the optimistic default — and warns, rather than routing a possibly-capable
   cluster down the fallback. With no `--es-url` there is no cluster to probe, so
   native PROMQL is assumed. Use `--translation-mode {auto,native,esql}` only
   for explicit operator overrides; `auto` remains the normal path.
   (Per-construct fallback to ES|QL still applies automatically when a specific
   PromQL function/family is unsupported.)
2. **Rule-engine ES|QL**: parses PromQL with `promql-parser`, classifies the
   expression, and translates it through the rule pipeline.
3. **LLM fallback ES|QL**: optional local-AI fallback for panels the rule
   engine marks `not_feasible`.
4. **Native ES|QL reuse**: passes through existing Elasticsearch queries.

## Entry Points

| Surface | Command |
|---|---|
| Dedicated CLI | `.venv/bin/grafana-migrate ...` |
| Module entry point | `.venv/bin/python -m observability_migration.adapters.source.grafana.cli ...` |
| Unified CLI | `.venv/bin/obs-migrate migrate --source grafana ...` |
| Integrated smoke validation | `grafana-migrate --smoke ...` or `obs-migrate migrate --source grafana --smoke ...` |
| Standalone smoke validation | `.venv/bin/grafana-validate-uploaded ...` or `.venv/bin/python -m observability_migration.adapters.source.grafana.validate_uploaded_dashboards ...` |
| Corpus generation | `.venv/bin/grafana-generate-corpus ...` or `.venv/bin/python -m observability_migration.adapters.source.grafana.corpus ...` |

## Supported Assets

| Asset | Status |
|---|---|
| Dashboards | Full extraction from files and API |
| Panels | 40+ panel types with layout preservation |
| Queries (PromQL) | Mature translation with native PROMQL and ES\|QL paths |
| Queries (LogQL) | ES\|QL translation |
| Variables / Controls | Label values, range, text, interval |
| Alerts (legacy) | Task extraction with Kibana rule suggestions |
| Annotations | Candidate event annotations |
| Links / Drilldowns | URL and dashboard drilldowns |
| Transformations | Redesign task classification |
| Preflight | Customer-facing readiness reports |
| Verification | Verification packets and semantic gates |
| Smoke validation | Saved-object runtime validation and browser audit |

### Live Extraction Scope

Live extraction is available through `--source api` on the dedicated CLI and
`--input-mode api` on `obs-migrate migrate --source grafana`.

The current API path:
- uses environment-driven connection settings (`GRAFANA_URL`, `GRAFANA_USER`, `GRAFANA_PASS`)
- sends HTTP basic-auth requests through the current extractor implementation
- pulls dashboard documents from `/api/search` and `/api/dashboards/uid/<uid>`
- is capped at 500 dashboards per search request today

Links, annotations, transformations, and legacy alert tasks are derived from
dashboard JSON during migration. They are not fetched as separate first-class
Grafana API assets in the current migration surface.

## Execution Pipeline

The dedicated Grafana CLI is not just a translator. It is the most complete
end-to-end source pipeline in the repo.

```text
rule packs/plugins/schema setup
  -> extract dashboards
  -> translate_dashboard() (assemble DashboardIR; derive native + YAML)
  -> optional metadata polish (rebuild DashboardIR; re-derive native + YAML)
  -> optional emitted-query validation and YAML sync (same IR rebuild)
  -> persist native/IR review artifacts
  -> YAML lint
  -> optional compile and layout validation
  -> optional upload (typed API prefers native_dashboard from IR)
  -> optional integrated smoke validation / browser audit / screenshot capture
  -> verification packets and report artifacts
  -> optional preflight probes and schema contract
  -> rollout plan
```

| Stage | Primary code | What happens |
|---|---|---|
| Setup | `cli.py`, `rules.py`, `schema.py` | Load rule packs/plugins, configure dataset filters, build `SchemaResolver`, discover fields when `--es-url` is present |
| Extract | `extract.py` | Read dashboards from files or Grafana API |
| Translate + emit | `panels.py`, `translate.py`, `promql.py` | Choose native `PROMQL`, rule-engine ES\|QL, LLM fallback, or native ES\|QL reuse; map panels; assemble `DashboardIR`; derive native Dashboards API payload + YAML |
| Feature-gap extraction | `links.py`, `annotations.py`, `alerts.py`, `transforms.py` | Collect reviewer-facing artifacts for non-query surfaces |
| Optional validate | `esql_validate.py` | Validate emitted target queries against Elasticsearch, auto-fix safe cases, and manualize broken ones |
| Native/IR review artifacts | `targets/kibana/native_artifacts.py` | Persist `dashboards/native/*.native.json`, `dashboards/ir/*.ir.json`, and `dashboards/native/index.json` after final IR/native regeneration so review artifacts match an immediate upload |
| Lint / compile / layout | `targets/kibana/compile.py` | Lint YAML; optional compile NDJSON and validate compiled layout |
| Optional upload | `targets/kibana/compile.py`, `dashboards_api.py`, `native_artifacts.py` | Typed API upload prefers in-memory `native_dashboard` from IR; standalone `obs-migrate upload --artifact-dir` prefers the persisted native artifact when present, rejects mixed native/YAML artifact roots, and falls back to YAML only when native artifacts are absent or YAML is selected |
| Optional integrated smoke | `cli.py`, `targets/kibana/smoke.py`, `smoke_integration.py` | Validate uploaded dashboards, optionally run browser audit / screenshots, then merge post-upload smoke results back into the migration evidence |
| Verification + reporting | `verification.py`, `report.py`, `manifest.py`, `rollout.py` | Build semantic gates, save reports/manifests/verification packets, and generate rollout guidance |
| Optional preflight mode | `preflight.py` | Probe source inventory, target readiness, and required target contract for readiness assessment |

Important detail: Grafana `translate_dashboard()` is a broad stage. It already
includes layout normalization, variable/control translation, `DashboardIR`
assembly, and derived native/YAML emission, not just query translation.

## Schema Resolution and Field Naming

Grafana dashboards use Prometheus label names (`instance`, `job`, `namespace`)
and metric names (`node_cpu_seconds_total`) that may not match the Elasticsearch
field names in the target cluster. The Grafana adapter uses `SchemaResolver`
and rule packs to bridge this gap — this is the Grafana equivalent of Datadog's
field profiles.

`--field-profile` selects the **planned** target layout. Translation emits field
names for that plan immediately (including offline runs with no `--es-url`).
With `--es-url`, live `_field_caps` **verify** the plan — they do not silently
remap queries to a different detected layout.

> **Breaking change (plan→emit→verify):** Default **`otel`** no longer picks a
> Prometheus namespaced layout from live caps alone. Use **`--field-profile auto
> --es-url`** to infer Fleet typed remote-write, classic Metricbeat nested, or
> native `/_prometheus` layouts, or set **`prometheus_remote_write`** /
> **`prometheus_metrics`** / **`prometheus_native`** explicitly when you know the
> ingest route. Under **`otel`**, `resolve_metric_field()` still field-selects
> `metrics.<name>` when caps advertise that field but not the bare PromQL name
> (OTel Collector / issue #270) — that is not profile remapping. When live caps
> clearly look like a named Prometheus layout while the plan is still `otel`,
> migrate records a warning; emit stays on `otel` (no silent remap).

| Profile | Offline emit | With `--es-url` |
|---|---|---|
| **`otel`** (default) | Bare / OTel-candidate mapping | Verify fields; warn on missing |
| **`prometheus_remote_write`** | `prometheus.<metric>.{counter,value,rate}`, `prometheus.labels.*` | Verify; `profile_mismatch` if caps look like another named layout |
| **`prometheus_metrics`** | `prometheus.metrics.<metric>`, `prometheus.labels.*` | Same mismatch rule (classic Metricbeat `use_types=false`) |
| **`prometheus_native`** | `metrics.<metric>`, `labels.*` | Same mismatch rule |
| **`passthrough`** | Source names verbatim (rule-pack overrides still apply) | Validate bare names when possible; no automatic remapping |
| **`auto`** (Grafana-only) | Rejected without `--es-url` | Detect clear typed / nested / native layout; ambiguous or empty caps → emit as **`otel`** + warn |

Example planned layouts:

| Profile | Metric `http_requests_total` → | Label `service` → |
|---|---|---|
| `prometheus_remote_write` | `prometheus.http_requests_total.counter` / `.value` / `.rate` | `prometheus.labels.service` |
| `prometheus_metrics` | `prometheus.metrics.http_requests_total` | `prometheus.labels.service` |
| `prometheus_native` | `metrics.http_requests_total` | `labels.service` |
| `otel` (default) | `http_requests_total` (pass-through) | exact match → OTel candidate → as-is |
| `passthrough` | `http_requests_total` | `service` |

When `profile_mismatch` is true (planned profile ≠ detected named layout),
translation **keeps the plan**. The flag is recorded on
`required_target_contract.json` for operator visibility; it is not a separate
preflight gate beyond existing missing-field severity.

### How Schema Resolution Works

Within the chosen profile, **labels** resolve through this order:

| Priority | Source | How to configure |
|---|---|---|
| 1 (highest) | Rule-pack `label_rewrites` | `--rules-file custom-pack.yaml` |
| 2 | Exact field match (source-faithful) | target advertises the label as a real field |
| 3 | Profile-namespaced field (`prometheus.labels.<l>` / `labels.<l>`) | chosen `--field-profile` |
| 4 | Live ES `_field_caps` OTel discovery | `--es-url` flag (verify only) |
| 5 | Built-in Prometheus → OTel candidate mappings | always available offline |
| 6 (lowest) | Pass-through (use label as-is) | default fallback |

**Metrics** may also use `--metric-map-file` (shared building block with
Datadog). The file uses a source-neutral top-level `metric_map` mapping. Exact
renames (`source: target` or `{target: ...}`) win over profile/passthrough and
over rule-pack `query.metric_map` entries with the same source metric. Entries
with `transform` or `attribute_filter`, or a non-1 `unit_scale`, are
**class-2**: the target rename applies and emitters honor filter, scale, and
rate-transform semantics in ES|QL (with warnings when counter/gauge kind is
unknown for transform planning).
Author maps for your environment; the tool does not auto-suggest metric
renames. Preflight
`required_target_contract.json` lists required fields (a worklist, not a full
Prom→OTEL dictionary) and may include `mapped_from` when source ≠ target.

`resolve_metric_field()` applies `metric_map` first, then rewrites metric names
per profile, and `is_counter()` resolves counter-vs-gauge
(rule-pack `metric_kinds` → `counter_suffixes` → the field's `time_series_metric`
capability → the profile's counter field) so `rate()`/`irate()` stay correct.

**`metric_map` and native PROMQL do not mix.** Native PROMQL embeds the
*literal* source PromQL text and never calls `resolve_metric_field`. When you
pass `--metric-map-file` and leave `--translation-mode` at `auto`, Grafana
automatically uses ES|QL translation so the map applies — the same operator
path as Datadog. If you force `--translation-mode native` while a map is
loaded, panels that stay on the native path attach a warning
(`metric_map not applied for <metric>: native PROMQL requires literal target
metric names`) and are marked `migrated_with_warnings`.

> **Verify requires live data.** Without `--es-url`, or before telemetry lands,
> per-field status may be `unknown` — the planned layout still drives emitted
> queries. After ingest, rerun with a reachable `--es-url` and confirm
> `field_profile`, `planned_schema_profile`, `detected_schema_profile`,
> `profile_mismatch`, `field_capabilities_discovery`, and resolved target-field
> `status` in `required_target_contract.json`.

Dashboard migration writes `schema_change_report.md` and
`telemetry_contract.json` under `<output-dir>/dashboards/` automatically. Use
the schema report for the per-panel Prometheus source field -> Elastic target
field table, and use `required_target_contract.json` for live field-existence
status.

### Built-in Prometheus → OTel Mappings

When no rule-pack override or live field match is available, the resolver
falls back to these built-in candidate mappings:

| Prometheus label | OTel / Elasticsearch candidates |
|---|---|
| `instance` | `service.instance.id`, `host.name`, `host.ip` |
| `job` | `service.name` |
| `namespace` | `k8s.namespace.name` |
| `pod` | `k8s.pod.name` |
| `container` | `k8s.container.name`, `container.name` |
| `node` | `k8s.node.name`, `host.name` |
| `cluster` | `k8s.cluster.name`, `orchestrator.cluster.name` |
| `hostname` | `host.name`, `nodename` |

When live `_field_caps` are available, the resolver checks which candidate
actually exists in the target cluster and picks the first match.

### Customizing Field Mapping via Rule Packs

Rule packs provide the Grafana-side equivalent of Datadog custom field
profiles. Under the `query:` section, a rule pack can specify
`label_rewrites` to override default resolution, `label_candidates` to
extend the candidate list, and `ignored_labels` to suppress labels that
should not appear in target queries. The `controls:` section can override
field names used by Kibana dashboard controls.

```yaml
query:
  label_rewrites:
    instance: my_custom_host_field
    job: my_custom_service_field

  label_candidates:
    datacenter:
      - cloud.region
      - cloud.availability_zone

  ignored_labels:
    - __name__

controls:
  field_overrides:
    job: service.name
    instance: service.instance.id
```

Load a rule pack with:

```bash
.venv/bin/grafana-migrate \
  --source files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --rules-file my-rule-pack.yaml \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY"
```

To emit a validated starter rule-pack template:

```bash
.venv/bin/obs-migrate extensions --source grafana --format yaml --template-out custom-rule-pack.yaml
```

### Comparison with Datadog Field Profiles

| Aspect | Grafana (SchemaResolver + rule packs) | Datadog (FieldMapProfile) |
|---|---|---|
| Operator model | Plan with `--field-profile`, then verify with `--es-url` | Same plan→verify model; **no `auto`** |
| Metric name mapping | Planned profile rewrites (`otel`, Fleet remote_write, Metricbeat nested, native, passthrough) | Explicit `metric_map` + automatic dot-to-underscore + optional prefix/suffix |
| Tag / label mapping | `SchemaResolver` with multi-level priority and live verification | `tag_map` dictionary with optional `tag_prefix` fallback |
| Customization | `--metric-map-file` for metric renames; rule-pack YAML (`--rules-file`) for advanced Grafana rules | `--metric-map-file` for metric renames; custom profile YAML (`--field-profile path.yaml`) for advanced Datadog profiles |
| Live field discovery | `--es-url` verifies the plan; does not silently remap | `--es-url` loads `_field_caps` into the profile |
| Built-in defaults | Prometheus → OTel candidate list | Per-profile tag maps (OTel, Prometheus, Elastic Agent) |
| Named profiles | `otel`, `prometheus_remote_write`, `prometheus_metrics`, `prometheus_native`, `passthrough`, `auto` (Grafana-only) | `otel`, `elastic_agent`, `prometheus` (Metricbeat), `prometheus_native` (ES `/_prometheus`), `passthrough`, or YAML path |

### Grouping Template Variables (Late-Bound `by ($var)`)

Grafana dashboards often expose the grouping dimension as a template variable
(`sum(rate(metric[5m])) by ($grouping)`), so the viewer picks the breakdown at
view time. This is a *late-bound* grouping dimension: the exact field is unknown
at migration time.

- **Pure `by ($var)` → interactive ES|QL field control.** When the target binds
  ES|QL named parameters (`esql_named_param_binding`, probed from `--es-url`) and
  the variable resolves to a set of selectable target fields, the dimension is
  migrated to a Kibana ES|QL identifier/field control (`variable_type: fields`).
  The query emits `STATS ... BY grouping = ??grouping`: the `??grouping`
  identifier binds to the viewer's selection, while the aggregated column keeps
  the **stable alias** `grouping` so the Lens breakdown accessor always resolves
  the same column. The control's `choices` come from the variable's option list
  and its current value becomes the default. For **custom** variables, options
  stay as control choices even when live schema discovery remaps them to a
  profile path that is not present yet (e.g. `exporter` → missing
  `labels.exporter`) — that is data readiness, not an empty choice set.
- **Concrete label alongside the variable → graceful degrade (collision fix).**
  `by (exporter, $grouping)` is **not** turned into a shared field control. One
  Lens XY breakdown accessor cannot safely follow a field control whose choices
  may collide with the concrete grouping column (`exporter`), which produced a
  "Provided column name or index is invalid" render error. Instead the explicit
  `exporter` grouping is kept and the optional `$grouping` selector is dropped
  with a warning, so the panel still renders. Re-add the extra breakdown in
  Kibana if needed.
- **Not feasible (degrade gracefully).** `without ($var)` (ES|QL grouping is
  positive), multiple variables in one clause (a single XY breakdown cannot host
  several field controls), an empty choice set after resolution (e.g. query
  variables whose options cannot be resolved), no
  `esql_named_param_binding` capability, and query shapes that cannot carry the
  identifier (e.g. two-stage counts, binary expressions) all stay
  `not_feasible`. A validator reverts to `not_feasible` if a deferred `??var`
  never reached the emitted query, so a grouping dimension is never silently
  dropped. If another panel uses the same variable as a value parameter
  (`?var`), the late-bound grouping panel also stays `not_feasible`: one
  dashboard control cannot bind the same name as both a value and an identifier,
  so the existing value-bound panel/control is preserved instead.

This is exercised by the late-bound grouping render-audit canary
(`build_late_bound_grouping_canary`) so the interactive control and the
collision degrade are both proven to render in Kibana (see `docs/testing.md`).

### Chained/Label-Filtered Query Variables And Control Warnings (Issue #269)

Grafana query variables can chain: `label_values(metric{instance="$instance"},
id)` scopes `$id`'s option list to whichever `$instance` is currently
selected. Two things follow from Kibana ES|QL controls having no
cross-control dependency mechanism (a control's populate-query cannot read
another control's live selection):

- **The chained scope itself degrades, not silently.** The migrated `$id`
  control still works and still lists real values, but it lists *every* `id`
  rather than only the ones under the selected `$instance` — the control is
  broader than the Grafana source, not broken. This degradation is recorded
  as a `MigrationResult.control_warnings` entry (`"variable 'id' is scoped by
  $instance in Grafana ... Kibana ES|QL controls cannot express that
  inter-control dependency ..."`), printed under `CONTROL WARNINGS` in the
  CLI summary, included in the Markdown summary warning worklist, and recorded
  per-dashboard in the JSON report, migration manifest, and preflight report
  (`control_warnings`), rather than only being discoverable by reading the
  emitted ES|QL. Unsupported visible `query_result()` variables, textbox
  variables, unresolved fields, incompatible field types, and non-aggregatable
  fields use the same surfaced warning path.
- **A control whose target field is absent is kept with a data-readiness
  warning.** When live schema discovery (`--es-url`) positively confirms a
  variable's resolved field doesn't exist on the target, the control remains
  in the dashboard so offline and live migrations have the same structure,
  any panel `?var` binding remains valid, and the dropdown can self-heal once
  telemetry containing the field arrives. Its option list may be empty until
  then, so a matching `control_warnings` entry explains the data-readiness
  gap. Controls have no `PanelResult`-style per-item tracking of their own, so
  `control_warnings` is dashboard-scoped rather than per-control.

### Variable Label Filters (`metric{label="$var"}` → `?var`)

When a dashboard's templating list defines named variables used in PromQL label
matchers, dashboard translation enables ES|QL named-parameter binding for that
pass (unless a live `--es-url` probe already recorded that the cluster cannot
bind). Matchers become `WHERE field == ?var` / `RLIKE ?var`, and
`_ensure_param_controls` synthesizes a binding control for every emitted
parameter (issue #131 / #132).

- Offline single-panel translation without templating still drops `$var`
  matchers and warns — that path has no controls to bind.
- A verified-unsupported probe state is never overridden: no unbound `?var` is
  uploaded.

Exercised by `build_label_matcher_param_canary` (also uploaded by
`scripts/run_render_audit_local.sh`).

## Command Coverage

Grafana command examples and the canonical shared migration contract are
centralized in `docs/command-contract.md` to avoid duplication and stale
snippets.

Use that doc for:
- dedicated Grafana migration flows (`grafana-migrate`)
- unified `obs-migrate migrate --source grafana`
- the asset scope contract (`--assets {dashboards,alerts,all}` plus the
  deprecated `--fetch-alerts` alias)
- integrated `--smoke`, `--browser-audit`, and `--capture-screenshots` migration flows
- extension catalog commands
- standalone post-upload smoke validation commands

## Grafana-Specific Notes

- `--assets {dashboards,alerts,all}` is the canonical selector on both the
  dedicated and unified migration surfaces. `--fetch-alerts` remains only as a
  deprecated compatibility alias. Using the alias always emits a deprecation
  warning; if the requested asset selection is `dashboards`, including explicit
  `--assets dashboards`, runtime normalization upgrades the run to `--assets all`.
- Dashboard artifacts are written under `<output-dir>/dashboards`; alert
  artifacts are written under `<output-dir>/alerts`.
- Native PromQL is the preferred, highest-fidelity target. When
  `--es-url` reaches a target *confirmed* to lack ES|QL `PROMQL` support, the
  run downgrades to ES|QL translation; an inconclusive probe keeps native PromQL
  and warns. `--translation-mode {auto,native,esql}` can override the automatic
  decision when an operator needs to request native PROMQL where supported or
  disable native PROMQL and force ES|QL translation.
- `--source api` (or unified `--input-mode api`) pulls dashboard documents over
  HTTP basic auth. Connection details are **flag-first with env fallback**:
  `--grafana-url` / `--grafana-user` / `--grafana-pass` default to `GRAFANA_URL`
  / `GRAFANA_USER` / `GRAFANA_PASS`; `--grafana-token` (env `GRAFANA_TOKEN`) is
  the bearer-token alternative.
- `--ca-cert <path>` (env `OBS_MIGRATE_CA_CERT`) and `--insecure` (env
  `OBS_MIGRATE_INSECURE`) control TLS verification for all outbound connections
  (Grafana, Elasticsearch, Kibana, and the Node upload step). Prefer `--ca-cert`
  for private CAs; `--insecure` disables verification for testing only.
- `--dataset-filter` and `--logs-dataset-filter` control the emitted dashboard
  filters when you need non-default dataset wiring.
- All 10 standard Grafana threshold operators are supported when translating a
  single-condition threshold alert: `gt` (Is above), `lt` (Is below), `eq` (Is
  equal to), `ne` (Is not equal to), `gte` (Is above or equal to), `lte` (Is
  below or equal to), `within_range` / `outside_range` (exclusive bounds), and
  `within_range_included` / `outside_range_included` (inclusive bounds). The
  emitted ES|QL `WHERE` clause fires on exactly the same values as the Grafana
  source, preserving the exclusive-vs-inclusive range distinction.
- `--create-alert-rules` runs after an alert-capable asset selection and writes
  `<output-dir>/alerts/alert_rule_upload_results.json`.
- `--rules-file` / `--plugin` extend deterministic translation without editing
  core code.
- `--preflight`, `--polish-metadata`, and `--review-explanations` remain
  Grafana-specific workflow helpers; use the canonical command doc for the
  audited CLI surfaces around upload, smoke, and shared target management.
- Dashboard-level `links[]` of type `"link"` (a concrete external URL) are
  synthesized into a real native Kibana `links` panel appended at the end of
  the dashboard (see `docs/targets/kibana.md#links-and-image-panels`).
  Tag-driven `type: "dashboards"` links, relative URLs, and URLs containing
  inline Grafana variables have no safely resolvable destination at translation
  time and are only surfaced as manual-navigation notes in the migration
  report. External links that request `includeVars` or `keepTime` are emitted
  with a migration warning because Kibana links panels cannot append Grafana
  variables or inherit the dashboard time range automatically. The `dashlist`
  *panel* type (a dynamic, search-driven widget with no fixed link list) is
  unaffected and still skipped -- see `GRAFANA_SKIPPED_PANEL_TYPES`.

For overlay-driven authoring before exporting YAML, a matching starter CUE file
is available at `examples/cue/grafana-rule-pack.cue`.

## Current Boundaries

- Some PromQL families still degrade to `not_feasible` or manual review, especially subqueries, `bottomk`/`count_values`, bare classic `_bucket` series without `sum by (le)`, known-wrong histogram field types (for example `aggregate_metric_double`), generic `sum(A/B)` that is not a `_sum`/`_count` pair, `__name__` introspection, and multi-branch join/or cases that cannot fuse.
- `histogram_quantile` with a standard `sum(... by (le))` shape translates to ES|QL `PERCENTILE()` when the base field is a histogram / exponential_histogram, or when the type is unknown (offline / no field caps): unknown types **assume** `exponential_histogram` and warn so operators can pin the mapping. Prefer ES ≥ 9.5 native `histogram_quantile` when available; the `PERCENTILE` path is approximate (t-digest).
- Histogram mean idioms `sum(increase|rate(m_sum) / increase|rate(m_count))` approximate as a ratio of aggregates (`sum(m_sum)/sum(m_count)`) with an explicit warning; unrelated per-element ratios stay `not_feasible`.
- Multi-target XY panels fuse when series share a compatible ES|QL shape. Summary panels (`stat` / `singlestat` / `gauge` / `bargauge` / table) use the same compatibility group and approximate multi-series stats as a summary table when needed. Grouping mismatches where one target's groups are a subset of another's (e.g. QoS `by (qos_class)` + ungrouped total) union the BY fields with a warning. Divergent label filters on otherwise identical measures CASE-inline into the shared `STATS` (including window-less `LAST_OVER_TIME`, used by Express-style status-class counters). `legendFormat` `{{label}}` placeholders on `rate`/`irate`/`increase` (and other TS paths covered by issue #99) are display hints — they become series aliases, not `BY` dimensions — so overlays like Redis in/out rates can share one panel. Targets that remain incompatible (Windows vs Linux metrics, complex `or`/`label_replace` trees) still keep the largest compatible group and warn; Windows-specific drop wording only applies when every dropped target is a `windows_*` metric.
- Kibana ES|QL visualizations are still effectively single-query / single data layer. Independent Grafana queries that cannot fuse into one wide ES|QL statement cannot be overlaid the way Grafana does; that is a platform limit, not a silent drop.
- Mixed-datasource and mixed-query-language panels are still weaker than single-source Prometheus or Loki paths.
- Verification is strongest when live Prometheus/Loki and Elasticsearch are available, but full measured source-vs-target comparison is still partial.
- Live API extraction is dashboard-first today; broader Grafana asset families are not first-class migration inputs, and the current search request is capped at 500 dashboards.

## Adapter Location

`observability_migration/adapters/source/grafana/`

Important modules:

- `adapter.py`: adapter registration for the unified CLI.
- `cli.py`: end-to-end migration orchestration.
- `extract.py`: dashboard extraction from files or the Grafana API.
- `panels.py`: panel translation, layout normalization, `DashboardIR` assembly, and derived native/YAML emission.
- `translate.py`, `promql.py`, `rules.py`, `schema.py`: query translation core.
- `preflight.py`, `verification.py`: readiness and verification artifacts.
- `observability_migration/adapters/source/grafana/smoke.py` and `observability_migration/adapters/source/grafana/validate_uploaded_dashboards.py`: post-upload saved-object validation.

---

**See also:** [Grafana Pipeline Trace](grafana-trace.md) — auto-generated per-dashboard translation traces | [Shared Pipeline Overview](../pipeline-trace.md)
