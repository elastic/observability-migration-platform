# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Core data models for the Datadog → Kibana migration pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from observability_migration.core.assets.status import AssetStatus

# ---------------------------------------------------------------------------
# Widget type mapping
# ---------------------------------------------------------------------------

WIDGET_TYPE_MAP: dict[str, str] = {
    "timeseries": "xy",
    "query_value": "metric",
    "toplist": "table",
    "bar_chart": "table",
    "table": "table",
    "heatmap": "heatmap",
    "distribution": "xy",
    "change": "metric",
    "scatterplot": "xy",
    "treemap": "treemap",
    "sunburst": "partition",
    "pie": "partition",
    "geomap": "map",
    "slo": "markdown",
    "manage_status": "markdown",
    "alert_graph": "markdown",
    "alert_value": "markdown",
    "check_status": "markdown",
    "hostmap": "markdown",
    "free_text": "markdown",
    "note": "markdown",
    "image": "markdown",
    "iframe": "markdown",
    "group": "group",
    "log_stream": "table",
    "list_stream": "table",
    "query_table": "table",
    "trace_service": "markdown",
    "servicemap": "markdown",
    "topology_map": "markdown",
    "powerpack": "group",
    "run_workflow": "markdown",
    "funnel": "markdown",
    "event_stream": "markdown",
    "event_timeline": "markdown",
    "process": "markdown",
}

SUPPORTED_WIDGET_TYPES: set[str] = {
    "timeseries", "query_value", "toplist", "bar_chart", "table", "query_table",
    "heatmap", "distribution", "change", "scatterplot", "treemap",
    "sunburst", "pie", "geomap", "log_stream", "list_stream",
    "note", "free_text", "image", "iframe", "group",
}

METRIC_DATA_SOURCES: set[str] = {"metrics", "cloud_cost"}
LOG_DATA_SOURCES: set[str] = {"logs", "logs_stream", "rum_logs", "ci_pipelines"}
UNSUPPORTED_DATA_SOURCES: set[str] = {
    "apm", "rum", "processes", "network", "profiles",
    "security_signals", "ci_tests", "audit_trail",
    "events", "event_stream",
}

DISPLAY_TYPE_MAP: dict[str, str] = {
    "line": "line",
    "area": "area",
    "bars": "bar_stacked",
}


# ---------------------------------------------------------------------------
# Parsed metric query components
# ---------------------------------------------------------------------------

@dataclass
class TagFilter:
    key: str
    value: str
    negated: bool = False
    # True when the filter came from a Datadog ``key IN (a, b, c)`` /
    # ``key NOT IN (...)`` list. ``value`` holds the pipe-joined members so it
    # renders to a native ES|QL ``IN (...)`` instead of a chain of ``==``/OR.
    is_in_list: bool = False

    def __str__(self) -> str:
        if self.is_in_list:
            op = "NOT IN" if self.negated else "IN"
            members = ", ".join(v for v in self.value.split("|") if v)
            return f"{self.key} {op} ({members})"
        neg = "!" if self.negated else ""
        return f"{neg}{self.key}:{self.value}"


@dataclass
class ScopeBoolOp:
    op: str
    children: list[Any] = field(default_factory=list)


@dataclass
class FunctionCall:
    name: str
    args: list[Any] = field(default_factory=list)

    def __str__(self) -> str:
        args_str = ", ".join(str(a) for a in self.args)
        return f".{self.name}({args_str})"


@dataclass
class MetricQuery:
    raw: str = ""
    space_agg: str = ""
    metric: str = ""
    scope: list[Any] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    functions: list[FunctionCall] = field(default_factory=list)
    as_rate: bool = False
    as_count: bool = False

    @property
    def rollup(self) -> FunctionCall | None:
        for fn in self.functions:
            if fn.name == "rollup":
                return fn
        return None

    @property
    def fill_value(self) -> str | None:
        for fn in self.functions:
            if fn.name == "fill":
                return str(fn.args[0]) if fn.args else None
        return None

    @property
    def scope_tags(self) -> dict[str, str]:
        return {
            f.key: f.value
            for f in self.scope
            if isinstance(f, TagFilter) and not f.negated
        }

    @property
    def negated_tags(self) -> dict[str, str]:
        return {
            f.key: f.value
            for f in self.scope
            if isinstance(f, TagFilter) and f.negated
        }


# ---------------------------------------------------------------------------
# Formula / expression AST
# ---------------------------------------------------------------------------

@dataclass
class FormulaRef:
    """Reference to a named query inside a formula expression."""
    name: str


@dataclass
class FormulaNumber:
    value: float


@dataclass
class FormulaString:
    """String literal inside a formula expression (e.g. top(q, 10, 'mean', 'desc'))."""
    value: str


@dataclass
class FormulaBinOp:
    op: str
    left: Any
    right: Any


@dataclass
class FormulaFuncCall:
    name: str
    args: list[Any] = field(default_factory=list)


