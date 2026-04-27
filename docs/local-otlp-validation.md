# Local OTLP Validation Lab

This guide shows how to run the repo end to end on a local machine using the built-in OTLP validation lab.

The goal is to make it easy to answer this question:

> Can this repo translate Grafana dashboards, upload the results to Kibana, and validate the uploaded dashboards against live Elasticsearch data?

The answer is now yes, using the scripts and infrastructure in this guide.

For the directory-level lab and fixture inventory, see `infra/README.md`.

## What You Get

The local lab gives you:

- a source-side Grafana with sample dashboards
- Prometheus and Loki backing the source dashboards
- an OpenTelemetry Collector that receives OTLP and scrapes Prometheus targets
- Elasticsearch and Kibana as the migration target
- optional Grafana Alloy in front of the collector for OTLP traffic
- helper scripts for bring-up, data-view provisioning, sample validation, and teardown

## Prerequisites

Install or verify:

- Docker with `docker compose`
- Python 3
- a project virtual environment with the repo installed in editable mode
- `uvx` so `kb-dashboard-cli` can compile and upload dashboards
- optional Chrome or Chromium if you want screenshot capture during smoke validation

Recommended setup:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

## Why The Lab Uses Non-Default Ports

The lab is intentionally isolated from the “usual” local observability ports so it does not collide with an existing Elasticsearch, Kibana, Prometheus, or OTLP setup on your machine.

Default host ports:

| Service | URL or Port |
| --- | --- |
| Grafana | `http://localhost:13000` |
| Elasticsearch | `http://localhost:19200` |
| Kibana | `http://localhost:15601` |
| Collector OTLP gRPC | `localhost:24317` |
| Collector OTLP HTTP | `http://localhost:24318` |
| Collector health | `http://localhost:23133` |
| Alloy UI | `http://localhost:12345` |
| Alloy OTLP gRPC | `localhost:14317` |
| Alloy OTLP HTTP | `http://localhost:14318` |

You can override these with environment variables such as `LOCAL_ES_PORT`, `LOCAL_KIBANA_PORT`, and `LOCAL_GRAFANA_PORT`.

## Quick Start

### 1. Start the default lab

```bash
bash scripts/start_local_lab.sh
```

This starts:

- Grafana
- Prometheus-backed sample targets
- Loki and Promtail
- OpenTelemetry Collector
- Elasticsearch
- Kibana
- sample OTLP telemetry generators

### 2. Run the sample migration and validation flow

```bash
bash scripts/full_local_demo.sh --sample-set bundled
```

This script:

1. checks Elasticsearch and Kibana readiness
2. waits for `metrics-*` and `logs-*` data to become queryable
3. provisions Kibana data views for `metrics-*`, `logs-*`, and `traces-*`
4. selects three representative Grafana dashboards
5. runs the migration pipeline with live ES|QL validation
6. uploads the compiled dashboards to Kibana
7. runs saved-object smoke validation
8. runs browser-side dashboard audit checks
9. captures screenshots by default

Artifacts are written to:

- `validation/local_otlp_sample_run`

Important files:

- `validation/local_otlp_sample_run/migration_report.json`
- `validation/local_otlp_sample_run/migration_manifest.json`
- `validation/local_otlp_sample_run/verification_packets.json`
- `validation/local_otlp_sample_run/upload_smoke_report.json`
- `validation/local_otlp_sample_run/browser_qa`
- `validation/local_otlp_sample_run/dashboard_qa`

### 3. Stop the lab

```bash
bash scripts/stop_local_lab.sh
```

To remove named volumes as well:

```bash
bash scripts/stop_local_lab.sh --volumes
```

## One-Command Clean Sample Run

If you want a clean recreate of the lab and a full sample validation in one step:

```bash
bash scripts/full_local_demo.sh --sample-set bundled --recreate-lab
```

That is the easiest command to hand to a new contributor.

## Optional Alloy Mode

To run the same flow with Grafana Alloy in front of OTLP traffic:

```bash
bash scripts/full_local_demo.sh --sample-set bundled --with-alloy --recreate-lab
```

What changes in Alloy mode:

- an Alloy container is started from `infra/alloy/config.alloy`
- the OTLP generator containers are rerouted through Alloy with `OTLP_FORWARD_TARGET=alloy:14317`
- Alloy forwards OTLP traffic to the existing OTel Collector
- the collector still exports into Elasticsearch

What does **not** change:

- Prometheus scrape targets still go straight into the collector’s Prometheus receiver
- the migration logic does not change
- the validation flow does not change

This keeps Alloy as an optional front end instead of making it a hard dependency.

