# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import importlib.util
import pathlib
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_skill_structure.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("check_skill_structure_script", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _skill(
    root: pathlib.Path,
    name: str,
    *,
    description: str = "Use when the user wants a thing — does the thing.",
    body: str = "# Heading\n\nSome content.\n",
    frontmatter: str | None = None,
) -> None:
    """Write a SKILL.md under .claude/skills/<name>/ with given frontmatter+body."""
    if frontmatter is None:
        frontmatter = f"---\nname: {name}\ndescription: {description}\n---\n\n"
    _write(root / ".claude" / "skills" / name / "SKILL.md", frontmatter + body)


class FrontmatterTests(unittest.TestCase):
    def test_valid_skill_passes(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(root, "my-skill")
            self.assertEqual(module.check_structure(root), [])

    def test_missing_frontmatter_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _write(root / ".claude" / "skills" / "my-skill" / "SKILL.md", "# No frontmatter here\n")
            errors = module.check_structure(root)
            self.assertEqual(len(errors), 1)
            self.assertIn("my-skill", errors[0])

    def test_malformed_yaml_frontmatter_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            fm = "---\nname: my-skill\ndescription: \"unterminated\n---\n\nBody\n"
            _skill(root, "my-skill", frontmatter=fm)
            errors = module.check_structure(root)
            self.assertEqual(len(errors), 1)
            self.assertIn("my-skill", errors[0])

    def test_empty_name_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            fm = "---\nname: \"\"\ndescription: A description.\n---\n\nBody\n"
            _skill(root, "my-skill", frontmatter=fm)
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("name" in e for e in errors))

    def test_empty_description_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            fm = "---\nname: my-skill\ndescription: \"\"\n---\n\nBody\n"
            _skill(root, "my-skill", frontmatter=fm)
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("description" in e for e in errors))


class NameMatchesDirTests(unittest.TestCase):
    def test_name_mismatch_with_dir_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            fm = "---\nname: wrong-name\ndescription: A description.\n---\n\nBody\n"
            _skill(root, "my-skill", frontmatter=fm)
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("my-skill" in e and "wrong-name" in e for e in errors))


