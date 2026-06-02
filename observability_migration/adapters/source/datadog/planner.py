# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Backend selection planner: decides how each Datadog widget should be translated."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import (
    SUPPORTED_WIDGET_TYPES,
    UNSUPPORTED_DATA_SOURCES,
    FormulaFuncCall,
    LogTerm,
    NormalizedWidget,
    PanelPlan,
)
from .rules import LOG_PLANNERS, METRIC_PLANNERS, PLANNER_PRECHECKS

COMPLEXITY_FUNCTIONS = {
    "anomalies", "forecast", "outliers", "robust_trend",
    "trend_line", "piecewise_constant",
}

TRANSLATABLE_ESQL_FORMULA_FUNCS = {
    "per_second", "per_minute", "per_hour",
    "abs", "log2", "log10", "ceil", "floor", "round",
    "default_zero", "exclude_null",
    "count_not_null", "count_nonzero",
}

UNTRANSLATABLE_FORMULA_FUNCS = {
    "clamp_min", "clamp_max", "cutoff_min", "cutoff_max",
    "timeshift", "calendar_shift",
    "diff", "derivative", "monotonic_diff",
    "cumsum", "integral",
    "top", "moving_rollup",
    "ewma_3", "ewma_5", "ewma_10", "ewma_20",
    "median_3", "median_5", "median_7", "median_9",
    "autosmooth",
}

TEXT_WIDGET_TYPES = {"note", "free_text", "image", "iframe"}

# Widget types that represent a Datadog-side status/check view rather than
# a queryable metric. These have no direct Kibana equivalent (Elastic uses
# Synthetics for synthetic checks and Alerts for monitor status), so we
# emit them as informative markdown placeholders rather than blocking.
STATUS_PLACEHOLDER_WIDGET_TYPES = {"check_status", "manage_status"}
GROUP_WIDGET_TYPES = {"group", "powerpack"}


@dataclass
class PlanContext:
    widget: NormalizedWidget
    plan: PanelPlan
    metric_queries: list[Any] = field(default_factory=list)
    has_multi_query_formula: bool = False
    use_lens: bool = False
    log_backend: str = ""

    @property
    def trace(self) -> list[dict[str, str]]:
        return self.plan.trace


def plan_widget(widget: NormalizedWidget) -> PanelPlan:
    """Decide the migration backend for a single widget."""
    plan = PanelPlan(
        widget_id=widget.id,
        data_source=widget.primary_data_source,
        kibana_type=widget.kibana_type,
    )
    context = PlanContext(widget=widget, plan=plan)

    PLANNER_PRECHECKS.apply(context, stop_when=_plan_is_complete)
    if context.plan.backend:
        return context.plan

    if widget.has_metric_queries:
        context.metric_queries = [q for q in widget.queries if q.metric_query]
        context.has_multi_query_formula = _has_multi_query_formula(widget, context.metric_queries)
        context.use_lens = _should_use_lens(widget, context.metric_queries, context.has_multi_query_formula)
        METRIC_PLANNERS.apply(context, stop_when=_plan_is_complete)
        return context.plan

    if widget.has_log_queries:
        context.log_backend = _choose_log_backend(widget)
        LOG_PLANNERS.apply(context, stop_when=_plan_is_complete)
        return context.plan

    return context.plan


def _plan_is_complete(context: PlanContext, _detail: str | None) -> bool:
    return bool(context.plan.backend)


@PLANNER_PRECHECKS.register(
    "datadog.plan.text_widget",
    priority=10,
    summary="Treat Datadog text-like widgets as markdown panels.",
)
def text_widget_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type not in TEXT_WIDGET_TYPES:
        return None
    context.plan.backend = "markdown"
    context.plan.reasons.append(f"text widget ({context.widget.widget_type})")
    return f"selected markdown for text widget {context.widget.widget_type}"


@PLANNER_PRECHECKS.register(
    "datadog.plan.group_widget",
    priority=20,
    summary="Keep Datadog group containers as structural group nodes.",
)
def group_widget_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type not in GROUP_WIDGET_TYPES:
        return None
    context.plan.backend = "group"
    context.plan.reasons.append("group/container widget")
    return "selected group backend"


