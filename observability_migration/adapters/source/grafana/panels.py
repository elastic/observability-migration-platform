# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Panel, variable, and dashboard translation helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from observability_migration.core.assets.operational import build_operational_ir
from observability_migration.core.assets.query import QueryIR, build_query_ir, infer_output_shape
from observability_migration.core.assets.visual import refresh_visual_ir
from observability_migration.core.reporting.report import MigrationResult, PanelResult, _panel_query_index
from observability_migration.core.verification.field_capabilities import assess_field_usage
from observability_migration.targets.kibana.emit.display import clean_template_variables, enrich_yaml_panel_display
from observability_migration.targets.kibana.emit.esql_utils import (
    ESQLShape as _ESQLShapeCanonical,
)
from observability_migration.targets.kibana.emit.esql_utils import (
    extract_esql_columns as _extract_esql_columns_canonical,
)
from observability_migration.targets.kibana.emit.esql_utils import (
    extract_esql_shape as _extract_esql_shape_canonical,
)
from observability_migration.targets.kibana.emit.esql_utils import (
    is_time_bucket_expression as _is_time_bucket_expression_canonical,
)
from observability_migration.targets.kibana.emit.esql_utils import (
    is_time_like_output_field as _is_time_like_output_field_canonical,
)
from observability_migration.targets.kibana.emit.esql_utils import (
    select_xy_dimension_fields as _select_xy_dimension_fields_canonical,
)
from observability_migration.targets.kibana.emit.esql_utils import (
    split_esql_pipeline as _split_esql_pipeline_canonical,
)
from observability_migration.targets.kibana.emit.esql_utils import (
    split_top_level_assignment as _split_top_level_assignment_canonical,
)
from observability_migration.targets.kibana.emit.esql_utils import (
    split_top_level_keyword as _split_top_level_keyword_canonical,
)
from observability_migration.targets.kibana.emit.layout import apply_style_guide_layout

from .extract import _normalize_text_panel_content
from .manifest import (
    analyze_panel_targets,
    build_dashboard_inventory,
    classify_panel_readiness,
    collect_panel_inventory,
    collect_panel_notes,
    infer_query_language,
    normalize_datasource,
    recommend_panel_target,
    target_query_text,
)
from .promql import (
    _build_formula_plan,
    _build_shared_measure_pipeline,
    _collapse_summary_ts_query,
    _format_scalar_value,
    _matcher_to_esql,
    _parse_fragment,
    _split_top_level_csv,
    _summary_mode_from_metadata,
    _unique_safe_alias,
)
from .rules import PANEL_TRANSLATORS, VARIABLE_TRANSLATORS, RulePackConfig, _append_unique
from .schema import SchemaResolver
from .translate import TranslationContext, _build_metric_contract_artifacts, translate_promql_to_esql

PANEL_TYPE_MAP = {
    "timeseries": "line",
    "graph": "line",
    "stat": "metric",
    "singlestat": "metric",
    "gauge": "gauge",
    "bargauge": "bar",
    "table": "datatable",
    "table-old": "datatable",
    "text": "markdown",
    "logs": "datatable",
    "heatmap": "heatmap",
    "piechart": "pie",
    "barchart": "bar",
}

SKIP_PANEL_TYPES = {"row", "news", "dashlist", "alertlist", "nodeGraph", "canvas"}

GRAFANA_GRID_COLS = 24
KIBANA_GRID_COLS = 48
GRAFANA_ROW_HEIGHT_PX = 30
KIBANA_ROW_HEIGHT_PX = 20
MINIMUM_KIBANA_VERSION = "9.1.0"
MIN_PANEL_WIDTH = 4

KIBANA_TYPE_HEIGHT = {
    "metric": 5,
    "gauge": 6,
    "bargauge": 5,
    "line": 12,
    "area": 12,
    "bar": 12,
    "datatable": 15,
    "pie": 12,
    "treemap": 12,
    "heatmap": 12,
    "markdown": 6,
}
KIBANA_DEFAULT_HEIGHT = 8


@dataclass
class PanelContext:
    panel: dict
    panel_type: str
    title: str
    kibana_type: str
    yaml_panel: dict
    translation: TranslationContext
    extra_translations: list = field(default_factory=list)
    handled: bool = False
    trace: list = field(default_factory=list)


@dataclass
class VariableContext:
    variable: dict
    data_view: str
    resolver: object = None
    rule_pack: RulePackConfig | None = None
    query_text: str = ""
    source_field: str = ""
    repeat_variable_names: set[str] = field(default_factory=set)
    control: dict | None = None
    handled: bool = False
    trace: list = field(default_factory=list)


ESQLShape = _ESQLShapeCanonical


@dataclass
class NormalizedPanelGroup:
    title: str | None
    panels: list[dict]
    skipped_panel_results: list[PanelResult] = field(default_factory=list)


_PLACEHOLDER_SECTION_TITLES = frozenset({"title", "new row", "row"})


def _resolved_panel_type_map(rule_pack):
    panel_type_map = dict(PANEL_TYPE_MAP)
    panel_type_map.update(rule_pack.panel_type_overrides)
    return panel_type_map


def _infer_graph_chart_style(panel):
    """Refine the Kibana chart type for legacy Grafana ``graph`` panels.

    The legacy ``graph`` plugin uses boolean flags (``bars``, ``lines``,
    ``stack``) to control visual style.  When ``bars`` is *True* and ``lines``
    is *False*, the panel is visually a bar chart, not a line chart.
    Stacked graphs become ``area`` charts in Kibana.
    """
    if panel.get("bars") and not panel.get("lines"):
        return "bar"
    if panel.get("stack"):
        return "area"
    return "line"


def _infer_timeseries_chart_style(panel):
    """Refine the Kibana chart type for modern Grafana ``timeseries`` panels.

    Stacked timeseries (``fieldConfig.defaults.custom.stacking.mode`` set to
    ``normal`` or ``percent``) map to ``area`` charts in Kibana.  The default
    ``drawStyle`` of ``"bars"`` maps to ``bar``.
    """
    defaults = ((panel.get("fieldConfig") or {}).get("defaults") or {})
    custom = defaults.get("custom") or {}
    stacking = custom.get("stacking") or {}
    stacking_mode = stacking.get("mode", "none") if isinstance(stacking, dict) else "none"
    if stacking_mode in ("normal", "percent"):
        return "area"
    draw_style = str(custom.get("drawStyle", "")).lower()
    if draw_style == "bars":
        return "bar"
    return "line"


def _infer_xy_stacking_mode(panel):
    """Return the kb-dashboard ``mode`` value for bar/area XY charts.

    Returns ``"stacked"``, ``"unstacked"``, or ``"percentage"`` based on
    the Grafana panel's stacking configuration.  Returns ``None`` for line
    charts (where the field is not applicable).
    """
    defaults = ((panel.get("fieldConfig") or {}).get("defaults") or {})
    custom = defaults.get("custom") or {}
    stacking = custom.get("stacking") or {}
    stacking_mode = stacking.get("mode", "none") if isinstance(stacking, dict) else "none"
    if stacking_mode == "percent":
        return "percentage"
    if stacking_mode == "normal":
        return "stacked"
    if panel.get("stack") and panel.get("percentage"):
        return "percentage"
    if panel.get("stack"):
        return "stacked"
    return "unstacked"


def _panel_value_aliases(panel):
    aliases = {}
    for style in panel.get("styles", []):
        pattern = str(style.get("pattern") or "").strip()
        alias = str(style.get("alias") or "").strip()
        match = re.fullmatch(r"Value\s+#([A-Za-z0-9_]+)", pattern, re.IGNORECASE)
        if match and alias:
            aliases[match.group(1)] = alias
    return aliases


def _panel_hides_unmapped_values(panel):
    for style in panel.get("styles", []):
        pattern = str(style.get("pattern") or "").strip()
        if style.get("type") == "hidden" and pattern in {"/.*/", "/.+/"}:
            return True
    return False


def _panel_group_label_patterns(panel):
    labels = []
    for style in panel.get("styles", []):
        pattern = str(style.get("pattern") or "").strip()
        if not pattern or style.get("type") == "hidden":
            continue
        if re.fullmatch(r"Value\s+#([A-Za-z0-9_]+)", pattern, re.IGNORECASE):
            continue
        if pattern.startswith("/") and pattern.endswith("/"):
            continue
        if pattern.lower() in {"time", "__name__", "metric", "value"}:
            continue
        if pattern not in labels:
            labels.append(pattern)
    return labels


def _target_series_alias(panel, target):
    ref_id = str(target.get("refId") or "").strip()
    style_alias = _panel_value_aliases(panel).get(ref_id)
    if style_alias:
        return style_alias
    legend = str(target.get("legendFormat") or "").strip()
    if legend:
        return legend
    return ref_id or "series"


def _target_summary_mode(panel_type, target):
    instant_like = bool(target.get("instant")) or (
        "range" in target and target.get("range") is False
    )
    if panel_type in {"stat", "singlestat", "gauge", "bargauge", "piechart"}:
        return True
    if not instant_like:
        return False
    if panel_type in {"table", "table-old"}:
        return True
    return str(target.get("format") or "").lower() == "table"


def _target_translation_hints(panel, panel_type, target):
    summary_mode = _target_summary_mode(panel_type, target)
    hints = {
        "summary_mode": summary_mode,
        "series_alias": _target_series_alias(panel, target),
    }
    preferred_group_labels = []
    style_labels = []
    if panel_type in {"table", "table-old"}:
        style_labels = _panel_group_label_patterns(panel)
        preferred_group_labels.extend(style_labels)
    legend_labels = _extract_legend_labels(target.get("legendFormat", ""))
    legend_contributed = False
    if not summary_mode or panel_type == "bargauge":
        for lbl in legend_labels:
            if lbl not in preferred_group_labels:
                preferred_group_labels.append(lbl)
                legend_contributed = True
    if preferred_group_labels:
        hints["preferred_group_labels"] = preferred_group_labels
    if legend_contributed and not style_labels:
        hints["preferred_group_labels_origin"] = "legend"
    legend_template = target.get("legendFormat", "")
    if isinstance(legend_template, str) and len(legend_labels) >= 2:
        hints["legend_format_template"] = legend_template
    return hints


def _humanize_identifier(raw):
    text = re.sub(r"[_\.]+", " ", str(raw or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "Untitled"
    return " ".join(part if part.isupper() else part.capitalize() for part in text.split(" "))


def _logql_title_hint(query_text):
    """Derive a semantic title for LogQL expressions instead of raw function names."""
    if re.search(r"\bcount_over_time\s*\(", query_text, re.IGNORECASE):
        return "Log Volume"
    if re.search(r"\bbytes_over_time\s*\(", query_text, re.IGNORECASE):
        return "Log Bytes"
    if re.search(r"\brate\s*\(", query_text, re.IGNORECASE):
        return "Log Rate"
    if re.match(r"^\s*\{[^}]*\}", query_text):
        return "Log Events"
    return None


_PROMQL_AGG_FUNCS = frozenset({
    "sum", "avg", "min", "max", "count", "stddev", "stdvar",
    "topk", "bottomk", "quantile", "group",
})


def _coalesce_panel_title(panel, panel_analysis=None):
    title = str(panel.get("title") or "").strip()
    if title:
        return clean_template_variables(title) or title
    panel_type = panel.get("type", "")
    targets = panel.get("targets", [])
    visible_legends = [
        str(t.get("legendFormat") or "").strip()
        for t in targets
        if str(t.get("legendFormat") or "").strip() and not t.get("hide")
    ]
    if panel_type in ("bargauge", "table", "table-old") and len(visible_legends) > 1:
        return "Summary"
    for target in targets:
        legend = str(target.get("legendFormat") or "").strip()
        if legend:
            return legend
        query_text = target_query_text(target)
        if not query_text:
            continue
        if query_text.upper().startswith(("FROM ", "TS ", "ROW ")):
            continue
        logql_hint = _logql_title_hint(query_text)
        if logql_hint:
            return logql_hint
        metric = re.split(r"[\{\[\(\s]", query_text, maxsplit=1)[0].strip()
        if metric and metric.lower() not in _PROMQL_AGG_FUNCS:
            return _humanize_identifier(metric)
    if panel_analysis and panel_analysis.get("primary", {}).get("query_language") == "logql":
        return "Log Events"
    return "Untitled"


def _promql_top_level_group_cols(cleaned):
    """Return top-level ``by (...)`` labels for a PromQL expression, if any."""
    depth = 0
    i = 0
    while i < len(cleaned):
        ch = cleaned[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(depth - 1, 0)
        elif depth == 0:
            for keyword in ("by", "without"):
                if cleaned[i:].lower().startswith(keyword):
                    j = i + len(keyword)
                    while j < len(cleaned) and cleaned[j].isspace():
                        j += 1
                    if j < len(cleaned) and cleaned[j] == "(":
                        end = cleaned.find(")", j + 1)
                        if end != -1:
                            if keyword == "without":
                                return ["_timeseries"]
                            return [part.strip() for part in cleaned[j + 1:end].split(",") if part.strip()]
        i += 1
    return None


def _promql_repeated_inner_group_cols(cleaned):
    groups = [
        tuple(part.strip() for part in raw.split(",") if part.strip())
        for raw in re.findall(r"\bby\s*\(([^)]*)\)", cleaned, flags=re.IGNORECASE)
    ]
    groups = [group for group in groups if group]
    if len(groups) < 2:
        return None
    first = groups[0]
    if all(group == first for group in groups[1:]):
        return list(first)
    return None


def _native_promql_result_shape(promql_expr):
    """Infer the output column names for a native PROMQL query.

    Returns ``(metric_col, group_cols)`` where *metric_col* is always
    ``"value"`` (we use the explicit ``value=(query)`` syntax) and
    *group_cols* reflects the PromQL grouping semantics:

    * Cross-series aggregation with ``by (label, ...)`` → ``[label, ...]``
    * Cross-series aggregation without ``by`` → ``[]`` (scalar)
    * ``topk`` / ``bottomk`` → ``["_timeseries"]`` (preserves inner labels)
    * Within-series only (rate, irate, …) or raw metric → ``["_timeseries"]``
    """
    cleaned = _clean_promql_for_native(promql_expr)
    top_level_group_cols = _promql_top_level_group_cols(cleaned)
    if top_level_group_cols is not None:
        return "value", top_level_group_cols
    repeated_inner_group_cols = _promql_repeated_inner_group_cols(cleaned)
    if repeated_inner_group_cols is not None:
        return "value", repeated_inner_group_cols
    if re.search(r"\b(?:topk|bottomk)\s*\(", cleaned, re.IGNORECASE):
        return "value", ["_timeseries"]
    if re.search(r"\b(?:sum|avg|min|max|count|stddev|stdvar|count_values|quantile)\s*\(", cleaned, re.IGNORECASE):
        return "value", []
    return "value", ["_timeseries"]


_split_esql_pipeline = _split_esql_pipeline_canonical
_split_top_level_keyword = _split_top_level_keyword_canonical
_split_top_level_assignment = _split_top_level_assignment_canonical
_is_time_like_output_field = _is_time_like_output_field_canonical
_is_time_bucket_expression = _is_time_bucket_expression_canonical
_select_xy_dimension_fields = _select_xy_dimension_fields_canonical


def _native_esql_panel_spec(query, kibana_type, promql_expr=None, panel=None,
                            override_group_cols=None, mode=None,
                            legend_format_template=None, legend_labels=None):
    metric_col = None
    metric_fields = None
    xy_by_cols = None
    table_by_cols = None
    time_fields = None
    if promql_expr:
        metric_col, group_cols = _native_promql_result_shape(promql_expr)
        if override_group_cols is not None:
            group_cols = list(override_group_cols)
        xy_by_cols = ["step"] + group_cols
        table_by_cols = group_cols
        time_fields = ["step"]
    else:
        shape = _extract_esql_shape(query)
        time_fields = list(shape.time_fields)
        if shape.mode == "stats":
            metric_fields = list(shape.metric_fields)
            if shape.group_fields:
                xy_by_cols = list(shape.group_fields)
                table_by_cols = list(shape.group_fields)
            if metric_fields:
                metric_col = metric_fields[0]
        elif kibana_type == "datatable" and shape.projected_fields:
            return _build_esql_datatable_panel(query, metric_fields=shape.projected_fields)
        elif kibana_type in ("metric", "gauge") and len(shape.projected_fields) == 1:
            metric_col = shape.projected_fields[0]
        else:
            return None
    if kibana_type == "metric":
        if metric_fields and len(metric_fields) > 1:
            return None
        return _build_esql_metric_panel(query, metric_col=metric_col)
    if kibana_type == "gauge":
        if metric_fields and len(metric_fields) > 1:
            return None
        return _build_esql_gauge_panel(query, metric_col=metric_col, panel=panel)
    if kibana_type in ("line", "bar", "area"):
        if not xy_by_cols:
            return None
        if metric_fields and len(metric_fields) > 1:
            return _build_esql_multi_series_xy(
                query,
                kibana_type,
                metric_fields,
                by_cols=xy_by_cols,
                time_fields=time_fields,
                mode=mode,
                legend_format_template=legend_format_template,
                legend_labels=legend_labels,
            )
        return _build_esql_xy_panel(
            query, kibana_type,
            metric_col=metric_col,
            by_cols=xy_by_cols,
            time_fields=time_fields,
            mode=mode,
            legend_format_template=legend_format_template,
            legend_labels=legend_labels,
        )
    if kibana_type == "datatable":
        if metric_fields and len(metric_fields) > 1:
            return _build_esql_datatable_panel(query, metric_fields=metric_fields, by_cols=table_by_cols)
        return _build_esql_datatable_panel(query, metric_col=metric_col, by_cols=table_by_cols)
    if kibana_type == "pie":
        if metric_fields and len(metric_fields) > 1:
            return None
        if not table_by_cols:
            return None
        return _build_esql_pie_panel(query, metric_col=metric_col, by_cols=table_by_cols)
    return None


_PROMQL_UNSUPPORTED_RE = re.compile(
    r"""
      @\s*\d                                      # @ timestamp modifier
    | \[\d+[smhd]:\d+[smhd]\]                    # subquery [range:step] syntax
    | \btopk\s*\(                                 # topk not supported by ES PROMQL bridge
    | \bbottomk\s*\(                              # bottomk not supported
    | \bchanges\s*\(                              # changes() not supported
    | \bhistogram_quantile\s*\(                   # histogram_quantile not supported
    | \bpredict_linear\s*\(                       # predict_linear not supported
    | \blabel_replace\s*\(                        # label_replace not supported
    | \blabel_join\s*\(                           # label_join not supported
    | \bscalar\s*\(                               # scalar() triggers planner error
    | \b(?:on|ignoring)\s*\(                      # vector matching modifiers not supported
    | \bgroup_(?:left|right)\b                    # group modifiers not supported
    """,
    re.VERBOSE | re.IGNORECASE,
)


_GRAFANA_VAR_TOKEN_PATTERN = (
    r"(?:"
    r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]+)?\}"
    r"|"
    r"\$[A-Za-z_][A-Za-z0-9_]*"
    r"|"
    r"\[\[[A-Za-z_][A-Za-z0-9_]*(?::[^\]]+)?\]\]"
    r")"
)
_GRAFANA_VAR_BRACED_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]+)?\}")
_GRAFANA_VAR_PLAIN_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_GRAFANA_VAR_BRACKET_RE = re.compile(r"\[\[[A-Za-z_][A-Za-z0-9_]*(?::[^\]]+)?\]\]")
_GRAFANA_INTERVAL_VAR_RE = re.compile(rf"\[\s*{_GRAFANA_VAR_TOKEN_PATTERN}\s*\]")
_DEFAULT_NATIVE_PROMQL_STEP = "1m"


