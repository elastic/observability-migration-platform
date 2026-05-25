---
name: obs-migrate-kibana-migration
description: >-
  Run and explain Grafana/Datadog → Kibana migrations using the elastic/mig-to-kbn
  CLI (obs-migrate): flags, env vars, manifests, validation scripts, and honest
  partial coverage. Use when the user wants to migrate observability dashboards
  or alerts to Kibana, interpret migration output, or choose PromQL vs ES|QL paths.
---

# Observability migration to Kibana (`obs-migrate`)

This skill guides use of the **Observability Migration Platform** in this repository: package `observability_migration`, CLI **`obs-migrate`** (plus `grafana-migrate` / `datadog-migrate`).

## Principles

- **~70% coverage is OK:** Expect **partial** translations and **manual-review markers** in emitted Lens YAML—not silent drops.
- **CLI is source of truth:** The agent proposes concrete `obs-migrate` commands; users run them in their environment.
- **Three data/query paths to mention when relevant:** (1) Grafana **PromQL** (often `--native-promql` on Serverless), (2) **ES\|QL** (rule engine / Datadog metrics), (3) data already in Elastic / OTel—do not imply a single pipeline.

## When to use which entry point

| User need | Prefer |
|-----------|--------|
| Single unified flow, CI, docs parity | `obs-migrate migrate`, `obs-migrate compile`, `obs-migrate upload`, `obs-migrate cluster` |
| Grafana-only deep options / smoke | `grafana-migrate` (see [docs/sources/grafana.md](../../../docs/sources/grafana.md)) |
| Datadog-only normalize/plan/report | `datadog-migrate` (see [docs/sources/datadog.md](../../../docs/sources/datadog.md)) |

## Environment (typical)

- **Kibana + Elasticsearch (Serverless or Stack):** `KIBANA_ENDPOINT`, `ELASTICSEARCH_ENDPOINT` (if needed), `KEY` (API key)—see repo `serverless_creds.env.example` and [README](../../../README.md).
- **Grafana API:** `GRAFANA_URL`, `GRAFANA_USER`, `GRAFANA_PASS` for `--input-mode api`.
- **Datadog API:** `DD_API_KEY`, `DD_APP_KEY` (optional `--env-file`); optional `.[datadog]` install.

## Golden paths for demos / tests

- Grafana dashboards: `infra/grafana/dashboards`
- Datadog dashboards: `infra/datadog/dashboards`
- Alert examples: `examples/alerting/`
- Datadog field profile sample: `examples/datadog-field-profile.example.yaml`

## Canonical commands (patterns)

**Grafana files → output (PromQL-first on metrics):**

```bash
.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --native-promql
```

Add `--upload --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"` when uploading.

**Grafana dashboards + alert rules (rules created disabled, tagged `obs-migration`):**

```bash
.venv/bin/obs-migrate migrate \
  --source grafana \
  --assets all \
  --input-mode files \
  --input-dir examples/alerting/grafana \
  --output-dir alert_migration_output \
  --native-promql \
  --upload \
  --create-alert-rules \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY"
```

**Datadog files with field profile:**

```bash
.venv/bin/obs-migrate migrate \
  --source datadog \
  --input-mode files \
  --input-dir infra/datadog/dashboards \
  --output-dir migration_output \
  --field-profile examples/datadog-field-profile.example.yaml \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY" \
  --upload
```

## Outputs to interpret

Under `migration_output/` (or chosen `--output-dir`):

- `dashboards/yaml/` — Lens **YAML** (canonical IR).
- `dashboards/compiled/` — **NDJSON** for import.
- `manifest.json` / migration reports — per-run status and warnings.
- `alerts/*_results.json` — when `--create-alert-rules` is used.

Point users to **manual-review** / `reasons` fields in panel results when explaining gaps.

## Grafana query routing (summary)

1. Prefer **`--native-promql`** for compatible metrics on Serverless.
2. Otherwise **ES\|QL** via rule engine; optional LLM fallback for hard panels.
3. **LogQL** → ES\|QL for logs indices.

Detail: [docs/sources/grafana.md](../../../docs/sources/grafana.md).

## Validation next steps

After migrate, use repo tooling—not ad-hoc checks:

- [.pm/milestones.md → Validation](../../../.pm/milestones.md#validation-and-extension) — `scripts/validate_panel_queries.py`, `audit_migrated_rules.py`, Grafana `grafana-validate-uploaded`, smoke flags.

## PM / positioning context (internal)

All PM docs live in 3 files under `.pm/` (separate from repo `docs/`):

- **Full product guide (scope, roadmap, blog, adoption, dogfooding, open questions):** [.pm/milestones.md](../../../.pm/milestones.md)
- **Competitive (Groundcover):** [.pm/competitive.md](../../../.pm/competitive.md)
- **Index + onboarding checklist:** [.pm/README.md](../../../.pm/README.md)

## Additional context for "Dashboard failed to compile" issues

### Key source files for Datadog compilation failures

- **Compile invocation:** `observability_migration/targets/kibana/compile.py` — wraps `uvx kb-dashboard-cli compile`
- **YAML generation:** `observability_migration/adapters/source/datadog/generate.py` — builds panel YAML structure
- **Display enrichment:** `observability_migration/adapters/source/datadog/display.py` — applies visual properties (axes, legends, colors) — common source of schema validation errors
- **Translation engine:** `observability_migration/adapters/source/datadog/translate.py` — converts queries to ES|QL
- **Planner:** `observability_migration/adapters/source/datadog/planner.py` — decides backend per widget
- **Normalization:** `observability_migration/adapters/source/datadog/normalize.py` — raw JSON → internal IR
- **Grafana equivalent for display:** `observability_migration/targets/kibana/emit/display.py` — useful as reference for correct patterns

### Reproduction pattern

1. Download the dashboard JSON to `/tmp/<test>/input/`
2. Run: `cd ~/Cursor/mig-to-kbn && uv run python -m observability_migration migrate --source datadog --input-mode files --input-dir /tmp/<test>/input --output-dir /tmp/<test>/output --compile`
3. Check generated YAML at: `/tmp/<test>/output/dashboards/yaml/*.yaml`
4. The compile error from `kb-dashboard-cli` is the actual root cause — search the YAML for the schema violation it reports

### Pattern: "Dashboard failed to compile" always means the ENTIRE dashboard YAML is rejected

- `kb-dashboard-cli` fails the whole file on any validation error
- ALL panels in the dashboard are counted as failed, even if only 1–2 panels have the issue
- The error is always a YAML schema violation — the generated YAML doesn't match what `kb-dashboard-cli` expects
- Common causes: missing required fields in extent/appearance blocks, invalid enum values, type mismatches

### The Grafana adapter is a reference implementation

When the Datadog adapter has a schema violation, check how the Grafana adapter handles the same `kb-dashboard` schema field — it's usually already correct and shows the expected pattern.

### Output format

When resolving a "Dashboard failed to compile" issue, always return the fix as a markdown (`.md`) file that includes: the root cause, the schema violation, the before/after code diff, and the corrected source file path.

## Guardrail

This PM program operates under an internal-only constraint: no GitHub commits or publish unless the owner explicitly lifts the rule. Do not push commits unless the user explicitly opts in.

## Skill version

Align with repo pre-1.0; bump this note when behavior changes: **Skill pack v0.1.0** (matches `obs-migrate` ~0.1.x).
