# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Validate every panel query in compiled YAML against the live ES cluster.

Two-phase approach:
  Phase 1: Fetch field_caps once per index pattern, locally verify all
           column references exist.  Catches 'Unknown column' errors with
           zero query execution.
  Phase 2: Run LIMIT 0 variants of surviving queries in parallel to catch
           syntax/type errors without scanning data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

ES_ENDPOINT = os.environ.get("ELASTICSEARCH_ENDPOINT", "")
KEY = os.environ.get("KEY", "")
CTX = ssl.create_default_context()
MAX_BROKEN_PCT = int(os.environ.get("MAX_BROKEN_PCT", "10"))
VALIDATION_WORKERS = int(os.environ.get("VALIDATION_WORKERS", "8"))


def _es_request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{ES_ENDPOINT}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "Authorization": f"ApiKey {KEY}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                return {"error": json.loads(raw)}
            except json.JSONDecodeError:
                return {"error": {"reason": raw[:300] or f"HTTP {e.code}"}}
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"error": {"reason": f"Connection failed: {e}"}}
    return {"error": {"reason": "Max retries exceeded"}}


# ---------------------------------------------------------------------------
# Phase 1: field_caps
# ---------------------------------------------------------------------------

_FIELD_CAPS_CACHE: dict[str, set[str]] = {}


def _fetch_field_caps(index_pattern: str) -> set[str]:
    if index_pattern in _FIELD_CAPS_CACHE:
        return _FIELD_CAPS_CACHE[index_pattern]
    result = _es_request("GET", f"/{index_pattern}/_field_caps?fields=*")
    fields = set(result.get("fields", {}).keys())
    _FIELD_CAPS_CACHE[index_pattern] = fields
    return fields


_BUILTIN = {"@timestamp", "time_bucket", "count", "value", "result", "BUCKET", "TBUCKET"}
_AGG_FNS = {
    "AVG", "SUM", "MAX", "MIN", "COUNT", "RATE", "IRATE",
    "PERCENTILE", "LAST", "FIRST", "COUNT_DISTINCT",
    "MEDIAN", "STDDEV", "VARIANCE",
}
_ESQL_FNS = {
    "BUCKET", "TBUCKET", "DATE_TRUNC", "DATE_FORMAT", "DATE_DIFF",
    "DATE_PARSE", "DATE_EXTRACT", "ROUND", "FLOOR", "CEIL", "ABS",
    "LENGTH", "TO_STRING", "TO_INTEGER", "TO_DOUBLE", "TRIM",
    "CASE", "COALESCE", "GREATEST", "LEAST", "MV_AVG",
    "TO_LOWER", "TO_UPPER", "CONCAT", "SUBSTRING",
}


def _extract_query_fields(query: str) -> tuple[str, list[str]]:
    index_pattern = "metrics-*"
    m = re.search(r"FROM\s+([\w.*-]+)", query, re.IGNORECASE)
    if m:
        index_pattern = m.group(1)

    fields: list[str] = []
    derived_aliases = {
        match.group(1)
        for match in re.finditer(r"(?<![!<>=])\b([A-Za-z_][\w.]*)\s*=(?!=)", query)
    }

    for m in re.finditer(r"(?:AVG|SUM|MAX|MIN|COUNT|RATE|IRATE)\((\w[\w.]*)\)", query):
        name = m.group(1)
        if name != "*":
            fields.append(name)

    for m in re.finditer(r"\bBY\b\s+(.+?)(?:\n|\||$)", query, re.IGNORECASE | re.DOTALL):
        for part in _split_parens(m.group(1)):
            part = part.strip()
            if "=" in part:
                rhs = part.split("=", 1)[1].strip()
                if "(" not in rhs and not rhs.startswith('"'):
                    fields.append(rhs)
            elif "(" not in part:
                fields.append(part)

    for m in re.finditer(
        r"(\w[\w.]*)\s*(?:NOT RLIKE|RLIKE|NOT LIKE|LIKE|==|!=)\s*\"",
        query,
        re.IGNORECASE,
    ):
        fields.append(m.group(1))

    skip = _BUILTIN | _AGG_FNS | _ESQL_FNS | derived_aliases
    clean = []
    for f in fields:
        f = f.strip().rstrip("|").strip()
        if not f or f in skip or f.upper() in skip or f.startswith('"') or f.startswith("?"):
            continue
        clean.append(f)
    return index_pattern, clean


