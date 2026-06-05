#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Fetch the version-pinned benchmark corpus described by a manifest.

Reads ``parity-rig/benchmark/corpus.manifest.yaml`` (or ``--manifest``), and for
every source in the selected ``--slice`` clones the source repo at its exact
pinned commit SHA (shallow), then copies the dashboard JSON matched by the
source ``globs`` into ``--output-dir``. Files are flattened and prefixed with the
source id so provenance is unambiguous and names never collide.

The result is a directory of Grafana dashboard JSON that
``obs-migrate migrate --source grafana --input-dir <output-dir>`` can consume
directly, plus a ``corpus_lock.json`` recording exactly which repo/SHA/path each
dashboard came from -- so a benchmark run is reproducible and auditable.

This script only writes to ``--output-dir``; it never touches the source repos
beyond a read-only shallow clone into a temp dir.

Usage:
    python scripts/fetch_benchmark_corpus.py --slice representative \\
        --output-dir /tmp/bench-corpus

    # Whole pinned corpus
    python scripts/fetch_benchmark_corpus.py --slice full --output-dir /tmp/bench-corpus

Requires ``git`` on PATH and network access at fetch time. Embedded-dashboard
extraction (kube-prometheus ConfigMap YAML) is handled when such a source is
enabled.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a project dependency
    print("ERROR: PyYAML is required (pip install pyyaml).", file=sys.stderr)
    raise SystemExit(2) from None

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "parity-rig" / "benchmark" / "corpus.manifest.yaml"


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _shallow_clone_at_sha(repo: str, sha: str, dest: Path) -> None:
    """Clone *repo* and check out *sha* with as little history as possible."""
    dest.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], cwd=dest)
    _run(["git", "remote", "add", "origin", repo], cwd=dest)
    # Fetch just the pinned commit when the server allows it; fall back to a
    # shallow fetch of the default branch otherwise.
    try:
        _run(["git", "fetch", "-q", "--depth", "1", "origin", sha], cwd=dest)
        _run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest)
    except subprocess.CalledProcessError:
        _run(["git", "fetch", "-q", "--depth", "50", "origin"], cwd=dest)
        _run(["git", "checkout", "-q", sha], cwd=dest)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in text)


def _iter_matches(tree: Path, globs: list[str]):
    for pattern in globs:
        # Support both explicit paths and glob patterns relative to the repo root.
        for path in sorted(tree.glob(pattern)) if any(ch in pattern for ch in "*?[") else [tree / pattern]:
            if path.is_file():
                yield pattern, path


def _looks_like_dashboard(obj: Any) -> bool:
    return isinstance(obj, dict) and ("panels" in obj or "rows" in obj)


def _extract_embedded_dashboards(path: Path) -> list[tuple[str, dict]]:
    """Pull dashboard JSON embedded in a ConfigMap / YAML (kube-prometheus)."""
    out: list[tuple[str, dict]] = []
    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except yaml.YAMLError:
        return out
    for doc in docs:
        data = (doc or {}).get("data", {}) if isinstance(doc, dict) else {}
        for key, raw in (data or {}).items():
            if not key.endswith(".json"):
                continue
            try:
                obj = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if _looks_like_dashboard(obj):
                out.append((key, obj))
    return out


def fetch_corpus(manifest_path: Path, slice_name: str, output_dir: Path) -> dict[str, Any]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    slices = manifest.get("slices", {})
    slice_name = slice_name or manifest.get("default_slice", "representative")
    if slice_name not in slices:
        raise SystemExit(f"ERROR: slice '{slice_name}' not in manifest slices {list(slices)}")
    wanted_ids = slices[slice_name].get("sources", [])
    by_id = {s["id"]: s for s in manifest.get("sources", [])}

    output_dir.mkdir(parents=True, exist_ok=True)
    for existing in output_dir.glob("*.json"):
        existing.unlink()

    lock: dict[str, Any] = {
        "manifest": str(manifest_path.relative_to(ROOT)) if manifest_path.is_relative_to(ROOT) else str(manifest_path),
        "slice": slice_name,
        "dashboards": [],
    }
    copied = 0
    with tempfile.TemporaryDirectory(prefix="bench-corpus-clone-") as tmp:
        tmp_root = Path(tmp)
        for source_id in wanted_ids:
            source = by_id.get(source_id)
            if source is None:
                print(f"WARN: slice references unknown source '{source_id}', skipping", file=sys.stderr)
                continue
            if source.get("enabled") is False:
                print(f"skip: source '{source_id}' is disabled in the manifest", file=sys.stderr)
                continue
            sha = (source.get("sha") or "").strip()
            if not sha:
                print(f"skip: source '{source_id}' has no pinned sha", file=sys.stderr)
                continue
            clone_dir = tmp_root / _slug(source_id)
            print(f"fetch: {source_id} @ {sha[:12]} ...", file=sys.stderr)
            _shallow_clone_at_sha(source["repo"], sha, clone_dir)
            for pattern, path in _iter_matches(clone_dir, source.get("globs", [])):
                if path.suffix == ".json":
                    obj = json.loads(path.read_text(encoding="utf-8"))
                    dashboards = [(path.name, obj)] if _looks_like_dashboard(obj) else []
                else:
                    dashboards = _extract_embedded_dashboards(path)
                for name, dash in dashboards:
                    out_name = f"{_slug(source_id)}__{_slug(Path(name).stem)}.json"
                    (output_dir / out_name).write_text(json.dumps(dash), encoding="utf-8")
                    copied += 1
                    lock["dashboards"].append(
                        {
                            "file": out_name,
                            "source_id": source_id,
                            "repo": source["repo"],
                            "sha": sha,
                            "path": pattern if path.suffix != ".json" else str(path.relative_to(clone_dir)),
                            "title": dash.get("title", Path(name).stem),
                        }
                    )
    lock["count"] = copied
    (output_dir / "corpus_lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
    print(f"\nfetched {copied} dashboard(s) for slice '{slice_name}' -> {output_dir}", file=sys.stderr)
    return lock


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to the corpus manifest YAML.")
    p.add_argument("--slice", default="", help="Slice name from the manifest (default: manifest default_slice).")
    p.add_argument("--output-dir", required=True, help="Directory to write fetched dashboard JSON into.")
    args = p.parse_args(argv)
    fetch_corpus(Path(args.manifest), args.slice, Path(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
