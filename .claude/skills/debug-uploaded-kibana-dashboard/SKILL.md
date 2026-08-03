---
name: debug-uploaded-kibana-dashboard
description: Use when an operator reports a migrated Kibana panel empty / "No results found" / "Migration Required" / wrong values after obs-migrate upload, or asks why a panel is broken — diagnoses uploaded dashboards with package CLIs (grafana-validate-uploaded, artifacts, /_query) and plain Kibana UI checks. Not for repo test harnesses or translator development.
---

# Debug an uploaded Kibana dashboard

**Audience:** operators running the published `obs-migrate` CLI against their real Grafana/Datadog source and Elastic/Kibana target. Help them decide whether the problem is missing data, wrong index/profile, an accepted translation warning, or a panel that needs rebuild — using only package commands and artifacts from their migrate run.

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


## What success looks like for the operator

You can answer, with evidence:

1. Which dashboard id / panel is broken  
2. Whether Kibana’s query returns rows, errors, or empty  
3. Whether the fix is **data/ingest**, **flags/profile**, **accepted approximation**, or **manual rebuild in Kibana**  
4. The next concrete CLI or Kibana step (not a code change)

## Fast triage (package-native — do this first)

```bash
# 1) dashboard_id from the migrate output:
#    <output-dir>/dashboards/native/*.native.json → "dashboard_id"
obs-migrate cluster list-dashboards \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# 2) Runtime empty / error panels (no browser automation required):
grafana-validate-uploaded \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY" \
  --dashboard-id <dashboard_id> \
  --output /tmp/upload-validate.json

# 3) Replay an empty panel query from that report against Elasticsearch:
curl -sS -H "Authorization: ApiKey $KEY" -H 'Content-Type: application/json' \
  "$ELASTICSEARCH_ENDPOINT/_query" \
  -d '{"query":"<materialized_query from the report>"}'
```

Open the dashboard in Kibana (view mode):  
`$KIBANA_ENDPOINT/app/dashboards#/view/<dashboard_id>`

On Elastic Serverless, a raw `GET /api/dashboards/dashboard/<id>` may 404 even when the dashboard lists — trust `cluster list-dashboards` and `grafana-validate-uploaded`.

### Read the migrate artifacts (same `--output-dir` as the upload)

| File | Use |
|---|---|
| `dashboards/migration_summary.md` | Must-fix / scorecard |
| `dashboards/migration_manifest.json` | Per-panel `status`, `reasons`, warnings |
| `dashboards/native/*.native.json` | Uploaded shape + `dashboard_id` |
| `dashboards/ir/*.ir.json` | Translator decisions (inspection-only; not re-ingested by CLI) |
| `dashboards/verification_packets.json` | Source vs translated query context |
| `dashboards/schema_change_report.md` | Field mapping table |
| `target_readiness_contract.json` (Datadog) / `required_target_contract.json` (Grafana) | Missing vs confirmed fields |

A migration writes no `dashboards/yaml/` — `native/` + `ir/` plus the validate report are “what Kibana ran.”

## Decision tree

| What the operator sees | Class | Next |
|---|---|---|
| Values wrong but panel has data | Wrong query / mapping / approximation | Compare manifest `reasons` + validate query; see `explain-migration-gaps` / `remediate-field-mapping-gaps` |
| “No results found” / validate `empty_panels` with `rows: 0` | Data, filter, time range, or wrong index | Widen time range; check `--esql-index` / `--data-view` / `--field-profile`; `prepare-target-telemetry` |
| “Migration Required” markdown | Hard stop / placeholder | Read `reasons` → `explain-migration-gaps` (rebuild guidance) |
| Red toast / runtime error in validate report | ES/Lens error | Classify unknown column / circuit breaker / type error below |
| Query returns rows in `/_query` but UI still empty | Kibana UI / Lens display | Screenshot + Elastic support / UI check; not automatically a migrate bug |

