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

- Grafana runs a broader end-to-end flow with translation, IR-first emission (`DashboardIR` → native Dashboards API payload), optional emitted-query validation, native review artifacts, optional upload, verification, and rollout artifacts.
- Datadog runs a more explicit `normalize -> plan -> translate -> emit` flow with capability-aware preflight, the same IR-first emission, first-class emitted-query validation, first-class upload, post-upload smoke validation, migration manifest and rollout artifacts, and live metric source execution during verification. The main remaining gap is broader source execution coverage for logs and multi-query widgets.

For both sources, `DashboardIR` is the primary working artifact after
translation/assembly; the native upload payload is derived from it, and the two
persisted artifacts are `dashboards/native/*.native.json` and
`dashboards/ir/*.ir.json`. A migration writes no dashboard YAML: the YAML
artifact directory, the `dashboards/compiled/` NDJSON directory, the lint and
compile stages, and every YAML CLI flag were removed. The `to_yaml_dict()`
shape survives only as an internal in-memory dict. See
`docs/architecture/asset-model.md`.

For the exact source-specific stage order, see `docs/architecture.md`,
`docs/sources/grafana.md`, and `docs/sources/datadog.md`.

---

## Cross-Source Summary

<!-- GENERATED:DASHBOARD_SUMMARY -->
| Source | Dashboard | Panels | Migrated | Warnings | Manual | Not Feasible | Skipped | Rows |
|--------|-----------|--------|----------|----------|--------|--------------|---------|------|
| grafana | Diverse Panel Types Test | 11 | 4 | 7 | 0 | 0 | 0 | 1 |
| grafana | Express Prometheus Middleware | 23 | 1 | 22 | 0 | 0 | 0 | 1 |
| grafana | Home - Migration Test Lab | 6 | 3 | 2 | 0 | 1 | 0 | 0 |
| grafana | Kubernetes / Views / Global | 26 | 11 | 15 | 0 | 0 | 0 | 4 |
| grafana | Kitchen Sink Panel Canary | 16 | 9 | 7 | 0 | 0 | 0 | 0 |
| grafana | Multi Pattern Coverage | 10 | 5 | 4 | 0 | 0 | 1 | 1 |
| grafana | Node Exporter Full | 117 | 43 | 74 | 0 | 0 | 0 | 16 |
| grafana | Prometheus 2.0 (by FUSAKLA) | 45 | 29 | 11 | 5 | 0 | 0 | 0 |
| grafana | Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha) | 12 | 9 | 3 | 0 | 0 | 0 | 0 |
| datadog | Apache - Overview | 22 | 12 | 9 | 1 | 0 | 0 | 0 |
| datadog | Celery Overview | 17 | 10 | 1 | 2 | 0 | 4 | 0 |
| datadog | Consul Overview | 27 | 16 | 2 | 4 | 0 | 5 | 0 |
| datadog | Docker - Overview | 28 | 6 | 19 | 1 | 2 | 0 | 0 |
| datadog | HAProxy - Overview | 29 | 21 | 1 | 1 | 0 | 6 | 0 |
| datadog | Kafka, Zookeeper and Kafka Consumer Overview | 55 | 38 | 6 | 1 | 1 | 9 | 0 |
| datadog | Kubernetes - Overview | 57 | 2 | 41 | 4 | 0 | 10 | 0 |
| datadog | MongoDB - Overview | 43 | 12 | 21 | 1 | 0 | 9 | 0 |
| datadog | MySQL - Overview | 11 | 0 | 11 | 0 | 0 | 0 | 0 |
| datadog | NGINX - Overview | 27 | 15 | 3 | 2 | 1 | 6 | 0 |
| datadog | Postgres - Metrics | 9 | 0 | 9 | 0 | 0 | 0 | 0 |
| datadog | RabbitMQ Overview (OpenMetrics Version) | 47 | 28 | 9 | 3 | 1 | 6 | 0 |
| datadog | Redis - Overview | 43 | 7 | 29 | 0 | 0 | 7 | 0 |
| datadog | Datadog Kitchen Sink Canary | 25 | 16 | 5 | 3 | 0 | 1 | 0 |
| datadog | System Overview - Sample | 11 | 9 | 1 | 1 | 0 | 0 | 0 |

**24 dashboards, 717 panels** audited from `infra/grafana/dashboards/` and `infra/datadog/dashboards/`.
<!-- /GENERATED:DASHBOARD_SUMMARY -->

<!-- GENERATED:VERDICT_SUMMARY -->
## Verdict Summary

| Verdict | Count | Meaning |
|---------|-------|---------|
| **CORRECT** | 122 | Translation is semantically accurate |
| **MINOR_ISSUE** | 406 | Translated with approximations — review recommended |
| **EXPECTED_LIMITATION** | 212 | Known unsupported feature — placeholder or skip |
<!-- /GENERATED:VERDICT_SUMMARY -->

<!-- GENERATED:WARNING_PATTERNS -->
## Top Warning Patterns

