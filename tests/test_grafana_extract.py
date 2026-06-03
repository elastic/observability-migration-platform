# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from observability_migration.adapters.source.grafana.extract import extract_dashboards_from_files


class GrafanaFileExtractionTests(unittest.TestCase):
    def test_accepts_grafana_api_dashboard_wrapper(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = {
                "dashboard": {
                    "uid": "dash-1",
                    "title": "API Exported Dashboard",
                    "panels": [],
                },
                "meta": {"slug": "api-exported-dashboard"},
            }
            (Path(tmpdir) / "api_export.json").write_text(json.dumps(payload), encoding="utf-8")

            dashboards = extract_dashboards_from_files(tmpdir)

        self.assertEqual(len(dashboards), 1)
        self.assertEqual(dashboards[0]["title"], "API Exported Dashboard")
        self.assertEqual(dashboards[0]["uid"], "dash-1")
        self.assertEqual(dashboards[0]["_source_file"], "api_export.json")

    def test_skips_non_dict_json_without_crashing(self):
        # Top-level JSON that is not an object (array or scalar) must be skipped
        # gracefully rather than aborting extraction of the whole directory.
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "array.json").write_text(json.dumps([{"panels": []}]), encoding="utf-8")
            (Path(tmpdir) / "scalar.json").write_text("123", encoding="utf-8")
            valid = {"uid": "dash-ok", "title": "Real", "panels": []}
            (Path(tmpdir) / "valid.json").write_text(json.dumps(valid), encoding="utf-8")

            dashboards = extract_dashboards_from_files(tmpdir)

        self.assertEqual(len(dashboards), 1)
        self.assertEqual(dashboards[0]["uid"], "dash-ok")


if __name__ == "__main__":
    unittest.main()
