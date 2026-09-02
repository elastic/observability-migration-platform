# Contributor / CI Command Reference

Runnable commands that need a **repo checkout**: verification and benchmark
gates, `scripts/` lab lifecycle, repo-oriented validation CLIs, and pytest.

Companion docs:

- [`../command-contract.md`](../command-contract.md) — the operator surface: everything you
  can run from an installed `elastic-observability-migration` wheel.
- [`../testing.md`](../testing.md) — why each gate exists, what it proves, and the
  confidence pyramid it belongs to.

Everything below assumes a checkout with the locked dev environment
(`make sync`), so examples use `.venv/bin/...`, `PYTHONPATH=parity-rig`,
`bash scripts/...`, and `docker compose`. An operator who only installed the
CLI never needs any of it.

## Dev Environment Setup

Prefer `make sync` (the locked `uv` environment used by CI):

```bash
make sync
```

The equivalent direct invocations, and the local git hooks:

```bash
uv sync --locked --all-extras
uv run obs-migrate doctor
```

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[all,dev]"
.venv/bin/pre-commit install
.venv/bin/pre-commit run --all-files
```

`.venv/bin/pip install -e ".[datadog]"` installs just the Datadog client extra
when you do not want the full `[all]` set. The `[kibana]` extra still pins the
`kb-dashboard-*` tools (Python 3.12+), but **no command invokes them any more**:
the dashboard-YAML compile/lint path was removed, and `obs-migrate doctor` no
longer treats a missing `kb-dashboard-*` tool as a blocking gap.

## Verification And Benchmark Gates

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
| `render_audit_driver` | uploaded dashboard + headless browser (+ `--es-url` field caps) | Each panel actually renders in real Kibana (no Lens "invalid column"/error embeddable). A panel whose error names only columns field caps confirm absent is `field_gap` (warn); an empty panel whose metric column is confirmed absent is `data_gap` (warn). Field caps come from the index each panel's own ES\|QL `FROM` names, so a `FROM logs-*` panel is never judged against `metrics-*`. Per-panel metadata comes from the audited dashboard only, never the whole run. Without field caps absence is unconfirmable: an error stays `render_error` and an empty panel stays `unexpected_empty` | no `render_error` |
| `scripts/run_interaction_audit_local.sh` | uploaded dashboard + Playwright + scenario manifest | Adapter-specific control state plus affected/unaffected panel request evidence | no unexpected `fail` |
| `verifier.mutations` | `migration_report.json` | The invariant verifier catches deliberate corruptions | all mutations pass |
| `verifier.lens_fixtures` | LensConfigBuilder fixture JSON | Authoritative Lens-as-code fixtures exist for required chart families | coverage complete |
| `verifier.corpus_manifest` | Grafana catalog + datasource map | Larger benchmark corpus is pinned/stratified/reproducible | committed manifest |

Every gate that measures a *percentage* of discovered panels is fail-closed on
an empty corpus: zero discovered panels exits non-zero instead of reporting a
vacuous 0%/`captured=0` pass. This applies to `verifier.visual_regression`
(exits `2`; also raised via `obs-migrate verify-visual`),
`scripts/validate_panel_queries.py`, and
`scripts/validate_panels_from_artifacts.py`
(both exit `1` and name the directory/globs they searched). Treat a
"nothing to measure" exit as a broken input — an un-run migration or a wrong
`--migration-out` / `E2E_ROOT` — not as a gate failure to be suppressed.

Offline coverage gates (no cluster, every PR) live in the unit suite, not
`verifier/`: `tests/core/coverage/test_supported_types.py` cross-checks the
supported-type registry (`observability_migration/core/coverage/supported_types.py`)
against the code's routing both ways; `tests/test_panel_matrix.py` (Grafana) and
`tests/test_datadog_panel_matrix.py` (Datadog) lint every panel/widget type
through the real pipeline; `tests/test_canary.py` validates the registry-driven
kitchen-sink canary against `docs/dashboards/schema.json`. The fidelity ratchet
runs as an e2e gate (`tests/e2e/test_fidelity_ratchet.py`) against committed
baselines (`parity-rig/benchmark/fidelity_baseline_{grafana,datadog}.json`).
Grafana curated-pack field-profile portability is gated by
`tests/test_field_profile_portability.py` (leakage linter + per-pack migrate)
and `scripts/run_cross_profile_corpus.py` (community corpus × every
`--field-profile`; see `docs/contributing/dev-commands.md`).

Examples:

```bash
# Runtime ES|QL oracle: catches invalid emitted ES|QL that static checks miss.
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

