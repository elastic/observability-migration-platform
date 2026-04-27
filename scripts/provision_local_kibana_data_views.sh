#!/usr/bin/env bash

set -euo pipefail

KIBANA_URL="${KIBANA_URL:-http://localhost:${LOCAL_KIBANA_PORT:-15601}}"
SPACE_ID="${SPACE_ID:-}"

usage() {
  cat <<'EOF'
Usage: bash scripts/provision_local_kibana_data_views.sh [--space-id ID]

Creates or updates the local Kibana data views used by the OTLP validation lab.
The saved object IDs are pinned to the pattern names so uploaded dashboards can
resolve their index-pattern references reliably.
EOF
}

while (($# > 0)); do
  case "$1" in
    --space-id)
      shift
      SPACE_ID="${1:-}"
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

api_prefix="/api"
if [[ -n "$SPACE_ID" ]]; then
  api_prefix="/s/$SPACE_ID/api"
fi

create_data_view() {
  local object_id="$1"
  local title="$2"
  local time_field="$3"

  curl -fsS \
    -X POST \
    "$KIBANA_URL$api_prefix/saved_objects/index-pattern/$object_id?overwrite=true" \
    -H 'kbn-xsrf: true' \
    -H 'Content-Type: application/json' \
    --data-binary @- >/dev/null <<EOF
{"attributes":{"title":"$title","name":"$title","timeFieldName":"$time_field"}}
EOF

  printf 'Provisioned data view: %s\n' "$title"
}

create_data_view "metrics-*" "metrics-*" "@timestamp"
create_data_view "logs-*" "logs-*" "@timestamp"
create_data_view "traces-*" "traces-*" "@timestamp"
