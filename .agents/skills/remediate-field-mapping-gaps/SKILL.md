---
name: remediate-field-mapping-gaps
description: Use when migrated Kibana panels are empty, show missing/unknown fields, query the wrong index or data view, or the user needs to fix Prometheus label / Datadog tag / metric-name mapping gaps after an obs-migrate run.
---

# Remediate field mapping gaps

**Audience:** operators of the published `obs-migrate` CLI (PyPI/`uvx`), using public docs and their real source + Elastic/Kibana — not a repo lab harness.

Goal: move from "the migrated panel is empty or wrong" to a concrete source-to-Elastic mapping fix. Field mapping gaps are expected in observability migrations; treat them as schema alignment work, not automatically as translator bugs.

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


## Start with the symptom

| Symptom | First move |
|---|---|
| Empty uploaded panel / "No results found" | Use `debug-uploaded-kibana-dashboard` to capture the exact ES|QL Kibana is running, then compare its fields and filters to the artifacts below. Classify with render-audit taxonomy when available: `render_error` (bug) vs `field_gap` / `data_gap` / `unexpected_empty` (data readiness) |
| Unknown column / missing field error | Read the field name from the Kibana/ES error and locate it in `required_target_contract.json` (Grafana), `target_readiness_contract.json` (Datadog), or the emitted query. For Grafana, also check `--esql-index` vs `--data-view` |
| Values look wrong but data exists | First rule out **accepted approximations** (`explain-migration-gaps` / `docs/sources/grafana.md` Current Boundaries: `histogram_quantile` → `PERCENTILE`, histogram mean ratio-of-aggregates, multi-target fusion). Only then compare source vs translated fields in `verification_packets.json` / schema report |
| Many panels fail the same way | Fix the rule pack / field profile / index flags and rerun; do not hand-edit every panel first |

## Package-native artifacts and commands

| What you need | File / command |
|---|---|
| Per-panel source fields -> target fields | `<output-dir>/dashboards/schema_change_report.md` (written by migration); use `obs-migrate schema-report --artifact-dir <output-dir>/dashboards --output schema_change_report.md --contract-out telemetry_contract.json` only to regenerate or combine outputs |
| Required target fields and missing/confirmed status | Grafana: `<output-dir>/dashboards/required_target_contract.json`; Datadog: `<output-dir>/dashboards/target_readiness_contract.json` (look at `totals.fields_missing` / `required_fields.*.status`) |
| Source query and translated query | `<output-dir>/dashboards/verification_packets.json` |
| Human-readable must-fix list | `<output-dir>/dashboards/migration_summary.md` |
| Uploaded-panel runtime truth | `debug-uploaded-kibana-dashboard` capture of Kibana's actual `/_query` request |

## Remediation loop

1. **Prove it is a mapping / index / data issue** — confirm the target has data in the selected time range and **correct stream**. For Grafana Prometheus panels, wrong/missing `--esql-index` (query + discovery target) vs `--data-view` (Kibana UI bind) is the most common empty-panel cause — fix flags before rewriting rule packs (`docs/command-contract.md` → Target index flags). Empty data is not a mapping fix.
2. **Open the schema report** — read `<output-dir>/dashboards/schema_change_report.md` and find the row for the failing panel. Regenerate with `obs-migrate schema-report` only if you are combining old artifact dirs or rebuilding the report.
3. **Check required fields** — open `required_target_contract.json` (Grafana) or `target_readiness_contract.json` (Datadog). Prioritize fields marked `missing` or `unknown`.
4. **Compare three sources of truth** — source query fields/tags, translated ES|QL fields (prefer `native/*.native.json` / `ir/*.ir.json`), and Kibana's actual runtime query. If Kibana changed aliases or buckets, note that separately.
5. **Choose the right fix layer**:
   - **Wrong index / data view (Grafana):** set `--esql-index` and `--data-view` correctly and rerun — often no rule-pack change needed.
   - **Approximation, not mapping:** if `reasons` cite `PERCENTILE` / assumed histogram / ratio of aggregates / multi-target fusion, explain via `explain-migration-gaps` — do not thrash profiles to "fix" expected warnings.
   - **Grafana / PromQL mapping:** add or adjust a rule pack and rerun with `--rules-file <custom-rule-pack.yaml>` on `obs-migrate migrate --source grafana` (or `grafana-migrate`).
   - **Datadog:** choose a built-in `--field-profile` (`otel`, `prometheus`, `prometheus_native`, `elastic_agent`, `passthrough`) or pass a custom YAML profile path.
   - **Target ingest:** if Elastic lacks the needed field entirely, fix the telemetry producer/index template/runtime field before rerunning migration (`prepare-target-telemetry`).
