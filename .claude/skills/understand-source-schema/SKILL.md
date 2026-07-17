---
name: understand-source-schema
description: Use when the user asks how their schema/fields/metric names/labels map or translate to Elastic, why migrated panels can't find data, what fields they need, or how to customize/override the field mapping (rule pack / field profile) — explains how a source observability schema (Prometheus metric/label names, Datadog dotted metrics/tags) maps to Kibana/Elastic field names, shows the concrete source-to-target field mapping for the user's own dashboards, and what to change so migrated dashboards find data.
---

# Understand the source schema (source → Elastic field mapping)

Goal: help the user see exactly how their source field names become Elastic field names, get that mapping for **their** dashboards, and know how to override it. Source schemas (Prometheus `instance`/`job`/`node_cpu_seconds_total`, Datadog `system.cpu.user`/`host`) usually do **not** match Elastic field names, so this gap is expected, not a bug.

## How the mapping works (Grafana)

`SchemaResolver` (`adapters/source/grafana/schema.py`) normally uses an explicit `--field-profile` as the **planned future Elastic layout**, then rewrites metric names, labels, and metric types to match it. This works before any target data exists. `auto` is an optional mode for populated targets that requires `--es-url` and infers the layout from `_field_caps`.

| Schema profile | How the data got into Elastic | Metric `http_requests_total` → | Label `service` → |
|---|---|---|---|
| `prometheus_remote_write` | Elastic Fleet/Agent Prometheus integration | `prometheus.http_requests_total.counter` / `.value` / `.rate` (suffix by role) | `prometheus.labels.service` |
| `prometheus_native` | Native ES `/_prometheus/api/v1/write` endpoint | `metrics.http_requests_total` | `labels.service` |
| `otel` (default) | OTel collector / generic normalized layout | `http_requests_total` (pass-through) | exact field → OTel candidate (`service.name`) → as-is |
| `passthrough` | Keep source metric/label names as-is | `http_requests_total` | `service` |
| `auto` (populated targets only; requires `--es-url`) | Infer from live `_field_caps` | remote_write / native when caps are clear; otherwise bare / OTel candidate + warn | same |

**Label resolution order** (`resolve_label`): ignored labels → rule-pack `label_rewrites` → exact field match (source-faithful) → profile-namespaced field (`prometheus.labels.<l>` / `labels.<l>`) → discovered OTel mapping from `_field_caps` → built-in candidate (e.g. `instance` → `service.instance.id`/`host.name`, `job` → `service.name`) → pass-through.

**Metric type matters too:** `rate()`/`irate()` only work if the metric is stored as a counter. `is_counter()` decides from rule-pack `metric_kinds` → `counter_suffixes` → the field's `time_series_metric` capability → the profile's counter field. A counter ingested as a gauge breaks rate math even when the field name is right.

**Histogram field type matters for `histogram_quantile`:** when field caps show `exponential_histogram` / `histogram`, translation uses `PERCENTILE()` (with `TO_TDIGEST()` for classic histograms). When the type is **unknown** (offline / no caps), the engine **assumes exponential_histogram and warns**. Known-wrong types such as `aggregate_metric_double` stay `not_feasible`. Prefer ES ≥ 9.5 native `histogram_quantile` when the runtime probe supports it.

**Assets first is supported.** Choose `otel`, `prometheus_remote_write`, `prometheus_native`, or `passthrough` from the intended ingest route, migrate the assets, ingest first, then rerun with `--es-url` and `--preflight`; `_field_caps` verifies the planned fields rather than defining the plan. Before that, `unknown` field status means verification is pending. Live `--es-url` also probes `esql_named_param_binding` and native `PROMQL` support (`--translation-mode`).

**Datadog** uses **field profiles** (`--field-profile`): `metric_map` (explicit metric overrides), `tag_map` (tag → ES field), plus `metric_prefix`/`tag_prefix` for unmapped names. Built-ins: `otel` (default), `prometheus` (Metricbeat remote_write), `prometheus_native` (ES `/_prometheus`), `elastic_agent`, `passthrough`. Datadog has **no `auto`** — always pick an explicit plan, then verify with `--es-url`. See `docs/sources/grafana.md` and `docs/sources/datadog.md` for the full tables.

## Get the mapping for the user's own dashboards

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. First run with the planned profile; add a live `--es-url` after telemetry starts so the resolver can confirm which target fields actually exist:

```bash
export GRAFANA_URL="https://grafana.example.com" GRAFANA_USER="..." GRAFANA_PASS="..."
export ELASTICSEARCH_ENDPOINT="https://...es..." KEY="<api-key>"

obs-migrate migrate \
  --source grafana --input-mode api \
  --output-dir migration_output \
  --assets dashboards \
  --field-profile prometheus_remote_write \
  --data-view "metrics-*" \
  --esql-index "metrics-*" \
  --preflight \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY"
```

