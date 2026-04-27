# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FULL_LOCAL_DEMO_SCRIPT = ROOT / "scripts" / "full_local_demo.sh"
REMOVED_SAMPLE_SCRIPT = ROOT / "scripts" / "validate_local_sample.sh"
REFERENCE_FILES = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "docs" / "README.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "command-contract.md",
    ROOT / "docs" / "local-otlp-validation.md",
]


def _run_full_local_demo(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FULL_LOCAL_DEMO_SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


class LocalValidationScriptTests(unittest.TestCase):
    def test_full_local_demo_help_accepts_sample_set(self):
        result = _run_full_local_demo("--sample-set", "bundled", "--help")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("--sample-set", result.stdout)
        self.assertIn("bundled", result.stdout)

    def test_full_local_demo_rejects_input_dir_with_sample_set(self):
        result = _run_full_local_demo(
            "--sample-set",
            "bundled",
            "--input-dir",
            "infra/grafana/dashboards",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "--input-dir cannot be used with --sample-set bundled",
            result.stderr,
        )

    def test_full_local_demo_contains_bundled_sample_contract(self):
        script_text = FULL_LOCAL_DEMO_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("validation/local_otlp_sample_run", script_text)
        self.assertIn("node-exporter-full.json", script_text)
        self.assertIn("k8s-views-global.json", script_text)
        self.assertIn("diverse-panels-test.json", script_text)
        self.assertIn("Node Exporter Full", script_text)
        self.assertIn("Kubernetes / Views / Global", script_text)
        self.assertIn("Diverse Panel Types Test", script_text)

    def test_full_local_demo_contains_external_port_conflict_guard(self):
        script_text = FULL_LOCAL_DEMO_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("docker compose -p \"$PROJECT_NAME\" -f \"$COMPOSE_FILE\" ps", script_text)
        self.assertIn("configured lab ports are already serving Elasticsearch/Kibana", script_text)

    def test_full_local_demo_builds_start_command_without_empty_array_expansion(self):
        script_text = FULL_LOCAL_DEMO_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('start_lab_cmd=(bash "$ROOT/scripts/start_local_lab.sh")', script_text)
        self.assertIn('if [[ ${#start_args[@]} -gt 0 ]]; then', script_text)

    def test_full_local_demo_uses_integrated_grafana_smoke_flow(self):
        script_text = FULL_LOCAL_DEMO_SCRIPT.read_text(encoding="utf-8")

        self.assertRegex(script_text, re.compile(r"--assets\s+dashboards"))
        self.assertIn("--smoke", script_text)
        self.assertIn("--browser-audit", script_text)
        self.assertIn("--smoke-output \"$OUTPUT_DIR/upload_smoke_report.json\"", script_text)
        self.assertIn("--time-from \"$TIME_FROM\"", script_text)
        self.assertIn("--time-to \"$TIME_TO\"", script_text)
        self.assertNotIn("observability_migration.adapters.source.grafana.validate_uploaded_dashboards", script_text)

    def test_full_local_demo_reports_dashboard_scoped_artifacts(self):
        script_text = FULL_LOCAL_DEMO_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("$OUTPUT_DIR/dashboards/yaml", script_text)
        self.assertIn("$OUTPUT_DIR/run_summary.json", script_text)

    def test_validate_local_sample_script_removed(self):
        self.assertFalse(REMOVED_SAMPLE_SCRIPT.exists())

    def test_reference_files_no_longer_use_removed_script(self):
        for path in REFERENCE_FILES:
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("scripts/validate_local_sample.sh", content, msg=str(path))


if __name__ == "__main__":
    unittest.main()
