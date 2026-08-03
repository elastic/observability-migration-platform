# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "scripts" / "check_release_version.py"
    spec = importlib.util.spec_from_file_location("check_release_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_matching_docs(root: Path, version: str) -> None:
    pin = (
        f"# PKG='elastic-observability-migration[all]=={version}'\n"
        f"PKG='elastic-observability-migration[all]@"
        f"git+https://github.com/elastic/observability-migration-platform.git@v{version}'\n"
    )
    (root / "README.md").write_text(pin, encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "command-contract.md").write_text(pin, encoding="utf-8")
    for tree in (".claude", ".cursor", ".agents"):
        skill = root / tree / "skills" / "install-obs-migrate"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(pin, encoding="utf-8")


class CheckReleaseVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def test_matching_tag_ok(self):
        self.mod.check_tag_matches_project("v1.2.3", "1.2.3")

    def test_tag_without_v_accepted_if_equal(self):
        self.mod.check_tag_matches_project("1.2.3", "1.2.3")

    def test_mismatch_fails(self):
        with self.assertRaises(SystemExit) as ctx:
            self.mod.check_tag_matches_project("v1.0.0", "1.2.3")
        self.assertEqual(ctx.exception.code, 1)

    def test_main_ok_against_repo(self):
        # Uses the real pyproject + operator docs in this checkout.
        version = self.mod.read_pyproject_version(ROOT)
        rc = self.mod.main(["--tag", f"v{version}", "--root", str(ROOT)])
        self.assertEqual(rc, 0)

    def test_main_mismatch_against_temp_pyproject(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "elastic-observability-migration"\nversion = "1.2.3"\n',
                encoding="utf-8",
            )
            _write_matching_docs(root, "1.2.3")
            with self.assertRaises(SystemExit) as ctx:
                self.mod.main(["--tag", "v1.0.0", "--root", str(root)])
            self.assertEqual(ctx.exception.code, 1)

    def test_docs_only_fails_on_stale_readme_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "elastic-observability-migration"\nversion = "1.2.3"\n',
                encoding="utf-8",
            )
            _write_matching_docs(root, "0.9.0")
            with self.assertRaises(SystemExit) as ctx:
                self.mod.main(["--docs-only", "--root", str(root)])
            self.assertEqual(ctx.exception.code, 1)

    def test_docs_only_ok_when_pins_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "elastic-observability-migration"\nversion = "1.2.3"\n',
                encoding="utf-8",
            )
            _write_matching_docs(root, "1.2.3")
            rc = self.mod.main(["--docs-only", "--root", str(root)])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
