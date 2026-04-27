# Examples

This directory holds example inputs, extension templates, and curated alerting
fixtures used by the migration tooling.

## What Lives Here

- `rule-pack.example.yaml` — starter Grafana rule-pack extension
- `datadog-field-profile.example.yaml` — starter Datadog field profile
- `corpus-profile.example.yaml` — starter corpus selection profile
- `plugin_example.py` — example extension module
- `cue/` — CUE siblings for selected example files
- `alerting/` — curated Grafana alert and Datadog monitor suites plus the local
  support-report workflow

## Start Here

- For alert support reporting, see `alerting/README.md`.
- For Grafana rule-pack usage, see `docs/sources/grafana.md`.
- For Datadog field-profile usage, see `docs/sources/datadog.md`.
- For tooling notes, see `docs/architecture/tooling-matrix.md`.
