#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Structural + CLI-flag validation for the skills in .claude/skills/.

Skills are markdown instruction files, not executable code, so we cannot
"unit test" their semantic output without an LLM.  But a large, high-value
surface *is* deterministically verifiable, and that is what this script checks
for every ``SKILL.md`` (the ``.cursor`` mirror is kept byte-identical by
``check_skill_mirror.py``, so validating one tree is sufficient):

1. Frontmatter validity   — parses as YAML; non-empty ``name`` + ``description``.
2. ``name`` matches dir    — ``name:`` equals its folder name.
3. Description budget      — descriptions feed skill-selection; cap the length.
4. Cross-reference integ.  — every ``[[skill]]`` / ```skill` skill`` resolves to a real skill.
5. Referenced paths exist  — repo-relative ``docs/``/``scripts/`` files and
                             ``~/.claude/skills/<own-skill>/`` files a skill points at are real.
6. Quoted-command validity — every ``obs-migrate ...`` line inside a ```bash``` block
                             uses a real subcommand and only flags the argparse parser knows.

Returns 0 when clean, 1 when any check fails.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

CLAUDE_SKILLS = Path(".claude/skills")

# Descriptions are loaded into the model's context for skill selection; keep
# them bounded so a long one cannot quietly blow the budget. Today's longest is
# ~640 chars; the cap leaves headroom while still catching runaway growth.
MAX_DESCRIPTION_CHARS = 700

# Generic skills this repo deliberately builds on but does not ship (they live
# in the user's global skill set). References to them are intentional, not rot,
# so cross-reference and ~/.claude path checks treat them as "known".
KNOWN_EXTERNAL_SKILLS = frozenset({"chrome-devtools-debugging"})

# Top-level repo directories whose referenced files we expect to exist on disk.
# Limited on purpose: prose freely mentions bare dir names (e.g. "no scripts/
# directory is needed"), which we must NOT treat as file references.
REPO_PATH_PREFIXES = ("docs", "scripts")

