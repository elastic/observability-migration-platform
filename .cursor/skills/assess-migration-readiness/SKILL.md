---
name: assess-migration-readiness
description: Use when the user wants a readiness assessment, feasibility verdict, "what will/won't migrate", how much manual effort is required, a go/no-go before committing, or to know how confident they can be in the result — assesses how much of a connected Grafana/Datadog environment will migrate cleanly versus need manual rework, and how trustworthy that assessment is. For a plain count/type inventory (no verdict), use scan-o11y-environment instead.
---

# Assess migration readiness

Goal: give the user a realistic, **trust-qualified** verdict of what migrates cleanly vs. what needs rework — without over-promising. The single most important thing to communicate is **how much evidence the verdict is based on**.

## Lead with evidence level (do not skip this)

The preflight report stamps an `evidence_level` that tells the user how much to trust the verdict:

| `evidence_level` | Means | Confidence |
|---|---|---|
| `full` | target ES **and** source (Prometheus/Loki) were reachable | highest |
| `target_only` | only `--es-url` was provided | medium |
| `source_only` | only source URLs were provided | medium |
| `static_analysis` | neither — translation analysis only | directional only |

Always tell the user which level their run achieved. A clean-looking verdict at `static_analysis` is **not** a guarantee the queries run against real data.

## Run the assessment

Readiness comes from a **preflight** run (`--preflight`): it translates and analyzes, optionally validates against live systems, and writes a customer-facing readiness report. It does not upload.

Assume the user **installed the package** (`grafana-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. The readiness artifacts below are written by the CLI, so they exist for package users without any `scripts/`/`infra/` directory.

Highest-evidence Grafana run (export the endpoints/keys you actually have):

```bash
export GRAFANA_URL="https://grafana.example.com" GRAFANA_USER="..." GRAFANA_PASS="..."
export ELASTICSEARCH_ENDPOINT="https://...es..." KEY="<api-key>"

grafana-migrate \
  --source api \
  --output-dir readiness_out \
  --assets all \
  --preflight \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY" \
  --prometheus-url "https://prometheus.example.com" \
  --loki-url "https://loki.example.com"
```

- `--prometheus-url` / `--loki-url` take **literal URLs** — there are no standard `$PROMETHEUS_URL`/`$LOKI_URL` repo env vars; substitute the user's real endpoints or omit them.
- Drop `--es-url`/source URLs to run a faster `static_analysis` pass (state the lower confidence).
- `obs-migrate migrate --source grafana --input-mode api --preflight ...` also works, but the `--prometheus-url` / `--loki-url` source-validation flags are exposed on the dedicated `grafana-migrate` CLI.

## Where to read the verdict

Primary artifact: `readiness_out/dashboards/preflight_report.json`.

| What | Field |
|---|---|
| Overall evidence/trust | `evidence_level` |
| Clean vs. rework buckets | `summary.readiness`: `ready` (clean) · `needs_metrics_mapping` / `needs_log_fielding` (mapping rework) · `manual_only` (redesign) |
| Quality gates | `summary.semantic_gates`: `green` / `yellow` / `red` |
| Hard stops | `blockers` (Red-gated panels, missing required fields, non-migratable datasources, RED cluster health, missing metrics) |
| Prep work (not blocking) | `actions` (field mapping needed, unconfirmed counters, missing labels, high-complexity dashboards, YELLOW cluster) |
| One-paragraph readout | `customer_action_summary` |

Human-readable: `readiness_out/dashboards/migration_summary.md` (verdict, scorecard, must-fix worklist).
Per-panel drill-down: `readiness_out/dashboards/migration_manifest.json` → `panels[].readiness`, `panels[].status`, `panels[].verification_packet.semantic_gate`, `panels[].reasons`.

## How to judge confidence (tell the user)

High confidence requires **all** of: `evidence_level: full`, `blockers` empty, Green dominating semantic gates. Treat `static_analysis` as directional. Yellow/Red gates, `metrics_missing`, or `datasource_audit.non_migratable_panels` represent real manual effort — the tool surfaces these gaps rather than hiding them (degrade-gracefully).

## Do NOT

- Do **not** report a readiness verdict without stating its `evidence_level`.
- Do **not** imply `$PROMETHEUS_URL`/`$LOKI_URL` (or other) env vars exist for the source-validation flags; pass literal URLs.
- Do **not** present `static_analysis` results as a guarantee panels will render against live data.
- Do **not** restate inventory counts as "readiness" — that is `scan-o11y-environment`.

## See also

- `scan-o11y-environment` skill — the descriptive inventory layer beneath this.
- `grafana-migrate --help` — confirm `--preflight`, `--es-url`, `--prometheus-url`, `--loki-url` for the installed version.
- `docs/command-contract.md` — preflight/validation flags and artifacts (online docs / repo).
