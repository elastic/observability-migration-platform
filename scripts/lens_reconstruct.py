# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Reconstruct the ES|QL a Kibana Lens panel would execute, for validation.

Lens panels store a declarative aggregation spec rather than an ES|QL string,
so the panel validator cannot POST them to ``/_query`` directly. This module
turns that spec back into the query Lens' runtime would build, so the existing
execute + zero-row + column checks apply to Lens panels too. Pure (no network).
"""
from __future__ import annotations

import re

# Canonical Lens aggregation name -> (ES|QL function, needs_TS_source).
# ``needs_ts`` marks aggregations that only exist on the TS command
# (LAST_OVER_TIME); everything else runs on FROM.
_AGG_FUNC: dict[str, tuple[str, bool]] = {
    "average": ("AVG", False),
    "sum": ("SUM", False),
    "min": ("MIN", False),
    "max": ("MAX", False),
    "unique_count": ("COUNT_DISTINCT", False),
    "median": ("MEDIAN", False),
    "standard_deviation": ("STD_DEV", False),
    "last_value": ("LAST_OVER_TIME", True),
}


def _agg_expr(metric: dict, counter_fields: set | None = None):
    """Return (esql_expr, alias, needs_ts) for one Lens metric, or None.

    None signals an unsupported aggregation so the caller can degrade to an
    explicit skip-with-reason rather than emit invalid ES|QL.

    ``counter_fields`` is the set of fields the cluster reports as
    ``time_series_metric: counter``. A counter cannot be aggregated on the FROM
    command with a bare ``SUM``/``AVG`` (ES|QL rejects it); it must run on the
    TS command with a counter-aware inner function. We wrap the field in
    ``LAST_OVER_TIME`` (the counter's value snapshot per series) and force a TS
    source, mirroring what a correct Lens-over-counter render resolves to.
    """
    agg = str(metric.get("aggregation") or "").strip().lower()
    field = metric.get("field")
    is_counter = bool(field) and bool(counter_fields) and field in counter_fields
    operand = f"LAST_OVER_TIME({field})" if is_counter else field

    if agg == "count":
        # Lens "count" is a row count; COUNT(*) is valid on FROM.
        return ("COUNT(*)", "count", False)

    if agg == "percentile":
        if not field:
            return None
        p = metric.get("percentile", 95)
        return (f"PERCENTILE({operand}, {p})", f"{field}_pct{p}", is_counter)

    spec = _AGG_FUNC.get(agg)
    if spec is None or not field:
        return None
    func, needs_ts = spec
    return (f"{func}({operand})", f"{field}_{agg}", needs_ts or is_counter)


# A bare ES|QL identifier: starts with a letter/underscore, then
# letters/digits/underscore/dot. Anything else (hyphen, space) must be quoted.
_BARE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _quote(field: str) -> str:
    """Backtick-quote a field name for ES|QL if it isn't a bare identifier."""
    f = str(field)
    if f.startswith("`") and f.endswith("`"):
        return f
    if _BARE_IDENT_RE.match(f):
        return f
    return f"`{f}`"


def _breakdown_fields(lens: dict) -> list[str]:
    """Return breakdown field names (singular ``breakdown`` or plural ``breakdowns``)."""
    fields: list[str] = []
    bd = lens.get("breakdown")
    if isinstance(bd, dict) and bd.get("field"):
        fields.append(bd["field"])
    for b in lens.get("breakdowns") or []:
        if isinstance(b, dict) and b.get("field"):
            fields.append(b["field"])
    return fields


_DATE_BUCKET = "tbucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)"
_TIME_CHARTS = ("line", "bar", "area")


def lens_to_esql(lens: dict, counter_fields: set | None = None):
    """Reconstruct the ES|QL a Lens panel would run.

    Returns ``(query, expected_cols, unsupported_reason)``:
      * success     -> (query, [cols], None)
      * unsupported -> (None, [], "reason")

    ``counter_fields`` (optional) is the set of metric fields the cluster types
    as counters; metrics on those fields are reconstructed on the TS command
    with a counter-aware inner function (see ``_agg_expr``). Omitting it keeps
    the FROM + bare-aggregation form (backward compatible).
    """
    if not isinstance(lens, dict):
        return (None, [], "malformed lens config: not a mapping")

    chart = str(lens.get("type") or "").strip().lower()
    data_view = lens.get("data_view") or "metrics-*"

    # ``metric`` chart carries a single ``primary`` metric; the others use
    # ``metrics[]``.
    if chart == "metric":
        primary = lens.get("primary")
        metrics = [primary] if isinstance(primary, dict) else None
    else:
        metrics = lens.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        return (None, [], f"unsupported lens chart '{chart or '?'}': no metrics")

    exprs: list[str] = []
    aliases: list[str] = []
    needs_ts = False
    for m in metrics:
        res = _agg_expr(m, counter_fields) if isinstance(m, dict) else None
        if res is None:
            return (None, [], f"unsupported lens aggregation: {m!r}")
        expr, alias, ts = res
        exprs.append(f"{alias} = {expr}")
        aliases.append(alias)
        needs_ts = needs_ts or ts

    breakdowns = _breakdown_fields(lens)
    by_keys = [_quote(b) for b in breakdowns]
    cols = list(aliases) + list(breakdowns)

    if chart in _TIME_CHARTS:
        dim = lens.get("dimension") or {}
        if dim.get("type") != "date_histogram":
            return (None, [], f"unsupported lens dimension: {dim.get('type')!r}")
        by_keys.append(_DATE_BUCKET)
        cols.append("tbucket")
    elif chart in ("metric", "pie", "datatable"):
        pass  # no date bucket; breakdowns (if any) already in by_keys
    else:
        return (None, [], f"unsupported lens chart type: {chart!r}")

    source = "TS" if needs_ts else "FROM"
    stats = "| STATS " + ", ".join(exprs)
    if by_keys:
        stats += " BY " + ", ".join(by_keys)
    query = f"{source} {data_view}\n{stats}"
    return (query, cols, None)
