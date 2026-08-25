# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Panel, variable, and dashboard translation helpers."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from typing import Any

from observability_migration.core.assets.dashboard import DashboardIR
from observability_migration.core.assets.operational import build_operational_ir
from observability_migration.core.assets.query import QueryIR, build_query_ir, infer_output_shape
from observability_migration.core.assets.visual import refresh_visual_ir
from observability_migration.core.reporting.report import (
    MigrationResult,
    PanelResult,
    _panel_query_index,
    recompute_result_counts,
)
from observability_migration.core.telemetry_contract import _extract_esql_values_bound_field
from observability_migration.core.verification.field_capabilities import assess_field_usage
from observability_migration.targets.kibana.dashboards_api import native_dashboard_from_ir
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
from .links import build_links_panel, translate_dashboard_links
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
    _finalize_fused_stats_assignments,
    _format_scalar_value,
    _inline_filters_into_stats_expr,
    _is_counter_fallback,
    _matcher_to_esql,
    _parse_fragment,
    _safe_alias,
    _split_top_level_csv,
    _summary_mode_from_metadata,
    _union_group_fields,
    _unique_safe_alias,
    collapse_or_for_native_promql,
    grafana_template_var_name,
    substitute_grafana_range_macros,
    substitute_scalar_template_vars,
)
from .rules import PANEL_TRANSLATORS, VARIABLE_TRANSLATORS, RulePackConfig, _append_unique
from .runtime_features import (
    ESQL_NAMED_PARAM_BINDING,
    KIBANA_PROMQL_CONTROL_PARAMS,
    PROMQL_HISTOGRAM_QUANTILE,
    PROMQL_LABEL_MATCHER_PARAMS,
    binds_esql_named_params,
    get_runtime_features,
    is_feature_supported,
    set_runtime_feature,
)
from .schema import SchemaResolver
from .series_labels import (
    _metrics_in_expr,
    build_metric_series_labels,
    expr_has_explicit_grouping,
)
from .transforms import apply_transformations_to_esql
from .translate import (
    TranslationContext,
    _build_metric_contract_artifacts,
    _collect_source_metrics,
    translate_promql_to_esql,
)
from .verification import panel_notes_imply_warning

PANEL_TYPE_MAP = {
    "timeseries": "line",
    "graph": "line",
    "stat": "metric",
    "singlestat": "metric",
    "gauge": "gauge",
    # Default/single-value mapping only. ``bargauge_panel_rule`` routes a
    # grouped or multi-value bargauge to a bar chart (``kibana_type="bar"``) at
    # translation time; a single-value bargauge stays a bullet gauge.
    "bargauge": "gauge",
    "table": "datatable",
    "table-old": "datatable",
    "text": "markdown",
    "logs": "datatable",
    "heatmap": "heatmap",
    "piechart": "pie",
    "grafana-piechart-panel": "pie",  # community plugin alias for built-in piechart
    "barchart": "bar",
    # Discrete-state visualizations. Kibana has no native state-timeline /
    # status-history panel, but the underlying query is an ordinary metric time
    # series, so the data is preserved by rendering it as a line chart. The
    # visual approximation is disclosed via ``APPROXIMATED_VIS_TYPE_NOTES`` so the
    # panel lands as ``migrated_with_warnings`` (degrade gracefully) rather than a
    # bare "Migration Required" placeholder that discards the query.
    "state-timeline": "line",
    "status-history": "line",
}

# Source panel types whose Kibana mapping is a deliberate visual approximation.
# The note is appended to the migrated panel's reasons and downgrades the status
# to ``migrated_with_warnings`` so the fidelity loss is never silent.
APPROXIMATED_VIS_TYPE_NOTES = {
    "state-timeline": (
        "Grafana state-timeline panel approximated as a Kibana line chart: the "
        "underlying time series is preserved, but Kibana has no discrete "
        "state-band visualization so state transitions render as line values"
    ),
    "status-history": (
        "Grafana status-history panel approximated as a Kibana line chart: the "
        "underlying time series is preserved, but Kibana has no periodic "
        "discrete-state (status cell) visualization so values render as a line"
    ),
}

SKIP_PANEL_TYPES = {"row", "news", "dashlist", "alertlist", "nodeGraph", "canvas"}

GRAFANA_GRID_COLS = 24
KIBANA_GRID_COLS = 48
GRAFANA_ROW_HEIGHT_PX = 30
KIBANA_ROW_HEIGHT_PX = 20
MINIMUM_KIBANA_VERSION = "9.5.0"
# Kibana forwards dashboard variable values into named params inside native
# PROMQL label matchers starting in 9.5 (elastic/kibana#271244). Kept as an
# explicit floor marker even though it currently equals MINIMUM_KIBANA_VERSION.
NATIVE_PROMQL_CONTROL_PARAMS_MIN_VERSION = "9.5.0"
# Floor required by panels that pass histogram_quantile through the native
# PROMQL path (Elasticsearch >= 9.5; elastic/elasticsearch#150578). Only the
# native path keeps the literal ``histogram_quantile(`` in the emitted ES|QL —
# the ES|QL fallback rewrites it to ``PERCENTILE(...)`` — so its presence in a
# panel query uniquely marks a 9.5-requiring panel.
NATIVE_HISTOGRAM_QUANTILE_MIN_VERSION = "9.5.0"
MIN_PANEL_WIDTH = 4


def _parse_kibana_version(version):
    parts = str(version or "").strip().split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except (ValueError, TypeError):
        return (0,)


def _source_dashboard_tags(dashboard):
    """Tags declared on the source dashboard, de-duplicated in order.

    Carried on the IR (not the YAML document, whose schema forbids unknown
    keys) so they reach the native payload. After a bulk migration these are
    how an operator finds anything.
    """
    raw = (dashboard or {}).get("tags")
    if not isinstance(raw, list):
        return []
    out = []
    for tag in raw:
        text = str(tag).strip()
        if text and text not in out:
            out.append(text)
    return out


# Duration units accepted by both Grafana (dashboard ``refresh``, panel
# ``timeFrom``) and Elasticsearch date math (``now-<n><unit>``). Case matters:
# ``m`` is minutes, ``M`` is months, matching ES date math.
_GRAFANA_DURATION_RE = re.compile(r"^(\d+)(ms|s|m|h|d|w|M|y)$")
_GRAFANA_DURATION_UNIT_MS = {
    "ms": 1,
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 7 * 86_400_000,
    "M": 30 * 86_400_000,
    "y": 365 * 86_400_000,
}


def _grafana_duration_to_ms(text):
    """Convert a Grafana duration string (``"5s"``, ``"1h"``) to milliseconds.

    Returns ``None`` when *text* is not a recognized duration. ``None`` is the
    signal callers use to warn-and-drop rather than guess.
    """
    match = _GRAFANA_DURATION_RE.match(str(text or "").strip())
    if not match:
        return None
    amount, unit = match.groups()
    return int(amount) * _GRAFANA_DURATION_UNIT_MS[unit]


