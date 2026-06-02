# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Query translation: Datadog metric/log queries → ES|QL.

This is the core translation engine. It converts parsed Datadog metric queries
and log queries into ES|QL strings suitable for Kibana dashboard panels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from observability_migration.core.verification.field_capabilities import (
    assess_field_usage,
    is_counter_metric_field,
)

from .field_map import FieldMapProfile
from .log_parser import log_ast_to_esql_where, log_ast_to_kql
from .models import (
    LOG_DATA_SOURCES,
    FormulaBinOp,
    FormulaFuncCall,
    FormulaNumber,
    FormulaRef,
    FormulaString,
    FormulaUnary,
    MetricQuery,
    NormalizedWidget,
    PanelPlan,
    ScopeBoolOp,
    TagFilter,
    TranslationResult,
    WidgetQuery,
)
from .rules import LENS_TRANSLATORS, LOG_TRANSLATORS, METRIC_TRANSLATORS

DD_AGG_TO_ESQL: dict[str, str] = {
    "avg": "AVG",
    "sum": "SUM",
    "min": "MIN",
    "max": "MAX",
    "count": "COUNT",
    "last": "LAST",
    "p50": "PERCENTILE(%, 50)",
    "p75": "PERCENTILE(%, 75)",
    "p90": "PERCENTILE(%, 90)",
    "p95": "PERCENTILE(%, 95)",
    "p99": "PERCENTILE(%, 99)",
}

TIME_BUCKET_EXPR = "BUCKET(@timestamp, 50, ?_tstart, ?_tend)"
TIME_FILTER = "@timestamp >= ?_tstart AND @timestamp <= ?_tend"
DEFAULT_RATE_WINDOW = "5m"
DEFAULT_RATE_WINDOW_SECONDS = 300.0
_CHANGE_WIDGET_COMPARE_TO_SECONDS = {
    "hour_before": 3600,
    "day_before": 86400,
    "week_before": 7 * 86400,
    "month_before": 28 * 86400,
}
_SPAN_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}
_DATADOG_SPAN_RE = re.compile(r"(?P<amount>\d+)(?P<unit>[smhdw])$", re.IGNORECASE)

_TEMPLATE_VAR_RE = re.compile(r"\$\w+(?:\.\w+)*")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")


class _RequiresManualError(ValueError):
    """Translator-internal signal: the source widget is ambiguous or uses
    a pattern we can't faithfully translate, but the failure is at the
    semantics level rather than the engine level. The widget is downgraded
    to `requires_manual` (not `not_feasible`) so the migration manifest
    surfaces a placeholder for human review instead of a hard block."""


@dataclass
class _MetricQuerySpec:
    query_name: str
    alias: str
    index: str
    where_str: str
    group_fields: list[str]
    agg_expr: str
    mq: MetricQuery
    es_metric: str = ""  # raw ES field name, used for FIRST/LAST in rate()/diff()
    tag_where_str: str = ""  # WHERE clauses excluding TIME_FILTER, used as per-agg
                              # filter when sibling specs have heterogeneous filters
    is_counter: bool = False  # target field is TSDS counter-typed — enables TS|QL RATE()


@dataclass
class _FormulaSpec:
    ast: Any
    alias: str
    raw: str


@dataclass
class _TopFunctionConfig:
    limit: int | None = None
    reducer: str | None = None
    sort_order: str = "DESC"


@dataclass
class _TranslationExecutionContext:
    widget: NormalizedWidget
    plan: PanelPlan
    field_map: FieldMapProfile
    result: TranslationResult
    use_kql_bridge: bool = False
    output: Any = None
    metric_queries: list[WidgetQuery] = field(default_factory=list)
    trace: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self):
        self.metric_queries = [q for q in self.widget.queries if q.metric_query]
        self.trace = self.result.trace


def translate_widget(
    widget: NormalizedWidget,
    plan: PanelPlan,
    field_map: FieldMapProfile,
) -> TranslationResult:
    """Translate a planned widget into an ES|QL query and panel config."""

    query_language = (
        "datadog_mixed"
        if widget.has_metric_queries and widget.has_log_queries
        else "datadog_log"
        if widget.has_log_queries
        else "datadog_metric"
        if widget.has_metric_queries
        else "datadog_widget"
    )
    result = TranslationResult(
        widget_id=widget.id,
        source_panel_id=widget.id,
        title=widget.title,
        dd_widget_type=widget.widget_type,
        kibana_type=plan.kibana_type,
        backend=plan.backend,
        confidence=plan.confidence,
        warnings=list(plan.warnings),
        reasons=list(plan.reasons),
        source_queries=[q.raw_query for q in widget.queries],
        query_language=query_language,
        trace=list(plan.trace),
    )

    if plan.backend in ("markdown", "blocked"):
        is_text_widget = widget.widget_type in (
            "note", "free_text", "image", "iframe",
        )
        if plan.backend == "blocked":
            result.status = "not_feasible"
        elif is_text_widget:
            result.status = "ok"
        else:
            result.status = "requires_manual"
        return result

    if plan.backend == "group":
        result.status = "skipped"
        return result

    if plan.backend == "lens":
        try:
            lens_config = _translate_lens_widget(widget, plan, field_map, result)
            result.yaml_panel = lens_config
            result.status = "warning" if result.warnings else "ok"
        except Exception as exc:
            result.status = "not_feasible"
            result.warnings.append(f"lens translation error: {exc}")
            result.semantic_losses.append(str(exc))
        return result

    try:
        if widget.has_log_queries:
            esql = _translate_log_widget(
                widget,
                plan,
                field_map,
                result,
                use_kql_bridge=(plan.backend == "esql_with_kql"),
            )
        elif widget.has_metric_queries:
            esql = _translate_metric_widget(widget, plan, field_map, result)
        else:
            result.status = "not_feasible"
            result.warnings.append("no translatable queries")
            return result

        result.esql_query = esql
        result.kibana_type = plan.kibana_type
        result.status = "warning" if result.warnings else "ok"

    except _RequiresManualError as exc:
        result.status = "requires_manual"
        result.warnings.append(f"manual review needed: {exc}")
        result.semantic_losses.append(str(exc))
    except Exception as exc:
        result.status = "not_feasible"
        result.warnings.append(f"translation error: {exc}")
        result.semantic_losses.append(str(exc))

    return result


# ---------------------------------------------------------------------------
# Metric translation
# ---------------------------------------------------------------------------

def _translate_metric_widget(
    widget: NormalizedWidget,
    plan: PanelPlan,
    field_map: FieldMapProfile,
    result: TranslationResult,
) -> str:
    """Translate metric queries for a widget through the registry."""
    context = _TranslationExecutionContext(
        widget=widget,
        plan=plan,
        field_map=field_map,
        result=result,
    )
    METRIC_TRANSLATORS.apply(
        context,
        stop_when=lambda ctx, _detail: ctx.output is not None,
    )
    if context.output is None:
        raise ValueError("no parsed metric queries")
    return str(context.output)


@METRIC_TRANSLATORS.register(
    "datadog.translate.metric_single_query",
    priority=10,
    summary="Translate single-query metric widgets into Lens or ES|QL primitives.",
)
def metric_single_query_rule(context: _TranslationExecutionContext) -> str | None:
    if not context.metric_queries:
        return None
    if context.widget.formulas or len(context.metric_queries) != 1:
        return None
    context.output = _translate_single_metric(
        context.metric_queries[0],
        context.widget,
        context.plan,
        context.field_map,
        context.result,
    )
    return "translated single metric query"


@METRIC_TRANSLATORS.register(
    "datadog.translate.metric_formula",
    priority=20,
    summary="Translate formula and multi-query metric widgets into ES|QL pipelines.",
)
def metric_formula_rule(context: _TranslationExecutionContext) -> str | None:
    if not context.metric_queries:
        return None
    if not context.widget.formulas and len(context.metric_queries) == 1:
        return None
    context.output = _translate_formula_metric_widget(
        context.metric_queries,
        context.widget,
        context.plan,
        context.field_map,
        context.result,
    )
    return "translated metric formula pipeline"


def _translate_single_metric(
    wq: WidgetQuery,
    widget: NormalizedWidget,
    plan: PanelPlan,
    field_map: FieldMapProfile,
    result: TranslationResult,
) -> str:
    if widget.widget_type == "change":
        return _build_change_widget_esql(wq, widget, plan, field_map, result)

    spec = _build_metric_query_spec(wq, field_map, result)
    top_config = _extract_top_function_config(wq.metric_query)
    is_timeseries = plan.kibana_type == "xy"
    is_heatmap = plan.kibana_type == "heatmap"
    # bar_chart shares the toplist grouped-aggregation shape (ranked groups).
    is_toplist = widget.widget_type in ("toplist", "bar_chart")
    is_table = widget.widget_type in ("table", "query_table") and not is_toplist
    is_partition = plan.kibana_type in ("partition", "treemap")
    reducer = None if is_timeseries or is_heatmap else _request_reducer_for_queries(
        [wq],
        default="last" if plan.kibana_type == "metric" else None,
    )
    if top_config.reducer:
        reducer = top_config.reducer

    if plan.kibana_type == "metric" and spec.group_fields:
        raise ValueError("metric widget with grouped query needs a reducing formula")
    if is_heatmap and not spec.group_fields:
        raise ValueError("heatmap requires at least one grouping dimension")
    if is_partition and not spec.group_fields:
        # Sunburst/treemap/pie widgets need at least one dimension to
        # carve up; without one the source dashboard is asking for an
        # ungrouped pie chart, which is a single-value question with a
        # multi-value visualization. Surface as requires_manual so the
        # placeholder uploads — the YAML still ships, just with a
        # marker for someone to redesign the panel.
        raise _RequiresManualError(
            f"{plan.kibana_type} widget needs at least one grouping dimension; "
            "the source query has none, so the chart can't be sliced "
            "automatically. Redesign as a single-value panel or add a `by {}`."
        )

    if is_timeseries or is_heatmap:
        if is_timeseries and top_config.limit is not None:
            group_clause = f"time_bucket = {TIME_BUCKET_EXPR}"
            if spec.group_fields:
                group_clause += ", " + ", ".join(spec.group_fields)
            rank_expr = _series_reducer_expr(top_config.reducer or "avg", "value")
            lines = [
                f"FROM {spec.index}",
                f"| WHERE {spec.where_str}",
                f"| STATS value = {spec.agg_expr} BY {group_clause}",
            ]
            if spec.group_fields:
                lines.append(f"| STATS _rank = {rank_expr} BY {', '.join(spec.group_fields)}")
            else:
                lines.append(f"| STATS _rank = {rank_expr}")
            lines.append(f"| SORT _rank {top_config.sort_order}")
            lines.append(f"| LIMIT {top_config.limit}")
            plan.kibana_type = "table"
            _append_unique_warning(
                result,
                f"top({top_config.limit}) on timeseries approximated as ranked table of top-{top_config.limit} groups"
                " — ES|QL cannot filter to N series in a single pass",
            )
            return "\n".join(lines)
        return _build_timeseries_esql(
            spec.index, spec.where_str, spec.agg_expr, spec.group_fields,
        )

    if is_toplist:
        limit = top_config.limit or _extract_toplist_limit(widget)
        return _build_categorical_esql(
            spec.index,
            spec.where_str,
            spec.agg_expr,
            spec.group_fields,
            sort_field="value",
            sort_order=top_config.sort_order,
            limit=limit,
            reducer=reducer,
        )

    if is_table or is_partition:
        return _build_categorical_esql(
            spec.index,
            spec.where_str,
            spec.agg_expr,
            spec.group_fields,
            sort_field="value",
            sort_order="DESC",
            limit=100,
            reducer=reducer,
        )

    return _build_scalar_esql(spec.index, spec.where_str, spec.agg_expr, reducer=reducer)


