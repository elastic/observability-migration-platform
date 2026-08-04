# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Curated layout packs for Datadog dashboard migrations."""

from __future__ import annotations

import os
from typing import Any

import yaml


def load_curated_pack(dashboard_title: str) -> dict[str, Any] | None:
    """Return the curated pack dict for a dashboard title, or None if no pack exists.

    Packs are visited in sorted directory-name order, not ``os.scandir`` order.
    ``scandir`` order is filesystem-defined, so if two packs' ``title_contains``
    values both matched a dashboard the winner -- and therefore the panel geometry
    in the uploaded payload -- would vary between machines. Sorting makes the
    first match deterministic; overlapping matchers are still an authoring bug,
    but they now fail the same way everywhere.
    """
    packs_dir = os.path.dirname(__file__)
    for entry in sorted(os.scandir(packs_dir), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        pack_path = os.path.join(entry.path, "pack.yaml")
        if not os.path.isfile(pack_path):
            continue
        with open(pack_path, encoding="utf-8") as f:
            pack = yaml.safe_load(f)
        if not isinstance(pack, dict):
            continue
        match = pack.get("match", {})
        title_contains = match.get("title_contains", "")
        if title_contains and title_contains.lower() in dashboard_title.lower():
            pack = dict(pack)
            pack["_curated_pack_name"] = entry.name
            return pack
    return None