# Shell tokens that end an ``obs-migrate`` invocation; flags after them belong
# to a different command (e.g. ``obs-migrate audit-rules ... | jq --raw-output``).
SHELL_TERMINATORS = {"|", "||", "&&", ";", ">", ">>", "<", "&"}


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #
def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_block, body). frontmatter_block is None if absent."""
    if not text.startswith("---"):
        return None, text
    lines = text.splitlines(keepends=True)
    # lines[0] is the opening '---'; find the closing fence.
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\n") == "---":
            return "".join(lines[1:idx]), "".join(lines[idx + 1 :])
    return None, text


# --------------------------------------------------------------------------- #
# Skill discovery
# --------------------------------------------------------------------------- #
def _skill_files(claude_base: Path) -> dict[str, Path]:
    """Return {skill_name: SKILL.md path} for every skill directory."""
    if not claude_base.exists():
        return {}
    out: dict[str, Path] = {}
    for skill_md in sorted(claude_base.glob("*/SKILL.md")):
        out[skill_md.parent.name] = skill_md
    return out


# --------------------------------------------------------------------------- #
# Cross-references
# --------------------------------------------------------------------------- #
_WIKILINK_RE = re.compile(r"\[\[([a-z][a-z0-9-]*)\]\]")
# A backtick-quoted skill-slug immediately qualified by the word "skill".
_SKILL_PHRASE_RE = re.compile(r"`([a-z][a-z0-9-]*)`\s+skill\b", re.IGNORECASE)


def _cross_reference_errors(rel: str, text: str, known_skills: set[str]) -> list[str]:
    errors: list[str] = []
    referenced: set[str] = set()
    referenced.update(_WIKILINK_RE.findall(text))
    referenced.update(name.lower() for name in _SKILL_PHRASE_RE.findall(text))
    for name in sorted(referenced):
        if name not in known_skills and name not in KNOWN_EXTERNAL_SKILLS:
            errors.append(f"{rel}: dangling skill cross-reference -> '{name}' (no such skill directory)")
    return errors


# --------------------------------------------------------------------------- #
# Referenced paths
# --------------------------------------------------------------------------- #
_REPO_PATH_RE = re.compile(
    r"(?<![\w/])(?:" + "|".join(REPO_PATH_PREFIXES) + r")/[A-Za-z0-9_./-]+"
)
_CLAUDE_SKILL_PATH_RE = re.compile(r"~/\.claude/skills/([a-z][a-z0-9-]*)/([A-Za-z0-9_./-]+)")

_TRAILING_PUNCT = ".,);:`"


def _looks_like_file(candidate: str) -> bool:
    """True if the path has a real basename with an alphanumeric extension."""
    base = os.path.basename(candidate)
    name, ext = os.path.splitext(base)
    return bool(name) and len(ext) > 1 and ext[1:].isalnum()


def _referenced_path_errors(
    rel: str, text: str, root: Path, known_skills: set[str]
) -> list[str]:
    errors: list[str] = []

    for raw in _REPO_PATH_RE.findall(text):
        candidate = raw.rstrip(_TRAILING_PUNCT)
        if not _looks_like_file(candidate):
            continue  # bare dir mention ("scripts/", "scripts/...") — not a file reference
        if not (root / candidate).exists():
            errors.append(f"{rel}: referenced path does not exist -> '{candidate}'")

    for skill_name, raw_rest in _CLAUDE_SKILL_PATH_RE.findall(text):
        if skill_name not in known_skills:
            continue  # external/generic skill, not shipped here — out of our control
        rest = raw_rest.rstrip(_TRAILING_PUNCT)
        target = root / CLAUDE_SKILLS / skill_name / rest
        if not target.exists():
            errors.append(
                f"{rel}: referenced skill path does not exist -> "
                f"'~/.claude/skills/{skill_name}/{rest}'"
            )

    return errors


# --------------------------------------------------------------------------- #
# CLI-flag validation
# --------------------------------------------------------------------------- #
_BASH_FENCE_RE = re.compile(r"^```+\s*(\w+)?\s*$")


_SHELL_LANGS = ("bash", "sh", "shell")


def _bash_blocks(text: str) -> list[str]:
    """Return the contents of every fenced ```bash``` (or sh/shell) code block."""
    blocks: list[str] = []
    open_lang: str | None = None  # language of the block we are currently inside
    buf: list[str] = []
    for line in text.splitlines():
        fence = _BASH_FENCE_RE.match(line.strip())
        if fence is None:
            if open_lang is not None:
                buf.append(line)
            continue
        if open_lang is None:
            # Opening fence.
            open_lang = (fence.group(1) or "").lower()
            buf = []
        else:
            # Closing fence.
            if open_lang in _SHELL_LANGS:
                blocks.append("\n".join(buf))
            open_lang = None
    return blocks


def _join_continuations(block: str) -> list[str]:
    """Join backslash-continued lines into single logical command lines."""
    logical: list[str] = []
    buf = ""
    for line in block.splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
        else:
            logical.append(buf + line)
            buf = ""
    if buf:
        logical.append(buf)
    return logical


def _tokenize(line: str) -> list[str]:
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return line.split()


def _flag_errors_for_command(tokens: list[str], flag_map: dict[str, set[str]]) -> list[str]:
    """Validate one obs-migrate invocation starting at tokens[0] == 'obs-migrate'."""
    errors: list[str] = []
    rest = tokens[1:]
    if not rest:
        return errors

    # Resolve the subcommand (first non-flag token).
    subcommand: str | None = None
    idx = 0
    if not rest[0].startswith("-"):
        subcommand = rest[0]
        idx = 1
        if subcommand not in flag_map:
            return [f"unknown 'obs-migrate' subcommand: '{subcommand}'"]

    allowed = flag_map.get(subcommand, set()) if subcommand else flag_map["__global__"]

    for tok in rest[idx:]:
        if tok in SHELL_TERMINATORS:
            break  # a piped/chained command begins; its flags are not ours
        if not tok.startswith("-") or tok == "-" or tok == "--":
            continue
        flag = tok.split("=", 1)[0]
        if flag not in allowed:
            cmd = f"obs-migrate {subcommand}" if subcommand else "obs-migrate"
            errors.append(f"unknown flag '{flag}' for '{cmd}'")
    return errors


def _cli_flag_errors(rel: str, text: str, flag_map: dict[str, set[str]]) -> list[str]:
    errors: list[str] = []
    for block in _bash_blocks(text):
        for line in _join_continuations(block):
            if "obs-migrate" not in line:
                continue
            tokens = _tokenize(line)
            for i, tok in enumerate(tokens):
                if tok == "obs-migrate":
                    for err in _flag_errors_for_command(tokens[i:], flag_map):
                        errors.append(f"{rel}: {err}")
    return errors


def _build_flag_map() -> dict[str, set[str]]:
    """Introspect the obs-migrate argparse parser into {subcommand: {flags}}.

    Includes a '__global__' entry for the top-level parser's options.

    Assumes a single subparser level: every subcommand's flags are collected
    directly off its parser (today ``cluster`` etc. use a positional ``action``
    arg, not nested subparsers). If a leaf command ever gains its own
    ``add_subparsers``, recurse here or its nested flags will falsely fail.
    """
    from observability_migration.app.cli import _build_parser

    parser = _build_parser()
    flag_map: dict[str, set[str]] = {}

    global_opts: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                opts: set[str] = set()
                for sub_action in subparser._actions:
                    opts.update(sub_action.option_strings)
                flag_map[name] = opts
        else:
            global_opts.update(action.option_strings)

    flag_map["__global__"] = global_opts
    for name in list(flag_map):
        if name != "__global__":
            flag_map[name] |= global_opts
    return flag_map


# --------------------------------------------------------------------------- #
# Top-level check
# --------------------------------------------------------------------------- #
def check_structure(root: Path) -> list[str]:
    """Return list of human-readable error strings; empty = clean."""
    root = root.expanduser().resolve()
    claude_base = root / CLAUDE_SKILLS

    skill_files = _skill_files(claude_base)
    known_skills = set(skill_files)

    errors: list[str] = []
    flag_map: dict[str, set[str]] | None = None

    for name in sorted(skill_files):
        path = skill_files[name]
        rel = f".claude/skills/{name}/SKILL.md"
        text = path.read_text(encoding="utf-8")

        # 1. Frontmatter validity
        block, body = _split_frontmatter(text)
        if block is None:
            errors.append(f"{rel}: missing or unterminated YAML frontmatter")
            continue
        try:
            meta = yaml.safe_load(block)
        except yaml.YAMLError as exc:
            errors.append(f"{rel}: frontmatter is not valid YAML ({exc})")
            continue
        if not isinstance(meta, dict):
            errors.append(f"{rel}: frontmatter does not parse to a mapping")
            continue

        fm_name = meta.get("name")
        description = meta.get("description")
        if not isinstance(fm_name, str) or not fm_name.strip():
            errors.append(f"{rel}: frontmatter 'name' is missing or empty")
            fm_name = None
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{rel}: frontmatter 'description' is missing or empty")
            description = None

        # 2. name matches directory
        if fm_name is not None and fm_name != name:
            errors.append(f"{rel}: frontmatter name '{fm_name}' != directory '{name}'")

        # 3. Description budget
        if description is not None and len(description) > MAX_DESCRIPTION_CHARS:
            errors.append(
                f"{rel}: description is {len(description)} chars "
                f"(> {MAX_DESCRIPTION_CHARS} limit)"
            )

        # 4. Cross-reference integrity
        errors.extend(_cross_reference_errors(rel, text, known_skills))

        # 5. Referenced paths exist
        errors.extend(_referenced_path_errors(rel, text, root, known_skills))

        # 6. Quoted-command validity
        if "obs-migrate" in body:
            if flag_map is None:
                flag_map = _build_flag_map()
            errors.extend(_cli_flag_errors(rel, body, flag_map))

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

    errors = check_structure(args.root)
    if errors:
        print("Skill structure check FAILED:", file=sys.stderr)
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
