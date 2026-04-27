#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0


set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/infra/docker-compose.yml"
PROJECT_NAME="${LOCAL_LAB_PROJECT:-infra}"
WITH_ALLOY=0
RECREATE=0

usage() {
  cat <<'EOF'
Usage: bash scripts/start_local_lab.sh [options]

Starts the local OTLP validation lab using infra/docker-compose.yml.

Options:
  --with-alloy   Start the optional Alloy profile and route OTLP generators through Alloy.
  --recreate     Force a clean recreate (compose down --remove-orphans) before startup.
  -h, --help     Show this help text.
EOF
}

while (($# > 0)); do
  case "$1" in
    --with-alloy)
      WITH_ALLOY=1
      ;;
    --recreate)
      RECREATE=1
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

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon is not running (start Docker Desktop / docker service and retry)" >&2
  exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ERROR: compose file not found at $COMPOSE_FILE" >&2
  exit 1
fi

if [[ $RECREATE -eq 1 ]]; then
  echo "Recreating local lab stack..."
  docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" down --remove-orphans || true
fi

if [[ $WITH_ALLOY -eq 1 ]]; then
  echo "Starting local lab with Alloy profile enabled..."
  OTLP_FORWARD_TARGET=alloy:14317 docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" --profile alloy up -d
else
  echo "Starting local lab..."
  docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d
fi

echo "Local lab startup command completed."
echo "  Grafana:       http://localhost:${LOCAL_GRAFANA_PORT:-13000}"
echo "  Elasticsearch: http://localhost:${LOCAL_ES_PORT:-19200}"
echo "  Kibana:        http://localhost:${LOCAL_KIBANA_PORT:-15601}"
