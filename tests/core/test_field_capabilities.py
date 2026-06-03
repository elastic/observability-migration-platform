# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest
from unittest.mock import Mock, patch

from observability_migration.core.verification.field_capabilities import (
    FieldCapability,
    assess_field_usage,
    fetch_field_capabilities,
    field_capability_from_es_field_caps,
    has_conflicting_types,
    is_aggregatable_field,
    is_counter_metric_field,
    is_numeric_field,
    is_searchable_field,
    is_text_like_field,
)


class TestFieldCapabilities(unittest.TestCase):
    def test_field_caps_derives_numeric_counter_capability(self):
        capability = field_capability_from_es_field_caps(
            "http_requests_total",
            {
                "counter_long": {
                    "searchable": True,
                    "aggregatable": True,
                    "time_series_metric": "counter",
                    "indices": ["metrics-a"],
                }
            },
        )
        self.assertEqual(capability.type, "counter_long")
        self.assertEqual(capability.type_family, "numeric")
        self.assertTrue(is_numeric_field(capability))
        self.assertTrue(is_counter_metric_field(capability))
        self.assertTrue(is_searchable_field(capability))
        self.assertTrue(is_aggregatable_field(capability))

    def test_field_caps_preserves_conflicting_types(self):
        capability = field_capability_from_es_field_caps(
            "status",
            {
                "keyword": {"searchable": True, "aggregatable": True, "indices": ["logs-a"]},
                "long": {"searchable": True, "aggregatable": True, "indices": ["logs-b"]},
            },
        )
        self.assertTrue(has_conflicting_types(capability))
        self.assertEqual(capability.conflicting_types, ["keyword", "long"])

    def test_text_like_helper_prefers_text_family_only(self):
        self.assertTrue(is_text_like_field(FieldCapability(name="body.text", type="text")))
        self.assertFalse(is_text_like_field(FieldCapability(name="message", type="keyword")))

    @patch("observability_migration.core.verification.field_capabilities.requests.get")
    def test_fetch_field_capabilities_normalizes_response(self, mock_get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "fields": {
                "system.cpu.user.pct": {
                    "double": {
                        "searchable": True,
                        "aggregatable": True,
                        "indices": ["metrics-prod"],
                    }
                }
            }
        }
        mock_get.return_value = response

        capabilities = fetch_field_capabilities(
            "https://example.es",
            "metrics-*",
            es_api_key="secret",
        )

        self.assertIn("system.cpu.user.pct", capabilities)
        self.assertTrue(is_numeric_field(capabilities["system.cpu.user.pct"]))
        mock_get.assert_called_once_with(
            "https://example.es/metrics-*/_field_caps",
            params={"fields": "*"},
            headers={"Authorization": "ApiKey secret"},
            timeout=10,
            verify=True,
        )


class TestAssessFieldUsage(unittest.TestCase):
    def _make_cap(self, *, type_name="long", searchable=True, aggregatable=True, conflicting=None):
        cap = FieldCapability(name="f", type=type_name)
        cap.searchable = searchable
        cap.aggregatable = aggregatable
        if conflicting:
            cap.conflicting_types = conflicting
        return cap

    def test_missing_field_produces_warning(self):
        result = assess_field_usage(None, field_name="cpu", usage="aggregate")
        self.assertFalse(result.exists)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("not found", result.warnings[0])
        self.assertEqual(result.blocking_reasons, [])

    def test_healthy_numeric_aggregate_no_issues(self):
        cap = self._make_cap(type_name="double")
        result = assess_field_usage(cap, field_name="cpu.pct", usage="aggregate", required_type_family="numeric")
        self.assertTrue(result.exists)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.blocking_reasons, [])

    def test_non_aggregatable_group_by_is_blocking(self):
        cap = self._make_cap(aggregatable=False)
        result = assess_field_usage(cap, field_name="body.text", usage="group_by")
        self.assertTrue(len(result.blocking_reasons) >= 1)
        self.assertIn("not aggregatable", result.blocking_reasons[0])

    def test_conflicting_types_produces_warning(self):
        cap = self._make_cap(conflicting=["keyword", "long"])
        result = assess_field_usage(cap, field_name="status", usage="filter")
        self.assertTrue(any("conflicting" in w for w in result.warnings))
        self.assertEqual(result.blocking_reasons, [])

    def test_non_searchable_filter_produces_warning(self):
        cap = self._make_cap(searchable=False)
        result = assess_field_usage(cap, field_name="internal_id", usage="filter")
        self.assertTrue(any("not searchable" in w for w in result.warnings))

    def test_type_family_mismatch_is_blocking(self):
        cap = self._make_cap(type_name="keyword")
        result = assess_field_usage(
            cap, field_name="host.name", usage="aggregate", required_type_family="numeric"
        )
        self.assertTrue(len(result.blocking_reasons) >= 1)
        self.assertIn("requires 'numeric'", result.blocking_reasons[0])

    def test_unknown_type_family_with_requirement_warns(self):
        cap = FieldCapability(name="f", type="")
        cap.searchable = True
        cap.aggregatable = True
        result = assess_field_usage(
            cap, field_name="custom.field", usage="aggregate", required_type_family="numeric"
        )
        self.assertTrue(any("unknown" in w for w in result.warnings))

    def test_display_name_fallback(self):
        result = assess_field_usage(None, field_name="cpu", usage="filter", display_name="")
        self.assertIn("cpu", result.warnings[0])
        result2 = assess_field_usage(None, field_name="cpu", usage="filter", display_name="CPU metric")
        self.assertIn("CPU metric", result2.warnings[0])


if __name__ == "__main__":
    unittest.main()
