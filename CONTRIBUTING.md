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

1. One-time: register `obs-migrate` on PyPI and add a Trusted Publisher for
   this repo + `.github/workflows/release.yml` (environment `pypi`).
2. Bump `version` in `pyproject.toml`; commit via PR.
3. Tag the merge commit `vX.Y.Z` and push the tag. The release workflow builds,
   publishes to PyPI via OIDC, and attaches the wheel/sdist + SBOM to a GitHub
   Release.

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
