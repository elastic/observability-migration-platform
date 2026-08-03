# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from observability_migration.core.reporting.report import (
    MigrationResult,
    print_report,
    save_detailed_report,
)


class ControlWarningReportingTests(unittest.TestCase):
    def _result(self) -> MigrationResult:
        return MigrationResult(
            dashboard_title="Chained controls",
            dashboard_uid="chained-controls",
            control_warnings=[
                "variable 'id' is broader than its chained Grafana source"
            ],
        )

    def test_print_report_surfaces_dashboard_control_warnings(self):
        result = self._result()
        output = io.StringIO()

        with redirect_stdout(output):
            print_report([result])

        self.assertIn("CONTROL WARNINGS (1):", output.getvalue())
        self.assertIn(
            "[Chained controls] variable 'id' is broader than its chained Grafana source",
            output.getvalue(),
        )

    def test_detailed_json_report_preserves_dashboard_control_warnings(self):
        result = self._result()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "migration_report.json"
            save_detailed_report([result], output_path)
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(
            payload["dashboards"][0]["control_warnings"],
            result.control_warnings,
        )


if __name__ == "__main__":
    unittest.main()
