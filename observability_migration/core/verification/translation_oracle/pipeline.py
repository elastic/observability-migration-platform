# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""ES|QL pipeline parsing helpers for the shared translation oracle."""

from __future__ import annotations

import re

_STATS_BODY = re.compile(r"^STATS\s+(.+?)\s+BY\s+", re.IGNORECASE | re.DOTALL)


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


__all__ = [
    "parse_stats_assignments",
    "split_pipeline_stages",
    "split_top_level_csv",
]
