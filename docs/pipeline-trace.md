# Pipeline Trace: How Data Flows Through the Migration

> **This document is partially auto-generated.** Sections between
> `<!-- GENERATED:xxx -->` markers are refreshed by running:
>
> ```bash
> python scripts/audit_pipeline.py --update-docs
> ```
>
> Static narrative lives in `docs/pipeline-trace.tpl.md`. Per-source trace
> data lives in `docs/sources/grafana-trace.tpl.md` and
> `docs/sources/datadog-trace.tpl.md`. Edit the templates, then regenerate.

This document is the **shared architecture overview** for the migration
pipeline. For per-dashboard traces with source queries, translation steps, and
translated output, see the source-specific trace docs:

- [Grafana Pipeline Trace](sources/grafana-trace.md) — per-dashboard PromQL / LogQL → Kibana traces
- [Datadog Pipeline Trace](sources/datadog-trace.md) — per-dashboard metric / log / formula → Kibana traces

This is the **shared** pipeline contract, not the exact dedicated CLI sequence
for every source. The source adapters differ materially:

- Grafana runs a broader end-to-end flow with translation, IR-first emission (`DashboardIR` → native Dashboards API payload + YAML), optional emitted-query validation, lint, optional compile/layout, optional upload, verification, and rollout artifacts.
- Datadog runs a more explicit `normalize -> plan -> translate -> emit` flow with capability-aware preflight, the same IR-first emission, first-class emitted-query validation, optional compile, first-class upload, post-upload smoke validation, migration manifest and rollout artifacts, and live metric source execution during verification. The main remaining gap is broader source execution coverage for logs and multi-query widgets.

For both sources, `DashboardIR` is the primary working artifact after
translation/assembly; native upload payload and on-disk YAML are derived from
it. See `docs/architecture/asset-model.md`.

For the exact source-specific stage order, see `docs/architecture.md`,
`docs/sources/grafana.md`, and `docs/sources/datadog.md`.

---

## Cross-Source Summary

