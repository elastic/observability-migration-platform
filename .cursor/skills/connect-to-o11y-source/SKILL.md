---
name: connect-to-o11y-source
description: Use when the user wants to connect, authenticate, point the tool at, or verify connectivity/credentials to their Grafana or Datadog instance, or asks "can the tool reach my Grafana/Datadog" / "how do I set up access" — connects the obs-migrate tool to a source observability vendor (Grafana or Datadog) and proves it can actually reach it before any migration.
---

# Connect to an o11y source (Grafana / Datadog)

Goal: get the user authenticated against their source vendor and **prove the tool can reach it** with the cheapest real call, before they invest in a migration.

## Which command form to use (package vs. repo)

Most consumers install from PyPI
([`elastic-observability-migration`](https://pypi.org/project/elastic-observability-migration/)).
Prefer the unified CLI **`obs-migrate`**. The older `grafana-migrate` /
`datadog-migrate` entry points remain as compatibility aliases.

- **Ephemeral (recommended):** `uvx --from 'elastic-observability-migration[…]' obs-migrate …`
- **Persistent:** `pip install 'elastic-observability-migration[…]'` then `obs-migrate` on `PATH`
- **Repo checkout only:** prefix `.venv/bin/`

This skill uses the package form. Do not assume a repo, `infra/`, `examples/`,
or `scripts/` directory exists.

## Core facts (do not invent around these)

- There is **no dedicated `ping`/`connect` command**. The smallest real proof of connectivity is a **live API extraction run** (`--input-mode api` on `obs-migrate`, or `--source api` on the dedicated CLIs), which makes authenticated HTTP calls to the vendor.
- Credentials come from **environment variables** (export them in the shell, or keep them in a local env file you `source`). Datadog also accepts `--env-file`.
- `--list-dashboards` is **target-side (Kibana), not source-side.** It lists dashboards *in Kibana* and needs `--kibana-url`. Do **not** use it to test a Grafana/Datadog connection.
- A connectivity check is a **source-only** operation: do not add target flags like `--es-url`, `--kibana-url`, `--data-view`, or `--field-profile`. Set `KIBANA_URL=` in the shell to suppress any default local-Kibana preflight.

## Install (once)

```bash
# Ephemeral check (needs uv on PATH):
uvx --from 'elastic-observability-migration[all]' obs-migrate doctor

# Or persistent venv:
pip install 'elastic-observability-migration[grafana]'   # or [datadog], or [all]
obs-migrate doctor
```

`grafana` and `datadog` are real optional extras. Datadog API mode **requires**
the `datadog` extra (`datadog-api-client`). From a repo checkout:
`python3 -m venv .venv && .venv/bin/pip install -e ".[grafana]"`.

## Grafana

Credentials are **flag-first with env fallback** (each flag defaults to its env var, matching how Elasticsearch/Kibana creds work): `--grafana-url` (env `GRAFANA_URL`) plus **either** `--grafana-user` + `--grafana-pass` (env `GRAFANA_USER` / `GRAFANA_PASS`, HTTP basic auth) **or** a bearer token via `--grafana-token` (env `GRAFANA_TOKEN`).

```bash
# Option A — environment variables
export GRAFANA_URL="https://grafana.example.com"
export GRAFANA_USER="..." GRAFANA_PASS="..."   # or a token below
```

Verify reachability (source-only live extraction to a throwaway dir):

```bash
KIBANA_URL= obs-migrate migrate \
  --source grafana --input-mode api \
  --output-dir /tmp/grafana_connect_check \
  --assets dashboards
# token auth instead of user/pass: add  --grafana-token "$GRAFANA_TOKEN"
```

Option B — pass connection details as flags instead of exporting env vars (useful in CI or when juggling multiple instances):

```bash
KIBANA_URL= obs-migrate migrate \
  --source grafana --input-mode api \
  --grafana-url "https://grafana.example.com" \
  --grafana-user "$GF_USER" --grafana-pass "$GF_PASS" \
  --output-dir /tmp/grafana_connect_check \
  --assets dashboards
```

(`grafana-migrate --source api …` is equivalent if the user prefers the alias.)

What it does under the hood: authenticates and calls Grafana `/api/search?type=dash-db` then `/api/dashboards/uid/<uid>` (capped at 500). If it pulls one or more dashboards, the tool reached Grafana and could read them. Auth/URL failures surface as an HTTP error from `raise_for_status()` (e.g. 401/403/404 or a connection error).

## Datadog

Credentials (env): `DD_API_KEY`, `DD_APP_KEY`, and optionally `DD_SITE` (default `datadoghq.com`). You can export them or put them in an env file passed via `--env-file`.

```bash
pip install 'elastic-observability-migration[datadog]'   # if not using [all]
# or: uvx --from 'elastic-observability-migration[datadog]' …
export DD_API_KEY="..." DD_APP_KEY="..." DD_SITE="datadoghq.com"
```

Verify reachability:

```bash
KIBANA_URL= obs-migrate migrate \
  --source datadog --input-mode api \
  --output-dir /tmp/dd_connect_check \
  --assets dashboards
# or, with creds in a file:  --env-file datadog_creds.env
```

(`datadog-migrate --source api …` is equivalent.)

What it does under the hood: uses the official `datadog-api-client` to call the Datadog Dashboards API (`list_dashboards`, then `get_dashboard` per id). Pulling dashboards proves the API + app keys and site are valid.

## TLS for self-signed or custom-CA clusters

Two TLS knobs apply across the migration/upload/connectivity paths the tool drives — source (Grafana/Prometheus/Loki), Elasticsearch, and Kibana — including the Node `kb-dashboard-cli` upload step where applicable (mapped to `NODE_EXTRA_CA_CERTS` / `NODE_TLS_REJECT_UNAUTHORIZED`). They live on `obs-migrate` (and the dedicated aliases):

- `--ca-cert <path>` (env `OBS_MIGRATE_CA_CERT`): verify TLS against a custom CA bundle/file. Use this for a private/internal CA — it keeps verification **on**.
- `--insecure` (env `OBS_MIGRATE_INSECURE`): skip certificate verification entirely. **Testing / trusted-network migration only.** It prints a one-time loud stderr warning and is vulnerable to interception. Prefer `--ca-cert` whenever you can.

```bash
# Internal CA (verification stays on) while checking Grafana over TLS:
KIBANA_URL= obs-migrate migrate --source grafana --input-mode api \
  --output-dir /tmp/grafana_connect_check --assets dashboards \
  --ca-cert /etc/ssl/corp-ca.pem

# Last resort for a self-signed lab cluster (verification OFF):
KIBANA_URL= obs-migrate migrate --source grafana --input-mode api \
  --output-dir /tmp/grafana_connect_check --assets dashboards \
  --insecure
```

If a connectivity check fails with a TLS error (e.g. `CERTIFICATE_VERIFY_FAILED` / self-signed certificate), reach for `--ca-cert` first; only suggest `--insecure` when the user explicitly accepts unverified TLS.

## Interpreting the result

- **Dashboards pulled (count > 0):** connection works; the user can move on to scanning/assessing.
- **HTTP 401 / 403:** credentials wrong or insufficient — re-check the env values; for Datadog confirm BOTH `DD_API_KEY` and `DD_APP_KEY`.
- **HTTP 404 / connection error:** wrong `GRAFANA_URL` / `DD_SITE` or network/VPN issue.
- **TLS / certificate error** (`CERTIFICATE_VERIFY_FAILED`, self-signed): the endpoint is reachable but its cert isn't trusted — pass `--ca-cert <bundle>` (preferred) or, only with explicit user consent, `--insecure`.
- **Zero dashboards but no error:** reachable and authenticated, but the account/org has no dashboards in scope.

Do not paste fabricated console output to the user. Report what actually printed, or describe the outcome in terms of "dashboards pulled" vs. "HTTP error".

## Do NOT

- Do **not** present `--list-dashboards` as a source connectivity test (it targets Kibana).
- Do **not** require `--data-view`, `--field-profile`, `--es-url`, or `--kibana-url` just to check the source connection.
- Do **not** invent install extras, flags, or exact log strings. If unsure of a flag, check `--help` on the relevant CLI.
- Do **not** assume a repo checkout (`.venv/bin/...`, `cp *.env.example`, `infra/`, `scripts/`). Use PyPI/`uvx` and exported env vars unless the user says they cloned the repo.

## See also

- `obs-migrate migrate --help` — authoritative flag list for the installed version (`grafana-migrate` / `datadog-migrate --help` for aliases).
- `docs/sources/grafana.md`, `docs/sources/datadog.md`, `docs/command-contract.md` — connection/auth and env-var reference (online docs / repo).
