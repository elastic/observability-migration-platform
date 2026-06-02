#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""
Validate panels by reading ES|QL queries directly from YAML source files.

Improvements over original:
  - Parallel execution (ThreadPoolExecutor, 20 workers)
  - Query deduplication (same query shared across panels runs once)
  - Tiered timeouts (ROW=5s, FROM=30s, TS=90s, PROMQL=60s, PROMQL+regex=300s)
  - Column schema validation (dimension/metric/breakdown fields checked against actual output)
  - Zero-row detection (query passes but panel would render blank)
  - Lens panel inventory (explicit skip with count, not silent ignore)
  - Markdown panels silently ignored (no query to validate)
  - Static structural validation (chart-type vs query shape, declared fields vs parsed fields)
"""
import glob
import importlib.util as _ilu
import json
import os
import re
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yaml

# Reconstruct Lens panels into ES|QL so they validate like native ES|QL panels.
# This script is run directly (not imported as a package), so load the sibling
# module by path rather than via a package-relative import.
_lr_spec = _ilu.spec_from_file_location(
    "lens_reconstruct",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "lens_reconstruct.py"),
)
assert _lr_spec is not None and _lr_spec.loader is not None
lens_reconstruct = _ilu.module_from_spec(_lr_spec)
_lr_spec.loader.exec_module(lens_reconstruct)

ES_ENDPOINT = os.environ["ELASTICSEARCH_ENDPOINT"].rstrip("/")
API_KEY = os.environ["KEY"]

HEADERS = {
    "Authorization": f"ApiKey {API_KEY}",
    "Content-Type": "application/json",
}

CTX = ssl.create_default_context()

_now = datetime.now(UTC)
_TSTART = (_now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
_TEND = _now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

_DEFAULT_PARAMS: dict = {
    "_tstart": _TSTART,
    "_tend": _TEND,
    "interval": "1m",
}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_E2E_ROOT = os.environ.get("E2E_ROOT", "/tmp/mig-to-kbn-e2e")

YAML_FILES = sorted(
    glob.glob(os.path.join(_E2E_ROOT, "grafana/*/dashboards/yaml/*.yaml"))
    + glob.glob(os.path.join(_REPO_ROOT, "e2e_datadog_run/*/dashboards/yaml/*.yaml"))
)

# Serverless ES has ~1 GB per-query circuit breaker. PROMQL and complex TS
# queries can approach that limit individually. FROM/ROW queries are tiny.
# Separate pools prevent large queries from racing each other to OOM.
WORKERS_SMALL = 10   # FROM, ROW
WORKERS_LARGE =  3   # TS, PROMQL

# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

# ?param -> underlying field, parsed from "WHERE <field> <op> ?param". Grafana
# dashboard template variables (?node, ?job, ?instance, ?cluster, ...) become
# ES|QL params; binding them to "" matches nothing, so a panel that is correct
# for a selected variable value looks like a (false) zero-row result. Sample a
# real value of the underlying field from the cluster instead — the faithful
# equivalent of a user picking a value from the dashboard dropdown.
# Matches both ES|QL comparisons (``instance == ?node``) and PromQL-style label
# matchers inside a PROMQL selector (``{instance=?instance}``). The operator
# alternation lists multi-char ops first; the trailing ``=(?!=)`` catches a bare
# single ``=`` (PromQL equality) without also consuming the ``==`` case.
_PARAM_FIELD_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_.]*)\s*"
    r"(?:==|!=|>=|<=|>|<|=~|!~|RLIKE|LIKE|IN|=(?!=))\s*\(?\s*\?(\w+)",
    re.IGNORECASE,
)

# Cache sampled field values for the whole run so we hit the cluster at most
# once per field. None means "looked up, nothing usable".
_FIELD_VALUE_CACHE: dict[str, str | None] = {}


def _param_field_map(query: str) -> dict[str, str]:
    """Map each ``?param`` to the field it is compared against in the query."""
    mapping: dict[str, str] = {}
    for field, param in _PARAM_FIELD_RE.findall(query):
        mapping.setdefault(param, field)
    return mapping


def _sample_field_value(field: str) -> str | None:
    """Return the most common non-null value of *field*, or None.

    Cached per field. Used only to bind dashboard-template params, never to
    alter the query under test.
    """
    if field in _FIELD_VALUE_CACHE:
        return _FIELD_VALUE_CACHE[field]
    value: str | None = None
    # ``field`` may be backtick-quoted in the query; the lookup needs it quoted
    # too, but the bound value is the bare string.
    quoted = field if field.startswith("`") else f"`{field}`" if "-" in field else field
    probe = (
        f"FROM metrics-* | WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend "
        f"| WHERE {quoted} IS NOT NULL | STATS c = COUNT(*) BY {quoted} "
        f"| SORT c DESC | LIMIT 1"
    )
    body = {
        "query": probe,
        "params": [{"_tstart": _TSTART}, {"_tend": _TEND}],
        "columnar": True,
    }
    req = Request(
        f"{ES_ENDPOINT}/_query", data=json.dumps(body).encode(),
        headers=HEADERS, method="POST",
    )
    try:
        with urlopen(req, timeout=30, context=CTX) as resp:
            data = json.loads(resp.read())
            cols = [c["name"] for c in data.get("columns", [])]
            columns = data.get("values", [])  # columnar: one list per column
            bare = field.strip("`")
            idx = next(
                (i for i, c in enumerate(cols) if c.strip("`") == bare),
                None,
            )
            if idx is not None and idx < len(columns) and columns[idx]:
                value = columns[idx][0]
    except Exception:
        value = None
    _FIELD_VALUE_CACHE[field] = value
    return value


def _build_params(query: str) -> list | None:
    names = set(re.findall(r"\?(\w+)", query))
    if not names:
        return None
    field_map = _param_field_map(query)
    params: list[dict] = []
    for name in names:
        if name in _DEFAULT_PARAMS:
            params.append({name: _DEFAULT_PARAMS[name]})
            continue
        field = field_map.get(name)
        sampled = _sample_field_value(field) if field else None
        params.append({name: sampled if sampled is not None else ""})
    return params


_DASHBOARD_TIME_FILTER = "| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend"


def _inject_dashboard_time_filter(query: str) -> str:
    """Bound a query to the dashboard time window, the way Kibana would.

    The migrator deliberately strips the ``@timestamp >= ?_tstart AND
    @timestamp <= ?_tend`` filter from emitted panel queries because Kibana
    applies the dashboard time picker implicitly at render time. Running the
    raw query here — outside Kibana — therefore scans the entire datastream
    history, which both trips the Serverless ~1 GB per-query circuit breaker on
    busy metrics and makes zero-row detection meaningless (a panel that is
    correct for "last 4h" can look non-empty only because it swept all of
    time). Re-inject the same bound Kibana would apply so validation mirrors
    real execution.

    * ``TS`` / ``FROM`` get a ``| WHERE @timestamp >= ?_tstart AND @timestamp
      <= ?_tend`` filter right after the source command.
    * ``PROMQL`` gets ``start=?_tstart end=?_tend`` options on the command line
      (per the PROMQL command reference, which documents ``start``/``end`` time
      range boundaries). The bare ``step=`` only sets resolution, not range.
    * ``ROW`` has no index to bound.

    Queries that already reference ``@timestamp``/``?_tstart`` (TS/FROM) or
    already carry ``start=``/``end=`` (PROMQL) are left untouched.
    """
    if not query:
        return query
    stripped = query.lstrip()
    first = stripped.split(None, 1)[0].upper() if stripped else ""
    lines = query.splitlines()
    if not lines:
        return query

    if first in ("TS", "FROM"):
        if "@timestamp" in query or "?_tstart" in query:
            return query
        # Insert immediately after the source command (line 0) so the time
        # bound is applied before any STATS/aggregation.
        lines.insert(1, _DASHBOARD_TIME_FILTER)
        return "\n".join(lines)

    if first == "PROMQL":
        head = lines[0]
        if re.search(r"\bstart\s*=", head) or re.search(r"\bend\s*=", head):
            return query
        # Insert start=/end= right after the PROMQL keyword, before the other
        # options (index=, step=, value=...).
        lines[0] = re.sub(
            r"^(\s*PROMQL)\b",
            r"\1 start=?_tstart end=?_tend",
            head,
            count=1,
        )
        return "\n".join(lines)

    return query


def _timeout_for(query: str) -> int:
    """Tiered timeout based on query mode and complexity."""
    first = query.split()[0].upper() if query else "FROM"
    if first == "ROW":
        return 5
    if first == "FROM":
        return 30
    if first == "TS":
        return 90
    # PROMQL — complex EVAL chains with regex REPLACE are the slow path
    if "REPLACE" in query and query.count("| EVAL") >= 3:
        return 300
    return 60


def es_esql(query: str) -> dict:
    """Execute query; return {ok, columns, row_count} or {ok:False, error, reason, raw}."""
    # Mirror Kibana's dashboard time picker: bound TS/FROM scans to the test
    # window so we don't false-positive on circuit breakers or sweep all of
    # history when checking for zero rows.
    query = _inject_dashboard_time_filter(query)
    timeout = _timeout_for(query)
    url = f"{ES_ENDPOINT}/_query"
    body: dict = {"query": query, "columnar": True}
    params = _build_params(query)
    if params:
        body["params"] = params
    req = Request(url, data=json.dumps(body).encode(), headers=HEADERS, method="POST")
    try:
        with urlopen(req, timeout=timeout, context=CTX) as resp:
            data = json.loads(resp.read())
            columns = [c["name"] for c in data.get("columns", [])]
            values = data.get("values", [])
            row_count = len(values[0]) if values else 0
            return {"ok": True, "columns": columns, "row_count": row_count}
    except HTTPError as e:
        body_text = e.read().decode(errors="replace")
        try:
            err_json = json.loads(body_text)
            err_type = (
                err_json.get("error", {}).get("type", "")
                or err_json.get("error", {}).get("reason", "")[:160]
            )
            reason = err_json.get("error", {}).get("reason", "")[:300]
        except Exception:
            err_type = body_text[:200]
            reason = ""
        return {"ok": False, "error": err_type, "reason": reason, "raw": body_text[:600]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "reason": "", "raw": ""}


# ---------------------------------------------------------------------------
# Static structural validation
# ---------------------------------------------------------------------------

_TIME_FIELDS = {"time_bucket", "timestamp_bucket", "step", "@timestamp"}

# chart types that require a time dimension in the query
_TIME_REQUIRED_TYPES = {"timeseries", "bar", "area", "line"}

# chart types that should NOT have a breakdown (or rarely do)
_NO_BREAKDOWN_TYPES = {"gauge", "singlestat", "metric", "stat"}


def _parse_query_shape(query: str) -> dict:
    """
    Parse an ES|QL query and return its structural shape without executing it.

    Returns:
        mode: "from" | "ts" | "row" | "promql" | "unknown"
        has_stats: bool — query contains a STATS stage
        projected_fields: list of field names that appear in the final output
        time_fields: list of fields that look like time dimensions
        group_fields: list of BY-clause fields from the last STATS
        metric_fields: list of aggregated output fields from the last STATS
    """
    if not query:
        return {"mode": "unknown", "has_stats": False, "projected_fields": [],
                "time_fields": [], "group_fields": [], "metric_fields": []}

    first_word = query.split()[0].upper()
    if first_word == "ROW":
        return {"mode": "row", "has_stats": False, "projected_fields": [],
                "time_fields": [], "group_fields": [], "metric_fields": []}

    mode_map = {"FROM": "from", "TS": "ts", "PROMQL": "promql"}
    mode = mode_map.get(first_word, "unknown")

    stages = _split_pipeline(query)

    has_stats = any(s.strip().upper().startswith("STATS ") for s in stages)

    projected_fields: list[str] = []
    time_fields: list[str] = []
    group_fields: list[str] = []
    metric_fields: list[str] = []

    # Find indices for STATS and KEEP stages
    stats_indices = [i for i, s in enumerate(stages) if s.strip().upper().startswith("STATS ")]
    keep_indices  = [i for i, s in enumerate(stages) if s.strip().upper().startswith("KEEP ")]

    # KEEP after the last STATS is the authoritative projected column list
    # (EVAL between STATS and KEEP can rename fields; we trust KEEP over STATS output)
    keep_after_stats = (
        keep_indices
        and (not stats_indices or keep_indices[-1] > stats_indices[-1])
    )

    if stats_indices:
        last_stats = stages[stats_indices[-1]].strip()
        body = last_stats[6:].strip()  # strip "STATS "
        agg_part, by_part = _split_by_keyword(body)

        for col in _split_csv(agg_part):
            name = _alias_name(col)
            if name:
                metric_fields.append(name)

        for col in _split_csv(by_part):
            name = _alias_name(col)
            if not name:
                continue
            group_fields.append(name)
            if name.lower() in {f.lower() for f in _TIME_FIELDS} or _is_time_expr(col):
                time_fields.append(name)

        if keep_after_stats:
            # KEEP is the authoritative output; use it for projected_fields
            keep_stage = stages[keep_indices[-1]].strip()
            projected_fields = [p.strip() for p in _split_csv(keep_stage[5:]) if p.strip()]
            # Extend time_fields with any KEEP fields that look time-like
            for f in projected_fields:
                if f.lower() in {t.lower() for t in _TIME_FIELDS} and f not in time_fields:
                    time_fields.append(f)
        else:
            projected_fields = group_fields + metric_fields
            # Collect EVAL-introduced field aliases (EVAL can add columns without KEEP)
            for s in stages:
                if not s.strip().upper().startswith("EVAL "):
                    continue
                eval_body = s.strip()[5:]
                for assignment in _split_csv(eval_body):
                    name = _alias_name(assignment)
                    if name and name not in projected_fields:
                        projected_fields.append(name)

    elif keep_indices:
        keep_stage = stages[keep_indices[-1]].strip()
        projected_fields = [p.strip() for p in _split_csv(keep_stage[5:]) if p.strip()]
        time_fields = [f for f in projected_fields if f.lower() in {t.lower() for t in _TIME_FIELDS}]

    return {
        "mode": mode,
        "has_stats": has_stats,
        "projected_fields": projected_fields,
        "time_fields": time_fields,
        "group_fields": group_fields,
        "metric_fields": metric_fields,
    }


def _split_pipeline(query: str) -> list[str]:
    """Split ES|QL pipeline on top-level | (respecting triple-quoted strings and parens)."""
    stages, current = [], []
    depth, in_triple, in_single, in_double = 0, False, False, False
    i = 0
    while i < len(query):
        c = query[i]
        if in_triple:
            current.append(c)
            if query[i:i+3] == '"""':
                in_triple = False
                current.append(query[i+1])
                current.append(query[i+2])
                i += 3
                continue
        elif in_single:
            current.append(c)
            if c == "'":
                in_single = False
        elif in_double:
            current.append(c)
            if c == '"' and query[i:i+3] != '"""':
                in_double = False
        elif query[i:i+3] == '"""':
            in_triple = True
            current.extend(['"""'])
            i += 3
            continue
        elif c == "'":
            in_single = True
            current.append(c)
        elif c == '"':
            in_double = True
            current.append(c)
        elif c == '(':
            depth += 1
            current.append(c)
        elif c == ')':
            depth = max(depth - 1, 0)
            current.append(c)
        elif c == '|' and depth == 0:
            stages.append("".join(current).strip())
            current = []
        else:
            current.append(c)
        i += 1
    if current:
        stages.append("".join(current).strip())
    return [s for s in stages if s]


