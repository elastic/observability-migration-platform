#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0


set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/infra/docker-compose.yml"
PROJECT_NAME="${LOCAL_LAB_PROJECT:-infra}"

TARGET="local"
START_LAB=0
WITH_ALLOY=0
RECREATE_LAB=0
INPUT_DIR=""
INPUT_DIR_SET=0
OUTPUT_DIR=""
OUTPUT_DIR_SET=0
FIELD_PROFILE="${FIELD_PROFILE:-otel}"
DATA_VIEW="${DATA_VIEW:-metrics-*}"
LOGS_INDEX="${LOGS_INDEX:-logs-*}"
DATA_HOURS="${DATA_HOURS:-1}"
INTERVAL_SEC="${INTERVAL_SEC:-30}"
BULK_WORKERS="${BULK_WORKERS:-2}"
BATCH_DOC_LIMIT="${BATCH_DOC_LIMIT:-4000}"
BROWSER_AUDIT=0
CAPTURE_SCREENSHOTS=0
CHROME_BINARY="${CHROME_BINARY:-}"
CREDS_FILE="serverless_creds.env"
ES_URL="${ES_URL:-}"
KIBANA_URL="${KIBANA_URL:-}"
ES_API_KEY="${ES_API_KEY:-}"
KIBANA_API_KEY="${KIBANA_API_KEY:-}"
TEMP_INPUT_DIR=""
RUN_INPUT_DIR=""

SMOKE_DASHBOARD_FILES=(
  "sample_dashboard.json"
  "integrations/postgres.json"
  "integrations/docker.json"
  "integrations/kubernetes.json"
)

usage() {
  cat <<'EOF'
Usage: bash scripts/run_datadog_demo.sh [options]

Runs a small end-to-end Datadog -> Kibana validation flow.

Targets:
  local       Fresh or reused local lab target. Uses a small generated dataset.
  serverless  Fresh serverless target using serverless_creds.env by default.

Default dashboard subset:
  - sample_dashboard.json
  - integrations/postgres.json
  - integrations/docker.json
  - integrations/kubernetes.json

Options:
  --target TARGET      Validation target: local or serverless. Default: local.
  --start-lab          For local target, explicitly start/reuse the local lab before running.
  --with-alloy         For local target startup, enable the Alloy profile.
  --recreate-lab       For local target, force a clean local-lab recreate first.
  --input-dir PATH     Optional Datadog dashboard directory instead of the bundled smoke subset.
  --output-dir PATH    Output directory for generated YAML, reports, and compiled artifacts.
  --field-profile ID   Datadog field profile to use. Default: otel.
  --data-view PATTERN  Metrics data view / index pattern. Default: metrics-*.
  --logs-index PATTERN Logs data view / index pattern. Default: logs-*.
  --data-hours HOURS   Synthetic data hours to generate. Default: 1.
  --interval-sec SEC   Synthetic data interval seconds. Default: 30.
  --bulk-workers N     Synthetic data ingest workers. Default: 2.
  --batch-doc-limit N  Synthetic data batch size. Default: 4000.
  --browser-audit      Enable browser audit during smoke validation.
  --capture-screenshots
                       Capture dashboard screenshots during smoke validation.
  --chrome-binary PATH Optional Chrome/Chromium binary path.
  --creds-file PATH    Serverless credentials env file. Default: serverless_creds.env.
  --es-url URL         Override Elasticsearch URL.
  --kibana-url URL     Override Kibana URL.
  -h, --help           Show this help text.
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
  local curl_args=(-fsS -H 'Content-Type: application/json')

  if [[ -n "$ES_API_KEY" ]]; then
    curl_args+=(-H "Authorization: ApiKey $ES_API_KEY")
  fi

  printf 'Waiting for %s data in Elasticsearch\n' "$name"
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if curl \
      "${curl_args[@]}" \
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

delete_legacy_datadog_templates() {
  local curl_args=(-fsS -X DELETE)

  if [[ -n "$ES_API_KEY" ]]; then
    curl_args+=(-H "Authorization: ApiKey $ES_API_KEY")
  fi

  printf 'Cleaning legacy Datadog templates if present\n'
  curl "${curl_args[@]}" "$ES_URL/_index_template/dd-dashboard-metrics" >/dev/null 2>&1 || true
  curl "${curl_args[@]}" "$ES_URL/_index_template/dd-dashboard-logs" >/dev/null 2>&1 || true
}

cleanup() {
  if [[ -n "$TEMP_INPUT_DIR" && -d "$TEMP_INPUT_DIR" ]]; then
    rm -rf "$TEMP_INPUT_DIR"
  fi
}
trap cleanup EXIT

while (($# > 0)); do
  case "$1" in
    --target)
      shift
      TARGET="${1:-}"
      ;;
    --start-lab)
      START_LAB=1
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
    --field-profile)
      shift
      FIELD_PROFILE="${1:-}"
      ;;
    --data-view)
      shift
      DATA_VIEW="${1:-}"
      ;;
    --logs-index)
      shift
      LOGS_INDEX="${1:-}"
      ;;
    --data-hours)
      shift
      DATA_HOURS="${1:-}"
      ;;
    --interval-sec)
      shift
      INTERVAL_SEC="${1:-}"
      ;;
    --bulk-workers)
      shift
      BULK_WORKERS="${1:-}"
      ;;
    --batch-doc-limit)
      shift
      BATCH_DOC_LIMIT="${1:-}"
      ;;
    --browser-audit)
      BROWSER_AUDIT=1
      ;;
    --capture-screenshots)
      CAPTURE_SCREENSHOTS=1
      ;;
    --chrome-binary)
      shift
      CHROME_BINARY="${1:-}"
      ;;
    --creds-file)
      shift
      CREDS_FILE="${1:-}"
      ;;
    --es-url)
      shift
      ES_URL="${1:-}"
      ;;
    --kibana-url)
      shift
      KIBANA_URL="${1:-}"
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

