#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
#
# Run the DD↔ES parity rig.
#
# Sources datadog_creds.env and serverless_creds.env, exports the
# expected env vars, and invokes scripts/run_datadog_parity.py.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"

DD_CREDS="${DD_CREDS_FILE:-$ROOT/datadog_creds.env}"
ES_CREDS="${ES_CREDS_FILE:-$ROOT/serverless_creds.env}"

if [[ ! -f "$DD_CREDS" ]]; then
  printf 'ERROR: Datadog credentials not found at %s\n' "$DD_CREDS" >&2
  exit 1
fi
if [[ ! -f "$ES_CREDS" ]]; then
  printf 'ERROR: Elastic credentials not found at %s\n' "$ES_CREDS" >&2
  exit 1
fi

set -a
# shellcheck source=/dev/null
source "$DD_CREDS"
# shellcheck source=/dev/null
source "$ES_CREDS"
set +a

# serverless_creds.env exports ELASTICSEARCH_ENDPOINT + KEY (Elastic
# convention); the parity script reads those same names.

PYTHON_BIN="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  printf 'ERROR: .venv/bin/python not found. Run: make sync\n' >&2
  exit 1
fi

exec "$PYTHON_BIN" "$ROOT/scripts/run_datadog_parity.py" "$@"
