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
    for token in _BARE_METRIC_RE.findall(scrubbed):
        if token in _PROMQL_KEYWORDS:
            continue
        if token.replace(".", "").isdigit():
            continue
        metrics.add(token)
    return metrics


def build_metric_series_labels(dashboard: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not isinstance(dashboard, dict):
        return {}

    for expr in _iter_panel_exprs(dashboard):
        without_labels = set()
        for body in _WITHOUT_RE.findall(expr):
            without_labels.update(_split_labels(body))
        expr_metrics = _metrics_in_expr(expr)

        # by(...) labels apply to the metrics aggregated in this expression.
        for body in _BY_RE.findall(expr):
            for label in _split_labels(body):
                if label in without_labels:
                    continue
                for metric in expr_metrics:
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
