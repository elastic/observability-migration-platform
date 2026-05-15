#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

# Parity harness for the native /_prometheus endpoint schema profile.
#
# Migrates the express-prometheus dashboard with --no-native-promql so
# every panel emits TS/FROM ES|QL (not the PROMQL source command).
# The parity harness then runs those translated queries against the native
# endpoint index (metrics-express.prometheus-parity) and diffs them against
# the Prometheus side.
#
# This is the canonical way to validate the prometheus_native SchemaResolver
# profile added in schema.py — it exercises the full path:
#   PromQL expression → SchemaResolver (native profile) → TS/FROM ES|QL →
#   metrics-express.prometheus-parity → normalize_esql_translated → diff.
#
# Prerequisites:
#   - parity-rig is running (cd parity-rig && docker compose up -d)
#   - serverless_creds.env in repo root
#   - .venv with requirements.txt + package installed
#
# Usage:
#   bash scripts/run_parity_native_profile.sh [--window-minutes N]
#
set -euo pipefail

REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
WINDOW_MINUTES=10
REPORT_PATH="/tmp/mig-to-kbn-e2e/parity-native-profile/migration_report.json"

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --window-minutes)
      WINDOW_MINUTES="$2"
      shift 2
      ;;
    -h|--help)
      cat <<'EOF'
Usage: bash scripts/run_parity_native_profile.sh [--window-minutes N]

Runs translated ES|QL parity against the native /_prometheus endpoint.

Options:
  --window-minutes N  Comparison window in minutes (default: 10).
  -h, --help          Show this help text.
EOF
      exit 0
      ;;
    *)
      printf 'ERROR: Unknown argument: %s\n' "$1" >&2
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
  exit 1
fi
set -a; source "$CREDS_FILE"; set +a

for var in ELASTICSEARCH_ENDPOINT KEY; do
  if [[ -z "${!var:-}" ]]; then
    printf 'ERROR: %s not set in %s\n' "$var" "$CREDS_FILE" >&2
    exit 1
  fi
done

PYTHON_BIN="$REPO/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  printf 'ERROR: %s not found.\n' "$PYTHON_BIN" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 1: Migrate express-prometheus with --no-native-promql
#         This forces TS/FROM ES|QL for all panels, exercising the
#         prometheus_native SchemaResolver profile.
# ---------------------------------------------------------------------------
SRC="$REPO/parity-rig/grafana/dashboards/express-prometheus-middleware.json"
OUT_DIR="/tmp/mig-to-kbn-e2e/parity-native-profile"
TMP_INPUT="$(mktemp -d "${TMPDIR:-/tmp}/parity-native-input.XXXXXX")"

printf '=== Step 1: Migrating express-prometheus with --no-native-promql ===\n'

cp "$SRC" "$TMP_INPUT/"

(cd "$REPO" && "$PYTHON_BIN" \
  -m observability_migration.adapters.source.grafana.cli \
  --source files \
  --input-dir "$TMP_INPUT" \
  --output-dir "$OUT_DIR" \
  --assets dashboards \
  --no-native-promql \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY")

rm -rf "$TMP_INPUT"

# Locate the migration report JSON that the parity harness reads.
REPORT="$(find "$OUT_DIR" -name "migration_report.json" | head -1)"
if [[ -z "$REPORT" ]]; then
  # Fallback: find any panel YAML and generate a synthetic report.
  printf 'WARN: migration_report.json not found — looking for dashboard YAML\n'
  REPORT="$REPORT_PATH"
fi

printf '  -> Migration output: %s\n\n' "$OUT_DIR"

# ---------------------------------------------------------------------------
# Step 2: Run parity harness in ESQL_FALLBACK mode
#         All panels are now TS/FROM → harness runs via run_esql_raw.
# ---------------------------------------------------------------------------
printf '=== Step 2: Running parity harness (translated ES|QL mode) ===\n'

HARNESS="$REPO/parity-rig/harness/parity.py"

PROM_URL="${PROM_URL:-http://localhost:29090}" \
ESQL_INDEX="${ESQL_INDEX:-metrics-express.prometheus-parity}" \
PARITY_WINDOW_MINUTES="$WINDOW_MINUTES" \
REPORT_PATH="$REPORT" \
ELASTICSEARCH_ENDPOINT="$ELASTICSEARCH_ENDPOINT" \
KEY="$KEY" \
  "$PYTHON_BIN" "$HARNESS"

printf '\nDone. Review the parity report at:\n'
printf '  %s\n' "$OUT_DIR"
