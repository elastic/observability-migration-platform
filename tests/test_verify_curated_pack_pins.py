# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline guards for scripts/verify_curated_pack_pins.py (issue #350).

No network: exercises the canonical-hashing/registry-parsing logic against
synthetic data and a mocked download, mirroring
``tests/test_community_corpus.py``'s coverage of the sibling
``fetch_community_corpus.py`` script. Live verification against grafana.com
is a separate, explicit, network-requiring maintainer command (see the
script's own docstring and ``docs/contributing/dev-commands.md``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.verify_curated_pack_pins import (
    DEFAULT_REGISTRY,
    canonical_sha256,
    load_packs,
    verify_pack,
    verify_registry,
)


def test_canonical_sha256_is_serialization_stable():
    a = b'{"b": 1, "a": [1, 2]}'
    b = b'{"a":[1,2],"b":1}'  # same data, different key order / whitespace
    assert canonical_sha256(a) == canonical_sha256(b)


def test_canonical_sha256_is_sensitive_to_content_changes():
    a = b'{"a": 1}'
    b = b'{"a": 2}'
    assert canonical_sha256(a) != canonical_sha256(b)


def test_default_registry_loads_and_matches_the_real_file():
    """Guards against the script's own path constant drifting from the real
    registry location (would otherwise fail silently with an empty list)."""
    assert DEFAULT_REGISTRY.exists()
    packs = load_packs(DEFAULT_REGISTRY)
    assert len(packs) >= 3
    for pack in packs:
        assert "gnet_id" in pack
        assert "gnet_revision" in pack
        assert "dashboard_sha256" in pack


def test_verify_pack_reports_ok_on_matching_hash():
    raw = b'{"panels": [1, 2, 3]}'
    digest = canonical_sha256(raw)
    pack = {"gnet_id": 1860, "gnet_revision": 37, "dashboard_sha256": digest, "name": "test_pack"}
    with patch("scripts.verify_curated_pack_pins._download", return_value=raw):
        ok, message = verify_pack(pack)
    assert ok
    assert "test_pack" in message


def test_verify_pack_reports_mismatch_on_wrong_hash():
    raw = b'{"panels": [1, 2, 3]}'
    pack = {
        "gnet_id": 1860,
        "gnet_revision": 37,
        "dashboard_sha256": "0" * 64,
        "name": "test_pack",
    }
    with patch("scripts.verify_curated_pack_pins._download", return_value=raw):
        ok, message = verify_pack(pack)
    assert not ok
    assert "mismatch" in message


def test_verify_pack_reports_download_errors():
    pack = {"gnet_id": 1860, "gnet_revision": 37, "dashboard_sha256": "0" * 64, "name": "test_pack"}
    with patch("scripts.verify_curated_pack_pins._download", side_effect=RuntimeError("boom")):
        ok, message = verify_pack(pack)
    assert not ok
    assert "verification error" in message
    assert "boom" in message


def test_verify_pack_reports_malformed_response_without_crashing():
    """A non-JSON (or otherwise malformed) download body must fail *this*
    pack's check, not raise an uncaught exception that aborts every
    remaining pack in the registry."""
    pack = {"gnet_id": 1860, "gnet_revision": 37, "dashboard_sha256": "0" * 64, "name": "test_pack"}
    with patch("scripts.verify_curated_pack_pins._download", return_value=b"not json at all"):
        ok, message = verify_pack(pack)
    assert not ok
    assert "verification error" in message


def test_verify_registry_fails_closed_on_an_empty_registry(tmp_path: Path):
    """An empty/unparseable ``packs`` list must not silently report
    "0/0 pins verified" with a success exit code."""
    registry = tmp_path / "registry.yaml"
    registry.write_text("packs: []\n")
    assert verify_registry(registry) == 1


def test_verify_pack_flags_missing_gnet_fields():
    ok, message = verify_pack({"name": "broken_pack"})
    assert not ok
    assert "missing gnet_id" in message


def test_verify_registry_aggregates_failures_without_network(tmp_path: Path):
    """Exercises the real ``load_packs`` YAML-parsing path (not a mocked
    return value) together with a mocked ``_download``, so this actually
    covers end-to-end registry parsing + per-pack aggregation offline."""
    raw = b'{"x": 1}'
    good_digest = canonical_sha256(raw)
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "packs:\n"
        "  - gnet_id: 1\n"
        "    name: pack_one\n"
        "    gnet_revision: 1\n"
        f'    dashboard_sha256: "{good_digest}"\n'
        "  - gnet_id: 2\n"
        "    name: pack_two\n"
        "    gnet_revision: 1\n"
        f'    dashboard_sha256: "{"b" * 64}"\n'
    )

    def fake_download(url):
        return raw

    with patch("scripts.verify_curated_pack_pins._download", side_effect=fake_download):
        exit_code = verify_registry(registry)
    assert exit_code == 1  # pack_two's hash does not match


def test_load_packs_rejects_a_non_list_packs_key(tmp_path: Path):
    registry = tmp_path / "registry.yaml"
    registry.write_text("packs: not-a-list\n")
    with pytest.raises(ValueError, match="must be a list"):
        load_packs(registry)


def test_load_packs_rejects_a_non_mapping_pack_entry(tmp_path: Path):
    registry = tmp_path / "registry.yaml"
    registry.write_text("packs:\n  - just_a_string\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_packs(registry)


def test_verify_registry_filters_by_gnet_id(tmp_path: Path):
    registry = tmp_path / "registry.yaml"
    registry.write_text("packs: []\n")
    with patch(
        "scripts.verify_curated_pack_pins.load_packs",
        return_value=[{"gnet_id": 1, "name": "pack_one", "gnet_revision": 1, "dashboard_sha256": "a" * 64}],
    ):
        exit_code = verify_registry(registry, gnet_id=999)
    assert exit_code == 1  # no entry with that gnet_id
