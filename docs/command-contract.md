# Command Contract

This is the canonical command inventory for the repo.

Use this file as the source of truth for:
- supported commands
- required environment variables
- safe example invocations

## Environment Baseline

| Variable | Required for | Notes |
|---|---|---|
| `ELASTICSEARCH_ENDPOINT` or `ES_URL` | live validate, upload smoke, data scripts | Elasticsearch URL |
| `KIBANA_ENDPOINT` or `KIBANA_URL` | upload, cluster commands, smoke | Kibana URL |
| `KEY` or `ES_API_KEY` | authenticated ES/Kibana operations | API key |
| `DD_API_KEY` / `DD_APP_KEY` | Datadog API extraction / verification | can also load via `--env-file` |

Preferred variable names in this repo are `ELASTICSEARCH_ENDPOINT`,
`KIBANA_ENDPOINT`, and `KEY`.

Compatibility aliases such as `ES_URL`, `KIBANA_URL`, and `ES_API_KEY` remain
documented where a command or script still accepts them.

Example env files are available at the repo root: `serverless_creds.env.example`, `datadog_creds.env.example`, and `grafana_creds.env.example`.

Before sourcing a repo-local env file from the examples below, copy the matching
example file first and fill in its values. For example:

```bash
cp serverless_creds.env.example serverless_creds.env
```

## Install And Setup

Use Python 3.11 or newer. If `python3` resolves to an older interpreter on your
machine, create `.venv` with an explicit 3.11+ executable instead. From a repo
checkout or release source archive, install the end-user extras before running
the CLI:

```bash
python3 -m venv .venv
.venv/bin/pip install ".[all]"
.venv/bin/obs-migrate doctor
```

For contributor workflows, prefer `make sync` (the locked `uv` environment used
by CI). If you are using a plain virtualenv directly, install the dev extra and
enable local git hooks:

```bash
.venv/bin/pip install -e ".[all,dev]"
.venv/bin/pre-commit install
.venv/bin/pre-commit run --all-files
```

Commands that invoke `kb-dashboard-cli` or `kb-dashboard-lint` (including
`obs-migrate compile` and `obs-migrate upload`) resolve the tool
**installed-first**: install the Kibana tools in-venv with
`.venv/bin/pip install ".[kibana]"` (requires Python 3.12+), otherwise the
runtime falls back to a pinned `uvx`, which requires `uv` on `PATH`. Run
`obs-migrate doctor` to see which path is active.

Datadog live API extraction (`--input-mode api` on either the unified or
dedicated CLI; legacy dedicated spelling `--source api` also works) requires
the optional Datadog client extra:

```bash
.venv/bin/pip install -e ".[datadog]"
```

## Before Elastic / Kibana

You can use the migration tooling productively before configuring a target cluster.

- Translate exported dashboards into YAML.
- List bundled sample dashboards with `obs-migrate list-samples` (offline, no
  credentials), then migrate one with
  `obs-migrate migrate --source <source> --input-mode files --input-dir <input_dir>`.
- Pull live dashboards from Grafana or Datadog APIs.
- Pull Grafana alert artifacts or Datadog monitor artifacts.
- Read `migration_summary.md` for a human-readable verdict, scorecard, and
  per-dashboard worklist, then drill into `migration_report.json`,
  `migration_manifest.json`, `verification_packets.json`, and `rollout_plan.json`.
- Compile generated YAML to NDJSON locally.

Add `--es-url` when you want live target field discovery or emitted-query validation. Add `--kibana-url` when you want upload, target dashboard listing/deletion, smoke validation, or alert-rule payload checks against a real Kibana target.

## Asset Scope Contract

Every migration command that moves source assets into target artifacts accepts
`--assets {dashboards,alerts,all}`.

- `--assets dashboards`: migrate dashboards only
- `--assets alerts`: migrate alerts only
- `--assets all`: run both isolated pipelines in one command

Rules:
- `dashboards` never writes alert artifacts
- `alerts` never writes dashboard YAML or compiled output
- `all` is the union of both isolated pipelines

Dashboard artifacts are written under `<output-dir>/dashboards`. Alert artifacts
are written under `<output-dir>/alerts`. Grafana and Datadog both write a root
`run_summary.json` that records which asset families ran.

Every dashboard run also writes `<output-dir>/dashboards/migration_summary.md`: a
human-readable Markdown summary (verdict, scorecard, per-dashboard table, must-fix
worklist, grouped warnings, and non-panel gaps) rendered identically for both
Grafana and Datadog. It is best-effort — if the summary cannot be rendered the
migration still completes and the JSON artifacts are unaffected.

### Audited Asset Flag Matrix

Use explicit `--assets` in new scripts. Legacy fetch flags remain only as
compatibility aliases.

| Command | Flag | Applies To | Meaning | Notes |
|---|---|---|---|---|
| `obs-migrate migrate` | `--assets` | Grafana, Datadog | Select `dashboards`, `alerts`, or `all` | Canonical asset selector |
| `obs-migrate migrate` | `--fetch-alerts` | Grafana, Datadog | Deprecated alias for alert-capable runs | Using the alias always emits a deprecation warning; if the requested asset selection is `dashboards`, including explicit `--assets dashboards`, runtime normalization upgrades the run to `--assets all` |
| `grafana-migrate` | `--assets` | Grafana | Same as unified | Dedicated CLI parity |
| `grafana-migrate` | `--fetch-alerts` | Grafana | Deprecated alias | Using the alias always emits a deprecation warning; if the requested asset selection is `dashboards`, including explicit `--assets dashboards`, runtime normalization upgrades the run to `--assets all` |
| `datadog-migrate` | `--assets` | Datadog | Same as unified | Dedicated CLI parity |
| `datadog-migrate` | `--fetch-monitors` | Datadog | Deprecated alias | Using the alias always emits a deprecation warning; if the requested asset selection is `dashboards`, including explicit `--assets dashboards`, runtime normalization upgrades the run to `--assets all` |

## Unified CLI (`obs-migrate`)

### Migrate

`obs-migrate migrate` is the canonical unified migration surface for Grafana and
Datadog.

