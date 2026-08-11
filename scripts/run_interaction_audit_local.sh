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
# Speed knobs (defaults favor interactive iteration; set FULL=1 for denser nightly seed).
BOOTSTRAP_MIGRATE="${BOOTSTRAP_MIGRATE:-0}"
INSTALL_BROWSER="${INSTALL_BROWSER:-auto}"
SCREENSHOTS="${SCREENSHOTS:-on-fail}"
SETTLE_TIMEOUT_SECONDS="${SETTLE_TIMEOUT_SECONDS:-20}"
SETTLE_POLL_INTERVAL_MS="${SETTLE_POLL_INTERVAL_MS:-75}"
SETTLE_STABLE_POLLS="${SETTLE_STABLE_POLLS:-2}"
# SKIP_MIGRATE=1 reuses a prior KEEP_WORK final/ tree (browser-only iteration).
SKIP_MIGRATE="${SKIP_MIGRATE:-0}"
# LIVE_VALIDATE=0 keeps YAML lint but skips post-seed ES|QL (faster local loops).
LIVE_VALIDATE="${LIVE_VALIDATE:-1}"
if [[ "${FULL:-0}" == "1" ]]; then
  INTERACTION_DATA_HOURS="${INTERACTION_DATA_HOURS:-3}"
  INTERACTION_INTERVAL_SEC="${INTERACTION_INTERVAL_SEC:-60}"
  INTERACTION_MAX_COMBINATIONS="${INTERACTION_MAX_COMBINATIONS:-12}"
  LIVE_VALIDATE=1
else
  # Enough history for now-3h dashboard windows without 50k-doc seeds.
  INTERACTION_DATA_HOURS="${INTERACTION_DATA_HOURS:-1}"
  INTERACTION_INTERVAL_SEC="${INTERACTION_INTERVAL_SEC:-300}"
  INTERACTION_MAX_COMBINATIONS="${INTERACTION_MAX_COMBINATIONS:-8}"
fi

phase() {
  echo "[$(date -u +%H:%M:%S)] $*"
}
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
echo "Seed: hours=$INTERACTION_DATA_HOURS interval=${INTERACTION_INTERVAL_SEC}s combos=$INTERACTION_MAX_COMBINATIONS screenshots=$SCREENSHOTS skip_migrate=$SKIP_MIGRATE live_validate=$LIVE_VALIDATE"

"$PY" -m observability_migration.targets.kibana.interaction_audit_local \
  check-environment \
  --stack-version "$STACK_VERSION" \
  --scenarios "$SCENARIOS" \
  --es-url "$ES_URL" \
  --kibana-url "$KIBANA_URL"

ensure_playwright_browser() {
  local mode="${INSTALL_BROWSER}"
  if [[ "$mode" == "0" || "$mode" == "never" ]]; then
    echo "-- playwright install: skipped (INSTALL_BROWSER=$mode) --"
    return 0
  fi
  if [[ "$mode" == "auto" ]]; then
    if "$PY" - <<'PY'
from playwright.sync_api import sync_playwright
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        browser.close()
except Exception:
    raise SystemExit(1)
raise SystemExit(0)
PY
    then
      echo "-- playwright install: chromium already available --"
      return 0
    fi
  fi
  echo "-- playwright install chromium --"
  "$PY" -m playwright install chromium
}

ensure_playwright_browser

