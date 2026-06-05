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

- Grafana runs a broader end-to-end flow with translation, optional emitted-query validation, lint/compile/layout, optional upload, verification, and rollout artifacts.
- Datadog runs a more explicit `normalize -> plan -> translate -> emit` flow with capability-aware preflight, first-class emitted-query validation, optional compile, first-class upload, post-upload smoke validation, migration manifest and rollout artifacts, and live metric source execution during verification. The main remaining gap is broader source execution coverage for logs and multi-query widgets.

For the exact source-specific stage order, see `docs/architecture.md`,
`docs/sources/grafana.md`, and `docs/sources/datadog.md`.

---

## Cross-Source Summary

<!-- GENERATED:DASHBOARD_SUMMARY -->
| Source | Dashboard | Panels | Migrated | Warnings | Manual | Not Feasible | Skipped | Rows |
|--------|-----------|--------|----------|----------|--------|--------------|---------|------|
| grafana | Diverse Panel Types Test | 10 | 2 | 8 | 0 | 0 | 0 | 1 |
| grafana | Home - Migration Test Lab | 6 | 2 | 3 | 0 | 1 | 0 | 0 |
| grafana | Kubernetes / Views / Global | 26 | 12 | 14 | 0 | 0 | 0 | 4 |
| grafana | Node Exporter Full | 116 | 3 | 111 | 0 | 2 | 0 | 16 |
| grafana | Prometheus 2.0 (by FUSAKLA) | 44 | 21 | 17 | 5 | 1 | 0 | 0 |
| datadog | Apache - Overview | 22 | 16 | 4 | 1 | 1 | 0 | 0 |
| datadog | Celery Overview | 17 | 5 | 6 | 2 | 0 | 4 | 0 |
| datadog | Consul Overview | 27 | 7 | 11 | 4 | 0 | 5 | 0 |
| datadog | Docker - Overview | 28 | 16 | 9 | 1 | 2 | 0 | 0 |
| datadog | HAProxy - Overview | 29 | 9 | 12 | 2 | 0 | 6 | 0 |
| datadog | Kafka, Zookeeper and Kafka Consumer Overview | 55 | 13 | 27 | 4 | 2 | 9 | 0 |
| datadog | Kubernetes - Overview | 57 | 17 | 24 | 4 | 2 | 10 | 0 |
| datadog | MongoDB - Overview | 43 | 15 | 18 | 1 | 0 | 9 | 0 |
| datadog | MySQL - Overview | 11 | 0 | 11 | 0 | 0 | 0 | 0 |
| datadog | NGINX - Overview | 27 | 12 | 5 | 2 | 2 | 6 | 0 |
| datadog | Postgres - Metrics | 9 | 0 | 9 | 0 | 0 | 0 | 0 |
| datadog | RabbitMQ Overview (OpenMetrics Version) | 47 | 11 | 23 | 6 | 1 | 6 | 0 |
| datadog | Redis - Overview | 43 | 9 | 27 | 0 | 0 | 7 | 0 |
| datadog | System Overview - Sample | 11 | 8 | 2 | 1 | 0 | 0 | 0 |

**19 dashboards, 628 panels** audited from `infra/grafana/dashboards/` and `infra/datadog/dashboards/`.
<!-- /GENERATED:DASHBOARD_SUMMARY -->

<!-- GENERATED:VERDICT_SUMMARY -->
## Verdict Summary

| Verdict | Count | Meaning |
|---------|-------|---------|
| **CORRECT** | 337 | Translation is semantically accurate |
| **MINOR_ISSUE** | 71 | Translated with approximations — review recommended |
| **EXPECTED_LIMITATION** | 241 | Known unsupported feature — placeholder or skip |
<!-- /GENERATED:VERDICT_SUMMARY -->

<!-- GENERATED:WARNING_PATTERNS -->
## Top Warning Patterns