def _split_by_keyword(text: str) -> tuple[str, str]:
    """Split 'agg_expr BY group_expr' at the top-level BY keyword."""
    tokens = re.split(r'\bBY\b', text, maxsplit=1, flags=re.IGNORECASE)
    return (tokens[0].strip(), tokens[1].strip()) if len(tokens) == 2 else (text.strip(), "")


def _split_csv(text: str) -> list[str]:
    """Split comma-separated list respecting parentheses."""
    parts, current, depth = [], [], 0
    for c in text:
        if c == "(":
            depth += 1
            current.append(c)
        elif c == ")":
            depth = max(depth - 1, 0)
            current.append(c)
        elif c == ',' and depth == 0:
            if current:
                parts.append("".join(current).strip())
            current = []
        else:
            current.append(c)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def _alias_name(col_expr: str) -> str:
    """Extract the alias from 'alias = expr' or just the bare field name."""
    col_expr = col_expr.strip()
    # Check for assignment: alias = expr (alias on LHS)
    eq_idx = col_expr.find("=")
    if eq_idx > 0 and col_expr[eq_idx - 1] not in ("!", "<", ">", "="):
        candidate = col_expr[:eq_idx].strip()
        if re.fullmatch(r"[A-Za-z_@][A-Za-z0-9_@.]*", candidate):
            return candidate
    # Bare field reference
    if re.fullmatch(r"[A-Za-z_@][A-Za-z0-9_@.]*", col_expr):
        return col_expr
    return ""


