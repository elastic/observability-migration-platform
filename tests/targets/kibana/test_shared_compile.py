"""Tests for shared Kibana target compile path."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from observability_migration.targets.kibana import compile as shared_compile


class TestSharedCompileImports(unittest.TestCase):
    def test_compile_yaml_exists(self):
        self.assertTrue(callable(shared_compile.compile_yaml))

    def test_compile_all_exists(self):
        self.assertTrue(callable(shared_compile.compile_all))

    def test_upload_yaml_exists(self):
        self.assertTrue(callable(shared_compile.upload_yaml))


class TestSharedCompileBehavior(unittest.TestCase):
    def test_compile_all_compiles_sorted_yaml_files_and_creates_output_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_dir = Path(tmpdir) / "yaml"
            compiled_dir = Path(tmpdir) / "compiled"
            yaml_dir.mkdir()
            (yaml_dir / "b.yaml").write_text("dashboards: []", encoding="utf-8")
            (yaml_dir / "a.yaml").write_text("dashboards: []", encoding="utf-8")
            (yaml_dir / "ignore.txt").write_text("not yaml", encoding="utf-8")

            calls: list[tuple[Path, Path]] = []

            def fake_compile_yaml(yaml_path, output_dir):
                calls.append((Path(yaml_path), Path(output_dir)))
                return True, f"compiled {Path(yaml_path).name}"

            with mock.patch.object(shared_compile, "compile_yaml", side_effect=fake_compile_yaml):
                results = shared_compile.compile_all(yaml_dir, compiled_dir)

            self.assertEqual([name for name, _, _ in results], ["a.yaml", "b.yaml"])
            self.assertEqual([call[0].name for call in calls], ["a.yaml", "b.yaml"])
            self.assertTrue((compiled_dir / "a").is_dir())
            self.assertTrue((compiled_dir / "b").is_dir())

    def test_lint_dashboard_yaml_uses_repo_script(self):
        with mock.patch.object(shared_compile, "_run_command", return_value=(True, "ok")) as run_command:
            shared_compile.lint_dashboard_yaml("/tmp/generated-yaml")

        cmd = run_command.call_args.args[0]
        self.assertEqual(cmd[0], "bash")
        self.assertTrue(cmd[1].endswith("scripts/validate_dashboard_yaml.sh"))
        self.assertEqual(cmd[2], "/tmp/generated-yaml")

    def test_validate_compiled_layout_uses_repo_script(self):
        with mock.patch.object(shared_compile, "_run_command", return_value=(True, "ok")) as run_command:
            shared_compile.validate_compiled_layout("/tmp/compiled")

        cmd = run_command.call_args.args[0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("scripts/validate_dashboard_layout.py"))
        self.assertEqual(cmd[2], "/tmp/compiled")

    def test_upload_yaml_uses_space_aware_url_and_api_key(self):
        with mock.patch.object(shared_compile, "_run_command", return_value=(True, "ok")) as run_command:
            shared_compile.upload_yaml(
                "dash.yaml",
                "compiled-out",
                "http://localhost:5601/s/observability",
                space_id="shadow",
                kibana_api_key="secret-key",
            )

        cmd = run_command.call_args.args[0]
        self.assertIn("--upload", cmd)
        self.assertIn("--kibana-url", cmd)
        self.assertIn("http://localhost:5601/s/shadow", cmd)
        self.assertIn("--kibana-api-key", cmd)
        self.assertIn("secret-key", cmd)

    def test_detect_space_id_from_url_without_space_returns_empty(self):
        self.assertEqual(shared_compile.detect_space_id_from_kibana_url("http://localhost:5601"), "")


if __name__ == "__main__":
    unittest.main()
