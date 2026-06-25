# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Introspect the Datadog planner for the widget types it routes.

The planner dispatches on ``widget_type`` inside individual ``@register``'d
rules rather than via one central map, so the supported set is discovered by
scanning the planner source for the ``widget_type`` comparison literals plus the
module-level text/group widget sets. The coverage cross-check uses this to fail
when the planner gains a widget type the curated registry hasn't acknowledged.
"""

from __future__ import annotations

import re
from pathlib import Path

_PLANNER = Path(__file__).resolve().parents[2] / "adapters" / "source" / "datadog" / "planner.py"

_PATTERNS = (
    re.compile(r'widget_type\s*(?:==|!=)\s*"([a-z_]+)"'),
    re.compile(r"widget_type\s+(?:not\s+)?in\s*\(([^)]*)\)"),
    re.compile(r"widget_type\s+(?:not\s+)?in\s*\{([^}]*)\}"),
)
_LITERAL = re.compile(r'"([a-z_]+)"')


def collect_planner_widget_types(planner_path: Path | None = None) -> set[str]:
    """Return every ``widget_type`` literal the planner routes on."""
    src = (planner_path or _PLANNER).read_text()
    found: set[str] = set()
    for pat in _PATTERNS:
        for match in pat.finditer(src):
            found.update(_LITERAL.findall(match.group(0)))
    for name in ("TEXT_WIDGET_TYPES", "GROUP_WIDGET_TYPES"):
        block = re.search(rf"{name}\s*=\s*\{{([^}}]*)\}}", src)
        if block:
            found.update(_LITERAL.findall(block.group(1)))
    found.discard("")
    return found