def _split_parens(text: str) -> list[str]:
    parts, current, depth = [], [], 0
    for ch in text:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def phase1_check(query: str) -> list[str]:
    index_pattern, fields = _extract_query_fields(query)
    if not fields:
        return []
    available = _fetch_field_caps(index_pattern)
    return [f for f in fields
            if f not in available and f not in _BUILTIN and f.upper() not in _AGG_FNS | _ESQL_FNS]


# ---------------------------------------------------------------------------
# Phase 2: LIMIT 0
# ---------------------------------------------------------------------------

def _sub_time_params(query: str) -> str:
    now_ms = int(time.time() * 1000)
    ago_ms = now_ms - 6 * 3600 * 1000
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now_ms / 1000))
    ago_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ago_ms / 1000))
    return query.replace("?_tstart", f'"{ago_iso}"').replace("?_tend", f'"{now_iso}"')


def _inject_limit_zero(query: str) -> str:
    q = query.rstrip()
    if re.search(r"\|\s*LIMIT\s+\d+", q, re.IGNORECASE):
        return re.sub(r"(\|\s*LIMIT\s+)\d+", r"\g<1>0", q, flags=re.IGNORECASE)
    return q + "\n| LIMIT 0"


def phase2_validate(query: str) -> tuple[str, str, int]:
    query = query.strip()
    is_promql = query.upper().startswith("PROMQL ")
    if query.upper().startswith("ROW "):
        return "OK", "constant/promql", 1

    if not is_promql:
        query = _sub_time_params(query)
        query = _inject_limit_zero(query)

    result = _es_request("POST", "/_query", {"query": query})
    if "error" in result:
        err = result["error"]
        if isinstance(err, dict):
            reason = err.get("reason", "")
            root = err.get("root_cause", [])
            if root and isinstance(root, list):
                reason = root[0].get("reason", reason)
            return "ERROR", (reason or json.dumps(err))[:250], 0
        return "ERROR", str(err)[:250], 0
    return "OK", "valid", len(result.get("values", []) or [])


# ---------------------------------------------------------------------------
# Panel extraction
# ---------------------------------------------------------------------------

def extract_panels(yaml_path: str):
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    panels = []
    for dash in data.get("dashboards", []):
        _walk_panels(dash.get("panels", []), panels)
    return panels