def _build_change_widget_esql(
    wq: WidgetQuery,
    widget: NormalizedWidget,
    plan: PanelPlan,
    field_map: FieldMapProfile,
    result: TranslationResult,
) -> str:
    spec = _build_metric_query_spec(wq, field_map, result)
    request = _extract_change_widget_request(widget)
    window_seconds = _change_widget_window_seconds(widget)
    if window_seconds <= 0:
        window_seconds = 3600
        _append_unique_warning(result, "change widget live span was unavailable; defaulted to 1 hour")

    compare_to = str(request.get("compare_to", "") or "").strip().lower()
    shift_seconds = _CHANGE_WIDGET_COMPARE_TO_SECONDS.get(compare_to, window_seconds)
    if compare_to and compare_to not in _CHANGE_WIDGET_COMPARE_TO_SECONDS:
        _append_unique_warning(
            result,
            f"change compare_to '{compare_to}' is approximated using the widget live span",
        )

    total_span = _seconds_to_esql_span(window_seconds + shift_seconds)
    current_span = _seconds_to_esql_span(window_seconds)
    shift_span = _seconds_to_esql_span(shift_seconds)
    if not total_span or not current_span or not shift_span:
        raise ValueError("change widget time spans could not be translated safely")

    where_clauses = []
    for scope_item in spec.mq.scope:
        clause = _metric_scope_to_esql(scope_item, field_map, context="metric")
        if clause:
            where_clauses.append(clause)
    where_clauses.append(f"@timestamp >= NOW() - {total_span}")

    lines = [f"FROM {spec.index}"]
    lines.append(f"| WHERE {' AND '.join(where_clauses)}")
    stats_parts = [
        f"current_value = {spec.agg_expr} WHERE @timestamp >= NOW() - {current_span}",
        (
            f"previous_value = {spec.agg_expr} "
            f"WHERE @timestamp >= NOW() - {total_span} AND @timestamp < NOW() - {shift_span}"
        ),
    ]
    stats_line = "| STATS " + ", ".join(stats_parts)
    if spec.group_fields:
        stats_line += f" BY {', '.join(spec.group_fields)}"
    lines.append(stats_line)
    lines.append("| WHERE current_value IS NOT NULL AND previous_value IS NOT NULL")

    change_type = str(request.get("change_type", "") or "").strip().lower()
    if change_type in {"relative", "percentage", "percent"}:
        lines.append(
            "| EVAL value = CASE(previous_value == 0, NULL, ((current_value - previous_value) / previous_value) * 100)"
        )
        lines.append("| WHERE value IS NOT NULL")
    else:
        lines.append("| EVAL value = current_value - previous_value")

    order_dir = str(request.get("order_dir", "") or "desc").strip().upper()
    if order_dir not in {"ASC", "DESC"}:
        order_dir = "DESC"
    if spec.group_fields or plan.kibana_type == "table":
        lines.append(f"| SORT value {order_dir}")
        lines.append("| LIMIT 100")
    else:
        lines.append("| KEEP value")
    return "\n".join(lines)