| Count | Warning |
|------:|---------|
| 136 | Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana |
| 56 | Composited multi-label grouping (instance, job) into a single XY breakdown column |
| 35 | Grafana panel description is not carried into the migrated Kibana panel automatically |
| 27 | Grafana panel has 1 field override(s); verify visual mappings manually |
| 22 | Approximated PromQL arithmetic using same-bucket ES\|QL math |
| 20 | Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value |
| 14 | PromQL series labels were not retained; output is bucket-level and may collapse multiple source series |
| 10 | rollup interval is approximated in ES\|QL |
| 9 | Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead |
| 9 | as_count interval semantics are approximated in ES\|QL |
| 7 | Grafana panel has 2 field override(s); verify visual mappings manually |
| 7 | fill(zero) only applies to null values in returned rows; empty buckets may still be omitted |
| 6 | Grafana panel has 18 field override(s); verify visual mappings manually |
| 6 | Grafana panel has 19 field override(s); verify visual mappings manually |
| 5 | Approximated bargauge as bar chart |
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
  │              produces: DashboardIR (primary) → native Dashboards API payload
  │                        (+ VisualIR / OperationalIR snapshots for reporting)
  ▼
[5] POLISH (optional) — improve titles and labels (heuristic or AI); rebuild DashboardIR
  │
  ▼
[6] VALIDATE (optional) — run emitted target queries against Elasticsearch, fix/downgrade broken ones
  │                        (rebuild DashboardIR; re-derive native payload)
  ▼
[7] REVIEW ARTIFACTS — write dashboards/native/*.native.json + dashboards/ir/*.ir.json
  │                        (unconditional; the old YAML lint and YAML→NDJSON compile
  │                         stages were removed)
  ▼
[8] VERIFY — build verification packets, assign semantic gates, refresh OperationalIR
  │
  ▼
[9] REPORT — write migration_report.json, manifest, verification packets
  │
  ▼
[10] UPLOAD (optional) — typed Dashboards API (PUT /api/dashboards/{id}) from the
  │                        in-memory native_dashboard, or from the reviewed
  │                        native/*.native.json via `upload --artifact-dir`
  ▼
[11] SMOKE (optional) — validate uploaded dashboards in Kibana
  │
  ▼
[12] INTERACTION AUDIT (optional, local/nightly) — Playwright control selection
       against seeded data; proves affected panel ES|QL changes (9.5+)
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

- Source queries + layout + display metadata → kb-dashboard-core-shaped dict, then
  `DashboardIR` (primary working artifact)
- From `DashboardIR`, derive the native Dashboards API payload
  (`native_dashboard_from_ir`) — persisted as `dashboards/native/*.native.json`,
  alongside `dashboards/ir/*.ir.json`. Nothing else is derived to disk;
  `DashboardIR.to_yaml_dict()` stays an internal in-memory dict used by the
  post-validation rebuild and the structural guards
  (`tests/native_payload_guard.py`)
- Grafana 24-column grid → Kibana 48-column grid
- Template variables → Kibana dashboard controls / `pinned_panels` (both sources)
- Display enrichment: units, legend, axis titles, thresholds, colour overrides

### Steps 5–12

| Step | Tool / Module | Outcome |
|------|--------------|---------|
| 5. Polish | Heuristic / AI | Better panel titles; rebuild `DashboardIR` + re-derive the native payload |
| 6. Validate | `_query` API | Catches runtime errors early; same IR rebuild on fixes |
| 7. Review artifacts | `targets/kibana/native_artifacts.py` | Writes `native/*.native.json` (the exact typed API payload), `ir/*.ir.json`, and `native/index.json` — unconditionally, every run |
| 8. Verify | Semantic gates | Green / yellow / red quality signal |
| 9. Report | `migration_report.json` | Persistent audit trail |
| 10. Upload | Typed Dashboards API | `PUT /api/dashboards/{id}` with the in-memory `native_dashboard` from the IR, or with a reviewed `native/*.native.json` byte-for-byte via `upload --artifact-dir` |
| 11. Smoke | Saved-object check | Validates dashboards are loadable |
| 12. Interaction audit (optional) | `targets/kibana/interaction_*.py` + Playwright | Control selection rewrites affected panel queries; see `docs/testing.md` |

---

## Why Each Step Matters

| Step | What It Does | What Happens If It Fails |
|------|-------------|-------------------------|
| **Extraction** | Loads JSON, cleans HTML | N/A — entry point |
| **Inventory** | Classifies query language | Wrong translator would run |
| **Translation** | Source query → target query | Panel becomes `not_feasible` placeholder |
| **QueryIR** | Typed contract of source meaning | Downstream analysis blind |
| **Assembly** | Query + layout + display → `DashboardIR` → native payload | No deployable artifact |
| **Layout** | 24→48 col, overlap resolution | Visual layout corruption |
| **Validation** | Runs query against ES | Errors surface only after upload |
| **Review artifacts** | Persists the exact payload + semantic IR | No "inspect before upload" step and nothing for the repo's own readers to consume |
| **Upload** | Typed API from the native payload | Dashboard not in Kibana; the failure is terminal — there is no second renderer to fall back to |
| **Verification** | Semantic gates | All panels look equally trustworthy |
| **Report** | Persistent audit trail | No post-run analysis |
| **Interaction audit** | Control → query evidence | False confidence that filters "work" |

---

## Appendix: Combined Stats

<!-- GENERATED:APPENDIX_STATS -->
From the latest trace run:

```
Elements:            740 total (717 panels + 23 rows)
Renderable panels:   717
  Migrated:             114 (15.9%)
  With warnings:        145 (20.2%)
  OK:                   192 (26.8%)
  Warning:              167 (23.3%)
  Requires manual:       29 (4.0%)
  Not feasible:           6 (0.8%)
  Skipped:               64 (8.9%)
```

Verdict breakdown:

```
  CORRECT:                  122
  MINOR_ISSUE:              406
  EXPECTED_LIMITATION:      212
```
<!-- /GENERATED:APPENDIX_STATS -->

---

*Last generated: 2026-08-04 18:38 UTC*