# Field-profile portability: migrate DIR under every --field-profile, fail on
# leaked namespaces (e.g. labels.* under otel) or a feasibility drop vs native.
# Optional --baseline-native DIR of saved prometheus_native *.native.json goldens.
PYTHONPATH=. .venv/bin/python scripts/run_cross_profile_corpus.py \
  --input-dir /tmp/community \
  --index 'metrics-*'

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

`live_validate` reads identifier-control defaults from each panel's QueryIR, so
queries containing `??field` are executed with the same selected field as the
migrated dashboard. Validation deduplication includes those defaults; two
otherwise identical queries with different selected fields are both checked.

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

# ALWAYS pass --es-url (+ --es-api-key) on a partially-seeded cluster. Field caps
# are the only evidence that lets the audit downgrade a panel whose error names
# absent columns to `field_gap` (warn), or an empty panel whose metric column is
# absent to `data_gap` (warn); without them absence is unconfirmable, so such an
# error stays `render_error` (fail) and such an empty stays `unexpected_empty` --
# by design, so a missing-evidence run cannot silently pass. This applies to the
# --elements section too, which shares the same classifier.
#
# --es-index is only the FALLBACK for panels whose query names no index: every
# panel that does is resolved against the index its own ES|QL `FROM` names (one
# cached `_field_caps` call per distinct index). You do NOT need to hand-union
# patterns; a dashboard mixing `metrics-*` and `FROM logs-*` panels resolves each
# against its own.
#
# --migration-out may point at a WHOLE run: the audit narrows the report to the
# dashboard --dashboard-id names before reading any panel list, so one
# dashboard's DOM is never segmented by another dashboard's panel titles (that
# join attributed a Docker error chunk to a Celery table and judged it against
# logs-*). The dashboard is matched on its recorded id (native/index.json), its
# uid/title, or an id that merely extends one of those; a single-dashboard
# --migration-out always matches. A dashboard the report cannot identify is
# reported as "per-panel attribution unavailable" on stderr AND in
# render.reasons, with "panels": [] -- it never falls back to the run's full
# title set, and whole-dashboard error markers still hard-fail.
#
# Within the dashboard, titles are matched longest-first and a hit inside another
# panel's title text is rejected, so a title that is a strict prefix of a sibling
# ("Running containers by image" vs "... (widget 27)", which the Datadog
# duplicate-title disambiguator produces by construction) cannot take the
# sibling's offset and a zero-length chunk. Read "panel title(s) did not render"
# in render.reasons as a real signal: that panel drew no title of its own, and it
# gets NO panel record rather than a phantom "rendered" one.
.venv/bin/python -m observability_migration.targets.kibana.render_audit_driver \
  --kibana-url "$KIBANA_ENDPOINT" --dashboard-id "<id>" \
  --migration-out migration_output/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" --es-index "metrics-*" \
  --time-from now-24h --time-to now --elements

# Focusing the right tab in a live agent-browser session (--agent-browser):
# bootstrap.sh logs in once and keeps a persistent profile
# (~/.agent-browser/profiles/obs-migrate-verifier) + saved state. A live session
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

Dashboard interaction audit (control selection → affected queries). Requires
Stack 9.5+. Offline unit coverage is `make test-interactions`; the live suite
is nightly/manual only (`.github/workflows/dashboard-interaction-audit.yml`).

```bash
# One-time Chromium install for Playwright (optional; not part of default unit tests).
make setup-browser

# Offline interaction contracts / scenario planning / fake-browser tests.
make test-interactions

# Live local suite (caller starts the stack). Default scenarios:
# synthetic-controls,redis-11835,k8s-views-global
STACK_VERSION=9.5.0-SNAPSHOT docker compose -f parity-rig/docker-compose.render-audit.yml up -d --wait
STACK_VERSION=9.5.0-SNAPSHOT make interaction-audit-local
# Or scope one dashboard:
SCENARIOS=redis-11835 bash scripts/run_interaction_audit_local.sh
docker compose -f parity-rig/docker-compose.render-audit.yml down -v
```

Artifacts land under `ARTIFACT_ROOT` (default
`./interaction-audit-artifacts/<scenario>/<run-id>/`), including `report.json`
and optional Playwright traces/screenshots. Exit code is `1` only when the
report status is `fail`; expected `migration_gap` / decorative controls warn
with exit `0`. Useful env knobs: `FULL=1` (denser nightly seed),
`SKIP_MIGRATE=1` + `KEEP_WORK=1` + `WORK_DIR=...` (browser-only re-run),
`SCREENSHOTS=on-fail|always|never`, `LIVE_VALIDATE=0` (lint only after seed).

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

