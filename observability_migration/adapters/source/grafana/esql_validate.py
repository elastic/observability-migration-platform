# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""ES|QL validation, auto-fixes, and rule-pack suggestion helpers."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import requests
import yaml

from observability_migration.core.http import apply_tls

from .rules import _append_unique, _merge_mapping_lists

DEFAULT_TSTART_EXPR = "NOW() - 1 hour"
DEFAULT_TEND_EXPR = "NOW()"
_QUERY_PARAM_RE = re.compile(r"\?([A-Za-z_][A-Za-z0-9_]*)")
KNOWN_FIELD_ALIASES = {
    "node_interrupts_total": "node_intr_total",
}


def validate_esql(query, es_url, index_pattern="metrics-*", es_api_key=None, verify: bool | str = True):
    """Validate an ES|QL query against Elasticsearch. Returns (ok, error_message)."""
    probe = _run_esql_query(query, es_url, es_api_key=es_api_key, verify=verify)
    return probe["ok"], probe["error"]


def materialize_dashboard_time_query(
    query,
    time_from=DEFAULT_TSTART_EXPR,
    time_to=DEFAULT_TEND_EXPR,
):
    if not query:
        return query
    rendered = query
    rendered = re.sub(
        r"\bTRANGE\([^)]+\)",
        lambda _: f"@timestamp >= {time_from} AND @timestamp < {time_to}",
        rendered,
    )
    rendered = rendered.replace("?_tstart", time_from)
    rendered = rendered.replace("?_tend", time_to)
    return rendered


def _build_es_headers(es_api_key=None):
    """Build HTTP headers for Elasticsearch requests, including auth when available."""
    headers = {"Content-Type": "application/json"}
    if es_api_key:
        headers["Authorization"] = f"ApiKey {es_api_key}"
    return headers


def _validation_params_for_query(query):
    rendered = materialize_dashboard_time_query(query)
    names = []
    for name in _QUERY_PARAM_RE.findall(rendered or ""):
        if name not in names:
            names.append(name)
    return [{name: _validation_param_value(rendered, name)} for name in names]


def _validation_param_value(query, name):
    param = re.escape(name)
    if re.search(rf"\bRLIKE\s+\?{param}\b", query or "", re.IGNORECASE):
        return ".*"
    if re.search(rf"(?:[+\-*/]\s*\?{param}\b|\?{param}\b\s*[+\-*/])", query or ""):
        return 0
    return ".*"


def _has_dashboard_params(query):
    rendered = materialize_dashboard_time_query(query)
    return bool(_QUERY_PARAM_RE.search(rendered or ""))


def _limit_query_for_validation(query, result_limit):
    if result_limit is None:
        return query
    try:
        limit = int(result_limit)
    except (TypeError, ValueError):
        return query
    if limit < 0:
        return query
    text = str(query or "").rstrip()
    if re.search(r"\|\s*LIMIT\s+\d+\s*$", text, re.IGNORECASE):
        return re.sub(r"(\|\s*LIMIT\s+)\d+\s*$", rf"\g<1>{limit}", text, flags=re.IGNORECASE)
    return f"{text}\n| LIMIT {limit}"


_module_es_api_key = None


def configure_es_auth(es_api_key):
    """Set module-level ES API key used by all validation and schema requests."""
    global _module_es_api_key
    _module_es_api_key = es_api_key


