# Contributing

Thanks for contributing to `obs-migrate`.

## Before You Start

- Read `README.md` for the public project overview.
- Use `docs/README.md` for the full documentation map.
- Use `docs/command-contract.md` for canonical commands.
- See `AGENTS.md` for automation/repo-working rules (including the `make`
  build/test/lint targets and the commit workflow).

## Setup

The `make` targets sync a locked `uv` dev environment that matches CI; this is
the preferred path (`uv` must be on `PATH`):

```bash
make sync   # uv sync --locked --all-extras
```

Or set up a plain virtualenv directly:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e ".[all,dev]"
.venv/bin/pre-commit install
```

Browser binaries are **not** part of the default unit-test setup. Only install
Chromium when you run live dashboard interaction / Playwright work:

```bash
make setup-browser          # python -m playwright install chromium
make test-interactions      # offline interaction-audit unit tests
```

Live control checks need a local no-SSO stack (9.5+) — see
`docs/testing.md` and `docs/command-contract.md`.

## Verification

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
.venv/bin/obs-migrate --help >/dev/null
.venv/bin/python -m pytest tests/ -x -q
```

Commits also run local `pre-commit` hooks for `gitleaks`, `ruff`, and a quick
CLI smoke subset via `pytest tests/test_app_cli.py -q`. Run the same checks on
demand with:

```bash
.venv/bin/pre-commit run --all-files
```

For the full picture of how migration correctness is verified — the layered
confidence pyramid, every gate (coverage matrices, fidelity ratchets, live
validation, render audit, interaction audit), and how to extend them — see
`docs/testing.md`.

## License Compliance And SBOM

When adding or bumping Python dependencies in `pyproject.toml` or `uv.lock`,
regenerate the license inventory and the CycloneDX SBOM and include both
refreshed files in your PR. Both files are produced deterministically from a
locked **Python 3.11** dependency environment — match the CI workflow exactly
or the drift check will fail:

```bash
# Use the same locked Python 3.11 dependency environment as CI:
UV_PROJECT_ENVIRONMENT=.venv-licensing \
  uv sync --locked --python 3.11 --all-extras
.venv-licensing/bin/python scripts/check_licenses.py --write-report
.venv-licensing/bin/cyclonedx-py environment \
  --output-reproducible \
  --pyproject pyproject.toml \
  -o docs/licenses/sbom.cdx.json
```

CI enforces these checks via `.github/workflows/license-check.yml`:

- The license gate fails the build on any dependency reporting a denied
  license (AGPL, SSPL, BUSL, GPL family) or one that is not yet on the
  allowlist. To add a new license label to the allowlist, update
  `scripts/check_licenses.py` and explain the rationale in the PR.
- The inventory and SBOM files are regenerated in CI and diff-checked
  against the committed copies — any drift fails the build with a
  pointer to the refresh command.
- Every successful workflow run uploads the CycloneDX SBOM as a
  downloadable artifact named `sbom-cyclonedx`.

## Releasing

1. Bump the package version (also refreshes `uv.lock` and license/SBOM docs):

   ```bash
   make bump-version VERSION=X.Y.Z
   ```

   (`scripts/bump_version.py` updates `pyproject.toml`, rewrites the example
   PyPI pin / git-tag install lines in `README.md`,
   `docs/command-contract.md`, and the mirrored `install-obs-migrate` skills,
   then runs `uv lock`. `make licenses` regenerates `docs/licenses/*`. Use
   `SKIP_LICENSES=1` only for local experiments — release PRs must refresh
   the SBOM.)
   Open a PR with that bump (and any release notes). Do **not** hand-edit
   version pins in the README — let `bump-version` keep them aligned.

2. After merge, tag the merge commit `vX.Y.Z` and push the tag. The release
   workflow fails fast if the tag does not match `[project].version` in
   `pyproject.toml`, **or** if operator install pins in README / command-contract
   / install skill drift from that version, then builds the wheel/sdist and
   attaches them (plus the SBOM) to a GitHub Release.

3. **PyPI Trusted Publishing** (configured; no Elastic PyPI org yet):

   Releases publish to
   [`elastic-observability-migration`](https://pypi.org/project/elastic-observability-migration/)
   via OIDC from `.github/workflows/release.yml` (GitHub environment `pypi`,
   tags `v*`). Ownership is on a personal PyPI account until an Elastic org
   exists; then transfer the project and re-check the Trusted Publisher.

   - Add maintainers as PyPI **Owner** collaborators:
     https://pypi.org/manage/project/elastic-observability-migration/collaboration/
   - Trusted Publisher fields (already set): Owner `elastic`, Repository
     `observability-migration-platform`, Workflow `release.yml`, Environment
     `pypi`.

4. Tagging a matching `vX.Y.Z` (or pre-release such as `v0.4.0rc1`) publishes
   via OIDC (no long-lived PyPI token).

5. Post-publish verification:

   ```bash
   uvx --from 'elastic-observability-migration[all]' obs-migrate doctor
   uvx --from 'elastic-observability-migration[all]' obs-migrate migrate --help
   ```

Operators install from PyPI as documented in `README.md` (git-tag `uvx` remains
an optional fallback).

## Docs And Structure Rules

- Keep `README.md` short and public-facing.
- Put canonical narrative docs under `docs/`.
- Update folder landing pages when `examples/` or `infra/` changes.
- Do not duplicate long command walkthroughs outside `docs/command-contract.md`.
- Do not commit secrets or generated local artifacts.

## Pull Requests

- Keep changes scoped.
- Update docs when behavior changes.
- Include validation notes in the PR description.
