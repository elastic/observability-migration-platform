# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Introspect the Datadog planner for the widget types it routes.

The planner dispatches on ``widget_type`` inside individual ``@register``'d
rules rather than via one central map, so the supported set is discovered by
scanning the planner source for the ``widget_type`` comparison literals plus
every module-level ``*_WIDGET_TYPES`` set (TEXT_, GROUP_, STATUS_PLACEHOLDER_,
and any future ones — a rule that does ``widget_type not in SOME_WIDGET_TYPES``
is otherwise invisible to the literal scan). The coverage cross-check uses this
to fail when the planner gains a widget type the curated registry hasn't
acknowledged.
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
# Any module-level ``<NAME>_WIDGET_TYPES = { ... }`` set, so a rule that routes
# via ``widget_type (not) in SOME_WIDGET_TYPES`` is discovered even though the
# literals live in the set definition, not at the comparison site.
_WIDGET_TYPE_SET = re.compile(r"\b[A-Z_]*WIDGET_TYPES\s*=\s*\{([^}]*)\}")


def collect_planner_widget_types(planner_path: Path | None = None) -> set[str]:
    """Return every ``widget_type`` literal the planner routes on."""
    src = (planner_path or _PLANNER).read_text()
    found: set[str] = set()
    for pat in _PATTERNS:
        for match in pat.finditer(src):
            found.update(_LITERAL.findall(match.group(0)))
    for block in _WIDGET_TYPE_SET.finditer(src):
        found.update(_LITERAL.findall(block.group(1)))
    found.discard("")
    return found
