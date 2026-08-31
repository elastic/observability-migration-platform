# Observability Migration Platform

[![Quality](https://github.com/elastic/observability-migration-platform/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/elastic/observability-migration-platform/actions/workflows/tests.yml)
[![License & SBOM](https://github.com/elastic/observability-migration-platform/actions/workflows/license-check.yml/badge.svg?branch=main)](https://github.com/elastic/observability-migration-platform/actions/workflows/license-check.yml)
[![License: Elastic-2.0](https://img.shields.io/badge/license-Elastic%20License%202.0-005571)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/elastic-observability-migration)](https://pypi.org/project/elastic-observability-migration/)

Migrate Grafana and Datadog dashboards, alerts, and monitors into Kibana.
Unsupported translations are marked for manual review instead of being
silently dropped.

**You do not need to clone this repository.** Install
[`elastic-observability-migration`](https://pypi.org/project/elastic-observability-migration/)
from PyPI and run **`obs-migrate`**.

## Requirements

| Need | Detail |
|------|--------|
| OS | macOS and Linux (Windows untested) |
| Python | 3.11 or newer (tested on 3.11–3.13) |
| Installer | [`uv`](https://docs.astral.sh/uv/) on `PATH` (provides `uv` and `uvx`) |
| Kibana | Elastic Serverless or Stack 9.5+ |

Install with the `[all]` extra so Grafana, Datadog, and Kibana tooling are
available together.

## Quick start

```bash
# Pin when you want a fixed release, e.g.
# PKG='elastic-observability-migration[all]==1.0.0'
PKG='elastic-observability-migration[all]'

uvx --from "$PKG" obs-migrate doctor
uvx --from "$PKG" obs-migrate list-samples
```

`doctor` exit code `0` means Ready; otherwise it prints what to fix.

Copy an `input_dir` path from the `list-samples` output and try an offline migrate:

```bash
uvx --from "$PKG" obs-migrate migrate \
  --source grafana --input-mode files \
  --input-dir "<input_dir from list-samples>" \
  --output-dir ./out --assets dashboards
```

Review the generated `./out/dashboards/native/*.native.json` artifacts (the
exact typed Dashboards API payloads), then upload:

```bash
uvx --from "$PKG" obs-migrate upload \
  --artifact-dir ./out/dashboards \
  --kibana-url "$KIBANA_ENDPOINT" \
  --kibana-api-key "$KEY"
```

For your own assets, use the same `migrate` command with exported JSON
(`--input-dir`) or `--input-mode api` plus credentials. Upload, verification,
index flags (`--data-view` / `--esql-index`), and the full flag reference:
[`docs/command-contract.md`](docs/command-contract.md).

## Run your first migration

Once `obs-migrate doctor` reports a healthy environment, you can migrate a
dashboard with no Elastic or Kibana credentials. The repo ships sample
dashboards for exactly this, so list them first (offline, no setup):

```bash
uvx --from "$PKG" obs-migrate list-samples
```

Copy the `input_dir` value for the Grafana sample, then migrate it into a
local output directory:

```bash
uvx --from "$PKG" obs-migrate migrate \
  --source grafana --input-mode files \
  --input-dir "<input_dir from list-samples>" \
  --output-dir sample_out --assets dashboards --compile
```

This translates the dashboard into Kibana-ready YAML and compiles it to
NDJSON. Anything that can't be expressed natively, such as the sample's
World Map panel, is kept as a manual-review marker rather than silently
dropped.

Key output lands under `sample_out/dashboards/`:

- `yaml/`: the generated dashboard YAML
- `compiled/`: the compiled Kibana NDJSON (requires `--compile`)
- `migration_summary.md`: a human-readable verdict, scorecard, and
  per-dashboard worklist; read this first

To go further:

- [`docs/command-contract.md`](docs/command-contract.md): the full command
  reference (env-file setup, live API extraction, alerts, and upload). Reach
  for it as the reference once the example above works, not as the starting
  point.

## Compatibility
Always reuse the same launcher as `doctor` (`uvx --from "$PKG" …`). `PKG` only
lives in the shell you set it in, so re-export it in a new terminal.

### If you see `command not found: obs-migrate`

`obs-migrate` is a console script, not a global binary: a bare `obs-migrate`
only works when its install location is on `PATH`. Otherwise, prefix it with a
launcher. Pick the line matching how you installed:

```bash
# uvx: works in any shell, nothing installed first (package spelled out in
# full, because a new shell has no variables from the Quick Start above)
uvx --from 'elastic-observability-migration[all]' obs-migrate doctor

# virtualenv, without activating it — relative path, so run it from the
# directory where you created the virtualenv
.venv/bin/obs-migrate doctor

# same virtualenv, activated once per shell — then the bare command works
source .venv/bin/activate && obs-migrate doctor
```

The last two need a `.venv` you already created — see
[Other install options](#other-install-options) — and both resolve `.venv`
against your current directory, so `cd` there first or use the full path.

If you want a bare `obs-migrate` in *every* shell with no prefix, install it as
a tool (`uv tool install`, or `pipx install` if you prefer pipx); that is the
first option below. A tool install cannot put its shim directory on the `PATH`
of the shell you run it in, so follow it with the `export` shown there.

## Other install options

**Persistent bare command** — installs once and puts `obs-migrate` on `PATH`
for every shell, so no launcher prefix is needed:

```bash
uv tool install 'elastic-observability-migration[all]'
export PATH="$HOME/.local/bin:$PATH"
obs-migrate doctor
```

`uv tool install` puts the shim in `~/.local/bin` (`uv tool dir --bin` prints
the real location) and warns when that directory is missing from `PATH`. It
cannot change the `PATH` of the shell that invoked it, hence the `export`; run
`uv tool update-shell` once so new shells pick it up too. `pipx install` works
the same way. `uv` picks your newest Python, so add `--python 3.13` to stay within
the CI-tested range if your system Python is 3.14 or newer.

**Persistent virtualenv** (optional; prefer `uvx` above for first runs):

```bash
PKG='elastic-observability-migration[all]'
python3 -m venv .venv
.venv/bin/pip install "$PKG"
.venv/bin/obs-migrate doctor
# Or: source .venv/bin/activate && obs-migrate doctor
```

**Narrower extras:** `[grafana]`, `[datadog]`, or `[kibana]` instead of `[all]`.

**GitHub tag fallback** (only if PyPI is unreachable; never `@main`):

```bash
PKG='elastic-observability-migration[all]@git+https://github.com/elastic/observability-migration-platform.git@v1.0.0'
uvx --from "$PKG" obs-migrate doctor
```

On **Python 3.11**, keep `uv` on `PATH` so Kibana compile tools can use the
`uvx` fallback when needed. Default typed dashboard upload does not require
those tools.

The older `grafana-migrate` and `datadog-migrate` commands remain as
compatibility aliases. Prefer `obs-migrate`.

## Documentation

| Doc | Use when |
| --- | --- |
| [`docs/command-contract.md`](docs/command-contract.md) | Commands, flags, upload, and verification |
| [`docs/README.md`](docs/README.md) | Full docs index |
| [`docs/sources/grafana.md`](docs/sources/grafana.md) / [`datadog.md`](docs/sources/datadog.md) | Source-specific behavior |
| [`docs/targets/kibana.md`](docs/targets/kibana.md) | Kibana target behavior |
| [`SUPPORT.md`](SUPPORT.md) | Getting help |
| [`SECURITY.md`](SECURITY.md) | Vulnerability reporting |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Repo checkout and contributor setup |

Bugs and feature requests:
[open an issue](https://github.com/elastic/observability-migration-platform/issues).

## Licensing

First-party content is source-available under the Elastic License 2.0
(`ELv2`); see [`LICENSE`](LICENSE). Redistributed third-party material is
listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Trademarks

Grafana is a trademark of Raintank, Inc. d/b/a Grafana Labs. Datadog is a
trademark of Datadog, Inc. Prometheus and Kubernetes are trademarks of
The Linux Foundation. Kibana and Elastic are trademarks of Elasticsearch B.V.
All other trademarks are the property of their respective owners.
Use of these names is solely for interoperability and identification
and does not imply affiliation or endorsement.
