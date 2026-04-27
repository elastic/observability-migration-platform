#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Verify first-party source/config files carry Elastic license headers."""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HEADER_LINES = (
    "Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.",
    "SPDX-License-Identifier: Elastic-2.0",
)

INCLUDE_PATTERNS = (
    "observability_migration/*.py",
    "observability_migration/**/*.py",
    "tests/*.py",
    "tests/**/*.py",
    "scripts/*.py",
    "scripts/**/*.py",
    "scripts/*.sh",
    "scripts/**/*.sh",
    ".github/**/*.yml",
    ".github/**/*.yaml",
    "infra/**/*.yml",
    "infra/**/*.yaml",
    "infra/**/*.conf",
    "examples/*.py",
    "examples/**/*.py",
    "examples/*.yaml",
    "examples/**/*.yaml",
    "examples/*.yml",
    "examples/**/*.yml",
    "examples/*.cue",
    "examples/**/*.cue",
    "pyproject.toml",
    "MANIFEST.in",
    "gitleaks.toml",
    ".pre-commit-config.yaml",
    "*.env.example",
)

EXCLUDE_PATTERNS = (
    "docs/licenses/**",
    "docs/dashboards/**",
    "licenses/**",
)

EXCLUDE_DIRECTORIES = {
    ".git",
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".serena",
    ".tox",
    ".venv",
    ".venv-licensing",
    "build",
    "dist",
    "validation",
    "__pycache__",
}


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _matches_any(relative_path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(relative_path, pattern) for pattern in patterns)


def _comment_prefix(path: Path) -> str:
    if path.suffix == ".cue":
        return "//"
    return "#"


def _expected_header(path: Path) -> list[str]:
    prefix = _comment_prefix(path)
    return [f"{prefix} {line}" for line in HEADER_LINES]


def _header_start(lines: list[str]) -> int:
    index = 0
    if lines and lines[0].startswith("#!"):
        index += 1
    if len(lines) > index and "coding" in lines[index] and lines[index].lstrip().startswith("#"):
        index += 1
    return index


def iter_candidate_paths(root: Path = REPO_ROOT) -> list[Path]:
    """Return first-party source/config files that should carry a header."""
    root = root.expanduser()
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDE_DIRECTORIES)
        current_dir = Path(dirpath)
        for filename in sorted(filenames):
            path = current_dir / filename
            relative_path = _relative_posix(path, root)
            if _matches_any(relative_path, EXCLUDE_PATTERNS):
                continue
            if _matches_any(relative_path, INCLUDE_PATTERNS):
                candidates.append(path)
    return candidates


def has_valid_header(path: Path) -> bool:
    """Return True when a file starts with the expected header."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    start = _header_start(lines)
    return lines[start : start + len(HEADER_LINES)] == _expected_header(path)


def find_missing_headers(paths: list[Path]) -> list[Path]:
    return [path for path in paths if not has_valid_header(path)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root to scan (default: inferred from this script).",
    )
    args = parser.parse_args(argv)

    candidates = iter_candidate_paths(args.root)
    missing = find_missing_headers(candidates)
    if missing:
        print("Source header check FAILED. Missing Elastic headers:", file=sys.stderr)
        root = args.root.resolve()
        for path in missing:
            print(f"  - {_relative_posix(path.resolve(), root)}", file=sys.stderr)
        return 1

    print(f"Source header check passed: {len(candidates)} checked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
