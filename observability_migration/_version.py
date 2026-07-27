# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Single source of truth for the package version.

Prefer ``pyproject.toml`` when present (source / editable checkouts). Fall back
to installed distribution metadata for a wheel/sdist install that has no
adjacent project file.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_PACKAGE_NAME = "obs-migrate"


@lru_cache(maxsize=4)
def read_project_version(root: Path | None = None) -> str:
    """Return the project version string (e.g. ``\"1.2.3\"``)."""
    base = root if root is not None else Path(__file__).resolve().parents[1]
    pyproject = base / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    try:
        return version(_PACKAGE_NAME)
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "obs-migrate version unavailable: no pyproject.toml next to the "
            "package and the distribution is not installed"
        ) from exc
