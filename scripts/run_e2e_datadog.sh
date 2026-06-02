#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

#
# End-to-end Datadog → Kibana migration for all 14 sample/integration dashboards.
#
# For each dashboard this script:
#   1. Copies the source JSON to a temp input directory
#   2. Runs the Datadog CLI with --compile --validate --upload --ensure-data-views
#   3. Accumulates pass/fail counts and exits non-zero on any failure
#
# Usage:
#   bash scripts/run_e2e_datadog.sh [--dry-run] [--output-dir PATH] [--creds-file PATH]
#   bash scripts/run_e2e_datadog.sh --help
#
# Prerequisites:
#   - serverless_creds.env in project root (ELASTICSEARCH_ENDPOINT, KIBANA_ENDPOINT, KEY)
#   - .venv with requirements installed
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DRY_RUN=0
CREDS_FILE="$ROOT/serverless_creds.env"
OUTPUT_DIR="$ROOT/e2e_datadog_run"
FIELD_PROFILE="${FIELD_PROFILE:-otel}"
DATA_VIEW="${DATA_VIEW:-metrics-*}"
LOGS_INDEX="${LOGS_INDEX:-logs-*}"

TEMP_INPUT_DIR=""

# ---------------------------------------------------------------------------
# Dashboard definitions: "source_path|slug"
# ---------------------------------------------------------------------------
DASHBOARDS=(
  "infra/datadog/dashboards/sample_dashboard.json|dd-sample"
  "infra/datadog/dashboards/integrations/docker.json|dd-docker"
  "infra/datadog/dashboards/integrations/kubernetes.json|dd-kubernetes"
  "infra/datadog/dashboards/integrations/nginx_overview.json|dd-nginx"
  "infra/datadog/dashboards/integrations/postgres.json|dd-postgres"
  "infra/datadog/dashboards/integrations/redis.json|dd-redis"
  "infra/datadog/dashboards/integrations/mysql.json|dd-mysql"
  "infra/datadog/dashboards/integrations/apache.json|dd-apache"
  "infra/datadog/dashboards/integrations/haproxy.json|dd-haproxy"
  "infra/datadog/dashboards/integrations/kafka.json|dd-kafka"
  "infra/datadog/dashboards/integrations/mongodb.json|dd-mongodb"
  "infra/datadog/dashboards/integrations/rabbitmq.json|dd-rabbitmq"
  "infra/datadog/dashboards/integrations/consul.json|dd-consul"
  "infra/datadog/dashboards/integrations/celery.json|dd-celery"
)

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  cat <<'EOF'
Usage: bash scripts/run_e2e_datadog.sh [options]

Migrates all 6 Datadog integration dashboards to Kibana.

