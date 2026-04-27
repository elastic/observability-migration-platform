# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""PromQL fragment parsing and ES|QL planning helpers."""

from __future__ import annotations

import dataclasses
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


def _esql_field(name: str) -> str:
    """Backtick-quote an ES|QL field reference that contains special characters.

    Plain identifiers ([a-zA-Z0-9_.]) are returned as-is.  Field paths that
    contain characters like ':' (Prometheus recording-rule metrics) or '-' are
    wrapped in backticks so ES|QL does not misinterpret them as operators.
    """
    if name and re.search(r"[^a-zA-Z0-9_.]", name):
        escaped = name.replace("`", "\\`")
        return f"`{escaped}`"
    return name


def _resolve_metric_field(resolver, metric_name, *, prefer=None):
    """Resolve a PromQL metric name to its physical target field, ES|QL-escaped.

    Passes through to ``resolver.resolve_metric_field`` when a resolver is
    available, otherwise returns ``metric_name`` unchanged so callers without
    a resolver (offline / fallback paths) still emit the source-faithful
    field reference.  The returned field path is always safe to embed directly
    inside ES|QL STATS / WHERE expressions.
    """
    if resolver is None or not metric_name:
        return metric_name
    resolve = getattr(resolver, "resolve_metric_field", None)
    if resolve is None:
        return _esql_field(metric_name)
    return _esql_field(resolve(metric_name, prefer=prefer))

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


# Degradations applied when the source PromQL asks for a counter-style
# range function but the resolved field is typed ``gauge`` (e.g. an
# Elastic ``/_prometheus/api/v1/write`` ingest that didn't detect a
# counter by name). ES|QL's RATE/IRATE/INCREASE require ``counter_*``
# typing; emitting them against a gauge produces a hard 400 in Kibana.
# The chosen gauge analogues let the panel still render real numbers
# while a warning explains the swap.
_COUNTER_TO_GAUGE_FALLBACK = {
    # ``rate``/``irate`` over a gauge degrades to the value averaged
    # across the window — not a per-second rate, but the closest honest
    # measurement available without a proper counter type.
    "rate": (
        "AVG_OVER_TIME",
        "Source PromQL used rate() but {metric} is typed as gauge in the "
        "target index; rendered as AVG_OVER_TIME instead. Fix the ingest "
        "mapping to mark this field as a counter to get a true rate.",
    ),
    "irate": (
        "AVG_OVER_TIME",
        "Source PromQL used irate() but {metric} is typed as gauge in the "
        "target index; rendered as AVG_OVER_TIME instead. Fix the ingest "
        "mapping to mark this field as a counter to get a true rate.",
    ),
    # ``increase`` is total change over the window; the closest gauge
    # analogue is MAX - MIN, but ES|QL only allows a single function
    # call inside STATS so we fall back to ``MAX_OVER_TIME`` (upper
    # bound of the cumulative value) and warn loudly.
    "increase": (
        "MAX_OVER_TIME",
        "Source PromQL used increase() but {metric} is typed as gauge in "
        "the target index; rendered as MAX_OVER_TIME (cumulative ceiling) "
        "instead. Fix the ingest mapping to mark this field as a counter "
        "to recover the true increase over the window.",
    ),
}


def _gauge_fallback_for_counter_range_func(range_func):
    """Return ``(esql_function, warning_template)`` to use when a
    counter-style range function (``rate``/``irate``/``increase``) is
    applied to a field that the target cluster has typed as a gauge.
    The warning template contains a ``{metric}`` placeholder for the
    caller to substitute the source metric name."""
    result = _COUNTER_TO_GAUGE_FALLBACK.get(range_func)
    if result is None:
        raise ValueError(
            f"no gauge fallback for range function {range_func!r}; "
            f"expected one of {sorted(_COUNTER_TO_GAUGE_FALLBACK)}"
        )
    return result

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

_SET_OPERATORS = frozenset({"or", "and", "unless"})


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
    metric_field: str = ""
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
    # Subquery form [$var:$var] — must run BEFORE the general $var→label_var
    # pass so both halves are still recognisable as variables.
    result = re.sub(
        r"\[\s*\$(?!__)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\$(?!__)([A-Za-z_][A-Za-z0-9_]*)\s*\]",
        f"[{default_window}:1m]",
        result,
    )
    result = re.sub(r"\[\s*label_([A-Za-z_][A-Za-z0-9_]*)\s*\]", f"[{default_window}]", result)
    # Subquery form with one substituted half: [5m:$var], [$var:5m], or after
    # label_xxx substitution [5m:label_xxx] / [label_xxx:5m].  Any bracket
    # selector that contains a colon and a non-duration token is a subquery
    # with template variables; normalise to a concrete [range:step] so the AST
    # parser correctly flags it as "subquery" rather than an opaque parse error.
    _DUR_RE = r"(?:\d+(?:ms|s|m|h|d|w|y))"
    result = re.sub(
        rf"\[\s*({_DUR_RE})\s*:\s*(?!\s*\d)[^\]]+\]",
        f"[{default_window}:1m]",
        result,
    )

    result = re.sub(r'\{([^}]*?)(\w+)=~"\$(\w+)"([^}]*?)\}', r'{\1\2=~".*"\4}', result)
    result = re.sub(r'\{([^}]*?)(\w+)="\$(\w+)"([^}]*?)\}', r'{\1\2=~".*"\4}', result)
    # ${var} and ${var:format} — Grafana advanced variable interpolation.
    # Must run before the bare $var substitution so the opening brace isn't
    # left as a dangling token that confuses the PromQL AST parser.
    result = re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}",
        lambda m: f"label_{m.group(1)}",
        result,
    )
    # Skip substitution for pure-digit sequences ($1, $2, …) — those are
    # PromQL/regex capture-group backreferences inside label_replace() strings,
    # not Grafana template variables (which always start with a letter).
    result = re.sub(
        r"\$(\w+)",
        lambda m: m.group(0) if (m.group(1).startswith("__") or m.group(1)[0].isdigit()) else f"label_{m.group(1)}",
        result,
    )
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
    escaped = False
    for char in expr:
        if in_quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_quote:
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


_PROMQL_VALID_LABEL_RE = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")
_PROMQL_LABEL_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\.:-]*$")


