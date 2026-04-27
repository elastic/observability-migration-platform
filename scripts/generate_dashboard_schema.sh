#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0


# Regenerate the dashboard schema from kb-dashboard-core.
#
# Optional overrides:
#   SCHEMA_CORE_SOURCE="kb-dashboard-core==<version>" bash scripts/generate_dashboard_schema.sh
#   PYTHON_BIN=python3.12 bash scripts/generate_dashboard_schema.sh

set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage: bash scripts/generate_dashboard_schema.sh

Regenerate the canonical dashboard schema artifacts under docs/dashboards/.

The script provisions a temporary virtualenv, installs kb-dashboard-core, and
emits docs/dashboards/schema.json plus docs/dashboards/schema.toon when npx is
available.

Optional overrides:
  SCHEMA_CORE_SOURCE  Pinned kb-dashboard-core spec, e.g. "kb-dashboard-core==1.2.3"
  PYTHON_BIN          Python interpreter to use for the throw-away venv

Pass -h or --help to print this message and exit without side effects.
EOF
  exit 0
fi

SCHEMA_CORE_SOURCE="${SCHEMA_CORE_SOURCE:-kb-dashboard-core}"
PYTHON_BIN="${PYTHON_BIN:-}"
DOCS_DIR="docs/dashboards"
SCHEMA_JSON="${DOCS_DIR}/schema.json"
SCHEMA_TOON="${DOCS_DIR}/schema.toon"
VENV_DIR=""
TMP_SCHEMA_JSON=""

cleanup() {
  if [[ -n "${VENV_DIR}" && -d "${VENV_DIR}" ]]; then
    rm -rf "${VENV_DIR}"
  fi
  if [[ -n "${TMP_SCHEMA_JSON}" && -f "${TMP_SCHEMA_JSON}" ]]; then
    rm -f "${TMP_SCHEMA_JSON}"
  fi
}
trap cleanup EXIT

if [[ -z "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "ERROR: python3 (or python) is required" >&2
    exit 1
  fi
fi

if [[ ! -d "docs" ]]; then
  echo "ERROR: docs directory not found" >&2
  exit 1
fi

mkdir -p "${DOCS_DIR}"
VENV_DIR="$(mktemp -d "${TMPDIR:-/tmp}/dashboard-schema-venv.XXXXXX")"
TMP_SCHEMA_JSON="$(mktemp "${TMPDIR:-/tmp}/dashboard-schema.XXXXXX")"

"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install -U "${SCHEMA_CORE_SOURCE}"

"${VENV_DIR}/bin/python" -c '
import json
import sys
from kb_dashboard_core.loader import DashboardConfig


def sort_enums(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "enum" and isinstance(value, list):
                try:
                    obj[key] = sorted(value, key=lambda item: str(item))
                except Exception:
                    pass
            else:
                sort_enums(value)
    elif isinstance(obj, list):
        for item in obj:
            sort_enums(item)


schema = DashboardConfig.model_json_schema()
sort_enums(schema)

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(schema, handle, indent=2, sort_keys=True)
    handle.write("\n")
' "${TMP_SCHEMA_JSON}"

cp "${TMP_SCHEMA_JSON}" "${SCHEMA_JSON}"
echo "Wrote ${SCHEMA_JSON}"

if command -v npx >/dev/null 2>&1; then
  npx @toon-format/cli "${TMP_SCHEMA_JSON}" -o "${SCHEMA_TOON}"
  echo "Wrote ${SCHEMA_TOON}"
else
  echo "Skipping ${SCHEMA_TOON} (npx not available)"
fi
