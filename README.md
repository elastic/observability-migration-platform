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

Pre-1.0 (`0.1.0`). Actively developed. Release tags use `vX.Y.Z`
(for example, `v0.1.0`) and build GitHub Release artifacts. PyPI
publishing is pending Trusted Publisher setup, so install from a checkout
or release source archive for this release. The CLI surface and emitted
YAML schema may change between releases — pin a tag if you build
automation on top.

## Quick Start

Requires Python 3.11+. From a repo checkout or unpacked release source
archive, on **Python 3.12+** the `[kibana]` extra bundles the Kibana
compile/lint tooling directly in your environment, so nothing else is
needed:

```bash
python3 -m venv .venv
.venv/bin/pip install ".[all]"        # grafana + datadog + kibana tooling
.venv/bin/obs-migrate doctor          # confirm the environment is ready
```

`obs-migrate doctor` reports whether the Kibana tooling resolves from the
installed extra or a `uvx` fallback:

```text
obs-migrate doctor
  pinned kb-dashboard tool version: 0.4.1
  uv on PATH: yes
  kb-dashboard-cli: available (installed)
  kb-dashboard-lint: available (installed)
```

On **Python 3.11** the Kibana tools are not installed by the extra (they
require 3.12+); compile and lint instead shell out to a pinned `uvx`
invocation, so install [`uv`](https://docs.astral.sh/uv/) on `PATH`. The
same fallback applies on 3.12+ if you skip the `[kibana]` extra. `doctor`
will print `(uvx fallback)` in that case.

Use `.[grafana]`, `.[datadog]`, or `.[kibana]` for a narrower install, or
`pip install .` for core only. Add `-e` for an editable/dev checkout.
`python -m observability_migration` is equivalent to the `obs-migrate`
entry point.

For full command walkthroughs, env-file setup, and end-to-end flows, see
[`docs/command-contract.md`](docs/command-contract.md).

## Run your first migration

Once `obs-migrate doctor` reports a healthy environment, you can migrate a
dashboard with no Elastic or Kibana credentials. The repo ships sample
dashboards for exactly this, so list them first (offline, no setup):

```bash
.venv/bin/obs-migrate list-samples
```

Then migrate the bundled Grafana sample from files into a local output
directory:

```bash
.venv/bin/obs-migrate migrate \
  --source grafana \
  --input-mode files \
  --input-dir observability_migration/sample_dashboards/grafana/prom-basics \
  --output-dir sample_out
```

This translates the dashboard into Kibana-ready YAML. Anything that can't be
expressed natively, such as the sample's World Map panel, is kept as a
manual-review marker rather than silently dropped.

Everything is written under your `--output-dir`:

- `sample_out/dashboards/yaml/`: the generated dashboard YAML
- `sample_out/dashboards/compiled/`: the compiled Kibana NDJSON
- `sample_out/dashboards/migration_summary.md`: a human-readable verdict,
  scorecard, and per-dashboard worklist; read this first

To go further:

- [`docs/command-contract.md`](docs/command-contract.md): the full command
  reference (env-file setup, live API extraction, alerts, and upload). Reach
  for it as the reference once the example above works, not as the starting
  point.

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
