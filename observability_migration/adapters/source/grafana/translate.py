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
    _ABSENT_OR_OPERAND_WARNING,
    _APPROX_AGG_OVER_SUMMARY_RATIO_WARNING,
    _COUNTER_UNSAFE_OUTER_AGGS,
    AGG_FUNCTION_MAP,
    OUTER_AGG_MAP,
    PromQLFragment,
    _agg_over_or_not_feasible_reason,
    _append_not_feasible_reason,
    _apply_fragment_to_context,
    _apply_metric_map_to_rate_on_simple,
    _apply_unit_scale,
    _build_esql,
    _build_formula_plan,
    _build_log_message_filter,
    _build_shared_measure_pipeline,
    _build_stats_call,
    _build_where_lines,
    _can_use_direct_ts_gauge,
    _collapse_summary_ts_query,
    _counter_type_uncertainty_warning,
    _counter_unsafe_cast_needed,
    _counter_unsafe_cast_warning,
    _drop_legend_labels_if_redundant,
    _esql_identifier,
    _expand_late_bound_group_by_terms,
    _finalize_fused_stats_assignments,
    _format_scalar_value,
    _frag_eval_line,
    _frag_filters,
    _frag_group_labels,
    _frag_has_incompatible_group_fields,
    _frag_has_incompatible_target_fields,
    _frag_source_labels,
    _fragment_metric_names,
    _gauge_can_use_ts,
    _grouping_parts,
    _inline_filters_into_stats_expr,
    _is_counter_fallback,
    _is_esql_control_token,
    _is_label_enrichment_metric,
    _iter_pending_join_rhs_fragments,
    _left_operand_of_same_metric_range_fallback,
    _metric_map_target_index,
    _metric_map_unapplied_notes,
    _metric_map_unit_scale,
    _mixed_os_or_operands,
    _or_chain_has_vector_matching,
    _parse_fragment,
    _parse_logql_search,
    _plan_metric_map_rate_transform,
    _range_call,
    _resolve_frag_metric_field,
    _resolve_metric_field,
    _same_metric_range_fallback_warning,
    _summary_mode_from_metadata,
    classify_promql_complexity,
    colocated_binary_agg_plan,
    colocated_metric_fields,
    gauge_default_agg_warning,
    iter_agg_over_or_reductions,
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
from .runtime_features import binds_esql_named_params
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
    "acosh": "ACOSH({m})",
    "asin": "ASIN({m})",
    "asinh": "ASINH({m})",
    "atan": "ATAN({m})",
    "atanh": "ATANH({m})",
    "cos": "COS({m})",
    "sin": "SIN({m})",
    "tan": "TAN({m})",
    "cosh": "COSH({m})",
    "sinh": "SINH({m})",
    "tanh": "TANH({m})",
    "deg": "({m} * 180 / PI())",
    "rad": "({m} * PI() / 180)",
}


# ES|QL TS functions that REQUIRE a counter argument. Every other range
# function (MAX_OVER_TIME, MIN_OVER_TIME, SUM_OVER_TIME, AVG_OVER_TIME,
# COUNT_OVER_TIME, DELTA, DERIV, PERCENTILE_OVER_TIME, …) rejects counter input
# ("argument of [...] must be [... except counter types]").
_COUNTER_INPUT_ESQL_FUNCS = frozenset({"RATE", "IRATE", "INCREASE"})

# PromQL range functions that are counter-only by Prometheus convention. When a
# panel's source query wraps a metric in one of these, the telemetry contract
# and the synthetic-data seeder both type that metric as a counter
# (``time_series_metric: counter``) — see
# ``observability_migration.core.telemetry_contract``. So even when the offline
# counter heuristic guessed "gauge" and we degraded the function to a gauge
# analogue (e.g. ``increase()`` -> ``MAX_OVER_TIME``), the *stored* field is a
# counter and ES|QL still rejects the bare metric. The counter-style source
# function is therefore an authoritative cast signal.
_COUNTER_STYLE_SOURCE_FUNCS = frozenset({"rate", "irate", "increase"})


def _counter_safe_metric_arg(
    esql_func: str,
    metric_expr: str,
    is_counter: bool,
    source_range_func: str | None = None,
    *,
    counter_refuted: bool = False,
    force_cast: bool = False,
) -> str:
    """Cast a counter metric to double for ES|QL functions that reject counters,
    so the emitted query executes instead of failing at runtime.

    Casts when EITHER:

    - the field is a confirmed counter (``is_counter``), OR
    - the panel's *source* PromQL used a counter-only range function
      (``rate``/``irate``/``increase``) and the target has NOT authoritatively
      refuted the counter classification (``counter_refuted``). This mirrors the
      telemetry contract / seeder, which type any ``rate()``/``increase()``-ed
      field as a counter unless an explicit rule-pack ``gauge`` pin or live
      gauge field-caps say otherwise. So when the offline heuristic merely
      *guessed* "gauge" and we degraded ``increase()`` -> ``MAX_OVER_TIME``, the
      stored field is still a counter and the bare metric would be rejected.
    - the caller passes ``force_cast=True`` (issue #245) -- the target maps
      this field with conflicting types across indices, which can make ES|QL
      reject the bare form regardless of which aggregation is applied.
      Compute it with :func:`_counter_unsafe_cast_needed`.

    The cast is skipped for counter-consuming ES|QL functions
    (``RATE``/``IRATE``/``INCREASE``), which take the raw counter, and for a
    field that is an authoritative gauge (``counter_refuted``) — there the
    stored field really is a gauge double, so no needless cast / snapshot churn
    is added on the common gauge ``*_over_time`` path.
    """
    if (esql_func or "").upper() in _COUNTER_INPUT_ESQL_FUNCS:
        return metric_expr
    counter_source = (
        (source_range_func or "").lower() in _COUNTER_STYLE_SOURCE_FUNCS
        and not counter_refuted
    )
    if is_counter or counter_source or force_cast:
        return f"TO_DOUBLE({metric_expr})"
    return metric_expr


def _counter_refuted(resolver, metric: str) -> bool:
    """True when the target authoritatively says ``metric`` is NOT a counter
    (explicit rule-pack ``gauge`` pin or live gauge field-caps). Silent
    (returns False) when offline or the field is unknown, so a counter-style
    source function can still drive the counter-safe cast."""
    if resolver is None or not metric:
        return False
    refutes = getattr(resolver, "refutes_counter", None)
    return bool(refutes(metric)) if callable(refutes) else False


def _default_instance_field(rp, resolver=None):
    """Return the series-identity field used when collapsing ``count()`` rows.

    Strict passthrough and native PROMQL keep the source label ``instance``.
    Otherwise fall back to the OTel default used by the ES|QL path.
    """
    if getattr(resolver, "_passthrough", False) or getattr(rp, "native_promql", False):
        return "instance"
    return "service.instance.id"


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


def _operand_by_labels(frag, resolver):
    """Resolved ``by(...)`` dimensions the source operands key their series on.

    These are the labels whose loss would *collapse* source series and change
    values (e.g. a per-``cpu`` numerator summed away to per-``instance``). We
    walk the operand subtree so nested arithmetic is covered too. The join's
    own ``on(...)`` key and ``group_left(...)`` enrichment labels are handled
    separately by :func:`_join_is_faithful`.
    """
    raw: list[str] = []

    def walk(node, depth):
        if not isinstance(node, PromQLFragment) or depth > 6:
            return
        raw.extend(node.group_labels or [])
        for key in ("left_frag", "right_frag", "inner_frag"):
            walk(node.extra.get(key), depth + 1)

    for key in ("left_frag", "right_frag"):
        walk(frag.extra.get(key), 0)
    resolved = resolver.resolve_labels(raw) if resolver else list(raw)
    seen: dict[str, None] = {}
    for label in resolved:
        if label:
            seen[label] = None
    return list(seen)


def _operand_retains_label(frag, raw_label, depth=0):
    """Whether an operand still carries ``raw_label`` at the vector-match point.

    PromQL matches ``on(k)`` on the labels each operand *actually* exposes. An
    aggregation projects its series to its ``by(...)`` set, so it only carries
    ``k`` when ``k`` is in that set; a bare selector / range function retains
    every label. If an operand aggregated ``k`` away (e.g. ``sum(irate(b))`` on
    the RHS of ``... / on(instance) ...``), the per-key match is impossible in
    the source and the ES|QL would *invent* a per-key numerator/denominator
    (review #164 follow-up). Conservatively recurse through nested arithmetic.
    """
    if not isinstance(frag, PromQLFragment) or depth > 6:
        return True
    if frag.outer_agg:
        return raw_label in (frag.group_labels or [])
    children = [
        frag.extra.get(key) for key in ("left_frag", "right_frag", "inner_frag")
    ]
    children = [child for child in children if isinstance(child, PromQLFragment)]
    if children:
        return all(_operand_retains_label(child, raw_label, depth + 1) for child in children)
    return True


def _join_is_faithful(frag, resolver, output_group_fields):
    """Whether a label-aligned join migrates bit-for-bit (so it can be clean).

    A label-aligned ``A op on(k) B`` is numerically identical to its ES|QL
    per-key aggregation **only when no source dimension is collapsed**:

    1. It must be an ``on(...)`` join (matcher ``type == "Include"``);
       ``ignoring(...)`` selects the complementary label set and is not the
       proven, parity-checked subset.
    2. Each operand must still carry every ``on(...)`` key at the match point —
       i.e. an aggregating operand must include the key in its ``by(...)`` set.
       Otherwise the source has no per-key match and the ES|QL invents one
       (review #164 follow-up: ``... / on(instance) group_left sum(irate(b))``).
    3. Every operand ``by(...)`` dimension must survive into the output grouping
       — otherwise series the source kept apart are summed together (review
       #164: a left ``by(instance,cpu)`` dropped to ``by(instance)``).
    4. The join key must be represented in the output, either directly or via
       the ``group_left`` enrichment label(s) that stand in for it (e.g.
       grouping by ``chip_name`` instead of its 1:1 ``chip`` key).

    Returns ``False`` (keep the same-bucket caveat / drop warning) otherwise.
    """
    matching = frag.extra.get("vector_matching") or {}
    if not matching or matching.get("type") == "Exclude":
        return False
    raw_on = matching.get("labels") or []
    if not raw_on:
        return False
    for key in ("left_frag", "right_frag"):
        operand = frag.extra.get(key)
        if isinstance(operand, PromQLFragment) and not all(
            _operand_retains_label(operand, label) for label in raw_on
        ):
            return False
    out = set(output_group_fields or [])
    for label in _operand_by_labels(frag, resolver):
        if label not in out:
            return False
    resolved_on = resolver.resolve_labels(raw_on) if resolver else list(raw_on)
    if all(label in out for label in resolved_on):
        return True
    raw_enrich = frag.extra.get("enrichment_labels") or []
    resolved_enrich = (
        resolver.resolve_labels(raw_enrich) if resolver else list(raw_enrich)
    )
    return bool(resolved_enrich) and all(label in out for label in resolved_enrich)


def _fully_aggregated_scalar_op(frag):
    """Whether a binary op has two fully-aggregated scalar operands with no by() clause.

    ``sum(A) - sum(B)`` (no ``by(...)`` on either side) produces a single unlabelled
    time-series on each side. In PromQL the binary op is applied per instant; in
    ES|QL ``SUM(A) - SUM(B)`` per TBUCKET is numerically identical when both metrics
    come from the same TS source at the same scrape interval, because each TBUCKET
    receives the same set of time-series rows for both metrics. No explicit ``on(...)``
    key is needed — both sides are already scalars. This is the ``sum(A) op sum(B)``
    and ``avg(A) op avg(B)`` family; bare ``A op B`` (no outer agg) is NOT covered
    because per-document values still need per-instance alignment.

    Terminal aggregates only: if an operand wraps an inner binary expression (e.g.
    ``sum(A * on(k) group_left(l) B)``), the enrichment join changes the semantics
    and the conservative warning should be kept.
    """
    for key in ("left_frag", "right_frag"):
        operand = frag.extra.get(key)
        if not isinstance(operand, PromQLFragment):
            return False
        if not operand.outer_agg:
            return False
        if operand.group_labels:
            return False
        # Operand wraps an inner binary expression — enrichment may have been dropped
        if isinstance(operand.extra.get("inner_frag"), PromQLFragment):
            return False
    matching = frag.extra.get("vector_matching") or {}
    return not matching


# Shared building blocks for every template-variable regex below, so the braced
# (``${var}``) and bracket (``[[var]]``) syntaxes are defined once and cannot
# drift out of sync between the base matcher and the glued-prefix guardrail.
_TV_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_TV_BRACED_FMT = r"(?::[^}]*)?"  # optional ``:format`` modifier inside ``${...}``
_TV_BRACKET_FMT = r"(?::[^\]]+)?"  # optional ``:format`` modifier inside ``[[...]]``
_GRAFANA_TEMPLATE_VAR_RE = re.compile(
    r"\$\{(?P<braced>" + _TV_NAME + r")" + _TV_BRACED_FMT + r"\}"
    r"|\$(?P<plain>" + _TV_NAME + r")"
    r"|\[\[(?P<bracket>" + _TV_NAME + r")" + _TV_BRACKET_FMT + r"\]\]"
)
_RANK_TEMPLATE_LIMIT_RE = re.compile(
    r"\b(?P<func>topk|bottomk)\s*\(\s*"
    r"(?P<token>\$\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}|\$[A-Za-z_][A-Za-z0-9_]*|\[\[[A-Za-z_][A-Za-z0-9_]*(?::[^\]]+)?\]\])"
    r"\s*,",
    re.IGNORECASE,
)
_GROUPING_TEMPLATE_RE = re.compile(r"\b(?P<kw>by|without)\s*\((?P<labels>[^)]*)\)", re.IGNORECASE)
# ``by (...)`` / ``without (...)`` clause, with any leading separator captured so
# a clause that becomes empty (its only label was a template variable) can be
# dropped cleanly without leaving a dangling space.
_GROUPING_TEMPLATE_STRIP_RE = re.compile(
    r"(?P<lead>\s*)\b(?P<kw>by|without)\s*\((?P<labels>[^)]*)\)", re.IGNORECASE
)
# A template variable used in function-call position (e.g. ``${metric:value}(...)``)
# — dynamic function selection (rate/increase/…) that cannot be resolved offline.
_TEMPLATE_FUNC_VAR_RE = re.compile(r"(?:" + _GRAFANA_TEMPLATE_VAR_RE.pattern + r")\s*\(")
# A template variable glued onto an identifier (e.g. ``metric_name${suffix}``) —
# the exact metric/label name is only known after Grafana expands the variable.
_GLUED_TEMPLATE_VAR_RE = re.compile(r"(?<=[A-Za-z0-9_])(?:" + _GRAFANA_TEMPLATE_VAR_RE.pattern + r")")
# A delimited template variable glued as a *prefix* onto an identifier
# (e.g. ``${prefix:raw}metric`` / ``[[prefix]]metric``) — same dynamic-name
# hazard as the suffix form.  The plain ``$var`` alternative is intentionally
# excluded: it has no right delimiter, so ``$metricfoo`` is the whole variable
# ``metricfoo``, not ``$metric`` glued to ``foo`` (and a lookahead would
# false-positive on ``$__rate_interval`` macros and bare ``$metric[5m]``).
# The ``(?!__)`` guards exclude Grafana built-in macros whose names start with
# ``__`` (e.g. ``${__range_s}s``) — those are time-range selectors, not a
# user variable prefixed onto a metric name.  The lookahead includes ``:`` so a
# prefix glued onto a recording-rule name (``${env}:job:rate``) — whose names may
# use ``:`` — is still caught.  Distinguishing a templated *duration* inside a
# range/subquery selector (``metric[${step}m]``, ``metric[5m:${step}m]``) from a
# genuine dynamic metric/label name is *not* done here — a fixed-width lookbehind
# cannot see the enclosing ``[...]`` context, so the guardrail strips range
# selectors first (``_RANGE_SELECTOR_RE``) before running this pattern.
_PREFIX_GLUED_TEMPLATE_VAR_RE = re.compile(
    r"(?:"
    r"\$\{(?!__)" + _TV_NAME + _TV_BRACED_FMT + r"\}"
    r"|\[\[(?!__)" + _TV_NAME + _TV_BRACKET_FMT + r"\]\]"
    r")"
    r"(?=[A-Za-z_:])"
)
# A PromQL range/subquery selector (``[5m]``, ``[${step}m]``, ``[5m:${step}m]``,
# ``[[[win]]m]``).  Its contents are templated *durations*, never metric/label
# names, so it is stripped before the glued-prefix check — otherwise an interval
# variable is misread as a dynamic metric name (a concrete metric would then be
# wrongly blamed).  ``(?<!\[)`` plus the explicit ``[[...]]`` branch protect a
# name-position bracket variable (``[[prefix]]metric``), which must still be
# caught; the ``\$\{...\}`` branch lets a braced interval var carry its own
# braces without ending the selector early.
_RANGE_SELECTOR_RE = re.compile(r"(?<!\[)\[(?:\$\{[^}]*\}|\[\[[^\]]*\]\]|[^\[\]{}])*\]")
# The ``offset`` modifier's value is likewise a templated *duration*
# (``metric offset ${off}h``), not a name; strip it (with any leading sign and
# glued unit) for the same reason.  ``\boffset`` keeps ``offset`` inside a metric
# name (``http_offset_total``) from matching.
_OFFSET_MODIFIER_RE = re.compile(
    r"\boffset\s+-?\s*"
    r"(?:\$\{[^}]*\}|\[\[[^\]]*\]\]|\$[A-Za-z_][A-Za-z0-9_]*|\d[\d.]*)"
    r"[A-Za-z0-9_]*"
)
_STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"])*"|\'(?:\\.|[^\'])*\'')


def _template_var_name(match) -> str:
    return match.group("braced") or match.group("plain") or match.group("bracket") or "var"


def _template_var_display(name: str) -> str:
    return f"${name}"


def _strip_promql_string_literals(expr: str) -> str:
    text = str(expr or "")
    text = re.sub(r'"(?:\\.|[^"])*"', '""', text)
    return re.sub(r"'(?:\\.|[^'])*'", "''", text)


def _apply_outside_string_literals(expr: str, transform) -> str:
    """Apply ``transform`` to the parts of ``expr`` outside string literals.

    String literals are preserved verbatim so a template-variable token that
    only appears inside a matcher value (e.g. ``job=~"$job"``) is never treated
    as query structure.
    """
    out: list[str] = []
    last = 0
    for match in _STRING_LITERAL_RE.finditer(expr):
        out.append(transform(expr[last:match.start()]))
        out.append(match.group(0))
        last = match.end()
    out.append(transform(expr[last:]))
    return "".join(out)