@PLANNER_PRECHECKS.register(
    "datadog.plan.status_placeholder",
    priority=25,
    summary="Emit Datadog status/check widgets (check_status, manage_status) "
            "as informative markdown placeholders rather than blocking — "
            "Elastic uses Synthetics / Alerts instead.",
)
def status_placeholder_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type not in STATUS_PLACEHOLDER_WIDGET_TYPES:
        return None
    context.plan.backend = "markdown"
    context.plan.kibana_type = "markdown"
    # Confidence stays at 0 — this is not a real metric translation, it's
    # an informative placeholder that needs human follow-up.
    context.plan.confidence = 0.0
    context.plan.reasons.append(
        f"status widget ({context.widget.widget_type}) — placeholder for "
        f"manual setup as an Elastic Synthetics check or Alert rule"
    )
    return f"selected markdown placeholder for status widget {context.widget.widget_type}"


@PLANNER_PRECHECKS.register(
    "datadog.plan.unsupported_widget",
    priority=30,
    summary="Block unsupported Datadog widget types.",
)
def unsupported_widget_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type in SUPPORTED_WIDGET_TYPES:
        return None
    context.plan.backend = "blocked"
    context.plan.reasons.append(f"unsupported widget type: {context.widget.widget_type}")
    context.plan.confidence = 0.0
    return f"blocked unsupported widget type {context.widget.widget_type}"


@PLANNER_PRECHECKS.register(
    "datadog.plan.unsupported_data_source",
    priority=40,
    summary="Downgrade unsupported Datadog data sources to markdown placeholders.",
)
def unsupported_data_source_rule(context: PlanContext) -> str | None:
    if not context.widget.has_unsupported_data_source:
        return None
    ds = next(
        (q.data_source for q in context.widget.queries if q.data_source in UNSUPPORTED_DATA_SOURCES),
        context.widget.primary_data_source,
    )
    context.plan.backend = "markdown"
    context.plan.reasons.append(f"unsupported data source: {ds}")
    context.plan.warnings.append(
        f"Data source '{ds}' has no direct Kibana equivalent; panel will be a placeholder"
    )
    context.plan.confidence = 0.0
    return f"selected markdown for unsupported data source {ds}"


@PLANNER_PRECHECKS.register(
    "datadog.plan.no_queries",
    priority=50,
    summary="Fallback widgets without queries to markdown.",
)
def no_queries_rule(context: PlanContext) -> str | None:
    if context.widget.queries:
        return None
    context.plan.backend = "markdown"
    context.plan.reasons.append("no queries found in widget")
    context.plan.confidence = 0.0
    return "selected markdown because widget has no queries"


@PLANNER_PRECHECKS.register(
    "datadog.plan.unparsed_query",
    priority=60,
    summary="Downgrade unparsed metric queries to manual review.",
)
def unparsed_query_rule(context: PlanContext) -> str | None:
    has_unparsed = any(
        q.query_type in ("metric_unparsed", "legacy_unparsed")
        for q in context.widget.queries
    )
    if not has_unparsed:
        return None
    context.plan.backend = "markdown"
    context.plan.reasons.append("metric query could not be parsed")
    context.plan.warnings.append("query syntax not recognized; manual review needed")
    context.plan.confidence = 0.2
    return "selected markdown because a metric query could not be parsed"


@PLANNER_PRECHECKS.register(
    "datadog.plan.mixed_sources",
    priority=70,
    summary="Require manual redesign for widgets that mix metrics and logs.",
)
def mixed_sources_rule(context: PlanContext) -> str | None:
    if not (context.widget.has_log_queries and context.widget.has_metric_queries):
        return None
    context.plan.backend = "markdown"
    context.plan.reasons.append("mixed metric and log queries in one widget")
    context.plan.warnings.append(
        "mixed metric/log widgets are not translated safely yet; manual redesign needed"
    )
    context.plan.confidence = 0.0
    return "selected markdown because widget mixes metrics and logs"


@PLANNER_PRECHECKS.register(
    "datadog.plan.formula_complexity",
    priority=80,
    summary="Lower confidence when formulas use complex or unsupported functions.",
)
def formula_complexity_rule(context: PlanContext) -> str | None:
    complexity_issues = _check_formula_complexity(context.widget)
    if not complexity_issues:
        return None
    context.plan.warnings.extend(complexity_issues)
    context.plan.confidence *= 0.6
    return f"recorded {len(complexity_issues)} formula complexity warnings"


@PLANNER_PRECHECKS.register(
    "datadog.plan.unhandled_source",
    priority=90,
    summary="Fallback unsupported source mixes to markdown.",
)
def unhandled_source_rule(context: PlanContext) -> str | None:
    if context.widget.has_metric_queries or context.widget.has_log_queries:
        return None
    context.plan.backend = "markdown"
    context.plan.reasons.append(f"unhandled data source: {context.widget.primary_data_source}")
    context.plan.confidence = 0.0
    return f"selected markdown for unhandled data source {context.widget.primary_data_source}"


