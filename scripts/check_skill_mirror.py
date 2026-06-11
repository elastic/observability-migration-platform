#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Verify that .claude/skills/ and .cursor/skills/ are byte-identical mirrors.

The only permitted difference is the self-reference prefix: ~/.claude/ in the
.claude tree versus ~/.cursor/ in the .cursor tree.  Any other divergence is
reported as an error.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CLAUDE_SKILLS = Path(".claude/skills")
CURSOR_SKILLS = Path(".cursor/skills")


def _collect_files(base: Path) -> dict[str, Path]:
    """Return {relative_posix_path: absolute_path} for all files under *base*."""
    if not base.exists():
        return {}
    return {
        p.relative_to(base).as_posix(): p
        for p in base.rglob("*")
        if p.is_file()
    }


def _normalise_cursor(content: str) -> str:
    """Replace ~/.cursor/ with ~/.claude/ so both copies can be compared."""
    return content.replace("~/.cursor/", "~/.claude/")


def check_mirror(root: Path) -> list[str]:
    """Return list of human-readable error strings; empty = clean."""
    root = root.expanduser().resolve()

    claude_base = root / CLAUDE_SKILLS
    cursor_base = root / CURSOR_SKILLS

    claude_files = _collect_files(claude_base)
    cursor_files = _collect_files(cursor_base)

    errors: list[str] = []

    claude_set = set(claude_files)
    cursor_set = set(cursor_files)

    for rel in sorted(claude_set - cursor_set):
        errors.append(f"MISSING from .cursor/skills/: {rel}")

    for rel in sorted(cursor_set - claude_set):
        errors.append(f"EXTRA in .cursor/skills/ (not in .claude/skills/): {rel}")

    for rel in sorted(claude_set & cursor_set):
        claude_content = claude_files[rel].read_text(encoding="utf-8")
        cursor_content = cursor_files[rel].read_text(encoding="utf-8")
        cursor_normalised = _normalise_cursor(cursor_content)

        if claude_content != cursor_normalised:
            diff_lines = list(
                difflib.unified_diff(
                    claude_content.splitlines(keepends=True),
                    cursor_normalised.splitlines(keepends=True),
                    fromfile=f".claude/skills/{rel}",
                    tofile=f".cursor/skills/{rel} (normalised)",
                )
            )
            diff_text = "".join(diff_lines)
            errors.append(f"CONTENT MISMATCH: {rel}\n{diff_text}")

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
