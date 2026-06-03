# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for shared Kibana target compile path."""

import os
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

    def test_upload_yaml_scopes_node_tls_env_to_subprocess(self):
        with mock.patch.dict(
            os.environ,
            {"NODE_EXTRA_CA_CERTS": "", "NODE_TLS_REJECT_UNAUTHORIZED": ""},
            clear=False,
        ), mock.patch.object(shared_compile, "_run_command", return_value=(True, "ok")) as run_command:
            shared_compile.upload_yaml(
                "dash.yaml",
                "compiled-out",
                "https://kibana.example",
                verify="/tmp/ca.pem",
            )

        env = run_command.call_args.kwargs["env"]
        self.assertEqual(env["NODE_EXTRA_CA_CERTS"], "/tmp/ca.pem")
        self.assertEqual(os.environ.get("NODE_EXTRA_CA_CERTS", ""), "")

    def test_detect_space_id_from_url_without_space_returns_empty(self):
        self.assertEqual(shared_compile.detect_space_id_from_kibana_url("http://localhost:5601"), "")


class TestCompileUsesResolverAndModules(unittest.TestCase):
    def test_no_repo_root_helper(self):
        self.assertFalse(hasattr(shared_compile, "_repo_root"))

    def test_compile_yaml_uses_kbtool_resolver(self):
        with (
            mock.patch(
                "observability_migration.targets.kibana.compile.tool_argv",
                return_value=["kb-dashboard-cli"],
            ) as argv,
            mock.patch.object(shared_compile, "_run_command", return_value=(True, "ok")) as run,
        ):
            shared_compile.compile_yaml("d.yaml", "out")
        argv.assert_called_once_with("kb-dashboard-cli")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "kb-dashboard-cli")
        self.assertIn("compile", cmd)

    def test_lint_delegates_to_lint_module(self):
        with mock.patch(
            "observability_migration.targets.kibana.compile.lint_module.lint_dashboard_yaml",
            return_value=(True, "passed"),
        ) as lint_fn:
            ok, output = shared_compile.lint_dashboard_yaml("yamldir")
        lint_fn.assert_called_once()
        self.assertTrue(ok)
        self.assertEqual(output, "passed")

    def test_layout_delegates_to_layout_module(self):
        with mock.patch(
            "observability_migration.targets.kibana.compile.layout_module.validate_compiled_layout",
            return_value=(True, "layout ok"),
        ) as layout_fn:
            ok, output = shared_compile.validate_compiled_layout("compiled")
        layout_fn.assert_called_once()
        self.assertTrue(ok)
        self.assertEqual(output, "layout ok")


if __name__ == "__main__":
    unittest.main()
