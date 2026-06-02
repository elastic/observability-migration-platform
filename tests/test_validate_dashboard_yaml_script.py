# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for scripts/validate_dashboard_yaml.sh (issue #60).

The lint gate must ignore ``esql-*`` lint findings on native ``PROMQL`` panels
(whose ``esql.query`` is PromQL embedded in a ``PROMQL ... value=(...)`` source
command, not real ES|QL) while still failing those findings on ordinary ES|QL
panels.

These tests run the real script but stub the external ``kb-dashboard-lint``
runner (invoked through ``uvx``) with a fake on ``PATH`` that replays canned
lint JSON, so no network or uv tool download is required.
"""

import json
import os
import pathlib
import stat
import subprocess
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATE_SCRIPT = ROOT / "scripts" / "validate_dashboard_yaml.sh"

# A group-by ES|QL lint finding, the exact false-positive class from issue #60.
_GROUP_BY_FINDING = {
    "rule_id": "esql-group-by-syntax",
    "severity": "warning",
    "message": "Unexpected GROUP BY; ES|QL uses STATS ... BY",
}


def _dashboard_yaml(panel_title: str, query: str) -> str:
    return textwrap.dedent(
        f"""\
        dashboards:
          - name: Test Dashboard
            panels:
              - title: {panel_title}
                esql:
                  query: "{query}"
        """
    )


class ValidateDashboardYamlPromqlLintTests(unittest.TestCase):
    def _run_with_fake_lint(self, yaml_text: str, finding: dict, panel_title: str):
        """Run validate_dashboard_yaml.sh with a stubbed kb-dashboard-lint.

        The fake ``uvx`` ignores its uv-tool arguments and emits a single lint
        finding (tagged with the dashboard/panel the script keys on) as JSON.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            yaml_file = tmp_path / "dashboard.yaml"
            yaml_file.write_text(yaml_text, encoding="utf-8")

            full_finding = {
                **finding,
                "dashboard_name": "Test Dashboard",
                "panel_title": panel_title,
            }

            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            fake_uvx = bin_dir / "uvx"
            fake_uvx.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"print(json.dumps({json.dumps([full_finding])}))\n",
                encoding="utf-8",
            )
            fake_uvx.chmod(fake_uvx.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

            return subprocess.run(
                ["bash", str(VALIDATE_SCRIPT), str(yaml_file)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env=env,
            )

    def test_native_promql_group_by_does_not_block_validation(self):
        # PROMQL source command carrying PromQL `group by (label) (metric)`.
        query = "PROMQL index=metrics-* step=1m value=(group by (type) (authentik_outpost_ldap_requests_sum))"
        result = self._run_with_fake_lint(
            _dashboard_yaml("Native PromQL Panel", query),
            _GROUP_BY_FINDING,
            "Native PromQL Panel",
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Expected validation to pass.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("Ignored 1 ES|QL lint entry on native PROMQL panels.", result.stdout)
        self.assertIn("Dashboard YAML validation passed.", result.stdout)

    def test_real_esql_group_by_still_fails_validation(self):
        # A genuine ES|QL panel: the same finding must still fail the gate.
        query = "FROM metrics-* | STATS c = COUNT(*) GROUP BY type"
        result = self._run_with_fake_lint(
            _dashboard_yaml("Real ESQL Panel", query),
            _GROUP_BY_FINDING,
            "Real ESQL Panel",
        )
        self.assertEqual(
            result.returncode,
            1,
            msg=f"Expected validation to fail.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("esql-group-by-syntax", result.stderr)


if __name__ == "__main__":
    unittest.main()