def _rewrite_grouping_template_vars(expr: str) -> tuple[str, list[str], bool]:
    """Strip Grafana template-variable tokens from ``by``/``without`` clauses.

    Grafana's optional-grouping variables (``by (exporter $grouping)``) expand
    to an *extra*, user-selected breakdown whose default/unset state adds no
    dimension. When a clause still names at least one concrete label, the token
    is dropped and the query keeps the explicit grouping (a faithful degrade).

    Returns the rewritten expression, the dropped variable names (in order), and
    whether any clause was left *fully* dynamic (only template variables, no
    concrete label — which the caller must treat as not-feasible).
    """
    dropped: list[str] = []
    fully_dynamic = False

    def transform(segment: str) -> str:
        nonlocal fully_dynamic

        def repl(clause):
            nonlocal fully_dynamic
            labels = clause.group("labels") or ""
            if not _GRAFANA_TEMPLATE_VAR_RE.search(labels):
                return clause.group(0)
            for var in _GRAFANA_TEMPLATE_VAR_RE.finditer(labels):
                name = _template_var_name(var)
                if name not in dropped:
                    dropped.append(name)
            cleaned = _GRAFANA_TEMPLATE_VAR_RE.sub("", labels)
            parts = [part for part in re.split(r"[,\s]+", cleaned) if part]
            if not parts:
                fully_dynamic = True
                return clause.group(0)
            prefix = clause.group(0)[: clause.group(0).index("(") + 1]
            return f"{prefix}{', '.join(parts)})"

        return _GROUPING_TEMPLATE_RE.sub(repl, segment)

    return _apply_outside_string_literals(expr, transform), dropped, fully_dynamic


def _string_literal_spans(expr: str):
    return [
        (m.start(), m.end())
        for m in re.finditer(r'"(?:\\.|[^"])*"|\'(?:\\.|[^\'])*\'', expr or "")
    ]


def _pos_in_spans(pos: int, spans) -> bool:
    return any(start <= pos < end for start, end in spans)


def _late_bound_group_choices(rule_pack, var_name):
    """Return the resolved ES|QL field-control spec for a grouping template var.

    Populated per-dashboard on the rule pack (``_late_bound_group_var_choices``)
    from ``templating.list`` when the target binds ES|QL parameters. ``None``
    means the variable is unknown/unresolvable, so the grouping stays
    ``not_feasible`` (degrade gracefully) instead of emitting a control with no
    selectable fields.
    """
    choices_map = getattr(rule_pack, "_late_bound_group_var_choices", None) or {}
    spec = choices_map.get(var_name)
    if isinstance(spec, dict) and spec.get("choices"):
        return spec
    return None


def _strip_group_template_vars(expr: str, var_names) -> str:
    """Remove late-bound grouping template variables from BY/WITHOUT clauses.

    The variable is re-attached downstream as an ES|QL identifier control
    (``STATS ... BY ??var``); stripping it here lets the concrete labels and the
    rest of the expression translate normally. A clause whose only label was the
    template variable is dropped entirely. Template text inside string literals
    is left untouched (mirrors the guardrail's literal-aware detection).
    """
    if not expr or not var_names:
        return expr
    targets = set(var_names)
    literal_spans = _string_literal_spans(expr)

    def _rewrite(match):
        if _pos_in_spans(match.start("kw"), literal_spans):
            return match.group(0)
        kept = []
        for token in re.split(r"[\s,]+", (match.group("labels") or "").strip()):
            if not token:
                continue
            var_match = _GRAFANA_TEMPLATE_VAR_RE.fullmatch(token)
            if var_match and _template_var_name(var_match) in targets:
                continue
            kept.append(token)
        if not kept:
            return ""
        return f"{match.group('lead')}{match.group('kw')} ({', '.join(kept)})"

    return _GROUPING_TEMPLATE_STRIP_RE.sub(_rewrite, expr)


def _try_defer_late_bound_grouping(context, stripped_expr):
    """Attempt to defer a ``by ($var)`` grouping to a Kibana ES|QL field control.

    Issue #282: a *pure* single positive grouping variable — ``by ($grouping)``
    with no concrete label alongside it — becomes an interactive ES|QL
    identifier/field control (``STATS ... BY grouping = ??grouping``) when the
    target binds ES|QL parameters and the variable resolves to a set of
    selectable fields. On success the token is stripped from the BY clause (it
    is re-attached downstream as ``??var``), the variable is recorded on the
    context, and a reason is returned.

    Every richer shape is intentionally *not* deferred and returns ``None`` so
    the caller falls back to the graceful strip/degrade path:

    * A concrete label alongside the variable (``by (exporter, $grouping)``) —
      one shared Lens breakdown accessor cannot safely follow a field control
      whose choices may collide with the concrete grouping column, so the
      concrete grouping is kept and the optional selector is dropped instead
      (this is the fix for the collision / "invalid column" render error).
    * ``without`` variables (ES|QL grouping is positive; an excluded dimension
      has no faithful control), multiple variables (a single XY breakdown cannot
      host several independent field controls), or an unresolvable/unsupported
      target.
    """
    by_vars: list[str] = []
    without_vars: list[str] = []
    by_has_concrete = False
    for grouping_match in _GROUPING_TEMPLATE_RE.finditer(stripped_expr):
        kw = grouping_match.group("kw").lower()
        labels = grouping_match.group("labels") or ""
        clause_vars = [
            _template_var_name(var_match)
            for var_match in _GRAFANA_TEMPLATE_VAR_RE.finditer(labels)
        ]
        concrete = [
            part
            for part in re.split(r"[,\s]+", _GRAFANA_TEMPLATE_VAR_RE.sub("", labels))
            if part
        ]
        if kw == "by":
            for name in clause_vars:
                if name not in by_vars:
                    by_vars.append(name)
            if concrete:
                by_has_concrete = True
        else:
            for name in clause_vars:
                if name not in without_vars:
                    without_vars.append(name)

    field_control_spec = (
        _late_bound_group_choices(context.rule_pack, by_vars[0])
        if len(by_vars) == 1
        else None
    )
    deferrable = (
        len(by_vars) == 1
        and not without_vars
        and not by_has_concrete
        and binds_esql_named_params(context.rule_pack)
        and field_control_spec is not None
    )
    if not deferrable:
        return None

    context.promql_expr = _strip_group_template_vars(context.promql_expr or "", by_vars)
    recorded = context.metadata.setdefault("late_bound_group_vars", [])
    for name in by_vars:
        if name not in recorded:
            recorded.append(name)
    choices = list(field_control_spec.get("choices") or [])
    default = field_control_spec.get("default") or (choices[0] if choices else None)
    if default:
        context.metadata.setdefault("esql_identifier_param_defaults", {})[by_vars[0]] = default
    return "deferred BY template variable to ES|QL field control: " + _template_var_display(by_vars[0])


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

    # A template variable used as a function name (``${metric:value}(...)``)
    # selects the rate/increase/... function dynamically — unknowable offline.
    func_var = _TEMPLATE_FUNC_VAR_RE.search(expr)
    if func_var:
        var_name = _template_var_name(func_var)
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        _append_unique(
            context.warnings,
            f"PromQL function name comes from a Grafana template variable ({_template_var_display(var_name)}); "
            "dynamic function selection (e.g. rate/increase/sum_over_time) is unknown at migration "
            "time and requires manual redesign",
        )
        return "dynamic function name requires manual redesign"

    # A template variable glued onto an identifier — either as a suffix
    # (``otelcol_..._spans${suffix}``) or as a delimited prefix
    # (``${prefix:raw}metric_name``) — makes the exact metric/label name dynamic;
    # resolving it to a placeholder would silently query a non-existent field,
    # so block it honestly.  The prefix form uses a separate regex because the
    # plain ``$var`` alternative cannot be a glued prefix (no right delimiter).
    # Range/subquery selectors and the ``offset`` modifier are stripped first so a
    # templated *duration* (``metric[${step}m]``, ``metric[5m:${step}m]``,
    # ``metric offset ${off}h``) is never mistaken for a dynamic metric/label
    # name — the surrounding metric there is concrete.
    glued_scan = _OFFSET_MODIFIER_RE.sub(" ", _RANGE_SELECTOR_RE.sub(" ", expr))
    glued_var = _GLUED_TEMPLATE_VAR_RE.search(glued_scan) or _PREFIX_GLUED_TEMPLATE_VAR_RE.search(glued_scan)
    if glued_var:
        inner = _GRAFANA_TEMPLATE_VAR_RE.search(glued_var.group(0))
        var_name = _template_var_name(inner) if inner else "var"
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        _append_unique(
            context.warnings,
            f"PromQL metric or label name is built from a Grafana template variable "
            f"({_template_var_display(var_name)}); the exact series is unknown at migration "
            "time and requires manual redesign",
        )
        return "dynamic metric/label name requires manual redesign"

    # A ``by``/``without`` clause with a Grafana template variable. Issue #282:
    # a *pure* single positive grouping variable (``by ($grouping)`` with no
    # concrete label) is deferred to a Kibana ES|QL identifier/field control when
    # the target binds ES|QL parameters and the variable resolves to selectable
    # fields. Everything else degrades gracefully: a variable alongside concrete
    # labels (``by (exporter $grouping)``) drops the optional selector token and
    # keeps the explicit grouping, and a clause that is *only* a template
    # variable has no resolvable dimension and stays not-feasible.
    if _GROUPING_TEMPLATE_RE.search(expr) and _GRAFANA_TEMPLATE_VAR_RE.search(expr):
        deferred = _try_defer_late_bound_grouping(context, expr)
        if deferred is not None:
            return deferred
        rewritten, dropped, fully_dynamic = _rewrite_grouping_template_vars(context.promql_expr or "")
        if fully_dynamic:
            var_match = _GRAFANA_TEMPLATE_VAR_RE.search(
                next(
                    (m.group("labels") for m in _GROUPING_TEMPLATE_RE.finditer(expr)
                     if _GRAFANA_TEMPLATE_VAR_RE.search(m.group("labels") or "")),
                    "",
                )
            )
            var_name = _template_var_name(var_match) if var_match else "var"
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            _append_unique(
                context.warnings,
                f"BY/WITHOUT clause contains Grafana template variable ({_template_var_display(var_name)}); "
                "grouping dimension is unknown at migration time and requires manual redesign",
            )
            return "grouping template variable requires manual redesign"
        if dropped:
            context.promql_expr = rewritten
            display = ", ".join(_template_var_display(name) for name in dropped)
            _append_unique(
                context.warnings,
                f"optional Grafana template-variable grouping dimension ({display}) could not be "
                "resolved at migration time and was omitted; the panel is grouped by its explicit "
                "label(s) only. Re-add the breakdown in Kibana if the dashboard needs it.",
            )
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
    # Carry late-bound grouping variables (issue #282) onto the fragment as ES|QL
    # identifier controls so ``_frag_group_labels`` re-attaches them to the
    # STATS ... BY clause after the concrete labels resolve.
    late_bound = context.metadata.get("late_bound_group_vars")
    if late_bound and context.fragment is not None:
        context.fragment.extra["late_bound_group_identifiers"] = [f"??{name}" for name in late_bound]
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


def _mixed_os_zero_fill_left_is_feasible(frag):
    """True when ``linux + on(ns) (windows_join or zero_fill)`` should defer to the rewrite.

    The Windows join stamps ``not_feasible_reasons`` onto the outer ``+`` via
    reason merging. Classifiers must not short-circuit before
    ``binary_expr_family_rule`` / ``_try_rewrite_mixed_os_zero_fill_plus`` can
    prefer the feasible Linux left operand. Also covers the community form
    where the ``OR`` is nested inside ``sum(...) by (namespace)``.
    """
    if frag is None or frag.family != "binary_expr":
        return False
    if (frag.binary_op or "").lower() not in {"+", "-"}:
        return False
    left = frag.extra.get("left_frag")
    right = frag.extra.get("right_frag")
    if left is None or right is None:
        return False
    if left.extra.get("not_feasible_reasons"):
        return False
    return _mixed_os_or_operands(right) is not None


def _binary_left_fallback_is_feasible(frag):
    """Defer classifier not_feasible when a left-preferring rewrite can still run."""
    return _or_left_is_feasible(frag) or _mixed_os_zero_fill_left_is_feasible(frag)


@QUERY_CLASSIFIERS.register("colocated_binary_agg_unblock", priority=0)
def colocated_binary_agg_unblock(context):
    """Clear the ``agg(A op B)`` refusal when the arithmetic renders exactly.

    Runs before ``fragment_guardrails`` (priority 1), which would otherwise turn
    the parser's reason into a dead panel. Only clears when
    ``colocated_binary_agg_plan`` -- a closed allowlist -- can render the whole
    tree, so genuinely unaligned joins keep their refusal.
    """
    frag = context.fragment
    extra = getattr(frag, "extra", None) if frag else None
    if not isinstance(extra, dict) or not extra.get("not_feasible_reasons"):
        return None
    if colocated_binary_agg_plan(frag, context.resolver, context.rule_pack) is None:
        return None
    extra.pop("not_feasible_reasons", None)
    return "cleared not_feasible for co-located per-element arithmetic"


@QUERY_CLASSIFIERS.register("agg_over_or_operand_drop", priority=0)
def agg_over_or_operand_drop_rule(context):
    """Refuse ``agg(A or B)`` when no reduction can elect a single operand.

    The parse-time guard in ``_ast_aggregate_fragment`` hands ``or`` on because
    its two reductions -- the same-metric range-window fallback and the
    live-absent operand drop -- need a resolver it does not have. Nothing
    downstream then claimed the fragment, so the generic
    ``fragment_extract``/``stats_expression`` fallback rebuilt
    ``agg(<first metric leaf>)`` from the fragment's summary fields and shipped
    it clean: ``count(node_a or node_b)`` became ``COUNT(node_a)`` with no
    warning (issue #434). The same fallback still fires when the ``or`` sits
    under a *nested* aggregation (``sum(count(A or B))``, ``max(sum(A or B))``),
    because the ``or`` is on the inner fragment rather than the root's
    ``inner_frag``. This rule therefore walks every nested ``agg(A or B)``.

    The bare chain keeps both operands (``COALESCE`` /
    unified ``WHERE ... OR``) or refuses outright, so the wrapper was inverting
    the verdict on the identical expression.

    Runs at priority 0, ahead of ``fragment_guardrails`` (1), which turns the
    reason into the refusal. Order against ``colocated_binary_agg_unblock``
    (also 0) does not matter: that rule only clears reasons when
    ``colocated_binary_agg_plan`` can render, which requires the same reduction
    to have elected an operand -- exactly the case this rule stays silent for.
    """
    details = []
    for current, reduction in iter_agg_over_or_reductions(
        context.fragment, context.resolver
    ):
        if reduction.preferred is None:
            reason = _agg_over_or_not_feasible_reason(
                current.outer_agg, _fragment_metric_names(reduction.chain)
            )
            # Tag the root fragment so fragment_guardrails (priority 1) sees
            # the reason. Nested aggregations store the ``or`` on an inner
            # fragment whose own not_feasible_reasons would otherwise never
            # reach the panel.
            _append_not_feasible_reason(context.fragment, reason)
            return reason
        if reduction.dropped_absent:
            _append_unique(context.warnings, _ABSENT_OR_OPERAND_WARNING)
            details.append("disclosed live-absent 'or' operand drop")
        if reduction.dropped_fallback:
            _append_unique(
                context.warnings, _same_metric_range_fallback_warning(reduction.chain)
            )
            details.append("disclosed dropped same-metric range-window fallback")
    return "; ".join(details) or None


@QUERY_CLASSIFIERS.register("fragment_guardrails", priority=1)
def fragment_guardrails_rule(context):
    frag = context.fragment
    if not frag:
        return None
    reasons = list(frag.extra.get("not_feasible_reasons", []) or [])
    if not reasons:
        return None
    if _binary_left_fallback_is_feasible(frag):
        return None  # let binary_expr_family_rule handle left-preferring rewrites
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
        "label_join",
    }
    if frag.family in families_that_bypass_patterns:
        nf_reasons = frag.extra.get("not_feasible_reasons") or []
        if nf_reasons:
            if _binary_left_fallback_is_feasible(frag):
                context.metadata["fragment_family"] = frag.family
                return (
                    f"fragment family {frag.family}: right-side not_feasible reasons "
                    "deferred to left-preferring rewrite"
                )
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            for r in nf_reasons:
                _append_unique(context.warnings, r)
            return f"fragment family {frag.family} has not-feasible reasons: {nf_reasons}"
        context.metadata["fragment_family"] = frag.family
        return f"fragment family {frag.family} bypasses unsupported-pattern check"
    return None


