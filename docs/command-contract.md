# Command Contract

This is the canonical command inventory for the **installed CLI**: everything
here runs from an `elastic-observability-migration` install, with no repo
checkout.

Use this file as the source of truth for:
- supported commands
- required environment variables
- safe example invocations

Contributor and CI commands — verification/benchmark gates, `scripts/` lab
lifecycle, repo-oriented validation CLIs, and the test suite — live in
[`contributing/dev-commands.md`](contributing/dev-commands.md).

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

**One tool:** use `obs-migrate` for everything (doctor, samples, migrate,
upload, verify, and cluster ops). Prefer the `[all]` extra so Grafana, Datadog, and Kibana
tooling install together. The older `grafana-migrate` / `datadog-migrate`
commands remain as compatibility aliases.

**Platforms:** macOS and Linux are supported. CI runs on Ubuntu; packaging is
also smoke-tested on macOS. Windows is not supported.

**Python:** 3.11 or newer (tested on 3.11, 3.12, and 3.13; 3.10 and older are
rejected, and 3.14+ works but is not in the CI matrix yet — doctor prints a note,
not a failure). On 3.11, keep `uv` on `PATH` for the kb-dashboard `uvx`
fallback. After install, run doctor with the **same launcher** you will use for
migrate — `uvx --from 'elastic-observability-migration[all]' obs-migrate doctor` if you are staying on `uvx`, or
a bare `obs-migrate doctor` once the install location is on `PATH`.
Doctor checks Python, required imports, extras, and compile tools, and exits
non-zero if something blocking is missing.

### Recommended (operators): `uvx` + `[all]`

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) on `PATH`.
Install from PyPI (recommended):

```bash
PKG='elastic-observability-migration[all]'
# Optional pin, e.g. PKG='elastic-observability-migration[all]==1.0.0'
uvx --from "$PKG" obs-migrate doctor
uvx --from "$PKG" obs-migrate list-samples
```

GitHub tag fallback (never `@main`):

```bash
PKG='elastic-observability-migration[all]@git+https://github.com/elastic/observability-migration-platform.git@v1.0.0'
uvx --from "$PKG" obs-migrate doctor
```

Example pins above are kept in lockstep with the released package version. The
PyPI badge on the README always shows the latest published version.

### Persistent tool install (bare `obs-migrate` in every shell)

Use this when you want `obs-migrate` on `PATH` permanently instead of prefixing
every command with `uvx --from`:

```bash
uv tool install 'elastic-observability-migration[all]'
export PATH="$HOME/.local/bin:$PATH"
obs-migrate doctor
```

The shim lands in `~/.local/bin` (`uv tool dir --bin` prints the real
location). `uv` cannot modify the `PATH` of the shell that invoked it — it only
warns that the directory is missing — so the `export` above is what makes the
bare command work *now*; run `uv tool update-shell` once so new shells get it
without the export.
`pipx install 'elastic-observability-migration[all]'` behaves the same way.
Upgrade with `uv tool upgrade elastic-observability-migration`.

`uv tool install` resolves against your newest available Python, which can be
above the tested range — pass `--python 3.13` to pin a CI-matrix interpreter.

### Persistent pip install

Activate the venv once per shell to get the bare command:

```bash
PKG='elastic-observability-migration[all]'
python3 -m venv .venv
source .venv/bin/activate
pip install "$PKG"
obs-migrate doctor
```

From an unpacked release source archive, install the current directory instead:

```bash
pip install ".[all]"
obs-migrate doctor
```

Setting up a repo checkout for development? Use `uv sync --locked --all-extras`
(or `make sync`) and `uv run obs-migrate doctor`. See
[`contributing/dev-commands.md`](contributing/dev-commands.md).

