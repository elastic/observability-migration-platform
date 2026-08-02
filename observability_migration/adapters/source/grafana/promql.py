# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""PromQL fragment parsing and ES|QL planning helpers."""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from observability_migration.core.metric_mapping import plan_rate_transform
from observability_migration.core.verification.field_capabilities import NUMERIC_FIELD_TYPES

from .rules import RulePackConfig
from .runtime_features import binds_esql_named_params


def _is_counter_fallback(metric_name, rule_pack):
    """Heuristic counter detection when no schema resolver is available."""
    if not metric_name:
        return False
    kind = str(getattr(rule_pack, "metric_kinds", {}).get(metric_name, "")).strip().lower()
    if kind == "counter":
        return True
    if kind == "gauge":
        return False
    suffixes = getattr(rule_pack, "counter_suffixes", ["_total"])
    return any(metric_name.endswith(s) for s in suffixes)


def _is_label_enrichment_metric(metric_name, rule_pack):
    """Return True when ``metric_name`` matches the info-metric naming convention.

    A Prometheus ``*_info`` metric (``node_uname_info``, ``rabbitmq_identity_info``,
    ``kube_pod_info``, ...) is a gauge whose value is always ``1``, published
    solely so its labels can be joined onto a real metric. A ``group_left`` join
    against one is pure label enrichment (issue #197): dropping it and
    aggregating the primary metric alone does not change the numeric value.
    """
    if not metric_name:
        return False
    suffixes = getattr(rule_pack, "info_metric_suffixes", ["_info"])
    return any(metric_name.endswith(s) for s in suffixes)


# ES|QL reserved keywords that are illegal as a *bare* column identifier (in
# ``EVAL <id> =``, ``STATS <id> =``, ``KEEP <id>``). A Grafana legendFormat can
# legitimately be one of these (e.g. HAProxy's "IN"/"OUT" data-transfer legend),
# so when such a token is used as a column alias it must be backtick-quoted or
# ES|QL rejects the whole query (``mismatched input 'IN'``). Kept lowercase for
# case-insensitive matching; the emitted alias text is preserved verbatim.
_ESQL_RESERVED_IDENTIFIERS = frozenset(
    {
        "and",
        "as",
        "asc",
        "by",
        "desc",
        "false",
        "first",
        "in",
        "is",
        "last",
        "like",
        "limit",
        "not",
        "null",
        "or",
        "rlike",
        "true",
        "where",
    }
)


# Kibana ES|QL control-variable references. A single ``?`` prefixes a *value*
# control (``WHERE field == ?var``); a double ``??`` prefixes an *identifier*
# / field control (``STATS ... BY ??var``). Both are bound to a dashboard
# control at view time and must be emitted verbatim — never backtick-quoted,
# label-resolved, or dropped as if they were a concrete field name.
_ESQL_CONTROL_TOKEN_RE = re.compile(r"^\?\??[A-Za-z_][A-Za-z0-9_]*$")


def _is_esql_control_token(name) -> bool:
    """True when *name* is an ES|QL control-variable reference (``?v`` / ``??v``)."""
    return isinstance(name, str) and bool(_ESQL_CONTROL_TOKEN_RE.match(name))


def _esql_field(name: str) -> str:
    """Backtick-quote an ES|QL field reference that contains special characters.

    Plain identifiers ([a-zA-Z0-9_.]) are returned as-is.  Field paths that
    contain characters like ':' (Prometheus recording-rule metrics) or '-' are
    wrapped in backticks so ES|QL does not misinterpret them as operators.
    """
    if name and re.search(r"[^a-zA-Z0-9_.]", name):
        escaped = name.replace("`", "\\`")
        return f"`{escaped}`"
    return name


def _esql_identifier(name: str) -> str:
    """Quote a *column alias/identifier* for safe ES|QL emission.

    Like :func:`_esql_field` but also backtick-quotes bare tokens that collide
    with an ES|QL reserved keyword (``IN``, ``AS``, ``BY`` ...). Use this at
    every site that renders a (possibly legend-derived) alias as an identifier
    in the query text — ``EVAL``/``STATS``/``KEEP`` — so the stored column name
    stays verbatim while the query still parses. The bare name must continue to
    be used wherever Kibana matches a result *column* (panel ``metrics[].field``,
    legend label hints), since Kibana strips the backticks.
    """
    if not name:
        return name
    # A Kibana ES|QL control-variable reference (``?var`` value control or
    # ``??var`` identifier/field control) is late-bound at view time; emit it
    # verbatim so backticks never break the ``??var`` substitution.
    if _is_esql_control_token(name):
        return name
    if re.search(r"[^a-zA-Z0-9_.]", name):
        escaped = name.replace("`", "\\`")
        return f"`{escaped}`"
    if name.lower() in _ESQL_RESERVED_IDENTIFIERS:
        return f"`{name}`"
    return name


def _resolve_metric_field(resolver, metric_name, *, prefer=None, source_labels=None):
    """Resolve a PromQL metric name to its physical target field, ES|QL-escaped.

    Passes through to ``resolver.resolve_metric_field`` when a resolver is
    available, otherwise returns ``metric_name`` unchanged so callers without
    a resolver (offline / fallback paths) still emit the source-faithful
    field reference.  The returned field path is always safe to embed directly
    inside ES|QL STATS / WHERE expressions.

    ``source_labels`` selects among metric_map ``variants`` when present.
    """
    if resolver is None or not metric_name:
        return metric_name
    resolve = getattr(resolver, "resolve_metric_field", None)
    if resolve is None:
        return _esql_field(metric_name)
    try:
        return _esql_field(resolve(metric_name, prefer=prefer, source_labels=source_labels))
    except TypeError:
        # Older resolvers without source_labels.
        return _esql_field(resolve(metric_name, prefer=prefer))


def _frag_source_labels(frag) -> dict[str, str]:
    """Equality matchers from a PromQL fragment, for metric_map variant selection."""
    labels: dict[str, str] = {}
    for matcher in getattr(frag, "matchers", None) or []:
        if not isinstance(matcher, dict):
            continue
        if matcher.get("op") != "=":
            continue
        label = str(matcher.get("label") or "").strip()
        value = str(matcher.get("value") or "")
        if not label or label == "__name__":
            continue
        if _grafana_param_name(value) or re.search(r"\$\w", value):
            continue
        labels[label] = value
    return labels


def _resolve_frag_metric_field(frag, resolver, *, prefer=None):
    """Resolve ``frag.metric`` with fragment equality labels for variant maps."""
    return _resolve_metric_field(
        resolver,
        getattr(frag, "metric", None),
        prefer=prefer,
        source_labels=_frag_source_labels(frag),
    )


def _resolve_metric_map_result(resolver, metric_name, source_labels=None):
    if resolver is None or not metric_name:
        return None
    resolve = getattr(resolver, "resolve_metric_map_result", None)
    if resolve is None:
        return None
    try:
        return resolve(metric_name, source_labels=source_labels)
    except TypeError:
        return resolve(metric_name)


def _metric_map_attribute_filters(frag, resolver) -> list[str]:
    metric_name = getattr(frag, "metric", None)
    if not metric_name:
        return []
    result = _resolve_metric_map_result(
        resolver, metric_name, source_labels=_frag_source_labels(frag)
    )
    if result is None or not result.applied or result.entry is None:
        return []
    filters: list[str] = []
    for key, value in result.entry.attribute_filter.items():
        field = _esql_field(key)
        filters.append(f"{field} == {_quote_esql_string(value)}")
    return filters


def _metric_map_source_filter(frag, resolver) -> dict[str, str]:
    """Source matchers consumed by the selected metric_map variant."""
    metric_name = getattr(frag, "metric", None)
    if not metric_name:
        return {}
    result = _resolve_metric_map_result(
        resolver, metric_name, source_labels=_frag_source_labels(frag)
    )
    if result is None or not result.applied or result.entry is None:
        return {}
    return dict(result.entry.source_filter)


def _matcher_consumed_by_metric_map(matcher, consumed: dict[str, str]) -> bool:
    if not consumed or not isinstance(matcher, dict):
        return False
    if matcher.get("op") != "=":
        return False
    label = str(matcher.get("label") or "").strip()
    value = str(matcher.get("value") or "")
    return bool(label) and consumed.get(label) == value


def _apply_unit_scale(expr, scale):
    if scale is None or scale == 1.0:
        return expr
    return f"({expr}) * {scale}"


def _metric_map_unit_scale(resolver, metric_name, source_labels=None):
    result = _resolve_metric_map_result(resolver, metric_name, source_labels=source_labels)
    if result is None or not result.applied:
        return None
    return result.unit_scale


def _metric_map_target_index(resolver, metric_name, source_labels=None) -> str:
    result = _resolve_metric_map_result(resolver, metric_name, source_labels=source_labels)
    if result is None or not result.applied or result.entry is None:
        return ""
    return str(result.entry.target_index or "").strip()


def _metric_map_unapplied_notes(resolver, metric_name, source_labels=None) -> list[str]:
    """Panel notes when a map entry exists but was not applied (variant/scaffold gaps)."""
    result = _resolve_metric_map_result(resolver, metric_name, source_labels=source_labels)
    if result is None or result.applied:
        return []
    note = str(result.gap_reason or "").strip()
    if not note:
        note = f"metric_map[{metric_name!r}] was not applied"
    return [note]


def _metric_map_target_is_counter(resolver, result) -> bool | None:
    """Counter/gauge kind for the *mapped target* field (not the source name)."""
    if resolver is None or result is None:
        return None
    target_field = str(result.target or "").strip()
    if not target_field:
        return None
    if resolver.is_counter(target_field):
        return True
    if resolver.refutes_counter(target_field):
        return False
    return None


def _plan_metric_map_rate_transform(frag, resolver, esql_inner, is_counter):
    """Adjust rate emission per metric_map ``transform``."""
    warnings: list[str] = []
    metric_name = getattr(frag, "metric", None)
    if not metric_name:
        return esql_inner, is_counter, warnings
    result = _resolve_metric_map_result(
        resolver, metric_name, source_labels=_frag_source_labels(frag)
    )
    if result is None or not result.applied or result.entry is None:
        return esql_inner, is_counter, warnings
    transform = result.entry.transform
    source_has_rate = getattr(frag, "range_func", None) in {"rate", "irate", "increase"}
    target_is_counter = _metric_map_target_is_counter(resolver, result)
    # Rename-only maps onto a known gauge still must drop RATE(...); otherwise
    # ``sum(rate(prom_counter))`` becomes ``SUM(RATE(otel.gauge))`` and ES 400s.
    if transform == "none":
        if source_has_rate and target_is_counter is False:
            warnings.append(
                f"metric_map[{metric_name!r}] targets a gauge field; "
                "dropped source rate()/irate()/increase() to avoid RATE(gauge)"
            )
            return "", False, warnings
        return esql_inner, is_counter, warnings
    action, gap_reason = plan_rate_transform(
        source_has_rate=source_has_rate,
        transform=transform,
        target_is_counter=target_is_counter,
    )
    if gap_reason:
        warnings.append(gap_reason)
    if action == "drop_rate" and source_has_rate:
        # Target is a gauge (or pre-rated equivalent): emit the bare field under
        # the outer aggregate instead of LAST_OVER_TIME, which forces multi-target
        # normalize to wrap sibling gauges as SUM(SUM_OVER_TIME(...)).
        esql_inner = ""
        is_counter = False
    elif action == "to_rate" and not source_has_rate:
        if (esql_inner or "").upper() not in _COUNTER_INPUT_ESQL_FUNCS:
            esql_inner = "RATE"
            is_counter = True
    return esql_inner, is_counter, warnings


def _apply_metric_map_to_rate_on_simple(
    frag,
    resolver,
    rule_pack,
    *,
    source: str,
    time_filter: str,
    bucket_expr: str,
    metric_field: str,
    stats_expr: str,
    warnings: list[str],
):
    """Honor ``transform: to_rate`` on non-range PromQL (simple_metric / simple_agg)."""
    metric_name = getattr(frag, "metric", None)
    if not metric_name:
        return source, time_filter, bucket_expr, metric_field, stats_expr
    result = _resolve_metric_map_result(
        resolver, metric_name, source_labels=_frag_source_labels(frag)
    )
    if result is None or not result.applied or result.entry is None:
        return source, time_filter, bucket_expr, metric_field, stats_expr
    if result.entry.transform != "to_rate":
        return source, time_filter, bucket_expr, metric_field, stats_expr
    if getattr(frag, "range_func", None) in {"rate", "irate", "increase"}:
        return source, time_filter, bucket_expr, metric_field, stats_expr
    target_is_counter = _metric_map_target_is_counter(resolver, result)
    action, gap_reason = plan_rate_transform(
        source_has_rate=False,
        transform="to_rate",
        target_is_counter=target_is_counter,
    )
    if gap_reason:
        warnings.append(gap_reason)
    if action != "to_rate":
        return source, time_filter, bucket_expr, metric_field, stats_expr
    window = str(getattr(rule_pack, "default_rate_window", None) or "5m").strip() or "5m"
    metric_field = _resolve_frag_metric_field(frag, resolver, prefer="counter")
    source = "TS"
    time_filter = rule_pack.ts_time_filter
    bucket_expr = rule_pack.ts_bucket
    outer = OUTER_AGG_MAP.get(getattr(frag, "outer_agg", None) or "", "") or "SUM"
    stats_expr = f"{outer}(RATE({metric_field}, {window}))"
    return source, time_filter, bucket_expr, metric_field, stats_expr


def _frag_metric_field_raw(frag, resolver):
    """Unescaped physical field for the fragment's metric, for metric-aware
    label resolution (issue #163).

    Unlike ``_resolve_metric_field`` this returns the field *without* ES|QL
    backticks, because the co-occurrence probe in ``resolve_label`` adds its
    own. Returns ``None`` when there is no metric or the resolver cannot resolve
    one, so the label paths fall back to index-global resolution.
    """
    metric_name = getattr(frag, "metric", None)
    if not metric_name or resolver is None:
        return None
    resolve = getattr(resolver, "resolve_metric_field", None)
    if resolve is None:
        return metric_name
    labels = _frag_source_labels(frag)
    try:
        try:
            return resolve(metric_name, source_labels=labels) or metric_name
        except TypeError:
            return resolve(metric_name) or metric_name
    except Exception:
        return metric_name


def _resolve_label_for(resolver, label, metric_field=None):
    """Resolve a label through the resolver, metric-aware when a metric is given.

    Falls back to the bare label when there is no resolver. Centralizes the
    metric-aware vs index-global choice so the filter/group *generators* and the
    incompatibility *checks* resolve the same field (issue #163).
    """
    if not resolver:
        return label
    if metric_field:
        return resolver.resolve_label(label, metric_field=metric_field)
    return resolver.resolve_label(label)


def _prime_frag_label_cooccurrence(frag, resolver, preferred_labels=None):
    """Pre-warm metric-scoped co-occurrence for every label of the fragment in
    one batched probe (issue #182).

    Each ``_frag_*`` helper below resolves labels scoped to the same metric,
    one label at a time. Without priming, each first-resolution issues its own
    co-occurrence round-trip; with it, the union of all the fragment's
    selector-matcher and group-by labels is counted once, and the per-label
    resolutions then hit the warm cache. Idempotent and cache-backed, so calling
    it from several helpers for the same fragment still costs a single probe.
    """
    if frag is None or resolver is None or not hasattr(resolver, "prime_label_cooccurrence"):
        return
    metric_field = _frag_metric_field_raw(frag, resolver)
    if not metric_field:
        return
    labels = [m["label"] for m in (frag.matchers or [])]
    labels += [lbl for lbl in (frag.group_labels or []) if not lbl.startswith("label_")]
    if preferred_labels:
        labels += list(preferred_labels)
    if labels:
        resolver.prime_label_cooccurrence(labels, metric_field)

try:
    import promql_parser  # pyright: ignore[reportMissingImports]
except ImportError:
    promql_parser = None  # Checked at parse time; raises ImportError with install instructions

AGG_FUNCTION_MAP = {
    "rate": "RATE",
    "irate": "IRATE",
    "increase": "INCREASE",
    "avg_over_time": "AVG_OVER_TIME",
    "sum_over_time": "SUM_OVER_TIME",
    "max_over_time": "MAX_OVER_TIME",
    "min_over_time": "MIN_OVER_TIME",
    "count_over_time": "COUNT_OVER_TIME",
    "last_over_time": "LAST_OVER_TIME",
    "present_over_time": "PRESENT_OVER_TIME",
    "delta": "DELTA",
    "deriv": "DERIV",
    "histogram_quantile": "PERCENTILE_OVER_TIME",
}


# Degradations applied when the source PromQL asks for a counter-style
# range function but the resolved field is typed ``gauge`` (e.g. an
# Elastic ``/_prometheus/api/v1/write`` ingest that didn't detect a
# counter by name). ES|QL's RATE/IRATE/INCREASE require ``counter_*``
# typing; emitting them against a gauge produces a hard 400 in Kibana.
# The chosen gauge analogues let the panel still render real numbers
# while a warning explains the swap.
_COUNTER_TO_GAUGE_FALLBACK = {
    # ``rate``/``irate`` over a gauge degrades to the value averaged
    # across the window — not a per-second rate, but the closest honest
    # measurement available without a proper counter type.
    "rate": (
        "AVG_OVER_TIME",
        "Source PromQL used rate() but {metric} is typed as gauge in the "
        "target index; rendered as AVG_OVER_TIME instead. Fix the ingest "
        "mapping to mark this field as a counter to get a true rate.",
    ),
    "irate": (
        "AVG_OVER_TIME",
        "Source PromQL used irate() but {metric} is typed as gauge in the "
        "target index; rendered as AVG_OVER_TIME instead. Fix the ingest "
        "mapping to mark this field as a counter to get a true rate.",
    ),
    # ``increase`` is total change over the window; the closest gauge
    # analogue is MAX - MIN, but ES|QL only allows a single function
    # call inside STATS so we fall back to ``MAX_OVER_TIME`` (upper
    # bound of the cumulative value) and warn loudly.
    "increase": (
        "MAX_OVER_TIME",
        "Source PromQL used increase() but {metric} is typed as gauge in "
        "the target index; rendered as MAX_OVER_TIME (cumulative ceiling) "
        "instead. Fix the ingest mapping to mark this field as a counter "
        "to recover the true increase over the window.",
    ),
}


def _gauge_fallback_for_counter_range_func(range_func):
    """Return ``(esql_function, warning_template)`` to use when a
    counter-style range function (``rate``/``irate``/``increase``) is
    applied to a field that the target cluster has typed as a gauge.
    The warning template contains a ``{metric}`` placeholder for the
    caller to substitute the source metric name."""
    result = _COUNTER_TO_GAUGE_FALLBACK.get(range_func)
    if result is None:
        raise ValueError(
            f"no gauge fallback for range function {range_func!r}; "
            f"expected one of {sorted(_COUNTER_TO_GAUGE_FALLBACK)}"
        )
    return result


# ``rate``/``irate`` are *counter-only* in PromQL — a gauge cannot be rated — so
# the source asserting one is authoritative proof the metric is a counter.
# Live caps typing the field as gauge are treated as a stale/wrong ingest
# (surfaced as a warning at the call site), NOT as refutation: the telemetry
# contract locks rate()-ed fields as counters, so degrading on live caps bakes
# in a translation that hard-fails (400) once the ingest follows the contract.
# Only an explicit rule-pack ``metric_kinds: gauge`` pin forces the degrade.
# ``increase`` is excluded: it can be misused on a real gauge, so it keeps the
# conservative heuristic-driven degradation.
_COUNTER_ONLY_RANGE_FUNCTIONS = frozenset({"rate", "irate"})
_COUNTER_INPUT_ESQL_FUNCS = frozenset({"RATE", "IRATE", "INCREASE"})
# Source PromQL range functions that are counter-only by Prometheus convention.
# When a panel wraps a metric in one of these, the telemetry contract / seeder
# type that field as a counter, so even a heuristic "gauge" guess that degraded
# the call to a gauge analogue (increase -> MAX_OVER_TIME) still queries a
# counter-typed stored field and must cast.
_COUNTER_STYLE_SOURCE_FUNCS = frozenset({"rate", "irate", "increase"})


