---
name: prepare-target-telemetry
description: Use when, before or while running an obs-migrate Grafana/Prometheus or Datadog migration, the user needs to prepare the Elastic target so migrated dashboards show data instead of being empty — deciding how to get Prometheus/Datadog telemetry into Elastic, which target layout or --field-profile that produces, when data must exist relative to migrating, and how to verify target fields. For pre-migration target/ingest readiness, not post-upload panel debugging.
---

# Plan target telemetry for an assets-first migration

**Audience:** operators of the published `obs-migrate` CLI (PyPI/`uvx`), using public docs and their real source + Elastic/Kibana — not a repo lab harness.

Goal: choose the future Elastic telemetry layout, migrate assets against that contract, then verify it after telemetry starts flowing. `obs-migrate` migrates dashboard definitions and queries — **not your data**; panels stay empty until matching telemetry lands under the planned field names. Per-source detail lives in the skills under **See also**.

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


## Normal migration sequence

1. Choose how Grafana/Prometheus or Datadog telemetry will be ingested into Elastic.
2. Select the matching `--field-profile`; this is the planned target schema.
3. Migrate and upload assets. An empty Elastic cluster is valid at this stage.
4. Point the telemetry pipeline at Elastic.
5. Rerun preflight with `--es-url` (and `--es-api-key` when needed) to compare live `_field_caps` with the plan.

`unknown` field status before step 4 means verification is pending, not that the asset migration failed.

## If telemetry already exists

- **Grafana:** use `--field-profile auto --es-url <url> --preflight`. `auto`
  requires `--es-url`; it detects clear named Prometheus layouts or, when caps
  are ambiguous, emits as **`otel`** with an explicit warning
  (`WARNING: field profile auto could not detect a named Prometheus layout;
  falling back to otel`).
- **Datadog:** select the expected profile explicitly and add
  `--es-url <url> --preflight`. Datadog does not auto-detect profiles; inspect
  the confirmed/missing totals and `target_readiness_contract.json`. Live
  missing fields show as `Preflight: issues` with `[warn] … field '…' not
  found in target mapping` (translation still continues).
- Treat a live `missing` field as a profile/index mismatch to resolve before
  upload. For Grafana, inspect `required_target_contract.json`:
  `profile_mismatch` (planned ≠ detected named layout) is recorded there for
  operator visibility — translation keeps the plan; it is **not** a separate
  preflight blocker beyond existing missing-field severity. For Datadog,
  `Preflight: issues` with missing-field warns means the live layout does not
  satisfy the selected profile even when translation continues. `unknown` means
  discovery did not provide evidence.

## Grafana / Prometheus — select the planned layout

How you plan to ship Prometheus into Elastic decides the field profile:

| Ingest route | `--field-profile` | Metric `http_requests_total` → | Label `service` → |
|---|---|---|---|
| Elastic Fleet/Agent Prometheus integration (`use_types`) | `prometheus_remote_write` | `prometheus.http_requests_total.counter`/`.value`/`.rate` | `prometheus.labels.service` |
| Classic Metricbeat remote_write (`use_types=false`) | `prometheus_metrics` | `prometheus.metrics.http_requests_total` | `prometheus.labels.service` |
| Native ES `/_prometheus/api/v1/write` endpoint | `prometheus_native` | `metrics.http_requests_total` | `labels.service` |
| OTel collector / generic normalized layout | `otel` (default) | `http_requests_total` | OTel candidate (`service.name`) → as-is |
| Keep source metric/label names as-is | `passthrough` | `http_requests_total` | `service` |
| Existing populated target where the tool should infer a layout | `auto` | detected from `_field_caps` | detected from `_field_caps` |

`rate()`/`irate()` also need the metric stored as a **counter** (see `understand-source-schema`). Histogram quantiles need the base field typed as histogram / exponential_histogram (or unknown → assume+warn); pin classic histograms via field caps / profile so `TO_TDIGEST()` is used when required.

### Grafana index flags (most common empty-panel cause)

- **`--esql-index`** — metrics query + schema-discovery target (`TS`/`FROM` / native `PROMQL index=…`). Required for Prometheus fidelity when the stream is not the default data-view pattern.
- **`--data-view`** — Kibana UI / control bind; may differ from `--esql-index`.
- See `docs/command-contract.md` → Target index flags **and** “Migrate-first vs data-first”. Wrong/missing `--esql-index` is the #1 reason migrated Prometheus panels query the wrong stream.
- **Migrate-first:** pick the concrete stream your ingest will create *before* migrating; empty panels until data lands are expected. **Data-first:** with `--es-url`, pin both flags away from a `metrics-*` wildcard, especially when several backends share it. Migrate warns whenever the metrics target is still a wildcard, names the streams it resolves to, and stays quiet once both flags are pinned.

