# Dashboard Tooling

Dashboard authoring flow for local migration work:

- `bash scripts/generate_dashboard_schema.sh`
  Regenerates `docs/dashboards/schema.json` from `kb-dashboard-core`.
  If `npx` is available, it also writes `docs/dashboards/schema.toon` for easier schema browsing.

- Dashboard YAML lint and compiled-layout validation now run **automatically**
  inside `obs-migrate compile`/`migrate` (in-process, via
  `observability_migration.targets.kibana.{lint,layout}`). They no longer have
  standalone scripts. To run them ad hoc:

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
