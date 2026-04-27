# Grafana Source Adapter

## Overview

The Grafana adapter is the most mature source path in the platform. It handles
file and API extraction, panel translation, verification artifacts, preflight
reporting, optional upload, and post-upload smoke validation.

Grafana query translation has four paths:

1. **Native PROMQL** (`--native-promql`): wraps compatible PromQL in
   `PROMQL index=... value=(expr)` for highest fidelity on Elastic Serverless.
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
  -> translate_dashboard() and emit YAML
  -> optional metadata polish
  -> optional emitted-query validation and YAML sync
  -> YAML lint
  -> compile and layout validation
  -> optional upload
  -> optional integrated smoke validation / browser audit / screenshot capture
  -> verification packets and report artifacts
  -> optional preflight probes and schema contract
  -> rollout plan
```

| Stage | Primary code | What happens |
|---|---|---|
| Setup | `cli.py`, `rules.py`, `schema.py` | Load rule packs/plugins, configure dataset filters, build `SchemaResolver`, discover fields when `--es-url` is present |
| Extract | `extract.py` | Read dashboards from files or Grafana API |
| Translate + emit | `panels.py`, `translate.py`, `promql.py` | Choose native `PROMQL`, rule-engine ES\|QL, LLM fallback, or native ES\|QL reuse; map panels; emit YAML |
| Feature-gap extraction | `links.py`, `annotations.py`, `alerts.py`, `transforms.py` | Collect reviewer-facing artifacts for non-query surfaces |
| Optional validate | `esql_validate.py` | Validate emitted target queries against Elasticsearch, auto-fix safe cases, and manualize broken ones |
| Lint / compile / layout | `targets/kibana/compile.py` | Lint YAML, compile NDJSON, validate compiled layout |
| Optional upload | `targets/kibana/compile.py` | Upload only after lint/compile/layout gates pass |
| Optional integrated smoke | `cli.py`, `targets/kibana/smoke.py`, `smoke_integration.py` | Validate uploaded dashboards, optionally run browser audit / screenshots, then merge post-upload smoke results back into the migration evidence |
| Verification + reporting | `verification.py`, `report.py`, `manifest.py`, `rollout.py` | Build semantic gates, save reports/manifests/verification packets, and generate rollout guidance |
| Optional preflight mode | `preflight.py` | Probe source inventory, target readiness, and required target contract for readiness assessment |

Important detail: Grafana `translate_dashboard()` is a broad stage. It already
includes layout normalization, variable/control translation, and initial YAML
emission, not just query translation.

## Schema Resolution and Field Naming

Grafana dashboards use Prometheus label names (`instance`, `job`, `namespace`)
and metric names (`node_cpu_seconds_total`) that may not match the Elasticsearch
field names in the target cluster. The Grafana adapter uses `SchemaResolver`
and rule packs to bridge this gap — this is the Grafana equivalent of Datadog's
field profiles.

### How Schema Resolution Works

`SchemaResolver` resolves Prometheus labels to target Elasticsearch fields
through a four-level priority chain:

| Priority | Source | How to configure |
|---|---|---|
| 1 (highest) | Rule-pack `label_rewrites` | `--rules-file custom-pack.yaml` |
| 2 | Live ES `_field_caps` discovery | `--es-url` flag |
| 3 | Built-in Prometheus → OTel candidate mappings | Always available offline |
| 4 (lowest) | Pass-through (use label as-is) | Default fallback |

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
  --native-promql
```

To emit a validated starter rule-pack template:

```bash
.venv/bin/obs-migrate extensions --source grafana --format yaml --template-out custom-rule-pack.yaml
```

### Comparison with Datadog Field Profiles

