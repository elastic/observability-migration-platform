#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Bump ``[project].version`` in pyproject.toml and refresh uv.lock."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([a-zA-Z0-9.-]*)?$")


def update_pyproject_version(text: str, new_version: str) -> str:
    """Replace the ``[project]`` version field; raise if not found or ambiguous."""
    pattern = re.compile(r'(?m)^version = "[^"]*"$')
    matches = list(pattern.finditer(text))
    if not matches:
        raise ValueError("no top-level version = \"...\" line found in pyproject.toml")
    # Prefer the first version= line (setuptools [project] block is first).
    match = matches[0]
    return text[: match.start()] + f'version = "{new_version}"' + text[match.end() :]


def read_pyproject_version(text: str) -> str:
    match = re.search(r'(?m)^version = "([^"]*)"$', text)
    if match is None:
        raise ValueError("no version = \"...\" line found in pyproject.toml")
    return match.group(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "version",
        help="New version (e.g. 0.4.0)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root (default: inferred from script location)",
    )
    parser.add_argument(
        "--skip-lock",
        action="store_true",
        help="Update pyproject.toml only (do not run uv lock)",
    )
    args = parser.parse_args(argv)

    if not VERSION_RE.match(args.version):
        print(
            f"error: invalid version {args.version!r}; expected X.Y.Z[.suffix]",
            file=sys.stderr,
        )
        return 2

    pyproject = args.root / "pyproject.toml"
    original = pyproject.read_text(encoding="utf-8")
    old = read_pyproject_version(original)
    updated = update_pyproject_version(original, args.version)
    if updated == original and old == args.version:
        print(f"version already {args.version}")
    else:
        pyproject.write_text(updated, encoding="utf-8")
        print(f"pyproject.toml: {old} -> {args.version}")

    if not args.skip_lock:
        subprocess.run(["uv", "lock"], cwd=args.root, check=True)
        print("uv.lock refreshed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
