# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Bundled curated packs for known Grafana community dashboards.

Each pack improves migration fidelity for a specific grafana.com dashboard
(identified by gnetId) beyond what the general pipeline produces. Packs are
loaded automatically when a matching dashboard is detected — zero operator
setup required. Operators can override any curated setting via --rules-file.

See: docs/design/curated-dashboard-packs.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REGISTRY_PATH = Path(__file__).parent / "registry.yaml"


def load_curated_registry() -> list[dict[str, Any]]:
    """Return all registered curated pack entries."""
    with open(_REGISTRY_PATH) as fh:
        data = yaml.safe_load(fh) or {}
    return list(data.get("packs") or [])


def _dashboard_expr_text(dashboard: dict[str, Any] | None) -> str:
    """Join panel ``expr`` strings so a shared title can be told apart."""
    if not isinstance(dashboard, dict):
        return ""
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for target in node.get("targets") or []:
                if isinstance(target, dict):
                    expr = target.get("expr")
                    if isinstance(expr, str) and expr.strip():
                        chunks.append(expr)
            for key in ("panels", "rows"):
                child = node.get(key)
                if isinstance(child, list):
                    for item in child:
                        walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(dashboard)
    return "\n".join(chunks)


def _title_hint_matches(entry: dict[str, Any], title_lower: str, tag_set: set[str]) -> bool:
    hint_title = str(entry.get("title_hint") or "").strip().lower()
    hint_tags = {str(tag).lower() for tag in (entry.get("tags_hint") or [])}
    if not hint_title or title_lower != hint_title:
        return False
    return not hint_tags or not tag_set or bool(hint_tags & tag_set)


def find_curated_pack(
    gnet_id: int | None,
    title: str,
    tags: list[str],
    dashboard: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Find the curated pack entry for a dashboard, or None if not registered.

    Detection order:
    1. Exact gnetId integer match (fast, reliable).
    2. Exact title_hint match when gnetId is absent. Copies and re-imports
       often strip both gnetId and tags; an empty tag list still matches.
       When the dashboard still has tags *and* the pack declares tags_hint,
       require overlap so a similarly titled unrelated dashboard does not
       pick up the pack. If several packs share that title, a
       ``query_contains`` fragment that appears in a panel ``expr`` wins;
       otherwise the pack with no fragment (the original) is used.
    """
    entries = load_curated_registry()

    if gnet_id is not None:
        try:
            gnet_id_int = int(gnet_id)
        except (TypeError, ValueError):
            gnet_id_int = None
        if gnet_id_int is not None:
            for entry in entries:
                if entry.get("gnet_id") == gnet_id_int:
                    return entry

    title_lower = (title or "").strip().lower()
    tag_set = {str(tag).lower() for tag in (tags or [])}
    matches = [
        entry for entry in entries if _title_hint_matches(entry, title_lower, tag_set)
    ]
    if not matches:
        return None
    text = _dashboard_expr_text(dashboard)
    signed = [
        entry
        for entry in matches
        if (sig := str(entry.get("query_contains") or "").strip()) and sig in text
    ]
    if len(signed) == 1:
        return signed[0]
    if len(signed) > 1:
        return max(signed, key=lambda entry: len(str(entry.get("query_contains") or "")))
    for entry in matches:
        if not str(entry.get("query_contains") or "").strip():
            return entry
    return matches[0]


__all__ = ["find_curated_pack", "load_curated_registry"]
