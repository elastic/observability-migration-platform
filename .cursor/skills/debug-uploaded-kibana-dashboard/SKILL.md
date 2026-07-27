---
name: debug-uploaded-kibana-dashboard
description: Use when the user reports a panel rendering empty / "No results found" / "Migration Required" / wrong-shape values after obs-migrate upload (or hands over a Kibana dashboard URL and asks "why is this panel broken") — diagnoses a Kibana dashboard that obs-migrate uploaded by capturing the ES|QL Kibana actually runs (package-native validators first; Chrome DevTools MCP / agent-browser when UI capture is needed), classifying empty vs runtime error vs not_feasible, and feeding real translator bugs back to the pipeline.
---

# Debug an uploaded Kibana dashboard

Diagnose why an **uploaded** migrated dashboard panel is empty, errored, or wrong in Kibana. Prefer **package-native** evidence first (`grafana-validate-uploaded`, artifacts, direct `/_query`); escalate to Chrome DevTools MCP / `agent-browser` when you need the exact Lens network request or a screenshot.

## Prerequisites (install)

If `obs-migrate` is missing, `uvx`/`doctor` fails, or the tool is not **Ready**,
**stop and follow `install-obs-migrate` first** — that skill owns PyPI/`uvx`/
pip install, extras (`[all]` / `[grafana]` / `[datadog]`), Python/`uv` gotchas,
and the Ready check. Do not invent alternate install commands here.
Credentials and live source proof stay in `connect-to-o11y-source`.

Use the installed `obs-migrate` CLI (or `uvx --from 'elastic-observability-migration[all]' obs-migrate …` after install). Prefix `.venv/bin/` only for a repo checkout.


## Fast package-native triage (do this first)

```bash
export ELASTICSEARCH_ENDPOINT="..." KIBANA_ENDPOINT="..." KEY="..."
# or: set -a; source ./serverless_creds.env; set +a

# 1) Confirm the dashboard id from the migrate artifacts:
#    <output-dir>/dashboards/native/*.native.json → dashboard_id
obs-migrate cluster list-dashboards \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" | rg -i '<title-or-id>'

# 2) Runtime emptiness / errors without a browser:
grafana-validate-uploaded \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY" \
  --dashboard-id <dashboard_id> \
  --output /tmp/upload-validate-<slug>.json

# 3) Replay an empty panel's materialized_query from that report:
curl -sS -H "Authorization: ApiKey $KEY" -H 'Content-Type: application/json' \
  "$ELASTICSEARCH_ENDPOINT/_query" \
  -d "{\"query\": \"<materialized_query from report>\"}"
```

Deep link: `$KIBANA_ENDPOINT/app/dashboards#/view/<dashboard_id>` (from `native/*.native.json` `dashboard_id`). On Elastic Serverless, `GET /api/dashboards/dashboard/<id>` may 404 even when the dashboard is listed — trust `cluster list-dashboards` / `grafana-validate-uploaded`, not that GET alone.

Classify from the validate report:

| Report signal | Likely class | Next step |
|---|---|---|
| `empty_panels` with `rows: 0`, no error | data / filter / field gap | Workflow B |
| `runtime_error_panels` / ES error text | runtime / mapping | Workflow D |
| Panel is markdown / status `not_feasible` in manifest | translator hard stop | Workflow C |
| Query returns rows but UI empty | Lens/UI / Suspense | Chrome / render-audit |