@dataclass
class FormulaUnary:
    op: str
    operand: Any


@dataclass
class FormulaExpression:
    raw: str = ""
    ast: Any = None
    alias: str = ""

    @property
    def referenced_queries(self) -> list[str]:
        refs: list[str] = []
        _collect_refs(self.ast, refs)
        return refs


def _collect_refs(node: Any, out: list[str]) -> None:
    if isinstance(node, FormulaRef):
        if node.name not in out:
            out.append(node.name)
    elif isinstance(node, FormulaBinOp):
        _collect_refs(node.left, out)
        _collect_refs(node.right, out)
    elif isinstance(node, FormulaFuncCall):
        for arg in node.args:
            _collect_refs(arg, out)
    elif isinstance(node, FormulaUnary):
        _collect_refs(node.operand, out)


# ---------------------------------------------------------------------------
# Log search AST
# ---------------------------------------------------------------------------

@dataclass
class LogTerm:
    value: str
    quoted: bool = False


@dataclass
class LogAttributeFilter:
    attribute: str
    value: str
    negated: bool = False
    is_tag: bool = False


@dataclass
class LogRange:
    attribute: str
    low: str
    high: str
    low_inclusive: bool = True
    high_inclusive: bool = True


@dataclass
class LogBoolOp:
    op: str
    children: list[Any] = field(default_factory=list)


@dataclass
class LogNot:
    child: Any = None


@dataclass
class LogWildcard:
    attribute: str
    pattern: str


@dataclass
class LogQuery:
    raw: str = ""
    ast: Any = None

    @property
    def is_empty(self) -> bool:
        return self.ast is None and self.raw.strip() in ("", "*")


# ---------------------------------------------------------------------------
# Normalized widget / dashboard models
# ---------------------------------------------------------------------------

@dataclass
class WidgetQuery:
    name: str = ""
    data_source: str = ""
    raw_query: str = ""
    metric_query: MetricQuery | None = None
    log_query: LogQuery | None = None
    aggregator: str = ""
    query_type: str = ""


@dataclass
class WidgetFormula:
    raw: str = ""
    alias: str = ""
    expression: FormulaExpression | None = None
    limit: dict[str, Any] | None = None


@dataclass
class ConditionalFormat:
    comparator: str = ""
    value: float = 0.0
    palette: str = ""


@dataclass
class TemplateVariable:
    name: str = ""
    tag: str = ""
    default: str = "*"
    prefix: str = ""
    defaults: list[str] = field(default_factory=list)
    available_values: list[str] = field(default_factory=list)


@dataclass
class NormalizedWidget:
    id: str = ""
    widget_type: str = ""
    title: str = ""
    queries: list[WidgetQuery] = field(default_factory=list)
    formulas: list[WidgetFormula] = field(default_factory=list)
    display_type: str = ""
    yaxis: dict[str, Any] = field(default_factory=dict)
    legend: dict[str, Any] = field(default_factory=dict)
    layout: dict[str, Any] = field(default_factory=dict)
    response_format: str = ""
    style: dict[str, Any] = field(default_factory=dict)
    conditional_formats: list[ConditionalFormat] = field(default_factory=list)
    custom_unit: str = ""
    precision: int | None = None
    text_align: str = ""
    autoscale: bool = True
    time: dict[str, Any] = field(default_factory=dict)
    raw_definition: dict[str, Any] = field(default_factory=dict)
    children: list[NormalizedWidget] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    markers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def kibana_type(self) -> str:
        return WIDGET_TYPE_MAP.get(self.widget_type, "markdown")

    @property
    def is_supported(self) -> bool:
        return self.widget_type in SUPPORTED_WIDGET_TYPES

    @property
    def has_metric_queries(self) -> bool:
        return any(q.data_source in METRIC_DATA_SOURCES for q in self.queries)

    @property
    def has_log_queries(self) -> bool:
        return any(q.data_source in LOG_DATA_SOURCES for q in self.queries)

    @property
    def has_unsupported_data_source(self) -> bool:
        return any(q.data_source in UNSUPPORTED_DATA_SOURCES for q in self.queries)

    @property
    def primary_data_source(self) -> str:
        if self.queries:
            return self.queries[0].data_source
        return ""


@dataclass
class NormalizedDashboard:
    id: str = ""
    title: str = ""
    description: str = ""
    layout_type: str = ""
    widgets: list[NormalizedWidget] = field(default_factory=list)
    template_variables: list[TemplateVariable] = field(default_factory=list)
    source_file: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    url: str = ""


# ---------------------------------------------------------------------------
# Planning / translation result models
# ---------------------------------------------------------------------------

@dataclass
class PanelPlan:
    widget_id: str = ""
    backend: str = ""
    kibana_type: str = ""
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    field_issues: list[str] = field(default_factory=list)
    confidence: float = 1.0
    data_source: str = ""
    trace: list[dict[str, str]] = field(default_factory=list)


