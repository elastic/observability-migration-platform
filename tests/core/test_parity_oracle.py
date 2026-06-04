# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest

from observability_migration.core.verification import parity_oracle as po


class VerdictTests(unittest.TestCase):
    def test_strict_pass_under_1pct(self):
        c = po.Comparison(expr="x", esql="TS ...", common_series=1, compared_points=5, max_relative_error=0.005)
        self.assertEqual(c.verdict(), "STRICT_PASS")

    def test_fuzzy_pass_under_5pct(self):
        c = po.Comparison(expr="x", esql="TS ...", common_series=1, compared_points=5, max_relative_error=0.03)
        self.assertEqual(c.verdict(), "FUZZY_PASS")

    def test_no_common_series_is_fail(self):
        c = po.Comparison(expr="x", esql="TS ...", common_series=0, compared_points=0)
        self.assertEqual(c.verdict(), "FAIL")

    def test_skip_reason_wins(self):
        c = po.Comparison(expr="x", skipped_reason="translator marked not_feasible")
        self.assertEqual(c.verdict(), "SKIP")

    def test_shape_pass_when_values_diverge_but_series_overlap(self):
        c = po.Comparison(expr="x", esql="TS ...", common_series=1, compared_points=5, max_relative_error=0.2)
        self.assertEqual(c.verdict(), "SHAPE_PASS")

    def test_translated_error_is_error(self):
        c = po.Comparison(expr="x", esql="TS ...", translated_error="boom")
        self.assertEqual(c.verdict(), "ERROR")


class NormalizeAndDiffTests(unittest.TestCase):
    def test_compute_diff_identical_series_zero_error(self):
        a = {po.SeriesKey((("host", "a"),)): [(0.0, 10.0), (60.0, 20.0), (120.0, 30.0), (180.0, 40.0)]}
        b = {po.SeriesKey((("host", "a"),)): [(0.0, 10.0), (60.0, 20.0), (120.0, 30.0), (180.0, 40.0)]}
        points, rmax, _rmean = po.compute_diff(a, b, 60)
        self.assertGreater(points, 0)
        self.assertEqual(rmax, 0.0)

    def test_normalize_native_parses_value_step_columns(self):
        data = {
            "columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}, {"name": "host", "type": "keyword"}],
            "values": [[10.0, "2026-01-01T00:00:00Z", "a"], [20.0, "2026-01-01T00:05:00Z", "a"]],
        }
        out = po.normalize_native(data)
        self.assertEqual(len(out), 1)

    def test_project_to_subset_sum_aligns_to_translated_dims(self):
        native = {
            po.SeriesKey((("dc", "x"), ("host", "a"))): [(0.0, 10.0)],
            po.SeriesKey((("dc", "y"), ("host", "a"))): [(0.0, 5.0)],
        }
        translated = {po.SeriesKey((("host", "a"),)): [(0.0, 15.0)]}
        projected = po._project_to_subset(native, translated)
        self.assertEqual(list(projected.keys()), [po.SeriesKey((("host", "a"),))])
        self.assertEqual(projected[po.SeriesKey((("host", "a"),))], [(0.0, 15.0)])

    def test_normalize_translated_canonicalizes_otel_labels(self):
        data = {
            "columns": [
                {"name": "computed_value", "type": "double"},
                {"name": "time_bucket", "type": "date"},
                {"name": "k8s.namespace.name", "type": "keyword"},
            ],
            "values": [
                [10.0, "2026-01-01T00:00:00Z", "ns1"],
                [20.0, "2026-01-01T00:00:00Z", "ns2"],
            ],
        }
        out = po.normalize_translated(data)
        self.assertEqual(len(out), 2)
        namespaces = sorted(dict(k.labels)["namespace"] for k in out)
        self.assertEqual(namespaces, ["ns1", "ns2"])