def _grafana_time_bound_to_api(value):
    """Normalize one Grafana ``dashboard.time`` bound to the API's shape.

    Returns ``(normalized, ok)``. Relative bounds (``now-6h``, ``now/d``)
    already use Elasticsearch date-math syntax and pass through unchanged.
    Absolute bounds arrive as epoch-millisecond numbers/strings (Grafana's
    usual form: 13+ digits) and are converted to ISO 8601 so the API's
    date-math-or-ISO-8601 ``time_range`` schema accepts them. Shorter all-
    digit strings (e.g. a bare epoch-seconds value) are refused rather than
    misread as milliseconds -- guessing a 1970-ish window is worse than
    dropping the bound with a warning.
    """
    if value is None:
        return "", True
    if isinstance(value, bool):
        return "", False
    text = str(value).strip()
    if not text:
        return "", True
    if text.lower().startswith("now"):
        return text, True
    # Grafana absolute times are epoch milliseconds (13+ digits from ~2001
    # onward). Require that length so a 10-digit epoch-seconds value is not
    # silently divided by 1000 into a 1970-ish date.
    if re.fullmatch(r"\d{13,}", text):
        try:
            dt = datetime.fromtimestamp(int(text) / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return "", False
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", True
    if re.fullmatch(r"\d+", text):
        return "", False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "", False
    return text, True


def _grafana_dashboard_time_range(dashboard, warnings):
    """Normalize Grafana ``dashboard.time`` into the API's ``time_range`` shape.

    Missing/empty leaves ``time_range`` unset (Kibana's own default) rather
    than guessing a window. An unrecognized ``from``/``to`` value is dropped
    with an explicit warning instead of shipping something Kibana would
    reject. Kibana's saved-object model only restores a time window when
    *both* ``timeFrom`` and ``timeTo`` are present, so a one-sided range is
    also refused rather than emitted and then flagged lossy on upload.
    """
    raw_time = dashboard.get("time") if isinstance(dashboard, dict) else None
    if not isinstance(raw_time, dict):
        return {}
    raw_from = raw_time.get("from")
    raw_to = raw_time.get("to")
    if raw_from in (None, "") and raw_to in (None, ""):
        return {}
    norm_from, from_ok = _grafana_time_bound_to_api(raw_from)
    norm_to, to_ok = _grafana_time_bound_to_api(raw_to)
    if not from_ok or not to_ok:
        warnings.append(
            f"Dashboard time range from={raw_from!r} to={raw_to!r} is dropped: "
            "unrecognized date-math or timestamp value"
        )
        return {}
    if not norm_from or not norm_to:
        warnings.append(
            f"Dashboard time range from={raw_from!r} to={raw_to!r} is dropped: "
            "Kibana requires both from and to bounds"
        )
        return {}
    return {
        "from": norm_from,
        "to": norm_to,
        "mode": (
            "relative"
            if all(str(bound).lower().startswith("now") for bound in (norm_from, norm_to))
            else "absolute"
        ),
    }


def _grafana_dashboard_refresh_interval(dashboard, warnings):
    """Normalize Grafana ``dashboard.refresh`` into the API's ``refresh_interval``.

    A missing ``refresh`` key leaves ``refresh_interval`` unset so Kibana
    keeps its own default. Explicit auto-refresh off (``False`` or ``""``)
    emits a paused interval so a dashboard whose author disabled refresh
    does not silently inherit a target Kibana's auto-refresh default. An
    unrecognized value is dropped with an explicit warning.
    """
    if not isinstance(dashboard, dict):
        return {}
    if "refresh" not in dashboard:
        return {}
    raw_refresh = dashboard.get("refresh")
    if raw_refresh is False:
        return {"pause": True, "value": 0}
    if raw_refresh is None:
        # Explicit ``refresh: null`` is the same author intent as ``false``.
        return {"pause": True, "value": 0}
    text = str(raw_refresh).strip()
    if not text:
        return {"pause": True, "value": 0}
    value_ms = _grafana_duration_to_ms(text)
    if value_ms is None:
        warnings.append(f"Dashboard refresh interval {text!r} is dropped: unrecognized format")
        return {}
    return {"pause": False, "value": value_ms}


def _grafana_panel_time_range_override(panel):
    """Convert a Grafana panel's ``timeFrom`` into the API's panel ``time_range``.

    Grafana's "Override relative time" panel option shows ``now-<timeFrom>``
    through ``now`` regardless of the dashboard's own time range -- the same
    date-math shape the Dashboards API panel-config ``time_range`` accepts.
    Returns ``(time_range, warning)``; *time_range* is ``{}`` when the panel
    sets no override, *warning* is set when ``timeFrom`` is present but not a
    recognized relative duration.
    """
    raw = str((panel or {}).get("timeFrom") or "").strip()
    if not raw:
        return {}, ""
    if _grafana_duration_to_ms(raw) is None:
        return {}, f"Panel time override timeFrom={raw!r} is dropped: unrecognized duration"
    return {"from": f"now-{raw}", "to": "now", "mode": "relative"}, ""


def _grafana_panel_time_shift_warning(panel):
    """Semantic-loss warning for a Grafana panel's ``timeShift``, if set.

    ``timeShift`` moves a panel's whole window into the past (e.g. "compare to
    last week") -- a shift, not a fixed range -- which the Dashboards API
    panel-config ``time_range`` cannot express (it is an absolute override).
    Graceful degradation: drop it with an operator-visible warning rather than
    emitting a ``time_range`` that would silently change what the panel shows.
    """
    raw = str((panel or {}).get("timeShift") or "").strip()
    if not raw:
        return ""
    return (
        f"Panel time shift timeShift={raw!r} has no Kibana Dashboards API "
        "equivalent (time_range is an absolute override, not a shift) and is dropped"
    )


def _dashboard_minimum_kibana_version(flat_panels):
    """Return the dashboard ``minimum_kibana_version`` floor for *flat_panels*.

    Defaults to :data:`MINIMUM_KIBANA_VERSION` (product floor: Kibana 9.5+).
    Raised further only if a future panel capability needs a higher version;
    today control-param forwarding and native ``histogram_quantile`` also
    require 9.5. The dashboard schema only carries this field per-dashboard,
    so the floor is the max across panels.
    """
    minimum = MINIMUM_KIBANA_VERSION
    for panel in flat_panels or []:
        query = (panel.get("esql") or {}).get("query", "") if isinstance(panel, dict) else ""
        if (
            query.lstrip().upper().startswith("PROMQL ")
            and _PROMQL_LABEL_MATCHER_PARAM_RE.search(query)
            and _parse_kibana_version(NATIVE_PROMQL_CONTROL_PARAMS_MIN_VERSION)
            > _parse_kibana_version(minimum)
        ):
            minimum = NATIVE_PROMQL_CONTROL_PARAMS_MIN_VERSION
        # Match the call form (``histogram_quantile(``), not a bare token, so a
        # metric/label whose name merely contains it does not trip the floor.
        if _PROMQL_HISTOGRAM_QUANTILE_RE.search(query) and _parse_kibana_version(
            NATIVE_HISTOGRAM_QUANTILE_MIN_VERSION
        ) > _parse_kibana_version(minimum):
            minimum = NATIVE_HISTOGRAM_QUANTILE_MIN_VERSION
    return minimum

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
    variables_by_name: dict[str, dict] = field(default_factory=dict)
    query_text: str = ""
    source_field: str = ""
    repeat_variable_names: set[str] = field(default_factory=set)
    control: dict | None = None
    handled: bool = False
    trace: list = field(default_factory=list)
    # Deliberately separate from `trace` (which mixes internal/speculative
    # notes, e.g. custom_variable_rule's note that is only accurate before
    # `_ensure_param_controls` runs): entries here are user-facing
    # dashboard-level warnings a caller can surface directly (issue #269).
    control_warnings: list = field(default_factory=list)


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
    fill_opacity = custom.get("fillOpacity")
    try:
        if fill_opacity is not None and float(fill_opacity) > 0:
            return "area"
    except (TypeError, ValueError):
        pass
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


def _grafana_override_regex_matches(pattern: str, candidate: str) -> bool:
    text = str(pattern or "").strip()
    value = str(candidate or "")
    if not text or not value:
        return False
    if len(text) >= 2 and text.startswith("/") and text.endswith("/"):
        text = text[1:-1]
    try:
        return re.search(text, value, re.IGNORECASE) is not None
    except re.error:
        return False


def _target_has_negative_y_override(panel: dict[str, Any], target: dict[str, Any]) -> bool:
    field_config = panel.get("fieldConfig") or {}
    overrides = field_config.get("overrides") if isinstance(field_config, dict) else []
    legend = str(target.get("legendFormat") or "").strip()
    ref_id = str(target.get("refId") or "").strip()
    expr = str(target.get("expr") or "").strip()
    candidates = [item for item in (legend, ref_id, expr) if item]
    for override in overrides or []:
        if not isinstance(override, dict):
            continue
        properties = override.get("properties")
        if not isinstance(properties, list):
            continue
        if not any(
            isinstance(prop, dict)
            and str(prop.get("id") or "").strip() == "custom.transform"
            and str(prop.get("value") or "").strip() == "negative-Y"
            for prop in properties
        ):
            continue
        matcher = override.get("matcher") or {}
        matcher_id = str(matcher.get("id") or "").strip()
        matcher_options = matcher.get("options")
        if matcher_id == "byName":
            expected = str(matcher_options or "").strip()
            if expected and any(candidate == expected for candidate in candidates):
                return True
        elif matcher_id == "byRegexp":
            pattern = str(matcher_options or "").strip()
            if pattern and any(_grafana_override_regex_matches(pattern, candidate) for candidate in candidates):
                return True
    return False


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


def _panel_reduce_calc(panel) -> str:
    """The reducer a Grafana scalar panel declares, e.g. ``lastNotNull``.

    An ABSENT ``calcs`` is not "unspecified" -- Grafana defaults stat, gauge and
    bargauge to ``lastNotNull``, and that is what the dashboard renders. Reporting
    "" here let the collapse fall back to MAX, so a gauge with no explicit calc
    showed the range PEAK instead of its current value.
    """
    calcs = (((panel or {}).get("options") or {}).get("reduceOptions") or {}).get("calcs")
    if isinstance(calcs, list) and calcs:
        return str(calcs[0] or "")
    if str((panel or {}).get("type") or "").lower() in _SCALAR_PANEL_TYPES:
        return "lastNotNull"
    return ""


def _target_translation_hints(panel, panel_type, target, metric_series_labels=None):
    summary_mode = _target_summary_mode(panel_type, target)
    hints = {
        "summary_mode": summary_mode,
        "series_alias": _target_series_alias(panel, target),
        # Grafana states how a scalar panel reduces its series here. It was never
        # read, so every such panel collapsed with MAX no matter what the
        # dashboard asked for -- "CPU Busy" declares lastNotNull and Grafana
        # draws 1.87% where MAX draws 79.1%.
        "reduce_calc": _panel_reduce_calc(panel),
    }
    preferred_group_labels = []
    style_labels = []
    if panel_type in {"table", "table-old"}:
        style_labels = _panel_group_label_patterns(panel)
        preferred_group_labels.extend(style_labels)
    legend_labels = _extract_legend_labels(target.get("legendFormat", ""))
    legend_contributed = False
    # Legend placeholders are display aliases on stat/gauge status grids — they
    # are not complete series identity (``{{job}}`` would merge instances) and
    # must not widen a scalar outer aggregation. Bargauge still uses a legend
    # placeholder as its categorical breakdown column. XY panels may hint the
    # label; ``_drop_legend_labels_if_redundant`` decides whether it is real.
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
    # Skip inferred dashboard-wide labels on summary panels: those are a guess.
    # Explicit PromQL ``by()`` remains authoritative; legend text is not.
    #
    # Also skip panels whose own expression already carries an explicit by()/without()
    # clause: that grouping is authoritative and the translator honors it directly, so
    # the dashboard-wide union must not overwrite it (issue #94).
    if (
        not summary_mode
        and not preferred_group_labels
        and metric_series_labels
        and not expr_has_explicit_grouping(target.get("expr", ""))
        and _allows_dashboard_label_inference(target.get("expr", ""))
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
_PROMQL_AGG_PREFIX_RE = re.compile(
    r"^\s*(?:"
    + "|".join(sorted(_PROMQL_AGG_FUNCS, key=len, reverse=True))
    + r")\b(?:\s+(?:by|without)\s*\([^)]*\))?\s*\(",
    re.IGNORECASE,
)
_PROMQL_LABEL_MATCHER_VAR_RE = re.compile(
    r"^\s*(?P<label>[A-Za-z_][A-Za-z0-9_\.:-]*)\s*(?P<op>=~|!~|!=|=)\s*"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)\s*$",
    re.DOTALL,
)
# Leading unary sign (``-sum(...)``) or scalar operand + arithmetic operator
# (``100 * sum(...)``, ``1 - avg(...)``). Peeling these exposes the inner
# aggregation so the prefix guard still fires on collapsed expressions wrapped
# in unary/scalar arithmetic. A PromQL metric name can never start with a digit,
# so stripping a leading number is unambiguous.
_PROMQL_LEADING_SCALAR_RE = re.compile(
    r"^\s*(?:[-+]|(?:\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*[-+*/%])",
)
# Leading ``func(`` call. A PromQL vector-transform function (``clamp_max``,
# ``abs``, ``round`` …) preserves the label set of its vector operand, which is
# always its first argument. ``topk``/``bottomk`` preserve the labels of their
# selected input series, whose vector operand is the second argument. Recursing
# into those operands exposes wrapped aggregations to the prefix guard.
_PROMQL_LEADING_FUNC_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_PROMQL_LABEL_PRESERVING_FUNCS = frozenset({
    "abs", "ceil", "clamp", "clamp_max", "clamp_min", "exp", "floor",
    "label_join", "label_replace", "ln", "log2", "log10", "round", "sgn",
    "sort", "sort_desc", "sqrt", "timestamp",
})
_PROMQL_LABEL_PRESERVING_AGG_FUNCS = frozenset({"topk", "bottomk"})
_PROMQL_UNLABELED_FUNCS = frozenset({
    "absent", "absent_over_time", "pi", "scalar", "time", "vector",
})

# Warnings that are generated during per-target pre-translation of binary
# expressions but become stale once _build_multi_target_series_query succeeds
# (co-located STATS+EVAL fusion resolves them exactly).
_STALE_AFTER_COLOCATED_FUSION = frozenset([
    "Approximated PromQL arithmetic using same-bucket ES|QL math",
    "PromQL series labels were not retained; output is bucket-level and may collapse multiple source series",
])


def _strip_wrapping_parentheses(expr):
    text = str(expr or "").strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        wraps_entire_expr = True
        quote = ""
        escaped = False
        for idx, char in enumerate(text):
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
                continue
            if char in ("'", '"'):
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and idx != len(text) - 1:
                    wraps_entire_expr = False
                    break
                if depth < 0:
                    wraps_entire_expr = False
                    break
        if not wraps_entire_expr or depth != 0:
            break
        text = text[1:-1].strip()
    return text


def _peel_leading_parenthesized_arithmetic_operand(text):
    """Return a leading parenthesized operand from scalar arithmetic.

    ``(sum(...)) * 100`` preserves the label shape of the left vector operand,
    just like ``100 * sum(...)``. Peeling that balanced left operand lets the
    aggregation guard see collapsed aggregations hidden by the parentheses.
    """
    text = str(text or "").strip()
    if not text.startswith("("):
        return text
    depth = 0
    quote = ""
    escaped = False
    for idx, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                rest = text[idx + 1:].lstrip()
                if rest.startswith(("+", "-", "*", "/", "%")):
                    return text[1:idx].strip()
                return text
            if depth < 0:
                return text
    return text


def _peel_leading_function_label_operand(text):
    """Return the label-preserving vector operand of a leading function call.

    PromQL vector-transform functions (``clamp_max``, ``abs``, ``round``,
    ``label_replace`` …) preserve the label set of their vector operand — always
    the first argument. ``topk``/``bottomk`` also preserve input labels, but their
    vector operand is the second argument after the scalar limit. Returns ``text``
    unchanged when it is not a label-preserving function call or when the
    parentheses are unbalanced.
    """
    match = _PROMQL_LEADING_FUNC_RE.match(text)
    if not match:
        return text
    function_name = match.group(1).lower()
    if function_name in _PROMQL_LABEL_PRESERVING_FUNCS:
        operand_index = 0
    elif function_name in _PROMQL_LABEL_PRESERVING_AGG_FUNCS:
        operand_index = 1
    else:
        return text
    open_idx = match.end() - 1  # position of the '('
    depth = 0
    quote = ""
    escaped = False
    args = []
    arg_start = open_idx + 1
    for idx in range(open_idx, len(text)):
        char = text[idx]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
        elif char in ("(", "[", "{"):
            depth += 1
        elif char in (")", "]", "}"):
            depth -= 1
            if depth == 0:
                args.append(text[arg_start:idx].strip())
                if len(args) > operand_index:
                    return args[operand_index]
                return text
        elif char == "," and depth == 1:
            args.append(text[arg_start:idx].strip())
            arg_start = idx + 1
    return text  # unbalanced — leave untouched


def _allows_dashboard_label_inference(expr):
    """Return True when dashboard-wide label inference can safely add grouping.

    An ungrouped PromQL aggregation collapses its input to a single (scalar)
    series, so re-widening it with inferred labels changes the intended series
    shape. The guard therefore blocks inference for such expressions — including
    ones wrapped in unary/scalar arithmetic (``-sum(...)``, ``100 * sum(...)``,
    ``1 - avg(...)``) or label-preserving function calls
    (``clamp_max(sum(...), 100)``, ``abs(sum(...))``), which all keep the
    collapsed shape — by peeling leading signs/scalar operands and non-aggregation
    function wrappers before checking for an aggregation prefix.
    """
    text = _strip_wrapping_parentheses(expr)
    while True:
        match = _PROMQL_LEADING_FUNC_RE.match(text)
        if match and match.group(1).lower() in _PROMQL_UNLABELED_FUNCS:
            return False
        peeled = _strip_wrapping_parentheses(
            _PROMQL_LEADING_SCALAR_RE.sub("", text, count=1)
        )
        peeled = _strip_wrapping_parentheses(
            _peel_leading_parenthesized_arithmetic_operand(peeled)
        )
        match = _PROMQL_LEADING_FUNC_RE.match(peeled)
        if match and match.group(1).lower() in _PROMQL_UNLABELED_FUNCS:
            return False
        peeled = _strip_wrapping_parentheses(
            _peel_leading_function_label_operand(peeled)
        )
        if peeled == text:
            break
        text = peeled
    return not _PROMQL_AGG_PREFIX_RE.search(text)


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
    """Return top-level ``by (...)`` labels for a PromQL expression, if any.

    Collects *all* ``by``/``without`` clauses at bracket depth 0 and returns
    the label list only when every clause agrees.  A binary expression like
    ``sum by (a)(...) + sum by (b)(...)`` has two conflicting depth-0 clauses
    and therefore returns ``None``, letting the caller fall through to
    ``_promql_repeated_inner_group_cols`` (which also returns ``None`` for
    differing groups) and ultimately the ``_timeseries`` fallback.

    Outer wrapping parens — ``(sum by (namespace)(...))`` — push the ``by``
    clause to depth ≥ 1, hiding it from the scan, so we peel them first
    (issue #162). ``_trim_wrapping_parens`` only strips parens enclosing the
    whole expression, leaving ``(A) / (B)`` ratios untouched.
    """
    cleaned = _trim_wrapping_parens(cleaned)
    depth = 0
    i = 0
    found = []
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
                                found.append(["_timeseries"])
                            else:
                                found.append([part.strip() for part in cleaned[j + 1:end].split(",") if part.strip()])
        i += 1
    if not found:
        return None
    first = found[0]
    if all(group == first for group in found[1:]):
        return first
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
                            legend_format_template=None, legend_labels=None,
                            warnings=None):
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
            if "computed_value" in metric_fields:
                metric_col = "computed_value"
            else:
                return None
        return _build_esql_metric_panel(query, metric_col=metric_col)
    if kibana_type == "gauge":
        if metric_fields and len(metric_fields) > 1:
            if "computed_value" in metric_fields:
                metric_col = "computed_value"
            else:
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
    if kibana_type == "heatmap":
        return _build_esql_heatmap_panel(
            query, metric_col=metric_col, by_cols=xy_by_cols, time_fields=time_fields,
            warnings=warnings,
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
    | \[\s*\d+[smhd]\s*:\s*(?:\d+[smhd]\s*)?\]   # subquery [range:step] or default-step [range:]
    | \btopk\s*\(                                 # topk not supported by ES PROMQL bridge
    | \bbottomk\s*\(                              # bottomk not supported
    | \bchanges\s*\(                              # changes() not supported
    | \bpredict_linear\s*\(                       # predict_linear not supported
    | \blabel_replace\s*\(                        # label_replace not supported
    | \blabel_join\s*\(                           # label_join not supported
    | \bscalar\s*\(                               # scalar() triggers planner error
    | \b(?:on|ignoring)\s*\(                      # vector matching modifiers not supported
    | \bgroup_(?:left|right)\b                    # group modifiers not supported
    """,
    re.VERBOSE | re.IGNORECASE,
)

# histogram_quantile is native only on Elasticsearch >= 9.5
# (elastic/elasticsearch#150578). It is gated separately from the unconditional
# blockers above so it can pass through when the target advertises the
# PROMQL_HISTOGRAM_QUANTILE runtime feature.
_PROMQL_HISTOGRAM_QUANTILE_RE = re.compile(r"\bhistogram_quantile\s*\(", re.IGNORECASE)
# Named params in native PROMQL label matchers, for example
# ``{instance=~?instance}``. Time params such as ``?_tstart`` occur in command
# arguments and deliberately do not match this pattern.
_PROMQL_LABEL_MATCHER_PARAM_RE = re.compile(
    r"(?:=~|!~|=|!=)\s*\?[A-Za-z_][A-Za-z0-9_]*"
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

# Adaptive range resolution for dashboard panels (#272). A range plot must size
# its bucketing to the dashboard time range at view time (like Grafana's
# ``$__interval``) instead of freezing a fixed ``step=``.
#
# A *bare* stepless ``PROMQL index=...`` command is NOT valid: Elasticsearch
# rejects it at plan time with "unable to create a bucket; provide either
# [step] or all of [start], [end], and [buckets]". Do not remove these args on
# the theory that Kibana supplies the timing at render — it does not. Kibana
# substitutes ``?name`` *placeholders*; it does not synthesise missing command
# arguments, so a bare command gives it nothing to fill. The form below is what
# makes the range adaptive: ``?_tstart`` / ``?_tend`` ARE placeholders, bound to
# the dashboard time picker at render, with a fixed bucket count matching the
# ES|QL ``BUCKET(@timestamp, 50, ?_tstart, ?_tend)`` path. Adaptive and
# executable. (A previous change dropped these and broke 8 corpus panels with
# exactly the plan-time error above; ``test_native_promql_adaptive_selector_*``
# pins it.)
_NATIVE_PROMQL_ADAPTIVE_BUCKETS = 50
_NATIVE_PROMQL_ADAPTIVE_SELECTOR = (
    f"start=?_tstart end=?_tend buckets={_NATIVE_PROMQL_ADAPTIVE_BUCKETS}"
)
# Adaptive ES|QL TS/FROM calendar buckets (issue #316), unified to match Kibana
# Lens's own auto-resolution target (``AUTO_TARGET_NUMBER_OF_BUCKETS`` = 75 in
# ``@kbn/expression-XY``/Lens histogram utils) so a migrated dashboard doesn't
# render at a coarser or finer default resolution than a native Lens chart over
# the same range would. Count form needs the range args; verified on ES 9.5.
_ADAPTIVE_CHART_BUCKETS = 75
# Panels whose target applies a windowed range function (rate/irate/increase/
# delta/deriv/*_over_time) need a *coarser* bucket floor than plain gauge
# charts: RATE/IRATE only look at the last two samples in a bucket, so once a
# bucket narrows below the source scrape interval it can contain 0-1 samples
# and the function returns null for the whole series. Confirmed live on ES
# 9.5.0-SNAPSHOT: a 15m dashboard range with a 10s Prometheus scrape interval
# and the old default TBUCKET(100, ...) picks 10s buckets and IRATE is null on
# every row; TBUCKET(20, ...) picks 60s buckets (6x the scrape interval) and
# IRATE is non-null throughout. This is what fixed Node Exporter Full's
# "Interrupts Detail" panel by curated override; making it the generic default
# for every range-function panel means most dashboards no longer need that
# override at all (docs/design/esql-time-bucketing-strategy.md).
_ADAPTIVE_RATE_BUCKETS = 20
_NATIVE_ESQL_ADAPTIVE_TBUCKET = f"time_bucket = TBUCKET({_ADAPTIVE_CHART_BUCKETS}, ?_tstart, ?_tend)"
_NATIVE_ESQL_ADAPTIVE_RATE_TBUCKET = f"time_bucket = TBUCKET({_ADAPTIVE_RATE_BUCKETS}, ?_tstart, ?_tend)"
_NATIVE_FROM_ADAPTIVE_BUCKET = (
    f"time_bucket = BUCKET(@timestamp, {_ADAPTIVE_CHART_BUCKETS}, ?_tstart, ?_tend)"
)
_NATIVE_FROM_ADAPTIVE_RATE_BUCKET = (
    f"time_bucket = BUCKET(@timestamp, {_ADAPTIVE_RATE_BUCKETS}, ?_tstart, ?_tend)"
)
# Scalar panels (stat/gauge/bargauge/piechart) collapse to one row anyway, so
# generating dozens of intermediate buckets is wasteful. One bucket gives the
# same scalar result, avoids the "MAX of per-bucket averages" semantic skew for
# AVG-outer aggregations, and is far cheaper for the STATS step. (Scalar panels
# whose target uses a range function are excluded from this — see
# ``_panel_uses_range_function`` — and fall through to the rate-safe buckets
# above instead, because a range function genuinely needs resolution.)
_SCALAR_ESQL_TBUCKET = "time_bucket = TBUCKET(1, ?_tstart, ?_tend)"
_SCALAR_FROM_BUCKET = "time_bucket = BUCKET(@timestamp, 1, ?_tstart, ?_tend)"
_SCALAR_PANEL_TYPES = frozenset({"stat", "singlestat", "gauge", "bargauge", "piechart"})
_SKIP_APPROXIMATION_NOTE = "curated_skip_approximation_note"


def _grafana_panel_fixed_interval(panel) -> str | None:
    """Return a concrete Grafana panel ``interval`` suitable for PROMQL ``step=``.

    Skips empty values and Grafana macros (``$__interval`` / ``$__rate_interval``
    / …) — those stay on the adaptive path. Accepts PromQL-style durations
    (``1h``, ``5m``, ``30s``).
    """
    if not isinstance(panel, dict):
        return None
    raw = panel.get("interval")
    if raw is None or raw == "":
        return None
    text = str(raw).strip()
    if not text or text.startswith("$") or "${" in text:
        return None
    # Reject obviously non-duration tokens.
    if not re.fullmatch(r"\d+(?:\.\d+)?(ms|s|m|h|d|w)", text.replace(" ", "")):
        return None
    return text.replace(" ", "")


_RANGE_FUNCTION_RE = re.compile(
    r"\b(?:i?rate|increase|delta|deriv|[a-z_]+_over_time)\s*\(", re.IGNORECASE
)


def _panel_uses_range_function(panel) -> bool:
    """Whether any target applies a windowed PromQL function.

    A rate is only a rate when it is evaluated at a sensible resolution.
    Collapsing one to ``TBUCKET(1, ?_tstart, ?_tend)`` -- a single bucket
    spanning the whole dashboard window -- does not merely lose detail, it
    returns a different number: Node Exporter Full's "CPU Busy" read
    avg(rate(idle)) as 10.75 instead of 0.98 and rendered -192% CPU, while the
    same query at TBUCKET(50) matched Prometheus exactly.

    So the scalar-panel bucket optimisation applies only to panels that do NOT
    use a range function; those genuinely collapse to one row and lose nothing.
    """
    for target in (panel or {}).get("targets") or []:
        if not isinstance(target, dict):
            continue
        if _RANGE_FUNCTION_RE.search(str(target.get("expr") or "")):
            return True
    return False


# Reducers whose value is unchanged when the range collapses to ONE bucket.
# mean/min/max/sum over the whole range equal themselves; a "last" reducer does
# not -- it needs enough resolution for a final bucket to exist to be last.
_ONE_BUCKET_SAFE_REDUCE_CALCS = frozenset({
    "mean", "avg", "average", "min", "max", "sum", "total", "count",
})


def _reduce_calc_survives_one_bucket(reduce_calc: str) -> bool:
    """Whether collapsing to a single whole-range bucket preserves the reducer.

    The scalar-panel bucket optimisation replaces adaptive ``TBUCKET(75, ...)``
    with ``TBUCKET(1, ...)``. That is sound for an order-independent reducer, but
    it silently redefines a ``lastNotNull`` panel: with one bucket spanning the
    dashboard window, ``AVG(field)`` is the RANGE MEAN, not the current value.
    Node Exporter Full's "Sys Load" read 3.48 against Grafana's 6.2 for exactly
    this reason, and ``lastNotNull`` is Grafana's DEFAULT for stat/gauge -- so an
    absent ``calcs`` must be treated as "last" too, not as safe.
    """
    return (reduce_calc or "").strip().lower() in _ONE_BUCKET_SAFE_REDUCE_CALCS


def _rule_pack_for_panel(rule_pack: RulePackConfig, panel) -> RulePackConfig:
    """Overlay per-panel bucket sizing onto a rule pack copy (issue #316).

    Dashboard panels use adaptive ``TBUCKET(75, ?_tstart, ?_tend)`` by default
    (matching Kibana Lens's own auto-resolution target) so zooming changes
    resolution. Panels whose target applies a windowed range function
    (rate/irate/increase/delta/deriv/*_over_time) instead get the coarser
    ``TBUCKET(20, ...)`` floor, because RATE/IRATE only look at the last two
    samples per bucket and go null once a bucket narrows below the source
    scrape interval. Scalar panels (stat/gauge/bargauge/piechart) that do NOT
    use a range function use ``TBUCKET(1, ...)`` — they collapse to one row
    anyway, so dozens of intermediate buckets are wasteful and skew AVG-outer
    queries toward the peak bucket. An explicit Grafana panel ``interval``
    becomes a fixed ``TBUCKET(<duration>)`` and wins over all of the above.
    Direct ``translate_promql_to_esql`` callers keep ``rule_pack.ts_bucket``
    unchanged.
    """
    panel_type = str((panel or {}).get("type") or "").lower()
    uses_range_function = _panel_uses_range_function(panel)
    is_scalar = (
        panel_type in _SCALAR_PANEL_TYPES
        and not uses_range_function
        and _reduce_calc_survives_one_bucket(_panel_reduce_calc(panel))
    )
    interval = _grafana_panel_fixed_interval(panel)
    new_bucket = None
    new_from_bucket = None
    if interval:
        from observability_migration.adapters.source.grafana.esql_validate import (
            _promql_window_to_esql_interval,
        )

        esql_dur = _promql_window_to_esql_interval(interval)
        if esql_dur:
            new_bucket = f"time_bucket = TBUCKET({esql_dur})"
    elif rule_pack.ts_bucket == "time_bucket = TBUCKET(5 minute)":
        # Adaptive auto resolution for dashboard panels (issue #316).
        if is_scalar:
            new_bucket = _SCALAR_ESQL_TBUCKET
            new_from_bucket = _SCALAR_FROM_BUCKET
        elif uses_range_function:
            new_bucket = _NATIVE_ESQL_ADAPTIVE_RATE_TBUCKET
            new_from_bucket = _NATIVE_FROM_ADAPTIVE_RATE_BUCKET
        else:
            new_bucket = _NATIVE_ESQL_ADAPTIVE_TBUCKET
            new_from_bucket = _NATIVE_FROM_ADAPTIVE_BUCKET
    if new_bucket is None or new_bucket == rule_pack.ts_bucket:
        return rule_pack
    kwargs = {"ts_bucket": new_bucket}
    if new_from_bucket and new_from_bucket != rule_pack.from_bucket:
        kwargs["from_bucket"] = new_from_bucket
    updated = replace(rule_pack, **kwargs)
    # ``replace`` only copies dataclass fields; propagate all dynamic attributes
    # (plugin pack markers, regex defaults, validators, runtime notes, etc.) so
    # that plugins whose marker-checks use ``context.rule_pack`` still fire after
    # a per-panel bucket adjustment.
    _field_names = {f.name for f in fields(rule_pack)}
    for attr, val in vars(rule_pack).items():
        if attr not in _field_names:
            setattr(updated, attr, val)
    return updated

# Grafana's adaptive step macros ($__rate_interval / $__interval / $interval /
# $__auto_interval_*). Grafana sizes these from the panel width and selected time
# range at view time, i.e. they encode "size the lookback to the view", not a
# fixed duration. Each spelling is accepted both unbraced (``$__rate_interval``)
# and braced (``${__rate_interval}``) — Grafana treats the two as identical, so
# the braced form carries the same adaptive intent (issue #273 review).
_GRAFANA_ADAPTIVE_INTERVAL_MACRO_NAME = (
    r"(?:__rate_interval|__interval|interval|__auto_interval_\w+)"
)
# The unbraced ``(?![\w])`` keeps ``$__interval`` from matching the prefix of a
# longer custom variable like ``$__interval_ms``; the braced form is delimited by
# ``}`` so the same over-match is impossible there.
_GRAFANA_ADAPTIVE_INTERVAL_MACRO = (
    r"(?:\$"
    + _GRAFANA_ADAPTIVE_INTERVAL_MACRO_NAME
    + r"(?![\w])|\$\{\s*"
    + _GRAFANA_ADAPTIVE_INTERVAL_MACRO_NAME
    + r"\s*\})"
)
# A ``rate(...)`` / ``increase(...)`` whose range-selector window is one of those
# adaptive macros: ``rate(metric{labels}[$__rate_interval])``. The vector
# selector (group 1) is preserved and the ``[<macro>]`` window is dropped so the
# native PROMQL command emits a *windowless* rate/increase, letting Elastic size
# the lookback to the step at view time (issue #273). Only ``rate``/``increase``
# are matched: Elastic's windowless form is confirmed for those two; other range
# functions (``irate``, ``*_over_time``, ``delta`` …) keep a fixed window because
# a windowless form for them is not confirmed and could emit an invalid query.
# The selector body forbids ``(``/``)`` so a range-on-nonselector shape (already
# rejected upstream) is never rewritten here.
#
# The vector selector accepts every shape ``can_use_native_promql`` does: a
# metric name (``metric``), a metric name with a label set (``metric{job="api"}``),
# or a selector-only vector with no leading metric name (``{__name__="m"}`` /
# ``{job="api"}``). Without the selector-only branch #273 would silently freeze
# those brace-only vectors to ``[5m]`` (issue #273 review).
#
# ``\s*`` before the ``{`` tolerates upstream dashboards that space out the
# selector (``metric {job="api"}``); the fixed-window fallback below collapses
# that space, and this rewrite runs first, so without it #273 would silently
# miss the spaced form and freeze it to ``[5m]``.
#
# The label set is matched by ``_PROMQL_LABEL_SELECTOR`` (defined just below),
# which is quote-aware so a value containing braces stays adaptive (#273 review).
#
# The trailing negative lookahead leaves a range vector that carries an
# ``offset`` / ``@`` modifier alone: ``rate(foo[$__rate_interval] offset 5m)``
# must not become ``rate(foo offset 5m)`` (a windowless-with-modifier form that
# is not confirmed and drops the range before the modifier). Skipping the match
# lets it fall through to the fixed-window path -> ``rate(foo[5m] offset 5m)``,
# the same valid query the pre-#273 code emitted.
#
# A PromQL label set ``{...}`` whose quoted values may contain ``{``/``}`` (e.g.
# ``{route="/api/{id}"}``). A naive ``[^{}]*`` stops at the first brace inside
# such a value, so scan the body as double-/single-quoted string literals (with
# escapes) or any char that is not a brace or quote. The alternatives are
# mutually exclusive on their first char, so the outer ``*`` cannot backtrack
# catastrophically.
_PROMQL_LABEL_SELECTOR = (
    r"\{(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^{}\"'])*\}"
)
_RATE_INCREASE_ADAPTIVE_WINDOW_RE = re.compile(
    r"(\b(?:rate|increase)\s*\(\s*"
    r"(?:[A-Za-z_:][A-Za-z0-9_:]*(?:\s*" + _PROMQL_LABEL_SELECTOR + r")?"
    r"|" + _PROMQL_LABEL_SELECTOR + r"))"
    r"\s*\[\s*" + _GRAFANA_ADAPTIVE_INTERVAL_MACRO + r"\s*\]"
    r"(?!\s*(?:offset\b|@))"
)


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

    return _map_promql_brace_selectors(expr, rewrite_selector)


def _map_promql_brace_selectors(expr, rewrite_selector):
    """Rewrite the contents of every top-level ``{...}`` PromQL selector."""
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


def _strip_ignored_promql_label_matchers(expr, ignored_labels):
    """Drop selector matchers whose label is in the rule-pack ignore list.

    Native PROMQL keeps Prometheus label names inside ``{}``. Pack
    ``ignored_labels`` already omit those filters from ES|QL WHERE clauses, but
    without this strip the native path still emits ``{release=~?release}``.
    Kibana then synthesizes a Release control from mixed ``metrics-*`` (often a
    kernel ``release`` field) and every panel that still binds ``?release``
    goes empty.
    """
    drop = {
        str(name).strip()
        for name in (ignored_labels or [])
        if str(name).strip()
    }
    if not drop or not expr:
        return expr

    def rewrite_selector(selector_text):
        kept = []
        changed = False
        for part in _split_top_level_csv(selector_text):
            matcher = _NATIVE_PROMQL_LABEL_MATCHER_RE.match(part)
            if matcher and matcher.group("label").strip() in drop:
                changed = True
                continue
            kept.append(part)
        if not changed:
            return selector_text
        return ", ".join(kept)

    rewritten = _map_promql_brace_selectors(expr, rewrite_selector)
    return re.sub(r"([A-Za-z_:][A-Za-z0-9_:]*)\{\s*\}", r"\1", rewritten)


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


# A range selector ``[<range>]`` (no resolution colon) immediately following a
# closing paren is applied to an aggregation/function result, e.g.
# ``irate(sum by (job)(metric)[5m])``. This is invalid PromQL — ranges may only
# follow a vector selector — and the ES native PROMQL engine rejects it at parse
# time ("ranges only allowed for vector selectors"). A *valid* range follows a
# selector (``metric[5m]``); a *valid* range over an expression is a subquery
# with a colon (``(...)[5m:]``), which is blocked separately by
# ``_PROMQL_UNSUPPORTED_RE``. So a colon-less ``)[...]`` is unambiguously the
# malformed shape.
_PROMQL_RANGE_ON_NONSELECTOR_RE = re.compile(r"\)\s*\[[^\]:]*\]")


def _promql_has_range_on_nonselector(expr):
    """True when a range selector is applied to a non-selector (a closing
    paren), e.g. ``irate(sum(metric)[5m])`` — invalid PromQL that the native
    PROMQL engine rejects at parse time. Such panels must degrade via the ES|QL
    translator (which marks the shape not_feasible) instead of emitting a native
    query that hard-errors in Kibana."""
    stripped = _strip_promql_string_literals(expr)
    return bool(_PROMQL_RANGE_ON_NONSELECTOR_RE.search(stripped))


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
    expr, runtime_features=None, regex_default_params=None, adaptive_window=False
):
    """Strip Grafana template variables from a PromQL expression so it can be
    sent to the ES PROMQL engine which does not know about ``$var`` syntax.

    When *adaptive_window* is set, a ``rate``/``increase`` whose window is a
    Grafana adaptive macro (``$__rate_interval`` etc.) is emitted *windowless*
    so Elastic sizes the lookback to the view at query time, preserving
    Grafana's adaptive behavior instead of freezing a fixed window (issue #273).
    Explicit numeric windows (``[5m]``) are always kept verbatim. This form does
    not parse under ``promql-parser`` (windowless rate is a type error there), so
    callers that still need to parse the expression must clean without this flag.
    """
    had_bare_variable = False
    expr = substitute_grafana_range_macros(expr)
    # #273: a Grafana adaptive-window macro on rate()/increase() means "size the
    # lookback to the view", so drop the window and let the native PROMQL command
    # pick it from the step. Runs before the fixed-window substitution below so
    # the macro never collapses to a frozen ``[5m]`` first. Explicit windows and
    # other range functions fall through to the fixed-window handling.
    if adaptive_window:
        expr = _RATE_INCREASE_ADAPTIVE_WINDOW_RE.sub(r"\1", expr)
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
    # Same gap after a label-selector close brace, e.g. ``foo{job="api"} [5m]``.
    # This shows up on the fixed-window path (explicit windows, or an adaptive
    # macro kept fixed because of a trailing offset/@ modifier, issue #273), so
    # normalize it too rather than emit a malformed ``} [`` range.
    expr = re.sub(r"\}\s+([\[{])", r"}\1", expr)

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


def _clean_promql_for_native(
    expr, runtime_features=None, regex_default_params=None, adaptive_window=False
):
    cleaned, _ = _clean_promql_for_native_with_state(
        expr,
        runtime_features=runtime_features,
        regex_default_params=regex_default_params,
        adaptive_window=adaptive_window,
    )
    return cleaned


# A PromQL string literal (label value); its contents must never be rewritten.
_PROMQL_STRING_LITERAL_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
# A candidate PromQL identifier: a metric name, function name, keyword, or label
# key. Anchored so it never matches inside a larger identifier or an already
# dot-qualified name (`attributes.job` — the `job` is skipped).
_PROMQL_IDENT_RE = re.compile(r"(?<![\w.])([A-Za-z_:][A-Za-z0-9_:]*)")
# Vector-matching / aggregation modifiers that introduce a parenthesised list of
# *label* names (`by (a, b)`, `on(x) group_left(y)`), never metric selectors.
_PROMQL_GROUPING_MODIFIERS = frozenset(
    {"by", "without", "on", "ignoring", "group_left", "group_right"}
)
# Aggregation operators. In the usual `sum(...)` form they are caught by the
# function-call guard (identifier followed by `(`), but in the modifier-before-
# args form (`sum by (job) (metric)`) the operator is followed by `by`/`without`
# instead, so it must be reserved explicitly.
_PROMQL_AGG_OPERATORS = frozenset(
    {
        "sum", "min", "max", "avg", "group", "count", "count_values",
        "stddev", "stdvar", "topk", "bottomk", "quantile", "limitk", "limit_ratio",
    }
)
# PromQL words that read as identifiers but are never metric selectors, so they
# must not be prefixed even if a colliding `metrics.<word>` field exists. The
# grouping modifiers are included for the case where they appear without a
# following `(` (`a * group_left b`).
_PROMQL_RESERVED_WORDS = _PROMQL_GROUPING_MODIFIERS | _PROMQL_AGG_OPERATORS | frozenset(
    {"offset", "bool", "and", "or", "unless", "atan2", "start", "end", "inf", "nan"}
)


def _promql_label_names(expr):
    """Return label names used by matchers or grouping modifiers."""
    labels = set()
    for selector in re.findall(r"\{([^{}]*)\}", str(expr or "")):
        labels.update(
            re.findall(
                r"(?:^|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=~|!~|!=|=)",
                selector,
            )
        )
    sanitized = _strip_promql_string_literals(str(expr or ""))
    for group in re.findall(
        r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)",
        sanitized,
        re.IGNORECASE,
    ):
        labels.update(name.strip() for name in group.split(",") if name.strip())
    # __name__ is a metric selector, not a stored label field.
    labels.discard("__name__")
    return labels


def _promql_uses_rule_pack_label_overrides(expr, rule_pack):
    """Whether native PROMQL would bypass an explicit label rule."""
    labels = _promql_label_names(expr)
    overridden = set(getattr(rule_pack, "label_rewrites", {}))
    overridden.update(getattr(rule_pack, "ignored_labels", []))
    return bool(labels & overridden)


def _record_passthrough_native_labels(expr, resolver):
    """Validate raw native-PROMQL labels without remapping the expression."""
    if not getattr(resolver, "_passthrough", False):
        return
    resolve = getattr(resolver, "resolve_label", None)
    if not callable(resolve):
        return
    for label in sorted(_promql_label_names(expr)):
        resolve(label)


def _native_metric_map_binding(resolver, metric_name: str):
    """Return a native-PromQL-compatible metric_map binding, if any."""
    resolve = getattr(resolver, "resolve_metric_map_result", None)
    if not callable(resolve):
        return None
    try:
        result = resolve(metric_name)
    except Exception:
        return None
    if result is None:
        return None
    from observability_migration.core.metric_mapping import binding_from_result

    binding = binding_from_result(result)
    return binding if binding.native_promql_compatible else None


def _recording_rule_metric_map_notes(metric_names, rule_pack) -> list[str]:
    from observability_migration.core.metric_mapping import looks_like_recording_rule_metric

    metric_map = getattr(rule_pack, "metric_map", None) or {}
    notes: list[str] = []
    for metric_name in sorted(set(metric_names)):
        if not looks_like_recording_rule_metric(metric_name):
            continue
        if metric_name in metric_map:
            continue
        notes.append(
            "Recording-rule metric "
            f"{metric_name!r} has no metric_map entry; add a mapping or "
            "recreate the rule in the target"
        )
    return notes


def _prefix_native_metric_fields(expr, resolver):
    """Rewrite bare metric selectors in a native PROMQL expression to their
    resolved ``metrics.<name>`` field (issue #270).

    OTel Collector (``prometheusreceiver``) indices store each metric under a
    ``metrics.<name>`` prefix, so a native ``value=(<bare>)`` command finds no
    field and the panel renders empty. Only a token in *metric-selector*
    position is rewritten; the scan tracks PromQL structure so that identifiers
    which merely look like metric names are protected regardless of the target's
    field caps:

    - label-matcher keys and values inside ``{...}``;
    - grouping labels inside a modifier's parens (``by (x)``, ``on(y)``,
      ``group_left(z)``);
    - reserved words (``offset``/``bool``/``and``/``or``/``unless``/…);
    - function names (any identifier directly followed by ``(``);
    - string literals (label values).

    The one label position that *is* rewritten is the ``__name__`` metric-name
    matcher: ``{__name__="foo"}`` is equivalent to selecting ``foo``, so its
    *exact*-match value takes the same field-cache-gated prefix. Regex
    (``=~``) and negative (``!=``/``!~``) ``__name__`` matchers are left bare —
    prefixing a regex is fragile and negation would change the selection.

    Both guards matter and neither is sufficient alone. A ``metrics.<token>``
    field can legitimately exist for a *label* name that collides with a metric
    name in the target, so ``resolve_metric_field`` / ``field_exists`` cannot
    distinguish a selector from a label key — only the structural position can.
    Conversely, ``resolve_metric_field`` returns ``metrics.<name>``
    unconditionally under the ``prometheus_native`` profile (for the preflight
    contract), so the resolved prefix is still confirmed against the field cache
    (``field_exists``) before it is applied.

    Deliberately runs on the *emitted* expression string, after AST analysis
    (counter/gauge detection, group-column shape) has already read the bare
    names — the resolver is keyed on the bare metric name, and the PromQL parser
    would choke on a dotted metric selector.
    """
    if resolver is None or not expr:
        return expr
    resolve = getattr(resolver, "resolve_metric_field", None)
    if not callable(resolve):
        return expr
    field_exists = getattr(resolver, "field_exists", None)

    def _prefixed_field_is_indexed(name):
        # Confirm ``metrics.<name>`` is a real field and the bare token is not,
        # so we never invent a prefix for an index that stores the metric bare.
        # When the resolver can't answer (no ``field_exists`` / empty cache),
        # fall back to the resolver's decision.
        if not callable(field_exists):
            return True
        prefixed = field_exists(f"metrics.{name}")
        if prefixed is None:
            return True
        return bool(prefixed) and not field_exists(name)

    def _resolve_selector(name, tail):
        # A trailing `(` marks a function call, never a metric selector.
        if tail.lstrip()[:1] == "(":
            return None
        if name in _PROMQL_RESERVED_WORDS:
            return None
        binding = _native_metric_map_binding(resolver, name)
        if binding is not None:
            return binding.target_field
        try:
            resolved = resolve(name)
        except Exception:
            return None
        if resolved != f"metrics.{name}":
            return None
        return resolved if _prefixed_field_is_indexed(name) else None

    def _rewrite_name_matcher_value(literal_text):
        # `{__name__="foo"}` is the metric-name matcher — equivalent to selecting
        # ``foo`` — so its *exact*-match value takes the same field-cache-gated
        # ``metrics.`` prefix a bare selector would. Only plain (non-regex)
        # values reach here; the gate still confirms the prefixed field exists
        # and the bare name does not, so this is a no-op on bare-metric targets.
        quote = literal_text[:1]
        if quote not in "\"'" or len(literal_text) < 2:
            return literal_text
        value = literal_text[1:-1]
        resolved = _resolve_selector(value, "")
        return f"{quote}{resolved}{quote}" if resolved is not None else literal_text

    out = []
    i = 0
    n = len(expr)
    brace_depth = 0
    # `__name__` exact-match matcher tracking: `name_key_pending` after the
    # ``__name__`` key, `name_value_exact` once an exact ``=`` (not ``=~``/
    # ``!=``/``!~``) confirms the next string literal is the metric name.
    name_key_pending = False
    name_value_exact = False
    # One flag per open paren: True when the paren is a grouping-label list, so
    # its contents are labels, not metrics. `expect_grouping` arms the next `(`
    # after a grouping modifier keyword.
    paren_is_grouping = []
    expect_grouping = False
    while i < n:
        ch = expr[i]
        if ch in "\"'":
            literal = _PROMQL_STRING_LITERAL_RE.match(expr, i)
            if literal:
                text = literal.group(0)
                out.append(
                    _rewrite_name_matcher_value(text) if name_value_exact else text
                )
                name_value_exact = False
                i = literal.end()
                continue
            out.append(ch)
            i += 1
            continue
        if ch == "{":
            brace_depth += 1
            out.append(ch)
            i += 1
            continue
        if ch == "}":
            brace_depth = max(0, brace_depth - 1)
            out.append(ch)
            i += 1
            continue
        if ch == "(":
            paren_is_grouping.append(expect_grouping)
            expect_grouping = False
            out.append(ch)
            i += 1
            continue
        if ch == ")":
            if paren_is_grouping:
                paren_is_grouping.pop()
            out.append(ch)
            i += 1
            continue
        ident = _PROMQL_IDENT_RE.match(expr, i)
        if ident:
            name = ident.group(1)
            protected = brace_depth > 0 or (
                bool(paren_is_grouping) and paren_is_grouping[-1]
            )
            resolved = None if protected else _resolve_selector(name, expr[ident.end():])
            out.append(resolved if resolved is not None else name)
            # Arm grouping only when a bare modifier keyword is immediately
            # followed by its paren list; any other identifier clears it.
            expect_grouping = name in _PROMQL_GROUPING_MODIFIERS
            # A `__name__` key inside `{...}` arms an exact-value rewrite; any
            # other identifier ends a pending matcher.
            name_key_pending = brace_depth > 0 and name == "__name__"
            name_value_exact = False
            i = ident.end()
            continue
        # Any other character (operators, brackets, whitespace).
        if name_key_pending and ch == "=":
            # Exact `=`, unless it is the `=~` regex matcher (never rewritten).
            name_value_exact = expr[i + 1:i + 2] != "~"
            name_key_pending = False
        elif name_key_pending and ch == "!":
            # `!=` / `!~`: negative matcher — prefixing would change meaning.
            name_key_pending = False
        elif not ch.isspace():
            # Any other non-space token ends a pending `__name__` matcher.
            name_key_pending = False
            name_value_exact = False
        # Only whitespace may sit between a grouping modifier and its `(`;
        # anything else cancels a pending grouping expectation.
        if not ch.isspace():
            expect_grouping = False
        out.append(ch)
        i += 1
    return "".join(out)


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


def _panel_static_legend_label(panel):
    """Return the one unambiguous static legend shared by visible targets."""
    labels = []
    for target in panel.get("targets") or []:
        if not isinstance(target, dict) or target.get("hide"):
            continue
        label = _static_legend_label(target.get("legendFormat", ""))
        if not label:
            return ""
        if label not in labels:
            labels.append(label)
    return labels[0] if len(labels) == 1 else ""


_PLACEHOLDER_VALUE_METRIC_FIELDS = frozenset({"value", "computed_value"})


def _label_placeholder_value_metric(yaml_panel, *, title, legend_format=""):
    """Give a synthetic ``value``/``computed_value`` metric column a real label.

    Both the native-PROMQL path (single ``value=(...)`` column) and the
    general ES|QL translator (single ``computed_value`` scalar-expression
    column) collapse a panel's target(s) into one numeric column with a
    placeholder name. Kibana's Lens then falls back to that raw column name
    -- ``value``/``computed_value`` -- as the axis/legend label (issue #351).
    Curated-pack overrides that fuse multiple metrics into one ``value``
    column via ``EVAL value = ...`` hit the same gap.

    Uses the same fallback as a single-target panel: the target's static
    legend text if the operator set one, otherwise the panel title.
    """
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
        if metric.get("field") not in _PLACEHOLDER_VALUE_METRIC_FIELDS:
            continue
        metric.setdefault("label", fallback_label)


def build_native_promql_query(promql_expr, index="metrics-prometheus-*",
                              legend_labels=None, kibana_type=None,
                              legend_format=None, runtime_features=None,
                              instant=False, regex_default_params=None, step=None,
                              resolver=None, adaptive_step=False):
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
    # A windowless rate()/increase() only makes sense on a range query, whose
    # step Elastic auto-sizes; an instant tile has no step, so keep its window
    # fixed. So the adaptive window (#273) rides on the adaptive step (#272) and
    # applies only to range dashboard panels (``adaptive_step`` and not instant).
    adaptive_window = adaptive_step and not instant
    cleaned = _clean_promql_for_native(
        promql_expr,
        runtime_features=runtime_features,
        regex_default_params=regex_default_params,
        adaptive_window=adaptive_window,
    )
    cleaned = _prefix_native_metric_fields(cleaned, resolver)

    # An instant query evaluates the expression at a single point (the Kibana
    # time-picker end, ``?_tend``) and returns one row per series = the current
    # value, with NO ``step`` time column. A range query walks ``step=`` buckets
    # and emits a ``step`` column to plot against. Single-value tiles
    # (metric/gauge) and table-format ``instant: true`` targets are instant
    # (issues #127, #102); everything else is a range plot. ``time=?_tend`` is
    # opt-in: callers that post-process the ``step`` column (e.g. the alert
    # ``LAST(value, step)`` reduction) leave ``instant`` False to keep ``step=``.
    #
    # Range resolution follows this precedence:
    #   1. instant           -> ``time=?_tend`` (no step column)
    #   2. explicit ``step`` -> ``step=<value>`` (e.g. a migrated Grafana alert
    #      honoring the source query interval, issue #209)
    #   3. ``adaptive_step`` -> emit bare ``PROMQL index=... value=(...)`` (no
    #      timing args). In a Kibana dashboard panel the time range comes from
    #      the dashboard time picker — Kibana injects it at render time, so
    #      the command resolves to the current view window (issues #272, #318).
    #      A raw ES ``_query`` call without timing args is rejected; this path
    #      is only used for Kibana dashboard panels, not alerts or direct calls.
    #      The command still emits the ``step`` time column.
    #   4. otherwise         -> the documented ``step=1m`` default, preserving
    #      historical behavior for direct callers that opt into neither.
    if instant:
        selector = "time=?_tend"
    elif step:
        selector = f"step={str(step).strip()}"
    elif adaptive_step:
        selector = _NATIVE_PROMQL_ADAPTIVE_SELECTOR
    else:
        selector = f"step={_DEFAULT_NATIVE_PROMQL_STEP}"

    header = f"PROMQL index={index}" + (f" {selector}" if selector else "")

    if kibana_type in ("metric", "gauge"):
        return f'{header} value=({cleaned})'

    base = f'{header} value=({cleaned})'

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


def _promql_fragment_has_nested_agg(frag) -> bool:
    """True when *frag* (or a binary operand) is a PromQL nested aggregation.

    Nested aggregations such as ``count(count(...) by (cpu))`` are valid PromQL
    and the ES|QL translator handles them, but Elasticsearch's native PROMQL
    command often rejects them at runtime. Detect via the AST family rather than
    a brittle regex so ``max(avg_over_time(...))`` (range_agg) stays native.
    """
    if frag is None:
        return False
    family = str(getattr(frag, "family", "") or "")
    if family == "nested_agg":
        return True
    if family == "binary_expr":
        extra = getattr(frag, "extra", {}) or {}
        return _promql_fragment_has_nested_agg(extra.get("left_frag")) or _promql_fragment_has_nested_agg(
            extra.get("right_frag")
        )
    return False


def _promql_has_nested_aggregation(promql_expr) -> bool:
    if not promql_expr or not str(promql_expr).strip():
        return False
    try:
        cleaned = _clean_promql_for_native(promql_expr)
        return _promql_fragment_has_nested_agg(_parse_fragment(cleaned or promql_expr))
    except Exception:
        return False


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
    if _PROMQL_HISTOGRAM_QUANTILE_RE.search(sanitized) and not is_feature_supported(
        runtime_features, PROMQL_HISTOGRAM_QUANTILE
    ):
        return False
    if _promql_has_unsupported_comparison(promql_expr):
        return False
    if _promql_has_known_server_bug(promql_expr):
        return False
    if _promql_has_range_on_nonselector(promql_expr):
        return False
    if _promql_has_nested_aggregation(promql_expr):
        return False
    return True


def _kibana_binds_promql_control_params(runtime_features=None) -> bool:
    return is_feature_supported(runtime_features, KIBANA_PROMQL_CONTROL_PARAMS)


_COUNTER_RANGE_FUNC_PATTERN = re.compile(
    r"\b(?P<func>rate|irate|increase)\s*\(\s*(?P<metric>[A-Za-z_:][A-Za-z0-9_:]*)\b",
    re.IGNORECASE,
)


def _is_bare_instant_selector(promql_expr) -> bool:
    """True when *promql_expr* is a bare instant-vector selector (gauge or counter)."""
    if not promql_expr:
        return False
    try:
        frag = _parse_fragment(promql_expr)
    except Exception:
        return False
    return bool(frag and getattr(frag, "family", None) == "simple_metric")


def _is_bare_counter_reference(promql_expr, resolver, rule_pack=None):
    """Return True if *promql_expr* is a bare counter instant-vector selector.

    A bare counter reference (e.g. ``http_requests_total{job="api"}``) with no
    ``rate()``/``irate()``/``increase()`` and no aggregation is a perfectly valid
    native PROMQL query: Kibana's PROMQL preview serves the raw cumulative value
    directly. The single-value-tile guard (metric/gauge) otherwise routes these
    to an ES|QL ``MAX(LAST_OVER_TIME(...))`` fallback with a misleading
    "Counter referenced without rate()" warning (issue #139). Detecting the case
    here lets the native path keep the original expression instead.

    Only the ``simple_metric`` family qualifies, which excludes rate/range
    functions (``range_agg``), explicit aggregations (``simple_agg``), and
    arithmetic (``binary_expr``) — those keep their existing behavior.
    """
    if not promql_expr:
        return False
    try:
        frag = _parse_fragment(promql_expr)
    except Exception:
        return False
    if not frag or getattr(frag, "family", None) != "simple_metric":
        return False
    metric = getattr(frag, "metric", "") or ""
    if not metric:
        return False
    if resolver is not None:
        return bool(resolver.is_counter(metric))
    return _is_counter_fallback(metric, rule_pack)


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


# Substrings that mark a target-side *parse* rejection of a native PROMQL query
# — the cluster cannot even understand the query syntax, so degrading to ES|QL
# is the right call. Field/index gaps ("Unknown column", "Unknown index",
# "verification_exception") are data-readiness conditions, NOT parse errors: the
# native query is structurally valid and self-heals once telemetry lands, so we
# keep it native (issue #158 / #154).
_NATIVE_PARSE_REJECTION_SIGNALS = (
    "mismatched input",
    "parsing_exception",
    "parse_exception",
    "extraneous input",
    "no viable alternative",
    "syntax error",
    "invalid date format",
    "input mismatch",
    "token recognition error",
    "cannot parse",
)


def _native_query_parse_rejected(err) -> bool:
    """Return True only when *err* describes a target *parse* rejection.

    A parse rejection means the native PROMQL query is malformed for this target
    and must degrade to ES|QL. Empty errors and data/field gaps return False so
    the native path is preserved.
    """
    text = str(err or "").lower()
    if not text:
        return False
    return any(signal in text for signal in _NATIVE_PARSE_REJECTION_SIGNALS)


def _native_promql_query_survives_validation(rule_pack, query) -> bool:
    """Run the rule pack's optional live native-PROMQL validator against a built
    native query.

    Returns False only when the validator is present AND the query is rejected at
    parse time, signalling the caller to degrade to ES|QL. An absent validator
    (offline) or a non-parse error (data/field gap, flaky transport) keeps the
    native path.

    Records per-panel decisions on ``rule_pack.native_validation_stats`` so the
    migrate CLI can print an observable summary. The validator may be cached by
    query upstream, but this gate runs once per panel, so counts reflect
    panel-level decisions.
    """
    validator = getattr(rule_pack, "native_promql_validator", None)
    if validator is None:
        return True
    stats = getattr(rule_pack, "native_validation_stats", None)
    if not isinstance(stats, dict):
        stats = {"checked": 0, "degraded": 0, "kept": 0}
        try:
            rule_pack.native_validation_stats = stats
        except Exception:
            pass
    stats["checked"] = stats.get("checked", 0) + 1
    try:
        ok, err = validator(query)
    except Exception:
        # A flaky/unavailable validator must never block migration; keep native.
        stats["kept"] = stats.get("kept", 0) + 1
        return True
    if ok or not _native_query_parse_rejected(err):
        stats["kept"] = stats.get("kept", 0) + 1
        return True
    stats["degraded"] = stats.get("degraded", 0) + 1
    return False


def _metric_map_bypass_note(metric_names, rule_pack):
    """Warn when native PROMQL cannot apply a class-2 ``metric_map`` entry.

    Class-1 exact mappings are rewritten in native PromQL via
    ``_prefix_native_metric_fields``. Class-2 mappings need ES|QL so
    transform/filter/scale semantics are honored.
    """
    metric_map = getattr(rule_pack, "metric_map", None) or {}
    if not metric_map:
        return None
    from observability_migration.core.metric_mapping import binding_from_result, resolve_metric_map

    class2_mapped: list[str] = []
    for name in metric_names:
        if name not in metric_map:
            continue
        result = resolve_metric_map(name, metric_map)
        if result is None:
            continue
        binding = binding_from_result(result)
        if not binding.native_promql_compatible:
            class2_mapped.append(name)
    if not class2_mapped:
        return None
    mapped = sorted(set(class2_mapped))
    return (
        f"metric_map class-2 entries not applied for {', '.join(mapped)}: "
        "native PROMQL cannot honor transform/filter/scale; use ES|QL translation "
        "(--metric-map-file auto-selects it, or pass --translation-mode esql)"
    )


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
    collapsed_expr = collapse_or_for_native_promql(
        expr, resolver=resolver, rule_pack=rule_pack
    )
    if collapsed_expr != expr:
        _append_unique(
            panel_notes,
            "PromQL same-metric 'or': preferred left range-window operand and "
            "dropped the alternate-window fallback; Grafana uses the right "
            "side only when the left lacks samples",
        )
        expr = collapsed_expr
    expr = _strip_ignored_promql_label_matchers(
        expr, getattr(rule_pack, "ignored_labels", None)
    )
    # Native PROMQL is attempted before the ES|QL live-missing loop. An
    # absent instant gauge would otherwise stay native, score Green, and
    # either smoke empty or 400 with ``value_$1``/``value_$2`` (issue #158
    # keeps native on field gaps *after* emit; this gate refuses emit when
    # field-caps already proved the source metrics are gone).
    if _live_missing_metrics_for_expr(expr, resolver):
        return None
    runtime_features = getattr(rule_pack, "runtime_features", {})
    _record_passthrough_native_labels(expr, resolver)
    if (
        getattr(resolver, "_passthrough", False)
        and _promql_uses_rule_pack_label_overrides(expr, rule_pack)
    ):
        _append_unique(
            panel_notes,
            "Native PROMQL skipped: explicit label rules require ES|QL field resolution",
        )
        return None
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
    # A control-bound label-matcher variable (e.g. ``{instance=~"$instance"}``)
    # is rewritten to ``{instance=~?instance}`` inside the opaque PromQL string.
    # ES 9.5+ accepts ``?param`` in PromQL label filters when the HTTP request
    # supplies the param in its body — ``PROMQL_LABEL_MATCHER_PARAMS`` probes
    # this ES-side capability. Kibana forwards dashboard control values into
    # that inner PromQL context on builds >= 9.5
    # (``KIBANA_PROMQL_CONTROL_PARAMS``). Migration prefers that native path by
    # default; only a verified Kibana older than 9.5 forces ES|QL where the
    # filter lands in ``WHERE … RLIKE ?instance``. Other construct / validator
    # gates can still degrade individual panels to ES|QL.
    if (
        _promql_label_matcher_has_template_variable(expr)
        and not _kibana_binds_promql_control_params(runtime_features)
    ):
        _append_unique(
            panel_notes,
            "Native PROMQL skipped: Kibana does not forward dashboard control "
            "params into PromQL label matchers on this target "
            "(requires Kibana 9.5+; uses ES|QL RLIKE binding instead)",
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
    # Native PROMQL returns before PANEL_TRANSLATORS. Multi-series bargauge
    # panels need ``bargauge_panel_rule`` (bar + category breakdown); keeping
    # them native collapses every series into one gauge tile. Defer when the
    # expression preserves per-series labels (``_timeseries``), has legend
    # placeholders, or has multiple targets. A true scalar stays native.
    if panel_type == "bargauge":
        _, shape_groups = _native_promql_result_shape(expr)
        if legend_labels or shape_groups == ["_timeseries"]:
            return None
    # Instant tables without an explicit ``by (...)`` (or legend placeholders)
    # would emit only a ``value`` column and drop Prometheus label columns
    # (ALERTS → alertname/severity/…). Prefer the ES|QL table path.
    if panel_type in ("table", "table-old") and _target_summary_mode(panel_type, target):
        _, shape_groups = _native_promql_result_shape(expr)
        real_groups = [col for col in shape_groups if col != "_timeseries"]
        if not real_groups and not legend_labels:
            return None

    index = datasource_index or "metrics-prometheus-*"
    regex_default_params = getattr(rule_pack, "_regex_default_param_names", frozenset())
    cleaned_expr, had_bare_variable = _clean_promql_for_native_with_state(
        expr,
        runtime_features=runtime_features,
        regex_default_params=regex_default_params,
    )
    _, group_cols = _native_promql_result_shape(expr)
    real_group_cols = [col for col in group_cols if col != "_timeseries"]
    # Grouped native PromQL XY / pie / heatmap panels are supported below via
    # ``_native_esql_panel_spec(... override_group_cols=...)``. Keep the
    # grouped-series rejection only for single-value tiles, which cannot render
    # one value per real breakdown dimension without changing the panel shape.
    # Parse the macro-resolved form once; reused by the metric/gauge gate below
    # and the QueryIR fields further down (avoids parsing the same expr twice).
    native_fragment = _parse_fragment(cleaned_expr or expr)
    if kibana_type in ("metric", "gauge"):
        # A real multi-series breakdown (``by (instance)`` → ``['instance']``)
        # can't be rendered as one value, so keep degrading those to ES|QL.
        if real_group_cols:
            return None
        # ``_timeseries`` is the time dimension only, not a breakdown. For a
        # distinct-metric ratio/difference (``1 - avail / size``, two metrics)
        # the instant query preserves the original arithmetic and collapses to
        # the latest value — the same outcome #138 gave line charts (#146).
        # A bare/single-metric expression (``up``, ``rate(foo[5m])``) instead
        # fans out to multiple series with no derived value, where ES|QL's
        # aggregating summary is the cleaner single tile — keep rejecting it.
        #
        # Count metrics from ``cleaned_expr``, not the raw ``expr``: a macro
        # range (``rate(foo_total[$__rate_interval])``) makes the AST parser
        # reject the raw form (``$`` is illegal inside ``[...]``) and fall to
        # the regex backend, which collects zero metrics — wrongly dropping a
        # genuine distinct-metric ratio. ``cleaned_expr`` is the macro-resolved
        # form the native command is actually built from below (#146).
        #
        # Count metric *occurrences* (``dedup=False``), not distinct names: the
        # canonical Prometheus error-rate ratio divides the *same* metric under
        # two selectors (``rate(http_requests_total{code=~"5.."}[5m]) /
        # rate(http_requests_total[5m])``). Counting distinct names collapses
        # that to one and wrongly degrades genuine derived arithmetic to the
        # same-bucket ES|QL approximation; counting occurrences sees the two
        # vector operands and keeps it native. A scalar-scaled single metric
        # (``node_cpu_seconds_total * 100``) still has one occurrence, so it
        # correctly stays degraded — the scalar literal contributes no metric.
        #
        # NOTE: occurrence count is a proxy for "derived value", not a guarantee
        # of a single row. An implicit-match ratio (``node_memory_MemAvailable
        # _bytes / node_memory_MemTotal_bytes``) has two operands and stays
        # native, yet when multiple instances are scraped it matches per-instance
        # and fans out to one series each — so single-value tiles can surface a
        # multi-row instant result. Kibana reduces/repeats it the same way
        # Grafana does for gauges; this is the intended outcome and mirrors
        # #138's accepted line-chart behavior — kept native by design rather
        # than degraded (#146). (Explicit vector matching like ``/ on(instance)``
        # is a separate case: ``build_native_promql_query`` rejects it, so those
        # degrade to ES|QL regardless of this gate.)
        #
        # Parse the *source* expression for the bare-selector check: native
        # cleaning rewrites ``{instance="$host"}`` to ``{instance=?host}``,
        # which the PromQL AST parser classifies as ``unknown`` and would
        # wrongly degrade Grafana 5 singlestat gauges (Uptime, buffer pool).
        is_bare_selector = (
            getattr(native_fragment, "family", None) == "simple_metric"
            or _is_bare_instant_selector(expr)
            or _is_bare_counter_reference(expr, resolver, rule_pack)
        )
        if "_timeseries" in group_cols and (
            len(_collect_source_metrics(native_fragment, dedup=False)) < 2
            and not is_bare_selector
        ):
            return None
    # Dashboard metric/gauge tiles collapse a *range* query via
    # ``LAST(value, step)`` (below) instead of PROMQL instant-at-``?_tend``.
    # Instant-at-now returns empty when the latest sample lags the dashboard
    # end (seeded/lab data, scrape stalls); ES|QL gauges already reduce over
    # the view window — match that. True instant (``time=?_tend``) stays for
    # ``instant: true`` table targets (#102). Alert instant (#200) calls
    # ``build_native_promql_query`` directly and is unchanged.
    #
    # Never let an instant query reach an XY (line/bar/area) spec: those bind
    # the x-axis to the ``step`` time column, which an instant query does NOT emit
    # (phantom axis / 400 — the #127 failure mode). ``_target_summary_mode``
    # returns True unconditionally for ``bargauge`` (→ ``bar``), so without this
    # guard a Prometheus ``bargauge`` panel would regress to a broken bar chart.
    range_collapsed_tile = kibana_type in ("metric", "gauge")
    instant = (not range_collapsed_tile) and (
        _target_summary_mode(panel_type, target)
        and kibana_type not in ("line", "bar", "area")
    )
    # Dashboard panels opt into adaptive resolution: a range plot emits bare
    # ``PROMQL index=... value=(...)`` so Kibana injects the dashboard time
    # range at render time (#272, #318), and a rate()/increase() over
    # ``$__rate_interval`` is emitted windowless so its lookback tracks the
    # view too (#273). Instant tiles ignore both (they carry no step). Alerts
    # never take this path — they call ``build_native_promql_query`` directly
    # with an explicit/default step.
    #
    # Explicit Grafana panel ``interval`` wins over adaptive bucketing and is
    # emitted as ``step=`` (issue #318). Keep ``adaptive_step=True`` even with
    # an explicit step so ``$__rate_interval`` still becomes windowless (#273)
    # — ``step=`` only overrides the timing selector precedence.
    panel_step = _grafana_panel_fixed_interval(panel)
    promql_query = build_native_promql_query(expr, index=index,
                                             legend_labels=legend_labels,
                                             kibana_type=kibana_type,
                                             legend_format=legend_format,
                                             runtime_features=runtime_features,
                                             instant=instant,
                                             regex_default_params=regex_default_params,
                                             resolver=resolver,
                                             step=panel_step,
                                             adaptive_step=True)
    if range_collapsed_tile:
        # Keep one row per series at the latest step in the view. Scalars have
        # no ``_timeseries``; multi-series tiles group on it so Kibana still
        # sees one current value per series (same fan-out instant used to emit).
        if "_timeseries" in group_cols:
            promql_query = f"{promql_query}\n| STATS value = LAST(value, step) BY _timeseries"
        else:
            promql_query = f"{promql_query}\n| STATS value = LAST(value, step)"
    # Live native-PROMQL parse gate: if a validator is attached (``--es-url``)
    # and the target rejects this query at parse time, degrade to ES|QL (return
    # None so the caller falls through to the ES|QL translator). A data/field gap
    # keeps the native path (issue #158).
    if not _native_promql_query_survives_validation(rule_pack, promql_query):
        _append_unique(
            panel_notes,
            "Native PROMQL degraded to ES|QL: target rejected the query at parse time",
        )
        return None
    if had_bare_variable:
        _append_unique(panel_notes, "Grafana template variables in arithmetic were replaced with literal 1")

    raw_legend = (legend_format or "").strip()
    # ``__auto`` is Grafana's "derive series identity from labels" sentinel —
    # not a literal series name. Treating it as a static label made Lens break
    # down on a missing ``label`` column (issue #317).
    static_legend_label = bool(raw_legend) and raw_legend != "__auto" and not legend_labels
    if "_timeseries" in group_cols:
        if legend_labels:
            effective_group_cols = legend_labels
        elif static_legend_label:
            # Single static label per series.
            effective_group_cols = ["label"]
        elif raw_legend == "__auto":
            # Native PROMQL keeps the ``_timeseries`` label blob; break down on
            # it so each series gets a legend entry (issue #317).
            effective_group_cols = ["_timeseries"]
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
    _label_placeholder_value_metric(yaml_panel, title=title, legend_format=legend_format)
    _apply_series_override_axes(yaml_panel, panel, [])

    notes = list(panel_notes) + ["Native PROMQL: original PromQL reused via ES|QL PROMQL command"]
    metric_map_note = _metric_map_bypass_note(
        _collect_source_metrics(native_fragment), rule_pack
    )
    if metric_map_note:
        _append_unique(notes, metric_map_note)
    for recording_rule_note in _recording_rule_metric_map_notes(
        _collect_source_metrics(native_fragment),
        rule_pack,
    ):
        _append_unique(notes, recording_rule_note)

    query_ir = QueryIR()
    query_ir.source_language = "promql"
    query_ir.source_expression = expr
    query_ir.clean_expression = cleaned_expr
    query_ir.panel_type = panel_type
    query_ir.datasource_type = datasource.get("type", "")
    query_ir.datasource_uid = datasource.get("uid", "")
    query_ir.datasource_name = datasource.get("name", "")
    query_ir.family = "native_promql"
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

    confidence = 0.90 if not metric_map_note else 0.7
    panel_result = PanelResult(
        title,
        panel_type,
        kibana_type,
        "migrated_with_warnings" if metric_map_note else "migrated",
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
        reasons=[metric_map_note] if metric_map_note else [],
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
    panel_inventory, targets_with_expr, resolver=None,
):
    """Combine multiple PromQL targets into a single native PROMQL panel.

    Uses ``label_replace`` + ``or`` to inject per-target legend labels so all
    series appear on one chart with distinct breakdown values.  Only attempted
    for XY chart types (line, bar, area) where overlay makes sense.
    """
    # As of August 7, 2026, Elasticsearch 9.5.0-SNAPSHOT still rejects PROMQL
    # ``label_replace()`` at runtime with "Function [label_replace] is not yet
    # implemented" (confirmed via live ``_query`` and Elastic PromQL function
    # docs). Bare structural ``or`` parses, but mirrored series that share
    # labels (Receive/Transmit, Read/Write) collapse to one timeseries without
    # a distinguishing label — so an ``or``-only combiner is not correct.
    # Keep the established ES|QL fusion path until label_replace (or an
    # equivalent series-label injection) lands. Evidence:
    # ``docs/design/node-exporter-1860-phase3-native-promql.md``.
    return None

    if kibana_type not in ("line", "bar", "area"):
        return None

    index = datasource_index or "metrics-prometheus-*"
    had_bare_variable = False
    parts: list[str] = []
    target_fragments = []

    for target, _ in targets_with_expr:
        expr = target.get("expr", "")
        runtime_features = getattr(rule_pack, "runtime_features", {})
        _record_passthrough_native_labels(expr, resolver)
        if (
            getattr(resolver, "_passthrough", False)
            and _promql_uses_rule_pack_label_overrides(expr, rule_pack)
        ):
            _append_unique(
                panel_notes,
                "Native PROMQL skipped: explicit label rules require ES|QL field resolution",
            )
            return None
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
        # Kibana-side forwarding of control params into inner PROMQL expressions
        # is gated by ``KIBANA_PROMQL_CONTROL_PARAMS`` (preferred by default;
        # forced off only for verified Kibana < 9.5). Keep the ES|QL fallback
        # when that feature is unsupported.
        if (
            _promql_label_matcher_has_template_variable(expr)
            and not _kibana_binds_promql_control_params(runtime_features)
        ):
            return None
        regex_default = getattr(
            rule_pack, "_regex_default_param_names", frozenset()
        )
        # Parse the fixed-window form (windowless rate is a type error under
        # promql-parser), but emit the adaptive/windowless form: these panels are
        # always range XY charts, so their rate()/increase() over
        # ``$__rate_interval`` tracks the view like Grafana (#273).
        cleaned, bare = _clean_promql_for_native_with_state(
            expr,
            runtime_features=runtime_features,
            regex_default_params=regex_default,
        )
        emit_cleaned, _ = _clean_promql_for_native_with_state(
            expr,
            runtime_features=runtime_features,
            regex_default_params=regex_default,
            adaptive_window=True,
        )
        had_bare_variable = had_bare_variable or bare
        # Parse the bare form for AST analysis, then rewrite metric selectors to
        # their `metrics.<name>` field for the emitted command (issue #270).
        target_fragments.append(_parse_fragment(cleaned or expr))
        emit_cleaned = _prefix_native_metric_fields(emit_cleaned, resolver)

        legend = (target.get("legendFormat") or "").strip()
        if not legend or legend == "{{}}":
            legend = expr[:40]
        legend = legend.replace('"', '\\"')

        parts.append(
            f'label_replace({emit_cleaned}, "__series", "{legend}", "", "")'
        )

    # Each individual expression has already passed can_use_native_promql (line
    # 2532), which rejects user-level `or`/`and`/`unless` binary ops via
    # _promql_has_known_server_bug.  The `or` injected here is a structural
    # multi-series join between label_replace() wrappers, not a user binary op;
    # applying _promql_has_known_server_bug to combined_expr would always block
    # this path.  The live validator at line 2594 provides the production safety net.
    combined_expr = " or ".join(parts)
    # #272 / #318: bind the overlay to the dashboard time range at view time, or
    # honor an explicit Grafana panel ``interval`` as ``step=``.
    panel_step = _grafana_panel_fixed_interval(panel)
    if panel_step:
        promql_query = (
            f"PROMQL index={index} step={panel_step} value=({combined_expr})"
        )
    else:
        promql_query = (
            f"PROMQL index={index} {_NATIVE_PROMQL_ADAPTIVE_SELECTOR} value=({combined_expr})"
        )

    # Live native-PROMQL parse gate (multi-target). A parse rejection degrades to
    # ES|QL translation; a data/field gap keeps native (issue #158).
    if not _native_promql_query_survives_validation(rule_pack, promql_query):
        _append_unique(
            panel_notes,
            "Native PROMQL degraded to ES|QL: target rejected the query at parse time",
        )
        return None
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
    all_source_metrics = []
    for frag in target_fragments:
        all_source_metrics.extend(_collect_source_metrics(frag))
    metric_map_note = _metric_map_bypass_note(all_source_metrics, rule_pack)
    if metric_map_note:
        _append_unique(notes, metric_map_note)
    for recording_rule_note in _recording_rule_metric_map_notes(all_source_metrics, rule_pack):
        _append_unique(notes, recording_rule_note)

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
        title,
        panel_type,
        kibana_type,
        "migrated_with_warnings" if metric_map_note else "migrated",
        0.70 if metric_map_note else 0.80,
        promql_expr=combined_expr,
        esql_query=promql_query,
        reasons=[metric_map_note] if metric_map_note else [],
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


_EXTRA_BREAKDOWN_WARNING_PREFIX = (
    "XY chart shows a single breakdown; additional grouping "
)
_FUSED_MULTI_TARGET_INFO = (
    "Fused multi-target panel from independently translated ES|QL queries"
)


def _panel_uses_composite_breakdown(yaml_panel) -> bool:
    esql = (yaml_panel or {}).get("esql")
    if not isinstance(esql, dict):
        return False
    breakdown_field = str((esql.get("breakdown") or {}).get("field") or "").strip()
    return breakdown_field in {"legend", "series_group"}


def _prune_non_semantic_panel_warnings(panel_result, yaml_panel):
    reasons = [str(item) for item in (getattr(panel_result, "reasons", []) or [])]
    if not reasons:
        return
    filtered: list[str] = []
    composite_breakdown = _panel_uses_composite_breakdown(yaml_panel)
    for reason in reasons:
        if reason == _FUSED_MULTI_TARGET_INFO:
            continue
        if composite_breakdown and reason.startswith(_EXTRA_BREAKDOWN_WARNING_PREFIX):
            continue
        filtered.append(reason)
    if filtered == reasons:
        return
    panel_result.reasons = filtered
    query_ir = getattr(panel_result, "query_ir", None)
    if isinstance(query_ir, dict):
        query_ir["warnings"] = list(filtered)
    if filtered:
        panel_result.status = "migrated_with_warnings"
        panel_result.confidence = min(panel_result.confidence, 0.8)
    elif panel_notes_imply_warning(panel_result.notes):
        panel_result.status = "migrated_with_warnings"
        panel_result.confidence = min(panel_result.confidence, 0.85)
    else:
        panel_result.status = "migrated"
        panel_result.confidence = max(panel_result.confidence, 0.85)


def _apply_panel_time_overrides(panel, yaml_panel, panel_result):
    """Translate Grafana panel ``timeFrom``/``timeShift`` onto *yaml_panel*.

    Only meaningful for a data (ES|QL) panel -- markdown/links/image panels
    have no query and no ``time_range`` slot to carry it. ``timeFrom`` becomes
    the panel-config ``time_range`` every ES|QL chart builder shares (applied
    uniformly in ``dashboards_api.map_yaml_panel``); ``timeShift`` has no API
    equivalent (it shifts the whole window, ``time_range`` only overrides it),
    so it degrades gracefully to an operator-visible warning instead of
    emitting a silently wrong ``time_range``.
    """
    esql_cfg = yaml_panel.get("esql") if isinstance(yaml_panel, dict) else None
    if not isinstance(esql_cfg, dict):
        return
    time_range, time_from_warning = _grafana_panel_time_range_override(panel)
    if time_range:
        esql_cfg["time_range"] = time_range
    elif time_from_warning:
        _append_unique(panel_result.reasons, time_from_warning)
        if panel_result.status == "migrated":
            panel_result.status = "migrated_with_warnings"
            panel_result.confidence = min(panel_result.confidence, 0.85)
    time_shift_warning = _grafana_panel_time_shift_warning(panel)
    if time_shift_warning:
        _append_unique(panel_result.reasons, time_shift_warning)
        if panel_result.status == "migrated":
            panel_result.status = "migrated_with_warnings"
            panel_result.confidence = min(panel_result.confidence, 0.85)


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
    approximation_note = APPROXIMATED_VIS_TYPE_NOTES.get(panel_result.grafana_type)
    if _SKIP_APPROXIMATION_NOTE in (panel_result.notes or []):
        approximation_note = None
    if approximation_note and panel_result.status in ("migrated", "migrated_with_warnings"):
        _append_unique(panel_result.reasons, approximation_note)
        panel_result.status = "migrated_with_warnings"
        panel_result.confidence = min(panel_result.confidence, 0.8)
    _apply_panel_time_overrides(panel, yaml_panel, panel_result)
    _prune_non_semantic_panel_warnings(panel_result, yaml_panel)
    # Notes that verification treats as semantic losses (e.g. field overrides
    # needing manual verify) must land in the With-warnings scorecard, not as
    # clean Migrated with a Yellow gate — otherwise Green << Migrated.
    if panel_result.status == "migrated" and panel_notes_imply_warning(panel_result.notes):
        panel_result.status = "migrated_with_warnings"
        panel_result.confidence = min(panel_result.confidence, 0.85)
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
        panel=context.panel,
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
        panel_unit = str(_panel_field_defaults(context.panel).get("unit") or "")
        category_query = _build_summary_category_bar_query(
            restored_query if restored else query,
            series_fields,
            primary.metadata.get("multi_series_metric_labels", {}),
            # Grafana percentunit is 0-1; metric tiles use number+% (0-100).
            scale_to_percent_points=(panel_unit == "percentunit"),
        )
        primary.esql_query = category_query
        context.yaml_panel["esql"] = _build_esql_metric_panel(
            category_query,
            metric_col="gauge_value",
            panel=context.panel,
            breakdown_col="label",
        )
        context.kibana_type = "metric"
        _append_unique(
            context.translation.warnings,
            "Approximated bargauge as metric tiles",
        )
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
        # A single-value bargauge is a value shown against a scale: the faithful
        # Kibana equivalent is a bullet gauge (horizontal/vertical per the source
        # orientation), not a plain number tile.
        context.yaml_panel["esql"] = _build_esql_gauge_panel(
            primary.esql_query,
            metric_col=primary.output_metric_field or None,
            panel=context.panel,
            shape=_bargauge_bullet_shape(context.panel),
        )
        context.kibana_type = "gauge"
        _append_unique(context.translation.warnings, "Approximated bargauge as a bullet gauge")
    context.handled = True
    return "approximated bargauge panel"


def _xy_layer_from_cross_index_spec(layer_spec, chart_type, mode=None, warnings=None):
    """Build one YAML XY chart dict for a cross-index layer partition."""
    metric_fields = list(layer_spec.get("metric_fields") or [])
    group_fields = list(layer_spec.get("group_fields") or [])
    query = layer_spec.get("query") or ""
    if len(metric_fields) > 1:
        return _build_esql_multi_series_xy(
            query,
            chart_type,
            metric_fields=metric_fields,
            by_cols=group_fields,
            mode=mode,
            warnings=warnings,
        )
    metric_col = metric_fields[0] if metric_fields else None
    return _build_esql_xy_panel(
        query,
        chart_type,
        metric_col=metric_col,
        by_cols=group_fields,
        mode=mode,
        warnings=warnings,
    )


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
    cross_layers = primary.metadata.get("cross_index_layers") or []
    if isinstance(cross_layers, list) and len(cross_layers) >= 2:
        built = []
        for layer_spec in cross_layers:
            if not isinstance(layer_spec, dict) or not layer_spec.get("query"):
                continue
            layer_panel = _xy_layer_from_cross_index_spec(
                layer_spec,
                context.kibana_type,
                mode=mode,
                warnings=primary.warnings,
            )
            if isinstance(layer_panel, dict) and layer_panel.get("query"):
                built.append(layer_panel)
        if len(built) >= 2:
            panel = dict(built[0])
            # Additional full chart layers keep their own ES|QL query so mixed
            # target_index panels do not collapse onto the first data stream.
            panel["layers"] = built[1:]
            context.yaml_panel["esql"] = panel
            context.handled = True
            return f"mapped to {context.kibana_type} panel (cross-index layers)"
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


def _build_esql_heatmap_panel(esql, metric_col=None, by_cols=None, time_fields=None, warnings=None):
    """Build a native Kibana heatmap (x=time, y=bucket, color=metric).

    A Grafana heatmap of histogram ``le`` buckets over time maps cleanly:
    ``STATS metric BY time_bucket, le`` -> x_axis=time_bucket, y_axis=le,
    metric=value. When the query lacks either a time axis or a second
    (y-axis) dimension, a heatmap is not well-defined, so degrade to the XY
    builder (which itself drops to a metric for single-value queries).
    """
    esql = _ensure_bucket_sort(esql)
    shape = _extract_esql_shape(esql)
    extracted_metric_col, extracted_by_cols = _extract_esql_columns(esql)
    if metric_col is None:
        metric_col = extracted_metric_col
    if not by_cols:
        by_cols = extracted_by_cols
    if time_fields is None:
        time_fields = shape.time_fields
    dimension_field, breakdown_field = _select_xy_dimension_fields(by_cols, time_fields=time_fields)
    if dimension_field is None or not breakdown_field:
        _append_unique(
            warnings if warnings is not None else [],
            "Approximated heatmap as line chart (needs both a time axis and a bucket dimension)",
        )
        return _build_esql_xy_panel(
            esql, "line", metric_col=metric_col, by_cols=by_cols,
            time_fields=time_fields, warnings=warnings,
        )
    return {
        "type": "heatmap",
        "query": esql,
        "x_axis": _dimension_field(dimension_field),
        "y_axis": {"field": breakdown_field},
        "metric": {"field": metric_col},
    }


@PANEL_TRANSLATORS.register("heatmap_panel", priority=45)
def heatmap_panel_rule(context):
    if context.kibana_type != "heatmap":
        return None
    context.yaml_panel["esql"] = _build_esql_heatmap_panel(
        context.translation.esql_query,
        metric_col=context.translation.output_metric_field or None,
        by_cols=context.translation.output_group_fields,
        warnings=context.translation.warnings,
    )
    emitted = (context.yaml_panel.get("esql") or {}).get("type", "heatmap")
    if emitted != "heatmap":
        context.kibana_type = emitted  # keep result type consistent with what was emitted
    context.handled = True
    return f"mapped to {emitted} panel"


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


def _clear_disagreeing_fused_legend_template(primary, fused_series):
    """Issue #354: never label every fused series with one target's legend.

    ``primary.metadata["legend_format_template"]`` is always the *first*
    target's literal ``legendFormat`` string. That is harmless for a
    single-metric panel, but when several *different-metric* targets are
    fused into one multi-series XY query (``multi_series_metric_fields``),
    applying target 0's template to every fused series (via
    ``_apply_composite_legend_to_xy_panel``'s ``CONCAT``) prefixes every
    series -- including the ones for metrics 1..N -- with metric 0's static
    legend text, e.g. every "Disk IO" series reading "Weighted IO time ..."
    even the ``Write time``/``Read time`` ones.

    Only *disagreement* matters: when every fused target shares the same
    template (the common single-metric, multi-label case), the composite
    legend is still correct and untouched. When templates disagree, clearing
    the template here makes the downstream ``xy_panel_rule`` skip the
    composite-legend path entirely, falling back to "the STATS column name
    carries the metric identity" -- already proven correct by the dashboard's
    own "Memory" panel, whose differing per-target prefixes never trigger the
    composite path because each has only one label placeholder.
    """
    if len(fused_series) < 2:
        return
    template = primary.metadata.get("legend_format_template")
    if not template:
        return
    if any(other.metadata.get("legend_format_template") != template for other in fused_series[1:]):
        primary.metadata["legend_format_template"] = None


def _split_fused_series_by_bare_aggregation_scope(fused_series):
    """Issue #355: separate a bare (no ``by()``) PromQL aggregation over the
    SAME metric as a grouped sibling target, so it is not union-grouped.

    ``min(x)`` / ``avg(x)`` / ``max(x)`` / ``sum(x)`` with no ``by()`` (and,
    equally, a target that simply has no grouping identity of its own)
    collapses across every series by definition -- Grafana always draws it
    as one line, not one per group. Each *individual* translation already
    carries its own correct standalone ``output_group_fields`` (from
    translating that target alone, before fusion); a target whose own
    grouping is empty has none. Fusing it into a shared ``STATS ... BY
    <union of every target's groups>`` computes it *inside* each group
    instead of *across* all of them (turning "Min"/"Avg"/"Max" into
    per-group duplicates of the raw series).

    Returns ``(grouped, matching_bare, unrelated_bare)`` when at least one
    ungrouped target shares its metric field with at least one grouped
    target. ``unrelated_bare`` holds any OTHER bare target whose metric
    differs from every grouped target's -- e.g. the "QoS"/"Total"-style
    broadcast case (a bare aggregate over a *different* metric shown
    alongside a per-category breakdown, where "Total" is not an aggregate of
    the breakdown's own series). Those stay on today's union path (fused
    together with ``grouped`` by the caller) rather than disabling the split
    entirely for the targets that *do* share a metric.
    """
    if len(fused_series) < 2:
        return None

    def _own_dims(translation):
        return [f for f in (translation.output_group_fields or []) if f != "time_bucket"]

    def _metric_key(translation):
        return translation.output_metric_field or translation.metric_name or ""

    grouped = [t for t in fused_series if _own_dims(t)]
    bare = [t for t in fused_series if not _own_dims(t)]
    if not grouped or not bare:
        return None
    grouped_metrics = {_metric_key(t) for t in grouped if _metric_key(t)}
    if not grouped_metrics:
        return None
    matching_bare = [t for t in bare if _metric_key(t) in grouped_metrics]
    unrelated_bare = [t for t in bare if _metric_key(t) not in grouped_metrics]
    if not matching_bare:
        return None
    return grouped, matching_bare, unrelated_bare


def _label_singleton_bare_layer(layer, target):
    """A lone bare-aggregation target's own standalone translation names its
    output column after the raw metric field -- there is no legend text to
    disambiguate one series from itself, unlike a multi-bare-target group
    (Min/Avg/Max), which already aliases each column to its own target's
    legend text. Rename the same way here so a static Grafana legend (e.g.
    ``legendFormat: "Min"``) is not silently dropped when the bare side of
    the #355 split happens to contain exactly one target.

    Only renames when the query has the exact expected ``| STATS <field> =
    ...`` shape (once, unambiguously); otherwise leaves the layer untouched
    rather than risk corrupting an unusual query.
    """
    alias = target.metadata.get("static_legend_label")
    field = target.output_metric_field
    if not alias or not field:
        return layer
    safe_alias = _safe_alias(alias)
    if not safe_alias or safe_alias == field:
        return layer
    pattern = re.compile(rf"(\|\s*STATS\s+){re.escape(field)}(\s*=)")
    query = layer.get("query") or ""
    if len(pattern.findall(query)) != 1:
        return layer
    layer = dict(layer)
    layer["query"] = pattern.sub(rf"\1{safe_alias}\2", query, count=1)
    layer["metric_fields"] = [safe_alias if f == field else f for f in layer.get("metric_fields") or []]
    layer["metric_label_hints"] = dict(layer.get("metric_label_hints") or {})
    layer["metric_label_hints"][safe_alias] = alias
    return layer


def _apply_bare_aggregation_scope_split(primary, fused_series):
    """Render a bare/grouped aggregation-scope split (issue #355) as two
    ES|QL layers, reusing the same multi-layer XY shape already proven for
    cross-index fusion (``xy_panel_rule``'s ``cross_index_layers``).

    Mutates *primary* in place and returns ``True`` when the split applies
    and both halves build cleanly; returns ``False`` (primary untouched)
    otherwise, so the caller falls back to today's single merged-query union.
    """
    split = _split_fused_series_by_bare_aggregation_scope(fused_series)
    if split is None:
        return False
    grouped, bare, unrelated_bare = split
    # Any bare target aggregating an unrelated metric keeps today's union
    # behavior alongside the grouped targets -- it never had a shared
    # per-series computation to split out of in the first place.
    grouped_layer = _fuse_same_index_series(grouped + unrelated_bare)
    if grouped_layer is None:
        return False
    bare_layer = _fuse_same_index_series(bare)
    if bare_layer is None:
        return False
    if len(bare) == 1:
        bare_layer = _label_singleton_bare_layer(bare_layer, bare[0])
    layers = [grouped_layer, bare_layer]
    primary.esql_query = layers[0]["query"]
    primary.source_type = layers[0]["source_type"]
    primary.metadata["cross_index_layers"] = layers
    primary.metadata["multi_series_metric_fields"] = list(layers[0].get("metric_fields") or [])
    primary.metadata["multi_series_metric_labels"] = dict(layers[0].get("metric_label_hints") or {})
    primary.output_metric_field = (
        (layers[0].get("metric_fields") or [None])[0] or primary.output_metric_field
    )
    primary.output_group_fields = list(
        layers[0].get("group_fields") or primary.output_group_fields or []
    )
    # ``primary`` (fused_series[0]) may itself be one of the *bare* targets
    # promoted out of this role -- its own static legend (e.g. "Avg") must
    # not leak onto the grouped layer's main metric label once primary now
    # represents that layer's identity instead of its own.
    primary.metadata["static_legend_label"] = (
        grouped[0].metadata.get("static_legend_label") if len(grouped) == 1 else None
    )
    collapsed = []
    for layer in layers:
        collapsed.extend(layer.get("targets") or [])
        for warning in layer.get("warnings") or []:
            _append_unique(primary.warnings, warning)
    primary.metadata["collapsed_targets"] = collapsed
    bare_names = ", ".join(
        str(t.metadata.get("series_alias") or t.output_metric_field or t.metric_name or "?")
        for t in bare
    )
    _append_unique(
        primary.warnings,
        f"Computed {bare_names} in a separate summary layer aggregated across every "
        "series, instead of grouping it with the per-series layer: the source PromQL "
        "aggregates without a `by()` clause, which Grafana always draws as one line "
        "across every series rather than one line per group"
    )
    return True


def _mismatched_grouping_union_warning(all_specs, plans):
    """Issue #355 (unrelated-metric case): name the changed semantics instead of
    only describing the mechanism, when a bare aggregation spec (no ``by()``)
    is unioned onto a grouped sibling's ``BY`` fields -- e.g. a fleet-wide
    "Total" broadcast alongside a per-category breakdown
    (``tests/test_grafana_qos_union_by.py``). ``_apply_bare_aggregation_scope_split``
    already splits the *same-metric* case (the disk-graphs Min/Avg/Max bug)
    into separate layers before this ever runs; this fallback only fires for
    the unrelated-metric case the issue explicitly says must keep the union.
    """
    base = "Unioned BY fields across multi-target series with mismatched grouping"
    union_dims = [f.rsplit("labels.", 1)[-1] for f in _union_group_fields(all_specs)]
    bare_aliases = []
    for translation, plan in plans:
        if any(spec.group_fields for spec in plan.specs):
            continue
        alias = (
            translation.metadata.get("series_alias")
            or translation.output_metric_field
            or translation.metric_name
            or (plan.specs[0].final_alias if plan.specs else "")
        )
        if alias:
            bare_aliases.append(str(alias))
    if not union_dims or not bare_aliases:
        return base
    names = ", ".join(dict.fromkeys(bare_aliases))
    dims = ", ".join(dict.fromkeys(union_dims))
    plural = len(set(bare_aliases)) > 1
    return (
        f"{base}: {names} {'are' if plural else 'is'} computed per {dims}, not across "
        f"{dims}, because the panel mixes grouped and ungrouped targets"
    )


def metrics_query_index(datasource_index=None, esql_index=None) -> str:
    """Return the index/stream every *metrics query* must read.

    Schema discovery already probes ``esql_index or data_view``. Native
    ``PROMQL index=…`` and ES|QL ``TS``/``FROM`` must use that same target so
    ``--esql-index`` cannot silently diverge from ``--data-view`` in ``auto``
    mode. ``datasource_index`` / ``--data-view`` remains the Kibana UI / control
    bind when callers pass it separately.
    """
    for candidate in (esql_index, datasource_index):
        token = str(candidate or "").strip()
        if token:
            return token
    return "metrics-*"


def translate_panel(panel, datasource_index="metrics-*", esql_index=None, rule_pack=None, resolver=None,
                    llm_endpoint="", llm_model="", llm_api_key="", metric_series_labels=None):
    """Translate a single Grafana panel, fusing multiple targets when possible."""
    rule_pack = _rule_pack_for_panel(rule_pack or RulePackConfig(), panel)
    # Single metrics read target for native PROMQL and ES|QL (see metrics_query_index).
    query_index = metrics_query_index(datasource_index, esql_index)
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

    grid = panel.get("gridPos", panel.get("gridData", {}))
    raw_w = grid.get("w", GRAFANA_GRID_COLS)
    raw_h = grid.get("h", 10)
    raw_x = grid.get("x", 0)
    raw_y = grid.get("y", 0)
    source_panel_id = str(panel.get("id") or panel.get("panelId") or "").strip()
    yaml_panel = {
        "title": title,
        "description": str(panel.get("description") or "").strip(),
        "size": {"w": KIBANA_GRID_COLS, "h": KIBANA_DEFAULT_HEIGHT},
        "position": {"x": 0, "y": 0},
        "_grafana_row_y": raw_y,
        "_grafana_row_x": raw_x,
        "_grafana_w": raw_w,
        "_grafana_h": raw_h,
    }
    if source_panel_id:
        # Survives on the in-memory YAML dict through layout/validation sync so
        # post-validation pairing does not depend on leaf order. DashboardIR
        # export strips underscore keys; title matching covers that path.
        yaml_panel["_source_panel_id"] = source_panel_id

    kibana_type = _resolved_panel_type_map(rule_pack).get(panel_type)
    if panel_type == "graph" and kibana_type == "line":
        kibana_type = _infer_graph_chart_style(panel)
    elif panel_type in ("timeseries", "state-timeline", "status-history") and kibana_type == "line":
        kibana_type = _infer_timeseries_chart_style(panel)
    if not kibana_type:
        reasons = [f"Unknown Grafana panel type: {panel_type}"]
        yaml_panel["markdown"] = {
            "content": f"**Migration Required**\n\nReasons: {', '.join(reasons)}"
        }
        panel_result = PanelResult(
            title,
            panel_type,
            "markdown",
            "not_feasible",
            0.0,
            reasons=reasons,
        )
        return yaml_panel, _enrich_panel_result(
            panel_result,
            panel=panel,
            datasource=datasource,
            query_language=query_language,
            notes=panel_notes,
            inventory=panel_inventory,
            yaml_panel=yaml_panel,
        )

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

    if rule_pack.panel_query_overrides and kibana_type:
        _title_lower = (title or "").lower()
        for _override in rule_pack.panel_query_overrides:
            if _title_lower == (_override.get("title_match") or "").lower():
                _curated_query = (_override.get("esql_query") or "").strip()
                _status = _override.get("status_override") or "migrated"
                if _curated_query and query_index:
                    # Replace the hardcoded source index with the runtime index so
                    # the override respects --datasource-index / --data-stream-name.
                    _curated_query = re.sub(
                        r"^(TS|FROM)\s+\S+",
                        lambda m: f"{m.group(1)} {query_index}",
                        _curated_query,
                        count=1,
                        flags=re.IGNORECASE,
                    )
                # Drop live_optional metrics that field-caps proved absent so a
                # curated override does not hard-require optional collector
                # fields (e.g. TCPRcvQDrop on TCP Errors).
                _optional_metric_result = _omit_absent_optional_metrics_from_curated_query_result(
                    _curated_query,
                    rule_pack.live_optional_metrics or [],
                    resolver,
                )
                if _optional_metric_result.exhausted_metrics:
                    missing_panel, panel_result = _make_missing_telemetry_panel(
                        yaml_panel,
                        title,
                        panel_type,
                        _optional_metric_result.exhausted_metrics,
                    )
                    return missing_panel, _enrich_panel_result(
                        panel_result,
                        panel=panel,
                        datasource=datasource,
                        query_language=query_language,
                        notes=panel_notes,
                        inventory=panel_inventory,
                        yaml_panel=missing_panel,
                    )
                _curated_query = _optional_metric_result.query
                _curated_query = _materialize_curated_query_override(_curated_query, resolver)
                if _curated_query:
                    _override_warnings = []
                    _override_type = str(
                        _override.get("kibana_type_override") or kibana_type or ""
                    ).strip() or kibana_type
                    _shape = _extract_esql_shape(_curated_query)
                    _projected = list(getattr(_shape, "projected_fields", []) or [])
                    _bargauge_measure = None
                    # Metric tiles with a label breakdown: either an explicit
                    # curated override, or the automatic multi-value bargauge
                    # approximation.
                    if "label" in _projected:
                        _measure_cols = [
                            field
                            for field in _projected
                            if field not in {"label", "sort_order"}
                        ]
                        if len(_measure_cols) == 1:
                            if (
                                not _override.get("kibana_type_override")
                                and panel_type == "bargauge"
                            ):
                                _override_type = "metric"
                            if _override_type == "metric":
                                _bargauge_measure = _measure_cols[0]
                    _esql_mode = _infer_xy_stacking_mode(panel) if _override_type in ("bar", "area") else None
                    _native_panel = _native_esql_panel_spec(
                        _curated_query,
                        _override_type,
                        panel=panel,
                        mode=_esql_mode,
                        warnings=_override_warnings,
                    )
                    if (
                        _native_panel is None
                        and _override_type == "datatable"
                        and _projected
                    ):
                        _native_panel = _build_esql_datatable_panel(
                            _curated_query,
                            metric_fields=_projected,
                        )
                    if (
                        _native_panel is None
                        and _override_type == "metric"
                        and _bargauge_measure
                    ):
                        _native_panel = _build_esql_metric_panel(
                            _curated_query,
                            metric_col=_bargauge_measure,
                            panel=panel,
                            breakdown_col="label",
                        )
                    if (
                        _native_panel is None
                        and _override_type in ("line", "bar", "area")
                    ):
                        if "label" in _projected and "value" in _projected:
                            _native_panel = _build_esql_xy_panel(
                                _curated_query,
                                _override_type,
                                metric_col="value",
                                by_cols=["label"],
                                mode=_esql_mode,
                                warnings=_override_warnings,
                            )
                    if _native_panel:
                        yaml_panel["esql"] = _native_panel
                        # Curated overrides skip PANEL_TRANSLATORS; honour
                        # pack-level timeFrom drops before enrich applies
                        # Grafana panel time_range.
                        if _override.get("drop_time_from"):
                            panel.pop("timeFrom", None)
                            panel.pop("timeShift", None)
                        # Apply display units first, then seriesOverrides so a
                        # right-axis ``format: none`` (Load 1m) can clear the
                        # inherited left-axis % / bytes format. Matches the
                        # generic PANEL_TRANSLATORS path.
                        enrich_yaml_panel_display(yaml_panel, panel)
                        # Wide multi-metric curated queries (e.g. Memory Basic)
                        # need Grafana field overrides like RAM Total
                        # ``stack: false`` applied the same as the generic path.
                        _apply_series_override_axes(
                            yaml_panel, panel, _override_warnings
                        )
                        _label_placeholder_value_metric(
                            yaml_panel,
                            title=title,
                            legend_format=_panel_static_legend_label(panel),
                        )
                        _score = 1.0 if _status == "migrated" else 0.7
                        _override_notes = list(panel_notes)
                        if _status == "migrated":
                            _override_notes = [
                                note
                                for note in _override_notes
                                if not (
                                    "field override(s)" in str(note)
                                    and "verify visual mappings manually" in str(note)
                                )
                            ]
                        if panel_type != _override_type:
                            _override_notes.append(_SKIP_APPROXIMATION_NOTE)
                        # Record the *emitted* panel query, not the bare curated
                        # override text. Gauge/metric builders append trailing
                        # ``| EVAL _gauge_*`` (and similar constants) after the
                        # override query; recording the bare text let the
                        # validate-stage ``sync_result_queries_to_yaml`` overwrite
                        # the YAML query and strip those columns, orphaning
                        # min/max/goal accessors (same failure class as issue
                        # #109 for native-PROMQL gauges). Memory Usage then
                        # uploaded without ``metric.max``, so Kibana auto-fit
                        # the dial to ~0-2% instead of the Grafana 0-100 domain.
                        _emitted_query = _native_panel.get("query", _curated_query)
                        # A curated override is hand-written and can omit a
                        # source metric the pack author never accounted for
                        # (issue #349). ``status_override`` must act as a
                        # ceiling on status/confidence, not an unconditional
                        # assignment, so a detected gap still surfaces -- the
                        # same discipline the general (non-pack) path applies
                        # for "Target telemetry missing" (issue #352).
                        _source_target_exprs = [
                            str(_t.get("expr") or "")
                            for _t in panel.get("targets", []) or []
                            if isinstance(_t, dict)
                            and _t.get("expr")
                            and not _t.get("hide")
                        ]
                        _dropped_curated_metrics = _source_metrics_absent_from_query(
                            _source_target_exprs, _emitted_query, resolver
                        )
                        # live_optional_metrics already stripped these because
                        # field-caps proved them absent. Re-flagging them as a
                        # pack omission fights that design and yellows panels
                        # (TCP Errors / TCPRcvQDrop) whose remaining series
                        # still render.
                        _optional_omitted = set(
                            _optional_metric_result.omitted_metrics or []
                        )
                        _optional_declared = {
                            str(name).strip()
                            for name in (rule_pack.live_optional_metrics or [])
                            if str(name).strip()
                        }
                        if _optional_omitted or _optional_declared:
                            _dropped_curated_metrics = [
                                metric
                                for metric in _dropped_curated_metrics
                                if metric not in _optional_omitted
                                and not _live_optional_source_metric_absent(
                                    metric, resolver, _optional_declared
                                )
                            ]
                        if _dropped_curated_metrics:
                            _append_unique(
                                _override_warnings,
                                "Target telemetry missing from curated override: "
                                + ", ".join(_dropped_curated_metrics),
                            )
                            if _status == "migrated":
                                _status = "migrated_with_warnings"
                            _score = min(_score, 0.6)
                        _panel_result = PanelResult(
                            title, panel_type, _override_type, _status, _score,
                            reasons=_override_warnings,
                            promql_expr=_curated_query,
                            esql_query=_emitted_query,
                        )
                        return yaml_panel, _enrich_panel_result(
                            _panel_result,
                            panel=panel,
                            datasource=datasource,
                            query_language=query_language,
                            notes=_override_notes,
                            inventory=panel_inventory,
                            yaml_panel=yaml_panel,
                        )
                break

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
        native_warnings = []
        native_panel = _native_esql_panel_spec(native_query, kibana_type, mode=esql_mode, warnings=native_warnings)
        if native_panel:
            native_shape = _extract_esql_shape(native_query)
            native_panel_type = str(native_panel.get("type") or "")
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
            datasource, query_index, rule_pack, panel_notes, panel_inventory,
            query_language, visible_targets, resolver=resolver,
        )
        if native_result is not None:
            return native_result

    targets_with_expr = [(target, query_text) for target, query_text in visible_targets if target.get("expr")]
    promql_exprs = [target.get("expr", "") for target, _ in targets_with_expr]

    # Prefer native multi-target PROMQL overlays on capable Kibana builds.
    # Historically this path was a last-ditch fallback after ES|QL merge
    # failed, because grouped control-bound label matchers could not bind
    # inside Kibana's PROMQL command. On 9.5 with the explicit Kibana control-
    # param opt-in, some real-world overlay charts (for example Redis
    # receive/transmit and hits/misses) stay source-faithful and upload cleanly
    # as native PROMQL. Keep ES|QL as the fallback whenever the native combiner
    # declines the panel.
    if rule_pack.native_promql and query_language == "promql" and len(targets_with_expr) > 1:
        multi_native_result = _translate_multi_target_native_promql(
            panel,
            yaml_panel,
            title,
            panel_type,
            kibana_type,
            datasource,
            query_index,
            rule_pack,
            panel_notes,
            panel_inventory,
            targets_with_expr,
            resolver=resolver,
        )
        if multi_native_result is not None:
            return multi_native_result

    if not promql_exprs:
        if visible_targets:
            _append_unique(panel_notes, "Visible panel targets did not expose PromQL-compatible expressions")
        placeholder_panel, panel_result = _make_placeholder_panel(
            yaml_panel, title, panel_type, kibana_type, panel=panel
        )
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
    dropped_live_metric_targets: list[tuple[str, list[str]]] = []
    tolerated_live_metric_target_refs: set[str] = set()
    tolerated_absent_live_metrics: list[str] = []
    live_optional_metrics = {str(name).strip() for name in (rule_pack.live_optional_metrics or []) if str(name).strip()}
    for idx, (target, _) in enumerate(targets_with_expr, start=1):
        expr = target.get("expr", "")
        negate_target = False
        negate_reason = ""
        stripped = expr.strip()
        if stripped.startswith("- ") or stripped.startswith("-\n") or (
            stripped.startswith("-") and len(stripped) > 1 and stripped[1] in "( "
        ):
            negate_target = True
            negate_reason = "expression"
            expr = stripped.lstrip("-").strip()
        elif _target_has_negative_y_override(panel, target):
            negate_target = True
            negate_reason = "display_transform"
        target_datasource = normalize_datasource(target.get("datasource") or datasource)
        target_query_language = infer_query_language(expr, target_datasource.get("type", ""), panel_type)
        target_resolver = resolver
        if target_query_language == "logql":
            target_resolver = _resolver_for_index(resolver, rule_pack, rule_pack.logs_index)
        try:
            t = translate_promql_to_esql(
                expr,
                datasource_index=query_index,
                esql_index=query_index,
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
                data_view=query_index,
                index=query_index,
                rule_pack=rule_pack or RulePackConfig(),
                resolver=target_resolver,
                panel_type=panel_type,
                clean_expr=expr,
            )
            t.feasibility = "not_feasible"
            t.warnings.append(f"Translation crashed: {type(exc).__name__}: {exc}")
        missing_live_metrics = _live_missing_metrics_for_expr(expr, target_resolver)
        if missing_live_metrics:
            target_ref = str(target.get("refId") or f"series_{idx}")
            t.metadata["missing_live_metrics"] = list(missing_live_metrics)
            non_optional_missing_live_metrics = [
                metric for metric in missing_live_metrics
                if metric not in live_optional_metrics
            ]
            if non_optional_missing_live_metrics:
                dropped_live_metric_targets.append((target_ref, non_optional_missing_live_metrics))
            else:
                tolerated_live_metric_target_refs.add(target_ref)
                t.metadata["tolerated_missing_live_metrics"] = list(missing_live_metrics)
                for metric in missing_live_metrics:
                    _append_unique(tolerated_absent_live_metrics, metric)
            continue
        t.metadata["target_ref_id"] = target.get("refId") or f"series_{idx}"
        # Keep the target's own expression: ``promql_expr`` is overwritten with
        # the merged " ||| " join below, but per-target provenance (and the
        # parity oracle that consumes it) needs the original sub-query.
        t.metadata["target_source_expr"] = expr
        if negate_target:
            t.metadata["negate_result"] = True
            t.metadata["negate_reason"] = negate_reason or "expression"
        translations.append(t)

    if not translations and (dropped_live_metric_targets or tolerated_live_metric_target_refs):
        missing_metrics: list[str] = []
        for _target_name, metrics in dropped_live_metric_targets:
            for metric in metrics:
                _append_unique(missing_metrics, metric)
        for metric in tolerated_absent_live_metrics:
            _append_unique(missing_metrics, metric)
        missing_panel, panel_result = _make_missing_telemetry_panel(
            yaml_panel,
            title,
            panel_type,
            missing_metrics or sorted(live_optional_metrics),
        )
        return missing_panel, _enrich_panel_result(
            panel_result,
            panel=panel,
            datasource=datasource,
            query_language=query_language,
            notes=panel_notes,
            inventory=panel_inventory,
            yaml_panel=missing_panel,
        )

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

    # A scalar-constant target (Grafana reference lines like ``expr: 1``) must
    # never be the thing that makes a panel look migrated. When every
    # substantive target was dropped as not-feasible and only constants remain,
    # reporting success renders a flat reference line with the real series
    # silently gone -- an operator sees a green panel and no indication the
    # metric is missing. Grafana 14091's "Hit ratio per instance" pairs an
    # unsupported self-referential ratio with ``expr: 1`` and did exactly that.
    # Degrade gracefully instead: keep the panel not-feasible so the reason
    # surfaces (project rule: never hide a semantic gap).
    if feasible_translations and any(t.feasibility == "not_feasible" for t in translations):
        if all(
            (t.output_metric_field or t.metric_name) == "constant_value"
            for t in feasible_translations
        ):
            _append_unique(
                panel_notes,
                "Only scalar-constant targets survived translation (e.g. a Grafana "
                "reference line); the substantive target(s) are not feasible, so the "
                "panel is reported as not feasible rather than rendering a bare constant",
            )
            feasible_translations = []

    collapsed = _try_collapse_same_metric_targets(feasible_translations)
    if collapsed:
        feasible_translations = [collapsed]

    primary = feasible_translations[0] if feasible_translations else translations[0]
    if dropped_live_metric_targets:
        dropped_metrics: list[str] = []
        for _target_name, metrics in dropped_live_metric_targets:
            for metric in metrics:
                _append_unique(dropped_metrics, metric)
        if dropped_metrics:
            _append_unique(
                primary.warnings,
                "Dropped series whose live target metrics are absent: "
                + ", ".join(sorted(dropped_metrics)),
            )
    fused_extra = []
    fused_series = [primary] if feasible_translations else []
    if len(feasible_translations) > 1:
        if panel_type in {"table", "table-old", "bargauge", "stat", "singlestat", "gauge"}:
            fused_series = _best_compatible_translation_group(feasible_translations)
        elif kibana_type in ("line", "bar", "area"):
            fused_series = [primary]
            for et in feasible_translations[1:]:
                if _translations_compatible(*(fused_series + [et])):
                    fused_series.append(et)
            # Same-index-incompatible targets may still share the panel via
            # separate Lens layers when their ES|QL indexes differ.
            if len(fused_series) < len(feasible_translations):
                leftover = [t for t in feasible_translations if t not in fused_series]
                for et in leftover:
                    if _translation_query_index(et) and (
                        _translation_query_index(et) != _translation_query_index(primary)
                    ):
                        fused_series.append(et)
        primary = fused_series[0]
        fused_extra = fused_series[1:]
        _clear_disagreeing_fused_legend_template(primary, fused_series)
        if len(fused_series) > 1:
            index_groups = _partition_translations_by_index(fused_series)
            distinct_indexes = [idx for idx, _ in index_groups if idx]
            if len(distinct_indexes) > 1:
                cross_layers = []
                for _index, group in index_groups:
                    # A same-metric bare/grouped pair (issue #355) can land
                    # in the same per-index partition as an unrelated
                    # cross-index target; split it the same way a
                    # single-index fusion would, instead of union-grouping
                    # it just because a *different* index also has a layer.
                    same_metric_split = _split_fused_series_by_bare_aggregation_scope(group)
                    if same_metric_split is not None:
                        sub_grouped, sub_bare, sub_unrelated = same_metric_split
                        grouped_layer = _fuse_same_index_series(sub_grouped + sub_unrelated)
                        bare_layer = _fuse_same_index_series(sub_bare)
                        if len(sub_bare) == 1 and bare_layer is not None:
                            bare_layer = _label_singleton_bare_layer(bare_layer, sub_bare[0])
                        if grouped_layer is not None and bare_layer is not None:
                            cross_layers.append(grouped_layer)
                            cross_layers.append(bare_layer)
                            continue
                    layer = _fuse_same_index_series(group)
                    if layer is None:
                        continue
                    cross_layers.append(layer)
                if len(cross_layers) >= 2:
                    primary.esql_query = cross_layers[0]["query"]
                    primary.source_type = cross_layers[0]["source_type"]
                    primary.metadata["cross_index_layers"] = cross_layers
                    primary.metadata["multi_series_metric_fields"] = list(
                        cross_layers[0].get("metric_fields") or []
                    )
                    primary.metadata["multi_series_metric_labels"] = dict(
                        cross_layers[0].get("metric_label_hints") or {}
                    )
                    primary.output_metric_field = (
                        (cross_layers[0].get("metric_fields") or [None])[0]
                        or primary.output_metric_field
                    )
                    primary.output_group_fields = list(
                        cross_layers[0].get("group_fields") or primary.output_group_fields or []
                    )
                    collapsed = []
                    for layer in cross_layers:
                        collapsed.extend(layer.get("targets") or [])
                        for warning in layer.get("warnings") or []:
                            _append_unique(primary.warnings, warning)
                    primary.metadata["collapsed_targets"] = collapsed
                    # Extra single-query overlays would fight per-index layers.
                    fused_extra = []
                    streams = ", ".join(
                        layer.get("index") or "(default)" for layer in cross_layers
                    )
                    _append_unique(
                        primary.warnings,
                        "Split multi-target panel across distinct data streams "
                        f"({streams}); each stream is a separate chart layer",
                    )
                else:
                    # Fall through to single-query merge with whatever fused.
                    merged_query = _build_multi_target_series_query(fused_series)
                    _colocated_fusion = merged_query is not None
                    if merged_query is None:
                        merged_query = _merge_pretranslated_xy_queries(fused_series)
                    if merged_query:
                        primary.esql_query = merged_query["query"]
                        primary.source_type = merged_query["source_type"]
                        primary.metadata["multi_series_metric_fields"] = merged_query["metric_fields"]
                        primary.metadata["multi_series_metric_labels"] = merged_query.get(
                            "metric_label_hints", {}
                        )
                        primary.metadata["collapsed_targets"] = merged_query.get("targets", [])
                        primary.output_metric_field = merged_query["metric_fields"][0]
                        primary.output_group_fields = merged_query["group_fields"]
                        for warning in merged_query["warnings"]:
                            _append_unique(primary.warnings, warning)
                        if _colocated_fusion:
                            primary.warnings = [
                                w for w in primary.warnings
                                if w not in _STALE_AFTER_COLOCATED_FUSION
                            ]
            elif _apply_bare_aggregation_scope_split(primary, fused_series):
                # Issue #355: rendered as its own two-layer split above;
                # nothing left over to also overlay as an "extra" translation.
                fused_extra = []
            else:
                merged_query = _build_multi_target_series_query(fused_series)
                _colocated_fusion = merged_query is not None
                if merged_query is None:
                    # Formula-plan fusion can fail on complex OR-chain targets that
                    # each translate alone (MySQL Network Traffic). Fall back to
                    # splicing the already-translated ES|QL bodies.
                    merged_query = _merge_pretranslated_xy_queries(fused_series)
                if merged_query:
                    primary.esql_query = merged_query["query"]
                    primary.source_type = merged_query["source_type"]
                    primary.metadata["multi_series_metric_fields"] = merged_query["metric_fields"]
                    primary.metadata["multi_series_metric_labels"] = merged_query.get(
                        "metric_label_hints", {}
                    )
                    primary.metadata["collapsed_targets"] = merged_query.get("targets", [])
                    primary.output_metric_field = merged_query["metric_fields"][0]
                    primary.output_group_fields = merged_query["group_fields"]
                    for warning in merged_query["warnings"]:
                        _append_unique(primary.warnings, warning)
                    if _colocated_fusion:
                        primary.warnings = [
                            w for w in primary.warnings
                            if w not in _STALE_AFTER_COLOCATED_FUSION
                        ]
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

    # Apply Grafana panel transformations to the fused wide ES|QL query
    # *before* PANEL_TRANSLATORS (bargauge unpivot / XY binding) so
    # calculateField/organize see the multi-series columns they name.
    applied_transform_indices: list[int] = []
    if primary.esql_query and panel.get("transformations"):
        _seed_transform_metric_labels(panel, primary)
        rewritten_query, tx_result = apply_transformations_to_esql(
            panel,
            primary,
            esql_query=primary.esql_query,
        )
        primary.esql_query = rewritten_query
        applied_transform_indices = list(tx_result.applied_indices)
        for warning in tx_result.warnings:
            _append_unique(primary.warnings, warning)
        if tx_result.updated_metric_fields:
            primary.metadata["multi_series_metric_fields"] = list(tx_result.updated_metric_fields)
            primary.output_metric_field = tx_result.updated_metric_fields[0]
        if tx_result.updated_metric_label_hints:
            primary.metadata["multi_series_metric_labels"] = dict(tx_result.updated_metric_label_hints)
        primary.query_ir = build_query_ir(primary)

    migrated_refs = {
        t.metadata.get("target_ref_id")
        for t in fused_series
        if t.metadata.get("target_ref_id")
    }
    migrated_refs.update(primary.metadata.get("collapsed_target_refs", []) or [])
    migrated_target_count = max(len(migrated_refs), int(primary.metadata.get("collapsed_target_count", 0) or 0))
    effective_target_total = len(targets_with_expr) - len(tolerated_live_metric_target_refs)
    dropped_count = effective_target_total - migrated_target_count
    if dropped_count > 0:
        dropped_exprs = [
            t.metadata.get("target_source_expr") or t.promql_expr
            for t in translations
            if t.metadata.get("target_ref_id") not in migrated_refs
            and (t.metadata.get("target_source_expr") or t.promql_expr)
        ]
        windows_dropped_count = sum(1 for e in dropped_exprs if "windows_" in e)
        # Only claim "dropped targets are Windows-specific" when every dropped
        # target actually is one — a dashboard commonly carries one PromQL
        # target per OS (Linux node_exporter vs. windows_exporter) alongside
        # other, unrelated targets. If just some of the drops are
        # Windows-specific, saying so unqualified implies the rest of the
        # loss is also OS-irrelevant and hides a real gap (e.g. a Linux
        # node_exporter target dropped for a genuine grouping mismatch).
        if dropped_exprs and windows_dropped_count == len(dropped_exprs):
            windows_suffix = " (dropped targets are Windows-specific)"
        elif windows_dropped_count > 0:
            windows_suffix = f" ({windows_dropped_count} of the dropped targets are Windows-specific)"
        else:
            windows_suffix = ""
        if migrated_target_count > 1:
            msg = f"Dropped {dropped_count} incompatible target(s); showing {migrated_target_count} mergeable targets"
            msg += windows_suffix
            _append_unique(primary.warnings, msg)
        elif migrated_target_count == 1:
            msg = f"Panel has {len(targets_with_expr)} PromQL targets but only 1 could be migrated"
            msg += windows_suffix
            _append_unique(primary.warnings, msg)

    if primary.feasibility == "not_feasible" or not primary.esql_query:
        if (
            rule_pack.native_promql
            and query_language == "promql"
            and len(targets_with_expr) > 1
        ):
            multi_result = _translate_multi_target_native_promql(
                panel, yaml_panel, title, panel_type, kibana_type,
                datasource, query_index, rule_pack, panel_notes,
                panel_inventory, targets_with_expr, resolver=resolver,
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
            if primary.metadata.get("negate_reason") != "display_transform":
                _append_unique(primary.warnings, "Applied negation to match leading minus in original expression")

    # Translators may have snapshotted the pre-transform query into yaml_panel;
    # keep the applied-transform body authoritative when present.
    if applied_transform_indices and primary.esql_query and isinstance(yaml_panel.get("esql"), dict):
        yaml_panel["esql"]["query"] = primary.esql_query
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
    _label_placeholder_value_metric(
        yaml_panel,
        title=title,
        # Same rule as curated overrides: only an unambiguous static legend
        # shared by every visible target is safe on a fused series. Mixed
        # legends fall back to the panel title rather than the primary target.
        legend_format=_panel_static_legend_label(panel) or static_legend_label or "",
    )
    _apply_series_override_axes(yaml_panel, panel, primary.warnings)
    if yaml_panel.get("esql", {}).get("query"):
        primary.esql_query = yaml_panel["esql"]["query"]
        primary.query_ir = build_query_ir(primary)
    # Surface unmapped Prometheus recording-rule metrics on the ES|QL path
    # (native PROMQL already does this) so operators see the same gap notes.
    source_metrics_for_notes: list[str] = []
    for series in list(fused_series or []) or [primary]:
        frag = getattr(series, "fragment", None)
        if frag is not None:
            source_metrics_for_notes.extend(_collect_source_metrics(frag))
        metric_name = str(getattr(series, "metric_name", "") or "").strip()
        if metric_name:
            source_metrics_for_notes.append(metric_name)
    if not source_metrics_for_notes:
        from observability_migration.core.metric_mapping.scaffold import (
            _PROMQL_METRIC_TOKEN_RE,
        )

        for expr in promql_exprs or []:
            source_metrics_for_notes.extend(
                match.group(1)
                for match in _PROMQL_METRIC_TOKEN_RE.finditer(str(expr or ""))
            )
    for recording_rule_note in _recording_rule_metric_map_notes(
        source_metrics_for_notes,
        rule_pack,
    ):
        _append_unique(primary.warnings, recording_rule_note)
    # Only check targets that were actually counted as migrated
    # (``fused_series``): targets dropped for a live-missing metric or an
    # incompatible grouping are already explained by the warnings above, so
    # re-checking them here would double-report the same gap under a
    # different reason (issue #352). This instead catches a target that WAS
    # judged mergeable yet whose metric silently never made it into the
    # final STATS/EVAL -- available but dropped by the translator, not a
    # target-schema gap.
    _migrated_target_exprs = [
        str(_series.metadata.get("target_source_expr") or _series.promql_expr or "")
        for _series in (fused_series or [primary])
    ]
    # A cross-index panel (issue #352 regression risk) splits fused targets
    # across multiple Lens layers, each with its own ES|QL query
    # (``primary.metadata["cross_index_layers"]``); ``primary.esql_query`` is
    # only the first layer's query. Checking against that alone would falsely
    # flag every target whose metric only appears in a later layer.
    _cross_index_layers = primary.metadata.get("cross_index_layers") or []
    _all_layer_queries = "\n".join(
        [primary.esql_query or ""]
        + [
            str(_layer.get("query") or "")
            for _layer in _cross_index_layers
            if isinstance(_layer, dict)
        ]
    )
    _dropped_source_metrics = _source_metrics_absent_from_query(
        _migrated_target_exprs, _all_layer_queries, resolver
    )
    if _dropped_source_metrics:
        _append_unique(
            primary.warnings,
            "Dropped from migrated query: " + ", ".join(_dropped_source_metrics),
        )
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
    panel_result.applied_transform_indices = list(applied_transform_indices)
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
        _cst_panel_type = getattr(collapsed, "panel_type", "")
        collapsed_summary = _collapse_summary_ts_query(
            parts, output_group_fields, metric_fields,
            keep_time_bucket=_cst_panel_type in {"table", "table-old"},
        )
    if collapsed_summary is None:
        # The KEEP is dropped downstream by _strip_dotted_group_keep when a
        # grouping field is dotted (avoids an ES|QL "Output has changed" error).
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


_ESQL_ALIAS_TOKEN_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|'
    r"'(?:\\.|[^'\\])*'|"
    r"`(?:\\.|``|[^`])*`|"
    r"[A-Za-z_][A-Za-z0-9_.]*"
)


def _canonical_esql_alias(identifier):
    """Return the column name represented by a possibly quoted identifier."""
    text = str(identifier or "").strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        return text[1:-1].replace("``", "`").replace("\\`", "`")
    return text


def _rewrite_esql_alias_references(expression, alias_map):
    """Rewrite identifier tokens without touching ES|QL string literals."""

    def _replace(match):
        token = match.group(0)
        if token.startswith(("'", '"')):
            return token
        return alias_map.get(_canonical_esql_alias(token), token)

    return _ESQL_ALIAS_TOKEN_RE.sub(_replace, expression)


def _stats_aliases(stats_stage):
    """Column names a STATS stage defines."""
    body = re.split(r"\bBY\b", stats_stage, maxsplit=1, flags=re.IGNORECASE)[0]
    body = body[len("STATS ") :] if body.upper().startswith("STATS ") else body
    aliases = set()
    for part in _split_top_level_csv(body):
        if "=" in part:
            aliases.add(_canonical_esql_alias(part.split("=", 1)[0]))
    return aliases


def _metric_field_is_defined_by_a_dropped_stage(
    metric_field, kept_assignments, post_stats, eval_stages
):
    """Whether this target's output column only exists in a stage the merge drops."""
    later_stats = [s for s in post_stats if s.upper().startswith("STATS ")]
    if not later_stats:
        return False
    name = _canonical_esql_alias(metric_field)
    kept = {
        _canonical_esql_alias(a.split("=", 1)[0]) for a in kept_assignments if "=" in a
    }
    if name in kept:
        return False
    # An EVAL in this target may define it instead; the merge rewrites those.
    for stage in eval_stages:
        body = stage[len("EVAL ") :]
        if "=" in body and _canonical_esql_alias(body.split("=", 1)[0]) == name:
            return False
    defined_later = set()
    for stage in later_stats:
        defined_later |= _stats_aliases(stage)
    return name in defined_later


def _merge_pretranslated_xy_queries(translations):
    """Fuse already-translated XY ES|QL queries when formula-plan fusion fails.

    Complex ``or``-chain targets (MySQL Network Traffic) each translate cleanly
    alone but produce duplicate measure aliases / incompatible formula plans
    when rebuilt together. When every target already has a time-bucketed ES|QL
    body with a single metric column, splice those STATS measures into one
    shared pipeline and EVAL one series column per target.
    """
    if not translations or len(translations) < 2:
        return None
    if any(not (t.esql_query or "").strip() for t in translations):
        return None
    if any(t.feasibility == "not_feasible" for t in translations):
        return None

    parsed = []
    for translation in translations:
        query = translation.esql_query or ""
        stages = [stage.strip() for stage in _split_esql_pipeline(query) if stage.strip()]
        if not stages:
            return None
        source = stages[0]
        if not source.upper().startswith(("TS ", "FROM ")):
            return None
        source_type = "TS" if source.upper().startswith("TS ") else "FROM"
        stats_idx = next(
            (
                idx
                for idx, stage in enumerate(stages)
                if stage.upper().startswith("STATS ")
            ),
            None,
        )
        if stats_idx is None:
            return None
        stats_stage = stages[stats_idx]
        by_split = re.split(r"\bBY\b", stats_stage, maxsplit=1, flags=re.IGNORECASE)
        if len(by_split) != 2:
            return None
        assignments_text = by_split[0][len("STATS ") :].strip()
        by_text = by_split[1].strip()
        if "time_bucket" not in by_text:
            return None
        assignments = [
            part.strip()
            for part in _split_top_level_csv(assignments_text)
            if part.strip() and "=" in part
        ]
        if not assignments:
            return None
        metric_field = str(translation.output_metric_field or "").strip()
        if not metric_field:
            shape = _extract_esql_shape(query)
            if shape.metric_fields:
                metric_field = shape.metric_fields[0]
        if not metric_field:
            return None
        # Capture EVAL lines that derive the final metric from STATS aliases
        # (COALESCE / arithmetic) so we can rename the result per target.
        post_stats = stages[stats_idx + 1 :]
        eval_stages = [
            stage
            for stage in post_stats
            if stage.upper().startswith("EVAL ")
        ]
        # Only the first STATS survives the merge, and the merge then binds this
        # target's series to ``metric_field``. A later STATS that re-aggregates
        # the same aliases (the summary-mode collapse) is safe to drop, but a
        # nested aggregation defines a NEW column: PromQL
        # ``min(sum(x) by (instance))`` emits an inner STATS grouped by instance
        # and an outer one producing ``<metric>_min``, which is what
        # ``metric_field`` names. Dropping that stage leaves the emitted
        # ``EVAL <series> = <metric>_min`` pointing at a column nothing defines,
        # and the panel fails in Kibana with "Unknown column". Refuse so it
        # falls back to a path that can express the shape.
        if _metric_field_is_defined_by_a_dropped_stage(
            metric_field, assignments, post_stats, eval_stages
        ):
            return None
        parsed.append(
            {
                "translation": translation,
                "source": source,
                "source_type": source_type,
                "pre_stats": stages[1:stats_idx],
                "assignments": assignments,
                "by_text": by_text,
                "eval_stages": eval_stages,
                "metric_field": metric_field,
            }
        )

    source_types = {item["source_type"] for item in parsed}
    if len(source_types) != 1:
        return None
    by_texts = {item["by_text"] for item in parsed}
    if len(by_texts) != 1:
        return None

    # Merge WHERE / filter stages.
    #
    # These stages must NOT simply be concatenated. Each target was translated
    # on its own, so its pre-STATS filters describe only that target: a metric
    # presence guard (``node_load5 IS NOT NULL``), a label matcher
    # (``mode == "idle"``), a device filter (``fstype RLIKE ...``). Emitting
    # them all as sibling ``| WHERE`` stages ANDs them across every target, and
    # since no single document carries every target's metric the fused query
    # matches nothing at all -- a panel that silently renders empty rather than
    # erroring, which is the worst possible failure mode.
    #
    # Keep only the stages EVERY target shares as global filters; fold each
    # target's own filters into its measure with CASE, exactly as the
    # formula-plan fusion path does. If a target-specific filter cannot be
    # folded, refuse the merge so the panel degrades to a path that is correct.
    common_pre: list[str] = []
    for stage in parsed[0]["pre_stats"]:
        key = stage.strip()
        if key and all(
            any(other.strip() == key for other in item["pre_stats"]) for item in parsed[1:]
        ):
            if key not in {existing.strip() for existing in common_pre}:
                common_pre.append(stage)
    common_keys = {stage.strip() for stage in common_pre}

    scoped_filters_by_item: dict[int, list[str]] = {}
    for position, item in enumerate(parsed):
        scoped: list[str] = []
        for stage in item["pre_stats"]:
            key = stage.strip()
            if not key or key in common_keys:
                continue
            if not key.upper().startswith("WHERE "):
                # Only filters can be folded into a measure; anything else
                # (EVAL, DROP, ...) would change the shared pipeline.
                return None
            scoped.append(key[len("WHERE ") :].strip())
        scoped_filters_by_item[position] = scoped
    merged_pre = common_pre

    used_aliases: set[str] = set()
    renamed_assignments: list[str] = []
    metric_fields: list[str] = []
    metric_label_hints: dict[str, str] = {}
    target_provenance: list[dict[str, object]] = []
    eval_parts: list[str] = []
    warnings: list[str] = [
        "Fused multi-target panel from independently translated ES|QL queries"
    ]

    for idx, item in enumerate(parsed, start=1):
        translation = item["translation"]
        scoped_filters = scoped_filters_by_item.get(idx - 1) or []
        alias_hint = translation.metadata.get("target_ref_id") or f"series_{idx}"
        raw_alias = (
            translation.metadata.get("series_alias")
            or translation.output_metric_field
            or translation.metric_name
            or f"series_{idx}"
        )
        result_alias = _unique_safe_alias(
            raw_alias,
            used_aliases,
            fallback_suffix=alias_hint,
        )
        # Prefix STATS aliases so receive/transmit OR-chains don't collide.
        alias_map: dict[str, str] = {}
        for assignment in item["assignments"]:
            left, right = assignment.split("=", 1)
            old_alias = _canonical_esql_alias(left)
            new_alias = _unique_safe_alias(
                f"{old_alias}_{alias_hint}",
                used_aliases,
                fallback_suffix=str(idx),
            )
            alias_map[old_alias] = new_alias
            measure_expr = right.strip()
            if scoped_filters:
                measure_expr = _inline_filters_into_stats_expr(measure_expr, scoped_filters)
                if not measure_expr:
                    return None
            renamed_assignments.append(f"{_esql_identifier(new_alias)} = {measure_expr}")

        # Rewrite EVAL expressions to use renamed STATS aliases, then bind the
        # final series column to ``result_alias``.
        # When there are no EVAL stages the STATS output name *is* the metric —
        # remap it through alias_map so legend EVAL does not reference the
        # pre-rename column (Node Exporter "CPU Frequency Scaling" smoke miss /
        # fused multi-target STATS aliases such as node_network_up -> ..._A).
        metric_field = _canonical_esql_alias(item["metric_field"])
        rewritten_metric_expr = metric_field
        if rewritten_metric_expr in alias_map:
            rewritten_metric_expr = alias_map[rewritten_metric_expr]
        for eval_stage in item["eval_stages"]:
            body = eval_stage[len("EVAL ") :].strip()
            if "=" not in body:
                continue
            left, right = body.split("=", 1)
            expr = _rewrite_esql_alias_references(right.strip(), alias_map)
            out_name = _canonical_esql_alias(left)
            if out_name == metric_field:
                rewritten_metric_expr = expr
            else:
                mapped = alias_map.get(out_name) or _unique_safe_alias(
                    f"{out_name}_{alias_hint}",
                    used_aliases,
                    fallback_suffix=str(idx),
                )
                alias_map[out_name] = mapped
                eval_parts.append(f"| EVAL {_esql_identifier(mapped)} = {expr}")

        if translation.metadata.get("negate_result"):
            rewritten_metric_expr = f"(-1 * {rewritten_metric_expr})"
        eval_parts.append(
            f"| EVAL {_esql_identifier(result_alias)} = {rewritten_metric_expr}"
        )
        metric_fields.append(result_alias)
        metric_label_hints[result_alias] = str(raw_alias)
        provenance_entry: dict[str, object] = {
            "ref_id": alias_hint,
            "source_expr": str(translation.metadata.get("target_source_expr") or ""),
            "value_column": result_alias,
            "whole_translated": True,
        }
        if translation.metadata.get("negate_result"):
            provenance_entry["negated"] = True
        target_provenance.append(provenance_entry)
        for warning in translation.warnings or []:
            if warning not in warnings:
                warnings.append(warning)

    by_text = next(iter(by_texts))
    group_fields = [
        part.strip().split("=", 1)[0].strip()
        for part in _split_top_level_csv(by_text)
        if part.strip()
    ]

    renamed_assignments = _finalize_fused_stats_assignments(
        renamed_assignments,
        group_fields=group_fields,
        source_type=next(iter(source_types)),
    )

    def _pipe_stage(stage: str) -> str:
        text = stage.strip()
        if not text:
            return text
        return text if text.startswith("|") else f"| {text}"

    parts = [parsed[0]["source"], *(_pipe_stage(stage) for stage in merged_pre)]
    parts.append(
        "| STATS "
        + ", ".join(renamed_assignments)
        + f" BY {by_text}"
    )
    parts.extend(_pipe_stage(stage) for stage in eval_parts)
    parts.append(
        "| KEEP "
        + ", ".join(
            _esql_identifier(f)
            for f in dict.fromkeys(group_fields + metric_fields)
        )
    )
    if "time_bucket" in group_fields:
        parts.append("| SORT time_bucket ASC")
    return {
        "query": "\n".join(parts),
        "metric_fields": metric_fields,
        "metric_label_hints": metric_label_hints,
        "group_fields": group_fields,
        "source_type": next(iter(source_types)),
        "warnings": warnings,
        "targets": target_provenance,
    }


def _build_multi_target_series_query(translations):
    if not translations:
        return None

    base = translations[0]
    post_filters: dict[int, dict] = {}
    comp_ops = {"==": "==", "!=": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<="}

    def _preserve_legend_grouping_for_multi_target() -> bool:
        """Keep a shared legend placeholder as a real grouping field when needed.

        Single-target TS translations intentionally treat a legend-only label like
        ``{{device}}`` as display-only so they can avoid the distorting
        ``AVG(IRATE(...)) BY device`` wrapper. That tradeoff breaks down for
        multi-target overlays whose source legends are ``{{device}} - Receive`` /
        ``{{device}} - Transmit``: dropping ``device`` collapses every interface
        into one aggregate and loses the panel's actual series identity.

        Restrict the opt-out to the narrow, node-exporter-style case where every
        target contributes the SAME single legend placeholder. Broader/mixed
        legend patterns keep the existing display-only behavior.
        """
        shared_labels: tuple[str, ...] | None = None
        saw_legend_origin = False
        for translation in translations:
            metadata = getattr(translation, "metadata", {}) or {}
            if metadata.get("preferred_group_labels_origin") != "legend":
                return False
            labels = tuple(str(label).strip() for label in (metadata.get("preferred_group_labels") or []) if str(label).strip())
            if len(labels) != 1:
                return False
            if shared_labels is None:
                shared_labels = labels
            elif labels != shared_labels:
                return False
            saw_legend_origin = True
        return saw_legend_origin

    def _build_plans(allow_tsds_gauge_promotion):
        plans = []
        all_specs = []
        warnings = []
        preserve_legend_grouping = _preserve_legend_grouping_for_multi_target()
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
                drop_legend_labels=not preserve_legend_grouping,
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

    # Dedup: when a formula target's intermediate spec and a simple target's spec
    # both compute the same aggregation (same stats_expr, source_type, filters),
    # rewrite the formula's plan.expr to use the simple-target alias directly and
    # remove the redundant intermediate spec from all_specs.  This avoids emitting
    # two identical STATS aggregations (e.g. two ``SUM(same_metric)`` under
    # different internal aliases).
    _simple_content: dict = {}  # (source_type, stats_expr, filters_key) -> alias
    for _, _p in plans:
        if len(_p.specs) == 1 and not _p.specs[0].eval_expr:
            _s = _p.specs[0]
            _key = (_s.source_type, _s.stats_expr, tuple(sorted(_s.filters or [])))
            if _key not in _simple_content:
                _simple_content[_key] = _s.alias
    _formula_remap: dict = {}  # intermediate_alias -> canonical_simple_alias
    for _, _p in plans:
        if len(_p.specs) <= 1:
            continue
        for _spec in _p.specs:
            _key = (_spec.source_type, _spec.stats_expr, tuple(sorted(_spec.filters or [])))
            _canon = _simple_content.get(_key)
            if _canon and _canon != _spec.alias:
                _formula_remap[_spec.alias] = _canon
    if _formula_remap:
        for _, _p in plans:
            for _old, _new in _formula_remap.items():
                _p.expr = re.sub(rf"\b{re.escape(_old)}\b", _new, _p.expr)
        all_specs = [_s for _s in all_specs if _s.alias not in _formula_remap]

    shared = _build_shared_measure_pipeline(base.index, all_specs)
    if not shared:
        return None

    parts, output_group_fields, _ = shared
    if len({tuple(spec.group_fields or []) for spec in all_specs}) > 1:
        warnings.append(_mismatched_grouping_union_warning(all_specs, plans))
    metric_fields = []
    metric_label_hints: dict[str, str] = {}
    target_provenance: list[dict[str, str]] = []
    used_aliases = set()
    _stats_renames: dict[str, str] = {}  # old_alias -> new_alias from inline-STATS opt
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
        #
        # Optimisation: when this EVAL is a pure column rename (single spec,
        # no negation/post_filter applied, no intermediate eval_expr on the
        # spec), inline the final alias directly into the STATS term and skip
        # the EVAL.  This eliminates one pipeline step per simple-metric target
        # (e.g. ``| STATS hits_A = IRATE(...)  | EVAL hits = hits_A`` becomes
        # ``| STATS hits = IRATE(...)``).
        if (
            eval_expr == plan.expr
            and len(plan.specs) == 1
            and plan.expr == plan.specs[0].final_alias
            and not plan.specs[0].eval_expr
            and result_alias != eval_expr
        ):
            old_col = _esql_identifier(plan.expr)
            new_col = _esql_identifier(result_alias)
            stats_idx = next(
                (i for i in range(len(parts) - 1, -1, -1) if parts[i].lstrip().startswith("| STATS")),
                None,
            )
            if stats_idx is not None:
                renamed = parts[stats_idx].replace(f"{old_col} =", f"{new_col} =", 1)
                if renamed != parts[stats_idx]:
                    parts[stats_idx] = renamed
                    _stats_renames[plan.expr] = result_alias
                    metric_fields.append(result_alias)
                    metric_label_hints[result_alias] = raw_alias
                    continue
        parts.append(f"| EVAL {_esql_identifier(result_alias)} = {eval_expr}")
        metric_fields.append(result_alias)
        metric_label_hints[result_alias] = raw_alias

    # Propagate inline-STATS renames to any formula EVALs that reference old aliases.
    # The inline-STATS optimisation renames a STATS column to the legend alias but
    # only skips the EVAL for *that* target; other targets that reference the old
    # alias in their own EVAL expressions still need updating.
    if _stats_renames:
        for _i, _part in enumerate(parts):
            if not _part.lstrip().startswith("| EVAL"):
                continue
            _updated = _part
            for _old, _new in _stats_renames.items():
                _updated = re.sub(rf"\b{re.escape(_old)}\b", _new, _updated)
            if _updated != _part:
                parts[_i] = _updated

    # Inject EVAL CONCAT for label_join targets.  The label_join wrapper is
    # unwrapped in _build_formula_plan so the shared STATS is built from the
    # inner fragment; here we restore the post-STATS CONCAT and extend
    # output_group_fields so the KEEP clause retains the derived column.
    # Deduplicate by (dst, concat_expr) pair: two targets with the same dst AND
    # the same expression share one EVAL; different dst names always get their
    # own EVAL (skipping the EVAL while adding the dst to KEEP would cause an
    # ES|QL "Unknown column" error).
    _lj_extra_group: list[str] = []
    _lj_seen: set[tuple] = set()
    for _tr, _ in plans:
        _frag = getattr(_tr, "fragment", None)
        if _frag is None or _frag.family != "label_join":
            continue
        _lj_dst = _frag.extra.get("lj_dst") or ""
        _lj_sep = _frag.extra.get("lj_sep") or ""
        _lj_src = _frag.extra.get("lj_src") or []
        if not _lj_dst or not _lj_src:
            continue
        _sep_lit = f'"{_lj_sep}"'
        _concat_args = []
        for _i2, _src in enumerate(_lj_src):
            if _i2 > 0:
                _concat_args.append(_sep_lit)
            _concat_args.append(_esql_identifier(_src))
        _concat_expr = f"CONCAT({', '.join(_concat_args)})"
        _lj_key = (_lj_dst, _concat_expr)
        if _lj_key not in _lj_seen:
            _lj_seen.add(_lj_key)
            parts.append(f"| EVAL {_esql_identifier(_lj_dst)} = {_concat_expr}")
        if _lj_dst not in output_group_fields and _lj_dst not in _lj_extra_group:
            _lj_extra_group.append(_lj_dst)
    if _lj_extra_group:
        output_group_fields = list(output_group_fields) + _lj_extra_group

    summary_mode = all(_summary_mode_from_metadata(translation.metadata) for translation, _ in plans)
    collapsed = None
    if summary_mode and plans[0][1].specs:
        _panel_type = plans[0][0].panel_type if plans else ""
        collapsed = _collapse_summary_ts_query(
            parts, output_group_fields, metric_fields,
            keep_time_bucket=_panel_type in {"table", "table-old"},
        )
    if collapsed is None:
        # The KEEP is dropped downstream by _strip_dotted_group_keep when a
        # grouping field is dotted (avoids an ES|QL "Output has changed" error).
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
    items = list(translations)
    if not items:
        return False
    indexes = {_translation_query_index(t) for t in items}
    indexes.discard("")
    # Distinct non-empty indexes cannot share one ES|QL pipeline.
    if len(indexes) > 1:
        return False
    if _build_multi_target_series_query(items) is not None:
        return True
    return _merge_pretranslated_xy_queries(items) is not None


def _translation_query_index(translation) -> str:
    """Return the ES|QL index pattern for a translated target."""
    query = getattr(translation, "esql_query", None) or ""
    if query:
        from observability_migration.adapters.source.grafana.esql_validate import (
            _query_source_and_index,
        )

        _, index = _query_source_and_index(query)
        if index:
            return str(index).strip()
    return str(getattr(translation, "index", "") or "").strip()


def _partition_translations_by_index(translations):
    """Group translations by ES|QL index; empty index buckets last."""
    groups: dict[str, list] = {}
    for translation in translations:
        index = _translation_query_index(translation) or ""
        groups.setdefault(index, []).append(translation)
    ordered = []
    for index in sorted(k for k in groups if k):
        ordered.append((index, groups[index]))
    if "" in groups:
        ordered.append(("", groups[""]))
    return ordered


def _fuse_same_index_series(group):
    """Merge one same-index translation group into a layer dict, or None."""
    if not group:
        return None
    if len(group) == 1:
        t = group[0]
        metric_fields = list(t.metadata.get("multi_series_metric_fields") or [])
        if not metric_fields and t.output_metric_field:
            metric_fields = [t.output_metric_field]
        return {
            "query": t.esql_query,
            "source_type": t.source_type,
            "metric_fields": metric_fields,
            "metric_label_hints": dict(t.metadata.get("multi_series_metric_labels") or {}),
            "group_fields": list(t.output_group_fields or []),
            "warnings": list(t.warnings or []),
            "targets": [
                {
                    "ref_id": t.metadata.get("target_ref_id") or "",
                    "source_expr": str(t.metadata.get("target_source_expr") or ""),
                    "whole_translated": True,
                }
            ],
            "index": _translation_query_index(t),
        }
    merged = _build_multi_target_series_query(group)
    if merged is None:
        merged = _merge_pretranslated_xy_queries(group)
    if not merged:
        return None
    return {
        "query": merged["query"],
        "source_type": merged["source_type"],
        "metric_fields": list(merged.get("metric_fields") or []),
        "metric_label_hints": dict(merged.get("metric_label_hints") or {}),
        "group_fields": list(merged.get("group_fields") or []),
        "warnings": list(merged.get("warnings") or []),
        "targets": list(merged.get("targets") or []),
        "index": _translation_query_index(group[0]),
    }


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


# Grafana ``target.dsType`` / datasource ``type`` values for query languages this
# engine does not translate. Naming the language turns a confusing "no PromQL
# found" into an accurate "this panel is not Prometheus".
_NON_PROMQL_QUERY_LANGUAGES = {
    "influxdb": "InfluxQL/Flux (InfluxDB)",
    "elasticsearch": "Elasticsearch DSL",
    "graphite": "Graphite",
    "mysql": "SQL (MySQL)",
    "postgres": "SQL (PostgreSQL)",
    "mssql": "SQL (MSSQL)",
    "cloudwatch": "CloudWatch metric queries",
    "stackdriver": "Google Cloud Monitoring",
    "azuremonitor": "Azure Monitor",
    "loki": "LogQL (Loki)",
    "tempo": "Tempo trace queries",
}


def _panel_query_language(panel):
    """Name the non-PromQL query language a panel uses, if it is identifiable."""
    seen = []
    sources = [panel.get("datasource")] + [
        t.get("dsType") or t.get("datasource")
        for t in (panel.get("targets") or [])
        if isinstance(t, dict)
    ]
    for src in sources:
        kind = src.get("type") if isinstance(src, dict) else src
        label = _NON_PROMQL_QUERY_LANGUAGES.get(str(kind or "").strip().lower())
        if label and label not in seen:
            seen.append(label)
    return " / ".join(seen)


def _make_placeholder_panel(yaml_panel, title, panel_type, kibana_type, panel=None):
    language = _panel_query_language(panel or {})
    if language:
        # Not a failure to parse: the panel is simply not Prometheus-backed, and
        # this engine translates PromQL. Say which language so the operator knows
        # to rebuild it rather than hunting for a parser bug.
        detail = (
            f"Panel queries {language}, not PromQL; this migration translates "
            "Prometheus queries, so it must be rebuilt against an Elasticsearch "
            "data source"
        )
        content = f"**{title}**\n\n*(Placeholder: original {panel_type} panel queries {language}, not PromQL)*"
    else:
        detail = "No PromQL expression found in panel targets"
        content = f"**{title}**\n\n*(Placeholder: original {panel_type} panel had no PromQL targets)*"
    yaml_panel["markdown"] = {"content": content}
    return yaml_panel, PanelResult(
        title,
        panel_type,
        "markdown",
        "requires_manual",
        0.3,
        reasons=[detail],
    )


_extract_esql_shape = _extract_esql_shape_canonical
_extract_esql_columns = _extract_esql_columns_canonical


_TIME_DIMENSION_FIELDS = {"time_bucket", "timestamp_bucket", "step"}
_CURATED_QUERY_TOKEN_RE = re.compile(
    r"\{\{\s*(?P<kind>control|label|metric):(?P<name>[A-Za-z0-9_.-]+)"
    r"(?::(?P<prefer>counter|gauge))?\s*\}\}"
)


def _dimension_field(field_name):
    dimension = {"field": field_name}
    if field_name in _TIME_DIMENSION_FIELDS:
        dimension["data_type"] = "date"
    return dimension


def _materialize_curated_query_override(query, resolver):
    if not query:
        return query

    def _replace(match):
        kind = match.group("kind")
        name = match.group("name")
        prefer = match.group("prefer") or None
        if resolver is None:
            return name
        try:
            if kind == "control":
                resolve = getattr(resolver, "resolve_control_field", None)
                resolved = resolve(name) if callable(resolve) else None
                return resolved or name
            if kind == "label":
                resolve = getattr(resolver, "resolve_label", None)
                resolved = resolve(name) if callable(resolve) else None
                return resolved or name
            if kind == "metric":
                resolve = getattr(resolver, "resolve_metric_field", None)
                resolved = resolve(name, prefer=prefer) if callable(resolve) else None
                return resolved or name
        except Exception:
            return name
        return name

    return _CURATED_QUERY_TOKEN_RE.sub(_replace, query)


def _curated_metric_token_pattern(metric_name: str) -> re.Pattern[str]:
    return re.compile(
        rf"\{{\{{\s*metric\s*:\s*{re.escape(metric_name)}\s*(?::(?:counter|gauge))?\s*\}}\}}",
        re.IGNORECASE,
    )


@dataclass
class _CuratedOptionalMetricStripResult:
    query: str
    removed_aliases: list[str] = field(default_factory=list)
    exhausted: bool = False


@dataclass
class _CuratedOptionalMetricOmissionResult:
    query: str
    exhausted_metrics: list[str] = field(default_factory=list)
    omitted_metrics: list[str] = field(default_factory=list)


def _live_optional_metric_is_absent(metric_name: str, resolver) -> bool:
    """Return True when live field-caps prove *metric_name* is missing."""
    return bool(metric_name) and metric_name in _live_missing_metrics_for_expr(
        metric_name, resolver
    )


def _split_top_level_boolean_terms(text: str, keyword: str) -> list[str]:
    """Split a boolean expression on a top-level keyword such as ``OR``."""
    parts: list[str] = []
    remaining = str(text or "").strip()
    while remaining:
        head, tail = _split_top_level_keyword(remaining, keyword)
        if not tail:
            parts.append(head.strip())
            break
        parts.append(head.strip())
        remaining = tail.strip()
    return [part for part in parts if part]


def _esql_expr_references_aliases(expression: str, aliases: set[str]) -> bool:
    """True when *expression* uses any identifier in *aliases* outside quotes."""
    if not expression or not aliases:
        return False
    for match in _ESQL_ALIAS_TOKEN_RE.finditer(expression):
        token = match.group(0)
        if token.startswith(("'", '"', "`")):
            continue
        if _canonical_esql_alias(token) in aliases:
            return True
    return False


def _tail_is_removed_unpivot_piece(tail: str, removed_aliases: set[str]) -> bool:
    """True when an ``MV_APPEND(inner, tail)`` tail is a stripped optional series."""
    if _esql_expr_references_aliases(tail, removed_aliases):
        return True
    stripped = str(tail or "").strip()
    if len(stripped) >= 2 and stripped[0] in {'"', "'"} and stripped[-1] == stripped[0]:
        text = stripped[1:-1]
        for alias in removed_aliases:
            if text == alias or text.startswith(f"{alias} - "):
                return True
    return False


def _unwrap_removed_unpivot_mv_appends(expression: str, removed_aliases: set[str]) -> str:
    """Peel ``MV_APPEND(inner, stripped_series)`` layers left by optional omit."""
    expr = str(expression or "").strip()
    while True:
        upper = expr.upper()
        if not upper.startswith("MV_APPEND(") or not expr.endswith(")"):
            return expr
        body = expr[len("MV_APPEND("):-1]
        parts = [part.strip() for part in _split_top_level_csv(body) if part.strip()]
        if len(parts) != 2:
            return expr
        inner, tail = parts
        if not _tail_is_removed_unpivot_piece(tail, removed_aliases):
            return expr
        expr = inner.strip()


def _strip_optional_metric_token_from_curated_esql_result(
    query: str,
    metric_name: str,
) -> _CuratedOptionalMetricStripResult:
    """Remove one optional ``{{metric:...}}`` series from a curated ES|QL override."""
    if not query or not metric_name:
        return _CuratedOptionalMetricStripResult(query=query)
    token_re = _curated_metric_token_pattern(metric_name)
    if not token_re.search(query):
        return _CuratedOptionalMetricStripResult(query=query)

    removed_aliases: list[str] = []
    stripped_stages: list[str] = []
    removed_alias_set: set[str] = set()
    for stage in _split_esql_pipeline(query):
        stripped = str(stage or "").strip()
        upper = stripped.upper()
        if upper.startswith("WHERE "):
            predicates = _split_top_level_boolean_terms(stripped[6:].strip(), "OR")
            kept_predicates = []
            for predicate in predicates:
                if token_re.search(predicate):
                    continue
                if _esql_expr_references_aliases(predicate, removed_alias_set):
                    continue
                kept_predicates.append(predicate)
            if kept_predicates:
                stripped_stages.append("WHERE " + " OR ".join(kept_predicates))
            continue
        if upper.startswith("STATS "):
            stats_body = stripped[6:].strip()
            assignments_text, by_text = _split_top_level_keyword(stats_body, "BY")
            assignments = [
                part.strip()
                for part in _split_top_level_csv(assignments_text)
                if part.strip()
            ]
            kept_assignments: list[str] = []
            for assignment in assignments:
                left, right = _split_top_level_assignment(assignment)
                alias = _canonical_esql_alias(left)
                if token_re.search(right or assignment):
                    if alias:
                        _append_unique(removed_aliases, alias)
                        removed_alias_set.add(alias)
                    continue
                kept_assignments.append(assignment)
            if not kept_assignments:
                return _CuratedOptionalMetricStripResult(
                    query="",
                    removed_aliases=removed_aliases,
                    exhausted=True,
                )
            rebuilt = "STATS " + ", ".join(kept_assignments)
            if by_text:
                rebuilt += f" BY {by_text}"
            stripped_stages.append(rebuilt)
            continue
        if upper.startswith("EVAL "):
            assignments = [
                part.strip()
                for part in _split_top_level_csv(stripped[5:].strip())
                if part.strip()
            ]
            changed = True
            while changed:
                changed = False
                kept_assignments: list[str] = []
                for assignment in assignments:
                    left, right = _split_top_level_assignment(assignment)
                    rhs = right if right is not None else assignment
                    rewritten = _unwrap_removed_unpivot_mv_appends(
                        rhs, removed_alias_set
                    )
                    if rewritten != rhs:
                        assignment = (
                            f"{left} = {rewritten}" if left else rewritten
                        )
                        rhs = rewritten
                        changed = True
                    if token_re.search(rhs) or _esql_expr_references_aliases(
                        rhs, removed_alias_set
                    ):
                        alias = _canonical_esql_alias(left) if left else ""
                        if alias:
                            _append_unique(removed_aliases, alias)
                            removed_alias_set.add(alias)
                        changed = True
                        continue
                    kept_assignments.append(assignment)
                assignments = kept_assignments
            if assignments:
                stripped_stages.append("EVAL " + ", ".join(assignments))
            continue
        if upper.startswith("KEEP ") and removed_aliases:
            keep_parts = [
                part.strip()
                for part in _split_top_level_csv(stripped[5:].strip())
                if part.strip()
            ]
            kept_parts = [
                part
                for part in keep_parts
                if _canonical_esql_alias(part) not in removed_alias_set
            ]
            if kept_parts:
                stripped_stages.append("KEEP " + ", ".join(kept_parts))
            continue
        stripped_stages.append(stripped)
    return _CuratedOptionalMetricStripResult(
        query=" | ".join(stripped_stages),
        removed_aliases=removed_aliases,
    )


def _strip_optional_metric_token_from_curated_esql(query: str, metric_name: str) -> str:
    """Remove one optional ``{{metric:...}}`` series from a curated ES|QL override.

    Handles the common Node Exporter shape: an ``OR … IS NOT NULL`` WHERE term,
    a ``alias = AVG(IRATE(...))`` STATS assignment, and the matching KEEP column.
    """
    return _strip_optional_metric_token_from_curated_esql_result(query, metric_name).query


def _omit_absent_optional_metrics_from_curated_query_result(
    query,
    optional_metrics,
    resolver,
) -> _CuratedOptionalMetricOmissionResult:
    """Strip live-optional metric tokens that field-caps prove are absent."""
    if not query:
        return _CuratedOptionalMetricOmissionResult(query=query)
    metrics = [str(name).strip() for name in (optional_metrics or []) if str(name).strip()]
    if not metrics or not resolver:
        return _CuratedOptionalMetricOmissionResult(query=query)
    out = str(query)
    exhausted_metrics: list[str] = []
    omitted_metrics: list[str] = []
    for metric_name in metrics:
        if not _live_optional_metric_is_absent(metric_name, resolver):
            continue
        strip_result = _strip_optional_metric_token_from_curated_esql_result(out, metric_name)
        if strip_result.query != out:
            _append_unique(omitted_metrics, metric_name)
        if strip_result.exhausted:
            _append_unique(exhausted_metrics, metric_name)
            return _CuratedOptionalMetricOmissionResult(
                query="",
                exhausted_metrics=exhausted_metrics,
                omitted_metrics=omitted_metrics,
            )
        out = strip_result.query
    return _CuratedOptionalMetricOmissionResult(
        query=out,
        omitted_metrics=omitted_metrics,
    )


def _omit_absent_optional_metrics_from_curated_query(query, optional_metrics, resolver):
    """Strip live-optional metric tokens that field-caps prove are absent.

    Curated overrides are otherwise materialized verbatim; without this step an
    optional collector metric listed in ``live_optional_metrics`` still hard-
    fails the whole panel when referenced in a hand-written override.
    """
    return _omit_absent_optional_metrics_from_curated_query_result(
        query,
        optional_metrics,
        resolver,
    ).query


def _live_missing_metrics_for_expr(expr, resolver):
    if not expr or not resolver:
        return []
    discovery_status = getattr(resolver, "discovery_status", lambda: {})()
    if discovery_status.get("status") != "ok":
        return []

    missing: list[str] = []
    resolve_metric = getattr(resolver, "resolve_metric_field", None)
    field_exists = getattr(resolver, "field_exists", None)
    if not callable(field_exists):
        return []

    for metric in sorted(_metrics_in_expr(expr)):
        candidates = [metric]
        if callable(resolve_metric):
            for prefer in ("gauge", "counter"):
                resolved = resolve_metric(metric, prefer=prefer)
                if resolved and resolved not in candidates:
                    candidates.append(resolved)
        statuses = [field_exists(candidate) for candidate in candidates if candidate]
        if any(status is True for status in statuses):
            continue
        if any(status is None for status in statuses):
            continue
        _append_unique(missing, metric)
    return missing


def _live_optional_source_metric_absent(metric, resolver, optional_names):
    """True when *metric* is pack-optional and live field-caps proved it absent.

    Curated overrides sometimes replace a source metric the target never
    ingested (postgres_exporter dropping ``pg_postmaster_start_time_seconds``)
    rather than listing it in the override text for later stripping. Those
    substitutions must not yellow the panel as a pack omission.
    """
    if metric not in optional_names or not metric:
        return False
    return metric in _live_missing_metrics_for_expr(metric, resolver)


def _source_metrics_absent_from_query(source_exprs, query_text, resolver):
    """Prometheus metrics referenced by *source_exprs* that never appear in the
    final emitted *query_text*.

    Complements ``_live_missing_metrics_for_expr``, which flags a metric that
    is absent from the *target's schema* (a data gap). This instead flags a
    metric the translator itself dropped while building the final query, even
    though the metric is queryable -- e.g. a target folded into a multi-target
    fusion whose column never made it into the emitted STATS/EVAL (issue
    #352), or a curated ``query_overrides`` entry that omits a source metric
    the pack author never accounted for (issue #349). Callers are responsible
    for only passing exprs/metrics not already explained by another check
    (live-missing metrics, incompatible-target drops) to avoid double
    reporting the same gap under two different reasons.

    Requires live field-caps discovery to have actually run (same gate as
    ``_live_missing_metrics_for_expr``): without a real target schema to
    resolve field names against, a bare metric-name substring match against
    the emitted query text is unreliable and would false-positive on curated
    overrides/tests that legitimately rename or synthesize fields.
    """
    if not resolver:
        return []
    discovery_status = getattr(resolver, "discovery_status", lambda: {})()
    if discovery_status.get("status") != "ok":
        return []
    source_metrics: set[str] = set()
    for expr in source_exprs or []:
        source_metrics |= _metrics_in_expr(str(expr or ""))
    if not source_metrics or not query_text:
        return []
    resolve_metric = getattr(resolver, "resolve_metric_field", None)
    missing: list[str] = []
    for metric in sorted(source_metrics):
        candidates = {metric}
        if callable(resolve_metric):
            for prefer in ("gauge", "counter"):
                resolved = resolve_metric(metric, prefer=prefer)
                if resolved:
                    candidates.add(resolved)
        if not any(candidate and candidate in query_text for candidate in candidates):
            _append_unique(missing, metric)
    return missing


def _make_missing_telemetry_panel(yaml_panel, title, panel_type, missing_metrics):
    metrics_text = ", ".join(sorted(dict.fromkeys(missing_metrics)))
    yaml_panel["markdown"] = {
        "content": (
            f"**{title}**\n\n"
            f"*(Telemetry missing in target: {metrics_text}. Re-upload after ingesting those metrics.)*"
        )
    }
    return yaml_panel, PanelResult(
        title,
        panel_type,
        "markdown",
        "migrated_with_warnings",
        0.4,
        reasons=[f"Target telemetry missing: {metrics_text}"],
    )


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
    for token, mapped in named.items():
        if lowered.endswith(f"-{token}"):
            return mapped
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
    """Append ``| EVAL field = literal`` constants for gauge accessors.

    Curated pack queries often already emit ``_gauge_min`` / ``_gauge_max`` /
    ``_gauge_goal``. Re-appending them duplicates the stage (harmless to ES|QL
    but noisy in reviews and SO diffs), so skip any field already assigned in
    the query text.
    """
    assignments = []
    for field_name, value in constants.items():
        number = _coerce_number(value)
        if number is None:
            continue
        if re.search(rf"(?m)\b{re.escape(field_name)}\s*=", esql or ""):
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


def _build_metric_color_mapping(panel, minimum=None, maximum=None):
    """Like ``_build_gauge_color_mapping`` but for metric/stat panels, which
    (unlike gauges) have no inherent axis domain: a top threshold step with no
    configured ``max`` still needs a color band, just an open-ended one.

    ``ColorThreshold.up_to`` requires a real number for every band (the YAML
    schema has no "open-ended" marker), so the unbounded top band gets a
    placeholder. It doesn't distort the rendered colors: the target mapper
    (``dashboards_api._api_color``) only uses a threshold's ``up_to`` to
    derive the *next* band's lower edge — the last band's own ``up_to`` is
    never turned into an upper bound, so any number at or above its own
    ``current_value`` is a safe stand-in.
    """
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
        next_value = steps[index + 1].get("value") if index + 1 < len(steps) else maximum
        is_placeholder = next_value is None
        if is_placeholder:
            next_value = current_value if current_value is not None else (minimum or 0.0)
        if maximum is not None and next_value > maximum:
            next_value = maximum
        if minimum is not None and next_value < minimum:
            next_value = minimum
        if thresholds and next_value <= thresholds[-1]["up_to"]:
            if not is_placeholder:
                continue
            # Placeholder band (open-ended top, no configured max): its
            # ``up_to`` is discarded downstream anyway, so nudge it strictly
            # above the previous cutoff instead of dropping the band outright.
            prev = thresholds[-1]["up_to"]
            next_value = prev + (abs(prev) * 1e-6 or 1e-6)
        thresholds.append({"up_to": next_value, "color": color})
    if not thresholds:
        return None
    color = {"thresholds": thresholds}
    if minimum is not None:
        color["range_min"] = minimum
    if maximum is not None:
        color["range_max"] = maximum
    return color


def _scale_metric_color_thresholds_to_percent_points(color):
    """Scale 0-1 metric threshold cutoffs into 0-100 percent points."""
    out = dict(color or {})
    thresholds = out.get("thresholds")
    if not isinstance(thresholds, list):
        return out
    scaled = []
    for step in thresholds:
        if not isinstance(step, dict):
            scaled.append(step)
            continue
        item = dict(step)
        up_to = item.get("up_to")
        if isinstance(up_to, (int, float)) and not isinstance(up_to, bool) and 0 <= up_to <= 1:
            item["up_to"] = up_to * 100.0
        scaled.append(item)
    out["thresholds"] = scaled
    return out


_ASCENDING_SORT_STAGE_RE = re.compile(
    r"^SORT\s+([A-Za-z_@][\w.@]*)(?:\s+ASC)?$",
    re.IGNORECASE,
)


def _already_ends_with_ascending_sort(esql, time_field):
    """True when *esql*'s last pipeline stage already sorts ascending on *time_field*.

    The comparison is on the *effective* clause rather than a byte-exact
    string, because raw ES|QL supplied by a dashboard author arrives as a
    single line with arbitrary spacing and casing (a line-based check misses
    it and emits a second, redundant ``| SORT`` -- see the "Native ESQL
    Errors" panel in ``multi-pattern-coverage.json``). ES|QL also treats an
    omitted direction as ``ASC``, so ``SORT time_bucket`` counts.

    A compound sort (``SORT time_bucket ASC, service.name``), a descending
    sort, a sort on another column, or a sort that is not the final stage are
    all *different* clauses: they do not match, and the caller still appends
    the ascending bucket sort it needs.
    """
    stages = [stage.strip() for stage in _split_esql_pipeline(esql) if stage.strip()]
    if len(stages) < 2:
        return False
    last_stage = " ".join(stages[-1].lstrip("|").split())
    match = _ASCENDING_SORT_STAGE_RE.match(last_stage)
    # Column names are case-sensitive in ES|QL, so only the keyword and the
    # direction tolerate case variation -- the field must match exactly.
    return bool(match) and match.group(1) == time_field


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
    if _already_ends_with_ascending_sort(esql, time_field):
        return esql
    lines.append(f"| SORT {time_field} ASC")
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


def _build_summary_category_bar_query(
    esql,
    metric_fields,
    metric_label_hints=None,
    *,
    scale_to_percent_points: bool = False,
):
    metric_fields = [field for field in (metric_fields or []) if field]
    if not esql or not metric_fields:
        return esql
    metric_label_hints = metric_label_hints or {}
    labels = [json.dumps(str(metric_label_hints.get(field, field) or field)) for field in metric_fields]
    # COALESCE each element: MV_APPEND propagates null, so a single metric with
    # no data in the target nulls the WHOLE array and every bar in the panel
    # comes back empty -- Node Exporter Full's "Pressure" rendered blank because
    # node_pressure_irq_stalled_seconds_total is absent, taking CPU, Mem and I/O
    # down with it. The "" placeholder survives MV_APPEND and TO_DOUBLE("")
    # yields null again at the far end, so only the absent metric's own bar is
    # empty.
    value_terms = [f'COALESCE(TO_STRING({field}), "")' for field in metric_fields]
    # ``gauge_value`` (not ``value``): Lens metric tiles have been observed to
    # bind breakdown labels while leaving a measure column literally named
    # ``value`` as N/A in the UI. Scale percentunit (0-1) into percent points
    # when the panel will display as number+% rather than Lens percent.
    gauge_expr = 'TO_DOUBLE(MV_LAST(SPLIT(__pairs, "\\t")))'
    if scale_to_percent_points:
        gauge_expr = f"({gauge_expr}) * 100"
    lines = esql.splitlines()
    lines.extend(
        [
            f"| EVAL __labels = {_nested_mv_append_expr(labels)}, __values = {_nested_mv_append_expr(value_terms)}",
            '| EVAL __pairs = MV_ZIP(__labels, __values, "\\t")',
            "| MV_EXPAND __pairs",
            f'| EVAL label = MV_FIRST(SPLIT(__pairs, "\\t")), gauge_value = {gauge_expr}',
            "| KEEP label, gauge_value",
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


def _seed_transform_metric_labels(panel, translation) -> None:
    """Ensure transform field resolution can map Grafana legends → ES|QL columns.

    Fusion populates ``multi_series_metric_labels``; single-target / partial-drop
    paths often leave it empty, so calculateField ``include: ["Real Linux"]``
    cannot resolve even when the primary metric *is* that series. Seed from the
    primary target's static legend (or series alias) when bookkeeping is absent.
    """
    metadata = getattr(translation, "metadata", None)
    if not isinstance(metadata, dict):
        return
    metric_field = str(getattr(translation, "output_metric_field", "") or "").strip()
    if not metric_field:
        return
    labels = dict(metadata.get("multi_series_metric_labels") or {})
    fields = list(metadata.get("multi_series_metric_fields") or [])
    if metric_field not in fields:
        fields = [metric_field] + [f for f in fields if f != metric_field]
        metadata["multi_series_metric_fields"] = fields
    if metric_field in labels and str(labels[metric_field]).strip():
        return
    legend = str(metadata.get("static_legend_label") or "").strip()
    if not legend:
        ref = str(metadata.get("target_ref_id") or "").strip()
        for target in panel.get("targets") or []:
            if not isinstance(target, dict):
                continue
            if ref and str(target.get("refId") or "").strip() != ref:
                continue
            legend = str(target.get("legendFormat") or "").strip()
            if legend:
                break
        if not legend:
            for target in panel.get("targets") or []:
                if not isinstance(target, dict) or target.get("hide"):
                    continue
                legend = str(target.get("legendFormat") or "").strip()
                if legend:
                    break
    if legend:
        labels[metric_field] = legend
        metadata["multi_series_metric_labels"] = labels


def _strip_dotted_group_keep(query):
    """Drop KEEP/DROP lines that re-project a dotted ``STATS BY`` grouping field.

    ES|QL's optimizer re-attributes a dotted grouping field (e.g. ``service.name``)
    from field -> reference across such a projection, raising a
    verification_exception "Output has changed from [..service.name{f}..] to
    [..service.name{r}..]" that breaks the panel in Kibana. Bisected live against
    Elastic 9.5.0; see tests/test_grafana_dotted_group_keep.py.

    Only projections that *include* a dotted grouping field are stripped.
    Transform-authored KEEP lines that project ``time_bucket`` + metrics (and
    omit the dotted group key) are preserved so calculateField/organize cleanup
    survives normalization.
    """
    if not query or "STATS" not in query:
        return query
    lines = query.splitlines()
    dotted_groups: set[str] = set()
    has_eval = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("| STATS") and " BY " in stripped:
            for token in stripped.split(" BY ", 1)[1].split(","):
                name = (token.split("=", 1)[-1] if "=" in token else token).strip().strip("`")
                if "." in name and "(" not in name:
                    dotted_groups.add(name)
        elif stripped.startswith("| EVAL"):
            has_eval = True
    if not (dotted_groups and has_eval):
        return query

    def _projects_dotted_group(line: str) -> bool:
        stripped = line.strip()
        if stripped.startswith("| KEEP "):
            body = stripped[len("| KEEP ") :]
        elif stripped.startswith("| DROP "):
            body = stripped[len("| DROP ") :]
        else:
            return False
        # DROP of non-group helpers/metrics is safe; only strip when a dotted
        # group key itself appears in the projection list.
        tokens = {
            part.strip().strip("`")
            for part in body.split(",")
            if part.strip()
        }
        return bool(tokens & dotted_groups)

    # Deleting the KEEP outright leaks every intermediate STATS alias into the
    # panel output (e.g. ``a``, ``b`` alongside ``computed_value``). Those extra
    # numeric columns are noise to Kibana and actively break numeric parity:
    # the oracle reads unpinned numeric columns as label dimensions, which
    # turned a 10-series panel into 375 and produced a false FAIL.
    #
    # A DROP of just the unwanted value columns expresses the same intent
    # without re-projecting the dotted grouping key, so it does not trip the
    # "Output has changed" optimizer error the strip exists to avoid.
    # Track the LIVE schema, not every alias ever produced. A STATS *replaces*
    # the output schema, so an alias from an earlier STATS that a later one does
    # not re-emit is already gone -- dropping it fails the whole query with
    # "Unknown column". Node Exporter Full's "Speed" and "Node Exporter Scrape"
    # died exactly this way: a summary-collapse STATS removed the inner alias and
    # the trailing DROP still named it.
    # The DROP is inserted where the stripped KEEP was, so it may only name
    # columns that exist AT THAT POINT. Accumulating over the whole pipeline
    # dropped names a later EVAL had not created yet -- Node Exporter Full's
    # "Node Exporter Scrape" emitted ``DROP __labels, __pairs, label`` above the
    # ``EVAL __labels = ...`` that defines them.
    keep_index = next(
        (i for i, line in enumerate(lines) if _projects_dotted_group(line)), len(lines)
    )
    produced: list[str] = []
    for line in lines[:keep_index]:
        stripped = line.strip()
        if stripped.startswith("| STATS"):
            head, _, grouping = stripped[len("| STATS "):].partition(" BY ")
            # A STATS emits its aggregate aliases plus its grouping keys, and
            # nothing else survives it.
            produced = []
            for part in _split_top_level_csv(head):
                if "=" in part:
                    produced.append(part.split("=", 1)[0].strip().strip("`"))
            # Split at top level only: a grouping key is routinely
            # ``time_bucket = TBUCKET(1, ?_tstart, ?_tend)``, and a naive comma
            # split reaches inside the call and yields ``?_tstart`` as a column
            # name -- which then lands in the DROP and fails every such query.
            for part in _split_top_level_csv(grouping):
                token = part.split("=", 1)[0].strip().strip("`") if "=" in part else part.strip().strip("`")
                if token and " " not in token and not token.startswith("?"):
                    produced.append(token)
        elif stripped.startswith("| EVAL"):
            body = stripped[len("| EVAL "):]
            for part in body.split(" = ")[:1]:
                name = part.strip().strip("`")
                if name and " " not in name:
                    produced.append(name)

    kept: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("| KEEP ") and _projects_dotted_group(line):
            kept |= {
                part.strip().strip("`")
                for part in stripped[len("| KEEP "):].split(",")
                if part.strip()
            }

    droppable = (
        [name for name in dict.fromkeys(produced) if name not in kept] if kept else []
    )
    drop_line = (
        "| DROP " + ", ".join(_esql_identifier(n) for n in droppable) if droppable else None
    )
    out: list[str] = []
    for line in lines:
        if _projects_dotted_group(line):
            # Substitute in place so the DROP keeps the KEEP's position in the
            # pipeline (before any trailing SORT) instead of trailing it.
            if drop_line is not None:
                out.append(drop_line)
                drop_line = None
            continue
        out.append(line)
    if drop_line is not None:
        out.append(drop_line)
    return "\n".join(out)


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
    query = _strip_dotted_group_keep(query)
    query = _strip_scalar_last_time_bucket_keep(query)
    esql_panel["query"] = _ensure_bucket_sort(query)
    yaml_panel["esql"] = esql_panel
    return yaml_panel


def _strip_scalar_last_time_bucket_keep(query):
    """Drop stale ``time_bucket`` from KEEP after a scalar ``LAST(..., time_bucket)``.

    A ``STATS value = LAST(value, time_bucket)`` with no ``BY`` collapses the
    time dimension. A trailing ``KEEP time_bucket, value`` is therefore invalid:
    the ``LAST`` output keeps only the aggregate alias, not ``time_bucket``.
    """
    if not query or "LAST(" not in query or "time_bucket" not in query:
        return query
    lines = query.splitlines()
    out: list[str] = []
    collapsed_scalar = False
    for line in lines:
        stripped = line.strip()
        if (
            stripped.startswith("| STATS")
            and "LAST(" in stripped
            and "time_bucket" in stripped
            and " BY " not in stripped
        ):
            collapsed_scalar = True
            out.append(line)
            continue
        if collapsed_scalar and stripped.startswith("| KEEP "):
            parts = _split_top_level_csv(stripped[len("| KEEP "):])
            filtered = [part for part in parts if part.strip().strip("`") != "time_bucket"]
            if filtered:
                out.append("| KEEP " + ", ".join(filtered))
            collapsed_scalar = False
            continue
        if stripped.startswith("| STATS"):
            collapsed_scalar = False
        out.append(line)
    return "\n".join(out)


def _percentunit_values_scaled_to_percent_points(panel, esql=None):
    """True when a ``percentunit`` panel's measure was rewritten to 0-100 points.

    ``bargauge_panel_rule`` (and the display enricher) multiply ratios by 100 so
    number+% metric tiles render correctly. Threshold / color-domain conversion
    must follow that same value-domain transform — not the presence of an
    explicit Grafana ``max``.
    """
    defaults = _panel_field_defaults(panel)
    return (
        str(defaults.get("unit") or "") == "percentunit"
        and bool(re.search(r"\*\s*100\b", esql or ""))
    )


def _panel_threshold_mode(panel):
    """Return Grafana ``fieldConfig.defaults.thresholds.mode`` (lowercased)."""
    thresholds = _panel_field_defaults(panel).get("thresholds") or {}
    if not isinstance(thresholds, dict):
        return ""
    return str(thresholds.get("mode") or "").strip().lower()


def _metric_display_domain(panel, esql=None):
    """Return ``(minimum, maximum)`` for metric color mapping.

    Grafana ``percentunit`` panels store data in 0-1 (often with ``max: 1``,
    sometimes with no ``max``). When the query scales into percent points via
    ``* 100``, keep color ``range_max`` on that 0-100 display domain so absolute
    threshold steps land in the same units as the measure.
    """
    defaults = _panel_field_defaults(panel)
    minimum = _coerce_number(defaults.get("min"))
    maximum = _coerce_number(defaults.get("max"))
    if _percentunit_values_scaled_to_percent_points(panel, esql):
        if minimum is not None and minimum <= 1:
            minimum = minimum * 100.0
        # Absent or fractional max → percent-point domain; leave an already
        # percent-point max (e.g. 100) alone.
        if maximum is None or maximum <= 1:
            maximum = 100.0
    return minimum, maximum


def _metric_threshold_color(panel, esql=None):
    """Map a Grafana stat/single-value panel's threshold steps to a Kibana
    metric ``primary.color`` (``MetricChartColor``: ``apply_to`` +
    ``thresholds``/``range_min``/``range_max``).

    Grafana stat panels color the value (or its background) by ``thresholds``;
    Kibana metric panels express the same via ``MetricChartColor``, the same
    ascending-``up_to`` threshold-band shape gauges use (``_build_gauge_color_
    mapping``), plus an ``apply_to`` mode. ``colorMode: none`` means Grafana
    explicitly disables value coloring, so we emit nothing in that case.
    """
    if not panel:
        return None
    options = panel.get("options") if isinstance(panel.get("options"), dict) else {}
    color_mode = str(options.get("colorMode") or "").strip().lower()
    if color_mode == "none":
        return None
    # A lone base step (``value: None``) is Grafana's default color, not a
    # threshold rule; without a real numeric boundary there is nothing to color
    # by, so emitting a bound-less single-band color would only force a constant
    # (often un-mapped) hue that misrepresents the panel.
    if not any(step.get("value") is not None for step in _gauge_threshold_steps(panel)):
        return None
    minimum, maximum = _metric_display_domain(panel, esql=esql)
    color = _build_metric_color_mapping(panel, minimum=minimum, maximum=maximum)
    if not color:
        return None
    # Absolute (raw-domain) fractional cutoffs move with the *100 value
    # transform. Percentage-mode cutoffs are already percent-of-range and must
    # not be re-scaled (0.8% of the range stays 0.8 after the domain shift).
    if (
        _percentunit_values_scaled_to_percent_points(panel, esql)
        and _panel_threshold_mode(panel) != "percentage"
    ):
        color = _scale_metric_color_thresholds_to_percent_points(color)
    color["apply_to"] = "background" if color_mode.startswith("background") else "value"
    return color


def _build_esql_metric_panel(esql, metric_col=None, panel=None, breakdown_col=None):
    esql = _ensure_bucket_sort(esql)
    if not metric_col:
        metric_col, _ = _extract_esql_columns(esql)
    primary = {"field": metric_col}
    color = _metric_threshold_color(panel, esql=esql)
    if color:
        primary["color"] = color
    out = {
        "type": "metric",
        "query": esql,
        "primary": primary,
    }
    # Multi-value Grafana bargauge summaries unpivot to label/value rows. Lens
    # XY categorical bars often render as empty for that shape; metric tiles
    # with a label breakdown are the Kibana-native first-row presentation.
    if breakdown_col:
        out["breakdown"] = {"field": breakdown_col, "columns": 1}
        # Narrow dashboard slots (common for summary rows) overflow with the
        # default metric density when several tiles share one panel. Stack in
        # one column (columns: 1) and use compact density so labels stay
        # readable in w≈6 first-row panels.
        out["styling"] = {"density": "compact"}
        # Empty primary label: otherwise Lens shows the measure field name
        # (e.g. "gauge_value" → "gaug...") under every breakdown tile.
        out["primary"]["label"] = ""
    return out


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
    for line in reversed(str(esql_query or "").splitlines()):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        body = stripped[1:].strip()
        if body.lower().startswith("keep "):
            return [part.strip() for part in _split_top_level_csv(body[5:].strip()) if part.strip()]
    return []


def _native_promql_command_value_expr(query):
    text = str(query or "").strip()
    if not text.upper().startswith("PROMQL "):
        return ""
    start = text.find("value=(")
    if start < 0:
        return ""
    pos = start + len("value=(")
    depth = 1
    quote = None
    escaped = False
    pieces = []
    while pos < len(text):
        char = text[pos]
        if quote:
            pieces.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            pos += 1
            continue
        if char in {'"', "'"}:
            quote = char
            pieces.append(char)
        elif char == "(":
            depth += 1
            pieces.append(char)
        elif char == ")":
            depth -= 1
            if depth == 0:
                return "".join(pieces).strip()
            pieces.append(char)
        else:
            pieces.append(char)
        pos += 1
    return ""


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
    if str(esql_query or "").lstrip().upper().startswith("PROMQL "):
        promql_expr = _native_promql_command_value_expr(esql_query)
        _metric_col, native_cols = _native_promql_result_shape(promql_expr)
        columns.update(col for col in native_cols if col != "_timeseries")
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
            # Quote the column reference: a Prometheus label can be a reserved
            # ES|QL keyword (e.g. ``in``) or carry dots (``prometheus.labels.x``),
            # both of which ES|QL rejects as a bare ``TO_STRING(...)`` argument.
            concat_args.append(f'COALESCE(TO_STRING({_esql_identifier(column)}), "")')
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


def _warn_extra_breakdown_dimensions(
    by_cols,
    dimension_field,
    breakdown_field,
    warnings,
    represented_breakdown_fields=None,
):
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
        if col != dimension_field
        and col != breakdown_field
        and col not in set(represented_breakdown_fields or [])
    ]
    if extra:
        _append_unique(
            warnings,
            "XY chart shows a single breakdown; additional grouping "
            f"dimension(s) {extra} are in the query but not on the chart, "
            "so series differing only by those are visually merged",
        )


def _apply_composite_group_breakdown_to_xy_panel(panel, *, group_cols, warnings=None):
    """Composite multiple non-time ``BY`` columns into one XY breakdown field.

    Lens XY supports a single ``breakdown.field``. When the query groups by two
    or more categorical dimensions (and no multi-placeholder legend template
    already produced ``legend``), synthesize ``series_group = CONCAT(...)`` so
    each distinct label tuple remains a separate series instead of silently
    merging on the first dimension alone.
    """
    if not isinstance(panel, dict):
        return panel
    if (panel.get("breakdown") or {}).get("field") == "legend":
        return panel
    dims = [str(col) for col in (group_cols or []) if col]
    if len(dims) < 2:
        return panel
    query = str(panel.get("query") or "")
    if not query.strip():
        return panel

    concat_args = []
    for index, dim in enumerate(dims):
        if index:
            concat_args.append('" / "')
        concat_args.append(f'COALESCE(TO_STRING({_esql_identifier(dim)}), "")')
    eval_line = f"| EVAL series_group = CONCAT({', '.join(concat_args)})"
    # Reuse the legend splice helper, then rename the KEEP column it appends
    # (``legend``) to ``series_group`` so the breakdown field matches.
    spliced = _splice_composite_legend_into_query(
        query,
        eval_line=eval_line,
        label_columns=set(dims),
    )
    # The helper may append ``legend`` to a KEEP list even though EVAL already
    # names the column ``series_group``. Rewrite that KEEP token only.
    stages = _split_esql_pipeline(spliced)
    for idx in range(len(stages) - 1, -1, -1):
        stage = stages[idx].strip()
        if stage.lower().startswith("keep "):
            keep_body = stage[5:].strip()
            parts = [part.strip() for part in _split_top_level_csv(keep_body) if part.strip()]
            rewritten = [("series_group" if part == "legend" else part) for part in parts]
            if "series_group" not in rewritten:
                rewritten.append("series_group")
            stages[idx] = f"KEEP {', '.join(rewritten)}"
            break
    if "\n" in spliced:
        # Rebuild from multiline form: replace KEEP line content carefully.
        lines = spliced.splitlines()
        for idx in range(len(lines) - 1, -1, -1):
            stripped = lines[idx].strip()
            if stripped.startswith("|") and stripped[1:].strip().lower().startswith("keep "):
                keep_body = stripped[1:].strip()[5:].strip()
                parts = [part.strip() for part in _split_top_level_csv(keep_body) if part.strip()]
                rewritten = [("series_group" if part == "legend" else part) for part in parts]
                if "series_group" not in rewritten:
                    rewritten.append("series_group")
                lines[idx] = f"| KEEP {', '.join(rewritten)}"
                panel["query"] = "\n".join(lines)
                break
        else:
            panel["query"] = spliced
    else:
        head = stages[0]
        tail = " | ".join(stages[1:]) if len(stages) > 1 else ""
        panel["query"] = f"{head} | {tail}" if tail else head
    panel["breakdown"] = {"field": "series_group"}
    _append_unique(
        warnings if warnings is not None else [],
        "Composited multi-label grouping "
        f"({', '.join(dims)}) into a single XY breakdown column",
    )
    return panel


def _job_scope_extra_group_fields(
    query: str,
    *,
    non_time_groups: list[str] | None,
    breakdown_field: str | None,
) -> list[str]:
    """Return extra grouping fields that can be treated as dashboard scope.

    The common Prometheus ``by (instance, job)`` shape on operational
    dashboards usually uses ``job`` as a top-level control / scoping dimension
    while the chart itself is intended to distinguish hosts. When the emitted
    query is explicitly filtered by ``?job``, keeping ``instance`` as the
    visible breakdown is a better Kibana fit than synthesizing a composite
    ``instance / job`` legend for every panel.
    """
    groups = [str(col) for col in (non_time_groups or []) if col]
    if len(groups) != 2 or not breakdown_field or "?job" not in str(query or ""):
        return []
    instance_fields = {"instance", "labels.instance", "service.instance.id", "host.name"}
    job_fields = {"job", "labels.job", "service.name"}
    if breakdown_field not in instance_fields:
        return []
    extras = [col for col in groups if col != breakdown_field]
    if len(extras) != 1:
        return []
    return extras if extras[0] in job_fields else []


def _build_esql_xy_panel(esql, chart_type, metric_col=None, by_cols=None,
                         time_fields=None, mode=None,
                         legend_format_template=None, legend_labels=None,
                         warnings=None):
    esql = _ensure_bucket_sort(esql)
    shape = _extract_esql_shape(esql)
    extracted_metric_col, extracted_by_cols = _extract_esql_columns(esql)
    if metric_col is None:
        metric_col = extracted_metric_col
    # Recover group columns from the query when the caller passes nothing OR an
    # empty list. A multi-target translation (eg. node-exporter-full "CPU", a
    # group_left target) can hand us empty group_fields even though the combined
    # ES|QL groups BY time_bucket; trusting that empty list would wrongly degrade
    # a time-series graph to a single-value metric. The query is the source of
    # truth — if it genuinely has no dimension, extraction is empty too.
    if not by_cols:
        by_cols = extracted_by_cols
    if time_fields is None:
        time_fields = shape.time_fields
    projected_fields = set(shape.projected_fields or [])
    if "series_group" in projected_fields and "value" in projected_fields:
        metric_col = "value"
        merged_by_cols = list(by_cols or [])
        if not merged_by_cols:
            merged_by_cols.extend(extracted_by_cols or [])
        if "series_group" not in merged_by_cols:
            merged_by_cols.append("series_group")
        by_cols = merged_by_cols
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
    non_time_groups = [
        col
        for col in (by_cols or [])
        if col and col != dimension_field and not _is_time_like_output_field(col)
    ]
    scope_only_groups = _job_scope_extra_group_fields(
        esql,
        non_time_groups=non_time_groups,
        breakdown_field=breakdown_field,
    )
    if (
        (panel.get("breakdown") or {}).get("field") != "legend"
        and len(non_time_groups) >= 2
        and not scope_only_groups
    ):
        _apply_composite_group_breakdown_to_xy_panel(
            panel,
            group_cols=non_time_groups,
            warnings=warnings,
        )
    represented = []
    breakdown_name = (panel.get("breakdown") or {}).get("field")
    if breakdown_name == "legend":
        represented = list(legend_labels or [])
    elif breakdown_name == "series_group":
        represented = list(non_time_groups)
    elif scope_only_groups:
        represented = list(scope_only_groups)
    _warn_extra_breakdown_dimensions(
        by_cols,
        dimension_field,
        breakdown_name,
        warnings,
        represented_breakdown_fields=represented,
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
    projected_fields = set(shape.projected_fields or [])
    if "series_group" in projected_fields and "value" in projected_fields:
        merged_by_cols = list(by_cols or [])
        if not merged_by_cols:
            merged_by_cols.extend(extracted_by_cols or [])
        if "series_group" not in merged_by_cols:
            merged_by_cols.append("series_group")
        return _build_esql_xy_panel(
            esql,
            chart_type,
            metric_col="value",
            by_cols=merged_by_cols,
            time_fields=time_fields if time_fields is not None else shape.time_fields,
            mode=mode,
            legend_format_template=legend_format_template,
            legend_labels=legend_labels,
            warnings=warnings,
        )
    # Recover group columns from the query on an empty/None caller value (see
    # _build_esql_xy_panel) so a grouped multi-series query is not mistaken for a
    # dimensionless one and degraded to a summary table.
    if not by_cols:
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
    non_time_groups = [
        col
        for col in (by_cols or [])
        if col and col != dimension_field and not _is_time_like_output_field(col)
    ]
    scope_only_groups = _job_scope_extra_group_fields(
        esql,
        non_time_groups=non_time_groups,
        breakdown_field=breakdown_field,
    )
    if (
        (panel.get("breakdown") or {}).get("field") != "legend"
        and len(non_time_groups) >= 2
        and not scope_only_groups
    ):
        _apply_composite_group_breakdown_to_xy_panel(
            panel,
            group_cols=non_time_groups,
            warnings=warnings,
        )
    represented = []
    breakdown_name = (panel.get("breakdown") or {}).get("field")
    if breakdown_name == "legend":
        represented = list(legend_labels or [])
    elif breakdown_name == "series_group":
        represented = list(non_time_groups)
    elif scope_only_groups:
        represented = list(scope_only_groups)
    _warn_extra_breakdown_dimensions(
        by_cols,
        dimension_field,
        breakdown_name,
        warnings,
        represented_breakdown_fields=represented,
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
    if isinstance(overrides, list) and overrides:
        right_format = _grafana_yaxis_metric_format(grafana_panel, "right")
        for override in overrides:
            if not isinstance(override, dict):
                continue
            alias = str(override.get("alias") or "").strip()
            axis = _grafana_override_axis(override.get("yaxis"))
            stack_override = override.get("stack")
            if axis != "right" and stack_override is not False:
                continue
            matched = False
            for metric in metrics:
                if not isinstance(metric, dict):
                    continue
                candidates = {
                    str(metric.get("field") or ""),
                    str(metric.get("label") or ""),
                }
                if not _series_override_alias_matches(alias, candidates):
                    continue
                matched = True
                if axis == "right":
                    metric["axis"] = "right"
                    if right_format:
                        metric["format"] = dict(right_format)
                    elif _grafana_right_axis_present(grafana_panel):
                        # Grafana ``yaxes[1].format: none`` is an explicit
                        # "no unit" on the overlay axis. Do not keep the
                        # left-axis format (Load 1m inheriting CPU %).
                        metric.pop("format", None)
                if stack_override is False:
                    metric["stack"] = False
            if alias and not matched:
                _append_unique(
                    warnings,
                    f'Dropped Grafana secondary y-axis assignment for unmatched series override "{alias}"',
                )
    _apply_field_override_metric_overrides(metrics, grafana_panel)


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


def _grafana_right_axis_present(grafana_panel: dict) -> bool:
    yaxes = grafana_panel.get("yaxes")
    return isinstance(yaxes, list) and len(yaxes) > 1 and isinstance(yaxes[1], dict)


def _field_override_targets_metric(
    override: dict[str, Any], metric: dict[str, Any]
) -> bool:
    matcher = override.get("matcher")
    if not isinstance(matcher, dict):
        return False
    matcher_id = str(matcher.get("id") or "").strip()
    options = matcher.get("options")
    candidates = {
        str(metric.get("field") or ""),
        str(metric.get("label") or ""),
    }
    candidates = {candidate for candidate in candidates if candidate}
    if matcher_id == "byName":
        needle = str(options or "").strip()
        return bool(needle) and needle in candidates
    if matcher_id == "byRegexp":
        return _grafana_override_regex_matches(options, candidates)
    return False


def _field_override_marks_metric_unstacked(override: dict[str, Any]) -> bool:
    properties = override.get("properties")
    if not isinstance(properties, list):
        return False
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        if str(prop.get("id") or "").strip() != "custom.stacking":
            continue
        value = prop.get("value")
        if not isinstance(value, dict):
            continue
        if str(value.get("mode") or "").strip() == "normal" and value.get("group") is False:
            return True
    return False


def _apply_field_override_metric_overrides(
    metrics: list[dict[str, Any]], grafana_panel: dict[str, Any]
) -> None:
    field_config = grafana_panel.get("fieldConfig")
    overrides = field_config.get("overrides") if isinstance(field_config, dict) else None
    if not isinstance(overrides, list) or not overrides:
        return
    for override in overrides:
        if not isinstance(override, dict) or not _field_override_marks_metric_unstacked(override):
            continue
        for metric in metrics:
            if isinstance(metric, dict) and _field_override_targets_metric(override, metric):
                metric["stack"] = False


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


def _bargauge_bullet_shape(panel):
    """Map a Grafana bargauge's orientation to a Kibana gauge bullet shape.

    A bargauge renders a value as a bar against a scale; the faithful Kibana
    gauge shape is a bullet. Grafana's ``orientation`` is ``horizontal`` (the
    common default) or ``vertical``; anything else falls back to horizontal.
    """
    orientation = str(((panel or {}).get("options") or {}).get("orientation", "")).lower()
    return "vertical_bullet" if orientation == "vertical" else "horizontal_bullet"


def _build_esql_gauge_panel(esql, metric_col=None, panel=None, shape=None):
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
    if panel or shape:
        gauge["appearance"] = {"shape": shape or "arc"}
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
    else:
        # A dimensionless summary-collapsed table (e.g. an instant/alerts query
        # reduced to a single row) still keeps its time column in the query
        # output (``| KEEP time_bucket, <metric>``) but drops it from
        # group_fields. Without a breakdown the datatable shows only the metric
        # and hides *when* the value is from, so surface the leftover time
        # column as a date row. Grouped tables keep their real breakdowns above
        # and intentionally omit the collapsed time to avoid a redundant column.
        metric_names = {m for m in metric_fields if m}
        shape = _extract_esql_shape(esql)
        time_cols = [
            c for c in shape.projected_fields
            if _is_time_like_output_field(c) and c not in metric_names
        ]
        if time_cols:
            panel["breakdowns"] = [{"field": time_cols[0], "data_type": "date"}]
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


def _extract_variable_source_metric(query_text):
    """Metric a ``label_values(metric, label)`` control is scoped to (issue #152).

    Grafana's two-argument ``label_values(metric, label)`` lists only label
    values that occur on ``metric``, so the migrated control must preserve that
    scope. The single-argument form ``label_values(label)`` and selector-only
    forms without a leading metric name (``label_values({job="api"}, label)``)
    have no scoping metric and return "".
    """
    query_text = (query_text or "").strip()
    match = re.match(r"^label_values\((?P<body>.+)\)$", query_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    parts = _split_top_level_csv(match.group("body"))
    if len(parts) < 2:
        return ""
    metric_match = re.match(r"\s*([a-zA-Z_:][a-zA-Z0-9_:]*)", parts[0])
    return metric_match.group(1) if metric_match else ""


def _extract_variable_scope_template_refs(query_text):
    """Other template variables that scope a ``label_values()`` control (#269).

    Grafana supports *chained* query variables, e.g.
    ``label_values(container_memory_cache{instance="$instance"}, id)``: the
    ``$id`` control's option list is meant to be scoped to whichever
    ``$instance`` is currently selected, not every ``id`` in the index.

    When the target can bind named ES|QL params inside another control's
    populate-query, callers may translate these references back into ``?var``
    predicates on the values-query control. Otherwise callers use this to
    detect the shape and attach an explicit degradation warning instead of
    silently listing every value — the control still works, it is just broader
    than the Grafana source.
    """
    query_text = (query_text or "").strip()
    match = re.match(r"^label_values\((?P<body>.+)\)$", query_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    parts = _split_top_level_csv(match.group("body"))
    if len(parts) < 2:
        return []
    selector_match = re.search(r"\{(?P<selector>.*)\}", parts[0], re.DOTALL)
    if not selector_match:
        return []
    refs: list[str] = []
    for ref_match in _VARIABLE_REFERENCE_RE.finditer(selector_match.group("selector")):
        name = ref_match.group(1) or ref_match.group(2)
        if name and name not in refs:
            refs.append(name)
    return refs


def _extract_variable_scope_param_filters(
    query_text,
    variables_by_name=None,
    resolver=None,
    rule_pack=None,
    *,
    control_warnings=None,
    variable_name="",
):
    """Template-variable label matchers that can survive as ES|QL params.

    Conservative by design: only single-reference, non-multi upstream
    variables are converted into chained control-query params here.
    """
    query_text = (query_text or "").strip()
    match = re.match(r"^label_values\((?P<body>.+)\)$", query_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return [], []
    parts = _split_top_level_csv(match.group("body"))
    if len(parts) < 2:
        return [], []
    selector_match = re.search(r"\{(?P<selector>.*)\}", parts[0], re.DOTALL)
    if not selector_match:
        return [], []
    filters: list[str] = []
    applied_refs: list[str] = []
    variables_by_name = variables_by_name or {}
    for matcher_text in _split_top_level_csv(selector_match.group("selector")):
        matcher = _PROMQL_LABEL_MATCHER_VAR_RE.match(matcher_text)
        if not matcher:
            continue
        value = matcher.group("value")
        refs: list[str] = []
        for ref_match in _VARIABLE_REFERENCE_RE.finditer(value):
            ref = ref_match.group(1) or ref_match.group(2)
            if ref and ref not in refs:
                refs.append(ref)
        if not refs:
            continue
        if len(refs) != 1:
            continue
        ref = refs[0]
        upstream = variables_by_name.get(ref) or {}
        if bool(upstream.get("multi")):
            if control_warnings is not None:
                _append_unique(
                    control_warnings,
                    f"variable '{variable_name}' is scoped by multi-select ${ref} in "
                    "Grafana; the migrated control kept a broader option list "
                    "because multi-value chained ES|QL control-query params are "
                    "not translated yet",
                )
            continue
        raw_label = matcher.group("label")
        if raw_label == "__name__":
            continue
        if resolver is not None:
            field = resolver.resolve_control_field(raw_label) or raw_label
            field_exists = getattr(resolver, "field_exists", None)
            if field_exists is not None and field_exists(field) is False:
                continue
        elif rule_pack is not None:
            field = rule_pack.control_field_overrides.get(raw_label, raw_label)
        else:
            field = raw_label
        column = _esql_identifier(field)
        param = f"?{ref}"
        op = matcher.group("op")
        if op in ("=", "=~"):
            filters.append(
                f"({param} == \"\" OR ({column} RLIKE {param} OR ({column} IS NULL AND \"\" RLIKE {param})))"
            )
            applied_refs.append(ref)
        elif op in ("!=", "!~"):
            filters.append(f"({param} == \"\" OR NOT ({column} RLIKE {param}))")
            applied_refs.append(ref)
    return filters, applied_refs


def _extract_variable_scope_filters(
    query_text,
    resolver=None,
    rule_pack=None,
    *,
    control_warnings=None,
    variable_name="",
):
    """Literal label matchers that scope a ``label_values()`` control (#312).

    Grafana's ``label_values(metric{device!="nbd1"}, device)`` restricts the
    listed values to series where ``device != "nbd1"``. Kibana's ES|QL
    ``VALUES_FROM_QUERY`` control can express that restriction directly as an
    extra ``WHERE`` predicate, so the migrated dropdown does not offer values
    (``nbd1``) the Grafana source excluded.

    Only *literal* matchers are returned as ``(field, esql_predicate)`` pairs:

    * ``label="v"`` / ``label!="v"`` become ``field == "v"`` / ``field != "v"``.
    * ``label=~"re"`` / ``label!~"re"`` become ``field RLIKE "re"`` /
      ``NOT field RLIKE "re"``.

    Matchers whose value references another template variable
    (``instance="$instance"``) are skipped here: they are chained scopes handled
    by :func:`_extract_variable_scope_template_refs` (Kibana controls cannot
    express that inter-control dependency, so those emit a degradation warning
    instead of a wrong literal predicate).
    """
    query_text = (query_text or "").strip()
    match = re.match(r"^label_values\((?P<body>.+)\)$", query_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    parts = _split_top_level_csv(match.group("body"))
    if len(parts) < 2:
        return []
    selector_match = re.search(r"\{(?P<selector>.*)\}", parts[0], re.DOTALL)
    if not selector_match:
        return []
    filters: list[str] = []
    for matcher_text in _split_top_level_csv(selector_match.group("selector")):
        matcher = _PROMQL_LABEL_MATCHER_VAR_RE.match(matcher_text)
        if not matcher:
            continue
        value = matcher.group("value")
        if _VARIABLE_REFERENCE_RE.search(value):
            # Chained scope on another variable -- handled separately (#269).
            continue
        raw_label = matcher.group("label")
        if raw_label == "__name__":
            if control_warnings is not None:
                _append_unique(
                    control_warnings,
                    f"variable '{variable_name}' has a label_values() selector filter on "
                    "__name__ (Prometheus metric-name matcher); Kibana ES|QL controls "
                    "cannot express metric-name scoped option lists, so that filter "
                    "was not applied to the control query",
                )
            continue
        if resolver is not None:
            field = resolver.resolve_control_field(raw_label) or raw_label
            field_exists = getattr(resolver, "field_exists", None)
            if field_exists is not None and field_exists(field) is False:
                if control_warnings is not None:
                    _append_unique(
                        control_warnings,
                        f"variable '{variable_name}' has a label_values() selector filter "
                        f"on '{raw_label}', but resolved field '{field}' is not present "
                        "on the target; that filter was not applied to the control query",
                    )
                continue
        elif rule_pack is not None:
            field = rule_pack.control_field_overrides.get(raw_label, raw_label)
        else:
            field = raw_label
        column = _esql_identifier(field)
        literal = _esql_string_literal(value)
        op = matcher.group("op")
        if op == "=":
            filters.append(f"{column} == {literal}")
        elif op == "!=":
            filters.append(f"{column} != {literal}")
        elif op == "=~":
            filters.append(f"{column} RLIKE {literal}")
        elif op == "!~":
            filters.append(f"NOT {column} RLIKE {literal}")
    return filters


def _esql_string_literal(value):
    """Quote a string value as an ES|QL double-quoted literal."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _resolve_control_scope_metric(metric_name, resolver, rule_pack):
    """Resolve a control's scoping metric to its physical target field.

    Returns "" when there is no scoping metric, or when the resolver positively
    knows the resolved field is absent from the target (scoping the control on a
    missing field would empty it). Stays scoped when the target is silent
    (offline migrate, or live caps unavailable) so the control mirrors the
    source-faithful ``label_values(metric, label)`` scope.
    """
    if not metric_name:
        return ""
    field = metric_name
    if resolver is not None:
        resolve = getattr(resolver, "resolve_metric_field", None)
        if resolve is not None:
            resolved = resolve(metric_name)
            if resolved:
                field = resolved
        field_exists = getattr(resolver, "field_exists", None)
        if field_exists is not None and field_exists(field) is False:
            return ""
    elif rule_pack is not None:
        field = rule_pack.control_field_overrides.get(metric_name, metric_name)
    return field


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


def _esql_values_control_query(
    field_name, data_view, metric_field=None, *, include_match_all=False, extra_filters=None
):
    """Build an ES|QL query that enumerates a control's selectable values.

    Mirrors Grafana's ``label_values()`` query variable: return the field's
    distinct values, sorted, so the Kibana control can populate its dropdown
    at render time.

    When the variable was defined as ``label_values(metric, label)`` the control
    is scoped to ``metric`` in Grafana, so ``metric_field`` (the resolved
    presence field) is added as ``WHERE <metric_field> IS NOT NULL`` to list only
    values coming from that metric instead of every value of the field in the
    index (issue #152).

    ``extra_filters`` carries literal label-matcher predicates from the source
    selector (``label_values(metric{device!="nbd1"}, device)`` -> ``device !=
    "nbd1"``) so the migrated dropdown excludes the values Grafana excluded
    instead of listing every value (issue #312).

    When ``include_match_all`` is true (includeAll / All default), prepend the
    regex match-all token ``.*`` via ``MV_APPEND`` so Kibana's selected default
    is a valid options-list value instead of an incompatible selection.
    """
    field = _esql_identifier(field_name)
    index = data_view or "metrics-*"
    clauses = []
    if metric_field:
        clauses.append(f"{_esql_identifier(metric_field)} IS NOT NULL")
    clauses.append(f"{field} IS NOT NULL")
    clauses.extend(extra_filters or [])
    where = "WHERE " + " AND ".join(clauses)
    if include_match_all:
        return (
            f"FROM {index} | {where}"
            f" | STATS count = COUNT(*) BY {field}"
            f' | EVAL options = MV_APPEND(".*", {field})'
            f" | MV_EXPAND options"
            f" | STATS count = COUNT(*) BY options"
            f" | KEEP options"
            f" | RENAME options AS {field}"
            f" | SORT {field} ASC | LIMIT 1000"
        )
    return (
        f"FROM {index} | {where}"
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
    """Names of template variables that need regex PromQL/ES|QL matchers.

    Includes:
    * variables whose binding control defaults to the regex match-all (".*")
      so ``label="$var"`` does not compare against the literal string ".*"
      (PR #133 review / issue #131)
    * multi-select variables — Grafana rewrites ``label="$var"`` to a regex
      matcher for multi/All, and native PROMQL must emit ``label=~?var``
      (issues #64 / #319)
    """
    names = set()
    for var in variables:
        if not isinstance(var, dict):
            continue
        name = var.get("name")
        if not name:
            continue
        if _variable_default_selection(var) == _MATCH_ALL_SELECTION or bool(var.get("multi")):
            names.add(name)
    return names


def _collect_multi_select_param_names(variables):
    """Names of Grafana ``multi: true`` template variables.

    These bind through ``MV_CONTAINS(?var, field)`` rather than ``RLIKE ?var``:
    a scalar parameter position can only ever hold one value, so RLIKE forces
    the control to single-select. ``MV_CONTAINS`` is Kibana's supported
    multi-value mechanism and pairs with ``single_select: false``.
    """
    names = set()
    for var in variables:
        if not isinstance(var, dict):
            continue
        name = var.get("name")
        if name and bool(var.get("multi")):
            names.add(name)
    return names


_REGEX_META_RE = re.compile(r"[.\\^$*+?{}[\]|()]")


def _variable_multi_select_has_regex_risk(variable) -> bool:
    """True when a multi-select variable can still carry a regex selection.

    Concrete ``label_values()`` options matched via ``MV_CONTAINS`` are
    equivalent to Grafana's ``a|b|c`` literal alternation. Warn only when the
    variable can still inject a real regex (custom type, ``regex`` filter, or a
    non-trivial ``allValue``).
    """
    if not isinstance(variable, dict):
        return False
    if str(variable.get("type") or "").strip().lower() == "custom":
        return True
    if str(variable.get("regex") or "").strip():
        return True
    all_value = variable.get("allValue")
    if isinstance(all_value, str):
        stripped = all_value.strip()
        if stripped and stripped not in {".*", ".+", ".+?"} and _REGEX_META_RE.search(stripped):
            return True
    return False


def _build_esql_param_control(
    variable_name,
    label,
    field_name,
    data_view,
    default=None,
    metric_field=None,
    source_field="",
    include_internal_metadata=False,
    extra_filters=None,
    multiple=False,
):
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
    references). ``multiple`` follows how the panel filters bind the parameter:
    scalar positions (``== ?var`` / ``RLIKE ?var``) require single-select, while
    a Grafana multi-select variable binds via ``MV_CONTAINS(?var, field)`` and
    can therefore stay multi-select.

    A ``default`` selection is emitted so the parameter is bound on first load
    instead of leaving the control empty (issue #131).
    """
    include_match_all = default == _MATCH_ALL_SELECTION
    control = {
        "type": "esql",
        "label": label,
        "variable_name": variable_name,
        "variable_type": "values",
        "query": _esql_values_control_query(
            field_name,
            data_view,
            metric_field=metric_field,
            include_match_all=include_match_all,
            extra_filters=extra_filters,
        ),
        # Multi-select only when the panel filters bind via MV_CONTAINS; a
        # scalar RLIKE position cannot accept more than one value.
        "multiple": bool(multiple),
    }
    if multiple:
        # Kibana's own helper: canBeMultiValue -> MULTI_VALUES, else VALUES.
        control["variable_type"] = "multi_values"
    if include_internal_metadata:
        control[_CONTROL_RESOLVED_FIELD_NAME] = field_name
        if source_field:
            control[_CONTROL_SOURCE_FIELD_NAME] = source_field
    if default not in (None, ""):
        # ESQLQueryMultiSelectControl types ``default`` as an array of strings;
        # the single-select variant types it as a scalar. Emitting the wrong
        # shape fails Kibana YAML schema validation.
        control["default"] = [default] if multiple else default
    return control


def _build_static_esql_param_control(variable):
    """Bind a Grafana custom variable from its literal option list.

    Custom variables are values, not target fields.  Querying a field inferred
    from the variable name (for example ``diskdevices``) is wrong when the
    variable is used as a regex for another label (for example ``device``).
    A static ES|QL values control preserves the source literals and guarantees
    that the selected default is one of the options Kibana can bind.
    """
    name = str(variable.get("name") or "")
    default = _variable_default_selection(variable)
    choices = _grouping_candidate_label_names(variable)
    if default and default not in choices:
        choices.insert(0, default)
    return {
        "type": "esql",
        "label": variable.get("label") or name,
        "variable_name": name,
        "variable_type": "values",
        "choices": choices,
        "multiple": False,
        "default": default,
    }


def _grouping_candidate_label_names(variable):
    """Collect the label names a grouping template variable can select.

    Grafana ``by ($grouping)`` selectors are ``custom`` variables (a fixed
    comma-separated list of dimension names) or ``query`` variables whose
    fetched ``options`` are dimension names. The current selection is always
    included so the migrated control has a concrete default. Nested template
    references and the "All" sentinel are skipped (issue #282).
    """
    names: list[str] = []

    def _add(value):
        if isinstance(value, (list, tuple)):
            for item in value:
                _add(item)
            return
        text = str(value or "").strip()
        if not text or text == "$__all" or text.lower() == "all":
            return
        if re.search(r"\$|\[\[", text):
            return
        if text not in names:
            names.append(text)

    for option in variable.get("options") or []:
        if isinstance(option, dict):
            _add(option.get("value"))
    if variable.get("type") == "custom":
        for part in _split_top_level_csv(_variable_query_text(variable)):
            _add(part.strip())
    current = variable.get("current")
    if isinstance(current, dict):
        _add(current.get("value"))
    return names


def _resolve_group_choice_field(
    raw_name, resolver, rule_pack, metric_field=None, *, allow_missing=False
):
    """Resolve one grouping-variable option to an aggregatable target field.

    When live schema discovery remaps a bare Grafana option (``exporter``) to a
    profile path that is not present yet (``labels.exporter``), treating that as
    "unresolvable" empties the late-bound choice set and wrongly marks
    ``by ($grouping)`` ``not_feasible``. Custom option lists are intentional
    field names — absence is data readiness. Pass ``allow_missing=True`` for
    those so the control still emits; the render audit / seeder can populate
    the dimensions later.
    """
    name = str(raw_name or "").strip()
    if not name:
        return None
    if resolver:
        field_name = resolver.resolve_control_field(name, metric_field=metric_field or None)
        if not field_name:
            field_name = name
        exists = resolver.field_exists(field_name)
        if exists is False:
            raw_exists = resolver.field_exists(name) if field_name != name else False
            if raw_exists is True:
                field_name = name
                exists = True
            elif allow_missing:
                # Prefer the Grafana option text: control choices and seeder
                # contracts key off the dashboard's declared names.
                field_name = name
                exists = None
            else:
                return None
        if exists is True and not resolver.is_aggregatable_field(field_name):
            return None
        return field_name
    return (rule_pack or RulePackConfig()).control_field_overrides.get(name, name)


def _build_late_bound_group_var_choices(variables, resolver, rule_pack):
    """Map grouping template variables to ES|QL field-control specs (issue #282).

    Only built when the target binds ES|QL parameters; the translation guardrail
    consults this map to decide whether a ``by ($var)`` grouping can be deferred
    to a Kibana ES|QL identifier control (``STATS ... BY ??var``) instead of
    failing. A variable with no resolvable field options is omitted so the
    grouping degrades gracefully to not_feasible.
    """
    if not binds_esql_named_params(rule_pack):
        return {}
    choices_map: dict[str, dict] = {}
    for variable in variables or []:
        if not isinstance(variable, dict) or variable.get("type") not in ("query", "custom"):
            continue
        name = variable.get("name")
        if not name:
            continue
        raw_names = _grouping_candidate_label_names(variable)
        if not raw_names:
            continue
        query_text = _variable_query_text(variable)
        metric_field = _resolve_control_scope_metric(
            _extract_variable_source_metric(query_text), resolver, rule_pack
        )
        # Custom variables declare an explicit option list of field names.
        # Missing target fields are data readiness, not an empty choice set.
        allow_missing = variable.get("type") == "custom"
        choices: list[str] = []
        for raw in raw_names:
            field_name = _resolve_group_choice_field(
                raw,
                resolver,
                rule_pack,
                metric_field,
                allow_missing=allow_missing,
            )
            if field_name and field_name not in choices:
                choices.append(field_name)
        if not choices:
            continue
        current = variable.get("current")
        default_raw = current.get("value") if isinstance(current, dict) else None
        if isinstance(default_raw, (list, tuple)):
            default_raw = default_raw[0] if len(default_raw) == 1 else None
        default = None
        if default_raw not in (None, "", "$__all"):
            default = _resolve_group_choice_field(
                default_raw,
                resolver,
                rule_pack,
                metric_field,
                allow_missing=allow_missing,
            )
        if default not in choices:
            default = choices[0]
        choices_map[name] = {
            "choices": choices,
            "default": default,
            "label": variable.get("label") or name,
        }
    return choices_map


def _build_esql_field_control(variable_name, spec):
    """Build a Kibana ES|QL identifier/field control (``??var``) (issue #282).

    The Grafana grouping variable becomes a ``variable_type: fields`` ES|QL
    control whose ``choices`` are the resolved candidate dimensions; ``??var``
    in ``STATS ... BY ??var`` binds to the field the viewer picks, reproducing
    the source dashboard's late-bound grouping dropdown.
    """
    control = {
        "type": "esql",
        "label": spec.get("label") or variable_name,
        "variable_name": variable_name,
        "variable_type": "fields",
        "choices": list(spec.get("choices") or []),
    }
    default = spec.get("default")
    if default:
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
            verify=getattr(resolver, "_verify", True),
            field_profile=getattr(resolver, "_field_profile", "otel"),
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
        context.control_warnings.append(
            f"variable '{name}' uses Grafana query_result(), which has no "
            "equivalent Kibana control population query; no Kibana control was emitted"
        )
        return f"skipped query_result helper variable {name}"
    source_field = _extract_variable_source_field(query_text) or name
    context.source_field = source_field

    # Resolve the control's scoping metric up front so the label field is picked
    # by co-occurrence with that metric, not by index-global existence (#163).
    source_metric = _extract_variable_source_metric(query_text)
    metric_field = _resolve_control_scope_metric(source_metric, resolver, context.rule_pack)

    if resolver:
        field_name = resolver.resolve_control_field(source_field, metric_field=metric_field or None)
    else:
        rule_pack = context.rule_pack or RulePackConfig()
        field_name = rule_pack.control_field_overrides.get(source_field, source_field)
    if field_name is None:
        context.control_warnings.append(
            f"variable '{name}' could not resolve source field '{source_field}' "
            "to a supported target field; no Kibana control was emitted"
        )
        return f"skipped unsupported control {name}"
    if resolver and resolver.field_exists(field_name) is False:
        # Issue #269 Defect 1: keep the source control even when live schema
        # discovery cannot find its field yet. Dropping it makes offline/live
        # output structurally inconsistent, and `_ensure_param_controls`
        # otherwise has to synthesize it again when a panel binds `?name`,
        # contradicting a "dropped" warning. The kept control starts empty and
        # self-heals once telemetry containing the field is ingested.
        context.control_warnings.append(
            f"variable '{name}' kept, but resolved field '{field_name}' is not "
            "present on the target (data not yet ingested, or genuinely "
            "absent); the control may have no options until the field is ingested"
        )
    if resolver and resolver.field_exists(field_name) is True:
        if resolver.has_conflicting_types(field_name) and _field_has_ts_metadata_conflict(field_name, resolver):
            context.control_warnings.append(
                f"variable '{name}' resolved to '{field_name}', but that field "
                "has incompatible target types; no Kibana control was emitted"
            )
            return f"skipped conflicting control field {field_name}"
        if not resolver.is_aggregatable_field(field_name):
            context.control_warnings.append(
                f"variable '{name}' resolved to non-aggregatable target field "
                f"'{field_name}'; no Kibana control was emitted"
            )
            return f"skipped non-aggregatable control field {field_name}"
    scope_refs = [
        ref for ref in _extract_variable_scope_template_refs(query_text) if ref != name
    ]
    if binds_esql_named_params(context.rule_pack):
        # The target binds Grafana template variables as native ES|QL
        # parameters (``?<name>``), so the control must DEFINE that ES|QL
        # variable rather than emit a generic data-view filter; otherwise the
        # panel queries fail with "Unknown query parameter [name]" (issue #107).
        # This must mirror the ES|QL matcher gate in ``_matcher_to_esql`` so a
        # cluster-wide ES|QL fallback run that preserves ``?var`` also emits the
        # binding control rather than a duplicate generic one (issue #132).
        scope_filters = _extract_variable_scope_filters(
            query_text,
            resolver,
            context.rule_pack,
            control_warnings=context.control_warnings,
            variable_name=name,
        )
        scope_param_filters, applied_scope_refs = _extract_variable_scope_param_filters(
            query_text,
            context.variables_by_name,
            resolver,
            context.rule_pack,
            control_warnings=context.control_warnings,
            variable_name=name,
        )
        scope_filters.extend(scope_param_filters)
        unhandled_scope_refs = [ref for ref in scope_refs if ref not in applied_scope_refs]
        if unhandled_scope_refs:
            context.control_warnings.append(
                f"variable '{name}' is scoped by {', '.join(f'${ref}' for ref in unhandled_scope_refs)} in "
                "Grafana (label_values() selector); the migrated control kept a broader option list "
                f"for '{field_name}' because that chained scope could not be translated"
            )
        # Repeated panels are collapsed into one, so a multi-selection there
        # would silently merge instances -- keep those single-select.
        multi_select = (
            bool(context.variable.get("multi"))
            and name not in context.repeat_variable_names
        )
        context.control = _build_esql_param_control(
            variable_name=name,
            label=label or name,
            field_name=field_name,
            data_view=context.data_view,
            default=_variable_default_selection(context.variable),
            metric_field=metric_field,
            source_field=source_field,
            include_internal_metadata=True,
            extra_filters=scope_filters,
            multiple=multi_select,
        )
        if bool(context.variable.get("multi")) and not multi_select:
            # Only reachable for a repeat variable: Kibana has no equivalent of
            # Grafana panel repetition, so the repeated panels collapse into
            # one and a multi-selection there would silently merge instances.
            context.control_warnings.append(
                f"variable '{name}' was multi-select in Grafana but also drives panel "
                "repetition, which Kibana cannot reproduce; emitted a single-select "
                "control so the collapsed panel shows one instance at a time rather "
                "than silently merging them"
            )
        elif multi_select:
            # Multi-select IS preserved via MV_CONTAINS (exact match). Warn only
            # when the Grafana variable can still carry a regex selection
            # (custom values, variable regex filter, or a non-trivial allValue):
            # plain label_values() multi-select of concrete label values is
            # equivalent under exact match and should not Yellow Redis-style
            # dashboards with a theoretical regex delta.
            if _variable_multi_select_has_regex_risk(context.variable):
                context.control_warnings.append(
                    f"variable '{name}' is multi-select: panel filters bind it with "
                    f"MV_CONTAINS(?{name}, <field>) and the control allows several values "
                    "at once. Matching is exact rather than regex (ES|QL RLIKE takes only "
                    "a literal pattern), so a Grafana value written as a regex will not "
                    "match the way it did in Grafana"
                )
        context.handled = True
        return f"translated variable {name} as ES|QL parameter control"
    if scope_refs:
        context.control_warnings.append(
            f"variable '{name}' is scoped by {', '.join(f'${ref}' for ref in scope_refs)} in "
            "Grafana (label_values() selector); Kibana ES|QL controls cannot "
            "express that inter-control dependency on this target, so the migrated control "
            f"lists every '{field_name}' value instead of only those under the "
            "selected scope"
        )
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
    context.control_warnings.append(
        f"textbox variable '{name}' has no direct Kibana control equivalent; "
        "use the Kibana query bar or a KQL filter instead"
    )
    context.trace.append(
        f"textbox variable '{name}' has no direct Kibana control equivalent; "
        "use the Kibana query bar or KQL filter instead"
    )
    return f"noted textbox variable {name} (no Kibana control equivalent)"


@VARIABLE_TRANSLATORS.register("interval_variable", priority=25)
def interval_variable_rule(context):
    """Grafana interval variables are handled by Kibana's time picker and
    auto-bucketing; no explicit control is needed."""
    if context.variable.get("type") != "interval":
        return None
    name = context.variable.get("name", "")
    context.handled = True
    return f"skipped interval variable {name} (handled by Kibana time picker)"


@VARIABLE_TRANSLATORS.register("custom_variable", priority=26)
def custom_variable_rule(context):
    """Grafana ``custom`` variables (static comma-separated value lists) have
    no query semantics of their own, so no dropdown control is generated
    here. If the variable is referenced as ``$var``/``?var`` in a panel
    query, ``_ensure_param_controls`` (issue #131) synthesizes a binding
    control after translation; a custom variable that is never referenced in
    a query has nothing to bind and is genuinely dropped.

    This must NOT claim the value is "handled by Kibana's time picker" (that
    is only true for ``interval`` variables, not arbitrary custom lists such
    as ArgoCD's health_status/sync_status) — see interval_variable_rule.
    """
    if context.variable.get("type") != "custom":
        return None
    name = context.variable.get("name", "")
    context.handled = True
    context.trace.append(
        f"custom variable '{name}' has no direct Kibana control by itself; "
        "it becomes an ES|QL parameter-binding control if referenced as "
        f"${name} in a panel query, otherwise it is dropped"
    )
    return f"noted custom variable {name} (control depends on query usage)"


def translate_variables(
    template_list,
    datasource_index="metrics-*",
    rule_pack=None,
    resolver=None,
    repeat_variable_names=None,
    include_variable_names=False,
    collect_warnings=None,
):
    """Translate Grafana template variables into Kibana controls.

    ``collect_warnings``, if given a list, is extended with any dashboard-
    level control-translation warnings: broader chained controls, controls
    retained against an absent target field, and unsupported variable shapes
    that cannot emit a Kibana control. Optional and additive so existing
    callers that only want ``controls`` are unaffected.
    """
    rule_pack = rule_pack or RulePackConfig()
    controls = []
    variables_by_name = {
        var.get("name"): var
        for var in template_list
        if isinstance(var, dict) and var.get("name")
    }
    for var in template_list:
        context = VariableContext(
            variable=var,
            data_view=datasource_index,
            resolver=resolver,
            rule_pack=rule_pack,
            variables_by_name=variables_by_name,
            query_text=_variable_query_text(var),
            repeat_variable_names=set(repeat_variable_names or ()),
        )
        VARIABLE_TRANSLATORS.apply(context, stop_when=lambda ctx, _: ctx.handled)
        if collect_warnings is not None:
            collect_warnings.extend(context.control_warnings)
        if context.control:
            control = dict(context.control)
            if include_variable_names and var.get("name"):
                control[_CONTROL_SOURCE_VARIABLE_NAME] = var.get("name")
                if context.source_field:
                    control[_CONTROL_SOURCE_FIELD_NAME] = context.source_field
            elif not include_variable_names:
                control.pop(_CONTROL_RESOLVED_FIELD_NAME, None)
                control.pop(_CONTROL_SOURCE_FIELD_NAME, None)
            controls.append(control)
    return controls


def _covered_control_variable_refs(controls):
    refs: set[tuple[str, str]] = set()
    for control in controls or []:
        if not isinstance(control, dict):
            continue
        variable_name = control.get("variable_name") or control.get(_CONTROL_SOURCE_VARIABLE_NAME)
        if not variable_name:
            continue
        for field_name in (
            control.get("field"),
            control.get(_CONTROL_RESOLVED_FIELD_NAME),
            control.get(_CONTROL_SOURCE_FIELD_NAME),
        ):
            if field_name:
                refs.add((str(variable_name), str(field_name)))
    return refs


def _strip_internal_control_metadata(controls):
    for control in controls or []:
        if isinstance(control, dict):
            control.pop(_CONTROL_SOURCE_VARIABLE_NAME, None)
            control.pop(_CONTROL_RESOLVED_FIELD_NAME, None)
            control.pop(_CONTROL_SOURCE_FIELD_NAME, None)
    return controls


# An ES|QL named parameter token (``?var``), excluding engine-internal params
# such as ``?_tstart`` / ``?_tend`` / ``?_job`` which are materialized at
# query time and never bound by a dashboard control.
_ESQL_PARAM_RE = re.compile(r"(?<!\?)\?(?!\?)(?P<name>[A-Za-z][A-Za-z0-9_]*)")
# Quoted string literals, stripped before scanning so a ``?`` inside a value
# (e.g. a ``RLIKE "ab?c"`` pattern) is not mistaken for a named parameter.
_ESQL_QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")


def _query_param_names(query):
    """Return the ES|QL named parameters referenced by a query string."""
    if not isinstance(query, str):
        return set()
    unquoted = _ESQL_QUOTED_RE.sub('""', query)
    return {match.group("name") for match in _ESQL_PARAM_RE.finditer(unquoted)}


_ESQL_FIELD_CONTROL_RE = re.compile(r"\?\?(?P<name>[A-Za-z][A-Za-z0-9_]*)")
_ESQL_VALUE_PARAM_FIELD_PATTERNS = (
    # ``?var`` may be wrapped in ``TO_STRING(...)`` (issue #353's multi-select
    # guardrail type-fix); match with or without that wrapper.
    lambda name: re.compile(
        rf"MV_CONTAINS\(\s*(?:TO_STRING\(\s*)?\?{re.escape(name)}\s*\)?\s*,\s*(?P<field>`[^`]+`|[A-Za-z_][A-Za-z0-9_.]*)\s*\)"
    ),
    lambda name: re.compile(
        rf"(?P<field>`[^`]+`|[A-Za-z_][A-Za-z0-9_.]*)\s+(?:RLIKE|LIKE|==|!=|>=|<=|>|<)\s+\?{re.escape(name)}\b"
    ),
)


def _collect_emitted_field_control_vars(panels):
    """Return every ES|QL identifier/field control (``??var``) used by panels.

    Late-bound grouping (issue #282) emits ``STATS ... BY ??var``; each one
    needs a ``variable_type: fields`` binding control or the panel fails to
    load. Mirrors :func:`_collect_emitted_param_names` for the ``??`` form.
    """
    names: set[str] = set()
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        esql_cfg = panel.get("esql")
        query = esql_cfg.get("query") if isinstance(esql_cfg, dict) else None
        if not isinstance(query, str):
            continue
        unquoted = _ESQL_QUOTED_RE.sub('""', query)
        names |= {match.group("name") for match in _ESQL_FIELD_CONTROL_RE.finditer(unquoted)}
    return names


def _normalize_esql_field_token(field_name):
    text = str(field_name or "").strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        text = text[1:-1]
    return text


def _collect_value_param_bound_fields(query):
    """Map each ES|QL value parameter to the field(s) that bind it.

    Controls must populate from the same field panel queries filter on. Query
    variables can otherwise diverge when the variable's own source metric
    resolves a label field differently from the dashboard panels that consume
    ``?var`` (for example ``labels.instance`` vs ``instance``), which leaves
    the uploaded dashboard visually empty even though the queries parse.
    """
    if not isinstance(query, str):
        return {}
    unquoted = _ESQL_QUOTED_RE.sub('""', query)
    bound: dict[str, set[str]] = {}
    for name in _query_param_names(query):
        fields: set[str] = set()
        for pattern_factory in _ESQL_VALUE_PARAM_FIELD_PATTERNS:
            for match in pattern_factory(name).finditer(unquoted):
                field_name = _normalize_esql_field_token(match.group("field"))
                if field_name:
                    fields.add(field_name)
        if fields:
            bound[name] = fields
    return bound


def _retarget_esql_param_controls_to_panel_bindings(controls, panels):
    """Align ES|QL values controls with the field panel queries actually bind.

    When every panel using ``?var`` binds it to one concrete field, retarget the
    control's values query to that same field. This keeps the Dashboard API
    source-to-target contract coherent and avoids UI-only empties caused by a
    control populating from one field while panels filter on another.
    """
    if not controls or not panels:
        return controls
    bound_fields: dict[str, set[str]] = {}
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        esql_cfg = panel.get("esql")
        query = esql_cfg.get("query") if isinstance(esql_cfg, dict) else None
        for name, bound_names in _collect_value_param_bound_fields(query).items():
            bucket = bound_fields.setdefault(name, set())
            bucket.update(bound_names)
    if not bound_fields:
        return controls

    for control in controls:
        if not isinstance(control, dict) or control.get("type") != "esql":
            continue
        if str(control.get("variable_type") or "") == "fields":
            continue
        variable_name = str(control.get("variable_name") or "")
        target_fields = bound_fields.get(variable_name) or set()
        if len(target_fields) != 1:
            continue
        target_field = next(iter(target_fields))
        current_field = str(
            control.get(_CONTROL_RESOLVED_FIELD_NAME)
            or _extract_esql_values_bound_field(str(control.get("query") or ""))
            or ""
        )
        if not current_field or current_field == target_field:
            if target_field:
                control[_CONTROL_RESOLVED_FIELD_NAME] = target_field
            continue
        current_identifier = _esql_identifier(current_field)
        target_identifier = _esql_identifier(target_field)
        query = str(control.get("query") or "")
        if current_identifier not in query:
            continue
        control["query"] = query.replace(current_identifier, target_identifier)
        control[_CONTROL_RESOLVED_FIELD_NAME] = target_field
    return controls


def _apply_late_bound_group_controls(controls, field_vars, rule_pack):
    """Emit a field control for each ``??var`` grouping identifier (issue #282).

    Runs before :func:`_ensure_param_controls` so the ``fields`` control binds
    the identifier instead of the generic ``values`` control that the ``?var``
    completeness pass would otherwise synthesise. Any pre-existing control bound
    to the same variable is replaced so the identifier binds a fields control.
    """
    if not field_vars:
        return controls
    choices_map = getattr(rule_pack, "_late_bound_group_var_choices", None) or {}
    result = list(controls)
    for name in sorted(field_vars):
        spec = choices_map.get(name)
        if not isinstance(spec, dict) or not spec.get("choices"):
            continue
        result = [
            control
            for control in result
            if not (isinstance(control, dict) and control.get("variable_name") == name)
        ]
        result.append(_build_esql_field_control(name, spec))
    return result


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


def _degrade_conflicting_late_bound_group_panels(panels, panel_results):
    """Replace ``??var`` panels when another dashboard panel emits ``?var``.

    Kibana controls bind a variable as either a value or an identifier, not
    both. The per-query validator catches dual use inside one query; this
    dashboard-level pass catches the same conflict across separate panels.
    Preserve the established values-control behavior and degrade only the new
    late-bound grouping panel instead of shipping a field control that leaves
    the value panel unbound.
    """
    value_vars = _collect_emitted_param_names(panels)
    field_vars = _collect_emitted_field_control_vars(panels)
    conflicts = value_vars & field_vars
    if not conflicts:
        return

    for panel, panel_result in zip(panels, panel_results):
        if not isinstance(panel, dict):
            continue
        esql_config = panel.get("esql")
        query = esql_config.get("query") if isinstance(esql_config, dict) else None
        if not isinstance(query, str):
            continue
        unquoted = _ESQL_QUOTED_RE.sub('""', query)
        panel_conflicts = sorted(
            {
                match.group("name")
                for match in _ESQL_FIELD_CONTROL_RE.finditer(unquoted)
            }
            & conflicts
        )
        if not panel_conflicts:
            continue

        names = ", ".join(panel_conflicts)
        reason = (
            f"ES|QL parameter {names} is used as both value and field control "
            "across dashboard panels; one dashboard control cannot preserve both semantics"
        )
        panel.pop("esql", None)
        content = ["**Migration Required**", "", f"Reasons: {reason}"]
        if panel_result.promql_expr:
            content.extend(["", "Original PromQL:", "```", panel_result.promql_expr, "```"])
        panel["markdown"] = {"content": "\n".join(content)}

        panel_result.status = "not_feasible"
        panel_result.kibana_type = "markdown"
        panel_result.confidence = 0.0
        panel_result.esql_query = ""
        _append_unique(panel_result.reasons, reason)
        query_ir_metadata = (
            panel_result.query_ir.get("metadata")
            if isinstance(panel_result.query_ir, dict)
            and isinstance(panel_result.query_ir.get("metadata"), dict)
            else {}
        )
        for metadata_key in (
            "esql_identifier_param_defaults",
            "late_bound_group_vars",
            "late_bound_group_controls",
        ):
            query_ir_metadata.pop(metadata_key, None)
        _sync_visual_ir(panel_result, panel)


def _maybe_enable_dashboard_named_param_binding(rule_pack, variables):
    """Enable ES|QL ``?var`` emission when dashboard templating can bind controls.

    Grafana ``$var`` label matchers become ``field == ?var`` only when the rule
    pack advertises named-parameter binding. Offline / library callers that skip
    the CLI probe historically left the feature unset and dropped those matchers
    even when the dashboard had templating that ``_ensure_param_controls`` can
    bind. Enable the feature for this translate pass when templating defines at
    least one named variable.

    Never overrides an existing runtime feature state — a live ``--es-url`` probe
    that marked binding unsupported must continue dropping matchers (no unbound
    ``?var`` uploads).
    """
    if rule_pack is None:
        return
    if ESQL_NAMED_PARAM_BINDING in get_runtime_features(rule_pack):
        return
    has_named = any(
        isinstance(var, dict) and str(var.get("name") or "").strip()
        for var in (variables or [])
    )
    if not has_named:
        return
    set_runtime_feature(
        rule_pack,
        ESQL_NAMED_PARAM_BINDING,
        supported=True,
        source="dashboard_templating",
        confidence="assumed",
        reason=(
            "dashboard templating present; emit ?var matchers with binding controls"
        ),
    )


def _ensure_param_controls(
    controls,
    emitted_params,
    variables,
    data_view,
    resolver=None,
    rule_pack=None,
    control_warnings=None,
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
    missing = sorted(
        name
        for name in emitted_params
        if name not in bound
        and name not in (getattr(rule_pack, "ignored_labels", None) or [])
    )
    # Inert controls (the reverse of ``missing``): a control whose ``?var`` is
    # bound by neither a migrated panel query nor another control's populate
    # query. Grafana cascade parents (e.g. ``$namespace`` narrowing the
    # ``$instance`` option list) are *not* inert when the dependent control's
    # ES|QL still references ``?namespace`` — selecting them does change the
    # downstream dropdown. Only warn when the variable is unused end-to-end
    # (degrade-gracefully: never hide a truly dead control).
    if control_warnings is not None:
        useful = set(emitted_params or ())
        # Built-in time-range params are always bound by Kibana; ignore them.
        useful -= {"_tstart", "_tend"}
        # Fixed-point: a control for a useful var may itself reference other
        # params that therefore need a binding control (cascade parents).
        changed = True
        while changed:
            changed = False
            for control in controls:
                if not isinstance(control, dict) or control.get("type") != "esql":
                    continue
                name = control.get("variable_name")
                if not name or name not in useful:
                    continue
                for needed in _query_param_names(control.get("query")):
                    if needed in {"_tstart", "_tend", name}:
                        continue
                    if needed not in useful:
                        useful.add(needed)
                        changed = True
        for control in controls:
            if not isinstance(control, dict) or control.get("type") != "esql":
                continue
            name = control.get("variable_name")
            if name and name not in useful:
                control_warnings.append(
                    f"variable '{name}' has a Kibana control, but no migrated panel "
                    f"query binds ?{name} — the control renders and is selectable "
                    "yet changes no panel. This happens when the Grafana variable "
                    "only scoped another variable's option list (no panel filtered "
                    "on it), or when its panel label filter could not be translated "
                    "and was dropped. Check the panel warnings to tell which, then "
                    "either filter panels on the field or remove the control."
                )
    if not missing:
        return controls
    variables_by_name = {
        var.get("name"): var
        for var in variables
        if isinstance(var, dict) and var.get("name")
    }
    for name in missing:
        variable = variables_by_name.get(name, {})
        if variable.get("type") == "custom":
            controls.append(_build_static_esql_param_control(variable))
            continue
        label = variable.get("label") or name
        query_text = _variable_query_text(variable)
        source_field = _extract_variable_source_field(query_text) or name
        field_name = source_field
        source_metric = _extract_variable_source_metric(query_text)
        metric_field = _resolve_control_scope_metric(source_metric, resolver, rule_pack)
        if resolver:
            resolved = resolver.resolve_control_field(source_field, metric_field=metric_field or None)
            if resolved:
                field_name = resolved
        scope_filters = _extract_variable_scope_filters(
            query_text,
            resolver,
            rule_pack,
            control_warnings=control_warnings,
            variable_name=name,
        )
        controls.append(
            _build_esql_param_control(
                variable_name=name,
                label=label,
                field_name=field_name,
                data_view=data_view,
                default=_variable_default_selection(variable),
                metric_field=metric_field,
                source_field=source_field,
                include_internal_metadata=True,
                extra_filters=scope_filters,
                multiple=bool(variable.get("multi")),
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


def _variable_names_referenced_in_panels(panels) -> set[str]:
    """Grafana variable names referenced by any panel target's raw PromQL.

    Scans ``$var`` / ``${var}`` / ``${var:fmt}`` tokens in each target's
    ``expr`` *before* any translation-time rewrite, so it reflects what the
    source dashboard actually used the variable for -- independent of
    whether the migrated query still carries an equivalent reference. Used
    by :func:`_disclose_dropped_referenced_variables` (issue #356) to tell a
    variable that is genuinely unused from one whose loss changes query
    behavior.
    """
    names: set[str] = set()
    for panel in panels or []:
        if not isinstance(panel, dict):
            continue
        targets = panel.get("targets")
        if not isinstance(targets, list):
            continue
        for target in targets:
            if not isinstance(target, dict):
                continue
            expr = target.get("expr")
            if not isinstance(expr, str) or not expr:
                continue
            for match in _VARIABLE_REFERENCE_RE.finditer(expr):
                name = match.group(1) or match.group(2)
                if name:
                    names.add(name)
    return names


def _disclose_dropped_referenced_variables(variables, controls, panels, control_warnings):
    """Warn when a variable referenced by panel queries never became a
    control (issue #356).

    ``interval`` variables are the sharpest case: Grafana's own docs and this
    codebase's ``interval_variable_rule`` frame them as "handled by Kibana's
    time picker", but a variable used as a rate/range-vector window
    (``rate(x[$RateInterval])``) has nothing to do with the displayed time
    range. Dropping it does not just remove a dropdown -- it silently hands
    control of the rate window to a translator-chosen substitute: ES|QL
    panels use the TBUCKET bucket-width heuristic, and native PROMQL panels
    typically inline a fixed range (e.g. ``[5m]``). Neither tracks the
    Grafana value (AGENTS.md: degrade gracefully, do not hide a semantic
    gap).

    Generalised to any Grafana variable type that ends up with no bound
    control, since the same silent loss applies to any of them -- e.g. a
    ``query`` variable hidden with ``hide: 2`` skips straight past
    ``query_variable_rule`` with no control and no warning today. Must run
    after every control-synthesis pass (``_ensure_param_controls``,
    late-bound group controls, ``?var`` retargeting) so a variable that one
    of those passes did bind is correctly excluded. Also skips a variable
    that some earlier pass already named in a ``control_warnings`` entry
    (for example ``query_variable_rule``'s "could not resolve source field"
    or ``textbox_variable_rule``'s "no direct Kibana control equivalent") --
    that is already disclosed, just in more specific language, and a second
    generic entry would only add noise.
    """
    if control_warnings is None:
        return
    # A control's owning variable name is ``variable_name`` for ES|QL
    # parameter-binding controls, but a classic (non-ESQL) options/range
    # control -- built directly from ``context.control`` in
    # ``query_variable_rule`` and friends -- never sets that key; it only
    # gets ``_CONTROL_SOURCE_VARIABLE_NAME`` attached afterwards in
    # ``translate_variables``. Checking only ``variable_name`` here would
    # falsely flag every variable that resolved to a working classic control
    # as "dropped". Mirrors ``_covered_control_variable_refs``'s lookup.
    bound_names = {
        name
        for control in controls or []
        if isinstance(control, dict)
        for name in (
            control.get("variable_name"),
            control.get(_CONTROL_SOURCE_VARIABLE_NAME),
        )
        if name
    }
    referenced = _variable_names_referenced_in_panels(panels)
    for variable in variables or []:
        if not isinstance(variable, dict):
            continue
        name = variable.get("name")
        if not name or name in bound_names or name not in referenced:
            continue
        if any(f"'{name}'" in warning for warning in control_warnings):
            continue
        var_type = variable.get("type") or "unknown"
        if var_type == "interval":
            control_warnings.append(
                f"variable '{name}' (type 'interval') is used by panel queries as a "
                f"rate/range window (e.g. '[${name}]') but was dropped during migration "
                "-- no Kibana control was emitted, and Kibana's time picker only "
                "controls the displayed range, not this window. The migrated query no "
                "longer uses the Grafana interval: ES|QL panels pick a TBUCKET "
                "bucket-width, and native PROMQL panels typically inline a fixed range "
                "(e.g. [5m]), either of which can be narrower or wider than the source "
                "value"
            )
        else:
            control_warnings.append(
                f"variable '{name}' (type '{var_type}') is referenced by panel queries "
                "but was dropped during migration -- no Kibana control or query "
                "parameter was emitted for it, so it no longer has any effect on "
                "query behavior"
            )


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


def _coerce_scalar_number(value) -> str | None:
    """Return *value* as a numeric string when it represents a single number.

    Preserves the source formatting (``"0.95"`` stays ``"0.95"``, ``"95"`` stays
    ``"95"``) so the substituted PromQL reads like the author's intent. Lists
    collapse to their first element (Grafana multi-select snapshot). Returns
    ``None`` for anything that is not a single finite number — label names,
    ``"All"`` sentinels, regexes, etc.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = repr(value)
    elif isinstance(value, list):
        return _coerce_scalar_number(value[0]) if value else None
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return text


def _dropdown_scalar_values(variables) -> dict[str, str]:
    """Map each Grafana template variable to its selected numeric value.

    Only variables whose dropdown is currently set to a single number are
    included — those are the ones that legitimately stand for a scalar in a
    PromQL slot (issue #157). The variable's ``current`` selection is preferred;
    a sole numeric option is used as a fallback when there is no ``current``.
    """
    out: dict[str, str] = {}
    for variable in variables or []:
        if not isinstance(variable, dict):
            continue
        name = variable.get("name")
        if not name or not isinstance(name, str):
            continue
        current = variable.get("current")
        number = None
        if isinstance(current, dict):
            number = _coerce_scalar_number(current.get("value"))
            if number is None:
                number = _coerce_scalar_number(current.get("text"))
        if number is None:
            values, _source = _resolve_variable_values(variable)
            if len(values) == 1:
                number = _coerce_scalar_number(values[0])
        if number is not None:
            out[name] = number
    return out


def _substitute_scalar_dropdown_values(dashboard: dict) -> dict:
    """Rewrite numeric-dropdown variables sitting in PromQL scalar slots.

    Walks every panel target's ``expr`` and substitutes the dropdown's selected
    value into scalar argument slots (``histogram_quantile``'s percentile,
    ``topk``'s ``k``, ``vector``'s value, ``clamp`` bounds, …). Mutates the
    target expressions in place and returns the dashboard so the call site can
    keep its fluent assignment. A no-op when the dashboard has no numeric
    dropdowns (the common case), so non-parameterized dashboards are untouched.
    """
    variables = (dashboard.get("templating", {}) or {}).get("list", []) or []
    scalar_values = _dropdown_scalar_values(variables)
    if not scalar_values:
        return dashboard

    for panel in _flatten_dashboard_panels(dashboard):
        targets = panel.get("targets")
        if not isinstance(targets, list):
            continue
        for target in targets:
            if not isinstance(target, dict):
                continue
            expr = target.get("expr")
            if isinstance(expr, str) and expr:
                target["expr"] = substitute_scalar_template_vars(expr, scalar_values)
    return dashboard


def _substitute_grafana_variables(text: str, substitutions: dict[str, str]) -> str:
    """Replace ``$var`` and ``${var}`` (and ``${var:fmt}``) in ``text``
    with ``substitutions[var]``. Variables not in the dict are left
    untouched so a downstream pass still sees them.
    """
    if not isinstance(text, str) or not substitutions:
        return text

    def _repl(match: re.Match) -> str:
        name = str(match.group(1) or match.group(2) or "")
        return substitutions.get(name, str(match.group(0) or ""))

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
                warn_result.reasons = [
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
                warn_result.reasons = [
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
_CONTROL_SOURCE_VARIABLE_NAME = "_source_variable_name"
_CONTROL_SOURCE_FIELD_NAME = "_source_field_name"
_CONTROL_RESOLVED_FIELD_NAME = "_resolved_field_name"


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


_CONTROL_COVERED_VARIABLE_WARNINGS = {
    _DROPPED_VARS_WARNING,
    _DROPPED_LOGQL_LABEL_WARNING,
}


def _template_var_name_from_matcher_value(value):
    text = str(value or "").strip()
    name = grafana_template_var_name(text)
    if name:
        return name
    unanchored = text
    if unanchored.startswith("^"):
        unanchored = unanchored[1:]
    if unanchored.endswith("$") and not unanchored.endswith("\\$"):
        unanchored = unanchored[:-1]
    return grafana_template_var_name(unanchored)


def _selector_metric_name_before_brace(text, brace_idx):
    prefix = str(text or "")[:brace_idx].rstrip()
    match = re.search(r"([A-Za-z_:][A-Za-z0-9_:]*)$", prefix)
    return match.group(1) if match else ""


def _resolve_metric_field_for_warning_coverage(metric_name, resolver):
    if not metric_name or not resolver:
        return None
    resolve_metric = getattr(resolver, "resolve_metric_field", None)
    if resolve_metric is None:
        return metric_name
    try:
        return resolve_metric(metric_name) or metric_name
    except Exception:
        return metric_name


def _resolve_label_field_for_warning_coverage(label, metric_name, resolver):
    if not label:
        return ""
    if not resolver:
        return label
    metric_field = _resolve_metric_field_for_warning_coverage(metric_name, resolver)
    try:
        resolved = resolver.resolve_label(label, metric_field=metric_field)
    except Exception:
        resolved = label
    return resolved or ""


def _source_label_matcher_variable_coverage(expr, resolver=None):
    matcher_ref_alternatives: list[set[tuple[str, str]]] = []
    has_uncoverable_matcher = False
    text = str(expr or "")
    idx = 0
    while idx < len(text):
        if text[idx] != "{":
            idx += 1
            continue
        end = idx + 1
        quote = ""
        escaped = False
        while end < len(text):
            char = text[end]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in ("'", '"'):
                quote = char
            elif char == "}":
                break
            end += 1
        if end >= len(text) or text[end] != "}":
            break
        selector = text[idx + 1:end]
        metric_name = _selector_metric_name_before_brace(text, idx)
        for matcher_text in _split_top_level_csv(selector):
            match = _PROMQL_LABEL_MATCHER_VAR_RE.match(matcher_text)
            if not match:
                continue
            name = _template_var_name_from_matcher_value(match.group("value"))
            if name and not name.startswith("__"):
                if match.group("op") in {"!=", "!~"}:
                    has_uncoverable_matcher = True
                else:
                    raw_label = match.group("label")
                    field_name = _resolve_label_field_for_warning_coverage(
                        raw_label,
                        metric_name,
                        resolver,
                    )
                    alternatives = {(name, raw_label)}
                    if field_name:
                        alternatives.add((name, field_name))
                    matcher_ref_alternatives.append(alternatives)
        idx = end + 1
    return matcher_ref_alternatives, has_uncoverable_matcher


def _panel_result_variable_warning_is_covered(panel_result, covered_control_refs, resolver=None):
    source_ref_alternatives, has_uncoverable_matcher = _source_label_matcher_variable_coverage(
        panel_result.promql_expr,
        resolver=resolver,
    )
    if has_uncoverable_matcher:
        return False
    return bool(source_ref_alternatives) and all(
        bool(alternatives & covered_control_refs)
        for alternatives in source_ref_alternatives
    )


def _rewrite_variable_warnings(panel_results, covered_control_refs, resolver=None):
    """Clear variable-drop warnings once dashboard controls cover them.

    ``PanelResult.reasons`` carries the translation warnings.
    """
    rewritten_panel_results = []
    if not covered_control_refs:
        return rewritten_panel_results
    for pr in panel_results:
        original_count = len(pr.reasons)
        pr.reasons = [
            w
            for w in pr.reasons
            if (
                w not in _CONTROL_COVERED_VARIABLE_WARNINGS
                or not _panel_result_variable_warning_is_covered(
                    pr,
                    covered_control_refs,
                    resolver=resolver,
                )
            )
        ]
        if len(pr.reasons) == original_count:
            continue
        if pr.status == "migrated_with_warnings" and not pr.reasons:
            # Keep warning status when notes still imply a semantic loss (e.g.
            # field overrides) even after variable-drop reasons were cleared.
            if not panel_notes_imply_warning(pr.notes):
                pr.status = "migrated"
                pr.confidence = max(pr.confidence, 0.85)
        rewritten_panel_results.append(pr)
    return rewritten_panel_results


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

    # L2b: raise panels to their legibility floor by scaling each horizontal
    # band uniformly, which preserves the band's internal proportions.
    _apply_band_uniform_min_height(yaml_panels)

    # L1.5 (gap compaction): strip stale vertical dead-space left by collapsed
    # Grafana rows (their children keep last-expanded absolute y, so the first
    # visible row can sit hundreds of rows above the rest). Runs after the
    # minimums so it operates on final heights.
    _compact_vertical_gaps(yaml_panels)

    return yaml_panels


# An empty horizontal band taller than this many Kibana rows is treated as a
# stale-coordinate artifact (collapsed-row children keep last-expanded absolute
# gridPos) rather than deliberate spacing, and is removed. Deliberate author
# gaps are at most a panel height or two; collapsed-row artifacts span hundreds
# of rows, so the two are cleanly separable. Kept well above the ~9-row gaps the
# faithful transform is designed to preserve.
MAX_INTRA_SECTION_GAP = 24


def _compact_vertical_gaps(yaml_panels: list[dict]) -> None:
    """Remove stale vertical dead-space within a single section's panels.

    Finds every fully-empty horizontal band (a contiguous range of Kibana rows
    occupied by no panel) taller than :data:`MAX_INTRA_SECTION_GAP` and shifts
    everything below it up so the band collapses to nothing — matching how
    Grafana re-flows a collapsed row's children contiguously on expand. Relative
    order, x positions, sizes, and smaller deliberate gaps are all preserved,
    and because only empty rows are removed no overlap can be introduced.
    """
    if len(yaml_panels) < 2:
        return
    spans = []
    max_bottom = 0
    for panel in yaml_panels:
        _x, y, _w, h = _rect(panel)
        if h <= 0:
            continue
        spans.append((panel, y))
        max_bottom = max(max_bottom, y + h)
    if not spans or max_bottom <= 0:
        return

    occupied = bytearray(max_bottom)
    for panel, _y in spans:
        _x, y, _w, h = _rect(panel)
        for row in range(max(0, y), min(max_bottom, y + h)):
            occupied[row] = 1

    # Collect empty bands taller than the threshold as (first_row_below, rows_removed).
    cuts: list[tuple[int, int]] = []
    run_start = None
    for row in range(max_bottom):
        if not occupied[row]:
            if run_start is None:
                run_start = row
        elif run_start is not None:
            if row - run_start > MAX_INTRA_SECTION_GAP:
                cuts.append((row, row - run_start))
            run_start = None
    if not cuts:
        return

    for panel, y in spans:
        delta = sum(amount for first_row, amount in cuts if y >= first_row)
        if delta:
            position = panel.setdefault("position", {})
            position["y"] = y - delta


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

    Row-uniformity guard: a panel is never grown taller than the tallest
    *source* panel sharing its top edge (its row). Grafana scoreboards put
    several short tiles of different Kibana types in one row (eg. a bargauge
    next to gauges, all at gridPos h=4); without this cap the gauge ``min_h``
    of 8 would bump only the gauges, leaving the bar at 6 and the row ragged.
    A panel alone at its y has no row-mates and still bumps freely.
    """
    # Tallest source (pre-bump) height per occupied top edge, for rows of >1.
    row_height_cap: dict[int, int] = {}
    rows_by_y: dict[int, list[int]] = {}
    for panel in yaml_panels:
        _x, py, _w, ph = _rect(panel)
        rows_by_y.setdefault(py, []).append(ph)
    for py, heights in rows_by_y.items():
        if len(heights) > 1:
            row_height_cap[py] = max(heights)

    # Lift each capped row's target to the highest type-specific legibility
    # floor among all panels in that row. A gauge (min_h=8) beside a bar
    # (min_h=6) raises the whole row to 8 so every panel reaches legibility
    # AND the row stays height-uniform.
    for panel in yaml_panels:
        esql_cfg = panel.get("esql")
        if isinstance(esql_cfg, dict) and esql_cfg.get("type"):
            _etype = str(esql_cfg["type"])
        elif "markdown" in panel:
            _etype = "markdown"
        else:
            _etype = str(_kibana_panel_type(panel) or "")
        _constraints = _TYPE_SIZE_CONSTRAINTS.get(_etype)
        if _constraints is None:
            continue
        _, _type_min_h, _ = _constraints
        _x, _py, _w, _ph = _rect(panel)
        if _py in row_height_cap:
            row_height_cap[_py] = max(row_height_cap[_py], _type_min_h)

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

        # min_h is applied by _apply_band_uniform_min_height, not here.
        min_w, _min_h, max_h = constraints
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

        # Height growth is NOT done here. Bumping one panel at a time and
        # rejecting on collision breaks the proportions the author chose, and
        # breaks them asymmetrically: on Node Exporter Full's first row
        # "CPU Cores" could not grow (RootFS Total sits directly below it) while
        # "RootFS Total" could, so two panels with identical source geometry
        # ended up 3 and 6 high and the row went ragged.
        # _apply_band_uniform_min_height scales each band as a unit instead.

        panel["size"] = {"w": w, "h": h}
        # Re-apply the legacy x-clamp + grid-overflow guard.
        position = dict(panel.get("position", {}))
        max_x = KIBANA_GRID_COLS - w
        if max_x < 0:
            max_x = 0
        position["x"] = min(int(position.get("x", 0) or 0), max_x)
        panel["position"] = position


def _panel_effective_type(panel):
    """The Kibana visualization type that governs this panel's size limits."""
    esql_cfg = panel.get("esql")
    if isinstance(esql_cfg, dict) and esql_cfg.get("type"):
        return str(esql_cfg["type"])
    if "markdown" in panel:
        return "markdown"
    return str(_kibana_panel_type(panel) or "")


def _vertical_bands(yaml_panels):
    """Group panel indices into maximal bands that share vertical space.

    A band is a run of panels whose vertical extents overlap transitively, so a
    tall panel and the stack of short ones beside it belong to the same band —
    which is exactly the relationship that has to be preserved for a row to look
    right. Bands never overlap each other, so each can be rescaled independently.
    """
    order = sorted(
        range(len(yaml_panels)), key=lambda i: (_rect(yaml_panels[i])[1], _rect(yaml_panels[i])[0])
    )
    bands: list[list[int]] = []
    current: list[int] = []
    current_bottom = None
    for i in order:
        _, y, _, h = _rect(yaml_panels[i])
        if current and current_bottom is not None and y >= current_bottom:
            bands.append(current)
            current = []
            current_bottom = None
        current.append(i)
        current_bottom = max(current_bottom or 0, y + h)
    if current:
        bands.append(current)
    return bands


def _apply_band_uniform_min_height(yaml_panels):
    """Raise panels to their legibility floor without distorting the layout.

    The faithful coordinate transform already reproduces the author's
    proportions: Node Exporter Full's first row has six gauges at h=4 beside a
    2+2 stat stack, and after the 30/20 row scale that is 6 beside 3+3 — still
    flush. Applying a per-type floor panel-by-panel destroys that, because the
    floors differ by type (gauge 8, metric 6) and a bump gets rejected wherever a
    neighbour is in the way.

    Scaling a whole band by ONE factor fixes both problems at once. The factor is
    the smallest that lifts every panel in the band to its own floor, so relative
    heights are untouched and the band stays flush. Scaling top and bottom edges
    (rather than heights) keeps panels that touched still touching, so no overlap
    can be introduced. Bands below are shifted by the growth so nothing collides.

    For that first row the factor is 2: gauges 6 -> 12, stats 3+3 -> 6+6. Flush,
    and every panel clears its floor.
    """
    if not yaml_panels:
        return

    def half_up(value: float) -> int:
        return int(value + 0.5)

    offset = 0
    for band in _vertical_bands(yaml_panels):
        rects = {i: _rect(yaml_panels[i]) for i in band}
        old_top = min(rects[i][1] for i in band)
        old_bottom = max(rects[i][1] + rects[i][3] for i in band)

        scale = 1.0
        for i in band:
            constraints = _TYPE_SIZE_CONSTRAINTS.get(_panel_effective_type(yaml_panels[i]))
            if constraints is None:
                continue
            _, min_h, _ = constraints
            height = rects[i][3]
            if height > 0 and height < min_h:
                scale = max(scale, min_h / height)

        new_top = old_top + offset
        for i in band:
            _, y, _, h = rects[i]
            ny = new_top + half_up((y - old_top) * scale)
            nb = new_top + half_up((y + h - old_top) * scale)
            panel = yaml_panels[i]
            position = dict(panel.get("position", {}))
            size = dict(panel.get("size", {}))
            position["y"] = ny
            size["h"] = max(1, nb - ny)
            panel["position"] = position
            panel["size"] = size
        new_bottom = new_top + half_up((old_bottom - old_top) * scale)
        offset = new_bottom - old_bottom


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


def _iter_leaf_panels(panels: list[dict]):
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            inner = section.get("panels")
            if isinstance(inner, list):
                yield from _iter_leaf_panels(inner)
            continue
        yield panel


def _apply_panel_layout_overrides_recursively(panels: list[dict], overrides: list[dict]) -> None:
    if not panels or not overrides:
        return

    override_map = {
        str(override.get("title_match") or "").strip().casefold(): override
        for override in overrides
        if str(override.get("title_match") or "").strip()
    }
    if not override_map:
        return

    for panel in panels:
        title_key = str(panel.get("title") or "").strip().casefold()
        override = override_map.get(title_key)
        if override:
            position_override = override.get("position") or {}
            if position_override:
                position = dict(panel.get("position", {}))
                for key in ("x", "y"):
                    value = position_override.get(key)
                    if value is not None:
                        position[key] = int(value)
                panel["position"] = position
            size_override = override.get("size") or {}
            if size_override:
                size = dict(panel.get("size", {}))
                for key in ("w", "h"):
                    value = size_override.get(key)
                    if value is not None:
                        size[key] = int(value)
                panel["size"] = size
            new_title = override.get("title")
            if isinstance(new_title, str) and new_title.strip():
                panel["title"] = new_title.strip()
            if "collapsed" in override and isinstance(panel.get("section"), dict):
                panel["section"]["collapsed"] = bool(override.get("collapsed"))
        section = panel.get("section")
        if isinstance(section, dict):
            inner = section.get("panels")
            if isinstance(inner, list):
                _apply_panel_layout_overrides_recursively(inner, overrides)


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


def translate_dashboard(dashboard, datasource_index="metrics-*", esql_index=None, rule_pack=None, resolver=None,
                        llm_endpoint="", llm_model="", llm_api_key="", output_stem=None,
                        id_disambiguator=""):
    """Translate one Grafana dashboard into a :class:`MigrationResult`.

    Returns the :class:`MigrationResult`. The artifacts the pipeline writes are
    ``native/*.native.json`` and ``ir/*.ir.json``, both derived from
    ``result.dashboard_ir``; nothing is written to disk here. Callers that need
    the kb-dashboard-core dict shape (for example a structural cross-check of
    the native payload) build it in memory with
    ``result.dashboard_ir.to_yaml_dict()``.

    ``id_disambiguator`` comes from the run's artifact-stem allocation and is
    non-empty only when another dashboard in the run has the same title; it
    keeps the two dashboards off one Kibana dashboard id (see
    ``targets/kibana/dashboards_api.py::_stable_dashboard_id_from_ir``).
    """
    rule_pack = rule_pack or RulePackConfig()
    title = dashboard.get("title", "Untitled Dashboard")
    uid = dashboard.get("uid", "unknown")
    description = dashboard.get("description", "") or f"Migrated from Grafana ({uid})"
    # Captured before ``_expand_repeat_panels`` rebuilds ``dashboard`` into a
    # panel-focused copy that does not carry dashboard-level metadata.
    source_tags = _source_dashboard_tags(dashboard)
    dashboard_settings_warnings: list[str] = []
    dashboard_time_range = _grafana_dashboard_time_range(dashboard, dashboard_settings_warnings)
    dashboard_refresh_interval = _grafana_dashboard_refresh_interval(dashboard, dashboard_settings_warnings)

    result = MigrationResult(
        dashboard_title=title,
        dashboard_uid=uid,
        source_file=str(dashboard.get("_source_file") or ""),
        folder_title=str((dashboard.get("_grafana_meta") or {}).get("folderTitle") or ""),
        inventory=build_dashboard_inventory(dashboard),
    )
    # Dashboard time/refresh has no PanelResult-style tracking of its own
    # (same rationale as ``control_warnings`` for template-variable controls),
    # so an unrecognized value surfaces here rather than vanishing silently.
    result.control_warnings.extend(dashboard_settings_warnings)

    # L4: expand ``repeat: $var`` panels into one concrete clone per
    # resolved variable value BEFORE any downstream logic walks the
    # panels. From here on every panel in ``dashboard`` is a regular
    # (non-templated) panel and the rest of the pipeline can stay
    # ignorant of the fan-out.
    dashboard = _expand_repeat_panels(dashboard, result)

    # Issue #157: a Grafana dropdown that stands for a *number* (percentile,
    # top-N, threshold) lands in a PromQL scalar slot. Substitute the dropdown's
    # selected value into those slots BEFORE any translation path runs so e.g.
    # ``histogram_quantile($quantile, …)`` becomes ``histogram_quantile(0.95, …)``
    # — valid PromQL that migrates into a working panel instead of silently
    # degrading to a "Migration Required" placeholder.
    dashboard = _substitute_scalar_dropdown_values(dashboard)

    all_panels = _flatten_dashboard_panels(dashboard)
    result.total_panels = len(all_panels)

    # Offline per-metric series-label map: lets bare gauge selectors that name no labels of
    # their own recover per-series grouping from other panels / template variables.
    metric_series_labels = build_metric_series_labels(dashboard)

    variables = dashboard.get("templating", {}).get("list", [])
    # Gap A: when templating can bind controls, emit ``?var`` label matchers
    # instead of dropping them. Must run before late-bound grouping choices
    # (those also require ``binds_esql_named_params``).
    _maybe_enable_dashboard_named_param_binding(rule_pack, variables)
    # Record which ``?var`` params default to the regex match-all so both the
    # ES|QL and native PROMQL matcher emitters loosen equality matchers on
    # All/multi variables into regex matches and render data on first load
    # (PR #133 review). Stored on the shared rule pack so it is reachable from
    # the resolver (``resolver._rule_pack``) on the ES|QL path and threaded
    # explicitly into the native path. Set before any panel translation runs.
    setattr(rule_pack, "_regex_default_param_names", _collect_regex_default_param_names(variables))
    # Multi-select variables bind via MV_CONTAINS instead of RLIKE so the
    # Kibana control can stay multi-select. Same storage contract as above:
    # set on the shared rule pack before any panel translation runs, reachable
    # from the resolver as ``resolver._rule_pack``.
    setattr(rule_pack, "_multi_select_param_names", _collect_multi_select_param_names(variables))
    # Issue #282: map grouping template variables (``by ($var)``) to ES|QL
    # field-control specs up front so the translation guardrail can defer the
    # dimension to a ``??var`` identifier control instead of failing. Gated on
    # the target binding ES|QL parameters inside the builder.
    setattr(
        rule_pack,
        "_late_bound_group_var_choices",
        _build_late_bound_group_var_choices(variables, resolver, rule_pack),
    )

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

    flat_panels = list(_iter_leaf_panels(top_level_panels))

    # Parameters (``?var``) actually emitted by panel queries drive control
    # completeness: every one needs a binding control, and any variable that
    # became a control should no longer be reported as a dropped filter.
    _degrade_conflicting_late_bound_group_panels(
        flat_panels,
        result.yaml_panel_results,
    )
    emitted_params = _collect_emitted_param_names(flat_panels)
    emitted_field_vars = _collect_emitted_field_control_vars(flat_panels)

    controls_data_view = _infer_controls_data_view(flat_panels, datasource_index, rule_pack)
    controls_resolver = _resolver_for_index(resolver, rule_pack, controls_data_view)
    controls = translate_variables(
        variables,
        controls_data_view,
        rule_pack=rule_pack,
        resolver=controls_resolver,
        repeat_variable_names=repeat_variable_names,
        include_variable_names=True,
        collect_warnings=result.control_warnings,
    )
    # Issue #282: bind each emitted ``??var`` grouping identifier to a fields
    # control before the ``?var`` completeness pass so it is not shadowed by a
    # generic values control.
    controls = _apply_late_bound_group_controls(controls, emitted_field_vars, rule_pack)
    controls = _ensure_param_controls(
        controls,
        emitted_params,
        variables,
        controls_data_view,
        resolver=controls_resolver,
        rule_pack=rule_pack,
        control_warnings=result.control_warnings,
    )
    controls = _retarget_esql_param_controls_to_panel_bindings(controls, flat_panels)
    _disclose_dropped_referenced_variables(
        variables, controls, all_panels, result.control_warnings
    )
    rewritten_panel_results = _rewrite_variable_warnings(
        result.panel_results,
        _covered_control_variable_refs(controls),
        resolver=controls_resolver,
    )
    for panel_result in rewritten_panel_results:
        panel_result.operational_ir = build_operational_ir(
            panel_result,
            dashboard_title=result.dashboard_title,
            dashboard_uid=result.dashboard_uid,
            source_file=result.source_file,
            folder_title=result.folder_title,
        )
    # Grafana dashboard-level ``links[]`` of type "link" (a concrete external
    # URL) have a resolvable destination and become a real Kibana ``links``
    # panel; tag-driven "dashboards" links stay manual (see
    # ``build_links_panel``). Appended last, at the next free row, so it
    # never overlaps a panel/section already placed by the loop above. A
    # matching ``PanelResult`` is added so rendered leaf panels and migration-
    # report panel results stay 1:1.
    grafana_dashboard_links = translate_dashboard_links(dashboard)
    links_panel = build_links_panel(grafana_dashboard_links)
    if links_panel is not None:
        links_panel["position"] = {"x": 0, "y": dashboard_y_cursor}
        top_level_panels.append(links_panel)
        n_url_links = sum(
            1 for link in grafana_dashboard_links if link.get("kibana_action") == "url_drilldown"
        )
        link_warnings: list[str] = []
        if any(
            link.get("kibana_action") == "url_drilldown" and link.get("include_vars")
            for link in grafana_dashboard_links
        ):
            link_warnings.append(
                "Grafana link template variables are dropped because Kibana links panels "
                "cannot forward dashboard variables automatically"
            )
        if any(
            link.get("kibana_action") == "url_drilldown" and link.get("keep_time")
            for link in grafana_dashboard_links
        ):
            link_warnings.append(
                "Grafana link time range forwarding is dropped because Kibana external "
                "links cannot inherit the dashboard time range automatically"
            )
        links_panel_result = PanelResult(
            str(links_panel.get("title") or "Dashboard Links"),
            "dashboard_links",
            "links",
            "migrated_with_warnings" if link_warnings else "migrated",
            0.8 if link_warnings else 1.0,
            reasons=[
                f"synthesized from {n_url_links} Grafana dashboard-level link(s)",
                *link_warnings,
            ],
        )
        _sync_visual_ir(links_panel_result, links_panel)
        result.panel_results.append(links_panel_result)
        result.yaml_panel_results.append(links_panel_result)
        # ``total_panels`` is also the denominator for target migration
        # dispositions. Include this synthesized renderable panel so
        # migrated/warning counts cannot exceed their denominator.
        result.total_panels += 1

    recompute_result_counts(result)
    controls = _strip_internal_control_metadata(controls)

    yaml_doc = {
        "dashboards": [
            {
                "name": title,
                "description": description,
                "minimum_kibana_version": _dashboard_minimum_kibana_version(flat_panels),
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
    for dashboard in yaml_doc.get("dashboards") or []:
        _apply_panel_layout_overrides_recursively(
            dashboard.get("panels") or [],
            getattr(rule_pack, "panel_layout_overrides", []) or [],
        )

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
    #
    # This 2D-grid layout math (row-filling, overlap resolution) still runs
    # on the kb-dashboard-core dict shape: it is grid arithmetic tied to that
    # shape, not dashboard semantics, so porting it to the IR buys nothing.
    # Everything downstream of this point -- the native Dashboards API
    # payload and the on-disk YAML -- is derived from the semantic
    # `DashboardIR` built right after, not from this dict directly (see
    # docs/architecture/asset-model.md).
    for dashboard in yaml_doc.get("dashboards") or []:
        _resolve_section_overlaps_recursively(dashboard.get("panels") or [])
        final_leaf_panels = list(_iter_leaf_panels(dashboard.get("panels") or []))
        for panel, panel_result in zip(final_leaf_panels, result.yaml_panel_results):
            _sync_visual_ir(panel_result, panel)

    # IR-first: `DashboardIR` is the primary working artifact from here on.
    # The native Dashboards API payload (`native_dashboard_from_ir`) and the
    # on-disk YAML (`DashboardIR.to_yaml_dict`) are both *derived* from it, so
    # they can never drift from each other -- see
    # tests/test_grafana_native_dashboard_emission.py for the parity guarantee.
    dashboard_ir = DashboardIR.from_yaml_dict(yaml_doc["dashboards"][0], source_adapter="grafana")
    dashboard_ir.uid = uid
    dashboard_ir.tags = source_tags
    dashboard_ir.time_range = dashboard_time_range
    dashboard_ir.refresh_interval = dashboard_refresh_interval
    # Set before `native_dashboard_from_ir`: it is what keeps two same-titled
    # dashboards off one Kibana dashboard id (the upsert key).
    dashboard_ir.id_disambiguator = str(id_disambiguator or "")
    # Source lineage is not expressible in the intermediate document shape, so it
    # has to be set here or ir/<stem>.ir.json ships with empty `source_file` and
    # `folder`. Taken off `result` rather than re-derived from the raw dashboard
    # so the IR artifact and the migration report cannot disagree about where a
    # dashboard came from.
    dashboard_ir.source_file = str(getattr(result, "source_file", "") or "")
    dashboard_ir.folder = str(getattr(result, "folder_title", "") or "")
    result.dashboard_ir = dashboard_ir

    native_dashboard, native_counts = native_dashboard_from_ir(dashboard_ir)
    result.native_dashboard = native_dashboard
    native_counts_dict, native_reasons = native_counts.as_dicts()
    result.native_dashboard_stats = {**native_counts_dict, "reasons": native_reasons}

    return result


__all__ = [
    "PANEL_TYPE_MAP",
    "SKIP_PANEL_TYPES",
    "PanelContext",
    "VariableContext",
    "_dashboard_output_stem",
    "metrics_query_index",
    "query_variable_rule",
    "translate_dashboard",
    "translate_panel",
    "translate_variables",
]
