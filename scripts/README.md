# Scripts

This directory holds repo-maintained helper scripts for local lab setup,
migration runs, validation, parity testing, reporting, and documentation
refreshes.

These scripts are part of the repository workflow, but they are not the same as
the installed CLI entry points declared in `pyproject.toml`. For the canonical
command inventory and supported invocation patterns, use
`docs/command-contract.md`.

## Main Groups

### Local lab and demos

- `start_local_lab.sh`, `stop_local_lab.sh` — bring the local Compose lab up/down
- `full_local_demo.sh` — end-to-end local Grafana demo against the lab
- `run_datadog_demo.sh` — end-to-end local Datadog demo against the lab
- `run_migration.sh` — convenience wrapper around a single migration run
- `provision_local_kibana_data_views.sh` — create the data views the demos expect

### End-to-end migration harnesses (live cluster)

These migrate the bundled source dashboards against a real serverless cluster.
They expect `serverless_creds.env` in the repo root and a `.venv` with the
package installed. Outputs land under `/tmp/mig-to-kbn-e2e/` (Grafana) and
`e2e_datadog_run/` (Datadog).

- `run_e2e_grafana.sh` — migrate all bundled Grafana dashboards (+ optional upload)
- `run_e2e_datadog.sh` — migrate all bundled Datadog sample/integration dashboards
- `run_seed_data.sh` — seed synthetic telemetry into the cluster for the
  migrated artifacts so panels render (calls `setup_telemetry_data.py`)

### Validation and reporting

- `audit_pipeline.py` — audit every bundled dashboard through the full pipeline
  and regenerate the trace docs (`--update-docs`)
- `validate_panel_queries.py` — validate emitted panel queries against a cluster
  (used by `run_migration.sh`)
- `validate_panels_from_yaml.py` — counter-aware ES|QL reconstruction validator
  that mirrors the Kibana time picker; pairs with `lens_reconstruct.py`
- `lens_reconstruct.py` — rebuild Lens panel ES|QL for the validator above
- `generate_alert_support_report.py`, `verify_alert_rule_uploads.py`,
  `audit_migrated_rules.py` — alert migration support reporting and verification

### PromQL / Datadog parity harnesses

- `parity_promql_esql_oracle.py` — numeric parity: run our translated ES|QL and
  the *same* expression through Elasticsearch's native `PROMQL` command on one
  index/window and compare per-bucket values. The native command is an
  independent PromQL implementation, so a match proves translation correctness
  without a live Prometheus. Source `serverless_creds.env` first; pass
  `--offline` to translate only.
- `validate_promql_esql_translations.py` — translate expressions and validate
  the emitted ES|QL on a live cluster.
- `validate_esql_function_semantics.py` — pin down the numeric semantics of
  individual ES|QL idioms with synthetic ROW data.
- `run_parity_native_profile.sh` — parity harness for the native
  `/_prometheus` endpoint schema profile (`--no-native-promql` path).
- `run_datadog_parity.py`, `run_datadog_parity.sh` — Datadog↔Elasticsearch
  parity orchestrator (see `parity-rig/datadog/README.md`).

### Data and setup helpers

- `setup_telemetry_data.py` — generate and index synthetic telemetry for a set
  of migrated dashboard artifacts
- `create_grafana_test_alerts.py` — create test alert rules in a Grafana instance
- `generate_routing_artifacts.py` — emit producer-side routing config
  (OTel Collector / Prometheus relabel / Elastic Agent) so live telemetry uses
  the field names the migrated dashboards expect

### Schema and analysis helpers

- `generate_dashboard_schema.sh` — regenerate the dashboard YAML JSON schema
- `generate_telemetry_contract.py` — emit the telemetry field contract

### Release and repo hygiene

- `check_licenses.py` — license gate / `THIRD_PARTY_NOTICES.md` refresh (CI)
- `check_source_headers.py` — enforce SPDX/source headers (CI, pre-commit)
- `check_local_paths.py` — block committing machine-local absolute paths (pre-commit)

## Start Here

- `docs/command-contract.md` for exact command examples and expected inputs
- `docs/local-otlp-validation.md` for the local lab workflow
- `docs/dashboards/README.md` for dashboard schema and validation tooling
- `examples/alerting/README.md` for alert support reporting and verification
- `parity-rig/README.md` for the live parity rigs
