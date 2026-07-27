---
name: assess-migration-readiness
description: Use when the user wants a readiness assessment, feasibility verdict, "what will/won't migrate", how much manual effort is required, a go/no-go before committing, or to know how confident they can be in the result — assesses how much of a connected Grafana/Datadog environment will migrate cleanly versus need manual rework, and how trustworthy that assessment is. For a plain count/type inventory (no verdict), use scan-o11y-environment instead.
---

# Assess migration readiness

**Audience:** operators of the published `obs-migrate` CLI (PyPI/`uvx`), using public docs and their real source + Elastic/Kibana — not a repo lab harness.

Goal: give the user a realistic, **trust-qualified** verdict of what migrates cleanly vs. what needs rework — without over-promising. The single most important thing to communicate is **how much evidence the verdict is based on**.

## Lead with evidence level (do not skip this)

For **Grafana**, the preflight report stamps an `evidence_level` that tells the user how much to trust the verdict:

| `evidence_level` | Means | Confidence |
|---|---|---|
| `full` | target ES **and** source (Prometheus/Loki) were reachable | highest |
| `target_only` | only `--es-url` was provided | medium |
| `source_only` | only source URLs were provided | medium |
| `static_analysis` | neither — translation analysis only | directional only |

Always tell the user which level their run achieved. A clean-looking verdict at `static_analysis` is **not** a guarantee the queries run against real data.

**Datadog:** `--preflight` runs a different checker (issues embed in `migration_report.json`). It does **not** emit Grafana's `preflight_report.json` / `evidence_level` contract. Treat Datadog preflight as gap surfacing (block/warn/info), and use target validation + try-one/`validate-side-by-side` for confidence — do not invent an `evidence_level` for Datadog.

## Prerequisites (install)

These skills help **operators** of the published CLI (not a repo checkout).
If `obs-migrate` is missing or `doctor` is not **Ready**, follow
`install-obs-migrate` first — that skill owns PyPI/`uvx`/pip, extras, and
Python/`uv` gotchas. Do not invent alternate install commands here.

```bash
uvx --from 'elastic-observability-migration[all]' obs-migrate doctor
# After a persistent install, the same check is: obs-migrate doctor
```

Source/Elastic credentials: `connect-to-o11y-source` (and your env exports).


## Run the assessment

Readiness comes from a **preflight** run (`--preflight`): it translates and analyzes, optionally validates against live systems, and writes a customer-facing readiness report. It does not upload.

Prefer the unified CLI for target-aware Grafana preflight. **`--prometheus-url` / `--loki-url` exist on `grafana-migrate` only** (not on `obs-migrate migrate`) — use the dedicated CLI when you need `evidence_level: full`:

```bash
export GRAFANA_URL="https://grafana.example.com" GRAFANA_USER="..." GRAFANA_PASS="..."
export ELASTICSEARCH_ENDPOINT="https://...es..." KEY="<api-key>"

# Canonical package entry (target-aware preflight):
obs-migrate migrate \
  --source grafana --input-mode api \
  --output-dir readiness_out \
  --assets all \
  --preflight \
  --field-profile otel \
  --data-view "metrics-*" \
  --esql-index "metrics-*" \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY"

# Highest evidence (also validate against live Prometheus/Loki) — dedicated CLI:
grafana-migrate \
  --source api \
  --output-dir readiness_out \
  --assets all \
  --preflight \
  --data-view "metrics-*" \
  --esql-index "metrics-*" \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY" \
  --prometheus-url "https://prometheus.example.com" \
  --loki-url "https://loki.example.com"
```

- `--prometheus-url` / `--loki-url` take **literal URLs** — there are no standard `$PROMETHEUS_URL`/`$LOKI_URL` repo env vars; substitute the user's real endpoints or omit them.
- **Populated targets:** Grafana may use `--field-profile auto --es-url ...` when telemetry already exists (ambiguous caps → `otel` + warn); otherwise choose an explicit planned profile (`prepare-target-telemetry`). Datadog always requires an explicit profile — no `auto`.
- Set `--esql-index` for Prometheus query/discovery even when `--data-view` differs as the Kibana UI bind.
- Drop `--es-url`/source URLs to run a faster `static_analysis` pass (state the lower confidence).

## Where to read the verdict (Grafana)

Primary artifact: `readiness_out/dashboards/preflight_report.json`.

| What | Field |
|---|---|
| Overall evidence/trust | `evidence_level` |
| Clean vs. rework buckets | `summary.readiness`: `ready` (clean) · `needs_metrics_mapping` / `needs_log_fielding` (mapping rework) · `manual_only` (redesign) |
| Quality gates | `summary.semantic_gates`: `green` / `yellow` / `red` |
| Hard stops | `blockers` (Red-gated panels, missing required fields, non-migratable datasources, RED cluster health, missing metrics) |
| Prep work (not blocking) | `actions` (field mapping needed, unconfirmed counters, missing labels, high-complexity dashboards, YELLOW cluster). Grafana `profile_mismatch` on `required_target_contract.json` is operator visibility — check planned vs detected layout there; it is not a separate preflight blocker. |
| One-paragraph readout | `customer_action_summary` |

Human-readable: `readiness_out/dashboards/migration_summary.md` (verdict, scorecard, must-fix worklist).
Per-panel drill-down: `readiness_out/dashboards/migration_manifest.json` → `panels[].readiness`, `panels[].status`, `panels[].verification_packet.semantic_gate`, `panels[].reasons`. Expect `migrated_with_warnings` for accepted approximations — triage with `explain-migration-gaps`, not as automatic blockers.

## How to judge confidence (tell the user)

High confidence (Grafana) requires **all** of: `evidence_level: full`, `blockers` empty, Green dominating semantic gates. Treat `static_analysis` as directional. Yellow/Red gates, `metrics_missing`, or `datasource_audit.non_migratable_panels` represent real manual effort — the tool surfaces these gaps rather than hiding them (degrade-gracefully). Preflight does **not** prove Lens UI render; for that, use render audit / `validate-side-by-side` after a try-one upload (`docs/testing.md`).

## Do NOT

- Do **not** report a Grafana readiness verdict without stating its `evidence_level`.
- Do **not** invent Grafana `evidence_level` / `preflight_report.json` semantics for Datadog.
- Do **not** imply `$PROMETHEUS_URL`/`$LOKI_URL` (or other) env vars exist for the source-validation flags; pass literal URLs.
- Do **not** present `static_analysis` results as a guarantee panels will render against live data.
- Do **not** restate inventory counts as "readiness" — that is `scan-o11y-environment`.
- Do **not** claim `obs-migrate migrate` accepts `--prometheus-url` / `--loki-url` — use `grafana-migrate` for those.

## See also

- `install-obs-migrate` — install/doctor when the CLI is missing or not Ready.
- `scan-o11y-environment` skill — the descriptive inventory layer beneath this.
- `prepare-target-telemetry` skill — field profile / `--esql-index` before readiness claims.
- `explain-migration-gaps` skill — warned vs blocked panel triage.
- `prepare-production-cutover` skill — final go/no-go after validation gates.
- `obs-migrate migrate --help` / `grafana-migrate --help` — confirm `--preflight`, `--es-url`, `--prometheus-url`, `--loki-url` for the installed version.
- `docs/command-contract.md` — preflight/validation flags and artifacts (online docs / repo).
- `docs/testing.md` — layered verifier / render-audit gates beyond preflight.
