# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Normalize raw Datadog dashboard JSON into internal IR models."""

from __future__ import annotations

import re
from typing import Any

from .log_parser import parse_log_query
from .models import (
    LOG_DATA_SOURCES,
    METRIC_DATA_SOURCES,
    ConditionalFormat,
    FormulaBinOp,
    FormulaFuncCall,
    FormulaUnary,
    MetricQuery,
    NormalizedDashboard,
    NormalizedWidget,
    TemplateVariable,
    WidgetFormula,
    WidgetQuery,
)
from .query_parser import ParseError, parse_formula, parse_legacy_query, parse_metric_query


def normalize_dashboard(raw: dict[str, Any]) -> NormalizedDashboard:
    """Convert a raw Datadog dashboard dict into a NormalizedDashboard."""
    widgets = []
    layout_type = raw.get("layout_type", "ordered")
    for i, w in enumerate(raw.get("widgets", [])):
        nw = _normalize_widget(w, index=i, parent_layout_type=layout_type)
        if nw:
            widgets.append(nw)

    template_vars = []
    for tv in (raw.get("template_variables") or []):
        template_vars.append(TemplateVariable(
            name=tv.get("name", ""),
            tag=tv.get("tag", tv.get("prefix", "")),
            default=tv.get("default", "*") or "*",
            prefix=tv.get("prefix", ""),
            defaults=tv.get("defaults", []) or [],
            available_values=tv.get("available_values", []) or [],
        ))

    return NormalizedDashboard(
        id=raw.get("id", raw.get("_dd_id", "")),
        title=raw.get("title", "Untitled Dashboard"),
        description=raw.get("description", ""),
        layout_type=layout_type,
        widgets=widgets,
        template_variables=template_vars,
        source_file=raw.get("_source_file", ""),
        raw=raw,
        tags=raw.get("tags", []) or [],
        url=raw.get("url", ""),
    )


def _normalize_widget(
    raw: dict[str, Any],
    index: int = 0,
    parent_layout_type: str = "ordered",
) -> NormalizedWidget | None:
    defn = raw.get("definition", {})
    if not defn:
        return None

    widget_type = defn.get("type", "")
    widget_id = str(raw.get("id", index))
    child_layout_type = defn.get("layout_type", parent_layout_type)

    children: list[NormalizedWidget] = []
    if widget_type in ("group", "powerpack") and "widgets" in defn:
        for ci, cw in enumerate(defn["widgets"]):
            child = _normalize_widget(cw, index=ci, parent_layout_type=child_layout_type)
            if child:
                children.append(child)

    queries, formulas = _extract_queries_and_formulas(defn)

    if not queries and widget_type in ("log_stream", "list_stream"):
        queries = _extract_log_stream_queries(defn)

    conditional_formats = []
    for cf in defn.get("conditional_formats", []):
        conditional_formats.append(ConditionalFormat(
            comparator=cf.get("comparator", ""),
            value=cf.get("value", 0),
            palette=cf.get("palette", ""),
        ))

    layout = raw.get("layout")
    if isinstance(layout, dict) and layout:
        normalized_layout = {
            "x": layout.get("x", 0),
            "y": layout.get("y", 0),
            "width": layout.get("width", 4),
            "height": layout.get("height", 2),
        }
    elif parent_layout_type == "ordered":
        # Ordered dashboards often omit explicit coordinates. Give each widget a
        # synthetic row slot so the Kibana layout can repack them intelligently.
        normalized_layout = {
            "x": 0,
            "y": index * 4,
            "width": 12,
            "height": 2,
        }
    else:
        normalized_layout = {
            "x": 0,
            "y": 0,
            "width": 4,
            "height": 2,
        }

    return NormalizedWidget(
        id=widget_id,
        widget_type=widget_type,
        title=defn.get("title", defn.get("title_text", "")),
        queries=queries,
        formulas=formulas,
        display_type=defn.get("display_type", ""),
        yaxis=defn.get("yaxis", {}),
        legend=_extract_legend(defn),
        layout=normalized_layout,
        response_format=_infer_response_format(defn),
        style=defn.get("style", {}),
        conditional_formats=conditional_formats,
        custom_unit=defn.get("custom_unit", ""),
        precision=defn.get("precision"),
        text_align=defn.get("text_align", ""),
        autoscale=defn.get("autoscale", True),
        time=defn.get("time", {}),
        raw_definition=defn,
        children=children,
        events=defn.get("events", []) or [],
        markers=defn.get("markers", []) or [],
    )