<!-- GENERATED:DASHBOARD_SUMMARY -->
| Source | Dashboard | Panels | Migrated | Warnings | Manual | Not Feasible | Skipped | Rows |
|--------|-----------|--------|----------|----------|--------|--------------|---------|------|
| grafana | Prometheus Blackbox Exporter | 10 | 9 | 1 | 0 | 0 | 0 | 2 |
| grafana | Diverse Panel Types Test | 10 | 3 | 7 | 0 | 0 | 0 | 1 |
| grafana | Docker and system monitoring | 15 | 6 | 8 | 0 | 1 | 0 | 0 |
| grafana | Docker and system monitoring | 20 | 11 | 6 | 0 | 3 | 0 | 0 |
| grafana | Express Prometheus Middleware | 23 | 1 | 20 | 0 | 2 | 0 | 1 |
| grafana | Flagger Canary Status | 34 | 1 | 31 | 0 | 2 | 0 | 4 |
| grafana | Home - Migration Test Lab | 6 | 3 | 2 | 0 | 1 | 0 | 0 |
| grafana | Kubernetes cluster monitoring (via Prometheus) | 21 | 15 | 6 | 0 | 0 | 0 | 0 |
| grafana | Kubernetes Cluster (Prometheus) | 29 | 1 | 28 | 0 | 0 | 0 | 6 |
| grafana | Kubernetes / Views / Global | 26 | 11 | 12 | 0 | 3 | 0 | 4 |
| grafana | Kubernetes Kafka | 32 | 21 | 11 | 0 | 0 | 0 | 3 |
| grafana | Multi Pattern Coverage | 10 | 5 | 4 | 0 | 0 | 1 | 1 |
| grafana | MySQL Overview | 36 | 7 | 12 | 0 | 17 | 0 | 14 |
| grafana | Node Exporter Full | 116 | 39 | 77 | 0 | 0 | 0 | 16 |
| grafana | NodeJS Application Dashboard | 9 | 5 | 3 | 0 | 1 | 0 | 0 |
| grafana | Prometheus 2.0 Overview | 30 | 1 | 29 | 0 | 0 | 0 | 0 |
| grafana | Prometheus 2.0 (by FUSAKLA) | 44 | 28 | 10 | 5 | 1 | 0 | 0 |
| grafana | Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha) | 12 | 7 | 5 | 0 | 0 | 0 | 0 |
| grafana | Redis Dashboard for Prometheus Redis Exporter 1.x | 13 | 9 | 3 | 0 | 1 | 0 | 0 |
| datadog | Apache - Overview | 22 | 12 | 8 | 1 | 1 | 0 | 0 |
| datadog | Celery Overview | 17 | 5 | 6 | 2 | 0 | 4 | 0 |
| datadog | Consul Overview | 27 | 7 | 11 | 4 | 0 | 5 | 0 |
| datadog | Docker - Overview | 28 | 6 | 19 | 1 | 2 | 0 | 0 |
| datadog | HAProxy - Overview | 29 | 9 | 13 | 1 | 0 | 6 | 0 |
| datadog | Kafka, Zookeeper and Kafka Consumer Overview | 55 | 13 | 28 | 3 | 2 | 9 | 0 |
| datadog | Kubernetes - Overview | 57 | 2 | 39 | 4 | 2 | 10 | 0 |
| datadog | MongoDB - Overview | 43 | 13 | 20 | 1 | 0 | 9 | 0 |
| datadog | MySQL - Overview | 11 | 0 | 11 | 0 | 0 | 0 | 0 |
| datadog | NGINX - Overview | 27 | 12 | 5 | 2 | 2 | 6 | 0 |
| datadog | Postgres - Metrics | 9 | 0 | 9 | 0 | 0 | 0 | 0 |
| datadog | RabbitMQ Overview (OpenMetrics Version) | 47 | 12 | 23 | 5 | 1 | 6 | 0 |
| datadog | Redis - Overview | 43 | 9 | 27 | 0 | 0 | 7 | 0 |
| datadog | System Overview - Sample | 11 | 8 | 2 | 1 | 0 | 0 | 0 |

**33 dashboards, 922 panels** audited from `infra/grafana/dashboards/` and `infra/datadog/dashboards/`.
<!-- /GENERATED:DASHBOARD_SUMMARY -->

<!-- GENERATED:VERDICT_SUMMARY -->
## Verdict Summary

| Verdict | Count | Meaning |
|---------|-------|---------|
| **CORRECT** | 252 | Translation is semantically accurate |
| **MINOR_ISSUE** | 452 | Translated with approximations — review recommended |
| **EXPECTED_LIMITATION** | 270 | Known unsupported feature — placeholder or skip |
<!-- /GENERATED:VERDICT_SUMMARY -->

<!-- GENERATED:WARNING_PATTERNS -->
## Top Warning Patterns

| Count | Warning |
|------:|---------|
| 220 | Scope filter with template variable could not be bound exactly; apply specific values via Kibana dashboard controls |
| 102 | Dropped variable-driven label filters during migration |
| 78 | Grafana panel description is not carried into Kibana YAML automatically |
| 66 | Approximated PromQL arithmetic using same-bucket ES\|QL math |
| 56 | XY chart shows a single breakdown; additional grouping dimension(s) ['job'] are in the query but not on the chart, so series differing only by those are visually merged |
| 42 | PromQL series labels were not retained; output is bucket-level and may collapse multiple source series |
| 32 | Grafana panel has 1 field override(s); verify visual mappings manually |
| 31 | Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value |
| 17 | PromQL 'or' between metrics that cannot be aligned in ES\|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped |
| 12 | Grafana panel has 1 link(s); verify drilldowns manually |
| 11 | Grafana panel has 2 field override(s); verify visual mappings manually |
| 9 | Panel has 2 PromQL targets but only 1 could be migrated |
| 9 | as_count interval semantics are approximated in ES\|QL |
| 7 | Panel has 3 PromQL targets but only 1 could be migrated |
| 7 | rollup interval is approximated in ES\|QL |
<!-- /GENERATED:WARNING_PATTERNS -->