@QUERY_CLASSIFIERS.register("join_label_enrichment_check", priority=6)
def join_label_enrichment_check_rule(context):
    """Gate the safe-subset aggregated group_left join rewrite on the RHS metric name.

    ``_ast_aggregate_fragment`` proves the rewrite structurally safe (a
    group_left(...) multiplication join whose outer by()/without() doesn't need
    an enrichment-only label) and stashes the join's RHS metric name in
    ``pending_join_rhs_metric`` — but it can't check that name against
    ``info_metric_suffixes`` because the rule pack isn't available during
    parsing. Do that check here, now that ``context.rule_pack`` is in scope
    (issue #197). The marker can be nested (e.g. a ratio of two aggregated
    joins parses as a binary_expr wrapping two reclassified join operands), so
    walk the whole fragment tree rather than only the top-level fragment.
    """
    frag = context.fragment
    if not frag:
        return None
    pending_frags = list(_iter_pending_join_rhs_fragments(frag))
    if not pending_frags:
        return None
    for pending_frag in pending_frags:
        pending_metric = pending_frag.extra.get("pending_join_rhs_metric")
        if not _is_label_enrichment_metric(pending_metric, context.rule_pack):
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            reason = (
                f"Aggregating over a PromQL vector-matching join with '{pending_metric}' requires "
                "manual redesign: not recognized as a label-only metric (no info_metric_suffixes "
                "match, default '_info'); dropping it would change numeric values unless it always "
                "evaluates to 1 — if it does, add its suffix to info_metric_suffixes in your rule pack"
            )
            _append_unique(context.warnings, reason)
            return reason
        # The RHS is a confirmed info metric.  Any labels that the outer by()
        # borrows from the group_left(...) enrichment list (stashed as
        # ``pending_join_enrichment_labels`` at parse time because rule_pack was
        # unavailable) are now promoted to ``pending_join_verify_labels`` so the
        # schema-check pass below can warn about missing dimensions while keeping
        # the panel feasible.
        enrichment_overlap = pending_frag.extra.get("pending_join_enrichment_labels") or []
        if enrichment_overlap:
            existing = list(pending_frag.extra.get("pending_join_verify_labels") or [])
            pending_frag.extra["pending_join_verify_labels"] = existing + [
                lbl for lbl in enrichment_overlap if lbl not in existing
            ]

    # A by()/without() label that is neither an on(...) match key nor a
    # group_left(...) enrichment label is assumed to survive on the primary
    # metric after the join RHS is dropped. When live target field capabilities
    # are available and the resolved grouping field is absent, the panel still
    # translated into valid ES|QL — the field is simply not (yet) ingested. Per
    # issue #187 that is a transient *data readiness* condition, not a
    # translation infeasibility, and must not flip the feasibility verdict (which
    # would otherwise depend on whether ``--es-url`` was supplied). Surface it as
    # a data-readiness warning and keep the panel feasible.
    resolver = context.resolver
    if resolver is not None and getattr(resolver, "has_field_capabilities", None) and resolver.has_field_capabilities():
        for pending_frag in pending_frags:
            verify_labels = pending_frag.extra.get("pending_join_verify_labels") or []
            if not verify_labels:
                continue
            primary_metric = pending_frag.metric or ""
            metric_field = (
                resolver.resolve_metric_field(primary_metric)
                if primary_metric and hasattr(resolver, "resolve_metric_field")
                else primary_metric
            )
            missing = []
            for label in verify_labels:
                resolved = resolver.resolve_label(label, metric_field=metric_field)
                if not resolved or resolver.field_exists(resolved) is False:
                    missing.append(label)
            if missing:
                labels_text = ", ".join(missing)
                _append_unique(
                    context.warnings,
                    f"Grouping field {labels_text} is missing from live schema discovery "
                    "(data readiness, not translation infeasibility): after dropping the "
                    f"group_left(...) enrichment, '{labels_text}' is not present on the primary "
                    f"metric '{primary_metric}' in the target yet, so this panel needs that "
                    "field ingested (or the grouping dimension dropped) to return rows",
                )

    rhs_metrics = sorted(
        {
            pending_frag.extra.get("pending_join_rhs_metric")
            for pending_frag in pending_frags
            if pending_frag.extra.get("pending_join_rhs_metric")
        }
    )
    rhs_text = ", ".join(f"'{metric}'" for metric in rhs_metrics) or "the join partner"
    dropped_filters = sorted(
        {
            pending_frag.extra.get("pending_join_rhs_filters")
            for pending_frag in pending_frags
            if pending_frag.extra.get("pending_join_rhs_filters")
        }
    )
    filter_clause = ""
    if dropped_filters:
        filter_clause = (
            f" Label filters on the partner ({'; '.join(dropped_filters)}) were also dropped, "
            "so the aggregation may span series (e.g. other clusters/namespaces) the original "
            "query excluded."
        )
    _append_unique(
        context.warnings,
        f"Dropped group_left label-enrichment join on {rhs_text} (assumed a constant-1 "
        "label-only metric by naming convention) and aggregated the primary metric alone."
        f"{filter_clause} Primary series with no matching join partner are kept — PromQL "
        "would drop them — so counts and totals may differ; verify the partner is truly a "
        "label-only metric.",
    )
    return f"{len(pending_frags)} join(s) recognized as label-only; RHS stripped"


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


@QUERY_TRANSLATORS.register("colocated_binary_agg_family", priority=0)
def colocated_binary_agg_family_rule(context):
    """``agg(A op B)`` over operands that share a label set.

    Evaluates the arithmetic per document and aggregates the result, which is
    exactly what the source asked for. Two curated packs (grafana-763 memory
    ratio, grafana-14091 hit ratio) each hand-wrote this query; this retires
    both.

    Priority 0 matters: the generic ``fragment_extract``/``stats_expression``
    stages (20/90) build ``agg(<frag.metric>)`` from fragment fields and know
    nothing about binary expressions, so letting them win drops an operand
    silently.
    """
    frag = context.fragment
    plan = colocated_binary_agg_plan(frag, context.resolver, context.rule_pack)
    if plan is None:
        return None
    value_expr, leaf = plan
    rp = context.rule_pack

    filters, had_vars = _frag_filters(leaf, context.resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven label filters during migration")

    # TS: the operands are per-series functions (RATE/IRATE) or raw gauges, and
    # FROM over a TSDS would inflate them by the per-bucket sample count.
    group_fields = _frag_group_labels(
        frag,
        context.resolver,
        context.metadata.get("preferred_group_labels"),
        preferred_origin=context.metadata.get("preferred_group_labels_origin"),
    )
    group_by_parts, output_group = _grouping_parts(rp.ts_bucket, group_fields, frag)

    alias = "computed_value"
    parts = [
        f"TS {context.index}",
        f"| WHERE {rp.ts_time_filter}",
        *_build_where_lines(filters),
    ]
    # Require every operand on the row. Co-location is the precondition for
    # evaluating the arithmetic per document, so a row missing either metric is
    # not a valid sample of the expression -- without this the aggregate silently
    # loses whole series (measured: 1.23 instead of 42.41, one of two instances).
    inner = (getattr(frag, "extra", {}) or {}).get("inner_frag")
    for metric_field in colocated_metric_fields(inner, context.resolver):
        parts.append(f"| WHERE {_esql_identifier(metric_field)} IS NOT NULL")
    stats_line = f"| STATS {alias} = {OUTER_AGG_MAP[(frag.outer_agg or '').lower()]}({value_expr})"
    if group_by_parts:
        stats_line += f" BY {', '.join(group_by_parts)}"
    parts.append(stats_line)

    context.parser_backend = "fragment"
    context.source_type = "TS"
    context.metric_name = alias
    context.output_metric_field = alias
    context.output_group_fields = output_group

    collapsed = None
    if _summary_mode_from_metadata(context.metadata):
        collapsed = _collapse_summary_ts_query(
            parts, output_group, [alias],
            keep_time_bucket=context.panel_type in {"table", "table-old"},
            reduce_calc=context.metadata.get("reduce_calc", ""),
        )
    if collapsed is None:
        if "time_bucket" in output_group:
            parts.append("| SORT time_bucket ASC")
    else:
        output_group = collapsed
    context.esql_query = "\n".join(parts)
    context.output_group_fields = output_group
    context.translation_complete = True
    _append_unique(
        context.warnings,
        "Per-element arithmetic between co-located metrics evaluated per document "
        "before aggregation (exact for Prometheus layouts that store one document "
        "per label-set; PromQL's all-label matching guarantees the operands align)",
    )
    return "translated co-located per-element arithmetic"


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

    _logql_summary = _summary_mode_from_metadata(context.metadata)
    context.parser_backend = "fragment"
    context.source_type = "FROM"
    context.index = rp.logs_index
    context.metric_name = "log_count"
    context.output_metric_field = "log_count"
    context.output_group_fields = [] if _logql_summary else ["time_bucket"]
    _logql_lines = [
        f"FROM {rp.logs_index}",
        f"| WHERE {rp.from_time_filter}",
        *_build_where_lines(filters),
        f"| STATS log_count = COUNT(*) BY {rp.from_bucket}",
    ]
    if _logql_summary:
        _logql_lines.append("| STATS log_count = MAX(log_count)")
        _logql_lines.append("| KEEP log_count")
    else:
        _logql_lines.append("| SORT time_bucket ASC")
    context.esql_query = "\n".join(_logql_lines)
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
    uptime_arg = physical_metric
    if _counter_unsafe_cast_needed(physical_metric, resolver):
        uptime_arg = f"TO_DOUBLE({physical_metric})"
        _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))

    context.parser_backend = "fragment"
    context.source_type = "FROM"
    context.metric_name = start_metric
    context.output_metric_field = result_alias
    context.output_group_fields = group_fields
    stats_line = f"| STATS start_time_ms = MAX({uptime_arg} * 1000)"
    if group_fields:
        stats_line += f" BY {', '.join(_expand_late_bound_group_by_terms(group_fields, frag))}"
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
            _ratio_summary = _summary_mode_from_metadata(context.metadata)
            # The STATS BY clause always includes the bucket to correctly scope
            # aggregation to the selected time range via TBUCKET parameter binding.
            # For summary-mode panels (stat/gauge), time_bucket is dropped from the
            # output so metric_panel_rule doesn't promote the panel to a datatable.
            output_group = (list(join_labels) if _ratio_summary else ["time_bucket"] + join_labels)
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
            left_metric_field = _resolve_frag_metric_field(left_frag, resolver, prefer=left_prefer)
            right_metric_field = _resolve_frag_metric_field(right_frag, resolver, prefer=right_prefer)
            for side_metric, side_is_counter, side_inner_func in (
                (left_metric_field, left_is_counter, left_inner_func),
                (right_metric_field, right_is_counter, right_inner_func),
            ):
                if (
                    not side_is_counter
                    and (side_inner_func or "").upper() not in _COUNTER_INPUT_ESQL_FUNCS
                    and _counter_unsafe_cast_needed(side_metric, resolver)
                ):
                    _append_unique(context.warnings, _counter_unsafe_cast_warning(side_metric, resolver))

            left_stats_call = _build_stats_call(
                left_info["outer_agg"],
                left_inner_func,
                left_metric_field,
                left_info["range_window"],
                left_frag,
                is_counter=left_is_counter,
                resolver=resolver,
            )
            right_stats_call = _build_stats_call(
                right_info["outer_agg"],
                right_inner_func,
                right_metric_field,
                right_info["range_window"],
                right_frag,
                is_counter=right_is_counter,
                resolver=resolver,
            )
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

            # Keep CASE-shaped and bare TS value args from mixing in one STATS
            # (same ClassCast class as multi-target merge).
            stats_assignments = _finalize_fused_stats_assignments(
                [
                    f"numerator = {left_stats_call}",
                    f"denominator = {right_stats_call}",
                ],
                group_fields=output_group,
                source_type="TS",
            )
            left_stats_call = stats_assignments[0].split("=", 1)[1].strip()
            right_stats_call = stats_assignments[1].split("=", 1)[1].strip()

            context.parser_backend = "fragment"
            context.source_type = "TS"
            context.metric_name = left_frag.metric
            context.output_metric_field = result_alias
            context.output_group_fields = output_group
            _ratio_by = f" BY {', '.join(group_by_parts)}" if group_by_parts else ""
            _ratio_lines = [
                f"TS {context.index}",
                f"| WHERE {rp.ts_time_filter}",
                *common_filters,
                f"| STATS numerator = {left_stats_call}, denominator = {right_stats_call}{_ratio_by}",
                f"| EVAL {result_alias} = numerator / denominator",
                f"| KEEP {_keep(output_group, result_alias)}",
            ]
            if not _ratio_summary:
                _ratio_lines.append("| SORT time_bucket ASC")
            context.esql_query = "\n".join(_ratio_lines)
            context.translation_complete = True
            # A label-aligned per-key ratio of aggregates (``sum(rate(A)) /
            # on(k) group_left sum(rate(B))``) is exact only when every source
            # dimension survives the grouping, so report it clean then (issue
            # #156). If an operand keyed by extra labels that were collapsed
            # (review #164) — or there is no ``on(...)`` key — keep the caveat.
            if not _join_is_faithful(frag, resolver, output_group):
                _append_unique(
                    context.warnings,
                    "Approximated PromQL join ratio as same-bucket ES|QL ratio",
                )
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
                    _lhs_collapsed = None
                    if _summary_mode_from_metadata(context.metadata):
                        _lhs_collapsed = _collapse_summary_ts_query(
                            parts, output_group_fields, [result_alias],
                            keep_time_bucket=context.panel_type in {"table", "table-old"},
                            reduce_calc=context.metadata.get("reduce_calc", ""),
                        )
                    if _lhs_collapsed is None:
                        parts.append(f"| KEEP {_keep(output_group_fields, result_alias)}")
                        if "time_bucket" in output_group_fields:
                            parts.append("| SORT time_bucket ASC")
                    else:
                        output_group_fields = _lhs_collapsed
                    for warning in plan.warnings:
                        _append_unique(context.warnings, warning)
                    for spec in plan.specs:
                        for warning in spec.warnings:
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
        base_group_fields = list(preferred_group_labels or join_labels)
        # ``A * on(k) group_left(l) B`` enriches A with label ``l`` from the
        # co-scraped info metric B (value 1). The label lands as a field on the
        # same data stream, so carry it into the grouping rather than dropping
        # it — no join needed (issue #156). Scoped to group_left (ManyToOne):
        # group_right flips the primary side and is left on the legacy path.
        matching = frag.extra.get("vector_matching") or {}
        enrichment_raw = (
            frag.extra.get("enrichment_labels", [])
            if matching.get("cardinality") == "ManyToOne"
            else []
        )
        enrichment_labels = (
            resolver.resolve_labels(enrichment_raw) if resolver else list(enrichment_raw)
        )
        candidate_group_fields = list(base_group_fields)
        for label in enrichment_labels:
            if label and label not in candidate_group_fields:
                candidate_group_fields.append(label)
        candidate_output = (
            ["time_bucket"] + candidate_group_fields if candidate_group_fields else ["time_bucket"]
        )
        # Carry the enrichment and report clean only when no source dimension is
        # collapsed (review #164). If the left operand keyed by extra labels that
        # the grouping can't retain, fall back to the honest degrade: drop the
        # join RHS and warn, without inventing enrichment-only columns.
        carry_enrichment = bool(enrichment_labels) and _join_is_faithful(
            frag, resolver, candidate_output
        )
        if carry_enrichment:
            group_fields = candidate_group_fields
        else:
            group_fields = base_group_fields
        _join_summary = _summary_mode_from_metadata(context.metadata)
        output_group = list(group_fields) if _join_summary else (["time_bucket"] + group_fields if group_fields else ["time_bucket"])
        default_agg = rp.default_gauge_agg.upper()
        is_counter = resolver.is_counter(metric_name) if resolver else _is_counter_fallback(metric_name, rp)
        source = "TS" if is_counter else "FROM"
        time_filter = rp.ts_time_filter if is_counter else rp.from_time_filter
        bucket = rp.ts_bucket if is_counter else rp.from_bucket
        physical_metric = _resolve_metric_field(
            resolver, metric_name, prefer="counter" if is_counter else "gauge"
        )
        join_agg_arg = physical_metric
        if not is_counter and _counter_unsafe_cast_needed(physical_metric, resolver):
            join_agg_arg = f"TO_DOUBLE({physical_metric})"
            _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))

        context.parser_backend = "fragment"
        context.source_type = source
        context.metric_name = metric_name
        context.output_metric_field = metric_alias
        context.output_group_fields = output_group
        by_clause = bucket + (f", {', '.join(group_fields)}" if group_fields else "")
        _join_lines = [
            f"{source} {context.index}",
            f"| WHERE {time_filter}",
            *_build_where_lines(filters),
            f"| WHERE {physical_metric} IS NOT NULL",
            f"| STATS {metric_alias} = {default_agg}({join_agg_arg}) BY {by_clause}",
        ]
        if not _join_summary:
            _join_lines.append("| SORT time_bucket ASC")
        context.esql_query = "\n".join(_join_lines)
        context.translation_complete = True
        if not carry_enrichment:
            # Nothing carried (bare/ambiguous group modifier, group_right, or a
            # collapsed source dimension): the join RHS is dropped, so keep the
            # honest degrade warning.
            _append_unique(
                context.warnings,
                "Dropped group_left label enrichment; kept primary metric series only",
            )
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
        _join_left_summary = _summary_mode_from_metadata(context.metadata)
        output_group = list(join_labels) if _join_left_summary else (["time_bucket"] + join_labels if join_labels else ["time_bucket"])
        is_counter = resolver.is_counter(left_frag.metric) if resolver else _is_counter_fallback(left_frag.metric, rp)
        source = "TS" if (is_counter or left_frag.range_func in AGG_FUNCTION_MAP) else "FROM"
        time_filter = rp.ts_time_filter if source == "TS" else rp.from_time_filter
        bucket = rp.ts_bucket if source == "TS" else rp.from_bucket
        if left_frag.range_func in {"rate", "irate", "increase"} or is_counter:
            prefer = "counter"
        else:
            prefer = "gauge"
        physical_metric = _resolve_frag_metric_field(left_frag, resolver, prefer=prefer)

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
            join_cast_needed = _counter_unsafe_cast_needed(physical_metric, resolver)
            if (
                not is_counter
                and (esql_inner or "").upper() not in _COUNTER_INPUT_ESQL_FUNCS
                and join_cast_needed
            ):
                _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))
            inner_expr = f"{esql_inner}({_counter_safe_metric_arg(esql_inner, physical_metric, is_counter, left_frag.range_func, counter_refuted=_counter_refuted(resolver, left_frag.metric), force_cast=join_cast_needed)}, {w})"
        elif is_counter:
            inner_expr = _range_call("RATE", physical_metric, rp.default_rate_window)
        else:
            inner_expr = physical_metric

        outer = OUTER_AGG_MAP.get(left_frag.outer_agg or "avg", "AVG")
        stats_expr = _agg_stats_expr(outer, inner_expr, left_frag, resolver)
        by_clause = bucket + (f", {', '.join(join_labels)}" if join_labels else "")

        context.parser_backend = "fragment"
        context.source_type = source
        context.metric_name = left_frag.metric
        context.output_metric_field = metric_alias
        context.output_group_fields = output_group
        _join_left_lines = [
            f"{source} {context.index}",
            f"| WHERE {time_filter}",
            *_build_where_lines(filters),
            f"| STATS {metric_alias} = {stats_expr} BY {by_clause}",
        ]
        if not _join_left_summary:
            _join_left_lines.append("| SORT time_bucket ASC")
        context.esql_query = "\n".join(_join_left_lines)
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
        # A cross-metric ``or`` reaches here only when it could not be unified
        # into a COALESCE union (``_try_rewrite_set_or_cross_metric`` returned
        # ``None`` because the operands cannot be lined up safely). ``and`` /
        # ``unless`` likewise have no honest single-stage ES|QL equivalent.
        # Flag for manual review instead of silently dropping half the data
        # (issue #167) — never emit the left operand alone.
        #
        # Exception: the Grafana same-metric range-window fallback idiom
        # (``rate(M[$i]) or irate(M[5m])``, including ``topk(rate) or
        # topk(irate)``). Formula-planable left operands are rewritten inside
        # ``_build_formula_plan``; families handled by a dedicated translator
        # (notably ``topk``) re-dispatch the left operand here.
        op_lower = (frag.binary_op or "").lower()
        if op_lower == "or":
            left = _left_operand_of_same_metric_range_fallback(frag)
            if left is not None:
                sub = TranslationContext(
                    promql_expr=left.raw_expr or context.promql_expr,
                    data_view=context.data_view,
                    index=context.index,
                    rule_pack=context.rule_pack,
                    resolver=context.resolver,
                    metadata=dict(context.metadata),
                )
                sub.fragment = left
                sub.metadata["fragment_family"] = left.family
                QUERY_TRANSLATORS.apply(sub, stop_when=lambda ctx, _: ctx.translation_complete)
                QUERY_POSTPROCESSORS.apply(sub)
                if sub.esql_query and sub.feasibility != "not_feasible":
                    context.esql_query = sub.esql_query
                    context.metric_name = sub.metric_name
                    context.output_metric_field = sub.output_metric_field
                    context.output_group_fields = sub.output_group_fields
                    context.source_type = sub.source_type
                    context.parser_backend = sub.parser_backend or "fragment"
                    context.feasibility = sub.feasibility
                    context.confidence = sub.confidence
                    for warning in sub.warnings:
                        _append_unique(context.warnings, warning)
                    _append_unique(
                        context.warnings,
                        _same_metric_range_fallback_warning(frag),
                    )
                    context.translation_complete = True
                    return "translated same-metric range-fallback 'or' via left operand"
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            context.translation_complete = True
            if _or_chain_has_vector_matching(frag):
                reason = (
                    "PromQL 'or' with an on()/ignoring() vector-matching modifier: "
                    "the union must match series by the modifier's label key, which "
                    "the ES|QL COALESCE rewrite cannot reproduce; marked for manual "
                    "review so right-operand series are not over-reported"
                )
            else:
                reason = (
                    "PromQL 'or' between metrics that cannot be aligned in ES|QL "
                    "(differing grouping dimensions or source shapes); marked for "
                    "manual review so no series are silently dropped"
                )
            _append_unique(context.warnings, reason)
            return "or union not alignable; marked not_feasible"
        if op_lower in {"and", "unless"}:
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            context.translation_complete = True
            _append_unique(
                context.warnings,
                f"PromQL set operator '{op_lower}' between distinct metrics or aggregations "
                "has no honest ES|QL translation; marked not_feasible",
            )
            return "set operator not feasible"
        if op_lower in ("+", "-", "*", "/"):
            # ``_build_formula_plan`` returned None because at least one
            # operand contains a nested pattern it cannot honestly express
            # (e.g. an outer aggregation wrapping a set operator or a
            # vector-matching join it can't line up — the "windows or
            # linux-only zero-fill" idiom seen in Kubernetes mixed-OS
            # dashboards: ``sum(rate(A)) + on(ns) (sum(rate(B) * on(...)
            # group_left(...) C) or D * 0)``). Falling through to the
            # generic fragment_extract fallback below would silently keep
            # only one bare metric name from one operand and drop the rest
            # of the arithmetic entirely — a much larger semantic gap than
            # a normal approximation. Mark not_feasible instead so the gap
            # is visible rather than hidden in an unexplained partial value.
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            context.translation_complete = True
            _append_unique(
                context.warnings,
                f"PromQL '{frag.binary_op}' arithmetic where an operand contains a "
                "nested set operator or vector-matching join that cannot be safely "
                "combined; marked for manual review so data is not silently dropped",
            )
            return "arithmetic operand unsupported; marked not_feasible"
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
                collapsed = _collapse_summary_ts_query(
                    parts, output_group_fields, [result_alias],
                    keep_time_bucket=context.panel_type in {"table", "table-old"},
                            reduce_calc=context.metadata.get("reduce_calc", ""),
                        )
            if collapsed is None:
                parts.append(f"| KEEP {_keep(output_group_fields, result_alias)}")
                if "time_bucket" in output_group_fields:
                    parts.append("| SORT time_bucket ASC")
            else:
                output_group_fields = collapsed
        context.output_group_fields = output_group_fields
        # A label-aligned ``A op on(k) B`` is numerically identical to the
        # source PromQL only when no source dimension is collapsed — then it
        # must migrate clean, since flagging it would wrongly signal degradation
        # (issue #156). Plain arithmetic (no matcher), ``ignoring(...)`` joins,
        # and joins that dropped an operand ``by(...)`` label (review #164) keep
        # the same-bucket caveat. A cross-metric ``or`` union carries its own
        # set-union note (issue #167) and is not arithmetic, so skip it.
        # Same-metric ``or`` rewritten as a WHERE OR clause (issue #252) is an
        # exact rewrite too, so skip the arithmetic caveat for it as well.
        # ``sum(A) op sum(B)`` with no ``by()`` on either side also needs no
        # caveat: both operands are scalar time-series (one value per instant),
        # so the same-bucket ES|QL math is numerically identical.
        if (
            not plan.set_or_fill
            and not plan.set_or_where
            and not _join_is_faithful(frag, resolver, output_group_fields)
            and not _fully_aggregated_scalar_op(frag)
        ):
            _append_unique(
                context.warnings,
                "Approximated PromQL arithmetic using same-bucket ES|QL math",
            )
    else:
        result_alias = "computed_value"
        parts = [f"ROW {result_alias} = {plan.expr}"]
        context.source_type = "ROW"
        context.output_group_fields = []

    if frag.extra.get("stripped_join"):
        _append_unique(context.warnings, "Dropped group_left label enrichment; kept primary metric series only")
    for warning in plan.warnings:
        _append_unique(context.warnings, warning)
    for spec in plan.specs:
        for warning in spec.warnings:
            _append_unique(context.warnings, warning)
    non_time_groups = [field for field in context.output_group_fields if field != "time_bucket"]
    # For ``sum(A) op sum(B)`` (both scalars, no by()), having no label groups is
    # intentional — the source PromQL also produces a single unlabelled time-series
    # per instant. Don't warn about missing labels that were never there.
    if plan.specs and not non_time_groups and not _fully_aggregated_scalar_op(frag):
        _append_unique(
            context.warnings,
            "PromQL series labels were not retained; output is bucket-level and may collapse multiple source series",
        )

    context.parser_backend = "fragment"
    context.metric_name = result_alias
    context.output_metric_field = result_alias
    context.esql_query = "\n".join(parts)
    context.translation_complete = True
    if plan.set_or_fill:
        return "translated cross-metric 'or' as COALESCE union"
    if plan.set_or_where:
        return "translated same-metric 'or' as unified WHERE OR clause"
    return "translated arithmetic expression"