def _extract_queries_and_formulas(
    defn: dict[str, Any],
) -> tuple[list[WidgetQuery], list[WidgetFormula]]:
    """Extract queries and formulas from a widget definition.

    Handles both modern format (requests[].queries + requests[].formulas)
    and legacy format (requests[].q).
    """
    queries: list[WidgetQuery] = []
    formulas: list[WidgetFormula] = []
    seen_names: set[str] = set()

    for req in defn.get("requests", []):
        if isinstance(req, dict):
            _extract_from_request(req, queries, formulas, seen_names, defn.get("type", ""))
        elif isinstance(req, list):
            for sub_req in req:
                if isinstance(sub_req, dict):
                    _extract_from_request(sub_req, queries, formulas, seen_names, defn.get("type", ""))

    if defn.get("type") == "query_value" and not queries:
        for req in defn.get("requests", []):
            if isinstance(req, dict):
                _extract_from_request(req, queries, formulas, seen_names, defn.get("type", ""))

    if defn.get("type") in ("note", "free_text", "image", "iframe"):
        pass

    return queries, formulas


def _extract_from_request(
    req: dict[str, Any],
    queries: list[WidgetQuery],
    formulas: list[WidgetFormula],
    seen_names: set[str],
    widget_type: str = "",
) -> None:
    request_name_map: dict[str, str] = {}
    for raw_q in req.get("queries", []):
        original_name = raw_q.get("name", f"query{len(queries)}") or f"query{len(queries)}"
        name = original_name
        if name in seen_names:
            base_name = name
            suffix = 2
            while f"{base_name}_{suffix}" in seen_names:
                suffix += 1
            name = f"{base_name}_{suffix}"
        seen_names.add(name)
        request_name_map[original_name] = name

        data_source = raw_q.get("data_source", "metrics")
        raw_query_str = raw_q.get("query", "")
        # Modern Datadog log/event queries put the filter expression in a
        # nested `search.query` field; fall back to it when the legacy
        # `query` field is empty so we don't lose the filter.
        if not raw_query_str and data_source in LOG_DATA_SOURCES:
            search = raw_q.get("search")
            if isinstance(search, dict):
                raw_query_str = search.get("query", "") or ""

        wq = WidgetQuery(
            name=name,
            data_source=data_source,
            raw_query=raw_query_str,
            aggregator=raw_q.get("aggregator", ""),
        )

        if data_source in METRIC_DATA_SOURCES and raw_query_str:
            try:
                wq.metric_query = parse_metric_query(raw_query_str)
                wq.query_type = "metric"
            except ParseError:
                mq2 = _try_parse_bare_metric(raw_query_str)
                if mq2:
                    wq.metric_query = mq2
                    wq.query_type = "metric"
                else:
                    wq.query_type = "metric_unparsed"
        elif data_source in LOG_DATA_SOURCES:
            # Always parse — parse_log_query handles empty/wildcard input
            # and returns a LogQuery(ast=None) marker. Downstream code can
            # treat that as a match-all filter.
            wq.log_query = parse_log_query(raw_query_str)
            wq.query_type = "log"
        else:
            wq.query_type = data_source

        queries.append(wq)

    for raw_f in req.get("formulas", []):
        formula_str = _rewrite_formula_refs(raw_f.get("formula", ""), request_name_map)
        alias = raw_f.get("alias", "")
        limit_cfg = raw_f.get("limit", None)
        wf = WidgetFormula(raw=formula_str, alias=alias, limit=limit_cfg)
        if formula_str:
            try:
                wf.expression = parse_formula(formula_str)
            except ParseError:
                pass
        formulas.append(wf)

    legacy_q = req.get("q", "")
    legacy_aggregator = req.get("aggregator", "")
    if legacy_q and not queries:
        for _idx, part in enumerate(_split_legacy_q(legacy_q)):
            part = part.strip()
            if not part:
                continue
            name = f"query{len(queries)}"
            if part.startswith("logs("):
                log_search = _extract_legacy_log_search(part)
                wq = WidgetQuery(
                    name=name,
                    data_source="logs",
                    raw_query=log_search,
                    query_type="log",
                    log_query=parse_log_query(log_search),
                    aggregator=legacy_aggregator,
                )
                queries.append(wq)
                continue
            wq = WidgetQuery(
                name=name,
                data_source="metrics",
                raw_query=part,
                query_type="legacy",
                aggregator=legacy_aggregator,
            )
            mq, outer_fns = parse_legacy_query(part)
            if mq:
                if outer_fns:
                    mq.functions.extend(outer_fns)
                wq.metric_query = mq
                wq.query_type = "metric"
            else:
                mq2 = _try_parse_bare_metric(part)
                if mq2:
                    wq.metric_query = mq2
                    wq.query_type = "metric"
                else:
                    existing_queries = list(queries)
                    formula_raw = _extract_legacy_metric_expression(
                        part,
                        queries,
                        legacy_aggregator,
                        require_group=widget_type == "heatmap",
                    )
                    if formula_raw:
                        if not formulas:
                            for query in existing_queries:
                                if query.query_type == "metric":
                                    _append_formula(query.name, formulas)
                        _append_formula(formula_raw, formulas)
                        continue
                    wq.query_type = "legacy_unparsed"
            queries.append(wq)
            if formulas and wq.query_type == "metric":
                _append_formula(wq.name, formulas)

    log_q = req.get("log_query", req.get("search", {}))
    if isinstance(log_q, dict) and log_q.get("query") and not any(
        q.query_type == "log" for q in queries
    ):
        query_str = log_q["query"]
        name = f"log_query{len(queries)}"
        wq = WidgetQuery(
            name=name,
            data_source="logs",
            raw_query=query_str,
            query_type="log",
        )
        wq.log_query = parse_log_query(query_str)
        queries.append(wq)

    apm_q = req.get("apm_query", {})
    if isinstance(apm_q, dict) and apm_q:
        name = f"apm_query{len(queries)}"
        queries.append(WidgetQuery(
            name=name,
            data_source="apm",
            raw_query=str(apm_q),
            query_type="apm",
        ))

    rum_q = req.get("rum_query", {})
    if isinstance(rum_q, dict) and rum_q:
        name = f"rum_query{len(queries)}"
        queries.append(WidgetQuery(
            name=name,
            data_source="rum",
            raw_query=str(rum_q),
            query_type="rum",
        ))


