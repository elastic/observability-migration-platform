# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _uv_lock_obs_migrate_version() -> str:
    text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    match = re.search(r'name = "elastic-observability-migration"\nversion = "([^"]+)"', text)
    assert match is not None, "obs-migrate entry missing from uv.lock"
    return match.group(1)


class VersionHygieneTests(unittest.TestCase):
    def test_dunder_version_matches_pyproject(self):
        from observability_migration import __version__
        from observability_migration._version import read_project_version

        expected = _pyproject_version()
        self.assertEqual(__version__, expected)
        self.assertEqual(read_project_version(ROOT), expected)

    def test_uv_lock_matches_pyproject(self):
        self.assertEqual(_uv_lock_obs_migrate_version(), _pyproject_version())


if __name__ == "__main__":
    unittest.main()
