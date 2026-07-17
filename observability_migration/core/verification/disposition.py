# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Source-agnostic disposition of live ES|QL validation failures.

When a translated query fails live validation, the engine decides whether the
failure is a data-timing issue that self-heals once telemetry arrives, or a
genuinely broken query. These helpers classify that distinction and live in
core so both source adapters and the shared reporting layer can use them without
importing an adapter (issue #154).

``validation_result`` may optionally include ``esql_query`` (or the legacy
``query`` key from ``validate_query_with_fixes``) so alias-shaped Unknown
column failures can be distinguished from missing telemetry.
"""

from __future__ import annotations

import re

from observability_migration.targets.kibana.emit.esql_utils import _split_top_level_csv

# Structured semantic-loss marker recorded when a panel/widget is kept as a
# self-healing visualization. Mirrors the placeholder path's marker so coverage
# reports surface the disposition (issue #154).
SELF_HEAL_SEMANTIC_LOSS = "target telemetry not yet ingested (self-healing panel)"

_ASSIGNMENT_LHS = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.+)$", re.DOTALL)
_EVAL_SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_STATS_BODY = re.compile(r"^STATS\s+(.+?)\s+BY\s+", re.IGNORECASE | re.DOTALL)
_EVAL_BODY = re.compile(r"^EVAL\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.+)$", re.IGNORECASE | re.DOTALL)


def unknown_column_looks_like_alias_bug(column_name: str, esql_query: str | None) -> bool:
    """True when an Unknown column error is translator-shaped alias confusion.

    Detects output-column mistakes such as referencing a pre-rename STATS alias
    from a simple ``EVAL alias = column`` RHS. Physical metric absence inside
    function arguments (for example ``RATE(http_requests_total, 5m)``) is not
    treated as an alias bug.
    """
    if not esql_query or not column_name:
        return False

    defined_columns: set[str] = set()
    for stage in _split_pipeline_stages(esql_query):
        stage_upper = stage.upper()
        if stage_upper.startswith("STATS"):
            defined_columns.update(_defined_columns_from_stats(stage))
        elif stage_upper.startswith("EVAL"):
            eval_match = _EVAL_BODY.match(stage.strip())
            if not eval_match:
                continue
            rhs = eval_match.group(2).strip()
            if (
                _EVAL_SIMPLE_IDENT.fullmatch(rhs)
                and rhs == column_name
                and rhs not in defined_columns
            ):
                return True
            defined_columns.add(eval_match.group(1))
    return False


def validation_failure_self_heals(validation_result):
    """True when a failed live validation is a data-timing issue rather than a
    broken query.

    A missing target field (``Unknown column``) or missing target index
    (``Unknown index``) means the telemetry has simply not been ingested yet.
    The translated ES|QL is structurally valid and the panel will populate on
    its own once data arrives, so it should be kept (with a warning) instead of
    being replaced by a markdown placeholder.

    A counter type mismatch is excluded: the field exists but has the wrong
    type, so waiting for data will not fix it.

    When ``validation_result`` includes ``esql_query`` (or legacy ``query``),
    alias-shaped Unknown column failures are excluded because the query is
    structurally broken until the translator is fixed.
    """
    validation_result = validation_result or {}
    analysis = validation_result.get("analysis") or {}
    if analysis.get("counter_mismatch_metrics"):
        return False
    query = validation_result.get("esql_query") or validation_result.get("query")
    for col in analysis.get("unknown_columns") or []:
        name = col.get("name", "") if isinstance(col, dict) else str(col)
        if query and unknown_column_looks_like_alias_bug(name, query):
            return False
    return bool(analysis.get("unknown_columns") or analysis.get("unknown_indexes"))


def missing_target_field_warning(validation_result):
    """Human-readable warning for a self-healing validation failure, naming the
    target fields/indexes that are not ingested yet.

    The message is deliberately not absolute: a field that is genuinely
    misnamed (rather than not-yet-ingested) is indistinguishable at validation
    time, so the wording invites the reviewer to check the field name if data is
    already flowing.
    """
    analysis = (validation_result or {}).get("analysis") or {}
    names = [col.get("name", "") for col in analysis.get("unknown_columns") or []]
    names.extend(analysis.get("unknown_indexes") or [])
    names = [name for name in names if name]
    if names:
        field_list = ", ".join(f"`{name}`" for name in names)
        return (
            f"Live ES|QL validation could not find target field/index {field_list} "
            "yet; the query is structurally valid and the panel will populate once "
            "this telemetry is ingested (verify the field name if data is already flowing)."
        )
    return (
        "Live ES|QL validation found no matching data yet; the query is "
        "structurally valid and the panel will populate once telemetry is ingested "
        "(verify the query if data is already flowing)."
    )


def _split_pipeline_stages(query: str) -> list[str]:
    text = query.strip()
    if not text:
        return []
    parts = re.split(r"\n\s*\|\s*", text)
    stages: list[str] = []
    first = parts[0].strip()
    if first:
        stages.append(first)
    stages.extend(part.strip() for part in parts[1:] if part.strip())
    return stages


def _parse_stats_assignments(stage: str) -> list[str]:
    match = _STATS_BODY.match(stage.strip())
    if not match:
        return []
    return _split_top_level_csv(match.group(1))


def _defined_columns_from_stats(stage: str) -> set[str]:
    defined: set[str] = set()
    for assignment in _parse_stats_assignments(stage):
        lhs_match = _ASSIGNMENT_LHS.match(assignment.strip())
        if lhs_match:
            defined.add(lhs_match.group(1))
    by_match = re.search(r"\bBY\s+(.+)$", stage.strip(), re.IGNORECASE | re.DOTALL)
    if by_match:
        for part in _split_top_level_csv(by_match.group(1)):
            lhs_match = _ASSIGNMENT_LHS.match(part.strip())
            if lhs_match:
                defined.add(lhs_match.group(1))
    return defined