def _walk_panels(items, out):
    for item in items:
        if "section" in item:
            _walk_panels(item["section"].get("panels", []), out)
        elif "esql" in item or "promql" in item:
            out.append(item)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "yaml_dir",
        nargs="?",
        default="migration_output_native/dashboards/yaml",
        help="Directory containing compiled YAML dashboards",
    )
    parser.add_argument(
        "specific",
        nargs="?",
        default=None,
        help="Optional dashboard filename substring to filter on",
    )
    parser.add_argument(
        "--es-endpoint",
        default=os.environ.get("ELASTICSEARCH_ENDPOINT", ""),
        help="Elasticsearch endpoint URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("KEY", ""),
        help="Elasticsearch API key",
    )
    parser.add_argument(
        "--max-broken-pct",
        type=int,
        default=int(os.environ.get("MAX_BROKEN_PCT", "10")),
        help="Maximum allowed broken-panel percentage",
    )
    parser.add_argument(
        "--validation-workers",
        type=int,
        default=int(os.environ.get("VALIDATION_WORKERS", "8")),
        help="Thread pool size for query validation",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None):
    global ES_ENDPOINT
    global KEY
    global MAX_BROKEN_PCT
    global VALIDATION_WORKERS

    args = parse_args(argv)
    ES_ENDPOINT = args.es_endpoint
    KEY = args.api_key
    MAX_BROKEN_PCT = args.max_broken_pct
    VALIDATION_WORKERS = args.validation_workers

    if not ES_ENDPOINT or not KEY:
        print("ERROR: ELASTICSEARCH_ENDPOINT and KEY must be set (or pass --es-endpoint/--api-key)")
        return 1

    yaml_dir = args.yaml_dir
    specific = args.specific

    yaml_files = sorted(
        f for f in os.listdir(yaml_dir)
        if f.endswith(".yaml") and (not specific or specific in f)
    )

    all_panels: list[tuple[str, str, str, str]] = []
    for yf in yaml_files:
        path = os.path.join(yaml_dir, yf)
        dash_name = yf.replace(".yaml", "")
        for panel in extract_panels(path):
            title = panel.get("title", "?")
            esql = panel.get("esql") or panel.get("promql") or {}
            query = esql.get("query", "") if isinstance(esql, dict) else ""
            ptype = esql.get("type", "?") if isinstance(esql, dict) else "?"
            if query:
                all_panels.append((dash_name, title, ptype, query))

    print(f"Found {len(all_panels)} panels across {len(yaml_files)} dashboards\n")

    # Phase 1
    t0 = time.time()
    index_patterns = set()
    for _, _, _, q in all_panels:
        m = re.search(r"FROM\s+([\w.*-]+)", q, re.IGNORECASE)
        if m:
            index_patterns.add(m.group(1))
    print(f"Phase 1: field_caps for {len(index_patterns)} index pattern(s)...")
    for ip in index_patterns:
        caps = _fetch_field_caps(ip)
        print(f"  {ip}: {len(caps)} fields")

    phase1_pass = []
    phase1_fail = []
    for dash, title, ptype, query in all_panels:
        if query.upper().startswith("PROMQL ") or query.upper().startswith("ROW "):
            phase1_pass.append((dash, title, ptype, query))
            continue
        missing = phase1_check(query)
        if missing:
            phase1_fail.append((dash, title, ptype, missing))
        else:
            phase1_pass.append((dash, title, ptype, query))

    t1 = time.time()
    print(f"  Phase 1: {t1 - t0:.1f}s — {len(phase1_pass)} pass, {len(phase1_fail)} fail\n")

    # Phase 2
    print(f"Phase 2: LIMIT 0 validation of {len(phase1_pass)} queries (workers={VALIDATION_WORKERS})...")

    phase2_results: dict[int, tuple] = {}

    def _validate(args):
        idx, dash, title, ptype, query = args
        status, detail, rows = phase2_validate(query)
        return idx, dash, title, ptype, status, detail, rows

    with ThreadPoolExecutor(max_workers=VALIDATION_WORKERS) as pool:
        work = [(i, d, t, p, q) for i, (d, t, p, q) in enumerate(phase1_pass)]
        futs = {pool.submit(_validate, item): item[0] for item in work}
        for fut in as_completed(futs):
            idx, dash, title, ptype, status, detail, rows = fut.result()
            phase2_results[idx] = (dash, title, ptype, status, detail, rows)

    t2 = time.time()
    print(f"  Phase 2: {t2 - t1:.1f}s\n")

    # Report
    total_ok = total_err = total_empty = 0
    dashboard_results: dict[str, dict] = {}

    for dash, title, ptype, missing in phase1_fail:
        entry = dashboard_results.setdefault(dash, {"ok": 0, "err": 0, "empty": 0, "errors": []})
        entry["err"] += 1
        entry["errors"].append((title, ptype, "MISSING_FIELD", ", ".join(missing[:3])))
        total_err += 1

    for idx in sorted(phase2_results):
        dash, title, ptype, status, detail, rows = phase2_results[idx]
        entry = dashboard_results.setdefault(dash, {"ok": 0, "err": 0, "empty": 0, "errors": []})
        if status == "OK":
            entry["ok"] += 1
            total_ok += 1
        else:
            entry["err"] += 1
            entry["errors"].append((title, ptype, "ERROR", detail[:120]))
            total_err += 1

    for dash in sorted(dashboard_results):
        r = dashboard_results[dash]
        total = r["ok"] + r["err"] + r["empty"]
        print(f"\n{'=' * 70}")
        print(f"  {dash}")
        print(f"  OK: {r['ok']}  |  ERROR: {r['err']}  |  Total: {total}")
        print(f"{'=' * 70}")
        for title, ptype, kind, detail in r["errors"]:
            print(f"  [{kind:14s}] ({ptype}) {title}")
            print(f"                  {detail}")

    total = total_ok + total_err + total_empty
    broken = total_err + total_empty
    broken_pct = (broken * 100 / total) if total else 0

    print(f"\n{'#' * 70}")
    print(f"  TOTAL: OK={total_ok}  ERROR={total_err}  EMPTY={total_empty}  ({total_ok}/{total})")
    print(f"  Broken: {broken_pct:.1f}% (threshold: {MAX_BROKEN_PCT}%)")
    print(f"  Wall time: {t2 - t0:.1f}s")
    print(f"{'#' * 70}")

    if broken_pct > MAX_BROKEN_PCT:
        print(f"\n  VALIDATION FAILED: {broken_pct:.1f}% broken > {MAX_BROKEN_PCT}% threshold.")
        return 1

    print("\n  VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
