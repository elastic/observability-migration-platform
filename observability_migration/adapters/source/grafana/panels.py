# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Panel, variable, and dashboard translation helpers."""

from __future__ import annotations

import copy
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
from observability_migration.targets.kibana.emit.display import (
    clean_template_variables,
    enrich_yaml_panel_display,
    grafana_unit_to_yaml_format,
)
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
from observability_migration.targets.kibana.emit.layout import (
    PANEL_SIZE_CONSTRAINTS as _TYPE_SIZE_CONSTRAINTS,
)
from observability_migration.targets.kibana.emit.layout import (
    apply_style_guide_layout,
)

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
    _ESQL_RESERVED_IDENTIFIERS,
    _build_formula_plan,
    _build_shared_measure_pipeline,
    _collapse_summary_ts_query,
    _format_scalar_value,
    _matcher_to_esql,
    _parse_fragment,
    _split_top_level_csv,
    _summary_mode_from_metadata,
    _unique_safe_alias,
    grafana_template_var_name,
    substitute_grafana_range_macros,
)
from .rules import PANEL_TRANSLATORS, VARIABLE_TRANSLATORS, RulePackConfig, _append_unique
from .runtime_features import (
    PROMQL_LABEL_MATCHER_PARAMS,
    binds_esql_named_params,
    is_feature_supported,
)
from .schema import SchemaResolver
from .series_labels import (
    _metrics_in_expr,
    build_metric_series_labels,
    expr_has_explicit_grouping,
)
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
    "grafana-piechart-panel": "pie",  # community plugin alias for built-in piechart
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
    "metric": 6,    # aligned to _TYPE_SIZE_CONSTRAINTS min_h=6
    "gauge": 8,     # aligned to _TYPE_SIZE_CONSTRAINTS min_h=8
    "bargauge": 6,  # aligned to _TYPE_SIZE_CONSTRAINTS min_h=6
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
    # L3: set when the normaliser decided this group should NOT be
    # emitted as a section even though it came from an explicit row
    # (eg. legacy single-panel rows where a 1-panel section would be
    # visual clutter; placeholder titles like "New Row" / "Row").
    # Defaults to False so callers default to the L3 "always section
    # for explicit rows" behaviour unless this overrides it.
    force_flatten: bool = False


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


def _target_translation_hints(panel, panel_type, target, metric_series_labels=None):
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
    if (
        isinstance(legend_template, str)
        and legend_template.strip()
        and legend_template.strip() != "__auto"
        and not legend_labels
    ):
        hints["static_legend_label"] = legend_template.strip()
    if isinstance(legend_template, str) and len(legend_labels) >= 2:
        hints["legend_format_template"] = legend_template

    # Offline backfill: when the panel named NO series labels of its own, recover them
    # from the dashboard-wide per-metric label map (other panels' by()/filters, template
    # variables). Tagged "dashboard_inferred" so the inference is auditable.
    #
    # Skip single-value panels (stat/gauge/bargauge/piechart -> summary_mode): they
    # intentionally render one current value, so adding an inferred breakdown would change
    # the panel's type/intent. Their own explicit legend/by() labels still apply above.
    #
    # Also skip panels whose own expression already carries an explicit by()/without()
    # clause: that grouping is authoritative and the translator honors it directly, so
    # the dashboard-wide union must not overwrite it (issue #94).
    if (
        not summary_mode
        and not preferred_group_labels
        and metric_series_labels
        and not expr_has_explicit_grouping(target.get("expr", ""))
    ):
        inferred = _inferred_labels_for_target(target, metric_series_labels)
        if inferred:
            hints["preferred_group_labels"] = inferred
            hints["preferred_group_labels_origin"] = "dashboard_inferred"
    return hints


def _inferred_labels_for_target(target, metric_series_labels):
    """Look up a target's metric in the dashboard-wide series-label map."""
    expr = str(target.get("expr", "") or "")
    for metric in _metrics_in_expr(expr):
        labels = metric_series_labels.get(metric)
        if labels:
            return list(labels)
    return []


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


def _promql_grouping_has_template_variable(expr):
    stripped = _strip_promql_string_literals(expr)
    return bool(
        re.search(
            rf"\b(?:by|without)\s*\([^)]*{_GRAFANA_VAR_TOKEN_PATTERN}[^)]*\)",
            stripped,
            re.IGNORECASE,
        )
    )


def _promql_label_matcher_has_template_variable(expr):
    return bool(
        re.search(
            rf"(?P<op>=~|!~|=|!=)(?P<quote>[\"'])\s*{_GRAFANA_VAR_TOKEN_PATTERN}\s*(?P=quote)",
            str(expr or ""),
        )
    )


_NATIVE_PROMQL_LABEL_MATCHER_RE = re.compile(
    r"(?P<label>\s*[A-Za-z_][A-Za-z0-9_\.:-]*\s*)"
    r"(?P<op>=~|!~|=|!=)(?P<ws>\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)(?P<suffix>\s*)$",
    re.DOTALL,
)


def _promql_label_matcher_vars_to_params(expr, regex_default_params=None):
    """Rewrite full-value Grafana label matcher variables to native params.

    ``regex_default_params`` names the variables whose binding control defaults
    to the regex match-all (".*"). Exact-equality matchers (``label="$var"``)
    on those variables are loosened to a regex match (``label=~?var``) so the
    ".*" default matches every series on first load instead of comparing the
    label against the literal string ".*" (PR #133 review). This mirrors
    Grafana auto-rewriting ``label="$var"`` to ``label=~"..."`` for All/multi
    variables and matches the ES|QL path's ``_matcher_to_esql`` handling.
    """
    regex_default_params = regex_default_params or frozenset()

    def rewrite_selector(selector_text):
        parts = []
        changed = False
        for part in _split_top_level_csv(selector_text):
            matcher = _NATIVE_PROMQL_LABEL_MATCHER_RE.match(part)
            if not matcher:
                parts.append(part)
                continue
            var_name = grafana_template_var_name(matcher.group("value"))
            if not var_name or var_name.startswith("__"):
                parts.append(part)
                continue
            op = matcher.group("op")
            if op == "=" and var_name in regex_default_params:
                op = "=~"
            parts.append(
                f"{matcher.group('label')}{op}{matcher.group('ws')}"
                f"?{var_name}{matcher.group('suffix')}"
            )
            changed = True
        return ", ".join(parts) if changed else selector_text

    pieces = []
    start = 0
    idx = 0
    text = str(expr or "")
    while idx < len(text):
        if text[idx] != "{":
            idx += 1
            continue
        pieces.append(text[start:idx])
        end = idx + 1
        depth = 1
        in_quote = None
        while end < len(text) and depth:
            char = text[end]
            if in_quote:
                if char == in_quote and text[end - 1] != "\\":
                    in_quote = None
            elif char in ('"', "'"):
                in_quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            end += 1
        if depth:
            pieces.append(text[idx:])
            return "".join(pieces)
        selector = text[idx + 1:end - 1]
        pieces.append("{" + rewrite_selector(selector) + "}")
        start = end
        idx = end
    pieces.append(text[start:])
    return "".join(pieces)


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


def _clean_promql_for_native_with_state(
    expr, runtime_features=None, regex_default_params=None
):
    """Strip Grafana template variables from a PromQL expression so it can be
    sent to the ES PROMQL engine which does not know about ``$var`` syntax."""
    had_bare_variable = False
    expr = substitute_grafana_range_macros(expr)
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

    if is_feature_supported(runtime_features, PROMQL_LABEL_MATCHER_PARAMS):
        expr = _promql_label_matcher_vars_to_params(expr, regex_default_params)

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

    # Normalize histogram boundary label values: some Prometheus exporters
    # store le as "1.0" / "10.0" while Grafana dashboards write le="1" / "10".
    # Rewrite bare-integer le matchers to the float form so the native PROMQL
    # engine finds the data that was actually scraped.
    expr = re.sub(r'\ble=("|\')(\d+)\1', lambda m: f'le={m.group(1)}{m.group(2)}.0{m.group(1)}', expr)

    expr = re.sub(r"\s+", " ", expr).strip()

    return expr, had_bare_variable


def _clean_promql_for_native(expr, runtime_features=None, regex_default_params=None):
    cleaned, _ = _clean_promql_for_native_with_state(
        expr,
        runtime_features=runtime_features,
        regex_default_params=regex_default_params,
    )
    return cleaned


