"""YAML compilation, upload, and post-validation sync helpers.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from observability_migration.core.assets.visual import refresh_visual_ir
from observability_migration.targets.kibana.emit.esql_utils import extract_esql_columns

COMMAND_TIMEOUT_SECONDS = 90
VALIDATION_TIMEOUT_SECONDS = 120


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def _run_command(cmd, timeout):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s: {shlex.join(str(part) for part in cmd)}"
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def compile_yaml(yaml_path, output_dir):
    cmd = [
        "uvx",
        "kb-dashboard-cli",
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
    script = _repo_root() / "scripts" / "validate_dashboard_yaml.sh"
    return _run_command(["bash", str(script), str(yaml_dir)], timeout=VALIDATION_TIMEOUT_SECONDS)


def validate_compiled_layout(compiled_dir):
    script = _repo_root() / "scripts" / "validate_dashboard_layout.py"
    return _run_command([sys.executable, str(script), str(compiled_dir)], timeout=VALIDATION_TIMEOUT_SECONDS)


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


def upload_yaml(yaml_path, output_dir, kibana_url, space_id="", kibana_api_key=""):
    upload_url = kibana_url_for_space(kibana_url, space_id)
    cmd = [
        "uvx",
        "kb-dashboard-cli",
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
    return _run_command(cmd, timeout=COMMAND_TIMEOUT_SECONDS)


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

    return changed


def _iter_leaf_panels(panels):
    """Yield mutable references to leaf panels, descending into sections."""
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            yield from _iter_leaf_panels(section.get("panels") or [])
        else:
            yield panel


def sync_result_queries_to_yaml(result, yaml_path):
    payload = yaml.safe_load(Path(yaml_path).read_text()) or {}
    dashboards = payload.get("dashboards") or []
    if not dashboards:
        return False
    panels = dashboards[0].get("panels") or []
    leaf_panels = list(_iter_leaf_panels(panels))
    yaml_panel_results = getattr(result, "yaml_panel_results", None)
    panel_results = yaml_panel_results if yaml_panel_results is not None else result.panel_results
    updated = False
    for yaml_panel, panel_result in zip(leaf_panels, panel_results):
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
    if updated:
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