@QUERY_TRANSLATORS.register("topk_family", priority=6)
def topk_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "topk":
        return None
    if not frag.metric:
        return None

    _apply_metric_map_index_override(context, frag)
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
    # A bare counter is auto-rated below (inner_func becomes "rate"), so it must
    # run under the TS command — RATE() is invalid under FROM. Select the source
    # from is_counter as well as an explicit range_func; time_filter and bucket
    # derive from source, so this also picks ts_time_filter / TBUCKET.
    source = "TS" if (is_counter or frag.range_func in AGG_FUNCTION_MAP) else "FROM"
    time_filter = rp.ts_time_filter if source == "TS" else rp.from_time_filter
    bucket = rp.ts_bucket if source == "TS" else rp.from_bucket
    inner_func = frag.range_func or ("rate" if is_counter else "")
    if inner_func in {"rate", "irate", "increase"}:
        prefer = "counter"
    else:
        prefer = "gauge"
    physical_metric = _resolve_frag_metric_field(frag, resolver, prefer=prefer)
    if inner_func:
        esql_inner = AGG_FUNCTION_MAP.get(inner_func, inner_func.upper())
        esql_inner, counter_warning, is_counter = resolve_counter_range_translation(
            inner_func, frag.metric, is_counter, resolver, esql_inner
        )
        if counter_warning:
            _append_unique(context.warnings, counter_warning)
        esql_inner, is_counter, map_rate_warnings = _plan_metric_map_rate_transform(
            frag, resolver, esql_inner, is_counter
        )
        for warning in map_rate_warnings:
            _append_unique(context.warnings, warning)
        # If drop_rate cleared esql_inner, downgrade to FROM so a non-TSDS gauge
        # is not queried under TS (which would inflate AVG across all samples).
        if not esql_inner and not is_counter:
            source = "FROM"
            time_filter = rp.from_time_filter
            bucket = rp.from_bucket
        # Prefer gauge field after drop_rate / mapped-gauge strip.
        if not is_counter and prefer == "counter":
            physical_metric = _resolve_frag_metric_field(frag, resolver, prefer="gauge")
        topk_cast_needed = _counter_unsafe_cast_needed(physical_metric, resolver)
        if (
            not is_counter
            and (esql_inner or "").upper() not in _COUNTER_INPUT_ESQL_FUNCS
            and topk_cast_needed
        ):
            _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))
        inner_arg = _counter_safe_metric_arg(esql_inner, physical_metric, is_counter, inner_func, counter_refuted=_counter_refuted(resolver, frag.metric), force_cast=topk_cast_needed)
        if esql_inner:
            inner_expr = _range_call(esql_inner, inner_arg, frag.range_window or rp.default_rate_window)
        else:
            # drop_rate / mapped gauge: outer agg operates on the bare field.
            inner_expr = inner_arg
        stats_expr = _agg_stats_expr(
            OUTER_AGG_MAP.get(frag.outer_agg or "avg", "AVG"),
            inner_expr,
            frag,
            resolver,
        )
    else:
        topk_agg_arg = physical_metric
        if _counter_unsafe_cast_needed(physical_metric, resolver):
            topk_agg_arg = f"TO_DOUBLE({physical_metric})"
            _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))
        stats_expr = _agg_stats_expr(
            OUTER_AGG_MAP.get(frag.outer_agg or "avg", "AVG"),
            topk_agg_arg,
            frag,
            resolver,
        )
    limit = int(frag.extra.get("topk_limit") or 10)
    sort_asc = frag.extra.get("topk_sort_asc", False)
    sort_dir = "ASC" if sort_asc else "DESC"
    topk_label = "bottomk" if sort_asc else "topk"

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
                f"| SORT value {sort_dir}",
                f"| LIMIT {limit}",
            ]
        )
        context.translation_complete = True
        n_label = "bottom N" if sort_asc else "top N"
        _append_unique(
            context.warnings,
            f"{topk_label}() without group labels: collapsed to single-series {n_label}; "
            "add preferred_group_labels hint for per-series breakdown",
        )
        return f"translated ungrouped {topk_label} as single-bucket {n_label}"

    n_label = "bottom N" if sort_asc else "top N"

    # For time-series XY panels (graph / timeseries), preserve the time dimension
    # so the chart renders a proper line/area per group label rather than a static
    # bar chart snapshot.  ES|QL has no subquery support, so the top-N filtering
    # cannot be applied over the full time range — all series are shown and the
    # user can apply legend filtering in Kibana.  For all other panel types (stat,
    # barchart, table …) keep the latest-bucket collapse which gives a ranked
    # snapshot that fits those display modes.
    _xy_panel_types = {"graph", "timeseries", "trend"}
    if context.panel_type in _xy_panel_types:
        context.output_group_fields = ["time_bucket"] + group_fields
        context.esql_query = "\n".join(
            [
                f"{source} {context.index}",
                f"| WHERE {time_filter}",
                *_build_where_lines(filters),
                f"| WHERE {physical_metric} IS NOT NULL",
                f"| STATS value = {stats_expr} BY {bucket}, {', '.join(_expand_late_bound_group_by_terms(group_fields, frag))}",
                "| SORT time_bucket ASC",
            ]
        )
        context.translation_complete = True
        _append_unique(
            context.warnings,
            f"Translated {topk_label}() as time-series breakdown by {', '.join(group_fields)}; "
            f"ES|QL has no subquery support so all series are shown (top-{limit} filtering approximated)",
        )
        return f"translated grouped {topk_label} as time-series breakdown for XY panel"

    context.output_group_fields = group_fields
    context.esql_query = "\n".join(
        [
            f"{source} {context.index}",
            f"| WHERE {time_filter}",
            *_build_where_lines(filters),
            f"| WHERE {physical_metric} IS NOT NULL",
            f"| STATS _bucket_value = {stats_expr} BY {bucket}, {', '.join(_expand_late_bound_group_by_terms(group_fields, frag))}",
            "| SORT time_bucket ASC",
            f"| STATS value = LAST(_bucket_value, time_bucket) BY {', '.join(group_fields)}",
            f"| KEEP {', '.join(group_fields + ['value'])}",
            f"| SORT value {sort_dir}",
            f"| LIMIT {limit}",
        ]
    )
    context.translation_complete = True
    _append_unique(context.warnings, f"Translated grouped {topk_label}() as latest-bucket ES|QL {n_label}")
    return f"translated grouped {topk_label} expression"


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
    """ES|QL clause for label_replace(): None if untranslatable, "" if a no-op.

    "" and None are deliberately different. None means the pattern could not be
    expressed and the operator needs to know the rename was dropped; "" means the
    rename is the identity and there is simply nothing to emit.
    """
    # Case 1: full copy — replacement captures everything unchanged
    if replacement in ("$1", "$0") and regex in ("(.*)", ".*", "(.+)", ".+"):
        # label_replace(x, "namespace", "$1", "namespace", "(.*)") copies a label
        # onto itself. Emitting `EVAL namespace = namespace` is a no-op that a
        # later KEEP discards, so it only adds noise to the query.
        same = str(dst).strip().strip("`") == str(src).strip().strip("`")
        return "" if same else f"| EVAL {dst} = {src}"
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


def _label_replace_clause_reads_source(eval_clause, dst: str, resolved_src: str) -> bool:
    """Does the emitted clause actually read the source column?

    Only the full-copy ``EVAL`` and the ``GROK`` extraction do. A constant
    replacement reads nothing, and an identity copy (``dst`` resolving to the
    same column as ``src``) emits nothing at all — neither needs ``src`` to
    survive the inner aggregation.
    """
    if not eval_clause:
        return False
    if eval_clause.lstrip().startswith("| GROK "):
        return True
    return eval_clause.strip() == f"| EVAL {dst} = {resolved_src}"


def _label_survives_aggregation(sub, resolved_src: str) -> bool:
    """Is ``resolved_src`` still readable at the end of ``sub``'s query?

    A ``STATS`` carries forward only its own aliases and grouping keys, so an
    ``EVAL dst = <src>`` appended afterwards fails at query time with an unknown
    column unless ``src`` is one of those keys. Un-aggregated queries still have
    every source field. ``label_replace`` asks for the column by appending it to
    ``preferred_group_labels``, but that is only a request: a rule is free to
    derive its grouping from the source expression alone (nested aggregations do,
    per issue #382), and even an honored inner grouping is dropped again by the
    outer ``STATS`` of a two-stage nested aggregation.
    """
    query = sub.esql_query or ""
    if not any(line.lstrip().startswith("| STATS") for line in query.splitlines()):
        return True
    available = {str(field).strip("`") for field in (sub.output_group_fields or [])}
    return resolved_src.strip("`") in available


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
    # Built before this check so an identity copy (which emits nothing) and a
    # constant replacement (which reads nothing) are not rejected for a column
    # they never reference.
    if _label_replace_clause_reads_source(eval_clause, dst, resolved_src):
        if not _label_survives_aggregation(sub, resolved_src):
            grouping = ", ".join(sub.output_group_fields or []) or "nothing"
            _append_unique(
                context.warnings,
                f"label_replace() derives {dst!r} from the source label {src!r}, but "
                f"the inner expression aggregates that label away (its result is "
                f"grouped by {grouping}), so the rewritten label cannot be computed "
                "from the aggregated result; requires manual redesign",
            )
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            context.translation_complete = True
            return "label_replace() source label does not survive aggregation"

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
    elif eval_clause is None:
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
    # Include the computed label as a group decoration field so downstream KEEP
    # clauses retain the column (mirrors label_join_family_rule).
    _lr_existing = list(sub.output_group_fields or [])
    context.output_group_fields = _lr_existing + ([dst] if dst and dst not in _lr_existing else [])
    context.source_type = sub.source_type
    context.parser_backend = "fragment"
    context.translation_complete = True
    return f"translated label_replace({dst!r})"


@QUERY_TRANSLATORS.register("label_join_family", priority=6)
def label_join_family_rule(context):
    """Translate label_join(v, dst, sep, src1, ...) via post-STATS ES|QL EVAL CONCAT."""
    frag = context.fragment
    if not frag or frag.family != "label_join":
        return None

    inner_frag = frag.extra.get("lj_inner_frag")
    if not inner_frag:
        return None

    dst = frag.extra.get("lj_dst", "")
    sep = frag.extra.get("lj_sep", "")
    src_labels = frag.extra.get("lj_src") or []

    sub = TranslationContext(
        promql_expr=inner_frag.raw_expr or context.promql_expr,
        data_view=context.data_view,
        index=context.index,
        rule_pack=context.rule_pack,
        resolver=context.resolver,
        metadata=dict(context.metadata),
    )
    sub.fragment = inner_frag
    sub.metadata["fragment_family"] = inner_frag.family
    QUERY_TRANSLATORS.apply(sub, stop_when=lambda ctx, _: ctx.translation_complete)
    QUERY_POSTPROCESSORS.apply(sub)

    if not sub.esql_query or sub.feasibility == "not_feasible":
        return None

    # Build CONCAT interleaving the separator literal between source labels.
    sep_literal = f'"{sep}"'
    concat_parts = []
    for i, src in enumerate(src_labels):
        if i > 0:
            concat_parts.append(sep_literal)
        concat_parts.append(_esql_identifier(src))
    eval_clause = f"| EVAL {_esql_identifier(dst)} = CONCAT({', '.join(concat_parts)})"

    lines = sub.esql_query.splitlines()
    # Insert EVAL before the first | KEEP or | SORT so that src labels are
    # still available (an inner KEEP from scaled_agg would drop them first).
    insert_idx = next(
        (i for i, ln in enumerate(lines)
         if ln.strip().startswith("| SORT") or ln.strip().startswith("| KEEP")),
        len(lines),
    )
    lines.insert(insert_idx, eval_clause)
    # If we inserted before a | KEEP, also extend that KEEP to include dst so
    # the new column is not immediately dropped.
    keep_idx = insert_idx + 1
    if keep_idx < len(lines) and lines[keep_idx].strip().startswith("| KEEP"):
        dst_id = _esql_identifier(dst)
        if dst_id not in lines[keep_idx]:
            lines[keep_idx] = lines[keep_idx].rstrip() + f", {dst_id}"

    _append_unique(context.warnings, f"label_join({dst!r}) approximated as ES|QL EVAL CONCAT")
    for w in sub.warnings:
        _append_unique(context.warnings, w)

    context.esql_query = "\n".join(lines)
    context.metric_name = sub.metric_name
    context.output_metric_field = sub.output_metric_field
    # Include the new derived label as an output group field so downstream KEEP
    # clauses and multi-target fusion retain the computed column.
    existing = list(sub.output_group_fields or [])
    context.output_group_fields = existing + ([dst] if dst and dst not in existing else [])
    context.source_type = sub.source_type
    context.parser_backend = "fragment"
    context.translation_complete = True
    return f"translated label_join({dst!r})"