def _is_time_expr(expr: str) -> bool:
    """Return True if the expression looks like a time-bucketing call."""
    upper = expr.upper()
    return (
        "BUCKET(@TIMESTAMP" in upper
        or "DATE_TRUNC" in upper
        or upper.strip() == "@TIMESTAMP"
    )


def static_structural_issues(panel: dict) -> list[str]:
    """
    Check YAML panel declaration against its parsed query shape.

    Returns a list of issue strings (empty = no issues).
    """
    issues: list[str] = []
    esql_block = panel.get("_esql_raw") or {}
    query = panel.get("query", "")
    chart_type = panel.get("chart_type", "")

    if not query:
        return issues

    shape = _parse_query_shape(query)

    # PROMQL and TS queries produce implicit columns (step, value, and by-group fields)
    # that aren't visible in the ES|QL pipeline syntax. Skip field-existence checks
    # for these modes — runtime schema validation covers them instead.
    if shape["mode"] in ("promql", "ts", "row"):
        return issues

    # --- Declared vs parsed field consistency ---
    declared_dim   = (esql_block.get("dimension") or {}).get("field")
    declared_metrics = [m.get("field") for m in (esql_block.get("metrics") or []) if m.get("field")]
    declared_bd    = (esql_block.get("breakdown") or {}).get("field")

    # Compare on the unquoted identifier: KEEP/declaration may backtick-quote
    # hyphenated or reserved names inconsistently, but they name the same column.
    projected = {_strip_backticks(f) for f in shape["projected_fields"]}

    # Dimension field must appear in query output
    if declared_dim and _strip_backticks(declared_dim) not in projected:
        issues.append(
            f"static: declared dimension '{declared_dim}' not found in query output "
            f"(query projects: {sorted(projected)[:6]})"
        )

    # Metric fields must appear in query output
    for mf in declared_metrics:
        if _strip_backticks(mf) not in projected:
            issues.append(
                f"static: declared metric '{mf}' not found in query output "
                f"(query projects: {sorted(projected)[:6]})"
            )

    # Breakdown field must appear in query output
    if declared_bd and _strip_backticks(declared_bd) not in projected:
        issues.append(
            f"static: declared breakdown '{declared_bd}' not found in query output "
            f"(query projects: {sorted(projected)[:6]})"
        )

    # --- Chart-type vs time dimension ---
    if chart_type and chart_type.lower() in _TIME_REQUIRED_TYPES:
        if not shape["time_fields"] and not declared_dim:
            issues.append(
                f"static: chart_type='{chart_type}' expects a time dimension "
                f"but query has no time-bucketing and no dimension declared"
            )

    # --- Gauge/stat panels should not have breakdown ---
    if chart_type and chart_type.lower() in _NO_BREAKDOWN_TYPES:
        if declared_bd:
            issues.append(
                f"static: chart_type='{chart_type}' is a scalar panel "
                f"but breakdown='{declared_bd}' is declared"
            )

    return issues


