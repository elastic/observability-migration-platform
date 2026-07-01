---
name: connect-to-o11y-source
description: Use when the user wants to connect, authenticate, point the tool at, or verify connectivity/credentials to their Grafana or Datadog instance, or asks "can the tool reach my Grafana/Datadog" / "how do I set up access" — connects the obs-migrate / mig-to-kbn tool to a source observability vendor (Grafana or Datadog) and proves it can actually reach it before any migration.
---

# Connect to an o11y source (Grafana / Datadog)

Goal: get the user authenticated against their source vendor and **prove the tool can reach it** with the cheapest real call, before they invest in a migration.

## Which command form to use (package vs. repo)

Most consumers have **installed the package** (`pip install 'obs-migrate[grafana]'`), so the CLIs are on `PATH`: call `obs-migrate`, `grafana-migrate`, `datadog-migrate` directly. Only inside a source checkout do you prefix `.venv/bin/`. This skill uses the bare (package) form; prefix `.venv/bin/` if and only if the user is working from a cloned repo. Do not assume a repo, `infra/`, `examples/`, or `scripts/` directory exists.

## Core facts (do not invent around these)

- There is **no dedicated `ping`/`connect` command**. The smallest real proof of connectivity is a **live API extraction run** (`--source api`), which makes authenticated HTTP calls to the vendor.
- Credentials come from **environment variables** (export them in the shell, or keep them in a local env file you `source`).
- `--list-dashboards` is **target-side (Kibana), not source-side.** It lists dashboards *in Kibana* and needs `--kibana-url`. Do **not** use it to test a Grafana/Datadog connection.
- A connectivity check is a **source-only** operation: do not add target flags like `--es-url`, `--kibana-url`, `--data-view`, or `--field-profile`. Set `KIBANA_URL=` in the shell to suppress any default local-Kibana preflight.

## Install (once)

```bash
pip install 'obs-migrate[grafana]'   # or 'obs-migrate[datadog]', or 'obs-migrate[all]'
obs-migrate doctor                   # confirms the install + tool resolution
```

`grafana` and `datadog` are real optional extras. Datadog API mode **requires** the `datadog` extra (the `datadog-api-client` dependency). (From a repo checkout: `python3 -m venv .venv && .venv/bin/pip install -e ".[grafana]"`.)

## Grafana

Credentials are **flag-first with env fallback** (each flag defaults to its env var, matching how Elasticsearch/Kibana creds work): `--grafana-url` (env `GRAFANA_URL`) plus **either** `--grafana-user` + `--grafana-pass` (env `GRAFANA_USER` / `GRAFANA_PASS`, HTTP basic auth) **or** a bearer token via `--grafana-token` (env `GRAFANA_TOKEN`).

```bash
# Option A — environment variables
export GRAFANA_URL="https://grafana.example.com"
export GRAFANA_USER="..." GRAFANA_PASS="..."   # or a token below
```

Verify reachability (source-only live extraction to a throwaway dir):

```bash
KIBANA_URL= grafana-migrate \
  --source api \
  --output-dir /tmp/grafana_connect_check \
  --assets dashboards
# token auth instead of user/pass: add  --grafana-token "$GRAFANA_TOKEN"
```

Option B — pass connection details as flags instead of exporting env vars (useful in CI or when juggling multiple instances):

```bash
KIBANA_URL= grafana-migrate \
  --source api \
  --grafana-url "https://grafana.example.com" \
  --grafana-user "$GF_USER" --grafana-pass "$GF_PASS" \
  --output-dir /tmp/grafana_connect_check \
  --assets dashboards
```

The same `--grafana-url/--grafana-user/--grafana-pass/--grafana-token` flags exist on `obs-migrate migrate --source grafana` and are forwarded through.

What it does under the hood: authenticates and calls Grafana `/api/search?type=dash-db` then `/api/dashboards/uid/<uid>` (capped at 500). If it pulls one or more dashboards, the tool reached Grafana and could read them. Auth/URL failures surface as an HTTP error from `raise_for_status()` (e.g. 401/403/404 or a connection error).

## Datadog

Credentials (env): `DD_API_KEY`, `DD_APP_KEY`, and optionally `DD_SITE` (default `datadoghq.com`). You can export them or put them in an env file passed via `--env-file`.

```bash
pip install 'obs-migrate[datadog]'
export DD_API_KEY="..." DD_APP_KEY="..." DD_SITE="datadoghq.com"
```

Verify reachability:

```bash
KIBANA_URL= datadog-migrate \
  --source api \
  --output-dir /tmp/dd_connect_check \
  --assets dashboards
# or, with creds in a file:  --env-file datadog_creds.env
```

What it does under the hood: uses the official `datadog-api-client` to call the Datadog Dashboards API (`list_dashboards`, then `get_dashboard` per id). Pulling dashboards proves the API + app keys and site are valid.

## TLS for self-signed or custom-CA clusters

Two TLS knobs apply across the migration/upload/connectivity paths the tool drives — source (Grafana/Prometheus/Loki), Elasticsearch, and Kibana — including the Node `kb-dashboard-cli` upload step where applicable (mapped to `NODE_EXTRA_CA_CERTS` / `NODE_TLS_REJECT_UNAUTHORIZED`). They live on the package CLIs used in this skill (`obs-migrate`, `grafana-migrate`, and `datadog-migrate`):

- `--ca-cert <path>` (env `OBS_MIGRATE_CA_CERT`): verify TLS against a custom CA bundle/file. Use this for a private/internal CA — it keeps verification **on**.
- `--insecure` (env `OBS_MIGRATE_INSECURE`): skip certificate verification entirely. **Testing / trusted-network migration only.** It prints a one-time loud stderr warning and is vulnerable to interception. Prefer `--ca-cert` whenever you can.

```bash
# Internal CA (verification stays on) while checking Grafana over TLS:
KIBANA_URL= grafana-migrate --source api \
  --output-dir /tmp/grafana_connect_check --assets dashboards \
  --ca-cert /etc/ssl/corp-ca.pem

# Last resort for a self-signed lab cluster (verification OFF):
KIBANA_URL= grafana-migrate --source api \
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
- Do **not** assume a repo checkout (`.venv/bin/...`, `cp *.env.example`, `infra/`, `scripts/`). Use the on-PATH CLIs and exported env vars unless the user says they cloned the repo.

## See also

- `grafana-migrate --help` / `datadog-migrate --help` — authoritative flag list for the installed version.
- `docs/sources/grafana.md`, `docs/sources/datadog.md`, `docs/command-contract.md` — connection/auth and env-var reference (online docs / repo).
