#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

#
# Seed synthetic telemetry data for all migrated dashboard artifacts.
#
# Discovers every output-* directory under /tmp/mig-to-kbn-e2e/ (for both
# grafana and datadog sources), collects their dashboards/ sub-directories,
# and runs setup_telemetry_data.py against the combined set.
#
# Usage:
#   bash scripts/run_seed_data.sh
#
# Prerequisites:
#   - serverless_creds.env in project root (ELASTICSEARCH_ENDPOINT and KEY)
#   - .venv with requirements.txt installed
#   - At least one migration output directory must exist under
#     /tmp/mig-to-kbn-e2e/{grafana,datadog}/output-*/dashboards/
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_FILE="/tmp/mig-to-kbn-e2e/seed-data.log"
E2E_ROOT="/tmp/mig-to-kbn-e2e"

cd "$PROJECT_ROOT"

VENV="$PROJECT_ROOT/.venv/bin/python"
if [ ! -f "$VENV" ]; then
  echo "ERROR: .venv not found. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e ." >&2
  exit 1
fi

CREDS_FILE="$PROJECT_ROOT/serverless_creds.env"
if [ ! -f "$CREDS_FILE" ]; then
  echo "ERROR: serverless_creds.env not found in project root." >&2
  echo "  Copy serverless_creds.env.example and fill in ELASTICSEARCH_ENDPOINT and KEY." >&2
  exit 1
fi

set -a
# shellcheck source=/dev/null
source "$CREDS_FILE"
set +a

if [ -z "${ELASTICSEARCH_ENDPOINT:-}" ] || [ -z "${KEY:-}" ]; then
  echo "ERROR: ELASTICSEARCH_ENDPOINT and KEY must both be set in serverless_creds.env." >&2
  exit 1
fi

# Collect artifact dirs: search all slug-named subdirectories under the
# Grafana E2E root and the in-repo Datadog output dir. Both migrations write
# <run-root>/<slug>/dashboards/ with a yaml/ sub-directory inside.
ARTIFACT_DIRS=()

# Grafana: /tmp/mig-to-kbn-e2e/grafana/<slug>/dashboards/
for dashboards_dir in "$E2E_ROOT/grafana"/*/dashboards; do
  [ -d "$dashboards_dir/yaml" ] || continue
  ARTIFACT_DIRS+=("$dashboards_dir")
done

# Datadog: <repo>/e2e_datadog_run/<slug>/dashboards/
DD_OUT_ROOT="$PROJECT_ROOT/e2e_datadog_run"
if [ -d "$DD_OUT_ROOT" ]; then
  for dashboards_dir in "$DD_OUT_ROOT"/*/dashboards; do
    [ -d "$dashboards_dir/yaml" ] || continue
    ARTIFACT_DIRS+=("$dashboards_dir")
  done
fi

if [ ${#ARTIFACT_DIRS[@]} -eq 0 ]; then
  echo "ERROR: No artifact directories found." >&2
  echo "  Expected:" >&2
  echo "    $E2E_ROOT/grafana/<slug>/dashboards/yaml/ (run scripts/run_e2e_grafana.sh first)" >&2
  echo "    $DD_OUT_ROOT/<slug>/dashboards/yaml/      (run scripts/run_e2e_datadog.sh first)" >&2
  exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")"

echo "=== run_seed_data.sh ===" | tee "$LOG_FILE"
echo "Artifact directories (${#ARTIFACT_DIRS[@]}):" | tee -a "$LOG_FILE"
for d in "${ARTIFACT_DIRS[@]}"; do
  echo "  $d" | tee -a "$LOG_FILE"
done
echo "" | tee -a "$LOG_FILE"

DATA_HOURS="${DATA_HOURS:-4}"
INTERVAL_SEC="${INTERVAL_SEC:-30}"

"$VENV" "$SCRIPT_DIR/setup_telemetry_data.py" \
  "${ARTIFACT_DIRS[@]}" \
  --es-endpoint "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --data-hours "$DATA_HOURS" \
  --interval-sec "$INTERVAL_SEC" \
  2>&1 | tee -a "$LOG_FILE"

STATUS=${PIPESTATUS[0]}

if [ "$STATUS" -ne 0 ]; then
  echo "" | tee -a "$LOG_FILE"
  echo "ERROR: setup_telemetry_data.py exited with status $STATUS." | tee -a "$LOG_FILE"
  echo "  See $LOG_FILE for details." >&2
  exit "$STATUS"
fi

echo "" | tee -a "$LOG_FILE"
echo "Seed data complete. Log: $LOG_FILE" | tee -a "$LOG_FILE"
