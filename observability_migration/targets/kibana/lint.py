# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""In-process dashboard-YAML lint gate (kb-dashboard-lint wrapper)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from observability_migration.targets.kibana._kbtool import tool_argv

VALIDATION_TIMEOUT_SECONDS = 120

DEFAULT_WARNING_ALLOWLIST = frozenset(
    {
        "esql-sql-syntax",
        "dashboard-dataset-filter",
        "panel-min-width",
        "narrow-xy-chart-side-legend",
        "esql-missing-sort-after-bucket",
        "panel-height-for-content",
        "gauge-goal-without-max",
        "esql-field-escaping",
    }
)


def _iter_leaf_panels(panels):
    for panel in panels or []:
        if not isinstance(panel, dict):
            continue
        section = panel.get("section")
        if isinstance(section, dict):
            yield from _iter_leaf_panels(section.get("panels") or [])
        else:
            yield panel


def _native_promql_panel_keys(yaml_path) -> set[tuple[str, str]]:
    try:
        payload = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    if not isinstance(payload, dict):
        return set()
    keys: set[tuple[str, str]] = set()
    for dashboard in payload.get("dashboards") or []:
        if not isinstance(dashboard, dict):
            continue
        dashboard_name = str(dashboard.get("name") or "")
        for panel in _iter_leaf_panels(dashboard.get("panels") or []):
            esql_config = panel.get("esql")
            if not isinstance(esql_config, dict):
                continue
            query = esql_config.get("query")
            if isinstance(query, str) and query.lstrip().upper().startswith("PROMQL "):
                keys.add((dashboard_name, str(panel.get("title") or "")))
    return keys


def _is_native_promql_entry(entry: dict, promql_keys: set[tuple[str, str]]) -> bool:
    if not str(entry.get("rule_id") or "").startswith("esql-"):
        return False
    key = (str(entry.get("dashboard_name") or ""), str(entry.get("panel_title") or ""))
    return key in promql_keys


def _run_lint_tool(yaml_file: Path) -> tuple[list[dict], str]:
    """Invoke kb-dashboard-lint on one file. Returns (findings, raw_stderr)."""
    cmd = tool_argv("kb-dashboard-lint") + [
        "check",
        "--input-file",
        str(yaml_file),
        "--severity-threshold",
        "error",
        "--format",
        "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=VALIDATION_TIMEOUT_SECONDS)
    raw = (proc.stdout or "").strip()
    if not raw:
        return [], (proc.stderr or "")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return [], raw
    return (payload if isinstance(payload, list) else []), ""


def lint_dashboard_yaml(yaml_dir, allowlist: frozenset[str] = DEFAULT_WARNING_ALLOWLIST) -> tuple[bool, str]:
    """Lint all YAML files under yaml_dir (or a single file). Returns (ok, output)."""
    base = Path(yaml_dir)
    if base.is_dir():
        yaml_files = sorted(base.glob("*.yaml")) + sorted(base.glob("*.yml"))
    else:
        yaml_files = [base]
    if not yaml_files:
        return False, f"ERROR: No YAML files found in {base}"

    lines: list[str] = ["Running dashboard YAML lint checks..."]
    entries: list[dict] = []
    ignored = 0
    for yaml_file in yaml_files:
        lines.append(f"--- {yaml_file.name} ---")
        findings, stderr = _run_lint_tool(yaml_file)
        if stderr and not findings:
            lines.append(stderr.strip()[:1000])
        promql_keys = _native_promql_panel_keys(yaml_file)
        for entry in findings:
            if _is_native_promql_entry(entry, promql_keys):
                ignored += 1
                continue
            entries.append(entry)

    if ignored:
        plural = "y" if ignored == 1 else "ies"
        lines.append(f"Ignored {ignored} ES|QL lint entr{plural} on native PROMQL panels.")

    errors = [e for e in entries if e.get("severity") == "error"]
    warnings = [e for e in entries if e.get("severity") == "warning" and e.get("rule_id") not in allowlist]
    info = [e for e in entries if e.get("severity") == "info"]
    lines.append(f"Lint summary: errors={len(errors)}, warnings={len(warnings)}, info={len(info)}")

    def _fmt(entry: dict) -> str:
        d = entry.get("dashboard_name", "<unknown dashboard>")
        p = entry.get("panel_title", "<unknown panel>")
        r = entry.get("rule_id", "<unknown rule>")
        m = str(entry.get("message", "")).strip()
        return f"  - [{d}] {p}: {r} - {m}"

    if errors:
        lines.append("ERROR: Lint reported error severity issues.")
        lines.extend(_fmt(e) for e in errors)
        return False, "\n".join(lines)
    if warnings:
        lines.append(f"ERROR: Found {len(warnings)} non-allowlisted lint warning(s).")
        lines.extend(_fmt(e) for e in warnings)
        return False, "\n".join(lines)

    lines.append("Dashboard YAML validation passed.")
    return True, "\n".join(lines)


__all__ = ["DEFAULT_WARNING_ALLOWLIST", "lint_dashboard_yaml"]
