# Observability Migration Platform

[![Quality](https://github.com/elastic/observability-migration-platform/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/elastic/observability-migration-platform/actions/workflows/tests.yml)
[![License & SBOM](https://github.com/elastic/observability-migration-platform/actions/workflows/license-check.yml/badge.svg?branch=main)](https://github.com/elastic/observability-migration-platform/actions/workflows/license-check.yml)
[![License: Elastic-2.0](https://img.shields.io/badge/license-Elastic%20License%202.0-005571)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)

Migrate Grafana and Datadog dashboards, alerts, and monitors into Kibana.
Unsupported translations are marked for manual review instead of silently
dropped.

Use one CLI: **`obs-migrate`**.

## Requirements

| Need | Detail |
|------|--------|
| OS | macOS or Linux (Windows not supported yet) |
| Python | **3.11+** (3.11–3.13 recommended) |
| Tooling | [`uv`](https://docs.astral.sh/uv/) on `PATH` (provides `uv` + `uvx`) |

No repo clone required. Install with the `[all]` extra so Grafana, Datadog,
and Kibana compile/lint dependencies arrive together.

Pre-1.0 (`0.4.0`). Python package only (no standalone binary). PyPI publish
is pending Trusted Publisher setup — until then, install from a **pinned Git
tag** (not `@main`).

## Quick start

Set the package once, then reuse it:

```bash
# Until PyPI is live, pin a release tag (example: latest published tag).
PKG='obs-migrate[all]@git+https://github.com/elastic/observability-migration-platform.git@v0.3.0'
```

After this packaging work ships as `v0.4.0` (or newer), switch the tag.
Once on PyPI, use `PKG='obs-migrate[all]'` instead.

### 1. Check the install

```bash
uvx --from "$PKG" obs-migrate doctor
```

`doctor` verifies Python, required imports, extras, `uv`/`uvx`, and Kibana
compile tools. Exit `0` means ready; non-zero prints what to fix.

### 2. Try a bundled sample (offline)

```bash
uvx --from "$PKG" obs-migrate list-samples
```

Copy a sample `input_dir` from the JSON, then:

```bash
uvx --from "$PKG" obs-migrate migrate \
  --source grafana --input-mode files \
  --input-dir "<sample-input_dir>" \
  --output-dir ./out --assets dashboards --compile
```

### 3. Migrate your assets

Same `migrate` command — point `--input-dir` at your exported Grafana or
Datadog JSON, or use `--input-mode api` with credentials. Upload,
verification, and full flag reference:
[`docs/command-contract.md`](docs/command-contract.md).

## Other installs

| When | Command |
|------|---------|
| Persistent venv | `python3 -m venv .venv && .venv/bin/pip install "$PKG"` then `.venv/bin/obs-migrate …` |
| Contributor checkout | `make sync` (or `uv sync --locked --all-extras`) then `uv run obs-migrate …` |
| Narrow extras | `[grafana]`, `[datadog]`, or `[kibana]` instead of `[all]` |

On **Python 3.11**, Kibana compile tools are not installed in-venv (they need
3.12+); `doctor` reports `(uvx fallback)` and `uv` must stay on `PATH`.

Legacy `grafana-migrate` / `datadog-migrate` entry points remain as
compatibility aliases — prefer `obs-migrate`.

## Compatibility

| Area | Support |
|------|---------|
| OS | **Supported:** macOS, Linux. **CI-tested:** Ubuntu. **Local smoke:** macOS. Windows not claimed. |
| Python | **Supported:** 3.11+. **CI pytest:** 3.11, 3.12, 3.13. **Clean-install smoke:** 3.11–3.12 (CI); 3.13–3.14 exercised locally. **Rejected:** ≤3.10. |
| Kibana target | Elastic Serverless and ES\|QL-capable Stack — [`docs/targets/kibana.md`](docs/targets/kibana.md) |
| Grafana | Dashboard JSON v1; alerts via unified alerting API — [`docs/sources/grafana.md`](docs/sources/grafana.md) |
| Datadog | Dashboards and monitors via public API — [`docs/sources/datadog.md`](docs/sources/datadog.md) |

## Documentation

| Doc | Use when |
| --- | --- |
| [`docs/README.md`](docs/README.md) | Docs index |
| [`docs/command-contract.md`](docs/command-contract.md) | Canonical CLI commands and install detail |
| [`docs/known-limitations.md`](docs/known-limitations.md) | Known gaps |
| [`docs/architecture.md`](docs/architecture.md) | How the pipeline fits together |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev setup, releasing, PR expectations |
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