@QUERY_TRANSLATORS.register("scaled_agg_family", priority=6)
def scaled_agg_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "scaled_agg":
        return None
    if not frag.metric or not frag.range_func:
        return None

    _apply_metric_map_index_override(context, frag)
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
    group_by_parts, output_group = _grouping_parts(bucket, group_fields, frag)

    esql_outer = OUTER_AGG_MAP.get(frag.outer_agg, "AVG")
    esql_inner = AGG_FUNCTION_MAP.get(frag.range_func, frag.range_func.upper())
    eval_line, final_alias = _frag_eval_line(alias, frag)
    is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rp)
    esql_inner, counter_warning, is_counter = resolve_counter_range_translation(
        frag.range_func, frag.metric, is_counter, resolver, esql_inner
    )
    if counter_warning:
        _append_unique(context.warnings, counter_warning)
    esql_inner, is_counter, map_rate_warnings = _plan_metric_map_rate_transform(
        frag, resolver, esql_inner, is_counter
    )
    for warning in map_rate_warnings:
        _append_unique(context.warnings, warning)
    # If drop_rate cleared esql_inner, downgrade to FROM so a non-TSDS gauge
    # is not queried under TS.  Recompute bucket and group_by_parts with the
    # FROM bucket expression so the STATS BY clause is valid.
    if not esql_inner and not is_counter:
        bucket = rp.from_bucket
        group_by_parts, output_group = _grouping_parts(bucket, group_fields, frag)
        _scaled_source = "FROM"
    else:
        _scaled_source = "TS"
    prefer = "counter" if (frag.range_func in {"rate", "irate", "increase"} and is_counter) else "gauge"
    physical_metric = _resolve_frag_metric_field(frag, resolver, prefer=prefer)
    cast_needed = _counter_unsafe_cast_needed(physical_metric, resolver)
    if (
        not is_counter
        and (esql_inner or "").upper() not in _COUNTER_INPUT_ESQL_FUNCS
        and cast_needed
    ):
        _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))
    inner_arg = _counter_safe_metric_arg(
        esql_inner,
        physical_metric,
        is_counter,
        frag.range_func,
        counter_refuted=_counter_refuted(resolver, frag.metric),
        force_cast=cast_needed,
    )
    if esql_inner:
        inner_windowed = _range_call(esql_inner, inner_arg, frag.range_window)
    else:
        # drop_rate → gauge: outer agg operates on the bare field.
        inner_windowed = inner_arg

    context.parser_backend = "fragment"
    context.source_type = _scaled_source
    context.metric_name = frag.metric
    context.output_metric_field = final_alias
    context.output_group_fields = output_group
    _scaled_time_filter = rp.ts_time_filter if _scaled_source == "TS" else rp.from_time_filter
    parts = [
        f"{_scaled_source} {context.index}",
        f"| WHERE {_scaled_time_filter}",
        *_build_where_lines(filters),
        f"| WHERE {physical_metric} IS NOT NULL",
    ]
    stats_line = f"| STATS {alias} = {_agg_stats_expr(esql_outer, inner_windowed, frag, resolver)}"
    if group_by_parts:
        stats_line += f" BY {', '.join(group_by_parts)}"
    parts.append(stats_line)
    if eval_line:
        parts.append(eval_line)
    collapsed = None
    if _summary_mode_from_metadata(context.metadata):
        collapsed = _collapse_summary_ts_query(
            parts, context.output_group_fields, [final_alias],
            keep_time_bucket=context.panel_type in {"table", "table-old"},
                            reduce_calc=context.metadata.get("reduce_calc", ""),
                        )
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

    # Nested grouping comes from the source expression's own ``by()`` clauses,
    # never from a panel display hint. The inner grouping decides what the outer
    # aggregation reduces over, so a hint here changes the returned number
    # instead of merely splitting series: ``max(sum(m))`` under ``legendFormat:
    # {{namespace}}`` reduced one sum *per namespace* and reported the largest
    # namespace instead of the collapsed total, silently and with no warning
    # (issue #382). Panel-derived ``preferred_group_labels`` (legendFormat
    # tokens, legacy table column patterns, dashboard-wide inference) carry no
    # source grouping, and PromQL's own ``sum(m)`` emits one label-less series,
    # so nothing is dropped and there is nothing to warn about. Explicit inner
    # ``by()`` labels are honored as before, and this matches
    # ``_build_measure_spec``'s nested_agg branch, which has always resolved the
    # inner grouping from ``inner_group`` alone.
    raw_inner_group = list(frag.extra.get("inner_group", []) or [])
    inner_group = resolver.resolve_labels(raw_inner_group) if resolver else list(raw_inner_group)
    result_alias = re.sub(r"[^a-zA-Z0-9_]", "_", f"{frag.metric}_{frag.outer_agg}")
    esql_outer = OUTER_AGG_MAP.get(frag.outer_agg, "COUNT")
    inner_agg_name = frag.extra.get("inner_agg", "count")
    esql_inner_agg = OUTER_AGG_MAP.get(inner_agg_name, "COUNT")
    inner_alias = "inner_val"
    physical_metric = _resolve_frag_metric_field(frag, resolver, prefer="gauge")
    count_presence_filter = f"| WHERE {physical_metric} IS NOT NULL" if inner_agg_name == "count" else ""
    metric_like_panels = {"stat", "singlestat", "gauge", "bargauge"}

    # Nested count(count by (...)) → COUNT_DISTINCT. Prefer the exclusive
    # inner label (in the inner by() but not the outer) so
    # ``count by(job, instance)(count by(job, instance, cpu)(...))`` counts
    # distinct ``cpu``, not ``job``.
    if frag.outer_agg == "count" and inner_agg_name == "count" and inner_group:
        outer_raw = {
            lbl for lbl in (frag.group_labels or []) if not str(lbl).startswith("label_")
        }
        exclusive_raw = [lbl for lbl in raw_inner_group if lbl not in outer_raw]
        if len(exclusive_raw) > 1:
            # The outer count() counts distinct *tuples* of every inner label
            # that is not already an outer grouping key — e.g.
            # ``count by(job)(count by(job, instance, cpu)(node_cpu))`` counts
            # distinct ``(instance, cpu)`` pairs per job. ES|QL COUNT_DISTINCT
            # takes a single field, so collapsing to one exclusive label
            # (``exclusive_raw[0]``) would under-count whenever another exclusive
            # label varies within a group (an instance with multiple CPUs).
            # There is no faithful single-field expression, so fail closed as
            # not_feasible rather than emit wrong math — this mirrors the
            # formula/measure path guard in ``promql.py``.
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            _append_unique(
                context.warnings,
                "nested count(count(...)) over multiple exclusive inner labels "
                f"({', '.join(exclusive_raw)}) counts distinct label tuples, which "
                "ES|QL COUNT_DISTINCT (single-field) cannot express without "
                "under-counting; requires manual redesign",
            )
            context.translation_complete = True
            return "nested count(count()) over multiple exclusive inner labels"
        if exclusive_raw:
            count_field = (
                resolver.resolve_label(exclusive_raw[0]) if resolver else exclusive_raw[0]
            )
        elif len(inner_group) == 1:
            count_field = inner_group[0]
        else:
            count_field = None
        if count_field:
            # Same invariant as the inner grouping above, for the same reason:
            # ``count(count by (cpu) (m))`` is a scalar — how many distinct CPUs
            # exist — so a ``legendFormat: {{cpu}}`` hint that reaches the outer
            # BY turns it into ``COUNT_DISTINCT(cpu) BY cpu``, which is 1 for
            # every CPU. ``_frag_group_labels`` adopts preferred labels whenever
            # the outer ``by()`` is empty, and for non-legend origins (legacy
            # table ``styles``) merges them in even when it is not, so pass none.
            outer_group_fields = _frag_group_labels(frag, resolver, None)
            lines = [
                f"FROM {context.index}",
                f"| WHERE {rp.from_time_filter}",
                *_build_where_lines(filters),
            ]
            if count_presence_filter:
                lines.append(count_presence_filter)
            if _summary_mode_from_metadata(context.metadata) or context.panel_type in metric_like_panels:
                if outer_group_fields:
                    context.output_group_fields = list(outer_group_fields)
                    lines.append(
                        f"| STATS {result_alias} = COUNT_DISTINCT({count_field}) "
                        f"BY {', '.join(outer_group_fields)}"
                    )
                else:
                    context.output_group_fields = []
                    lines.append(f"| STATS {result_alias} = COUNT_DISTINCT({count_field})")
            else:
                by_parts = [rp.from_bucket] + list(outer_group_fields)
                context.output_group_fields = ["time_bucket"] + list(outer_group_fields)
                lines.append(
                    f"| STATS {result_alias} = COUNT_DISTINCT({count_field}) BY {', '.join(by_parts)}"
                )
                lines.append("| SORT time_bucket ASC")
            context.esql_query = "\n".join(lines)
            _append_unique(
                context.warnings,
                f"Approximated nested count(count()) as COUNT_DISTINCT({count_field})",
            )
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
        physical_metric = _resolve_frag_metric_field(frag, resolver, prefer=prefer)
        nested_cast_needed = _counter_unsafe_cast_needed(physical_metric, resolver)
        if (
            not is_counter
            and (esql_inner_name or "").upper() not in _COUNTER_INPUT_ESQL_FUNCS
            and nested_cast_needed
        ):
            _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))
        _inner_arg = _counter_safe_metric_arg(
            esql_inner_name, physical_metric, is_counter, frag.range_func,
            counter_refuted=_counter_refuted(resolver, frag.metric),
            force_cast=nested_cast_needed,
        )
        first_stats_expr = (
            f"{inner_alias} = {esql_inner_agg}"
            f"({_range_call(esql_inner_name, _inner_arg, frag.range_window)})"
        )
        first_stats_by = (
            f"{rp.ts_bucket}, {', '.join(inner_group)}"
            if inner_group
            else rp.ts_bucket
        )
        outer_stats_expr = f"{result_alias} = {_agg_stats_expr(esql_outer, inner_alias, frag, resolver)}"
        parts = [
            f"TS {context.index}",
            f"| WHERE {rp.ts_time_filter}",
            *_build_where_lines(filters),
            f"| WHERE {physical_metric} IS NOT NULL",
            f"| STATS {first_stats_expr} BY {first_stats_by}",
            f"| STATS {outer_stats_expr} BY time_bucket",
        ]
        # Same scalar reduction as every other summary path: keep the outer
        # aggregation per bucket and let the panel's declared reducer pick the
        # value. Collapsing with the outer aggregation over the whole window
        # reported the window's extreme, and for a rate it also read the
        # incomplete boundary bucket that ``_collapse_summary_ts_query`` skips.
        output_group = ["time_bucket"]
        collapsed = None
        if _summary_mode_from_metadata(context.metadata) or context.panel_type in metric_like_panels:
            collapsed = _collapse_summary_ts_query(
                parts,
                output_group,
                [result_alias],
                keep_time_bucket=context.panel_type in {"table", "table-old"},
                reduce_calc=context.metadata.get("reduce_calc", ""),
            )
        if collapsed is None:
            parts.append("| SORT time_bucket ASC")
        else:
            output_group = collapsed
        context.esql_query = "\n".join(parts)
        context.output_group_fields = output_group
        context.parser_backend = "fragment"
        context.source_type = "TS"
        context.metric_name = result_alias
        context.output_metric_field = result_alias
        context.translation_complete = True
        return f"translated nested {frag.outer_agg} over {frag.range_func} expression"

    # Issue #380: the inner operand is a BARE metric selector here, so this
    # branch must make the same counter decision ``simple_agg_family_rule``
    # already makes for ``sum(<counter>)``. ES|QL rejects SUM/AVG/MIN/MAX/
    # PERCENTILE on counter_long/counter_double, so the inner aggregation has to
    # read the counter through LAST_OVER_TIME under TS. Emitting
    # ``FROM ... | STATS SUM(<counter field>)`` made
    # ``max(sum by (ns) (<counter>))`` fail with verification_exception while
    # its single-level sibling over the same metric family rendered fine.
    # ``count`` never reads the value, so it stays legal on a counter field.
    inner_reads_metric = inner_agg_name != "count"
    inner_is_counter = inner_reads_metric and (
        resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rp)
    )
    if inner_is_counter:
        physical_metric = _resolve_frag_metric_field(frag, resolver, prefer="counter")

    nested_agg_arg = physical_metric
    if inner_is_counter:
        nested_agg_arg = f"LAST_OVER_TIME({physical_metric})"
        _append_unique(
            context.warnings,
            "Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value",
        )
    elif inner_reads_metric and _counter_unsafe_cast_needed(physical_metric, resolver):
        nested_agg_arg = f"TO_DOUBLE({physical_metric})"
        _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))
    elif inner_reads_metric and inner_agg_name in _COUNTER_UNSAFE_OUTER_AGGS:
        # Issue #148 in the nested shape: without live capabilities the metric
        # kind cannot be proven, so the inner aggregation may still hit the
        # verification_exception above. Keep the query and name the risk rather
        # than letting a warning-free result imply the panel is safe.
        counter_warning = _counter_type_uncertainty_warning(frag.metric, resolver)
        if counter_warning:
            _append_unique(context.warnings, counter_warning)

    # Same source policy as ``simple_agg_family_rule``: ``FROM`` aggregates every
    # per-sample document in a bucket, so a TSDS gauge needs ``TS`` for the same
    # reason a counter does (see ``_gauge_can_use_ts``). Only a field that cannot
    # be shown to be a TSDS gauge stays on ``FROM``.
    gauge_uses_ts = (
        inner_reads_metric
        and not inner_is_counter
        and _gauge_can_use_ts(frag.metric, resolver, rp)
    )
    source = "TS" if (inner_is_counter or gauge_uses_ts) else "FROM"
    time_filter = rp.ts_time_filter if source == "TS" else rp.from_time_filter
    bucket = rp.ts_bucket if source == "TS" else rp.from_bucket
    # TS groups by every series in the index, so filter the metric's own
    # presence first — the same guard the range-function branch above and the
    # single-level path already apply.
    presence_filter = (
        f"| WHERE {physical_metric} IS NOT NULL"
        if (source == "TS" or inner_agg_name == "count")
        else ""
    )

    first_stats_expr = (
        f"{inner_alias} = {esql_inner_agg}({nested_agg_arg})"
        if inner_reads_metric
        else f"{inner_alias} = COUNT(*)"
    )
    # The inner aggregation is per instant in PromQL, so it is grouped by the
    # time bucket even on a scalar panel. Grouping by the inner labels alone
    # folded every sample in the dashboard window into one value, so a stat
    # panel read a window total instead of an instant.
    first_stats_by = f"{bucket}, {', '.join(inner_group)}" if inner_group else bucket
    outer_stats_expr = (
        f"{result_alias} = {_agg_stats_expr(esql_outer, inner_alias, frag, resolver)}"
    )
    parts = [
        f"{source} {context.index}",
        f"| WHERE {time_filter}",
        *_build_where_lines(filters),
        *([presence_filter] if presence_filter else []),
        f"| STATS {first_stats_expr} BY {first_stats_by}",
        f"| STATS {outer_stats_expr} BY time_bucket",
    ]
    # A scalar panel reduces the per-bucket series with the reducer the panel
    # itself declares (``lastNotNull`` by default). Collapsing with the outer
    # aggregation instead reported the window's extreme: this dashboard's
    # ``max(sum by (ns) (...))`` stat asks for the latest value, so MAX over
    # every bucket showed a historical peak whenever the metric had moved.
    output_group = ["time_bucket"]
    collapsed = None
    if _summary_mode_from_metadata(context.metadata) or context.panel_type in metric_like_panels:
        collapsed = _collapse_summary_ts_query(
            parts,
            output_group,
            [result_alias],
            keep_time_bucket=context.panel_type in {"table", "table-old"},
            reduce_calc=context.metadata.get("reduce_calc", ""),
        )
    if collapsed is None:
        parts.append("| SORT time_bucket ASC")
    else:
        output_group = collapsed
    context.esql_query = "\n".join(parts)
    context.output_group_fields = output_group

    context.parser_backend = "fragment"
    context.source_type = source
    context.metric_name = result_alias
    context.output_metric_field = result_alias
    context.translation_complete = True
    return f"translated nested {frag.outer_agg} expression"


