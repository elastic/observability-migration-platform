# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATADOG_DEMO_SCRIPT = ROOT / "scripts" / "run_datadog_demo.sh"


def _run_datadog_demo(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DATADOG_DEMO_SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


class DatadogDemoScriptTests(unittest.TestCase):
    def test_help_lists_local_and_serverless_targets(self):
        result = _run_datadog_demo("--help")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("--target", result.stdout)
        self.assertIn("local", result.stdout)
        self.assertIn("serverless", result.stdout)

    def test_serverless_target_rejects_local_lab_flags(self):
        result = _run_datadog_demo("--target", "serverless", "--start-lab")

        self.assertEqual(result.returncode, 1)
        self.assertIn("--start-lab is only valid with --target local", result.stderr)

    def test_local_target_builds_start_command_without_empty_array_expansion(self):
        script_text = DATADOG_DEMO_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('start_lab_cmd=(bash "$ROOT/scripts/start_local_lab.sh")', script_text)
        self.assertIn('if [[ ${#start_args[@]} -gt 0 ]]; then', script_text)

    def test_datadog_demo_uses_dashboard_scoped_outputs(self):
        script_text = DATADOG_DEMO_SCRIPT.read_text(encoding="utf-8")

        self.assertRegex(script_text, re.compile(r"--assets\s+dashboards"))
        self.assertIn('ALERT_ARTIFACT_DIR="$OUTPUT_DIR/alerts"', script_text)
        self.assertIn('DASHBOARD_YAML_DIR="$OUTPUT_DIR/dashboards/yaml"', script_text)
        self.assertIn('RUN_SUMMARY="$OUTPUT_DIR/run_summary.json"', script_text)
        self.assertIn('YAML:   $DASHBOARD_YAML_DIR', script_text)
        self.assertIn('Run summary: $RUN_SUMMARY', script_text)
        self.assertNotIn('DASHBOARD_YAML_DIR="$OUTPUT_DIR/yaml"', script_text)
        self.assertNotIn('YAML:   $OUTPUT_DIR/yaml', script_text)


if __name__ == "__main__":
    unittest.main()
