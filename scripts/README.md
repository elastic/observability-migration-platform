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