def _run_esql_query(
    query,
    es_url,
    es_api_key=None,
    session=None,
    timeout=15,
    result_limit=None,
    verify: bool | str = True,
):
    """Execute ES|QL and return validation status plus lightweight result metadata."""
    if not es_url or not query:
        return {"ok": None, "error": "", "rows": 0, "columns": [], "values": [], "metadata": {}}
    query = materialize_dashboard_time_query(query)
    query = _limit_query_for_validation(query, result_limit)
    api_key = es_api_key or _module_es_api_key
    try:
        client = session or requests
        payload = {"query": query}
        params = _validation_params_for_query(query)
        if params:
            payload["params"] = params
        post_kwargs = {
            "json": payload,
            "params": {"format": "json"},
            "headers": _build_es_headers(api_key),
            "timeout": timeout,
        }
        if session is None:
            post_kwargs["verify"] = verify
        resp = client.post(
            f"{es_url}/_query",
            **post_kwargs,
        )
        if resp.status_code == 200:
            body = resp.json()
            values = list(body.get("values", []) or [])
            return {
                "ok": True,
                "error": "",
                "rows": len(values),
                "columns": [column.get("name", "") for column in body.get("columns", [])],
                "values": values[:10],
                "metadata": {
                    "sampled_rows": min(len(values), 10),
                    "truncated": len(values) > 10,
                    "result_limit": result_limit,
                },
            }
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        reason = ""
        if "error" in body:
            err = body["error"]
            if isinstance(err, dict):
                reason = err.get("reason", "") or str(err.get("caused_by", {}).get("reason", ""))
            else:
                reason = str(err)
        return {
            "ok": False,
            "error": reason or f"HTTP {resp.status_code}",
            "rows": 0,
            "columns": [],
            "values": [],
            "metadata": {},
        }
    except Exception as exc:
        return {"ok": None, "error": str(exc), "rows": 0, "columns": [], "values": [], "metadata": {}}


def _query_source_and_index(query):
    if not query:
        return "", ""
    first_line = next((line.strip() for line in query.splitlines() if line.strip()), "")
    match = re.match(r"^(FROM|TS)\s+(\S+)", first_line)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def _query_runtime_metadata(query):
    rendered = materialize_dashboard_time_query(query)
    _, target_index = _query_source_and_index(query)
    return {
        "materialized_query": rendered,
        "target_index": target_index,
        "sample_window": {
            "mode": "dashboard_time",
            "time_from": DEFAULT_TSTART_EXPR,
            "time_to": DEFAULT_TEND_EXPR,
            "contains_time_placeholders": rendered != (query or ""),
        },
    }


def _format_esql_interval(total_seconds):
    units = (
        ("week", 7 * 24 * 60 * 60),
        ("day", 24 * 60 * 60),
        ("hour", 60 * 60),
        ("minute", 60),
        ("second", 1),
    )
    for unit_name, unit_seconds in units:
        if total_seconds % unit_seconds == 0:
            value = total_seconds // unit_seconds
            suffix = "" if value == 1 else "s"
            return f"{value} {unit_name}{suffix}"
    return ""


def _promql_window_to_esql_interval(window):
    cleaned = (window or "").strip().replace(" ", "")
    if not cleaned:
        return ""
    matches = re.findall(r"(\d+)(ms|s|m|h|d|w)", cleaned)
    if not matches or "".join(f"{value}{unit}" for value, unit in matches) != cleaned:
        return ""
    if len(matches) == 1 and matches[0][1] == "ms":
        value = int(matches[0][0])
        suffix = "" if value == 1 else "s"
        return f"{value} millisecond{suffix}"
    total_seconds = 0
    multipliers = {
        "s": 1,
        "m": 60,
        "h": 60 * 60,
        "d": 24 * 60 * 60,
        "w": 7 * 24 * 60 * 60,
    }
    for value, unit in matches:
        if unit == "ms":
            return ""
        total_seconds += int(value) * multipliers[unit]
    return _format_esql_interval(total_seconds)


_NARROW_PROBE_TIMEOUT = 3
_NARROW_MAX_CANDIDATES = 10