@QUERY_TRANSLATORS.register("histogram_quantile_family", priority=6)
def histogram_quantile_family_rule(context):
    """Translate ``histogram_quantile(phi, <bucket series>)`` to ES|QL PERCENTILE().

    The ES|QL form depends on the target field type of the base histogram metric
    (issue #55). Only the two Elasticsearch histogram field types can produce an
    arbitrary percentile:

    - ``exponential_histogram`` -> ``PERCENTILE(field, phi*100)``
    - ``histogram``             -> ``PERCENTILE(TO_TDIGEST(field), phi*100)``

    Any other type (including ``aggregate_metric_double``, which stores only
    min/max/sum/value_count and not the distribution) or an unknown/unavailable
    schema degrades to not_feasible — we fail closed rather than emit a query
    that may 400 or return wrong-shaped values.
    """
    frag = context.fragment
    if not frag or frag.family != "histogram_quantile" or not frag.metric:
        return None
    phi = frag.extra.get("quantile_phi")
    if phi is None:
        return None

    # PromQL defines histogram_quantile for phi outside [0, 1] (it returns
    # +Inf/-Inf), but ES|QL PERCENTILE's second argument must be a 0-100
    # percentile, so phi*100 outside that range is an invalid query. Degrade
    # rather than emit e.g. PERCENTILE(field, 150).
    if not 0.0 <= phi <= 1.0:
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        _append_unique(
            context.warnings,
            f"histogram_quantile quantile {phi} is outside [0, 1]; ES|QL PERCENTILE() "
            "cannot represent an out-of-range quantile, so this requires manual redesign",
        )
        context.translation_complete = True
        return "histogram_quantile quantile out of range"

    # Only ``sum by (le)`` (or a bare bucket series with no outer aggregation)
    # maps faithfully to a PERCENTILE() over the histogram field: summing the
    # bucket counts across series is exactly what the histogram field encodes.
    # A non-sum aggregation (max/min/avg/...) is a different computation that
    # PERCENTILE cannot reproduce, so degrade rather than silently emit the same
    # query (the "degrade gracefully" contract).
    bucket_agg = frag.extra.get("bucket_agg") or ""
    if bucket_agg and bucket_agg != "sum":
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        _append_unique(
            context.warnings,
            f"histogram_quantile bucket series uses a non-sum aggregation ({bucket_agg}); "
            "only sum by (le) maps to an ES|QL PERCENTILE() over the histogram field, "
            "so this requires manual redesign",
        )
        context.translation_complete = True
        return "histogram_quantile non-sum bucket aggregation"

    # Classic Prometheus ``_bucket`` operands carry their distribution in the
    # ``le`` label. PERCENTILE() runs over the target's native histogram field,
    # which encodes the distribution per document, so it can only reproduce the
    # source when the bucket boundaries are used in the standard way:
    #   * the aggregation must keep ``le`` (e.g. ``sum by (le)``) — a bare series
    #     or a non-``le`` grouping collapses/destroys the buckets, and the
    #     implicit non-``le`` series can't be enumerated here;
    #   * there must be no ``le`` matcher — a filtered bucket set (``le!="+Inf"``)
    #     has no PERCENTILE() equivalent.
    # Anything else degrades rather than emitting a query with different meaning.
    bucket_metric = frag.extra.get("bucket_metric") or ""
    if bucket_metric.endswith("_bucket"):
        has_le_matcher = any(
            isinstance(m, dict) and m.get("label") == "le" for m in (frag.matchers or [])
        )
        if has_le_matcher:
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            _append_unique(
                context.warnings,
                "histogram_quantile bucket series has an le label matcher; an ES|QL "
                "PERCENTILE() over the histogram field cannot reproduce a filtered "
                "bucket set, so this requires manual redesign",
            )
            context.translation_complete = True
            return "histogram_quantile le matcher"
        if not frag.extra.get("had_le_grouping"):
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            _append_unique(
                context.warnings,
                "histogram_quantile over a classic _bucket series requires the le "
                "label in the aggregation (e.g. sum by (le)); without it the bucket "
                "boundaries are lost or per-series breakdown cannot be preserved, so "
                "this requires manual redesign",
            )
            context.translation_complete = True
            return "histogram_quantile missing le grouping"

    resolver = context.resolver
    rp = context.rule_pack
    _apply_metric_map_index_override(context, frag)
    filters, had_vars = _frag_filters(frag, resolver)
    if had_vars:
        _append_unique(context.warnings, "Dropped variable-driven label filters during migration")
    group_fields = _frag_group_labels(
        frag,
        resolver,
        context.metadata.get("preferred_group_labels"),
        preferred_origin=context.metadata.get("preferred_group_labels_origin"),
    )

    # A histogram metric is neither a counter nor a gauge scalar, so resolve
    # with no counter/gauge preference. (For the Fleet remote_write layout, a
    # histogram stored under a suffixed field other than the bare name may not
    # resolve here; that degrades to not_feasible below rather than mis-typing.)
    physical_metric = _resolve_frag_metric_field(frag, resolver, prefer=None)
    field_type = ((resolver.field_type(physical_metric) if resolver else None) or "").strip().lower()
    if field_type == "exponential_histogram":
        value_expr = physical_metric
    elif field_type == "histogram":
        value_expr = f"TO_TDIGEST({physical_metric})"
    elif not field_type:
        # Caps unavailable (offline / empty discovery): assume exponential_histogram
        # so common Prometheus histogram_quantile panels still migrate. A wrong
        # assumption fails at render; positively typed non-histogram fields still
        # degrade below.
        value_expr = physical_metric
        _append_unique(
            context.warnings,
            "histogram_quantile target field type could not be determined; "
            "assumed exponential_histogram and emitted PERCENTILE(). "
            "If the field is a classic histogram, pin the mapping or re-run with "
            "field capabilities so TO_TDIGEST() is used",
        )
    else:
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        reason = (
            f"histogram_quantile target field '{physical_metric}' is typed "
            f"'{field_type}', not a histogram or exponential_histogram field, so it "
            "cannot be translated to an ES|QL PERCENTILE() (requires manual redesign)"
        )
        _append_unique(context.warnings, reason)
        context.translation_complete = True
        return "histogram_quantile field type unsupported"

    percentile_value = _format_scalar_value(round(phi * 100, 10))
    stats_expr = f"PERCENTILE({value_expr}, {percentile_value})"
    alias = re.sub(r"[^a-zA-Z0-9_]", "_", frag.metric)
    group_by_parts, output_group = _grouping_parts(rp.ts_bucket, group_fields, frag)

    parts = [
        f"TS {context.index}",
        f"| WHERE {rp.ts_time_filter}",
        *_build_where_lines(filters),
        f"| WHERE {physical_metric} IS NOT NULL",
    ]
    stats_line = f"| STATS {alias} = {stats_expr}"
    if group_by_parts:
        stats_line += f" BY {', '.join(group_by_parts)}"
    parts.append(stats_line)
    collapsed = None
    if _summary_mode_from_metadata(context.metadata):
        collapsed = _collapse_summary_ts_query(
            parts, output_group, [alias],
            keep_time_bucket=context.panel_type in {"table", "table-old"},
                            reduce_calc=context.metadata.get("reduce_calc", ""),
                        )
    if collapsed is None:
        if "time_bucket" in output_group:
            parts.append("| SORT time_bucket ASC")
    else:
        output_group = collapsed

    context.esql_query = "\n".join(parts)
    context.parser_backend = "fragment"
    context.source_type = "TS"
    context.metric_name = frag.metric
    context.output_metric_field = alias
    context.output_group_fields = output_group
    _append_unique(
        context.warnings,
        "histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is "
        "approximate — PERCENTILE uses t-digest, which treats histogram buckets as point "
        "masses rather than interpolating within them as Prometheus does, so results can "
        "diverge noticeably when traffic concentrates in a few wide buckets (the common "
        "latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for "
        "exact results.",
    )
    context.translation_complete = True
    return "translated histogram_quantile to PERCENTILE"


def _drop_redundant_legend_grouping(context, frag, group_fields):
    """Issue #99: drop legendFormat-origin BY labels that ES|QL TSID already splits.

    When a PromQL expression has no explicit outer aggregation and runs on the TS
    path, ``BY TBUCKET`` alone yields one row per TSID per bucket — series are
    split natively. Adding a ``legendFormat``-derived label to BY is redundant and
    forces a distorting outer ``AVG`` (issue #99). It is only safe to drop on the
    TS path and only when the label did not come from an explicit PromQL ``by()``.

    The TSID-driven split is a *time series chart* affordance: Kibana renders one
    line per TSID row. Summary/categorical panels (bargauge, which still carries
    legend-origin labels — see ``_target_translation_hints``) instead render a
    breakdown from the explicit ``output_group_fields`` column, so dropping the
    label there would collapse the per-series bars rather than relocate them.

    The decision is shared with the formula/binary path via
    :func:`_drop_legend_labels_if_redundant`; the direct family rules always allow
    the bare direct-TS-gauge form, so ``allow_direct_ts_gauge`` defaults to True.
    """
    return _drop_legend_labels_if_redundant(
        frag,
        context.resolver,
        context.rule_pack,
        group_fields,
        context.metadata.get("preferred_group_labels_origin"),
        _summary_mode_from_metadata(context.metadata),
    )


@QUERY_TRANSLATORS.register("range_agg_family", priority=8)
def range_agg_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "range_agg":
        return None
    if not frag.metric or not frag.range_func:
        return None

    _apply_metric_map_index_override(context, frag)
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
    group_fields = _drop_redundant_legend_grouping(context, frag, group_fields)
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
    esql_inner_name, is_counter, map_rate_warnings = _plan_metric_map_rate_transform(
        frag, resolver, esql_inner_name, is_counter
    )
    for warning in map_rate_warnings:
        _append_unique(context.warnings, warning)
    for note in _metric_map_unapplied_notes(
        resolver, frag.metric, source_labels=_frag_source_labels(frag)
    ):
        _append_unique(context.warnings, note)
    # When _plan_metric_map_rate_transform clears esql_inner_name (drop_rate),
    # no counter/rate function is emitted; use FROM so non-TSDS gauges are
    # queried correctly.  frag.range_func in AGG_FUNCTION_MAP is always True
    # here (the guard above returned None when the key was absent), so using
    # it would force TS even in the drop_rate case.
    source = "TS" if (bool(esql_inner_name) or is_counter) else "FROM"
    time_filter = rp.ts_time_filter if source == "TS" else rp.from_time_filter
    bucket = rp.ts_bucket if source == "TS" else rp.from_bucket
    prefer = "counter" if (frag.range_func in {"rate", "irate", "increase"} and is_counter) else "gauge"
    physical_metric = _resolve_frag_metric_field(frag, resolver, prefer=prefer)

    cast_needed = _counter_unsafe_cast_needed(physical_metric, resolver)
    if (
        not is_counter
        and (esql_inner_name or "").upper() not in _COUNTER_INPUT_ESQL_FUNCS
        and cast_needed
    ):
        _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))
    inner_arg = _counter_safe_metric_arg(
        esql_inner_name,
        physical_metric,
        is_counter,
        frag.range_func,
        counter_refuted=_counter_refuted(resolver, frag.metric),
        force_cast=cast_needed,
    )
    if esql_inner_name:
        inner_expr = _range_call(esql_inner_name, inner_arg, frag.range_window)
    else:
        # drop_rate → gauge: outer agg operates on the bare field.
        inner_expr = inner_arg
    outer = OUTER_AGG_MAP.get(frag.outer_agg, "") if frag.outer_agg else ""
    if not outer and source == "TS" and group_fields:
        stats_expr = _apply_unit_scale(
            f"AVG({inner_expr})",
            _metric_map_unit_scale(resolver, frag.metric, source_labels=_frag_source_labels(frag)),
        )
    else:
        stats_expr = (
            _agg_stats_expr(outer, inner_expr, frag, resolver)
            if outer
            else _apply_unit_scale(
                inner_expr,
                _metric_map_unit_scale(
                    resolver, frag.metric, source_labels=_frag_source_labels(frag)
                ),
            )
        )

    alias = re.sub(r"[^a-zA-Z0-9_]", "_", frag.metric)
    group_by_parts, output_group = _grouping_parts(bucket, group_fields, frag)
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
        collapsed = _collapse_summary_ts_query(
            parts, output_group, [final_alias],
            keep_time_bucket=context.panel_type in {"table", "table-old"},
                            reduce_calc=context.metadata.get("reduce_calc", ""),
                        )
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


def _apply_metric_map_index_override(context, frag) -> None:
    """Override ``context.index`` when metric_map sets ``target_index``."""
    if frag is None or not getattr(frag, "metric", None):
        return
    mapped = _metric_map_target_index(
        context.resolver,
        frag.metric,
        source_labels=_frag_source_labels(frag),
    )
    if mapped:
        context.index = mapped


def _agg_stats_expr(outer, inner_expr, frag, resolver=None):
    """Render an aggregation call, special-casing quantile -> PERCENTILE(expr, phi*100).

    PromQL quantile(phi, m) is the phi-quantile across the grouped series, which is
    exactly ES|QL PERCENTILE(m, phi*100). All other aggregations are AGG(expr).
    """
    labels = _frag_source_labels(frag) if frag is not None else None
    metric_name = frag.metric if frag is not None else None
    if frag is not None and frag.outer_agg == "quantile":
        phi = frag.extra.get("quantile_phi")
        if phi is not None:
            expr = f"PERCENTILE({inner_expr}, {_format_scalar_value(phi * 100)})"
            return _apply_unit_scale(
                expr, _metric_map_unit_scale(resolver, metric_name, source_labels=labels)
            )
    expr = f"{outer}({inner_expr})"
    return _apply_unit_scale(
        expr, _metric_map_unit_scale(resolver, metric_name, source_labels=labels)
    )


