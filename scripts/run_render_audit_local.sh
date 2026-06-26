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

# INPUT_DIR: a dir of Grafana dashboard JSON to migrate+render. Default: the
# generated kitchen-sink canary. Point it at a community corpus (e.g. one
# materialized by scripts/fetch_community_corpus.py) to render the whole set.
mkdir -p "$WORK/in"
if [ -n "${INPUT_DIR:-}" ]; then
  cp "$INPUT_DIR"/*.json "$WORK/in/"
  echo "-- using INPUT_DIR=$INPUT_DIR ($(ls "$WORK/in" | wc -l) dashboards) --"
else
  echo "-- generate kitchen-sink canary --"
  "$PY" -c "import json; from observability_migration.core.coverage.canary import build_grafana_canary; json.dump(build_grafana_canary(), open('$WORK/in/canary.json','w'))"
fi

echo "-- migrate + upload to local Kibana (security disabled, no key) --"
"$PY" -m observability_migration.adapters.source.grafana.cli \
  --source files --input-dir "$WORK/in" --output-dir "$WORK/out" --assets dashboards \
  --es-url "$ES_URL" --kibana-url "$KIBANA_URL" --upload --ensure-data-views

echo "-- seed telemetry (fresh, so instant panels populate up to now) --"
"$PY" scripts/setup_telemetry_data.py "$WORK/out/dashboards" \
  --es-endpoint "$ES_URL" --api-key "" --data-hours 3 --interval-sec 60

echo "-- render audit + per-panel element check for every uploaded dashboard --"
rc=0
ids="$(curl -fs "$KIBANA_URL/api/saved_objects/_find?type=dashboard&per_page=200" -H 'kbn-xsrf: true' \
  | "$PY" -c "import sys,json,pathlib; payload=json.load(sys.stdin); report=json.loads(pathlib.Path('$WORK/out/dashboards/migration_report.json').read_text()); titles={str(d.get('title') or d.get('dashboard_title') or '') for d in report.get('dashboards', [])}; [print(x['id']) for x in payload.get('saved_objects', []) if str((x.get('attributes') or {}).get('title') or '') in titles]")"
if [ -z "$ids" ]; then
  echo "No uploaded dashboard ids matched this run's migration_report.json" >&2
  exit 1
fi
for did in $ids; do
  echo "  -- dashboard $did --"
  "$PY" -m observability_migration.targets.kibana.render_audit_driver \
    --kibana-url "$KIBANA_URL" --dashboard-id "$did" \
    --time-from now-3h --time-to now --fail-on-error \
    --elements --migration-out "$WORK/out/dashboards" \
    --es-url "$ES_URL" --es-index "metrics-*,logs-*" || rc=1
done

[ "$rc" -eq 0 ] && echo "== render audit PASSED for all dashboards ==" || { echo "== render audit FAILED =="; exit 1; }
