# Third-Party Notices

This file records third-party material redistributed in the public repository
and third-party tooling used to produce shipped generated artifacts.

- `LICENSE` applies only to first-party repository content.
- Third-party material listed below remains governed by its upstream license
  terms and is not relicensed by `LICENSE`.
- Bundled third-party license texts live in `licenses/`.
- Python packages resolved into the checked dependency environment from
  `pyproject.toml` and `uv.lock` are not redistributed in source form and are
  therefore not enumerated here. Their licenses are inventoried in
  `docs/licenses/dependencies.md` and enforced by the license-compliance job
  described at the bottom of this file.
- Project-authored fixtures and excluded evaluation assets are not listed here.

## Bundled Datadog Dashboards (`DataDog/integrations-core`)

- Upstream repository: <https://github.com/DataDog/integrations-core>
- License: BSD-3-Clause
- Upstream copyright: `Copyright (c) 2016, Datadog, Inc. All rights reserved.`
- Bundled license text: `licenses/BSD-3-Clause-DataDog-integrations-core.txt`
- Upstream `NOTICE` file: none shipped by the upstream repository at the time
  of snapshot; BSD-3-Clause has no NOTICE obligation.
- Redistributed files:

- `infra/datadog/dashboards/integrations/docker.json`
  Source: `docker_daemon/assets/dashboards/docker_dashboard.json`
- `infra/datadog/dashboards/integrations/kubernetes.json`
  Source: `kubernetes/assets/dashboards/kubernetes_dashboard.json`
- `infra/datadog/dashboards/integrations/redis.json`
  Source: `redisdb/assets/dashboards/overview.json`
- `infra/datadog/dashboards/integrations/nginx_overview.json`
  Source: Datadog `nginx` dashboard assets in `DataDog/integrations-core`
- `infra/datadog/dashboards/integrations/postgres.json`
  Source: Datadog `postgres` dashboard assets in `DataDog/integrations-core`

Retain the applicable BSD notice and disclaimer when redistributing these
files. The Datadog name and marks may not be used to imply endorsement.

## Bundled Grafana Dashboards (Apache-2.0)

- License: Apache License, Version 2.0
- Bundled license text: `licenses/Apache-2.0.txt`
- Upstream `NOTICE` files: none shipped by either upstream repository at the
  time of snapshot; Apache-2.0 §4(c) is satisfied by carrying the full
  `Apache-2.0` license text and preserving the upstream copyright lines below.
- Redistributed files:

- `infra/grafana/dashboards/node-exporter-full.json`
  Upstream repository: <https://github.com/rfmoz/grafana-dashboards>
  Upstream dashboard listing: <https://grafana.com/grafana/dashboards/1860>
  Upstream copyright: the repository's `LICENSE` carries the Apache-2.0
  template copyright placeholder (unfilled); contributions are attributed to
  the `rfmoz/grafana-dashboards` maintainers and contributors.
- `infra/grafana/dashboards/k8s-views-global.json`
  Upstream repository:
  <https://github.com/dotdc/grafana-dashboards-kubernetes>
  Upstream dashboard listing: <https://grafana.com/grafana/dashboards/15757>
  Upstream copyright: `Copyright 2020 David Calvert`.

## Bundled Grafana Dashboards (MIT)

- License: MIT License
- Bundled license text:
  `licenses/MIT-FUSAKLA-Prometheus2-grafana-dashboard.txt`
- Upstream copyright: `Copyright (c) 2017 Martin Chodur`
- Redistributed files:

- `infra/grafana/dashboards/prometheus-all.json`
  Upstream repository:
  <https://github.com/FUSAKLA/Prometheus2-grafana-dashboard>
  Upstream dashboard listing: <https://grafana.com/grafana/dashboards/3681>

## Generated Artifacts With Separate Provenance

The tracked files below are machine-generated from the upstream
`strawgate/kb-yaml-to-lens` project, which publishes `kb-dashboard-core`
under the MIT License:

