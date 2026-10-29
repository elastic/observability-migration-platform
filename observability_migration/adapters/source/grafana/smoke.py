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

from .esql_validate import _validation_params_for_query, materialize_dashboard_time_query
from .schema import SchemaResolver

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
NO_QUERY_EXPECTED_PANEL_TYPES = {"control_group", "markdown", "links"}
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
BROWSER_EMPTY_STATE_PATTERNS = (
    r'data-test-subj=["\']emptyPlaceholder["\'][^>]*>.*?<p>\s*No results found\s*</p>',
    r'class=["\'][^"\']*echMetricText__value[^"\']*["\'][^>]*>\s*N/A\s*<',
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
        "--dashboards-from",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Scope validation to the dashboards recorded in a migration artifact "
            "(a migration detailed report or a prior smoke report). Uploaded "
            "saved-object IDs are used when present, otherwise dashboard titles. "
            "Repeatable."
        ),
    )
    parser.add_argument(
        "--include-deleted",
        action="store_true",
        help="Also validate '[DELETED]' placeholder dashboards (skipped by default when unscoped).",
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
        "--fail-on-binding-gaps",
        action="store_true",
        help=(
            "Exit non-zero when a panel's breakdown column is null in every row or "
            "an ES|QL control has no options (label bound to a field the data lacks)."
        ),
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


# Result column types that identify a series (breakdown, legend, control value)
# rather than carry a measurement.
_DIMENSION_COLUMN_TYPES = {"keyword", "text", "ip", "version", "boolean"}
_MEASURE_COLUMN_TYPES = {
    "double", "float", "half_float", "scaled_float", "long", "integer",
    "short", "byte", "unsigned_long", "counter_double", "counter_long",
    "counter_integer", "aggregate_metric_double",
}


def _null_dimension_columns(columns, values):
    """Dimension columns that are null in every row while a measure has data.

    That shape is a label bound to a field the data does not carry: Kibana draws
    the values as one ``(null)`` series instead of one series per label value.
    A result with no measure values is an empty panel, not this.
    """
    if not values:
        return []
    measure_idx = [i for i, col in enumerate(columns) if col.get("type") in _MEASURE_COLUMN_TYPES]
    if not any(i < len(row) and row[i] is not None for row in values for i in measure_idx):
        return []
    return [
        col.get("name", "")
        for i, col in enumerate(columns)
        if col.get("type") in _DIMENSION_COLUMN_TYPES
        and all(i >= len(row) or row[i] is None for row in values)
    ]


def _all_measures_null(columns, values):
    """Rows came back but every measure in them is null.

    Kibana draws "No results found" for this, yet the row count says the query
    worked. The usual cause is arithmetic on a zero or missing operand: ES|QL
    returns null for ``x / 0`` (e.g. Redis memory % when ``maxmemory`` is 0).
    """
    measure_idx = [i for i, col in enumerate(columns) if col.get("type") in _MEASURE_COLUMN_TYPES]
    if not values or not measure_idx:
        return False
    return all(i >= len(row) or row[i] is None for row in values for i in measure_idx)


def _column_max_abs(columns, values):
    """Largest absolute value per measure column (columns with no value omitted)."""
    out = {}
    for i, col in enumerate(columns):
        if col.get("type") not in _MEASURE_COLUMN_TYPES:
            continue
        numbers = [
            abs(row[i]) for row in values
            if i < len(row) and isinstance(row[i], int | float) and not isinstance(row[i], bool)
        ]
        if numbers:
            out[col.get("name", "")] = max(numbers)
    return out


# Seconds per Kibana duration-format ``fromUnit``.
_KIBANA_DURATION_UNIT_SECONDS = {
    "picoseconds": 1e-12, "nanoseconds": 1e-9, "microseconds": 1e-6, "milliseconds": 1e-3,
    "seconds": 1.0, "minutes": 60.0, "hours": 3600.0, "days": 86400.0, "weeks": 604800.0,
}


def _panel_duration_formats(panel):
    """``(field, fromUnit, decimals)`` for each duration-formatted Lens column.

    Read from the stored Lens state, i.e. what Kibana will actually render.
    Kibana 9.7 stores the Dashboards API ``duration`` (``to: auto``) as
    ``toUnit: humanizePrecise`` with ``decimals: 0``.
    """
    state = (((panel.get("embeddableConfig") or {}).get("attributes") or {}).get("state") or {})
    found = []
    for datasource in (state.get("datasourceStates") or {}).values():
        if not isinstance(datasource, dict):
            continue
        layers = datasource.get("layers")
        for layer in layers.values() if isinstance(layers, dict) else []:
            columns = layer.get("columns") if isinstance(layer, dict) else None
            # Text-based (ES|QL) layers list column objects; form-based layers
            # key them by column id.
            if isinstance(columns, dict):
                columns = list(columns.values())
            for column in columns if isinstance(columns, list) else []:
                if not isinstance(column, dict):
                    continue
                fmt = ((column.get("params") or {}).get("format") or {})
                if fmt.get("id") != "duration":
                    continue
                params = fmt.get("params") or {}
                found.append((column.get("fieldName", ""), params.get("fromUnit", "seconds"), params.get("decimals")))
    return found


def _unreadable_duration_axes(panel, column_max_abs):
    """Duration columns whose every value renders identically (#480).

    The duration formatter's smallest output unit is the millisecond, so with
    ``decimals`` d it cannot tell apart values under 10^-d ms: a 0.2 ms series
    formatted with ``decimals: 0`` reads "0 ms" on every axis tick.
    """
    findings = []
    for field_name, from_unit, decimals in _panel_duration_formats(panel):
        factor = _KIBANA_DURATION_UNIT_SECONDS.get(str(from_unit))
        peak = column_max_abs.get(field_name)
        if factor is None or not isinstance(decimals, int) or not peak:
            continue
        if peak * factor < 1e-3 * 10 ** (-decimals):
            findings.append({"column": field_name, "peak_seconds": peak * factor, "decimals": decimals})
    return findings


_NULL_VALUES_REASON = (
    "every value the query returned is null; Kibana shows \"No results found\". "
    "For a ratio panel, check whether the divisor metric is 0 or missing "
    "(ES|QL returns null for division by zero)."
)


def validate_esql(
    es_url,
    query,
    timeout,
    es_api_key="",
    session=None,
    dsl_filter=None,
    identifier_params=None,
    keep_values=False,
):
    materialized_query = materialize_dashboard_time_query(query)
    headers = {"Authorization": f"ApiKey {es_api_key}"} if es_api_key else None
    client = session or requests
    body = {"query": materialized_query}
    params = _validation_params_for_query(
        materialized_query,
        identifier_params=identifier_params,
    )
    if params:
        body["params"] = params
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
        columns = payload.get("columns", []) or []
        values = payload.get("values", []) or []
        result = {
            "status": "pass",
            "rows": len(values),
            "columns": [column.get("name", "") for column in columns],
            "null_columns": _null_dimension_columns(columns, values),
            "null_values": _all_measures_null(columns, values),
            "column_max_abs": _column_max_abs(columns, values),
            "error": "",
            "materialized_query": materialized_query,
        }
        if keep_values:
            result["values"] = values
        return result
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


def should_include_dashboard(saved_object, dashboard_titles, dashboard_ids, include_deleted=False):
    title = saved_object.get("attributes", {}).get("title", "")
    dashboard_id = saved_object.get("id", "")
    if dashboard_titles or dashboard_ids:
        # Scoped: include a dashboard matching any requested id OR any requested
        # title (a union). Combining id- and title-based scope must never drop a
        # dashboard that matches only one dimension.
        return dashboard_id in set(dashboard_ids) or title in set(dashboard_titles)
    if not include_deleted and title.startswith("[DELETED]"):
        return False
    return True


def _iter_dashboard_records(payload):
    """Yield dashboard-like dicts from a smoke report, migration report, or list."""
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = payload.get("dashboards")
    else:
        records = None
    for item in records or []:
        if isinstance(item, dict):
            yield item


def _kibana_saved_object_id(record):
    """Return a record's *Kibana* saved-object ID, never a source dashboard ID.

    The record shape differs by artifact, and a naive top-level ``id`` is a trap:

    - A migration detailed report (Grafana/Datadog) sets top-level ``id`` to the
      *source* dashboard ID and carries the uploaded Kibana ID under
      ``upload.saved_object_id``. Trusting ``id`` here would try to load a
      source ID from Kibana and match nothing.
    - A prior smoke report has no ``upload`` block; its own ``id`` *is* the
      Kibana saved-object ID.
    """
    upload = record.get("upload")
    if isinstance(upload, dict):
        return str(upload.get("saved_object_id") or "").strip()
    return str(record.get("kibana_saved_object_id") or record.get("id") or "").strip()


def load_scope_from_artifact(path):
    """Extract ``(ids, titles)`` to scope validation from a migration artifact.

    Accepts a migration detailed report or a prior smoke report (both expose a
    top-level ``dashboards`` list). Each record contributes its uploaded Kibana
    saved-object ID when present (a smoke report's ``id`` or a migration report's
    ``upload.saved_object_id``), otherwise its title (a migration report that
    never uploaded has titles but no Kibana ID). Preferring IDs lets the caller
    load only those dashboards by ID instead of scanning the whole space, which
    is what makes validation practical on a busy space (#198).
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = []
    titles = []
    for record in _iter_dashboard_records(payload):
        dashboard_id = _kibana_saved_object_id(record)
        title = str(record.get("title") or record.get("dashboard_title") or "").strip()
        if dashboard_id:
            if dashboard_id not in ids:
                ids.append(dashboard_id)
        elif title and title not in titles:
            titles.append(title)
    return ids, titles


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
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        snippet = re.sub(r"\s+", " ", html[max(0, match.start() - 120) : match.end() + 120]).strip()
        if snippet:
            issues.append(snippet[:240])
    for pattern in BROWSER_EMPTY_STATE_PATTERNS:
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
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
            if not issues:
                return {
                    "status": "clean",
                    "path": str(output_path),
                    "error": "",
                    "issues": [],
                    "url": url,
                }
            # Headless Chrome can dump a transient pre-settled DOM even when the
            # eventual dashboard screenshot is fine. Give visible DOM issues the
            # same larger-budget retry path as outright browser failures before
            # declaring a dashboard-level browser error.
            last_error = "; ".join(issues)[:800]
            if attempt < max(0, args.screenshot_retries):
                continue
            return {
                "status": "error",
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
    embeddable = panel.get("embeddableConfig", {}) or {}
    return (
        (embeddable.get("attributes", {}) or {}).get("title")
        # Dashboards API uploads keep the title on the embeddable config, not
        # on the Lens attributes; without it panels are reported by UUID and
        # smoke results never match migration panels by title.
        or embeddable.get("title")
        or panel.get("title")
        or panel.get("panelIndex", "")
    )


_CONTROL_WILDCARD_OPTIONS = {"", ".*", "*"}
_UNKNOWN_COLUMN_RE = re.compile(r"Unknown column \[`?([^\]`]+)`?\]")


def _as_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


def extract_dashboard_controls(attributes):
    """ES|QL controls from a dashboard saved object, either storage shape.

    Dashboards API uploads keep controls in ``pinned_panels``; older objects
    use ``controlGroupInput.panelsJSON``.
    """
    entries = []
    pinned = _as_json(attributes.get("pinned_panels"))
    if isinstance(pinned, dict):
        pinned = pinned.get("panels", pinned)
    entries.extend(pinned.values() if isinstance(pinned, dict) else pinned or [])
    legacy = _as_json((attributes.get("controlGroupInput") or {}).get("panelsJSON"))
    entries.extend(legacy.values() if isinstance(legacy, dict) else legacy or [])

    controls = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        config = entry.get("config") or entry.get("explicitInput") or {}
        query = str(config.get("esql_query") or config.get("esqlQuery") or "").strip()
        variable = str(config.get("variable_name") or config.get("variableName") or "").strip()
        if not query or not variable:
            continue
        controls.append(
            {
                "title": str(config.get("title") or variable),
                "variable": variable,
                "query": query,
            }
        )
    return controls


def _control_option_count(values):
    options = {
        str(row[0])
        for row in values or []
        if row and row[0] is not None and str(row[0]) not in _CONTROL_WILDCARD_OPTIONS
    }
    return len(options)


def _panels_binding_variable(variable, panel_queries):
    """Titles of panels whose queries bind ``?variable`` (not ``??variable``)."""
    pattern = re.compile(r"(?<!\?)\?" + re.escape(variable) + r"\b")
    bound, native = [], []
    for title, queries in panel_queries:
        hits = [query for query in queries if pattern.search(query)]
        if not hits:
            continue
        bound.append(title)
        if any(query.lstrip().upper().startswith("PROMQL") for query in hits):
            native.append(title)
    return bound, native


_QUERY_INDEX_RE = re.compile(r"^\s*(?:FROM|TS)\s+([^\s|]+)", re.IGNORECASE)


def _query_index(query):
    match = _QUERY_INDEX_RE.match(query or "")
    return match.group(1) if match else ""


def _alternative_label_fields(field_name):
    """Other spellings the same Prometheus label can have on a target."""
    labels = [
        label
        for label, candidates in SchemaResolver.PROM_TO_OTEL_CANDIDATES.items()
        if field_name in candidates
    ]
    for prefix in ("prometheus.labels.", "labels."):
        if field_name.startswith(prefix):
            labels.append(field_name[len(prefix):])
            break
    else:
        if "." not in field_name:
            labels.append(field_name)
    alternatives = []
    for label in labels:
        alternatives.extend((label, f"labels.{label}", f"prometheus.labels.{label}"))
        alternatives.extend(SchemaResolver.PROM_TO_OTEL_CANDIDATES.get(label, []))
    seen = {field_name}
    ordered = []
    for alternative in alternatives:
        if alternative not in seen:
            seen.add(alternative)
            ordered.append(alternative)
    return ordered


def _existing_fields(es_url, index, fields, timeout, es_api_key="", verify: bool | str = True):
    if not es_url or not index or not fields:
        return []
    headers = {"Authorization": f"ApiKey {es_api_key}"} if es_api_key else None
    try:
        response = requests.get(
            f"{es_url}/{index}/_field_caps",
            params={"fields": ",".join(fields), "ignore_unavailable": "true"},
            headers=headers,
            timeout=timeout,
            verify=verify,
        )
        if response.status_code != 200:
            return []
        present = response.json().get("fields", {}) or {}
    except (requests.RequestException, ValueError):
        return []
    return [name for name in fields if name in present]


def _substitute_field(query, old, new):
    replacement = f"`{new}`" if "." in new else new
    pattern = re.compile(r"(?<![\w.?])`?" + re.escape(old) + r"`?(?![\w.])")
    return pattern.sub(lambda _match: replacement, query)


# Bounds the extra queries a hint may run per finding.
_MAX_HINT_PROBES = 4


def binding_gap_hint(
    field_name,
    query,
    es_url,
    timeout,
    *,
    kind,
    es_api_key="",
    verify: bool | str = True,
    dsl_filter=None,
    identifier_params=None,
):
    """One-line remediation for a label field that has no values in *query*.

    Field existence is not enough evidence: on a shared ``metrics-*`` pattern
    another stream can carry the alternative spelling. So each candidate that
    exists is substituted into the same query, and only spellings that then
    return values (control options, or a non-null breakdown) are suggested.
    """
    index = _query_index(query)
    present = _existing_fields(
        es_url, index, _alternative_label_fields(field_name), timeout, es_api_key=es_api_key, verify=verify,
    )
    # Prometheus spellings first: they are what a Prometheus-shaped target has.
    present.sort(key=lambda name: 0 if "." not in name or name.startswith(("labels.", "prometheus.labels.")) else 1)
    working = []
    for alternative in present[:_MAX_HINT_PROBES]:
        outcome = validate_esql(
            es_url,
            _substitute_field(query, field_name, alternative),
            timeout,
            es_api_key=es_api_key,
            dsl_filter=dsl_filter,
            identifier_params=identifier_params,
            keep_values=True,
        )
        if outcome["status"] != "pass":
            continue
        if kind == "control":
            has_values = _control_option_count(outcome.get("values")) > 0
        else:
            has_values = bool(outcome["rows"]) and alternative not in outcome.get("null_columns", [])
        if has_values:
            working.append(alternative)
    if not working:
        checked = f" (checked {', '.join(f'`{name}`' for name in present)})" if present else ""
        return (
            f"`{field_name}` has no values for these documents and no other spelling of the "
            f"label does either{checked}: the label is likely not ingested for this metric "
            "(a data gap, not a field-mapping error)."
        )
    hint = (
        f"`{field_name}` has no values, but the same query returns values with "
        f"{', '.join(f'`{name}`' for name in working)}. Re-migrate with --es-url (and "
        "--esql-index) so labels resolve against the target"
    )
    if any(name.startswith("labels.") for name in working):
        hint += ", or with --field-profile prometheus_native"
    elif any(name.startswith("prometheus.labels.") for name in working):
        hint += ", or with --field-profile prometheus_remote_write / prometheus_metrics"
    return hint + "."


def inspect_controls(
    controls, panel_queries, es_url, timeout, es_api_key="", verify: bool | str = True,
):
    """Run each control's options query and flag controls with no options.

    A control with no options cannot be used to filter, and Kibana sends its
    unset ``?variable`` as null: ES|QL panels that guard for that still render,
    but a native PROMQL matcher fails with ``Parameter [?var] cannot be null``.
    """
    session = requests.Session()
    apply_tls(session, verify)
    results = []
    for control in controls:
        outcome = validate_esql(
            es_url, control["query"], timeout, es_api_key=es_api_key, session=session, keep_values=True,
        )
        bound, native = _panels_binding_variable(control["variable"], panel_queries)
        record = {
            "title": control["title"],
            "variable": control["variable"],
            "query": control["query"],
            "dependent_panels": bound,
            "native_promql_dependents": native,
        }
        if outcome["status"] == "fail":
            record.update(status="fail", options=0, error=outcome["error"])
            # A control whose options query errors has no options either. When
            # the error names the missing field, probe its other spellings.
            missing = _UNKNOWN_COLUMN_RE.search(outcome["error"] or "")
            if missing:
                record["field"] = missing.group(1)
                record["hint"] = binding_gap_hint(
                    missing.group(1), control["query"], es_url, timeout,
                    kind="control", es_api_key=es_api_key, verify=verify,
                )
        else:
            options = _control_option_count(outcome.get("values"))
            record.update(status="ok" if options else "no_options", options=options, error="")
            if not options:
                field_name = (outcome.get("columns") or [""])[0]
                record["field"] = field_name
                record["hint"] = binding_gap_hint(
                    field_name, control["query"], es_url, timeout,
                    kind="control", es_api_key=es_api_key, verify=verify,
                )
        results.append(record)
    return results


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
    dsl_filter=None, identifier_params=None,
):
    if not queries:
        return []

    def _run(query):
        session = requests.Session()
        apply_tls(session, verify)
        return validate_esql(
            es_url,
            query,
            timeout,
            es_api_key=es_api_key,
            session=session,
            dsl_filter=dsl_filter,
            identifier_params=identifier_params,
        )

    workers = max(1, int(validation_workers or 1))
    if workers == 1 or len(queries) == 1:
        session = requests.Session()
        apply_tls(session, verify)
        return [
            validate_esql(
                es_url,
                query,
                timeout,
                es_api_key=es_api_key,
                session=session,
                dsl_filter=dsl_filter,
                identifier_params=identifier_params,
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
    identifier_params=None,
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
            "null_series_panels": [],
            "unreadable_axis_panels": [],
            "controls": [],
            "empty_controls": [],
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
    null_series_panels = []
    unreadable_axis_panels = []
    not_runtime_checked_panels = []
    lens_by_design_panels = []
    unexpected_runtime_gap_panels = []
    non_query_panels = []
    panel_queries = []

    for panel in panels:
        title = _panel_title(panel)
        panel_type = str(panel.get("type", "") or "")
        queries = extract_panel_queries(panel)
        if queries:
            panel_queries.append((title, queries))
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
            identifier_params=identifier_params,
        )
        failing = [item for item in validations if item["status"] == "fail"]
        rows = max((item["rows"] for item in validations), default=0)
        null_columns = list(
            dict.fromkeys(name for item in validations for name in item.get("null_columns", []) or [])
        )
        null_values = bool(validations) and all(item.get("null_values") for item in validations)
        result_status = (
            "fail" if failing
            else "empty" if rows == 0 or null_values
            else "null_series" if null_columns
            else "pass"
        )
        result = {
            "panel": title,
            "type": panel_type,
            "status": result_status,
            "rows": rows,
            "error": "; ".join(dict.fromkeys(item["error"] for item in failing if item["error"])),
            "validation_mode": "esql",
            "checked_queries": len(queries),
        }
        if result_status == "empty" and rows:
            result["empty_reason"] = _NULL_VALUES_REASON
        peaks = {}
        for item in validations:
            for name, peak in (item.get("column_max_abs") or {}).items():
                peaks[name] = max(peak, peaks.get(name, 0))
        unreadable = _unreadable_duration_axes(panel, peaks) if result_status == "pass" else []
        if unreadable:
            result["unreadable_axes"] = unreadable
            unreadable_axis_panels.append(result)
        if result_status == "null_series":
            result["null_columns"] = null_columns
            hint_query = next(
                (item["materialized_query"] for item in validations if item.get("null_columns")),
                queries[0],
            )
            result["hints"] = [
                binding_gap_hint(
                    name, hint_query, es_url, timeout, kind="panel", es_api_key=es_api_key,
                    verify=verify, dsl_filter=dashboard_filter, identifier_params=identifier_params,
                )
                for name in null_columns
            ]
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
        elif result_status == "null_series":
            null_series_panels.append(result)

    controls = inspect_controls(
        extract_dashboard_controls(attributes),
        panel_queries,
        es_url,
        timeout,
        es_api_key=es_api_key,
        verify=verify,
    )
    empty_controls = [control for control in controls if control["status"] in {"no_options", "fail"}]
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
            1 for result in panel_results if result["status"] in {"pass", "fail", "empty", "null_series"}
        ),
        "failing_panels": failing_panels,
        "empty_panels": empty_panels,
        "null_series_panels": null_series_panels,
        "unreadable_axis_panels": unreadable_axis_panels,
        "controls": controls,
        "empty_controls": empty_controls,
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
            else "has_binding_gaps"
            if null_series_panels or empty_controls
            else "has_unreadable_axes"
            if unreadable_axis_panels
            else "has_empty_panels"
            if empty_panels
            else "clean"
        ),
        "panels": panel_results,
    }


def print_panel_findings(dashboard):
    """Print smoke findings that need an explanation: label-binding gaps (with
    their remediation hint) and empty panels whose rows were all null."""
    for panel in dashboard.get("empty_panels", []) or []:
        if panel.get("empty_reason"):
            print(f"  empty: {panel['panel']} — {panel['empty_reason']}")
    for panel in dashboard.get("unreadable_axis_panels", []) or []:
        for axis in panel.get("unreadable_axes", []) or []:
            print(
                f"  unreadable axis: {panel['panel']} — `{axis['column']}` peaks at "
                f"{axis['peak_seconds']:.3g} s, below what its duration format shows with "
                f"{axis['decimals']} decimal(s), so every tick reads the same (e.g. \"0 ms\"). "
                "Re-upload with an ES URL so the format is fitted to the data."
            )
    for panel in dashboard.get("null_series_panels", []) or []:
        columns = ", ".join(f"`{name}`" for name in panel.get("null_columns", []))
        print(f"  null series: {panel['panel']} — {columns} is null in all {panel['rows']} row(s)")
        for hint in panel.get("hints", []) or []:
            print(f"    hint: {hint}")
    for control in dashboard.get("empty_controls", []) or []:
        failed = f" — options query failed: {control['error'].splitlines()[-1]}" if control.get("status") == "fail" else ""
        print(f"  control without options: {control['title']} (?{control['variable']}){failed}")
        if control.get("hint"):
            print(f"    hint: {control['hint']}")
        if control.get("dependent_panels"):
            print(f"    filters {len(control['dependent_panels'])} panel(s): {', '.join(control['dependent_panels'])}")
        if control.get("native_promql_dependents"):
            print(
                "    native PROMQL panels fail in Kibana while it has no selection "
                f"(Parameter [?{control['variable']}] cannot be null): "
                + ", ".join(control["native_promql_dependents"])
            )


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
        "null_series_panels": sum(len(item.get("null_series_panels", [])) for item in dashboards),
        "unreadable_axis_panels": sum(len(item.get("unreadable_axis_panels", [])) for item in dashboards),
        "empty_controls": sum(len(item.get("empty_controls", [])) for item in dashboards),
        "dashboards_with_binding_gaps": sum(
            1 for item in dashboards if item.get("null_series_panels") or item.get("empty_controls")
        ),
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

    requested_ids = list(args.dashboard_id)
    requested_titles = list(args.dashboard_title)
    for artifact_path in getattr(args, "dashboards_from", []) or []:
        artifact_ids, artifact_titles = load_scope_from_artifact(artifact_path)
        requested_ids.extend(item for item in artifact_ids if item not in requested_ids)
        requested_titles.extend(item for item in artifact_titles if item not in requested_titles)
        print(
            f"Scope from {artifact_path}: {len(artifact_ids)} dashboard ID(s), "
            f"{len(artifact_titles)} title(s)"
        )
    include_deleted = getattr(args, "include_deleted", False)
    scope_requested = bool(
        args.dashboard_id or args.dashboard_title or (getattr(args, "dashboards_from", []) or [])
    )
    scoped = bool(requested_ids or requested_titles)
    if scope_requested and not scoped:
        # The user asked to scope the run but nothing resolved (e.g. an empty or
        # wrong-shape --dashboards-from artifact). Fail loudly instead of
        # silently validating every dashboard in the space (#198).
        raise SystemExit(
            "No dashboards resolved from the requested scope "
            "(--dashboards-from/--dashboard-id/--dashboard-title). Refusing to "
            "fall back to an all-space run; check the artifact contents or scope flags."
        )

    dashboards = []
    dashboard_items = []
    seen_ids = set()
    if requested_ids:
        for dashboard_id in requested_ids:
            try:
                item = load_dashboard(session, args.kibana_url, args.space_id, dashboard_id, args.timeout)
            except Exception as exc:  # one stale ID should not abort the run
                print(f"  WARNING: could not load dashboard {dashboard_id}: {exc}")
                continue
            dashboard_items.append(item)
            object_id = item.get("id")
            if object_id:
                seen_ids.add(object_id)
    # Titles can only be resolved from the full listing, and an unscoped run
    # lists everything anyway. IDs fetched directly above are deduped by id so a
    # combined id+title scope validates the union without loading anything twice.
    if requested_titles or not requested_ids:
        for item in load_dashboards(
            session,
            args.kibana_url,
            args.space_id,
            args.timeout,
            per_page=args.saved_objects_per_page,
        ):
            if item.get("id") not in seen_ids:
                dashboard_items.append(item)

    selected_items = [
        item
        for item in dashboard_items
        if should_include_dashboard(item, requested_titles, requested_ids, include_deleted=include_deleted)
    ]
    space_label = args.space_id or "default"
    if scoped:
        scope = "requested dashboard(s)"
    else:
        scope = "all dashboard(s)" if include_deleted else "all non-deleted dashboard(s)"
        print(
            f"WARNING: no dashboard scope provided; validating {scope} in space '{space_label}'. "
            "Pass --dashboards-from, --dashboard-id, or --dashboard-title to scope to a migration's uploaded dashboards."
        )
    if not scoped and not include_deleted:
        deleted_skipped = sum(
            1
            for item in dashboard_items
            if str(item.get("attributes", {}).get("title", "")).startswith("[DELETED]")
        )
        if deleted_skipped:
            print(
                f"  Skipping {deleted_skipped} '[DELETED]' placeholder dashboard(s) "
                "(use --include-deleted to validate them)."
            )
    total_selected = len(selected_items)
    print(f"Inspecting {total_selected}/{len(dashboard_items)} {scope}")

    for index, item in enumerate(selected_items, start=1):
        item_title = item.get("attributes", {}).get("title", "") or item.get("id", "")
        print(f"  [{index}/{total_selected}] {item_title}", flush=True)
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
        f"Null-series panels: {summary['null_series_panels']} | "
        f"Controls without options: {summary['empty_controls']} | "
        f"Unreadable axes: {summary['unreadable_axis_panels']} | "
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
            f"null_series={len(dashboard.get('null_series_panels', []))}, "
            f"empty_controls={len(dashboard.get('empty_controls', []))}, "
            f"not_checked={len(dashboard['not_runtime_checked_panels'])}, "
            f"overlaps={len(dashboard['layout']['overlaps'])}, "
            f"screenshot={screenshot_status}, "
            f"browser={browser_status}"
        )
        print_panel_findings(dashboard)
    print(f"Report: {output_path}")

    failure_reasons = []
    if args.fail_on_runtime_errors and summary["runtime_error_panels"]:
        failure_reasons.append(f"{summary['runtime_error_panels']} panel runtime error(s)")
    if args.fail_on_layout_issues and summary["dashboards_with_layout_issues"]:
        failure_reasons.append(f"{summary['dashboards_with_layout_issues']} dashboard(s) with layout issues")
    if args.fail_on_empty_panels and summary["empty_panels"]:
        failure_reasons.append(f"{summary['empty_panels']} empty panel(s)")
    if getattr(args, "fail_on_binding_gaps", False) and summary["dashboards_with_binding_gaps"]:
        failure_reasons.append(
            f"{summary['null_series_panels']} null-series panel(s), "
            f"{summary['empty_controls']} control(s) without options"
        )
    if args.fail_on_not_runtime_checked and summary["not_runtime_checked_panels"]:
        failure_reasons.append(f"{summary['not_runtime_checked_panels']} panel(s) not runtime-checked")
    if args.fail_on_browser_errors and summary["dashboards_with_browser_errors"]:
        failure_reasons.append(f"{summary['dashboards_with_browser_errors']} dashboard(s) with browser-visible errors")
    if failure_reasons:
        raise SystemExit("Smoke validation failed: " + ", ".join(failure_reasons))


if __name__ == "__main__":
    main()