# ---------------------------------------------------------------------------
# Panel collection
# ---------------------------------------------------------------------------

def _expected_columns(esql: dict) -> list[str]:
    """Ordered list of column names the panel declaration requires."""
    cols: list[str] = []
    dim = esql.get("dimension") or {}
    if dim.get("field"):
        cols.append(dim["field"])
    for m in esql.get("metrics") or []:
        if m.get("field"):
            cols.append(m["field"])
    bd = esql.get("breakdown") or {}
    if bd.get("field"):
        cols.append(bd["field"])
    return cols


def _lens_metric_fields(lens: dict) -> list[str]:
    """Return the metric (not breakdown) field names referenced by a Lens dict.

    Counter typing only matters for the aggregated metric; breakdown fields are
    grouping keys, never aggregated.
    """
    fields: list[str] = []
    metrics = lens.get("metrics")
    if isinstance(metrics, list):
        for m in metrics:
            if isinstance(m, dict) and m.get("field"):
                fields.append(m["field"])
    primary = lens.get("primary")
    if isinstance(primary, dict) and primary.get("field"):
        fields.append(primary["field"])
    return fields


def _counter_fields(fields: set[str]) -> set[str]:
    """Return the subset of ``fields`` the cluster types as counters.

    A counter (``time_series_metric: counter``) cannot be aggregated on the
    FROM command with a bare SUM/AVG; the reconstruction needs to know which
    fields require the TS form. One batched ``_field_caps`` POST (body, not URL,
    to avoid URL-length limits) covers every Lens metric field at once.
    """
    if not fields:
        return set()
    body = json.dumps({"fields": sorted(fields)}).encode()
    req = Request(
        f"{ES_ENDPOINT}/metrics-*/_field_caps?include_unmapped=false",
        data=body, headers=HEADERS, method="POST",
    )
    counters: set[str] = set()
    try:
        with urlopen(req, timeout=60, context=CTX) as resp:
            data = json.loads(resp.read())
        for field, types in data.get("fields", {}).items():
            for type_name, meta in types.items():
                if type_name == "unmapped":
                    continue
                if meta.get("time_series_metric") == "counter":
                    counters.add(field)
                    break
    except Exception:
        # On probe failure, degrade to gauge assumption (FROM form). Worst case
        # the counter panels report the same failure they would have anyway.
        return set()
    return counters


