#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Fail if a release tag or operator doc pins disagree with pyproject.toml."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_bump_module():
    path = REPO_ROOT / "scripts" / "bump_version.py"
    spec = importlib.util.spec_from_file_location("bump_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize_tag(tag: str) -> str:
    """Strip a leading ``v`` from Git tags (``v1.2.3`` → ``1.2.3``)."""
    tag = tag.strip()
    if tag.startswith("v") or tag.startswith("V"):
        return tag[1:]
    return tag


def read_pyproject_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def check_tag_matches_project(tag: str, project_version: str) -> None:
    """Raise SystemExit(1) when tag and project version disagree."""
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


def check_operator_doc_pins(root: Path, project_version: str) -> None:
    """Raise SystemExit(1) when README/docs/skill pins drift from pyproject."""
    bump = _load_bump_module()
    errors: list[str] = []
    for rel in bump.DOC_VERSION_RELATIVE_PATHS:
        path = root / rel
        if not path.is_file():
            errors.append(f"missing operator doc pin file: {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        pins = bump.collect_doc_pin_versions(text)
        if not pins:
            errors.append(
                f"{rel}: no elastic-observability-migration[all]==… or "
                f"git@v… pin examples found (expected at least one)"
            )
            continue
        unexpected = sorted(pins - {project_version})
        if unexpected:
            errors.append(
                f"{rel}: package pin(s) {unexpected} do not match "
                f"pyproject.toml version {project_version!r}"
            )
    if errors:
        print("error: operator install docs are out of sync with the package version:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            f"\nFix with: make bump-version VERSION={project_version}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        help="Git tag name (e.g. v1.2.3 or 1.2.3). Optional when using --docs-only.",
    )
    parser.add_argument(
        "--docs-only",
        action="store_true",
        help="Only verify README / command-contract / install-skill pins.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root (default: inferred from script location)",
    )
    args = parser.parse_args(argv)
    project_version = read_pyproject_version(args.root)

    if not args.docs_only:
        if not args.tag:
            print("error: --tag is required unless --docs-only is set", file=sys.stderr)
            return 2
        check_tag_matches_project(args.tag, project_version)
        print(f"ok: tag {args.tag!r} matches pyproject version {project_version}")

    check_operator_doc_pins(args.root, project_version)
    print(f"ok: operator doc pins match pyproject version {project_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
