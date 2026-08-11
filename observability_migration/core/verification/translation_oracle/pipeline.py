# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""ES|QL pipeline parsing helpers for the shared translation oracle."""

from __future__ import annotations

import re

_STATS_BODY = re.compile(r"^STATS\s+(.+?)\s+BY\s+", re.IGNORECASE | re.DOTALL)
_STATS_GROUPING = re.compile(r"^STATS\s+.+?\s+BY\s+(.+)$", re.IGNORECASE | re.DOTALL)
_GROUPING_ALIAS = re.compile(r"^([A-Za-z_`][A-Za-z0-9_.`]*)\s*=", re.DOTALL)


def split_top_level_csv(expr: str) -> list[str]:
    """Split a comma-separated expression respecting parentheses and quotes."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = None
    for char in expr or "":
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


def split_pipeline_stages(query: str) -> list[str]:
    """Split ES|QL into stages on newline-prefixed pipes (emitter convention)."""
    text = (query or "").strip()
    if not text:
        return []
    parts = re.split(r"\n\s*\|\s*", text)
    stages: list[str] = []
    first = parts[0].strip()
    if first:
        stages.append(first)
    stages.extend(part.strip() for part in parts[1:] if part.strip())
    return stages


def parse_stats_assignments(stage: str) -> list[str]:
    match = _STATS_BODY.match((stage or "").strip())
    if not match:
        return []
    return split_top_level_csv(match.group(1))


def parse_stats_grouping(stage: str) -> list[str]:
    """Column names a STATS stage's ``BY`` clause contributes to its output.

    A STATS emits its aggregate aliases *and* its grouping keys, so both are
    defined downstream. Tracking only the aliases made the oracle report
    ``EVAL references undefined column 'namespace'`` for a perfectly valid
    ``STATS x = MAX(x) BY namespace, ... | EVAL namespace = namespace``.

    Handles the aliased form (``time_bucket = TBUCKET(...)`` contributes
    ``time_bucket``) and the bare form (``labels.instance`` contributes itself).
    An unaliased expression contributes nothing nameable and is skipped.
    """
    match = _STATS_GROUPING.match((stage or "").strip())
    if not match:
        return []
    names: list[str] = []
    for part in split_top_level_csv(match.group(1)):
        text = part.strip()
        if not text:
            continue
        alias = _GROUPING_ALIAS.match(text)
        if alias:
            names.append(alias.group(1).strip("`"))
            continue
        bare = text.strip("`")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", bare):
            names.append(bare)
    return names


__all__ = [
    "parse_stats_assignments",
    "parse_stats_grouping",
    "split_pipeline_stages",
    "split_top_level_csv",
]
