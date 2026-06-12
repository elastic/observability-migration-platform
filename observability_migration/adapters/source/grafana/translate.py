# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Rule-based PromQL to ES|QL translation pipeline.

Falls back to LLM-assisted translation when the rule engine cannot handle
an expression (requires ``--local-ai-endpoint`` and ``--local-ai-model``).
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from observability_migration.core.assets.query import QueryIR, build_query_ir
from observability_migration.core.assets.target_query_contract import (
    FieldRequirement,
    TargetEnvironmentSnapshot,
    TargetQueryContract,
)
from observability_migration.core.verification.field_capabilities import FieldCapability

from .contract_evaluator import evaluate_target_query_contract
from .fulfillment_planner import plan_contract_fulfillment
from .llm_translate import attempt_llm_translation
from .preflight import (
    _DERIVED_METRIC_NAMES,
    _looks_like_counter_metric,
    _metric_candidates,
)
from .promql import (
    AGG_FUNCTION_MAP,
    OUTER_AGG_MAP,
    PromQLFragment,
    _apply_fragment_to_context,
    _build_esql,
    _build_formula_plan,
    _build_log_message_filter,
    _build_shared_measure_pipeline,
    _build_stats_call,
    _build_where_lines,
    _can_use_direct_ts_gauge,
    _collapse_summary_ts_query,
    _format_scalar_value,
    _frag_eval_line,
    _frag_filters,
    _frag_group_labels,
    _frag_has_incompatible_group_fields,
    _frag_has_incompatible_target_fields,
    _gauge_can_use_ts,
    _grouping_parts,
    _inline_filters_into_stats_expr,
    _is_counter_fallback,
    _parse_fragment,
    _parse_logql_search,
    _resolve_metric_field,
    _summary_mode_from_metadata,
    classify_promql_complexity,
    gauge_default_agg_warning,
    preprocess_grafana_macros,
    resolve_counter_range_translation,
)
from .rules import (
    QUERY_CLASSIFIERS,
    QUERY_POSTPROCESSORS,
    QUERY_PREPROCESSORS,
    QUERY_TRANSLATORS,
    QUERY_VALIDATORS,
    RulePackConfig,
    _append_unique,
)
from .semantic_planner import RuntimeCapabilities, plan_grafana_metric_contract

# Exact ES|QL renderings for PromQL elementwise math/trig wrappers. ``{m}`` is the
# metric field. Verified on-cluster: every PromQL function maps to an exact ES|QL
# function or closed-form expression (ln -> natural LOG, log2 -> LOG(2, x),
# deg/rad -> the radian<->degree conversions).
_MATH_FN_ESQL = {
    "abs": "ABS({m})",
    "ceil": "CEIL({m})",
    "floor": "FLOOR({m})",
    "sqrt": "SQRT({m})",
    "exp": "EXP({m})",
    "ln": "LOG({m})",
    "log2": "LOG(2, {m})",
    "log10": "LOG10({m})",
    "acos": "ACOS({m})",
    "asin": "ASIN({m})",
    "atan": "ATAN({m})",
    "cos": "COS({m})",
    "sin": "SIN({m})",
    "tan": "TAN({m})",
    "cosh": "COSH({m})",
    "sinh": "SINH({m})",
    "tanh": "TANH({m})",
    "deg": "({m} * 180 / PI())",
    "rad": "({m} * PI() / 180)",
}


def _default_instance_field(rp):
    return "instance" if rp.native_promql else "service.instance.id"


def _keep(*field_lists) -> str:
    """Build a deduplicated KEEP clause from one or more field lists."""
    seen: dict[str, None] = {}
    for lst in field_lists:
        if isinstance(lst, str):
            seen[lst] = None
        else:
            for f in lst:
                seen[f] = None
    return ", ".join(seen)


def _format_vector_matching_clause(matching: dict) -> str:
    """Render a PromQL vector-matching modifier the way the source wrote it.

    The parsed ``vector_matching`` dict carries the matcher ``type`` (``Include``
    for ``on(...)``, ``Exclude`` for ``ignoring(...)``) and the join
    ``cardinality`` (``OneToMany`` -> ``group_right()``, ``ManyToOne`` ->
    ``group_left()``). Composing the warning from these fields keeps the message
    faithful to the original expression instead of always saying ``on(...)``
    (issue #65).
    """
    labels = ", ".join(matching.get("labels") or [])
    keyword = "ignoring" if matching.get("type") == "Exclude" else "on"
    clause = f"{keyword}({labels})"
    cardinality = matching.get("cardinality")
    if cardinality == "OneToMany":
        clause += " group_right()"
    elif cardinality == "ManyToOne":
        clause += " group_left()"
    return clause


_GRAFANA_TEMPLATE_VAR_RE = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}"
    r"|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
    r"|\[\[(?P<bracket>[A-Za-z_][A-Za-z0-9_]*)(?::[^\]]+)?\]\]"
)
_RANK_TEMPLATE_LIMIT_RE = re.compile(
    r"\b(?P<func>topk|bottomk)\s*\(\s*"
    r"(?P<token>\$\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}|\$[A-Za-z_][A-Za-z0-9_]*|\[\[[A-Za-z_][A-Za-z0-9_]*(?::[^\]]+)?\]\])"
    r"\s*,",
    re.IGNORECASE,
)
_GROUPING_TEMPLATE_RE = re.compile(r"\b(?:by|without)\s*\((?P<labels>[^)]*)\)", re.IGNORECASE)


def _template_var_name(match) -> str:
    return match.group("braced") or match.group("plain") or match.group("bracket") or "var"


def _template_var_display(name: str) -> str:
    return f"${name}"


def _strip_promql_string_literals(expr: str) -> str:
    text = str(expr or "")
    text = re.sub(r'"(?:\\.|[^"])*"', '""', text)
    return re.sub(r"'(?:\\.|[^'])*'", "''", text)


@dataclass
class TranslationContext:
    promql_expr: str
    data_view: str
    index: str
    rule_pack: RulePackConfig
    resolver: Any = None
    fragment: PromQLFragment | None = None
    panel_type: str = ""
    clean_expr: str = ""
    metric_name: str = ""
    inner_func: str = ""
    range_window: str = ""
    outer_agg: str = ""
    group_labels: list = field(default_factory=list)
    label_filters: list = field(default_factory=list)
    source_type: str = ""
    time_filter: str = ""
    bucket_expr: str = ""
    stats_expr: str = ""
    esql_query: str = ""
    parser_backend: str = ""
    feasibility: str = "feasible"
    confidence: float = 0.0
    output_metric_field: str = ""
    output_group_fields: list = field(default_factory=list)
    translation_complete: bool = False
    metadata: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    datasource_type: str = ""
    datasource_uid: str = ""
    datasource_name: str = ""
    query_language: str = ""
    query_ir: QueryIR | None = None
    target_query_contract: Any = field(default_factory=dict)
    contract_evaluation: Any = field(default_factory=dict)
    fulfillment_plan: Any = field(default_factory=dict)