def _sanitize_promql_labels_for_ast(expr):
    replacements = {}

    def safe_label(label):
        if _PROMQL_VALID_LABEL_RE.match(label):
            return label
        if label not in replacements:
            counter = len(replacements)
            candidate = f"__obs_migration_sanitized_label_{counter}"
            while candidate in expr or candidate in replacements.values():
                counter += 1
                candidate = f"__obs_migration_sanitized_label_{counter}"
            replacements[label] = candidate
        return replacements[label]

    def sanitize_selector(selector_text):
        parts = []
        changed = False
        for matcher_text in _split_top_level_csv(selector_text):
            match = re.match(
                r"(?P<prefix>\s*)(?P<label>[A-Za-z_][A-Za-z0-9_\.:-]*)(?P<space>\s*)(?P<op>=~|!~|=|!=)(?P<rest>.*)\s*$",
                matcher_text,
                flags=re.DOTALL,
            )
            if not match:
                parts.append(matcher_text)
                continue
            replacement = safe_label(match.group("label"))
            changed = changed or replacement != match.group("label")
            parts.append(
                f"{match.group('prefix')}{replacement}{match.group('space')}{match.group('op')}{match.group('rest')}"
            )
        return ", ".join(parts), changed

    pieces = []
    idx = 0
    changed_selectors = False
    while idx < len(expr):
        if expr[idx] != "{":
            pieces.append(expr[idx])
            idx += 1
            continue
        end = idx + 1
        quote = ""
        escaped = False
        while end < len(expr):
            char = expr[end]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in ('"', "'"):
                quote = char
            elif char == "}":
                break
            end += 1
        if end >= len(expr) or expr[end] != "}":
            pieces.append(expr[idx:])
            idx = len(expr)
            break
        selector_text, selector_changed = sanitize_selector(expr[idx + 1 : end])
        changed_selectors = changed_selectors or selector_changed
        pieces.append("{" + selector_text + "}")
        idx = end + 1
    sanitized = "".join(pieces) if changed_selectors else expr

    def grouping_repl(match):
        labels = []
        changed = False
        for label in _split_top_level_csv(match.group("labels")):
            stripped = label.strip()
            if _PROMQL_LABEL_TOKEN_RE.match(stripped):
                replacement = safe_label(stripped)
                changed = changed or replacement != stripped
                labels.append(replacement)
            else:
                labels.append(stripped)
        if not changed:
            return match.group(0)
        return f"{match.group('kw')}({', '.join(labels)})"

    grouping_pattern = re.compile(
        r"\b(?P<kw>by|without|on|ignoring)\s*\((?P<labels>[^)]*)\)",
        flags=re.IGNORECASE,
    )

    pieces = []
    start = 0
    idx = 0
    quote = ""
    escaped = False
    while idx < len(sanitized):
        char = sanitized[idx]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
                pieces.append(sanitized[start : idx + 1])
                start = idx + 1
        elif char in ('"', "'"):
            if start < idx:
                pieces.append(grouping_pattern.sub(grouping_repl, sanitized[start:idx]))
            start = idx
            quote = char
        idx += 1
    if start < len(sanitized):
        if quote:
            pieces.append(sanitized[start:])
        else:
            pieces.append(grouping_pattern.sub(grouping_repl, sanitized[start:]))
    sanitized = "".join(pieces) if pieces else sanitized
    return sanitized, {safe: original for original, safe in replacements.items()}


def _le_float_alt(value: str) -> str | None:
    """Return the ".0" float alternative for an integer-string histogram boundary.

    Some Prometheus exporters (e.g. express-prometheus-middleware) store the
    histogram `le` label as "1.0" / "10.0" while Grafana dashboards reference
    them as "1" / "10".  When the value has no decimal point and parses as a
    non-negative integer we return the float form so callers can emit an OR
    clause covering both representations.
    """
    if "." in value or "e" in value.lower():
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    if not (0 <= f < 1e15) or f != int(f):
        return None
    return f"{int(f)}.0"


# Labels that use floating-point storage in some Prometheus exporters.
_FLOAT_LABEL_NAMES = frozenset({"le"})


_VAR_TOKEN_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _extract_single_var_name(value: str) -> str | None:
    matches = list(_VAR_TOKEN_RE.finditer(value))
    if len(matches) != 1:
        return None
    name = matches[0].group(1) or matches[0].group(2)
    span = matches[0].span()
    bare = value[: span[0]] + value[span[1] :]
    if bare:
        return None
    return name


def _bound_param_clause(label, op, var_name, multi):
    if multi:
        if op == "=~":
            return f"MV_CONTAINS(?{var_name}, {label})"
        if op == "!~":
            return f"NOT MV_CONTAINS(?{var_name}, {label})"
        return None
    if op == "=" or op == "=~":
        return f"{label} == ?{var_name}"
    if op == "!=" or op == "!~":
        return f"{label} != ?{var_name}"
    return None


def _matcher_to_esql(matcher, resolver, *, binding_map=None):
    label = resolver.resolve_label(matcher["label"]) if resolver else matcher["label"]
    op = matcher["op"]
    value = matcher["value"]
    if not label:
        return None
    if binding_map:
        var_name = _extract_single_var_name(value)
        if var_name and var_name in binding_map:
            from observability_migration.core.variable_classifier import AcceptedBinding
            binding = binding_map[var_name]
            if isinstance(binding, AcceptedBinding):
                return _bound_param_clause(label, op, var_name, binding.multi)
    # Drop preprocessed Grafana variables (label_Var / ^label_Var*) and
    # unprocessed special variables ($__interval etc.).  Use \$\w to avoid
    # false-positives on regex end-of-string anchors like ".*cam(era)?$".
    if value.startswith("label_") or value.startswith("^label_") or re.search(r"\$\w", value):
        return None
    if op == "=":
        if matcher["label"] in _FLOAT_LABEL_NAMES:
            alt = _le_float_alt(value)
            if alt is not None:
                return (
                    f"({label} == {_quote_esql_string(value)}"
                    f" OR {label} == {_quote_esql_string(alt)})"
                )
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