@METRIC_PLANNERS.register(
    "datadog.plan.metric_nested_query",
    priority=10,
    summary="Warn on nested metric queries before choosing a backend.",
)
def metric_nested_query_rule(context: PlanContext) -> str | None:
    has_nested = any(
        q.metric_query and _is_nested_query(q.metric_query)
        for q in context.metric_queries
    )
    if not has_nested:
        return None
    context.plan.warnings.append(
        "nested query detected — multi-layer aggregation may be approximated"
    )
    context.plan.field_issues.append("nested_aggregation")
    context.plan.confidence *= 0.5
    return "recorded nested metric query warning"


@METRIC_PLANNERS.register(
    "datadog.plan.metric_query_value",
    priority=20,
    summary="Plan query-value metrics as Lens or ES|QL metric panels.",
)
def metric_query_value_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type != "query_value":
        return None
    context.plan.backend = _metric_widget_backend(context)
    context.plan.kibana_type = "metric"
    context.plan.reasons.append(f"single-value metric → {context.plan.backend} metric panel")
    return f"selected {context.plan.backend} metric panel"


@METRIC_PLANNERS.register(
    "datadog.plan.metric_toplist",
    priority=30,
    summary="Plan Datadog toplists and bar charts as Lens or ES|QL tables.",
)
def metric_toplist_rule(context: PlanContext) -> str | None:
    # Datadog bar_chart is a grouped scalar aggregation (group_by + compute
    # + sort + limit) — the same data shape as a toplist, just drawn as bars.
    # Route it through the toplist grouped-aggregation path so it produces a
    # faithful ranked table instead of an "unsupported widget" placeholder.
    if context.widget.widget_type not in ("toplist", "bar_chart"):
        return None
    context.plan.backend = _metric_widget_backend(context)
    context.plan.kibana_type = "table"
    label = "bar chart" if context.widget.widget_type == "bar_chart" else "top list"
    context.plan.reasons.append(f"{label} → {context.plan.backend} table with ORDER BY + LIMIT")
    return f"selected {context.plan.backend} {context.widget.widget_type} table"


@METRIC_PLANNERS.register(
    "datadog.plan.metric_table",
    priority=40,
    summary="Plan Datadog tables as Lens or ES|QL tables.",
)
def metric_table_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type not in ("table", "query_table"):
        return None
    context.plan.backend = _metric_widget_backend(context)
    context.plan.kibana_type = "table"
    context.plan.reasons.append(f"table → {context.plan.backend} table")
    return f"selected {context.plan.backend} table"


@METRIC_PLANNERS.register(
    "datadog.plan.metric_timeseries",
    priority=50,
    summary="Plan Datadog timeseries metrics as Lens or ES|QL XY charts.",
)
def metric_timeseries_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type != "timeseries":
        return None
    if context.has_multi_query_formula:
        context.plan.backend = "esql"
        context.plan.kibana_type = "xy"
        context.plan.reasons.append("multi-query formula → ES|QL for query-side computation")
        return "selected ES|QL XY because widget uses a multi-query formula"
    context.plan.backend = "lens" if context.use_lens else "esql"
    context.plan.kibana_type = "xy"
    context.plan.reasons.append(f"timeseries → {context.plan.backend} XY panel")
    return f"selected {context.plan.backend} XY panel"


@METRIC_PLANNERS.register(
    "datadog.plan.metric_heatmap_distribution",
    priority=60,
    summary="Force heatmaps and distributions through ES|QL.",
)
def metric_heatmap_distribution_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type not in ("heatmap", "distribution"):
        return None
    context.plan.backend = "esql"
    context.plan.kibana_type = context.widget.kibana_type
    context.plan.reasons.append(f"{context.widget.widget_type} → ES|QL")
    return f"selected ES|QL for {context.widget.widget_type}"


@METRIC_PLANNERS.register(
    "datadog.plan.metric_change",
    priority=70,
    summary="Approximate Datadog change widgets as ES|QL metrics.",
)
def metric_change_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type != "change":
        return None
    has_grouped_change = any(
        q.metric_query and q.metric_query.group_by
        for q in context.metric_queries
    )
    context.plan.backend = "esql"
    context.plan.kibana_type = "table" if has_grouped_change else "metric"
    context.plan.reasons.append(
        "change widget → ES|QL table (comparison shift)"
        if has_grouped_change
        else "change widget → ES|QL metric (comparison shift)"
    )
    context.plan.warnings.append("change calculation is approximated")
    context.plan.confidence *= 0.7
    return (
        "selected ES|QL table for grouped change widget"
        if has_grouped_change
        else "selected ES|QL metric for change widget"
    )