def _strip_promql_string_literals(expr):
    text = str(expr or "")
    text = re.sub(r'"(?:\\.|[^"])*"', '""', text)
    text = re.sub(r"'(?:\\.|[^'])*'", "''", text)
    return text


def _trim_wrapping_parens(expr):
    text = str(expr or "").strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        wraps = True
        for idx, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(depth - 1, 0)
                if depth == 0 and idx != len(text) - 1:
                    wraps = False
                    break
        if not wraps or depth != 0:
            break
        text = text[1:-1].strip()
    return text


def _promql_has_unsupported_comparison(expr):
    """Check for comparison operators that the ES PROMQL engine cannot handle.

    The ES PROMQL engine supports comparisons only when they are at the
    **top level** of the expression and the right-hand side is a **literal
    number**.  Comparisons inside aggregation functions (``count(up == 1)``)
    or between two metric expressions (``metric_a == metric_b``) are rejected.
    """
    cleaned = _trim_wrapping_parens(_clean_promql_for_native(expr))
    stripped = re.sub(r"\{[^{}]*\}", "{}", cleaned)
    stripped = _strip_promql_string_literals(stripped)

    comp_re = re.compile(r"(==\s*bool\b|==|!=|>=|<=|(?<![=!~<>])>(?![=])|(?<![=!~<>])<(?![=]))")
    depth = 0
    i = 0
    while i < len(stripped):
        ch = stripped[i]
        if ch == '(' or ch == '[':
            depth += 1
            i += 1
            continue
        if ch == ')' or ch == ']':
            depth = max(0, depth - 1)
            i += 1
            continue
        m = comp_re.match(stripped, i)
        if m:
            if depth > 0:
                return True
            rhs = stripped[m.end():].lstrip()
            if rhs and not re.match(r'^[\d.+-]', rhs):
                return True
            i = m.end()
            continue
        i += 1
    return False


def _promql_has_known_server_bug(expr):
    cleaned = _clean_promql_for_native(expr)
    if (
        "node_filesystem_avail_bytes" in cleaned
        and "node_filesystem_free_bytes" in cleaned
        and "node_filesystem_size_bytes" in cleaned
        and "+(" in cleaned
    ):
        return True
    stripped = _strip_promql_string_literals(cleaned)
    stripped = re.sub(r"\{[^{}]*\}", "{}", stripped)
    if re.search(r"\band\b", stripped, re.IGNORECASE):
        return True
    if re.search(r"\bor\b", stripped, re.IGNORECASE):
        return True
    if re.search(r"\bunless\b", stripped, re.IGNORECASE):
        return True
    return False


def _clean_promql_for_native_with_state(expr):
    """Strip Grafana template variables from a PromQL expression so it can be
    sent to the ES PROMQL engine which does not know about ``$var`` syntax."""
    had_bare_variable = False
    # Replace $__rate_interval / $__interval with the window from the
    # expression itself, falling back to 5m.
    window_match = re.search(r"\[(\d+[smhd])\]", expr)
    fallback = window_match.group(1) if window_match else "5m"
    expr = re.sub(r"\$__rate_interval", fallback, expr)
    expr = re.sub(r"\$__interval", _DEFAULT_NATIVE_PROMQL_STEP, expr)
    expr = re.sub(r"\$interval", _DEFAULT_NATIVE_PROMQL_STEP, expr)
    expr = _GRAFANA_INTERVAL_VAR_RE.sub(f"[{fallback}]", expr)

    # Turn single-quoted strings into double-quoted (PromQL standard).
    expr = re.sub(r"='([^']*)'", r'="\1"', expr)
    expr = re.sub(r"!~'([^']*)'", r'!~"\1"', expr)
    expr = re.sub(r"=~'([^']*)'", r'=~"\1"', expr)

    # Replace $variable references inside label selectors with wildcards.
    expr = re.sub(rf'=~"\s*{_GRAFANA_VAR_TOKEN_PATTERN}\s*"', '=~".*"', expr)
    expr = re.sub(rf'="\s*{_GRAFANA_VAR_TOKEN_PATTERN}\s*"', '=~".*"', expr)
    expr = re.sub(rf'!~"\s*{_GRAFANA_VAR_TOKEN_PATTERN}\s*"', '!~""', expr)
    expr = re.sub(rf'!="\s*{_GRAFANA_VAR_TOKEN_PATTERN}\s*"', '!= ""', expr)

    # Some upstream dashboards contain whitespace between a metric name and its
    # selector/range, e.g. ``node_filesystem_avail_bytes {..}``, which ES rejects.
    expr = re.sub(r"([A-Za-z_:][A-Za-z0-9_:]*)\s+([\[{])", r"\1\2", expr)

    # Remove any remaining bare $variable tokens (e.g. in arithmetic).
    # Multiplicative identity preserves magnitude better than 0.
    if (
        _GRAFANA_VAR_BRACED_RE.search(expr)
        or _GRAFANA_VAR_PLAIN_RE.search(expr)
        or _GRAFANA_VAR_BRACKET_RE.search(expr)
    ):
        had_bare_variable = True
    expr = _GRAFANA_VAR_BRACED_RE.sub("1", expr)
    expr = _GRAFANA_VAR_PLAIN_RE.sub("1", expr)
    expr = _GRAFANA_VAR_BRACKET_RE.sub("1", expr)

    expr = re.sub(r"\s+", " ", expr).strip()

    return expr, had_bare_variable


def _clean_promql_for_native(expr):
    cleaned, _ = _clean_promql_for_native_with_state(expr)
    return cleaned


def _extract_legend_labels(legend_format):
    """Parse ``{{label}}`` placeholders from a Grafana legendFormat string."""
    if not legend_format or legend_format in ("__auto", ""):
        return []
    return re.findall(r"\{\{\s*(\w+)\s*\}\}", legend_format)


def _static_legend_label(legend_format):
    if not legend_format:
        return ""
    if _extract_legend_labels(legend_format):
        return ""
    label = clean_template_variables(str(legend_format).strip())
    label = re.sub(r"^[\s\-–—:,;]+|[\s\-–—:,;]+$", "", label)
    return label


def _label_native_promql_value_metric(yaml_panel, *, title, legend_format=""):
    esql = yaml_panel.get("esql")
    if not isinstance(esql, dict):
        return
    metrics = esql.get("metrics")
    if not isinstance(metrics, list):
        return
    fallback_label = _static_legend_label(legend_format)
    if not fallback_label:
        fallback_label = clean_template_variables(str(title or "").strip())
    fallback_label = fallback_label.strip()
    if not fallback_label or fallback_label == "Untitled":
        return
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        if metric.get("field") != "value":
            continue
        metric.setdefault("label", fallback_label)


def build_native_promql_query(promql_expr, index="metrics-prometheus-*",
                              legend_labels=None, kibana_type=None,
                              legend_format=None):
    """Build a PROMQL ES|QL source command that wraps the original PromQL expression.

    Uses the explicit value column name syntax ``value=(query)`` so that the
    output metric column is always named ``value`` regardless of the PromQL
    expression complexity.  This makes Kibana panel field references stable.

    When the PROMQL result includes ``_timeseries`` (no explicit ``by`` clause)
    and *legend_labels* are provided, appends ``EVAL`` pipes to extract those
    labels from the ``_timeseries`` JSON string, producing clean named columns.

    When *legend_labels* is empty but *legend_format* is a non-empty literal
    string with no placeholders, emits ``EVAL label = "<text>"`` so Lens
    renders the author's chosen series name instead of the raw label tuple.
    When both are absent, no synthetic label column is added: Lens renders a
    single unlabeled series, which matches what Grafana shows for an empty
    ``legendFormat`` and avoids dumping the stringified ``_timeseries`` JSON
    as the legend entry.

    For single-value panel types (metric, gauge) the ``_timeseries`` extraction
    is skipped because aggregated scalars don't have that column.
    """
    if not can_use_native_promql(promql_expr):
        raise ValueError("PromQL expression is not supported by the native PROMQL path")
    cleaned = _clean_promql_for_native(promql_expr)
    step = _DEFAULT_NATIVE_PROMQL_STEP
    base = (
        f'PROMQL index={index} step={step} '
        f'value=({cleaned})'
    )

    if kibana_type in ("metric", "gauge"):
        return base

    _, group_cols = _native_promql_result_shape(promql_expr)
    if "_timeseries" not in group_cols:
        return base

    if legend_labels:
        evals = [
            '| EVAL _ts = COALESCE(_timeseries, "")',
        ]
        for lbl in legend_labels:
            raw = f"_raw_{lbl}"
            evals.append(
                f'| EVAL {raw} = CASE(_ts == "", "unknown", '
                f'REPLACE(_ts, """.*"{lbl}":"([^"]+)".*""", "$1"))'
            )
            evals.append(
                f'| EVAL {lbl} = CASE(STARTS_WITH({raw}, "{{"), '
                f'REPLACE(REPLACE(_ts, """[{{}}""]""", ""), ",", ", "), '
                f'{raw})'
            )
        keep = ["step", "value"] + list(legend_labels)
        return base + "\n" + "\n".join(evals) + f'\n| KEEP {", ".join(keep)}'

    static_label = (legend_format or "").strip()
    if static_label:
        # Static legend text (no placeholders) — emit it verbatim as the
        # series label so Lens uses the author's chosen name.
        escaped = _escape_esql_double_quoted_literal(static_label)
        return (
            base
            + f'\n| EVAL label = "{escaped}"'
            + "\n| KEEP step, value, label"
        )

    # Neither placeholders nor static legend text — drop the synthetic
    # label column entirely. Lens then renders one unlabeled series,
    # matching Grafana's behaviour for an empty legendFormat. Previously
    # we emitted ``EVAL label = CASE(_ts == "", "series", REPLACE(...))``
    # which dumped the stringified label tuple as the legend, an ugly
    # regression spotted in NEF screenshots.
    return base


def can_use_native_promql(promql_expr):
    """Return True if the expression is within the server-supported PromQL subset."""
    if not promql_expr or not promql_expr.strip():
        return False
    sanitized = _strip_promql_string_literals(promql_expr)
    if _PROMQL_UNSUPPORTED_RE.search(sanitized):
        return False
    if _promql_has_unsupported_comparison(promql_expr):
        return False
    if _promql_has_known_server_bug(promql_expr):
        return False
    return True


_COUNTER_RANGE_FUNC_PATTERN = re.compile(
    r"\b(?P<func>rate|irate|increase)\s*\(\s*(?P<metric>[A-Za-z_:][A-Za-z0-9_:]*)\b",
    re.IGNORECASE,
)


_METRIC_REF_PATTERN = re.compile(
    r"\b(?P<metric>[a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{|\[|\(|$|\s|/|\*|\+|-)",
)

# PromQL function / aggregator / keyword tokens that look like metric
# selectors but aren't.
_PROMQL_KEYWORDS = frozenset({
    "by", "without", "on", "ignoring", "group_left", "group_right",
    "and", "or", "unless", "bool", "offset", "atan2",
    # Aggregators
    "sum", "avg", "min", "max", "count", "stddev", "stdvar",
    "topk", "bottomk", "quantile", "group", "count_values",
    # Range/instant functions
    "rate", "irate", "increase", "delta", "deriv", "predict_linear",
    "changes", "resets", "idelta",
    "avg_over_time", "sum_over_time", "min_over_time", "max_over_time",
    "stddev_over_time", "stdvar_over_time", "count_over_time",
    "last_over_time", "quantile_over_time", "present_over_time",
    "histogram_quantile", "histogram_count", "histogram_sum",
    "histogram_avg", "histogram_fraction", "histogram_stddev",
    "histogram_stdvar",
    # Math / time functions
    "abs", "absent", "absent_over_time", "ceil", "exp", "floor",
    "ln", "log2", "log10", "round", "scalar", "sgn", "sort", "sort_desc",
    "sqrt", "time", "year", "month", "day_of_month", "day_of_week",
    "day_of_year", "days_in_month", "hour", "minute", "timestamp",
    "vector", "pi", "label_replace", "label_join", "clamp", "clamp_max",
    "clamp_min", "acos", "acosh", "asin", "asinh", "atan", "atanh",
    "cos", "cosh", "sin", "sinh", "tan", "tanh", "deg", "rad",
    "nan", "inf",
})


def _extract_promql_metric_names(promql_expr):
    """Return the distinct metric names referenced by *promql_expr*.

    Strips string literals and label-set bodies first, then walks the
    remaining text picking up tokens that look like metric identifiers.
    PromQL keywords and function names are filtered against a curated
    list so they don't show up as fake metric references.
    """
    if not promql_expr:
        return []
    sanitized = _strip_promql_string_literals(promql_expr)
    # Replace ``{...}`` label-set bodies with empty braces so the inside
    # doesn't leak label names as metric tokens.
    sanitized = re.sub(r"\{[^{}]*\}", "{}", sanitized)
    seen: list[str] = []
    for match in _METRIC_REF_PATTERN.finditer(sanitized):
        name = match.group("metric")
        if not name or name.lower() in _PROMQL_KEYWORDS:
            continue
        # Skip bare numbers picked up as identifiers (the pattern
        # already filters them out, but be safe).
        if name[0].isdigit():
            continue
        if name not in seen:
            seen.append(name)
    return seen


def _native_promql_has_distinct_metric_arithmetic(promql_expr):
    """Return True if *promql_expr* contains an arithmetic binary op
    (``+``/``-``/``*``/``/``/``%``/``^``) between two operands that
    reference different metric names.

    Elastic's PROMQL preview can't infer the implicit 1:1 label-set
    match Prometheus uses for such expressions and returns an empty
    result or 400. The translator should detect this and fall through
    to ES|QL translation, which performs the arithmetic at the bucket
    level with explicit groupings.

    Explicit ``ignoring(...)`` / ``on(...)`` modifiers are already
    rejected by ``can_use_native_promql``; here we catch the
    implicit-match case where the user just wrote ``A / B``. Grafana
    template variables (``$var`` / ``${var}`` / ``[[var]]``) are
    excluded from the metric-name extraction so panels like
    ``up * $scale`` still route through native PROMQL.
    """
    if not promql_expr:
        return False
    sanitized = _strip_promql_string_literals(promql_expr)
    # Replace label-set bodies so their contents don't leak operators.
    sanitized = re.sub(r"\{[^{}]*\}", "{}", sanitized)
    # Remove Grafana template variable tokens so they don't get
    # mistaken for metric references.
    sanitized_no_vars = sanitized
    for pattern in (
        _GRAFANA_VAR_BRACED_RE,
        _GRAFANA_VAR_BRACKET_RE,
        _GRAFANA_VAR_PLAIN_RE,
    ):
        sanitized_no_vars = pattern.sub(" ", sanitized_no_vars)
    metrics = _extract_promql_metric_names(sanitized_no_vars)
    if len(metrics) < 2:
        return False
    # If the expression contains any arithmetic operator AND references
    # two or more distinct metric names, treat it as distinct-metric
    # arithmetic that Elastic's PROMQL preview can't safely evaluate.
    if re.search(r"[+\-*/%^]", sanitized_no_vars):
        return True
    return False