---

## The Full Pipeline

```
Source dashboard files (Grafana JSON / Datadog JSON)
  │
  ▼
[1] EXTRACT — load dashboards, normalise structure, clean HTML
  │
  ▼
[2] INVENTORY — classify query language, detect features, assess readiness
  │
  ▼
[3] TRANSLATE — native PROMQL fast path, rule-engine ES|QL, or Datadog query translation
  │                         produces: emitted target query + QueryIR
  ▼
[4] ASSEMBLE — panel type mapping, layout normalisation, variable→control, display enrichment
  │              produces: DashboardIR (primary) → native Dashboards API payload + YAML
  │                        (+ VisualIR / OperationalIR snapshots for reporting)
  ▼
[5] POLISH (optional) — improve titles and labels (heuristic or AI); rebuild DashboardIR
  │
  ▼
[6] VALIDATE (optional) — run emitted target queries against Elasticsearch, fix/downgrade broken ones
  │                        (rebuild DashboardIR; re-derive native + YAML)
  ▼
[7] LINT — schema-validate derived YAML via kb-dashboard-lint
  │
  ▼
[8] COMPILE (optional) — YAML → Kibana NDJSON via kb-dashboard-cli (--compile / --legacy-import)
  │
  ▼
[9] VERIFY — build verification packets, assign semantic gates, refresh OperationalIR
  │
  ▼
[10] REPORT — write migration_report.json, manifest, verification packets
  │
  ▼
[11] UPLOAD (optional) — typed Dashboards API from native_dashboard (IR); YAML fallback for standalone upload
  │
  ▼
[12] SMOKE (optional) — validate uploaded dashboards in Kibana
```

---

## Step-by-Step Explanation

### Step 1 — Extraction

| Concern | What happens |
|---------|-------------|
| **Grafana** | Loads JSON, normalises `panels[]`, cleans HTML text panels via `markdownify`, injects `_source_file` metadata |
| **Datadog** | Normalises `widgets[]` into `NormalizedWidget` with unified `queries`, `children`, layout; parses `template_variables` |

Key details:

- Grafana text panels with `mode: "html"` are converted to Markdown — `<div>`,
  `<style>`, `<script>` wrappers are stripped.
- Grafana row panels (`type: "row"`) are structural separators that become
  section markers later.
- Datadog group/powerpack widgets are flattened into parent+children.
- Both paths inject source file metadata for downstream lineage tracking.

### Step 2 — Inventory & Analysis

Before translating, each panel is inspected to determine:

- **Query language** — PromQL, LogQL, ES|QL, Datadog metric/log/formula, or unknown
- **Datasource type** — prometheus, loki, elasticsearch, datadog, etc.
- **Mixed datasources?** — if yes, flagged as `requires_manual`
- **Special features** — transformations, field overrides, repeat variables, library panels, links

This analysis selects the translation path. A PromQL panel enters the PromQL
translator; a LogQL panel enters the LogQL path; a Datadog metric query enters
the Datadog adapter.

### Step 3 — Translation

**Grafana** has four translation paths, chosen automatically per panel:

1. **Native PROMQL** (the preferred path; when `--es-url` is set, target detection
   downgrades to ES|QL translation only if the `PROMQL` command is confirmed
   unsupported — an inconclusive probe keeps native and warns; `--translation-mode`
   can explicitly request native PROMQL where supported or force ES|QL) — wraps
   the original PromQL in `PROMQL index=… value=(expr)`. Highest fidelity.
2. **Rule-engine ES|QL** — parses PromQL AST via `promql-parser`, classifies,
   runs through priority-ordered translation rules, renders ES|QL.
