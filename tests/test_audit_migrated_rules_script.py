# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "audit_migrated_rules.py"


class AuditMigratedRulesScriptTests(unittest.TestCase):
    @staticmethod
    def _load_script_module():
        spec = importlib.util.spec_from_file_location("audit_migrated_rules_script", SCRIPT_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_script_prefers_local_checkout_over_earlier_sys_path_package(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_root = pathlib.Path(tmpdir)
            fake_pkg = fake_root / "observability_migration" / "targets" / "kibana"
            fake_pkg.mkdir(parents=True)
            (fake_root / "observability_migration" / "__init__.py").write_text("", encoding="utf-8")
            (fake_root / "observability_migration" / "targets" / "__init__.py").write_text("", encoding="utf-8")
            (fake_root / "observability_migration" / "targets" / "kibana" / "__init__.py").write_text(
                "",
                encoding="utf-8",
            )
            (fake_pkg / "alerting.py").write_text(
                "def audit_migrated_rules(*args, **kwargs):\n"
                "    return {}\n",
                encoding="utf-8",
            )

            original_sys_path = list(sys.path)
            removed_modules = {
                name: sys.modules.pop(name)
                for name in list(sys.modules)
                if name == "observability_migration" or name.startswith("observability_migration.")
            }
            try:
                sys.path = [str(fake_root), *original_sys_path]
                spec = importlib.util.spec_from_file_location("audit_migrated_rules_script", SCRIPT_PATH)
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
                imported_alerting = sys.modules["observability_migration.targets.kibana.alerting"]
            finally:
                sys.path = original_sys_path
                for name in list(sys.modules):
                    if name == "observability_migration" or name.startswith("observability_migration."):
                        sys.modules.pop(name, None)
                sys.modules.update(removed_modules)

        self.assertTrue(callable(module.main))
        self.assertEqual(pathlib.Path(imported_alerting.__file__).resolve(), ROOT / "observability_migration/targets/kibana/alerting.py")
        self.assertIs(module.audit_migrated_rules, imported_alerting.audit_migrated_rules)

    def test_main_returns_nonzero_when_enabled_migrated_rules_exist(self):
        module = self._load_script_module()
        with patch.object(
            module,
            "audit_migrated_rules",
            return_value={
                "enabled_migrated_rule_ids": ["rule-1"],
                "remediation": {"failed_rule_ids": []},
            },
        ):
            self.assertEqual(module.main(["--kibana-url", "http://kibana:5601", "--api-key", "secret"]), 1)

    def test_main_returns_zero_after_successful_disable_remediation(self):
        module = self._load_script_module()
        with patch.object(
            module,
            "audit_migrated_rules",
            return_value={
                "enabled_migrated_rule_ids": ["rule-1"],
                "remediation": {"failed_rule_ids": []},
            },
        ):
            self.assertEqual(
                module.main(
                    [
                        "--kibana-url",
                        "http://kibana:5601",
                        "--api-key",
                        "secret",
                        "--disable-enabled",
                    ]
                ),
                0,
            )

    def test_main_returns_two_when_audit_reports_fetch_errors(self):
        module = self._load_script_module()
        with patch.object(
            module,
            "audit_migrated_rules",
            return_value={
                "enabled_migrated_rule_ids": [],
                "remediation": {"failed_rule_ids": []},
                "errors": ["connection refused"],
            },
        ):
            self.assertEqual(module.main(["--kibana-url", "http://kibana:5601", "--api-key", "secret"]), 2)


if __name__ == "__main__":
    unittest.main()