def _native_promql_has_counter_func_on_gauge(promql_expr, resolver):
    """Return True if *promql_expr* applies ``rate``/``irate``/``increase``
    to a metric that the resolver has *positively* identified as
    gauge-typed in the target index.

    Used as a pre-flight gate before emitting native PROMQL: Elastic's
    PROMQL command rejects counter-style range functions on gauge-typed
    fields at render time with ``first argument of [RATE(...)] must be
    counter``. Falling through to ES|QL translation lets the gauge
    fallback emit a degraded query the cluster can actually serve.

    The gate requires positive evidence (the field is present in the
    target index AND is typed gauge). Unknown fields or fields without
    a recorded ``time_series_metric`` are left alone so existing
    coverage of expressions like ``rate(foo[5m]) offset 1h`` against a
    bare/empty schema isn't disturbed.
    """
    if resolver is None or not promql_expr:
        return False
    sanitized = _strip_promql_string_literals(promql_expr)
    for match in _COUNTER_RANGE_FUNC_PATTERN.finditer(sanitized):
        metric = match.group("metric")
        if not metric:
            continue
        try:
            cap = resolver.field_capability(metric)
        except Exception:
            continue
        if cap is None:
            continue
        # Only act when the cluster has explicitly typed this field as
        # something other than ``counter``. ``None`` / unknown means
        # "no evidence either way" — leave the native PROMQL path alone.
        kind = getattr(cap, "time_series_metric_kind", None)
        if kind and kind != "counter":
            return True
    return False


def _translate_panel_native_promql(
    panel, yaml_panel, title, panel_type, kibana_type,
    datasource, datasource_index, rule_pack, panel_notes, panel_inventory,
    query_language, visible_targets, resolver=None,
):
    """Attempt native PROMQL translation for single or multi-target panels.

    Returns ``(yaml_panel, panel_result)`` on success, or ``None`` to signal
    the caller should fall through to the normal ES|QL translation path.
    """
    targets_with_expr = [
        (target, query_text)
        for target, query_text in visible_targets
        if target.get("expr")
    ]
    if not targets_with_expr:
        return None

    if len(targets_with_expr) != 1:
        return None

    target = targets_with_expr[0][0]
    expr = target.get("expr", "")
    if not can_use_native_promql(expr):
        return None
    # Pre-flight type check: if the source PromQL applies a counter-style
    # range function (``rate``/``irate``/``increase``) to a metric that
    # the target index has typed as gauge, the native PROMQL command will
    # 400 with ``first argument of [RATE(...)] must be counter`` at
    # render time. Fall through to ES|QL translation, which knows how to
    # degrade to a gauge-equivalent. Surfaced by validating uploaded
    # Node Exporter Full panels referencing node_vmstat_* / node_netstat_*
    # counters that don't end in ``_total`` (Elastic's auto-mapping
    # treats them as gauges).
    if resolver is not None and _native_promql_has_counter_func_on_gauge(expr, resolver):
        return None
    # Gate: Elastic's PROMQL preview can't perform implicit label-set
    # match for arithmetic between two distinct instant vectors (e.g.
    # ``A / B`` without ``on()``). Fall through to ES|QL translation,
    # which does the math at the bucket level. Surfaced by reviewing
    # uploaded NEF panels like ``Disk Space Used Basic`` that rendered
    # "No results found" because the native PROMQL command rejected
    # the query.
    if _native_promql_has_distinct_metric_arithmetic(expr):
        return None
    legend_format = target.get("legendFormat", "")
    legend_labels = _extract_legend_labels(legend_format)

    index = datasource_index or "metrics-prometheus-*"
    cleaned_expr, had_bare_variable = _clean_promql_for_native_with_state(expr)
    _, group_cols = _native_promql_result_shape(expr)
    if kibana_type in ("metric", "gauge") and group_cols:
        return None
    promql_query = build_native_promql_query(expr, index=index,
                                             legend_labels=legend_labels,
                                             kibana_type=kibana_type,
                                             legend_format=legend_format)
    if had_bare_variable:
        _append_unique(panel_notes, "Grafana template variables in arithmetic were replaced with literal 1")

    static_legend_label = (legend_format or "").strip() and not legend_labels
    if "_timeseries" in group_cols:
        if legend_labels:
            effective_group_cols = legend_labels
        elif static_legend_label:
            # Single static label per series.
            effective_group_cols = ["label"]
        else:
            # No legend dimension; the query keeps just step+value.
            effective_group_cols = []
    else:
        effective_group_cols = group_cols

    xy_mode = _infer_xy_stacking_mode(panel) if kibana_type in ("bar", "area") else None
    composite_legend_template = legend_format if len(legend_labels) >= 2 else None
    native_panel = _native_esql_panel_spec(
        promql_query, kibana_type, promql_expr=expr, panel=panel,
        override_group_cols=effective_group_cols, mode=xy_mode,
        legend_format_template=composite_legend_template,
        legend_labels=legend_labels if composite_legend_template else None,
    )
    if not native_panel:
        return None

    yaml_panel["esql"] = native_panel
    enrich_yaml_panel_display(yaml_panel, panel)
    _label_native_promql_value_metric(yaml_panel, title=title, legend_format=legend_format)

    notes = list(panel_notes) + ["Native PROMQL: original PromQL reused via ES|QL PROMQL command"]

    query_ir = QueryIR()
    query_ir.source_language = "promql"
    query_ir.source_expression = expr
    query_ir.clean_expression = cleaned_expr
    query_ir.panel_type = panel_type
    query_ir.datasource_type = datasource.get("type", "")
    query_ir.datasource_uid = datasource.get("uid", "")
    query_ir.datasource_name = datasource.get("name", "")
    query_ir.family = "native_promql"
    native_fragment = _parse_fragment(cleaned_expr or expr)
    query_ir.metric = str(getattr(native_fragment, "metric", "") or "")
    query_ir.range_function = str(getattr(native_fragment, "range_func", "") or "")
    query_ir.range_window = str(getattr(native_fragment, "range_window", "") or "")
    query_ir.outer_agg = str(getattr(native_fragment, "outer_agg", "") or "")
    query_ir.group_labels = list(getattr(native_fragment, "group_labels", []) or [])
    query_ir.group_mode = str(getattr(native_fragment, "group_mode", "") or "by")
    if kibana_type in ("line", "bar", "area"):
        query_ir.output_group_fields = ["step"] + list(effective_group_cols)
    elif kibana_type == "datatable" or kibana_type == "pie":
        query_ir.output_group_fields = list(effective_group_cols)
    else:
        query_ir.output_group_fields = []
    query_ir.output_shape = infer_output_shape(panel_type, query_ir.output_group_fields, "promql")
    query_ir.target_index = index
    query_ir.target_query = promql_query

    confidence = 0.90
    panel_result = PanelResult(
        title,
        panel_type,
        kibana_type,
        "migrated",
        confidence,
        promql_expr=expr,
        esql_query=promql_query,
    )
    return yaml_panel, _enrich_panel_result(
        panel_result,
        panel=panel,
        datasource=datasource,
        query_language="promql",
        notes=notes,
        inventory=panel_inventory,
        query_ir=query_ir,
        yaml_panel=yaml_panel,
        rule_pack=rule_pack,
    )


def _translate_multi_target_native_promql(
    panel, yaml_panel, title, panel_type, kibana_type,
    datasource, datasource_index, rule_pack, panel_notes,
    panel_inventory, targets_with_expr,
):
    """Combine multiple PromQL targets into a single native PROMQL panel.

    Uses ``label_replace`` + ``or`` to inject per-target legend labels so all
    series appear on one chart with distinct breakdown values.  Only attempted
    for XY chart types (line, bar, area) where overlay makes sense.
    """
    if kibana_type not in ("line", "bar", "area"):
        return None

    index = datasource_index or "metrics-prometheus-*"
    had_bare_variable = False
    parts: list[str] = []
    target_fragments = []

    for target, _ in targets_with_expr:
        expr = target.get("expr", "")
        if not can_use_native_promql(expr):
            return None
        cleaned, bare = _clean_promql_for_native_with_state(expr)
        had_bare_variable = had_bare_variable or bare
        target_fragments.append(_parse_fragment(cleaned or expr))

        legend = (target.get("legendFormat") or "").strip()
        if not legend or legend == "{{}}":
            legend = expr[:40]
        legend = legend.replace('"', '\\"')

        parts.append(
            f'label_replace({cleaned}, "__series", "{legend}", "", "")'
        )

    combined_expr = " or ".join(parts)
    step = _DEFAULT_NATIVE_PROMQL_STEP
    promql_query = f"PROMQL index={index} step={step} value=({combined_expr})"

    if had_bare_variable:
        _append_unique(panel_notes, "Grafana template variables in arithmetic were replaced with literal 1")

    effective_group_cols = ["__series"]
    xy_mode = _infer_xy_stacking_mode(panel) if kibana_type in ("bar", "area") else None
    native_panel = _native_esql_panel_spec(
        promql_query, kibana_type, promql_expr=combined_expr, panel=panel,
        override_group_cols=effective_group_cols, mode=xy_mode,
    )
    if not native_panel:
        return None

    yaml_panel["esql"] = native_panel
    enrich_yaml_panel_display(yaml_panel, panel)

    notes = list(panel_notes) + [
        "Native PROMQL: multi-target combined via label_replace + or",
    ]

    query_ir = QueryIR()
    query_ir.source_language = "promql"
    query_ir.source_expression = " ; ".join(t.get("expr", "") for t, _ in targets_with_expr)
    query_ir.clean_expression = combined_expr
    query_ir.panel_type = panel_type
    query_ir.datasource_type = datasource.get("type", "")
    query_ir.datasource_uid = datasource.get("uid", "")
    query_ir.datasource_name = datasource.get("name", "")
    query_ir.family = "native_promql"
    metric_names = []
    for frag in target_fragments:
        metric_name = str(getattr(frag, "metric", "") or "").strip()
        if metric_name and metric_name not in metric_names:
            metric_names.append(metric_name)
    if len(metric_names) == 1:
        query_ir.metric = metric_names[0]
    elif len(metric_names) > 1:
        query_ir.metadata["multi_series_metric_fields"] = list(metric_names)
    range_functions = {
        str(getattr(frag, "range_func", "") or "").strip()
        for frag in target_fragments
        if frag
    }
    range_functions.discard("")
    if len(range_functions) == 1:
        query_ir.range_function = next(iter(range_functions))
    range_windows = {
        str(getattr(frag, "range_window", "") or "").strip()
        for frag in target_fragments
        if frag
    }
    range_windows.discard("")
    if len(range_windows) == 1:
        query_ir.range_window = next(iter(range_windows))
    outer_aggs = {
        str(getattr(frag, "outer_agg", "") or "").strip()
        for frag in target_fragments
        if frag
    }
    outer_aggs.discard("")
    if len(outer_aggs) == 1:
        query_ir.outer_agg = next(iter(outer_aggs))
    group_labels = {
        tuple(getattr(frag, "group_labels", []) or [])
        for frag in target_fragments
        if frag
    }
    group_labels.discard(())
    if len(group_labels) == 1:
        query_ir.group_labels = list(next(iter(group_labels)))
    group_modes = {
        str(getattr(frag, "group_mode", "") or "by").strip()
        for frag in target_fragments
        if frag
    }
    if len(group_modes) == 1:
        query_ir.group_mode = next(iter(group_modes))
    query_ir.output_group_fields = ["step", "__series"]
    query_ir.output_shape = infer_output_shape(panel_type, query_ir.output_group_fields, "promql")
    query_ir.target_index = index
    query_ir.target_query = promql_query

    panel_result = PanelResult(
        title, panel_type, kibana_type, "migrated", 0.80,
        promql_expr=combined_expr, esql_query=promql_query,
    )
    return yaml_panel, _enrich_panel_result(
        panel_result,
        panel=panel,
        datasource=datasource,
        query_language="promql",
        notes=notes,
        inventory=panel_inventory,
        query_ir=query_ir,
        yaml_panel=yaml_panel,
        rule_pack=rule_pack,
    )


def _sync_visual_ir(panel_result, yaml_panel):
    panel_result.visual_ir = refresh_visual_ir(panel_result, yaml_panel)