def _translate_formula_metric_widget(
    metric_queries: list[WidgetQuery],
    widget: NormalizedWidget,
    plan: PanelPlan,
    field_map: FieldMapProfile,
    result: TranslationResult,
) -> str:
    specs = [_build_metric_query_spec(q, field_map, result) for q in metric_queries]
    spec_map = {spec.query_name: spec for spec in specs}
    formulas = _extract_formula_specs(widget, specs, plan)
    special_query = _try_translate_formula_reducer(formulas, spec_map, plan)
    if special_query:
        return special_query
    count_formula_query = _try_translate_count_formula_pipeline(formulas, spec_map, plan)
    if count_formula_query:
        return count_formula_query

    used_specs = _resolve_used_specs(formulas, spec_map)
    _ensure_formula_specs_compatible(used_specs)

    if plan.kibana_type == "metric":
        if len(formulas) != 1:
            raise ValueError("metric widgets support exactly one translated formula")
        if used_specs[0].group_fields:
            raise _RequiresManualError(
                "grouped query used in a scalar (query_value) widget — "
                "reduce to a single value or convert to a table panel"
            )
    if plan.kibana_type == "heatmap" and not used_specs[0].group_fields:
        raise ValueError("heatmap requires at least one grouping dimension")
    if plan.kibana_type in ("partition", "treemap") and not used_specs[0].group_fields:
        raise _RequiresManualError(
            f"{plan.kibana_type} formula widget needs at least one grouping "
            "dimension; the source dashboard's query has none. Surface for "
            "manual redesign rather than blocking the upload."
        )

    # For xy panels whose formula root is top(expr, N, agg, order), extract
    # the top-N parameters so we can emit a ranked flat table instead.
    # ES|QL has no subquery support so we cannot filter to exactly N series
    # while keeping the time dimension; we collapse to per-group ranking.
    top_params: tuple[int, str, str] | None = None
    if plan.kibana_type == "xy" and len(formulas) == 1:
        top_params = _extract_top_params(formulas[0].ast)

    reducer = None
    if plan.kibana_type not in ("xy", "heatmap"):
        used_names = {spec.query_name for spec in used_specs}
        reducer = _request_reducer_for_queries(
            [q for q in metric_queries if q.name in used_names],
            default="last" if plan.kibana_type == "metric" else None,
        )

    include_time_bucket = plan.kibana_type in ("xy", "heatmap") or reducer is not None
    dim_exprs, dim_aliases = _metric_dimension_exprs(
        used_specs[0].group_fields,
        include_time_bucket=include_time_bucket,
    )
    needs_bucket_span = any(_formula_needs_bucket_span(formula.ast) for formula in formulas)

    derivative_refs: set[str] = set()
    for formula in formulas:
        derivative_refs |= _collect_derivative_query_refs(formula.ast)

    heterogeneous = _specs_have_heterogeneous_filters(used_specs)

    # ----------------------------------------------------------------
    # TS|QL path: when the formula reduces to rate()/diff() of a single
    # counter-typed metric reference, emit `TS index | STATS RATE(field,
    # window)` — the native ES|QL time-series aggregation. Grafana uses
    # the same pattern for PromQL rate(). Falls back to the FROM +
    # FIRST/LAST path below when the metric is a gauge or when the
    # formula is more complex than a direct counter rate/diff.
    # ----------------------------------------------------------------
    ts_rate_spec: _MetricQuerySpec | None = None
    ts_fn_name: str = ""
    if (
        len(used_specs) == 1
        and used_specs[0].is_counter
        and used_specs[0].query_name in derivative_refs
        and len(formulas) == 1
        and isinstance(formulas[0].ast, FormulaFuncCall)
        and (formulas[0].ast.name or "").lower() in _DERIVATIVE_FORMULA_FNS
        and len(formulas[0].ast.args) == 1
        and isinstance(formulas[0].ast.args[0], FormulaRef)
        and formulas[0].ast.args[0].name == used_specs[0].query_name
        and plan.kibana_type in ("xy", "heatmap")
    ):
        ts_rate_spec = used_specs[0]
        ts_fn_name = (formulas[0].ast.name or "").lower()

    if ts_rate_spec is not None:
        # ES|QL native TS aggregation:
        # rate / monotonic counter rate / increase per bucket.
        es_agg = "RATE" if ts_fn_name == "rate" else "INCREASE"
        window = "5 minute"
        spec = ts_rate_spec
        alias = _safe_alias(formulas[0].alias or formulas[0].raw or f"{ts_fn_name}_{spec.alias}")
        by_clause = f"time_bucket = TBUCKET({window})"
        if spec.group_fields:
            by_clause += ", " + ", ".join(spec.group_fields)
        ts_lines = [
            f"TS {spec.index}",
            f"| WHERE {spec.where_str}",
            f"| STATS {alias} = {es_agg}({spec.es_metric}, {window}) BY {by_clause}",
            "| KEEP time_bucket, " + ", ".join(spec.group_fields + [alias])
            if spec.group_fields
            else f"| KEEP time_bucket, {alias}",
            "| SORT time_bucket",
        ]
        if result is not None:
            _append_unique_warning(
                result,
                f"{ts_fn_name}() translated via ES|QL TS|QL "
                f"{es_agg}({spec.es_metric}, {window}) — requires the target "
                f"field to be a counter in a time_series index",
            )
        return "\n".join(ts_lines)

    def _per_agg_where(spec: _MetricQuerySpec) -> str:
        """Return the WHERE clause to append to an aggregation expression
        in STATS. Empty string when not needed."""
        if heterogeneous and spec.tag_where_str:
            return f" WHERE {spec.tag_where_str}"
        return ""

    stats_parts = []
    for spec in used_specs:
        stats_parts.append(f"{spec.alias} = {spec.agg_expr}{_per_agg_where(spec)}")
    for spec in used_specs:
        if spec.query_name in derivative_refs and spec.es_metric:
            # FIRST/LAST give the bucket-endpoint values needed for true
            # rate/diff semantics; the WHERE filter skips rows where the
            # metric column is null (other metrics share the index in
            # multi-metric data streams) AND (when sibling specs differ in
            # filters) limits the row population to this spec's filter set.
            null_guard = f"{spec.es_metric} IS NOT NULL"
            spec_filter = spec.tag_where_str if heterogeneous else ""
            agg_where = f" WHERE {null_guard}"
            if spec_filter:
                agg_where = f" WHERE {null_guard} AND {spec_filter}"
            stats_parts.append(
                f"{spec.alias}_first = FIRST({spec.es_metric}, @timestamp){agg_where}"
            )
            stats_parts.append(
                f"{spec.alias}_last = LAST({spec.es_metric}, @timestamp){agg_where}"
            )
    if needs_bucket_span:
        stats_parts.append(
            'bucket_span_seconds = DATE_DIFF("seconds", MIN(@timestamp), MAX(@timestamp)) + 1'
        )

    # When filters are heterogeneous, the outer WHERE is the time filter
    # plus the OR of all per-spec filters. This narrows the candidate row
    # set without filtering out anything any aggregation needs.
    if heterogeneous:
        outer_filters = [TIME_FILTER]
        spec_filters = [s.tag_where_str for s in used_specs if s.tag_where_str]
        if spec_filters:
            outer_filters.append("(" + " OR ".join(f"({f})" for f in spec_filters) + ")")
        outer_where = " AND ".join(outer_filters)
    else:
        outer_where = used_specs[0].where_str

    lines = [
        f"FROM {used_specs[0].index}",
        f"| WHERE {outer_where}",
    ]
    if dim_exprs:
        lines.append(f"| STATS {', '.join(stats_parts)} BY {', '.join(dim_exprs)}")
    else:
        lines.append(f"| STATS {', '.join(stats_parts)}")

    query_aliases = {spec.query_name: spec.alias for spec in used_specs}
    output_fields: list[str] = []
    eval_parts: list[str] = []
    formula_warnings: list[str] = []
    for formula in formulas:
        expr = _formula_ast_to_esql(
            formula.ast, query_aliases, formula_warnings, derivative_refs
        )
        output_fields.append(formula.alias)
        if expr != _esql_identifier(formula.alias):
            eval_parts.append(f"{formula.alias} = {expr}")
    for w in formula_warnings:
        _append_unique_warning(result, w)
    if eval_parts:
        lines.append(f"| EVAL {', '.join(eval_parts)}")

    if reducer:
        group_aliases = [alias for alias in dim_aliases if alias != "time_bucket"]
        reduced_parts = [
            f"{field} = {_series_reducer_expr(reducer, field)}"
            for field in output_fields
        ]
        if group_aliases:
            lines.append(f"| STATS {', '.join(reduced_parts)} BY {', '.join(group_aliases)}")
        else:
            lines.append(f"| STATS {', '.join(reduced_parts)}")
        keep_fields = group_aliases + output_fields
    else:
        keep_fields = dim_aliases + output_fields
    if keep_fields:
        lines.append(f"| KEEP {', '.join(keep_fields)}")

    if top_params is not None and plan.kibana_type == "xy":
        # top(expr, N, agg, order) in a timeseries formula: ES|QL cannot
        # filter to exactly N time-series in a single pass (no subqueries).
        # Collapse to a per-group ranked flat table — the time dimension is
        # dropped but the top-N groups are correctly identified.
        top_n, top_agg, top_order = top_params
        group_aliases_no_bucket = [a for a in dim_aliases if a != "time_bucket"]
        formula_field = output_fields[0] if output_fields else "_rank"
        rank_expr = _series_reducer_expr(top_agg, formula_field)
        if group_aliases_no_bucket:
            lines.append(
                f"| STATS _rank = {rank_expr} BY {', '.join(group_aliases_no_bucket)}"
            )
        else:
            lines.append(f"| STATS _rank = {rank_expr}")
        lines.append(f"| SORT _rank {top_order}")
        lines.append(f"| LIMIT {top_n}")
        plan.kibana_type = "table"
        # Replace the generic "uses panel-level sort/limit" warning that
        # _formula_ast_to_esql already added with a more accurate message.
        _OLD_TOP_WARNING = (
            "top() filtering is approximated — uses panel-level sort/limit "
            "instead of formula-scoped top-N"
        )
        if _OLD_TOP_WARNING in result.warnings:
            result.warnings.remove(_OLD_TOP_WARNING)
        _append_unique_warning(
            result,
            f"top({top_n}) on timeseries approximated as ranked table of top-{top_n} groups"
            f" — ES|QL cannot filter to N series in a single pass",
        )
    elif plan.kibana_type in ("xy", "heatmap"):
        lines.append("| SORT time_bucket")
    elif widget.widget_type == "toplist" or plan.kibana_type in ("table", "partition", "treemap"):
        sort_field, sort_order, limit = _extract_metric_sort(widget, output_fields)
        if sort_field:
            lines.append(f"| SORT {sort_field} {sort_order}")
        if limit:
            lines.append(f"| LIMIT {limit}")

    return "\n".join(lines)


def _build_metric_query_spec(
    wq: WidgetQuery,
    field_map: FieldMapProfile,
    result: TranslationResult,
) -> _MetricQuerySpec:
    mq = wq.metric_query
    assert mq is not None

    es_metric = field_map.map_metric(mq.metric)
    es_agg = _resolve_agg(mq.space_agg, es_metric)
    raw_group_fields = [field_map.map_tag(tag, context="metric") for tag in mq.group_by]
    group_fields = [_esql_identifier(field_name) for field_name in raw_group_fields]

    metric_cap = field_map.field_capability(es_metric, context="metric")
    if metric_cap:
        metric_assessment = assess_field_usage(
            metric_cap,
            field_name=es_metric,
            display_name=es_metric,
            usage="aggregate",
            required_type_family="numeric",
        )
        for warning in metric_assessment.warnings:
            _append_unique_warning(result, warning)
        if metric_assessment.blocking_reasons:
            if not field_map.is_aggregatable_field(es_metric, context="metric"):
                raise ValueError(f"target metric field `{es_metric}` is not aggregatable")
            raise ValueError(
                f"target metric field `{es_metric}` is typed as `{metric_cap.type or 'unknown'}` and is not safe for metric aggregation"
            )

    for raw_group_field in raw_group_fields:
        group_cap = field_map.field_capability(raw_group_field, context="metric")
        if not group_cap:
            continue
        group_assessment = assess_field_usage(
            group_cap,
            field_name=raw_group_field,
            display_name=raw_group_field,
            usage="group_by",
        )
        for warning in group_assessment.warnings:
            _append_unique_warning(result, warning)
        if group_assessment.blocking_reasons:
            raise ValueError(f"target group field `{raw_group_field}` is not aggregatable")

    where_clauses = [TIME_FILTER]
    for filt in mq.scope:
        clause = _metric_scope_to_esql(filt, field_map, context="metric")
        if clause:
            where_clauses.append(clause)
        if _scope_item_has_template_vars(filt):
            _append_unique_warning(
                result,
                "Scope filter with template variable could not be bound exactly; "
                "apply specific values via Kibana dashboard controls",
            )
        if isinstance(filt, TagFilter):
            if _has_template_vars(filt.value):
                _append_unique_warning(
                    result,
                    "Scope filter with template variable broadened to LIKE pattern; "
                    "apply specific values via Kibana dashboard controls",
                )
            filter_field = field_map.map_tag(filt.key, context="metric")
            filter_cap = field_map.field_capability(filter_field, context="metric")
            if filter_cap:
                filter_assessment = assess_field_usage(
                    filter_cap,
                    field_name=filter_field,
                    display_name=filter_field,
                    usage="filter",
                )
                for warning in filter_assessment.warnings:
                    _append_unique_warning(result, warning)

    if mq.as_rate or _needs_rate(mq):
        _append_unique_warning(
            result,
            "rate semantics approximated with delta over observed bucket span",
        )
    if mq.as_count:
        _append_unique_warning(
            result,
            "as_count interval semantics are approximated in ES|QL",
        )
    if mq.rollup:
        _append_unique_warning(
            result,
            "rollup interval is approximated in ES|QL",
        )
    if mq.fill_value == "zero":
        _append_unique_warning(
            result,
            "fill(zero) only applies to null values in returned rows; empty buckets may still be omitted",
        )

    tag_where = " AND ".join(c for c in where_clauses if c != TIME_FILTER)
    return _MetricQuerySpec(
        query_name=wq.name,
        alias=_safe_alias(wq.name or "query"),
        index=field_map.metric_index,
        where_str=" AND ".join(where_clauses),
        group_fields=group_fields,
        agg_expr=_format_agg_expr(es_agg, es_metric, mq),
        mq=mq,
        es_metric=es_metric,
        tag_where_str=tag_where,
        is_counter=is_counter_metric_field(metric_cap),
    )


