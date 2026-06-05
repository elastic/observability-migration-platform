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
*Run `python scripts/audit_pipeline.py --update-docs` to populate.*
<!-- /GENERATED:DASHBOARD_SUMMARY -->

<!-- GENERATED:VERDICT_SUMMARY -->
<!-- /GENERATED:VERDICT_SUMMARY -->

<!-- GENERATED:WARNING_PATTERNS -->
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
*Run `python scripts/audit_pipeline.py --update-docs` to populate.*
<!-- /GENERATED:APPENDIX_STATS -->
