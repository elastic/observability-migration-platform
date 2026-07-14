#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
#
# Run dashboard interaction scenarios against an already-running local stack.
# This script never starts or stops Docker. The caller owns stack lifecycle.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

PY="${PY:-.venv/bin/python}"
STACK_VERSION="${STACK_VERSION:-9.5.0-SNAPSHOT}"
SCENARIOS="${SCENARIOS:-synthetic-controls,redis-11835,k8s-views-global}"
ES_URL="${ES_URL:-http://localhost:9200}"
KIBANA_URL="${KIBANA_URL:-http://localhost:5601}"
ES_API_KEY="${ES_API_KEY:-}"
KIBANA_API_KEY="${KIBANA_API_KEY:-}"
ARTIFACT_ROOT="${ARTIFACT_ROOT:-$PROJECT_ROOT/interaction-audit-artifacts}"
KEEP_WORK="${KEEP_WORK:-0}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"

mkdir -p "$ARTIFACT_ROOT"
if [[ "$KEEP_WORK" == "1" ]]; then
  WORK="${WORK_DIR:-$ARTIFACT_ROOT/work-$RUN_ID-$$}"
  mkdir -p "$WORK"
else
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/obs-migrate-interaction.XXXXXX")"
  trap 'rm -rf "$WORK"' EXIT
fi

echo "== interaction audit: ES=$ES_URL KIBANA=$KIBANA_URL stack=$STACK_VERSION =="
echo "Artifact root: $ARTIFACT_ROOT"
echo "Work directory: $WORK"

"$PY" -m observability_migration.targets.kibana.interaction_audit_local \
  check-environment \
  --stack-version "$STACK_VERSION" \
  --scenarios "$SCENARIOS" \
  --es-url "$ES_URL" \
  --kibana-url "$KIBANA_URL"

# A direct invocation of this script must be as reproducible as the Make target.
"$PY" -m playwright install chromium

run_grafana_scenario() {
  local scenario_id="$1"
  local source_relative="$2"
  local scenario_root="$WORK/scenarios/$scenario_id"
  local input_dir="$scenario_root/input"
  local bootstrap_root="$WORK/bootstrap/$scenario_id"
  local final_root="$WORK/final/$scenario_id"
  local bootstrap_artifacts="$bootstrap_root/dashboards"
  local final_artifacts="$final_root/dashboards"

  mkdir -p "$input_dir"
  cp "$PROJECT_ROOT/$source_relative" "$input_dir/"

  echo "-- $scenario_id: bootstrap migrate (isolated source, no upload) --"
  "$PY" -m observability_migration.adapters.source.grafana.cli \
    --source files \
    --input-dir "$input_dir" \
    --output-dir "$bootstrap_root" \
    --assets dashboards

  echo "-- $scenario_id: bootstrap telemetry seed --"
  "$PY" scripts/setup_telemetry_data.py "$bootstrap_artifacts" \
    --es-endpoint "$ES_URL" \
    --api-key "$ES_API_KEY" \
    --data-hours 3 \
    --interval-sec 60

  echo "-- $scenario_id: live-schema migrate and native upload --"
  "$PY" -m observability_migration.adapters.source.grafana.cli \
    --source files \
    --input-dir "$input_dir" \
    --output-dir "$final_root" \
    --assets dashboards \
    --es-url "$ES_URL" \
    --es-api-key "$ES_API_KEY" \
    --validate \
    --kibana-url "$KIBANA_URL" \
    --kibana-api-key "$KIBANA_API_KEY" \
    --upload \
    --ensure-data-views

  echo "-- $scenario_id: final-artifact telemetry seed --"
  "$PY" scripts/setup_telemetry_data.py "$final_artifacts" \
    --es-endpoint "$ES_URL" \
    --api-key "$ES_API_KEY" \
    --data-hours 3 \
    --interval-sec 60

  echo "-- $scenario_id: YAML/schema lint and live ES|QL validation --"
  "$PY" -m observability_migration.targets.kibana.interaction_audit_local \
    validate-final \
    --migration-out "$final_artifacts" \
    --es-url "$ES_URL" \
    --api-key "$ES_API_KEY"
}