class DescriptionBudgetTests(unittest.TestCase):
    def test_description_too_long_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            long_desc = "x" * (module.MAX_DESCRIPTION_CHARS + 1)
            _skill(root, "my-skill", description=long_desc)
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("description" in e.lower() for e in errors))

    def test_description_at_limit_passes(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            desc = "x" * module.MAX_DESCRIPTION_CHARS
            _skill(root, "my-skill", description=desc)
            self.assertEqual(module.check_structure(root), [])


class CrossReferenceTests(unittest.TestCase):
    def test_valid_skill_cross_reference_passes(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(root, "other-skill")
            _skill(root, "my-skill", body="## See also\n\n- `other-skill` skill — does another thing.\n")
            self.assertEqual(module.check_structure(root), [])

    def test_dangling_skill_cross_reference_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(root, "my-skill", body="## See also\n\n- `no-such-skill` skill — does nothing.\n")
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("no-such-skill" in e for e in errors))

    def test_dangling_wikilink_cross_reference_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(root, "my-skill", body="See [[ghost-skill]] for details.\n")
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("ghost-skill" in e for e in errors))

    def test_valid_wikilink_cross_reference_passes(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(root, "other-skill")
            _skill(root, "my-skill", body="See [[other-skill]] for details.\n")
            self.assertEqual(module.check_structure(root), [])

    def test_known_external_skill_reference_allowed(self):
        """A reference to an allowlisted generic skill (not shipped here) is not dangling."""
        module = _load_script_module()
        external = sorted(module.KNOWN_EXTERNAL_SKILLS)[0]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(root, "my-skill", body=f"Pairs the generic `{external}` skill with this workflow.\n")
            self.assertEqual(module.check_structure(root), [])


class ReferencedPathTests(unittest.TestCase):
    def test_missing_referenced_doc_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(root, "my-skill", body="See `docs/nope.md` for details.\n")
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("docs/nope.md" in e for e in errors))

    def test_existing_referenced_doc_passes(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _write(root / "docs" / "command-contract.md", "# contract\n")
            _skill(root, "my-skill", body="See `docs/command-contract.md` for details.\n")
            self.assertEqual(module.check_structure(root), [])

    def test_bare_directory_mention_not_validated(self):
        """Prose like 'no scripts/ directory is needed' must not be treated as a path."""
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(
                root,
                "my-skill",
                body="No `scripts/`, `infra/`, or `examples/` directory is needed. See `scripts/...`.\n",
            )
            self.assertEqual(module.check_structure(root), [])

    def test_external_claude_skill_reference_ignored(self):
        """~/.claude/skills/<external>/... not shipped here is out of our control → ignored."""
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(
                root,
                "my-skill",
                body="See [generic](~/.claude/skills/chrome-devtools-debugging/SKILL.md).\n",
            )
            self.assertEqual(module.check_structure(root), [])

    def test_self_claude_skill_reference_missing_fails(self):
        """A ~/.claude/skills/<known-skill>/file.md pointing at a missing file fails."""
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(root, "my-skill", body="See [ref](~/.claude/skills/my-skill/missing.md).\n")
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("missing.md" in e for e in errors))

    def test_self_claude_skill_reference_existing_passes(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(root, "my-skill", body="See [ref](~/.claude/skills/my-skill/extra.md).\n")
            _write(root / ".claude" / "skills" / "my-skill" / "extra.md", "more\n")
            self.assertEqual(module.check_structure(root), [])


class CliFlagTests(unittest.TestCase):
    def test_valid_obs_migrate_flags_pass(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            body = textwrap.dedent(
                """
                Run it:

                ```bash
                obs-migrate migrate \\
                  --source grafana \\
                  --input-dir ./in \\
                  --output-dir out \\
                  --preflight
                ```
                """
            )
            _skill(root, "my-skill", body=body)
            self.assertEqual(module.check_structure(root), [])

    def test_stale_obs_migrate_flag_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            body = "```bash\nobs-migrate migrate --source grafana --totally-bogus-flag\n```\n"
            _skill(root, "my-skill", body=body)
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("--totally-bogus-flag" in e for e in errors))

    def test_unknown_obs_migrate_subcommand_fails(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            body = "```bash\nobs-migrate frobnicate --es-url x\n```\n"
            _skill(root, "my-skill", body=body)
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("frobnicate" in e for e in errors))

    def test_cluster_positional_with_valid_flag_passes(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            body = "```bash\nobs-migrate cluster delete-dashboards --dashboard-ids abc\n```\n"
            _skill(root, "my-skill", body=body)
            self.assertEqual(module.check_structure(root), [])

    def test_flag_outside_bash_block_ignored(self):
        """A bogus flag mentioned in prose (not a fenced bash block) is not validated."""
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _skill(root, "my-skill", body="Once we had a flag `obs-migrate migrate --gone-flag` long ago.\n")
            self.assertEqual(module.check_structure(root), [])

    def test_equals_form_flag_validated(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            body = "```bash\nobs-migrate migrate --source=grafana --nope=1\n```\n"
            _skill(root, "my-skill", body=body)
            errors = module.check_structure(root)
            self.assertTrue(errors)
            self.assertTrue(any("--nope" in e for e in errors))

    def test_non_obs_migrate_commands_ignored(self):
        """grafana-migrate / arbitrary commands in bash blocks are not flag-checked here."""
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            body = "```bash\ngrafana-migrate --whatever-flag\npip install 'obs-migrate[grafana]'\n```\n"
            _skill(root, "my-skill", body=body)
            self.assertEqual(module.check_structure(root), [])


class RealRepoTests(unittest.TestCase):
    def test_main_returns_0_on_real_repo(self):
        module = _load_script_module()
        self.assertEqual(module.main(["--root", str(ROOT)]), 0)

    def test_main_returns_1_on_structural_error(self):
        module = _load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            _write(root / ".claude" / "skills" / "broken" / "SKILL.md", "no frontmatter\n")
            self.assertEqual(module.main(["--root", str(root)]), 1)


if __name__ == "__main__":
    unittest.main()
