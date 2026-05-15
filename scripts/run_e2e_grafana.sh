#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

# End-to-end Grafana → Kibana migration for all 6 source dashboards.
#
# Usage:
#   bash scripts/run_e2e_grafana.sh              # migrate + upload
#   bash scripts/run_e2e_grafana.sh --dry-run    # migrate only (no --upload)
#
# Prerequisites:
#   - serverless_creds.env in repo root (exports ELASTICSEARCH_ENDPOINT,
#     KIBANA_ENDPOINT, KEY)
#   - .venv with requirements.txt + package installed
#
set -euo pipefail

REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: bash scripts/run_e2e_grafana.sh [--dry-run]

Migrates all 6 Grafana dashboards to Kibana.

Options:
  --dry-run   Run migration without --upload (translate + validate only).
  -h, --help  Show this help text.
EOF
      exit 0
      ;;
    *)
      printf 'ERROR: Unknown argument: %s\n' "$arg" >&2
      echo "Run with --help for usage." >&2
      exit 1
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Source credentials
# ---------------------------------------------------------------------------
CREDS_FILE="$REPO/serverless_creds.env"
if [[ ! -f "$CREDS_FILE" ]]; then
  printf 'ERROR: credentials file not found: %s\n' "$CREDS_FILE" >&2
  printf '       Copy serverless_creds.env.example and fill in the values.\n' >&2
  exit 1
fi
set -a
source "$CREDS_FILE"
set +a

# ---------------------------------------------------------------------------
# Validate required environment variables
# ---------------------------------------------------------------------------
missing=()
[[ -z "${ELASTICSEARCH_ENDPOINT:-}" ]] && missing+=("ELASTICSEARCH_ENDPOINT")
[[ -z "${KIBANA_ENDPOINT:-}" ]]        && missing+=("KIBANA_ENDPOINT")
[[ -z "${KEY:-}" ]]                    && missing+=("KEY")

if [[ ${#missing[@]} -gt 0 ]]; then
  printf 'ERROR: Required environment variable(s) not set after sourcing %s:\n' "$CREDS_FILE" >&2
  for var in "${missing[@]}"; do
    printf '  - %s\n' "$var" >&2
  done
  exit 1
fi

# ---------------------------------------------------------------------------
# Dashboard manifest: (slug, source-file)
# ---------------------------------------------------------------------------
declare -a SLUGS=(
  "diverse-panels-test"
  "home"
  "k8s-views-global"
  "node-exporter-full"
  "prometheus-all"
  "express-prometheus"
)

declare -a SOURCES=(
  "$REPO/infra/grafana/dashboards/diverse-panels-test.json"
  "$REPO/infra/grafana/dashboards/home.json"
  "$REPO/infra/grafana/dashboards/k8s-views-global.json"
  "$REPO/infra/grafana/dashboards/node-exporter-full.json"
  "$REPO/infra/grafana/dashboards/prometheus-all.json"
  "$REPO/parity-rig/grafana/dashboards/express-prometheus-middleware.json"
)

# ---------------------------------------------------------------------------
# Output directories
# ---------------------------------------------------------------------------
OUT_ROOT="/tmp/mig-to-kbn-e2e/grafana"
LOG_DIR="$OUT_ROOT/logs"
mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
# Python binary
# ---------------------------------------------------------------------------
PYTHON_BIN="$REPO/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  printf 'ERROR: %s not found or not executable.\n' "$PYTHON_BIN" >&2
  printf '       Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .\n' >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Mode banner
# ---------------------------------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
  printf '\n[dry-run] Skipping --upload for all dashboards.\n\n'
else
  printf '\nRunning full E2E migration (--upload enabled).\n\n'
fi

# ---------------------------------------------------------------------------
# Per-dashboard migration loop
# ---------------------------------------------------------------------------
declare -a SUCCEEDED=()
declare -a FAILED=()

for i in "${!SLUGS[@]}"; do
  slug="${SLUGS[$i]}"
  src="${SOURCES[$i]}"
  out_dir="$OUT_ROOT/$slug"
  log_file="$LOG_DIR/$slug.log"
  tmp_input="$(mktemp -d "${TMPDIR:-/tmp}/grafana-e2e-input.$slug.XXXXXX")"

  printf '=== Migrating: %s ===\n' "$slug"

  # Validate source file
  if [[ ! -f "$src" ]]; then
    printf 'ERROR: source dashboard not found: %s\n' "$src" >&2
    FAILED+=("$slug")
    rm -rf "$tmp_input"
    continue
  fi

  # Copy source JSON to temp input dir
  cp "$src" "$tmp_input/"

  # Build CLI command
  migrate_cmd=(
    "$PYTHON_BIN"
    -m observability_migration.adapters.source.grafana.cli
    --source files
    --input-dir "$tmp_input"
    --output-dir "$out_dir"
    --assets dashboards
    --native-promql
    --es-url "$ELASTICSEARCH_ENDPOINT"
    --es-api-key "$KEY"
    --kibana-url "$KIBANA_ENDPOINT"
    --kibana-api-key "$KEY"
    --ensure-data-views
  )

  if [[ $DRY_RUN -eq 0 ]]; then
    migrate_cmd+=(--upload)
  fi

  # Run migration, tee to log
  if (cd "$REPO" && "${migrate_cmd[@]}" 2>&1) | tee "$log_file"; then
    printf '  -> SUCCESS: %s  (log: %s)\n\n' "$slug" "$log_file"
    SUCCEEDED+=("$slug")
  else
    printf '  -> FAILED:  %s  (log: %s)\n\n' "$slug" "$log_file"
    FAILED+=("$slug")
  fi

  rm -rf "$tmp_input"
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
printf '\n============================================================\n'
printf '  E2E Grafana Migration Summary\n'
printf '============================================================\n'
printf '  Output root : %s\n' "$OUT_ROOT"
printf '  Logs        : %s\n' "$LOG_DIR"
if [[ $DRY_RUN -eq 1 ]]; then
  printf '  Mode        : dry-run (no upload)\n'
else
  printf '  Mode        : full upload\n'
fi
printf '\n'

if [[ ${#SUCCEEDED[@]} -gt 0 ]]; then
  printf 'SUCCESS (%d):\n' "${#SUCCEEDED[@]}"
  for slug in "${SUCCEEDED[@]}"; do
    printf '  [OK]  %s\n' "$slug"
  done
fi

if [[ ${#FAILED[@]} -gt 0 ]]; then
  printf '\nFAIL (%d):\n' "${#FAILED[@]}"
  for slug in "${FAILED[@]}"; do
    printf '  [!!]  %s\n' "$slug"
  done
fi

printf '\n'

if [[ ${#FAILED[@]} -gt 0 ]]; then
  printf 'One or more dashboards failed. Check logs in %s\n' "$LOG_DIR" >&2
  exit 1
fi

printf 'All dashboards migrated successfully.\n'
