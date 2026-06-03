---
name: connect-to-o11y-source
description: Connect the obs-migrate / mig-to-kbn tool to a source observability vendor (Grafana or Datadog) and prove the tool can actually reach it before any migration. Use when the user wants to connect, authenticate, point the tool at, or verify connectivity/credentials to their Grafana or Datadog instance, or asks "can the tool reach my Grafana/Datadog" / "how do I set up access".
---

# Connect to an o11y source (Grafana / Datadog)

Goal: get the user authenticated against their source vendor and **prove the tool can reach it** with the cheapest real call, before they invest in a migration.

## Core facts (do not invent around these)

- There is **no dedicated `ping`/`connect` command**. The smallest real proof of connectivity is a **live API extraction run** (`--source api`), which makes authenticated HTTP calls to the vendor.
- Credentials come from **environment variables**, conventionally loaded from a repo-local `*_creds.env` file copied from the matching `*.env.example`.
- `--list-dashboards` is **target-side (Kibana), not source-side.** It lists dashboards *in Kibana* and needs `--kibana-url`. Do **not** use it to test a Grafana/Datadog connection.
- A connectivity check is a **source-only** operation: do not add target flags like `--es-url`, `--kibana-url`, `--data-view`, or `--field-profile`. Set `KIBANA_URL=` in the shell to suppress any default local-Kibana preflight.

## Install (once)

```bash
cd <repo-root>
python3 -m venv .venv
.venv/bin/pip install -e ".[grafana]"   # or ".[datadog]", or ".[all]"
.venv/bin/obs-migrate doctor             # confirms the install + tool resolution
```

`.[grafana]` and `.[datadog]` are real optional extras. Datadog API mode **requires** `.[datadog]` (the `datadog-api-client` dependency).

## Grafana

Credentials (env): `GRAFANA_URL` plus **either** `GRAFANA_USER` + `GRAFANA_PASS` (HTTP basic auth) **or** a bearer token passed as `--grafana-token`.

```bash
cp grafana_creds.env.example grafana_creds.env
# edit: GRAFANA_URL, GRAFANA_USER, GRAFANA_PASS  (or use a token)
set -a && source grafana_creds.env && set +a
```

Verify reachability (source-only live extraction to a throwaway dir):

```bash
KIBANA_URL= .venv/bin/grafana-migrate \
  --source api \
  --output-dir /tmp/grafana_connect_check \
  --assets dashboards
# token auth instead of user/pass: add  --grafana-token "$GRAFANA_TOKEN"
```

What it does under the hood: authenticates and calls Grafana `/api/search?type=dash-db` then `/api/dashboards/uid/<uid>` (capped at 500). If it pulls one or more dashboards, the tool reached Grafana and could read them. Auth/URL failures surface as an HTTP error from `raise_for_status()` (e.g. 401/403/404 or a connection error).

## Datadog

Credentials (env, via `--env-file`): `DD_API_KEY`, `DD_APP_KEY`, and optionally `DD_SITE` (default `datadoghq.com`).

```bash
cp datadog_creds.env.example datadog_creds.env
# edit: DD_API_KEY, DD_APP_KEY, DD_SITE
.venv/bin/pip install -e ".[datadog]"
```

Verify reachability:

```bash
KIBANA_URL= .venv/bin/datadog-migrate \
  --source api \
  --env-file datadog_creds.env \
  --output-dir /tmp/dd_connect_check \
  --assets dashboards
```

What it does under the hood: uses the official `datadog-api-client` to call the Datadog Dashboards API (`list_dashboards`, then `get_dashboard` per id). Pulling dashboards proves the API + app keys and site are valid.

## Interpreting the result

- **Dashboards pulled (count > 0):** connection works; the user can move on to scanning/assessing.
- **HTTP 401 / 403:** credentials wrong or insufficient — re-check the env values; for Datadog confirm BOTH `DD_API_KEY` and `DD_APP_KEY`.
- **HTTP 404 / connection error:** wrong `GRAFANA_URL` / `DD_SITE` or network/VPN issue.
- **Zero dashboards but no error:** reachable and authenticated, but the account/org has no dashboards in scope.

Do not paste fabricated console output to the user. Report what actually printed, or describe the outcome in terms of "dashboards pulled" vs. "HTTP error".

## Do NOT

- Do **not** present `--list-dashboards` as a source connectivity test (it targets Kibana).
- Do **not** require `--data-view`, `--field-profile`, `--es-url`, or `--kibana-url` just to check the source connection.
- Do **not** invent install extras, flags, or exact log strings. If unsure of a flag, check `--help` on the relevant CLI.

## See also

- `docs/sources/grafana.md`, `docs/sources/datadog.md` — adapter connection/auth details.
- `docs/command-contract.md` — canonical env vars and command surface.
