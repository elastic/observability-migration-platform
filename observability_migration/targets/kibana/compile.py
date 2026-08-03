# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""YAML compilation, upload, and post-validation sync helpers.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from observability_migration.core.assets.visual import refresh_visual_ir
from observability_migration.core.http import apply_subprocess_tls_env
from observability_migration.targets.kibana import layout as layout_module
from observability_migration.targets.kibana import lint as lint_module
from observability_migration.targets.kibana._kbtool import tool_argv
from observability_migration.targets.kibana.emit.esql_utils import extract_esql_columns

COMMAND_TIMEOUT_SECONDS = 90
VALIDATION_TIMEOUT_SECONDS = 120


def _run_command(cmd, timeout, env=None):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s: {shlex.join(str(part) for part in cmd)}"
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def compile_yaml(yaml_path, output_dir):
    cmd = tool_argv("kb-dashboard-cli") + [
        "compile",
        "--input-file",
        str(yaml_path),
        "--output-dir",
        str(output_dir),
    ]
    return _run_command(cmd, timeout=COMMAND_TIMEOUT_SECONDS)


def compile_all(yaml_dir, compiled_dir):
    Path(compiled_dir).mkdir(parents=True, exist_ok=True)
    results = []
    for yaml_file in sorted(Path(yaml_dir).glob("*.yaml")):
        out_dir = Path(compiled_dir) / yaml_file.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        success, output = compile_yaml(yaml_file, out_dir)
        results.append((yaml_file.name, success, output))
    return results


def lint_dashboard_yaml(yaml_dir):
    return lint_module.lint_dashboard_yaml(yaml_dir)


def validate_compiled_layout(compiled_dir):
    return layout_module.validate_compiled_layout(compiled_dir)


def detect_space_id_from_kibana_url(kibana_url):
    path_parts = [part for part in urlsplit(str(kibana_url or "")).path.split("/") if part]
    for idx, part in enumerate(path_parts[:-1]):
        if part == "s":
            return path_parts[idx + 1]
    return ""


def kibana_url_for_space(kibana_url, space_id=""):
    if not space_id:
        return str(kibana_url or "")
    split = urlsplit(str(kibana_url or ""))
    path_parts = [part for part in split.path.split("/") if part]
    normalized_parts = []
    idx = 0
    while idx < len(path_parts):
        if path_parts[idx] == "s" and idx + 1 < len(path_parts):
            idx += 2
            continue
        normalized_parts.append(path_parts[idx])
        idx += 1
    if space_id:
        normalized_parts.extend(["s", str(space_id)])
    normalized_path = "/" + "/".join(normalized_parts) if normalized_parts else ""
    return urlunsplit((split.scheme, split.netloc, normalized_path, split.query, split.fragment))


def upload_yaml(
    yaml_path,
    output_dir,
    kibana_url,
    space_id="",
    kibana_api_key="",
    verify: bool | str = True,
):
    upload_url = kibana_url_for_space(kibana_url, space_id)
    cmd = tool_argv("kb-dashboard-cli") + [
        "compile",
        "--input-file",
        str(yaml_path),
        "--output-dir",
        str(output_dir),
        "--upload",
        "--kibana-url",
        str(upload_url),
        "--no-browser",
    ]
    if kibana_api_key:
        cmd.extend(["--kibana-api-key", str(kibana_api_key)])
    # The uploader is a Python/aiohttp tool: --insecure (verify is False) only
    # takes effect via its own flag, not the Node TLS env vars.
    if verify is False:
        cmd.append("--kibana-no-ssl-verify")
    env = apply_subprocess_tls_env(verify, env=os.environ.copy())
    return _run_command(cmd, timeout=COMMAND_TIMEOUT_SECONDS, env=env)


