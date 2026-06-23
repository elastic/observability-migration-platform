#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0


from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from observability_migration.core.http import apply_tls

from .esql_validate import materialize_dashboard_time_query

DEFAULT_CHROME_CANDIDATES = (
    "chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
DEFAULT_SAVED_OBJECTS_PER_PAGE = 1000
GRID_WIDTH = 48
QUERY_EXPECTED_PANEL_TYPES = {"lens", "search", "visualization"}
NO_QUERY_EXPECTED_PANEL_TYPES = {"control_group", "markdown"}
BROWSER_ERROR_PATTERNS = (
    r"dashboardPanelError",
    r"embPanel__error",
    r"Error loading data",
    r"An error occurred while loading this panel",
    r"Provided column name or index is invalid",
    r"Could not locate that (?:data view|index-pattern)",
    r"No matching data view",
    r"Embeddable factory",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Validate uploaded Kibana dashboards for query errors, layout overlaps, and optional screenshots."
    )
    parser.add_argument("--kibana-url", default="http://localhost:5601")
    parser.add_argument(
        "--kibana-api-key",
        default=os.getenv("KIBANA_API_KEY", os.getenv("KEY", "")),
        help="API key for Kibana saved-object requests (defaults to KIBANA_API_KEY or KEY env var).",
    )
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument(
        "--es-api-key",
        default=os.getenv("ES_API_KEY", os.getenv("KEY", "")),
        help="API key for Elasticsearch runtime validation (defaults to ES_API_KEY or KEY env var).",
    )
    parser.add_argument("--space-id", default="")
    parser.add_argument("--output", default="uploaded_dashboard_smoke_report.json")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument(
        "--saved-objects-per-page",
        type=int,
        default=DEFAULT_SAVED_OBJECTS_PER_PAGE,
        help="Saved objects page size to request from Kibana during dashboard discovery.",
    )
    parser.add_argument(
        "--validation-workers",
        type=int,
        default=int(os.getenv("SMOKE_VALIDATION_WORKERS", "6")),
        help="Thread pool size for panel ES|QL runtime validation.",
    )
    parser.add_argument(
        "--dashboard-title",
        action="append",
        default=[],
        help="Restrict validation to one or more exact dashboard titles.",
    )
    parser.add_argument(
        "--dashboard-id",
        action="append",
        default=[],
        help="Restrict validation to one or more Kibana dashboard IDs.",
    )
    parser.add_argument(
        "--capture-screenshots",
        action="store_true",
        help="Capture dashboard screenshots using headless Chrome/Chromium.",
    )
    parser.add_argument(
        "--segmented-screenshots",
        action="store_true",
        help="Capture multiple viewport screenshots per dashboard for visual panel review.",
    )
    parser.add_argument(
        "--browser-audit",
        action="store_true",
        help="Use headless Chrome/Chromium to dump dashboard HTML and scan for visible runtime errors.",
    )
    parser.add_argument(
        "--screenshot-dir",
        default="",
        help="Directory for PNG screenshots. Defaults to <report-stem>_screenshots.",
    )
    parser.add_argument(
        "--browser-audit-dir",
        default="",
        help="Directory for saved browser-audit HTML snapshots. Defaults to <report-stem>_browser_audit.",
    )
    parser.add_argument(
        "--chrome-binary",
        default=os.getenv("CHROME_BINARY", ""),
        help="Path to the Chrome/Chromium binary. Auto-detected when omitted.",
    )
    parser.add_argument(
        "--time-from",
        default="now-1h",
        help="Dashboard time range start for screenshots.",
    )
    parser.add_argument(
        "--time-to",
        default="now",
        help="Dashboard time range end for screenshots.",
    )
    parser.add_argument(
        "--window-width",
        type=int,
        default=1600,
        help="Screenshot browser width in pixels.",
    )
    parser.add_argument(
        "--window-height",
        type=int,
        default=2200,
        help="Screenshot browser height in pixels.",
    )
    parser.add_argument(
        "--segment-count",
        type=int,
        default=6,
        help="Number of viewport chunks to capture when --segmented-screenshots is enabled.",
    )
    parser.add_argument(
        "--segment-overlap",
        type=int,
        default=200,
        help="Vertical pixel overlap between segmented screenshots.",
    )
    parser.add_argument(
        "--virtual-time-budget-ms",
        type=int,
        default=30000,
        help="Headless Chrome virtual-time budget in milliseconds.",
    )
    parser.add_argument(
        "--screenshot-retries",
        type=int,
        default=1,
        help="Retry failed screenshots this many times with a larger virtual-time budget.",
    )
    parser.add_argument(
        "--fail-on-runtime-errors",
        action="store_true",
        help="Exit non-zero when one or more panels fail ES|QL runtime validation.",
    )
    parser.add_argument(
        "--fail-on-layout-issues",
        action="store_true",
        help="Exit non-zero when uploaded dashboards have layout overlaps or invalid bounds.",
    )
    parser.add_argument(
        "--fail-on-empty-panels",
        action="store_true",
        help="Exit non-zero when validated panels return zero rows.",
    )
    parser.add_argument(
        "--fail-on-not-runtime-checked",
        action="store_true",
        help="Exit non-zero when a panel looked query-backed but no runnable ES|QL query could be extracted.",
    )
    parser.add_argument(
        "--fail-on-browser-errors",
        action="store_true",
        help="Exit non-zero when browser audit detects visible dashboard runtime errors.",
    )
    return parser.parse_args()


def _safe_stem(value):
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in (value or "").lower()).strip("_")


