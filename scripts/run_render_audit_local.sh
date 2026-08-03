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
#   STACK_VERSION=9.5.0-SNAPSHOT docker compose -f parity-rig/docker-compose.render-audit.yml up -d --wait
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
  # Issue #282: also render-audit the late-bound grouping canary so the
  # ``by ($grouping)`` field-control path (and the concrete+variable collision
  # degrade) is exercised in a real Kibana render every nightly run.
  echo "-- generate late-bound grouping canaries for every field choice (issue #282) --"
  for grouping in exporter transport receiver; do
    "$PY" -c "import json; from observability_migration.core.coverage.canary import build_late_bound_grouping_canary; json.dump(build_late_bound_grouping_canary(default_grouping='$grouping'), open('$WORK/in/late-bound-grouping-$grouping.json','w'))"
  done
  # Gap A: also render-audit the label-matcher param canary so
  # ``metric{instance="$instance"}`` → ``?instance`` + control binding is
  # exercised in a real Kibana render every nightly run.
  echo "-- generate label-matcher param canaries for every instance choice (gap A) --"
  for instance in 'localhost:8888' 'remote:9100'; do
    safe="$(printf '%s' "$instance" | tr ':' '_')"
    "$PY" -c "import json; from observability_migration.core.coverage.canary import build_label_matcher_param_canary; json.dump(build_label_matcher_param_canary(default_instance='$instance'), open('$WORK/in/label-matcher-param-$safe.json','w'))"
  done
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
dashboard_rows="$(curl -fs "$KIBANA_URL/api/saved_objects/_find?type=dashboard&per_page=200" -H 'kbn-xsrf: true' \
  | "$PY" -c "import sys,json,pathlib; payload=json.load(sys.stdin); report=json.loads(pathlib.Path('$WORK/out/dashboards/migration_report.json').read_text()); titles={str(d.get('title') or d.get('dashboard_title') or '') for d in report.get('dashboards', [])}; [print(str(x['id']) + '\t' + str((x.get('attributes') or {}).get('title') or '')) for x in payload.get('saved_objects', []) if str((x.get('attributes') or {}).get('title') or '') in titles]")"
if [ -z "$dashboard_rows" ]; then
  echo "No uploaded dashboard ids matched this run's migration_report.json" >&2
  exit 1
fi
mkdir -p "$WORK/audit_reports"
while IFS=$'\t' read -r did dtitle; do
  safe_id="$(printf '%s' "$did" | tr -c 'A-Za-z0-9_.-' '_')"
  report_dir="$WORK/audit_reports/$safe_id"
  mkdir -p "$report_dir"
  "$PY" - "$WORK/out/dashboards/migration_report.json" "$dtitle" "$report_dir/migration_report.json" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
title = sys.argv[2]
target = Path(sys.argv[3])
report = json.loads(source.read_text())
report["dashboards"] = [
    dashboard
    for dashboard in report.get("dashboards", [])
    if str(dashboard.get("title") or dashboard.get("dashboard_title") or "") == title
]
target.write_text(json.dumps(report))
PY
  echo "  -- dashboard $did ($dtitle) --"
  "$PY" -m observability_migration.targets.kibana.render_audit_driver \
    --kibana-url "$KIBANA_URL" --dashboard-id "$did" \
    --time-from now-3h --time-to now --fail-on-error \
    --chrome-no-sandbox \
    --elements --migration-out "$report_dir" \
    --es-url "$ES_URL" --es-index "metrics-*,logs-*" || rc=1
done <<< "$dashboard_rows"

[ "$rc" -eq 0 ] && echo "== render audit PASSED for all dashboards ==" || { echo "== render audit FAILED =="; exit 1; }
echo "For live dashboard control behavior, run: make interaction-audit-local"
