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
    parts = []
    current = []
    in_quote = None
    for char in str(esql or ""):
        if in_quote:
            current.append(char)
            if char == in_quote:
                in_quote = None
            continue
        if char in ("'", '"'):
            in_quote = char
            current.append(char)
            continue
        if char == "|":
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)
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
    by_cols = list(by_cols or [])
    time_fields = [f for f in (time_fields or []) if f in by_cols]
    dimension = None
    if time_fields:
        dimension = time_fields[0]
    elif by_cols:
        dimension = by_cols[0]
    else:
        dimension = "time_bucket"
    breakdown = next((f for f in by_cols if f != dimension), None)
    return dimension, breakdown


def extract_esql_shape(esql):
    commands = split_esql_pipeline(esql)
    for command in commands:
        if not command.lower().startswith("stats "):
            continue
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
        return ESQLShape(
            metric_fields=metric_fields,
            group_fields=group_fields,
            time_fields=time_fields,
            projected_fields=list(group_fields) + list(metric_fields),
            mode="stats",
        )
    for command in reversed(commands):
        if not command.lower().startswith("keep "):
            continue
        projected_fields = [part.strip() for part in _split_top_level_csv(command[5:].strip()) if part.strip()]
        time_fields = [f for f in projected_fields if is_time_like_output_field(f)]
        return ESQLShape(
            projected_fields=projected_fields,
            time_fields=time_fields,
            mode="keep",
        )
    if commands and commands[0].lower().startswith("row "):
        projected_fields = []
        for assignment in _split_top_level_csv(commands[0][4:].strip()):
            alias, expr = split_top_level_assignment(assignment)
            field_name = alias or expr
            if field_name:
                projected_fields.append(field_name)
        time_fields = [f for f in projected_fields if is_time_like_output_field(f)]
        return ESQLShape(
            projected_fields=projected_fields,
            time_fields=time_fields,
            mode="row",
        )
    return ESQLShape()


def extract_esql_columns(esql):
    shape = extract_esql_shape(esql)
    if shape.metric_fields:
        return shape.metric_fields[0], shape.group_fields
    if shape.projected_fields:
        return shape.projected_fields[0], shape.group_fields
    return "value", ["time_bucket"]
