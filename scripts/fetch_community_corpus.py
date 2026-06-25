#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Fetch the pinned community Grafana dashboards for benchmark/regression runs.

Reads ``parity-rig/benchmark/community_corpus.json`` and downloads each pinned
dashboard from grafana.com at its exact ``revision``, verifying the recorded
``sha256`` so a run is reproducible and tamper-evident. The third-party JSON is
deliberately NOT committed (grafana.com marketplace dashboards drift and add
noise; only the pin is committed) — this script materializes them on demand.

The output directory can be fed straight to the migration + gates::

    python scripts/fetch_community_corpus.py --output-dir /tmp/community
    grafana-migrate --source files --input-dir /tmp/community \\
        --output-dir /tmp/community_out --assets dashboards
    PYTHONPATH=parity-rig python -m verifier.scorecard \\
        --migration-out /tmp/community_out/dashboards \\
        --baseline parity-rig/benchmark/fidelity_baseline_community.json --update

Requires network access at fetch time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "parity-rig" / "benchmark" / "community_corpus.json"
DOWNLOAD_URL = "https://grafana.com/api/dashboards/{id}/revisions/{revision}/download"


def _download(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "obs-migrate-community-corpus"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def canonical_sha256(raw: bytes) -> str:
    """Checksum a canonical (sort_keys, compact) form of the dashboard JSON.

    grafana.com re-serializes the same revision with differing key order /
    whitespace across requests, so raw-byte hashes are not stable. Canonical JSON
    is, which makes the pin tamper-evident without being serialization-fragile.
    """
    return hashlib.sha256(
        json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def fetch_corpus(manifest_path: Path, output_dir: Path, *, verify: bool = True) -> int:
    manifest = json.loads(manifest_path.read_text())
    output_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for entry in manifest.get("dashboards", []):
        url = DOWNLOAD_URL.format(id=entry["id"], revision=entry["revision"])
        try:
            raw = _download(url)
        except Exception as exc:
            print(f"FAIL {entry['slug']}: download error: {exc}", file=sys.stderr)
            failures += 1
            continue
        digest = canonical_sha256(raw)
        if verify and digest != entry.get("sha256"):
            print(
                f"FAIL {entry['slug']}: sha256 mismatch "
                f"(expected {entry.get('sha256', '')[:12]}, got {digest[:12]})",
                file=sys.stderr,
            )
            failures += 1
            continue
        (output_dir / f"{entry['slug']}.json").write_bytes(raw)
        print(f"OK   {entry['slug']:20} id={entry['id']} rev={entry['revision']} ({entry['panels']} panels)")
    print(f"--- {len(manifest.get('dashboards', [])) - failures} fetched, {failures} failed ---")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--no-verify", action="store_true",
        help="Skip sha256 verification (e.g. to refresh pins after an intentional bump).",
    )
    args = parser.parse_args(argv)
    return fetch_corpus(args.manifest, args.output_dir, verify=not args.no_verify)


if __name__ == "__main__":
    raise SystemExit(main())