def _extract_formula_specs(
    widget: NormalizedWidget,
    specs: list[_MetricQuerySpec],
    plan: PanelPlan,
) -> list[_FormulaSpec]:
    spec_map = {spec.query_name: spec for spec in specs}
    if not widget.formulas:
        if plan.kibana_type == "metric" and len(specs) == 1:
            return [_FormulaSpec(ast=FormulaRef(name=specs[0].query_name), alias="value", raw=specs[0].query_name)]
        return [
            _FormulaSpec(
                ast=FormulaRef(name=spec.query_name),
                alias=_safe_alias(spec.query_name),
                raw=spec.query_name,
            )
            for spec in specs
        ]

    formulas: list[_FormulaSpec] = []
    single_metric_output = plan.kibana_type == "metric" and len(widget.formulas) == 1
    for idx, formula in enumerate(widget.formulas, start=1):
        ast = formula.expression.ast if formula.expression and formula.expression.ast else None
        raw = (formula.raw or "").strip()
        if ast is None and raw in spec_map:
            ast = FormulaRef(name=raw)
        if ast is None:
            raise ValueError(f"formula syntax not recognized: {formula.raw or '<empty>'}")
        alias = "value" if single_metric_output else _safe_alias(formula.alias or raw or f"formula_{idx}")
        formulas.append(_FormulaSpec(ast=ast, alias=alias, raw=raw))
    return formulas


def _try_translate_formula_reducer(
    formulas: list[_FormulaSpec],
    spec_map: dict[str, _MetricQuerySpec],
    plan: PanelPlan,
) -> str | None:
    if len(formulas) != 1:
        return None
    formula = formulas[0]
    ast = formula.ast
    if not isinstance(ast, FormulaFuncCall):
        return None
    fn_name = ast.name.lower()
    if fn_name not in ("count_nonzero", "count_not_null"):
        return None
    if len(ast.args) != 1 or not isinstance(ast.args[0], FormulaRef):
        return None
    spec = spec_map.get(ast.args[0].name)
    if spec is None:
        raise ValueError(f"unknown query reference in {fn_name}: {ast.args[0].name}")

    dim_exprs, _ = _metric_dimension_exprs(
        spec.group_fields,
        include_time_bucket=plan.kibana_type in ("xy", "heatmap"),
    )
    first_stage = (
        f"| STATS {spec.alias} = {spec.agg_expr} BY {', '.join(dim_exprs)}"
        if dim_exprs
        else f"| STATS {spec.alias} = {spec.agg_expr}"
    )
    predicate = (
        f"{spec.alias} > 0"
        if fn_name == "count_nonzero"
        else f"{spec.alias} IS NOT NULL"
    )
    lines = [
        f"FROM {spec.index}",
        f"| WHERE {spec.where_str}",
        first_stage,
        f"| WHERE {predicate}",
    ]
    if plan.kibana_type in ("xy", "heatmap"):
        lines.append("| STATS value = COUNT(*) BY time_bucket")
        lines.append("| SORT time_bucket")
    else:
        lines.append("| STATS value = COUNT(*)")
    return "\n".join(lines)


@dataclass
class _CountReducer:
    alias: str
    query_name: str
    predicate: str


def _try_translate_count_formula_pipeline(
    formulas: list[_FormulaSpec],
    spec_map: dict[str, _MetricQuerySpec],
    plan: PanelPlan,
) -> str | None:
    if not formulas or plan.kibana_type not in ("metric", "xy"):
        return None

    reducers: dict[tuple[str, str, str], _CountReducer] = {}
    output_exprs: list[tuple[str, str]] = []
    used_query_names: set[str] = set()
    for formula in formulas:
        expr = _count_formula_ast_to_esql(formula.ast, spec_map, reducers, used_query_names)
        if expr is None:
            return None
        output_exprs.append((formula.alias, expr))

    if not reducers or len(used_query_names) != 1:
        return None

    spec = spec_map[next(iter(used_query_names))]
    include_time_bucket = plan.kibana_type == "xy"
    dim_exprs, _dim_aliases = _metric_dimension_exprs(
        spec.group_fields,
        include_time_bucket=include_time_bucket,
    )
    lines = [
        f"FROM {spec.index}",
        f"| WHERE {spec.where_str}",
    ]
    first_stats = f"{spec.alias} = {spec.agg_expr}"
    if dim_exprs:
        lines.append(f"| STATS {first_stats} BY {', '.join(dim_exprs)}")
    else:
        lines.append(f"| STATS {first_stats}")

    second_group_fields = ["time_bucket"] if include_time_bucket else []
    reducer_parts = [
        f"{reducer.alias} = COUNT(*) WHERE {reducer.predicate}"
        for reducer in reducers.values()
    ]
    if second_group_fields:
        lines.append(f"| STATS {', '.join(reducer_parts)} BY {', '.join(second_group_fields)}")
    else:
        lines.append(f"| STATS {', '.join(reducer_parts)}")

    eval_parts = [
        f"{alias} = {expr}"
        for alias, expr in output_exprs
    ]
    lines.append(f"| EVAL {', '.join(eval_parts)}")

    keep_fields = second_group_fields + [alias for alias, _ in output_exprs]
    if keep_fields:
        lines.append(f"| KEEP {', '.join(keep_fields)}")
    if include_time_bucket:
        lines.append("| SORT time_bucket")
    return "\n".join(lines)


def _count_formula_ast_to_esql(
    node: Any,
    spec_map: dict[str, _MetricQuerySpec],
    reducers: dict[tuple[str, str, str], _CountReducer],
    used_query_names: set[str],
) -> str | None:
    if isinstance(node, FormulaNumber):
        return _formula_number_literal(node.value)
    if isinstance(node, FormulaUnary):
        operand = _count_formula_ast_to_esql(node.operand, spec_map, reducers, used_query_names)
        if operand is None or node.op != "-":
            return None
        return f"(-{operand})"
    if isinstance(node, FormulaBinOp):
        left = _count_formula_ast_to_esql(node.left, spec_map, reducers, used_query_names)
        right = _count_formula_ast_to_esql(node.right, spec_map, reducers, used_query_names)
        if left is None or right is None:
            return None
        return f"({left} {node.op} {right})"
    if isinstance(node, FormulaFuncCall):
        fn_name = (node.name or "").lower()
        if fn_name == "default_zero" and len(node.args or []) == 1:
            arg = _count_formula_ast_to_esql(node.args[0], spec_map, reducers, used_query_names)
            return f"COALESCE({arg}, 0)" if arg is not None else None
        if fn_name == "exclude_null" and len(node.args or []) == 1:
            return _count_formula_ast_to_esql(node.args[0], spec_map, reducers, used_query_names)
        if fn_name in ("count_nonzero", "count_not_null") and len(node.args or []) == 1:
            reducer = _count_reducer_for_arg(
                fn_name,
                node.args[0],
                spec_map,
                reducers,
                used_query_names,
            )
            return reducer.alias if reducer else None
    return None


def _count_reducer_for_arg(
    fn_name: str,
    arg: Any,
    spec_map: dict[str, _MetricQuerySpec],
    reducers: dict[tuple[str, str, str], _CountReducer],
    used_query_names: set[str],
) -> _CountReducer | None:
    query_ref, extra_predicate, suffix = _count_reducer_query_predicate(arg, spec_map)
    if query_ref is None:
        return None
    spec = spec_map.get(query_ref)
    if spec is None:
        return None
    base_predicate = (
        f"{spec.alias} > 0"
        if fn_name == "count_nonzero"
        else f"{spec.alias} IS NOT NULL"
    )
    predicates = [base_predicate]
    if extra_predicate:
        predicates.append(extra_predicate)
    predicate = " AND ".join(predicates)
    key = (fn_name, query_ref, predicate)
    if key not in reducers:
        alias_suffix = f"_{suffix}" if suffix else ""
        reducers[key] = _CountReducer(
            alias=f"_{fn_name}_{spec.alias}{alias_suffix}",
            query_name=query_ref,
            predicate=predicate,
        )
    used_query_names.add(query_ref)
    return reducers[key]


def _count_reducer_query_predicate(
    arg: Any,
    spec_map: dict[str, _MetricQuerySpec],
) -> tuple[str | None, str, str]:
    if isinstance(arg, FormulaRef):
        return arg.name, "", ""
    if not isinstance(arg, FormulaFuncCall):
        return None, "", ""
    fn_name = (arg.name or "").lower()
    if fn_name not in ("cutoff_max", "cutoff_min") or len(arg.args or []) != 2:
        return None, "", ""
    ref, threshold = arg.args
    if not isinstance(ref, FormulaRef) or not isinstance(threshold, FormulaNumber):
        return None, "", ""
    spec = spec_map.get(ref.name)
    if spec is None:
        return None, "", ""
    op = "<=" if fn_name == "cutoff_max" else ">="
    threshold_literal = _formula_number_literal(threshold.value)
    suffix = f"{fn_name}_{threshold_literal.replace('-', 'neg_').replace('.', '_')}"
    return ref.name, f"{spec.alias} {op} {threshold_literal}", suffix


def _formula_number_literal(value: float | int | None) -> str:
    val = value if value is not None else 0
    return str(int(val)) if float(val).is_integer() else str(val)


def _resolve_used_specs(
    formulas: list[_FormulaSpec],
    spec_map: dict[str, _MetricQuerySpec],
) -> list[_MetricQuerySpec]:
    used: list[_MetricQuerySpec] = []
    for formula in formulas:
        for ref_name in _formula_ref_names(formula.ast):
            spec = spec_map.get(ref_name)
            if spec is None:
                raise ValueError(f"unknown query reference in formula: {ref_name}")
            if spec not in used:
                used.append(spec)
    if not used:
        raise ValueError("formula has no query references")
    return used


def _ensure_formula_specs_compatible(specs: list[_MetricQuerySpec]) -> None:
    base = specs[0]
    for spec in specs[1:]:
        if spec.index != base.index:
            raise ValueError("formula queries span different index patterns")
        if spec.group_fields != base.group_fields:
            # Different per-query groupings is a semantic ambiguity DD
            # resolves by convention; we can't reproduce it cleanly in
            # one ES|QL pipeline. Surface as requires_manual so the
            # widget gets a placeholder for human review.
            raise _RequiresManualError(
                "multi-query formulas with different groupings need a "
                "manually-designed ES|QL query (e.g. UNION ALL or split "
                "into separate panels) — automatic translation would be "
                "semantically ambiguous"
            )
    # Heterogeneous filters across specs are translated via per-aggregation
    # WHERE clauses (no error). Heterogeneous groupings still raise because
    # they would require a UNION/join that ES|QL can't express in one STATS.