def _extract_legend(defn: dict[str, Any]) -> dict[str, Any]:
    legend = defn.get("legend_layout", defn.get("legend", {}))
    if isinstance(legend, str):
        return {"mode": legend}
    if isinstance(legend, dict):
        return legend
    show_legend = defn.get("show_legend", defn.get("legend_size"))
    if show_legend is not None:
        return {"visible": bool(show_legend)}
    return {}


def _infer_response_format(defn: dict[str, Any]) -> str:
    for req in defn.get("requests", []):
        if isinstance(req, dict):
            rf = req.get("response_format", "")
            if rf:
                return rf
    wtype = defn.get("type", "")
    if wtype == "timeseries":
        return "timeseries"
    if wtype in ("toplist", "table", "list_stream", "query_table"):
        return "scalar"
    if wtype == "query_value":
        return "scalar"
    return ""


# ---------------------------------------------------------------------------
# Log stream / list stream extraction
# ---------------------------------------------------------------------------

def _extract_log_stream_queries(defn: dict[str, Any]) -> list[WidgetQuery]:
    """Extract queries from log_stream and list_stream widgets.

    These use different query structures:
    - Old: top-level `query` string + `columns`
    - New: `requests[].query.query_string` with `data_source: logs_stream`
    """
    queries: list[WidgetQuery] = []

    top_query = defn.get("query", "")
    if isinstance(top_query, str) and top_query:
        lq = parse_log_query(top_query)
        queries.append(WidgetQuery(
            name="log_query0",
            data_source="logs",
            raw_query=top_query,
            log_query=lq,
            query_type="log",
        ))

    for req in defn.get("requests", []):
        if not isinstance(req, dict):
            continue
        inner_q = req.get("query", {})
        if isinstance(inner_q, dict):
            qs = inner_q.get("query_string", "")
            if qs:
                data_source = inner_q.get("data_source", "logs")
                if data_source == "logs_stream":
                    data_source = "logs"
                lq = parse_log_query(qs)
                queries.append(WidgetQuery(
                    name=f"log_query{len(queries)}",
                    data_source=data_source,
                    raw_query=qs,
                    log_query=lq,
                    query_type="log",
                ))

    return queries


def _extract_legacy_log_search(query: str) -> str:
    match = re.search(r'logs\("((?:[^"\\]|\\.)*)"\)', query)
    if not match:
        return "*"
    return match.group(1) or "*"


# ---------------------------------------------------------------------------
# Legacy `q` string helpers
# ---------------------------------------------------------------------------

_BARE_METRIC_RE = re.compile(
    r"^([\w.]+)\{([^}]*)\}(.*)$"
)