def _selector_filters(matchers, resolver, *, binding_map=None):
    filters = []
    for matcher in matchers:
        filter_expr = _matcher_to_esql(matcher, resolver, binding_map=binding_map)
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
    if not search_expr:
        return None
    # Strip leading inline regex flags like (?i) before variable-reference checks so
    # that "(?i)$searchable_pattern" (preprocessed Grafana variable) is correctly dropped
    # rather than rendered as RLIKE ".*(?i)label_searchable_pattern.*".
    check = re.sub(r"^\(\?[imsx-]+\)", "", search_expr).strip()
    if check.startswith("$") or check.startswith("label_") or re.search(r"\$\w", check):
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

    # sort() / sort_desc() — strip outer wrapper, flag for value-sort postprocessor
    if func_name in {"sort", "sort_desc"} and len(child_frags) == 1:
        inner = child_frags[0]
        if not inner.extra.get("not_feasible_reasons"):
            result = _copy_fragment_summary(_new_fragment(expr, family=inner.family), inner)
            for k, v in inner.extra.items():
                result.extra.setdefault(k, v)
            result.extra["value_sort_desc"] = (func_name == "sort_desc")
            return result

    # round() — strip outer wrapper, carry precision for ROUND() postprocessor
    if func_name == "round" and 1 <= len(child_frags) <= 2:
        inner = child_frags[0]
        if not inner.extra.get("not_feasible_reasons"):
            precision = (
                child_frags[1].scalar_value
                if len(child_frags) == 2 and child_frags[1].is_scalar
                else None
            )
            result = _copy_fragment_summary(_new_fragment(expr, family=inner.family), inner)
            for k, v in inner.extra.items():
                result.extra.setdefault(k, v)
            result.extra["has_round"] = True
            result.extra["round_precision"] = precision
            return result

    # clamp_min() — strip outer wrapper, carry threshold for GREATEST() postprocessor
    if func_name == "clamp_min" and len(child_frags) == 2:
        inner, threshold_frag = child_frags
        if (
            not inner.extra.get("not_feasible_reasons")
            and threshold_frag.is_scalar
            and threshold_frag.scalar_value is not None
        ):
            result = _copy_fragment_summary(_new_fragment(expr, family=inner.family), inner)
            for k, v in inner.extra.items():
                result.extra.setdefault(k, v)
            result.extra["clamp_min_value"] = threshold_frag.scalar_value
            return result

    # label_replace(v, dst, replacement, src, regex) — new fragment family
    if func_name == "label_replace" and len(child_frags) == 5:
        value_frag = child_frags[0]
        string_args = [f.extra.get("string_value") for f in child_frags[1:]]
        if (
            all(s is not None for s in string_args)
            and not value_frag.extra.get("not_feasible_reasons")
        ):
            dst, replacement, src, regex = string_args
            result = _copy_fragment_summary(
                _new_fragment(expr, family="label_replace"), value_frag
            )
            result.extra["lr_dst"] = dst
            result.extra["lr_replacement"] = replacement
            result.extra["lr_src"] = src
            result.extra["lr_regex"] = regex
            result.extra["lr_inner_frag"] = value_frag
            return result

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


def _contains_join_frag(frag, _depth=0):
    """Return True if *frag* or any binary_expr descendant is a join fragment."""
    if frag is None or _depth > 8:
        return False
    if frag.family == "join":
        return True
    if frag.family == "binary_expr":
        return _contains_join_frag(frag.extra.get("left_frag"), _depth + 1) or _contains_join_frag(
            frag.extra.get("right_frag"), _depth + 1
        )
    return False


def _push_outer_agg(frag, outer_agg, group_labels, group_mode):
    """Push an outer aggregation down to a leaf fragment.

    Used to apply linearity when rewriting ``sum(A ± B)`` as
    ``sum(A) ± sum(B)``.  Also handles two deeper cases:

    * ``family="join"`` — strip the label-enrichment RHS and push the agg
      to the primary (LHS) metric, mirroring what ``join_family_rule`` does.
      This enables ``agg(join_result / k)`` → ``agg(primary) / k``.
    * ``family="binary_expr"`` with a scalar operand — recurse through nested
      scalar divisions/multiplications so ``agg(join / k1 / k2)`` resolves to
      ``agg(primary) / k1 / k2``.

    Returns ``None`` when the fragment cannot accept a pushed aggregation.
    """
    new_family = frag.family
    if frag.family == "simple_metric":
        new_family = "simple_agg"
    elif frag.family in {"range_agg", "simple_agg"}:
        pass
    elif frag.family == "join" and frag.binary_op == "*":
        # Strip the label-enrichment join; push the agg to the primary metric
        # on the LHS (same side join_family_rule uses in translate.py).
        left = frag.extra.get("left_frag")
        if left is None or left.extra.get("not_feasible_reasons"):
            return None
        return _push_outer_agg(left, outer_agg, group_labels, group_mode)
    elif frag.family == "binary_expr" and frag.binary_op in {"/", "*"}:
        # Recursive scalar hoisting through nested binary_expr layers.
        # Handles e.g. agg(join_result / 1024 / 1024).
        inner_left = frag.extra.get("left_frag")
        inner_right = frag.extra.get("right_frag")
        scalar_side = None
        vector_side = None
        if inner_right is not None and inner_right.is_scalar and inner_right.scalar_value is not None:
            scalar_side = inner_right
            vector_side = inner_left
        elif frag.binary_op == "*" and inner_left is not None and inner_left.is_scalar and inner_left.scalar_value is not None:
            scalar_side = inner_left
            vector_side = inner_right
        if scalar_side is None or vector_side is None or vector_side.extra.get("not_feasible_reasons"):
            return None
        pushed = _push_outer_agg(vector_side, outer_agg, group_labels, group_mode)
        if pushed is None:
            return None
        if frag.binary_op == "/" and scalar_side is inner_left:
            return _make_binary_fragment(frag.raw_expr, scalar_side, "/", pushed)
        return _make_binary_fragment(frag.raw_expr, pushed, frag.binary_op, scalar_side)
    else:
        return None
    return dataclasses.replace(
        frag,
        family=new_family,
        outer_agg=outer_agg,
        group_labels=list(group_labels),
        group_mode=group_mode,
        extra=dict(frag.extra),
    )