@dataclass
class TranslationResult:
    widget_id: str = ""
    title: str = ""
    dd_widget_type: str = ""
    kibana_type: str = ""
    status: str = "ok"
    backend: str = ""
    esql_query: str = ""
    yaml_panel: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    semantic_losses: list[str] = field(default_factory=list)
    source_queries: list[str] = field(default_factory=list)
    confidence: float = 1.0
    trace: list[dict[str, str]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    query_language: str = ""
    source_panel_id: str = ""
    query_ir: dict[str, Any] = field(default_factory=dict)
    runtime_rollups: list[str] = field(default_factory=list)
    target_candidates: list[dict[str, Any]] = field(default_factory=list)
    recommended_target: str = ""
    verification_packet: dict[str, Any] = field(default_factory=dict)
    review_explanation: dict[str, Any] = field(default_factory=dict)
    operational_ir: Any = None
    post_validation_action: str = ""
    post_validation_message: str = ""

    @property
    def asset_status(self) -> AssetStatus:
        return AssetStatus.from_datadog(self.status)


@dataclass
class DashboardResult:
    dashboard_id: str = ""
    dashboard_title: str = ""
    source_file: str = ""
    total_widgets: int = 0
    migrated: int = 0
    migrated_with_warnings: int = 0
    requires_manual: int = 0
    not_feasible: int = 0
    skipped: int = 0
    blocked: int = 0
    panel_results: list[TranslationResult] = field(default_factory=list)
    preflight_passed: bool = True
    preflight_issues: list[dict[str, str]] = field(default_factory=list)
    validation_summary: dict[str, int] = field(default_factory=dict)
    yaml_path: str = ""
    compiled_path: str = ""
    compiled: bool = False
    compile_error: str = ""
    layout_checked: bool = False
    layout_error: str = ""
    upload_attempted: bool = False
    uploaded: bool | None = None
    upload_error: str = ""
    uploaded_space: str = ""
    uploaded_kibana_url: str = ""
    kibana_saved_object_id: str = ""
    smoke_attempted: bool = False
    smoke_status: str = "not_run"
    smoke_error: str = ""
    smoke_report_path: str = ""
    browser_audit_attempted: bool = False
    browser_audit_status: str = "not_run"
    browser_audit_error: str = ""
    verification_summary: dict[str, int] = field(default_factory=dict)
    alert_results: list = field(default_factory=list)
    alert_summary: dict = field(default_factory=dict)

    def recompute_counts(self) -> None:
        self.migrated = 0
        self.migrated_with_warnings = 0
        self.requires_manual = 0
        self.not_feasible = 0
        self.skipped = 0
        self.blocked = 0
        for pr in self.panel_results:
            if pr.status == "ok":
                if pr.warnings:
                    self.migrated_with_warnings += 1
                else:
                    self.migrated += 1
            elif pr.status == "warning":
                self.migrated_with_warnings += 1
            elif pr.status == "requires_manual":
                self.requires_manual += 1
            elif pr.status == "not_feasible":
                self.not_feasible += 1
            elif pr.status == "skipped":
                self.skipped += 1
            elif pr.status == "blocked":
                self.blocked += 1

    def build_runtime_summary(self) -> dict[str, Any]:
        """Return a runtime summary in the shared format."""
        layout_status = {"status": "not_run", "error": ""}
        if self.layout_checked or self.layout_error:
            layout_status = {
                "status": "pass" if not self.layout_error else "fail",
                "error": self.layout_error or "",
            }
        upload_status = {"status": "not_run", "error": ""}
        if self.upload_attempted or self.upload_error:
            upload_status = {
                "status": "pass" if self.uploaded and not self.upload_error else "fail",
                "error": self.upload_error or "",
            }
        smoke_status = {"status": "not_run", "error": ""}
        if self.smoke_attempted or self.smoke_error:
            smoke_status = {
                "status": self.smoke_status or "not_run",
                "error": self.smoke_error or "",
            }
        browser_status = {"status": "not_run", "error": ""}
        if self.browser_audit_attempted or self.browser_audit_error:
            browser_status = {
                "status": self.browser_audit_status or "not_run",
                "error": self.browser_audit_error or "",
            }
        return {
            "yaml_lint": {"status": "not_run", "error": ""},
            "compile": {
                "status": "pass" if self.compiled else "fail" if self.compile_error else "not_run",
                "error": self.compile_error or "",
            },
            "layout": layout_status,
            "upload": upload_status,
            "smoke": smoke_status,
            "browser": browser_status,
        }

    @property
    def unified_status_counts(self) -> dict[str, int]:
        """Return panel counts using shared AssetStatus vocabulary."""
        counts: dict[str, int] = {s.value: 0 for s in AssetStatus}
        for pr in self.panel_results:
            counts[pr.asset_status.value] = counts.get(pr.asset_status.value, 0) + 1
        return counts
