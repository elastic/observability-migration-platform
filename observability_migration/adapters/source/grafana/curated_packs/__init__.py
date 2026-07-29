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


def find_curated_pack(
    gnet_id: int | None,
    title: str,
    tags: list[str],
) -> dict[str, Any] | None:
    """Find the curated pack entry for a dashboard, or None if not registered.

    Detection order:
    1. Exact gnetId integer match (fast, reliable).
    2. Exact title_hint match + tag_hint overlap (fallback when gnetId absent).
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
    tag_set = {str(t).lower() for t in (tags or [])}
    for entry in entries:
        hint_title = str(entry.get("title_hint") or "").strip().lower()
        hint_tags = {str(t).lower() for t in (entry.get("tags_hint") or [])}
        if hint_title and title_lower == hint_title:
            if not hint_tags or hint_tags & tag_set:
                return entry

    return None


__all__ = ["find_curated_pack", "load_curated_registry"]