def _try_narrow_index_pattern(
    query, es_url, resolver, es_api_key=None, session=None,
    max_candidates=_NARROW_MAX_CANDIDATES, probe_timeout=_NARROW_PROBE_TIMEOUT,
    result_limit=None,
):
    if not resolver or not es_url or not query:
        return None
    if not hasattr(resolver, "concrete_index_candidates"):
        return None
    source_cmd, current_index = _query_source_and_index(query)
    if not source_cmd or not current_index:
        return None
    if current_index != getattr(resolver, "_index_pattern", current_index):
        return None
    if not any(token in current_index for token in ("*", "?", ",")):
        return None
    best_empty = None
    candidates = resolver.concrete_index_candidates()
    if max_candidates and max_candidates > 0:
        candidates = candidates[:max_candidates]
    for candidate in candidates:
        if not candidate or candidate == current_index:
            continue
        narrowed = re.sub(r"^(FROM|TS)\s+\S+", rf"\1 {candidate}", query, count=1)
        run_kwargs = {
            "es_api_key": es_api_key,
            "session": session,
            "timeout": probe_timeout,
        }
        if result_limit is not None:
            run_kwargs["result_limit"] = result_limit
        probe = _run_esql_query(narrowed, es_url, **run_kwargs)
        if probe["ok"] is True and probe["rows"] > 0:
            return {
                "query": narrowed,
                "rows": probe["rows"],
                "columns": probe["columns"],
            }
        if probe["ok"] is True and best_empty is None:
            best_empty = {
                "query": narrowed,
                "rows": probe["rows"],
                "columns": probe["columns"],
            }
    return best_empty


def _classify_unknown_column(query, column):
    column_re = re.escape(column)
    metric_pattern = (
        r"\b(?:AVG|SUM|COUNT|MAX|MIN|RATE|IRATE|INCREASE|DELTA|DERIV|"
        r"AVG_OVER_TIME|SUM_OVER_TIME|MAX_OVER_TIME|MIN_OVER_TIME|"
        r"COUNT_OVER_TIME|COUNT_DISTINCT|PERCENTILE_OVER_TIME)\(\s*" + column_re + r"(?:\b|[,)])"
    )
    label_patterns = [
        r"\|\s*WHERE[^\n]*\b" + column_re + r"\b",
        r"\bBY\s+[^\n]*\b" + column_re + r"\b",
        r"\|\s*KEEP[^\n]*\b" + column_re + r"\b",
    ]
    if re.search(metric_pattern, query):
        return "metric"
    if any(re.search(pattern, query) for pattern in label_patterns):
        return "label"
    return "unknown"


def _candidate_fields_from_error(column, suggested_fields, resolver):
    candidates = []
    if resolver:
        for field_name in resolver._candidate_fields(column):
            if resolver.field_exists(field_name):
                _append_unique(candidates, field_name)
    for field_name in suggested_fields:
        if not resolver or resolver.field_exists(field_name):
            _append_unique(candidates, field_name)
    return candidates


def _replace_exact_field(query, old_field, new_field):
    if not query or not old_field or not new_field or old_field == new_field:
        return query
    pattern = rf"(?<![A-Za-z0-9_.]){re.escape(old_field)}(?![A-Za-z0-9_.])"
    return re.sub(pattern, new_field, query)


def analyze_validation_error(query, error_msg, resolver=None):
    unknown_columns = []
    for match in re.finditer(
        r"Unknown column \[([^\]]+)\](?:, did you mean(?: any of)? \[([^\]]+)\])?",
        error_msg,
    ):
        column = match.group(1)
        suggested = []
        if match.group(2):
            suggested = [item.strip() for item in match.group(2).split(",") if item.strip()]
        unknown_columns.append(
            {
                "name": column,
                "role": _classify_unknown_column(query, column),
                "suggested_fields": _candidate_fields_from_error(column, suggested, resolver),
            }
        )

    unknown_indexes = re.findall(r"Unknown index \[([^\]]+)\]", error_msg)
    counter_mismatch_metrics = [
        metric.strip()
        for metric in re.findall(
            r"first argument of \[(?:RATE|IRATE|INCREASE|DELTA)\(([^,]+)",
            error_msg,
        )
    ]
    return {
        "unknown_columns": unknown_columns,
        "unknown_indexes": unknown_indexes,
        "counter_mismatch_metrics": counter_mismatch_metrics,
        "raw_error": error_msg,
    }


