---
name: validate-side-by-side
description: Prove a migrated Kibana dashboard matches the original Grafana/Datadog source panel-by-panel by running obs-migrate compare, which numerically checks each PromQL panel's emitted ES|QL against Elasticsearch's native PROMQL oracle and degrades to a structural check otherwise. Use when the user asks to "validate the migration", "compare side by side", "did my panels translate correctly", "verify the numbers match", or "check parity". Writes nothing to the source; runs read-only queries against the target cluster. For an overall coverage summary use report-migration-coverage; to understand panels that failed use explain-migration-gaps.
---

# Validate side by side

Goal: run `obs-migrate compare` to check per-panel parity against the source — numerically where the native PROMQL oracle applies, and as a structural-only row (never hidden) for Datadog, non-PromQL, or no-oracle panels. The command issues read-only `_query` requests against the **target** Elasticsearch cluster; it writes nothing to Grafana or Datadog and does not re-run migration.

## Command

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout.

```bash
obs-migrate compare \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"
```

Repeat `--artifact-dir` to merge multiple migrate runs. Add `--ca-cert` / `--insecure` for TLS, and tune `--index`, `--step-seconds`, `--window-minutes`, or `--report-out` when the default oracle window does not match the dashboard. Full flag list and defaults: `docs/command-contract.md`.

## What "verified" means (honest table)

| Source / cluster | Mode | Verdicts | What it proves |
|---|---|---|---|
| PromQL / Grafana on a cluster with native PROMQL | Numeric | `STRICT_PASS` (≤1% max relative error), `FUZZY_PASS` (≤5%), `SHAPE_PASS`, `FAIL`, `SKIP`, `ERROR` | Translated ES|QL buckets match Elasticsearch's native `PROMQL(<source query>)` oracle over the same index and time window |
| Datadog panels, non-PromQL panels, or clusters without native PROMQL | Structural | `STRUCTURAL` | Semantic gate only — **not numerically verified**; the command checked shape/metadata, not bucket-by-bucket numbers |

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
  --api-key "$KEY"

obs-migrate remove-sample-data \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --confirm
```

`seed-sample-data` and `remove-sample-data` are **ES-only** (they touch the target cluster, not the source). `remove-sample-data` is **dry-run by default** — pass `--confirm` to actually delete seeder-owned streams.

## Reading the result

The command writes **`comparison_report.json`** (machine-readable) and a sibling **`comparison_report.md`** with a panel-by-panel table: dashboard, panel, mode, verdict, max relative error, reason.

**Exit codes:**

- **`2`** — Elasticsearch unreachable or invalid input (missing credentials, bad/missing `verification_packets.json`).
- **`1`** — at least one panel parity check returned `FAIL`.
- **`0`** — otherwise (including runs where every row is `STRUCTURAL` or non-`FAIL` numeric verdicts).

Besides **`FAIL`** (which sets exit `1`), verdicts **`ERROR`**, **`SKIP`**, and **`SHAPE_PASS`** do not fail the run but still warrant a look — route them to **`explain-migration-gaps`** or re-check `--window-minutes` / `--step-seconds` before trusting an all-green exit code.

Route panels with verdict **`FAIL`** or structural rows the user expected to be numerically verified to the **`explain-migration-gaps`** skill for rebuild guidance. For a shareable headline scorecard (not per-panel parity), use **`report-migration-coverage`**.

## Honest limits / Do NOT

- **Exit `0` with all-`STRUCTURAL` rows is NOT numeric proof** — you only confirmed structural compatibility, not that numbers match.
- **A `FAIL` may be a data-window or step mismatch**, not a translation bug — re-run with `--window-minutes` and `--step-seconds` aligned to the dashboard (and consider `seed-sample-data`) before declaring a translation defect.
- **Do not claim Datadog or non-PromQL panels were numerically verified** — they degrade to `STRUCTURAL` by design.
- **Do not write to the source** — compare is read-only on the target cluster; it does not prove the uploaded Kibana dashboard renders in the UI (empty panels may still be missing telemetry).

## See also

- `report-migration-coverage` skill — shareable coverage summary from migrate artifacts.
- `explain-migration-gaps` skill — why a panel did not migrate cleanly and how to rebuild it.
- `obs-migrate seed-sample-data` / `obs-migrate remove-sample-data` — optional deterministic data setup and teardown (`docs/command-contract.md`).
- `docs/command-contract.md` — full compare, seed, and remove flag reference for the installed version.
