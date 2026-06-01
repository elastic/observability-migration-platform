# Scripts

This directory holds repo-maintained helper scripts for local lab setup,
validation, reporting, and documentation refreshes.

These scripts are part of the repository workflow, but they are not the same as
the installed CLI entry points declared in `pyproject.toml`. For the canonical
command inventory and supported invocation patterns, use
`docs/command-contract.md`.

## Main Groups

- Local lab and demos: `start_local_lab.sh`, `stop_local_lab.sh`,
  `full_local_demo.sh`, `run_datadog_demo.sh`, `run_migration.sh`
- Validation and reporting: `audit_pipeline.py`, `validate_panel_queries.py`,
  `validate_dashboard_layout.py`, `generate_alert_support_report.py`,
  `verify_alert_rule_uploads.py`, `audit_migrated_rules.py`
- PromQL -> ES|QL translator dev harnesses:
  `validate_promql_esql_translations.py` (translate expressions and validate the
  emitted ES|QL on a live cluster), `validate_esql_function_semantics.py`
  (pin down the numeric semantics of individual ES|QL idioms with synthetic ROW
  data), and `parity_promql_esql_oracle.py` (numeric parity: run our translated
  ES|QL and the *same* expression through Elasticsearch's native `PROMQL` command
  on one index/window and compare per-bucket values — the native command is an
  independent PromQL implementation, so a match proves translation correctness
  without needing a live Prometheus). Source `serverless_creds.env` first; pass
  `--offline` to translate only.
- Data and setup helpers: `setup_telemetry_data.py`,
  `provision_local_kibana_data_views.sh`, `create_grafana_test_alerts.py`
- Schema and analysis helpers: `generate_dashboard_schema.sh`,
  `generate_telemetry_contract.py`
- Release hygiene: `check_licenses.py`

## Start Here

- `docs/command-contract.md` for exact command examples and expected inputs
- `docs/local-otlp-validation.md` for the local lab workflow
- `docs/dashboards/README.md` for dashboard schema and validation tooling
- `examples/alerting/README.md` for alert support reporting and verification