run_synthetic_scenario() {
  local scenario_id="$1"
  local final_root="$WORK/final/$scenario_id"
  local final_artifacts="$final_root/dashboards"

  echo "-- $scenario_id: write Native IR-derived artifacts --"
  "$PY" -m observability_migration.targets.kibana.interaction_audit_local \
    prepare-synthetic \
    --artifact-dir "$final_artifacts"

  echo "-- $scenario_id: seed telemetry from the IR-derived YAML contract --"
  "$PY" scripts/setup_telemetry_data.py "$final_artifacts" \
    --es-endpoint "$ES_URL" \
    --api-key "$ES_API_KEY" \
    --data-hours 3 \
    --interval-sec 60

  "$PY" -m observability_migration.app.cli cluster ensure-data-views \
    --kibana-url "$KIBANA_URL" \
    --kibana-api-key "$KIBANA_API_KEY" \
    --data-view-patterns "metrics-*"

  echo "-- $scenario_id: upload reviewed native artifact --"
  "$PY" -m observability_migration.app.cli upload \
    --artifact-dir "$final_artifacts" \
    --artifact-format native \
    --kibana-url "$KIBANA_URL" \
    --kibana-api-key "$KIBANA_API_KEY"
}

run_browser_audit() {
  local scenario_id="$1"
  local final_artifacts="$WORK/final/$scenario_id/dashboards"
  local runtime_dir="$WORK/runtime/$scenario_id"
  local manifest="$PROJECT_ROOT/parity-rig/interaction-scenarios/$scenario_id.yaml"

  echo "-- $scenario_id: resolve exact dashboard and runtime panel contract --"
  "$PY" -m observability_migration.targets.kibana.interaction_audit_local \
    prepare-runtime \
    --manifest "$manifest" \
    --migration-out "$final_artifacts" \
    --kibana-url "$KIBANA_URL" \
    --api-key "$KIBANA_API_KEY" \
    --output-dir "$runtime_dir"

  local runtime_manifest
  local dashboard_url
  local panel_contract
  runtime_manifest="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifest"])' "$runtime_dir/runtime.json")"
  dashboard_url="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["dashboard_url"])' "$runtime_dir/runtime.json")"
  panel_contract="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["panel_contract"])' "$runtime_dir/runtime.json")"

  echo "-- $scenario_id: browser interaction audit --"
  "$PY" scripts/run_interaction_audit.py \
    --manifest "$runtime_manifest" \
    --dashboard-url "$dashboard_url" \
    --panel-contract "$panel_contract" \
    --artifact-root "$ARTIFACT_ROOT" \
    --run-id "$RUN_ID"
  echo "Report: $ARTIFACT_ROOT/$scenario_id/$RUN_ID/report.json"
}

IFS=',' read -r -a selected_scenarios <<< "$SCENARIOS"
for raw_scenario_id in "${selected_scenarios[@]}"; do
  scenario_id="${raw_scenario_id//[[:space:]]/}"
  case "$scenario_id" in
    synthetic-controls)
      run_synthetic_scenario "$scenario_id"
      ;;
    redis-11835)
      run_grafana_scenario "$scenario_id" "infra/grafana/dashboards/redis-11835.json"
      ;;
    k8s-views-global)
      run_grafana_scenario "$scenario_id" "infra/grafana/dashboards/k8s-views-global.json"
      ;;
    *)
      echo "Unknown scenario: $scenario_id" >&2
      exit 2
      ;;
  esac
  run_browser_audit "$scenario_id"
done

echo "== interaction audit completed =="
if [[ "$KEEP_WORK" == "1" ]]; then
  echo "Preserved work directory: $WORK"
fi
