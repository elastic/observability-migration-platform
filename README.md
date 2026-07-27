# Observability Migration Platform

[![Quality](https://github.com/elastic/observability-migration-platform/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/elastic/observability-migration-platform/actions/workflows/tests.yml)
[![License & SBOM](https://github.com/elastic/observability-migration-platform/actions/workflows/license-check.yml/badge.svg?branch=main)](https://github.com/elastic/observability-migration-platform/actions/workflows/license-check.yml)
[![License: Elastic-2.0](https://img.shields.io/badge/license-Elastic%20License%202.0-005571)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)

Migrate Grafana and Datadog dashboards, alerts, and monitors into Kibana.
Unsupported translations are marked for manual review instead of being
silently dropped.

Use one CLI: **`obs-migrate`**.

Pre-1.0. Distributed as a Python package only — there is no standalone
binary. The package is not on PyPI yet, so install from a **pinned GitHub
release tag** (never `@main`) until publishing is enabled.

## Requirements

| Need | Detail |
|------|--------|
| OS | macOS or Linux (Windows is not supported) |
| Python | 3.11 or newer (3.11–3.13 recommended) |
| Installer | [`uv`](https://docs.astral.sh/uv/) on `PATH` (provides `uv` and `uvx`) |

You do not need to clone this repository. Install with the `[all]` extra so
Grafana, Datadog, and Kibana compile/lint dependencies are included together.

## Quick start

Set the package source once, then reuse it:

```bash
# Pin the latest release tag (example below). See:
# https://github.com/elastic/observability-migration-platform/releases
PKG='elastic-observability-migration[all]@git+https://github.com/elastic/observability-migration-platform.git@v0.3.0'
```

When the package is on PyPI, use:

```bash
PKG='elastic-observability-migration[all]'
```

### 1. Check the install

```bash
uvx --from "$PKG" obs-migrate doctor
```

`doctor` checks your Python version, required imports, optional extras,
`uv`/`uvx`, and Kibana compile tools. Exit code `0` means ready; a non-zero
exit prints what to fix.

### 2. Try a bundled sample (offline)

```bash
uvx --from "$PKG" obs-migrate list-samples
```

Copy a sample’s `input_dir` from the JSON output, then:

```bash
uvx --from "$PKG" obs-migrate migrate \
  --source grafana --input-mode files \
  --input-dir "<sample-input_dir>" \
  --output-dir ./out --assets dashboards --compile
```

### 3. Migrate your assets

Use the same `migrate` command with your exported Grafana or Datadog JSON
(`--input-dir`), or use `--input-mode api` with credentials. For upload,
verification, and the full flag reference, see
[`docs/command-contract.md`](docs/command-contract.md).

## Other install options

| When | How |
|------|-----|
| Persistent virtualenv | `python3 -m venv .venv && .venv/bin/pip install "$PKG"` then `.venv/bin/obs-migrate …` |
| Contributor checkout | `make sync` (or `uv sync --locked --all-extras`) then `uv run obs-migrate …` |
| Narrower extras | Use `[grafana]`, `[datadog]`, or `[kibana]` instead of `[all]` |

On **Python 3.11**, Kibana compile tools are not installed into the
environment (they require 3.12+). `doctor` reports `uvx fallback`, and `uv`
must remain on `PATH`.

The older `grafana-migrate` and `datadog-migrate` commands still work as
compatibility aliases. Prefer `obs-migrate`.

## Compatibility

| Area | Detail |
|------|--------|
| OS | Supported on macOS and Linux. CI runs on Ubuntu; packaging is also smoke-tested on macOS. Windows is not supported. |
| Python | Supported: 3.11+. CI pytest: 3.11, 3.12, 3.13. Clean-install smoke in CI: 3.11 and 3.12 (also verified on 3.13 and 3.14). Python 3.10 and older are rejected. |
| Kibana | Elastic Serverless and ES\|QL-capable Stack — [`docs/targets/kibana.md`](docs/targets/kibana.md) |
| Grafana | Dashboard JSON v1; alerts via the unified alerting API — [`docs/sources/grafana.md`](docs/sources/grafana.md) |
| Datadog | Dashboards and monitors via the public API — [`docs/sources/datadog.md`](docs/sources/datadog.md) |

## Documentation

| Doc | Use when |
| --- | --- |
| [`docs/README.md`](docs/README.md) | Docs index |
| [`docs/command-contract.md`](docs/command-contract.md) | Canonical CLI commands and install detail |
| [`docs/known-limitations.md`](docs/known-limitations.md) | Known gaps |
| [`docs/architecture.md`](docs/architecture.md) | How the pipeline fits together |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev setup, releasing, and PR expectations |
| [`SUPPORT.md`](SUPPORT.md) | Getting help |
| [`SECURITY.md`](SECURITY.md) | Vulnerability reporting |

Bugs and feature requests:
[open an issue](https://github.com/elastic/observability-migration-platform/issues).

## Licensing

First-party content is source-available under the Elastic License 2.0
(`ELv2`); see [`LICENSE`](LICENSE). Redistributed third-party material is
listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). CI runs a
locked license check and CycloneDX SBOM — see
[`.github/workflows/license-check.yml`](.github/workflows/license-check.yml).

## Trademarks

Grafana is a trademark of Raintank, Inc. d/b/a Grafana Labs. Datadog is a
trademark of Datadog, Inc. Prometheus and Kubernetes are trademarks of
The Linux Foundation. Kibana and Elastic are trademarks of Elasticsearch
B.V. All other trademarks are the property of their respective owners.
Use of these names here is solely for interoperability and identification
and does not imply affiliation or endorsement.
