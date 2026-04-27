# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "verify_alert_rule_uploads.py"


class VerifyAlertRuleUploadsScriptTests(unittest.TestCase):
    @staticmethod
    def _load_script_module():
        spec = importlib.util.spec_from_file_location("verify_alert_rule_uploads_script", SCRIPT_PATH)
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
                "def create_rule(*args, **kwargs):\n"
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
                spec = importlib.util.spec_from_file_location("verify_alert_rule_uploads_script", SCRIPT_PATH)
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
        self.assertIs(module.create_rule, imported_alerting.create_rule)

    def test_main_short_circuits_when_preflight_is_unreachable(self):
        module = self._load_script_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            comparison_path = pathlib.Path(tmpdir) / "comparison.json"
            comparison_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "alert_id": "a1",
                                "name": "Alert 1",
                                "target": {
                                    "payload_status": "emitted",
                                    "target_rule_payload": {"rule_type_id": ".es-query", "params": {}},
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(
                    module,
                    "run_alerting_preflight",
                    return_value={
                        "health": {"error": "connection refused"},
                        "rule_types_count": 0,
                        "connector_types_count": 0,
                        "can_create_es_query_rules": False,
                        "can_create_index_threshold_rules": False,
                        "can_create_custom_threshold_rules": False,
                    },
                ),
                patch.object(module, "create_rule") as mock_create_rule,
            ):
                code = module.main(
                    [
                        "--comparison",
                        str(comparison_path),
                        "--kibana-url",
                        "http://localhost:1",
                        "--api-key",
                        "test-key",
                    ]
                )
            self.assertEqual(code, 2)
            mock_create_rule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
