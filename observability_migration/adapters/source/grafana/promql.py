# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""PromQL fragment parsing and ES|QL planning helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .rules import RulePackConfig


def _is_counter_fallback(metric_name, rule_pack):
    """Heuristic counter detection when no schema resolver is available."""
    if not metric_name:
        return False
    suffixes = getattr(rule_pack, "counter_suffixes", ["_total"])
    return any(metric_name.endswith(s) for s in suffixes)

try:
    import promql_parser  # pyright: ignore[reportMissingImports]
except ImportError:
    promql_parser = None  # Checked at parse time; raises ImportError with install instructions

AGG_FUNCTION_MAP = {
    "rate": "RATE",
    "irate": "IRATE",
    "increase": "INCREASE",
    "avg_over_time": "AVG_OVER_TIME",
    "sum_over_time": "SUM_OVER_TIME",
    "max_over_time": "MAX_OVER_TIME",
    "min_over_time": "MIN_OVER_TIME",
    "count_over_time": "COUNT_OVER_TIME",
    "delta": "DELTA",
    "deriv": "DERIV",
    "histogram_quantile": "PERCENTILE_OVER_TIME",
}

OUTER_AGG_MAP = {
    "sum": "SUM",
    "avg": "AVG",
    "max": "MAX",
    "min": "MIN",
    "count": "COUNT",
    "stddev": "STDDEV",
}

SUPPORTED_RANGE_FUNCTIONS = {
    "avg_over_time",
    "count_over_time",
    "delta",
    "deriv",
    "increase",
    "irate",
    "max_over_time",
    "min_over_time",
    "rate",
    "sum_over_time",
}

HARD_UNSUPPORTED_AST_REASONS = {
    "__name__": "PromQL metric-name introspection via __name__ requires manual redesign",
    "offset": "Contains unsupported pattern: offset",
    "subquery": "Contains unsupported pattern: subquery",
    "without": "PromQL without aggregation requires manual redesign",
}

HARD_UNSUPPORTED_CALL_REASONS = {
    "absent": "absent() checks metric existence and has no ES|QL equivalent",
    "absent_over_time": "absent_over_time() checks metric existence and has no ES|QL equivalent",
    "bottomk": "bottomk requires manual redesign",
    "changes": "changes() counts value transitions and has no ES|QL equivalent",
    "count_values": "count_values requires manual redesign",
    "histogram_quantile": "histogram_quantile over Prometheus bucket series requires manual redesign",
    "label_join": "label_join requires manual redesign",
    "quantile": "quantile requires manual redesign",
    "resets": "resets() counts counter resets and has no ES|QL equivalent",
    "timestamp": "timestamp() returns sample timestamps and has no ES|QL equivalent",
    "topk": "topk requires manual redesign",
}


@dataclass
class PromQLFragment:
    """Intermediate representation of a parsed PromQL (sub-)expression."""

    metric: str = ""
    matchers: list = field(default_factory=list)
    range_func: str = ""
    range_window: str = ""
    outer_agg: str = ""
    group_labels: list = field(default_factory=list)
    group_mode: str = "by"
    binary_op: str = ""
    binary_rhs: Any = None
    scalar_value: float | None = None
    is_scalar: bool = False
    is_time_call: bool = False
    raw_expr: str = ""
    family: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class MeasureSpec:
    source_type: str
    time_filter: str
    bucket_expr: str
    group_fields: list
    filters: list
    alias: str
    stats_expr: str
    final_alias: str
    eval_expr: str = ""
    metric_name: str = ""
    warnings: list = field(default_factory=list)


@dataclass
class FormulaPlan:
    specs: list
    expr: str
    warnings: list = field(default_factory=list)


def preprocess_grafana_macros(expr, rule_pack=None):
    """Replace Grafana-specific macros with valid PromQL placeholders."""
    default_window = (rule_pack.default_rate_window if rule_pack else "5m") or "5m"
    replacements = [
        (r"\$__rate_interval", "5m"),
        (r"\$__interval", "5m"),
        (r"\$__range", "1h"),
        (r"\$interval", "5m"),
        (r"\[\$__interval\]", "[5m]"),
        (r"\[\$__rate_interval\]", "[5m]"),
        (r"\[\$__range\]", "[1h]"),
        (r"\[\$interval\]", "[5m]"),
        (r"\$__auto_interval_\w+", "5m"),
    ]
    result = expr
    for pattern, replacement in replacements:
        result = re.sub(pattern, replacement, result)
    result = re.sub(r"\[\s*\$(?!__)([A-Za-z_][A-Za-z0-9_]*)\s*\]", f"[{default_window}]", result)
    result = re.sub(r"\[\s*label_([A-Za-z_][A-Za-z0-9_]*)\s*\]", f"[{default_window}]", result)

    result = re.sub(r'\{([^}]*?)(\w+)=~"\$(\w+)"([^}]*?)\}', r'{\1\2=~".*"\4}', result)
    result = re.sub(r'\{([^}]*?)(\w+)="\$(\w+)"([^}]*?)\}', r'{\1\2=~".*"\4}', result)
    result = re.sub(r"\$(\w+)", lambda m: m.group(0) if m.group(1).startswith("__") else f"label_{m.group(1)}", result)
    return result


def classify_promql_complexity(expr, rule_pack=None):
    """Classify a PromQL expression's translation complexity."""
    rule_pack = rule_pack or RulePackConfig()
    for rule in rule_pack.not_feasible_patterns:
        if re.search(rule.pattern, expr, re.IGNORECASE):
            return "not_feasible", rule.reason
    for rule in rule_pack.warning_patterns:
        if re.search(rule.pattern, expr, re.IGNORECASE):
            return "warning", rule.reason
    return "feasible", ""


def _normalize_range_window(seconds):
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _detect_outer_agg(expr):
    agg_pattern = "|".join(re.escape(agg) for agg in OUTER_AGG_MAP)
    match = re.match(rf"^\s*(?P<agg>{agg_pattern})\b", expr, re.IGNORECASE)
    if match:
        return match.group("agg").lower()
    return None


