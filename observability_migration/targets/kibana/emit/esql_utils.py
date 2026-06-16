# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""ES|QL string parsing and shape extraction utilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _split_top_level_csv(expr):
    """Split a comma-separated expression respecting parentheses and quotes."""
    parts = []
    current = []
    depth = 0
    in_quote = None
    for char in expr:
        if in_quote:
            current.append(char)
            if char == in_quote:
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


_TIME_DIMENSION_FIELDS = {"time_bucket", "timestamp_bucket", "step"}


@dataclass
class ESQLShape:
    metric_fields: list[str] = field(default_factory=list)
    group_fields: list[str] = field(default_factory=list)
    time_fields: list[str] = field(default_factory=list)
    projected_fields: list[str] = field(default_factory=list)
    mode: str = ""


def split_esql_pipeline(esql):
    """Split an ES|QL pipeline on top-level ``|`` separators.

    The splitter understands three string forms so pipes inside string
    literals do not accidentally split a stage:

    * ``\"\"\"...\"\"\"`` — ES|QL raw triple-quoted strings. Backslash escapes
      are *not* processed; the literal terminates at the next ``\"\"\"``.
    * ``\"...\"`` — regular double-quoted strings.
    * ``'...'`` — kept for safety even though ES|QL does not officially use
      single-quoted string literals.

    Without triple-quoted awareness, each individual ``\"`` in a
    ``\"\"\"...\"\"\"`` literal would flip an in/out-of-quote boundary, which
    causes pipeline stages following the literal to be merged or dropped
    whenever the surrounding string contains additional ``\"`` characters
    (as the native PROMQL emission's ``REPLACE(..., \"\"\"...\"\"\", \"$1\")``
    calls routinely do).
    """
    text = str(esql or "")
    parts = []
    current = []
    mode = "out"
    quote_char = None
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if mode == "in_triple":
            if char == '"' and text.startswith('"""', i):
                current.append('"""')
                i += 3
                mode = "out"
                continue
            current.append(char)
            i += 1
            continue
        if mode == "in_single":
            current.append(char)
            if char == quote_char:
                mode = "out"
                quote_char = None
            i += 1
            continue
        if char == '"' and text.startswith('"""', i):
            current.append('"""')
            i += 3
            mode = "in_triple"
            continue
        if char in ('"', "'"):
            current.append(char)
            mode = "in_single"
            quote_char = char
            i += 1
            continue
        if char == "|":
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            i += 1
            continue
        current.append(char)
        i += 1
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def split_top_level_keyword(text, keyword):
    upper_keyword = f" {keyword.upper()} "
    depth = 0
    in_quote = None
    upper_text = str(text or "").upper()
    for idx, char in enumerate(str(text or "")):
        if in_quote:
            if char == in_quote:
                in_quote = None
            continue
        if char in ("'", '"'):
            in_quote = char
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")":
            depth = max(depth - 1, 0)
            continue
        if depth == 0 and upper_text[idx:].startswith(upper_keyword):
            return text[:idx].strip(), text[idx + len(upper_keyword):].strip()
    return str(text or "").strip(), ""


def split_top_level_assignment(text):
    depth = 0
    in_quote = None
    for idx, char in enumerate(str(text or "")):
        if in_quote:
            if char == in_quote:
                in_quote = None
            continue
        if char in ("'", '"'):
            in_quote = char
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")":
            depth = max(depth - 1, 0)
            continue
        if char == "=" and depth == 0:
            return text[:idx].strip(), text[idx + 1:].strip()
    return "", str(text or "").strip()


def is_time_like_output_field(field_name):
    return str(field_name or "").strip() in _TIME_DIMENSION_FIELDS or str(field_name or "").strip() == "@timestamp"


def is_time_bucket_expression(expr):
    text = str(expr or "").strip().upper()
    return text == "@TIMESTAMP" or "BUCKET(@TIMESTAMP" in text or text.startswith("TBUCKET(")