def _kibana_api_prefix(space_id):
    if space_id:
        return f"/s/{space_id}/api"
    return "/api"


def _kibana_app_prefix(kibana_url, space_id):
    return f"{kibana_url}{f'/s/{space_id}' if space_id else ''}/app/dashboards#/view"


def build_dashboard_url(kibana_url, space_id, dashboard_id, time_from="", time_to="", embed=True):
    params = []
    if embed:
        params.append("embed=true")
    if time_from and time_to:
        params.append(f"_g=(time:(from:{time_from},to:{time_to}))")
    suffix = f"?{'&'.join(params)}" if params else ""
    return f"{_kibana_app_prefix(kibana_url, space_id)}/{dashboard_id}{suffix}"


def discover_chrome_binary(preferred=""):
    for candidate in [preferred, *DEFAULT_CHROME_CANDIDATES]:
        if not candidate:
            continue
        if Path(candidate).exists():
            return str(Path(candidate))
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return ""


def kibana_get(session, kibana_url, space_id, path, **kwargs):
    return session.get(f"{kibana_url}{_kibana_api_prefix(space_id)}{path}", **kwargs)


def kibana_post(session, kibana_url, space_id, path, **kwargs):
    return session.post(f"{kibana_url}{_kibana_api_prefix(space_id)}{path}", **kwargs)


def _saved_objects_find_unavailable(response):
    if response.status_code not in {400, 404}:
        return False
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    message = str(payload.get("message", "") or getattr(response, "text", ""))
    return "not available with the current configuration" in message.lower()


def _load_dashboards_via_export(session, kibana_url, space_id, timeout):
    response = kibana_post(
        session,
        kibana_url,
        space_id,
        "/saved_objects/_export",
        json={"type": ["dashboard"], "excludeExportDetails": True},
        timeout=timeout,
    )
    response.raise_for_status()
    dashboards = []
    for raw_line in response.text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        item = json.loads(line)
        if item.get("type") != "dashboard":
            continue
        dashboards.append(item)
    return sorted(dashboards, key=lambda item: item.get("attributes", {}).get("title", ""))


def load_dashboards(session, kibana_url, space_id, timeout, per_page=DEFAULT_SAVED_OBJECTS_PER_PAGE):
    dashboards = []
    seen_ids = set()
    page = 1
    while True:
        response = kibana_get(
            session,
            kibana_url,
            space_id,
            "/saved_objects/_find",
            params={"type": "dashboard", "per_page": per_page, "page": page},
            timeout=timeout,
        )
        if _saved_objects_find_unavailable(response):
            return _load_dashboards_via_export(session, kibana_url, space_id, timeout)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("saved_objects", []) or []
        total = int(payload.get("total", 0) or 0)
        for item in items:
            dashboard_id = str(item.get("id", "") or "")
            if dashboard_id and dashboard_id in seen_ids:
                continue
            if dashboard_id:
                seen_ids.add(dashboard_id)
            dashboards.append(item)
        if not items or len(items) < per_page or (total and len(dashboards) >= total):
            break
        page += 1
    return sorted(dashboards, key=lambda item: item.get("attributes", {}).get("title", ""))


def load_dashboard(session, kibana_url, space_id, dashboard_id, timeout):
    response = kibana_get(
        session,
        kibana_url,
        space_id,
        f"/saved_objects/dashboard/{dashboard_id}",
        timeout=timeout,
    )
    if _saved_objects_find_unavailable(response):
        return _load_dashboard_via_export(session, kibana_url, space_id, dashboard_id, timeout)
    response.raise_for_status()
    return response.json()