`verifier.cli` exits non-zero when T1 (translator ES|QL) is populated for **zero**
panels while the report describes some. An empty T1 short-circuits every
comparison to `SKIP`, so that run reports 0 drift on all five axes whatever the
translator emitted — indistinguishable from a perfect run, and exactly the state
`07e5829` fixed one cause of. Pass `--allow-empty-t1` only for a source that
genuinely translated nothing.

The repo-oriented `obs-migrate verify-panels` and `obs-migrate verify-visual`
wrappers also delegate to `parity-rig/verifier`. Operator docs only point here;
use the invocations below from a synced checkout.

### `obs-migrate verify-panels` (5-tier panel verifier)

Requires a repo checkout (verifier code under `parity-rig/`). Not a substitute
for `obs-migrate verify` on an installed wheel.

```bash
obs-migrate verify-panels \
  --migration-out <output-dir>/dashboards \
  --output panel_verify_report.json \
  --kibana-url "$KIBANA_ENDPOINT" \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --dashboard-id "<uploaded-dashboard-id>"
```

`--migration-out` and `--output` are required. T3 needs `--kibana-url`; T4/T5
also need `--es-url`, `--api-key`, and `--dashboard-id`. This wrapper does
**not** expose `--ca-cert` / `--insecure`. With `--kibana-url`, T3 is read from
`GET /api/dashboards/{id}` for every dashboard that has a `native/` artifact.
A vacuous T1 (zero panels with translator output) exits `1`. See
`verifier.cli` above for `--allow-empty-t1` (checkout-only).

### `obs-migrate verify-visual` (pixel-diff Grafana vs Kibana)

Requires the local Grafana stack from a checkout (and optionally a bootstrapped
agent-browser state file for Kibana SAML).

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

Exits `2` when either side discovers zero panels. Does **not** expose
`--ca-cert` / `--insecure`.

## Validation / Verification CLIs

```bash
# Scope validation to just the dashboards a migration uploaded (recommended).
# --dashboards-from reads a migration detailed report or a prior smoke report and
# validates only those dashboards — by uploaded saved-object ID when the artifact
# carries one, otherwise by title. This keeps runs practical on busy spaces (#198).
.venv/bin/grafana-validate-uploaded \
  --kibana-url "$KIBANA_ENDPOINT" \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --dashboards-from migration_output/dashboards/migration_report.json \
  --output upload_smoke_report.json

.venv/bin/grafana-generate-corpus --help
```

Scope flags are also available for ad-hoc runs: `--dashboard-id` /
`--dashboard-title` (repeatable) restrict to specific dashboards. With no scope
flags the validator inspects **every** dashboard in the space, prints an
explicit `WARNING` that it is doing so, and skips `[DELETED]` placeholder
dashboards (pass `--include-deleted` to validate those too). The run prints
per-dashboard `[i/N]` progress before writing the final report.

## Legacy Repo-Checkout Alert Flow

