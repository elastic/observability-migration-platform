# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for Prometheus recording-rule metric heuristics."""

from __future__ import annotations

import unittest

from observability_migration.core.metric_mapping import looks_like_recording_rule_metric


class RecordingRuleMetricTests(unittest.TestCase):
    def test_colon_separated_name(self):
        self.assertTrue(looks_like_recording_rule_metric("job:http_requests:rate5m"))

    def test_plain_metric_name(self):
        self.assertFalse(looks_like_recording_rule_metric("http_requests_total"))

    def test_empty_name(self):
        self.assertFalse(looks_like_recording_rule_metric(""))
        self.assertFalse(looks_like_recording_rule_metric("   "))


if __name__ == "__main__":
    unittest.main()