## Sample Dashboards Used By The Sample Mode

`bash scripts/full_local_demo.sh --sample-set bundled` uses three dashboards from `infra/grafana/dashboards`:

- `node-exporter-full.json`
- `k8s-views-global.json`
- `diverse-panels-test.json`

These give you a reasonable mix of:

- large Prometheus-node style dashboards
- Kubernetes cluster views
- mixed panel semantics, including placeholder/manual cases

## Manual Commands Behind The Helper Script

If you prefer to run the steps manually, the bundled sample mode is roughly doing this:

```bash
.venv/bin/python -m observability_migration.adapters.source.grafana.cli \
  --source files \
  --input-dir <sample-input-dir> \
  --output-dir validation/local_otlp_sample_run \
  --data-view "metrics-*" \
  --esql-index "metrics-*" \
  --logs-index "logs-*" \
  --es-url "http://localhost:19200" \
  --validate \
  --upload \
  --kibana-url "http://localhost:15601"
```

Then it runs:

```bash
.venv/bin/python -m observability_migration.adapters.source.grafana.validate_uploaded_dashboards \
  --kibana-url "http://localhost:15601" \
  --es-url "http://localhost:19200" \
  --output validation/local_otlp_sample_run/upload_smoke_report.json \
  --browser-audit \
  --browser-audit-dir validation/local_otlp_sample_run/browser_qa \
  --capture-screenshots \
  --screenshot-dir validation/local_otlp_sample_run/dashboard_qa \
  --fail-on-runtime-errors \
  --fail-on-layout-issues \
  --fail-on-not-runtime-checked \
  --fail-on-browser-errors \
  --dashboard-title "Node Exporter Full" \
  --dashboard-title "Kubernetes / Views / Global" \
  --dashboard-title "Diverse Panel Types Test"
```

## Kibana Data Views

Fresh Kibana instances do not automatically have the data views needed by uploaded dashboards.

That is why the repo now includes:

```bash
bash scripts/provision_local_kibana_data_views.sh
```

This script creates or updates saved objects with fixed IDs for:

- `metrics-*`
- `logs-*`
- `traces-*`

The fixed IDs matter because the compiled dashboards expect those references to exist in Kibana.

## What Counts As A Good Validation Result

A good run does **not** require every panel to be green.

In this repo, a successful end-to-end lab run means:

- the infrastructure started
- data reached Elasticsearch
- dashboards translated and compiled
- dashboards uploaded to Kibana
- smoke validation ran against the uploaded saved objects without hard runtime failures, layout failures, browser-visible errors, or unvalidated query panels

Some panels can still be empty because the local lab does not emit every field or metric family required by the original Grafana dashboards.

That is expected and useful. The lab is supposed to reveal:

- missing metrics
- missing labels
- unsupported source semantics
- panels that need manual redesign

The validation reports are there to make those gaps explicit.

## Troubleshooting

### Upload fails with missing `index-pattern:*` references

Run:

```bash
bash scripts/provision_local_kibana_data_views.sh
```

Then rerun the validation flow.

### Elasticsearch queries work, but some dashboards still show runtime panel errors

Open:

- `migration_report.json`
- `verification_packets.json`
- `upload_smoke_report.json`

These will usually show whether the issue is:

- missing source metrics in the lab
- missing labels
- a translation gap
- a query that compiled but still fails at runtime
- a panel that did not expose a runnable ES|QL query after upload

### The sample dashboards upload, but some panels are empty

That usually means the required telemetry is not present in the current lab dataset. You can either:

- accept the result as a real validation signal
- use `observability_migration.adapters.source.grafana.corpus` or `grafana-generate-corpus` to backfill synthetic data for missing fields and metrics

### Screenshots are skipped

That means the smoke validator could not find Chrome or Chromium. The dashboard validation still runs, but screenshot capture is omitted.

### You already have local Elasticsearch or Kibana running

That is exactly why the lab uses `19200`, `15601`, and `13000` by default. If those are still taken, override them with environment variables before starting the lab.

Example:

```bash
export LOCAL_ES_PORT=29200
export LOCAL_KIBANA_PORT=25601
export LOCAL_GRAFANA_PORT=23000
bash scripts/start_local_lab.sh
```

## Where To Go Next

After you are comfortable running the lab:

1. read `docs/architecture.md`
2. inspect `observability_migration/adapters/source/grafana/cli.py`, `translate.py`, and `panels.py`
3. inspect `observability_migration/core/assets/query.py`
4. inspect `observability_migration/adapters/source/grafana/verification.py`

That path gives you the operational view first and the implementation details second.
