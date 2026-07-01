---
name: prepare-target-telemetry
description: Use when, before or while running an obs-migrate Grafana/Prometheus or Datadog migration, the user needs to prepare the Elastic target so migrated dashboards show data instead of being empty — deciding how to get Prometheus/Datadog telemetry into Elastic, which target layout or --field-profile that produces, when data must exist relative to migrating, and how to verify target fields. For pre-migration target/ingest readiness, not post-upload panel debugging.
---

# Prepare the Elastic target before migrating

Goal: get telemetry into Elastic in a layout the migrator maps correctly, and verify it, **before** you trust migrated dashboards. `obs-migrate` migrates dashboard definitions and queries — **not your data**; panels stay empty until matching telemetry lands under field names the translator targets. Per-source detail lives in the skills under **See also**.

## The rule that bites people

**Ingest first, then migrate with a reachable `--es-url`.** With `--es-url`, the migrator reads live `_field_caps` to map fields. If the data is not in Elastic yet (or `--es-url` is unreachable), Grafana detection silently falls back to OTel guesses + pass-through, and you cannot verify Datadog field existence — dashboards look migrated but query the wrong fields.

## Grafana / Prometheus — target layout is auto-detected

How you ship Prometheus into Elastic decides the auto-detected schema profile (from `_field_caps`):

| Ingest route | Detected profile | Metric `http_requests_total` → | Label `service` → |
|---|---|---|---|
| Elastic Fleet/Agent Prometheus integration | `prometheus_remote_write` | `prometheus.http_requests_total.counter`/`.value`/`.rate` | `prometheus.labels.service` |
| Native ES `/_prometheus/api/v1/write` endpoint | `prometheus_native` | `metrics.http_requests_total` | `labels.service` |
| OTel collector / other / **no data found** | generic / none | `http_requests_total` (pass-through) | OTel candidate (`service.name`) → as-is |

`rate()`/`irate()` also need the metric stored as a **counter** (see `understand-source-schema`).

## Datadog — you pick the profile (no auto-detection)

You cannot point Datadog at Elastic directly: choose an ingest route, then **manually pick the matching `--field-profile`**. A wrong profile yields wrong fields even when data exists.

| Ingest route | `--field-profile` | Metric `system.cpu.user` → | Tag `host` → |
|---|---|---|---|
| OTel Collector → ES | `otel` (default) | `system_cpu_user` | `host.name` |
| Elastic Agent / Metricbeat | `elastic_agent` | `system.cpu.user.pct` | `host.name` |
| Prometheus remote_write → ES | `prometheus` | `prometheus.metrics.system_cpu_user` | `instance` |
| Custom / unknown | `passthrough` or custom YAML | `system_cpu_user` | `host` (as-is) |

## Verify before trusting (both sources)

1. Migrate **one** dashboard with `--es-url` (+ `--preflight`). Read field existence: Grafana writes `required_target_contract.json`; Datadog writes `target_readiness_contract.json`. Both carry `status` values such as `confirmed`, `missing`, or `unknown`.
2. Open the per-panel source→target table written by the migration: `<out>/dashboards/schema_change_report.md`. Use `obs-migrate schema-report --artifact-dir <out>/dashboards --output schema_change_report.md` only to regenerate or combine existing outputs.
3. **Prove panels light up without waiting for real ingest:** `obs-migrate seed-sample-data --artifact-dir <out>/dashboards --es-url "$ES"` ingests synthetic docs matching the contract so panels render; tear down with `obs-migrate remove-sample-data`.
4. Only roll out once fields are `confirmed` / panels light up.

## Honest limits / Do NOT

- **The tool does not ingest data or set up collectors/Fleet/Agent for you.** Follow Elastic's ingestion docs for the route you pick; this skill covers only what `obs-migrate` reads and produces.
- **Do NOT migrate before data exists with a reachable `--es-url`** — unverified field mappings are guesses.
- **Do NOT assume a Datadog profile** — it must match your real ingest layout; there is no auto-detection.
- **Do NOT treat `unknown` as proven.** It means live target field caps were unavailable; rerun with data in Elastic and a reachable `--es-url`.
- An empty panel after upload is often missing/wrong-window data, not a translator bug.

## See also

- `understand-source-schema` — exact source→Elastic field mapping model, profiles, and report locations.
- `remediate-field-mapping-gaps` — fix empty/wrong panels after upload.
- `assess-migration-readiness` — readiness verdict from migration artifacts.
- `connect-to-o11y-source` — connect to Grafana/Datadog and Elastic endpoints.
- `validate-side-by-side` — numeric parity once data is flowing.
