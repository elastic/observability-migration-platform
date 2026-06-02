# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import pathlib
import re
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_MIGRATION_SCRIPT = ROOT / "scripts" / "run_migration.sh"
VALIDATE_PANEL_QUERIES_SCRIPT = ROOT / "scripts" / "validate_panel_queries.py"
SETUP_TELEMETRY_DATA_SCRIPT = ROOT / "scripts" / "setup_telemetry_data.py"
GENERATE_ALERT_SUPPORT_REPORT_SCRIPT = ROOT / "scripts" / "generate_alert_support_report.py"
SCRIPTS = [
    "audit_migrated_rules.py",
    "audit_pipeline.py",
    "check_licenses.py",
    "create_grafana_test_alerts.py",
    "generate_alert_support_report.py",
    "generate_telemetry_contract.py",
    "setup_telemetry_data.py",
    "validate_panel_queries.py",
    "verify_alert_rule_uploads.py",
]
SHELL_SCRIPTS = [
    "full_local_demo.sh",
    "generate_dashboard_schema.sh",
    "provision_local_kibana_data_views.sh",
    "run_datadog_demo.sh",
    "run_migration.sh",
    "start_local_lab.sh",
    "stop_local_lab.sh",
]


class ScriptHelpCliTests(unittest.TestCase):
    def test_run_migration_uses_dashboard_scoped_output_layout(self):
        script_text = RUN_MIGRATION_SCRIPT.read_text(encoding="utf-8")

        self.assertRegex(script_text, re.compile(r"--assets\s+dashboards"))
        self.assertIn('ALERT_ARTIFACT_DIR="$OUTPUT_DIR/alerts"', script_text)
        self.assertIn('DASHBOARD_YAML_DIR="$OUTPUT_DIR/dashboards/yaml"', script_text)
        self.assertIn('COMPILED_DIR="$OUTPUT_DIR/dashboards/compiled"', script_text)
        self.assertIn('RUN_SUMMARY="$OUTPUT_DIR/run_summary.json"', script_text)
        self.assertNotIn('DASHBOARD_YAML_DIR="$OUTPUT_DIR/yaml"', script_text)

    def test_helper_scripts_default_to_dashboard_scoped_layouts(self):
        panel_help = subprocess.run(
            [sys.executable, str(VALIDATE_PANEL_QUERIES_SCRIPT), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        self.assertIn("migration_output_native/dashboards/yaml", panel_help.stdout)

    def test_alert_support_scripts_use_canonical_alert_assets_and_paths(self):
        report_text = GENERATE_ALERT_SUPPORT_REPORT_SCRIPT.read_text(encoding="utf-8")

        self.assertRegex(
            report_text,
            re.compile(r'--assets",\s*"alerts"', re.MULTILINE),
        )
        self.assertNotIn("--fetch-alerts", report_text)
        self.assertNotIn("--fetch-monitors", report_text)
        self.assertIn(
            "examples/alerting/generated/grafana/alerts/alert_comparison_results.json",
            report_text,
        )
        self.assertIn(
            "examples/alerting/generated/datadog/alerts/monitor_migration_results.json",
            report_text,
        )
        self.assertIn(
            "examples/alerting/generated/datadog/alerts/monitor_comparison_results.json",
            report_text,
        )

    def test_every_python_script_supports_help_without_runtime_side_effects(self):
        for script_name in SCRIPTS:
            script_path = ROOT / "scripts" / script_name
            result = subprocess.run(
                [sys.executable, str(script_path), "--help"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"{script_name} failed --help:\nstdout={result.stdout}\nstderr={result.stderr}",
            )
            combined_output = f"{result.stdout}\n{result.stderr}".lower()
            self.assertIn(
                "usage",
                combined_output,
                msg=f"{script_name} did not emit usage text for --help",
            )

    def test_every_wrapper_shell_script_supports_help_without_runtime_side_effects(self):
        for script_name in SHELL_SCRIPTS:
            script_path = ROOT / "scripts" / script_name
            result = subprocess.run(
                ["bash", str(script_path), "--help"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"{script_name} failed --help:\nstdout={result.stdout}\nstderr={result.stderr}",
            )
            combined_output = f"{result.stdout}\n{result.stderr}".lower()
            self.assertIn(
                "usage",
                combined_output,
                msg=f"{script_name} did not emit usage text for --help",
            )


if __name__ == "__main__":
    unittest.main()