if [[ "$TARGET" != "local" && "$TARGET" != "serverless" ]]; then
  printf 'ERROR: Unsupported target: %s (supported: local, serverless)\n' "$TARGET" >&2
  exit 1
fi

if [[ "$TARGET" == "serverless" && $START_LAB -eq 1 ]]; then
  echo "ERROR: --start-lab is only valid with --target local" >&2
  exit 1
fi
if [[ "$TARGET" == "serverless" && $WITH_ALLOY -eq 1 ]]; then
  echo "ERROR: --with-alloy is only valid with --target local" >&2
  exit 1
fi
if [[ "$TARGET" == "serverless" && $RECREATE_LAB -eq 1 ]]; then
  echo "ERROR: --recreate-lab is only valid with --target local" >&2
  exit 1
fi

check_command curl
check_command docker
check_command uvx

PYTHON_BIN="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: .venv/bin/python is required" >&2
  exit 1
fi

if [[ "$TARGET" == "serverless" ]]; then
  if [[ ! -f "$CREDS_FILE" ]]; then
    echo "ERROR: credentials file not found: $CREDS_FILE" >&2
    exit 1
  fi
  set -a
  source "$CREDS_FILE"
  set +a
  ES_URL="${ES_URL:-${ELASTICSEARCH_ENDPOINT:-}}"
  KIBANA_URL="${KIBANA_URL:-${KIBANA_ENDPOINT:-}}"
  ES_API_KEY="${ES_API_KEY:-${KEY:-}}"
  KIBANA_API_KEY="${KIBANA_API_KEY:-${KEY:-}}"
fi

if [[ -z "$ES_URL" ]]; then
  if [[ "$TARGET" == "local" ]]; then
    ES_URL="http://localhost:${LOCAL_ES_PORT:-19200}"
  else
    echo "ERROR: Elasticsearch URL is required for serverless target" >&2
    exit 1
  fi
fi

if [[ -z "$KIBANA_URL" ]]; then
  if [[ "$TARGET" == "local" ]]; then
    KIBANA_URL="http://localhost:${LOCAL_KIBANA_PORT:-15601}"
  else
    echo "ERROR: Kibana URL is required for serverless target" >&2
    exit 1
  fi
fi

if [[ $OUTPUT_DIR_SET -eq 0 ]]; then
  if [[ "$TARGET" == "local" ]]; then
    OUTPUT_DIR="$ROOT/validation/local_datadog_demo_run"
  else
    OUTPUT_DIR="$ROOT/datadog_serverless_demo_run"
  fi
fi

ALERT_ARTIFACT_DIR="$OUTPUT_DIR/alerts"
DASHBOARD_YAML_DIR="$OUTPUT_DIR/dashboards/yaml"
RUN_SUMMARY="$OUTPUT_DIR/run_summary.json"

if [[ $INPUT_DIR_SET -eq 1 ]]; then
  RUN_INPUT_DIR="$INPUT_DIR"