Options:
  --dry-run            Print the commands that would run without executing them.
  --output-dir PATH    Output directory for generated YAML and reports.
                       Default: <repo-root>/e2e_datadog_run
  --creds-file PATH    Credentials env file. Default: <repo-root>/serverless_creds.env
  --field-profile ID   Datadog field profile. Default: otel
  --data-view PATTERN  Metrics index pattern. Default: metrics-*
  --logs-index PATTERN Logs index pattern. Default: logs-*
  -h, --help           Show this help text.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while (($# > 0)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --output-dir)
      shift
      OUTPUT_DIR="${1:-}"
      ;;
    --creds-file)
      shift
      CREDS_FILE="${1:-}"
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
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'ERROR: Unknown argument: %s\n\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Validate prerequisites
# ---------------------------------------------------------------------------
PYTHON_BIN="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  printf 'ERROR: .venv/bin/python not found. Set up the virtual environment first.\n' >&2
  exit 1
fi

if [[ ! -f "$CREDS_FILE" ]]; then
  printf 'ERROR: Credentials file not found: %s\n' "$CREDS_FILE" >&2
  printf '       Copy serverless_creds.env.example to serverless_creds.env and fill in values.\n' >&2
  exit 1
fi

# Source credentials
set -a
# shellcheck source=/dev/null
source "$CREDS_FILE"
set +a

ES_URL="${ES_URL:-${ELASTICSEARCH_ENDPOINT:-}}"
KIBANA_URL="${KIBANA_URL:-${KIBANA_ENDPOINT:-}}"
ES_API_KEY="${ES_API_KEY:-${KEY:-}}"
KIBANA_API_KEY="${KIBANA_API_KEY:-${KEY:-}}"

if [[ -z "$ES_URL" ]]; then
  printf 'ERROR: ELASTICSEARCH_ENDPOINT (or ES_URL) is not set in %s\n' "$CREDS_FILE" >&2
  exit 1
fi

if [[ -z "$KIBANA_URL" ]]; then
  printf 'ERROR: KIBANA_ENDPOINT (or KIBANA_URL) is not set in %s\n' "$CREDS_FILE" >&2
  exit 1
fi

if [[ -z "$ES_API_KEY" ]]; then
  printf 'ERROR: KEY (or ES_API_KEY) is not set in %s\n' "$CREDS_FILE" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Validate source dashboards exist
# ---------------------------------------------------------------------------
for entry in "${DASHBOARDS[@]}"; do
  src_rel="${entry%%|*}"
  src_abs="$ROOT/$src_rel"
  if [[ ! -f "$src_abs" ]]; then
    printf 'ERROR: Source dashboard not found: %s\n' "$src_abs" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
cleanup() {
  if [[ -n "$TEMP_INPUT_DIR" && -d "$TEMP_INPUT_DIR" ]]; then
    rm -rf "$TEMP_INPUT_DIR"
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
PASS=0
FAIL=0
FAILED_SLUGS=()

printf '\n=== Datadog E2E migration: %d dashboards ===\n\n' "${#DASHBOARDS[@]}"

for entry in "${DASHBOARDS[@]}"; do
  src_rel="${entry%%|*}"
  slug="${entry##*|}"
  src_abs="$ROOT/$src_rel"

  # Create a per-dashboard temp input dir (single JSON file)
  TEMP_INPUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/obs-dd-e2e-${slug}.XXXXXX")"
  cp "$src_abs" "$TEMP_INPUT_DIR/"

  DASHBOARD_OUTPUT_DIR="$OUTPUT_DIR/$slug"

  migrate_cmd=(
    "$PYTHON_BIN"
    -m observability_migration.adapters.source.datadog.cli
    --source files
    --input-dir "$TEMP_INPUT_DIR"
    --output-dir "$DASHBOARD_OUTPUT_DIR"
    --assets dashboards
    --field-profile "$FIELD_PROFILE"
    --data-view "$DATA_VIEW"
    --logs-index "$LOGS_INDEX"
    --compile
    --validate
    --upload
    --ensure-data-views
    --es-url "$ES_URL"
    --kibana-url "$KIBANA_URL"
  )

  if [[ -n "$ES_API_KEY" ]]; then
    migrate_cmd+=(--es-api-key "$ES_API_KEY")
  fi
  if [[ -n "$KIBANA_API_KEY" ]]; then
    migrate_cmd+=(--kibana-api-key "$KIBANA_API_KEY")
  fi

  printf '%s\n' "--- [$slug] $src_rel"

  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    DRY-RUN: %s\n\n' "${migrate_cmd[*]}"
    # Clean up per-dashboard temp dir
    rm -rf "$TEMP_INPUT_DIR"
    TEMP_INPUT_DIR=""
    PASS=$((PASS + 1))
    continue
  fi

  mkdir -p "$DASHBOARD_OUTPUT_DIR"

  set +e
  (cd "$ROOT" && "${migrate_cmd[@]}")
  exit_code=$?
  set -e

  # Clean up per-dashboard temp dir now
  rm -rf "$TEMP_INPUT_DIR"
  TEMP_INPUT_DIR=""

  if [[ $exit_code -eq 0 ]]; then
    printf '    PASS\n\n'
    PASS=$((PASS + 1))
  else
    printf '    FAIL (exit %d)\n\n' "$exit_code"
    FAIL=$((FAIL + 1))
    FAILED_SLUGS+=("$slug")
  fi
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
TOTAL=${#DASHBOARDS[@]}
printf '=== Summary: %d/%d passed ===\n' "$PASS" "$TOTAL"

if [[ ${#FAILED_SLUGS[@]} -gt 0 ]]; then
  printf 'Failed dashboards:\n'
  for s in "${FAILED_SLUGS[@]}"; do
    printf '  - %s\n' "$s"
  done
fi

if [[ $DRY_RUN -eq 1 ]]; then
  printf '\nDry-run complete. No uploads were performed.\n'
  exit 0
fi

if [[ $FAIL -gt 0 ]]; then
  printf '\nE2E migration finished with %d failure(s).\n' "$FAIL" >&2
  exit 1
fi

printf '\nE2E migration complete. Output: %s\n' "$OUTPUT_DIR"
exit 0
