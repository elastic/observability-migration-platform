# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the shared Kibana space-URL helpers.

The dashboard-YAML artifact surfaces this module used to host (rendering YAML,
shelling out to ``kb-dashboard-cli compile``, the saved-objects ``_import``
upload, and the lint/layout wrappers around them) are gone: a migration writes
``native/*.native.json`` and uploads through the typed Dashboards API. What
remains here is space-URL derivation, so that is what is covered.
"""

import unittest

from observability_migration.targets.kibana import compile as shared_compile


class TestSharedCompileBehavior(unittest.TestCase):
    def test_detect_space_id_from_url_without_space_returns_empty(self):
        self.assertEqual(shared_compile.detect_space_id_from_kibana_url("http://localhost:5601"), "")

    def test_no_repo_root_helper(self):
        # Nothing here may locate the repo checkout at runtime: the installed
        # CLI has no checkout to find.
        self.assertFalse(hasattr(shared_compile, "_repo_root"))

    def test_sync_esql_panel_fields_rebuilds_long_form_xy_breakdown(self):
        yaml_panel = {
            "title": "CPU Basic",
            "esql": {
                "type": "area",
                "query": "TS metrics-* | STATS Busy_System = AVG(v) BY time_bucket = TBUCKET(10, ?_tstart, ?_tend) | KEEP time_bucket, Busy_System",
                "dimension": {"field": "time_bucket", "data_type": "date"},
                "metrics": [
                    {"field": "Busy_System", "format": {"type": "percent"}},
                    {"field": "Busy_User", "format": {"type": "percent"}},
                ],
                "mode": "percentage",
            },
        }
        new_query = (
            "TS metrics-* "
            "| STATS Busy_System = AVG(v) BY time_bucket = TBUCKET(10, ?_tstart, ?_tend) "
            "| EVAL series_group = \"Busy System\", value = Busy_System "
            "| KEEP time_bucket, series_group, value"
        )

        changed = shared_compile._sync_esql_panel_fields(
            yaml_panel,
            yaml_panel["esql"]["query"],
            new_query,
        )

        self.assertTrue(changed)
        esql = yaml_panel["esql"]
        self.assertEqual(esql["dimension"], {"field": "time_bucket", "data_type": "date"})
        self.assertEqual(esql["breakdown"], {"field": "series_group"})
        self.assertEqual(
            esql["metrics"],
            [{"field": "value", "format": {"type": "percent"}}],
        )

    def test_sync_esql_panel_fields_keeps_time_dimension_metadata_when_query_changes(self):
        yaml_panel = {
            "title": "Traffic",
            "esql": {
                "type": "line",
                "query": "TS metrics-* | STATS recv = AVG(v) BY bucket = TBUCKET(10, ?_tstart, ?_tend) | KEEP bucket, recv",
                "dimension": {"field": "bucket"},
                "metrics": [{"field": "recv"}],
            },
        }
        new_query = (
            "TS metrics-* "
            "| STATS recv = AVG(v) BY time_bucket = TBUCKET(10, ?_tstart, ?_tend), labels.device "
            "| KEEP time_bucket, labels.device, recv"
        )

        changed = shared_compile._sync_esql_panel_fields(
            yaml_panel,
            yaml_panel["esql"]["query"],
            new_query,
        )

        self.assertTrue(changed)
        self.assertEqual(
            yaml_panel["esql"]["dimension"],
            {"field": "time_bucket", "data_type": "date"},
        )


if __name__ == "__main__":
    unittest.main()