else
  TEMP_INPUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/obs-datadog-demo.XXXXXX")"
  for dashboard_file in "${SMOKE_DASHBOARD_FILES[@]}"; do
    cp "$ROOT/infra/datadog/dashboards/$dashboard_file" "$TEMP_INPUT_DIR/"
  done
  RUN_INPUT_DIR="$TEMP_INPUT_DIR"
fi

if [[ "$TARGET" == "local" ]]; then
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
      if [[ $START_LAB -eq 1 ]]; then
        "${start_lab_cmd[@]}"
      fi
    else
      cat >&2 <<EOF
ERROR: The configured lab ports are already serving Elasticsearch/Kibana, but they are not managed by the local lab compose project '$PROJECT_NAME'.
Stop the external services or set LOCAL_ES_PORT / LOCAL_KIBANA_PORT / LOCAL_GRAFANA_PORT (and related local lab ports) to unused values.
EOF
      exit 1
    fi
  else
    printf 'Starting local lab for Datadog demo\n'
    "${start_lab_cmd[@]}"
  fi

  wait_for_http "Elasticsearch" "$ES_URL/_cluster/health?wait_for_status=yellow&timeout=1s" 60 2
  wait_for_http "Kibana" "$KIBANA_URL/api/status" 90 2
fi

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

printf 'Preparing Datadog demo YAML\n'
prepare_cmd=(
  "$PYTHON_BIN"
  -m
  observability_migration.adapters.source.datadog.cli
  --source files
  --input-dir "$RUN_INPUT_DIR"
  --output-dir "$OUTPUT_DIR"
  --assets dashboards
  --field-profile "$FIELD_PROFILE"
  --data-view "$DATA_VIEW"
  --logs-index "$LOGS_INDEX"
  --preflight
  --es-url "$ES_URL"
)
if [[ -n "$ES_API_KEY" ]]; then
  prepare_cmd+=(--es-api-key "$ES_API_KEY")
fi
(
  cd "$ROOT"
  "${prepare_cmd[@]}"
)

delete_legacy_datadog_templates

printf 'Generating small Datadog validation dataset\n'
ELASTICSEARCH_ENDPOINT="$ES_URL" \
KEY="${ES_API_KEY:-dummy}" \
DATA_HOURS="$DATA_HOURS" \
INTERVAL_SEC="$INTERVAL_SEC" \
BATCH_DOC_LIMIT="$BATCH_DOC_LIMIT" \
  "$PYTHON_BIN" "$ROOT/scripts/setup_telemetry_data.py" "$DASHBOARD_YAML_DIR"

wait_for_esql_query "metrics" "FROM $DATA_VIEW | LIMIT 1" 45 2
wait_for_esql_query "logs" "FROM $LOGS_INDEX | LIMIT 1" 45 2

printf 'Running Datadog end-to-end validation\n'
validate_cmd=(
  "$PYTHON_BIN"
  -m
  observability_migration.adapters.source.datadog.cli
  --source files
  --input-dir "$RUN_INPUT_DIR"
  --output-dir "$OUTPUT_DIR"
  --assets dashboards
  --field-profile "$FIELD_PROFILE"
  --data-view "$DATA_VIEW"
  --logs-index "$LOGS_INDEX"
  --preflight
  --compile
  --validate
  --upload
  --smoke
  --ensure-data-views
  --es-url "$ES_URL"
  --kibana-url "$KIBANA_URL"
)
if [[ -n "$ES_API_KEY" ]]; then
  validate_cmd+=(--es-api-key "$ES_API_KEY")
fi
if [[ -n "$KIBANA_API_KEY" ]]; then
  validate_cmd+=(--kibana-api-key "$KIBANA_API_KEY")
fi
if [[ $BROWSER_AUDIT -eq 1 ]]; then
  validate_cmd+=(--browser-audit)
fi
if [[ $CAPTURE_SCREENSHOTS -eq 1 ]]; then
  validate_cmd+=(--capture-screenshots)
fi
if [[ -n "$CHROME_BINARY" ]]; then
  validate_cmd+=(--chrome-binary "$CHROME_BINARY")
fi
(
  cd "$ROOT"
  "${validate_cmd[@]}"
)

cat <<EOF

Datadog demo completed.

Artifacts:
  Output: $OUTPUT_DIR
  YAML:   $DASHBOARD_YAML_DIR
  Run summary: $RUN_SUMMARY
EOF