def _counter_safe_metric_arg(
    esql_func, metric_expr, is_counter, source_range_func=None, *, counter_refuted=False, force_cast=False
):
    """Cast a counter metric to double for ES|QL functions that reject counters.

    Casts when EITHER the field is a confirmed counter, OR the panel's *source*
    PromQL used a counter-only range function (rate/irate/increase) and the
    target has not authoritatively refuted the counter classification — mirroring
    the telemetry contract / seeder. RATE/IRATE/INCREASE consume the raw counter
    directly, so they are never cast; an authoritative gauge (``counter_refuted``)
    is left unchanged to avoid needless cast / snapshot churn.

    ``force_cast`` (issue #245) casts regardless of counter status — pass
    ``True`` when the target maps this field with conflicting types across
    indices, which can make ES|QL reject the bare form regardless of which
    aggregation is applied. Callers should compute it with
    :func:`_counter_unsafe_cast_needed`. It has no effect on
    RATE/IRATE/INCREASE, which cannot be cast without changing their meaning.

    This is the shared counter-safe helper used by both the direct/topk family
    rules and the composed binary measure-spec / join-ratio paths below, so
    degraded-range casting stays consistent.
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


def _counter_refuted(resolver, metric):
    """True when the target authoritatively says ``metric`` is NOT a counter
    (explicit rule-pack ``gauge`` pin or live gauge field-caps). Silent (False)
    when offline or the field is unknown, so a counter-style source function can
    still drive the counter-safe cast."""
    if resolver is None or not metric:
        return False
    refutes = getattr(resolver, "refutes_counter", None)
    return bool(refutes(metric)) if callable(refutes) else False


def _resolve_conflicting_type_candidate(resolver, metric):
    """Find the physical field -- ``metric`` itself, or a profile-resolved
    counter/gauge variant -- that the live target maps with conflicting
    *numeric* exact types across indices, if any (issue #245).

    Checks every candidate rather than trusting the caller to have already
    resolved the right one: ``metric`` may be the raw PromQL name (e.g. the
    shared RATE/IRATE/INCREASE degrade decision runs before the counter-vs-
    gauge choice, and thus before any physical field is resolved) or it may
    already be a caller-resolved physical field. Fleet ``prometheus.<metric>.
    value``/``.counter`` and native ``metrics.<metric>`` layouts can carry
    the conflict even when the bare logical name is absent from the live
    cache entirely, so a raw-name-only check would silently miss them. If
    ``metric`` is already a resolved physical field, re-resolving it through
    ``resolve_metric_field`` just yields extra candidates that fail to match
    and are harmlessly skipped.

    Returns ``(field_name, conflicting_types)`` for the first candidate with
    a same-family numeric conflict, or ``(None, [])`` when there is none.
    Excludes a conflict against a non-numeric type (e.g. ``keyword``
    alongside ``double``) -- that is a field-name collision between two
    unrelated series, not the same metric stored inconsistently, and casting
    to double would not resolve it. Existing behavior (keep the plain
    aggregation; other checks such as ``is_numeric_field`` decide
    feasibility) is left untouched for that case. Returns ``(None, [])``
    when offline or the resolver lacks the capability."""
    if resolver is None or not metric:
        return None, []
    has_conflicts = getattr(resolver, "has_conflicting_types", None)
    field_capability = getattr(resolver, "field_capability", None)
    if not callable(has_conflicts) or not callable(field_capability):
        return None, []
    resolve = getattr(resolver, "resolve_metric_field", None)
    candidates = [metric]
    if callable(resolve):
        for prefer in ("counter", "gauge"):
            candidate = resolve(metric, prefer=prefer)
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    for candidate in candidates:
        if not has_conflicts(candidate):
            continue
        capability = field_capability(candidate)
        conflicting_types = list(getattr(capability, "conflicting_types", None) or [])
        if conflicting_types and all(t in NUMERIC_FIELD_TYPES for t in conflicting_types):
            return candidate, conflicting_types
    return None, []


def _metric_has_conflicting_types(resolver, metric):
    """True when ``metric`` (or a profile-resolved physical variant of it)
    is mapped with different exact numeric types across indices (e.g.
    ``long`` in one dual-shipped index and ``double`` in another). ES|QL
    rejects a bare reference to such a field ("ambiguities in index
    mappings"); TO_DOUBLE is the documented fix for a same-family numeric
    mismatch. See :func:`_resolve_conflicting_type_candidate` for exactly
    which candidates are checked and why."""
    candidate, _ = _resolve_conflicting_type_candidate(resolver, metric)
    return candidate is not None


def _conflicting_type_cast_warning(metric, resolver):
    """Warning for defensively casting a metric whose target field (or a
    profile-resolved variant of it) is mapped with conflicting types across
    indices (#245) -- e.g. ``long`` in one dual-shipped index and ``double``
    in another. A bare reference to such a field is rejected by ES|QL
    ("ambiguities in index mappings"); the emitted query casts to double
    instead so it can still run."""
    candidate, conflicting_types = _resolve_conflicting_type_candidate(resolver, metric)
    field_name = candidate or metric
    types_desc = f" ({', '.join(conflicting_types)})" if conflicting_types else ""
    return (
        f"Target field '{field_name}' is mapped with conflicting types{types_desc} "
        "across indices; cast to double so the query can still run. Align the "
        "ingest mapping for this field to remove the ambiguity."
    )


def _counter_unsafe_cast_needed(metric, resolver):
    """True when a counter-unsafe aggregation over ``metric`` needs a
    defensive cast/wrap rather than a bare reference: the target maps the
    field with conflicting types across indices (issue #245), which can make
    ES|QL reject the bare form ("ambiguities in index mappings") regardless
    of which aggregation is applied.

    Deliberately narrower than the general "counter status unproven" case
    (issue #148, see :func:`_counter_type_uncertainty_warning`): that bucket
    is the offline/no-live-caps default for the vast majority of migrations
    and panels, and already has its own keep-the-query-and-warn behavior that
    a large, deliberately-asserted test corpus depends on. Conflicting live
    field-caps, by contrast, are a rare, concrete, target-verified signal —
    safe to act on unconditionally."""
    return _metric_has_conflicting_types(resolver, metric)


def _counter_unsafe_cast_warning(metric, resolver):
    """The warning to surface alongside the defensive cast/wrap triggered by
    :func:`_counter_unsafe_cast_needed`."""
    if _metric_has_conflicting_types(resolver, metric):
        return _conflicting_type_cast_warning(metric, resolver)
    return None


# Outer aggregations ES|QL rejects directly on counter_long/counter_double
# fields (verification_exception). ``count`` is intentionally absent — it counts
# documents, not values, so it is legal on counter types.
_COUNTER_UNSAFE_OUTER_AGGS = frozenset({"sum", "avg", "max", "min", "stddev", "quantile"})


def _counter_type_uncertainty_warning(metric, resolver):
    """Warning for emitting a counter-unsafe aggregation when the field's
    metric type could not be verified against live target capabilities (#148).

    The ``verification_exception`` this guards against only arises when live
    field capabilities were NOT available: counter detection then falls back to
    the ``_total`` naming heuristic, which OTel counter names
    (``trace_http_request_hits``, ``system_net_bytes_sent``, ...) do not match,
    so a bare ``SUM``/``MAX``/``MIN``/``AVG`` is emitted as feasible even though
    the field is stored as ``counter_long``/``counter_double`` in Elasticsearch.
    We keep the generated query (degrade gracefully) and surface the risk so the
    user can pin the metric kind.

    Returns ``None`` when: there is no resolver; live caps WERE fetched (the
    field is then either correctly typed — handled by ``is_counter`` — or absent
    and marked ``not_feasible`` upstream, so a warning would be dead or
    contradictory); or the target positively refutes counter typing (an explicit
    rule-pack ``gauge`` pin)."""
    if resolver is None:
        return None
    has_caps = getattr(resolver, "has_field_capabilities", None)
    if callable(has_caps) and has_caps():
        return None
    if resolver.refutes_counter(metric):
        return None
    return (
        f"Target field capabilities were unavailable, so the metric type of "
        f"'{metric}' could not be verified. If it is stored as "
        f"counter_long/counter_double in Elasticsearch, this panel may fail "
        f"with a verification_exception (ES|QL forbids standard aggregations "
        f"such as SUM/MAX/MIN/AVG on counter fields); most gauge fields are "
        f"unaffected. Pin 'metric_kinds: {metric}: counter' (or gauge) in the "
        f"rule pack, or re-run with target field capabilities reachable, to "
        f"resolve this."
    )


def _should_degrade_counter_range_func(range_func, metric, is_counter, resolver):
    """Whether a counter-style range function must degrade to a gauge analogue.

    Degrade when the resolved field is not a counter AND either the source
    function tolerates gauge misuse (``increase``) or the user explicitly
    pinned the metric as a gauge in the rule pack. A source ``rate``/``irate``
    otherwise keeps its true ``RATE``/``IRATE`` form — the source asserts the
    field is a counter (``rate`` is counter-only in PromQL) and the telemetry
    contract seeds such fields as counters, so live-caps gauge typing is a
    stale/wrong ingest to be fixed, not a reason to change the translation."""
    if is_counter:
        return False
    if range_func not in {"rate", "irate", "increase"}:
        return False
    if range_func in _COUNTER_ONLY_RANGE_FUNCTIONS:
        # Trust the source unless the user's rule pack explicitly pins gauge,
        # or the field's own mapping is too ambiguous for RATE/IRATE to run at
        # all (issue #245): a field with conflicting exact types across
        # indices can't be guaranteed counter_* in every index, and staying
        # source-faithful there just trades a "not counter" 400 for an
        # "ambiguities in index mappings" one. Degrade to the gauge analogue,
        # which the conflicting-types cast below can still make runnable.
        declared_gauge = getattr(resolver, "declared_gauge", None) if resolver else None
        return bool(declared_gauge and declared_gauge(metric)) or _metric_has_conflicting_types(resolver, metric)
    return True


def _target_gauge_disagreement_warning(range_func, metric):
    """Warning for a counter-only range function kept as RATE/IRATE while the
    live target currently types the field as gauge. The translation is
    source-faithful; the panel will fail at runtime until the ingest mapping
    is corrected (or the user pins the metric as gauge in the rule pack)."""
    esql_func = AGG_FUNCTION_MAP.get(range_func, range_func.upper())
    return (
        f"Source PromQL used {range_func}() on {metric} but the target "
        f"currently types this field as gauge; kept {esql_func} because "
        f"{range_func}() is counter-only and contract-faithful ingest types "
        "this field as a counter. Fix the ingest mapping to mark this field "
        f"as a counter, or pin metric_kinds {metric}: gauge in the rule pack "
        "if the gauge typing is intentional."
    )


def resolve_counter_range_translation(range_func, metric, is_counter, resolver, inner_func):
    """Apply the counter-vs-gauge policy for a counter-style range function.

    Single entry point for every translation path that emits
    RATE/IRATE/INCREASE (or their gauge analogues), so the degrade decision
    and its user-facing warnings stay consistent across call sites.

    Returns ``(inner_func, warning, is_counter)``: the ES|QL inner function
    to emit (the gauge analogue when degrading), an optional warning to
    surface on the panel, and the effective counter flag (flipped True when
    a counter-only source function overrides the gauge heuristic)."""
    if _should_degrade_counter_range_func(range_func, metric, is_counter, resolver):
        fallback_func, template = _gauge_fallback_for_counter_range_func(range_func)
        # The degraded form is also counter-unsafe, so when the target cannot
        # prove the field is a gauge (offline / field absent from caps) flag the
        # counter_long risk instead of asserting it "is typed as gauge". A
        # cross-index type conflict (#245) gets its own, more accurate warning
        # naming the actual problem (mapping ambiguity, not counter/gauge).
        if _metric_has_conflicting_types(resolver, metric):
            warning = _conflicting_type_cast_warning(metric, resolver)
        else:
            uncertainty = _counter_type_uncertainty_warning(metric, resolver)
            warning = uncertainty if uncertainty else template.format(metric=metric)
        return fallback_func, warning, is_counter
    warning = None
    if not is_counter and range_func in _COUNTER_ONLY_RANGE_FUNCTIONS:
        # Source rate()/irate() is counter-only; trust it over the gauge
        # heuristic, but surface the disagreement when live caps refute it.
        is_counter = True
        if resolver and resolver.refutes_counter(metric):
            warning = _target_gauge_disagreement_warning(range_func, metric)
    return inner_func, warning, is_counter


OUTER_AGG_MAP = {
    "sum": "SUM",
    "avg": "AVG",
    "max": "MAX",
    "min": "MIN",
    "count": "COUNT",
    "stddev": "STD_DEV",
    "quantile": "PERCENTILE",
}

SUPPORTED_RANGE_FUNCTIONS = {
    "avg_over_time",
    "count_over_time",
    "delta",
    "deriv",
    "increase",
    "irate",
    "last_over_time",
    "max_over_time",
    "min_over_time",
    "present_over_time",
    "rate",
    "sum_over_time",
}

# ``*_over_time`` range functions are instant gauge-shaped aggregations: on the
# TS path with only ``BY TBUCKET`` they yield one value per series per bucket, so
# legend labels need not enter BY (issue #99). Counter rates (rate/irate/increase/
# delta/deriv) keep their intentional outer-AVG downsample and are excluded here.
_OVER_TIME_RANGE_FUNCS = frozenset(
    {
        "avg_over_time",
        "count_over_time",
        "last_over_time",
        "max_over_time",
        "min_over_time",
        "present_over_time",
        "sum_over_time",
    }
)

_SET_OPERATORS = frozenset({"or", "and", "unless"})


HARD_UNSUPPORTED_AST_REASONS = {
    "__name__": "PromQL metric-name introspection via __name__ requires manual redesign",
    "offset": "Contains unsupported pattern: offset",
    "subquery": "Contains unsupported pattern: subquery",
    "without": "PromQL without aggregation requires manual redesign",
}

HARD_UNSUPPORTED_CALL_REASONS = {
    "absent": "absent() checks metric existence and has no ES|QL equivalent",
    "absent_over_time": "absent_over_time() checks metric existence and has no ES|QL equivalent",
    "bottomk": "bottomk requires manual redesign",
    "changes": "changes() counts value transitions and has no ES|QL equivalent",
    "count_values": "count_values requires manual redesign",
    "group": (
        "group() returns the constant 1 per label set (value-discarding); "
        "ES|QL has no equivalent and aggregating the metric value instead would "
        "change the result, so it requires manual redesign"
    ),
    "label_join": "label_join requires manual redesign",
    "resets": "resets() counts counter resets and has no ES|QL equivalent",
    "stdvar": (
        "stdvar() is population variance; ES|QL has no variance aggregation and "
        "STATS cannot square STD_DEV() inline, so it requires manual redesign"
    ),
    "timestamp": "timestamp() returns sample timestamps and has no ES|QL equivalent",
}

# PromQL elementwise math/trig wrappers with exact single-argument ES|QL
# equivalents. These are value-transforming wrappers (like sgn/clamp): strip the
# outer call, carry the function name, and emit `EVAL value = FN(value)` in the
# translator. The ES|QL rendering is defined in translate._MATH_FN_ESQL.
ELEMENTWISE_MATH_FUNCTIONS = frozenset(
    {
        "abs",
        "ceil",
        "floor",
        "sqrt",
        "exp",
        "ln",
        "log2",
        "log10",
        "acos",
        "acosh",
        "asin",
        "asinh",
        "atan",
        "atanh",
        "cos",
        "sin",
        "tan",
        "cosh",
        "sinh",
        "tanh",
        "deg",
        "rad",
    }
)


@dataclass
class PromQLFragment:
    """Intermediate representation of a parsed PromQL (sub-)expression."""

    metric: str = ""
    matchers: list = field(default_factory=list)
    range_func: str = ""
    range_window: str = ""
    outer_agg: str = ""
    group_labels: list = field(default_factory=list)
    group_mode: str = "by"
    binary_op: str = ""
    binary_rhs: Any = None
    scalar_value: float | None = None
    is_scalar: bool = False
    is_time_call: bool = False
    raw_expr: str = ""
    family: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class MeasureSpec:
    source_type: str
    time_filter: str
    bucket_expr: str
    group_fields: list
    filters: list
    alias: str
    stats_expr: str
    final_alias: str
    eval_expr: str = ""
    metric_name: str = ""
    metric_field: str = ""
    warnings: list = field(default_factory=list)
    target_index: str = ""


@dataclass
class FormulaPlan:
    specs: list
    expr: str
    warnings: list = field(default_factory=list)
    # Raw ``<lhs> <op> <rhs>`` condition when ``expr`` is a PromQL ``bool``
    # comparison indicator (``CASE(cond, 1, 0)``). A parent division uses this to
    # re-render the indicator with a NULL false-branch so it never divides by 0.
    bool_compare_cond: str = ""
    # Set when ``expr`` is a cross-metric PromQL ``or`` rendered as a
    # ``COALESCE(left, right, ...)`` union (left precedence, right fills the
    # gaps). The translator uses this to emit the correct set-union note instead
    # of the same-bucket arithmetic caveat.
    set_or_fill: bool = False
    # Set when ``expr`` is a same-metric PromQL ``or`` rewritten as a single
    # fetch with a unified WHERE OR clause (see
    # ``_try_rewrite_set_or_same_metric``). This is an exact rewrite, not an
    # approximation, so the translator must skip the same-bucket arithmetic
    # caveat for it too.
    set_or_where: bool = False


_GRAFANA_RANGE_MACRO_REPLACEMENTS = (
    ("__range_ms", "3600000"),
    ("__range_s", "3600"),
    ("__range", "1h"),
)
_GRAFANA_PARAM_VALUE_PREFIX = "__obs_migration_param_"
_GRAFANA_FULL_VAR_VALUE_RE = re.compile(
    r"^\s*(?:"
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}"
    r"|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
    r"|\[\[(?P<bracket>[A-Za-z_][A-Za-z0-9_]*)(?::[^\]]+)?\]\]"
    r")\s*$"
)
_PROMQL_LABEL_MATCHER_RE = re.compile(
    r"(?P<prefix>\s*[A-Za-z_][A-Za-z0-9_\.:-]*\s*(?:=~|!~|=|!=)\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)(?P<suffix>\s*)$",
    re.DOTALL,
)


def grafana_template_var_name(token: str) -> str | None:
    """Return the Grafana variable name when *token* is exactly one variable."""
    match = _GRAFANA_FULL_VAR_VALUE_RE.match(str(token or ""))
    if not match:
        return None
    return match.group("braced") or match.group("plain") or match.group("bracket")


def _has_unescaped_trailing_dollar(value: str) -> bool:
    if not value.endswith("$"):
        return False
    backslashes = 0
    idx = len(value) - 2
    while idx >= 0 and value[idx] == "\\":
        backslashes += 1
        idx -= 1
    return backslashes % 2 == 0


def _strip_promql_regex_anchors(value: str) -> str:
    """Drop PromQL regex anchors that ES|QL RLIKE treats as literals."""
    text = str(value or "")
    if text.startswith("^"):
        text = text[1:]
    if _has_unescaped_trailing_dollar(text):
        text = text[:-1]
    return text


def _grafana_param_value(name: str) -> str:
    return f"{_GRAFANA_PARAM_VALUE_PREFIX}{name}"


def _grafana_param_name(value: str) -> str | None:
    if not str(value or "").startswith(_GRAFANA_PARAM_VALUE_PREFIX):
        return None
    name = str(value)[len(_GRAFANA_PARAM_VALUE_PREFIX):]
    return name or None


def substitute_grafana_range_macros(expr):
    """Expand Grafana range macros before generic template-variable handling."""
    result = expr
    result = re.sub(r"\[\s*(?:\$\{__range_ms\}|\$__range_ms)\s*\]", "[3600000ms]", result)
    result = re.sub(r"\[\s*(?:\$\{__range_s\}|\$__range_s)\s*\]", "[3600s]", result)
    for name, replacement in _GRAFANA_RANGE_MACRO_REPLACEMENTS:
        result = re.sub(rf"\$\{{{name}\}}", replacement, result)
        result = re.sub(rf"\${name}\b", replacement, result)
    return result


def _parameterize_grafana_label_matchers(expr: str) -> str:
    """Preserve full-value Grafana label matcher variables as parseable params."""

    def rewrite_selector(selector_text):
        parts = []
        changed = False
        for part in _split_top_level_csv(selector_text):
            matcher = _PROMQL_LABEL_MATCHER_RE.match(part)
            if not matcher:
                parts.append(part)
                continue
            is_regex = "=~" in matcher.group("prefix") or "!~" in matcher.group("prefix")
            value = matcher.group("value")
            var_name = grafana_template_var_name(_strip_promql_regex_anchors(value) if is_regex else value)
            if not var_name or var_name.startswith("__"):
                parts.append(part)
                continue
            parts.append(
                f"{matcher.group('prefix')}{matcher.group('quote')}"
                f"{_grafana_param_value(var_name)}{matcher.group('quote')}"
                f"{matcher.group('suffix')}"
            )
            changed = True
        if not changed:
            return selector_text
        return ", ".join(parts)

    pieces = []
    start = 0
    idx = 0
    while idx < len(expr):
        if expr[idx] != "{":
            idx += 1
            continue
        pieces.append(expr[start:idx])
        end = idx + 1
        quote = ""
        escaped = False
        while end < len(expr):
            char = expr[end]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in ('"', "'"):
                quote = char
            elif char == "}":
                break
            end += 1
        if end >= len(expr) or expr[end] != "}":
            pieces.append(expr[idx:])
            return "".join(pieces)
        pieces.append("{" + rewrite_selector(expr[idx + 1 : end]) + "}")
        idx = end + 1
        start = idx
    pieces.append(expr[start:])
    return "".join(pieces)


def _normalize_count_scalar(expr):
    """Rewrite the removed Prometheus 1.x ``count_scalar(v)`` to ``scalar(count(v))``.

    ``count_scalar`` was dropped in Prometheus 2.0 but lingers in old community
    dashboards. It is exactly equivalent to ``scalar(count(v))``, which the
    translator already handles, so this substitution is lossless. The argument
    may contain its own parentheses/braces, so the closing paren is located by
    balancing rather than a naive regex (issue #63).
    """
    needle = "count_scalar("
    lowered = expr.lower()
    idx = lowered.find(needle)
    if idx == -1:
        return expr
    out = []
    pos = 0
    while idx != -1:
        out.append(expr[pos:idx])
        arg_start = idx + len(needle)
        depth = 1
        i = arg_start
        while i < len(expr) and depth:
            ch = expr[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            # Unbalanced parens: leave the original text untouched and stop.
            out.append(expr[idx:])
            return "".join(out)
        inner = expr[arg_start:i]
        out.append(f"scalar(count({inner}))")
        pos = i + 1
        idx = lowered.find(needle, pos)
    out.append(expr[pos:])
    return "".join(out)


def preprocess_grafana_macros(expr, rule_pack=None):
    """Replace Grafana-specific macros with valid PromQL placeholders."""
    default_window = (rule_pack.default_rate_window if rule_pack else "5m") or "5m"
    expr = _normalize_count_scalar(expr)
    # Grafana's dynamic step macros ($__interval / $__rate_interval /
    # $__auto_interval_* / $interval) resolve at render time from the selected
    # range and panel width; ES|QL has no equivalent, so they collapse to a
    # single window here. Honor rule_pack.default_rate_window (issue #87) so the
    # collapsed step is at least configurable per run instead of a hardcoded 5m.
    # $__range is the full dashboard time range, not a step, and has no rule-pack
    # knob, so it keeps its own 1h default.
    replacements = [
        (r"\$__rate_interval", default_window),
        (r"\$__interval", default_window),
        (r"\$__range", "1h"),
        (r"\$interval", default_window),
        (r"\[\$__interval\]", f"[{default_window}]"),
        (r"\[\$__rate_interval\]", f"[{default_window}]"),
        (r"\[\$__range\]", "[1h]"),
        (r"\[\$interval\]", f"[{default_window}]"),
        (r"\$__auto_interval_\w+", default_window),
    ]
    result = substitute_grafana_range_macros(expr)
    for pattern, replacement in replacements:
        result = re.sub(pattern, replacement, result)
    result = _parameterize_grafana_label_matchers(result)
    result = re.sub(r"\[\s*\$(?!__)([A-Za-z_][A-Za-z0-9_]*)\s*\]", f"[{default_window}]", result)
    # Subquery form [$var:$var] — must run BEFORE the general $var→label_var
    # pass so both halves are still recognisable as variables.
    result = re.sub(
        r"\[\s*\$(?!__)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\$(?!__)([A-Za-z_][A-Za-z0-9_]*)\s*\]",
        f"[{default_window}:1m]",
        result,
    )
    result = re.sub(r"\[\s*label_([A-Za-z_][A-Za-z0-9_]*)\s*\]", f"[{default_window}]", result)
    # Subquery form with one substituted half: [5m:$var], [$var:5m], or after
    # label_xxx substitution [5m:label_xxx] / [label_xxx:5m].  Any bracket
    # selector that contains a colon and a non-duration token is a subquery
    # with template variables; normalise to a concrete [range:step] so the AST
    # parser correctly flags it as "subquery" rather than an opaque parse error.
    _DUR_RE = r"(?:\d+(?:ms|s|m|h|d|w|y))"
    result = re.sub(
        rf"\[\s*({_DUR_RE})\s*:\s*(?!\s*\d)[^\]]+\]",
        f"[{default_window}:1m]",
        result,
    )

    # ${var} and ${var:format} — Grafana advanced variable interpolation.
    # Must run before the bare $var substitution so the opening brace isn't
    # left as a dangling token that confuses the PromQL AST parser.
    result = re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}",
        lambda m: f"label_{m.group(1)}",
        result,
    )
    # Skip substitution for pure-digit sequences ($1, $2, …) — those are
    # PromQL/regex capture-group backreferences inside label_replace() strings,
    # not Grafana template variables (which always start with a letter).
    result = re.sub(
        r"\$(\w+)",
        lambda m: m.group(0) if (m.group(1).startswith("__") or m.group(1)[0].isdigit()) else f"label_{m.group(1)}",
        result,
    )
    return result


# Functions whose listed (0-based) argument slots take a *scalar* (a number),
# not an instant/range vector. A Grafana dropdown sitting in one of these slots
# stands for a number (percentile, top-N, threshold), so substituting the
# dropdown's bare ``label_<name>`` identifier there produces a vector where a
# scalar is required and the PromQL parser rejects it (issue #157). When the
# dropdown's selected value is known we substitute the number instead.
_SCALAR_ARG_SLOTS = {
    "histogram_quantile": (0,),
    "quantile_over_time": (0,),
    "quantile": (0,),
    "topk": (0,),
    "bottomk": (0,),
    "limitk": (0,),
    "vector": (0,),
    "clamp": (1, 2),
    "clamp_min": (1,),
    "clamp_max": (1,),
    "round": (1,),
}

_SCALAR_VAR_TOKEN_RE = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}"
    r"|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
    r"|\[\[(?P<bracket>[A-Za-z_][A-Za-z0-9_]*)(?::[^\]]+)?\]\]"
)

# The aggregate operators among the scalar-slot functions. Unlike ordinary
# functions, PromQL aggregations accept an optional ``by (…)`` / ``without (…)``
# modifier that may appear *before* the argument list — ``topk by (pod) (5, …)``
# is equivalent to ``topk(5, …) by (pod)``. The argument list (and thus the
# scalar slot) sits after that leading modifier, so the call matcher has to skip
# it to land on the right ``(`` (issue #157 review follow-up).
_SCALAR_AGG_FUNCS = {"topk", "bottomk", "limitk", "quantile"}
_SCALAR_NONAGG_FUNCS = set(_SCALAR_ARG_SLOTS) - _SCALAR_AGG_FUNCS

# An optional leading aggregation modifier: ``by (a, b)`` or ``without ()``.
_AGG_MODIFIER = r"(?:(?:by|without)\b\s*\([^)]*\)\s*)?"

_SCALAR_FUNC_CALL_RE = re.compile(
    r"(?<![\w:.])(?:"
    + r"(?P<agg>" + "|".join(sorted(_SCALAR_AGG_FUNCS, key=len, reverse=True)) + r")\s*" + _AGG_MODIFIER + r"\("
    + r"|"
    + r"(?P<func>" + "|".join(sorted(_SCALAR_NONAGG_FUNCS, key=len, reverse=True)) + r")\s*\("
    + r")",
    re.IGNORECASE,
)


def _scan_call_args(text, open_idx):
    """Split a function call's arguments at top-level commas.

    ``open_idx`` is the index of the call's ``(``. Returns
    ``(args, end_idx)`` where *args* is the list of raw argument substrings
    and *end_idx* is the index just past the matching ``)``. Returns
    ``(None, len(text))`` when the parentheses are unbalanced.

    Commas inside nested parentheses, ``{...}`` label selectors, ``[...]``
    ranges, and quoted strings do not split arguments.
    """
    paren = 0
    bracket = 0
    args = []
    start = open_idx + 1
    quote = ""
    escaped = False
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren -= 1
            if paren == 0:
                args.append(text[start:i])
                return args, i + 1
        elif ch in "{[":
            bracket += 1
        elif ch in "}]":
            bracket -= 1
        elif ch == "," and paren == 1 and bracket == 0:
            args.append(text[start:i])
            start = i + 1
        i += 1
    return None, n


def substitute_scalar_template_vars(expr, values):
    """Substitute Grafana template variables that sit in a *scalar* argument
    slot with their resolved numeric value (issue #157).

    *values* maps a Grafana variable name to the numeric string the dropdown is
    set to (e.g. ``{"quantile": "0.95", "top_n": "5"}``). Only variables present
    in *values* are substituted, and only when they occur inside a scalar slot
    of one of :data:`_SCALAR_ARG_SLOTS` (``histogram_quantile``'s percentile,
    ``topk``'s ``k``, ``vector``'s value, a ``clamp`` bound, etc.). Every other
    occurrence is left untouched so the generic ``$var`` handling and the
    label-matcher / control machinery still apply elsewhere.

    Substituting before either translation path runs turns
    ``histogram_quantile($quantile, …)`` into ``histogram_quantile(0.95, …)`` —
    valid PromQL that migrates into a working panel instead of silently
    degrading to a "Migration Required" placeholder.
    """
    if not values or not isinstance(expr, str) or not expr:
        return expr
    if "$" not in expr and "[[" not in expr:
        return expr

    def _replace_tokens(text):
        def repl(match):
            name = match.group("braced") or match.group("plain") or match.group("bracket")
            replacement = values.get(name)
            return replacement if replacement is not None else match.group(0)

        return _SCALAR_VAR_TOKEN_RE.sub(repl, text)

    def process(text):
        out = []
        pos = 0
        while True:
            match = _SCALAR_FUNC_CALL_RE.search(text, pos)
            if not match:
                out.append(text[pos:])
                break
            fname = (match.group("agg") or match.group("func")).lower()
            open_idx = match.end() - 1
            args, end_idx = _scan_call_args(text, open_idx)
            if args is None:
                out.append(text[pos:])
                break
            out.append(text[pos:match.end()])
            slots = _SCALAR_ARG_SLOTS[fname]
            rendered = []
            for idx, arg in enumerate(args):
                processed = process(arg)
                if idx in slots:
                    processed = _replace_tokens(processed)
                rendered.append(processed)
            out.append(",".join(rendered))
            out.append(")")
            pos = end_idx
        return "".join(out)

    return process(expr)


def classify_promql_complexity(expr, rule_pack=None):
    """Classify a PromQL expression's translation complexity."""
    rule_pack = rule_pack or RulePackConfig()
    for rule in rule_pack.not_feasible_patterns:
        if re.search(rule.pattern, expr, re.IGNORECASE):
            return "not_feasible", rule.reason
    for rule in rule_pack.warning_patterns:
        if re.search(rule.pattern, expr, re.IGNORECASE):
            return "warning", rule.reason
    return "feasible", ""


def _normalize_range_window(seconds):
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _detect_outer_agg(expr):
    agg_pattern = "|".join(re.escape(agg) for agg in OUTER_AGG_MAP)
    match = re.match(rf"^\s*(?P<agg>{agg_pattern})\b", expr, re.IGNORECASE)
    if match:
        return match.group("agg").lower()
    return None


def _trim_outer_parens(expr):
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        balanced = True
        for idx, char in enumerate(expr):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and idx != len(expr) - 1:
                    balanced = False
                    break
        if not balanced or depth != 0:
            break
        expr = expr[1:-1].strip()
    return expr


MAX_ALIAS_LENGTH = 128

_GRAFANA_TEMPLATE_SUB_RE = re.compile(r"\{\{[^}]*\}\}")
_UNDERSCORE_RUN_RE = re.compile(r"_+")


def _strip_grafana_substitutions(text):
    """Drop ``{{label}}`` placeholders so internal aliases stay readable.

    Grafana legend formats embed runtime label substitutions like
    ``{{instance}}``.  Those values are emitted as separate ES|QL columns,
    so they do not need to leak into the synthetic alias used as a column
    name.  Stripping them upstream avoids ``on____instance`` artefacts.
    """
    if not text:
        return text
    cleaned = _GRAFANA_TEMPLATE_SUB_RE.sub("", str(text))
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _sanitize_alias_token(raw):
    if not raw:
        return ""
    token = re.sub(r"[^a-zA-Z0-9_]", "_", str(raw))
    token = _UNDERSCORE_RUN_RE.sub("_", token)
    return token.strip("_")


def _truncate_alias_at_word_boundary(alias, limit):
    if len(alias) <= limit:
        return alias
    truncated = alias[:limit]
    if "_" in truncated:
        head, _ = truncated.rsplit("_", 1)
        head = head.rstrip("_")
        if head:
            return head
    return truncated.rstrip("_") or alias[:limit].rstrip("_")


def _safe_alias(raw, suffix=""):
    alias = _sanitize_alias_token(raw) or "value"
    if alias and alias[0].isdigit():
        alias = f"series_{alias}"
    safe_suffix = _sanitize_alias_token(suffix)
    if safe_suffix:
        alias = f"{alias}_{safe_suffix}"
    if len(alias) > MAX_ALIAS_LENGTH:
        alias = _truncate_alias_at_word_boundary(alias, MAX_ALIAS_LENGTH)
    return alias


def _unique_safe_alias(raw, used_aliases, fallback_suffix=""):
    seed = _strip_grafana_substitutions(raw) or raw
    alias = _safe_alias(seed)
    if alias not in used_aliases:
        used_aliases.add(alias)
        return alias
    alias = _safe_alias(seed, fallback_suffix)
    if alias not in used_aliases:
        used_aliases.add(alias)
        return alias
    base = _safe_alias(seed)
    counter = 2
    candidate = f"{base}_{counter}"
    while candidate in used_aliases:
        counter += 1
        candidate = f"{base}_{counter}"
    used_aliases.add(candidate)
    return candidate


def _format_scalar_value(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _quote_esql_string(value):
    return json.dumps(value)


def _split_top_level_csv(expr):
    parts = []
    current = []
    depth = 0
    in_quote = None
    escaped = False
    for char in expr:
        if in_quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_quote:
                in_quote = None
            continue
        if char in ('"', "'"):
            in_quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(depth - 1, 0)
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _parse_selector_matchers(selector_text):
    matchers = []
    for part in _split_top_level_csv(selector_text or ""):
        match = re.match(r'\s*([A-Za-z_][A-Za-z0-9_\.:-]*)\s*(=~|!~|=|!=)\s*([\'"])(.*?)\3\s*$', part)
        if not match:
            continue
        matchers.append(
            {
                "label": match.group(1),
                "op": match.group(2),
                "value": match.group(4),
            }
        )
    return matchers


_PROMQL_VALID_LABEL_RE = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")
_PROMQL_LABEL_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\.:-]*$")


def _sanitize_promql_labels_for_ast(expr):
    replacements = {}

    def safe_label(label):
        if _PROMQL_VALID_LABEL_RE.match(label):
            return label
        if label not in replacements:
            counter = len(replacements)
            candidate = f"__obs_migration_sanitized_label_{counter}"
            while candidate in expr or candidate in replacements.values():
                counter += 1
                candidate = f"__obs_migration_sanitized_label_{counter}"
            replacements[label] = candidate
        return replacements[label]

    def sanitize_selector(selector_text):
        parts = []
        changed = False
        for matcher_text in _split_top_level_csv(selector_text):
            match = re.match(
                r"(?P<prefix>\s*)(?P<label>[A-Za-z_][A-Za-z0-9_\.:-]*)(?P<space>\s*)(?P<op>=~|!~|=|!=)(?P<rest>.*)\s*$",
                matcher_text,
                flags=re.DOTALL,
            )
            if not match:
                parts.append(matcher_text)
                continue
            replacement = safe_label(match.group("label"))
            changed = changed or replacement != match.group("label")
            parts.append(
                f"{match.group('prefix')}{replacement}{match.group('space')}{match.group('op')}{match.group('rest')}"
            )
        return ", ".join(parts), changed

    pieces = []
    idx = 0
    changed_selectors = False
    while idx < len(expr):
        if expr[idx] != "{":
            pieces.append(expr[idx])
            idx += 1
            continue
        end = idx + 1
        quote = ""
        escaped = False
        while end < len(expr):
            char = expr[end]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in ('"', "'"):
                quote = char
            elif char == "}":
                break
            end += 1
        if end >= len(expr) or expr[end] != "}":
            pieces.append(expr[idx:])
            idx = len(expr)
            break
        selector_text, selector_changed = sanitize_selector(expr[idx + 1 : end])
        changed_selectors = changed_selectors or selector_changed
        pieces.append("{" + selector_text + "}")
        idx = end + 1
    sanitized = "".join(pieces) if changed_selectors else expr

    def grouping_repl(match):
        labels = []
        changed = False
        for label in _split_top_level_csv(match.group("labels")):
            stripped = label.strip()
            if _PROMQL_LABEL_TOKEN_RE.match(stripped):
                replacement = safe_label(stripped)
                changed = changed or replacement != stripped
                labels.append(replacement)
            else:
                labels.append(stripped)
        if not changed:
            return match.group(0)
        return f"{match.group('kw')}({', '.join(labels)})"

    grouping_pattern = re.compile(
        r"\b(?P<kw>by|without|on|ignoring)\s*\((?P<labels>[^)]*)\)",
        flags=re.IGNORECASE,
    )

    pieces = []
    start = 0
    idx = 0
    quote = ""
    escaped = False
    while idx < len(sanitized):
        char = sanitized[idx]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
                pieces.append(sanitized[start : idx + 1])
                start = idx + 1
        elif char in ('"', "'"):
            if start < idx:
                pieces.append(grouping_pattern.sub(grouping_repl, sanitized[start:idx]))
            start = idx
            quote = char
        idx += 1
    if start < len(sanitized):
        if quote:
            pieces.append(sanitized[start:])
        else:
            pieces.append(grouping_pattern.sub(grouping_repl, sanitized[start:]))
    sanitized = "".join(pieces) if pieces else sanitized
    return sanitized, {safe: original for original, safe in replacements.items()}


def _le_float_alt(value: str) -> str | None:
    """Return the ".0" float alternative for an integer-string histogram boundary.

    Some Prometheus exporters (e.g. express-prometheus-middleware) store the
    histogram `le` label as "1.0" / "10.0" while Grafana dashboards reference
    them as "1" / "10".  When the value has no decimal point and parses as a
    non-negative integer we return the float form so callers can emit an OR
    clause covering both representations.
    """
    if "." in value or "e" in value.lower():
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    if not (0 <= f < 1e15) or f != int(f):
        return None
    return f"{int(f)}.0"


# Labels that use floating-point storage in some Prometheus exporters.
_FLOAT_LABEL_NAMES = frozenset({"le"})


def _target_binds_label_matcher_params(resolver):
    """Whether the target can bind Grafana ``$var`` matchers as ES|QL params.

    The ES|QL ``WHERE field == ?var`` / ``RLIKE ?var`` path needs ES|QL
    named-parameter binding, which the target advertises either via the broader
    ``esql_named_param_binding`` capability or via ``promql_label_matcher_params``
    (a subset). Gating on both means a cluster-wide ES|QL fallback run can
    still preserve ``?var`` label filters instead of dropping them (issue #132).
    Targets that advertise neither fall back to dropping the matcher (issue #100).
    """
    return binds_esql_named_params(getattr(resolver, "_rule_pack", None))


def _param_binds_regex_default(resolver, param_name):
    """Whether *param_name*'s binding control defaults to the regex match-all.

    Grafana ``All``/multi template variables with no single ``current`` value
    bind their ES|QL control to the regex match-all (".*"). Equality matchers
    on such a param must be emitted as regex matches so the default selects
    every series instead of comparing the field against the literal string
    ".*" (PR #133 review). The set is populated per dashboard on the shared
    rule pack in ``translate_dashboard``; absent it (single-expression
    translation, no dashboard context) equality matchers keep exact-match
    semantics.
    """
    rule_pack = getattr(resolver, "_rule_pack", None)
    names = getattr(rule_pack, "_regex_default_param_names", None)
    return bool(names) and param_name in names


def _param_is_multi_select(resolver, param_name):
    """Whether *param_name* comes from a Grafana ``multi: true`` variable."""
    rule_pack = getattr(resolver, "_rule_pack", None)
    names = getattr(rule_pack, "_multi_select_param_names", None)
    return bool(names) and param_name in names


def _mv_contains_filter(label, param_name, negate=False):
    """Multi-value label filter for a Grafana multi-select variable.

    ``RLIKE ?var`` is a scalar parameter position, so it can bind only one
    value and forces the Kibana control to single-select. ``MV_CONTAINS`` is
    Kibana's supported multi-value mechanism and pairs with
    ``single_select: false``.

    The ``".*"`` disjunct preserves Grafana's ``All``: the binding control's
    option list already offers ``.*`` (injected by ``MV_APPEND`` in the control
    query) and defaults to it, so selecting All yields ``[".*"]`` and matches
    every series, while any explicit selection matches just those values.

    Matching is exact rather than regex. That is forced by the platform, not a
    preference: ES|QL ``RLIKE`` requires a literal pattern and rejects a
    computed one, so ``RLIKE MV_CONCAT(?var, "|")`` -- which would have
    rebuilt Grafana's own ``(a|b)`` alternation -- is not expressible.
    """
    expr = (
        f'(MV_CONTAINS(?{param_name}, ".*")'
        f" OR MV_CONTAINS(?{param_name}, {label}))"
    )
    return f"NOT {expr}" if negate else expr


def _absent_aware(label, predicate, empty_predicate):
    """Make a matcher treat an absent label as "", without losing pushdown.

    Prometheus has no NULL: a series that does not carry a label behaves as if
    the label were "". ES|QL NULL propagates instead, so `release RLIKE ?p`
    drops those series (measured: 0 matched where Prometheus matched 1565).

    The obvious fix, ``COALESCE(field, "") RLIKE ?p``, is correct but wraps the
    field in a function and Elasticsearch can no longer push the filter down to
    Lucene -- measured on the rig, 176 documents scanned instead of 16 for the
    same selective filter, on every label matcher of every panel.

    This form keeps both: the first disjunct is a bare field comparison that
    still pushes down, and the second only fires for documents where the field is
    absent, deciding via the same predicate applied to "". Verified identical
    results to the COALESCE form and identical document counts to the bare one.
    """
    return f"({predicate} OR ({label} IS NULL AND {empty_predicate}))"


def _matcher_to_esql(matcher, resolver, metric_field=None):
    label = _resolve_label_for(resolver, matcher["label"], metric_field)
    op = matcher["op"]
    value = matcher["value"]
    if not label:
        return None
    if _matcher_has_incompatible_target_field(matcher, label, resolver):
        return None
    if op in {"=~", "!~"}:
        value = _strip_promql_regex_anchors(value)
    param_name = _grafana_param_name(value)
    if param_name:
        if not _target_binds_label_matcher_params(resolver):
            # Capability-off targets cannot bind ``?var`` ES|QL parameters, so
            # keeping ``?var`` here would make uploaded dashboards fail with
            # "Unknown query parameter [var]". Drop the matcher and let a
            # generic dashboard filter control apply it instead (issue #100).
            return None
        # Capability-on: preserve the variable-driven label filter as a native
        # ES|QL named parameter bound by an esqlControl, instead of silently
        # dropping it (issues #64 / #131). The matching control is guaranteed
        # by ``_ensure_param_controls`` during dashboard assembly.
        if _param_is_multi_select(resolver, param_name) and op in {"=", "=~", "!=", "!~"}:
            # Grafana multi-select: bind the whole selection, not one value.
            return _mv_contains_filter(
                label, param_name, negate=op in {"!=", "!~"}
            )
        if op == "=":
            if _param_binds_regex_default(resolver, param_name):
                # The binding control defaults this param to the regex
                # match-all (".*") because the Grafana variable is All/multi
                # with no single ``current`` value. ES|QL ``==`` would compare
                # the field against the literal string ".*" and match nothing
                # on first load (PR #133 review), so emit a regex match: the
                # match-all default then selects every series, mirroring
                # Grafana auto-rewriting ``label="$var"`` to ``label=~"..."``
                # for All/multi variables. (allValue-as-regex equality is a
                # narrower residual not covered here.)
                return _absent_aware(
                    label, f"{label} RLIKE ?{param_name}", f'"" RLIKE ?{param_name}'
                )
            return _absent_aware(
                label, f"{label} == ?{param_name}", f'"" == ?{param_name}'
            )
        if op == "!=":
            # Left as ``!=``: with the match-all default the param resolves to
            # ".*" and ``field != ".*"`` still matches every series (a safe,
            # non-empty default), unlike the ``==`` case which would be empty.
            return _absent_aware(
                label, f"{label} != ?{param_name}", f'"" != ?{param_name}'
            )
        if op == "=~":
            return _absent_aware(
                label, f"{label} RLIKE ?{param_name}", f'"" RLIKE ?{param_name}'
            )
        if op == "!~":
            return _absent_aware(
                label,
                f"NOT ({label} RLIKE ?{param_name})",
                f'NOT ("" RLIKE ?{param_name})',
            )
        return None
    # Drop preprocessed Grafana variables (label_Var / ^label_Var*) and
    # unprocessed special variables ($__interval etc.).  Use \$\w to avoid
    # false-positives on regex end-of-string anchors like ".*cam(era)?$".
    if value.startswith("label_") or value.startswith("^label_") or re.search(r"\$\w", value):
        return None
    if op == "=":
        if matcher["label"] in _FLOAT_LABEL_NAMES:
            alt = _le_float_alt(value)
            if alt is not None:
                return (
                    f"({label} == {_quote_esql_string(value)}"
                    f" OR {label} == {_quote_esql_string(alt)})"
                )
        return f"{label} == {_quote_esql_string(value)}"
    if op == "!=":
        # PromQL matches an absent label here (absent == ""), ES|QL NULL does not.
        return _absent_aware(
            label,
            f"{label} != {_quote_esql_string(value)}",
            f'"" != {_quote_esql_string(value)}',
        )
    if op == "=~":
        if value in (".*", ".+", ""):
            return None
        return f"{label} RLIKE {_quote_esql_string(value)}"
    if op == "!~":
        if value in (".*", ".+", ""):
            return None
        return _absent_aware(
            label,
            f"NOT ({label} RLIKE {_quote_esql_string(value)})",
            f'NOT ("" RLIKE {_quote_esql_string(value)})',
        )
    return None


def _matcher_has_incompatible_target_field(matcher, label, resolver):
    """Return True when field caps prove a string matcher would fail at runtime."""
    if resolver is None or not hasattr(resolver, "is_text_like_field"):
        return False
    if matcher.get("label") in _FLOAT_LABEL_NAMES:
        return False
    if matcher.get("op") not in {"=", "!=", "=~", "!~"}:
        return False
    exists = resolver.field_exists(label) if hasattr(resolver, "field_exists") else None
    if exists is not True:
        return False
    return resolver.field_type_family(label) == "numeric"


def _common_matchers(left_matchers, right_matchers):
    right_lookup = {(m["label"], m["op"], m["value"]) for m in right_matchers}
    return [m for m in left_matchers if (m["label"], m["op"], m["value"]) in right_lookup]


def _build_where_lines(filters):
    return [f"| WHERE {flt}" for flt in filters if flt]


def _selector_filters(matchers, resolver):
    filters = []
    for matcher in matchers:
        filter_expr = _matcher_to_esql(matcher, resolver)
        if filter_expr:
            filters.append(filter_expr)
    return filters


def _parse_logql_selector(expr):
    match = re.search(r"\{(?P<selectors>[^}]*)\}", expr)
    if not match:
        return [], []
    selector_text = match.group("selectors")
    matchers = _parse_selector_matchers(selector_text)
    fields = [matcher["label"] for matcher in matchers]
    return matchers, fields


def _parse_logql_search(expr):
    match = re.search(r'\|\s*(?:~|=)\s*"([^"]*)"', expr)
    if not match:
        return ""
    return match.group(1)


def _build_log_message_filter(search_expr, rule_pack):
    if not search_expr:
        return None
    # Strip leading inline regex flags like (?i) before variable-reference checks so
    # that "(?i)$searchable_pattern" (preprocessed Grafana variable) is correctly dropped
    # rather than rendered as RLIKE ".*(?i)label_searchable_pattern.*".
    check = re.sub(r"^\(\?[imsx-]+\)", "", search_expr).strip()
    if check.startswith("$") or check.startswith("label_") or re.search(r"\$\w", check):
        return None
    if re.fullmatch(r"[A-Za-z0-9_\-\. ]+", search_expr):
        return f'{rule_pack.logs_message_field} LIKE {_quote_esql_string(f"*{search_expr}*")}'
    if not search_expr.startswith(".*"):
        search_expr = f".*{search_expr}"
    if not search_expr.endswith(".*"):
        search_expr = f"{search_expr}.*"
    return f"{rule_pack.logs_message_field} RLIKE {_quote_esql_string(search_expr)}"


def _extract_group_labels(expr):
    match = re.search(r"\b(?:by|without)\s*\(([^)]+)\)", expr, re.IGNORECASE)
    if not match:
        return []
    return [label.strip() for label in match.group(1).split(",") if label.strip()]


def _ast_node_expr(node):
    prettify = getattr(node, "prettify", None)
    if callable(prettify):
        try:
            return prettify()
        except Exception:
            pass
    return str(node)


def _ast_enum_name(value):
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if name:
        return str(name)
    rendered = str(value)
    if "." in rendered:
        rendered = rendered.split(".")[-1]
    return rendered


def _duration_to_promql(delta: timedelta | None):
    if delta is None:
        return ""
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "0s"

    parts = []
    remaining = total_seconds
    for unit_seconds, suffix in (
        (3600, "h"),
        (60, "m"),
        (1, "s"),
    ):
        count, remaining = divmod(remaining, unit_seconds)
        if count:
            parts.append(f"{count}{suffix}")
    return "".join(parts) or "0s"


def _new_fragment(expr, family="unknown", backend="ast"):
    return PromQLFragment(raw_expr=expr, family=family, extra={"parser_backend": backend})


def _strip_le_bucket_suffix(metric):
    """Return the base histogram metric name for a Prometheus ``_bucket`` series.

    ``http_request_duration_seconds_bucket`` -> ``http_request_duration_seconds``.
    Metrics without the suffix are returned unchanged.
    """
    if metric and metric.endswith("_bucket"):
        return metric[: -len("_bucket")]
    return metric


def _append_not_feasible_reason(frag, reason):
    if not reason:
        return
    reasons = frag.extra.setdefault("not_feasible_reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _merge_not_feasible_reasons(target, *children):
    for child in children:
        if not child:
            continue
        for reason in child.extra.get("not_feasible_reasons", []):
            _append_not_feasible_reason(target, reason)


def _copy_fragment_summary(target, source):
    if not source:
        return target

    if not target.metric and source.metric:
        target.metric = source.metric
    if not target.matchers and source.matchers:
        target.matchers = list(source.matchers)
    if not target.range_func and source.range_func:
        target.range_func = source.range_func
    if not target.range_window and source.range_window:
        target.range_window = source.range_window
    if not target.outer_agg and source.outer_agg:
        target.outer_agg = source.outer_agg
    if not target.group_labels and source.group_labels:
        target.group_labels = list(source.group_labels)
    if target.group_mode == "by" and source.group_mode != "by":
        target.group_mode = source.group_mode
    if not target.binary_op and source.binary_op:
        target.binary_op = source.binary_op
    if target.binary_rhs is None and source.binary_rhs is not None:
        target.binary_rhs = source.binary_rhs
    if not target.is_scalar and source.is_scalar:
        target.is_scalar = True
        target.scalar_value = source.scalar_value
    if not target.is_time_call and source.is_time_call:
        target.is_time_call = True

    for key in (
        "call_name",
        "inner_agg",
        "inner_group",
        "join_labels",
        "offset",
        # A stripped ``X or vector(N)`` fallback tags its surviving operand so
        # the translator can warn about the dropped zero-fill. That survivor is
        # frequently wrapped in an aggregation (``sum(X or vector(0))``), which
        # rebuilds the fragment via this copy; carry the flag through so the
        # warning is not lost on the wrapped shape (issue #252 review).
        "or_vector_fallback",
        "post_filter",
        "quantile_phi",
        "start_matchers",
        "start_metric",
        "vector_matching",
        "wrapped_scalar",
    ):
        if source.extra.get(key) is not None and key not in target.extra:
            target.extra[key] = source.extra[key]

    _merge_not_feasible_reasons(target, source)
    return target


def _iter_fragment_children(frag):
    if not frag:
        return []
    children = []
    child = frag.extra.get("inner_frag")
    if isinstance(child, PromQLFragment):
        children.append(child)
    for key in ("left_frag", "right_frag"):
        child = frag.extra.get(key)
        if isinstance(child, PromQLFragment):
            children.append(child)
    if isinstance(frag.binary_rhs, PromQLFragment):
        children.append(frag.binary_rhs)
    return children


def _iter_pending_join_rhs_fragments(frag, _seen=None):
    """Recursively yield every fragment carrying a ``pending_join_rhs_metric`` marker.

    The safe-subset aggregated group_left join rewrite (issue #197) can end up
    nested inside a larger expression — e.g. a ratio of two aggregated joins
    parses as a top-level ``binary_expr`` whose operands are the reclassified
    join fragments. A classifier that only inspects the top-level fragment
    would miss the marker entirely, so walk the whole fragment tree.
    """
    if frag is None:
        return
    seen = _seen if _seen is not None else set()
    if id(frag) in seen:
        return
    seen.add(id(frag))
    if frag.extra.get("pending_join_rhs_metric") is not None:
        yield frag
    for child in _iter_fragment_children(frag):
        yield from _iter_pending_join_rhs_fragments(child, seen)


def _find_summary_fragment(frag):
    if not frag:
        return None
    if frag.metric or frag.range_func or frag.extra.get("call_name"):
        return frag
    for child in _iter_fragment_children(frag):
        summary = _find_summary_fragment(child)
        if summary:
            return summary
    return None


def _ast_matchers(matchers_obj):
    parsed = []
    for matcher in list(getattr(matchers_obj, "matchers", []) or []):
        op_name = _ast_enum_name(getattr(matcher, "op", None))
        op = {
            "Equal": "=",
            "NotEqual": "!=",
            "Re": "=~",
            "NotRe": "!~",
        }.get(op_name)
        if not op:
            continue
        parsed.append(
            {
                "label": str(getattr(matcher, "name", "") or ""),
                "op": op,
                "value": str(getattr(matcher, "value", "") or ""),
            }
        )
    return parsed


def _ast_vector_selector_fragment(node, expr):
    frag = _new_fragment(expr, family="simple_metric")
    frag.metric = str(getattr(node, "name", "") or "")
    frag.matchers = _ast_matchers(getattr(node, "matchers", None))

    if any(m["label"] == "__name__" for m in frag.matchers):
        _append_not_feasible_reason(frag, HARD_UNSUPPORTED_AST_REASONS["__name__"])

    offset = getattr(node, "offset", None)
    if offset:
        frag.extra["offset"] = _duration_to_promql(offset)
        _append_not_feasible_reason(frag, HARD_UNSUPPORTED_AST_REASONS["offset"])

    if getattr(node, "at", None):
        _append_not_feasible_reason(frag, "PromQL @ modifiers require manual redesign")

    if not frag.metric:
        frag.family = "unknown"
    if frag.extra.get("not_feasible_reasons"):
        frag.family = "unknown"
    return frag


def _ast_matrix_selector_fragment(node, expr):
    frag = _new_fragment(expr)
    selector_frag = _ast_from_node(node.vector_selector, _ast_node_expr(node.vector_selector))
    _copy_fragment_summary(frag, selector_frag)
    frag.range_window = _duration_to_promql(getattr(node, "range", None))
    return frag


def _ast_call_fragment(node, expr):
    func_name = str(getattr(getattr(node, "func", None), "name", "") or "").lower()
    args = list(getattr(node, "args", []) or [])

    if func_name == "time" and not args:
        frag = _new_fragment(expr, family="scalar")
        frag.is_time_call = True
        return frag

    child_frags = [_ast_from_node(arg, _ast_node_expr(arg)) for arg in args]

    if func_name == "scalar" and len(child_frags) == 1:
        child = child_frags[0]
        if child.metric and not child.extra.get("not_feasible_reasons") and child.family in {
            "binary_expr",
            "nested_agg",
            "range_agg",
            "scaled_agg",
            "simple_agg",
            "simple_metric",
            "uptime",
        }:
            wrapped = _copy_fragment_summary(_new_fragment(expr, family=child.family), child)
            wrapped.binary_rhs = child.binary_rhs
            for key in ("left_frag", "right_frag"):
                if key in child.extra:
                    wrapped.extra[key] = child.extra[key]
            wrapped.extra["wrapped_scalar"] = True
            return wrapped

    if func_name == "topk" and len(child_frags) == 2:
        limit_frag, value_frag = child_frags
        if (
            limit_frag.is_scalar
            and limit_frag.scalar_value is not None
            and value_frag.metric
            and not value_frag.extra.get("not_feasible_reasons")
        ):
            frag = _copy_fragment_summary(_new_fragment(expr, family="topk"), value_frag)
            frag.extra["topk_limit"] = int(limit_frag.scalar_value)
            frag.extra["topk_value_expr"] = value_frag.raw_expr
            return frag

    if func_name in SUPPORTED_RANGE_FUNCTIONS and len(args) == 1 and type(args[0]).__name__ == "MatrixSelector":
        matrix_frag = child_frags[0]
        if matrix_frag.metric and not matrix_frag.extra.get("not_feasible_reasons"):
            frag = _copy_fragment_summary(_new_fragment(expr, family="range_agg"), matrix_frag)
            frag.range_func = func_name
            return frag

    # sort() / sort_desc() — strip outer wrapper, flag for value-sort postprocessor
    if func_name in {"sort", "sort_desc"} and len(child_frags) == 1:
        inner = child_frags[0]
        if not inner.extra.get("not_feasible_reasons"):
            result = _copy_fragment_summary(_new_fragment(expr, family=inner.family), inner)
            for k, v in inner.extra.items():
                result.extra.setdefault(k, v)
            result.extra["value_sort_desc"] = (func_name == "sort_desc")
            return result

    # round() — strip outer wrapper, carry precision for ROUND() postprocessor
    if func_name == "round" and 1 <= len(child_frags) <= 2:
        inner = child_frags[0]
        if not inner.extra.get("not_feasible_reasons"):
            precision = (
                child_frags[1].scalar_value
                if len(child_frags) == 2 and child_frags[1].is_scalar
                else None
            )
            result = _copy_fragment_summary(_new_fragment(expr, family=inner.family), inner)
            for k, v in inner.extra.items():
                result.extra.setdefault(k, v)
            result.extra["has_round"] = True
            result.extra["round_precision"] = precision
            return result

    # clamp_min() — strip outer wrapper, carry threshold for GREATEST() postprocessor
    if func_name == "clamp_min" and len(child_frags) == 2:
        inner, threshold_frag = child_frags
        if (
            not inner.extra.get("not_feasible_reasons")
            and threshold_frag.is_scalar
            and threshold_frag.scalar_value is not None
        ):
            result = _copy_fragment_summary(_new_fragment(expr, family=inner.family), inner)
            for k, v in inner.extra.items():
                result.extra.setdefault(k, v)
            result.extra["clamp_min_value"] = threshold_frag.scalar_value
            return result

    # clamp_max() — strip outer wrapper, carry threshold for LEAST() postprocessor
    if func_name == "clamp_max" and len(child_frags) == 2:
        inner, threshold_frag = child_frags
        if (
            not inner.extra.get("not_feasible_reasons")
            and threshold_frag.is_scalar
            and threshold_frag.scalar_value is not None
        ):
            result = _copy_fragment_summary(_new_fragment(expr, family=inner.family), inner)
            for k, v in inner.extra.items():
                result.extra.setdefault(k, v)
            result.extra["clamp_max_value"] = threshold_frag.scalar_value
            return result

    # clamp(v, lo, hi) — equals GREATEST(LEAST(v, hi), lo); carry both bounds and
    # reuse the clamp_min (GREATEST) + clamp_max (LEAST) postprocessors.
    if func_name == "clamp" and len(child_frags) == 3:
        inner, lo_frag, hi_frag = child_frags
        if (
            not inner.extra.get("not_feasible_reasons")
            and lo_frag.is_scalar
            and lo_frag.scalar_value is not None
            and hi_frag.is_scalar
            and hi_frag.scalar_value is not None
        ):
            result = _copy_fragment_summary(_new_fragment(expr, family=inner.family), inner)
            for k, v in inner.extra.items():
                result.extra.setdefault(k, v)
            result.extra["clamp_min_value"] = lo_frag.scalar_value
            result.extra["clamp_max_value"] = hi_frag.scalar_value
            return result

    # sgn() — strip outer wrapper, carry flag for SIGNUM() postprocessor
    if func_name == "sgn" and len(child_frags) == 1:
        inner = child_frags[0]
        if not inner.extra.get("not_feasible_reasons"):
            result = _copy_fragment_summary(_new_fragment(expr, family=inner.family), inner)
            for k, v in inner.extra.items():
                result.extra.setdefault(k, v)
            result.extra["has_sgn"] = True
            return result

    # Elementwise math/trig wrappers (abs, ceil, sqrt, ln, sin, deg, ...) — strip
    # the outer call and carry the function name for an exact EVAL postprocessor.
    # Nested wrappers accumulate in evaluation order (innermost first) so that
    # e.g. sqrt(abs(x)) emits ABS then SQRT.
    if func_name in ELEMENTWISE_MATH_FUNCTIONS and len(child_frags) == 1:
        inner = child_frags[0]
        if not inner.extra.get("not_feasible_reasons"):
            result = _copy_fragment_summary(_new_fragment(expr, family=inner.family), inner)
            for k, v in inner.extra.items():
                result.extra.setdefault(k, v)
            existing = list(result.extra.get("math_fns", []))
            existing.append(func_name)
            result.extra["math_fns"] = existing
            return result

    # label_replace(v, dst, replacement, src, regex) — new fragment family
    if func_name == "label_replace" and len(child_frags) == 5:
        value_frag = child_frags[0]
        string_args = [f.extra.get("string_value") for f in child_frags[1:]]
        # A bare ``vector(N)`` value is itself "not feasible" standalone, but
        # that is exactly the shape of the ``or`` zero-fill idiom Grafana
        # dashboards use to label a fallback value (e.g.
        # ``X or on() label_replace(vector(0), "status", "0", "", "")``).
        # Let it through here so ``_is_vector_fallback_operand`` can still
        # recognize and strip it in ``_strip_or_vector_fallback`` below.
        value_ok = not value_frag.extra.get(
            "not_feasible_reasons"
        ) or _is_vector_fallback_operand(value_frag)
        if all(s is not None for s in string_args) and value_ok:
            dst, replacement, src, regex = string_args
            result = _copy_fragment_summary(
                _new_fragment(expr, family="label_replace"), value_frag
            )
            result.extra["lr_dst"] = dst
            result.extra["lr_replacement"] = replacement
            result.extra["lr_src"] = src
            result.extra["lr_regex"] = regex
            result.extra["lr_inner_frag"] = value_frag
            return result

    # histogram_quantile(phi, <bucket series>) — translate to an ES|QL
    # PERCENTILE() over the base histogram metric. The bucket-series operand is
    # the Prometheus idiom for reconstructing a distribution (rate()/sum by
    # (le)); an Elasticsearch histogram-typed field already encodes it, so the
    # le dimension and any rate()/sum wrapper are consumed by PERCENTILE.
    if func_name == "histogram_quantile" and len(child_frags) == 2:
        phi_frag, value_frag = child_frags
        if (
            phi_frag.is_scalar
            and phi_frag.scalar_value is not None
            and value_frag.metric
            and not value_frag.extra.get("not_feasible_reasons")
        ):
            frag = _copy_fragment_summary(
                _new_fragment(expr, family="histogram_quantile"), value_frag
            )
            frag.family = "histogram_quantile"
            frag.extra["bucket_metric"] = value_frag.metric
            # The bucket-series aggregation must be ``sum`` (or absent) for the
            # PERCENTILE-over-histogram mapping to be faithful; record it so the
            # translator can degrade a non-sum aggregation instead of silently
            # discarding it.
            frag.extra["bucket_agg"] = value_frag.outer_agg
            # Whether the source aggregation kept the ``le`` bucket-boundary
            # label. A classic ``_bucket`` series needs it (e.g. ``sum by (le)``)
            # for the percentile to be meaningful; the translator degrades when
            # it's missing.
            frag.extra["had_le_grouping"] = "le" in value_frag.group_labels
            frag.metric = _strip_le_bucket_suffix(value_frag.metric)
            frag.group_labels = [g for g in value_frag.group_labels if g != "le"]
            frag.range_func = ""
            frag.outer_agg = ""
            frag.extra["quantile_phi"] = float(phi_frag.scalar_value)
            return frag

    frag = _new_fragment(expr)
    for child in child_frags:
        _copy_fragment_summary(frag, child)
    if func_name:
        frag.extra["call_name"] = func_name

    if func_name in HARD_UNSUPPORTED_CALL_REASONS:
        _append_not_feasible_reason(frag, HARD_UNSUPPORTED_CALL_REASONS[func_name])
    elif func_name:
        _append_not_feasible_reason(frag, f"{func_name}() requires manual redesign")
    if func_name == "time" and args:
        _append_not_feasible_reason(frag, "PromQL time() call shape requires manual redesign")
    return frag


def _contains_join_frag(frag, _depth=0):
    """Return True if *frag* or any binary_expr descendant is a join fragment."""
    if frag is None or _depth > 8:
        return False
    if frag.family == "join":
        return True
    if frag.family == "binary_expr":
        return _contains_join_frag(frag.extra.get("left_frag"), _depth + 1) or _contains_join_frag(
            frag.extra.get("right_frag"), _depth + 1
        )
    return False


def _histogram_summary_base(metric: str) -> str | None:
    """Return the base name for a Prometheus histogram ``_sum`` / ``_count`` metric."""
    name = str(metric or "").strip()
    if name.endswith("_sum"):
        return name[: -len("_sum")]
    if name.endswith("_count"):
        return name[: -len("_count")]
    return None


def _is_histogram_summary_ratio_pair(left_frag, right_frag) -> bool:
    """True for ``increase|rate|irate(m_sum) / increase|rate|irate(m_count)``.

    That shape is the Prometheus histogram *mean* idiom (average of per-series
    ``sum/count``). ``sum(A/B)`` is not equal to ``sum(A)/sum(B)``, but the
    ratio-of-aggregates form is the ES|QL-expressible approximation used for
    panels like Prometheus Compaction duration.
    """
    if left_frag is None or right_frag is None:
        return False
    if left_frag.extra.get("not_feasible_reasons") or right_frag.extra.get("not_feasible_reasons"):
        return False
    left_metric = str(left_frag.metric or "")
    right_metric = str(right_frag.metric or "")
    if not left_metric.endswith("_sum") or not right_metric.endswith("_count"):
        return False
    left_base = _histogram_summary_base(left_metric)
    right_base = _histogram_summary_base(right_metric)
    if not left_base or left_base != right_base:
        return False
    left_range = str(left_frag.range_func or "").lower()
    right_range = str(right_frag.range_func or "").lower()
    if left_range not in {"increase", "rate", "irate"}:
        return False
    if left_range != right_range:
        return False
    # Both operands should already be range wrappers without their own outer agg
    # (the outer sum is what we're about to push down).
    if left_frag.outer_agg or right_frag.outer_agg:
        return False
    return True


_APPROX_AGG_OVER_SUMMARY_RATIO_WARNING = (
    "Approximated sum(increase|rate(m_sum)/increase|rate(m_count)) as a ratio of "
    "aggregates (sum(m_sum)/sum(m_count)); per-series means are not weighted the "
    "same as Prometheus"
)


def _push_outer_agg(frag, outer_agg, group_labels, group_mode):
    """Push an outer aggregation down to a leaf fragment.

    Used to apply linearity when rewriting ``sum(A ± B)`` as
    ``sum(A) ± sum(B)``.  Also handles two deeper cases:

    * ``family="join"`` — strip the label-enrichment RHS and push the agg
      to the primary (LHS) metric, mirroring what ``join_family_rule`` does.
      This enables ``agg(join_result / k)`` → ``agg(primary) / k``.
    * ``family="binary_expr"`` with a scalar operand — recurse through nested
      scalar divisions/multiplications so ``agg(join / k1 / k2)`` resolves to
      ``agg(primary) / k1 / k2``.

    Returns ``None`` when the fragment cannot accept a pushed aggregation.
    """
    new_family = frag.family
    if frag.family == "simple_metric":
        new_family = "simple_agg"
    elif frag.family in {"range_agg", "simple_agg"}:
        pass
    elif frag.family == "join" and frag.binary_op == "*":
        # Multiplication across a vector-matching join is not linear under an
        # outer aggregation. Stripping the RHS keeps the query syntactically
        # migratable but changes the numeric value, so refuse this path.
        return None
    elif frag.family == "binary_expr" and frag.binary_op in {"/", "*"}:
        # Recursive scalar hoisting through nested binary_expr layers.
        # Handles e.g. agg(join_result / 1024 / 1024).
        inner_left = frag.extra.get("left_frag")
        inner_right = frag.extra.get("right_frag")
        scalar_side = None
        vector_side = None
        if inner_right is not None and inner_right.is_scalar and inner_right.scalar_value is not None:
            scalar_side = inner_right
            vector_side = inner_left
        elif frag.binary_op == "*" and inner_left is not None and inner_left.is_scalar and inner_left.scalar_value is not None:
            scalar_side = inner_left
            vector_side = inner_right
        if scalar_side is None or vector_side is None or vector_side.extra.get("not_feasible_reasons"):
            return None
        pushed = _push_outer_agg(vector_side, outer_agg, group_labels, group_mode)
        if pushed is None:
            return None
        if frag.binary_op == "/" and scalar_side is inner_left:
            return _make_binary_fragment(frag.raw_expr, scalar_side, "/", pushed)
        return _make_binary_fragment(frag.raw_expr, pushed, frag.binary_op, scalar_side)
    else:
        return None
    return dataclasses.replace(
        frag,
        family=new_family,
        outer_agg=outer_agg,
        group_labels=list(group_labels),
        group_mode=group_mode,
        extra=dict(frag.extra),
    )


def _join_not_eligible_reason(cardinality, binary_op):
    """Explain why an aggregated vector-matching join can't use the safe-subset rewrite.

    Only a ``group_left(...)`` (``ManyToOne``) multiplication join is eligible
    (issue #197) — ``group_right`` and non-``*`` operators keep the pre-existing
    conservative behavior, but with a message naming which condition failed
    instead of one generic reason.
    """
    if binary_op != "*":
        detail = f"a '{binary_op}' vector-matching join"
    elif cardinality == "OneToMany":
        detail = "a group_right(...) vector-matching join"
    else:
        detail = "an unrecognized vector-matching join"
    return (
        "Aggregating over a PromQL vector-matching join requires manual redesign; "
        "only a group_left(...) label-enrichment multiplication join can be safely "
        f"approximated today, and this is {detail}"
    )


def _join_by_clause_enrichment_reason(overlap_labels, enrichment_labels, rhs_metric, primary_metric):
    """Explain why an outer by()/without() can't be satisfied after stripping the join RHS.

    The overlapping label(s) only exist on the join's RHS (the ``group_left(...)``
    include list) — dropping the RHS to keep the primary metric's value would
    leave nothing to group by for them.
    """
    labels_text = ", ".join(overlap_labels)
    return (
        f"Aggregating by '{labels_text}' over a PromQL vector-matching join requires "
        f"manual redesign: '{labels_text}' only exists via "
        f"`group_left({', '.join(enrichment_labels)}) {rhs_metric}`, not on the primary "
        f"metric '{primary_metric}'; rebuild this panel with a manual ES|QL lookup/enrich, "
        "or drop that grouping dimension"
    )


def _render_label_matchers(matchers):
    """Render label matchers as ``label op 'value'`` text for a warning message."""
    return ", ".join(f"{m['label']}{m['op']}'{m['value']}'" for m in matchers or [])


def _join_unverifiable_group_reason(labels, primary_metric):
    """Explain why a by()/without() label can't be verified after stripping the RHS.

    The ``group_left(...)`` include list couldn't be recovered (a bare
    ``group_left()`` or an ambiguous nested modifier leaves the enrichment label
    set empty), so a grouping label that isn't an ``on(...)`` match key can't be
    proven to exist on the primary metric. Fail closed rather than emit a
    ``STATS ... BY`` over a possibly-absent column (issue #197 review finding 3).
    """
    labels_text = ", ".join(labels)
    return (
        f"Aggregating by '{labels_text}' over a PromQL vector-matching join requires "
        f"manual redesign: '{labels_text}' is not an on(...) match key and the "
        "group_left(...) enrichment labels could not be determined, so it can't be "
        f"proven to exist on the primary metric '{primary_metric}' once the join is "
        "dropped; rebuild this panel with a manual ES|QL lookup/enrich, or drop that "
        "grouping dimension"
    )


def _join_rhs_not_plain_selector_reason(right_frag):
    """Explain why a non-selector join partner can't use the safe-subset rewrite.

    The ``_info`` label-enrichment idiom always joins against a *plain vector
    selector* (``... group_left(x) foo_info{...}``). A range/aggregate/function
    wrapper — ``rate(foo_info[5m])``, ``sum(foo_info) by(...)``,
    ``count(foo_info)`` — is not a constant-``1`` multiplier (its value can be a
    rate, a sum, a count, or ``0``), so it must not be dropped even though its
    summary metric name ends in ``_info`` (issue #197 review). We only look at
    ``right_frag.metric`` (the summary), which alone can't distinguish these
    shapes, so gate on the fragment family here.
    """
    shape = "a compound expression"
    if right_frag is not None:
        if right_frag.range_func:
            shape = f"a {right_frag.range_func}(...) range expression"
        elif right_frag.outer_agg:
            shape = f"a {right_frag.outer_agg}(...) aggregate"
        elif right_frag.extra.get("call_name"):
            shape = f"a {right_frag.extra['call_name']}(...) call"
        elif right_frag.family == "binary_expr":
            shape = "a binary expression"
    return (
        "Aggregating over a PromQL vector-matching join requires manual redesign: the "
        f"group_left(...) partner is {shape}, not a plain `<metric>_info{{...}}` vector "
        "selector, so it is not a constant-1 label-only metric and dropping it would "
        "change the numeric value"
    )


def _ast_aggregate_fragment(node, expr):
    child = _ast_from_node(node.expr, _ast_node_expr(node.expr))
    frag = _copy_fragment_summary(_new_fragment(expr), child)
    frag.extra["inner_frag"] = child
    frag.outer_agg = str(getattr(node, "op", "") or "").lower()

    if frag.outer_agg == "topk" and not child.extra.get("not_feasible_reasons"):
        topk_source = child if child.metric else _find_summary_fragment(child)
        if not topk_source or not topk_source.metric:
            return frag
        topk_frag = _copy_fragment_summary(_new_fragment(expr, family="topk"), topk_source)
        if child.outer_agg:
            topk_frag.outer_agg = child.outer_agg
        if child.group_labels:
            topk_frag.group_labels = list(child.group_labels)
            topk_frag.group_mode = child.group_mode
        try:
            param = getattr(node, "param", None)
            topk_frag.extra["topk_limit"] = int(float(getattr(param, "val", param) or 10))
        except (TypeError, ValueError):
            topk_frag.extra["topk_limit"] = 10
        topk_frag.extra["topk_value_expr"] = child.raw_expr
        return topk_frag

    # quantile(phi, expr) by (..) == ES|QL PERCENTILE(expr, phi*100). Capture the
    # phi parameter; only the simple aggregation form over a metric is feasible.
    if frag.outer_agg == "quantile":
        param = getattr(node, "param", None)
        raw_phi = getattr(param, "val", param)
        try:
            phi = float(raw_phi) if raw_phi is not None else None
        except (TypeError, ValueError):
            phi = None
        if phi is None or not (0.0 <= phi <= 1.0):
            _append_not_feasible_reason(
                frag, "quantile() requires a constant phi in [0, 1]; got a non-literal argument"
            )
        else:
            frag.extra["quantile_phi"] = phi

    if frag.outer_agg in HARD_UNSUPPORTED_CALL_REASONS:
        _append_not_feasible_reason(frag, HARD_UNSUPPORTED_CALL_REASONS[frag.outer_agg])

    modifier = getattr(node, "modifier", None)
    outer_group_labels = []
    outer_group_mode = "by"
    if modifier:
        outer_group_labels = list(getattr(modifier, "labels", []) or [])
        modifier_type = _ast_enum_name(getattr(modifier, "type", None))
        outer_group_mode = "without" if modifier_type == "Without" else "by"
        if outer_group_mode == "without":
            _append_not_feasible_reason(frag, HARD_UNSUPPORTED_AST_REASONS["without"])
    frag.group_labels = outer_group_labels
    frag.group_mode = outer_group_mode

    if frag.extra.get("not_feasible_reasons"):
        return frag

    if child.family == "range_agg" and child.metric and not child.outer_agg:
        frag.family = "range_agg"
        return frag

    if child.family == "uptime" and child.metric:
        frag.family = "uptime"
        return frag

    if child.family == "simple_metric" and child.metric:
        frag.family = "simple_agg"
        return frag

    if child.family == "simple_agg" and child.metric and child.outer_agg:
        frag.family = "nested_agg"
        frag.extra["inner_agg"] = child.outer_agg
        frag.extra["inner_group"] = list(child.group_labels)
        return frag

    if child.family == "range_agg" and child.metric and child.outer_agg:
        frag.family = "nested_agg"
        frag.extra["inner_agg"] = child.outer_agg
        frag.extra["inner_group"] = list(child.group_labels)
        return frag

    if child.family == "join":
        matching = child.extra.get("vector_matching") or {}
        cardinality = matching.get("cardinality")
        left_frag = child.extra.get("left_frag")
        right_frag = child.extra.get("right_frag")

        # Only a group_left(...) (ManyToOne) multiplication join is eligible for
        # the safe-subset rewrite; group_right and other operators keep the
        # conservative not_feasible behavior (issue #197 scope decision).
        if cardinality != "ManyToOne" or child.binary_op != "*" or left_frag is None:
            _append_not_feasible_reason(frag, _join_not_eligible_reason(cardinality, child.binary_op))
            return frag

        # The label-enrichment idiom joins against a *plain vector selector* for
        # the _info metric. A range/aggregate/function wrapper over an _info
        # metric (rate(foo_info[5m]), sum(foo_info) by(...), count(foo_info)) is
        # not a constant-1 multiplier, so it must not be stripped even though its
        # summary metric ends in _info — the later suffix check only inspects the
        # RHS metric name and can't tell these shapes apart (issue #197 review).
        if right_frag is None or right_frag.family != "simple_metric" or not right_frag.metric:
            _append_not_feasible_reason(frag, _join_rhs_not_plain_selector_reason(right_frag))
            return frag

        # An explicit by()/without() can only be honoured after dropping the RHS
        # when every grouping label still exists on the primary metric.
        enrichment_labels = list(child.extra.get("enrichment_labels", []) or [])
        on_keys = set(matching.get("labels") or []) if matching.get("type") == "Include" else set()

        # (a) A label carried only by the group_left(...) include list has nothing
        # to group by once the RHS is dropped.
        overlap = [label for label in frag.group_labels if label in enrichment_labels]
        if overlap:
            rhs_metric = right_frag.metric if right_frag else ""
            _append_not_feasible_reason(
                frag,
                _join_by_clause_enrichment_reason(overlap, enrichment_labels, rhs_metric, left_frag.metric),
            )
            return frag

        # (b) When the include list couldn't be recovered (a bare ``group_left()``
        # or an ambiguous nested modifier leaves ``enrichment_labels`` empty), any
        # grouping label that isn't an on(...) match key can't be proven to exist
        # on the primary metric. Fail closed rather than emit a STATS BY over a
        # possibly-absent column (issue #197 review finding 3).
        if not enrichment_labels:
            unverifiable = [label for label in frag.group_labels if label not in on_keys]
            if unverifiable:
                _append_not_feasible_reason(
                    frag, _join_unverifiable_group_reason(unverifiable, left_frag.metric)
                )
                return frag

        # Structurally safe to strip the RHS and aggregate the primary metric
        # alone — re-home the aggregation onto the join's left (primary)
        # operand. ``frag.metric``/``matchers``/``range_func`` already carry
        # left_frag's values (copied transitively via ``_copy_fragment_summary``
        # at the top of this function), so only the family needs updating.
        # Whether the RHS is actually a provable info-metric (not just any
        # group_left partner) can't be checked here — rule_pack isn't available
        # during parsing — so defer that to join_label_enrichment_check_rule at
        # translation time, and stash the RHS metric name for it to check.
        if left_frag.family == "range_agg" and left_frag.metric and not left_frag.outer_agg:
            frag.family = "range_agg"
        elif left_frag.family == "simple_metric" and left_frag.metric:
            frag.family = "simple_agg"
        else:
            # The join's primary operand is itself a nested expression (a
            # chained/multi-hop join, or another aggregate) — not safely
            # approximated today.
            _append_not_feasible_reason(
                frag,
                "Aggregating over a PromQL vector-matching join requires manual redesign; "
                "the join's primary operand is itself a nested expression (chained/multi-hop "
                "join), which isn't safely approximated today",
            )
            return frag
        frag.extra["pending_join_rhs_metric"] = right_frag.metric if right_frag else ""
        # Label matchers on the join partner (e.g. ``info{cluster="prod"}``) are
        # dropped with the RHS. Where the label doesn't also exist on the primary
        # metric that can broaden the aggregation to series the filter excluded —
        # a numeric change in multi-value deployments. Per the design's accepted
        # approximation we keep the panel feasible but stash the dropped filter
        # text so join_label_enrichment_check_rule surfaces it in the warning
        # rather than dropping it silently (issue #197 review finding 1).
        if right_frag is not None and right_frag.matchers:
            frag.extra["pending_join_rhs_filters"] = _render_label_matchers(right_frag.matchers)
        # A by()/without() label that is neither an on(...) match key nor a
        # group_left(...) enrichment label is assumed to exist on the primary
        # metric. That assumption can't be checked at parse time (no rule pack /
        # resolver), so record the labels for join_label_enrichment_check_rule to
        # verify against a live schema when one is available (issue #197 review).
        assumed_group_labels = [
            label
            for label in frag.group_labels
            if label not in on_keys and label not in enrichment_labels
        ]
        if assumed_group_labels:
            frag.extra["pending_join_verify_labels"] = assumed_group_labels
        return frag

    # Handle aggregation over a binary expression between two time-series.
    # SUM is linear so sum(A ± B) = sum(A) ± sum(B); push the aggregation
    # down to each operand and return a binary_expr the pipeline can handle.
    # Division and multiplication are not linear: sum(A/B) ≠ sum(A)/sum(B),
    # so those patterns are marked not_feasible rather than silently dropped.
    if child.family == "binary_expr":
        inner_left = child.extra.get("left_frag")
        inner_right = child.extra.get("right_frag")
        if (
            child.binary_op in {"+", "-"}
            and frag.outer_agg == "sum"
            and inner_left
            and inner_right
            and not inner_left.extra.get("not_feasible_reasons")
            and not inner_right.extra.get("not_feasible_reasons")
        ):
            new_left = _push_outer_agg(inner_left, "sum", frag.group_labels, frag.group_mode)
            new_right = _push_outer_agg(inner_right, "sum", frag.group_labels, frag.group_mode)
            if new_left and new_right:
                new_binary = _make_binary_fragment(expr, new_left, child.binary_op, new_right)
                new_binary.group_labels = list(frag.group_labels)
                new_binary.group_mode = frag.group_mode
                return new_binary
        elif child.binary_op in {"/", "*"}:
            # Constant scaling: agg(X op k) = agg(X) op k.  When one operand
            # is a scalar literal the aggregation distributes over it, so hoist
            # the scalar out and push the aggregation down to the vector side.
            # This covers patterns like max(rate(A[5m]) * 8) or avg(up * 100).
            scalar_side = None
            vector_side = None
            if inner_right is not None and inner_right.is_scalar and inner_right.scalar_value is not None:
                scalar_side = inner_right
                vector_side = inner_left
            elif inner_left is not None and inner_left.is_scalar and inner_left.scalar_value is not None:
                scalar_side = inner_left
                vector_side = inner_right
            if (
                scalar_side is not None
                and vector_side is not None
                and not vector_side.extra.get("not_feasible_reasons")
            ):
                pushed = _push_outer_agg(vector_side, frag.outer_agg, frag.group_labels, frag.group_mode)
                if pushed is not None:
                    # Preserve order for non-commutative division (k / agg(X)).
                    if child.binary_op == "/" and scalar_side is inner_left:
                        new_binary = _make_binary_fragment(expr, scalar_side, "/", pushed)
                    else:
                        new_binary = _make_binary_fragment(expr, pushed, child.binary_op, scalar_side)
                    new_binary.group_labels = list(frag.group_labels)
                    new_binary.group_mode = frag.group_mode
                    if _contains_join_frag(vector_side):
                        new_binary.extra["stripped_join"] = True
                    return new_binary
                if _contains_join_frag(vector_side):
                    _append_not_feasible_reason(
                        frag,
                        "Aggregating over a PromQL vector-matching join with scalar arithmetic requires manual redesign; "
                        "dropping the joined metric would change numeric values",
                    )
                    return frag
            # Histogram summary pair: sum(increase(m_sum)/increase(m_count)).
            # Exact per-element mean cannot be preserved; rewrite as the ratio of
            # aggregates so Compaction-duration style panels still migrate.
            if (
                child.binary_op == "/"
                and frag.outer_agg == "sum"
                and _is_histogram_summary_ratio_pair(inner_left, inner_right)
            ):
                new_left = _push_outer_agg(inner_left, "sum", frag.group_labels, frag.group_mode)
                new_right = _push_outer_agg(inner_right, "sum", frag.group_labels, frag.group_mode)
                if new_left is not None and new_right is not None:
                    new_binary = _make_binary_fragment(expr, new_left, "/", new_right)
                    new_binary.group_labels = list(frag.group_labels)
                    new_binary.group_mode = frag.group_mode
                    new_binary.extra["approximated_agg_over_summary_ratio"] = True
                    return new_binary
            # Two true time-series operands — multiplication/division is not
            # linearisable: agg(A op B) ≠ agg(A) op agg(B).
            _append_not_feasible_reason(
                frag,
                f"Aggregating over a per-element {child.binary_op} between two time-series "
                f"({frag.outer_agg}(A {child.binary_op} B)) cannot be expressed accurately in ES|QL; "
                "rewrite as a ratio of aggregates if the series are label-aligned",
            )

    return frag


_GROUP_MODIFIER_LABELS_RE = re.compile(
    r"\bgroup_(?:left|right)\s*\(\s*([^)]*?)\s*\)", re.IGNORECASE
)
_GROUP_MODIFIER_KEYWORD_RE = re.compile(r"\bgroup_(?:left|right)\b", re.IGNORECASE)


def _extract_enrichment_labels(expr):
    """Recover the ``group_left(...)``/``group_right(...)`` enrichment labels.

    The ``promql-parser`` AST exposes the ``on``/``ignoring`` matching labels
    but drops the include-list carried by ``group_left``/``group_right``. Those
    labels are exactly what a label-enrichment join copies onto the result, so
    pull them back out of the raw expression text.

    Only the *current* binary node's modifier may be attributed here, but the
    raw text spans nested sub-joins too. To avoid borrowing a nested join's
    include list (e.g. attributing ``group_left(inner)`` to an outer
    ``group_left(outer)``), bail out when more than one group modifier is
    present — the caller is reached only for a node that itself carries a group
    modifier, so a single occurrence is unambiguously this node's. Returns
    ``[]`` for the ambiguous (nested) case and for a bare ``group_left`` with no
    parenthesised label list.
    """
    if not expr:
        return []
    if len(_GROUP_MODIFIER_KEYWORD_RE.findall(expr)) != 1:
        return []
    match = _GROUP_MODIFIER_LABELS_RE.search(expr)
    if not match:
        return []
    inner = match.group(1).strip()
    if not inner:
        return []
    return [label.strip() for label in inner.split(",") if label.strip()]


def _ast_binary_matching(modifier):
    matching = getattr(modifier, "matching", None)
    labels = list(getattr(matching, "labels", []) or []) if matching else []
    return {
        "cardinality": _ast_enum_name(getattr(modifier, "card", None)),
        "labels": labels,
        "type": _ast_enum_name(getattr(matching, "type", None)) if matching else "",
    }


def _ast_binary_fragment(node, expr):
    left = _ast_from_node(node.lhs, _ast_node_expr(node.lhs))
    right = _ast_from_node(node.rhs, _ast_node_expr(node.rhs))
    op = str(getattr(node, "op", "") or "")
    return_bool = bool(getattr(getattr(node, "modifier", None), "return_bool", False))

    # PromQL ``bool`` modifier (``A > bool B``) turns a comparison into a numeric
    # 0/1 indicator rather than a filter that drops series. Model it as a
    # ``binary_expr`` flagged ``bool_compare`` so the formula plan renders it as
    # ``CASE(<lhs> <op> <rhs>, 1, 0)``. This is distinct from a bare comparison
    # (no ``bool``), which keeps filter semantics via ``post_filter`` below.
    if return_bool and op in {">", "<", ">=", "<=", "==", "!="}:
        frag = _make_binary_fragment(expr, left, op, right)
        frag.extra["bool_compare"] = True
        return frag

    if op in {">", "<", ">=", "<=", "==", "!="} and right.is_scalar and right.scalar_value is not None:
        frag = _copy_fragment_summary(_new_fragment(expr, family=left.family), left)
        frag.extra["post_filter"] = {
            "op": op,
            "value": right.scalar_value,
        }
        # When left is a binary_expr (e.g. -(A+B) < 0), propagate left_frag/right_frag
        # so the formula plan can still decompose the expression.
        if left.family == "binary_expr":
            for key in ("left_frag", "right_frag"):
                if key in left.extra and key not in frag.extra:
                    frag.extra[key] = left.extra[key]
        return frag

    if left.is_time_call and op == "-" and right.family in {"join", "range_agg", "simple_agg", "simple_metric"}:
        frag = _new_fragment(expr, family="uptime")
        frag.is_time_call = True
        frag.binary_op = "-"
        frag.binary_rhs = right
        if right.family == "join" and isinstance(right.binary_rhs, PromQLFragment):
            frag.group_labels = list(right.group_labels)
            frag.extra["start_metric"] = right.binary_rhs.metric or ""
            frag.extra["start_matchers"] = list(right.binary_rhs.matchers or [])
        else:
            _copy_fragment_summary(frag, right)
        _merge_not_feasible_reasons(frag, left, right)
        return frag

    modifier = getattr(node, "modifier", None)

    # Set operators (``or``/``and``/``unless``) are not joins; they have
    # set-union/intersection/difference semantics that preserve operands'
    # label sets. Even though the parser models them with a ManyToMany
    # cardinality modifier, obs-migrate's join translation path is wrong
    # for them. Route them to the binary_expr family so the formula plan
    # builder can either apply the safe same-metric ``or`` rewrite or
    # refuse the translation honestly.
    if op.lower() in _SET_OPERATORS:
        if op.lower() == "or":
            survivor = _strip_or_vector_fallback(expr, left, right)
            if survivor is not None:
                survivor.extra.setdefault("parser_backend", "ast")
                return survivor
        frag = _make_binary_fragment(expr, left, op.lower(), right)
        frag.extra.setdefault("parser_backend", "ast")
        if modifier:
            frag.extra["vector_matching"] = _ast_binary_matching(modifier)
        return frag

    if modifier:
        matching = _ast_binary_matching(modifier)
        if matching["cardinality"] in {"ManyToOne", "OneToMany", "ManyToMany"}:
            frag = _copy_fragment_summary(_new_fragment(expr, family="join"), left)
            frag.binary_op = op
            frag.binary_rhs = right
            frag.group_labels = list(matching["labels"] or left.group_labels)
            frag.extra["join_labels"] = list(frag.group_labels)
            frag.extra["left_frag"] = left
            frag.extra["right_frag"] = right
            frag.extra["vector_matching"] = matching
            enrichment = _extract_enrichment_labels(expr)
            if enrichment:
                frag.extra["enrichment_labels"] = enrichment
            _merge_not_feasible_reasons(frag, left, right)
            return frag

    scalar_side = left if left.is_scalar else right if right.is_scalar else None
    agg_side = right if left.is_scalar else left if right.is_scalar else None
    if (
        op == "*"
        and scalar_side
        and agg_side
        and agg_side.family == "range_agg"
        and agg_side.outer_agg in {"avg", "sum", "max", "min"}
        and not agg_side.extra.get("not_feasible_reasons")
    ):
        frag = _copy_fragment_summary(_new_fragment(expr, family="scaled_agg"), agg_side)
        frag.binary_op = "*"
        frag.binary_rhs = PromQLFragment(
            scalar_value=scalar_side.scalar_value,
            is_scalar=True,
            extra={"parser_backend": scalar_side.extra.get("parser_backend", "ast")},
        )
        return frag

    frag = _make_binary_fragment(expr, left, op, right)
    frag.extra.setdefault("parser_backend", "ast")
    if modifier:
        frag.extra["vector_matching"] = _ast_binary_matching(modifier)
    return frag


def _ast_from_node(node, expr=None):
    expr = _trim_outer_parens(expr or _ast_node_expr(node))
    node_type = type(node).__name__

    if node_type == "ParenExpr":
        return _ast_from_node(node.expr, expr)

    if node_type == "UnaryExpr":
        child = _ast_from_node(node.expr, _ast_node_expr(node.expr))
        if child.is_scalar and child.scalar_value is not None:
            frag = _new_fragment(expr, family="scalar")
            frag.is_scalar = True
            frag.scalar_value = -child.scalar_value
            return frag
        # Rewrite -(vector_expr) as 0 - vector_expr so downstream formula
        # planning preserves the sign instead of copying the child unchanged.
        zero = _new_fragment("0", family="scalar")
        zero.is_scalar = True
        zero.scalar_value = 0.0
        return _make_binary_fragment(expr, zero, "-", child)

    if node_type == "NumberLiteral":
        frag = _new_fragment(expr, family="scalar")
        frag.is_scalar = True
        frag.scalar_value = float(getattr(node, "val", 0.0))
        return frag

    if node_type == "StringLiteral":
        frag = _new_fragment(expr, family="string_literal")
        frag.extra["string_value"] = str(getattr(node, "val", "") or "")
        return frag

    if node_type == "VectorSelector":
        return _ast_vector_selector_fragment(node, expr)

    if node_type == "MatrixSelector":
        return _ast_matrix_selector_fragment(node, expr)

    if node_type == "Call":
        return _ast_call_fragment(node, expr)

    if node_type == "AggregateExpr":
        return _ast_aggregate_fragment(node, expr)

    if node_type == "BinaryExpr":
        return _ast_binary_fragment(node, expr)

    if node_type == "SubqueryExpr":
        child = _ast_from_node(node.expr, _ast_node_expr(node.expr))
        frag = _copy_fragment_summary(_new_fragment(expr), child)
        _append_not_feasible_reason(frag, HARD_UNSUPPORTED_AST_REASONS["subquery"])
        frag.extra["subquery_range"] = _duration_to_promql(getattr(node, "range", None))
        if getattr(node, "step", None):
            frag.extra["subquery_step"] = _duration_to_promql(getattr(node, "step", None))
        return frag

    return _new_fragment(expr)


def _parse_logql_fragment(expr):
    frag = _new_fragment(expr, backend="regex")
    if re.match(r'^\s*\{[^}]*\}\s*(?:\|\s*(?:~|=)\s*"[^"]*")?\s*$', expr, re.DOTALL):
        frag.family = "logql_stream"
        matchers, _ = _parse_logql_selector(expr)
        frag.matchers = matchers
        return frag

    logql_count = re.match(
        r'^\s*(?P<outer>sum|count)\s*\(\s*count_over_time\s*\(\s*\{(?P<selectors>[^}]*)\}.*?\[(?P<window>[^\]]+)\]\s*\)\s*\)\s*$',
        expr,
        re.IGNORECASE | re.DOTALL,
    )
    if logql_count:
        frag.outer_agg = logql_count.group("outer").lower()
        frag.matchers = _parse_selector_matchers(logql_count.group("selectors"))
        frag.range_func = "count_over_time"
        frag.range_window = logql_count.group("window")
        frag.family = "logql_count"
        return frag
    return None


def _restore_sanitized_labels(frag, label_map):
    if not frag or not label_map:
        return frag
    for matcher in frag.matchers:
        label = matcher.get("label")
        if label in label_map:
            matcher["label"] = label_map[label]
    frag.group_labels = [label_map.get(label, label) for label in frag.group_labels]
    for key in ("inner_group", "join_labels"):
        labels = frag.extra.get(key)
        if isinstance(labels, list):
            frag.extra[key] = [label_map.get(label, label) for label in labels]
    matching = frag.extra.get("vector_matching")
    if isinstance(matching, dict) and isinstance(matching.get("labels"), list):
        matching["labels"] = [label_map.get(label, label) for label in matching["labels"]]
    for child in _iter_fragment_children(frag):
        _restore_sanitized_labels(child, label_map)
    return frag


def _is_vector_fallback_operand(frag):
    """Return True for a bare ``vector(N)`` call used as an ``or`` fallback,
    including one wrapped in ``label_replace(vector(N), ...)``.

    ``vector(N)`` has no series labels; in ``X or vector(N)`` it only fills the
    gaps where ``X`` has no data with the constant ``N``. It is not a metric in
    its own right, so when it is the fallback side of an ``or`` we can drop it
    (issue #66 Pattern A). Dashboards commonly wrap the fallback in
    ``label_replace(...)`` to stamp a label onto the synthetic zero row (e.g.
    ``X or on() label_replace(vector(0), "status", "0", "", "")``); that label
    only matters for the vector's own (dropped) series, so unwrap through it
    the same way (issue #252).
    """
    if frag is None:
        return False
    if frag.family == "label_replace":
        return _is_vector_fallback_operand(frag.extra.get("lr_inner_frag"))
    if frag.extra.get("call_name") != "vector":
        return False
    reasons = frag.extra.get("not_feasible_reasons") or []
    # Only the bare ``vector()`` redesign reason may be present; anything else
    # means the operand carries real translation work we must not silently drop.
    return all(r == "vector() requires manual redesign" for r in reasons)


def _strip_or_vector_fallback(expr, left_frag, right_frag):
    """Collapse ``X or vector(N)`` (or the mirror) to ``X`` with a zero-fill note.

    Returns the surviving operand fragment tagged with ``or_vector_fallback`` so
    the translator can emit the approximation warning, or ``None`` when neither
    side is a bare ``vector()`` fallback.
    """
    survivor = None
    if _is_vector_fallback_operand(right_frag) and not _is_vector_fallback_operand(left_frag):
        survivor = left_frag
    elif _is_vector_fallback_operand(left_frag) and not _is_vector_fallback_operand(right_frag):
        survivor = right_frag
    if survivor is None:
        return None
    # Drop the vector operand's not-feasible reason from the survivor: it was
    # only carried because the parser unioned child reasons upward.
    reasons = [
        r
        for r in (survivor.extra.get("not_feasible_reasons") or [])
        if r != "vector() requires manual redesign"
    ]
    if reasons:
        survivor.extra["not_feasible_reasons"] = reasons
    else:
        survivor.extra.pop("not_feasible_reasons", None)
    survivor.extra["or_vector_fallback"] = True
    return survivor


def _make_binary_fragment(expr, left_frag, op, right_frag):
    reasons = []
    for child in (left_frag, right_frag):
        for reason in child.extra.get("not_feasible_reasons", []):
            if reason not in reasons:
                reasons.append(reason)

    backend = left_frag.extra.get("parser_backend") or right_frag.extra.get("parser_backend")
    extra = {
        "left_frag": left_frag,
        "right_frag": right_frag,
    }
    if reasons:
        extra["not_feasible_reasons"] = reasons
    if backend:
        extra["parser_backend"] = backend
    return PromQLFragment(
        raw_expr=expr,
        family="binary_expr",
        binary_op=op,
        extra=extra,
    )


def _parse_fragment(expr, depth=0):
    """Parse a PromQL expression into a PromQLFragment using the AST parser.

    Requires the ``promql-parser`` package (``pip install promql-parser``).
    """
    if promql_parser is None:
        raise ImportError(
            "The 'promql-parser' package is required but not installed. "
            "Install it with: pip install promql-parser"
        )

    expr = _trim_outer_parens(expr.strip())
    logql_frag = _parse_logql_fragment(expr)
    if logql_frag:
        return logql_frag

    try:
        ast = promql_parser.parse(expr)
    except (ValueError, TypeError, Exception) as exc:
        sanitized_expr, label_map = _sanitize_promql_labels_for_ast(expr)
        if label_map and sanitized_expr != expr:
            try:
                ast = promql_parser.parse(sanitized_expr)
            except (ValueError, TypeError, Exception):
                pass
            else:
                frag = _ast_from_node(ast, expr)
                _restore_sanitized_labels(frag, label_map)
                frag.extra.setdefault("parser_backend", "ast_sanitized")
                return frag
        frag = _new_fragment(expr, backend="regex")
        frag.extra["parse_error"] = str(exc)
        return frag
    frag = _ast_from_node(ast, expr)
    frag.extra.setdefault("parser_backend", "ast")
    return frag


def _apply_fragment_to_context(frag, context):
    backend = frag.extra.get("parser_backend")
    if backend:
        context.parser_backend = backend

    summary = _find_summary_fragment(frag) or frag

    if not context.group_labels:
        context.group_labels = list(frag.group_labels or _extract_group_labels(context.clean_expr or context.promql_expr))

    if not context.outer_agg:
        context.outer_agg = frag.outer_agg or summary.outer_agg or _detect_outer_agg(context.clean_expr or context.promql_expr) or ""

    summary_inner = frag.extra.get("call_name") or frag.range_func or summary.extra.get("call_name") or summary.range_func
    if not context.inner_func and summary_inner:
        context.inner_func = summary_inner

    if not context.metric_name and summary.metric:
        context.metric_name = summary.metric

    if not context.range_window and (frag.range_window or summary.range_window):
        context.range_window = frag.range_window or summary.range_window

def _build_stats_call(
    outer_agg, inner_func, metric_name, range_window, frag=None,
    *, is_counter=False, resolver=None,
):
    esql_outer = OUTER_AGG_MAP.get(outer_agg, outer_agg.upper())
    esql_inner = AGG_FUNCTION_MAP.get(inner_func, inner_func.upper()) if inner_func else ""
    # Keep the counter-safe cast on composed (join-ratio) operands: a degraded
    # increase()/rate() over an unknown-caps counter must still wrap the metric
    # in TO_DOUBLE, exactly like the standalone/measure-spec paths (PR #234).
    # Issue #245: also cast when the target maps this field with conflicting
    # types across indices, independent of counter status.
    metric_arg = _counter_safe_metric_arg(
        esql_inner,
        metric_name,
        is_counter,
        frag.range_func if frag else None,
        counter_refuted=_counter_refuted(resolver, frag.metric) if frag else False,
        force_cast=_counter_unsafe_cast_needed(metric_name, resolver),
    )
    if esql_inner:
        inner_expr = f"{esql_inner}({metric_arg}, {range_window})"
    else:
        inner_expr = metric_arg
    return _apply_outer_agg(esql_outer, inner_expr, frag)


def _apply_outer_agg(esql_outer, inner_expr, frag):
    """Wrap ``inner_expr`` in the outer ES|QL aggregation.

    ES|QL ``PERCENTILE`` requires the percentile as a second argument, so the
    PromQL ``quantile(phi, …)`` fraction (captured as ``quantile_phi`` in the
    ``[0, 1]`` range) must be emitted as ``phi * 100``. Emitting a one-argument
    ``PERCENTILE(...)`` compiles and lints clean but fails at query time with
    "error building [percentile]: expects exactly two arguments" (issue #213).
    """
    if esql_outer == "PERCENTILE":
        phi = frag.extra.get("quantile_phi") if frag else None
        if phi is not None:
            return f"PERCENTILE({inner_expr}, {_format_scalar_value(float(phi) * 100)})"
    return f"{esql_outer}({inner_expr})"


def _esql_binary_expr(left, op, right):
    """Render a PromQL binary arithmetic expression in ES|QL syntax.

    PromQL ``^`` (power) has no ES|QL infix operator; ES|QL spells it
    ``POW(base, exponent)``. Passing ``^`` through verbatim compiles and lints
    clean but fails at query time with "token recognition error at: '^'".
    """
    if op == "^":
        return f"POW({left}, {right})"
    return f"({left} {op} {right})"


def _build_esql(context):
    alias = re.sub(r"[^a-zA-Z0-9_]", "_", context.metric_name)
    parts = [f"{context.source_type} {context.index}"]
    if context.time_filter:
        parts.append(f"| WHERE {context.time_filter}")
    for label_filter in context.label_filters:
        parts.append(f"| WHERE {label_filter}")
    stats_line = f"| STATS {alias} = {context.stats_expr}"
    by_parts = []
    if context.bucket_expr:
        by_parts.append(context.bucket_expr)
    by_parts.extend(context.group_labels)
    if by_parts:
        stats_line += f" BY {', '.join(by_parts)}"
    parts.append(stats_line)
    return "\n".join(parts)


def _frag_filters(frag, resolver):
    """Build ES|QL WHERE clauses from fragment matchers using the resolver.

    ``had_vars`` reports variable-driven label filters that were actually
    dropped, so the "Dropped variable-driven label filters" warning is only
    emitted when a matcher produced no WHERE clause. When the target binds
    ``?var`` parameters the filter is preserved (issue #64) and not counted.
    """
    _prime_frag_label_cooccurrence(frag, resolver)
    metric_field = _frag_metric_field_raw(frag, resolver)
    consumed = _metric_map_source_filter(frag, resolver)
    filters = []
    had_vars = False
    for matcher in frag.matchers:
        if _matcher_consumed_by_metric_map(matcher, consumed):
            continue
        filter_expr = _matcher_to_esql(matcher, resolver, metric_field=metric_field)
        if filter_expr:
            filters.append(filter_expr)
        elif _matcher_has_dropped_variable(matcher):
            had_vars = True
    filters.extend(_metric_map_attribute_filters(frag, resolver))
    return filters, had_vars


def _frag_has_incompatible_target_fields(frag, resolver):
    # Resolve with the same scoped metric the generator uses (issue #163);
    # otherwise this inspects a different (index-global) field than the WHERE
    # clause emits and produces a false "dropped incompatible field" warning.
    _prime_frag_label_cooccurrence(frag, resolver)
    metric_field = _frag_metric_field_raw(frag, resolver)
    consumed = _metric_map_source_filter(frag, resolver)
    return any(
        _matcher_has_incompatible_target_field(
            m,
            _resolve_label_for(resolver, m["label"], metric_field),
            resolver,
        )
        for m in frag.matchers
        if not _matcher_consumed_by_metric_map(m, consumed)
    )


def _matcher_has_dropped_variable(m):
    value = str(m.get("value", ""))
    if _grafana_param_name(value):
        return True
    return (
        bool(re.search(r"\$\w", value))
        or (m.get("op") == "=~" and value.strip() == ".*")
        or value.startswith("label_")
        or value.startswith("^label_")
    )


def _summary_mode_from_metadata(metadata):
    return bool((metadata or {}).get("summary_mode"))


def _merge_group_fields(explicit_fields, preferred_fields, preferred_origin=None):
    if preferred_origin == "legend" and explicit_fields:
        return explicit_fields
    if not preferred_fields:
        return explicit_fields
    merged = list(preferred_fields)
    for field_name in explicit_fields:
        if field_name not in merged:
            merged.append(field_name)
    return merged


def _filter_missing_resolved_fields(fields, resolver):
    """Drop resolved fields when live schema discovery proves they are absent."""
    if not fields or resolver is None or not hasattr(resolver, "field_exists"):
        return list(fields or [])
    kept = []
    for field_name in fields:
        exists = resolver.field_exists(field_name)
        if exists is False:
            continue
        kept.append(field_name)
    return kept


def _group_field_is_usable(field_name, resolver, *, drop_missing=False):
    if not field_name or resolver is None or not hasattr(resolver, "field_exists"):
        return True
    exists = resolver.field_exists(field_name)
    if exists is False:
        return not drop_missing
    if exists is not True:
        return True
    if hasattr(resolver, "has_conflicting_types") and resolver.has_conflicting_types(field_name):
        return False
    if hasattr(resolver, "is_aggregatable_field") and not resolver.is_aggregatable_field(field_name):
        return False
    return True


def _filter_usable_group_fields(fields, resolver, *, drop_missing=False):
    return [
        field_name
        for field_name in (fields or [])
        if _group_field_is_usable(field_name, resolver, drop_missing=drop_missing)
    ]


def _frag_group_labels(frag, resolver, preferred_labels=None, preferred_origin=None):
    """Resolve fragment group labels through the resolver.

    Labels that start with ``label_`` are preprocessed Grafana template
    variables (``$Var`` → ``label_Var``) and are silently dropped; keeping
    them would emit non-existent field names in the BY clause.
    """
    _prime_frag_label_cooccurrence(frag, resolver, preferred_labels)
    metric_field = _frag_metric_field_raw(frag, resolver)
    raw = [lbl for lbl in (frag.group_labels or []) if not lbl.startswith("label_")]
    explicit = resolver.resolve_labels(raw, metric_field=metric_field) if resolver else list(raw)
    preferred = (
        resolver.resolve_labels(preferred_labels or [], metric_field=metric_field)
        if resolver
        else list(preferred_labels or [])
    )
    explicit = _filter_usable_group_fields(explicit, resolver)
    preferred = _filter_usable_group_fields(preferred, resolver, drop_missing=preferred_origin == "legend")
    merged = _merge_group_fields(explicit, preferred, preferred_origin=preferred_origin)
    return _append_late_bound_group_identifiers(merged, frag)


def _late_bound_group_alias(identifier: str) -> str:
    """Stable output-column alias for a late-bound identifier control.

    ``??grouping`` -> ``grouping``. The alias is the column the query emits and
    the Lens breakdown accessor binds to; it must stay constant regardless of
    which field the viewer selects (see ``_append_late_bound_group_identifiers``).
    """
    return identifier.lstrip("?")


def _append_late_bound_group_identifiers(group_fields, frag):
    """Append late-bound ES|QL identifier controls (``??var``) to a group list.

    A Grafana ``by ($var)`` grouping names a dimension that is only chosen at
    view time. The guardrail records the variable on the fragment as an
    identifier control (``??var``); it must ride alongside the concrete group
    fields into ``STATS ... BY`` (and any downstream ``KEEP``/collapse),
    bypassing schema resolution and field filtering because it is not a physical
    field but a control reference (issue #282).

    The group list carries the *stable alias* (``grouping``) rather than the raw
    control token (``??grouping``). ``STATS ... BY grouping = ??grouping`` names
    the aggregated dimension deterministically, so the Lens breakdown accessor
    resolves the same column whichever field the control selects. Emitting the
    bare token instead would name the output column after the substituted field
    (``exporter``/``transport``/...), which the fixed accessor can never match —
    the panel then fails to render ("invalid column"). The alias -> token map is
    recorded on the fragment so the primary ``BY`` clause can expand it while
    downstream clauses keep referencing the bare alias.
    """
    identifiers = (frag.extra.get("late_bound_group_identifiers") or []) if frag else []
    if not identifiers:
        return group_fields
    out = list(group_fields)
    by_map = frag.extra.setdefault("late_bound_group_by_map", {})
    for identifier in identifiers:
        alias = _late_bound_group_alias(identifier)
        # A concrete grouping label already occupies this column name (the
        # variable is named after a real label, e.g. ``by (job, $job)``).
        # Aliasing ``job = ??job`` would rebind the concrete ``job`` grouping to
        # the control and silently drop the source dimension, so leave the token
        # out: ``late_bound_group_control_rule`` then sees the ``??var`` missing
        # from the query and degrades to not_feasible (issue #282 review).
        if alias in out and by_map.get(alias) != identifier:
            continue
        by_map[alias] = identifier
        if alias not in out:
            out.append(alias)
    return out


def _expand_late_bound_group_by_terms(by_terms, frag):
    """Render primary ``STATS ... BY`` terms, aliasing late-bound identifiers.

    A late-bound grouping alias (recorded by
    :func:`_append_late_bound_group_identifiers`) is emitted as
    ``<alias> = ??var`` so the ES|QL identifier control binds at view time while
    the aggregated column keeps a stable name. Every other term passes through
    verbatim. Use this only at the *primary* ``BY`` clause that introduces the
    grouping column; downstream ``KEEP``/``SORT``/collapse clauses reference the
    bare alias (the column already exists by then).
    """
    by_map = (frag.extra.get("late_bound_group_by_map") or {}) if frag else {}
    if not by_map:
        return list(by_terms)
    out = []
    for term in by_terms:
        token = by_map.get(term)
        if token:
            out.append(f"{_esql_identifier(term)} = {token}")
        else:
            out.append(term)
    return out


def _frag_has_incompatible_group_fields(frag, resolver, preferred_labels=None):
    if frag is None:
        return False
    # Mirror the metric-aware resolution in `_frag_group_labels` so the check
    # inspects the same BY/KEEP fields the generator emits (issue #163).
    _prime_frag_label_cooccurrence(frag, resolver, preferred_labels)
    metric_field = _frag_metric_field_raw(frag, resolver)
    raw = [lbl for lbl in (frag.group_labels or []) if not lbl.startswith("label_")]
    explicit = resolver.resolve_labels(raw, metric_field=metric_field) if resolver else list(raw)
    preferred = (
        resolver.resolve_labels(preferred_labels or [], metric_field=metric_field)
        if resolver
        else list(preferred_labels or [])
    )
    return any(not _group_field_is_usable(field_name, resolver) for field_name in explicit) or any(
        not _group_field_is_usable(field_name, resolver, drop_missing=False) for field_name in preferred
    )


def _grouping_parts(bucket_expr, group_fields, frag=None):
    """Split group fields into ``BY``-clause parts and output column names.

    ``by_parts`` feed the primary ``STATS ... BY`` (late-bound grouping aliases
    expand to ``<alias> = ??var``); ``output_group_fields`` are the resulting
    column names (bare aliases) used for breakdowns, KEEP and collapse. Pass
    ``frag`` so late-bound identifier controls are aliased (issue #282).
    """
    by_parts = []
    output_group_fields = []
    if bucket_expr:
        by_parts.append(bucket_expr)
        output_group_fields.append("time_bucket")
    by_parts.extend(_expand_late_bound_group_by_terms(group_fields, frag))
    output_group_fields.extend(group_fields)
    return by_parts, output_group_fields


_RANGE_FUNC_IN_ESQL = re.compile(
    r"\b(?:RATE|IRATE|INCREASE|DELTA|DERIV|[A-Z_]+_OVER_TIME)\s*\(", re.IGNORECASE
)


def _parts_use_range_function(parts) -> bool:
    """Whether the pipeline so far computes a windowed time-series function."""
    return any(_RANGE_FUNC_IN_ESQL.search(str(line) or "") for line in parts or [])


def _collapse_summary_ts_query(parts, output_group_fields, keep_fields, keep_time_bucket=False,
                               reduce_calc=""):
    if not output_group_fields or output_group_fields[0] != "time_bucket":
        return None
    group_fields = list(output_group_fields[1:])
    # Use ``MAX(field)`` instead of ``LAST(field, time_bucket)`` so the
    # collapse is null-safe across multi-target TS queries. When the
    # upstream STATS aggregates several metrics with implicit
    # ``_timeseries`` grouping, each per-series row has one non-null
    # column and nulls for the other series. ``LAST`` may pick any of
    # those rows and return null. ``MAX`` ignores nulls, so it returns
    # the actual measurement. The semantics are identical to ``LAST``
    # for monotonically-bucketed gauges and stats; this was surfaced by
    # reviewing the Node Exporter Full "Pressure" bar chart, which had
    # data in every bucket but rendered all-null bars.
    # Honour the panel's own reducer when we can do so safely. Grafana states it
    # in ``reduceOptions.calcs`` and it was never read: every scalar panel
    # collapsed with MAX regardless. Node Exporter Full's "CPU Busy" asks for
    # lastNotNull -- Grafana draws 1.87%, MAX over the buckets draws 79.1%. Both
    # are real numbers from real data, which is why no gate flagged it.
    #
    # LAST is only used for a single kept field. The MAX default exists for
    # null-safety: in a multi-target TS query each per-series row carries one
    # non-null column and nulls for the others, and LAST can land on a null row
    # (this is what made the "Pressure" bars render all-null). With one field
    # there are no sibling columns to land on, so LAST is safe there.
    calc = str(reduce_calc or "").lower()
    reducer = "MAX"
    if calc in ("mean", "avg"):
        reducer = "AVG"
    elif calc == "min":
        reducer = "MIN"
    wants_last = calc in ("last", "lastnotnull") and len(keep_fields) == 1
    # A rate in the FINAL bucket of the window is wrong, not merely coarse: that
    # bucket is bounded by the window edge, so it can hold too few samples.
    # Measured over one 7-minute span, 100*(1-avg(rate(idle[5m]))) read 1.682,
    # 1.711 and 1.696 in interior buckets -- tracking Prometheus -- and 22.549 in
    # the boundary one. In Kibana the window ends at "now", so a scalar panel
    # collapsing with LAST reads that boundary bucket every time.
    #
    # ES|QL has no OFFSET, so the penultimate bucket is reached by taking the
    # last two and then the older of them. With a single bucket this degrades to
    # that bucket, which is the best available answer.
    # Only for a genuinely scalar panel. A grouped panel (a pie by handler, say)
    # has one row per group, so LIMIT 1 would keep a single slice and discard the
    # rest. Grouped panels hit the same boundary-bucket problem but need a
    # per-group fix, which this is not.
    if wants_last and not group_fields and _parts_use_range_function(parts):
        parts.append("| SORT time_bucket DESC")
        parts.append("| LIMIT 2")
        parts.append("| SORT time_bucket ASC")
        parts.append("| LIMIT 1")
        # Mirror the projection the LAST path produces, so panels whose spec
        # references time_bucket (tables surface it as a date breakdown) keep it.
        kept = ", ".join(_esql_identifier(f) for f in keep_fields)
        parts.append(f"| KEEP time_bucket, {kept}" if keep_time_bucket else f"| KEEP {kept}")
        return []
    if wants_last:
        reduced = ", ".join(
            f"{_esql_identifier(field)} = LAST({_esql_identifier(field)}, time_bucket)"
            for field in keep_fields
        )
    else:
        reduced = ", ".join(
            f"{_esql_identifier(field)} = {reducer}({_esql_identifier(field)})"
            for field in keep_fields
        )
    if group_fields:
        # MAX is order-independent; no pre-collapse sort needed.
        parts.append(
            f"| STATS {reduced} BY {', '.join(_esql_identifier(f) for f in group_fields)}"
        )
        parts.append(
            "| KEEP "
            + ", ".join(_esql_identifier(f) for f in group_fields + keep_fields)
        )
        return group_fields
    if output_group_fields != ["time_bucket"]:
        return None
    # MAX is order-independent; the pre-collapse sort is a no-op in all cases.
    if keep_time_bucket:
        # Table panels surface time_bucket as a date breakdown for the operator;
        # keep it in the output. The trailing sort added by _ensure_bucket_sort
        # on the 1-row result is harmless.
        parts.append(f"| STATS time_bucket = MAX(time_bucket), {reduced}")
        parts.append(
            "| KEEP time_bucket, " + ", ".join(_esql_identifier(f) for f in keep_fields)
        )
    else:
        # Scalar panels (stat/gauge/bargauge/piechart) don't need time_bucket in
        # the output. Omitting it prevents _ensure_bucket_sort from appending a
        # redundant trailing sort on the already-collapsed single-row result.
        parts.append(f"| STATS {reduced}")
        parts.append(
            "| KEEP " + ", ".join(_esql_identifier(f) for f in keep_fields)
        )
    return []


def _frag_eval_expr(alias, frag):
    if not frag.binary_op:
        return alias, ""
    final_alias = f"{alias}_calc"
    if frag.extra.get("scalar_left") is not None:
        sv = _format_scalar_value(frag.extra["scalar_left"])
        if frag.binary_op == "^":
            return final_alias, f"POW({sv}, {alias})"
        return final_alias, f"{sv} {frag.binary_op} {alias}"
    if frag.binary_rhs and frag.binary_rhs.is_scalar:
        sv = _format_scalar_value(frag.binary_rhs.scalar_value)
        if frag.binary_op == "^":
            return final_alias, f"POW({alias}, {sv})"
        return final_alias, f"{alias} {frag.binary_op} {sv}"
    return alias, ""


def _frag_eval_line(alias, frag):
    """Build an optional EVAL line for binary-op-with-scalar."""
    final_alias, eval_expr = _frag_eval_expr(alias, frag)
    if eval_expr:
        return f"| EVAL {final_alias} = {eval_expr}", final_alias
    return None, final_alias


def _scalar_fragment_expr(frag):
    if not frag:
        return None
    if frag.family == "uptime":
        return None
    if frag.is_scalar:
        return _format_scalar_value(frag.scalar_value)
    if frag.is_time_call:
        return 'DATE_DIFF("seconds", TO_DATETIME(0), NOW())'
    return None


def _rename_measure_alias(spec, new_alias):
    old_alias = spec.alias
    if old_alias == new_alias:
        return
    spec.alias = new_alias
    if spec.final_alias == old_alias:
        spec.final_alias = new_alias
    elif spec.final_alias == f"{old_alias}_calc":
        spec.final_alias = f"{new_alias}_calc"
    if spec.eval_expr:
        spec.eval_expr = re.sub(rf"\b{re.escape(old_alias)}\b", new_alias, spec.eval_expr)


def _is_variable_driven_matcher(m):
    """Return True for matchers that originate from Grafana template variables.

    ``preprocess_grafana_macros`` preserves full-value matcher variables as
    parameter sentinels and converts remaining ``$var`` tokens to ``label_var``.
    These forms are variable-driven and should not contribute to the alias
    suffix — they are the same across binary-expression operands and would
    produce identical suffixes even when a static distinguishing matcher
    (e.g. ``status!~"[4-5].*"``) is present.

    Also handles anchored forms like ``^label_Container$`` that arise when the
    original matcher had regex anchors around the variable (``^$Container$``).
    """
    v = str(m.get("value", ""))
    # label_Var (bare preprocessed variable) or ^label_Var* (anchored form)
    return (
        v == ".*"
        or v.startswith("label_")
        or v.startswith("$")
        or v.startswith("^label_")
        or _grafana_param_name(v) is not None
    )


def _is_phantom_grafana_var(frag):
    """Return True when *frag* is a bare Grafana variable masquerading as a metric.

    ``preprocess_grafana_macros`` converts a bare ``$var`` token that appears
    outside curly-brace label selectors to ``label_var`` — a name that the
    PromQL parser accepts as a vector selector but which resolves to nothing in
    ES|QL.  When such a fragment appears as one operand of a ``*`` or ``/``
    expression (e.g. ``rate(A) * $trends``) it behaves as a user-supplied
    scalar constant.  Stripping it and emitting only the other operand is safe
    for multiplicative binary ops.
    """
    if frag is None:
        return False
    return (
        frag.family == "simple_metric"
        and frag.metric.startswith("label_")
        and not frag.matchers
        and not frag.range_func
        and not frag.outer_agg
        and not frag.is_scalar
    )


def _matcher_alias_suffix(frag):
    # Prefer non-variable matchers so that when both operands of a binary_expr
    # share the same variable-driven matchers (e.g. controller_pod=~".*"), the
    # distinguishing static matcher (e.g. status!~"[4-5].*") contributes to
    # the alias.  Without this, both operands produce identical aliases and
    # _build_shared_measure_pipeline incorrectly treats them as duplicates.
    static = [m for m in frag.matchers if not _is_variable_driven_matcher(m)]
    source = (static or frag.matchers)[:2]
    parts = []
    for matcher in source:
        label = re.sub(r"[^a-zA-Z0-9_]", "_", matcher["label"]).strip("_")
        if _is_variable_driven_matcher(matcher):
            value = ""
        else:
            value = re.sub(r"[^a-zA-Z0-9_]", "_", matcher["value"]).strip("_")[:12]
        if label or value:
            parts.append("_".join(part for part in (label, value) if part))
    if frag.range_func:
        parts.append(frag.range_func)
    if frag.outer_agg:
        parts.append(frag.outer_agg)
    return "_".join(part for part in parts if part)


def _capability_for_gauge_ts_decision(metric_name, resolver):
    """Field capability used for gauge ``TS`` vs ``FROM`` decisions.

    Prefer the *physical* target after metric_map / profile resolve. When a
    source name (e.g. a Prometheus recording rule) still exists in the index as
    a counter while ``metric_map`` remaps it to an OTel gauge, consulting the
    source capability first wrongly "disproves" TSDS-gauge and demotes the
    whole multi-target panel to ``FROM`` (inflating SUM by sample multiplicity).
    If the resolved target differs from the source name, only the target's
    capability counts — an unknown target stays unknown rather than inheriting
    the source kind.
    """
    if not metric_name or not resolver:
        return None
    resolved = _resolve_metric_field(resolver, metric_name, prefer="gauge")
    if resolved and resolved != metric_name:
        return resolver.field_capability(resolved)
    return resolver.field_capability(metric_name)


def _field_is_proven_tsds_gauge(metric_name, resolver):
    """Return True iff resolver proves the metric field is a TSDS gauge.

    "Proven" means the resolver has a non-empty field capability for the metric
    (or its resolved physical name) with ``time_series_metric=gauge``, numeric
    type family, and no conflicting type mappings. This signal lets the
    translator emit ``TS`` (which has time-series-aware aggregation semantics)
    instead of ``FROM`` (which sums every per-sample doc) for the field — see
    issue #8.
    """
    capability = _capability_for_gauge_ts_decision(metric_name, resolver)
    if not capability:
        return False
    if capability.conflicting_types:
        return False
    if capability.time_series_metric_kind != "gauge":
        return False
    return capability.type_family == "numeric"


def _field_disproven_tsds_gauge(metric_name, resolver):
    """Return True iff the resolver positively proves the field is NOT a TSDS gauge.

    "Disproven" means the resolver HAS a capability for the field (or its resolved
    physical name) and that capability is incompatible with a clean TSDS gauge:
    conflicting types across indices, a non-gauge time-series kind (e.g. counter), or
    a non-numeric type family. Returns False when the resolver has *no* information for
    the field (offline, or field not yet in the mapping) — that is the "unknown" state,
    not a disproof. This lets ``assume_tsds_gauges`` apply only when we lack evidence and
    never override evidence we do have.
    """
    capability = _capability_for_gauge_ts_decision(metric_name, resolver)
    if not capability:
        return False
    if capability.conflicting_types:
        return True
    if capability.time_series_metric_kind and capability.time_series_metric_kind != "gauge":
        return True
    return capability.type_family != "numeric"


def _gauge_can_use_ts(metric_name, resolver, rule_pack):
    """Decide whether a gauge aggregation may use ``TS`` instead of ``FROM``.

    Three-state policy:
      * resolver proves a clean TSDS gauge -> True (evidence)
      * resolver disproves TSDS gauge      -> False (evidence)
      * no information (offline / unknown)  -> ``rule_pack.assume_tsds_gauges``

    ``TS`` is required for correct gauge aggregation on a TSDS: ``FROM`` sums every
    per-sample document in a bucket, inflating SUM/COUNT by the sample multiplicity.
    """
    if _field_is_proven_tsds_gauge(metric_name, resolver):
        return True
    if not getattr(rule_pack, "assume_tsds_gauges", True):
        return False
    return not _field_disproven_tsds_gauge(metric_name, resolver)


def _legend_grouping_redundant_on_ts(frag, resolver, rule_pack):
    """Issue #99: decide whether legend-derived BY labels are redundant on TS.

    When a PromQL expression has **no explicit outer aggregation** (a bare
    selector or a range-vector function with no enclosing ``sum()``/``avg()``/…)
    and it lands on the ES|QL ``TS`` source, time series mode already groups by
    TSID (the full set of dimensions) when only ``TBUCKET`` is in the ``BY``
    clause — each unique series gets its own row. Adding a ``legendFormat``-origin
    label to ``BY`` is therefore unnecessary, and worse: it forces the time
    series function to be wrapped in an outer ``AVG()``, which distorts the value
    (~4x low on gauges). The label adds nothing Kibana's TSID-driven legend does
    not already show, so it is dropped.

    Only applies on the ``TS`` path — ``FROM`` has no TSID grouping, so dropping
    the label there would collapse multiple series into one line.
    """
    if frag.outer_agg:
        return False
    if frag.family == "simple_metric":
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        # Counters already wrap LAST_OVER_TIME in MAX (no AVG distortion); leave
        # them be. Gauges must actually be able to use TS for TSID grouping.
        return (not is_counter) and _gauge_can_use_ts(frag.metric, resolver, rule_pack)
    if frag.family == "range_agg":
        # ``rate`` / ``irate`` / ``increase`` always emit ``TS`` (counter-typed RATE
        # path). BY TBUCKET alone already yields one row per TSID per bucket, so
        # legendFormat-derived labels are redundant — and harmful when authors use
        # placeholders like ``{{input}}`` / ``{{output}}`` that are not real series
        # labels (they force AVG(RATE(...)) and break multi-target fusion).
        if frag.range_func in {"rate", "irate", "increase"}:
            return True
        # ``*_over_time`` instant aggregations also run on TS and produce one value
        # per TSID per bucket with BY TBUCKET alone. Gate on ``_gauge_can_use_ts``:
        # ``range_agg_family_rule`` forces ``source=TS`` for any ``*_over_time``
        # regardless of field typing, so without this guard a non-TSDS field would
        # have its only series-splitting label dropped and collapse every series.
        return frag.range_func in _OVER_TIME_RANGE_FUNCS and _gauge_can_use_ts(
            frag.metric, resolver, rule_pack
        )
    return False


def _drop_legend_labels_if_redundant(
    frag,
    resolver,
    rule_pack,
    group_fields,
    preferred_origin,
    summary_mode,
    allow_direct_ts_gauge=True,
):
    """Issue #99: return ``group_fields`` with redundant legendFormat-origin labels
    dropped, or unchanged if dropping is unsafe.

    Shared by the direct family rules (``translate.py``) and the formula/binary
    measure-spec path (:func:`_build_measure_spec`) so arithmetic panels avoid the
    same distorting outer ``AVG`` the direct path does. All guards must hold to drop:

    * **Non-summary panel** — the TSID split is a line-chart affordance; summary /
      categorical panels (bargauge) render their breakdown from the explicit
      ``output_group_fields`` column, so dropping there collapses per-series bars.
    * **Legend origin** — the labels came from ``legendFormat``, not an explicit
      PromQL ``by()`` (which is semantically meaningful and stays).
    * **Redundant on TS** — see :func:`_legend_grouping_redundant_on_ts`.
    * **Direct-TS form reachable** — ``simple_metric`` only splits series via the
      bare ``STATS field = field`` form; when that is disabled (multi-target fusion
      passes ``allow_direct_ts_gauge=False``) an explicit ``AVG`` would collapse the
      series, so the label is kept. ``range_agg`` does not use this form, so it is
      unaffected.
    """
    if summary_mode:
        return group_fields
    if preferred_origin != "legend":
        return group_fields
    if not group_fields:
        return group_fields
    if _frag_group_labels(frag, resolver):
        return group_fields
    if frag.family == "simple_metric" and not allow_direct_ts_gauge:
        return group_fields
    if not _legend_grouping_redundant_on_ts(frag, resolver, rule_pack):
        return group_fields
    if _legend_group_fields_are_real(group_fields, resolver):
        # The TSID split is invisible to Kibana. ``TS`` does emit one row per
        # series per bucket, but the chart binds series identity to a breakdown
        # *column*, not to the TSID -- so dropping a legend label that names a
        # real dimension leaves N rows per bucket that are column-identical, and
        # Kibana draws N same-named, indistinguishable series (Redis 763
        # Hits/Misses showed two "hits" and two "misses" in one tooltip once a
        # second instance existed). Keep the label: the resulting outer
        # aggregation is over a group that already holds one value per series
        # per bucket, so it does not distort. Phantom placeholders such as
        # ``{{input}}`` fail this check and still drop, which is what makes the
        # AVG-wrapping / fusion-breaking case above safe.
        return group_fields
    return []


def _legend_group_fields_are_real(group_fields, resolver):
    """True only when every legend-derived BY field is a proven target field.

    Deliberately conservative: ``field_exists`` returns ``None`` when discovery
    never ran (offline, no ``--es-url``), and that is treated as "not proven" so
    offline runs keep their existing behaviour. Only a live-confirmed dimension
    earns the label back.
    """
    if not resolver or not group_fields:
        return False
    return all(resolver.field_exists(field) is True for field in group_fields)


def _can_use_direct_ts_gauge(metric_name, resolver, group_fields, frag, rule_pack=None):
    if group_fields:
        return False
    if frag and frag.extra.get("wrapped_scalar"):
        return False
    if rule_pack is not None:
        return _gauge_can_use_ts(metric_name, resolver, rule_pack)
    return _field_is_proven_tsds_gauge(metric_name, resolver)


def gauge_default_agg_warning(group_fields, metric, default_agg):
    """Honest warning for the default-aggregation gauge path.

    With grouping labels present, the aggregator is a faithful per-series
    intra-bucket downsample, not a migration warning. Without any labels,
    multiple series collapse into a single line — say so, and include the token
    ``drop`` so ``build_query_ir`` records it as a semantic loss.
    """
    if group_fields:
        return ""
    return (
        f"Collapsed all series of `{metric}` into a single {default_agg} line; the source "
        "selector has no series labels (no legend, by(), or dashboard reference), so per-series "
        "detail is dropped. Add a legend/by() or migrate with target access to recover "
        "per-series fidelity."
    )


def _build_measure_spec(
    frag,
    resolver,
    rule_pack,
    alias_hint="",
    summary_mode=False,
    preferred_group_labels=None,
    allow_direct_ts_gauge=True,
    preferred_group_labels_origin=None,
    allow_tsds_gauge_promotion=True,
    drop_legend_labels=True,
):
    if not frag or (not frag.metric and frag.family != "uptime"):
        return None

    filters, had_vars = _frag_filters(frag, resolver)
    warnings = []
    if had_vars:
        warnings.append("Dropped variable-driven label filters during migration")
    had_incompatible_fields = _frag_has_incompatible_target_fields(frag, resolver)
    if had_incompatible_fields:
        warnings.append("Dropped label filters with incompatible target field types during migration")
    group_fields = _frag_group_labels(
        frag,
        resolver,
        preferred_group_labels,
        preferred_origin=preferred_group_labels_origin,
    )
    # Issue #99: drop legend-origin BY labels that ES|QL TSID already splits, so
    # formula/binary panels avoid the distorting outer AVG the direct path now skips.
    # ``drop_legend_labels`` lets the formula planner force-disable the drop when a
    # mixed-family plan would otherwise produce divergent (unmergeable) groupings.
    if drop_legend_labels:
        group_fields = _drop_legend_labels_if_redundant(
            frag,
            resolver,
            rule_pack,
            group_fields,
            preferred_group_labels_origin,
            summary_mode,
            allow_direct_ts_gauge,
        )
    if _frag_has_incompatible_group_fields(frag, resolver, preferred_group_labels):
        warnings.append("Dropped grouping fields with incompatible target field types during migration")
    if alias_hint:
        suffix = alias_hint
    else:
        suffix = _matcher_alias_suffix(frag)
    alias = _safe_alias(frag.metric, suffix)
    final_alias = None
    eval_expr = ""

    metric_field = frag.metric

    if frag.family == "simple_metric":
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        can_use_direct_ts_gauge = allow_direct_ts_gauge and _can_use_direct_ts_gauge(
            frag.metric, resolver, group_fields, frag, rule_pack
        )
        # Issue #8: keep TS for TSDS gauges whenever the direct-gauge path isn't
        # available — either because of group_fields or because the caller disabled it
        # (multi-target fusion uses ``allow_direct_ts_gauge=False`` since ``STATS field =
        # field`` cannot be CASE-wrapped, but ``AVG(field)`` can). ``FROM`` against a TSDS
        # sums every per-sample doc and inflates the value, so use ``TS`` with the default
        # aggregator instead. Gauge TSDS status is proven by the resolver or, when unknown,
        # assumed per ``rule_pack.assume_tsds_gauges`` (the migration default).
        can_use_ts_aggregated_gauge = (
            allow_tsds_gauge_promotion
            and (not is_counter)
            and (not can_use_direct_ts_gauge)
            and (not (frag.extra.get("wrapped_scalar") if frag else False))
            and _gauge_can_use_ts(frag.metric, resolver, rule_pack)
        )
        if is_counter:
            source = "TS"
            time_filter = rule_pack.ts_time_filter
            bucket_expr = rule_pack.ts_bucket
            metric_field = _resolve_frag_metric_field(frag, resolver, prefer="counter")
            # Bare counter reference: use LAST_OVER_TIME to return the raw cumulative
            # value per TBUCKET window, matching PromQL instant-vector semantics.
            stats_expr = f"MAX(LAST_OVER_TIME({metric_field}))"
            warnings.append("Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value")
        elif can_use_direct_ts_gauge:
            source = "TS"
            time_filter = rule_pack.ts_time_filter
            bucket_expr = rule_pack.ts_bucket
            metric_field = _resolve_frag_metric_field(frag, resolver, prefer="gauge")
            stats_expr = f"MAX(LAST_OVER_TIME({metric_field}))"
        elif can_use_ts_aggregated_gauge:
            source = "TS"
            time_filter = rule_pack.ts_time_filter
            bucket_expr = rule_pack.ts_bucket
            default_agg = rule_pack.default_gauge_agg.upper()
            metric_field = _resolve_frag_metric_field(frag, resolver, prefer="gauge")
            agg_arg = metric_field
            if _counter_unsafe_cast_needed(metric_field, resolver):
                agg_arg = f"TO_DOUBLE({metric_field})"
                warnings.append(_counter_unsafe_cast_warning(metric_field, resolver))
            stats_expr = f"{default_agg}({agg_arg})"
            warning = gauge_default_agg_warning(group_fields, frag.metric, default_agg)
            if warning:
                warnings.append(warning)
        else:
            source = "FROM"
            time_filter = rule_pack.from_time_filter
            bucket_expr = rule_pack.from_bucket
            default_agg = rule_pack.default_gauge_agg.upper()
            metric_field = _resolve_frag_metric_field(frag, resolver, prefer="gauge")
            agg_arg = metric_field
            if _counter_unsafe_cast_needed(metric_field, resolver):
                agg_arg = f"TO_DOUBLE({metric_field})"
                warnings.append(_counter_unsafe_cast_warning(metric_field, resolver))
            stats_expr = f"{default_agg}({agg_arg})"
            if frag.extra.get("wrapped_scalar"):
                warnings.append("Approximated scalar() as a direct metric value")
            else:
                warning = gauge_default_agg_warning(group_fields, frag.metric, default_agg)
                if warning:
                    warnings.append(warning)
    elif frag.family == "simple_agg":
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        if frag.outer_agg == "count" and is_counter:
            return None
        # Issue #8: gauge aggregations against a TSDS must use TS, not FROM — FROM sums
        # every per-sample doc instead of one value per series per bucket. TSDS status is
        # proven by the resolver or, when unknown, assumed per ``assume_tsds_gauges``.
        gauge_uses_ts = (
            allow_tsds_gauge_promotion
            and (not is_counter)
            and _gauge_can_use_ts(frag.metric, resolver, rule_pack)
        )
        source = "TS" if (is_counter or gauge_uses_ts) else "FROM"
        time_filter = rule_pack.ts_time_filter if source == "TS" else rule_pack.from_time_filter
        bucket_expr = rule_pack.ts_bucket if source == "TS" else rule_pack.from_bucket
        gauge_metric_field = _resolve_frag_metric_field(frag, resolver, prefer="gauge")
        if is_counter and frag.outer_agg != "count":
            metric_field = _resolve_frag_metric_field(frag, resolver, prefer="counter")
            # Bare counter aggregation: use LAST_OVER_TIME as inner function so the
            # outer aggregation operates on raw cumulative values, not rates.
            inner_expr = f"LAST_OVER_TIME({metric_field})"
            warnings.append("Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value")
        elif (
            frag.outer_agg in _COUNTER_UNSAFE_OUTER_AGGS
            and _counter_unsafe_cast_needed(gauge_metric_field, resolver)
        ):
            # Issue #245: the target maps this field with conflicting types
            # across indices, so SUM/MAX/MIN/AVG/STDDEV/QUANTILE may reject
            # the bare field at runtime ("ambiguities in index mappings").
            # TO_DOUBLE is valid under either FROM or TS (unlike
            # LAST_OVER_TIME, a TS-only function), so it defends the
            # aggregation without disturbing the source/bucket already chosen
            # above, instead of gambling on a bare aggregation.
            metric_field = gauge_metric_field
            inner_expr = f"TO_DOUBLE({metric_field})"
            warnings.append(_counter_unsafe_cast_warning(gauge_metric_field, resolver))
        else:
            metric_field = gauge_metric_field
            inner_expr = metric_field
            # Issue #148: a bare SUM/MAX/MIN/AVG against a field that is actually
            # counter_long in ES fails with verification_exception. When the
            # target cannot prove the field is a gauge, keep the query but warn.
            if frag.outer_agg in _COUNTER_UNSAFE_OUTER_AGGS:
                counter_warning = _counter_type_uncertainty_warning(frag.metric, resolver)
                if counter_warning:
                    warnings.append(counter_warning)
        outer = OUTER_AGG_MAP.get(frag.outer_agg, rule_pack.default_gauge_agg.upper())
        stats_expr = _apply_outer_agg(outer, inner_expr, frag)
    elif frag.family == "range_agg":
        esql_inner = AGG_FUNCTION_MAP.get(frag.range_func)
        if not esql_inner:
            return None
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        # ES|QL's RATE / IRATE / INCREASE require a ``counter_*`` typed
        # field; emitting them against a gauge-typed field hard-fails with
        # ``first argument of [RATE(...)] must be counter``. The shared
        # policy in resolve_counter_range_translation decides whether to
        # degrade to a gauge analogue (warned) or keep the source-faithful
        # counter form (warned when live caps disagree).
        esql_inner, counter_warning, is_counter = resolve_counter_range_translation(
            frag.range_func, frag.metric, is_counter, resolver, esql_inner
        )
        if counter_warning:
            warnings.append(counter_warning)
        esql_inner, is_counter, map_rate_warnings = _plan_metric_map_rate_transform(
            frag, resolver, esql_inner, is_counter
        )
        warnings.extend(map_rate_warnings)
        needs_ts = is_counter or frag.range_func in AGG_FUNCTION_MAP
        source = "TS" if needs_ts else "FROM"
        time_filter = rule_pack.ts_time_filter if source == "TS" else rule_pack.from_time_filter
        bucket_expr = rule_pack.ts_bucket if source == "TS" else rule_pack.from_bucket
        prefer = "counter" if (frag.range_func in {"rate", "irate", "increase"} and is_counter) else "gauge"
        metric_field = _resolve_frag_metric_field(frag, resolver, prefer=prefer)
        inner_arg = _counter_safe_metric_arg(
            esql_inner,
            metric_field,
            is_counter,
            frag.range_func,
            counter_refuted=_counter_refuted(resolver, frag.metric),
            force_cast=_counter_unsafe_cast_needed(metric_field, resolver),
        )
        if (
            not is_counter
            and (esql_inner or "").upper() not in _COUNTER_INPUT_ESQL_FUNCS
            and _counter_unsafe_cast_needed(metric_field, resolver)
        ):
            warnings.append(_counter_unsafe_cast_warning(metric_field, resolver))
        if esql_inner:
            inner_expr = f"{esql_inner}({inner_arg}, {frag.range_window})"
        else:
            # drop_rate → gauge: outer agg operates on the bare field.
            inner_expr = inner_arg
        outer = OUTER_AGG_MAP.get(frag.outer_agg, "") if frag.outer_agg else ""
        if not outer and source == "TS" and group_fields:
            stats_expr = f"AVG({inner_expr})"
        else:
            stats_expr = _apply_outer_agg(outer, inner_expr, frag) if outer else inner_expr
    elif frag.family == "scaled_agg":
        esql_inner = AGG_FUNCTION_MAP.get(frag.range_func)
        if not esql_inner:
            return None
        source = "TS"
        time_filter = rule_pack.ts_time_filter
        bucket_expr = rule_pack.ts_bucket
        is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
        esql_inner, counter_warning, is_counter = resolve_counter_range_translation(
            frag.range_func, frag.metric, is_counter, resolver, esql_inner
        )
        if counter_warning:
            warnings.append(counter_warning)
        esql_inner, is_counter, map_rate_warnings = _plan_metric_map_rate_transform(
            frag, resolver, esql_inner, is_counter
        )
        warnings.extend(map_rate_warnings)
        esql_outer = OUTER_AGG_MAP.get(frag.outer_agg, "AVG")
        prefer = "counter" if (frag.range_func in {"rate", "irate", "increase"} and is_counter) else "gauge"
        metric_field = _resolve_frag_metric_field(frag, resolver, prefer=prefer)
        inner_arg = _counter_safe_metric_arg(
            esql_inner,
            metric_field,
            is_counter,
            frag.range_func,
            counter_refuted=_counter_refuted(resolver, frag.metric),
            force_cast=_counter_unsafe_cast_needed(metric_field, resolver),
        )
        if (
            not is_counter
            and (esql_inner or "").upper() not in _COUNTER_INPUT_ESQL_FUNCS
            and _counter_unsafe_cast_needed(metric_field, resolver)
        ):
            warnings.append(_counter_unsafe_cast_warning(metric_field, resolver))
        if esql_inner:
            inner_windowed = f"{esql_inner}({inner_arg}, {frag.range_window})"
        else:
            inner_windowed = inner_arg
        stats_expr = _apply_outer_agg(esql_outer, inner_windowed, frag)
    elif frag.family == "nested_agg":
        raw_inner_groups = list(frag.extra.get("inner_group", []) or [])
        inner_groups = (
            resolver.resolve_labels(raw_inner_groups) if resolver else list(raw_inner_groups)
        )
        if frag.outer_agg == "count" and frag.extra.get("inner_agg") == "count" and inner_groups:
            # A plain COUNT_DISTINCT(label) has no time-series-specific
            # semantics, so it is safe on either source. Prefer TS whenever
            # the metric would use TS elsewhere (proven/assumed counter or
            # TSDS gauge) — otherwise this spec is forced onto FROM and can
            # never merge with a sibling RATE()/IRATE() operand on the same
            # metric (the extremely common node_exporter "busy % = per-mode
            # rate / core count" idiom), which needs TS and has no FROM
            # equivalent. COUNT_DISTINCT mixed into a TS STATS alongside a
            # windowed time-series aggregate is valid ES|QL.
            #
            # Gate the whole preference behind ``allow_tsds_gauge_promotion``
            # (not just the gauge half) so the existing binary-expr
            # reconciliation retry — which rebuilds both operands with
            # promotion disabled when their sources diverge — can pull this
            # spec back onto FROM to match a sibling that is unconditionally
            # pinned there (e.g. a ``scalar()``-wrapped gauge). Without this,
            # a same-metric ratio against a scalar-wrapped gauge sibling would
            # regress from feasible (both FROM) to not_feasible (FROM vs TS).
            #
            # Prefer the *exclusive* inner label (in the inner ``by(...)`` but
            # not the outer) for COUNT_DISTINCT — e.g. ``count by(job, instance)
            # (count by(job, instance, cpu)(...))`` must count distinct ``cpu``,
            # not ``job``. Falling back to ``inner_groups[0]`` would pick a
            # grouping key and under-count cores (Docker/node Load panels).
            outer_raw = {
                lbl for lbl in (frag.group_labels or []) if not str(lbl).startswith("label_")
            }
            exclusive_raw = [lbl for lbl in raw_inner_groups if lbl not in outer_raw]
            if len(exclusive_raw) > 1:
                # The outer count() counts distinct *tuples* of every inner
                # label that is not already an outer grouping key — e.g.
                # ``count by(job)(count by(job, instance, cpu)(node_cpu))``
                # counts distinct ``(instance, cpu)`` pairs per job. ES|QL
                # COUNT_DISTINCT takes a single field, so collapsing to one
                # exclusive label (``exclusive_raw[0]``) would under-count
                # whenever another exclusive label varies within a group (an
                # instance with multiple CPUs). There is no faithful
                # single-field expression, so fail closed as not_feasible
                # rather than emit wrong math.
                return None
            if exclusive_raw:
                count_field = (
                    resolver.resolve_label(exclusive_raw[0]) if resolver else exclusive_raw[0]
                )
            else:
                count_field = inner_groups[0]
            # The COUNT_DISTINCT is over a label, but this spec still emits a
            # ``<metric> IS NOT NULL`` presence guard in the fused-measure
            # pipeline. Resolve the metric to its physical field so a
            # metric_map / profile rename is honored there too — otherwise the
            # guard references the raw source name and empties the panel.
            metric_field = _resolve_frag_metric_field(frag, resolver, prefer="gauge")
            is_counter = resolver.is_counter(frag.metric) if resolver else _is_counter_fallback(frag.metric, rule_pack)
            gauge_uses_ts = (not is_counter) and _gauge_can_use_ts(frag.metric, resolver, rule_pack)
            if allow_tsds_gauge_promotion and (is_counter or gauge_uses_ts):
                source = "TS"
                time_filter = rule_pack.ts_time_filter
                bucket_expr = rule_pack.ts_bucket
            else:
                source = "FROM"
                time_filter = rule_pack.from_time_filter
                bucket_expr = rule_pack.from_bucket
            stats_expr = f"COUNT_DISTINCT({count_field})"
            warnings.append(f"Approximated nested count(count()) as COUNT_DISTINCT({count_field})")
        else:
            return None
    elif frag.family == "histogram_quantile":
        phi = frag.extra.get("quantile_phi")
        if phi is None or not 0.0 <= phi <= 1.0:
            return None
        bucket_agg = frag.extra.get("bucket_agg") or ""
        if bucket_agg and bucket_agg != "sum":
            return None
        bucket_metric = frag.extra.get("bucket_metric") or ""
        if bucket_metric.endswith("_bucket"):
            has_le_matcher = any(
                isinstance(m, dict) and m.get("label") == "le" for m in (frag.matchers or [])
            )
            if has_le_matcher or not frag.extra.get("had_le_grouping"):
                return None

        # Match the direct histogram_quantile translator: only emit PERCENTILE()
        # when target schema proves the base field is a native histogram. Unknown
        # or scalar fields fail closed so formula wrapping never hides that gap.
        physical_metric = _resolve_frag_metric_field(frag, resolver, prefer=None)
        field_type = (
            (resolver.field_type(physical_metric) if resolver else "") or ""
        ).strip().lower()
        if field_type == "exponential_histogram":
            value_expr = physical_metric
        elif field_type == "histogram":
            value_expr = f"TO_TDIGEST({physical_metric})"
        else:
            return None

        source = "TS"
        time_filter = rule_pack.ts_time_filter
        bucket_expr = rule_pack.ts_bucket
        metric_field = physical_metric
        percentile_value = _format_scalar_value(round(phi * 100, 10))
        stats_expr = f"PERCENTILE({value_expr}, {percentile_value})"
        warnings.append(
            "histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is "
            "approximate — PERCENTILE uses t-digest, which treats histogram buckets as point "
            "masses rather than interpolating within them as Prometheus does, so results can "
            "diverge noticeably when traffic concentrates in a few wide buckets (the common "
            "latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for "
            "exact results."
        )
    elif frag.family == "uptime":
        start_metric = frag.metric
        start_matchers = frag.matchers
        if not start_metric and frag.binary_rhs and isinstance(frag.binary_rhs, PromQLFragment):
            if frag.binary_rhs.family == "join" and frag.extra.get("start_metric"):
                start_metric = frag.extra["start_metric"]
                start_matchers = frag.extra.get("start_matchers", [])
            elif frag.binary_rhs.metric:
                start_metric = frag.binary_rhs.metric
                start_matchers = frag.binary_rhs.matchers
        if not start_metric:
            return None
        start_frag = PromQLFragment(matchers=start_matchers)
        filters, had_vars = _frag_filters(start_frag, resolver)
        warnings = []
        if had_vars:
            warnings.append("Dropped variable-driven label filters during migration")
        had_incompatible_fields = _frag_has_incompatible_target_fields(start_frag, resolver)
        if had_incompatible_fields:
            warnings.append("Dropped label filters with incompatible target field types during migration")
        alias = _safe_alias(f"{start_metric}_start_time_ms", suffix)
        final_alias = _safe_alias(f"{start_metric}_uptime_seconds", alias_hint)
        source = "FROM"
        time_filter = rule_pack.from_time_filter
        bucket_expr = rule_pack.from_bucket if summary_mode else ""
        metric_field = _resolve_metric_field(resolver, start_metric, prefer="gauge")
        uptime_arg = metric_field
        if _counter_unsafe_cast_needed(metric_field, resolver):
            uptime_arg = f"TO_DOUBLE({metric_field})"
            warnings.append(_counter_unsafe_cast_warning(metric_field, resolver))
        stats_expr = f"MAX({uptime_arg} * 1000)"
        eval_expr = f'DATE_DIFF("seconds", TO_DATETIME({alias}), NOW())'
    else:
        return None

    if final_alias is None:
        final_alias, eval_expr = _frag_eval_expr(alias, frag)
    labels = _frag_source_labels(frag)
    if frag.family in {"simple_metric", "simple_agg"}:
        source, time_filter, bucket_expr, metric_field, stats_expr = (
            _apply_metric_map_to_rate_on_simple(
                frag,
                resolver,
                rule_pack,
                source=source,
                time_filter=time_filter,
                bucket_expr=bucket_expr,
                metric_field=metric_field,
                stats_expr=stats_expr,
                warnings=warnings,
            )
        )
    unit_scale = _metric_map_unit_scale(resolver, frag.metric, source_labels=labels)
    stats_expr = _apply_unit_scale(stats_expr, unit_scale)
    for note in _metric_map_unapplied_notes(resolver, frag.metric, source_labels=labels):
        if note not in warnings:
            warnings.append(note)
    return MeasureSpec(
        source_type=source,
        time_filter=time_filter,
        bucket_expr=bucket_expr,
        group_fields=group_fields,
        filters=filters,
        alias=alias,
        stats_expr=stats_expr,
        final_alias=final_alias,
        eval_expr=eval_expr,
        metric_name=frag.metric,
        metric_field=metric_field,
        warnings=warnings,
        target_index=_metric_map_target_index(resolver, frag.metric, source_labels=labels),
    )


def _union_group_fields(specs):
    """Preserve first-seen order while collecting the union of group fields."""
    ordered: list[str] = []
    for spec in specs:
        for group_field in spec.group_fields or []:
            if group_field not in ordered:
                ordered.append(group_field)
    return ordered


def _group_fields_nested_subsets(specs):
    """True when every spec's groups are a subset of some single max set.

    Enables broadcasting an ungrouped series into a grouped peer (e.g. QoS
    ``by (qos_class)`` + total ``sum(...)``) without joining unrelated
    dimensions (``qos_class`` vs ``instance``).
    """
    sets = [frozenset(spec.group_fields or []) for spec in specs]
    return any(all(s <= candidate for s in sets) for candidate in sets)


def _measure_specs_mergeable(specs):
    if not specs or any(spec is None for spec in specs):
        return False
    base = specs[0]
    base_filters = sorted(base.filters)
    if not _group_fields_nested_subsets(specs):
        return False
    for spec in specs[1:]:
        if spec.source_type != base.source_type:
            return False
        if spec.time_filter != base.time_filter or spec.bucket_expr != base.bucket_expr:
            return False
        if sorted(spec.filters) != base_filters:
            # Divergent per-target filters must be CASE-wrapped into the
            # stats_expr. ``_inline_filters_into_stats_expr`` already verifies
            # the expression is shaped as ``AGG(field)`` (returns None otherwise)
            # so the source command (FROM or TS) does not matter — CASE inside
            # an aggregation works the same in either mode (issue #8 follow-up).
            if _inline_filters_into_stats_expr(base.stats_expr, base.filters) is None:
                return False
            if _inline_filters_into_stats_expr(spec.stats_expr, spec.filters) is None:
                return False
    return True


def _common_filters(specs):
    if not specs:
        return []
    common = []
    for filter_expr in specs[0].filters:
        if filter_expr not in common and all(filter_expr in spec.filters for spec in specs[1:]):
            common.append(filter_expr)
    return common


def _inline_filters_into_stats_expr(stats_expr, filters, timeseries_window="5m"):
    if not filters:
        return stats_expr
    match = re.match(r"^(?P<agg>[A-Z_]+)\((?P<inner>.+)\)$", stats_expr or "")
    if not match:
        return None
    agg = match.group("agg")
    inner = match.group("inner").strip()
    condition = " and ".join(f"({filter_expr})" for filter_expr in filters)
    # PERCENTILE(value_expr, phi) takes the percentile as a second, top-level
    # argument (issue #213). Only the value expression may be wrapped in CASE;
    # folding the whole inner would push the percentile constant inside CASE and
    # emit an invalid 3-arg CASE. Split the trailing percentile arg off first.
    if agg == "PERCENTILE":
        args = _split_top_level_csv(inner)
        if len(args) == 2:
            value_expr, percentile = args[0].strip(), args[1].strip()
            return f"{agg}(CASE({condition}, {value_expr}, NULL), {percentile})"
        return None
    ts_match = re.fullmatch(r"(?P<field>.+),\s*(?P<window>[^,]+)", inner)
    # A top-level windowed time-series function (the *_OVER_TIME family AND the
    # counter range functions RATE/IRATE/INCREASE/DELTA/DERIV) takes the window
    # as its own trailing argument. Only the value (field) may be wrapped in
    # CASE; folding the whole inner would push the window literal into the CASE
    # and emit an invalid 4-arg CASE (issue: bare RATE + per-operand filter).
    if (
        agg.endswith("_OVER_TIME")
        or agg in {"RATE", "IRATE", "INCREASE", "DELTA", "DERIV"}
    ) and ts_match:
        field = ts_match.group("field").strip()
        window = ts_match.group("window").strip()
        # CASE must wrap the time-series call, not the metric field inside it.
        # ``IRATE(CASE(cond, field, NULL), window)`` ClassCasts
        # (ReferenceAttribute → Bucket) on current ES; ``CASE(cond, IRATE(field,
        # window), NULL)`` is legal.
        return f"CASE({condition}, {agg}({field}, {window}), NULL)"
    nested_ts = re.fullmatch(
        r"(?P<func>RATE|IRATE|INCREASE|DELTA|DERIV|AVG_OVER_TIME|SUM_OVER_TIME|MIN_OVER_TIME|MAX_OVER_TIME|COUNT_OVER_TIME|LAST_OVER_TIME|PRESENT_OVER_TIME)\((?P<field>.+),\s*(?P<window>[^,]+)\)",
        inner,
    )
    if nested_ts:
        func = nested_ts.group("func")
        field = nested_ts.group("field").strip()
        window = nested_ts.group("window").strip()
        if func in {"RATE", "IRATE", "INCREASE", "DELTA", "DERIV"}:
            # Filtering the counter argument itself makes Elasticsearch 9.5
            # crash for RATE/IRATE with a grouping Bucket cast. Apply the
            # per-series filter to the range-function result instead; the
            # enclosing aggregate still ignores non-matching rows via NULL.
            return f"{agg}(CASE({condition}, {func}({field}, {window}), NULL))"
        return f"{agg}({func}(CASE({condition}, {field}, NULL), {window}))"
    # Window-less ``LAST_OVER_TIME(field)`` (and siblings) are common on the
    # counter-without-rate summary path. CASE-wrap the field the same way as the
    # windowed form so multi-target panels with divergent label filters (Express
    # "Count by class") can fuse via the shared measure pipeline instead of
    # AND-merging incompatible WHERE clauses.
    nested_bare = re.fullmatch(
        r"(?P<func>AVG_OVER_TIME|SUM_OVER_TIME|MIN_OVER_TIME|MAX_OVER_TIME|COUNT_OVER_TIME|LAST_OVER_TIME|PRESENT_OVER_TIME)\((?P<field>[^,]+)\)",
        inner,
    )
    if nested_bare:
        func = nested_bare.group("func")
        field = nested_bare.group("field").strip()
        window = str(timeseries_window or "").strip()
        if window:
            return f"{agg}({func}(CASE({condition}, {field}, NULL), {window}))"
        return f"{agg}({func}(CASE({condition}, {field}, NULL)))"
    if inner == "*":
        if agg == "COUNT":
            return f"SUM(CASE({condition}, 1, 0))"
        return None
    return f"{agg}(CASE({condition}, {inner}, NULL))"


def _measure_pipeline_index(index, specs) -> str:
    """Prefer a unanimous metric_map ``target_index`` when present."""
    overrides = [
        str(getattr(spec, "target_index", "") or "").strip()
        for spec in specs or []
        if str(getattr(spec, "target_index", "") or "").strip()
    ]
    unique = sorted(set(overrides))
    if overrides and len(unique) == 1:
        return overrides[0]
    if len(unique) > 1:
        note = (
            "metric_map target_index values differ across metrics in one query "
            f"({', '.join(unique)}); using default index {index!r}"
        )
        for spec in specs or []:
            if note not in getattr(spec, "warnings", []):
                spec.warnings.append(note)
    return index


def _build_shared_measure_pipeline(index, specs):
    if not _measure_specs_mergeable(specs):
        return None

    unique_specs = []
    by_alias = {}
    for spec in specs:
        signature = (
            spec.source_type,
            spec.time_filter,
            spec.bucket_expr,
            tuple(spec.group_fields),
            tuple(spec.filters),
            spec.stats_expr,
            spec.final_alias,
            spec.eval_expr,
        )
        existing = by_alias.get(spec.alias)
        if existing is None:
            by_alias[spec.alias] = signature
            unique_specs.append(spec)
            continue
        if existing != signature:
            return None
    specs = unique_specs
    specs = _normalize_mixed_ts_stats_exprs(specs)

    base = specs[0]
    common_filters = _common_filters(specs)
    union_groups = _union_group_fields(specs)
    group_fields = (["time_bucket"] if base.bucket_expr else []) + union_groups
    by_parts = ([base.bucket_expr] if base.bucket_expr else []) + union_groups
    stats_terms = []
    timeseries_window = _timeseries_stats_window(specs)
    for spec in specs:
        scoped_filters = [filter_expr for filter_expr in spec.filters if filter_expr not in common_filters]
        scoped_expr = _inline_filters_into_stats_expr(
            spec.stats_expr,
            scoped_filters,
            timeseries_window=timeseries_window,
        )
        if not scoped_expr:
            return None
        stats_terms.append(f"{_esql_identifier(spec.alias)} = {scoped_expr}")
    # Same CASE-shape / mixed-TS invariants as ``_merge_pretranslated_xy_queries``.
    stats_terms = _finalize_fused_stats_assignments(
        stats_terms,
        group_fields=base.group_fields,
        source_type=base.source_type,
    )
    parts = [
        f"{base.source_type} {_measure_pipeline_index(index, specs)}",
        f"| WHERE {base.time_filter}",
        *_build_where_lines(common_filters),
    ]
    presence_metrics = []
    for spec in specs:
        physical_field = str(spec.metric_field or spec.metric_name or "").strip()
        if physical_field and physical_field not in presence_metrics:
            presence_metrics.append(physical_field)
    if presence_metrics:
        parts.append("| WHERE " + " OR ".join(f"{metric} IS NOT NULL" for metric in presence_metrics))
    stats_line = "| STATS " + ", ".join(stats_terms)
    if by_parts:
        stats_line += f" BY {', '.join(by_parts)}"
    parts.append(stats_line)
    metric_fields = []
    for spec in specs:
        if spec.eval_expr:
            # ``final_alias`` may be a legend-derived reserved word (e.g. "IN");
            # quote the emitted identifier while keeping the bare name in
            # ``metric_fields`` for Kibana column/label matching.
            parts.append(f"| EVAL {_esql_identifier(spec.final_alias)} = {spec.eval_expr}")
        metric_fields.append(spec.final_alias)
    return parts, group_fields, metric_fields


def _timeseries_stats_window(specs):
    for spec in specs:
        match = re.search(r"\b[A-Z_]+_OVER_TIME\([^,]+,\s*([^)]+)\)", spec.stats_expr or "")
        if match:
            return match.group(1).strip()
    return "5m"


_ESQL_FIELD_REFERENCE_PATTERN = r"(?:`(?:\\.|``|[^`])*`|[A-Za-z_][A-Za-z0-9_.]*)"


_BARE_TS_VALUE_ARG = re.compile(
    r"\b(?P<func>RATE|IRATE|INCREASE|DELTA|DERIV|AVG_OVER_TIME|SUM_OVER_TIME|"
    r"MIN_OVER_TIME|MAX_OVER_TIME|COUNT_OVER_TIME|LAST_OVER_TIME|PRESENT_OVER_TIME)"
    rf"\((?P<field>{_ESQL_FIELD_REFERENCE_PATTERN})\s*,\s*(?P<window>[^)]+)\)"
)


# CASE(cond, field, NULL) nested as the *value* arg of a TS range/window func.
# ``cond`` is typically ``true`` or a parenthesized comparison like
# ``(mode == "user")``. Only RATE/IRATE/INCREASE/DELTA/DERIV are rewritten to
# outer CASE — OVER_TIME keeps the inner-CASE shape used by the translator.
_TS_INNER_CASE_VALUE_ARG = re.compile(
    r"\b(?P<func>RATE|IRATE|INCREASE|DELTA|DERIV)"
    rf"\(\s*CASE\((?P<cond>\([^)]*\)|true|false|[A-Za-z_][A-Za-z0-9_.]*)\s*,\s*"
    rf"(?P<field>{_ESQL_FIELD_REFERENCE_PATTERN})\s*,\s*NULL\)\s*,\s*(?P<window>[^)]+)\)",
    re.IGNORECASE,
)

# The condition may itself contain commas -- ``COALESCE(label, "") RLIKE ?var``
# is a normal label matcher (an absent label reads as "" in PromQL). Matching the
# condition with ``[^,]+`` silently stopped recognising the outer shape as soon as
# one appeared, and the caller then nested an identity CASE inside the outer
# filter CASE. Anchor on the TS call and the trailing ``, NULL)`` instead, and let
# the condition be anything up to it.
_OUTER_CASE_TS_FUNC = re.compile(
    r"CASE\(.+,\s*(?:RATE|IRATE|INCREASE|DELTA|DERIV)\([^)]+\),\s*NULL\)"
)


def _rewrite_ts_inner_case_to_outer_case(assignments: list[str]) -> list[str]:
    """Rewrite ``FUNC(CASE(cond, field, NULL), window)`` → ``CASE(cond, FUNC(...), NULL)``.

    Inner-CASE value args ClassCast on current Elasticsearch (``ReferenceAttribute``
    → ``Bucket``) for RATE/IRATE/INCREASE. Outer CASE around the time-series call
    is legal and preserves the filter semantics used for join-ratio / per-operand
    label filters.
    """

    def _repl(match: re.Match[str]) -> str:
        return (
            f"CASE({match.group('cond')}, "
            f"{match.group('func')}({match.group('field')}, {match.group('window')}), "
            f"NULL)"
        )

    return [_TS_INNER_CASE_VALUE_ARG.sub(_repl, assignment) for assignment in assignments]


def _wrap_bare_ts_value_args_when_case_siblings(assignments: list[str]) -> list[str]:
    """Normalize fused STATS so CASE-inlined and bare TS value args don't mix.

    1. Rewrite illegal ``IRATE(CASE(cond, field, NULL), w)`` shapes to
       ``CASE(cond, IRATE(field, w), NULL)``.
    2. Elasticsearch can ClassCast (``ReferenceAttribute`` → ``Bucket``) when one
       ``TS ... | STATS`` measure uses a CASE-shaped time-series aggregate and
       another uses a bare ``IRATE(other_metric, …)``.

    Two CASE shapes appear in the translator:

    * Inner (``IRATE(CASE(cond, metric, NULL), …)`` / OVER_TIME): wrap bare
      siblings as ``IRATE(CASE(true, other_metric, NULL), …)``.
    * Outer (``CASE(cond, IRATE(metric, …), NULL)`` — required so ES 9.5 does
      not Bucket-cast when filtering the counter argument of RATE/IRATE): wrap
      bare siblings as ``CASE(true, IRATE(other_metric, …), NULL)`` and leave
      already-outer-CASE measures alone so we do not nest ``CASE(true, …)``
      inside an outer filter CASE.

    Shared by formula-plan fusion (``_build_shared_measure_pipeline``) and the
    pretranslated-query merge path (``_merge_pretranslated_xy_queries``).
    """
    assignments = _rewrite_ts_inner_case_to_outer_case(assignments)
    if not any("CASE(" in assignment for assignment in assignments):
        return assignments

    if any(_OUTER_CASE_TS_FUNC.search(assignment) for assignment in assignments):
        def _wrap_outer(assignment: str) -> str:
            if "CASE(" in assignment:
                return assignment

            def _repl(match: re.Match[str]) -> str:
                return (
                    f"CASE(true, {match.group('func')}({match.group('field')}, "
                    f"{match.group('window')}), NULL)"
                )

            return _BARE_TS_VALUE_ARG.sub(_repl, assignment)

        return [_wrap_outer(assignment) for assignment in assignments]

    def _repl(match: re.Match[str]) -> str:
        func = match.group("func")
        field, window = match.group("field"), match.group("window")
        # The counter range functions reject CASE as their value argument, which
        # is why _rewrite_ts_inner_case_to_outer_case exists. Emitting the inner
        # shape for them here just recreates the form that pass removed, so keep
        # them outer. OVER_TIME genuinely uses the inner shape.
        if func.upper() in {"RATE", "IRATE", "INCREASE", "DELTA", "DERIV"}:
            return f"CASE(true, {func}({field}, {window}), NULL)"
        return f"{func}(CASE(true, {field}, NULL), {window})"

    def _wrap_inner(assignment: str) -> str:
        # Same guard as the outer branch: a measure that already carries a
        # filter CASE must not gain an identity CASE nested inside it. Without
        # this, an outer-shaped measure whose condition the detector above
        # failed to recognise becomes
        # ``SUM(CASE(cond, RATE(CASE(true, field, w), NULL)))`` -- which
        # Elasticsearch rejects, since a time-series function may not take CASE
        # as its value argument.
        if "CASE(" in assignment:
            return assignment
        return _BARE_TS_VALUE_ARG.sub(_repl, assignment)

    return [_wrap_inner(assignment) for assignment in assignments]


def _infer_stats_metric_field(expr: str) -> str:
    """Best-effort metric field from a STATS RHS for mixed-TS normalization."""
    text = (expr or "").strip()
    wrapped = re.fullmatch(
        rf"(?:AVG|SUM|MIN|MAX|COUNT)\(\s*(?:{_TS_AGG_FUNC_PATTERN})\(\s*"
        rf"({_ESQL_FIELD_REFERENCE_PATTERN})\s*,\s*[^)]+\)\s*\)",
        text,
    )
    if wrapped:
        return wrapped.group(1)
    bare_ts = re.fullmatch(
        rf"(?:{_TS_AGG_FUNC_PATTERN})\(\s*({_ESQL_FIELD_REFERENCE_PATTERN})\s*,\s*[^)]+\)",
        text,
    )
    if bare_ts:
        return bare_ts.group(1)
    bare_regular = re.fullmatch(
        rf"(?:AVG|SUM|MIN|MAX|COUNT)\(\s*({_ESQL_FIELD_REFERENCE_PATTERN})\s*\)",
        text,
    )
    if bare_regular:
        return bare_regular.group(1)
    return ""


def _finalize_fused_stats_assignments(
    assignments: list[str],
    *,
    group_fields: list[str] | None = None,
    source_type: str = "TS",
) -> list[str]:
    """Apply mixed-TS normalize then CASE-shape wrap to fused STATS assignments.

    Used by pretranslated multi-target merge and single-query join-ratio emission
    so both stay aligned with ``_build_shared_measure_pipeline``.
    """
    if not assignments:
        return assignments
    if source_type == "TS":
        dims = [g for g in (group_fields or []) if g and g != "time_bucket"]
        specs = []
        alias_order: list[str] = []
        for assignment in assignments:
            if "=" not in assignment:
                continue
            left, right = assignment.split("=", 1)
            alias = left.strip()
            expr = right.strip()
            alias_order.append(alias)
            bare_alias = alias.strip("`")
            specs.append(
                MeasureSpec(
                    source_type="TS",
                    time_filter="",
                    bucket_expr="time_bucket = TBUCKET(5 minute)",
                    group_fields=list(dims),
                    filters=[],
                    alias=bare_alias,
                    stats_expr=expr,
                    final_alias=bare_alias,
                    metric_field=_infer_stats_metric_field(expr),
                )
            )
        if specs:
            specs = _normalize_mixed_ts_stats_exprs(specs)
            assignments = [
                f"{alias} = {spec.stats_expr}"
                for alias, spec in zip(alias_order, specs)
            ]
    return _wrap_bare_ts_value_args_when_case_siblings(assignments)


_OUTER_TO_TS_AGG = {
    "AVG": "AVG_OVER_TIME",
    "SUM": "SUM_OVER_TIME",
    "MIN": "MIN_OVER_TIME",
    "MAX": "MAX_OVER_TIME",
    "COUNT": "COUNT_OVER_TIME",
}
# When wrapping a bare cross-series aggregate into a time-series form so it can
# share a STATS with OVER_TIME siblings, SUM must NOT become SUM_OVER_TIME —
# that sums every sample in the window and inflates gauges (requests/limits).
# LAST_OVER_TIME keeps one value per series per bucket, matching PromQL's
# instant-vector sum across series.
_OUTER_TO_SAFE_TS_INNER = {
    "AVG": "AVG_OVER_TIME",
    "SUM": "LAST_OVER_TIME",
    "MIN": "MIN_OVER_TIME",
    "MAX": "MAX_OVER_TIME",
    "COUNT": "COUNT_OVER_TIME",
}
_TS_TO_OUTER_AGG = {ts: outer for outer, ts in _OUTER_TO_TS_AGG.items()}
# LAST_OVER_TIME is used as the safe SUM wrap; map it back to SUM for wrapping.
_TS_TO_OUTER_AGG.setdefault("LAST_OVER_TIME", "SUM")
_TS_AGG_FUNC_PATTERN = r"(?:RATE|IRATE|INCREASE|AVG_OVER_TIME|SUM_OVER_TIME|MIN_OVER_TIME|MAX_OVER_TIME|COUNT_OVER_TIME|LAST_OVER_TIME|PRESENT_OVER_TIME)"


def _normalize_mixed_ts_stats_exprs(specs):
    """Keep a single TS ``STATS`` internally consistent.

    Elasticsearch rejects a ``TS ... | STATS`` that mixes a *time-series*
    aggregate (``AVG_OVER_TIME(...)`` etc.) with a *regular* aggregate
    (``AVG(...)`` / ``AVG(AVG_OVER_TIME(...))``) in the same
    ``TimeSeriesAggregate`` ("Cannot mix time-series aggregate ... and regular
    aggregate ..."). Two valid shapes exist, and which one is legal depends on
    the grouping:

    * **Bare** time-series aggregate — ``AVG_OVER_TIME(field, w)`` — is only
      legal when the ``STATS`` groups solely by the time bucket. With any extra
      grouping dimension ES requires a regular aggregate.
    * **Wrapped** regular aggregate — ``AVG(AVG_OVER_TIME(field, w))`` — is
      legal for *both* time-bucket-only and time-bucket-plus-dimensions
      grouping.

    Single-target translation already follows this invariant. The multi-target
    merge path, however, can leave one target bare (an instant-selector target
    such as ``process_resident_memory_max_bytes{...}`` → ``AVG_OVER_TIME(...)``)
    while its ``irate(...)`` siblings become ``AVG(AVG_OVER_TIME(...))``. That
    mix passed offline but 400s at runtime.

    Normalization rule:

    * If the panel groups by extra dimensions, **or** any term is already a
      wrapped regular aggregate, every TS-bearing term is rendered in the
      universally-valid wrapped form (bare ``X_OVER_TIME`` gets an outer
      aggregate; a bare regular ``AGG(field)`` gets an ``X_OVER_TIME`` inner).
    * Otherwise (time-bucket-only grouping, no wrapped terms) the historical
      bare-TS form is preserved so single-metric / time-bucket-only queries are
      unchanged.
    """
    if not specs or specs[0].source_type != "TS":
        return specs
    if not any(re.search(rf"\b{_TS_AGG_FUNC_PATTERN}\(", spec.stats_expr or "") for spec in specs):
        return specs
    window = _timeseries_stats_window(specs)

    has_extra_group_dims = any(spec.group_fields for spec in specs)
    has_wrapped_regular_ts = any(
        re.search(rf"\b(AVG|SUM|MIN|MAX|COUNT)\(\s*{_TS_AGG_FUNC_PATTERN}\(", spec.stats_expr or "")
        for spec in specs
    )
    # When grouping by extra dimensions, or when at least one term is already a
    # regular aggregate, the only universally-valid shape is the wrapped form.
    prefer_wrapped = has_extra_group_dims or has_wrapped_regular_ts

    normalized = []
    for spec in specs:
        expr = (spec.stats_expr or "").strip()
        metric_field = str(spec.metric_field or "").strip()
        if not metric_field:
            normalized.append(spec)
            continue

        bare_regular = re.fullmatch(
            rf"(AVG|SUM|MIN|MAX|COUNT)\(\s*{re.escape(metric_field)}\s*\)",
            expr,
        )
        bare_ts = re.fullmatch(
            rf"({_TS_AGG_FUNC_PATTERN})\(\s*{re.escape(metric_field)}\s*,\s*([^)]+)\)",
            expr,
        )

        if prefer_wrapped:
            # Target the wrapped ``OUTER(TS_FUNC(field, w))`` form.
            if bare_ts:
                ts_func = bare_ts.group(1)
                ts_window = bare_ts.group(2).strip()
                outer = _TS_TO_OUTER_AGG.get(ts_func, "AVG")
                new_expr = f"{outer}({ts_func}({metric_field}, {ts_window}))"
                warning = (
                    f"Wrapped {ts_func}({metric_field}, {ts_window}) in {outer}(...) so "
                    f"the grouped TS panel target validates (no bare time-series "
                    f"aggregate mixed with regular aggregates)"
                )
            elif bare_regular:
                outer = bare_regular.group(1)
                ts_func = _OUTER_TO_SAFE_TS_INNER[outer]
                new_expr = f"{outer}({ts_func}({metric_field}, {window}))"
                warning = (
                    f"Converted {outer}({metric_field}) to "
                    f"{outer}({ts_func}({metric_field}, {window})) so the grouped "
                    f"TS panel target validates"
                )
            else:
                normalized.append(spec)
                continue
        else:
            # Time-bucket-only grouping and no wrapped terms: keep the historical
            # bare time-series form. Convert a bare regular aggregate to the bare
            # TS aggregate so it lines up with the other bare TS terms.
            if not bare_regular:
                normalized.append(spec)
                continue
            outer = bare_regular.group(1)
            ts_func = _OUTER_TO_SAFE_TS_INNER[outer]
            new_expr = f"{ts_func}({metric_field}, {window})"
            warning = (
                f"Converted {outer}({metric_field}) to "
                f"{ts_func}({metric_field}, {window}) so mixed TS panel targets validate"
            )

        warnings = list(spec.warnings)
        if warning not in warnings:
            warnings.append(warning)
        normalized.append(
            dataclasses.replace(spec, stats_expr=new_expr, warnings=warnings)
        )
    return normalized


def _try_rewrite_set_or_same_metric(
    frag,
    resolver,
    rule_pack,
    alias_hint="",
    summary_mode=False,
    preferred_group_labels=None,
    allow_direct_ts_gauge=False,
    preferred_group_labels_origin=None,
):
    """Rewrite ``A{f1} or A{f2}`` (and longer chains of same-metric ``or``)
    as a single MeasureSpec whose filters union the operands' matchers.

    This is the one set-operator case that has an honest single-stage
    ES|QL equivalent: PromQL's ``or`` of same-metric instant vectors is
    set union over distinct matcher tuples, which is the same as a
    single fetch of the metric with an ``OR`` over the matcher filter
    sets. Each operand must:

    - Be a leaf metric reference (``simple_metric`` family) — no inner
      rate/aggregation, since rate over a unioned filter still produces
      the right rate per resulting series.
    - Reference the **same** metric name on both sides.
    - Resolve to the **same** non-filter matcher structure (same
      grouping labels, same range/agg shape if any).

    For anything else we return ``None`` so the caller refuses the
    translation.
    """
    op_lower = (frag.binary_op or "").lower()
    if op_lower != "or":
        return None

    left_frag = frag.extra.get("left_frag")
    right_frag = frag.extra.get("right_frag")
    if not left_frag or not right_frag:
        return None

    # A bare ``or`` matches on the full label set, so two operands with
    # disjoint matchers never collide and their union is exactly the OR of
    # their filters. A modifier that *narrows* the match key — ``on(...)`` /
    # ``ignoring(...)`` with labels, or a label-less ``on()`` (matches on the
    # empty set) — makes PromQL suppress a right-hand series wherever the left
    # shares the matched labels, even when a differing label (e.g. ``status``)
    # makes them logically distinct. A flat WHERE-OR keeps both and
    # over-includes the suppressed rows, so it is no longer exact (issue #252
    # review). Reuse the shared predicate so a non-narrowing modifier such as a
    # label-less ``ignoring()`` (equivalent to the full-label-set match) still
    # takes the exact rewrite; it also walks the whole ``or`` chain.
    if _or_chain_has_vector_matching(frag):
        return None

    # Recurse first into a left-leaning ``or`` chain so ``A or A or A``
    # works.
    operand_frags = []
    for child in (left_frag, right_frag):
        if child.family == "binary_expr" and (child.binary_op or "").lower() == "or":
            sub = _try_rewrite_set_or_same_metric(
                child,
                resolver,
                rule_pack,
                alias_hint=alias_hint,
                summary_mode=summary_mode,
                preferred_group_labels=preferred_group_labels,
                preferred_group_labels_origin=preferred_group_labels_origin,
            )
            if sub is None:
                return None
            if len(sub.specs) != 1:
                return None
            # Re-extract the operand fragments out of the nested ``or``
            # chain so the unified filter logic below sees a flat list.
            stack = [child]
            while stack:
                cur = stack.pop()
                if cur.family == "binary_expr" and (cur.binary_op or "").lower() == "or":
                    stack.append(cur.extra.get("left_frag"))
                    stack.append(cur.extra.get("right_frag"))
                else:
                    operand_frags.append(cur)
        else:
            operand_frags.append(child)

    if not operand_frags:
        return None

    # All operands must be simple metric references against the same
    # metric. Range functions, outer aggregations, joins etc. all have
    # set-union semantics that differ from a plain matcher OR.
    metrics = {f.metric for f in operand_frags}
    if len(metrics) != 1 or "" in metrics:
        return None
    if any(f.family != "simple_metric" for f in operand_frags):
        return None
    if any(f.binary_op for f in operand_frags):
        return None
    if any(f.outer_agg or f.range_func for f in operand_frags):
        return None

    # Build a single MeasureSpec from the first operand, then OR-fold
    # the other operands' filter strings into its WHERE clause.
    base = _build_measure_spec(
        operand_frags[0],
        resolver,
        rule_pack,
        alias_hint=alias_hint,
        summary_mode=summary_mode,
        preferred_group_labels=preferred_group_labels,
        allow_direct_ts_gauge=allow_direct_ts_gauge,
        preferred_group_labels_origin=preferred_group_labels_origin,
    )
    if base is None:
        return None

    per_operand_filters = [list(base.filters)]
    for other in operand_frags[1:]:
        spec = _build_measure_spec(
            other,
            resolver,
            rule_pack,
            alias_hint=alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
            allow_direct_ts_gauge=allow_direct_ts_gauge,
            preferred_group_labels_origin=preferred_group_labels_origin,
        )
        if spec is None:
            return None
        # The non-filter parts of each MeasureSpec (source_type, stats,
        # grouping) must agree for the union to be safe.
        if (
            spec.source_type != base.source_type
            or spec.stats_expr != base.stats_expr
            or spec.bucket_expr != base.bucket_expr
            or spec.group_fields != base.group_fields
        ):
            return None
        per_operand_filters.append(list(spec.filters))

    # Compute the AND-intersection of filter clauses that are identical
    # across every operand (those become unconditional WHERE clauses).
    # The remaining per-operand filter clauses are OR'd together inside
    # a single combined WHERE.
    if all(filt_list == per_operand_filters[0] for filt_list in per_operand_filters):
        unified_filters = per_operand_filters[0]
    else:
        common = []
        for filt in per_operand_filters[0]:
            if all(filt in other for other in per_operand_filters[1:]):
                common.append(filt)
        remainders = []
        for filt_list in per_operand_filters:
            rest = [f for f in filt_list if f not in common]
            if not rest:
                # An operand with no distinguishing filter means "match
                # everything"; the union is therefore unfiltered.
                remainders = []
                break
            if len(rest) == 1:
                remainders.append(rest[0])
            else:
                remainders.append("(" + " AND ".join(rest) + ")")
        if remainders:
            common.append("(" + " OR ".join(remainders) + ")")
        unified_filters = common

    # The labels that differ across operands (e.g. ``status`` in
    # ``A{status=~"4.."} or A{status=~"5.."}``) are the dimensions
    # PromQL's set-or uses to keep operand series separate. The
    # straightforward unified WHERE we just built would otherwise
    # average them together and lose the operands' distinguishing
    # dimensions. Promote any such labels to additional BY columns so
    # the rate is computed per-(method, path, status, …) tuple, which
    # matches PromQL's per-series output. Labels that the resolver
    # cannot map to a known field are skipped.
    distinguishing_labels = _set_or_distinguishing_labels(operand_frags)
    if distinguishing_labels:
        resolved = []
        for label in distinguishing_labels:
            field = resolver.resolve_label(label) if resolver else label
            if field and field not in base.group_fields and field not in resolved:
                resolved.append(field)
        if resolved:
            new_group_fields = list(base.group_fields) + resolved
        else:
            new_group_fields = base.group_fields
    else:
        new_group_fields = base.group_fields

    new_spec = dataclasses.replace(
        base,
        filters=unified_filters,
        group_fields=new_group_fields,
        warnings=list(base.warnings)
        + [
            "Rewrote PromQL set-or between same metric as a unified WHERE OR clause"
        ],
    )
    return FormulaPlan(
        specs=[new_spec],
        expr=new_spec.final_alias,
        warnings=list(new_spec.warnings),
        set_or_where=True,
    )


def _set_or_distinguishing_labels(operand_frags):
    """Return the matcher labels that differ across operands of an
    ``A{...} or A{...}`` (or longer chain) so the rewrite can promote
    them to BY columns and preserve the union's distinguishing dimensions.
    """
    by_label_values = {}
    for frag in operand_frags:
        for matcher in frag.matchers:
            label = matcher.get("label")
            if not label:
                continue
            by_label_values.setdefault(label, set()).add(
                (matcher.get("op", "="), matcher.get("value", ""))
            )
    return [label for label, values in by_label_values.items() if len(values) > 1]


def _flatten_or_operands(frag):
    """Flatten a left-leaning ``A or B or C`` chain into ``[A, B, C]``.

    PromQL ``or`` is left-associative, so the operands are returned in
    source order — left to right — which is the precedence order
    ``COALESCE`` must preserve. Returns ``None`` if the chain is malformed.
    """
    if frag.family == "binary_expr" and (frag.binary_op or "").lower() == "or":
        left = frag.extra.get("left_frag")
        right = frag.extra.get("right_frag")
        if left is None or right is None:
            return None
        left_ops = _flatten_or_operands(left)
        right_ops = _flatten_or_operands(right)
        if left_ops is None or right_ops is None:
            return None
        return left_ops + right_ops
    return [frag]


def _matcher_identity(frag) -> frozenset[tuple[str, str, str]]:
    """Stable matcher identity for comparing OR operands."""
    out: set[tuple[str, str, str]] = set()
    for matcher in frag.matchers or []:
        label = str(matcher.get("label") or "")
        if not label:
            continue
        out.add((label, str(matcher.get("op") or "="), str(matcher.get("value") or "")))
    return frozenset(out)


def _scalar_identity(frag) -> str | None:
    if frag is None or frag.family != "scalar":
        return None
    if frag.binary_rhs is not None:
        return str(frag.binary_rhs)
    return str(frag.extra.get("scalar_value") or frag.metric or "")


def _range_fallback_identity(frag) -> tuple | None:
    """Structural identity that ignores ``range_func`` / ``range_window``.

    Used to detect the Grafana ``rate(M[$interval]) or irate(M[5m])`` (and
    ``max_over_time(M[$interval]) or max_over_time(M[5m])``) idiom: same
    metric + matchers + wrapper shape, differing only in the range window
    or the rate-vs-irate choice. Returns ``None`` when the fragment is too
    complex to treat as a range-fallback operand.
    """
    if frag is None:
        return None
    if frag.family == "binary_expr":
        op = (frag.binary_op or "").lower()
        if op in _SET_OPERATORS:
            return None
        left = frag.extra.get("left_frag")
        right = frag.extra.get("right_frag")
        left_scalar = _scalar_identity(left)
        right_scalar = _scalar_identity(right)
        if right_scalar is not None and left is not None:
            inner = _range_fallback_identity(left)
            if inner is None:
                return None
            return ("binop", op, inner, ("scalar", right_scalar))
        if left_scalar is not None and right is not None:
            inner = _range_fallback_identity(right)
            if inner is None:
                return None
            return ("binop", op, ("scalar", left_scalar), inner)
        return None
    if frag.family == "topk":
        if not frag.metric:
            return None
        post = frag.extra.get("post_filter") or {}
        post_key = (
            str(post.get("op") or ""),
            str(post.get("value") if post.get("value") is not None else ""),
        )
        return (
            "topk",
            int(frag.extra.get("topk_limit") or 0),
            post_key,
            str(frag.metric),
            _matcher_identity(frag),
            str(frag.outer_agg or ""),
        )
    if frag.family in {"range_agg", "simple_agg", "simple_metric"}:
        if not frag.metric:
            return None
        return (
            frag.family,
            str(frag.metric),
            _matcher_identity(frag),
            str(frag.outer_agg or ""),
        )
    return None


def _operands_are_same_metric_range_fallback(operands: list) -> bool:
    """True when OR operands are the Grafana same-metric range-window fallback idiom."""
    if len(operands) < 2:
        return False
    identities = [_range_fallback_identity(op) for op in operands]
    if any(identity is None for identity in identities):
        return False
    if len({identity for identity in identities}) != 1:
        return False
    # Require that at least one operand actually carries a range function /
    # window so we don't steal the plain same-metric WHERE-OR case
    # (``A{f1} or A{f2}``) — those differ in matchers and already fail the
    # identity check above, but also refuse when every operand is a bare
    # metric with no range shape (cross-metric COALESCE should own that).
    if not any(getattr(op, "range_func", None) or getattr(op, "range_window", None) for op in operands):
        # topk / scaled wrappers store range on the fragment itself for
        # range_agg children; walk one level.
        def _has_range(op) -> bool:
            if getattr(op, "range_func", None) or getattr(op, "range_window", None):
                return True
            for key in ("left_frag", "right_frag", "inner_frag"):
                child = (op.extra or {}).get(key)
                if child is not None and _has_range(child):
                    return True
            return False

        if not any(_has_range(op) for op in operands):
            return False
    # Prefer this rewrite only when the operands are *not* identical — if
    # they are byte-identical after macro expansion there is nothing to
    # fall back from, but translating the left is still correct. Always OK.
    return True


def _left_operand_of_same_metric_range_fallback(frag):
    """Return the preferred left operand of a same-metric range-fallback ``or``.

    Detects the Grafana ``rate(M[$interval]) or irate(M[5m])`` (and
    ``topk(rate) or topk(irate)``, ``rate*N or irate*N``) idiom. Returns
    ``None`` when the fragment is not that pattern. Callers that can
    translate the left via ``_build_formula_plan`` or a dedicated family
    rule (e.g. ``topk``) should prefer that operand and warn that the
    short-window fallback was dropped.
    """
    if frag is None or frag.family != "binary_expr":
        return None
    if (frag.binary_op or "").lower() != "or":
        return None
    if _or_chain_has_vector_matching(frag):
        return None
    operands = _flatten_or_operands(frag)
    if not operands or not _operands_are_same_metric_range_fallback(operands):
        return None
    return operands[0]


def _same_metric_range_fallback_warning(frag) -> str:
    """Warning text for preferring the left operand of a range-fallback ``or``."""
    operands = _flatten_or_operands(frag) if frag is not None else []
    left = operands[0] if operands else None
    left_range = (left.range_func if left is not None else None) or ""
    right_ranges = sorted(
        {
            str(getattr(op, "range_func", None) or "")
            for op in operands[1:]
            if getattr(op, "range_func", None)
        }
    )
    if left_range and right_ranges and any(r != left_range for r in right_ranges):
        return (
            f"PromQL same-metric 'or': preferred left '{left_range}(...)' and "
            f"dropped short-window fallback {', '.join(repr(r + '(...)') for r in right_ranges)}; "
            "Grafana uses the right side only when the left lacks samples"
        )
    return (
        "PromQL same-metric 'or': preferred left range-window operand and "
        "dropped the alternate-window fallback; Grafana uses the right "
        "side only when the left lacks samples"
    )


def _is_zero_scaled_operand(frag) -> bool:
    """True for ``0 * X`` / ``X * 0`` (including ``scaled_agg`` with scalar 0)."""
    if frag is None:
        return False
    if frag.family == "scaled_agg":
        rhs = frag.binary_rhs
        return bool(rhs is not None and rhs.is_scalar and rhs.scalar_value == 0.0)
    if frag.family == "binary_expr" and (frag.binary_op or "") == "*":
        left = frag.extra.get("left_frag")
        right = frag.extra.get("right_frag")
        for side in (left, right):
            if side is not None and side.is_scalar and side.scalar_value == 0.0:
                return True
    return False


def _mixed_os_or_operands(frag):
    """Return ``(windows_side, zero_side)`` for a mixed-OS ``or`` zero-fill.

    Accepts both shapes used by Kubernetes Views Global:

    * bare ``windows_join or 0*linux`` / ``windows_join or metric*0``
    * ``sum(windows_join or metric*0) by (namespace)`` — the community
      dashboard nests the ``OR`` *inside* the outer aggregation, so the
      right operand of ``+`` is an ``unknown``/agg fragment whose
      ``inner_frag`` is the ``or``.
    """
    if frag is None:
        return None
    or_frag = frag
    if frag.family != "binary_expr" or (frag.binary_op or "").lower() != "or":
        # Nested: sum(... OR ...) by (...) lands as unknown/agg with inner_frag.
        inner = (frag.extra or {}).get("inner_frag")
        if (
            inner is not None
            and inner.family == "binary_expr"
            and (inner.binary_op or "").lower() == "or"
            and (frag.outer_agg or frag.family in {"unknown", "nested_agg", "simple_agg"})
        ):
            or_frag = inner
        else:
            return None
    win_side = or_frag.extra.get("left_frag")
    zero_side = or_frag.extra.get("right_frag")
    if win_side is None or zero_side is None:
        return None
    if not _is_zero_scaled_operand(zero_side):
        if _is_zero_scaled_operand(win_side):
            win_side, zero_side = zero_side, win_side
        else:
            return None
    win_blocked = bool(
        (win_side.extra or {}).get("not_feasible_reasons")
        or _contains_join_frag(win_side)
        or win_side.family in {"join", "unknown"}
    )
    if not win_blocked:
        return None
    return win_side, zero_side


def _try_rewrite_mixed_os_zero_fill_plus(
    frag,
    resolver,
    rule_pack,
    alias_hint="",
    summary_mode=False,
    preferred_group_labels=None,
    preferred_group_labels_origin=None,
    allow_direct_ts_gauge=False,
    allow_tsds_gauge_promotion=False,
):
    """Prefer the Linux left of ``linux + on(ns) (windows_join or zero_fill)``.

    Kubernetes Views Global (and similar mixed-OS dashboards) add a Windows
    contribution that is itself a vector-matching join, then ``or`` a
    zero-fill (``0 * linux`` or ``kube_namespace_created * 0``) so namespaces
    without Windows still appear. The community form nests that ``OR`` inside
    ``sum(...) by (namespace)``. The join cannot be expressed in ES|QL; keeping
    only the Linux left is correct for Linux-only clusters and an honest
    degradation (with warning) for mixed clusters — better than marking the
    whole panel not_feasible.
    """
    op = (frag.binary_op or "").lower()
    if op not in {"+", "-"}:
        return None
    left = frag.extra.get("left_frag")
    right = frag.extra.get("right_frag")
    if left is None or right is None:
        return None
    if _mixed_os_or_operands(right) is None:
        return None

    plan = _build_formula_plan(
        left,
        resolver,
        rule_pack,
        alias_hint=alias_hint,
        summary_mode=summary_mode,
        preferred_group_labels=preferred_group_labels,
        allow_direct_ts_gauge=allow_direct_ts_gauge,
        preferred_group_labels_origin=preferred_group_labels_origin,
        allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
    )
    if plan is None:
        return None
    detail = (
        "PromQL mixed-OS '+ on(...) (windows_join or zero_fill)' : "
        "preferred left (Linux) operand and dropped the Windows join contribution; "
        "Windows namespaces will under-report until the join is redesigned"
    )
    if detail not in plan.warnings:
        plan.warnings.append(detail)
    return plan


def _try_rewrite_set_or_same_metric_range_fallback(
    frag,
    resolver,
    rule_pack,
    alias_hint="",
    summary_mode=False,
    preferred_group_labels=None,
    preferred_group_labels_origin=None,
    allow_direct_ts_gauge=False,
    allow_tsds_gauge_promotion=False,
):
    """Prefer the left operand of ``rate(M[$i]) or irate(M[5m])``-style ORs.

    Grafana community dashboards (MySQL/Percona, node vmstat panels, …)
    commonly write ``rate(M[$interval]) or irate(M[5m])`` so a short
    ``irate`` fills in when the dashboard interval is too long for
    ``rate`` to have two samples. Both sides share the metric and
    matchers; only the range function / window differs. The same-metric
    WHERE-OR rewrite refuses range functions, and the cross-metric
    COALESCE rewrite requires distinct metric names — so without this
    bridge the panel was marked ``not_feasible`` even though the left
    operand alone is an honest, high-fidelity translation.

    Prefer the left operand (dashboard-interval ``rate`` / ``max_over_time``)
    and warn that the short-window fallback was dropped.

    Returns a ``FormulaPlan`` when the left operand is formula-planable.
    Families handled outside ``_build_formula_plan`` (notably ``topk``)
    are re-dispatched by ``binary_expr_family_rule`` via
    ``_left_operand_of_same_metric_range_fallback``.
    """
    left = _left_operand_of_same_metric_range_fallback(frag)
    if left is None:
        return None

    plan = _build_formula_plan(
        left,
        resolver,
        rule_pack,
        alias_hint=alias_hint,
        summary_mode=summary_mode,
        preferred_group_labels=preferred_group_labels,
        allow_direct_ts_gauge=allow_direct_ts_gauge,
        preferred_group_labels_origin=preferred_group_labels_origin,
        allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
    )
    if plan is None:
        return None

    detail = _same_metric_range_fallback_warning(frag)
    if detail not in plan.warnings:
        plan.warnings.append(detail)
    return plan


def _vector_matching_restricts_or(matching):
    """Return True when an ``or`` modifier narrows the series-matching key.

    The AST parser attaches a ``vector_matching`` entry to every set-operator
    node, even a bare ``or`` with no modifier (``type=''``, no labels) and a
    label-less ``ignoring()`` (``type='Exclude'``, no labels) — both of which
    mean "match on the full label set", exactly what the ``COALESCE`` union's
    full-grouping identity already does. Only an explicit key narrows matching:
    any ``on(...)``/``ignoring(...)`` with labels, or an ``on()`` (type
    ``Include``) that matches on the empty set.
    """
    if not matching:
        return False
    if matching.get("labels"):
        return True
    return matching.get("type") == "Include"


def _or_chain_has_vector_matching(frag):
    """Return True if any ``or`` node in the chain carries an ``on()``/``ignoring()`` key.

    PromQL ``or`` with a vector-matching modifier matches the operands by the
    modifier's label key — not by the full series label set — when deciding
    which right-operand series fill a missing left-operand series. The
    ``COALESCE`` union rewrite uses the emitted ES|QL grouping fields as the
    series identity and has no way to honor that narrower key, so a right
    series with the same matched labels but a differing unmatched label would
    survive when PromQL would suppress it. Refuse the rewrite in that case so
    the panel is flagged for manual review instead of over-reporting series.
    """
    if frag.family == "binary_expr" and (frag.binary_op or "").lower() == "or":
        if _vector_matching_restricts_or(frag.extra.get("vector_matching")):
            return True
        left = frag.extra.get("left_frag")
        right = frag.extra.get("right_frag")
        return bool(
            (left is not None and _or_chain_has_vector_matching(left))
            or (right is not None and _or_chain_has_vector_matching(right))
        )
    return False


def _try_rewrite_set_or_cross_metric(
    frag,
    resolver,
    rule_pack,
    alias_hint="",
    summary_mode=False,
    preferred_group_labels=None,
    preferred_group_labels_origin=None,
):
    """Rewrite a cross-metric ``A or B`` as a ``COALESCE(A, B)`` union.

    PromQL's ``or`` is set union with **left precedence**: every series the
    left operand produces survives unchanged, and a right-operand series fills
    in only where the left has no series with that label set. When both
    operands line up to the same ES|QL grouping (same source command, time
    bucket and group fields), that is exactly ``COALESCE(left, right)`` grouped
    by that label set — the left value wins inside any group it produced and
    the right value fills the groups the left never produced. Longer chains
    ``A or B or C`` collapse to ``COALESCE(A, B, C)`` in source order.

    This keeps **both** metrics instead of silently dropping the right operand
    (issue #167). When the operands cannot be aligned safely — different
    grouping dimensions, divergent source commands, or an operand that itself
    has no honest translation — we return ``None`` so the caller marks the
    panel for manual review rather than emitting half the data.
    """
    if (frag.binary_op or "").lower() != "or":
        return None

    operand_frags = _flatten_or_operands(frag)
    if not operand_frags or len(operand_frags) < 2:
        return None

    # An ``on(...)``/``ignoring(...)`` modifier changes which right-operand
    # series fill a missing left-operand series — PromQL matches by the
    # modifier's key, not the full label set. The COALESCE union groups by the
    # emitted ES|QL fields and cannot honor that key, so refuse the rewrite
    # (→ manual review) rather than over-report right-operand series.
    if _or_chain_has_vector_matching(frag):
        return None

    # Every operand must be translatable on its own. A nested set operator or a
    # carried not-feasible reason means we cannot faithfully include that side,
    # so refuse the whole union (→ manual review) instead of dropping it.
    for operand in operand_frags:
        if not operand.metric:
            return None
        if operand.extra.get("not_feasible_reasons"):
            return None
        if operand.family == "binary_expr" and (operand.binary_op or "").lower() in _SET_OPERATORS:
            return None

    # A pure same-metric ``or`` is handled by the dedicated WHERE-OR rewrite,
    # which keeps a single fetch; only fall through to the union when the
    # operands span more than one metric.
    if len({operand.metric for operand in operand_frags}) < 2:
        return None

    specs = []
    for operand in operand_frags:
        # ``allow_direct_ts_gauge=False`` forces a gauge to an aggregatable
        # ``AVG(field)`` (rather than a bare ``field``) so multiple operands can
        # share one ``STATS`` and be CASE-wrapped if their filters diverge.
        spec = _build_measure_spec(
            operand,
            resolver,
            rule_pack,
            alias_hint=alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
            allow_direct_ts_gauge=False,
            preferred_group_labels_origin=preferred_group_labels_origin,
        )
        if spec is None:
            return None
        specs.append(spec)

    # The operands must align to one shared pipeline (same source/bucket/group
    # fields). When they don't, there is no safe ES|QL union — refuse so the
    # panel is flagged for manual review.
    if not _measure_specs_mergeable(specs):
        return None

    coalesce_args = ", ".join(_esql_identifier(spec.final_alias) for spec in specs)
    expr = f"COALESCE({coalesce_args})"

    warnings = []
    for spec in specs:
        for w in spec.warnings:
            if w not in warnings:
                warnings.append(w)
    note = (
        "PromQL 'or': kept both metrics — left operand takes precedence and the "
        "right operand fills series where the left has no data"
    )
    if note not in warnings:
        warnings.append(note)

    return FormulaPlan(specs=specs, expr=expr, warnings=warnings, set_or_fill=True)


def _build_formula_plan(
    frag,
    resolver,
    rule_pack,
    alias_hint="",
    summary_mode=False,
    preferred_group_labels=None,
    allow_direct_ts_gauge=True,
    preferred_group_labels_origin=None,
    allow_tsds_gauge_promotion=True,
    drop_legend_labels=True,
):
    scalar_expr = _scalar_fragment_expr(frag)
    if scalar_expr is not None:
        return FormulaPlan(specs=[], expr=scalar_expr)

    # An outer aggregation wrapped around a group_left/group_right vector-matching
    # join (e.g. sum(rate(A) * on(ns,pod) group_left(w,wt) B) by (pod)) produces
    # family='unknown' with extra['vector_matching'] set and the join RHS on
    # binary_rhs.  _build_measure_spec has no 'unknown' handler and returns None,
    # blocking multi-target fusion and ratio expressions.  Strip the join RHS and
    # re-classify as the appropriate aggregate family so the primary metric can
    # participate in both.  Label enrichment from the join is silently dropped —
    # identical to what join_family_rule does in translate.py.
    if (
        frag
        and frag.family == "unknown"
        and frag.extra.get("vector_matching")
        and frag.binary_op == "*"
    ):
        stripped_fields = {f.name: getattr(frag, f.name) for f in dataclasses.fields(frag)}
        if frag.range_func:
            stripped_fields["family"] = "range_agg"
        elif frag.outer_agg:
            stripped_fields["family"] = "simple_agg"
        else:
            stripped_fields["family"] = "simple_metric"
        stripped_fields["binary_op"] = ""
        stripped_fields["binary_rhs"] = None
        stripped = PromQLFragment(**stripped_fields)
        plan = _build_formula_plan(
            stripped,
            resolver,
            rule_pack,
            alias_hint=alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
            allow_direct_ts_gauge=allow_direct_ts_gauge,
            preferred_group_labels_origin=preferred_group_labels_origin,
            allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
        )
        if plan and "Dropped group_left label enrichment" not in (plan.warnings or []):
            plan.warnings.append("Dropped group_left label enrichment; kept primary metric series only")
        return plan

    # A bare group_left/group_right vector-matching join without an outer
    # aggregation (e.g. ``A * on(chip) group_left(chip_name) B``) lands as
    # family='join' in the fragment parser.  _build_measure_spec has no
    # 'join' handler and returns None, blocking multi-target fusion.  Strip
    # the join RHS — identical to what join_family_rule does in translate.py
    # for the ``binary_op == '*'`` branch — and delegate to the left_frag
    # which already carries the correct metric and family.  Use join_labels
    # as the fallback preferred group fields so the resulting spec retains the
    # label enrichment fields (e.g. chip_name) that the join was providing.
    if (
        frag
        and frag.family == "join"
        and frag.binary_op == "*"
        and frag.extra.get("left_frag")
    ):
        left_frag = frag.extra["left_frag"]
        join_labels = frag.extra.get("join_labels", []) or []
        matching = frag.extra.get("vector_matching") or {}
        # group_left enrichment label(s) (e.g. chip_name) live on the same data
        # stream as the primary metric, so fold them into the grouping instead
        # of dropping them (issue #156). group_right flips the primary side and
        # keeps the legacy strip-and-warn behavior.
        enrichment_labels = (
            list(frag.extra.get("enrichment_labels", []))
            if matching.get("cardinality") == "ManyToOne"
            else []
        )
        base_preferred = list(preferred_group_labels) if preferred_group_labels else list(join_labels)
        for label in enrichment_labels:
            if label and label not in base_preferred:
                base_preferred.append(label)
        effective_preferred = base_preferred or None
        plan = _build_formula_plan(
            left_frag,
            resolver,
            rule_pack,
            alias_hint=alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=effective_preferred,
            allow_direct_ts_gauge=allow_direct_ts_gauge,
            preferred_group_labels_origin=preferred_group_labels_origin,
            allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
        )
        if (
            plan
            and not enrichment_labels
            and "Dropped group_left label enrichment" not in (plan.warnings or [])
        ):
            plan.warnings.append("Dropped group_left label enrichment; kept primary metric series only")
        return plan

    if frag and frag.family == "binary_expr":
        # Set operators (``or`` / ``and`` / ``unless``) are not arithmetic
        # and cannot be composed by interpolating the operands into a single
        # EVAL expression. PromQL's ``or`` is set union, ``and`` is set
        # intersection, ``unless`` is set difference — all preserve the
        # operands' label set and have no honest single-stage ES|QL
        # equivalent. We handle one common, safe rewrite below
        # (``A{f1} or A{f2}`` → ``A WHERE f1 OR f2``) and refuse everything
        # else so the rule layer can mark the panel ``not_feasible``
        # instead of silently dropping one operand or every breakdown
        # label.
        op_lower = (frag.binary_op or "").lower()
        if op_lower in _SET_OPERATORS:
            rewritten = _try_rewrite_set_or_same_metric(
                frag,
                resolver,
                rule_pack,
                alias_hint=alias_hint,
                summary_mode=summary_mode,
                preferred_group_labels=preferred_group_labels,
                allow_direct_ts_gauge=False,
                preferred_group_labels_origin=preferred_group_labels_origin,
            )
            if rewritten is not None:
                return rewritten
            if op_lower == "or":
                # Same-metric OR that only differs by range func / window
                # (``rate(M[$interval]) or irate(M[5m])``,
                # ``max_over_time(M[$i]) or max_over_time(M[5m])``) — prefer
                # the left operand. Neither WHERE-OR (refuses range funcs)
                # nor cross-metric COALESCE (requires distinct metrics) can
                # own this Grafana idiom.
                range_fallback = _try_rewrite_set_or_same_metric_range_fallback(
                    frag,
                    resolver,
                    rule_pack,
                    alias_hint=alias_hint,
                    summary_mode=summary_mode,
                    preferred_group_labels=preferred_group_labels,
                    preferred_group_labels_origin=preferred_group_labels_origin,
                    allow_direct_ts_gauge=allow_direct_ts_gauge,
                    allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
                )
                if range_fallback is not None:
                    return range_fallback
                # Cross-metric ``A or B``: keep both metrics as a COALESCE union
                # (left precedence, right fills the gaps) instead of dropping the
                # right operand (issue #167). Returns ``None`` when the operands
                # cannot be aligned safely so the caller marks it for manual review.
                cross = _try_rewrite_set_or_cross_metric(
                    frag,
                    resolver,
                    rule_pack,
                    alias_hint=alias_hint,
                    summary_mode=summary_mode,
                    preferred_group_labels=preferred_group_labels,
                    preferred_group_labels_origin=preferred_group_labels_origin,
                )
                if cross is not None:
                    return cross
            return None

        if op_lower in ("+", "-"):
            # Kubernetes mixed-OS: ``linux + on(ns) (windows_join or 0*linux)``.
            # Prefer the Linux left when the Windows side is an untranslatable
            # vector-matching join (Views Global CPU/Memory/Network panels).
            mixed_os = _try_rewrite_mixed_os_zero_fill_plus(
                frag,
                resolver,
                rule_pack,
                alias_hint=alias_hint,
                summary_mode=summary_mode,
                preferred_group_labels=preferred_group_labels,
                preferred_group_labels_origin=preferred_group_labels_origin,
                allow_direct_ts_gauge=allow_direct_ts_gauge,
                allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
            )
            if mixed_os is not None:
                return mixed_os

            left_frag_peek = frag.extra.get("left_frag")
            right_frag_peek = frag.extra.get("right_frag")
            phantom_side = None
            real_side = None
            phantom_on_left = False
            if _is_phantom_grafana_var(right_frag_peek):
                phantom_side = right_frag_peek
                real_side = left_frag_peek
            elif _is_phantom_grafana_var(left_frag_peek):
                phantom_side = left_frag_peek
                real_side = right_frag_peek
                phantom_on_left = True
            if phantom_side is not None and real_side is not None:
                plan = _build_formula_plan(
                    real_side,
                    resolver,
                    rule_pack,
                    alias_hint=alias_hint,
                    summary_mode=summary_mode,
                    preferred_group_labels=preferred_group_labels,
                    allow_direct_ts_gauge=allow_direct_ts_gauge,
                    preferred_group_labels_origin=preferred_group_labels_origin,
                    allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
                )
                if plan:
                    var_name = (phantom_side.metric or "").removeprefix("label_") or "var"
                    replacement = "0"
                    expr = (
                        _esql_binary_expr(replacement, frag.binary_op, plan.expr)
                        if phantom_on_left
                        else _esql_binary_expr(plan.expr, frag.binary_op, replacement)
                    )
                    warning = (
                        f"Grafana variable ${var_name} used as scalar arithmetic value "
                        f"was replaced with literal {replacement}"
                    )
                    if warning not in (plan.warnings or []):
                        plan.warnings.append(warning)
                    return FormulaPlan(specs=plan.specs, expr=expr, warnings=list(plan.warnings))

        # When a Grafana variable like ``$trends`` is used as a scalar
        # multiplier/divisor (e.g. ``rate(A) * $trends``), the preprocessor
        # converts it to ``label_trends`` — a bare simple_metric with no
        # matchers or aggregation.  This "phantom metric" can never be queried
        # from ES|QL, so strip it from ``*`` / ``÷`` expressions and emit the
        # remaining operand unchanged.  ``+`` / ``-`` are left alone: adding a
        # phantom metric would change the numeric value.
        if op_lower in ("*", "/"):
            left_frag_peek = frag.extra.get("left_frag")
            right_frag_peek = frag.extra.get("right_frag")
            phantom_side = None
            real_side = None
            if _is_phantom_grafana_var(right_frag_peek):
                phantom_side = right_frag_peek
                real_side = left_frag_peek
            elif op_lower == "*" and _is_phantom_grafana_var(left_frag_peek):
                phantom_side = left_frag_peek
                real_side = right_frag_peek
            if phantom_side is not None and real_side is not None:
                plan = _build_formula_plan(
                    real_side,
                    resolver,
                    rule_pack,
                    alias_hint=alias_hint,
                    summary_mode=summary_mode,
                    preferred_group_labels=preferred_group_labels,
                    allow_direct_ts_gauge=allow_direct_ts_gauge,
                    preferred_group_labels_origin=preferred_group_labels_origin,
                    allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
                )
                if plan:
                    var_name = (phantom_side.metric or "").removeprefix("label_") or "var"
                    if (
                        f"Grafana variable ${var_name} dropped"
                        not in (plan.warnings or [])
                    ):
                        plan.warnings.append(
                            f"Grafana variable ${var_name} used as scalar "
                            f"multiplier/divisor was dropped; chart values unscaled"
                        )
                    return plan

        # A caller-supplied ``alias_hint`` (e.g. a multi-target ``target_ref_id``
        # like "A") is passed as-is to a single measure spec's alias. Passing the
        # *same* hint to both operands here collapses their aliases whenever left
        # and right reference the same metric (e.g. the node_exporter "busy % =
        # per-mode rate / core count" idiom: numerator and denominator both read
        # ``node_cpu_seconds_total``). ``_build_shared_measure_pipeline`` then
        # sees two same-alias specs with different stats_exprs and rejects the
        # whole multi-target fusion as an unresolvable duplicate. Suffix each
        # side so left/right always get distinct aliases.
        left_plan = _build_formula_plan(
            frag.extra.get("left_frag"),
            resolver,
            rule_pack,
            f"{alias_hint}_lhs" if alias_hint else alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
            allow_direct_ts_gauge=False,
            preferred_group_labels_origin=preferred_group_labels_origin,
            allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
            drop_legend_labels=drop_legend_labels,
        )
        right_plan = _build_formula_plan(
            frag.extra.get("right_frag"),
            resolver,
            rule_pack,
            f"{alias_hint}_rhs" if alias_hint else alias_hint,
            summary_mode=summary_mode,
            preferred_group_labels=preferred_group_labels,
            allow_direct_ts_gauge=False,
            preferred_group_labels_origin=preferred_group_labels_origin,
            allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
            drop_legend_labels=drop_legend_labels,
        )
        if not left_plan or not right_plan:
            return None

        # Align groupings when one operand is ungrouped and the other carries
        # an explicit ``by(...)`` (Docker/node Load: ``load / count by(job,
        # instance)(count by(..., cpu)(...))``). PromQL matches on the shared
        # label set; ES|QL needs both measures in the same STATS BY. Rebuild
        # the ungrouped side with the sibling's group fields as preferred
        # labels so the merge succeeds without inventing new dimensions.
        #
        # Skip this when the binary op carries an explicit ``on(...)``/
        # ``ignoring(...)`` key that narrows the match away from full-label
        # equality. That modifier is PromQL's own signal that the operands do
        # NOT share the same label set, so forcing the ungrouped side into
        # ``BY <donor labels>`` can silently aggregate together multiple
        # distinct series (e.g. across an ``instance`` dimension the modifier
        # deliberately ignores) behind whatever aggregate function this
        # fragment happens to use — a real correctness risk, not just an
        # approximation. Fall through to the unmergeable path below instead
        # so a divergent-grouping ratio like ``sum(rate(a)) by (application)
        # / on(application) b`` is marked ``not_feasible`` rather than
        # silently coalesced.
        vector_matching_narrows = _vector_matching_restricts_or(frag.extra.get("vector_matching"))
        left_groups = {tuple(spec.group_fields) for spec in left_plan.specs}
        right_groups = {tuple(spec.group_fields) for spec in right_plan.specs}
        if len(left_groups | right_groups) > 1 and not vector_matching_narrows:
            left_empty = left_groups == {()}
            right_empty = right_groups == {()}
            if left_empty ^ right_empty:
                donor = right_plan if left_empty else left_plan
                needy_frag = (
                    frag.extra.get("left_frag") if left_empty else frag.extra.get("right_frag")
                )
                donor_labels = list(donor.specs[0].group_fields) if donor.specs else []
                if needy_frag is not None and donor_labels:
                    rebuilt = _build_formula_plan(
                        needy_frag,
                        resolver,
                        rule_pack,
                        f"{alias_hint}_lhs" if left_empty and alias_hint else (
                            f"{alias_hint}_rhs" if alias_hint else alias_hint
                        ),
                        summary_mode=summary_mode,
                        preferred_group_labels=donor_labels,
                        allow_direct_ts_gauge=False,
                        preferred_group_labels_origin="sibling_binary",
                        allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
                        drop_legend_labels=False,
                    )
                    if rebuilt is not None:
                        if left_empty:
                            left_plan = rebuilt
                        else:
                            right_plan = rebuilt

        # If one operand was promoted to TS (proven TSDS gauge) but the other
        # stayed on FROM (unknown / non-TSDS), rebuild both with promotion
        # disabled so they share a source command. FROM is the safe common
        # denominator; promoting unknown operands to TS would risk runtime
        # errors on non-TSDS fields. Same-source operands skip this rebuild.
        if allow_tsds_gauge_promotion:
            left_sources = {spec.source_type for spec in left_plan.specs}
            right_sources = {spec.source_type for spec in right_plan.specs}
            all_sources = left_sources | right_sources
            if len(all_sources) > 1:
                return _build_formula_plan(
                    frag,
                    resolver,
                    rule_pack,
                    alias_hint=alias_hint,
                    summary_mode=summary_mode,
                    preferred_group_labels=preferred_group_labels,
                    allow_direct_ts_gauge=allow_direct_ts_gauge,
                    preferred_group_labels_origin=preferred_group_labels_origin,
                    allow_tsds_gauge_promotion=False,
                    drop_legend_labels=drop_legend_labels,
                )

        # Issue #99: the legend-label drop is decided per operand, so a mixed-family
        # formula (e.g. ``max_over_time(...)[5m] + go_goroutines``) can end up with
        # the range_agg side dropping its legend labels (``[]``) while the
        # simple_metric side keeps them (the bare direct-TS-gauge form is disabled
        # in fusion). The mergeability check rejects that divergent grouping and the
        # panel falls to ``not_feasible``. When the operands' groupings disagree only
        # because of the drop, rebuild both with the drop disabled so they share one
        # consistent grouping (the pre-#99 AVG form, feasible). Genuine source-level
        # divergence still falls through to the unmergeable path below.
        if drop_legend_labels:
            group_fields_seen = {
                tuple(spec.group_fields) for spec in left_plan.specs + right_plan.specs
            }
            if len(group_fields_seen) > 1:
                return _build_formula_plan(
                    frag,
                    resolver,
                    rule_pack,
                    alias_hint=alias_hint,
                    summary_mode=summary_mode,
                    preferred_group_labels=preferred_group_labels,
                    allow_direct_ts_gauge=allow_direct_ts_gauge,
                    preferred_group_labels_origin=preferred_group_labels_origin,
                    allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
                    drop_legend_labels=False,
                )
        warnings = []
        for warning in left_plan.warnings + right_plan.warnings:
            if warning not in warnings:
                warnings.append(warning)

        # PromQL ``bool`` comparison: render a numeric 0/1 indicator instead of
        # a boolean, so the result composes with surrounding arithmetic.
        if frag.extra.get("bool_compare"):
            condition = f"{left_plan.expr} {frag.binary_op} {right_plan.expr}"
            return FormulaPlan(
                specs=left_plan.specs + right_plan.specs,
                expr=f"CASE({condition}, 1, 0)",
                warnings=warnings,
                bool_compare_cond=condition,
            )

        # Guard a ``bool`` indicator used as a divisor: 1 stays 1, but the false
        # branch becomes NULL (not 0) so we never divide by zero — matching
        # PromQL, where ``x / (y > bool 0)`` yields no sample when y <= 0.
        if frag.binary_op == "/" and right_plan.bool_compare_cond:
            divisor = f"CASE({right_plan.bool_compare_cond}, 1, NULL)"
            return FormulaPlan(
                specs=left_plan.specs + right_plan.specs,
                expr=f"({left_plan.expr} / {divisor})",
                warnings=warnings,
            )

        return FormulaPlan(
            specs=left_plan.specs + right_plan.specs,
            expr=_esql_binary_expr(left_plan.expr, frag.binary_op, right_plan.expr),
            warnings=warnings,
        )

    spec = _build_measure_spec(
        frag,
        resolver,
        rule_pack,
        alias_hint=alias_hint,
        summary_mode=summary_mode,
        preferred_group_labels=preferred_group_labels,
        allow_direct_ts_gauge=allow_direct_ts_gauge,
        preferred_group_labels_origin=preferred_group_labels_origin,
        allow_tsds_gauge_promotion=allow_tsds_gauge_promotion,
        drop_legend_labels=drop_legend_labels,
    )
    if not spec:
        return None
    return FormulaPlan(specs=[spec], expr=spec.final_alias, warnings=list(spec.warnings))


__all__ = [
    "AGG_FUNCTION_MAP",
    "OUTER_AGG_MAP",
    "FormulaPlan",
    "MeasureSpec",
    "PromQLFragment",
    "_apply_fragment_to_context",
    "_build_esql",
    "_build_formula_plan",
    "_build_log_message_filter",
    "_build_measure_spec",
    "_build_shared_measure_pipeline",
    "_build_stats_call",
    "_build_where_lines",
    "_collapse_summary_ts_query",
    "_common_matchers",
    "_detect_outer_agg",
    "_expand_late_bound_group_by_terms",
    "_extract_group_labels",
    "_finalize_fused_stats_assignments",
    "_format_scalar_value",
    "_frag_eval_line",
    "_frag_filters",
    "_frag_group_labels",
    "_frag_has_incompatible_group_fields",
    "_frag_has_incompatible_target_fields",
    "_grouping_parts",
    "_inline_filters_into_stats_expr",
    "_is_esql_control_token",
    "_matcher_alias_suffix",
    "_parse_fragment",
    "_parse_logql_search",
    "_parse_logql_selector",
    "_parse_selector_matchers",
    "_quote_esql_string",
    "_scalar_fragment_expr",
    "_selector_filters",
    "_split_top_level_csv",
    "_strip_grafana_substitutions",
    "_summary_mode_from_metadata",
    "_unique_safe_alias",
    "_wrap_bare_ts_value_args_when_case_siblings",
    "classify_promql_complexity",
    "grafana_template_var_name",
    "preprocess_grafana_macros",
    "substitute_grafana_range_macros",
    "substitute_scalar_template_vars",
]


# --------------------------------------------------------------------------- #
# Co-located per-element arithmetic (agg(A op B))
# --------------------------------------------------------------------------- #
#
# ``agg(A op B) != agg(A) op agg(B)``, which is why the normaliser refuses this
# shape by default. That inequality only matters if A and B must be aggregated
# separately. When the operands share a label set they land on the SAME document
# row in every Prometheus->Elasticsearch layout (one document per timestamp +
# label-set carrying each metric of that set), so ES|QL can evaluate ``A op B``
# per row and aggregate the result -- which is exactly ``agg(A op B)``.
#
# PromQL itself proves the label sets match: a binary operation with no
# ``on()``/``ignoring()`` modifier matches on ALL labels, so a dashboard that
# renders in Grafana necessarily has aligned operands. Fragments carrying
# ``vector_matching``/``join_labels`` are excluded -- those are the genuinely
# unaligned joins.
#
# The renderer is deliberately closed: anything outside the allowlist returns
# None and keeps the existing not-feasible behaviour. A wrong rendering here
# produces silently incorrect numbers rather than a visible failure, so the bias
# is strongly toward refusing.

_COLOCATED_BINARY_OPS = frozenset({"+", "-", "*", "/"})

_COLOCATED_RANGE_FUNCS = frozenset({
    "rate", "irate", "increase", "delta",
    "avg_over_time", "max_over_time", "min_over_time", "sum_over_time",
    "last_over_time",
})


def _colocated_leaf_matchers(frag):
    return tuple(sorted(
        (str(m.get("label", "")), str(m.get("op", "")), str(m.get("value", "")))
        for m in (frag.matchers or [])
        if isinstance(m, dict)
    ))


def _render_colocated_arithmetic(frag, resolver, rule_pack, depth=0):
    """Render ``A op B`` as one inline ES|QL expression, or None if unsafe.

    Returns ``(expression, matcher_signature)``. ``matcher_signature`` is the
    label-matcher set every metric leaf shares; a mismatch means the operands
    are not the same series and the caller must refuse.
    """
    if frag is None or depth > 32:
        return None

    family = getattr(frag, "family", "")
    extra = getattr(frag, "extra", {}) or {}
    if extra.get("vector_matching") or extra.get("join_labels"):
        return None

    if family == "binary_expr":
        op = (frag.binary_op or "").strip()
        if op not in _COLOCATED_BINARY_OPS:
            return None
        left = _render_colocated_arithmetic(extra.get("left_frag"), resolver, rule_pack, depth + 1)
        right = _render_colocated_arithmetic(extra.get("right_frag"), resolver, rule_pack, depth + 1)
        if left is None or right is None:
            return None
        l_expr, l_sig = left
        r_expr, r_sig = right
        if l_sig is not None and r_sig is not None and l_sig != r_sig:
            return None
        sig = l_sig if l_sig is not None else r_sig
        # No divide-by-zero guard: ES|QL already yields NULL for ``x / 0``
        # (verified: ROW a=5.0, b=0.0 | EVAL a/b -> null), which is the absent
        # point we want. NULLIF is not an ES|QL function -- using it here made
        # every such query fail with "Unknown function [NULLIF]".
        return (f"({l_expr} {op} {r_expr})", sig)

    if getattr(frag, "is_scalar", False) or frag.scalar_value is not None:
        try:
            return (repr(float(frag.scalar_value)), None)
        except (TypeError, ValueError):
            return None

    if not frag.metric:
        return None
    # An operand carrying its own aggregation is a separate reduction; only bare
    # selectors and single range calls are safe to inline.
    if frag.outer_agg or frag.group_labels:
        return None

    field = resolver.resolve_metric_field(frag.metric) if resolver else frag.metric
    field_ref = _esql_identifier(field)
    sig = _colocated_leaf_matchers(frag)

    if family == "simple_metric" and not frag.range_func:
        return (field_ref, sig)

    rf = (frag.range_func or "").lower()
    if family == "range_agg" and rf in _COLOCATED_RANGE_FUNCS:
        esql_fn = AGG_FUNCTION_MAP.get(rf)
        window = frag.range_window or ""
        if not esql_fn or not window:
            return None
        return (f"{esql_fn}({field_ref}, {window})", sig)
    return None


def first_colocated_leaf(frag, depth=0):
    """First metric-bearing leaf of a rendered arithmetic tree."""
    if frag is None or depth > 32:
        return None
    if getattr(frag, "metric", ""):
        return frag
    extra = getattr(frag, "extra", {}) or {}
    for key in ("left_frag", "right_frag"):
        found = first_colocated_leaf(extra.get(key), depth + 1)
        if found is not None:
            return found
    return None


def colocated_metric_fields(frag, resolver, out=None, depth=0):
    """Resolved field paths of every metric leaf in an arithmetic tree."""
    if out is None:
        out = []
    if frag is None or depth > 32:
        return out
    if getattr(frag, "metric", ""):
        field = resolver.resolve_metric_field(frag.metric) if resolver else frag.metric
        if field and field not in out:
            out.append(field)
        return out
    extra = getattr(frag, "extra", {}) or {}
    for key in ("left_frag", "right_frag"):
        colocated_metric_fields(extra.get(key), resolver, out, depth + 1)
    return out


def colocated_binary_agg_plan(frag, resolver, rule_pack):
    """``(value_expr, leaf)`` for a renderable ``agg(A op B)``, else None."""
    if not frag or not frag.outer_agg:
        return None
    inner = (getattr(frag, "extra", {}) or {}).get("inner_frag")
    if inner is None or getattr(inner, "family", "") != "binary_expr":
        return None
    rendered = _render_colocated_arithmetic(inner, resolver, rule_pack)
    if rendered is None:
        return None
    value_expr, matcher_sig = rendered
    if matcher_sig is None:
        # Pure scalar arithmetic: no metric to read.
        return None
    # NOTE: the OUTER aggregation lives in OUTER_AGG_MAP. AGG_FUNCTION_MAP is the
    # RANGE-function map and has no "sum"/"avg" -- looking the outer agg up there
    # silently returns None, which is how an earlier attempt at this fell through
    # to the generic single-metric builder and dropped an operand.
    if (frag.outer_agg or "").lower() not in OUTER_AGG_MAP:
        return None
    leaf = first_colocated_leaf(inner)
    if leaf is None:
        return None
    return value_expr, leaf