def _load_dashboard_via_export(session, kibana_url, space_id, dashboard_id, timeout):
    """Fallback for Serverless where GET saved_objects is disabled."""
    response = kibana_post(
        session,
        kibana_url,
        space_id,
        "/saved_objects/_export",
        json={
            "objects": [{"type": "dashboard", "id": dashboard_id}],
            "includeReferencesDeep": False,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    for raw_line in response.text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("type") == "dashboard" and item.get("id") == dashboard_id:
            return item
    raise ValueError(f"Dashboard {dashboard_id} not found via _export")


def _kbn_filter_to_dsl(kbn_filter):
    """Convert one compiled Kibana filter into ``(dsl, negate)`` or ``None``.

    Mirrors how Kibana resolves a saved-object filter into Query DSL: disabled
    filters are dropped, ``meta.negate`` routes the clause to ``must_not``, and
    combined (AND/OR) filters reconstruct their DSL from ``meta.params`` since
    their top-level ``query`` is empty.
    """
    meta = kbn_filter.get("meta", {}) or {}
    if meta.get("disabled"):
        return None
    negate = bool(meta.get("negate", False))
    query = kbn_filter.get("query") or {}
    if query:
        return query, negate
    if meta.get("type") == "combined":
        relation = str(meta.get("relation", "AND") or "AND").upper()
        clauses = []
        for param in meta.get("params", []) or []:
            resolved = _kbn_filter_to_dsl(param)
            if resolved is None:
                continue
            sub_dsl, sub_negate = resolved
            clauses.append({"bool": {"must_not": [sub_dsl]}} if sub_negate else sub_dsl)
        if not clauses:
            return None
        if relation == "OR":
            return {"bool": {"should": clauses, "minimum_should_match": 1}}, negate
        return {"bool": {"filter": clauses}}, negate
    return None


def extract_dashboard_filter_dsl(attributes):
    """Build the dashboard-level Query DSL filter from a saved object's attributes.

    Kibana Lens merges ``kibanaSavedObjectMeta.searchSourceJSON`` filters into the
    ``/_query`` request before dispatching to Elasticsearch; the smoke validator
    must do the same so a mismatched filter (e.g. wrong ``data_stream.dataset``)
    is caught instead of producing a false-clean report (#113). Returns a bool
    Query DSL object, or ``None`` when there is no effective filter.
    """
    meta = attributes.get("kibanaSavedObjectMeta", {}) or {}
    raw = meta.get("searchSourceJSON", "") or ""
    if isinstance(raw, str):
        if not raw or raw == "{}":
            return None
        try:
            search_source = json.loads(raw)
        except json.JSONDecodeError:
            return None
    else:
        search_source = raw or {}
    must = []
    must_not = []
    for kbn_filter in search_source.get("filter", []) or []:
        resolved = _kbn_filter_to_dsl(kbn_filter)
        if resolved is None:
            continue
        dsl, negate = resolved
        (must_not if negate else must).append(dsl)
    if not must and not must_not:
        return None
    bool_query = {}
    if must:
        bool_query["filter"] = must
    if must_not:
        bool_query["must_not"] = must_not
    return {"bool": bool_query}


def validate_esql(es_url, query, timeout, es_api_key="", session=None, dsl_filter=None):
    materialized_query = materialize_dashboard_time_query(query)
    headers = {"Authorization": f"ApiKey {es_api_key}"} if es_api_key else None
    client = session or requests
    body = {"query": materialized_query}
    if dsl_filter:
        body["filter"] = dsl_filter
    response = client.post(
        f"{es_url}/_query",
        params={"format": "json"},
        json=body,
        headers=headers,
        timeout=timeout,
    )
    if response.status_code == 200:
        payload = response.json()
        return {
            "status": "pass",
            "rows": len(payload.get("values", [])),
            "columns": [column.get("name", "") for column in payload.get("columns", [])],
            "error": "",
            "materialized_query": materialized_query,
        }
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    error = body.get("error", {})
    if isinstance(error, dict):
        reason = error.get("reason", "") or str(error.get("caused_by", {}).get("reason", ""))
    else:
        reason = str(error)
    return {
        "status": "fail",
        "rows": 0,
        "columns": [],
        "error": reason or f"HTTP {response.status_code}",
        "materialized_query": materialized_query,
    }


def panel_bounds(panel):
    grid = panel.get("gridData", {}) or {}
    return {
        "x": int(grid.get("x", 0) or 0),
        "y": int(grid.get("y", 0) or 0),
        "w": int(grid.get("w", 0) or 0),
        "h": int(grid.get("h", 0) or 0),
    }


def overlaps(left, right):
    return (
        left["x"] < right["x"] + right["w"]
        and left["x"] + left["w"] > right["x"]
        and left["y"] < right["y"] + right["h"]
        and left["y"] + left["h"] > right["y"]
    )


def analyze_layout(panels):
    issues = {
        "overlaps": [],
        "invalid_sizes": [],
        "out_of_bounds": [],
        "max_x": 0,
        "max_y": 0,
    }
    bounds_by_section = {}
    for panel in panels:
        title = _panel_title(panel)
        bound = panel_bounds(panel)
        section_id = str((panel.get("gridData", {}) or {}).get("sectionId") or "__root__")
        issues["max_x"] = max(issues["max_x"], bound["x"] + bound["w"])
        issues["max_y"] = max(issues["max_y"], bound["y"] + bound["h"])
        bounds_by_section.setdefault(section_id, []).append((title, bound))
        if bound["w"] <= 0 or bound["h"] <= 0:
            issues["invalid_sizes"].append({"panel": title, **bound})
    for bounds in bounds_by_section.values():
        for title, bound in bounds:
            if bound["x"] < 0 or bound["y"] < 0 or bound["x"] + bound["w"] > GRID_WIDTH:
                issues["out_of_bounds"].append({"panel": title, **bound})
        for idx, (left_title, left_bound) in enumerate(bounds):
            for right_title, right_bound in bounds[idx + 1 :]:
                if overlaps(left_bound, right_bound):
                    issues["overlaps"].append(
                        {
                            "left_panel": left_title,
                            "right_panel": right_title,
                            "left": left_bound,
                            "right": right_bound,
                        }
                    )
    return issues


def should_include_dashboard(saved_object, dashboard_titles, dashboard_ids):
    title = saved_object.get("attributes", {}).get("title", "")
    dashboard_id = saved_object.get("id", "")
    if dashboard_titles and title not in set(dashboard_titles):
        return False
    if dashboard_ids and dashboard_id not in set(dashboard_ids):
        return False
    return True


def _chrome_command(chrome_binary, url, args, current_budget, extra_args=None):
    return [
        chrome_binary,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={args.window_width},{args.window_height}",
        f"--virtual-time-budget={current_budget}",
        *(extra_args or []),
        url,
    ]


def capture_dashboard_screenshot(saved_object, args):
    title = saved_object.get("attributes", {}).get("title", "") or saved_object.get("id", "dashboard")
    dashboard_id = saved_object.get("id", "")
    url = build_dashboard_url(
        args.kibana_url,
        args.space_id,
        dashboard_id,
        time_from=args.time_from,
        time_to=args.time_to,
    )
    chrome_binary = discover_chrome_binary(args.chrome_binary)
    if not chrome_binary:
        return {
            "status": "skipped",
            "path": "",
            "error": "Chrome/Chromium binary not found",
            "url": url,
        }

    screenshot_dir = Path(args.screenshot_dir or f"{Path(args.output).stem}_screenshots")
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    output_path = screenshot_dir / f"{_safe_stem(title)}.png"
    last_error = ""
    for attempt in range(max(0, args.screenshot_retries) + 1):
        current_budget = args.virtual_time_budget_ms * (attempt + 1)
        command = _chrome_command(
            chrome_binary,
            url,
            args,
            current_budget,
            extra_args=[f"--screenshot={output_path}"],
        )
        timeout_seconds = max(args.timeout, current_budget // 1000 + 30)
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            last_error = f"Screenshot timed out after {timeout_seconds}s"
            continue

        if proc.returncode == 0 and output_path.exists():
            return {
                "status": "captured",
                "path": str(output_path),
                "error": "",
                "url": url,
            }

        last_error = proc.stderr.strip() or proc.stdout.strip() or f"Chrome exited with code {proc.returncode}"

    return {
        "status": "failed",
        "path": str(output_path),
        "error": last_error[:800],
        "url": url,
    }


def capture_segmented_screenshots(saved_object, args):
    title = saved_object.get("attributes", {}).get("title", "") or saved_object.get("id", "dashboard")
    dashboard_id = saved_object.get("id", "")
    url = build_dashboard_url(
        args.kibana_url,
        args.space_id,
        dashboard_id,
        time_from=args.time_from,
        time_to=args.time_to,
    )
    chrome_binary = discover_chrome_binary(args.chrome_binary)
    if not chrome_binary:
        return {
            "status": "skipped",
            "path": "",
            "error": "Chrome/Chromium binary not found",
            "url": url,
            "segments": [],
        }

    screenshot_dir = Path(args.screenshot_dir or f"{Path(args.output).stem}_screenshots")
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    segment_count = max(1, int(getattr(args, "segment_count", 1) or 1))
    overlap = max(0, int(getattr(args, "segment_overlap", 0) or 0))
    step = max(1, int(args.window_height) - overlap)
    segments = []
    errors = []
    for idx in range(segment_count):
        output_path = screenshot_dir / f"{_safe_stem(title)}_{idx + 1:02d}.png"
        scroll_y = idx * step
        script = (
            "window.scrollTo(0, "
            f"{scroll_y}"
            "); document.body.style.overflow='hidden';"
        )
        current_budget = int(args.virtual_time_budget_ms)
        command = _chrome_command(
            chrome_binary,
            url,
            args,
            current_budget,
            extra_args=[
                "--run-all-compositor-stages-before-draw",
                f"--screenshot={output_path}",
                f"--run-script={script}",
            ],
        )
        timeout_seconds = max(args.timeout, current_budget // 1000 + 30)
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            error = f"Segment {idx + 1} timed out after {timeout_seconds}s"
            errors.append(error)
            segments.append({"status": "failed", "path": str(output_path), "scroll_y": scroll_y, "error": error})
            continue
        if proc.returncode == 0 and output_path.exists():
            segments.append({"status": "captured", "path": str(output_path), "scroll_y": scroll_y, "error": ""})
        else:
            error = proc.stderr.strip() or proc.stdout.strip() or f"Chrome exited with code {proc.returncode}"
            errors.append(error)
            segments.append({"status": "failed", "path": str(output_path), "scroll_y": scroll_y, "error": error[:800]})

    captured = sum(1 for segment in segments if segment["status"] == "captured")
    status = "captured" if captured == len(segments) else "partial" if captured else "failed"
    return {
        "status": status,
        "path": str(screenshot_dir),
        "error": "; ".join(errors)[:800],
        "url": url,
        "segments": segments,
    }


def _browser_audit_issues(html):
    issues = []
    for pattern in BROWSER_ERROR_PATTERNS:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if not match:
            continue
        snippet = re.sub(r"\s+", " ", html[max(0, match.start() - 120) : match.end() + 120]).strip()
        if snippet:
            issues.append(snippet[:240])
    return issues


def capture_browser_audit(saved_object, args):
    title = saved_object.get("attributes", {}).get("title", "") or saved_object.get("id", "dashboard")
    dashboard_id = saved_object.get("id", "")
    url = build_dashboard_url(
        args.kibana_url,
        args.space_id,
        dashboard_id,
        time_from=args.time_from,
        time_to=args.time_to,
    )
    chrome_binary = discover_chrome_binary(args.chrome_binary)
    if not chrome_binary:
        return {
            "status": "skipped",
            "path": "",
            "error": "Chrome/Chromium binary not found",
            "issues": [],
            "url": url,
        }

    audit_dir = Path(args.browser_audit_dir or f"{Path(args.output).stem}_browser_audit")
    audit_dir.mkdir(parents=True, exist_ok=True)
    output_path = audit_dir / f"{_safe_stem(title)}.html"
    last_error = ""
    for attempt in range(max(0, args.screenshot_retries) + 1):
        current_budget = args.virtual_time_budget_ms * (attempt + 1)
        command = _chrome_command(
            chrome_binary,
            url,
            args,
            current_budget,
            extra_args=["--dump-dom"],
        )
        timeout_seconds = max(args.timeout, current_budget // 1000 + 30)
        try:
            with output_path.open("w", encoding="utf-8") as handle:
                proc = subprocess.run(
                    command,
                    stdout=handle,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_seconds,
                )
        except subprocess.TimeoutExpired:
            last_error = f"Browser audit timed out after {timeout_seconds}s"
            continue

        if proc.returncode == 0:
            html = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
            issues = _browser_audit_issues(html)
            return {
                "status": "error" if issues else "clean",
                "path": str(output_path),
                "error": "",
                "issues": issues,
                "url": url,
            }

        last_error = proc.stderr.strip() or f"Chrome exited with code {proc.returncode}"

    return {
        "status": "failed",
        "path": str(output_path),
        "error": last_error[:800],
        "issues": [],
        "url": url,
    }


def _panel_title(panel):
    return (
        panel.get("embeddableConfig", {}).get("attributes", {}).get("title")
        or panel.get("title")
        or panel.get("panelIndex", "")
    )


def _path_value(payload, *path):
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


def _collect_esql_queries(node, queries):
    if isinstance(node, dict):
        nested_query = node.get("query")
        if isinstance(nested_query, dict):
            esql = nested_query.get("esql")
            if isinstance(esql, str) and esql.strip():
                queries.append(esql.strip())
        esql = node.get("esql")
        if isinstance(esql, str) and esql.strip():
            queries.append(esql.strip())
        for value in node.values():
            _collect_esql_queries(value, queries)
    elif isinstance(node, list):
        for item in node:
            _collect_esql_queries(item, queries)


_LENS_AGG_TO_ESQL = {
    "average": "AVG",
    "avg": "AVG",
    "sum": "SUM",
    "min": "MIN",
    "max": "MAX",
    "median": "MEDIAN",
    "unique_count": "COUNT_DISTINCT",
}


def _lens_layer_index(attrs: dict, layer_id: str) -> str:
    expected_ref = f"indexpattern-datasource-layer-{layer_id}"
    for ref in attrs.get("references", []) or []:
        if not isinstance(ref, dict):
            continue
        if ref.get("name") == expected_ref and ref.get("id"):
            return str(ref["id"])
    return "metrics-*"


def _lens_metric_expr(column: dict) -> str:
    operation = str(column.get("operationType") or "").lower()
    source_field = str(column.get("sourceField") or "").strip()
    if operation == "count":
        return "COUNT(*)"
    if operation == "percentile" and source_field:
        percentile = (column.get("params") or {}).get("percentile", 95)
        return f"PERCENTILE({source_field}, {int(percentile)})"
    function_name = _LENS_AGG_TO_ESQL.get(operation)
    if function_name and source_field:
        return f"{function_name}({source_field})"
    return ""


def _collect_lens_form_based_queries(attrs: dict) -> list[str]:
    layers = (
        attrs.get("state", {})
        .get("datasourceStates", {})
        .get("formBased", {})
        .get("layers", {})
    )
    if not isinstance(layers, dict):
        return []

    queries: list[str] = []
    for layer_id, layer in layers.items():
        if not isinstance(layer, dict):
            continue
        columns = layer.get("columns", {})
        if not isinstance(columns, dict):
            continue
        column_order = [col for col in layer.get("columnOrder", []) if col in columns]
        ordered_columns = [columns[col] for col in column_order] or list(columns.values())
        date_field = "@timestamp"
        breakdown_fields: list[str] = []
        metric_exprs: list[str] = []
        for column in ordered_columns:
            if not isinstance(column, dict):
                continue
            operation = str(column.get("operationType") or "").lower()
            source_field = str(column.get("sourceField") or "").strip()
            if operation == "date_histogram":
                date_field = source_field or "@timestamp"
            elif column.get("isBucketed") and source_field:
                breakdown_fields.append(source_field)
            elif not column.get("isBucketed"):
                metric_expr = _lens_metric_expr(column)
                if metric_expr:
                    metric_exprs.append(metric_expr)
        if not metric_exprs:
            continue

        stats_parts = [
            f"value = {metric_exprs[0]}",
            *[
                f"value_{idx} = {metric_expr}"
                for idx, metric_expr in enumerate(metric_exprs[1:], start=2)
            ],
        ]
        by_fields = [f"time_bucket = BUCKET({date_field}, 50, ?_tstart, ?_tend)", *breakdown_fields]
        query_lines = [
            f"FROM {_lens_layer_index(attrs, str(layer_id))}",
            f"| WHERE {date_field} >= ?_tstart AND {date_field} <= ?_tend",
            f"| STATS {', '.join(stats_parts)} BY {', '.join(by_fields)}",
            "| SORT time_bucket",
        ]
        queries.append("\n".join(query_lines))
    return queries


def extract_panel_queries(panel):
    attrs = panel.get("embeddableConfig", {}).get("attributes", {}) or {}
    state = attrs.get("state", {}) or {}
    candidates = [
        _path_value(attrs, "state", "query", "esql"),
        _path_value(attrs, "query", "esql"),
        _path_value(state, "query", "esql"),
        _path_value(panel, "query", "esql"),
    ]
    _collect_esql_queries(attrs, candidates)
    _collect_esql_queries(panel.get("embeddableConfig", {}), candidates)
    candidates.extend(_collect_lens_form_based_queries(attrs))

    unique = []
    seen = set()
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _panel_runtime_expectation(panel, queries):
    if queries:
        return "query"
    panel_type = str(panel.get("type", "") or "").lower()
    attrs = panel.get("embeddableConfig", {}).get("attributes", {}) or {}
    visualization_type = str(attrs.get("visualizationType", "") or "").lower()
    saved_vis = panel.get("embeddableConfig", {}).get("savedVis", {}) or {}
    saved_vis_type = str(saved_vis.get("type", "") or "").lower()
    if panel_type in NO_QUERY_EXPECTED_PANEL_TYPES:
        return "no_query_expected"
    if panel_type == "visualization" and saved_vis_type == "markdown":
        return "no_query_expected"
    if panel_type in QUERY_EXPECTED_PANEL_TYPES or visualization_type.startswith("lns"):
        return "query_expected"
    if attrs.get("state"):
        return "query_expected"
    return "unknown"


def _runtime_gap_reason(panel: dict) -> str:
    panel_type = str(panel.get("type", "") or "").lower()
    attrs = panel.get("embeddableConfig", {}).get("attributes", {}) or {}
    visualization_type = str(attrs.get("visualizationType", "") or "").lower()
    if panel_type == "lens" or visualization_type.startswith("lns"):
        return "lens_by_design"
    return "unexpected_gap"


def _validate_panel_queries(
    es_url, queries, timeout, es_api_key="", validation_workers=1, verify: bool | str = True,
    dsl_filter=None,
):
    if not queries:
        return []

    def _run(query):
        session = requests.Session()
        apply_tls(session, verify)
        return validate_esql(
            es_url, query, timeout, es_api_key=es_api_key, session=session, dsl_filter=dsl_filter
        )

    workers = max(1, int(validation_workers or 1))
    if workers == 1 or len(queries) == 1:
        session = requests.Session()
        apply_tls(session, verify)
        return [
            validate_esql(
                es_url, query, timeout, es_api_key=es_api_key, session=session, dsl_filter=dsl_filter
            )
            for query in queries
        ]
    with ThreadPoolExecutor(max_workers=min(workers, len(queries))) as pool:
        return list(pool.map(_run, queries))


def inspect_dashboard(
    saved_object,
    es_url,
    timeout,
    screenshot=None,
    browser_audit=None,
    es_api_key="",
    validation_workers=1,
    verify: bool | str = True,
):
    attributes = saved_object.get("attributes", {})
    raw_panels = attributes.get("panelsJSON", "[]")
    try:
        panels = json.loads(raw_panels)
    except json.JSONDecodeError as exc:
        return {
            "id": saved_object.get("id", ""),
            "title": attributes.get("title", ""),
            "total_panels": 0,
            "esql_panels": 0,
            "runtime_checked_panels": 0,
            "failing_panels": [
                {
                    "panel": attributes.get("title", "") or saved_object.get("id", ""),
                    "type": "dashboard",
                    "status": "fail",
                    "rows": 0,
                    "error": f"Invalid panelsJSON: {exc}",
                }
            ],
            "empty_panels": [],
            "not_runtime_checked_panels": [],
            "lens_by_design_panels": [],
            "unexpected_runtime_gap_panels": [],
            "non_query_panels": [],
            "layout": {"overlaps": [], "invalid_sizes": [], "out_of_bounds": [], "max_x": 0, "max_y": 0},
            "screenshot": screenshot or {"status": "not_requested", "path": "", "error": "", "url": ""},
            "browser_audit": browser_audit
            or {"status": "not_requested", "path": "", "error": "", "issues": [], "url": ""},
            "status": "has_runtime_errors",
            "panels": [],
        }
    dashboard_filter = extract_dashboard_filter_dsl(attributes)
    panel_results = []
    failing_panels = []
    empty_panels = []
    not_runtime_checked_panels = []
    lens_by_design_panels = []
    unexpected_runtime_gap_panels = []
    non_query_panels = []

    for panel in panels:
        title = _panel_title(panel)
        panel_type = str(panel.get("type", "") or "")
        queries = extract_panel_queries(panel)
        expectation = _panel_runtime_expectation(panel, queries)
        if not queries:
            result = {
                "panel": title,
                "type": panel_type,
                "status": "no_query_expected" if expectation == "no_query_expected" else "not_runtime_checked",
            }
            if result["status"] == "not_runtime_checked":
                result["coverage_reason"] = _runtime_gap_reason(panel)
            panel_results.append(result)
            if result["status"] == "no_query_expected":
                non_query_panels.append(result)
            else:
                not_runtime_checked_panels.append(result)
                if result.get("coverage_reason") == "lens_by_design":
                    lens_by_design_panels.append(result)
                else:
                    unexpected_runtime_gap_panels.append(result)
            continue

        validations = _validate_panel_queries(
            es_url,
            queries,
            timeout,
            es_api_key=es_api_key,
            validation_workers=validation_workers,
            verify=verify,
            dsl_filter=dashboard_filter,
        )
        failing = [item for item in validations if item["status"] == "fail"]
        rows = max((item["rows"] for item in validations), default=0)
        result_status = "fail" if failing else "empty" if rows == 0 else "pass"
        result = {
            "panel": title,
            "type": panel_type,
            "status": result_status,
            "rows": rows,
            "error": "; ".join(dict.fromkeys(item["error"] for item in failing if item["error"])),
            "validation_mode": "esql",
            "checked_queries": len(queries),
        }
        if len(queries) == 1:
            result["query"] = queries[0]
            result["materialized_query"] = validations[0]["materialized_query"]
        else:
            result["queries"] = queries
            result["materialized_queries"] = [item["materialized_query"] for item in validations]
        if any(item["columns"] for item in validations):
            result["columns"] = next((item["columns"] for item in validations if item["columns"]), [])
        panel_results.append(result)
        if result_status == "fail":
            failing_panels.append(result)
        elif result_status == "empty":
            empty_panels.append(result)

    layout = analyze_layout(panels)
    browser_info = browser_audit or {"status": "not_requested", "path": "", "error": "", "issues": [], "url": ""}
    return {
        "id": saved_object.get("id", ""),
        "title": attributes.get("title", ""),
        "total_panels": len(panels),
        "esql_panels": sum(
            1 for result in panel_results if result["status"] not in {"no_query_expected", "not_runtime_checked"}
        ),
        "runtime_checked_panels": sum(
            1 for result in panel_results if result["status"] in {"pass", "fail", "empty"}
        ),
        "failing_panels": failing_panels,
        "empty_panels": empty_panels,
        "not_runtime_checked_panels": not_runtime_checked_panels,
        "lens_by_design_panels": lens_by_design_panels,
        "unexpected_runtime_gap_panels": unexpected_runtime_gap_panels,
        "non_query_panels": non_query_panels,
        "layout": layout,
        "screenshot": screenshot or {"status": "not_requested", "path": "", "error": "", "url": ""},
        "browser_audit": browser_info,
        "status": (
            "has_runtime_errors"
            if failing_panels or browser_info.get("status") == "error"
            else "has_layout_issues"
            if layout["overlaps"] or layout["invalid_sizes"] or layout["out_of_bounds"]
            else "has_runtime_gaps"
            if not_runtime_checked_panels
            else "has_empty_panels"
            if empty_panels
            else "clean"
        ),
        "panels": panel_results,
    }


def build_summary(dashboards):
    return {
        "dashboards": len(dashboards),
        "total_panels": sum(item["total_panels"] for item in dashboards),
        "total_esql_panels": sum(item["esql_panels"] for item in dashboards),
        "runtime_checked_panels": sum(item["runtime_checked_panels"] for item in dashboards),
        "non_query_panels": sum(len(item["non_query_panels"]) for item in dashboards),
        "not_runtime_checked_panels": sum(len(item["not_runtime_checked_panels"]) for item in dashboards),
        "lens_by_design_panels": sum(len(item.get("lens_by_design_panels", [])) for item in dashboards),
        "unexpected_runtime_gap_panels": sum(len(item.get("unexpected_runtime_gap_panels", [])) for item in dashboards),
        "dashboards_with_runtime_errors": sum(1 for item in dashboards if item["failing_panels"]),
        "dashboards_with_layout_issues": sum(
            1
            for item in dashboards
            if item["layout"]["overlaps"] or item["layout"]["invalid_sizes"] or item["layout"]["out_of_bounds"]
        ),
        "dashboards_with_runtime_gaps": sum(1 for item in dashboards if item["not_runtime_checked_panels"]),
        "dashboards_with_browser_errors": sum(
            1 for item in dashboards if item.get("browser_audit", {}).get("status") == "error"
        ),
        "runtime_error_panels": sum(len(item["failing_panels"]) for item in dashboards),
        "empty_panels": sum(len(item["empty_panels"]) for item in dashboards),
        "screenshots_captured": sum(
            1 for item in dashboards if item.get("screenshot", {}).get("status") in {"captured", "partial"}
        ),
        "screenshots_failed": sum(1 for item in dashboards if item.get("screenshot", {}).get("status") == "failed"),
        "screenshots_skipped": sum(1 for item in dashboards if item.get("screenshot", {}).get("status") == "skipped"),
        "browser_audits_clean": sum(1 for item in dashboards if item.get("browser_audit", {}).get("status") == "clean"),
        "browser_audits_failed": sum(1 for item in dashboards if item.get("browser_audit", {}).get("status") == "failed"),
        "browser_audits_skipped": sum(1 for item in dashboards if item.get("browser_audit", {}).get("status") == "skipped"),
    }


def main(verify: bool | str = True):
    args = parse_args()
    session = requests.Session()
    apply_tls(session, verify)
    session.headers.update({"kbn-xsrf": "true"})
    if args.kibana_api_key:
        session.headers.update({"Authorization": f"ApiKey {args.kibana_api_key}"})

    dashboards = []
    if args.dashboard_id:
        dashboard_items = [
            load_dashboard(session, args.kibana_url, args.space_id, dashboard_id, args.timeout)
            for dashboard_id in args.dashboard_id
        ]
    else:
        dashboard_items = load_dashboards(
            session,
            args.kibana_url,
            args.space_id,
            args.timeout,
            per_page=args.saved_objects_per_page,
        )

    for item in dashboard_items:
        if not should_include_dashboard(item, args.dashboard_title, args.dashboard_id):
            continue
        attributes = item.get("attributes", {}) or {}
        saved_object = item if attributes.get("panelsJSON") else load_dashboard(
            session,
            args.kibana_url,
            args.space_id,
            item["id"],
            args.timeout,
        )
        if args.segmented_screenshots:
            screenshot = capture_segmented_screenshots(saved_object, args)
        elif args.capture_screenshots:
            screenshot = capture_dashboard_screenshot(saved_object, args)
        else:
            screenshot = None
        browser_audit = capture_browser_audit(saved_object, args) if args.browser_audit else None
        dashboards.append(
            inspect_dashboard(
                saved_object,
                args.es_url,
                args.timeout,
                screenshot=screenshot,
                browser_audit=browser_audit,
                es_api_key=args.es_api_key,
                validation_workers=getattr(args, "validation_workers", 1),
                verify=verify,
            )
        )

    if not dashboards:
        raise SystemExit("No dashboards matched the requested filters.")

    payload = {
        "summary": build_summary(dashboards),
        "dashboards": dashboards,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2))

    summary = payload["summary"]
    print(
        f"Dashboards: {summary['dashboards']} | "
        f"Runtime error panels: {summary['runtime_error_panels']} | "
        f"Layout issue dashboards: {summary['dashboards_with_layout_issues']} | "
        f"Empty panels: {summary['empty_panels']} | "
        f"Not runtime-checked panels: {summary['not_runtime_checked_panels']}"
    )
    if args.capture_screenshots:
        print(
            f"Screenshots: {summary['screenshots_captured']} captured, "
            f"{summary['screenshots_failed']} failed, {summary['screenshots_skipped']} skipped"
        )
    if args.browser_audit:
        print(
            f"Browser audit: {summary['browser_audits_clean']} clean, "
            f"{summary['dashboards_with_browser_errors']} with visible errors, "
            f"{summary['browser_audits_failed']} failed, {summary['browser_audits_skipped']} skipped"
        )
    for dashboard in dashboards:
        screenshot_status = dashboard.get("screenshot", {}).get("status", "not_requested")
        browser_status = dashboard.get("browser_audit", {}).get("status", "not_requested")
        print(
            f"{dashboard['title']}: "
            f"errors={len(dashboard['failing_panels'])}, "
            f"empty={len(dashboard['empty_panels'])}, "
            f"not_checked={len(dashboard['not_runtime_checked_panels'])}, "
            f"overlaps={len(dashboard['layout']['overlaps'])}, "
            f"screenshot={screenshot_status}, "
            f"browser={browser_status}"
        )
    print(f"Report: {output_path}")

    failure_reasons = []
    if args.fail_on_runtime_errors and summary["runtime_error_panels"]:
        failure_reasons.append(f"{summary['runtime_error_panels']} panel runtime error(s)")
    if args.fail_on_layout_issues and summary["dashboards_with_layout_issues"]:
        failure_reasons.append(f"{summary['dashboards_with_layout_issues']} dashboard(s) with layout issues")
    if args.fail_on_empty_panels and summary["empty_panels"]:
        failure_reasons.append(f"{summary['empty_panels']} empty panel(s)")
    if args.fail_on_not_runtime_checked and summary["not_runtime_checked_panels"]:
        failure_reasons.append(f"{summary['not_runtime_checked_panels']} panel(s) not runtime-checked")
    if args.fail_on_browser_errors and summary["dashboards_with_browser_errors"]:
        failure_reasons.append(f"{summary['dashboards_with_browser_errors']} dashboard(s) with browser-visible errors")
    if failure_reasons:
        raise SystemExit("Smoke validation failed: " + ", ".join(failure_reasons))


if __name__ == "__main__":
    main()
