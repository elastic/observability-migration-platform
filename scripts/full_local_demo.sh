#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0


set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/infra/docker-compose.yml"
PROJECT_NAME="${LOCAL_LAB_PROJECT:-infra}"
ES_URL="${ES_URL:-http://localhost:${LOCAL_ES_PORT:-19200}}"
KIBANA_URL="${KIBANA_URL:-http://localhost:${LOCAL_KIBANA_PORT:-15601}}"
DATA_VIEW="${DATA_VIEW:-metrics-*}"
ESQL_INDEX="${ESQL_INDEX:-metrics-*}"
LOGS_INDEX="${LOGS_INDEX:-logs-*}"
DATASET_FILTER="${DATASET_FILTER:-prometheusreceiver.otel}"
INPUT_DIR="${INPUT_DIR:-$ROOT/infra/grafana/dashboards}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT/validation/full_local_demo_run}"
TIME_FROM="${TIME_FROM:-now-1h}"
TIME_TO="${TIME_TO:-now}"
SAMPLE_SET=""
WITH_ALLOY=0
RECREATE_LAB=0
CAPTURE_SCREENSHOTS=1
INPUT_DIR_SET=0
OUTPUT_DIR_SET=0
RUN_INPUT_DIR=""
RUN_LABEL="full bundled dashboard set"
TEMP_INPUT_DIR=""

BUNDLED_SAMPLE_FILES=(
  "node-exporter-full.json"
  "k8s-views-global.json"
  "diverse-panels-test.json"
)

BUNDLED_SAMPLE_TITLES=(
  "Node Exporter Full"
  "Kubernetes / Views / Global"
  "Diverse Panel Types Test"
)

usage() {
  cat <<'EOF'
Usage: bash scripts/full_local_demo.sh [options]

Runs the local OTLP validation flow against the repo's bundled dashboards.
By default this runs the full dashboard set from infra/grafana/dashboards.
Use --sample-set bundled to run the curated three-dashboard sample flow.

The script:
1. Ensures the local lab is running.
2. Waits for Elasticsearch, Kibana, and source data readiness.
3. Provisions Kibana data views.
4. Migrates the selected dashboard set.
5. Compiles and uploads them to Kibana.
6. Smoke-validates the uploaded saved objects.

Options:
  --sample-set NAME   Use a built-in dashboard subset. Supported: bundled.
  --with-alloy        When starting the lab, use Alloy mode.
  --recreate-lab      Force a clean local-lab reset before running.
  --input-dir PATH    Dashboard input directory for full/custom runs.
  --output-dir PATH   Output directory for generated artifacts.
  --time-from VALUE   Dashboard time range start for smoke validation.
  --time-to VALUE     Dashboard time range end for smoke validation.
  --no-screenshots    Skip screenshot capture during smoke validation.
  -h, --help          Show this help text.
EOF
}

check_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'ERROR: Required command not found: %s\n' "$command_name" >&2
    exit 1
  fi
}

is_http_ready() {
  local url="$1"
  curl -fsS "$url" >/dev/null 2>&1
}

local_lab_services_running() {
  local services
  services="$(docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" ps --services --status running 2>/dev/null || true)"
  [[ "$services" == *"elasticsearch"* && "$services" == *"kibana"* ]]
}

wait_for_http() {
  local name="$1"
  local url="$2"
  local attempts="$3"
  local sleep_seconds="${4:-2}"
  local attempt

  printf 'Checking %s at %s\n' "$name" "$url"
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      printf '  %s is reachable\n' "$name"
      return 0
    fi
    sleep "$sleep_seconds"
  done

  printf 'ERROR: %s is not reachable at %s\n' "$name" "$url" >&2
  return 1
}

wait_for_esql_query() {
  local name="$1"
  local query="$2"
  local attempts="${3:-45}"
  local sleep_seconds="${4:-2}"
  local attempt

  printf 'Waiting for %s data in Elasticsearch\n' "$name"
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if curl -fsS \
      -H 'Content-Type: application/json' \
      "$ES_URL/_query?format=json" \
      --data-binary @- >/dev/null 2>&1 <<EOF
{"query":"$query"}
EOF
    then
      printf '  %s data is queryable\n' "$name"
      return 0
    fi
    sleep "$sleep_seconds"
  done

  printf 'WARN: %s data did not become queryable in time\n' "$name" >&2
  return 0
}

while (($# > 0)); do
  case "$1" in
    --sample-set)
      shift
      SAMPLE_SET="${1:-}"
      ;;
    --with-alloy)
      WITH_ALLOY=1
      ;;
    --recreate-lab)
      RECREATE_LAB=1
      ;;
    --input-dir)
      shift
      INPUT_DIR="${1:-}"
      INPUT_DIR_SET=1
      ;;
    --output-dir)
      shift
      OUTPUT_DIR="${1:-}"
      OUTPUT_DIR_SET=1
      ;;
    --time-from)
      shift
      TIME_FROM="${1:-}"
      ;;
    --time-to)
      shift
      TIME_TO="${1:-}"
      ;;
    --no-screenshots)
      CAPTURE_SCREENSHOTS=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

if [[ -n "$SAMPLE_SET" && "$SAMPLE_SET" != "bundled" ]]; then
  printf 'ERROR: Unsupported sample set: %s (supported: bundled)\n' "$SAMPLE_SET" >&2
  exit 1
fi