def summarize_validation_records(records):
    summary = {
        "counts": Counter(),
        "missing_metrics": Counter(),
        "missing_labels": Counter(),
        "missing_indexes": Counter(),
        "counter_type_mismatches": Counter(),
        "empty_fallback_indexes": Counter(),
        "other_errors": Counter(),
        "suggested_label_candidates": {},
    }

    for record in records:
        summary["counts"][record["status"]] += 1
        analysis = record.get("analysis") or {}

        for entry in analysis.get("unknown_columns", []):
            if entry["role"] == "label":
                summary["missing_labels"][entry["name"]] += 1
                _merge_mapping_lists(
                    summary["suggested_label_candidates"],
                    {entry["name"]: entry.get("suggested_fields", [])},
                )
            elif entry["role"] == "metric":
                summary["missing_metrics"][entry["name"]] += 1
            else:
                summary["other_errors"][f"unknown_column:{entry['name']}"] += 1

        for index_name in analysis.get("unknown_indexes", []):
            summary["missing_indexes"][index_name] += 1

        for metric_name in analysis.get("counter_mismatch_metrics", []):
            summary["counter_type_mismatches"][metric_name] += 1

        if record["status"] == "fixed_empty":
            narrowed_to = analysis.get("narrowed_to_index", "")
            if narrowed_to:
                summary["empty_fallback_indexes"][narrowed_to] += 1

        if (
            record["status"] == "fail"
            and not analysis.get("unknown_columns")
            and not analysis.get("unknown_indexes")
            and not analysis.get("counter_mismatch_metrics")
        ):
            first_line = (analysis.get("raw_error") or "").splitlines()[0][:200]
            summary["other_errors"][first_line or "unknown_error"] += 1

    return {
        "counts": dict(summary["counts"]),
        "missing_metrics": dict(summary["missing_metrics"].most_common()),
        "missing_labels": dict(summary["missing_labels"].most_common()),
        "missing_indexes": dict(summary["missing_indexes"].most_common()),
        "counter_type_mismatches": dict(summary["counter_type_mismatches"].most_common()),
        "empty_fallback_indexes": dict(summary["empty_fallback_indexes"].most_common()),
        "other_errors": dict(summary["other_errors"].most_common()),
        "suggested_label_candidates": summary["suggested_label_candidates"],
    }


def build_suggested_rule_pack(validation_summary):
    suggested = {
        label: candidates
        for label, candidates in (validation_summary.get("suggested_label_candidates", {}) or {}).items()
        if candidates
    }
    unresolved_labels = {
        label: count
        for label, count in (validation_summary.get("missing_labels", {}) or {}).items()
        if label not in suggested
    }
    return {
        "_generated": {
            "note": "Generated from live ES|QL validation failures. Review before using in production.",
            "purpose": "Environment-specific schema candidates for PromQL label resolution",
        },
        "schema": {
            "label_candidates": suggested,
        },
        "_validation_hints": {
            "missing_indexes": validation_summary.get("missing_indexes", {}),
            "missing_labels": validation_summary.get("missing_labels", {}),
            "unresolved_labels": unresolved_labels,
            "missing_metrics_sample": dict(list(validation_summary.get("missing_metrics", {}).items())[:25]),
        },
    }


def write_suggested_rule_pack(path, validation_summary):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        yaml.safe_dump(build_suggested_rule_pack(validation_summary), fh, sort_keys=False)


