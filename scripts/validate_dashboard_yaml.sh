#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/validate_dashboard_yaml.sh [input_path]

Validate generated dashboard YAML with kb-dashboard-lint.

Arguments:
  input_path  Dashboard YAML file or directory containing dashboard YAML files
              (default: migration_output/dashboards/yaml)

Environment variables:
  KB_DASHBOARD_LINT_SOURCE
      uv tool source passed to `uvx --from`
      (default: kb-dashboard-lint@latest)

  DASHBOARD_LINT_WARNING_ALLOWLIST
      Comma-separated list of warning rule IDs to allow
      (default: esql-sql-syntax,dashboard-dataset-filter)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v uvx >/dev/null 2>&1; then
  echo "ERROR: uvx is required" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required" >&2
  exit 1
fi

INPUT_PATH="${1:-migration_output/dashboards/yaml}"
if [[ ! -e "${INPUT_PATH}" ]]; then
  echo "ERROR: YAML input path not found: ${INPUT_PATH}" >&2
  exit 1
fi

KB_DASHBOARD_LINT_SOURCE="${KB_DASHBOARD_LINT_SOURCE:-kb-dashboard-lint@latest}"
DASHBOARD_LINT_WARNING_ALLOWLIST="${DASHBOARD_LINT_WARNING_ALLOWLIST:-esql-sql-syntax,dashboard-dataset-filter,panel-min-width,narrow-xy-chart-side-legend,esql-missing-sort-after-bucket}"

shopt -s nullglob
if [[ -d "${INPUT_PATH}" ]]; then
  yaml_files=( "${INPUT_PATH}"/*.yaml "${INPUT_PATH}"/*.yml )
else
  yaml_files=( "${INPUT_PATH}" )
fi
shopt -u nullglob

if [[ "${#yaml_files[@]}" -eq 0 ]]; then
  echo "ERROR: No YAML files found in ${INPUT_PATH}" >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

echo "Running dashboard YAML lint checks..."

json_outputs=()
lint_exit=0
for file in "${yaml_files[@]}"; do
  out_file="${tmp_dir}/$(basename "${file}").lint.json"
  echo "--- $(basename "${file}") ---"
  if ! uvx --refresh --from "${KB_DASHBOARD_LINT_SOURCE}" kb-dashboard-lint check \
    --input-file "${file}" \
    --severity-threshold error \
    --format json > "${out_file}"; then
    lint_exit=1
  fi
  json_outputs+=( "${out_file}" )
done

python3 - "${DASHBOARD_LINT_WARNING_ALLOWLIST}" "${json_outputs[@]}" <<'PY'
import json
import sys
from pathlib import Path

allowlisted = {
    item.strip()
    for item in sys.argv[1].split(",")
    if item.strip()
}

entries = []
parse_errors = 0
for raw_path in sys.argv[2:]:
    path = Path(raw_path)
    if not path.exists() or path.stat().st_size == 0:
        continue
    with path.open(encoding="utf-8") as handle:
        try:
            payload = json.load(handle)
        except json.JSONDecodeError:
            parse_errors += 1
            dashboard_file = path.name.removesuffix(".lint.json")
            raw_output = path.read_text(encoding="utf-8", errors="replace").strip()
            print(
                f"ERROR: Linter did not produce JSON for {dashboard_file}.",
                file=sys.stderr,
            )
            if raw_output:
                print(raw_output[:1000], file=sys.stderr)
            continue
    if isinstance(payload, list):
        entries.extend(payload)

if parse_errors:
    raise SystemExit(1)

errors = [entry for entry in entries if entry.get("severity") == "error"]
warnings = [
    entry
    for entry in entries
    if entry.get("severity") == "warning" and entry.get("rule_id") not in allowlisted
]
info = [entry for entry in entries if entry.get("severity") == "info"]

print("")
print(
    f"Lint summary: errors={len(errors)}, warnings={len(warnings)}, info={len(info)}"
)

if errors:
    print("ERROR: Lint reported error severity issues.", file=sys.stderr)
    for entry in errors:
        dashboard = entry.get("dashboard_name", "<unknown dashboard>")
        panel = entry.get("panel_title", "<unknown panel>")
        rule_id = entry.get("rule_id", "<unknown rule>")
        message = entry.get("message", "").strip()
        print(f"  - [{dashboard}] {panel}: {rule_id} - {message}", file=sys.stderr)
    raise SystemExit(1)

if warnings:
    print(
        f"ERROR: Found {len(warnings)} non-allowlisted lint warning(s).",
        file=sys.stderr,
    )
    print(
        f"Allowlisted warning rule IDs: {', '.join(sorted(allowlisted)) or '(none)'}",
        file=sys.stderr,
    )
    for entry in warnings:
        dashboard = entry.get("dashboard_name", "<unknown dashboard>")
        panel = entry.get("panel_title", "<unknown panel>")
        rule_id = entry.get("rule_id", "<unknown rule>")
        message = entry.get("message", "").strip()
        print(f"  - [{dashboard}] {panel}: {rule_id} - {message}", file=sys.stderr)
    raise SystemExit(1)
PY

if [[ "${lint_exit}" -ne 0 ]]; then
  exit "${lint_exit}"
fi

echo "Dashboard YAML validation passed."
