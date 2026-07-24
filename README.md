# Observability Migration Platform

[![Quality](https://github.com/elastic/mig-to-kbn/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/elastic/mig-to-kbn/actions/workflows/tests.yml)
[![License & SBOM](https://github.com/elastic/mig-to-kbn/actions/workflows/license-check.yml/badge.svg?branch=main)](https://github.com/elastic/mig-to-kbn/actions/workflows/license-check.yml)
[![License: Elastic-2.0](https://img.shields.io/badge/license-Elastic%20License%202.0-005571)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)

Migrate Grafana and Datadog dashboards, alerts, and monitors into Kibana.
Unsupported translations are marked for manual review instead of being
silently dropped.

**One tool:** `obs-migrate` — install it once, then doctor → try a sample →
migrate your assets. (Legacy `grafana-migrate` / `datadog-migrate` scripts
still exist for compatibility; prefer `obs-migrate`.)

## Status

Pre-1.0 (`0.4.0`). No standalone binary — Python package only. PyPI
publishing is pending Trusted Publisher setup; until then use the
git-based `uvx` install below (pin a release tag, not `@main`). Pin tags
if you automate on top of pre-1.0 releases.

## Quick Start (operators)

**Need (first-time machine):**
1. **Python 3.11+** (3.11–3.13 recommended; see Compatibility)
2. **[`uv`](https://docs.astral.sh/uv/)** on `PATH` (installs `uv` + `uvx`)
3. macOS or Linux

You do **not** need to clone this repo. `obs-migrate[all]` pulls Grafana,
Datadog, and Kibana compile/lint dependencies in one install.

### 1. Install and check

```bash
uvx --from "obs-migrate[all]@git+https://github.com/elastic/observability-migration-platform.git@v0.3.0" \
  obs-migrate doctor
```

Use `[all]` so Grafana, Datadog, and Kibana compile/lint tooling are
available in one shot. After the next release that includes this packaging
work, prefer `@v0.4.0` (or newer). Once PyPI publishing is enabled:

```bash
uvx --from 'obs-migrate[all]' obs-migrate doctor
```

Healthy `doctor` output looks like:

```text
obs-migrate doctor
  package version: …
  package location: …/site-packages/observability_migration
  pinned kb-dashboard tool version: 0.4.1
  uv on PATH: yes
  kb-dashboard-cli: available (installed)
  kb-dashboard-lint: available (installed)

Next steps:
  obs-migrate list-samples
  obs-migrate migrate --source grafana --input-mode files --input-dir <dir> --output-dir ./out
```

### 2. Try a bundled sample (offline)

```bash
uvx --from "obs-migrate[all]@git+https://github.com/elastic/observability-migration-platform.git@v0.3.0" \
  obs-migrate list-samples
```

Pick a sample `input_dir` from the JSON, then:

```bash
uvx --from "obs-migrate[all]@git+https://github.com/elastic/observability-migration-platform.git@v0.3.0" \
  obs-migrate migrate --source grafana --input-mode files \
  --input-dir "<sample-input_dir>" --output-dir ./out --assets dashboards --compile
```

### 3. Migrate your dashboards

Same command, point `--input-dir` at your exported Grafana or Datadog JSON
(or use `--input-mode api` with credentials). Full flags, env files, upload,
and verification: [`docs/command-contract.md`](docs/command-contract.md).

### Other install options

| When | How |
|------|-----|
| Persistent local install | `python3 -m venv .venv && .venv/bin/pip install "obs-migrate[all]@git+…@TAG"` then `.venv/bin/obs-migrate …` |
| Repo contributor / CI | `uv sync --locked --all-extras` then `uv run obs-migrate …` or `make sync` |
| Narrow extras | `.[grafana]`, `.[datadog]`, `.[kibana]` instead of `[all]` if you know you need only one source |

On **Python 3.11**, the `[kibana]` / `[all]` extras do not install
`kb-dashboard-*` in-venv (those tools need 3.12+); compile/lint use a pinned
`uvx` fallback — keep `uv` on `PATH`. `doctor` prints `(uvx fallback)` then.

## Compatibility

- **OS (supported)**: macOS and Linux. Windows is not a supported or
  CI-tested install target yet.
- **OS (tested)**:
  - **Linux (Ubuntu)**: CI unit tests, packaging smoke, and release builds
    (`ubuntu-latest`). Clean-install package smoke covers Python **3.11** and
    **3.12**.
  - **macOS**: local packaging / clean-install smoke (wheel + sdist,
    `obs-migrate doctor`, offline Grafana/Datadog `--compile`).
- **Python**:
  - **Supported:** 3.11+ (`requires-python = ">=3.11"`).
  - **Recommended / CI-tested:** 3.11, 3.12, 3.13 (pytest matrix on Ubuntu).
  - **Package smoke (clean wheel install + migrate):** 3.11 and 3.12 in CI;
    local matrix also covers 3.13 and 3.14 where available.
  - **Not supported:** 3.10 and older (installer must reject).
  - **Kibana tooling:** `[kibana]` / `[all]` install `kb-dashboard-*` only on
    **3.12+**; on **3.11** compile/lint use a pinned `uvx` fallback (so `uv`
    must be on `PATH`).
  - **Dependencies:** core install needs `grafana-client`, `promql-parser`,
    `pydantic`, `PyYAML`, `requests`, `lark`. Operators should use
    `obs-migrate[all]` so Datadog + Kibana extras are included. Run
    `obs-migrate doctor` on a fresh machine to verify imports and tools.
- **Kibana**: Elastic Serverless and ES|QL-capable Stack Kibana — see
  [`docs/targets/kibana.md`](docs/targets/kibana.md).
- **Grafana source**: dashboard JSON v1 schema; alerts via the unified
  alerting API.
- **Datadog source**: dashboards and monitors via the public Datadog API.

## Documentation

- Docs index — [`docs/README.md`](docs/README.md)
- Canonical commands — [`docs/command-contract.md`](docs/command-contract.md)
- Known limitations — [`docs/known-limitations.md`](docs/known-limitations.md)
- Architecture — [`docs/architecture.md`](docs/architecture.md)
- Grafana source — [`docs/sources/grafana.md`](docs/sources/grafana.md)
- Datadog source — [`docs/sources/datadog.md`](docs/sources/datadog.md)
- Kibana target — [`docs/targets/kibana.md`](docs/targets/kibana.md)

## Governance

| Doc | Use when |
| --- | --- |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Setup, verification, docs rules, and pull request expectations |
| [`SUPPORT.md`](SUPPORT.md) | Getting help and what to include in issues |
| [`SECURITY.md`](SECURITY.md) | Reporting a vulnerability (not via public issues) |
| [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) | Community expectations and conduct reporting |

For bugs and feature requests,
[open an issue](https://github.com/elastic/mig-to-kbn/issues).

## Licensing

First-party content is source-available under the Elastic License 2.0
(`ELv2`); see [`LICENSE`](LICENSE). Redistributed third-party material is
listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) under its
upstream terms. A locked license-compliance check and CycloneDX SBOM run
in CI — see
[`.github/workflows/license-check.yml`](.github/workflows/license-check.yml).

## Trademarks

Grafana is a trademark of Raintank, Inc. d/b/a Grafana Labs. Datadog is a
trademark of Datadog, Inc. Prometheus and Kubernetes are trademarks of
The Linux Foundation. Kibana and Elastic are trademarks of Elasticsearch
B.V. All other trademarks are the property of their respective owners.
Use of these names here is solely for interoperability and identification
and does not imply affiliation or endorsement.
