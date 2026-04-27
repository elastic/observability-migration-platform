# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import generate_telemetry_contract


class GenerateTelemetryContractScriptTests(unittest.TestCase):
    def test_main_writes_contract_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir()
            output_path = Path(tmpdir) / "contract.json"
            contract = {
                "version": 1,
                "streams": {"metrics-*": {"fields": {}}},
                "summary": {"streams": 1},
            }

            with mock.patch.object(
                generate_telemetry_contract,
                "build_telemetry_contract",
                return_value=contract,
            ):
                exit_code = generate_telemetry_contract.main(
                    [str(artifact_dir), "--output", str(output_path)]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), contract)

    def test_main_accepts_multiple_artifact_dirs_and_writes_schema_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = Path(tmpdir) / "first"
            second = Path(tmpdir) / "second"
            first.mkdir()
            second.mkdir()
            output_path = Path(tmpdir) / "contract.json"
            report_path = Path(tmpdir) / "schema.md"
            contract = {
                "version": 1,
                "artifact_dirs": [str(first), str(second)],
                "streams": {},
                "summary": {"streams": 0},
            }

            with mock.patch.object(
                generate_telemetry_contract,
                "build_combined_telemetry_contract",
                return_value=contract,
            ) as combined, mock.patch.object(
                generate_telemetry_contract,
                "build_schema_change_report",
                return_value="# report\n",
            ) as report:
                exit_code = generate_telemetry_contract.main(
                    [
                        str(first),
                        str(second),
                        "--output",
                        str(output_path),
                        "--schema-report",
                        str(report_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            combined.assert_called_once_with([Path(first), Path(second)])
            self.assertEqual(report.call_count, 1)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), contract)
            self.assertEqual(report_path.read_text(encoding="utf-8"), "# report\n")


if __name__ == "__main__":
    unittest.main()