def _split_legacy_q(q: str) -> list[str]:
    """Split comma-separated legacy `q` strings, respecting parens and braces."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in q:
        if ch in ("(", "{"):
            depth += 1
            current.append(ch)
        elif ch in (")", "}"):
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


def _append_formula(raw: str, formulas: list[WidgetFormula]) -> None:
    wf = WidgetFormula(raw=raw)
    try:
        wf.expression = parse_formula(raw)
    except ParseError:
        pass
    formulas.append(wf)


def _extract_legacy_metric_expression(
    text: str,
    queries: list[WidgetQuery],
    aggregator: str,
    require_group: bool = False,
) -> str | None:
    """Convert legacy arithmetic over metric atoms into a normalized formula."""
    formula: list[str] = []
    new_queries: list[WidgetQuery] = []
    idx = 0
    replacements = 0
    while idx < len(text):
        candidate = _longest_legacy_metric_atom(text[idx:])
        if candidate is None:
            formula.append(text[idx])
            idx += 1
            continue

        raw_atom, metric_query = candidate
        name = f"query{len(queries) + len(new_queries)}"
        new_queries.append(WidgetQuery(
            name=name,
            data_source="metrics",
            raw_query=raw_atom,
            metric_query=metric_query,
            aggregator=aggregator,
            query_type="metric",
        ))
        formula.append(name)
        idx += len(raw_atom)
        replacements += 1

    if replacements == 0:
        return None
    raw_formula = "".join(formula).strip()
    try:
        parsed = parse_formula(raw_formula)
    except ParseError:
        return None
    if parsed.ast is None:
        return None
    if _formula_uses_unsafe_legacy_function(parsed.ast):
        return None
    if require_group and not any(query.metric_query and query.metric_query.group_by for query in new_queries):
        return None
    if any(
        query.metric_query
        and query.metric_query.as_count
        and not _metric_name_is_count_like(query.metric_query.metric)
        for query in new_queries
    ):
        return None
    queries.extend(new_queries)
    return raw_formula


def _longest_legacy_metric_atom(text: str) -> tuple[str, MetricQuery] | None:
    if not _looks_like_legacy_metric_start(text):
        return None
    for end in range(len(text), 0, -1):
        raw = text[:end]
        candidate = raw.strip()
        if not candidate:
            continue
        metric_query = _parse_legacy_metric_atom(candidate)
        if metric_query is not None:
            return raw[:len(raw.rstrip())], metric_query
    return None


def _looks_like_legacy_metric_start(text: str) -> bool:
    return bool(
        re.match(r"(?:avg|sum|min|max|count|last|median|p\d+):", text, re.IGNORECASE)
        or re.match(r"[\w.]+\{", text)
    )


def _parse_legacy_metric_atom(text: str) -> MetricQuery | None:
    mq, outer_fns = parse_legacy_query(text)
    if mq:
        if outer_fns:
            mq.functions.extend(outer_fns)
        return mq
    return _try_parse_bare_metric(text)


def _formula_uses_unsafe_legacy_function(node: Any) -> bool:
    if isinstance(node, FormulaFuncCall):
        fn_name = (node.name or "").lower()
        if fn_name in {"top", "timeshift", "derivative"}:
            return True
        return any(_formula_uses_unsafe_legacy_function(arg) for arg in node.args or [])
    if isinstance(node, FormulaBinOp):
        return (
            _formula_uses_unsafe_legacy_function(node.left)
            or _formula_uses_unsafe_legacy_function(node.right)
        )
    if isinstance(node, FormulaUnary):
        return _formula_uses_unsafe_legacy_function(node.operand)
    return False


def _metric_name_is_count_like(metric_name: str) -> bool:
    lowered = metric_name.lower()
    return lowered.endswith((".count", "_count", ".total", "_total"))


def _try_parse_bare_metric(text: str) -> MetricQuery | None:
    """Try to parse a legacy metric query without an aggregator prefix.

    Format: `metric.name{scope} [by {tags}] [.functions()]`
    Infers `avg` as the default space aggregator.
    """
    text = text.strip()
    m = _BARE_METRIC_RE.match(text)
    if not m:
        return None

    metric = m.group(1)
    scope_str = m.group(2)
    rest = m.group(3).strip()

    synthetic = f"avg:{metric}{{{scope_str}}}{rest}"
    try:
        mq = parse_metric_query(synthetic)
        mq.raw = text
        return mq
    except ParseError:
        return None


def _rewrite_formula_refs(formula: str, name_map: dict[str, str]) -> str:
    rewritten = formula or ""
    for original, renamed in sorted(name_map.items(), key=lambda item: len(item[0]), reverse=True):
        if not original or original == renamed:
            continue
        rewritten = re.sub(rf"\b{re.escape(original)}\b", renamed, rewritten)
    return rewritten
