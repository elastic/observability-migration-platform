# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Resolve how to invoke the external kb-dashboard tooling.

Prefers a locally-installed console script (the ``obs-migrate[kibana]`` extra),
falls back to a pinned ``uvx`` invocation, and otherwise raises a clear,
actionable error instead of a raw shell failure.
"""

from __future__ import annotations

import shutil

KB_DASHBOARD_TOOL_VERSION = "0.4.1"

_SUPPORTED_TOOLS = ("kb-dashboard-cli", "kb-dashboard-lint")


class KbToolUnavailableError(RuntimeError):
    """Raised when neither an installed tool nor uv is available."""


def tool_argv(tool: str) -> list[str]:
    """Return the argv prefix used to invoke ``tool``.

    Append tool subcommands/flags to the returned list.
    """
    if tool not in _SUPPORTED_TOOLS:
        raise ValueError(f"Unsupported kb-dashboard tool: {tool!r}")

    installed = shutil.which(tool)
    if installed:
        return [installed]

    if shutil.which("uvx"):
        return ["uvx", "--from", f"{tool}=={KB_DASHBOARD_TOOL_VERSION}", tool]

    raise KbToolUnavailableError(
        f"{tool} is not available. Install the Kibana tools with "
        f'`pip install "obs-migrate[kibana]"` (Python 3.12+), or install `uv` '
        f"so the pinned tool can be fetched via uvx."
    )


__all__ = ["KB_DASHBOARD_TOOL_VERSION", "KbToolUnavailableError", "tool_argv"]