run_grafana_scenario() {
  local scenario_id="$1"
  local source_relative="$2"
  local control_schema_relative="${3:-}"
  local scenario_root="$WORK/scenarios/$scenario_id"
  local input_dir="$scenario_root/input"
  local bootstrap_root="$WORK/bootstrap/$scenario_id"
  local final_root="$WORK/final/$scenario_id"
  local final_artifacts="$final_root/dashboards"
  local control_schema_args=()

  if [[ "$SKIP_MIGRATE" == "1" ]]; then
    if [[ ! -d "$final_artifacts" ]]; then
      echo "ERROR: SKIP_MIGRATE=1 but missing $final_artifacts (reuse a KEEP_WORK tree via WORK_DIR=...)" >&2
      exit 2
    fi
    phase "$scenario_id: skip migrate/seed (reusing $final_artifacts)"
    return 0
  fi

  mkdir -p "$input_dir"
  cp "$PROJECT_ROOT/$source_relative" "$input_dir/"

  if [[ -n "$control_schema_relative" && -f "$PROJECT_ROOT/$control_schema_relative" ]]; then
    control_schema_args=(--control-schema "$PROJECT_ROOT/$control_schema_relative")
  fi

  if [[ "$BOOTSTRAP_MIGRATE" == "1" ]]; then
    phase "$scenario_id: bootstrap migrate (isolated source, no upload)"
    "$PY" -m observability_migration.adapters.source.grafana.cli \
      --source files \
      --input-dir "$input_dir" \
      --output-dir "$bootstrap_root" \
      --assets dashboards
  else
    phase "$scenario_id: bootstrap migrate skipped (set BOOTSTRAP_MIGRATE=1 to enable)"
  fi

  # Skip --validate here: seed has not run yet, so live ES|QL fails loudly and
  # wastes minutes. validate-final below runs after telemetry is present.
  phase "$scenario_id: live-schema migrate and native upload"
  "$PY" -m observability_migration.adapters.source.grafana.cli \
    --source files \
    --input-dir "$input_dir" \
    --output-dir "$final_root" \
    --assets dashboards \
    --es-url "$ES_URL" \
    --es-api-key "$ES_API_KEY" \
    "${control_schema_args[@]}" \
    --kibana-url "$KIBANA_URL" \
    --kibana-api-key "$KIBANA_API_KEY" \
    --upload \
    --ensure-data-views

  phase "$scenario_id: final-artifact telemetry seed"
  "$PY" scripts/setup_telemetry_data.py "$final_artifacts" \
    --es-endpoint "$ES_URL" \
    --api-key "$ES_API_KEY" \
    --data-hours "$INTERACTION_DATA_HOURS" \
    --interval-sec "$INTERACTION_INTERVAL_SEC" \
    --max-combinations "$INTERACTION_MAX_COMBINATIONS"

  if [[ "$LIVE_VALIDATE" == "1" ]]; then
    phase "$scenario_id: artifact lint and live ES|QL validation"
    "$PY" -m observability_migration.targets.kibana.interaction_audit_local \
      validate-final \
      --migration-out "$final_artifacts" \
      --es-url "$ES_URL" \
      --api-key "$ES_API_KEY"
  else
    phase "$scenario_id: artifact lint only (LIVE_VALIDATE=0)"
    "$PY" - <<PY
from pathlib import Path
from observability_migration.targets.kibana.interaction_audit_local import lint_migration_artifacts
lint_migration_artifacts(Path(r"$final_artifacts"))
print("lint ok")
PY
  fi
}

run_synthetic_scenario() {
  local scenario_id="$1"
  local final_root="$WORK/final/$scenario_id"
  local final_artifacts="$final_root/dashboards"

  if [[ "$SKIP_MIGRATE" == "1" ]]; then
    if [[ ! -d "$final_artifacts" ]]; then
      echo "ERROR: SKIP_MIGRATE=1 but missing $final_artifacts" >&2
      exit 2
    fi
    phase "$scenario_id: skip synthetic prepare/seed (reusing $final_artifacts)"
    return 0
  fi

  phase "$scenario_id: write Native IR-derived artifacts"
  "$PY" -m observability_migration.targets.kibana.interaction_audit_local \
    prepare-synthetic \
    --artifact-dir "$final_artifacts"

  phase "$scenario_id: seed telemetry from the IR-derived YAML contract"
  "$PY" scripts/setup_telemetry_data.py "$final_artifacts" \
    --es-endpoint "$ES_URL" \
    --api-key "$ES_API_KEY" \
    --data-hours "$INTERACTION_DATA_HOURS" \
    --interval-sec "$INTERACTION_INTERVAL_SEC" \
    --max-combinations "$INTERACTION_MAX_COMBINATIONS"

  "$PY" -m observability_migration.app.cli cluster ensure-data-views \
    --kibana-url "$KIBANA_URL" \
    --kibana-api-key "$KIBANA_API_KEY" \
    --data-view-patterns "metrics-*"

  phase "$scenario_id: upload reviewed native artifact"
  "$PY" -m observability_migration.app.cli upload \
    --artifact-dir "$final_artifacts" \
    --kibana-url "$KIBANA_URL" \
    --kibana-api-key "$KIBANA_API_KEY"
}

run_browser_audit() {
  local scenario_id="$1"
  local final_artifacts="$WORK/final/$scenario_id/dashboards"
  local runtime_dir="$WORK/runtime/$scenario_id"
  local manifest="$PROJECT_ROOT/parity-rig/interaction-scenarios/$scenario_id.yaml"

  phase "$scenario_id: resolve exact dashboard and runtime panel contract"
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

  phase "$scenario_id: browser interaction audit"
  "$PY" scripts/run_interaction_audit.py \
    --manifest "$runtime_manifest" \
    --dashboard-url "$dashboard_url" \
    --panel-contract "$panel_contract" \
    --artifact-root "$ARTIFACT_ROOT" \
    --run-id "$RUN_ID" \
    --screenshots "$SCREENSHOTS" \
    --timeout-seconds "$SETTLE_TIMEOUT_SECONDS" \
    --poll-interval-ms "$SETTLE_POLL_INTERVAL_MS" \
    --stable-polls "$SETTLE_STABLE_POLLS"
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
      run_grafana_scenario "$scenario_id" "infra/grafana/dashboards/redis-11835.json" "infra/grafana/dashboards/control_schemas/redis-11835.json"
      ;;
    k8s-views-global)
      run_grafana_scenario "$scenario_id" "infra/grafana/dashboards/k8s-views-global.json" "infra/grafana/dashboards/control_schemas/k8s-views-global.json"
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