def _walk_panels(panels: list, entries: list, slug: str, dashboard_title: str) -> None:
    for p in panels:
        if "section" in p:
            _walk_panels(p["section"].get("panels", []), entries, slug, dashboard_title)
            continue

        base = {"slug": slug, "dashboard": dashboard_title, "panel": p.get("title", "?")}

        if "esql" in p:
            esql = p["esql"]
            query = (esql.get("query") or "").strip()
            entries.append({
                **base,
                "kind": "esql",
                "query": query,
                "expected_cols": _expected_columns(esql),
                "chart_type": p.get("type", ""),
                "_esql_raw": esql,
            })
        elif "lens" in p:
            # Defer ES|QL reconstruction until after a batch counter-field probe
            # (collect_panels) so counter-typed metrics get the TS form.
            entries.append({
                **base,
                "kind": "lens",
                "query": None,
                "expected_cols": [],
                "is_lens": True,
                "_lens_raw": p["lens"],
            })
        # markdown → no entry (nothing to validate)


def collect_panels() -> list[dict]:
    all_panels: list[dict] = []
    for yf in YAML_FILES:
        slug = yf.split("/")[-4]
        with open(yf) as f:
            doc = yaml.safe_load(f)
        if not isinstance(doc, dict):
            continue
        for dash in doc.get("dashboards", []):
            title = dash.get("name") or dash.get("title") or yf.split("/")[-1]
            _walk_panels(dash.get("panels", []), all_panels, slug, title)

    _reconstruct_lens_panels(all_panels)
    return all_panels


