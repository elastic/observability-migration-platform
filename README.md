# Observability Migration Platform

[![Quality](https://github.com/elastic/mig-to-kbn/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/elastic/mig-to-kbn/actions/workflows/tests.yml)
[![License & SBOM](https://github.com/elastic/mig-to-kbn/actions/workflows/license-check.yml/badge.svg?branch=main)](https://github.com/elastic/mig-to-kbn/actions/workflows/license-check.yml)
[![License: Elastic-2.0](https://img.shields.io/badge/license-Elastic%20License%202.0-005571)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)

Source-agnostic migration tooling for moving observability assets — Grafana
and Datadog dashboards, alerts, and monitors — into Kibana. The installable
CLI is `obs-migrate`; the Python package is `observability_migration`.

## What This Is

A pipeline that takes dashboards/alerts from your existing observability
source (Grafana files or live API; Datadog files or live API), translates
them into Kibana Lens YAML — dashboard panels expressed declaratively in
YAML, then compiled to importable NDJSON — and uploads them to a target
Kibana cluster (Elastic Serverless or Stack), with linting, layout
validation, and post-upload smoke checks built in.

In scope:

- Grafana dashboard + alert migration (PromQL, ES|QL, Loki LogQL queries)
- Datadog dashboard + monitor migration (metric, log, query-value, etc.)
- Shared Kibana compile / upload / validation / smoke workflows
- "Degrade gracefully" handling: unsupported translations are surfaced as
  manual-review markers rather than silently dropped

Out of scope (today): non-dashboard / non-alert assets, fully automated
remediation of unsupported queries, and arbitrary custom Kibana extensions.

## Status

Pre-1.0 (`0.1.0`). Actively developed. CLI surface and emitted YAML schema
may change between releases — pin a tag if you build automation on top.

## Getting Help

- [Open an issue](https://github.com/elastic/mig-to-kbn/issues) for bugs
  and feature requests.
- See [`SUPPORT.md`](SUPPORT.md) for what context to include in a report.
- Security issues: see [`SECURITY.md`](SECURITY.md). Do not file these as
  public issues.

## Quick Start

Requires Python 3.11 or newer. If `python3` resolves to an older
interpreter on your machine, create `.venv` with an explicit 3.11+
executable instead.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[all]"
```

The `[all]` extra installs both source adapters (`grafana`, `datadog`) plus
dev tooling. Use `.[grafana]` or `.[datadog]` if you only need one source,
or `pip install -e .` for a minimal install with just core deps.

Commands that lint or compile dashboards also require `uvx` on `PATH`.
`uvx` ships with [`uv`](https://docs.astral.sh/uv/) — install via
`pip install uv`, `brew install uv`, or follow the `uv` install docs.

`python -m observability_migration` is also available if you prefer module
execution over the installed `obs-migrate` entry point.

## Compatibility

- **Python**: 3.11+
- **Kibana**: tested against Elastic Serverless and ES|QL-capable Stack
  Kibana. Serverless API surface caveats (saved-object endpoints, deletion
  workarounds) are handled by the Kibana target adapter — see
  [`docs/targets/kibana.md`](docs/targets/kibana.md) for the
  Serverless-specific compatibility matrix.
- **Grafana source**: dashboard JSON v1 schema; alerts via the unified
  alerting API.
- **Datadog source**: dashboard JSON and monitors via the public Datadog
  API.

## Common Commands

The examples below use the canonical credential variables
`ELASTICSEARCH_ENDPOINT`, `KIBANA_ENDPOINT`, and `KEY` (a single Elastic
API key valid for both Kibana and Elasticsearch). Copy
`serverless_creds.env.example` to `serverless_creds.env`, fill in the
values, then load them into the shell with:

```bash
set -a && source serverless_creds.env && set +a
```

The sample dashboards under `infra/grafana/dashboards`,
`infra/datadog/dashboards`, and `examples/alerting/` ship with the repo
and are useful for validating your setup before pointing at real data.

### Migrate dashboards

```bash
# Grafana (files) → Kibana, lint + compile + upload.
.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir infra/grafana/dashboards \
  --output-dir migration_output \
  --native-promql \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY" \
  --upload

# Datadog (files) → Kibana, with a custom field profile.
.venv/bin/obs-migrate migrate \
  --source datadog \
  --input-mode files \
  --input-dir infra/datadog/dashboards \
  --output-dir migration_output \
  --field-profile examples/datadog-field-profile.example.yaml \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY" \
  --upload
```

### Migrate dashboards and alerts in one command

Use `--assets all --create-alert-rules` to create the emitted Kibana rules
alongside the dashboard upload. Rules are created **disabled** and tagged
`obs-migration` so you can review them before enabling. In Kibana,
filter by the `obs-migration` tag in **Stack Management → Rules** to find
them, then enable in bulk once you've reviewed them.

```bash
.venv/bin/obs-migrate migrate \
  --source grafana \
  --assets all \
  --input-mode files \
  --input-dir examples/alerting/grafana \
  --output-dir alert_migration_output \
  --native-promql \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY" \
  --upload \
  --create-alert-rules
```

A per-run summary is written to `<output-dir>/alerts/alert_rule_upload_results.json`
(Grafana) or `<output-dir>/alerts/monitor_rule_upload_results.json`
(Datadog).

### Compile or upload YAML directly

```bash
# Compile dashboard YAML to NDJSON locally.
.venv/bin/obs-migrate compile \
  --yaml-dir migration_output/dashboards/yaml \
  --output-dir migration_output/dashboards/compiled

# Upload a directory of dashboard YAML (recompiles internally).
.venv/bin/obs-migrate upload \
  --yaml-dir migration_output/dashboards/yaml \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY"
```

### Inspect a target Kibana cluster

```bash
.venv/bin/obs-migrate cluster list-dashboards \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"

.venv/bin/obs-migrate cluster ensure-data-views \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --data-view-patterns "metrics-*,logs-*"

.venv/bin/obs-migrate cluster detect-serverless \
  --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY"
```

### Explore flags and extensions

```bash
.venv/bin/obs-migrate --help
.venv/bin/obs-migrate migrate --help
.venv/bin/obs-migrate extensions --source grafana --format yaml
```

See [`docs/command-contract.md`](docs/command-contract.md) for the full
command catalog, dedicated source CLIs, and end-to-end tested flows.

## Output Layout

A typical `--output-dir` (e.g. `migration_output/`) looks like:

```text
migration_output/
├── dashboards/
│   ├── yaml/                 # emitted Lens dashboard YAML (canonical IR)
│   └── compiled/             # NDJSON compiled from YAML for Kibana import
├── alerts/                   # only present when --create-alert-rules is used
│   ├── alert_rule_upload_results.json     # Grafana run summary
│   └── monitor_rule_upload_results.json   # Datadog run summary
├── manifest.json             # per-run manifest (assets, status, warnings)
└── verification/             # source-vs-target comparison packets
```

## Re-running Migrations

The migrate command is intended to be re-runnable:

- Dashboard upload uses Kibana saved-object import with overwrite semantics,
  so re-running with the same input replaces the previously uploaded
  dashboards rather than duplicating them.
- Alert rules are tagged `obs-migration` and tagged with the source kind
  (`source:grafana` / `source:datadog`). Re-running with
  `--create-alert-rules` will create new rules; review and clean up via the
  Kibana **Rules** UI (filter by the `obs-migration` tag) or via
  [`scripts/audit_migrated_rules.py`](scripts/audit_migrated_rules.py)
  before re-running on the same target.

## Documentation

- Docs index — [`docs/README.md`](docs/README.md)
- Canonical commands — [`docs/command-contract.md`](docs/command-contract.md)
- Architecture — [`docs/architecture.md`](docs/architecture.md)
- Grafana source guide — [`docs/sources/grafana.md`](docs/sources/grafana.md)
- Datadog source guide — [`docs/sources/datadog.md`](docs/sources/datadog.md)
- Kibana target — [`docs/targets/kibana.md`](docs/targets/kibana.md)

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for
setup, verification commands, documentation rules, and PR expectations.

## Governance

| Doc | Use when |
| --- | --- |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Setup, verification, docs rules, and pull request expectations |
| [`SECURITY.md`](SECURITY.md) | Reporting a vulnerability (not via public issues) |
| [`SUPPORT.md`](SUPPORT.md) | Getting help and what to include in issues |
| [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) | Community expectations and conduct reporting |

## Licensing

- First-party repository content is source-available under the Elastic
  License 2.0 (`ELv2`); see [`LICENSE`](LICENSE).
- Third-party material redistributed in this repository is listed in
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and remains governed
  by its upstream license terms.
- A locked license-compliance check and CycloneDX SBOM run in CI for the
  checked Python dependency environment — see
  [`.github/workflows/license-check.yml`](.github/workflows/license-check.yml)
  and [`scripts/check_licenses.py`](scripts/check_licenses.py).

## Trademarks

Grafana is a trademark of Raintank, Inc. d/b/a Grafana Labs. Datadog is a
trademark of Datadog, Inc. Prometheus is a trademark of The Linux Foundation.
Kubernetes is a trademark of The Linux Foundation. Kibana and Elastic are
trademarks of Elasticsearch B.V. All other trademarks are the property of
their respective owners. Use of these names in this repository is solely
for interoperability and identification purposes and does not imply any
affiliation with or endorsement by the respective trademark holders.