def validate_query_with_fixes(
    query, es_url, resolver, max_attempts=8, es_api_key=None,
    narrow_limit=_NARROW_MAX_CANDIDATES, narrow_timeout=_NARROW_PROBE_TIMEOUT,
    result_limit=None,
    verify: bool | str = True,
):
    original_query = query
    _, original_index = _query_source_and_index(query)
    current_query = query
    seen_queries = {current_query}
    fix_errors = []
    original_error = ""
    original_analysis = {}
    api_key = es_api_key or _module_es_api_key
    session = requests.Session()
    apply_tls(session, verify)
    # Track the last index pattern for which narrow probing was attempted so we
    # don't re-probe the same wildcard pattern on every field-fix iteration.
    _narrow_probing_done_for: set[str] = set()

    for _ in range(max_attempts + 1):
        run_kwargs = {"es_api_key": api_key, "session": session}
        if result_limit is not None:
            run_kwargs["result_limit"] = result_limit
        probe = _run_esql_query(current_query, es_url, **run_kwargs)
        ok = probe["ok"]
        err = probe["error"]
        if ok is True:
            _, current_index = _query_source_and_index(current_query)
            narrowed = bool(fix_errors and original_index and current_index and current_index != original_index)
            param_dependent_rows = _has_dashboard_params(current_query)
            analysis = {
                **_query_runtime_metadata(current_query),
                "result_rows": probe["rows"],
                "result_columns": probe["columns"],
                "result_values": list(probe.get("values", []) or []),
                "result_metadata": dict(probe.get("metadata", {}) or {}),
                "param_dependent_rows": param_dependent_rows,
            }
            if narrowed:
                analysis["narrowed_from_index"] = original_index
                analysis["narrowed_to_index"] = current_index
            return {
                "status": (
                    "fixed_empty"
                    if narrowed and probe["rows"] == 0 and not param_dependent_rows
                    else "pass"
                    if not fix_errors
                    else "fixed"
                ),
                "query": current_query,
                "error": "",
                "analysis": analysis,
                "fix_attempts": list(fix_errors),
            }
        if ok is None:
            analysis = {
                **_query_runtime_metadata(current_query),
                "raw_error": err,
            }
            return {
                "status": "skip",
                "query": current_query,
                "error": err,
                "analysis": analysis,
                "fix_attempts": list(fix_errors),
            }

        analysis = analyze_validation_error(current_query, err, resolver)
        analysis.update(_query_runtime_metadata(current_query))
        if not original_error:
            original_error = err
            original_analysis = analysis

        # Only attempt narrow-index probing when:
        #   (a) the error contains unknown index references (probing can find a better index), AND
        #   (b) we have not already probed this index pattern (avoids 30 s re-spend per field-fix retry).
        # Pure "Unknown column" errors are field-mapping issues, not index issues — probing
        # different index candidates won't surface the missing field any faster.
        _, probe_index = _query_source_and_index(current_query)
        has_index_error = bool(analysis.get("unknown_indexes"))
        only_column_errors = bool(analysis.get("unknown_columns")) and not has_index_error
        narrowed_query = None
        if not only_column_errors and probe_index not in _narrow_probing_done_for:
            _narrow_probing_done_for.add(probe_index or "")
            narrowed_query = _try_narrow_index_pattern(
                current_query,
                es_url,
                resolver,
                es_api_key=api_key,
                session=session,
                max_candidates=narrow_limit,
                probe_timeout=narrow_timeout,
                result_limit=result_limit,
            )
        if narrowed_query and narrowed_query["query"] not in seen_queries:
            fix_errors.append(err)
            seen_queries.add(narrowed_query["query"])
            current_query = narrowed_query["query"]
            continue
        fixed_query = _try_fix_esql_field_error(current_query, err, resolver)
        if not fixed_query or fixed_query == current_query or fixed_query in seen_queries:
            return {
                "status": "fail",
                "query": original_query if fix_errors else current_query,
                "error": original_error or err,
                "analysis": original_analysis or analysis,
                "fix_attempts": list(fix_errors),
            }
        fix_errors.append(err)
        seen_queries.add(fixed_query)
        current_query = fixed_query

    final_query = original_query if fix_errors else current_query
    final_analysis = original_analysis or analyze_validation_error(final_query, fix_errors[-1] if fix_errors else "", resolver)
    final_analysis.update(_query_runtime_metadata(final_query))
    return {
        "status": "fail",
        "query": original_query,
        "error": original_error or (fix_errors[-1] if fix_errors else ""),
        "analysis": final_analysis,
        "fix_attempts": list(fix_errors),
    }


