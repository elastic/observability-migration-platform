# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""CLI tests for metric-map scaffold."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from observability_migration.app import cli as app_cli


class MetricMapScaffoldCliTests(unittest.TestCase):
    def test_scaffold_writes_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "artifacts"
            artifact_dir.mkdir()
            (artifact_dir / "required_target_contract.json").write_text(
                json.dumps(
                    {
                        "required_fields": {
                            "needs.map": {
                                "target_field": "needs.map",
                                "source_fields": ["needs.map"],
                                "roles": ["metric"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            output_path = Path(tmp) / "metric-map.yaml"
            with patch.object(app_cli.sys, "argv", [
                "obs-migrate",
                "metric-map",
                "scaffold",
                "--artifact-dir",
                str(artifact_dir),
                "--output",
                str(output_path),
            ]):
                with self.assertRaises(SystemExit) as raised:
                    app_cli.main()
                self.assertEqual(raised.exception.code, 0)
            self.assertTrue(output_path.is_file())
            self.assertIn("needs.map", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
