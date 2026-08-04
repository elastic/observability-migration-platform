# Dashboard Tooling

Dashboard authoring flow for local migration work:

- `bash scripts/generate_dashboard_schema.sh`
  Regenerates `docs/dashboards/schema.json` from `kb-dashboard-core`. No YAML
  file is produced or consumed anywhere in the engine any more; this schema is
  kept because it documents the internal in-memory dict shape that
  `DashboardIR.to_yaml_dict()` produces and `dashboards_api`'s `*_yaml_*`
  mappers accept.
  If `npx` is available, it also writes `docs/dashboards/schema.toon` for easier schema browsing.

- `.venv/bin/python scripts/fetch_dashboards_api_schema.py --require-full-schema`
  Fetches/checks the typed Kibana Dashboards API OpenAPI bundle for
  `/api/dashboards`. This is the native API schema refresh path — and the one
  that governs what actually ships. It is separate
  from the dict-shape schema above because the Dashboards API is still
  technical preview and its full schemas may be hosted outside the standard
  Kibana OpenAPI bundle. The current native IR enforces the documented limits:
  1,000 top-level dashboard items, 1,000 panels per section, 100 pinned
  controls, and 1,000 total panels/sections/controls.

- `make check-native-schema`
  CI-friendly wrapper around the same native schema check. By default it checks
  the live external Dashboards API bundle. Override
  `KIBANA_DASHBOARDS_API_SCHEMA_URL` when you want to validate against a pinned
  local copy or a different external bundle.

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
