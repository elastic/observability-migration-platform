# Observability Migration Platform

[![Quality](https://github.com/elastic/mig-to-kbn/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/elastic/mig-to-kbn/actions/workflows/tests.yml)
[![License & SBOM](https://github.com/elastic/mig-to-kbn/actions/workflows/license-check.yml/badge.svg?branch=main)](https://github.com/elastic/mig-to-kbn/actions/workflows/license-check.yml)
[![License: Elastic-2.0](https://img.shields.io/badge/license-Elastic%20License%202.0-005571)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)

Source-agnostic tooling for migrating observability assets — Grafana and
Datadog dashboards, alerts, and monitors — into Kibana. Translations that
can't be expressed natively are surfaced as manual-review markers rather
than silently dropped. The installable CLI is `obs-migrate`; the Python
package is `observability_migration`.

## Status

Pre-1.0 (`0.1.0`). Actively developed. The CLI surface and emitted YAML
schema may change between releases — pin a tag if you build automation on
top.

## Quick Start

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/) on `PATH`
(provides `uvx`, used by compile and lint commands).

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[all]"
.venv/bin/obs-migrate --help
```

Use `.[grafana]` or `.[datadog]` for a single-source install, or
`pip install -e .` for core only. `python -m observability_migration` is
equivalent to the `obs-migrate` entry point.

For full command walkthroughs, env-file setup, and end-to-end flows, see
[`docs/command-contract.md`](docs/command-contract.md).

## Compatibility

- **Python**: 3.11+
- **Kibana**: Elastic Serverless and ES|QL-capable Stack Kibana — see
  [`docs/targets/kibana.md`](docs/targets/kibana.md) for the
  Serverless-specific matrix.
- **Grafana source**: dashboard JSON v1 schema; alerts via the unified
  alerting API.
- **Datadog source**: dashboards and monitors via the public Datadog API.

## Documentation

- Docs index — [`docs/README.md`](docs/README.md)
- Canonical commands — [`docs/command-contract.md`](docs/command-contract.md)
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