def _specs_have_heterogeneous_filters(specs: list[_MetricQuerySpec]) -> bool:
    if len(specs) < 2:
        return False
    base = specs[0].where_str
    return any(s.where_str != base for s in specs[1:])


def _metric_dimension_exprs(
    group_fields: list[str],
    include_time_bucket: bool,
) -> tuple[list[str], list[str]]:
    exprs: list[str] = []
    aliases: list[str] = []
    if include_time_bucket:
        exprs.append(f"time_bucket = {TIME_BUCKET_EXPR}")
        aliases.append("time_bucket")
    exprs.extend(group_fields)
    aliases.extend(group_fields)
    return exprs, aliases


def _extract_metric_sort(
    widget: NormalizedWidget,
    output_fields: list[str],
) -> tuple[str, str, int]:
    sort_field = output_fields[0] if output_fields else ""
    sort_order = "DESC"
    limit = (
        _extract_toplist_limit(widget)
        if widget.widget_type in ("toplist", "bar_chart")
        else 100
    )

    for req in widget.raw_definition.get("requests", []):
        if not isinstance(req, dict):
            continue
        sort_cfg = req.get("sort", {})
        if not isinstance(sort_cfg, dict):
            continue
        count = sort_cfg.get("count")
        if isinstance(count, int):
            limit = count
        elif isinstance(count, str) and count.isdigit():
            limit = int(count)
        order_by = sort_cfg.get("order_by", [])
        if order_by and isinstance(order_by[0], dict):
            order = order_by[0].get("order", "desc")
            if isinstance(order, str) and order.upper() in ("ASC", "DESC"):
                sort_order = order.upper()
            if order_by[0].get("type") == "formula":
                idx = order_by[0].get("index", 0)
                if isinstance(idx, int) and 0 <= idx < len(output_fields):
                    sort_field = output_fields[idx]
        break

    return sort_field, sort_order, limit