def _trim_outer_parens(expr):
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        balanced = True
        for idx, char in enumerate(expr):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and idx != len(expr) - 1:
                    balanced = False
                    break
        if not balanced or depth != 0:
            break
        expr = expr[1:-1].strip()
    return expr


MAX_ALIAS_LENGTH = 128

_GRAFANA_TEMPLATE_SUB_RE = re.compile(r"\{\{[^}]*\}\}")
_UNDERSCORE_RUN_RE = re.compile(r"_+")


def _strip_grafana_substitutions(text):
    """Drop ``{{label}}`` placeholders so internal aliases stay readable.

    Grafana legend formats embed runtime label substitutions like
    ``{{instance}}``.  Those values are emitted as separate ES|QL columns,
    so they do not need to leak into the synthetic alias used as a column
    name.  Stripping them upstream avoids ``on____instance`` artefacts.
    """
    if not text:
        return text
    cleaned = _GRAFANA_TEMPLATE_SUB_RE.sub("", str(text))
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _sanitize_alias_token(raw):
    if not raw:
        return ""
    token = re.sub(r"[^a-zA-Z0-9_]", "_", str(raw))
    token = _UNDERSCORE_RUN_RE.sub("_", token)
    return token.strip("_")


def _truncate_alias_at_word_boundary(alias, limit):
    if len(alias) <= limit:
        return alias
    truncated = alias[:limit]
    if "_" in truncated:
        head, _ = truncated.rsplit("_", 1)
        head = head.rstrip("_")
        if head:
            return head
    return truncated.rstrip("_") or alias[:limit].rstrip("_")


def _safe_alias(raw, suffix=""):
    alias = _sanitize_alias_token(raw) or "value"
    if alias and alias[0].isdigit():
        alias = f"series_{alias}"
    safe_suffix = _sanitize_alias_token(suffix)
    if safe_suffix:
        alias = f"{alias}_{safe_suffix}"
    if len(alias) > MAX_ALIAS_LENGTH:
        alias = _truncate_alias_at_word_boundary(alias, MAX_ALIAS_LENGTH)
    return alias


def _unique_safe_alias(raw, used_aliases, fallback_suffix=""):
    seed = _strip_grafana_substitutions(raw) or raw
    alias = _safe_alias(seed)
    if alias not in used_aliases:
        used_aliases.add(alias)
        return alias
    alias = _safe_alias(seed, fallback_suffix)
    if alias not in used_aliases:
        used_aliases.add(alias)
        return alias
    base = _safe_alias(seed)
    counter = 2
    candidate = f"{base}_{counter}"
    while candidate in used_aliases:
        counter += 1
        candidate = f"{base}_{counter}"
    used_aliases.add(candidate)
    return candidate