@METRIC_PLANNERS.register(
    "datadog.plan.metric_partition",
    priority=80,
    summary="Plan pie, treemap, and sunburst widgets as ES|QL partition charts.",
)
def metric_partition_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type not in ("pie", "treemap", "sunburst"):
        return None
    context.plan.backend = "esql"
    context.plan.kibana_type = context.widget.kibana_type
    context.plan.reasons.append(f"{context.widget.widget_type} → ES|QL partition chart")
    return f"selected ES|QL partition chart for {context.widget.widget_type}"


@METRIC_PLANNERS.register(
    "datadog.plan.metric_scatterplot",
    priority=90,
    summary="Plan scatterplots as ES|QL XY charts with a warning.",
)
def metric_scatterplot_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type != "scatterplot":
        return None
    context.plan.backend = "esql"
    context.plan.kibana_type = "xy"
    context.plan.reasons.append("scatterplot → ES|QL XY scatter")
    context.plan.warnings.append("scatter mode requires manual axis mapping verification")
    context.plan.confidence *= 0.8
    return "selected ES|QL scatter plot"


@METRIC_PLANNERS.register(
    "datadog.plan.metric_geomap",
    priority=100,
    summary="Downgrade geomaps to markdown because Kibana Maps output is not implemented.",
)
def metric_geomap_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type != "geomap":
        return None
    context.plan.backend = "markdown"
    context.plan.kibana_type = "markdown"
    context.plan.reasons.append("geomap requires Kibana Maps — not yet supported")
    context.plan.warnings.append("geomap migration needs dedicated Maps saved object support")
    context.plan.confidence = 0.0
    return "selected markdown for geomap"


@METRIC_PLANNERS.register(
    "datadog.plan.metric_default",
    priority=110,
    summary="Default remaining metrics widgets to Lens or ES|QL.",
)
def metric_default_rule(context: PlanContext) -> str | None:
    context.plan.backend = "lens" if context.use_lens else "esql"
    context.plan.reasons.append(f"default {context.plan.backend} path for {context.widget.widget_type}")
    return f"selected default {context.plan.backend} backend"


@LOG_PLANNERS.register(
    "datadog.plan.log_stream",
    priority=10,
    summary="Plan log stream widgets as tables.",
)
def log_stream_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type not in ("log_stream", "list_stream"):
        return None
    context.plan.backend = context.log_backend
    context.plan.kibana_type = "table"
    context.plan.reasons.append(f"log stream → {context.log_backend} table")
    return f"selected {context.log_backend} log stream table"


@LOG_PLANNERS.register(
    "datadog.plan.log_table",
    priority=20,
    summary="Plan Datadog log tables as Kibana tables.",
)
def log_table_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type not in ("table", "query_table"):
        return None
    context.plan.backend = context.log_backend
    context.plan.kibana_type = "table"
    context.plan.reasons.append(f"log table → {context.log_backend} table")
    return f"selected {context.log_backend} log table"


@LOG_PLANNERS.register(
    "datadog.plan.log_timeseries",
    priority=30,
    summary="Plan log timeseries widgets as count-by-bucket XY charts.",
)
def log_timeseries_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type != "timeseries":
        return None
    context.plan.backend = context.log_backend
    context.plan.kibana_type = "xy"
    context.plan.reasons.append(f"log timeseries (count by bucket) → {context.log_backend} XY")
    return f"selected {context.log_backend} log timeseries"


@LOG_PLANNERS.register(
    "datadog.plan.log_toplist",
    priority=40,
    summary="Plan log toplists as ordered tables.",
)
def log_toplist_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type != "toplist":
        return None
    context.plan.backend = context.log_backend
    context.plan.kibana_type = "table"
    context.plan.reasons.append(f"log top list → {context.log_backend} table with ORDER BY + LIMIT")
    return f"selected {context.log_backend} log toplist"


@LOG_PLANNERS.register(
    "datadog.plan.log_query_value",
    priority=50,
    summary="Plan log count widgets as metric panels.",
)
def log_query_value_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type != "query_value":
        return None
    context.plan.backend = context.log_backend
    context.plan.kibana_type = "metric"
    context.plan.reasons.append(f"log count → {context.log_backend} metric")
    return f"selected {context.log_backend} log metric"


