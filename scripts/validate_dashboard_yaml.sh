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
DASHBOARD_LINT_WARNING_ALLOWLIST="${DASHBOARD_LINT_WARNING_ALLOWLIST:-esql-sql-syntax,dashboard-dataset-filter,panel-min-width,narrow-xy-chart-side-legend,esql-missing-sort-after-bucket,panel-height-for-content,gauge-goal-without-max,esql-field-escaping}"

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

lint_outputs=()
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
  lint_outputs+=( "${out_file}" "${file}" )
done

python3 - "${DASHBOARD_LINT_WARNING_ALLOWLIST}" "${lint_outputs[@]}" <<'PY'
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

allowlisted = {
    item.strip()
    for item in sys.argv[1].split(",")
    if item.strip()
}


def iter_leaf_panels(panels):
    for panel in panels or []:
        if not isinstance(panel, dict):
            continue
        section = panel.get("section")
        if isinstance(section, dict):
            yield from iter_leaf_panels(section.get("panels") or [])
        else:
            yield panel


def native_promql_panel_keys(yaml_path):
    if yaml is None:
        return set()
    try:
        payload = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    if not isinstance(payload, dict):
        return set()

    keys = set()
    for dashboard in payload.get("dashboards") or []:
        if not isinstance(dashboard, dict):
            continue
        dashboard_name = str(dashboard.get("name") or "")
        for panel in iter_leaf_panels(dashboard.get("panels") or []):
            esql_config = panel.get("esql")
            if not isinstance(esql_config, dict):
                continue
            query = esql_config.get("query")
            if isinstance(query, str) and query.lstrip().upper().startswith("PROMQL "):
                keys.add((dashboard_name, str(panel.get("title") or "")))
    return keys


def is_native_promql_esql_entry(entry, promql_panel_keys):
    if not str(entry.get("rule_id") or "").startswith("esql-"):
        return False
    key = (str(entry.get("dashboard_name") or ""), str(entry.get("panel_title") or ""))
    return key in promql_panel_keys


entries = []
parse_errors = 0
ignored_native_promql_entries = 0
if (len(sys.argv) - 2) % 2:
    print("ERROR: Internal lint argument mismatch.", file=sys.stderr)
    raise SystemExit(1)

for idx in range(2, len(sys.argv), 2):
    raw_path = sys.argv[idx]
    yaml_path = sys.argv[idx + 1]
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
        promql_panel_keys = native_promql_panel_keys(yaml_path)
        for entry in payload:
            if is_native_promql_esql_entry(entry, promql_panel_keys):
                ignored_native_promql_entries += 1
                continue
            entries.append(entry)

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
if ignored_native_promql_entries:
    print(
        "Ignored "
        f"{ignored_native_promql_entries} ES|QL lint entr"
        f"{'y' if ignored_native_promql_entries == 1 else 'ies'} "
        "on native PROMQL panels."
    )
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
