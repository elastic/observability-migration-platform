---
name: validate-side-by-side
description: Use when the user asks to "validate the migration", "compare side by side", "did my panels translate correctly", "verify the numbers match", or "check parity" — checks whether a migrated Kibana dashboard matches the original Grafana/Datadog source panel-by-panel, numerically where Elasticsearch's native PROMQL oracle applies and structural-only otherwise. Runs read-only queries against the target cluster and writes nothing to the source. For an overall coverage summary use report-migration-coverage; to understand panels that failed use explain-migration-gaps.
---

# Validate side by side

Goal: run `obs-migrate compare` to check per-panel parity against the source — numerically where the native PROMQL oracle applies, and as a structural-only row (never hidden) for Datadog, non-PromQL, or no-oracle panels. The command issues read-only `_query` requests against the **target** Elasticsearch cluster; it writes nothing to Grafana or Datadog and does not re-run migration.

## Which command form to use (package vs. repo)

Install from PyPI
([`elastic-observability-migration`](https://pypi.org/project/elastic-observability-migration/)).
Prefer **`obs-migrate`** via `uvx --from 'elastic-observability-migration[all]' …`
or a persistent `pip install`. Prefix `.venv/bin/` only for a repo checkout.

## Command

```bash
obs-migrate compare \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --report-out <output-dir>/dashboards/comparison_report.json
```

`--es-url` / `--api-key` default to `ELASTICSEARCH_ENDPOINT|ES_URL` and `KEY`. Repeat `--artifact-dir` to merge multiple migrate runs. Add `--ca-cert` / `--insecure` for TLS, and tune `--index`, `--step-seconds`, `--window-minutes`, or `--report-out` when the default oracle window does not match the dashboard. Full flag list and defaults: `docs/command-contract.md`.

Optional package-native scorecard that also runs emitted-query acceptance (and optionally compare in-process):

```bash
obs-migrate verify \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --compare \
  --report-out <output-dir>/dashboards/verify_report.json
```

## What "verified" means (honest table)

| Source / cluster | Mode | Verdicts | What it proves |
|---|---|---|---|
| PromQL / Grafana on a cluster with native PROMQL | Numeric (`mode=native_oracle`) | `STRICT_PASS` (≤1% max relative error), `FUZZY_PASS` (≤5%), `SHAPE_PASS`, `FAIL`, `SKIP`, `ERROR` | Translated ES|QL buckets match Elasticsearch's native `PROMQL(<source query>)` oracle over the same index and time window. Multi-target panels verify one row per target (`target` = refId); mirrorable stat reductions (window `MAX` / latest-bucket `LAST`) compare as scalars |
| Panels whose packets carry live source-vs-target verdicts (`obs-migrate migrate --source datadog --source-execution --validate`) | Live source (`mode=live_source`) | `SOURCE_PASS`, `SOURCE_DRIFT`, `SOURCE_FAIL` (fails the run), `ERROR` (target broken) | The source API's own numbers vs the target ES|QL over the same window — only meaningful when both ingest the same telemetry. Without matching data you commonly see `SOURCE_DRIFT` (does **not** alone fail the run) |
| Datadog panels without live comparison, non-PromQL panels, or clusters without native PROMQL | Structural (`mode=structural`) | `STRUCTURAL` | Semantic gate only — **not numerically verified**; the command checked shape/metadata, not bucket-by-bucket numbers |

Never describe a `STRUCTURAL` row as numeric proof. Never hide the structural fallback behind exit code `0`.

## Deterministic flow (optional)

When live telemetry is sparse or mismatched, seed synthetic data both sides can read, compare, then clean up:

```bash
obs-migrate seed-sample-data \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

obs-migrate compare \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --report-out <output-dir>/dashboards/comparison_report.json

obs-migrate remove-sample-data \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --confirm
```

`seed-sample-data` and `remove-sample-data` are **ES-only** (they touch the target cluster, not the source). `remove-sample-data` is **dry-run by default** — pass `--confirm` to actually delete seeder-owned streams. Auth flag is **`--api-key`** (not `--es-api-key`).

## Reading the result

The command writes **`comparison_report.json`** (machine-readable) and a sibling **`comparison_report.md`** with a panel-by-panel table: dashboard, panel, mode, verdict, max relative error, native/translated/common series counts, reason. Numeric JSON rows also carry `native_series`/`translated_series`/`common_series` and `notes`, and every FAIL or SKIP has a populated reason. **`--report-out` defaults to `comparison_report.json` in the current working directory** — pass an explicit path under the artifact dir to keep the report beside the migration artifacts.

**Exit codes:**

- **`2`** — Elasticsearch unreachable or invalid input (missing credentials, bad/missing `verification_packets.json`).
- **`1`** — at least one panel parity check returned `FAIL` (or a live source comparison returned `SOURCE_FAIL`).
- **`0`** — otherwise (including runs where every row is `STRUCTURAL`, `SOURCE_DRIFT`, `SKIP`, or non-`FAIL` numeric verdicts).

Besides **`FAIL`** / **`SOURCE_FAIL`** (which set exit `1`), verdicts **`ERROR`**, **`SKIP`**, **`SHAPE_PASS`**, and **`SOURCE_DRIFT`** do not fail the run but still warrant a look — route them to **`explain-migration-gaps`** or re-check `--window-minutes` / `--step-seconds` / target telemetry before trusting an all-green exit code.

Route panels with verdict **`FAIL`** / **`SOURCE_FAIL`** or structural rows the user expected to be numerically verified to the **`explain-migration-gaps`** skill for rebuild guidance. Note that **`STRUCTURAL`** can also hide panels that migrated with **accepted approximations** (`migrated_with_warnings` / Datadog `warning`) — structural shape ≠ semantic fidelity; use `explain-migration-gaps` when the user expected numeric proof. For a shareable headline scorecard (not per-panel parity), use **`report-migration-coverage`**.

### Beyond compare (see also)

`obs-migrate compare` proves oracle / live-source parity where applicable; it does **not** prove Lens UI render. When the user asks "will it show up correctly in Kibana?", also point to:

- `obs-migrate verify` (emitted-query acceptance + optional `--compare` scorecard)
- Render audit / `verifier.dashboards_api` / `verifier.live_validate` — deeper gates; `obs-migrate verify` lists the ones it does **not** run (`docs/testing.md`, often via `parity-rig/` in a repo checkout)
- `debug-uploaded-kibana-dashboard` for a single broken panel

## Honest limits / Do NOT

- **Exit `0` with all-`STRUCTURAL` rows is NOT numeric proof** — you only confirmed structural compatibility, not that numbers match. Datadog without `--source-execution` typically lands here.
- **A `FAIL` / `SOURCE_DRIFT` may be a data-window or step mismatch**, not a translation bug — re-run with `--window-minutes` and `--step-seconds` aligned to the dashboard (and consider `seed-sample-data`) before declaring a translation defect.
- **Do not claim Datadog panels were numerically verified via the PROMQL oracle** — without live source packets they degrade to `STRUCTURAL`; with `--source-execution --validate` they use `SOURCE_*` verdicts instead.
- **Do not write to the source** — compare is read-only on the target cluster; it does not prove the uploaded Kibana dashboard renders in the UI (empty panels may still be missing telemetry).

## See also

- `report-migration-coverage` skill — shareable coverage summary from migrate artifacts.
- `explain-migration-gaps` skill — why a panel did not migrate cleanly and how to rebuild it.
- `debug-uploaded-kibana-dashboard` skill — UI render failures after upload.
- `prepare-production-cutover` skill — go/no-go using compare + render-audit + coverage.
- `obs-migrate seed-sample-data` / `obs-migrate remove-sample-data` — optional deterministic data setup and teardown (`docs/command-contract.md`).
- For Datadog, `obs-migrate migrate --source datadog --source-execution --validate` fills the verification packets' `source_execution`/`comparison` blocks so a later `obs-migrate compare` can emit `SOURCE_*` verdicts (needs DD creds and comparable telemetry on both sides).
- `docs/command-contract.md` — full compare, seed, and remove flag reference for the installed version.
- `docs/testing.md` — layered verifier and render-audit gates.
