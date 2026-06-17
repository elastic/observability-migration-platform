#!/usr/bin/env bash
# Re-migrate every parity fixture dashboard with --upload + --ensure-data-views
# so they land in the target Kibana cluster's default space. Same input set
# and CLI flags as run-all-parity.sh (this script is the rig's "deploy"
# counterpart to that "verify" driver).
#
# Usage: bash upload-all.sh
#
# Requires:
# - Credentials exporting ELASTICSEARCH_ENDPOINT, KIBANA_ENDPOINT, KEY —
#   either already in the environment, or in a creds file at $CREDS_FILE
#   (defaults to serverless_creds.env in the repo root).
# - A .venv in the repo root (override the interpreter with $PYTHON).
set -euo pipefail

# Derive paths from this script's location: it lives in <repo>/parity-rig.
RIG=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORKTREE=$(cd "$RIG/.." && pwd)

PYTHON="${PYTHON:-$WORKTREE/.venv/bin/python}"
CREDS_FILE="${CREDS_FILE:-$WORKTREE/serverless_creds.env}"
if [[ -f "$CREDS_FILE" ]]; then
  set -a
  source "$CREDS_FILE"
  set +a
fi

for var in ELASTICSEARCH_ENDPOINT KIBANA_ENDPOINT KEY; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: $var not set (export it, or provide it via \$CREDS_FILE)" >&2
    exit 2
  fi
done

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

SUCCESS=()
FAIL=()
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

  if $PYTHON -m observability_migration.adapters.source.grafana.cli \
       --input-dir "$INPUT" \
       --output-dir "$OUTPUT" \
       --assets dashboards \
       --es-url "$ELASTICSEARCH_ENDPOINT" \
       --es-api-key "$KEY" \
       --data-view metrics-express.prometheus-parity \
       --esql-index metrics-express.prometheus-parity \
       --upload \
       --kibana-url "$KIBANA_ENDPOINT" \
       --kibana-api-key "$KEY" \
       --ensure-data-views 2>&1 | tee "$OUTPUT/upload.log" \
       | rg -i 'PROMQL.*detected|Migrated|Upload|Dashboard|data view|Error|Warn' | head -20; then
    SUCCESS+=("$slug")
  else
    FAIL+=("$slug")
  fi
done

echo
echo "============================================================"
echo "Upload summary"
echo "============================================================"
echo "  SUCCESS (${#SUCCESS[@]}):"
for s in "${SUCCESS[@]}"; do echo "    - $s"; done
if [[ ${#FAIL[@]} -gt 0 ]]; then
  echo "  FAILED (${#FAIL[@]}):"
  for s in "${FAIL[@]}"; do echo "    - $s"; done
  exit 1
fi
echo
echo "Open Kibana → Dashboards: $KIBANA_ENDPOINT/app/dashboards"