def _sync_esql_panel_fields(yaml_panel, old_query, new_query):
    esql_config = yaml_panel.get("esql")
    if not isinstance(esql_config, dict):
        return False
    old_metric, old_by_cols = extract_esql_columns(old_query or "")
    new_metric, new_by_cols = extract_esql_columns(new_query or "")
    changed = False

    def _replace_field(container, old_value, new_value):
        nonlocal changed
        if not isinstance(container, dict):
            return
        if old_value and new_value and container.get("field") == old_value and old_value != new_value:
            container["field"] = new_value
            changed = True

    for key in ("primary", "metric"):
        _replace_field(esql_config.get(key), old_metric, new_metric)

    metrics = esql_config.get("metrics")
    if isinstance(metrics, list):
        for item in metrics:
            _replace_field(item, old_metric, new_metric)

    if old_by_cols and new_by_cols:
        dimension = esql_config.get("dimension")
        _replace_field(dimension, old_by_cols[0], new_by_cols[0])
        if isinstance(dimension, dict):
            if dimension.get("field") == "time_bucket":
                if dimension.get("data_type") != "date":
                    dimension["data_type"] = "date"
                    changed = True
            elif "data_type" in dimension:
                dimension.pop("data_type", None)
                changed = True

    if len(old_by_cols) > 1:
        breakdown = esql_config.get("breakdown")
        if isinstance(breakdown, dict):
            new_breakdown = new_by_cols[1] if len(new_by_cols) > 1 else ""
            if new_breakdown:
                _replace_field(breakdown, old_by_cols[1], new_breakdown)

    breakdowns = esql_config.get("breakdowns")
    if isinstance(breakdowns, list):
        for old_value, new_value in zip(old_by_cols, new_by_cols):
            for item in breakdowns:
                _replace_field(item, old_value, new_value)

    # Issue #109 safety net: a gauge's min/max/goal accessors must reference
    # columns the (resynced) query actually produces. If a resync replaced the
    # query with one that no longer carries the ``_gauge_*`` bounds (e.g. a
    # native-PROMQL gauge whose trailing ``| EVAL _gauge_*`` was dropped), keep
    # the bound only when its column still appears in the query. Otherwise drop
    # it so the gauge degrades gracefully instead of erroring with "Provided
    # column name or index is invalid".
    if esql_config.get("type") == "gauge":
        for bound_key in ("minimum", "maximum", "goal"):
            bound = esql_config.get(bound_key)
            if not isinstance(bound, dict):
                continue
            field = bound.get("field")
            if field and not re.search(rf"\b{re.escape(field)}\b", new_query or ""):
                esql_config.pop(bound_key, None)
                changed = True

    return changed


def _iter_leaf_panels(panels):
    """Yield mutable references to leaf panels, descending into sections."""
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            yield from _iter_leaf_panels(section.get("panels") or [])
        else:
            yield panel


def _pair_yaml_leaves_to_panel_results(leaf_panels, panel_results):
    """Pair YAML leaf panels to translation results without relying on list order.

    Post-translation layout, section wrapping, and polish can reorder YAML leaves
    relative to ``yaml_panel_results``. Index-based ``zip`` then writes the wrong
    query/placeholder into a panel and corrupts ``visual_ir``. Prefer an explicit
    ``_source_panel_id`` stamp when present; otherwise match by title (with a
    same-title ordinal for duplicates), then fall back to remaining positional
    slots.
    """
    leaves = list(leaf_panels or [])
    results = list(panel_results or [])
    paired: list[tuple[dict, object | None]] = [(leaf, None) for leaf in leaves]
    used_results: set[int] = set()

    def _claim(leaf_idx: int, result_idx: int) -> None:
        paired[leaf_idx] = (leaves[leaf_idx], results[result_idx])
        used_results.add(result_idx)

    # 1) Explicit source-panel id (survives only while still on the in-memory
    #    YAML dict; DashboardIR export strips underscore keys).
    by_source_id: dict[str, list[int]] = {}
    for idx, panel_result in enumerate(results):
        source_id = str(getattr(panel_result, "source_panel_id", "") or "").strip()
        if source_id:
            by_source_id.setdefault(source_id, []).append(idx)
    for leaf_idx, leaf in enumerate(leaves):
        source_id = str(leaf.get("_source_panel_id") or "").strip()
        if not source_id:
            continue
        candidates = by_source_id.get(source_id) or []
        for result_idx in candidates:
            if result_idx not in used_results:
                _claim(leaf_idx, result_idx)
                break

    # 2) Title match, preserving relative order among duplicate titles.
    by_title: dict[str, list[int]] = {}
    for idx, panel_result in enumerate(results):
        if idx in used_results:
            continue
        title = str(getattr(panel_result, "title", "") or "").strip()
        if title:
            by_title.setdefault(title, []).append(idx)
    title_cursors: dict[str, int] = {}
    for leaf_idx, (leaf, matched) in enumerate(paired):
        if matched is not None:
            continue
        title = str(leaf.get("title") or "").strip()
        if not title:
            continue
        candidates = by_title.get(title) or []
        cursor = title_cursors.get(title, 0)
        while cursor < len(candidates) and candidates[cursor] in used_results:
            cursor += 1
        if cursor < len(candidates):
            _claim(leaf_idx, candidates[cursor])
            title_cursors[title] = cursor + 1

    # 3) Positional fallback for anything still unmatched.
    unused = [idx for idx in range(len(results)) if idx not in used_results]
    unused_iter = iter(unused)
    for leaf_idx, (leaf, matched) in enumerate(paired):
        if matched is not None:
            continue
        try:
            result_idx = next(unused_iter)
        except StopIteration:
            paired[leaf_idx] = (leaf, None)
            continue
        _claim(leaf_idx, result_idx)

    return paired