def _artifact_to_dict(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return dict(value)
    return {}


def _build_metric_contract_artifacts(query_ir, *, resolver=None, rule_pack=None):
    if not query_ir:
        return {}, {}, {}

    metadata = (
        query_ir.get("metadata", {})
        if isinstance(query_ir, dict)
        else getattr(query_ir, "metadata", {})
    ) or {}
    multi_series_metric_fields = []
    for field_name in (metadata.get("multi_series_metric_fields", []) or []):
        normalized = str(field_name or "").strip()
        if normalized and normalized not in multi_series_metric_fields:
            multi_series_metric_fields.append(normalized)

    source_language = str(
        query_ir.get("source_language", "")
        if isinstance(query_ir, dict)
        else getattr(query_ir, "source_language", "")
        or ""
    ).strip().lower()
    family = str(
        query_ir.get("family", "")
        if isinstance(query_ir, dict)
        else getattr(query_ir, "family", "")
        or ""
    ).strip().lower()
    metric_name = str(
        query_ir.get("metric", "")
        if isinstance(query_ir, dict)
        else getattr(query_ir, "metric", "")
        or ""
    ).strip()
    range_function = str(
        query_ir.get("range_function", "")
        if isinstance(query_ir, dict)
        else getattr(query_ir, "range_function", "")
        or ""
    ).strip().lower()
    if source_language != "promql":
        return {}, {}, {}

    if metric_name in _DERIVED_METRIC_NAMES:
        metric_name = ""

    # Prefer real source metric names from the source expression when one is
    # available. This handles two cases:
    #   1. The translator rewrote the panel to a synthetic alias like
    #      `computed_value` or `constant_value` and the IR's `metric` field no
    #      longer points at a real target field.
    #   2. Panel-level fusion populated `multi_series_metric_fields` with the
    #      output column aliases emitted in the ES|QL `STATS` clause (e.g.
    #      `Namespaces`, `Linux_Packets_dropped_receive`); those are not source
    #      field names and should never reach the contract.
    query_ir_dict = (
        query_ir if isinstance(query_ir, dict) else query_ir.to_dict()
    )
    has_source_expression = bool(
        str(query_ir_dict.get("source_expression", "") or "").strip()
        or str(query_ir_dict.get("clean_expression", "") or "").strip()
    )
    if has_source_expression:
        derived_candidates = _metric_candidates(query_ir_dict) - _DERIVED_METRIC_NAMES
        if derived_candidates:
            multi_series_metric_fields = sorted(derived_candidates)

    runtime_capabilities = RuntimeCapabilities(promql=bool((rule_pack or RulePackConfig()).native_promql))
    index_pattern = str(
        query_ir.get("target_index", "")
        if isinstance(query_ir, dict)
        else getattr(query_ir, "target_index", "")
        or ""
    ) or "metrics-*"
    planner_metric_name = metric_name or (multi_series_metric_fields[0] if multi_series_metric_fields else "")
    if family == "native_promql" and runtime_capabilities.promql and not (planner_metric_name or range_function):
        contract = TargetQueryContract(
            canonical_target="promql",
            exactness_class="exact_if_contract_met",
            target_shape={"required_index_patterns": [index_pattern]},
            runtime_requirements={"source_command": "PROMQL"},
            degradation_policy={"fallback": "explicit_only"},
        )
    else:
        if not (planner_metric_name or range_function):
            return {}, {}, {}
        contract = plan_grafana_metric_contract(
            QueryIR(
                source_language=source_language,
                panel_type=str(
                    query_ir.get("panel_type", "")
                    if isinstance(query_ir, dict)
                    else getattr(query_ir, "panel_type", "")
                    or ""
                ),
                metric=planner_metric_name,
                range_function=range_function,
                outer_agg=str(
                    query_ir.get("outer_agg", "")
                    if isinstance(query_ir, dict)
                    else getattr(query_ir, "outer_agg", "")
                    or ""
                ),
                target_index=index_pattern,
            ),
            runtime_capabilities=runtime_capabilities,
        )

    if multi_series_metric_fields:
        field_names = list(multi_series_metric_fields)
    elif planner_metric_name:
        field_names = [planner_metric_name]
    else:
        field_names = []
    if field_names:
        template = (
            contract.field_requirements[0]
            if contract.field_requirements
            else FieldRequirement(name=field_names[0], role="metric")
        )
        resolved_field_names = []
        for field_name in field_names:
            metric_kind = (
                "counter"
                if template.metric_kind and _looks_like_counter_metric(field_name)
                else template.metric_kind
            )
            if resolver is not None and hasattr(resolver, "resolve_metric_field"):
                prefer = "counter" if metric_kind == "counter" else "gauge"
                resolved_name = resolver.resolve_metric_field(field_name, prefer=prefer)
            else:
                resolved_name = field_name
            resolved_field_names.append((resolved_name, metric_kind))
        contract.field_requirements = [
            FieldRequirement(
                name=resolved_name,
                role=template.role,
                type_family=template.type_family,
                metric_kind=metric_kind,
                context=template.context,
            )
            for resolved_name, metric_kind in resolved_field_names
        ]

    field_capabilities = {}
    for requirement in contract.field_requirements:
        if not requirement.name:
            continue
        capability = resolver.field_capability(requirement.name) if resolver else None
        if capability is None and contract.canonical_target == "promql":
            capability = FieldCapability(name=requirement.name)
        if capability is not None:
            field_capabilities[requirement.name] = capability

    if resolver is not None and hasattr(resolver, "concrete_index_candidates"):
        concrete_indexes = list(resolver.concrete_index_candidates() or [])
    else:
        concrete_indexes = []
    # When `field_capabilities` is empty (e.g. every required field is missing
    # from the target), we have no information about the index mode and must
    # not emit a misleading "not all-TSDS" reason on top of the genuine
    # "missing field" reasons. Treat all-TSDS as unknown-but-true in that case
    # so the evaluator stays silent on index mode.
    all_tsds = True
    if len(concrete_indexes) == 1 and field_capabilities:
        all_tsds = all(
            bool(getattr(capability, "time_series_metric_kind", "") or "")
            for capability in field_capabilities.values()
        )
    elif len(concrete_indexes) > 1:
        all_tsds = False
    snapshot = TargetEnvironmentSnapshot(
        target_patterns={
            index_pattern: {
                "all_tsds": all_tsds,
            }
        },
        field_capabilities=field_capabilities,
        runtime_capabilities={
            "PROMQL": runtime_capabilities.promql,
            "TS": True,
            "FROM": True,
            "TBUCKET": True,
            "RATE": True,
            "IRATE": True,
            "INCREASE": True,
        },
    )
    evaluation = evaluate_target_query_contract(contract, snapshot)
    fulfillment = plan_contract_fulfillment(contract, evaluation)
    return contract, evaluation, fulfillment


def _field_is_available(resolver, field_name):
    if not resolver or not field_name:
        return True
    exists = resolver.field_exists(field_name)
    return exists is not False


def _available_fields(resolver, field_names):
    available = []
    for field_name in field_names or []:
        if field_name and _field_is_available(resolver, field_name):
            _append_unique(available, field_name)
    return available


def _resolve_logs_message_field(rule_pack, resolver):
    candidates = [
        rule_pack.logs_message_field,
        "body.text",
        "event.original",
        "log.original",
        "message",
    ]
    available = _available_fields(resolver, candidates)
    if not available:
        return rule_pack.logs_message_field
    if not resolver:
        return available[0]

    preferred = [
        field_name
        for field_name in available
        if resolver.is_text_like_field(field_name)
        and resolver.is_searchable_field(field_name)
        and not resolver.has_conflicting_types(field_name)
    ]
    if preferred:
        return preferred[0]

    searchable = [field_name for field_name in available if resolver.is_searchable_field(field_name)]
    if searchable:
        return searchable[0]

    conflict_free = [field_name for field_name in available if not resolver.has_conflicting_types(field_name)]
    if conflict_free:
        return conflict_free[0]

    return available[0]


@QUERY_PREPROCESSORS.register("template_variable_guardrails", priority=5)
def template_variable_guardrail_rule(context):
    expr = _strip_promql_string_literals(context.promql_expr or "")
    rank_match = _RANK_TEMPLATE_LIMIT_RE.search(expr)
    if rank_match:
        func = rank_match.group("func").lower()
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        _append_unique(
            context.warnings,
            f"{func}() with a Grafana template-variable limit cannot be translated automatically; "
            "top-N time-series requires manual redesign",
        )
        return f"{func} template-variable limit requires manual redesign"

    for grouping_match in _GROUPING_TEMPLATE_RE.finditer(expr):
        var_match = _GRAFANA_TEMPLATE_VAR_RE.search(grouping_match.group("labels") or "")
        if not var_match:
            continue
        var_name = _template_var_name(var_match)
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        _append_unique(
            context.warnings,
            f"BY/WITHOUT clause contains Grafana template variable ({_template_var_display(var_name)}); "
            "grouping dimension is unknown at migration time and requires manual redesign",
        )
        return "grouping template variable requires manual redesign"
    return None


@QUERY_PREPROCESSORS.register("grafana_macros", priority=10)
def grafana_macro_rule(context):
    clean_expr = preprocess_grafana_macros(context.promql_expr, context.rule_pack)
    context.clean_expr = clean_expr
    if clean_expr != context.promql_expr:
        return "expanded Grafana macros"
    return None


@QUERY_PREPROCESSORS.register("parse_fragment", priority=20)
def parse_fragment_rule(context):
    """Parse the cleaned expression into a PromQLFragment."""
    if context.feasibility == "not_feasible":
        return None
    context.fragment = _parse_fragment(context.clean_expr or context.promql_expr)
    parse_error = context.fragment.extra.get("parse_error")
    if parse_error:
        if context.query_language == "logql":
            # Complex LogQL with pipeline stages (| logfmt, | json, etc.) cannot be parsed
            # as PromQL — give a clean actionable message rather than a PromQL parse error.
            _append_unique(context.warnings, "LogQL pipeline stages require manual redesign")
            context.feasibility = "not_feasible"
        else:
            _append_unique(context.warnings, f"AST parse failed ({parse_error}), using regex fragment parser")
    backend = context.fragment.extra.get("parser_backend", "unknown")
    return f"parsed fragment family={context.fragment.family} backend={backend}"


def _or_left_is_feasible(frag):
    """Return True when a binary_expr 'or' fragment should defer to the or-fallback path.

    Reasons on the right operand alone don't block translation — binary_expr_family_rule
    will translate the left operand with a warning. Only block when the left side itself
    carries not_feasible reasons.
    """
    if frag.family != "binary_expr" or (frag.binary_op or "").lower() != "or":
        return False
    left_frag = frag.extra.get("left_frag")
    left_reasons = (left_frag.extra.get("not_feasible_reasons") or []) if left_frag else frag.extra.get("not_feasible_reasons") or []
    return not left_reasons


@QUERY_CLASSIFIERS.register("fragment_guardrails", priority=1)
def fragment_guardrails_rule(context):
    frag = context.fragment
    if not frag:
        return None
    reasons = list(frag.extra.get("not_feasible_reasons", []) or [])
    if not reasons:
        return None
    if _or_left_is_feasible(frag):
        return None  # let binary_expr_family_rule handle the or-fallback
    context.feasibility = "not_feasible"
    context.confidence = 0.0
    for reason in reasons:
        _append_unique(context.warnings, reason)
    return "; ".join(reasons)


@QUERY_CLASSIFIERS.register("family_classifier", priority=5)
def family_classifier_rule(context):
    """Use the parsed fragment family to decide feasibility before pattern-matching."""
    frag = context.fragment
    if not frag:
        return None
    families_that_bypass_patterns = {
        "logql_count",
        "logql_stream",
        "join",
        "uptime",
        "scalar",
        "scaled_agg",
        "nested_agg",
        "binary_expr",
        "topk",
        "label_replace",
    }
    if frag.family in families_that_bypass_patterns:
        nf_reasons = frag.extra.get("not_feasible_reasons") or []
        if nf_reasons:
            if _or_left_is_feasible(frag):
                context.metadata["fragment_family"] = frag.family
                return f"fragment family {frag.family} 'or': right-side reasons deferred to or-fallback"
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            for r in nf_reasons:
                _append_unique(context.warnings, r)
            return f"fragment family {frag.family} has not-feasible reasons: {nf_reasons}"
        context.metadata["fragment_family"] = frag.family
        return f"fragment family {frag.family} bypasses unsupported-pattern check"
    return None


@QUERY_CLASSIFIERS.register("unsupported_patterns", priority=10)
def unsupported_pattern_rule(context):
    if context.metadata.get("fragment_family"):
        return None
    expr = context.clean_expr or context.promql_expr
    complexity, reason = classify_promql_complexity(expr, context.rule_pack)
    if complexity == "not_feasible":
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        _append_unique(context.warnings, reason)
        return reason
    return None


@QUERY_CLASSIFIERS.register("warning_patterns", priority=20)
def warning_pattern_rule(context):
    expr = context.clean_expr or context.promql_expr
    matches = []
    for rule in context.rule_pack.warning_patterns:
        if re.search(rule.pattern, expr, re.IGNORECASE):
            _append_unique(context.warnings, rule.reason)
            matches.append(rule.reason)
    if matches:
        return "; ".join(matches)
    return None


@QUERY_TRANSLATORS.register("scalar_family", priority=1)
def scalar_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "scalar":
        return None
    if frag.is_scalar:
        context.parser_backend = "fragment"
        context.metric_name = "constant_value"
        context.output_metric_field = "constant_value"
        context.esql_query = f"ROW constant_value = {frag.scalar_value}"
        context.translation_complete = True
        return "translated scalar constant"
    return None


@QUERY_TRANSLATORS.register("logql_stream_family", priority=2)
def logql_stream_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "logql_stream":
        return None
    resolver = context.resolver
    rp = context.rule_pack
    raw_labels = [m["label"] for m in frag.matchers]
    selector_fields = resolver.resolve_labels(raw_labels) if resolver else list(raw_labels)
    selector_fields = _available_fields(resolver, selector_fields)
    filters, had_vars = _frag_filters(frag, resolver)
    message_field = _resolve_logs_message_field(rp, resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven LogQL label filters during migration")
    search_expr = _parse_logql_search(frag.raw_expr)
    log_rule_pack = rp
    if message_field != rp.logs_message_field:
        log_rule_pack = RulePackConfig()
        log_rule_pack.__dict__.update(rp.__dict__)
        log_rule_pack.logs_message_field = message_field
        _append_unique(
            context.warnings,
            f"Remapped Loki log message field to `{message_field}` based on the discovered target schema",
        )
    msg_filter = _build_log_message_filter(search_expr, log_rule_pack)
    if msg_filter:
        filters.append(msg_filter)
    elif search_expr:
        _append_unique(context.warnings, "Dropped variable-driven LogQL text filter during migration")

    keep_fields = [rp.logs_timestamp_field]
    for fn in selector_fields:
        _append_unique(keep_fields, fn)
    _append_unique(keep_fields, message_field)

    context.parser_backend = "fragment"
    context.source_type = "FROM"
    context.index = rp.logs_index
    context.metric_name = message_field
    context.output_metric_field = message_field
    context.output_group_fields = [rp.logs_timestamp_field] + selector_fields
    context.esql_query = "\n".join(
        [
            f"FROM {rp.logs_index}",
            f"| WHERE {rp.from_time_filter}",
            *_build_where_lines(filters),
            f"| KEEP {', '.join(keep_fields)}",
            f"| SORT {rp.logs_timestamp_field} DESC",
            f"| LIMIT {int(rp.logs_limit)}",
        ]
    )
    context.translation_complete = True
    _append_unique(context.warnings, "Approximated Loki logs panel as an ES|QL datatable")
    return "translated LogQL logs query"


@QUERY_TRANSLATORS.register("logql_count_family", priority=3)
def logql_count_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "logql_count":
        return None
    resolver = context.resolver
    rp = context.rule_pack
    filters, had_vars = _frag_filters(frag, resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven LogQL label filters during migration")
    search_expr = _parse_logql_search(frag.raw_expr)
    msg_filter = _build_log_message_filter(search_expr, rp)
    if msg_filter:
        filters.append(msg_filter)
    elif search_expr:
        _append_unique(context.warnings, "Dropped variable-driven LogQL text filter during migration")

    context.parser_backend = "fragment"
    context.source_type = "FROM"
    context.index = rp.logs_index
    context.metric_name = "log_count"
    context.output_metric_field = "log_count"
    context.output_group_fields = ["time_bucket"]
    context.esql_query = "\n".join(
        [
            f"FROM {rp.logs_index}",
            f"| WHERE {rp.from_time_filter}",
            *_build_where_lines(filters),
            f"| STATS log_count = COUNT(*) BY {rp.from_bucket}",
            "| SORT time_bucket ASC",
        ]
    )
    context.translation_complete = True
    _append_unique(context.warnings, "Translated LogQL count_over_time using log document counts")
    return "translated LogQL count_over_time"


@QUERY_TRANSLATORS.register("uptime_family", priority=4)
def uptime_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "uptime":
        return None

    resolver = context.resolver
    rp = context.rule_pack
    start_metric = frag.metric
    start_matchers = frag.matchers
    if not start_metric and frag.binary_rhs:
        if isinstance(frag.binary_rhs, PromQLFragment):
            if frag.binary_rhs.family == "join" and frag.extra.get("start_metric"):
                start_metric = frag.extra["start_metric"]
                start_matchers = frag.extra.get("start_matchers", [])
            elif frag.binary_rhs.metric:
                start_metric = frag.binary_rhs.metric
                start_matchers = frag.binary_rhs.matchers
    if not start_metric:
        return None

    filters, had_vars = _frag_filters(PromQLFragment(matchers=start_matchers), resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven label filters during migration")
    group_fields = _frag_group_labels(
        frag,
        resolver,
        context.metadata.get("preferred_group_labels"),
        preferred_origin=context.metadata.get("preferred_group_labels_origin"),
    )
    result_alias = re.sub(r"[^a-zA-Z0-9_]", "_", f"{start_metric}_uptime_seconds")
    physical_metric = _resolve_metric_field(resolver, start_metric, prefer="gauge")

    context.parser_backend = "fragment"
    context.source_type = "FROM"
    context.metric_name = start_metric
    context.output_metric_field = result_alias
    context.output_group_fields = group_fields
    stats_line = f"| STATS start_time_ms = MAX({physical_metric} * 1000)"
    if group_fields:
        stats_line += f" BY {', '.join(group_fields)}"
    context.esql_query = "\n".join(
        [
            f"FROM {context.index}",
            f"| WHERE {rp.from_time_filter}",
            *_build_where_lines(filters),
            f"| WHERE {physical_metric} IS NOT NULL",
            stats_line,
            f'| EVAL {result_alias} = DATE_DIFF("seconds", TO_DATETIME(start_time_ms), NOW())',
            f"| KEEP {_keep(group_fields, result_alias) if group_fields else result_alias}",
        ]
    )
    context.translation_complete = True
    _append_unique(context.warnings, "Approximated time() - metric as uptime from metric timestamp")
    return "translated uptime expression"


def _try_agg_range_info(frag):
    if not frag.range_func:
        return None
    return {
        "outer_agg": frag.outer_agg or "avg",
        "inner_func": frag.range_func,
        "range_window": frag.range_window or "5m",
    }


@QUERY_TRANSLATORS.register("join_family", priority=5)
def join_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "join":
        return None

    resolver = context.resolver
    rp = context.rule_pack
    left_frag = frag.extra.get("left_frag")
    right_frag = frag.extra.get("right_frag")

    if not left_frag or not right_frag:
        return None

    join_labels = resolver.resolve_labels(frag.extra.get("join_labels", [])) if resolver else list(frag.extra.get("join_labels", []))

    if frag.binary_op == "/" and left_frag.range_func and right_frag.range_func:
        left_info = _try_agg_range_info(left_frag)
        right_info = _try_agg_range_info(right_frag)
        if left_info and right_info:
            left_filters, left_had_vars = _frag_filters(left_frag, resolver)
            right_filters, right_had_vars = _frag_filters(right_frag, resolver)
            if left_had_vars or right_had_vars:
                _append_unique(context.warnings, "Dropped variable-driven label filters during migration")
            common_filter_exprs = [f for f in left_filters if f in right_filters]
            common_filters = _build_where_lines(common_filter_exprs)
            left_only = [f for f in left_filters if f not in common_filter_exprs]
            right_only = [f for f in right_filters if f not in common_filter_exprs]

            result_alias = re.sub(r"[^a-zA-Z0-9_]", "_", f"{left_frag.metric}_ratio")
            output_group = ["time_bucket"] + join_labels
            group_by_parts = [rp.ts_bucket] + join_labels
            left_is_counter = resolver.is_counter(left_frag.metric) if resolver else _is_counter_fallback(left_frag.metric, rp)
            right_is_counter = resolver.is_counter(right_frag.metric) if resolver else _is_counter_fallback(right_frag.metric, rp)
            left_inner_func = left_info["inner_func"]
            right_inner_func = right_info["inner_func"]
            left_inner_func, left_counter_warning, left_is_counter = resolve_counter_range_translation(
                left_frag.range_func, left_frag.metric, left_is_counter, resolver, left_inner_func
            )
            if left_counter_warning:
                _append_unique(context.warnings, left_counter_warning)
            right_inner_func, right_counter_warning, right_is_counter = resolve_counter_range_translation(
                right_frag.range_func, right_frag.metric, right_is_counter, resolver, right_inner_func
            )
            if right_counter_warning:
                _append_unique(context.warnings, right_counter_warning)
            left_prefer = "counter" if left_frag.range_func in {"rate", "irate", "increase"} and left_is_counter else "gauge"
            right_prefer = "counter" if right_frag.range_func in {"rate", "irate", "increase"} and right_is_counter else "gauge"
            left_metric_field = _resolve_metric_field(resolver, left_frag.metric, prefer=left_prefer)
            right_metric_field = _resolve_metric_field(resolver, right_frag.metric, prefer=right_prefer)

            left_stats_call = _build_stats_call(left_info["outer_agg"], left_inner_func, left_metric_field, left_info["range_window"])
            right_stats_call = _build_stats_call(right_info["outer_agg"], right_inner_func, right_metric_field, right_info["range_window"])
            # Apply per-side exclusive filters via CASE() so that label
            # selectors which appear on only one operand (e.g. mode="user" on
            # the numerator) are not silently dropped.
            if left_only:
                inlined = _inline_filters_into_stats_expr(left_stats_call, left_only)
                if inlined:
                    left_stats_call = inlined
                else:
                    _append_unique(context.warnings, f"Numerator-only filter(s) could not be inlined and were dropped: {left_only}")
            if right_only:
                inlined = _inline_filters_into_stats_expr(right_stats_call, right_only)
                if inlined:
                    right_stats_call = inlined
                else:
                    _append_unique(context.warnings, f"Denominator-only filter(s) could not be inlined and were dropped: {right_only}")

            context.parser_backend = "fragment"
            context.source_type = "TS"
            context.metric_name = left_frag.metric
            context.output_metric_field = result_alias
            context.output_group_fields = output_group
            context.esql_query = "\n".join(
                [
                    f"TS {context.index}",
                    f"| WHERE {rp.ts_time_filter}",
                    *common_filters,
                    f"| STATS numerator = {left_stats_call}, denominator = {right_stats_call} BY {', '.join(group_by_parts)}",
                    f"| EVAL {result_alias} = numerator / denominator",
                    f"| KEEP {_keep(output_group, result_alias)}",
                    "| SORT time_bucket ASC",
                ]
            )
            context.translation_complete = True
            _append_unique(context.warnings, "Approximated PromQL join ratio as same-bucket ES|QL ratio")
            return "translated join ratio expression"

    if frag.binary_op == "*":
        filters, had_vars = _frag_filters(left_frag, resolver)
        if had_vars:
            _append_unique(context.warnings, "Dropped variable-driven label filters during migration")
        metric_name = left_frag.metric or frag.metric

        if not metric_name and left_frag.family == "binary_expr":
            # Left side is itself a binary_expr (e.g. A-B); no single metric_name.
            # Delegate to _build_formula_plan so the arithmetic is handled normally
            # while the join RHS (label enrichment) is still stripped.
            plan = _build_formula_plan(
                left_frag,
                resolver,
                rp,
                summary_mode=_summary_mode_from_metadata(context.metadata),
                preferred_group_labels=context.metadata.get("preferred_group_labels"),
                preferred_group_labels_origin=context.metadata.get("preferred_group_labels_origin"),
            )
            if plan and plan.specs:
                shared = _build_shared_measure_pipeline(context.index, plan.specs)
                if shared:
                    parts, output_group_fields, _ = shared
                    result_alias = "computed_value"
                    parts.append(f"| EVAL {result_alias} = {plan.expr}")
                    parts.append(f"| KEEP {_keep(output_group_fields, result_alias)}")
                    if "time_bucket" in output_group_fields:
                        parts.append("| SORT time_bucket ASC")
                    for warning in plan.warnings:
                        _append_unique(context.warnings, warning)
                    _append_unique(context.warnings, "Dropped group_left label enrichment; kept primary metric series only")
                    _append_unique(context.warnings, "Approximated PromQL arithmetic using same-bucket ES|QL math")
                    context.parser_backend = "fragment"
                    context.source_type = plan.specs[0].source_type
                    context.metric_name = result_alias
                    context.output_metric_field = result_alias
                    context.output_group_fields = output_group_fields
                    context.esql_query = "\n".join(parts)
                    context.translation_complete = True
                    return "translated label enrichment join over binary_expr lhs"
            return None

        metric_alias = re.sub(r"[^a-zA-Z0-9_]", "_", metric_name)
        preferred_group_labels = (
            resolver.resolve_labels(context.metadata.get("preferred_group_labels", []))
            if resolver
            else list(context.metadata.get("preferred_group_labels", []))
        )
        group_fields = list(preferred_group_labels or join_labels)
        output_group = ["time_bucket"] + group_fields if group_fields else ["time_bucket"]
        default_agg = rp.default_gauge_agg.upper()
        is_counter = resolver.is_counter(metric_name) if resolver else _is_counter_fallback(metric_name, rp)
        source = "TS" if is_counter else "FROM"
        time_filter = rp.ts_time_filter if is_counter else rp.from_time_filter
        bucket = rp.ts_bucket if is_counter else rp.from_bucket
        physical_metric = _resolve_metric_field(
            resolver, metric_name, prefer="counter" if is_counter else "gauge"
        )

        context.parser_backend = "fragment"
        context.source_type = source
        context.metric_name = metric_name
        context.output_metric_field = metric_alias
        context.output_group_fields = output_group
        by_clause = bucket + (f", {', '.join(group_fields)}" if group_fields else "")
        context.esql_query = "\n".join(
            [
                f"{source} {context.index}",
                f"| WHERE {time_filter}",
                *_build_where_lines(filters),
                f"| WHERE {physical_metric} IS NOT NULL",
                f"| STATS {metric_alias} = {default_agg}({physical_metric}) BY {by_clause}",
                "| SORT time_bucket ASC",
            ]
        )
        context.translation_complete = True
        _append_unique(context.warnings, "Dropped group_left label enrichment; kept primary metric series only")
        return "translated label enrichment join"

    matching = frag.extra.get("vector_matching") or {}
    has_explicit_on = bool(matching.get("labels"))
    is_additive_join = frag.binary_op in {"+", "-"}

    if is_additive_join and has_explicit_on and right_frag.metric and left_frag.metric != right_frag.metric:
        context.feasibility = "not_feasible"
        match_clause = _format_vector_matching_clause(matching)
        _append_unique(
            context.warnings,
            f"Cross-metric {frag.binary_op} {match_clause} join cannot be accurately represented in ES|QL",
        )
        return "join requires both sides — marked not_feasible"

    if left_frag.metric:
        filters, had_vars = _frag_filters(left_frag, resolver)
        if had_vars:
            _append_unique(context.warnings, "Dropped variable-driven label filters during migration")
        metric_alias = re.sub(r"[^a-zA-Z0-9_]", "_", left_frag.metric)
        output_group = ["time_bucket"] + join_labels if join_labels else ["time_bucket"]
        is_counter = resolver.is_counter(left_frag.metric) if resolver else _is_counter_fallback(left_frag.metric, rp)
        source = "TS" if (is_counter or left_frag.range_func in AGG_FUNCTION_MAP) else "FROM"
        time_filter = rp.ts_time_filter if source == "TS" else rp.from_time_filter
        bucket = rp.ts_bucket if source == "TS" else rp.from_bucket
        if left_frag.range_func in {"rate", "irate", "increase"} or is_counter:
            prefer = "counter"
        else:
            prefer = "gauge"
        physical_metric = _resolve_metric_field(resolver, left_frag.metric, prefer=prefer)

        if left_frag.range_func and left_frag.range_func in AGG_FUNCTION_MAP:
            esql_inner = AGG_FUNCTION_MAP[left_frag.range_func]
            w = left_frag.range_window or rp.default_rate_window
            # Same gauge-fallback story as range_agg_family_rule: emitting
            # RATE/IRATE/INCREASE on a gauge-typed field hard-fails. Counter-only
            # rate()/irate() keep their true form unless the rule pack pins gauge.
            esql_inner, counter_warning, is_counter = resolve_counter_range_translation(
                left_frag.range_func, left_frag.metric, is_counter, resolver, esql_inner
            )
            if counter_warning:
                _append_unique(context.warnings, counter_warning)
            inner_expr = f"{esql_inner}({physical_metric}, {w})"
        elif is_counter:
            inner_expr = f"RATE({physical_metric}, {rp.default_rate_window})"
        else:
            inner_expr = physical_metric

        outer = OUTER_AGG_MAP.get(left_frag.outer_agg or "avg", "AVG")
        stats_expr = f"{outer}({inner_expr})"
        by_clause = bucket + (f", {', '.join(join_labels)}" if join_labels else "")

        context.parser_backend = "fragment"
        context.source_type = source
        context.metric_name = left_frag.metric
        context.output_metric_field = metric_alias
        context.output_group_fields = output_group
        context.esql_query = "\n".join(
            [
                f"{source} {context.index}",
                f"| WHERE {time_filter}",
                *_build_where_lines(filters),
                f"| STATS {metric_alias} = {stats_expr} BY {by_clause}",
                "| SORT time_bucket ASC",
            ]
        )
        context.translation_complete = True
        _append_unique(context.warnings, "Approximated join expression using left side only")
        return "translated join (left-side fallback)"

    return None


@QUERY_TRANSLATORS.register("binary_expr_family", priority=6)
def binary_expr_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "binary_expr":
        return None

    resolver = context.resolver
    rp = context.rule_pack
    plan = _build_formula_plan(
        frag,
        resolver,
        rp,
        summary_mode=_summary_mode_from_metadata(context.metadata),
        preferred_group_labels=context.metadata.get("preferred_group_labels"),
        preferred_group_labels_origin=context.metadata.get("preferred_group_labels_origin"),
    )
    if not plan:
        # ``or`` between distinct metrics: translate the left operand alone.
        # This covers the common "primary or fallback" / "metric or vector(0)"
        # PromQL idioms where the left side is the meaningful signal.
        op_lower = (frag.binary_op or "").lower()
        if op_lower == "or":
            left_frag = frag.extra.get("left_frag")
            if left_frag and not left_frag.extra.get("not_feasible_reasons"):
                sub = TranslationContext(
                    promql_expr=left_frag.raw_expr or context.promql_expr,
                    data_view=context.data_view,
                    index=context.index,
                    rule_pack=context.rule_pack,
                    resolver=context.resolver,
                    metadata=dict(context.metadata),
                )
                sub.fragment = left_frag
                sub.metadata["fragment_family"] = left_frag.family
                QUERY_TRANSLATORS.apply(sub, stop_when=lambda ctx, _: ctx.translation_complete)
                QUERY_POSTPROCESSORS.apply(sub)
                if sub.esql_query and sub.feasibility != "not_feasible":
                    context.esql_query = sub.esql_query
                    context.metric_name = sub.metric_name
                    context.output_metric_field = sub.output_metric_field
                    context.output_group_fields = sub.output_group_fields
                    context.source_type = sub.source_type
                    context.parser_backend = "fragment"
                    context.translation_complete = True
                    for w in sub.warnings:
                        _append_unique(context.warnings, w)
                    _append_unique(
                        context.warnings,
                        "PromQL 'or' fallback: using left operand only; "
                        "right-hand side metric ignored",
                    )
                    return "or fallback: translated left operand"

        # ``and`` / ``unless`` and unresolvable ``or`` have no honest single-stage
        # ES|QL equivalent. Surface a clear ``not_feasible``.
        if op_lower in {"or", "and", "unless"}:
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            context.translation_complete = True
            _append_unique(
                context.warnings,
                f"PromQL set operator '{op_lower}' between distinct metrics or aggregations "
                "has no honest ES|QL translation; marked not_feasible",
            )
            return "set operator not feasible"
        return None

    if plan.specs:
        shared = _build_shared_measure_pipeline(context.index, plan.specs)
        if not shared:
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            context.translation_complete = True
            _append_unique(
                context.warnings,
                "PromQL arithmetic with divergent filters/groupings cannot be translated safely yet",
            )
            return "binary expression requires unsafe measure merge; marked not_feasible"
        else:
            parts, output_group_fields, _ = shared
            result_alias = "computed_value"
            parts.append(f"| EVAL {result_alias} = {plan.expr}")
            context.source_type = plan.specs[0].source_type
            collapsed = None
            if _summary_mode_from_metadata(context.metadata):
                collapsed = _collapse_summary_ts_query(parts, output_group_fields, [result_alias])
            if collapsed is None:
                parts.append(f"| KEEP {_keep(output_group_fields, result_alias)}")
                if "time_bucket" in output_group_fields:
                    parts.append("| SORT time_bucket ASC")
            else:
                output_group_fields = collapsed
        context.output_group_fields = output_group_fields
        _append_unique(context.warnings, "Approximated PromQL arithmetic using same-bucket ES|QL math")
    else:
        result_alias = "computed_value"
        parts = [f"ROW {result_alias} = {plan.expr}"]
        context.source_type = "ROW"
        context.output_group_fields = []

    if frag.extra.get("stripped_join"):
        _append_unique(context.warnings, "Dropped group_left label enrichment; kept primary metric series only")
    for warning in plan.warnings:
        _append_unique(context.warnings, warning)
    non_time_groups = [field for field in context.output_group_fields if field != "time_bucket"]
    if plan.specs and not non_time_groups:
        _append_unique(
            context.warnings,
            "PromQL series labels were not retained; output is bucket-level and may collapse multiple source series",
        )

    context.parser_backend = "fragment"
    context.metric_name = result_alias
    context.output_metric_field = result_alias
    context.esql_query = "\n".join(parts)
    context.translation_complete = True
    return "translated arithmetic expression"


@QUERY_TRANSLATORS.register("topk_family", priority=6)
def topk_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "topk":
        return None
    if not frag.metric:
        return None

    resolver = context.resolver
    rp = context.rule_pack
    filters, had_vars = _frag_filters(frag, resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven label filters during migration")
    group_fields = _frag_group_labels(
        frag,
        resolver,
        context.metadata.get("preferred_group_labels"),
        preferred_origin=context.metadata.get("preferred_group_labels_origin"),
    )
    source = "TS" if frag.range_func in AGG_FUNCTION_MAP else "FROM"
    time_filter = rp.ts_time_filter if source == "TS" else rp.from_time_filter
    bucket = rp.ts_bucket if source == "TS" else rp.from_bucket
    inner_func = frag.range_func or ("rate" if _is_counter_fallback(frag.metric, rp) else "")
    if inner_func in {"rate", "irate", "increase"}:
        prefer = "counter"
    else:
        prefer = "gauge"
    physical_metric = _resolve_metric_field(resolver, frag.metric, prefer=prefer)
    if inner_func:
        stats_expr = _build_stats_call(
            frag.outer_agg or "avg",
            inner_func,
            physical_metric,
            frag.range_window or rp.default_rate_window,
        )
    else:
        stats_expr = f"{OUTER_AGG_MAP.get(frag.outer_agg or 'avg', 'AVG')}({physical_metric})"
    limit = int(frag.extra.get("topk_limit") or 10)

    context.parser_backend = "fragment"
    context.source_type = source
    context.metric_name = frag.metric
    context.output_metric_field = "value"

    if not group_fields:
        # No labels available — single-bucket top N (useful for stat panels)
        context.output_group_fields = []
        context.esql_query = "\n".join(
            [
                f"{source} {context.index}",
                f"| WHERE {time_filter}",
                *_build_where_lines(filters),
                f"| WHERE {physical_metric} IS NOT NULL",
                f"| STATS _bucket_value = {stats_expr} BY {bucket}",
                "| SORT time_bucket ASC",
                "| STATS value = LAST(_bucket_value, time_bucket)",
                "| SORT value DESC",
                f"| LIMIT {limit}",
            ]
        )
        context.translation_complete = True
        _append_unique(
            context.warnings,
            "topk() without group labels: collapsed to single-series top N; "
            "add preferred_group_labels hint for per-series breakdown",
        )
        return "translated ungrouped topk as single-bucket top N"

    context.output_group_fields = group_fields
    context.esql_query = "\n".join(
        [
            f"{source} {context.index}",
            f"| WHERE {time_filter}",
            *_build_where_lines(filters),
            f"| WHERE {physical_metric} IS NOT NULL",
            f"| STATS _bucket_value = {stats_expr} BY {bucket}, {', '.join(group_fields)}",
            "| SORT time_bucket ASC",
            f"| STATS value = LAST(_bucket_value, time_bucket) BY {', '.join(group_fields)}",
            f"| KEEP {', '.join(group_fields + ['value'])}",
            "| SORT value DESC",
            f"| LIMIT {limit}",
        ]
    )
    context.translation_complete = True
    _append_unique(context.warnings, "Translated grouped topk() as latest-bucket ES|QL top N")
    return "translated grouped topk expression"


# Characters that carry special meaning in a regex. If a label_replace regex's
# literal (non-capture) portion contains any of these, it is not safe to splice
# verbatim into a GROK pattern, so we degrade gracefully instead of guessing.
_REGEX_META_CHARS = set(r".^$*+?()[]{}|\\")


def _grok_escape_literal(literal: str) -> str | None:
    """Escape a literal regex fragment for inclusion in a GROK pattern.

    Returns ``None`` if the fragment contains regex metacharacters that cannot be
    represented as a plain GROK literal (so the caller can degrade gracefully).
    GROK only treats ``%`` (start of ``%{...}``) specially among ordinary text,
    so a fragment with no regex metacharacters is GROK-literal-safe once any
    ``%`` is escaped.
    """
    if any(ch in _REGEX_META_CHARS for ch in literal):
        return None
    # Escape characters that GROK's Oniguruma layer would otherwise interpret.
    return literal.replace("%", "\\%")


def _build_label_replace_grok(dst, src, regex):
    """Translate a single-capture label_replace regex to an anchored GROK command.

    PromQL ``label_replace`` matches the *entire* source label value against the
    regex (it is fully anchored) and ``$1`` extracts the first capture group.
    ES|QL has no inline regex-extract function, so we use a fully anchored
    ``GROK`` command. Only patterns of the form ``<literal>(.*)<literal>`` (with
    literal portions free of regex metacharacters) are translated; anything else
    returns ``None`` so the caller degrades gracefully rather than emitting a
    semantically wrong extraction.
    """
    # Exactly one greedy capture group, surrounded by optional literal text.
    match = re.fullmatch(r"(?P<pre>[^()]*)\((?:\.\*)\)(?P<post>[^()]*)", regex)
    if not match:
        return None
    pre = _grok_escape_literal(match.group("pre"))
    post = _grok_escape_literal(match.group("post"))
    if pre is None or post is None:
        return None
    pattern = f"^{pre}%{{GREEDYDATA:{dst}}}{post}$"
    return f'| GROK {src} "{pattern}"'


def _build_label_replace_eval(dst, replacement, src, regex):
    """Return an ES|QL clause for label_replace(), or None if untranslatable."""
    # Case 1: full copy — replacement captures everything unchanged
    if replacement in ("$1", "$0") and regex in ("(.*)", ".*", "(.+)", ".+"):
        return f"| EVAL {dst} = {src}"
    # Case 2: constant string — no $N capture group references
    if not re.search(r"\$\d+", replacement):
        safe = replacement.replace('"', '\\"')
        return f'| EVAL {dst} = "{safe}"'
    # Case 3: single capture group substitution via anchored GROK (ES|QL has no
    # inline regex-extract function). Only safe literal-bounded patterns qualify.
    if replacement == "$1":
        return _build_label_replace_grok(dst, src, regex)
    # Complex multi-group: cannot translate cleanly
    return None


def _label_replace_needs_source_label(replacement: str) -> bool:
    return "$" in str(replacement or "")


@QUERY_TRANSLATORS.register("label_replace_family", priority=6)
def label_replace_family_rule(context):
    """Translate label_replace(v, dst, replacement, src, regex) via ES|QL EVAL."""
    frag = context.fragment
    if not frag or frag.family != "label_replace":
        return None

    inner_frag = frag.extra.get("lr_inner_frag")
    if not inner_frag:
        return None

    dst = frag.extra.get("lr_dst", "")
    replacement = frag.extra.get("lr_replacement", "")
    src = frag.extra.get("lr_src", "")
    regex = frag.extra.get("lr_regex", "")
    resolved_src = context.resolver.resolve_label(src) if (src and context.resolver) else src

    # Translate the inner metric expression via a sub-context
    sub_metadata = dict(context.metadata)
    if resolved_src and _label_replace_needs_source_label(str(replacement)):
        preferred = list(sub_metadata.get("preferred_group_labels") or [])
        if src not in preferred and resolved_src not in preferred:
            preferred.append(src)
        sub_metadata["preferred_group_labels"] = preferred
    sub = TranslationContext(
        promql_expr=inner_frag.raw_expr or context.promql_expr,
        data_view=context.data_view,
        index=context.index,
        rule_pack=context.rule_pack,
        resolver=context.resolver,
        metadata=sub_metadata,
    )
    sub.fragment = inner_frag
    sub.metadata["fragment_family"] = inner_frag.family
    QUERY_TRANSLATORS.apply(sub, stop_when=lambda ctx, _: ctx.translation_complete)
    QUERY_POSTPROCESSORS.apply(sub)

    if not sub.esql_query or sub.feasibility == "not_feasible":
        return None  # fall through to not_feasible

    eval_clause = _build_label_replace_eval(dst, replacement, resolved_src, regex)
    lines = sub.esql_query.splitlines()
    if eval_clause:
        sort_idx = next(
            (i for i, ln in enumerate(lines) if ln.strip().startswith("| SORT")),
            len(lines),
        )
        lines.insert(sort_idx, eval_clause)
        if eval_clause.lstrip().startswith("| GROK"):
            warning = (
                f"label_replace({dst!r}) approximated with an anchored ES|QL GROK; "
                "rows where the regex does not match will produce null (PromQL preserves the original value)"
            )
        else:
            warning = f"label_replace({dst!r}) approximated with ES|QL EVAL"
        _append_unique(context.warnings, warning)
    else:
        _append_unique(
            context.warnings,
            f"label_replace(): complex replacement pattern not translatable; "
            f"label renaming for {dst!r} skipped",
        )

    for w in sub.warnings:
        _append_unique(context.warnings, w)

    context.esql_query = "\n".join(lines)
    context.metric_name = sub.metric_name
    context.output_metric_field = sub.output_metric_field
    context.output_group_fields = sub.output_group_fields
    context.source_type = sub.source_type
    context.parser_backend = "fragment"
    context.translation_complete = True
    return f"translated label_replace({dst!r})"


@QUERY_TRANSLATORS.register("scaled_agg_family", priority=6)
def scaled_agg_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "scaled_agg":
        return None
    if not frag.metric or not frag.range_func:
        return None

    resolver = context.resolver
    rp = context.rule_pack
    filters, had_vars = _frag_filters(frag, resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven label filters during migration")

    group_fields = _frag_group_labels(
        frag,
        resolver,
        context.metadata.get("preferred_group_labels"),
        preferred_origin=context.metadata.get("preferred_group_labels_origin"),
    )
    alias = re.sub(r"[^a-zA-Z0-9_]", "_", frag.metric)
    bucket = rp.ts_bucket
    group_by_parts, output_group = _grouping_parts(bucket, group_fields)

    esql_outer = OUTER_AGG_MAP.get(frag.outer_agg, "AVG")
    esql_inner = AGG_FUNCTION_MAP.get(frag.range_func, frag.range_func.upper())
    eval_line, final_alias = _frag_eval_line(alias, frag)
    is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rp)
    esql_inner, counter_warning, is_counter = resolve_counter_range_translation(
        frag.range_func, frag.metric, is_counter, resolver, esql_inner
    )
    if counter_warning:
        _append_unique(context.warnings, counter_warning)
    prefer = "counter" if (frag.range_func in {"rate", "irate", "increase"} and is_counter) else "gauge"
    physical_metric = _resolve_metric_field(resolver, frag.metric, prefer=prefer)

    context.parser_backend = "fragment"
    context.source_type = "TS"
    context.metric_name = frag.metric
    context.output_metric_field = final_alias
    context.output_group_fields = output_group
    parts = [
        f"TS {context.index}",
        f"| WHERE {rp.ts_time_filter}",
        *_build_where_lines(filters),
        f"| WHERE {physical_metric} IS NOT NULL",
    ]
    stats_line = f"| STATS {alias} = {esql_outer}({esql_inner}({physical_metric}, {frag.range_window}))"
    if group_by_parts:
        stats_line += f" BY {', '.join(group_by_parts)}"
    parts.append(stats_line)
    if eval_line:
        parts.append(eval_line)
    collapsed = None
    if _summary_mode_from_metadata(context.metadata):
        collapsed = _collapse_summary_ts_query(parts, context.output_group_fields, [final_alias])
    if collapsed is None:
        if eval_line:
            parts.append(f"| KEEP {_keep(context.output_group_fields, final_alias)}")
        if "time_bucket" in context.output_group_fields:
            parts.append("| SORT time_bucket ASC")
    else:
        context.output_group_fields = collapsed
    context.esql_query = "\n".join(parts)
    context.translation_complete = True
    return "translated scaled aggregation expression"


@QUERY_TRANSLATORS.register("nested_agg_family", priority=7)
def nested_agg_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "nested_agg":
        return None
    if not frag.metric:
        return None

    resolver = context.resolver
    rp = context.rule_pack
    filters, had_vars = _frag_filters(frag, resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven label filters during migration")

    inner_group = resolver.resolve_labels(frag.extra.get("inner_group", [])) if resolver else list(frag.extra.get("inner_group", []))
    if not inner_group:
        inner_group = resolver.resolve_labels(context.metadata.get("preferred_group_labels", [])) if resolver else list(context.metadata.get("preferred_group_labels", []))
    result_alias = re.sub(r"[^a-zA-Z0-9_]", "_", f"{frag.metric}_{frag.outer_agg}")
    esql_outer = OUTER_AGG_MAP.get(frag.outer_agg, "COUNT")
    inner_agg_name = frag.extra.get("inner_agg", "count")
    esql_inner_agg = OUTER_AGG_MAP.get(inner_agg_name, "COUNT")
    inner_alias = "inner_val"
    physical_metric = _resolve_metric_field(resolver, frag.metric, prefer="gauge")
    count_presence_filter = f"| WHERE {physical_metric} IS NOT NULL" if inner_agg_name == "count" else ""
    metric_like_panels = {"stat", "singlestat", "gauge", "bargauge"}

    if frag.outer_agg == "count" and inner_agg_name == "count" and len(inner_group) == 1:
        count_field = inner_group[0]
        lines = [
            f"FROM {context.index}",
            f"| WHERE {rp.from_time_filter}",
            *_build_where_lines(filters),
        ]
        if count_presence_filter:
            lines.append(count_presence_filter)
        if _summary_mode_from_metadata(context.metadata) or context.panel_type in metric_like_panels:
            context.output_group_fields = []
            lines.append(f"| STATS {result_alias} = COUNT_DISTINCT({count_field})")
        else:
            context.output_group_fields = ["time_bucket"]
            lines.append(f"| STATS {result_alias} = COUNT_DISTINCT({count_field}) BY {rp.from_bucket}")
            lines.append("| SORT time_bucket ASC")
        context.esql_query = "\n".join(lines)
        _append_unique(context.warnings, f"Approximated nested count(count()) as COUNT_DISTINCT({count_field})")
        context.parser_backend = "fragment"
        context.source_type = "FROM"
        context.metric_name = result_alias
        context.output_metric_field = result_alias
        context.translation_complete = True
        return "translated nested count(count()) expression"

    if frag.range_func in AGG_FUNCTION_MAP:
        esql_inner_name = AGG_FUNCTION_MAP[frag.range_func]
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rp)
        esql_inner_name, counter_warning, is_counter = resolve_counter_range_translation(
            frag.range_func, frag.metric, is_counter, resolver, esql_inner_name
        )
        if counter_warning:
            _append_unique(context.warnings, counter_warning)
        prefer = "counter" if (frag.range_func in {"rate", "irate", "increase"} and is_counter) else "gauge"
        physical_metric = _resolve_metric_field(resolver, frag.metric, prefer=prefer)
        first_stats_expr = f"{inner_alias} = {esql_inner_agg}({esql_inner_name}({physical_metric}, {frag.range_window}))"
        first_stats_by = (
            f"{rp.ts_bucket}, {', '.join(inner_group)}"
            if inner_group
            else rp.ts_bucket
        )
        context.output_group_fields = ["time_bucket"]
        context.esql_query = "\n".join(
            [
                f"TS {context.index}",
                f"| WHERE {rp.ts_time_filter}",
                *_build_where_lines(filters),
                f"| WHERE {physical_metric} IS NOT NULL",
                f"| STATS {first_stats_expr} BY {first_stats_by}",
                f"| STATS {result_alias} = {esql_outer}({inner_alias}) BY time_bucket",
                "| SORT time_bucket ASC",
            ]
        )
        context.parser_backend = "fragment"
        context.source_type = "TS"
        context.metric_name = result_alias
        context.output_metric_field = result_alias
        context.translation_complete = True
        return f"translated nested {frag.outer_agg} over {frag.range_func} expression"

    first_stats_expr = (
        f"{inner_alias} = {esql_inner_agg}({physical_metric})"
        if inner_agg_name != "count"
        else f"{inner_alias} = COUNT(*)"
    )
    second_stats_arg = inner_alias
    if _summary_mode_from_metadata(context.metadata) or context.panel_type in metric_like_panels:
        context.output_group_fields = []
        summary_lines = [
            f"FROM {context.index}",
            f"| WHERE {rp.from_time_filter}",
            *_build_where_lines(filters),
        ]
        if count_presence_filter:
            summary_lines.append(count_presence_filter)
        if inner_group:
            summary_lines.append(f"| STATS {first_stats_expr} BY {', '.join(inner_group)}")
        else:
            summary_lines.append(f"| STATS {first_stats_expr}")
        summary_lines.append(f"| STATS {result_alias} = {esql_outer}({second_stats_arg})")
        context.esql_query = "\n".join(summary_lines)
    else:
        context.output_group_fields = ["time_bucket"]
        first_stats_by = (
            f"{rp.from_bucket}, {', '.join(inner_group)}"
            if inner_group
            else rp.from_bucket
        )
        context.esql_query = "\n".join(
            [
                f"FROM {context.index}",
                f"| WHERE {rp.from_time_filter}",
                *_build_where_lines(filters),
                *( [count_presence_filter] if count_presence_filter else [] ),
                f"| STATS {first_stats_expr} BY {first_stats_by}",
                f"| STATS {result_alias} = {esql_outer}({second_stats_arg}) BY time_bucket",
                "| SORT time_bucket ASC",
            ]
        )

    context.parser_backend = "fragment"
    context.source_type = "FROM"
    context.metric_name = result_alias
    context.output_metric_field = result_alias
    context.translation_complete = True
    return f"translated nested {frag.outer_agg} expression"


@QUERY_TRANSLATORS.register("range_agg_family", priority=8)
def range_agg_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "range_agg":
        return None
    if not frag.metric or not frag.range_func:
        return None

    resolver = context.resolver
    rp = context.rule_pack
    filters, had_vars = _frag_filters(frag, resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven label filters during migration")

    group_fields = _frag_group_labels(
        frag,
        resolver,
        context.metadata.get("preferred_group_labels"),
        preferred_origin=context.metadata.get("preferred_group_labels_origin"),
    )
    esql_inner_name = AGG_FUNCTION_MAP.get(frag.range_func)
    if not esql_inner_name:
        return None

    is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rp)
    # ES|QL's RATE / IRATE / INCREASE require a ``counter_*`` typed
    # field; emitting them against a gauge-typed field hard-fails with
    # ``first argument of [RATE(...)] must be counter``. The shared policy in
    # resolve_counter_range_translation decides whether to degrade to a gauge
    # analogue (warned) or keep the source-faithful counter form (warned when
    # live caps disagree).
    esql_inner_name, counter_warning, is_counter = resolve_counter_range_translation(
        frag.range_func, frag.metric, is_counter, resolver, esql_inner_name
    )
    if counter_warning:
        _append_unique(context.warnings, counter_warning)
    needs_ts = is_counter or frag.range_func in AGG_FUNCTION_MAP
    source = "TS" if needs_ts else "FROM"
    time_filter = rp.ts_time_filter if source == "TS" else rp.from_time_filter
    bucket = rp.ts_bucket if source == "TS" else rp.from_bucket
    prefer = "counter" if (frag.range_func in {"rate", "irate", "increase"} and is_counter) else "gauge"
    physical_metric = _resolve_metric_field(resolver, frag.metric, prefer=prefer)

    inner_expr = f"{esql_inner_name}({physical_metric}, {frag.range_window})"
    outer = OUTER_AGG_MAP.get(frag.outer_agg, "") if frag.outer_agg else ""
    if not outer and source == "TS" and group_fields:
        stats_expr = f"AVG({inner_expr})"
        _append_unique(
            context.warnings,
            f"Added outer AVG() around {frag.range_func} because ES|QL requires an outer aggregation "
            "when grouping TS functions by label fields",
        )
    else:
        stats_expr = f"{outer}({inner_expr})" if outer else inner_expr

    alias = re.sub(r"[^a-zA-Z0-9_]", "_", frag.metric)
    group_by_parts, output_group = _grouping_parts(bucket, group_fields)
    eval_line, final_alias = _frag_eval_line(alias, frag)

    context.parser_backend = "fragment"
    context.source_type = source
    context.metric_name = frag.metric
    context.output_metric_field = final_alias
    context.output_group_fields = output_group
    parts = [
        f"{source} {context.index}",
        f"| WHERE {time_filter}",
        *_build_where_lines(filters),
        f"| WHERE {physical_metric} IS NOT NULL",
    ]
    stats_line = f"| STATS {alias} = {stats_expr}"
    if group_by_parts:
        stats_line += f" BY {', '.join(group_by_parts)}"
    parts.append(stats_line)
    if eval_line:
        parts.append(eval_line)
    collapsed = None
    if _summary_mode_from_metadata(context.metadata):
        collapsed = _collapse_summary_ts_query(parts, output_group, [final_alias])
    if collapsed is None:
        if eval_line:
            parts.append(f"| KEEP {_keep(output_group, final_alias)}")
        if "time_bucket" in output_group:
            parts.append("| SORT time_bucket ASC")
    else:
        output_group = collapsed
    context.esql_query = "\n".join(parts)
    context.translation_complete = True
    context.output_group_fields = output_group
    return "translated range aggregation expression"


def _agg_stats_expr(outer, inner_expr, frag):
    """Render an aggregation call, special-casing quantile -> PERCENTILE(expr, phi*100).

    PromQL quantile(phi, m) is the phi-quantile across the grouped series, which is
    exactly ES|QL PERCENTILE(m, phi*100). All other aggregations are AGG(expr).
    """
    if frag is not None and frag.outer_agg == "quantile":
        phi = frag.extra.get("quantile_phi")
        if phi is not None:
            return f"PERCENTILE({inner_expr}, {_format_scalar_value(phi * 100)})"
    return f"{outer}({inner_expr})"


@QUERY_TRANSLATORS.register("simple_agg_family", priority=9)
def simple_agg_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "simple_agg":
        return None
    if not frag.metric:
        return None

    resolver = context.resolver
    rp = context.rule_pack
    filters, had_vars = _frag_filters(frag, resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven label filters during migration")

    group_fields = _frag_group_labels(
        frag,
        resolver,
        context.metadata.get("preferred_group_labels"),
        preferred_origin=context.metadata.get("preferred_group_labels_origin"),
    )
    is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rp)
    pre_agg_filter = frag.extra.get("post_filter") if frag.extra.get("inner_frag") else None
    physical_metric = _resolve_metric_field(
        resolver, frag.metric, prefer="counter" if is_counter else "gauge"
    )
    gauge_physical_metric = (
        physical_metric
        if not is_counter
        else _resolve_metric_field(resolver, frag.metric, prefer="gauge")
    )

    if pre_agg_filter:
        alias = re.sub(r"[^a-zA-Z0-9_]", "_", f"{frag.metric}_{frag.outer_agg}")
        metric_like = _summary_mode_from_metadata(context.metadata) or context.panel_type in {"stat", "singlestat", "gauge", "bargauge"}
        filter_value = _format_scalar_value(pre_agg_filter["value"])
        # Issue #8: when the filtered metric is a TSDS gauge, the pre-agg filter must
        # run under TS so that the outer SUM/AVG/MAX aggregates one value per (series,
        # bucket) instead of every per-sample doc. TSDS is proven by the resolver or, when
        # unknown, assumed per ``assume_tsds_gauges`` (the migration default).
        gauge_uses_ts = (not is_counter) and _gauge_can_use_ts(frag.metric, resolver, rp)
        pre_source = "TS" if gauge_uses_ts else "FROM"
        pre_time_filter = rp.ts_time_filter if pre_source == "TS" else rp.from_time_filter
        pre_bucket = rp.ts_bucket if pre_source == "TS" else rp.from_bucket
        lines = [
            f"{pre_source} {context.index}",
            f"| WHERE {pre_time_filter}",
            *_build_where_lines(filters),
            f"| WHERE {gauge_physical_metric} {pre_agg_filter['op']} {filter_value}",
        ]
        # The TS command rejects ``COUNT(*)`` ("count_star can't be used with TS
        # command; use count on a field instead"). The WHERE has already
        # constrained rows to the comparison, so counting the (non-null) metric
        # field is equivalent and valid. FROM keeps the cheaper ``COUNT(*)``.
        count_expr = f"COUNT({gauge_physical_metric})" if pre_source == "TS" else "COUNT(*)"
        if metric_like and not group_fields:
            context.output_group_fields = []
            lines.append(f"| STATS {alias} = {count_expr}" if frag.outer_agg == "count" else f"| STATS {alias} = {_agg_stats_expr(OUTER_AGG_MAP.get(frag.outer_agg, rp.default_gauge_agg.upper()), gauge_physical_metric, frag)}")
        else:
            group_by_parts = list(group_fields)
            context.output_group_fields = list(group_fields)
            if not metric_like:
                group_by_parts = [pre_bucket, *group_by_parts]
                context.output_group_fields = ["time_bucket", *context.output_group_fields]
            stats_expr = count_expr if frag.outer_agg == "count" else _agg_stats_expr(OUTER_AGG_MAP.get(frag.outer_agg, rp.default_gauge_agg.upper()), gauge_physical_metric, frag)
            stats_line = f"| STATS {alias} = {stats_expr}"
            if group_by_parts:
                stats_line += f" BY {', '.join(group_by_parts)}"
            lines.append(stats_line)
            if "time_bucket" in context.output_group_fields:
                lines.append("| SORT time_bucket ASC")
        context.esql_query = "\n".join(lines)
        context.parser_backend = "fragment"
        context.source_type = pre_source
        context.metric_name = alias
        context.output_metric_field = alias
        if frag.outer_agg == "count":
            _append_unique(
                context.warnings,
                "count() over a comparison is approximated as document COUNT(*); multi-sample series may be over-counted",
            )
        frag.extra.pop("post_filter", None)
        context.translation_complete = True
        return "translated aggregation with pre-aggregation comparison filter"

    if frag.outer_agg == "count" and is_counter:
        alias = re.sub(r"[^a-zA-Z0-9_]", "_", f"{frag.metric}_count")
        metric_like = context.panel_type in {"stat", "singlestat", "gauge", "bargauge"}
        if metric_like:
            context.output_group_fields = []
            by_clause = ", ".join(group_fields) if group_fields else _default_instance_field(rp)
            context.esql_query = "\n".join(
                [
                    f"FROM {context.index}",
                    f"| WHERE {rp.from_time_filter}",
                    *_build_where_lines(filters),
                    f"| WHERE {physical_metric} IS NOT NULL",
                    f"| STATS series_present = COUNT(*) BY {by_clause}",
                    f"| STATS {alias} = COUNT(*)",
                ]
            )
        else:
            context.output_group_fields = ["time_bucket"]
            by_clause = f"{rp.from_bucket}, " + (", ".join(group_fields) if group_fields else _default_instance_field(rp))
            context.esql_query = "\n".join(
                [
                    f"FROM {context.index}",
                    f"| WHERE {rp.from_time_filter}",
                    *_build_where_lines(filters),
                    f"| WHERE {physical_metric} IS NOT NULL",
                    f"| STATS series_present = COUNT(*) BY {by_clause}",
                    f"| STATS {alias} = COUNT(*) BY time_bucket",
                    "| SORT time_bucket ASC",
                ]
            )
        context.parser_backend = "fragment"
        context.source_type = "FROM"
        context.metric_name = alias
        context.output_metric_field = alias
        context.translation_complete = True
        return "translated count of counter metric"

    gauge_uses_ts = (not is_counter) and _gauge_can_use_ts(frag.metric, resolver, rp)
    source = "TS" if (is_counter or gauge_uses_ts) else "FROM"
    time_filter = rp.ts_time_filter if source == "TS" else rp.from_time_filter
    bucket = rp.ts_bucket if source == "TS" else rp.from_bucket

    if is_counter and frag.outer_agg != "count":
        # Bare counter aggregation: source PromQL applies an aggregator (sum/avg/min/max)
        # directly to a counter field without rate(). Use LAST_OVER_TIME as the inner
        # function to get the raw cumulative value, then apply the outer aggregation.
        inner_expr = f"LAST_OVER_TIME({physical_metric})"
        _append_unique(context.warnings, "Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value")
    else:
        inner_expr = physical_metric

    outer = OUTER_AGG_MAP.get(frag.outer_agg, rp.default_gauge_agg.upper())
    stats_expr = _agg_stats_expr(outer, inner_expr, frag)
    alias = re.sub(r"[^a-zA-Z0-9_]", "_", frag.metric)
    group_by_parts, output_group = _grouping_parts(bucket, group_fields)
    eval_line, final_alias = _frag_eval_line(alias, frag)

    context.parser_backend = "fragment"
    context.source_type = source
    context.metric_name = frag.metric
    context.output_metric_field = final_alias
    context.output_group_fields = output_group
    parts = [
        f"{source} {context.index}",
        f"| WHERE {time_filter}",
        *_build_where_lines(filters),
        f"| WHERE {physical_metric} IS NOT NULL",
    ]
    stats_line = f"| STATS {alias} = {stats_expr}"
    if group_by_parts:
        stats_line += f" BY {', '.join(group_by_parts)}"
    parts.append(stats_line)
    if eval_line:
        parts.append(eval_line)
    collapsed = None
    if _summary_mode_from_metadata(context.metadata):
        collapsed = _collapse_summary_ts_query(parts, output_group, [final_alias])
    if collapsed is None:
        if eval_line:
            parts.append(f"| KEEP {_keep(output_group, final_alias)}")
        if "time_bucket" in output_group:
            parts.append("| SORT time_bucket ASC")
    else:
        output_group = collapsed
    context.esql_query = "\n".join(parts)
    context.translation_complete = True
    context.output_group_fields = output_group
    return "translated simple aggregation expression"


@QUERY_TRANSLATORS.register("simple_metric_family", priority=10)
def simple_metric_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "simple_metric":
        return None
    if not frag.metric:
        return None

    if frag.metric == "ALERTS":
        _append_unique(
            context.warnings,
            "ALERTS{} is a Prometheus meta-metric exposing per-alert label sets; "
            "ES|QL aggregation collapses individual alerts into a single value",
        )

    resolver = context.resolver
    rp = context.rule_pack
    filters, had_vars = _frag_filters(frag, resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven label filters during migration")

    group_fields = _frag_group_labels(
        frag,
        resolver,
        context.metadata.get("preferred_group_labels"),
        preferred_origin=context.metadata.get("preferred_group_labels_origin"),
    )
    is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rp)
    can_use_direct_ts_gauge = _can_use_direct_ts_gauge(frag.metric, resolver, group_fields, frag, rp)
    # Issue #8: when the field is a TSDS gauge but ``_can_use_direct_ts_gauge`` rejects it
    # (group_fields present, or caller disabled the path), TS is still the correct source —
    # ``FROM`` against a TSDS sums every per-sample doc and inflates the value. Wrap with
    # default AVG so the result collapses cleanly whether grouping is present or not. TSDS
    # is proven by the resolver or, when unknown, assumed per ``assume_tsds_gauges``.
    can_use_ts_aggregated_gauge = (
        (not is_counter)
        and (not can_use_direct_ts_gauge)
        and (not (frag.extra.get("wrapped_scalar") if frag else False))
        and _gauge_can_use_ts(frag.metric, resolver, rp)
    )

    if is_counter:
        source = "TS"
        time_filter = rp.ts_time_filter
        bucket = rp.ts_bucket
        physical_metric = _resolve_metric_field(resolver, frag.metric, prefer="counter")
        # Bare counter reference: the source PromQL asks for the raw cumulative value
        # (no rate()/irate()/increase() applied). LAST_OVER_TIME returns the counter's
        # final value within each TBUCKET window, faithfully mirroring Prometheus's
        # instant-vector semantics. RATE would change the panel's meaning entirely.
        inner_expr = f"LAST_OVER_TIME({physical_metric})"
        _append_unique(context.warnings, "Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value")
        stats_expr = f"MAX({inner_expr})"
    elif can_use_direct_ts_gauge:
        source = "TS"
        time_filter = rp.ts_time_filter
        bucket = rp.ts_bucket
        physical_metric = _resolve_metric_field(resolver, frag.metric, prefer="gauge")
        stats_expr = physical_metric
    elif can_use_ts_aggregated_gauge:
        source = "TS"
        time_filter = rp.ts_time_filter
        bucket = rp.ts_bucket
        default_agg = rp.default_gauge_agg.upper()
        physical_metric = _resolve_metric_field(resolver, frag.metric, prefer="gauge")
        stats_expr = f"{default_agg}({physical_metric})"
        # No explicit PromQL aggregator was given; default to the gauge aggregator. With
        # grouping labels this is a faithful per-series downsample; without them it collapses
        # series and the warning says so (and is recorded as a semantic loss).
        _append_unique(context.warnings, gauge_default_agg_warning(group_fields, frag.metric, default_agg))
    else:
        source = "FROM"
        time_filter = rp.from_time_filter
        bucket = rp.from_bucket
        default_agg = rp.default_gauge_agg.upper()
        physical_metric = _resolve_metric_field(resolver, frag.metric, prefer="gauge")
        stats_expr = f"{default_agg}({physical_metric})"
        if frag.extra.get("wrapped_scalar"):
            _append_unique(context.warnings, "Approximated scalar() as a direct metric value")
        else:
            _append_unique(context.warnings, gauge_default_agg_warning(group_fields, frag.metric, default_agg))

    alias = re.sub(r"[^a-zA-Z0-9_]", "_", frag.metric)
    eval_line, final_alias = _frag_eval_line(alias, frag)
    group_by_parts, output_group = _grouping_parts(bucket, group_fields)

    context.parser_backend = "fragment"
    context.source_type = source
    context.metric_name = frag.metric
    context.output_metric_field = final_alias
    context.output_group_fields = output_group
    parts = [
        f"{source} {context.index}",
        f"| WHERE {time_filter}",
        *_build_where_lines(filters),
        f"| WHERE {physical_metric} IS NOT NULL",
    ]
    stats_line = f"| STATS {alias} = {stats_expr}"
    if group_by_parts:
        stats_line += f" BY {', '.join(group_by_parts)}"
    parts.append(stats_line)
    if eval_line:
        parts.append(eval_line)
    collapsed = None
    if _summary_mode_from_metadata(context.metadata):
        collapsed = _collapse_summary_ts_query(parts, output_group, [final_alias])
    if collapsed is None:
        if eval_line:
            parts.append(f"| KEEP {_keep(output_group, final_alias)}")
        if "time_bucket" in output_group:
            parts.append("| SORT time_bucket ASC")
    else:
        output_group = collapsed
    context.esql_query = "\n".join(parts)
    context.translation_complete = True
    context.output_group_fields = output_group
    return "translated simple metric expression"


@QUERY_TRANSLATORS.register("fragment_extract", priority=20)
def fragment_extract_rule(context):
    if context.translation_complete:
        return None
    if context.metric_name:
        return None
    frag = context.fragment
    if not frag:
        return None
    before = (
        context.metric_name,
        context.inner_func,
        context.range_window,
        context.outer_agg,
        tuple(context.group_labels),
        context.parser_backend,
    )
    _apply_fragment_to_context(frag, context)
    after = (
        context.metric_name,
        context.inner_func,
        context.range_window,
        context.outer_agg,
        tuple(context.group_labels),
        context.parser_backend,
    )
    if before == after:
        return None
    return f"extracted fragment fields via {context.parser_backend or 'fragment'}"


@QUERY_TRANSLATORS.register("extract_label_filters", priority=25)
def extract_label_filters_rule(context):
    """Populate ``context.label_filters`` from the parsed fragment matchers.

    The fallback translation path (fragment_extract → stats_expression →
    render_esql) builds queries via ``_build_esql(context)`` which uses
    ``context.label_filters`` for WHERE clauses.  Without this step the
    fallback path silently drops all label selectors from the source PromQL
    expression — for example ``mode!~"idle|iowait|steal"`` on a nested-agg
    query such as ``avg(sum by(cpu)(rate(node_cpu_seconds_total{mode!~...})))``.
    Specific family rules (binary_expr, join, simple_agg, range_agg, …) handle
    their own filters directly, so we only fill in here when
    translation_complete is still False and label_filters is still empty.
    """
    if context.translation_complete:
        return None
    if not context.metric_name:
        return None
    if context.label_filters:
        return None
    frag = context.fragment
    if not frag:
        return None
    filters, had_vars = _frag_filters(frag, context.resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven label filters during migration")
    if filters:
        context.label_filters = filters
        return f"extracted {len(filters)} label filter(s) from fragment matchers"
    return None


@QUERY_TRANSLATORS.register("scalar_outer_agg", priority=40)
def scalar_outer_agg_rule(context):
    if context.translation_complete:
        return None
    if context.inner_func in OUTER_AGG_MAP and not context.outer_agg:
        context.outer_agg = context.inner_func
        context.inner_func = ""
        return f"treated {context.outer_agg} as outer aggregation"
    return None


@QUERY_TRANSLATORS.register("resolve_labels", priority=45)
def resolve_labels_rule(context):
    if context.translation_complete:
        return None
    if not context.resolver:
        return None
    original = list(context.group_labels)
    context.group_labels = context.resolver.resolve_labels(context.group_labels)
    if context.output_group_fields:
        context.output_group_fields = context.resolver.resolve_labels(context.output_group_fields)
    if original != context.group_labels:
        return f"resolved labels {original} -> {context.group_labels}"
    return None


@QUERY_TRANSLATORS.register("counter_detection", priority=50)
def counter_detection_rule(context):
    if context.translation_complete:
        return None
    if not context.metric_name or not context.resolver:
        return None
    if not context.resolver.is_counter(context.metric_name):
        return None
    if context.outer_agg == "count":
        return "kept counter metric raw for COUNT aggregation"
    context.source_type = "TS"
    if not context.inner_func:
        context.inner_func = "rate"
        context.range_window = context.range_window or context.rule_pack.default_rate_window
        _append_unique(
            context.warnings,
            f"Detected counter metric; defaulting to RATE over {context.range_window}",
        )
        return "auto-wrapped counter metric in RATE"
    return "forced TS source for counter metric"


@QUERY_TRANSLATORS.register("source_type", priority=60)
def source_type_rule(context):
    if context.translation_complete:
        return None
    if context.source_type:
        return None
    context.source_type = "TS" if context.inner_func in AGG_FUNCTION_MAP else "FROM"
    return f"selected {context.source_type} source"


@QUERY_TRANSLATORS.register("time_filter", priority=70)
def time_filter_rule(context):
    if context.translation_complete:
        return None
    if context.time_filter:
        return None
    if context.source_type == "TS":
        context.time_filter = context.rule_pack.ts_time_filter
    else:
        context.time_filter = context.rule_pack.from_time_filter
    return f"applied time filter {context.time_filter}"


@QUERY_TRANSLATORS.register("bucket", priority=80)
def bucket_rule(context):
    if context.translation_complete:
        return None
    if context.bucket_expr:
        return None
    if context.source_type == "TS":
        context.bucket_expr = context.rule_pack.ts_bucket
    else:
        context.bucket_expr = context.rule_pack.from_bucket
    return f"applied bucket {context.bucket_expr}"


@QUERY_TRANSLATORS.register("stats_expression", priority=90)
def stats_expression_rule(context):
    if context.translation_complete:
        return None
    if not context.metric_name:
        return None

    prefer = "counter" if context.inner_func in {"rate", "irate", "increase"} else "gauge"
    physical_metric = _resolve_metric_field(context.resolver, context.metric_name, prefer=prefer)
    inner_expr = physical_metric
    if context.inner_func in AGG_FUNCTION_MAP:
        esql_func = AGG_FUNCTION_MAP[context.inner_func]
        window_arg = f", {context.range_window}" if context.range_window else ""
        inner_expr = f"{esql_func}({physical_metric}{window_arg})"

    if context.outer_agg in OUTER_AGG_MAP:
        context.stats_expr = f"{OUTER_AGG_MAP[context.outer_agg]}({inner_expr})"
        return f"built stats expression {context.stats_expr}"

    if context.inner_func in AGG_FUNCTION_MAP:
        if context.source_type == "TS" and context.group_labels:
            context.stats_expr = f"AVG({inner_expr})"
            _append_unique(
                context.warnings,
                f"Added outer AVG() around {context.inner_func} because ES|QL requires an outer aggregation "
                "when grouping TS functions by label fields",
            )
            return f"built stats expression {context.stats_expr}"
        context.stats_expr = inner_expr
        return f"built stats expression {context.stats_expr}"

    default_agg = context.rule_pack.default_gauge_agg.upper()
    context.stats_expr = f"{default_agg}({physical_metric})"
    if context.inner_func:
        _append_unique(
            context.warnings,
            f"Unmapped function {context.inner_func}; approximating with {default_agg}",
        )
    else:
        _append_unique(context.warnings, f"No explicit aggregation; using {default_agg} (correct for gauge metrics)")
    return f"built stats expression {context.stats_expr}"


@QUERY_POSTPROCESSORS.register("index_rewrite", priority=10)
def index_rewrite_rule(context):
    original = context.index
    for rewrite in context.rule_pack.index_rewrites:
        if fnmatch.fnmatch(context.index, rewrite.match):
            context.index = rewrite.replace
            break
    if context.index != original:
        if context.esql_query:
            context.esql_query = re.sub(
                rf"^((?:FROM|TS)\s+){re.escape(original)}",
                rf"\1{context.index}",
                context.esql_query,
                count=1,
                flags=re.MULTILINE,
            )
        return f"rewrote index {original} -> {context.index}"
    return None


@QUERY_POSTPROCESSORS.register("render_esql", priority=90)
def render_esql_rule(context):
    if context.translation_complete and context.esql_query:
        return None
    if context.feasibility == "not_feasible" or not context.metric_name or not context.stats_expr:
        return None
    context.esql_query = _build_esql(context)
    return "rendered ES|QL query"


def _projected_metric_field_from_esql(esql_query):
    for line in esql_query.splitlines():
        stripped = line.strip()
        if not stripped.startswith("| STATS") and not stripped.startswith("| EVAL"):
            continue
        match = re.match(r"\|\s+(?:STATS|EVAL)\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=", stripped)
        if match:
            return match.group(1)
    return ""


@QUERY_POSTPROCESSORS.register("value_wrapper_transforms", priority=92)
def value_wrapper_transforms_rule(context):
    """Apply ES|QL equivalents for sort/round/clamp_min wrapper functions."""
    frag = context.fragment
    if not frag or not context.esql_query or context.feasibility == "not_feasible":
        return None

    metric_field = (
        context.output_metric_field
        or _projected_metric_field_from_esql(context.esql_query)
        or context.metric_name
        or "value"
    )
    if not context.output_metric_field and metric_field != "value":
        context.output_metric_field = metric_field
    lines = context.esql_query.splitlines()
    applied = []

    # Detect two-stage topk shape: contains "STATS <field> = LAST(..." which
    # means the output metric isn't defined until after that line — EVAL must
    # be inserted *after* it, not before the first SORT.
    last_stats_idx = next(
        (i for i, ln in enumerate(lines) if "= LAST(" in ln and ln.strip().startswith("| STATS")),
        None,
    )

    def _eval_insert_idx(lines):
        if last_stats_idx is not None:
            return last_stats_idx + 1
        return next(
            (i for i, ln in enumerate(lines) if ln.strip().startswith("| SORT")),
            len(lines),
        )

    # round() → EVAL value = ROUND(value, N)
    if frag.extra.get("has_round"):
        precision = frag.extra.get("round_precision")
        if precision is not None:
            prec_arg = int(precision) if precision == int(precision) else precision
            eval_clause = f"| EVAL {metric_field} = ROUND({metric_field}, {prec_arg})"
        else:
            eval_clause = f"| EVAL {metric_field} = ROUND({metric_field})"
        lines.insert(_eval_insert_idx(lines), eval_clause)
        _append_unique(context.warnings, "round() approximated with ES|QL ROUND()")
        applied.append("round")

    # clamp_min() → EVAL value = GREATEST(value, min)
    clamp_min = frag.extra.get("clamp_min_value")
    if clamp_min is not None:
        val = _format_scalar_value(clamp_min)
        eval_clause = f"| EVAL {metric_field} = GREATEST({metric_field}, {val})"
        lines.insert(_eval_insert_idx(lines), eval_clause)
        _append_unique(context.warnings, "clamp_min() approximated with ES|QL GREATEST()")
        applied.append("clamp_min")

    # clamp_max() → EVAL value = LEAST(value, max). For clamp(v, lo, hi) both
    # clamp_min and clamp_max are set; applying GREATEST then LEAST yields
    # GREATEST(LEAST(v, hi), lo) == clamp(v, lo, hi) (bounds are order-independent).
    clamp_max = frag.extra.get("clamp_max_value")
    if clamp_max is not None:
        val = _format_scalar_value(clamp_max)
        eval_clause = f"| EVAL {metric_field} = LEAST({metric_field}, {val})"
        lines.insert(_eval_insert_idx(lines), eval_clause)
        _append_unique(context.warnings, "clamp_max() translated via ES|QL LEAST()")
        applied.append("clamp_max")

    # sgn() → EVAL value = SIGNUM(value) (exact equivalent)
    if frag.extra.get("has_sgn"):
        eval_clause = f"| EVAL {metric_field} = SIGNUM({metric_field})"
        lines.insert(_eval_insert_idx(lines), eval_clause)
        _append_unique(context.warnings, "sgn() translated via ES|QL SIGNUM()")
        applied.append("sgn")

    # Elementwise math/trig wrappers → EVAL value = FN(value), applied in
    # evaluation order (innermost first). All are exact ES|QL equivalents.
    for math_fn in frag.extra.get("math_fns", []):
        template = _MATH_FN_ESQL.get(math_fn)
        if not template:
            continue
        eval_clause = f"| EVAL {metric_field} = {template.format(m=metric_field)}"
        lines.insert(_eval_insert_idx(lines), eval_clause)
        _append_unique(context.warnings, f"{math_fn}() translated via exact ES|QL equivalent")
        applied.append(math_fn)

    # sort() / sort_desc() → set the output sort direction.
    # For two-stage topk, replace the LAST "| SORT <field>" line (which controls
    # output order) rather than the first SORT (which orders time buckets for LAST()).
    if "value_sort_desc" in frag.extra:
        sort_desc = frag.extra["value_sort_desc"]
        direction = "DESC" if sort_desc else "ASC"
        if last_stats_idx is not None:
            # Two-stage topk: update the last value-sort line only
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip().startswith("| SORT") and metric_field in lines[i]:
                    lines[i] = f"| SORT {metric_field} {direction}"
                    break
            else:
                lines.append(f"| SORT {metric_field} {direction}")
        else:
            new_lines = []
            replaced = False
            for ln in lines:
                if ln.strip().startswith("| SORT") and not replaced:
                    new_lines.append(f"| SORT {metric_field} {direction}")
                    replaced = True
                else:
                    new_lines.append(ln)
            if not replaced:
                new_lines.append(f"| SORT {metric_field} {direction}")
            lines = new_lines
        func = "sort_desc" if sort_desc else "sort"
        _append_unique(
            context.warnings,
            f"{func}() applied — ES|QL output sorted by value {direction}",
        )
        applied.append(func)

    if applied:
        context.esql_query = "\n".join(lines)
        return f"applied value wrapper transforms: {', '.join(applied)}"
    return None


def _has_or_vector_fallback(frag, _depth=0):
    """Return True if *frag* or any operand was an ``X or vector(N)`` fallback."""
    if frag is None or _depth > 8:
        return False
    if frag.extra.get("or_vector_fallback"):
        return True
    return _has_or_vector_fallback(frag.extra.get("left_frag"), _depth + 1) or _has_or_vector_fallback(
        frag.extra.get("right_frag"), _depth + 1
    )


@QUERY_POSTPROCESSORS.register("or_vector_fallback_note", priority=94)
def or_vector_fallback_note_rule(context):
    """Warn that a stripped ``or vector(N)`` zero-fill is only approximated.

    Dropping the ``vector(N)`` operand keeps the panel translatable, but ES|QL
    will leave gaps where Grafana would have shown the constant fallback value
    instead. Surface that honestly rather than hide the semantic gap (issue #66).
    """
    frag = context.fragment
    if not frag or not context.esql_query or context.feasibility == "not_feasible":
        return None
    if not _has_or_vector_fallback(frag):
        return None
    _append_unique(
        context.warnings,
        "Approximated PromQL 'or vector(N)' zero-fill fallback by dropping the "
        "constant operand; time ranges with no data appear as gaps instead of "
        "the fallback value",
    )
    return "noted or-vector zero-fill approximation"


@QUERY_POSTPROCESSORS.register("post_filter", priority=95)
def post_filter_rule(context):
    frag = context.fragment
    post_filter = frag.extra.get("post_filter") if frag else None
    if not post_filter or not context.esql_query or not context.output_metric_field:
        return None
    value = _format_scalar_value(post_filter["value"])
    clause = f"| WHERE {context.output_metric_field} {post_filter['op']} {value}"
    lines = context.esql_query.splitlines()
    sort_idx = next((idx for idx, line in enumerate(lines) if line.strip().startswith("| SORT")), None)
    if sort_idx is None:
        lines.append(clause)
    else:
        lines.insert(sort_idx, clause)
    context.esql_query = "\n".join(lines)
    return f"applied post-aggregation filter {post_filter['op']} {value}"


@QUERY_VALIDATORS.register("metric_name_required", priority=10)
def metric_name_required_rule(context):
    if context.feasibility == "not_feasible" or context.metric_name:
        return None
    if context.fragment and context.fragment.extra.get("parse_error"):
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        return "missing metric name after parse failure"
    context.feasibility = "not_feasible"
    context.confidence = 0.0
    _append_unique(context.warnings, "Could not extract metric name")
    return "missing metric name"


@QUERY_VALIDATORS.register("dynamic_metric_name", priority=12)
def dynamic_metric_name_rule(context):
    if context.feasibility == "not_feasible":
        return None
    metric_name = str(context.metric_name or "")
    if not metric_name.startswith("label_"):
        return None
    variable_name = metric_name.removeprefix("label_") or "metric"
    context.feasibility = "not_feasible"
    context.confidence = 0.0
    _append_unique(
        context.warnings,
        f"PromQL metric name comes from Grafana template variable (${variable_name}); "
        "dynamic metric selection requires manual redesign",
    )
    return "dynamic metric name"


@QUERY_VALIDATORS.register("time_filter_source_alignment", priority=20)
def time_filter_source_alignment_rule(context):
    if context.feasibility == "not_feasible":
        return None
    if context.source_type == "FROM" and context.time_filter and "TRANGE(" in context.time_filter:
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        _append_unique(context.warnings, "FROM queries cannot use TRANGE()")
        return "invalid FROM + TRANGE combination"
    return None


@QUERY_VALIDATORS.register("rendered_query_required", priority=30)
def rendered_query_required_rule(context):
    if context.feasibility == "not_feasible" or context.esql_query:
        return None
    context.feasibility = "not_feasible"
    context.confidence = 0.0
    _append_unique(context.warnings, "No ES|QL query was produced")
    return "missing ES|QL output"


def _collect_source_metrics(frag, seen=None):
    seen = seen or set()
    metrics = []
    if not frag or id(frag) in seen:
        return metrics
    seen.add(id(frag))
    metric = str(getattr(frag, "metric", "") or "")
    if metric and not metric.startswith("label_"):
        metrics.append(metric)
    for key in ("left_frag", "right_frag"):
        child = frag.extra.get(key) if getattr(frag, "extra", None) else None
        if child:
            metrics.extend(_collect_source_metrics(child, seen))
    rhs = getattr(frag, "binary_rhs", None)
    if rhs:
        metrics.extend(_collect_source_metrics(rhs, seen))
    return list(dict.fromkeys(metrics))


def _metric_exists_in_live_schema(metric, resolver):
    candidates = [metric]
    for prefer in ("gauge", "counter"):
        resolved = _resolve_metric_field(resolver, metric, prefer=prefer)
        if resolved and resolved not in candidates:
            candidates.append(resolved)
    statuses = [resolver.field_exists(candidate) for candidate in candidates]
    if any(status is True for status in statuses):
        return True
    if any(status is None for status in statuses):
        return None
    return False


@QUERY_VALIDATORS.register("live_metric_fields_exist", priority=25)
def live_metric_fields_exist_rule(context):
    resolver = context.resolver
    if context.feasibility == "not_feasible" or not resolver:
        return None
    if resolver.discovery_status().get("status") != "ok":
        return None
    missing = []
    for metric in _collect_source_metrics(context.fragment):
        if _metric_exists_in_live_schema(metric, resolver) is False:
            missing.append(metric)
    if not missing:
        return None
    context.feasibility = "not_feasible"
    context.confidence = 0.0
    for metric in missing:
        resolved = _resolve_metric_field(resolver, metric, prefer="gauge") or metric
        _append_unique(context.warnings, f"Target field {resolved} is missing from live schema discovery")
    return "missing live metric fields"


_logger = logging.getLogger(__name__)


def translate_promql_to_esql(
    expr,
    datasource_index="metrics-*",
    esql_index=None,
    panel_type="",
    rule_pack=None,
    resolver=None,
    translation_hints=None,
    datasource_type="",
    datasource_uid="",
    datasource_name="",
    query_language="",
    llm_endpoint="",
    llm_model="",
    llm_api_key="",
):
    """Rule-based PromQL → ES|QL translation via fragment model + pipeline.

    When the rule engine marks a query ``not_feasible`` and LLM config is
    provided (``llm_endpoint`` + ``llm_model``), an LLM-assisted translation
    is attempted as a last resort.
    """
    context = TranslationContext(
        promql_expr=expr,
        data_view=datasource_index,
        index=esql_index or datasource_index,
        rule_pack=rule_pack or RulePackConfig(),
        resolver=resolver,
        panel_type=panel_type,
        clean_expr=expr,
        metadata=dict(translation_hints or {}),
        datasource_type=datasource_type,
        datasource_uid=datasource_uid,
        datasource_name=datasource_name,
        query_language=query_language,
    )
    QUERY_PREPROCESSORS.apply(context)
    QUERY_CLASSIFIERS.apply(context, stop_when=lambda ctx, _: ctx.feasibility == "not_feasible")
    if context.feasibility != "not_feasible":
        QUERY_TRANSLATORS.apply(context, stop_when=lambda ctx, _: ctx.translation_complete)
        QUERY_POSTPROCESSORS.apply(context)
        QUERY_VALIDATORS.apply(context, stop_when=lambda ctx, _: ctx.feasibility == "not_feasible")

    if context.feasibility == "not_feasible" and llm_endpoint and llm_model:
        llm_result = attempt_llm_translation(
            promql_expr=context.clean_expr or context.promql_expr,
            index=context.index,
            panel_type=panel_type,
            endpoint=llm_endpoint,
            model=llm_model,
            api_key=llm_api_key,
            extra_context={"warnings": context.warnings},
        )
        if llm_result and llm_result.get("esql_query"):
            _logger.info("LLM recovered not_feasible expression: %s", expr[:80])
            context.esql_query = llm_result["esql_query"]
            context.metric_name = llm_result.get("metric_name") or context.metric_name or "llm_value"
            context.output_metric_field = context.metric_name
            context.source_type = llm_result.get("source_type") or "TS"
            context.feasibility = "feasible"
            context.parser_backend = "llm"
            context.translation_complete = True
            for w in llm_result.get("warnings") or []:
                _append_unique(context.warnings, w)

    if context.feasibility == "not_feasible":
        context.confidence = 0.0
    else:
        if context.fragment and _frag_has_incompatible_target_fields(context.fragment, context.resolver):
            _append_unique(context.warnings, "Dropped label filters with incompatible target field types during migration")
        if context.fragment and _frag_has_incompatible_group_fields(
            context.fragment,
            context.resolver,
            context.metadata.get("preferred_group_labels", []),
        ):
            _append_unique(context.warnings, "Dropped grouping fields with incompatible target field types during migration")
        context.confidence = 0.85 if not context.warnings else 0.6
    context.query_ir = build_query_ir(context)
    contract, evaluation, fulfillment = _build_metric_contract_artifacts(
        context.query_ir,
        resolver=context.resolver,
        rule_pack=context.rule_pack,
    )
    context.target_query_contract = _artifact_to_dict(contract)
    context.contract_evaluation = _artifact_to_dict(evaluation)
    context.fulfillment_plan = _artifact_to_dict(fulfillment)
    return context


__all__ = [
    "TranslationContext",
    "binary_expr_family_rule",
    "counter_detection_rule",
    "extract_label_filters_rule",
    "fragment_extract_rule",
    "fragment_guardrails_rule",
    "grafana_macro_rule",
    "index_rewrite_rule",
    "join_family_rule",
    "parse_fragment_rule",
    "post_filter_rule",
    "range_agg_family_rule",
    "render_esql_rule",
    "resolve_labels_rule",
    "scaled_agg_family_rule",
    "simple_agg_family_rule",
    "simple_metric_family_rule",
    "translate_promql_to_esql",
    "uptime_family_rule",
]