if [[ "$SAMPLE_SET" == "bundled" && $INPUT_DIR_SET -eq 1 ]]; then
  echo "ERROR: --input-dir cannot be used with --sample-set bundled" >&2
  exit 1
fi

cleanup() {
  if [[ -n "$TEMP_INPUT_DIR" && -d "$TEMP_INPUT_DIR" ]]; then
    rm -rf "$TEMP_INPUT_DIR"
  fi
}
trap cleanup EXIT

if [[ "$SAMPLE_SET" == "bundled" ]]; then
  RUN_LABEL="bundled sample dashboard set"
  if [[ $OUTPUT_DIR_SET -eq 0 ]]; then
    OUTPUT_DIR="$ROOT/validation/local_otlp_sample_run"
  fi

  TEMP_INPUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/obs-migrate-local-sample.XXXXXX")"
  for dashboard_file in "${BUNDLED_SAMPLE_FILES[@]}"; do
    cp "$ROOT/infra/grafana/dashboards/$dashboard_file" "$TEMP_INPUT_DIR/"
  done
  RUN_INPUT_DIR="$TEMP_INPUT_DIR"
else
  RUN_INPUT_DIR="$INPUT_DIR"
fi

DASHBOARD_YAML_DIR="$OUTPUT_DIR/dashboards/yaml"
RUN_SUMMARY="$OUTPUT_DIR/run_summary.json"

check_command docker
check_command curl
check_command uvx

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
else
  check_command python3
  PYTHON_BIN="$(command -v python3)"
fi

if [[ $RECREATE_LAB -eq 1 ]]; then
  printf 'Resetting repo-owned local stacks for a clean full-demo run\n'
  bash "$ROOT/scripts/stop_local_lab.sh" --volumes >/dev/null 2>&1 || true
  docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" down --remove-orphans --volumes >/dev/null 2>&1 || true
fi

declare -a start_args=()
if [[ $WITH_ALLOY -eq 1 ]]; then
  start_args+=(--with-alloy)
fi
if [[ $RECREATE_LAB -eq 1 ]]; then
  start_args+=(--recreate)
fi

start_lab_cmd=(bash "$ROOT/scripts/start_local_lab.sh")
if [[ ${#start_args[@]} -gt 0 ]]; then
  start_lab_cmd+=("${start_args[@]}")
fi

if is_http_ready "$ES_URL/_cluster/health?wait_for_status=yellow&timeout=1s" \
  && is_http_ready "$KIBANA_URL/api/status"; then
  if local_lab_services_running; then
    printf 'Existing local lab detected at %s / %s; reusing it\n' "$ES_URL" "$KIBANA_URL"
  else
    cat >&2 <<EOF
ERROR: The configured lab ports are already serving Elasticsearch/Kibana, but they are not managed by the local lab compose project '$PROJECT_NAME'.
ERROR: The configured lab ports are already serving Elasticsearch/Kibana.
Stop the external services or set LOCAL_ES_PORT / LOCAL_KIBANA_PORT / LOCAL_GRAFANA_PORT (and related local lab ports) to unused values.
EOF
    exit 1
  fi
else
  printf 'Starting local lab for %s\n' "$RUN_LABEL"
  "${start_lab_cmd[@]}"
fi

wait_for_http "Elasticsearch" "$ES_URL/_cluster/health?wait_for_status=yellow&timeout=1s" 60 2
wait_for_http "Kibana" "$KIBANA_URL/api/status" 90 2
wait_for_esql_query "metrics" "FROM $ESQL_INDEX | LIMIT 1" 45 2
wait_for_esql_query "logs" "FROM $LOGS_INDEX | LIMIT 1" 45 2

printf 'Provisioning Kibana data views\n'
bash "$ROOT/scripts/provision_local_kibana_data_views.sh"

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

grafana_cmd=(
  "$PYTHON_BIN"
  -m
  observability_migration.adapters.source.grafana.cli
  --source files
  --input-dir "$RUN_INPUT_DIR"
  --output-dir "$OUTPUT_DIR"
  --assets dashboards
  --data-view "$DATA_VIEW"
  --esql-index "$ESQL_INDEX"
  --logs-index "$LOGS_INDEX"
  --dataset-filter "$DATASET_FILTER"
  --es-url "$ES_URL"
  --kibana-url "$KIBANA_URL"
  --smoke
  --browser-audit
  --smoke-output "$OUTPUT_DIR/upload_smoke_report.json"
  --time-from "$TIME_FROM"
  --time-to "$TIME_TO"
)

if [[ $CAPTURE_SCREENSHOTS -eq 1 ]]; then
  grafana_cmd+=(--capture-screenshots)
fi

printf 'Running integrated migration, upload, and smoke flow for %s\n' "$RUN_LABEL"
(
  cd "$ROOT"
  "${grafana_cmd[@]}"
)

cat <<EOF

Full local demo completed.

Artifacts:
  Migration output: $OUTPUT_DIR
  Dashboard YAML:   $DASHBOARD_YAML_DIR
  Run summary:      $RUN_SUMMARY
  Smoke report:     $OUTPUT_DIR/upload_smoke_report.json
  Browser audit:    $OUTPUT_DIR/browser_qa
EOF

if [[ $CAPTURE_SCREENSHOTS -eq 1 ]]; then
  cat <<EOF
  Screenshots:      $OUTPUT_DIR/dashboard_qa
EOF
fi