def _reconstruct_lens_panels(entries: list[dict]) -> None:
    """Reconstruct deferred Lens entries into ES|QL, counter-aware.

    Runs after the walk so a single batched ``_field_caps`` probe classifies
    every Lens metric field's counter-ness before reconstruction. Mutates each
    Lens entry in place: success fills ``query``/``expected_cols``/``chart_type``;
    failure records ``unsupported_reason``.
    """
    lens_entries = [e for e in entries if e.get("is_lens") and "_lens_raw" in e]
    if not lens_entries:
        return

    metric_fields: set[str] = set()
    for e in lens_entries:
        metric_fields.update(_lens_metric_fields(e["_lens_raw"]))
    counters = _counter_fields(metric_fields)

    for e in lens_entries:
        lens = e.pop("_lens_raw")
        query, cols, reason = lens_reconstruct.lens_to_esql(lens, counter_fields=counters)
        if query:
            e["query"] = query
            e["expected_cols"] = cols
            e["chart_type"] = lens.get("type", "")
        else:
            e["query"] = None
            e["expected_cols"] = []
            e["unsupported_reason"] = reason


# ---------------------------------------------------------------------------
# Schema validation (runtime: actual ES response columns)
# ---------------------------------------------------------------------------

def _zero_row_cause(query: str) -> str:
    """Return a short human-readable hint about why a query might return zero rows."""
    q = query.upper()
    first = query.split()[0].upper() if query else ""
    if first == "FROM" and query.lstrip().upper().startswith("FROM LOGS"):
        return "logs index likely not seeded — run: bash scripts/run_seed_data.sh"
    if "NOW() -" in q and "?_TSTART" not in q:
        return "hardcoded time window (not using ?_tstart) — check if seed covers that range"
    # Check for equality filters that need specific seeded values
    value_filters = re.findall(r'(\w[\w.]*)\s*==\s*"([^"]+)"', query)
    if value_filters:
        pairs = ", ".join(f"{k}={v}" for k, v in value_filters[:3])
        return f"filter requires seeded data with exact values ({pairs}) — re-run seed"
    return "data may not be seeded for this stream — run: bash scripts/run_seed_data.sh"