_ESQL_PARAM_RE = re.compile(r"(?<!\?)\?(?!\?)(?P<name>[A-Za-z][A-Za-z0-9_]*)")
_ESQL_FIELD_CONTROL_RE = re.compile(r"\?\?(?P<name>[A-Za-z][A-Za-z0-9_]*)")
_ESQL_QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
_INTERNAL_ESQL_PARAMS = {"_tstart", "_tend", "_job"}


def _query_param_names(query):
    if not isinstance(query, str):
        return set()
    unquoted = _ESQL_QUOTED_RE.sub('""', query)
    return {
        match.group("name")
        for match in _ESQL_PARAM_RE.finditer(unquoted)
        if match.group("name") not in _INTERNAL_ESQL_PARAMS
    }


def _query_field_control_names(query):
    if not isinstance(query, str):
        return set()
    unquoted = _ESQL_QUOTED_RE.sub('""', query)
    return {
        match.group("name")
        for match in _ESQL_FIELD_CONTROL_RE.finditer(unquoted)
    }


def _esql_identifier(name):
    text = str(name or "")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        return text
    return "`" + text.replace("`", "``") + "`"


def _query_index(query):
    match = re.search(r"(?im)^\s*(?:FROM|TS)\s+([^\s|]+)", str(query or ""))
    return match.group(1).strip() if match else ""


def _infer_control_data_view(dashboard, leaf_panels):
    for panel in leaf_panels:
        esql_config = panel.get("esql") if isinstance(panel, dict) else None
        if isinstance(esql_config, dict):
            index = _query_index(esql_config.get("query"))
            if index:
                return index
    for control in dashboard.get("controls") or []:
        if not isinstance(control, dict):
            continue
        index = _query_index(control.get("query"))
        if index:
            return index
    return "metrics-*"


def _values_control_query(field_name, data_view):
    field = _esql_identifier(field_name)
    return (
        f"FROM {data_view or 'metrics-*'} | WHERE {field} IS NOT NULL"
        f" | STATS count = COUNT(*) BY {field}"
        f" | SORT {field} ASC | KEEP {field} | LIMIT 1000"
    )