def select_xy_dimension_fields(by_cols, time_fields=None):
    """Pick the x-axis (and optional breakdown) dimension for an XY chart.

    Returns ``(None, None)`` when neither a time field nor any group column is
    available: an XY chart needs a real x-axis column, and inventing a
    ``time_bucket`` dimension the query never outputs makes Lens fail at render
    time with "Provided column name or index is invalid" (issue #127). Callers
    must treat a ``None`` dimension as "this query has no time series" and
    degrade to a single-value/metric visualization instead.
    """
    by_cols = list(by_cols or [])
    time_fields = [f for f in (time_fields or []) if f in by_cols]
    dimension = None
    if time_fields:
        dimension = time_fields[0]
    elif by_cols:
        dimension = by_cols[0]
    else:
        return None, None
    breakdown = next((f for f in by_cols if f != dimension), None)
    return dimension, breakdown


def _metric_fields_from_projection(projected_fields, group_fields):
    return [
        field
        for field in projected_fields
        if field not in group_fields and not is_time_like_output_field(field)
    ]


def extract_esql_shape(esql):
    commands = split_esql_pipeline(esql)
    shape = ESQLShape()
    for command in commands:
        lower_command = command.lower()
        if lower_command.startswith("stats "):
            assignments_text, by_text = split_top_level_keyword(command[6:].strip(), "BY")
            metric_fields = []
            for assignment in _split_top_level_csv(assignments_text):
                alias, expr = split_top_level_assignment(assignment)
                field_name = alias or expr
                if field_name:
                    metric_fields.append(field_name)
            group_fields = []
            time_fields = []
            for part in _split_top_level_csv(by_text):
                alias, expr = split_top_level_assignment(part)
                field_name = alias or expr
                if not field_name:
                    continue
                group_fields.append(field_name)
                if is_time_like_output_field(field_name) or is_time_bucket_expression(expr or field_name):
                    time_fields.append(field_name)
            shape = ESQLShape(
                metric_fields=metric_fields,
                group_fields=group_fields,
                time_fields=time_fields,
                projected_fields=list(group_fields) + list(metric_fields),
                mode="stats",
            )
            continue

        if lower_command.startswith("eval "):
            for assignment in _split_top_level_csv(command[5:].strip()):
                alias, _expr = split_top_level_assignment(assignment)
                if alias and alias not in shape.projected_fields:
                    shape.projected_fields.append(alias)
            continue

        if lower_command.startswith("keep "):
            projected_fields = [part.strip() for part in _split_top_level_csv(command[5:].strip()) if part.strip()]
            group_fields = [field for field in shape.group_fields if field in projected_fields]
            metric_fields = [field for field in shape.metric_fields if field in projected_fields]
            if not metric_fields:
                metric_fields = _metric_fields_from_projection(projected_fields, group_fields)
            time_fields = [
                field
                for field in projected_fields
                if field in shape.time_fields or is_time_like_output_field(field)
            ]
            shape = ESQLShape(
                metric_fields=metric_fields,
                group_fields=group_fields,
                time_fields=time_fields,
                projected_fields=projected_fields,
                mode=shape.mode or "keep",
            )
            continue

        if lower_command.startswith("drop "):
            dropped_fields = {part.strip() for part in _split_top_level_csv(command[5:].strip()) if part.strip()}
            shape.metric_fields = [field for field in shape.metric_fields if field not in dropped_fields]
            shape.group_fields = [field for field in shape.group_fields if field not in dropped_fields]
            shape.time_fields = [field for field in shape.time_fields if field not in dropped_fields]
            shape.projected_fields = [field for field in shape.projected_fields if field not in dropped_fields]
            if not shape.metric_fields:
                shape.metric_fields = _metric_fields_from_projection(shape.projected_fields, shape.group_fields)
            continue

        if lower_command.startswith("row "):
            projected_fields = []
            for assignment in _split_top_level_csv(command[4:].strip()):
                alias, expr = split_top_level_assignment(assignment)
                field_name = alias or expr
                if field_name:
                    projected_fields.append(field_name)
            time_fields = [f for f in projected_fields if is_time_like_output_field(f)]
            shape = ESQLShape(
                projected_fields=projected_fields,
                time_fields=time_fields,
                mode="row",
            )
    return shape


def extract_esql_columns(esql):
    shape = extract_esql_shape(esql)
    if shape.metric_fields:
        return shape.metric_fields[0], shape.group_fields
    if shape.projected_fields:
        return shape.projected_fields[0], shape.group_fields
    return "value", ["time_bucket"]
