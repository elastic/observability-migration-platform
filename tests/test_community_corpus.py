# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline guards for the pinned community benchmark corpus (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.fetch_community_corpus import canonical_sha256

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "parity-rig" / "benchmark" / "community_corpus.json"


def test_canonical_sha256_is_serialization_stable():
    a = b'{"b": 1, "a": [1, 2]}'
    b = b'{"a":[1,2],"b":1}'  # same data, different key order / whitespace
    assert canonical_sha256(a) == canonical_sha256(b)


def test_manifest_is_well_formed():
    manifest = json.loads(MANIFEST.read_text())
    dashboards = manifest["dashboards"]
    # A broad, stratified sample of the most-downloaded Prometheus-backed
    # grafana.com dashboards. Grown intentionally; keep it large so the pinned
    # benchmark stays representative (bump this floor only when adding pins).
    assert len(dashboards) >= 60
    for d in dashboards:
        assert isinstance(d["id"], int)
        assert d["revision"]  # pinned to a concrete revision, not "latest"
        assert len(d["sha256"]) == 64
        # sha256 is a lowercase hex digest
        assert all(c in "0123456789abcdef" for c in d["sha256"])
        assert d["panels"] >= 1
        assert d["slug"] and isinstance(d["slug"], str)
        assert d["title"] and isinstance(d["title"], str)
    # ids are unique and sorted
    ids = [d["id"] for d in dashboards]
    assert len(set(ids)) == len(ids), "duplicate dashboard ids in the pin manifest"
    assert ids == sorted(ids), "manifest dashboards must be sorted by id"
    # slugs are unique: fetch_community_corpus.py writes one {slug}.json per
    # entry, so a collision would silently overwrite a dashboard on disk.
    slugs = [d["slug"] for d in dashboards]
    assert len(set(slugs)) == len(slugs), "duplicate slugs would overwrite fetched files"


def test_manifest_slugs_are_filesystem_safe():
    """Slugs become ``{slug}.json`` filenames; keep them path-safe."""
    manifest = json.loads(MANIFEST.read_text())
    for d in manifest["dashboards"]:
        slug = d["slug"]
        assert "/" not in slug and "\\" not in slug and ".." not in slug
        assert slug == slug.strip()