| Flag | Applies To | Meaning | Notes |
|---|---|---|---|
| `--input-mode {files,api}` | Grafana, Datadog | Choose file imports or live extraction | Use with `--source` |
| `--assets {dashboards,alerts,all}` | Grafana, Datadog | Run dashboard migration, alert migration, or both | Preferred explicit selector |
| `--field-profile` | Grafana, Datadog | Target field mapping profile | Defaults to `otel` for every source. Grafana currently supports `otel` only; Datadog also supports source-specific built-ins and YAML profile files. ECS fallback is not implemented in this pass. |
| `--data-view` | Grafana, Datadog | The Kibana **data view / index pattern the migrated panels bind to in the UI** | When omitted, the source adapter keeps its own default (Grafana: `metrics-*`). For Datadog, non-OTel profiles keep their profile index (for example `prometheus` keeps `metrics-prometheus-*`). See [Target index flags](#target-index-flags-data-view-vs-esql-index). |
| `--esql-index` | Grafana | The index / data stream **written into generated ES\|QL (fallback) queries and inspected during target schema discovery** | Defaults to `--data-view` when unset. Override it (with `--es-url`) when the ES\|QL/fallback queries should read from, and fields should be discovered from, a specific data stream — required for Prometheus fidelity. It does **not** retarget native-PROMQL panels, which read from `--data-view` in the default `auto` mode (use `--translation-mode esql` to route every panel through ES\|QL). Grafana-only today; Datadog controls its metric query target through `--data-view` / the active `--field-profile` instead. See [Target index flags](#target-index-flags-data-view-vs-esql-index). |
| `--logs-index` | Grafana, Datadog | The index / data stream written into translated Loki / LogQL (log) panels | Defaults to the source/profile log index (`logs-*`) when unset, not `--data-view`; the log analog of `--esql-index`. |
| `--translation-mode {auto,native,esql}` | Grafana (Datadog accepts as no-op) | Override Grafana's native-PROMQL/ES\|QL selection | Defaults to `auto`; use `native` or `esql` only for explicit operator control |
| `--fetch-alerts` | Grafana, Datadog | Deprecated compatibility alias | See [Audited Asset Flag Matrix](#audited-asset-flag-matrix) |
| `--env-file` | Datadog | Load Datadog credentials for API extraction and verification | Unified Datadog-only forwarding surface |
| `--dashboard-ids` | Datadog dashboard pipeline | Scope Datadog dashboard extraction by comma-separated dashboard IDs | Only affects Datadog dashboard runs |
| `--monitor-ids`, `--monitor-query` | Datadog alert pipeline | Scope Datadog monitor extraction | Only affect Datadog alert runs |
| `--alert-uids` | Grafana alert pipeline | Comma-separated Grafana unified alert rule UIDs to migrate | Skips all other unified rules; does not affect legacy panel-embedded alerts |
| `--alert-folder` | Grafana alert pipeline | Comma-separated Grafana folder UIDs; only unified rules from those folders are migrated | Combines with `--alert-uids` (AND logic) |
| `--select-folder`, `--select-tag`, `--select-datasource`, `--select-team`, `--select-updated-after`, `--select-updated-before`, `--select-starred` | Grafana, Datadog (dashboards and alerts) | Metadata-aware selection: filter assets by folder/tag/datasource/team/last-updated/starred | Repeatable or comma-separated; OR within a flag, AND across flags; case-insensitive exact match. Client-side filter applied after extraction. Dimensions a source/asset cannot supply **degrade gracefully** (asset kept + `WARN`), per the [selection availability matrix](#metadata-selection-availability). A `--select-*` set matching no dashboards exits non-zero. |
| `--grafana-url`, `--grafana-user`, `--grafana-pass`, `--grafana-token` | Grafana | Grafana API connection (basic auth or bearer token) | Flag-first with env fallback (`GRAFANA_URL` / `GRAFANA_USER` / `GRAFANA_PASS` / `GRAFANA_TOKEN`); forwarded to `grafana-migrate` |
| `--ca-cert <path>` | Grafana, Datadog | Verify TLS against a custom CA bundle for **all** outbound connections (source, Elasticsearch, Kibana, incl. the Node upload step) | Env fallback `OBS_MIGRATE_CA_CERT`; keeps verification on |
| `--insecure` | Grafana, Datadog | Disable TLS certificate verification for **all** outbound connections | Env fallback `OBS_MIGRATE_INSECURE`; testing/trusted-network only, prints a one-time warning. Prefer `--ca-cert` |
| `--smoke`, `--smoke-output`, `--browser-audit`, `--capture-screenshots` | Grafana, Datadog | Run shared post-upload validation | Forwarded to source runtimes when smoke is enabled; use `--smoke-output` to choose the report path |

Use `obs-migrate cluster ...` for shared target-management operations.

Dedicated source CLIs still expose `--list-dashboards`, `--delete-dashboards`,
and `--ensure-data-views` for source-local operator workflows, but unified
`obs-migrate migrate` no longer multiplexes those flags.

Integrated smoke validation is a post-upload evidence report, not a replacement
for reading the migration summary. `--smoke` writes and merges the smoke report
into the dashboard artifacts; pass `--smoke-output <path>` to choose that report
path. `--smoke-report <path>` is Grafana-only and only merges a pre-existing
smoke report; it cannot be combined with `--smoke`, and it is not forwarded to
Datadog. A run can still exit `0` while smoke reports empty panels or runtime
errors, so inspect `migration_report.json`, `migration_summary.md`, and the
smoke report before declaring the uploaded dashboard production-ready.

Examples below use the canonical environment names
(`$ELASTICSEARCH_ENDPOINT`, `$KIBANA_ENDPOINT`, `$KEY`) that match
`serverless_creds.env`. The compatibility aliases `$ES_URL`, `$KIBANA_URL`, and
`$ES_API_KEY` are still accepted by every CLI and refer to the same values.

```bash
# Grafana dashboards only (files); native PROMQL is the default
.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --assets dashboards \
  --field-profile otel \
  --data-view "metrics-*" \
  --esql-index "metrics-*"

# Datadog alerts only (API)
.venv/bin/obs-migrate migrate \
  --source datadog \
  --input-mode api \
  --env-file datadog_creds.env \
  --output-dir datadog_migration_output \
  --assets alerts \
  --field-profile otel \
  --data-view "metrics-*" \
  --monitor-ids 12345678

# Grafana dashboards + alerts from one run
KIBANA_URL= GRAFANA_URL=http://localhost:23000 GRAFANA_USER=admin GRAFANA_PASS=admin \
.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode api \
  --output-dir migration_output \
  --assets all \
  --field-profile otel \
  --data-view "metrics-*" \
  --esql-index "metrics-*"

# Grafana alerts only — selected rules by UID
GRAFANA_URL=http://localhost:23000 GRAFANA_USER=admin GRAFANA_PASS=admin \
.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode api \
  --output-dir migration_output \
  --assets alerts \
  --alert-uids "rule-uid-1,rule-uid-2"

# Grafana alerts only — all rules from a specific folder
GRAFANA_URL=http://localhost:23000 GRAFANA_USER=admin GRAFANA_PASS=admin \
.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode api \
  --output-dir migration_output \
  --assets alerts \
  --alert-folder "infra-folder-uid"
```

`obs-migrate migrate` compiles dashboard YAML to NDJSON during dashboard runs for
both Grafana and Datadog. Alerts-only runs do not emit dashboard YAML or
compiled output.

When a dashboard run discovers no input dashboards (for example
`--input-dir` points at an empty directory, or none of its files match the
expected source shape), `obs-migrate migrate` exits non-zero with a message
naming the directory and the expected JSON shape, rather than reporting
`0/0 dashboards compiled successfully`.

#### Metadata selection availability

The `--select-*` flags are uniform across both sources and both asset families,
but the underlying metadata is not uniformly available. When a selected
dimension cannot be supplied for a given source/asset, the engine **degrades
gracefully**: the asset is kept (not dropped) and a `WARN` names the skipped
dimension. Selection that genuinely matches nothing for a dashboard run exits
non-zero; for alerts it yields an empty alert set. Each run prints
`Selected N of M …` so the narrowing is auditable.

| Dimension | Grafana dashboards | Grafana alerts | Datadog dashboards | Datadog monitors |
|---|---|---|---|---|
| `--select-folder` | ✅ folder title | legacy ✅ (via dashboard) · unified ⚠️ (folderUID only) | ⚠️ (Dashboard Lists API not fetched) | ⚠️ |
| `--select-tag` | ✅ | ✅ (rule labels) | ✅ | ✅ |
| `--select-datasource` | ✅ panel datasource types | ✅ rule query datasources | ⚠️ (`datadog`) | ⚠️ |
| `--select-team` | ⚠️ (no first-class team) | ✅ (`team` label) | ✅ (`team:` tag) | ✅ (`team:` tag) |
| `--select-updated-after` / `--select-updated-before` | ✅ | ⚠️ (only if rule carries `updated`) | ✅ (`modified_at`) | ✅ (`modified`) |
| `--select-starred` | ✅ (`isStarred`) | ⚠️ | ⚠️ | ⚠️ |

✅ = filters; ⚠️ = degrades gracefully (asset kept + `WARN`).

### Field Profile Contract

`--field-profile` defaults to `otel` for every source migration. Grafana
currently accepts only `otel`; Datadog accepts `otel` plus its existing
Datadog-specific built-ins and YAML profile files. ECS fallback is planned
separately and is not part of this contract.

Datadog `--data-view` is an explicit override, not a hidden default. If omitted,
the active profile controls the metric index (`otel` uses `metrics-*`,
`prometheus` uses `metrics-prometheus-*`, and custom YAML profiles can set their
own `metric_index`).

### Target index flags: data-view vs esql-index

`--data-view` and `--esql-index` look interchangeable but control different
things, and getting `--esql-index` wrong is the most common reason a migrated
Prometheus dashboard renders empty.

| Flag | Default | What it controls |
|---|---|---|
| `--data-view` | `metrics-*` (Grafana) | The **Kibana data view** the migrated panels bind to in the UI. It is the saved-object reference Kibana resolves when it draws the panel. |
| `--esql-index` | falls back to `--data-view` when unset | The **index / data stream pattern written into generated ES\|QL (fallback) queries** (the `FROM` target those queries read) **and the target of schema discovery** — the index the tool inspects (with `--es-url`) to learn your field layout. It does **not** change the read target of native-PROMQL panels (see the note below). |

In code the Grafana schema resolver is constructed with
`index_pattern=args.esql_index or args.data_view`, so when you omit
`--esql-index` it inherits `--data-view` for both ES|QL query emission and field
discovery. Set it explicitly when your metrics live in a data stream whose name
differs from the data view you want the panels bound to. `--esql-index` is a
Grafana flag; Datadog has no separate ES|QL-index override and instead derives
its metric query target from `--data-view` / the active `--field-profile`.

**Native-PROMQL vs ES|QL read target.** Grafana's default `auto` translation
mode keeps PROMQL-capable panels native, and those panels build their
`PROMQL index=…` command from `--data-view` (`datasource_index`), **not**
`--esql-index`. `--esql-index` only sets the `FROM` target for panels that fall
back to ES|QL translation, plus the schema-discovery probe. So in `auto` mode,
pointing only `--esql-index` at a specific stream leaves native panels reading
from `--data-view` while ES|QL-fallback panels and discovery use `--esql-index`.

**Prometheus users:** point `--esql-index` (with `--es-url`) at your real
Prometheus data stream so fields resolve and the emitted ES|QL reads from the
right place. Leaving it at the default while your data lives elsewhere is the
difference between a working dashboard and an empty one. For example, to bind
panels to the broad `metrics-*` data view while ES|QL-fallback panels and schema
discovery read from the `metrics-alloy.prometheus-default` stream:

```bash
.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --assets dashboards \
  --field-profile otel \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY" \
  --data-view "metrics-*" \
  --esql-index "metrics-alloy.prometheus-default"
```

Because native-PROMQL panels still read from `--data-view` in `auto` mode, if
you need **every** generated panel to read the specific stream, either set
`--data-view` to that stream as well, or force ES|QL translation for all panels
with `--translation-mode esql`.

Without `--es-url`, schema discovery is skipped entirely, so `--esql-index`
only changes the ES|QL-fallback `FROM` target; the run falls back to OTel field
defaults and cannot warn you that the index does not match your data.

`--logs-index` is the log analog: it sets the index / data stream written into
translated Loki / LogQL panels. Unlike `--esql-index`, it does **not** fall back
to `--data-view` — when unset it defaults to the source/profile log index
(`logs-*`).

For Grafana native PromQL validation, this repo is exercised against
Prometheus-style layouts that Elasticsearch native PROMQL can query directly,
including the synthetic `metrics-prometheus-*` TSDB seed and the local OTel
lab's `metrics-*` data view. Grafana migration always emits native PROMQL with
automatic ES|QL fallback; when `--es-url` is set it probes the target and
downgrades to ES|QL translation only when the `PROMQL` command is confirmed
unsupported (an inconclusive probe keeps native and warns). Use
`--translation-mode {auto,native,esql}` only when you need to override that
probe-driven default: `auto` is the normal path, `native` requests native
PROMQL wherever the translator can safely emit it, and `esql` disables native
PROMQL so Grafana panels use ES|QL translation. Construct-level unsupported
cases can still degrade or require manual review. Datadog accepts the flag for
CLI parity, but it is a no-op because Datadog has no native-PROMQL path. If you
point `--data-view` at a different Prometheus integration layout, verify the
target schema first before treating empty panels as a migration bug.

For Datadog, `--source-execution` additionally executes each panel's source
query against the live Datadog API (requires `DD_API_KEY`/`DD_APP_KEY` via env
or `--env-file`) and, combined with `--validate`, fills the verification
packets' `source_execution`/`comparison` blocks with live source-vs-target
verdicts (`within_tolerance`/`drift`/`material_drift`); those verdicts can
override the semantic gate. Off by default: translation stays fully offline
and never calls the Datadog API. Numeric agreement is only meaningful when
the source and the target ingest the same telemetry.

Dashboard migrations also write `schema_change_report.md` and
`telemetry_contract.json` inside the per-source `dashboards/` artifact
directory. Live target readiness artifacts are source-specific: Grafana
preflight writes `required_target_contract.json` with `schema_profile`,
`field_capabilities_discovery`, and resolved target-field statuses; Datadog
dashboard runs write `target_readiness_contract.json` with the active
`field_profile`, metric/log index patterns, source fields, resolved target
fields, and statuses.

**Live extraction (`--input-mode api`)**

Grafana API mode accepts connection details **flag-first with env fallback**:
`--grafana-url` / `--grafana-user` / `--grafana-pass` (HTTP basic auth) or
`--grafana-token` (bearer), each defaulting to the matching environment variable
(`GRAFANA_URL`, `GRAFANA_USER`, `GRAFANA_PASS`, `GRAFANA_TOKEN`; defaults exist
for local labs). The flags exist on both `obs-migrate migrate --source grafana`
and the dedicated `grafana-migrate` CLI. For the full environment-driven setup
and entry points, see [Grafana source adapter](sources/grafana.md).

**TLS for custom-CA / self-signed clusters**

The migration, upload, cluster-management, alert-rule audit/delete, and
alert-rule verification commands accept two TLS knobs that apply to their outbound HTTPS connections —
source (Grafana/Prometheus/Loki), Elasticsearch, Kibana, Datadog, and the Node
`kb-dashboard-cli` compile/upload step where applicable (mapped to
`NODE_EXTRA_CA_CERTS` / `NODE_TLS_REJECT_UNAUTHORIZED`):

- `--ca-cert <path>` (env `OBS_MIGRATE_CA_CERT`): verify against a custom CA
  bundle/file; verification stays on. Preferred for private/internal CAs.
- `--insecure` (env `OBS_MIGRATE_INSECURE`): skip certificate verification
  entirely. Testing or trusted-network migration only; prints a one-time loud
  stderr warning. Prefer `--ca-cert` whenever possible.

On the dedicated CLIs these flags are honored across schema discovery, ES|QL
validation, source preflight/execution probes, dashboard upload, smoke
validation, and the alerting preflight/create/audit paths.

The repo-oriented `verify-panels` and `verify-visual` parity-rig wrappers do not
expose these TLS flags today; prefer the package-native migration/upload/smoke
paths for custom-CA or self-signed target validation.

Unified Datadog API mode exposes `--env-file`, `--dashboard-ids`,
`--monitor-ids`, and `--monitor-query`. Datadog API mode still requires the
optional `datadog-api-client` dependency:

```bash
.venv/bin/pip install -e ".[datadog]"
```

When unified Datadog API mode runs without a dashboard ID list, the extractor
uses the dashboard list returned by the Datadog API.

**Source-only / offline evaluation**

These runs intentionally omit target-aware flags such as `--es-url`,
`--validate`, `--upload`, and `--smoke`. If your shell already exports
Elastic/Kibana variables from another workflow, unset them first for a pure
source-only run.

Use `--assets alerts` for pure alert extraction and `--assets all` when you
want one command to produce both dashboard and alert artifacts.

#### Creating Kibana alert rules from a single command

By default, alert-capable runs selected through `--assets alerts` or
`--assets all` (or the deprecated legacy aliases) only extract, map, and
validate rule payloads; they do not create rules in Kibana. Pass
`--create-alert-rules` alongside an alert-capable asset selection together with
`--kibana-url` and `--kibana-api-key` to have `obs-migrate` create the emitted
rules immediately after the mapping step. Rules are created disabled by default
and tagged `obs-migration`.

- Grafana writes `<output-dir>/alerts/alert_rule_upload_results.json`
- Datadog writes `<output-dir>/alerts/monitor_rule_upload_results.json`

Use `obs-migrate audit-rules` (or the Kibana UI) to review the rules before
enabling them. `obs-migrate verify-alert-rules` is the self-cleaning round-trip
verifier (it creates rules with a temporary marker tag and cleans them up on
exit unless `--keep-rules` is passed). Both ship in the installed package; the
`scripts/audit_migrated_rules.py` and `scripts/verify_alert_rule_uploads.py`
files are the equivalent repo-checkout entry points.

```bash
# Unified: migrate dashboards + alerts + create rules (disabled).
set -a && source serverless_creds.env && set +a
.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --assets all \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY" \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY" \
  --upload \
  --create-alert-rules
```

#### Supported live source scope

- **Grafana (`input-mode api`)** — Pulls dashboard documents from the Grafana
  API. Links, annotations, transforms, and alert tasks are derived from that
  dashboard JSON during migration; they are not fetched as separate first-class
  API assets.
- **Datadog (`input-mode api`)** — Pulls dashboard objects from the Datadog
  API. Alert-capable runs can also pull monitors, emit/validate Kibana rule
  payloads, and optionally create rules with `--create-alert-rules`. Unified
  mode also accepts `--dashboard-ids` for explicit dashboard scoping.

### Compile / Upload

```bash
# Compile dashboard YAML to NDJSON locally.
.venv/bin/obs-migrate compile \
  --yaml-dir migration_output/dashboards/yaml \
  --output-dir migration_output/dashboards/compiled

# Upload dashboards to Kibana. The upload step recompiles YAML internally via
# kb-dashboard-cli and accepts either the YAML directory or the dashboard
# artifacts directory that contains a sibling yaml/ subfolder.
.venv/bin/obs-migrate upload \
  --yaml-dir migration_output/dashboards \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY"
```

`obs-migrate compile` is a local step and does not require Elasticsearch or Kibana. It can still exit nonzero after writing NDJSON if the YAML lint or compiled-layout checks return nonzero, so inspect both the exit status and the generated output directory.

`obs-migrate upload` takes a directory of YAML dashboards and recompiles them through the same installed-first `kb-dashboard-cli` resolution path used by `obs-migrate compile` (installed console script, otherwise pinned `uvx` fallback). It does **not** consume the NDJSON produced by `obs-migrate compile`. The legacy alias `--compiled-dir` is still accepted for backward compatibility but prefer `--yaml-dir` in new scripts. Pointing `--yaml-dir` at `migration_output/dashboards` (which contains a `yaml/` subdirectory) also works.

### Cluster

```bash
.venv/bin/obs-migrate cluster list-dashboards --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
.venv/bin/obs-migrate cluster ensure-data-views --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --data-view-patterns "metrics-*,logs-*"
.venv/bin/obs-migrate cluster delete-dashboards --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --dashboard-ids "id1,id2"
.venv/bin/obs-migrate cluster detect-serverless --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
```

`delete-dashboards` clears saved objects into `[DELETED]` placeholders by
overwriting each dashboard with empty content. This Serverless-safe behavior is
used for every target; use the Kibana UI if you need the placeholder saved
objects fully removed.

### Extensions

```bash
.venv/bin/obs-migrate extensions --source grafana --format yaml
.venv/bin/obs-migrate extensions --source datadog --format json
.venv/bin/obs-migrate extensions --source grafana --format yaml --template-out custom-rule-pack.yaml
.venv/bin/obs-migrate extensions --source datadog --format yaml --template-out custom-field-profile.yaml
```

### Schema Report

Dashboard migration writes `schema_change_report.md` and
`telemetry_contract.json` automatically. `obs-migrate schema-report` is the
advanced regeneration/combination command: it emits the same per-panel
source-to-target schema-change report
(`dashboard | panel | source_fields | target_stream | target_fields`) from one
or more existing dashboard artifact directories. It is the package-native form
of `scripts/generate_telemetry_contract.py` — it ships in the installed wheel
and needs no source checkout.

```bash
# Single source
.venv/bin/obs-migrate schema-report \
  --artifact-dir migration_output/dashboards \
  --output schema_change_report.md

# Merge multiple sources, and also emit the telemetry producer contract JSON
.venv/bin/obs-migrate schema-report \
  --artifact-dir grafana_output/dashboards \
  --artifact-dir datadog_output/dashboards \
  --output schema_change_report.md \
  --contract-out telemetry_contract.json
```

Each `--artifact-dir` is a per-source `dashboards/` output (containing `yaml/`
and `verification_packets.json`). `--contract-out` is optional; without it only
the Markdown report is written.

### Audit Rules

`obs-migrate audit-rules` lists migrated Kibana alerting rules (those tagged
`obs-migration` or named `[migrated] ...`) and reports which are enabled. It is
**read-only by default**; pass `--disable-enabled` to disable the enabled
subset. This is the package-native form of `scripts/audit_migrated_rules.py`.
Exit code is non-zero while enabled migrated rules remain (or remediation
fails).

```bash
.venv/bin/obs-migrate audit-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
.venv/bin/obs-migrate audit-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --disable-enabled
```

### Delete Rules

`obs-migrate delete-rules` reverts the alert-rule half of a migration by
deleting the rules it created (those tagged `obs-migration` or named
`[migrated] ...`). It is **dry-run by default** — it lists the rule IDs that
would be removed without touching them. Pass `--confirm` to actually delete.
Exit code is `2` when the cluster is unreachable or the rule listing is
truncated, `1` when any delete fails, and `0` otherwise. Unlike
`audit-rules --disable-enabled` (which only disables enabled rules), this
removes the rules entirely; unlike `verify-alert-rules` (which only cleans up
its own temporary verification rules), this targets the migrated rules already
in Kibana.

By default the command scans 20 pages of 100 rules (`--max-pages 20`,
`--per-page 100`) in the default Kibana space. Use `--space-id <space>` for
non-default spaces. In large spaces, if the listing hits the scan limit before
all rules are inspected, the command returns `rule_listing_truncated`, exits
`2`, and does **not** delete anything. Increase `--max-pages` and rerun the dry
run before passing `--confirm`.

```bash
# Dry run: show which migrated rules would be deleted.
.venv/bin/obs-migrate delete-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# Confirm: delete the migrated rules.
.venv/bin/obs-migrate delete-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --confirm --max-pages 50
```

### Verify Alert Rules

`obs-migrate verify-alert-rules` is the package-native form of
`scripts/verify_alert_rule_uploads.py`: a self-cleaning round trip that creates
the emitted alert-rule payloads in Kibana **disabled**, confirms none came back
enabled, then deletes them (unless `--keep-rules`). `--comparison` is required
and points at a comparison report written by a prior alert-capable migration
(for example `<output-dir>/alerts/alert_comparison_results.json` for Grafana, or
`<output-dir>/alerts/monitor_comparison_results.json` for Datadog). Repeat
`--comparison` to verify multiple reports.

```bash
.venv/bin/obs-migrate verify-alert-rules \
  --comparison alert_migration_output/alerts/alert_comparison_results.json \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY" \
  --limit 1
```

Exit code is `2` when the cluster is unreachable or no payloads are found, `1`
when any rule landed enabled / failed to create / failed to clean up, and `0`
on a clean round trip.

### Seed Sample Data

`obs-migrate seed-sample-data` builds a telemetry contract from one or more
migrated dashboard artifact directories and ingests synthetic documents into
Elasticsearch so the migrated panels light up. It is the package-native,
TLS-aware form of `scripts/setup_telemetry_data.py` (which is now a thin shim
over the same library) — it ships in the installed wheel and needs no source
checkout. It is **ES-only** (it does not touch Kibana); pair it with
`remove-sample-data` to clean up afterward. Exit code is `2` when Elasticsearch
is unreachable or inputs are invalid, `1` on ingest errors, and `0` otherwise.

```bash
# Seed synthetic data for a single migrated artifact directory.
.venv/bin/obs-migrate seed-sample-data \
  --artifact-dir migration_output/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

# Merge multiple sources and cap cardinality; honors --ca-cert / --insecure.
.venv/bin/obs-migrate seed-sample-data \
  --artifact-dir grafana_output/dashboards \
  --artifact-dir datadog_output/dashboards \
  --data-hours 6 --interval-sec 30 --max-combinations 8 \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"
```

`--es-url`/`--api-key` fall back to `ELASTICSEARCH_ENDPOINT`/`KEY`. Use
`--purge-foreign-streams` to drop non-seeder streams overlapping the contract
wildcards before seeding, `--no-recreate` to ingest without recreating
templates/streams, and `--rules-file`/`--prometheus-url` to supply authoritative
metric kinds.

### Compare (side-by-side parity)

`obs-migrate compare` reads `verification_packets.json` from one or more migrated
dashboard artifact directories and, per panel, checks that the emitted ES|QL
matches the source query on the target cluster.

For **PromQL / Grafana panels** on a cluster with native PROMQL support, the
command runs the panel's translated ES|QL and Elasticsearch's native
`PROMQL(<source query>)` command over the **same** index pattern and time window,
then diffs per bucket. Verdicts are `STRICT_PASS` (≤1% relative error),
`FUZZY_PASS` (≤5%), `SHAPE_PASS`, `FAIL`, `SKIP`, or `ERROR`.

For **Datadog panels**, non-PromQL panels, or clusters without native PROMQL,
the command degrades to a `STRUCTURAL` row (semantic gate only) — clearly labeled
**not numerically verified**.

```bash
# Compare migrated panels against the source on the target cluster.
.venv/bin/obs-migrate compare \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

# Repeat --artifact-dir to merge multiple runs; honors --ca-cert / --insecure.
.venv/bin/obs-migrate compare \
  --artifact-dir grafana_output/dashboards \
  --artifact-dir datadog_output/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --index "metrics-*" \
  --step-seconds 300 \
  --window-minutes 60 \
  --report-out comparison_report.json
```

`--artifact-dir` is required and repeatable (each directory must contain
`verification_packets.json`). `--es-url`/`--api-key` default to
`ELASTICSEARCH_ENDPOINT`/`ES_URL` and `KEY`. `--index` overrides the native
PROMQL oracle index pattern (default: inferred per panel from the translated
ES|QL). `--step-seconds` sets the oracle bucket step (default `300`).
`--window-minutes` sets the look-back window (default `60`). `--report-out`
names the JSON report (default `comparison_report.json`); a sibling
`comparison_report.md` is written with a panel-by-panel table (dashboard, panel,
mode, verdict, max relative error, native/translated/common series counts,
reason). Numeric rows in the JSON report also carry `native_series`,
`translated_series`, `common_series`, and `notes`, and every `FAIL` or `SKIP`
verdict has a populated `reason` (e.g. "series keys did not align",
"no data on either side in the compare window", "multi-query panel ... merged
into one ES|QL"). Multi-target panels with per-target provenance produce one
row per target (`target` carries the refId); stat panels whose terminal
reduction is mirrorable (window `MAX`, latest-bucket `LAST`) are compared as
scalars instead of SKIPping. Packets that carry live source-vs-target verdicts
(from `migrate --source-execution --validate`) surface as `mode: live_source`
rows with verdicts `SOURCE_PASS` / `SOURCE_DRIFT` / `SOURCE_FAIL` (or `ERROR`
for `target_broken`) instead of `STRUCTURAL`.

Exit code is `2` when Elasticsearch is unreachable or inputs are invalid
(missing/malformed `verification_packets.json`, missing credentials), `1` when
any panel parity check returns `FAIL` or a live source comparison returns
`SOURCE_FAIL` (material drift), and `0` otherwise (structural-only rows never
fail the run).

**Deterministic trial:** seed synthetic data both sides can read, compare parity,
then clean up:

```bash
.venv/bin/obs-migrate seed-sample-data \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

.venv/bin/obs-migrate compare \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

.venv/bin/obs-migrate remove-sample-data \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --confirm
```

### Verify (one-command package-native scorecard)

`obs-migrate verify` is a thin orchestrator that runs the **package-native**
correctness gates over an already-migrated artifact dir and prints ONE
consolidated scorecard. It exists so users don't have to assemble the
individual gates by hand. It is read-only on the cluster.

```bash
.venv/bin/obs-migrate verify \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --report-out verify_report.json

# Add numeric parity (runs obs-migrate compare in-process over the same dir):
.venv/bin/obs-migrate verify \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" --api-key "$KEY" \
  --compare
```

What it runs:

1. **Emitted-query acceptance gate** — reads each panel's emitted ES|QL from
   `verification_packets.json` (`packets[].translated_query`) and/or
   `migration_report.json` (`dashboards[].panels[].esql_query`), dedupes
   identical queries, runs each against Elasticsearch via the package-native
   `esql_validate.validate_esql`, and classifies the result as `ok` /
   `real_bug` (a genuine parse/type/argument/function error in the emitted
   ES|QL) / `data_gap` (well-formed query, telemetry absent — unknown
   column/index) / `other`. Data-gap signals win over real-bug signals when
   both appear.
2. **Numeric parity gate (opt-in, `--compare`)** — invokes the existing
   `obs-migrate compare` implementation in-process over the same artifact dir
   and surfaces its STRICT/FUZZY/SHAPE/STRUCTURAL/FAIL/ERROR counts. If compare
   can't run (no data / unreachable), the scorecard says so rather than failing.

`--artifact-dir` is required (a single migrated dashboard artifact dir).
`--es-url`/`--api-key` default to `ELASTICSEARCH_ENDPOINT`/`ES_URL` and `KEY`.
`--index` (default `metrics-*`) is the pattern used to validate queries and to
seed the compare native-PROMQL oracle. `--report-out` (default
`verify_report.json`) writes the consolidated JSON report. `--kibana-url` is
accepted for parity with sibling commands but is not required by the
package-native gates. Honors `--ca-cert` / `--insecure`.

**Coverage honesty.** `verify` is intentionally NOT exhaustive. The scorecard
always lists the deeper gates it does NOT run, with the exact command for each
— these live in `parity-rig/` and are not importable from the installed
package:

- `verifier.dashboards_api` — Kibana typed UI-contract validation (accessor
  wiring, column refs).
- render audit (`parity-rig/render_audit_driver.py`) — the only gate that
  catches Lens accessor / "invalid column" / empty-state render failures that
  ES|QL execution and the schema gate miss.
- `obs-migrate verify-panels` — the full 5-tier panel verifier (delegates to
  `parity-rig/verifier`).

Exit code is `2` when Elasticsearch is unreachable or inputs are invalid
(missing artifact dir, missing credentials, no emitted queries), `1` on any
`real_bug` or compare `FAIL`/`ERROR`, and `0` otherwise (`data_gap`/`other`
are warnings, not failures).

### Verification And Benchmark Gates

The `parity-rig/verifier/` tools are repo-oriented correctness gates used by
development and CI. They are intentionally layered: each gate answers a different
question, and no single gate is sufficient for "the dashboard is correct".

| Tool | Input | What it proves | Typical gate |
|---|---|---|---|
| `verifier.live_validate` | `migration_report.json` | Elasticsearch accepts the emitted ES|QL (`real_bug` vs `data_gap`) | no `real_bug` |
| `verifier.dashboards_api` | `migration_report.json` + Kibana | Kibana's typed Dashboards API accepts the mapped panel payload | no `dashboards_api_rejected` |
| `obs-migrate compare` | `verification_packets.json` + seeded data | Native PromQL and translated ES|QL are numerically close | no `FAIL`/`ERROR`; bounded `SHAPE_PASS` |
| `verifier.corpus_gate` | `obs-migrate compare` report(s) | Frozen semantic corpus does not regress | configured budgets |
| `verifier.benchmark_gate` | PM `benchmark_history.json` | Migration success metrics do not drop vs compatible baseline | configured budgets |
| `verifier.scorecard` | `migration_report.json` + committed baseline | Layer-9 invariant ERROR counts do not regress vs baseline (fidelity ratchet) | no error-count increase |
| `render_audit_driver` | uploaded dashboard + headless browser | Each panel actually renders in real Kibana (no Lens "invalid column"/error embeddable) | no `render_error` |
| `verifier.mutations` | `migration_report.json` | The invariant verifier catches deliberate corruptions | all mutations pass |
| `verifier.lens_fixtures` | LensConfigBuilder fixture JSON | Authoritative Lens-as-code fixtures exist for required chart families | coverage complete |
| `verifier.corpus_manifest` | Grafana catalog + datasource map | Larger benchmark corpus is pinned/stratified/reproducible | committed manifest |

Offline coverage gates (no cluster, every PR) live in the unit suite, not
`verifier/`: `tests/core/coverage/test_supported_types.py` cross-checks the
supported-type registry (`observability_migration/core/coverage/supported_types.py`)
against the code's routing both ways; `tests/test_panel_matrix.py` (Grafana) and
`tests/test_datadog_panel_matrix.py` (Datadog) lint every panel/widget type
through the real pipeline; `tests/test_canary.py` validates the registry-driven
kitchen-sink canary against `docs/dashboards/schema.json`. The fidelity ratchet
runs as an e2e gate (`tests/e2e/test_fidelity_ratchet.py`) against committed
baselines (`parity-rig/benchmark/fidelity_baseline_{grafana,datadog}.json`).

Examples:

```bash
# Runtime ES|QL oracle: catches invalid emitted ES|QL that compile/lint miss.
PYTHONPATH=parity-rig .venv/bin/python -m verifier.live_validate \
  --migration-out migration_output/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --fail-on-bug

# Typed Kibana UI contract: validates mapped panels against /api/dashboards.
PYTHONPATH=parity-rig .venv/bin/python -m verifier.dashboards_api \
  --migration-out migration_output/dashboards \
  --kibana-url "$KIBANA_ENDPOINT" \
  --api-key "$KEY" \
  --fail-on-error

# Semantic corpus gate over compare reports.
PYTHONPATH=parity-rig .venv/bin/python -m verifier.corpus_gate \
  --report comparison_report.json \
  --max-fail 0 \
  --max-error 0 \
  --max-shape-pass 25

# PM benchmark-history gate. Compare the latest run to the most recent
# compatible different CLI hash (same G/D config and schema-discovery class).
PYTHONPATH=parity-rig .venv/bin/python -m verifier.benchmark_gate \
  --history benchmark_history.json \
  --max-drop-pp 0.5 \
  --max-count-drop 5 \
  --max-duration-increase-pct 100

# Same gate, but scoped like the PM UI's datasource filters.
PYTHONPATH=parity-rig .venv/bin/python -m verifier.benchmark_gate \
  --history benchmark_history.json \
  --source grafana \
  --grafana-datasource prometheus \
  --grafana-datasource-map grafana-datasources.json \
  --max-drop-pp 0.5 \
  --max-count-drop 5

# Build a bigger pinned corpus manifest without introducing marketplace noise.
PYTHONPATH=parity-rig .venv/bin/python -m verifier.corpus_manifest \
  --grafana-catalog dashboards.json \
  --grafana-datasource-map grafana-datasources.json \
  --top 500 \
  --long-tail tail_500_2000:500:2000:100 \
  --datasource-quota prometheus=100 \
  --datasource-quota loki=50 \
  --bug-seed 1860 \
  --output corpus.manifest.json
```

Fidelity ratchet and render audit:

```bash
# Fidelity ratchet: Layer-9 invariant ERROR counts must not regress vs the
# committed baseline. Refresh a baseline (only after an intentional change) with
# --update; CI runs it without --update via tests/e2e/test_fidelity_ratchet.py.
PYTHONPATH=parity-rig .venv/bin/python -m verifier.scorecard \
  --migration-out migration_output/dashboards \
  --baseline parity-rig/benchmark/fidelity_baseline_grafana.json

# Render audit: prove panels actually render in Kibana (catches Lens accessor /
# "invalid column" errors that live_validate and the schema gate cannot see).
# Serverless needs a one-time SSO login into a persistent Chrome profile
# (--user-data-dir); a local no-SSO Kibana needs no profile.
.venv/bin/python -m observability_migration.targets.kibana.render_audit_driver \
  --kibana-url "$KIBANA_ENDPOINT" --dashboard-id "<id>" \
  --user-data-dir /path/to/logged-in-chrome-profile --fail-on-error

# Focusing the right tab in a live agent-browser session (--agent-browser):
# bootstrap.sh logs in once and keeps a persistent profile
# (~/.agent-browser/profiles/mig-to-kbn-verifier) + saved state. A live session
# often has MULTIPLE tabs — Kibana tabs PLUS a Gemini "glic" side-panel
# (https://gemini.google.com/glic), staging.found.no, or an SSO interstitial
# (/internal/security/capture-url, auth_provider_hint). --agent-browser is a
# tab-selection helper: it enumerates `tab list --json` and activates the Kibana
# /app/* tab matching the host + dashboard id (ignoring the stray tabs) so the
# session isn't left on the wrong tab. DOM capture ALWAYS uses the headless
# dump_dom path (it reads HTML, so CSS-class render markers like embPanel__error
# are visible, and it navigates to the exact target URL), so you still pass a
# logged-in --user-data-dir profile. The pure selection rule is
# select_kibana_page_url() in render_audit_driver.py.
# Manual equivalent: `agent-browser tab list` then `agent-browser tab t<N>` for
# the Kibana tab whose URL matches the cluster host + dashboard id.
KIBANA_URL="$KIBANA_ENDPOINT" bash parity-rig/verifier/bootstrap.sh   # one-time SSO
.venv/bin/python -m observability_migration.targets.kibana.render_audit_driver \
  --kibana-url "$KIBANA_ENDPOINT" --dashboard-id "<id>" \
  --user-data-dir /path/to/logged-in-chrome-profile \
  --agent-browser --fail-on-error

# Full local automation (no SSO): spin up a security-disabled ES+Kibana, then
# migrate+upload the canary, seed, and render-audit it.
STACK_VERSION=9.5.0-SNAPSHOT docker compose -f parity-rig/docker-compose.render-audit.yml up -d --wait
bash scripts/run_render_audit_local.sh
docker compose -f parity-rig/docker-compose.render-audit.yml down -v
```

`benchmark_gate` exits non-zero when no comparison was made (empty history, no
compatible baseline, or a datasource/source filter matching no current/baseline
metrics). Use `--allow-skip` only when intentionally bootstrapping a new
history/baseline, and `--allow-filter-skip` only when an empty filtered slice is
expected.

Regression-gate guidance:

- Use `benchmark_gate` for the PM trend numbers (`dashboard_migration_pct`,
  `dashboard_clean_pct`, `panel_migration_pct`, `panel_clean_pct`,
  `panel_verified_pct`, and optional duration). It also checks denominator drops
  (`dashboards`, `panels_total`, `verification_total`) so stable percentages
  cannot hide a smaller corpus or reduced verification coverage.
- Keep PR gates smaller and deterministic. Use a pinned manifest from
  `corpus_manifest` plus bug seeds. Run the larger stratified corpus nightly or
  before risky translator changes.
- A `benchmark_gate` "no compatible baseline" result is not a pass on quality;
  it means the run changed config/schema class enough that the gate cannot make
  a fair comparison. By default this exits non-zero in CI; establish a new
  baseline before relying on trend decisions.

### Remove Sample Data

`obs-migrate remove-sample-data` tears down what `seed-sample-data` created. It
is **fail-closed**: it only deletes data streams it can positively prove were
created by the seeder (their backing index template is prefixed
`telemetry-data-`); foreign or unverifiable streams are skipped, never deleted.
It is **dry-run by default** — it prints the plan (`deleted_streams`,
`deleted_templates`, `skipped_not_owned`, `errors`) and deletes nothing; pass
`--confirm` to actually delete. Exit code is `2` when Elasticsearch is
unreachable or inputs are invalid, `1` when any delete fails, and `0` otherwise.

```bash
# Dry run: show which seeder-owned streams/templates would be removed.
.venv/bin/obs-migrate remove-sample-data \
  --artifact-dir migration_output/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

# Confirm: delete the seeder-owned streams and templates.
.venv/bin/obs-migrate remove-sample-data \
  --artifact-dir migration_output/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --confirm
```

## Dedicated Source CLIs

Dedicated entry points (`grafana-migrate`, `datadog-migrate`) are thin wrappers around `python -m observability_migration.adapters.source.grafana.cli` and `python -m observability_migration.adapters.source.datadog.cli`.
They accept the same `--input-mode {files,api}` extraction selector as unified
`obs-migrate migrate`. The older dedicated-CLI spelling `--source files|api`
is still accepted as a compatibility alias; if both are provided, they must
match. Both dedicated CLIs also accept the same `--select-*` metadata selection
flags described under [Migrate](#migrate) (with the same per-source availability
and graceful-degradation behavior).

### Grafana

Use the shared asset contract above for `--assets` and the deprecated
`--fetch-alerts` alias. For Grafana-specific runtime details, see [Grafana
source adapter](sources/grafana.md).

```bash
# Files: dashboards only (native PROMQL is the default)
.venv/bin/grafana-migrate \
  --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --assets dashboards \
  --field-profile otel \
  --data-view "metrics-*" \
  --esql-index "metrics-*"

# Live Grafana API: alerts only
KIBANA_URL= GRAFANA_URL=http://localhost:23000 GRAFANA_USER=admin GRAFANA_PASS=admin \
.venv/bin/grafana-migrate \
  --input-mode api \
  --output-dir migration_output \
  --assets alerts

# Files: dashboards + alerts + integrated smoke
.venv/bin/python -m observability_migration.adapters.source.grafana.cli \
  --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --assets all \
  --field-profile otel \
  --data-view "metrics-*" \
  --esql-index "metrics-*" \
  --es-api-key "$KEY" \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --smoke \
  --browser-audit \
  --capture-screenshots \
  --kibana-url "$KIBANA_ENDPOINT"
```

Without `--es-url`, Grafana skips schema discovery and emitted-query
validation. Dashboard-capable runs (`--assets dashboards` or `--assets all`)
still write dashboard YAML, compiled NDJSON, and the normal dashboard report
artifacts. Alerts-only runs (`--assets alerts`) skip dashboard emission and
write alert artifacts under `<output-dir>/alerts`. For pure source-side alert
extraction, set `KIBANA_URL=` in the shell to suppress the default local Kibana
alerting preflight.

### Datadog

Use the shared asset contract above for `--assets` and the deprecated
`--fetch-monitors` alias. For Datadog-specific runtime details, see [Datadog
source adapter](sources/datadog.md).

```bash
# Files: dashboards only
.venv/bin/datadog-migrate \
  --input-mode files \
  --input-dir infra/datadog/dashboards \
  --output-dir datadog_migration_output \
  --assets dashboards \
  --field-profile otel \
  --data-view "metrics-*"

# Live Datadog API with explicit dashboard scoping
.venv/bin/datadog-migrate \
  --input-mode api \
  --env-file datadog_creds.env \
  --dashboard-ids abc-def-123 \
  --output-dir datadog_migration_output \
  --assets dashboards \
  --data-view "metrics-*"

# Live Datadog API: alerts only
.venv/bin/datadog-migrate \
  --input-mode api \
  --env-file datadog_creds.env \
  --output-dir datadog_migration_output \
  --assets alerts \
  --field-profile otel \
  --data-view "metrics-*" \
  --monitor-ids 12345678
```

Same scope as [Supported live source scope](#supported-live-source-scope) under
unified migrate: Grafana dashboards via API (related artifacts from dashboard
JSON only); Datadog dashboards via API, with monitor extraction available
through alert-capable runs and rule payload emission/validation limited to
validated monitor shapes.

Without `--es-url`, Datadog stays in offline field-capabilities mode.
Dashboard-capable runs (`--assets dashboards` or `--assets all`) compile by
default and still write dashboard YAML plus the standard dashboard run reports;
pass `--no-compile` only when you explicitly want to skip local dashboard
compilation. Upload still compiles dashboard YAML during the upload step (it
recompiles via `kb-dashboard-cli` and does not consume the NDJSON written by
`obs-migrate compile`). Alerts-only runs
(`--assets alerts`) skip dashboard YAML and compiled output, write monitor
artifacts under `<output-dir>/alerts`, and still emit the root
`run_summary.json`. Use the dedicated Datadog CLI when you need explicit
dashboard scoping via `--dashboard-ids` before any Elastic target exists.

## Validation / Verification CLIs

```bash
# Scope validation to just the dashboards a migration uploaded (recommended).
# --dashboards-from reads a migration detailed report or a prior smoke report and
# validates only those dashboards — by uploaded saved-object ID when the artifact
# carries one, otherwise by title. This keeps runs practical on busy spaces (#198).
.venv/bin/grafana-validate-uploaded \
  --kibana-url "$KIBANA_ENDPOINT" \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --dashboards-from migration_output/migration_report.json \
  --output upload_smoke_report.json

.venv/bin/grafana-generate-corpus --help
```

Scope flags are also available for ad-hoc runs: `--dashboard-id` /
`--dashboard-title` (repeatable) restrict to specific dashboards. With no scope
flags the validator inspects **every** dashboard in the space, prints an
explicit `WARNING` that it is doing so, and skips `[DELETED]` placeholder
dashboards (pass `--include-deleted` to validate those too). The run prints
per-dashboard `[i/N]` progress before writing the final report.

## Tested Alert Upload Flow

This sequence was re-run against the Serverless target using the curated example corpus.
Create `serverless_creds.env` from `serverless_creds.env.example` before
running the commands below.

### Preferred: one unified command

```bash
set -a && source serverless_creds.env && set +a
.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir examples/alerting/grafana \
  --output-dir alert_migration_output \
  --assets all \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY" \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY" \
  --upload \
  --create-alert-rules

set -a && source serverless_creds.env && set +a
.venv/bin/obs-migrate audit-rules \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY"
```

`obs-migrate migrate --assets all --upload --create-alert-rules` uploads the
generated dashboards, extracts and validates the alert payloads, and creates
the emitted Kibana rules disabled by default in a single run. The alert rule
upload summary is written to
`alert_migration_output/alerts/alert_rule_upload_results.json` (or
`alert_migration_output/alerts/monitor_rule_upload_results.json` for
`--source datadog`).

### Legacy repo-checkout multi-step flow

This flow remains supported in a source checkout when you want to regenerate the curated example artifacts without touching dashboards, or when you want the destructive round-trip `verify_alert_rule_uploads.py` path:

```bash
.venv/bin/python scripts/generate_alert_support_report.py

set -a && source serverless_creds.env && set +a
.venv/bin/obs-migrate upload \
  --yaml-dir examples/alerting/generated/grafana/dashboards/yaml \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY"

set -a && source serverless_creds.env && set +a
.venv/bin/python scripts/verify_alert_rule_uploads.py \
  --kibana-url "$KIBANA_ENDPOINT" \
  --api-key "$KEY" \
  --keep-rules

set -a && source serverless_creds.env && set +a
.venv/bin/python scripts/audit_migrated_rules.py
```

This sequence regenerates the curated Grafana and Datadog alert comparison artifacts, uploads the generated `Legacy Alert Examples` dashboard, round-trips the emitted rules through Kibana, and then audits the migrated rules present in Kibana. `scripts/verify_alert_rule_uploads.py` deletes its verification rules unless `--keep-rules` is passed.

## Script Commands

### Local Lab Lifecycle

```bash
bash scripts/start_local_lab.sh
bash scripts/start_local_lab.sh --with-alloy --recreate
bash scripts/stop_local_lab.sh
bash scripts/stop_local_lab.sh --volumes
```

These commands assume the selected local lab project owns the configured local ports. If another repo-owned lab is already using them, set `LOCAL_LAB_PROJECT`, `LOCAL_GRAFANA_PORT`, `LOCAL_ES_PORT`, `LOCAL_KIBANA_PORT`, and any colliding OTLP / Alloy ports before starting a second stack.

### Local Validation Flows

```bash
bash scripts/full_local_demo.sh --sample-set bundled
bash scripts/full_local_demo.sh --sample-set bundled --recreate-lab
bash scripts/full_local_demo.sh
```

These wrappers write reports even when smoke validation or query validation finds issues, so inspect `migration_report.json` and `upload_smoke_report.json` instead of treating exit `0` as “all panels are perfect.”

### Datadog Demo Flows

Default mode uses the curated four-dashboard smoke subset. Browser extras are opt-in.

```bash
bash scripts/run_datadog_demo.sh
bash scripts/run_datadog_demo.sh --browser-audit --capture-screenshots
bash scripts/run_datadog_demo.sh --target serverless
```

For local-target Datadog demos, keep a single local lab stack active on the selected ports. If you just recreated the lab, wait for the chosen Elasticsearch container to report Docker health `healthy` before rerunning the wrapper.

### Migration Helpers

```bash
bash scripts/run_migration.sh
bash scripts/run_migration.sh --skip-data
bash scripts/run_migration.sh --skip-upload
```

### Schema / Lint / Layout

```bash
bash scripts/generate_dashboard_schema.sh
```

Dashboard YAML lint and compiled-layout validation run automatically inside
`obs-migrate compile`/`migrate`. To run them ad hoc, call the in-process modules:

```python
from observability_migration.targets.kibana.lint import lint_dashboard_yaml
ok, output = lint_dashboard_yaml("migration_output/dashboards/yaml")

from observability_migration.targets.kibana.layout import validate_compiled_layout
ok, output = validate_compiled_layout("migration_output/dashboards/compiled")
```

The lint gate calls `kb-dashboard-lint`, resolved installed-first via the
`obs-migrate[kibana]` extra (Python 3.12+) with a pinned `uvx` fallback on 3.11.

### Data Setup

For new use, prefer the package-native
[`obs-migrate seed-sample-data`](#seed-sample-data) /
[`obs-migrate remove-sample-data`](#remove-sample-data) subcommands, which ship
in the installed wheel and honor the shared `--ca-cert`/`--insecure` TLS flags.
`scripts/setup_telemetry_data.py` is now a thin shim over the same library, kept
for existing automation:

```bash
set -a && source serverless_creds.env && set +a
DATA_HOURS=6 INTERVAL_SEC=30 BATCH_DOC_LIMIT=8000 \
  .venv/bin/python scripts/setup_telemetry_data.py migration_output/dashboards
```

Use the migrated dashboard artifact directory for any source. Pass multiple
artifact roots to generate one combined target schema/data set:

```bash
DATA_HOURS=6 INTERVAL_SEC=30 BATCH_DOC_LIMIT=8000 \
  .venv/bin/python scripts/setup_telemetry_data.py \
    grafana_output/dashboards datadog_output/dashboards
```

When validating multiple source families together, keep their metric streams
source-specific to avoid mapping conflicts between Prometheus-style labels and
Datadog/ECS field objects. A typical shared validation target uses:

- Grafana Prometheus-style dashboards: `metrics-prometheus-default`
- Datadog dashboards: `metrics-datadog-default`
- Shared logs: `logs-generic-default`

```bash
set -a && source serverless_creds.env && set +a

.venv/bin/obs-migrate cluster ensure-data-views \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY" \
  --data-view-patterns "metrics-prometheus-default,metrics-datadog-default,logs-generic-default"

.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir grafana_assets \
  --output-dir grafana_output \
  --assets dashboards \
  --data-view metrics-prometheus-default \
  --esql-index metrics-prometheus-default \
  --logs-index logs-generic-default

.venv/bin/obs-migrate migrate \
  --source datadog \
  --input-mode files \
  --input-dir datadog_assets/dashboards \
  --output-dir datadog_output \
  --assets dashboards \
  --data-view metrics-datadog-default \
  --logs-index logs-generic-default

DATA_HOURS=168 INTERVAL_SEC=3600 BATCH_DOC_LIMIT=8000 \
  .venv/bin/python scripts/setup_telemetry_data.py \
    grafana_output/dashboards datadog_output/dashboards
```

The common setup script discovers YAML and verification packets from each
artifact root. Useful flags:

| Flag | Meaning |
|---|---|
| `--data-hours` | Hours of synthetic data to generate. Defaults to 2. Falls back to `DATA_HOURS` env. |
| `--interval-sec` | Seconds between samples. Defaults to 60. Falls back to `INTERVAL_SEC` env. |
| `--batch-docs` | Documents per bulk request. Defaults to 5000. Falls back to `BATCH_DOC_LIMIT` env. |
| `--max-combinations` | Maximum dimension combinations per stream per timestamp. Defaults to 12. Falls back to `MAX_COMBINATIONS` env. Lower this for very high-cardinality contracts. |
| `--no-recreate` | Skip all index template and data stream operations. Use when the streams already exist with the desired mappings and you only want to ingest more synthetic documents. |

Dashboard migration writes `schema_change_report.md` and
`telemetry_contract.json` automatically. To regenerate schema changes from
source queries to target fields, or to combine several source outputs, use the
package-native [`obs-migrate schema-report`](#schema-report) subcommand. The
equivalent repo-checkout script is:

```bash
.venv/bin/python scripts/generate_telemetry_contract.py \
  grafana_output/dashboards datadog_output/dashboards \
  --output telemetry_contract.json \
  --schema-report schema_change_report.md
```

Both forms write a single Markdown document with a top-level summary plus one
section per artifact directory, mapping every panel from its source
fields/queries to the target stream/fields it produces.

### Pipeline Trace Regeneration

```bash
.venv/bin/python scripts/audit_pipeline.py --update-docs
```

## Test Commands

```bash
.venv/bin/python -m pytest tests/ -x -q
.venv/bin/python -m pytest tests/core/ -x -q
.venv/bin/python -m pytest tests/test_migrate.py -x -q
.venv/bin/python -m pytest tests/test_datadog_migrate.py -x -q
.venv/bin/python -m pytest tests/e2e/ -x -q
```
