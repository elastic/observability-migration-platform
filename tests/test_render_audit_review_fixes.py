# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for PR #234 review findings on the render-audit driver.

Issue 3: ``fetch_available_fields`` must distinguish a successful-but-empty
field-caps result (``{}`` -> ``set()``) from a fetch failure / no-ES-URL
(``None``), so "schema known, no fields matched" can still attribute field gaps.
"""

from __future__ import annotations

import unittest
from unittest import mock

from observability_migration.targets.kibana import render_audit_driver as rad


class TestFetchAvailableFieldsEmptyCaps(unittest.TestCase):
    def test_empty_caps_returns_empty_set_not_none(self):
        # Successful field-caps call that matched no fields -> set(), so the
        # classifier can prove a field gap ("schema known, field absent").
        with mock.patch.object(rad, "fetch_field_capabilities", return_value={}):
            result = rad.fetch_available_fields("http://es", "key", "metrics-*")
        self.assertEqual(result, set())
        self.assertIsNotNone(result)

    def test_populated_caps_returns_field_set(self):
        with mock.patch.object(
            rad, "fetch_field_capabilities", return_value={"a": object(), "b": object()}
        ):
            result = rad.fetch_available_fields("http://es", "key", "metrics-*")
        self.assertEqual(result, {"a", "b"})

    def test_no_es_url_returns_none(self):
        self.assertIsNone(rad.fetch_available_fields("", "key", "metrics-*"))

    def test_fetch_failure_returns_none(self):
        with mock.patch.object(
            rad, "fetch_field_capabilities", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(rad.fetch_available_fields("http://es", "key", "metrics-*"))


if __name__ == "__main__":
    unittest.main()