6. **Use generated starters when possible**:
   - Both sources: `obs-migrate extensions --source grafana|datadog --format yaml --template-out <path>`.
   - **Grafana suggested pack from live validation failures:** use the **`grafana-migrate`** alias (this flag is **not** on `obs-migrate migrate`):

```bash
grafana-migrate \
  --input-mode files --input-dir <dashboards> \
  --output-dir <out> \
  --data-view "metrics-*" --esql-index "metrics-*" \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY" \
  --validate \
  --suggest-rule-pack-out suggested-rule-pack.yaml
```

   `--suggest-rule-pack-out` writes only when validation produced a summary (typically needs `--es-url` + `--validate`). Review the generated file before using it as `--rules-file`.
7. **Rerun the smallest useful scope** — prefer one dashboard (`try-one-source-dashboard`) or selected assets before a full sweep.
8. **Validate again** — use `validate-side-by-side` for numeric parity where applicable, render audit / `debug-uploaded-kibana-dashboard` if the UI is still empty.

## Fix examples

Grafana rule-pack path:

```bash
obs-migrate extensions --source grafana --format yaml --template-out custom-rule-pack.yaml
# edit label_rewrites / label_candidates
obs-migrate migrate --source grafana ... --rules-file custom-rule-pack.yaml \
  --data-view "metrics-*" --esql-index "metrics-*"
```

Datadog field-profile path:

```bash
obs-migrate extensions --source datadog --format yaml --template-out custom-field-profile.yaml
# edit metric_map / tag_map / prefixes
obs-migrate migrate --source datadog ... --field-profile custom-field-profile.yaml
```

## Honest limits / Do NOT

- **Do NOT invent `verification_packets.json` keys from memory.** Open the file and read the actual shape for the run.
- **Do NOT call every empty panel a translator bug.** Missing telemetry, wrong time range, wrong `--esql-index`/`--data-view`, and filters that match no documents are common.
- **Do NOT treat approximation warnings as mapping bugs** — triage with `explain-migration-gaps` first.
- **Do NOT patch Kibana panels one-by-one before finding the shared mapping root cause** unless the user explicitly needs a one-off emergency repair.
- **Do NOT pass `--suggest-rule-pack-out` to `obs-migrate migrate`** — it is unrecognized there; use `grafana-migrate`.
- **Do NOT use repo-only scripts for package users.** `obs-migrate schema-report`, `obs-migrate extensions`, `--rules-file`, `--field-profile`, and `grafana-migrate --suggest-rule-pack-out` are the package-native paths.
- **Do NOT present a custom rule pack/profile as proven until you rerun and validate the affected panel.**

## See also

- `install-obs-migrate` — install/doctor when the CLI is missing or not Ready.
- `understand-source-schema` — source-to-target mapping model and report locations.
- `prepare-target-telemetry` — ingest route / index flags before blaming translation.
- `explain-migration-gaps` — approximation vs redesign triage.
- `debug-uploaded-kibana-dashboard` — capture Kibana's actual runtime query + render-audit taxonomy.
- `validate-side-by-side` — compare translated results after remediation.
- `try-one-source-dashboard` / `migrate-selected-assets` — rerun the smallest useful scope.
- `docs/testing.md` — render-audit classifications.
