# Dashboard Tooling

Dashboard authoring flow for local migration work:

- `bash scripts/generate_dashboard_schema.sh`
  Regenerates `docs/dashboards/schema.json` from `kb-dashboard-core`.
  If `npx` is available, it also writes `docs/dashboards/schema.toon` for easier schema browsing.

- `.venv/bin/python scripts/fetch_dashboards_api_schema.py --require-full-schema --url <kibana-full-openapi.yaml>`
  Fetches/checks the typed Kibana Dashboards API OpenAPI bundle for
  `/api/dashboards`. This is the native API schema refresh path; it is separate
  from the YAML bridge schema above because the Dashboards API is still
  technical preview and its full schemas may be hosted outside the standard
  Kibana OpenAPI bundle. The current native IR enforces the documented limits:
  1,000 top-level dashboard items, 1,000 panels per section, 100 pinned
  controls, and 1,000 total panels/sections/controls.

- `KIBANA_DASHBOARDS_API_SCHEMA_URL=<kibana-full-openapi.yaml> make check-native-schema`
  CI-friendly wrapper around the same native schema check. It requires the URL
  or local path explicitly so ordinary lint/test runs do not depend on live
  network availability.

- Dashboard YAML lint now runs automatically inside dashboard `migrate` runs.
  Compiled-layout validation runs when YAML-to-NDJSON compilation runs
  (`obs-migrate compile`, `migrate --compile`, or
  `migrate --upload --legacy-import`) via
  `observability_migration.targets.kibana.{lint,layout}`. These checks no
  longer have standalone scripts. To run them ad hoc:

```python
from observability_migration.targets.kibana.lint import lint_dashboard_yaml
ok, output = lint_dashboard_yaml("migration_output/dashboards/yaml")

from observability_migration.targets.kibana.layout import validate_compiled_layout
ok, output = validate_compiled_layout("migration_output/dashboards/compiled")
```

  The lint gate calls `kb-dashboard-lint`. Install the Kibana tools in-venv with
  `.venv/bin/pip install ".[kibana]"` (requires Python 3.12+); on 3.11 the
  runtime falls back to a pinned `uvx`, so `uv` must be on `PATH`. Run
  `obs-migrate doctor` to check which path is active.

The migration pipeline now targets the newer dashboard YAML conventions where possible:

- dashboard-time parameters (`?_tstart`, `?_tend`) instead of fixed one-hour windows
- `BUCKET(@timestamp, 50, ?_tstart, ?_tend)` for adaptive time bucketing
- native `gauge` configs instead of forcing gauges into `metric`
- `dimensions` for table and pie panels, matching the current dashboard guide