def _artifact_to_dict(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return dict(value)
    return {}


def _query_ir_multi_series_metric_fields(query_ir):
    if not query_ir:
        return []
    metadata = (
        query_ir.get("metadata", {})
        if isinstance(query_ir, dict)
        else getattr(query_ir, "metadata", {})
    ) or {}
    fields = []
    for field_name in (metadata.get("multi_series_metric_fields", []) or []):
        normalized = str(field_name or "").strip()
        if normalized and normalized not in fields:
            fields.append(normalized)
    return fields


def _enrich_panel_result(
    panel_result,
    panel=None,
    datasource=None,
    query_language="",
    notes=None,
    inventory=None,
    query_ir=None,
    yaml_panel=None,
    translation=None,
    rule_pack=None,
):
    panel = panel or {}
    datasource = datasource or {}
    panel_result.source_panel_id = str(panel.get("id") or panel.get("panelId") or "")
    panel_result.datasource_type = str(datasource.get("type") or "")
    panel_result.datasource_uid = str(datasource.get("uid") or "")
    panel_result.datasource_name = str(datasource.get("name") or "")
    panel_result.query_language = query_language or infer_query_language(
        panel_result.promql_expr or panel_result.esql_query,
        panel_result.datasource_type,
        panel_result.grafana_type,
    )
    panel_result.inventory = dict(inventory or {})
    for note in notes or []:
        _append_unique(panel_result.notes, note)
    if query_ir:
        panel_result.query_ir = query_ir.to_dict() if hasattr(query_ir, "to_dict") else dict(query_ir)
    carrier_query_ir = query_ir or panel_result.query_ir
    contract = getattr(translation, "target_query_contract", {}) if translation is not None else {}
    evaluation = getattr(translation, "contract_evaluation", {}) if translation is not None else {}
    fulfillment = getattr(translation, "fulfillment_plan", {}) if translation is not None else {}
    if carrier_query_ir and (
        _query_ir_multi_series_metric_fields(carrier_query_ir)
        or not any((contract, evaluation, fulfillment))
    ):
        rebuilt_contract, rebuilt_evaluation, rebuilt_fulfillment = _build_metric_contract_artifacts(
            carrier_query_ir,
            resolver=getattr(translation, "resolver", None),
            rule_pack=rule_pack or getattr(translation, "rule_pack", None),
        )
        if any((rebuilt_contract, rebuilt_evaluation, rebuilt_fulfillment)):
            contract = rebuilt_contract
            evaluation = rebuilt_evaluation
            fulfillment = rebuilt_fulfillment
    panel_result.target_query_contract = _artifact_to_dict(contract)
    panel_result.contract_evaluation = _artifact_to_dict(evaluation)
    panel_result.fulfillment_plan = _artifact_to_dict(fulfillment)
    final_source_type = str((panel_result.query_ir or {}).get("source_type", "") or "").upper()
    if final_source_type == "FROM" and panel_result.target_query_contract.get("canonical_target") in {"ts", "promql"}:
        existing_status = (panel_result.contract_evaluation or {}).get("status")
        if existing_status != "blocked":
            if panel_result.contract_evaluation:
                panel_result.contract_evaluation = dict(panel_result.contract_evaluation)
                panel_result.contract_evaluation["status"] = "degraded_if_forced"
            panel_result.fulfillment_plan = {
                "status": "not_required",
                "actions": [],
            }
    panel_result.readiness = classify_panel_readiness(panel_result)
    panel_result.recommended_target = recommend_panel_target(panel_result)
    _sync_visual_ir(panel_result, yaml_panel)
    return panel_result


@PANEL_TRANSLATORS.register("metric_panel", priority=10)
def metric_panel_rule(context):
    if context.kibana_type != "metric":
        return None
    series_fields = context.translation.metadata.get("multi_series_metric_fields", [])
    if context.translation.output_group_fields or series_fields:
        context.yaml_panel["esql"] = _build_esql_datatable_panel(
            context.translation.esql_query,
            metric_col=context.translation.output_metric_field or None,
            metric_fields=series_fields or None,
            by_cols=context.translation.output_group_fields,
        )
        context.kibana_type = "datatable"
        _append_unique(
            context.translation.warnings,
            "Approximated grouped stat panel as summary table",
        )
        context.handled = True
        return "approximated grouped stat as datatable"
    context.yaml_panel["esql"] = _build_esql_metric_panel(
        context.translation.esql_query,
        metric_col=context.translation.output_metric_field or None,
    )
    context.handled = True
    return "mapped to metric panel"


@PANEL_TRANSLATORS.register("bargauge_panel", priority=15)
def bargauge_panel_rule(context):
    if context.panel_type != "bargauge":
        return None
    primary = context.translation
    series_fields = primary.metadata.get("multi_series_metric_fields", [])
    query = primary.esql_query or ""
    has_time_dim = "TBUCKET(" in query and not (
        "| KEEP " in query and "time_bucket" not in query.split("| KEEP")[-1]
    )
    if series_fields and (_summary_mode_from_metadata(primary.metadata) or not has_time_dim):
        restored_query, restored = _restore_summary_time_bucket(query)
        category_query = _build_summary_category_bar_query(
            restored_query if restored else query,
            series_fields,
            primary.metadata.get("multi_series_metric_labels", {}),
        )
        primary.esql_query = category_query
        context.yaml_panel["esql"] = _build_esql_xy_panel(
            category_query,
            "bar",
            metric_col="value",
            by_cols=["label"],
        )
        context.kibana_type = "bar"
        context.yaml_panel["esql"]["legend"] = {
            "visible": "hide",
            "position": "right",
            "truncate_labels": 1,
        }
        _append_unique(context.translation.warnings, "Approximated bargauge as bar chart")
    elif series_fields:
        context.yaml_panel["esql"] = _build_esql_multi_series_xy(
            primary.esql_query,
            "bar",
            metric_fields=series_fields,
            by_cols=primary.output_group_fields,
        )
        context.kibana_type = "bar"
        _append_unique(context.translation.warnings, "Approximated bargauge as bar chart")
    elif primary.output_group_fields:
        context.yaml_panel["esql"] = _build_esql_xy_panel(
            primary.esql_query,
            "bar",
            metric_col=primary.output_metric_field or None,
            by_cols=primary.output_group_fields,
        )
        context.kibana_type = "bar"
        _append_unique(context.translation.warnings, "Approximated bargauge as bar chart")
    else:
        context.yaml_panel["esql"] = _build_esql_metric_panel(
            primary.esql_query,
            metric_col=primary.output_metric_field or None,
        )
        context.kibana_type = "metric"
        _append_unique(context.translation.warnings, "Approximated bargauge as metric")
    context.handled = True
    return "approximated bargauge panel"


@PANEL_TRANSLATORS.register("xy_panel", priority=20)
def xy_panel_rule(context):
    if context.kibana_type not in ("line", "bar", "area") or context.panel_type == "bargauge":
        return None
    primary = context.translation
    mode = _infer_xy_stacking_mode(context.panel) if context.kibana_type in ("bar", "area") else None
    series_fields = primary.metadata.get("multi_series_metric_fields", [])
    legend_template = primary.metadata.get("legend_format_template") or None
    legend_labels = _extract_legend_labels(legend_template) if legend_template else []
    composite_template = legend_template if len(legend_labels) >= 2 else None
    if series_fields:
        context.yaml_panel["esql"] = _build_esql_multi_series_xy(
            primary.esql_query,
            context.kibana_type,
            metric_fields=series_fields,
            by_cols=primary.output_group_fields,
            mode=mode,
            legend_format_template=composite_template,
            legend_labels=legend_labels if composite_template else None,
        )
    else:
        context.yaml_panel["esql"] = _build_esql_xy_panel(
            primary.esql_query,
            context.kibana_type,
            metric_col=primary.output_metric_field or None,
            by_cols=primary.output_group_fields,
            mode=mode,
            legend_format_template=composite_template,
            legend_labels=legend_labels if composite_template else None,
        )
    context.handled = True
    return f"mapped to {context.kibana_type} panel"


@PANEL_TRANSLATORS.register("gauge_panel", priority=30)
def gauge_panel_rule(context):
    if context.kibana_type != "gauge":
        return None
    series_fields = context.translation.metadata.get("multi_series_metric_fields", [])
    if context.translation.output_group_fields or series_fields:
        context.yaml_panel["esql"] = _build_esql_datatable_panel(
            context.translation.esql_query,
            metric_col=context.translation.output_metric_field or None,
            metric_fields=series_fields or None,
            by_cols=context.translation.output_group_fields,
        )
        context.kibana_type = "datatable"
        _append_unique(
            context.translation.warnings,
            "Approximated grouped gauge panel as summary table",
        )
        context.handled = True
        return "approximated grouped gauge as datatable"
    context.yaml_panel["esql"] = _build_esql_gauge_panel(
        context.translation.esql_query,
        metric_col=context.translation.output_metric_field or None,
        panel=context.panel,
    )
    context.handled = True
    return "mapped to gauge panel"


@PANEL_TRANSLATORS.register("datatable_panel", priority=40)
def datatable_panel_rule(context):
    if context.kibana_type != "datatable":
        return None
    metric_fields = context.translation.metadata.get("multi_series_metric_fields", [])
    context.yaml_panel["esql"] = _build_esql_datatable_panel(
        context.translation.esql_query,
        metric_col=context.translation.output_metric_field or None,
        metric_fields=metric_fields or None,
        by_cols=context.translation.output_group_fields,
    )
    context.handled = True
    return "mapped to datatable panel"


@PANEL_TRANSLATORS.register("pie_panel", priority=50)
def pie_panel_rule(context):
    if context.kibana_type != "pie":
        return None
    context.yaml_panel["esql"] = _build_esql_pie_panel(
        context.translation.esql_query,
        metric_col=context.translation.output_metric_field or None,
        by_cols=context.translation.output_group_fields,
    )
    if (context.yaml_panel.get("esql") or {}).get("type") != "pie":
        _append_unique(
            context.translation.warnings,
            "Approximated pie chart as bar chart because no categorical breakdown was available",
        )
    context.handled = True
    return f"mapped to {(context.yaml_panel.get('esql') or {}).get('type', 'pie')} panel"


@PANEL_TRANSLATORS.register("fallback_line_panel", priority=90)
def fallback_line_panel_rule(context):
    if context.handled:
        return None
    primary = context.translation
    legend_template = primary.metadata.get("legend_format_template") or None
    legend_labels = _extract_legend_labels(legend_template) if legend_template else []
    composite_template = legend_template if len(legend_labels) >= 2 else None
    context.yaml_panel["esql"] = _build_esql_xy_panel(
        primary.esql_query,
        "line",
        metric_col=primary.output_metric_field or None,
        by_cols=primary.output_group_fields,
        legend_format_template=composite_template,
        legend_labels=legend_labels if composite_template else None,
    )
    _append_unique(
        primary.warnings,
        f"Approximated as line chart (no direct {context.kibana_type} mapping)",
    )
    context.handled = True
    return "fell back to line panel"


def translate_panel(panel, datasource_index="metrics-*", esql_index=None, rule_pack=None, resolver=None,
                    llm_endpoint="", llm_model="", llm_api_key=""):
    """Translate a single Grafana panel, fusing multiple targets when possible."""
    rule_pack = rule_pack or RulePackConfig()
    panel_type = panel.get("type", "unknown")
    panel_analysis = analyze_panel_targets(panel)
    title = _coalesce_panel_title(panel, panel_analysis)
    panel_inventory = collect_panel_inventory(panel)
    panel_notes = collect_panel_notes(panel, panel_analysis)
    primary_target = panel_analysis.get("primary", {})
    datasource = primary_target.get("datasource", normalize_datasource(panel.get("datasource")))
    query_language = primary_target.get("query_language", "unknown")
    skip_panel_types = SKIP_PANEL_TYPES | set(rule_pack.skip_panel_types)

    if panel_type in skip_panel_types:
        panel_result = PanelResult(title, panel_type, "", "skipped", 1.0)
        return None, _enrich_panel_result(
            panel_result,
            panel=panel,
            datasource=datasource,
            query_language=query_language,
            notes=panel_notes,
            inventory=panel_inventory,
            yaml_panel=None,
        )

    kibana_type = _resolved_panel_type_map(rule_pack).get(panel_type)
    if panel_type == "graph" and kibana_type == "line":
        kibana_type = _infer_graph_chart_style(panel)
    elif panel_type == "timeseries" and kibana_type == "line":
        kibana_type = _infer_timeseries_chart_style(panel)
    if not kibana_type:
        panel_result = PanelResult(
            title,
            panel_type,
            "",
            "not_feasible",
            0.0,
            reasons=[f"Unknown Grafana panel type: {panel_type}"],
        )
        return None, _enrich_panel_result(
            panel_result,
            panel=panel,
            datasource=datasource,
            query_language=query_language,
            notes=panel_notes,
            inventory=panel_inventory,
            yaml_panel=None,
        )

    grid = panel.get("gridPos", panel.get("gridData", {}))
    raw_w = grid.get("w", GRAFANA_GRID_COLS)
    raw_h = grid.get("h", 10)
    raw_x = grid.get("x", 0)
    raw_y = grid.get("y", 0)

    yaml_panel = {
        "title": title,
        "size": {"w": KIBANA_GRID_COLS, "h": KIBANA_DEFAULT_HEIGHT},
        "position": {"x": 0, "y": 0},
        "_grafana_row_y": raw_y,
        "_grafana_row_x": raw_x,
        "_grafana_w": raw_w,
        "_grafana_h": raw_h,
    }

    if panel_type == "text":
        content = _normalized_text_panel_content(panel)
        yaml_panel["markdown"] = {"content": content or "*(migrated text panel)*"}
        if not str(panel.get("title") or "").strip():
            yaml_panel["hide_title"] = True
        panel_result = PanelResult(title, panel_type, "markdown", "migrated", 1.0)
        return yaml_panel, _enrich_panel_result(
            panel_result,
            panel=panel,
            datasource=datasource,
            query_language="text",
            notes=panel_notes,
            inventory=panel_inventory,
            yaml_panel=yaml_panel,
        )

    if panel_analysis.get("mixed_datasource"):
        reasons = ["Mixed datasource or query-language panel targets require manual redesign"]
        yaml_panel["markdown"] = {
            "content": f"**Migration Required**\n\nReasons: {', '.join(reasons)}"
        }
        panel_result = PanelResult(title, panel_type, "markdown", "not_feasible", 0.0, reasons=reasons)
        return yaml_panel, _enrich_panel_result(
            panel_result,
            panel=panel,
            datasource=datasource,
            query_language=query_language,
            notes=panel_notes,
            inventory=panel_inventory,
            yaml_panel=yaml_panel,
        )

    targets = panel.get("targets", [])
    value_aliases = _panel_value_aliases(panel)
    hide_unmapped_values = panel_type in {"table", "table-old"} and bool(value_aliases) and _panel_hides_unmapped_values(panel)
    visible_targets = []
    for target in targets:
        query_text = target_query_text(target)
        if not query_text or target.get("hide"):
            continue
        ref_id = str(target.get("refId") or "").strip()
        if hide_unmapped_values and ref_id not in value_aliases:
            continue
        visible_targets.append((target, query_text))

    if query_language == "esql" and len(visible_targets) == 1:
        native_query = visible_targets[0][1]
        esql_mode = _infer_xy_stacking_mode(panel) if kibana_type in ("bar", "area") else None
        native_panel = _native_esql_panel_spec(native_query, kibana_type, mode=esql_mode)
        if native_panel:
            native_shape = _extract_esql_shape(native_query)
            native_panel_type = str(native_panel.get("type") or "")
            native_warnings = []
            if kibana_type == "pie" and native_panel_type != "pie":
                native_warnings.append(
                    "Approximated pie chart as bar chart because no categorical breakdown was available"
                )
            yaml_panel["esql"] = native_panel
            enrich_yaml_panel_display(yaml_panel, panel)
            query_ir = QueryIR()
            query_ir.source_language = "esql"
            query_ir.source_expression = native_query
            query_ir.clean_expression = native_query
            query_ir.panel_type = panel_type
            query_ir.datasource_type = datasource.get("type", "")
            query_ir.datasource_uid = datasource.get("uid", "")
            query_ir.datasource_name = datasource.get("name", "")
            query_ir.family = "native_esql"
            query_ir.output_group_fields = list(native_shape.group_fields)
            if native_shape.metric_fields:
                query_ir.output_metric_field = native_shape.metric_fields[0]
            elif len(native_shape.projected_fields) == 1:
                query_ir.output_metric_field = native_shape.projected_fields[0]
            query_ir.output_shape = infer_output_shape(panel_type, query_ir.output_group_fields, "esql")
            query_ir.target_index = _panel_query_index({"esql": {"query": native_query}})
            query_ir.target_query = native_query
            panel_result = PanelResult(
                title,
                panel_type,
                kibana_type,
                "migrated_with_warnings" if native_warnings else "migrated",
                0.7 if native_warnings else 1.0,
                reasons=native_warnings,
                promql_expr=native_query,
                esql_query=native_query,
            )
            return yaml_panel, _enrich_panel_result(
                panel_result,
                panel=panel,
                datasource=datasource,
                query_language="esql",
                notes=panel_notes,
                inventory=panel_inventory,
                query_ir=query_ir,
                yaml_panel=yaml_panel,
            )
        _append_unique(panel_notes, "Native ES|QL query detected but this panel type still needs manual mapping")

    if rule_pack.native_promql and query_language == "promql":
        native_result = _translate_panel_native_promql(
            panel, yaml_panel, title, panel_type, kibana_type,
            datasource, datasource_index, rule_pack, panel_notes, panel_inventory,
            query_language, visible_targets, resolver=resolver,
        )
        if native_result is not None:
            return native_result

    targets_with_expr = [(target, query_text) for target, query_text in visible_targets if target.get("expr")]
    promql_exprs = [target.get("expr", "") for target, _ in targets_with_expr]

    if not promql_exprs:
        if visible_targets:
            _append_unique(panel_notes, "Visible panel targets did not expose PromQL-compatible expressions")
        placeholder_panel, panel_result = _make_placeholder_panel(yaml_panel, title, panel_type, kibana_type)
        return placeholder_panel, _enrich_panel_result(
            panel_result,
            panel=panel,
            datasource=datasource,
            query_language=query_language,
            notes=panel_notes,
            inventory=panel_inventory,
            yaml_panel=placeholder_panel,
        )

    translations = []
    for idx, (target, _) in enumerate(targets_with_expr, start=1):
        expr = target.get("expr", "")
        negate_target = False
        stripped = expr.strip()
        if stripped.startswith("- ") or stripped.startswith("-\n") or (
            stripped.startswith("-") and len(stripped) > 1 and stripped[1] in "( "
        ):
            negate_target = True
            expr = stripped.lstrip("-").strip()
        target_datasource = normalize_datasource(target.get("datasource") or datasource)
        target_query_language = infer_query_language(expr, target_datasource.get("type", ""), panel_type)
        target_resolver = resolver
        if target_query_language == "logql":
            target_resolver = _resolver_for_index(resolver, rule_pack, rule_pack.logs_index)
        try:
            t = translate_promql_to_esql(
                expr,
                datasource_index=datasource_index,
                esql_index=esql_index,
                panel_type=panel_type,
                rule_pack=rule_pack,
                resolver=target_resolver,
                translation_hints=_target_translation_hints(panel, panel_type, target),
                datasource_type=target_datasource.get("type", ""),
                datasource_uid=target_datasource.get("uid", ""),
                datasource_name=target_datasource.get("name", ""),
                query_language=target_query_language,
                llm_endpoint=llm_endpoint,
                llm_model=llm_model,
                llm_api_key=llm_api_key,
            )
        except Exception as exc:
            t = TranslationContext(
                promql_expr=expr,
                data_view=datasource_index,
                index=esql_index or datasource_index,
                rule_pack=rule_pack or RulePackConfig(),
                resolver=target_resolver,
                panel_type=panel_type,
                clean_expr=expr,
            )
            t.feasibility = "not_feasible"
            t.warnings.append(f"Translation crashed: {type(exc).__name__}: {exc}")
        t.metadata["target_ref_id"] = target.get("refId") or f"series_{idx}"
        if negate_target:
            t.metadata["negate_result"] = True
        translations.append(t)

    if len(translations) > 1:
        all_source_exprs = [t.promql_expr for t in translations if getattr(t, "promql_expr", "")]
        all_clean_exprs = [t.clean_expr for t in translations if getattr(t, "clean_expr", "")]
        merged_source_expr = " ||| ".join(all_source_exprs)
        merged_clean_expr = " ||| ".join(all_clean_exprs)
        for translation in translations:
            if merged_source_expr:
                translation.promql_expr = merged_source_expr
            if merged_clean_expr:
                translation.clean_expr = merged_clean_expr

    feasible_translations = [t for t in translations if t.feasibility != "not_feasible" and t.esql_query]

    collapsed = _try_collapse_same_metric_targets(feasible_translations)
    if collapsed:
        feasible_translations = [collapsed]

    primary = feasible_translations[0] if feasible_translations else translations[0]
    fused_extra = []
    fused_series = [primary] if feasible_translations else []
    if len(feasible_translations) > 1:
        if panel_type in {"table", "table-old", "bargauge"}:
            fused_series = _best_compatible_translation_group(feasible_translations)
        elif kibana_type in ("line", "bar", "area"):
            fused_series = [primary]
            for et in feasible_translations[1:]:
                if _translations_compatible(*(fused_series + [et])):
                    fused_series.append(et)
        primary = fused_series[0]
        fused_extra = fused_series[1:]
        if len(fused_series) > 1:
            merged_query = _build_multi_target_series_query(fused_series)
            if merged_query:
                primary.esql_query = merged_query["query"]
                primary.source_type = merged_query["source_type"]
                primary.metadata["multi_series_metric_fields"] = merged_query["metric_fields"]
                primary.metadata["multi_series_metric_labels"] = merged_query.get("metric_label_hints", {})
                primary.output_metric_field = merged_query["metric_fields"][0]
                primary.output_group_fields = merged_query["group_fields"]
                for warning in merged_query["warnings"]:
                    _append_unique(primary.warnings, warning)
    primary.query_ir = build_query_ir(primary)

    migrated_refs = {
        t.metadata.get("target_ref_id")
        for t in fused_series
        if t.metadata.get("target_ref_id")
    }
    migrated_refs.update(primary.metadata.get("collapsed_target_refs", []) or [])
    migrated_target_count = max(len(migrated_refs), int(primary.metadata.get("collapsed_target_count", 0) or 0))
    dropped_count = len(targets_with_expr) - migrated_target_count
    if dropped_count > 0:
        dropped_exprs = [
            t.promql_expr
            for t in translations
            if t.promql_expr and t.metadata.get("target_ref_id") not in migrated_refs
        ]
        has_windows = any("windows_" in e for e in dropped_exprs)
        if migrated_target_count > 1:
            msg = f"Dropped {dropped_count} incompatible target(s); showing {migrated_target_count} mergeable targets"
            if has_windows:
                msg += " (dropped targets are Windows-specific)"
            _append_unique(primary.warnings, msg)
        elif migrated_target_count == 1:
            msg = f"Panel has {len(targets_with_expr)} PromQL targets but only 1 could be migrated"
            if has_windows:
                msg += " (dropped targets are Windows-specific)"
            _append_unique(primary.warnings, msg)

    if primary.feasibility == "not_feasible" or not primary.esql_query:
        if (
            rule_pack.native_promql
            and query_language == "promql"
            and len(targets_with_expr) > 1
        ):
            multi_result = _translate_multi_target_native_promql(
                panel, yaml_panel, title, panel_type, kibana_type,
                datasource, datasource_index, rule_pack, panel_notes,
                panel_inventory, targets_with_expr,
            )
            if multi_result is not None:
                return multi_result

        expr = promql_exprs[0]
        yaml_panel["markdown"] = {
            "content": f"**Migration Required**\n\nOriginal PromQL:\n```\n{expr}\n```\n\nReasons: {', '.join(primary.warnings)}"
        }
        panel_result = PanelResult(
            title,
            panel_type,
            "markdown",
            "not_feasible",
            0.0,
            reasons=primary.warnings,
            promql_expr=expr,
            trace=primary.trace,
            query_ir=primary.query_ir.to_dict() if primary.query_ir else {},
        )
        return yaml_panel, _enrich_panel_result(
            panel_result,
            panel=panel,
            datasource=datasource,
            query_language=query_language,
            notes=panel_notes,
            inventory=panel_inventory,
            query_ir=primary.query_ir,
            yaml_panel=yaml_panel,
            translation=primary,
            rule_pack=rule_pack,
        )

    panel_context = PanelContext(
        panel=panel,
        panel_type=panel_type,
        title=title,
        kibana_type=kibana_type,
        yaml_panel=yaml_panel,
        translation=primary,
        extra_translations=fused_extra,
    )
    PANEL_TRANSLATORS.apply(panel_context, stop_when=lambda ctx, _: ctx.handled)
    kibana_type = panel_context.kibana_type

    if primary.metadata.get("negate_result") and not fused_extra:
        metric_field = primary.output_metric_field
        if metric_field and primary.esql_query:
            negate_eval = f"| EVAL {metric_field} = -1 * {metric_field}"
            lines = primary.esql_query.split("\n")
            insert_idx = len(lines)
            for i, line in enumerate(lines):
                if line.strip().startswith("| SORT") or line.strip().startswith("| KEEP") or line.strip().startswith("| LIMIT"):
                    insert_idx = i
                    break
            lines.insert(insert_idx, negate_eval)
            primary.esql_query = "\n".join(lines)
            if yaml_panel.get("esql", {}).get("query"):
                yaml_panel["esql"]["query"] = primary.esql_query
            _append_unique(primary.warnings, "Applied negation to match leading minus in original expression")

    yaml_panel = _normalize_esql_panel_query(yaml_panel, primary.rule_pack)
    enrich_yaml_panel_display(
        yaml_panel,
        panel,
        metric_labels=primary.metadata.get("multi_series_metric_labels"),
    )
    if yaml_panel.get("esql", {}).get("query"):
        primary.esql_query = yaml_panel["esql"]["query"]
        primary.query_ir = build_query_ir(primary)
    panel_confidence = 0.85 if not primary.warnings else 0.6
    status = "migrated" if not primary.warnings else "migrated_with_warnings"

    all_exprs = " ||| ".join(promql_exprs) if len(promql_exprs) > 1 else promql_exprs[0]
    panel_result = PanelResult(
        title,
        panel_type,
        kibana_type,
        status,
        panel_confidence,
        reasons=primary.warnings,
        promql_expr=all_exprs,
        esql_query=primary.esql_query,
        trace=primary.trace + panel_context.trace,
    )
    return yaml_panel, _enrich_panel_result(
        panel_result,
        panel=panel,
        datasource=datasource,
        query_language=query_language,
        notes=panel_notes,
        inventory=panel_inventory,
        query_ir=primary.query_ir,
        yaml_panel=yaml_panel,
        translation=primary,
        rule_pack=rule_pack,
    )


def _try_collapse_same_metric_targets(translations):
    """Detect targets that share the same metric/agg but differ in one label value.

    Returns a single modified translation with that label added to the BY clause,
    or None if the pattern doesn't apply.
    """
    if len(translations) < 2:
        return None
    metrics = {t.metric_name for t in translations if t.metric_name}
    if len(metrics) != 1:
        return None
    inners = {t.inner_func for t in translations}
    outers = {t.outer_agg for t in translations}
    if len(inners) > 1 or len(outers) > 1:
        return None
    sources = {t.source_type for t in translations}
    if len(sources) > 1:
        return None
    if any(t.metadata.get("series_alias") != t.metadata.get("target_ref_id") for t in translations):
        return None

    frags = [t.fragment for t in translations]
    if not all(frags):
        return None
    supported_families = {"simple_metric", "simple_agg", "range_agg", "scaled_agg", "nested_agg"}
    if any(getattr(f, "family", "") not in supported_families for f in frags):
        return None

    matchers_per = [
        {(m["label"], m.get("op", "="), m["value"]) for m in f.matchers}
        for f in frags
    ]
    shared = matchers_per[0]
    for ms in matchers_per[1:]:
        shared = shared & ms
    diffs = [ms - shared for ms in matchers_per]
    # Permit any matcher operator (=, ==, =~, !=, !~) in the diffs. The
    # legacy implementation only allowed equality and bailed otherwise,
    # which silently dropped 5 of 6 targets on common Grafana panels like
    # Node Exporter Full's "CPU Basic" (mixed equality / regex / negated
    # ``mode`` matchers). For non-equality ops we add a unified
    # ``WHERE (op1 OR op2 OR ...)`` clause to the generated query below.
    diff_labels = set()
    nonequality_present = False
    for d in diffs:
        for label, op, _val in d:
            diff_labels.add(label)
            if op not in ("=", "=="):
                nonequality_present = True
    if len(diff_labels) != 1:
        return None
    # Refuse if any target has no distinguishing matcher (would mean
    # "match everything for this label", which can't be OR-folded with
    # the other targets' filters safely).
    if any(not d for d in diffs):
        return None

    collapse_label = diff_labels.pop()

    primary = translations[0]
    import copy
    collapsed = copy.deepcopy(primary)
    collapsed.fragment.matchers = [
        m for m in collapsed.fragment.matchers
        if m["label"] != collapse_label
    ]
    if collapse_label not in (collapsed.fragment.group_labels or []):
        collapsed.fragment.group_labels = list(collapsed.fragment.group_labels or []) + [collapse_label]
    if collapse_label not in (collapsed.group_labels or []):
        collapsed.group_labels = list(collapsed.group_labels or []) + [collapse_label]
    if collapse_label not in (collapsed.output_group_fields or []):
        collapsed.output_group_fields = list(collapsed.output_group_fields or []) + [collapse_label]

    plan = _build_formula_plan(
        collapsed.fragment,
        collapsed.resolver,
        collapsed.rule_pack,
        alias_hint=collapsed.metadata.get("target_ref_id") or "collapsed",
        summary_mode=_summary_mode_from_metadata(collapsed.metadata),
        preferred_group_labels=collapsed.metadata.get("preferred_group_labels"),
        preferred_group_labels_origin=collapsed.metadata.get("preferred_group_labels_origin"),
    )
    if not plan or not plan.specs:
        return None
    shared = _build_shared_measure_pipeline(collapsed.index, plan.specs)
    if not shared:
        return None
    parts, output_group_fields, metric_fields = shared

    # When the diffs include non-equality matchers, insert a unified
    # WHERE clause built from each target's distinguishing matchers
    # OR'd together. ``=`` collapses naturally because the BY column
    # alone splits series; ``=~`` / ``!=`` / ``!~`` need an explicit
    # filter to bound the result set.
    if nonequality_present:
        per_target_clauses = []
        seen_clauses: set[str] = set()
        for diff_set in diffs:
            collect = [
                _matcher_to_esql(
                    {"label": label, "op": op, "value": value},
                    collapsed.resolver,
                )
                for label, op, value in diff_set
                if label == collapse_label
            ]
            collect = [c for c in collect if c]
            if not collect:
                continue
            clause = collect[0] if len(collect) == 1 else "(" + " AND ".join(collect) + ")"
            if clause not in seen_clauses:
                seen_clauses.add(clause)
                per_target_clauses.append(clause)
        if per_target_clauses:
            if len(per_target_clauses) == 1:
                unified_where = f"| WHERE {per_target_clauses[0]}"
            else:
                unified_where = "| WHERE " + " OR ".join(per_target_clauses)
            # Insert the unified WHERE right after the source command
            # (line 0). Order is the same as other generated WHEREs:
            # source / time-filter / unified matcher OR / IS NOT NULL /
            # STATS.
            insert_at = 1
            for idx, part in enumerate(parts):
                if part.lstrip().startswith("| WHERE @timestamp"):
                    insert_at = idx + 1
                    break
            parts.insert(insert_at, unified_where)

    collapsed.source_type = plan.specs[0].source_type
    collapsed_summary = None
    if _summary_mode_from_metadata(collapsed.metadata):
        collapsed_summary = _collapse_summary_ts_query(parts, output_group_fields, metric_fields)
    if collapsed_summary is None:
        parts.append(f"| KEEP {', '.join(output_group_fields + metric_fields)}")
        if "time_bucket" in output_group_fields:
            parts.append("| SORT time_bucket ASC")
    else:
        output_group_fields = collapsed_summary
    collapsed.esql_query = "\n".join(parts)
    collapsed.output_group_fields = output_group_fields
    if metric_fields:
        collapsed.output_metric_field = metric_fields[0]
    collapsed.metadata["collapsed_target_count"] = len(translations)
    collapsed.metadata["collapsed_target_refs"] = [
        t.metadata.get("target_ref_id")
        for t in translations
        if t.metadata.get("target_ref_id")
    ]
    full_exprs = []
    for translation in translations:
        expr = getattr(translation, "promql_expr", "")
        if expr and expr not in full_exprs:
            full_exprs.append(expr)
    if full_exprs:
        collapsed.promql_expr = " ||| ".join(full_exprs)
    full_clean_exprs = []
    for translation in translations:
        expr = getattr(translation, "clean_expr", "")
        if expr and expr not in full_clean_exprs:
            full_clean_exprs.append(expr)
    if full_clean_exprs:
        collapsed.clean_expr = " ||| ".join(full_clean_exprs)

    for warning in plan.warnings:
        _append_unique(collapsed.warnings, warning)
    _append_unique(collapsed.warnings,
                   f"Collapsed {len(translations)} same-metric targets into BY {collapse_label}")
    return collapsed


def _build_multi_target_series_query(translations):
    if not translations:
        return None

    base = translations[0]
    plans = []
    all_specs = []
    warnings = []

    post_filters: dict[int, dict] = {}
    comp_ops = {"==": "==", "!=": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<="}
    for idx, translation in enumerate(translations, start=1):
        pf = None
        if translation.fragment and translation.fragment.extra.get("post_filter"):
            pf = translation.fragment.extra.pop("post_filter")
            post_filters[idx] = pf
        alias_hint = translation.metadata.get("target_ref_id") or f"series_{idx}"
        plan = _build_formula_plan(
            translation.fragment,
            translation.resolver,
            translation.rule_pack,
            alias_hint=alias_hint,
            summary_mode=_summary_mode_from_metadata(translation.metadata),
            preferred_group_labels=translation.metadata.get("preferred_group_labels"),
            allow_direct_ts_gauge=False,
            preferred_group_labels_origin=translation.metadata.get("preferred_group_labels_origin"),
        )
        if pf is not None:
            translation.fragment.extra["post_filter"] = pf
        if not plan or not plan.specs:
            return None
        plans.append((translation, plan))
        all_specs.extend(plan.specs)
        for warning in plan.warnings:
            if warning not in warnings:
                warnings.append(warning)

    shared = _build_shared_measure_pipeline(base.index, all_specs)
    if not shared:
        return None

    parts, output_group_fields, _ = shared
    metric_fields = []
    metric_label_hints: dict[str, str] = {}
    used_aliases = set()
    for idx, (translation, plan) in enumerate(plans, start=1):
        alias_hint = translation.metadata.get("target_ref_id") or f"series_{idx}"
        raw_alias = translation.metadata.get("series_alias") or translation.output_metric_field or translation.metric_name or "series"
        result_alias = _unique_safe_alias(
            raw_alias,
            used_aliases,
            fallback_suffix=alias_hint,
        )
        eval_expr = plan.expr
        if translation.metadata.get("negate_result"):
            eval_expr = f"(-1 * {plan.expr})"
        pf = post_filters.get(idx)
        if pf:
            esql_op = comp_ops.get(pf["op"], pf["op"])
            compare_value = _format_scalar_value(pf["value"])
            eval_expr = f"CASE({eval_expr} {esql_op} {compare_value}, {eval_expr}, NULL)"
        parts.append(f"| EVAL {result_alias} = {eval_expr}")
        metric_fields.append(result_alias)
        metric_label_hints[result_alias] = raw_alias

    summary_mode = all(_summary_mode_from_metadata(translation.metadata) for translation, _ in plans)
    collapsed = None
    if summary_mode and plans[0][1].specs:
        collapsed = _collapse_summary_ts_query(parts, output_group_fields, metric_fields)
    if collapsed is None:
        parts.append(f"| KEEP {', '.join(output_group_fields + metric_fields)}")
        if "time_bucket" in output_group_fields:
            parts.append("| SORT time_bucket ASC")
    else:
        output_group_fields = collapsed
    warnings.append("Merged compatible panel targets into a single ES|QL query")
    return {
        "query": "\n".join(parts),
        "metric_fields": metric_fields,
        "metric_label_hints": metric_label_hints,
        "group_fields": output_group_fields,
        "source_type": all_specs[0].source_type,
        "warnings": warnings,
    }


def _translations_compatible(*translations):
    """Check if translations can be fused into a single XY panel safely."""
    return _build_multi_target_series_query(list(translations)) is not None


def _best_compatible_translation_group(translations):
    if not translations:
        return []
    best_group = [0]
    best_score = (1, 0)
    for seed_idx in range(len(translations)):
        candidate = [seed_idx]
        for idx in range(len(translations)):
            if idx == seed_idx:
                continue
            merged = sorted(candidate + [idx])
            if _translations_compatible(*[translations[pos] for pos in merged]):
                candidate = merged
        score = (len(candidate), -sum(candidate))
        if score > best_score:
            best_group = candidate
            best_score = score
    return [translations[idx] for idx in best_group]


def _make_placeholder_panel(yaml_panel, title, panel_type, kibana_type):
    yaml_panel["markdown"] = {
        "content": f"**{title}**\n\n*(Placeholder: original {panel_type} panel had no PromQL targets)*"
    }
    return yaml_panel, PanelResult(
        title,
        panel_type,
        "markdown",
        "requires_manual",
        0.3,
        reasons=["No PromQL expression found in panel targets"],
    )


_extract_esql_shape = _extract_esql_shape_canonical
_extract_esql_columns = _extract_esql_columns_canonical


_TIME_DIMENSION_FIELDS = {"time_bucket", "timestamp_bucket", "step"}


def _dimension_field(field_name):
    dimension = {"field": field_name}
    if field_name in _TIME_DIMENSION_FIELDS:
        dimension["data_type"] = "date"
    return dimension


def _panel_field_defaults(panel):
    defaults = ((panel or {}).get("fieldConfig") or {}).get("defaults") or {}
    return defaults if isinstance(defaults, dict) else {}


def _coerce_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    return None


def _normalize_color(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    named = {
        "green": "#54B399",
        "red": "#E7664C",
        "orange": "#D6BF57",
        "yellow": "#D6BF57",
    }
    return named.get(lowered, text)


def _gauge_threshold_steps(panel):
    thresholds = _panel_field_defaults(panel).get("thresholds") or {}
    steps = thresholds.get("steps") if isinstance(thresholds, dict) else []
    steps = steps or []
    normalized = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        normalized.append(
            {
                "value": _coerce_number(step.get("value")),
                "color": _normalize_color(step.get("color")),
            }
        )
    return normalized


def _first_numeric_threshold(panel):
    for step in _gauge_threshold_steps(panel):
        value = step.get("value")
        if value is not None:
            return value
    return None


def _append_esql_constants(esql, constants):
    assignments = []
    for field_name, value in constants.items():
        number = _coerce_number(value)
        if number is None:
            continue
        assignments.append(f"{field_name} = {number}")
    if not assignments:
        return esql
    return f"{esql}\n| EVAL {', '.join(assignments)}"


def _build_gauge_color_mapping(panel, minimum=None, maximum=None):
    steps = sorted(
        _gauge_threshold_steps(panel),
        key=lambda step: float("-inf") if step.get("value") is None else step.get("value"),
    )
    if not steps:
        return None
    thresholds = []
    for index, step in enumerate(steps):
        color = step.get("color")
        if not color:
            continue
        current_value = step.get("value")
        if maximum is not None and current_value is not None and current_value >= maximum:
            continue
        next_value = None
        if index + 1 < len(steps):
            next_value = steps[index + 1].get("value")
        elif maximum is not None:
            next_value = maximum
        if next_value is None:
            continue
        if maximum is not None and next_value > maximum:
            next_value = maximum
        if minimum is not None and next_value <= minimum:
            continue
        if thresholds and next_value <= thresholds[-1]["up_to"]:
            continue
        thresholds.append({"up_to": next_value, "color": color})
    if not thresholds:
        return None
    color = {"thresholds": thresholds}
    if minimum is not None:
        color["range_min"] = minimum
    if maximum is not None:
        color["range_max"] = maximum
    return color


def _ensure_bucket_sort(esql):
    if not esql or esql.lstrip().startswith("PROMQL "):
        return esql
    upper_esql = esql.upper()
    if "BUCKET(" not in upper_esql and "TBUCKET(" not in upper_esql:
        return esql
    shape = _extract_esql_shape(esql)
    if not shape.time_fields:
        return esql
    time_field = shape.time_fields[0]
    lines = esql.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped.startswith("| KEEP "):
            continue
        keep_fields = [part.strip() for part in stripped[len("| KEEP "):].split(",") if part.strip()]
        if keep_fields and time_field not in keep_fields:
            return esql
        break
    asc_sort = f"| SORT {time_field} ASC"
    stripped_lines = [line.strip() for line in lines if line.strip()]
    if stripped_lines and stripped_lines[-1] == asc_sort:
        return esql
    lines.append(asc_sort)
    return "\n".join(lines)


def _strip_summary_bucket(esql):
    if not esql or "BUCKET(@timestamp" not in esql:
        return esql
    collapsed = re.sub(
        r"\s+BY\s+time_bucket\s*=\s*BUCKET\(@timestamp,\s*50,\s*\?_tstart,\s*\?_tend\)",
        "",
        esql,
        flags=re.MULTILINE | re.DOTALL,
    )
    lines = []
    for line in collapsed.splitlines():
        stripped = line.strip()
        if stripped in {"| SORT time_bucket ASC", "| SORT time_bucket DESC", "| LIMIT 1"}:
            continue
        if stripped.startswith("| KEEP time_bucket,"):
            line = line.replace("time_bucket, ", "", 1)
        elif stripped == "| KEEP time_bucket":
            continue
        lines.append(line)
    return "\n".join(lines)


def _restore_summary_time_bucket(esql):
    if not esql or "time_bucket" not in esql:
        return esql, False
    if "| LIMIT 1" not in esql and "LAST(" not in esql:
        return esql, False
    lines = esql.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("| KEEP "):
            continue
        keep_fields = [part.strip() for part in stripped[len("| KEEP "):].split(",") if part.strip()]
        if not keep_fields or "time_bucket" in keep_fields:
            return esql, False
        prefix = line[: line.index("| KEEP ")]
        lines[index] = f"{prefix}| KEEP time_bucket, {', '.join(keep_fields)}"
        return "\n".join(lines), True
    return esql, False


def _nested_mv_append_expr(items):
    values = [str(item) for item in items if str(item)]
    if not values:
        return '""'
    expr = values[0]
    for value in values[1:]:
        expr = f"MV_APPEND({expr}, {value})"
    return expr


def _build_summary_category_bar_query(esql, metric_fields, metric_label_hints=None):
    metric_fields = [field for field in (metric_fields or []) if field]
    if not esql or not metric_fields:
        return esql
    metric_label_hints = metric_label_hints or {}
    labels = [json.dumps(str(metric_label_hints.get(field, field) or field)) for field in metric_fields]
    value_terms = [f"TO_STRING({field})" for field in metric_fields]
    lines = esql.splitlines()
    lines.extend(
        [
            f"| EVAL __labels = {_nested_mv_append_expr(labels)}, __values = {_nested_mv_append_expr(value_terms)}",
            '| EVAL __pairs = MV_ZIP(__labels, __values, "~")',
            "| MV_EXPAND __pairs",
            '| EVAL label = MV_FIRST(SPLIT(__pairs, "~")), value = TO_DOUBLE(MV_LAST(SPLIT(__pairs, "~")))',
            "| KEEP label, value",
            "| SORT label ASC",
        ]
    )
    return "\n".join(lines)


def _strip_dashboard_timestamp_range_filter(esql, time_filters=None):
    if not esql:
        return esql
    removable_filters = {
        f"| WHERE {str(time_filter).strip()}"
        for time_filter in (time_filters or [])
        if str(time_filter).strip()
    }
    if not removable_filters:
        return str(esql)
    lines = [line for line in str(esql).splitlines() if line.strip() not in removable_filters]
    return "\n".join(lines)


def _normalize_esql_panel_query(yaml_panel, rule_pack=None):
    esql_panel = yaml_panel.get("esql")
    if not isinstance(esql_panel, dict):
        return yaml_panel
    query = esql_panel.get("query")
    if not query:
        return yaml_panel
    rule_pack = rule_pack or RulePackConfig()
    query = _strip_dashboard_timestamp_range_filter(
        query,
        [rule_pack.from_time_filter, rule_pack.ts_time_filter],
    )
    esql_panel["query"] = _ensure_bucket_sort(query)
    yaml_panel["esql"] = esql_panel
    return yaml_panel


def _build_esql_metric_panel(esql, metric_col=None):
    esql = _ensure_bucket_sort(esql)
    if not metric_col:
        metric_col, _ = _extract_esql_columns(esql)
    return {
        "type": "metric",
        "query": esql,
        "primary": {"field": metric_col},
    }


_COMPOSITE_LEGEND_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _resolve_legend_label_to_column(label, columns):
    """Map a ``legendFormat`` label name to an actual ES|QL output column.

    Tries the bare label name, then the ``prometheus.labels.<label>`` Fleet
    layout, then a generic ``labels.<label>`` fallback. Returns ``None`` when
    no candidate is in *columns*.
    """
    if not label:
        return None
    candidates = [
        label,
        f"prometheus.labels.{label}",
        f"labels.{label}",
    ]
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _extract_keep_columns(esql_query):
    """Return the column names from the **last** ``KEEP …`` pipeline stage.

    Returns ``[]`` when no ``KEEP`` stage is present. Operates on pipeline
    stages produced by :func:`_split_esql_pipeline` so the parser handles both
    multi-line (``| KEEP …`` on its own line) and inline single-line queries.
    """
    for stage in reversed(_split_esql_pipeline(esql_query)):
        body = str(stage or "").strip()
        if not body.lower().startswith("keep "):
            continue
        return [part.strip() for part in _split_top_level_csv(body[5:].strip()) if part.strip()]
    return []


def _output_columns_for_composite_legend(esql_query):
    """Return the best-effort set of output column names for the query.

    Combines the canonical shape extractor (which is robust for ``STATS …``
    queries) with a direct parse of the trailing ``KEEP`` line (which is the
    canonical XY shape used by the native-PROMQL path).
    """
    columns = set()
    metric_col, by_cols = _extract_esql_columns(esql_query)
    if metric_col:
        columns.add(metric_col)
    columns.update(by_cols or [])
    columns.update(_extract_keep_columns(esql_query))
    return columns


def _escape_esql_double_quoted_literal(text):
    """Escape backslashes and double quotes for an ES|QL double-quoted string."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _apply_composite_legend_to_xy_panel(yaml_panel, *,
                                        legend_format_template, legend_labels):
    """Rewrite an XY panel to break down by a synthetic ``legend`` column.

    Lens ``breakdown.field`` only supports a single column, so a Grafana panel
    with a multi-label legend like ``"{{ method }} {{ path }} - {{ status }}"``
    collapses to one series per ``method`` value unless we pre-compute a
    composite breakdown column. This helper:

    * Bails out when the template has fewer than 2 ``{{ label }}`` placeholders.
    * Resolves each label to an actual output column (bare, prefixed with
      ``prometheus.labels.``, or ``labels.``); bails out if any label fails.
    * Inserts ``| EVAL legend = CONCAT(...)`` before the final ``| KEEP`` and
      rewrites that ``KEEP`` to drop the now-redundant per-label columns.
    * Sets ``breakdown.field = "legend"``.

    Returns the panel either way; the panel is mutated in place.
    """
    if not legend_format_template:
        return yaml_panel
    template_labels = list(legend_labels or [])
    if len(template_labels) < 2:
        return yaml_panel
    esql = yaml_panel.get("esql")
    if not isinstance(esql, dict):
        return yaml_panel
    query = str(esql.get("query") or "")
    if not query.strip():
        return yaml_panel

    columns = _output_columns_for_composite_legend(query)
    resolved = {}
    for label in template_labels:
        column = _resolve_legend_label_to_column(label, columns)
        if column is None:
            return yaml_panel
        resolved[label] = column

    segments = _COMPOSITE_LEGEND_PLACEHOLDER_RE.split(legend_format_template)
    concat_args = []
    for index, segment in enumerate(segments):
        is_label = index % 2 == 1
        if is_label:
            column = resolved.get(segment)
            if column is None:
                return yaml_panel
            concat_args.append(f'COALESCE({column}, "")')
        else:
            if segment == "":
                continue
            concat_args.append(f'"{_escape_esql_double_quoted_literal(segment)}"')
    if not concat_args:
        return yaml_panel
    concat_expr = "CONCAT(" + ", ".join(concat_args) + ")"
    eval_line = f"| EVAL legend = {concat_expr}"

    label_columns = set(resolved.values())
    new_query = _splice_composite_legend_into_query(
        query, eval_line=eval_line, label_columns=label_columns,
    )
    esql["query"] = new_query
    esql["breakdown"] = {"field": "legend"}
    return yaml_panel


def _splice_composite_legend_into_query(query, *, eval_line, label_columns):
    """Insert *eval_line* immediately before the trailing ``KEEP`` and append
    ``legend`` to that ``KEEP`` while keeping the original per-label columns.

    Lens uses ``breakdown.field = "legend"`` to render one series per
    composite-label tuple and ignores the per-label columns; downstream
    consumers (parity harnesses, raw ES|QL drilldowns) still need the
    underlying labels to distinguish series whose ``legend`` strings
    collide. The ``label_columns`` parameter is accepted for backward
    compatibility but no longer drives column removal.

    When the query has no trailing ``KEEP`` stage (the canonical ``STATS …``
    form used by translated PromQL), the helper appends ``EVAL legend = …``
    only. No synthetic ``KEEP`` is added because that would silently drop
    the metric and time-bucket columns required by the XY panel shape.

    Handles both multi-line and inline single-line queries by operating on the
    pipeline stages.
    """
    pipeline_stages = _split_esql_pipeline(query)
    if not pipeline_stages:
        return query
    last_keep_index = None
    for idx in range(len(pipeline_stages) - 1, -1, -1):
        stage = pipeline_stages[idx].strip()
        if stage.lower().startswith("keep "):
            last_keep_index = idx
            break

    if last_keep_index is None:
        return _append_eval_before_trailing_sort(query, eval_line)

    keep_body = pipeline_stages[last_keep_index].strip()[5:].strip()
    existing = [part.strip() for part in _split_top_level_csv(keep_body) if part.strip()]
    # Keep the original label columns alongside ``legend``. Lens uses
    # ``breakdown.field = "legend"`` and ignores the other columns when
    # rendering, but downstream consumers (parity harnesses, raw-ESQL
    # readers, drilldown link generation) still need the underlying
    # labels to distinguish series. Previously we removed the per-label
    # columns and only emitted ``legend``, which made the output
    # ambiguous when two underlying tuples mapped to the same legend
    # string (e.g. when a status filter was unified into a WHERE OR).
    rewritten = list(existing)
    if "legend" not in rewritten:
        rewritten.append("legend")
    new_keep_stage = f"KEEP {', '.join(rewritten)}"

    is_multiline = "\n" in query
    if is_multiline:
        lines = query.splitlines()
        keep_line_index = None
        for idx in range(len(lines) - 1, -1, -1):
            stripped = lines[idx].strip()
            if stripped.startswith("|") and stripped[1:].strip().lower().startswith("keep "):
                keep_line_index = idx
                break
        if keep_line_index is not None:
            lines.insert(keep_line_index, eval_line)
            lines[keep_line_index + 1] = "| " + new_keep_stage
            return "\n".join(lines)

    rebuilt_stages = list(pipeline_stages)
    rebuilt_stages[last_keep_index] = new_keep_stage
    rebuilt_stages.insert(last_keep_index, eval_line.lstrip("|").strip())
    head = rebuilt_stages[0]
    tail = " | ".join(rebuilt_stages[1:]) if len(rebuilt_stages) > 1 else ""
    return f"{head} | {tail}" if tail else head


def _append_eval_before_trailing_sort(query, eval_line):
    """Append *eval_line* at the tail of *query*, but BEFORE a trailing ``SORT``.

    The translated ES|QL bodies frequently end with ``| SORT time_bucket ASC``
    so we want ``EVAL`` to sit before that to (a) keep the SORT semantically
    last and (b) avoid the downstream ``_ensure_bucket_sort`` appending a
    duplicate trailing SORT.
    """
    is_multiline = "\n" in query
    if is_multiline:
        lines = query.splitlines()
        sort_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            stripped = lines[idx].strip()
            if not stripped:
                continue
            if stripped.startswith("|") and stripped[1:].strip().lower().startswith("sort "):
                sort_idx = idx
            break
        if sort_idx is not None:
            lines.insert(sort_idx, eval_line)
            return "\n".join(lines)
        if query.endswith("\n"):
            return query + eval_line + "\n"
        return query + "\n" + eval_line
    stages = _split_esql_pipeline(query)
    if stages and stages[-1].strip().lower().startswith("sort "):
        stages.insert(len(stages) - 1, eval_line.lstrip("|").strip())
        head = stages[0]
        tail = " | ".join(stages[1:])
        return f"{head} | {tail}" if tail else head
    return query + " " + eval_line


def _build_esql_xy_panel(esql, chart_type, metric_col=None, by_cols=None,
                         time_fields=None, mode=None,
                         legend_format_template=None, legend_labels=None):
    esql = _ensure_bucket_sort(esql)
    shape = _extract_esql_shape(esql)
    extracted_metric_col, extracted_by_cols = _extract_esql_columns(esql)
    if metric_col is None:
        metric_col = extracted_metric_col
    if by_cols is None:
        by_cols = extracted_by_cols
    if time_fields is None:
        time_fields = shape.time_fields
    dimension_field, breakdown_field = _select_xy_dimension_fields(by_cols, time_fields=time_fields)
    panel = {
        "type": chart_type,
        "query": esql,
        "dimension": _dimension_field(dimension_field),
        "metrics": [{"field": metric_col}],
    }
    if chart_type in ("bar", "area") and mode:
        panel["mode"] = mode
    if breakdown_field:
        panel["breakdown"] = {"field": breakdown_field}
    if legend_format_template and legend_labels and len(legend_labels) >= 2:
        _apply_composite_legend_to_xy_panel(
            {"esql": panel},
            legend_format_template=legend_format_template,
            legend_labels=legend_labels,
        )
    return panel


def _build_esql_multi_series_xy(esql, chart_type, metric_fields, by_cols=None,
                                time_fields=None, mode=None,
                                legend_format_template=None, legend_labels=None):
    """Build an XY panel from a single merged ES|QL query."""
    esql = _ensure_bucket_sort(esql)
    shape = _extract_esql_shape(esql)
    _, extracted_by_cols = _extract_esql_columns(esql)
    if by_cols is None:
        by_cols = extracted_by_cols
    if time_fields is None:
        time_fields = shape.time_fields
    dimension_field, breakdown_field = _select_xy_dimension_fields(by_cols, time_fields=time_fields)
    panel = {
        "type": chart_type,
        "query": esql,
        "dimension": _dimension_field(dimension_field),
        "metrics": [{"field": metric} for metric in metric_fields],
    }
    if chart_type in ("bar", "area") and mode:
        panel["mode"] = mode
    if breakdown_field:
        panel["breakdown"] = {"field": breakdown_field}
    if legend_format_template and legend_labels and len(legend_labels) >= 2:
        _apply_composite_legend_to_xy_panel(
            {"esql": panel},
            legend_format_template=legend_format_template,
            legend_labels=legend_labels,
        )
    return panel


def _build_esql_gauge_panel(esql, metric_col=None, panel=None):
    if not metric_col:
        metric_col, _ = _extract_esql_columns(esql)
    defaults = _panel_field_defaults(panel)
    minimum = _coerce_number(defaults.get("min"))
    maximum = _coerce_number(defaults.get("max"))
    goal = _first_numeric_threshold(panel)
    constants = {
        "_gauge_min": minimum,
        "_gauge_max": maximum,
        "_gauge_goal": goal,
    }
    gauge = {
        "type": "gauge",
        "query": _ensure_bucket_sort(_append_esql_constants(esql, constants)),
        "metric": {"field": metric_col},
    }
    if panel:
        gauge["appearance"] = {"shape": "arc"}
    if minimum is not None:
        gauge["minimum"] = {"field": "_gauge_min"}
    if maximum is not None:
        gauge["maximum"] = {"field": "_gauge_max"}
    if goal is not None:
        gauge["goal"] = {"field": "_gauge_goal"}
    color = _build_gauge_color_mapping(panel, minimum=minimum, maximum=maximum)
    if color:
        gauge["color"] = color
    return gauge


def _build_esql_datatable_panel(esql, metric_col=None, metric_fields=None, by_cols=None):
    esql = _ensure_bucket_sort(esql)
    extracted_metric_col, extracted_by_cols = _extract_esql_columns(esql)
    if metric_col is None:
        metric_col = extracted_metric_col
    if by_cols is None:
        by_cols = extracted_by_cols
    if metric_fields is None:
        metric_fields = [metric_col]
    panel = {
        "type": "datatable",
        "query": esql,
        "metrics": [{"field": field_name} for field_name in metric_fields],
    }
    if by_cols:
        panel["breakdowns"] = [{"field": c} for c in by_cols]
    return panel


def _build_esql_pie_panel(esql, metric_col=None, by_cols=None):
    esql = _ensure_bucket_sort(esql)
    extracted_metric_col, extracted_by_cols = _extract_esql_columns(esql)
    if metric_col is None:
        metric_col = extracted_metric_col
    if by_cols is None:
        by_cols = extracted_by_cols
    breakdowns = [{"field": c} for c in by_cols if not _is_time_like_output_field(c)]
    if not breakdowns:
        return _build_esql_xy_panel(
            esql,
            "bar",
            metric_col=metric_col,
            by_cols=by_cols or ["time_bucket"],
        )
    panel = {
        "type": "pie",
        "query": esql,
        "metrics": [{"field": metric_col}],
    }
    panel["breakdowns"] = breakdowns
    return panel


def _variable_query_text(variable):
    query_text = variable.get("definition") or variable.get("query") or ""
    if isinstance(query_text, dict):
        query_text = query_text.get("query", "")
    return query_text if isinstance(query_text, str) else ""


def _extract_variable_source_field(query_text):
    query_text = (query_text or "").strip()
    match = re.match(r"^label_values\((?P<body>.+)\)$", query_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    parts = _split_top_level_csv(match.group("body"))
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].strip()
    return parts[-1].strip()


def _infer_controls_data_view(yaml_panels, datasource_index, rule_pack):
    indexes = {_panel_query_index(panel) for panel in yaml_panels if _panel_query_index(panel)}
    if indexes == {rule_pack.logs_index}:
        return rule_pack.logs_index
    metrics_indexes = {idx for idx in indexes if idx and idx != rule_pack.logs_index}
    if len(metrics_indexes) == 1:
        return next(iter(metrics_indexes))
    return datasource_index


def _infer_dashboard_filters(yaml_panels, rule_pack):
    """Decide what dashboard-level filters to emit.

    The historical design auto-added a ``data_stream.dataset`` ``match_phrase``
    filter (defaulting to the literal ``"prometheus"``) as a safety net when
    panels queried the broad ``metrics-*`` pattern: it kept the
    multi-backend ``metrics-*`` view scoped to the Prometheus dataset only.

    That safety net is destructive when:

    * Every panel already targets a narrow concrete index (e.g. the migration
      ran with ``--esql-index metrics-prometheus.remote_write-express``).
      Adding a literal-``prometheus`` filter on top of a narrow Fleet
      ``prometheus.remote_write`` data stream filters out **all** documents
      because ``data_stream.dataset`` is the constant_keyword
      ``"prometheus.remote_write"``, not ``"prometheus"``.
    * The user explicitly disabled the filter via ``--dataset-filter ""`` —
      already honored.

    Skip the filter when none of the panel ESQL index patterns contain a
    wildcard, since the index pattern is itself the constraint and adding an
    unrelated literal filter is strictly harmful.
    """
    indexes = {_panel_query_index(panel) for panel in yaml_panels if _panel_query_index(panel)}
    if not indexes:
        return []
    if indexes == {rule_pack.logs_index}:
        if not rule_pack.logs_dataset_filter:
            return []
        if not _has_wildcard_index(indexes):
            return []
        return [{"field": "data_stream.dataset", "equals": rule_pack.logs_dataset_filter}]
    if rule_pack.logs_index in indexes:
        return []
    if not rule_pack.metrics_dataset_filter:
        return []
    if not _has_wildcard_index(indexes):
        return []
    return [{"field": "data_stream.dataset", "equals": rule_pack.metrics_dataset_filter}]


def _has_wildcard_index(indexes):
    return any(any(token in idx for token in ("*", "?", ",")) for idx in indexes if idx)


def _field_control_type(field_name, resolver):
    if not resolver or not field_name:
        return "options"
    assessment = assess_field_usage(
        resolver.field_capability(field_name),
        field_name=field_name,
        display_name=field_name,
        usage="filter",
    )
    if not assessment.exists or assessment.capability is None or assessment.capability.conflicting_types:
        return "options"
    return "range" if assessment.capability.type_family == "numeric" else "options"


MIN_DATATABLE_HEIGHT = 5


def _normalize_tile_size(panel, kibana_type):
    size = dict(panel.get("size", {}))
    width = int(size.get("w", 0) or 0)
    height = int(size.get("h", 0) or 0)
    actual_type = (panel.get("esql") or {}).get("type", kibana_type)
    if kibana_type == "metric" and 0 < width < MIN_PANEL_WIDTH:
        size["w"] = MIN_PANEL_WIDTH
    if actual_type == "datatable" and 0 < height < MIN_DATATABLE_HEIGHT:
        size["h"] = MIN_DATATABLE_HEIGHT
    panel["size"] = size
    position = dict(panel.get("position", {}))
    max_x = KIBANA_GRID_COLS - int(size.get("w", 0) or 0)
    if max_x < 0:
        max_x = 0
    position["x"] = min(int(position.get("x", 0) or 0), max_x)
    panel["position"] = position
    return panel


def _dashboard_output_stem(title):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", (title or "").lower())[:60]


def _resolver_for_index(resolver, rule_pack, index_pattern):
    if not resolver or not index_pattern:
        return resolver
    if getattr(resolver, "_index_pattern", "") == index_pattern:
        return resolver
    es_url = getattr(resolver, "_es_url", "")
    if not es_url:
        return resolver
    cache = getattr(resolver, "_alternate_resolvers", None)
    if cache is None:
        cache = {}
        setattr(resolver, "_alternate_resolvers", cache)
    if index_pattern not in cache:
        cache[index_pattern] = SchemaResolver(
            rule_pack or RulePackConfig(),
            es_url=es_url,
            index_pattern=index_pattern,
            es_api_key=getattr(resolver, "_es_api_key", None),
        )
    return cache[index_pattern]


@VARIABLE_TRANSLATORS.register("query_variable", priority=10)
def query_variable_rule(context):
    if context.variable.get("type") != "query":
        return None
    if context.variable.get("hide"):
        return f"skipped hidden variable {context.variable.get('name', '')}"

    resolver = context.resolver
    name = context.variable.get("name", "")
    label = context.variable.get("label", name)
    query_text = context.query_text or _variable_query_text(context.variable)
    context.query_text = query_text
    if "query_result(" in query_text.lower():
        return f"skipped query_result helper variable {name}"
    source_field = _extract_variable_source_field(query_text) or name
    context.source_field = source_field

    if resolver:
        field_name = resolver.resolve_control_field(source_field)
    else:
        rule_pack = context.rule_pack or RulePackConfig()
        field_name = rule_pack.control_field_overrides.get(source_field, source_field)
    if field_name is None:
        return f"skipped unsupported control {name}"
    if resolver and resolver.field_exists(field_name) is False:
        return f"skipped unavailable control field {field_name}"
    control_type = _field_control_type(field_name, resolver)
    context.control = {
        "type": control_type,
        "label": label or name,
        "data_view": context.data_view,
        "field": field_name,
    }
    if control_type == "options":
        if name in context.repeat_variable_names:
            # Repeated Grafana panels cannot be preserved literally in Kibana,
            # so we force the driver control to a single selection to avoid
            # collapsing multiple repeated instances into one misleading panel.
            context.control["multiple"] = False
        else:
            context.control["multiple"] = bool(context.variable.get("multi"))
    context.handled = True
    return f"translated variable {name}"


@VARIABLE_TRANSLATORS.register("textbox_variable", priority=20)
def textbox_variable_rule(context):
    """Grafana textbox variables have no direct Kibana control equivalent.

    The built-in Kibana query bar or KQL filters serve the same purpose.
    We record the variable metadata so the migration report reflects it
    rather than silently dropping it.
    """
    if context.variable.get("type") != "textbox":
        return None
    name = context.variable.get("name", "")
    context.handled = True
    context.trace.append(
        f"textbox variable '{name}' has no direct Kibana control equivalent; "
        "use the Kibana query bar or KQL filter instead"
    )
    return f"noted textbox variable {name} (no Kibana control equivalent)"


@VARIABLE_TRANSLATORS.register("interval_variable", priority=25)
def interval_variable_rule(context):
    """Grafana interval and custom-interval variables are handled by Kibana's
    time picker and auto-bucketing; no explicit control is needed."""
    var_type = context.variable.get("type", "")
    if var_type not in ("interval", "custom"):
        return None
    name = context.variable.get("name", "")
    context.handled = True
    return f"skipped {var_type} variable {name} (handled by Kibana time picker)"


def translate_variables(
    template_list,
    datasource_index="metrics-*",
    rule_pack=None,
    resolver=None,
    repeat_variable_names=None,
):
    rule_pack = rule_pack or RulePackConfig()
    controls = []
    for var in template_list:
        context = VariableContext(
            variable=var,
            data_view=datasource_index,
            resolver=resolver,
            rule_pack=rule_pack,
            query_text=_variable_query_text(var),
            repeat_variable_names=set(repeat_variable_names or ()),
        )
        VARIABLE_TRANSLATORS.apply(context, stop_when=lambda ctx, _: ctx.handled)
        if context.control:
            controls.append(context.control)
    return controls


def _panel_sort_key(panel):
    grid = panel.get("gridPos", panel.get("gridData", {})) or {}
    return (
        int(grid.get("y", 0) or 0),
        int(grid.get("x", 0) or 0),
        int(panel.get("id", 0) or 0),
    )


def _flatten_dashboard_panels(dashboard):
    all_panels = []
    for panel in dashboard.get("panels", []):
        all_panels.append(panel)
        for sub_panel in panel.get("panels", []):
            all_panels.append(sub_panel)
    for row in dashboard.get("rows", []):
        for panel in row.get("panels", []):
            all_panels.append(panel)
    return sorted(all_panels, key=_panel_sort_key)


def _build_section_groups(dashboard):
    """Group Grafana panels by their parent row.

    Returns a list of ``(row_title | None, [panel, ...])``.
    Panels before the first row form a group with ``row_title=None``.
    Collapsed rows carry their children in ``panel["panels"]``.
    """
    groups: list[tuple[str | None, list[dict]]] = []
    current_title: str | None = None
    current_panels: list[dict] = []

    top_level = dashboard.get("panels", [])
    for panel in sorted(top_level, key=_panel_sort_key):
        if panel.get("type") == "row":
            if current_panels or groups:
                groups.append((current_title, current_panels))
            current_title = str(panel.get("title") or "").strip() or None
            current_panels = list(panel.get("panels", []))
        else:
            current_panels.append(panel)

    for row in dashboard.get("rows", []):
        row_title = str(row.get("title") or "").strip() or None
        row_panels = row.get("panels", [])
        if not row_panels:
            continue
        row_height_px = row.get("height", 250)
        if isinstance(row_height_px, str):
            row_height_px = int("".join(c for c in row_height_px if c.isdigit()) or "250")
        grid_h = max(row_height_px // 30, 4)
        patched: list[dict] = []
        x_cursor = 0
        for rp in row_panels:
            enriched = dict(rp)
            enriched["_legacy_row"] = True
            if rp.get("gridPos"):
                enriched["gridPos"] = dict(rp.get("gridPos") or {})
                patched.append(enriched)
                continue
            span = int(rp.get("span", 12) or 12)
            w = span * 2
            enriched["gridPos"] = {"x": x_cursor, "y": 0, "w": w, "h": grid_h}
            x_cursor += w
            if x_cursor >= GRAFANA_GRID_COLS:
                x_cursor = 0
            patched.append(enriched)
        groups.append((row_title, patched))

    if current_panels or not groups:
        groups.append((current_title, current_panels))

    return groups


def _repeat_variable_name(value):
    if not isinstance(value, str):
        return ""
    return value.strip()


def _collect_repeat_variable_names(dashboard):
    repeat_variables: set[str] = set()
    for panel in _flatten_dashboard_panels(dashboard):
        repeat_name = _repeat_variable_name(panel.get("repeat"))
        if repeat_name:
            repeat_variables.add(repeat_name)
    for panel in dashboard.get("panels", []):
        if panel.get("type") != "row":
            continue
        repeat_name = _repeat_variable_name(panel.get("repeat"))
        if repeat_name:
            repeat_variables.add(repeat_name)
    for row in dashboard.get("rows", []):
        repeat_name = _repeat_variable_name(row.get("repeat"))
        if repeat_name:
            repeat_variables.add(repeat_name)
    return repeat_variables


_DROPPED_VARS_WARNING = "Dropped variable-driven label filters during migration"
_DROPPED_LOGQL_LABEL_WARNING = "Dropped variable-driven LogQL label filters during migration"
_DROPPED_LOGQL_TEXT_WARNING = "Dropped variable-driven LogQL text filter during migration"
_CONTROLS_VARS_WARNING = (
    "Variable-driven label filters applied via Kibana dashboard controls"
)
_CONTROLS_LOGQL_LABEL_WARNING = (
    "Variable-driven LogQL label filters applied via Kibana dashboard controls"
)
_CONTROLS_LOGQL_TEXT_WARNING = (
    "Variable-driven LogQL text filter applied via Kibana dashboard controls"
)


def _pre_scan_control_variables(template_list):
    """Return the set of variable names that will become Kibana controls.

    A variable becomes a control when it is ``type == "query"`` and not hidden.
    This mirrors the logic in ``query_variable_rule``.
    """
    names: set[str] = set()
    for var in template_list:
        if var.get("type") == "query" and not var.get("hide"):
            name = var.get("name", "")
            if name:
                names.add(name)
    return names


_WARNING_REWRITE_MAP = {
    _DROPPED_VARS_WARNING: _CONTROLS_VARS_WARNING,
    _DROPPED_LOGQL_LABEL_WARNING: _CONTROLS_LOGQL_LABEL_WARNING,
    _DROPPED_LOGQL_TEXT_WARNING: _CONTROLS_LOGQL_TEXT_WARNING,
}


def _rewrite_variable_warnings(panel_results, control_variable_names):
    """Replace 'Dropped variable-driven …' with a controls-aware message.

    ``PanelResult.reasons`` carries the translation warnings.
    """
    if not control_variable_names:
        return
    for pr in panel_results:
        for i, w in enumerate(pr.reasons):
            replacement = _WARNING_REWRITE_MAP.get(w)
            if replacement:
                pr.reasons[i] = replacement


def _normalized_text_panel_content(panel):
    text_options = panel.get("options", {}) or {}
    content = text_options.get("content", "")
    if not content:
        content = panel.get("content", "")
    mode = text_options.get("mode") or panel.get("mode", "")
    return _normalize_text_panel_content(content, mode)


def _is_decorative_repeat_header_panel(panel):
    if panel.get("type") != "text":
        return False
    if not _repeat_variable_name(panel.get("repeat")):
        return False
    if _normalized_text_panel_content(panel).strip():
        return False
    cleaned_title = clean_template_variables(str(panel.get("title") or "")).strip()
    return not cleaned_title


def _is_placeholder_section_title(title):
    cleaned = clean_template_variables(str(title or "")).strip()
    if not cleaned:
        return True
    return cleaned.casefold() in _PLACEHOLDER_SECTION_TITLES


def _build_normalization_skip_result(panel, reason):
    title = str(panel.get("title") or panel.get("type") or "panel").strip() or "panel"
    datasource = normalize_datasource(panel.get("datasource"))
    panel_result = PanelResult(
        title,
        str(panel.get("type") or ""),
        "markdown",
        "skipped",
        1.0,
        reasons=[reason],
    )
    return _enrich_panel_result(
        panel_result,
        panel=panel,
        datasource=datasource,
        query_language="text" if panel.get("type") == "text" else "",
        notes=collect_panel_notes(panel),
        inventory=collect_panel_inventory(panel),
        yaml_panel=None,
    )


def _normalize_panel_group(row_title, group_panels):
    retained_panels: list[dict] = []
    skipped_panel_results: list[PanelResult] = []
    for panel in sorted(group_panels, key=_panel_sort_key):
        if _is_decorative_repeat_header_panel(panel):
            skipped_panel_results.append(
                _build_normalization_skip_result(
                    panel,
                    "Dropped decorative repeat header panel; repeated Grafana context is represented through dashboard controls instead.",
                )
            )
            continue
        retained_panels.append(panel)

    cleaned_title = clean_template_variables(str(row_title or "")).strip() or None
    legacy_row = any(bool(panel.get("_legacy_row")) for panel in group_panels)
    should_flatten = cleaned_title is None
    if _is_placeholder_section_title(row_title) or (legacy_row and len(retained_panels) <= 1):
        should_flatten = True
    elif len(retained_panels) == 1 and cleaned_title:
        child_title = clean_template_variables(str(retained_panels[0].get("title") or "")).strip()
        if not child_title:
            child_title = str(retained_panels[0].get("title") or "").strip()
        if child_title and child_title.casefold() == cleaned_title.casefold():
            should_flatten = True

    return NormalizedPanelGroup(
        title=None if should_flatten else cleaned_title,
        panels=retained_panels,
        skipped_panel_results=skipped_panel_results,
    )


def _panel_group_height(yaml_panels):
    if not yaml_panels:
        return 0
    return max(
        int(panel.get("position", {}).get("y", 0) or 0)
        + int(panel.get("size", {}).get("h", 0) or 0)
        for panel in yaml_panels
    )


def _offset_yaml_panels(yaml_panels, *, y_offset):
    if not y_offset:
        return yaml_panels
    for panel in yaml_panels:
        position = dict(panel.get("position", {}))
        position["y"] = int(position.get("y", 0) or 0) + y_offset
        panel["position"] = position
    return yaml_panels


def _restore_flattened_legacy_panel_titles(yaml_panels):
    for panel in yaml_panels:
        if panel.get("hide_title") is not True:
            continue
        esql = panel.get("esql")
        if not isinstance(esql, dict):
            continue
        chart_type = str(esql.get("type") or "")
        title = str(panel.get("title") or "").strip()
        if not title or chart_type not in {"metric", "gauge"}:
            continue
        panel.pop("hide_title", None)
        if chart_type == "metric":
            primary = esql.get("primary")
            if isinstance(primary, dict) and primary.get("label") == title:
                primary.pop("label", None)
        elif chart_type == "gauge":
            metric = esql.get("metric")
            if isinstance(metric, dict) and metric.get("label") == title:
                metric.pop("label", None)
    return yaml_panels


def _kibana_panel_type(yaml_panel):
    """Return the effective Kibana visualization type for layout purposes."""
    return (
        (yaml_panel.get("esql") or {}).get("type")
        or ("markdown" if "markdown" in yaml_panel else "metric")
    )


def _apply_kibana_native_layout(yaml_panels):
    """Assign Kibana-native sizes and positions to a group of panels.

    Uses the ``_grafana_row_y`` / ``_grafana_row_x`` metadata tags set during
    translation to detect which panels belong to the same visual row, then
    distributes them across the 48-column Kibana grid with
    type-appropriate heights.

    **L1 universal layout (the "faithful coordinate transform")**: when
    every panel carries the original Grafana geometry
    (``_grafana_w`` and ``_grafana_h`` are both set) we scale each
    panel's ``(x, y, w, h)`` independently and shift the whole group
    so the topmost panel sits at Kibana y=0. This preserves the
    *relative* vertical spacing that the Grafana author chose
    (a 9-row gap stays a ~14-row gap in Kibana after the 30/20 row
    scale), instead of stacking every Grafana y-band sequentially
    with a cumulative y-cursor.

    Scale factors:

    * Column scale = ``KIBANA_GRID_COLS / GRAFANA_GRID_COLS = 48/24 = 2``
    * Row scale    = ``GRAFANA_ROW_HEIGHT_PX / KIBANA_ROW_HEIGHT_PX = 30/20 = 1.5``

    When some panels lack original geometry (legacy schema 14 row
    panels, dashboards built before this metadata was tagged) we fall
    back to the even-distribution path which keeps panels sequential
    with a y-cursor. This is the "best effort" branch and will go
    away with L3 (row-aware sectioning).
    """
    if not yaml_panels:
        return yaml_panels

    has_original_geometry = all(
        panel.get("_grafana_w") is not None
        and panel.get("_grafana_h") is not None
        for panel in yaml_panels
    )

    if has_original_geometry:
        _apply_faithful_coordinate_transform(yaml_panels)
    else:
        _apply_even_distribution_fallback(yaml_panels)

    for panel in yaml_panels:
        panel.pop("_grafana_row_y", None)
        panel.pop("_grafana_row_x", None)
        panel.pop("_grafana_w", None)
        panel.pop("_grafana_h", None)
        _normalize_tile_size(panel, _kibana_panel_type(panel))

    return yaml_panels


def _apply_faithful_coordinate_transform(yaml_panels):
    """L1: scale each panel's Grafana coords independently and shift
    the group so the topmost panel sits at Kibana y=0.

    See :func:`_apply_kibana_native_layout` for the rationale and
    scale factors. This function assumes every panel has
    ``_grafana_w`` and ``_grafana_h``; callers route to
    :func:`_apply_even_distribution_fallback` otherwise.

    Edge alignment: rather than scaling ``y`` and ``h`` independently
    (which lets rounding errors introduce 1-row overlaps between
    panels that are exactly touching in Grafana, eg.
    ``y=25,h=6`` immediately followed by ``y=31,h=4``), we scale
    the *top* and the *bottom* of each panel and derive the height
    from their difference. This guarantees that touching Grafana
    panels remain touching (not overlapping) in Kibana, which the
    downstream ``kb-dashboard-cli`` compile step refuses.

    We use round-half-up (``int(x + 0.5)``) instead of Python's
    default banker's rounding (``round(0.5) == 0``). Banker's rounding
    silently strips half-rows from panel heights when the scaled
    bottom edge lands on ``.5``, which over time eats into the
    minimum tile heights downstream code assumes.
    """
    col_scale = KIBANA_GRID_COLS / GRAFANA_GRID_COLS
    row_scale = GRAFANA_ROW_HEIGHT_PX / KIBANA_ROW_HEIGHT_PX

    def half_up(value: float) -> int:
        return int(value + 0.5)

    # First pass: compute every panel's absolute Kibana coords and
    # remember the minimum scaled y so we can normalise.
    scaled: list[tuple[dict, int, int, int, int]] = []
    min_y = None
    for panel in yaml_panels:
        gy = int(panel.get("_grafana_row_y", 0) or 0)
        gx = int(panel.get("_grafana_row_x", 0) or 0)
        raw_w = int(
            panel.get("_grafana_w", GRAFANA_GRID_COLS) or GRAFANA_GRID_COLS
        )
        raw_h = int(
            panel.get("_grafana_h", KIBANA_DEFAULT_HEIGHT)
            or KIBANA_DEFAULT_HEIGHT
        )
        # Scale the right and bottom edges, then derive width/height
        # from the difference so adjacent panels stay adjacent.
        kx = half_up(gx * col_scale)
        kx_right = half_up((gx + raw_w) * col_scale)
        ky = half_up(gy * row_scale)
        ky_bottom = half_up((gy + raw_h) * row_scale)
        kw = max(1, kx_right - kx)
        kh = max(1, ky_bottom - ky)
        scaled.append((panel, kx, ky, kw, kh))
        if min_y is None or ky < min_y:
            min_y = ky

    shift_y = -(min_y or 0)
    for panel, kx, ky, kw, kh in scaled:
        panel["size"] = {"w": kw, "h": kh}
        panel["position"] = {"x": kx, "y": ky + shift_y}


def _apply_even_distribution_fallback(yaml_panels):
    """Best-effort layout for panels without original Grafana
    geometry. Groups by ``_grafana_row_y`` and distributes each band's
    panels evenly across the 48-col grid, stacking bands with a
    y-cursor.

    This is the only path that still uses cumulative y-cursor banding;
    L3 (row-aware sectioning) is expected to eliminate the need for
    this branch by always tagging panels with original geometry.
    """
    rows: dict[int, list[dict]] = {}
    for panel in yaml_panels:
        gy = panel.get("_grafana_row_y", 0)
        rows.setdefault(gy, []).append(panel)

    y_cursor = 0
    for grafana_y in sorted(rows):
        row_panels = rows[grafana_y]
        row_panels.sort(key=lambda p: p.get("_grafana_row_x", 0))
        n = len(row_panels)
        row_height = max(
            KIBANA_TYPE_HEIGHT.get(_kibana_panel_type(p), KIBANA_DEFAULT_HEIGHT)
            for p in row_panels
        )
        base_w = KIBANA_GRID_COLS // n
        remainder = KIBANA_GRID_COLS - base_w * n
        x_cursor = 0
        for i, panel in enumerate(row_panels):
            pw = base_w + (1 if i < remainder else 0)
            panel["size"] = {"w": pw, "h": row_height}
            panel["position"] = {"x": x_cursor, "y": y_cursor}
            x_cursor += pw
        y_cursor += row_height


def _panel_bounds(yaml_panel):
    position = yaml_panel.get("position", {})
    size = yaml_panel.get("size", {})
    x = int(position.get("x", 0) or 0)
    y = int(position.get("y", 0) or 0)
    w = int(size.get("w", 0) or 0)
    h = int(size.get("h", 0) or 0)
    return x, y, w, h


def _panels_overlap(left, right):
    lx, ly, lw, lh = _panel_bounds(left)
    rx, ry, rw, rh = _panel_bounds(right)
    return lx < rx + rw and lx + lw > rx and ly < ry + rh and ly + lh > ry


def _resolve_panel_overlaps(yaml_panels):
    placed = []
    for original_index, panel in sorted(
        enumerate(yaml_panels),
        key=lambda entry: (
            int(entry[1].get("position", {}).get("y", 0) or 0),
            int(entry[1].get("position", {}).get("x", 0) or 0),
            str(entry[1].get("title", "")),
        ),
    ):
        panel = dict(panel)
        panel["position"] = dict(panel.get("position", {}))
        panel["size"] = dict(panel.get("size", {}))
        while True:
            overlaps = [other_panel for _, other_panel in placed if _panels_overlap(panel, other_panel)]
            if not overlaps:
                break
            panel["position"]["y"] = max(
                int(other["position"].get("y", 0) or 0) + int(other["size"].get("h", 0) or 0)
                for other in overlaps
            )
        placed.append((original_index, panel))
    return [panel for _, panel in sorted(placed, key=lambda entry: entry[0])]


def _translate_panel_group(
    panels,
    *,
    datasource_index,
    esql_index,
    rule_pack,
    resolver,
    result,
    llm_endpoint="",
    llm_model="",
    llm_api_key="",
):
    """Translate a group of Grafana panels, returning (yaml_panels, panel_results)."""
    yaml_panels: list[dict] = []
    panel_results: list[PanelResult] = []

    if not panels:
        return yaml_panels, panel_results

    sorted_panels = sorted(panels, key=_panel_sort_key)

    for panel in sorted_panels:
        yaml_panel, panel_result = translate_panel(
            panel,
            datasource_index=datasource_index,
            esql_index=esql_index,
            rule_pack=rule_pack,
            resolver=resolver,
            llm_endpoint=llm_endpoint,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
        )
        result.panel_results.append(panel_result)
        panel_result.operational_ir = build_operational_ir(
            panel_result,
            dashboard_title=result.dashboard_title,
            dashboard_uid=result.dashboard_uid,
            source_file=result.source_file,
            folder_title=result.folder_title,
        )

        if panel_result.status == "skipped":
            result.skipped += 1
            continue
        elif panel_result.status == "migrated":
            result.migrated += 1
        elif panel_result.status == "migrated_with_warnings":
            result.migrated_with_warnings += 1
        elif panel_result.status == "requires_manual":
            result.requires_manual += 1
        elif panel_result.status == "not_feasible":
            result.not_feasible += 1

        if yaml_panel:
            _sync_visual_ir(panel_result, yaml_panel)
            yaml_panels.append(yaml_panel)
            panel_results.append(panel_result)

    yaml_panels = _apply_kibana_native_layout(yaml_panels)
    for yp, pr in zip(yaml_panels, panel_results):
        _sync_visual_ir(pr, yp)

    return yaml_panels, panel_results


def translate_dashboard(dashboard, output_dir, datasource_index="metrics-*", esql_index=None, rule_pack=None, resolver=None,
                        llm_endpoint="", llm_model="", llm_api_key=""):
    rule_pack = rule_pack or RulePackConfig()
    title = dashboard.get("title", "Untitled Dashboard")
    uid = dashboard.get("uid", "unknown")
    description = dashboard.get("description", "") or f"Migrated from Grafana ({uid})"

    result = MigrationResult(
        dashboard_title=title,
        dashboard_uid=uid,
        source_file=str(dashboard.get("_source_file") or ""),
        folder_title=str((dashboard.get("_grafana_meta") or {}).get("folderTitle") or ""),
        inventory=build_dashboard_inventory(dashboard),
    )

    all_panels = _flatten_dashboard_panels(dashboard)
    result.total_panels = len(all_panels)

    variables = dashboard.get("templating", {}).get("list", [])
    control_variable_names = _pre_scan_control_variables(variables)

    section_groups = _build_section_groups(dashboard)
    repeat_variable_names = _collect_repeat_variable_names(dashboard)
    top_level_panels: list[dict] = []
    dashboard_y_cursor = 0

    for panel in all_panels:
        if panel.get("type") == "row":
            row_pr = PanelResult(
                str(panel.get("title") or "row"), "row", "section", "skipped", 1.0
            )
            result.panel_results.append(row_pr)
            result.skipped += 1

    used_section_titles: dict[str, int] = {}
    for row_title, group_panels in section_groups:
        normalized_group = _normalize_panel_group(row_title, group_panels)
        legacy_group = any(bool(panel.get("_legacy_row")) for panel in group_panels)
        for panel_result in normalized_group.skipped_panel_results:
            panel_result.operational_ir = build_operational_ir(
                panel_result,
                dashboard_title=result.dashboard_title,
                dashboard_uid=result.dashboard_uid,
                source_file=result.source_file,
                folder_title=result.folder_title,
            )
            result.panel_results.append(panel_result)
            result.skipped += 1
        if not normalized_group.panels:
            continue

        translated, panel_results = _translate_panel_group(
            normalized_group.panels,
            datasource_index=datasource_index,
            esql_index=esql_index,
            rule_pack=rule_pack,
            resolver=resolver,
            result=result,
            llm_endpoint=llm_endpoint,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
        )
        result.yaml_panel_results.extend(panel_results)

        if not translated:
            continue

        if legacy_group and normalized_group.title is None:
            _restore_flattened_legacy_panel_titles(translated)
        group_height = _panel_group_height(translated)
        if normalized_group.title:
            cleaned = clean_template_variables(normalized_group.title) or normalized_group.title
            count = used_section_titles.get(cleaned, 0) + 1
            used_section_titles[cleaned] = count
            unique_title = f"{cleaned} ({count})" if count > 1 else cleaned
            section_panel = {
                "title": unique_title,
                "section": {
                    "collapsed": False,
                    "panels": translated,
                },
            }
            top_level_panels.append(section_panel)
        else:
            _offset_yaml_panels(translated, y_offset=dashboard_y_cursor)
            top_level_panels.extend(translated)
        dashboard_y_cursor += group_height

    flat_panels: list[dict] = []
    for panel in top_level_panels:
        if "section" in panel:
            for inner in panel["section"].get("panels", []):
                flat_panels.append(inner)
        else:
            flat_panels.append(panel)

    _rewrite_variable_warnings(result.panel_results, control_variable_names)

    controls_data_view = _infer_controls_data_view(flat_panels, datasource_index, rule_pack)
    controls_resolver = _resolver_for_index(resolver, rule_pack, controls_data_view)
    controls = translate_variables(
        variables,
        controls_data_view,
        rule_pack=rule_pack,
        resolver=controls_resolver,
        repeat_variable_names=repeat_variable_names,
    )

    yaml_doc = {
        "dashboards": [
            {
                "name": title,
                "description": description,
                "minimum_kibana_version": MINIMUM_KIBANA_VERSION,
                "settings": {"sync": {"cursor": True}},
                "panels": top_level_panels,
            }
        ]
    }

    filters = _infer_dashboard_filters(flat_panels, rule_pack)
    if filters:
        yaml_doc["dashboards"][0]["filters"] = filters
    if controls:
        yaml_doc["dashboards"][0]["controls"] = controls

    apply_style_guide_layout(yaml_doc)

    safe_name = _dashboard_output_stem(title)
    output_path = Path(output_dir) / f"{safe_name}.yaml"
    with open(output_path, "w") as f:
        yaml.dump(yaml_doc, f, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120)

    return result, output_path


__all__ = [
    "PANEL_TYPE_MAP",
    "SKIP_PANEL_TYPES",
    "PanelContext",
    "VariableContext",
    "_dashboard_output_stem",
    "query_variable_rule",
    "translate_dashboard",
    "translate_panel",
    "translate_variables",
]
