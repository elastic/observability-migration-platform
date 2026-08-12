# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for Datadog's adaptive FROM/TS bucket-width unification.

See docs/design/datadog-esql-time-bucketing-adaptivity.md.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.datadog import translate


class TestTimeBucketExprHelper(unittest.TestCase):
    def test_default_time_bucket_expr_uses_75(self):
        self.assertEqual(
            translate.TIME_BUCKET_EXPR,
            "BUCKET(@timestamp, 75, ?_tstart, ?_tend)",
        )

    def test_rate_safe_time_bucket_expr_uses_20(self):
        self.assertEqual(
            translate._time_bucket_expr(True),
            "BUCKET(@timestamp, 20, ?_tstart, ?_tend)",
        )

    def test_non_rate_safe_time_bucket_expr_matches_default(self):
        self.assertEqual(
            translate._time_bucket_expr(False),
            translate.TIME_BUCKET_EXPR,
        )
