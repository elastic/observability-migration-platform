# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import importlib.util
import pathlib
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_licenses.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("check_licenses_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CheckLicensesScriptTests(unittest.TestCase):
    def test_run_pip_licenses_includes_system_packages(self):
        module = _load_script_module()

        with patch.object(
            module.subprocess,
            "run",
            return_value=module.subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
        ) as mock_run:
            module._run_pip_licenses()

        command = mock_run.call_args.args[0]
        self.assertIn("--with-system", command)
        self.assertIn("--with-urls", command)

    def test_main_is_strict_by_default_for_unknown_licenses(self):
        module = _load_script_module()

        with patch.object(
            module,
            "_run_pip_licenses",
            return_value=[{"Name": "mystery", "Version": "1.0", "License": "UNKNOWN", "URL": ""}],
        ):
            code = module.main([])

        self.assertEqual(code, 1)

    def test_main_can_disable_unknown_license_failures(self):
        module = _load_script_module()

        with patch.object(
            module,
            "_run_pip_licenses",
            return_value=[{"Name": "mystery", "Version": "1.0", "License": "UNKNOWN", "URL": ""}],
        ):
            code = module.main(["--no-strict-unknown"])

        self.assertEqual(code, 0)

    def test_write_report_uses_dependency_environment_title(self):
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = pathlib.Path(tmpdir) / "dependencies.md"
            original_report_path = module.REPORT_PATH
            module.REPORT_PATH = report_path
            try:
                module._write_report(
                    [
                        {
                            "name": "example",
                            "version": "1.2.3",
                            "license": "MIT",
                            "url": "https://example.invalid/project",
                            "override_source": None,
                        }
                    ]
                )
            finally:
                module.REPORT_PATH = original_report_path

            content = report_path.read_text(encoding="utf-8")

        self.assertTrue(content.startswith("# Python Dependency Environment License Inventory\n"))

    def test_write_report_uses_override_source_when_package_url_is_missing(self):
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = pathlib.Path(tmpdir) / "dependencies.md"
            original_report_path = module.REPORT_PATH
            module.REPORT_PATH = report_path
            try:
                module._write_report(
                    [
                        {
                            "name": "promql-parser",
                            "version": "0.8.0",
                            "license": "MIT",
                            "url": "UNKNOWN",
                            "override_source": "https://github.com/messense/py-promql-parser/blob/main/LICENSE",
                        }
                    ]
                )
            finally:
                module.REPORT_PATH = original_report_path

            content = report_path.read_text(encoding="utf-8")

        self.assertIn(
            "<https://github.com/messense/py-promql-parser/blob/main/LICENSE>",
            content,
        )


if __name__ == "__main__":
    unittest.main()
