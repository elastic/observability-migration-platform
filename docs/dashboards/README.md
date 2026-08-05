# Dashboard Tooling

Dashboard authoring flow for local migration work:

- `bash scripts/generate_dashboard_schema.sh`
  Regenerates `docs/dashboards/schema.json` from `kb-dashboard-core`. No YAML
  file is produced or consumed anywhere in the engine any more; this schema is
  kept because it documents the internal in-memory dict shape that
  `DashboardIR.to_yaml_dict()` produces and `dashboards_api`'s `*_yaml_*`
  mappers accept.

- `docs/dashboards/kibana_dashboards_api.openapi.yaml`
  Pinned native Kibana Dashboards API OpenAPI bundle for `/api/dashboards`.
  This is the schema authority for what the typed upload path may emit.
  Refresh with `make refresh-native-schema` (or
  `.venv/bin/python scripts/fetch_dashboards_api_schema.py --require-full-schema`)
  and commit the result when intentionally bumping the pin. The Dashboards API
  may still ship full schemas outside the standard Kibana OpenAPI bundle, so
  the refresh URL defaults to Elastic's hosted Dashboards API spec.

- `make check-native-schema`
  CI-friendly validation of the **committed** OpenAPI pin above
  (`--require-full-schema`). Override `KIBANA_DASHBOARDS_API_SCHEMA_URL` only
  when refreshing from a different upstream bundle via
  `make refresh-native-schema`.

The current native IR enforces the documented limits: 1,000 top-level dashboard
items, 1,000 panels per section, 100 pinned controls, and 1,000 total
panels/sections/controls.

- **The dashboard-YAML lint stage and the compiled-layout validation stage were
  removed**, along with `obs-migrate compile` and `migrate
  --compile`/`--legacy-import`. A migration writes only
  `dashboards/native/*.native.json` and `dashboards/ir/*.ir.json`, and uploads
  the native payload through `PUT /api/dashboards/{id}`. Nothing shells out to
  `kb-dashboard-cli` or `kb-dashboard-lint`, so `uv` is no longer needed on
  Python 3.11 for dashboard work.

  `observability_migration.targets.kibana.lint` and `.layout` still exist as
  library code, but no user-facing command calls `lint_dashboard_yaml` or
  `validate_compiled_layout`, and there is no `yaml/` or `compiled/` directory to
  point them at. The one check that carried over is the `?param`/`??param`
  control-binding gate (`lint.unbound_param_findings`), which the interaction
  audit runs against the IR export — see
  `docs/contributing/dev-commands.md`.

The migration pipeline now targets the newer dashboard conventions where possible:

- dashboard-time parameters (`?_tstart`, `?_tend`) instead of fixed one-hour windows
- `BUCKET(@timestamp, 50, ?_tstart, ?_tend)` for adaptive time bucketing
- native `gauge` configs instead of forcing gauges into `metric`
- `dimensions` for table and pie panels, matching the current dashboard guide