| Count | Warning |
|------:|---------|
| 188 | Scope filter with template variable could not be bound exactly; apply specific values via Kibana dashboard controls |
| 60 | No explicit aggregation; using AVG per series (faithful gauge downsample) |
| 54 | XY chart shows a single breakdown; additional grouping dimension(s) ['job'] are in the query but not on the chart, so series differing only by those are visually merged |
| 45 | Added outer AVG() around irate because ES\|QL requires an outer aggregation when grouping TS functions by label fields |
| 35 | Grafana panel description is not carried into Kibana YAML automatically |
| 27 | Grafana panel has 1 field override(s); verify visual mappings manually |
| 24 | Approximated PromQL arithmetic using same-bucket ES\|QL math |
| 13 | PromQL series labels were not retained; output is bucket-level and may collapse multiple source series |
| 9 | as_count interval semantics are approximated in ES\|QL |
| 7 | Grafana panel has 2 field override(s); verify visual mappings manually |
| 6 | Grafana panel has 18 field override(s); verify visual mappings manually |
| 6 | Grafana panel has 19 field override(s); verify visual mappings manually |
| 6 | fill(zero) only applies to null values in returned rows; empty buckets may still be omitted |
| 5 | Grafana panel has 20 field override(s); verify visual mappings manually |
| 5 | Grafana panel has 17 field override(s); verify visual mappings manually |
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
  │              produces: Kibana dashboard YAML + VisualIR + OperationalIR
  ▼
[5] POLISH (optional) — improve titles and labels (heuristic or AI)
  │
  ▼
[6] VALIDATE (optional) — run emitted target queries against Elasticsearch, fix/downgrade broken ones
  │
  ▼
[7] LINT — schema-validate all YAML files via kb-dashboard-lint
  │
  ▼
[8] COMPILE — YAML → Kibana NDJSON via kb-dashboard-cli
  │
  ▼
[9] VERIFY — build verification packets, assign semantic gates, refresh OperationalIR
  │
  ▼
[10] REPORT — write migration_report.json, manifest, verification packets
  │
  ▼
[11] UPLOAD (optional) — import NDJSON into Kibana
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

1. **Native PROMQL** (the default; when `--es-url` is set, target detection
   downgrades to ES|QL translation if the `PROMQL` command is unsupported;
   `--native-promql` forces it and `--no-native-promql` opts out) — wraps
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

### Step 4 — Panel Assembly & Layout

- Source queries + layout + display metadata → YAML panel structures
- Grafana 24-column grid → Kibana 48-column grid
- Template variables → Kibana dashboard controls (both sources)
- Display enrichment: units, legend, axis titles, thresholds, colour overrides

### Steps 5–12

| Step | Tool / Module | Outcome |
|------|--------------|---------|
| 5. Polish | Heuristic / AI | Better panel titles |
| 6. Validate | `_query` API | Catches runtime errors early |
| 7. Lint | `kb-dashboard-lint` | Schema validation |
| 8. Compile | `kb-dashboard-cli` | YAML → Kibana NDJSON |
| 9. Verify | Semantic gates | Green / yellow / red quality signal |
| 10. Report | `migration_report.json` | Persistent audit trail |
| 11. Upload | Kibana API | Import NDJSON |
| 12. Smoke | Saved-object check | Validates dashboards are loadable |

---

## Why Each Step Matters

| Step | What It Does | What Happens If It Fails |
|------|-------------|-------------------------|
| **Extraction** | Loads JSON, cleans HTML | N/A — entry point |
| **Inventory** | Classifies query language | Wrong translator would run |
| **Translation** | Source query → target query | Panel becomes `not_feasible` placeholder |
| **QueryIR** | Typed contract of source meaning | Downstream analysis blind |
| **Assembly** | Query + layout + display → YAML | No compilable output |
| **Layout** | 24→48 col, overlap resolution | Visual layout corruption |
| **Validation** | Runs query against ES | Errors surface only after upload |
| **Lint** | Schema validation | Blocks compilation |
| **Compile** | YAML → NDJSON | Dashboard can't deploy |
| **Verification** | Semantic gates | All panels look equally trustworthy |
| **Report** | Persistent audit trail | No post-run analysis |

---

## Appendix: Combined Stats

<!-- GENERATED:APPENDIX_STATS -->
From the latest trace run:

```
Elements:            649 total (628 panels + 21 rows)
Renderable panels:   628
  Migrated:              40 (6.4%)
  With warnings:        153 (24.4%)
  OK:                   138 (22.0%)
  Warning:              188 (29.9%)
  Requires manual:       33 (5.3%)
  Not feasible:          14 (2.2%)
  Skipped:               62 (9.9%)
```

Verdict breakdown:

```
  CORRECT:                  337
  MINOR_ISSUE:               71
  EXPECTED_LIMITATION:      241
```
<!-- /GENERATED:APPENDIX_STATS -->

---

*Last generated: 2026-06-02 10:51 UTC*