def _extract_legend_labels(legend_format):
    """Parse ``{{label}}`` placeholders from a Grafana legendFormat string."""
    if not legend_format or legend_format in ("__auto", ""):
        return []
    return re.findall(r"\{\{\s*(\w+)\s*\}\}", legend_format)


def _static_legend_label(legend_format):
    if not legend_format or legend_format in ("__auto", ""):
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
                              legend_format=None, runtime_features=None,
                              instant=False, regex_default_params=None):
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
    if not can_use_native_promql(promql_expr, runtime_features=runtime_features):
        raise ValueError("PromQL expression is not supported by the native PROMQL path")
    cleaned = _clean_promql_for_native(
        promql_expr,
        runtime_features=runtime_features,
        regex_default_params=regex_default_params,
    )

    # An instant query evaluates the expression at a single point (the Kibana
    # time-picker end, ``?_tend``) and returns one row per series = the current
    # value, with NO ``step`` time column. A range query walks ``step=`` buckets
    # and emits a ``step`` column to plot against. Single-value tiles
    # (metric/gauge) and table-format ``instant: true`` targets are instant
    # (issues #127, #102); everything else is a range plot. ``time=?_tend`` is
    # opt-in: callers that post-process the ``step`` column (e.g. the alert
    # ``LAST(value, step)`` reduction) leave ``instant`` False to keep ``step=``.
    selector = "time=?_tend" if instant else f"step={_DEFAULT_NATIVE_PROMQL_STEP}"

    if kibana_type in ("metric", "gauge"):
        return f'PROMQL index={index} {selector} value=({cleaned})'

    base = f'PROMQL index={index} {selector} value=({cleaned})'

    _, group_cols = _native_promql_result_shape(promql_expr)
    if "_timeseries" not in group_cols:
        return base

    # The ``step`` column only exists on range queries; an instant query must
    # not KEEP it (referencing a column the command never emits is a 400).
    value_cols = ["value"] if instant else ["step", "value"]

    if legend_labels:
        # Extract each series label from the native ``_timeseries`` JSON with a
        # single GROK scan per label. GROK reads the string once, so this stays
        # linear in the blob size; the previous ``REPLACE(_ts, """.*"k":"..."",
        # "$1")`` chains backtracked over the whole blob (with leading/trailing
        # ``.*``) and a full-blob ``REPLACE(REPLACE(...))`` fallback per row,
        # which degraded super-linearly on wide label sets. A label absent from a
        # given series yields NULL (correct: that series has no such dimension).
        evals = [_grok_label_extraction(lbl) for lbl in legend_labels]
        keep = value_cols + [_esql_identifier(lbl) for lbl in legend_labels]
        return base + "\n" + "\n".join(evals) + f'\n| KEEP {", ".join(keep)}'

    static_label = (legend_format or "").strip()
    if static_label and static_label != "__auto":
        # Static legend text (no placeholders) — emit it verbatim as the
        # series label so Lens uses the author's chosen name.
        # Skip Grafana's "__auto" sentinel — it means "derive automatically"
        # and must not appear as a literal string in the ES|QL output.
        escaped = _escape_esql_double_quoted_literal(static_label)
        keep = value_cols + ["label"]
        return (
            base
            + f'\n| EVAL label = "{escaped}"'
            + f'\n| KEEP {", ".join(keep)}'
        )

    # Neither placeholders nor static legend text — drop the synthetic
    # label column entirely. Lens then renders one unlabeled series,
    # matching Grafana's behaviour for an empty legendFormat. Previously
    # we emitted ``EVAL label = CASE(_ts == "", "series", REPLACE(...))``
    # which dumped the stringified label tuple as the legend, an ugly
    # regression spotted in NEF screenshots.
    return base


def can_use_native_promql(promql_expr, runtime_features=None):
    """Return True if the expression is within the server-supported PromQL subset."""
    if not promql_expr or not promql_expr.strip():
        return False
    if (
        _promql_label_matcher_has_template_variable(promql_expr)
        and not is_feature_supported(runtime_features, PROMQL_LABEL_MATCHER_PARAMS)
    ):
        return False
    if _promql_grouping_has_template_variable(promql_expr):
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
    runtime_features = getattr(rule_pack, "runtime_features", {})
    if not can_use_native_promql(expr, runtime_features=runtime_features):
        if (
            _promql_label_matcher_has_template_variable(expr)
            and not is_feature_supported(runtime_features, PROMQL_LABEL_MATCHER_PARAMS)
        ):
            _append_unique(
                panel_notes,
                "Native PROMQL skipped: target does not support PromQL label matcher params yet",
            )
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
    legend_format = target.get("legendFormat", "")
    legend_labels = _extract_legend_labels(legend_format)

    index = datasource_index or "metrics-prometheus-*"
    regex_default_params = getattr(rule_pack, "_regex_default_param_names", frozenset())
    cleaned_expr, had_bare_variable = _clean_promql_for_native_with_state(
        expr,
        runtime_features=runtime_features,
        regex_default_params=regex_default_params,
    )
    _, group_cols = _native_promql_result_shape(expr)
    if kibana_type in ("metric", "gauge") and group_cols:
        return None
    # Emit an instant (``time=?_tend``) query when the source target is one:
    # single-value tiles, or a ``instant: true`` table-format target (issue
    # #102). ``_target_summary_mode`` already encodes that policy for the ES|QL
    # path, so reuse it for parity; ``kibana_type in (metric, gauge)`` keeps the
    # existing single-value behavior even when the panel type doesn't map there.
    #
    # But never let an instant query reach an XY (line/bar/area) spec: those bind
    # the x-axis to the ``step`` time column, which an instant query does NOT emit
    # (phantom axis / 400 — the #127 failure mode). ``_target_summary_mode``
    # returns True unconditionally for ``bargauge`` (→ ``bar``), so without this
    # guard a Prometheus ``bargauge`` panel would regress to a broken bar chart.
    instant = kibana_type in ("metric", "gauge") or (
        _target_summary_mode(panel_type, target)
        and kibana_type not in ("line", "bar", "area")
    )
    promql_query = build_native_promql_query(expr, index=index,
                                             legend_labels=legend_labels,
                                             kibana_type=kibana_type,
                                             legend_format=legend_format,
                                             runtime_features=runtime_features,
                                             instant=instant,
                                             regex_default_params=regex_default_params)
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
        # Record the *emitted* panel query, not the bare ``PROMQL …`` command.
        # Gauge/metric native panels append a trailing ``| EVAL _gauge_*`` (or
        # other constants) to ``native_panel["query"]`` after
        # ``build_native_promql_query`` returns; recording the bare command here
        # let the validate-stage ``sync_result_queries_to_yaml`` overwrite the
        # YAML query and strip those columns, orphaning the gauge min/max/goal
        # accessors (issue #109). ``query_ir.target_query`` stays bare for the
        # parity oracle.
        esql_query=native_panel.get("query", promql_query),
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
        runtime_features = getattr(rule_pack, "runtime_features", {})
        if not can_use_native_promql(expr, runtime_features=runtime_features):
            if (
                _promql_label_matcher_has_template_variable(expr)
                and not is_feature_supported(runtime_features, PROMQL_LABEL_MATCHER_PARAMS)
            ):
                _append_unique(
                    panel_notes,
                    "Native PROMQL skipped: target does not support PromQL label matcher params yet",
                )
            return None
        cleaned, bare = _clean_promql_for_native_with_state(
            expr,
            runtime_features=runtime_features,
            regex_default_params=getattr(
                rule_pack, "_regex_default_param_names", frozenset()
            ),
        )
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
            warnings=primary.warnings,
        )
        context.kibana_type = "bar"
        _append_unique(context.translation.warnings, "Approximated bargauge as bar chart")
    elif primary.output_group_fields:
        context.yaml_panel["esql"] = _build_esql_xy_panel(
            primary.esql_query,
            "bar",
            metric_col=primary.output_metric_field or None,
            by_cols=primary.output_group_fields,
            warnings=primary.warnings,
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
            warnings=primary.warnings,
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
            warnings=primary.warnings,
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
        warnings=primary.warnings,
    )
    emitted_type = context.yaml_panel["esql"].get("type", "line")
    if emitted_type == "line":
        _append_unique(
            primary.warnings,
            f"Approximated as line chart (no direct {context.kibana_type} mapping)",
        )
    context.handled = True
    return f"fell back to {emitted_type} panel"