def _ast_aggregate_fragment(node, expr):
    child = _ast_from_node(node.expr, _ast_node_expr(node.expr))
    frag = _copy_fragment_summary(_new_fragment(expr), child)
    frag.extra["inner_frag"] = child
    frag.outer_agg = str(getattr(node, "op", "") or "").lower()

    if frag.outer_agg == "topk" and child.metric and not child.extra.get("not_feasible_reasons"):
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

    # Handle aggregation over a binary expression between two time-series.
    # SUM is linear so sum(A ± B) = sum(A) ± sum(B); push the aggregation
    # down to each operand and return a binary_expr the pipeline can handle.
    # Division and multiplication are not linear: sum(A/B) ≠ sum(A)/sum(B),
    # so those patterns are marked not_feasible rather than silently dropped.
    if child.family == "binary_expr":
        inner_left = child.extra.get("left_frag")
        inner_right = child.extra.get("right_frag")
        if (
            child.binary_op in {"+", "-"}
            and frag.outer_agg == "sum"
            and inner_left
            and inner_right
            and not inner_left.extra.get("not_feasible_reasons")
            and not inner_right.extra.get("not_feasible_reasons")
        ):
            new_left = _push_outer_agg(inner_left, "sum", frag.group_labels, frag.group_mode)
            new_right = _push_outer_agg(inner_right, "sum", frag.group_labels, frag.group_mode)
            if new_left and new_right:
                new_binary = _make_binary_fragment(expr, new_left, child.binary_op, new_right)
                new_binary.group_labels = list(frag.group_labels)
                new_binary.group_mode = frag.group_mode
                return new_binary
        elif child.binary_op in {"/", "*"}:
            # Constant scaling: agg(X op k) = agg(X) op k.  When one operand
            # is a scalar literal the aggregation distributes over it, so hoist
            # the scalar out and push the aggregation down to the vector side.
            # This covers patterns like max(rate(A[5m]) * 8) or avg(up * 100).
            scalar_side = None
            vector_side = None
            if inner_right is not None and inner_right.is_scalar and inner_right.scalar_value is not None:
                scalar_side = inner_right
                vector_side = inner_left
            elif inner_left is not None and inner_left.is_scalar and inner_left.scalar_value is not None:
                scalar_side = inner_left
                vector_side = inner_right
            if (
                scalar_side is not None
                and vector_side is not None
                and not vector_side.extra.get("not_feasible_reasons")
            ):
                pushed = _push_outer_agg(vector_side, frag.outer_agg, frag.group_labels, frag.group_mode)
                if pushed is not None:
                    # Preserve order for non-commutative division (k / agg(X)).
                    if child.binary_op == "/" and scalar_side is inner_left:
                        new_binary = _make_binary_fragment(expr, scalar_side, "/", pushed)
                    else:
                        new_binary = _make_binary_fragment(expr, pushed, child.binary_op, scalar_side)
                    new_binary.group_labels = list(frag.group_labels)
                    new_binary.group_mode = frag.group_mode
                    if _contains_join_frag(vector_side):
                        new_binary.extra["stripped_join"] = True
                    return new_binary
            # Two true time-series operands — multiplication/division is not
            # linearisable: agg(A op B) ≠ agg(A) op agg(B).
            _append_not_feasible_reason(
                frag,
                f"Aggregating over a per-element {child.binary_op} between two time-series "
                f"({frag.outer_agg}(A {child.binary_op} B)) cannot be expressed accurately in ES|QL; "
                "rewrite as a ratio of aggregates if the series are label-aligned",
            )

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
        # When left is a binary_expr (e.g. -(A+B) < 0), propagate left_frag/right_frag
        # so the formula plan can still decompose the expression.
        if left.family == "binary_expr":
            for key in ("left_frag", "right_frag"):
                if key in left.extra and key not in frag.extra:
                    frag.extra[key] = left.extra[key]
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

    # Set operators (``or``/``and``/``unless``) are not joins; they have
    # set-union/intersection/difference semantics that preserve operands'
    # label sets. Even though the parser models them with a ManyToMany
    # cardinality modifier, mig-to-kbn's join translation path is wrong
    # for them. Route them to the binary_expr family so the formula plan
    # builder can either apply the safe same-metric ``or`` rewrite or
    # refuse the translation honestly.
    if op.lower() in _SET_OPERATORS:
        frag = _make_binary_fragment(expr, left, op.lower(), right)
        frag.extra.setdefault("parser_backend", "ast")
        if modifier:
            frag.extra["vector_matching"] = _ast_binary_matching(modifier)
        return frag

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
        if child.is_scalar and child.scalar_value is not None:
            frag = _new_fragment(expr, family="scalar")
            frag.is_scalar = True
            frag.scalar_value = -child.scalar_value
            return frag
        if child.family == "binary_expr":
            # Rewrite -(A op B) as 0 - (A op B) so _make_binary_fragment
            # preserves left_frag/right_frag; without this, _copy_fragment_summary
            # drops those extra keys and the formula plan cannot extract a metric name.
            zero = _new_fragment("0", family="scalar")
            zero.is_scalar = True
            zero.scalar_value = 0.0
            return _make_binary_fragment(expr, zero, "-", child)
        frag = _copy_fragment_summary(_new_fragment(expr), child)
        return frag

    if node_type == "NumberLiteral":
        frag = _new_fragment(expr, family="scalar")
        frag.is_scalar = True
        frag.scalar_value = float(getattr(node, "val", 0.0))
        return frag

    if node_type == "StringLiteral":
        frag = _new_fragment(expr, family="string_literal")
        frag.extra["string_value"] = str(getattr(node, "val", "") or "")
        return frag

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


def _restore_sanitized_labels(frag, label_map):
    if not frag or not label_map:
        return frag
    for matcher in frag.matchers:
        label = matcher.get("label")
        if label in label_map:
            matcher["label"] = label_map[label]
    frag.group_labels = [label_map.get(label, label) for label in frag.group_labels]
    for key in ("inner_group", "join_labels"):
        labels = frag.extra.get(key)
        if isinstance(labels, list):
            frag.extra[key] = [label_map.get(label, label) for label in labels]
    matching = frag.extra.get("vector_matching")
    if isinstance(matching, dict) and isinstance(matching.get("labels"), list):
        matching["labels"] = [label_map.get(label, label) for label in matching["labels"]]
    for child in _iter_fragment_children(frag):
        _restore_sanitized_labels(child, label_map)
    return frag


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
        sanitized_expr, label_map = _sanitize_promql_labels_for_ast(expr)
        if label_map and sanitized_expr != expr:
            try:
                ast = promql_parser.parse(sanitized_expr)
            except (ValueError, TypeError, Exception):
                pass
            else:
                frag = _ast_from_node(ast, expr)
                _restore_sanitized_labels(frag, label_map)
                frag.extra.setdefault("parser_backend", "ast_sanitized")
                return frag
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
        bool(re.search(r"\$\w", m.get("value", "")))
        or (m.get("op") == "=~" and m.get("value", "").strip() == ".*")
        or m.get("value", "").startswith("label_")
        or m.get("value", "").startswith("^label_")
        for m in frag.matchers
    )
    return filters, had_vars


def _summary_mode_from_metadata(metadata):
    return bool((metadata or {}).get("summary_mode"))


def _merge_group_fields(explicit_fields, preferred_fields, preferred_origin=None):
    if preferred_origin == "legend" and explicit_fields:
        return explicit_fields
    if not preferred_fields:
        return explicit_fields
    merged = list(preferred_fields)
    for field_name in explicit_fields:
        if field_name not in merged:
            merged.append(field_name)
    return merged


