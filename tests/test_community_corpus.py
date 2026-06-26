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
    assert len(dashboards) == 10
    for d in dashboards:
        assert isinstance(d["id"], int)
        assert d["revision"]  # pinned to a concrete revision, not "latest"
        assert len(d["sha256"]) == 64
        assert d["panels"] >= 1
    # ids are unique and sorted
    ids = [d["id"] for d in dashboards]
    assert len(set(ids)) == 10
    assert ids == sorted(ids)