def _ensure_controls_for_emitted_params(dashboard, leaf_panels):
    emitted: set[str] = set()
    emitted_fields: set[str] = set()
    for panel in leaf_panels:
        if not isinstance(panel, dict):
            continue
        esql_config = panel.get("esql")
        query = esql_config.get("query") if isinstance(esql_config, dict) else None
        emitted |= _query_param_names(query)
        emitted_fields |= _query_field_control_names(query)

    controls = dashboard.setdefault("controls", [])
    value_bound = {
        control.get("variable_name")
        for control in controls
        if isinstance(control, dict)
        and control.get("type") == "esql"
        and control.get("variable_name")
        and control.get("variable_type") != "fields"
    }
    # A fields control cannot bind ``?name``. Do not auto-create a duplicate
    # values control when the dashboard also still emits ``??name``; the lint
    # gate reports that unsatisfiable dual-semantics shape. When the field
    # control is stale, replace it with the required values control.
    missing = sorted(
        name
        for name in emitted
        if name not in value_bound and name not in emitted_fields
    )
    if not missing:
        return False

    data_view = _infer_control_data_view(dashboard, leaf_panels)
    for name in missing:
        controls[:] = [
            control
            for control in controls
            if not (
                isinstance(control, dict)
                and control.get("type") == "esql"
                and control.get("variable_name") == name
                and control.get("variable_type") == "fields"
            )
        ]
        controls.append(
            {
                "type": "esql",
                "label": name,
                "variable_name": name,
                "variable_type": "values",
                "query": _values_control_query(name, data_view),
                "multiple": False,
                "default": ".*",
            }
        )
    return True


def sync_result_queries_to_yaml(result, yaml_path):
    payload = yaml.safe_load(Path(yaml_path).read_text()) or {}
    dashboards = payload.get("dashboards") or []
    if not dashboards:
        return False
    dashboard = dashboards[0]
    panels = dashboard.get("panels") or []
    leaf_panels = list(_iter_leaf_panels(panels))
    yaml_panel_results = getattr(result, "yaml_panel_results", None)
    panel_results = yaml_panel_results if yaml_panel_results is not None else result.panel_results
    updated = False
    for yaml_panel, panel_result in _pair_yaml_leaves_to_panel_results(leaf_panels, panel_results):
        if panel_result is None:
            continue
        if str(panel_result.post_validation_action or "").startswith("placeholder_"):
            yaml_panel.pop("esql", None)
            yaml_panel["markdown"] = {
                "content": panel_result.post_validation_message or "*(Manual review required after validation.)*"
            }
            updated = True
            panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)
            continue
        esql_config = yaml_panel.get("esql")
        if not isinstance(esql_config, dict):
            panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)
            continue
        if not panel_result.esql_query:
            panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)
            continue
        existing_query = esql_config.get("query") or ""
        if existing_query == panel_result.esql_query:
            panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)
            continue
        esql_config["query"] = panel_result.esql_query
        _sync_esql_panel_fields(yaml_panel, existing_query, panel_result.esql_query)
        updated = True
        panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)
    if _ensure_controls_for_emitted_params(dashboard, leaf_panels):
        updated = True
    if updated:
        if getattr(result, "native_dashboard", None) is not None or getattr(result, "dashboard_ir", None) is not None:
            # Deferred import: dashboards_api.py imports kibana_url_for_space
            # from this module, so a module-level import here would cycle.
            # `DashboardIR` becomes the primary artifact from this point on:
            # rebuild it from the same in-memory `dashboard` dict just
            # mutated (post-validation fixes -- placeholder rewrites,
            # corrected queries/indexes/controls) and derive both the native
            # IR and the on-disk YAML *from that IR*, so neither one can
            # drift from post-validation fixes or from each other.
            from observability_migration.core.assets.dashboard import DashboardIR
            from observability_migration.targets.kibana.dashboards_api import (
                native_dashboard_from_ir,
            )

            dashboard_ir = DashboardIR.from_yaml_dict(dashboard, source_adapter="grafana")
            result.dashboard_ir = dashboard_ir
            payload = {"dashboards": [dashboard_ir.to_yaml_dict()]}
            native_dashboard, native_counts = native_dashboard_from_ir(dashboard_ir)
            result.native_dashboard = native_dashboard
            native_counts_dict, native_reasons = native_counts.as_dicts()
            result.native_dashboard_stats = {**native_counts_dict, "reasons": native_reasons}
        Path(yaml_path).write_text(
            yaml.dump(payload, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120)
        )
    return updated


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "compile_all",
    "compile_yaml",
    "detect_space_id_from_kibana_url",
    "kibana_url_for_space",
    "lint_dashboard_yaml",
    "sync_result_queries_to_yaml",
    "upload_yaml",
    "validate_compiled_layout",
]
