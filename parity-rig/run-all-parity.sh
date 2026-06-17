#!/usr/bin/env bash
# Migrate every fixture dashboard + the canonical 1860 against the parity-rig
# data stream and run the parity harness against each. Aggregates the
# per-dashboard verdict counts and writes the combined report to
# parity-rig/reports/parity-all.json.
#
# Usage: bash run-all-parity.sh
#
# Requires:
# - parity-rig containers running (docker compose up -d)
# - Credentials exporting ELASTICSEARCH_ENDPOINT, KEY — either already in
#   the environment, or in a creds file at $CREDS_FILE (defaults to
#   serverless_creds.env in the repo root).
# - A .venv in the repo root (override the interpreter with $PYTHON).
set -euo pipefail

# Derive paths from this script's location: it lives in <repo>/parity-rig.
RIG=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORKTREE=$(cd "$RIG/.." && pwd)
REPORTS=$RIG/reports/parity-all
mkdir -p "$REPORTS"

PYTHON="${PYTHON:-$WORKTREE/.venv/bin/python}"
CREDS_FILE="${CREDS_FILE:-$WORKTREE/serverless_creds.env}"
if [[ -f "$CREDS_FILE" ]]; then
  set -a
  source "$CREDS_FILE"
  set +a
fi

for var in ELASTICSEARCH_ENDPOINT KEY; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: $var not set (export it, or provide it via \$CREDS_FILE)" >&2
    exit 2
  fi
done

# Map each fixture / dashboard JSON to a short slug.
declare -a DASHBOARDS=(
  "diverse-panels-test:Diverse Panel Types Test:/tmp/mig-to-kbn-e2e/input-all/diverse-panels-test.json"
  "home:Home - Migration Test Lab:/tmp/mig-to-kbn-e2e/input-all/home.json"
  "k8s-views-global:Kubernetes / Views / Global:/tmp/mig-to-kbn-e2e/input-all/k8s-views-global.json"
  "node-exporter-full:Node Exporter Full:/tmp/mig-to-kbn-e2e/input-all/node-exporter-full.json"
  "prometheus-all:Prometheus 2.0 (by FUSAKLA):/tmp/mig-to-kbn-e2e/input-all/prometheus-all.json"
  "node-exporter-full-1860:Node Exporter Full (canonical 1860):$RIG/grafana/dashboards/node-exporter-full-1860.json"
  "express-prometheus-middleware:Express Prometheus Middleware:/tmp/mig-to-kbn-e2e/input-express/express-prometheus-middleware.json"
)

cd "$WORKTREE"

for entry in "${DASHBOARDS[@]}"; do
  IFS=':' read -r slug title path <<<"$entry"
  echo
  echo "============================================================"
  echo "  $title  ($slug)"
  echo "============================================================"
  INPUT=/tmp/mig-to-kbn-e2e/parity-input-$slug
  OUTPUT=/tmp/mig-to-kbn-e2e/parity-out-$slug
  mkdir -p "$INPUT"
  cp "$path" "$INPUT/dashboard.json"

  $PYTHON -m observability_migration.adapters.source.grafana.cli \
    --input-dir "$INPUT" \
    --output-dir "$OUTPUT" \
    --assets dashboards \
    --es-url "$ELASTICSEARCH_ENDPOINT" \
    --es-api-key "$KEY" \
    --data-view metrics-express.prometheus-parity \
    --esql-index metrics-express.prometheus-parity 2>&1 | rg -i 'PROMQL.*detected|Migrated' | head -3 || true

  REPORT_PATH="$OUTPUT/dashboards/migration_report.json"
  if [ ! -f "$REPORT_PATH" ]; then
    echo "  WARNING: no migration report at $REPORT_PATH (migration may have errored)"
    continue
  fi

  # Run the parity harness against the migrated panels.
  PROM_URL=http://localhost:29090 \
  ESQL_INDEX=metrics-express.prometheus-parity \
  REPORT_PATH="$REPORT_PATH" \
  OUTPUT_DIR="$REPORTS/$slug" \
  PARITY_WINDOW_MINUTES=10 \
  PARITY_STEP_SECONDS=60 \
  $PYTHON "$RIG/harness/parity.py" 2>&1 | tail -n +2 | sed 's/^/  /' | grep -E 'STRICT|FUZZY|SHAPE|FAIL|SKIP|ERROR|Verdict' || true
done

echo
echo "============================================================"
echo "Aggregate summary"
echo "============================================================"
$PYTHON <<PY
import json
from pathlib import Path
from collections import Counter

reports_dir = Path("$REPORTS")
overall = Counter()
per_dashboard = {}
for sub in sorted(reports_dir.iterdir()):
    if not sub.is_dir(): continue
    rep_path = sub / "parity-report.json"
    if not rep_path.exists(): continue
    d = json.loads(rep_path.read_text())
    counts = Counter(d.get("verdict_counts", {}))
    overall.update(counts)
    per_dashboard[sub.name] = counts

print(f"{'Dashboard':50s} {'STRICT':>7} {'FUZZY':>7} {'SHAPE':>7} {'FAIL_NO':>7} {'FAIL':>7} {'SKIP':>5} {'ERROR':>6} {'Total':>5}")
print('-' * 110)
for name, c in per_dashboard.items():
    total = sum(c.values())
    print(f"{name:50s} {c.get('STRICT_PASS',0):>7} {c.get('FUZZY_PASS',0):>7} {c.get('SHAPE_PASS',0):>7} {c.get('FAIL_NO_OVERLAP',0):>7} {c.get('FAIL',0):>7} {c.get('SKIP',0):>5} {c.get('ERROR',0):>6} {total:>5}")
print('-' * 110)
total = sum(overall.values())
print(f"{'OVERALL':50s} {overall.get('STRICT_PASS',0):>7} {overall.get('FUZZY_PASS',0):>7} {overall.get('SHAPE_PASS',0):>7} {overall.get('FAIL_NO_OVERLAP',0):>7} {overall.get('FAIL',0):>7} {overall.get('SKIP',0):>5} {overall.get('ERROR',0):>6} {total:>5}")

# Save combined report
combined = {
    "per_dashboard": {k: dict(v) for k,v in per_dashboard.items()},
    "overall": dict(overall),
}
out = reports_dir / "_combined.json"
out.write_text(json.dumps(combined, indent=2))
print(f"\nCombined report: {out}")
PY
