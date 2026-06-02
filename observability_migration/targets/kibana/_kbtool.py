# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Resolve how to invoke the external kb-dashboard tooling.

Prefers a locally-installed console script (the ``obs-migrate[kibana]`` extra),
falls back to a pinned ``uvx`` invocation, and otherwise raises a clear,
actionable error instead of a raw shell failure.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig

KB_DASHBOARD_TOOL_VERSION = "0.4.1"

_SUPPORTED_TOOLS = ("kb-dashboard-cli", "kb-dashboard-lint")


class KbToolUnavailableError(RuntimeError):
    """Raised when neither an installed tool nor uv is available."""


def _interpreter_script_dirs() -> list[str]:
    """Directories that hold console scripts for the running interpreter.

    When ``obs-migrate`` is launched via its venv path (``.venv/bin/obs-migrate``)
    without the venv being *activated*, the venv's scripts directory is not on
    ``PATH``, so ``shutil.which`` cannot see sibling console scripts such as
    ``kb-dashboard-cli``. We therefore also look next to the interpreter so the
    installed ``[kibana]`` extra is preferred over the uvx fallback regardless of
    activation.
    """
    dirs: list[str] = []
    for candidate in (
        sysconfig.get_path("scripts"),
        os.path.dirname(os.path.abspath(sys.executable)) if sys.executable else "",
    ):
        if candidate and candidate not in dirs and os.path.isdir(candidate):
            dirs.append(candidate)
    return dirs


def _find_in_interpreter_scripts(tool: str) -> str | None:
    """Return the path to ``tool`` if it sits next to the running interpreter."""
    # On Windows, console scripts carry an executable extension.
    names = [tool]
    if os.name == "nt":
        names = [f"{tool}.exe", f"{tool}.cmd", f"{tool}.bat", tool]
    for scripts_dir in _interpreter_script_dirs():
        for name in names:
            candidate = os.path.join(scripts_dir, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def tool_argv(tool: str) -> list[str]:
    """Return the argv prefix used to invoke ``tool``.

    Resolution order: a console script on ``PATH``, then a console script next
    to the running interpreter (the installed ``[kibana]`` extra in an
    unactivated venv), then a pinned ``uvx`` fetch. Append tool
    subcommands/flags to the returned list.
    """
    if tool not in _SUPPORTED_TOOLS:
        raise ValueError(f"Unsupported kb-dashboard tool: {tool!r}")

    installed = shutil.which(tool)
    if installed:
        return [installed]

    sibling = _find_in_interpreter_scripts(tool)
    if sibling:
        return [sibling]

    if shutil.which("uvx"):
        return ["uvx", "--from", f"{tool}=={KB_DASHBOARD_TOOL_VERSION}", tool]

    raise KbToolUnavailableError(
        f"{tool} is not available. Install the Kibana tools with "
        f'`pip install "obs-migrate[kibana]"` (Python 3.12+), or install `uv` '
        f"so the pinned tool can be fetched via uvx."
    )


__all__ = ["KB_DASHBOARD_TOOL_VERSION", "KbToolUnavailableError", "tool_argv"]