3. **LLM fallback** (optional) — for `not_feasible` panels, asks an LLM.
4. **Native ES|QL** — passes through pre-existing Elasticsearch queries.

**Datadog** has per-query-type translators:

- **Metric queries** — `metric:field{tags}` → ES|QL with mapped fields, aggregation, grouping
- **Log queries** — faceted/grouped log searches → ES|QL with KQL bridge or direct filters
- **Formula queries** — inline ES|QL math over lettered query references

Both paths produce a `QueryIR` — a typed contract of source meaning used by
reports, verification, and downstream analysis.

### Step 4 — Panel Assembly & Layout (IR-first)

- Source queries + layout + display metadata → kb-dashboard-core dict, then
  `DashboardIR` (primary working artifact)
- From `DashboardIR`, derive both:
  - native Dashboards API payload (`native_dashboard_from_ir`)
  - on-disk YAML (`DashboardIR.to_yaml_dict`) for lint / `--compile` / standalone upload
- Grafana 24-column grid → Kibana 48-column grid
- Template variables → Kibana dashboard controls / `pinned_panels` (both sources)
- Display enrichment: units, legend, axis titles, thresholds, colour overrides

### Steps 5–12

| Step | Tool / Module | Outcome |
|------|--------------|---------|
| 5. Polish | Heuristic / AI | Better panel titles; rebuild `DashboardIR` + re-derive native/YAML |
| 6. Validate | `_query` API | Catches runtime errors early; same IR rebuild on fixes |
| 7. Lint | `kb-dashboard-lint` | Schema validation of derived YAML |
| 8. Compile (optional) | `kb-dashboard-cli` | YAML → Kibana NDJSON when `--compile` / `--legacy-import` |
| 9. Verify | Semantic gates | Green / yellow / red quality signal |
| 10. Report | `migration_report.json` | Persistent audit trail |
| 11. Upload | Typed Dashboards API | Prefer in-memory `native_dashboard` from IR; standalone upload maps YAML |
| 12. Smoke | Saved-object check | Validates dashboards are loadable |

---

## Why Each Step Matters

| Step | What It Does | What Happens If It Fails |
|------|-------------|-------------------------|
| **Extraction** | Loads JSON, cleans HTML | N/A — entry point |
| **Inventory** | Classifies query language | Wrong translator would run |
| **Translation** | Source query → target query | Panel becomes `not_feasible` placeholder |
| **QueryIR** | Typed contract of source meaning | Downstream analysis blind |
| **Assembly** | Query + layout + display → `DashboardIR` → native + YAML | No deployable / lintable output |
| **Layout** | 24→48 col, overlap resolution | Visual layout corruption |
| **Validation** | Runs query against ES | Errors surface only after upload |
| **Lint** | Schema validation | Blocks optional compile / flags bad YAML |
| **Compile** | Optional YAML → NDJSON | Only blocks `--legacy-import` / explicit `--compile` paths |
| **Upload** | Typed API from native IR (default) | Dashboard not in Kibana; legacy fallback may still succeed |
| **Verification** | Semantic gates | All panels look equally trustworthy |
| **Report** | Persistent audit trail | No post-run analysis |

---

## Appendix: Combined Stats

<!-- GENERATED:APPENDIX_STATS -->
From the latest trace run:

```
Elements:            974 total (922 panels + 52 rows)
Renderable panels:   922
  Migrated:             183 (19.8%)
  With warnings:        275 (29.8%)
  OK:                   108 (11.7%)
  Warning:              221 (24.0%)
  Requires manual:       30 (3.3%)
  Not feasible:          42 (4.6%)
  Skipped:               63 (6.8%)
```

Verdict breakdown:

```
  CORRECT:                  252
  MINOR_ISSUE:              452
  EXPECTED_LIMITATION:      270
```
<!-- /GENERATED:APPENDIX_STATS -->

---

*Last generated: 2026-07-09 10:11 UTC*