(Have exported JSON instead of API access? Use `--input-mode files --input-dir <their-dashboards-dir>`.) `--es-url` is what makes the field-existence (`confirmed`/`missing`) check meaningful; `--preflight` writes the contract artifacts below. For Prometheus, set `--esql-index` to the metrics query/discovery stream even when `--data-view` differs as the Kibana UI bind (`docs/command-contract.md` → Target index flags).
## Get the purpose-built per-panel mapping table (start here)

The most direct answer to "how do my fields map?" is the **schema-change report**, a per-panel `dashboard │ panel │ source_fields │ target_stream │ target_fields` table. Dashboard migration writes it automatically at `<output-dir>/dashboards/schema_change_report.md`, alongside `<output-dir>/dashboards/telemetry_contract.json`.

To regenerate the report, or to merge several source outputs into one table, use the installed package command (no source checkout, no `scripts/` directory needed):

```bash
obs-migrate schema-report \
  --artifact-dir migration_output/dashboards \
  --output schema_change_report.md
```

Point `--artifact-dir` at the per-source `dashboards/` output (the dir containing `yaml/` and `verification_packets.json`). Repeat `--artifact-dir` to merge several sources into one report. Add `--contract-out telemetry_contract.json` to also emit the machine-readable telemetry producer contract. Open `schema_change_report.md` and read the table.

## Where else the same information lives

These artifacts are also written by the migration run itself, under `migration_output/dashboards/`:

| What | File | Notes |
|---|---|---|
| **Grafana required target fields + whether they exist** | `required_target_contract.json` | includes `field_profile`, `planned_schema_profile`, `detected_schema_profile`, `profile_mismatch`, backward-compatible `schema_profile` (detected layout), `field_capabilities_discovery`, and each resolved target field's `status` (e.g. `confirmed`/`missing`/`unknown`) when `--es-url` was used. |
| **Datadog required target fields + whether they exist** | `target_readiness_contract.json` | includes the active `field_profile`, metric/log index patterns, source fields, resolved target fields, and `status`. |
| Per-panel translation detail (source vs. translated query) | `verification_packets.json` | **Open the file to read the exact key names** rather than assuming them — packet shape varies. |
| Must-fix worklist | `migration_summary.md` | human-readable verdict + actions |

## Customize / override the mapping

**Grafana** — emit a starter rule pack, edit, re-run with `--rules-file`:

```bash
obs-migrate extensions --source grafana --format yaml --template-out custom-rule-pack.yaml
```

```yaml
query:
  label_rewrites:
    instance: my.host.field
    job: my.service.field
  label_candidates:
    datacenter: [cloud.region, cloud.availability_zone]
  ignored_labels: [__name__]
controls:
  field_overrides:
    instance: service.instance.id
```

```bash
obs-migrate migrate --source grafana ... --rules-file custom-rule-pack.yaml
```

The CLI can also suggest a starter pack from validation failures via `--suggest-rule-pack-out <path>` (writes auto-detected label candidates). `extensions` and `--suggest-rule-pack-out` are shipped in the package.

**Datadog** — pick a built-in `--field-profile {otel,prometheus,prometheus_native,elastic_agent,passthrough}` or pass a custom YAML profile path (`metric_map`/`tag_map`). Emit a starter with `obs-migrate extensions --source datadog --template-out custom-field-profile.yaml`.

## Do NOT

- Do **not** assert `verification_packets.json` field/key names from memory — open the file and read them. Packet keys are easy to get subtly wrong.
- Do **not** invent metric-name transformation rules (e.g. exact `prometheus.<metric>.value` forms) without confirming against the emitted YAML/packets for the actual run.
- Do **not** use Grafana `auto` before telemetry exists. Use an explicit planned profile, then rerun validation with a reachable `--es-url` after ingest begins.
- Do **not** treat a source-vs-Elastic naming difference as a migration bug — it is the schema gap this skill exists to map and resolve.
- Do **not** reach for repo-only scripts for the schema report: migration writes `schema_change_report.md` automatically, and `obs-migrate schema-report` is the package-native regeneration/merge command. (`scripts/generate_telemetry_contract.py` is the same thing in a source checkout.)

## See also

- `obs-migrate schema-report --help` — the per-panel source→target table command (shipped in the package).
- `docs/sources/grafana.md` (SchemaResolver + rule packs + Current Boundaries) and `docs/sources/datadog.md` (field profiles) — the full mapping tables (online docs / repo).
- `prepare-target-telemetry` skill — choose ingest route / `--esql-index` before data exists.
- `assess-migration-readiness` skill — `missing` fields/metrics show up there as blockers/actions.
- `explain-migration-gaps` skill — approximation warnings (e.g. histogram assume+warn) vs mapping bugs.
- `obs-migrate extensions --help` and `grafana-migrate --help` — rule-pack and `--rules-file` options for the installed version.