@LOG_PLANNERS.register(
    "datadog.plan.log_geomap",
    priority=55,
    summary="Downgrade log-based geomaps to markdown (Kibana Maps not yet supported).",
)
def log_geomap_rule(context: PlanContext) -> str | None:
    if context.widget.widget_type != "geomap":
        return None
    context.plan.backend = "markdown"
    context.plan.kibana_type = "markdown"
    context.plan.reasons.append("geomap requires Kibana Maps — not yet supported")
    context.plan.warnings.append("geomap migration needs dedicated Maps saved object support")
    context.plan.confidence = 0.0
    return "selected markdown for log geomap"


@LOG_PLANNERS.register(
    "datadog.plan.log_default",
    priority=60,
    summary="Default remaining log widgets to the chosen log backend.",
)
def log_default_rule(context: PlanContext) -> str | None:
    context.plan.backend = context.log_backend
    context.plan.reasons.append(f"default {context.log_backend} path for log {context.widget.widget_type}")
    return f"selected default {context.log_backend} log backend"


def _should_use_lens(
    widget: NormalizedWidget,
    metric_queries: list[Any],
    has_multi_query_formula: bool,
) -> bool:
    """Prefer Lens for simple single-query metrics without formulas or rate logic."""
    if has_multi_query_formula:
        return False
    if widget.formulas:
        return False
    if len(metric_queries) != 1:
        return False
    mq = metric_queries[0].metric_query
    if not mq:
        return False
    if mq.as_rate or mq.as_count:
        return False
    if any(fn.name in ("per_second", "per_minute", "per_hour", "derivative", "top") for fn in mq.functions):
        return False
    if widget.widget_type in ("heatmap", "scatterplot", "distribution"):
        return False
    return True


def _metric_widget_backend(context: PlanContext) -> str:
    if _prefer_esql_for_simple_metric_widget(
        context.widget,
        context.metric_queries,
        context.has_multi_query_formula,
    ):
        return "esql"
    return "lens" if context.use_lens else "esql"


def _prefer_esql_for_simple_metric_widget(
    widget: NormalizedWidget,
    metric_queries: list[Any],
    has_multi_query_formula: bool,
) -> bool:
    if widget.widget_type not in {"query_value", "toplist", "table", "query_table"}:
        return False
    if has_multi_query_formula or widget.formulas:
        return False
    if len(metric_queries) != 1:
        return False
    mq = metric_queries[0].metric_query
    if not mq:
        return False
    if mq.as_rate or mq.as_count:
        return False
    if any(fn.name in ("per_second", "per_minute", "per_hour", "derivative") for fn in mq.functions):
        return False
    if _is_nested_query(mq):
        return False
    if widget.widget_type == "query_value" and mq.group_by:
        return False
    return True


def _choose_log_backend(widget: NormalizedWidget) -> str:
    """Choose a log backend based on whether free-text search is present."""
    for q in widget.queries:
        if q.log_query and q.log_query.ast and _ast_has_free_text(q.log_query.ast):
            return "esql_with_kql"
    return "esql"


def _ast_has_free_text(node: Any) -> bool:
    """Return True if the AST contains free-text LogTerm nodes."""
    if isinstance(node, LogTerm):
        return True
    if hasattr(node, "children"):
        return any(_ast_has_free_text(child) for child in node.children)
    if hasattr(node, "child"):
        return _ast_has_free_text(node.child)
    return False


def _check_formula_complexity(widget: NormalizedWidget) -> list[str]:
    """Check if any formula uses functions we can't translate."""
    issues: list[str] = []
    for formula in widget.formulas:
        if not formula.expression or not formula.expression.ast:
            continue
        _walk_formula_funcs(formula.expression.ast, issues)
    return issues


def _walk_formula_funcs(node: Any, issues: list[str]) -> None:
    if isinstance(node, FormulaFuncCall):
        if node.name.lower() in COMPLEXITY_FUNCTIONS:
            issues.append(
                f"formula function '{node.name}' has no ES|QL equivalent — panel may need manual redesign"
            )
        for arg in node.args:
            _walk_formula_funcs(arg, issues)
    elif hasattr(node, "left"):
        _walk_formula_funcs(node.left, issues)
        _walk_formula_funcs(node.right, issues)
    elif hasattr(node, "operand"):
        _walk_formula_funcs(node.operand, issues)


def _has_multi_query_formula(widget: NormalizedWidget, metric_queries: list[Any]) -> bool:
    return (
        len(metric_queries) > 1
        and bool(widget.formulas)
        and any(
            len(formula.expression.referenced_queries if formula.expression else []) > 1
            for formula in widget.formulas
        )
    )


def _is_nested_query(mq: Any) -> bool:
    """Heuristic: more than one rollup or aggregation layer indicates nesting."""
    rollup_count = sum(1 for fn in mq.functions if fn.name == "rollup")
    return rollup_count > 1