def _frag_group_labels(frag, resolver, preferred_labels=None, preferred_origin=None):
    """Resolve fragment group labels through the resolver.

    Labels that start with ``label_`` are preprocessed Grafana template
    variables (``$Var`` → ``label_Var``) and are silently dropped; keeping
    them would emit non-existent field names in the BY clause.
    """
    raw = [lbl for lbl in (frag.group_labels or []) if not lbl.startswith("label_")]
    explicit = resolver.resolve_labels(raw) if resolver else list(raw)
    preferred = resolver.resolve_labels(preferred_labels or []) if resolver else list(preferred_labels or [])
    return _merge_group_fields(explicit, preferred, preferred_origin=preferred_origin)


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
    # Use ``MAX(field)`` instead of ``LAST(field, time_bucket)`` so the
    # collapse is null-safe across multi-target TS queries. When the
    # upstream STATS aggregates several metrics with implicit
    # ``_timeseries`` grouping, each per-series row has one non-null
    # column and nulls for the other series. ``LAST`` may pick any of
    # those rows and return null. ``MAX`` ignores nulls, so it returns
    # the actual measurement. The semantics are identical to ``LAST``
    # for monotonically-bucketed gauges and stats; this was surfaced by
    # reviewing the Node Exporter Full "Pressure" bar chart, which had
    # data in every bucket but rendered all-null bars.
    reduced = ", ".join(
        f"{field} = MAX({field})" for field in keep_fields
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


def _is_variable_driven_matcher(m):
    """Return True for matchers that originate from Grafana template variables.

    ``preprocess_grafana_macros`` converts ``=~"$var"`` inside ``{}`` to
    ``=~".*"`` (match-all catch-all) and converts remaining ``$var`` tokens to
    ``label_var``.  Both forms are variable-driven and should not contribute to
    the alias suffix — they are the same across binary-expression operands and
    would produce identical suffixes even when a static distinguishing matcher
    (e.g. ``status!~"[4-5].*"``) is present.

    Also handles anchored forms like ``^label_Container$`` that arise when the
    original matcher had regex anchors around the variable (``^$Container$``).
    """
    v = str(m.get("value", ""))
    # label_Var (bare preprocessed variable) or ^label_Var* (anchored form)
    return v == ".*" or v.startswith("label_") or v.startswith("$") or v.startswith("^label_")


def _is_phantom_grafana_var(frag):
    """Return True when *frag* is a bare Grafana variable masquerading as a metric.

    ``preprocess_grafana_macros`` converts a bare ``$var`` token that appears
    outside curly-brace label selectors to ``label_var`` — a name that the
    PromQL parser accepts as a vector selector but which resolves to nothing in
    ES|QL.  When such a fragment appears as one operand of a ``*`` or ``/``
    expression (e.g. ``rate(A) * $trends``) it behaves as a user-supplied
    scalar constant.  Stripping it and emitting only the other operand is safe
    for multiplicative binary ops.
    """
    if frag is None:
        return False
    return (
        frag.family == "simple_metric"
        and frag.metric.startswith("label_")
        and not frag.matchers
        and not frag.range_func
        and not frag.outer_agg
        and not frag.is_scalar
    )


def _matcher_alias_suffix(frag):
    # Prefer non-variable matchers so that when both operands of a binary_expr
    # share the same variable-driven matchers (e.g. controller_pod=~".*"), the
    # distinguishing static matcher (e.g. status!~"[4-5].*") contributes to
    # the alias.  Without this, both operands produce identical aliases and
    # _build_shared_measure_pipeline incorrectly treats them as duplicates.
    static = [m for m in frag.matchers if not _is_variable_driven_matcher(m)]
    source = (static or frag.matchers)[:2]
    parts = []
    for matcher in source:
        label = re.sub(r"[^a-zA-Z0-9_]", "_", matcher["label"]).strip("_")
        value = re.sub(r"[^a-zA-Z0-9_]", "_", matcher["value"]).strip("_")[:12]
        if label or value:
            parts.append("_".join(part for part in (label, value) if part))
    if frag.range_func:
        parts.append(frag.range_func)
    if frag.outer_agg:
        parts.append(frag.outer_agg)
    return "_".join(part for part in parts if part)


def _field_is_proven_tsds_gauge(metric_name, resolver):
    """Return True iff resolver proves the metric field is a TSDS gauge.

    "Proven" means the resolver has a non-empty field capability for the metric
    (or its resolved physical name) with ``time_series_metric=gauge``, numeric
    type family, and no conflicting type mappings. This signal lets the
    translator emit ``TS`` (which has time-series-aware aggregation semantics)
    instead of ``FROM`` (which sums every per-sample doc) for the field — see
    issue #8.
    """
    if not metric_name or not resolver:
        return False
    capability = resolver.field_capability(metric_name)
    if capability is None:
        resolved = _resolve_metric_field(resolver, metric_name, prefer="gauge")
        if resolved and resolved != metric_name:
            capability = resolver.field_capability(resolved)
    if not capability:
        return False
    if capability.conflicting_types:
        return False
    if capability.time_series_metric_kind != "gauge":
        return False
    return capability.type_family == "numeric"


def _can_use_direct_ts_gauge(metric_name, resolver, group_fields, frag):
    if group_fields:
        return False
    if frag and frag.extra.get("wrapped_scalar"):
        return False
    return _field_is_proven_tsds_gauge(metric_name, resolver)


def _build_measure_spec(
    frag,
    resolver,
    rule_pack,
    alias_hint="",
    summary_mode=False,
    preferred_group_labels=None,
    allow_direct_ts_gauge=True,
    preferred_group_labels_origin=None,
    allow_tsds_gauge_promotion=True,
):
    if not frag or (not frag.metric and frag.family != "uptime"):
        return None

    filters, had_vars = _frag_filters(frag, resolver)
    warnings = []
    if had_vars:
        warnings.append("Dropped variable-driven label filters during migration")
    group_fields = _frag_group_labels(
        frag,
        resolver,
        preferred_group_labels,
        preferred_origin=preferred_group_labels_origin,
    )
    if alias_hint:
        suffix = alias_hint
    else:
        suffix = _matcher_alias_suffix(frag)
    alias = _safe_alias(frag.metric, suffix)
    final_alias = None
    eval_expr = ""

    metric_field = frag.metric

    if frag.family == "simple_metric":
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        can_use_direct_ts_gauge = allow_direct_ts_gauge and _can_use_direct_ts_gauge(
            frag.metric, resolver, group_fields, frag
        )
        # Issue #8: keep TS for proven TSDS gauges whenever the direct-gauge path
        # isn't available — either because of group_fields or because the caller
        # disabled it (multi-target fusion uses ``allow_direct_ts_gauge=False``
        # since ``STATS field = field`` cannot be CASE-wrapped, but ``AVG(field)``
        # can). ``FROM`` against a TSDS sums every per-sample doc and inflates the
        # value, so use ``TS`` with the default aggregator instead.
        can_use_ts_aggregated_gauge = (
            allow_tsds_gauge_promotion
            and (not is_counter)
            and (not can_use_direct_ts_gauge)
            and (not (frag.extra.get("wrapped_scalar") if frag else False))
            and _field_is_proven_tsds_gauge(frag.metric, resolver)
        )
        if is_counter:
            source = "TS"
            time_filter = rule_pack.ts_time_filter
            bucket_expr = rule_pack.ts_bucket
            metric_field = _resolve_metric_field(resolver, frag.metric, prefer="counter")
            # Bare counter reference: use LAST_OVER_TIME to return the raw cumulative
            # value per TBUCKET window, matching PromQL instant-vector semantics.
            stats_expr = f"MAX(LAST_OVER_TIME({metric_field}))"
            warnings.append("Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value")
        elif can_use_direct_ts_gauge:
            source = "TS"
            time_filter = rule_pack.ts_time_filter
            bucket_expr = rule_pack.ts_bucket
            metric_field = _resolve_metric_field(resolver, frag.metric, prefer="gauge")
            stats_expr = metric_field
        elif can_use_ts_aggregated_gauge:
            source = "TS"
            time_filter = rule_pack.ts_time_filter
            bucket_expr = rule_pack.ts_bucket
            default_agg = rule_pack.default_gauge_agg.upper()
            metric_field = _resolve_metric_field(resolver, frag.metric, prefer="gauge")
            stats_expr = f"{default_agg}({metric_field})"
            warnings.append(f"No explicit aggregation; using {default_agg} (correct for gauge metrics)")
        else:
            source = "FROM"
            time_filter = rule_pack.from_time_filter
            bucket_expr = rule_pack.from_bucket
            default_agg = rule_pack.default_gauge_agg.upper()
            metric_field = _resolve_metric_field(resolver, frag.metric, prefer="gauge")
            stats_expr = f"{default_agg}({metric_field})"
            if frag.extra.get("wrapped_scalar"):
                warnings.append("Approximated scalar() as a direct metric value")
            else:
                warnings.append(f"No explicit aggregation; using {default_agg} (correct for gauge metrics)")
    elif frag.family == "simple_agg":
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        if frag.outer_agg == "count" and is_counter:
            return None
        # Issue #8: gauge aggregations against a proven TSDS must use TS, not FROM —
        # FROM sums every per-sample doc instead of one value per series per bucket.
        is_proven_tsds_gauge = (
            allow_tsds_gauge_promotion
            and (not is_counter)
            and _field_is_proven_tsds_gauge(frag.metric, resolver)
        )
        source = "TS" if (is_counter or is_proven_tsds_gauge) else "FROM"
        time_filter = rule_pack.ts_time_filter if source == "TS" else rule_pack.from_time_filter
        bucket_expr = rule_pack.ts_bucket if source == "TS" else rule_pack.from_bucket
        if is_counter and frag.outer_agg != "count":
            metric_field = _resolve_metric_field(resolver, frag.metric, prefer="counter")
            # Bare counter aggregation: use LAST_OVER_TIME as inner function so the
            # outer aggregation operates on raw cumulative values, not rates.
            inner_expr = f"LAST_OVER_TIME({metric_field})"
            warnings.append("Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value")
        else:
            metric_field = _resolve_metric_field(resolver, frag.metric, prefer="gauge")
            inner_expr = metric_field
        outer = OUTER_AGG_MAP.get(frag.outer_agg, rule_pack.default_gauge_agg.upper())
        stats_expr = f"{outer}({inner_expr})"
    elif frag.family == "range_agg":
        esql_inner = AGG_FUNCTION_MAP.get(frag.range_func)
        if not esql_inner:
            return None
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        # ES|QL's RATE / IRATE / INCREASE require a ``counter_*`` typed
        # field. Some Prometheus counters don't end in ``_total`` (kernel
        # vmstat / netstat / softnet); Elastic's ``/_prometheus/api/v1/write``
        # ingest types those as ``gauge`` based on the metric-name
        # heuristic, and the resulting ES|QL panel hard-fails with
        # ``first argument of [RATE(...)] must be counter``. Degrade the
        # query to a gauge-equivalent so the panel still renders honest
        # numbers and surface a warning.
        if (
            not is_counter
            and frag.range_func in {"rate", "irate", "increase"}
        ):
            esql_inner, warning = _gauge_fallback_for_counter_range_func(frag.range_func)
            warnings.append(warning.format(metric=frag.metric))
        needs_ts = is_counter or frag.range_func in AGG_FUNCTION_MAP
        source = "TS" if needs_ts else "FROM"
        time_filter = rule_pack.ts_time_filter if source == "TS" else rule_pack.from_time_filter
        bucket_expr = rule_pack.ts_bucket if source == "TS" else rule_pack.from_bucket
        prefer = "counter" if (frag.range_func in {"rate", "irate", "increase"} and is_counter) else "gauge"
        metric_field = _resolve_metric_field(resolver, frag.metric, prefer=prefer)
        inner_expr = f"{esql_inner}({metric_field}, {frag.range_window})"
        outer = OUTER_AGG_MAP.get(frag.outer_agg, "") if frag.outer_agg else ""
        if not outer and source == "TS" and group_fields:
            stats_expr = f"AVG({inner_expr})"
            warnings.append(
                f"Added outer AVG() around {frag.range_func} because ES|QL requires an outer aggregation "
                "when grouping TS functions by label fields"
            )
        else:
            stats_expr = f"{outer}({inner_expr})" if outer else inner_expr
    elif frag.family == "scaled_agg":
        esql_inner = AGG_FUNCTION_MAP.get(frag.range_func)
        if not esql_inner:
            return None
        source = "TS"
        time_filter = rule_pack.ts_time_filter
        bucket_expr = rule_pack.ts_bucket
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        if (
            not is_counter
            and frag.range_func in {"rate", "irate", "increase"}
        ):
            esql_inner, warning = _gauge_fallback_for_counter_range_func(frag.range_func)
            warnings.append(warning.format(metric=frag.metric))
        esql_outer = OUTER_AGG_MAP.get(frag.outer_agg, "AVG")
        prefer = "counter" if (frag.range_func in {"rate", "irate", "increase"} and is_counter) else "gauge"
        metric_field = _resolve_metric_field(resolver, frag.metric, prefer=prefer)
        stats_expr = f"{esql_outer}({esql_inner}({metric_field}, {frag.range_window}))"
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
        metric_field = _resolve_metric_field(resolver, start_metric, prefer="gauge")
        stats_expr = f"MAX({metric_field} * 1000)"
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
        metric_field=metric_field,
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
            # Divergent per-target filters must be CASE-wrapped into the
            # stats_expr. ``_inline_filters_into_stats_expr`` already verifies
            # the expression is shaped as ``AGG(field)`` (returns None otherwise)
            # so the source command (FROM or TS) does not matter — CASE inside
            # an aggregation works the same in either mode (issue #8 follow-up).
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
        physical_field = str(spec.metric_field or spec.metric_name or "").strip()
        if physical_field and physical_field not in presence_metrics:
            presence_metrics.append(physical_field)
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


def _try_rewrite_set_or_same_metric(
    frag,
    resolver,
    rule_pack,
    alias_hint="",
    summary_mode=False,
    preferred_group_labels=None,
    allow_direct_ts_gauge=False,
    preferred_group_labels_origin=None,
):
    """Rewrite ``A{f1} or A{f2}`` (and longer chains of same-metric ``or``)
    as a single MeasureSpec whose filters union the operands' matchers.

    This is the one set-operator case that has an honest single-stage
    ES|QL equivalent: PromQL's ``or`` of same-metric instant vectors is
    set union over distinct matcher tuples, which is the same as a
    single fetch of the metric with an ``OR`` over the matcher filter
    sets. Each operand must:

    - Be a leaf metric reference (``simple_metric`` family) — no inner
      rate/aggregation, since rate over a unioned filter still produces
      the right rate per resulting series.
    - Reference the **same** metric name on both sides.
    - Resolve to the **same** non-filter matcher structure (same
      grouping labels, same range/agg shape if any).

    For anything else we return ``None`` so the caller refuses the
    translation.
    """
    op_lower = (frag.binary_op or "").lower()
    if op_lower != "or":
        return None

    left_frag = frag.extra.get("left_frag")
    right_frag = frag.extra.get("right_frag")
    if not left_frag or not right_frag:
        return None

    # Recurse first into a left-leaning ``or`` chain so ``A or A or A``
    # works.
    operand_frags = []
    for child in (left_frag, right_frag):
        if child.family == "binary_expr" and (child.binary_op or "").lower() == "or":
            sub = _try_rewrite_set_or_same_metric(
                child,
                resolver,
                rule_pack,
                alias_hint=alias_hint,
                summary_mode=summary_mode,
                preferred_group_labels=preferred_group_labels,
                preferred_group_labels_origin=preferred_group_labels_origin,
            )
            if sub is None:
                return None
            if len(sub.specs) != 1:
                return None
            # Re-extract the operand fragments out of the nested ``or``
            # chain so the unified filter logic below sees a flat list.
            stack = [child]
            while stack:
                cur = stack.pop()
                if cur.family == "binary_expr" and (cur.binary_op or "").lower() == "or":
                    stack.append(cur.extra.get("left_frag"))
                    stack.append(cur.extra.get("right_frag"))
                else:
                    operand_frags.append(cur)
        else:
            operand_frags.append(child)

    if not operand_frags:
        return None

    # All operands must be simple metric references against the same
    # metric. Range functions, outer aggregations, joins etc. all have
    # set-union semantics that differ from a plain matcher OR.
    metrics = {f.metric for f in operand_frags}
    if len(metrics) != 1 or "" in metrics:
        return None
    if any(f.family != "simple_metric" for f in operand_frags):
        return None
    if any(f.binary_op for f in operand_frags):
        return None
    if any(f.outer_agg or f.range_func for f in operand_frags):
        return None

    # Build a single MeasureSpec from the first operand, then OR-fold
    # the other operands' filter strings into its WHERE clause.
    base = _build_measure_spec(
        operand_frags[0],
        resolver,
        rule_pack,
        alias_hint=alias_hint,
        summary_mode=summary_mode,
        preferred_group_labels=preferred_group_labels,
        allow_direct_ts_gauge=allow_direct_ts_gauge,
        preferred_group_labels_origin=preferred_group_labels_origin,
    )
    if base is None:
        return None

    per_operand_filters = [list(base.filters)]
    for other in operand_frags[1:]:
        spec = _build_measure_spec(
            other,
            resolver,
            rule_pack,
            alias_hint=alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
            allow_direct_ts_gauge=allow_direct_ts_gauge,
            preferred_group_labels_origin=preferred_group_labels_origin,
        )
        if spec is None:
            return None
        # The non-filter parts of each MeasureSpec (source_type, stats,
        # grouping) must agree for the union to be safe.
        if (
            spec.source_type != base.source_type
            or spec.stats_expr != base.stats_expr
            or spec.bucket_expr != base.bucket_expr
            or spec.group_fields != base.group_fields
        ):
            return None
        per_operand_filters.append(list(spec.filters))

    # Compute the AND-intersection of filter clauses that are identical
    # across every operand (those become unconditional WHERE clauses).
    # The remaining per-operand filter clauses are OR'd together inside
    # a single combined WHERE.
    if all(filt_list == per_operand_filters[0] for filt_list in per_operand_filters):
        unified_filters = per_operand_filters[0]
    else:
        common = []
        for filt in per_operand_filters[0]:
            if all(filt in other for other in per_operand_filters[1:]):
                common.append(filt)
        remainders = []
        for filt_list in per_operand_filters:
            rest = [f for f in filt_list if f not in common]
            if not rest:
                # An operand with no distinguishing filter means "match
                # everything"; the union is therefore unfiltered.
                remainders = []
                break
            if len(rest) == 1:
                remainders.append(rest[0])
            else:
                remainders.append("(" + " AND ".join(rest) + ")")
        if remainders:
            common.append("(" + " OR ".join(remainders) + ")")
        unified_filters = common

    # The labels that differ across operands (e.g. ``status`` in
    # ``A{status=~"4.."} or A{status=~"5.."}``) are the dimensions
    # PromQL's set-or uses to keep operand series separate. The
    # straightforward unified WHERE we just built would otherwise
    # average them together and lose the operands' distinguishing
    # dimensions. Promote any such labels to additional BY columns so
    # the rate is computed per-(method, path, status, …) tuple, which
    # matches PromQL's per-series output. Labels that the resolver
    # cannot map to a known field are skipped.
    distinguishing_labels = _set_or_distinguishing_labels(operand_frags)
    if distinguishing_labels:
        resolved = []
        for label in distinguishing_labels:
            field = resolver.resolve_label(label) if resolver else label
            if field and field not in base.group_fields and field not in resolved:
                resolved.append(field)
        if resolved:
            new_group_fields = list(base.group_fields) + resolved
        else:
            new_group_fields = base.group_fields
    else:
        new_group_fields = base.group_fields

    new_spec = dataclasses.replace(
        base,
        filters=unified_filters,
        group_fields=new_group_fields,
        warnings=list(base.warnings)
        + [
            "Rewrote PromQL set-or between same metric as a unified WHERE OR clause"
        ],
    )
    return FormulaPlan(
        specs=[new_spec],
        expr=new_spec.final_alias,
        warnings=list(new_spec.warnings),
    )


def _set_or_distinguishing_labels(operand_frags):
    """Return the matcher labels that differ across operands of an
    ``A{...} or A{...}`` (or longer chain) so the rewrite can promote
    them to BY columns and preserve the union's distinguishing dimensions.
    """
    by_label_values = {}
    for frag in operand_frags:
        for matcher in frag.matchers:
            label = matcher.get("label")
            if not label:
                continue
            by_label_values.setdefault(label, set()).add(
                (matcher.get("op", "="), matcher.get("value", ""))
            )
    return [label for label, values in by_label_values.items() if len(values) > 1]


def _build_formula_plan(
    frag,
    resolver,
    rule_pack,
    alias_hint="",
    summary_mode=False,
    preferred_group_labels=None,
    allow_direct_ts_gauge=True,
    preferred_group_labels_origin=None,
    allow_tsds_gauge_promotion=True,
):
    scalar_expr = _scalar_fragment_expr(frag)
    if scalar_expr is not None:
        return FormulaPlan(specs=[], expr=scalar_expr)

    # An outer aggregation wrapped around a group_left/group_right vector-matching
    # join (e.g. sum(rate(A) * on(ns,pod) group_left(w,wt) B) by (pod)) produces
    # family='unknown' with extra['vector_matching'] set and the join RHS on
    # binary_rhs.  _build_measure_spec has no 'unknown' handler and returns None,
    # blocking multi-target fusion and ratio expressions.  Strip the join RHS and
    # re-classify as the appropriate aggregate family so the primary metric can
    # participate in both.  Label enrichment from the join is silently dropped —
    # identical to what join_family_rule does in translate.py.
    if (
        frag
        and frag.family == "unknown"
        and frag.extra.get("vector_matching")
        and frag.binary_op == "*"
    ):
        stripped_fields = {f.name: getattr(frag, f.name) for f in dataclasses.fields(frag)}
        if frag.range_func:
            stripped_fields["family"] = "range_agg"
        elif frag.outer_agg:
            stripped_fields["family"] = "simple_agg"
        else:
            stripped_fields["family"] = "simple_metric"
        stripped_fields["binary_op"] = ""
        stripped_fields["binary_rhs"] = None
        stripped = PromQLFragment(**stripped_fields)
        plan = _build_formula_plan(
            stripped,
            resolver,
            rule_pack,
            alias_hint=alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
            allow_direct_ts_gauge=allow_direct_ts_gauge,
            preferred_group_labels_origin=preferred_group_labels_origin,
            allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
        )
        if plan and "Dropped group_left label enrichment" not in (plan.warnings or []):
            plan.warnings.append("Dropped group_left label enrichment; kept primary metric series only")
        return plan

    # A bare group_left/group_right vector-matching join without an outer
    # aggregation (e.g. ``A * on(chip) group_left(chip_name) B``) lands as
    # family='join' in the fragment parser.  _build_measure_spec has no
    # 'join' handler and returns None, blocking multi-target fusion.  Strip
    # the join RHS — identical to what join_family_rule does in translate.py
    # for the ``binary_op == '*'`` branch — and delegate to the left_frag
    # which already carries the correct metric and family.  Use join_labels
    # as the fallback preferred group fields so the resulting spec retains the
    # label enrichment fields (e.g. chip_name) that the join was providing.
    if (
        frag
        and frag.family == "join"
        and frag.binary_op == "*"
        and frag.extra.get("left_frag")
    ):
        left_frag = frag.extra["left_frag"]
        join_labels = frag.extra.get("join_labels", []) or []
        effective_preferred = preferred_group_labels or (join_labels if join_labels else None)
        plan = _build_formula_plan(
            left_frag,
            resolver,
            rule_pack,
            alias_hint=alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=effective_preferred,
            allow_direct_ts_gauge=allow_direct_ts_gauge,
            preferred_group_labels_origin=preferred_group_labels_origin,
            allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
        )
        if plan and "Dropped group_left label enrichment" not in (plan.warnings or []):
            plan.warnings.append("Dropped group_left label enrichment; kept primary metric series only")
        return plan

    if frag and frag.family == "binary_expr":
        # Set operators (``or`` / ``and`` / ``unless``) are not arithmetic
        # and cannot be composed by interpolating the operands into a single
        # EVAL expression. PromQL's ``or`` is set union, ``and`` is set
        # intersection, ``unless`` is set difference — all preserve the
        # operands' label set and have no honest single-stage ES|QL
        # equivalent. We handle one common, safe rewrite below
        # (``A{f1} or A{f2}`` → ``A WHERE f1 OR f2``) and refuse everything
        # else so the rule layer can mark the panel ``not_feasible``
        # instead of silently dropping one operand or every breakdown
        # label.
        op_lower = (frag.binary_op or "").lower()
        if op_lower in _SET_OPERATORS:
            rewritten = _try_rewrite_set_or_same_metric(
                frag,
                resolver,
                rule_pack,
                alias_hint=alias_hint,
                summary_mode=summary_mode,
                preferred_group_labels=preferred_group_labels,
                allow_direct_ts_gauge=False,
                preferred_group_labels_origin=preferred_group_labels_origin,
            )
            if rewritten is not None:
                return rewritten
            return None

        # When a Grafana variable like ``$trends`` is used as a scalar
        # multiplier/divisor (e.g. ``rate(A) * $trends``), the preprocessor
        # converts it to ``label_trends`` — a bare simple_metric with no
        # matchers or aggregation.  This "phantom metric" can never be queried
        # from ES|QL, so strip it from ``*`` / ``÷`` expressions and emit the
        # remaining operand unchanged.  ``+`` / ``-`` are left alone: adding a
        # phantom metric would change the numeric value.
        if op_lower in ("*", "/"):
            left_frag_peek = frag.extra.get("left_frag")
            right_frag_peek = frag.extra.get("right_frag")
            phantom_side = None
            real_side = None
            if _is_phantom_grafana_var(right_frag_peek):
                phantom_side = right_frag_peek
                real_side = left_frag_peek
            elif op_lower == "*" and _is_phantom_grafana_var(left_frag_peek):
                phantom_side = left_frag_peek
                real_side = right_frag_peek
            if phantom_side is not None and real_side is not None:
                plan = _build_formula_plan(
                    real_side,
                    resolver,
                    rule_pack,
                    alias_hint=alias_hint,
                    summary_mode=summary_mode,
                    preferred_group_labels=preferred_group_labels,
                    allow_direct_ts_gauge=allow_direct_ts_gauge,
                    preferred_group_labels_origin=preferred_group_labels_origin,
                    allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
                )
                if plan:
                    var_name = (phantom_side.metric or "").removeprefix("label_") or "var"
                    if (
                        f"Grafana variable ${var_name} dropped"
                        not in (plan.warnings or [])
                    ):
                        plan.warnings.append(
                            f"Grafana variable ${var_name} used as scalar "
                            f"multiplier/divisor was dropped; chart values unscaled"
                        )
                    return plan

        left_plan = _build_formula_plan(
            frag.extra.get("left_frag"),
            resolver,
            rule_pack,
            alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
            allow_direct_ts_gauge=False,
            preferred_group_labels_origin=preferred_group_labels_origin,
            allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
        )
        right_plan = _build_formula_plan(
            frag.extra.get("right_frag"),
            resolver,
            rule_pack,
            alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
            allow_direct_ts_gauge=False,
            preferred_group_labels_origin=preferred_group_labels_origin,
            allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
        )
        if not left_plan or not right_plan:
            return None
        # If one operand was promoted to TS (proven TSDS gauge) but the other
        # stayed on FROM (unknown / non-TSDS), rebuild both with promotion
        # disabled so they share a source command. FROM is the safe common
        # denominator; promoting unknown operands to TS would risk runtime
        # errors on non-TSDS fields. Same-source operands skip this rebuild.
        if allow_tsds_gauge_promotion:
            left_sources = {spec.source_type for spec in left_plan.specs}
            right_sources = {spec.source_type for spec in right_plan.specs}
            all_sources = left_sources | right_sources
            if len(all_sources) > 1:
                return _build_formula_plan(
                    frag,
                    resolver,
                    rule_pack,
                    alias_hint=alias_hint,
                    summary_mode=summary_mode,
                    preferred_group_labels=preferred_group_labels,
                    allow_direct_ts_gauge=allow_direct_ts_gauge,
                    preferred_group_labels_origin=preferred_group_labels_origin,
                    allow_tsds_gauge_promotion=False,
                )
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
        allow_direct_ts_gauge=allow_direct_ts_gauge,
        preferred_group_labels_origin=preferred_group_labels_origin,
        allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
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
    "_inline_filters_into_stats_expr",
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