Also open artifacts (prefer user's `--output-dir`):

- `dashboards/migration_manifest.json` — `status`, `reasons`, `warnings`
- `dashboards/native/*.native.json` / `ir/*.ir.json` — emitted panel config / queries
- `dashboards/verification_packets.json` — source vs translated query context
- YAML under `dashboards/yaml/` is a **compatibility** view, not the Lens runtime truth

Map UI emptiness to **render-audit taxonomy** when available (`docs/testing.md`): `render_error` (translator/Lens bug, fail) vs `field_gap` / `data_gap` / `unexpected_empty` (data readiness, warn).

## Decision tree (when looking at the Kibana UI)

1. **Does the panel actually render with values?** → If wrong-looking, *Workflow A*.
2. **"No results found" / empty?** → *Workflow B*.
3. **"Migration Required" markdown?** → *Workflow C*.
4. **Red toast / runtime error?** → *Workflow D*.

## Workflow A — "panel shows wrong values"

Need the **exact** ES|QL Lens sent (may differ from native artifacts).

**Package-first:** use `grafana-validate-uploaded` query fields / `obs-migrate verify` emitted-query gate when they already captured the query.

**Browser (Chrome DevTools MCP):** prerequisites — `chrome-devtools` MCP with `--autoConnect` (see [setup](~/.cursor/skills/chrome-devtools-debugging/setup-autoconnect.md)); Chrome signed into the target Kibana on the dashboard.

1. **`take_snapshot`** to map the panel.
2. **`list_network_requests`** with `resourceTypes: ["xhr","fetch"]`; filter URLs ending in `_query`.
3. **`get_network_request`** → save request/response under `/tmp/<panel-slug>.*`. Request body `query` is the Lens truth.
4. Compare to `native/*.native.json` / `ir/*.ir.json`. Kibana may add `BUCKET(@timestamp,...)` or aliases — that is downstream transform, not automatically a translator bug.
5. Re-run via `curl` `$ELASTICSEARCH_ENDPOINT/_query` with the same query body.
6. Cluster correct + Lens wrong → UI/mapping issue (screenshot). Cluster wrong → translator/data (Workflow B/C).

## Workflow B — "No results found"

Order of frequency:

### B1: Filter / time window matches no docs

1. Capture query (validate report or Workflow A).
2. Strip the suspected `WHERE` / widen the time range; re-run `/_query`.
3. Inspect distinct values:
   `FROM <index> | WHERE <metric> IS NOT NULL | STATS n = COUNT(*) BY <field> | SORT n DESC | LIMIT 20`
4. If production filters are fine but **lab** data lacks them, that is a data/seed gap (`prepare-target-telemetry` / `seed-sample-data`), not a silent translator pass. Do **not** require `parity-rig/producer` for package operators.

### B2: Field name resolved wrong

```bash
curl -sS -H "Authorization: ApiKey $KEY" \
  "$ELASTICSEARCH_ENDPOINT/<index>/_field_caps?fields=instance,service.instance.id" | jq .
```

Compare to the emitted filter fields. Fix via rule pack / `--field-profile` / `remediate-field-mapping-gaps` (package path), not by hand-editing Kibana first.

### B3: Aggregation collapses to null

Multi-target TS collapse picking null-only rows — historically fixed with null-safe `MAX` collapse in PromQL translation. If a **new** shape recurs, reproduce with a unit test; do not "fix" by editing the uploaded saved object alone.

## Workflow C — "Migration Required" markdown

1. Snapshot/screenshot the panel (browser) or read markdown from artifacts.
2. Find the panel in `migration_manifest.json` / `migration_report.json` by title; read `reasons` / status.
3. Classify against `docs/sources/grafana.md` Current Boundaries (approximations vs hard stops). Do **not** call standard `histogram_quantile` + `sum(... by (le))` "out of scope" when status is `migrated_with_warnings`.
4. Hand plain-language rebuild guidance to `explain-migration-gaps`.

## Workflow D — runtime error popup

1. Browser: `list_console_messages` / network error body — richer than the toast.
2. Or package: `grafana-validate-uploaded` / `obs-migrate verify` error classification (`real_bug` vs `data_gap`).
3. Classify:
   - circuit breaker / data too large → cluster pressure, not translator
   - `Unknown column [X]` present in query → data/index gap
   - `Unknown column [Y]` **not** in query → alias/SchemaResolver mismatch class
   - counter/gauge / native PROMQL `verification_exception` → translation-mode / type gate

## Workflow E — bulk UI walks (`agent-browser`, repo)

When you need every `/_query`, pixel diffs, or Suspense walks across many panels, use `agent-browser` + `parity-rig/verifier/bootstrap.sh` (**repo-only** SAML bootstrap). See [agent-browser companion](~/.cursor/skills/chrome-devtools-debugging/agent-browser.md) and `docs/testing.md`.

Watch for wrong-tab / Gemini "glic" targets: `agent-browser tab list` and select the Kibana dashboard tab before URL checks.

Render-audit driver (importable from the package): `python -m observability_migration.targets.kibana.render_audit_driver` (browser session still required). Local no-SSO helper: `scripts/run_render_audit_local.sh` (repo).

## After diagnosis (real translator bug)

1. Red→green unit test in the repo (`tests/…`).
2. Smallest translator/profile fix; `make test` / targeted pytest.
3. Re-migrate the single dashboard (`try-one-source-dashboard` / `migrate --dashboard-ids` / files) and `obs-migrate upload` (or `migrate --upload`). Prefer package CLIs over `parity-rig/upload-all.sh` for operators.
4. Re-check with `grafana-validate-uploaded` and/or the failing panel's `/_query` replay.

## Things not to do

- Don't invent `OBS_MIGRATE_CREDS` — export `KEY` / `ELASTICSEARCH_ENDPOINT` / `KIBANA_ENDPOINT` (or `source` a local `serverless_creds.env`).
- Don't claim `obs-migrate verify-panels` works from bare `uvx` without `parity-rig` on `PYTHONPATH`.
- Don't blame the translator without reproducing via `/_query` or `grafana-validate-uploaded`.
- Don't `evaluate_script` in ways that read credentials/secrets from the Kibana session.
- Don't paste full network response bodies into chat — save to files and summarize.
- Don't treat empty panels as translator bugs when `/_query` returns 0 rows for missing telemetry.

## See also

- `install-obs-migrate` — install/doctor when the CLI is missing or not Ready.
- [`~/.cursor/skills/chrome-devtools-debugging/SKILL.md`](~/.cursor/skills/chrome-devtools-debugging/SKILL.md) — Chrome DevTools MCP foundation.
- [`~/.cursor/skills/chrome-devtools-debugging/agent-browser.md`](~/.cursor/skills/chrome-devtools-debugging/agent-browser.md) — bulk HAR / diff / Suspense primitives.
- `explain-migration-gaps` — warned/blocked status triage.
- `remediate-field-mapping-gaps` — field/profile/index fixes.
- `validate-side-by-side` — numeric parity (`obs-migrate compare`).
- `prepare-target-telemetry` / `seed-sample-data` — data readiness.
- `docs/testing.md` — render-audit taxonomy and layered gates.
- `docs/command-contract.md` — `upload`, `grafana-validate-uploaded`, `verify`.