def translate_panel(panel, datasource_index="metrics-*", esql_index=None, rule_pack=None, resolver=None,
                    llm_endpoint="", llm_model="", llm_api_key="", metric_series_labels=None):
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
                translation_hints=_target_translation_hints(panel, panel_type, target, metric_series_labels),
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
        # Keep the target's own expression: ``promql_expr`` is overwritten with
        # the merged " ||| " join below, but per-target provenance (and the
        # parity oracle that consumes it) needs the original sub-query.
        t.metadata["target_source_expr"] = expr
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
                primary.metadata["collapsed_targets"] = merged_query.get("targets", [])
                primary.output_metric_field = merged_query["metric_fields"][0]
                primary.output_group_fields = merged_query["group_fields"]
                for warning in merged_query["warnings"]:
                    _append_unique(primary.warnings, warning)
    if (
        len(targets_with_expr) > 1
        and len(fused_series) == 1
        and feasible_translations
        and not primary.metadata.get("collapsed_targets")
        and not primary.metadata.get("multi_series_metric_fields")
        and primary.esql_query
    ):
        # Fusion kept only the primary target: the translated query IS that
        # target's translation, so the parity oracle can verify it whole.
        # The dropped siblings are recorded as explicitly unverifiable so
        # they surface as reasoned SKIP rows instead of hiding inside the
        # joined source_query.
        primary_ref = primary.metadata.get("target_ref_id") or ""
        unfused_provenance: list[dict[str, object]] = [{
            "ref_id": primary_ref,
            "source_expr": str(primary.metadata.get("target_source_expr") or ""),
            "whole_translated": True,
        }]
        for t in translations:
            ref = t.metadata.get("target_ref_id") or ""
            if ref and ref != primary_ref:
                unfused_provenance.append({
                    "ref_id": ref,
                    "source_expr": str(t.metadata.get("target_source_expr") or ""),
                    "unsupported_reason": (
                        "target was not migrated; the translated query covers "
                        "the primary target only"
                    ),
                })
        primary.metadata["collapsed_targets"] = unfused_provenance
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
    metric_labels = dict(primary.metadata.get("multi_series_metric_labels") or {})
    static_legend_label = primary.metadata.get("static_legend_label")
    if static_legend_label and primary.output_metric_field:
        metric_labels.setdefault(primary.output_metric_field, static_legend_label)
    enrich_yaml_panel_display(
        yaml_panel,
        panel,
        metric_labels=metric_labels or None,
    )
    _apply_series_override_axes(yaml_panel, panel, primary.warnings)
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
        parts.append("| KEEP " + ", ".join(dict.fromkeys(output_group_fields + metric_fields)))
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
    # Per-target provenance for the parity oracle. Unlike the formula merge
    # (one output column per target), this collapse maps each target to a
    # VALUE of the BY column, so verification scopes the translated response
    # by (label_column, label_value). Non-equality matchers (regex / negated)
    # would require re-implementing matcher semantics client-side - a
    # false-verdict risk - so those targets carry an explicit
    # unsupported_reason instead.
    target_provenance = []
    for translation, diff_set in zip(translations, diffs):
        entry = {
            "ref_id": translation.metadata.get("target_ref_id") or "",
            "source_expr": str(translation.metadata.get("target_source_expr") or ""),
        }
        equality = [
            (label, op, value)
            for label, op, value in diff_set
            if label == collapse_label and op in ("=", "==")
        ]
        if len(diff_set) == 1 and len(equality) == 1:
            entry["label_column"] = collapse_label
            entry["label_value"] = equality[0][2]
        else:
            entry["unsupported_reason"] = (
                "distinguishing matcher is non-equality or compound; "
                "per-target comparison is not supported"
            )
        target_provenance.append(entry)
    collapsed.metadata["collapsed_targets"] = target_provenance
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
    post_filters: dict[int, dict] = {}
    comp_ops = {"==": "==", "!=": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<="}

    def _build_plans(allow_tsds_gauge_promotion):
        plans = []
        all_specs = []
        warnings = []
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
                allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
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
        return plans, all_specs, warnings

    built = _build_plans(allow_tsds_gauge_promotion=True)
    if built is None:
        return None
    plans, all_specs, warnings = built

    # When targets resolve to mixed source commands (e.g. an uptime/`MAX` target stays
    # FROM while an assumed-TSDS gauge target promotes to TS), the shared pipeline can't
    # fuse them. Rebuild once with gauge->TS promotion disabled so every target shares the
    # common FROM denominator. Mirrors the binary-expr reconciliation in
    # ``_build_formula_plan``. Fused multiplicity-invariant aggregators (AVG/MAX/MIN) are
    # correct on FROM; non-idempotent ones keep TS when not mixed.
    if len({spec.source_type for spec in all_specs}) > 1:
        rebuilt = _build_plans(allow_tsds_gauge_promotion=False)
        if rebuilt is not None and len({spec.source_type for spec in rebuilt[1]}) == 1:
            plans, all_specs, warnings = rebuilt

    shared = _build_shared_measure_pipeline(base.index, all_specs)
    if not shared:
        return None

    parts, output_group_fields, _ = shared
    metric_fields = []
    metric_label_hints: dict[str, str] = {}
    target_provenance: list[dict[str, str]] = []
    used_aliases = set()
    for idx, (translation, plan) in enumerate(plans, start=1):
        alias_hint = translation.metadata.get("target_ref_id") or f"series_{idx}"
        raw_alias = translation.metadata.get("series_alias") or translation.output_metric_field or translation.metric_name or "series"
        result_alias = _unique_safe_alias(
            raw_alias,
            used_aliases,
            fallback_suffix=alias_hint,
        )
        provenance_entry = {
            "ref_id": alias_hint,
            "source_expr": str(translation.metadata.get("target_source_expr") or ""),
            "value_column": result_alias,
        }
        if translation.metadata.get("negate_result"):
            provenance_entry["negated"] = True
        target_provenance.append(provenance_entry)
        eval_expr = plan.expr
        if translation.metadata.get("negate_result"):
            eval_expr = f"(-1 * {plan.expr})"
        pf = post_filters.get(idx)
        if pf:
            esql_op = comp_ops.get(pf["op"], pf["op"])
            compare_value = _format_scalar_value(pf["value"])
            eval_expr = f"CASE({eval_expr} {esql_op} {compare_value}, {eval_expr}, NULL)"
        # ``result_alias`` may be a legend-derived token that collides with an
        # ES|QL reserved word (e.g. "IN"); quote it for the query text but keep
        # the bare name in ``metric_fields``/hints for Kibana column matching.
        parts.append(f"| EVAL {_esql_identifier(result_alias)} = {eval_expr}")
        metric_fields.append(result_alias)
        metric_label_hints[result_alias] = raw_alias

    summary_mode = all(_summary_mode_from_metadata(translation.metadata) for translation, _ in plans)
    collapsed = None
    if summary_mode and plans[0][1].specs:
        collapsed = _collapse_summary_ts_query(parts, output_group_fields, metric_fields)
    if collapsed is None:
        parts.append(
            "| KEEP "
            + ", ".join(
                _esql_identifier(f)
                for f in dict.fromkeys(output_group_fields + metric_fields)
            )
        )
        if "time_bucket" in output_group_fields:
            parts.append("| SORT time_bucket ASC")
    else:
        output_group_fields = collapsed
    return {
        "query": "\n".join(parts),
        "metric_fields": metric_fields,
        "metric_label_hints": metric_label_hints,
        "group_fields": output_group_fields,
        "source_type": all_specs[0].source_type,
        "warnings": warnings,
        "targets": target_provenance,
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


# Regex metacharacters that must be escaped when a literal label name is spliced
# into the constant prefix of a GROK pattern (GROK patterns are regex-based).
_GROK_LITERAL_ESCAPE_RE = re.compile(r'([.^$*+?()\[\]{}|\\])')


def _esql_identifier(name):
    """Quote an ES|QL column identifier with backticks only when needed.

    Bare alphanumeric/underscore names are emitted as-is (matching prior output);
    names with dots or other special characters are backtick-quoted so they are
    valid in ``EVAL`` targets and ``KEEP`` lists. Tokens that collide with an
    ES|QL reserved keyword (e.g. a legendFormat of ``IN``/``BY``) are also
    quoted, otherwise ES|QL rejects ``EVAL IN = ...`` with ``mismatched input``.
    """
    text = str(name)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text) and text.lower() not in _ESQL_RESERVED_IDENTIFIERS:
        return text
    return "`" + text.replace("`", "``") + "`"


def _grok_label_extraction(label):
    """Emit a GROK pipe that pulls a single PromQL series label out of the
    native ``_timeseries`` JSON string.

    The label appears in the blob as ``"<label>":"<value>"``; GROK reads the
    string once and binds ``<value>`` to a column named after the label. When the
    label is not present on a series the column is NULL.
    """
    literal = _GROK_LITERAL_ESCAPE_RE.sub(r"\\\1", str(label))
    # Triple-quoted ES|QL string so inner double quotes need no escaping. The
    # pattern is ``"<label>":"%{DATA:<label>}\"`` — DATA (non-greedy) is bounded
    # by the trailing ``\"`` which matches the JSON value's closing quote.
    #
    # The key is anchored to a TOP-LEVEL position: object start (optionally
    # through the ``{"labels":{...}}`` wrapper) or a preceding comma. An
    # unanchored first-occurrence match binds a same-named key nested inside
    # OTel resource attributes instead — ``k8s.cluster.name`` sorts before a
    # top-level ``name`` and ``service.name`` exists on any OTel-mapped
    # cluster — so the panel legend (and parity series keys) would carry the
    # wrong label's value. Nested first keys are always preceded by ``:{``,
    # which the anchor excludes; nested non-first keys are comma-preceded and
    # remain theoretically ambiguous, but the known OTel collision shapes
    # (service.name, host.name, k8s.*.name) are all single-key objects.
    pattern = f'(?:\\A\\{{(?:"labels":\\{{)?|,)"{literal}":"%{{DATA:{label}}}\\"'
    return f'| GROK _timeseries """{pattern}"""'


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
            concat_args.append(f'COALESCE(TO_STRING({column}), "")')
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


def _warn_extra_breakdown_dimensions(by_cols, dimension_field, breakdown_field, warnings):
    """Warn when an XY panel has more grouping dimensions than it can display.

    A Kibana XY chart breaks the series down by a single field. When the ES|QL
    query groups by two or more non-time dimensions, only the first becomes the
    visual breakdown and the rest are not represented on the chart, so series
    that differ only in a dropped dimension are visually merged. Surface that as
    a warning rather than silently rendering a different shape than the source.
    """
    if warnings is None:
        return
    extra = [
        col
        for col in (by_cols or [])
        if col != dimension_field and col != breakdown_field
    ]
    if extra:
        _append_unique(
            warnings,
            "XY chart shows a single breakdown; additional grouping "
            f"dimension(s) {extra} are in the query but not on the chart, "
            "so series differing only by those are visually merged",
        )


def _build_esql_xy_panel(esql, chart_type, metric_col=None, by_cols=None,
                         time_fields=None, mode=None,
                         legend_format_template=None, legend_labels=None,
                         warnings=None):
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
    if dimension_field is None:
        # The query collapses to a single row (no time dimension, no group
        # columns), so it cannot be an XY chart — emitting one would bind the
        # x-axis to a phantom ``time_bucket`` column the query never outputs
        # (issue #127). Degrade gracefully to a single-value metric.
        _append_unique(
            warnings if warnings is not None else [],
            "Rendered instant/single-value query as a metric (no time dimension to plot)",
        )
        return _build_esql_metric_panel(esql, metric_col=metric_col)
    _warn_extra_breakdown_dimensions(by_cols, dimension_field, breakdown_field, warnings)
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
                                legend_format_template=None, legend_labels=None,
                                warnings=None):
    """Build an XY panel from a single merged ES|QL query."""
    esql = _ensure_bucket_sort(esql)
    shape = _extract_esql_shape(esql)
    _, extracted_by_cols = _extract_esql_columns(esql)
    if by_cols is None:
        by_cols = extracted_by_cols
    if time_fields is None:
        time_fields = shape.time_fields
    dimension_field, breakdown_field = _select_xy_dimension_fields(by_cols, time_fields=time_fields)
    if dimension_field is None:
        # No time/group dimension to plot (issue #127). Multiple metric series
        # can't collapse to a single metric tile, so present them as a
        # single-row summary table instead of an XY chart with a phantom axis.
        _append_unique(
            warnings if warnings is not None else [],
            "Rendered instant/single-value query as a summary table (no time dimension to plot)",
        )
        return _build_esql_datatable_panel(esql, metric_fields=metric_fields)
    _warn_extra_breakdown_dimensions(by_cols, dimension_field, breakdown_field, warnings)
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


