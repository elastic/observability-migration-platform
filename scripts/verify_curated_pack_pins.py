#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Verify the curated-pack provenance pins in registry.yaml (issue #350).

Each entry in
``observability_migration/adapters/source/grafana/curated_packs/registry.yaml``
pins the exact grafana.com ``gnet_revision`` and canonical-JSON ``sha256`` the
pack authors read when writing that pack's ``pack.yaml`` overrides. This is a
**maintainer provenance check**, not a migration-time gate: it lets a
maintainer confirm a pack still matches its stated source dashboard (or
deliberately re-pin it) as the upstream dashboard evolves on grafana.com. It
is intentionally NOT run against operator dashboards at migration time -- a
pristine grafana.com download differs structurally from any real Grafana
instance's import/export (mutated ``id``/``uid``/``version``/etc.), so that
comparison would mismatch on every real migration. The actual risk a pin
guards against (a pack silently missing dashboard content) is instead caught
per-panel by the dropped-source-metric detection in ``panels.py``.

Mirrors the download + ``canonical_sha256`` pattern already proven by
``scripts/fetch_community_corpus.py`` for the benchmark corpus pins.

Requires network access. Not part of ``make test`` -- run manually (or from a
maintainer CI job) after touching a curated pack or its registry entry::

    python scripts/verify_curated_pack_pins.py
    python scripts/verify_curated_pack_pins.py --gnet-id 1860
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = (
    REPO_ROOT
    / "observability_migration"
    / "adapters"
    / "source"
    / "grafana"
    / "curated_packs"
    / "registry.yaml"
)
DOWNLOAD_URL = "https://grafana.com/api/dashboards/{id}/revisions/{revision}/download"


def _download(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "obs-migrate-curated-pack-pin-verify"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def canonical_sha256(raw: bytes) -> str:
    """Checksum a canonical (sort_keys, compact) form of the dashboard JSON.

    grafana.com re-serializes the same revision with differing key order /
    whitespace across requests, so raw-byte hashes are not stable. Canonical
    JSON is, which makes the pin tamper-evident without being
    serialization-fragile. Identical to
    ``scripts/fetch_community_corpus.py::canonical_sha256`` by design -- both
    pin the same kind of grafana.com download.
    """
    return hashlib.sha256(
        json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_packs(registry_path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(registry_path.read_text()) or {}
    packs = data.get("packs") or []
    if not isinstance(packs, list):
        raise ValueError(f"{registry_path}: 'packs' must be a list, got {type(packs).__name__}")
    for entry in packs:
        if not isinstance(entry, dict):
            raise ValueError(f"{registry_path}: each pack entry must be a mapping, got {entry!r}")
    return list(packs)


def verify_pack(pack: dict[str, Any]) -> tuple[bool, str]:
    gnet_id = pack.get("gnet_id")
    revision = pack.get("gnet_revision")
    expected = str(pack.get("dashboard_sha256") or "")
    name = pack.get("name") or f"gnet_{gnet_id}"
    if not gnet_id or not revision:
        return False, f"{name}: missing gnet_id/gnet_revision in registry entry"
    url = DOWNLOAD_URL.format(id=gnet_id, revision=revision)
    try:
        raw = _download(url)
        digest = canonical_sha256(raw)
    except Exception as exc:
        # Covers both transport failures (network/HTTP) and a malformed/
        # non-JSON response body -- either way this pack's pin cannot be
        # confirmed, and it must not abort the rest of the registry's checks.
        return False, f"{name} (id={gnet_id}, rev={revision}): verification error: {exc}"
    if digest != expected:
        return False, (
            f"{name} (id={gnet_id}, rev={revision}): sha256 mismatch "
            f"(registry has {expected[:12] or '(empty)'}, grafana.com now hashes to {digest[:12]})"
        )
    return True, f"{name} (id={gnet_id}, rev={revision}): OK"


def verify_registry(registry_path: Path, *, gnet_id: int | None = None) -> int:
    packs = load_packs(registry_path)
    if gnet_id is not None:
        packs = [p for p in packs if p.get("gnet_id") == gnet_id]
        if not packs:
            print(f"No registry entry with gnet_id={gnet_id}", file=sys.stderr)
            return 1
    elif not packs:
        # An empty registry is never a meaningful "all pins verified" --
        # most likely a parsing/path problem that would otherwise silently
        # print "0/0 pins verified" and exit 0.
        print(f"No pack entries found in {registry_path}", file=sys.stderr)
        return 1
    failures = 0
    for pack in packs:
        ok, message = verify_pack(pack)
        print(("OK   " if ok else "FAIL ") + message)
        if not ok:
            failures += 1
    print(f"--- {len(packs) - failures}/{len(packs)} pins verified ---")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--gnet-id", type=int, default=None,
        help="Verify only the entry with this gnet_id (default: verify every entry).",
    )
    args = parser.parse_args(argv)
    return verify_registry(args.registry, gnet_id=args.gnet_id)


if __name__ == "__main__":
    raise SystemExit(main())
