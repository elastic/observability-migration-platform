# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for PR #234 review findings on the render-audit driver.

``fetch_available_fields`` returns a field set ONLY for a non-empty field-caps
result. An empty result (``{}``) is indistinguishable from an absent/unreadable
index (a 200 with no fields), so it returns ``None`` (unknown schema) — a render
marker then stays a hard ``render_error`` rather than being silently downgraded
to a ``field_gap`` we cannot prove (hunt #4: empty/unreachable-but-200 masked
real render errors).
"""

from __future__ import annotations

import unittest
from unittest import mock

from observability_migration.targets.kibana import render_audit_driver as rad


class TestFetchAvailableFieldsEmptyCaps(unittest.TestCase):
    def test_empty_caps_returns_none(self):
        # An empty field-caps result is indistinguishable from an absent/empty
        # index (a 200 with no fields), so it is treated as an UNKNOWN schema
        # (None) -> a render marker stays a hard render_error rather than being
        # downgraded to an unprovable field_gap (hunt #4).
        with mock.patch.object(rad, "fetch_field_capabilities", return_value={}):
            result = rad.fetch_available_fields("http://es", "key", "metrics-*")
        self.assertIsNone(result)

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