def _apply_series_override_axes(yaml_panel: dict, grafana_panel: dict, warnings: list[str]) -> None:
    esql = yaml_panel.get("esql")
    if not isinstance(esql, dict) or esql.get("type") not in {"line", "bar", "area"}:
        return
    metrics = esql.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        return
    overrides = grafana_panel.get("seriesOverrides")
    if not isinstance(overrides, list) or not overrides:
        return

    right_format = _grafana_yaxis_metric_format(grafana_panel, "right")
    for override in overrides:
        if not isinstance(override, dict) or _grafana_override_axis(override.get("yaxis")) != "right":
            continue
        alias = str(override.get("alias") or "").strip()
        matched = False
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            candidates = {
                str(metric.get("field") or ""),
                str(metric.get("label") or ""),
            }
            if _series_override_alias_matches(alias, candidates):
                metric["axis"] = "right"
                if right_format:
                    metric["format"] = dict(right_format)
                matched = True
        if alias and not matched:
            _append_unique(
                warnings,
                f'Dropped Grafana secondary y-axis assignment for unmatched series override "{alias}"',
            )


def _grafana_override_axis(value) -> str:
    try:
        axis = int(value)
    except (TypeError, ValueError):
        return ""
    return "right" if axis == 2 else "left" if axis == 1 else ""


def _grafana_yaxis_metric_format(grafana_panel: dict, axis: str) -> dict | None:
    yaxes = grafana_panel.get("yaxes")
    axis_idx = 1 if axis == "right" else 0
    if not isinstance(yaxes, list) or len(yaxes) <= axis_idx or not isinstance(yaxes[axis_idx], dict):
        return None
    unit = str(yaxes[axis_idx].get("format") or "")
    return grafana_unit_to_yaml_format(unit)


def _series_override_alias_matches(alias: str, candidates: set[str]) -> bool:
    if not alias:
        return False
    if alias.startswith("/") and alias.endswith("/") and len(alias) > 1:
        try:
            pattern = re.compile(alias[1:-1])
        except re.error:
            return False
        return any(candidate and pattern.search(candidate) for candidate in candidates)
    return alias in candidates


def _build_esql_gauge_panel(esql, metric_col=None, panel=None):
    if not metric_col:
        metric_col, _ = _extract_esql_columns(esql)
    defaults = _panel_field_defaults(panel)
    minimum = _coerce_number(defaults.get("min"))
    maximum = _coerce_number(defaults.get("max"))
    goal = _first_numeric_threshold(panel)
    # When a goal is set but no explicit max exists, infer max=100 for gauges
    # that use percentage-mode thresholds or a percent unit.  Without a max,
    # the Kibana gauge cannot position the goal arc correctly and the YAML lint
    # rule gauge-goal-without-max fires and blocks compilation.
    if goal is not None and maximum is None:
        thresholds_cfg = defaults.get("thresholds") or {}
        threshold_mode = thresholds_cfg.get("mode") if isinstance(thresholds_cfg, dict) else ""
        unit = defaults.get("unit") or ""
        if threshold_mode == "percentage" or unit in ("percent", "percentunit"):
            maximum = 100
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


def _field_has_ts_metadata_conflict(field_name, resolver):
    cache = getattr(resolver, "_field_cache", None) or {}
    variants = cache.get(field_name) or {}
    has_dimension = any(bool(meta.get("time_series_dimension")) for meta in variants.values() if isinstance(meta, dict))
    has_metric = any(bool(meta.get("time_series_metric")) for meta in variants.values() if isinstance(meta, dict))
    return has_dimension and has_metric