The operator path is the single unified command documented under
[Tested Alert Upload Flow](../command-contract.md#tested-alert-upload-flow).
This multi-step flow remains supported in a source checkout when you want to
regenerate the curated example artifacts without touching dashboards, or when
you want the destructive round-trip `verify_alert_rule_uploads.py` path:

```bash
.venv/bin/python scripts/generate_alert_support_report.py

set -a && source serverless_creds.env && set +a
.venv/bin/obs-migrate upload \
  --artifact-dir examples/alerting/generated/grafana/dashboards \
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

Two caveats on the `upload` step above. The `--artifact-format yaml` flag it used
to pass was **removed** — `obs-migrate upload` now takes only `--artifact-dir`,
pointed at a dashboard artifact directory holding `native/`. And
`generate_alert_support_report.py` runs both source CLIs with `--assets alerts`,
which skips dashboard migration, so it writes no
`examples/alerting/generated/grafana/dashboards/native/` for `upload` to read.
Migrate `examples/alerting/grafana/legacy_dashboard_alert.json` with
`--assets all` first if you need that dashboard in Kibana; otherwise `upload`
exits `1` with `no_native_artifacts_found`.

`scripts/audit_migrated_rules.py` and `scripts/verify_alert_rule_uploads.py` are
the repo-checkout equivalents of the packaged `obs-migrate audit-rules` and
`obs-migrate verify-alert-rules` subcommands.

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

### Curated Pack Pin Verification

`observability_migration/adapters/source/grafana/curated_packs/registry.yaml`
pins each curated pack's exact grafana.com `gnet_revision` and canonical-JSON
`dashboard_sha256` — a maintainer provenance check (issue #350), not a
migration-time gate. Re-verify after touching a pack or its registry entry, or
before re-pinning to a newer revision (requires network; mirrors
`scripts/fetch_community_corpus.py`'s `canonical_sha256` pattern; not part of
`make test`):

```bash
.venv/bin/python scripts/verify_curated_pack_pins.py
.venv/bin/python scripts/verify_curated_pack_pins.py --gnet-id 1860
```

`tests/test_verify_curated_pack_pins.py` covers the hashing/parsing logic
offline (mocked download); `tests/test_curated_packs.py` guards the registry's
shape (required fields, unique ids/names/paths/hashes, hex-digest format).

### Schema / Lint / Layout

```bash
bash scripts/generate_dashboard_schema.sh
make check-native-schema
# Intentionally bump the committed OpenAPI pin:
make refresh-native-schema
# Optional upstream override when refreshing from a different bundle:
KIBANA_DASHBOARDS_API_SCHEMA_URL=<kibana-full-openapi.yaml> make refresh-native-schema
```

`generate_dashboard_schema.sh` refreshes the `kb-dashboard-core` dashboard JSON
schema (`docs/dashboards/schema.json`), which is still the reference for the
internal `DashboardIR.to_yaml_dict()` dict shape even though no YAML file is
ever written. The native upload schema authority is the committed OpenAPI pin
`docs/dashboards/kibana_dashboards_api.openapi.yaml`. `make check-native-schema`
validates that pin offline (`--require-full-schema`); `make refresh-native-schema`
re-fetches Elastic's hosted Dashboards API bundle and rewrites the pin for an
intentional bump (the standard Kibana OpenAPI bundle may still contain
redirect-only shells).

**The dashboard-YAML lint and compiled-layout stages were removed**, together
with `obs-migrate compile` and `migrate --compile` / `--legacy-import`. The
previous recipe here (render YAML with `compile.write_dashboard_yaml`, then run
`lint.lint_dashboard_yaml` / `layout.validate_compiled_layout`) no longer works:
`write_dashboard_yaml` is gone, and no migration writes a `yaml/` or `compiled/`
directory to point the other two at. `lint.py` and `layout.py` themselves are
kept as library code, but nothing user-facing calls
`lint_dashboard_yaml` / `validate_compiled_layout`.

The check that survived is the `?param`/`??param` binding gate (issues #131 /
#282). It reads the IR export instead of YAML and is what the interaction audit
runs:

```python
from observability_migration.targets.kibana.interaction_audit_local import (
    lint_migration_artifacts,
)
lint_migration_artifacts(Path("migration_output/dashboards"))
```

### Data Setup

For new use, prefer the package-native
[`obs-migrate seed-sample-data`](../command-contract.md#seed-sample-data) /
[`obs-migrate remove-sample-data`](../command-contract.md#remove-sample-data)
subcommands, which ship in the installed wheel and honor the shared
`--ca-cert`/`--insecure` TLS flags. `scripts/setup_telemetry_data.py` is now a
thin shim over the same library, kept for existing automation:

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

Keep the per-source stream layout the operator doc recommends under
[Seeding more than one source at once](../command-contract.md#seeding-more-than-one-source-at-once):
`metrics-prometheus-default` for Grafana, `metrics-datadog-default` for
Datadog, `logs-generic-default` shared. Mixing Prometheus-style labels and
Datadog/ECS field objects in one stream produces mapping conflicts.

The common setup script discovers dashboard IR artifacts (`ir/*.ir.json`) and
verification packets from each artifact root. Useful flags:

| Flag | Meaning |
|---|---|
| `--data-hours` | Dense recent window of synthetic data (hours). Defaults to 2. Falls back to `DATA_HOURS` env. When a dashboard contract's `minimum_lookback` is longer (for example week-over-week panels that query `NOW() - 14 days`), the seeder also emits sparse hourly points across that older span so historical windows are non-empty without exploding document count at the dense interval. |
| `--interval-sec` | Seconds between samples. Defaults to 60. Falls back to `INTERVAL_SEC` env. |
| `--batch-docs` | Documents per bulk request. Defaults to 5000. Falls back to `BATCH_DOC_LIMIT` env. |
| `--max-combinations` | Maximum dimension combinations per stream per timestamp. Defaults to 12. Falls back to `MAX_COMBINATIONS` env. Lower this for very high-cardinality contracts. |
| `--no-recreate` | Skip all index template and data stream operations. Use when the streams already exist with the desired mappings and you only want to ingest more synthetic documents. |

Dashboard migration writes `schema_change_report.md` and
`telemetry_contract.json` automatically. To regenerate schema changes from
source queries to target fields, or to combine several source outputs, prefer
the package-native
[`obs-migrate schema-report`](../command-contract.md#schema-report) subcommand.
The equivalent repo-checkout script is:

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
