# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for metric_map reporting helpers."""

from __future__ import annotations

import unittest

from observability_migration.core.metric_mapping.reporting import (
    attach_metric_map_to_contract,
    build_metric_map_summary,
    metric_map_summary_from_tracker,
)


class BuildMetricMapSummaryTests(unittest.TestCase):
    def test_summary_shape(self):
        summary = build_metric_map_summary(
            applied={"a.metric": "a.metric.pct"},
            gaps=["missing variant"],
            warnings=["class-2 mapping applied"],
        )
        self.assertEqual(summary["applied"], [{"source": "a.metric", "target": "a.metric.pct"}])
        self.assertEqual(summary["gaps"], ["missing variant"])
        self.assertEqual(summary["warnings"], ["class-2 mapping applied"])
        self.assertEqual(
            summary["totals"],
            {"applied": 1, "gaps": 1, "warnings": 1},
        )


class MetricMapSummaryFromTrackerTests(unittest.TestCase):
    def test_tracker_summary(self):
        class Tracker:
            def metric_map_applied(self):
                return {"src": "dst"}

            def metric_map_gaps(self):
                return ["gap"]

            def metric_map_warnings(self):
                return ["warn"]

        summary = metric_map_summary_from_tracker(Tracker())
        assert summary is not None
        self.assertEqual(summary["totals"]["applied"], 1)
        self.assertEqual(summary["gaps"], ["gap"])
        self.assertEqual(summary["warnings"], ["warn"])

    def test_attach_to_contract(self):
        class Tracker:
            metric_map = {"src": "dst"}

            def metric_map_applied(self):
                return {}

            def metric_map_gaps(self):
                return []

            def metric_map_warnings(self):
                return []

        contract = {"required_fields": {}}
        attach_metric_map_to_contract(contract, Tracker())
        self.assertIn("metric_map", contract)
        self.assertIn("totals", contract["metric_map"])


if __name__ == "__main__":
    unittest.main()
