# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the in-process dashboard-YAML lint gate.

The gate must ignore esql-* findings on native PROMQL panels (issue #60) while
still failing them on ordinary ES|QL panels. The external kb-dashboard-lint
runner is stubbed by patching the per-file lint runner.
"""

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from observability_migration.targets.kibana import lint


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


_GROUP_BY_FINDING = {
    "rule_id": "esql-group-by-syntax",
    "severity": "warning",
    "message": "Unexpected GROUP BY; ES|QL uses STATS ... BY",
    "dashboard_name": "Test Dashboard",
}


class LintGateTests(unittest.TestCase):
    def _run(self, yaml_text: str, panel_title: str):
        finding = {**_GROUP_BY_FINDING, "panel_title": panel_title}
        with tempfile.TemporaryDirectory() as tmp:
            yaml_file = Path(tmp) / "dashboard.yaml"
            yaml_file.write_text(yaml_text, encoding="utf-8")
            # Stub the external lint runner: return (findings, raw_stderr).
            with mock.patch.object(lint, "_run_lint_tool", return_value=([finding], "")):
                return lint.lint_dashboard_yaml(tmp)

    def test_native_promql_finding_is_ignored(self):
        query = "PROMQL index=metrics-* step=1m value=(group by (type) (authentik_outpost_ldap_requests_sum))"
        ok, output = self._run(_dashboard_yaml("Native PromQL Panel", query), "Native PromQL Panel")
        self.assertTrue(ok, msg=output)
        self.assertIn("Ignored 1 ES|QL lint entry on native PROMQL panels.", output)

    def test_real_esql_finding_still_fails(self):
        query = "FROM metrics-* | STATS c = COUNT(*) GROUP BY type"
        ok, output = self._run(_dashboard_yaml("Real ESQL Panel", query), "Real ESQL Panel")
        self.assertFalse(ok)
        self.assertIn("esql-group-by-syntax", output)


class UnboundParamGateTests(unittest.TestCase):
    """Issue #131 regression gate: a panel ?param with no control must fail."""

    def _run(self, yaml_text: str):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dashboard.yaml").write_text(yaml_text, encoding="utf-8")
            # External tool returns nothing; only the in-process gate runs.
            with mock.patch.object(lint, "_run_lint_tool", return_value=([], "")):
                return lint.lint_dashboard_yaml(tmp)

    def test_native_promql_unbound_param_fails(self):
        yaml_text = textwrap.dedent(
            """\
            dashboards:
              - name: Test Dashboard
                panels:
                  - title: Apps
                    esql:
                      query: "PROMQL index=metrics-* step=1m value=(sum(argocd_app_info{namespace=~?namespace}))"
            """
        )
        ok, output = self._run(yaml_text)
        self.assertFalse(ok, msg=output)
        self.assertIn("unbound-esql-param", output)
        self.assertIn("?namespace", output)

    def test_param_with_matching_control_passes(self):
        yaml_text = textwrap.dedent(
            """\
            dashboards:
              - name: Test Dashboard
                controls:
                  - type: esql
                    variable_name: namespace
                    variable_type: values
                    query: "FROM metrics-* | KEEP namespace"
                    default: ".*"
                panels:
                  - title: Apps
                    esql:
                      query: "PROMQL index=metrics-* step=1m value=(sum(argocd_app_info{namespace=~?namespace}))"
            """
        )
        ok, output = self._run(yaml_text)
        self.assertTrue(ok, msg=output)

    def test_question_mark_inside_quoted_value_is_not_a_param(self):
        yaml_text = textwrap.dedent(
            """\
            dashboards:
              - name: Test Dashboard
                panels:
                  - title: Pattern
                    esql:
                      query: 'FROM metrics-* | WHERE host RLIKE "ab?c"'
            """
        )
        ok, output = self._run(yaml_text)
        self.assertTrue(ok, msg=output)

    def test_internal_time_params_are_ignored(self):
        yaml_text = textwrap.dedent(
            """\
            dashboards:
              - name: Test Dashboard
                panels:
                  - title: TS
                    esql:
                      query: "FROM metrics-* | WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend"
            """
        )
        ok, output = self._run(yaml_text)
        self.assertTrue(ok, msg=output)


if __name__ == "__main__":
    unittest.main()
