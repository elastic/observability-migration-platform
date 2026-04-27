#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0


set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/infra/docker-compose.yml"
PROJECT_NAME="${LOCAL_LAB_PROJECT:-infra}"
REMOVE_VOLUMES=0

usage() {
  cat <<'EOF'
Usage: bash scripts/stop_local_lab.sh [options]

Stops the local OTLP validation lab started from infra/docker-compose.yml.

Options:
  --volumes      Remove named volumes in addition to stopping containers.
  -h, --help     Show this help text.
EOF
}

while (($# > 0)); do
  case "$1" in
    --volumes)
      REMOVE_VOLUMES=1
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

down_args=(down --remove-orphans)
if [[ $REMOVE_VOLUMES -eq 1 ]]; then
  down_args+=(--volumes)
fi

echo "Stopping local lab..."
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "${down_args[@]}"
echo "Local lab stopped."