def _esql_values_control_query(field_name, data_view):
    """Build an ES|QL query that enumerates a control's selectable values.

    Mirrors Grafana's ``label_values()`` query variable: return the field's
    distinct values, sorted, so the Kibana control can populate its dropdown
    at render time.
    """
    field = _esql_identifier(field_name)
    index = data_view or "metrics-*"
    return (
        f"FROM {index} | WHERE {field} IS NOT NULL"
        f" | STATS count = COUNT(*) BY {field}"
        f" | SORT {field} ASC | KEEP {field} | LIMIT 1000"
    )


# Grafana's "All" selection (and any unknown default) maps to a regex
# match-all so the rewritten ``label=~?var`` matcher binds to every series,
# mirroring the source dashboard's default view instead of erroring.
_MATCH_ALL_SELECTION = ".*"


def _variable_default_selection(variable):
    """Pick a default selection for a template variable's binding control.

    Without a default the emitted control starts empty (``selectedOptions:
    []``) and the bound ES|QL parameter stays unset, so Kibana renders
    "Parameter [?var] value not found" on first load (issue #131). We mirror
    the Grafana variable's ``current`` selection / ``All`` so the migrated
    panel renders immediately, falling back to a regex match-all ("All") when
    no concrete default is available.
    """
    if not isinstance(variable, dict):
        return _MATCH_ALL_SELECTION
    current = variable.get("current")
    value = current.get("value") if isinstance(current, dict) else None
    if isinstance(value, (list, tuple)):
        # A scalar ES|QL parameter can hold only one value; a multi-value
        # current selection has no faithful single binding, so fall back to
        # "All" rather than arbitrarily picking one of the selected values.
        value = value[0] if len(value) == 1 else None
    # A concrete saved selection wins over "All" so the dashboard opens on the
    # same value the source did.
    if value not in (None, "", "$__all"):
        return str(value)
    if variable.get("includeAll"):
        all_value = variable.get("allValue")
        return str(all_value) if all_value else _MATCH_ALL_SELECTION
    return _MATCH_ALL_SELECTION


def _collect_regex_default_param_names(variables):
    """Names of template variables whose binding control defaults to the regex
    match-all (".*").

    ``_matcher_to_esql`` emits equality matchers (``field == ?var``) on these
    params as regex matches instead, so the control's ".*" default actually
    selects every series on first load rather than comparing the field against
    the literal string ".*" (PR #133 review). Keyed by Grafana variable name,
    which is exactly the ES|QL parameter name the matcher references.
    """
    names = set()
    for var in variables:
        if not isinstance(var, dict):
            continue
        name = var.get("name")
        if name and _variable_default_selection(var) == _MATCH_ALL_SELECTION:
            names.add(name)
    return names


def _build_esql_param_control(variable_name, label, field_name, data_view, default=None):
    """Build an ES|QL parameter-binding control (issue #107).

    When the target supports the ``promql_label_matcher_params`` capability the
    engine rewrites full-value Grafana template-variable matchers into native
    ES|QL named parameters (``WHERE instance == ?node``). A generic
    options/range data-view control does NOT define that ES|QL variable, so the
    uploaded panels fail to parse with "Unknown query parameter [node]". The
    control has to be an ES|QL control that binds the variable.

    A query-driven values control is emitted: it enumerates the resolved
    field's values at render time and binds them to the ES|QL variable named
    after the Grafana variable (which is exactly the parameter the query
    references). Single-select is used because the rewritten matchers reference
    the parameter in scalar positions (``== ?var`` / ``RLIKE ?var``); a
    multi-value binding would be invalid ES|QL there.

    A ``default`` selection is emitted so the parameter is bound on first load
    instead of leaving the control empty (issue #131).
    """
    control = {
        "type": "esql",
        "label": label,
        "variable_name": variable_name,
        "variable_type": "values",
        "query": _esql_values_control_query(field_name, data_view),
        "multiple": False,
    }
    if default not in (None, ""):
        control["default"] = default
    return control


MIN_DATATABLE_HEIGHT = 5


# _TYPE_SIZE_CONSTRAINTS is imported from layout.py as _TYPE_SIZE_CONSTRAINTS
# via the PANEL_SIZE_CONSTRAINTS alias at the top of this file.


def _normalize_tile_size(panel, kibana_type):
    """Apply per-type width/height min and max clamps (L2).

    Resolves the effective panel type from the panel's
    ``esql.type`` if present (this is the actual Kibana
    visualization), falling back to the caller-supplied
    ``kibana_type``, then ``markdown`` if the panel is a plain
    markdown tile. Unknown types pass through with no clamping,
    preserving the legacy behaviour for any future visualization
    type that doesn't have an entry in the constraint table.
    """
    size = dict(panel.get("size", {}))
    width = int(size.get("w", 0) or 0)
    height = int(size.get("h", 0) or 0)

    esql_cfg = panel.get("esql")
    if isinstance(esql_cfg, dict) and esql_cfg.get("type"):
        effective_type = str(esql_cfg["type"])
    elif "markdown" in panel:
        effective_type = "markdown"
    else:
        effective_type = str(kibana_type or "")

    constraints = _TYPE_SIZE_CONSTRAINTS.get(effective_type)
    if constraints is not None:
        min_w, min_h, max_h = constraints
        if 0 < width < min_w:
            width = min_w
        if 0 < height < min_h:
            height = min_h
        if max_h is not None and height > max_h:
            height = max_h

    if width > 0:
        size["w"] = width
    if height > 0:
        size["h"] = height
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
    if resolver and resolver.field_exists(field_name) is True:
        if resolver.has_conflicting_types(field_name) and _field_has_ts_metadata_conflict(field_name, resolver):
            return f"skipped conflicting control field {field_name}"
        if not resolver.is_aggregatable_field(field_name):
            return f"skipped non-aggregatable control field {field_name}"
    if binds_esql_named_params(context.rule_pack):
        # The target binds Grafana template variables as native ES|QL
        # parameters (``?<name>``), so the control must DEFINE that ES|QL
        # variable rather than emit a generic data-view filter; otherwise the
        # panel queries fail with "Unknown query parameter [name]" (issue #107).
        # This must mirror the ES|QL matcher gate in ``_matcher_to_esql`` so a
        # ``--no-native-promql`` run that preserves ``?var`` also emits the
        # binding control rather than a duplicate generic one (issue #132).
        context.control = _build_esql_param_control(
            variable_name=name,
            label=label or name,
            field_name=field_name,
            data_view=context.data_view,
            default=_variable_default_selection(context.variable),
        )
        if bool(context.variable.get("multi")) and name not in context.repeat_variable_names:
            context.trace.append(
                f"variable '{name}' was multi-select in Grafana but binds a scalar "
                "ES|QL parameter; emitted a single-select control"
            )
        context.handled = True
        return f"translated variable {name} as ES|QL parameter control"
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


# An ES|QL named parameter token (``?var``), excluding engine-internal params
# such as ``?_tstart`` / ``?_tend`` / ``?_job`` which are materialized at
# query time and never bound by a dashboard control.
_ESQL_PARAM_RE = re.compile(r"\?(?P<name>[A-Za-z][A-Za-z0-9_]*)")
# Quoted string literals, stripped before scanning so a ``?`` inside a value
# (e.g. a ``RLIKE "ab?c"`` pattern) is not mistaken for a named parameter.
_ESQL_QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")


def _query_param_names(query):
    """Return the ES|QL named parameters referenced by a query string."""
    if not isinstance(query, str):
        return set()
    unquoted = _ESQL_QUOTED_RE.sub('""', query)
    return {match.group("name") for match in _ESQL_PARAM_RE.finditer(unquoted)}


def _collect_emitted_param_names(panels):
    """Return every ES|QL named parameter (``?var``) referenced by panels.

    Both the native PROMQL path (``...{label=~?var}``) and the ES|QL path
    (``WHERE field == ?var``) emit Grafana template variables as ES|QL named
    parameters into ``esql.query``. Each one must have a binding control or the
    panel fails with "Parameter [?var] value not found" (issue #131).
    """
    names: set[str] = set()
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        esql_cfg = panel.get("esql")
        query = esql_cfg.get("query") if isinstance(esql_cfg, dict) else None
        names |= _query_param_names(query)
    return names


