#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Verify that repo skill directories are byte-identical mirrors.

The .claude/skills/ tree is canonical.  The only permitted difference in a
mirror is the self-reference prefix for global skill paths, e.g. ~/.claude/ in
the canonical tree versus ~/.cursor/ or ~/.codex/ in mirror trees.  Any other
divergence is reported as an error.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CANONICAL_SKILLS = Path(".claude/skills")
CANONICAL_SELF_REFERENCE_PREFIX = "~/.claude/"


MIRROR_SKILLS = (
    (Path(".cursor/skills"), "~/.cursor/"),
    (Path(".agents/skills"), "~/.codex/"),
)


def _collect_files(base: Path) -> dict[str, Path]:
    """Return {relative_posix_path: absolute_path} for all files under *base*."""
    if not base.exists():
        return {}
    return {
        p.relative_to(base).as_posix(): p
        for p in base.rglob("*")
        if p.is_file()
    }


def _normalise_mirror(content: str, self_reference_prefix: str) -> str:
    """Replace a mirror's global skill prefix with the canonical prefix."""
    return content.replace(self_reference_prefix, CANONICAL_SELF_REFERENCE_PREFIX)


def check_mirror(root: Path) -> list[str]:
    """Return list of human-readable error strings; empty = clean."""
    root = root.expanduser().resolve()

    canonical_base = root / CANONICAL_SKILLS
    canonical_files = _collect_files(canonical_base)

    errors: list[str] = []

    canonical_set = set(canonical_files)

    for mirror_path, self_reference_prefix in MIRROR_SKILLS:
        mirror_base = root / mirror_path
        mirror_files = _collect_files(mirror_base)
        mirror_set = set(mirror_files)
        mirror_label = mirror_path.as_posix()
        canonical_label = CANONICAL_SKILLS.as_posix()

        for rel in sorted(canonical_set - mirror_set):
            errors.append(f"MISSING from {mirror_label}/: {rel}")

        for rel in sorted(mirror_set - canonical_set):
            errors.append(f"EXTRA in {mirror_label}/ (not in {canonical_label}/): {rel}")

        for rel in sorted(canonical_set & mirror_set):
            canonical_content = canonical_files[rel].read_text(encoding="utf-8")
            mirror_content = mirror_files[rel].read_text(encoding="utf-8")
            mirror_normalised = _normalise_mirror(mirror_content, self_reference_prefix)

            if canonical_content != mirror_normalised:
                diff_lines = list(
                    difflib.unified_diff(
                        canonical_content.splitlines(keepends=True),
                        mirror_normalised.splitlines(keepends=True),
                        fromfile=f"{canonical_label}/{rel}",
                        tofile=f"{mirror_label}/{rel} (normalised)",
                    )
                )
                diff_text = "".join(diff_lines)
                errors.append(f"CONTENT MISMATCH in {mirror_label}: {rel}\n{diff_text}")

    return errors


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Accepts --root PATH. Returns 0 = clean, 1 = errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root to scan (default: inferred from this script).",
    )
    args = parser.parse_args(argv)

    errors = check_mirror(args.root)
    if errors:
        print("Skill mirror check FAILED:", file=sys.stderr)
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
