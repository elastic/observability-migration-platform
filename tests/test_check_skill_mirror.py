# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_skill_mirror.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("check_skill_mirror_script", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class CheckSkillMirrorScriptTests(unittest.TestCase):
    def test_identical_content_passes(self):
        """Both trees have the same file with identical content."""
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            content = "# Test skill content\nSome documentation here."
            _write(root / ".claude" / "skills" / "my-skill" / "SKILL.md", content)
            _write(root / ".cursor" / "skills" / "my-skill" / "SKILL.md", content)

            errors = module.check_mirror(root)

        self.assertEqual(errors, [])

    def test_prefix_only_diff_passes(self):
        """Prefix-only differences (~/.claude/ vs ~/.cursor/) are allowed."""
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            claude_content = "See [this](~/.claude/skills/other/SKILL.md) for details."
            cursor_content = "See [this](~/.cursor/skills/other/SKILL.md) for details."
            _write(root / ".claude" / "skills" / "my-skill" / "SKILL.md", claude_content)
            _write(root / ".cursor" / "skills" / "my-skill" / "SKILL.md", cursor_content)

            errors = module.check_mirror(root)

        self.assertEqual(errors, [])

    def test_semantic_diff_fails(self):
        """Content differs beyond prefix → error with relative path."""
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            claude_content = "Content version A"
            cursor_content = "Content version B"
            _write(root / ".claude" / "skills" / "my-skill" / "SKILL.md", claude_content)
            _write(root / ".cursor" / "skills" / "my-skill" / "SKILL.md", cursor_content)

            errors = module.check_mirror(root)

        self.assertEqual(len(errors), 1)
        self.assertIn("my-skill/SKILL.md", errors[0])

    def test_missing_cursor_file_fails(self):
        """File in .claude/ but not in .cursor/ → error."""
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _write(root / ".claude" / "skills" / "my-skill" / "SKILL.md", "content")

            errors = module.check_mirror(root)

        self.assertEqual(len(errors), 1)
        self.assertIn("my-skill/SKILL.md", errors[0])

    def test_extra_cursor_file_fails(self):
        """File in .cursor/ but not in .claude/ → error."""
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _write(root / ".cursor" / "skills" / "extra-skill" / "SKILL.md", "content")

            errors = module.check_mirror(root)

        self.assertEqual(len(errors), 1)
        self.assertIn("extra-skill/SKILL.md", errors[0])

    def test_nested_file_passes_when_mirrored(self):
        """Nested files with identical content in both trees pass."""
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            content = "Nested file content"
            _write(root / ".claude" / "skills" / "my-skill" / "subdir" / "extra.md", content)
            _write(root / ".cursor" / "skills" / "my-skill" / "subdir" / "extra.md", content)

            errors = module.check_mirror(root)

        self.assertEqual(errors, [])

    def test_main_returns_0_on_real_repo(self):
        """Main function returns 0 for the real repo (no drifts)."""
        module = _load_script_module()

        exit_code = module.main(["--root", str(ROOT)])

        self.assertEqual(exit_code, 0)

    def test_main_returns_1_on_drift(self):
        """Main function returns 1 when drift is detected."""
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            # Create a drift: same skill name, different content
            _write(
                root / ".claude" / "skills" / "test-skill" / "SKILL.md",
                "Claude version",
            )
            _write(
                root / ".cursor" / "skills" / "test-skill" / "SKILL.md",
                "Cursor version",
            )

            exit_code = module.main(["--root", str(root)])

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