If `obs-migrate` is not found after install, see
[If you see `command not found: obs-migrate`](#if-you-see-command-not-found-obs-migrate)
below.

Every example below assumes `obs-migrate` is on `PATH` (an activated
virtualenv, a `pipx` / `uv tool` install, or the `uvx --from "$PKG"` prefix
from the section above). If you see `command not found`, use the same
troubleshooting section.

No command shells out to `kb-dashboard-cli` any more. The dashboard-YAML
compile path was removed (see [Removed: the dashboard-YAML
surfaces](#removed-the-dashboard-yaml-surfaces)), so migrate and upload need
nothing beyond the Python package: `obs-migrate doctor` no longer treats a
missing `kb-dashboard-*` as a blocking gap.

Datadog live API extraction (`--input-mode api` on either the unified or
dedicated CLI; legacy dedicated spelling `--source api` also works) requires
the optional Datadog client extra:

```bash
pip install "elastic-observability-migration[datadog]"
```

Setting up a repo checkout for development instead? See
[`contributing/dev-commands.md`](contributing/dev-commands.md).

### If you see `command not found: obs-migrate`

`obs-migrate` is a console script, not a global binary: it is only callable as
a bare command when its install location is on `PATH`. That happens after
`source .venv/bin/activate`, or after a `pipx install` / `uv tool install`
([Persistent tool install](#persistent-tool-install-bare-obs-migrate-in-every-shell)).
Otherwise, prefix it with a launcher.

Pick **one** of these, matching how you installed:

```bash
# uvx, no install step (works in any shell)
uvx --from 'elastic-observability-migration[all]' obs-migrate doctor

# persistent virtualenv: activate once per shell, then use the bare command.
# The path is relative — run it from the directory holding the virtualenv.
source .venv/bin/activate && obs-migrate doctor

# permanent bare command, no prefix and no activate. The export is required in
# the shell you install from, because the install cannot change its PATH.
uv tool install 'elastic-observability-migration[all]'
export PATH="$HOME/.local/bin:$PATH"
obs-migrate doctor
```

A fresh shell does not remember the previous shell's activation or `PKG`
value, so re-activate (or re-export `PKG`) before reusing the examples in this
document. A tool install is the only one of the three that survives a new
shell on its own, and only after `uv tool update-shell`.

## Before Elastic / Kibana

You can use the migration tooling productively before configuring a target cluster.

- Translate exported dashboards into native Dashboard-as-Code artifacts (`native/*.native.json`) — the exact typed Kibana Dashboards API payload, ready to review and upload.
- List bundled sample dashboards with `obs-migrate list-samples` (offline, no
  credentials), then migrate one with
  `obs-migrate migrate --source <source> --input-mode files --input-dir <input_dir>`.
- Pull live dashboards from Grafana or Datadog APIs.
- Pull Grafana alert artifacts or Datadog monitor artifacts.
- Read `migration_summary.md` for a human-readable verdict, scorecard, and
  per-dashboard worklist, then drill into `migration_report.json`,
  `migration_manifest.json`, `verification_packets.json`, and `rollout_plan.json`.
- Review `native/*.native.json` artifacts offline before uploading.

Add `--es-url` when you want live target field discovery or emitted-query validation. Add `--kibana-url` when you want upload, target dashboard listing/deletion, smoke validation, or alert-rule payload checks against a real Kibana target.

## Asset Scope Contract

Every migration command that moves source assets into target artifacts accepts
`--assets {dashboards,alerts,all}`.

- `--assets dashboards`: migrate dashboards only
- `--assets alerts`: migrate alerts only
- `--assets all`: run both isolated pipelines in one command

Rules:
- `dashboards` never writes alert artifacts
- `alerts` never writes dashboard artifacts or compiled output
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
| `--field-profile` | Grafana, Datadog | Target field mapping profile (plan, then verify with `--es-url`) | Defaults to `otel` for every source. **Grafana:** `otel`, `prometheus_remote_write` (Fleet `use_types` typed leaves), `prometheus_metrics` (classic Metricbeat `prometheus.metrics.*` / `prometheus.labels.*`), `prometheus_native`, `passthrough`, `auto` (`auto` requires `--es-url`; ambiguous caps → `otel` + warn). **Datadog:** `otel`/`default`, `elastic_agent`, `prometheus` (Metricbeat `prometheus.metrics.*` / `prometheus.labels.*`), `prometheus_native` (ES `/_prometheus` `metrics.*` / `labels.*`), `passthrough`, YAML — **no `auto`**. Live `_field_caps` verify the plan; they do not silently remap to a different layout. Datadog Prometheus profiles apply label paths to metric queries while log queries retain ECS / OTel fields. |
| `--metric-map-file` | Grafana, Datadog | Source metric name → target field override file | Source-neutral YAML with top-level `metric_map`. Use this for explicit metric renames while `--field-profile` continues to select the target schema family. May be repeated; later files override earlier entries and adapter-specific maps. On Grafana, when mode is still `auto`, this also selects ES\|QL translation so the map applies (parity with Datadog). |
| `--data-view` | Grafana, Datadog | The Kibana **data view / index pattern the migrated panels bind to in the UI** | When omitted, the source adapter keeps its own default (Grafana: `metrics-*`). For Datadog, non-OTel profiles keep their profile index (for example `prometheus` keeps `metrics-prometheus-*`). See [Target index flags](#target-index-flags-data-view-vs-esql-index). |
| `--esql-index` | Grafana | The index / data stream for **schema discovery and every emitted metrics query** (native `PROMQL index=…` and ES\|QL `TS`/`FROM`) | Defaults to `--data-view` when unset. Override it (with `--es-url`) when queries and field discovery should use a specific data stream — required for Prometheus fidelity. `--data-view` may still differ as the Kibana UI / control bind. Grafana-only today; Datadog controls its metric query target through `--data-view` / the active `--field-profile` instead. See [Target index flags](#target-index-flags-data-view-vs-esql-index). |
| `--logs-index` | Grafana, Datadog | The index / data stream written into translated Loki / LogQL (log) panels | Defaults to the source/profile log index (`logs-*`) when unset, not `--data-view`; the log analog of `--esql-index`. |
| `--translation-mode {auto,native,esql}` | Grafana (Datadog accepts as no-op) | Override Grafana's native-PROMQL/ES\|QL selection | Defaults to `auto`; use `native` or `esql` only for explicit operator control |
| `--preflight` | Grafana, Datadog | Probe target field capabilities and write a readiness contract before migration | Grafana writes `required_target_contract.json`; Datadog writes `target_readiness_contract.json`. Requires `--es-url` for live field discovery; offline runs record every field as `unknown`. |
| `--validate` | Grafana, Datadog | Validate emitted ES\|QL queries against Elasticsearch after translation | Requires `--es-url`. Auto-applies safe query fixes and manualizes broken ones before upload. |
| `--upload` | Grafana, Datadog dashboards | Upload dashboards during the migration run | Uses the in-memory native Dashboards API payload; still writes `native/*.native.json`, `ir/*.ir.json`, and reports for review/audit. |
| `--create-alert-rules` | Grafana, Datadog | Create emitted Kibana alerting rules immediately after the alert mapping step | Requires alert-capable asset selection (`--assets alerts` or `--assets all`), `--kibana-url`, and `--kibana-api-key`. Rules are created **disabled** and tagged `obs-migration`; draft (review-required) rules also get `obs-migration-review`. Writes `alert_rule_upload_results.json` (Grafana) or `monitor_rule_upload_results.json` (Datadog). A requested creation that does not happen **fails the run** rather than warning and exiting `0` — see [Creating Kibana alert rules from a single command](#creating-kibana-alert-rules-from-a-single-command) for the exit codes. |
| `--no-draft-alert-rules` | Grafana, Datadog | With `--create-alert-rules`, skip draft rules and create only fully-automated translations | Draft rules are created by default. Use this to restrict creation to translations the engine is confident about. |
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
Datadog. Grafana integrated smoke threads each QueryIR identifier-control
default into direct ES|QL validation, so a `??field` grouping is checked with
the same initial field selection as the uploaded dashboard. A run can still
exit `0` while smoke reports empty panels or runtime errors, so inspect
`migration_report.json`, `migration_summary.md`, and the smoke report before
declaring the uploaded dashboard production-ready.

Examples below use the canonical environment names
(`$ELASTICSEARCH_ENDPOINT`, `$KIBANA_ENDPOINT`, `$KEY`) that match
`serverless_creds.env`. The compatibility aliases `$ES_URL`, `$KIBANA_URL`, and
`$ES_API_KEY` are still accepted by every CLI and refer to the same values.

```bash
# Grafana dashboards only (files); native PROMQL is the default
obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --assets dashboards \
  --field-profile otel \
  --data-view "metrics-*" \
  --esql-index "metrics-*"

# Datadog alerts only (API)
obs-migrate migrate \
  --source datadog \
  --input-mode api \
  --env-file datadog_creds.env \
  --output-dir datadog_migration_output \
  --assets alerts \
  --field-profile otel \
  --data-view "metrics-*" \
  --monitor-ids 12345678

# Grafana dashboards + alerts from one run (live API)
obs-migrate migrate \
  --source grafana \
  --input-mode api \
  --grafana-url "$GRAFANA_URL" \
  --grafana-token "$GRAFANA_TOKEN" \
  --output-dir migration_output \
  --assets all \
  --field-profile otel \
  --data-view "metrics-*" \
  --esql-index "metrics-*"

# Grafana alerts only — selected rules by UID
obs-migrate migrate \
  --source grafana \
  --input-mode api \
  --grafana-url "$GRAFANA_URL" \
  --grafana-token "$GRAFANA_TOKEN" \
  --output-dir migration_output \
  --assets alerts \
  --alert-uids "rule-uid-1,rule-uid-2"

# Grafana alerts only — all rules from a specific folder
obs-migrate migrate \
  --source grafana \
  --input-mode api \
  --grafana-url "$GRAFANA_URL" \
  --grafana-token "$GRAFANA_TOKEN" \
  --output-dir migration_output \
  --assets alerts \
  --alert-folder "infra-folder-uid"
```

`obs-migrate migrate` emits exactly two dashboard representations during
dashboard runs for both Grafana and Datadog: native Dashboard-as-Code review
artifacts (`dashboards/native/*.native.json`) and semantic IR review artifacts
(`dashboards/ir/*.ir.json`). **A migration writes no `dashboards/yaml/` and no
`dashboards/compiled/` directory.** The typed-API upload uses the native payload
derived from `DashboardIR`. Alerts-only runs emit no dashboard artifacts.

For both Grafana and Datadog a semantic `DashboardIR` is the primary working
artifact, and both persisted representations are *derived* from it
(`observability_migration/core/assets/dashboard.py`,
`observability_migration/targets/kibana/dashboards_api.py::
native_dashboard_from_ir`). `DashboardIR.to_yaml_dict()` survives as the
in-memory *dict* shape the panel mapping layer speaks — it is not a file format,
and nothing writes it to disk. See `docs/architecture/asset-model.md`.

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

`--field-profile` defaults to `otel` for every source migration, including
Grafana (`grafana-migrate`, `obs-migrate migrate --source grafana`) and
Datadog. Explicitly setting `--field-profile otel` is equivalent to omitting
the flag.

Both sources share the same operator model:

1. **`--field-profile` selects the plan** — emitted queries and field names follow
   that profile's mapping rules, including offline runs with no `--es-url`.
2. **With `--es-url`, verify against live `_field_caps`** — readiness and
   type-aware checks only; the tool does **not** silently remap to a different
   layout when caps disagree with the plan.
3. **Artifacts record the plan plus per-field status** where contracts exist
   (Grafana `required_target_contract.json`, Datadog
   `target_readiness_contract.json`).

> **Breaking change:** Default Grafana **`otel`** no longer auto-namespaces from
> live caps. For Fleet typed remote-write, classic Metricbeat nested, or native
> Prometheus endpoint layouts, pass **`--field-profile auto --es-url`** or an
> explicit **`prometheus_remote_write`** / **`prometheus_metrics`** /
> **`prometheus_native`** plan. Explicit **`otel`** still field-selects
> `metrics.<name>` when the bare PromQL name is absent from caps (OTel Collector
> shape; issue #270).

Grafana accepts:

- **`otel`** (default) — bare / OTel-candidate metric and label mapping. With
  `--es-url`, verify fields exist; warn on missing.
- **`prometheus_remote_write`** — planned Fleet/Agent remote-write layout
  (`use_types`): `prometheus.<metric>.{counter,value,rate}`,
  `prometheus.labels.*`. With `--es-url`, verify; set `profile_mismatch` when
  live caps look like another named layout (translation keeps the plan).
- **`prometheus_metrics`** — classic Metricbeat remote_write
  (`use_types=false`): `prometheus.metrics.<metric>`, `prometheus.labels.*`.
  Same verify / mismatch rule. Aligns with Datadog's `prometheus` profile.
- **`prometheus_native`** — planned native ES Prometheus endpoint layout:
  `metrics.<metric>`, `labels.*`. Same verify / mismatch rule as
  `prometheus_remote_write`.
- **`passthrough`** — emit source label and metric names verbatim; automatic mapping is disabled. Explicit rule-pack `label_rewrites` / `ignored_labels` /
  `control_field_overrides` still apply. With `--es-url`, validate bare names
  when possible; no automatic remapping. Alerts-only runs perform the same
  validation and record it under `alerts.field_discovery` in `run_summary.json`.
  A native PROMQL query that references a rewritten or ignored label routes
  through the ES|QL translator so the explicit rule-pack override is not
  bypassed.
- **`auto`** (Grafana-only) — requires `--es-url`. Detect a clear
  `prometheus_remote_write`, `prometheus_metrics`, or `prometheus_native`
  layout from caps; if ambiguous, emit as **`otel`** and warn. Rejected
  without `--es-url`.

Grafana field-discovery summaries retain `automatic_mapping` as the mapping
state (`false` only for `passthrough`). The separate
`automatic_profile_selection` key is `true` only when the requested profile is
`auto`.

Datadog accepts `otel`, `default` (alias of `otel`), the Datadog-specific
built-ins `elastic_agent`, `prometheus` (Metricbeat remote_write:
`prometheus.metrics.*` / `prometheus.labels.*`; Grafana twin:
`prometheus_metrics`), `prometheus_native`
(Elasticsearch `/_prometheus` write: `metrics.*` / `labels.*`), and
`passthrough`, plus YAML profile files. Datadog has **no `auto`** profile —
always pick an explicit plan. With
`--es-url`, field readiness uses `confirmed` / `missing` / `unknown` against
that plan. Any other value is rejected (Grafana exits `2`, Datadog exits `1`).
Prometheus profile label paths apply only to metric queries; Datadog log
queries continue to use ECS / OTel field mappings.

> **Breaking change:** Datadog `--field-profile prometheus` now emits
> `prometheus.labels.*` for metric tags rather than ECS/bare fields. Choose
> `prometheus_native` for native `labels.*` metrics. Log-query mappings are
> unchanged.

```bash
# Grafana passthrough: keep raw Prometheus names when the target already stores them
obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --assets dashboards \
  --field-profile passthrough \
  --data-view "metrics-*" \
  --esql-index "metrics-*" \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY"
```

Datadog `--data-view` is an explicit override, not a hidden default. If omitted,
the active profile controls the metric index (`otel` / `default` / `passthrough`
/ `elastic_agent` use `metrics-*`, `prometheus` uses `metrics-prometheus-*`, and
custom YAML profiles can set their own `metric_index`).

### Target index flags: data-view vs esql-index

`--data-view` and `--esql-index` look interchangeable but control different
things, and getting `--esql-index` wrong is the most common reason a migrated
Prometheus dashboard renders empty.

| Flag | Default | What it controls |
|---|---|---|
| `--data-view` | `metrics-*` (Grafana) | The **Kibana data view** the migrated panels / controls bind to in the UI when it differs from the query target. Also the fallback metrics query index when `--esql-index` is unset. |
| `--esql-index` | falls back to `--data-view` when unset | The **metrics query + schema-discovery target**: native `PROMQL index=…`, ES\|QL `TS`/`FROM`, and the index inspected with `--es-url` for field layout. |

In code the Grafana schema resolver and panel translator both use
`args.esql_index or args.data_view` as the metrics query / discovery target, so
when you omit `--esql-index` it inherits `--data-view` for native PROMQL, ES|QL
emission, and field discovery. Set `--esql-index` explicitly when your metrics
live in a data stream whose name differs from the Kibana data view you want
panels bound to. `--esql-index` is a Grafana flag; Datadog has no separate
ES|QL-index override and instead derives its metric query target from
`--data-view` / the active `--field-profile`.

**Metrics query target (native PROMQL and ES|QL).** Every emitted *metrics
query* — native `PROMQL index=…` and ES|QL `TS`/`FROM` — reads
`esql_index or data_view` (the same pattern schema discovery probes). Setting
`--esql-index` to a concrete Prometheus stream therefore retargets **both**
native and ES|QL panels. `--data-view` remains the Kibana UI / control bind
when it differs; it is no longer a second, silent query index for native
PROMQL.

**Prometheus users:** point `--esql-index` (with `--es-url`) at your real
Prometheus data stream so discovery and every generated metrics query read the
same place. Leaving the default while your data lives elsewhere is the
difference between a working dashboard and an empty one. For example, to keep
a broad Kibana data-view bind while queries and discovery use a concrete
stream:

```bash
obs-migrate migrate \
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

Both native-PROMQL and ES|QL-fallback panels then query
`metrics-alloy.prometheus-default`. Prefer setting `--data-view` to that same
stream when you also want the Kibana data-view object / controls scoped there.

Without `--es-url`, schema discovery is skipped entirely, so `--esql-index`
still sets the query `FROM`/`PROMQL index` target, but the run falls back to
OTel field defaults and cannot warn you that the index does not match your data.

**Exception — Grafana panels already written as raw ES|QL.** If a source
Grafana panel's `datasource.type` is `elasticsearch` and its query text is
already `FROM …` / `TS …` (Grafana's native Elasticsearch query editor, not
PromQL), the migration passes that query through **verbatim** and does not
retarget its index with `--esql-index` / `--data-view` — rewriting hand-authored
ES|QL text risks breaking the operator's exact `STATS`/`WHERE` syntax, so the
tool leaves it alone rather than guess. `migration_report.json` still shows
`query_language: esql` and `target_index` for that panel so you can see (and,
if needed, hand-edit) which index it kept. This is intentional and does not
warn, because dashboards that mix ES|QL log/metric panels with PromQL panels
legitimately target different indexes on purpose.

### Migrate-first vs data-first (data plane before assets)

`obs-migrate` moves dashboard/alert **definitions**. It does not create
collectors or invent a data stream from Grafana datasource UIDs. Empty panels
after a green migrate are usually a **data-plane** problem. Two valid
sequences:

**Migrate-first (assets before telemetry):**

1. Choose the ingest route and the **concrete** stream name it will create.
2. Migrate with both `--data-view` and `--esql-index` set to that planned
   stream (avoid a mixed `metrics-*` wildcard on shared clusters).
3. Without `--es-url`, field mappings are unverified guesses — empty panels
   until data lands are expected.
4. When dual-write starts, re-run with `--es-url` / `--preflight` against the
   **same** stream, then `live_validate` / compare / render audit.
5. Use `seed-sample-data` only for demos on a dedicated stream — not as cutover
   proof.

**Data-first (telemetry already in Elastic):**

1. Pass `--es-url` so migrate can list concrete streams under your pattern.
2. If the metrics target is a wildcard, the CLI names the streams it resolves
   to and asks you to pin both flags to one of them; when those streams span
   several backends (Prometheus + Datadog + OTel) it says so explicitly. Treat
   TSDB dimension/metric merge failures as **index readiness**, not translator
   bugs.
3. Migrate pointed at that stream, then verify immediately.

**Operator rule of thumb:** ingest path → concrete stream → set both
`--data-view` and `--esql-index` → migrate/verify.

`grafana-migrate` (dashboards and alerts-only runs alike) prints a
metrics-target readiness warning only when the target is actually risky — a
wildcard query target (with or without `--es-url`), a wildcard that resolves to
several streams, a target whose streams cannot be listed, a pinned target that
does not resolve on the cluster, or TSDB dimension/metric conflicts. A run that
already pins both flags to an existing concrete stream prints nothing. When the
two flags differ, queries follow `--esql-index` (or `--data-view` when unset)
and the CLI prints an informational note; it escalates to a warning only in the
surprising direction, where `--data-view` is concrete but the queries still span
a wildcard. The same findings are written to `run_summary.json` under
`metrics_target` (`alerts.metrics_target` for alerts-only runs) so CI and the
reporting skills see them after the console output has scrolled away.
`datadog-migrate` does not print this warning yet; apply the same rule of thumb
to its `--data-view` / `--field-profile` metric index by hand.

#### The `data_stream.dataset` filter is scoped to wildcard targets

A migrated Grafana dashboard can carry a dashboard-level
`data_stream.dataset` filter, so "I pointed at `metrics-*`" does not always mean
"I am reading all of `metrics-*`". The rules:

- **Metrics panels.** The filter is emitted only when *every* panel query index
  contains a wildcard, and defaults to the literal `prometheus`
  (`--dataset-filter` overrides it, `--dataset-filter ""` disables it). Pinning
  `--esql-index` to a concrete stream drops the filter, because the index
  pattern is already the constraint and a literal `prometheus` would exclude
  every document in, say, a `prometheus.remote_write` data stream.
- **Native PROMQL** clears the default filter outright, and `--translation-mode
  esql` clears it too for the `otel` / `auto` / `passthrough` profiles, since
  binding `data_stream.dataset: prometheus` on OTel data renders panels empty.
- **Logs panels.** No filter unless you pass `--logs-dataset-filter`, and then
  only for wildcard log targets.

#### `--logs-index` is independent of `--data-view`

Loki/LogQL panels read `--logs-index` (default `logs-*`, also settable as
`logs_index` in a rule pack). It does **not** inherit `--data-view` or
`--esql-index`, so a dashboard that mixes metrics and logs panels needs both
targets set explicitly.

### Reusing existing OTEL metrics with `--metric-map-file`

Use `--metric-map-file` when the dashboard was authored against one metric
vocabulary but the target Elasticsearch data uses another one. This is common
when a Grafana Kubernetes dashboard uses Prometheus/cAdvisor metric names while
the target cluster already has OpenTelemetry semantic-convention metrics, or when
a Datadog customer moves collection from the Datadog Agent to OTel.

`--metric-map-file` is an operator-authored override, not an auto-suggested
mapping library. Build the YAML from your target schema knowledge plus the
migration artifacts (`required_target_contract.json` /
`target_readiness_contract.json` and `schema_change_report.md`), then verify
against real data with `--es-url --preflight`. Offline runs can validate the YAML
shape, but live field status remains `unknown` until `_field_caps` can inspect
the target index.

Keep the roles separate:

| Flag | Job |
|---|---|
| `--field-profile` | Target schema family (`otel`, `prometheus_native`, `auto`, …) |
| `--data-view` / `--esql-index` | Which index / data view panels bind to and query |
| `--metric-map-file` | Explicit source-metric → target-field renames |

#### Shared metric map file

Example `my-otel-metric-map.yaml` (same file works for Grafana and Datadog):

```yaml
metric_map:
  # Grafana / Prometheus exact rename: v1 applies this rename.
  container_memory_working_set_bytes: container.memory.working_set

  # Grafana / Prometheus Class-2: attribute_filter, transform, and unit_scale
  # are applied in emitted ES|QL (target rename plus filter/scale/rate semantics).
  container_network_receive_bytes_total:
    target: k8s.pod.network.io
    attribute_filter: { network.direction: receive }

  # Datadog exact rename: v1 applies this rename.
  system.cpu.user: system.cpu.user.pct

  # Datadog Class-2: transform/to_rate is honored when target counter kind is known.
  system.net.bytes_rcvd:
    target: system.network.in.bytes
    transform: to_rate

# Optional: rename tag / label / attribute names to target ES fields. Datadog
# applies these over the profile tag_map; Grafana applies them as label
# rewrites (highest precedence). Metric queries only.
tag_map:
  host: host.name
  instance: host.name
  env: deployment.environment
```

The file must have a top-level `metric_map:` and/or `tag_map:` key (either or
both). Grafana rule-pack wrappers (`query: { metric_map: … }`) and full Datadog
field-profile YAML are **not** accepted by `--metric-map-file`.

##### `metric_map` targets are verbatim — include the profile prefix yourself

A `metric_map` target **replaces** profile-based namespacing; the profile's
metric prefix is *not* prepended to it. An unmapped metric still gets the
prefix, so a partially-mapped file silently produces two different field
shapes:

```yaml
# --field-profile prometheus_native
metric_map:
  redis_uptime_in_seconds: system.uptime
```

```
redis_uptime_in_seconds  (mapped)   -> system.uptime                    # no metrics. prefix
redis_connected_clients  (unmapped) -> metrics.redis_connected_clients  # prefix applied
```

Write the fully-qualified target field instead:

```yaml
# --field-profile prometheus_native
metric_map:
  redis_uptime_in_seconds: metrics.system.uptime

# --field-profile prometheus_metrics  (Datadog: prometheus)
metric_map:
  redis_uptime_in_seconds: prometheus.metrics.system.uptime
```

This is deliberate — an explicit rename must be able to target any field,
including one outside the profile's namespace — but it means the profile does
not "finish" a `metric_map` entry for you. With `--es-url`, a target that does
not exist is reported as `missing` in `target_readiness_contract.json`; offline
it is unverifiable, so check the prefix by hand.

#### Grafana existing-OTEL example

Run the migration against the existing OTEL metrics stream. With
`--metric-map-file`, Grafana automatically uses ES|QL translation so the map
applies (same operator path as Datadog). Pass `--translation-mode native` only
if you intentionally want literal Prometheus metric names instead.

```bash
obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir ./grafana_exports \
  --output-dir ./out_grafana_otel \
  --assets dashboards \
  --field-profile otel \
  --metric-map-file ./my-otel-metric-map.yaml \
  --data-view metrics-otel-* \
  --esql-index metrics-otel-* \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY" \
  --preflight
```

Expected result:

- Exact entries appear in emitted ES|QL and in `dashboards/native/*.native.json`.
- `required_target_contract.json` includes `mapped_from` for renamed fields.
- Class-2 entries (`transform`, `attribute_filter`, or non-1 `unit_scale`) apply
  in emitted ES|QL: target rename plus attribute filters, unit scaling, and
  rate transform planning when the target field kind is known.
- For standard Kubernetes OTEL dashboards, first compare against the managed
  `[OTEL] [Metrics Kubernetes]` dashboards. If adapting a migrated board, bind
  `--data-view` / `--esql-index` to the same metrics stream those dashboards
  use.

#### Datadog existing-OTEL example

```bash
obs-migrate migrate \
  --source datadog \
  --input-mode files \
  --input-dir ./datadog_exports \
  --output-dir ./out_dd_otel \
  --assets dashboards \
  --field-profile otel \
  --metric-map-file ./my-otel-metric-map.yaml \
  --data-view metrics-otel-* \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY" \
  --preflight
```

Expected result:

- Exact entries apply through the same shared core as Grafana.
- Class-2 entries apply through the same shared core as Grafana; attribute
  filters, unit scaling, and rate transforms appear in emitted ES|QL when
  supported.
- `target_readiness_contract.json` lists required target fields and includes
  `mapped_from` for default or explicit metric renames.
- Custom application metrics stay unmapped/missing until the operator authors
  explicit rows or keeps collection names aligned.

Scaffold a starter map from migration artifacts when you need to fill gaps:

```bash
obs-migrate metric-map scaffold \
  --artifact-dir migration_output/dashboards \
  --output ./my-otel-metric-map.yaml
```

Reads `required_target_contract.json`, `target_readiness_contract.json`, and/or
`migration_manifest.json` under `--artifact-dir`, collects source metric names
that are not already mapped, and writes source-neutral YAML with empty
`target: ""` placeholders and `provenance: scaffold`. Those entries load safely
but resolve as unapplied gaps until each target is filled. The scaffold never
invents target names. Prometheus recording-rule-style names (containing `:`)
also get a scaffold hint of `transform: drop_rate` plus a header comment —
fill `target` with the pre-rated gauge/OTel field (or recreate the rule);
do not treat those names as exact renames to a counter.

Authoring tip: start from scaffold output, fill Class-1 renames first, then add
Class-2 fields (`attribute_filter`, `transform`, `unit_scale`, `target_index`,
`variants`) only where the target schema needs them. Prefer
`--metric-map-file` over embedding maps in rule packs / field profiles so the
same file works for Grafana and Datadog.

#### Advanced alternatives

Prefer `--metric-map-file` for metric renames. Keep these for broader adapter
customization:

- Grafana: `--rules-file` for label rewrites, metric kinds, panel overrides, and
  optional embedded `query.metric_map` (overridden by `--metric-map-file` on
  duplicate keys). When a dashboard carries a `gnetId` that matches a bundled
  curated pack (e.g. Redis 763, Redis Enterprise 18405, Redis Cloud 18406), the
  pack is merged in automatically beneath the user `--rules-file` so the user
  always wins on collision. Pass `--no-curated-packs` to skip all curated packs
  and use only the base rule pack.
- Datadog: YAML `--field-profile path.yaml` for a full custom profile
  (`metric_index`, `tag_map`, prefixes, embedded `metric_map`). `--metric-map-file`
  still overrides embedded `metric_map` entries for duplicate keys.

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
preflight writes `required_target_contract.json` with the operator's
`field_profile`, `planned_schema_profile`, `detected_schema_profile`,
`profile_mismatch` (planned ≠ detected named layout; surfaced for operator
visibility — translation keeps the plan), backward-compatible `schema_profile`
(the detected layout), `field_capabilities_discovery`, and resolved
target-field statuses; Datadog dashboard runs write `target_readiness_contract.json`
with the active `field_profile`, metric/log index patterns, source fields,
resolved target fields, and statuses.

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
typed Dashboards API upload step (mapped to
`NODE_EXTRA_CA_CERTS` / `NODE_TLS_REJECT_UNAUTHORIZED`):

- `--ca-cert <path>` (env `OBS_MIGRATE_CA_CERT`): verify against a custom CA
  bundle/file; verification stays on. Preferred for private/internal CAs.
- `--insecure` (env `OBS_MIGRATE_INSECURE`): skip certificate verification
  entirely. Testing or trusted-network migration only; prints a one-time loud
  stderr warning. Prefer `--ca-cert` whenever possible.

On the dedicated CLIs these flags are honored across schema discovery, ES|QL
validation, source preflight/execution probes, dashboard upload, smoke
validation, and the alerting preflight/create/audit paths.

The repo-oriented `verify-panels` and `verify-visual` wrappers do not expose
these TLS flags today; prefer the package-native migration/upload/smoke paths
for custom-CA or self-signed target validation.

Unified Datadog API mode exposes `--env-file`, `--dashboard-ids`,
`--monitor-ids`, and `--monitor-query`. Datadog API mode still requires the
optional `datadog-api-client` dependency:

```bash
pip install "elastic-observability-migration[datadog]"
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

**A requested rule creation that does not happen fails the run.** Asking for
rules and getting none used to print a warning and still exit `0`, which is the
one failure nobody investigates because the rest of the run looks green.

| Situation | Exit code | When it is reported |
| --- | --- | --- |
| `--create-alert-rules` without an alert-capable `--assets` | `2` | Up front, before any migration work: `--assets dashboards` never runs the alert pipeline, so the request can never be satisfied. |
| `--create-alert-rules` without `--kibana-url` | `2` | Up front: there is no Kibana target to create rules in. |
| `--create-alert-rules` without `--kibana-api-key` | `1` | After the alert artifacts and run summary are written, so the translated payloads are still usable. |
| `--create-alert-rules` with an unreachable alerting preflight | `1` | Same — nothing was created, so the run does not report success. |
| `--create-alert-rules` with rules actually created | `0` | — |
| No `--create-alert-rules` | `0` | Never reported; the flag is opt-in and silent when unused. |

A skip is recorded in the rule-upload artifact and in
`<output-dir>/run_summary.json` under `alerts.rule_creation` (`status`,
`reason`, `message`), so CI keeps the evidence after the console scrolls. A run
whose translations are all `manual_required` legitimately creates zero rules and
still exits `0` — the check fires on *creation never being attempted*, not on a
zero count.

Use `obs-migrate audit-rules` (or the Kibana UI) to review the rules before
enabling them. `obs-migrate verify-alert-rules` is the self-cleaning round-trip
verifier (it creates rules with a temporary marker tag and cleans them up on
exit unless `--keep-rules` is passed). Both ship in the installed package.

```bash
# Unified: migrate dashboards + alerts + create rules (disabled).
set -a && source serverless_creds.env && set +a
obs-migrate migrate \
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

### Review Dashboard Artifacts Before Upload

Every dashboard migration run (`obs-migrate migrate`, `grafana-migrate`,
`datadog-migrate`) writes two parallel representations of each dashboard
under the dashboard artifact directory, whether or not `--upload` is passed:

```text
migration_output/dashboards/native/<stem>.native.json   # exact typed Dashboards API payload
migration_output/dashboards/native/index.json           # index over every native artifact in the run
migration_output/dashboards/ir/<stem>.ir.json            # semantic DashboardIR export
```

There is no `migration_output/dashboards/yaml/` and no
`migration_output/dashboards/compiled/` directory. Dashboard YAML is neither
produced nor consumed anywhere: `native/*.native.json` is the only upload input.

Because of that, the JSON artifacts that used to point at YAML now describe what
the run produced:

| Artifact | Field | Now |
|---|---|---|
| `rollout_plan.json` | `artifact_bundle.yaml_paths` | **removed**, replaced by `artifact_bundle.native_artifact_paths` and `artifact_bundle.ir_artifact_paths` |
| `rollout_plan.json` | `dashboards[].yaml_path` | **removed**, replaced by `dashboards[].native_artifact_path` and `dashboards[].ir_artifact_path` |
| `rollout_plan.json` | `artifact_bundle.compiled_paths` | **removed**, no NDJSON is produced |
| `rollout_plan.json` | `dashboards[].compiled_path` | **removed** |
| `migration_manifest.json`, `migration_report.json` | `dashboards[].yaml_path` | **removed**. Read `native_artifact_path` / `ir_artifact_path`, or `artifact_stem` (the shared filename stem of a dashboard's `native/` and `ir/` artifacts) |
| `migration_manifest.json`, `migration_report.json` | `dashboards[].compiled_path`, `dashboards[].compiled`, `dashboards[].compile_error` | **removed** |
| `migration_report.json` | `summary.compiled_ok`, `summary.yaml_lint_ok`, `summary.layout_ok` | **removed** |
| `migration_manifest.json` | `runtime_summary.yaml_lint`, `runtime_summary.compile`, `runtime_summary.layout` (Grafana) | **removed**. Datadog keeps `runtime_summary.layout`, which its post-upload smoke check still populates |
| `migration_summary.md` | the `Compiled` column and the `N/M compiled` run line | **removed** |

A stale `yaml/` or `compiled/` directory left in an output directory by an older
release is deleted on the next run, so an operator cannot mistake it for
something the current run produced.

`native/<stem>.native.json` is exactly `NativeDashboard.to_api_payload()` — the
same body `migrate --upload` would send immediately — wrapped in a small
envelope (`kind`, `version`, `dashboard_id`, `title`, `payload`, `mapping`).
Reviewing it before upload gives a "generate, inspect, upload" workflow with no
compile step at all: see `docs/architecture/asset-model.md`.

`ir/<stem>.ir.json` contains the full semantic `DashboardIR` serialized via
`asdict()`: every panel with its emitted queries, controls, alerts,
annotations, links, transforms, and per-panel metadata. It records the
translator's intermediate decisions — which panel type was chosen, what ES|QL
was derived from the source query, how controls were mapped — so you can audit
translator coverage and understand *why* a panel was translated a certain way.
`ir/*.ir.json` is **inspection-only**: no CLI flag or subcommand re-ingests
it. To adjust a panel's behavior, edit `native/<stem>.native.json` (which
holds the already-derived API payload) or re-run `obs-migrate migrate` with
different flags.

`native/<stem>.native.json` is uploaded **verbatim** by
`obs-migrate upload --artifact-dir`. Only the envelope structure (`kind`,
`version`, `payload` type) is validated; the `payload` content is sent as-is.
Operator edits made before upload are reflected in Kibana — remove a panel
from `payload.panels`, rename `payload.title`, or adjust time-range fields.
`upload` exits `1` when no reviewed native artifacts are found, rather than
uploading nothing quietly.

#### Two-step review workflow (Grafana and Datadog)

`migrate` writes both artifacts unconditionally — whether or not `--upload`
is passed. To inspect before committing to Kibana:

```bash
# Step 1 — translate only (no --upload)
obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir ./grafana_exports \
  --output-dir ./out \
  --assets dashboards

# Step 2a — inspect translator decisions (panel types, emitted queries, controls)
python3 -m json.tool out/dashboards/ir/my-dashboard.ir.json | less

# Step 2b — inspect the exact Dashboards API payload that will be sent
python3 -m json.tool out/dashboards/native/my-dashboard.native.json | less

# Optional: edit native/*.native.json before upload — edits are sent verbatim.

# Step 3 — upload the reviewed artifacts (rejects if native files are missing)
obs-migrate upload \
  --artifact-dir ./out/dashboards \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY"
```

The same pattern applies to Datadog — replace `--source grafana` and
`--input-dir` with the Datadog equivalents. `migrate` always writes
`native/*.native.json` and `ir/*.ir.json` for both sources.

#### Removed: the dashboard-YAML surfaces

Dashboard YAML support was deprecated and is now **removed**. Nothing in the
engine produces or consumes a kb-dashboard YAML document, and nothing shells out
to `kb-dashboard-cli`. The only dashboard artifacts are `native/*.native.json`
and `ir/*.ir.json`; the only deploy path is the typed Kibana Dashboards API
(`PUT /api/dashboards/{id}`).

**This is a breaking change.** Each surface below now exits `2` with a message
naming the replacement, rather than silently ignoring the flag:

| Removed surface | Replacement |
|---|---|
| `obs-migrate compile --yaml-dir <dir> --output-dir <dir>` | None. Nothing consumes NDJSON. Use `migrate` + `upload --artifact-dir`. |
| `obs-migrate upload --yaml-dir <dir>` | `obs-migrate upload --artifact-dir <dashboards>` |
| `obs-migrate upload --compiled-dir <dir>` | `obs-migrate upload --artifact-dir <dashboards>` |
| `obs-migrate upload --artifact-format {auto,native,yaml}` | Drop the flag. There is one format: the native payload under `native/`. |
| `obs-migrate upload --legacy-import` | Drop the flag. Uploads go through the typed Dashboards API. |
| `obs-migrate migrate --compile` / `--no-compile` | Drop the flag. No `compiled/` directory is written. |
| `obs-migrate migrate --legacy-import` | Drop the flag. |
| `grafana-migrate` / `datadog-migrate` `--compile`, `--no-compile`, `--legacy-import` | Drop the flag. |
| `--use-dashboards-api` (was a hidden no-op on `migrate` and `upload`) | Drop the flag. The typed Dashboards API is the only upload path. |

Removed with them: the `dashboards/compiled/` directory, the YAML lint stage,
the compiled-layout validation stage, and the per-dashboard legacy
compile+`_import` fallback for a rejected payload. A payload the typed API
rejects is now reported as a terminal failure instead of being quietly retried
through a different renderer.

Grafana's console pipeline is correspondingly **5 stages instead of 7**: the
`[4/7] Linting generated dashboard YAML` and `[5/7] Compiling YAML -> Kibana
NDJSON` stages are gone, so the remaining stages renumber to `[1/5]` … `[5/5]`.

| Surface | Status | Use it when |
|---|---|---|
| `migrate --upload` | Current default | You want a one-step migration that uploads the in-memory `native_dashboard` derived from `DashboardIR`. |
| `obs-migrate upload --artifact-dir <dashboards>` | Current default for two-step review/upload | You reviewed `native/*.native.json` and want to upload the exact persisted typed Dashboards API payload. |
| `--fetch-alerts` / `--fetch-monitors` | Deprecated compatibility aliases | Older scripts still use them. New scripts should use `--assets alerts` or `--assets all`. |

### Review and Upload

```bash
# Review migration_output/dashboards/native/*.native.json (the exact typed
# Dashboards API payloads written unconditionally by every migrate run), then
# upload the reviewed artifact directory. Deploys via PUT /api/dashboards/{id}
# by default, uploading the reviewed native/*.native.json payloads verbatim.
obs-migrate upload \
  --artifact-dir migration_output/dashboards \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY"
```

`obs-migrate upload` takes one input: `--artifact-dir <path>`, the dashboard
artifact directory a migration wrote (or directly its `native/` child). Every
`native/*.native.json` under it is sent to `PUT /api/dashboards/{id}` exactly as
persisted — no re-mapping, no second renderer. A rejection is reported as-is,
because there is nothing to silently re-derive the dashboard from.

Accepted input shapes:

- the dashboard artifact directory itself, e.g. `migration_output/dashboards`
  (it holds `native/` and `ir/`);
- the `native/` directory directly;
- any sibling child of the artifact root, which still resolves to `../native/`.

If no `native/*.native.json` is found, `upload` exits `1` and says so rather than
uploading nothing quietly.

During `obs-migrate migrate --upload`, Grafana and Datadog upload the in-memory
`native_dashboard` already derived from `DashboardIR` — the same payload shape as
the persisted `native/*.native.json` artifact — so a one-step run and a
review-then-upload run deploy identical bodies.

**Silently dropped panels (`lossy`):** `PUT /api/dashboards/{id}` answers `200`/`201`
even when Kibana could not transform some of the panels it was sent — those panels
are simply absent from the saved object, and the PUT body carries no `warnings` key
to say so. Every accepted upload therefore counts the leaf panels it sent against
the ones the response echoes back in `data.panels` (recursing one level into
sections). When fewer came back, the upload is reported with status `lossy`:

- `lossy` is a **failure**, not a warning. It never counts toward `uploaded_ok`,
  so `obs-migrate upload` exits `1` and the per-dashboard `runtime_summary.upload.status`
  in `migration_manifest.json` is `fail`. A silent partial write is worse than an
  outright rejection precisely because it looks successful.
- There is no fallback path it could be re-routed to. The PUT already wrote a
  partial dashboard, and re-rendering it through a second code path would hide the
  loss (the same reasoning as `409 conflict`).
- The dropped panels are named, not just counted. On a detected mismatch — and only
  then — one follow-up `GET /api/dashboards/{id}` retrieves Kibana's own
  `warnings[].message` ("Unable to transform panel config. Error: …") so the operator
  gets the actual validation error. Detection never depends on that GET succeeding.
- Per-panel detail (`title`, `reason`, `section`, `grid`) is reported on stderr by
  `obs-migrate upload` and `obs-migrate migrate --upload`, in the console migration
  report's `UPLOAD DATA LOSS` section, and in the JSON artifacts:
  `migration_report.json` → `summary.upload_panels_dropped` plus
  `runtime_summary.upload.dropped_panels` per dashboard. The `obs-migrate upload`
  summary additionally reports `panels_dropped` across the batch.

**Same-titled dashboards (`duplicate_id`):** `PUT /api/dashboards/{id}` is an
upsert, and the dashboard id is the title slug (`obs-migrate-<title-slug>`). Two
source dashboards sharing a title would therefore land on one id, with the second
replacing the first and reporting a routine `updated`. Instead:

- The run appends the same token that made the two dashboards' *artifact stems*
  unique to the second dashboard's id, so artifact `shared_title_dash-beta` is
  uploaded as `obs-migrate-shared-title-dash-beta`. A dashboard whose title is
  unique in the run keeps exactly the id it has always had, so previously
  uploaded copies are still addressed by it.
- Each disambiguation is printed during migration, naming both the id used and
  the plain title slug it is not, because that slug is what you would search for:
  `'Shared Title' shares its title with another dashboard in this run, so its
  Kibana dashboard id is disambiguated to 'obs-migrate-shared-title-dash-beta'
  rather than the plain title slug 'obs-migrate-shared-title'.`
- If two payloads still resolve to one id in a single upload, the second is
  **not sent** and is reported with status `duplicate_id`. Like `lossy`, it is a
  failure: it never counts toward `uploaded_ok`, so `obs-migrate upload` and
  `migrate --upload` exit `1`. Give the dashboards distinct titles, or upload
  them separately.

**Unresolvable control data views:** A control's `data_view_id` is an index
pattern (`metrics-*`) that upload rewrites to the saved-object id Kibana assigned
it. When a value cannot be resolved, the raw pattern is left in place and Kibana
renders that control as "An error occurred" — which looks like a product bug. Each
such control is now named on stderr (`control '<title>' points at data view
'<pattern>', which matches no Kibana data view id or title`) and reported in the
upload record as `unresolved_data_views`. This is a **warning**, not a failure:
the dashboard uploads and its panels work; that one control does not. A value
that is already a real saved-object id, or a data view whose title is its own id,
is a correct fallback and is not reported.

**Re-upload conflict:** The native `PUT /api/dashboards/{id}` returns `409
Conflict` if a saved object with the same ID already exists in a *different*
space — including `[DELETED]` placeholder objects left by `obs-migrate cluster
delete-dashboards`. Dashboards are a shareable saved-object type, so their ids
are cluster-global rather than space-scoped. This is reported as a terminal
failure: the `--legacy-import` `_import?overwrite=true` route was removed with
the rest of the YAML surfaces. Resolve it by removing the conflicting object, or
by uploading into the space that owns the id.

### Cluster

```bash
obs-migrate cluster list-dashboards --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
obs-migrate cluster ensure-data-views --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --data-view-patterns "metrics-*,logs-*"
obs-migrate cluster delete-dashboards --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --dashboard-ids "id1,id2"
obs-migrate cluster detect-serverless --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
```

`delete-dashboards` clears saved objects into `[DELETED]` placeholders by
overwriting each dashboard with empty content. This Serverless-safe behavior is
used for every target; use the Kibana UI if you need the placeholder saved
objects fully removed.

### Extensions

`obs-migrate extensions` prints the extension schema for a source adapter — the registry-driven query, panel, and variable extension points available for Grafana (rule packs, Python plugins), or the field-profile contract for Datadog. Use it to understand what can be customized and to scaffold a starter extension file. Pass `--template-only` to print just the starter template without the full schema, or `--template-out <path>` to write it to a file.

```bash
obs-migrate extensions --source grafana --format yaml
obs-migrate extensions --source datadog --format json
obs-migrate extensions --source grafana --format yaml --template-only
obs-migrate extensions --source grafana --format yaml --template-out custom-rule-pack.yaml
obs-migrate extensions --source datadog --format yaml --template-out custom-field-profile.yaml
```

### Schema Report

Dashboard migration writes `schema_change_report.md` and
`telemetry_contract.json` automatically. `obs-migrate schema-report` is the
advanced regeneration/combination command: it emits the same per-panel
source-to-target schema-change report
(`dashboard | panel | source_fields | target_stream | target_fields`) from one
or more existing dashboard artifact directories. It ships in the installed
wheel and needs no source checkout.

```bash
# Single source
obs-migrate schema-report \
  --artifact-dir migration_output/dashboards \
  --output schema_change_report.md

# Merge multiple sources, and also emit the telemetry producer contract JSON
obs-migrate schema-report \
  --artifact-dir grafana_output/dashboards \
  --artifact-dir datadog_output/dashboards \
  --output schema_change_report.md \
  --contract-out telemetry_contract.json
```

Each `--artifact-dir` is a per-source `dashboards/` output (containing `ir/`
and `verification_packets.json`). `--contract-out` is optional; without it only
the Markdown report is written.

### Audit Rules

`obs-migrate audit-rules` lists migrated Kibana alerting rules (those tagged
`obs-migration` or named `[migrated] ...`) and reports which are enabled. It is
**read-only by default**; pass `--disable-enabled` to disable the enabled
subset. Exit code is non-zero while enabled migrated rules remain (or
remediation fails).

```bash
obs-migrate audit-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
obs-migrate audit-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --disable-enabled
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
obs-migrate delete-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

# Confirm: delete the migrated rules.
obs-migrate delete-rules --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" --confirm --max-pages 50
```

### Verify Alert Rules

`obs-migrate verify-alert-rules` is a self-cleaning round trip: it creates
the emitted alert-rule payloads in Kibana **disabled**, confirms none came back
enabled, then deletes them (unless `--keep-rules`). `--comparison` is required
and points at a comparison report written by a prior alert-capable migration
(for example `<output-dir>/alerts/alert_comparison_results.json` for Grafana, or
`<output-dir>/alerts/monitor_comparison_results.json` for Datadog). Repeat
`--comparison` to verify multiple reports.

```bash
obs-migrate verify-alert-rules \
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
Elasticsearch so the migrated panels light up. It ships in the installed wheel,
honors the shared `--ca-cert`/`--insecure` TLS flags, and is **ES-only** (it
does not touch Kibana); pair it with `remove-sample-data` to clean up
afterward. Exit code is `2` when Elasticsearch is unreachable or inputs are
invalid, `1` on ingest errors, and `0` otherwise.

It is **fail-closed on empty discovery**: seeding exits `2` when the contract
discovers no telemetry requirements at all, and also when the artifacts declare
dashboard controls (`mapping.controls` in `native/*.native.json`) but the
contract produced zero control fields. The second case used to seed
"successfully" while omitting every field the dashboard filters on, so the
seeded documents matched no control selection and every filtered panel rendered
empty.

```bash
# Seed synthetic data for a single migrated artifact directory.
obs-migrate seed-sample-data \
  --artifact-dir migration_output/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

# Merge multiple sources and cap cardinality; honors --ca-cert / --insecure.
obs-migrate seed-sample-data \
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
metric kinds. Progress (batch-flush counts, bulk-retry/split notices) prints to
stderr as the ingest runs; pass `--quiet` to suppress it.

#### Seeding more than one source at once

When validating Grafana and Datadog dashboards against the same cluster, keep
their metric streams source-specific to avoid mapping conflicts between
Prometheus-style labels and Datadog/ECS field objects. A typical shared
validation target uses:

- Grafana Prometheus-style dashboards: `metrics-prometheus-default`
- Datadog dashboards: `metrics-datadog-default`
- Shared logs: `logs-generic-default`

Bind each migration to its own stream, then seed both artifact directories in
one pass:

```bash
set -a && source serverless_creds.env && set +a

obs-migrate cluster ensure-data-views \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY" \
  --data-view-patterns "metrics-prometheus-default,metrics-datadog-default,logs-generic-default"

obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir grafana_assets \
  --output-dir grafana_output \
  --assets dashboards \
  --data-view metrics-prometheus-default \
  --esql-index metrics-prometheus-default \
  --logs-index logs-generic-default

obs-migrate migrate \
  --source datadog \
  --input-mode files \
  --input-dir datadog_assets/dashboards \
  --output-dir datadog_output \
  --assets dashboards \
  --data-view metrics-datadog-default \
  --logs-index logs-generic-default

obs-migrate seed-sample-data \
  --artifact-dir grafana_output/dashboards \
  --artifact-dir datadog_output/dashboards \
  --data-hours 168 --interval-sec 3600 \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"
```

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
obs-migrate remove-sample-data \
  --artifact-dir migration_output/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

# Confirm: delete the seeder-owned streams and templates.
obs-migrate remove-sample-data \
  --artifact-dir migration_output/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --confirm
```

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
obs-migrate compare \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"

# Repeat --artifact-dir to merge multiple runs; honors --ca-cert / --insecure.
obs-migrate compare \
  --artifact-dir grafana_output/dashboards \
  --artifact-dir datadog_output/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --index "metrics-*" \
  --step-seconds 300 \
  --window-minutes 60 \
  --report-out comparison_report.json
```

Progress (panel counts as they're compared, the report path) prints to stderr
as the run progresses; pass `--quiet` to suppress it.

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

### Verify (one-command package-native scorecard)

`obs-migrate verify` is a thin orchestrator that runs the **package-native**
correctness gates over an already-migrated artifact dir and prints ONE
consolidated scorecard. It exists so users don't have to assemble the
individual gates by hand. It is read-only on the cluster.

```bash
obs-migrate verify \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --report-out verify_report.json

# Add numeric parity (runs obs-migrate compare in-process over the same dir):
obs-migrate verify \
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
always lists the deeper gates it does NOT run, with the exact command for each.
Those gates need a repo checkout — they are documented in
[`contributing/dev-commands.md`](contributing/dev-commands.md):

- `verifier.dashboards_api` — Kibana typed UI-contract validation (accessor
  wiring, column refs).
- the render audit — the only gate that catches Lens accessor / "invalid
  column" / empty-state render failures that ES|QL execution and the schema
  gate miss.
- `obs-migrate verify-panels` — the full 5-tier panel verifier.

Exit code is `2` when Elasticsearch is unreachable or inputs are invalid
(missing artifact dir, missing credentials, no emitted queries), `1` on any
`real_bug` or compare `FAIL`/`ERROR`, and `0` otherwise (`data_gap`/`other`
are warnings, not failures).

### Verify Panels (5-tier panel verifier)

`obs-migrate verify-panels` is the repo-oriented 5-tier panel verifier wrapper
(source query → translator → IR export → dashboard as stored in Kibana →
cluster saved object → live `_query`). It delegates to verifier code that only
exists in a repo checkout, so it is intended for development / CI, not as a
substitute for `obs-migrate verify` on an installed wheel. Both sources are
covered: the translator tier reads Grafana's `esql` and Datadog's `esql_query`
from `migration_report.json`.

```bash
obs-migrate verify-panels \
  --migration-out <output-dir>/dashboards \
  --output panel_verify_report.json \
  --kibana-url "$KIBANA_ENDPOINT" \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --dashboard-id "<uploaded-dashboard-id>"
```

`--migration-out` and `--output` are required. T3 (the stored dashboard) needs
`--kibana-url`; T4/T5 (cluster + live query) additionally need `--es-url`,
`--api-key`, and `--dashboard-id`. This wrapper does **not** expose
`--ca-cert` / `--insecure` today.

**T3 comes from the Dashboards API.** With `--kibana-url`, T3 is read from
`GET /api/dashboards/{id}` for every dashboard the run wrote a `native/`
artifact for — the dashboard as Kibana actually stored it, rather than a
compiler artifact. The ids are deterministic (`obs-migrate-<title-slug>`) and
already recorded in `native/index.json`, so none has to be guessed. Panel
records then also carry Kibana's own panel UUID (`stored.panel_id`), which is
what the render audit and visual-regression harnesses address panels by; the
IR's `panel_id` is a *migration* id and cannot substitute for it. The legacy
`compiled/<slug>/compiled_dashboards.ndjson` reader is retained so it still
works when pointed at an *archived* artifact directory from an older release; no
current migration produces that file.

**Absent tiers are reported, not inferred.** Without `--kibana-url` and without
`compiled/`, T3 is reported unavailable with a reason on every panel record;
the `T2=T3` / `T3=T4` axes are skipped rather than reporting one "right side
empty" finding per panel, and the panel is *not* labelled `NOT_UPLOADED` —
that verdict is reserved for a panel genuinely missing from a dashboard that
*was* read back. The same rule applies to T4 when no cluster saved object was
requested. Every report carries a `tier_population` block (also printed in the
console and Markdown summaries) giving the per-tier panel count, T3's
provenance, and how many records carry a real Kibana UUID — read it alongside
the verdict counts, because a verdict distribution alone cannot distinguish
"checked and agreed" from "never checked".

`--migration-out` may hold one dashboard or a whole run's worth. Every tier is
joined per dashboard (report `uid`, else `title`), never by panel
title alone — titles repeat across dashboards, and a title-only join compares
one dashboard's panel against another's. When a dashboard cannot be matched to
an artifact, that tier is reported empty with a note on the panel record
instead of being filled from a neighbouring dashboard.

### Verify Visual (pixel-diff Grafana vs Kibana)

`obs-migrate verify-visual` pixel-diffs a migrated Kibana dashboard against its
source Grafana dashboard (agent-browser screenshots + per-panel / median / p95
diff scores). Like `verify-panels` it is repo-oriented: it requires the local
Grafana stack from a checkout (and optionally a bootstrapped agent-browser
state file for Kibana SAML).

```bash
obs-migrate verify-visual \
  --migration-out <output-dir>/dashboards \
  --grafana-uid "<uid>" \
  --grafana-slug "<slug>" \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-dash-id "<uploaded-dashboard-id>" \
  --output-dir visual_diff_out \
  --report visual_diff_report.json
```

Like `verify-panels`, this wrapper does **not** expose `--ca-cert` /
`--insecure` today.

It exits `2` when either side discovers zero panels — an empty/absent
`--migration-out` `ir/` directory, or a `--grafana-uid` with no panels. A
zero-panel run previously reported `captured=0 median=0.0000` and exited `0`,
which is indistinguishable from a perfect pixel match.

## Dedicated Source CLIs

Dedicated entry points (`grafana-migrate`, `datadog-migrate`) are thin wrappers around `python -m observability_migration.adapters.source.grafana.cli` and `python -m observability_migration.adapters.source.datadog.cli`.
They accept the same `--input-mode {files,api}` extraction selector as unified
`obs-migrate migrate`. The older dedicated-CLI spelling `--source files|api`
is still accepted as a compatibility alias; if both are provided, they must
match. Both dedicated CLIs also accept the same `--select-*` metadata selection
flags described under [Migrate](#migrate) (with the same per-source availability
and graceful-degradation behavior). Prefer the unified `obs-migrate migrate`
spelling in docs, runbooks, and user workflows; use dedicated CLIs only for
compatibility or adapter-local debugging.

### CLI parity (unified vs dedicated)

The three migrate surfaces are **consistent on the shared migration contract**,
not byte-identical. Intentional differences:

| Topic | Unified `obs-migrate migrate` | `grafana-migrate` | `datadog-migrate` |
|---|---|---|---|
| Shared flags | `--assets`, `--input-mode`, `--data-view`, `--logs-index`, `--field-profile`, `--metric-map-file`, `--translation-mode`, `--ca-cert`/`--insecure`, smoke/upload/validate/select-* | same shared set | same shared set |
| `--esql-index` | Grafana-only (forwarded when set) | present | absent (metrics target comes from `--data-view` / `--field-profile`) |
| Deprecated alert alias | `--fetch-alerts` (both sources) | `--fetch-alerts` | `--fetch-monitors` |
| Kibana space | `--space-id` | `--shadow-space` (unified maps `--space-id` → `--shadow-space`) | `--space-id` |
| Cluster ops | `obs-migrate cluster …` | still exposes `--list-dashboards` / `--ensure-data-views` / `--delete-dashboards` | same dedicated cluster flags |
| `--data-view` default | empty → source adapter default | `metrics-*` | unset → active `--field-profile` metric index |
| Grafana-only extras | connection flags (`--grafana-url`/token/…) forwarded | local-AI / Loki / Prometheus / review-explanations / validate-workers | n/a |
| Datadog-only extras | `--env-file`, `--dashboard-ids`, `--monitor-ids`, `--monitor-query`, `--source-execution` | n/a | same on dedicated CLI |

When a shared flag is omitted on unified migrate, the dedicated adapter keeps its
own default (Grafana still binds `metrics-*`; Datadog still derives the metric
index from the field profile). Setting the flag explicitly on any of the three
CLIs has the same meaning.

### Grafana

Use the shared asset contract above for `--assets` and the deprecated
`--fetch-alerts` alias. For Grafana-specific runtime details, see [Grafana
source adapter](sources/grafana.md). For existing-OTEL metric renames, see
[Reusing existing OTEL metrics with `--metric-map-file`](#reusing-existing-otel-metrics-with---metric-map-file).

```bash
# Files: dashboards only (native PROMQL is the default)
obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --assets dashboards \
  --field-profile otel \
  --data-view "metrics-*" \
  --esql-index "metrics-*"

# Live Grafana API: alerts only
obs-migrate migrate \
  --source grafana \
  --input-mode api \
  --grafana-url "$GRAFANA_URL" \
  --grafana-token "$GRAFANA_TOKEN" \
  --output-dir migration_output \
  --assets alerts

# Files: dashboards + alerts + integrated smoke
obs-migrate migrate \
  --source grafana \
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
still write `dashboards/native/*.native.json`, `dashboards/ir/*.ir.json`
and the normal dashboard report artifacts (no NDJSON is produced).
Alerts-only runs (`--assets alerts`) skip dashboard emission and
write alert artifacts under `<output-dir>/alerts`. For pure source-side alert
extraction, set `KIBANA_URL=` in the shell to suppress the default local Kibana
alerting preflight.

### Datadog

Use the shared asset contract above for `--assets` and the deprecated
`--fetch-monitors` alias. For Datadog-specific runtime details, see [Datadog
source adapter](sources/datadog.md). For existing-OTEL metric renames, see
[Reusing existing OTEL metrics with `--metric-map-file`](#reusing-existing-otel-metrics-with---metric-map-file).

```bash
# Files: dashboards only
obs-migrate migrate \
  --source datadog \
  --input-mode files \
  --input-dir infra/datadog/dashboards \
  --output-dir datadog_migration_output \
  --assets dashboards \
  --field-profile otel \
  --data-view "metrics-*"

# Files: alerts only (place monitor JSON under <input-dir>/monitors/)
obs-migrate migrate \
  --source datadog \
  --input-mode files \
  --input-dir infra/datadog \
  --output-dir datadog_migration_output \
  --assets alerts \
  --field-profile otel \
  --data-view "metrics-*"

# Files: dashboards + alerts
obs-migrate migrate \
  --source datadog \
  --input-mode files \
  --input-dir infra/datadog \
  --output-dir datadog_migration_output \
  --assets all \
  --field-profile otel \
  --data-view "metrics-*"

# Live Datadog API with explicit dashboard scoping
obs-migrate migrate \
  --source datadog \
  --input-mode api \
  --env-file datadog_creds.env \
  --dashboard-ids abc-def-123 \
  --output-dir datadog_migration_output \
  --assets dashboards \
  --data-view "metrics-*"

# Live Datadog API: alerts only
obs-migrate migrate \
  --source datadog \
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
Dashboard-capable runs (`--assets dashboards` or `--assets all`) write
`dashboards/native/*.native.json` and `dashboards/ir/*.ir.json` (both derived
from `DashboardIR`) plus the standard dashboard run reports. No dashboard YAML
and no NDJSON is produced. Upload deploys through Kibana's typed Dashboards API:
migrate `--upload` sends the in-memory `native_dashboard` from `DashboardIR`,
while standalone `obs-migrate upload` sends the persisted native review
artifacts. A rejection is terminal — there is no second renderer to fall back
to. Alerts-only runs (`--assets alerts`) skip dashboard native/IR artifacts,
write monitor artifacts under `<output-dir>/alerts`, and still emit the root
`run_summary.json`. Use the dedicated Datadog CLI when you need explicit
dashboard scoping via `--dashboard-ids` before any Elastic target exists.

## Tested Alert Upload Flow

This sequence was re-run against the Serverless target using the curated example corpus.
Create `serverless_creds.env` from `serverless_creds.env.example` before
running the commands below.

### Preferred: one unified command

```bash
set -a && source serverless_creds.env && set +a
obs-migrate migrate \
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
obs-migrate audit-rules \
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

An older multi-step variant of this flow still exists for regenerating the
curated example artifacts from a repo checkout; see
[`contributing/dev-commands.md`](contributing/dev-commands.md).