@QUERY_TRANSLATORS.register("simple_agg_family", priority=9)
def simple_agg_family_rule(context):
    frag = context.fragment
    if not frag or frag.family != "simple_agg":
        return None
    if not frag.metric:
        return None

    _apply_metric_map_index_override(context, frag)
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
    # Same issue-#99 drop as range_agg / simple_metric: outer agg without by()
    # means legendFormat {{…}} is a series alias, not a BY dimension.
    group_fields = _drop_redundant_legend_grouping(context, frag, group_fields)
    is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rp)
    pre_agg_filter = frag.extra.get("post_filter") if frag.extra.get("inner_frag") else None
    physical_metric = _resolve_frag_metric_field(
        frag, resolver, prefer="counter" if is_counter else "gauge"
    )
    gauge_physical_metric = (
        physical_metric
        if not is_counter
        else _resolve_frag_metric_field(frag, resolver, prefer="gauge")
    )

    if pre_agg_filter:
        # Issue #245: a field with conflicting exact types across indices is
        # unsafe to reference bare in EITHER the comparison filter or the
        # outer aggregation ("ambiguities in index mappings"). Cast once and
        # reuse everywhere `gauge_physical_metric` appears in this block.
        pre_agg_metric_arg = gauge_physical_metric
        if _counter_unsafe_cast_needed(gauge_physical_metric, resolver):
            pre_agg_metric_arg = f"TO_DOUBLE({gauge_physical_metric})"
            _append_unique(context.warnings, _counter_unsafe_cast_warning(gauge_physical_metric, resolver))
        # Issue #148: a pre-aggregation comparison filter combined with a
        # counter aggregation referenced without rate() has no counter-safe
        # ES|QL form — the comparison must run on the raw value while the outer
        # SUM/MAX/MIN cannot be applied to a counter type. When the counter is
        # proven (live caps or a rule-pack pin), mark not_feasible instead of
        # emitting a query that errors with verification_exception. COUNT is
        # exempt (it counts documents, legal on counters).
        if is_counter and frag.outer_agg in _COUNTER_UNSAFE_OUTER_AGGS:
            context.feasibility = "not_feasible"
            context.confidence = 0.0
            context.translation_complete = True
            _append_unique(
                context.warnings,
                f"{frag.outer_agg}() over a comparison filter on counter metric "
                f"'{frag.metric}' has no counter-safe ES|QL translation "
                f"(ES|QL forbids SUM/MAX/MIN/AVG on counter fields and the "
                f"comparison must run on the raw value); marked not_feasible",
            )
            frag.extra.pop("post_filter", None)
            return "counter pre-aggregation comparison not feasible"
        alias = re.sub(r"[^a-zA-Z0-9_]", "_", f"{frag.metric}_{frag.outer_agg}")
        metric_like = _summary_mode_from_metadata(context.metadata) or context.panel_type in {"stat", "singlestat", "gauge", "bargauge"}
        # The field is not a proven counter here; if caps were unavailable it may
        # still be counter_long in ES, so flag the same risk as the bare-agg path.
        if frag.outer_agg in _COUNTER_UNSAFE_OUTER_AGGS:
            counter_warning = _counter_type_uncertainty_warning(frag.metric, resolver)
            if counter_warning:
                _append_unique(context.warnings, counter_warning)
        filter_value = _format_scalar_value(pre_agg_filter["value"])

        # Issue #166: ``count(<metric> <cmp> <value>)`` ("how many targets match")
        # must count distinct SERIES (targets), not raw samples. Counting documents
        # (``COUNT(*)``) inflates the answer with every stored sample, so the number
        # grows the longer data is collected. The TS command is no help here either
        # — it cannot filter on a metric value in a bare ``WHERE`` (``WHERE up == 0``
        # fails with "Unknown column [up]"). So collapse to one row per series first,
        # then COUNT the series, exactly like the counter ``count()`` path below. The
        # series identity is the source grouping labels plus the instance dimension;
        # if neither can be determined we cannot honestly name the targets, so flag
        # for manual review instead of emitting a wrong number.
        if frag.outer_agg == "count":
            instance_field = _default_instance_field(rp, resolver)
            series_dims = [d for d in [*group_fields, instance_field] if d]
            series_dims = list(dict.fromkeys(series_dims))
            if not series_dims:
                context.feasibility = "not_feasible"
                context.confidence = 0.0
                context.translation_complete = True
                _append_unique(
                    context.warnings,
                    "count() over a comparison cannot identify the target series "
                    "(no grouping labels or instance dimension available); "
                    "flagged for manual review",
                )
                frag.extra.pop("post_filter", None)
                return "count over a comparison: target series unidentifiable"
            # Issue #166 follow-up (review): PromQL ``count()`` counts vector
            # elements — series keyed by their FULL label set (e.g. ``up`` is
            # keyed by ``{job, instance}``). When the query supplies no grouping
            # labels the collapse key falls back to the instance dimension ALONE,
            # which is not a complete Prometheus series identity: two matching
            # series that share an ``instance`` but differ on another label (e.g.
            # ``job``) collapse into one row and the count is under-stated. The
            # target schema cannot tell us the source label set, so we keep the
            # best-effort collapse but surface the ambiguity instead of letting a
            # clean (warning-free) migration imply the result is exact.
            if not group_fields:
                _append_unique(
                    context.warnings,
                    "count() over a comparison collapses matching samples to "
                    "series by the instance dimension only (the query supplied no "
                    "grouping labels); if matching series share an instance but "
                    "differ on another label such as job, the count may be "
                    "under-stated — add the distinguishing label to by(...) or "
                    "verify the series identity",
                )
            where_lines = [
                f"FROM {context.index}",
                f"| WHERE {rp.from_time_filter}",
                *_build_where_lines(filters),
                f"| WHERE {pre_agg_metric_arg} {pre_agg_filter['op']} {filter_value}",
            ]
            inner_by = ", ".join(series_dims)
            if metric_like:
                context.output_group_fields = list(group_fields)
                outer_line = f"| STATS {alias} = COUNT(*)"
                if group_fields:
                    outer_line += f" BY {', '.join(group_fields)}"
                lines = [
                    *where_lines,
                    f"| STATS series_present = COUNT(*) BY {inner_by}",
                    outer_line,
                ]
            else:
                context.output_group_fields = ["time_bucket", *group_fields]
                outer_line = f"| STATS {alias} = COUNT(*) BY time_bucket"
                if group_fields:
                    outer_line += f", {', '.join(group_fields)}"
                lines = [
                    *where_lines,
                    f"| STATS series_present = COUNT(*) BY {rp.from_bucket}, {inner_by}",
                    outer_line,
                    "| SORT time_bucket ASC",
                ]
            context.esql_query = "\n".join(lines)
            context.parser_backend = "fragment"
            context.source_type = "FROM"
            context.metric_name = alias
            context.output_metric_field = alias
            frag.extra.pop("post_filter", None)
            context.translation_complete = True
            return "translated count() over a comparison by distinct series"

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
            f"| WHERE {pre_agg_metric_arg} {pre_agg_filter['op']} {filter_value}",
        ]
        # ``count()`` returned above (issue #166); only SUM/AVG/MAX/MIN reach here.
        if metric_like and not group_fields:
            context.output_group_fields = []
            lines.append(f"| STATS {alias} = {_agg_stats_expr(OUTER_AGG_MAP.get(frag.outer_agg, rp.default_gauge_agg.upper()), pre_agg_metric_arg, frag, resolver)}")
        else:
            # This is a single-level aggregation, so a late-bound ``by ($var)``
            # dimension aliases cleanly to a stable ES|QL field control
            # (``grouping = ??grouping``) here too — mirror the main agg path so
            # the guardrail's deferral is honored instead of degrading to
            # not_feasible (issue #282). ``output_group_fields`` keeps the bare
            # alias so the Lens breakdown binds the stable column.
            group_by_parts = _expand_late_bound_group_by_terms(group_fields, frag)
            context.output_group_fields = list(group_fields)
            if not metric_like:
                group_by_parts = [pre_bucket, *group_by_parts]
                context.output_group_fields = ["time_bucket", *context.output_group_fields]
            stats_expr = _agg_stats_expr(OUTER_AGG_MAP.get(frag.outer_agg, rp.default_gauge_agg.upper()), pre_agg_metric_arg, frag, resolver)
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
        frag.extra.pop("post_filter", None)
        context.translation_complete = True
        return "translated aggregation with pre-aggregation comparison filter"

    if frag.outer_agg == "count" and is_counter:
        alias = re.sub(r"[^a-zA-Z0-9_]", "_", f"{frag.metric}_count")
        metric_like = context.panel_type in {"stat", "singlestat", "gauge", "bargauge"}
        if metric_like:
            context.output_group_fields = []
            by_clause = ", ".join(group_fields) if group_fields else _default_instance_field(rp, resolver)
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
            by_clause = f"{rp.from_bucket}, " + (", ".join(group_fields) if group_fields else _default_instance_field(rp, resolver))
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
    elif (
        frag.outer_agg in _COUNTER_UNSAFE_OUTER_AGGS
        and _counter_unsafe_cast_needed(physical_metric, resolver)
    ):
        # Issue #245: the target maps this field with conflicting types
        # across indices, so SUM/MAX/MIN/AVG/STDDEV/QUANTILE may reject the
        # bare field at runtime ("ambiguities in index mappings"). TO_DOUBLE
        # is valid under either FROM or TS (unlike LAST_OVER_TIME, a TS-only
        # function), so it defends the aggregation without disturbing the
        # source/bucket already chosen above, instead of gambling on a bare
        # aggregation.
        inner_expr = f"TO_DOUBLE({physical_metric})"
        _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))
    else:
        inner_expr = physical_metric
        # Issue #148: a bare SUM/MAX/MIN/AVG against a field that is actually
        # counter_long in ES fails with verification_exception. When the target
        # cannot prove the field is a gauge, keep the query but warn.
        if frag.outer_agg in _COUNTER_UNSAFE_OUTER_AGGS:
            counter_warning = _counter_type_uncertainty_warning(frag.metric, resolver)
            if counter_warning:
                _append_unique(context.warnings, counter_warning)

    outer = OUTER_AGG_MAP.get(frag.outer_agg, rp.default_gauge_agg.upper())
    stats_expr = _agg_stats_expr(outer, inner_expr, frag, resolver)
    source, time_filter, bucket, physical_metric, stats_expr = (
        _apply_metric_map_to_rate_on_simple(
            frag,
            resolver,
            rp,
            source=source,
            time_filter=time_filter,
            bucket_expr=bucket,
            metric_field=physical_metric,
            stats_expr=stats_expr,
            warnings=context.warnings,
        )
    )
    for note in _metric_map_unapplied_notes(
        resolver, frag.metric, source_labels=_frag_source_labels(frag)
    ):
        _append_unique(context.warnings, note)
    alias = re.sub(r"[^a-zA-Z0-9_]", "_", frag.metric)
    group_by_parts, output_group = _grouping_parts(bucket, group_fields, frag)
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
        collapsed = _collapse_summary_ts_query(
            parts, output_group, [final_alias],
            keep_time_bucket=context.panel_type in {"table", "table-old"},
                            reduce_calc=context.metadata.get("reduce_calc", ""),
                        )
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

    _apply_metric_map_index_override(context, frag)
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
    group_fields = _drop_redundant_legend_grouping(context, frag, group_fields)
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
        and _gauge_can_use_ts(frag.metric, resolver, rp)
    )

    if is_counter:
        source = "TS"
        time_filter = rp.ts_time_filter
        bucket = rp.ts_bucket
        physical_metric = _resolve_frag_metric_field(frag, resolver, prefer="counter")
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
        physical_metric = _resolve_frag_metric_field(frag, resolver, prefer="gauge")
        stats_expr = f"MAX(LAST_OVER_TIME({physical_metric}))"
        if context.metadata.get("summary_mode"):
            warning = gauge_default_agg_warning(group_fields, frag.metric, "MAX")
            if warning:
                _append_unique(context.warnings, warning)
    elif can_use_ts_aggregated_gauge:
        source = "TS"
        time_filter = rp.ts_time_filter
        bucket = rp.ts_bucket
        default_agg = rp.default_gauge_agg.upper()
        physical_metric = _resolve_frag_metric_field(frag, resolver, prefer="gauge")
        agg_arg = physical_metric
        if _counter_unsafe_cast_needed(physical_metric, resolver):
            agg_arg = f"TO_DOUBLE({physical_metric})"
            _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))
        # No explicit PromQL aggregator was given. Collapse ACROSS TIME with
        # LAST_OVER_TIME first, then aggregate ACROSS SERIES with the gauge
        # default. Aggregating the raw field does BOTH at once, which is not a
        # downsample of an instant vector: a bare selector's value at each step is
        # the most recent sample at or before it, never the step mean.
        #
        # "Node Exporter Scrape Time" had 46 of its 48 per-collector series
        # disagree with Prometheus for exactly this reason (collector=nfs by
        # 5276%: 0.0123 against 0.000228) -- a scrape-duration gauge spikes, so a
        # 14-minute bucket mean sits far above the latest sample.
        stats_expr = f"{default_agg}(LAST_OVER_TIME({agg_arg}))"
        warning = gauge_default_agg_warning(group_fields, frag.metric, default_agg)
        if warning:
            _append_unique(context.warnings, warning)
    else:
        source = "FROM"
        time_filter = rp.from_time_filter
        bucket = rp.from_bucket
        default_agg = rp.default_gauge_agg.upper()
        physical_metric = _resolve_frag_metric_field(frag, resolver, prefer="gauge")
        agg_arg = physical_metric
        if _counter_unsafe_cast_needed(physical_metric, resolver):
            agg_arg = f"TO_DOUBLE({physical_metric})"
            _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, resolver))
        stats_expr = f"{default_agg}({agg_arg})"
        if frag.extra.get("wrapped_scalar"):
            _append_unique(context.warnings, "Approximated scalar() as a direct metric value")
        else:
            warning = gauge_default_agg_warning(group_fields, frag.metric, default_agg)
            if warning:
                _append_unique(context.warnings, warning)

    stats_expr = _apply_unit_scale(
        stats_expr,
        _metric_map_unit_scale(resolver, frag.metric, source_labels=_frag_source_labels(frag)),
    )

    alias = re.sub(r"[^a-zA-Z0-9_]", "_", frag.metric)
    eval_line, final_alias = _frag_eval_line(alias, frag)
    group_by_parts, output_group = _grouping_parts(bucket, group_fields, frag)

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
        collapsed = _collapse_summary_ts_query(
            parts, output_group, [final_alias],
            keep_time_bucket=context.panel_type in {"table", "table-old"},
                            reduce_calc=context.metadata.get("reduce_calc", ""),
                        )
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


def _resolve_labels_preserving_controls(resolver, labels):
    """Resolve concrete labels while passing ES|QL control tokens through.

    A late-bound grouping control (``??var``) is not a physical field, so it
    must never be handed to ``resolver.resolve_labels`` (which would drop or
    mangle it); it is preserved verbatim (issue #282).
    """
    controls = [label for label in labels if _is_esql_control_token(label)]
    resolvable = [label for label in labels if not _is_esql_control_token(label)]
    return resolver.resolve_labels(resolvable) + controls


@QUERY_TRANSLATORS.register("resolve_labels", priority=45)
def resolve_labels_rule(context):
    if context.translation_complete:
        return None
    if not context.resolver:
        return None
    original = list(context.group_labels)
    context.group_labels = _resolve_labels_preserving_controls(context.resolver, context.group_labels)
    if context.output_group_fields:
        context.output_group_fields = _resolve_labels_preserving_controls(
            context.resolver, context.output_group_fields
        )
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
            return f"built stats expression {context.stats_expr}"
        context.stats_expr = inner_expr
        return f"built stats expression {context.stats_expr}"

    default_agg = context.rule_pack.default_gauge_agg.upper()
    default_agg_arg = physical_metric
    if _counter_unsafe_cast_needed(physical_metric, context.resolver):
        default_agg_arg = f"TO_DOUBLE({physical_metric})"
        _append_unique(context.warnings, _counter_unsafe_cast_warning(physical_metric, context.resolver))
    # Collapse ACROSS TIME with LAST_OVER_TIME before aggregating ACROSS SERIES.
    # A bare instant-vector selector has a value at each step -- the most recent
    # sample at or before it -- so averaging a gauge over the step interval is a
    # different number, not a smoothed one. "Node Exporter Scrape Time" had 46 of
    # its 48 per-collector series disagree with Prometheus for this reason
    # (collector=nfs by 5276%: 0.0123 against 0.000228), because a scrape-duration
    # gauge spikes and a 14-minute bucket mean sits far above the latest sample.
    # LAST_OVER_TIME is a TS-command function, so the FROM path keeps the plain
    # aggregate (it has no equivalent; see open-problems 0f).
    if context.source_type == "TS" and not context.inner_func:
        context.stats_expr = f"{default_agg}(LAST_OVER_TIME({default_agg_arg}))"
    else:
        context.stats_expr = f"{default_agg}({default_agg_arg})"
    if context.inner_func:
        _append_unique(
            context.warnings,
            f"Unmapped function {context.inner_func}; approximating with {default_agg}",
        )
    else:
        _append_unique(
            context.warnings,
            f"No explicit aggregation; using {default_agg} over each series' latest "
            f"sample (instant-vector semantics)",
        )
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


_COUNTER_RANGE_WINDOW_RE = re.compile(
    r"\b(RATE|IRATE|INCREASE)\((?P<arg>[^(),]+),\s*[0-9]+(?:ms|s|m|h|d)\)", re.IGNORECASE
)

# Any counter-family range call left in an emitted ES|QL query. By the time the
# postprocessor runs these are always windowless (see ``_range_call``).
_COUNTER_RANGE_CALL_RE = re.compile(r"\b(?:RATE|IRATE|INCREASE)\s*\(")

_WINDOW_UNIT_SECONDS = {
    "ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000,
}
# Prometheus durations may be compound (``1h30m``, ``1d12h``), so a window is one
# or more count+unit components. Matching only a single component would let
# ``rate(foo[1h30m])`` drop its window silently while the identical ``[90m]``
# reported the loss. A bare number is also accepted here (and by this
# translator) and means seconds, so ``[7200]`` must not slip through either.
_DURATION_TOKEN = r"(?:(?:[0-9]+(?:ms|s|m|h|d|w|y))+|[0-9]+(?:\.[0-9]+)?)"
# ``$__range``/``$__range_s``/``$__range_ms`` are the *whole* dashboard window,
# unlike the step macros below, and are substituted upstream to a literal ``1h``
# (see ``promql.preprocess_grafana_macros``). A window of "the entire view" is
# many buckets wide at every range, so it is always a loss and must not be lumped
# in with the step macros. The unit suffix may sit outside the braces
# (``[${__range_s}s]``, ``[${__range_ms}ms]``), which is a form this codebase
# explicitly supports.
_RANGE_MACRO = (
    r"\$(?:\{__range(?:_ms|_s)?\}|__range(?:_ms|_s)?)(?:ms|s|m|h|d|w|y)?"
)
# ``[window]`` attached to a counter-style PromQL range function in the *source*
# expression, e.g. ``rate(foo[1w])`` -> ``1w``. A subquery selector (``[1h:5m]``)
# never matches because ``:`` is excluded, and those are rejected upstream as
# unsupported anyway.
_SOURCE_COUNTER_RANGE_WINDOW_RE = re.compile(
    r"\b(?:rate|irate|increase)\s*\([^()\[\]]*"
    rf"\[\s*(?P<window>{_DURATION_TOKEN}|{_RANGE_MACRO})\s*\]",
    re.IGNORECASE,
)
_RANGE_MACRO_RE = re.compile(rf"^{_RANGE_MACRO}$", re.IGNORECASE)
# Dashboard panels get an *adaptive* bucket, ``view_range / 20``
# (``_ADAPTIVE_RATE_BUCKETS`` in panels.py), whose width is unknown at
# translation time. A bucket stands in for an authored ``[window]`` when the
# dashboard is viewed over roughly 20x it, which up to an hour holds for ordinary
# views — a ``[1h]`` lookback needs a ~20h range — and the windowless form is
# then the closer match to Prometheus anyway (see ``_range_call``). Past an hour
# the author is asking for a long-horizon average no view reproduces: ``[1d]``
# would need a 20-day range, ``[1w]`` a 140-day one (issue #379).
_ADAPTIVE_BUCKET_REPRODUCIBLE_WINDOW_SECONDS = 3600
# An explicit Grafana panel ``interval`` (and a rule pack overriding
# ``ts_bucket``) instead pins a *fixed* bucket, so the guess above does not
# apply — the exact width is known and any longer lookback is measurably lost.
_FIXED_BUCKET_RE = re.compile(
    r"TBUCKET\(\s*(?P<count>[0-9]+)\s*(?P<unit>millisecond|second|minute|hour|day|week|year)s?\s*\)",
    re.IGNORECASE,
)
_ESQL_BUCKET_UNIT_SECONDS = {
    "millisecond": 0.001, "second": 1, "minute": 60,
    "hour": 3600, "day": 86400, "week": 604800, "year": 31536000,
}


def _bucket_reproducible_window_seconds(ts_bucket):
    """Longest authored lookback the configured time bucket can stand in for,
    plus a human label for a fixed bucket (``None`` when adaptive).

    A fixed ``TBUCKET(<n> <unit>)`` knows its own width, so that width is the
    limit. An adaptive ``TBUCKET(<n>, ?_tstart, ?_tend)`` does not (it depends on
    the view the operator picks), so fall back to the ordinary-view heuristic.
    """
    match = _FIXED_BUCKET_RE.search(str(ts_bucket or ""))
    if not match:
        return _ADAPTIVE_BUCKET_REPRODUCIBLE_WINDOW_SECONDS, None
    count, unit = match.group("count"), match.group("unit").lower()
    seconds = int(count) * _ESQL_BUCKET_UNIT_SECONDS[unit]
    return seconds, f"{count}-{unit}"


def _window_seconds(window):
    """Seconds for a Prometheus duration, or ``None`` if unparseable.

    Handles compound durations (``1h30m`` -> 5400) by summing components, and a
    bare number as seconds (``7200`` -> 7200), both of which reach this code.
    """
    text = str(window or "").strip()
    if not re.fullmatch(_DURATION_TOKEN, text, re.IGNORECASE):
        return None
    components = re.findall(r"([0-9]+)(ms|s|m|h|d|w|y)", text, re.IGNORECASE)
    if not components:
        return float(text)  # bare number == seconds
    return sum(
        int(count) * _WINDOW_UNIT_SECONDS[unit.lower()] for count, unit in components
    )


def _unreproducible_counter_range_windows(promql_expr, ts_bucket=None):
    """Authored rate/irate/increase lookbacks the ES|QL time bucket cannot stand in for.

    Grafana's dynamic *step* macros (``$__rate_interval``, ``$__interval``,
    ``$interval``, ``$__auto_interval_*``) are excluded: they resolve at render
    time to the view's step, so a window derived from one already means "follow
    the view", which is exactly what the bucket does. The ``$__range`` family is
    not a step — it is the whole view, many buckets wide — so it is reported like
    any other over-long window. Windowless calls are skipped too: their window is
    the rule pack's ``default_rate_window``, not an authored value.

    *ts_bucket* is the emitted bucket expression, which decides how long a
    lookback can be before it is lost (see
    :func:`_bucket_reproducible_window_seconds`).
    """
    windows = []
    limit, _fixed = _bucket_reproducible_window_seconds(ts_bucket)
    sanitized = _strip_promql_string_literals(promql_expr or "")
    for match in _SOURCE_COUNTER_RANGE_WINDOW_RE.finditer(sanitized):
        window = match.group("window")
        # A ``$__range`` window is "the whole view" and so is never
        # bucket-reproducible; a literal one has to outgrow the bucket.
        if not _RANGE_MACRO_RE.match(window):
            seconds = _window_seconds(window)
            if seconds is None or seconds <= limit:
                continue
        if window not in windows:
            windows.append(window)
    return windows


def _dropped_range_window_warning(windows, fixed_bucket=None):
    """Semantic-loss warning for a source lookback window ES|QL cannot keep.

    *fixed_bucket* is the bucket's human width when it is fixed, so the message
    can name what the rate is actually computed over instead of describing the
    adaptive case.
    """
    listed = ", ".join(f"[{window}]" for window in windows)
    if fixed_bucket:
        became = (
            f"this panel now reports a rate over the fixed {fixed_bucket} time "
            "bucket instead of that window"
        )
    else:
        became = (
            "this panel now reports a rate over the dashboard time range instead "
            "of that long fixed window (carrying the window alongside an adaptive "
            "bucket over-reads by up to 5x, so it cannot be kept)"
        )
    return (
        f"Dropped the source rate()/irate()/increase() lookback window {listed}: "
        f"ES|QL computes RATE/IRATE/INCREASE over the query's time bucket, so "
        f"{became}. Panels that differ only by such a window produce the same "
        "values here. Native PROMQL preserves the window and is the default when "
        "the target supports the ES|QL PROMQL command."
    )


