---
name: remediate-field-mapping-gaps
description: Use when migrated Kibana panels are empty, show missing/unknown fields, query the wrong index or data view, or the user needs to fix Prometheus label / Datadog tag / metric-name mapping gaps after an obs-migrate run.
---

# Remediate field mapping gaps

Goal: move from "the migrated panel is empty or wrong" to a concrete source-to-Elastic mapping fix. Field mapping gaps are expected in observability migrations; treat them as schema alignment work, not automatically as translator bugs.

## Start with the symptom

| Symptom | First move |
|---|---|
| Empty uploaded panel / "No results found" | Use `debug-uploaded-kibana-dashboard` to capture the exact ES|QL Kibana is running, then compare its fields and filters to the artifacts below |
| Unknown column / missing field error | Read the field name from the Kibana/ES error and locate it in `required_target_contract.json` (Grafana), `target_readiness_contract.json` (Datadog), or the emitted query |
| Values look wrong but data exists | Compare source query fields/tags to translated query fields in `verification_packets.json` and the schema report |
| Many panels fail the same way | Fix the rule pack / field profile and rerun; do not hand-edit every panel first |

## Package-native artifacts and commands

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout.

| What you need | File / command |
|---|---|
| Per-panel source fields -> target fields | `<output-dir>/dashboards/schema_change_report.md` (written by migration); use `obs-migrate schema-report --artifact-dir <output-dir>/dashboards --output schema_change_report.md --contract-out telemetry_contract.json` only to regenerate or combine outputs |
| Required target fields and missing/confirmed status | Grafana: `<output-dir>/dashboards/required_target_contract.json`; Datadog: `<output-dir>/dashboards/target_readiness_contract.json` |
| Source query and translated query | `<output-dir>/dashboards/verification_packets.json` |
| Human-readable must-fix list | `<output-dir>/dashboards/migration_summary.md` |
| Uploaded-panel runtime truth | `debug-uploaded-kibana-dashboard` capture of Kibana's actual `/_query` request |

## Remediation loop

1. **Prove it is a mapping/data-view issue** — confirm the target has data in the selected time range and index. Empty data is not a mapping fix.
2. **Open the schema report** — read `<output-dir>/dashboards/schema_change_report.md` and find the row for the failing panel. Regenerate with `obs-migrate schema-report` only if you are combining old artifact dirs or rebuilding the report.
3. **Check required fields** — open `required_target_contract.json` (Grafana) or `target_readiness_contract.json` (Datadog). Prioritize fields marked `missing` or `unknown`.
4. **Compare three sources of truth** — source query fields/tags, translated ES|QL fields, and Kibana's actual runtime query. If Kibana changed aliases or buckets, note that separately.
5. **Choose the right fix layer**:
   - **Grafana / PromQL:** add or adjust a rule pack and rerun with `--rules-file <custom-rule-pack.yaml>`.
   - **Datadog:** choose a built-in `--field-profile` (`otel`, `prometheus`, `elastic_agent`, `passthrough`) or pass a custom YAML profile.
   - **Target ingest:** if Elastic lacks the needed field entirely, fix the telemetry producer/index template/runtime field before rerunning migration.
6. **Use generated starters when possible** — Grafana runs can write suggestions with `--suggest-rule-pack-out <path>`; both sources can emit templates with `obs-migrate extensions --source grafana|datadog --template-out <path>`.
7. **Rerun the smallest useful scope** — prefer one dashboard (`try-one-source-dashboard`) or selected assets before a full sweep.
8. **Validate again** — use `validate-side-by-side` for numeric parity where applicable, and `debug-uploaded-kibana-dashboard` if the UI is still empty.

## Fix examples

Grafana rule-pack path:

```bash
obs-migrate extensions --source grafana --format yaml --template-out custom-rule-pack.yaml
# edit label_rewrites / label_candidates
obs-migrate migrate --source grafana ... --rules-file custom-rule-pack.yaml
```

Datadog field-profile path:

```bash
obs-migrate extensions --source datadog --template-out custom-field-profile.yaml
# edit metric_map / tag_map / prefixes
obs-migrate migrate --source datadog ... --field-profile custom-field-profile.yaml
```

## Honest limits / Do NOT

- **Do NOT invent `verification_packets.json` keys from memory.** Open the file and read the actual shape for the run.
- **Do NOT call every empty panel a translator bug.** Missing telemetry, wrong time range, wrong data view, and filters that match no documents are common.
- **Do NOT patch Kibana panels one-by-one before finding the shared mapping root cause** unless the user explicitly needs a one-off emergency repair.
- **Do NOT use repo-only scripts for package users.** `obs-migrate schema-report`, `obs-migrate extensions`, `--rules-file`, `--field-profile`, and `--suggest-rule-pack-out` are the package-native paths.
- **Do NOT present a custom rule pack/profile as proven until you rerun and validate the affected panel.**

## See also

- `understand-source-schema` — source-to-target mapping model and report locations.
- `debug-uploaded-kibana-dashboard` — capture Kibana's actual runtime query.
- `validate-side-by-side` — compare translated results after remediation.
- `try-one-source-dashboard` / `migrate-selected-assets` — rerun the smallest useful scope.