def _ensure_param_controls(
    controls,
    emitted_params,
    variables,
    data_view,
    resolver=None,
    rule_pack=None,
):
    """Guarantee a binding control exists for every emitted ``?var`` (issue #131).

    Control generation is otherwise driven only by ``templating.list`` via the
    registered variable translators, which miss two cases that still emit a
    ``?var`` into panel queries:

    * ``custom`` template variables (e.g. ArgoCD ``health_status`` /
      ``sync_status``), which are routed to the time-picker rule and skipped.
    * ``query`` variables skipped because their control field could not be
      resolved or did not exist in the target.

    For each referenced parameter without a control we synthesise an ES|QL
    values control bound to the parameter, with a default selection so the
    panel renders on first load.
    """
    bound = {
        control.get("variable_name")
        for control in controls
        if isinstance(control, dict)
        and control.get("type") == "esql"
        and control.get("variable_name")
    }
    missing = sorted(name for name in emitted_params if name not in bound)
    if not missing:
        return controls
    variables_by_name = {
        var.get("name"): var
        for var in variables
        if isinstance(var, dict) and var.get("name")
    }
    for name in missing:
        variable = variables_by_name.get(name, {})
        label = variable.get("label") or name
        source_field = (
            _extract_variable_source_field(_variable_query_text(variable)) or name
        )
        field_name = source_field
        if resolver:
            resolved = resolver.resolve_control_field(source_field)
            if resolved:
                field_name = resolved
        controls.append(
            _build_esql_param_control(
                variable_name=name,
                label=label,
                field_name=field_name,
                data_view=data_view,
                default=_variable_default_selection(variable),
            )
        )
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
    for panel in (dashboard.get("panels") or []):
        all_panels.append(panel)
        for sub_panel in (panel.get("panels") or []):
            all_panels.append(sub_panel)
    for row in (dashboard.get("rows") or []):
        for panel in (row.get("panels") or []):
            all_panels.append(panel)
    return sorted(all_panels, key=_panel_sort_key)


def _build_section_groups(dashboard):
    """Group Grafana panels by their parent row.

    Returns a list of ``(row_title | None, [panel, ...], is_explicit_row, collapsed)``.

    * ``row_title`` is the source row's title (``None`` when the row
      had an empty/missing title).
    * ``is_explicit_row`` is True iff the group came from a real
      Grafana row container (modern ``type: row`` or legacy
      ``rows[]``). False marks panels that genuinely live at the
      top level, before any row.
    * ``collapsed`` mirrors the source row's open/closed state:
      modern ``type: row`` panels carry ``collapsed: bool``, legacy
      ``rows[]`` entries carry ``collapse: bool`` (note the missing
      ``-d`` — see prometheus-all.json fixture / Grafana schema v14).
      Top-level (non-row) groups always have ``collapsed=False``.

    Downstream, :func:`translate_dashboard` uses ``is_explicit_row``
    to decide whether to emit a Kibana section (L3): every explicit
    row becomes a section, even when the source row had no title.
    Top-level panels stay flat. ``collapsed`` is threaded into the
    emitted ``section.collapsed`` field so the Kibana dashboard
    opens with the same sections expanded/closed as the source
    (issue #23).
    """
    groups: list[tuple[str | None, list[dict], bool, bool]] = []
    current_title: str | None = None
    current_panels: list[dict] = []
    current_is_row: bool = False
    current_collapsed: bool = False

    top_level = dashboard.get("panels", [])
    for panel in sorted(top_level, key=_panel_sort_key):
        if panel.get("type") == "row":
            if current_panels or groups:
                groups.append(
                    (current_title, current_panels, current_is_row, current_collapsed)
                )
            current_title = str(panel.get("title") or "").strip() or None
            current_panels = list(panel.get("panels", []))
            current_is_row = True
            current_collapsed = bool(panel.get("collapsed", False))
        else:
            current_panels.append(panel)

    for row in (dashboard.get("rows") or []):
        row_title = str(row.get("title") or "").strip() or None
        row_panels = row.get("panels", [])
        if not row_panels:
            continue
        # Legacy (schemaVersion < 14) rows use ``collapse`` (no -d); a
        # handful of exports also carry ``collapsed`` so accept either
        # rather than silently ignoring the wrong spelling.
        row_collapsed = bool(row.get("collapse", row.get("collapsed", False)))
        row_height_px = row.get("height") or 250
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
        groups.append((row_title, patched, True, row_collapsed))

    if current_panels or not groups:
        groups.append(
            (current_title, current_panels, current_is_row, current_collapsed)
        )

    return groups


def _repeat_variable_name(value):
    if not isinstance(value, str):
        return ""
    return value.strip()


# L4: maximum number of fan-out clones produced per repeating panel.
# Beyond this, we emit a warning and keep the first N. The cap stops
# a single ``repeat: instance`` on a 50-node cluster from ballooning
# the dashboard into 50 separate Lens panels.
L4_REPEAT_EXPANSION_CAP = 8


_VARIABLE_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _resolve_variable_values(variable: dict) -> tuple[list[str], str]:
    """Return ``(values, source)`` for a Grafana templating variable.

    Resolution order:

    * ``variable["options"]`` -- present for custom vars (always) and
      cached for query vars when the dashboard JSON has been saved
      with a "current" snapshot. Each option is ``{text, value}``.
    * ``variable["current"]["text"]`` / ``["value"]`` -- the last
      multi-select snapshot the Grafana UI cached.

    ``source`` is one of ``"options"``, ``"current"``, or ``""`` when
    no values could be resolved (most often: a fresh query var that
    has never been evaluated, or a query var pointing at a metric
    series we can't enumerate without hitting the live Elasticsearch).
    """
    options = variable.get("options")
    if isinstance(options, list) and options:
        out: list[str] = []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            value = opt.get("value")
            if value is None:
                value = opt.get("text")
            if value in ("$__all", "$__all_value", "All"):
                # Skip the "All" sentinel; we expand its constituents.
                continue
            if isinstance(value, str) and value:
                out.append(value)
            elif isinstance(value, list):
                out.extend(str(v) for v in value if v)
        if out:
            return out, "options"

    current = variable.get("current") or {}
    if isinstance(current, dict):
        text = current.get("text")
        value = current.get("value")
        for candidate in (text, value):
            if isinstance(candidate, list) and candidate:
                vals = [str(v) for v in candidate if v and v != "All"]
                if vals:
                    return vals, "current"
            if isinstance(candidate, str) and candidate and candidate != "All":
                return [candidate], "current"

    return [], ""


def _substitute_grafana_variables(text: str, substitutions: dict[str, str]) -> str:
    """Replace ``$var`` and ``${var}`` (and ``${var:fmt}``) in ``text``
    with ``substitutions[var]``. Variables not in the dict are left
    untouched so a downstream pass still sees them.
    """
    if not isinstance(text, str) or not substitutions:
        return text

    def _repl(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return substitutions.get(name, match.group(0))

    return _VARIABLE_REFERENCE_RE.sub(_repl, text)


def _clone_panel_with_substitutions(
    panel: dict,
    substitutions: dict[str, str],
    new_id: int,
) -> dict:
    """Deep-copy a panel and substitute ``$var`` references in its
    title and target expressions. ``gridPos`` is preserved verbatim
    here; the caller is responsible for repositioning the clones."""
    clone = copy.deepcopy(panel)
    clone["id"] = new_id
    clone.pop("repeat", None)
    clone.pop("repeatDirection", None)
    clone.pop("repeatPanelId", None)

    if "title" in clone:
        clone["title"] = _substitute_grafana_variables(
            str(clone.get("title") or ""), substitutions
        )

    targets = clone.get("targets")
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, dict):
                continue
            if "expr" in target and isinstance(target["expr"], str):
                target["expr"] = _substitute_grafana_variables(
                    target["expr"], substitutions
                )
            if "query" in target and isinstance(target["query"], str):
                target["query"] = _substitute_grafana_variables(
                    target["query"], substitutions
                )
    return clone


