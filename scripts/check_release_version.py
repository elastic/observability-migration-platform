#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Fail if a release tag does not match ``[project].version`` in pyproject.toml."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def normalize_tag(tag: str) -> str:
    """Strip a leading ``v`` from Git tags (``v0.4.0`` → ``0.4.0``)."""
    tag = tag.strip()
    if tag.startswith("v") or tag.startswith("V"):
        return tag[1:]
    return tag


def read_pyproject_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def check_tag_matches_project(tag: str, project_version: str) -> None:
    """Exit process with code 1 when tag and project version disagree."""
    normalized = normalize_tag(tag)
    if normalized != project_version:
        print(
            f"error: release tag {tag!r} (version {normalized!r}) does not match "
            f"pyproject.toml version {project_version!r}.\n"
            f"Bump with: make bump-version VERSION={normalized}\n"
            f"Or retag to: v{project_version}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        required=True,
        help="Git tag name (e.g. v0.4.0 or 0.4.0)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root (default: inferred from script location)",
    )
    args = parser.parse_args(argv)
    project_version = read_pyproject_version(args.root)
    check_tag_matches_project(args.tag, project_version)
    print(f"ok: tag {args.tag!r} matches pyproject version {project_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
