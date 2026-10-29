# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Counter functions over fields the live target stores as gauges.

Prometheus exporters declare many cumulative counters as ``untyped``
(mysqld_exporter status counters, postgres_exporter ``pg_stat_database_*``,
node_exporter vmstat/netstat). Elasticsearch's ``/_prometheus`` remote-write
endpoint stores ``untyped`` as a gauge, and ES|QL ``RATE``/``IRATE``/``INCREASE``
only accept ``counter_*`` fields, so a translation that keeps ``RATE`` fails on
every such panel. When live field caps prove the emitted field is not a
counter, each call is rewritten to the gauge form PromQL itself uses for a
counter: the change over a window divided by its length. On the native
remote-write layout ``SUM(DELTA(x, 5 minutes)) / 300`` matches Prometheus
``rate(x[5m])`` (measured 47.58/s vs 47.62/s); like PromQL ``delta()`` it does
not compensate for counter resets.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_COUNTER_CALL_RE = re.compile(
    r"\b(?P<fn>RATE|IRATE|INCREASE)\(\s*(?P<field>`[^`]+`|[A-Za-z_][\w.:]*)\s*(?:,\s*(?P<window>[^()]+?))?\s*\)"
)
_FIXED_BUCKET_RE = re.compile(r"\bTBUCKET\(\s*(\d+)\s*(second|minute|hour|day)s?\s*\)", re.IGNORECASE)
_WINDOW_RE = re.compile(r"^\s*(\d+)\s*(second|minute|hour|day)s?\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
# Used when neither the call nor the bucketing names a window; the common
# Grafana range for rate panels.
_DEFAULT_WINDOW = (5, "minute")


def _window(text: str | None, query: str) -> tuple[str, int]:
    for candidate in (text, *(m.group(0)[len("TBUCKET("):-1] for m in _FIXED_BUCKET_RE.finditer(query))):
        match = _WINDOW_RE.match(candidate or "")
        if match:
            amount, unit = int(match.group(1)), match.group(2).lower()
            return f"{amount} {unit}{'s' if amount != 1 else ''}", amount * _UNIT_SECONDS[unit]
    amount, unit = _DEFAULT_WINDOW
    return f"{amount} {unit}s", amount * _UNIT_SECONDS[unit]


def rewrite_gauge_typed_counter_calls(
    query: str,
    refutes_counter: Callable[[str], bool],
) -> tuple[str, list[str]]:
    """Rewrite ``RATE``/``IRATE``/``INCREASE`` over fields the target types as gauges.

    ``refutes_counter(field)`` must be True only on positive target evidence
    (the field exists and is not counter-typed); without it nothing changes.
    Returns the new query and the fields that were rewritten.
    """
    if not query or query.lstrip().upper().startswith("PROMQL"):
        return query, []
    rewritten: list[str] = []

    def replace(match: re.Match[str]) -> str:
        field = match.group("field")
        name = field.strip("`")
        if not refutes_counter(name):
            return match.group(0)
        if name not in rewritten:
            rewritten.append(name)
        if match.group("fn") == "INCREASE":
            window = f", {match.group('window')}" if match.group("window") else ""
            return f"DELTA({field}{window})"
        window_text, seconds = _window(match.group("window"), query)
        return f"(DELTA({field}, {window_text}) / {seconds})"

    return _COUNTER_CALL_RE.sub(replace, query), rewritten


def gauge_typed_counter_note(fields: list[str]) -> str:
    names = ", ".join(f"`{name}`" for name in fields)
    return (
        f"The target stores {names} as a gauge (Prometheus untyped counter on the "
        "/_prometheus endpoint), where ES|QL RATE/IRATE/INCREASE fail; computed the "
        "rate as DELTA over the window divided by its length, as PromQL does for a "
        "counter. Counter resets inside a window show as a negative dip."
    )


_KEPT_RATE_WARNING = "but the target currently types this field as gauge; kept"


def apply_gauge_typed_counter_rewrites(results, resolver) -> int:
    """Rewrite counter calls in every panel query the live target would reject.

    Updates ``esql_query`` / ``query_ir["target_query"]`` and replaces the
    "kept RATE" warning with what was computed. Callers sync the IR afterwards.
    Returns the number of panels changed.
    """
    refutes = getattr(resolver, "field_refutes_counter", None)
    has_caps = getattr(resolver, "has_field_capabilities", None)
    if not callable(refutes) or not callable(has_caps) or not has_caps():
        return 0
    changed = 0
    for result in results:
        for panel_result in getattr(result, "panel_results", []) or []:
            query, fields = rewrite_gauge_typed_counter_calls(panel_result.esql_query or "", refutes)
            if not fields:
                continue
            panel_result.esql_query = query
            if isinstance(panel_result.query_ir, dict) and panel_result.query_ir.get("target_query"):
                panel_result.query_ir["target_query"] = query
            bare = {name.split(".", 1)[1] if name.startswith("metrics.") else name for name in fields}
            panel_result.reasons = [
                reason for reason in panel_result.reasons
                if not (_KEPT_RATE_WARNING in reason and any(f" on {name} " in reason for name in bare | set(fields)))
            ]
            panel_result.reasons.append(gauge_typed_counter_note(fields))
            if panel_result.status == "migrated":
                panel_result.status = "migrated_with_warnings"
            changed += 1
    return changed
