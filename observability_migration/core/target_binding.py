# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Per-asset provenance for the label fields a migrated query emits.

A translation can be semantically right and still bind to field names the
target data does not have (e.g. Prometheus ``instance`` emitted as the OTel
``service.instance.id`` against a target that stores ``instance``). Such a
panel renders with a ``(null)`` series and its control has no options, yet the
translation status is clean. Recording *why* each field was chosen lets the
report separate "translated" from "bound to fields the target confirmed".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# Worst first; mirrors ``SchemaResolver.BINDING_STATUS_ORDER`` (Grafana).
BINDING_STATUS_ORDER = ("missing", "guessed", "asserted", "verified")


def begin_capture(resolver: Any) -> bool:
    """Start a provenance capture when *resolver* supports it."""
    begin = getattr(resolver, "begin_binding_capture", None)
    if not callable(begin):
        return False
    begin()
    return True


def end_capture(resolver: Any) -> list[dict[str, str]]:
    end = getattr(resolver, "end_binding_capture", None)
    if not callable(end):
        return []
    return list(end() or [])


def _field_pattern(field_name: str) -> re.Pattern[str]:
    # ``instance`` must not match inside ``labels.instance``, ``instance_id``
    # or the control parameter ``?instance``; backticks are optional quoting.
    return re.compile(r"(?<![\w.?])`?" + re.escape(field_name) + r"`?(?![\w.])")


def summarize_binding(entries: Iterable[dict[str, str]], emitted_text: str) -> dict[str, Any]:
    """Collapse captured resolutions into the fields *emitted_text* uses.

    Translation can resolve a label and then discard that branch (e.g. a native
    PROMQL attempt), so only fields that appear in the final query count.
    """
    text = emitted_text or ""
    fields: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.get("label", ""), entry.get("field", ""))
        if key in seen or not key[1]:
            continue
        if not _field_pattern(key[1]).search(text):
            continue
        seen.add(key)
        fields.append(dict(entry))
    return {"status": worst_status(entry.get("status", "") for entry in fields), "fields": fields}


def worst_status(statuses: Iterable[str]) -> str:
    ranks = [BINDING_STATUS_ORDER.index(s) for s in statuses if s in BINDING_STATUS_ORDER]
    return BINDING_STATUS_ORDER[min(ranks)] if ranks else "none"


def count_statuses(bindings: Iterable[dict[str, Any] | None]) -> dict[str, int]:
    """Count assets per binding status (``none`` = no label fields)."""
    counts = {status: 0 for status in (*BINDING_STATUS_ORDER, "none")}
    for binding in bindings:
        status = (binding or {}).get("status") or "none"
        counts[status if status in counts else "none"] += 1
    return counts


def unverified_examples(bindings: Iterable[dict[str, Any] | None], limit: int = 3) -> list[str]:
    """Distinct ``label → field`` pairs that were guessed or are missing."""
    examples: list[str] = []
    for binding in bindings:
        for entry in (binding or {}).get("fields", []):
            if entry.get("status") not in {"guessed", "missing"}:
                continue
            text = f"{entry.get('label')} → {entry.get('field')} ({entry.get('status')})"
            if text not in examples:
                examples.append(text)
            if len(examples) >= limit:
                return examples
    return examples