def _format_scalar_value(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _quote_esql_string(value):
    return json.dumps(value)


def _split_top_level_csv(expr):
    parts = []
    current = []
    depth = 0
    in_quote = None
    for char in expr:
        if in_quote:
            current.append(char)
            if char == in_quote:
                in_quote = None
            continue
        if char in ('"', "'"):
            in_quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(depth - 1, 0)
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _parse_selector_matchers(selector_text):
    matchers = []
    for part in _split_top_level_csv(selector_text or ""):
        match = re.match(r'\s*([A-Za-z_][A-Za-z0-9_\.:-]*)\s*(=~|!~|=|!=)\s*([\'"])(.*?)\3\s*$', part)
        if not match:
            continue
        matchers.append(
            {
                "label": match.group(1),
                "op": match.group(2),
                "value": match.group(4),
            }
        )
    return matchers


def _matcher_to_esql(matcher, resolver):
    label = resolver.resolve_label(matcher["label"]) if resolver else matcher["label"]
    op = matcher["op"]
    value = matcher["value"]
    if not label:
        return None
    if "$" in value or value.startswith("label_"):
        return None
    if op == "=":
        return f"{label} == {_quote_esql_string(value)}"
    if op == "!=":
        return f"{label} != {_quote_esql_string(value)}"
    if op == "=~":
        if value in (".*", ".+", ""):
            return None
        return f"{label} RLIKE {_quote_esql_string(value)}"
    if op == "!~":
        if value in (".*", ".+", ""):
            return None
        return f"NOT ({label} RLIKE {_quote_esql_string(value)})"
    return None


def _common_matchers(left_matchers, right_matchers):
    right_lookup = {(m["label"], m["op"], m["value"]) for m in right_matchers}
    return [m for m in left_matchers if (m["label"], m["op"], m["value"]) in right_lookup]


def _build_where_lines(filters):
    return [f"| WHERE {flt}" for flt in filters if flt]


def _selector_filters(matchers, resolver):
    filters = []
    for matcher in matchers:
        filter_expr = _matcher_to_esql(matcher, resolver)
        if filter_expr:
            filters.append(filter_expr)
    return filters


def _parse_logql_selector(expr):
    match = re.search(r"\{(?P<selectors>[^}]*)\}", expr)
    if not match:
        return [], []
    selector_text = match.group("selectors")
    matchers = _parse_selector_matchers(selector_text)
    fields = [matcher["label"] for matcher in matchers]
    return matchers, fields


def _parse_logql_search(expr):
    match = re.search(r'\|\s*(?:~|=)\s*"([^"]*)"', expr)
    if not match:
        return ""
    return match.group(1)


def _build_log_message_filter(search_expr, rule_pack):
    if not search_expr or search_expr.startswith("$") or search_expr.startswith("label_"):
        return None
    if re.fullmatch(r"[A-Za-z0-9_\-\. ]+", search_expr):
        return f'{rule_pack.logs_message_field} LIKE {_quote_esql_string(f"*{search_expr}*")}'
    if not search_expr.startswith(".*"):
        search_expr = f".*{search_expr}"
    if not search_expr.endswith(".*"):
        search_expr = f"{search_expr}.*"
    return f"{rule_pack.logs_message_field} RLIKE {_quote_esql_string(search_expr)}"


def _extract_group_labels(expr):
    match = re.search(r"\b(?:by|without)\s*\(([^)]+)\)", expr, re.IGNORECASE)
    if not match:
        return []
    return [label.strip() for label in match.group(1).split(",") if label.strip()]


def _ast_node_expr(node):
    prettify = getattr(node, "prettify", None)
    if callable(prettify):
        try:
            return prettify()
        except Exception:
            pass
    return str(node)


def _ast_enum_name(value):
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if name:
        return str(name)
    rendered = str(value)
    if "." in rendered:
        rendered = rendered.split(".")[-1]
    return rendered


def _duration_to_promql(delta: timedelta | None):
    if delta is None:
        return ""
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "0s"

    parts = []
    remaining = total_seconds
    for unit_seconds, suffix in (
        (7 * 24 * 3600, "w"),
        (24 * 3600, "d"),
        (3600, "h"),
        (60, "m"),
        (1, "s"),
    ):
        count, remaining = divmod(remaining, unit_seconds)
        if count:
            parts.append(f"{count}{suffix}")
    return "".join(parts) or "0s"


def _new_fragment(expr, family="unknown", backend="ast"):
    return PromQLFragment(raw_expr=expr, family=family, extra={"parser_backend": backend})


def _append_not_feasible_reason(frag, reason):
    if not reason:
        return
    reasons = frag.extra.setdefault("not_feasible_reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _merge_not_feasible_reasons(target, *children):
    for child in children:
        if not child:
            continue
        for reason in child.extra.get("not_feasible_reasons", []):
            _append_not_feasible_reason(target, reason)


def _copy_fragment_summary(target, source):
    if not source:
        return target

    if not target.metric and source.metric:
        target.metric = source.metric
    if not target.matchers and source.matchers:
        target.matchers = list(source.matchers)
    if not target.range_func and source.range_func:
        target.range_func = source.range_func
    if not target.range_window and source.range_window:
        target.range_window = source.range_window
    if not target.outer_agg and source.outer_agg:
        target.outer_agg = source.outer_agg
    if not target.group_labels and source.group_labels:
        target.group_labels = list(source.group_labels)
    if target.group_mode == "by" and source.group_mode != "by":
        target.group_mode = source.group_mode
    if not target.binary_op and source.binary_op:
        target.binary_op = source.binary_op
    if target.binary_rhs is None and source.binary_rhs is not None:
        target.binary_rhs = source.binary_rhs
    if not target.is_scalar and source.is_scalar:
        target.is_scalar = True
        target.scalar_value = source.scalar_value
    if not target.is_time_call and source.is_time_call:
        target.is_time_call = True

    for key in (
        "call_name",
        "inner_agg",
        "inner_group",
        "join_labels",
        "offset",
        "post_filter",
        "start_matchers",
        "start_metric",
        "vector_matching",
        "wrapped_scalar",
    ):
        if source.extra.get(key) is not None and key not in target.extra:
            target.extra[key] = source.extra[key]

    _merge_not_feasible_reasons(target, source)
    return target


def _iter_fragment_children(frag):
    if not frag:
        return []
    children = []
    child = frag.extra.get("inner_frag")
    if isinstance(child, PromQLFragment):
        children.append(child)
    for key in ("left_frag", "right_frag"):
        child = frag.extra.get(key)
        if isinstance(child, PromQLFragment):
            children.append(child)
    if isinstance(frag.binary_rhs, PromQLFragment):
        children.append(frag.binary_rhs)
    return children


def _find_summary_fragment(frag):
    if not frag:
        return None
    if frag.metric or frag.range_func or frag.extra.get("call_name"):
        return frag
    for child in _iter_fragment_children(frag):
        summary = _find_summary_fragment(child)
        if summary:
            return summary
    return None


def _ast_matchers(matchers_obj):
    parsed = []
    for matcher in list(getattr(matchers_obj, "matchers", []) or []):
        op_name = _ast_enum_name(getattr(matcher, "op", None))
        op = {
            "Equal": "=",
            "NotEqual": "!=",
            "Re": "=~",
            "NotRe": "!~",
        }.get(op_name)
        if not op:
            continue
        parsed.append(
            {
                "label": str(getattr(matcher, "name", "") or ""),
                "op": op,
                "value": str(getattr(matcher, "value", "") or ""),
            }
        )
    return parsed


def _ast_vector_selector_fragment(node, expr):
    frag = _new_fragment(expr, family="simple_metric")
    frag.metric = str(getattr(node, "name", "") or "")
    frag.matchers = _ast_matchers(getattr(node, "matchers", None))

    if any(m["label"] == "__name__" for m in frag.matchers):
        _append_not_feasible_reason(frag, HARD_UNSUPPORTED_AST_REASONS["__name__"])

    offset = getattr(node, "offset", None)
    if offset:
        frag.extra["offset"] = _duration_to_promql(offset)
        _append_not_feasible_reason(frag, HARD_UNSUPPORTED_AST_REASONS["offset"])

    if getattr(node, "at", None):
        _append_not_feasible_reason(frag, "PromQL @ modifiers require manual redesign")

    if not frag.metric:
        frag.family = "unknown"
    if frag.extra.get("not_feasible_reasons"):
        frag.family = "unknown"
    return frag


def _ast_matrix_selector_fragment(node, expr):
    frag = _new_fragment(expr)
    selector_frag = _ast_from_node(node.vector_selector, _ast_node_expr(node.vector_selector))
    _copy_fragment_summary(frag, selector_frag)
    frag.range_window = _duration_to_promql(getattr(node, "range", None))
    return frag


def _ast_call_fragment(node, expr):
    func_name = str(getattr(getattr(node, "func", None), "name", "") or "").lower()
    args = list(getattr(node, "args", []) or [])

    if func_name == "time" and not args:
        frag = _new_fragment(expr, family="scalar")
        frag.is_time_call = True
        return frag

    child_frags = [_ast_from_node(arg, _ast_node_expr(arg)) for arg in args]

    if func_name == "scalar" and len(child_frags) == 1:
        child = child_frags[0]
        if child.metric and not child.extra.get("not_feasible_reasons") and child.family in {
            "binary_expr",
            "nested_agg",
            "range_agg",
            "scaled_agg",
            "simple_agg",
            "simple_metric",
            "uptime",
        }:
            wrapped = _copy_fragment_summary(_new_fragment(expr, family=child.family), child)
            wrapped.binary_rhs = child.binary_rhs
            for key in ("left_frag", "right_frag"):
                if key in child.extra:
                    wrapped.extra[key] = child.extra[key]
            wrapped.extra["wrapped_scalar"] = True
            return wrapped

    if func_name == "topk" and len(child_frags) == 2:
        limit_frag, value_frag = child_frags
        if (
            limit_frag.is_scalar
            and limit_frag.scalar_value is not None
            and value_frag.metric
            and value_frag.group_labels
            and value_frag.group_mode == "by"
            and not value_frag.extra.get("not_feasible_reasons")
        ):
            frag = _copy_fragment_summary(_new_fragment(expr, family="topk"), value_frag)
            frag.extra["topk_limit"] = int(limit_frag.scalar_value)
            frag.extra["topk_value_expr"] = value_frag.raw_expr
            return frag

    if func_name in SUPPORTED_RANGE_FUNCTIONS and len(args) == 1 and type(args[0]).__name__ == "MatrixSelector":
        matrix_frag = child_frags[0]
        if matrix_frag.metric and not matrix_frag.extra.get("not_feasible_reasons"):
            frag = _copy_fragment_summary(_new_fragment(expr, family="range_agg"), matrix_frag)
            frag.range_func = func_name
            return frag

    frag = _new_fragment(expr)
    for child in child_frags:
        _copy_fragment_summary(frag, child)
    if func_name:
        frag.extra["call_name"] = func_name

    if func_name in HARD_UNSUPPORTED_CALL_REASONS:
        _append_not_feasible_reason(frag, HARD_UNSUPPORTED_CALL_REASONS[func_name])
    elif func_name:
        _append_not_feasible_reason(frag, f"{func_name}() requires manual redesign")
    if func_name == "time" and args:
        _append_not_feasible_reason(frag, "PromQL time() call shape requires manual redesign")
    return frag


def _ast_aggregate_fragment(node, expr):
    child = _ast_from_node(node.expr, _ast_node_expr(node.expr))
    frag = _copy_fragment_summary(_new_fragment(expr), child)
    frag.extra["inner_frag"] = child
    frag.outer_agg = str(getattr(node, "op", "") or "").lower()

    if (
        frag.outer_agg == "topk"
        and child.metric
        and child.group_labels
        and child.group_mode == "by"
        and not child.extra.get("not_feasible_reasons")
    ):
        topk_frag = _copy_fragment_summary(_new_fragment(expr, family="topk"), child)
        try:
            param = getattr(node, "param", None)
            topk_frag.extra["topk_limit"] = int(float(getattr(param, "val", param) or 10))
        except (TypeError, ValueError):
            topk_frag.extra["topk_limit"] = 10
        topk_frag.extra["topk_value_expr"] = child.raw_expr
        return topk_frag

    if frag.outer_agg in HARD_UNSUPPORTED_CALL_REASONS:
        _append_not_feasible_reason(frag, HARD_UNSUPPORTED_CALL_REASONS[frag.outer_agg])

    modifier = getattr(node, "modifier", None)
    outer_group_labels = []
    outer_group_mode = "by"
    if modifier:
        outer_group_labels = list(getattr(modifier, "labels", []) or [])
        modifier_type = _ast_enum_name(getattr(modifier, "type", None))
        outer_group_mode = "without" if modifier_type == "Without" else "by"
        if outer_group_mode == "without":
            _append_not_feasible_reason(frag, HARD_UNSUPPORTED_AST_REASONS["without"])
    frag.group_labels = outer_group_labels
    frag.group_mode = outer_group_mode

    if frag.extra.get("not_feasible_reasons"):
        return frag

    if child.family == "range_agg" and child.metric and not child.outer_agg:
        frag.family = "range_agg"
        return frag

    if child.family == "uptime" and child.metric:
        frag.family = "uptime"
        return frag

    if child.family == "simple_metric" and child.metric:
        frag.family = "simple_agg"
        return frag

    if child.family == "simple_agg" and child.metric and child.outer_agg:
        frag.family = "nested_agg"
        frag.extra["inner_agg"] = child.outer_agg
        frag.extra["inner_group"] = list(child.group_labels)
        return frag

    return frag


def _ast_binary_matching(modifier):
    matching = getattr(modifier, "matching", None)
    labels = list(getattr(matching, "labels", []) or []) if matching else []
    return {
        "cardinality": _ast_enum_name(getattr(modifier, "card", None)),
        "labels": labels,
        "type": _ast_enum_name(getattr(matching, "type", None)) if matching else "",
    }


def _ast_binary_fragment(node, expr):
    left = _ast_from_node(node.lhs, _ast_node_expr(node.lhs))
    right = _ast_from_node(node.rhs, _ast_node_expr(node.rhs))
    op = str(getattr(node, "op", "") or "")

    if op in {">", "<", ">=", "<=", "==", "!="} and right.is_scalar and right.scalar_value is not None:
        frag = _copy_fragment_summary(_new_fragment(expr, family=left.family), left)
        frag.extra["post_filter"] = {
            "op": op,
            "value": right.scalar_value,
        }
        return frag

    if left.is_time_call and op == "-" and right.family in {"join", "range_agg", "simple_agg", "simple_metric"}:
        frag = _new_fragment(expr, family="uptime")
        frag.is_time_call = True
        frag.binary_op = "-"
        frag.binary_rhs = right
        if right.family == "join" and isinstance(right.binary_rhs, PromQLFragment):
            frag.group_labels = list(right.group_labels)
            frag.extra["start_metric"] = right.binary_rhs.metric or ""
            frag.extra["start_matchers"] = list(right.binary_rhs.matchers or [])
        else:
            _copy_fragment_summary(frag, right)
        _merge_not_feasible_reasons(frag, left, right)
        return frag

    modifier = getattr(node, "modifier", None)
    if modifier:
        matching = _ast_binary_matching(modifier)
        if matching["cardinality"] in {"ManyToOne", "OneToMany", "ManyToMany"}:
            frag = _copy_fragment_summary(_new_fragment(expr, family="join"), left)
            frag.binary_op = op
            frag.binary_rhs = right
            frag.group_labels = list(matching["labels"] or left.group_labels)
            frag.extra["join_labels"] = list(frag.group_labels)
            frag.extra["left_frag"] = left
            frag.extra["right_frag"] = right
            frag.extra["vector_matching"] = matching
            _merge_not_feasible_reasons(frag, left, right)
            return frag

    scalar_side = left if left.is_scalar else right if right.is_scalar else None
    agg_side = right if left.is_scalar else left if right.is_scalar else None
    if (
        op == "*"
        and scalar_side
        and agg_side
        and agg_side.family == "range_agg"
        and agg_side.outer_agg in {"avg", "sum", "max", "min"}
        and not agg_side.extra.get("not_feasible_reasons")
    ):
        frag = _copy_fragment_summary(_new_fragment(expr, family="scaled_agg"), agg_side)
        frag.binary_op = "*"
        frag.binary_rhs = PromQLFragment(
            scalar_value=scalar_side.scalar_value,
            is_scalar=True,
            extra={"parser_backend": scalar_side.extra.get("parser_backend", "ast")},
        )
        return frag

    frag = _make_binary_fragment(expr, left, op, right)
    frag.extra.setdefault("parser_backend", "ast")
    if modifier:
        frag.extra["vector_matching"] = _ast_binary_matching(modifier)
    return frag


def _ast_from_node(node, expr=None):
    expr = _trim_outer_parens(expr or _ast_node_expr(node))
    node_type = type(node).__name__

    if node_type == "ParenExpr":
        return _ast_from_node(node.expr, expr)

    if node_type == "UnaryExpr":
        child = _ast_from_node(node.expr, _ast_node_expr(node.expr))
        frag = _copy_fragment_summary(_new_fragment(expr), child)
        if child.is_scalar and child.scalar_value is not None:
            frag.family = "scalar"
            frag.is_scalar = True
            frag.scalar_value = -child.scalar_value
        return frag

    if node_type == "NumberLiteral":
        frag = _new_fragment(expr, family="scalar")
        frag.is_scalar = True
        frag.scalar_value = float(getattr(node, "val", 0.0))
        return frag

    if node_type == "StringLiteral":
        return _new_fragment(expr)

    if node_type == "VectorSelector":
        return _ast_vector_selector_fragment(node, expr)

    if node_type == "MatrixSelector":
        return _ast_matrix_selector_fragment(node, expr)

    if node_type == "Call":
        return _ast_call_fragment(node, expr)

    if node_type == "AggregateExpr":
        return _ast_aggregate_fragment(node, expr)

    if node_type == "BinaryExpr":
        return _ast_binary_fragment(node, expr)

    if node_type == "SubqueryExpr":
        child = _ast_from_node(node.expr, _ast_node_expr(node.expr))
        frag = _copy_fragment_summary(_new_fragment(expr), child)
        _append_not_feasible_reason(frag, HARD_UNSUPPORTED_AST_REASONS["subquery"])
        frag.extra["subquery_range"] = _duration_to_promql(getattr(node, "range", None))
        if getattr(node, "step", None):
            frag.extra["subquery_step"] = _duration_to_promql(getattr(node, "step", None))
        return frag

    return _new_fragment(expr)


def _parse_logql_fragment(expr):
    frag = _new_fragment(expr, backend="regex")
    if re.match(r'^\s*\{[^}]*\}\s*(?:\|\s*(?:~|=)\s*"[^"]*")?\s*$', expr, re.DOTALL):
        frag.family = "logql_stream"
        matchers, _ = _parse_logql_selector(expr)
        frag.matchers = matchers
        return frag

    logql_count = re.match(
        r'^\s*(?P<outer>sum|count)\s*\(\s*count_over_time\s*\(\s*\{(?P<selectors>[^}]*)\}.*?\[(?P<window>[^\]]+)\]\s*\)\s*\)\s*$',
        expr,
        re.IGNORECASE | re.DOTALL,
    )
    if logql_count:
        frag.outer_agg = logql_count.group("outer").lower()
        frag.matchers = _parse_selector_matchers(logql_count.group("selectors"))
        frag.range_func = "count_over_time"
        frag.range_window = logql_count.group("window")
        frag.family = "logql_count"
        return frag
    return None


def _make_binary_fragment(expr, left_frag, op, right_frag):
    reasons = []
    for child in (left_frag, right_frag):
        for reason in child.extra.get("not_feasible_reasons", []):
            if reason not in reasons:
                reasons.append(reason)

    backend = left_frag.extra.get("parser_backend") or right_frag.extra.get("parser_backend")
    extra = {
        "left_frag": left_frag,
        "right_frag": right_frag,
    }
    if reasons:
        extra["not_feasible_reasons"] = reasons
    if backend:
        extra["parser_backend"] = backend
    return PromQLFragment(
        raw_expr=expr,
        family="binary_expr",
        binary_op=op,
        extra=extra,
    )


def _parse_fragment(expr, depth=0):
    """Parse a PromQL expression into a PromQLFragment using the AST parser.

    Requires the ``promql-parser`` package (``pip install promql-parser``).
    """
    if promql_parser is None:
        raise ImportError(
            "The 'promql-parser' package is required but not installed. "
            "Install it with: pip install promql-parser"
        )

    expr = _trim_outer_parens(expr.strip())
    logql_frag = _parse_logql_fragment(expr)
    if logql_frag:
        return logql_frag

    try:
        ast = promql_parser.parse(expr)
    except (ValueError, TypeError, Exception) as exc:
        frag = _new_fragment(expr, backend="regex")
        frag.extra["parse_error"] = str(exc)
        return frag
    frag = _ast_from_node(ast, expr)
    frag.extra.setdefault("parser_backend", "ast")
    return frag


def _apply_fragment_to_context(frag, context):
    backend = frag.extra.get("parser_backend")
    if backend:
        context.parser_backend = backend

    summary = _find_summary_fragment(frag) or frag

    if not context.group_labels:
        context.group_labels = list(frag.group_labels or _extract_group_labels(context.clean_expr or context.promql_expr))

    if not context.outer_agg:
        context.outer_agg = frag.outer_agg or summary.outer_agg or _detect_outer_agg(context.clean_expr or context.promql_expr) or ""

    summary_inner = frag.extra.get("call_name") or frag.range_func or summary.extra.get("call_name") or summary.range_func
    if not context.inner_func and summary_inner:
        context.inner_func = summary_inner

    if not context.metric_name and summary.metric:
        context.metric_name = summary.metric

    if not context.range_window and (frag.range_window or summary.range_window):
        context.range_window = frag.range_window or summary.range_window

def _build_stats_call(outer_agg, inner_func, metric_name, range_window):
    esql_outer = OUTER_AGG_MAP.get(outer_agg, outer_agg.upper())
    esql_inner = AGG_FUNCTION_MAP.get(inner_func, inner_func.upper())
    return f"{esql_outer}({esql_inner}({metric_name}, {range_window}))"


def _build_esql(context):
    alias = re.sub(r"[^a-zA-Z0-9_]", "_", context.metric_name)
    parts = [f"{context.source_type} {context.index}"]
    if context.time_filter:
        parts.append(f"| WHERE {context.time_filter}")
    for label_filter in context.label_filters:
        parts.append(f"| WHERE {label_filter}")
    stats_line = f"| STATS {alias} = {context.stats_expr}"
    by_parts = []
    if context.bucket_expr:
        by_parts.append(context.bucket_expr)
    by_parts.extend(context.group_labels)
    if by_parts:
        stats_line += f" BY {', '.join(by_parts)}"
    parts.append(stats_line)
    return "\n".join(parts)


def _frag_filters(frag, resolver):
    """Build ES|QL WHERE clauses from fragment matchers using the resolver."""
    filters = _selector_filters(frag.matchers, resolver)
    had_vars = any(
        "$" in m.get("value", "")
        or (m.get("op") == "=~" and m.get("value", "").strip() == ".*")
        for m in frag.matchers
    )
    return filters, had_vars


def _summary_mode_from_metadata(metadata):
    return bool((metadata or {}).get("summary_mode"))


def _merge_group_fields(explicit_fields, preferred_fields):
    if not preferred_fields:
        return explicit_fields
    merged = list(preferred_fields)
    for field_name in explicit_fields:
        if field_name not in merged:
            merged.append(field_name)
    return merged


def _frag_group_labels(frag, resolver, preferred_labels=None):
    """Resolve fragment group labels through the resolver."""
    explicit = resolver.resolve_labels(frag.group_labels) if resolver else list(frag.group_labels or [])
    preferred = resolver.resolve_labels(preferred_labels or []) if resolver else list(preferred_labels or [])
    return _merge_group_fields(explicit, preferred)


def _grouping_parts(bucket_expr, group_fields):
    by_parts = []
    output_group_fields = []
    if bucket_expr:
        by_parts.append(bucket_expr)
        output_group_fields.append("time_bucket")
    by_parts.extend(group_fields)
    output_group_fields.extend(group_fields)
    return by_parts, output_group_fields


def _collapse_summary_ts_query(parts, output_group_fields, keep_fields):
    if not output_group_fields or output_group_fields[0] != "time_bucket":
        return None
    group_fields = list(output_group_fields[1:])
    reduced = ", ".join(
        f"{field} = LAST({field}, time_bucket)" for field in keep_fields
    )
    if group_fields:
        parts.append("| SORT time_bucket ASC")
        parts.append(f"| STATS {reduced} BY {', '.join(group_fields)}")
        parts.append(f"| KEEP {', '.join(group_fields + keep_fields)}")
        return group_fields
    if output_group_fields != ["time_bucket"]:
        return None
    parts.append("| SORT time_bucket ASC")
    parts.append(f"| STATS time_bucket = MAX(time_bucket), {reduced}")
    parts.append(f"| KEEP time_bucket, {', '.join(keep_fields)}")
    return []


def _frag_eval_expr(alias, frag):
    if not frag.binary_op:
        return alias, ""
    final_alias = f"{alias}_calc"
    if frag.extra.get("scalar_left") is not None:
        sv = _format_scalar_value(frag.extra["scalar_left"])
        return final_alias, f"{sv} {frag.binary_op} {alias}"
    if frag.binary_rhs and frag.binary_rhs.is_scalar:
        sv = _format_scalar_value(frag.binary_rhs.scalar_value)
        return final_alias, f"{alias} {frag.binary_op} {sv}"
    return alias, ""


def _frag_eval_line(alias, frag):
    """Build an optional EVAL line for binary-op-with-scalar."""
    final_alias, eval_expr = _frag_eval_expr(alias, frag)
    if eval_expr:
        return f"| EVAL {final_alias} = {eval_expr}", final_alias
    return None, final_alias


def _scalar_fragment_expr(frag):
    if not frag:
        return None
    if frag.family == "uptime":
        return None
    if frag.is_scalar:
        return _format_scalar_value(frag.scalar_value)
    if frag.is_time_call:
        return 'DATE_DIFF("seconds", TO_DATETIME(0), NOW())'
    return None


def _rename_measure_alias(spec, new_alias):
    old_alias = spec.alias
    if old_alias == new_alias:
        return
    spec.alias = new_alias
    if spec.final_alias == old_alias:
        spec.final_alias = new_alias
    elif spec.final_alias == f"{old_alias}_calc":
        spec.final_alias = f"{new_alias}_calc"
    if spec.eval_expr:
        spec.eval_expr = re.sub(rf"\b{re.escape(old_alias)}\b", new_alias, spec.eval_expr)


def _matcher_alias_suffix(frag):
    parts = []
    for matcher in frag.matchers[:2]:
        label = re.sub(r"[^a-zA-Z0-9_]", "_", matcher["label"]).strip("_")
        value = re.sub(r"[^a-zA-Z0-9_]", "_", matcher["value"]).strip("_")[:12]
        if label or value:
            parts.append("_".join(part for part in (label, value) if part))
    if frag.range_func:
        parts.append(frag.range_func)
    if frag.outer_agg:
        parts.append(frag.outer_agg)
    return "_".join(part for part in parts if part)


def _build_measure_spec(frag, resolver, rule_pack, alias_hint="", summary_mode=False, preferred_group_labels=None):
    if not frag or (not frag.metric and frag.family != "uptime"):
        return None

    filters, had_vars = _frag_filters(frag, resolver)
    warnings = []
    if had_vars:
        warnings.append("Dropped variable-driven label filters during migration")
    group_fields = _frag_group_labels(frag, resolver, preferred_group_labels)
    if alias_hint:
        suffix = alias_hint
    else:
        suffix = _matcher_alias_suffix(frag)
    alias = _safe_alias(frag.metric, suffix)
    final_alias = None
    eval_expr = ""

    if frag.family == "simple_metric":
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        source = "TS" if is_counter else "FROM"
        time_filter = rule_pack.ts_time_filter if source == "TS" else rule_pack.from_time_filter
        bucket_expr = rule_pack.ts_bucket if source == "TS" else rule_pack.from_bucket
        if is_counter:
            stats_expr = f"AVG(RATE({frag.metric}, {rule_pack.default_rate_window}))"
            warnings.append(f"Detected counter metric; defaulting to RATE over {rule_pack.default_rate_window}")
        else:
            default_agg = rule_pack.default_gauge_agg.upper()
            stats_expr = f"{default_agg}({frag.metric})"
            if frag.extra.get("wrapped_scalar"):
                warnings.append("Approximated scalar() as a direct metric value")
            else:
                warnings.append(f"No explicit aggregation; using {default_agg} (correct for gauge metrics)")
    elif frag.family == "simple_agg":
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        if frag.outer_agg == "count" and is_counter:
            return None
        source = "TS" if is_counter else "FROM"
        time_filter = rule_pack.ts_time_filter if source == "TS" else rule_pack.from_time_filter
        bucket_expr = rule_pack.ts_bucket if source == "TS" else rule_pack.from_bucket
        if is_counter and frag.outer_agg != "count":
            inner_expr = f"RATE({frag.metric}, {rule_pack.default_rate_window})"
            warnings.append(f"Detected counter metric; defaulting to RATE over {rule_pack.default_rate_window}")
        else:
            inner_expr = frag.metric
        outer = OUTER_AGG_MAP.get(frag.outer_agg, rule_pack.default_gauge_agg.upper())
        stats_expr = f"{outer}({inner_expr})"
    elif frag.family == "range_agg":
        esql_inner = AGG_FUNCTION_MAP.get(frag.range_func)
        if not esql_inner:
            return None
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        needs_ts = is_counter or frag.range_func in AGG_FUNCTION_MAP
        source = "TS" if needs_ts else "FROM"
        time_filter = rule_pack.ts_time_filter if source == "TS" else rule_pack.from_time_filter
        bucket_expr = rule_pack.ts_bucket if source == "TS" else rule_pack.from_bucket
        inner_expr = f"{esql_inner}({frag.metric}, {frag.range_window})"
        outer = OUTER_AGG_MAP.get(frag.outer_agg, "") if frag.outer_agg else ""
        if not outer and source == "TS" and group_fields:
            stats_expr = f"AVG({inner_expr})"
            warnings.append(f"Wrapped {frag.range_func} in AVG() to support grouped TS queries")
        else:
            stats_expr = f"{outer}({inner_expr})" if outer else inner_expr
    elif frag.family == "scaled_agg":
        esql_inner = AGG_FUNCTION_MAP.get(frag.range_func)
        if not esql_inner:
            return None
        source = "TS"
        time_filter = rule_pack.ts_time_filter
        bucket_expr = rule_pack.ts_bucket
        esql_outer = OUTER_AGG_MAP.get(frag.outer_agg, "AVG")
        stats_expr = f"{esql_outer}({esql_inner}({frag.metric}, {frag.range_window}))"
    elif frag.family == "nested_agg":
        inner_groups = resolver.resolve_labels(frag.extra.get("inner_group", [])) if resolver else list(frag.extra.get("inner_group", []))
        if frag.outer_agg == "count" and frag.extra.get("inner_agg") == "count" and inner_groups:
            source = "FROM"
            time_filter = rule_pack.from_time_filter
            bucket_expr = rule_pack.from_bucket
            stats_expr = f"COUNT_DISTINCT({inner_groups[0]})"
            warnings.append(f"Approximated nested count(count()) as COUNT_DISTINCT({inner_groups[0]})")
        else:
            return None
    elif frag.family == "uptime":
        start_metric = frag.metric
        start_matchers = frag.matchers
        if not start_metric and frag.binary_rhs and isinstance(frag.binary_rhs, PromQLFragment):
            if frag.binary_rhs.family == "join" and frag.extra.get("start_metric"):
                start_metric = frag.extra["start_metric"]
                start_matchers = frag.extra.get("start_matchers", [])
            elif frag.binary_rhs.metric:
                start_metric = frag.binary_rhs.metric
                start_matchers = frag.binary_rhs.matchers
        if not start_metric:
            return None
        filters, had_vars = _frag_filters(PromQLFragment(matchers=start_matchers), resolver)
        warnings = []
        if had_vars:
            warnings.append("Dropped variable-driven label filters during migration")
        alias = _safe_alias(f"{start_metric}_start_time_ms", suffix)
        final_alias = _safe_alias(f"{start_metric}_uptime_seconds", alias_hint)
        source = "FROM"
        time_filter = rule_pack.from_time_filter
        bucket_expr = rule_pack.from_bucket if summary_mode else ""
        stats_expr = f"MAX({start_metric} * 1000)"
        eval_expr = f'DATE_DIFF("seconds", TO_DATETIME({alias}), NOW())'
    else:
        return None

    if final_alias is None:
        final_alias, eval_expr = _frag_eval_expr(alias, frag)
    return MeasureSpec(
        source_type=source,
        time_filter=time_filter,
        bucket_expr=bucket_expr,
        group_fields=group_fields,
        filters=filters,
        alias=alias,
        stats_expr=stats_expr,
        final_alias=final_alias,
        eval_expr=eval_expr,
        metric_name=frag.metric,
        warnings=warnings,
    )


def _measure_specs_mergeable(specs):
    if not specs or any(spec is None for spec in specs):
        return False
    base = specs[0]
    base_filters = sorted(base.filters)
    for spec in specs[1:]:
        if spec.source_type != base.source_type:
            return False
        if spec.time_filter != base.time_filter or spec.bucket_expr != base.bucket_expr:
            return False
        if spec.group_fields != base.group_fields:
            return False
        if sorted(spec.filters) != base_filters:
            if base.source_type != "FROM":
                return False
            if _inline_filters_into_stats_expr(base.stats_expr, base.filters) is None:
                return False
            if _inline_filters_into_stats_expr(spec.stats_expr, spec.filters) is None:
                return False
    return True


def _common_filters(specs):
    if not specs:
        return []
    common = []
    for filter_expr in specs[0].filters:
        if filter_expr not in common and all(filter_expr in spec.filters for spec in specs[1:]):
            common.append(filter_expr)
    return common


def _inline_filters_into_stats_expr(stats_expr, filters):
    if not filters:
        return stats_expr
    match = re.match(r"^(?P<agg>[A-Z_]+)\((?P<inner>.+)\)$", stats_expr or "")
    if not match:
        return None
    agg = match.group("agg")
    inner = match.group("inner").strip()
    condition = " and ".join(f"({filter_expr})" for filter_expr in filters)
    if inner == "*":
        if agg == "COUNT":
            return f"SUM(CASE({condition}, 1, 0))"
        return None
    return f"{agg}(CASE({condition}, {inner}, NULL))"


def _build_shared_measure_pipeline(index, specs):
    if not _measure_specs_mergeable(specs):
        return None

    unique_specs = []
    by_alias = {}
    for spec in specs:
        signature = (
            spec.source_type,
            spec.time_filter,
            spec.bucket_expr,
            tuple(spec.group_fields),
            tuple(spec.filters),
            spec.stats_expr,
            spec.final_alias,
            spec.eval_expr,
        )
        existing = by_alias.get(spec.alias)
        if existing is None:
            by_alias[spec.alias] = signature
            unique_specs.append(spec)
            continue
        if existing != signature:
            return None
    specs = unique_specs

    base = specs[0]
    common_filters = _common_filters(specs)
    group_fields = (["time_bucket"] if base.bucket_expr else []) + base.group_fields
    by_parts = ([base.bucket_expr] if base.bucket_expr else []) + base.group_fields
    stats_terms = []
    for spec in specs:
        scoped_filters = [filter_expr for filter_expr in spec.filters if filter_expr not in common_filters]
        scoped_expr = _inline_filters_into_stats_expr(spec.stats_expr, scoped_filters)
        if not scoped_expr:
            return None
        stats_terms.append(f"{spec.alias} = {scoped_expr}")
    parts = [
        f"{base.source_type} {index}",
        f"| WHERE {base.time_filter}",
        *_build_where_lines(common_filters),
    ]
    presence_metrics = []
    for spec in specs:
        metric_name = str(spec.metric_name or "").strip()
        if metric_name and metric_name not in presence_metrics:
            presence_metrics.append(metric_name)
    if presence_metrics:
        parts.append("| WHERE " + " OR ".join(f"{metric} IS NOT NULL" for metric in presence_metrics))
    stats_line = "| STATS " + ", ".join(stats_terms)
    if by_parts:
        stats_line += f" BY {', '.join(by_parts)}"
    parts.append(stats_line)
    metric_fields = []
    for spec in specs:
        if spec.eval_expr:
            parts.append(f"| EVAL {spec.final_alias} = {spec.eval_expr}")
        metric_fields.append(spec.final_alias)
    return parts, group_fields, metric_fields


def _build_formula_plan(frag, resolver, rule_pack, alias_hint="", summary_mode=False, preferred_group_labels=None):
    scalar_expr = _scalar_fragment_expr(frag)
    if scalar_expr is not None:
        return FormulaPlan(specs=[], expr=scalar_expr)

    if frag and frag.family == "binary_expr":
        left_plan = _build_formula_plan(
            frag.extra.get("left_frag"),
            resolver,
            rule_pack,
            alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
        )
        right_plan = _build_formula_plan(
            frag.extra.get("right_frag"),
            resolver,
            rule_pack,
            alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
        )
        if not left_plan or not right_plan:
            return None
        warnings = []
        for warning in left_plan.warnings + right_plan.warnings:
            if warning not in warnings:
                warnings.append(warning)
        return FormulaPlan(
            specs=left_plan.specs + right_plan.specs,
            expr=f"({left_plan.expr} {frag.binary_op} {right_plan.expr})",
            warnings=warnings,
        )

    spec = _build_measure_spec(
        frag,
        resolver,
        rule_pack,
        alias_hint=alias_hint,
        summary_mode=summary_mode,
        preferred_group_labels=preferred_group_labels,
    )
    if not spec:
        return None
    return FormulaPlan(specs=[spec], expr=spec.final_alias, warnings=list(spec.warnings))


__all__ = [
    "AGG_FUNCTION_MAP",
    "OUTER_AGG_MAP",
    "FormulaPlan",
    "MeasureSpec",
    "PromQLFragment",
    "_apply_fragment_to_context",
    "_build_esql",
    "_build_formula_plan",
    "_build_log_message_filter",
    "_build_measure_spec",
    "_build_shared_measure_pipeline",
    "_build_stats_call",
    "_build_where_lines",
    "_collapse_summary_ts_query",
    "_common_matchers",
    "_detect_outer_agg",
    "_extract_group_labels",
    "_format_scalar_value",
    "_frag_eval_line",
    "_frag_filters",
    "_frag_group_labels",
    "_grouping_parts",
    "_matcher_alias_suffix",
    "_parse_fragment",
    "_parse_logql_search",
    "_parse_logql_selector",
    "_parse_selector_matchers",
    "_quote_esql_string",
    "_scalar_fragment_expr",
    "_selector_filters",
    "_split_top_level_csv",
    "_strip_grafana_substitutions",
    "_summary_mode_from_metadata",
    "_unique_safe_alias",
    "classify_promql_complexity",
    "preprocess_grafana_macros",
]
