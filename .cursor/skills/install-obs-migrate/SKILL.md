---
name: install-obs-migrate
description: Use when obs-migrate is missing, doctor fails, the user asks how to install/set up the CLI, uvx/pip fails, Python is too old, extras are missing, or any other migration skill cannot run because the tool is not Ready — owns getting elastic-observability-migration installed and verified before connect/migrate skills proceed. Not for Grafana/Datadog/Elastic credentials (use connect-to-o11y-source).
---

# Install and verify `obs-migrate`

Goal: get a **Ready** `obs-migrate` on this machine, then hand off to the skill
the user actually wanted (`connect-to-o11y-source`, `scan-o11y-environment`,
migrate skills, etc.). This skill is the **only** place that owns install
commands, extras, and doctor gotchas — other skills must defer here instead of
inventing install steps.

## When to run this skill

Run **before** any other migration skill when any of these are true:

- `obs-migrate` / `uvx … obs-migrate` is not found
- `obs-migrate doctor` exits non-zero or is not `Ready`
- User asks how to install, which extra to use, or why Datadog/Grafana tooling is missing
- Default system Python is too old (common on macOS)

Do **not** use this skill for vendor/Elastic credentials — that is
`connect-to-o11y-source`.

## Supported platforms

- **macOS and Linux** only. Windows is not supported.
- **Python 3.11+** required (3.12+ preferred for in-venv Kibana compile tools).
- Canonical docs: `docs/command-contract.md` → Install And Setup; `README.md`.

## Step 0 — Detect current state

```bash
command -v uvx
command -v uv
command -v obs-migrate
python3 --version
```

Then run doctor with whichever form exists:

```bash
obs-migrate doctor
```

or:

```bash
uvx --from 'elastic-observability-migration[all]' obs-migrate doctor
```

Interpret:

| Observation | Action |
|---|---|
| `doctor` prints `Ready.` | Install is done — hand off to the calling skill |
| `command not found: obs-migrate` and no `uv`/`uvx` | Install `uv`, then Step 1 |
| Python `<3.11` | Install/use a newer Python (pyenv, python.org, Homebrew `python@3.12`), then retry |
| doctor notes Datadog client not installed | Reinstall with `[datadog]` or `[all]` if the user needs Datadog **API** mode |
| doctor Ready but kb-dashboard only via `uvx fallback` | Fine for default typed upload; keep `uv` on `PATH` on Python 3.11 |

## Step 1 — Install `uv` (recommended path)

[`uv`](https://docs.astral.sh/uv/) provides `uv` + `uvx` (no global pip required):

```bash
# https://docs.astral.sh/uv/getting-started/installation/
curl -LsSf https://astral.sh/uv/install.sh | sh
# then open a new shell or source the installer hint so uv/uvx are on PATH
command -v uvx
```

## Step 2 — Choose the footprint

| User needs | Extra | Notes |
|---|---|---|
| First-time / unsure / both sources | `[all]` (**default**) | Grafana + Datadog + tooling together |
| Grafana / Prometheus only | `[grafana]` or `[all]` | Base package already includes Grafana translation deps; `[all]` is still simplest |
| Datadog live API (`--input-mode api`) | `[datadog]` or `[all]` | File-mode Datadog works without the client; API mode needs `datadog-api-client` |
| Contributor / repo checkout | `.[all,dev]` via `make sync` | Not the operator path |

**Default recommendation:** `[all]` via `uvx` so the agent never strand the user
on a missing extra mid-migration.

## Step 3 — Install and verify (operator path)

### Recommended: ephemeral `uvx` (no venv)

```bash
PKG='elastic-observability-migration[all]'
# Optional pin: PKG='elastic-observability-migration[all]==0.4.0rc1'
uvx --from "$PKG" obs-migrate doctor
uvx --from "$PKG" obs-migrate list-samples
```

Always prefix commands with `uvx --from "$PKG"` unless the user wants a
persistent install.

### Persistent venv

```bash
PKG='elastic-observability-migration[all]'
python3 -m venv .venv
.venv/bin/pip install "$PKG"
.venv/bin/obs-migrate doctor
```

Put `.venv/bin` on `PATH` or invoke `.venv/bin/obs-migrate` explicitly.

### GitHub tag fallback (never `@main`)

Only if PyPI is unreachable:

```bash
PKG='elastic-observability-migration[all]@git+https://github.com/elastic/observability-migration-platform.git@v0.4.0rc1'
uvx --from "$PKG" obs-migrate doctor
```

### Repo checkout (contributors)

```bash
make sync
uv run obs-migrate doctor
# or: .venv/bin/pip install -e ".[all,dev]"
```

## Step 4 — Ready checklist (must pass)

`obs-migrate doctor` must show:

- `Ready.`
- Required dependencies `ok`
- For Datadog API work: `datadog (datadog-api-client): ok`
- `uv`/`uvx` on `PATH` when relying on kb-dashboard `uvx` fallback (Python 3.11)

Then run one offline smoke:

```bash
uvx --from 'elastic-observability-migration[all]' obs-migrate list-samples
```

Exit `0` + sample JSON ⇒ install verified. **Hand off** to the skill the user
originally asked for.

## Kibana tooling note

Default **typed Dashboards API** upload does **not** require `kb-dashboard-cli`.
`doctor` may report kb-dashboard as `installed` or `uvx fallback` — both are
fine. Legacy `--compile` / `--legacy-import` prefer an in-venv
`[kibana]` extra on Python 3.12+, otherwise `uvx` fallback.

## Honest limits / Do NOT

- **Do NOT** send users to Docker or Windows install paths (unsupported).
- **Do NOT** document `@main` git installs — pin a release tag.
- **Do NOT** fold Grafana/Datadog/Elastic credential setup into this skill —
  hand off to `connect-to-o11y-source`.
- **Do NOT** duplicate long install blocks in other skills — point here.
- **Do NOT** claim install success without a `Ready` doctor (or explain the
  exact doctor failure and fix it).

## See also

- `connect-to-o11y-source` — credentials and live source proof after install.
- `evaluate-o11y-permissions` — Elastic/Kibana key capabilities.
- `docs/command-contract.md` — Install And Setup (canonical).
- `README.md` — short operator install.
- PyPI: https://pypi.org/project/elastic-observability-migration/
