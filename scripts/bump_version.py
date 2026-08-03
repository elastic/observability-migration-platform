#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Bump ``[project].version`` in pyproject.toml, sync operator doc pins, refresh uv.lock."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([a-zA-Z0-9.-]*)?$")

# Operator-facing files that show an example PyPI pin and/or git tag install.
# Patterns are package-scoped so unrelated versions (e.g. kb-dashboard 0.4.1) are
# left alone. ``make bump-version`` keeps these in lockstep with pyproject.toml.
DOC_VERSION_RELATIVE_PATHS: tuple[str, ...] = (
    "README.md",
    "docs/command-contract.md",
    ".claude/skills/install-obs-migrate/SKILL.md",
    ".cursor/skills/install-obs-migrate/SKILL.md",
    ".agents/skills/install-obs-migrate/SKILL.md",
)

# elastic-observability-migration[all]==0.4.0rc1  (and quoted variants)
_PIN_EQ_RE = re.compile(
    r"(elastic-observability-migration\[all\]==)(\d+\.\d+\.\d+[a-zA-Z0-9.-]*)"
)
# ...observability-migration-platform.git@v0.4.0rc1
_GIT_TAG_RE = re.compile(
    r"(observability-migration-platform\.git@v)(\d+\.\d+\.\d+[a-zA-Z0-9.-]*)"
)


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


def sync_doc_version_pins(text: str, new_version: str) -> tuple[str, int]:
    """Rewrite package pin / git-tag examples to ``new_version``.

    Returns ``(updated_text, replacement_count)``.
    """
    count = 0

    def _eq(match: re.Match[str]) -> str:
        nonlocal count
        if match.group(2) == new_version:
            return match.group(0)
        count += 1
        return f"{match.group(1)}{new_version}"

    def _tag(match: re.Match[str]) -> str:
        nonlocal count
        if match.group(2) == new_version:
            return match.group(0)
        count += 1
        return f"{match.group(1)}{new_version}"

    updated = _PIN_EQ_RE.sub(_eq, text)
    updated = _GIT_TAG_RE.sub(_tag, updated)
    return updated, count


def collect_doc_pin_versions(text: str) -> set[str]:
    """Return every package pin / git-tag version found in ``text``."""
    found: set[str] = set()
    found.update(m.group(2) for m in _PIN_EQ_RE.finditer(text))
    found.update(m.group(2) for m in _GIT_TAG_RE.finditer(text))
    return found


def sync_operator_doc_pins(root: Path, new_version: str) -> list[str]:
    """Update DOC_VERSION_RELATIVE_PATHS under ``root``; return changed paths."""
    changed: list[str] = []
    for rel in DOC_VERSION_RELATIVE_PATHS:
        path = root / rel
        if not path.is_file():
            print(f"warning: skip missing doc pin file: {rel}", file=sys.stderr)
            continue
        original = path.read_text(encoding="utf-8")
        updated, n = sync_doc_version_pins(original, new_version)
        if n == 0 and not collect_doc_pin_versions(original):
            print(
                f"warning: no package version pins found in {rel} "
                "(expected ==X.Y.Z and/or git@vX.Y.Z examples)",
                file=sys.stderr,
            )
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed.append(rel)
            print(f"{rel}: synced package pins -> {new_version} ({n} replacement(s))")
        else:
            pins = collect_doc_pin_versions(original)
            if pins and pins != {new_version}:
                # Should not happen if regexes applied; defensive.
                print(
                    f"warning: {rel} still has unexpected pins {sorted(pins)}",
                    file=sys.stderr,
                )
            else:
                print(f"{rel}: package pins already {new_version}")
    return changed


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
        help="Update pyproject.toml / docs only (do not run uv lock)",
    )
    parser.add_argument(
        "--skip-docs",
        action="store_true",
        help="Do not sync README / command-contract / install-skill version pins",
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

    if not args.skip_docs:
        sync_operator_doc_pins(args.root, args.version)

    if not args.skip_lock:
        subprocess.run(["uv", "lock"], cwd=args.root, check=True)
        print("uv.lock refreshed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
