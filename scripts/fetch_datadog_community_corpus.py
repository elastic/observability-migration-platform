#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Fetch the pinned community DataDog integration dashboards for benchmark runs.

Reads ``parity-rig/benchmark/datadog_community_corpus.json`` and downloads each
pinned dashboard from the public ``DataDog/integrations-core`` repo (BSD-3-Clause)
at the manifest's fixed commit ``ref``, verifying the recorded canonical-JSON
``sha256`` so a run is reproducible and tamper-evident. The third-party JSON is
deliberately NOT committed (marketplace-noise rule, AGENTS.md); only the pin is —
this script materializes the dashboards on demand.

The output directory can be fed straight to the migration + gates::

    python scripts/fetch_datadog_community_corpus.py --output-dir /tmp/dd_community
    datadog-migrate --source files --input-dir /tmp/dd_community \\
        --output-dir /tmp/dd_community_out --assets dashboards
    PYTHONPATH=parity-rig python -m verifier.scorecard \\
        --migration-out /tmp/dd_community_out/dashboards \\
        --baseline parity-rig/benchmark/fidelity_baseline_datadog_community.json

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
DEFAULT_MANIFEST = REPO_ROOT / "parity-rig" / "benchmark" / "datadog_community_corpus.json"
DOWNLOAD_URL = "https://raw.githubusercontent.com/DataDog/integrations-core/{ref}/{path}"


def _download(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "obs-migrate-dd-community-corpus"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def canonical_sha256(raw: bytes) -> str:
    """Checksum a canonical (sort_keys, compact) form of the dashboard JSON.

    Pinning to canonical JSON (not raw bytes) makes the pin stable against
    whitespace / key-order reserialization while staying tamper-evident.
    """
    return hashlib.sha256(
        json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def fetch_corpus(manifest_path: Path, output_dir: Path, *, verify: bool = True) -> int:
    manifest = json.loads(manifest_path.read_text())
    ref = manifest["ref"]
    output_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for entry in manifest.get("dashboards", []):
        url = DOWNLOAD_URL.format(ref=ref, path=entry["path"])
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
        print(f"OK   {entry['slug']:26} ({entry['panels']} panels) {entry['path']}")
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
