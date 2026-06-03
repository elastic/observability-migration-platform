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

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. Run a migration to an artifact dir with a live `--es-url` so the resolver can confirm which target fields actually exist:

```bash
export GRAFANA_URL="https://grafana.example.com" GRAFANA_USER="..." GRAFANA_PASS="..."
export ELASTICSEARCH_ENDPOINT="https://...es..." KEY="<api-key>"

obs-migrate migrate \
  --source grafana --input-mode api \
  --output-dir migration_output \
  --assets dashboards --native-promql --preflight \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY"
```

(Have exported JSON instead of API access? Use `--input-mode files --input-dir <their-dashboards-dir>`.) `--es-url` is what makes the field-existence (`confirmed`/`missing`) check meaningful; `--preflight` writes the contract artifacts below.

## Get the purpose-built per-panel mapping table (start here)

The most direct answer to "how do my fields map?" is the **schema-change report**, a per-panel `dashboard │ panel │ source_fields │ target_stream │ target_fields` table. It ships in the installed package as the `obs-migrate schema-report` subcommand (no source checkout, no `scripts/` directory needed):

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
| **Required target fields + whether they exist** | `required_target_contract.json` | each field has a `status` (e.g. `confirmed`/`missing`) when `--es-url` was used. Best for "which fields are missing/renamed". |
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
- Do **not** treat a source-vs-Elastic naming difference as a migration bug — it is the schema gap this skill exists to map and resolve.
- Do **not** reach for repo-only scripts for the schema report: `obs-migrate schema-report` is the package-native command. (`scripts/generate_telemetry_contract.py` is the same thing in a source checkout.)

## See also

- `obs-migrate schema-report --help` — the per-panel source→target table command (shipped in the package).
- `docs/sources/grafana.md` (SchemaResolver + rule packs) and `docs/sources/datadog.md` (field profiles) — the full mapping tables (online docs / repo).
- `assess-migration-readiness` skill — `missing` fields/metrics show up there as blockers/actions.
- `obs-migrate extensions --help` and `grafana-migrate --help` — rule-pack and `--rules-file` options for the installed version.
