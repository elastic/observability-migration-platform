#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

#
# End-to-end migration pipeline:
#   1. Migrate Grafana dashboards → Kibana YAML  (with --native-promql)
#   2. Extract required metrics from compiled YAML
#   3. Generate & ingest synthetic data (with preflight validation)
#   4. Upload compiled dashboards to Kibana
#   5. Validate every panel query against live ES cluster
#
# Usage:
#   ./scripts/run_migration.sh                # full pipeline
#   ./scripts/run_migration.sh --skip-data    # skip data generation (steps 2-3)
#   ./scripts/run_migration.sh --skip-upload  # skip upload + validate (steps 4-5)
#
# Prerequisites:
#   - serverless_creds.env in project root
#   - .venv with requirements.txt installed
#   - uvx kb-dashboard-cli on PATH (for compile step)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

SKIP_DATA=false
SKIP_UPLOAD=false
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      cat <<'EOF'
Usage: bash scripts/run_migration.sh [options]

Runs the Grafana native-PROMQL migration flow with optional data generation and upload checks.

Options:
  --skip-data     Skip synthetic data extraction/generation (steps 2-3).
  --skip-upload   Skip upload and panel runtime validation (steps 4-5).
  -h, --help      Show this help text.
EOF
      exit 0
      ;;
    --skip-data)   SKIP_DATA=true ;;
    --skip-upload) SKIP_UPLOAD=true ;;
    *)
      echo "ERROR: Unknown argument: $arg" >&2
      echo "Run with --help to see supported options." >&2
      exit 1
      ;;
  esac
done

VENV=".venv/bin/python"
if [ ! -f "$VENV" ]; then
  echo "ERROR: .venv not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e ."
  exit 1
fi

if [ ! -f serverless_creds.env ]; then
  echo "ERROR: serverless_creds.env not found in project root."
  exit 1
fi

set -a && source serverless_creds.env && set +a

INPUT_DIR="infra/grafana/dashboards"
OUTPUT_DIR="migration_output_native"
ALERT_ARTIFACT_DIR="$OUTPUT_DIR/alerts"
DASHBOARD_YAML_DIR="$OUTPUT_DIR/dashboards/yaml"
COMPILED_DIR="$OUTPUT_DIR/dashboards/compiled"
RUN_SUMMARY="$OUTPUT_DIR/run_summary.json"
DATA_VIEW="metrics-*"
ESQL_INDEX="metrics-*"

echo ""
echo "============================================================"
echo "  Step 1: Migrate Grafana → Kibana YAML (native PROMQL)"
echo "============================================================"
$VENV -m observability_migration.adapters.source.grafana.cli \
  --source files \
  --assets dashboards \
  --input-dir "$INPUT_DIR" \
  --output-dir "$OUTPUT_DIR" \
  --native-promql \
  --data-view "$DATA_VIEW" \
  --esql-index "$ESQL_INDEX"

if [ "$SKIP_DATA" = false ]; then
  echo ""
  echo "============================================================"
  echo "  Step 2: Generate & ingest synthetic telemetry data"
  echo "============================================================"
  # Remove leftover data streams that overlap metrics-*/logs-* but were not
  # created by this seeder (old parity/experiment streams). Their incompatible
  # mappings make shared fields conflict across the wildcard, so panels querying
  # metrics-* return zero rows. Default on for parity with run_seed_data.sh; set
  # PURGE_FOREIGN_STREAMS=0 to skip.
  PURGE_FOREIGN_STREAMS="${PURGE_FOREIGN_STREAMS:-1}"
  PURGE_FLAG=()
  if [ "$PURGE_FOREIGN_STREAMS" = "1" ]; then
    PURGE_FLAG=(--purge-foreign-streams)
  fi
  DATA_HOURS="${DATA_HOURS:-6}" \
  INTERVAL_SEC="${INTERVAL_SEC:-30}" \
  BATCH_DOC_LIMIT="${BATCH_DOC_LIMIT:-8000}" \
    $VENV "$SCRIPT_DIR/setup_telemetry_data.py" "$DASHBOARD_YAML_DIR" "${PURGE_FLAG[@]}"
fi

if [ "$SKIP_UPLOAD" = false ]; then
  echo ""
  echo "============================================================"
  echo "  Step 3: Upload compiled dashboards to Kibana"
  echo "============================================================"
  upload_ok=0
  upload_fail=0
  for dir in "$COMPILED_DIR"/*/; do
    ndjson="$dir/compiled_dashboards.ndjson"
    if [ ! -f "$ndjson" ]; then
      continue
    fi
    name="$(basename "$dir")"
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
      -X POST "$KIBANA_ENDPOINT/api/saved_objects/_import?overwrite=true" \
      -H "kbn-xsrf: true" \
      -H "Authorization: ApiKey $KEY" \
      -F "file=@$ndjson")
    if [ "$http_code" = "200" ]; then
      echo "  OK: $name"
      upload_ok=$((upload_ok + 1))
    else
      echo "  FAIL ($http_code): $name"
      upload_fail=$((upload_fail + 1))
    fi
  done
  echo "  Uploaded: $upload_ok OK, $upload_fail failed"

  echo ""
  echo "============================================================"
  echo "  Step 5: Validate panel queries against live ES"
  echo "============================================================"
  MAX_BROKEN_PCT="${MAX_BROKEN_PCT:-10}" \
    $VENV "$SCRIPT_DIR/validate_panel_queries.py" "$DASHBOARD_YAML_DIR"
fi

echo ""
echo "============================================================"
echo "  Pipeline complete"
echo "============================================================"
echo "Output dir:         $OUTPUT_DIR"
echo "Dashboard YAML:     $DASHBOARD_YAML_DIR"
echo "Run summary:        $RUN_SUMMARY"
