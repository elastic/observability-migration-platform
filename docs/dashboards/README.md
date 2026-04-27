# Dashboard Tooling

Dashboard authoring flow for local migration work:

- `bash scripts/generate_dashboard_schema.sh`
  Regenerates `docs/dashboards/schema.json` from `kb-dashboard-core`.
  If `npx` is available, it also writes `docs/dashboards/schema.toon` for easier schema browsing.

- `bash scripts/validate_dashboard_yaml.sh <output-dir>/yaml`
  Runs `kb-dashboard-lint` against generated dashboard YAML before compile or upload.

- `python scripts/validate_dashboard_layout.py <output-dir>/compiled`
  Checks compiled dashboard artifacts for out-of-bounds panels and grid overlaps before upload.

The migration pipeline now targets the newer dashboard YAML conventions where possible:

- dashboard-time parameters (`?_tstart`, `?_tend`) instead of fixed one-hour windows
- `BUCKET(@timestamp, 50, ?_tstart, ?_tend)` for adaptive time bucketing
- native `gauge` configs instead of forcing gauges into `metric`
- `dimensions` for table and pie panels, matching the current dashboard guide