def _formula_ref_names(node: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(node, FormulaRef):
        refs.append(node.name)
    elif isinstance(node, FormulaBinOp):
        refs.extend(_formula_ref_names(node.left))
        refs.extend(_formula_ref_names(node.right))
    elif isinstance(node, FormulaFuncCall):
        for arg in node.args:
            refs.extend(_formula_ref_names(arg))
    elif isinstance(node, FormulaUnary):
        refs.extend(_formula_ref_names(node.operand))
    # FormulaString / FormulaNumber: no refs
    return list(dict.fromkeys(refs))


_BUCKET_SPAN_FORMULA_FNS = {"per_second", "per_minute", "per_hour", "rate"}
_DERIVATIVE_FORMULA_FNS = {"rate", "diff", "monotonic_diff"}


def _formula_needs_bucket_span(node: Any) -> bool:
    if isinstance(node, FormulaFuncCall):
        fn_name = (node.name or "").lower()
        if fn_name in _BUCKET_SPAN_FORMULA_FNS:
            return True
        return any(_formula_needs_bucket_span(arg) for arg in (node.args or []))
    if isinstance(node, FormulaBinOp):
        return _formula_needs_bucket_span(node.left) or _formula_needs_bucket_span(node.right)
    if isinstance(node, FormulaUnary):
        return _formula_needs_bucket_span(node.operand)
    return False


def _collect_derivative_query_refs(node: Any) -> set[str]:
    """Find query names that are direct arguments to rate()/diff() so the
    STATS clause can emit FIRST/LAST aggregations for them (enabling true
    derivative semantics, not the value/span approximation)."""

    refs: set[str] = set()
    if isinstance(node, FormulaFuncCall):
        fn_name = (node.name or "").lower()
        if fn_name in _DERIVATIVE_FORMULA_FNS and node.args:
            arg = node.args[0]
            if isinstance(arg, FormulaRef):
                refs.add(arg.name)
        for child in (node.args or []):
            refs |= _collect_derivative_query_refs(child)
    elif isinstance(node, FormulaBinOp):
        refs |= _collect_derivative_query_refs(node.left)
        refs |= _collect_derivative_query_refs(node.right)
    elif isinstance(node, FormulaUnary):
        refs |= _collect_derivative_query_refs(node.operand)
    return refs


def _formula_ast_to_esql(
    node: Any,
    query_aliases: dict[str, str],
    warnings: list[str] | None = None,
    derivative_refs: set[str] | None = None,
) -> str:
    if isinstance(node, FormulaRef):
        if node.name not in query_aliases:
            raise ValueError(f"unknown query reference in formula: {node.name}")
        return _esql_identifier(query_aliases[node.name])
    if isinstance(node, FormulaNumber):
        val = node.value if node.value is not None else 0
        if float(val).is_integer():
            return str(int(val))
        return str(val)
    if isinstance(node, FormulaString):
        # String literals only appear as positional args to functions like
        # top(); they have no direct ES|QL equivalent in an expression
        # context and the surrounding function handler reads them directly.
        raise ValueError(
            f"string literal {node.value!r} is not allowed in an expression position"
        )
    if isinstance(node, FormulaBinOp):
        left = _formula_ast_to_esql(node.left, query_aliases, warnings, derivative_refs)
        right = _formula_ast_to_esql(node.right, query_aliases, warnings, derivative_refs)
        return f"({left} {node.op} {right})"
    if isinstance(node, FormulaUnary):
        operand = _formula_ast_to_esql(node.operand, query_aliases, warnings, derivative_refs)
        if node.op == "-":
            return f"(-{operand})"
        raise ValueError(f"unsupported unary formula operator: {node.op}")
    if isinstance(node, FormulaFuncCall):
        fn_name = (node.name or "").lower()
        if fn_name == "top":
            # top(query_ref, N, 'agg', 'order') — list-semantic top-N. We
            # cannot apply the N/agg/order constraints inside an expression,
            # so we unwrap to the first arg and record an approximation
            # warning. The surrounding panel sort/limit logic still applies.
            if not node.args:
                raise ValueError("top() requires at least one argument")
            if warnings is not None:
                warnings.append(
                    "top() filtering is approximated — uses panel-level sort/limit "
                    "instead of formula-scoped top-N"
                )
            return _formula_ast_to_esql(node.args[0], query_aliases, warnings, derivative_refs)
        args = [_formula_ast_to_esql(arg, query_aliases, warnings, derivative_refs) for arg in (node.args or [])]
        if fn_name == "abs" and len(args) == 1:
            return f"ABS({args[0]})"
        if fn_name == "ceil" and len(args) == 1:
            return f"CEIL({args[0]})"
        if fn_name == "floor" and len(args) == 1:
            return f"FLOOR({args[0]})"
        if fn_name == "round" and len(args) in (1, 2):
            return f"ROUND({', '.join(args)})"
        if fn_name == "default_zero" and len(args) == 1:
            if warnings is not None:
                warnings.append(
                    "default_zero() only coalesces returned rows; missing series or empty buckets may still be omitted"
                )
            return f"COALESCE({args[0]}, 0)"
        if fn_name == "exclude_null" and len(args) == 1:
            return args[0]
        if fn_name in ("per_second", "per_minute", "per_hour") and len(args) == 1:
            multiplier = {"per_second": 1, "per_minute": 60, "per_hour": 3600}[fn_name]
            expr = f"({args[0]}) / bucket_span_seconds"
            if multiplier != 1:
                expr = f"({expr}) * {multiplier}"
            return expr
        if fn_name == "rate" and len(args) == 1:
            # DD rate(x) is the per-second rate of change. When x is a
            # direct query reference, the STATS clause has emitted
            # {alias}_first and {alias}_last via FIRST(metric, @timestamp)
            # / LAST(metric, @timestamp), enabling proper derivative
            # semantics: (last - first) / bucket_span_seconds.
            orig_arg = (node.args or [None])[0]
            if (
                derivative_refs is not None
                and isinstance(orig_arg, FormulaRef)
                and orig_arg.name in derivative_refs
            ):
                if warnings is not None:
                    warnings.append(
                        "rate() on a query reference is approximated with bucket FIRST/LAST deltas; "
                        "values may differ for non-monotonic gauges"
                    )
                alias = _esql_identifier(query_aliases[orig_arg.name])
                return f"({alias}_last - {alias}_first) / bucket_span_seconds"
            if warnings is not None:
                warnings.append(
                    "rate() approximated as per-bucket value divided by bucket span; "
                    "for counters this matches DD semantics within a bucket"
                )
            return f"({args[0]}) / bucket_span_seconds"
        if fn_name in ("diff", "monotonic_diff") and len(args) == 1:
            # DD diff(x) is x[t]-x[t-1]. With FIRST/LAST in STATS we can
            # express it as (last - first) per bucket; otherwise fall
            # back to identity with warning.
            orig_arg = (node.args or [None])[0]
            if (
                derivative_refs is not None
                and isinstance(orig_arg, FormulaRef)
                and orig_arg.name in derivative_refs
            ):
                alias = _esql_identifier(query_aliases[orig_arg.name])
                return f"({alias}_last - {alias}_first)"
            if warnings is not None:
                warnings.append(
                    f"{fn_name}() per-bucket delta is approximated as the bucket value; "
                    "absolute numbers may differ from DD"
                )
            return f"({args[0]})"
        raise ValueError(f"unsupported formula function: {node.name}")
    raise ValueError(f"unsupported formula node: {type(node).__name__}")


def _metric_scope_to_esql(scope_item: Any, field_map: FieldMapProfile, context: str = "") -> str:
    if isinstance(scope_item, ScopeBoolOp):
        clauses = [
            _metric_scope_to_esql(child, field_map, context=context)
            for child in scope_item.children
        ]
        clauses = [clause for clause in clauses if clause]
        if not clauses:
            return ""
        joiner = f" {scope_item.op} "
        return "(" + joiner.join(clauses) + ")"
    return _tag_filter_to_esql(scope_item, field_map, context=context)


def _scope_item_has_template_vars(scope_item: Any) -> bool:
    if isinstance(scope_item, TagFilter):
        return _has_template_vars(scope_item.key) or _has_template_vars(scope_item.value)
    if isinstance(scope_item, ScopeBoolOp):
        return any(_scope_item_has_template_vars(child) for child in scope_item.children)
    return False


def _append_unique_warning(result: TranslationResult, message: str) -> None:
    if message not in result.warnings:
        result.warnings.append(message)


def _safe_alias(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", raw or "").strip("_").lower()
    if not cleaned:
        return "value"
    if cleaned[0].isdigit():
        cleaned = f"f_{cleaned}"
    return cleaned


def _metric_is_count_like(metric_name: str) -> bool:
    lowered = metric_name.lower()
    return lowered.endswith((".count", "_count", ".total", "_total"))


def _extract_change_widget_request(widget: NormalizedWidget) -> dict[str, Any]:
    for req in (widget.raw_definition or {}).get("requests", []):
        if isinstance(req, dict):
            return req
    return {}


def _change_widget_window_seconds(widget: NormalizedWidget) -> int:
    live_span = (
        str((widget.time or {}).get("live_span", "") or "").strip()
        or str(((widget.raw_definition or {}).get("time", {}) or {}).get("live_span", "") or "").strip()
    )
    return _datadog_span_to_seconds(live_span)


def _datadog_span_to_seconds(raw: str) -> int:
    match = _DATADOG_SPAN_RE.fullmatch(str(raw or "").strip())
    if not match:
        return 0
    amount = int(match.group("amount"))
    unit = str(match.group("unit") or "").lower()
    return amount * _SPAN_UNIT_SECONDS.get(unit, 0)


def _seconds_to_esql_span(total_seconds: int) -> str:
    if total_seconds <= 0:
        return ""
    if total_seconds % _SPAN_UNIT_SECONDS["d"] == 0:
        return f"{total_seconds // _SPAN_UNIT_SECONDS['d']} days"
    if total_seconds % _SPAN_UNIT_SECONDS["w"] == 0:
        return f"{total_seconds // _SPAN_UNIT_SECONDS['w']} weeks"
    if total_seconds % _SPAN_UNIT_SECONDS["h"] == 0:
        return f"{total_seconds // _SPAN_UNIT_SECONDS['h']} hours"
    if total_seconds % _SPAN_UNIT_SECONDS["m"] == 0:
        return f"{total_seconds // _SPAN_UNIT_SECONDS['m']} minutes"
    return f"{total_seconds} seconds"


# ---------------------------------------------------------------------------
# Log translation
# ---------------------------------------------------------------------------

def _translate_log_widget(
    widget: NormalizedWidget,
    plan: PanelPlan,
    field_map: FieldMapProfile,
    result: TranslationResult,
    use_kql_bridge: bool = False,
) -> str:
    """Translate log widgets through the registry-backed log passes."""
    context = _TranslationExecutionContext(
        widget=widget,
        plan=plan,
        field_map=field_map,
        result=result,
        use_kql_bridge=use_kql_bridge,
    )
    LOG_TRANSLATORS.apply(
        context,
        stop_when=lambda ctx, _detail: ctx.output is not None,
    )
    if context.output is None:
        raise ValueError("no parsed log queries")
    return str(context.output)


@LOG_TRANSLATORS.register(
    "datadog.translate.log_direct_esql",
    priority=10,
    summary="Translate structured Datadog log widgets directly to ES|QL.",
)
def log_direct_esql_rule(context: _TranslationExecutionContext) -> str | None:
    if context.use_kql_bridge:
        return None
    context.output = _build_log_widget_query(
        context.widget,
        context.plan,
        context.field_map,
        use_kql_bridge=False,
    )
    return "translated log widget with direct ES|QL filters"


@LOG_TRANSLATORS.register(
    "datadog.translate.log_kql_bridge",
    priority=20,
    summary="Translate free-text Datadog log widgets via the ES|QL KQL bridge.",
)
def log_kql_bridge_rule(context: _TranslationExecutionContext) -> str | None:
    if not context.use_kql_bridge:
        return None
    context.output = _build_log_widget_query(
        context.widget,
        context.plan,
        context.field_map,
        use_kql_bridge=True,
    )
    return "translated log widget via KQL bridge"


def _build_log_widget_query(
    widget: NormalizedWidget,
    plan: PanelPlan,
    field_map: FieldMapProfile,
    use_kql_bridge: bool = False,
) -> str:
    log_queries = [q for q in widget.queries if q.log_query]
    if not log_queries:
        raise ValueError("no parsed log queries")
    if len(log_queries) > 1:
        if plan.kibana_type == "xy":
            return _build_multi_log_timeseries_query(
                widget,
                log_queries,
                field_map,
                use_kql_bridge=use_kql_bridge,
            )
        raise ValueError("widgets with multiple log queries are not translated safely yet")

    primary = log_queries[0]
    lq = primary.log_query
    assert lq is not None

    index = field_map.logs_index
    tag_map = {
        key: _esql_identifier(field_map.map_tag(key, context="log"))
        for key in field_map.tag_map
    }

    where_parts = [TIME_FILTER]
    if lq.ast:
        if use_kql_bridge:
            kql_str = log_ast_to_kql(lq.ast, field_map=tag_map)
            if kql_str and kql_str != "*":
                where_parts.append(f'KQL("{_esql_escape(kql_str)}")')
        else:
            esql_filter = log_ast_to_esql_where(lq.ast, field_map)
            if esql_filter:
                where_parts.append(esql_filter)

    where_str = " AND ".join(where_parts)

    is_stream = widget.widget_type in ("log_stream", "list_stream")
    is_timeseries = plan.kibana_type == "xy"
    is_toplist = widget.widget_type in ("toplist", "bar_chart")
    is_scalar = plan.kibana_type == "metric"

    group_fields = _infer_log_group_by(widget, field_map)

    if is_stream:
        keep_fields = ["@timestamp", "message", "log.level", "service.name", "host.name"]
        keep_str = ", ".join(keep_fields)
        return (
            f"FROM {index}\n"
            f"| WHERE {where_str}\n"
            f"| SORT @timestamp DESC\n"
            f"| KEEP {keep_str}\n"
            f"| LIMIT 100"
        )

    if is_timeseries:
        time_bucket = TIME_BUCKET_EXPR
        group_clause = f"time_bucket = {time_bucket}"
        if group_fields:
            group_clause += ", " + ", ".join(group_fields)
        return (
            f"FROM {index}\n"
            f"| WHERE {where_str}\n"
            f"| STATS count = COUNT(*) BY {group_clause}\n"
            f"| SORT time_bucket"
        )

    if is_toplist:
        limit = _extract_toplist_limit(widget)
        if group_fields:
            gb = ", ".join(group_fields)
            return (
                f"FROM {index}\n"
                f"| WHERE {where_str}\n"
                f"| STATS count = COUNT(*) BY {gb}\n"
                f"| SORT count DESC\n"
                f"| LIMIT {limit}"
            )
        return (
            f"FROM {index}\n"
            f"| WHERE {where_str}\n"
            f"| STATS count = COUNT(*)\n"
            f"| LIMIT {limit}"
        )

    if is_scalar:
        return (
            f"FROM {index}\n"
            f"| WHERE {where_str}\n"
            f"| STATS count = COUNT(*)"
        )

    if group_fields:
        gb = ", ".join(group_fields)
        return (
            f"FROM {index}\n"
            f"| WHERE {where_str}\n"
            f"| STATS count = COUNT(*) BY {gb}\n"
            f"| SORT count DESC\n"
            f"| LIMIT 100"
        )

    return (
        f"FROM {index}\n"
        f"| WHERE {where_str}\n"
        f"| SORT @timestamp DESC\n"
        f"| LIMIT 100"
    )


def _build_multi_log_timeseries_query(
    widget: NormalizedWidget,
    log_queries: list[WidgetQuery],
    field_map: FieldMapProfile,
    use_kql_bridge: bool = False,
) -> str:
    raw_query_defs = _raw_log_query_defs_by_name(widget)
    query_aliases = {q.name: _safe_alias(q.name or "query") for q in log_queries}
    group_keys = _shared_log_query_group_by(log_queries, raw_query_defs)
    group_fields = [_esql_identifier(field_map.map_tag(k, context="log")) for k in group_keys]

    filters: dict[str, str] = {}
    stats_parts: list[str] = []
    for query in log_queries:
        filter_expr = _log_query_filter_expr(query, field_map, use_kql_bridge=use_kql_bridge)
        if filter_expr:
            filters[query.name] = filter_expr
        agg_expr = _log_compute_agg_expr(raw_query_defs.get(query.name, {}), field_map)
        alias = query_aliases[query.name]
        if filter_expr:
            stats_parts.append(f"{alias} = {agg_expr} WHERE {filter_expr}")
        else:
            stats_parts.append(f"{alias} = {agg_expr}")

    where_parts = [TIME_FILTER]
    if filters:
        where_parts.append("(" + " OR ".join(f"({expr})" for expr in filters.values()) + ")")

    group_clause = f"time_bucket = {TIME_BUCKET_EXPR}"
    if group_fields:
        group_clause += ", " + ", ".join(group_fields)

    lines = [
        f"FROM {field_map.logs_index}",
        f"| WHERE {' AND '.join(where_parts)}",
        f"| STATS {', '.join(stats_parts)} BY {group_clause}",
    ]

    output_fields: list[str] = []
    eval_parts: list[str] = []
    if widget.formulas:
        for idx, formula in enumerate(widget.formulas, start=1):
            ast = formula.expression.ast if formula.expression and formula.expression.ast else None
            raw = (formula.raw or "").strip()
            if ast is None and raw in query_aliases:
                ast = FormulaRef(name=raw)
            if ast is None:
                raise ValueError(f"formula syntax not recognized: {formula.raw or '<empty>'}")
            alias = _safe_alias(formula.alias or raw or f"formula_{idx}")
            expr = _formula_ast_to_esql(ast, query_aliases)
            output_fields.append(alias)
            if expr != _esql_identifier(alias):
                eval_parts.append(f"{alias} = {expr}")
    else:
        output_fields = [query_aliases[q.name] for q in log_queries]

    if eval_parts:
        lines.append(f"| EVAL {', '.join(eval_parts)}")

    keep_fields = ["time_bucket"] + group_fields + output_fields
    lines.append(f"| KEEP {', '.join(keep_fields)}")
    lines.append("| SORT time_bucket")
    return "\n".join(lines)


def _raw_log_query_defs_by_name(widget: NormalizedWidget) -> dict[str, dict[str, Any]]:
    raw_defs: dict[str, dict[str, Any]] = {}
    for req in widget.raw_definition.get("requests", []) or []:
        if not isinstance(req, dict):
            continue
        for query in req.get("queries", []) or []:
            if not isinstance(query, dict) or query.get("data_source") not in LOG_DATA_SOURCES:
                continue
            name = str(query.get("name") or "")
            if name:
                raw_defs[name] = query
    return raw_defs


def _shared_log_query_group_by(
    log_queries: list[WidgetQuery],
    raw_query_defs: dict[str, dict[str, Any]],
) -> list[str]:
    group_sets = [
        _raw_log_query_group_by(raw_query_defs.get(query.name, {}))
        for query in log_queries
    ]
    if not group_sets:
        return []
    first = group_sets[0]
    if any(group != first for group in group_sets[1:]):
        raise ValueError("multiple log queries with different group-by fields need manual review")
    return first


def _raw_log_query_group_by(raw_query: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for item in raw_query.get("group_by", []) or []:
        if isinstance(item, dict):
            facet = str(item.get("facet") or "").strip()
            if facet:
                fields.append(facet)
    return fields


def _log_query_filter_expr(
    query: WidgetQuery,
    field_map: FieldMapProfile,
    use_kql_bridge: bool = False,
) -> str:
    lq = query.log_query
    if not lq or not lq.ast:
        return ""
    if use_kql_bridge:
        tag_map = {
            key: _esql_identifier(field_map.map_tag(key, context="log"))
            for key in field_map.tag_map
        }
        kql_str = log_ast_to_kql(lq.ast, field_map=tag_map)
        return f'KQL("{_esql_escape(kql_str)}")' if kql_str and kql_str != "*" else ""
    return log_ast_to_esql_where(lq.ast, field_map)


def _log_compute_agg_expr(raw_query: dict[str, Any], field_map: FieldMapProfile) -> str:
    compute = raw_query.get("compute", {}) if isinstance(raw_query, dict) else {}
    if not isinstance(compute, dict):
        compute = {}
    aggregation = str(compute.get("aggregation") or "count").lower().strip()
    metric = _normalize_log_measure_field(str(compute.get("metric") or ""))
    if aggregation == "count" and not metric:
        return "COUNT(*)"
    if not metric:
        raise ValueError(f"log compute aggregation '{aggregation}' requires a metric")
    field_ident = _esql_identifier(field_map.map_tag(metric, context="log"))
    if aggregation == "count":
        return f"COUNT({field_ident})"
    if aggregation == "cardinality":
        return f"COUNT_DISTINCT({field_ident})"
    if aggregation == "sum":
        return f"SUM({field_ident})"
    if aggregation == "avg":
        return f"AVG({field_ident})"
    raise ValueError(f"unsupported log compute aggregation: {aggregation}")


def _normalize_log_measure_field(field_name: str) -> str:
    return field_name.strip()


# ---------------------------------------------------------------------------
# ES|QL building helpers
# ---------------------------------------------------------------------------

def _build_timeseries_esql(
    index: str,
    where: str,
    agg_expr: str,
    group_fields: list[str],
) -> str:
    time_bucket = TIME_BUCKET_EXPR
    group_clause = f"time_bucket = {time_bucket}"
    if group_fields:
        group_clause += ", " + ", ".join(group_fields)

    return (
        f"FROM {index}\n"
        f"| WHERE {where}\n"
        f"| STATS value = {agg_expr} BY {group_clause}\n"
        f"| SORT time_bucket"
    )


def _build_toplist_esql(
    index: str,
    where: str,
    agg_expr: str,
    group_fields: list[str],
    limit: int,
) -> str:
    return _build_categorical_esql(
        index,
        where,
        agg_expr,
        group_fields,
        sort_field="value",
        sort_order="DESC",
        limit=limit,
    )


def _build_table_esql(
    index: str,
    where: str,
    agg_expr: str,
    group_fields: list[str],
) -> str:
    return _build_categorical_esql(
        index,
        where,
        agg_expr,
        group_fields,
        sort_field="value",
        sort_order="DESC",
        limit=100,
    )


def _build_scalar_esql(
    index: str,
    where: str,
    agg_expr: str,
    reducer: str | None = None,
) -> str:
    if reducer:
        lines = [
            f"FROM {index}",
            f"| WHERE {where}",
            f"| STATS _bucket_value = {agg_expr} BY time_bucket = {TIME_BUCKET_EXPR}",
        ]
        lines.append(f"| STATS value = {_series_reducer_expr(reducer, '_bucket_value')}")
        return "\n".join(lines)
    return (
        f"FROM {index}\n"
        f"| WHERE {where}\n"
        f"| STATS value = {agg_expr}"
    )


def _build_categorical_esql(
    index: str,
    where: str,
    agg_expr: str,
    group_fields: list[str],
    sort_field: str,
    sort_order: str,
    limit: int | None,
    reducer: str | None = None,
) -> str:
    lines = [
        f"FROM {index}",
        f"| WHERE {where}",
    ]
    if reducer:
        group_clause = f"time_bucket = {TIME_BUCKET_EXPR}"
        if group_fields:
            group_clause += ", " + ", ".join(group_fields)
        lines.append(f"| STATS _bucket_value = {agg_expr} BY {group_clause}")
        reduce_expr = _series_reducer_expr(reducer, "_bucket_value")
        if group_fields:
            lines.append(f"| STATS value = {reduce_expr} BY {', '.join(group_fields)}")
        else:
            lines.append(f"| STATS value = {reduce_expr}")
    elif group_fields:
        lines.append(f"| STATS value = {agg_expr} BY {', '.join(group_fields)}")
    else:
        lines.append(f"| STATS value = {agg_expr}")
    if sort_field:
        lines.append(f"| SORT {sort_field} {sort_order}")
    if limit is not None and limit > 0:
        lines.append(f"| LIMIT {limit}")
    return "\n".join(lines)


def _format_agg_expr(agg: str, metric_field: str, mq: MetricQuery | None = None) -> str:
    metric_expr = _esql_identifier(metric_field)
    if mq and mq.as_rate and (mq.space_agg or "").lower() == "count":
        expr = 'COUNT(*) / (DATE_DIFF("seconds", MIN(@timestamp), MAX(@timestamp)) + 1)'
    elif mq and (mq.as_rate or _needs_rate(mq)):
        expr = _rate_approx_expr(metric_field, mq)
    else:
        if "%" in agg:
            expr = agg.replace("%", metric_expr)
        elif agg == "COUNT":
            expr = f"COUNT({metric_expr})"
        elif mq and mq.as_count and not _metric_is_count_like(mq.metric):
            expr = f"SUM({metric_expr})"
        else:
            expr = f"{agg}({metric_expr})"

    multiplier = _rate_multiplier(mq) if mq else 1
    if multiplier != 1:
        expr = f"({expr}) * {multiplier}"
    return expr


def _resolve_agg(dd_agg: str, metric_field: str) -> str:
    dd_agg = (dd_agg or "").lower().strip()
    if dd_agg in DD_AGG_TO_ESQL:
        return DD_AGG_TO_ESQL[dd_agg]
    raise ValueError(f"unsupported Datadog aggregator: {dd_agg or '<empty>'}")


def _normalize_request_reducer(raw: str) -> str:
    reducer = raw.lower().strip()
    if not reducer:
        return ""
    if reducer not in {"avg", "sum", "min", "max", "last"}:
        raise ValueError(f"unsupported Datadog request aggregator: {raw}")
    return reducer


def _request_reducer_for_queries(
    queries: list[WidgetQuery],
    default: str | None = None,
) -> str | None:
    reducers = {
        _normalize_request_reducer(q.aggregator)
        for q in queries
        if q.metric_query and _normalize_request_reducer(q.aggregator)
    }
    if not reducers:
        return default
    if len(reducers) > 1:
        raise ValueError("multi-query widgets with different request aggregators are not translated safely yet")
    return next(iter(reducers))


def _extract_top_function_config(mq: MetricQuery | None) -> _TopFunctionConfig:
    config = _TopFunctionConfig()
    if not mq:
        return config
    top_fn = next((fn for fn in mq.functions if (fn.name or "").lower() == "top"), None)
    if not top_fn:
        return config
    if top_fn.args:
        limit = top_fn.args[0]
        if isinstance(limit, int):
            config.limit = limit
        elif isinstance(limit, str) and limit.isdigit():
            config.limit = int(limit)
    if len(top_fn.args) >= 2 and isinstance(top_fn.args[1], str):
        reducer = str(top_fn.args[1]).strip().lower()
        reducer_map = {"mean": "avg", "avg": "avg", "sum": "sum", "min": "min", "max": "max", "last": "last"}
        config.reducer = reducer_map.get(reducer)
    if len(top_fn.args) >= 3 and isinstance(top_fn.args[2], str):
        order = str(top_fn.args[2]).strip().upper()
        if order in {"ASC", "DESC"}:
            config.sort_order = order
    return config


def _extract_top_params(ast: Any) -> tuple[int, str, str] | None:
    """If the formula root is top(expr, N, agg, order), return (N, agg, order).

    The Datadog top() signature is top(expr, N, 'agg', 'order') where:
    - expr is the inner formula expression (args[0])
    - N    is the series count limit   (args[1])
    - agg  is the ranking aggregator   (args[2]): 'mean'/'sum'/'min'/'max'/'last'
    - order is 'asc' or 'desc'         (args[3])
    """
    if not isinstance(ast, FormulaFuncCall) or (ast.name or "").lower() != "top":
        return None
    args = ast.args or []
    n = 10
    agg = "avg"
    order = "DESC"
    if len(args) >= 2:
        raw_n = args[1]
        if isinstance(raw_n, FormulaNumber):
            n = int(raw_n.value)
        elif isinstance(raw_n, (int, float)):
            n = int(raw_n)
    if len(args) >= 3:
        raw_agg = args[2]
        agg_str = (raw_agg.value if isinstance(raw_agg, FormulaString) else str(raw_agg)).strip().lower()
        reducer_map = {"mean": "avg", "avg": "avg", "sum": "sum", "min": "min", "max": "max", "last": "last"}
        agg = reducer_map.get(agg_str, "avg")
    if len(args) >= 4:
        raw_order = args[3]
        order_str = (raw_order.value if isinstance(raw_order, FormulaString) else str(raw_order)).strip().upper()
        if order_str in {"ASC", "DESC"}:
            order = order_str
    return n, agg, order


def _series_reducer_expr(reducer: str, field: str) -> str:
    field_ident = _esql_identifier(field)
    return {
        "avg": f"AVG({field_ident})",
        "sum": f"SUM({field_ident})",
        "min": f"MIN({field_ident})",
        "max": f"MAX({field_ident})",
        "last": f"LAST({field_ident}, time_bucket)",
    }[reducer]


def _tag_filter_to_esql(filt, field_map: FieldMapProfile, context: str = "") -> str:
    if not isinstance(filt, TagFilter):
        return ""

    es_field = _esql_identifier(field_map.map_tag(filt.key, context=context))
    value = filt.value or ""

    if value == "*" and not filt.negated:
        return ""

    if getattr(filt, "is_in_list", False):
        members = [
            _esql_escape(member)
            for member in value.split("|")
            if member and not _has_template_vars(member)
        ]
        if not members:
            return ""
        rendered = ", ".join(f'"{member}"' for member in members)
        op = "NOT IN" if filt.negated else "IN"
        return f"{es_field} {op} ({rendered})"

    if "|" in value:
        clauses = []
        for option in [part.strip() for part in value.split("|") if part.strip()]:
            if _has_template_vars(option):
                continue
            if "*" in option or "?" in option:
                pattern = _esql_escape(option.replace("*", "%").replace("?", "_"))
                like_op = "NOT LIKE" if filt.negated else "LIKE"
                clauses.append(f'{es_field} {like_op} "{pattern}"')
            else:
                op = "!=" if filt.negated else "=="
                clauses.append(f'{es_field} {op} "{_esql_escape(option)}"')
        if not clauses:
            return ""
        joiner = " AND " if filt.negated else " OR "
        return "(" + joiner.join(clauses) + ")"

    if _has_template_vars(value):
        pattern = _template_value_to_like_pattern(value)
        if pattern in ("", "*") and not filt.negated:
            return ""
        op = "NOT LIKE" if filt.negated else "LIKE"
        return f'{es_field} {op} "{pattern}"'

    if "*" in value or "?" in value:
        pattern = value.replace("*", "%").replace("?", "_")
        op = "NOT LIKE" if filt.negated else "LIKE"
        return f'{es_field} {op} "{_esql_escape(pattern)}"'

    op = "!=" if filt.negated else "=="
    return f'{es_field} {op} "{_esql_escape(value)}"'


def _needs_rate(mq: MetricQuery) -> bool:
    return any(
        fn.name in ("per_second", "per_minute", "per_hour", "derivative")
        for fn in mq.functions
    )


def _extract_toplist_limit(widget: NormalizedWidget) -> int:
    for f in widget.formulas:
        if f.limit and isinstance(f.limit, dict):
            count = f.limit.get("count", 10)
            if isinstance(count, int):
                return count
            if isinstance(count, str) and count.isdigit():
                return int(count)
    return 10


def _infer_log_group_by(widget: NormalizedWidget, field_map: FieldMapProfile) -> list[str]:
    """Infer group-by fields from log widget queries or formulas."""
    group_by_keys = []
    for q in widget.queries:
        if q.metric_query and q.metric_query.group_by:
            group_by_keys.extend(q.metric_query.group_by)

    raw_def = widget.raw_definition
    for req in raw_def.get("requests", []):
        if isinstance(req, dict):
            for gb in req.get("group_by", []):
                if isinstance(gb, dict):
                    facet = gb.get("facet", "")
                    if facet:
                        group_by_keys.append(facet)
            legacy_q = str(req.get("q", "") or "")
            for facet in re.findall(r'\.by\("([^"]+)"\)', legacy_q):
                group_by_keys.append(facet)

    return [_esql_identifier(field_map.map_tag(k, context="log")) for k in group_by_keys]


def _has_template_vars(text: str) -> bool:
    return bool(text and _TEMPLATE_VAR_RE.search(text))


def _template_value_to_like_pattern(value: str) -> str:
    pattern = _TEMPLATE_VAR_RE.sub("*", value)
    return _esql_escape(pattern)


def _rate_multiplier(mq: MetricQuery | None) -> int:
    if not mq:
        return 1
    fn_names = {fn.name.lower() for fn in mq.functions}
    if "per_hour" in fn_names:
        return 3600
    if "per_minute" in fn_names:
        return 60
    return 1


def _rate_approx_expr(metric_field: str, mq: MetricQuery | None = None) -> str:
    metric_ident = _esql_identifier(metric_field)
    rollup_seconds = _rollup_seconds(mq)
    if rollup_seconds is not None:
        denominator = str(rollup_seconds)
    else:
        denominator = '(DATE_DIFF("seconds", MIN(@timestamp), MAX(@timestamp)) + 1)'
    return f"(MAX({metric_ident}) - MIN({metric_ident})) / {denominator}"


def _rollup_seconds(mq: MetricQuery | None) -> float | None:
    if not mq or not mq.rollup or len(mq.rollup.args) < 2:
        return None
    interval = mq.rollup.args[1]
    if isinstance(interval, (int, float)) and interval > 0:
        return float(interval)
    return None


def _esql_identifier(field_name: str) -> str:
    parts = []
    for part in field_name.split("."):
        if _SAFE_IDENTIFIER_RE.match(part):
            parts.append(part)
        else:
            parts.append(f"`{part.replace('`', '``')}`")
    return ".".join(parts)


def _esql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Lens translation
# ---------------------------------------------------------------------------

def _translate_lens_widget(
    widget: NormalizedWidget,
    plan: PanelPlan,
    field_map: FieldMapProfile,
    result: TranslationResult,
) -> dict:
    """Build a Lens-style panel config through the registry-backed lens pass."""
    context = _TranslationExecutionContext(
        widget=widget,
        plan=plan,
        field_map=field_map,
        result=result,
    )
    LENS_TRANSLATORS.apply(
        context,
        stop_when=lambda ctx, _detail: ctx.output is not None,
    )
    if context.output is None:
        raise ValueError("no parsed metric queries for lens")
    return dict(context.output)


@LENS_TRANSLATORS.register(
    "datadog.translate.lens_single_query",
    priority=10,
    summary="Translate simple metric widgets into Lens configs.",
)
def lens_single_query_rule(context: _TranslationExecutionContext) -> str | None:
    if context.plan.backend != "lens":
        return None
    context.output = _build_lens_widget_config(
        context.widget,
        context.plan,
        context.field_map,
    )
    return "translated Lens metric widget"


def _build_lens_widget_config(
    widget: NormalizedWidget,
    plan: PanelPlan,
    field_map: FieldMapProfile,
) -> dict:
    """Build a Lens-style panel config for simple metric queries.

    Lens panels use a data view and Kibana's native aggregation framework
    instead of hand-built ES|QL. This is lower-risk for straightforward
    single-query metrics.
    """
    metric_queries = [q for q in widget.queries if q.metric_query]
    if not metric_queries:
        raise ValueError("no parsed metric queries for lens")

    wq = metric_queries[0]
    mq = wq.metric_query
    assert mq is not None

    es_metric = field_map.map_metric(mq.metric)
    agg = _resolve_agg(mq.space_agg, es_metric)

    # For scalar/metric panels the Datadog request ``aggregator`` (time
    # reducer) determines what the user actually sees.  ``aggregator: last``
    # means "show the most recent value" — in Lens this must be
    # ``last_value``, not the space-aggregation which would accumulate
    # every document in the query window.
    if plan.kibana_type == "metric":
        request_reducer = _normalize_request_reducer(wq.aggregator) if wq.aggregator else ""
        _REDUCER_TO_LENS: dict[str, str] = {
            "last": "LAST",
            "avg": "AVG",
            "max": "MAX",
            "min": "MIN",
            "sum": "SUM",
        }
        if request_reducer in _REDUCER_TO_LENS:
            agg = _REDUCER_TO_LENS[request_reducer]

    group_fields = [field_map.map_tag(tag, context="metric") for tag in mq.group_by]
    filters = []
    for filt in mq.scope:
        if isinstance(filt, TagFilter):
            if filt.value != "*" or filt.negated:
                es_field = field_map.map_tag(filt.key, context="metric")
                filters.append({
                    "field": es_field,
                    "value": filt.value,
                    "negated": filt.negated,
                })

    lens_config = {
        "type": "lens",
        "data_view": field_map.metric_index,
        "metric_field": es_metric,
        "aggregation": agg,
        "group_by": group_fields,
        "filters": filters,
        "kibana_type": plan.kibana_type,
        "time_field": field_map.timestamp_field,
    }
    return lens_config
