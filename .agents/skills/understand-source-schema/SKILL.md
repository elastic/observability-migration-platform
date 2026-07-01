---
name: understand-source-schema
description: Use when the user asks how their schema/fields/metric names/labels map or translate to Elastic, why migrated panels can't find data, what fields they need, or how to customize/override the field mapping (rule pack / field profile) — explains how a source observability schema (Prometheus metric/label names, Datadog dotted metrics/tags) maps to Kibana/Elastic field names, shows the concrete source-to-target field mapping for the user's own dashboards, and what to change so migrated dashboards find data.
---

# Understand the source schema (source → Elastic field mapping)

Goal: help the user see exactly how their source field names become Elastic field names, get that mapping for **their** dashboards, and know how to override it. Source schemas (Prometheus `instance`/`job`/`node_cpu_seconds_total`, Datadog `system.cpu.user`/`host`) usually do **not** match Elastic field names, so this gap is expected, not a bug.

## How the mapping works (Grafana)

`SchemaResolver` (`adapters/source/grafana/schema.py`) first **auto-detects how your Prometheus data actually landed in Elastic** by reading `_field_caps` from `--es-url`, then rewrites metric names, labels, and metric types to match that layout. There are three layouts (schema profiles):

| Schema profile | How the data got into Elastic | Metric `http_requests_total` → | Label `service` → |
|---|---|---|---|
| `prometheus_remote_write` | Elastic Fleet/Agent Prometheus integration | `prometheus.http_requests_total.counter` / `.value` / `.rate` (suffix by role) | `prometheus.labels.service` |
| `prometheus_native` | Native ES `/_prometheus/api/v1/write` endpoint | `metrics.http_requests_total` | `labels.service` |
| generic / OTel (none detected) | OTel collector, custom mapping, or no data found | `http_requests_total` (pass-through) | exact field → OTel candidate (`service.name`) → as-is |

**Label resolution order** (`resolve_label`): ignored labels → rule-pack `label_rewrites` → exact field match (source-faithful) → profile-namespaced field (`prometheus.labels.<l>` / `labels.<l>`) → discovered OTel mapping from `_field_caps` → built-in candidate (e.g. `instance` → `service.instance.id`/`host.name`, `job` → `service.name`) → pass-through.

**Metric type matters too:** `rate()`/`irate()` only work if the metric is stored as a counter. `is_counter()` decides from rule-pack `metric_kinds` → `counter_suffixes` → the field's `time_series_metric` capability → the profile's counter field. A counter ingested as a gauge breaks rate math even when the field name is right.

**Hard dependency — ingest first, then migrate with `--es-url` and `--preflight`.** Profile detection only works when the data is already in Elastic *and* `--es-url` is reachable. If it is not (wrong/missing key, TLS failure, or **migrating before pointing Prometheus at Elastic**), detection finds nothing, the profile is `none`, and the resolver falls back to OTel candidates + pass-through — dashboards look migrated but query the wrong fields. Confirm the profile via Grafana `required_target_contract.json` (`schema_profile`, `field_capabilities_discovery`, field `status`) from a preflight run before trusting any mapping.

**Datadog** uses **field profiles** (`--field-profile`): `metric_map` (explicit metric overrides), `tag_map` (tag → ES field), plus `metric_prefix`/`tag_prefix` for unmapped names. Built-ins: `otel` (default), `prometheus`, `elastic_agent`, `passthrough`. See `docs/sources/grafana.md` and `docs/sources/datadog.md` for the full tables.

## Get the mapping for the user's own dashboards

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. Run a migration to an artifact dir with a live `--es-url` so the resolver can confirm which target fields actually exist:

```bash
export GRAFANA_URL="https://grafana.example.com" GRAFANA_USER="..." GRAFANA_PASS="..."
export ELASTICSEARCH_ENDPOINT="https://...es..." KEY="<api-key>"

obs-migrate migrate \
  --source grafana --input-mode api \
  --output-dir migration_output \
  --assets dashboards --preflight \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY"
```

(Have exported JSON instead of API access? Use `--input-mode files --input-dir <their-dashboards-dir>`.) `--es-url` is what makes the field-existence (`confirmed`/`missing`) check meaningful; `--preflight` writes the contract artifacts below.

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
| **Grafana required target fields + whether they exist** | `required_target_contract.json` | includes `schema_profile`, `field_capabilities_discovery`, and each resolved target field's `status` (e.g. `confirmed`/`missing`/`unknown`) when `--es-url` was used. |
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

**Datadog** — pick a built-in `--field-profile {otel,prometheus,elastic_agent,passthrough}` or pass a custom YAML profile path (`metric_map`/`tag_map`). Emit a starter with `obs-migrate extensions --source datadog --template-out custom-field-profile.yaml`.

## Do NOT

- Do **not** assert `verification_packets.json` field/key names from memory — open the file and read them. Packet keys are easy to get subtly wrong.
- Do **not** invent metric-name transformation rules (e.g. exact `prometheus.<metric>.value` forms) without confirming against the emitted YAML/packets for the actual run.
- Do **not** trust field mappings from a run where `--es-url` was unreachable or the target had no data yet — with no detected schema profile the resolver guesses OTel candidates and passes names through. Ingest first, then re-run with a reachable `--es-url`.
- Do **not** treat a source-vs-Elastic naming difference as a migration bug — it is the schema gap this skill exists to map and resolve.
- Do **not** reach for repo-only scripts for the schema report: migration writes `schema_change_report.md` automatically, and `obs-migrate schema-report` is the package-native regeneration/merge command. (`scripts/generate_telemetry_contract.py` is the same thing in a source checkout.)

## See also

- `obs-migrate schema-report --help` — the per-panel source→target table command (shipped in the package).
- `docs/sources/grafana.md` (SchemaResolver + rule packs) and `docs/sources/datadog.md` (field profiles) — the full mapping tables (online docs / repo).
- `assess-migration-readiness` skill — `missing` fields/metrics show up there as blockers/actions.
- `obs-migrate extensions --help` and `grafana-migrate --help` — rule-pack and `--rules-file` options for the installed version.