def _try_fix_esql_field_error(query, error_msg, resolver):
    """Attempt to fix common ES|QL field errors by column-name substitution or type fallback."""
    if "count_star [COUNT(*)] can't be used with TS command" in error_msg:
        match = re.search(
            r"\|\s*WHERE\s+([A-Za-z_][A-Za-z0-9_.]*)\s+(?:IS\s+NOT\s+NULL|==|!=|>=|<=|>|<)",
            query,
            re.IGNORECASE,
        )
        if match:
            metric = match.group(1)
            fixed = re.sub(r"\bCOUNT\(\*\)", f"COUNT({metric})", query, count=1)
            if fixed != query:
                return fixed

    analysis = analyze_validation_error(query, error_msg, resolver)
    for entry in analysis.get("unknown_columns", []):
        bad_field = entry["name"]
        candidates = []
        alias_field = KNOWN_FIELD_ALIASES.get(bad_field, "")
        if alias_field and ((not resolver) or resolver.field_exists(alias_field)):
            _append_unique(candidates, alias_field)
        if resolver and bad_field.startswith("otelcol_exporter_enqueue_failed_"):
            renamed = bad_field.replace("otelcol_exporter_enqueue_failed_", "otelcol_exporter_send_failed_", 1)
            if resolver.field_exists(renamed):
                _append_unique(candidates, renamed)
        resolved = resolver.resolve_label(bad_field) if resolver else ""
        if resolved and resolved != bad_field:
            _append_unique(candidates, resolved)
        for suggested in entry.get("suggested_fields", []) or []:
            if suggested and suggested != bad_field:
                _append_unique(candidates, suggested)
        for candidate in candidates:
            fixed = _replace_exact_field(query, bad_field, candidate)
            if fixed != query:
                return fixed

    unsupported_window = re.search(
        r"Unsupported window \[([^\]]+)\].*time bucket \[TBUCKET\([^)]+\)\]",
        error_msg,
    )
    if unsupported_window:
        interval = _promql_window_to_esql_interval(unsupported_window.group(1))
        if interval:
            fixed = re.sub(r"TBUCKET\([^)]+\)", f"TBUCKET({interval})", query)
            if fixed != query:
                return fixed

    counter_mismatch = re.search(r"first argument of \[(?:RATE|IRATE|INCREASE|DELTA)\(([^,]+)", error_msg)
    if counter_mismatch:
        metric = counter_mismatch.group(1).strip()
        fixed = re.sub(r"^TS\b", "FROM", query, count=1)
        fixed = re.sub(
            r"@timestamp\s*>=\s*NOW\(\)\s*-\s*1 hour(?:\s*AND\s*@timestamp\s*<\s*NOW\(\))?",
            "@timestamp >= ?_tstart AND @timestamp <= ?_tend",
            fixed,
        )
        fixed = re.sub(
            r"\bTRANGE\([^)]+\)",
            "@timestamp >= ?_tstart AND @timestamp <= ?_tend",
            fixed,
        )
        fixed = re.sub(r"TBUCKET\([^)]+\)", "BUCKET(@timestamp, 50, ?_tstart, ?_tend)", fixed)
        fixed = re.sub(
            r"BUCKET\(@timestamp,\s*\d+\s*,\s*NOW\(\)\s*-\s*1 hour\s*,\s*NOW\(\)\)",
            "BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
            fixed,
        )
        rate_in_agg = re.compile(r"(\w+)\((?:RATE|IRATE|INCREASE|DELTA)\(" + re.escape(metric) + r"[^)]*\)\)")
        if rate_in_agg.search(fixed):
            fixed = rate_in_agg.sub(r"\1(" + metric + ")", fixed)
        else:
            fixed = re.sub(
                r"(?:RATE|IRATE|INCREASE|DELTA)\(" + re.escape(metric) + r"[^)]*\)",
                f"AVG({metric})",
                fixed,
            )
        if fixed != query:
            return fixed

    return None


__all__ = [
    "_query_source_and_index",
    "_run_esql_query",
    "analyze_validation_error",
    "build_suggested_rule_pack",
    "materialize_dashboard_time_query",
    "summarize_validation_records",
    "validate_esql",
    "validate_query_with_fixes",
    "write_suggested_rule_pack",
]
