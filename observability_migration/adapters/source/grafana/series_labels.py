# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline, dashboard-wide mining of per-metric series-dimension labels.

PromQL renders one series per natural label set. When a bare gauge selector names no labels of its
own, we recover its likely series dimensions from *other* signals in the same dashboard:

* labels in any panel's ``by(...)`` clause for the metric,
* labels named by ``label_values(metric, label)`` template variables,
* variable-driven / regex label filters (e.g. ``instance="$inst"`` or ``pod=~"web-.*"``).

Single-value equality filters (``instance="x"``) pin the value and are excluded; ``without(...)``
clauses are ignored. The result is a deterministic ``metric -> [labels]`` map (first-seen order,
capped) used only to backfill grouping when the panel itself provides none.
"""

from __future__ import annotations

import re

# Cap inferred labels per metric; above this we prefer the honest-warning fallback over guessing.
MAX_INFERRED_LABELS = 3

_METRIC_RE = r"[a-zA-Z_:][a-zA-Z0-9_:]*"

# PromQL keywords / aggregation operators / common functions that are NOT metric names. Used to
# keep the bare-identifier scanner from treating syntax tokens as metrics.
_PROMQL_KEYWORDS = frozenset(
    {
        "by", "without", "on", "ignoring", "group_left", "group_right", "offset", "bool",
        "and", "or", "unless", "inf", "nan",
        "sum", "avg", "min", "max", "count", "count_values", "stddev", "stdvar", "group",
        "topk", "bottomk", "quantile",
        "rate", "irate", "increase", "delta", "idelta", "deriv", "predict_linear",
        "histogram_quantile", "label_replace", "label_join", "absent", "absent_over_time",
        "vector", "scalar", "clamp", "clamp_max", "clamp_min", "sgn", "abs", "ceil", "floor",
        "sqrt", "exp", "ln", "log2", "log10", "round", "time", "timestamp", "changes", "resets",
        "sort", "sort_desc", "acos", "asin", "atan", "atan2", "cos", "sin", "tan", "cosh", "sinh",
        "tanh", "deg", "rad", "pi",
        "rate_interval", "interval", "range",
    }
)

_BY_RE = re.compile(r"\bby\s*\(([^)]*)\)", re.IGNORECASE)
_WITHOUT_RE = re.compile(r"\bwithout\s*\(([^)]*)\)", re.IGNORECASE)
_SELECTOR_RE = re.compile(rf"({_METRIC_RE})\s*\{{([^}}]*)\}}")
_LABEL_FILTER_RE = re.compile(rf"({_METRIC_RE})\s*(=~|!=|!~|=)\s*\"([^\"]*)\"")
_LABEL_VALUES_RE = re.compile(rf"label_values\(\s*({_METRIC_RE})\s*,\s*({_METRIC_RE})\s*\)")
_BARE_METRIC_RE = re.compile(rf"\b({_METRIC_RE})\b(?!\s*\()")
_GROUP_MOD_RE = re.compile(r"\b(?:group_left|group_right|on|ignoring)\s*\([^)]*\)", re.IGNORECASE)


def _split_labels(group_body: str) -> list[str]:
    return [tok.strip() for tok in group_body.split(",") if tok.strip()]


def _iter_panel_exprs(dashboard: dict) -> list[str]:
    exprs: list[str] = []

    def _walk(panels):
        for panel in panels or []:
            if not isinstance(panel, dict):
                continue
            for target in panel.get("targets", []) or []:
                if isinstance(target, dict) and target.get("expr"):
                    exprs.append(str(target["expr"]))
            _walk(panel.get("panels", []) or [])

    _walk(dashboard.get("panels", []) or [])
    return exprs


def _add(out: dict[str, list[str]], metric: str, label: str) -> None:
    if not metric or not label or label in _PROMQL_KEYWORDS:
        return
    bucket = out.setdefault(metric, [])
    if label not in bucket:
        bucket.append(label)


def expr_has_explicit_grouping(expr: str) -> bool:
    """True if a PromQL expression declares its own grouping via ``by(...)``/``without(...)``.

    A panel whose query carries an explicit grouping clause has already stated its
    series dimensions, so the dashboard-wide series-label backfill (which exists only
    to recover dimensions for *bare* selectors) must not override that intent.
    """
    text = str(expr or "")
    return bool(_BY_RE.search(text) or _WITHOUT_RE.search(text))


def _metrics_in_expr(expr: str) -> set[str]:
    """Best-effort set of metric names referenced in a PromQL expression.

    Prefers explicit selectors (``metric{...}``); also accepts bare identifiers that are not
    PromQL keywords / function names and are not immediately followed by ``(`` (a function call).
    Identifiers that appear only as label names inside ``{...}`` or ``by(...)`` are excluded.
    """
    metrics: set[str] = {m.group(1) for m in _SELECTOR_RE.finditer(expr)}

    # Strip selector bodies and by/without bodies so their label names aren't scanned as metrics.
    scrubbed = _SELECTOR_RE.sub(lambda m: f"{m.group(1)} ", expr)
    scrubbed = _BY_RE.sub(" ", scrubbed)
    scrubbed = _WITHOUT_RE.sub(" ", scrubbed)
    # Grafana injects variables such as $__rate_interval inside range selectors.
    # They are duration/control tokens, not Prometheus metric names.
    scrubbed = re.sub(r"\$[A-Za-z_:][A-Za-z0-9_:]*", " ", scrubbed)
    for token in _BARE_METRIC_RE.findall(scrubbed):
        if token in _PROMQL_KEYWORDS:
            continue
        if token.replace(".", "").isdigit():
            continue
        metrics.add(token)
    return metrics


def _metrics_in_fragment(fragment: str) -> set[str]:
    """Metrics in an aggregation argument, ignoring vector-matching modifier
    label lists (``group_left(...)`` / ``on(...)``) and case-variant keywords."""
    scrubbed = _GROUP_MOD_RE.sub(" ", fragment)
    return {m for m in _metrics_in_expr(scrubbed) if m.lower() not in _PROMQL_KEYWORDS}


def _balanced_paren_back(text: str, close_idx: int) -> int | None:
    """Index of the ``(`` matching the ``)`` at ``close_idx`` (or None)."""
    depth = 0
    for i in range(close_idx, -1, -1):
        char = text[i]
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth == 0:
                return i
    return None


def _balanced_paren_fwd(text: str, open_idx: int) -> int | None:
    """Index of the ``)`` matching the ``(`` at ``open_idx`` (or None)."""
    depth = 0
    for i in range(open_idx, len(text)):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _aggregation_argument(expr: str, by_start: int, by_end: int) -> str:
    """Return the aggregation argument that a ``by(...)`` clause groups.

    PromQL spells grouping two ways around the aggregation's argument list:
    ``sum(<arg>) by (labels)`` (suffix) and ``sum by (labels) (<arg>)`` (prefix).
    We isolate ``<arg>`` so a clause bound to one operand of a larger expression
    is not mis-attributed to sibling metrics. Falls back to the whole expression
    when the structure can't be matched.
    """
    prefix = expr[:by_start].rstrip()
    if prefix.endswith(")"):
        open_idx = _balanced_paren_back(prefix, len(prefix) - 1)
        if open_idx is not None:
            return prefix[open_idx + 1 : len(prefix) - 1]

    rest = expr[by_end:]
    lead = len(rest) - len(rest.lstrip())
    if lead < len(rest) and rest[lead] == "(":
        close_idx = _balanced_paren_fwd(rest, lead)
        if close_idx is not None:
            return rest[lead + 1 : close_idx]

    return expr


def _scoped_by_clauses(expr: str) -> list[tuple[list[str], set[str]]]:
    """For each ``by(...)`` clause, the (labels, metrics-it-groups) pair."""
    pairs: list[tuple[list[str], set[str]]] = []
    for match in _BY_RE.finditer(expr):
        labels = _split_labels(match.group(1))
        if not labels:
            continue
        arg = _aggregation_argument(expr, match.start(), match.end())
        pairs.append((labels, _metrics_in_fragment(arg)))
    return pairs


def build_metric_series_labels(dashboard: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(dashboard, dict):
        return {}

    for expr in _iter_panel_exprs(dashboard):
        without_labels = set()
        for body in _WITHOUT_RE.findall(expr):
            without_labels.update(_split_labels(body))

        # by(...) labels apply only to the metrics inside the aggregation that the
        # clause actually groups. Attributing them to every metric in the
        # expression leaks a sibling operand's grouping onto unrelated metrics
        # (e.g. ``scalar(node_load1) / count(... node_cpu_seconds_total ...) by (cpu)``
        # must not mark node_load1 as per-cpu).
        for labels, metrics in _scoped_by_clauses(expr):
            for label in labels:
                if label in without_labels:
                    continue
                for metric in metrics:
                    _add(out, metric, label)

        # Variable-driven / regex label filters inside selectors.
        for sel in _SELECTOR_RE.finditer(expr):
            metric, body = sel.group(1), sel.group(2)
            for label, op, value in _LABEL_FILTER_RE.findall(body):
                is_variable = "$" in value or value == ""
                is_regex = op in ("=~", "!~")
                if is_variable or is_regex:
                    _add(out, metric, label)

    # label_values(metric, label) template variables.
    for var in (dashboard.get("templating", {}) or {}).get("list", []) or []:
        query = var.get("query") if isinstance(var, dict) else None
        if isinstance(query, dict):
            query = query.get("query")
        for metric, label in _LABEL_VALUES_RE.findall(str(query or "")):
            _add(out, metric, label)

    # Apply the cap: drop metrics whose inferred label set is too large to chart safely.
    return {
        metric: labels
        for metric, labels in out.items()
        if 1 <= len(labels) <= MAX_INFERRED_LABELS
    }