Render-audit style labels (when you have them): `render_error` (real product bug) vs `field_gap` / `data_gap` / `unexpected_empty` (data readiness). Prefer honest data-gap language over blaming the translator when `/_query` returns 0 rows.

## Empty panel checklist

1. **Time range** — match the source dashboard window in Kibana.  
2. **Index / data view** — Grafana: `--esql-index` (query) vs `--data-view` (UI). Wrong stream is the most common empty Prometheus panel.  
3. **Field profile** — Datadog/Grafana profile must match how telemetry was ingested (`prepare-target-telemetry`).  
4. **Filters** — strip a suspicious `WHERE` and re-run `/_query`; check distinct values for the filter field.  
5. **Seed only for lab proof** — optional `obs-migrate seed-sample-data` / `remove-sample-data` (`--api-key`, not `--es-api-key`) to prove panels can light up; not a substitute for real ingest.

## Runtime errors (toasts / validate report)

- **Circuit breaker / data too large** — cluster capacity; narrow time range; not a migrate bug.  
- **Unknown column that appears in the query** — missing field in the target index → ingest / profile / index flags.  
- **Unknown column that does not appear in the query** — alias/mapping mismatch → `remediate-field-mapping-gaps`.  
- **Counter/gauge or native PROMQL verification errors** — try `--translation-mode esql` or fix metric typing in ingest; see `explain-migration-gaps`.

## After you know the class (operator actions)

| Class | Operator action |
|---|---|
| Missing / wrong telemetry | Fix ingest or flags; re-run migrate on the one dashboard; re-upload |
| Accepted approximation | Document and accept, or rebuild the panel in Kibana (`explain-migration-gaps`) |
| `not_feasible` / blocked | Rebuild in Kibana using reasons + recommended targets |
| Suspected product bug | Capture dashboard id, panel title, materialized query, validate JSON path; open an issue with those artifacts (no need to patch the translator yourself) |

Re-migrate the smallest scope:

```bash
# Datadog example
obs-migrate migrate --source datadog --input-mode api \
  --dashboard-ids <id> --output-dir retry_out --assets dashboards \
  --field-profile otel --data-view "metrics-*" \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY" \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --upload
```

Then re-run `grafana-validate-uploaded` on the same `dashboard_id`.

## Optional deeper checks (not required for operators)

These help when an agent or lab environment is available; **do not block the operator journey on them**:

- Browser DevTools / screenshots while signed into Kibana (capture the Lens `/_query` body if validate did not).  
- `obs-migrate verify` for emitted-query acceptance.  
- Repo-only gates (`verify-panels`, `parity-rig`, render-audit scripts) — see `https://github.com/elastic/observability-migration-platform/blob/main/docs/testing.md` if you have a checkout; bare `uvx` does not include those harnesses.

## Do NOT

- Do **not** require cloning this repo, `parity-rig/`, or unit tests to help an operator.  
- Do **not** invent `OBS_MIGRATE_CREDS` — use ordinary env exports.  
- Do **not** blame the translator when `/_query` returns 0 rows for missing data.  
- Do **not** hand-edit every Kibana panel before fixing shared index/profile/ingest issues.  
- Do **not** paste full network bodies into chat — save files and summarize.

## See also

- `install-obs-migrate` — Ready install.  
- `prepare-target-telemetry` / `remediate-field-mapping-gaps` — empty from data/mapping.  
- `explain-migration-gaps` — warnings and rebuild guidance.  
- `validate-side-by-side` — numeric/structural parity (`obs-migrate compare`).  
- `revert-migration` — remove a bad upload.  
- `https://github.com/elastic/observability-migration-platform/blob/main/docs/command-contract.md` — `upload`, `grafana-validate-uploaded`, `verify`.  
- `https://github.com/elastic/observability-migration-platform/blob/main/docs/testing.md` — layered gates (including optional lab/repo tools).
