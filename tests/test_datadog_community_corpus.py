# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline guards for the pinned DataDog community benchmark corpus (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.fetch_datadog_community_corpus import canonical_sha256

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "parity-rig" / "benchmark" / "datadog_community_corpus.json"


def test_canonical_sha256_is_serialization_stable():
    a = b'{"b": 1, "a": [1, 2]}'
    b = b'{"a":[1,2],"b":1}'  # same data, different key order / whitespace
    assert canonical_sha256(a) == canonical_sha256(b)


def test_manifest_is_well_formed():
    manifest = json.loads(MANIFEST.read_text())
    # Pinned to a concrete integrations-core commit so a fetch is reproducible.
    ref = manifest["ref"]
    assert len(ref) == 40
    assert all(c in "0123456789abcdef" for c in ref), "ref must be a full git commit sha"

    dashboards = manifest["dashboards"]
    # A broad, diverse sample of popular, metrics-heavy official DataDog
    # integration dashboards. Grown intentionally; keep it large so the pinned
    # benchmark stays representative (bump this floor only when adding pins).
    assert len(dashboards) >= 45
    for d in dashboards:
        assert d["integration"] and isinstance(d["integration"], str)
        assert d["slug"] and isinstance(d["slug"], str)
        assert d["title"] and isinstance(d["title"], str)
        assert d["panels"] >= 1
        # path points at a real integrations-core dashboard asset.
        assert d["path"].endswith(".json")
        assert "/assets/dashboards/" in d["path"]
        # sha256 is a 64-char lowercase hex digest of the canonical JSON.
        assert len(d["sha256"]) == 64
        assert all(c in "0123456789abcdef" for c in d["sha256"])
    # slugs are unique: fetch_datadog_community_corpus.py writes one {slug}.json
    # per entry, so a collision would silently overwrite a dashboard on disk.
    slugs = [d["slug"] for d in dashboards]
    assert len(set(slugs)) == len(slugs), "duplicate slugs would overwrite fetched files"
    # paths are unique too (two integrations should not pin the same asset).
    paths = [d["path"] for d in dashboards]
    assert len(set(paths)) == len(paths), "duplicate dashboard paths in the pin manifest"
    # sorted by slug for stable, reviewable diffs.
    assert slugs == sorted(slugs), "manifest dashboards must be sorted by slug"


def test_manifest_slugs_are_filesystem_safe():
    """Slugs become ``{slug}.json`` filenames; keep them path-safe."""
    manifest = json.loads(MANIFEST.read_text())
    for d in manifest["dashboards"]:
        slug = d["slug"]
        assert "/" not in slug and "\\" not in slug and ".." not in slug
        assert slug == slug.strip()