def _strip_backticks(name: str) -> str:
    """Normalize an ES|QL identifier for comparison.

    Panel declarations store hyphenated / reserved field names backtick-quoted
    (``breakdown.field: '`client-id`'``) because the query text requires the
    quoting, but the ``_query`` response reports the bare column name
    (``client-id``). Compare on the unquoted form so a correctly-projected
    column is not reported as missing.
    """
    return str(name).strip().strip("`")


def _missing_columns(result: dict, expected: list[str]) -> list[str]:
    """Return expected column names absent from the query's actual output."""
    if not result.get("ok") or not expected:
        return []
    actual = {_strip_backticks(c) for c in result["columns"]}
    return [c for c in expected if _strip_backticks(c) not in actual]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    panels = collect_panels()

    # Reconstructed Lens panels carry a query and validate exactly like ES|QL
    # panels; unsupported Lens panels (no query) are reported with their reason.
    esql_panels = [p for p in panels
                   if (p["kind"] == "esql" or p.get("is_lens")) and p.get("query")]
    lens_unsupported = [p for p in panels if p.get("is_lens") and not p.get("query")]

    # Static structural validation (no ES call needed). Reconstructed Lens
    # panels are skipped: the reconstruction parses migrator-authored ES|QL
    # idioms it never emits and guarantees declared==projected by construction,
    # so the static check would be redundant and false-positive-prone.
    static_issues_all: list[dict] = []
    for p in esql_panels:
        if p.get("is_lens"):
            continue
        issues = static_structural_issues(p)
        if issues:
            static_issues_all.append({**p, "static_issues": issues})

    # Deduplicate: build a set of unique query strings
    unique_queries = list({p["query"] for p in esql_panels})
    total_panels = len(esql_panels)
    reused = total_panels - len(unique_queries)

    lens_validated = [p for p in esql_panels if p.get("is_lens")]
    print(f"Panels:        {len(panels)} total  "
          f"({total_panels} validated incl. {len(lens_validated)} lens, "
          f"{len(lens_unsupported)} lens unsupported)")
    print(f"Unique queries: {len(unique_queries)}  ({reused} reused across panels)")
    print(f"Workers:       {WORKERS_SMALL} small (FROM/ROW)  {WORKERS_LARGE} large (TS/PROMQL)")
    print(f"Static issues: {len(static_issues_all)} panels with structural problems")
    print()

    # --- Run queries in parallel (tiered by memory cost) -------------------
    query_results: dict[str, dict] = {}
    done = 0

    def _is_small(q: str) -> bool:
        first = q.split()[0].upper() if q else "FROM"
        return first in ("FROM", "ROW")

    small_queries = [q for q in unique_queries if _is_small(q)]
    large_queries = [q for q in unique_queries if not _is_small(q)]

    def _run_batch(queries: list[str], workers: int, label: str) -> None:
        nonlocal done
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {pool.submit(es_esql, q): q for q in queries}
            for future in as_completed(future_map):
                q = future_map[future]
                try:
                    query_results[q] = future.result()
                except Exception as exc:
                    query_results[q] = {"ok": False, "error": str(exc), "reason": "", "raw": ""}
                done += 1
                if done % 25 == 0 or done == len(unique_queries):
                    print(f"  [{done}/{len(unique_queries)}] done ({label}) …", flush=True)

    _run_batch(small_queries, WORKERS_SMALL, "FROM/ROW")
    _run_batch(large_queries, WORKERS_LARGE, "TS/PROMQL")

    print()

    # --- Evaluate each panel -----------------------------------------------
    passed: int = 0
    failed: list[dict] = []
    warnings: list[dict] = []
    static_warn: list[dict] = []

    for p in esql_panels:
        result = query_results.get(p["query"], {"ok": False, "error": "missing result"})
        label  = f"[{p['slug']}] {p['dashboard']} / {p['panel']}"

        # Static structural issues (reported regardless of ES outcome).
        # Reconstructed Lens panels skip this check (see note above).
        s_issues = [] if p.get("is_lens") else static_structural_issues(p)
        if s_issues:
            for issue in s_issues:
                print(f"  ⚑ {label}")
                print(f"      {issue}")
            static_warn.append({**p, "static_issues": s_issues})

        if not result["ok"]:
            err    = result.get("error", "")
            reason = result.get("reason", "")
            print(f"  ✗ {label}")
            print(f"      {err}")
            if reason and reason != err:
                print(f"      {reason[:120]}")
            failed.append({**p, "error": err, "reason": reason, "raw": result.get("raw", "")})
            continue

        missing  = _missing_columns(result, p["expected_cols"])
        row_count = result.get("row_count", -1)

        if missing:
            print(f"  ⚠ {label}")
            print(f"      schema: columns missing from output: {missing}")
            warnings.append({**p, "issue": f"missing columns: {missing}"})
        elif row_count == 0:
            cause = _zero_row_cause(p["query"])
            print(f"  ⚠ {label}")
            print(f"      zero rows — {cause}")
            warnings.append({**p, "issue": f"zero rows: {cause}"})
        else:
            passed += 1
            print(f"  ✓ {label}")

    # --- Lens unsupported summary ------------------------------------------
    if lens_unsupported:
        print(f"\n  [LENS UNSUPPORTED] {len(lens_unsupported)} panels could not be reconstructed:")
        for lp in lens_unsupported:
            print(f"    [{lp['slug']}] {lp['dashboard']} / {lp['panel']}")
            print(f"      {lp.get('unsupported_reason', 'unknown')}")

    # --- Summary ------------------------------------------------------------
    print()
    print("=" * 70)
    print(f"RESULTS: {passed}/{total_panels} panels passed")
    print(f"FAILED:  {len(failed)}")
    print(f"WARN:    {len(warnings)}  (schema / zero-row)")
    print(f"STATIC:  {len(static_warn)}  (structural issues detected before ES call)")
    print(f"LENS:    {len(lens_validated)} validated, {len(lens_unsupported)} unsupported")
    print("=" * 70)

    if failed:
        print("\nFAILED PANELS:")
        for fp in failed:
            print(f"  [{fp['slug']}] {fp['dashboard']} / {fp['panel']}")
            print(f"    {fp['error'][:120]}")

    if warnings:
        print("\nWARNINGS (zero-row / missing cols):")
        for w in warnings:
            print(f"  [{w['slug']}] {w['dashboard']} / {w['panel']}")
            print(f"    {w['issue']}")

    if static_warn:
        print("\nSTATIC STRUCTURAL ISSUES:")
        for sw in static_warn:
            print(f"  [{sw['slug']}] {sw['dashboard']} / {sw['panel']}")
            for si in sw["static_issues"]:
                print(f"    {si}")

    out_path = os.path.join(_E2E_ROOT, "panel_validation_yaml.json")
    with open(out_path, "w") as f:
        json.dump({
            "total": total_panels,
            "passed": passed,
            "failed": failed,
            "warnings": warnings,
            "static_warnings": static_warn,
            "lens_skipped": lens_unsupported,
            "lens_validated": len(lens_validated),
        }, f, indent=2)
    print(f"\nFull results: {out_path}")
    return len(failed)


if __name__ == "__main__":
    raise SystemExit(main())