def _expand_repeat_panels(
    dashboard: dict,
    result: MigrationResult,
) -> dict:
    """L4: fan out ``repeat: $var`` panels into one clone per resolved
    variable value, returning a new dashboard with the expansion in
    place of the templates.

    The pass runs before :func:`_build_section_groups`, so downstream
    layout / translation logic sees ordinary, distinct panels rather
    than the original templates. Sections / rows / legacy
    ``dashboard.rows[]`` panel arrays are all handled by walking the
    same shape recursively.

    Cap behaviour: panels whose variable resolves to more than
    :data:`L4_REPEAT_EXPANSION_CAP` values produce the first
    ``L4_REPEAT_EXPANSION_CAP`` clones and a ``skipped`` PanelResult
    warning so the operator can spot the dropped dimension.

    Unresolvable variables (query vars without cached options /
    current) leave the original panel in place and record a
    ``skipped`` warning so the lost ``repeat`` dimension is visible.
    """
    variables = {
        v.get("name", ""): v
        for v in (dashboard.get("templating", {}).get("list") or [])
        if isinstance(v, dict) and v.get("name")
    }
    if not variables:
        # No variables -> no repeats can resolve; cheap-skip.
        return dashboard

    # Find the maximum existing panel id so synthesised ids never
    # collide with author-supplied ids.
    max_id = 0
    for panel in _flatten_dashboard_panels(dashboard):
        pid = panel.get("id")
        if isinstance(pid, int) and pid > max_id:
            max_id = pid

    next_id = [max_id + 1]

    def expand_panels(panel_list: list[dict]) -> list[dict]:
        out: list[dict] = []
        for panel in panel_list:
            if not isinstance(panel, dict):
                out.append(panel)
                continue

            # Recurse into row containers first so any repeats nested
            # in a collapsed row are also expanded.
            if panel.get("type") == "row" and panel.get("panels"):
                new_panel = dict(panel)
                new_panel["panels"] = expand_panels(panel["panels"])
                out.append(new_panel)
                continue

            repeat_name = _repeat_variable_name(panel.get("repeat"))
            if not repeat_name or repeat_name not in variables:
                out.append(panel)
                continue

            values, _source = _resolve_variable_values(variables[repeat_name])
            if not values:
                # Variable can't be resolved at translation time;
                # keep the original single panel (downstream control
                # logic in ``translate_variables`` will collapse the
                # repeat dimension into a single-select control as a
                # best-effort fallback) and emit a warning so the
                # operator knows the repeat dimension wasn't fanned
                # out.
                warn_result = PanelResult(
                    str(panel.get("title") or panel.get("type") or "panel"),
                    str(panel.get("type") or ""),
                    "skipped",
                    "skipped",
                    1.0,
                )
                warn_result.warnings = [
                    f"Could not resolve repeat variable ${repeat_name}; "
                    f"the dashboard's templating doesn't expose its values "
                    f"(no options[] or current cached). The repeat "
                    f"dimension is lost; consider adding explicit options "
                    f"to the variable definition.",
                ]
                result.panel_results.append(warn_result)
                result.skipped += 1
                # Preserve the original panel unchanged so the
                # existing decorative-header / control-collapse paths
                # downstream still recognise it.
                out.append(panel)
                continue

            capped_values = values[:L4_REPEAT_EXPANSION_CAP]
            if len(values) > L4_REPEAT_EXPANSION_CAP:
                warn_result = PanelResult(
                    str(panel.get("title") or panel.get("type") or "panel"),
                    str(panel.get("type") or ""),
                    "skipped",
                    "skipped",
                    1.0,
                )
                warn_result.warnings = [
                    f"Repeat variable ${repeat_name} has {len(values)} "
                    f"values; capped expansion to the first "
                    f"{L4_REPEAT_EXPANSION_CAP} to prevent dashboard "
                    f"explosion. Add a dashboard control filter to "
                    f"select among the remaining "
                    f"{len(values) - L4_REPEAT_EXPANSION_CAP} values.",
                ]
                result.panel_results.append(warn_result)
                result.skipped += 1

            direction = str(panel.get("repeatDirection") or "v").lower()
            origin = panel.get("gridPos") or {}
            base_x = int(origin.get("x", 0) or 0)
            base_y = int(origin.get("y", 0) or 0)
            base_w = int(origin.get("w", GRAFANA_GRID_COLS) or GRAFANA_GRID_COLS)
            base_h = int(origin.get("h", 4) or 4)

            for idx, value in enumerate(capped_values):
                subs = {repeat_name: str(value)}
                clone = _clone_panel_with_substitutions(panel, subs, next_id[0])
                next_id[0] += 1
                if direction == "h":
                    # Lay out horizontally, wrapping at the 24-col
                    # Grafana grid. Each clone keeps the source
                    # gridPos width and height.
                    cols_per_row = max(1, GRAFANA_GRID_COLS // base_w)
                    row_offset = idx // cols_per_row
                    col_offset = idx % cols_per_row
                    gpos = {
                        "x": base_x + col_offset * base_w,
                        "y": base_y + row_offset * base_h,
                        "w": base_w,
                        "h": base_h,
                    }
                else:
                    # Vertical (default): stack top-to-bottom.
                    gpos = {
                        "x": base_x,
                        "y": base_y + idx * base_h,
                        "w": base_w,
                        "h": base_h,
                    }
                clone["gridPos"] = gpos
                out.append(clone)
        return out

    expanded = dict(dashboard)
    if dashboard.get("panels"):
        expanded["panels"] = expand_panels(dashboard["panels"])
    if dashboard.get("rows"):
        new_rows = []
        for row in dashboard["rows"]:
            new_row = dict(row)
            new_row["panels"] = expand_panels(row.get("panels") or [])
            new_rows.append(new_row)
        expanded["rows"] = new_rows
    return expanded


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
    for row in (dashboard.get("rows") or []):
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
    """True for Grafana's stock "untitled row" placeholders.

    L3 deliberately *excludes* the truly-empty case from this check:
    an empty row title means "the author didn't bother labelling
    this row", which L3 handles by synthesising a numbered section
    title rather than flattening. The stock placeholder strings
    (``Title``, ``New Row``, ``Row``) DO indicate "this is just
    Grafana's default, please flatten".
    """
    cleaned = clean_template_variables(str(title or "")).strip()
    if not cleaned:
        return False
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

    # ``force_flatten`` is only True when there is a positive reason
    # to drop the section wrapper (placeholder row title, legacy
    # single-panel row, or a section whose only child has the same
    # title as the section). A *missing* row title alone is NOT a
    # reason -- L3 wants to wrap untitled explicit rows in
    # synthesised-title sections, not flatten them.
    force_flatten = False
    if _is_placeholder_section_title(row_title) or (legacy_row and len(retained_panels) <= 1):
        force_flatten = True
    elif len(retained_panels) == 1 and cleaned_title:
        child_title = clean_template_variables(str(retained_panels[0].get("title") or "")).strip()
        if not child_title:
            child_title = str(retained_panels[0].get("title") or "").strip()
        if child_title and child_title.casefold() == cleaned_title.casefold():
            force_flatten = True

    # ``title is None`` still signals "no source title" to callers
    # that don't read force_flatten; they decide whether to synthesise
    # one based on whether the group came from an explicit row.
    return NormalizedPanelGroup(
        title=None if force_flatten else cleaned_title,
        panels=retained_panels,
        skipped_panel_results=skipped_panel_results,
        force_flatten=force_flatten,
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

    # L2 (collision-aware): apply per-type minimums **without**
    # breaking the 2D grid the source author authored. If bumping a
    # panel's w or h to its L2 minimum would overlap another panel
    # in this group, prefer the smaller dimension (the author's
    # intent) over the readability floor.
    _apply_collision_aware_minimums(yaml_panels)

    return yaml_panels


def _rect(panel: dict) -> tuple[int, int, int, int]:
    """Return ``(x, y, w, h)`` from a panel's position/size dicts.

    Defaults to (0, 0, 0, 0) for missing fields so callers can
    short-circuit on zero-sized panels.
    """
    pos = panel.get("position", {}) or {}
    sz = panel.get("size", {}) or {}
    return (
        int(pos.get("x", 0) or 0),
        int(pos.get("y", 0) or 0),
        int(sz.get("w", 0) or 0),
        int(sz.get("h", 0) or 0),
    )


def _rects_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def _apply_collision_aware_minimums(yaml_panels: list[dict]) -> None:
    """L2 with a 2D-grid safety guard.

    For each panel we compute its current ``(x, y, w, h)`` (post-L1)
    plus the L2 per-type ``(min_w, min_h, max_h)``. We try to grow
    the panel to those minimums **only when** doing so does not
    collide with another panel in the same group. If a bump would
    overlap a neighbour we keep the smaller dimension -- the source
    author chose those dimensions for a reason (typically because
    the panel sits in a 2D grid beside taller panels).

    Specifically the algorithm walks panels in **document order**
    (so earlier panels get the first crack at the readability bump)
    and treats already-bumped neighbours as fixed obstacles.

    ``max_h`` clamps always apply because shrinking a panel cannot
    create new overlaps.
    """
    for idx, panel in enumerate(yaml_panels):
        kibana_type = _kibana_panel_type(panel)
        esql_cfg = panel.get("esql")
        if isinstance(esql_cfg, dict) and esql_cfg.get("type"):
            effective_type = str(esql_cfg["type"])
        elif "markdown" in panel:
            effective_type = "markdown"
        else:
            effective_type = str(kibana_type or "")

        constraints = _TYPE_SIZE_CONSTRAINTS.get(effective_type)
        if constraints is None:
            # Apply legacy single-rule clamps and the position-clamp
            # via the standard helper for unknown types.
            _normalize_tile_size(panel, kibana_type)
            continue

        min_w, min_h, max_h = constraints
        x, y, w, h = _rect(panel)
        if w <= 0 or h <= 0:
            _normalize_tile_size(panel, kibana_type)
            continue

        # Max-h always applies (shrinking never creates overlap).
        if max_h is not None and h > max_h:
            h = max_h

        # Try to bump width to min_w. Reject if it would overlap any
        # other panel in this group.
        if w < min_w:
            candidate = (x, y, min_w, h)
            collides = any(
                i != idx and _rects_overlap(candidate, _rect(other))
                for i, other in enumerate(yaml_panels)
            )
            if not collides:
                w = min_w

        # Try to bump height to min_h. Same collision check.
        if h < min_h:
            candidate = (x, y, w, min_h)
            collides = any(
                i != idx and _rects_overlap(candidate, _rect(other))
                for i, other in enumerate(yaml_panels)
            )
            if not collides:
                h = min_h

        panel["size"] = {"w": w, "h": h}
        # Re-apply the legacy x-clamp + grid-overflow guard.
        position = dict(panel.get("position", {}))
        max_x = KIBANA_GRID_COLS - w
        if max_x < 0:
            max_x = 0
        position["x"] = min(int(position.get("x", 0) or 0), max_x)
        panel["position"] = position


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


def _resolve_section_overlaps_recursively(panels: list[dict]) -> None:
    """Walk the panel tree, calling :func:`_resolve_panel_overlaps` on
    every section's leaf-panel list (and on the top-level non-section
    panels) in place.

    Each section's coordinate space is independent (panels inside a
    section are positioned relative to that section in Kibana), so we
    resolve overlaps **within** each section, not across sections.
    """
    section_groups: list[list[dict]] = []
    top_leaves: list[dict] = []
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            inner = section.get("panels")
            if isinstance(inner, list) and inner:
                section_groups.append(inner)
        else:
            top_leaves.append(panel)

    for group in section_groups:
        resolved = _resolve_panel_overlaps(group)
        # ``_resolve_panel_overlaps`` returns a new list of dicts in
        # the original order, but the dicts themselves are shallow
        # copies. Patch position/size back into the originals so the
        # caller's list (which is the actual YAML doc tree) sees the
        # change.
        for src, dst in zip(resolved, group):
            dst["position"] = src["position"]
            dst["size"] = src["size"]

    if top_leaves:
        resolved = _resolve_panel_overlaps(top_leaves)
        for src, dst in zip(resolved, top_leaves):
            dst["position"] = src["position"]
            dst["size"] = src["size"]


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
    metric_series_labels=None,
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
            metric_series_labels=metric_series_labels,
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

    # L4: expand ``repeat: $var`` panels into one concrete clone per
    # resolved variable value BEFORE any downstream logic walks the
    # panels. From here on every panel in ``dashboard`` is a regular
    # (non-templated) panel and the rest of the pipeline can stay
    # ignorant of the fan-out.
    dashboard = _expand_repeat_panels(dashboard, result)

    all_panels = _flatten_dashboard_panels(dashboard)
    result.total_panels = len(all_panels)

    # Offline per-metric series-label map: lets bare gauge selectors that name no labels of
    # their own recover per-series grouping from other panels / template variables.
    metric_series_labels = build_metric_series_labels(dashboard)

    variables = dashboard.get("templating", {}).get("list", [])
    control_variable_names = _pre_scan_control_variables(variables)
    # Record which ``?var`` params default to the regex match-all so both the
    # ES|QL and native PROMQL matcher emitters loosen equality matchers on
    # All/multi variables into regex matches and render data on first load
    # (PR #133 review). Stored on the shared rule pack so it is reachable from
    # the resolver (``resolver._rule_pack``) on the ES|QL path and threaded
    # explicitly into the native path. Set before any panel translation runs.
    rule_pack._regex_default_param_names = _collect_regex_default_param_names(variables)

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
    untitled_section_counter = 0
    for row_title, group_panels, is_explicit_row, source_collapsed in section_groups:
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
            metric_series_labels=metric_series_labels,
        )
        result.yaml_panel_results.extend(panel_results)

        if not translated:
            continue

        if legacy_group and normalized_group.title is None:
            _restore_flattened_legacy_panel_titles(translated)
        group_height = _panel_group_height(translated)

        # L3: every explicit Grafana row container becomes a Kibana
        # section, even when the source row had no title. Synthesise
        # a fallback title in that case so each section gets a
        # unique, human-readable label. Panels before any row stay
        # flat at the top level.
        #
        # The pre-existing ``_normalize_panel_group`` flattening
        # heuristic (legacy single-panel rows, placeholder titles
        # like "New Row") wins over L3 -- it knows when a section
        # would be visual clutter, and we don't want to undo that.
        should_emit_section = (
            bool(normalized_group.title) or is_explicit_row
        ) and not normalized_group.force_flatten
        if should_emit_section:
            if normalized_group.title:
                cleaned = (
                    clean_template_variables(normalized_group.title)
                    or normalized_group.title
                )
            else:
                untitled_section_counter += 1
                cleaned = f"Section {untitled_section_counter}"
            count = used_section_titles.get(cleaned, 0) + 1
            used_section_titles[cleaned] = count
            unique_title = f"{cleaned} ({count})" if count > 1 else cleaned
            section_panel = {
                "title": unique_title,
                "section": {
                    # Issue #23: mirror the source row's collapsed state so the
                    # Kibana dashboard opens with the same sections expanded /
                    # closed as the Grafana original. Modern type=="row" panels
                    # carry ``collapsed``; legacy rows[] carry ``collapse``
                    # (both normalised upstream in _build_section_groups).
                    "collapsed": source_collapsed,
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

    # Parameters (``?var``) actually emitted by panel queries drive control
    # completeness: every one needs a binding control, and any variable that
    # became a control should no longer be reported as a dropped filter.
    emitted_params = _collect_emitted_param_names(flat_panels)
    _rewrite_variable_warnings(
        result.panel_results, control_variable_names | emitted_params
    )

    controls_data_view = _infer_controls_data_view(flat_panels, datasource_index, rule_pack)
    controls_resolver = _resolver_for_index(resolver, rule_pack, controls_data_view)
    controls = translate_variables(
        variables,
        controls_data_view,
        rule_pack=rule_pack,
        resolver=controls_resolver,
        repeat_variable_names=repeat_variable_names,
    )
    controls = _ensure_param_controls(
        controls,
        emitted_params,
        variables,
        controls_data_view,
        resolver=controls_resolver,
        rule_pack=rule_pack,
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

    # Safety net: ``apply_style_guide_layout`` (specifically
    # ``_fill_simple_row``) can rescale a row's widths to total
    # exactly 48 columns, which sometimes nudges panels by 1-2 cols
    # and pushes them into a neighbouring 2D-grid panel below.
    # ``_resolve_panel_overlaps`` walks the post-layout panel list
    # in (y, x) order and bumps any overlapping panel's y down to
    # the bottom of its conflicting neighbours. This keeps L2's
    # per-type minimums (which sometimes widen panels) from being
    # punished by the downstream ``kb-dashboard-cli`` compile step,
    # which rejects any overlap.
    for dashboard in yaml_doc.get("dashboards") or []:
        _resolve_section_overlaps_recursively(dashboard.get("panels") or [])

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
