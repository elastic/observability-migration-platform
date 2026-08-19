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
  -> translate_dashboard() (assemble DashboardIR; derive native payload)
  -> optional metadata polish (rebuild DashboardIR; re-derive native payload)
  -> optional emitted-query validation and IR sync (same IR rebuild)
  -> persist native/IR review artifacts
  -> optional upload (typed API, native_dashboard derived from IR)
  -> optional integrated smoke validation / browser audit / screenshot capture
  -> verification packets and report artifacts
  -> optional preflight probes and schema contract
  -> rollout plan
```

| Stage | Primary code | What happens |
|---|---|---|
| Setup | `cli.py`, `rules.py`, `schema.py` | Load rule packs/plugins, configure dataset filters, build `SchemaResolver`, discover fields when `--es-url` is present |
| Extract | `extract.py` | Read dashboards from files or Grafana API |
| Translate + emit | `panels.py`, `translate.py`, `promql.py` | Choose native `PROMQL`, rule-engine ES\|QL, LLM fallback, or native ES\|QL reuse; map panels; assemble `DashboardIR`; derive the native Dashboards API payload from it. No dashboard YAML is written |
| Feature-gap extraction | `links.py`, `annotations.py`, `alerts.py`, `transforms.py` | Collect reviewer-facing artifacts for non-query surfaces |
| Optional validate | `esql_validate.py` | Validate emitted target queries against Elasticsearch, auto-fix safe cases, and manualize broken ones |
| Native/IR review artifacts | `targets/kibana/native_artifacts.py` | Persist `dashboards/native/*.native.json`, `dashboards/ir/*.ir.json`, and `dashboards/native/index.json` after final IR/native regeneration so review artifacts match an immediate upload |
| Optional upload | `targets/kibana/adapter.py`, `dashboards_api.py`, `native_artifacts.py` | Typed API upload of the in-memory `native_dashboard` derived from the IR; standalone `obs-migrate upload --artifact-dir` uploads the persisted `native/*.native.json` byte-for-byte. There is no YAML input and no fallback renderer |
| Optional integrated smoke | `cli.py`, `targets/kibana/smoke.py`, `smoke_integration.py` | Validate uploaded dashboards, optionally run browser audit / screenshots, then merge post-upload smoke results back into the migration evidence |
| Verification + reporting | `verification.py`, `report.py`, `manifest.py`, `rollout.py` | Build semantic gates, save reports/manifests/verification packets, and generate rollout guidance |
| Optional preflight mode | `preflight.py` | Probe source inventory, target readiness, and required target contract for readiness assessment |

Important detail: Grafana `translate_dashboard()` is a broad stage. It already
includes layout normalization, variable/control translation, `DashboardIR`
assembly, and the derived native payload, not just query translation. It writes
nothing to disk and returns just the `MigrationResult` (it no longer takes an
`output_dir` or returns a YAML path).

Bundled Grafana curated packs can change query semantics, variable/control
behavior, and final Kibana geometry. Query fixes land through
`panel.query_overrides`; scope-only or misleading variable controls can be
suppressed or rewritten through curated-pack plugins; layout fixes land through
`panel.layout_overrides` after the standard Kibana layout transform and before
final overlap cleanup.

The console pipeline is **5 stages**, not 7: `[1/5] Extracting dashboards`,
`[2/5] Translating dashboards`, `[3/5] Verification-packet ES|QL validation`,
`[4/5] Writing native Dashboard-as-Code review artifacts`, `[5/5] Generating
report`, followed by an unnumbered `Rollout plan & feature summaries` step. The
old `[4/7] Linting generated dashboard YAML` and `[5/7] Compiling YAML ->
Kibana NDJSON via kb-dashboard-cli` stages were removed.

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

When live discovery is decisive enough to help but not safe enough to remap
automatically, the CLI now prints operator guidance directly:

- `suggested_field_profile=<profile>` when live caps clearly match another
  named Prometheus layout
- `Next step: ...` with the exact re-run guidance

The same guidance is also recorded under `operator_guidance` in
`required_target_contract.json`.

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
Author maps for your environment with `--metric-map-file` (and
`obs-migrate metric-map scaffold` to list unmapped sources); the tool does not
ship a dashboard-specific Prom→OTel dictionary or auto-suggest renames.
Preflight
`required_target_contract.json` lists required fields (a worklist, not a full
Prom→OTEL dictionary) and may include `mapped_from` when source ≠ target.
Unmapped Prometheus recording-rule names (colon-separated) are flagged in
panel notes and contract gaps so empty panels are not mistaken for ordinary
rename misses.

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

> **Important limitation:** Grafana has strong built-in support for
> Prometheus-shaped targets and OTel-style field selection, but it does **not**
> currently ship a built-in `elastic_agent` schema profile for ECS/system-metric
> targets. If your target stores semantically renamed system fields rather than
> Prometheus metric names, expect explicit `--metric-map-file` and/or rule-pack
> overrides.

> **Feasibility is invariant to `--es-url`.** The feasibility verdict answers
> only *was the panel translated successfully?* A panel that translates into
> valid ES|QL but whose source metric or grouping field has not yet been
> ingested is **not** reclassified as `not_feasible` — that is a transient *data
> readiness* condition surfaced as a warning (`... is missing from live schema
> discovery (data readiness, not translation infeasibility)`), owned by the
> telemetry-preparation step. `not_feasible` is reserved for constructs the
> target genuinely cannot express, so pointing the same run at a populated vs.
> empty cluster yields the same per-panel verdict.

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
obs-migrate migrate \
  --source grafana \
  --input-mode files \
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

### Template Variables in Metric / Label Names

A Grafana template variable that forms part of the **metric or label name** is
unresolvable at migration time and always degrades to `not_feasible` with a
clear warning — never a silent or garbage query. Three forms are caught:

| Form | Example | Guardrail |
|------|---------|-----------|
| Dynamic **function name** | `${metric:value}(series[5m])` | `_TEMPLATE_FUNC_VAR_RE` |
| **Prefix**-glued to identifier | `${prefix:raw}metric_total` | `_PREFIX_GLUED_TEMPLATE_VAR_RE` |
| **Suffix**-glued to identifier | `metric_total${suffix}` | `_GLUED_TEMPLATE_VAR_RE` |

All three are caught by the priority-5 `template_variable_guardrail_rule`
preprocessor, before any ES|QL is rendered, so the result carries an empty
`esql_query` and a warning naming the specific variable (e.g. `$prefix`).
Grafana's own built-in macros (`${__range_s}`, `${__rate_interval}`, …) start
with `__` and are excluded from the prefix-glued check; they are expanded by
`preprocess_grafana_macros` at priority 10.

The prefix-glued check deliberately ignores **templated durations**: in
`metric[${step}m]` (range), `metric[5m:${step}m]` (subquery resolution), and
`metric offset ${off}h` (offset modifier) the variable is a duration, not a
metric or label name, and the surrounding metric is concrete. Matching it here
would emit a misleading "metric or label name is built from a Grafana template
variable" diagnostic that blames the (concrete) metric name. Rather than
special-case where the variable sits, the guardrail **strips range/subquery
selectors and offset values first** (`_RANGE_SELECTOR_RE`, `_OFFSET_MODIFIER_RE`)
and then scans what remains — so a template variable anywhere in an actual
identifier, including a recording-rule name (`${env}:job:rate` **or**
`job:${env}:rate`, whose variable segment follows a colon), still degrades with
the dynamic-name warning, while every duration variable is removed regardless of
its position.

### Chained/Label-Filtered Query Variables And Control Warnings (Issue #269)

Grafana query variables can chain: `label_values(metric{instance="$instance"},
id)` scopes `$id`'s option list to whichever `$instance` is currently
selected.

- **On targets that bind named ES|QL params inside values-query controls, the
  chained scope is preserved.** The migrated `$id` control keeps the source
  dependency by emitting a parameterized populate query, for example
  `... WHERE (?instance == "" OR labels.instance RLIKE ?instance ...)`.
  This was verified against the local Kibana `9.5.0-SNAPSHOT` stack in August
  2026: selecting an upstream control narrowed the downstream control's option
  list at runtime.
- **When that capability is not available, the chained scope still degrades
  loudly rather than silently.** The migrated `$id` control still works and
  still lists real values, but it lists *every* `id` rather than only the ones
  under the selected `$instance`. That degradation is recorded as a
  `MigrationResult.control_warnings` entry, printed under `CONTROL WARNINGS`
  in the CLI summary, included in the Markdown summary warning worklist, and
  recorded per-dashboard in the JSON report, migration manifest, and preflight
  report (`control_warnings`), rather than only being discoverable by reading
  the emitted ES|QL. Unsupported visible `query_result()` variables, textbox
  variables, unresolved fields, incompatible field types, and non-aggregatable
  fields use the same surfaced warning path.
- **Cascade parents are not treated as inert.** A variable such as `$namespace`
  that no panel binds, but that still appears in a dependent control's
  populate query (`?namespace` narrowing `$instance` options), is kept without
  the "renders but changes no panel" warning — selecting it does change the
  downstream dropdown. The inert-control warning still fires when a variable is
  unused by both panels and other controls.
- **A control whose target field is absent is kept with a data-readiness
  warning.** When live schema discovery (`--es-url`) positively confirms a
  variable's resolved field doesn't exist on the target, the control remains
  in the dashboard so offline and live migrations have the same structure,
  any panel `?var` binding remains valid, and the dropdown can self-heal once
  telemetry containing the field arrives. Its option list may be empty until
  then, so a matching `control_warnings` entry explains the data-readiness
  gap. Controls have no `PanelResult`-style per-item tracking of their own, so
  `control_warnings` is dashboard-scoped rather than per-control.

### Interval, Custom, And Other Non-Query Variables (Issue #356)

Grafana `interval` variables (a dropdown of durations, e.g. `20s,1m,5m`) have
no Kibana control equivalent and are intentionally skipped by
`interval_variable_rule` — Kibana's time picker controls the *displayed*
range, which is the variable's most common use. `custom` variables (a static
comma-separated value list) are also skipped by default; if one is referenced
as `$var`/`?var` inside a panel query, `_ensure_param_controls` (issue #131)
synthesizes a binding control after translation, but a `custom` variable never
referenced that way has nothing to bind.

Neither skip is safe when the variable is doing more than that. Dashboard
9852's `RateInterval` is the sharp counter-example: 16 targets use it as the
**rate window** (`rate(node_disk_written_bytes_total[$RateInterval])`), which
has nothing to do with the time picker — Grafana keeps the rate window fixed
at, e.g., `1m` regardless of the displayed range so the line stays smooth. A
duration variable used this way, or any other variable type that ends up with
no control *and* no `?var` binding, is not equivalent to "handled by the time
picker" — it silently hands control of the window to a translator-chosen
substitute. ES|QL panels pick a `TBUCKET` bucket-width; native PROMQL panels
typically inline a fixed range (dashboard 9852's disk panels become
`rate(...[5m])` even when Grafana's current `RateInterval` was `1m`). Neither
tracks the source value, and either can differ from it in either direction.

`translate_dashboard` therefore runs one disclosure pass after every control
has been synthesized (variable translation, `_ensure_param_controls`,
late-bound group controls, `?var` retargeting): for every templating-list
variable that is not bound to a control — checking both the ES|QL
`variable_name` key and the classic options/range `_source_variable_name` key
(a classic control never sets `variable_name`, so looking at that key alone
would falsely flag a working dropdown as dropped) — it checks whether any
panel's *original* PromQL `expr` still references `$var` / `${var}`. If so, it
appends a `control_warnings` entry naming the variable, so the loss is printed
under `CONTROL WARNINGS` and recorded in the JSON report / migration manifest
/ preflight report, matching every other control degradation on this page.
`interval` variables get a specific message calling out both the ES|QL
`TBUCKET` substitute and the native PROMQL fixed-range inline; every other
type gets a generic "referenced but dropped" message. A variable that is
genuinely unused by every panel is never warned about — there is nothing lost
to disclose. This is disclosure only: the variable is not migrated into a
working control (that would require parameterizing the ES|QL duration literal,
which is unverified and out of scope for this fix).

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
- Native `PROMQL` is preferred for control-bound label matchers by default
  (no `--kibana-url`, or an inconclusive Kibana version probe), matching the
  offline native-PROMQL posture. Panels that still cannot stay native fall
  through to ES|QL via the existing translator / live-validator gates.
  When `--kibana-url` reports Kibana 9.5+, control binding is confirmed
  (elastic/kibana#271244). A verified Kibana older than 9.5 (for example 9.4)
  forces the ES|QL `RLIKE ?var` path as a safety net — the supported product
  floor remains Kibana 9.5+ (`minimum_kibana_version: 9.5.0`).

Exercised by `build_label_matcher_param_canary` (also uploaded by
`scripts/run_render_audit_local.sh`).

### Regular Controls vs. Variable (ES|QL) Controls (Issue #312)

Kibana has two dashboard control shapes, and a migrated Grafana template
variable becomes one or the other by a single deterministic rule — whether the
target can **bind ES|QL named parameters** for that migration pass:

- **Variable (ES|QL) control** (`type: esql`, `variable_type: values`) — emitted
  when named-parameter binding is available. Panel `$var` label matchers are
  rewritten to native ES|QL parameters (`WHERE field == ?var` / `RLIKE ?var`),
  so the control must *define* that parameter. Because a dashboard's templating
  list enables binding for the pass (see *Variable Label Filters* above, unless
  a live `--es-url` probe recorded the cluster cannot bind), **every** variable
  on a templated dashboard — including ones no panel references — becomes a
  variable control. This keeps all variables the same shape (a dashboard does
  not end up with one regular control and one variable control) and preserves
  Grafana parity for unused variables instead of quietly downgrading them.
- **Regular options/range control** (`type: options` / `type: range`, backed by
  a `field` + `data_view`) — emitted only when the target cannot bind ES|QL
  parameters at all. Then `$var` matchers are dropped-and-warned (no `?var` to
  bind), so a generic data-view control is the faithful shape and, again, every
  variable uses it consistently.

The split is therefore *per target capability*, never per individual variable.

**Multi-select.** A Grafana `multi: true` variable stays multi-select in
Kibana. Scalar `== ?var` / `RLIKE ?var` cannot bind an array, so the matcher is
emitted as `MV_CONTAINS(TO_STRING(?var), ".*") OR MV_CONTAINS(TO_STRING(?var), field)`
with `single_select: false`. The `".*"` sentinel preserves Grafana's All option.
`TO_STRING` (issue #353) keeps that guardrail type-safe: Elasticsearch infers
the bound parameter's type from the JSON values Kibana sends, so numeric-looking
options (CPU indices, ports, PIDs) can arrive as an integer array and fail
compile-time type verification against the keyword sentinel/field. Some Kibana
builds still send those options as keyword strings (`["0","1"]`); `TO_STRING` on
an already-keyword value is a no-op, so the wrap is unconditional. Matching is
exact rather than regex (`RLIKE` rejects a computed pattern). Regular options
controls, which do not bind an ES|QL parameter, keep the source `multi` flag.

**Value-list filters.** A `label_values(metric{device!="nbd1"}, device)`
variable restricts its option list to series matching the selector. The
migrated ES|QL values control preserves those *literal* matchers as extra
`WHERE` predicates (`... AND device != "nbd1"`), so excluded values (`nbd1`) do
not appear in the dropdown. Supported operators map as `=`/`!=` →
`==`/`!=` and `=~`/`!~` → `RLIKE`/`NOT RLIKE`. Selector matchers that reference
*another* template variable (`{instance="$instance"}`) are the chained-scope
case above (issue #269): on param-capable targets they stay as `?var`
predicates inside the control query; otherwise they cannot be expressed as a
fixed predicate and are surfaced as a `control_warnings` degradation instead.

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

## Dashboard and Panel Time State

- The source dashboard's own time window (`dashboard.time.{from,to}`) and
  auto-refresh (`dashboard.refresh`) are normalized onto `DashboardIR.time_range`
  / `.refresh_interval` and carried straight through to the Dashboards API's
  own `time_range: {from, to, mode}` / `refresh_interval: {pause, value}`
  fields -- not through the deprecated kb-dashboard-core YAML shape, which has
  no slot for `refresh_interval` at all and no `mode` on `time_range`. A
  relative bound (`now-6h`, `now/d`) passes through unchanged as Elasticsearch
  date math; an absolute bound (epoch milliseconds, 13+ digits) converts to
  ISO 8601. A bare shorter all-digit string (e.g. epoch seconds) is refused
  rather than misread as milliseconds. Kibana only restores a window when
  both `from` and `to` are present, so a one-sided source range is dropped
  with a warning instead of being emitted and then flagged lossy on upload.
  `dashboard.refresh` (e.g. `"30s"`) converts to milliseconds; an explicit
  off (`refresh: false`, `""`, or `null`) emits `{pause: true, value: 0}` so
  the author's "do not auto-refresh" intent survives a target Kibana whose
  own default may auto-refresh. A missing `refresh` key leaves
  `refresh_interval` unset so Kibana keeps its default. An unrecognized
  `from`/`to`/`refresh` value is dropped with a `control_warnings` entry
  instead of shipping something the API would reject.
- A panel's "Override relative time" (`timeFrom`, e.g. `"6h"`) becomes that
  panel's own `esql.time_range` -- Kibana's per-panel `time_range` override,
  applied uniformly across every ES|QL chart type. `timeShift` (a "compare to
  last week"-style window shift) has no Dashboards API equivalent -- the API's
  panel `time_range` is an absolute override, not a shift -- so it degrades
  gracefully to a migration-report warning instead of emitting a `time_range`
  that would silently show the wrong window. Setting both on the same panel
  keeps the `timeFrom` override and still warns about the dropped `timeShift`.

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
  `<output-dir>/alerts/alert_rule_upload_results.json`. If it was requested but
  no rules were created (no `--kibana-api-key`, unreachable alerting preflight)
  the run exits non-zero and records the reason under `alerts.rule_creation` in
  `run_summary.json`; a missing `--kibana-url` or non-alert `--assets` is
  rejected up front with exit `2`.
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

- `topk(k, expr)` and `bottomk(k, expr)` have two translation modes. Non-XY targets (for example `barchart`, `stat`, table-like snapshots) translate to an ES|QL `SORT value DESC/ASC | LIMIT k` over the latest bucket per group. XY targets (`graph`, `timeseries`, `trend`) keep the full time-series breakdown instead, because ES|QL has no subquery form that can both preserve time buckets and rank groups across the whole window; those panels therefore warn that all series are shown and the top-N filter is only approximated. Ungrouped forms still collapse to a single-series ranking with a `preferred_group_labels` hint. When a `drop_rate` metric_map entry remaps the inner metric to a gauge, both `topk`/`bottomk` and scaled-rate expressions (`sum(rate(...)) * scalar`) switch to `FROM` source automatically — using `TS` source on a gauge field would query the wrong index and inflate averages.
- Some PromQL families still degrade to `not_feasible` or manual review, especially subqueries, `count_values`, known-wrong histogram field types (for example `aggregate_metric_double`), generic `sum(A/B)` that is not a `_sum`/`_count` pair, `__name__` introspection, and multi-branch join/or cases that cannot fuse.
- `histogram_quantile` translates to ES|QL `PERCENTILE()` for both the common `sum(rate(bucket[w])) by (le)` shape and the bare `rate(bucket[w])` shape (no outer aggregation — `rate()` preserves `le` implicitly). An outer aggregation that explicitly drops `le` — e.g. `sum by (instance) (rate(bucket[w]))` — stays `not_feasible` because the bucket boundaries are destroyed. The target field must be a histogram or exponential_histogram; unknown types assume exponential_histogram and warn. Prefer ES ≥ 9.5 native `histogram_quantile` when available; `PERCENTILE` is approximate (t-digest).
- Label-enrichment info-metric joins (`metric * on(k) group_left(extra_labels) metric_info`) whose outer `by()` references the enrichment labels are now feasible for `*_info` metrics: the join is dropped and the outer aggregation runs directly on the primary metric with the enrichment labels as additional `BY` dimensions. A schema-check warning is added when the dimensions cannot be confirmed from live field capabilities.
- `label_join(v, dst, separator, src1, src2, ...)` translates to a post-`STATS` `| EVAL dst = CONCAT(src1, "separator", src2, ...)` when all source labels appear in the inner expression's `by()` clause. If any source label is absent from the `by()` clause, the panel stays `not_feasible` (the column would not exist in the `STATS` output and `CONCAT` cannot reference it).
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
- `panels.py`: panel translation, layout normalization, `DashboardIR` assembly, and the derived native payload.
- `translate.py`, `promql.py`, `rules.py`, `schema.py`: query translation core.
- `preflight.py`, `verification.py`: readiness and verification artifacts.
- `observability_migration/adapters/source/grafana/smoke.py` and `observability_migration/adapters/source/grafana/validate_uploaded_dashboards.py`: post-upload saved-object validation.

---

**See also:** [Grafana Pipeline Trace](grafana-trace.md) — auto-generated per-dashboard translation traces | [Shared Pipeline Overview](../pipeline-trace.md)
