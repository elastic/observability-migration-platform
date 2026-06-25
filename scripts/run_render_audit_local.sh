#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
#
# Render-audit the kitchen-sink canary against a LOCAL no-SSO Kibana.
#
# The serverless render audit needs an interactive SSO login (no CI automation).
# This runs the same gate against the local security-disabled stack from
# parity-rig/docker-compose.render-audit.yml, so headless Chrome can drive the
# dashboard with no auth wall.
#
# Usage (stack must already be up and healthy):
#   STACK_VERSION=9.1.0 docker compose -f parity-rig/docker-compose.render-audit.yml up -d --wait
#   bash scripts/run_render_audit_local.sh
#
# Env overrides: ES_URL (default http://localhost:9200), KIBANA_URL
# (default http://localhost:5601).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

ES_URL="${ES_URL:-http://localhost:9200}"
KIBANA_URL="${KIBANA_URL:-http://localhost:5601}"
PY="${PY:-.venv/bin/python}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== render-audit (local, no SSO): ES=$ES_URL KIBANA=$KIBANA_URL =="

echo "-- generate kitchen-sink canary --"
mkdir -p "$WORK/in"
"$PY" -c "import json; from observability_migration.core.coverage.canary import build_grafana_canary; json.dump(build_grafana_canary(), open('$WORK/in/canary.json','w'))"

echo "-- migrate + upload canary to local Kibana (security disabled, no key) --"
"$PY" -m observability_migration.adapters.source.grafana.cli \
  --source files --input-dir "$WORK/in" --output-dir "$WORK/out" --assets dashboards \
  --es-url "$ES_URL" --kibana-url "$KIBANA_URL" --upload --ensure-data-views

echo "-- seed canary telemetry --"
"$PY" scripts/setup_telemetry_data.py "$WORK/out/dashboards" \
  --es-endpoint "$ES_URL" --api-key "" --data-hours 3 --interval-sec 60

echo "-- resolve uploaded dashboard id --"
DASH_ID="$(curl -fs "$KIBANA_URL/api/dashboards" -H 'kbn-xsrf: true' \
  | "$PY" -c "import sys,json; d=json.load(sys.stdin); print(next(x['id'] for x in d['dashboards'] if 'canary' in x['data']['title'].lower()))")"
echo "   dashboard id: $DASH_ID"

echo "-- render audit (headless Chrome, no SSO) --"
"$PY" -m observability_migration.targets.kibana.render_audit_driver \
  --kibana-url "$KIBANA_URL" --dashboard-id "$DASH_ID" \
  --time-from now-3h --time-to now --fail-on-error

echo "== render audit PASSED =="
