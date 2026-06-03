---
name: understand-source-schema
description: Understand how a source observability schema (Prometheus metric/label names, Datadog dotted metrics/tags) maps to Kibana/Elastic field names, see the concrete source-to-target field mapping for the user's own dashboards, and figure out what to change so migrated dashboards find data. Use when the user asks how their schema/fields/metric names/labels map or translate to Elastic, why migrated panels can't find data, what fields they need, or how to customize/override the field mapping (rule pack / field profile).
---

# Understand the source schema (source → Elastic field mapping)

Goal: help the user see exactly how their source field names become Elastic field names, get that mapping for **their** dashboards, and know how to override it. Source schemas (Prometheus `instance`/`job`/`node_cpu_seconds_total`, Datadog `system.cpu.user`/`host`) usually do **not** match Elastic field names, so this gap is expected, not a bug.

## How the mapping works

**Grafana** uses `SchemaResolver` (`adapters/source/grafana/schema.py`) with a 4-level priority chain:

1. Rule-pack `label_rewrites` (`--rules-file`) — highest
2. Live ES `_field_caps` discovery (`--es-url`) — picks the candidate that actually exists
3. Built-in Prometheus → OTel candidates (e.g. `instance` → `service.instance.id`/`host.name`, `job` → `service.name`, `namespace` → `k8s.namespace.name`)
4. Pass-through (use the label as-is) — lowest

**Datadog** uses **field profiles** (`--field-profile`): `metric_map` (explicit metric overrides), `tag_map` (tag → ES field), plus `metric_prefix`/`tag_prefix` for unmapped names. Built-ins: `otel` (default), `prometheus`, `elastic_agent`, `passthrough`. See `docs/sources/grafana.md` and `docs/sources/datadog.md` for the full tables.

## Get the mapping for the user's own dashboards

Two steps: migrate to an artifact dir, then generate the schema-change report.

```bash
set -a && source serverless_creds.env && set +a   # for live field check (optional but recommended)

.venv/bin/obs-migrate migrate \
  --source grafana --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --assets dashboards --native-promql \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY"

.venv/bin/python scripts/generate_telemetry_contract.py \
  migration_output/dashboards \
  --output migration_output/telemetry_contract.json \
  --schema-report migration_output/schema_change_report.md
```

`--es-url` matters here: it lets the resolver confirm which candidate target field actually exists, so the mapping reflects the real cluster.

## Where to read it

| What | File | Notes |
|---|---|---|
| **Per-panel source → target field map** | `migration_output/schema_change_report.md` | Purpose-built table: `dashboard │ panel │ source_fields │ target_stream │ target_fields`. **Start here.** |
| Required target fields + whether they exist | `migration_output/dashboards/required_target_contract.json` | each field has `status` (e.g. `confirmed`/`missing`) when `--es-url` was used |
| Per-panel translation detail | `migration_output/dashboards/verification_packets.json` | source vs. translated query per panel. **Open the file to read the exact key names** rather than assuming them — packet shape varies. |
| Must-fix worklist | `migration_output/dashboards/migration_summary.md` | human-readable |

## Customize / override the mapping

**Grafana** — emit a starter rule pack, edit, re-run with `--rules-file`:

```bash
.venv/bin/obs-migrate extensions --source grafana --format yaml --template-out custom-rule-pack.yaml
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
.venv/bin/obs-migrate migrate --source grafana ... --rules-file custom-rule-pack.yaml
```

The CLI can also suggest a starter pack from validation failures via `--suggest-rule-pack-out <path>` (writes auto-detected label candidates).

**Datadog** — pick a built-in `--field-profile {otel,prometheus,elastic_agent,passthrough}` or pass a custom YAML profile path (`metric_map`/`tag_map`). Emit a starter with `obs-migrate extensions --source datadog --template-out custom-field-profile.yaml`.

## Do NOT

- Do **not** assert `verification_packets.json` field/key names from memory — open the file and read them. Packet keys are easy to get subtly wrong.
- Do **not** invent metric-name transformation rules (e.g. exact `prometheus.<metric>.value` forms) without confirming against the emitted YAML/packets for the actual run.
- Do **not** treat a source-vs-Elastic naming difference as a migration bug — it is the schema gap this skill exists to map and resolve.

## See also

- `docs/sources/grafana.md` (SchemaResolver + rule packs) and `docs/sources/datadog.md` (field profiles) — the full mapping tables.
- `assess-migration-readiness` skill — `missing` fields/metrics show up there as blockers/actions.
- `observability_migration/core/telemetry_contract.py` (`build_schema_change_report`) — how the report is built.