- Upstream project: <https://github.com/strawgate/kb-yaml-to-lens>
- Upstream package used to generate the tracked files:
  `kb-dashboard-core` (<https://pypi.org/project/kb-dashboard-core/>)
- Upstream copyright:
  `Copyright (c) 2025 kb-yaml-to-lens contributors`
- Bundled license text: `licenses/MIT-strawgate-kb-yaml-to-lens.txt`

Generated files tracked in this repository:

- `docs/dashboards/schema.json` — regenerated from `kb-dashboard-core` by
  `scripts/generate_dashboard_schema.sh`.
- `docs/dashboards/schema.toon` — regenerated from the same source when
  `npx` is available.

The local dashboard workflow may also invoke `kb-dashboard-cli` and
`kb-dashboard-lint`, but those tools are not themselves tracked artifacts in
this repository.

### `obs-migrate[kibana]` Extra

The optional `[kibana]` dependency group (installed via
`pip install ".[kibana]"`, resolved only on **Python 3.12+** per the
`pyproject.toml` environment markers) declares the following packages so the
Kibana compile/lint tooling can be installed in-environment instead of fetched
at runtime via `uvx`:

- `kb-dashboard-cli`, `kb-dashboard-lint`, and their transitive
  `kb-dashboard-core`, `kb-dashboard-tools`, and `kb-dashboard-docs` — all
  published by `strawgate/kb-yaml-to-lens` under the **MIT License**
  (bundled text: `licenses/MIT-strawgate-kb-yaml-to-lens.txt`).
- Their further transitive runtime dependencies (e.g. `click` (BSD-3-Clause),
  `rich-click` (MIT), `elasticsearch` (Apache-2.0), `pygls` (Apache-2.0),
  `lsprotocol` (MIT)) are likewise permissively licensed and compatible with
  Elastic License 2.0 redistribution.

These packages are **not** redistributed in source form by this repository and,
because they resolve only on Python 3.12+, they are **not** present in the
Python 3.11 dependency inventory/SBOM described below. They remain governed by
their upstream licenses and are installed directly from PyPI into the user's
environment. Revisit this section with legal before changing the distribution
model (for example vendoring or bundling these tools into a shipped artifact).

## Python Dependency Environment

Python packages resolved from `pyproject.toml` and `uv.lock` are not
redistributed in source form by this repository — they are installed into a
locked Python 3.11 dependency environment for local verification and CI.
Their licenses are captured in two places:

- [`docs/licenses/dependencies.md`](docs/licenses/dependencies.md) — a
  human-readable license inventory produced by the MIT-licensed
  [`pip-licenses`](https://pypi.org/project/pip-licenses/) tool.
- [`docs/licenses/sbom.cdx.json`](docs/licenses/sbom.cdx.json) — a
  machine-readable CycloneDX 1.6 SBOM produced by the Apache-2.0-licensed
  [`cyclonedx-bom`](https://pypi.org/project/cyclonedx-bom/) tool.

Both files are regenerated deterministically from the locked Python 3.11
dependency environment and their drift is enforced on every pull request by
`.github/workflows/license-check.yml`, which also uploads the SBOM as a
workflow artifact named `sbom-cyclonedx`.

Because the gate runs on Python 3.11, the `[kibana]` extra's `kb-dashboard-*`
tools (gated to Python 3.12+) are intentionally absent from this 3.11 inventory
and SBOM; their licenses are documented in the `obs-migrate[kibana]` Extra
section above.

To regenerate locally (requires Python 3.11 to match CI):

```bash
UV_PROJECT_ENVIRONMENT=.venv-licensing \
  uv sync --locked --python 3.11 --all-extras
.venv-licensing/bin/python scripts/check_licenses.py --write-report
.venv-licensing/bin/cyclonedx-py environment \
  --output-reproducible \
  --pyproject pyproject.toml \
  -o docs/licenses/sbom.cdx.json
```