Late-bound grouping (`by ($var)` → `??var`) and label-matcher params (`$var` → `?var`) need live `--es-url` so `esql_named_param_binding` can probe. `--translation-mode {auto,native,esql}` controls native PROMQL vs forced ES|QL.

## Datadog — select the planned layout

You cannot point Datadog at Elastic directly, and its field profile does not auto-detect: choose an ingest route, then **manually pick the matching `--field-profile`**. A wrong profile yields wrong fields even when data exists.

| Ingest route | `--field-profile` | Metric `system.cpu.user` → | Tag `host` → |
|---|---|---|---|
| OTel Collector → ES | `otel` (default) | `system.cpu.user` | `host.name` |
| Elastic Agent / Metricbeat | `elastic_agent` | `system.cpu.user.pct` | `host.name` |
| Metricbeat / Agent Prometheus remote_write | `prometheus` | `prometheus.metrics.system_cpu_user` | `prometheus.labels.instance` |
| Elasticsearch native `/_prometheus` write | `prometheus_native` | `metrics.system_cpu_user` | `labels.instance` |
| Custom / unknown | `passthrough` or custom YAML | `system.cpu.user` | `host` (as-is) |

Datadog has **no** `auto` profile (confirmed in `obs-migrate migrate --help`).

## Verify after telemetry starts (both sources)

1. Rerun **one** dashboard with `--es-url` / `--es-api-key` (+ `--preflight`). For Grafana, set `--esql-index` / `--data-view` correctly. Read field existence: Grafana writes `required_target_contract.json`; Datadog writes `target_readiness_contract.json`. Both carry `status` values such as `confirmed`, `missing`, or `unknown`.
2. Open the per-panel source→target table written by the migration: `<out>/dashboards/schema_change_report.md`. Use `obs-migrate schema-report --artifact-dir <out>/dashboards --output schema_change_report.md` only to regenerate or combine existing outputs.
3. **Prove panels light up without waiting for real ingest** (auth flag is **`--api-key`**, not `--es-api-key`):

```bash
# Defaults: --es-url ← ELASTICSEARCH_ENDPOINT|ES_URL, --api-key ← KEY
obs-migrate seed-sample-data \
  --artifact-dir <out>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

# Dry-run (prints plan, deletes nothing) — --artifact-dir is required:
obs-migrate remove-sample-data \
  --artifact-dir <out>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

# Actually tear down seeder-owned streams:
obs-migrate remove-sample-data \
  --artifact-dir <out>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --confirm
```

4. Expect approximation warnings once data lands (`histogram_quantile` assume+warn, multi-target fusion, histogram mean ratio) — triage with `explain-migration-gaps`, not profile thrashing.
5. Only roll out once fields are `confirmed` / panels light up.

## Honest limits / Do NOT

- **The tool does not ingest production telemetry or set up collectors/Fleet/Agent for you.** `seed-sample-data` only writes **synthetic** docs for lab proof; follow Elastic's ingestion docs for the real route.
- **Do NOT use Grafana `auto` before data exists** — choose an explicit planned profile instead.
- **Do NOT assume a profile** — it must match the ingest route you intend to deploy.
- **Do NOT omit `--esql-index` on Grafana/Prometheus** when the query stream differs from `--data-view`.
- **Do NOT treat `unknown` as proven or failed.** It means live target field caps were unavailable; rerun after data starts flowing.
- **Do NOT pass `--es-api-key` to `seed-sample-data` / `remove-sample-data`** — those commands take `--api-key` (or env `KEY`).
- An empty panel after upload is often missing/wrong-window data or wrong index, not a translator bug.

## See also

- `install-obs-migrate` — install/doctor when the CLI is missing or not Ready.
- `understand-source-schema` — exact source→Elastic field mapping model, profiles, and report locations.
- `remediate-field-mapping-gaps` — fix empty/wrong panels after upload.
- `explain-migration-gaps` — approximation warnings vs redesign work.
- `assess-migration-readiness` — readiness verdict from migration artifacts.
- `connect-to-o11y-source` — connect to Grafana/Datadog and Elastic endpoints.
- `validate-side-by-side` — numeric parity once data is flowing.
- `docs/command-contract.md` — `--field-profile`, `--esql-index`, seed/remove-sample-data.