| Aspect | Grafana (SchemaResolver + rule packs) | Datadog (FieldMapProfile) |
|---|---|---|
| Metric name mapping | Not needed — PromQL metric names pass through to ES or are wrapped in `PROMQL` | Explicit `metric_map` + automatic dot-to-underscore + optional prefix/suffix |
| Tag / label mapping | `SchemaResolver` with multi-level priority and live discovery | `tag_map` dictionary with optional `tag_prefix` fallback |
| Customization | Rule-pack YAML (`--rules-file`) | Custom profile YAML (`--field-profile path.yaml`) |
| Live field discovery | `--es-url` feeds `SchemaResolver` | `--es-url` loads `_field_caps` into the profile |
| Built-in defaults | Prometheus → OTel candidate list | Per-profile tag maps (OTel, Prometheus, Elastic Agent) |

## Query Variables and ES|QL Parameters (Phase B)

Grafana query variables that resolve to a single ES|QL field, use `=` / `=~`
matchers without regex metacharacters, and don't drive panel `repeat` are
bound to ES|QL parameters: `WHERE field == ?varname` for single-select,
`WHERE MV_CONTAINS(?varname, field)` for multi-select. The corresponding
`ESQLQuery*SelectControl` populates the dropdown via a deterministic
`FROM <data_view> | WHERE <field> IS NOT NULL | STATS BY <field> | KEEP <field> | LIMIT 1000`
query. Variables that fail the classifier (e.g. `includeAll: true` with
single-select, regex template, datasource references) keep today's classic
options-control fallback.

See [`docs/roadmap/2026-04-27-kibana-variable-controls-design.md`](../roadmap/2026-04-27-kibana-variable-controls-design.md)
for the full design and
[`docs/roadmap/2026-04-27-kibana-variable-controls-implementation-plan.md`](../roadmap/2026-04-27-kibana-variable-controls-implementation-plan.md)
for the implementation plan.

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
- `--native-promql` prefers native PromQL over ES|QL translation for compatible
  panels.
- `--source api` (or unified `--input-mode api`) pulls dashboard documents
  through the env-driven HTTP basic-auth path; `--grafana-token` is the bearer
  token alternative for API access.
- `--dataset-filter` and `--logs-dataset-filter` control the emitted dashboard
  filters when you need non-default dataset wiring.
- `--create-alert-rules` runs after an alert-capable asset selection and writes
  `<output-dir>/alerts/alert_rule_upload_results.json`.
- `--rules-file` / `--plugin` extend deterministic translation without editing
  core code.
- `--preflight`, `--polish-metadata`, and `--review-explanations` remain
  Grafana-specific workflow helpers; use the canonical command doc for the
  audited CLI surfaces around upload, smoke, and shared target management.

For overlay-driven authoring before exporting YAML, a matching starter CUE file
is available at `examples/cue/grafana-rule-pack.cue`.

## Current Boundaries

- Some PromQL families still degrade to `not_feasible` or manual review, especially subqueries, `topk`, complex quantiles, and multi-branch join/or cases.
- Mixed-datasource and mixed-query-language panels are still weaker than single-source Prometheus or Loki paths.
- Verification is strongest when live Prometheus/Loki and Elasticsearch are available, but full measured source-vs-target comparison is still partial.
- Live API extraction is dashboard-first today; broader Grafana asset families are not first-class migration inputs, and the current search request is capped at 500 dashboards.

## Adapter Location

`observability_migration/adapters/source/grafana/`

Important modules:

- `adapter.py`: adapter registration for the unified CLI.
- `cli.py`: end-to-end migration orchestration.
- `extract.py`: dashboard extraction from files or the Grafana API.
- `panels.py`: panel translation, layout normalization, and YAML generation.
- `translate.py`, `promql.py`, `rules.py`, `schema.py`: query translation core.
- `preflight.py`, `verification.py`: readiness and verification artifacts.
- `observability_migration/adapters/source/grafana/smoke.py` and `observability_migration/adapters/source/grafana/validate_uploaded_dashboards.py`: post-upload saved-object validation.

---

**See also:** [Grafana Pipeline Trace](grafana-trace.md) — auto-generated per-dashboard translation traces | [Shared Pipeline Overview](../pipeline-trace.md)
