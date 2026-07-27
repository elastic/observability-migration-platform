# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_bump_module():
    path = ROOT / "scripts" / "bump_version.py"
    spec = importlib.util.spec_from_file_location("bump_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BumpVersionScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bump = _load_bump_module()

    def test_update_pyproject_version_replaces_first_version_line(self):
        text = (
            '[build-system]\nrequires = ["setuptools"]\n\n'
            '[project]\nname = "elastic-observability-migration"\nversion = "0.1.0"\n'
            'description = "x"\n'
        )
        updated = self.bump.update_pyproject_version(text, "0.4.0")
        self.assertIn('version = "0.4.0"', updated)
        self.assertNotIn('version = "0.1.0"', updated)
        self.assertEqual(self.bump.read_pyproject_version(updated), "0.4.0")

    def test_update_pyproject_version_requires_version_line(self):
        with self.assertRaises(ValueError):
            self.bump.update_pyproject_version("[project]\nname = \"x\"\n", "0.4.0")

    def test_main_writes_pyproject_with_skip_lock(self):
        sample = (
            '[project]\nname = "elastic-observability-migration"\nversion = "0.1.0"\n'
            'description = "x"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(sample, encoding="utf-8")
            rc = self.bump.main(["0.4.0", "--root", str(root), "--skip-lock"])
            self.assertEqual(rc, 0)
            text = (root / "pyproject.toml").read_text(encoding="utf-8")
            self.assertEqual(self.bump.read_pyproject_version(text), "0.4.0")

    def test_main_rejects_invalid_version(self):
        rc = self.bump.main(["not-a-version", "--skip-lock"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
