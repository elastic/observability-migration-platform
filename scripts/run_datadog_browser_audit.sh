#!/usr/bin/env bash
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
#
# Plans the Datadog browser audit.
#
# 1. Generates e2e_datadog_run/browser_audit_plan.json from migration reports
# 2. Prints instructions for the agent to drive Chrome DevTools MCP
#
# The agent walks the plan, navigates Chrome to each URL, captures
# screenshot + console + network, and writes per-dashboard reports via
# observability_migration.adapters.source.datadog.browser_audit helpers.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  printf 'ERROR: .venv/bin/python not found. Run: make sync\n' >&2
  exit 1
fi

"$PYTHON_BIN" "$ROOT/scripts/datadog_browser_audit_driver.py"

cat <<'EOF'

Plan written to e2e_datadog_run/browser_audit_plan.json.

Next step (agent-driven):
  For each dashboard in the plan, drive Chrome DevTools MCP to:
    1. navigate_page --url <kibana_url>
    2. wait_for "[data-test-subj='globalLoadingIndicator-hidden']" (timeout 30s)
    3. take_screenshot --fullPage --filePath <screenshot_path>
    4. list_console_messages
    5. list_network_requests --resourceTypes xhr,fetch
  Then write per-dashboard browser_audit_report.json via
  observability_migration.adapters.source.datadog.browser_audit.write_finding
  and aggregate via write_summary to
  e2e_datadog_run/browser_audit_summary.json.
EOF