@QUERY_POSTPROCESSORS.register("counter_range_window", priority=93)
def counter_range_window_rule(context):
    """Drop the explicit window from RATE/IRATE/INCREASE so it follows the bucket.

    Elasticsearch computes these over the TIME BUCKET, not over the window:
    ``RateDoubleGroupingAggregatorFunction.computeRate`` derives the value at
    ``tbucketStart``/``tbucketEnd`` (extrapolating to the boundaries) and divides
    by that span. The window argument defaults to ``NO_WINDOW`` --
    ``Duration.ZERO`` -- which means "use the bucket".

    Passing a fixed window alongside an ADAPTIVE ``TBUCKET(100, ?_tstart,
    ?_tend)`` therefore desynchronises the two as soon as the dashboard's range
    grows. Measured on the rig with node_cpu_seconds_total, whose correct idle
    rate is ~0.98:

        50 min range, 2.5 min buckets   RATE(x, 5m) 0.982   RATE(x) 0.982
        12 h   range, 7.2 min buckets   RATE(x, 5m) 1.937   RATE(x) 0.984

    A 12-hour view is ordinary, and every rate panel on it read about double; at
    24 hours the windowed form reads 5.70 against a true 0.984. Omitting the window
    is correct at every range because the bucket then defines the span on both
    sides. Note the adaptive bucket is essential to reproduce -- a fixed-width
    TBUCKET at the same range and window looks fine.

    Only the counter functions are touched. The ``*_OVER_TIME`` family takes its
    window as a genuine lookback and keeps it.

    Dropping the window is the only correct ES|QL shape, but it becomes a
    semantic loss once the window is longer than any bucket a dashboard view
    produces: ``rate(m[1d])`` and ``rate(m[1w])`` then collapse into the same
    query as ``rate(m[1h])``. Report those instead of hiding them (issue #379) —
    see :func:`_unreproducible_counter_range_windows` for the exclusions.
    """
    query = context.esql_query
    if not query or "(" not in query:
        return None
    rewritten = _COUNTER_RANGE_WINDOW_RE.sub(
        lambda m: f"{m.group(1)}({m.group('arg')})", query
    )
    stripped = rewritten != query
    if stripped:
        context.esql_query = rewritten
        query = rewritten
    # ``_range_call`` already emits the counter family windowless, so most
    # queries reach here with nothing to strip. Either way, any counter range
    # call left in the query now carries no window, so report the loss when the
    # source asked for one the bucket cannot stand in for.
    warned = False
    if _COUNTER_RANGE_CALL_RE.search(query):
        ts_bucket = getattr(context.rule_pack, "ts_bucket", None)
        lost_windows = _unreproducible_counter_range_windows(context.promql_expr, ts_bucket)
        if lost_windows:
            _, fixed_bucket = _bucket_reproducible_window_seconds(ts_bucket)
            _append_unique(
                context.warnings,
                _dropped_range_window_warning(lost_windows, fixed_bucket),
            )
            warned = True
    if stripped:
        return "counter range windows follow the time bucket"
    if warned:
        return "counter range window dropped; reported as a semantic loss"
    return None


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

    # Detect two-stage topk shape: the output metric is defined in a terminal
    # STATS line after the first time-bucket SORT, so wrapper filters/evals must
    # be inserted after that line, not before the first SORT.
    value_stats_idx = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.strip().startswith("| STATS")
            and re.search(rf"\b{re.escape(metric_field)}\s*=", ln)
        ),
        None,
    )

    def _eval_insert_idx(lines):
        if value_stats_idx is not None:
            return value_stats_idx + 1
        return next(
            (i for i, ln in enumerate(lines) if ln.strip().startswith("| SORT")),
            len(lines),
        )

    # round() → faithful round-to-nearest-multiple.
    # PromQL round(v, to_nearest) rounds v to the nearest multiple of
    # `to_nearest` (default 1). ES|QL ROUND(v, decimals) is different: its
    # second argument is a *whole number of decimal places*, so a fractional
    # step like 0.001 makes ROUND(v, 0.001) an invalid query
    # ("second argument ... must be [whole number ...], found ... [double]").
    # Emit ROUND(v / step) * step, which reproduces PromQL's semantics for any
    # step (integer or fractional) and is always valid ES|QL.
    if frag.extra.get("has_round"):
        precision = frag.extra.get("round_precision")
        if precision is None or precision in (0, 1):
            eval_clause = f"| EVAL {metric_field} = ROUND({metric_field})"
            round_warning = "round() approximated with ES|QL ROUND()"
        else:
            step = int(precision) if precision == int(precision) else precision
            eval_clause = (
                f"| EVAL {metric_field} = ROUND({metric_field} / {step}) * {step}"
            )
            round_warning = "round(v, step) emitted as ROUND(v / step) * step"
        lines.insert(_eval_insert_idx(lines), eval_clause)
        _append_unique(context.warnings, round_warning)
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
    # For two-stage topk, replace the final value-sort line rather than the first
    # SORT (which orders time buckets for the terminal value collapse).
    if "value_sort_desc" in frag.extra:
        sort_desc = frag.extra["value_sort_desc"]
        direction = "DESC" if sort_desc else "ASC"
        if value_stats_idx is not None:
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


def _has_approximated_agg_over_summary_ratio(frag, _depth=0):
    if frag is None or _depth > 8:
        return False
    if frag.extra.get("approximated_agg_over_summary_ratio"):
        return True
    return _has_approximated_agg_over_summary_ratio(
        frag.extra.get("left_frag"), _depth + 1
    ) or _has_approximated_agg_over_summary_ratio(frag.extra.get("right_frag"), _depth + 1)


@QUERY_POSTPROCESSORS.register("approx_agg_over_summary_ratio_note", priority=94)
def approx_agg_over_summary_ratio_note_rule(context):
    """Warn when ``sum(m_sum/m_count)`` was rewritten as a ratio of aggregates."""
    frag = context.fragment
    if not frag or not context.esql_query or context.feasibility == "not_feasible":
        return None
    if not _has_approximated_agg_over_summary_ratio(frag):
        return None
    _append_unique(context.warnings, _APPROX_AGG_OVER_SUMMARY_RATIO_WARNING)
    return "noted histogram summary-ratio approximation"


@QUERY_POSTPROCESSORS.register("post_filter", priority=95)
def post_filter_rule(context):
    frag = context.fragment
    post_filter = frag.extra.get("post_filter") if frag else None
    if not post_filter or not context.esql_query or not context.output_metric_field:
        return None
    value = _format_scalar_value(post_filter["value"])
    clause = f"| WHERE {context.output_metric_field} {post_filter['op']} {value}"
    lines = context.esql_query.splitlines()
    value_def_idx = next(
        (
            idx
            for idx, line in enumerate(lines)
            if line.strip().startswith(("| STATS", "| EVAL"))
            and re.search(rf"\b{re.escape(context.output_metric_field)}\s*=", line)
        ),
        None,
    )
    if value_def_idx is not None:
        lines.insert(value_def_idx + 1, clause)
    else:
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


# PromQL string literals in all three quote styles. The backtick form is raw, so
# it processes no escapes. Matcher values live here, and a ``label_<var>`` that
# only appears inside one is a string, never a column.
_PROMQL_STRING_LITERAL_RE = re.compile(r'"(?:\\.|[^"\\])*"' r"|'(?:\\.|[^'\\])*'" r"|`[^`]*`")

# A PromQL metric identifier, including the ``:`` segments of a recording rule.
_PROMQL_IDENTIFIER_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_:]*")

# An ES|QL column reference, optionally backtick-quoted (which is how a
# recording-rule name with ``:`` segments is emitted).
_ESQL_COLUMN_TOKEN_RE = re.compile(r"`[^`]+`|[A-Za-z_][A-Za-z0-9_.:]*")

# ``prometheus_remote_write`` stores each metric as ``prometheus.<name>.<suffix>``
# (see ``SchemaResolver.resolve_metric``), so the column carries a value suffix
# that is not part of the logical PromQL name.
_METRIC_VALUE_SUFFIXES = (".value", ".counter", ".rate")

# ``name =`` (but never the comparisons ``==``, ``>=``, ``<=``, ``!=``) and
# ``… AS name`` introduce a column the query computes for itself.
_ESQL_ASSIGNS_RE = re.compile(r"\s*=(?!=)")
_ESQL_RENAME_TARGET_RE = re.compile(r"\bAS\s*$", re.IGNORECASE)


def _identifier_counts(text: str) -> dict[str, int]:
    """How often each PromQL metric identifier occurs outside string literals."""
    counts: dict[str, int] = {}
    for token in _PROMQL_IDENTIFIER_TOKEN_RE.findall(_PROMQL_STRING_LITERAL_RE.sub(" ", text)):
        counts[token] = counts.get(token, 0) + 1
    return counts


def _esql_column_metric_name(token: str) -> str:
    """The logical PromQL metric name one ES|QL column token stands for.

    Strips backtick quoting, the field profile's prefix (``metrics.``,
    ``prometheus.``) and the ``prometheus_remote_write`` value suffix, so
    ``metrics.label_threshold``, ``prometheus.label_threshold.value`` and
    ``` `metric:label_threshold:rate5m` ``` all reduce to the PromQL name.
    """
    name = token.strip("`")
    for suffix in _METRIC_VALUE_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.rpartition(".")[2] or name


def _esql_assignment_rhs(text: str, start: int) -> str:
    """The right-hand side of an assignment whose ``=`` ends at ``start``.

    Ends at the comma separating the next assignment in the same command, or at
    the command's ``|``, whichever comes first — neither counted inside
    parentheses.
    """
    depth = 0
    idx = start
    while idx < len(text):
        char = text[idx]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and char in "|,":
            break
        idx += 1
    return text[start:idx]


def _esql_column_metric_names(esql_query: str) -> set[str]:
    """The metric names the query *reads from the index*.

    A bound parameter (``?threshold``) is a value supplied with the request, and
    the target of ``EVAL x = <expr>`` / ``STATS x = <expr>`` / ``RENAME y AS x``
    is a column the query computes, so neither occurrence can be a missing
    field — counting them would blame a variable for a query that merely *names*
    something ``label_<var>``.

    Assignment is judged per occurrence, in query order, because the renderer's
    ordinary shape is ``metric = AGG(metric)``: that right-hand side really does
    read the index field, so only a target that does not read itself is purely
    computed, and a read that happened *before* the name was computed still
    counts.
    """
    text = _STRING_LITERAL_RE.sub(" ", esql_query or "")
    read: set[str] = set()
    computed: set[str] = set()
    for match in _ESQL_COLUMN_TOKEN_RE.finditer(text):
        before = text[: match.start()]
        if before.endswith("?"):
            continue
        name = _esql_column_metric_name(match.group(0))
        assignment = _ESQL_ASSIGNS_RE.match(text, match.end())
        if assignment:
            rhs = _esql_assignment_rhs(text, assignment.end())
            if name not in {
                _esql_column_metric_name(token)
                for token in _ESQL_COLUMN_TOKEN_RE.findall(rhs)
            }:
                computed.add(name)
            continue
        if _ESQL_RENAME_TARGET_RE.search(before):
            computed.add(name)
            continue
        if name not in computed:
            read.add(name)
    return read


def _template_variable_placeholder_columns(promql_expr, esql_query, clean_expr=None):
    """Template variables that reached ``esql_query`` as ``label_<var>`` columns.

    ``preprocess_grafana_macros`` rewrites any ``$var`` it cannot bind as a
    label-matcher parameter into the bare PromQL identifier ``label_<var>`` so
    the AST parser still accepts the expression. In most shapes a later rule
    drops or blocks that placeholder, but a variable sitting in a binary
    operand (``... >= ($threshold / 100)``) is parsed as a genuine metric
    selector and survives into the emitted ES|QL as a column reference (issue
    #378).

    Returns the source variable names, in source order. Blaming a variable takes
    three pieces of evidence, so a target metric genuinely named ``label_...``
    is never mistaken for a placeholder:

    1. the name appears as a ``$var`` reference in the *source* PromQL;
    2. macro preprocessing *created* an identifier carrying ``label_<var>`` as a
       whole ``:``-delimited segment — it occurs more often in ``clean_expr``
       than in the source, so ``foo + label_threshold{job="$threshold"}`` (a real
       metric next to a matcher variable) is left alone while
       ``label_threshold + $threshold`` is not;
    3. the *emitted* query reads a column that resolves to exactly one of those
       created identifiers.

    ``clean_expr`` is the macro-expanded expression (``context.clean_expr``); it
    is recomputed when the caller does not have it.
    """
    raw = str(promql_expr or "")
    if clean_expr is None:
        clean_expr = preprocess_grafana_macros(raw)
    raw_counts = _identifier_counts(raw)
    clean_counts = _identifier_counts(str(clean_expr or ""))
    emitted = _esql_column_metric_names(str(esql_query or ""))

    names: list[str] = []
    for match in _GRAFANA_TEMPLATE_VAR_RE.finditer(raw):
        name = match.group("braced") or match.group("plain") or match.group("bracket")
        if not name or name.startswith("__") or name in names:
            continue
        placeholder = f"label_{name}"
        created = {
            identifier
            for identifier, count in clean_counts.items()
            if placeholder in identifier.split(":") and count > raw_counts.get(identifier, 0)
        }
        if created & emitted:
            names.append(name)
    return names


@QUERY_VALIDATORS.register("template_variable_placeholder_column", priority=13)
def template_variable_placeholder_column_rule(context):
    """Never ship a query that reads a Grafana template variable as a column.

    A template variable is a dashboard UI input, not telemetry, so the
    ``label_<var>`` placeholder names a field that can never exist: the panel
    uploads cleanly and then renders an Elasticsearch ``Unknown column`` error.
    Degrade to ``not_feasible`` — a "Manual review required" placeholder is
    honest about the gap, whereas the phantom column hides it behind an error
    tile (issue #378).

    Literal-valued variables (``textbox`` / ``constant``) are interpolated
    before translation by ``_substitute_literal_variable_values``, so this rule
    fires only for a variable whose value could not be represented as a PromQL
    literal — a query variable used as a scalar, a textbox holding a regex or a
    whole sub-expression, and so on.
    """
    if context.feasibility == "not_feasible" or not context.esql_query:
        return None
    names = _template_variable_placeholder_columns(
        context.promql_expr, context.esql_query, clean_expr=context.clean_expr
    )
    if not names:
        return None
    context.feasibility = "not_feasible"
    context.confidence = 0.0
    for name in names:
        _append_unique(
            context.warnings,
            f"Grafana template variable ({_template_var_display(name)}) is used as a "
            f"metric value, so the query would read column 'label_{name}' — a "
            "dashboard input is not telemetry, so that column can never exist. "
            "Requires manual redesign; a textbox or constant variable holding a "
            "number, duration, or metric name is inlined automatically",
        )
    return "template variable survived as a metric column"


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


@QUERY_VALIDATORS.register("late_bound_group_control", priority=35)
def late_bound_group_control_rule(context):
    """Confirm each deferred grouping variable reached the query as ``??var``.

    The guardrail strips ``by ($var)`` and records the variable (issue #282);
    ``_frag_group_labels`` re-attaches it as an ES|QL identifier control. Only a
    subset of query shapes route grouping through that seam, so if the emitted
    query never gained the ``??var`` identifier the grouping dimension was
    silently lost — revert to not_feasible with the original manual-redesign
    reason rather than ship a query missing its grouping.
    """
    late_bound = context.metadata.get("late_bound_group_vars")
    if not late_bound or context.feasibility == "not_feasible":
        return None
    query = context.esql_query or ""
    identifier_names = set(re.findall(r"\?\?([A-Za-z][A-Za-z0-9_]*)", query))
    value_names = set(
        re.findall(r"(?<!\?)\?(?!\?)([A-Za-z][A-Za-z0-9_]*)", query)
    )
    dual_semantics = sorted(identifier_names & value_names)
    if dual_semantics:
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        for name in dual_semantics:
            _append_unique(
                context.warnings,
                f"ES|QL parameter {name} is used as both value and field control; "
                "one dashboard control cannot preserve both semantics",
            )
        return "late-bound grouping variable conflicts with a value parameter"
    missing = [name for name in late_bound if f"??{name}" not in query]
    if missing:
        context.feasibility = "not_feasible"
        context.confidence = 0.0
        for name in missing:
            _append_unique(
                context.warnings,
                f"BY/WITHOUT clause contains Grafana template variable ({_template_var_display(name)}); "
                "grouping dimension is unknown at migration time and requires manual redesign",
            )
        return "late-bound grouping variable could not be represented as an ES|QL field control"
    context.metadata["late_bound_group_controls"] = list(late_bound)
    return "bound late grouping variable(s) to ES|QL field control: " + ", ".join(late_bound)


def _collect_source_metrics(frag, seen=None, dedup=True):
    """Walk a parsed fragment and collect the source metric names it reads.

    ``dedup=True`` (default) returns distinct names — what the live-schema rule
    wants so a metric missing from the target is reported once. ``dedup=False``
    returns one entry per *occurrence*, which the single-value gate needs to
    tell a same-metric ratio (two occurrences of one name, e.g.
    ``rate(http_requests_total{code=~"5.."}[5m]) / rate(http_requests_total
    [5m])`` — genuine derived arithmetic) apart from a bare single-metric
    expression that merely fans out.
    """
    seen = seen if seen is not None else set()
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
            metrics.extend(_collect_source_metrics(child, seen, dedup))
    rhs = getattr(frag, "binary_rhs", None)
    if rhs:
        metrics.extend(_collect_source_metrics(rhs, seen, dedup))
    return list(dict.fromkeys(metrics)) if dedup else metrics


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
    for metric in missing:
        resolved = _resolve_metric_field(resolver, metric, prefer="gauge") or metric
        _append_unique(
            context.warnings,
            f"Target field {resolved} is missing from live schema discovery (data readiness, not translation infeasibility)",
        )
    return "missing live metric fields (data readiness warning)"


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
        recovered = (llm_result or {}).get("esql_query")
        # The recovery runs after QUERY_VALIDATORS, so its query is never
        # re-validated. An LLM shown the *cleaned* PromQL sees the synthetic
        # ``label_<var>`` placeholder and reads it as a field, which would put
        # back exactly the phantom column this pass exists to keep out
        # (issue #378). Refuse such a recovery instead of resetting feasibility.
        if recovered and _template_variable_placeholder_columns(
            context.promql_expr, recovered, clean_expr=context.clean_expr
        ):
            _logger.info(
                "Rejected LLM translation that reads a template-variable column: %s", expr[:80]
            )
            recovered = None
        if recovered:
            _logger.info("LLM recovered not_feasible expression: %s", expr[:80])
            context.esql_query = recovered
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
